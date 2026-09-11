# Adversarial Hardening Report — JobTriage

Branch: `hardening/adversarial-r1`. Base: `9b33a72e78cfe771e468196e6e656ad1dc48af00` (frozen `chore/production-readiness-r1`). This is a NEW audit pass — it does not repeat `docs/PRODUCTION_READINESS_AUDIT.md`'s findings (AUD-001 through AUD-020), only references them where directly relevant. Every finding below traces to either a passing/failing automated test, a live failure-injection experiment against real Docker/PostgreSQL, or a static AST/code-read proof — never invented.

---

## How to read this document

- **Priority:** P0 (deployment blocker / data-loss / serious security risk) · P1 (should fix before real-world deployment) · P2 (important hardening) · P3 (optional / scaling / polish)
- **Verified/Theoretical:** VERIFIED (reproduced by a passing test or a live experiment) · THEORETICAL (static proof, not exercised live)
- **Fix status:** FIXED (implemented + tested this branch) · DOCUMENTED (test/proof exists, deliberately not fixed) · CONFIRMED-SAFE (investigated, no issue found)
- **Codex/Astra required:** whether independent review is needed before the finding (if unfixed) could ever be acted on

---

## Findings

### HARD-001 — Rate limiter bucket dict keys for expired hosts were never evicted
- **Priority:** P2 · **Verified/Theoretical:** VERIFIED · **File/function:** `app/security/rate_limit.py::_RateLimiter`
- **Reproduction:** `tests/test_security.py::TestRateLimiterBucketEviction` (3 tests) — populate 513 distinct host buckets, advance simulated `time.monotonic()` past the window, make one more request, assert the dict shrinks back to 1 entry; a below-threshold control test confirms no sweep runs below the threshold (no per-call cost regression).
- **Impact:** unbounded, slow memory growth proportional to distinct-client count for the life of the process (originally flagged as AUD-005 in the prior audit, deferred there — now fixed).
- **Fix status:** **FIXED.** Added a bounded, amortized sweep: once a limiter's dict exceeds `_SWEEP_THRESHOLD = 512` distinct hosts, the request that crosses the threshold pays the one-time cost of evicting every fully-expired key. Below the threshold, `check()` is byte-identical to before (zero added cost). All 14 independent limiters share the same `_RateLimiter` class, so the fix applies uniformly.
- **Codex/Astra:** No — mechanical, well-tested, no security/business-logic change.

### HARD-002 — Bundesagentur collector-run failure logged the full traceback (sibling of an already-fixed XING leak)
- **Priority:** P2 · **Verified/Theoretical:** VERIFIED · **File/function:** `app/api/routes.py::run_bundesagentur_collector`
- **Reproduction:** `tests/test_collector_endpoint.py::TestRunBundesagenturCollector::test_upstream_failure_log_does_not_leak_via_exc_info` — a chained `OSError` with a sensitive marker string, confirms the marker never reaches `caplog.text` and no log record has `exc_info` set.
- **Impact:** `logger.exception(...)` (`exc_info=True`) printed the full traceback including any chained `__cause__`'s own message — the exact leakage class already identified and fixed for the sibling XING endpoint (Codex gate follow-up, Astra R4B, NEW-003) but never applied here, three lines above it in the same file.
- **Fix status:** **FIXED.** Changed to `logger.warning(..., error_type=type(exc).__name__)`, byte-for-byte mirroring the XING branch's already-reviewed pattern.
- **Codex/Astra:** No — identical, already-approved pattern applied to a second call site.

### HARD-003 — Company Research's `last_error` persisted and returned the raw exception message
- **Priority:** P2 · **Verified/Theoretical:** VERIFIED · **File/function:** `app/services/company_research.py::CompanyResearchService.get_or_run`
- **Reproduction:** `tests/test_company_research_service.py::test_provider_failure_error_field_never_echoes_raw_exception_text` — injects `RuntimeError("SECRET_PROVIDER_DETAIL_MUST_NOT_LEAK://user:token@internal-host")`, asserts `result.error == "RuntimeError"` and the sensitive text is absent.
- **Impact:** this was the ONLY `last_error`-writing site in the codebase using `str(exc)` instead of a sanitized derived string — every sibling (`scheduler.py`, `follow_up_send.py`, `response_draft_send.py`) already used `type(exc).__name__`. The value is both persisted (`CompanyResearchRecord.last_error`) and returned verbatim via the public API (`CompanyResearchRunResponse.error`). v1's only provider makes zero outbound network calls today, so real-world risk was low, but the code is written generically for a future network-based provider.
- **Fix status:** **FIXED.** Changed to `type(exc).__name__`, matching the established project-wide convention.
- **Codex/Astra:** No — brings a lagging call site in line with an already-established, already-reviewed convention; response *type* (string) unchanged, only content.

