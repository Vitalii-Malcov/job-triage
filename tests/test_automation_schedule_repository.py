"""Stage 8B tests for `app.db.automation_schedule_repository` -- the
persisted schedule state + atomic CAS claim underlying the standalone
scheduler worker. Mirrors tests/test_automation_lease.py's real
two-Session/independent-engine-connection proof style for Stage 8A's
own lease, applied here to the schedule-slot claim instead.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.db.automation_schedule_repository as schedule_repo_module
from app.db.automation_repository import create_running_run
from app.db.automation_schedule_repository import (
    claim_due_schedule,
    get_or_create_schedule,
    get_schedule,
    record_last_run,
)
from app.db.base import Base
from app.db.models import AutomationScheduleRecord

ACCOUNT = "me@example.com"


def _ensure_utc(value: datetime) -> datetime:
    """SQLite (unlike Postgres) doesn't preserve tzinfo through a
    `DateTime(timezone=True)` round-trip -- a value read back from the DB
    comes back naive even though it was written as UTC. Every timestamp
    this test module ever writes is UTC, so reattaching UTC here is
    always safe -- mirrors
    app.db.automation_schedule_repository._ensure_utc exactly (the same
    normalization the repository itself already applies internally for
    its own comparisons; tests need it too whenever they compare a
    freshly-read value against a tz-aware `datetime.now(UTC)`).
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_automation_schedule.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class TestInitialScheduleCreation:
    def test_get_or_create_seeds_a_row_immediately_due(self, session_factory):
        db = session_factory()
        try:
            now = datetime.now(UTC)
            schedule = get_or_create_schedule(db, ACCOUNT, now=now)
            assert schedule.account_key == ACCOUNT
            assert _ensure_utc(schedule.next_run_at) == now
            assert schedule.last_claimed_at is None
            assert schedule.last_run_id is None
        finally:
            db.close()

    def test_get_or_create_is_idempotent_and_returns_the_same_row(self, session_factory):
        db = session_factory()
        try:
            first = get_or_create_schedule(db, ACCOUNT)
            second = get_or_create_schedule(db, ACCOUNT)
            assert first.id == second.id
        finally:
            db.close()

    def test_concurrent_first_creation_deduplicates_to_one_row(self, session_factory):
        """Two independent sessions racing the very first
        get_or_create_schedule call for the same account must never
        both succeed in inserting -- the UNIQUE constraint on
        account_key + IntegrityError-catch idiom collapses them to one
        row, mirroring app.db.automation_repository's own pattern.
        """
        session_a = session_factory()
        session_b = session_factory()
        try:
            record_a = get_or_create_schedule(session_a, ACCOUNT)
            record_b = get_or_create_schedule(session_b, ACCOUNT)
            assert record_a.id == record_b.id
        finally:
            session_a.close()
            session_b.close()


class TestDueVsNotDueClaim:
    def test_due_schedule_claims_successfully(self, session_factory):
        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            claimed = claim_due_schedule(db, ACCOUNT, interval_seconds=3600)
            assert claimed is not None
            assert claimed.account_key == ACCOUNT
        finally:
            db.close()

    def test_not_due_schedule_does_not_claim(self, session_factory):
        db = session_factory()
        try:
            now = datetime.now(UTC)
            future = now + timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=future)

            claimed = claim_due_schedule(db, ACCOUNT, interval_seconds=3600, now=now)
            assert claimed is None

            # The row must be completely untouched by the failed claim
            # attempt.
            unchanged = get_schedule(db, ACCOUNT)
            assert _ensure_utc(unchanged.next_run_at) == future
            assert unchanged.last_claimed_at is None
        finally:
            db.close()

    def test_claim_with_no_schedule_row_returns_none(self, session_factory):
        db = session_factory()
        try:
            claimed = claim_due_schedule(db, "never-scheduled@example.com", interval_seconds=3600)
            assert claimed is None
        finally:
            db.close()

    def test_next_run_at_moves_forward_after_claim(self, session_factory):
        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            now = datetime.now(UTC)
            claimed = claim_due_schedule(db, ACCOUNT, interval_seconds=3600, now=now)
            assert claimed is not None
            expected = now + timedelta(seconds=3600)
            assert abs((_ensure_utc(claimed.next_run_at) - expected).total_seconds()) < 1
            assert _ensure_utc(claimed.next_run_at) > now
            assert _ensure_utc(claimed.last_claimed_at) == now
        finally:
            db.close()


