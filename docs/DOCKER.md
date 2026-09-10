# Docker Plan — JobTriage

Written before implementation, based on direct inspection of the runtime (not assumptions).

## Local validation performed (2026-09-11)

Docker was available locally (Docker Desktop, `docker compose v5.4.0`). A full smoke test was run against a **temporary, gitignored `.env`** with placeholder secrets (`validation-only-key`/`validation-only-pw`), deleted immediately after the test — never committed.

| Step | Command | Result |
|---|---|---|
| Compose config validity | `docker compose config` | Passed — resolved services, env, healthchecks, volumes as expected. |
| Scheduler profile visibility | `docker compose --profile scheduler config --services` | `db`, `scheduler`, `web` all listed. |
| Image build | `docker compose build web` | Succeeded — `pip install ".[postgres]"` resolved and installed cleanly on `python:3.13-slim`, non-root `appuser` created, entrypoint copied with exec bit. |
| Postgres startup + healthcheck | `docker compose up -d db` | Container reached `healthy` via `pg_isready`. |
| Migrations against real Postgres | `docker compose run --rm web alembic upgrade head` | All 30 migrations applied in order, ending at `c7d3f9a1e5b8` — the same head CI's `scheduler-postgres` job asserts. |
| Web container startup | `docker compose up -d web` | Reached `healthy` via the `/api/v1/health` healthcheck; logs show clean structured-JSON startup (`telegram_bot_disabled reason=no_bot_token_configured` — correct, no token configured) and `Uvicorn running on http://0.0.0.0:8000`. |
| Host reachability | `curl http://localhost:8000/api/v1/health` | `200 OK`. |
| Auth behavior | `curl http://localhost:8000/api/v1/jobs` (no API key) | `401 Unauthorized` — confirms `require_api_key` is active inside the container, not accidentally bypassed. |
| Clean shutdown | `docker compose stop web`, timed | Stopped in ~3.3s, **exit code 0** — confirms the `exec "$@"` entrypoint correctly passes SIGTERM straight to uvicorn (PID 1), which shuts down gracefully rather than being killed after a timeout. |
| Scheduler container smoke test | `docker compose --profile scheduler up -d scheduler` | Started, logged `automation_scheduler_disabled` and exited `0` (both scheduler/digest flags are `false` by default) — **this surfaced a real finding**: with `restart: unless-stopped`, a disabled scheduler exits `0` and restart-loops forever. Fixed in this branch by changing `scheduler`'s restart policy to `on-failure` (see `compose.yaml` and `docs/DEPLOYMENT.md`) — only a genuine crash triggers a restart; a clean, intentional "nothing to do" exit does not. |
| Teardown | `docker compose --profile scheduler down`, `docker volume rm ai_agent_pgdata` | Clean removal; no leftover containers/networks; the temporary `.env` was deleted immediately after. |

No real external services (IMAP, SMTP, Bundesagentur, Telegram) were contacted — all credential-requiring features stayed in their default-disabled state for this smoke test, consistent with the "do not run real collectors against external accounts" constraint.

Real command output (exit codes, container logs, migration log) was inspected directly during this session, not fabricated.

## Runtime facts gathered

