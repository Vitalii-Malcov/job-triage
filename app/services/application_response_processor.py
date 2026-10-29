"""Stage 15: inbound employer e-mail -> application lifecycle.

    fetch (read-only) -> normalize -> correlate -> classify -> decide
      -> [apply only] one transaction: event + status projection

**Read-only towards Gmail, local-only towards everything else.** Messages
come either from a live read-only IMAP fetch (`fetch_application_mailboxes`:
`SELECT ... readonly`, `BODY.PEEK[]`, nothing persisted) or from
`GmailMessageRecord` rows a Stage 7A sync already stored. Nothing here
sends, replies, drafts, labels, marks read, posts or notifies.

**Dry run writes nothing.** With `apply=False` every step is a SELECT and
the session is rolled back at the end; the result shows the transition
that WOULD be applied. With `apply=True` each message is one
`ApplicationTracker` unit of work, keyed by the message's deterministic
`correlation_key` -- reprocessing the same message is a no-op
(`duplicate=True`).

Results never contain message bodies, addresses or Message-IDs; logs carry
ids, classifications and reasons only.

**Monitored mailboxes.** Applications go out from more than one of the
applicant's own accounts, so `fetch_application_mailboxes` reads every
configured one read-only: the primary `GMAIL_*` account and, when
configured, the `XING_MAILBOX_*` account (same credentials the XING
collector already uses; no new secret). XING platform mail in that mailbox
is UNMATCHED by the correlator (`JOB_PLATFORM_NOTIFICATION`), and the XING
collector accepts only its digest sender, so the two paths never share a
message. The same Message-ID delivered to two accounts is processed once
(fetch-time dedup plus a cross-account duplicate check).
"""

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.application_response_classifier import (
    classify_application_response,
    normalize_body,
    normalize_subject,
)
from app.db import application_repository as repo
from app.db.models import GmailMessageRecord
from app.domain.application_lifecycle import InvalidApplicationTransitionError
from app.domain.application_response_policy import decide_response
from app.models.application_lifecycle import Application, ApplicationEventOrigin
from app.providers.email.base import ParsedGmailMessage
from app.services.application_correlation import (
    InboundMessage,
    correlate_message,
    in_response_window,
    message_key_for,
    needs_sender,
    normalize_occurred_at,
    stable_hash,
)
from app.services.application_tracker import ApplicationConcurrencyError, ApplicationTracker

logger = logging.getLogger(__name__)
PROCESSOR_ACTOR = "stage15_response_processor"
# Report-only marker (nothing is written for it): the message looks like a
# lifecycle event, but this pipeline may not or could not act on it -- an
# outcome the operator must confirm, or a lifecycle message whose
# correlation is too weak / ambiguous to record.
NEEDS_OPERATOR_REVIEW = "NEEDS_OPERATOR_REVIEW"
_REVIEWABLE_CLASSIFICATIONS = frozenset(
    {
        "RECEIPT_CONFIRMATION",
        "UNDER_REVIEW_NOTICE",
        "INTERVIEW_INVITATION",
        "TECHNICAL_INTERVIEW_INVITATION",
        "REQUEST_FOR_INFORMATION",
        "OFFER",
        "REJECTION",
    }
)


