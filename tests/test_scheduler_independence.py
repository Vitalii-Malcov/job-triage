"""Stage 8E: proves the automation cycle and the daily Telegram digest
are genuinely INDEPENDENT features inside `app.scheduler._poll_loop` and
`app.scheduler.main` -- each gated on its own settings flag, neither
implying or requiring the other. Directly monkeypatches
`app.scheduler.run_due_cycle_if_claimed`/`run_due_digest_if_claimed`/
`_poll_loop` (never real DB/network I/O) so each call count can be
asserted deterministically.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.scheduler import _poll_loop, main

ACCOUNT = "me@example.com"


def _fake_settings(**overrides) -> SimpleNamespace:
    defaults = {
        "automation_scheduler_account_key": ACCOUNT,
        "automation_scheduler_poll_seconds": 0.01,
        "automation_scheduler_interval_seconds": 3600,
        "automation_scheduler_enabled": False,
        "telegram_daily_digest_enabled": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def _cancel_after(task: asyncio.Task, event: asyncio.Event, timeout: float = 5.0) -> None:
    await asyncio.wait_for(event.wait(), timeout=timeout)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestPollLoopGating:
    def test_automation_only_never_triggers_digest(self, monkeypatch):
        automation_calls = 0
        digest_calls = 0
        ticked_twice = asyncio.Event()

        async def _fake_cycle(db, *, account_key, settings):
            nonlocal automation_calls
            automation_calls += 1
            if automation_calls >= 2:
                ticked_twice.set()
            return True

        async def _fake_digest(db, *, account_key, settings):
            nonlocal digest_calls
            digest_calls += 1
            return True

        monkeypatch.setattr("app.scheduler.run_due_cycle_if_claimed", _fake_cycle)
        monkeypatch.setattr("app.scheduler.run_due_digest_if_claimed", _fake_digest)

        class _FakeSession:
            def rollback(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr("app.db.session.SessionLocal", lambda: _FakeSession())

        async def _drive():
            settings = _fake_settings(automation_scheduler_enabled=True)
            task = asyncio.create_task(_poll_loop(settings))
            await _cancel_after(task, ticked_twice)

        asyncio.run(_drive())

        assert automation_calls >= 2
        assert digest_calls == 0

    def test_digest_only_never_triggers_automation(self, monkeypatch):
        automation_calls = 0
        digest_calls = 0
        ticked_twice = asyncio.Event()

        async def _fake_cycle(db, *, account_key, settings):
            nonlocal automation_calls
            automation_calls += 1
            return True

        async def _fake_digest(db, *, account_key, settings):
            nonlocal digest_calls
            digest_calls += 1
            if digest_calls >= 2:
                ticked_twice.set()
            return True

        monkeypatch.setattr("app.scheduler.run_due_cycle_if_claimed", _fake_cycle)
        monkeypatch.setattr("app.scheduler.run_due_digest_if_claimed", _fake_digest)

        class _FakeSession:
            def rollback(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr("app.db.session.SessionLocal", lambda: _FakeSession())

        async def _drive():
            settings = _fake_settings(telegram_daily_digest_enabled=True)
            task = asyncio.create_task(_poll_loop(settings))
            await _cancel_after(task, ticked_twice)

        asyncio.run(_drive())

        assert digest_calls >= 2
        assert automation_calls == 0

    def test_both_enabled_both_run_every_tick(self, monkeypatch):
        automation_calls = 0
        digest_calls = 0
        both_ticked = asyncio.Event()

        async def _fake_cycle(db, *, account_key, settings):
            nonlocal automation_calls
            automation_calls += 1
            return True

        async def _fake_digest(db, *, account_key, settings):
            nonlocal digest_calls
            digest_calls += 1
            if digest_calls >= 2 and automation_calls >= 2:
                both_ticked.set()
            return True

        monkeypatch.setattr("app.scheduler.run_due_cycle_if_claimed", _fake_cycle)
        monkeypatch.setattr("app.scheduler.run_due_digest_if_claimed", _fake_digest)

        class _FakeSession:
            def rollback(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr("app.db.session.SessionLocal", lambda: _FakeSession())

        async def _drive():
            settings = _fake_settings(
                automation_scheduler_enabled=True, telegram_daily_digest_enabled=True
            )
            task = asyncio.create_task(_poll_loop(settings))
            await _cancel_after(task, both_ticked)

        asyncio.run(_drive())

        assert automation_calls >= 2
        assert digest_calls >= 2

    def test_a_failure_in_automation_does_not_prevent_digest_this_same_tick(self, monkeypatch):
        digest_calls = 0
        digest_ran = asyncio.Event()

        async def _boom(db, *, account_key, settings):
            raise RuntimeError("secret-should-never-leak")

        async def _fake_digest(db, *, account_key, settings):
            nonlocal digest_calls
            digest_calls += 1
            digest_ran.set()
            return True

        monkeypatch.setattr("app.scheduler.run_due_cycle_if_claimed", _boom)
        monkeypatch.setattr("app.scheduler.run_due_digest_if_claimed", _fake_digest)

        class _FakeSession:
            def rollback(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr("app.db.session.SessionLocal", lambda: _FakeSession())

        async def _drive():
            settings = _fake_settings(
                automation_scheduler_enabled=True, telegram_daily_digest_enabled=True
            )
            task = asyncio.create_task(_poll_loop(settings))
            await _cancel_after(task, digest_ran)

        asyncio.run(_drive())

        assert digest_calls >= 1


class TestMainEntrypointGating:
    def test_both_disabled_exits_cleanly_without_starting_loop(self, monkeypatch, capsys):
        settings = Settings(automation_scheduler_enabled=False, telegram_daily_digest_enabled=False)
        monkeypatch.setattr("app.scheduler.get_settings", lambda: settings)

        loop_started = False

        async def _fake_poll_loop(_settings):
            nonlocal loop_started
            loop_started = True

        monkeypatch.setattr("app.scheduler._poll_loop", _fake_poll_loop)

        exit_code = main()

        assert exit_code == 0
        assert loop_started is False
        assert "disabled" in capsys.readouterr().out.lower()

    def test_digest_only_enabled_starts_the_loop_without_automation(self, monkeypatch):
        settings = Settings(
            automation_scheduler_enabled=False,
            telegram_daily_digest_enabled=True,
            automation_scheduler_account_key=ACCOUNT,
            telegram_daily_digest_hour=8,
            telegram_daily_digest_timezone="Europe/Berlin",
            telegram_bot_token="tok",
            telegram_chat_id="1",
        )
        monkeypatch.setattr("app.scheduler.get_settings", lambda: settings)

        captured = {}

        async def _fake_poll_loop(passed_settings):
            captured["settings"] = passed_settings

        monkeypatch.setattr("app.scheduler._poll_loop", _fake_poll_loop)

        exit_code = main()

        assert exit_code == 0
        assert captured.get("settings") is settings

    def test_automation_only_enabled_starts_the_loop_without_digest(self, monkeypatch):
        settings = Settings(
            automation_scheduler_enabled=True,
            automation_scheduler_account_key=ACCOUNT,
            telegram_daily_digest_enabled=False,
        )
        monkeypatch.setattr("app.scheduler.get_settings", lambda: settings)

        captured = {}

        async def _fake_poll_loop(passed_settings):
            captured["settings"] = passed_settings

        monkeypatch.setattr("app.scheduler._poll_loop", _fake_poll_loop)

        exit_code = main()

        assert exit_code == 0
        assert captured.get("settings") is settings
