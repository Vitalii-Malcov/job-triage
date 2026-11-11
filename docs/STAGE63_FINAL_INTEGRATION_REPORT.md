# Stage 63 — Final integration report (2026-11-11)

Scope: merge PR #58, verify production after Stage 61, prepare key rotation and daily
discovery, review candidates, check laptop resources. Everything else was read-only or ran on
copies under `E:\JobTriage-Temp\s63`. **Production writes: 0. Bewerbungen sent: 0.**

## Summary table

| Criterion | Result |
|---|---|
| PR #58 merged | **Yes**, merge commit `6f5c7f02703f1692e62a0d591a67efc0a9e0550b` (2026-11-11 06:20:58 UTC), `--merge --match-head-commit 8951580`, no `--admin`, no force push, branch not deleted |
| Main CI | **success** (run 42971800250 on `6f5c7f0`: test, docker, scheduler-postgres all success) |
| Production integrity | integrity_check `ok`, foreign_key_check empty, Alembic `a7c3e5f1b9d2`, no `-wal`/`-shm`; SHA-256 `a2cf5973…38df` identical to the Stage 61 report; file mtime 10.11. 22:03 (unchanged) |
| Applications unchanged | Yes: 25 rows, content digest identical to the Stage 61 backup |
| Events unchanged | Yes: 54 rows, digest identical |
| Backup/production difference explained | Yes, by data (section 5) |
| API-key rotation ready | Plan written, key **not** changed (section 6) |
| Daily discovery tested | Yes, on a copy (section 7), one partial-write alert handled by re-run |
| Candidate shortlist reviewed | Yes (`STAGE63_JOB_CANDIDATES.md`): no confirmed suitable vacancy |
| Production writes | **0** |
| Bewerbungen sent | **0** |

## 1. PR #58

Base `main`, head `8951580cd8de043efbb4d1c1594a471661698a32`, MERGEABLE/CLEAN, no review
comments, CI (test, scheduler-postgres, docker) green on that head in both runs; Codex had no open
P0–P2. The diff was 10 files (+352): 3 app files, `.env.example`, 3 test files, 4 docs; nothing
unexpected. Behaviour re-checked after the merge on the merged code:

* Gate: `automatic_preparation_check` over the copy gives gate-open **5** (52, 212, 551, 772,
  2351); #3203 BASF Praktikum is `NOT_EMPLOYMENT_POSTING`. Blocks: Praktikum/Intern/Werkstudent/
  Abschlussarbeit/Thesis by title, `APPRENTICESHIP` and `SELF_EMPLOYMENT` by posting type; trainee
  programmes stay admissible (unit test).
* API docs: `/docs`, `/redoc`, `/openapi.json` are `None` by default (test) and
  `API_DOCS_ENABLED=true` turns them on (checked in a separate process: `/docs /redoc /openapi.json`).
  The enable path has no automated test; a one-line test would be a cheap follow-up.
* **Follow-up defect (P3, fail-closed, not fixed in this stage):** the title pattern `intern(ship)?`
  in `shortlist_gate.py` has no word boundary, so titles such as "Interne Revision",
  "Softwareentwickler für interne Systeme", "… Internet-Filiale" or "… Mietwagen International"
  are also treated as internships (19 of 227 matching titles on the copy). Effect: they can never
  be auto-prepared (they stay in manual review). No gate-open candidate is affected today (the
  gate-open set is 5 with or without it). Fix: `\bintern(ship)?\b` plus a test, in a separate small PR.
  Production `.env` was not touched and does not set the variable.

## 2. Merge commit and main

`main` = `6f5c7f0`. The user's untracked files and `CLAUDE.md` in `E:\Projects\job-triage`
(branch `docs/stage60-final-status`) were not touched; Stage 63 documents live in the new worktree
`E:\Projects\job-triage-stage63` (branch `docs/stage63-final-integration`).

## 3. Main CI

Run 42971800250 (push to main, `6f5c7f0`): `test` success, `docker` success, `scheduler-postgres` success. Full pytest, Ruff and the PostgreSQL job therefore ran on the merged code; I did not repeat the suite locally.

## 4. Production baseline (read-only)

3 234 jobs, 25 applications (SUBMITTED 8, RECEIVED 8, REJECTED 9 as reported in Stage 61), 54
events, revision `a7c3e5f1b9d2`. Safety flags in `.env`: outbound, scheduler, auto-prepare, Gmail
cycle, follow-up cycle, Telegram digest, company-research auto, Alembic auto-upgrade all `false`.
The UI server is not running (no listener on 8000), so nothing was started with a write-capable
configuration. Observation for the operator: `TELEGRAM_BOT_TOKEN` is **non-empty** in `.env`
(the Stage 61 report says "no token"). Bot-enabling flags are not set, so it is inactive, and
`run_operator_ui.py` forces it off; confirm that is intended.

## 5. Why production differs from the `stage61-final` backup

Not a mystery and not corruption: it is Stage 61's own write steps. Data-based chain, compared
table by table (digest of every row) on copies:

| From → to | Changed tables | Evidence |
|---|---|---|
| `pre-stage61` backup (20:03Z) = `stage61-final` backup (20:57Z) | none | identical SHA-256 `97874106…b7cd`; the "final" backup was taken before any write, so it equals the "pre" one |
| `final` → snapshot `pre-stage21-…T205725Z` | none | same bytes |
| → snapshot `pre-stage22-refresh-ba-…T210123Z` | `job_fit_assessments` | rollout step "fit cache: 3 234 rows written" (log `stage_k.log`: stale 3 234 → written) |
| → snapshot `pre-stage21-…T210157Z` | `jobs`, `automation_runs` | BA refresh: `automation_runs` has one COMPLETED `manual:ba_refresh` run (21:01:24Z, "updated 1077, failed 0") |
| → production now | `job_fit_assessments` | rollout step "1 077 affected assessments recomputed" |

