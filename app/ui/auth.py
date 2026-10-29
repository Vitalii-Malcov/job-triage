"""Stage 20: browser login for the read-only UI.

A browser does not send the API's `X-API-Key` header, so the UI exchanges
the same `API_KEY` for a session cookie. The cookie is stateless:

    <expiry unix seconds>.<hex HMAC-SHA256(API_KEY, "jobtriage-ui/1|<expiry>")>

It holds no key material, expires after `UI_SESSION_HOURS`, and every
cookie becomes invalid when `API_KEY` is rotated. An empty `API_KEY` refuses
every login and every cookie (same fail-closed rule as `require_api_key`).
"""

import hashlib
import hmac
import re
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

COOKIE_NAME = "jobtriage_ui"
_CONTEXT = "jobtriage-ui/1|"
# Codex review A (P2): parse a cookie only in its exact shape; anything else
# (non-ASCII digits, odd signature) is "not logged in", never a 500.
_TOKEN = re.compile(r"([0-9]{1,12})\.([0-9a-f]{64})")


class NotLoggedInError(Exception):
    """No valid UI session; the route redirects to the login page."""


def _signature(api_key: str, expiry: int) -> str:
    return hmac.new(
        api_key.encode("utf-8"), f"{_CONTEXT}{expiry}".encode(), hashlib.sha256
    ).hexdigest()


def issue_session_token(api_key: str, *, hours: int, now: float | None = None) -> str:
    expiry = int((time.time() if now is None else now) + hours * 3600)
    return f"{expiry}.{_signature(api_key, expiry)}"


def session_token_valid(token: str | None, api_key: str, *, now: float | None = None) -> bool:
    if not token or not api_key:
        return False
    match = _TOKEN.fullmatch(token)
    if match is None:
        return False
    expiry = int(match.group(1))
    if expiry <= (time.time() if now is None else now):
        return False
    return hmac.compare_digest(match.group(2), _signature(api_key, expiry))


def api_key_matches(submitted: str, api_key: str) -> bool:
    if not api_key:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), api_key.encode("utf-8"))


def require_ui_enabled() -> None:
    if not get_settings().ui_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def require_ui_session(request: Request) -> None:
    if not session_token_valid(request.cookies.get(COOKIE_NAME), get_settings().api_key):
        raise NotLoggedInError


def require_same_origin(request: Request) -> None:
    """Login/logout are the only POSTs; a cross-site form post is refused
    (login CSRF). The Origin's host must equal the request's Host header --
    the scheme is not compared, because behind the documented TLS-terminating
    proxy the browser says https while Uvicorn sees http (Codex review A, P2).
    `UI_PUBLIC_ORIGIN` additionally allows one configured external origin
    for a proxy that rewrites Host. A missing Origin header (non-browser
    client) is allowed: the request then still needs the key itself (login)
    or the SameSite=Strict cookie (logout)."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    forbidden = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-origin request")
    try:
        parsed = urlsplit(origin)
    except ValueError:  # e.g. "https://[" (Codex review A round 2)
        raise forbidden from None
    public = get_settings().ui_public_origin.rstrip("/")
    if public and origin.rstrip("/").casefold() == public.casefold():
        return
    host = request.headers.get("host", "")
    if not parsed.netloc or parsed.netloc.casefold() != host.casefold():
        raise forbidden
