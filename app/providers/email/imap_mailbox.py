"""IMAP mailbox-name wire encoding shared by the read-only inbox provider
(`imap.py`) and the Stage 9D Drafts provider (`imap_draft.py`).

imaplib sends every command argument as-is, ASCII-encoded: it neither
quotes nor applies the RFC 3501 §5.1.3 modified UTF-7 mailbox encoding,
so a non-ASCII name (e.g. a localized "[Gmail]/Отправленные") fails with
`UnicodeEncodeError` before reaching the server. Pure functions only --
no I/O, no IMAP client.
"""

import base64
import re

MAILBOX_INVALID = "DRAFTS_MAILBOX_INVALID"
MAILBOX_UNENCODABLE = "DRAFTS_MAILBOX_UNENCODABLE"


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


# RFC 3501 atom-specials that an unquoted astring may not contain, besides
# SP and CTL (`]` is allowed: ASTRING-CHAR includes resp-specials).
_ASTRING_SPECIALS = frozenset('(){%*"\\')


def mailbox_command_arg(name: str) -> str:
    """A syntactically valid IMAP mailbox argument for a read-only
    SELECT/EXAMINE/STATUS, or `MailboxNameError`.

    A name that already is canonical modified UTF-7 (it survives the
    strict decode -> encode round trip, e.g. `[Gmail]/&BB4E...-` or
    `Jobs &- Co`) is used as-is, never encoded twice; any other name is
    readable and gets encoded (`Jobs & Co` -> `Jobs &- Co`). The result
    is sent bare when it is a valid astring (e.g. `INBOX`) and
    quoted/escaped otherwise (e.g. `"[Gmail]/Sent Mail"`)."""
    wire = _canonical_wire(name)
    if wire and " " not in wire and not _ASTRING_SPECIALS.intersection(wire):
        return wire
    return quote_imap_string(wire)


def _canonical_wire(name: str) -> str:
    """The unquoted canonical modified-UTF-7 form of `name` (see
    `mailbox_command_arg`): the one spelling of the server mailbox it
    selects."""
    try:
        decode_modified_utf7(name)
        return name
    except MailboxNameError:
        try:
            return encode_modified_utf7(name)
        except UnicodeEncodeError as exc:  # lone surrogates
            raise MailboxNameError(MAILBOX_UNENCODABLE) from exc


def mailbox_identity_aliases(name: str) -> tuple[str, ...]:
    """Every persisted spelling of the SAME server mailbox `name` selects,
    `name` first: its canonical modified-UTF-7 form and that form's
    readable decoding (e.g. `[Gmail]/Отправленные` and
    `[Gmail]/&BB4E...-`). A spelling is included only if it selects
    exactly the same wire mailbox, so different mailboxes never share an
    alias. An unencodable name is only ever itself."""
    try:
        wire = _canonical_wire(name)
    except MailboxNameError:
        return (name,)
    candidates = dict.fromkeys((name, wire, decode_modified_utf7(wire)))
    return tuple(alias for alias in candidates if alias == name or _canonical_wire(alias) == wire)


def canonical_mailbox_identity(name: str) -> str:
    """ASTRA-GMAIL-001-R1: the ONE spelling persisted for the server
    mailbox `name` selects -- its canonical modified-UTF-7 wire form
    (`[Gmail]/Отправленные` and `[Gmail]/&BB4E...-` both give
    `[Gmail]/&BB4E...-`). Injective over wire mailboxes, so equal results
    mean the same mailbox and different mailboxes never collide; writing
    only this spelling lets the database's plain-string UNIQUE key
    enforce dedup across every alias. An unencodable name is itself."""
    try:
        return _canonical_wire(name)
    except MailboxNameError:
        return name


def same_mailbox(a: str, b: str) -> bool:
    """True when `a` and `b` are spellings of the same server mailbox."""
    return b in mailbox_identity_aliases(a)
