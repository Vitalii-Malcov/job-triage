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
from app.services.automation import AutomationRunLeaseLostError
from app.services.scheduler import (
    SchedulerConfigurationError,
    run_due_cycle_if_claimed,
    validate_scheduler_settings,
)

ACCOUNT = "me@example.com"


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_scheduler_service.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


async def _noop_collector(db, settings):
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

        async def _tracking_bundesagentur(db, settings):
            calls.append((db, "bundesagentur"))
            return await _noop_collector(db, settings)

        async def _tracking_xing(db, settings):
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

        async def _should_not_run(db, settings):
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
            next_run_at = schedule.next_run_at
            if next_run_at.tzinfo is None:
                next_run_at = next_run_at.replace(tzinfo=UTC)
            assert next_run_at > datetime.now(UTC)
        finally:
            db.close()
            other_session.close()


class TestUnexpectedExceptionFailureIsolation:
    def test_unexpected_exception_does_not_propagate_and_is_sanitized(
        self, session_factory, monkeypatch, caplog
    ):
        async def _boom(db, settings):
            raise RuntimeError("secret-db-detail-should-never-leak")

        monkeypatch.setattr("app.services.automation.run_bundesagentur", _boom)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        try:
            past = datetime.now(UTC) - timedelta(hours=1)
            get_or_create_schedule(db, ACCOUNT, now=past)

            with caplog.at_level("DEBUG"):
                # run_bundesagentur's own per-step isolation
                # (app.services.automation._run_step) already swallows
                # this -- run_automation_cycle itself never raises for a
                # single step's failure. This test still proves the
                # scheduler layer's OWN except Exception branch never
                # leaks a raw secret even in the (defense-in-depth)
                # case of a truly unexpected failure reaching it.
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
        raising -- proving run_due_cycle_if_claimed's own except Exception
        branch absorbs it, sanitized, without propagating.
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


class TestLeaseLostDoesNotRetryImmediately:
    def test_lease_lost_is_absorbed_without_raising(self, session_factory, monkeypatch, caplog):
        async def _raise_lease_lost(db, settings):
            raise AutomationRunLeaseLostError("lease lost mid-run")

        monkeypatch.setattr("app.services.automation.run_bundesagentur", _raise_lease_lost)
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