class TestCoalescing:
    def test_very_old_missed_next_run_at_claims_exactly_once_with_no_catch_up(
        self, session_factory
    ):
        """A slot that was due 5 intervals ago (the worker was offline)
        must be claimed exactly once, and the resulting next_run_at
        must be relative to THIS claim moment -- never a replay of the
        missed interval count.
        """
        db = session_factory()
        try:
            interval = 3600
            very_old = datetime.now(UTC) - timedelta(seconds=interval * 5)
            get_or_create_schedule(db, ACCOUNT, now=very_old)

            now = datetime.now(UTC)
            first_claim = claim_due_schedule(db, ACCOUNT, interval_seconds=interval, now=now)
            assert first_claim is not None
            expected_next = now + timedelta(seconds=interval)
            assert abs((_ensure_utc(first_claim.next_run_at) - expected_next).total_seconds()) < 1

            # Immediately re-attempting a claim at the same "now" must
            # fail -- the slot is no longer due, no backlog of 5 replays
            # is available.
            second_claim = claim_due_schedule(db, ACCOUNT, interval_seconds=interval, now=now)
            assert second_claim is None
        finally:
            db.close()


class TestConcurrentClaim:
    def test_exactly_one_of_two_concurrent_claim_attempts_wins(self, session_factory):
        """Two independent Sessions (standing in for two scheduler
        processes) both observing the same due slot at the same moment
        -- only one may win the CAS; the loser must see None and must
        never have advanced next_run_at itself.
        """
        session_a = session_factory()
        session_b = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(session_a, ACCOUNT, now=past)

            now = datetime.now(UTC)
            claim_a = claim_due_schedule(session_a, ACCOUNT, interval_seconds=3600, now=now)
            claim_b = claim_due_schedule(session_b, ACCOUNT, interval_seconds=3600, now=now)

            results = [claim_a, claim_b]
            winners = [r for r in results if r is not None]
            losers = [r for r in results if r is None]
            assert len(winners) == 1
            assert len(losers) == 1

        finally:
            session_a.close()
            session_b.close()

    def test_original_due_slot_cannot_trigger_twice(self, session_factory):
        """After a winning claim, a THIRD attempt to claim using the
        original (now stale) observed next_run_at must also fail -- the
        slot has already moved into the future.
        """
        session_a = session_factory()
        session_b = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(session_a, ACCOUNT, now=past)

            now = datetime.now(UTC)
            first = claim_due_schedule(session_a, ACCOUNT, interval_seconds=3600, now=now)
            assert first is not None

            # session_b re-reads and re-attempts right after -- the slot
            # is no longer due (moved 3600s into the future), so it must
            # not be claimable again.
            second = claim_due_schedule(session_b, ACCOUNT, interval_seconds=3600, now=now)
            assert second is None
        finally:
            session_a.close()
            session_b.close()


class TestRestartSurvivesFutureSchedule:
    def test_persisted_future_next_run_at_survives_a_fresh_session_and_is_not_claimable_early(
        self, session_factory
    ):
        """Simulates a worker restart: a fresh Session (standing in for
        a brand-new process) reads back the persisted, still-future
        next_run_at and must not treat restart itself as a trigger to
        run early.
        """
        db_setup = session_factory()
        try:
            now = datetime.now(UTC)
            future = now + timedelta(minutes=30)
            get_or_create_schedule(db_setup, ACCOUNT, now=future)
        finally:
            db_setup.close()

        db_after_restart = session_factory()
        try:
            reloaded = get_schedule(db_after_restart, ACCOUNT)
            assert reloaded is not None
            assert _ensure_utc(reloaded.next_run_at) == future

            claimed = claim_due_schedule(db_after_restart, ACCOUNT, interval_seconds=3600, now=now)
            assert claimed is None
        finally:
            db_after_restart.close()


