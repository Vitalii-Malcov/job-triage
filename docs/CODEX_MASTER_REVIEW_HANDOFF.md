# Codex Master Independent-Review Handoff — Full Accumulated Delta

**BASE:** `23c8cb0a5b055d940bd155f8adb6900d07610079` (merge: Astra R4C PostgreSQL CI Alembic migration gate)
**FINAL HEAD:** `8dff9fe022b9e86acfb3128c726f1fb1814225cb` (branch `hardening/api-boundaries-r1`, frozen, unmerged)

This is the single consolidated handoff covering **every** commit across five
sequential, independently-frozen work waves that accumulated on top of one
another since `23c8cb0`. It supersedes nothing — `docs/ADVERSARIAL_HARDENING_REPORT.md`,
`docs/CODEX_PRODUCTION_READINESS_HANDOFF.md`, `docs/PRODUCTION_READINESS_AUDIT.md`,
`docs/API_BOUNDARY_HARDENING_REPORT.md`, `docs/API_RETRY_SEMANTICS.md`, and
`docs/DATA_RETENTION_CAPACITY.md` remain the full-narrative sources of truth
for their respective waves. This document is the compact index a reviewer
should start from, plus the delta-spanning analysis (state-machine safety,
outbound-safety preservation, etc.) that no single per-wave document could
provide on its own since each wave only diffs against its own immediate
predecessor.

**Status: FROZEN.** No production code, test code, or existing documentation
was modified to produce this handoff. This file is a pure addition.

---

## Internal checkpoints (for orientation)