@dataclass(frozen=True)
class ResponseTrackingResult:
    message_ref: str  # first 12 hex chars of the correlation key -- not the Message-ID
    application_id: int | None
    company: str | None
    role: str | None
    classification: str | None
    rule: str | None
    classifier_confidence: str | None
    correlation_status: str
    correlation_confidence: str
    correlation_signals: tuple[str, ...]
    previous_status: str | None
    new_status: str | None
    action: str
    transition_applied: bool
    event_recorded: bool
    duplicate: bool
    dry_run: bool
    reason: str
    # REJECTION/OFFER mail is only reported: the operator must confirm the
    # outcome (CLI transition), e-mail never applies a terminal status.
    operator_confirmation_required: bool = False
    proposed_status: str | None = None
    operator_review: str | None = None
    # Stage 19: applications an unmatched lifecycle mail may belong to (its
    # sender already wrote about them); shown to the operator, never written.
    review_candidates: tuple[int, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def inbound_from_parsed(message: ParsedGmailMessage) -> InboundMessage:
    return InboundMessage(
        account_key=message.account_key,
        message_key=message_key_for(
            message.message_id_header, message.mailbox, message.uid_validity, message.uid
        ),
        message_id_header=message.message_id_header,
        reply_to_ids=tuple(i for i in (message.in_reply_to, *message.references) if i),
        from_address=message.from_address,
        from_display_name=message.from_display_name,
        to_addresses=message.to_addresses,
        subject=message.subject or "",
        body=message.body_plain or message.body_html_text,
        has_html=message.has_html and not message.body_plain,
        occurred_at=normalize_occurred_at(message.provider_arrival_at, message.sent_at),
        direction=message.direction,
    )


def inbound_from_record(record: GmailMessageRecord) -> InboundMessage:
    references = tuple(json.loads(record.references_json or "[]"))
    return InboundMessage(
        account_key=record.account_key,
        message_key=message_key_for(
            record.message_id_header, record.mailbox, record.uid_validity, record.uid
        ),
        message_id_header=record.message_id_header,
        reply_to_ids=tuple(i for i in (record.in_reply_to, *references) if i),
        from_address=record.from_address,
        from_display_name=record.from_display_name,
        to_addresses=tuple(json.loads(record.to_addresses_json or "[]")),
        subject=record.subject or "",
        body=record.body_plain or "",
        has_html=record.has_html,
        occurred_at=normalize_occurred_at(
            record.provider_arrival_at if record.provider_arrival_is_trusted else None,
            record.sent_at,
        ),
        direction=record.direction,
    )


def _thread_hashes(db: Session, message: InboundMessage) -> dict[int, frozenset[str]]:
    """Applications whose already-recorded Gmail events are the messages this
    one replies to: an indexed lookup of the referenced messages' own
    correlation keys (bounded by the provider's MAX_REFERENCES), never a
    scan of all events."""
    by_app: dict[int, set[str]] = {}
    for event in repo.get_events_by_correlation_keys(db, message.reply_correlation_keys):
        value = json.loads(event.metadata_json or "{}").get("message_id_hash")
        if value:
            by_app.setdefault(event.application_id, set()).add(value)
    return {app_id: frozenset(values) for app_id, values in by_app.items()}


def _sender_lookup(db: Session, message: InboundMessage):
    """SENDER signal: has this exact sender already had a message correlated
    to the application? Called only for candidates that already match on
    COMPANY; each call is one indexed per-application event read, cached."""
    sender_hash = message.sender_address_hash
    if sender_hash is None:
        return None
    cache: dict[int, bool] = {}

    def seen(application_id: int) -> bool:
        if application_id not in cache:
            cache[application_id] = any(
                json.loads(event.metadata_json or "{}").get("sender_address_hash") == sender_hash
                for event in repo.list_events(db, application_id)
                if event.origin == ApplicationEventOrigin.GMAIL.value
            )
        return cache[application_id]

    return seen


def _applications_seen_from_sender(applications, sender_seen, message) -> tuple[int, ...]:
    """Applications (still in their response window) whose earlier mail came
    from this exact sender."""
    if sender_seen is None or message.occurred_at is None:
        return ()
    return tuple(
        sorted(
            app.id
            for app in applications
            if in_response_window(app, message.occurred_at) and sender_seen(app.id)
        )
    )


_OPERATOR_DECISIONS = frozenset({"STATUS_CHANGED", "STATUS_OVERRIDDEN"})
# Codex Stage 19 review round 2: only a status that answers THIS mail (or a
# terminal one) counts as handling it; an unrelated change never hides it.
_TERMINAL = frozenset({"REJECTED", "WITHDRAWN", "OFFER"})
_ANSWERS = {
    "RECEIPT_CONFIRMATION": frozenset({"RECEIVED", "UNDER_REVIEW"}),
    "UNDER_REVIEW_NOTICE": frozenset({"UNDER_REVIEW"}),
    "INTERVIEW_INVITATION": frozenset({"INTERVIEW", "TECHNICAL_INTERVIEW"}),
    "TECHNICAL_INTERVIEW_INVITATION": frozenset({"TECHNICAL_INTERVIEW"}),
    "REJECTION": frozenset({"REJECTED"}),
    "OFFER": frozenset({"OFFER"}),
}


def _operator_acted_since(db: Session, application_ids, occurred_at, classification) -> bool:
    """The operator moved one of these applications, after the mail, to a
    status that answers it (or to a terminal status)."""
    since = normalize_occurred_at(occurred_at)
    if since is None:
        return False
    answers = _ANSWERS.get(classification, frozenset()) | _TERMINAL
    for application_id in application_ids:
        for event in repo.list_events(db, application_id):
            recorded = normalize_occurred_at(event.recorded_at)
            if (
                event.origin == ApplicationEventOrigin.OPERATOR.value
                # a status decision, not the creation of the record itself
                and event.event_type in _OPERATOR_DECISIONS
                and event.new_status in answers
                and recorded is not None
                and recorded >= since
            ):
                return True
    return False


def _review_flag(classification: str | None, *, needs_confirmation: bool) -> str | None:
    if needs_confirmation:
        return NEEDS_OPERATOR_REVIEW
    return NEEDS_OPERATOR_REVIEW if classification in _REVIEWABLE_CLASSIFICATIONS else None


class ApplicationResponseProcessor:
    def __init__(self, tracker: ApplicationTracker | None = None) -> None:
        self.tracker = tracker or ApplicationTracker()

    def process(
        self,
        db: Session,
        messages: Iterable[InboundMessage],
        *,
        apply: bool,
        applicant_addresses: Sequence[str] = (),
        only_application_ids: frozenset[int] | None = None,
        monitored_account_keys: Sequence[str] = (),
    ) -> list[ResponseTrackingResult]:
        """`only_application_ids` restricts correlation candidates (e.g. a
        one-application bootstrap); every other application is untouched.
        `monitored_account_keys` are all mailbox accounts being processed:
        a message already recorded under any of them is a duplicate."""
        results: list[ResponseTrackingResult] = []
        try:
            for message in messages:
                results.append(
                    self._process_one(
                        db,
                        message,
                        apply=apply,
                        applicant_addresses=applicant_addresses,
                        only_application_ids=only_application_ids,
                        monitored_account_keys=monitored_account_keys,
                    )
                )
        finally:
            if not apply:
                db.rollback()
        return results

    def _process_one(
        self,
        db: Session,
        message: InboundMessage,
        *,
        apply: bool,
        applicant_addresses: Sequence[str],
        only_application_ids: frozenset[int] | None,
        monitored_account_keys: Sequence[str] = (),
    ) -> ResponseTrackingResult:
        ref = message.correlation_key.removeprefix("gmail:")[:12]
        # Complete candidate set via a batched keyset scan (bounded memory);
        # never a single page that could silently drop application #201+.
        sender_seen = _sender_lookup(db, message)
        correlation = correlate_message(
            message,
            self.tracker.iter_correlation_candidates(db, only_ids=only_application_ids),
            applicant_addresses=applicant_addresses,
            thread_hashes_by_application=_thread_hashes(db, message),
            sender_seen=sender_seen,
        )
        base = dict(
            message_ref=ref,
            correlation_status=correlation.status,
            correlation_confidence=correlation.confidence,
            correlation_signals=correlation.signals,
            dry_run=not apply,
        )
        if correlation.status != "MATCHED":
            logger.info(
                "gmail_unmatched message_ref=%s status=%s reason=%s candidates=%d",
                ref,
                correlation.status,
                correlation.reason,
                len(correlation.candidates),
            )
            # An AMBIGUOUS message concerns one of several applications; if it
            # reads like a lifecycle event the operator must pick the one.
            # Stage 19: so does a mail that names no employer/role at all
            # ("Wir laden Sie gerne zu einem Vorstellungsgespräch ein.") but
            # comes from a sender that already wrote about an application --
            # it was silently dropped before. Still nothing is written.
            known = (
                _applications_seen_from_sender(
                    self.tracker.iter_correlation_candidates(db, only_ids=only_application_ids),
                    sender_seen,
                    message,
                )
                if correlation.reason == "NO_SIGNAL"
                else ()
            )
            ambiguous = (
                classify_application_response(
                    message.subject, message.body, message.from_address, has_html=message.has_html
                )
                if correlation.status == "AMBIGUOUS" or known
                else None
            )
            # Codex Stage 19 review: once the operator acted on a candidate
            # application after this mail arrived, it no longer asks for a
            # decision on every scan.
            # Codex round 3 (design rethink): cleared automatically only when
            # exactly ONE application can be meant; with several the operator
            # decides, and the mail keeps asking until its window passes.
            handled = (
                len(known) == 1
                and ambiguous is not None
                and _operator_acted_since(
                    db, known, message.occurred_at, ambiguous.classification.value
                )
            )
            return ResponseTrackingResult(
                **base,
                application_id=None,
                company=None,
                role=None,
                classification=ambiguous.classification.value if ambiguous else None,
                rule=ambiguous.rule if ambiguous else None,
                classifier_confidence=ambiguous.confidence if ambiguous else None,
                operator_review=(
                    _review_flag(ambiguous.classification.value, needs_confirmation=False)
                    if ambiguous and not handled
                    else None
                ),
                previous_status=None,
                new_status=None,
                action="IGNORE",
                transition_applied=False,
                event_recorded=False,
                duplicate=False,
                reason=(
                    (
                        "UNMATCHED_FROM_KNOWN_SENDER_HANDLED"
                        if handled
                        else "UNMATCHED_FROM_KNOWN_SENDER"
                    )
                    if known and ambiguous is not None
                    else correlation.reason
                ),
                review_candidates=known,
            )
        application: Application = self.tracker.get_application(db, correlation.application_id)
        logger.info(
            "gmail_correlated message_ref=%s application_id=%s confidence=%s signals=%s",
            ref,
            application.id,
            correlation.confidence,
            ",".join(correlation.signals),
        )
        classified = classify_application_response(
            message.subject, message.body, message.from_address, has_html=message.has_html
        )
        logger.info(
            "gmail_classified message_ref=%s classification=%s rule=%s",
            ref,
            classified.classification,
            classified.rule,
        )
        common = dict(
            **base,
            application_id=application.id,
            company=application.company_name,
            role=application.role_title,
            classification=classified.classification.value,
            rule=classified.rule,
            classifier_confidence=classified.confidence,
            previous_status=application.current_status.value,
        )
        if any(
            self.tracker.event_exists(db, key)
            for key in sorted(message.correlation_keys_across(monitored_account_keys))
        ):
            logger.info("gmail_duplicate message_ref=%s application_id=%s", ref, application.id)
            return ResponseTrackingResult(
                **common,
                new_status=application.current_status.value,
                action="IGNORE",
                transition_applied=False,
                event_recorded=False,
                duplicate=True,
                reason="ALREADY_PROCESSED",
            )
        decision = decide_response(
            application.current_status,
            classified.classification,
            correlation_confidence=correlation.confidence,
            classifier_confidence=classified.confidence,
        )
        if decision.action != "IGNORE" and needs_sender(correlation.signals):
            # Night audit (Codex round 3): the sender only POINTS to an
            # application; it may be another vacancy of the same employer
            # under an alias. Nothing is written; the operator decides.
            proposed = decision.target or decision.proposed_status
            return ResponseTrackingResult(
                **common,
                new_status=application.current_status.value,
                action="IGNORE",
                operator_review=NEEDS_OPERATOR_REVIEW,
                proposed_status=proposed.value if proposed else None,
                transition_applied=False,
                event_recorded=False,
                duplicate=False,
                reason="SENDER_ONLY_CORRELATION_NEEDS_OPERATOR",
            )
        intended = (decision.target or application.current_status).value
        if decision.action != "TRANSITION":
            logger.info(
                "transition_skipped message_ref=%s application_id=%s reason=%s",
                ref,
                application.id,
                decision.reason,
            )
        review = _review_flag(
            classified.classification.value if decision.reason == "CORRELATION_TOO_WEAK" else None,
            needs_confirmation=decision.operator_confirmation_required,
        )
        if decision.action == "IGNORE" or not apply:
            return ResponseTrackingResult(
                **common,
                operator_review=review,
                new_status=intended,
                action=decision.action,
                operator_confirmation_required=decision.operator_confirmation_required,
                proposed_status=(
                    decision.proposed_status.value if decision.proposed_status else None
                ),
                transition_applied=False,
                event_recorded=False,
                duplicate=False,
                reason=decision.reason,
            )

        evidence_text = (
            normalize_subject(message.subject)
            + "\n"
            + normalize_body(message.body, has_html=message.has_html)
        )
        metadata = {
            "classification": classified.classification.value,
            "base_category": classified.base_category,
            "rule": classified.rule,
            "classifier_confidence": classified.confidence,
            "evidence_fragment": classified.evidence_fragment,
            "correlation_confidence": correlation.confidence,
            "correlation_signals": list(correlation.signals),
            "message_id_hash": message.message_id_hash,
            "sender_address_hash": message.sender_address_hash,
            "decision": decision.reason,
            "operator_confirmation_required": decision.operator_confirmation_required,
            "proposed_status": (
                decision.proposed_status.value if decision.proposed_status else None
            ),
        }
        summary = (
            f"Inbound e-mail classified {classified.classification.value} "
            f"({classified.rule}); correlation {correlation.confidence} "
            f"[{', '.join(correlation.signals)}]"
        )
        kwargs = dict(
            correlation_key=message.correlation_key,
            occurred_at=message.occurred_at,
            evidence_hash=stable_hash(evidence_text),
            evidence_summary=summary,
            metadata=metadata,
        )
        try:
            if decision.action == "TRANSITION":
                outcome = self.tracker.transition_application(
                    db,
                    application.id,
                    decision.target,
                    origin=ApplicationEventOrigin.GMAIL,
                    actor=PROCESSOR_ACTOR,
                    reason=decision.reason,
                    **kwargs,
                )
            else:
                outcome = self.tracker.record_inbound_message(
                    db,
                    application.id,
                    origin=ApplicationEventOrigin.GMAIL,
                    actor=PROCESSOR_ACTOR,
                    reason=decision.reason,
                    followup_required=(
                        decision.followup_required or decision.operator_confirmation_required
                    ),
                    **kwargs,
                )
        except (ApplicationConcurrencyError, InvalidApplicationTransitionError):
            return ResponseTrackingResult(
                **common,
                new_status=application.current_status.value,
                action=decision.action,
                transition_applied=False,
                event_recorded=False,
                duplicate=False,
                reason="CONCURRENT_CHANGE_RETRY",
                operator_review=review,
                operator_confirmation_required=decision.operator_confirmation_required,
                proposed_status=(
                    decision.proposed_status.value if decision.proposed_status else None
                ),
            )
        return ResponseTrackingResult(
            **common,
            new_status=outcome.application.current_status.value,
            action=decision.action,
            transition_applied=outcome.transition_applied,
            operator_confirmation_required=decision.operator_confirmation_required,
            proposed_status=(decision.proposed_status.value if decision.proposed_status else None),
            event_recorded=outcome.event_recorded,
            duplicate=outcome.duplicate,
            reason=decision.reason if not outcome.duplicate else "ALREADY_PROCESSED",
            operator_review=review,
        )


MailboxSelection = Literal["primary", "secondary", "all"]
# Provider pages (each <= MAX_MESSAGES_PER_SYNC messages) one mailbox may
# take per run; a window larger than this is reported incomplete.
MAX_APPLICATION_SCAN_PAGES = 10


@dataclass(frozen=True)
class ApplicationMailbox:
    """One of the applicant's own accounts to read replies from."""

    label: str  # "primary" (GMAIL_*) | "secondary" (XING_MAILBOX_*)
    imap_host: str
    imap_port: int
    username: str
    app_password: str = field(repr=False)


@dataclass(frozen=True)
class MailboxFetchReport:
    """Per-mailbox fetch outcome -- counts and an error CLASS only, never a
    credential, address or message content."""

    label: str
    fetched: int = 0
    skipped: int = 0
    deadline_exceeded: bool = False
    error: str | None = None
    pages: int = 0

    @property
    def complete(self) -> bool:
        """True only when every message in the window was read: no error,
        no deadline, nothing skipped (cap, page limit, unreadable)."""
        return self.error is None and not self.deadline_exceeded and self.skipped == 0


def application_mailboxes(
    settings, selection: MailboxSelection = "all"
) -> list[ApplicationMailbox]:
    """Configured reply mailboxes. An account without both username and
    App Password is simply not configured (never a login attempt with empty
    credentials); the secondary is skipped when it is the primary account."""
    from app.providers.email.base import normalize_account_key

    mailboxes: list[ApplicationMailbox] = []
    if selection in ("primary", "all") and settings.gmail_username and settings.gmail_app_password:
        mailboxes.append(
            ApplicationMailbox(
                "primary",
                settings.gmail_imap_host,
                settings.gmail_imap_port,
                settings.gmail_username,
                settings.gmail_app_password,
            )
        )
    secondary = settings.xing_mailbox_username
    if (
        selection in ("secondary", "all")
        and secondary
        and settings.xing_mailbox_app_password
        and normalize_account_key(secondary) != normalize_account_key(settings.gmail_username or "")
    ):
        mailboxes.append(
            ApplicationMailbox(
                "secondary",
                settings.xing_mailbox_imap_host,
                settings.xing_mailbox_imap_port,
                secondary,
                settings.xing_mailbox_app_password,
            )
        )
    return mailboxes


def monitored_account_keys(settings) -> list[str]:
    """Every configured applicant mailbox account (identity only)."""
    from app.providers.email.base import normalize_account_key

    return [
        normalize_account_key(name)
        for name in (settings.gmail_username, settings.xing_mailbox_username)
        if name
    ]


# The operator's application identity file (project root, never committed).
APPLICATION_IDENTITY_PATH = Path(__file__).resolve().parents[2] / "application_identity.local.json"


def applicant_addresses(settings, identity_path: Path | None = None) -> list[str]:
    """The applicant's own addresses: mail FROM any of them is never an
    employer response (all monitored mailboxes plus the identity file)."""
    addresses = [a for a in (settings.gmail_username, settings.xing_mailbox_username) if a]
    path = APPLICATION_IDENTITY_PATH if identity_path is None else identity_path
    if path.is_file():
        from app.models.application_identity import load_application_identity

        identity = load_application_identity(path)
        if identity and identity.email:
            addresses.append(identity.email)
    return addresses


def stored_inbound_messages(db: Session, settings, *, since: datetime) -> list[InboundMessage]:
    """The primary account's already-synced inbound rows since `since`
    (arrival order). Reads the database only; IMAP is never contacted."""
    from app.providers.email.base import normalize_account_key

    account = normalize_account_key(settings.gmail_username or "")
    records = db.scalars(
        select(GmailMessageRecord)
        .where(
            GmailMessageRecord.account_key == account,
            GmailMessageRecord.direction == "INBOUND",
            GmailMessageRecord.provider_arrival_at >= since,
        )
        .order_by(GmailMessageRecord.provider_arrival_at, GmailMessageRecord.id)
    ).all()
    return [inbound_from_record(r) for r in records]


def _arrival_order(message: InboundMessage) -> tuple[int, datetime]:
    if message.occurred_at is None:
        return (1, datetime.max.replace(tzinfo=UTC))
    return (0, message.occurred_at)


async def fetch_application_mailboxes(
    settings,
    *,
    since: datetime,
    selection: MailboxSelection = "all",
    provider_factory=None,
) -> tuple[list[InboundMessage], list[MailboxFetchReport]]:
    """Live, read-only IMAP fetch (INBOX, readonly SELECT, `BODY.PEEK[]`) of
    every selected applicant mailbox since `since`. One failing mailbox
    never hides the others: its report carries the error class and the
    caller decides the exit status. Messages are merged in arrival order and
    a Message-ID seen in an earlier mailbox is dropped from later ones.

    A mailbox with more than MAX_MESSAGES_PER_SYNC messages in the window is
    read in successive pages (see `known_uids` below) until nothing is left,
    no page made progress, the deadline fired, or MAX_APPLICATION_SCAN_PAGES
    was reached; `MailboxFetchReport.complete` says which."""
    from app.providers.email.base import GmailProviderError
    from app.providers.email.imap import GmailImapProvider

    factory = provider_factory or GmailImapProvider
    merged: list[InboundMessage] = []
    reports: list[MailboxFetchReport] = []
    seen_ids: set[str] = set()
    for mailbox in application_mailboxes(settings, selection):
        # UIDs already handled IN THIS RUN, per UIDVALIDITY. The provider
        # drops them before applying MAX_MESSAGES_PER_SYNC (oldest first),
        # so each further page reaches the next-later UIDs instead of the
        # same capped oldest set. Nothing is persisted: the next run pages
        # the `since` window again, and re-processing is idempotent by
        # correlation key.
        handled: dict[int, set[int]] = {}

        def known_uids(uid_validity: int, candidates: list[int], _handled=handled) -> set[int]:
            return _handled.get(uid_validity, set()) & set(candidates)

        provider = factory(
            imap_host=mailbox.imap_host,
            imap_port=mailbox.imap_port,
            username=mailbox.username,
            app_password=mailbox.app_password,
            mailbox="INBOX",
            lookback_days=settings.gmail_lookback_days,
            get_known_uids=known_uids,
            trusted_outbound=False,
        )
        count = pages = earlier_permanent = 0
        last = None
        error: str | None = None
        while pages < MAX_APPLICATION_SCAN_PAGES:
            try:
                fetched = await provider.fetch(since=since)
            except (GmailProviderError, OSError, TimeoutError) as exc:
                logger.warning(
                    "application_mailbox_fetch_failed mailbox=%s page=%d error=%s",
                    mailbox.label,
                    pages + 1,
                    type(exc).__name__,
                )
                error = type(exc).__name__
                break
            pages += 1
            uids = handled.setdefault(fetched.uid_validity, set())
            before = len(uids)
            for parsed in fetched.messages:
                if parsed.uid in uids:
                    continue
                uids.add(parsed.uid)
                message = inbound_from_parsed(parsed)
                identity = message.message_id_hash
                if identity is not None:
                    if identity in seen_ids:
                        continue
                    seen_ids.add(identity)
                merged.append(message)
                count += 1
            uids.update(uid for uid, _reason in fetched.permanently_skipped)
            if last is not None:
                earlier_permanent += len(last.permanently_skipped)
            last = fetched
            if fetched.deadline_exceeded or fetched.skipped_count == 0 or len(uids) == before:
                break
        reports.append(
            MailboxFetchReport(
                mailbox.label,
                fetched=count,
                # Messages still unread at the end of this run: whatever the
                # last page could not reach or parse, plus earlier pages'
                # permanently unreadable ones (never retried).
                skipped=earlier_permanent + (last.skipped_count if last is not None else 0),
                deadline_exceeded=last is not None and last.deadline_exceeded,
                error=error,
                pages=pages,
            )
        )
    merged.sort(key=_arrival_order)
    return merged, reports
