"""Stage 16: Telegram operator control -- the workflow behind the operator
card's APPROVE / SKIP / DETAILS / LATER buttons.

Telegram-free: this module never talks to Telegram itself. The bot handler
(`app.services.telegram_bot.on_operator_callback`) turns a callback query
into an `OperatorCallback` and acts on the returned `CallbackOutcome`;
outbound cards go through an injected `TelegramOperatorTransport` whose
destination is fixed to the configured operator chat.

**Trust.** `authorize` admits a callback only from the configured operator
chat AND -- when `telegram_operator_user_id` is set -- that user; without a
configured user id only a PRIVATE chat whose id equals the sender's id is
accepted (a group member could otherwise press the buttons). Anything else
is UNAUTHORIZED: nothing is read beyond the callback itself, nothing is
written, and the bot stays silent.

**APPROVE is not submission.** APPROVE records an operator decision and
moves the application PACKAGE_READY -> APPROVED through the Stage 15
`ApplicationTracker` -- in ONE transaction (the decision row is flushed
into the tracker's unit of work and committed by it, or rolled back with
it). Nothing is sent, drafted, posted or submitted; there is no import of
any e-mail, portal or submission code here.

**SKIP / LATER never touch the lifecycle.** SKIP is an operator decision,
not an employer REJECTED and not WITHDRAWN. LATER is non-final.

**Idempotency / conflicts.** A retried callback (same callback-query id) is
recognized by `callback_ref` and changes nothing. A second APPROVE (or
SKIP) press finds the recorded final decision and changes nothing. A final
decision is never silently replaced: a conflicting press is refused with
CONFLICT_REQUIRES_OVERRIDE, and only `override_decision` (operator CLI,
mandatory reason) records a superseding decision. Concurrent presses are
serialized by UNIQUE(idempotency_key).

**Approval vs. concurrent review changes (Codex S16 P1).** An APPROVE never
trusts the readiness read by `load_target`: inside the approval
transaction -- after the decision row is flushed, before the lifecycle
write commits -- `_recheck_review_locked` re-reads the review row with
`SELECT ... FOR UPDATE` (PostgreSQL: waits for, then sees, a concurrent
Stage 6E reject/revision; holding the lock makes later review writes wait
for this commit) and refuses unless the package is still ready with the
same review status, version and revision. On SQLite the flushed INSERT
already holds the database write lock, so the re-read is final.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import telegram_operator_repository as repo
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    get_match_by_id,
    to_candidate_job_match,
)
from app.db.candidate_profile_repository import get_candidate_profile
from app.db.datetime_utils import ensure_utc
from app.db.models import (
    ApplicationPackageReviewRecord,
    CompanyResearchRecord,
    JobRecord,
    TelegramOperatorCardRecord,
)
from app.db.review_package_repository import get_current_revision, get_review_by_id
from app.domain.application_lifecycle import InvalidApplicationTransitionError
from app.models.application_lifecycle import (
    Application,
    ApplicationEventOrigin,
)
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.telegram_operator import (
    CLI_OPERATOR_ACTOR,
    FINAL_DECISIONS,
    TELEGRAM_OPERATOR_ACTOR,
    OperatorAction,
    OperatorDecision,
    TelegramDeliveryState,
)
from app.services.application_tracker import (
    ApplicationConcurrencyError,
    ApplicationInputError,
    ApplicationNotFoundError,
    ApplicationTracker,
)
from app.services.telegram import TelegramSendOutcome
from app.services.telegram_operator_render import (
    HistoryItem,
    MatchSummary,
    OperatorCardView,
    PackageSummary,
    RenderedMessage,
    parse_callback_data,
    render_details,
    render_operator_card,
)
from app.services.telegram_operator_transport import TelegramOperatorTransport

logger = logging.getLogger(__name__)

# Statuses in which an operator decision about a package still makes sense
# (before anything reached the employer).
DECIDABLE_STATUSES = frozenset({S.DISCOVERED, S.MATCHED, S.PACKAGE_READY})
# Card delivery states whose buttons may legitimately be pressed.
PRESSABLE_CARD_STATES = frozenset(
    {TelegramDeliveryState.SENT.value, TelegramDeliveryState.UNCERTAIN.value}
)
APPROVE_REASON = "Operator approved the review package via Telegram (no submission)."
PREVIEW_TOKEN = "PREVIEWxxxxxxxx0"  # 16 chars: same callback size as a real token


class CallbackCode(StrEnum):
    UNAUTHORIZED = "UNAUTHORIZED"
    DISABLED = "DISABLED"
    MALFORMED = "MALFORMED"
    UNKNOWN_CARD = "UNKNOWN_CARD"
    TARGET_MISSING = "TARGET_MISSING"
    STALE_CARD = "STALE_CARD"
    DUPLICATE_CALLBACK = "DUPLICATE_CALLBACK"
    DETAILS = "DETAILS"
    APPROVED = "APPROVED"
    ALREADY_APPROVED = "ALREADY_APPROVED"
    SKIPPED = "SKIPPED"
    ALREADY_SKIPPED = "ALREADY_SKIPPED"
    LATER_RECORDED = "LATER_RECORDED"
    ALREADY_LATER = "ALREADY_LATER"
    CONFLICT_REQUIRES_OVERRIDE = "CONFLICT_REQUIRES_OVERRIDE"
    PACKAGE_NOT_READY = "PACKAGE_NOT_READY"
    NOT_DECIDABLE = "NOT_DECIDABLE"
    STATE_CHANGED = "STATE_CHANGED"


_ANSWERS: dict[CallbackCode, str] = {
    CallbackCode.DISABLED: "Operator control is disabled. Nothing changed.",
    CallbackCode.MALFORMED: "Unknown action. Nothing changed.",
    CallbackCode.UNKNOWN_CARD: "Unknown or expired card. Nothing changed.",
    CallbackCode.TARGET_MISSING: "This application no longer exists. Nothing changed.",
    CallbackCode.STALE_CARD: "This card is outdated (the review package changed). Nothing changed.",
    CallbackCode.DUPLICATE_CALLBACK: "Already processed. Nothing changed.",
    CallbackCode.DETAILS: "",
    CallbackCode.APPROVED: "✅ Approved locally. Nothing was sent to the employer.",
    CallbackCode.ALREADY_APPROVED: "Already approved. Nothing changed.",
    CallbackCode.SKIPPED: "⏭ Skipped. Nothing was sent.",
    CallbackCode.ALREADY_SKIPPED: "Already skipped. Nothing changed.",
    CallbackCode.LATER_RECORDED: "🕒 Marked for later review.",
    CallbackCode.ALREADY_LATER: "Already marked for later. Nothing changed.",
    CallbackCode.STATE_CHANGED: "The application changed meanwhile. Nothing changed.",
}
_ALERT_CODES = frozenset(
    {
        CallbackCode.CONFLICT_REQUIRES_OVERRIDE,
        CallbackCode.PACKAGE_NOT_READY,
        CallbackCode.NOT_DECIDABLE,
        CallbackCode.STALE_CARD,
    }
)


@dataclass(frozen=True)
class OperatorCallback:
    """The trusted-to-be-checked facts of one Telegram callback query."""

    chat_id: str
    chat_type: str | None
    user_id: str | None
    callback_query_id: str
    data: str | None
    message_id: int | None = None


@dataclass(frozen=True)
class CallbackOutcome:
    code: CallbackCode
    # None = stay silent (unauthorized). "" = acknowledge without text.
    answer: str | None
    alert: bool = False
    details_text: str | None = None
    card: RenderedMessage | None = None
    decision_id: int | None = None
    state_changed: bool = False


def _outcome(code: CallbackCode, answer: str | None = None, **kwargs) -> CallbackOutcome:
    text = answer if answer is not None else _ANSWERS.get(code, "Nothing changed.")
    return CallbackOutcome(code=code, answer=text, alert=code in _ALERT_CODES, **kwargs)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


# --- authorization -------------------------------------------------------------------


def authorize(settings, callback: OperatorCallback) -> bool:
    configured_chat = (settings.telegram_chat_id or "").strip()
    if not configured_chat or callback.chat_id != configured_chat:
        return False
    operator_user = (settings.telegram_operator_user_id or "").strip()
    if operator_user:
        return callback.user_id == operator_user
    # No configured operator user: only a private chat, where the chat id
    # IS the sender's user id, identifies the operator.
    return callback.chat_type == "private" and callback.user_id == callback.chat_id


# --- read side: view model ---------------------------------------------------------


def _requirement_label(item: RequirementMatch) -> str:
    return item.requirement.strip() or item.normalized_requirement


def _summarize_match(match: CandidateJobMatch) -> MatchSummary:
    def ordered(items: list[RequirementMatch]) -> list[RequirementMatch]:
        rank = {"REQUIRED": 0, "PREFERRED": 1, "UNKNOWN": 2}
        return sorted(items, key=lambda i: rank.get(i.importance, 3))

    language = [
        i
        for i in (
            *match.matched_requirements,
            *match.partial_requirements,
            *match.missing_requirements,
            *match.unknown_requirements,
        )
        if i.requirement_type == "LANGUAGE"
    ]
    experience = [
        i
        for i in (
            *match.matched_requirements,
            *match.partial_requirements,
            *match.missing_requirements,
            *match.unknown_requirements,
        )
        if i.requirement_type == "EXPERIENCE"
    ]
    return MatchSummary(
        overall=match.overall_score,
        required=match.required_skill_score,
        preferred=match.preferred_skill_score,
        experience=match.experience_support_score,
        matched=tuple(_requirement_label(i) for i in ordered(match.matched_requirements)),
        missing=tuple(_requirement_label(i) for i in ordered(match.missing_requirements)),
        partial=tuple(_requirement_label(i) for i in ordered(match.partial_requirements)),
        unknown_count=len(match.unknown_requirements),
        language_issues=tuple(
            f"{_requirement_label(i)} ({i.match_status.lower()})"
            for i in language
            if i.match_status != "MATCH"
        ),
        language_requirements=tuple(
            f"{_requirement_label(i)} ({i.match_status.lower()})" for i in language
        ),
        experience_requirements=tuple(
            f"{_requirement_label(i)} ({i.match_status.lower()})" for i in experience
        ),
        warnings=tuple(match.warnings),
    )


def assess_package(db: Session, application: Application) -> tuple[PackageSummary, object | None]:
    """Read-only readiness of the application's review package. Returns
    the summary and the review record (or None). Ready requires every
    check; each failed check contributes a reason code."""
    reasons: list[str] = []
    if application.current_status != S.PACKAGE_READY:
        reasons.append(f"STATUS_{application.current_status.value}")
    if application.review_package_id is None:
        reasons.append("NO_REVIEW_PACKAGE")
        return PackageSummary(ready=False, reasons=tuple(reasons)), None
    review = get_review_by_id(db, application.review_package_id)
    if review is None:
        reasons.append("REVIEW_PACKAGE_MISSING")
        return PackageSummary(
            ready=False, reasons=tuple(reasons), review_package_id=application.review_package_id
        ), None
    revision = get_current_revision(db, review)
    if review.status == "REJECTED":
        reasons.append("REVIEW_PACKAGE_REJECTED")
    if revision is None:
        reasons.append("NO_REVISION")
    if application.job_id is not None and review.job_id != application.job_id:
        reasons.append("PACKAGE_JOB_MISMATCH")
    profile = get_candidate_profile(db)
    if profile is None:
        reasons.append("PROFILE_MISSING")
    elif profile.profile_version != review.candidate_profile_version:
        reasons.append("PROFILE_CHANGED")
    job = db.get(JobRecord, review.job_id)
    if job is None:
        reasons.append("JOB_MISSING")
    elif compute_job_snapshot_fingerprint(job) != review.job_snapshot_fingerprint:
        reasons.append("JOB_CHANGED")
    if review.has_manual_overrides and review.status == "PENDING_REVIEW":
        # Approving manual edits needs the explicit acknowledgment of the
        # Stage 6E review flow; Telegram never acknowledges it implicitly.
        reasons.append("MANUAL_EDITS_UNACKNOWLEDGED")
    summary = PackageSummary(
        ready=not reasons,
        reasons=tuple(reasons),
        review_package_id=review.id,
        review_status=review.status,
        review_version=review.review_version,
        revision_id=revision.id if revision is not None else None,
        cv_draft_id=review.cv_draft_id,
        bewerbung_draft_id=review.bewerbung_draft_id,
        has_manual_overrides=review.has_manual_overrides,
    )
    return summary, review


def _research(db: Session, match: CandidateJobMatch | None) -> tuple[str | None, tuple[str, ...]]:
    if match is None or match.company_research_id is None:
        return None, ()
    record = db.get(CompanyResearchRecord, match.company_research_id)
    if record is None:
        return None, ()
    try:
        risks = tuple(str(r) for r in json.loads(record.risk_signals_json or "[]"))
    except (TypeError, ValueError):
        risks = ()
    return (record.short_summary or None), risks


@dataclass(frozen=True)
class DecisionState:
    generation: int
    effective_final: object | None  # ApplicationOperatorDecisionRecord
    pending_later: object | None

    @property
    def display(self) -> OperatorDecision | None:
        if self.effective_final is not None:
            return OperatorDecision(self.effective_final.decision)
        if self.pending_later is not None:
            return OperatorDecision.LATER
        return None


def decision_state(db: Session, key: str) -> DecisionState:
    records = repo.list_decisions_for_target(db, key)
    generation = sum(1 for r in records if r.supersedes_decision_id is not None)
    final = None
    later = None
    for record in records:
        if OperatorDecision(record.decision) in FINAL_DECISIONS:
            final = record
            later = None
        elif final is None:
            later = record
    return DecisionState(generation=generation, effective_final=final, pending_later=later)


@dataclass(frozen=True)
class OperatorTarget:
    application: Application
    view: OperatorCardView
    review: object | None
    key: str


def load_target(db: Session, application_id: int, *, tracker: ApplicationTracker) -> OperatorTarget:
    application = tracker.get_application(db, application_id)
    package, review = assess_package(db, application)
    match = None
    if review is not None:
        match_record = get_match_by_id(db, review.match_id)
        match = to_candidate_job_match(match_record) if match_record is not None else None
    job_id = application.job_id
    if job_id is None and review is not None:
        job_id = review.job_id
    job = db.get(JobRecord, job_id) if job_id is not None else None
    summary, risks = _research(db, match)
    history = tuple(
        HistoryItem(
            sequence=e.sequence,
            event_type=e.event_type.value,
            new_status=e.new_status.value if e.new_status else None,
            origin=e.origin.value,
            at=e.occurred_at or e.recorded_at,
        )
        for e in tracker.get_application_history(db, application_id)
    )
    key = repo.target_key(application.id, application.review_package_id)
    view = OperatorCardView(
        application_id=application.id,
        company=application.company_name,
        role=application.role_title,
        location=application.location,
        source=application.source,
        reference=application.source_reference,
        channel=application.application_channel.value,
        status=application.current_status.value,
        package=package,
        match=_summarize_match(match) if match is not None else None,
        job_url=(job.url if job is not None else None) or application.destination_url,
        first_seen_at=ensure_utc(job.first_seen_at) if job is not None else None,
        research_summary=summary,
        research_risks=risks,
        history=history,
        decision=decision_state(db, key).display,
    )
    return OperatorTarget(application=application, view=view, review=review, key=key)


# --- outbound card -----------------------------------------------------------------


class CardSendCode(StrEnum):
    SENT = "SENT"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    ALREADY_SENT = "ALREADY_SENT"
    RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
    DISABLED = "DISABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_DECIDABLE = "NOT_DECIDABLE"


@dataclass(frozen=True)
class CardPreview:
    application_id: int
    target_key: str
    message: RenderedMessage
    approve_offered: bool
    package: PackageSummary
    status: str
    existing_card_state: str | None
    decision: OperatorDecision | None


@dataclass(frozen=True)
class CardSendResult:
    code: CardSendCode
    card_id: int | None = None
    message_id: int | None = None


def preview_card(
    db: Session,
    application_id: int,
    *,
    tracker: ApplicationTracker | None = None,
    now: datetime | None = None,
) -> CardPreview:
    """Render the card exactly as it would be sent -- read-only (no row is
    written, nothing is sent). Buttons carry a placeholder token of the real
    token's size, never an existing card's token."""
    tracker = tracker or ApplicationTracker()
    target = load_target(db, application_id, tracker=tracker)
    existing = repo.get_card_for_target(db, target.key)
    approve_offered = target.view.package.ready
    # Never the real token: it is a capability and the preview is printed.
    message = render_operator_card(
        target.view, PREVIEW_TOKEN, approve_offered=approve_offered, now=now or _now()
    )
    logger.info("telegram_operator_card_rendered application_id=%s", application_id)
    return CardPreview(
        application_id=application_id,
        target_key=target.key,
        message=message,
        approve_offered=approve_offered,
        package=target.view.package,
        status=target.view.status,
        existing_card_state=existing.delivery_state if existing is not None else None,
        decision=target.view.decision,
    )


async def send_card(
    db: Session,
    settings,
    application_id: int,
    transport: TelegramOperatorTransport,
    *,
    tracker: ApplicationTracker | None = None,
    now: datetime | None = None,
) -> CardSendResult:
    """Send ONE operator card for `application_id` to the operator chat.

    One card per target (application + review package): an existing card
    that was SENT, is SENDING or UNCERTAIN is never sent again; a provably
    FAILED one is retried on the same row while attempts remain. Delivery
    bookkeeping never changes the application."""
    if not settings.telegram_operator_control_enabled:
        return CardSendResult(CardSendCode.DISABLED)
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return CardSendResult(CardSendCode.NOT_CONFIGURED)
    tracker = tracker or ApplicationTracker()
    target = load_target(db, application_id, tracker=tracker)
    if target.application.current_status not in DECIDABLE_STATUSES:
        return CardSendResult(CardSendCode.NOT_DECIDABLE)
    approve_offered = target.view.package.ready
    when = now or _now()

    card = repo.get_card_for_target(db, target.key)
    if card is not None:
        repo.reconcile_stale_card(db, card)
        if card.delivery_state != TelegramDeliveryState.FAILED.value:
            return CardSendResult(CardSendCode.ALREADY_SENT, card_id=card.id)
        message = render_operator_card(
            target.view, card.callback_token, approve_offered=approve_offered, now=when
        )
        if not repo.reclaim_failed_card(
            db, card, approve_offered=approve_offered, card_text_sha256=_sha256(message.text)
        ):
            return CardSendResult(CardSendCode.RETRIES_EXHAUSTED, card_id=card.id)
    else:
        # The token must exist before rendering (it is in the buttons), so
        # insert first with a placeholder digest, then render.
        draft = render_operator_card(
            target.view, PREVIEW_TOKEN, approve_offered=approve_offered, now=when
        )
        try:
            card = repo.insert_sending_card(
                db,
                application_id=target.application.id,
                review_package_id=target.application.review_package_id,
                approve_offered=approve_offered,
                card_text_sha256=_sha256(draft.text),
            )
        except IntegrityError:
            db.rollback()
            existing = repo.get_card_for_target(db, target.key)
            return CardSendResult(
                CardSendCode.ALREADY_SENT, card_id=existing.id if existing else None
            )
        message = render_operator_card(
            target.view, card.callback_token, approve_offered=approve_offered, now=when
        )

    try:
        result = await transport.send(message.text, message.reply_markup)
    except Exception as exc:
        # Raised before any request (e.g. message too long): provably unsent.
        repo.resolve_card(db, card, TelegramDeliveryState.FAILED, error=type(exc).__name__)
        logger.warning(
            "telegram_transport_failed card_id=%s error_type=%s", card.id, type(exc).__name__
        )
        return CardSendResult(CardSendCode.FAILED, card_id=card.id)
    state = {
        TelegramSendOutcome.SENT: TelegramDeliveryState.SENT,
        TelegramSendOutcome.FAILED: TelegramDeliveryState.FAILED,
        TelegramSendOutcome.UNCERTAIN: TelegramDeliveryState.UNCERTAIN,
    }[result.outcome]
    repo.resolve_card(db, card, state, message_id=result.message_id)
    if state is TelegramDeliveryState.SENT:
        logger.info(
            "telegram_operator_message_sent card_id=%s application_id=%s",
            card.id,
            application_id,
        )
    else:
        logger.warning("telegram_transport_failed card_id=%s outcome=%s", card.id, state.value)
    return CardSendResult(CardSendCode(state.value), card_id=card.id, message_id=result.message_id)


# --- callbacks -----------------------------------------------------------------------


def _recheck_review_locked(
    db: Session, application: Application, pinned: PackageSummary
) -> tuple[str, ...]:
    """Inside the approval transaction: lock and re-read the authoritative
    review row, then re-assess readiness. Returns refusal reasons (empty
    means still approvable with exactly the pinned status/version/revision).
    Must run after this transaction has written (SQLite write lock)."""
    if application.review_package_id is None:
        return ("NO_REVIEW_PACKAGE",)
    db.execute(
        select(ApplicationPackageReviewRecord)
        .where(ApplicationPackageReviewRecord.id == application.review_package_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    fresh, _ = assess_package(db, application)
    reasons = list(fresh.reasons)
    if not reasons and (
        fresh.review_status != pinned.review_status
        or fresh.review_version != pinned.review_version
        or fresh.revision_id != pinned.revision_id
    ):
        reasons.append("REVIEW_PACKAGE_CHANGED")
    return tuple(reasons)


def _card_after_change(
    db: Session, card: TelegramOperatorCardRecord, *, tracker: ApplicationTracker, now: datetime
) -> RenderedMessage:
    target = load_target(db, card.application_id, tracker=tracker)
    return render_operator_card(
        target.view, card.callback_token, approve_offered=target.view.package.ready, now=now
    )


def _vacancy_reference(application: Application) -> str | None:
    if application.source_reference is None:
        return None
    return f"{application.source}:{application.source_reference}"[:200]


def handle_callback(
    db: Session,
    settings,
    callback: OperatorCallback,
    *,
    tracker: ApplicationTracker | None = None,
    now: datetime | None = None,
) -> CallbackOutcome:
    """Validate and apply one operator callback. Every refusal leaves the
    database exactly as it was."""
    if not authorize(settings, callback):
        # Never log chat/user ids or the payload of an unauthorized sender.
        logger.warning("telegram_callback_refused reason=unauthorized")
        return CallbackOutcome(code=CallbackCode.UNAUTHORIZED, answer=None)
    if not settings.telegram_operator_control_enabled:
        logger.info("telegram_callback_refused reason=disabled")
        return _outcome(CallbackCode.DISABLED)
    parsed = parse_callback_data(callback.data)
    if parsed is None:
        logger.warning("telegram_callback_refused reason=malformed")
        return _outcome(CallbackCode.MALFORMED)
    action, token = parsed
    tracker = tracker or ApplicationTracker()
    when = now or _now()

    card = repo.get_card_by_token(db, token)
    if card is None or card.delivery_state not in PRESSABLE_CARD_STATES:
        logger.warning("telegram_callback_refused reason=unknown_card")
        return _outcome(CallbackCode.UNKNOWN_CARD)
    logger.info("telegram_callback_received card_id=%s action=%s", card.id, action.value)

    callback_ref = _sha256(f"telegram-callback:{callback.callback_query_id}")
    if action is not OperatorAction.DETAILS:
        prior = repo.get_decision_by_callback_ref(db, callback_ref)
        if prior is not None:
            logger.info("telegram_duplicate_callback card_id=%s", card.id)
            return _outcome(CallbackCode.DUPLICATE_CALLBACK, decision_id=prior.id)

    try:
        target = load_target(db, card.application_id, tracker=tracker)
    except ApplicationNotFoundError:
        logger.warning("telegram_callback_refused reason=target_missing card_id=%s", card.id)
        return _outcome(CallbackCode.TARGET_MISSING)
    application = target.application
    if target.key != card.target_key:
        logger.warning("telegram_callback_refused reason=stale_card card_id=%s", card.id)
        return _outcome(CallbackCode.STALE_CARD)

    if action is OperatorAction.DETAILS:
        logger.info("telegram_callback_details card_id=%s", card.id)
        return _outcome(CallbackCode.DETAILS, details_text=render_details(target.view, now=when))

    state = decision_state(db, target.key)
    decision = {
        OperatorAction.APPROVE: OperatorDecision.APPROVE,
        OperatorAction.SKIP: OperatorDecision.SKIP,
        OperatorAction.LATER: OperatorDecision.LATER,
    }[action]
    final = state.effective_final
    if final is not None:
        recorded = OperatorDecision(final.decision)
        if recorded == decision:
            code = (
                CallbackCode.ALREADY_APPROVED
                if decision is OperatorDecision.APPROVE
                else CallbackCode.ALREADY_SKIPPED
            )
            logger.info("telegram_duplicate_callback card_id=%s decision=%s", card.id, decision)
            return _outcome(code, decision_id=final.id)
        logger.warning(
            "telegram_callback_refused reason=conflict card_id=%s recorded=%s requested=%s",
            card.id,
            recorded.value,
            decision.value,
        )
        return _outcome(
            CallbackCode.CONFLICT_REQUIRES_OVERRIDE,
            answer=(
                f"A final decision ({recorded.value}) is already recorded. Changing it "
                "needs an explicit operator override (CLI). Nothing changed."
            ),
            decision_id=final.id,
        )
    if decision is OperatorDecision.LATER and state.pending_later is not None:
        return _outcome(CallbackCode.ALREADY_LATER, decision_id=state.pending_later.id)
    if application.current_status not in DECIDABLE_STATUSES:
        logger.warning(
            "telegram_callback_refused reason=not_decidable card_id=%s status=%s",
            card.id,
            application.current_status.value,
        )
        return _outcome(
            CallbackCode.NOT_DECIDABLE,
            answer=(
                f"The application is {application.current_status.value}; operator decisions "
                "apply only before submission. Nothing changed."
            ),
        )
    if decision is OperatorDecision.APPROVE and not target.view.package.ready:
        reasons = ", ".join(target.view.package.reasons) or "UNKNOWN"
        logger.warning(
            "telegram_callback_refused reason=package_not_ready card_id=%s reasons=%s",
            card.id,
            reasons,
        )
        return _outcome(
            CallbackCode.PACKAGE_NOT_READY,
            answer=f"PACKAGE_NOT_READY: {reasons}. Nothing changed."[:200],
        )

    suffix = "final" if decision in FINAL_DECISIONS else "later"
    values = dict(
        application_id=application.id,
        card_id=card.id,
        review_package_id=application.review_package_id,
        target_key=target.key,
        vacancy_reference=_vacancy_reference(application),
        decision=decision.value,
        idempotency_key=f"{target.key}:{suffix}:{state.generation}",
        callback_ref=callback_ref,
        actor=TELEGRAM_OPERATOR_ACTOR,
        operator_ref=_sha256(f"telegram-user:{callback.user_id}") if callback.user_id else None,
        chat_ref=_sha256(f"telegram-chat:{callback.chat_id}"),
        telegram_message_id=callback.message_id,
        metadata={
            "source": "telegram_callback",
            "application_status": application.current_status.value,
            "review_status": target.view.package.review_status,
            "review_version": target.view.package.review_version,
            "revision_id": target.view.package.revision_id,
        },
    )
    try:
        if decision is OperatorDecision.APPROVE:
            correlation_key = f"s16:approve:{target.key}:g{state.generation}"
            record = repo.add_decision(db, lifecycle_correlation_key=correlation_key, **values)
            stale = _recheck_review_locked(db, application, target.view.package)
            if stale:
                db.rollback()
                reasons = ", ".join(stale)
                logger.warning(
                    "telegram_callback_refused reason=package_changed card_id=%s reasons=%s",
                    card.id,
                    reasons,
                )
                return _outcome(
                    CallbackCode.PACKAGE_NOT_READY,
                    answer=f"PACKAGE_NOT_READY: {reasons}. Nothing changed."[:200],
                )
            outcome = tracker.transition_application(
                db,
                application.id,
                S.APPROVED,
                origin=ApplicationEventOrigin.OPERATOR,
                actor=TELEGRAM_OPERATOR_ACTOR,
                occurred_at=when,
                correlation_key=correlation_key,
                reason=APPROVE_REASON,
                metadata={"operator_decision": "APPROVE", "card_id": card.id},
            )
            if outcome.duplicate:
                db.rollback()
                return _outcome(CallbackCode.ALREADY_APPROVED)
        else:
            record = repo.add_decision(db, **values)
            db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("telegram_duplicate_callback card_id=%s reason=concurrent", card.id)
        latest = decision_state(db, target.key)
        if latest.effective_final is not None:
            recorded = OperatorDecision(latest.effective_final.decision)
            if recorded == decision:
                return _outcome(
                    CallbackCode.ALREADY_APPROVED
                    if decision is OperatorDecision.APPROVE
                    else CallbackCode.ALREADY_SKIPPED
                )
            return _outcome(
                CallbackCode.CONFLICT_REQUIRES_OVERRIDE,
                answer=(
                    f"A final decision ({recorded.value}) is already recorded. Nothing changed."
                ),
            )
        return _outcome(CallbackCode.DUPLICATE_CALLBACK)
    except (
        ApplicationConcurrencyError,
        InvalidApplicationTransitionError,
        ApplicationInputError,
    ) as exc:
        db.rollback()
        logger.warning(
            "telegram_callback_refused reason=state_changed card_id=%s error_type=%s",
            card.id,
            type(exc).__name__,
        )
        return _outcome(CallbackCode.STATE_CHANGED)
    except Exception:
        db.rollback()
        raise

    decision_id = record.id
    logger.info(
        "telegram_decision_recorded decision_id=%s card_id=%s application_id=%s decision=%s",
        decision_id,
        card.id,
        application.id,
        decision.value,
    )
    code = {
        OperatorDecision.APPROVE: CallbackCode.APPROVED,
        OperatorDecision.SKIP: CallbackCode.SKIPPED,
        OperatorDecision.LATER: CallbackCode.LATER_RECORDED,
    }[decision]
    return _outcome(
        code,
        decision_id=decision_id,
        state_changed=True,
        card=_card_after_change(db, card, tracker=tracker, now=when),
    )


# --- operator override (CLI only) ---------------------------------------------------


class OverrideCode(StrEnum):
    OVERRIDDEN = "OVERRIDDEN"
    NO_FINAL_DECISION = "NO_FINAL_DECISION"
    SAME_DECISION = "SAME_DECISION"
    NOT_PERMITTED = "NOT_PERMITTED"
    PACKAGE_NOT_READY = "PACKAGE_NOT_READY"
    STATE_CHANGED = "STATE_CHANGED"


@dataclass(frozen=True)
class OverrideResult:
    code: OverrideCode
    detail: str
    decision_id: int | None = None


def plan_override(
    db: Session,
    application_id: int,
    decision: OperatorDecision,
    *,
    tracker: ApplicationTracker | None = None,
) -> OverrideResult:
    """Validate an override without writing (the CLI dry run)."""
    tracker = tracker or ApplicationTracker()
    target = load_target(db, application_id, tracker=tracker)
    return _check_override(target, decision_state(db, target.key), decision)


def _check_override(
    target: OperatorTarget, state: DecisionState, decision: OperatorDecision
) -> OverrideResult:
    if decision not in FINAL_DECISIONS:
        return OverrideResult(OverrideCode.NOT_PERMITTED, "only APPROVE or SKIP can override")
    final = state.effective_final
    if final is None:
        return OverrideResult(
            OverrideCode.NO_FINAL_DECISION, "no final decision recorded; nothing to override"
        )
    if OperatorDecision(final.decision) == decision:
        return OverrideResult(OverrideCode.SAME_DECISION, f"{decision.value} is already recorded")
    status = target.application.current_status
    if decision is OperatorDecision.APPROVE:
        if not target.view.package.ready:
            return OverrideResult(
                OverrideCode.PACKAGE_NOT_READY, ", ".join(target.view.package.reasons)
            )
        return OverrideResult(
            OverrideCode.OVERRIDDEN, f"SKIP -> APPROVE; application {status.value} -> APPROVED"
        )
    if status != S.APPROVED:
        return OverrideResult(
            OverrideCode.NOT_PERMITTED,
            f"application is {status.value}; APPROVE -> SKIP is only possible while APPROVED",
        )
    return OverrideResult(
        OverrideCode.OVERRIDDEN, "APPROVE -> SKIP; application APPROVED -> PACKAGE_READY"
    )


def override_decision(
    db: Session,
    application_id: int,
    decision: OperatorDecision,
    *,
    reason: str,
    actor: str = CLI_OPERATOR_ACTOR,
    tracker: ApplicationTracker | None = None,
    now: datetime | None = None,
) -> OverrideResult:
    """Explicit operator override of a recorded final decision: a NEW
    decision row superseding the old one, with a mandatory reason, plus the
    matching lifecycle change (SKIP -> APPROVE: PACKAGE_READY -> APPROVED;
    APPROVE -> SKIP: an operator status override APPROVED -> PACKAGE_READY).
    Never touches a submitted application."""
    if not reason or not reason.strip():
        raise ApplicationInputError("reason is required")
    tracker = tracker or ApplicationTracker()
    when = now or _now()
    target = load_target(db, application_id, tracker=tracker)
    state = decision_state(db, target.key)
    check = _check_override(target, state, decision)
    if check.code is not OverrideCode.OVERRIDDEN:
        return check
    generation = state.generation + 1
    correlation_key = f"s16:override:{target.key}:g{generation}"
    application = target.application
    try:
        record = repo.add_decision(
            db,
            application_id=application.id,
            card_id=None,
            review_package_id=application.review_package_id,
            target_key=target.key,
            vacancy_reference=_vacancy_reference(application),
            decision=decision.value,
            supersedes_decision_id=state.effective_final.id,
            idempotency_key=f"{target.key}:override:{generation}",
            actor=actor[:40],
            lifecycle_correlation_key=correlation_key,
            reason=reason.strip()[:1000],
            metadata={"source": "operator_override"},
        )
        if decision is OperatorDecision.APPROVE:
            stale = _recheck_review_locked(db, application, target.view.package)
            if stale:
                db.rollback()
                return OverrideResult(OverrideCode.PACKAGE_NOT_READY, ", ".join(stale))
            lifecycle = tracker.transition_application(
                db,
                application.id,
                S.APPROVED,
                origin=ApplicationEventOrigin.OPERATOR,
                actor=actor,
                occurred_at=when,
                correlation_key=correlation_key,
                reason=f"Operator override SKIP -> APPROVE: {reason.strip()}",
            )
        else:
            lifecycle = tracker.override_status(
                db,
                application.id,
                S.PACKAGE_READY,
                actor=actor,
                reason=f"Operator override APPROVE -> SKIP: {reason.strip()}",
                occurred_at=when,
                correlation_key=correlation_key,
            )
        if lifecycle.duplicate:
            db.rollback()
            return OverrideResult(OverrideCode.STATE_CHANGED, "lifecycle event already recorded")
    except (
        IntegrityError,
        ApplicationConcurrencyError,
        InvalidApplicationTransitionError,
        ApplicationInputError,
    ) as exc:
        db.rollback()
        return OverrideResult(OverrideCode.STATE_CHANGED, type(exc).__name__)
    except Exception:
        db.rollback()
        raise
    logger.info(
        "telegram_decision_recorded decision_id=%s application_id=%s decision=%s override=true",
        record.id,
        application.id,
        decision.value,
    )
    return OverrideResult(OverrideCode.OVERRIDDEN, check.detail, decision_id=record.id)
