import imaplib
import inspect
import socket
import ssl
import threading
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

import app.collectors.xing_email as xing_email_module
from app.collectors.xing_email import (
    IMAP_OPERATION_TIMEOUT_SECONDS,
    XingAuthError,
    XingConnectionError,
    XingEmailCollector,
)
from app.providers.email.imap_deadline import ImapSessionDeadline

XING_SENDER = "jobs@mail.xing.com"

SEPARATOR = "-" * 40

BLOCK_WITH_ALL_OPTIONAL_FIELDS = """Bis 18% mehr Gehalt
(Junior) Software Entwickler (m/w/d)
=> https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1

CAREL Deutschland GmbH
Gelnhausen

44.000 € - 59.000 €bevorzugtesTätigkeitsfeldKarriere-Stufe
Vollzeit"""

BLOCK_WITHOUT_OPTIONAL_FIELDS = """Junior Informatiker (m/w/d)
=> https://www.xing.com/m/BBBBBBBBBBBBBBBBBBBB2

Institut für Kommunikations- und Prüfungsforschung gGmbH
Heidelberg"""

FOOTER_BLOCK = """Alle Suchergebnisse anzeigen
=> https://www.xing.com/jobs/search?query=python"""

# Real digest shape for KPMG's "(Junior) Consultant AI Security &
# Governance" posting: no salary line at all, so the decorative
# "Karriere-Stufe" badge and the real employment type each land on their
# own line instead of being concatenated onto a salary line.
KPMG_BLOCK = """(Junior) Consultant AI Security & Governance
=> https://www.xing.com/m/CCCCCCCCCCCCCCCCCCCC3

KPMG
Frankfurt am Main

Karriere-Stufe
Vollzeit"""

BLOCK_WITH_ONLY_DECORATIVE_TAGS = """Werkstudent Data Engineering
=> https://www.xing.com/m/DDDDDDDDDDDDDDDDDDDD4

Example Analytics GmbH
Munich

Karriere-Stufe
bevorzugtes Tätigkeitsfeld"""


def _digest_body(*blocks: str, separator_repeat: int = 1) -> str:
    separator = ("\n" + SEPARATOR) * separator_repeat
    return ("\n" + separator + "\n").join(["Hallo, hier sind deine neuen Jobs:", *blocks, ""])


