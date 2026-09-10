"""Tests for app.providers.email.imap.GmailImapProvider (Stage 7A + the
Stage 7A security fix round — GMAIL-001/004/005/009/010).

Mirrors tests/test_collectors_xing_email.py's approach: a lightweight fake
IMAP client (no real socket/network I/O anywhere), plus a source-inspection
test asserting this package has no means to make an HTTP request at all.
"""

import email.errors
import imaplib
import inspect
import socket
import ssl
import threading
import time
from datetime import UTC, datetime
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

import app.providers.email.base as email_base_module
import app.providers.email.imap as gmail_imap_module
from app.providers.email.base import GmailAuthError, GmailConnectionError
from app.providers.email.imap import IMAP_OPERATION_TIMEOUT_SECONDS, GmailImapProvider
from app.providers.email.imap_deadline import ImapSessionDeadline

ACCOUNT = "me@example.com"


class FakeImapClient:
    """Minimal fake matching the ImapClient Protocol. No real socket/network
    I/O anywhere in this class.

    Discriminates a "fetch" UID command by its requested item spec:
    `(RFC822.SIZE)` returns a size-only response (GMAIL-005's pre-check),
    anything else (in practice always `(BODY.PEEK[])`) returns the full
    raw message.
    """

    def __init__(
        self,
        messages: dict[int, bytes] | None = None,
        uid_validity: int = 100,
        select_typ: str = "OK",
        status_data: list[bytes] | None = None,
        search_typ: str = "OK",
        search_uids: list[int] | None = None,
        size_override: dict[int, int] | None = None,
        internal_dates: dict[int, str] | None = None,
        raise_oserror_on_uid: set[int] | None = None,
    ) -> None:
        self._messages = messages or {}
        self._uid_validity = uid_validity
        self._select_typ = select_typ
        self._status_data = status_data
        self._search_typ = search_typ
        self._search_uids = search_uids
        self._size_override = size_override or {}
        self._internal_dates = internal_dates or {}
        # Codex gate follow-up (Astra R4A MEDIUM): a fetch for any UID in
        # this set raises OSError -- lets a test deterministically
        # simulate a transport-level failure (including "the deadline
        # watchdog force-closed the socket mid-FETCH") without any real
        # socket/timing involved.
        self._raise_oserror_on_uid = raise_oserror_on_uid or set()
        self.select_calls: list[tuple[str, bool]] = []
        self.uid_calls: list[tuple[str, tuple]] = []
        self.closed = False
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return (self._select_typ, [str(len(self._messages)).encode()])

    def status(self, mailbox: str, names: str) -> tuple[str, list[bytes]]:
        if self._status_data is not None:
            return ("OK", self._status_data)
        return ("OK", [f'"{mailbox}" (UIDVALIDITY {self._uid_validity})'.encode()])

    def uid(self, command: str, *args) -> tuple[str, list]:
        self.uid_calls.append((command, args))
        if command == "search":
            if self._search_typ != "OK":
                return (self._search_typ, [None])
            uids = self._search_uids if self._search_uids is not None else sorted(self._messages)
            data = b" ".join(str(u).encode() for u in uids)
            return ("OK", [data])
        if command == "fetch":
            uid = int(args[0])
            item_spec = args[1] if len(args) > 1 else ""
            raw = self._messages.get(uid)
            if raw is None:
                return ("OK", [None])
            if "RFC822.SIZE" in item_spec:
                size = self._size_override.get(uid, len(raw))
                return ("OK", [f"{uid} (UID {uid} RFC822.SIZE {size})".encode()])
            if uid in self._raise_oserror_on_uid:
                raise OSError("simulated transport failure")
            internal_date = self._internal_dates.get(uid)
            if internal_date is not None:
                header = b'%d (UID %d INTERNALDATE "%s" BODY[] {%d}' % (
                    uid,
                    uid,
                    internal_date.encode(),
                    len(raw),
                )
            else:
                header = b"%d (UID %d BODY[] {%d}" % (uid, uid, len(raw))
            return ("OK", [(header, raw)])
        raise AssertionError(f"unexpected uid command {command!r}")

    def close(self) -> tuple[str, list[bytes]]:
        self.closed = True
        return ("OK", [b"CLOSE completed"])

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return ("OK", [b"BYE"])


class RaisingImapClient:
    """Used to assert a code path never touches the injected client at all
    (e.g. the not-configured check must short-circuit before any I/O)."""

    def __getattr__(self, name):
        raise AssertionError(f"ImapClient.{name} must not be called")


