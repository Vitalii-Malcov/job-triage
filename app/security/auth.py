import hmac

from fastapi import Header, HTTPException, Request, status

from app.core.config import get_settings, reveal
from app.security.id_bounds import reject_out_of_range_ids
from app.security.rate_limit import begin_auth_attempt, end_auth_attempt_ok, enforce_rate_limit


def api_key_matches(submitted: str, expected: str) -> bool:
    """Constant-time comparison; an unset expected key never matches."""
    if not expected:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def _authenticate(request: Request, x_api_key: str) -> None:
    # A failure slot is reserved atomically BEFORE the comparison (429 when the
    # client has none left, even with the right key) and refunded on success.
    token = begin_auth_attempt(request)
    if not api_key_matches(x_api_key, reveal(get_settings().api_key)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    end_auth_attempt_ok(request, token)


def require_api_key(request: Request, x_api_key: str = Header(default="")) -> None:
    _authenticate(request, x_api_key)
    # API-01: only an authenticated caller learns that an id is unrepresentable (422).
    try:
        reject_out_of_range_ids(request)
    except HTTPException:
        # The refusal happens before the route's own limiter dependency would run, so it is
        # charged to the generic limiter here: oversized ids must not be a free, unmetered probe.
        enforce_rate_limit(request)
        raise
