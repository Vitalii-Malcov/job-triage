"""Per-client-IP, in-process sliding-window rate limiting for the HTTP API.

Architecture:
- Each endpoint (or group sharing a cost profile) gets its own independent
  `_RateLimiter` instance: own bucket dict (keyed by the client identity
  from `app.security.client_identity.resolve_client_identity` -- the TCP
  peer unless that peer is an explicitly trusted proxy),
  own lock, own fixed 429 detail message. Never shared across limiters --
  see each bucket's own comment below for why it's separate.
- Same-limiter read/expire/count/append is atomic: `check()` does the
  whole sequence inside one `with self._lock:` block.
- `max_requests`/`window_seconds` are passed into `check()` at call time,
  not bound at construction, so each `enforce_*_rate_limit` function's own
  bare-name reference to its module-level constant (or, for
  `enforce_rate_limit`, a fresh `get_settings()` call) stays monkeypatchable
  exactly as before this refactor.
- Each bucket dict is also exposed under its original module-level name
  (e.g. `_xing_requests = _xing_limiter.buckets`, the same object, not a
  copy) solely so the existing test suite's
  `rate_limit_module._xing_requests.clear()` fixtures keep working.
"""

import ipaddress
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.security import shared_state
from app.security.client_identity import resolve_client_identity


class _RateLimiter:
    """One independent sliding-window rate-limit bucket set, keyed by
    the resolved client identity (see module docstring), plus the lock
    that guards it. A class is used here -- rather than a bare function +
    module dict -- because this object owns mutable, concurrently-accessed state; rate/window
    configuration itself stays plain module-level constants (see module
    docstring), not attributes of this class.
    """

    # a distinct-host key is never removed from `self.buckets`
    # just by its own deque emptying out (trimming only ever runs when
    # THAT host makes another request) -- a host that is never seen again
    # would otherwise leave its dict entry allocated for the life of the
    # process. Bounded, amortized-cost fix: once a limiter has
    # accumulated more distinct host keys than this threshold, one
    # request pays the cost of sweeping out every key whose bucket is now
    # fully expired. Below the threshold, check() is unchanged (no sweep
    # cost at all) -- see docs/ADVERSARIAL_HARDENING_REPORT.md HARD-001.
    _SWEEP_THRESHOLD = 512

    def __init__(self, *, name: str, detail: str) -> None:
        self.name = name  # stable id in the shared store (Task 23)
        self.buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._detail = detail

    # ---- Task 23: process-shared backend (SHARED_STATE_BACKEND=database) ----------------
    def _shared_hit(self, key: str, *, max_requests: int, window_seconds: float) -> int:
        try:
            hit = shared_state.rate_hit(
                self.name, key, max_requests=max_requests, window_seconds=window_seconds
            )
        except shared_state.SharedStateUnavailableError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limit store unavailable",
            ) from None
        if hit is None:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=self._detail)
        return hit

    def check(self, request: Request, *, max_requests: int, window_seconds: float) -> None:
        # Stage 13A: the trusted-proxy-aware identity, never a raw
        # forwarding header -- see app/security/client_identity.py.
        key = resolve_client_identity(request, get_settings().trusted_proxy_ip_set)
        if shared_state.enabled():
            self._shared_hit(key, max_requests=max_requests, window_seconds=window_seconds)
            return
        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            bucket = self.buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=self._detail,
                )
            bucket.append(now)
            if len(self.buckets) > self._SWEEP_THRESHOLD:
                self._evict_expired_buckets(cutoff)

    # Failure-budget path only: live identities are capped, and the expiry
    # sweep runs at most once per `_MIN_SWEEP_INTERVAL` seconds so rotating
    # source addresses cannot force a full scan on every request.
    _MAX_KEYS = 4096

    @staticmethod
    def _failure_key(identity: str) -> str:
        """IPv6 clients are tracked per /64: one host routinely controls a
        whole /64, so per-address identities would be free to rotate."""
        try:
            address = ipaddress.ip_address(identity)
        except ValueError:
            return identity
        if address.version == 6:
            return str(ipaddress.ip_network(f"{address}/64", strict=False))
        return identity

    _MIN_SWEEP_INTERVAL = 1.0
    _last_sweep = 0.0

    def reserve(self, request: Request, *, max_requests: int, window_seconds: float) -> float | int:
        """Atomically refuse (429) a client already holding `max_requests`
        live entries, otherwise record one entry for it. The caller refunds
        it with `refund(request, token)` (the returned token) when the attempt
        turns out to be legitimate."""
        key = self._failure_key(
            resolve_client_identity(request, get_settings().trusted_proxy_ip_set)
        )
        if shared_state.enabled():
            return self._shared_hit(key, max_requests=max_requests, window_seconds=window_seconds)
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self.buckets.get(key)
            if bucket is not None:
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if len(bucket) >= max_requests:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=self._detail,
                    )
            if len(self.buckets) >= self._SWEEP_THRESHOLD and (
                now - self._last_sweep >= self._MIN_SWEEP_INTERVAL
            ):
                self._last_sweep = now
                self._evict_expired_buckets(cutoff)
            if len(self.buckets) >= self._MAX_KEYS and key not in self.buckets:
                # Hard cap: forget the longest-tracked identity that is NOT
                # locked out. A locked identity is never forgotten (that
                # would let address churn reset lockouts); if every tracked
                # identity is locked, refuse the newcomer (fail closed).
                victim = next(
                    (k for k, b in self.buckets.items() if len(b) < max_requests),
                    None,
                )
                if victim is None:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=self._detail,
                    )
                del self.buckets[victim]
            self.buckets[key].append(now)
            return now

    def refund(self, request: Request, token: float | int) -> None:
        """Undo exactly the `reserve` that returned `token` (a successful
        authentication). Already-expired entries are simply gone."""
        if shared_state.enabled():
            identity = self._failure_key(
                resolve_client_identity(request, get_settings().trusted_proxy_ip_set)
            )
            try:
                shared_state.rate_refund(self.name, identity, int(token))
            except shared_state.SharedStateUnavailableError:
                # The attempt stays charged: failing to refund can only make the budget
                # stricter, never looser.
                return
            return
        key = self._failure_key(
            resolve_client_identity(request, get_settings().trusted_proxy_ip_set)
        )
        with self._lock:
            bucket = self.buckets.get(key)
            if bucket is None:
                return
            try:
                bucket.remove(token)
            except ValueError:
                return
            if not bucket:
                del self.buckets[key]

    def _evict_expired_buckets(self, cutoff: float) -> None:
        # Called with self._lock already held. A bucket is fully expired
        # (safe to forget) if its newest entry is still older than the
        # current cutoff -- `key`'s own bucket (just appended to above)
        # can never match this, so it's never evicted by its own request.
        stale_keys = [k for k, bucket in self.buckets.items() if not bucket or bucket[-1] < cutoff]
        for stale_key in stale_keys:
            del self.buckets[stale_key]