class TestRecordLastRun:
    def test_record_last_run_persists_the_run_id(self, session_factory):
        db = session_factory()
        try:
            get_or_create_schedule(db, ACCOUNT)
            run, _created = create_running_run(db, account_key=ACCOUNT, holder="holder-A")

            record_last_run(db, ACCOUNT, run_id=run.id)

            reloaded = get_schedule(db, ACCOUNT)
            assert reloaded.last_run_id == run.id
        finally:
            db.close()


# ---------------------------------------------------------------------------
# S8B-TEST-001 (Codex): the tests above use two Sessions but never
# guarantee real overlap -- both calls happen sequentially on the SAME
# thread, so the "concurrency" they exercise is only ever "two Sessions
# called back to back", never a genuine race at the DB level. The classes
# below add REAL threads, REAL separate Sessions/DB connections, against
# a file-backed SQLite DB, synchronized with threading.Barrier so both
# racers genuinely overlap on the same read/observe-then-write window.
#
# The barrier is injected via a THIN, test-only wrapper around the REAL
# `get_schedule` (never a reimplementation of the CAS SQL itself, and
# never a change to production code) -- since both `get_or_create_schedule`
# and `claim_due_schedule` call `get_schedule(db, account_key)` as their
# own first step, wrapping that one function is enough to force "both
# callers observe the identical starting state before either writes",
# without touching a single line of the actual INSERT/UPDATE logic under
# test.
# ---------------------------------------------------------------------------


