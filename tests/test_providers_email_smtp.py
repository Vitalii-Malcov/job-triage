"""Tests for app.providers.email.smtp.GmailSmtpProvider (Stage 7D).

Mirrors tests/test_providers_email_imap.py's approach: a lightweight fake
SMTP client (no real socket/network I/O anywhere), plus regression guards
that (a) this module is fully independent of the read-only IMAP provider
and (b) that provider's read-only contract was never touched/weakened by
adding outbound capability.
"""

import inspect
import smtplib
import socket
import ssl
import threading
import time

import pytest

import app.providers.email.base as email_base_module
import app.providers.email.imap as email_imap_module
import app.providers.email.outbound_base as outbound_base_module
import app.providers.email.smtp as smtp_module
from app.db.gmail_repository import THREAD_LOCK_TTL_SECONDS
from app.providers.email.outbound_base import (
    EmailSendAuthError,
    EmailSendConnectionError,
    EmailSendOutcomeUnknownError,
    OutboundMessage,
)
from app.providers.email.smtp import SMTP_OPERATION_TIMEOUT_SECONDS, GmailSmtpProvider

ACCOUNT = "me@example.com"


class FakeSmtpClient:
    def __init__(self, *, send_error: Exception | None = None) -> None:
        self._send_error = send_error
        self.login_calls: list[tuple[str, str]] = []
        self.sent_messages: list = []
        self.quit_called = False

    def login(self, user: str, password: str):
        self.login_calls.append((user, password))
        return (235, b"Authentication successful")

    def send_message(self, msg):
        if self._send_error is not None:
            raise self._send_error
        self.sent_messages.append(msg)
        return {}

    def quit(self):
        self.quit_called = True
        return (221, b"Bye")


def _provider(client: FakeSmtpClient | None = None, **overrides) -> GmailSmtpProvider:
    kwargs = dict(
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        username=ACCOUNT,
        app_password="app-password",
        smtp_client=client,
    )
    kwargs.update(overrides)
    return GmailSmtpProvider(**kwargs)


def _message(**overrides) -> OutboundMessage:
    kwargs = dict(
        to_address="recruiter@acme.example.com",
        subject="Re: Backend Engineer",
        body="Thank you for your message.",
        in_reply_to="<orig@acme.example.com>",
        references=("<root@acme.example.com>", "<orig@acme.example.com>"),
    )
    kwargs.update(overrides)
    return OutboundMessage(**kwargs)


class TestSend:
    def test_successful_send_builds_expected_headers(self):
        client = FakeSmtpClient()
        provider = _provider(client)

        result = provider.send(_message())

        assert len(client.sent_messages) == 1
        sent = client.sent_messages[0]
        assert sent["To"] == "recruiter@acme.example.com"
        assert sent["Subject"] == "Re: Backend Engineer"
        assert sent["In-Reply-To"] == "<orig@acme.example.com>"
        assert sent["References"] == "<root@acme.example.com> <orig@acme.example.com>"
        assert sent["From"] == ACCOUNT
        assert sent.get_content().strip() == "Thank you for your message."
        assert result.provider_message_id is None  # no Message-Id set by this provider

    def test_injected_client_skips_login_assumed_pre_authenticated(self):
        """Mirrors GmailImapProvider._fetch_sync's own convention: login
        happens only inside `_connect()`, which is skipped entirely for
        an injected client (the test double / caller-managed connection
        is assumed already authenticated).
        """
        client = FakeSmtpClient()
        provider = _provider(client)

        provider.send(_message())

        assert client.login_calls == []
        assert len(client.sent_messages) == 1

    def test_injected_client_connection_is_never_closed_by_provider(self):
        """owns_connection=False for an injected client — mirrors
        GmailImapProvider's own "caller manages injected connections"
        convention.
        """
        client = FakeSmtpClient()
        provider = _provider(client)

        provider.send(_message())

        assert client.quit_called is False

    def test_missing_in_reply_to_omits_header(self):
        client = FakeSmtpClient()
        provider = _provider(client)

        provider.send(_message(in_reply_to=None, references=()))

        sent = client.sent_messages[0]
        assert "In-Reply-To" not in sent
        assert "References" not in sent

    def test_not_configured_raises_auth_error_without_attempting_connection(self):
        provider = _provider(client=None, username="", app_password="")

        with pytest.raises(EmailSendAuthError):
            provider.send(_message())

    def test_send_message_failure_after_transmission_starts_is_outcome_unknown(self):
        """Transmission was ATTEMPTED (send_message was actually called) —
        this must NEVER be classified as a definite failure (see
        EmailSendOutcomeUnknownError's docstring): the server may have
        already accepted the message before this exception occurred.
        """
        client = FakeSmtpClient(send_error=smtplib.SMTPException("boom"))
        provider = _provider(client)

        with pytest.raises(EmailSendOutcomeUnknownError):
            provider.send(_message())

    def test_os_error_during_transmission_is_also_outcome_unknown(self):
        """Covers a dropped connection / disconnect mid-transmission —
        OSError, not just smtplib.SMTPException, must be classified the
        same way once send_message() has been invoked.
        """
        client = FakeSmtpClient(send_error=OSError("Connection reset by peer"))
        provider = _provider(client)

        with pytest.raises(EmailSendOutcomeUnknownError):
            provider.send(_message())

    def test_crlf_header_injection_attempt_in_subject_raises_email_send_error(self):
        """Python's email.message rejects a header value containing
        '\\r'/'\\n' by raising ValueError, not an SMTPException — this
        must still surface as EmailSendError (never an unhandled
        ValueError, which would leave a caller's PENDING send claim
        stuck forever — see app.services.response_draft_send's retry
        contract).
        """
        client = FakeSmtpClient()
        provider = _provider(client)

        with pytest.raises(EmailSendConnectionError):
            provider.send(_message(subject="Legit\r\nBcc: attacker@evil.com"))
        assert client.sent_messages == []


