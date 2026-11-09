"""FLOW-15: what kind of outcome each operator-facing Telegram notice reports.

A notice for a refusal that retrying cannot change must not tell the operator to try again.
The kind of every known notice code is declared here; `tests/test_audit_telegram_notice_kinds.py`
fails if a code is added without a kind or if a deterministic notice carries a retry hint.
Pure data: nothing here sends, reads or writes anything.
"""

import re
from enum import StrEnum


class NoticeKind(StrEnum):
    OK = "ok"  # the action happened
    TRANSIENT = "transient"  # temporary failure or busy; trying again can work
    INVALID_DATA = "invalid_data"  # unknown/expired/missing input or profile data
    NOT_AUTHORIZED = "not_authorized"  # the caller may not do this
    ALREADY_PROCESSED = "already_processed"  # done before, replaced or superseded
    FORBIDDEN = "forbidden"  # the current state or policy does not allow the action


# kinds whose notices must not suggest "try again"
DETERMINISTIC_KINDS = frozenset(
    {
        NoticeKind.INVALID_DATA,
        NoticeKind.NOT_AUTHORIZED,
        NoticeKind.ALREADY_PROCESSED,
        NoticeKind.FORBIDDEN,
    }
)

K = NoticeKind

# app.services.telegram_bewerbung.NOTICES
BEWERBUNG_KINDS: dict[str, NoticeKind] = {
    "UNKNOWN_REVIEW": K.INVALID_DATA,
    "REVIEW_NOT_ELIGIBLE": K.FORBIDDEN,
    "JOB_MISSING": K.INVALID_DATA,
    "JOB_NOT_ELIGIBLE": K.FORBIDDEN,
    "NO_PROFILE": K.INVALID_DATA,
    "PROFILE_NOT_READY": K.INVALID_DATA,
    "NO_RELEVANT_EVIDENCE": K.INVALID_DATA,
    "PROFILE_CHANGED": K.TRANSIENT,
    "JOB_CHANGED": K.TRANSIENT,
    "BUSY": K.TRANSIENT,
    "PREVIEW_BUSY": K.TRANSIENT,
    "ATTEMPTS_EXHAUSTED": K.FORBIDDEN,
    "PREPARATION_FAILED": K.TRANSIENT,
    "PREPARATION_LOST": K.ALREADY_PROCESSED,
    "ALREADY_SHOWN": K.ALREADY_PROCESSED,
    "PREVIEW_UNCERTAIN": K.TRANSIENT,
    "PREVIEW_FAILED": K.TRANSIENT,
    "PREVIEW_EXPIRED": K.ALREADY_PROCESSED,
    "PACKAGE_UNAVAILABLE": K.INVALID_DATA,
    "INVALID_PAGE": K.INVALID_DATA,
}

# app.services.telegram_bewerbung_approval.NOTICES
APPROVAL_KINDS: dict[str, NoticeKind] = {
    "UNKNOWN_CAPABILITY": K.INVALID_DATA,
    "PACKAGE_EXPIRED": K.ALREADY_PROCESSED,
    "PACKAGE_REPLACED": K.ALREADY_PROCESSED,
    "PACKAGE_STALE": K.TRANSIENT,
    "PACKAGE_UNAVAILABLE": K.INVALID_DATA,
    "JOB_NOT_ELIGIBLE": K.FORBIDDEN,
    "NO_PROFILE": K.INVALID_DATA,
    "REVIEW_TOO_LARGE": K.FORBIDDEN,
    "REVIEW_INCOMPLETE": K.FORBIDDEN,
    "REVIEW_CHANGED": K.FORBIDDEN,
    "DISPLAY_FAILED": K.TRANSIENT,
    "DISPLAY_UNCERTAIN": K.TRANSIENT,
    "INVALID_PAGE": K.INVALID_DATA,
    "BUSY": K.TRANSIENT,
    "APPROVED": K.OK,
    "ALREADY_APPROVED": K.ALREADY_PROCESSED,
    "APPROVED_ELSEWHERE": K.ALREADY_PROCESSED,
    "REJECTED": K.OK,
    "ALREADY_REJECTED": K.ALREADY_PROCESSED,
}

# app.services.telegram_operator_control._ANSWERS
OPERATOR_KINDS: dict[str, NoticeKind] = {
    "DISABLED": K.FORBIDDEN,
    "MALFORMED": K.INVALID_DATA,
    "UNKNOWN_CARD": K.INVALID_DATA,
    "TARGET_MISSING": K.INVALID_DATA,
    "STALE_CARD": K.INVALID_DATA,
    "DUPLICATE_CALLBACK": K.ALREADY_PROCESSED,
    "DETAILS": K.OK,
    "APPROVED": K.OK,
    "ALREADY_APPROVED": K.ALREADY_PROCESSED,
    "SKIPPED": K.OK,
    "ALREADY_SKIPPED": K.ALREADY_PROCESSED,
    "LATER_RECORDED": K.OK,
    "ALREADY_LATER": K.ALREADY_PROCESSED,
    "STATE_CHANGED": K.INVALID_DATA,
}

# Phrases that tell the reader to try again. "kein/nicht ... erneut" says the opposite and is
# removed before matching.
_NEGATED = re.compile(r"(kein\w*|nicht|never|not)\s+(\w+\s+){0,3}(erneut\w*|again)", re.IGNORECASE)
_RETRY_HINT = re.compile(
    r"erneut|nochmals|noch einmal|später|wiederhol|try again|retry|versuchen|gerade nicht",
    re.IGNORECASE,
)


def suggests_retry(text: str) -> bool:
    return bool(_RETRY_HINT.search(_NEGATED.sub("", text)))
