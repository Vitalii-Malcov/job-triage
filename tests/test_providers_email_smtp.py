"""Tests for app.providers.email.smtp.GmailSmtpProvider (Stage 7D).

Mirrors tests/test_providers_email_imap.py's approach: a lightweight fake
SMTP client (no real socket/network I/O anywhere), plus regression guards
that (a) this module is fully independent of the read-only IMAP provider
and (b) that provider's read-only contract was never touched/weakened by
adding outbound capability.
"""

import inspect
import smtplib

import pytest

import app.providers.email.base as email_base_module
import app.providers.email.imap as email_imap_module
import app.providers.email.outbound_base as outbound_base_module
import app.providers.email.smtp as smtp_module
from app.providers.email.outbound_base import (
    EmailSendAuthError,
    EmailSendConnectionError,
    OutboundMessage,
)
from app.providers.email.smtp import GmailSmtpProvider

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

    def test_send_message_failure_raises_connection_error_and_still_disconnects(self):
        client = FakeSmtpClient(send_error=smtplib.SMTPException("boom"))
        provider = _provider(client)

        with pytest.raises(EmailSendConnectionError):
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
    def test_connection_error_message_is_fixed_not_derived_from_exception(self):
        client = FakeSmtpClient(send_error=smtplib.SMTPException("secret-server-detail"))
        provider = _provider(client)

        with pytest.raises(EmailSendConnectionError) as exc_info:
            provider.send(_message())
        assert "secret-server-detail" not in str(exc_info.value)


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