class TestErrorMessagesNeverLeakUpstreamText:
    def test_uncertain_outcome_message_is_fixed_not_derived_from_exception(self):
        client = FakeSmtpClient(send_error=smtplib.SMTPException("secret-server-detail"))
        provider = _provider(client)

        with pytest.raises(EmailSendOutcomeUnknownError) as exc_info:
            provider.send(_message())
        assert "secret-server-detail" not in str(exc_info.value)

    def test_crlf_injection_pre_send_error_message_is_fixed(self):
        client = FakeSmtpClient()
        provider = _provider(client)

        with pytest.raises(EmailSendConnectionError) as exc_info:
            provider.send(_message(subject="Legit\r\nBcc: attacker@evil.com"))
        assert "attacker@evil.com" not in str(exc_info.value)


class TestNoOutboundHttpOrImapCoupling:
    def test_smtp_module_has_no_http_client_imports(self):
        source = inspect.getsource(smtp_module)
        for forbidden in ("requests", "httpx", "urllib.request", "urlopen("):
            assert forbidden not in source

    def test_smtp_module_never_imports_imaplib(self):
        """The outbound provider must be structurally independent of the
        read-only IMAP provider — see outbound_base.py's module
        docstring. AST-based (not a naive substring check): the module's
        own docstring legitimately mentions "imaplib" in prose comparing
        itself to GmailImapProvider's blocking-call convention.
        """
        import ast

        tree = ast.parse(inspect.getsource(smtp_module))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "imaplib" not in imported_modules

    def test_outbound_base_module_has_no_http_or_imap_imports(self):
        source = inspect.getsource(outbound_base_module)
        for forbidden in ("requests", "httpx", "urllib.request", "urlopen(", "imaplib"):
            assert forbidden not in source


class TestInboundProviderContractUnweakened:
    """Regression guard: adding Stage 7D outbound capability must never
    touch/weaken the Stage 7A read-only IMAP contract.
    """

    def test_imap_client_protocol_still_has_no_mutating_methods(self):
        protocol_methods = {
            name
            for name, _ in inspect.getmembers(email_base_module.ImapClient)
            if not name.startswith("_")
        }
        for mutating in ("store", "append", "expunge", "copy", "send", "sendmail"):
            assert mutating not in protocol_methods

    def test_imap_module_still_documents_read_only_guarantee(self):
        source = " ".join(inspect.getsource(email_imap_module).lower().split())
        assert "read-only" in source
        assert "never sends or drafts" in source

    def test_imap_module_never_imports_smtplib(self):
        source = inspect.getsource(email_imap_module)
        assert "smtplib" not in source


