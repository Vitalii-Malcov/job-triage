"""Audit ARC-02: a failed Telegram bot start is retried instead of final (no real Telegram)."""

import asyncio

import pytest

from app.core.config import Settings
from app.services import telegram_bot as bot


class _FakeApplication:
    def __init__(self):
        self.stopped = False


@pytest.fixture()
def patched(monkeypatch):
    state = {"attempts": 0, "fail_first": 2, "stopped": [], "sleeps": []}

    async def fake_start_bot(settings):
        state["attempts"] += 1
        if state["attempts"] <= state["fail_first"]:
            return None, "NetworkError"
        return _FakeApplication(), None

    async def fake_stop_bot(application):
        state["stopped"].append(application)

    async def fake_sleep(seconds):
        state["sleeps"].append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(bot, "_start_bot_attempt", fake_start_bot)
    monkeypatch.setattr(bot, "stop_bot", fake_stop_bot)
    state["sleep"] = fake_sleep
    return state


@pytest.mark.asyncio
async def test_no_token_means_no_task():
    assert await bot.start_bot_supervised(Settings(_env_file=None, telegram_bot_token="")) is None


@pytest.mark.asyncio
async def test_a_failed_start_is_retried_with_backoff_until_it_succeeds(patched):
    settings = Settings(_env_file=None, telegram_bot_enabled=True, telegram_bot_token="test-token")
    task = asyncio.create_task(bot._run_bot_supervised(settings, patched["sleep"]))
    for _ in range(20):
        await asyncio.sleep(0)
    assert patched["attempts"] == 3
    assert patched["sleeps"] == [5.0, 10.0]  # exponential, then it started
    assert not task.done()  # the running bot is held until shutdown
    await bot.stop_bot_supervised(task)
    assert len(patched["stopped"]) == 1  # the started application is stopped on shutdown


@pytest.mark.asyncio
async def test_backoff_is_capped(patched):
    patched["fail_first"] = 12
    settings = Settings(_env_file=None, telegram_bot_enabled=True, telegram_bot_token="test-token")
    task = asyncio.create_task(bot._run_bot_supervised(settings, patched["sleep"]))
    for _ in range(80):
        await asyncio.sleep(0)
    await bot.stop_bot_supervised(task)
    assert max(patched["sleeps"]) == bot.BOT_START_RETRY_MAX_SECONDS
    assert patched["sleeps"][:7] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0]


@pytest.mark.asyncio
async def test_shutdown_while_still_retrying_cancels_cleanly(patched):
    patched["fail_first"] = 10**6
    settings = Settings(_env_file=None, telegram_bot_enabled=True, telegram_bot_token="test-token")
    task = asyncio.create_task(bot._run_bot_supervised(settings, patched["sleep"]))
    for _ in range(5):
        await asyncio.sleep(0)
    await bot.stop_bot_supervised(task)  # must not raise
    assert task.cancelled() or task.done()
    assert patched["stopped"] == []


@pytest.mark.asyncio
async def test_stop_is_a_noop_for_none():
    await bot.stop_bot_supervised(None)


def test_main_lifespan_uses_the_supervised_variants():
    import app.main as main

    assert main.start_bot is bot.start_bot_supervised
    assert main.stop_bot is bot.stop_bot_supervised


# ---- lifecycle unwinding (no network: fake Application objects) -------------------------


class _FakeUpdater:
    def __init__(self, owner):
        self.owner = owner
        self.running = False

    async def start_polling(self):
        if self.owner.fail_at == "polling":
            raise RuntimeError("polling failed")
        self.running = True

    async def stop(self):
        self.running = False
        self.owner.log.append("updater_stop")


class _FakeFullApplication:
    def __init__(self, fail_at=None, block_at=None):
        self.fail_at = fail_at
        self.block_at = block_at
        self.running = False
        self.initialized = False
        self.log = []
        self.updater = _FakeUpdater(self)
        self.bot = type("B", (), {"_request": ()})()

    async def initialize(self):
        if self.block_at == "initialize":
            await asyncio.Event().wait()
        if self.fail_at == "initialize":
            raise RuntimeError("init failed")
        self.initialized = True

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False
        self.log.append("stop")

    async def shutdown(self):
        if self.running:
            raise RuntimeError("This Application is still running!")
        self.log.append("shutdown")


