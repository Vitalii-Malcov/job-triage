# Daily Discovery — Manual Mode

One operator-run pass that fetches new Bundesagentur vacancies, skips duplicates, scores them for
Junior Python/Backend and shows the result for a human decision. It **never** creates a Bewerbung,
sends mail/Telegram, enables the scheduler or auto-prepare, or changes an application status.
It is not a scheduled task: you start it, you read the result.

Tested in Stage 63 on a copy of production (`E:\JobTriage-Temp\s63\daily.db`, rehearsal below).
Running it on production **writes the production database and needs your explicit go-ahead.**

## Prerequisites

* Run from `E:\Projects\job-triage` (the `.env` with `BUNDESAGENTUR_API_KEY` must be in the
  working directory; a worktree without `.env` is refused).
* The UI server is stopped (a competing writer is refused): `Ctrl+C` in its window.
* Free space on E: (the run needs about 3 × the database size for snapshots: ~170 MB).

```powershell
cd E:\Projects\job-triage
$env:TEMP = "E:\JobTriage-Temp"; $env:TMP = "E:\JobTriage-Temp"
$py = ".\.venv\Scripts\python.exe"; $db = "runtime\job_triage_runtime.db"; $snap = "E:\JobTriage-Backups"
```

## Steps

| # | Step | Command | Writes? |
|---|---|---|---|
| 0 | Verified backup (restorable, with `.sha256`) | `& $py -m scripts.backup_sqlite --db $db --dest $snap --label before-daily-discovery` | file in E:\JobTriage-Backups |
| 1 | Dry run: what BA returns today | `& $py scripts\stage22_refresh_ba.py --database $db` | **no** (GET to BA only) |
| 2 | Read the dry-run summary | `new=N`, `to_update`, "BA-reference conflicts" (never merged), "not returned today" (**not** evidence of closing) | — |
| 3 | Apply: update BA facts, store new vacancies, duplicate-protected | `& $py scripts\stage22_refresh_ba.py --database $db --apply --create-new --snapshot $snap` | production DB |
| 4 | Match (candidate-job analysis for new rows) | `& $py scripts\stage22_match_vacancies.py --database $db --apply --snapshot $snap` | production DB |
| 5 | Fit-cache assessment (Junior Python/Backend tiers) | `& $py scripts\stage21_assess_vacancies.py --database $db --apply --snapshot $snap` | production DB |
| 6 | Start the UI (read-only) | `& $py scripts\run_operator_ui.py --database $db --snapshot $snap` | snapshot only |
| 7 | Read: `http://127.0.0.1:8000/ui/review` (filter *junior*), `/ui/vacancies`, `/ui/applications` | | no |

Order matters: refresh → match → assess (assessing before matching scores new vacancies with
`skills NOT_EVALUATED`).

### Exit codes of step 3
* `0` done. Final lines: `protected tables unchanged (47 tables digest-checked)`, `APPLIED`.
* `1` incomplete — run step 3 again (idempotent).
* `2` refused (another writer holds the slot, missing snapshot, missing BA key) — nothing written.
* `3` **ALERT** — read the printed line:
  * *"the collector-exclusion lease was lost; N updated and M created (partial write)"*:
    the rows already written are valid; run step 3 again (it adds the rest). Seen once in the
    Stage 63 rehearsal (83 created, then the lease heartbeat hit an `OperationalError`); the second
    run created the remaining 196 and ended `APPLIED`.
  * *"protected tables changed"* or a post-write check failure: **stop**, do not continue,
    restore the printed snapshot (see the runbook §6) and report.

## What to check after steps 3–5

1. `applications` stays 25 and `application_events` stays 54 (or higher only by your own entries).
2. UI `/ui/applications`: count not lower; no status changed.
3. New vacancies: tiers are `NOT_SUITABLE` / `NEEDS_OPERATOR` / `STRETCH` / `PARTIAL` / `GOOD`.
   Rehearsal: 279 new → 189 / 72 / 18 / 0 / 0.
4. Duplicates: the tool refuses to create a second row for a known BA reference or fingerprint
   (rehearsal: 0 fingerprint duplicates, 28 reference conflicts listed for manual review,
   1 `ALREADY_APPLIED`).
5. Safety: the first log line of the UI shows `startup_safety_state` with every flag `false`.

## Rehearsal result (copy of production, 2026-11-11)

| Step | Result |
|---|---|
| Dry run | found 1 407, existing 1 104, **new 279**, 28 reference conflicts, exit 0 |
| Apply (1st) | exit 3: lease lost, 2 updated + 83 created (partial) |
| Apply (2nd) | exit 0: 196 created, 47 protected tables unchanged, `APPLIED` |
| Match | 83 + remaining analyses written, 0 failed |
| Assess | rows written, exit 0 |
| Final | 3 513 jobs (3 234 + 279), integrity ok, foreign_key_check empty, 0 fingerprint duplicates, `applications` and `application_events` byte-identical to production |

## Not part of this procedure

Preparing a Bewerbung, approving, sending, Gmail/Telegram, scheduler, auto-prepare, changing
statuses, key rotation, migrations. Those stay separate, manual and individually approved
(`docs/DAILY_OPERATIONS.md` §6–7, CLAUDE.md "Browser submission gate").
