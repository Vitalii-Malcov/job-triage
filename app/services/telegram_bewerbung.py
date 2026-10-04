"""Stage 9B: Telegram "✅ Bewerbung erstellen" -> durable, idempotent
Bewerbung DRAFT preparation and a truthful preview in the operator's
private chat. Draft-only: nothing here sends an application or an email,
creates a Gmail draft, touches Stage 6E review/approval, or writes
`JobRecord.status` or Stage 9A review state.

**Reuse, not a second pipeline.** The package is the existing 6B match,
6C tailored CV and 6D letter, produced by the existing services
(`prepare_candidate_job_match`, `prepare_candidate_cv_draft_with_outcome`,
`BewerbungService` with the deterministic provider pinned explicitly).
This module adds only the Telegram workflow around them:

1. **Eligibility** (read-only): review state, job status NEW/SAVED, an
   existing candidate profile (fail-closed `get_candidate_profile`, never
   bootstrapped) with a trusted name and at least one trusted
   skill/project/experience.
2. **Ownership**: a generation + token claim on the preparation ledger
   (`app.db.telegram_bewerbung_repository`). A fresh PREPARING claim makes
   every other press "busy"; a stale one is recovered to FAILED first.
3. **Preparation**: 6B and 6C are prepared under their own unique cache
   identities (their commits are safe to keep). Then ONE final short
   transaction locks the profile and job rows (in that order -- the same
   order Stage 12's offline rescore uses), adopts the already-pinned or a
   suitable existing letter via the shared `bewerbung_draft_is_current`
   identity, or flushes a NEW letter without committing, re-verifies the
   inputs, and publishes PREPARED by token+generation CAS. Letter and
   publication commit together; if ownership or inputs changed, both roll
   back, so a stale worker never leaves a letter behind.
4. **Preview**: the frozen summary is sent through the Stage 9A-style
   delivery claim (`SENDING` committed before the HTTP request, resolved by
   token; ambiguous results are UNCERTAIN and never retried automatically).

The only network call is `send_telegram_message` to the configured
(private) chat. No DB lock is held across it.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.bewerbung_generator import BEWERBUNG_GENERATOR_VERSION
from app.agents.candidate_job_matcher import ALGORITHM_VERSION
from app.agents.cv_adapter import CV_ADAPTER_VERSION
from app.db.bewerbung_repository import (
    get_bewerbung_draft_by_id,
    get_latest_bewerbung_draft,
    to_bewerbung_draft,
)
from app.db.candidate_cv_draft_repository import get_draft_by_id, to_tailored_cv_draft
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    get_match_by_id,
    to_candidate_job_match,
)
from app.db.candidate_profile_repository import (
    get_candidate_profile,
    to_candidate_profile_response,
)
from app.db.models import (
    BewerbungDraftRecord,
    CandidateCVDraftRecord,
    CandidateJobMatchRecord,
    CandidateProfileRecord,
    JobRecord,
    TelegramBewerbungPreparationRecord,
    TelegramVacancyReviewRecord,
)
from app.db.telegram_bewerbung_repository import (
    MAX_PREPARATION_ATTEMPTS,
    PreparationClaim,
    claim_preparation,
    claim_preview,
    create_preparation_claim,
    fail_preparation,
    get_preparation_by_package_token,
    get_preparation_for_review,
    publish_preparation,
    reconcile_stale_preparation,
    reconcile_stale_preview,
    resolve_preview,
)
from app.models.candidate_profile import (
    is_top_level_fact_usable_for_generation,
    is_usable_for_generation,
)
from app.models.cv_draft import TailoredCVDraft
from app.providers.bewerbung.deterministic import PROVIDER_NAME as DETERMINISTIC_PROVIDER
from app.providers.bewerbung.deterministic import DeterministicBewerbungProvider
from app.services.bewerbung import BewerbungService
from app.services.bewerbung_reuse import bewerbung_draft_is_current
from app.services.candidate_preparation import (
    prepare_candidate_cv_draft_with_outcome,
    prepare_candidate_job_match,
)
from app.services.telegram import TelegramSendOutcome, send_telegram_message
from app.services.telegram_bewerbung_preview import (
    MAX_LETTER_PAGES,
    PREVIEW_RENDERER_VERSION,
    paginate_letter,
    render_summary,
)

logger = logging.getLogger(__name__)

# Stage 9A review states from which a card can legitimately have been seen
# and pressed. SKIPPED is allowed (Skip is a review decision, not a ban) and
# stays SKIPPED -- this module never writes review state.
ELIGIBLE_REVIEW_STATES = frozenset({"TELEGRAM_SENT", "DELIVERY_UNCERTAIN", "SAVED", "SKIPPED"})
ELIGIBLE_JOB_STATUSES = frozenset({"NEW", "SAVED"})

PREVIEW_CALLBACK_PREFIX = "bp"
# Stage 9C "✅ Zur Prüfung" request (handled by app.services.
# telegram_bewerbung_approval). Defined here so this Stage 9B module never
# imports Stage 9C/6E code; the button only carries the package capability.
REVIEW_REQUEST_CALLBACK_PREFIX = "ba"
_PAGE_DIGITS_MAX = 2

# Preparation failures caused by the inputs themselves: retrying the same
# input identity cannot succeed, so a press reports them without spending
# another attempt. A changed profile/job yields a new identity and budget.
_INPUT_BOUND_ERRORS = frozenset({"NO_RELEVANT_EVIDENCE"})

# Apply may (re)send the summary only if it was never delivered or definitely
# failed. Vorschau is an explicit redisplay request and may follow SENT or
# UNCERTAIN, but never takes over an in-flight SENDING.
_APPLY_PREVIEW_STATES = ("NONE", "FAILED")
_EXPLICIT_PREVIEW_STATES = ("NONE", "FAILED", "SENT", "UNCERTAIN")

Sender = Callable[..., Awaitable]

_NOTHING_SENT = "Nichts wurde gesendet."

NOTICES: dict[str, str] = {
    "UNKNOWN_REVIEW": "Unbekannte oder abgelaufene Stelle. " + _NOTHING_SENT,
    "REVIEW_NOT_ELIGIBLE": (
        "Für diese Stelle ist gerade kein Bewerbungsentwurf möglich. " + _NOTHING_SENT
    ),
    "JOB_MISSING": "Die Stelle existiert nicht mehr. " + _NOTHING_SENT,
    "JOB_NOT_ELIGIBLE": (
        "Für diese Stelle wird kein Entwurf erstellt (Status nicht NEW/SAVED). " + _NOTHING_SENT
    ),
    "NO_PROFILE": (
        "Kein Kandidatenprofil vorhanden. Bitte zuerst ein Profil anlegen und Angaben "
        "bestätigen. Es wurde kein Entwurf erstellt oder gesendet."
    ),
    "PROFILE_NOT_READY": (
        "Es fehlen bestätigte Angaben für einen verlässlichen Entwurf. Bitte Name und "
        "relevante Kenntnisse oder Projekte ergänzen. " + _NOTHING_SENT
    ),
    "NO_RELEVANT_EVIDENCE": (
        "Im Profil sind keine bestätigten Kenntnisse, Projekte oder Erfahrungen belegt, die "
        "zu dieser Stelle passen. Es wurde kein Entwurf erstellt. " + _NOTHING_SENT
    ),
    "PROFILE_CHANGED": "Das Profil hat sich geändert. Bitte erneut versuchen; " + _NOTHING_SENT,
    "JOB_CHANGED": "Die Stelle hat sich geändert. Bitte erneut versuchen; " + _NOTHING_SENT,
    "BUSY": "Der Entwurf wird bereits vorbereitet. " + _NOTHING_SENT,
    "PREVIEW_BUSY": "Die Vorschau wird gerade gesendet. Bitte kurz warten.",
    "ATTEMPTS_EXHAUSTED": (
        f"Die Vorbereitung ist {MAX_PREPARATION_ATTEMPTS}-mal fehlgeschlagen und wird für "
        "diese Angaben nicht erneut versucht. Erst nach einer Änderung an Profil oder Stelle "
        "ist ein neuer Versuch möglich. " + _NOTHING_SENT
    ),
    "PREPARATION_FAILED": (
        "Die Vorbereitung ist fehlgeschlagen. Ein erneuter Versuch ist möglich. " + _NOTHING_SENT
    ),
    "PREPARATION_LOST": "Der Entwurf wurde inzwischen neu vorbereitet. " + _NOTHING_SENT,
    "ALREADY_SHOWN": (
        "Der Entwurf ist bereits vorbereitet und wurde angezeigt. 👁 Vorschau zeigt das "
        "Anschreiben erneut. Status: ENTWURF — NICHT GESENDET."
    ),
    "PREVIEW_UNCERTAIN": (
        "Die Vorschau wurde möglicherweise bereits zugestellt; sie wird nicht automatisch "
        "erneut gesendet. 👁 Vorschau zeigt sie auf Wunsch erneut. Status: ENTWURF — NICHT "
        "GESENDET."
    ),
    "PREVIEW_FAILED": (
        "Die Vorschau konnte nicht gesendet werden. Erneut drücken, um es nochmals zu "
        "versuchen. Status: ENTWURF — NICHT GESENDET."
    ),
    "PREVIEW_EXPIRED": (
        "Diese Vorschau ist abgelaufen, weil der Entwurf inzwischen neu vorbereitet wurde. "
        + _NOTHING_SENT
    ),
    "PACKAGE_UNAVAILABLE": "Der Entwurf ist nicht mehr vollständig verfügbar. " + _NOTHING_SENT,
    "INVALID_PAGE": "Diese Seite gibt es nicht.",
}


@dataclass(frozen=True)
class BewerbungOutcome:
    """What happened, plus an optional short notice for the operator's chat
    (no candidate data) and its optional keyboard."""

    code: str
    notice: str | None = None
    notice_markup: dict | None = None


def _outcome(
    code: str, *, package_token: str | None = None, approval_enabled: bool = False
) -> BewerbungOutcome:
    markup = (
        preview_keyboard(package_token, approval_enabled=approval_enabled)
        if package_token
        else None
    )
    return BewerbungOutcome(code, NOTICES.get(code), markup)


def approval_flags_enabled(settings) -> bool:
    """Stage 9C is effective only when BOTH the 9B draft flag and the 9C
    approval flag are on."""
    return bool(
        getattr(settings, "telegram_bewerbung_draft_enabled", False)
        and getattr(settings, "telegram_bewerbung_approval_enabled", False)
    )


# --- callback data ---------------------------------------------------------


def build_preview_callback_data(package_token: str, page: int) -> str:
    return f"{PREVIEW_CALLBACK_PREFIX}:{package_token}:{page}"


def parse_preview_callback_data(data: str | None) -> tuple[str, int] | None:
    """Strictly parse `bp:<package_token>:<page>`; anything else is None.
    Token SHAPE is validated by the repository lookup; the page must be a
    bounded positive integer."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != PREVIEW_CALLBACK_PREFIX:
        return None
    page_text = parts[2]
    if not page_text.isdigit() or len(page_text) > _PAGE_DIGITS_MAX or not page_text.isascii():
        return None
    page = int(page_text)
    if not 1 <= page <= MAX_LETTER_PAGES:
        return None
    return parts[1], page


