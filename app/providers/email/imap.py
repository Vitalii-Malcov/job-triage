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
import email
import imaplib
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from app.collectors.base import is_configured
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

logger = logging.getLogger(__name__)

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)")
_RFC822_SIZE_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)")


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


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


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
        state["body_plain"] = _decode_part(part)
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


def _direction(from_address: str | None, account_address: str) -> Direction:
    if from_address and from_address.casefold() == account_address.casefold():
        return "OUTBOUND"
    return "INBOUND"


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
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self.app_password = app_password
        self.mailbox = mailbox
        self.lookback_days = lookback_days
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
        if client is None:
            client = self._connect()

        try:
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
                self._get_known_uids(uid_validity, candidate_uid_ints)
                if candidate_uid_ints
                else set()
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
            for uid_bytes in uids:
                parsed = self._fetch_one(client, uid_bytes, uid_validity)
                if parsed is None:
                    skipped_count += 1
                else:
                    messages.append(parsed)
            return GmailFetchResult(messages=tuple(messages), skipped_count=skipped_count)
        finally:
            if owns_connection:
                self._disconnect(client)

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

    def _connect(self) -> imaplib.IMAP4_SSL:
        try:
            client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        except OSError as exc:
            # GMAIL-003: never interpolate the underlying OSError/host/port
            # into the raised message — see base.py's GmailProviderError
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
    ) -> ParsedGmailMessage | None:
        # GMAIL-005: check the server-reported size BEFORE transferring
        # the body at all. An unknown size (None) proceeds rather than
        # failing closed — see _read_message_size's docstring.
        size = self._read_message_size(client, uid_bytes)
        if size is not None and size > MAX_RAW_MESSAGE_SIZE:
            logger.warning("gmail_message_oversized")
            return None

        try:
            # GMAIL-001: BODY.PEEK[] fetches the full message without
            # setting \Seen — a bare RFC822/BODY[] fetch would mutate the
            # mailbox as a side effect of this "read".
            typ, msg_data = client.uid("fetch", uid_bytes, "(BODY.PEEK[])")
        except Exception as exc:
            logger.warning("gmail_message_fetch_error error_type=%s", type(exc).__name__)
            return None
        if typ != "OK" or not msg_data or msg_data[0] is None:
            logger.warning("gmail_message_fetch_failed")
            return None

        # GMAIL-010: everything from here — including validating the
        # shape of `msg_data[0]` itself — stays inside this single
        # per-message try/except, so a malformed/unexpected FETCH response
        # shape can never propagate out of _fetch_one and abort the rest
        # of the sync.
        try:
            uid = int(uid_bytes)
            if uid <= 0:
                logger.warning("gmail_message_invalid_uid")
                return None

            item = msg_data[0]
            if not isinstance(item, tuple) or len(item) < 2:
                logger.warning("gmail_message_fetch_response_malformed")
                return None
            raw_email = item[1]
            if not isinstance(raw_email, bytes | bytearray):
                logger.warning("gmail_message_fetch_response_malformed")
                return None

            msg = email.message_from_bytes(bytes(raw_email))
            return self._parse_message(msg, uid=uid, uid_validity=uid_validity)
        except Exception as exc:
            # Any malformed-MIME/parse failure is a skip, never a crash of
            # the whole sync — one bad message must not stop the rest of
            # an otherwise-successful mailbox read.
            logger.warning("gmail_message_parse_failed error_type=%s", type(exc).__name__)
            return None

    def _parse_message(self, msg: Message, *, uid: int, uid_validity: int) -> ParsedGmailMessage:
        message_id = _clean_header(msg.get("Message-ID"))
        in_reply_to = _clean_header(msg.get("In-Reply-To"))
        references = _parse_references(msg.get("References"))
        from_name, from_address = _parse_single_address(msg.get("From"))
        to_addresses = _parse_address_list(msg.get("To"))
        cc_addresses = _parse_address_list(msg.get("Cc"))
        subject = _decode_mime_words(msg.get("Subject", ""))[:MAX_SUBJECT_LENGTH]
        sent_at = _parse_date(msg.get("Date"))
        body_plain, body_truncated, has_html, attachments = _extract_content(msg)
        direction = _direction(from_address, self.username)

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
            attachments=attachments,
        )
