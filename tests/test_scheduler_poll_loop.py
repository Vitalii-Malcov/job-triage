"""S8B-TEST-002 (poll loop / session close / cancellation): direct tests
of `app.scheduler._poll_loop` itself -- not merely
`run_due_cycle_if_claimed` (already covered end-to-end in
tests/test_scheduler_service.py). These use a fake `SessionLocal`
factory (monkeypatching the lazy `from app.db.session import
SessionLocal` import inside `_poll_loop`) and a fake
`run_due_cycle_if_claimed` so the loop's own session-lifecycle and
exception-containment contract can be proven in complete isolation from
real DB I/O and real business logic, deterministically via
`asyncio.Event`s rather than timing sleeps.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.scheduler import _poll_loop


def _fake_settings(**overrides) -> SimpleNamespace:
    defaults = {
        "automation_scheduler_account_key": "me@example.com",
        "automation_scheduler_poll_seconds": 0.01,
        "automation_scheduler_interval_seconds": 3600,
        "automation_scheduler_enabled": True,
        "telegram_daily_digest_enabled": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.rollback_called = False

    def rollback(self) -> None:
        self.rollback_called = True

    def close(self) -> None:
        self.closed = True


class _FakeSessionLocal:
    """Stands in for app.db.session.SessionLocal -- a plain callable
    factory, tracking every Session it hands out so tests can assert on
    open/close state afterward."""

    def __init__(self) -> None:
        self.created: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession()
        self.created.append(session)
        return session


async def _cancel_after(task: asyncio.Task, event: asyncio.Event, timeout: float = 5.0) -> None:
    await asyncio.wait_for(event.wait(), timeout=timeout)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestSessionLifecyclePerTick:
    def test_opens_and_closes_exactly_one_session_per_completed_tick(self, monkeypatch):
        fake_session_local = _FakeSessionLocal()
        monkeypatch.setattr("app.db.session.SessionLocal", fake_session_local)

        tick_count = 0
        ticked_twice = asyncio.Event()

        async def _fake_run_due_cycle_if_claimed(db, *, account_key, settings):
            nonlocal tick_count
            assert db in fake_session_local.created
            assert db.closed is False  # never closed while still in use
            tick_count += 1
            if tick_count >= 2:
                ticked_twice.set()
            return True

        monkeypatch.setattr(
            "app.scheduler.run_due_cycle_if_claimed", _fake_run_due_cycle_if_claimed
        )

        async def _drive():
            task = asyncio.create_task(_poll_loop(_fake_settings()))
            await _cancel_after(task, ticked_twice)

        asyncio.run(_drive())

        assert tick_count >= 2
        # Every Session ever opened was closed -- including the LAST one,
        # whose tick had already completed before cancellation landed
        # during the subsequent asyncio.sleep.
        assert len(fake_session_local.created) == tick_count
        assert all(session.closed for session in fake_session_local.created)

    def test_closes_session_even_when_cancelled_mid_iteration(self, monkeypatch):
        """Cancellation lands WHILE run_due_cycle_if_claimed is actively
        suspended (not merely between ticks during asyncio.sleep) -- the
        finally: db.close() must still run."""
        fake_session_local = _FakeSessionLocal()
        monkeypatch.setattr("app.db.session.SessionLocal", fake_session_local)

        entered_iteration = asyncio.Event()

        async def _fake_run_due_cycle_if_claimed(db, *, account_key, settings):
            entered_iteration.set()
            await asyncio.sleep(60)  # the test cancels long before this ever returns
            return True

        monkeypatch.setattr(
            "app.scheduler.run_due_cycle_if_claimed", _fake_run_due_cycle_if_claimed
        )

        async def _drive():
            task = asyncio.create_task(_poll_loop(_fake_settings()))
            await _cancel_after(task, entered_iteration)

        asyncio.run(_drive())

        assert len(fake_session_local.created) == 1
        assert fake_session_local.created[0].closed is True

    def test_no_session_is_left_open_after_several_ticks_and_cancellation(self, monkeypatch):
        fake_session_local = _FakeSessionLocal()
        monkeypatch.setattr("app.db.session.SessionLocal", fake_session_local)

        tick_count = 0
        ticked_five_times = asyncio.Event()

        async def _fake_run_due_cycle_if_claimed(db, *, account_key, settings):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 5:
                ticked_five_times.set()
            return True

        monkeypatch.setattr(
            "app.scheduler.run_due_cycle_if_claimed", _fake_run_due_cycle_if_claimed
        )

        async def _drive():
            task = asyncio.create_task(_poll_loop(_fake_settings()))
            await _cancel_after(task, ticked_five_times)

        asyncio.run(_drive())

        assert tick_count >= 5
        assert len(fake_session_local.created) == tick_count
        assert all(session.closed for session in fake_session_local.created), (
            "no Session may be left open after the loop is cancelled"
        )


class TestCancellationPropagation:
    def test_cancellation_propagates_out_of_asyncio_run_style_await(self, monkeypatch):
        fake_session_local = _FakeSessionLocal()
        monkeypatch.setattr("app.db.session.SessionLocal", fake_session_local)

        started = asyncio.Event()

        async def _fake_run_due_cycle_if_claimed(db, *, account_key, settings):
            started.set()
            return True

        monkeypatch.setattr(
            "app.scheduler.run_due_cycle_if_claimed", _fake_run_due_cycle_if_claimed
        )

        async def _drive():
            task = asyncio.create_task(_poll_loop(_fake_settings()))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            # Mirrors how app.scheduler.main() ultimately awaits
            # _poll_loop via asyncio.run(_poll_loop(settings)) -- a
            # cancelled task's CancelledError propagates to the awaiter,
            # never silently swallowed inside the loop.
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()

        asyncio.run(_drive())


class TestTickLevelExceptionContainment:
    def test_tick_exception_is_rolled_back_closed_and_loop_continues(self, monkeypatch, caplog):
        fake_session_local = _FakeSessionLocal()
        monkeypatch.setattr("app.db.session.SessionLocal", fake_session_local)

        call_count = 0
        second_tick_started = asyncio.Event()

        async def _fake_run_due_cycle_if_claimed(db, *, account_key, settings):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("secret-db-detail-should-never-leak")
            second_tick_started.set()
            return True

        monkeypatch.setattr(
            "app.scheduler.run_due_cycle_if_claimed", _fake_run_due_cycle_if_claimed
        )

        async def _drive():
            task = asyncio.create_task(_poll_loop(_fake_settings()))
            await _cancel_after(task, second_tick_started)

        with caplog.at_level("DEBUG"):
            asyncio.run(_drive())

        assert call_count >= 2  # the loop continued to a second iteration

        first_session = fake_session_local.created[0]
        assert first_session.rollback_called is True
        assert first_session.closed is True
        # No secret exception message anywhere in the logs -- only the
        # exception TYPE.
        assert "secret-db-detail-should-never-leak" not in caplog.text
        assert "RuntimeError" in caplog.text

    def test_every_session_across_a_failing_and_a_succeeding_tick_is_closed(
        self, monkeypatch, caplog
    ):
        fake_session_local = _FakeSessionLocal()
        monkeypatch.setattr("app.db.session.SessionLocal", fake_session_local)

        outcomes = iter([RuntimeError("boom-1"), None, RuntimeError("boom-2"), None])
        third_tick_started = asyncio.Event()
        call_count = 0

        async def _fake_run_due_cycle_if_claimed(db, *, account_key, settings):
            nonlocal call_count
            call_count += 1
            outcome = next(outcomes, None)
            if call_count >= 3:
                third_tick_started.set()
            if isinstance(outcome, Exception):
                raise outcome
            return True

        monkeypatch.setattr(
            "app.scheduler.run_due_cycle_if_claimed", _fake_run_due_cycle_if_claimed
        )

        async def _drive():
            task = asyncio.create_task(_poll_loop(_fake_settings()))
            await _cancel_after(task, third_tick_started)

        with caplog.at_level("DEBUG"):
            asyncio.run(_drive())

        assert call_count >= 3
        assert all(session.closed for session in fake_session_local.created)
        assert "boom-1" not in caplog.text
        assert "boom-2" not in caplog.text


class TestStartupLoggingPrivacy:
    """AUD-010: `_poll_loop`'s one-time startup log line must never
    include `account_key` -- an operator's real account identity
    (an email address in practice, see app.core.config's own account_key
    docstrings). Before the fix, `automation_scheduler_started` logged
    `account_key=%s` directly.
    """

    def test_startup_log_never_contains_the_configured_account_key(self, monkeypatch, caplog):
        fake_session_local = _FakeSessionLocal()
        monkeypatch.setattr("app.db.session.SessionLocal", fake_session_local)

        started = asyncio.Event()

        async def _fake_run_due_cycle_if_claimed(db, *, account_key, settings):
            started.set()
            return True

        monkeypatch.setattr(
            "app.scheduler.run_due_cycle_if_claimed", _fake_run_due_cycle_if_claimed
        )

        secret_account_key = "vitalikmalkov003@example.com"

        async def _drive():
            task = asyncio.create_task(
                _poll_loop(_fake_settings(automation_scheduler_account_key=secret_account_key))
            )
            await _cancel_after(task, started)

        with caplog.at_level("DEBUG"):
            asyncio.run(_drive())

        assert secret_account_key not in caplog.text
        assert "account_key" not in caplog.text
        # Non-identifying operational fields must still be logged -- this
        # is a privacy fix, not a removal of useful startup observability.
        assert "automation_scheduler_started" in caplog.text
        assert "interval_seconds" in caplog.text
        assert "poll_seconds" in caplog.text