def review_request_button(package_token: str) -> dict:
    """Stage 9C: request review of exactly this package (no DB ids)."""
    return {
        "text": "✅ Zur Prüfung",
        "callback_data": f"{REVIEW_REQUEST_CALLBACK_PREFIX}:{package_token}",
    }


def preview_keyboard(package_token: str, *, approval_enabled: bool = False) -> dict:
    rows = [
        [
            {
                "text": "👁 Vorschau",
                "callback_data": build_preview_callback_data(package_token, 1),
            }
        ]
    ]
    if approval_enabled:
        rows.append([review_request_button(package_token)])
    return {"inline_keyboard": rows}


def page_keyboard(
    package_token: str, page: int, total: int, *, approval_enabled: bool = False
) -> dict | None:
    buttons = []
    if page > 1:
        buttons.append(
            {
                "text": "◀ Zurück",
                "callback_data": build_preview_callback_data(package_token, page - 1),
            }
        )
    if page < total:
        buttons.append(
            {
                "text": "Weiter ▶",
                "callback_data": build_preview_callback_data(package_token, page + 1),
            }
        )
    rows = [buttons] if buttons else []
    if approval_enabled:
        rows.append([review_request_button(package_token)])
    return {"inline_keyboard": rows} if rows else None


# --- inputs ----------------------------------------------------------------


