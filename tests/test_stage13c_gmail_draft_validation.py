"""Stage 13C: the Gmail draft path is pinned to ONE operator-chosen mailbox
(GMAIL_DRAFT_ALLOWED_ACCOUNT, empty = refuse) and structurally cannot send.

Runs the REAL Stage 9B/9C/6E/9D services on file-backed SQLite (the Stage
9C fixtures, including the network guard) with the recording fake draft
provider of tests/test_gmail_application_draft.py -- no IMAP, no Gmail,
nothing created, nothing sent. The rest of the Stage 13C test matrix
(approval binding, staleness, idempotency, provider failure, UNCERTAIN,
lost commit acknowledgments, privacy) is covered by that module and by
tests/integration/test_gmail_application_draft_postgres.py; see
docs/STAGE_13C_GMAIL_DRAFT_VALIDATION.md for the mapping.
"""

import ast
import dataclasses
import inspect
import logging
from datetime import timedelta
from pathlib import Path

import pytest

import app.services.gmail_application_draft as gd
from app.core.config import Settings
from app.providers.email import draft_base
from app.providers.email.draft_base import (
    DraftAccountNotAllowedError,
    DraftLookupError,
    DraftMessage,
    DraftProvider,
    build_draft_mime,
)
from app.providers.email.imap_draft import GmailImapDraftProvider
from tests import test_gmail_application_draft as s9d
from tests.test_gmail_application_draft import (
    ACCOUNT,
    PASSWORD,
    _create,
    _ledgers,
    _parsed,
    _recheck,
)
from tests.test_gmail_draft_provider import NOW, FakeClient, _message, _target

# The Stage 9C/9D fixtures (real 9B/9C/6E flow on SQLite, network guard,
# recording fake draft provider, frozen clock).
approved = s9d.approved
clock = s9d.clock
network_guard = s9d.network_guard
provider = s9d.provider
requested = s9d.requested
seeded = s9d.seeded
sender = s9d.sender
session_factory = s9d.session_factory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OTHER = "personal.mailbox@example.com"
PINNED_FALSE = ("outbound_sending_enabled", "telegram_gmail_draft_enabled", "alembic_auto_upgrade")


# --- H: mailbox identity pin (service) ---------------------------------------------------


@pytest.mark.parametrize(
    "pin",
    [
        "",  # the default: no pinned account at all
        OTHER,  # a different (e.g. personal) mailbox
        f"{ACCOUNT}, {OTHER}",  # never a list
        f"Anna <{ACCOUNT}>",  # never a display-name form
        f" {ACCOUNT}",  # never silently stripped
        "not-an-address",
    ],
)
@pytest.mark.asyncio
async def test_unpinned_or_mismatched_account_refuses_before_any_claim(
    session_factory, approved, provider, pin
):
    outcome = await _create(
        session_factory, approved["capability"], provider, gmail_draft_allowed_account=pin
    )
    assert outcome.code == "ACCOUNT_NOT_ALLOWED"
    assert outcome.action is None  # no create/retry button
    assert "nichts" in outcome.notice.lower()
    assert provider.creates == [] and _ledgers(session_factory) == []


@pytest.mark.asyncio
async def test_username_not_matching_the_pin_refuses(session_factory, approved, provider):
    outcome = await _create(session_factory, approved["capability"], provider, gmail_username=OTHER)
    assert outcome.code == "ACCOUNT_NOT_ALLOWED"
    assert provider.creates == [] and _ledgers(session_factory) == []


@pytest.mark.asyncio
async def test_pin_is_an_identity_comparison(session_factory, approved, provider):
    outcome = await _create(
        session_factory,
        approved["capability"],
        provider,
        gmail_draft_allowed_account=ACCOUNT.upper(),
    )
    assert outcome.code == "CREATED" and len(provider.creates) == 1
    (_message_sent, target, _deadline) = provider.creates[0]
    assert target.account_key == ACCOUNT


