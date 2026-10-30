"""Stage 15: the application tracker -- the ONLY writer of applications and
application events.

**One unit of work per call.** Every mutating method either commits one
event together with the status projection it implies, or rolls back and
leaves the database exactly as it was:

1. If the evidence's `correlation_key` already has an event, nothing is
   written and the outcome is `duplicate=True` (idempotent replay).
2. The event row is inserted at the next per-application `sequence`.
3. The application row is updated by CAS on `(id, current_status,
   version)`; losing the CAS (a concurrent writer) rolls everything back
   and raises `ApplicationConcurrencyError`.
4. Commit. A UNIQUE violation at commit (two writers racing on the same
   correlation key) is resolved by re-reading: if the key now exists the
   outcome is a duplicate, otherwise the error propagates.

**Local only.** Nothing here sends, drafts, posts or notifies. Status
changes never touch `JobRecord.status`.

Follow-up metadata is informational: while waiting for the employer
(SUBMITTED / RECEIVED / UNDER_REVIEW) `next_followup_at` is the last
employer contact (or the submission) plus `FOLLOWUP_INTERVAL`; it is never
acted on automatically. `followup_required` is set only when the employer
asked for something (REQUEST_FOR_INFORMATION) and cleared by the next
status change.
"""

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import application_repository as repo
from app.db.datetime_utils import ensure_utc
from app.db.models import ApplicationRecord
from app.domain.application_lifecycle import is_terminal, validate_transition
from app.models.application_lifecycle import (
    Application,
    ApplicationChannel,
    ApplicationDetail,
    ApplicationEvent,
    ApplicationEventOrigin,
    ApplicationEventType,
    ApplicationLifecycleStatus,
    OutboundMessageKind,
)

logger = logging.getLogger(__name__)

S = ApplicationLifecycleStatus
DEFAULT_PAGE_SIZE = 200
FOLLOWUP_INTERVAL = timedelta(days=14)
_WAITING_STATUSES = frozenset({S.SUBMITTED, S.RECEIVED, S.UNDER_REVIEW})
MAX_ACTOR_LENGTH = 100
MAX_REASON_LENGTH = 1000
MAX_SUMMARY_LENGTH = 500
MAX_MESSAGE_ID_LENGTH = 300
MAX_SUBJECT_LENGTH = 200
_MESSAGE_ID = re.compile(r"^<[^<>\s@]+@[^<>\s@]+>$")
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class ApplicationNotFoundError(LookupError):
    pass


class ApplicationConcurrencyError(RuntimeError):
    """The application changed between read and CAS update."""


class ApplicationInputError(ValueError):
    pass


class TerminalStatusNotPermittedError(ApplicationInputError):
    """A terminal status (OFFER/REJECTED/WITHDRAWN) was requested by an
    origin other than OPERATOR, or without a reason."""


@dataclass(frozen=True)
class EventOutcome:
    application: Application
    event: ApplicationEvent | None
    event_recorded: bool
    duplicate: bool
    transition_applied: bool
    previous_status: ApplicationLifecycleStatus | None
    reason: str


def _now() -> datetime:
    return datetime.now(UTC)


def _require_text(name: str, value: str | None, limit: int) -> str:
    if value is None or not value.strip():
        raise ApplicationInputError(f"{name} is required")
    if len(value) > limit:
        raise ApplicationInputError(f"{name} is longer than {limit} characters")
    return value.strip()


def outbound_correlation_key(message_id: str, application_id: int) -> str:
    """Deterministic idempotency key of one sent message FOR one application.

    The same Message-ID may legitimately belong to several applications (one
    mail covering two vacancies of the same employer), so the application id
    is part of the key; recording the same message for the same application
    twice is a duplicate."""
    digest = hashlib.sha256(f"{message_id}|{application_id}".encode()).hexdigest()
    return f"outbound:{digest[:40]}"