| Question | Answer | Evidence |
|---|---|---|
| Python version | 3.13 (match CI exactly) | `pyproject.toml:9` (`requires-python = ">=3.11"`, open lower bound) + `.github/workflows/ci.yml` pins `python-version: "3.13"` for both jobs — using the same version CI already validates against removes a variable. |
| Install command | `pip install ".[postgres]"` (non-editable, production image — no `-e`, no dev/test extras) | `pyproject.toml` optional-dependencies: `postgres = ["psycopg[binary]>=3.1"]`. `psycopg[binary]` ships a prebuilt wheel, so **no C compiler or system `libpq` package is required** in the image. |
| Application import path | `app.main:app` | `app/main.py` defines `app = FastAPI(...)`. |
| Uvicorn command | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1` | `--workers 1` is not a style choice — see AUD-004 (in-memory rate limiter is not multi-worker safe) and `docs/DEPLOYMENT.md`. |
| Database URL requirement | `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db` | `app/db/session.py` reads `settings.database_url` directly; `.env.example` already documents the Postgres URL shape (commented). |
| Migrations command | `python -m alembic upgrade head`, run as an explicit separate step (not automatic) | `app/db/session.py::run_migrations_if_enabled` is gated by `ALEMBIC_AUTO_UPGRADE` (default `false`, must stay `false` in production per its own docstring); `alembic/env.py` reads `get_settings().database_url` when no `sqlalchemy.url` override is set — so the same `DATABASE_URL` env var drives both the app and Alembic with no extra config needed. |
| Required OS packages | None beyond the `python:3.13-slim` base | `psycopg[binary]` bundles libpq statically; no other compiled dependency exists in `pyproject.toml`. Verified via full dependency list read. |
| OpenSSL requirements | Satisfied by the base image's Python build | Python's `ssl` module (used by `imaplib`/`smtplib`'s TLS wrapping) links against the base image's system OpenSSL; `python:3.13-slim` ships a current OpenSSL — no extra step needed. |
| Filesystem requirements | None | No `tempfile`/`NamedTemporaryFile`/`mkstemp` usage anywhere in `app/` (verified via grep in the production-readiness audit) — CV/Bewerbung content is generated in memory and persisted to the database, never written to disk. |
| Temp directory requirements | None | Same evidence as above. |
| Mail/network requirements | Outbound TCP egress only (IMAP 993, SMTP 587/465, HTTPS 443 to Bundesagentur/Telegram) | No inbound network surface is needed beyond the API's own port; no special container networking beyond normal outbound internet access for the opt-in collectors/providers. |
| Non-python files needed at runtime | `alembic/` directory + `alembic.ini` (for the migration command), `app/` package | `find app -type f ! -name "*.py"` returned nothing — no templates/static assets to bundle. |

## `pyproject.toml` blockers

**None found.** Everything the container needs (the `postgres` extra, no compiled system dependencies, no missing runtime asset) is already expressible through the existing `pyproject.toml` as-is. `pyproject.toml` itself is **not modified** by this work, per explicit instruction — if a real blocker had been found, it would be documented here instead of silently edited; none was.

## Image design

Single-stage build (no multi-stage split needed — there is no compiled-artifact step to separate from a runtime stage, since `psycopg[binary]` is a prebuilt wheel):

1. `FROM python:3.13-slim` — matches the exact Python version CI already validates against.
2. Set `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`, `PIP_NO_CACHE_DIR=1` — standard container hygiene (no `.pyc` clutter, unbuffered logs reach `docker logs` immediately, no pip cache bloating the image).
3. Copy only what's needed to install and run: `pyproject.toml`, `app/`, `alembic/`, `alembic.ini`. (`tests/`, `docs/`, `.claude/`, `.codex/`, review scratch directories are excluded via `.dockerignore` — none of them are runtime dependencies.)
4. `pip install ".[postgres]"`.
5. Create and switch to a non-root user (`appuser`) — standard container hardening; the app has no need for root privileges (no port <1024 binding, no system-level file access).
6. `EXPOSE 8000`.
7. `ENTRYPOINT ["docker/entrypoint.sh"]` — a thin `exec "$@"` passthrough (no hidden wait-loops, no swallowed exit codes — see rules below), `CMD` set to the default uvicorn invocation so `docker run <image>` "just works," while `compose.yaml` overrides `command:` for the `scheduler` service and ad hoc `docker compose run --rm web alembic upgrade head` for migrations.

## Compose target (initial)

`db` (PostgreSQL 16) + `web` (FastAPI, 1 worker) + `scheduler` (`python -m app.scheduler`, opt-in via the same image/different command) — see `docs/DEPLOYMENT.md` for the full topology and the Option A vs Option B scheduler-placement decision (Option B, matching what the code already does, is recommended).

## Rules followed (from the task brief, restated as a checklist for implementation)

- [x] No secrets hardcoded in `Dockerfile`/`compose.yaml` — all credentials come from environment variables / a git-ignored `.env`.
- [x] No passwords committed — `.env.example` (already tracked) only ever holds placeholders; the actual `.env` used with compose is git-ignored (already covered by `.gitignore`'s `.env` entry).
- [x] PostgreSQL healthcheck (`pg_isready`) before `web`/`scheduler` are allowed to start (`depends_on: condition: service_healthy`).
- [x] Application startup does not race DB startup — enforced via the healthcheck-gated `depends_on`, not a hand-rolled wait loop inside the app.
- [x] Migrations are explicit and deterministic — a separate `docker compose run --rm web alembic upgrade head` step, never automatic on container boot (`ALEMBIC_AUTO_UPGRADE` stays `false`).
- [x] Container stops cleanly — `ENTRYPOINT` uses `exec "$@"` so the app process is PID 1's direct child (receives SIGTERM directly, no shell wrapper swallowing signals); combined with this branch's scheduler SIGTERM fix (AUD-008), `docker compose stop` shuts down cleanly.
- [x] Migration failures are not hidden — the entrypoint never catches/suppresses the Alembic command's exit code; a failed `alembic upgrade head` step fails the `docker compose run` command visibly.
- [x] SQLite is not used in the production compose target — `DATABASE_URL` in `compose.yaml` always points at the `db` Postgres service.
- [x] No accidental multiple scheduler instances — `scheduler` is defined as a single compose service with no replica/scaling configuration; `docs/DEPLOYMENT.md` explicitly calls out that it must never be scaled.
- [x] Named PostgreSQL volume (`pgdata`) — not a bind mount, so it isn't tied to a specific host path.
- [x] No real `.env` committed — only `.env.example` (with placeholders) is tracked; `.gitignore` already excludes `.env`.
