"""Outbound mail provider — shared types (Stage 7D).

Deliberately a SEPARATE, minimal Protocol from
app/providers/email/base.py's read-only `ImapClient` — see that module's
"HARD SECURITY CONSTRAINT" docstring and app/providers/email/imap.py's
own docstring. The inbound (Stage 7A) provider contract is never
weakened, extended, or reused to carry outbound capability; this module
and app/providers/email/smtp.py are new, independent code with their own
narrow surface.

**Trust boundary (spec-mandated, enforced by construction).** Nothing in
this package ever reads a recipient, subject, or body out of raw,
unauthenticated email content. Every `OutboundMessage` field is expected
to already have been resolved from TRUSTED, already-persisted data by
`app.services.response_draft_send` BEFORE this Protocol is ever called:

- `to_address` — the ORIGINAL inbound `GmailMessageRecord.from_address`
  this draft is replying to (structural correspondence metadata Stage 7A
  already parsed and persisted, not text re-interpreted from the body).
- `subject`/`body` — the exact, human-APPROVED `pinned_subject`/
  `pinned_body` from a `ResponseDraftApprovalRecord` (see that model's
  docstring for why these are pinned copies, not a live re-read).
- `in_reply_to`/`references` — the original message's own threading
  headers, copied verbatim.

Nothing under app/providers/email/ (inbound OR outbound) ever follows a
URL/link found in email content, and this package makes no HTTP request
of any kind — only SMTP, to the one configured account.
"""

from dataclasses import dataclass, field
from typing import Protocol


class EmailSendError(Exception):
    """Base exception for the outbound mail provider (auth, connection,
    or protocol failures). Callers (app.services.response_draft_send)
    catch this single type — mirrors
    app.providers.email.base.GmailProviderError's role for the inbound
    provider.

    Every message raised anywhere in this package is a fixed, static
    string — never an f-string embedding the *upstream* exception/server
    response text, for the same GMAIL-003-style reason
    GmailProviderError's docstring gives (a server-echoed address/
    hostname must never reach an API response or a log line). Callers
    must log only `type(exc).__name__`, never `str(exc)`.
    """


class EmailSendAuthError(EmailSendError):
    """SMTP login was rejected (bad username/App Password), or
    credentials are not configured at all. Not retried by this package —
    retrying with the same credentials cannot succeed; a caller-level
    retry (see app.services.response_draft_send's FAILED -> PENDING CAS)
    still applies at the send-attempt level, since a human may fix the
    configured credentials between attempts.
    """


class EmailSendConnectionError(EmailSendError):
    """The SMTP server could not be reached, or an SMTP command other
    than login failed.
    """


@dataclass(frozen=True)
class OutboundMessage:
    """The complete, minimal input to `OutboundEmailProvider.send` — see
    module docstring for the trust chain every field must already have
    passed through before this dataclass is constructed.
    """

    to_address: str
    subject: str
    body: str
    in_reply_to: str | None
    references: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OutboundSendResult:
    """What a successful send reports back — traceability only.
    `provider_message_id` is the RFC 5322 Message-ID the provider
    assigned to the sent message, if known; `None` if the provider
    cannot report one. Never used as a security/identity boundary
    anywhere in this project (mirrors GmailMessageRecord.message_id_header's
    own "never the dedup identity" convention).
    """

    provider_message_id: str | None


class OutboundEmailProvider(Protocol):
    """The entire outbound surface this project exposes. One method,
    deliberately: no draft-creation, no label/mark/trash operation, no
    read access — those all remain exclusively in the Stage 7A `ImapClient`
    Protocol (still fully read-only) or are simply not implemented at
    all. A concrete implementation (app.providers.email.smtp.
    GmailSmtpProvider) may raise `EmailSendError`/subclasses; it must
    never partially send (either the provider confirms success, or the
    caller treats the attempt as failed).
    """

    def send(self, message: OutboundMessage) -> OutboundSendResult: ...