def validate_outbound_message(
    *,
    message_id: str,
    recipient: str,
    sent_at: datetime,
    actor: str,
    reason: str,
    sender: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str, datetime]:
    """Normalized (message_id, recipient, sent_at UTC) of a sent message, or
    ApplicationInputError. Shared by the tracker and the CLI dry run, so a
    dry run refuses exactly what --apply would refuse."""
    _require_text("actor", actor, MAX_ACTOR_LENGTH)
    _require_text("reason", reason, MAX_REASON_LENGTH)
    message = _require_text("message_id", message_id, MAX_MESSAGE_ID_LENGTH)
    if not _MESSAGE_ID.match(message):
        raise ApplicationInputError("message_id must look like <local@domain>")
    to = _require_text("recipient", recipient, 254)
    if not _EMAIL.match(to):
        raise ApplicationInputError("recipient must be a plain e-mail address")
    if sender is not None and not _EMAIL.match(sender.strip()):
        raise ApplicationInputError("sender must be a plain e-mail address")
    if sent_at.tzinfo is None:
        raise ApplicationInputError("sent_at needs an explicit timezone")
    when = ensure_utc(sent_at)
    if when > (now or _now()):
        raise ApplicationInputError("sent_at lies in the future")
    return message, to, when


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _followup_values(
    status: S, record: ApplicationRecord, contact_at: datetime | None
) -> dict[str, Any]:
    if status not in _WAITING_STATUSES:
        return {"next_followup_at": None}
    anchor = contact_at or (
        ensure_utc(record.last_employer_contact_at) if record.last_employer_contact_at else None
    )
    anchor = anchor or (ensure_utc(record.submitted_at) if record.submitted_at else None)
    return {"next_followup_at": anchor + FOLLOWUP_INTERVAL if anchor else None}