| Label | Commit | Marks the end of |
|---|---|---|
| BASE | `23c8cb0a5b055d940bd155f8adb6900d07610079` | Astra R4C (pre-existing, out of this delta's scope) |
| R5A | `579e150b51cd5eb9dcbf222a7d70ef11c9663168` | Astra R5A inbound-email-correctness wave (FINAL-001/003/004, 3 rounds) |
| cleanup R1 | `f88b84abd38a383a42c62d30e6306e1fcec76513` | `refactor/project-cleanup-r1` (7 behavior-preserving refactors) |
| production readiness | `9b33a72e78cfe771e468196e6e656ad1dc48af00` | `chore/production-readiness-r1` (AUD-001..020, Docker, docs) |
| adversarial | `f9181a194a9a2e2e5a36e779c707a983e1b85c2e` | `hardening/adversarial-r1` (HARD-001..016) |
| API boundaries | `8dff9fe022b9e86acfb3128c726f1fb1814225cb` | `hardening/api-boundaries-r1` (BOUND-001..016) — **FINAL HEAD** |

Each later wave branched from the previous wave's frozen HEAD and never
modified the prior wave's branch.

---

## 1. Executive summary

**R5A** (`23c8cb0`..`579e150`, 3 commits) fixed three independent Astra
findings scoped entirely to inbound email collection: an uncaught
`imaplib.IMAP4.abort` that could leak raw socket-error text into logs
(FINAL-001); an unbounded DNS-resolution phase in the shared IMAP deadline
watchdog, fixed twice — first with a background thread (rejected by Codex
re-review as not actually bounding the worker's lifetime), then with a
genuinely killable child **process** (FINAL-003); and a starvation bug where
permanently-unparseable Gmail messages were never distinguished from
transient failures, letting them consume every future sync's entire budget
forever (FINAL-004, new `GmailPermanentSkipRecord` table + migration
`c7d3f9a1e5b8`). This is the **only wave in the whole delta that adds a
database migration.**

**Cleanup R1** (`2441aa3`..`f88b84a`, 7 commits) is pure refactoring:
consolidating duplicated UTC-normalization, MIME-decoding, truncate-ellipsis,
subject-trust, outbound-letter-boilerplate, and telegram-int-parsing logic
into shared helpers, plus restructuring the rate limiter's 14 independent
bucket-dict instances into one reusable `_RateLimiter` class. No new
behavior anywhere in this wave — each commit claims byte-for-byte behavior
preservation, backed by the pre-existing test suite passing unchanged.

**Production readiness** (`e11c799`..`9b33a72`, 6 commits) is the first
wave with genuine, if narrow, behavior change: `pool_pre_ping=True` on the
DB engine (AUD-007) and a SIGTERM handler for the scheduler (AUD-008) — both
additive, both live-verified inside a real Docker container. The rest of
this wave is exclusively new artifacts: `Dockerfile`, `compose.yaml`,
entrypoint script, a new CI `docker` validation job, and 12 new/rewritten
documentation files. `docs/PRODUCTION_READINESS_AUDIT.md` (AUD-001..020) is
this wave's own findings inventory — 2 fixed, 18 documented/deferred.

**Adversarial hardening** (`2801d31`..`f9181a1`, 8 commits) is a dedicated
adversarial-testing pass (branch `hardening/adversarial-r1`) producing
HARD-001..016. 6 fixed (rate-limiter bucket eviction, 2 privacy-leak sites,
an `is_configured` import-cycle relocation, a scheduler SIGTERM-swallowing
bug, 3 config validation bounds — see the findings matrix for the precise
split), 1 test-coverage-only addition (HARD-010, a new real-PostgreSQL CAS
race test), 9 documented-not-fixed (including HARD-007, HARD-008, HARD-009 —
all three explicitly reserved for Codex, HARD-008 also for Astra).

**API boundary hardening** (`92582cb`..`8dff9fe`, this document's own
predecessor wave, branch `hardening/api-boundaries-r1`) targeted attack
surfaces the adversarial pass hadn't deeply exercised: public API validation
boundaries, resource bounds, state machines, and a genuinely independent
privacy second-pass (which found 2 NEW leak instances the adversarial pass's
own privacy phase had missed). Produced BOUND-001..016 — 5 fixed (all narrow,
regression-tested, Safe-Fix-Policy-compliant), the rest documented for Codex
or confirmed-safe. Full detail already in `docs/API_BOUNDARY_HARDENING_REPORT.md`
— not repeated here beyond the findings-matrix summary in §6.

---

## 2. Exact commit range

31 commits, `BASE..HEAD`, chronological order, grouped by milestone:

### R5A (3 commits)
```
a9e32aa fix: Astra R5A inbound email correctness (FINAL-001/003/004)
da724f6 fix: Astra R5A Codex re-review correction (FINAL-003 process-based DNS, FINAL-004 classification narrowing, CI head)
579e150 fix: Astra R5A Codex targeted re-review correction (FINAL-003 guaranteed-dead postcondition)
```

### Cleanup R1 (7 commits)
```
2441aa3 refactor: consolidate persistence-layer UTC datetime normalization
c969147 refactor: consolidate MIME part decoding for inbound email
adfc28a refactor: extract shared truncate-with-ellipsis into app/utils/text.py
3978169 refactor: share subject-bound/trusted-source helpers via existing response_draft -> follow_up dependency
556349a refactor: extract shared outbound-letter boilerplate for the two deterministic generators
13516f1 refactor: consolidate telegram bot int-argument parsing
f88b84a refactor: consolidate rate-limit buckets into a per-limiter _RateLimiter class
```

### Production readiness (6 commits)
```
e11c799 fix: harden scheduler shutdown and DB engine resilience (AUD-007/AUD-008)
2b16903 docs: add production readiness audit, architecture map, and refactoring backlog
893eaa9 chore: add Docker deployment support
a5e2099 ci: add Docker build/config validation gate
9519182 docs: restructure README and add security, runbook, interview, and portfolio guides
9b33a72 docs: add Codex production-readiness handoff for the base..HEAD delta
```

### Adversarial hardening (8 commits)
```
2801d31 refactor: remove providers/collectors import-cycle dependency, sanitize Bundesagentur error logging
c89efec fix: harden bounded in-memory state, scheduler shutdown, and DB connection resilience
471d4b4 fix: strengthen configuration validation and sanitize company-research error logging
8d8ab7e test: add adversarial concurrency, Unicode-identity, and outbound-ambiguity regression coverage
c626920 docs: add adversarial hardening report
e3fce98 fix: complete HARD-003 log sanitization (company_research.py exc_info leak)
472110d test: strengthen HARD-005/HARD-007 evidence with deeper and real-PostgreSQL tests
f9181a1 docs: correct and strengthen adversarial hardening report claims
```

### API boundaries (7 commits)
```
92582cb fix: BOUND-001 pagination offset overflow + body/cardinality/pagination hardening tests
f32f3ff test: BOUND-002 auth-before-rate-limit ordering evidence (HARD-015 deep dive)
a75b49f test: BOUND-010 live Europe/Berlin DST transition tests for daily digest gate
8ae7746 fix: BOUND-011/011b privacy second-pass -- 2 new leak instances
b7e48e1 test: BOUND-015 malformed email adversarial corpus
b0d374b fix: BOUND-006 review-package field bounds + BOUND-007 stale comment fix
8dff9fe docs: add API retry, capacity, and boundary-hardening analysis; finalize BOUND IDs
```

---

## 3. Application-code changes

Only entries below touch `app/`. Refactor-wave entries are marked
**[behavior-preserving, claimed]** — Codex should independently judge
whether the claim holds, not accept it at face value.

### R5A

| File | Commit | Reason | Behavioral impact | Risk | Tests |
|---|---|---|---|---|---|
| `app/collectors/xing_email.py` | a9e32aa | `imaplib.IMAP4.abort` is not an `OSError` subclass but is what a deadline-forced socket close surfaces as | 3 call sites (`_fetch_sync_body` per-UID loop, `_fetch_sync` outer handler, `_read_message_id_header` pre-check) now catch `abort` alongside `OSError`; deadline-caused aborts preserve already-completed batches, genuine aborts sanitize to `XingConnectionError` with `type(exc).__name__` only | Medium (fixes an uncaught-exception path that could leak raw socket text via FastAPI's default handler) | `tests/test_collectors_xing_email.py` (+123 lines, 2 new abort-preservation/sanitization tests) |
| `app/providers/email/imap_deadline.py` | a9e32aa, da724f6, 579e150 | Bound the previously-unbounded DNS+connect phase of IMAP session establishment | v1 (a9e32aa): background daemon thread, lock-guarded phase handoff. v2 (da724f6): **replaced entirely** — DNS resolution moved to an isolated `multiprocessing` child process (spawn context), genuinely killable via OS signal; TCP connect happens back in the parent, registered with the session deadline before `connect()`. v3 (579e150): post-kill cleanup changed from a bounded `join(timeout=5.0)` to an untimed `join()` after escalating to `kill()`, since SIGKILL/TerminateProcess cannot be declined | High — this is the component Codex twice rejected as not actually bounding worker lifetime before it was accepted; the underlying primitive (thread → process) changed, not just tuning | `tests/test_imap_deadline.py` (158 → 387 → +153 lines across the 3 rounds; process-death confirmed via `multiprocessing.active_children()`, not just timing) |
| `app/providers/email/imap.py` | a9e32aa, da724f6 | FINAL-004: classify each `_fetch_one` skip as PERMANENT (deterministic given the message's own bytes) vs. transient | v1: broad `except Exception` around both `email.message_from_bytes` and this project's own `_parse_message` classified as PERMANENT together. v2 (da724f6): **narrowed** — only a failure in `email.message_from_bytes` itself is PERMANENT; any exception from `_parse_message` (our own code) is now transient/retryable, so an internal bug can never durably mark a message as permanently unfetchable | Medium-high — v1 had a real risk (a bug in our own parsing code could permanently blacklist a recoverable message); v2 closes it | `tests/test_providers_email_imap.py` (+99, then +32 lines; new test monkeypatches `_parse_message` to raise, proving it's never recorded PERMANENT) |
| `app/db/gmail_repository.py`, `app/db/models.py` | a9e32aa | New `GmailPermanentSkipRecord` table; `get_known_uids` now unions persisted messages with permanent skips | **Only schema-affecting change in the entire delta** — new migration `c7d3f9a1e5b8`, chained on prior head `b4f6a1c9e7d2` | Medium — new table, additive only, no existing table altered | `tests/test_gmail_repository.py` (+95 lines), `tests/test_migrations.py` (+79 lines) |
| `app/services/gmail_inbox.py` | a9e32aa | Wires `record_permanent_skips` into `sync`, best-effort/isolated per-row exactly like `upsert_message` | Low — additive, mirrors an existing isolation pattern | Low | `tests/test_gmail_inbox_service.py` (+80 lines) |
| `app/providers/email/base.py` | a9e32aa | Shared classification constants/types for the new PERMANENT/transient distinction | Low — pure additive types | Low | covered by the imap.py/gmail_repository.py tests above |

### Cleanup R1 — **[behavior-preserving, claimed]**

| File | Commit | Reason |
|---|---|---|
| `app/db/automation_repository.py`, `app/db/automation_schedule_repository.py`, `app/db/follow_up_repository.py`, `app/services/company_research.py`, `app/db/datetime_utils.py` (new) | 2441aa3 | Consolidate 4 duplicated UTC-normalization snippets into one shared helper |
| `app/collectors/xing_email.py`, `app/providers/email/imap.py`, `app/providers/email/mime_utils.py` (new) | c969147 | Consolidate duplicated MIME part decoding |
| `app/agents/email_classifier.py`, `app/services/email_matching.py`, `app/utils/text.py` (new) | adfc28a | Extract shared truncate-with-ellipsis helper |
| `app/services/follow_up.py`, `app/services/response_draft.py` | 3978169 | Share subject-bound/trusted-source helpers via the existing `response_draft → follow_up` dependency direction (no new dependency introduced) |
| `app/agents/follow_up_generator.py`, `app/agents/response_draft_generator.py`, `app/agents/letter_content.py` (new) | 556349a | Extract shared outbound-letter boilerplate for the two deterministic generators |
| `app/services/telegram_bot.py` | 13516f1 | Consolidate int-argument parsing |
| `app/security/rate_limit.py` | f88b84a | Largest single-file diff in this wave (459 → 270 lines, -189 net) — 14 independent bucket-dict instances consolidated into one reusable `_RateLimiter` class |

Each cleanup commit's stated verification (per commit message, not
independently re-run for this handoff) was: targeted test file(s) for the
touched module plus the full suite passing unchanged. **Codex should verify
the rate-limit consolidation (f88b84a) most carefully of this group** — it
is the largest diff and the one most plausible to have subtly changed
bucket-key/eviction/threshold semantics; see §5.

### Production readiness

| File | Commit | Reason | Behavioral impact | Risk | Tests |
|---|---|---|---|---|---|
| `app/db/session.py` | e11c799 | AUD-007: no `pool_pre_ping` meant a stale pooled connection (DB restart, LB idle-drop) surfaced as an unhandled `OperationalError` instead of transparent replacement | Added `pool_pre_ping=True` unconditionally (harmless no-op for SQLite) | Low — additive, well-understood SQLAlchemy idiom | Full suite green unchanged; live Docker stop/restart experiment (see §9) |
| `app/scheduler.py` | e11c799 | AUD-008: no SIGTERM handler — `docker stop` terminated the process with no graceful-shutdown log line | New `_ShutdownRequested(BaseException)` class, `_handle_sigterm`, `signal.signal(signal.SIGTERM, _handle_sigterm)` registered in `main()`, new `except _ShutdownRequested:` branch alongside the existing `except KeyboardInterrupt:` | Low-medium — additive signal handling; **later found to have a real gap** (HARD-005, see below) that this same wave did not yet catch | Live-verified inside a real Linux container (`docker compose stop scheduler`, exit 0, correct log line) |

### Adversarial hardening

| File | Commit | Finding | Behavioral impact | Risk | Tests |
|---|---|---|---|---|---|
| `app/security/rate_limit.py` | c89efec | HARD-001: bucket dict keys for expired hosts never evicted | Added a bounded, amortized sweep — once a limiter's dict exceeds `_SWEEP_THRESHOLD=512` distinct hosts, the crossing request pays a one-time eviction cost; below threshold, byte-identical to before | Low | `tests/test_security.py::TestRateLimiterBucketEviction` (3 tests) |
| `app/api/routes.py` | c89efec / 2801d31 | HARD-002: Bundesagentur collector-run failure logged full traceback via `exc_info=True` | Changed to `logger.warning(..., error_type=type(exc).__name__)`, mirroring the already-fixed XING sibling | Low (log-only) | `tests/test_collector_endpoint.py::TestRunBundesagenturCollector` |
| `app/services/company_research.py` | 471d4b4, e3fce98 | HARD-003 (2 independent halves): `last_error` persisted the raw exception message (API/DB half, 471d4b4); a separate `logger.warning(..., exc_info=True)` in the same except block was missed on the first pass and fixed in a dedicated follow-up commit (e3fce98) | `error_message` now uses `type(exc).__name__`; log line no longer carries `exc_info` | Low (log/data-shape only, no schema change) | `tests/test_company_research_service.py` (2 dedicated tests, one per half; the LOG-half test confirmed FAILING pre-fix) |
| `app/collectors/base.py`, `app/utils/config_flags.py` (new), 8 importers | 2801d31 | HARD-004: `is_configured` created a providers↔collectors import-cycle smell (AUD-012 pre-authorized this exact fix) | Pure relocation to a new leaf module; 8 known importers updated | Low — zero-behavior-change relocation, pre-authorized | 293 tests across every touched module, unchanged |
| `app/scheduler.py` | c89efec | HARD-005: `_ShutdownRequested` subclassed `Exception`, not `BaseException` — a SIGTERM landing inside `_poll_loop`'s per-tick `except Exception:` block would be silently swallowed and misreported as a generic tick error | Changed to subclass `BaseException` directly, mirroring `KeyboardInterrupt`/`SystemExit` | Medium — this is a real correction to production readiness's own AUD-008 fix, found one wave later | `tests/test_scheduler_poll_loop.py::TestShutdownRequestedPropagatesThroughTickLevelExceptionHandling` (2 tests) + a deeper real-`run_automation_cycle`+real-heartbeat-thread test added later (472110d) |
| `app/core/config.py` | 471d4b4 | HARD-006: `rate_limit_requests`/`rate_limit_window_seconds`/`xing_lookback_days` accepted pathological values (0, negative) with no error, causing total self-lockout or a silent fail-open rate limiter | Added `Field(ge=1)` to the two rate-limit fields, `Field(ge=1, le=1095)` to `xing_lookback_days` | Low-medium (startup-time validation only, fails closed earlier) | `tests/test_config.py` (8 new tests) |
| `app/db/response_draft_approval_repository.py`, CI wiring | 8d8ab7e | HARD-010: no real-PostgreSQL coverage existed for the send-approval CAS gate (`claim_send_attempt`) | **No application code changed** — pure test-coverage addition | Low | New `tests/integration/test_response_draft_send_postgres_concurrency.py`, wired into `scheduler-postgres` CI job |
| `app/db/session.py` | 471d4b4 | HARD-011: no `connect_timeout` — a DB outage hung every DB-touching request indefinitely instead of failing fast | `_connect_args_for` now returns `{"connect_timeout": 10}` for non-SQLite URLs | Medium — changes real-world outage behavior (hang → ~20s bounded failure); dialect-exclusivity (Postgres-only project) explicitly re-verified before applying unconditionally | `tests/test_db_session_connect_args.py` (3 tests) + a live Docker stop/reconnect experiment |

Not fixed in this wave (documented only) — see §7 for the full deferred list;
notably **no application code changed** for HARD-007, HARD-008, HARD-009,
HARD-012 through HARD-016.

### API boundaries

| File | Commit | Finding | Behavioral impact | Risk | Tests |
|---|---|---|---|---|---|
| `app/api/routes.py` | 92582cb | BOUND-001: `offset: int = Query(default=0, ge=0)` had no upper bound; `offset=2**63` crashed SQLite's driver with an uncaught `OverflowError` (raw 500) | Added `MAX_OFFSET = 2**31-1`, applied `le=MAX_OFFSET` to all 7 `offset` params | Low-medium — a real, live-reproduced crash-to-clean-422 fix, uniformly applied | `tests/test_api_boundary_hardening.py::TestPaginationAbuse` |
| `app/services/follow_up_send.py` | 8ae7746 | BOUND-011: `_ThreadLockHeartbeat._run`'s lock-renewal-failure log used `exc_info=True` — 3rd instance of the HARD-002/003 leak-pattern class, missed when the sibling `_RunLeaseHeartbeat` was fixed | Changed to `type(exc).__name__`-only logging | Low (log-only) | `tests/test_follow_up_send_service.py::TestLeaseRenewalHeartbeat` (new test, confirmed failing pre-fix) |
| `app/collectors/bundesagentur.py`, `app/api/routes.py` (comment only) | 8ae7746 | BOUND-011b: a non-JSON upstream response's raw body snippet (`response.text[:100]`) was embedded directly into `BundesagenturAPIError`'s message, which reaches the API client via `routes.py`'s `detail=f"...: {exc}"` pattern | Snippet now logged server-side only; client-visible exception message is snippet-free | Low-medium (closes a real client-visible upstream-body leak) | `tests/test_collectors_bundesagentur.py` (new test) |
| `app/models/review_package.py` | b0d374b | BOUND-006: `expected_review_version` lacked `Field(ge=1)` unlike sibling `CandidateProfilePatchRequest.expected_profile_version`; `edit_note`/`decision_note` unbounded unlike sibling `.note` fields (`max_length=2000`) | Added the matching bounds to all 3 `expected_review_version` fields and all 3 note fields | Low (schema-boundary tightening only, 0/negative was already rejected downstream by a 409 CAS check) | `tests/test_review_package_endpoints.py` (4 new tests) |
| `app/db/response_draft_repository.py` | b0d374b | BOUND-007: stale comment claiming the module's 20/100 limits "mirror" `GMAIL_ANALYSES_*` (actually 50/200) | Comment-only correction | None (no code change) | N/A |

---

## 4. Infra / Docker / CI changes

All in the **production readiness** wave (`e11c799`..`9b33a72`), unchanged
by every later wave.

- **`Dockerfile`** (new): single-stage, `python:3.13-slim`, `pip install ".[postgres]"` (non-editable), non-root `appuser` (uid 1000), `COPY --chmod=755` for the entrypoint, `ENTRYPOINT ["/entrypoint.sh"]`, default `CMD` runs uvicorn `--workers 1`.
- **`compose.yaml`** (new): 3 services (`db`, `web`, `scheduler`); `scheduler` is `profiles: ["scheduler"]`-gated and uses `restart: on-failure` (changed from an initial `unless-stopped` after the smoke test surfaced a restart-loop when the scheduler is disabled by config — see §9's Docker evidence table); `web`/`db` use `restart: unless-stopped`; `depends_on: db: condition: service_healthy`; named volume `pgdata`; `ALEMBIC_AUTO_UPGRADE: "false"` set explicitly in both `web` and `scheduler` environments (migrations must be run explicitly, by design).
- **`docker/entrypoint.sh`** (new): 9 lines, `set -e; exec "$@"` — no wait-loop, no retry logic, no swallowed exit codes.
- **`.dockerignore`**, **`docker/README.md`** (new).
- **`.env.example`** (modified): appended `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`/`WEB_PORT` placeholders only — no existing line changed.
- **`.github/workflows/ci.yml`**: one new job (`docker`) appended in the production-readiness wave (builds the image, writes a throwaway `.env` removed via `if: always()`, runs `docker compose config` for both profiles — does not start containers or run migrations); the two pre-existing jobs (`test`, `scheduler-postgres`) were byte-for-byte unchanged by that wave. **R5A's `da724f6` later made a real, necessary correction**: both the CI Alembic-head gate and `tests/test_ci_postgres_migration_gate.py` had hardcoded the pre-R5A head `b4f6a1c9e7d2`, but R5A's own migration (`c7d3f9a1e5b8`) had already moved the real head forward — this was a genuine gate-goes-stale bug caught by Codex re-review, not a hypothetical.
- **Migration strategy** (unchanged across every wave): `ALEMBIC_AUTO_UPGRADE=false` in production compose environments — migrations are never auto-run on container start; an operator runs `alembic upgrade head` explicitly. R5A's `c7d3f9a1e5b8` is the only migration added anywhere in this entire delta; no other wave touches `alembic/`.
- **Scheduler deployment strategy** (unchanged): the scheduler runs as a separate, single, profile-gated Compose service — never inside the same process as the web workers, and never more than one instance (lease/CAS in `app/db/automation_repository.py` enforces this at the DB level regardless, but the deployment topology itself also only ever starts one).

Full Docker smoke-test command-by-command evidence (all against a real,
disposable local `docker compose v5.4.0` stack, no production credentials)
is in `docs/CODEX_PRODUCTION_READINESS_HANDOFF.md` §4 — not reproduced here.

---

## 5. High-risk Codex review targets

In priority order:

1. **HARD-008 — `response_draft_send.py` stranded `PENDING` / SMTP send ambiguity.** `app/services/response_draft_send.py::send_response_draft` (around line 300) and `mark_send_sent` (`app/db/response_draft_approval_repository.py`). If the provider's `send()` succeeds (irreversible external effect already happened) and `mark_send_sent`'s commit then fails (process kill, DB drop at that exact instant), the send record is left `PENDING` forever — a second attempt raises `ResponseDraftSendInProgressError` again, with zero new provider calls (never double-sends, but never un-sticks either, and is invisible to any operator dashboard). The sibling `follow_up_send.py` has a `send_attempted` CAS flag specifically for this scenario; `response_draft_send.py` has no equivalent. **DOCUMENTED, NOT FIXED** — the obvious fix touches the outbound-send state machine directly, explicitly out of every wave's safe-fix scope. **Codex: YES. Astra: YES.**

2. **HARD-007 — `upsert_job` concurrent-INSERT race has no `IntegrityError` handling.** `app/db/repositories.py::_finalize_job_write` (line 125-143, `db.commit()` at line 141) is called by `upsert_job`'s new-record path (line 146 onward) with no `try/except IntegrityError`, unlike every sibling insert-race site in the project (`get_or_create_schedule`, `create_approval`, `claim_send_attempt`, `upsert_message`). Real-PostgreSQL evidence (added `472110d`, `tests/integration/test_upsert_job_postgres_concurrency.py`) confirms the loser gets `IntegrityError` wrapping `psycopg`'s `UniqueViolation` on `uq_jobs_fingerprint`, AND that PostgreSQL poisons the losing transaction until an explicit `rollback()` — a caller reusing the same `Session` without rolling back first would see every subsequent statement fail too. `JobRecord.fingerprint` has a DB-level UNIQUE constraint, so data integrity itself is never violated — this is a robustness/UX gap (unhandled 500-class failure), not a safety gap. **DOCUMENTED, NOT FIXED. Codex: YES.**

3. **HARD-009 — Unicode NFC/NFD fingerprint identity.** `app/db/repositories.py::_fingerprint` does `.strip().casefold()` only, no `unicodedata.normalize`. The same visible company name in NFC vs. NFD form produces two different fingerprints (live-reproduced: `tests/test_repository.py::test_fingerprint_nfc_vs_nfd_same_visible_company_name_does_not_dedup`), risking a duplicate `JobRecord`. **DOCUMENTED, NOT FIXED** per this project's own established convention (referenced in `CLAUDE.md`) that fingerprint changes require a live-data-driven audit first, not a speculative one-line normalization add. **Codex: YES** (live-data audit as a prerequisite). **Astra: No.**

4. **HARD-005 — SIGTERM → `BaseException` scheduler shutdown model.** `app/scheduler.py::_ShutdownRequested` (line 63) now subclasses `BaseException` directly (corrected from `Exception` in this same delta — see §3). The wave's own verification trace (in `docs/ADVERSARIAL_HARDENING_REPORT.md`) argues this is safe because the DB, not in-process state, is the sole source of durable truth, and every write is a single atomic commit an async exception can only land before (nothing durable) or after (already correct) — explicitly distinguished from HARD-008's genuine irreversible-effect stakes. Verdict recorded as "SAFE AS IMPLEMENTED," not "requires Codex" — **Codex should independently judge whether that self-assessment holds**, particularly the claim that an async-raised `BaseException` inside `asyncio.run()`'s event loop is safe at every interpreter-level boundary (the report itself states this specific claim is NOT proven to CPython-internals-level certainty).

5. **FINAL-001 / FINAL-003 / FINAL-004** (R5A, §3 above) — the DNS-resolution-process-isolation design (FINAL-003) in particular is architecturally novel for this codebase (first use of `multiprocessing` anywhere in it) and went through 2 rejected iterations before landing; Codex should re-examine whether the "spawn" context choice, the picklable-only cross-process data contract, and the untimed post-kill `join()` (579e150) are all still sound together, not just each individually as originally reviewed.

6. **Rate-limit refactor semantic equivalence** (`f88b84a`, cleanup wave). `app/security/rate_limit.py` — 459 → 270 lines, 14 independent bucket-dict instances consolidated into one `_RateLimiter` class. This is the single largest diff in the cleanup wave and the most plausible place for a subtle behavior change (bucket-key derivation, eviction threshold, per-limiter isolation) to have slipped in despite the "behavior-preserving" claim. The subsequent adversarial wave's HARD-001 fix (bucket eviction sweep) was built on top of this refactored class — worth confirming the eviction fix's `_SWEEP_THRESHOLD=512` behavior is correctly scoped per-limiter-instance, not accidentally shared/global after the consolidation.

7. **`pool_pre_ping` + `connect_timeout`** (`app/db/session.py`). Two separate waves each added one engine-configuration change (`pool_pre_ping=True` in production-readiness; `connect_timeout: 10` for non-SQLite URLs in adversarial). Both are live-Docker-verified individually; Codex should confirm they compose correctly together (e.g., does a `connect_timeout`-bounded failed connection attempt ever get treated as a "successful ping" by `pool_pre_ping`'s pre-flight check, or vice versa) — not verified as a combined interaction in either wave's own testing.

8. **Scheduler SIGTERM handling**, full picture: production-readiness added the handler (AUD-008), adversarial found and fixed a real gap in it (HARD-005 — `Exception` vs `BaseException`). Codex should treat this as one continuous review target, not two separate ones — the second wave's fix is a correction to the first wave's own work, both authored within this same delta.

9. **`MAX_OFFSET` API change** (`app/api/routes.py`, BOUND-001). A genuine breaking-change-adjacent addition: any existing client passing `offset > 2**31-1` (previously silently crashing anyway) now gets a clean `422` instead. Confirm this is judged acceptable as a "clarifying an existing crash into a clean error" change, not a public-API-compatibility break requiring a version bump or deprecation notice.

10. **Privacy/logging fixes**, full picture across waves: HARD-002 (Bundesagentur `exc_info`), HARD-003 (company_research, 2 halves), BOUND-011 (follow_up_send heartbeat), BOUND-011b (Bundesagentur response-snippet leak). Four independent leaks found across two separate "independent" audit passes, two of them (HARD-003's log half, and both BOUND-011 findings) were misses from an earlier pass in the *same* delta claiming completeness. Codex should treat "the privacy audit is now exhaustive" with real skepticism given this track record — a third pass finding a fifth leak would not be surprising.

11. **Review-package validation changes** (BOUND-006, `app/models/review_package.py`) — confirm the new `Field(ge=1)`/`max_length=2000` bounds cannot reject any value a legitimate existing client could plausibly have been sending (both waves' test suites found none, but Codex has broader context on real usage patterns than a test-suite-only check can provide).

12. **Docker/CI/migration startup behavior** — confirm `ALEMBIC_AUTO_UPGRADE=false` is genuinely enforced (not just documented) across every deployment path added in the production-readiness wave, and that R5A's new migration (`c7d3f9a1e5b8`) is correctly reachable via the same explicit-upgrade path with no special-casing.

---

## 6. Findings matrix

Compact index only — full narrative for every entry lives in the
per-wave report named in the "Source" column.

| ID | Priority | Status | Fixed/Deferred | Codex | Astra | One-line reason |
|---|---|---|---|---|---|---|
| FINAL-001 | — | RESOLVED | Fixed | — | — | `imaplib.IMAP4.abort` now caught alongside `OSError` at all 3 XING call sites |
| FINAL-003 | — | RESOLVED (3rd round) | Fixed | — | — | DNS+connect phase now bounded via a killable child process, not a thread |
| FINAL-004 | — | RESOLVED (2nd round) | Fixed | — | — | Permanent-vs-transient skip classification narrowed to exclude our own parsing bugs |
| AUD-001 | P1 | Addressed | Fixed | No | No | Docker artifacts added |
| AUD-002 | P1 | Deferred | Documented | Yes | No | `/health` doesn't check DB — API-surface change |
| AUD-003 | P1 | Deferred | Documented | Yes | No | Rate limiter collapses behind a reverse proxy |
| AUD-004 | P1 | Documented | Documented | Yes | No | In-memory limiter not multi-worker safe; single-worker is the supported deployment |
| AUD-005 | P2 | Deferred (superseded) | Documented → later **fixed as HARD-001** | No | No | Unbounded bucket dict growth |
| AUD-006 | P2 | Deferred | Documented | Yes | No | API key compare not constant-time |
| AUD-007 | P1 | **Fixed** | Fixed | No | No | `pool_pre_ping=True` added |
| AUD-008 | P2 | **Fixed** (later corrected) | Fixed, then corrected by HARD-005 | No | No | SIGTERM handler added; own gap found one wave later |
| AUD-009 | P3 | Deferred | Documented | No | No | No startup diagnostic log |
| AUD-010 | P2 | Deferred | Documented | No | No | No request-correlation ID |
| AUD-011 | P2 | Documented | Documented | No | No | Postgres not yet field-proven in long-lived prod |
| AUD-012 | P2 | **Fixed as HARD-004** | Fixed (adversarial wave) | No | No | providers↔collectors import cycle relocated |
| AUD-013 | P3 | Deferred | Documented | No | No | core→providers inverted layering |
| AUD-014 | P2 | Deferred | Documented | Yes | No | db→services/agents inverted layering |
| AUD-015 | P3 | Deferred | Documented | No | No | providers→db.models ORM coupling |
| AUD-016 | P2 | Deferred | Documented | Yes | No | routes.py 118 HTTPException sites, ≥12 duplicated blocks |
| AUD-017 | P3 | Deferred | Documented | Yes | No | 200+ LOC orchestration functions |
| AUD-018 | P2 | Accepted (pre-existing) | N/A | N/A | N/A | XING IMAP credential over-scope — deliberate v1 compromise |
| AUD-019 | P2 | **Fixed** | Fixed | No | No | CI Docker validation gate added |
| AUD-020 | P3 | Documented | Documented | No | No | No Python version upper bound |
| HARD-001 | P2 | **Fixed** | Fixed | No | No | Rate-limiter bucket eviction sweep added |
| HARD-002 | P2 | **Fixed** | Fixed | No | No | Bundesagentur `exc_info=True` → `type(exc).__name__` |
| HARD-003 | P2 | **Fixed** (2 halves) | Fixed | No | No | company_research API + log both sanitized |
| HARD-004 | P3 | **Fixed** | Fixed | No | No | `is_configured` relocated, pre-authorized |
| HARD-005 | P2 | **Fixed** | Fixed, verdict "SAFE AS IMPLEMENTED" | No | No | `_ShutdownRequested` now `BaseException` |
| HARD-006 | P1 | **Fixed** | Fixed | No | No | 3 config fields given `Field(ge=1[,le=...])` |
| HARD-007 | P2 | Deferred | Documented | **YES** | No | `upsert_job` no `IntegrityError` handling |
| HARD-008 | P1 | Deferred | Documented | **YES** | **YES** | Stranded `PENDING` send, no recovery path |
| HARD-009 | P2 | Deferred | Documented | **YES** | No | NFC/NFD fingerprint dedup miss |
| HARD-010 | P2 | **Fixed** (test-only) | Fixed | No | No | Real-PostgreSQL CAS race coverage added |
| HARD-011 | P1 | **Fixed** | Fixed | No | No | `connect_timeout` added, dialect-verified |
| HARD-012 | P3 | Deferred | Documented | **YES** | No | Skill-matching NFKC normalization gap |
| HARD-013 | P3 | Deferred | Documented | No | No | Two `normalize_company_name` functions, different rules |
| HARD-014 | P3 | Deferred | Documented | No | No | `record_failed_attempt` read-modify-write, no CAS |
| HARD-015 | P2 | Deferred (deep-dived as BOUND-002) | Documented | **YES** | No | Auth runs before rate limiter |
| HARD-016 | P3 | Deferred (inherent) | Documented | No | No | Lease expiry uses wall-clock time |
| BOUND-001 | High | **Fixed** | Fixed | No | No | Pagination offset overflow → uncaught 500 |
| BOUND-002 | Info | Confirmed safe | Documented | No | No | Auth-before-rate-limit not a lockout vector |
| BOUND-003 | Medium | Deferred | Documented | **YES** | No | No HTTP body size limit anywhere |
| BOUND-004 | Info | Confirmed safe | Documented | No | No | List cardinality up to 5000 — no quadratic blowup |
| BOUND-005 | Low/Medium | Analysis only | Documented | **YES** | No | `jobs.last_seen_at` unindexed, seq scan + sort |
| BOUND-006 | Low | **Fixed** | Fixed | No | No | review-package version/note field bounds added |
| BOUND-006b | Low | Deferred | Documented | **YES** | No | CV/Bewerbung free-text fields deliberately left unbounded |
| BOUND-007 | Trivial | **Fixed** | Fixed | No | No | Stale comment corrected |
| BOUND-010 | Info | Confirmed safe | Documented | No | No | Live 2026 Berlin DST transitions, no bug found |
| BOUND-011 | Medium | **Fixed** | Fixed | No | No | follow_up_send heartbeat `exc_info=True` leak |
| BOUND-011b | Medium | **Fixed** | Fixed | No | No | Bundesagentur raw response snippet leak |
| BOUND-012 | Low | Deferred | Documented | **YES** | No | Same 422 error, 2 different detail strings |
| BOUND-013 | Low | Deferred | Documented | **YES** | No | follow-up cycle silently never eligible w/o Gmail sync |
| BOUND-014 | Low | Deferred | Documented | **YES** | No | poll_seconds > interval_seconds cadence confusion |
| BOUND-015 | Info | Confirmed safe | Documented | No | No | 15-case malformed MIME corpus, no crashes |
| BOUND-016 | Info | Confirmed safe | Documented | No | No | 5-invariant mutation testing, no weak tests found |

**Sources:** FINAL-* → R5A commit messages (no dedicated doc exists for R5A;
see §12 note). AUD-* → `docs/PRODUCTION_READINESS_AUDIT.md`. HARD-* →
`docs/ADVERSARIAL_HARDENING_REPORT.md`. BOUND-* → `docs/API_BOUNDARY_HARDENING_REPORT.md`.

---

## 7. Explicitly deferred items (not fixed — do not read as fixed)

| ID | What | Why deferred |
|---|---|---|
| **HARD-007** | `upsert_job` concurrent-insert `IntegrityError` handling | Would touch write/retry semantics — explicitly out of every wave's safe-fix scope, despite dialect-specific evidence now fully characterizing the needed fix (including the mandatory PostgreSQL `rollback()` step) |
| **HARD-008** | `response_draft_send.py` stranded `PENDING` recovery | Touches the outbound-send state machine directly — the exact class of change every wave's brief calls out as "flag as REQUIRES CODEX even if a likely fix is obvious" |
| **HARD-009** | Fingerprint NFC/NFD Unicode canonicalization | Fingerprint changes require a live-data-driven audit first, per this project's own established `CLAUDE.md` convention — not a speculative one-line change |
| **HARD-012** | Skill-matching NFKC normalization gap | Same root cause as HARD-009, different subsystem; changing `normalize_skill` could shift real scoring/matching outcomes for real candidate data |
| **HARD-015** | Auth-before-rate-limit dependency ordering | Reordering is a security-relevant behavior change; API-boundaries wave's BOUND-002 deep-dive concluded current ordering is NOT a lockout vector, but did not change the ordering itself |
| **BOUND-003** | No HTTP body size limit anywhere | No established per-field precedent at this scale; conventionally an infra-layer (reverse proxy / ASGI) concern, not a per-endpoint Pydantic bound — recommend Codex confirm whether the production deployment has an infra-level cap |
| **BOUND-005** | `jobs.last_seen_at` missing index | Would require an Alembic migration — explicitly reserved for Codex once a slow query is reproduced and the index clearly matches (both true here, still not applied) |
| **BOUND-006b** | `CVContentPatch`/`BewerbungContentPatch` free-text fields left unbounded | No established sibling precedent for this specific field class exists elsewhere in the codebase; "do not add arbitrary bounds merely because a field is currently unlimited" |
| **BOUND-012** | `ResponseDraftNotApprovableError` → 422 with 2 different detail strings | Changing client-visible `detail` text is a minor but real breaking change for any consumer string-matching on it |
| **BOUND-013** | `automation_follow_up_cycle_enabled=True` without Gmail sync silently never eligible | Legitimate config combination in some real deployment setups (manual/external Gmail sync); fails the "can never be useful" bar for startup validation |
| **BOUND-014** | `poll_seconds > interval_seconds` cadence confusion | Both values individually legal; the combination is counter-intuitive, not invalid — recommend a startup log line, not a validation error |

None of the above should be read, cited, or reported as fixed anywhere in
this delta.

---

## 8. Outbound / human-approval safety — what was NOT intentionally changed

Across all 5 waves:

- **Approval requirements** — `app/services/response_draft.py`,
  `app/services/follow_up.py`, `app/services/review_package.py`'s approval
  gates were not touched by the production-readiness or cleanup waves at
  all (explicitly confirmed in `docs/CODEX_PRODUCTION_READINESS_HANDOFF.md`
  §6). The adversarial and API-boundary waves only added validation bounds
  (HARD-006, BOUND-006) and log sanitization (HARD-002/003, BOUND-011/011b)
  around the existing approval machinery — the approval CAS/version-check
  logic itself (`ReviewPackageService.approve`/`.reject`, `decide_review`'s
  atomic update) is byte-for-byte unchanged.
- **Outbound-send authorization** — `app/services/response_draft_send.py`,
  `app/services/follow_up_send.py`, `app/providers/email/smtp.py` are
  untouched by every wave except one narrow, documented-not-code-changed
  fact: BOUND-011 fixed a *logging* leak inside `follow_up_send.py`'s
  heartbeat renewal handler, not any send/approval logic in that file. No
  wave added, removed, or altered a single line of actual send-authorization
  code. HARD-008 (the one real gap found in this area) was explicitly
  **not** fixed, precisely to preserve this invariant until Codex/Astra
  review it.
- **Provenance/trust rules** — `TRUSTED_JOB_SOURCES`,
  `is_top_level_fact_usable_for_generation`,
  `app/models/candidate_profile.py`'s trust semantics are untouched across
  every wave. Cleanup R1's `3978169` shared subject-bound/trusted-source
  *helper functions* between `response_draft.py` and `follow_up.py` but did
  not change what either considers trusted — both callers' actual trust
  decisions are asserted unchanged by that commit's own test evidence.
- **Retry semantics** — unchanged everywhere except where explicitly
  documented: `docs/API_RETRY_SEMANTICS.md` (API-boundaries wave) is a pure
  inventory/documentation deliverable, not a retry-behavior change. HARD-008
  and HARD-007 (both retry-adjacent) were deliberately left unfixed for
  exactly this reason.
- **DB schema** — the only schema change anywhere in this entire delta is
  R5A's `c7d3f9a1e5b8_add_gmail_permanent_skips_table` (additive new table,
  chained on the prior head). No other wave adds, drops, or alters a table,
  column, index, or constraint. `alembic heads` at FINAL HEAD is
  `c7d3f9a1e5b8`, unchanged since R5A.
- **Application-state semantics** — `ApplicationStatus` transitions, job
  scoring, matching, and CV/Bewerbung generation logic are untouched by
  every wave in this delta; all five waves are scoped to
  infrastructure/hardening/validation/observability concerns, not the
  business-logic state machine itself.

---

## 9. Database / concurrency review

- **CAS (compare-and-swap) gates:** `claim_send_attempt`/`begin_transmission`
  (response-draft and follow-up send dispatch), `decide_review`
  (review-package approve/reject), `get_or_create_schedule`/schedule-claim
  CAS, `renew_run_lease` (automation-run lease ownership) — none of their
  underlying CAS logic was changed by any wave in this delta. What changed:
  test coverage. HARD-010 added the first real-PostgreSQL race test for
  `claim_send_attempt` (previously SQLite-only); BOUND-016's mutation
  testing (5 invariants, including lease-ownership CAS and fingerprint
  uniqueness) confirmed the existing test suite genuinely catches a broken
  CAS, not vacuously.
- **Leases:** `AUTOMATION_RUN_LEASE_TTL_SECONDS`-based lease expiry
  (`app/db/automation_repository.py::_is_lease_expired`) is architecturally
  wall-clock-based by necessity (HARD-016, documented as an accepted
  tradeoff, not a defect) — unchanged by any wave.
- **PostgreSQL concurrency tests, evidence strength by scenario:**
  - **Real PostgreSQL (Docker container, real Alembic chain):**
    `upsert_job` concurrent-insert race (HARD-007, `472110d`),
    `claim_send_attempt` 5-thread race (HARD-010, `8d8ab7e`),
    the pre-existing schedule-claim CAS and Gmail-watermark commit-order
    races (predate this delta, re-confirmed present and unchanged).
  - **SQLite-only:** the *original* `upsert_job` race reproduction
    (`test_repository.py`, superseded by the PostgreSQL version above but
    both still present), most of the adversarial-wave's other concurrency
    checks that didn't specifically require dialect-divergent behavior.
  - **Live Docker failure injection (not a concurrency test, but real
    infrastructure behavior):** DB stop/restart during a live connection
    (HARD-011, confirms `connect_timeout` + `pool_pre_ping` interaction
    empirically, not just by code reading).
- **Watermark progression:** Gmail sync watermark commit-order concurrency
  test predates this delta; not touched by any of the 5 waves.
- **Scheduler ownership:** single-process-by-deployment-design (Compose
  profile-gating) plus DB-level lease/CAS enforcement — both layers
  unchanged in this delta except HARD-005's `BaseException` correction to
  the SIGTERM-interruption safety model (see §5, item 4).
- **Approval/send claims:** `claim_send_attempt` unchanged; only its test
  coverage strengthened (HARD-010).
- **`upsert_job` race:** the one confirmed, unresolved concurrency gap in
  this entire delta — see HARD-007 (§5, item 2). Data integrity is never at
  risk (DB-level UNIQUE constraint holds on both dialects); the gap is an
  unhandled-exception/UX robustness issue, dialect-specific in its exact
  failure shape (PostgreSQL additionally poisons the losing transaction
  until `rollback()`).

---

## 10. Security review

- **API authentication:** `app/security/auth.py::require_api_key` — plain
  `!=` string comparison, not `hmac.compare_digest` (AUD-006, pre-existing,
  deferred across every wave, theoretical-only risk at this scale). Not
  reordered relative to rate limiting (HARD-015/BOUND-002: current ordering
  means unlimited-401 spam never trips 429, but also never erodes the
  legitimate operator's own budget — deep-dived and confirmed non-exploitable
  for lockout in the API-boundaries wave, not fixed).
- **Rate limiting:** consolidated into one reusable `_RateLimiter` class
  (cleanup wave, `f88b84a` — Codex should verify semantic equivalence, §5
  item 6), then given a bounded eviction sweep (HARD-001) on top of that
  consolidation. Remains in-memory, per-process (AUD-004, documented
  limitation for the single-worker-only supported deployment) and unaware of
  reverse-proxy client IPs (AUD-003, deferred — would collapse to one shared
  bucket if fronted by a proxy, not currently the case in the documented
  deployment target).
- **Reverse proxy limitations:** no reverse-proxy `X-Forwarded-For`
  awareness anywhere in this delta (AUD-003); the documented supported
  deployment (`docs/DEPLOYMENT.md`) is direct/VPN-gated, not
  reverse-proxy-fronted, so this is a documented constraint on deployment
  topology rather than a live gap in the current setup.
- **Privacy fixes (4 total across 2 waves):** HARD-002 (Bundesagentur
  `exc_info`), HARD-003 (company_research, 2 halves — API/DB half and a
  separately-missed LOG half), BOUND-011 (follow_up_send heartbeat
  `exc_info`), BOUND-011b (Bundesagentur response-snippet in client-visible
  exception message). See §5 item 10 for why Codex should not assume this
  list is now exhaustive.
- **Error leakage fixes:** same set as above; all follow one established
  convention (`type(exc).__name__` only, never `str(exc)`/`exc_info=True`)
  applied consistently once each leak was found.
- **Secret handling:** unchanged across every wave — secrets remain
  environment-variable-only (`.env`, never committed); the one credential
  with broader-than-needed scope (XING IMAP App Password, AUD-018) is a
  pre-existing, explicitly accepted v1 compromise, not touched.
- **Malformed MIME tests:** BOUND-015, 15 synthetic-corpus cases fed
  directly into the real lowest-level parsing functions
  (`GmailImapProvider._parse_message`, `XingEmailCollector._process_message`)
  — no crashes, no trust escalation, no fix needed.
- **Body-size limitation:** confirmed absent everywhere (BOUND-003,
  Phase 4 of the adversarial wave independently confirmed the same absence
  earlier) — deliberately left unbounded at the application layer per
  explicit instruction against arbitrary bounds; recommended as an
  infra-layer (reverse proxy/ASGI) concern for Codex to confirm is actually
  configured in the real deployment.

---

## 11. Test evidence

Evidence is **not** uniform across waves — stated per-milestone rather than
implied as one continuous number, since methodology differed:

- **R5A (`579e150`):** targeted suites only, per commit message — 312 tests
  (a9e32aa: xing_email, xing_endpoint, imap_deadline, migrations,
  ci_postgres_migration_gate, providers_email_imap, gmail_repository,
  gmail_inbox_service, gmail_endpoints), then 195 tests (da724f6, the
  correction round), then `test_imap_deadline.py` alone re-run 3 consecutive
  times for stability (579e150). **No full-suite run recorded in any R5A
  commit message** — this is a real evidence gap Codex should note, not
  paper over.
- **Cleanup R1 (`f88b84a`):** per-commit targeted-file + full-suite-unchanged
  claims (per commit message convention), not independently re-verified for
  this handoff.
- **Production readiness (`9b33a72`):** full suite, explicitly recorded —
  **1934 passed, 4 skipped, 0 failed, 1 warning in 916.94s** (detached
  background process run), identical pass/skip counts to the pre-change
  baseline. Ruff check/format clean. Alembic head `c7d3f9a1e5b8`.
- **Adversarial (`f9181a1`):** per-phase and per-finding targeted test
  evidence throughout `docs/ADVERSARIAL_HARDENING_REPORT.md`, but **no
  single consolidated full-suite pass/fail count appears anywhere in that
  report or its commits' messages** — another real evidence gap, distinct
  from (but same class as) R5A's. Individual new/changed test files are all
  confirmed passing in their own targeted runs.
- **API boundaries (`8dff9fe`, FINAL HEAD):** full suite, explicitly
  recorded — **2006 passed, 7 skipped, 0 failed (2013 collected)**. Run as 4
  sequential chunks by test file (463 + 501 + 535 + 507 = 2006) because two
  unsplit background full-suite runs were killed by the environment partway
  through (at 46% and 59% respectively, zero failures observed up to the
  kill point in either case) — splitting was a reliability workaround for
  the execution environment, **not a response to any test failure**.
  `ruff check app tests alembic` → all checks passed. `ruff format --check
  app tests alembic` → 230 files already formatted. `alembic heads` →
  `c7d3f9a1e5b8` (head), confirmed unchanged from R5A.

**Test count growth across the delta:** 1934 (production readiness) → 2013
collected (API boundaries, FINAL HEAD) — consistent with new test files
added by the adversarial and API-boundaries waves; no count ever decreased,
and no wave reported a test removal without a corresponding replacement.

**PostgreSQL integration evidence:** real, disposable `postgres:16` Docker
containers, migrated via the actual Alembic chain (never `create_all`), used
for: the pre-existing schedule-claim/Gmail-watermark races (predate this
delta), HARD-007's `upsert_job` race, HARD-010's `claim_send_attempt` race,
HARD-011's live connect-timeout/pool_pre_ping experiment, BOUND-005's
`EXPLAIN ANALYZE` capacity simulation, and the production-readiness wave's
full Docker smoke test (image build, migration run, health/auth checks,
clean SIGTERM shutdown for both `web` and `scheduler`).

---

## 12. Codex review instructions

**Codex is READ-ONLY for this review. Codex must NOT fix its own findings.**
Use HIGH reasoning.

**Normal review range:**
```
23c8cb0a5b055d940bd155f8adb6900d07610079..8dff9fe022b9e86acfb3128c726f1fb1814225cb
```

**Review primarily:** correctness, security, trust boundaries, transactions,
concurrency, data integrity, idempotency, outbound side effects, human
approval, public API compatibility, Docker/deployment correctness.

**Do not spend review budget on cosmetic style.** Targeted tests only unless
a specific finding requires deeper evidence to confirm or refute.

**Note on source material:** unlike the other 3 waves, R5A has no dedicated
per-wave findings report (`docs/ADVERSARIAL_HARDENING_REPORT.md` and
`docs/PRODUCTION_READINESS_AUDIT.md`/`docs/API_BOUNDARY_HARDENING_REPORT.md`
exist for their respective waves; no equivalent `R5A_REPORT.md` exists) —
§1/§3/§5 above and the 3 R5A commit messages themselves (`a9e32aa`,
`da724f6`, `579e150`, all reproduced in full where relevant) are the only
consolidated R5A material. Codex should read those commit messages directly
if deeper detail is needed than this handoff provides.

For every finding return:
```
ID
severity
file:line
scenario
impact
acceptance criteria
```

**Do NOT implement fixes.**

---

## 13. Required final Codex verdict

*(Placeholders — to be filled in by Codex's own review output, not by this
handoff document.)*

```
R5A FINAL-001:              RESOLVED / NOT RESOLVED
R5A FINAL-003:               RESOLVED / NOT RESOLVED
R5A FINAL-004:               RESOLVED / NOT RESOLVED
Cleanup behavior-preserving: YES / NO
Production-readiness changes safe: YES / NO
Adversarial fixes safe:      YES / NO
API-boundary fixes safe:     YES / NO
Security/trust preserved:    YES / NO
Persistence/integrity preserved: YES / NO
Human approval preserved:    YES / NO
Outbound safety preserved:   YES / NO
Docker/deployment acceptable: YES / NO

MERGE GATE: PASS / FAIL
```

This handoff's own assessment (not a substitute for Codex's independent
verdict, offered only as the preparer's read of the evidence assembled
above): every wave's own stated intent is internally consistent with the
evidence gathered for it, the one real schema change (R5A) is additive and
migration-tested on real PostgreSQL, and no wave's diff touches approval,
outbound-send, provenance/trust, or retry-semantics code paths except where
explicitly and narrowly documented in §8. The two known, unresolved
outbound-safety-adjacent gaps (HARD-007, HARD-008) are exactly the ones this
handoff flags for Codex/Astra in §5 and §14 — they are not silently carried
forward.

---

## 14. Astra escalation

Findings that should go to **Astra** specifically, **after** Codex review
(not instead of it) — Astra's prior rounds (R4B, R4C, R5A) in this project
have focused on outbound-safety-critical and irreversible-external-effect
correctness, so escalation here is limited to findings that fit that same
class, not general code-quality items:

1. **HARD-008 — `response_draft_send.py` stranded `PENDING` send state.**
   Must be included, per explicit instruction and per its own classification
   throughout every wave (`Codex: YES. Astra: YES`) — this is a genuine gap
   in the human-approval-adjacent outbound-email safety machinery, the same
   class of rigor FINAL-001/003/004 received from Astra in R5A. The
   `follow_up_send.py` sibling's `send_attempted` CAS pattern is the
   evident fix direction but was deliberately not implemented, pending
   review.

**Explicitly NOT escalated to Astra** (Codex-only, or no review needed) —
and why, so this isn't read as an oversight:

- **HARD-007** (`upsert_job` race) — data integrity is never actually at
  risk on either dialect (DB-level UNIQUE constraint holds); this is a
  robustness/UX gap, not an irreversible-outbound-effect gap. Codex-only.
- **HARD-009 / HARD-012** (Unicode fingerprint/skill-matching gaps) — dedup
  and matching-quality issues, not outbound-safety or approval-boundary
  issues. Codex-only.
- **BOUND-003, BOUND-005, BOUND-006b, BOUND-012, BOUND-013, BOUND-014** — all
  API-boundary/validation/observability findings with no outbound-send or
  approval-trust dimension. Codex-only, and several are genuinely low-risk
  (documented, not urgent) — not auto-escalated to either reviewer beyond
  the standard Codex pass.
- **AUD-002/003/004/006/012/014/016/017** — production-readiness/
  architecture findings (health-check depth, rate-limiter proxy-awareness,
  layering smells, file-size/duplication) with no outbound or approval
  dimension. Codex-only, per the production-readiness wave's own explicit
  merge-gate recommendation.
- Cleanup-wave refactors — claimed behavior-preserving, test-suite-verified;
  no outbound/approval surface touched by any of the 7 commits. Not
  escalated to either reviewer beyond Codex's standard correctness pass
  (§5 item 6 flags the rate-limit consolidation specifically for Codex,
  not Astra, since it's a resource-management/security-hardening concern,
  not an outbound-effect one).

Low-risk, purely additive, or purely-documentation findings across every
wave (AUD-001/009/010/011/013/015/018/019/020, HARD-004/010/013/014/016,
BOUND-002/004/007/010/015/016) are not escalated to either reviewer beyond
what's already recorded in their own wave's report — auto-escalating
confirmed-safe or trivial findings would dilute review budget away from the
items that actually need it.
