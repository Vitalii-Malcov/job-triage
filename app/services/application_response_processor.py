"""Stage 15: inbound employer e-mail -> application lifecycle.

    fetch (read-only) -> normalize -> correlate -> classify -> decide
      -> [apply only] one transaction: event + status projection

**Read-only towards Gmail, local-only towards everything else.** Messages
come either from a live read-only IMAP fetch (`fetch_inbound_messages`:
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
"""

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

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
    message_key_for,
    normalize_occurred_at,
    stable_hash,
)
from app.services.application_tracker import ApplicationConcurrencyError, ApplicationTracker

logger = logging.getLogger(__name__)
PROCESSOR_ACTOR = "stage15_response_processor"


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
    ) -> list[ResponseTrackingResult]:
        """`only_application_ids` restricts correlation candidates (e.g. a
        one-application bootstrap); every other application is untouched."""
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
    ) -> ResponseTrackingResult:
        ref = message.correlation_key.removeprefix("gmail:")[:12]
        # Complete candidate set via a batched keyset scan (bounded memory);
        # never a single page that could silently drop application #201+.
        correlation = correlate_message(
            message,
            self.tracker.iter_correlation_candidates(db, only_ids=only_application_ids),
            applicant_addresses=applicant_addresses,
            thread_hashes_by_application=_thread_hashes(db, message),
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
            return ResponseTrackingResult(
                **base,
                application_id=None,
                company=None,
                role=None,
                classification=None,
                rule=None,
                classifier_confidence=None,
                previous_status=None,
                new_status=None,
                action="IGNORE",
                transition_applied=False,
                event_recorded=False,
                duplicate=False,
                reason=correlation.reason,
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
        if self.tracker.event_exists(db, message.correlation_key):
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
        intended = (decision.target or application.current_status).value
        if decision.action != "TRANSITION":
            logger.info(
                "transition_skipped message_ref=%s application_id=%s reason=%s",
                ref,
                application.id,
                decision.reason,
            )
        if decision.action == "IGNORE" or not apply:
            return ResponseTrackingResult(
                **common,
                new_status=intended,
                action=decision.action,
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
            "decision": decision.reason,
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
                    followup_required=decision.followup_required,
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
            )
        return ResponseTrackingResult(
            **common,
            new_status=outcome.application.current_status.value,
            action=decision.action,
            transition_applied=outcome.transition_applied,
            event_recorded=outcome.event_recorded,
            duplicate=outcome.duplicate,
            reason=decision.reason if not outcome.duplicate else "ALREADY_PROCESSED",
        )


async def fetch_inbound_messages(settings, *, since: datetime) -> list[InboundMessage]:
    """Live, read-only IMAP fetch of the configured primary mailbox since
    `since` (bounded by the provider's own per-run limits). Nothing is
    persisted; no flag is changed (`BODY.PEEK[]`, readonly SELECT)."""
    from app.providers.email.imap import GmailImapProvider

    provider = GmailImapProvider(
        imap_host=settings.gmail_imap_host,
        imap_port=settings.gmail_imap_port,
        username=settings.gmail_username,
        app_password=settings.gmail_app_password,
        mailbox=settings.gmail_mailbox,
        lookback_days=settings.gmail_lookback_days,
        get_known_uids=lambda _uid_validity, _candidates: set(),
        trusted_outbound=False,
    )
    fetched = await provider.fetch(since=since)
    return [inbound_from_parsed(m) for m in fetched.messages]
