# Stage 59 — controlled production rollout runbook

**Status: prepared, NOT executed.** Permission to merge a PR is not permission to migrate, refresh the
cache or enable anything. Every step below that writes needs the operator's explicit go.
Production DB: `runtime/job_triage_runtime.db` (historical: 3,234 jobs, 25 applications, 54 events,
Alembic `d6e9a2b4c8f1`; the code expects `a7c3e5f1b9d2`). Do not use the historical numbers to roll back
newer rows.

1. **Check main** — `git fetch`; confirm the intended SHA is on `main`, CI green on it.
2. **Stop** the app and the scheduler (UI process, any scheduled task). Confirm no process holds the DB.
3. **Backup** — `python scripts/backup_sqlite.py --db runtime/job_triage_runtime.db --dest <dir outside
   the repo> --label pre-stage59` (online backup API, integrity + count check, `.sha256` sidecar).
4. **Verify backup** — `PRAGMA integrity_check` = `ok`; recompute the SHA-256; record job / application /
   event counts and the current `alembic current`.
5. **Migrate** — `alembic upgrade head` (adds `jobs.full_time_offered`; rehearsed on a copy, both directions).
6. **Fit-cache refresh** — `python scripts/stage21_assess_vacancies.py --database <db> --snapshot <dir>
   --apply` (writes only `job_fit_assessments`; requires the snapshot for the production path).
7. **Applications intact** — counts equal step 4; `SELECT status, count(*)` unchanged.
8. **Application events intact** — event count equals step 4; the append-only triggers still present.
9. **Smoke `/ui/review`** — page loads read-only; queue count ≈ 684 (± newly collected jobs); #551/#1261
   listed with reason `WORK_TIME_NOT_CONFIRMED`.
10. **Source URLs** — spot-check ten rows' outbound links resolve to the source (no tracking redirects are
    followed by the app).
11. **Junior Python/Backend shortlist** — the 8 review items are present; none is auto-cleared.
12. **Automation still off** — `TELEGRAM_*` control flags, `OUTBOUND_SENDING_ENABLED`, `AUTO_PREPARE`,
    scheduler: all disabled; nothing sent.

Roll back (if step 5–7 fails): stop, restore the step-3 backup (`scripts/restore_sqlite.py` after checking
the sidecar digest). Never restore over rows created after the backup without comparing first.