def _build_email(
    sender: str,
    subject: str,
    plaintext_body: str,
    message_id: str = "<default@mail.xing.com>",
    html_body: str | None = None,
) -> bytes:
    if html_body is not None:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(plaintext_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(plaintext_body, "plain", "utf-8")
    msg["From"] = sender
    msg["Subject"] = Header(subject, "utf-8").encode()
    msg["Message-ID"] = message_id
    return msg.as_bytes()


class FakeImapClient:
    """Minimal fake matching the subset of imaplib.IMAP4_SSL this collector
    uses. No real socket/network I/O anywhere in this class.
    """

    def __init__(
        self,
        messages: list[bytes],
        *,
        raise_oserror_on_message_set: set[bytes] | None = None,
        raise_abort_on_message_set: set[bytes] | None = None,
        uid_validity: int = 1,
    ) -> None:
        self._messages = messages
        # Codex gate follow-up (Astra R4A HIGH): a fetch for any
        # message_set in this set raises OSError -- lets a test
        # deterministically simulate a transport-level failure
        # (including "the deadline watchdog force-closed the socket
        # mid-FETCH") without any real socket/timing involved.
        self._raise_oserror_on_message_set = raise_oserror_on_message_set or set()
        # FINAL-001 (Astra R5A): same idea, but raises imaplib.IMAP4.abort
        # instead -- NOT an OSError subclass, but exactly what a real
        # deadline-forced socket close surfaces as when it interrupts
        # imaplib's own buffered readline() mid-FETCH (imaplib's
        # readline() catches the underlying ConnectionError itself and
        # turns it into a clean-EOF abort -- see
        # app.collectors.xing_email._fetch_sync's own except clause for
        # the confirmed source-level detail).
        self._raise_abort_on_message_set = raise_abort_on_message_set or set()
        # Codex gate follow-up (Astra R4A MEDIUM, starvation): this fake
        # uses UID == 1-based list index throughout (both `.uid("search",
        # ...)` and `.uid("fetch", ...)` delegate straight to the
        # existing `search`/`fetch` below) -- realistic enough to
        # exercise the real collector's UID-based scan-position logic
        # while keeping every existing byte-string assertion in this
        # file (e.g. `fetch_calls == [b"1", b"1"]`) meaningful unchanged.
        self.uid_validity = uid_validity
        self.select_calls: list[tuple[str, bool]] = []
        self.search_calls: list[tuple[str, ...]] = []
        self.fetch_calls: list = []
        self.status_calls: list[tuple[str, str]] = []
        self.closed = False
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return ("OK", [str(len(self._messages)).encode()])

    def status(self, mailbox: str, names: str) -> tuple[str, list[bytes]]:
        self.status_calls.append((mailbox, names))
        line = f"INBOX (UIDVALIDITY {self.uid_validity})".encode()
        return ("OK", [line])

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
        self.search_calls.append(criteria)
        numbers = b" ".join(str(i + 1).encode() for i in range(len(self._messages)))
        return ("OK", [numbers])

    def fetch(self, message_set, message_parts: str) -> tuple[str, list]:
        self.fetch_calls.append(message_set)
        if message_set in self._raise_oserror_on_message_set:
            raise OSError("simulated transport failure")
        if message_set in self._raise_abort_on_message_set:
            raise imaplib.IMAP4.abort("simulated deadline-forced abort")
        index = int(message_set) - 1
        raw = self._messages[index]
        return ("OK", [(b"1 (RFC822 {%d}" % len(raw), raw)])

    def uid(self, command: str, *args) -> tuple[str, list]:
        if command == "search":
            # args = (charset, criteria...) -- same shape `search` takes.
            return self.search(*args)
        if command == "fetch":
            # Deliberately NOT decoding `message_set` -- it stays exactly
            # the bytes the production collector passed (e.g. b"1"), so
            # `fetch_calls`/`raise_oserror_on_message_set` behave
            # identically to before this fake gained UID support.
            message_set, message_parts = args
            return self.fetch(message_set, message_parts)
        raise NotImplementedError(f"FakeImapClient.uid: unsupported command {command!r}")

    def close(self) -> tuple[str, list[bytes]]:
        self.closed = True
        return ("OK", [b"CLOSE completed"])

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return ("OK", [b"BYE"])


def _make_collector(messages: list[bytes], **kwargs) -> tuple[XingEmailCollector, FakeImapClient]:
    client = FakeImapClient(messages)
    collector = XingEmailCollector(
        imap_host="imap.example.com",
        imap_port=993,
        username="user@example.com",
        app_password="app-password",
        imap_client=client,
        **kwargs,
    )
    return collector, client


# ---------------------------------------------------------------------------
# Hard security constraint: no HTTP client anywhere in this module.
# ---------------------------------------------------------------------------


def test_module_never_imports_an_http_client():
    """The tracking URLs in XING digest emails are personal recruiter-view
    redirects (see module docstring) — resolving them is a real side effect
    on a third party, not an inert read. This collector must not have the
    means to make an HTTP request at all: no httpx/requests/aiohttp/urllib
    import anywhere in the module.
    """
    source = inspect.getsource(xing_email_module)
    forbidden_imports = ["httpx", "requests", "aiohttp", "urllib.request", "http.client"]
    for name in forbidden_imports:
        assert f"import {name}" not in source, f"module must not import {name}"

    for name in ("httpx", "requests", "aiohttp"):
        assert not hasattr(xing_email_module, name)


@pytest.mark.asyncio
async def test_tracking_url_is_stored_verbatim_and_never_fetched():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw = _build_email(XING_SENDER, "3 neue Stellenangebote für Python", body)
    collector, client = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert str(jobs[0].url) == "https://www.xing.com/m/BBBBBBBBBBBBBBBBBBBB2"
    # FakeImapClient never performs any HTTP call by construction (it only
    # implements IMAP methods) — the only "requests" that happened at all
    # were IMAP protocol calls to the mocked mailbox, not to the job URL.


# ---------------------------------------------------------------------------
# Subject / sender filtering.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_processes_both_known_digest_subject_patterns():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw1 = _build_email(
        XING_SENDER,
        "3 neue Stellenangebote für Python Entwickler in Heidelberg",
        body,
        message_id="<digest-1@mail.xing.com>",
    )
    raw2 = _build_email(
        XING_SENDER,
        "Entdecke ähnliche Jobs wie Junior Informatiker (m/w/d)",
        body,
        message_id="<digest-2@mail.xing.com>",
    )
    collector, _ = _make_collector([raw1, raw2])

    jobs = await collector.fetch()

    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_skips_non_job_digest_subjects_and_senders_without_crashing():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    wochencheck = _build_email(
        "mailrobot@mail.xing.com", "Dein Wochencheck", body, message_id="<w@mail.xing.com>"
    )
    news = _build_email(
        "news@mail.xing.com", "Neuigkeiten aus deiner Branche", body, message_id="<n@mail.xing.com>"
    )
    unrelated_subject = _build_email(
        XING_SENDER, "Dein XING-Profil wurde besucht", body, message_id="<v@mail.xing.com>"
    )
    collector, _ = _make_collector([wochencheck, news, unrelated_subject])

    jobs = await collector.fetch()

    assert jobs == []


# ---------------------------------------------------------------------------
# Block parsing: variable separators, optional fields, invalid blocks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parses_block_with_all_optional_fields_present():
    body = _digest_body(BLOCK_WITH_ALL_OPTIONAL_FIELDS)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Software Entwickler", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "(Junior) Software Entwickler (m/w/d)"
    assert job.company == "CAREL Deutschland GmbH"
    assert job.location == "Gelnhausen"
    assert str(job.url) == "https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1"
    assert "44.000" in job.description
    assert "Vollzeit" in job.description


