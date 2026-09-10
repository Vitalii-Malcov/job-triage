# Production Readiness Audit — JobTriage (AI Job Search Control Center)

Date: 2026-09-11
Branch: `chore/production-readiness-r1`
Audited HEAD (base): `f88b84abd38a383a42c62d30e6306e1fcec76513` (frozen `refactor/project-cleanup-r1`)
Method: read-only static inspection of `app/`, `tests/`, `alembic/`, `.github/`, `pyproject.toml`, `README.md`, `.env.example`. No code was changed to produce this document. No findings are manufactured — every entry below has file:line evidence.

This audit evaluates the project **as if it had to be deployed tomorrow**, not as a code-quality review of the business logic itself. FINAL-001/003/004 (R5A inbound-email correctness) semantics are explicitly out of scope for re-litigation here — they are treated as frozen and correct.

---

## How to read this document

Each finding has:
- **Priority**: P0 (deployment blocker / data-loss / serious security risk) · P1 (should fix before real-world deployment) · P2 (important hardening) · P3 (optional / scaling / polish)
- **Type**: VERIFIED ISSUE · THEORETICAL LIMITATION · DEPLOYMENT REQUIREMENT · SCALABILITY CONCERN · DOCUMENTATION GAP

---

## Findings

### AUD-001 — No Dockerfile / container deployment artifacts existed
- **Priority:** P1
- **Type:** DEPLOYMENT REQUIREMENT
- **File/module:** repository root (absence)
- **Evidence:** `find . -iname "*docker*" -o -iname "*compose*"` returned nothing before this branch.
- **Failure scenario:** there is no repeatable, reviewable way to run the app in a production-like environment; every deployment would be ad hoc.
- **Impact:** blocks any containerized deployment path.
- **Recommended direction:** add `Dockerfile`, `.dockerignore`, `compose.yaml`, entrypoint script (this branch, Phase 7-9).
- **Code must change:** yes (infra files only, no `app/` changes).
- **Future Codex review mandatory:** no — infrastructure-only.
- **Status:** addressed in this branch (see `docs/DOCKER.md`).

### AUD-002 — `/health` does not check any dependency
- **Priority:** P1
- **Type:** VERIFIED ISSUE
- **File/module:** `app/api/routes.py:267-269`
- **Evidence:**
  ```python
  @router.get("/health")
  def health() -> dict[str, str]:
      return {"status": "ok"}
  ```
- **Failure scenario:** the database is down or unreachable (connection refused, disk full, credentials rotated) but `/health` still returns `200 {"status": "ok"}` because it never touches `get_db()`. A container orchestrator, load balancer, or uptime monitor watching this endpoint will report the service healthy while every real request 500s.
- **Impact:** false-positive health signal; delays incident detection; a rolling deploy could keep routing traffic to a broken instance.
- **Recommended direction:** this is an **API-surface change** (new response shape / behavior for an existing endpoint), so per this session's constraints it is **not** implemented automatically. Recorded here and in `docs/TECHNICAL_DEBT.md` for a reviewed change (e.g. a lightweight `SELECT 1` check with a bounded timeout, still returning fast).
- **Code must change:** yes, but deliberately deferred (touches public API contract).
- **Future Codex review mandatory:** yes.

### AUD-003 — Rate limiter has no reverse-proxy client-IP awareness
- **Priority:** P1
- **Type:** VERIFIED ISSUE
- **File/module:** `app/security/rate_limit.py`
- **Evidence:** every limiter keys on `request.client.host if request.client else "unknown"`; no `X-Forwarded-For`/`Forwarded` handling exists anywhere in the file or repo (zero grep hits).
- **Failure scenario:** the recommended deployment (Phase 6) puts a reverse proxy in front of FastAPI. Every request then arrives from the proxy's local IP, so `request.client.host` is constant for all real clients. All 14 rate limiters collapse to a single shared bucket — one abusive client can exhaust the budget for every other client.
- **Impact:** rate limiting becomes ineffective (denial of service against legitimate users) the moment a reverse proxy is introduced — which is the deployment this project recommends.
- **Recommended direction:** trusted-proxy-aware `X-Forwarded-For` parsing (only trust it when the immediate peer is a known proxy). This changes security-relevant logic — **not implemented in this session**; recorded for Codex review.
- **Code must change:** yes, deferred.
- **Future Codex review mandatory:** yes (security-relevant).