Row level, `final` vs production: 1 077 `jobs` rows changed, **all `source=bundesagentur`**,
every one with `full_time_offered` filled (NULL → 1 for 1 006, NULL → 0 for 71), plus a few
workplace/home-office fields on ≤ 10 rows each. No row added or removed (3 234 ↔ 3 234).
Seven application-linked jobs (39, 174, 195, 365, 379, 828, 2892) differ **only** in
`full_time_offered`, matching the "7 allowed ids" of the Stage 61 report. All 3 234 fit rows were
recomputed (new `input_key`/`computed_at`); content changes: 202 tier, 40 decision, 222 work mode,
1 `applied` flag (job 2892, which has an application). `applications`, `application_events`,
`alembic_version` and every other table: identical.

Conclusion: the difference is exactly what the Stage 61 report lists after the backup (fit-cache
write, BA refresh, affected assessments), with timestamps and snapshots matching. Only the
snapshot hashes of the intermediate states and this comparison were available; a SHA of the live
file "before" quoted in the Stage 61 report (`8a1b6e12…`) does not equal the backup's; a byte-level
difference between a live file and an `sqlite3` backup copy with identical content is plausible
but was not proven. No rollback was done or is needed. The operator's question "who changed
it" is answered: the Stage 61 rollout, by design.

## 6. API-key rotation plan

`docs/STAGE63_API_KEY_ROTATION_PLAN.md`. Key is 23 characters (rule: ≥ 32) → `weak`; value never
printed. No Postman environment in the repo holds it (check your cloud workspace variable); CI
does not use it; UI sessions are signed out on rotation. Not rotated without your approval.

## 7. Daily Discovery readiness

`docs/STAGE63_DAILY_DISCOVERY_MANUAL_MODE.md`; runbook §3 exit-code text corrected. Rehearsal on a
copy: found 1 407, **279 new**, 28 reference conflicts (not merged), 0 fingerprint duplicates,
first apply ended with **exit 3 (lease lost, 83 created)**: the heartbeat thread of the collector
exclusion lease got an `OperationalError` while the long write ran (SQLite). The second apply
created the other 196 and ended `APPLIED`; protected tables unchanged; applications/events
byte-identical to production. This occurred once in three BA applies (Stage 61 production and
Stage 62 copy ran clean); cause not proven (a write lock contention or a security scanner on the
fresh copy are possibilities). It is a robustness follow-up, not a data risk: the procedure is
idempotent and says what to do. Ready for manual daily use once you approve the first production run.

## 8. Candidates

`docs/STAGE63_JOB_CANDIDATES.md`. 5 gate-open, 10 junior titles, 279 new: **none confirmed
suitable.** Read first: #324, #212, #551, #310, #347. After matching the 279 new postings are
189 NOT_SUITABLE / 72 NEEDS_OPERATOR / 18 STRETCH / 0 GOOD or PARTIAL. #3264 is already applied
(duplicate protection works).

## 9. Laptop resources

* Processes: no pytest, codex, uvicorn or stray python before my work; only my two rehearsal
  python processes were present during it, and they ended.
* E: 230 GB free (C: 370 GB). `E:\JobTriage-Temp` 2.4 GB (pytest-of-V 1.2 GB, s62 337 MB, s63
  383 MB, snap 164 MB, rehearsal 383 MB), backups 0.1 GB. Nothing deleted.
* Tests: there is no xdist, so the full suite (9 024 tests) runs serially: about 6 h locally,
  20–22 min on GitHub. 16 logical CPUs idle.
* Reboot: Windows restarted at 03:47 on 11.11. The last Claude session had finished at 23:08
  (22:08 UTC); nothing was lost, no interrupted command.
* Safe night mode (recommended; nothing changed automatically):
  1. Run the full suite in GitHub CI, locally only targeted modules.
  2. Before any long local job commit/push a WIP branch (checkpoint) and write progress to
     `E:\JobTriage-Temp\logs`.
  3. One-suite lock: start with
     `New-Item E:\JobTriage-Temp\locks\suite.lock -ItemType File -ErrorAction Stop` and remove the file
     in `finally`; a second start fails.
  4. Set `--basetemp E:\JobTriage-Temp\pytest-run` and `-p timeout`/`--timeout` so a hang ends.
  5. Windows Update: set Active hours or "Pause updates" yourself before a long night run; I did
     not touch any Windows setting.

## 10. Safety flags

All outbound/automation flags false in `.env`; no server running; no migration; no `.env` or SQLite
write; no Gmail/Telegram/portal action. Stage 63 changes only documents.

## 11. Open questions

1. Approve the first production Daily Discovery run (needs backup, UI stopped).
2. Approve key rotation (plan ready).
3. `TELEGRAM_BOT_TOKEN` is set in `.env`; was that intended after Stage 61?
4. Lease heartbeat `OperationalError` on SQLite during a long write: investigate later (separate PR).
5. Product decisions from Stage 62 (S62-04…S62-08) remain open; second off-disk backup missing.
6. `main` in `E:\Projects\job-triage` is on an old docs branch; switch when convenient.

## 12. Next steps for the operator

1. Review and merge the Stage 63 docs PR (opened by me, not merged).
2. Read the candidates in the order of `STAGE63_JOB_CANDIDATES.md`; open XING links yourself.
3. Say "go" for the production Daily Discovery run (backup is step 0).
4. Rotate the API key per the plan when convenient.
