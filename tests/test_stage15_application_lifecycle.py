"""Stage 15: application lifecycle state machine, tracker, schema, migration.

Everything runs against throwaway SQLite databases; nothing here touches the
network, a mailbox or the canonical Stage 14B database.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.db import application_repository as repo
from app.db.base import Base
from app.db.models import (
    ApplicationEventImmutableError,
    ApplicationEventRecord,
    ApplicationRecord,
    JobRecord,
)
from app.domain.application_lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidApplicationTransitionError,
    is_transition_allowed,
)
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationEventType as T
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_tracker import (
    FOLLOWUP_INTERVAL,
    ApplicationConcurrencyError,
    ApplicationInputError,
    ApplicationTracker,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUBMITTED_AT = datetime(2026, 10, 17, 14, 2, 50, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage15.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _create(
    db,
    *,
    status=S.SUBMITTED,
    ref="20385-2347902-S",
    company="ALD Vacuum Technologies GmbH",
    role="Backend Developer (m/w/div.)",
    key="portal-submission:ald",
    **extra,
):
    return ApplicationTracker().create_application(
        db,
        company_name=company,
        role_title=role,
        source="bundesagentur",
        source_reference=ref,
        application_channel=C.EMPLOYER_PORTAL,
        initial_status=status,
        origin=O.IMPORT,
        actor="test",
        occurred_at=SUBMITTED_AT,
        employer_domains=("ald-vt.de",),
        correlation_key=key,
        **extra,
    )


# --- A. enum validation -------------------------------------------------------------


def test_status_vocabulary_is_exactly_the_stage15_lifecycle():
    assert [s.value for s in S] == [
        "DISCOVERED",
        "MATCHED",
        "PACKAGE_READY",
        "APPROVED",
        "SUBMITTED",
        "RECEIVED",
        "UNDER_REVIEW",
        "INTERVIEW",
        "TECHNICAL_INTERVIEW",
        "OFFER",
        "REJECTED",
        "WITHDRAWN",
    ]
    with pytest.raises(ValueError):
        S("APPLIED")


def test_database_rejects_an_unknown_status(db):
    app_id = _create(db).application.id
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.execute(
            text("UPDATE applications SET current_status = 'APPLIED' WHERE id = :i"), {"i": app_id}
        )
        db.commit()
    db.rollback()


def test_database_rejects_malformed_events(db):
    app_id = _create(db).application.id
    bad = [
        dict(event_type="STATUS_CHANGED", old_status=None, new_status="RECEIVED"),
        dict(event_type="INBOUND_MESSAGE", old_status=None, new_status="RECEIVED"),
        dict(event_type="STATUS_OVERRIDDEN", old_status="SUBMITTED", new_status="REJECTED"),
        dict(event_type="NOPE", old_status=None, new_status=None),
    ]
    for index, values in enumerate(bad, start=10):
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            repo.insert_event(
                db, application_id=app_id, sequence=index, origin="SYSTEM", actor="t", **values
            )
        db.rollback()


# --- B/C/D. transition policy -------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.DISCOVERED, S.MATCHED),
        (S.MATCHED, S.PACKAGE_READY),
        (S.PACKAGE_READY, S.APPROVED),
        (S.APPROVED, S.SUBMITTED),
        (S.SUBMITTED, S.RECEIVED),
        (S.RECEIVED, S.UNDER_REVIEW),
        (S.RECEIVED, S.INTERVIEW),
        (S.UNDER_REVIEW, S.INTERVIEW),
        (S.INTERVIEW, S.TECHNICAL_INTERVIEW),
        (S.INTERVIEW, S.OFFER),
        (S.TECHNICAL_INTERVIEW, S.OFFER),
        (S.SUBMITTED, S.INTERVIEW),
        (S.DISCOVERED, S.REJECTED),
        (S.TECHNICAL_INTERVIEW, S.REJECTED),
        (S.MATCHED, S.WITHDRAWN),
        (S.INTERVIEW, S.WITHDRAWN),
    ],
)
def test_valid_transitions(current, target):
    assert is_transition_allowed(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.REJECTED, S.SUBMITTED),
        (S.OFFER, S.DISCOVERED),
        (S.RECEIVED, S.SUBMITTED),
        (S.INTERVIEW, S.RECEIVED),
        (S.DISCOVERED, S.SUBMITTED),
        (S.MATCHED, S.APPROVED),
        (S.SUBMITTED, S.SUBMITTED),
        (S.WITHDRAWN, S.REJECTED),
        (S.OFFER, S.REJECTED),
        (S.PACKAGE_READY, S.RECEIVED),
    ],
)
def test_invalid_transitions(current, target):
    assert not is_transition_allowed(current, target)


def test_terminal_statuses_have_no_exit():
    assert TERMINAL_STATUSES == {S.OFFER, S.REJECTED, S.WITHDRAWN}
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_tracker_refuses_invalid_and_terminal_transitions(db):
    tracker = ApplicationTracker()
    app_id = _create(db).application.id
    with pytest.raises(InvalidApplicationTransitionError):
        tracker.transition_application(db, app_id, S.DISCOVERED, origin=O.OPERATOR, actor="op")
    tracker.transition_application(db, app_id, S.REJECTED, origin=O.OPERATOR, actor="op")
    with pytest.raises(InvalidApplicationTransitionError):
        tracker.transition_application(db, app_id, S.INTERVIEW, origin=O.OPERATOR, actor="op")
    assert tracker.get_application(db, app_id).current_status == S.REJECTED
    assert len(tracker.get_application_history(db, app_id)) == 2


def test_full_preparation_path_is_recorded_in_order(db):
    tracker = ApplicationTracker()
    app_id = _create(db, status=S.DISCOVERED, key=None).application.id
    for target in (S.MATCHED, S.PACKAGE_READY, S.APPROVED, S.SUBMITTED):
        tracker.transition_application(
            db, app_id, target, origin=O.SYSTEM, actor="t", occurred_at=SUBMITTED_AT
        )
    app = tracker.get_application(db, app_id)
    assert app.current_status == S.SUBMITTED and app.submitted_at == SUBMITTED_AT
    history = tracker.get_application_history(db, app_id)
    assert [e.sequence for e in history] == [1, 2, 3, 4, 5]
    assert [(e.old_status, e.new_status) for e in history[1:]] == [
        (S.DISCOVERED, S.MATCHED),
        (S.MATCHED, S.PACKAGE_READY),
        (S.PACKAGE_READY, S.APPROVED),
        (S.APPROVED, S.SUBMITTED),
    ]


# --- E. manual override audit -------------------------------------------------------


def test_override_is_audited_and_never_rewrites_history(db):
    tracker = ApplicationTracker()
    app_id = _create(db).application.id
    tracker.transition_application(db, app_id, S.REJECTED, origin=O.GMAIL, actor="proc")
    with pytest.raises(ApplicationInputError):
        tracker.override_status(db, app_id, S.INTERVIEW, actor="operator", reason="  ")
    with pytest.raises(ApplicationInputError):
        tracker.override_status(db, app_id, S.INTERVIEW, actor="", reason="misread")
    outcome = tracker.override_status(
        db, app_id, S.INTERVIEW, actor="operator", reason="Rejection mail was for another role"
    )
    assert outcome.event.event_type == T.STATUS_OVERRIDDEN
    assert (outcome.event.old_status, outcome.event.new_status) == (S.REJECTED, S.INTERVIEW)
    assert outcome.event.origin == O.OPERATOR and outcome.event.actor == "operator"
    history = tracker.get_application_history(db, app_id)
    assert [e.event_type for e in history] == [T.CREATED, T.STATUS_CHANGED, T.STATUS_OVERRIDDEN]
    assert history[1].new_status == S.REJECTED  # the original rejection is still there


def test_override_to_same_status_is_refused(db):
    app_id = _create(db).application.id
    with pytest.raises(ApplicationInputError):
        ApplicationTracker().override_status(db, app_id, S.SUBMITTED, actor="op", reason="x")


# --- F. append-only events ----------------------------------------------------------


def test_events_cannot_be_updated_or_deleted_through_the_orm(db):
    _create(db)
    event = db.scalars(select(ApplicationEventRecord)).one()
    event.reason = "rewritten"
    with pytest.raises(ApplicationEventImmutableError):
        db.flush()
    db.rollback()
    event = db.scalars(select(ApplicationEventRecord)).one()
    db.delete(event)
    with pytest.raises(ApplicationEventImmutableError):
        db.flush()
    db.rollback()
    assert db.scalar(select(func.count()).select_from(ApplicationEventRecord)) == 1
    assert db.scalars(select(ApplicationEventRecord)).one().reason is None


def test_repository_exposes_no_event_mutation():
    names = {n for n in dir(repo) if not n.startswith("_")}
    assert not {n for n in names if ("update" in n or "delete" in n) and "event" in n}


def test_event_sequence_is_unique_per_application(db):
    app_id = _create(db).application.id
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        repo.insert_event(
            db,
            application_id=app_id,
            sequence=1,
            event_type="INBOUND_MESSAGE",
            origin="GMAIL",
            actor="t",
        )
    db.rollback()


# --- G. transaction rollback --------------------------------------------------------


def test_failure_after_event_insert_rolls_back_everything(db, monkeypatch):
    tracker = ApplicationTracker()
    app_id = _create(db).application.id

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash between event and status update")

    monkeypatch.setattr(repo, "cas_update_application", boom)
    with pytest.raises(RuntimeError):
        tracker.transition_application(
            db, app_id, S.RECEIVED, origin=O.GMAIL, actor="p", correlation_key="gmail:x"
        )
    monkeypatch.undo()
    assert tracker.get_application(db, app_id).current_status == S.SUBMITTED
    assert len(tracker.get_application_history(db, app_id)) == 1
    assert not tracker.event_exists(db, "gmail:x")


def test_lost_cas_rolls_back_and_raises(db, monkeypatch):
    tracker = ApplicationTracker()
    app_id = _create(db).application.id
    monkeypatch.setattr(repo, "cas_update_application", lambda *a, **k: False)
    with pytest.raises(ApplicationConcurrencyError):
        tracker.transition_application(db, app_id, S.RECEIVED, origin=O.GMAIL, actor="p")
    monkeypatch.undo()
    assert len(tracker.get_application_history(db, app_id)) == 1


def test_stale_version_cannot_be_written(db):
    app_id = _create(db).application.id
    assert (
        repo.cas_update_application(
            db, app_id, expected_status="SUBMITTED", expected_version=99, values={}
        )
        is False
    )
    assert (
        repo.cas_update_application(
            db, app_id, expected_status="RECEIVED", expected_version=1, values={}
        )
        is False
    )


# --- H. duplicate submission evidence / idempotent creation -------------------------


def test_creating_the_same_application_twice_writes_once(db):
    first = _create(db)
    second = _create(db)
    assert first.event_recorded and not first.duplicate
    assert second.duplicate and not second.event_recorded
    assert second.application.id == first.application.id
    assert db.scalar(select(func.count()).select_from(ApplicationRecord)) == 1
    assert db.scalar(select(func.count()).select_from(ApplicationEventRecord)) == 1


def test_replaying_the_same_evidence_key_is_a_no_op(db):
    tracker = ApplicationTracker()
    app_id = _create(db).application.id
    a = tracker.record_receipt(
        db,
        app_id,
        origin=O.GMAIL,
        actor="p",
        correlation_key="gmail:k",
        occurred_at=SUBMITTED_AT + timedelta(minutes=3),
    )
    b = tracker.record_receipt(
        db,
        app_id,
        origin=O.GMAIL,
        actor="p",
        correlation_key="gmail:k",
        occurred_at=SUBMITTED_AT + timedelta(minutes=3),
    )
    assert a.transition_applied and not a.duplicate
    assert b.duplicate and not b.transition_applied and not b.event_recorded
    assert len(tracker.get_application_history(db, app_id)) == 2


def test_create_with_a_correlation_key_already_used_returns_the_owner(db):
    first = _create(db)
    other = _create(db, ref="OTHER-REF", key="portal-submission:ald")
    assert other.duplicate and other.application.id == first.application.id


# --- timestamps / follow-up metadata -------------------------------------------------


def test_receipt_timestamps_and_followup_are_informational(db):
    tracker = ApplicationTracker()
    created = _create(db).application
    assert created.submitted_at == SUBMITTED_AT
    assert created.next_followup_at == SUBMITTED_AT + FOLLOWUP_INTERVAL
    receipt_at = SUBMITTED_AT + timedelta(minutes=3)
    app = tracker.record_receipt(
        db, created.id, origin=O.GMAIL, actor="p", occurred_at=receipt_at, correlation_key="gmail:r"
    ).application
    assert app.received_at == receipt_at
    assert app.last_employer_contact_at == receipt_at
    assert app.next_followup_at == receipt_at + FOLLOWUP_INTERVAL
    assert app.followup_required is False
    app = tracker.transition_application(
        db, created.id, S.INTERVIEW, origin=O.GMAIL, actor="p", occurred_at=receipt_at
    ).application
    assert app.next_followup_at is None


def test_receipt_dated_before_submission_is_clamped_to_submission(db):
    tracker = ApplicationTracker()
    app_id = _create(db).application.id
    early = SUBMITTED_AT - timedelta(minutes=5)
    app = tracker.record_receipt(
        db, app_id, origin=O.GMAIL, actor="p", occurred_at=early, correlation_key="gmail:early"
    ).application
    assert app.received_at == SUBMITTED_AT


def test_inbound_message_event_keeps_status_and_sets_followup_flag(db):
    tracker = ApplicationTracker()
    app_id = _create(db).application.id
    outcome = tracker.record_inbound_message(
        db,
        app_id,
        correlation_key="gmail:rfi",
        occurred_at=SUBMITTED_AT + timedelta(days=1),
        reason="NO_STATUS_FOR_REQUEST_FOR_INFORMATION",
        followup_required=True,
    )
    assert outcome.event.event_type == T.INBOUND_MESSAGE and outcome.event.new_status is None
    assert outcome.application.current_status == S.SUBMITTED
    assert outcome.application.followup_required is True


def test_stage15_never_touches_job_status(db):
    db.add(
        JobRecord(
            fingerprint="f" * 64,
            source="bundesagentur",
            title="t",
            company="c",
            url="https://example.invalid",
            score=1,
            recommendation="MAYBE",
        )
    )
    db.commit()
    tracker = ApplicationTracker()
    app_id = _create(db, job_id=1).application.id
    tracker.record_receipt(db, app_id, origin=O.GMAIL, actor="p", correlation_key="gmail:j")
    assert db.scalars(select(JobRecord.status)).one() == "NEW"


def test_metadata_is_stored_as_json(db):
    app_id = _create(db, metadata={"note": "ü"}).application.id
    event = repo.list_events(db, app_id)[0]
    assert json.loads(event.metadata_json) == {"note": "ü"}


# --- T. SQLite migration ------------------------------------------------------------


def _alembic(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_sqlite_migration_creates_tables_constraints_and_indexes(tmp_path):
    db_path = tmp_path / "migrated.db"
    cfg = _alembic(db_path)
    upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert {"applications", "application_events"} <= set(inspector.get_table_names())
    model_columns = {c.name for c in ApplicationRecord.__table__.columns}
    assert {c["name"] for c in inspector.get_columns("applications")} == model_columns
    event_columns = {c.name for c in ApplicationEventRecord.__table__.columns}
    assert {c["name"] for c in inspector.get_columns("application_events")} == event_columns
    indexes = {i["name"] for i in inspector.get_indexes("applications")}
    assert {
        "ix_applications_current_status",
        "ix_applications_company_name",
        "ix_applications_external_job_id",
        "ix_applications_created_at",
        "ix_applications_last_status_changed_at",
    } <= indexes
    uniques = {u["name"] for u in inspector.get_unique_constraints("application_events")}
    assert {"uq_application_events_correlation_key", "uq_application_events_sequence"} <= uniques
    checks = {c["name"] for c in inspector.get_check_constraints("applications")}
    assert "ck_applications_current_status_valid" in checks

    session = sessionmaker(engine)()
    outcome = _create(session)
    assert _create(session).duplicate
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        repo.insert_event(
            session,
            application_id=outcome.application.id,
            sequence=2,
            event_type="INBOUND_MESSAGE",
            origin="GMAIL",
            actor="t",
            correlation_key="portal-submission:ald",
        )
    session.rollback()
    session.close()
    engine.dispose()

    downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_path}")
    assert not {"applications", "application_events"} & set(inspect(engine).get_table_names())
    engine.dispose()
    upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    assert {"applications", "application_events"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_model_checks_match_the_frozen_migration_vocabulary():
    import importlib.util

    path = PROJECT_ROOT / "alembic/versions/b5d2e8f4a1c7_add_application_lifecycle_tables.py"
    spec = importlib.util.spec_from_file_location("stage15_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._STATUSES == tuple(s.value for s in S)
    assert module._CHANNELS == tuple(c.value for c in C)
    assert module._EVENT_TYPES == tuple(t.value for t in T)
    assert module._ORIGINS == tuple(o.value for o in O)
