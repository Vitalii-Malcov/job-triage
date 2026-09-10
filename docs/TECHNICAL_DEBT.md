# Technical Debt Master Backlog — JobTriage

Consolidates every known outstanding item as of `chore/production-readiness-r1`, including pre-existing debt already tracked in `CLAUDE.md` and new findings from this session's audit (`docs/PRODUCTION_READINESS_AUDIT.md`, `docs/ARCHITECTURE.md`, `docs/REFACTORING_BACKLOG.md`).

Complexity: **S** (< 1 hour), **M** (< 1 day), **L** (multi-day), **XL** (needs its own dedicated review round, like FINAL-001/003/004 were).

| ID | Item | Priority | Impact/Risk | Complexity | Reviewer |
|---|---|---|---|---|---|
| TD-001 | `app/api/routes.py` centralized exception→HTTP translation (≥12 confirmed duplicate blocks of 85) | P2 | Maintainability/consistency drift risk, no current runtime bug | L | Codex (explicitly reserved) |
| TD-002 | Individually-reviewed decomposition of `run_xing` (259 LOC), `run_bundesagentur` (206), `prepare_shortlist_drafts` (213), `send_follow_up` (205) | P3 | Maintenance/review cost; `send_follow_up` touches the send-safety boundary | XL | Codex, and likely Astra for `send_follow_up` |
| TD-003 | In-memory rate limiter: no `X-Forwarded-For` trust behind reverse proxy | P1 | Rate limiting becomes ineffective once proxied (AUD-003) | M | Codex (security-relevant) |
| TD-004 | In-memory rate limiter: not multi-worker safe | P1 | Effective limit multiplies by worker count if scaled (AUD-004) | L (needs shared store, e.g. Redis) | Codex |
| TD-005 | Rate limiter host-key dict growth is unbounded | P2 | Slow memory growth on long-lived, publicly-hit processes (AUD-005) | S | Codex |
| TD-006 | `/health` does not check DB connectivity | P1 | False-positive health signal (AUD-002) | M (public API shape change) | Codex |
| TD-007 | API key comparison uses `!=` not `hmac.compare_digest` | P2 | Theoretical timing side-channel (AUD-006) | S | Codex (touches auth boundary) |
| TD-008 | No correlation/request ID in logs | P2 | Harder multi-line request tracing at scale (AUD-010) | M | Manual/Claude (low risk) |
| TD-009 | No startup diagnostics log line | P3 | Minor operational friction (AUD-009) | S | Manual/Claude |
| TD-010 | `providers` ↔ `collectors` circular import | P2 | Latent fragility, no current bug (AUD-012) | M | Codex (touches multiple modules) |
| TD-011 | `app/core` imports `app/providers` (inverted layering) | P3 | Layering smell (AUD-013) | S | Manual/Claude |
| TD-012 | `app/db` imports `app/services`/`app/agents` (inverted layering) | P2 | Coupling risk for future changes (AUD-014) | M | Codex |
| TD-013 | `app/providers` imports `app.db.models` (ORM) directly | P3 | Provider contract coupled to persistence internals (AUD-015) | M | Manual/Claude |
| TD-014 | Bundesagentur collector: no overall timeout/circuit-breaker on `_collect_pages` | P2 | Worst case can block a worker + DB session for minutes | M | Codex |
| TD-015 | Bundesagentur collector: non-401 4xx retried like 429/5xx | P2 | Malformed query param retried instead of failing fast | S | Codex |
| TD-016 | Bundesagentur collector: injected `http_client` never closed | P3 | Real leak pattern if copied for a future collector against a real transport | S | Manual/Claude |
| TD-017 | Bundesagentur collector: negative `maxErgebnisse` silently terminates pagination | P3 | Silent under-collection, no warning logged | S | Manual/Claude |
| TD-018 | Bundesagentur collector: `radius_km=0` treated as unset (`if self.radius_km:`) | P3 | Silently drops `umkreis` param for an explicit 0 | S | Manual/Claude |
| TD-019 | Bundesagentur collector: `refnr` not URL-encoded in `JOB_DETAIL_URL_TEMPLATE` | P2 | Malformed URL for refnr values with special characters | S | Codex (verify no injection surface) |
| TD-020 | Bundesagentur collector: auth vs. API errors both map to HTTP 502 | P3 | Broken stored key indistinguishable from transient outage in logs | S | Manual/Claude |
| TD-021 | Bundesagentur collector: no test for `fetch(since=...)` day-window logic | P3 | Zero production call sites today; untested path | S | Claude (add test) |
| TD-022 | Bundesagentur collector: `url` fallback has same fingerprint-instability structure as the (already-fixed) `title` bug | P2 | Possible duplicate `JobRecord` inserts if `externeURL` intermittently present/absent | M | Codex (needs live-data-driven audit, per existing note) |
| TD-023 | XING mailbox uses full-access IMAP App Password, not an isolated mailbox | P2 | Broader blast radius than needed if credential leaks | L (needs dedicated mailbox + migration) | Manual/operational — explicitly NOT to be fixed opportunistically |
| TD-024 | PostgreSQL not yet field-tested in a real long-running production deployment | P2 | Unknown-unknowns from sustained real-world load (AUD-011) | — (operational, not code) | Manual operational verification |
| TD-025 | No health-check/readiness depth beyond a static `/health` | P1 | Same root cause as TD-006 | M | Codex |
| TD-026 | No backup/restore automation (procedure documented, not automated) | P2 | Manual step, human-error-prone | M | Manual/Claude (could script `docs/RUNBOOK.md`'s procedure later) |
| TD-027 | Dependency reproducibility: no upper Python version bound, ranged (not pinned) dependencies | P3 | Low risk given CI pins 3.13 and Docker image pins `python:3.13-slim` | S | Manual (deliberately not touching `pyproject.toml`) |
| TD-028 | `app/scheduler.py` had no SIGTERM handler | P2 | Ungraceful container shutdown (log noise only, correctness preserved by lease design) | S | **FIXED this branch** (AUD-008) |
| TD-029 | No `pool_pre_ping`/pool sizing on the DB engine | P1 | Stale-connection errors after DB restarts under PostgreSQL | S | **FIXED this branch** (AUD-007) |
| TD-030 | No Docker/deployment artifacts existed | P1 | No repeatable deployment path | L | **FIXED this branch** (AUD-001) |
| TD-031 | No CI Docker build/config gate | P2 | Dockerfile could silently bit-rot | M | **FIXED this branch** (AUD-019) |
| TD-032 | `compose.yaml` scheduler `restart: unless-stopped` restart-loops when both automation flags are disabled | P2 | Log noise, wasted restarts, confusing operator signal | S | **FIXED this branch** (found during local Docker smoke test, changed to `restart: on-failure`) |

## Notes

- Items marked "FIXED this branch" were implemented under Phase 17's safe-change criteria: deployment/configuration-only, no schema change, no business-semantics change, no change to outbound/approval/trust/concurrency behavior.
- TD-001/TD-002 are explicitly reserved for independent Codex review per direct user instruction — not to be implemented speculatively even if they look mechanically safe.
- TD-014 through TD-022 (Bundesagentur collector) are carried forward unchanged from `CLAUDE.md`'s existing "Known tech debt" section — not re-investigated or re-scoped in this session, only consolidated here for a single point of reference.
- TD-023 (XING mailbox credential scope) is carried forward from `CLAUDE.md`'s "Known security-relevant risk" section — explicitly not to be fixed opportunistically.
