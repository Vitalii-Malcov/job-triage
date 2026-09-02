"""Gmail outbound SMTP provider (Stage 7D).

Sends exactly one message per `send()` call, over SMTPS (implicit TLS),
using the SAME Gmail account credentials as the read-only IMAP provider
(`GMAIL_USERNAME`/`GMAIL_APP_PASSWORD`) — a Gmail App Password is valid
for both IMAP and SMTP against the same account, so no new secret is
introduced. This module is completely independent of
app/providers/email/imap.py; see app/providers/email/outbound_base.py's
module docstring for why the two are never merged.

**No mailbox read access whatsoever.** This provider only ever opens an
SMTP connection — never IMAP — so it structurally cannot read, label,
mark, or delete anything in the mailbox; the "read-only IMAP contract"
that provider enforces is simply not reachable from here.

**No content is ever read from raw email here.** The only inputs this
module ever sees are `OutboundMessage`'s already-resolved, already-
trusted fields (see outbound_base.py's module docstring) — this module
does not parse MIME, does not read `body_plain`, and never touches
`app.db.models.GmailMessageRecord` directly.

**Ambiguous-outcome handling (see outbound_base.py's "honest
delivery-outcome contract").** `send()` builds and validates the
outbound `EmailMessage` BEFORE opening a connection or calling
`send_message()` — a malformed message (e.g. a CRLF/header-injection
attempt) is therefore always a DEFINITE pre-transmission failure
(`EmailSendConnectionError`), never confused with a genuinely ambiguous
outcome. Once `send_message()` is actually invoked, this provider makes
NO claim that a raised exception proves the message was not delivered —
`smtplib.SMTPException`/`OSError` raised during or after that call
(including a dropped connection mid-transmission) is reported as
`EmailSendOutcomeUnknownError`, distinctly from a pre-transmission
failure. This is a deliberately conservative ("safest-acceptable")
classification: this project does not attempt to distinguish
"definitely rejected before send" from "possibly accepted, response
never received" among post-`send_message()` exceptions.
"""

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from app.collectors.base import is_configured
from app.providers.email.outbound_base import (
    EmailSendAuthError,
    EmailSendConnectionError,
    EmailSendOutcomeUnknownError,
    OutboundMessage,
    OutboundSendResult,
)

logger = logging.getLogger(__name__)


class SmtpClient(Protocol):
    """The subset of smtplib.SMTP_SSL's interface this provider uses —
    mirrors app.providers.email.base.ImapClient's "narrow Protocol so
    tests can inject a fake instead of a real connection" rationale.
    """

    def login(self, user: str, password: str) -> tuple[int, bytes]: ...

    def send_message(self, msg: EmailMessage) -> dict: ...

    def quit(self) -> tuple[int, bytes]: ...


class GmailSmtpProvider:
    """Structurally satisfies `app.providers.email.outbound_base.
    OutboundEmailProvider` (a `Protocol` — no explicit inheritance
    needed, mirrors `GmailImapProvider`'s own relationship to `ImapClient`).
    Sends one outbound reply via Gmail SMTP. Not async (mirrors
    XingEmailCollector's synchronous IMAP calls) — the caller
    (app.services.response_draft_send) is responsible for offloading this
    to a worker thread if called from an async context, exactly as
    app.providers.email.imap.GmailImapProvider._fetch_sync's own
    docstring documents for its blocking imaplib calls.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        app_password: str,
        from_address: str | None = None,
        smtp_client: SmtpClient | None = None,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.app_password = app_password
        # The account's own address is used as the From header — falls
        # back to `username` (a Gmail username IS the account's email
        # address) when not given a separate value.
        self.from_address = from_address or username
        # Injected only by tests, to avoid a real SMTP connection —
        # mirrors GmailImapProvider._injected_client.
        self._injected_client = smtp_client

    def send(self, message: OutboundMessage) -> OutboundSendResult:
        if not is_configured(self.username) or not is_configured(self.app_password):
            raise EmailSendAuthError("GMAIL_USERNAME / GMAIL_APP_PASSWORD is not configured")

        # Build/validate the message BEFORE any connection/transmission
        # attempt — a malformed header (e.g. a CRLF-header-injection
        # attempt smuggled into subject, which Python's email.message
        # rejects with ValueError) must remain a DEFINITE pre-send
        # failure, never misclassified as an ambiguous outcome. No
        # connection has been opened yet at this point, so "not sent" is
        # provably true here.
        try:
            msg = self._build_message(message)
        except ValueError as exc:
            logger.warning("outbound_email_build_failed error_type=%s", type(exc).__name__)
            raise EmailSendConnectionError("Building the outbound email failed") from exc

        client = self._injected_client
        owns_connection = client is None
        if client is None:
            client = self._connect()

        try:
            client.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            # Transmission was ATTEMPTED — the server may or may not have
            # accepted the message before this exception occurred (a
            # dropped connection, a timeout waiting for the final reply,
            # etc.). This package has no positive proof of non-delivery
            # for ANY exception raised past this point (safest-acceptable
            # rule — see EmailSendOutcomeUnknownError's docstring), so
            # this is NEVER reported as a definite failure.
            logger.warning("outbound_email_send_outcome_unknown error_type=%s", type(exc).__name__)
            raise EmailSendOutcomeUnknownError(
                "Sending the outbound email had an uncertain outcome"
            ) from exc
        finally:
            if owns_connection:
                self._disconnect(client)

        return OutboundSendResult(provider_message_id=msg.get("Message-Id"))

    def _build_message(self, message: OutboundMessage) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.from_address
        msg["To"] = message.to_address
        msg["Subject"] = message.subject
        if message.in_reply_to:
            msg["In-Reply-To"] = message.in_reply_to
        if message.references:
            msg["References"] = " ".join(message.references)
        msg.set_content(message.body)
        return msg

    def _connect(self) -> smtplib.SMTP_SSL:
        try:
            client = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port)
        except OSError as exc:
            # Never interpolate the underlying OSError/host/port into the
            # raised message — same GMAIL-003-style rationale as
            # GmailImapProvider._connect.
            logger.warning("outbound_smtp_connect_failed error_type=%s", type(exc).__name__)
            raise EmailSendConnectionError(
                "Could not connect to the configured outbound SMTP host"
            ) from exc

        try:
            client.login(self.username, self.app_password)
        except smtplib.SMTPException as exc:
            logger.warning("outbound_smtp_login_failed error_type=%s", type(exc).__name__)
            raise EmailSendAuthError("Outbound SMTP login was rejected") from exc
        return client

    def _disconnect(self, client: SmtpClient) -> None:
        try:
            client.quit()
        except Exception as exc:
            logger.warning("outbound_smtp_quit_failed error_type=%s", type(exc).__name__)