### HARD-004 — `is_configured` created a cross-package layering smell (providers ↔ collectors)
- **Priority:** P3 · **Verified/Theoretical:** VERIFIED (AST-based import-graph scan; no actual cycle existed, confirmed) · **Files:** `app/collectors/base.py` → `app/utils/config_flags.py` (new), 8 importers updated
- **Reproduction:** full test suite for every touched module (293 tests, `tests/test_collectors_xing_email.py`, `tests/test_collectors_bundesagentur.py`, `tests/test_automation_gmail.py`, `tests/test_collector_runner_*.py`, `tests/test_providers_email_{smtp,imap}.py`, `tests/test_collector_endpoint.py`, `tests/test_collector_xing_endpoint.py`, `tests/test_gmail_endpoints.py`) — all pass unchanged.
- **Impact:** none currently (an AST cycle-detector over all 105 `app/` modules found zero real cycles either way — this was a package-level layering smell, not a live bug), but corresponds to AUD-012 in the prior audit, which explicitly pre-authorized this exact fix ("if AUD-012 can be fixed by moving a tiny PURE helper... with zero behavioral change... Claude MAY implement it").
- **Fix status:** **FIXED.** `is_configured` moved to a new leaf module `app/utils/config_flags.py` (zero `app.*` imports); all 8 known importers (`app/collectors/{xing_email,bundesagentur}.py`, `app/services/{gmail_sync,collector_runner,automation_gmail}.py`, `app/api/routes.py`, `app/providers/email/{smtp,imap}.py`) updated to import from the new location. `app/collectors/base.py` no longer defines it.
- **Codex/Astra:** No — pre-authorized, zero-behavior-change relocation.

### HARD-005 — `_ShutdownRequested` (scheduler SIGTERM handling) could be silently swallowed mid-cycle
- **Priority:** P2 · **Verified/Theoretical:** VERIFIED · **File/function:** `app/scheduler.py::_ShutdownRequested`, `_handle_sigterm`
- **Reproduction:** `tests/test_scheduler_poll_loop.py::TestShutdownRequestedPropagatesThroughTickLevelExceptionHandling` (2 tests) — raises `_ShutdownRequested` from inside the faked `run_due_cycle_if_claimed`, asserts it propagates out of `_poll_loop` uncaught (not logged as `automation_scheduler_poll_iteration_error`), and asserts the class hierarchy directly (`not issubclass(_ShutdownRequested, Exception)` / `issubclass(_ShutdownRequested, BaseException)`).
- **Impact:** `_ShutdownRequested` (added in the prior `chore/production-readiness-r1` branch to fix AUD-008) subclassed `Exception`. A SIGTERM landing while control was inside `_poll_loop`'s own per-tick `try: ... except Exception:` block (wrapping `run_due_cycle_if_claimed`/`run_due_digest_if_claimed`) would have been silently caught there and misreported as a generic tick error, with the poll loop continuing instead of shutting down — meaning a single `docker stop` could occasionally be absorbed and ignored, requiring a second signal (or an eventual SIGKILL after the orchestrator's grace period) to actually stop the container.
- **Fix status:** **FIXED.** `_ShutdownRequested` now subclasses `BaseException` directly, deliberately mirroring `KeyboardInterrupt`/`SystemExit` — the exact same reasoning the standard library uses for those two.
- **Codex/Astra:** No — mechanical, narrow, exactly mirrors an established stdlib pattern, does not touch business logic, does not weaken the lease/CAS crash-recovery design this fix sits on top of.

