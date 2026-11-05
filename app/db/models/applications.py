"""ORM models: applications (import via `app.db.models`)."""

from datetime import UTC, datetime

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy import (
    false as sa_false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import _in_list
from app.models.application_lifecycle import (
    ApplicationChannel,
    ApplicationEventOrigin,
    ApplicationEventType,
    ApplicationLifecycleStatus,
)
from app.models.telegram_operator import OperatorDecision

APPLICATION_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in ApplicationLifecycleStatus)


APPLICATION_CHECKS: dict[str, str] = {
    "ck_applications_current_status_valid": _in_list("current_status", APPLICATION_STATUS_VALUES),
    "ck_applications_channel_valid": _in_list(
        "application_channel", (c.value for c in ApplicationChannel)
    ),
    "ck_applications_version_positive": "version >= 1",
    "ck_applications_submitted_before_received": (
        "received_at IS NULL OR (submitted_at IS NOT NULL AND received_at >= submitted_at)"
    ),
}


APPLICATION_EVENT_CHECKS: dict[str, str] = {
    "ck_application_events_type_valid": _in_list(
        "event_type", (t.value for t in ApplicationEventType)
    ),
    "ck_application_events_origin_valid": _in_list(
        "origin", (o.value for o in ApplicationEventOrigin)
    ),
    "ck_application_events_old_status_valid": (
        "old_status IS NULL OR " + _in_list("old_status", APPLICATION_STATUS_VALUES)
    ),
    "ck_application_events_new_status_valid": (
        "new_status IS NULL OR " + _in_list("new_status", APPLICATION_STATUS_VALUES)
    ),
    "ck_application_events_sequence_positive": "sequence >= 1",
    # A status event always names its target; an inbound-message event never
    # changes status; an outbound-message event names no status at all; only
    # CREATED has no previous status.
    "ck_application_events_shape": (
        "(event_type = 'CREATED' AND old_status IS NULL AND new_status IS NOT NULL) OR "
        "(event_type IN ('STATUS_CHANGED', 'STATUS_OVERRIDDEN') "
        "AND old_status IS NOT NULL AND new_status IS NOT NULL) OR "
        "(event_type = 'INBOUND_MESSAGE' AND new_status IS NULL) OR "
        "(event_type = 'OUTBOUND_MESSAGE' AND old_status IS NULL AND new_status IS NULL)"
    ),
    "ck_application_events_override_reason": (
        "event_type <> 'STATUS_OVERRIDDEN' OR (reason IS NOT NULL AND reason <> '')"
    ),
}


