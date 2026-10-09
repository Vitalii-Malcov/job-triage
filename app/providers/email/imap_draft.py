"""Stage 9D: APPEND-only Gmail Drafts provider over IMAP.

**GMAIL DRAFT CREATED != APPLICATION SENT.** The single mailbox mutation is
ONE `APPEND` per `create_draft` call into the exact, verified, FROZEN Drafts
target. Everything else is read-only: `LIST`, `EXAMINE`
(`select(readonly=True)`), `UID SEARCH`. There is no SMTP, Gmail API, OAuth
or HTTP here, and no STORE/EXPUNGE/COPY/MOVE/DELETE/CREATE/RENAME/FETCH/
CLOSE. The existing read-only `ImapClient`/`GmailImapProvider` is not
touched or reused for writing.

Kill switch: `drafts_enabled=False` (the default) refuses before any
connection. It is wired only from `TELEGRAM_GMAIL_DRAFT_ENABLED` and never
consults `OUTBOUND_SENDING_ENABLED`.

Session discipline (installed CPython 3.14 imaplib, verified in source):
`append()` returns the TAGGED response `(typ, [data])`, e.g.
`('OK', [b'[APPENDUID 7 42] (Success)'])`; a tagged BAD raises
`IMAP4.error`; a bracketed response code is ALSO cached as an untagged
entry that `response('APPENDUID')` returns and removes. imaplib's `_command`
clears only stale OK/NO/BAD entries, so a stale APPENDUID is drained
immediately before the APPEND. One dedicated client per call, exactly one
APPEND per session, no reconnect or retry around it. imaplib's `_command`
concatenates arguments without quoting or mailbox encoding, so the provider
always passes the exact pre-encoded, quoted wire string.

Result classification:

* anything before the APPEND is invoked (kill switch, MIME build, target
  shape, budget, DNS/TLS/connect/login, LIST verification) -> definite
  `DraftCreateDefiniteError`;
* a fully parsed tagged NO -> definite `DraftCreateRejectedError`;
* a fully parsed tagged OK -> `DraftCreateResult`, LATCHED immediately: a
  missing/malformed/conflicting APPENDUID only nulls the UID pair, and a
  logout/cleanup failure afterwards changes nothing;
* invoked without a definitive tagged result (timeout, reset, abort,
  `IMAP4.error` incl. imaplib's raised BAD path, parse error, deadline) ->
  `DraftCreateOutcomeUnknownError`. Exception text is never inspected.

Mailbox names: an explicit IMAP modified UTF-7 codec (RFC 3501 §5.1.3) and
a real LIST tokenizer (flags, quoted/NIL delimiter, atom/quoted/literal
name). `HEADER Message-ID` SEARCH is SUBSTRING containment (RFC 3501
§6.4.4), not exact equality: a single match on a fresh private 128-bit
marker is accepted as positive evidence for that attempt only.

Logs carry only fixed event names, fixed codes and exception TYPES --
never the address, mailbox, marker, UID/UIDVALIDITY, raw IMAP or `str(exc)`.
"""

import base64
import contextlib
import logging
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.providers.email import draft_base
from app.providers.email.base import normalize_account_key
from app.providers.email.draft_base import (
    MESSAGE_ID_PATTERN,
    UID_MAX,
    DraftAuthError,
    DraftBudgetExhaustedError,
    DraftConnectError,
    DraftCreateDefiniteError,
    DraftCreateOutcomeUnknownError,
    DraftCreateRejectedError,
    DraftCreateResult,
    DraftLookupError,
    DraftLookupResult,
    DraftMessage,
    DraftsDisabledError,
    DraftsMailboxInvalidError,
    DraftTarget,
    ReconcileTarget,
    build_draft_mime,
)
from app.providers.email.imap_deadline import DeadlineIMAP4SSL, ImapSessionDeadline

logger = logging.getLogger(__name__)

IMAP_OPERATION_TIMEOUT_SECONDS = 30.0
# Never START an APPEND with less remaining absolute budget than this.
APPEND_SAFETY_MARGIN_SECONDS = 5.0
DRAFT_FLAGS = r"(\Draft)"