class TestHardConnectionTimeout:
    """S7E-014 (Codex re-review, final lock hardening): `send_follow_up`
    now holds a per-Gmail-thread lock (`app.db.gmail_repository`'s
    THREAD_LOCK_TTL_SECONDS) across this provider's ENTIRE `send()` call
    — a hung/black-holed SMTP peer must never be able to hold that lease
    hostage. These tests prove the hard timeout is real (a genuine socket,
    not just a mocked kwarg), correctly classified, and safely bounded
    below the lock's own lease.
    """

    def test_operation_timeout_is_safely_below_thread_lock_ttl(self):
        """The actual proof of the safety margin this module's own
        docstring claims — not just prose. A comfortable margin (at least
        half the lock's own TTL) is required, not merely `<`, since a
        pathological peer could hit the per-call cap on more than one
        round trip (see SMTP_OPERATION_TIMEOUT_SECONDS's docstring)."""
        assert SMTP_OPERATION_TIMEOUT_SECONDS < THREAD_LOCK_TTL_SECONDS
        margin = THREAD_LOCK_TTL_SECONDS - SMTP_OPERATION_TIMEOUT_SECONDS
        assert margin >= THREAD_LOCK_TTL_SECONDS / 2

    def test_connect_passes_the_hard_timeout_to_smtp_ssl(self, monkeypatch):
        captured = {}

        class _StubClient:
            def login(self, user, password):
                return (235, b"OK")

            def send_message(self, msg):
                return {}

            def quit(self):
                return (221, b"Bye")

        def _fake_smtp_ssl(host, port, timeout=None, context=None):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout
            captured["context"] = context
            return _StubClient()

        monkeypatch.setattr(smtp_module.smtplib, "SMTP_SSL", _fake_smtp_ssl)
        provider = _provider(client=None)

        provider.send(_message())

        assert captured["timeout"] == SMTP_OPERATION_TIMEOUT_SECONDS

    def test_connect_passes_a_verifying_ssl_context(self, monkeypatch):
        """AUD-001: smtplib.SMTP_SSL's own default (context=None) resolves
        to ssl._create_stdlib_context(), which disables certificate
        verification entirely (verify_mode=CERT_NONE,
        check_hostname=False) -- a real vulnerability for a connection
        that authenticates with a real mailbox password. This proves an
        explicit, verifying context is always supplied instead."""
        captured = {}

        class _StubClient:
            def login(self, user, password):
                return (235, b"OK")

            def send_message(self, msg):
                return {}

            def quit(self):
                return (221, b"Bye")

        def _fake_smtp_ssl(host, port, timeout=None, context=None):
            captured["context"] = context
            return _StubClient()

        monkeypatch.setattr(smtp_module.smtplib, "SMTP_SSL", _fake_smtp_ssl)
        provider = _provider(client=None)

        provider.send(_message())

        assert isinstance(captured["context"], ssl.SSLContext)
        assert captured["context"].verify_mode == ssl.CERT_REQUIRED
        assert captured["context"].check_hostname is True

    def test_hung_smtp_peer_raises_within_bounded_time_not_indefinitely(self):
        """A REAL socket, not a mock: a listener that accepts the
        connection and then sends nothing at all (simulating a
        black-holed/hung SMTP peer during the TLS handshake). Proves the
        actual mechanism — not merely that a `timeout=` kwarg is passed —
        raises well within bounds rather than hanging."""
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
            # Never write anything back — the client's TLS handshake
            # read blocks until its own socket timeout fires.
            time.sleep(2)
            conn.close()

        server_thread = threading.Thread(target=_accept_and_hang, daemon=True)
        server_thread.start()
        try:
            provider = GmailSmtpProvider(
                smtp_host=host,
                smtp_port=port,
                username=ACCOUNT,
                app_password="app-password",
                timeout_seconds=0.3,
            )

            start = time.monotonic()
            with pytest.raises(EmailSendConnectionError):
                provider.send(_message())
            elapsed = time.monotonic() - start

            assert accepted.wait(timeout=2), "test server never accepted the connection"
            assert elapsed < 2.0, f"the hard timeout did not bound the hang (took {elapsed:.2f}s)"
        finally:
            server.close()
            server_thread.join(timeout=3)

    def test_login_timeout_is_classified_as_connection_error_not_leaked(self, monkeypatch):
        """S7E-014: a timeout waiting for the login exchange raises
        `OSError`/`socket.timeout`, NOT `smtplib.SMTPException` — before
        this fix, `_connect()` only caught the latter around `login()`,
        so this would have leaked as a raw, unclassified exception
        instead of the honest `EmailSendConnectionError` a caller
        (app.services.follow_up_send / response_draft_send) knows how to
        handle."""

        class _HangingLoginClient:
            def login(self, user, password):
                raise TimeoutError("timed out waiting for login response")

            def quit(self):
                return (221, b"Bye")

        monkeypatch.setattr(smtp_module.smtplib, "SMTP_SSL", lambda *a, **kw: _HangingLoginClient())
        provider = _provider(client=None)

        with pytest.raises(EmailSendConnectionError):
            provider.send(_message())