def _quota(name: str, limiter: "_RateLimiter", quota):
    """FastAPI dependency enforcing a fixed per-client quota on `limiter`.

    `quota` is a zero-argument callable returning `(max_requests, window_seconds)`; the
    limits are module-level constants read at CALL time (not bound at definition), so a
    test or an operator hook can still replace `XING_RATE_LIMIT_REQUESTS` etc. The
    dependency keeps its `enforce_*` name (introspection, route-guard tests)."""

    def enforce(request: Request) -> None:
        max_requests, window_seconds = quota()
        limiter.check(request, max_requests=max_requests, window_seconds=window_seconds)

    enforce.__name__ = name
    enforce.__qualname__ = name
    return enforce


_generic_limiter = _RateLimiter(name="generic", detail="Rate limit exceeded")
_requests = _generic_limiter.buckets


def enforce_rate_limit(request: Request) -> None:
    settings = get_settings()
    _generic_limiter.check(
        request,
        max_requests=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )


# Separate, stricter bucket for expensive collector runs: each call makes
# many outbound requests to a third-party API (itself rate-limited) and
# writes to the DB in a loop, unlike a single /jobs/score call. Fixed rather
# than Settings-driven since this endpoint is meant to be triggered manually
# and infrequently, not tuned per deployment.
_collector_limiter = _RateLimiter(name="collector", detail="Collector rate limit exceeded")
_collector_requests = _collector_limiter.buckets
COLLECTOR_RATE_LIMIT_REQUESTS = 5
COLLECTOR_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_collector_rate_limit = _quota(
    "enforce_collector_rate_limit",
    _collector_limiter,
    lambda: (COLLECTOR_RATE_LIMIT_REQUESTS, COLLECTOR_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate, stricter bucket than the generic collector limit above, specific
# to the IMAP-based XING collector. Rationale: repeated failed/rapid IMAP
# logins risk tripping the mailbox provider's own abuse/suspicious-activity
# detection (e.g. Gmail temporarily blocking the account), which is a
# distinct and more disruptive failure mode than merely hitting a public
# HTTP API's rate limit — an accidental lockout of the user's real mailbox
# is worse than a slow collector run. Kept in its own bucket rather than
# sharing `_collector_requests` so calling the Bundesagentur collector
# doesn't eat into the IMAP collector's (tighter) budget or vice versa.
_xing_limiter = _RateLimiter(name="xing", detail="XING collector rate limit exceeded")
_xing_requests = _xing_limiter.buckets
XING_RATE_LIMIT_REQUESTS = 3
XING_RATE_LIMIT_WINDOW_SECONDS = 600


enforce_xing_rate_limit = _quota(
    "enforce_xing_rate_limit",
    _xing_limiter,
    lambda: (XING_RATE_LIMIT_REQUESTS, XING_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate, stricter bucket for company-research runs: each call can make
# one outbound HTTP fetch to a third-party company website (when enabled)
# plus a DB write, similar cost profile to the collector endpoints above.
# Fixed rather than Settings-driven for the same reason as the collector
# bucket: triggered manually/infrequently, not tuned per deployment.
_company_research_limiter = _RateLimiter(
    name="company_research", detail="Company research rate limit exceeded"
)
_company_research_requests = _company_research_limiter.buckets
COMPANY_RESEARCH_RATE_LIMIT_REQUESTS = 10
COMPANY_RESEARCH_RATE_LIMIT_WINDOW_SECONDS = 600


enforce_company_research_rate_limit = _quota(
    "enforce_company_research_rate_limit",
    _company_research_limiter,
    lambda: (COMPANY_RESEARCH_RATE_LIMIT_REQUESTS, COMPANY_RESEARCH_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate bucket for candidate-job-match runs: unlike the collector/XING/
# company-research buckets above, matching makes zero outbound network
# calls (pure local computation over already-persisted data) and is cheap
# per call — but a POST still writes a new candidate_job_matches row when
# the cache misses, so it gets its own bucket rather than sharing the
# generic per-key budget, sized more generously than the network-bound
# buckets above to reflect that lower cost.
_match_limiter = _RateLimiter(name="match", detail="Candidate job match rate limit exceeded")
_match_requests = _match_limiter.buckets
MATCH_RATE_LIMIT_REQUESTS = 30
MATCH_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_match_rate_limit = _quota(
    "enforce_match_rate_limit",
    _match_limiter,
    lambda: (MATCH_RATE_LIMIT_REQUESTS, MATCH_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate bucket for CV draft generation: like matching (section above),
# this is pure local computation with zero network calls, but a POST still
# writes a new candidate_cv_drafts row when the cache misses — same
# rationale and same generous sizing as MATCH_RATE_LIMIT above.
_cv_draft_limiter = _RateLimiter(name="cv_draft", detail="CV draft rate limit exceeded")
_cv_draft_requests = _cv_draft_limiter.buckets
CV_DRAFT_RATE_LIMIT_REQUESTS = 30
CV_DRAFT_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_cv_draft_rate_limit = _quota(
    "enforce_cv_draft_rate_limit",
    _cv_draft_limiter,
    lambda: (CV_DRAFT_RATE_LIMIT_REQUESTS, CV_DRAFT_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate, stricter bucket for Bewerbung generation: unlike match/cv-draft
# (pure local computation), this endpoint calls out to a BewerbungProvider
# (spec: "generation is an external/expensive operation" even though v1's
# only shipped provider is local/deterministic — sized for a future
# real-LLM provider's cost profile now rather than widening later).
_bewerbung_limiter = _RateLimiter(name="bewerbung", detail="Bewerbung draft rate limit exceeded")
_bewerbung_requests = _bewerbung_limiter.buckets
BEWERBUNG_RATE_LIMIT_REQUESTS = 5
BEWERBUNG_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_bewerbung_rate_limit = _quota(
    "enforce_bewerbung_rate_limit",
    _bewerbung_limiter,
    lambda: (BEWERBUNG_RATE_LIMIT_REQUESTS, BEWERBUNG_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate bucket for review-package writes (create/patch/approve/reject):
# like match/cv-draft, this is pure local DB read+write with zero network
# cost (spec: "Approval does not need external-operation limits ... Do not
# reuse an expensive LLM rate bucket unnecessarily") — sized identically to
# MATCH_RATE_LIMIT/CV_DRAFT_RATE_LIMIT rather than the stricter Bewerbung
# generation bucket.
_review_write_limiter = _RateLimiter(
    name="review_write", detail="Review package rate limit exceeded"
)
_review_write_requests = _review_write_limiter.buckets
REVIEW_WRITE_RATE_LIMIT_REQUESTS = 30
REVIEW_WRITE_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_review_write_rate_limit = _quota(
    "enforce_review_write_rate_limit",
    _review_write_limiter,
    lambda: (REVIEW_WRITE_RATE_LIMIT_REQUESTS, REVIEW_WRITE_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate, stricter bucket for the Gmail inbox IMAP sync endpoint — same
# rationale as XING_RATE_LIMIT above (repeated/rapid IMAP logins risk
# tripping the mailbox provider's own abuse detection, e.g. Gmail
# temporarily locking the account). Kept in its own bucket rather than
# sharing XING's so the two independent mailboxes/credentials never
# compete for the same budget.
_gmail_limiter = _RateLimiter(name="gmail", detail="Gmail inbox sync rate limit exceeded")
_gmail_requests = _gmail_limiter.buckets
GMAIL_RATE_LIMIT_REQUESTS = 3
GMAIL_RATE_LIMIT_WINDOW_SECONDS = 600


enforce_gmail_rate_limit = _quota(
    "enforce_gmail_rate_limit",
    _gmail_limiter,
    lambda: (GMAIL_RATE_LIMIT_REQUESTS, GMAIL_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate bucket for Stage 7B analysis runs: like match/cv-draft/
# review-write above, this is pure local computation (deterministic
# regex-based matching/classification, zero network calls) with a DB
# write on cache miss — sized identically to those buckets rather than
# the stricter network-bound ones (XING/Gmail sync/Bewerbung).
_gmail_analysis_limiter = _RateLimiter(
    name="gmail_analysis", detail="Gmail message analysis rate limit exceeded"
)
_gmail_analysis_requests = _gmail_analysis_limiter.buckets
GMAIL_ANALYSIS_RATE_LIMIT_REQUESTS = 30
GMAIL_ANALYSIS_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_gmail_analysis_rate_limit = _quota(
    "enforce_gmail_analysis_rate_limit",
    _gmail_analysis_limiter,
    lambda: (GMAIL_ANALYSIS_RATE_LIMIT_REQUESTS, GMAIL_ANALYSIS_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate bucket for Stage 7C response-draft generation: like the Stage
# 7B analysis bucket above, this is pure local computation (deterministic
# template lookup, zero network calls) with a DB write on cache miss —
# sized identically to GMAIL_ANALYSIS_RATE_LIMIT rather than the
# stricter network-bound buckets (XING/Gmail sync/Bewerbung).
_response_draft_limiter = _RateLimiter(
    name="response_draft", detail="Response draft rate limit exceeded"
)
_response_draft_requests = _response_draft_limiter.buckets
RESPONSE_DRAFT_RATE_LIMIT_REQUESTS = 30
RESPONSE_DRAFT_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_response_draft_rate_limit = _quota(
    "enforce_response_draft_rate_limit",
    _response_draft_limiter,
    lambda: (RESPONSE_DRAFT_RATE_LIMIT_REQUESTS, RESPONSE_DRAFT_RATE_LIMIT_WINDOW_SECONDS),
)


# Bucket for Stage 7D approve/reject decisions: pure local DB read+write,
# zero network cost — sized like MATCH_RATE_LIMIT/REVIEW_WRITE_RATE_LIMIT
# rather than the stricter network-bound buckets below.
_response_draft_decision_limiter = _RateLimiter(
    name="response_draft_decision", detail="Response draft decision rate limit exceeded"
)
_response_draft_decision_requests = _response_draft_decision_limiter.buckets
RESPONSE_DRAFT_DECISION_RATE_LIMIT_REQUESTS = 30
RESPONSE_DRAFT_DECISION_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_response_draft_decision_rate_limit = _quota(
    "enforce_response_draft_decision_rate_limit",
    _response_draft_decision_limiter,
    lambda: (
        RESPONSE_DRAFT_DECISION_RATE_LIMIT_REQUESTS,
        RESPONSE_DRAFT_DECISION_RATE_LIMIT_WINDOW_SECONDS,
    ),
)


# Separate, stricter bucket for Stage 7D SEND — same rationale as
# XING_RATE_LIMIT/GMAIL_RATE_LIMIT above: this is the one endpoint in
# this project that transmits a real outbound email, and repeated/rapid
# SMTP logins risk tripping Gmail's own abuse detection on the same
# account the read-only IMAP sync uses. Kept in its own bucket, sized
# tightly, rather than sharing any other bucket.
_response_draft_send_limiter = _RateLimiter(
    name="response_draft_send", detail="Response draft send rate limit exceeded"
)
_response_draft_send_requests = _response_draft_send_limiter.buckets
RESPONSE_DRAFT_SEND_RATE_LIMIT_REQUESTS = 5
RESPONSE_DRAFT_SEND_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_response_draft_send_rate_limit = _quota(
    "enforce_response_draft_send_rate_limit",
    _response_draft_send_limiter,
    lambda: (
        RESPONSE_DRAFT_SEND_RATE_LIMIT_REQUESTS,
        RESPONSE_DRAFT_SEND_RATE_LIMIT_WINDOW_SECONDS,
    ),
)


# Bucket for Stage 7E follow-up evaluation: unlike a single-message Stage
# 7B analysis, one call scans up to FOLLOW_UP_JOB_SCAN_LIMIT tracked jobs
# (pure local computation + a DB write per newly-eligible one) — sized
# tighter than GMAIL_ANALYSIS_RATE_LIMIT to reflect that bulk-scan cost.
_follow_up_evaluate_limiter = _RateLimiter(
    name="follow_up_evaluate", detail="Follow-up evaluation rate limit exceeded"
)
_follow_up_evaluate_requests = _follow_up_evaluate_limiter.buckets
FOLLOW_UP_EVALUATE_RATE_LIMIT_REQUESTS = 10
FOLLOW_UP_EVALUATE_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_follow_up_evaluate_rate_limit = _quota(
    "enforce_follow_up_evaluate_rate_limit",
    _follow_up_evaluate_limiter,
    lambda: (FOLLOW_UP_EVALUATE_RATE_LIMIT_REQUESTS, FOLLOW_UP_EVALUATE_RATE_LIMIT_WINDOW_SECONDS),
)


# Bucket for Stage 7E follow-up approve/reject decisions: pure local DB
# read+write, zero network cost — sized like
# RESPONSE_DRAFT_DECISION_RATE_LIMIT.
_follow_up_decision_limiter = _RateLimiter(
    name="follow_up_decision", detail="Follow-up decision rate limit exceeded"
)
_follow_up_decision_requests = _follow_up_decision_limiter.buckets
FOLLOW_UP_DECISION_RATE_LIMIT_REQUESTS = 30
FOLLOW_UP_DECISION_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_follow_up_decision_rate_limit = _quota(
    "enforce_follow_up_decision_rate_limit",
    _follow_up_decision_limiter,
    lambda: (FOLLOW_UP_DECISION_RATE_LIMIT_REQUESTS, FOLLOW_UP_DECISION_RATE_LIMIT_WINDOW_SECONDS),
)


# Separate, stricter bucket for Stage 7E follow-up SEND — same rationale
# as RESPONSE_DRAFT_SEND_RATE_LIMIT: this transmits a real outbound email
# over the same account's SMTP credentials.
_follow_up_send_limiter = _RateLimiter(
    name="follow_up_send", detail="Follow-up send rate limit exceeded"
)
_follow_up_send_requests = _follow_up_send_limiter.buckets
FOLLOW_UP_SEND_RATE_LIMIT_REQUESTS = 5
FOLLOW_UP_SEND_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_follow_up_send_rate_limit = _quota(
    "enforce_follow_up_send_rate_limit",
    _follow_up_send_limiter,
    lambda: (FOLLOW_UP_SEND_RATE_LIMIT_REQUESTS, FOLLOW_UP_SEND_RATE_LIMIT_WINDOW_SECONDS),
)


# Stage 8A: POST /automation/runs coordinates BOTH the Bundesagentur and
# XING collectors in one call — sized at least as strict as the
# stricter of the two per-collector buckets it triggers
# (XING_RATE_LIMIT above), since one orchestrated run costs at least as
# much as one XING run plus one Bundesagentur run. Kept in its own
# bucket rather than sharing either collector's so a manual single-
# collector run and an orchestrated run never compete for the same
# budget.
_automation_run_limiter = _RateLimiter(
    name="automation_run", detail="Automation run rate limit exceeded"
)
_automation_run_requests = _automation_run_limiter.buckets
AUTOMATION_RUN_RATE_LIMIT_REQUESTS = 3
AUTOMATION_RUN_RATE_LIMIT_WINDOW_SECONDS = 600


enforce_automation_run_rate_limit = _quota(
    "enforce_automation_run_rate_limit",
    _automation_run_limiter,
    lambda: (AUTOMATION_RUN_RATE_LIMIT_REQUESTS, AUTOMATION_RUN_RATE_LIMIT_WINDOW_SECONDS),
)


# Accepted trade-off: when 4096 distinct networks are all locked out the table
# is full and further NEW clients (including the operator) get 429 until a
# lockout expires (<= AUTH_FAILURE_WINDOW_SECONDS after the attack stops).
# Failing open instead would allow unlimited key guessing at that scale; a
# temporary denial is the smaller harm. Existing lockouts are never evicted.
# Failed-credential budget, shared by every route that checks the API key
# (REST dependency and the UI login form guard the same secret). Only
# FAILURES stay charged (a slot is reserved before the comparison and refunded
# on success); once a client holds AUTH_FAILURE_LIMIT of them inside
# the window it is refused with 429 BEFORE its key is compared, so a locked-out
# client learns nothing more about the key -- not even from a correct guess.
_auth_failure_limiter = _RateLimiter(
    name="auth_failure", detail="Too many failed authentication attempts"
)
_auth_failure_requests = _auth_failure_limiter.buckets
AUTH_FAILURE_LIMIT = 10
AUTH_FAILURE_WINDOW_SECONDS = 300


def begin_auth_attempt(request: Request) -> float | int:
    """Charge one failure slot for this attempt (429 if none left). Atomic:
    concurrent attempts cannot overshoot the limit. The caller MUST call
    `end_auth_attempt_ok` if the credential turns out to be valid."""
    return _auth_failure_limiter.reserve(
        request,
        max_requests=AUTH_FAILURE_LIMIT,
        window_seconds=AUTH_FAILURE_WINDOW_SECONDS,
    )


def end_auth_attempt_ok(request: Request, token: float | int) -> None:
    _auth_failure_limiter.refund(request, token)


# Stage 20: the browser login form compares a submitted API key, so it gets
# its own small bucket (a password-guessing surface, unlike a read).
_ui_login_limiter = _RateLimiter(name="ui_login", detail="Login rate limit exceeded")
UI_LOGIN_RATE_LIMIT_REQUESTS = 10
UI_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300


enforce_ui_login_rate_limit = _quota(
    "enforce_ui_login_rate_limit",
    _ui_login_limiter,
    lambda: (UI_LOGIN_RATE_LIMIT_REQUESTS, UI_LOGIN_RATE_LIMIT_WINDOW_SECONDS),
)