class ApplicationRecord(Base):
    """Stage 15: one real job application and its CURRENT lifecycle status.

    The status is a cached projection of the immutable
    `ApplicationEventRecord` history: it is only ever changed in the same
    transaction that appends the event explaining the change, through a CAS
    UPDATE on `(id, current_status, version)` (see
    app/db/application_repository.py). `JobRecord.status` is never touched.

    **Identity is external, not DB-local.** `(source, source_reference)` --
    the Stage 14D stable vacancy identity, e.g. ("bundesagentur",
    "20385-2347902-S") -- is unique; `job_id` is an optional, informational
    pointer to a local `JobRecord` and deliberately NOT a foreign key (a
    re-imported or rebuilt jobs table must never orphan or cascade-delete
    an application's history).

    `employer_domains_json` is the operator/evidence-confirmed list of
    e-mail domains the employer (or its applicant-tracking provider) writes
    from; inbound correlation treats it as a supporting signal, never as
    authentication (inbound mail is unauthenticated -- see
    app/services/application_correlation.py).
    """

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("source", "source_reference", name="uq_applications_source_reference"),
        *(CheckConstraint(body, name=name) for name, body in APPLICATION_CHECKS.items()),
        Index("ix_applications_company_name", "company_name"),
        Index("ix_applications_external_job_id", "external_job_id"),
        Index("ix_applications_created_at", "created_at"),
        Index("ix_applications_last_status_changed_at", "last_status_changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)
    role_title: Mapped[str] = mapped_column(String(300), nullable=False)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vacancy_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    application_channel: Mapped[str] = mapped_column(String(20), nullable=False)
    destination_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    employer_domains_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    review_package_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_employer_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_followup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    followup_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ApplicationEventRecord(Base):
    """Stage 15: one immutable fact in an application's history.

    **Append-only.** Rows are only ever INSERTed (the repository has no
    update/delete path, and the ORM guards below refuse an UPDATE or DELETE
    flush). `sequence` is the 1-based position within one application,
    unique per application, so concurrent writers cannot interleave two
    events at the same position.

    **Idempotency.** `correlation_key` is a deterministic key derived from
    the evidence that caused the event (an inbound Gmail message, the portal
    submission evidence, an import step) -- never random -- and is UNIQUE
    across the whole table (NULLs allowed for operator actions). Reprocessing
    the same evidence therefore cannot create a second event, and because
    the status change is committed in the same transaction as the event, it
    cannot create a second transition either.

    `metadata_json` carries bounded, non-body details (classifier rule,
    correlation signals); full e-mail bodies are never stored here.
    """

    __tablename__ = "application_events"
    __table_args__ = (
        UniqueConstraint("application_id", "sequence", name="uq_application_events_sequence"),
        UniqueConstraint("correlation_key", name="uq_application_events_correlation_key"),
        *(CheckConstraint(body, name=name) for name, body in APPLICATION_EVENT_CHECKS.items()),
        Index("ix_application_events_application_occurred", "application_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    old_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    correlation_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


# Database-level append-only guarantee (Codex S15 P2). The migration
# c8e4a2f6d1b9 creates exactly these objects; the listeners below make
# `Base.metadata.create_all` (test and dev schemas) install them as well, so
# the guarantee never depends on the ORM guards further down.
APPLICATION_EVENT_IMMUTABILITY_DDL: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "CREATE TRIGGER trg_application_events_no_update BEFORE UPDATE ON application_events "
        "BEGIN SELECT RAISE(ABORT, 'application_events is append-only'); END",
        "CREATE TRIGGER trg_application_events_no_delete BEFORE DELETE ON application_events "
        "BEGIN SELECT RAISE(ABORT, 'application_events is append-only'); END",
    ),
    "postgresql": (
        "CREATE FUNCTION application_events_append_only() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'application_events is append-only' "
        "USING ERRCODE = 'restrict_violation'; END $$",
        "CREATE TRIGGER trg_application_events_append_only BEFORE UPDATE OR DELETE "
        "ON application_events FOR EACH ROW EXECUTE FUNCTION application_events_append_only()",
    ),
}


_APPLICATION_EVENT_IMMUTABILITY_DROP: dict[str, tuple[str, ...]] = {
    "postgresql": ("DROP FUNCTION IF EXISTS application_events_append_only()",),
}


for _dialect, _statements in APPLICATION_EVENT_IMMUTABILITY_DDL.items():
    for _statement in _statements:
        event.listen(
            ApplicationEventRecord.__table__,
            "after_create",
            DDL(_statement).execute_if(dialect=_dialect),
        )


for _dialect, _statements in _APPLICATION_EVENT_IMMUTABILITY_DROP.items():
    for _statement in _statements:
        event.listen(
            ApplicationEventRecord.__table__,
            "after_drop",
            DDL(_statement).execute_if(dialect=_dialect),
        )


class ApplicationEventImmutableError(RuntimeError):
    """An UPDATE or DELETE of an ApplicationEventRecord was attempted."""


@event.listens_for(ApplicationEventRecord, "before_update")
def _refuse_event_update(mapper, connection, target) -> None:
    raise ApplicationEventImmutableError("application events are append-only")


@event.listens_for(ApplicationEventRecord, "before_delete")
def _refuse_event_delete(mapper, connection, target) -> None:
    raise ApplicationEventImmutableError("application events are append-only")


OPERATOR_DECISION_VALUES: tuple[str, ...] = tuple(d.value for d in OperatorDecision)


OPERATOR_DECISION_CHECKS: dict[str, str] = {
    "ck_application_operator_decisions_decision_valid": _in_list(
        "decision", OPERATOR_DECISION_VALUES
    ),
    # An override always names the decision it supersedes and says why.
    "ck_application_operator_decisions_override_reason": (
        "supersedes_decision_id IS NULL OR reason IS NOT NULL"
    ),
}


class ApplicationOperatorDecisionRecord(Base):
    """Stage 16: one operator decision (APPROVE / SKIP / LATER) about one
    review target. Append-only audit: rows are only ever INSERTed (ORM
    guards below and database triggers refuse UPDATE/DELETE); an override
    is a NEW row whose `supersedes_decision_id` names the decision it
    replaces, with a mandatory reason.

    **Idempotency.** `idempotency_key` is deterministic and UNIQUE:
    `<target_key>:final:<generation>` admits exactly one final decision per
    target and override generation, `<target_key>:later:<generation>` at
    most one LATER, and `<target_key>:override:<n>` one override per step.
    `callback_ref` (SHA-256 of the Telegram callback-query id) is UNIQUE, so
    a retried delivery of the same callback can never record twice.

    Telegram identities are stored only as SHA-256 references
    (`operator_ref`, `chat_ref`), never raw.
    """

    __tablename__ = "application_operator_decisions"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_application_operator_decisions_idempotency_key"
        ),
        UniqueConstraint("callback_ref", name="uq_application_operator_decisions_callback_ref"),
        *(CheckConstraint(body, name=name) for name, body in OPERATOR_DECISION_CHECKS.items()),
        Index("ix_application_operator_decisions_target_key", "target_key"),
        Index("ix_application_operator_decisions_application_id", "application_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    card_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("telegram_operator_cards.id", ondelete="RESTRICT"), nullable=True
    )
    review_package_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_key: Mapped[str] = mapped_column(String(80), nullable=False)
    vacancy_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    supersedes_decision_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("application_operator_decisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    callback_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(String(40), nullable=False)
    operator_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chat_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lifecycle_correlation_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


# Same append-only shape as APPLICATION_EVENT_IMMUTABILITY_DDL; the Stage 16
# migration creates exactly these objects.
OPERATOR_DECISION_IMMUTABILITY_DDL: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "CREATE TRIGGER trg_application_operator_decisions_no_update BEFORE UPDATE ON "
        "application_operator_decisions "
        "BEGIN SELECT RAISE(ABORT, 'application_operator_decisions is append-only'); END",
        "CREATE TRIGGER trg_application_operator_decisions_no_delete BEFORE DELETE ON "
        "application_operator_decisions "
        "BEGIN SELECT RAISE(ABORT, 'application_operator_decisions is append-only'); END",
    ),
    "postgresql": (
        "CREATE FUNCTION application_operator_decisions_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
        "'application_operator_decisions is append-only' "
        "USING ERRCODE = 'restrict_violation'; END $$",
        "CREATE TRIGGER trg_application_operator_decisions_append_only BEFORE UPDATE OR DELETE "
        "ON application_operator_decisions FOR EACH ROW "
        "EXECUTE FUNCTION application_operator_decisions_append_only()",
    ),
}


