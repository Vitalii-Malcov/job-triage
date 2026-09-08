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

    @pytest.mark.asyncio
    async def test_connect_timeout_is_failed_not_uncertain(self, monkeypatch):
        """A timeout while still establishing the connection (never
        reached the point of sending request bytes) is provably
        undelivered -- FAILED, safe to retry."""
        _patch_client(monkeypatch, httpx.ConnectTimeout("connect timed out"))

        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")

        assert outcome is TelegramSendOutcome.FAILED

    @pytest.mark.asyncio
    async def test_pool_timeout_is_failed_not_uncertain(self, monkeypatch):
        """A timeout waiting for a pooled connection -- never even got
        as far as connecting, let alone sending. Provably undelivered."""
        _patch_client(monkeypatch, httpx.PoolTimeout("pool timed out"))

        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")

        assert outcome is TelegramSendOutcome.FAILED


class TestAdversarialAmbiguousOutcomesAreUncertain:
    """Codex Stage 8E BLOCKER (OUTBOUND UNCERTAINTY): every network
    failure that occurs AFTER a connection was already established must
    be UNCERTAIN, never FAILED -- the request may have been partially
    or fully transmitted (and possibly already processed by Telegram)
    before the failure surfaced. Classifying any of these FAILED would
    let the daily digest's CAS retry a send that may have already
    landed, risking a real duplicate message.
    """

    @pytest.mark.asyncio
    async def test_read_timeout_is_uncertain(self, monkeypatch):
        _patch_client(monkeypatch, httpx.ReadTimeout("read timed out"))
        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")
        assert outcome is TelegramSendOutcome.UNCERTAIN

    @pytest.mark.asyncio
    async def test_write_timeout_is_uncertain(self, monkeypatch):
        """A timeout while WRITING the request -- some or all request
        bytes may already have left the client."""
        _patch_client(monkeypatch, httpx.WriteTimeout("write timed out"))
        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")
        assert outcome is TelegramSendOutcome.UNCERTAIN

    @pytest.mark.asyncio
    async def test_read_error_is_uncertain_not_failed(self, monkeypatch):
        """The connection was established and the request may have been
        sent; the failure happened while reading the response. Whether
        Telegram processed the request cannot be disproven."""
        _patch_client(monkeypatch, httpx.ReadError("connection reset while reading"))
        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")
        assert outcome is TelegramSendOutcome.UNCERTAIN

    @pytest.mark.asyncio
    async def test_write_error_is_uncertain_not_failed(self, monkeypatch):
        """The connection was established; the failure happened while
        WRITING the request, so it may have been partially transmitted
        and partially processed server-side."""
        _patch_client(monkeypatch, httpx.WriteError("connection reset while writing"))
        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")
        assert outcome is TelegramSendOutcome.UNCERTAIN

    @pytest.mark.asyncio
    async def test_remote_protocol_error_is_uncertain_not_failed(self, monkeypatch):
        """The server sent back a malformed/unexpected response --
        meaning Telegram DID receive and act on the request; we simply
        couldn't parse what came back. Must never be classified FAILED
        (that would imply "safe to retry", which here risks a real
        duplicate send)."""
        _patch_client(monkeypatch, httpx.RemoteProtocolError("malformed response"))
        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")
        assert outcome is TelegramSendOutcome.UNCERTAIN

    @pytest.mark.asyncio
    async def test_generic_transport_error_is_uncertain_not_failed(self, monkeypatch):
        """A generic httpx.TransportError not otherwise special-cased --
        the conservative default per send_telegram_text's classification
        rule ("if we cannot prove Telegram did NOT receive/process the
        request, the outcome is UNCERTAIN") must apply, not FAILED."""
        _patch_client(monkeypatch, httpx.TransportError("unspecified transport failure"))
        outcome = await send_telegram_text(BOT_TOKEN, CHAT_ID, "hello")
        assert outcome is TelegramSendOutcome.UNCERTAIN

    @pytest.mark.asyncio
    async def test_generic_request_error_is_uncertain_not_failed(self, monkeypatch):
        _patch_client(monkeypatch, httpx.RequestError("unspecified request failure"))
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
            httpx.WriteError("connection reset while writing"),
            httpx.RemoteProtocolError("malformed response"),
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