def _build_email(
    *,
    sender: str = "alice@example.com",
    to: str = ACCOUNT,
    cc: str | None = None,
    subject: str = "Hello",
    plaintext_body: str | None = "Hi there",
    html_body: str | None = None,
    message_id: str | None = "<msg1@example.com>",
    in_reply_to: str | None = None,
    references: str | None = None,
    date: str | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    extra_parts: list[MIMEBase] | None = None,
) -> bytes:
    if html_body is not None or attachments or extra_parts:
        msg = MIMEMultipart("mixed")
        if html_body is not None:
            alt = MIMEMultipart("alternative")
            if plaintext_body is not None:
                alt.attach(MIMEText(plaintext_body, "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(alt)
        elif plaintext_body is not None:
            msg.attach(MIMEText(plaintext_body, "plain", "utf-8"))
        for filename, content_type, data in attachments or []:
            maintype, subtype = content_type.split("/")
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
        for part in extra_parts or []:
            msg.attach(part)
    else:
        msg = MIMEText(plaintext_body or "", "plain", "utf-8")

    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = Header(subject, "utf-8").encode()
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if date:
        msg["Date"] = date
    return msg.as_bytes()


def _provider(client: object, **overrides) -> GmailImapProvider:
    kwargs = {
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "username": ACCOUNT,
        "app_password": "app-password",
        "mailbox": "INBOX",
        "lookback_days": 30,
        "imap_client": client,
    }
    kwargs.update(overrides)
    return GmailImapProvider(**kwargs)


# ---------------------------------------------------------------------------
# Configuration / auth / connection failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_raises_auth_error_when_username_missing():
    provider = _provider(RaisingImapClient(), username="")
    with pytest.raises(GmailAuthError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_fetch_raises_auth_error_when_password_missing():
    provider = _provider(RaisingImapClient(), app_password="")
    with pytest.raises(GmailAuthError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_fetch_raises_auth_error_when_password_whitespace_only():
    provider = _provider(RaisingImapClient(), app_password="   ")
    with pytest.raises(GmailAuthError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_login_rejected_raises_auth_error(monkeypatch):
    class RejectingClient(FakeImapClient):
        def login(self, user, password):
            raise imaplib.IMAP4.error("bad credentials")

    def fake_deadline_client(host, port, **kwargs):
        return RejectingClient()

    # _provider(None) (owns_connection=True) now constructs via
    # DeadlineIMAP4SSL, not imaplib.IMAP4_SSL directly -- see
    # TestSessionDeadlineWiring below for why.
    monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", fake_deadline_client)
    provider = _provider(None)

    with pytest.raises(GmailAuthError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_connect_os_error_raises_connection_error(monkeypatch):
    def fake_deadline_client(host, port, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", fake_deadline_client)
    provider = _provider(None)

    with pytest.raises(GmailConnectionError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_connect_os_error_does_not_leak_host_port_or_raw_exception_text(monkeypatch):
    """Codex final review, MEDIUM: constructor-time OSError sanitization
    (still) must not leak the configured host/port or raw exception
    text. Uses distinctive fake sensitive values so a leak is
    unambiguous."""
    sensitive_host = "corp-mailserver-do-not-leak.internal"
    sensitive_port = 47993
    sensitive_exc_text = "SECRET_RAW_OS_TEXT_LEAKED_IF_VISIBLE"

    def fake_deadline_client(host, port, **kwargs):
        raise OSError(sensitive_exc_text)

    monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", fake_deadline_client)
    provider = _provider(None, imap_host=sensitive_host, imap_port=sensitive_port)

    with pytest.raises(GmailConnectionError) as exc_info:
        await provider.fetch()

    message = str(exc_info.value)
    assert sensitive_host not in message
    assert str(sensitive_port) not in message
    assert sensitive_exc_text not in message


@pytest.mark.asyncio
async def test_constructor_imap4_abort_raises_sanitized_connection_error(monkeypatch):
    """Codex final review, MEDIUM: DeadlineIMAP4SSL's constructor (TLS/
    greeting/CAPABILITY processing, or a deadline-triggered forced
    close mid-read) can raise imaplib.IMAP4.abort -- a plain Exception
    subclass, NOT an OSError -- carrying raw, potentially
    server-controlled text. This must be caught and sanitized exactly
    like a constructor-time OSError, not left to escape raw."""
    sensitive_host = "corp-mailserver-do-not-leak.internal"
    sensitive_port = 47993
    sensitive_exc_text = "SECRET_RAW_SERVER_TEXT"

    def fake_deadline_client(host, port, **kwargs):
        raise imaplib.IMAP4.abort(sensitive_exc_text)

    monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", fake_deadline_client)
    provider = _provider(None, imap_host=sensitive_host, imap_port=sensitive_port)

    with pytest.raises(GmailConnectionError) as exc_info:
        await provider.fetch()

    message = str(exc_info.value)
    assert sensitive_exc_text not in message
    assert sensitive_host not in message
    assert str(sensitive_port) not in message


@pytest.mark.asyncio
async def test_constructor_imap4_error_raises_sanitized_connection_error(monkeypatch):
    """Same as above for the base imaplib.IMAP4.error (abort's parent
    class) -- e.g. a malformed/unexpected greeting or CAPABILITY
    response the constructor rejects outright."""
    sensitive_host = "corp-mailserver-do-not-leak.internal"
    sensitive_port = 47993
    sensitive_exc_text = "SECRET_RAW_SERVER_TEXT"

    def fake_deadline_client(host, port, **kwargs):
        raise imaplib.IMAP4.error(sensitive_exc_text)

    monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", fake_deadline_client)
    provider = _provider(None, imap_host=sensitive_host, imap_port=sensitive_port)

    with pytest.raises(GmailConnectionError) as exc_info:
        await provider.fetch()

    message = str(exc_info.value)
    assert sensitive_exc_text not in message
    assert sensitive_host not in message
    assert str(sensitive_port) not in message


@pytest.mark.asyncio
async def test_login_rejected_with_imap4_error_still_raises_sanitized_auth_error(monkeypatch):
    """The constructor-time imaplib.IMAP4.error handling added above
    must not swallow the EXPLICIT, later client.login() rejection into
    a connection error -- login rejection is a distinct, separately
    try/excepted block and must keep raising GmailAuthError, sanitized
    exactly like the constructor path."""
    sensitive_exc_text = "SECRET_RAW_LOGIN_REJECTION_TEXT"

    class RejectingClient(FakeImapClient):
        def login(self, user, password):
            raise imaplib.IMAP4.error(sensitive_exc_text)

    def fake_deadline_client(host, port, **kwargs):
        return RejectingClient()

    monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", fake_deadline_client)
    provider = _provider(None)

    with pytest.raises(GmailAuthError) as exc_info:
        await provider.fetch()

    assert sensitive_exc_text not in str(exc_info.value)


@pytest.mark.asyncio
async def test_select_failure_raises_connection_error():
    client = FakeImapClient(select_typ="NO")
    provider = _provider(client)

    with pytest.raises(GmailConnectionError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_missing_uidvalidity_raises_connection_error():
    client = FakeImapClient(status_data=[b'"INBOX" (MESSAGES 0)'])
    provider = _provider(client)

    with pytest.raises(GmailConnectionError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_search_failure_raises_connection_error():
    client = FakeImapClient(search_typ="NO")
    provider = _provider(client)

    with pytest.raises(GmailConnectionError):
        await provider.fetch()


# ---------------------------------------------------------------------------
# GMAIL-009: identity invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_uidvalidity_raises_connection_error():
    client = FakeImapClient(status_data=[b'"INBOX" (UIDVALIDITY 0)'])
    provider = _provider(client)

    with pytest.raises(GmailConnectionError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_zero_uid_is_skipped_not_crashed():
    raw = _build_email()
    client = FakeImapClient(messages={0: raw}, search_uids=[0])
    provider = _provider(client)

    result = await provider.fetch()

    assert result.messages == ()
    assert result.skipped_count == 1


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_is_called_readonly():
    client = FakeImapClient(messages={})
    provider = _provider(client)

    await provider.fetch()

    assert client.select_calls == [("INBOX", True)]


@pytest.mark.asyncio
async def test_disconnect_closes_and_logs_out_owned_connections(monkeypatch):
    fake_client = FakeImapClient(messages={})

    def fake_deadline_client(host, port, **kwargs):
        return fake_client

    monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", fake_deadline_client)
    provider = _provider(None)

    await provider.fetch()

    assert fake_client.closed is True
    assert fake_client.logged_out is True


def test_module_never_imports_an_http_client():
    for module in (gmail_imap_module, email_base_module):
        source = inspect.getsource(module)
        for name in ("httpx", "requests", "aiohttp", "urllib.request", "http.client"):
            assert f"import {name}" not in source, f"module must not import {name}"
        for name in ("httpx", "requests", "aiohttp"):
            assert not hasattr(module, name)


def test_module_never_calls_mailbox_write_commands():
    """No STORE/EXPUNGE/COPY/APPEND IMAP command anywhere — this provider
    must only ever be able to read the mailbox, never mutate it. Checks
    for the IMAP client call forms specifically (`.store(`/`.expunge(`/
    `.copy(`/`client.append(`), not Python's unrelated `list.append`.
    """
    source = inspect.getsource(gmail_imap_module)
    for forbidden in (".store(", ".expunge(", ".copy(", "client.append("):
        assert forbidden not in source, f"module must not call {forbidden}"
    for forbidden_uid_command in ('"store"', "'store'", '"expunge"', "'expunge'"):
        assert forbidden_uid_command not in source, (
            f"module must not issue IMAP UID command {forbidden_uid_command}"
        )


# ---------------------------------------------------------------------------
# GMAIL-001: BODY.PEEK[], never bare RFC822/BODY[]
# ---------------------------------------------------------------------------


def test_body_fetch_uses_peek_never_plain_rfc822_or_bare_body():
    """Static regression guard: if this module is ever changed back to a
    bare `(RFC822)` or `(BODY[])` fetch item, this test fails even before
    any runtime test does — see base.py's module docstring for why a
    non-PEEK fetch is itself a mailbox mutation (\\Seen).
    """
    source = inspect.getsource(gmail_imap_module)
    assert "BODY.PEEK[]" in source
    assert "RFC822)" not in source
    assert "BODY[])" not in source


@pytest.mark.asyncio
async def test_fetch_issues_body_peek_command_not_rfc822():
    raw = _build_email()
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    await provider.fetch()

    fetch_items = [args[1] for cmd, args in client.uid_calls if cmd == "fetch" and len(args) > 1]
    # S7E-011: INTERNALDATE is requested in the SAME fetch as BODY.PEEK[]
    # (one round trip, not two) — never a bare RFC822/BODY[] fetch.
    assert "(INTERNALDATE BODY.PEEK[])" in fetch_items
    assert "(RFC822)" not in fetch_items
    assert "(BODY[])" not in fetch_items


# ---------------------------------------------------------------------------
# S7E-011 (Codex re-review): trusted Gmail-assigned arrival chronology
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internaldate_is_parsed_into_provider_arrival_at():
    raw = _build_email(date="Mon, 5 Jan 2026 08:00:00 +0000")
    client = FakeImapClient(messages={1: raw}, internal_dates={1: "07-Sep-2026 12:34:56 +0000"})
    provider = _provider(client)

    result = await provider.fetch()

    assert len(result.messages) == 1
    message = result.messages[0]
    # The server-assigned INTERNALDATE, NOT the sender-controlled Date
    # header (which claims a much earlier date here) — proves the two are
    # parsed and kept independently.
    assert message.provider_arrival_at == datetime(2026, 9, 7, 12, 34, 56, tzinfo=UTC)
    assert message.sent_at == datetime(2026, 1, 5, 8, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_missing_internaldate_falls_back_to_none():
    """Honest, documented gap (mirrors `_read_message_size`'s RFC822.SIZE
    fallback): a server response with no parseable INTERNALDATE leaves
    `provider_arrival_at` as None rather than guessing — the persistence
    layer (app.db.gmail_repository.upsert_message) supplies its own
    wall-clock fallback in that case."""
    raw = _build_email()
    client = FakeImapClient(messages={1: raw})  # no internal_dates override
    provider = _provider(client)

    result = await provider.fetch()

    assert len(result.messages) == 1
    assert result.messages[0].provider_arrival_at is None


# ---------------------------------------------------------------------------
# GMAIL-005: bound resources before large allocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_message_is_skipped_before_body_fetch():
    raw = _build_email(plaintext_body="small body")
    client = FakeImapClient(messages={1: raw}, size_override={1: 10_000_000})
    provider = _provider(client)

    result = await provider.fetch()

    assert result.messages == ()
    assert result.skipped_count == 1
    body_fetch_calls = [
        args
        for cmd, args in client.uid_calls
        if cmd == "fetch" and len(args) > 1 and args[1] == "(INTERNALDATE BODY.PEEK[])"
    ]
    assert body_fetch_calls == [], "an oversized message's body must never be fetched at all"


@pytest.mark.asyncio
async def test_oversized_message_is_reported_as_permanently_skipped():
    """FINAL-004 (Astra R5A): an oversized message's content will always
    be oversized on every future attempt too -- it must be reported via
    `GmailFetchResult.permanently_skipped`, not just counted in the
    plain (retryable) `skipped_count`, so the caller can durably record
    it via `app.db.gmail_repository.record_permanent_skips` and never
    re-select it as a candidate again (see
    `app.db.models.GmailPermanentSkipRecord`'s docstring for the
    starvation this closes)."""
    raw = _build_email(plaintext_body="small body")
    client = FakeImapClient(messages={1: raw}, size_override={1: 10_000_000}, uid_validity=555)
    provider = _provider(client)

    result = await provider.fetch()

    assert result.permanently_skipped == ((1, "OVERSIZED"),)
    assert result.uid_validity == 555


@pytest.mark.asyncio
async def test_transport_failure_is_not_reported_as_permanently_skipped():
    """The other half of the same distinction: a transient transport
    failure (a different attempt could plausibly succeed) must NEVER be
    recorded as permanent -- it must stay eligible for retry on the next
    sync, exactly like before this fix existed."""
    raw = _build_email(plaintext_body="hi")
    client = FakeImapClient(messages={1: raw}, raise_oserror_on_uid={1})
    provider = _provider(client)

    with pytest.raises(GmailConnectionError):
        await provider.fetch()

    # The OSError propagated (a genuine, non-deadline transport failure)
    # rather than being swallowed into a GmailFetchResult -- confirming
    # this failure mode was never at risk of being misclassified as
    # permanent in the first place (there is no GmailFetchResult to
    # inspect here at all, which is itself the correct, unchanged
    # behavior for a real connection failure).


@pytest.mark.asyncio
async def test_permanent_skips_do_not_starve_later_valid_uids_across_syncs(monkeypatch):
    """FINAL-004 (Astra R5A): the exact Astra scenario -- a prefix of
    permanently-unfetchable messages (here: oversized, standing in for
    "500 permanent skips" at a scale a unit test can actually run) must
    not consume every future sync's entire MAX_MESSAGES_PER_SYNC budget
    forever. Mirrors
    `test_messages_per_sync_cap_drains_backlog_across_syncs_via_get_known_uids`
    above exactly (same oldest-first-prioritization-alone-is-not
    -sufficient structure) but with PERMANENTLY bad messages as the
    prefix instead of merely not-yet-persisted ones -- proving the
    starvation fix also covers messages that can never be persisted at
    all, not just ones that eventually get persisted.

    `known` here plays the same role `app.db.gmail_repository
    .get_known_uids`'s real UNION of GmailMessageRecord and
    GmailPermanentSkipRecord plays in production (see that function's
    own docstring) -- updated from `result.permanently_skipped` exactly
    like `app.services.gmail_inbox.GmailInboxService.sync` updates the
    real DB via `record_permanent_skips` between sync runs.
    """
    monkeypatch.setattr(gmail_imap_module, "MAX_MESSAGES_PER_SYNC", 2)
    known: set[int] = set()
    # UIDs 1, 2: permanently oversized -- will NEVER be persisted, no
    # matter how many times a sync attempts them.
    # UID 3: a genuinely valid, fetchable message.
    messages = {
        1: _build_email(message_id="<1@example.com>", plaintext_body="oversized-1"),
        2: _build_email(message_id="<2@example.com>", plaintext_body="oversized-2"),
        3: _build_email(message_id="<3@example.com>", plaintext_body="valid"),
    }
    size_override = {1: 10_000_000, 2: 10_000_000}
    client = FakeImapClient(messages=messages, size_override=size_override)
    provider = _provider(
        client,
        get_known_uids=lambda uid_validity, candidate_uids: known & set(candidate_uids),
    )

    first_run = await provider.fetch()
    # The cap (2) is entirely consumed by the two permanently-oversized
    # UIDs -- UID 3 is never even attempted this run, exactly the
    # starvation-in-progress state before the fix's caller-side
    # exclusion kicks in.
    assert first_run.messages == ()
    assert {uid for uid, _reason in first_run.permanently_skipped} == {1, 2}
    # Simulates GmailInboxService.sync persisting these via
    # record_permanent_skips, then a later sync's get_known_uids
    # (backed by the real UNION query) excluding them.
    known.update(uid for uid, _reason in first_run.permanently_skipped)

    second_run = await provider.fetch()
    # UID 3 is NOW reached -- forward progress was made past the
    # permanently-bad prefix instead of the same two UIDs being
    # reselected and re-consuming the cap forever.
    assert {msg.uid for msg in second_run.messages} == {3}
    assert second_run.permanently_skipped == ()


@pytest.mark.asyncio
async def test_unknown_size_proceeds_to_fetch_body():
    """A server/fake that can't report RFC822.SIZE cleanly must not fail
    closed — size gating is a best-effort optimization on top of the
    other bounds, not the only one."""
    raw = _build_email(plaintext_body="hello")
    client = FakeImapClient(messages={1: raw}, status_data=None)
    # Simulate a SIZE response with no parseable size at all.
    original_uid = client.uid

    def uid_no_size(command, *args):
        if command == "fetch" and len(args) > 1 and "RFC822.SIZE" in args[1]:
            return ("OK", [b"1 (UID 1)"])
        return original_uid(command, *args)

    client.uid = uid_no_size
    provider = _provider(client)

    result = await provider.fetch()

    assert len(result.messages) == 1


@pytest.mark.asyncio
async def test_messages_per_sync_is_capped(monkeypatch):
    monkeypatch.setattr(gmail_imap_module, "MAX_MESSAGES_PER_SYNC", 3)
    messages = {uid: _build_email(message_id=f"<{uid}@example.com>") for uid in range(1, 6)}
    client = FakeImapClient(messages=messages)
    provider = _provider(client)

    result = await provider.fetch()

    assert len(result.messages) == 3
    assert result.skipped_count == 2
    # The OLDEST (lowest) UIDs are prioritized — see _fetch_sync's
    # comment: preferring the newest UIDs would risk starving the same
    # backlog of older messages out of the lookback window forever
    # whenever arrivals sustainedly exceed the cap, instead of merely
    # deferring them one sync at a time.
    fetched_uids = {msg.uid for msg in result.messages}
    assert fetched_uids == {1, 2, 3}


@pytest.mark.asyncio
async def test_messages_per_sync_cap_drains_backlog_across_syncs_via_get_known_uids(monkeypatch):
    """The concrete GMAIL-005 starvation scenario: a sustained backlog
    (arrivals exceeding the cap on every sync) must make real forward
    progress across successive syncs, not perpetually re-fetch the same
    slice.

    Oldest-first prioritization ALONE is not sufficient for this — IMAP
    UID SEARCH always returns the same full backlog every time (nothing
    about the mailbox itself changes), so without filtering by
    `get_known_uids` the oldest UIDs would win the cap forever and the
    provider would never reach anything newer as long as the backlog
    stays above the cap. `get_known_uids` (bound to already-persisted
    state by the caller — see app/api/routes.py's _run_gmail_sync) is
    what actually lets each sync's cap apply to genuinely new work.
    """
    monkeypatch.setattr(gmail_imap_module, "MAX_MESSAGES_PER_SYNC", 2)
    known: set[int] = set()
    messages = {uid: _build_email(message_id=f"<{uid}@example.com>") for uid in range(1, 5)}
    client = FakeImapClient(messages=messages)
    provider = _provider(
        client,
        get_known_uids=lambda uid_validity, candidate_uids: known & set(candidate_uids),
    )

    first_run = await provider.fetch()
    assert {msg.uid for msg in first_run.messages} == {1, 2}
    known.update({1, 2})  # simulate the caller persisting these

    second_run = await provider.fetch()
    assert {msg.uid for msg in second_run.messages} == {3, 4}


@pytest.mark.asyncio
async def test_already_known_uids_are_not_counted_as_skipped():
    known = {1}
    messages = {1: _build_email(), 2: _build_email(message_id="<2@example.com>")}
    client = FakeImapClient(messages=messages)
    provider = _provider(
        client, get_known_uids=lambda uid_validity, candidate_uids: known & set(candidate_uids)
    )

    result = await provider.fetch()

    assert {msg.uid for msg in result.messages} == {2}
    assert result.skipped_count == 0


@pytest.mark.asyncio
async def test_get_known_uids_is_called_once_with_the_full_candidate_list_not_per_uid():
    """GMAIL-012: the provider must call `get_known_uids` ONCE with the
    whole candidate list, never once per UID — a Codex probe reproduced
    the old per-UID wiring as literally N calls for N SEARCH results.
    """
    call_log: list[list[int]] = []

    def recording_get_known_uids(uid_validity, candidate_uids):
        call_log.append(list(candidate_uids))
        return set()

    messages = {uid: _build_email(message_id=f"<{uid}@example.com>") for uid in range(1, 101)}
    client = FakeImapClient(messages=messages)
    provider = _provider(client, get_known_uids=recording_get_known_uids)

    await provider.fetch()

    assert len(call_log) == 1
    assert sorted(call_log[0]) == list(range(1, 101))


@pytest.mark.asyncio
async def test_mime_part_count_is_bounded(monkeypatch):
    monkeypatch.setattr(gmail_imap_module, "MAX_MIME_PARTS", 5)
    many_parts = [MIMEText(f"part {i}", "plain", "utf-8") for i in range(50)]
    raw = _build_email(plaintext_body=None, extra_parts=many_parts)
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    # Must not hang/crash; some body text is still captured from the
    # first parts visited before the bound was reached.
    assert len(result.messages) == 1


@pytest.mark.asyncio
async def test_mime_depth_is_bounded(monkeypatch):
    monkeypatch.setattr(gmail_imap_module, "MAX_MIME_DEPTH", 3)
    inner = MIMEText("deep", "plain", "utf-8")
    for _ in range(10):
        wrapper = MIMEMultipart("mixed")
        wrapper.attach(inner)
        inner = wrapper
    raw = _build_email(plaintext_body=None, extra_parts=[inner])
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    assert len(result.messages) == 1


# ---------------------------------------------------------------------------
# GMAIL-010: malformed FETCH response isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_fetch_response_is_skipped_not_raised():
    client = FakeImapClient(messages={})
    original_uid = client.uid

    def uid_with_phantom(command, *args):
        if command == "search":
            return ("OK", [b"999"])
        return original_uid(command, *args)

    client.uid = uid_with_phantom
    provider = _provider(client)

    result = await provider.fetch()

    assert result.messages == ()
    assert result.skipped_count == 1


@pytest.mark.parametrize(
    "broken_response",
    [
        pytest.param(("OK", []), id="empty_response"),
        pytest.param(("OK", [None]), id="none_item"),
        pytest.param(("OK", [(b"1 (UID 1 BODY[] {5}",)]), id="short_tuple"),
        pytest.param(("OK", [b"1 (UID 1)"]), id="metadata_only_non_tuple"),
        pytest.param(("OK", ["not-a-tuple"]), id="non_tuple_item"),
        pytest.param(("OK", [(b"1 (UID 1 BODY[] {5}", "not-bytes")]), id="payload_not_bytes"),
    ],
)
@pytest.mark.asyncio
async def test_malformed_fetch_response_shapes_are_skipped(broken_response):
    client = FakeImapClient(messages={1: _build_email()})
    original_uid = client.uid

    def uid_with_broken_body(command, *args):
        if command == "fetch" and len(args) > 1 and args[1] == "(INTERNALDATE BODY.PEEK[])":
            return broken_response
        return original_uid(command, *args)

    client.uid = uid_with_broken_body
    provider = _provider(client)

    result = await provider.fetch()

    assert result.messages == ()
    assert result.skipped_count == 1


@pytest.mark.asyncio
async def test_valid_malformed_valid_sequence_all_processed():
    """One malformed FETCH response between two valid ones must not
    prevent the valid messages from being processed (GMAIL-010)."""
    messages = {
        1: _build_email(message_id="<first@example.com>", subject="First"),
        2: _build_email(message_id="<second@example.com>", subject="Second"),
        3: _build_email(message_id="<third@example.com>", subject="Third"),
    }
    client = FakeImapClient(messages=messages)
    original_uid = client.uid

    def uid_break_middle(command, *args):
        if (
            command == "fetch"
            and len(args) > 1
            and args[1] == "(INTERNALDATE BODY.PEEK[])"
            and args[0] == b"2"
        ):
            return ("OK", [None])
        return original_uid(command, *args)

    client.uid = uid_break_middle
    provider = _provider(client)

    result = await provider.fetch()

    subjects = {msg.subject for msg in result.messages}
    assert subjects == {"First", "Third"}
    assert result.skipped_count == 1


@pytest.mark.asyncio
async def test_structurally_malformed_message_is_skipped(monkeypatch):
    raw = _build_email()
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    def broken_extract(msg):
        raise email.errors.MessageParseError("boom")

    monkeypatch.setattr(gmail_imap_module, "_extract_content", broken_extract)

    result = await provider.fetch()

    assert result.messages == ()
    assert result.skipped_count == 1


@pytest.mark.asyncio
async def test_one_bad_message_does_not_prevent_others_from_syncing():
    good_raw = _build_email(message_id="<good@example.com>", subject="Good message")
    client = FakeImapClient(messages={1: good_raw})
    original_uid = client.uid

    def uid_with_extra_phantom(command, *args):
        if command == "search":
            return ("OK", [b"1 999"])
        return original_uid(command, *args)

    client.uid = uid_with_extra_phantom
    provider = _provider(client)

    result = await provider.fetch()

    assert result.skipped_count == 1
    assert len(result.messages) == 1
    assert result.messages[0].subject == "Good message"


# ---------------------------------------------------------------------------
# GMAIL-002: account scoping
# ---------------------------------------------------------------------------


def test_account_key_is_normalized_username():
    provider = _provider(None, username="  Someone@Example.com  ")
    assert provider.account_key == "someone@example.com"


@pytest.mark.asyncio
async def test_parsed_messages_carry_the_provider_account_key():
    raw = _build_email()
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client, username=ACCOUNT)

    result = await provider.fetch()

    assert result.messages[0].account_key == "me@example.com"


# ---------------------------------------------------------------------------
# MIME parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_fetch_parses_plaintext_message():
    raw = _build_email(subject="Regarding your application", plaintext_body="Thanks for applying.")
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    assert result.skipped_count == 0
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.subject == "Regarding your application"
    assert message.body_plain == "Thanks for applying."
    assert message.has_html is False
    assert message.uid == 1
    assert message.uid_validity == 100
    assert message.mailbox == "INBOX"


@pytest.mark.asyncio
async def test_multipart_prefers_plaintext_and_flags_has_html():
    raw = _build_email(
        plaintext_body="Plain version",
        html_body="<p>HTML version</p>",
    )
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_plain == "Plain version"
    assert message.has_html is True


@pytest.mark.asyncio
async def test_html_only_email_has_empty_body_plain_and_has_html_true():
    raw = _build_email(plaintext_body=None, html_body="<p>Only HTML</p>")
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_plain == ""
    assert message.has_html is True


@pytest.mark.asyncio
async def test_encoded_subject_and_sender_display_name_are_decoded():
    encoded_from = f"{Header('Müller Bewerbung', 'utf-8').encode()} <mueller@example.com>"
    raw = _build_email(
        sender=encoded_from,
        subject="Bewerbung für die Stelle",
    )
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.subject == "Bewerbung für die Stelle"
    assert message.from_display_name == "Müller Bewerbung"
    assert message.from_address == "mueller@example.com"


@pytest.mark.asyncio
async def test_missing_message_id_does_not_crash_and_is_none():
    raw = _build_email(message_id=None)
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    assert result.skipped_count == 0
    assert result.messages[0].message_id_header is None


@pytest.mark.asyncio
async def test_references_and_in_reply_to_are_parsed():
    raw = _build_email(
        in_reply_to="<parent@example.com>",
        references="<root@example.com> <parent@example.com>",
    )
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.in_reply_to == "<parent@example.com>"
    assert message.references == ("<root@example.com>", "<parent@example.com>")


@pytest.mark.asyncio
async def test_attachment_metadata_captured_without_storing_content():
    payload = b"%PDF-1.4 fake pdf content"
    raw = _build_email(attachments=[("resume.pdf", "application/pdf", payload)])
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.filename == "resume.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.size == len(payload)


@pytest.mark.asyncio
async def test_body_is_truncated_past_size_limit():
    huge_body = "a" * 25_000
    raw = _build_email(plaintext_body=huge_body)
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_truncated is True
    assert len(message.body_plain) == 20_000


@pytest.mark.asyncio
async def test_direction_inbound_regardless_of_sender_when_not_trusted_outbound():
    """S7E-001 (Codex remediation, HIGH): a provider instance NOT
    explicitly configured for the account's real Sent-mail folder
    (`trusted_outbound=False`, the default — used for the primary
    INBOX/etc. mailbox) must classify EVERY message INBOUND, even one
    whose `From` header claims to be the account's own address. The OLD
    behavior (`From == account` -> OUTBOUND) is exactly what let a
    trivially spoofed header be trusted as genuine outbound
    correspondence."""
    raw = _build_email(sender=ACCOUNT)
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client, trusted_outbound=False)

    result = await provider.fetch()

    assert result.messages[0].direction == "INBOUND"


@pytest.mark.asyncio
async def test_direction_inbound_when_sender_is_not_account_address():
    raw = _build_email(sender="recruiter@company.example")
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client, trusted_outbound=False)

    result = await provider.fetch()

    assert result.messages[0].direction == "INBOUND"


@pytest.mark.asyncio
async def test_direction_outbound_only_when_provider_configured_for_sent_mailbox():
    """A provider instance explicitly constructed for the real,
    authenticated Sent-mail folder (`trusted_outbound=True`) classifies
    every message it fetches as OUTBOUND — regardless of the message's
    own `From` header content, which is never consulted at all."""
    raw = _build_email(sender="anyone@example.com")
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client, mailbox="[Gmail]/Sent Mail", trusted_outbound=True)

    result = await provider.fetch()

    assert result.messages[0].direction == "OUTBOUND"


@pytest.mark.asyncio
async def test_spoofed_from_header_in_inbox_is_never_trusted_as_outbound():
    """S7E-001 regression: a header-spoofed message claiming `From:
    <our own address>` that arrives in the primary (non-Sent) mailbox
    must NEVER be classified OUTBOUND — this is exactly the attack this
    remediation closes (a spoofed message could otherwise become the
    trusted anchor/recipient source for a Stage 7E follow-up send)."""
    spoofed = _build_email(sender=ACCOUNT, subject="Re: totally legitimate")
    client = FakeImapClient(messages={1: spoofed})
    provider = _provider(client, mailbox="INBOX", trusted_outbound=False)

    result = await provider.fetch()

    assert result.messages[0].direction == "INBOUND"


# ---------------------------------------------------------------------------
# GMAIL-004: MIME attachment subtree isolation
# ---------------------------------------------------------------------------


def _rfc822_attachment(inner: MIMEText | MIMEMultipart, filename: str | None = "original.eml"):
    part = MIMEMessage(inner)
    if filename is not None:
        part.add_header("Content-Disposition", "attachment", filename=filename)
    return part


@pytest.mark.asyncio
async def test_message_rfc822_attachment_does_not_leak_into_body():
    inner = MIMEText(
        "This is the ORIGINAL forwarded email body — must never leak.", "plain", "utf-8"
    )
    inner["Subject"] = "Original conversation"
    attachment = _rfc822_attachment(inner)

    raw = _build_email(
        plaintext_body=None,
        html_body="<p>Please see forwarded email</p>",
        extra_parts=[attachment],
    )
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_plain == ""
    assert message.has_html is True
    assert len(message.attachments) == 1
    assert message.attachments[0].content_type == "message/rfc822"


@pytest.mark.asyncio
async def test_nested_message_rfc822_attachment_does_not_crash():
    innermost = MIMEText("innermost body", "plain", "utf-8")
    nested = _rfc822_attachment(innermost, filename="innermost.eml")
    wrapper = MIMEMultipart("mixed")
    wrapper.attach(MIMEText("middle layer", "plain", "utf-8"))
    wrapper.attach(nested)
    outer_attachment = _rfc822_attachment(wrapper, filename="outer.eml")

    raw = _build_email(plaintext_body="Top-level body", extra_parts=[outer_attachment])
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_plain == "Top-level body"
    assert len(message.attachments) == 1
    assert message.attachments[0].content_type == "message/rfc822"


@pytest.mark.asyncio
async def test_multipart_attachment_is_isolated():
    multipart_attachment = MIMEMultipart("mixed")
    multipart_attachment.attach(MIMEText("hidden inner text", "plain", "utf-8"))
    multipart_attachment.add_header("Content-Disposition", "attachment", filename="bundle.mixed")

    raw = _build_email(plaintext_body="Visible body", extra_parts=[multipart_attachment])
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_plain == "Visible body"
    assert len(message.attachments) == 1


@pytest.mark.asyncio
async def test_text_plain_attachment_is_not_treated_as_body():
    raw = _build_email(
        plaintext_body=None,
        attachments=[("notes.txt", "text/plain", b"attachment file content")],
    )
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_plain == ""
    assert len(message.attachments) == 1
    assert message.attachments[0].filename == "notes.txt"


@pytest.mark.asyncio
async def test_attachment_without_filename_is_isolated():
    part = MIMEText("attachment body with no filename", "plain", "utf-8")
    part.add_header("Content-Disposition", "attachment")

    raw = _build_email(plaintext_body="Real body", extra_parts=[part])
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_plain == "Real body"
    assert len(message.attachments) == 1
    assert message.attachments[0].filename is None


@pytest.mark.asyncio
async def test_multipart_related_container_is_not_treated_as_attachment():
    related = MIMEMultipart("related")
    related.attach(MIMEText("related body text", "plain", "utf-8"))

    raw = _build_email(plaintext_body=None, extra_parts=[related])
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    message = result.messages[0]
    assert message.body_plain == "related body text"
    assert message.attachments == ()


# ---------------------------------------------------------------------------
# AUD-001 (TLS certificate verification) / AUD-005 (unbounded IMAP ops).
# ---------------------------------------------------------------------------


class TestConnectSecurityHardening:
    def test_connect_passes_a_verifying_ssl_context_and_a_finite_timeout(self, monkeypatch):
        captured = {}

        class _StubClient:
            def login(self, user, password):
                return ("OK", [b"LOGIN completed"])

        def _fake_imap4_ssl(host, port, *, ssl_context=None, timeout=None):
            captured["host"] = host
            captured["port"] = port
            captured["ssl_context"] = ssl_context
            captured["timeout"] = timeout
            return _StubClient()

        monkeypatch.setattr(gmail_imap_module.imaplib, "IMAP4_SSL", _fake_imap4_ssl)
        provider = _provider(client=None)

        provider._connect()

        assert isinstance(captured["ssl_context"], ssl.SSLContext)
        assert captured["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert captured["ssl_context"].check_hostname is True
        assert captured["timeout"] == IMAP_OPERATION_TIMEOUT_SECONDS

    def test_hung_imap_peer_raises_within_bounded_time_not_indefinitely(self, monkeypatch):
        """A REAL socket, not a mock: a listener that accepts the
        connection and then sends nothing at all (simulating a
        black-holed/hung IMAP peer during the TLS handshake). Proves the
        actual mechanism -- not merely that a `timeout=` kwarg is passed
        -- raises well within bounds instead of hanging the worker
        thread indefinitely (AUD-005: an asyncio-level timeout wrapped
        around a thread running this call could not itself unblock or
        cancel it)."""
        monkeypatch.setattr(gmail_imap_module, "IMAP_OPERATION_TIMEOUT_SECONDS", 0.3)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()
        accepted = threading.Event()

        def _accept_and_hang():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            accepted.set()
            time.sleep(2)
            conn.close()

        server_thread = threading.Thread(target=_accept_and_hang, daemon=True)
        server_thread.start()
        try:
            provider = _provider(client=None, imap_host=host, imap_port=port)

            start = time.monotonic()
            with pytest.raises(GmailConnectionError):
                provider._connect()
            elapsed = time.monotonic() - start

            assert accepted.wait(timeout=2), "test server never accepted the connection"
            assert elapsed < 2.0, f"the hard timeout did not bound the hang (took {elapsed:.2f}s)"
        finally:
            server.close()
            server_thread.join(timeout=3)


# ---------------------------------------------------------------------------
# AUD-005 (total wall-clock session deadline, not just per-read inactivity).
# The mechanism itself (ImapSessionDeadline: a slow-drip peer bounded by
# TOTAL elapsed time, and a fully silent peer bounded) is unit-tested
# directly in tests/test_imap_deadline.py. These tests cover how
# GmailImapProvider wires that mechanism in.
# ---------------------------------------------------------------------------


class _AlreadyExceededDeadline:
    """Stands in for an ImapSessionDeadline that has already fired by the
    time the per-UID fetch loop runs -- lets the "deadline exceeded mid
    session" control-flow path be tested deterministically, without
    relying on real timing.
    """

    def __init__(self, _total_seconds: float) -> None:
        pass

    def bind_socket(self, sock, *, extra_closable=None) -> None:
        pass

    @property
    def exceeded(self) -> bool:
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _ExceedsAfterFirstCheckDeadline:
    """NEW-001 (Astra R4A): stands in for a deadline that has NOT yet
    fired for the loop's first iteration, but HAS by the second -- lets
    "one message completed, then the deadline fires before the next"
    be tested deterministically. `.exceeded` is read once per loop
    iteration (top-of-loop check) plus once more in the post-loop check,
    so returning False only for the very first read and True for every
    read after that reproduces exactly "UID 1 was already in flight/done
    when time ran out for UID 2".
    """

    def __init__(self, _total_seconds: float) -> None:
        self._check_count = 0

    def bind_socket(self, sock, *, extra_closable=None) -> None:
        pass

    @property
    def exceeded(self) -> bool:
        self._check_count += 1
        return self._check_count > 1

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _make_exceeds_after_n_checks_deadline_class(threshold: int):
    """Codex gate follow-up (Astra R4A MEDIUM): generalizes
    `_ExceedsAfterFirstCheckDeadline` to an arbitrary threshold -- lets a
    test place the deadline's transition to "exceeded" at an exact call
    number, e.g. to prove "UID 1's own top-of-loop check reads False,
    UID 2's ALSO reads False (so a fetch is genuinely attempted), and
    only the except-handler's read -- checked after a simulated
    mid-FETCH OSError -- reads True". Returns a class (not an instance)
    so it can directly replace `ImapSessionDeadline` via monkeypatch.
    """

    class _Deadline:
        def __init__(self, _total_seconds: float) -> None:
            self._check_count = 0

        def bind_socket(self, sock, *, extra_closable=None) -> None:
            pass

        @property
        def exceeded(self) -> bool:
            self._check_count += 1
            return self._check_count > threshold

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    return _Deadline


class TestSessionDeadlineWiring:
    def test_connect_passes_a_verifying_ssl_context_to_deadline_imap4ssl(self, monkeypatch):
        """AUD-001 must remain true for the path production actually uses
        (owns_connection=True, deadline is not None): `_connect(deadline)`
        must construct `DeadlineIMAP4SSL` with the same verifying
        ssl_context/finite timeout as the plain `imaplib.IMAP4_SSL` path
        above, plus the given deadline. Real socket binding is
        DeadlineIMAP4SSL's own concern, covered by its dedicated tests in
        tests/test_imap_deadline.py -- this test only checks wiring.
        """
        captured = {}

        class _StubClient:
            def login(self, user, password):
                return ("OK", [b"LOGIN completed"])

        def _stub_deadline_client(host, port, *, ssl_context, timeout, deadline):
            captured["ssl_context"] = ssl_context
            captured["timeout"] = timeout
            captured["deadline"] = deadline
            return _StubClient()

        monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", _stub_deadline_client)
        provider = _provider(client=None)
        deadline = ImapSessionDeadline(5.0)

        provider._connect(deadline)

        assert isinstance(captured["ssl_context"], ssl.SSLContext)
        assert captured["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert captured["ssl_context"].check_hostname is True
        assert captured["timeout"] == IMAP_OPERATION_TIMEOUT_SECONDS
        assert captured["deadline"] is deadline

    def test_connect_without_a_deadline_does_not_require_one(self, monkeypatch):
        """`deadline` defaults to None -- existing callers/tests that call
        `_connect()` with no argument (e.g. the AUD-001 tests above) must
        keep working unchanged, still via plain imaplib.IMAP4_SSL."""

        class _StubClient:
            def login(self, user, password):
                return ("OK", [b"LOGIN completed"])

        monkeypatch.setattr(gmail_imap_module.imaplib, "IMAP4_SSL", lambda *a, **kw: _StubClient())
        provider = _provider(client=None)

        client = provider._connect()

        assert isinstance(client, _StubClient)

    @pytest.mark.asyncio
    async def test_normal_fetch_succeeds_with_the_session_deadline_active(self, monkeypatch):
        """Wiring the deadline in for an owned connection must not disturb
        a normal, fast, successful fetch."""
        raw = _build_email(plaintext_body="hi")
        fake_client = FakeImapClient(messages={1: raw})

        monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        provider = _provider(client=None)

        result = await provider.fetch()

        assert len(result.messages) == 1
        assert fake_client.closed is True
        assert fake_client.logged_out is True

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_partial_result_when_deadline_already_exceeded(
        self, monkeypatch
    ):
        """NEW-001 (Astra R4A): if the total session deadline has already
        fired by the time the per-UID fetch loop runs, the sync must stop
        -- but must NOT raise. It returns a `GmailFetchResult` with no
        messages (nothing could complete) and `deadline_exceeded=True`,
        never a discarded/opaque exception. A prior version of this
        provider raised `GmailConnectionError` here; that silently
        discarded any work that DID complete before the deadline in the
        general case (see the partial-batch test below) -- even in this
        zero-messages-completed edge case, an explicit partial result is
        more honest than an exception, since the caller
        (app.services.gmail_inbox) can now distinguish "genuinely nothing
        got done, time ran out" from a real connection/auth failure.
        """
        raw = _build_email(plaintext_body="hi")
        fake_client = FakeImapClient(messages={1: raw})

        monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        monkeypatch.setattr(gmail_imap_module, "ImapSessionDeadline", _AlreadyExceededDeadline)
        provider = _provider(client=None)

        result = await provider.fetch()

        assert result.messages == ()
        assert result.deadline_exceeded is True

        # Connection cleanup must still run even though the fetch loop
        # never got to run.
        assert fake_client.closed is True
        assert fake_client.logged_out is True

    @pytest.mark.asyncio
    async def test_fetch_persists_completed_messages_when_deadline_fires_mid_loop(
        self, monkeypatch
    ):
        """NEW-001 (Astra R4A) core regression: two messages are due; the
        deadline fires only AFTER the first one has already completed
        fetch+parse. That completed message must still come back in
        `result.messages` -- never discarded merely because the SECOND
        message's turn never arrived before time ran out.
        """
        raw1 = _build_email(plaintext_body="first", message_id="<msg1@example.com>")
        raw2 = _build_email(plaintext_body="second", message_id="<msg2@example.com>")
        fake_client = FakeImapClient(messages={1: raw1, 2: raw2})

        monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        monkeypatch.setattr(
            gmail_imap_module, "ImapSessionDeadline", _ExceedsAfterFirstCheckDeadline
        )
        provider = _provider(client=None)

        result = await provider.fetch()

        assert len(result.messages) == 1
        assert result.messages[0].body_plain == "first"
        assert result.deadline_exceeded is True
        # UID 2 was never even attempted -- the loop broke before it.
        fetch_uids = [args[0] for command, args in fake_client.uid_calls if command == "fetch"]
        assert b"2" not in fetch_uids

        # Connection cleanup must still run.
        assert fake_client.closed is True
        assert fake_client.logged_out is True

    @pytest.mark.asyncio
    async def test_next_cycle_retries_only_the_deadline_skipped_uid(self, monkeypatch):
        """NEW-001 (Astra R4A): after a deadline-partial fetch, the NEXT
        sync must not re-fetch (and so must not risk duplicating) the
        message that already completed and was persisted -- only the
        UID the deadline left behind is attempted again. `get_known_uids`
        is exactly the existing GMAIL-005/012 starvation-protection
        closure `app.services.gmail_sync.make_gmail_provider` binds to
        already-persisted UIDs; this proves the NEW-001 fix composes with
        it correctly rather than needing a new idempotency mechanism.
        """
        raw1 = _build_email(plaintext_body="first", message_id="<msg1@example.com>")
        raw2 = _build_email(plaintext_body="second", message_id="<msg2@example.com>")
        fake_client = FakeImapClient(messages={1: raw1, 2: raw2})

        monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        # No deadline pressure this time -- simulates the retry cycle,
        # with UID 1 now reported as already-known (persisted by the
        # prior, deadline-truncated run).
        provider = _provider(client=None, get_known_uids=lambda _uid_validity, _uids: {1})

        result = await provider.fetch()

        assert len(result.messages) == 1
        assert result.messages[0].body_plain == "second"
        fetch_uids = [args[0] for command, args in fake_client.uid_calls if command == "fetch"]
        assert b"1" not in fetch_uids
        assert b"2" in fetch_uids

    @pytest.mark.asyncio
    async def test_fetch_preserves_messages_when_deadline_fires_mid_fetch(self, monkeypatch):
        """Codex gate follow-up (Astra R4A MEDIUM): unlike the
        between-iterations case above, here the deadline fires WHILE a
        FETCH is already blocked in flight -- the watchdog force-closes
        the socket mid-call, so `client.uid("fetch", ...)` itself raises
        OSError rather than the top-of-loop `.exceeded` check catching
        it cleanly beforehand. UID 1 must still complete normally, and
        UID 1's message must still be returned -- never discarded merely
        because UID 2's in-flight FETCH is what actually observed the
        deadline. Critically, UID 2 must be reported as `skipped_count
        == 0` (deadline-truncated, still pending, will be retried), NOT
        folded into an ordinary "unparseable message" skip count -- a
        prior version of `_fetch_one` swallowed ANY exception (including
        OSError) as a plain skip, which happened to still preserve UID
        1's message in this exact scenario but silently mis-classified a
        real transport failure as "message 2 was bad" instead of "time
        ran out" (see `test_genuine_transport_failure_without_deadline_
        still_raises` below for the case that actually distinguishes the
        swallowing bug on its own).
        """
        raw1 = _build_email(plaintext_body="first", message_id="<msg1@example.com>")
        raw2 = _build_email(plaintext_body="second", message_id="<msg2@example.com>")
        fake_client = FakeImapClient(messages={1: raw1, 2: raw2}, raise_oserror_on_uid={2})

        monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        # Both UIDs' own top-of-loop checks read False (a fetch is
        # genuinely attempted for each) -- only the except-handler's
        # read, right after UID 2's simulated mid-FETCH OSError, reads
        # True.
        monkeypatch.setattr(
            gmail_imap_module,
            "ImapSessionDeadline",
            _make_exceeds_after_n_checks_deadline_class(2),
        )
        provider = _provider(client=None)

        result = await provider.fetch()

        assert len(result.messages) == 1
        assert result.messages[0].body_plain == "first"
        assert result.deadline_exceeded is True
        assert result.skipped_count == 0, (
            "UID 2 must be treated as deadline-truncated (never attempted a full "
            "classification), never counted as an ordinary skipped/unparseable message"
        )

    @pytest.mark.asyncio
    async def test_genuine_transport_failure_without_deadline_still_raises(self, monkeypatch):
        """Codex gate follow-up (Astra R4A MEDIUM): a real, unexpected
        connection failure mid-FETCH -- NOT caused by the session
        deadline -- must still raise `GmailConnectionError` normally.
        The deadline-preservation behavior above must never mask a
        genuine transport/protocol failure as an ordinary skipped
        message.
        """
        raw1 = _build_email(plaintext_body="first", message_id="<msg1@example.com>")
        fake_client = FakeImapClient(messages={1: raw1}, raise_oserror_on_uid={1})

        monkeypatch.setattr(gmail_imap_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        # A real ImapSessionDeadline (never fires within this fast test)
        # -- `.exceeded` stays False throughout, so the OSError below is
        # unambiguously a genuine failure, not a deadline artifact.
        provider = _provider(client=None)

        with pytest.raises(GmailConnectionError):
            await provider.fetch()

        assert fake_client.closed is True
        assert fake_client.logged_out is True
