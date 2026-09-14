"""DEPLOY-002 (Codex master review): `python -m app.scheduler` used to
exit `0` immediately whenever both `automation_scheduler_enabled` and
`telegram_daily_digest_enabled` were false. Combined with `compose.yaml`'s
`restart: unless-stopped` (needed so a deliberately-enabled scheduler
resumes after a Docker daemon/host restart, which `on-failure` never
does), that clean exit restart-looped the container forever whenever the
scheduler was disabled.

The fix: a disabled scheduler now idles in `_idle_forever` instead of
returning immediately -- these tests prove (1) `_idle_forever` never
returns on its own (only via cancellation/shutdown), and (2) `main()`'s
disabled branch actually drives it through the same shutdown path the
real poll loop uses, still returning `0` on a clean SIGINT/SIGTERM-style
stop, and never touches `_poll_loop` while disabled.
"""

import asyncio

import pytest

from app.scheduler import _idle_forever, _ShutdownRequested, main


class TestIdleForeverNeverReturnsOnItsOwn:
    def test_idle_forever_is_still_running_after_multiple_scheduler_ticks(self):
        """Proves this is a genuine block-forever loop, not something that
        happens to return quickly -- cooperatively yields control (so a
        real event loop can still service other tasks/signals) without
        ever completing on its own.
        """

        async def _run():
            task = asyncio.ensure_future(_idle_forever())
            for _ in range(50):
                await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_run())

    def test_idle_forever_propagates_cancellation_cleanly(self):
        async def _run():
            task = asyncio.ensure_future(_idle_forever())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_run())


class TestMainIdlesWhenDisabledInsteadOfExiting:
    """Regression coverage for the restart-loop bug: main()'s disabled
    branch must no longer return 0 the instant it's entered -- it must
    block (via _idle_forever) until a shutdown signal, exactly mirroring
    how the enabled/poll-loop branch already behaves.
    """

    def _disabled_settings(self):
        from app.core.config import Settings

        return Settings(automation_scheduler_enabled=False, telegram_daily_digest_enabled=False)

    def test_disabled_scheduler_calls_idle_forever_not_poll_loop(self, monkeypatch):
        poll_loop_called = False

        async def _fake_poll_loop(settings):
            nonlocal poll_loop_called
            poll_loop_called = True

        idle_forever_called = False

        async def _fake_idle_forever():
            nonlocal idle_forever_called
            idle_forever_called = True
            raise _ShutdownRequested()

        monkeypatch.setattr("app.scheduler.get_settings", lambda: self._disabled_settings())
        monkeypatch.setattr("app.scheduler._poll_loop", _fake_poll_loop)
        monkeypatch.setattr("app.scheduler._idle_forever", _fake_idle_forever)

        exit_code = main()

        assert idle_forever_called is True
        assert poll_loop_called is False
        assert exit_code == 0

    def test_disabled_scheduler_returns_zero_on_clean_shutdown_request(self, monkeypatch, capsys):
        async def _fake_idle_forever():
            raise _ShutdownRequested()

        monkeypatch.setattr("app.scheduler.get_settings", lambda: self._disabled_settings())
        monkeypatch.setattr("app.scheduler._idle_forever", _fake_idle_forever)

        exit_code = main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "idling until stopped" in captured.out

    def test_disabled_scheduler_returns_zero_on_keyboard_interrupt(self, monkeypatch):
        async def _fake_idle_forever():
            raise KeyboardInterrupt()

        monkeypatch.setattr("app.scheduler.get_settings", lambda: self._disabled_settings())
        monkeypatch.setattr("app.scheduler._idle_forever", _fake_idle_forever)

        exit_code = main()

        assert exit_code == 0

    def test_disabled_scheduler_does_not_return_immediately_without_a_shutdown_signal(
        self, monkeypatch
    ):
        """The actual restart-loop bug, reproduced directly: if main() ever
        goes back to returning as soon as it observes the disabled state
        (instead of blocking on _idle_forever until asked to stop), this
        test's real (non-faked) _idle_forever call would need to complete
        on its own for main() to return -- which it must never do, so this
        test bounds how long we're willing to wait and treats "still
        blocked" as the passing outcome, run in a thread so the test
        itself doesn't hang forever if the fix ever regresses.
        """
        import threading

        monkeypatch.setattr("app.scheduler.get_settings", lambda: self._disabled_settings())
        # signal.signal only works from the main thread of the main
        # interpreter -- main() is run off-thread below purely so this
        # test can bound how long it waits without hanging the suite, so
        # its (irrelevant to this test's assertion) SIGTERM registration
        # must be stubbed out here.
        monkeypatch.setattr("app.scheduler.signal.signal", lambda *args, **kwargs: None)

        result: dict = {}

        def _run_main():
            result["exit_code"] = main()

        thread = threading.Thread(target=_run_main, daemon=True)
        thread.start()
        thread.join(timeout=1.0)

        assert thread.is_alive(), (
            "main() returned on its own while disabled -- this is exactly the "
            "restart-loop bug DEPLOY-002 fixes (a disabled scheduler exiting 0 "
            "on its own, which restart: unless-stopped restart-loops forever)"
        )
        # Best-effort cleanup: nothing joins/kills this daemon thread beyond
        # process exit -- it is parked inside asyncio.sleep(3600) with no
        # side effects, so leaving it running for the rest of the test
        # session is harmless.
