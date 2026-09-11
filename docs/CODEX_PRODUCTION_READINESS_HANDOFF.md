# Codex Independent Review Handoff — Production Readiness Delta

**BASE:** `f88b84abd38a383a42c62d30e6306e1fcec76513` (frozen `refactor/project-cleanup-r1`)
**HEAD:** `95191824baf8c7f985cc0289998359a52f486621` (`chore/production-readiness-r1`, frozen, not merged)

This document covers **only the delta** between BASE and HEAD. It does not re-litigate anything already reviewed on `refactor/project-cleanup-r1` or earlier. Full narrative detail lives in `docs/PRODUCTION_READINESS_AUDIT.md`, `docs/ARCHITECTURE.md`, `docs/REFACTORING_BACKLOG.md`, `docs/DEPLOYMENT.md`, `docs/DOCKER.md`, `docs/SECURITY_MODEL.md`, `docs/TECHNICAL_DEBT.md` — this handoff is a compact index into those, not a replacement for them.

---

## 1. Commits, in order

1. `e11c799` — fix: harden scheduler shutdown and DB engine resilience (AUD-007/AUD-008)
2. `2b16903` — docs: add production readiness audit, architecture map, and refactoring backlog
3. `893eaa9` — chore: add Docker deployment support
4. `a5e2099` — ci: add Docker build/config validation gate
5. `9519182` — docs: restructure README and add security, runbook, interview, and portfolio guides

---

## 2. Changed files, grouped

### Application code (2 files — the only behavioral changes in this delta)
- `app/db/session.py` — added `pool_pre_ping=True` to the engine.
- `app/scheduler.py` — added a `SIGTERM` handler (`_handle_sigterm` / `_ShutdownRequested`), a new import (`signal`), and one new `except` branch in `main()`.

### Docker / deployment (7 files, all new)
- `Dockerfile`
- `.dockerignore`
- `compose.yaml`
- `docker/entrypoint.sh`
- `docker/README.md`
- `.env.example` (modified — appended `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`/`WEB_PORT` placeholders only; no existing lines changed)

### CI (1 file, modified)
- `.github/workflows/ci.yml` — one new job (`docker`) appended; the two pre-existing jobs (`test`, `scheduler-postgres`) are byte-for-byte unchanged.

### Documentation (13 files: 12 new, 1 rewritten)
- New: `docs/PRODUCTION_READINESS_AUDIT.md`, `docs/ARCHITECTURE.md`, `docs/REFACTORING_BACKLOG.md`, `docs/DEPLOYMENT.md`, `docs/DOCKER.md`, `docs/SECURITY_MODEL.md`, `docs/RUNBOOK.md`, `docs/TECHNICAL_DEBT.md`, `docs/INTERVIEW_GUIDE.md`, `docs/PORTFOLIO_NOTES.md`, `docs/DEVELOPMENT_HISTORY.md`, this file.
- Rewritten: `README.md` (original 2110-line content moved verbatim into `docs/DEVELOPMENT_HISTORY.md`, not lost).

No files outside these three categories (application code, Docker/deployment, CI) plus documentation were touched. In particular: **zero changes** under `alembic/`, `tests/`, `app/api/`, `app/services/`, `app/agents/`, `app/providers/`, `app/collectors/`, `app/security/`, `app/domain/`, `app/models/`.

---

## 3. Highest-risk review targets

### `app/db/session.py` — `pool_pre_ping=True`
Full diff (3 lines of comment + 1 changed line):
```python
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
```
No other line in the file changed. `connect_args` logic (SQLite `check_same_thread` conditional) is untouched.

### `app/scheduler.py` — SIGTERM handling
New: `import signal`; a new exception class `_ShutdownRequested`; a new function `_handle_sigterm(signum, frame)` that raises it; one line in `main()` registering `signal.signal(signal.SIGTERM, _handle_sigterm)` (placed immediately after `configure_logging()`, before settings are loaded); one new `except _ShutdownRequested:` branch alongside the pre-existing `except KeyboardInterrupt:` branch around `asyncio.run(_poll_loop(settings))`, logging `automation_scheduler_stopped_sigterm` instead of `automation_scheduler_stopped_keyboard_interrupt`. `_poll_loop` itself, all automation-cycle logic, all lease/CAS logic — untouched.

