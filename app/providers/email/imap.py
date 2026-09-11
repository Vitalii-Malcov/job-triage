"""Gmail inbox IMAP provider (Stage 7A).

Read-only, idempotent fetch of the configured Gmail mailbox's messages,
normalized into `ParsedGmailMessage` for app.services.gmail_inbox to
persist. This module performs ZERO business-logic interpretation of a
message: no classification, no job/application linkage, no LLM call —
see app/providers/email/base.py's module docstring for the hard
"never make an outbound HTTP request from email content" constraint that
applies here exactly as it does to app/collectors/xing_email.py.

Deliberately NOT a refactor of XingEmailCollector / merged with it: the
two mailboxes serve different purposes (job-digest ingestion that maps
into `Job` rows vs. the user's real reply inbox stored as its own
correspondence schema), have independent credentials
(GMAIL_* vs XING_MAILBOX_*, see app/core/config.py), and XING's collector
already has a stable, tested IMAP flow that must not be disturbed. This
provider duplicates a small amount of connect/disconnect/select
boilerplate rather than risk changing XING's behavior for the sake of
sharing it.

Threading limitation (documented, not a bug): standard IMAP (as used via
the `ImapClient` Protocol in base.py) does not expose Gmail's own
X-GM-THRID extension attribute, so this provider never invents/reads a
Gmail-native thread id. Instead it derives thread linkage purely from the
Message-ID / In-Reply-To / References headers (see
app/db/gmail_repository.py's `resolve_thread_anchor`). A message whose
mail client only sets In-Reply-To (no References) links to its immediate
parent's Message-ID rather than the true thread root, so a small minority
of dropped-References threads may end up split across more than one
GmailThreadRecord. app/db/gmail_repository.py additionally guards against
a *reused* Message-ID being trusted as high-confidence proof of shared
conversation (GMAIL-011) — see that module's docstring. Full server-side
Gmail threading is left for a later stage if ever needed.

**Read-only / no-Seen guarantee (GMAIL-001).** Every body fetch uses
`BODY.PEEK[]`, never bare `RFC822`/`BODY[]` — see base.py's module
docstring for why a non-PEEK fetch would itself mutate the mailbox
(setting `\\Seen`) even though it looks like a pure read.
"""

import asyncio
import contextlib
import email
import imaplib
import logging
import re
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from app.providers.email.base import (
    MAX_ADDRESS_LENGTH,
    MAX_ADDRESSES_PER_HEADER,
    MAX_ATTACHMENTS,
    MAX_BODY_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_HEADER_VALUE_LENGTH,
    MAX_MESSAGES_PER_SYNC,
    MAX_MIME_DEPTH,
    MAX_MIME_PARTS,
    MAX_RAW_MESSAGE_SIZE,
    MAX_REFERENCES,
    MAX_SUBJECT_LENGTH,
    Direction,
    GmailAuthError,
    GmailConnectionError,
    GmailFetchResult,
    ImapClient,
    ParsedAttachment,
    ParsedGmailMessage,
    normalize_account_key,
)
from app.providers.email.imap_deadline import (
    IMAP_SESSION_DEADLINE_SECONDS,
    DeadlineIMAP4SSL,
    ImapSessionDeadline,
)
from app.providers.email.mime_utils import decode_mime_part
from app.utils.config_flags import is_configured

logger = logging.getLogger(__name__)

# AUD-005: bounds every blocking socket operation on the IMAP connection
# (connect, TLS handshake, and every later imaplib call on the same
# socket -- SELECT/STATUS/UID SEARCH/UID FETCH/CLOSE/LOGOUT all share it).
# A hung/black-holed IMAP peer raises socket.timeout within this bound
# instead of blocking the worker thread (see `fetch`'s
# asyncio.to_thread docstring) indefinitely -- an asyncio-level timeout
# wrapped around that thread could not itself unblock or cancel a still
# in-flight blocking socket call, only give up waiting on it, leaking the
# thread. 30s is generous for a full BODY.PEEK[] fetch of one message
# while still bounding a truly unresponsive server.
IMAP_OPERATION_TIMEOUT_SECONDS = 30.0

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)")
_RFC822_SIZE_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"')

# FINAL-004 (Astra R5A): reasons `_fetch_one` classifies as PERMANENT --
# the message's own content is what's wrong, so retrying will fail
# identically forever -- as opposed to every other `_fetch_one` `None`
# return (fetch failed, malformed response shape, transport error),
# which is a transient/retryable skip and never recorded via these. See
# app.db.models.GmailPermanentSkipRecord's docstring for why this
# distinction is what makes durable forward progress possible.
PERMANENT_SKIP_REASON_OVERSIZED = "OVERSIZED"
PERMANENT_SKIP_REASON_PARSE_FAILED = "PARSE_FAILED"
PERMANENT_SKIP_REASON_INVALID_UID = "INVALID_UID"


