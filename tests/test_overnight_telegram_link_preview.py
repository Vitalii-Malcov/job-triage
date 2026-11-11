"""SEC-04 regression: every Telegram message disables server-side link previews.

Telegram fetches URLs in message text to build previews. Job links may be per-recipient
tracking redirects, so a fetch could produce a recruiter view-notification.
"""

import asyncio
import json

import httpx

from app.services.telegram import send_telegram_message, send_telegram_text


def _capture(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


def test_send_message_disables_link_preview(monkeypatch):
    seen = _capture(monkeypatch)
    asyncio.run(send_telegram_message("123:token", "42", "https://www.xing.com/jobs/x?track=abc"))
    assert seen[0]["link_preview_options"] == {"is_disabled": True}
    assert "parse_mode" not in seen[0]


def test_send_text_disables_link_preview(monkeypatch):
    seen = _capture(monkeypatch)
    asyncio.run(send_telegram_text("123:token", "42", "https://example.test/job"))
    assert seen[0]["link_preview_options"] == {"is_disabled": True}


def test_sdk_application_disables_link_previews_by_default():
    """`reply_text` / `send_message` / `edit_message_text` in telegram_bot.py use the SDK, not
    send_telegram_message; the application-wide default must disable previews too."""
    from app.core.config import Settings
    from app.services.telegram_bot import build_application

    app = build_application(Settings(telegram_bot_token="123456:ABCdef", _env_file=None))
    opts = app.bot.defaults.link_preview_options
    assert opts is not None and opts.is_disabled is True