### `Dockerfile`
New file. Single-stage, `python:3.13-slim` base, `pip install ".[postgres]"` (non-editable), non-root `appuser` (uid 1000), `COPY --chmod=755` for the entrypoint, `ENTRYPOINT ["/entrypoint.sh"]` + default `CMD` running uvicorn with `--workers 1`.

### `compose.yaml`
New file. Three services (`db`, `web`, `scheduler`); `scheduler` is profile-gated (`profiles: ["scheduler"]`) and uses `restart: on-failure` (not `unless-stopped` — see §4, this was a finding from the smoke test, not a guess); `web`/`db` use `restart: unless-stopped`; `depends_on: db: condition: service_healthy` gates `web`/`scheduler` startup; named volume `pgdata`; `ALEMBIC_AUTO_UPGRADE: "false"` is set explicitly (not just inherited) in both `web` and `scheduler` environments.

### `docker/entrypoint.sh`
New file, 9 lines total. `set -e; exec "$@"` — no wait-loop, no retry logic, no swallowed exit codes.

### `.github/workflows/ci.yml`
One new job appended (`docker:`), 30 lines. Builds the image, writes a throwaway `.env` (placeholder values only, removed at job end via `if: always()`), runs `docker compose config` for both the default and `scheduler` profile. Does not start containers, run migrations, or contact external services.

---

## 4. Docker smoke-test evidence (exact, reproduced from this session's real command output)

All commands run against Docker Desktop (`docker compose v5.4.0`) on the local dev machine, using a temporary, gitignored `.env` with placeholder secrets (`validation-only-key` / `validation-only-pw`), deleted immediately after each test run — never committed.

| Step | Command | Result |
|---|---|---|
| Compose config validity | `docker compose config` | Exit 0; resolved services/env/healthchecks/volumes as expected. |
| Scheduler profile visibility | `docker compose --profile scheduler config --services` | Listed `db`, `scheduler`, `web`. |
| Image build | `docker compose build web` | Succeeded; `pip install ".[postgres]"` resolved cleanly; non-root user created; entrypoint copied with exec bit. |
| Postgres startup | `docker compose up -d db` | Reached `healthy` via `pg_isready`. |
| Migrations against real Postgres | `docker compose run --rm web alembic upgrade head` | All 30 migrations applied in order, ending at `c7d3f9a1e5b8`. |
| Web startup | `docker compose up -d web` | Reached `healthy`; log line `Uvicorn running on http://0.0.0.0:8000`; `telegram_bot_disabled reason=no_bot_token_configured` (correct — no token configured). |
| Host reachability | `curl http://localhost:8000/api/v1/health` | `200 OK`. |
| Auth enforcement inside container | `curl http://localhost:8000/api/v1/jobs` (no API key) | `401 Unauthorized`. |
| Clean shutdown — web | `time docker compose stop web` | Stopped in ~3.256s, **exit code 0**. Log tail: `Shutting down` → `Waiting for application shutdown.` → `Application shutdown complete.` → `Finished server process [1]`. |
| Scheduler enabled + real SIGTERM | Started `scheduler` with `AUTOMATION_SCHEDULER_ENABLED=true`, let it complete one poll tick (logged `automation_run_finished run_id=1 status=FAILED` — expected, no real collector credentials configured), then `time docker compose stop scheduler` | Stopped in ~3.206s, **exit code 0**, log line `automation_scheduler_stopped_sigterm` present. |
| Scheduler disabled (both flags false) | `docker compose --profile scheduler up -d scheduler` | Logged `automation_scheduler_disabled`, exited `0` — **this run surfaced the `restart: unless-stopped` restart-loop finding**, fixed by switching to `restart: on-failure` (see §2/§3, `compose.yaml`). |
| Teardown | `docker compose --profile scheduler down`, `docker volume rm ai_agent_pgdata` | Clean removal, no leftover containers/networks. |

