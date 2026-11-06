"""Shared pieces of the HTTP routers: list limits, the settings accessor, small helpers.

Routers call `common.get_settings()` (never their own import), so a test (or a future
configuration hook) has exactly one place to replace the settings."""

from app.core.config import get_settings  # noqa: F401  (the ONE patch point for settings in routers)
from app.providers.email.base import normalize_account_key
from app.services.application_guard import AlreadyAppliedError

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
GMAIL_DEFAULT_LIST_LIMIT = 50
GMAIL_MAX_LIST_LIMIT = 200
GMAIL_ANALYSES_DEFAULT_LIST_LIMIT = 50
GMAIL_ANALYSES_MAX_LIST_LIMIT = 200


# BOUND-001 (api-boundaries hardening r1): every `offset: int = Query(...)`
# below previously had `ge=0` but no upper bound at all. offset=2**63
# (still a legal, non-negative Python int) crashes the SQLite driver with
# an unhandled `OverflowError` ("Python int too large to convert to
# SQLite INTEGER") -- confirmed live via TestClient
# (tests/test_api_boundary_hardening.py::TestPaginationAbuse::test_offset_at_2_63_does_not_crash),
# propagating uncaught to a raw 500 instead of a clean 422. No legitimate
# caller of this single-user API ever needs an offset anywhere near this
# project's realistic row-count scale (see
# docs/DATA_RETENTION_CAPACITY.md) -- 2**31-1 is a conventional,
# comfortably-generous bound, verified to sit far below both SQLite's
# signed-64-bit affinity limit (2**63-1) and PostgreSQL's `bigint` range,
# so the same bound is safe for both supported dialects.
MAX_OFFSET = 2**31 - 1


def _already_applied_detail(exc: AlreadyAppliedError) -> dict:
    return {
        "message": str(exc),
        "application_id": exc.application_id,
        "application_status": exc.application_status,
    }


def _current_gmail_account_key(settings) -> str:
    """The account_key every Gmail read endpoint scopes itself to
     — the currently configured GMAIL_USERNAME, normalized.
    Read endpoints never require Gmail to be configured to respond (an
    empty account_key simply matches no persisted rows, which is a safe,
    fail-closed default), but scoping every read by it means a later
    GMAIL_USERNAME change can never leak a previous account's persisted
    correspondence through the read API.
    """
    return normalize_account_key(settings.gmail_username)