_OPERATOR_DECISION_IMMUTABILITY_DROP: dict[str, tuple[str, ...]] = {
    "postgresql": ("DROP FUNCTION IF EXISTS application_operator_decisions_append_only()",),
}


for _dialect, _statements in OPERATOR_DECISION_IMMUTABILITY_DDL.items():
    for _statement in _statements:
        event.listen(
            ApplicationOperatorDecisionRecord.__table__,
            "after_create",
            DDL(_statement).execute_if(dialect=_dialect),
        )


for _dialect, _statements in _OPERATOR_DECISION_IMMUTABILITY_DROP.items():
    for _statement in _statements:
        event.listen(
            ApplicationOperatorDecisionRecord.__table__,
            "after_drop",
            DDL(_statement).execute_if(dialect=_dialect),
        )


class OperatorDecisionImmutableError(RuntimeError):
    """An UPDATE or DELETE of an ApplicationOperatorDecisionRecord was attempted."""


@event.listens_for(ApplicationOperatorDecisionRecord, "before_update")
def _refuse_decision_update(mapper, connection, target) -> None:
    raise OperatorDecisionImmutableError("operator decisions are append-only")


@event.listens_for(ApplicationOperatorDecisionRecord, "before_delete")
def _refuse_decision_delete(mapper, connection, target) -> None:
    raise OperatorDecisionImmutableError("operator decisions are append-only")