### AUD-004 — In-memory rate limiter is per-process (not multi-worker safe)
- **Priority:** P1
- **Type:** SCALABILITY CONCERN
- **File/module:** `app/security/rate_limit.py`
- **Evidence:** buckets are plain `dict[str, deque[float]]` + `threading.Lock`, no Redis/DB-backed store.
- **Failure scenario:** running `uvicorn --workers 4` gives each worker its own bucket set; the effective limit becomes `configured_limit × 4` rather than the configured value, and a client's requests are only rate-limited within whichever worker happened to receive them.
- **Impact:** rate limits silently weaken under horizontal/multi-worker scaling.
- **Recommended direction:** for the single-server, single-worker initial deployment this project targets (see `docs/DEPLOYMENT.md`), this is a documented **known limitation**, not a blocker. A shared store (Redis) is future work if multi-worker scaling is adopted.
- **Code must change:** no (for the recommended single-worker deployment); yes if multi-worker is adopted later.
- **Future Codex review mandatory:** yes, before any multi-worker deployment.

### AUD-005 — Rate limiter bucket dictionaries grow unbounded
- **Priority:** P2
- **Type:** VERIFIED ISSUE
- **File/module:** `app/security/rate_limit.py`
- **Evidence:** `self.buckets: dict[str, deque[float]] = defaultdict(deque)` — per-key deques are trimmed (`popleft()` on expiry), but a host key itself is never removed from the outer dict. There are 14 separate `_RateLimiter` instances, each with its own such dict.
- **Failure scenario:** a public-facing instance hit by many distinct source IPs (including scanners/bots) accumulates one dict entry per distinct IP per limiter, forever, for the life of the process.
- **Impact:** slow unbounded memory growth proportional to distinct-client count; only relevant for long-lived, publicly reachable processes.
- **Recommended direction:** low priority given the recommended non-public-facing initial deployment (`docs/DEPLOYMENT.md`); periodic eviction of empty buckets would be a safe, additive future fix.
- **Code must change:** yes, deferred (P2, not implemented this session — touches security module behavior).
- **Future Codex review mandatory:** yes.

### AUD-006 — API key comparison is not constant-time
- **Priority:** P2
- **Type:** THEORETICAL LIMITATION
- **File/module:** `app/security/auth.py:8` (`x_api_key != expected`)
- **Evidence:** plain string `!=` comparison, not `hmac.compare_digest`.
- **Failure scenario:** a network-observable timing side channel on API key comparison. Realistically low-impact here — a single static key behind normal network jitter/TLS makes practical timing exploitation very hard — but it is a textbook hardening gap.
- **Impact:** theoretical only at this scale; standard practice is `hmac.compare_digest` for any secret comparison.
- **Recommended direction:** swap to `hmac.compare_digest(x_api_key, expected)` — small, low-risk, security-relevant change. **Not implemented in this session** per the "no security-behavior changes without review" constraint; recorded for Codex review even though the fix itself is small, since it touches the auth boundary.
- **Code must change:** yes, deferred.
- **Future Codex review mandatory:** yes.

### AUD-007 — No `pool_pre_ping` / explicit pool sizing for the DB engine
- **Priority:** P1
- **Type:** VERIFIED ISSUE
- **File/module:** `app/db/session.py:11`
- **Evidence:** `engine = create_engine(settings.database_url, connect_args=connect_args)` — no `pool_pre_ping`, `pool_size`, or `max_overflow` arguments; SQLAlchemy defaults apply.
- **Failure scenario:** a long-running PostgreSQL deployment where the DB restarts, a firewall/load balancer drops idle connections, or a managed Postgres instance recycles connections — the next request to check out that stale connection gets an unhandled `OperationalError` instead of SQLAlchemy transparently detecting and replacing it.
- **Impact:** intermittent 500s after any DB-side connection interruption, until the pool happens to cycle.
- **Recommended direction:** add `pool_pre_ping=True` (and consider `pool_recycle`) for non-SQLite URLs. This is an engine-configuration change, not a business-logic change — **candidate for safe implementation in this session** (Phase 17) since it doesn't alter schema, transactions, retry semantics, or persisted data, only connection health-checking.
- **Code must change:** yes.
- **Future Codex review mandatory:** recommended but not strictly required (low-risk, additive, well-understood SQLAlchemy idiom).
- **Status:** **fixed this branch** — `app/db/session.py` now passes `pool_pre_ping=True` unconditionally (harmless no-op for SQLite). Verified: `python -m pytest -q` full suite still green after the change (see final report); targeted scheduler/service tests re-run explicitly.