@pytest.mark.asyncio
async def test_reconcile_lookup_requires_the_pin(session_factory, approved, provider, clock):
    provider.create_exc = draft_base.DraftCreateOutcomeUnknownError("x")
    assert (await _create(session_factory, approved["capability"], provider)).code == "UNCERTAIN"
    clock.advance(200)
    outcome = await _recheck(
        session_factory, approved["capability"], provider, gmail_draft_allowed_account=""
    )
    assert outcome.code == "ACCOUNT_NOT_ALLOWED" and provider.lookups == []
    (row,) = _ledgers(session_factory)
    assert row.state == "UNCERTAIN"  # sticky: a refusal never downgrades it


def test_build_provider_wires_the_pin():
    built = gd.build_provider(s9d._settings())
    assert built.allowed_account == ACCOUNT
    assert gd.build_provider(s9d._settings(gmail_draft_allowed_account="")).allowed_account == ""


# --- H: mailbox identity pin (provider, defense in depth) ----------------------------------


def _provider(allowed_account, factory_calls):
    def factory(deadline, timeout):
        factory_calls.append(timeout)
        return FakeClient()

    return GmailImapDraftProvider(
        "imap.example.com",
        993,
        ACCOUNT,
        PASSWORD,
        drafts_enabled=True,
        allowed_account=allowed_account,
        client_factory=factory,
        clock=lambda: NOW,
    )


@pytest.mark.parametrize("pin", ["", OTHER, "   "])
def test_provider_refuses_create_before_connecting(pin):
    calls: list = []
    with pytest.raises(DraftAccountNotAllowedError) as raised:
        _provider(pin, calls).create_draft(_message(), _target(), NOW + timedelta(seconds=60))
    assert calls == []  # no TLS connection, no LOGIN, no APPEND
    assert raised.value.code == "ACCOUNT_NOT_ALLOWED"
    assert isinstance(raised.value, draft_base.DraftCreateDefiniteError)
    assert ACCOUNT not in str(raised.value) and PASSWORD not in str(raised.value)


@pytest.mark.parametrize("pin", ["", OTHER])
def test_provider_refuses_lookup_before_connecting(pin):
    calls: list = []
    target = draft_base.ReconcileTarget(_target(), f"<s9d.{'a' * 32}@ai-job-search.invalid>")
    with pytest.raises(DraftLookupError):
        _provider(pin, calls).find_by_message_id(target, NOW + timedelta(seconds=60))
    assert calls == []


def test_provider_default_has_no_pinned_account():
    unpinned = GmailImapDraftProvider("h", 993, ACCOUNT, PASSWORD, drafts_enabled=True)
    with pytest.raises(DraftAccountNotAllowedError):
        unpinned.create_draft(_message(), _target(), NOW + timedelta(seconds=60))


def test_pinned_provider_still_logs_in_as_exactly_the_pinned_account():
    logins: list = []
    client = FakeClient()
    original_login = client.login

    def login(user, password):
        logins.append(user)
        return original_login(user, password)

    client.login = login

    def factory(deadline, timeout):
        return client

    pinned = GmailImapDraftProvider(
        "imap.example.com",
        993,
        ACCOUNT,
        PASSWORD,
        drafts_enabled=True,
        allowed_account=ACCOUNT,
        client_factory=factory,
        clock=lambda: NOW,
    )
    pinned.create_draft(_message(), _target(), NOW + timedelta(seconds=60))
    assert client.commands().count("APPEND") == 1
    assert logins == [ACCOUNT]


# --- D/E/F: recipient safety (structural: Stage 13C drafts carry NO recipient) ------------


def test_draft_message_has_no_recipient_field():
    names = {field.name for field in dataclasses.fields(DraftMessage)}
    assert names == {"from_address", "subject", "body_lf", "message_id"}


def test_entry_points_take_no_recipient_or_content_argument():
    for entry in (gd.handle_create, gd.handle_reconcile):
        assert list(inspect.signature(entry).parameters) == [
            "session_factory",
            "settings",
            "capability",
            "provider",
        ]


@pytest.mark.asyncio
async def test_created_mime_has_no_recipient_and_only_allowed_headers(
    session_factory, approved, provider
):
    assert (await _create(session_factory, approved["capability"], provider)).code == "CREATED"
    message = _parsed(provider.mime[0])
    assert {name.lower() for name in message.keys()} <= draft_base.ALLOWED_HEADERS
    for header in draft_base.FORBIDDEN_HEADERS:
        assert message[header] is None


