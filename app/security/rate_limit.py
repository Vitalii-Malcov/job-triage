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


# Separate bucket for candidate-job-match runs: unlike the collector/XING/
# company-research buckets above, matching makes zero outbound network
# calls (pure local computation over already-persisted data) and is cheap
# per call — but a POST still writes a new candidate_job_matches row when
# the cache misses, so it gets its own bucket rather than sharing the
# generic per-key budget, sized more generously than the network-bound
# buckets above to reflect that lower cost.
_match_requests: dict[str, deque[float]] = defaultdict(deque)
MATCH_RATE_LIMIT_REQUESTS = 30
MATCH_RATE_LIMIT_WINDOW_SECONDS = 300


def enforce_match_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - MATCH_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _match_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= MATCH_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Candidate job match rate limit exceeded",
            )
        bucket.append(now)


# Separate bucket for CV draft generation: like matching (section above),
# this is pure local computation with zero network calls, but a POST still
# writes a new candidate_cv_drafts row when the cache misses — same
# rationale and same generous sizing as MATCH_RATE_LIMIT above.
_cv_draft_requests: dict[str, deque[float]] = defaultdict(deque)
CV_DRAFT_RATE_LIMIT_REQUESTS = 30
CV_DRAFT_RATE_LIMIT_WINDOW_SECONDS = 300


def enforce_cv_draft_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - CV_DRAFT_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _cv_draft_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= CV_DRAFT_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="CV draft rate limit exceeded",
            )
        bucket.append(now)


# Separate, stricter bucket for Bewerbung generation: unlike match/cv-draft
# (pure local computation), this endpoint calls out to a BewerbungProvider
# (spec: "generation is an external/expensive operation" even though v1's
# only shipped provider is local/deterministic — sized for a future
# real-LLM provider's cost profile now rather than widening later).
_bewerbung_requests: dict[str, deque[float]] = defaultdict(deque)
BEWERBUNG_RATE_LIMIT_REQUESTS = 5
BEWERBUNG_RATE_LIMIT_WINDOW_SECONDS = 300


def enforce_bewerbung_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - BEWERBUNG_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _bewerbung_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= BEWERBUNG_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Bewerbung draft rate limit exceeded",
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


# Separate bucket for review-package writes (create/patch/approve/reject):
# like match/cv-draft, this is pure local DB read+write with zero network
# cost (spec: "Approval does not need external-operation limits ... Do not
# reuse an expensive LLM rate bucket unnecessarily") — sized identically to
# MATCH_RATE_LIMIT/CV_DRAFT_RATE_LIMIT rather than the stricter Bewerbung
# generation bucket.
_review_write_requests: dict[str, deque[float]] = defaultdict(deque)
REVIEW_WRITE_RATE_LIMIT_REQUESTS = 30
REVIEW_WRITE_RATE_LIMIT_WINDOW_SECONDS = 300


# Separate, stricter bucket for the Gmail inbox IMAP sync endpoint — same
# rationale as XING_RATE_LIMIT above (repeated/rapid IMAP logins risk
# tripping the mailbox provider's own abuse detection, e.g. Gmail
# temporarily locking the account). Kept in its own bucket rather than
# sharing XING's so the two independent mailboxes/credentials never
# compete for the same budget.
_gmail_requests: dict[str, deque[float]] = defaultdict(deque)
GMAIL_RATE_LIMIT_REQUESTS = 3
GMAIL_RATE_LIMIT_WINDOW_SECONDS = 600


def enforce_gmail_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - GMAIL_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _gmail_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= GMAIL_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Gmail inbox sync rate limit exceeded",
            )
        bucket.append(now)


# Separate bucket for Stage 7B analysis runs: like match/cv-draft/
# review-write above, this is pure local computation (deterministic
# regex-based matching/classification, zero network calls) with a DB
# write on cache miss — sized identically to those buckets rather than
# the stricter network-bound ones (XING/Gmail sync/Bewerbung).
_gmail_analysis_requests: dict[str, deque[float]] = defaultdict(deque)
GMAIL_ANALYSIS_RATE_LIMIT_REQUESTS = 30
GMAIL_ANALYSIS_RATE_LIMIT_WINDOW_SECONDS = 300


def enforce_gmail_analysis_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - GMAIL_ANALYSIS_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _gmail_analysis_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= GMAIL_ANALYSIS_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Gmail message analysis rate limit exceeded",
            )
        bucket.append(now)


# Separate bucket for Stage 7C response-draft generation: like the Stage
# 7B analysis bucket above, this is pure local computation (deterministic
# template lookup, zero network calls) with a DB write on cache miss —
# sized identically to GMAIL_ANALYSIS_RATE_LIMIT rather than the
# stricter network-bound buckets (XING/Gmail sync/Bewerbung).
_response_draft_requests: dict[str, deque[float]] = defaultdict(deque)
RESPONSE_DRAFT_RATE_LIMIT_REQUESTS = 30
RESPONSE_DRAFT_RATE_LIMIT_WINDOW_SECONDS = 300


def enforce_response_draft_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - RESPONSE_DRAFT_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _response_draft_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= RESPONSE_DRAFT_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Response draft rate limit exceeded",
            )
        bucket.append(now)


def enforce_review_write_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - REVIEW_WRITE_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _review_write_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= REVIEW_WRITE_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Review package rate limit exceeded",
            )
        bucket.append(now)


# Bucket for Stage 7D approve/reject decisions: pure local DB read+write,
# zero network cost — sized like MATCH_RATE_LIMIT/REVIEW_WRITE_RATE_LIMIT
# rather than the stricter network-bound buckets below.
_response_draft_decision_requests: dict[str, deque[float]] = defaultdict(deque)
RESPONSE_DRAFT_DECISION_RATE_LIMIT_REQUESTS = 30
RESPONSE_DRAFT_DECISION_RATE_LIMIT_WINDOW_SECONDS = 300


def enforce_response_draft_decision_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - RESPONSE_DRAFT_DECISION_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _response_draft_decision_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= RESPONSE_DRAFT_DECISION_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Response draft decision rate limit exceeded",
            )
        bucket.append(now)


# Separate, stricter bucket for Stage 7D SEND — same rationale as
# XING_RATE_LIMIT/GMAIL_RATE_LIMIT above: this is the one endpoint in
# this project that transmits a real outbound email, and repeated/rapid
# SMTP logins risk tripping Gmail's own abuse detection on the same
# account the read-only IMAP sync uses. Kept in its own bucket, sized
# tightly, rather than sharing any other bucket.
_response_draft_send_requests: dict[str, deque[float]] = defaultdict(deque)
RESPONSE_DRAFT_SEND_RATE_LIMIT_REQUESTS = 5
RESPONSE_DRAFT_SEND_RATE_LIMIT_WINDOW_SECONDS = 300


def enforce_response_draft_send_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - RESPONSE_DRAFT_SEND_RATE_LIMIT_WINDOW_SECONDS

    with _lock:
        bucket = _response_draft_send_requests[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= RESPONSE_DRAFT_SEND_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Response draft send rate limit exceeded",
            )
        bucket.append(now)
