"""AUD-002: httpx (and python-telegram-bot, which sends its own requests
through an internal httpx.AsyncClient) logs every request at INFO level
as `HTTP Request: %s %s "%s %d %s"`, embedding the FULL request URL --
for a Telegram Bot API call, that URL contains the bot token itself
(https://api.telegram.org/bot<TOKEN>/sendMessage). These tests exercise
the REAL configured logging pipeline (app.core.logging.configure_logging)
and httpx's own real internal logging call (via httpx.MockTransport, so
no actual network I/O happens) with a distinctive fake token, proving the
token never reaches the log output.
"""

import asyncio
import io
import logging

import httpx

from app.core.logging import configure_logging

FAKE_TOKEN = "ASTRA-R1-DISTINCTIVE-FAKE-TOKEN-99182"


def _do_mock_request(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def _post() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post(url, json={"chat_id": "123", "text": "hi"})

    asyncio.run(_post())


def test_configure_logging_suppresses_httpx_request_log_leak():
    """The real regression guard: after configure_logging() runs (as it
    does at process startup -- see app.scheduler.main /
    app.main's lifespan), a real httpx request through the same code
    path python-telegram-bot/app.services.telegram use must never write
    the token-embedding URL to the configured log stream."""
    configure_logging()
    root = logging.getLogger()
    assert root.handlers, "configure_logging() must install a handler on the root logger"

    stream = io.StringIO()
    original_stream = root.handlers[0].stream
    root.handlers[0].stream = stream
    try:
        _do_mock_request(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage")
    finally:
        root.handlers[0].stream = original_stream

    output = stream.getvalue()
    assert FAKE_TOKEN not in output


def test_configure_logging_raises_httpx_and_httpcore_logger_levels():
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_sanity_check_the_leak_is_real_without_the_fix():
    """Proves the two tests above are a meaningful regression guard, not
    a vacuously-passing check: with the httpx logger level forced back
    down to INFO (simulating the pre-fix state), the SAME real httpx
    request DOES write the token into the log stream."""
    configure_logging()
    root = logging.getLogger()
    stream = io.StringIO()
    original_stream = root.handlers[0].stream
    root.handlers[0].stream = stream

    httpx_logger = logging.getLogger("httpx")
    original_level = httpx_logger.level
    httpx_logger.setLevel(logging.INFO)
    try:
        _do_mock_request(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage")
    finally:
        root.handlers[0].stream = original_stream
        httpx_logger.setLevel(original_level)

    assert FAKE_TOKEN in stream.getvalue()