def _make_barrier_synced_get_schedule(original, barrier: threading.Barrier):
    """Wraps the REAL `get_schedule` (`original`) so that the FIRST call
    made by each calling thread blocks until BOTH threads have made their
    own first call -- forcing `claim_due_schedule`'s (or
    `get_or_create_schedule`'s) internal read to genuinely overlap across
    two real threads before either proceeds to its own INSERT/UPDATE. Any
    SUBSEQUENT call from the SAME thread (e.g. `get_or_create_schedule`'s
    own fallback re-read after losing an IntegrityError race) passes
    straight through -- tracked per-thread so an already-resolved thread
    can never deadlock waiting for a second rendezvous nobody else is
    coming to.
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
            barrier.wait(timeout=10)
        return result

    return _wrapped


class TestRealConcurrentInitialScheduleCreation:
    """S8B-TEST-001 Part A: force both workers to observe "no row" before
    either completes its INSERT, then race the REAL
    `get_or_create_schedule(...)`.
    """

    def test_both_workers_observe_no_row_then_race_the_insert(self, tmp_path):
        db_path = tmp_path / "test_automation_schedule_concurrent_create.db"
        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        barrier = threading.Barrier(2)

        now = datetime.now(UTC)
        results: dict[str, tuple[str, object]] = {}

        def _worker(name, db):
            try:
                record = get_or_create_schedule(db, ACCOUNT, now=now)
                results[name] = ("ok", record)
            except Exception as exc:
                db.rollback()
                results[name] = ("error", exc)

        session_a = factory()
        session_b = factory()
        original_get_schedule = schedule_repo_module.get_schedule
        wrapped = _make_barrier_synced_get_schedule(original_get_schedule, barrier)
        schedule_repo_module.get_schedule = wrapped
        try:
            thread_a = threading.Thread(target=_worker, args=("a", session_a))
            thread_b = threading.Thread(target=_worker, args=("b", session_b))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=15)
            thread_b.join(timeout=15)
        finally:
            schedule_repo_module.get_schedule = original_get_schedule

        try:
            assert not thread_a.is_alive()
            assert not thread_b.is_alive()
            assert set(results) == {"a", "b"}

            # No duplicate row -- exactly one automation_schedules row for
            # this account, regardless of which branch fired below.
            verify_session = factory()
            try:
                row_count = verify_session.scalar(
                    select(func.count())
                    .select_from(AutomationScheduleRecord)
                    .where(AutomationScheduleRecord.account_key == ACCOUNT)
                )
                assert row_count == 1
                canonical_row = get_schedule(verify_session, ACCOUNT)
            finally:
                verify_session.close()

            for name, (status, payload) in results.items():
                db = session_a if name == "a" else session_b
                if status == "ok":
                    # Both callers ultimately observe the SAME row.
                    assert payload.id == canonical_row.id
                else:
                    # A documented, safely-CONTAINED SQLite contention
                    # error (e.g. "database is locked") -- never hidden
                    # behind a retry loop added to the repository just to
                    # make this test green. Prove the LOSING Session is
                    # genuinely rolled back/reusable afterward -- the same
                    # recovery app.scheduler._poll_loop's own
                    # except/finally already performs at the tick level.
                    assert isinstance(payload, Exception)
                    reloaded = get_schedule(db, ACCOUNT)
                    assert reloaded is not None
                    assert reloaded.id == canonical_row.id

            # A later, completely normal poll succeeds afterward.
            later_session = factory()
            try:
                claimed = claim_due_schedule(later_session, ACCOUNT, interval_seconds=3600, now=now)
                assert claimed is not None
            finally:
                later_session.close()
        finally:
            session_a.close()
            session_b.close()
            engine.dispose()


class TestRealConcurrentDueSlotClaim:
    """S8B-TEST-001 Part B: two real threads observe the SAME due
    next_run_at, reach a barrier, then attempt the REAL
    `claim_due_schedule(...)` CAS UPDATE concurrently. Proves "at most
    one winner" under genuine DB-level concurrency, not just two
    sequential in-process calls.
    """

    def test_only_one_thread_wins_the_same_due_slot(self, tmp_path):
        db_path = tmp_path / "test_automation_schedule_concurrent_claim.db"
        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        setup_db = factory()
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

        session_a = factory()
        session_b = factory()
        original_get_schedule = schedule_repo_module.get_schedule
        wrapped = _make_barrier_synced_get_schedule(original_get_schedule, barrier)
        schedule_repo_module.get_schedule = wrapped
        try:
            thread_a = threading.Thread(target=_worker, args=("a", session_a))
            thread_b = threading.Thread(target=_worker, args=("b", session_b))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=15)
            thread_b.join(timeout=15)
        finally:
            schedule_repo_module.get_schedule = original_get_schedule

        try:
            winners = [
                payload
                for status, payload in results.values()
                if status == "ok" and payload is not None
            ]
            # The actual invariant: NEVER two rowcount==1 winners.
            assert len(winners) <= 1
            # Given the generous 30s busy_timeout above, real contention
            # resolves by blocking/retrying (not erroring) in practice --
            # the slot genuinely was due, so exactly one racer wins it.
            assert len(winners) == 1, "the due slot must be claimed by exactly one racer"

            for name, (status, _payload) in results.items():
                db = session_a if name == "a" else session_b
                if status == "error":
                    reloaded = get_schedule(db, ACCOUNT)
                    assert reloaded is not None

            # next_run_at was advanced exactly once, to now + interval --
            # not double-advanced. run_due_cycle_if_claimed only ever
            # calls run_automation_cycle when claim_due_schedule returns
            # non-None (see TestStage8AReuse in
            # tests/test_scheduler_service.py) -- so "at most one winner"
            # proven here is exactly "at most one automation run
            # triggered for this slot" at the service layer above it.
            verify_session = factory()
            try:
                final = get_schedule(verify_session, ACCOUNT)
                expected_next = now + timedelta(seconds=3600)
                assert abs((_ensure_utc(final.next_run_at) - expected_next).total_seconds()) < 1
            finally:
                verify_session.close()

            # The original due slot cannot trigger twice.
            later_session = factory()
            try:
                stale_claim = claim_due_schedule(
                    later_session, ACCOUNT, interval_seconds=3600, now=now
                )
                assert stale_claim is None
            finally:
                later_session.close()
        finally:
            session_a.close()
            session_b.close()
            engine.dispose()


class TestSQLiteWALRealConcurrentClaim:
    """S8B-TEST-001 Part C: same real-thread barrier-controlled claim as
    above, but with the file-backed DB explicitly switched to WAL
    (`PRAGMA journal_mode=WAL`) and a much smaller busy_timeout on the
    racing connections -- WAL improves reader/writer concurrency but
    still serializes writer/writer contention exactly like the default
    rollback journal, so a real SQLITE_BUSY/"database is locked" is far
    more likely to surface here than under the generous 30s timeout
    above. The invariant must hold identically either way.
    """

    def test_wal_mode_still_produces_at_most_one_winner(self, tmp_path):
        db_path = tmp_path / "test_automation_schedule_wal.db"

        setup_engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
        )
        Base.metadata.create_all(setup_engine)
        with setup_engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert mode is not None and mode.lower() == "wal"

        setup_session = sessionmaker(bind=setup_engine)()
        past = datetime.now(UTC) - timedelta(hours=1)
        get_or_create_schedule(setup_session, ACCOUNT, now=past)
        setup_session.close()
        setup_engine.dispose()

        # A tiny busy_timeout (vs the 30s used elsewhere in this file) so
        # genuine writer/writer contention is far more likely to surface
        # as a real OperationalError instead of silently blocking for the
        # full window -- the scenario this test exists to exercise.
        racer_engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 0.2}
        )
        with racer_engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        factory = sessionmaker(bind=racer_engine, autoflush=False, autocommit=False)

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

        session_a = factory()
        session_b = factory()
        original_get_schedule = schedule_repo_module.get_schedule
        wrapped = _make_barrier_synced_get_schedule(original_get_schedule, barrier)
        schedule_repo_module.get_schedule = wrapped
        try:
            thread_a = threading.Thread(target=_worker, args=("a", session_a))
            thread_b = threading.Thread(target=_worker, args=("b", session_b))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=15)
            thread_b.join(timeout=15)
        finally:
            schedule_repo_module.get_schedule = original_get_schedule

        try:
            winners = [
                payload
                for status, payload in results.values()
                if status == "ok" and payload is not None
            ]
            # NEVER a second successful claim, WAL or not -- this is the
            # invariant that must never be weakened just because SQLite
            # may fail one writer under tight contention.
            assert len(winners) <= 1

            for name, (status, payload) in results.items():
                db = session_a if name == "a" else session_b
                if status == "error":
                    assert isinstance(payload, Exception)
                    # A failed iteration must leave no poisoned
                    # Session/transaction -- already rolled back inside
                    # _worker; prove it is genuinely usable afterward.
                    reloaded = get_schedule(db, ACCOUNT)
                    assert reloaded is not None

            # Next poll (a fresh Session) can operate normally afterward,
            # regardless of which branch fired above.
            verify_session = factory()
            try:
                final = get_schedule(verify_session, ACCOUNT)
                if winners:
                    expected_next = now + timedelta(seconds=3600)
                    assert abs((_ensure_utc(final.next_run_at) - expected_next).total_seconds()) < 1
                    later = expected_next + timedelta(seconds=1)
                else:
                    # Both racers hit a contained contention error -- the
                    # slot is still due (never advanced); the next poll
                    # must still be able to claim it normally.
                    assert _ensure_utc(final.next_run_at) <= now
                    later = now
            finally:
                verify_session.close()

            next_poll_session = factory()
            try:
                next_claim = claim_due_schedule(
                    next_poll_session, ACCOUNT, interval_seconds=3600, now=later
                )
                assert next_claim is not None
            finally:
                next_poll_session.close()
        finally:
            session_a.close()
            session_b.close()
            racer_engine.dispose()
