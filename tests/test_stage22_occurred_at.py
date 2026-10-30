"""Stage 22 P2: a manual status change can carry its real time (--occurred-at)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.db.models import ApplicationEventRecord, ApplicationRecord, Base
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_tracker import ApplicationTracker
from app.services.readiness import expected_alembic_heads
from scripts import stage15_application_tracker as cli

SUBMITTED = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "occurred.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    session = sessionmaker(engine)()
    ApplicationTracker().create_application(
        session,
        company_name="ACME GmbH",
        role_title="Junior Python Developer",
        source="manual",
        source_reference="ACME-1",
        application_channel=C.EMAIL,
        initial_status=S.SUBMITTED,
        origin=O.OPERATOR,
        actor="test",
        occurred_at=SUBMITTED,
    )
    session.commit()
    session.close()
    engine.dispose()
    return path


def _args(path, *extra):
    return [
        "--database",
        str(path),
        "transition",
        "1",
        "RECEIVED",
        "--actor",
        "op",
        "--reason",
        "receipt mail",
        *extra,
    ]


def _state(path):
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with sessionmaker(engine)() as session:
        app = session.scalar(select(ApplicationRecord))
        events = session.scalars(
            select(ApplicationEventRecord).order_by(ApplicationEventRecord.id)
        ).all()
        state = (app.current_status, app.received_at, [e.occurred_at for e in events])
    engine.dispose()
    return state


def test_the_receipt_keeps_its_real_time(db_path, capsys):
    when = "2026-10-29T14:05:00+01:00"
    assert cli.run(_args(db_path, "--occurred-at", when)) == 0
    assert "at 2026-10-29T13:05:00+00:00 (nothing written)" in capsys.readouterr().out
    assert cli.run(_args(db_path, "--occurred-at", when, "--apply")) == 0
    status, received_at, times = _state(db_path)
    assert status == "RECEIVED"
    expected = datetime(2026, 10, 29, 13, 5, tzinfo=UTC)
    assert received_at.replace(tzinfo=UTC) == expected
    assert times[-1].replace(tzinfo=UTC) == expected


def test_without_the_flag_now_is_used(db_path):
    before = datetime.now(UTC) - timedelta(seconds=5)
    assert cli.run(_args(db_path, "--apply")) == 0
    _status, _received, times = _state(db_path)
    assert times[-1].replace(tzinfo=UTC) >= before


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("2026-10-29T14:05:00", "explicit UTC offset"),
        ("yesterday", "ISO 8601"),
        ((datetime.now(UTC) + timedelta(days=1)).isoformat(), "future"),
        ("2026-10-27T09:00:00+00:00", "before the current status"),  # before SUBMITTED
    ],
)
def test_bad_times_are_refused_and_nothing_is_written(db_path, capsys, value, message):
    assert cli.run(_args(db_path, "--occurred-at", value, "--apply")) == 2
    assert message in capsys.readouterr().err
    status, _received, times = _state(db_path)
    assert status == "SUBMITTED" and len(times) == 1


def test_override_accepts_the_time_too(db_path):
    args = [
        "--database",
        str(db_path),
        "override",
        "1",
        "REJECTED",
        "--actor",
        "op",
        "--reason",
        "rejection mail",
        "--occurred-at",
        "2026-10-30T08:00:00Z",
        "--apply",
    ]
    assert cli.run(args) == 0
    status, _received, times = _state(db_path)
    assert status == "REJECTED"
    assert times[-1].replace(tzinfo=UTC) == datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize("command", ["transition", "override"])
def test_a_concurrent_change_cannot_make_history_run_backwards(db_path, monkeypatch, command):
    """Codex P2 round 1: the CLI checks a snapshot; another writer moves the
    status (dated 29.10.) before the write -- the tracker must refuse 28.10."""
    tracker_cls = cli.ApplicationTracker
    real_get = tracker_cls.get_application

    def get_then_concurrent_change(self, session, application_id):
        snapshot = real_get(self, session, application_id)
        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        with sessionmaker(engine)() as other:
            ApplicationTracker().transition_application(
                other,
                application_id,
                S.UNDER_REVIEW,
                origin=O.OPERATOR,
                actor="other",
                occurred_at=datetime(2026, 10, 29, 12, 0, tzinfo=UTC),
            )
            other.commit()
        engine.dispose()
        return snapshot

    monkeypatch.setattr(tracker_cls, "get_application", get_then_concurrent_change)
    target = "REJECTED"
    args = [
        "--database",
        str(db_path),
        command,
        "1",
        target,
        "--actor",
        "op",
        "--reason",
        "r",
        "--occurred-at",
        "2026-10-28T12:00:00Z",
        "--apply",
    ]
    assert cli.run(args) == 1
    status, _received, times = _state(db_path)
    assert status == "UNDER_REVIEW"
    assert [t.replace(tzinfo=UTC) for t in times] == sorted(t.replace(tzinfo=UTC) for t in times)
