# Operations Runbook — JobTriage

Every command here is based on actual repository behavior verified in this session (Docker smoke test, CI configuration, code inspection) — nothing invented. Assumes the Docker Compose deployment from `docs/DEPLOYMENT.md`; adjust paths for a non-Docker deployment.

## First deployment

```bash
cp .env.example .env
# edit .env: API_KEY, POSTGRES_PASSWORD, and any collector/provider credentials
docker compose build
docker compose up -d db
# wait for db healthy:
docker compose ps db
docker compose run --rm web alembic upgrade head
docker compose up -d web
# optional, only if AUTOMATION_SCHEDULER_ENABLED or TELEGRAM_DAILY_DIGEST_ENABLED:
docker compose --profile scheduler up -d scheduler
```

Verify: `curl http://localhost:8000/api/v1/health` → `{"status": "ok"}`. Note this does **not** confirm DB connectivity (AUD-002) — also check `docker compose logs web` for a clean startup with no `OperationalError`.

## Database migration (after pulling new code)

```bash
docker compose run --rm web alembic upgrade head
docker compose up -d web    # only after migration succeeds
```

**Never** set `ALEMBIC_AUTO_UPGRADE=true` in this deployment — migrations are an explicit, out-of-band step by design (`app/db/session.py::run_migrations_if_enabled`).

## Normal startup

```bash
docker compose up -d db web
docker compose --profile scheduler up -d scheduler   # if enabled
```

`web` waits for `db`'s healthcheck (`pg_isready`) before starting — no manual wait needed.

## Shutdown

```bash
docker compose stop web scheduler    # keeps db running
# or, everything:
docker compose stop
```

Confirmed clean (exit code 0, ~3s) in this session's smoke test — the app receives SIGTERM directly (via the `exec`-based entrypoint) and shuts down its lifespan cleanly (stops the Telegram bot poller).

## Restart

```bash
docker compose restart web
```

For the scheduler, a restart mid-cycle is safe by design (lease/CAS reconciliation — see `docs/ARCHITECTURE.md` "Concurrency-sensitive areas").

## Migration failure

If `alembic upgrade head` fails:
1. **Do not start `web`/`scheduler`** against the partially-migrated database.
2. Read the Alembic error — it names the failing revision.
3. Check `alembic/versions/<revision>.py` for what it attempts; check the actual DB state (`docker compose exec db psql -U <user> -d <db> -c '\d'`) to see how far it got.
4. Postgres runs each migration inside a transaction by default (`alembic/env.py`: "Will assume transactional DDL") — a failed migration should already be rolled back, not left half-applied. Confirm via `alembic current` before retrying.
5. Fix the underlying issue (schema conflict, bad data) and re-run `alembic upgrade head` — do not skip revisions.

## Database unavailable

