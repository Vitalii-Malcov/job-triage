"""Stage 8B tests for `app.services.scheduler` -- the thin trigger layer
that claims a due schedule slot and, only if that claim was won, calls
the EXISTING, UNCHANGED `app.services.automation.run_automation_cycle`.
Mirrors tests/test_automation_lease.py's monkeypatch-the-collector-steps
approach (no real network I/O anywhere) plus
tests/test_follow_up_send_service.py's source-scan safety-import check.
"""

import asyncio
import inspect
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.automation_repository import create_running_run
from app.db.automation_schedule_repository import get_or_create_schedule, get_schedule
from app.db.base import Base
from app.services.automation import AutomationRunAlreadyInProgressError, AutomationRunLeaseLostError
from app.services.scheduler import (
    SchedulerConfigurationError,
    run_due_cycle_if_claimed,
    validate_scheduler_settings,
)

ACCOUNT = "me@example.com"


def _ensure_utc(value: datetime) -> datetime:
    """SQLite doesn't preserve tzinfo through a `DateTime(timezone=True)`
    round-trip -- mirrors app.db.automation_schedule_repository._ensure_utc
    exactly; tests need the same normalization whenever comparing a
    freshly-read value against a tz-aware `datetime.now(UTC)`."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_scheduler_service.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


async def _noop_collector(db, settings, *, touched_jobs=None, is_lease_lost=None):
    return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}


class TestValidateSchedulerSettings:
    def test_disabled_never_raises_regardless_of_account_key(self):
        validate_scheduler_settings(Settings(automation_scheduler_enabled=False))

    def test_enabled_with_account_key_does_not_raise(self):
        validate_scheduler_settings(
            Settings(
                automation_scheduler_enabled=True,
                automation_scheduler_account_key="me@example.com",
            )
        )

    def test_enabled_with_blank_account_key_raises(self):
        # Settings itself already fails closed at construction (see
        # tests/test_config.py) -- this proves validate_scheduler_settings
        # ALSO fails closed independently, e.g. if a future caller ever
        # builds a Settings-like object without going through
        # Settings()'s own model_validator.
        settings = Settings.model_construct(
            automation_scheduler_enabled=True, automation_scheduler_account_key="   "
        )
        with pytest.raises(SchedulerConfigurationError):
            validate_scheduler_settings(settings)


class TestStage8AReuse:
    def test_due_slot_calls_run_automation_cycle_exactly_once_with_own_session(
        self, session_factory, monkeypatch
    ):
        calls: list[tuple[object, str]] = []

        async def _tracking_bundesagentur(db, settings, *, touched_jobs=None, is_lease_lost=None):
            calls.append((db, "bundesagentur"))
            return await _noop_collector(db, settings)

        async def _tracking_xing(db, settings, *, touched_jobs=None, is_lease_lost=None):
            calls.append((db, "xing"))
            return await _noop_collector(db, settings)

        monkeypatch.setattr("app.services.automation.run_bundesagentur", _tracking_bundesagentur)
        monkeypatch.setattr("app.services.automation.run_xing", _tracking_xing)

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            triggered = asyncio.run(
                run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
            )

            assert triggered is True
            assert len(calls) == 2  # bundesagentur + xing, exactly once each
            assert all(call_db is db for call_db, _ in calls)  # the scheduler's OWN session

            schedule = get_schedule(db, ACCOUNT)
            assert schedule.last_run_id is not None
        finally:
            db.close()

    def test_not_due_slot_never_calls_run_automation_cycle(self, session_factory, monkeypatch):
        called = False

        async def _should_not_run(db, settings, *, touched_jobs=None, is_lease_lost=None):
            nonlocal called
            called = True
            return await _noop_collector(db, settings)

        monkeypatch.setattr("app.services.automation.run_bundesagentur", _should_not_run)
        monkeypatch.setattr("app.services.automation.run_xing", _should_not_run)

        db = session_factory()
        try:
            future = datetime.now(UTC) + timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=future)

            triggered = asyncio.run(
                run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
            )

            assert triggered is False
            assert called is False
        finally:
            db.close()


class TestAlreadyInProgressDoesNotRetryImmediately:
    """Realistic scenario: run_automation_cycle raises
    AutomationRunAlreadyInProgressError because a DIFFERENT session
    genuinely already holds the RUNNING lease -- exercises the exact
    same exception, raised the exact same way Stage 8A itself raises it
    (app.db.automation_repository.create_running_run's own claim,
    before any collector step ever runs), so unlike a collector-level
    RuntimeError this one is never intercepted by
    app.services.automation._run_step.
    """

    def test_already_in_progress_is_absorbed_without_raising_or_retry(
        self, session_factory, monkeypatch
    ):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        other_session = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)
            # A different session already holds the RUNNING lease for
            # this account -- run_automation_cycle must raise
            # AutomationRunAlreadyInProgressError.
            create_running_run(other_session, account_key=ACCOUNT, holder="other-holder")

            triggered = asyncio.run(
                run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
            )

            # The tick still reports "attempted" (True) but must not
            # raise AutomationRunAlreadyInProgressError up to the caller
            # -- absorbed and logged, no immediate retry within this
            # call.
            assert triggered is True

            # The schedule slot was still consumed exactly once -- not
            # left due for an immediate re-claim within the same tick.
            schedule = get_schedule(db, ACCOUNT)
            assert _ensure_utc(schedule.next_run_at) > datetime.now(UTC)
        finally:
            db.close()
            other_session.close()


class TestDirectAlreadyInProgressBranch:
    """S8B-TEST-002 Part A: directly monkeypatches
    app.services.scheduler.run_automation_cycle (the name
    run_due_cycle_if_claimed itself calls) to raise
    AutomationRunAlreadyInProgressError -- deterministic, and proves the
    exact except branch in run_due_cycle_if_claimed is reached,
    independent of how Stage 8A happens to produce that exception in
    practice (see TestAlreadyInProgressDoesNotRetryImmediately above for
    that realistic, contention-based proof).
    """

    def test_direct_raise_is_absorbed_slot_stays_advanced_and_no_retry_before_due(
        self, session_factory, monkeypatch
    ):
        call_count = 0

        async def _raise_already_in_progress(db, *, account_key, settings):
            nonlocal call_count
            call_count += 1
            raise AutomationRunAlreadyInProgressError("already running")

        monkeypatch.setattr(
            "app.services.scheduler.run_automation_cycle", _raise_already_in_progress
        )

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            triggered = asyncio.run(
                run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
            )
            # run_due_cycle_if_claimed handles it as designed: absorbed,
            # reported as "attempted", never re-raised to the caller.
            assert triggered is True
            assert call_count == 1

            # The slot was already advanced by claim_due_schedule BEFORE
            # run_automation_cycle was ever called -- no immediate second
            # run within this same tick.
            schedule = get_schedule(db, ACCOUNT)
            assert _ensure_utc(schedule.next_run_at) > datetime.now(UTC)

            # A poll BEFORE next_run_at must not call run_automation_cycle
            # again -- claim_due_schedule itself reports "not due", so
            # run_due_cycle_if_claimed returns False without ever
            # reaching run_automation_cycle a second time.
            triggered_again = asyncio.run(
                run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
            )
            assert triggered_again is False
            assert call_count == 1

            # Session remains usable after absorbing the exception.
            assert get_schedule(db, ACCOUNT) is not None
        finally:
            db.close()


class TestUnexpectedExceptionFailureIsolation:
    def test_collector_level_runtime_error_never_leaks_end_to_end(
        self, session_factory, monkeypatch, caplog
    ):
        """NOT a test of run_due_cycle_if_claimed's own except Exception
        branch -- run_bundesagentur's own per-step isolation
        (app.services.automation._run_step) already swallows a
        collector-level RuntimeError internally, so run_automation_cycle
        returns normally (a COMPLETED/PARTIAL/FAILED AutomationRunRecord)
        rather than raising; the scheduler's own except Exception branch
        is never actually reached here. This test proves a narrower but
        still real property: a collector-level secret-bearing failure
        never leaks end-to-end through the scheduler entrypoint either
        -- see TestDirectRunAutomationCycleException below for the
        DIRECT proof of run_due_cycle_if_claimed's own except Exception
        branch (S8B-TEST-002 Part C).
        """

        async def _boom(db, settings, *, touched_jobs=None, is_lease_lost=None):
            raise RuntimeError("secret-db-detail-should-never-leak")

        monkeypatch.setattr("app.services.automation.run_bundesagentur", _boom)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            with caplog.at_level("DEBUG"):
                triggered = asyncio.run(
                    run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
                )

            assert triggered is True
            assert "secret-db-detail-should-never-leak" not in caplog.text
        finally:
            db.close()

    def test_scheduler_own_exception_branch_never_kills_the_iteration(
        self, session_factory, monkeypatch, caplog
    ):
        """Simulates an unexpected failure inside the scheduler layer
        itself (not inside run_automation_cycle) -- e.g. record_last_run
        raising -- proving run_due_cycle_if_claimed's own BEST-EFFORT
        record_last_run except Exception branch absorbs it, sanitized,
        without propagating. Distinct from
        TestDirectRunAutomationCycleException below, which raises from
        run_automation_cycle itself (a different except Exception branch,
        the one wrapping the run_automation_cycle call).
        """
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        def _boom(db, account_key, *, run_id, now=None):
            raise RuntimeError("another-secret-detail")

        monkeypatch.setattr("app.services.scheduler.record_last_run", _boom)

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            with caplog.at_level("DEBUG"):
                triggered = asyncio.run(
                    run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
                )

            assert triggered is True
            assert "another-secret-detail" not in caplog.text
            assert "RuntimeError" in caplog.text
        finally:
            db.close()


class TestDirectRunAutomationCycleException:
    """S8B-TEST-002 Part C: directly monkeypatches
    app.services.scheduler.run_automation_cycle to raise a plain
    RuntimeError -- the DIRECT proof of run_due_cycle_if_claimed's own
    `except Exception` branch (the one wrapping the run_automation_cycle
    call itself), which
    TestUnexpectedExceptionFailureIsolation.test_collector_level_runtime_error_never_leaks_end_to_end
    above does NOT actually reach (that RuntimeError is intercepted one
    layer down, inside app.services.automation._run_step).
    """

    def test_direct_runtime_error_is_rolled_back_sanitized_and_session_stays_usable(
        self, session_factory, monkeypatch, caplog
    ):
        SENTINEL = "secret-must-never-leak"

        async def _boom(db, *, account_key, settings):
            raise RuntimeError(SENTINEL)

        monkeypatch.setattr("app.services.scheduler.run_automation_cycle", _boom)

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            with caplog.at_level("DEBUG"):
                triggered = asyncio.run(
                    run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
                )

            # Exception does not propagate out of run_due_cycle_if_claimed.
            assert triggered is True
            # Sanitized: only the exception TYPE may appear, never the
            # sentinel/message.
            assert SENTINEL not in caplog.text
            assert "RuntimeError" in caplog.text

            # db.rollback() happened inside the except Exception branch --
            # proven by the Session remaining genuinely usable afterward
            # (a poisoned/un-rolled-back SQLAlchemy Session would raise
            # PendingRollbackError on the very next operation).
            reloaded = get_schedule(db, ACCOUNT)
            assert reloaded is not None

            # Next normal iteration remains usable: the slot was already
            # advanced by claim_due_schedule before run_automation_cycle
            # was ever called, so a poll before next_run_at reports
            # nothing due, without erroring.
            triggered_again = asyncio.run(
                run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
            )
            assert triggered_again is False
        finally:
            db.close()


class TestLeaseLostDoesNotRetryImmediately:
    """S8B-TEST-002 Part B (Codex finding, FIXED): this test previously
    monkeypatched app.services.automation.run_bundesagentur to raise
    AutomationRunLeaseLostError -- invalid, because Stage 8A's own
    app.services.automation._run_step intercepts ANY collector-level
    exception (including this one) internally and records it as a
    failed step; run_automation_cycle itself never actually raises for
    a single step's failure, so run_due_cycle_if_claimed's own `except
    AutomationRunLeaseLostError` branch was never really exercised. Now
    directly monkeypatches app.services.scheduler.run_automation_cycle
    (the name run_due_cycle_if_claimed itself calls) so the actual
    scheduler except branch is genuinely reached.
    """

    def test_lease_lost_is_absorbed_without_raising_slot_stays_advanced_no_retry(
        self, session_factory, monkeypatch, caplog
    ):
        call_count = 0

        async def _raise_lease_lost(db, *, account_key, settings):
            nonlocal call_count
            call_count += 1
            raise AutomationRunLeaseLostError("lease lost mid-run")

        monkeypatch.setattr("app.services.scheduler.run_automation_cycle", _raise_lease_lost)

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            with caplog.at_level("DEBUG"):
                triggered = asyncio.run(
                    run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
                )

            # The actual scheduler except AutomationRunLeaseLostError
            # branch was reached (not swallowed one layer down).
            assert triggered is True
            assert call_count == 1
            # Sanitized logging: the fixed, hardcoded event message only
            # -- this branch never touches str(exc) in the first place,
            # so there is nothing exception-specific to leak.
            #
            # AUD-010 LOW (Codex gate follow-up, Astra R4B): account_key
            # is a normalized email address and must never appear in
            # runtime scheduler logs -- schedule_id (a non-identity
            # surrogate key) is the correlator instead, mirroring
            # run_due_digest_if_claimed's existing delivery_id
            # convention (S8E-PRIVACY-001).
            assert "automation_scheduler_run_lease_lost" in caplog.text
            assert "schedule_id=" in caplog.text
            assert ACCOUNT not in caplog.text

            # The slot remains advanced -- no immediate retry.
            schedule = get_schedule(db, ACCOUNT)
            assert _ensure_utc(schedule.next_run_at) > datetime.now(UTC)

            triggered_again = asyncio.run(
                run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
            )
            assert triggered_again is False
            assert call_count == 1

            # Session remains usable.
            assert get_schedule(db, ACCOUNT) is not None
        finally:
            db.close()


def _scheduler_log_text(caplog) -> str:
    """Only `app.services.scheduler`'s OWN log records -- this ticket
    (AUD-010) scopes to the scheduler module specifically.
    `app.services.automation.run_automation_cycle` (called BY the
    scheduler, but also directly by POST /automation/runs) logs its own
    `account_key=...` separately and is out of scope here -- fixing that
    would touch a shared module used by a different, unrelated caller.
    """
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name == "app.services.scheduler"
    )


class TestSchedulerLogsNeverIncludeAccountIdentity:
    """AUD-010 LOW (Codex gate follow-up, Astra R4B) regression:
    `account_key` is a normalized email address
    (`AutomationScheduleRecord`'s own docstring) -- `run_due_cycle_if_
    claimed` must never write it into a log line, on ANY branch, exactly
    like `run_due_digest_if_claimed` already never logs it (S8E-
    PRIVACY-001). Covers the remaining branches
    TestLeaseLostDoesNotRetryImmediately's own regression above does not:
    already-in-progress, the success/run_triggered path, and the
    best-effort record_last_run failure path.
    """

    def test_already_in_progress_log_omits_account_key(self, session_factory, monkeypatch, caplog):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        other_session = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)
            create_running_run(other_session, account_key=ACCOUNT, holder="other-holder")

            with caplog.at_level("DEBUG"):
                triggered = asyncio.run(
                    run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
                )

            assert triggered is True
            scheduler_log_text = _scheduler_log_text(caplog)
            assert "automation_scheduler_run_already_in_progress" in scheduler_log_text
            assert "schedule_id=" in scheduler_log_text
            assert ACCOUNT not in scheduler_log_text
        finally:
            db.close()
            other_session.close()

    def test_successful_run_triggered_log_omits_account_key(
        self, session_factory, monkeypatch, caplog
    ):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            with caplog.at_level("DEBUG"):
                triggered = asyncio.run(
                    run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
                )

            assert triggered is True
            scheduler_log_text = _scheduler_log_text(caplog)
            assert "automation_scheduler_run_triggered" in scheduler_log_text
            assert "schedule_id=" in scheduler_log_text
            assert "run_id=" in scheduler_log_text
            assert ACCOUNT not in scheduler_log_text
        finally:
            db.close()

    def test_record_last_run_failure_log_omits_account_key(
        self, session_factory, monkeypatch, caplog
    ):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        def _boom(db, account_key, *, run_id, now=None):
            raise RuntimeError("secret-detail-must-not-leak")

        monkeypatch.setattr("app.services.scheduler.record_last_run", _boom)

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            with caplog.at_level("DEBUG"):
                triggered = asyncio.run(
                    run_due_cycle_if_claimed(db, account_key=ACCOUNT, settings=Settings())
                )

            assert triggered is True
            scheduler_log_text = _scheduler_log_text(caplog)
            assert "automation_scheduler_record_last_run_failed" in scheduler_log_text
            assert "schedule_id=" in scheduler_log_text
            assert "secret-detail-must-not-leak" not in caplog.text
            assert ACCOUNT not in scheduler_log_text
        finally:
            db.close()


class TestSafetyNoSendOrApprovalImports:
    def test_scheduler_service_module_never_imports_send_or_approval_logic(self):
        import app.services.scheduler as module

        source = inspect.getsource(module)
        for forbidden in (
            "smtp",
            "SMTP",
            "send_follow_up",
            "send_response",
            "approve_or_reject",
            "bewerbung_send",
            "TelegramNotifier(",
        ):
            assert forbidden not in source

    def test_scheduler_entrypoint_module_never_imports_send_or_approval_logic(self):
        import app.scheduler as module

        source = inspect.getsource(module)
        for forbidden in (
            "smtp",
            "SMTP",
            "send_follow_up",
            "send_response",
            "approve_or_reject",
            "bewerbung_send",
            "TelegramNotifier(",
        ):
            assert forbidden not in source

    def test_scheduler_service_only_calls_the_existing_run_automation_cycle(self):
        """Stage 8A reuse, not reimplementation -- the scheduler module
        must import run_automation_cycle from app.services.automation,
        never define its own collector-orchestration logic."""
        import app.services.automation as automation_module
        import app.services.scheduler as module

        assert module.run_automation_cycle is automation_module.run_automation_cycle
