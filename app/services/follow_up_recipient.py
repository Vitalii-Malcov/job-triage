"""S7E-008 (Codex remediation): canonical, validated Stage 7E follow-up
recipient derivation.

**Why this exists as its own module.** Both proposal-build time
(`app.services.follow_up`, which derives+validates the recipient once and
persists it on `FollowUpProposalRecord.recipient`) and send-time
revalidation (`app.services.follow_up_send`, which re-derives it from a
freshly-loaded anchor message and requires an EXACT match against the
pinned, human-approved value) must apply identical rules — duplicating the
validation logic between those two call sites would risk exactly the kind
of "one call site was updated, the other wasn't" drift this project's
established fingerprint/pin conventions exist to prevent.

**Trust boundary.** The only input this module ever reads is the anchor
OUTBOUND message's own `to_addresses` — already-parsed, already-persisted
Stage 7A structural metadata (never text re-derived from `body_plain`).
Combined with S7E-001 (a message is only ever OUTBOUND if it was fetched
from the account's real, authenticated Sent-mail folder — never from a
spoofable `From` header), this means `to_addresses` here reflects what the
candidate's own mail client actually addressed a real sent message to.

**Fails closed, never guesses.** Empty, more than one DISTINCT address,
malformed, containing CR/LF (header-injection defense in depth — a
well-formed address should never contain one, but this is checked
explicitly rather than assumed), or equal to the configured account's own
address (an external follow-up must never be "sent" to ourselves) are all
rejected with a specific reason. There is no fallback address and no
default — a follow-up with no safely-derivable single recipient is simply
not proposable.
"""

import re
from collections.abc import Sequence

from app.providers.email.base import MAX_ADDRESS_LENGTH

# Deliberately simple (RFC 5322 has no practical validating regex) — this
# is a sanity/shape check against obviously-malformed input, not a claim of
# full RFC 5322 compliance. Mirrors this project's general preference for
# a bounded, explicit check over an attempted "complete" validator.
_EMAIL_SHAPE_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_CONTROL_CHARS = ("\r", "\n", "\x00")


class FollowUpRecipientInvalidError(Exception):
    """No single, safe, unambiguous external recipient could be derived —
    see module docstring for the exact rejected cases. Carries a specific,
    non-sensitive reason (never echoes the rejected address itself, which
    could otherwise leak a malformed/injected value into an error message
    or log line — mirrors this project's GMAIL-003 convention).
    """


def derive_canonical_recipient(to_addresses: Sequence[str], *, account_key: str) -> str:
    """Returns the single canonical recipient, or raises
    `FollowUpRecipientInvalidError`. `account_key` is the normalized
    (stripped + casefolded) configured mailbox account address — see
    `app.providers.email.base.normalize_account_key` — compared against
    each candidate the same way.
    """
    distinct: dict[str, str] = {}
    for raw in to_addresses:
        if not raw:
            continue
        candidate = raw.strip()
        if not candidate:
            continue
        if any(ch in candidate for ch in _CONTROL_CHARS):
            raise FollowUpRecipientInvalidError(
                "recipient address contains CR/LF or NUL control characters"
            )
        distinct.setdefault(candidate.casefold(), candidate)

    if not distinct:
        raise FollowUpRecipientInvalidError("no recipient address on record")
    if len(distinct) > 1:
        raise FollowUpRecipientInvalidError(
            f"ambiguous recipient: {len(distinct)} distinct addresses on record"
        )

    (casefolded, candidate) = next(iter(distinct.items()))

    if len(candidate) > MAX_ADDRESS_LENGTH:
        raise FollowUpRecipientInvalidError("recipient address exceeds maximum length")
    if not _EMAIL_SHAPE_RE.match(candidate):
        raise FollowUpRecipientInvalidError("recipient address is malformed")
    if casefolded == account_key:
        raise FollowUpRecipientInvalidError(
            "recipient address is the configured account's own address"
        )

    return candidate
