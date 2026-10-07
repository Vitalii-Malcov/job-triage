"""Stage 9D Telegram boundary: the `bm:` handler's gates (authorized
operator, all three flags, PRIVATE chat, strict parse -- all BEFORE any DB
or content access), the explicit "📧 Gmail-Entwurf erstellen" offer on Stage
9C approval notices, button policy (never "Senden", never create/retry for
UNCERTAIN), log privacy, and runtime outbound safety through the REAL
APPEND-only provider over a fake IMAP transport."""

import json
import logging
import smtplib
import urllib.request
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

import app.providers.email.imap_draft as imap_draft
import app.services.gmail_application_draft as gd
import app.services.telegram_bot as bot
from app.db.models import GmailApplicationDraftRecord
from app.providers.email.draft_base import DraftCreateOutcomeUnknownError, DraftLookupResult
from tests import test_gmail_application_draft as s9d
from tests.test_gmail_application_draft import (
    ACCOUNT,
    PASSWORD,
    FakeProvider,
    _settings,
)
from tests.test_gmail_draft_provider import ALLOWED_COMMANDS, FakeClient
from tests.test_telegram_bewerbung import STRANGER_CHAT_ID, _update
from tests.test_telegram_bewerbung_approval import MagicMockContext, _regenerate

network_guard = s9d.network_guard
requested = s9d.requested
seeded = s9d.seeded
sender = s9d.sender
session_factory = s9d.session_factory
approved = s9d.approved
clock = s9d.clock

CAP = "AbCdEfGh_-123456"


@pytest.fixture()
def fake_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(gd, "build_provider", lambda settings: provider)
    return provider


@pytest.fixture()
def bot_env(monkeypatch, session_factory, sender):
    state = {"settings": _settings()}
    monkeypatch.setattr(bot, "SessionLocal", session_factory)
    monkeypatch.setattr(bot, "get_settings", lambda: state["settings"])
    monkeypatch.setattr(bot, "send_telegram_message", sender)
    return state


def _ledgers(session_factory):
    db = session_factory()
    try:
        return list(db.scalars(select(GmailApplicationDraftRecord)))
    finally:
        db.close()


def _buttons(markup) -> list[dict]:
    if not markup:
        return []
    return [button for row in markup["inline_keyboard"] for button in row]