def test_mime_builder_cannot_be_given_a_recipient():
    mime = build_draft_mime(_message(), date=NOW)
    head = mime.split(b"\r\n\r\n", 1)[0].lower()
    for header in ("to:", "cc:", "bcc:", "reply-to:"):
        assert b"\r\n" + header.encode() not in b"\r\n" + head


# --- N: no send path reachable from Stage 13C ----------------------------------------------

_STAGE_13C_MODULES = (
    "app/services/gmail_application_draft.py",
    "app/providers/email/imap_draft.py",
    "app/providers/email/draft_base.py",
)
_SEND_MODULE_PREFIXES = (
    "smtplib",
    "app.providers.email.smtp",
    "app.providers.email.outbound_base",
    "app.services.response_draft_send",
    "app.services.follow_up_send",
)


@pytest.mark.parametrize("relative", _STAGE_13C_MODULES)
def test_stage13c_modules_import_no_send_capability(relative):
    tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            names = []
        for name in names:
            assert not name.startswith(_SEND_MODULE_PREFIXES), (relative, name)
        if isinstance(node, ast.Attribute):
            assert not node.attr.startswith(("send", "sendmail")), (relative, node.attr)


def test_draft_provider_surface_is_create_and_lookup_only():
    protocol_methods = {
        name
        for name, value in vars(DraftProvider).items()
        if inspect.isfunction(value) and not name.startswith("__")
    }
    assert protocol_methods == {"create_draft", "find_by_message_id"}
    public = {
        name
        for name, value in vars(GmailImapDraftProvider).items()
        if inspect.isfunction(value) and not name.startswith("_")
    }
    assert public == {"create_draft", "find_by_message_id"}


# --- O: secrets stay out of logs and reprs -------------------------------------------------


@pytest.mark.asyncio
async def test_pin_refusal_logs_no_identity_or_secret(session_factory, approved, provider, caplog):
    caplog.set_level(logging.DEBUG)
    await _create(session_factory, approved["capability"], provider, gmail_username=OTHER)
    text = caplog.text
    assert OTHER not in text and ACCOUNT not in text and PASSWORD not in text


def test_provider_repr_hides_account_and_password():
    calls: list = []
    text = repr(_provider(ACCOUNT, calls))
    assert ACCOUNT not in text and PASSWORD not in text


# --- P / flag defaults ------------------------------------------------------------------------


def test_safety_defaults_stay_closed():
    settings = Settings(_env_file=None)
    for name in PINNED_FALSE:
        assert getattr(settings, name) is False, name
    assert settings.gmail_draft_allowed_account == ""
    assert gd.gmail_draft_flags_enabled(settings) is False
    assert gd.account_allowed(settings) is False


def test_env_example_ships_the_pin_empty_and_drafts_disabled():
    lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "GMAIL_DRAFT_ALLOWED_ACCOUNT=" in lines
    assert "TELEGRAM_GMAIL_DRAFT_ENABLED=false" in lines
    assert "OUTBOUND_SENDING_ENABLED=false" in lines


def test_draft_flag_never_consults_outbound_sending():
    assert gd.gmail_draft_flags_enabled(s9d._settings(outbound_sending_enabled=False)) is True
    assert (
        gd.gmail_draft_flags_enabled(
            s9d._settings(telegram_gmail_draft_enabled=False, outbound_sending_enabled=True)
        )
        is False
    )


# --- documentation contract -------------------------------------------------------------------


def test_stage13c_runbook_requires_explicit_confirmation_and_states_no_send():
    text = (PROJECT_ROOT / "docs/STAGE_13C_GMAIL_DRAFT_VALIDATION.md").read_text(encoding="utf-8")
    for phrase in (
        "GMAIL_DRAFT_ALLOWED_ACCOUNT",
        "does NOT send",
        "non-production",
        "explicit operator confirmation",
        "UNCERTAIN",
    ):
        assert phrase in text, phrase