def _decode_mime_words(raw: str) -> str:
    """Decode an RFC 2047 encoded-word header value (Subject, display
    names) into plain text. Mirrors
    app.collectors.xing_email._decode_subject, generalized to any header.
    """
    if not raw:
        return ""
    decoded_parts = []
    for text, encoding in decode_header(raw):
        if isinstance(text, bytes):
            decoded_parts.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded_parts.append(text)
    return "".join(decoded_parts)


def _clean_header(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    return value[:MAX_HEADER_VALUE_LENGTH]


def _parse_single_address(header_value: str | None) -> tuple[str | None, str | None]:
    if not header_value:
        return None, None
    display_name, address = parseaddr(header_value)
    decoded_name = _decode_mime_words(display_name).strip() or None
    if decoded_name:
        decoded_name = decoded_name[:MAX_DISPLAY_NAME_LENGTH]
    clean_address = address.strip()[:MAX_ADDRESS_LENGTH] or None
    return decoded_name, clean_address


def _parse_address_list(header_value: str | None) -> tuple[str, ...]:
    if not header_value:
        return ()
    addresses = []
    for _name, address in getaddresses([header_value]):
        address = address.strip()
        if address:
            addresses.append(address[:MAX_ADDRESS_LENGTH])
        if len(addresses) >= MAX_ADDRESSES_PER_HEADER:
            break
    return tuple(addresses)


def _parse_references(header_value: str | None) -> tuple[str, ...]:
    if not header_value:
        return ()
    tokens = header_value.split()
    bounded = [token[:MAX_HEADER_VALUE_LENGTH] for token in tokens[:MAX_REFERENCES]]
    return tuple(bounded)


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_internal_date(fetch_header: bytes) -> datetime | None:
    """S7E-011 (Codex re-review): extract the server-assigned IMAP
    INTERNALDATE from a `(INTERNALDATE BODY.PEEK[])` fetch response's
    header line (e.g. `1 (UID 1 INTERNALDATE "07-Sep-2026 12:34:56 +0000"
    BODY[] {123}`), per RFC 3501's `"dd-Mon-yyyy hh:mm:ss +zzzz"` format —
    NEVER the sender-controlled RFC 5322 `Date` header (see `_parse_date`
    above, used only for the separate, still-untrusted `sent_at` field).

    Returns None (an honestly documented gap, same shape as
    `_read_message_size`'s RFC822.SIZE fallback) if the server's response
    omitted INTERNALDATE entirely or the value doesn't match the RFC 3501
    format — real Gmail IMAP always answers INTERNALDATE, so this is not
    expected to be reachable against Gmail itself. The caller
    (app.db.gmail_repository.upsert_message) falls back to its own
    wall-clock persist time in that case.
    """
    match = _INTERNALDATE_RE.search(fetch_header)
    if not match:
        return None
    raw_value = match.group(1).decode("ascii", errors="replace")
    try:
        parsed = datetime.strptime(raw_value, "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _attachment_metadata(part: Message, filename: str | None) -> ParsedAttachment:
    decoded_filename = _decode_mime_words(filename).strip()[:255] if filename else None
    content_type = part.get_content_type()
    # Decoding here is purely to measure byte length for metadata — the
    # decoded bytes are never stored, returned beyond this int, or
    # otherwise inspected/opened. See base.py's ParsedAttachment docstring
    # (GMAIL-006) for the honest transfer-vs-persistence distinction.
    payload = part.get_payload(decode=True)
    size = len(payload) if payload is not None else None
    return ParsedAttachment(filename=decoded_filename or None, content_type=content_type, size=size)


def _is_attachment_part(part: Message) -> bool:
    # GMAIL-004: an embedded email (message/rfc822) is always treated as
    # an opaque attachment, regardless of whether it declares an explicit
    # Content-Disposition — its own inner parts must never be mistaken for
    # the parent message's body. See `_walk_body_and_attachments`, which
    # prunes this part's entire subtree instead of recursing into it.
    if part.get_content_type() == "message/rfc822":
        return True
    disposition = str(part.get("Content-Disposition") or "").lower()
    if "attachment" in disposition:
        return True
    return bool(part.get_filename()) and "inline" not in disposition


def _walk_body_and_attachments(
    part: Message,
    attachments: list[ParsedAttachment],
    state: dict,
    depth: int = 0,
) -> None:
    """Recursively extract body text + attachment metadata, pruning an
    attachment's entire subtree instead of flattening it (GMAIL-004).

    `email.message.Message.walk()` flattens every part in the MIME tree,
    including the descendants of a `message/rfc822` (or any other)
    attachment — so a naive "skip parts that are themselves multipart"
    filter still lets an *attached* email's own inner text/plain part
    reach the loop and get mistaken for the parent message's body. This
    function instead only ever descends into a part's children after
    confirming that part itself is not an attachment; an attachment part
    is recorded (bounded by MAX_ATTACHMENTS) and its subtree is never
    visited at all.

    `depth`/`state["parts_seen"]` bound pathological MIME structures
    (very deep nesting / very many parts) — GMAIL-005.
    """
    if depth > MAX_MIME_DEPTH:
        return

    if _is_attachment_part(part):
        if len(attachments) < MAX_ATTACHMENTS:
            attachments.append(_attachment_metadata(part, part.get_filename()))
        return

    if part.is_multipart():
        for sub_part in part.get_payload():
            if state["parts_seen"] >= MAX_MIME_PARTS:
                return
            state["parts_seen"] += 1
            _walk_body_and_attachments(sub_part, attachments, state, depth + 1)
        return

    content_type = part.get_content_type()
    if content_type == "text/plain" and not state["body_plain"]:
        state["body_plain"] = decode_mime_part(part)
    elif content_type == "text/html":
        state["has_html"] = True


def _extract_content(
    msg: Message,
) -> tuple[str, bool, bool, tuple[ParsedAttachment, ...]]:
    """Extract plaintext body + has_html flag + bounded attachment metadata.

    Plaintext is preferred over HTML (never rendered/executed/fetched —
    see this module's docstring). If only an HTML part exists, body_plain
    stays "" and has_html is True; no HTML-to-text conversion is
    attempted in Stage 7A.
    """
    attachments: list[ParsedAttachment] = []
    state = {"body_plain": "", "has_html": False, "parts_seen": 0}
    _walk_body_and_attachments(msg, attachments, state)

    body_plain = state["body_plain"]
    truncated = len(body_plain) > MAX_BODY_LENGTH
    if truncated:
        body_plain = body_plain[:MAX_BODY_LENGTH]
    return body_plain, truncated, state["has_html"], tuple(attachments)


def _direction(*, trusted_outbound: bool) -> Direction:
    """S7E-001 (Codex remediation, HIGH): direction is decided ENTIRELY by
    which mailbox this message was fetched from — never by inspecting the
    message's own `From` header. A `From` header claiming to be our own
    account address is trivially forgeable by anyone able to send us mail
    at all (plain SMTP header spoofing, no mailbox access required); the
    OLD `from_address == account_address` comparison this replaced would
    let such a spoofed message land in INBOX and be trusted as a genuine
    OUTBOUND message we sent — the exact anchor
    app.services.follow_up_eligibility uses to decide a follow-up is due,
    and (via that anchor's own `to_addresses`) the very recipient a
    follow-up would be sent to.

    `trusted_outbound` is caller-supplied per mailbox (see
    `GmailImapProvider.__init__`) — True only when this provider instance
    was explicitly configured to sync the account's real, authenticated
    Sent-mail folder (`Settings.gmail_sent_mailbox`), never derived from
    message content. Every message fetched from any OTHER mailbox
    (including the primary INBOX) is unconditionally INBOUND, regardless
    of its `From` header.
    """
    return "OUTBOUND" if trusted_outbound else "INBOUND"


class GmailImapProvider:
    """Fetches messages from the configured Gmail mailbox via IMAP4_SSL,
    read-only. Never marks read/unread, never deletes/moves/labels, never
    sends or drafts — see base.py's module docstring for the full
    read-only guarantee and its hard no-outbound-HTTP constraint.
    """

    def __init__(
        self,
        imap_host: str,
        imap_port: int,
        username: str,
        app_password: str,
        mailbox: str = "INBOX",
        lookback_days: int = 30,
        imap_client: ImapClient | None = None,
        get_known_uids: Callable[[int, list[int]], set[int]] | None = None,
        trusted_outbound: bool = False,
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self.app_password = app_password
        self.mailbox = mailbox
        self.lookback_days = lookback_days
        # S7E-001: True only for a provider instance explicitly constructed
        # to sync the account's real Sent-mail folder — see `_direction`'s
        # docstring. Every message this instance parses is classified
        # OUTBOUND/INBOUND purely from this flag, never from message
        # content.
        self.trusted_outbound = trusted_outbound
        # GMAIL-002: the stable, non-secret account identity every
        # ParsedGmailMessage from this provider is scoped by.
        self.account_key = normalize_account_key(username)
        # Injected only by tests, to avoid a real IMAP connection —
        # mirrors app.collectors.xing_email.XingEmailCollector.
        self._injected_client = imap_client
        # GMAIL-005 starvation fix (GMAIL-012: bulk, not per-UID): bound to
        # the caller's db.Session via closure (see app/api/routes.py's
        # _run_gmail_sync) — lets this provider skip already-persisted
        # UIDs BEFORE applying MAX_MESSAGES_PER_SYNC, so the cap only ever
        # constrains genuinely new work. Without this, a sustained backlog
        # larger than the cap would waste every sync's entire budget
        # re-fetching the same already-known messages and never make
        # progress on newer ones — merely relocating the starvation
        # failure mode rather than fixing it (oldest-first alone is not
        # sufficient; see _fetch_sync). Takes the FULL candidate UID list
        # and returns the known subset in one call — a Codex probe proved
        # a per-UID callable here reproduces as one DB query per SEARCH
        # result (100 UIDs -> 100 SELECTs) before the cap is even applied.
        self._get_known_uids = get_known_uids or (lambda _uid_validity, _uids: set())

    async def fetch(self, since: datetime | None = None) -> GmailFetchResult:
        if not is_configured(self.username) or not is_configured(self.app_password):
            raise GmailAuthError("GMAIL_USERNAME / GMAIL_APP_PASSWORD is not configured")

        since_date = since or (datetime.now(UTC) - timedelta(days=self.lookback_days))

        # imaplib is synchronous/blocking; run it off the event loop via a
        # worker thread, same rationale as XingEmailCollector.fetch().
        return await asyncio.to_thread(self._fetch_sync, since_date)

    def _fetch_sync(self, since: datetime) -> GmailFetchResult:
        client = self._injected_client
        owns_connection = client is None
        # AUD-005: a real total wall-clock deadline for this whole session
        # (login through select/search/fetch/close/logout), not just the
        # per-read inactivity timeout `timeout=` already gives the socket
        # -- see imap_deadline.py's module docstring for why a slow-drip
        # peer needs this. Only applied when this call owns the
        # connection: a test-injected client has no real socket to bound.
        deadline = ImapSessionDeadline(IMAP_SESSION_DEADLINE_SECONDS) if owns_connection else None

        with contextlib.ExitStack() as stack:
            if deadline is not None:
                stack.enter_context(deadline)
            if client is None:
                client = self._connect(deadline)
            try:
                return self._fetch_sync_body(client, since, deadline)
            except OSError as exc:
                if deadline is not None and deadline.exceeded:
                    logger.warning("gmail_imap_session_deadline_exceeded")
                else:
                    logger.warning("gmail_imap_operation_failed error_type=%s", type(exc).__name__)
                raise GmailConnectionError("IMAP operation failed") from exc
            finally:
                if owns_connection:
                    self._disconnect(client)

    def _fetch_sync_body(
        self,
        client: ImapClient,
        since: datetime,
        deadline: ImapSessionDeadline | None,
    ) -> GmailFetchResult:
        typ, _ = client.select(self.mailbox, readonly=True)
        if typ != "OK":
            raise GmailConnectionError("IMAP SELECT failed")

        uid_validity = self._read_uid_validity(client)

        criteria = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
        typ, data = client.uid("search", None, criteria)
        if typ != "OK":
            raise GmailConnectionError("IMAP UID SEARCH failed")

        uids = data[0].split() if data and data[0] else []
        skipped_count = 0

        # GMAIL-005 starvation fix (GMAIL-012: one bulk lookup, not
        # one query per UID): filter out UIDs already known to be
        # persisted BEFORE the cap below is applied — otherwise, once
        # a backlog exceeds MAX_MESSAGES_PER_SYNC, every sync would
        # spend its entire budget re-fetching bodies for the same
        # already-known messages and never reach anything new,
        # regardless of which end (oldest/newest) is prioritized.
        # Malformed (non-integer) UID tokens are left in the
        # candidate list unfiltered — downstream per-message handling
        # in _fetch_one treats them as malformed the same way it
        # always has.
        candidate_uid_ints: list[int] = []
        for uid_bytes in uids:
            try:
                candidate_uid_ints.append(int(uid_bytes))
            except ValueError:
                continue

        known_uids = (
            self._get_known_uids(uid_validity, candidate_uid_ints) if candidate_uid_ints else set()
        )

        not_yet_known = []
        for uid_bytes in uids:
            try:
                uid_int = int(uid_bytes)
            except ValueError:
                not_yet_known.append(uid_bytes)
                continue
            if uid_int not in known_uids:
                not_yet_known.append(uid_bytes)
        uids = not_yet_known

        # GMAIL-005: bound how many message bodies one sync run will
        # fetch at all. The OLDEST UIDs (Gmail UIDs are monotonically
        # increasing within a mailbox) are prioritized, not the
        # newest — deliberately, to avoid a starvation failure mode:
        # if arrivals within the lookback window sustainedly exceed
        # the cap on every single sync, always preferring the newest
        # UIDs would mean the same tail of older-but-still-in-window
        # messages is deferred run after run, potentially aging them
        # completely out of the lookback window before they are ever
        # fetched — a silent, permanent loss, not just a delay.
        # Prioritizing the oldest UIDs instead means each capped sync
        # makes real forward progress on the backlog; messages
        # deferred this run are still the newest ones, so they remain
        # within the lookback window (and get retried) on the next
        # sync as long as sync frequency leaves them enough runway
        # before GMAIL_LOOKBACK_DAYS. This does not eliminate
        # starvation in the degenerate case of arrivals perpetually
        # exceeding the cap forever — no bounded-per-run design can —
        # but it converts "the same messages always lost" into "the
        # backlog drains oldest-first," which is the honest, weaker
        # guarantee this cap actually provides.
        if len(uids) > MAX_MESSAGES_PER_SYNC:
            overflow = len(uids) - MAX_MESSAGES_PER_SYNC
            try:
                uids = sorted(uids, key=int)[:MAX_MESSAGES_PER_SYNC]
            except ValueError:
                uids = uids[:MAX_MESSAGES_PER_SYNC]
            skipped_count += overflow
            logger.warning("gmail_sync_message_cap_exceeded cap=%s", MAX_MESSAGES_PER_SYNC)

        messages: list[ParsedGmailMessage] = []
        # FINAL-004 (Astra R5A): (uid, reason) pairs for every UID this
        # run determined is PERMANENTLY unfetchable (message content
        # itself is the problem) -- see GmailFetchResult.permanently_skipped
        # and app.db.models.GmailPermanentSkipRecord for how the caller
        # persists these so a future sync's candidate list excludes them,
        # closing the starvation bug a never-recorded permanent skip
        # would otherwise cause.
        permanently_skipped: list[tuple[int, str]] = []
        for uid_bytes in uids:
            # AUD-005: once the total session deadline has fired, the
            # connection's socket is already forcibly closed (see
            # ImapSessionDeadline) -- stop issuing further FETCHes on it
            # instead of letting each remaining UID fail one at a time.
            if deadline is not None and deadline.exceeded:
                break
            try:
                parsed, permanent_skip_reason = self._fetch_one(client, uid_bytes, uid_validity)
            except OSError:
                # Codex gate follow-up (Astra R4A MEDIUM): a transport
                # -level failure mid-FETCH (the between-iterations
                # `.exceeded` check above cannot catch this -- the
                # watchdog can force-close the socket WHILE a FETCH is
                # already blocked in flight, raising OSError from inside
                # _fetch_one rather than being observed cleanly at the
                # top of the next iteration). If the deadline is what
                # caused this, stop cleanly and let the SAME post-loop
                # logic below return the messages already completed,
                # flagged via `deadline_exceeded` -- never silently
                # discarded. If the deadline did NOT cause it (a
                # genuine, unexpected connection failure), re-raise so
                # `_fetch_sync`'s existing outer handler logs and raises
                # `GmailConnectionError`, unchanged.
                if deadline is not None and deadline.exceeded:
                    break
                raise
            if parsed is None:
                skipped_count += 1
                if permanent_skip_reason is not None:
                    permanently_skipped.append((int(uid_bytes), permanent_skip_reason))
            else:
                messages.append(parsed)

        # NEW-001 (Astra R4A): a prior version of this method RAISED
        # GmailConnectionError here, discarding `messages` entirely --
        # every message that had already completed fetch+parse before the
        # deadline fired was silently lost, never persisted, with no
        # signal to the caller that real work had been done. A backlog
        # that consistently exceeds the deadline would then repeatedly
        # re-fetch the same oldest-UID prefix while persisting zero
        # progress, and those messages could eventually age out of the
        # lookback window entirely -- a real, permanent data-loss bug, not
        # a cosmetic one. Returning the completed batch instead (flagged
        # via `deadline_exceeded`, never silently reported as full
        # success) lets `app.services.gmail_inbox.GmailInboxService.sync`
        # persist everything that finished in time and report a truthful
        # partial outcome. A GENUINE connection/auth failure (the socket
        # dying during SELECT/STATUS/SEARCH/one message's own FETCH, i.e.
        # anywhere other than this clean post-loop deadline check) still
        # raises normally via `_fetch_sync`'s OSError handler below --
        # this change touches ONLY the case where the loop above already
        # finished (by exhausting `uids` or breaking cleanly) and nothing
        # actually failed except running out of time.
        return GmailFetchResult(
            messages=tuple(messages),
            skipped_count=skipped_count,
            deadline_exceeded=deadline is not None and deadline.exceeded,
            uid_validity=uid_validity,
            permanently_skipped=tuple(permanently_skipped),
        )

    def _read_uid_validity(self, client: ImapClient) -> int:
        typ, data = client.status(self.mailbox, "(UIDVALIDITY)")
        if typ != "OK":
            raise GmailConnectionError("IMAP STATUS failed")
        for line in data:
            if not isinstance(line, bytes):
                continue
            match = _UIDVALIDITY_RE.search(line)
            if match:
                value = int(match.group(1))
                # GMAIL-009: UIDVALIDITY 0 is a reserved/invalid value per
                # RFC 3501 — never trust it as a real mailbox generation.
                if value <= 0:
                    raise GmailConnectionError("IMAP reported an invalid UIDVALIDITY")
                return value
        raise GmailConnectionError("Could not determine mailbox UIDVALIDITY")

    def _read_message_size(self, client: ImapClient, uid_bytes: bytes) -> int | None:
        """GMAIL-005: a lightweight `RFC822.SIZE` FETCH, used to decide
        whether the full body is even worth fetching — never itself
        transfers the message body.

        Returns None if the size could not be determined (some
        servers/fakes may omit or malform it); the caller (`_fetch_one`)
        treats that as "proceed, size unknown" rather than failing
        closed. **This is a real, documented gap, not just a defensive
        fallback**: in that case the full `BODY.PEEK[]` fetch is
        requested with NO pre-transfer size bound at all for that one
        message — MAX_RAW_MESSAGE_SIZE is not enforced in this path. See
        base.py's MAX_RAW_MESSAGE_SIZE docstring for the full residual-risk
        statement. This is accepted because (a) real Gmail IMAP always
        answers RFC822.SIZE, so the gap is not expected to be reachable
        against Gmail itself, and (b) MAX_MESSAGES_PER_SYNC/MAX_MIME_PARTS/
        MAX_BODY_LENGTH still bound the surrounding blast radius even when
        this one optimization doesn't fire.
        """
        try:
            typ, data = client.uid("fetch", uid_bytes, "(RFC822.SIZE)")
        except Exception:
            return None
        if typ != "OK" or not data:
            return None
        for item in data:
            candidate = item[0] if isinstance(item, tuple) else item
            if not isinstance(candidate, bytes):
                continue
            match = _RFC822_SIZE_RE.search(candidate)
            if match:
                return int(match.group(1))
        return None

    def _connect(self, deadline: ImapSessionDeadline | None = None) -> imaplib.IMAP4_SSL:
        try:
            # AUD-001: imaplib.IMAP4_SSL's default ssl_context (when None)
            # is built via ssl._create_stdlib_context(), which -- unlike
            # ssl.create_default_context() -- sets verify_mode=CERT_NONE
            # and check_hostname=False. Without an explicit verifying
            # context, this connection would accept ANY certificate,
            # including a forged one from a MITM peer, over a channel
            # that authenticates with a real mailbox password. AUD-005:
            # `timeout=` bounds each individual blocking socket read on
            # this connection (see IMAP_OPERATION_TIMEOUT_SECONDS).
            ssl_context = ssl.create_default_context()
            if deadline is not None:
                # AUD-005 (narrow re-review): DeadlineIMAP4SSL binds the
                # watchdog to the real socket from the moment it's
                # created -- covering the TCP connect, TLS handshake, and
                # (once this constructor call returns and
                # imaplib.IMAP4.__init__ proceeds to IMAP4._connect())
                # the greeting/CAPABILITY reads, not just the commands
                # this provider issues after _connect() returns. See
                # imap_deadline.py's DeadlineIMAP4SSL docstring for why
                # binding the socket only after this call returned (the
                # previous version of this fix) left the whole
                # constructor unprotected.
                client = DeadlineIMAP4SSL(
                    self.imap_host,
                    self.imap_port,
                    ssl_context=ssl_context,
                    timeout=IMAP_OPERATION_TIMEOUT_SECONDS,
                    deadline=deadline,
                )
            else:
                client = imaplib.IMAP4_SSL(
                    self.imap_host,
                    self.imap_port,
                    ssl_context=ssl_context,
                    timeout=IMAP_OPERATION_TIMEOUT_SECONDS,
                )
        except (OSError, imaplib.IMAP4.error) as exc:
            # GMAIL-003 (Codex final review, MEDIUM): constructor-time
            # protocol failures -- imaplib.IMAP4.error/abort, e.g. a
            # malformed/aborted greeting or CAPABILITY response, or the
            # deadline forcing the socket closed mid-read -- can carry
            # raw server-controlled text just like an OSError can carry
            # raw host/port text. `imaplib.IMAP4.abort` subclasses
            # `imaplib.IMAP4.error`, so catching `error` alone already
            # covers both. Never interpolate exc/host/port into the
            # raised message — see base.py's GmailProviderError
            # docstring. Internal-only diagnosis uses type(exc).__name__.
            logger.warning("gmail_connect_failed error_type=%s", type(exc).__name__)
            raise GmailConnectionError(
                "Could not connect to the configured Gmail IMAP host"
            ) from exc

        try:
            client.login(self.username, self.app_password)
        except imaplib.IMAP4.error as exc:
            logger.warning("gmail_login_failed error_type=%s", type(exc).__name__)
            raise GmailAuthError("Gmail mailbox IMAP login was rejected") from exc
        return client

    def _disconnect(self, client: ImapClient) -> None:
        try:
            client.close()
        except Exception as exc:
            logger.warning("gmail_imap_close_failed error_type=%s", type(exc).__name__)
        try:
            client.logout()
        except Exception as exc:
            logger.warning("gmail_imap_logout_failed error_type=%s", type(exc).__name__)

    def _fetch_one(
        self, client: ImapClient, uid_bytes: bytes, uid_validity: int
    ) -> tuple[ParsedGmailMessage | None, str | None]:
        """Returns `(parsed, permanent_skip_reason)`.

        `permanent_skip_reason` is non-None (FINAL-004, Astra R5A) ONLY
        for a narrow set of cases that are a pure, deterministic
        function of this message's own immutable CONTENT: an oversized
        raw size, an invalid UID value, or `email.message_from_bytes`
        itself failing to parse the raw bytes into a Message object at
        all (confirmed empirically: this is notoriously lenient and
        essentially never raises in practice, but when it does, it is a
        pure function of the bytes). Retrying can never change any of
        these outcomes, so they are safe to durably record.

        Astra correction: this deliberately does NOT extend to an
        exception from OUR OWN downstream extraction logic
        (`_parse_message` and everything it calls, after
        `email.message_from_bytes` already succeeded) -- an internal
        programming bug in that code is far more likely than a genuine
        property of the message, and durably recording it as permanent
        would mean a future bugfix could never recover a message that
        was only ever "unparseable" because of OUR bug. Those cases
        (see the outer except below) return `permanent_skip_reason=None`
        and stay eligible for retry indefinitely, exactly like every
        OTHER transient `parsed is None` case (a non-OK FETCH, malformed
        response shape, or any other transport/protocol hiccup) already
        does. See
        app.providers.email.base.GmailFetchResult.permanently_skipped
        and app.db.models.GmailPermanentSkipRecord for how the caller
        uses this distinction.
        """
        # GMAIL-005: check the server-reported size BEFORE transferring
        # the body at all. An unknown size (None) proceeds rather than
        # failing closed — see _read_message_size's docstring.
        size = self._read_message_size(client, uid_bytes)
        if size is not None and size > MAX_RAW_MESSAGE_SIZE:
            logger.warning("gmail_message_oversized")
            return None, PERMANENT_SKIP_REASON_OVERSIZED

        try:
            # GMAIL-001: BODY.PEEK[] fetches the full message without
            # setting \Seen — a bare RFC822/BODY[] fetch would mutate the
            # mailbox as a side effect of this "read". INTERNALDATE is
            # requested in the SAME fetch (S7E-011, Codex re-review): it
            # costs nothing extra over BODY.PEEK[] (one round trip either
            # way) and is the server-assigned arrival timestamp this
            # project's Gmail correspondence chronology now trusts — see
            # `_parse_internal_date` and ParsedGmailMessage.provider_arrival_at.
            typ, msg_data = client.uid("fetch", uid_bytes, "(INTERNALDATE BODY.PEEK[])")
        except OSError:
            # Codex gate follow-up (Astra R4A MEDIUM): a genuine
            # transport/protocol failure mid-FETCH (including the total
            # session deadline's watchdog force-closing the socket
            # WHILE this call is blocked in flight) must NEVER be
            # reinterpreted as an ordinary skipped message — that would
            # both quietly discard the distinction between "this one
            # message was unreadable" and "the whole connection just
            # died", and (for the deadline case specifically) risk the
            # caller's `for uid_bytes in uids` loop just moving on to
            # the NEXT uid on an already-dead socket instead of stopping
            # cleanly. Propagate to `_fetch_sync_body`'s loop, which
            # decides: deadline-caused -> stop and preserve what already
            # completed; genuine failure -> re-raise up to `_fetch_sync`'s
            # existing OSError handler, unchanged.
            raise
        except Exception as exc:
            logger.warning("gmail_message_fetch_error error_type=%s", type(exc).__name__)
            return None, None
        if typ != "OK" or not msg_data or msg_data[0] is None:
            logger.warning("gmail_message_fetch_failed")
            return None, None

        # GMAIL-010: everything from here — including validating the
        # shape of `msg_data[0]` itself — stays inside this single
        # per-message try/except, so a malformed/unexpected FETCH response
        # shape can never propagate out of _fetch_one and abort the rest
        # of the sync.
        try:
            uid = int(uid_bytes)
            if uid <= 0:
                logger.warning("gmail_message_invalid_uid")
                return None, PERMANENT_SKIP_REASON_INVALID_UID

            item = msg_data[0]
            if not isinstance(item, tuple) or len(item) < 2:
                logger.warning("gmail_message_fetch_response_malformed")
                return None, None
            raw_email = item[1]
            if not isinstance(raw_email, bytes | bytearray):
                logger.warning("gmail_message_fetch_response_malformed")
                return None, None

            fetch_header = item[0]
            provider_arrival_at = (
                _parse_internal_date(fetch_header)
                if isinstance(fetch_header, bytes | bytearray)
                else None
            )

            # FINAL-004 (Astra R5A correction): ONLY a failure parsing
            # the raw bytes themselves into a Message object is
            # classified PERMANENT -- a genuinely deterministic property
            # of these exact immutable bytes (email.message_from_bytes
            # is notoriously lenient and essentially never raises in
            # practice, confirmed empirically, but IS a pure function of
            # the bytes when it does). Any exception from OUR OWN
            # downstream extraction logic below (_parse_message and
            # everything it calls) is deliberately NOT classified
            # permanent -- it is far more likely to be an internal bug
            # in this project's own parsing code than an inherent
            # property of the message's content, and durably recording
            # it as permanent would mean a future bugfix could never
            # recover a message that was only ever unparseable because
            # of OUR bug, not its own content.
            try:
                msg = email.message_from_bytes(bytes(raw_email))
            except Exception as exc:
                logger.warning("gmail_message_bytes_unparseable error_type=%s", type(exc).__name__)
                return None, PERMANENT_SKIP_REASON_PARSE_FAILED

            parsed = self._parse_message(
                msg,
                uid=uid,
                uid_validity=uid_validity,
                provider_arrival_at=provider_arrival_at,
            )
            return parsed, None
        except Exception as exc:
            # GMAIL-010: everything above (validating msg_data[0]'s
            # shape, and OUR OWN _parse_message extraction logic) stays
            # inside this single per-message try/except, so a malformed/
            # unexpected FETCH response shape or an internal bug in our
            # own parsing code can never propagate out of _fetch_one and
            # abort the rest of the sync -- but (see the narrower
            # try/except above) this is deliberately NOT classified as
            # a permanent skip: an internal programming error must stay
            # retryable, never durably blamed on the message's content.
            logger.warning("gmail_message_parse_failed error_type=%s", type(exc).__name__)
            return None, None

    def _parse_message(
        self,
        msg: Message,
        *,
        uid: int,
        uid_validity: int,
        provider_arrival_at: datetime | None,
    ) -> ParsedGmailMessage:
        message_id = _clean_header(msg.get("Message-ID"))
        in_reply_to = _clean_header(msg.get("In-Reply-To"))
        references = _parse_references(msg.get("References"))
        from_name, from_address = _parse_single_address(msg.get("From"))
        to_addresses = _parse_address_list(msg.get("To"))
        cc_addresses = _parse_address_list(msg.get("Cc"))
        subject = _decode_mime_words(msg.get("Subject", ""))[:MAX_SUBJECT_LENGTH]
        sent_at = _parse_date(msg.get("Date"))
        body_plain, body_truncated, has_html, attachments = _extract_content(msg)
        direction = _direction(trusted_outbound=self.trusted_outbound)

        return ParsedGmailMessage(
            account_key=self.account_key,
            mailbox=self.mailbox,
            uid=uid,
            uid_validity=uid_validity,
            message_id_header=message_id,
            in_reply_to=in_reply_to,
            references=references,
            from_address=from_address,
            from_display_name=from_name,
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            subject=subject,
            sent_at=sent_at,
            direction=direction,
            body_plain=body_plain,
            body_truncated=body_truncated,
            has_html=has_html,
            provider_arrival_at=provider_arrival_at,
            attachments=attachments,
        )
