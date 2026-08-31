"""Tests for app.providers.email.imap.GmailImapProvider (Stage 7A).

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
    """

    def __init__(
        self,
        messages: dict[int, bytes] | None = None,
        uid_validity: int = 100,
        select_typ: str = "OK",
        status_data: list[bytes] | None = None,
        search_typ: str = "OK",
    ) -> None:
        self._messages = messages or {}
        self._uid_validity = uid_validity
        self._select_typ = select_typ
        self._status_data = status_data
        self._search_typ = search_typ
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
            uids = b" ".join(str(u).encode() for u in sorted(self._messages))
            return ("OK", [uids])
        if command == "fetch":
            uid = int(args[0])
            raw = self._messages.get(uid)
            if raw is None:
                return ("OK", [None])
            return ("OK", [(b"1 (RFC822 {%d}" % len(raw), raw)])
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
) -> bytes:
    if html_body is not None or attachments:
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


@pytest.mark.asyncio
async def test_malformed_fetch_response_is_skipped_not_raised():
    client = FakeImapClient(messages={})
    # Simulate a UID present in SEARCH results but missing from FETCH
    # (e.g. deleted between SEARCH and FETCH) by overriding the uid()
    # search result to advertise a uid with no backing message.
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
