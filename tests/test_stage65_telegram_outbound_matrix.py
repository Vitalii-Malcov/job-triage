"""Stage 65: no Telegram outbound path is enabled by token + chat id alone.

Credentials are present everywhere below; every send primitive is a recorder, so no network
call and no real message is possible.
"""

import pathlib
import re

import pytest

from app.core.config import Settings
from app.services import collector_runner
from app.services import telegram as tg
from app.services.telegram import TelegramNotifier, job_alert_credentials
from app.services.telegram_operator_control import CardSendCode, send_card

TOKEN = "123456:sentinel-token"


def _settings(**kw):
    return Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_chat_id="42", **kw)


class _Job:
    title = "Junior Python Developer"
    company = "Example GmbH"
    location = "Frankfurt"
    url = "https://example.invalid/job/1"


class _Score:
    score = 90
    recommendation = "APPLY"


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send(token, chat_id, text, **kw):
        calls.append((chat_id, text))
        return tg.TelegramSendOutcome.SENT

    monkeypatch.setattr(tg, "send_telegram_text", fake_send)
    return calls


def test_job_alert_flag_defaults_false():
    assert Settings(_env_file=None).telegram_job_alerts_enabled is False


def test_credentials_helper_is_blank_unless_flag_true():
    assert job_alert_credentials(_settings()) == ("", "")
    assert job_alert_credentials(_settings(telegram_job_alerts_enabled=False)) == ("", "")
    assert job_alert_credentials(_settings(telegram_job_alerts_enabled=True)) == (TOKEN, "42")


@pytest.mark.asyncio
async def test_job_alert_not_sent_with_credentials_but_no_flag(sent):
    token, chat = job_alert_credentials(_settings())
    notifier = TelegramNotifier(bot_token=token, chat_id=chat)
    assert notifier.enabled is False
    assert await notifier.send_job(_Job(), _Score()) is False
    assert sent == []


@pytest.mark.asyncio
async def test_job_alert_sent_to_mock_only_with_flag(sent):
    token, chat = job_alert_credentials(_settings(telegram_job_alerts_enabled=True))
    notifier = TelegramNotifier(bot_token=token, chat_id=chat)
    assert await notifier.send_job(_Job(), _Score()) is True
    assert [c for c, _ in sent] == ["42"]


def test_every_notifier_construction_uses_the_gated_credentials():
    """No call site may build the legacy notifier from the raw token/chat settings."""
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    found = 0
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"TelegramNotifier\(", text):
            if path.name == "telegram.py":
                continue
            found += 1
            window = text[match.end() : match.end() + 200]
            assert "job_alert_credentials(settings)" in window, f"{path}: ungated notifier"
            assert "reveal(settings.telegram_bot_token)" not in window
    assert found >= 3


@pytest.mark.asyncio
async def test_vacancy_feed_sweep_is_a_noop_when_flag_false(monkeypatch):
    called = []

    async def fake_deliver(*a, **k):
        called.append(True)

    monkeypatch.setattr(collector_runner, "deliver_queued_vacancy_cards", fake_deliver)
    await collector_runner._deliver_vacancy_feed(None, _settings(), is_lease_lost=None)
    assert called == []


@pytest.mark.asyncio
async def test_operator_card_is_disabled_when_flag_false():
    result = await send_card(None, _settings(), 1, None)
    assert result.code is CardSendCode.DISABLED


@pytest.mark.parametrize(
    "flag",
    [
        "telegram_bot_enabled",
        "telegram_job_alerts_enabled",
        "telegram_vacancy_feed_enabled",
        "telegram_bewerbung_draft_enabled",
        "telegram_bewerbung_approval_enabled",
        "telegram_gmail_draft_enabled",
        "telegram_operator_control_enabled",
        "telegram_daily_digest_enabled",
        "outbound_sending_enabled",
        "automation_scheduler_enabled",
        "automation_auto_prepare_enabled",
    ],
)
def test_credentials_never_turn_an_outbound_flag_on(flag):
    assert getattr(_settings(), flag) is False
