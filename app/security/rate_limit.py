import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

_lock = threading.Lock()
_requests: dict[str, deque[float]] = defaultdict(deque)

# Separate, stricter bucket for expensive collector runs: each call makes
# many outbound requests to a third-party API (itself rate-limited) and
# writes to the DB in a loop, unlike a single /jobs/score call. Fixed rather
# than Settings-driven since this endpoint is meant to be triggered manually
# and infrequently, not tuned per deployment.
_collector_requests: dict[str, deque[float]] = defaultdict(deque)
COLLECTOR_RATE_LIMIT_REQUESTS = 5
COLLECTOR_RATE_LIMIT_WINDOW_SECONDS = 300

# Separate, stricter bucket than the generic collector limit above, specific
# to the IMAP-based XING collector. Rationale: repeated failed/rapid IMAP
# logins risk tripping the mailbox provider's own abuse/suspicious-activity
# detection (e.g. Gmail temporarily blocking the account), which is a
# distinct and more disruptive failure mode than merely hitting a public
# HTTP API's rate limit — an accidental lockout of the user's real mailbox
# is worse than a slow collector run. Kept in its own bucket rather than
# sharing `_collector_requests` so calling the Bundesagentur collector
# doesn't eat into the IMAP collector's (tighter) budget or vice versa.
_xing_requests: dict[str, deque[float]] = defaultdict(deque)
XING_RATE_LIMIT_REQUESTS = 3
XING_RATE_LIMIT_WINDOW_SECONDS = 600


# Separate, stricter bucket for company-research runs: each call can make
# one outbound HTTP fetch to a third-party company website (when enabled)
# plus a DB write, similar cost profile to the collector endpoints above.
# Fixed rather than Settings-driven for the same reason as the collector
# bucket: triggered manually/infrequently, not tuned per deployment.
_company_research_requests: dict[str, deque[float]] = defaultdict(deque)
COMPANY_RESEARCH_RATE_LIMIT_REQUESTS = 10
COMPANY_RESEARCH_RATE_LIMIT_WINDOW_SECONDS = 600


def enforce_rate_limit(request: Request) -> None:
    settings = get_settings()
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - settings.rate_limit_window_seconds

    with _lock:
        bucket = _requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= settings.rate_limit_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )
        bucket.append(now)


def enforce_collector_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - COLLECTOR_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _collector_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= COLLECTOR_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Collector rate limit exceeded",
            )
        bucket.append(now)


def enforce_xing_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - XING_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _xing_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= XING_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="XING collector rate limit exceeded",
            )
        bucket.append(now)


def enforce_company_research_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - COMPANY_RESEARCH_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _company_research_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= COMPANY_RESEARCH_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Company research rate limit exceeded",
            )
        bucket.append(now)
