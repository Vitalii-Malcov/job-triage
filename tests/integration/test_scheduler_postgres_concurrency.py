"""S8B-TEST-001 (Codex): a focused PostgreSQL integration test proving
`app.db.automation_schedule_repository.claim_due_schedule`'s CAS UPDATE
holds "at most one winner" under genuine concurrency against a REAL
PostgreSQL server -- not just SQLite (see
tests/test_automation_schedule_repository.py's
`TestRealConcurrentDueSlotClaim`/`TestSQLiteWALRealConcurrentClaim` for
the SQLite proofs this mirrors).

**Local execution:** skipped automatically unless `TEST_POSTGRES_URL` is
set (e.g. `postgresql+psycopg://user:password@localhost:5432/dbname`) --
this project's default dev/test setup is SQLite-only and does not assume
a local PostgreSQL server is available.

**CI:** `.github/workflows/ci.yml`'s dedicated `scheduler-postgres` job
starts a real `postgres:16` service container and always sets
`TEST_POSTGRES_URL`, so this module actually runs there on every push/PR
-- never silently skipped in CI.

This module never duplicates the CAS SQL itself -- it calls the REAL
`claim_due_schedule`/`get_or_create_schedule`, synchronized with a
`threading.Barrier` exactly like the SQLite tests, via the SAME
test-only `get_schedule` wrapper technique (never a change to production
code).
"""

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

import app.db.automation_schedule_repository as schedule_repo_module
from app.db.automation_schedule_repository import (
    claim_due_schedule,
    get_or_create_schedule,
    get_schedule,
)
from app.db.base import Base
from app.db.models import AutomationScheduleRecord

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job always sets it).",
)

ACCOUNT = "me@postgres-integration.example.com"


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _make_barrier_synced_get_schedule(original, barrier: threading.Barrier):
    """Identical technique to
    tests/test_automation_schedule_repository.py's own helper -- see
    that module's docstring for the full rationale. Duplicated here
    (rather than imported) so this integration module stays fully
    self-contained and skippable without importing anything that assumes
    a SQLite-only test layout.
    """
    waited_thread_ids: set[int] = set()
    lock = threading.Lock()

    def _wrapped(db, account_key):
        result = original(db, account_key)
        ident = threading.get_ident()
        with lock:
            first_call_from_this_thread = ident not in waited_thread_ids
            waited_thread_ids.add(ident)
        if first_call_from_this_thread:
            barrier.wait(timeout=15)
        return result

    return _wrapped


@pytest.fixture()
def pg_session_factory():
    engine = create_engine(TEST_POSTGRES_URL, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    with engine.begin() as conn:
        conn.execute(
            delete(AutomationScheduleRecord).where(AutomationScheduleRecord.account_key == ACCOUNT)
        )
    engine.dispose()


class TestPostgresReadCommittedConcurrentClaim:
    """Default PostgreSQL isolation level (READ COMMITTED) -- required
    "at minimum" by the Stage 8B hardening spec."""

    def test_at_most_one_winner_two_independent_connections(self, pg_session_factory):
        setup_db = pg_session_factory()
        past = datetime.now(UTC) - timedelta(hours=1)
        get_or_create_schedule(setup_db, ACCOUNT, now=past)
        setup_db.close()

        barrier = threading.Barrier(2)
        now = datetime.now(UTC)
        results: dict[str, tuple[str, object]] = {}

        def _worker(name, db):
            try:
                result = claim_due_schedule(db, ACCOUNT, interval_seconds=3600, now=now)
                results[name] = ("ok", result)
            except Exception as exc:
                db.rollback()
                results[name] = ("error", exc)

        # Two genuinely independent Sessions/connections -- each
        # sessionmaker() call against the same engine checks out its own
        # DBAPI connection from the pool.
        session_a = pg_session_factory()
        session_b = pg_session_factory()
        original_get_schedule = schedule_repo_module.get_schedule
        wrapped = _make_barrier_synced_get_schedule(original_get_schedule, barrier)
        schedule_repo_module.get_schedule = wrapped
        try:
            thread_a = threading.Thread(target=_worker, args=("a", session_a))
            thread_b = threading.Thread(target=_worker, args=("b", session_b))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=20)
            thread_b.join(timeout=20)
        finally:
            schedule_repo_module.get_schedule = original_get_schedule

        try:
            winners = [
                payload
                for status, payload in results.values()
                if status == "ok" and payload is not None
            ]
            # The actual invariant: at most one winner, no duplicate
            # AutomationRun trigger for this slot.
            assert len(winners) <= 1
            assert len(winners) == 1, "the due slot must be claimed by exactly one racer"

            for name, (status, _payload) in results.items():
                db = session_a if name == "a" else session_b
                if status == "error":
                    # Losing transaction behavior is clean -- rolled back
                    # inside _worker; the Session must be reusable
                    # afterward.
                    reloaded = get_schedule(db, ACCOUNT)
                    assert reloaded is not None

            verify_session = pg_session_factory()
            try:
                final = get_schedule(verify_session, ACCOUNT)
                expected_next = now + timedelta(seconds=3600)
                assert abs((_ensure_utc(final.next_run_at) - expected_next).total_seconds()) < 2
            finally:
                verify_session.close()
        finally:
            session_a.close()
            session_b.close()


