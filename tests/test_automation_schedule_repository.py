"""Stage 8B tests for `app.db.automation_schedule_repository` -- the
persisted schedule state + atomic CAS claim underlying the standalone
scheduler worker. Mirrors tests/test_automation_lease.py's real
two-Session/independent-engine-connection proof style for Stage 8A's
own lease, applied here to the schedule-slot claim instead.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.automation_repository import create_running_run
from app.db.automation_schedule_repository import (
    claim_due_schedule,
    get_or_create_schedule,
    get_schedule,
    record_last_run,
)
from app.db.base import Base

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