@pytest.mark.asyncio
async def test_parses_block_with_no_optional_fields_present():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw = _build_email(XING_SENDER, "2 neue Stellenangebote für Informatiker", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Junior Informatiker (m/w/d)"
    assert job.company == "Institut für Kommunikations- und Prüfungsforschung gGmbH"
    assert job.location == "Heidelberg"
    assert job.description == ""


@pytest.mark.asyncio
async def test_parses_block_with_no_salary_and_standalone_occupation_badge():
    # Real KPMG digest shape: no salary line, so "Karriere-Stufe" (decorative)
    # and "Vollzeit" (real employment type) each sit on their own line. The
    # employment type must be matched against the known-values list, not
    # taken as "the first non-salary tail line" — that bug picked
    # "Karriere-Stufe" and lost "Vollzeit" entirely.
    body = _digest_body(KPMG_BLOCK)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Consultant", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "(Junior) Consultant AI Security & Governance"
    assert job.company == "KPMG"
    assert job.location == "Frankfurt am Main"
    assert job.description == "Beschäftigung: Vollzeit"
    assert "Karriere-Stufe" not in job.description
    assert "Karriere-Stufe" not in job.title
    assert "Karriere-Stufe" not in job.company
    assert "Karriere-Stufe" not in job.location


@pytest.mark.asyncio
async def test_parses_block_with_multiple_decorative_tags_and_no_employment_type():
    # Tail lines are all decorative badges — none matches a known employment
    # type. The block is still a valid posting (title/company/location are
    # fine); description must simply omit "Beschäftigung: ..." rather than
    # guess at one of the decorative tags.
    body = _digest_body(BLOCK_WITH_ONLY_DECORATIVE_TAGS)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Data Engineering", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Example Analytics GmbH"
    assert job.location == "Munich"
    assert "Beschäftigung:" not in job.description
    assert job.description == ""


@pytest.mark.asyncio
async def test_handles_variable_number_of_separator_lines_between_blocks():
    body = _digest_body(
        BLOCK_WITH_ALL_OPTIONAL_FIELDS, BLOCK_WITHOUT_OPTIONAL_FIELDS, separator_repeat=2
    )
    raw = _build_email(XING_SENDER, "2 neue Stellenangebote für Python", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_skips_footer_block_without_crashing():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS, FOOTER_BLOCK)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Python", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    # 2 skips, not just the footer: _digest_body's greeting line before the
    # first separator is itself a "block" with no tracking line, matching
    # real digests where preamble text precedes the first separator too.
    assert collector.skipped_invalid_count == 2


@pytest.mark.asyncio
async def test_email_with_no_valid_job_blocks_yields_empty_list_not_exception():
    # Only the footer/"show all" block and a greeting line — no real posting.
    body = _digest_body(FOOTER_BLOCK)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Python", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert jobs == []
    assert collector.skipped_invalid_count >= 1


@pytest.mark.asyncio
async def test_reads_plaintext_part_of_multipart_message():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    html = "<html><body>Some HTML the collector must ignore</body></html>"
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Python", body, html_body=html)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert jobs[0].company == "Institut für Kommunikations- und Prüfungsforschung gGmbH"


# ---------------------------------------------------------------------------
# Read-only IMAP access.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_is_called_readonly():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Python", body)
    collector, client = _make_collector([raw])

    await collector.fetch()

    assert client.select_calls == [("INBOX", True)]


# ---------------------------------------------------------------------------
# Message-ID based deduplication.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_message_id_is_not_reprocessed_on_second_fetch():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw = _build_email(
        XING_SENDER,
        "1 neue Stellenangebote für Python",
        body,
        message_id="<digest-1@mail.xing.com>",
    )
    processed: set[str] = set()
    collector, client = _make_collector(
        [raw],
        is_message_processed=lambda message_id: message_id in processed,
    )

    first_batches = await collector.fetch_message_batches()
    first = [job for batch in first_batches for job in batch.jobs]
    assert len(first_batches) == 1

    # A persistence-owning caller acknowledges only after all jobs in this
    # batch succeed. The collector itself deliberately never acknowledges.
    processed.add(first_batches[0].message_id)
    second = await collector.fetch()

    assert len(first) == 1
    assert second == []