def compute_input_identity(job: JobRecord, profile_version: int) -> str:
    """Digest of everything the package is prepared from: the job's
    matching fingerprint AND exact display context, the candidate profile
    version, and every algorithm/renderer version. A changed digest means
    the published package is no longer current."""
    payload = [
        job.id,
        profile_version,
        compute_job_snapshot_fingerprint(job),
        job.title,
        job.company,
        ALGORITHM_VERSION,
        CV_ADAPTER_VERSION,
        BEWERBUNG_GENERATOR_VERSION,
        DETERMINISTIC_PROVIDER,
        PREVIEW_RENDERER_VERSION,
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def profile_readiness_problem(record: CandidateProfileRecord) -> str | None:
    """Minimum readiness under the project's trust rule (source AND
    confidence, `is_usable_for_generation`): a trusted non-blank first or
    last name, and at least one trusted skill, project or experience."""
    profile = to_candidate_profile_response(record)
    has_name = any(
        is_top_level_fact_usable_for_generation(profile, field)
        and (getattr(profile, field) or "").strip()
        for field in ("first_name", "last_name")
    )
    if not has_name:
        return "PROFILE_NO_TRUSTED_NAME"
    evidence = [*profile.skills, *profile.projects, *profile.experiences]
    if not any(is_usable_for_generation(item.source, item.confidence) for item in evidence):
        return "PROFILE_NO_TRUSTED_EVIDENCE"
    return None


def _has_relevant_evidence(cv: TailoredCVDraft) -> bool:
    """The pinned CV must carry at least one item relevant to THIS job:
    a matched skill, a match-relevant project, or an experience with
    matched skills (6C already applied the trust rule to all of them)."""
    return bool(cv.skills or cv.projects or any(exp.matched_skills for exp in cv.experience))


def _job_for_update(db: Session, job_id: int) -> JobRecord | None:
    stmt = (
        select(JobRecord)
        .where(JobRecord.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.scalar(stmt)


def _review(db: Session, review_id: int) -> TelegramVacancyReviewRecord | None:
    stmt = (
        select(TelegramVacancyReviewRecord)
        .where(TelegramVacancyReviewRecord.id == review_id)
        .execution_options(populate_existing=True)
    )
    return db.scalar(stmt)


def _reload_preparation(db: Session, preparation_id: int) -> TelegramBewerbungPreparationRecord:
    stmt = (
        select(TelegramBewerbungPreparationRecord)
        .where(TelegramBewerbungPreparationRecord.id == preparation_id)
        .execution_options(populate_existing=True)
    )
    return db.scalar(stmt)


@dataclass(frozen=True)
class _ExpectedPackage:
    """The exact package a preview capability authorized, captured once at
    token lookup. It stays authoritative for the whole request: a reloaded
    row must match it exactly, and it is never re-derived from a reload."""

    preparation_id: int
    generation: int
    package_token: str
    match_id: int
    cv_draft_id: int
    bewerbung_draft_id: int
    preview_renderer_version: str | None

    @classmethod
    def of(cls, prep: TelegramBewerbungPreparationRecord) -> "_ExpectedPackage":
        return cls(
            prep.id,
            prep.generation,
            prep.package_token,
            prep.match_id,
            prep.cv_draft_id,
            prep.bewerbung_draft_id,
            prep.preview_renderer_version,
        )

    def matches(self, prep: TelegramBewerbungPreparationRecord | None) -> bool:
        return prep is not None and prep.state == "PREPARED" and _ExpectedPackage.of(prep) == self


@dataclass(frozen=True)
class _Package:
    match: CandidateJobMatchRecord
    cv: CandidateCVDraftRecord
    letter: BewerbungDraftRecord


def _load_pinned_package(
    db: Session, prep: TelegramBewerbungPreparationRecord, job: JobRecord
) -> _Package | None:
    """The pinned artifacts, only if they all still exist and belong
    together and to `job`. A dangling or inconsistent pin fails closed."""
    if prep.match_id is None or prep.cv_draft_id is None or prep.bewerbung_draft_id is None:
        return None
    match = get_match_by_id(db, prep.match_id)
    cv = get_draft_by_id(db, prep.cv_draft_id)
    letter = get_bewerbung_draft_by_id(db, prep.bewerbung_draft_id)
    if match is None or cv is None or letter is None:
        return None
    consistent = (
        match.job_id == job.id
        and cv.job_id == job.id
        and letter.job_id == job.id
        and cv.match_id == match.id
        and letter.cv_draft_id == cv.id
        and letter.match_id == match.id
    )
    return _Package(match, cv, letter) if consistent else None


def _publication_problem(
    review: TelegramVacancyReviewRecord | None,
    job: JobRecord | None,
    profile: CandidateProfileRecord | None,
    *,
    input_identity: str,
    job_id: int,
    prepared_profile_version: int,
) -> str | None:
    """Fresh re-validation of every publication input. Returns a fixed
    error code, or None when the package may be published."""
    if review is None or review.job_id != job_id:
        return "REVIEW_CHANGED"
    if job is None:
        return "JOB_MISSING"
    if job.status not in ELIGIBLE_JOB_STATUSES:
        return "JOB_NOT_ELIGIBLE"
    if (
        profile is None
        or profile.profile_version != prepared_profile_version
        or profile_readiness_problem(profile) is not None
    ):
        return "PROFILE_CHANGED"
    if compute_input_identity(job, profile.profile_version) != input_identity:
        # Profile version is unchanged (checked above), so the job moved.
        return "JOB_CHANGED"
    return None


# --- preparation -----------------------------------------------------------


async def _prepare_owned(
    db: Session,
    claim: PreparationClaim,
    *,
    review_id: int,
    job_id: int,
    input_identity: str,
    prior_letter_id: int | None,
) -> str:
    """Run one owned preparation generation. Returns "PREPARED", "LOST"
    (ownership moved on -- nothing of ours was committed after the claim),
    or a FAILED code (the owned claim was resolved to FAILED)."""
    # 6B and 6C: existing services, existing unique cache identities. Their
    # own commits are safe to keep even if this generation later loses.
    try:
        match = prepare_candidate_job_match(db, job_id, force_recompute=False)
        if match is None:
            raise LookupError("job vanished")
        cv_result = prepare_candidate_cv_draft_with_outcome(
            db, job_id, match.id, force_recompute=False
        )
        if cv_result is None:
            raise LookupError("job vanished")
        cv_draft, _created = cv_result
    except Exception as exc:
        db.rollback()
        code = "JOB_MISSING" if isinstance(exc, LookupError) else type(exc).__name__
        fail_preparation(db, claim, error_code=code)
        return code
    if not _has_relevant_evidence(cv_draft):
        fail_preparation(db, claim, error_code="NO_RELEVANT_EVIDENCE")
        return "NO_RELEVANT_EVIDENCE"

    # Final short transaction: lock profile then job, adopt or flush the
    # letter, re-verify, publish by CAS -- one commit or one rollback.
    db.expire_all()
    try:
        profile = get_candidate_profile(db, for_update=True)
        job = _job_for_update(db, job_id)
        checks = dict(
            input_identity=input_identity,
            job_id=job_id,
            prepared_profile_version=cv_draft.candidate_profile_version,
        )
        problem = _publication_problem(_review(db, review_id), job, profile, **checks)
        cv_record = get_draft_by_id(db, cv_draft.id)
        match_record = get_match_by_id(db, cv_record.match_id) if cv_record else None
        if problem is None and (
            cv_record is None or match_record is None or match_record.job_id != job_id
        ):
            problem = "PACKAGE_INCONSISTENT"
        if problem is not None:
            db.rollback()
            fail_preparation(db, claim, error_code=problem)
            return problem

        # Prefer the letter this ledger already pinned, then a suitable
        # existing one; never "latest" without the shared current check.
        letter = None
        if prior_letter_id is not None:
            pinned = get_bewerbung_draft_by_id(db, prior_letter_id)
            if bewerbung_draft_is_current(pinned, cv_record, job):
                letter = pinned
        if letter is None:
            latest = get_latest_bewerbung_draft(db, job_id)
            if bewerbung_draft_is_current(latest, cv_record, job):
                letter = latest
        if letter is None:
            service = BewerbungService(DeterministicBewerbungProvider())
            draft = await service.generate(db, job, cv_record.id, commit=False)
            letter = get_bewerbung_draft_by_id(db, draft.id)

        # Re-verify with FRESH reads immediately before publication: under
        # PostgreSQL the row locks above already pin profile and job; this
        # also covers databases without row locks (SQLite in tests).
        job = _job_for_update(db, job_id)
        profile = get_candidate_profile(db, for_update=True)
        if profile is not None:
            db.refresh(profile)
        problem = _publication_problem(_review(db, review_id), job, profile, **checks)
        if problem is not None:
            db.rollback()
            fail_preparation(db, claim, error_code=problem)
            return problem

        preview_text = render_summary(
            to_bewerbung_draft(letter),
            to_tailored_cv_draft(cv_record),
            to_candidate_job_match(match_record),
        )
        package_token = publish_preparation(
            db,
            claim,
            match_id=match_record.id,
            cv_draft_id=cv_record.id,
            bewerbung_draft_id=letter.id,
            preview_text=preview_text,
            preview_renderer_version=PREVIEW_RENDERER_VERSION,
        )
        if package_token is None:
            # Ownership lost: roll back -- a provisional letter flushed
            # above disappears with it.
            db.rollback()
            logger.info(
                "telegram_bewerbung_publication_lost preparation_id=%s generation=%s",
                claim.preparation_id,
                claim.generation,
            )
            return "LOST"
        db.commit()
    except Exception as exc:
        db.rollback()
        code = type(exc).__name__
        fail_preparation(db, claim, error_code=code)
        logger.warning(
            "telegram_bewerbung_preparation_failed preparation_id=%s error_type=%s",
            claim.preparation_id,
            code,
        )
        return code

    logger.info(
        "telegram_bewerbung_prepared preparation_id=%s generation=%s bewerbung_draft_id=%s",
        claim.preparation_id,
        claim.generation,
        letter.id,
    )
    return "PREPARED"


# --- preview delivery ------------------------------------------------------


_SEND_OUTCOME_TO_PREVIEW_STATE = {
    TelegramSendOutcome.SENT: "SENT",
    TelegramSendOutcome.FAILED: "FAILED",
    TelegramSendOutcome.UNCERTAIN: "UNCERTAIN",
}


async def _deliver(
    db: Session,
    settings,
    prep: TelegramBewerbungPreparationRecord,
    *,
    text: str,
    reply_markup: dict | None,
    allowed_states: tuple[str, ...],
    send: Sender,
    expected: _ExpectedPackage | None = None,
) -> str:
    """Claim the preview delivery (committed BEFORE networking), send once,
    resolve by token. Returns the resulting preview state, or "BUSY" if
    another delivery holds the claim. No DB lock is held during the send.
    With `expected`, the claim is bound to that exact generation/package."""
    claim = claim_preview(
        db,
        prep,
        allowed_states=allowed_states,
        expected_generation=expected.generation if expected else None,
        expected_package_token=expected.package_token if expected else None,
    )
    if claim is None:
        return "BUSY"
    try:
        result = await send(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            text,
            reply_markup=reply_markup,
            timeout_seconds=settings.telegram_timeout_seconds,
        )
        state = _SEND_OUTCOME_TO_PREVIEW_STATE[result.outcome]
        message_id = result.message_id
    except Exception as exc:
        # The request may have reached Telegram: never assume it did not.
        logger.warning(
            "telegram_bewerbung_preview_send_error preparation_id=%s error_type=%s",
            claim.preparation_id,
            type(exc).__name__,
        )
        state, message_id = "UNCERTAIN", None
    resolved = resolve_preview(db, claim, state, message_id=message_id)
    logger.info(
        "telegram_bewerbung_preview preparation_id=%s generation=%s state=%s resolved=%s",
        claim.preparation_id,
        claim.generation,
        state,
        resolved,
    )
    return state


def _recover_stale(db: Session, prep: TelegramBewerbungPreparationRecord):
    """Resolve an expired preview send to UNCERTAIN and an expired
    preparation claim to FAILED (each only for the exact observed claim)."""
    if prep.preview_state == "SENDING" and reconcile_stale_preview(db, prep):
        prep = _reload_preparation(db, prep.id)
    if prep.state == "PREPARING" and reconcile_stale_preparation(db, prep):
        prep = _reload_preparation(db, prep.id)
    return prep


def _job_still_eligible(db: Session, job_id: int) -> JobRecord | None:
    db.expire_all()
    job = db.get(JobRecord, job_id)
    if job is None or job.status not in ELIGIBLE_JOB_STATUSES:
        return None
    return job


async def _offer_summary(
    db: Session,
    settings,
    prep: TelegramBewerbungPreparationRecord,
    job_id: int,
    send: Sender,
) -> BewerbungOutcome:
    """A current package exists: send its frozen summary once (NONE or a
    definite FAILED), never automatically again after SENT/UNCERTAIN."""
    package_token = prep.package_token
    if prep.preview_state == "SENT":
        return _outcome(
            "ALREADY_SHOWN",
            package_token=package_token,
            approval_enabled=approval_flags_enabled(settings),
        )
    if prep.preview_state == "UNCERTAIN":
        return _outcome(
            "PREVIEW_UNCERTAIN",
            package_token=package_token,
            approval_enabled=approval_flags_enabled(settings),
        )
    if prep.preview_state == "SENDING":
        return _outcome("PREVIEW_BUSY")
    if _job_still_eligible(db, job_id) is None:
        return _outcome("JOB_NOT_ELIGIBLE")
    prep = _reload_preparation(db, prep.id)
    state = await _deliver(
        db,
        settings,
        prep,
        text=prep.preview_text,
        reply_markup=preview_keyboard(
            prep.package_token, approval_enabled=approval_flags_enabled(settings)
        ),
        allowed_states=_APPLY_PREVIEW_STATES,
        send=send,
    )
    if state == "SENT":
        return BewerbungOutcome("PREVIEW_SENT")
    if state == "BUSY":
        return _outcome("PREVIEW_BUSY")
    if state == "FAILED":
        return _outcome("PREVIEW_FAILED")
    return _outcome(
        "PREVIEW_UNCERTAIN",
        package_token=package_token,
        approval_enabled=approval_flags_enabled(settings),
    )


# --- entry points ----------------------------------------------------------


async def handle_apply(
    session_factory: Callable[[], Session],
    settings,
    review_id: int,
    *,
    send: Sender = send_telegram_message,
) -> BewerbungOutcome:
    """The Bewerbung-erstellen press for one review row, already resolved
    from its opaque callback token by an authorized, private-chat callback.
    Never writes review state, `JobRecord.status` or Stage 6E."""
    db = session_factory()
    try:
        review = _review(db, review_id)
        if review is None:
            return _outcome("UNKNOWN_REVIEW")
        if review.state not in ELIGIBLE_REVIEW_STATES:
            return _outcome("REVIEW_NOT_ELIGIBLE")
        job_id = review.job_id
        job = db.get(JobRecord, job_id)
        if job is None:
            return _outcome("JOB_MISSING")
        if job.status not in ELIGIBLE_JOB_STATUSES:
            return _outcome("JOB_NOT_ELIGIBLE")
        profile = get_candidate_profile(db)
        if profile is None:
            return _outcome("NO_PROFILE")
        if profile_readiness_problem(profile) is not None:
            return _outcome("PROFILE_NOT_READY")
        identity = compute_input_identity(job, profile.profile_version)

        prep = get_preparation_for_review(db, review_id)
        if prep is not None:
            prep = _recover_stale(db, prep)
            if prep.state == "PREPARING":
                return _outcome("BUSY")
            if prep.preview_state == "SENDING":
                return _outcome("PREVIEW_BUSY")
            if (
                prep.state == "PREPARED"
                and prep.input_identity == identity
                and _load_pinned_package(db, prep, job) is not None
            ):
                return await _offer_summary(db, settings, prep, job_id, send)
            if prep.state == "FAILED" and prep.input_identity == identity:
                if prep.last_error in _INPUT_BOUND_ERRORS:
                    return _outcome(prep.last_error)
                if prep.attempt_count >= MAX_PREPARATION_ATTEMPTS:
                    return _outcome("ATTEMPTS_EXHAUSTED")
            prior_letter_id = prep.bewerbung_draft_id
            claim = claim_preparation(db, prep, input_identity=identity)
        else:
            prior_letter_id = None
            claim = create_preparation_claim(db, review_id, input_identity=identity)
        if claim is None:
            return _outcome("BUSY")

        result = await _prepare_owned(
            db,
            claim,
            review_id=review_id,
            job_id=job_id,
            input_identity=identity,
            prior_letter_id=prior_letter_id,
        )
        if result == "LOST":
            return _outcome("PREPARATION_LOST")
        if result != "PREPARED":
            return _outcome(result if result in NOTICES else "PREPARATION_FAILED")
        prep = _reload_preparation(db, claim.preparation_id)
        return await _offer_summary(db, settings, prep, job_id, send)
    finally:
        db.close()


async def handle_preview_page(
    session_factory: Callable[[], Session],
    settings,
    package_token: str,
    page: int,
    *,
    send: Sender = send_telegram_message,
) -> BewerbungOutcome:
    """The read-only Vorschau / page-navigation press. Displays one page of
    the EXACT letter pinned to this capability; an old capability gets an
    expired message, never a newer package. Never generates."""
    db = session_factory()
    try:
        prep = get_preparation_by_package_token(db, package_token)
        if prep is None:
            return _outcome("PREVIEW_EXPIRED")
        prep = _recover_stale(db, prep)
        if prep.state != "PREPARED" or prep.package_token != package_token:
            return _outcome("PREVIEW_EXPIRED")
        if prep.preview_state == "SENDING":
            return _outcome("PREVIEW_BUSY")
        # The capability authorizes exactly this package, for the whole
        # request; a replacement published meanwhile is never adopted.
        expected = _ExpectedPackage.of(prep)
        review = _review(db, prep.review_id)
        if review is None:
            return _outcome("PREVIEW_EXPIRED")
        job = _job_still_eligible(db, review.job_id)
        if job is None:
            return _outcome("JOB_NOT_ELIGIBLE")
        prep = _reload_preparation(db, expected.preparation_id)
        if not expected.matches(prep):
            return _outcome("PREVIEW_EXPIRED")
        package = _load_pinned_package(db, prep, job)
        if package is None:
            return _outcome("PACKAGE_UNAVAILABLE")
        pages = paginate_letter(to_bewerbung_draft(package.letter))
        if page > len(pages):
            return _outcome("INVALID_PAGE")
        state = await _deliver(
            db,
            settings,
            prep,
            text=pages[page - 1],
            reply_markup=page_keyboard(
                expected.package_token,
                page,
                len(pages),
                approval_enabled=approval_flags_enabled(settings),
            ),
            allowed_states=_EXPLICIT_PREVIEW_STATES,
            send=send,
            expected=expected,
        )
        if state == "SENT":
            return BewerbungOutcome("PAGE_SENT")
        if state == "BUSY":
            # The bound claim lost: either a send of this exact package is in
            # flight, or the package was replaced after content selection.
            if not expected.matches(_reload_preparation(db, expected.preparation_id)):
                return _outcome("PREVIEW_EXPIRED")
            return _outcome("PREVIEW_BUSY")
        if state == "FAILED":
            return _outcome("PREVIEW_FAILED")
        return _outcome("PREVIEW_UNCERTAIN")
    finally:
        db.close()
