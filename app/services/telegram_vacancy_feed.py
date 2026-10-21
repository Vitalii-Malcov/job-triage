"""Stage 9A: Telegram vacancy feed -- one reviewable card per newly
discovered, eligible vacancy.

Flow (wired into app.services.collector_runner when
`settings.telegram_vacancy_feed_enabled`):

1. A collector persists a job (`score_and_persist`: normalize + fingerprint
   dedup + scoring -- unchanged).
2. `record_collected_job` records review state for that job
   (`DISCOVERED`, or `QUEUED_FOR_REVIEW` if eligible) -- one row per job,
   so re-collecting the same vacancy never queues it twice.
3. After the run's loop, `deliver_queued_vacancy_cards` sends queued cards
   (bounded, paced, lease-aware). A provably failed send goes back to the
   queue and is retried by the next run's sweep; an uncertain one is never
   auto-retried (no duplicate cards).

**Scope boundary.** Nothing here applies to a job, generates a CV or cover
letter, sends email, or writes `JobRecord.status`. Every line on a card is
derived from data already persisted on the `JobRecord` (or the candidate's
own trusted skills) -- nothing is invented: a field that is not available
(e.g. salary, which no collector provides yet) is simply omitted.

**Rendering.** Plain text (no `parse_mode`), so untrusted job content can
never be interpreted as Telegram markup. Every variable field is capped,
and the whole card is kept under `CARD_SOFT_LIMIT` (well below Telegram's
4096-char hard cap) by dropping the least important sections first.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agents.job_scorer import normalize_skill
from app.agents.requirement_extractor import extract_language_requirements
from app.agents.score_labels import VACANCY_SCORE_LABEL
from app.db.application_repository import has_application_for_job
from app.db.candidate_profile_repository import get_candidate_skills_for_scoring
from app.db.models import JobRecord, TelegramVacancyReviewRecord
from app.db.repositories import get_job_by_id
from app.db.telegram_vacancy_review_repository import (
    claim_for_sending,
    dequeue,
    ensure_review,
    list_queued,
    mark_send_failed,
    mark_sent,
    mark_uncertain,
    reconcile_stale_sending,
    release_claim,
)
from app.models.application_status import ApplicationStatus
from app.services.telegram import TelegramSendOutcome, send_telegram_message

logger = logging.getLogger(__name__)

CARD_SOFT_LIMIT = 3500
DETAILS_SOFT_LIMIT = 3500
TITLE_MAX_LEN = 200
COMPANY_MAX_LEN = 120
LOCATION_MAX_LEN = 120
SOURCE_MAX_LEN = 60
URL_MAX_LEN = 1000
SKILL_MAX_LEN = 40
LINE_MAX_LEN = 200
TECH_DISPLAY_LIMIT = 12
REASONS_LIMIT = 5
WARNINGS_LIMIT = 5
DETAILS_DESCRIPTION_MAX_LEN = 1500
MANUAL_REVIEW_MARKER = "👤 Manuell zur Prüfung geöffnet"
LOW_DATA_CONFIDENCE = 0.6
# Seconds between two card sends in one sweep (Telegram flood limits) --
# same 1s pacing the legacy collector alerts use.
SEND_PACING_SECONDS = 1.0

# Callback data: "vf:<action>:<token>" -- at most 21 bytes, well under
# Telegram's 64-byte callback_data limit. The token is the review row's
# opaque random callback_token, never a job id.
CALLBACK_PREFIX = "vf"
ACTION_APPLY = "a"
ACTION_DETAILS = "d"
ACTION_SAVE = "s"
ACTION_SKIP = "x"
CALLBACK_ACTIONS = (ACTION_APPLY, ACTION_DETAILS, ACTION_SAVE, ACTION_SKIP)

# Telegram-feed German-level requirements at or above this are surfaced as
# a warning (the candidate's own level is not compared here -- that would
# need the full candidate-match pipeline; Stage 9B).
_HIGH_GERMAN_LEVELS = ("C1", "C2", "NATIVE")


def _cap(value: str, limit: int) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _json_list(raw: str | None) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(v) for v in values if isinstance(v, str) and v.strip()]


def is_eligible_for_review(record: JobRecord, settings) -> bool:
    """Same gate the legacy collector alert uses (APPLY and score >=
    `min_job_score_to_notify`), plus: the application lifecycle has not
    moved on yet (a job the operator already saved/applied to via /status
    is not carded)."""
    return (
        record.recommendation == "APPLY"
        and record.score >= settings.min_job_score_to_notify
        and record.status == ApplicationStatus.NEW.value
    )


@dataclass(frozen=True)
class TechRequirement:
    name: str
    required: bool
    matched: bool


@dataclass(frozen=True)
class VacancyCard:
    title: str
    company: str
    source: str
    url: str
    score: int
    location: str | None = None
    salary: str | None = None
    german_level: str | None = None
    technologies: list[TechRequirement] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # The operator opened this card with `/review <job_id>`; the scorer's
    # own verdict (still shown in `reasons`) was not APPLY-eligible.
    manual_review: bool = False


def _german_level(record: JobRecord) -> str | None:
    levels = []
    for requirement in extract_language_requirements(record.title, record.description):
        if requirement.language == "German" and requirement.level not in levels:
            levels.append(requirement.level)
    return "/".join(levels) if levels else None


def build_vacancy_card(
    record: JobRecord, candidate_skills: Collection[str], *, manual_review: bool = False
) -> VacancyCard:
    """Derive a card purely from persisted job data and the candidate's
    trusted skill set. Matching uses `normalize_skill`, the same identity
    JobScorer uses, so a ✅ here is exactly a JobScorer match."""
    candidate = {normalize_skill(skill) for skill in candidate_skills}
    must = _json_list(record.must_have_skills_json)
    must_keys = {normalize_skill(skill) for skill in must}
    nice = [
        skill
        for skill in _json_list(record.nice_to_have_skills_json)
        if normalize_skill(skill) not in must_keys
    ]

    technologies = [
        TechRequirement(skill, required=True, matched=normalize_skill(skill) in candidate)
        for skill in must
    ] + [
        TechRequirement(skill, required=False, matched=normalize_skill(skill) in candidate)
        for skill in nice
    ]
    matched_must = [t.name for t in technologies if t.required and t.matched]
    missing_must = [t.name for t in technologies if t.required and not t.matched]
    matched_nice = [t.name for t in technologies if not t.required and t.matched]

    reasons: list[str] = []
    if matched_must:
        reasons.append(
            f"{len(matched_must)} of {len(must)} required technologies in your profile: "
            + ", ".join(matched_must)
        )
    if matched_nice:
        reasons.append("Nice-to-have in your profile: " + ", ".join(matched_nice))
    reasons.append(f"Scorer recommendation: {record.recommendation} ({record.score}/100)")

    german_level = _german_level(record)
    warnings: list[str] = []
    if missing_must:
        warnings.append("Required but not in your profile: " + ", ".join(missing_must))
    if german_level and any(level in german_level for level in _HIGH_GERMAN_LEVELS):
        warnings.append(f"High German level required: {german_level}")
    if not technologies:
        warnings.append("No technologies could be extracted from the posting")
    if record.data_confidence < LOW_DATA_CONFIDENCE:
        warnings.append(
            f"Low data confidence ({record.data_confidence:.2f}) - posting text may be incomplete"
        )
    if record.posting_type and record.posting_type.upper() != "ARBEIT":
        warnings.append(f"Posting type: {record.posting_type}")

    return VacancyCard(
        title=record.title,
        company=record.company,
        source=record.source,
        url=record.url,
        score=record.score,
        location=record.location or None,
        german_level=german_level,
        technologies=technologies,
        reasons=reasons,
        warnings=warnings,
        manual_review=manual_review,
    )


def _tech_line(tech: TechRequirement) -> str:
    name = _cap(tech.name, SKILL_MAX_LEN)
    if tech.matched:
        return f"✅ {name}"
    if tech.required:
        return f"❌ {name} required"
    return f"⚠️ {name} preferred"


def render_vacancy_card(card: VacancyCard) -> str:
    """Plain-text card, guaranteed <= CARD_SOFT_LIMIT characters."""
    title_emoji = "🐍" if "python" in card.title.casefold() else "💼"
    header = [f"{title_emoji} {_cap(card.title, TITLE_MAX_LEN)}"]
    if card.manual_review:
        header.append(MANUAL_REVIEW_MARKER)
    header += [
        f"Firma: {_cap(card.company, COMPANY_MAX_LEN)}",
        f"📍 {_cap(card.location, LOCATION_MAX_LEN) if card.location else 'Ort nicht angegeben'}",
    ]
    if card.german_level:
        header.append(f"🇩🇪 Deutsch: {card.german_level}")
    if card.salary:
        header.append(f"💰 {_cap(card.salary, LINE_MAX_LEN)}")
    header.append(f"🔎 Quelle: {_cap(card.source, SOURCE_MAX_LEN)}")
    header.append("")
    header.append(f"{VACANCY_SCORE_LABEL}: {card.score}/100")

    sections: list[str] = []
    if card.technologies:
        shown = card.technologies[:TECH_DISPLAY_LIMIT]
        lines = [_tech_line(tech) for tech in shown]
        hidden = len(card.technologies) - len(shown)
        if hidden > 0:
            lines.append(f"(+{hidden} more - see Details)")
        sections.append("\n".join(lines))
    if card.reasons:
        sections.append(
            "Why it may fit:\n"
            + "\n".join(f"- {_cap(r, LINE_MAX_LEN)}" for r in card.reasons[:REASONS_LIMIT])
        )
    if card.warnings:
        sections.append(
            "Watch out:\n"
            + "\n".join(f"- {_cap(w, LINE_MAX_LEN)}" for w in card.warnings[:WARNINGS_LIMIT])
        )

    url = card.url if len(card.url) <= URL_MAX_LEN else "(link too long - see Details)"
    core = "\n".join(header)

    def assemble(parts: list[str]) -> str:
        return "\n\n".join([core, *parts, url])

    text = assemble(sections)
    while len(text) > CARD_SOFT_LIMIT and sections:
        sections.pop()
        text = assemble(sections)
    return text[:CARD_SOFT_LIMIT]


def build_callback_data(action: str, token: str) -> str:
    return f"{CALLBACK_PREFIX}:{action}:{token}"


def parse_callback_data(data: str | None) -> tuple[str, str] | None:
    """Strictly parse `vf:<action>:<token>`; anything else is None. Token
    SHAPE is validated by the repository lookup itself."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[1] not in CALLBACK_ACTIONS:
        return None
    return parts[1], parts[2]


