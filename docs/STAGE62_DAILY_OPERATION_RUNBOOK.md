# Stage 62 — Daily operation runbook (manual mode)

Manual mode: scheduler OFF, auto-prepare OFF, outbound sending OFF, Telegram bot OFF.
Nothing here submits an application or contacts an employer. Every command below uses only
entrypoints that exist in the repository (`scripts/*.py`, `uvicorn`, `alembic`).
Everything new lives on `E:`: project `E:\Projects\job-triage`, backups `E:\JobTriage-Backups`,
temp/logs `E:\JobTriage-Temp` (`logs\`).

Set once per PowerShell session (keeps temp files off `C:`):

```powershell
cd E:\Projects\job-triage
$env:TEMP = "E:\JobTriage-Temp"; $env:TMP = "E:\JobTriage-Temp"
New-Item -ItemType Directory -Force E:\JobTriage-Temp\logs, E:\JobTriage-Backups | Out-Null
```

## 1. Start the server (read-only operator UI)

```powershell
.\.venv\Scripts\python.exe scripts\run_operator_ui.py `
  --database runtime\job_triage_runtime.db --snapshot E:\JobTriage-Backups
```

Binds `127.0.0.1:8000` only, forces the Telegram bot, scheduler and every send/draft flag off for
this process and snapshots the database first. Open <http://127.0.0.1:8000/ui/review> and log in
with `API_KEY`. The session cookie is `Secure`; Chrome/Edge accept it on `http://127.0.0.1`
(Safari does not). Health: `GET /api/v1/ready` → `{"status":"ready"}`.

## 2. Stop the server

`Ctrl+C` in the window that runs it. Stop it **before** any step that writes the database
(sections 3–4): a competing writer is refused.

## 3. Get new vacancies (production write — needs the operator's go-ahead)

```powershell
# a) dry run (read-only DB, GET to rest.arbeitsagentur.de only)
.\.venv\Scripts\python.exe scripts\stage22_refresh_ba.py --database runtime\job_triage_runtime.db
# b) apply: BA facts for stored vacancies + new vacancies, backup first
.\.venv\Scripts\python.exe scripts\stage22_refresh_ba.py --database runtime\job_triage_runtime.db `
  --apply --create-new --snapshot E:\JobTriage-Backups
# c) match + refresh the fit cache for the new rows
.\.venv\Scripts\python.exe scripts\stage22_match_vacancies.py --database runtime\job_triage_runtime.db --apply --snapshot E:\JobTriage-Backups
.\.venv\Scripts\python.exe scripts\stage21_assess_vacancies.py --database runtime\job_triage_runtime.db --apply --snapshot E:\JobTriage-Backups
```

Exit codes of (b): 0 done, 1 incomplete (re-run, idempotent), 2 refused, 3 ALERT: read the
printed line. "lease was lost ... partial write" = the written rows are valid, re-run (b) to add the
rest (Stage 63 rehearsal: 83 created, re-run created 196, `APPLIED`); "protected tables changed" or
a failed post-write check = stop and restore the printed snapshot. See
`STAGE63_DAILY_DISCOVERY_MANUAL_MODE.md`. `applications` / `application_events` are digest-checked
unchanged. No Telegram, mail or auto-prepare is touched.

## 4. Check the result

1. `/ui/review` — "Needs review"; Junior Python/Backend titles come first. Each row shows the
   reason, what to clarify, the working-time evidence and an "Existing Bewerbung" mark.
2. `/ui/vacancies` — filter by fit tier / source; open a vacancy for criteria and evidence.
3. `/ui/applications` — existing Bewerbungen (the 25 protected ones). Count must not drop.
4. Duplicates: `.\.venv\Scripts\python.exe scripts\vacancy_identity_report.py --help`.
5. Closed postings: `scripts\ba_liveness.py` (a CLOSED hint is for hand-checking, never a status change).

## 5. Prepare a Bewerbung (manual, conscious action only)

Use the existing documented path (`docs/DAILY_OPERATIONS.md` §6–7, `docs/APPLICATION_TRACKER_RUNBOOK.md`):
draft → review package → operator approval → manual submit in the portal/mail by the operator →
record it. Before pressing the final submit in a portal run `scripts/presubmit_check.py`
(CLAUDE.md "Browser submission gate"). Never press "Vorschau"-type buttons. Auto-prepare stays
off; the gate-open list is a *suggestion list*, not a to-do queue.

## 6. Restore from a backup

The `--snapshot` copies made by the UI launcher and the refresh/match/assess tools are safety nets
without a `.sha256` sidecar and with names `restore_sqlite.py` does not accept. For a restorable,
verified backup, make one explicitly **before** every production write (sections 3):

```powershell
.\.venv\Scripts\python.exe -m scripts.backup_sqlite --db runtime\job_triage_runtime.db `
  --dest E:\JobTriage-Backups --label before-daily-refresh
```

```powershell
Get-FileHash E:\JobTriage-Backups\<file>.db -Algorithm SHA256     # compare with <file>.db.sha256
.\.venv\Scripts\python.exe scripts\restore_sqlite.py --backup E:\JobTriage-Backups\<file>.db `
  --target runtime\job_triage_runtime.db --replace --allow-production
```

Stop every process first; the current file is kept as `.pre-restore-<stamp>`. Do not downgrade
migrations on production. A restore that discards newer data needs the operator's confirmation.

## 7. Diagnose errors

| Symptom | Check |
|---|---|
| `/api/v1/ready` 503 | `alembic current` must equal head (`a7c3e5f1b9d2` at Stage 61); no auto-upgrade in production |
| login 401 / 429 | wrong `API_KEY`; the failed-attempt budget refills after the window |
| UI login loops (Safari) | `Secure` cookie on plain HTTP: use Chrome/Edge or `UI_COOKIE_SECURE=false` for that shell only |
| refresh exit 2 | another writer/refresh/collector run holds the slot; stop the UI, retry |
| refresh exit 3 | restore the snapshot it printed, report |
| slow `/ui/review` | ~1–1.5 s per page is normal (freshness hashing); stale cache → `stage21_assess_vacancies.py --only-stale --apply` |

Logs: redirect to `E:\JobTriage-Temp\logs\` (`... *> E:\JobTriage-Temp\logs\ui.log`).

## 8. API-key rotation (the operator does this; nothing is changed by the code)

The Stage 61 diagnostic reported `api_key_strength=weak`. The rule is ≥ 32 random characters
without long repeated runs.

1. Generate without printing it into a shared place:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` — copy straight into the password manager.
2. Dependent clients: the browser UI login, any script/Postman collection sending `X-API-Key`
   (`postman/` environment variables), and `UI_SESSION_SECRET` if set (otherwise derived from the key).
3. Stop the server; edit `API_KEY=` in `E:\Projects\job-triage\.env`; start the server.
   Changing it signs every browser out — expected.
4. Verify: `startup_safety_state ... api_key_strength=strong` in the first log line; old key → 401.
5. Rollback: put the previous value back (keep it in the password manager until the check passes).
