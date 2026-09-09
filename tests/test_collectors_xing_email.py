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

    def __init__(self, messages: list[bytes]) -> None:
        self._messages = messages
        self.select_calls: list[tuple[str, bool]] = []
        self.search_calls: list[tuple[str, ...]] = []
        self.closed = False
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return ("OK", [str(len(self._messages)).encode()])

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
        self.search_calls.append(criteria)
        numbers = b" ".join(str(i + 1).encode() for i in range(len(self._messages)))
        return ("OK", [numbers])

    def fetch(self, message_set, message_parts: str) -> tuple[str, list]:
        index = int(message_set) - 1
        raw = self._messages[index]
        return ("OK", [(b"1 (RFC822 {%d}" % len(raw), raw)])

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
    async def test_fetch_raises_xing_connection_error_once_the_session_deadline_is_exceeded(
        self, monkeypatch
    ):
        """If the total session deadline has already fired by the time the
        per-message fetch loop runs, the sync must stop and raise rather
        than keep attempting fetches on a connection whose socket has
        already been force-closed."""
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

        with pytest.raises(XingConnectionError):
            await collector.fetch()

        # Connection cleanup must still run even though the sync aborted.
        assert fake_client.closed is True
        assert fake_client.logged_out is True
