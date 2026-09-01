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
command (LOGIN, SELECT ... readonly=True, STATUS, UID SEARCH, UID FETCH
of RFC822.SIZE/BODY.PEEK[], CLOSE, LOGOUT). Nothing here ever calls STORE,
EXPUNGE, COPY, APPEND, or any other command that could mutate the mailbox
(mark read/unread, delete, move, label, send, or create a draft).
**`BODY.PEEK[]` — never bare `RFC822` or `BODY[]`** — is used specifically
because a plain `RFC822`/`BODY[]` FETCH is defined by RFC 3501 to
implicitly set the `\\Seen` flag as a side effect of transferring the
body; `.PEEK` is the documented way to fetch the same content without
that mutation. See app/providers/email/imap.py's
`test_module_never_calls_mailbox_write_commands`-style regression tests.

Attachment content note (see ParsedAttachment's docstring for the full,
honest contract): a bounded `BODY.PEEK[]` fetch transfers the complete
raw message, including attachment bytes, from the IMAP server — Stage 7A
does not use section-level partial fetches to avoid that transfer. What
Stage 7A guarantees is narrower and still true: attachment bytes are
never persisted, never opened/rendered, and never analyzed as business
content — only bounded metadata (filename/content_type/byte length) is
kept, and the decoded bytes are discarded immediately after measuring
their length.
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

# GMAIL-005: bounds applied BEFORE the expensive part of a fetch, not just
# after decoding.
#
# `MAX_RAW_MESSAGE_SIZE` gates a lightweight `RFC822.SIZE` FETCH against
# the server-reported size before the full `BODY.PEEK[]` body transfer is
# ever requested — an oversized message is skipped without pulling its
# body across the wire at all, PROVIDED the server actually answers
# RFC822.SIZE with a parseable value.
#
# Documented residual risk (honesty per GMAIL-005 — do not claim a
# stronger guarantee than the code provides): if RFC822.SIZE is missing,
# unparseable, or the server simply doesn't support it,
# app.providers.email.imap.GmailImapProvider._read_message_size returns
# None, and the caller (`_fetch_one`) proceeds to the full `BODY.PEEK[]`
# fetch anyway — the pre-transfer size gate is skipped entirely for that
# one message, not conservatively enforced. In that fallback path,
# MAX_RAW_MESSAGE_SIZE provides NO bound on the bytes transferred/held in
# memory for that message; the only bounds still in effect are
# MAX_BODY_LENGTH (post-transfer truncation of the parsed text) and
# MAX_MESSAGES_PER_SYNC (how many such messages one run will attempt).
# Real Gmail IMAP always answers RFC822.SIZE, so this fallback is not
# expected to be reachable in production against Gmail itself — it exists
# for any other/future IMAP server this provider might point at.
#
# `MAX_MESSAGES_PER_SYNC` bounds how many messages one sync run will fetch
# bodies for at all. The OLDEST UIDs are prioritized, not the newest — see
# app.providers.email.imap.GmailImapProvider._fetch_sync's comment for why
# preferring the newest UIDs would risk starving the same backlog of
# older messages out of the lookback window forever, rather than merely
# deferring them.
#
# `MAX_MIME_PARTS`/`MAX_MIME_DEPTH` bound the cost of walking a
# pathological (very wide or very deeply nested) MIME structure.
#
# Documented residual limitation (honesty per GMAIL-005/006): computing an
# individual attachment part's byte-length still requires decoding that
# part's payload (base64/quoted-printable) — IMAP has no cheap
# per-MIME-part size query in the subset of commands this project's
# read-only ImapClient Protocol exposes, and adding one (BODYSTRUCTURE
# parsing) was judged not worth the added parsing-surface complexity for
# Stage 7A. This decode is bounded, not unbounded, when the RFC822.SIZE
# gate above did fire; it is NOT bounded by MAX_RAW_MESSAGE_SIZE in the
# fallback path described above.
MAX_RAW_MESSAGE_SIZE = 5_000_000
MAX_MESSAGES_PER_SYNC = 500
MAX_MIME_PARTS = 500
MAX_MIME_DEPTH = 20


def normalize_account_key(username: str) -> str:
    """The stable, non-secret identity a mailbox account is scoped by
    (GMAIL-002) — never the password, never anything else secret.

    Deliberately simple (strip + casefold of the configured
    `GMAIL_USERNAME`): this is an account *identity* for dedup/threading
    namespacing, not a validated email address — an operator typo still
    produces a stable, self-consistent key.
    """
    return username.strip().casefold()


class GmailProviderError(Exception):
    """Base exception for the Gmail inbox provider (auth, connection, or
    protocol failures). Callers (app.services.gmail_inbox) catch this
    single type to distinguish "not configured" / "auth rejected" /
    "connection failed" from a persistence-layer error, mirroring
    app.collectors.base.CollectorError's role for job collectors.

    GMAIL-003: every message raised anywhere in this package is a fixed,
    static string — never an f-string embedding the *upstream*
    exception/server-response text (which could otherwise carry back a
    server-echoed mailbox address or other operator-identifying text to
    an API caller, or into a log line). Callers must also never log
    `str(exc)` for one of these — see app/services/gmail_inbox.py and
    app/api/routes.py's Gmail error handling, which log only
    `type(exc).__name__`.
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
    """Attachment metadata only.

    True, unconditionally: attachment content is never PERSISTED, never
    OPENED/rendered, and never analyzed as Stage 7A/7B business content —
    only this bounded metadata is kept.

    NOT unconditionally true: attachment BYTES may still be TRANSFERRED
    from the IMAP server as part of the bounded `BODY.PEEK[]` full-message
    fetch (see app/providers/email/base.py's module docstring and
    MAX_RAW_MESSAGE_SIZE) — Stage 7A does not use section-level partial
    IMAP fetches to avoid that transfer, and computing `size` below
    requires briefly decoding the part's payload. The decoded bytes are
    discarded immediately after measuring their length; they are never
    stored in `size` or anywhere else beyond this int.
    """

    filename: str | None
    content_type: str
    size: int | None


@dataclass(frozen=True)
class ParsedGmailMessage:
    """One fully-parsed, ready-to-persist inbound/outbound message.

    `account_key`/`mailbox`/`uid`/`uid_validity` together are the stable
    provider identity used for dedup (see app/db/gmail_repository.py).
    `account_key` (GMAIL-002) scopes identity to the configured mailbox
    account: IMAP UID/UIDVALIDITY are only meaningful within one account's
    mailbox, so switching `GMAIL_USERNAME` to a different account must
    never collide with — or silently inherit — another account's history.
    `message_id_header` (the RFC 5322 Message-ID) is kept separately for
    threading, but is deliberately NOT part of the dedup identity: it can
    be missing, and in principle a malformed/replayed message could repeat
    one (see GmailMessageRecord's docstring and
    app/db/gmail_repository.py's collision-aware thread anchoring,
    GMAIL-011).
    """

    account_key: str
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
    fetch response, oversized per MAX_RAW_MESSAGE_SIZE, or deferred past
    MAX_MESSAGES_PER_SYNC) — distinct from a persistence failure, which
    the caller (app.services.gmail_inbox) tracks separately once messages
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
