"""Stage 8E tests for app.services.telegram.send_telegram_text -- outcome
classification (SENT/FAILED/UNCERTAIN) and the hardening invariant that
the bot token, the request URL, and the message text are never logged.
"""

import httpx
import pytest

from app.services.telegram import TelegramNotifier, TelegramSendOutcome, send_telegram_text

BOT_TOKEN = "123456:super-secret-token-value"  # nosec: test fixture, not real
CHAT_ID = "999"


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient -- either returns a fixed
    httpx.Response (so real Response.raise_for_status() semantics apply
    unmodified) or raises a fixed exception from `post`.
    """

    def __init__(self, outcome, **_kwargs):
        self._outcome = outcome

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _patch_client(monkeypatch, outcome):
    monkeypatch.setattr(
        "app.services.telegram.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(outcome, **kwargs),
    )


def _ok_response() -> httpx.Response:
    return httpx.Response(200, request=httpx.Request("POST", "https://api.telegram.org/x"))


def _rejected_response() -> httpx.Response:
    return httpx.Response(401, request=httpx.Request("POST", "https://api.telegram.org/x"))


class TestOutcomeClassification:
    @pytest.mark.asyncio
    async def test_2xx_response_is_sent(self, monkeypatch):
        _patch_client(monkeypatch, _ok_response())

        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")

        assert outcome is TelegramSendOutcome.SENT

    @pytest.mark.asyncio
    async def test_non_2xx_response_is_failed_not_uncertain(self, monkeypatch):
        _patch_client(monkeypatch, _rejected_response())

        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")

        assert outcome is TelegramSendOutcome.FAILED

    @pytest.mark.asyncio
    async def test_connection_level_error_is_failed_not_uncertain(self, monkeypatch):
        _patch_client(monkeypatch, httpx.ConnectError("connection refused"))

        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")

        assert outcome is TelegramSendOutcome.FAILED

    @pytest.mark.asyncio
    async def test_timeout_is_uncertain_not_failed(self, monkeypatch):
        """A timeout while awaiting the response means the request may
        already have reached Telegram -- this must be UNCERTAIN, never
        FAILED, so callers (the daily digest) never automatically retry
        it and risk a duplicate send."""
        _patch_client(monkeypatch, httpx.ReadTimeout("timed out"))

        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")

        assert outcome is TelegramSendOutcome.UNCERTAIN


class TestNoSensitiveLogging:
    @pytest.mark.asyncio
    async def test_bot_token_never_logged_on_any_outcome(self, monkeypatch, caplog):
        for outcome_obj in (
            _ok_response(),
            _rejected_response(),
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("timed out"),
        ):
            _patch_client(monkeypatch, outcome_obj)
            with caplog.at_level("DEBUG"):
                await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")

        assert BOT_TOKEN not in caplog.text
        assert "api.telegram.org" not in caplog.text

    @pytest.mark.asyncio
    async def test_message_text_never_logged(self, monkeypatch, caplog):
        secret_text = "SECRET-MESSAGE-BODY-MUST-NOT-LEAK"
        _patch_client(monkeypatch, _ok_response())

        with caplog.at_level("DEBUG"):
            await send_telegram_text(BOT_TOKEN, CHAT_ID, secret_text)

        assert secret_text not in caplog.text

    @pytest.mark.asyncio
    async def test_raw_exception_message_never_logged_only_type(self, monkeypatch, caplog):
        _patch_client(monkeypatch, httpx.ConnectError("secret-upstream-detail-should-not-leak"))

        with caplog.at_level("DEBUG"):
            await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")

        assert "secret-upstream-detail-should-not-leak" not in caplog.text
        assert "ConnectError" in caplog.text


class TestTelegramNotifierStillWorks:
    """Regression: app.services.telegram.TelegramNotifier (pre-Stage-8E
    job-alert path) must keep working unchanged after being refactored
    onto the shared send_telegram_text helper."""

    @pytest.mark.asyncio
    async def test_disabled_notifier_is_non_fatal(self):
        from app.models.job import Job, JobScore

        notifier = TelegramNotifier("", "", max_retries=1)
        job = Job(source="test", title="Python", company="X", url="https://example.com")
        score = JobScore(score=90, recommendation="APPLY")
        assert await notifier.send_job(job, score) is False

    @pytest.mark.asyncio
    async def test_enabled_notifier_sends_successfully(self, monkeypatch):
        from app.models.job import Job, JobScore

        _patch_client(monkeypatch, _ok_response())
        notifier = TelegramNotifier(BOT_TOKEN, CHAT_ID, max_retries=1)
        job = Job(source="test", title="Python", company="X", url="https://example.com")
        score = JobScore(score=90, recommendation="APPLY")

        assert await notifier.send_job(job, score) is True
