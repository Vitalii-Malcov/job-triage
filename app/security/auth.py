import hmac

from fastapi import Header, HTTPException, Request, status

from app.core.config import get_settings, reveal
from app.security.id_bounds import reject_out_of_range_ids
from app.security.key_strength import is_strong_secret
from app.security.rate_limit import begin_auth_attempt, end_auth_attempt_ok, enforce_rate_limit


def api_key_matches(submitted: str, expected: str) -> bool:
    """Constant-time comparison; an unset expected key never matches."""
    if not expected:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def _authenticate(request: Request, x_api_key: str) -> None:
    expected = reveal(get_settings().api_key)
    # SEC-03: with a strong key (>= MIN_STRONG_KEY_CHARS) the failed-attempt budget
    # cannot be brute-forced anyway, so a CORRECT key is never refused because other
    # clients -- or everyone, behind a proxy that hides client addresses -- burned the
    # budget. Wrong keys are still charged and still earn 429 once the budget is spent.
    if is_strong_secret(expected) and api_key_matches(x_api_key, expected):
        return
    # Otherwise (weak key, or wrong key): a failure slot is reserved atomically BEFORE
    # the comparison (429 when the client has none left) and refunded on success.
    token = begin_auth_attempt(request)
    if not api_key_matches(x_api_key, expected):
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