No real external service (IMAP, SMTP, Bundesagentur, Telegram) was contacted at any point — all credential-requiring features stayed in their default-disabled state.

---

## 5. Full pytest / Ruff / Alembic results (this delta, post-fix)

- **Full pytest:** `1934 passed, 4 skipped, 0 failed, 1 warning in 916.94s (0:15:16)` — run via a detached OS-level background process (`python -m pytest -q`) after both `app/db/session.py` and `app/scheduler.py` changes were applied. Identical pass/skip counts to the pre-change baseline on `refactor/project-cleanup-r1` (`1934 passed, 4 skipped`) — no regressions.
- **Ruff check:** `python -m ruff check app tests alembic` → `All checks passed!`
- **Ruff format --check:** `python -m ruff format --check app tests alembic` → `223 files already formatted`
- **Alembic heads:** `python -m alembic heads` → `c7d3f9a1e5b8 (head)` — matches the expected head; also independently confirmed via a live `alembic upgrade head` run against a real PostgreSQL container in the Docker smoke test (§4).

Targeted pre-checks before the full run: `pytest -q tests/test_scheduler_entrypoint.py tests/test_scheduler_independence.py tests/test_scheduler_poll_loop.py tests/test_scheduler_service.py tests/test_telegram_daily_digest_scheduler.py` → `59 passed`.

---

## 6. Explicitly NOT changed in this delta

