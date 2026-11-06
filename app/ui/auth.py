"""Stage 20: browser login for the read-only UI.

A browser does not send the API's `X-API-Key` header, so the UI exchanges
the same `API_KEY` for a session cookie:

    <expiry unix seconds>.<random session id>.<hex HMAC-SHA256(signing key,
    "jobtriage-ui/2|<expiry>|<session id>")>

It holds no key material and expires after `UI_SESSION_HOURS`. The signing key
(`signing_key`) is derived from the API key / `UI_SESSION_SECRET` and a random
per-process nonce, so:

- rotating `API_KEY` or `UI_SESSION_SECRET` ("log out everywhere") revokes
  every cookie once the web process is restarted with the new value;
- a RESTART of the web process also invalidates every cookie (the nonce is
  not persisted): a logged-out cookie can therefore not come back to life;
- LOGOUT revokes this session's id in a registry until the cookie's own
  expiry (bounded; a full registry fails the logout with 503 instead of
  forgetting a revocation).

Two backends (`SHARED_STATE_BACKEND`, app/security/shared_state.py). `memory`
(default) relies on the documented topology of ONE web process (`--workers 1`,
one replica; a second process refuses to start): the nonce and the registry are
per process. `database`: the registry is a table of the existing database and the
nonce is a constant, so any number of workers accept each other's cookies and honour
every logout, and a restart no longer invalidates cookies (the revocations persist).
The UI request session stays read-only in both modes; the shared tables are written
through their own engine. An empty `API_KEY` refuses every login and every cookie
(same fail-closed rule as `require_api_key`).
"""

import hashlib
import hmac
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from app.core.config import Settings, get_settings, reveal
from app.security import shared_state
from app.security.id_bounds import reject_out_of_range_ids

COOKIE_NAME = "jobtriage_ui"
_CONTEXT = "jobtriage-ui/2|"
# Random per web process: a restart invalidates every cookie (see module docstring).
_PROCESS_NONCE = secrets.token_hex(16)
# parse a cookie only in its exact shape; anything else
# (non-ASCII digits, odd signature) is "not logged in", never a 500.
_TOKEN = re.compile(r"([0-9]{1,12})\.([0-9a-f]{32})\.([0-9a-f]{64})")


class NotLoggedInError(Exception):
    """No valid UI session; the route redirects to the login page."""


_KEY_CONTEXT = b"jobtriage-ui-session-key/1|"


def _nonce() -> str:
    """Per-process random nonce (a restart invalidates every cookie), or - with the shared
    backend, where several workers must accept each other's cookies - a constant: the logout
    revocations are then persistent and shared, so a restart no longer needs to invalidate."""
    return "shared" if shared_state.enabled() else _PROCESS_NONCE


