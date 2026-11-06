"""Review finding 1.3: the one place a response-draft reply recipient is derived.

The recipient of a reply is the inbound message's stored `from_address`,
which comes from a header anyone can forge (no SPF/DKIM verification is done
here). Pinning it on the approval cannot prove the sender is genuine; it
guarantees that the address captured when the human approved is the only one
a send may use, and that a later change of the stored header refuses the
send instead of redirecting it.

Fails closed and never echoes the rejected address.
"""

import re
from email.utils import getaddresses

from app.providers.email.base import MAX_ADDRESS_LENGTH, normalize_account_key
from app.services.follow_up_recipient import _CONTROL_CHARS, _EMAIL_SHAPE_RE

# FLOW-04: an automated sender's address is not a person: an interview confirmation sent to
# `noreply@` is a black hole while the candidate believes the interview is confirmed. A reply
# to such a sender is refused (fail closed) instead of being approved and sent; Reply-To is
# not stored, so the operator answers through the sender's own channel or portal.
_NO_REPLY_LOCAL_PART = re.compile(
    r"(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_]?daemon|postmaster|bounces?)"
    r"(?:[-_+.].*)?",
    re.IGNORECASE,
)


class ReplyRecipientInvalidError(Exception):
    """No single, safe reply recipient can be derived (reason is non-sensitive)."""


def derive_reply_recipient(from_address: str | None, *, account_key: str) -> str:
    if not from_address:
        raise ReplyRecipientInvalidError("no from_address on record")
    # Controls are checked on the RAW value: stripping first would hide a
    # trailing CR/LF.
    if any(ch in from_address for ch in _CONTROL_CHARS):
        raise ReplyRecipientInvalidError("address contains control characters")
    candidate = from_address.strip()
    if not candidate or len(candidate) > MAX_ADDRESS_LENGTH:
        raise ReplyRecipientInvalidError("address is empty or too long")
    parsed = [addr for _name, addr in getaddresses([candidate]) if addr]
    # Exactly one mailbox and it must be the whole value (no list, no
    # display-name wrapper, no group syntax).
    if len(parsed) != 1 or parsed[0] != candidate or "," in candidate or ";" in candidate:
        raise ReplyRecipientInvalidError("address is not a single bare mailbox")
    if not _EMAIL_SHAPE_RE.match(candidate):
        raise ReplyRecipientInvalidError("address is malformed")
    if candidate.casefold() == normalize_account_key(account_key):
        raise ReplyRecipientInvalidError("address is the configured mailbox itself")
    if _NO_REPLY_LOCAL_PART.fullmatch(candidate.rsplit("@", 1)[0]):
        raise ReplyRecipientInvalidError("address is a no-reply mailbox")
    return candidate
