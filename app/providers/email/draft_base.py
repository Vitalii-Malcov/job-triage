"""Stage 9D Gmail DRAFT provider -- shared types, errors and the MIME
builder (see app/providers/email/imap_draft.py for the IMAP implementation).

**GMAIL DRAFT CREATED != APPLICATION SENT.** The provider's only mailbox
mutation is ONE IMAP `APPEND` of a new message into the verified Drafts
mailbox. Its Protocol has no send, delete, move, store, expunge, copy,
create, rename or body-fetch operation, and it never touches SMTP, HTTP, the
Gmail API or OAuth. The read-only inbox reader (`base.py`/`imap.py`) is a
separate code path whose read-only guarantee is unchanged.

**No recipient.** `DraftMessage` structurally has NO recipient field; the
serialized MIME carries only From/Subject/Date/Message-ID plus the
library-generated MIME headers, and `build_draft_mime` asserts the final
bytes contain no To/Cc/Bcc/Reply-To/In-Reply-To/References. No attachment,
no HTML, a single text/plain UTF-8 part.

**Privacy.** Every error message is a fixed string; DTO reprs never show the
address, subject, body, Message-ID marker, mailbox, UID or UIDVALIDITY.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.header import Header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime
from typing import Protocol

# `<s9d.{32 lowercase hex}@ai-job-search.invalid>`: 128 random bits per
# attempt, pure ASCII, no ids/PII/account/company.
MESSAGE_ID_PATTERN = re.compile(r"<s9d\.[0-9a-f]{32}@ai-job-search\.invalid>")

# Decoded configured Drafts mailbox name bound (code points).
MAX_DRAFTS_MAILBOX_LENGTH = 200
# S9D-CONF-001: the quoted modified-UTF-7 wire form is bound-checked against
# the ledger column (String(2048)) BEFORE any claim -- never truncated.
MAX_DRAFTS_MAILBOX_WIRE_LENGTH = 2048

UID_MAX = 4294967295

ALLOWED_HEADERS = frozenset(
    {
        "from",
        "subject",
        "date",
        "message-id",
        "mime-version",
        "content-type",
        "content-transfer-encoding",
    }
)
FORBIDDEN_HEADERS = ("To", "Cc", "Bcc", "Reply-To", "In-Reply-To", "References")
_HEADER_UNSAFE = ("\r", "\n", "\x00")

# S9D-ASTRA-001: the header registry DECODES RFC 2047 encoded-word syntax
# ("=?charset?q|b?...?=") in an unstructured Subject and the folder re-emits
# that ASCII text verbatim, so a literal approved "=?utf-8?q?X?=" would
# arrive as "X" (or as a decoded CR/LF/NUL). Such a subject is therefore
# encoded WHOLE by the stdlib `email.header` encoder; the resulting
# encoded-words decode back to exactly the approved string.
_ENCODED_WORD_START = "=?"
# Serialize stored header values as-is: refolding a pre-encoded Subject
# would re-parse (and so re-decode) it. Registry headers still fold normally.
_SERIALIZE_POLICY = policy.SMTP.clone(refold_source="none")
# S9D-CODEX-ASTRAFIX-001 -- RFC 2047 section 2: an encoded word is at most
# 75 characters and a line containing one at most 76. The stdlib default
# (78) can emit a 76-character word, so the encoder is asked for 76 and its
# output is checked word by word; anything else is refused pre-APPEND.
_ENCODED_WORD_MAX = 75
_ENCODED_LINE_MAX = 76
# Encoded-text is printable ASCII without "?" or space.
_ENCODED_WORD = re.compile(r"=\?utf-8\?[qb]\?[!->@-~]*\?=")


@dataclass(frozen=True, repr=False)
class DraftMessage:
    """The complete logical draft. There is deliberately no recipient
    field: the human enters the recipient in Gmail."""

    from_address: str
    subject: str
    body_lf: str
    message_id: str

    def __repr__(self) -> str:
        return "DraftMessage(<redacted>)"


@dataclass(frozen=True, repr=False)
class DraftTarget:
    """The FROZEN Drafts target of one attempt: the account it belongs to,
    the decoded mailbox name and its exact quoted modified-UTF-7 wire
    form. Used byte-identically for LIST matching, APPEND and EXAMINE."""

    account_key: str
    mailbox: str
    mailbox_wire: str

    def __repr__(self) -> str:
        return "DraftTarget(<redacted>)"


@dataclass(frozen=True, repr=False)
class ReconcileTarget:
    """The ORIGINAL frozen target plus the attempt's marker. Never built
    from current configuration."""

    target: DraftTarget
    message_id: str

    def __repr__(self) -> str:
        return "ReconcileTarget(<redacted>)"


@dataclass(frozen=True, repr=False)
class DraftCreateResult:
    """A fully parsed tagged APPEND OK. The UID pair is optional evidence
    (both None when APPENDUID was absent or unusable) -- never a reason to
    doubt the OK itself."""

    uid_validity: int | None
    uid: int | None

    def __repr__(self) -> str:
        return f"DraftCreateResult(uid_known={self.uid is not None})"


@dataclass(frozen=True, repr=False)
class DraftLookupResult:
    """A strictly parsed read-only Message-ID SEARCH in the original
    target. Only `len(uids) == 1` is positive evidence."""

    uid_validity: int
    uids: tuple[int, ...]

    def __repr__(self) -> str:
        return f"DraftLookupResult(matches={len(self.uids)})"


class DraftProviderError(Exception):
    """Base error. Messages are fixed strings; callers log only the type
    and the fixed `code`, never `str(exc)`."""

    code = "PROVIDER_ERROR"


class DraftCreateDefiniteError(DraftProviderError):
    """Definite proof that THIS create call stored nothing: the APPEND was
    never invoked, or it returned a fully parsed tagged NO."""

    code = "PRE_APPEND_FAILURE"


class DraftsDisabledError(DraftCreateDefiniteError):
    code = "GMAIL_DRAFT_DISABLED"


class DraftMessageInvalidError(DraftCreateDefiniteError):
    code = "MESSAGE_INVALID"


class DraftAuthError(DraftCreateDefiniteError):
    code = "AUTH_FAILED"


class DraftConnectError(DraftCreateDefiniteError):
    code = "CONNECT_FAILED"


class DraftsMailboxInvalidError(DraftCreateDefiniteError):
    code = "DRAFTS_MAILBOX_INVALID"


class DraftBudgetExhaustedError(DraftCreateDefiniteError):
    code = "BUDGET_EXHAUSTED"


class DraftCreateRejectedError(DraftCreateDefiniteError):
    code = "REJECTED"


class DraftCreateOutcomeUnknownError(DraftProviderError):
    """The APPEND was invoked without a definitive tagged result: a draft
    may or may not have been stored."""

    code = "OUTCOME_UNKNOWN"


class DraftLookupError(DraftProviderError):
    """The read-only lookup could not produce a strictly parsed result.
    Never evidence of non-creation."""

    code = "LOOKUP_FAILED"


class DraftProvider(Protocol):
    def create_draft(
        self, message: DraftMessage, target: DraftTarget, deadline_at: datetime
    ) -> DraftCreateResult: ...

    def find_by_message_id(
        self, reconcile_target: ReconcileTarget, deadline_at: datetime
    ) -> DraftLookupResult: ...


def build_draft_mime(message: DraftMessage, *, date: datetime) -> bytes:
    """Serialize the draft as a single text/plain UTF-8 part with a fixed
    header allowlist, then re-parse the final bytes and assert the
    contract. Any problem is a `DraftMessageInvalidError` (a definite
    pre-APPEND failure)."""
    try:
        return _build(message, date)
    except DraftMessageInvalidError:
        raise
    except Exception as exc:
        raise DraftMessageInvalidError("Draft message could not be built") from exc


@dataclass(frozen=True)
class _EncodedSubject:
    """A Subject already encoded (folded, pure ASCII) by `email.header`."""

    value: str


def _subject_header_value(subject: str) -> "str | _EncodedSubject":
    """The Subject as given, or -- when it contains encoded-word syntax --
    wholly encoded so the parser cannot reinterpret any of it."""
    if _ENCODED_WORD_START not in subject:
        return subject
    encoded = Header(subject, "utf-8", maxlinelen=_ENCODED_LINE_MAX, header_name="Subject").encode()
    if not _is_rfc2047_compliant(encoded):
        raise DraftMessageInvalidError("Draft header is unsafe")
    return _EncodedSubject(encoded)


def _is_rfc2047_compliant(encoded: str) -> bool:
    """True when the folded value is ASCII, every token on every line is
    one encoded word of at most 75 characters, every line (the first with
    its "Subject: " prefix) is at most 76 characters, and every
    continuation line starts with one space."""
    if not encoded.isascii() or any(char in encoded for char in ("\r", "\x00")):
        return False
    for index, line in enumerate(encoded.split("\n")):
        if index and not line.startswith(" "):
            return False
        if len(line if index else f"Subject: {line}") > _ENCODED_LINE_MAX:
            return False
        tokens = line.split(" ")
        if any(token and len(token) > _ENCODED_WORD_MAX for token in tokens):
            return False
        words = [token for token in tokens if token]
        if not words or not all(_ENCODED_WORD.fullmatch(word) for word in words):
            return False
    return True


def _build(message: DraftMessage, date: datetime) -> bytes:
    if not MESSAGE_ID_PATTERN.fullmatch(message.message_id):
        raise DraftMessageInvalidError("Draft marker is malformed")
    for value in (message.from_address, message.subject):
        if not value or any(char in value for char in _HEADER_UNSAFE):
            raise DraftMessageInvalidError("Draft header is unsafe")
    if "\x00" in message.body_lf or "\r" in message.body_lf:
        raise DraftMessageInvalidError("Draft body is unsafe")

    mime = EmailMessage(policy=policy.SMTP)
    mime["From"] = message.from_address
    subject = _subject_header_value(message.subject)
    if isinstance(subject, _EncodedSubject):
        mime.set_raw("Subject", subject.value)
    else:
        mime["Subject"] = subject
    mime["Date"] = format_datetime(date)
    mime["Message-ID"] = message.message_id
    mime.set_content(message.body_lf, subtype="plain", charset="utf-8")
    data = mime.as_bytes(policy=_SERIALIZE_POLICY)
    if isinstance(subject, _EncodedSubject):
        wire = "\r\nSubject: " + subject.value.replace("\n", "\r\n") + "\r\n"
        if wire.encode("ascii") not in data:
            raise DraftMessageInvalidError("Draft subject was not serialized as validated")

    parsed = BytesParser(policy=policy.SMTP).parsebytes(data)
    names = {name.lower() for name in parsed.keys()}
    if not names <= ALLOWED_HEADERS or len(parsed.keys()) != len(names):
        raise DraftMessageInvalidError("Draft carries an unexpected header")
    if any(parsed.get(name) is not None for name in FORBIDDEN_HEADERS):
        raise DraftMessageInvalidError("Draft carries a recipient header")
    subjects = parsed.get_all("Subject") or []
    decoded = str(subjects[0]) if len(subjects) == 1 else ""
    if (
        decoded != message.subject
        or not decoded.strip()
        or any(char in decoded for char in _HEADER_UNSAFE)
    ):
        raise DraftMessageInvalidError("Draft subject did not round-trip")
    if parsed.is_multipart() or parsed.get_content_type() != "text/plain":
        raise DraftMessageInvalidError("Draft is not a single text/plain part")
    if (parsed.get_content_charset() or "").lower() != "utf-8":
        raise DraftMessageInvalidError("Draft is not UTF-8")
    if str(parsed["Message-ID"]).strip() != message.message_id:
        raise DraftMessageInvalidError("Draft marker did not round-trip")
    if parsed.get_content().replace("\r\n", "\n") != message.body_lf:
        raise DraftMessageInvalidError("Draft body did not round-trip")
    return data