def _no_db(monkeypatch):
    """Prove a rejected press never reaches the service (DB/content)."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("service reached")

    monkeypatch.setattr(gd, "handle_create", forbidden)
    monkeypatch.setattr(gd, "handle_reconcile", forbidden)


class TestGates:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "flags",
        [
            {"telegram_bewerbung_draft_enabled": False},
            {"telegram_bewerbung_approval_enabled": False},
            {"telegram_gmail_draft_enabled": False},
        ],
    )
    async def test_each_flag_off_rejects_before_db(self, bot_env, monkeypatch, flags):
        bot_env["settings"] = _settings(**flags)
        _no_db(monkeypatch)
        for data in (f"bm:{CAP}:c", f"bm:{CAP}:r"):
            update_ = _update(data)
            await bot.on_gmail_draft_callback(update_, MagicMockContext())
            update_.callback_query.answer.assert_awaited_once_with(bot._GMAIL_DRAFT_DISABLED)

    @pytest.mark.asyncio
    async def test_outbound_sending_flag_does_not_enable_drafts(self, bot_env, monkeypatch):
        bot_env["settings"] = _settings(
            telegram_gmail_draft_enabled=False, outbound_sending_enabled=True
        )
        _no_db(monkeypatch)
        update_ = _update(f"bm:{CAP}:c")
        await bot.on_gmail_draft_callback(update_, MagicMockContext())
        update_.callback_query.answer.assert_awaited_once_with(bot._GMAIL_DRAFT_DISABLED)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
    async def test_non_private_chats_rejected_before_db(self, bot_env, monkeypatch, chat_type):
        _no_db(monkeypatch)
        update_ = _update(f"bm:{CAP}:c", chat_type=chat_type)
        await bot.on_gmail_draft_callback(update_, MagicMockContext())
        update_.callback_query.answer.assert_awaited_once_with(
            bot._PRIVATE_CHAT_ONLY, show_alert=True
        )

    @pytest.mark.asyncio
    async def test_unauthorized_and_missing_chat_get_silence(self, bot_env, monkeypatch):
        _no_db(monkeypatch)
        stranger = _update(f"bm:{CAP}:c", chat_id=STRANGER_CHAT_ID)
        missing = _update(f"bm:{CAP}:c")
        missing.effective_chat = None
        for update_ in (stranger, missing):
            await bot.on_gmail_draft_callback(update_, MagicMockContext())
            update_.callback_query.answer.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data",
        [
            "bm:",
            f"bm:{CAP}",
            f"bm:{CAP}:x",
            f"bm:{CAP}:c:extra",
            "bm:short:c",
            "bm:AbCdEfGh_-12345=:c",
            "bm:42:c",
            f"bz:{CAP}:f",
            f"BM:{CAP}:c",
        ],
    )
    async def test_malformed_callbacks_touch_nothing(self, bot_env, monkeypatch, data):
        _no_db(monkeypatch)
        update_ = _update(data)
        await bot.on_gmail_draft_callback(update_, MagicMockContext())
        update_.callback_query.answer.assert_awaited_once_with("Unbekannte Aktion.")

    def test_handler_is_registered_for_bm_only(self, monkeypatch):
        handlers = []

        class FakeApplication:
            def add_handler(self, handler):
                handlers.append(handler)

            def add_error_handler(self, handler):
                pass

        class FakeBuilder:
            def token(self, value):
                return self

            def build(self):
                return FakeApplication()

        monkeypatch.setattr(bot, "ApplicationBuilder", FakeBuilder)
        bot.build_application(_settings())
        patterns = [
            handler.pattern.pattern
            for handler in handlers
            if getattr(handler, "callback", None) is bot.on_gmail_draft_callback
        ]
        assert patterns == ["^bm:"]


class TestApprovalOffer:
    @pytest.mark.asyncio
    async def test_approval_notice_offers_the_create_button_only_with_all_flags(
        self, bot_env, session_factory, requested, sender
    ):
        cap = requested["capability"]
        await bot.on_review_decision_callback(_update(f"bz:{cap}:f"), MagicMockContext())
        notice = sender.calls[-1]
        assert "FREIGEGEBEN" in notice["text"]
        assert _buttons(notice["reply_markup"]) == [
            {"text": "📧 Gmail-Entwurf erstellen", "callback_data": f"bm:{cap}:c"}
        ]
        assert _ledgers(session_factory) == []  # never automatic

        bot_env["settings"] = _settings(telegram_gmail_draft_enabled=False)
        await bot.on_review_decision_callback(_update(f"bz:{cap}:f"), MagicMockContext())
        assert "Bereits FREIGEGEBEN" in sender.calls[-1]["text"]
        assert sender.calls[-1]["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_replaced_package_approval_offers_nothing(
        self, bot_env, session_factory, approved, sender
    ):
        await _regenerate(session_factory, approved)
        cap = approved["capability"]
        await bot.on_review_page_callback(_update(f"bv:{cap}:1"), MagicMockContext())
        assert "ersetzt" in sender.calls[-1]["text"]
        assert sender.calls[-1]["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_rejection_offers_nothing(self, bot_env, session_factory, requested, sender):
        cap = requested["capability"]
        await bot.on_review_decision_callback(_update(f"bz:{cap}:x"), MagicMockContext())
        assert sender.calls[-1]["reply_markup"] is None


class TestFlows:
    @pytest.mark.asyncio
    async def test_create_then_replay(
        self, bot_env, session_factory, approved, sender, fake_provider
    ):
        cap = approved["capability"]
        update_ = _update(f"bm:{cap}:c")
        await bot.on_gmail_draft_callback(update_, MagicMockContext())
        update_.callback_query.answer.assert_awaited_once()
        assert "NOCH NICHT GESENDET" in sender.calls[-1]["text"]
        assert sender.calls[-1]["reply_markup"] is None
        await bot.on_gmail_draft_callback(_update(f"bm:{cap}:c"), MagicMockContext())
        assert "bereits" in sender.calls[-1]["text"]
        assert len(fake_provider.creates) == 1

    @pytest.mark.asyncio
    async def test_uncertain_offers_only_the_recheck_button(
        self, bot_env, session_factory, approved, sender, fake_provider, clock
    ):
        cap = approved["capability"]
        fake_provider.create_exc = DraftCreateOutcomeUnknownError("x")
        await bot.on_gmail_draft_callback(_update(f"bm:{cap}:c"), MagicMockContext())
        assert _buttons(sender.calls[-1]["reply_markup"]) == [
            {"text": "🔄 Gmail-Status prüfen", "callback_data": f"bm:{cap}:r"}
        ]
        fake_provider.lookup_result = DraftLookupResult(7, ())
        clock.advance(500)
        await bot.on_gmail_draft_callback(_update(f"bm:{cap}:r"), MagicMockContext())
        assert "weiterhin unklar" in sender.calls[-1]["text"]
        assert _buttons(sender.calls[-1]["reply_markup"]) == [
            {"text": "🔄 Gmail-Status prüfen", "callback_data": f"bm:{cap}:r"}
        ]
        assert len(fake_provider.creates) == 1

    @pytest.mark.asyncio
    async def test_no_send_button_anywhere(
        self, bot_env, session_factory, requested, sender, fake_provider, clock
    ):
        cap = requested["capability"]
        await bot.on_review_decision_callback(_update(f"bz:{cap}:f"), MagicMockContext())
        fake_provider.create_exc = DraftCreateOutcomeUnknownError("x")
        await bot.on_gmail_draft_callback(_update(f"bm:{cap}:c"), MagicMockContext())
        clock.advance(500)
        await bot.on_gmail_draft_callback(_update(f"bm:{cap}:r"), MagicMockContext())
        texts = json.dumps([call["reply_markup"] for call in sender.calls], ensure_ascii=False)
        assert "Senden" not in texts and "senden" not in texts and "Send" not in texts

    @pytest.mark.asyncio
    async def test_logs_never_contain_capability_or_content(
        self, bot_env, session_factory, approved, sender, fake_provider, caplog
    ):
        caplog.set_level(logging.DEBUG)
        cap = approved["capability"]
        await bot.on_gmail_draft_callback(_update(f"bm:{cap}:c"), MagicMockContext())
        message = fake_provider.creates[0][0]
        for secret in (cap, f"bm:{cap}", ACCOUNT, message.subject, message.message_id):
            assert secret not in caplog.text
        assert "telegram_gmail_draft_callback action=c result=CREATED" in caplog.text


# --- full stack through the REAL provider over a fake IMAP transport --------------------------


@pytest_asyncio.fixture()
async def real_stack(monkeypatch):
    """The REAL GmailImapDraftProvider; only the IMAP transport class is a
    command-recording fake. Every forbidden outbound path raises."""
    clients: list[FakeClient] = []

    class Transport(FakeClient):
        def __init__(self, host, port, *, ssl_context, timeout, deadline):
            super().__init__()
            clients.append(self)

    monkeypatch.setattr(imap_draft, "DeadlineIMAP4SSL", Transport)

    def forbidden(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"forbidden outbound call: {name}")

        return _raise

    monkeypatch.setattr(smtplib, "SMTP", forbidden("smtplib.SMTP"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden("smtplib.SMTP_SSL"))
    monkeypatch.setattr(urllib.request, "urlopen", forbidden("urllib.urlopen"))
    monkeypatch.setattr(httpx, "AsyncClient", forbidden("httpx.AsyncClient"))
    monkeypatch.setattr(httpx, "Client", forbidden("httpx.Client"))
    monkeypatch.setattr(
        "app.providers.email.smtp.GmailSmtpProvider.send", forbidden("GmailSmtpProvider.send")
    )
    for target in (
        "app.services.response_draft_send.ResponseDraftSendService",
        "app.services.follow_up_send.FollowUpSendService",
    ):
        try:
            monkeypatch.setattr(target, forbidden(target))
        except AttributeError:
            pass
    return clients


@pytest.mark.asyncio
async def test_full_stack_create_and_reconcile_use_only_allowed_imap_commands(
    bot_env, session_factory, approved, sender, real_stack, clock
):
    cap = approved["capability"]
    # Real clock for the provider's own deadline arithmetic.
    clock.now = datetime.now(UTC)
    await bot.on_gmail_draft_callback(_update(f"bm:{cap}:c"), MagicMockContext())
    assert "NOCH NICHT GESENDET" in sender.calls[-1]["text"]
    (create_client,) = real_stack
    assert set(create_client.commands()) <= ALLOWED_COMMANDS
    assert create_client.commands().count("APPEND") == 1
    append = next(entry for entry in create_client.trace if entry[0] == "APPEND")
    assert append[1:3] == ('"[Gmail]/Drafts"', "(\\Draft)")
    raw = create_client.appended[0]
    assert b"\r\nTo:" not in raw and b"\r\nCc:" not in raw and b"\r\nBcc:" not in raw
    row = _ledgers(session_factory)[0]
    assert (row.state, row.uid_validity, row.draft_uid) == ("CREATED", 7, 42)

    # A repeated press never opens another session or APPEND.
    await bot.on_gmail_draft_callback(_update(f"bm:{cap}:c"), MagicMockContext())
    assert len(real_stack) == 1


@pytest.mark.asyncio
async def test_full_stack_reconcile_is_read_only(
    bot_env, session_factory, approved, sender, real_stack, clock, monkeypatch
):
    cap = approved["capability"]
    clock.now = datetime.now(UTC)

    def broken_append(self, mailbox, flags, date_time, message):
        self.trace.append(("APPEND", mailbox, flags, date_time))
        raise OSError("connection reset during APPEND")

    monkeypatch.setattr(FakeClient, "append", broken_append)
    await bot.on_gmail_draft_callback(_update(f"bm:{cap}:c"), MagicMockContext())
    assert _ledgers(session_factory)[0].state == "UNCERTAIN"

    clock.advance(500)
    await bot.on_gmail_draft_callback(_update(f"bm:{cap}:r"), MagicMockContext())
    lookup_client = real_stack[-1]
    assert "APPEND" not in lookup_client.commands()
    assert set(lookup_client.commands()) <= ALLOWED_COMMANDS
    marker = _ledgers(session_factory)[0].marker_message_id
    assert ("UID", "SEARCH", "HEADER", "Message-ID", f'"{marker}"') in lookup_client.trace
    assert _ledgers(session_factory)[0].state == "CREATED"
    assert sum(client.commands().count("APPEND") for client in real_stack) == 1
    assert PASSWORD not in json.dumps([call["text"] for call in sender.calls])
