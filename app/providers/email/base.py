"""Gmail inbox provider — shared types (Stage 7A).

=====================================================================
HARD SECURITY CONSTRAINT — DO NOT VIOLATE. THIS IS NOT TECH DEBT.
=====================================================================
This package NEVER makes an outbound HTTP request to a URL or remote
resource extracted from an email — not to resolve a link, not to fetch a
tracking pixel, not to render HTML, not for any reason. The only network
I/O anywhere under app/providers/email/ is IMAP against the user's own
configured mailbox. See app/collectors/xing_email.py's module docstring
for the concrete incident class this class of rule prevents (a recruiter
being notified their listing was "viewed" purely because our own code
followed a per-recipient tracking link); the same rule applies here even
though Gmail's own reply/response mailbox is not a job-digest source.

Concretely: this package has no dependency on httpx, requests, aiohttp,
urllib, or any other HTTP client — tests assert this by inspecting
app/providers/email/imap.py's source, not just by testing behavior with a
mocked IMAP client.
=====================================================================

Read-only guarantee: every IMAP command this package issues is a read
command (LOGIN, SELECT ... readonly=True, STATUS, UID SEARCH, UID FETCH,
CLOSE, LOGOUT). Nothing here ever calls STORE, EXPUNGE, COPY, APPEND, or
any other command that could mutate the mailbox (mark read/unread,
delete, move, label, send, or create a draft).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

Direction = Literal["INBOUND", "OUTBOUND"]

# Bounds applied while parsing a message, so one oversized/malicious email
# can't blow up memory or the database. Values are generous for real
# correspondence but finite.
MAX_SUBJECT_LENGTH = 998  # RFC 5322 recommended max header line length.
MAX_HEADER_VALUE_LENGTH = 998
MAX_ADDRESS_LENGTH = 320  # RFC 5321 4.5.3.1.3 max reverse-path/forward-path.
MAX_DISPLAY_NAME_LENGTH = 200
MAX_ADDRESSES_PER_HEADER = 50
MAX_REFERENCES = 20
MAX_BODY_LENGTH = 20_000
MAX_ATTACHMENTS = 20


class GmailProviderError(Exception):
    """Base exception for the Gmail inbox provider (auth, connection, or
    protocol failures). Callers (app.services.gmail_inbox) catch this
    single type to distinguish "not configured" / "auth rejected" /
    "connection failed" from a persistence-layer error, mirroring
    app.collectors.base.CollectorError's role for job collectors.
    """


class GmailAuthError(GmailProviderError):
    """IMAP login was rejected (bad username/App Password), or credentials
    are not configured at all. Not retried — retrying with the same
    credentials cannot succeed.
    """


class GmailConnectionError(GmailProviderError):
    """The IMAP server could not be reached, or an IMAP command other than
    login failed (SELECT/STATUS/SEARCH/FETCH).
    """


@dataclass(frozen=True)
class ParsedAttachment:
    """Attachment metadata only — Stage 7A never downloads, stores, or
    inspects attachment content. `size` is the decoded byte length,
    computed only to report it; the decoded bytes are discarded
    immediately afterward and never persisted.
    """

    filename: str | None
    content_type: str
    size: int | None


@dataclass(frozen=True)
class ParsedGmailMessage:
    """One fully-parsed, ready-to-persist inbound/outbound message.

    `uid`/`uid_validity`/`mailbox` are the stable provider identity used
    for dedup (see app/db/gmail_repository.py) — IMAP UIDs are only
    guaranteed unique for as long as UIDVALIDITY doesn't change, so both
    must be stored and compared together, never the UID alone.
    `message_id_header` (the RFC 5322 Message-ID) is kept separately for
    threading, but is deliberately NOT the dedup identity: it can be
    missing, and in principle a malformed/replayed message could repeat
    one.
    """

    mailbox: str
    uid: int
    uid_validity: int
    message_id_header: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    from_address: str | None
    from_display_name: str | None
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    subject: str
    sent_at: datetime | None
    direction: Direction
    body_plain: str
    body_truncated: bool
    has_html: bool
    attachments: tuple[ParsedAttachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GmailFetchResult:
    """Everything one sync run's IMAP fetch produced.

    `skipped_count` counts messages that could not be parsed into a
    ParsedGmailMessage at all (structurally malformed MIME, unreadable
    fetch response) — distinct from a persistence failure, which the
    caller (app.services.gmail_inbox) tracks separately once messages
    reach the database layer.
    """

    messages: tuple[ParsedGmailMessage, ...]
    skipped_count: int


class ImapClient(Protocol):
    """The subset of imaplib.IMAP4_SSL's interface this provider uses.

    Every method here is read-only by construction — there is no `store`,
    `expunge`, `copy`, or `append` in this Protocol, so a real
    imaplib.IMAP4_SSL instance passed in can only ever be driven through
    these read operations by code that only has this narrower type in
    hand. Exists so tests can inject a lightweight fake instead of opening
    a real IMAP connection — mirrors
    app.collectors.xing_email.ImapClient's own rationale.
    """

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]: ...

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]: ...

    def status(self, mailbox: str, names: str) -> tuple[str, list[bytes]]: ...

    def uid(self, command: str, *args: str) -> tuple[str, list]: ...

    def close(self) -> tuple[str, list[bytes]]: ...

    def logout(self) -> tuple[str, list[bytes]]: ...
