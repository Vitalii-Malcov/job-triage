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

**Honest delivery-outcome contract (no false exactly-once claim).** SMTP
over an unreliable network has a well-known ambiguous-outcome window: an
exception raised AFTER the provider has started transmitting the message
to the server does NOT prove the server never accepted it — a dropped
TCP connection, a timeout waiting for the final server reply, or similar
can occur after the message was already fully accepted. This package
does **not** claim exactly-once delivery and does not claim a raised
exception proves non-delivery. What it DOES guarantee: (1) this project's
own DB-level concurrency control (`ResponseDraftSendRecord`'s
`UNIQUE(response_draft_id)` claim + CAS state machine — see that model's
docstring) prevents two concurrent/retried REQUESTS from both attempting
to send the same approval; (2) `EmailSendOutcomeUnknownError` (see below)
is raised, distinctly from a definite pre-transmission failure, whenever
transmission was attempted but acceptance cannot be proven either way —
`app.services.response_draft_send` treats that as a terminal, fail-closed
`UNCERTAIN` state that is never automatically retried (see that module's
docstring for the full contract). Manual reconciliation of a genuinely
`UNCERTAIN` send is out of scope for Stage 7D.
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
    """A DEFINITE pre-transmission failure: the SMTP server could not be
    reached, login failed, or the outbound message itself could not even
    be constructed (e.g. a CRLF/header-injection attempt in `subject`
    that `email.message` rejects). Raised only for failures that occur
    strictly BEFORE `send_message()`/transmission is invoked — see
    `EmailSendOutcomeUnknownError`'s docstring for the boundary. Safe to
    treat as "definitely not sent"; `app.services.response_draft_send`
    retries these via its `FAILED -> PENDING` CAS.
    """


class EmailSendOutcomeUnknownError(EmailSendError):
    """Transmission to the SMTP server was ATTEMPTED (`send_message()`
    was invoked) but an exception occurred before this package could
    confirm the server accepted the message — delivery can be neither
    confirmed NOR ruled out. This is deliberately a SEPARATE exception
    from `EmailSendConnectionError`: the two must never be handled the
    same way.

    **Safest-acceptable rule (see module docstring's "honest
    delivery-outcome contract").** Once `send_message()` has been called,
    EVERY `smtplib.SMTPException`/`OSError` (including a dropped
    connection mid-transmission) is treated as outcome-unknown — never as
    a definite failure — because this package has no positive proof of
    non-delivery for any of them. `app.providers.email.smtp.GmailSmtpProvider`
    builds and validates the outbound message BEFORE calling
    `send_message()` specifically so a message-construction failure (a
    DEFINITE pre-send failure) can never be misclassified as this.

    `app.services.response_draft_send` treats this as fail-closed and
    terminal: the send transitions to `UNCERTAIN` (never `FAILED`), is
    never automatically retried, and a later send attempt for the same
    draft is refused before this provider is ever called again — see
    that module's docstring.
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
    GmailSmtpProvider) either returns `OutboundSendResult` (confirmed
    success), or raises: `EmailSendAuthError`/`EmailSendConnectionError`
    for a DEFINITE pre-transmission failure, or
    `EmailSendOutcomeUnknownError` when transmission was attempted but
    acceptance cannot be proven either way — see that exception's and the
    module's own docstring. This project does NOT claim exactly-once
    delivery or that a raised exception always proves non-delivery.
    """

    def send(self, message: OutboundMessage) -> OutboundSendResult: ...