- `web` will fail its healthcheck (once AUD-002's dependency-aware health check is implemented — until then, the app returns 500s on any DB-touching route while `/health` still reports "ok").
- Check: `docker compose ps db`, `docker compose logs db`.
- If `db` container is unhealthy: check disk space (`docker system df`), check the `pgdata` volume isn't corrupted, check Postgres logs for the actual startup error.
- `web`/`scheduler` do not currently retry DB connection failures beyond SQLAlchemy's pool behavior — restart them (`docker compose restart web`) once `db` is confirmed healthy again.

## Gmail unavailable (IMAP/SMTP)

- Gmail sync (`app/services/gmail_sync.py`) and response-draft/follow-up send failures are logged with `type(exc).__name__` only (no credentials/content leaked — verified in the audit).
- IMAP operations are bounded by `IMAP_SESSION_DEADLINE_SECONDS` (`app/providers/email/imap_deadline.py`) — a hung Gmail connection cannot hang the calling process indefinitely.
- Action: check `docker compose logs web` (or `scheduler`, if the Gmail cycle runs from there) for the specific exception type; verify `GMAIL_USERNAME`/`GMAIL_APP_PASSWORD` are still valid (Google App Passwords can be revoked); retry the sync manually via the relevant API route once credentials are confirmed.

## SMTP failure (sending a response/follow-up)

- Ambiguous send outcomes are recorded as a terminal `UNCERTAIN` state (`ResponseDraftSendRecord`/follow-up equivalent) — **never auto-retried**, specifically to avoid a duplicate send if the first attempt actually succeeded server-side but the confirmation was lost.
- Action: manually verify (check the actual mailbox's Sent folder, or ask the recipient) whether the email actually went out, then resolve the `UNCERTAIN` record's downstream state manually — do not blindly re-trigger the send.

## Provider timeout (Bundesagentur API)

- Retryable vs. permanent classification already applies (see `docs/PRODUCTION_READINESS_AUDIT.md` AUD-017 note and the project's known Bundesagentur collector tech debt in `CLAUDE.md`).
- Action: check logs for repeated timeouts; the collector run itself fails cleanly per-run (doesn't corrupt state) — simply re-trigger the collector run once the upstream API recovers.

## Stuck automation cycle

- Check `automation_runs` table for a row `status='RUNNING'` with an old `lease_expires_at`.
- If the lease has expired, the **next** scheduled tick automatically reconciles it to `FAILED` (`app/db/automation_repository.py::reconcile_stale_run_to_failed`) — no manual intervention needed, just wait for the next poll interval (`AUTOMATION_SCHEDULER_POLL_SECONDS`, default 15s).
- If it needs immediate reconciliation: restart the `scheduler` container — its next poll tick will run the reconciliation check.
- **Never manually `UPDATE automation_runs SET status='FAILED'`** without also checking the partial unique index isn't still held by a genuinely-live process — let the lease-expiry mechanism handle it.

## Duplicate scheduler concern

- If you suspect two scheduler processes are running (e.g. an old container wasn't cleaned up): `docker compose ps scheduler` should show exactly one. If more than one container is somehow running, the DB-level partial unique index + lease still prevents a duplicate `RUNNING` automation cycle — but stop the extra container anyway (`docker stop <container-id>`) to avoid wasted work/log noise.

## Log inspection

```bash
docker compose logs -f web
docker compose logs -f scheduler
docker compose logs -f db
```

Logs are structured JSON (`app/core/logging.py`) — pipe through `jq` for filtering, e.g.:
```bash
docker compose logs web | grep -o '{.*}' | jq 'select(.level=="ERROR")'
```

## Backup

```bash
docker compose exec db pg_dump -U ${POSTGRES_USER:-jobtriage} -d ${POSTGRES_DB:-jobtriage} -F c -f /tmp/backup.dump
docker compose cp db:/tmp/backup.dump ./backup-$(date +%Y%m%d).dump
```

Schedule this externally (cron, orchestrator-native backup) — no in-app backup mechanism exists or is planned.

## Restore (concept)

```bash
# against a fresh/empty db container:
docker compose cp ./backup-YYYYMMDD.dump db:/tmp/restore.dump
docker compose exec db pg_restore -U ${POSTGRES_USER:-jobtriage} -d ${POSTGRES_DB:-jobtriage} --clean --if-exists /tmp/restore.dump
```

Always restore into a fresh/empty database, not over a live one with divergent state, unless you specifically intend to overwrite it (`--clean` drops existing objects first).

## Credential rotation (IMAP/SMTP App Passwords, Bundesagentur key, Telegram token)

1. Generate the new credential at the source (Google App Passwords page, Telegram BotFather, etc.).
2. Update `.env` with the new value.
3. `docker compose up -d web` (and `scheduler`, if it uses the same credential) — recreates the container with the new environment. No code change, no migration needed (credentials are never persisted to the database).
4. Revoke the old credential at the source once the new one is confirmed working.

## API key rotation

1. Generate a new long random value for `API_KEY` in `.env`.
2. `docker compose up -d web`.
3. Update every API client configuration to use the new key.
4. There is no dual-key grace period supported (`require_api_key` checks against exactly one configured value) — rotation causes a hard cutover; coordinate client updates accordingly.

## Emergency disabling of outbound send

The send paths are already approval-gated (`app/services/response_draft_send.py`, `app/services/follow_up_send.py`) — the fastest emergency stop is to **not approve any more drafts** (the human-approval step is the actual kill switch, not a config flag). To additionally stop the automated *drafting* cycle:

```bash
# in .env, set:
AUTOMATION_GMAIL_CYCLE_ENABLED=false
AUTOMATION_FOLLOW_UP_CYCLE_ENABLED=false
# then:
docker compose --profile scheduler up -d scheduler   # recreates with new env
```

This stops new drafts/proposals from being generated; it does not touch already-approved-but-not-yet-sent items (there should be none, since send happens synchronously within the approval flow per the code's design — verify via the relevant API route if in doubt).

## Rollback (concept)

- **Application code**: redeploy the previous image tag/commit; `web`/`scheduler` restart with the old code against the same database.
- **Database schema**: only roll back via `alembic downgrade -1` (or to a specific revision) if the previous application version is actually compatible with the downgraded schema — check the migration's `downgrade()` implementation first. This project does not maintain a tested rollback-compatibility matrix; treat schema rollback as a last resort, not routine.
