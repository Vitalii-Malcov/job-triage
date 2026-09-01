"""Tests for app.providers.email.imap.GmailImapProvider (Stage 7A + the
Stage 7A security fix round — GMAIL-001/004/005/009/010).

Mirrors tests/test_collectors_xing_email.py's approach: a lightweight fake
IMAP client (no real socket/network I/O anywhere), plus a source-inspection
test asserting this package has no means to make an HTTP request at all.
"""

import email.errors
import imaplib
import inspect
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
from app.providers.email.imap import GmailImapProvider

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
    ) -> None:
        self._messages = messages or {}
        self._uid_validity = uid_validity
        self._select_typ = select_typ
        self._status_data = status_data
        self._search_typ = search_typ
        self._search_uids = search_uids
        self._size_override = size_override or {}
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
            return ("OK", [(b"%d (UID %d BODY[] {%d}" % (uid, uid, len(raw)), raw)])
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

    def fake_ssl(host, port):
        return RejectingClient()

    monkeypatch.setattr(gmail_imap_module.imaplib, "IMAP4_SSL", fake_ssl)
    provider = _provider(None)

    with pytest.raises(GmailAuthError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_connect_os_error_raises_connection_error(monkeypatch):
    def fake_ssl(host, port):
        raise OSError("network unreachable")

    monkeypatch.setattr(gmail_imap_module.imaplib, "IMAP4_SSL", fake_ssl)
    provider = _provider(None)

    with pytest.raises(GmailConnectionError):
        await provider.fetch()


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

    def fake_ssl(host, port):
        return fake_client

    monkeypatch.setattr(gmail_imap_module.imaplib, "IMAP4_SSL", fake_ssl)
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
    assert "(BODY.PEEK[])" in fetch_items
    assert "(RFC822)" not in fetch_items
    assert "(BODY[])" not in fetch_items


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
        if cmd == "fetch" and len(args) > 1 and args[1] == "(BODY.PEEK[])"
    ]
    assert body_fetch_calls == [], "an oversized message's body must never be fetched at all"


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
        if command == "fetch" and len(args) > 1 and args[1] == "(BODY.PEEK[])":
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
        if command == "fetch" and len(args) > 1 and args[1] == "(BODY.PEEK[])" and args[0] == b"2":
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
async def test_direction_outbound_when_sender_is_account_address():
    raw = _build_email(sender=ACCOUNT)
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

    result = await provider.fetch()

    assert result.messages[0].direction == "OUTBOUND"


@pytest.mark.asyncio
async def test_direction_inbound_when_sender_is_not_account_address():
    raw = _build_email(sender="recruiter@company.example")
    client = FakeImapClient(messages={1: raw})
    provider = _provider(client)

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