### AUD-008 — `app/scheduler.py` has no SIGTERM handler
- **Priority:** P2
- **Type:** VERIFIED ISSUE
- **File/module:** `app/scheduler.py:141-192` (`main()`)
- **Evidence:** the poll loop only catches `KeyboardInterrupt` (SIGINT) around `asyncio.run(_poll_loop(settings))` and logs `"automation_scheduler_stopped_keyboard_interrupt"`. No `signal.signal(signal.SIGTERM, ...)` exists anywhere in the file.
- **Failure scenario:** `docker stop`, `systemctl stop`, or `kill <pid>` (without `-INT`) sends SIGTERM by default — the process terminates immediately with no graceful-shutdown log line. This is **not a correctness risk**: `AutomationRunRecord.lease_holder`/`lease_expires_at` and the CAS-based schedule claim are specifically designed so a hard-killed scheduler leaves, at worst, a `RUNNING` row whose lease expires and is reconciled by the next process (`app/db/automation_repository.py`). It is purely an operational-visibility gap (no clean log line, no bounded grace period for an in-flight cycle to finish).
- **Impact:** low — correctness is preserved by the lease/CAS design; only operational cleanliness (log noise, container shutdown semantics) is affected.
- **Recommended direction:** add a `signal.signal(signal.SIGTERM, ...)` handler that sets the same stop flag `KeyboardInterrupt` already triggers, so containerized deployments (`docker stop`) shut down as cleanly as Ctrl+C. This changes only shutdown signal handling, not automation/business logic — **candidate for safe implementation in this session** (Phase 17).
- **Code must change:** yes.
- **Future Codex review mandatory:** no (mechanical, well-scoped, does not touch business logic).
- **Status:** **fixed this branch** — `app/scheduler.py` now installs a `signal.signal(signal.SIGTERM, _handle_sigterm)` handler that raises a dedicated `_ShutdownRequested` exception, caught alongside `KeyboardInterrupt` around `asyncio.run(_poll_loop(settings))`, logging `automation_scheduler_stopped_sigterm`. **Live-verified inside the actual Linux container** (not just read): started the `scheduler` service with `AUTOMATION_SCHEDULER_ENABLED=true`, let it complete one poll tick, then ran `docker compose stop scheduler` — container exited in ~3.2s with **exit code 0** and the log line `automation_scheduler_stopped_sigterm` present. (An earlier attempt to verify this via a plain Windows `subprocess.send_signal(SIGTERM)` was inconclusive/misleading — on Windows, `Popen.send_signal(SIGTERM)` calls `TerminateProcess` directly rather than delivering a real SIGTERM to the Python handler, so the only trustworthy verification is inside the real Linux container, which is what's reported here.)

### AUD-009 — No startup diagnostic logging
- **Priority:** P3
- **Type:** DOCUMENTATION GAP
- **File/module:** `app/main.py:12-20`
- **Evidence:** the lifespan calls `configure_logging()`, `run_migrations_if_enabled()`, `start_bot(...)` with no explicit "app started, DB reachable, features X/Y/Z enabled" log line.
- **Failure scenario:** an operator watching container logs at boot has no single line confirming the app came up cleanly with a given configuration; they must infer it from the absence of errors.
- **Impact:** minor operational friction, not a correctness issue.
- **Recommended direction:** optional future hardening — add one structured `INFO` log line at the end of lifespan startup. Not implemented in this session (would touch `app/main.py`, kept minimal-risk by not adding it opportunistically).
- **Code must change:** yes, deferred.
- **Future Codex review mandatory:** no.

### AUD-010 — No correlation/request ID in logs
- **Priority:** P2
- **Type:** DOCUMENTATION GAP / OBSERVABILITY GAP
- **File/module:** `app/core/logging.py`
- **Evidence:** `JsonFormatter` emits timestamp/level/logger/message/exception fields; no `request_id`/`X-Request-ID` middleware or `contextvars` propagation exists anywhere in the repo.
- **Failure scenario:** a single failing user request touches multiple log lines across services/repositories with no shared identifier, making it hard to reconstruct one request's full trace in production logs, especially under concurrent traffic.
- **Impact:** slower incident diagnosis at scale; not a correctness issue for the single-user/small-scale deployment this project currently targets.
- **Recommended direction:** future hardening — ASGI middleware assigning a request-scoped correlation ID. Not implemented this session (touches request-handling middleware, deferred as non-essential for the initial deployment target).
- **Code must change:** yes, deferred.
- **Future Codex review mandatory:** no.

### AUD-011 — PostgreSQL support is explicitly self-described as not yet field-proven
- **Priority:** P2
- **Type:** DOCUMENTATION GAP / THEORETICAL LIMITATION
- **File/module:** `README.md` (pre-restructure), `.env.example:2-4`
- **Evidence:** the original README states Postgres production use is "ещё предстоит обкатать" (still to be field-tested); `.env.example` defaults `DATABASE_URL` to SQLite with Postgres as a commented-out example.
- **Failure scenario:** none observed in code — the CI `scheduler-postgres` job does exercise real PostgreSQL concurrency paths (`tests/integration/test_scheduler_postgres_concurrency.py`, `test_gmail_watermark_postgres_concurrency.py`) and the Alembic-head gate — so Postgres is tested, just not yet run as a long-lived production workload by the author.
- **Impact:** honest disclosure, not a defect. Worth stating plainly in `docs/DEPLOYMENT.md`/`docs/SECURITY_MODEL.md` rather than only in an informal README aside.
- **Recommended direction:** carry this caveat forward explicitly into the new documentation set (done in this branch).
- **Code must change:** no.
- **Future Codex review mandatory:** no.

### AUD-012 — Circular import between `app/providers` and `app/collectors`
- **Priority:** P2
- **Type:** VERIFIED ISSUE
- **File/module:** `app/providers/email/imap.py:54`, `app/providers/email/smtp.py:55` (`from app.collectors.base import is_configured`) vs. `app/collectors/xing_email.py:72,77` (`from app.providers.email.imap_deadline import ...`, `from app.providers.email.mime_utils import decode_mime_part`)
- **Evidence:** as above — two packages import from each other.
- **Failure scenario:** currently masked by Python's module cache (whichever import happens first wins) and by both modules always being imported together in practice; a future refactor that changes import order or splits either module could surface a real `ImportError` from a genuine circular-import cycle.
- **Impact:** latent architectural fragility, not a current runtime bug.
- **Recommended direction:** move `is_configured` to `app/core` or `app/utils` so `providers` no longer needs to reach into `collectors`. Recorded in `docs/REFACTORING_BACKLOG.md`; not implemented this session (architecture-shape change, deferred to keep this branch documentation/deployment-focused).
- **Code must change:** yes, deferred.
- **Future Codex review mandatory:** recommended (low risk, but touches multiple modules' import graph).

### AUD-013 — `app/core` imports from `app/providers` (inverted layering)
- **Priority:** P3
- **Type:** VERIFIED ISSUE
- **File/module:** `app/core/config.py:6` (`from app.providers.email.base import (MAX_ADDRESS_LENGTH, ...)`)
- **Evidence:** as above.
- **Failure scenario:** none currently — `app/providers/email/base.py` has no further dependencies that would create an import cycle back to `core`. It is a layering smell (the intended-foundational `core` package depends on a mid-layer package) rather than a live bug.
- **Impact:** makes `app/core` harder to reason about as a dependency-free foundation; low urgency.
- **Recommended direction:** move the shared validation length constants into `app/core` or a shared constants module. Recorded, not implemented this session.
- **Code must change:** yes, deferred.
- **Future Codex review mandatory:** no.

### AUD-014 — `app/db` (repository layer) imports from `app/services` and `app/agents`
- **Priority:** P2
- **Type:** VERIFIED ISSUE
- **File/module:** `app/db/gmail_analysis_repository.py:41,50`, `app/db/follow_up_repository.py:23`, `app/db/repositories.py:29`
- **Evidence:** repositories import pure types/constants/functions (`ClassificationEvidenceItem`, `MATCH_CANDIDATE_SCAN_LIMIT`, `ThreadMessageInfo`, `extract_reference_tokens`, etc.) from the service layer above them.
- **Failure scenario:** none currently observed (these are pure, side-effect-free imports), but it inverts the intended dependency direction (repositories should sit beneath services, not depend on them), which risks a real circular import if a future service-layer change adds a DB-layer dependency into one of these same modules.
- **Impact:** architectural smell that increases coupling and risk for future changes; no current runtime defect.
- **Recommended direction:** relocate the shared pure types/constants into `app/domain` or `app/models`. Recorded in `docs/REFACTORING_BACKLOG.md`, not implemented this session.
- **Code must change:** yes, deferred.
- **Future Codex review mandatory:** recommended.

### AUD-015 — `app/providers` imports `app.db.models` (ORM) directly
- **Priority:** P3
- **Type:** VERIFIED ISSUE
- **File/module:** `app/providers/base.py`, `app/providers/job_data_provider.py`
- **Evidence:** provider abstraction modules import ORM records directly rather than `app/models` DTOs.
- **Failure scenario:** couples the provider contract surface to persistence internals; a schema change could ripple into provider interfaces unnecessarily.
- **Impact:** low — no current bug, just tighter coupling than ideal.
- **Recommended direction:** future refactor to depend on `app/models` DTOs instead. Recorded, not implemented.
- **Code must change:** yes, deferred.
- **Future Codex review mandatory:** no.

### AUD-016 — `app/api/routes.py` is a single 1860-line file with 118 duplicated-pattern `HTTPException` sites
- **Priority:** P2
- **Type:** VERIFIED ISSUE
- **File/module:** `app/api/routes.py`
- **Evidence:** 50 route functions, 118 `raise HTTPException(...)` sites, 85 `except` blocks translating domain exceptions, no `@app.exception_handler`. At least 12 of those 85 blocks are byte-identical duplicates of 4 patterns (`ReviewNotFoundError`→404, `CollectorNotConfiguredError`→503, `ResponseDraftNotFoundError`→404, `FollowUpProposalNotFoundError`→404 — see `docs/REFACTORING_BACKLOG.md` for exact line numbers).
- **Failure scenario:** none currently (behavior is correct, just repetitive) — the risk is maintenance drift: a new route copy-pasting an existing translation block could diverge (wrong status code, wrong message) without anyone noticing, since there is no single source of truth per exception type.
- **Impact:** maintainability/consistency risk, not a runtime defect.
- **Recommended direction:** staged introduction of centralized exception→HTTP translation (see `docs/REFACTORING_BACKLOG.md` for the proposed migration strategy). **Explicitly deferred** per this session's freeze instruction — not implemented.
- **Code must change:** yes, deferred, individually reviewed.
- **Future Codex review mandatory:** yes (explicitly flagged by the user as reserved for independent Codex review).

### AUD-017 — Largest orchestration functions (`run_xing`, `prepare_shortlist_drafts`, `run_bundesagentur`, `send_follow_up`) are 200+ LOC
- **Priority:** P3
- **Type:** VERIFIED ISSUE
- **File/module:** `app/services/collector_runner.py:433-691` (259 LOC), `app/services/automation_shortlist.py:159-371` (213 LOC), `app/services/collector_runner.py:225-430` (206 LOC), `app/services/follow_up_send.py:669-873` (205 LOC)
- **Evidence:** AST line-count scan, see `docs/REFACTORING_BACKLOG.md` for the full top-15 table and per-function analysis.
- **Failure scenario:** none currently — these are large but each was found to be a coherent, mostly-sequential orchestration of one business cycle with per-step error isolation already in place. Risk is reviewer/maintenance cost, not runtime correctness.
- **Impact:** cognitive load for future changes; higher review cost per PR touching these functions.
- **Recommended direction:** individually-reviewed decomposition per function (see backlog doc) — explicitly **not** attempted mechanically, since each function's extraction boundaries require domain judgment, not a generic split.
- **Code must change:** yes, deferred, individually reviewed.
- **Future Codex review mandatory:** yes (explicitly flagged by the user as reserved for independent Codex review).

### AUD-018 — `XingEmailCollector` uses a full-access IMAP App Password, not an isolated mailbox
- **Priority:** P2
- **Type:** THEORETICAL LIMITATION (pre-existing, already accepted)
- **File/module:** `app/collectors/xing_email.py`
- **Evidence:** documented as an accepted v1 compromise in this project's `CLAUDE.md` ("Known security-relevant risk"): read-only `SELECT`, no message mutation, but the credential grants broader mailbox access than the collector uses.
- **Failure scenario:** if the App Password were ever leaked, the blast radius is the whole mailbox, not just job-alert messages.
- **Impact:** already a known, deliberately-accepted risk — repeated here only so the production-readiness audit is complete; **not to be "fixed" opportunistically** per explicit project instruction.
- **Recommended direction:** a dedicated/isolated mailbox for job alerts is future work, not in scope here.
- **Code must change:** no (out of scope by explicit instruction).
- **Future Codex review mandatory:** n/a — pre-accepted.

### AUD-019 — No CI Docker build verification existed before this branch
- **Priority:** P2
- **Type:** DEPLOYMENT REQUIREMENT
- **File/module:** `.github/workflows/ci.yml`
- **Evidence:** two jobs exist (`test`, `scheduler-postgres`); neither builds or validates a Docker image.
- **Failure scenario:** a Dockerfile could silently bit-rot (e.g. after a dependency bump) with no CI signal until an actual deployment attempt fails.
- **Recommended direction:** add a lightweight `docker compose config` + `docker build` CI gate (Phase 10). Implemented in this branch if safe/low-complexity — see final report.
- **Code must change:** yes (CI-only).
- **Future Codex review mandatory:** no.

### AUD-020 — No upper bound on Python version in `pyproject.toml`
- **Priority:** P3
- **Type:** THEORETICAL LIMITATION
- **File/module:** `pyproject.toml:9` (`requires-python = ">=3.11"`)
- **Evidence:** CI pins `python-version: "3.13"`; no `<4` or similar upper bound is declared.
- **Failure scenario:** a future Python release with a breaking change could be installed by a user without CI's guardrail, though this is a common and generally low-risk pattern.
- **Impact:** minor reproducibility risk.
- **Recommended direction:** Docker image pins `python:3.13-slim` explicitly (this branch) as the deployment-time guardrail; no `pyproject.toml` change (out of scope — `pyproject.toml` must not be touched this session regardless).
- **Code must change:** no (deliberately not touching `pyproject.toml`).
- **Future Codex review mandatory:** no.

---

## Summary table

| ID | Priority | Type | Area |
|---|---|---|---|
| AUD-001 | P1 | DEPLOYMENT REQUIREMENT | Docker (addressed this branch) |
| AUD-002 | P1 | VERIFIED ISSUE | `/health` no dependency check (deferred) |
| AUD-003 | P1 | VERIFIED ISSUE | rate limiter reverse-proxy IP (deferred) |
| AUD-004 | P1 | SCALABILITY CONCERN | rate limiter multi-worker (documented) |
| AUD-005 | P2 | VERIFIED ISSUE | rate limiter unbounded dict (deferred) |
| AUD-006 | P2 | THEORETICAL LIMITATION | API key non-constant-time compare (deferred) |
| AUD-007 | P1 | VERIFIED ISSUE | no `pool_pre_ping` (**fixed this branch**) |
| AUD-008 | P2 | VERIFIED ISSUE | scheduler no SIGTERM handler (**fixed this branch**) |
| AUD-009 | P3 | DOCUMENTATION GAP | no startup diagnostics log (deferred) |
| AUD-010 | P2 | DOCUMENTATION GAP | no correlation ID (deferred) |
| AUD-011 | P2 | DOCUMENTATION GAP | Postgres not yet field-proven (documented) |
| AUD-012 | P2 | VERIFIED ISSUE | providers↔collectors circular import (backlog) |
| AUD-013 | P3 | VERIFIED ISSUE | core→providers inverted layering (backlog) |
| AUD-014 | P2 | VERIFIED ISSUE | db→services/agents inverted layering (backlog) |
| AUD-015 | P3 | VERIFIED ISSUE | providers→db.models coupling (backlog) |
| AUD-016 | P2 | VERIFIED ISSUE | routes.py duplication (backlog, Codex-reserved) |
| AUD-017 | P3 | VERIFIED ISSUE | large orchestration functions (backlog, Codex-reserved) |
| AUD-018 | P2 | THEORETICAL LIMITATION | XING IMAP credential scope (pre-accepted) |
| AUD-019 | P2 | DEPLOYMENT REQUIREMENT | no CI Docker gate (addressed this branch) |
| AUD-020 | P3 | THEORETICAL LIMITATION | no Python upper bound (documented) |

---

## Production Readiness Score

> Scored against the audited HEAD **before** this branch's fixes were applied. See the "AFTER" score in the final report for the effect of AUD-007/AUD-008 plus the documentation/Docker additions.

| Dimension | Score (0-10) | Rationale |
|---|---|---|
| Architecture | 6.5 | Clear layered intent (API→services→agents/providers/collectors→db), consistently followed at the top level; several inverted-dependency smells (AUD-012–015) but no current runtime bugs from them. |
| Correctness / testing | 9 | 1934 passing tests, 4 skipped, 0 failed; real PostgreSQL concurrency integration tests in CI; Alembic-head CI gate proves the migration chain actually works, not just `create_all`. |
| Database / persistence | 7.5 | Strong CAS/lease/idempotency design (verified via dedicated concurrency tests); missing `pool_pre_ping` (AUD-007) and no engine-level resilience tuning documented. |
| Security | 6 | Fail-closed auth and secret handling are solid; rate limiter has real gaps once fronted by a reverse proxy or run multi-worker (AUD-003/004/005); API key compare is timing-observable (AUD-006, low real-world impact). |
| Concurrency | 8.5 | Automation/scheduler overlap prevention is DB-enforced (partial unique index + lease + CAS), not just app-level convention; well tested against both SQLite and PostgreSQL. |
| External integrations | 8.5 | IMAP/SMTP deadline handling, DNS-resolution process isolation, retryable-vs-permanent classification, and human-approval-gated sends are all deliberately engineered and documented. |
| Observability | 5 | Structured JSON logging exists; no request correlation ID, no dependency-aware health check, no startup diagnostics line. |
| Deployment | 3 → 7 (after this branch) | No Docker/compose existed before this session; single-process scheduler-separation is already correctly designed in the app itself. |
| Documentation | 4 → 8 (after this branch) | Original README was a 2110-line stage-by-stage development log (valuable but not recruiter/operator-friendly); no architecture, deployment, security-model, or runbook docs existed before this branch. |
| Operational resilience | 6.5 | Crash-safe by design (lease/CAS), but no SIGTERM handling in the scheduler (AUD-008, fixed this branch) and no health-check depth (AUD-002, deferred). |

**PRODUCTION READINESS (before this branch): ≈ 64/100**
**PRODUCTION READINESS (after this branch's safe fixes + Docker + docs): ≈ 74/100** — see final report for the precise delta and what remains outstanding (routes.py centralization, rate-limiter proxy-awareness, and health-check depth are the three biggest remaining levers, all explicitly deferred to reviewed follow-up work).

### Deployment-safety matrix

| Scenario | Safe? | Why |
|---|---|---|
| Local development | **YES** | SQLite default, no external calls required except opt-in collectors/Telegram; extensively tested. |
| Portfolio / demo use | **YES** | Deterministic generation, no real outbound side effects unless explicitly configured and approved; human-approval gates protect against accidental real sends. |
| Single-user local real use | **YES** | The application's core safety invariants (approval-gated sends, provenance/trust gating, dedup/idempotency) are enforced in code and tested, not just documented. |
| Single-server private deployment | **YES, with caveats** | Requires running migrations explicitly (`ALEMBIC_AUTO_UPGRADE=false` in production, by design), a single scheduler process (by design), and applying AUD-007/AUD-008 (done this branch). Not yet battle-tested on PostgreSQL in a real long-running deployment (AUD-011) — treat the first weeks as monitored. |
| Public internet exposure | **NO** | AUD-002 (health check doesn't verify dependencies), AUD-003 (rate limiter collapses behind a reverse proxy), AUD-006 (non-constant-time key compare), and the absence of a WAF/DDoS layer make this unsuitable for direct public exposure without further hardening. A private/VPN-gated single-server deployment is the appropriate initial target (see `docs/DEPLOYMENT.md`). |
| Multi-worker deployment | **NO** | AUD-004: the in-memory rate limiter is not multi-worker safe (effective limit multiplies by worker count); the scheduler must also remain a single separate process regardless of worker count (already enforced by design, not a gap). Single-worker (`--workers 1`) is the only currently-safe FastAPI process count. |

---

## Notes on scope and honesty

- This audit did **not** re-examine FINAL-001/003/004 semantics — those are frozen per explicit instruction and were the subject of prior, dedicated review rounds.
- Every finding above traces to specific file:line evidence gathered via direct code reading (three parallel read-only research passes) and independently spot-verified (`app/security/auth.py`, `app/api/routes.py` health route) before being written here.
- No finding was invented to pad the list; several plausible-sounding candidates (e.g. "does the DB engine get disposed on shutdown") were investigated and explicitly noted as non-issues rather than reported as findings, where the evidence didn't support real impact.