### HARD-006 — `rate_limit_requests`/`rate_limit_window_seconds`/`xing_lookback_days` accepted pathological values with no error
- **Priority:** P1 · **Verified/Theoretical:** VERIFIED · **File/function:** `app/core/config.py::Settings`
- **Reproduction:** `tests/test_config.py` — 8 new tests covering zero/negative rejection and positive-value acceptance for all three fields, plus a Gmail/XING lookback-bound parity check.
- **Impact:** `rate_limit_requests=0` (or negative) made `_RateLimiter.check`'s `len(bucket) >= max_requests` always true — **every single request, including the very first, would be 429'd** (total self-inflicted lockout). `rate_limit_window_seconds` negative made `cutoff = now - window_seconds` a FUTURE timestamp, so every bucket entry always looked expired — **the rate limiter would silently never limit anything** (a security-relevant fail-open, not a crash). `xing_lookback_days<=0` pushed the IMAP search window into the future, silently returning zero messages every collector run forever, inconsistent with the already-bounded sibling `gmail_lookback_days`. All three passed `Settings()` construction successfully and only misbehaved later, at request/collector-run time.
- **Fix status:** **FIXED.** Added `Field(ge=1)` to the two rate-limit fields and `Field(ge=1, le=1095)` (matching `gmail_lookback_days`'s existing bound) to `xing_lookback_days`. Verified no existing test or documented `.env.example` default used an invalid value (all use 1000/60/1/2/3, or the safe default 7).
- **Codex/Astra:** No — startup-time validation only, fails closed earlier than before, does not change any accepted-and-working configuration.

### HARD-007 — `upsert_job`'s new-record path has no `IntegrityError` handling, unlike every sibling insert-race site
- **Priority:** P2 · **Verified/Theoretical:** VERIFIED · **File/function:** `app/db/repositories.py::upsert_job` / `_finalize_job_write`
- **Reproduction:** `tests/test_repository.py::test_concurrent_insert_of_the_same_new_job_can_raise_unhandled_integrity_error` — two real threads/sessions racing to `upsert_job` the exact same brand-new fingerprint against a real SQLite file-backed engine (`check_same_thread=False`); asserts exactly one thread succeeds (a `tuple` result), exactly one gets an unhandled exception, and the DB never ends up with two rows for the same fingerprint.
- **Impact:** `JobRecord.fingerprint` **does** have a DB-level `UNIQUE` constraint (`uq_jobs_fingerprint`) — data integrity is never actually violated. But unlike every other insert-race site in this project (`get_or_create_schedule`, `create_approval`, `claim_send_attempt`, `upsert_message`), the losing thread's `_finalize_job_write`'s `db.commit()` has no `try/except IntegrityError` — the loser gets an **unhandled, ugly 500-class failure** (e.g. two automation cycles for different accounts, or a manual run racing the scheduler, discovering the same new posting simultaneously) instead of the graceful "someone else already inserted it, use their row" resolution this project uses everywhere else it has the same race shape.
- **Fix status:** **DOCUMENTED, NOT FIXED.** A fix (catch `IntegrityError`, re-read, return the winner's row) would touch `upsert_job`'s write/retry semantics — explicitly out of scope for this session's "no retry/idempotency guarantees without explicit evidence" / "stop and document" constraint, despite the fix direction being fairly obvious.
- **Codex/Astra:** **Codex: YES.** Astra: No (data integrity is never at risk; this is a robustness/UX gap, not a safety-critical outbound-effect gap).

### HARD-008 — `response_draft_send.py` has no recovery path for a stranded `PENDING` send record (P1, highest-priority finding)
- **Priority:** P1 · **Verified/Theoretical:** VERIFIED · **File/function:** `app/services/response_draft_send.py::send_response_draft`, `app/db/response_draft_approval_repository.py::mark_send_sent`
- **Reproduction:** `tests/test_response_draft_send_service.py::TestStrandedPendingAfterSuccessfulSendCommitFailure::test_send_record_is_stranded_pending_forever_after_post_success_commit_failure` — a fake provider's `send()` succeeds (message genuinely "transmitted"), `mark_send_sent` is monkeypatched to raise (simulating a DB connection drop / process kill at that exact instant). Asserts: the provider really was called once (the irreversible side effect happened), the send record is left `PENDING`, and — critically — a **second** attempt to send the same draft does not resolve, retry, or recover it: it raises `ResponseDraftSendInProgressError` again, forever, with zero new provider calls (so at least it never double-sends — but it also never un-sticks).
- **Impact:** this is the exact "Scenario F/G" gap identified in this session's own Phase 6 SMTP-ambiguity state-machine audit. `follow_up_send.py` (the near-identical sibling module) has a `send_attempted` CAS flag specifically so a later request can recognize "a send may already be in flight/done" and fail closed to a terminal, human-reviewable `UNCERTAIN` state. `response_draft_send.py` has **no equivalent** — a crash at exactly the wrong instant (SMTP accepted, before the local `SENT` commit) leaves the draft's send state permanently stuck, invisible to any operator dashboard/review flow, recoverable only by manual DB intervention. Same underlying gap also applies to `mark_send_sent`'s own uncaught commit (no `try/except` around it) — an ordinary DB hiccup at that exact moment produces the identical stuck state even without a process crash.
- **Fix status:** **DOCUMENTED, NOT FIXED.** This is precisely the class of change the task brief calls out explicitly: "Flag anything involving irreversible external effects as REQUIRES CODEX even if a likely fix is obvious." The obvious fix (mirror `follow_up_send.py`'s `send_attempted` flag) touches the outbound-send state machine directly — not implemented here.
- **Codex/Astra:** **Codex: YES. Astra: YES** — this is a real gap in the human-approval-adjacent outbound-email safety machinery, the same class of rigor FINAL-001/003/004 received.

### HARD-009 — Job-fingerprint dedup has no Unicode canonicalization (NFC/NFD collision-miss)
- **Priority:** P2 · **Verified/Theoretical:** VERIFIED · **File/function:** `app/db/repositories.py::_fingerprint`
- **Reproduction:** `tests/test_repository.py::test_fingerprint_nfc_vs_nfd_same_visible_company_name_does_not_dedup` — the same visible company name `"Café Zentrale"` constructed as NFC (single codepoint é) vs. NFD (decomposed e + combining accent); confirms the two byte sequences are genuinely different, confirms they're visually/canonically identical (`unicodedata.normalize("NFC", nfd) == nfc`), then confirms `_fingerprint` produces two DIFFERENT hashes for them.
- **Impact:** `_fingerprint` only does `.strip().casefold()` per field — `casefold()` does not perform Unicode canonical composition. Two collector runs (or a re-scrape) that normalize text differently upstream and produce the same visible posting in different Unicode forms would create a duplicate `JobRecord` instead of deduping — the same root-cause class as the already-fixed title-normalization bug referenced in this project's own `CLAUDE.md` Bundesagentur tech-debt notes (`url`-fallback fingerprint instability).
- **Fix status:** **DOCUMENTED, NOT FIXED.** Per this project's own established convention (referenced directly in `CLAUDE.md`'s tech debt notes), fingerprint changes require a live-data-driven audit before being changed — not a speculative one-line `unicodedata.normalize` addition, however small it looks.
- **Codex/Astra:** **Codex: YES** (with a live-data audit as a prerequisite, per existing project convention). Astra: No.

### HARD-010 — No PostgreSQL concurrency proof existed for the send-approval CAS gate
- **Priority:** P2 · **Verified/Theoretical:** VERIFIED (now covered) · **File:** `tests/integration/test_response_draft_send_postgres_concurrency.py` (new)
- **Reproduction:** a real-PostgreSQL 5-thread race on `claim_send_attempt` for the same `response_draft_id`, against a disposable `postgres:16` Docker container migrated via the real Alembic chain to `c7d3f9a1e5b8`. Asserts exactly 1 of 5 concurrent claims wins, all 5 observe the same winning row id, and the final DB state is `PENDING`/`attempt_count=1`. Passed on first run.
- **Impact:** this test-quality gap was identified by this session's own test-quality audit: `claim_send_attempt`/`begin_transmission` — the literal mechanism preventing a response-draft/follow-up email from being sent twice under concurrent dispatch — was previously proven only against SQLite's single-writer lock, unlike the schedule-claim CAS and Gmail-watermark commit-order race, which both already have dedicated real-PostgreSQL integration tests. "Do not trust a passing SQLite concurrency test as sufficient evidence" (this session's own Phase 7 instruction) was, until now, not actually honored for this specific CAS gate.
- **Fix status:** **FIXED (new test).** Added and wired into `.github/workflows/ci.yml`'s existing `scheduler-postgres` job (same real `postgres:16` service container the other two PostgreSQL integration tests already use) — runs on every push/PR, never silently skipped there.
- **Codex/Astra:** No — pure test-coverage addition, calls the real production CAS function, changes no application code.

### HARD-011 — No `connect_timeout` on the DB engine: a real outage hung requests indefinitely instead of failing fast
- **Priority:** P1 · **Verified/Theoretical:** VERIFIED (live Docker experiment) · **File/function:** `app/db/session.py`
- **Reproduction — live, not simulated:** started a disposable `postgres:16` container, confirmed a healthy connection, then `docker stop`'d it and attempted a query through the SAME SQLAlchemy engine/pool. **Before the fix:** the attempt hung past 60+ seconds with no application-level bound (the test script itself had to be killed). Repeated the experiment starting a live `uvicorn` process against the same setup: `GET /api/v1/health` correctly still returned `200` (confirms AUD-002's existing finding), but `GET /api/v1/jobs` (a real DB-touching route) hung past 30 seconds with no response. **After the fix:** the same stopped-DB experiment failed cleanly with `psycopg.errors.ConnectionTimeout` — see the dual-stack nuance below. Automated regression: `tests/test_db_session_connect_args.py` (3 tests) proves the pure `_connect_args_for(url)` helper returns the right `connect_args` per dialect.
- **Impact:** a DB outage (container stopped, network partition, host down but port/network path still reachable at the OS/container level rather than actively refusing) left every DB-touching request thread hung indefinitely — no timeout, no error, no log line — rather than failing fast with a diagnosable error. Combined with AUD-002 (`/health` staying `200`), this made a real outage look like a "frozen but healthy" service from the outside: health checks pass, but no actual request ever completes or errors.
- **Fix status:** **FIXED**, with an honestly-documented nuance: `_connect_args_for` now returns `{"connect_timeout": 10}` for non-SQLite URLs (SQLite is unaffected — `{"check_same_thread": False}` as before). Re-running the exact same live stop/reconnect experiment after the fix: the failure now takes **~20 seconds, not 10** — libpq/psycopg apply `connect_timeout` **per resolved address**, and `localhost` resolves to both an IPv6 and an IPv4 address, so a dual-stack host pays the timeout twice sequentially. This is still a massive improvement (bounded ~20s vs. unbounded 60s+), but the code comment and this report both state the real, measured behavior rather than claiming an exact "10 second" guarantee. `pool_pre_ping=True` (from the prior branch's AUD-007 fix) was also re-confirmed live in this same experiment: after restarting the stopped container, a query through the SAME engine/pool (not a new engine) transparently recovered without raising a stale-connection error.
- **Codex/Astra:** No — additive engine-configuration bound, no schema/business-logic change, doesn't alter retry semantics (there is no retry here, just a bound on how long a connection ATTEMPT can take).

---

## Additional confirmed findings (documented only, no fix — lower priority or genuinely out of scope)

### HARD-012 — Skill-name matching drops the NFKC normalization step candidate-profile persistence already applies
- **Priority:** P3 · **Verified/Theoretical:** THEORETICAL (static proof from code read, not exercised by a new test — same underlying mechanism as HARD-009, not independently re-tested to avoid redundant test weight)
- **File/function:** `app/agents/job_scorer.py::normalize_skill` (`.strip().casefold()`, no `unicodedata.normalize`) vs. `app/models/candidate_profile.py::normalize_text_identity` (NFKC + casefold), used respectively by `app/agents/candidate_job_matcher.py` for matching and by `app/db/candidate_profile_repository.py` for persistence/dedup of the same conceptual skill-name identity.
- **Impact:** a candidate skill and a job's required-skill text that are visually identical but differ in Unicode composition form would not be recognized as the same skill during matching, silently undercounting matched requirements — same root cause as HARD-009, different subsystem (skill matching, not job dedup).
- **Fix status:** DOCUMENTED, NOT FIXED — changing `normalize_skill`'s normalization could shift scoring/matching outcomes for real candidate data; out of this session's safe-fix scope ("do not broaden normalization unless proven safe").
- **Codex/Astra:** Codex: YES. Astra: No.

### HARD-013 — Two functions named `normalize_company_name` with materially different normalization rules
- **Priority:** P3 · **Verified/Theoretical:** THEORETICAL
- **File/function:** `app/db/repositories.py:375-391` (NFKC + casefold, no suffix-stripping) vs. `app/services/email_matching.py:343-354` (casefold + legal-form-suffix stripping, no NFKC)
- **Impact:** currently used in disjoint subsystems (company research identity vs. Gmail-thread-to-job matching) so no direct collision has been observed, but the identical function name with materially different behavior is a latent footgun if a future feature ever cross-references company identity between the two domains.
- **Fix status:** DOCUMENTED, NOT FIXED — a rename/consolidation decision belongs to a deliberate architecture review, not an adversarial-testing pass.
- **Codex/Astra:** No (not urgent enough to warrant a dedicated review round on its own — noted for the next architecture pass).

### HARD-014 — Company Research's `record_failed_attempt` is a read-modify-write with no CAS
- **Priority:** P3 · **Verified/Theoretical:** THEORETICAL
- **File/function:** `app/db/repositories.py::record_failed_attempt` (`existing.last_attempt_at = now; ...; db.commit()`, no version check)
- **Impact:** two concurrent failed-attempt recorders racing on the same row is a last-write-wins scenario — benign in practice since only attempt-metadata (`last_attempt_at`, `attempt_count`, `last_error`) is touched, never `research_status` or research content itself.
- **Fix status:** DOCUMENTED, NOT FIXED — genuinely low priority, real fix would add a CAS layer for a benign race.
- **Codex/Astra:** No.

### HARD-015 — Rate limiting runs AFTER API-key auth, so invalid-key requests are unlimited
- **Priority:** P2 · **Verified/Theoretical:** VERIFIED (static proof — every one of ~50 routes' `dependencies=[Depends(require_api_key), Depends(enforce_*_rate_limit)]` lists was grepped and confirmed to declare auth first, consistently, with zero exceptions)
- **File/function:** `app/api/routes.py` (all rate-limited route decorators)
- **Impact:** FastAPI resolves a `dependencies=[...]` list sequentially and stops at the first exception — since `require_api_key` always comes first, a request with a wrong/missing `X-API-Key` never reaches the rate limiter at all. An attacker can brute-force the API key at unlimited request rate; only requests that already pass the key check consume rate-limit quota.
- **Fix status:** DOCUMENTED, NOT FIXED — reordering dependencies is a security-relevant behavior change (changes what gets rate-limited and when); explicitly out of this session's safe-fix scope.
- **Codex/Astra:** Codex: YES. Astra: No — API-key brute-forcing is already mitigated by the key's length/entropy being the operator's responsibility (documented in `docs/SECURITY_MODEL.md`), not a send/approval-safety-critical gap.

### HARD-016 — Automation-run lease expiry uses wall-clock time (inherent, not a code defect)
- **Priority:** P3 · **Verified/Theoretical:** THEORETICAL
- **File/function:** `app/db/automation_repository.py::_is_lease_expired`, `AUTOMATION_RUN_LEASE_TTL_SECONDS`
- **Impact:** a large forward host-clock step (NTP correction, VM pause/resume) could make a live lease appear expired early, letting a second process reclaim it while the first is still genuinely running. This is architecturally necessary — the lease must be comparable across process restarts and hosts, which a monotonic clock (reset on restart, not comparable cross-process) cannot provide — and mirrors the identical, deliberate design already used for the Gmail thread lock in the same codebase.
- **Fix status:** DOCUMENTED, NOT FIXED (and not clearly fixable without a fundamentally different locking primitive, e.g. an external coordination service, which is out of scope for this project's single-server target). Recorded so it's a known, accepted tradeoff rather than a silent assumption.
- **Codex/Astra:** No — inherent to wall-clock leases generally, not a fixable code defect.

---

## Phase-by-phase results

### Phase 3 — Resource-exhaustion audit
Full inventory of every module-level mutable collection in `app/` and every DB table's growth/retention behavior. Result: `app/security/rate_limit.py` was the only genuinely unbounded in-memory structure found (HARD-001, fixed). Every other module-level dict/set is a static, hardcoded lookup table built once at import time, never mutated at runtime. `functools.lru_cache` is used exactly once (`get_settings()`, bounded to 1 entry by construction). Of 17 persistent tables inventoried, exactly one (`job_reference_tokens`) has any DELETE logic at all (a delete+reinsert re-sync inside `upsert_job`, not a leak) — every other table grows without retention, by explicit design (audit trail / idempotency-identity tables per their own docstrings, e.g. `application_package_review_revisions`: "must never be cascade-deleted"). Not a new finding — consistent with the prior audit's assessment that this project doesn't yet need retention policies at its current expected scale, but worth flagging for a future capacity-planning pass if usage grows substantially.

### Phase 4 — HTTP input hardening
Full inventory of every POST/PATCH endpoint's Pydantic model bounds. Confirmed: **no application-level or server-level HTTP body size limit exists anywhere** (no Starlette size-limiting middleware, no uvicorn flag, no reverse-proxy layer in `compose.yaml`) — relies entirely on OS/infra defaults. Several fields are genuinely unbounded (`Job.description`, `Job.skills`/`must_have_skills`/`nice_to_have_skills`, `CandidateProfilePatchRequest`'s 6 list fields and their nested free-text fields, `ReviewPackagePatchRequest`'s nested content-patch text fields) — an authenticated caller could submit a multi-megabyte `description` or thousands of list items in one request. Pagination endpoints, by contrast, are uniformly well-bounded (`MAX_LIST_LIMIT=200` and siblings, `Query(..., ge=1, le=<MAX>)` throughout). **Not fixed this session** — per explicit instruction ("Do NOT add arbitrary limits without examining normal use cases"), and because the API is currently only reachable by an authenticated single operator (not multi-tenant), the practical risk is self-inflicted resource use, not an external attack surface. Recorded for a future, deliberately-scoped bounds-setting pass rather than speculative limits added under time pressure.

### Phase 8 — Scheduler crash matrix
All 10 requested crash points traced through the actual lease/CAS code (not simulated blindly): claim-before-commit, claim-then-crash-before-run-creation, run-creation-then-crash-before-lease, crash-mid-heartbeat, crash-before-completion-commit, crash-after-completion-commit, SIGTERM-while-idle (already fixed, HARD-005 fixed the mid-cycle case), SIGKILL-mid-cycle, and DB-disconnect-mid-cycle. Every scenario either recovers cleanly with no data loss (worst case ~30s stall via `reconcile_stale_run_to_failed`'s lease-expiry check) or is explicitly documented as an accepted, by-design tradeoff (e.g. a crash between schedule-claim and run-creation silently skips that slot — "Fail-closed slot claim... accepted crash-between-claim-and-run tradeoff", already in the code's own docstring). No catch-up storm risk exists (confirmed: `next_run_at` is always computed relative to the CURRENT claim moment, never replayed from a stale value). One real gap found and fixed: HARD-005 (SIGTERM absorbed mid-cycle).

### Phase 9 — PostgreSQL failure injection (live, Docker)
All scenarios run against a real, disposable `postgres:16` Docker container (never a shared/production database):
- **Stop DB while a live SQLAlchemy engine is running, restart, query again through the same engine/pool:** before the connect-timeout fix, the query during the outage hung 60+ seconds; after both fixes (this session's `connect_timeout` + the prior branch's `pool_pre_ping`), the outage query fails cleanly in ~20s and the SAME pool transparently recovers post-restart with no stale-connection error (HARD-011).
- **Wrong DB password:** `alembic current` fails fast, clean `OperationalError`, exit code 1.
- **Wrong hostname:** fails fast via DNS resolution failure, exit code 1.
- **Schema at an older migration, then upgrade to head:** downgraded 3 revisions live, then upgraded back — round-tripped cleanly to `c7d3f9a1e5b8`.
- **Schema already at head, upgrade again (no-op):** exit code 0, no error, no spurious changes.
- **Empty/fresh database → head:** a genuinely empty database migrated cleanly through all 29 migrations to `c7d3f9a1e5b8`, exit code 0.
- **`/health` during a real DB outage:** confirmed live (not just re-asserted from the prior audit) — `GET /api/v1/health` returns `200` while the DB is stopped; a real DB-touching route (`GET /api/v1/jobs`) hangs (pre-fix) or fails cleanly after ~20s (post-fix) instead.
- **`/health` vs `/ready` split:** evaluated per the task brief's suggested design (`/health` = process liveness, `/ready` = dependency readiness). **Not implemented** — this is a public API surface addition, and per this session's "no public API breaking change" boundary for unsupervised fixes, it's recorded as a recommendation for Codex-reviewed follow-up (see `docs/TECHNICAL_DEBT.md` AUD-002) rather than added unilaterally, even though it's additive.

### Phase 10 — Configuration fuzzing
Full field-by-field review of `Settings`. Found and fixed the three fields with genuinely pathological unvalidated values (HARD-006). Every other numeric/bounded field already has an explicit `Field(ge=.../le=...)` constraint or a cross-field `model_validator` (spot-checked: `gmail_imap_port`, `gmail_lookback_days`, `automation_scheduler_interval_seconds`, `automation_shortlist_min_match_score`, `telegram_daily_digest_hour`, and 8 others — all correctly bounded). `telegram_daily_digest_timezone` accepts any string at the `Settings` level (only validated later, inside `app.scheduler.main()`'s `validate_scheduler_settings` call) — a pure-API-server-only deployment never validates it at all; recorded as a defense-in-depth gap, not exploitable (the standalone scheduler worker still fails closed correctly when it does start), not fixed this session.

### Phase 11 — Time / timezone / DST
No issues found — CONFIRMED-SAFE. `next_run_at` scheduling uses pure UTC arithmetic (not DST-affected). The daily digest's local-hour gate uses a correct DST-aware `astimezone(ZoneInfo(...))` conversion with a "not-before" inequality (survives a spring-forward hour-skip) and DB-CAS-based once-per-calendar-day idempotency (survives a fall-back repeated hour without double-sending). Follow-up eligibility timing is confirmed to use only provider-trusted IMAP `INTERNALDATE`, never the sender-controlled `Date` header. Every deadline/timeout mechanism outside the (frozen, out-of-scope) `imap_deadline.py` was grepped and confirmed monotonic-based (`rate_limit.py`, `gmail_repository.py`'s thread-lock wait, `smtp.py`'s watchdog via `threading.Event.wait`) — no wall-clock-diffed duration measurement found anywhere else in the codebase. Lease expiry is wall-clock by architectural necessity, documented as HARD-016 (not a defect).

### Phase 12 — Unicode / identity adversarial testing
Two real, verified collision-miss gaps found (HARD-009, HARD-012) plus one latent footgun (HARD-013) — all documented, none fixed (normalization changes affect matching/dedup outcomes for real data, explicitly out of this session's safe-fix scope). Confirmed-safe: Message-ID comparison (correctly byte-for-byte, case-sensitive, per RFC 5322 opaque-token semantics), account-key normalization (consistent `.strip().casefold()` between `Settings` and the provider layer), and every `.lower()` (vs. `.casefold()`) use spot-checked is on ASCII-only protocol tokens (DNS hostnames, MIME headers, URL schemes) where `.lower()` is actually correct, not a bug.

### Phase 13 — Privacy / secret leakage scan
Two real leaks found and fixed (HARD-002, HARD-003). Every other `logger.exception`/`exc_info=True`/`str(exc)` site in `app/` was inventoried and confirmed to either already log only `type(exc).__name__` (the established convention) or raise against project-defined domain exceptions with fixed/derived messages (never a raw upstream exception). `app/core/logging.py` already deliberately suppresses `httpx`/`httpcore` INFO logging specifically to avoid leaking the Telegram bot token embedded in request URLs (confirmed present, unchanged). `app/services/telegram_bot.py` echoes `str(exc)` back to the Telegram chat operator (the single authenticated bot owner, not a multi-tenant surface) — noted as lower priority, not fixed.

### Phase 14 — Auth / header edge cases
Confirmed via direct code read + Starlette internals: a duplicated `X-API-Key` header resolves to the FIRST occurrence (not last, not concatenated). Empty/missing key fails closed either way. No length cap on the key itself (comparison is safe, non-crashing, just non-constant-time — already tracked as AUD-006 in the prior audit). No accidental `X-Forwarded-For`/`Forwarded`/`X-Real-IP` trust exists anywhere (confirmed zero matches across `app/`), consistent with the prior audit's AUD-003/AUD-004 findings. One new, real finding: rate limiting runs strictly after auth in every route's dependency list, so invalid-key requests are never rate-limited (HARD-015, documented, not fixed).

### Phase 15 — Migration resilience
All scenarios covered live against the same disposable PostgreSQL container as Phase 9 (see that section for the shared results): empty→head, older→head, head→no-op, failed-connection→non-zero exit, invalid-credentials→non-zero exit — all behave correctly. **Static downgrade-function inspection** (AST scan of all 29 migration files' `downgrade()` functions): 28 of 29 migrations call `drop_table`/`drop_column` in their downgrade path. This is **expected and by design**, not a defect — every one of these migrations is additive (a new table or column), so downgrading inherently discards whatever data was written to that new schema element after it was added. This project does not claim (and this report does not recommend) that downgrades are lossless; it's standard Alembic practice for additive schema evolution. Documented here honestly per the task's explicit request, not flagged as a finding requiring a fix.

### Phase 16 — Dependency graph hardening
Full AST-based import-graph scan of all 105 `app/` modules: **zero real cycles found** (confirms the prior audit's AUD-012 was a package-level layering smell, not an actual cycle — same conclusion, now independently re-verified with a fresh script rather than trusted from the prior report). HARD-004 (fixed) implements the pre-authorized `is_configured` relocation. AUD-013 (core→providers), AUD-014 (db→services/agents), AUD-015 (providers→ORM) were all re-confirmed present, still safe (every db→services import is a pure, side-effect-free dataclass/constant/function with no import path back down into db/api/security), and left as analysis-only per the explicit instruction that AUD-014-class inversions "should remain analysis-only unless extraction is extremely small and obvious" (they involve multiple symbols across 3 files, not a single trivial helper like `is_configured` was).

### Phase 18 — Test quality audit
Identified genuine `time.sleep`-based timing sensitivity in the lease/deadline/concurrency test suites (`test_automation_lease.py`, `test_gmail_repository.py`, `test_follow_up_send_service.py`, `test_providers_email_smtp.py`, `test_imap_deadline.py`) — a real, pre-existing CI-flakiness risk class, not introduced this session, not fixed here (rewriting an established, working test suite's synchronization strategy is out of scope for an adversarial-testing pass). No evidence found of order-dependent tests, mock-checking-mocks, or unverified "no exception raised" assertions after inspecting the highest-risk candidates directly. The one genuine, high-value missing-test gap identified (real-PostgreSQL coverage for the send-approval CAS gate) is now fixed (HARD-010).

---

## Summary table

| ID | Priority | Verified/Theoretical | Fix status | Codex | Astra |
|---|---|---|---|---|---|
| HARD-001 | P2 | VERIFIED | FIXED | No | No |
| HARD-002 | P2 | VERIFIED | FIXED | No | No |
| HARD-003 | P2 | VERIFIED | FIXED | No | No |
| HARD-004 | P3 | VERIFIED | FIXED | No | No |
| HARD-005 | P2 | VERIFIED | FIXED | No | No |
| HARD-006 | P1 | VERIFIED | FIXED | No | No |
| HARD-007 | P2 | VERIFIED | DOCUMENTED | YES | No |
| HARD-008 | P1 | VERIFIED | DOCUMENTED | YES | YES |
| HARD-009 | P2 | VERIFIED | DOCUMENTED | YES | No |
| HARD-010 | P2 | VERIFIED | FIXED (new test) | No | No |
| HARD-011 | P1 | VERIFIED | FIXED | No | No |
| HARD-012 | P3 | THEORETICAL | DOCUMENTED | YES | No |
| HARD-013 | P3 | THEORETICAL | DOCUMENTED | No | No |
| HARD-014 | P3 | THEORETICAL | DOCUMENTED | No | No |
| HARD-015 | P2 | VERIFIED | DOCUMENTED | YES | No |
| HARD-016 | P3 | THEORETICAL | DOCUMENTED (inherent) | No | No |

**P0 findings: 0. P1 findings: 3 (HARD-006, HARD-008, HARD-011 — 2 of 3 fixed, HARD-008 deliberately deferred). P2 findings: 8. P3 findings: 4.**