def signing_key(settings: Settings) -> str:
    """The key UI cookies are signed with -- never the raw API key.

    HMAC-SHA256 keyed with `UI_SESSION_SECRET` (independent secret, when set)
    or, as a compatibility fallback, the API key itself, over a fixed context
    plus the CURRENT API key. Binding the API key keeps two properties: an
    empty `API_KEY` fails closed (empty result, nothing validates) and
    rotating `API_KEY` revokes every issued cookie. Signing-secret rotation
    revokes them too. The fallback adds domain separation only (anyone who
    knows API_KEY can derive it); and a stolen API key still logs in via the
    login form regardless of mode.
    """
    api_key = reveal(settings.api_key)
    if not api_key:
        return ""
    secret = reveal(settings.ui_session_secret) or api_key
    return hmac.new(
        secret.encode("utf-8"),
        _KEY_CONTEXT + api_key.encode("utf-8") + b"|" + _nonce().encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _signature(key: str, expiry: int, sid: str) -> str:
    return hmac.new(
        key.encode("utf-8"), f"{_CONTEXT}{expiry}|{sid}".encode(), hashlib.sha256
    ).hexdigest()


def issue_session_token(key: str, *, hours: int, now: float | None = None) -> str:
    expiry = int((time.time() if now is None else now) + hours * 3600)
    sid = secrets.token_hex(16)
    return f"{expiry}.{sid}.{_signature(key, expiry, sid)}"


def parse_session_token(
    token: str | None, key: str, *, now: float | None = None
) -> tuple[int, str] | None:
    """(expiry, session id) of a correctly signed, unexpired token, else None.
    Says nothing about revocation (see `SessionRevocations`)."""
    if not token or not key:
        return None
    match = _TOKEN.fullmatch(token)
    if match is None:
        return None
    expiry, sid, signature = int(match.group(1)), match.group(2), match.group(3)
    if expiry <= (time.time() if now is None else now):
        return None
    if not hmac.compare_digest(signature, _signature(key, expiry, sid)):
        return None
    return expiry, sid


def session_token_valid(token: str | None, key: str, *, now: float | None = None) -> bool:
    return parse_session_token(token, key, now=now) is not None


class SessionRevocations:
    """In-process set of logged-out session ids, each kept until the cookie's
    own expiry. Never evicts an unexpired entry: when full, `revoke` returns
    False and the caller must report the logout as failed."""

    CAPACITY = 10_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revoked: dict[str, int] = {}

    def is_revoked(self, sid: str) -> bool:
        if shared_state.enabled():
            try:
                return shared_state.session_is_revoked(sid)
            except shared_state.SharedStateUnavailableError:
                return True  # fail closed: when revocation cannot be checked, the cookie is refused
        with self._lock:
            return sid in self._revoked

    def revoke(self, sid: str, expiry: int, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        if shared_state.enabled():
            try:
                return shared_state.session_revoke(sid, expiry, capacity=self.CAPACITY, now=moment)
            except shared_state.SharedStateUnavailableError:
                return False  # the logout is reported as failed (503), never silently dropped
        with self._lock:
            if sid in self._revoked:
                return True
            if len(self._revoked) >= self.CAPACITY:
                for known in [k for k, e in self._revoked.items() if e <= moment]:
                    del self._revoked[known]
                if len(self._revoked) >= self.CAPACITY:
                    return False
            self._revoked[sid] = expiry
            return True

    def clear(self) -> None:
        with self._lock:
            self._revoked.clear()
        if shared_state.enabled():
            shared_state.session_clear()


revocations = SessionRevocations()


def api_key_matches(submitted: str, api_key: str | object) -> bool:
    api_key = reveal(api_key)
    if not api_key:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), api_key.encode("utf-8"))


def require_ui_enabled() -> None:
    if not get_settings().ui_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def require_ui_session(request: Request) -> None:
    # A forged cookie signed with a guessed key is an online guess at the API
    # key, so a presented cookie is charged to the shared failed-auth budget
    # (refunded when valid). No cookie = nothing to guess = no charge.
    from app.security.rate_limit import begin_auth_attempt, end_auth_attempt_ok

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise NotLoggedInError
    attempt = begin_auth_attempt(request)
    parsed = parse_session_token(token, signing_key(get_settings()))
    if parsed is None:
        raise NotLoggedInError
    # Correctly signed and unexpired: not a key guess, whatever happens next
    # (a stale tab with a logged-out cookie must not burn the guess budget).
    end_auth_attempt_ok(request, attempt)
    if revocations.is_revoked(parsed[1]):
        raise NotLoggedInError
    # API-01: only a logged-in operator learns that an id is unrepresentable (422).
    reject_out_of_range_ids(request)


def require_same_origin(request: Request) -> None:
    """Login/logout are the only POSTs; a cross-site form post is refused
    (login CSRF). The Origin's host must equal the request's Host header --
    the scheme is not compared, because behind the documented TLS-terminating
    proxy the browser says https while Uvicorn sees http.
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
    except ValueError:  # e.g. "https://["
        raise forbidden from None
    public = get_settings().ui_public_origin.rstrip("/")
    if public and origin.rstrip("/").casefold() == public.casefold():
        return
    host = request.headers.get("host", "")
    if not parsed.netloc or parsed.netloc.casefold() != host.casefold():
        raise forbidden