class TestPostgresSerializableConcurrentClaim:
    """ "If practical" bonus per the hardening spec: SERIALIZABLE
    isolation is strictest -- a losing transaction here may raise a real
    serialization failure (psycopg wraps PostgreSQL's `40001` SQLSTATE as
    a SQLAlchemy OperationalError) instead of a clean CAS-loss None. Both
    outcomes are acceptable; a second successful claim is not. No retry
    machinery is added to production code for this -- the loser's
    Session is simply rolled back and proven reusable, exactly like the
    SQLite WAL contention test.
    """

    def test_never_two_winners_under_serializable_isolation(self, pg_session_factory):
        setup_db = pg_session_factory()
        past = datetime.now(UTC) - timedelta(hours=1)
        get_or_create_schedule(setup_db, ACCOUNT, now=past)
        setup_db.close()

        serializable_engine = create_engine(
            TEST_POSTGRES_URL, future=True, isolation_level="SERIALIZABLE"
        )
        serializable_factory = sessionmaker(
            bind=serializable_engine, autoflush=False, autocommit=False
        )

        barrier = threading.Barrier(2)
        now = datetime.now(UTC)
        results: dict[str, tuple[str, object]] = {}

        def _worker(name, db):
            try:
                result = claim_due_schedule(db, ACCOUNT, interval_seconds=3600, now=now)
                results[name] = ("ok", result)
            except Exception as exc:
                db.rollback()
                results[name] = ("error", exc)

        session_a = serializable_factory()
        session_b = serializable_factory()
        original_get_schedule = schedule_repo_module.get_schedule
        wrapped = _make_barrier_synced_get_schedule(original_get_schedule, barrier)
        schedule_repo_module.get_schedule = wrapped
        try:
            thread_a = threading.Thread(target=_worker, args=("a", session_a))
            thread_b = threading.Thread(target=_worker, args=("b", session_b))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=20)
            thread_b.join(timeout=20)
        finally:
            schedule_repo_module.get_schedule = original_get_schedule

        try:
            winners = [
                payload
                for status, payload in results.values()
                if status == "ok" and payload is not None
            ]
            # NEVER a second successful claim, regardless of isolation
            # level or whether the loser errored or cleanly lost the CAS.
            assert len(winners) <= 1

            for name, (status, payload) in results.items():
                db = session_a if name == "a" else session_b
                if status == "error":
                    assert isinstance(payload, Exception)
                    # Transaction is rolled back/reusable after the
                    # serialization/contention error.
                    reloaded = get_schedule(db, ACCOUNT)
                    assert reloaded is not None
        finally:
            session_a.close()
            session_b.close()
            serializable_engine.dispose()


def test_module_never_leaves_the_default_get_schedule_patched() -> None:
    """Sanity check -- both test classes above restore
    schedule_repo_module.get_schedule in a finally block; this guards
    against a future edit accidentally dropping that restoration and
    poisoning every OTHER test in the same session."""
    assert schedule_repo_module.get_schedule is get_schedule
