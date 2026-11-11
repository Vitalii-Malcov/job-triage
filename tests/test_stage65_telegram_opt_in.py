"""Stage 65: the Telegram bot starts only with TELEGRAM_BOT_ENABLED=true AND credentials.

Mock-only: `build_application` is replaced by a recorder, so no Telegram client, token check or
network call ever happens.
"""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.startup_diagnostics import startup_safety_fields
from app.services import telegram_bot as bot
from scripts import run_operator_ui, stage22_refresh_ba

TOKEN = "123456:sentinel-token"


class _FakeUpdater:
    running = True

    async def start_polling(self):
        return None

    async def stop(self):
        self.running = False


class _FakeApp:
    def __init__(self):
        self.updater = _FakeUpdater()
        self.running = True

    async def initialize(self):
        return None

    async def start(self):
        return None

    async def stop(self):
        self.running = False

    async def shutdown(self):
        return None


@pytest.fixture
def built(monkeypatch):
    calls = []

    def fake_build(settings):
        calls.append(True)
        return _FakeApp()

    monkeypatch.setattr(bot, "build_application", fake_build)
    return calls


def _settings(**kw):
    return Settings(_env_file=None, **kw)


def test_flag_defaults_to_false():
    assert _settings().telegram_bot_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"telegram_bot_enabled": False}])
async def test_token_set_flag_absent_or_false_does_not_start(built, extra):
    settings = _settings(telegram_bot_token=TOKEN, telegram_chat_id="42", **extra)
    assert await bot.start_bot_supervised(settings) is None
    assert await bot.start_bot(settings) is None
    assert built == []


@pytest.mark.asyncio
async def test_token_set_flag_true_starts_mock_bot(built):
    settings = _settings(telegram_bot_enabled=True, telegram_bot_token=TOKEN, telegram_chat_id="42")
    application = await bot.start_bot(settings)
    assert application is not None
    assert built == [True]


@pytest.mark.asyncio
async def test_supervised_start_with_flag_true_runs_a_task(built):
    settings = _settings(telegram_bot_enabled=True, telegram_bot_token=TOKEN, telegram_chat_id="42")
    task = await bot.start_bot_supervised(settings)
    try:
        assert task is not None
        await asyncio.sleep(0.05)
        assert built == [True]
    finally:
        await bot.stop_bot_supervised(task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw",
    [
        {"telegram_chat_id": "42"},  # token missing
        {"telegram_bot_token": TOKEN},  # chat id missing
    ],
)
async def test_flag_true_without_credentials_refuses_without_leaking(built, caplog, kw):
    settings = _settings(telegram_bot_enabled=True, **kw)
    with caplog.at_level(logging.INFO):
        assert await bot.start_bot_supervised(settings) is None
        assert await bot.start_bot(settings) is None
    assert built == []
    assert TOKEN not in caplog.text


def test_block_reasons_are_secret_free():
    assert bot.bot_start_block_reason(_settings(telegram_bot_token=TOKEN)) == "bot_not_enabled"
    assert (
        bot.bot_start_block_reason(_settings(telegram_bot_enabled=True))
        == "no_bot_token_configured"
    )
    assert (
        bot.bot_start_block_reason(_settings(telegram_bot_enabled=True, telegram_bot_token=TOKEN))
        == "no_chat_id_configured"
    )
    assert (
        bot.bot_start_block_reason(
            _settings(telegram_bot_enabled=True, telegram_bot_token=TOKEN, telegram_chat_id="1")
        )
        is None
    )


@pytest.mark.asyncio
async def test_start_failure_in_supervisor_sends_no_message(monkeypatch):
    sent = []

    def failing_build(settings):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "build_application", failing_build)
    monkeypatch.setattr(
        "app.services.telegram.TelegramNotifier.send_message",
        lambda *a, **k: sent.append(1),
        raising=False,
    )
    settings = _settings(telegram_bot_enabled=True, telegram_bot_token=TOKEN, telegram_chat_id="42")
    task = await bot.start_bot_supervised(settings)
    try:
        await asyncio.sleep(0.05)  # attempt fails and the supervisor schedules a quiet retry
    finally:
        await bot.stop_bot_supervised(task)
    assert sent == []


def test_startup_diagnostics_show_flag_not_values():
    fields = startup_safety_fields(_settings(telegram_bot_token=TOKEN, telegram_chat_id="42"))
    assert fields["telegram_bot_enabled"] == "false"
    assert TOKEN not in "".join(fields.values())


def test_manual_ui_and_discovery_script_force_the_flag_off():
    assert run_operator_ui.FORCED_ENV["TELEGRAM_BOT_ENABLED"] == "false"
    assert "TELEGRAM_BOT_ENABLED" in stage22_refresh_ba.FORCED_OFF_FLAGS


@pytest.fixture
def web_env(monkeypatch, tmp_path):
    for name, value in {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'web.db').as_posix()}",
        "DATABASE_ROLE": "test",
        "SINGLE_PROCESS_LOCK_ENABLED": "false",
        "API_KEY": "x" * 40,
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_CHAT_ID": "42",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TELEGRAM_BOT_ENABLED", raising=False)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _run_web(monkeypatch):
    started = []

    def fake_build(settings):
        started.append(True)
        raise RuntimeError("fake bot refuses to start")

    monkeypatch.setattr(bot, "build_application", fake_build)
    from app.main import app

    with TestClient(app, base_url="http://localhost"):
        pass
    return started


def test_uvicorn_style_default_startup_does_not_start_bot(web_env):
    assert _run_web(web_env) == []


def test_docker_style_default_env_does_not_start_bot(web_env):
    # the Docker image sets no TELEGRAM_BOT_ENABLED; same lifespan as uvicorn
    web_env.delenv("TELEGRAM_BOT_ENABLED", raising=False)
    assert _run_web(web_env) == []


def test_explicit_flag_true_reaches_the_mock_bot_via_lifespan(web_env):
    web_env.setenv("TELEGRAM_BOT_ENABLED", "true")
    get_settings.cache_clear()
    assert _run_web(web_env) == [True]


def test_manual_ui_env_does_not_start_even_with_flag_in_environment(web_env):
    web_env.setenv("TELEGRAM_BOT_ENABLED", "true")
    for name, value in run_operator_ui.FORCED_ENV.items():
        web_env.setenv(name, value)
    get_settings.cache_clear()
    assert _run_web(web_env) == []