def _settings():
    return Settings(
        _env_file=None,
        telegram_bot_enabled=True,
        telegram_bot_token="test-token",
        telegram_chat_id="1",
    )


@pytest.mark.asyncio
async def test_a_failed_polling_start_unwinds_updater_application_and_shutdown(monkeypatch):
    fake = _FakeFullApplication(fail_at="polling")
    monkeypatch.setattr(bot, "build_application", lambda settings: fake)
    assert await bot.start_bot(_settings()) is None
    assert fake.log == ["stop", "shutdown"]  # `start()` had succeeded: it must be stopped first
    assert fake.running is False


@pytest.mark.asyncio
async def test_a_failed_initialize_only_shuts_down(monkeypatch):
    fake = _FakeFullApplication(fail_at="initialize")
    monkeypatch.setattr(bot, "build_application", lambda settings: fake)
    assert await bot.start_bot(_settings()) is None
    assert fake.log == ["shutdown"]


@pytest.mark.asyncio
async def test_cancellation_during_startup_still_unwinds(monkeypatch):
    fake = _FakeFullApplication(block_at="initialize")
    monkeypatch.setattr(bot, "build_application", lambda settings: fake)
    task = asyncio.create_task(bot.start_bot(_settings()))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.log == ["shutdown"]


@pytest.mark.asyncio
async def test_a_supervisor_crash_in_build_application_is_retried_not_fatal(monkeypatch):
    calls = {"n": 0}
    good = _FakeFullApplication()

    def build(settings):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError("bad SSL_CERT_FILE")
        return good

    async def fake_sleep(_seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(bot, "build_application", build)
    task = asyncio.create_task(bot._run_bot_supervised(_settings(), fake_sleep))
    for _ in range(30):
        await asyncio.sleep(0)
    assert calls["n"] == 2 and good.updater.running is True  # second attempt started the bot
    await bot.stop_bot_supervised(task)
    assert good.log[-2:] == ["stop", "shutdown"]


@pytest.mark.asyncio
async def test_an_invalid_token_is_terminal_not_retried_forever(monkeypatch):
    attempts = {"n": 0}

    async def fake_start_bot(settings):
        attempts["n"] += 1
        return None, "InvalidToken"

    async def fake_sleep(_seconds):
        raise AssertionError("must not sleep/retry after InvalidToken")

    monkeypatch.setattr(bot, "_start_bot_attempt", fake_start_bot)
    await bot._run_bot_supervised(_settings(), fake_sleep)  # returns by itself
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_a_crashed_supervisor_is_reported_at_once(caplog):
    async def boom():
        raise OSError("unexpected")

    task = asyncio.create_task(boom())
    task.add_done_callback(bot._log_supervisor_end)
    with pytest.raises(OSError):
        await task
    await asyncio.sleep(0)
    assert any(
        "telegram_bot_supervisor_crashed error_type=OSError" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_teardown_runs_to_the_end_even_when_cancelled_repeatedly(monkeypatch):
    fake = _FakeFullApplication(block_at="initialize")
    release = asyncio.Event()
    original_shutdown = fake.shutdown

    async def slow_shutdown():
        await release.wait()
        await original_shutdown()

    fake.shutdown = slow_shutdown
    monkeypatch.setattr(bot, "build_application", lambda settings: fake)
    task = asyncio.create_task(bot.start_bot(_settings()))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()  # during initialize -> teardown starts and blocks in shutdown
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()  # a second cancellation while the cleanup is in flight
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()  # still waiting for the cleanup instead of abandoning it
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.log == ["shutdown"]


@pytest.mark.asyncio
async def test_teardown_also_closes_the_request_clients(monkeypatch):
    closed = []

    class _Request:
        def __init__(self, name):
            self.name = name

        async def shutdown(self):
            closed.append(self.name)

    fake = _FakeFullApplication(fail_at="initialize")
    fake.bot = type("B", (), {"_request": (_Request("updates"), _Request("general"))})()
    monkeypatch.setattr(bot, "build_application", lambda settings: fake)
    assert await bot.start_bot(_settings()) is None
    assert closed == ["updates", "general"]
