"""OUTBOUND_MESSAGE application events: service mail the candidate already sent.

Everything runs against throwaway SQLite databases. Nothing here sends mail,
touches the network or a mailbox.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import ApplicationEventRecord, ApplicationRecord
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationEventType as T
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.application_lifecycle import OutboundMessageKind as K
from app.models.telegram_operator import TelegramDeliveryState
from app.services.application_tracker import (
    ApplicationInputError,
    ApplicationTracker,
    outbound_correlation_key,
)
from app.services.telegram_operator_notifications import build_notification_view
from scripts import stage15_application_tracker as cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUBMITTED_AT = datetime(2026, 10, 17, 14, 2, 50, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 10, 17, 14, 6, 17, tzinfo=UTC)
SENT_AT = datetime(2026, 10, 23, 11, 26, 36, tzinfo=UTC)
MESSAGE_ID = "<179275479657.15788.8600720621948195661@gmail.com>"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'outbound.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _create(db, *, ref="20385-2347902-S", key="portal-submission:ald", role="Backend Developer"):
    return (
        ApplicationTracker()
        .create_application(
            db,
            company_name="ALD Vacuum Technologies GmbH",
            role_title=role,
            source="bundesagentur",
            source_reference=ref,
            application_channel=C.EMPLOYER_PORTAL,
            initial_status=S.SUBMITTED,
            origin=O.IMPORT,
            actor="test",
            occurred_at=SUBMITTED_AT,
            employer_domains=("ald-vt.de",),
            correlation_key=key,
        )
        .application
    )


def _received(db, app_id):
    return (
        ApplicationTracker()
        .record_receipt(
            db,
            app_id,
            origin=O.GMAIL,
            actor="test",
            occurred_at=RECEIVED_AT,
            correlation_key=f"gmail:receipt-{app_id}",
        )
        .application
    )


def _outbound(db, app_id, *, message_id=MESSAGE_ID, related=(), **overrides):
    values = dict(
        message_id=message_id,
        recipient="karriere@ald-vt.de",
        channel=C.EMAIL,
        sent_at=SENT_AT,
        kind=K.CONTACT_CORRECTION,
        actor="operator",
        reason="Telefonnummer korrigiert",
        sender="vitalii.malcov.dev@gmail.com",
        subject="Korrektur meiner Telefonnummer",
        evidence_summary="Gesendet-Ordner verifiziert",
        related_application_ids=related,
        now=NOW,
    )
    values.update(overrides)
    return ApplicationTracker().record_outbound_message(db, app_id, **values)


def _event_count(db):
    return db.scalar(select(func.count()).select_from(ApplicationEventRecord))


# --- vocabulary ---------------------------------------------------------------------


def test_outbound_message_extends_the_event_vocabulary_without_renaming():
    assert [t.value for t in T] == [
        "CREATED",
        "STATUS_CHANGED",
        "STATUS_OVERRIDDEN",
        "INBOUND_MESSAGE",
        "OUTBOUND_MESSAGE",
    ]
    assert [k.value for k in K] == ["CONTACT_CORRECTION", "OTHER"]


# --- tracker ------------------------------------------------------------------------


def test_outbound_message_never_changes_status_followup_or_employer_contact(db):
    app = _received(db, _create(db).id)
    before = db.get(ApplicationRecord, app.id)
    snapshot = (
        before.current_status,
        before.next_followup_at,
        before.last_employer_contact_at,
        before.received_at,
        before.submitted_at,
        before.followup_required,
    )
    outcome = _outbound(db, app.id)
    assert outcome.event_recorded and not outcome.duplicate and not outcome.transition_applied
    after = db.get(ApplicationRecord, app.id)
    db.refresh(after)
    assert (
        after.current_status,
        after.next_followup_at,
        after.last_employer_contact_at,
        after.received_at,
        after.submitted_at,
        after.followup_required,
    ) == snapshot
    event = outcome.event
    assert event.event_type == T.OUTBOUND_MESSAGE
    assert event.old_status is None and event.new_status is None
    assert event.origin == O.OPERATOR
    assert event.occurred_at == SENT_AT
    assert event.correlation_key == outbound_correlation_key(MESSAGE_ID, app.id)
    assert event.metadata == {
        "direction": "OUTBOUND",
        "kind": "CONTACT_CORRECTION",
        "message_id": MESSAGE_ID,
        "recipient": "karriere@ald-vt.de",
        "channel": "EMAIL",
        "sender": "vitalii.malcov.dev@gmail.com",
        "subject": "Korrektur meiner Telefonnummer",
    }


def test_same_message_for_same_application_is_recorded_once(db):
    app = _create(db)
    _outbound(db, app.id)
    count = _event_count(db)
    replay = _outbound(db, app.id, reason="anderer Grund")
    assert replay.duplicate and not replay.event_recorded
    assert replay.event.metadata["message_id"] == MESSAGE_ID
    assert _event_count(db) == count


def test_one_shared_mail_is_recorded_on_two_applications(db):
    first = _create(db)
    second = _create(db, ref="20385-2352935-S", key="portal-submission:ald2", role="SW Engineer")
    a = _outbound(db, first.id, related=(first.id, second.id))
    b = _outbound(db, second.id, related=(first.id, second.id))
    assert a.event_recorded and b.event_recorded
    assert a.event.correlation_key != b.event.correlation_key
    assert a.event.metadata["related_application_ids"] == [second.id]
    assert b.event.metadata["related_application_ids"] == [first.id]


def test_outbound_events_follow_the_existing_history(db):
    app = _received(db, _create(db).id)
    _outbound(db, app.id)
    history = ApplicationTracker().get_application_history(db, app.id)
    assert [e.event_type for e in history] == [T.CREATED, T.STATUS_CHANGED, T.OUTBOUND_MESSAGE]
    assert [e.sequence for e in history] == [1, 2, 3]


@pytest.mark.parametrize(
    "overrides",
    [
        {"message_id": "no-brackets@gmail.com"},
        {"message_id": "<missing-at>"},
        {"message_id": "  "},
        {"recipient": "not-an-address"},
        {"recipient": "Name <a@b.de>"},
        {"sender": "kaputt"},
        {"sent_at": datetime(2026, 10, 23, 11, 0)},
        {"sent_at": NOW + timedelta(minutes=1)},
        {"reason": " "},
        {"actor": ""},
    ],
)
def test_invalid_outbound_input_writes_nothing(db, overrides):
    app = _create(db)
    count = _event_count(db)
    with pytest.raises(ApplicationInputError):
        _outbound(db, app.id, **overrides)
    assert _event_count(db) == count


def test_database_refuses_an_outbound_event_that_names_a_status(db):
    app = _create(db)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.execute(
            text(
                "INSERT INTO application_events (application_id, sequence, event_type, "
                "old_status, new_status, origin, actor, recorded_at, metadata_json) VALUES "
                "(:a, 2, 'OUTBOUND_MESSAGE', NULL, 'RECEIVED', 'OPERATOR', 't', :r, '{}')"
            ),
            {"a": app.id, "r": NOW.isoformat()},
        )
        db.commit()
    db.rollback()


def test_outbound_events_are_never_telegram_notifications(db):
    app = _create(db)
    outcome = _outbound(db, app.id)
    record = db.get(ApplicationEventRecord, outcome.event.id)
    assert build_notification_view(record, db.get(ApplicationRecord, app.id)) is None


# --- CLI ------------------------------------------------------------------------------


def _alembic(path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return cfg


@pytest.fixture()
def cli_db(tmp_path):
    path = tmp_path / "cli_outbound.db"
    upgrade(_alembic(path), "head")
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    first = _create(session)
    second = _create(session, ref="20385-2352935-S", key="portal-submission:ald2", role="SW")
    ids = (first.id, second.id)
    session.close()
    engine.dispose()
    return path, ids


def _cli_args(path, ids, *extra):
    args = ["--database", str(path), "record-outbound"]
    for app_id in ids:
        args += ["--application-id", str(app_id)]
    return [
        *args,
        "--message-id",
        MESSAGE_ID,
        "--recipient",
        "karriere@ald-vt.de",
        "--channel",
        "EMAIL",
        "--sent-at",
        "2026-10-23T11:26:36+00:00",
        "--kind",
        "CONTACT_CORRECTION",
        "--actor",
        "operator",
        "--reason",
        "Telefonnummer korrigiert",
        *extra,
    ]


def _outbound_rows(path):
    with sqlite3.connect(path) as con:
        return con.execute(
            "SELECT application_id, metadata_json FROM application_events "
            "WHERE event_type = 'OUTBOUND_MESSAGE' ORDER BY application_id"
        ).fetchall()


def test_cli_record_outbound_dry_run_writes_nothing(cli_db, capsys):
    path, ids = cli_db
    assert cli.run(_cli_args(path, ids)) == 0
    out = capsys.readouterr().out
    assert out.count("DRY RUN: would record OUTBOUND_MESSAGE") == 2
    assert "SUBMITTED unchanged" in out
    assert _outbound_rows(path) == []


def test_cli_record_outbound_apply_then_replay_is_idempotent(cli_db, capsys):
    path, ids = cli_db
    assert cli.run(_cli_args(path, ids, "--apply")) == 0
    assert capsys.readouterr().out.count("APPLIED:") == 2
    rows = _outbound_rows(path)
    assert [r[0] for r in rows] == list(ids)
    assert all(json.loads(r[1])["message_id"] == MESSAGE_ID for r in rows)
    assert cli.run(_cli_args(path, ids, "--apply")) == 0
    assert capsys.readouterr().out.count("ALREADY_RECORDED") == 2
    assert len(_outbound_rows(path)) == 2
    with sqlite3.connect(path) as con:
        statuses = {r[0] for r in con.execute("SELECT current_status FROM applications")}
    assert statuses == {"SUBMITTED"}
    assert cli.run(["--database", str(path), "--json", "history", str(ids[0])]) == 0
    history = json.loads(capsys.readouterr().out)
    assert history[-1]["type"] == "OUTBOUND_MESSAGE"
    assert history[-1]["recipient"] == "karriere@ald-vt.de"
    assert history[-1]["message_id"] == MESSAGE_ID
    assert "recipient" not in history[0]


def test_cli_record_outbound_refuses_bad_input_before_any_write(cli_db, capsys):
    path, ids = cli_db
    naive = _cli_args(path, ids, "--apply")
    naive[naive.index("2026-10-23T11:26:36+00:00")] = "2026-10-23T11:26:36"
    assert cli.run(naive) == 2
    bad_id = _cli_args(path, ids, "--apply")
    bad_id[bad_id.index(MESSAGE_ID)] = "kein-message-id"
    assert cli.run(bad_id) == 1
    unknown = _cli_args(path, (*ids, 999), "--apply")
    assert cli.run(unknown) == 1
    assert _outbound_rows(path) == []


# --- migration ------------------------------------------------------------------------


def _seed_pre_migration(path: Path) -> None:
    """Revision e3a9c5d7f2b4 with history rows and a lifecycle notification
    that references an event (FK target of the SQLite table rebuild)."""
    upgrade(_alembic(path), "e3a9c5d7f2b4")
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO applications (id, company_name, role_title, source, "
            "source_reference, application_channel, employer_domains_json, current_status, "
            "submitted_at, last_status_changed_at, followup_required, version, created_at, "
            "updated_at) VALUES (1, 'ALD', 'Backend', 'bundesagentur', 'R1', 'EMPLOYER_PORTAL', "
            "'[]', 'RECEIVED', '2026-10-17 14:02:50', '2026-10-17 14:06:17', 0, 2, "
            "'2026-10-17 14:02:50', '2026-10-17 14:06:17')"
        )
        for seq, (etype, old, new, key) in enumerate(
            [
                ("CREATED", None, "SUBMITTED", "portal-submission:r1"),
                ("STATUS_CHANGED", "SUBMITTED", "RECEIVED", "gmail:r1"),
            ],
            start=1,
        ):
            con.execute(
                "INSERT INTO application_events (id, application_id, sequence, event_type, "
                "old_status, new_status, origin, actor, recorded_at, correlation_key, "
                "metadata_json) VALUES (?, 1, ?, ?, ?, ?, 'IMPORT', 't', '2026-10-17', ?, '{}')",
                (seq, seq, etype, old, new, key),
            )
        con.execute(
            "INSERT INTO telegram_lifecycle_notifications (application_event_id, "
            "application_id, delivery_state, attempt_count, created_at, updated_at) "
            "VALUES (2, 1, ?, 0, '2026-10-17', '2026-10-17')",
            (list(TelegramDeliveryState)[0].value,),
        )


def _triggers(path: Path) -> set[str]:
    with sqlite3.connect(path) as con:
        return {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'application_events'"
            )
        }


def test_migration_keeps_rows_indexes_fks_and_append_only_triggers(tmp_path):
    path = tmp_path / "migrate.db"
    _seed_pre_migration(path)
    upgrade(_alembic(path), "f4b8d2a6c9e1")
    with sqlite3.connect(path) as con:
        rows = con.execute(
            "SELECT id, sequence, event_type, correlation_key FROM application_events ORDER BY id"
        ).fetchall()
        assert rows == [
            (1, 1, "CREATED", "portal-submission:r1"),
            (2, 2, "STATUS_CHANGED", "gmail:r1"),
        ]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        indexes = {r[1] for r in con.execute("PRAGMA index_list('application_events')").fetchall()}
        assert "ix_application_events_application_occurred" in indexes
        con.execute(
            "INSERT INTO application_events (application_id, sequence, event_type, origin, "
            "actor, recorded_at, correlation_key, metadata_json) VALUES "
            "(1, 3, 'OUTBOUND_MESSAGE', 'OPERATOR', 't', '2026-10-23', 'outbound:x', '{}')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO application_events (application_id, sequence, event_type, "
                "new_status, origin, actor, recorded_at, metadata_json) VALUES "
                "(1, 4, 'OUTBOUND_MESSAGE', 'RECEIVED', 'OPERATOR', 't', '2026-10-23', '{}')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE application_events SET actor = 'x' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM application_events WHERE id = 1")
    assert _triggers(path) == {
        "trg_application_events_no_update",
        "trg_application_events_no_delete",
    }


def test_downgrade_refuses_while_outbound_events_exist(tmp_path):
    path = tmp_path / "downgrade_refused.db"
    _seed_pre_migration(path)
    upgrade(_alembic(path), "f4b8d2a6c9e1")
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO application_events (application_id, sequence, event_type, origin, "
            "actor, recorded_at, metadata_json) VALUES "
            "(1, 3, 'OUTBOUND_MESSAGE', 'OPERATOR', 't', '2026-10-23', '{}')"
        )
    with pytest.raises(RuntimeError, match="cannot downgrade"):
        downgrade(_alembic(path), "e3a9c5d7f2b4")
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "f4b8d2a6c9e1",
        )


def test_downgrade_without_outbound_events_restores_the_old_constraint(tmp_path):
    path = tmp_path / "roundtrip.db"
    _seed_pre_migration(path)
    upgrade(_alembic(path), "f4b8d2a6c9e1")
    downgrade(_alembic(path), "e3a9c5d7f2b4")
    assert _triggers(path) == {
        "trg_application_events_no_update",
        "trg_application_events_no_delete",
    }
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM application_events").fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO application_events (application_id, sequence, event_type, origin, "
                "actor, recorded_at, metadata_json) VALUES "
                "(1, 3, 'OUTBOUND_MESSAGE', 'OPERATOR', 't', '2026-10-23', '{}')"
            )
    upgrade(_alembic(path), "head")


def test_migrated_schema_matches_the_model_constraints(tmp_path):
    migrated = tmp_path / "migrated.db"
    upgrade(_alembic(migrated), "head")
    fresh = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{fresh}")
    Base.metadata.create_all(engine)
    engine.dispose()

    def checks(path):
        engine = create_engine(f"sqlite:///{path}")
        from sqlalchemy import inspect

        found = {
            c["name"]: " ".join(c["sqltext"].split())
            for c in inspect(engine).get_check_constraints("application_events")
        }
        engine.dispose()
        return found

    migrated_checks, model_checks = checks(migrated), checks(fresh)
    for name in ("ck_application_events_type_valid", "ck_application_events_shape"):
        assert "OUTBOUND_MESSAGE" in migrated_checks[name]
        assert "OUTBOUND_MESSAGE" in model_checks[name]
    assert set(migrated_checks) == set(model_checks)
