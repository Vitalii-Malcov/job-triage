# Overnight code quality report — main @ 5834fe7

Evidence base: static reading of current code, `ruff check`/`format --check` (clean, 676 files), 213 telegram/hardening tests run locally
(pass), CI green on every merged PR incl. PostgreSQL and Docker jobs, the prior audits (see `OVERNIGHT_ISSUES_REVALIDATION.md`).
**Not done:** the full local pytest (~hours; CI is the authority), mypy, pip-audit, a live Docker run, a PostgreSQL run by me.

Size: ~70.7 k lines in `app/` (≈80 modules in `services/`), 269 test files, ~5 000 test functions (CI reports ~9 000 with parametrization), 55 Alembic revisions, 79 docs.

| Area | Score | Evidence |
|---|---|---|
| Architecture | 7 | Clear layers (api/routers → services → db repositories/models), collectors behind a base interface, provider Protocols for IMAP, import-graph tests that enforce the "no employer outbound" invariant. Minus: `repositories.py` ~1 800 lines, services layer very wide, history-driven script names (`stage22_refresh_ba.py`). `routes.py` is now a 44-line aggregator and models are a package (earlier God files fixed). |
| Readability | 6 | Modules are focused, but comment density is high and much of it is review history (ticket ids, "Codex"/"Astra") — noise for a reader. |
| Python practices | 7 | Ruff clean, typed Pydantic models, `SecretStr`, `hmac.compare_digest`, StrEnum. No type checker in CI (CQ-04), engine created at import (ARC-09). |
| FastAPI | 7 | Dependency-based auth + per-route limiter, ids bounded, docs off by default, `run_off_loop` for sync work, route-auth matrix test. Minus: error shapes not unified (API-05), no OpenAPI snapshot test. |
| Database | 7 | Alembic with migration-vs-model tests, append-only DB triggers, FK pragma, PostgreSQL CI job, leases/CAS. Minus: SQLite without WAL in production, some missing FKs (DB-03), unindexed `last_seen_at`. |
| Security | 8 | Fail-closed outbound flags, constant-time auth, masked secrets, loopback binding, session revocation, outbound-safety matrix tests, SEC-04 link-preview closed tonight. Open: weak production API key (operator), accepted full-access mailbox credential (documented). |
| Testing | 8 | ~9 k cases, adversarial inputs, concurrency/lease tests, import-graph and outbound matrices, PG tests in CI. Minus: tests that grep source strings, very large files, ~1 h CI, no coverage gate evidence. |
| Maintainability | 6 | Strong documentation of invariants but heavy: 79 docs, many stage reports; changes require reading a lot of history. |
| Documentation | 7 | README status was stale (fixed tonight), architecture/security/runbook docs good; missing LICENSE, docs index. |
| Production readiness | 6 | Safe defaults and backup tooling; but single-maintainer operations, SQLite, no alerting/supervision (OPS-02), weak prod key pending rotation. |

## Is this a convincing Middle Python Backend portfolio?
**Yes for engineering rigor (security, testing, migrations, CI), with caveats a reviewer will notice:** the project is unusually process-heavy
(stage reports, reviewer names inside code comments), is a single-user tool, and "AI agents" in package names are regex matchers (README says so).
Minimum plan: (1) strip reviewer/ticket history from code comments and add a docs index (S–M), (2) add mypy to CI on `app/security`, `app/core` first (S),
(3) add LICENSE (owner choice) and an OpenAPI snapshot test (S), (4) split `repositories.py` by aggregate (M). No scores were inflated; Production readiness 6 reflects operator-side gaps.

## Fixed tonight
SEC-04 (Telegram link previews disabled, test added), README status table, Dependabot config, CONTRIBUTING.md. No P0/P1 defect found open that can be fixed without the operator.