class ApplicationTracker:
    # --- reads ---------------------------------------------------------------

    def get_application(self, db: Session, application_id: int) -> Application:
        record = repo.get_application(db, application_id)
        if record is None:
            raise ApplicationNotFoundError(application_id)
        return repo.to_application(record)

    def list_applications(
        self,
        db: Session,
        statuses: tuple[S, ...] | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        after_id: int | None = None,
    ) -> list[Application]:
        """One explicit page (id order). A full page means more may exist:
        continue with `after_id=<last id>`. For a complete set use
        `iter_correlation_candidates` / `repo.iter_applications`."""
        return [
            repo.to_application(r)
            for r in repo.list_applications(db, statuses=statuses, limit=limit, after_id=after_id)
        ]

    def iter_correlation_candidates(
        self,
        db: Session,
        *,
        only_ids: frozenset[int] | None = None,
        batch_size: int = repo.DEFAULT_SCAN_BATCH_SIZE,
    ) -> Iterator[Application]:
        """Every submitted application (batched keyset scan) -- correlation
        must never miss a candidate because of a page size."""
        for record in repo.iter_applications(
            db, submitted_only=True, only_ids=only_ids, batch_size=batch_size
        ):
            yield repo.to_application(record)

    def get_application_history(self, db: Session, application_id: int) -> list[ApplicationEvent]:
        self.get_application(db, application_id)
        return [repo.to_application_event(e) for e in repo.list_events(db, application_id)]

    def get_application_detail(self, db: Session, application_id: int) -> ApplicationDetail:
        return ApplicationDetail(
            application=self.get_application(db, application_id),
            events=self.get_application_history(db, application_id),
        )

    def find_by_source_reference(
        self, db: Session, source: str, source_reference: str
    ) -> Application | None:
        record = repo.get_application_by_source_reference(db, source, source_reference)
        return repo.to_application(record) if record else None

    def find_active_for_company_role(
        self, db: Session, company_name: str, role_title: str
    ) -> list[Application]:
        role = role_title.casefold().strip()
        return [
            repo.to_application(r)
            for r in repo.list_applications_for_company(db, company_name)
            if r.role_title.casefold().strip() == role
            and S(r.current_status) not in {S.OFFER, S.REJECTED, S.WITHDRAWN}
        ]

    def event_exists(self, db: Session, correlation_key: str) -> bool:
        return repo.get_event_by_correlation_key(db, correlation_key) is not None

    # --- writes --------------------------------------------------------------

    def create_application(
        self,
        db: Session,
        *,
        company_name: str,
        role_title: str,
        source: str,
        source_reference: str | None,
        application_channel: ApplicationChannel,
        initial_status: S = S.DISCOVERED,
        origin: ApplicationEventOrigin,
        actor: str,
        occurred_at: datetime | None = None,
        location: str | None = None,
        external_job_id: str | None = None,
        vacancy_fingerprint: str | None = None,
        destination_url: str | None = None,
        employer_domains: tuple[str, ...] = (),
        candidate_profile_id: int | None = None,
        job_id: int | None = None,
        review_package_id: int | None = None,
        correlation_key: str | None = None,
        evidence_hash: str | None = None,
        evidence_summary: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EventOutcome:
        """Create (or idempotently locate) an application. An existing
        `(source, source_reference)` is returned unchanged, never updated."""
        if is_terminal(S(initial_status)):
            raise TerminalStatusNotPermittedError("an application cannot start terminal")
        company = _require_text("company_name", company_name, 300)
        role = _require_text("role_title", role_title, 300)
        source_value = _require_text("source", source, 64)
        actor_value = _require_text("actor", actor, MAX_ACTOR_LENGTH)
        if source_reference is not None:
            existing = repo.get_application_by_source_reference(db, source_value, source_reference)
            if existing is not None:
                return self._duplicate(existing, correlation_key, db, "APPLICATION_EXISTS")
        if correlation_key and repo.get_event_by_correlation_key(db, correlation_key):
            event = repo.get_event_by_correlation_key(db, correlation_key)
            record = repo.get_application(db, event.application_id)
            return self._duplicate(record, correlation_key, db, "DUPLICATE_EVIDENCE")
        when = ensure_utc(occurred_at) if occurred_at else _now()
        domains = sorted({d.strip().casefold() for d in employer_domains if d and d.strip()})
        values: dict[str, Any] = dict(
            candidate_profile_id=candidate_profile_id,
            job_id=job_id,
            company_name=company,
            role_title=role,
            location=location,
            source=source_value,
            source_reference=source_reference,
            external_job_id=external_job_id,
            vacancy_fingerprint=vacancy_fingerprint,
            application_channel=ApplicationChannel(application_channel).value,
            destination_url=destination_url,
            employer_domains_json=json.dumps(domains),
            review_package_id=review_package_id,
            current_status=S(initial_status).value,
            submitted_at=when if initial_status == S.SUBMITTED else None,
            last_status_changed_at=when,
            version=1,
            created_at=_now(),
            updated_at=_now(),
        )
        try:
            record = repo.insert_application(db, **values)
            if initial_status == S.SUBMITTED:
                record.next_followup_at = when + FOLLOWUP_INTERVAL
            event = repo.insert_event(
                db,
                application_id=record.id,
                sequence=1,
                event_type=ApplicationEventType.CREATED.value,
                old_status=None,
                new_status=S(initial_status).value,
                origin=ApplicationEventOrigin(origin).value,
                actor=actor_value,
                reason=_bounded(reason, MAX_REASON_LENGTH),
                source_reference=source_reference,
                occurred_at=when,
                evidence_hash=evidence_hash,
                evidence_summary=_bounded(evidence_summary, MAX_SUMMARY_LENGTH),
                correlation_key=correlation_key,
                metadata=metadata,
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = (
                repo.get_application_by_source_reference(db, source_value, source_reference)
                if source_reference is not None
                else None
            )
            if existing is not None:
                return self._duplicate(existing, correlation_key, db, "APPLICATION_EXISTS")
            raise
        logger.info(
            "application_created application_id=%s status=%s origin=%s",
            record.id,
            initial_status,
            origin,
        )
        db.refresh(record)
        return EventOutcome(
            application=repo.to_application(record),
            event=repo.to_application_event(event),
            event_recorded=True,
            duplicate=False,
            transition_applied=True,
            previous_status=None,
            reason="CREATED",
        )

    def transition_application(
        self,
        db: Session,
        application_id: int,
        target: S,
        *,
        origin: ApplicationEventOrigin,
        actor: str,
        occurred_at: datetime | None = None,
        correlation_key: str | None = None,
        evidence_hash: str | None = None,
        evidence_summary: str | None = None,
        source_reference: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        chronological: bool = False,
    ) -> EventOutcome:
        """Policy-validated status change; raises
        `InvalidApplicationTransitionError` for a transition not in the
        table (including any exit from a terminal status). `chronological`
        (Stage 22 P2, an operator-given `occurred_at`): refuse a time before
        the current status was set."""
        return self._write(
            db,
            application_id,
            event_type=ApplicationEventType.STATUS_CHANGED,
            target=S(target),
            origin=origin,
            actor=actor,
            occurred_at=occurred_at,
            correlation_key=correlation_key,
            evidence_hash=evidence_hash,
            evidence_summary=evidence_summary,
            source_reference=source_reference,
            reason=reason,
            metadata=metadata,
            chronological=chronological,
        )

    def record_submission(self, db: Session, application_id: int, **kwargs: Any) -> EventOutcome:
        return self.transition_application(db, application_id, S.SUBMITTED, **kwargs)

    def record_receipt(self, db: Session, application_id: int, **kwargs: Any) -> EventOutcome:
        return self.transition_application(db, application_id, S.RECEIVED, **kwargs)

    def override_status(
        self,
        db: Session,
        application_id: int,
        target: S,
        *,
        actor: str,
        reason: str,
        occurred_at: datetime | None = None,
        correlation_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        chronological: bool = False,
    ) -> EventOutcome:
        """Operator correction OUTSIDE the transition table (e.g. leaving a
        terminal status). Always origin OPERATOR; requires an actor and a
        non-empty reason; the old status stays in history -- nothing is
        rewritten."""
        _require_text("reason", reason, MAX_REASON_LENGTH)
        _require_text("actor", actor, MAX_ACTOR_LENGTH)
        origin = ApplicationEventOrigin.OPERATOR
        outcome = self._write(
            db,
            application_id,
            event_type=ApplicationEventType.STATUS_OVERRIDDEN,
            target=S(target),
            origin=origin,
            actor=actor,
            occurred_at=occurred_at,
            correlation_key=correlation_key,
            reason=reason,
            metadata=metadata,
            chronological=chronological,
        )
        if outcome.event_recorded:
            logger.info(
                "manual_override application_id=%s old_status=%s new_status=%s origin=%s",
                application_id,
                outcome.previous_status,
                target,
                origin,
            )
        return outcome

    def record_inbound_message(
        self,
        db: Session,
        application_id: int,
        *,
        correlation_key: str,
        occurred_at: datetime | None,
        origin: ApplicationEventOrigin = ApplicationEventOrigin.GMAIL,
        actor: str = "application_response_processor",
        reason: str,
        evidence_hash: str | None = None,
        evidence_summary: str | None = None,
        followup_required: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> EventOutcome:
        """Correlated correspondence that does not change the status."""
        return self._write(
            db,
            application_id,
            event_type=ApplicationEventType.INBOUND_MESSAGE,
            target=None,
            origin=origin,
            actor=actor,
            occurred_at=occurred_at,
            correlation_key=correlation_key,
            evidence_hash=evidence_hash,
            evidence_summary=evidence_summary,
            reason=reason,
            metadata=metadata,
            followup_required=followup_required,
        )

    def record_outbound_message(
        self,
        db: Session,
        application_id: int,
        *,
        message_id: str,
        recipient: str,
        channel: ApplicationChannel,
        sent_at: datetime,
        kind: OutboundMessageKind,
        actor: str,
        reason: str,
        sender: str | None = None,
        subject: str | None = None,
        evidence_summary: str | None = None,
        related_application_ids: tuple[int, ...] = (),
        now: datetime | None = None,
    ) -> EventOutcome:
        """Record a service message the candidate ALREADY sent about an
        existing application (e.g. a phone-number correction).

        Never sends anything and never changes the status, the follow-up date
        or `last_employer_contact_at`. Idempotent per (Message-ID,
        application): a replay returns `duplicate=True` and writes nothing.
        `related_application_ids` lists the other applications the same mail
        was recorded for (informational, stored in metadata).
        """
        message, to, when = validate_outbound_message(
            message_id=message_id,
            recipient=recipient,
            sent_at=sent_at,
            actor=actor,
            reason=reason,
            sender=sender,
            now=now,
        )
        metadata: dict[str, Any] = {
            "direction": "OUTBOUND",
            "kind": OutboundMessageKind(kind).value,
            "message_id": message,
            "recipient": to,
            "channel": ApplicationChannel(channel).value,
        }
        if sender:
            metadata["sender"] = sender.strip()
        if subject:
            metadata["subject"] = _bounded(subject.strip(), MAX_SUBJECT_LENGTH)
        related = sorted({int(i) for i in related_application_ids} - {application_id})
        if related:
            metadata["related_application_ids"] = related
        return self._write(
            db,
            application_id,
            event_type=ApplicationEventType.OUTBOUND_MESSAGE,
            target=None,
            origin=ApplicationEventOrigin.OPERATOR,
            actor=actor,
            occurred_at=when,
            correlation_key=outbound_correlation_key(message, application_id),
            evidence_hash=hashlib.sha256(message.encode()).hexdigest(),
            evidence_summary=evidence_summary,
            reason=reason,
            metadata=metadata,
        )

    # --- internals -----------------------------------------------------------

    def _duplicate(
        self,
        record: ApplicationRecord,
        correlation_key: str | None,
        db: Session,
        reason: str,
    ) -> EventOutcome:
        event = repo.get_event_by_correlation_key(db, correlation_key) if correlation_key else None
        logger.info("application_event_duplicate application_id=%s reason=%s", record.id, reason)
        return EventOutcome(
            application=repo.to_application(record),
            event=repo.to_application_event(event) if event else None,
            event_recorded=False,
            duplicate=True,
            transition_applied=False,
            previous_status=S(record.current_status),
            reason=reason,
        )

    def _write(
        self,
        db: Session,
        application_id: int,
        *,
        event_type: ApplicationEventType,
        target: S | None,
        origin: ApplicationEventOrigin,
        actor: str,
        occurred_at: datetime | None,
        correlation_key: str | None,
        evidence_hash: str | None = None,
        evidence_summary: str | None = None,
        source_reference: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        followup_required: bool = False,
        chronological: bool = False,
    ) -> EventOutcome:
        actor_value = _require_text("actor", actor, MAX_ACTOR_LENGTH)
        record = repo.get_application(db, application_id)
        if record is None:
            raise ApplicationNotFoundError(application_id)
        if correlation_key and repo.get_event_by_correlation_key(db, correlation_key):
            return self._duplicate(record, correlation_key, db, "DUPLICATE_EVIDENCE")
        current = S(record.current_status)
        if event_type == ApplicationEventType.STATUS_CHANGED:
            validate_transition(current, target)
        elif event_type == ApplicationEventType.STATUS_OVERRIDDEN and target == current:
            raise ApplicationInputError("override target equals the current status")
        if event_type == ApplicationEventType.STATUS_OVERRIDDEN and (
            origin != ApplicationEventOrigin.OPERATOR
        ):
            raise TerminalStatusNotPermittedError("overrides are operator actions only")
        if target is not None and is_terminal(target):
            # Codex S15 P1: the single lifecycle writer refuses a terminal
            # status from any non-operator origin -- inbound mail (GMAIL) is
            # unauthenticated, and SYSTEM/PORTAL/IMPORT never decide outcomes.
            if origin != ApplicationEventOrigin.OPERATOR:
                raise TerminalStatusNotPermittedError(
                    f"{target} may only be set by an OPERATOR (origin was {origin})"
                )
            _require_text("reason", reason, MAX_REASON_LENGTH)

        when = ensure_utc(occurred_at) if occurred_at else None
        if (
            chronological
            and when is not None
            and record.last_status_changed_at is not None
            and when < ensure_utc(record.last_status_changed_at)
        ):
            # Stage 22 P2: checked on the row this write is version-bound to
            # (the update below is a compare-and-swap on `version`), so a
            # concurrent change can never let history run backwards.
            raise ApplicationInputError(
                "occurred_at lies before the current status was set "
                f"({ensure_utc(record.last_status_changed_at).isoformat()})"
            )
        now = _now()
        new_status = target if target is not None else current
        values: dict[str, Any] = {"updated_at": now}
        if target is not None:
            values["current_status"] = target.value
            values["last_status_changed_at"] = when or now
            values["followup_required"] = False
            if target == S.SUBMITTED and record.submitted_at is None:
                values["submitted_at"] = when or now
            if target == S.RECEIVED and record.received_at is None:
                received = when or now
                submitted = values.get("submitted_at") or (
                    ensure_utc(record.submitted_at) if record.submitted_at else None
                )
                # Employer clocks may be slightly behind ours; a receipt is
                # never recorded as earlier than the submission it confirms.
                values["received_at"] = max(received, submitted) if submitted else received
        contact_at = None
        if origin == ApplicationEventOrigin.GMAIL and when is not None:
            previous = (
                ensure_utc(record.last_employer_contact_at)
                if record.last_employer_contact_at
                else None
            )
            contact_at = max(when, previous) if previous else when
            values["last_employer_contact_at"] = contact_at
        if followup_required:
            values["followup_required"] = True
        if event_type != ApplicationEventType.OUTBOUND_MESSAGE:
            # A candidate's own service mail is not employer contact and does
            # not move the follow-up schedule.
            values.update(_followup_values(new_status, record, contact_at))

        expected_version = record.version
        try:
            event = repo.insert_event(
                db,
                application_id=application_id,
                sequence=repo.next_event_sequence(db, application_id),
                event_type=event_type.value,
                old_status=current.value if target is not None else None,
                new_status=target.value if target is not None else None,
                origin=ApplicationEventOrigin(origin).value,
                actor=actor_value,
                reason=_bounded(reason, MAX_REASON_LENGTH),
                source_reference=source_reference,
                occurred_at=when,
                evidence_hash=evidence_hash,
                evidence_summary=_bounded(evidence_summary, MAX_SUMMARY_LENGTH),
                correlation_key=correlation_key,
                metadata=metadata,
            )
            if not repo.cas_update_application(
                db,
                application_id,
                expected_status=current.value,
                expected_version=expected_version,
                values=values,
            ):
                raise ApplicationConcurrencyError(application_id)
            db.commit()
        except ApplicationConcurrencyError:
            db.rollback()
            raise
        except IntegrityError:
            db.rollback()
            if correlation_key and repo.get_event_by_correlation_key(db, correlation_key):
                record = repo.get_application(db, application_id)
                return self._duplicate(record, correlation_key, db, "DUPLICATE_EVIDENCE")
            raise
        except Exception:
            db.rollback()
            raise
        db.expire_all()
        record = repo.get_application(db, application_id)
        if target is not None:
            logger.info(
                "status_transition application_id=%s old_status=%s new_status=%s origin=%s "
                "event_type=%s",
                application_id,
                current,
                target,
                origin,
                event_type,
            )
        return EventOutcome(
            application=repo.to_application(record),
            event=repo.to_application_event(event),
            event_recorded=True,
            duplicate=False,
            transition_applied=target is not None,
            previous_status=current,
            reason=event_type.value,
        )