def build_card_keyboard(token: str) -> dict:
    """Telegram `InlineKeyboardMarkup` JSON for one card."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Bewerbung erstellen",
                    "callback_data": build_callback_data(ACTION_APPLY, token),
                },
                {"text": "📄 Details", "callback_data": build_callback_data(ACTION_DETAILS, token)},
            ],
            [
                {"text": "⭐ Speichern", "callback_data": build_callback_data(ACTION_SAVE, token)},
                {
                    "text": "❌ Überspringen",
                    "callback_data": build_callback_data(ACTION_SKIP, token),
                },
            ],
        ]
    }


def render_vacancy_details(record: JobRecord) -> str:
    """Longer, plain-text detail view for the Details button."""
    must = _json_list(record.must_have_skills_json)
    nice = _json_list(record.nice_to_have_skills_json)
    description = record.description.strip()
    if len(description) > DETAILS_DESCRIPTION_MAX_LEN:
        description = description[:DETAILS_DESCRIPTION_MAX_LEN].rstrip() + "…"
    lines = [
        _cap(record.title, TITLE_MAX_LEN),
        f"Firma: {_cap(record.company, COMPANY_MAX_LEN)}",
        f"Ort: {_cap(record.location, LOCATION_MAX_LEN) if record.location else 'n/a'}",
        f"Quelle: {_cap(record.source, SOURCE_MAX_LEN)}",
        f"{VACANCY_SCORE_LABEL}: {record.score}/100 ({record.recommendation})",
        f"Status: {record.status}",
        "Required: " + (", ".join(_cap(s, SKILL_MAX_LEN) for s in must) or "none extracted"),
        "Preferred: " + (", ".join(_cap(s, SKILL_MAX_LEN) for s in nice) or "none extracted"),
    ]
    head = "\n".join(lines)
    url = record.url if len(record.url) <= URL_MAX_LEN else record.url[:URL_MAX_LEN] + "…"
    room = DETAILS_SOFT_LIMIT - len(head) - len(url) - 4
    body = description[: max(room, 0)] if description else ""
    text = "\n\n".join(part for part in (head, body, url) if part)
    return text[:DETAILS_SOFT_LIMIT]


def record_collected_job(db: Session, job_record: JobRecord, settings) -> None:
    """Collector hook: record review state for one just-persisted job.
    Best-effort -- a failure here is logged and rolled back, never raised:
    the job itself is already committed, and the next re-collection will
    simply try again."""
    try:
        review = ensure_review(
            db, job_record.id, eligible=is_eligible_for_review(job_record, settings)
        )
    except Exception as exc:
        db.rollback()
        logger.warning(
            "vacancy_feed_record_failed job_id=%s error_type=%s",
            job_record.id,
            type(exc).__name__,
        )
        return
    logger.debug(
        "vacancy_feed_recorded job_id=%s review_id=%s state=%s",
        review.job_id,
        review.id,
        review.state,
    )


_SENT = "SENT"
_FAILED = "FAILED"
_UNCERTAIN = "UNCERTAIN"
_RELEASED = "RELEASED"


async def _send_claimed_card(
    db: Session,
    settings,
    review: TelegramVacancyReviewRecord,
    job: JobRecord,
    claim_token: str,
    candidate_skills: Collection[str],
    *,
    is_lease_lost: Callable[[], bool] | None = None,
    manual_review: bool = False,
) -> str:
    """Render, send ONE attempt and resolve one row this caller holds the
    `SENDING` claim `claim_token` for. Shared by the sweep and the
    operator's `/review` so both use the same card, keyboard and outcome
    handling: SENT -> `TELEGRAM_SENT`; provably FAILED (incl. a render
    error) -> back to the queue / `DELIVERY_FAILED`; UNCERTAIN ->
    `DELIVERY_UNCERTAIN` (never auto-retried); lease lost before any
    request -> claim released untouched. For `manual_review` a known-unsent
    outcome is held for the operator's next explicit `/review`
    (`manual_hold`), never returned to the automatic queue."""
    try:
        text = render_vacancy_card(
            build_vacancy_card(job, candidate_skills, manual_review=manual_review)
        )
        reply_markup = build_card_keyboard(review.callback_token)
    except Exception as exc:
        # Nothing reached Telegram -- provably failed, retryable.
        mark_send_failed(
            db,
            review,
            claim_token=claim_token,
            last_error=f"render:{type(exc).__name__}",
            manual_hold=manual_review,
        )
        logger.warning(
            "vacancy_feed_render_failed review_id=%s error_type=%s",
            review.id,
            type(exc).__name__,
        )
        return _FAILED

    # Re-check immediately before networking (Codex S9A-CODEX-002): the
    # heartbeat thread may have confirmed lease loss during the claim's
    # commit/refresh or the render above. No HTTP request has started,
    # so the row is known-not-sent -- release it back to the queue
    # (not DELIVERY_UNCERTAIN) and stop. A request that HAS started is
    # never cancelled; it finishes and resolves below.
    if is_lease_lost is not None and is_lease_lost():
        released = release_claim(
            db,
            review,
            claim_token=claim_token,
            reason="lease_lost_before_send",
            manual_hold=manual_review,
        )
        logger.info(
            "vacancy_feed_delivery_stopped reason=lease_lost_after_claim review_id=%s released=%s",
            review.id,
            released,
        )
        return _RELEASED

    result = await send_telegram_message(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        text,
        reply_markup=reply_markup,
        timeout_seconds=settings.telegram_timeout_seconds,
    )
    if result.outcome is TelegramSendOutcome.SENT:
        mark_sent(db, review, claim_token=claim_token, message_id=result.message_id)
        logger.info(
            "vacancy_feed_card_sent review_id=%s job_id=%s attempt=%s manual=%s",
            review.id,
            review.job_id,
            review.attempt_count,
            manual_review,
        )
        return _SENT
    if result.outcome is TelegramSendOutcome.FAILED:
        mark_send_failed(
            db,
            review,
            claim_token=claim_token,
            last_error=result.outcome.value,
            manual_hold=manual_review,
        )
        logger.warning(
            "vacancy_feed_card_failed review_id=%s job_id=%s attempt=%s state=%s",
            review.id,
            review.job_id,
            review.attempt_count,
            review.state,
        )
        return _FAILED
    mark_uncertain(db, review, claim_token=claim_token, last_error=result.outcome.value)
    logger.warning(
        "vacancy_feed_card_uncertain review_id=%s job_id=%s attempt=%s",
        review.id,
        review.job_id,
        review.attempt_count,
    )
    return _UNCERTAIN


@dataclass
class VacancyFeedDeliveryStats:
    sent: int = 0
    failed: int = 0
    uncertain: int = 0
    dequeued: int = 0
    stale_reconciled: int = 0


async def deliver_queued_vacancy_cards(
    db: Session,
    settings,
    *,
    is_lease_lost: Callable[[], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> VacancyFeedDeliveryStats:
    """Send up to `telegram_vacancy_feed_max_per_run` queued cards.

    Per row: re-check eligibility against the CURRENT JobRecord (dequeue if
    it no longer qualifies), CAS-claim it (`QUEUED_FOR_REVIEW -> SENDING`,
    so a concurrent sweep can never send the same card), send ONE attempt,
    then resolve: SENT -> `TELEGRAM_SENT`; provably FAILED -> back to the
    queue (retried next run, bounded by MAX_DELIVERY_ATTEMPTS); UNCERTAIN ->
    `DELIVERY_UNCERTAIN` (never auto-retried). `is_lease_lost` is checked
    before every claim AND again immediately before the HTTP request; a
    claim whose lease was lost before networking is released back to the
    queue untouched (`release_claim`).
    """
    stats = VacancyFeedDeliveryStats()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.info("vacancy_feed_delivery_skipped reason=telegram_not_configured")
        return stats

    stats.stale_reconciled = reconcile_stale_sending(db)
    if stats.stale_reconciled:
        logger.warning("vacancy_feed_stale_sending_reconciled count=%s", stats.stale_reconciled)

    queued = list_queued(db, limit=settings.telegram_vacancy_feed_max_per_run)
    if not queued:
        return stats
    candidate_skills = get_candidate_skills_for_scoring(db)

    attempted = 0
    for review in queued:
        job = get_job_by_id(db, review.job_id)
        if job is None or not is_eligible_for_review(job, settings):
            if dequeue(db, review):
                stats.dequeued += 1
                logger.info("vacancy_feed_dequeued review_id=%s", review.id)
            continue

        if attempted > 0:
            await sleep(SEND_PACING_SECONDS)
        if is_lease_lost is not None and is_lease_lost():
            logger.info("vacancy_feed_delivery_stopped reason=lease_lost")
            break
        claim_token = claim_for_sending(db, review)
        if claim_token is None:
            continue
        attempted += 1

        result = await _send_claimed_card(
            db,
            settings,
            review,
            job,
            claim_token,
            candidate_skills,
            is_lease_lost=is_lease_lost,
        )
        if result == _RELEASED:
            break
        if result == _SENT:
            stats.sent += 1
        elif result == _FAILED:
            stats.failed += 1
        else:
            stats.uncertain += 1

    logger.info(
        "vacancy_feed_delivery sent=%s failed=%s uncertain=%s dequeued=%s stale_reconciled=%s",
        stats.sent,
        stats.failed,
        stats.uncertain,
        stats.dequeued,
        stats.stale_reconciled,
    )
    return stats


# Automatic verdicts the operator may override for REVIEW (never SKIP).
MANUAL_REVIEW_RECOMMENDATIONS = ("APPLY", "MAYBE")


@dataclass(frozen=True)
class ManualReviewOutcome:
    code: str
    reply: str


def _manual_uncertain(job_id: int) -> ManualReviewOutcome:
    return ManualReviewOutcome(
        "DELIVERY_UNCERTAIN",
        f"Zustellung der Karte für Job #{job_id} ist unsicher - "
        "kein automatischer erneuter Versand (keine doppelte Karte).",
    )


def _manual_send_failed(job_id: int) -> ManualReviewOutcome:
    return ManualReviewOutcome(
        "SEND_FAILED",
        f"Karte für Job #{job_id} wurde nicht gesendet. Bitte später /review {job_id} erneut.",
    )


def _manual_state_refusal(job_id: int, state: str) -> ManualReviewOutcome | None:
    """`/review` never re-sends a card the state machine already resolved,
    and never reopens an operator decision."""
    if state in ("TELEGRAM_SENT", "SAVED"):
        return ManualReviewOutcome(
            "ALREADY_SENT", f"Karte für Job #{job_id} wurde bereits gesendet."
        )
    if state == "SKIPPED":
        return ManualReviewOutcome(
            "OPERATOR_SKIPPED",
            f"Job #{job_id} wurde von dir übersprungen - wird nicht erneut geöffnet.",
        )
    if state == "SENDING":
        return ManualReviewOutcome("IN_FLIGHT", f"Karte für Job #{job_id} wird gerade gesendet.")
    if state == "DELIVERY_UNCERTAIN":
        return _manual_uncertain(job_id)
    if state == "DELIVERY_FAILED":
        return ManualReviewOutcome(
            "DELIVERY_FAILED",
            f"Zustellung der Karte für Job #{job_id} ist endgültig fehlgeschlagen - "
            "kein erneuter Versand.",
        )
    return None


async def open_manual_vacancy_review(db: Session, settings, job_id: int) -> ManualReviewOutcome:
    """Operator `/review <job_id>`: send the normal Stage 9A card for ONE
    existing job whose automatic verdict was not APPLY-eligible (e.g.
    MAYBE), because the human explicitly wants to review it.

    Only review/delivery state is written (`telegram_vacancy_reviews`);
    `JobRecord.score`/`.recommendation`/`.status` are never touched, and no
    application, Bewerbung preparation, draft or email is created. The card
    shows the original score and recommendation plus MANUAL_REVIEW_MARKER.
    Idempotency is the existing state machine's: only a `DISCOVERED` or
    `QUEUED_FOR_REVIEW` row is claimed (CAS), so a sent, in-flight,
    uncertain, failed or operator-decided row is never re-sent or reopened.
    A known-unsent manual card (Telegram FAILED, or any failure before the
    request) is held in `DISCOVERED` (`manual_hold`): the automatic sweep
    never resends it, only another explicit `/review` (attempt cap applies).
    The single network call is the card to the configured operator chat.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return ManualReviewOutcome("NOT_CONFIGURED", "Telegram ist nicht konfiguriert.")
    job = get_job_by_id(db, job_id)
    if job is None:
        return ManualReviewOutcome("JOB_NOT_FOUND", f"Job #{job_id} nicht gefunden.")
    if job.recommendation not in MANUAL_REVIEW_RECOMMENDATIONS:
        return ManualReviewOutcome(
            "RECOMMENDATION_NOT_ALLOWED",
            f"Automatische Empfehlung ist {job.recommendation}. "
            "Manuelle Freigabe über diesen Befehl nicht erlaubt.",
        )
    if job.status != ApplicationStatus.NEW.value:
        return ManualReviewOutcome(
            "JOB_NOT_ELIGIBLE",
            f"Job #{job_id} hat Status {job.status} - keine manuelle Prüfung möglich.",
        )
    if has_application_for_job(db, job_id):
        return ManualReviewOutcome(
            "APPLICATION_EXISTS",
            f"Für Job #{job_id} existiert bereits eine Bewerbung - keine neue Karte.",
        )

    review = ensure_review(db, job_id, eligible=False)
    refusal = _manual_state_refusal(job_id, review.state)
    if refusal is not None:
        return refusal
    claim_token = claim_for_sending(db, review, from_state=review.state)
    if claim_token is None:
        return ManualReviewOutcome(
            "BUSY", f"Karte für Job #{job_id} wird gerade bearbeitet - bitte später erneut prüfen."
        )

    try:
        candidate_skills = get_candidate_skills_for_scoring(db)
    except Exception as exc:
        # No Telegram request was started: known-unsent. Release the claim
        # (no attempt consumed) to the manual hold so it can never become
        # SENDING-stale -> DELIVERY_UNCERTAIN, and only another explicit
        # /review retries it (Codex 1671ebc P1 #2). Type only, no detail.
        db.rollback()
        release_claim(
            db,
            review,
            claim_token=claim_token,
            reason=f"pre_send:{type(exc).__name__}",
            manual_hold=True,
        )
        logger.warning(
            "vacancy_feed_manual_review_pre_send_failed review_id=%s job_id=%s error_type=%s",
            review.id,
            job_id,
            type(exc).__name__,
        )
        return _manual_send_failed(job_id)

    logger.info(
        "vacancy_feed_manual_review_opened review_id=%s job_id=%s score=%s recommendation=%s",
        review.id,
        job_id,
        job.score,
        job.recommendation,
    )
    result = await _send_claimed_card(
        db,
        settings,
        review,
        job,
        claim_token,
        candidate_skills,
        manual_review=True,
    )
    if result == _SENT:
        return ManualReviewOutcome(
            "SENT",
            f"{MANUAL_REVIEW_MARKER}: Job #{job_id} (Score {job.score}/100, automatische "
            f"Empfehlung {job.recommendation} unverändert). Karte gesendet - nichts wurde "
            "beworben oder an Arbeitgeber gesendet.",
        )
    if result in (_FAILED, _RELEASED):
        return _manual_send_failed(job_id)
    return _manual_uncertain(job_id)