# ---------------------------------------------------------------------------
# Auth / configuration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_raises_auth_error_when_not_configured():
    collector = XingEmailCollector(
        imap_host="imap.example.com",
        imap_port=993,
        username="",
        app_password="",
    )

    with pytest.raises(XingAuthError):
        await collector.fetch()


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

        monkeypatch.setattr(xing_email_module.imaplib, "IMAP4_SSL", _fake_imap4_ssl)
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        collector._connect()

        assert isinstance(captured["ssl_context"], ssl.SSLContext)
        assert captured["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert captured["ssl_context"].check_hostname is True
        assert captured["timeout"] == IMAP_OPERATION_TIMEOUT_SECONDS

    def test_hung_imap_peer_raises_within_bounded_time_not_indefinitely(self):
        """A REAL socket, not a mock: a listener that accepts the
        connection and then sends nothing at all (simulating a
        black-holed/hung IMAP peer during the TLS handshake). Proves the
        actual mechanism -- not merely that a `timeout=` kwarg is passed
        -- raises well within bounds instead of hanging the worker
        thread indefinitely (AUD-005: an asyncio-level timeout wrapped
        around a thread running this call could not itself unblock it)."""
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
            collector = XingEmailCollector(
                imap_host=host,
                imap_port=port,
                username="user@example.com",
                app_password="app-password",
            )
            # _connect() reads the module-level constant directly (not an
            # instance attribute), so a short bound for this test is set
            # by patching the module, restored in `finally`.
            original_timeout = xing_email_module.IMAP_OPERATION_TIMEOUT_SECONDS
            xing_email_module.IMAP_OPERATION_TIMEOUT_SECONDS = 0.3
            start = time.monotonic()
            try:
                with pytest.raises(XingConnectionError):
                    collector._connect()
            finally:
                xing_email_module.IMAP_OPERATION_TIMEOUT_SECONDS = original_timeout
            elapsed = time.monotonic() - start

            assert accepted.wait(timeout=2), "test server never accepted the connection"
            assert elapsed < 2.0, f"the hard timeout did not bound the hang (took {elapsed:.2f}s)"
        finally:
            server.close()
            server_thread.join(timeout=3)

    def test_connect_os_error_does_not_leak_host_port_or_raw_exception_text(self, monkeypatch):
        """Codex final review, MEDIUM: `_connect()`'s raised
        XingConnectionError must never embed the configured host/port or
        the raw OSError text -- this exception's str() can reach an
        operator-visible surface (app/api/routes.py's `run_xing_collector`
        puts `str(exc)` straight into an HTTP 502 `detail`) as well as
        application logs. Mirrors GmailImapProvider's sanitization
        exactly (GMAIL-003). Uses deliberately distinctive fake sensitive
        values so a leak of ANY of them is unambiguous.
        """
        sensitive_host = "corp-mailserver-do-not-leak.internal"
        sensitive_port = 47993
        sensitive_exc_text = "SECRET_TOKEN_ABC123_LEAKED_IF_VISIBLE"

        def _fake_imap4_ssl(host, port, **kwargs):
            raise OSError(sensitive_exc_text)

        monkeypatch.setattr(xing_email_module.imaplib, "IMAP4_SSL", _fake_imap4_ssl)
        collector = XingEmailCollector(
            imap_host=sensitive_host,
            imap_port=sensitive_port,
            username="user@example.com",
            app_password="app-password",
        )

        with pytest.raises(XingConnectionError) as exc_info:
            collector._connect()

        message = str(exc_info.value)
        assert sensitive_host not in message
        assert str(sensitive_port) not in message
        assert sensitive_exc_text not in message

    def test_connect_login_rejected_does_not_leak_raw_exception_text(self, monkeypatch):
        """Same leak surface as above, for the login-rejected path
        (imaplib.IMAP4.error can carry server-echoed text)."""
        sensitive_exc_text = "SECRET_TOKEN_XYZ789_LEAKED_IF_VISIBLE"

        class _RejectingClient:
            def login(self, user, password):
                raise imaplib.IMAP4.error(sensitive_exc_text)

        monkeypatch.setattr(
            xing_email_module.imaplib, "IMAP4_SSL", lambda *a, **kw: _RejectingClient()
        )
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        with pytest.raises(XingAuthError) as exc_info:
            collector._connect()

        assert sensitive_exc_text not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_constructor_imap4_abort_raises_sanitized_connection_error(self, monkeypatch):
        """Codex final review, MEDIUM: DeadlineIMAP4SSL's constructor
        (TLS/greeting/CAPABILITY processing, or a deadline-triggered
        forced close mid-read) can raise imaplib.IMAP4.abort -- a plain
        Exception subclass, NOT an OSError -- carrying raw, potentially
        server-controlled text. Exercises the REAL production path
        (owns_connection=True, deadline is not None -> DeadlineIMAP4SSL),
        not the deadline=None path the OSError test above uses."""
        sensitive_host = "corp-mailserver-do-not-leak.internal"
        sensitive_port = 47993
        sensitive_exc_text = "SECRET_RAW_SERVER_TEXT"

        def fake_deadline_client(host, port, **kwargs):
            raise imaplib.IMAP4.abort(sensitive_exc_text)

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", fake_deadline_client)
        collector = XingEmailCollector(
            imap_host=sensitive_host,
            imap_port=sensitive_port,
            username="user@example.com",
            app_password="app-password",
        )

        with pytest.raises(XingConnectionError) as exc_info:
            await collector.fetch()

        message = str(exc_info.value)
        assert sensitive_exc_text not in message
        assert sensitive_host not in message
        assert str(sensitive_port) not in message

    @pytest.mark.asyncio
    async def test_constructor_imap4_error_raises_sanitized_connection_error(self, monkeypatch):
        """Same as above for the base imaplib.IMAP4.error (abort's
        parent class) -- e.g. a malformed/unexpected greeting or
        CAPABILITY response the constructor rejects outright."""
        sensitive_host = "corp-mailserver-do-not-leak.internal"
        sensitive_port = 47993
        sensitive_exc_text = "SECRET_RAW_SERVER_TEXT"

        def fake_deadline_client(host, port, **kwargs):
            raise imaplib.IMAP4.error(sensitive_exc_text)

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", fake_deadline_client)
        collector = XingEmailCollector(
            imap_host=sensitive_host,
            imap_port=sensitive_port,
            username="user@example.com",
            app_password="app-password",
        )

        with pytest.raises(XingConnectionError) as exc_info:
            await collector.fetch()

        message = str(exc_info.value)
        assert sensitive_exc_text not in message
        assert sensitive_host not in message
        assert str(sensitive_port) not in message

    @pytest.mark.asyncio
    async def test_login_rejected_with_imap4_error_still_raises_sanitized_auth_error(
        self, monkeypatch
    ):
        """The constructor-time imaplib.IMAP4.error handling added
        above must not swallow the EXPLICIT, later client.login()
        rejection into a connection error -- login rejection is a
        distinct, separately try/excepted block and must keep raising
        XingAuthError, sanitized exactly like the constructor path."""
        sensitive_exc_text = "SECRET_RAW_LOGIN_REJECTION_TEXT"

        class _RejectingClient:
            def login(self, user, password):
                raise imaplib.IMAP4.error(sensitive_exc_text)

        def fake_deadline_client(host, port, **kwargs):
            return _RejectingClient()

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", fake_deadline_client)
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        with pytest.raises(XingAuthError) as exc_info:
            await collector.fetch()

        assert sensitive_exc_text not in str(exc_info.value)

    def test_disconnect_close_failure_does_not_leak_via_exc_info(self, caplog):
        """Codex gate follow-up (Astra R4B, NEW-003: XING log leakage)
        regression: `_disconnect`'s close-failure log previously passed
        `exc_info=True`, which logs the FULL traceback INCLUDING the
        exception's own str() -- exactly the raw provider detail this
        module's other exception paths (see
        test_connect_raises_sanitized_connection_error_without_host_port_or_raw_text
        above) deliberately keep out of logs via `type(exc).__name__`
        alone. A `close()` failure on a real connection can be a raw
        `imaplib.IMAP4.error`/`OSError` carrying server-controlled or
        connection-internal text.
        """
        sensitive_exc_text = "SECRET_CLOSE_FAILURE_TEXT_MUST_NOT_LEAK"

        class _FailingCloseClient:
            def close(self):
                raise imaplib.IMAP4.error(sensitive_exc_text)

            def logout(self):
                return ("BYE", [b"logging out"])

        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        with caplog.at_level("DEBUG"):
            collector._disconnect(_FailingCloseClient())

        assert sensitive_exc_text not in caplog.text
        assert "xing_email_imap_close_failed" in caplog.text
        assert "error_type=IMAP4.error" in caplog.text or "error_type=error" in caplog.text
        # The actual mechanism, not just the text: no record on this
        # logger carries exc_info (a full traceback dump), for EITHER
        # phase of _disconnect -- exc_info=True is exactly what let the
        # raw exception text above leak before this fix.
        xing_records = [r for r in caplog.records if r.name == xing_email_module.__name__]
        assert xing_records
        assert all(r.exc_info is None for r in xing_records)

    def test_disconnect_logout_failure_does_not_leak_via_exc_info(self, caplog):
        """Same regression as above, for the logout phase -- a separate
        try/except block in `_disconnect`, so must be proven
        independently."""
        sensitive_exc_text = "SECRET_LOGOUT_FAILURE_TEXT_MUST_NOT_LEAK"

        class _FailingLogoutClient:
            def close(self):
                return ("OK", [b"closed"])

            def logout(self):
                raise OSError(sensitive_exc_text)

        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        with caplog.at_level("DEBUG"):
            collector._disconnect(_FailingLogoutClient())

        assert sensitive_exc_text not in caplog.text
        assert "xing_email_imap_logout_failed" in caplog.text
        assert "error_type=OSError" in caplog.text
        xing_records = [r for r in caplog.records if r.name == xing_email_module.__name__]
        assert xing_records
        assert all(r.exc_info is None for r in xing_records)


# ---------------------------------------------------------------------------
# AUD-005 (total wall-clock session deadline, not just per-read inactivity).
# The mechanism itself (ImapSessionDeadline: a slow-drip peer bounded by
# TOTAL elapsed time, and a fully silent peer bounded) is unit-tested
# directly in tests/test_imap_deadline.py. These tests cover how
# XingEmailCollector wires that mechanism in.
# ---------------------------------------------------------------------------


class _AlreadyExceededDeadline:
    """Stands in for an ImapSessionDeadline that has already fired by the
    time the per-message fetch loop runs -- lets the "deadline exceeded
    mid session" control-flow path be tested deterministically, without
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
    "one message batch completed, then the deadline fires before the
    next" be tested deterministically. `.exceeded` is read once per loop
    iteration (top-of-loop check) plus once more in the post-loop check,
    so returning False only for the very first read and True for every
    read after that reproduces exactly "message 1 was already in
    flight/done when time ran out for message 2".
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
    """Codex gate follow-up (Astra R4A HIGH): generalizes
    `_ExceedsAfterFirstCheckDeadline` to an arbitrary threshold -- lets a
    test place the deadline's transition to "exceeded" at an exact call
    number, e.g. to prove "message 1's own top-of-loop check reads
    False, message 2's ALSO reads False (so a fetch is genuinely
    attempted), and only the except-handler's read -- checked after a
    simulated mid-FETCH OSError -- reads True". Returns a class (not an
    instance) so it can directly replace `ImapSessionDeadline` via
    monkeypatch, exactly like `_AlreadyExceededDeadline`/
    `_ExceedsAfterFirstCheckDeadline` above.
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

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", _stub_deadline_client)
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )
        deadline = ImapSessionDeadline(5.0)

        collector._connect(deadline)

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

        monkeypatch.setattr(xing_email_module.imaplib, "IMAP4_SSL", lambda *a, **kw: _StubClient())
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        client = collector._connect()

        assert isinstance(client, _StubClient)

    @pytest.mark.asyncio
    async def test_normal_fetch_succeeds_with_the_session_deadline_active(self, monkeypatch):
        """Wiring the deadline in for an owned connection must not disturb
        a normal, fast, successful fetch."""
        body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
        raw = _build_email(XING_SENDER, "3 neue Stellenangebote für Python", body)
        fake_client = FakeImapClient([raw])

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        jobs = await collector.fetch()

        assert len(jobs) == 1
        assert fake_client.closed is True
        assert fake_client.logged_out is True

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_result_when_deadline_already_exceeded(self, monkeypatch):
        """NEW-001 (Astra R4A): if the total session deadline has already
        fired by the time the per-message fetch loop runs, the sync must
        stop -- but must NOT raise. It returns no batches (nothing could
        complete) and sets `collector.deadline_exceeded = True`, never a
        discarded/opaque exception. A prior version of this collector
        raised `XingConnectionError` here; that silently discarded any
        work that DID complete before the deadline in the general case
        (see the partial-batch test below) -- even in this
        zero-messages-completed edge case, an explicit flagged empty
        result is more honest than an exception, since the caller
        (app.services.collector_runner.run_xing) can now distinguish
        "genuinely nothing got done, time ran out" from a real
        connection/auth failure.
        """
        body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
        raw = _build_email(XING_SENDER, "3 neue Stellenangebote für Python", body)
        fake_client = FakeImapClient([raw])

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        monkeypatch.setattr(xing_email_module, "ImapSessionDeadline", _AlreadyExceededDeadline)
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        jobs = await collector.fetch()

        assert jobs == []
        assert collector.deadline_exceeded is True

        # Connection cleanup must still run even though the fetch loop
        # never got to run.
        assert fake_client.closed is True
        assert fake_client.logged_out is True

    @pytest.mark.asyncio
    async def test_fetch_persists_completed_batches_when_deadline_fires_mid_loop(self, monkeypatch):
        """NEW-001 (Astra R4A) core regression: two digest messages are
        due; the deadline fires only AFTER the first one has already
        completed fetch+parse. That completed batch's jobs must still
        come back -- never discarded merely because the SECOND message's
        turn never arrived before time ran out.
        """
        body1 = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
        body2 = _digest_body(BLOCK_WITH_ALL_OPTIONAL_FIELDS)
        raw1 = _build_email(
            XING_SENDER,
            "3 neue Stellenangebote für Python",
            body1,
            message_id="<first@mail.xing.com>",
        )
        raw2 = _build_email(
            XING_SENDER,
            "5 neue Stellenangebote für Python",
            body2,
            message_id="<second@mail.xing.com>",
        )
        fake_client = FakeImapClient([raw1, raw2])

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        monkeypatch.setattr(
            xing_email_module, "ImapSessionDeadline", _ExceedsAfterFirstCheckDeadline
        )
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        batches = await collector.fetch_message_batches()

        assert len(batches) == 1
        assert batches[0].message_id == "<first@mail.xing.com>"
        assert len(batches[0].jobs) == 1
        assert collector.deadline_exceeded is True
        # Message 2 was never even attempted -- the loop broke before it.
        # Message 1 sees TWO fetch calls: the cheap Message-ID pre-check
        # (Codex gate follow-up) followed by the real full-body fetch.
        assert fake_client.fetch_calls == [b"1", b"1"]

        # Connection cleanup must still run.
        assert fake_client.closed is True
        assert fake_client.logged_out is True

    @pytest.mark.asyncio
    async def test_next_cycle_retries_only_the_deadline_skipped_message(self, monkeypatch):
        """NEW-001 (Astra R4A): after a deadline-partial fetch, the NEXT
        cycle must not re-fetch (and so must not risk duplicating) the
        message that already completed and was acknowledged -- only the
        message the deadline left behind is attempted again.
        `is_message_processed` is exactly the existing acknowledgment
        mechanism (app.db.repositories.mark_message_processed, driven by
        app.services.collector_runner.run_xing after every job in a
        batch persists); this proves the NEW-001 fix composes with it
        correctly rather than needing a new idempotency mechanism.
        """
        body1 = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
        body2 = _digest_body(BLOCK_WITH_ALL_OPTIONAL_FIELDS)
        raw1 = _build_email(
            XING_SENDER,
            "3 neue Stellenangebote für Python",
            body1,
            message_id="<first@mail.xing.com>",
        )
        raw2 = _build_email(
            XING_SENDER,
            "5 neue Stellenangebote für Python",
            body2,
            message_id="<second@mail.xing.com>",
        )
        fake_client = FakeImapClient([raw1, raw2])

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        # No deadline pressure this time -- simulates the retry cycle,
        # with message 1 now reported as already-acknowledged (persisted
        # by the prior, deadline-truncated run).
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
            is_message_processed=lambda message_id: message_id == "<first@mail.xing.com>",
        )

        batches = await collector.fetch_message_batches()

        assert len(batches) == 1
        assert batches[0].message_id == "<second@mail.xing.com>"
        assert collector.deadline_exceeded is False

    @pytest.mark.asyncio
    async def test_fetch_preserves_batches_when_deadline_fires_mid_fetch(self, monkeypatch):
        """Codex gate follow-up (Astra R4A HIGH): unlike the
        between-iterations case above, here the deadline fires WHILE a
        FETCH is already blocked in flight -- the watchdog force-closes
        the socket mid-call, so `client.fetch(...)` itself raises
        OSError rather than the top-of-loop `.exceeded` check catching
        it cleanly beforehand. Message 1 must still complete normally,
        and message 1's batch must still be returned -- never discarded
        merely because message 2's in-flight FETCH is what actually
        observed the deadline.
        """
        body1 = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
        raw1 = _build_email(
            XING_SENDER,
            "3 neue Stellenangebote für Python",
            body1,
            message_id="<first@mail.xing.com>",
        )
        raw2 = _build_email(
            XING_SENDER,
            "5 neue Stellenangebote für Python",
            _digest_body(BLOCK_WITH_ALL_OPTIONAL_FIELDS),
            message_id="<second@mail.xing.com>",
        )
        fake_client = FakeImapClient([raw1, raw2], raise_oserror_on_message_set={b"2"})

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        # Both messages' own top-of-loop checks read False (a fetch is
        # genuinely attempted for each) -- only the except-handler's
        # read, right after message 2's simulated mid-FETCH OSError,
        # reads True.
        monkeypatch.setattr(
            xing_email_module,
            "ImapSessionDeadline",
            _make_exceeds_after_n_checks_deadline_class(2),
        )
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        batches = await collector.fetch_message_batches()

        assert len(batches) == 1
        assert batches[0].message_id == "<first@mail.xing.com>"
        assert collector.deadline_exceeded is True

    @pytest.mark.asyncio
    async def test_genuine_transport_failure_without_deadline_still_raises(self, monkeypatch):
        """Codex gate follow-up (Astra R4A HIGH): a real, unexpected
        connection failure mid-FETCH -- NOT caused by the session
        deadline -- must still raise `XingConnectionError` normally,
        exactly like before either NEW-001 or this follow-up fix. The
        deadline-preservation behavior above must never mask a genuine
        transport/protocol failure as a harmless partial result.
        """
        raw1 = _build_email(
            XING_SENDER,
            "3 neue Stellenangebote für Python",
            _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS),
            message_id="<first@mail.xing.com>",
        )
        fake_client = FakeImapClient([raw1], raise_oserror_on_message_set={b"1"})

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        # A real ImapSessionDeadline (never fires within this fast test)
        # -- `.exceeded` stays False throughout, so the OSError below is
        # unambiguously a genuine failure, not a deadline artifact.
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        with pytest.raises(XingConnectionError):
            await collector.fetch_message_batches()

        assert fake_client.closed is True
        assert fake_client.logged_out is True

    @pytest.mark.asyncio
    async def test_fetch_preserves_batches_when_deadline_fires_mid_fetch_via_imap4_abort(
        self, monkeypatch
    ):
        """FINAL-001 (Astra R5A): the exact same scenario as
        `test_fetch_preserves_batches_when_deadline_fires_mid_fetch`
        above, but the deadline-forced socket close surfaces as
        `imaplib.IMAP4.abort` instead of `OSError` -- confirmed as the
        REAL symptom by reading imaplib's own readline()/`_get_line`
        source (readline() catches the interrupted recv()'s
        ConnectionError itself and turns it into a clean-EOF abort, not
        a propagated OSError). Before this fix, `except OSError:` alone
        in `_fetch_sync_body`'s per-UID loop never caught this, so it
        escaped uncaught past this whole preserve-completed-batches path
        -- message 1's already-completed batch must still be preserved
        here exactly like the OSError case.
        """
        body1 = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
        raw1 = _build_email(
            XING_SENDER,
            "3 neue Stellenangebote für Python",
            body1,
            message_id="<first@mail.xing.com>",
        )
        raw2 = _build_email(
            XING_SENDER,
            "5 neue Stellenangebote für Python",
            _digest_body(BLOCK_WITH_ALL_OPTIONAL_FIELDS),
            message_id="<second@mail.xing.com>",
        )
        fake_client = FakeImapClient([raw1, raw2], raise_abort_on_message_set={b"2"})

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        monkeypatch.setattr(
            xing_email_module,
            "ImapSessionDeadline",
            _make_exceeds_after_n_checks_deadline_class(2),
        )
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        batches = await collector.fetch_message_batches()

        assert len(batches) == 1
        assert batches[0].message_id == "<first@mail.xing.com>"
        assert collector.deadline_exceeded is True
        # Message 2's abort must have been observed on its very FIRST
        # fetch attempt (the cheap Message-ID header pre-check via
        # _read_message_id_header) -- proving that method's own
        # `except (OSError, imaplib.IMAP4.abort): raise` propagates the
        # abort immediately, never silently swallowing it as "header
        # absent, fall back to the full RFC822 fetch" (which would show
        # up here as TWO fetch_calls entries for b"2" instead of one).
        assert fake_client.fetch_calls.count(b"2") == 1

    @pytest.mark.asyncio
    async def test_genuine_imap4_abort_without_deadline_raises_sanitized_connection_error(
        self, monkeypatch, caplog
    ):
        """FINAL-001 (Astra R5A): a real, unexpected `imaplib.IMAP4.abort`
        mid-FETCH -- NOT caused by the session deadline -- must still
        raise a sanitized `XingConnectionError` (never the raw abort
        object/message escaping uncaught into
        app.api.routes.run_xing_collector's generic-exception path,
        which would otherwise reach FastAPI/Starlette's own unhandled
        -exception traceback logging -- see
        app.collectors.xing_email._fetch_sync's own except clause for
        the full rationale) and must never log the abort's own raw,
        potentially server-controlled message text -- only
        `type(exc).__name__`.
        """
        sensitive_exc_text = "SECRET_RAW_ABORT_TEXT_MUST_NOT_LEAK"
        raw1 = _build_email(
            XING_SENDER,
            "3 neue Stellenangebote für Python",
            _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS),
            message_id="<first@mail.xing.com>",
        )

        class _AbortingClient(FakeImapClient):
            def fetch(self, message_set, message_parts: str) -> tuple[str, list]:
                self.fetch_calls.append(message_set)
                if message_set == b"1":
                    raise imaplib.IMAP4.abort(sensitive_exc_text)
                return super().fetch(message_set, message_parts)

        fake_client = _AbortingClient([raw1])

        monkeypatch.setattr(xing_email_module, "DeadlineIMAP4SSL", lambda *a, **kw: fake_client)
        # A real ImapSessionDeadline (never fires within this fast test)
        # -- `.exceeded` stays False throughout, so the abort below is
        # unambiguously a genuine failure, not a deadline artifact.
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
        )

        with pytest.raises(XingConnectionError) as exc_info:
            await collector.fetch_message_batches()

        assert sensitive_exc_text not in str(exc_info.value)
        assert sensitive_exc_text not in caplog.text
        assert fake_client.closed is True
        assert fake_client.logged_out is True

    @pytest.mark.asyncio
    async def test_already_processed_message_skips_full_body_fetch(self, monkeypatch):
        """Codex gate follow-up (Astra R4A HIGH, starvation): an
        already-acknowledged message must be recognized via the cheap
        Message-ID pre-check and skipped WITHOUT ever transferring its
        full RFC822 body -- otherwise a backlog dominated by
        already-processed messages would spend its entire per-run fetch
        budget re-transferring their full bodies before ever reaching a
        genuinely new one, starving it. Message 1 (already processed)
        must see exactly ONE fetch call (the header pre-check); message
        2 (new) must see two (pre-check + full body).
        """
        raw1 = _build_email(
            XING_SENDER,
            "3 neue Stellenangebote für Python",
            _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS),
            message_id="<first@mail.xing.com>",
        )
        raw2 = _build_email(
            XING_SENDER,
            "5 neue Stellenangebote für Python",
            _digest_body(BLOCK_WITH_ALL_OPTIONAL_FIELDS),
            message_id="<second@mail.xing.com>",
        )
        fake_client = FakeImapClient([raw1, raw2])
        collector = XingEmailCollector(
            imap_host="imap.example.com",
            imap_port=993,
            username="user@example.com",
            app_password="app-password",
            imap_client=fake_client,
            is_message_processed=lambda message_id: message_id == "<first@mail.xing.com>",
        )

        batches = await collector.fetch_message_batches()

        assert len(batches) == 1
        assert batches[0].message_id == "<second@mail.xing.com>"
        message_1_fetch_calls = [call for call in fake_client.fetch_calls if call == b"1"]
        message_2_fetch_calls = [call for call in fake_client.fetch_calls if call == b"2"]
        assert len(message_1_fetch_calls) == 1, (
            "an already-processed message must never reach the full-body fetch"
        )
        assert len(message_2_fetch_calls) == 2
