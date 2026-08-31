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
GmailThreadRecord. Full server-side Gmail threading is left for a later
stage if ever needed.
"""

import asyncio
import email
import imaplib
import logging
import re
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
    MAX_REFERENCES,
    MAX_SUBJECT_LENGTH,
    Direction,
    GmailAuthError,
    GmailConnectionError,
    GmailFetchResult,
    ImapClient,
    ParsedAttachment,
    ParsedGmailMessage,
)

logger = logging.getLogger(__name__)

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)")


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
    # otherwise inspected/opened.
    payload = part.get_payload(decode=True)
    size = len(payload) if payload is not None else None
    return ParsedAttachment(filename=decoded_filename or None, content_type=content_type, size=size)


def _is_attachment_part(part: Message) -> bool:
    disposition = str(part.get("Content-Disposition") or "").lower()
    if "attachment" in disposition:
        return True
    return bool(part.get_filename()) and "inline" not in disposition


def _extract_content(
    msg: Message,
) -> tuple[str, bool, bool, tuple[ParsedAttachment, ...]]:
    """Extract plaintext body + has_html flag + bounded attachment metadata.

    Plaintext is preferred over HTML (never rendered/executed/fetched —
    see this module's docstring). If only an HTML part exists, body_plain
    stays "" and has_html is True; no HTML-to-text conversion is
    attempted in Stage 7A.
    """
    body_plain = ""
    has_html = False
    attachments: list[ParsedAttachment] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            if _is_attachment_part(part):
                if len(attachments) < MAX_ATTACHMENTS:
                    attachments.append(_attachment_metadata(part, part.get_filename()))
                continue
            if content_type == "text/plain" and not body_plain:
                body_plain = _decode_part(part)
            elif content_type == "text/html":
                has_html = True
    else:
        content_type = msg.get_content_type()
        if content_type == "text/plain":
            body_plain = _decode_part(msg)
        elif content_type == "text/html":
            has_html = True

    truncated = len(body_plain) > MAX_BODY_LENGTH
    if truncated:
        body_plain = body_plain[:MAX_BODY_LENGTH]
    return body_plain, truncated, has_html, tuple(attachments)


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
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self.app_password = app_password
        self.mailbox = mailbox
        self.lookback_days = lookback_days
        # Injected only by tests, to avoid a real IMAP connection —
        # mirrors app.collectors.xing_email.XingEmailCollector.
        self._injected_client = imap_client

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
                raise GmailConnectionError(f"IMAP SELECT failed: {typ}")

            uid_validity = self._read_uid_validity(client)

            criteria = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
            typ, data = client.uid("search", None, criteria)
            if typ != "OK":
                raise GmailConnectionError(f"IMAP UID SEARCH failed: {typ}")

            uids = data[0].split() if data and data[0] else []
            messages: list[ParsedGmailMessage] = []
            skipped_count = 0
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
            raise GmailConnectionError(f"IMAP STATUS failed: {typ}")
        for line in data:
            if not isinstance(line, bytes):
                continue
            match = _UIDVALIDITY_RE.search(line)
            if match:
                return int(match.group(1))
        raise GmailConnectionError("Could not determine mailbox UIDVALIDITY")

    def _connect(self) -> imaplib.IMAP4_SSL:
        try:
            client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        except OSError as exc:
            raise GmailConnectionError(
                f"Could not connect to {self.imap_host}:{self.imap_port}: {exc}"
            ) from exc

        try:
            client.login(self.username, self.app_password)
        except imaplib.IMAP4.error as exc:
            raise GmailAuthError(f"Gmail mailbox IMAP login rejected: {exc}") from exc
        return client

    def _disconnect(self, client: ImapClient) -> None:
        try:
            client.close()
        except Exception:
            logger.warning("gmail_imap_close_failed", exc_info=True)
        try:
            client.logout()
        except Exception:
            logger.warning("gmail_imap_logout_failed", exc_info=True)

    def _fetch_one(
        self, client: ImapClient, uid_bytes: bytes, uid_validity: int
    ) -> ParsedGmailMessage | None:
        try:
            typ, msg_data = client.uid("fetch", uid_bytes, "(RFC822)")
        except Exception:
            logger.warning("gmail_message_fetch_error", exc_info=True)
            return None
        if typ != "OK" or not msg_data or msg_data[0] is None:
            logger.warning("gmail_message_fetch_failed")
            return None

        raw_email = msg_data[0][1]
        try:
            uid = int(uid_bytes)
            msg = email.message_from_bytes(raw_email)
            return self._parse_message(msg, uid=uid, uid_validity=uid_validity)
        except Exception:
            # Any malformed-MIME/parse failure is a skip, never a crash of
            # the whole sync — one bad message must not stop the rest of
            # an otherwise-successful mailbox read.
            logger.warning("gmail_message_parse_failed", exc_info=True)
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
