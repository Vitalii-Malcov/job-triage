import hmac

from fastapi import Header, HTTPException, Request, status

from app.core.config import get_settings, reveal
from app.security.rate_limit import begin_auth_attempt, end_auth_attempt_ok


def api_key_matches(submitted: str, expected: str) -> bool:
    """Constant-time comparison; an unset expected key never matches."""
    if not expected:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def require_api_key(request: Request, x_api_key: str = Header(default="")) -> None:
    # A failure slot is reserved atomically BEFORE the comparison (429 when the
    # client has none left, even with the right key) and refunded on success.
    token = begin_auth_attempt(request)
    if not api_key_matches(x_api_key, reveal(get_settings().api_key)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    end_auth_attempt_ok(request, token)
