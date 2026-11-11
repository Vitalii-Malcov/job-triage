"""Stage 64: a configured Telegram token / Gmail password is not an enabled sender.

Everything uses doubles: SMTP and IMAP constructors raise if touched, the Telegram bot is a
fake that only records that it was started. No real account, token or network is involved.

Matrix proved below (production-like config: Telegram token + chat and Gmail credentials
present, every feature flag false):

* Telegram bot: configured, no TELEGRAM_BOT_ENABLED flag (Stage 65); a plain web start does NOT
  start the bot.
* Gmail / SMTP: configured, not enabled; no mail connection is opened.
* Scheduler, auto-prepare, outbound: off.
"""

import imaplib
import smtplib

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from scripts import run_operator_ui

FLAGS = (
    "outbound_sending_enabled",
    "telegram_gmail_draft_enabled",
    "telegram_bewerbung_draft_enabled",
    "telegram_bewerbung_approval_enabled",
    "telegram_vacancy_feed_enabled",
    "telegram_daily_digest_enabled",
    "telegram_operator_control_enabled",
    "automation_scheduler_enabled",
    "automation_auto_prepare_enabled",
    "automation_gmail_cycle_enabled",
    "automation_follow_up_cycle_enabled",
    "company_research_auto_enabled",
    "alembic_auto_upgrade",
)

CONFIGURED = dict(
    telegram_bot_token="123456:sentinel-token",
    telegram_chat_id="42",
    gmail_username="operator@example.invalid",
    gmail_app_password="sentinel-app-password",
)


def test_configured_credentials_do_not_turn_any_flag_on():
    settings = Settings(_env_file=None, **CONFIGURED)
    assert {name: getattr(settings, name) for name in FLAGS} == dict.fromkeys(FLAGS, False)


@pytest.fixture
def web_env(monkeypatch, tmp_path):
    for name, value in {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'web.db').as_posix()}",
        "DATABASE_ROLE": "test",
        "SINGLE_PROCESS_LOCK_ENABLED": "false",
        "API_KEY": "x" * 40,
        "TELEGRAM_BOT_TOKEN": "123456:sentinel-token",
        "TELEGRAM_CHAT_ID": "42",
        "GMAIL_USERNAME": "operator@example.invalid",
        "GMAIL_APP_PASSWORD": "sentinel-app-password",
    }.items():
        monkeypatch.setenv(name, value)
    for name in FLAGS:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})

    def forbidden(*_a, **_k):
        raise AssertionError("a mail connection was opened")

    monkeypatch.setattr(smtplib.SMTP, "__init__", forbidden)
    monkeypatch.setattr(smtplib.SMTP, "connect", forbidden)
    monkeypatch.setattr(imaplib.IMAP4, "__init__", forbidden)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _start_web(monkeypatch):
    started = []

    class FakeApp:
        pass

    def fake_build(settings):
        started.append(True)
        raise RuntimeError("fake bot: refuse to start so nothing runs")

    monkeypatch.setattr("app.services.telegram_bot.build_application", fake_build)
    from app.main import app

    with TestClient(app, base_url="http://localhost"):
        pass
    return started


def test_a_plain_web_start_never_starts_the_bot_and_never_opens_mail(web_env):
    # Stage 65: token + chat present but no TELEGRAM_BOT_ENABLED -> no bot; mail untouched
    # (the SMTP/IMAP doubles above would have raised)
    assert _start_web(web_env) == []


def test_the_operator_launcher_blanks_the_token_so_the_bot_is_not_started(web_env):
    for name, value in run_operator_ui.FORCED_ENV.items():
        web_env.setenv(name, value)
    get_settings.cache_clear()
    assert _start_web(web_env) == []