MAILBOX_INVALID = "DRAFTS_MAILBOX_INVALID"
MAILBOX_UNENCODABLE = "DRAFTS_MAILBOX_UNENCODABLE"

_FORBIDDEN_FLAGS = frozenset(
    {
        "\\noselect",
        "\\nonexistent",
        "\\inbox",
        "\\sent",
        "\\all",
        "\\trash",
        "\\junk",
        "\\flagged",
        "\\important",
    }
)
_DRAFTS_FLAG = "\\drafts"


# --- modified UTF-7 + quoting ----------------------------------------------------


class MailboxNameError(ValueError):
    """A mailbox name that cannot be used exactly. `code` is a fixed
    outcome code; the name itself is never part of the message."""

    def __init__(self, code: str) -> None:
        super().__init__("Mailbox name is not usable")
        self.code = code


def encode_modified_utf7(name: str) -> str:
    """RFC 3501 §5.1.3: printable US-ASCII except `&` stands for itself,
    `&` is `&-`, every other run is `&` + modified base64 (`,` for `/`, no
    padding) of its UTF-16BE encoding + `-`. Deterministic."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            raw = "".join(run).encode("utf-16-be")
            encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            out.append(f"&{encoded}-")
            run.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    flush()
    return "".join(out)


_MUTF7_SEGMENT = re.compile(r"[A-Za-z0-9+,]+")


def decode_modified_utf7(raw: str) -> str:
    """Strict decode: printable ASCII only, well-formed segments, even
    UTF-16 length, valid surrogates, and CANONICAL (re-encoding must give
    back exactly `raw`), so two different wire names never decode to the
    same mailbox."""
    if any(not 0x20 <= ord(char) <= 0x7E for char in raw):
        raise MailboxNameError(MAILBOX_UNENCODABLE)
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "&":
            out.append(char)
            index += 1
            continue
        end = raw.find("-", index + 1)
        if end < 0:
            raise MailboxNameError(MAILBOX_UNENCODABLE)
        segment = raw[index + 1 : end]
        if not segment:
            out.append("&")
        else:
            if not _MUTF7_SEGMENT.fullmatch(segment):
                raise MailboxNameError(MAILBOX_UNENCODABLE)
            b64 = segment.replace(",", "/")
            try:
                data = base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
                if len(data) % 2:
                    raise ValueError("odd UTF-16 length")
                out.append(data.decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise MailboxNameError(MAILBOX_UNENCODABLE) from exc
        index = end + 1
    decoded = "".join(out)
    try:
        canonical = encode_modified_utf7(decoded)
    except UnicodeEncodeError as exc:
        raise MailboxNameError(MAILBOX_UNENCODABLE) from exc
    if canonical != raw:
        raise MailboxNameError(MAILBOX_UNENCODABLE)
    return decoded


def quote_imap_string(value: str) -> str:
    """An IMAP quoted string with `\\` and `"` escaped."""
    if any(char in value for char in ("\r", "\n", "\x00")):
        raise MailboxNameError(MAILBOX_INVALID)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate_drafts_mailbox_name(name: str) -> None:
    """Shape rules for the configured DECODED Drafts mailbox name."""
    if not isinstance(name, str) or not name or name != name.strip():
        raise MailboxNameError(MAILBOX_INVALID)
    if len(name) > draft_base.MAX_DRAFTS_MAILBOX_LENGTH:
        raise MailboxNameError(MAILBOX_INVALID)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise MailboxNameError(MAILBOX_INVALID)  # CR, LF, NUL, every C0 control, DEL
    if "*" in name or "%" in name:
        raise MailboxNameError(MAILBOX_INVALID)  # LIST wildcards
    if name.upper() == "INBOX":
        raise MailboxNameError(MAILBOX_INVALID)


def encode_mailbox_wire(name: str) -> str:
    """The exact quoted modified-UTF-7 wire form of the configured decoded
    name, or `MailboxNameError`. Bound-checked against
    `MAX_DRAFTS_MAILBOX_WIRE_LENGTH` (S9D-CONF-001) -- never truncated --
    and round-trip verified."""
    validate_drafts_mailbox_name(name)
    try:
        encoded = encode_modified_utf7(name)
    except UnicodeEncodeError as exc:  # lone surrogates
        raise MailboxNameError(MAILBOX_UNENCODABLE) from exc
    wire = quote_imap_string(encoded)
    if len(wire) > draft_base.MAX_DRAFTS_MAILBOX_WIRE_LENGTH:
        raise MailboxNameError(MAILBOX_UNENCODABLE)
    if decode_modified_utf7(_unquote(wire)) != name:
        raise MailboxNameError(MAILBOX_UNENCODABLE)
    return wire


def _unquote(wire: str) -> str:
    value, rest = _read_quoted(wire)
    if rest:
        raise MailboxNameError(MAILBOX_UNENCODABLE)
    return value


# --- LIST tokenizer ------------------------------------------------------------------


class ListParseError(ValueError):
    def __init__(self) -> None:
        super().__init__("LIST response could not be parsed")


@dataclass(frozen=True, repr=False)
class ListEntry:
    flags: frozenset[str]  # lower-cased
    delimiter: str | None
    raw_name: str  # modified UTF-7 as transmitted
    name: str  # decoded

    def __repr__(self) -> str:
        return "ListEntry(<redacted>)"


_FLAG = re.compile(r"\\?[A-Za-z0-9$&'+\-.:;<=>?@^_`|}~!#\[]+")
_ATOM = re.compile(r"[A-Za-z0-9$&'+\-.:;<=>?@^_`|}~!#\[\]/,]+")
_LITERAL_SUFFIX = re.compile(r"\{([0-9]{1,6})\}")


def _read_quoted(text: str) -> tuple[str, str]:
    """Parse one IMAP quoted string at the start of `text`; only `\\\\` and
    `\\"` escapes are legal."""
    if not text.startswith('"'):
        raise ListParseError()
    out: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text) or text[index + 1] not in ('"', "\\"):
                raise ListParseError()
            out.append(text[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(out), text[index + 1 :]
        if char in ("\r", "\n", "\x00"):
            raise ListParseError()
        out.append(char)
        index += 1
    raise ListParseError()


def _ascii(data: bytes) -> str:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ListParseError() from exc
    if any(char in text for char in ("\r", "\n", "\x00")):
        raise ListParseError()
    return text


def _parse_list_line(line: str, literal: str | None) -> ListEntry:
    if not line.startswith("("):
        raise ListParseError()
    close = line.find(")")
    if close < 0:
        raise ListParseError()
    flag_text = line[1:close]
    flags = flag_text.split(" ") if flag_text else []
    if any(not _FLAG.fullmatch(flag) for flag in flags):
        raise ListParseError()
    rest = line[close + 1 :]
    if not rest.startswith(" "):
        raise ListParseError()
    rest = rest[1:]
    if rest.startswith("NIL"):
        delimiter, rest = None, rest[3:]
    else:
        delimiter, rest = _read_quoted(rest)
        if len(delimiter) != 1:
            raise ListParseError()
    if not rest.startswith(" "):
        raise ListParseError()
    rest = rest[1:]
    if literal is not None:
        if not _LITERAL_SUFFIX.fullmatch(rest):
            raise ListParseError()
        raw_name = literal
    elif rest.startswith('"'):
        raw_name, tail = _read_quoted(rest)
        if tail:
            raise ListParseError()
    elif _ATOM.fullmatch(rest):
        raw_name = rest
    else:
        raise ListParseError()
    if not raw_name:
        raise ListParseError()
    try:
        name = decode_modified_utf7(raw_name)
    except MailboxNameError as exc:
        raise ListParseError() from exc
    return ListEntry(frozenset(flag.lower() for flag in flags), delimiter, raw_name, name)


def parse_list_response(data: list) -> list[ListEntry]:
    """Strictly parse imaplib's LIST data: bytes lines, or a
    `(prefix ending in {N}, literal)` tuple followed by its empty trailer.
    Anything unsupported or malformed raises `ListParseError` -- the
    target is then unverifiable and nothing is mutated."""
    if not isinstance(data, list):
        raise ListParseError()
    if data == [None]:
        return []
    entries: list[ListEntry] = []
    index = 0
    while index < len(data):
        item = data[index]
        if isinstance(item, tuple):
            if len(item) != 2 or not all(isinstance(part, bytes) for part in item):
                raise ListParseError()
            prefix, literal = _ascii(item[0]), _ascii(item[1])
            match = re.search(r"\{([0-9]{1,6})\}$", prefix)
            if match is None or int(match.group(1)) != len(item[1]):
                raise ListParseError()
            entries.append(_parse_list_line(prefix, literal))
            if index + 1 >= len(data) or data[index + 1] != b"":
                raise ListParseError()
            index += 2
            continue
        if not isinstance(item, bytes):
            raise ListParseError()
        entries.append(_parse_list_line(_ascii(item), None))
        index += 1
    return entries


def verify_drafts_target(entries: list[ListEntry], target: DraftTarget) -> None:
    """Exactly ONE LIST entry decodes to the frozen mailbox, carries
    `\\Drafts`, is selectable, is no other special-use folder, is not
    INBOX, and has exactly the frozen wire form."""
    matches = [entry for entry in entries if entry.name == target.mailbox]
    if len(matches) != 1:
        raise DraftsMailboxInvalidError("Drafts mailbox is missing or ambiguous")
    entry = matches[0]
    if _DRAFTS_FLAG not in entry.flags or entry.flags & _FORBIDDEN_FLAGS:
        raise DraftsMailboxInvalidError("Mailbox is not a selectable Drafts mailbox")
    if entry.name.upper() == "INBOX":
        raise DraftsMailboxInvalidError("Mailbox is not a Drafts mailbox")
    if quote_imap_string(entry.raw_name) != target.mailbox_wire:
        raise DraftsMailboxInvalidError("Mailbox wire identity differs")


# --- provider -------------------------------------------------------------------------


class ImapDraftClient(Protocol):
    """The narrow client surface this provider may use. No close, store,
    expunge, copy, move, delete, create, rename or fetch."""

    capabilities: tuple

    def login(self, user: str, password: str) -> tuple[str, list]: ...

    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list]: ...

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list]: ...

    def append(self, mailbox: str, flags: str, date_time, message: bytes) -> tuple[str, list]: ...

    def response(self, code: str) -> tuple[str, list]: ...

    def uid(self, command: str, *args: str) -> tuple[str, list]: ...

    def logout(self) -> tuple[str, list]: ...


ClientFactory = Callable[[ImapSessionDeadline, float], ImapDraftClient]

_UID_PAIR = re.compile(r"([1-9][0-9]{0,9}) ([1-9][0-9]{0,9})")
_APPENDUID_CODE = re.compile(rb"\[APPENDUID ([^\]]*)\]")
_UID = re.compile(r"[1-9][0-9]{0,9}")


def _parse_uid_pair(value) -> tuple[int, int] | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    match = _UID_PAIR.fullmatch(value)
    if match is None:
        return None
    uid_validity, uid = int(match.group(1)), int(match.group(2))
    if uid_validity > UID_MAX or uid > UID_MAX:
        return None
    return uid_validity, uid


def _parse_uid(value: str) -> int | None:
    if not _UID.fullmatch(value):
        return None
    number = int(value)
    return number if number <= UID_MAX else None


class GmailImapDraftProvider:
    """One dedicated IMAP session per call. See the module docstring."""

    def __init__(
        self,
        imap_host: str,
        imap_port: int,
        username: str,
        app_password: str,
        *,
        drafts_enabled: bool = False,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self._app_password = app_password
        self.drafts_enabled = bool(drafts_enabled)
        self._client_factory = client_factory or self._connect
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return f"GmailImapDraftProvider(drafts_enabled={self.drafts_enabled})"

    # --- connection -------------------------------------------------------------

    def _connect(self, deadline: ImapSessionDeadline, timeout: float) -> ImapDraftClient:
        client = DeadlineIMAP4SSL(
            self.imap_host,
            self.imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=timeout,
            deadline=deadline,
        )
        client.debug = 0
        return client

    def _remaining(self, deadline_at: datetime) -> float:
        return (deadline_at - self._clock()).total_seconds()

    def _open(self, deadline: ImapSessionDeadline, remaining: float) -> ImapDraftClient:
        try:
            client = self._client_factory(
                deadline, min(IMAP_OPERATION_TIMEOUT_SECONDS, max(remaining, 0.1))
            )
        except Exception as exc:
            logger.warning("gmail_draft_connect_failed error_type=%s", type(exc).__name__)
            raise DraftConnectError("Could not connect to the Gmail IMAP host") from None
        if getattr(client, "debug", 0):
            client.debug = 0
        try:
            typ, _ = client.login(self.username, self._app_password)
        except Exception as exc:
            logger.warning("gmail_draft_login_failed error_type=%s", type(exc).__name__)
            _logout(client)
            raise DraftAuthError("Gmail IMAP login was rejected") from None
        if typ != "OK":
            _logout(client)
            raise DraftAuthError("Gmail IMAP login was rejected")
        return client

    def _verify(self, client: ImapDraftClient, target: DraftTarget) -> None:
        typ, data = client.list('""', "*")
        if typ != "OK":
            raise DraftsMailboxInvalidError("LIST failed")
        try:
            entries = parse_list_response(data)
        except ListParseError:
            raise DraftsMailboxInvalidError("LIST response is not verifiable") from None
        verify_drafts_target(entries, target)

    def _check_target(self, target: DraftTarget) -> None:
        if normalize_account_key(self.username) != target.account_key:
            raise DraftsMailboxInvalidError("Frozen target belongs to another account")
        try:
            wire = encode_mailbox_wire(target.mailbox)
        except MailboxNameError:
            raise DraftsMailboxInvalidError("Frozen target is not encodable") from None
        if wire != target.mailbox_wire:
            raise DraftsMailboxInvalidError("Frozen target wire identity differs")

    # --- create ----------------------------------------------------------------

    def create_draft(
        self, message: DraftMessage, target: DraftTarget, deadline_at: datetime
    ) -> DraftCreateResult:
        """ONE APPEND of `message` into the frozen `target`, within the
        absolute `deadline_at`. See the module docstring for the result
        classification."""
        if not self.drafts_enabled:
            raise DraftsDisabledError("Gmail draft creation is disabled")
        mime = build_draft_mime(message, date=self._clock())
        self._check_target(target)
        remaining = self._remaining(deadline_at)
        if remaining <= APPEND_SAFETY_MARGIN_SECONDS:
            raise DraftBudgetExhaustedError("Attempt budget exhausted before APPEND")

        deadline = ImapSessionDeadline(remaining)
        with deadline:
            client = self._open(deadline, remaining)
            try:
                return self._append_once(client, mime, target, deadline_at)
            finally:
                _logout(client)

    def _append_once(
        self, client: ImapDraftClient, mime: bytes, target: DraftTarget, deadline_at: datetime
    ) -> DraftCreateResult:
        try:
            self._verify(client, target)
            if self._remaining(deadline_at) <= APPEND_SAFETY_MARGIN_SECONDS:
                raise DraftBudgetExhaustedError("Attempt budget exhausted before APPEND")
            client.response("APPENDUID")  # drain a stale code; never attributed
        except DraftCreateDefiniteError:
            raise
        except Exception as exc:
            logger.warning("gmail_draft_pre_append_failed error_type=%s", type(exc).__name__)
            raise DraftConnectError("IMAP failure before APPEND") from None

        # --- the single APPEND: from here on, only a tagged result is definite
        try:
            typ, data = client.append(target.mailbox_wire, DRAFT_FLAGS, None, mime)
        except Exception as exc:
            logger.warning("gmail_draft_append_unknown error_type=%s", type(exc).__name__)
            raise DraftCreateOutcomeUnknownError("APPEND outcome unknown") from None
        if typ == "NO":
            raise DraftCreateRejectedError("APPEND was rejected")
        if typ != "OK" or not isinstance(data, list):
            raise DraftCreateOutcomeUnknownError("APPEND outcome unknown")

        # --- latched OK: nothing below may turn it into a failure
        pair = None
        try:
            pair = _appenduid(client, data)
        except Exception as exc:
            logger.info("gmail_draft_appenduid_unavailable error_type=%s", type(exc).__name__)
        if pair is None:
            return DraftCreateResult(None, None)
        return DraftCreateResult(*pair)

    # --- read-only lookup ----------------------------------------------------------

    def find_by_message_id(
        self, reconcile_target: ReconcileTarget, deadline_at: datetime
    ) -> DraftLookupResult:
        """LIST-verify the ORIGINAL frozen target, EXAMINE it, read
        UIDVALIDITY and `UID SEARCH HEADER Message-ID "<marker>"`, all
        strictly parsed. Any failure is `DraftLookupError` -- never an
        empty result."""
        marker = reconcile_target.message_id
        target = reconcile_target.target
        if not MESSAGE_ID_PATTERN.fullmatch(marker):
            raise DraftLookupError("Marker is malformed")
        try:
            self._check_target(target)
        except DraftsMailboxInvalidError:
            raise DraftLookupError("Frozen target is unusable") from None
        remaining = self._remaining(deadline_at)
        if remaining <= 0:
            raise DraftLookupError("Lookup budget exhausted")
        deadline = ImapSessionDeadline(remaining)
        with deadline:
            try:
                client = self._open(deadline, remaining)
            except DraftCreateDefiniteError:
                raise DraftLookupError("Could not open the lookup session") from None
            try:
                return self._lookup(client, target, marker)
            except DraftLookupError:
                raise
            except Exception as exc:
                logger.warning("gmail_draft_lookup_failed error_type=%s", type(exc).__name__)
                raise DraftLookupError("Lookup failed") from None
            finally:
                _logout(client)

    def _lookup(self, client: ImapDraftClient, target: DraftTarget, marker: str):
        try:
            self._verify(client, target)
        except DraftsMailboxInvalidError:
            raise DraftLookupError("Original Drafts target is not verifiable") from None
        typ, _ = client.select(target.mailbox_wire, readonly=True)
        if typ != "OK":
            raise DraftLookupError("EXAMINE failed")
        _, values = client.response("UIDVALIDITY")
        present = [value for value in (values or []) if value is not None]
        if len(present) != 1:
            raise DraftLookupError("UIDVALIDITY missing or ambiguous")
        raw = present[0].decode("ascii") if isinstance(present[0], bytes) else present[0]
        uid_validity = _parse_uid(raw) if isinstance(raw, str) else None
        if uid_validity is None:
            raise DraftLookupError("UIDVALIDITY malformed")
        typ, data = client.uid("SEARCH", "HEADER", "Message-ID", quote_imap_string(marker))
        if typ != "OK" or not isinstance(data, list) or len(data) != 1:
            raise DraftLookupError("SEARCH failed or partial")
        line = data[0]
        if not isinstance(line, bytes):
            raise DraftLookupError("SEARCH response missing")
        text = line.decode("ascii").strip(" ")
        uids: list[int] = []
        for token in text.split(" ") if text else []:
            uid = _parse_uid(token)
            if uid is None or uid in uids:
                raise DraftLookupError("SEARCH response malformed")
            uids.append(uid)
        return DraftLookupResult(uid_validity, tuple(uids))


def _appenduid(client: ImapDraftClient, tagged_data: list) -> tuple[int, int] | None:
    """Exactly one well-formed `uidvalidity uid` pair, consistent with any
    APPENDUID code in the tagged data -- otherwise None (never an error)."""
    _, values = client.response("APPENDUID")
    present = [value for value in (values or []) if value is not None]
    if len(present) != 1:
        return None
    pair = _parse_uid_pair(present[0])
    if pair is None:
        return None
    for item in tagged_data:
        if isinstance(item, bytes):
            for code in _APPENDUID_CODE.findall(item):
                if _parse_uid_pair(code) != pair:
                    return None
    return pair


def _logout(client: ImapDraftClient) -> None:
    with contextlib.suppress(Exception):
        client.logout()
