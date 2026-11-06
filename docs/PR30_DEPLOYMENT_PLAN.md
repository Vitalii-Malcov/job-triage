# PR #30 — Deployment and rollback plan (PREPARED, NOT EXECUTED)

Scope: roll the Hard Code Review remediation (PR #30, head `1a9cc22`) onto the working system
(`E:\Projects\job-triage`, SQLite `runtime\job_triage_runtime.db`, now at Alembic `e3b7c9d1f4a6`).
Target head: `d6e9a2b4c8f1` (FIVE migrations, in order: `f1a8c3d5e7b9`, `a2b7d4f9c1e3`, `b4c7e9a1d3f5`, `c5d8f1a3b7e2`, `d6e9a2b4c8f1`). The first two were rehearsed in the first audit; the last three belong to the final remediation (TASKs 20-23) and were rehearsed on a copy of the production backup together with the first two (see `docs/PR30_FINAL_REMEDIATION_REPORT.md`).

**Nothing in this document has been executed.** Every step that writes (merge, code update, backup of
a *live* state, migration, start) needs the operator's explicit go. Commands follow
`docs/DAILY_OPERATIONS.md` (PowerShell, one shell, nothing written to `.env`).

## What changes for the operator (read first)

| Change | Effect in production |
|---|---|
| `response_draft_approvals.pinned_recipient` (NOT NULL, default `''`) | Existing approvals keep `''` and are **never sendable** (fail closed). Rehearsal: the table had 0 rows, so nothing is affected today. Any approval created before the update would need re-approval. |
| `processed_email_messages` unique on `(source, message_id)` | Pure relaxation of a global unique index. Rehearsal: 25 rows, 0 duplicates, all preserved. |
| UI sessions | Cookies are bound to a per-process nonce; **every restart logs all browsers out**. Launchers are pinned to `--workers 1`. |
| Credentials are `SecretStr`; optional `UI_SESSION_SECRET` | Existing `.env` keeps working; unset `UI_SESSION_SECRET` derives the key from `API_KEY` (changing either logs everyone out). |
| Operator CLI | Stable command names over the stage-named scripts; the old `scripts\stage*.py` paths still exist. |
| Manual collector runs | Hold the automation lease slot; a manual run during a scheduled cycle is refused instead of racing. |
| XING vacancy identity (TASK 20, migration `b4c7e9a1d3f5`: nullable `jobs.identity_key`) | A posting that shares company/title/location with a stored one but has different salary/employment text becomes its OWN row instead of being frozen out; an existing row is never rewritten by a different posting; an evidence-free digest block in a multi-row group changes nothing and is only counted. The run summary has `variant_postings` / `ambiguous_observations` (`possible_collisions` is their sum). Existing rows keep `identity_key = NULL`; the application duplicate guard still blocks a second Bewerbung to the same employer/role. |
| Gmail sender authentication (TASK 21, migration `c5d8f1a3b7e2`: nullable `gmail_messages.sender_auth`) | Approving and sending a response draft now REQUIRE a DMARC pass aligned with the From domain, recorded by Gmail's own `Authentication-Results`. Every message stored before the update (NULL) reads as unverified, so replying to old messages from this system is refused; reply manually. `GET /response-drafts/{id}/state` shows `sender_authentication` and `sender_warning`. A forged-first-header insert by someone who can write into the mailbox is a documented limit. |
| Collector exclusion (TASK 22) | `stage22_refresh_ba.py --apply` and `backfill_source_reference.py --apply` now claim the same collector slot as the scheduler, REST and Telegram runs: they are refused (exit 2, nothing written) while a run is active and block those runs while they write; they appear as `manual:ba_refresh` / `manual:backfill_ref` in `/automation/runs`. A cancelled or SIGTERMed automation cycle keeps its slot for about 6 minutes (fail closed). |
| Shared state (TASK 23, migration `d6e9a2b4c8f1`: two new, initially empty tables) | Default unchanged (`SHARED_STATE_BACKEND=memory`), but the web process now takes an exclusive per-database lock at startup: a second web process (an old one still running, `--workers 2`) REFUSES to start with a clear message. The lock file lives in the system temp directory (override: `SINGLE_PROCESS_LOCK_DIR`). The new tables are used only with `SHARED_STATE_BACKEND=database` (separate decision; not part of this rollout). |
| `app/db/models.py` is now a package `app/db/models/`; `app/api/routes.py` an aggregator | No schema or API change (fingerprint-verified); only custom code importing removed incidental attributes would break. |

## 0. Preconditions

- [ ] Operator approval of the PR #30 merge (this plan does not merge).
- [ ] `docs/PR30_FINAL_REMEDIATION_REPORT.md` verdict is `ALL REMAINING FINDINGS RESOLVED — READY FOR OPERATOR REVIEW` AND it was written for the commit you are about to merge: `git rev-parse HEAD` on the PR branch equals the commit named in the operator hand-over, and GitHub Actions for THAT commit (push and PR runs: `test`, `docker`, `scheduler-postgres`) are green. If the branch moved after the report, the evidence is stale: re-verify before merging. (The earlier `PR30_FINAL_READINESS_REPORT.md` covered `1a9cc22` and two migrations only and is superseded.)
- [ ] No Bewerbung is mid-flow (no browser submission in progress, no pending Gmail draft handoff).
- [ ] Know that after the update replies to Gmail messages stored before it cannot be approved/sent from the system (TASK 21: no stored DMARC evidence); the operator answers those manually.
- [ ] The uncommitted operator edits (`CLAUDE.md`, `app/collectors/CLAUDE.md`, `.postman/`, `postman/`, the two review `.md` files) are committed or stashed on purpose — the code update must not overwrite them.

## 1. Stop every writer, then check the environment

Stop first (details in section 3, performed *before* the baseline and the backup): Ctrl+C uvicorn / `run_operator_ui.py`, stop `python -m app.scheduler`, close any tracker CLI / browser-agent session. Writers stay stopped until the end of section 6. Only then:

```powershell
cd E:\Projects\job-triage
git status --short                      # identify every non-PR change
git rev-parse HEAD; git branch --show-current
Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'uvicorn|run_operator_ui|stage14d|app\.scheduler' } | select ProcessId,CommandLine
Get-Item runtime\job_triage_runtime.db* | select Name,Length,LastWriteTime   # expect no -wal/-shm (journal_mode=delete)
.\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('file:runtime/job_triage_runtime.db?mode=ro',uri=True);print(c.execute('select version_num from alembic_version').fetchall(),c.execute('pragma integrity_check').fetchall())"
```
Expect revision `e3b7c9d1f4a6`, `ok`. Record the aggregate counters (row count and row hash per table; hash the ORIGINAL column projection - `alembic_version` excluded and the columns the update adds, `pinned_recipient`, `identity_key`, `sender_auth`, left out of the projection) with the same read-only snapshot script used in the rehearsal; keep it for section 6. The baseline and the section-2 backup are taken in the same quiescent window, so nothing can change between them.

## 2. Backup and its verification (writers already stopped)

```powershell
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$dst = "$env:USERPROFILE\JobTriage-Backups\job_triage_runtime.pre-pr30.$ts.db"
.\.venv\Scripts\python.exe -c "import sqlite3,sys;s=sqlite3.connect('file:runtime/job_triage_runtime.db?mode=ro',uri=True);d=sqlite3.connect(sys.argv[1]);s.backup(d);d.close()" $dst
```
Verify: `PRAGMA integrity_check = ok`, `PRAGMA foreign_key_check` empty, `alembic_version = e3b7c9d1f4a6`,
store SHA-256 + size next to the file. Keep it outside the repository, ACL limited to the operator.
(Rehearsal backup of 2026-11-05: `C:\Users\V\job-triage-backups\pr30\job_triage_runtime.pre-pr30.db`,
SHA-256 in the neighbouring `.sha256`; take a **fresh** one if anything wrote to the DB since.)

## 3. Writer shutdown (executed at the start of section 1)

1. Ctrl+C the uvicorn / `run_operator_ui.py` shell; stop the scheduler (`python -m app.scheduler`) if it runs.
2. Confirm no process matches the filter from step 1 and that `runtime\job_triage_runtime.db-journal` does not exist.
3. Outbound stays off: `OUTBOUND_SENDING_ENABLED=false`, `TELEGRAM_GMAIL_DRAFT_ENABLED=false` in the shell.

## 4. Controlled code update

1. Operator merges PR #30 (or approves it to be merged) — **not** done by the agent.
2. `git fetch origin; git status --short` — commit/stash local edits first.
3. `git switch main; git pull --ff-only origin main` (the merge commit is on `main`); verify
   `git log -1` and that `alembic heads` prints one head `d6e9a2b4c8f1`.
4. `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"` (no dependency change; `pyproject.toml` only adds the `jobtriage` console
   script, the pytest marker and a new description — the reinstall registers the `jobtriage` command; confirm with
   `git diff 78a947b..HEAD -- pyproject.toml`).
5. `ruff check app scripts tests alembic` and `ruff format --check ...` clean.

## 5. Apply migrations (explicit, one shell)

```powershell
$env:DATABASE_URL  = "sqlite:///./runtime/job_triage_runtime.db"
$env:DATABASE_ROLE = "production"
$env:ALEMBIC_AUTO_UPGRADE = "false"
# POSTGRES_HOST (process env OR .env) overrides DATABASE_URL. NOTE: in PowerShell 5.1 `$env:POSTGRES_HOST = ""` DELETES the variable, so it cannot mask a .env value.
# Required precondition: .env has no POSTGRES_HOST line (true on 2026-11-05: 0 lines) and the variable is not set in the shell; the gate below aborts otherwise.
if ($env:POSTGRES_HOST -or (Select-String -Path .env -Pattern '^\s*POSTGRES_HOST\s*=\s*\S' -Quiet)) { throw 'POSTGRES_HOST is configured - fix .env (operator) before migrating' }
$ErrorActionPreference = "Stop"   # PowerShell 5.1 continues after a failed native command, so every gate below also checks $LASTEXITCODE
# assert the resolved target is the backed-up SQLite file BEFORE any Alembic write:
.\.venv\Scripts\python.exe -c "from app.core.config import get_settings as g;u=g().database_url;print(u.split('://')[0]);assert u.startswith('sqlite') and u.endswith('runtime/job_triage_runtime.db'),'WRONG TARGET'"
if ($LASTEXITCODE -ne 0) { throw 'WRONG TARGET - do not run alembic' }
.\.venv\Scripts\python.exe -m alembic current          # expect e3b7c9d1f4a6
if ($LASTEXITCODE -ne 0) { throw 'alembic current failed' }   # and the printed revision MUST be e3b7c9d1f4a6, otherwise stop
.\.venv\Scripts\python.exe -m alembic upgrade head     # f1a8 -> a2b7 -> b4c7 -> c5d8 -> d6e9 (five steps)
if ($LASTEXITCODE -ne 0) { throw 'upgrade failed - go to section 8' }
.\.venv\Scripts\python.exe -m alembic current          # expect d6e9a2b4c8f1 (head)
```
Rehearsal on a copy of the production DB (2026-11-05/06, all five migrations): upgrade clean, 48 -> 50 tables, the
row content of every existing table is identical except `alembic_version` (the three new columns and the two new
tables are empty/NULL), downgrade to `e3b7c9d1f4a6` restores every table exactly, a second upgrade is identical.
Stop on any error: do **not** retry; go to section 8.

## 6. Verify data after the migration (read-only)

- `integrity_check = ok`, `foreign_key_check` empty, `alembic_version = d6e9a2b4c8f1`.
- New objects are empty: `shared_rate_limit_hits` and `shared_ui_session_revocations` have 0 rows; `jobs.identity_key` and `gmail_messages.sender_auth` are NULL for every existing row; 50 tables, the 6 append-only triggers still present.
- Per-table row counts and row hashes of the ORIGINAL column projection equal the section-1 baseline (`alembic_version` content and the added columns `pinned_recipient`, `identity_key`, `sender_auth` are excluded from the hash; the revision is checked separately above; the added columns and the two new tables are validated by their own bullet below);
  expected rehearsal figures: 48 tables, 13 028 rows in total, `jobs` 3 234 all `NEW`,
  `processed_email_messages` 25 (all `xing`), `response_draft_approvals` 0.
- Applications / Bewerbung records: compare counts per table and status with the step-1 snapshot
  (`stage15_application_tracker.py --database runtime/job_triage_runtime.db list`, read-only) — no record lost, no status changed.
- `PRAGMA index_list(processed_email_messages)` contains `uq_...source_message_id`'s autoindex;
  the 6 append-only triggers are still present.

## 6b. Vacancy fit cache (needs the operator's go; writes only the cache table)

The cached `job_fit_assessments` rows can become stale after the classifier/source change; the dashboard then shows `stale` / `not assessed`. After section 6 passes: dry run `.\.venv\Scripts\python.exe scripts\stage21_assess_vacancies.py --database runtime/job_triage_runtime.db` (read-only counts); if rows are stale, the refill per `docs/DAILY_OPERATIONS.md` (Stage 21 section, `--only-stale`, Ctrl+C-safe) is run only with approval and *before* the final step-10 sign-off, then freshness is checked on the dashboard. It does not touch Bewerbungen or statuses.

## 7. Start and check — without sending anything

```powershell
$env:OUTBOUND_SENDING_ENABLED = "false"; $env:TELEGRAM_GMAIL_DRAFT_ENABLED = "false"
$env:TELEGRAM_OPERATOR_CONTROL_ENABLED = "true"   # as in DAILY_OPERATIONS §1
$env:UI_ENABLED = "true"   # default is false (UI returns 404); needed for the UI smoke test (see UI_ENABLED / launcher docs)
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000 --workers 1
```
- If startup fails with "another web process is already running against this database", an OLD web process is still alive (section 3 was incomplete): stop it; do NOT disable the guard. (`SINGLE_PROCESS_LOCK_DIR` moves the lock file; `SHARED_STATE_BACKEND=database` is a separate, later decision.)
- First log line `startup_safety_state`: `outbound_sending_enabled=false telegram_gmail_draft_enabled=false alembic_auto_upgrade=false shared_state_backend=memory`.
- `GET /api/v1/health` -> ok; `GET /api/v1/ready` -> ready (503 would mean DB not at head).
- UI first: log in, confirm the page renders, log out, confirm the old cookie is refused. Do not start from `0.0.0.0`.
- API last: a request without / with a wrong `X-API-Key` -> 401; repeated wrong keys -> 429. Use a fake key only. **This lockout also blocks correct UI credentials from the same address for up to `AUTH_FAILURE_WINDOW_SECONDS` = 300 s** (`app/security/rate_limit.py`), so run it after the UI checks or wait it out; a lockout is not a rollback trigger.
- Telegram: only a read command to the operator chat (e.g. status); **no** APPROVE/SEND press. Gmail: IMAP read paths only if the collector is enabled; **no** draft, **no** send. `GET /response-drafts/{id}/state` for an old draft shows `sender_authentication: UNVERIFIED` (expected for every pre-update message).
- Scheduler stays `AUTOMATION_SCHEDULER_ENABLED=false` until step 10.

## 8. Rollback

Trigger: migration error, failed check in 6/7, unexpected data difference, or operator decision.

1. Stop all processes (section 3).
2. **Schema-only problem, data intact:** `alembic downgrade e3b7c9d1f4a6` (five steps back: `d6e9` drops the two empty shared-state tables, `c5d8` drops `sender_auth`, `b4c7` drops `identity_key` and REFUSES before any DDL if distinguished XING sibling rows exist, `a2b7` REFUSES before any DDL if two sources hold the same Message-ID, `f1a8` drops the recipient pins; nothing is deleted or merged automatically — resolve manually), then verify against the ORIGINAL schema, not section 6: revision `e3b7c9d1f4a6`; `integrity_check = ok`, `foreign_key_check` empty; 48 tables; NO `shared_rate_limit_hits` / `shared_ui_session_revocations`; NO `jobs.identity_key`, `gmail_messages.sender_auth` or `response_draft_approvals.pinned_recipient`; `ix_processed_email_messages_message_id` is the UNIQUE index again; counts and original-projection hashes equal the section-1 baseline.
3. **Anything uncertain:** restore the file. Keep the current DB as `runtime\job_triage_runtime.broken-<ts>.db`,
   copy the section-2 backup to `runtime\job_triage_runtime.db`, run the same ORIGINAL-schema verification as in step 2 (revision `e3b7c9d1f4a6`, 48 tables, none of the added objects, baseline counts/hashes). Note: a downgrade drops the recipient pins, the sender evidence and the sibling grouping on purpose, so a rollback AFTER the new code created sibling rows should restore the file instead of downgrading; a later re-upgrade leaves those approvals unsendable until re-approved.
4. **Code:** `git switch --detach 78a947b` (previous `main`; this predates the merge) or `git revert -m 1 <merge>` via a new
   reviewed PR. Never force-push `main`.
5. Start the OLD code (`78a947b`) against the restored DB (section 7 commands) and run ONLY the section-7 checks that exist in that version: `startup_safety_state` line (without `shared_state_backend`), `/health`, `/ready`, API key 401, UI login/logout renders. The following do NOT exist in the old code and must be skipped, not treated as failures: the 429 lockout after repeated wrong keys, server-side logout revocation (the old cookie is only invalidated by the restart), `sender_authentication` / `sender_warning` in the draft state, the single-process lock message, `variant_postings` / `ambiguous_observations` in collector summaries.
6. Write what happened in `docs/` evidence; do not delete the failed DB copy.

## 9. No Bewerbung lost — acceptance check

Equal to the section-1 baseline: row counts for `applications*`, `application_events`, operator decisions,
`response_draft_approvals`, `jobs`, `processed_email_messages`; the append-only tables' hashes unchanged;
the `runtime\evidence` directory untouched. Any difference = rollback.

## 10. Conditions for the final go-live

- Sections 5-7 all passed; operator saw the 7 outputs.
- Backup kept at least until one full working day passes without incident.
- Only then (separate operator decision) re-enable `AUTOMATION_SCHEDULER_ENABLED` / other flags.
  Send/draft flags stay `false` unless the operator enables them for a specific, approved Bewerbung.
- Real-Gmail validation of the draft handoff remains a separate production-enable gate (CLAUDE.md, Stage 9D).