- **DB schema / migrations** — zero files under `alembic/` touched; migration count remains 30, head remains `c7d3f9a1e5b8`.
- **Approval logic** — `app/services/response_draft.py`, `app/services/follow_up.py`, `app/services/review_package.py` untouched.
- **Outbound-send logic** — `app/services/response_draft_send.py`, `app/services/follow_up_send.py`, `app/providers/email/smtp.py` untouched.
- **Trust/provenance rules** — `TRUSTED_JOB_SOURCES`, `is_top_level_fact_usable_for_generation`, `app/models/candidate_profile.py` untouched.
- **R5A FINAL-001/003/004** — `app/providers/email/imap_deadline.py` untouched (not present anywhere in this delta's diff).
- **`pyproject.toml`** — never staged, never committed; the file's pre-existing local (uncommitted, unrelated) modification remains exactly as it was at session start, still not part of any commit on this branch.
- **`app/api/routes.py`** — untouched (the ≥12 duplicated exception-translation blocks identified in `docs/REFACTORING_BACKLOG.md` remain exactly as they were).
- **Large orchestration functions** (`run_xing`, `run_bundesagentur`, `prepare_shortlist_drafts`, `send_follow_up`, etc.) — untouched.

---

## 7. Known unresolved production findings (carried forward, not fixed in this delta)

| ID | Summary | Priority |
|---|---|---|
| AUD-002 | `/health` does not check DB connectivity — false-positive health signal possible | P1 |
| AUD-003 | Rate limiter has no reverse-proxy client-IP awareness — collapses to one bucket behind a proxy | P1 |
| AUD-004 | In-memory rate limiter is per-process — not multi-worker safe | P1 |
| AUD-006 | API key comparison uses `!=`, not `hmac.compare_digest` — theoretical timing side-channel | P2 |
| AUD-012 | Circular import between `app/providers` and `app/collectors` | P2 |
| AUD-014 | `app/db` (repository layer) imports from `app/services`/`app/agents` — inverted layering | P2 |
| AUD-016 | `app/api/routes.py` — 1860 lines, 118 `HTTPException` sites, ≥12 confirmed byte-identical duplicated exception-translation blocks, no centralized handler | P2 |
| AUD-017 | Largest orchestration functions (`run_xing` 259 LOC, `prepare_shortlist_drafts` 213, `run_bundesagentur` 206, `send_follow_up` 205) — decomposition candidates, none attempted | P3 |

Full evidence, failure scenarios, and recommended direction for each: `docs/PRODUCTION_READINESS_AUDIT.md`. AUD-016 and AUD-017 are explicitly reserved for independent Codex review per prior project instruction, not for opportunistic implementation.

---

## 8. Recommended Codex review questions per high-risk file

**`app/db/session.py`**
- Does `pool_pre_ping=True` interact correctly with the existing SQLite `connect_args={"check_same_thread": False}` branch, or could the added kwarg change SQLite pooling behavior in a way the test suite wouldn't catch (e.g. `NullPool` vs `QueuePool` defaults differing by dialect)?
- Is there any code path that constructs its own engine separately from this module (bypassing `pool_pre_ping`) that should also get it for consistency?

**`app/scheduler.py`**
- Does registering `signal.signal(signal.SIGTERM, _handle_sigterm)` before `get_settings()` is called risk raising `_ShutdownRequested` from a code path not wrapped in the `try/except` around `asyncio.run(...)` (i.e., during the synchronous settings-validation phase before the loop starts), and if so, is an unhandled `_ShutdownRequested` there acceptable (process exits non-zero) or should the handler/try-scope be widened?
- Is raising an exception from a synchronous `signal.signal()` handler while inside `asyncio.run()`'s event loop guaranteed safe across the Python versions this project supports (3.11+), or could it interrupt at an unsafe point (e.g. mid-`await db.close()`) leaving a session in an inconsistent state? The existing `KeyboardInterrupt`-via-SIGINT default handler uses the identical mechanism — does that precedent fully cover this, or does `KeyboardInterrupt`'s C-level implementation differ from a Python-level `signal.signal()` callback in ways that matter here?

**`Dockerfile`**
- Is running `pip install` as root before `USER appuser` a concern beyond the standard pip warning (e.g. files installed under a path `appuser` can't later write to, if any runtime write were ever needed)?
- Should `COPY --chmod=755` be trusted as available in the CI runner's Docker version, or does it need a BuildKit-availability guard?

**`compose.yaml`**
- Does `env_file: .env` combined with an explicit `environment:` override for `DATABASE_URL`/`ALEMBIC_AUTO_UPGRADE` correctly win in all Compose versions this project might run under, or is there a version where `env_file` and `environment` precedence differs from what's assumed here?
- Is `restart: on-failure` for `scheduler` (vs. `unless-stopped`) the right call long-term, or should it have a `max_attempts` bound to avoid a crash-loop if the scheduler fails immediately on every start for a persistent reason (bad credentials, unreachable DB)?

**`docker/entrypoint.sh`**
- Is a bare `exec "$@"` sufficient, or is there a container-signal edge case (e.g. `docker compose run` vs `docker compose up`, TTY vs non-TTY) where PID 1 signal handling behaves differently than what was smoke-tested?

**`.github/workflows/ci.yml`**
- Does the throwaway `.env` written by the new `docker` job risk masking a real `.env` if this job is ever run in a context where one already exists (e.g. a future self-hosted runner with persistent workspace)? Should the step check for and refuse to overwrite a pre-existing `.env` instead of unconditionally `cp`-ing over it?

---

## 9. Proposed merge gate

Recommend merge is blocked on:
1. Codex sign-off on `app/scheduler.py`'s SIGTERM-during-asyncio-event-loop safety question (§8) — the only genuinely novel *runtime behavior* change in this delta.
2. Confirmation that `pool_pre_ping=True` has no unexpected interaction with SQLite's engine configuration (§8) — low risk, but the only other behavioral code change.
3. No merge-blocking requirement on the Docker/CI/documentation files themselves (infrastructure-only, already locally validated with real command evidence in §4) — but Codex should skim `compose.yaml`'s `restart`/`depends_on`/`profiles` semantics for anything this session's manual testing might not have exercised (e.g. behavior under `docker compose up` with no prior `db` container, cold-start ordering races not covered by the smoke test's step-by-step manual sequencing).
4. AUD-002/003/004/006/012/014/016/017 (§7) are **not** merge blockers for this branch — they are pre-existing or explicitly-deferred findings, not introduced by this delta, and are already tracked in `docs/TECHNICAL_DEBT.md` for separate, individually-reviewed follow-up work.

This branch remains **frozen, unmerged** pending the above.
