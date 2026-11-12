# JobTriage v1.0.0

First stable release of the operator-driven job-search tool. **FEATURE_FREEZE = TRUE** from this
release (organisational rule, see the last section).

## What it is

A local Windows/FastAPI tool that collects vacancies from the Bundesagentur für Arbeit, scores them
against the operator's confirmed candidate profile, explains why a vacancy fits or not, prepares
CV and cover-letter drafts from confirmed profile facts only, and tracks every application and
employer reply. **It does not apply for you.** Every real submission is made by the operator by hand
(portal, e-mail, job board) and only *recorded* afterwards. It is **not** a fully automatic system.

## Main functions

| Area | What works (checked in the 2026-11-12 acceptance test on a copy of production) |
|---|---|
| Collect | BA search through the API or `scripts/stage22_refresh_ba.py`; pages up to the announced total (remote search: 5 886 of 5 913 announced results; commute + remote), drops repeats, reports `COMPLETE` or `PARTIAL_*`; re-run is idempotent |
| Assess | Skill extraction (Python, FastAPI, Django, PostgreSQL under Markdown headers too), fit tier with reasons per criterion, junior/entry-level, role, language, commute, remote |
| Review | Browser UI (`/ui/`): vacancy list with filters, vacancy card with criteria and evidence, manual review queue, applications with history, inbox, follow-ups |
| Prepare | Matcher, CV draft, Bewerbung draft, review package with human approval; every statement bound to a confirmed profile fact (projects, employers, education, German B1 verified identical to the profile) |
| Track | Record a submission, receipt, interview, rejection, offer; append-only event history; duplicate protection (re-applying is refused, HTTP 409 / `ALREADY_APPLIED`) |
| Safety | API key auth (401 without), strong-key check, all outbound flags off by default, `/docs` off, pre-submit check `scripts/presubmit_check.py` |

## Disabled by default (do not enable casually)

`OUTBOUND_SENDING_ENABLED`, `TELEGRAM_BOT_ENABLED`, `TELEGRAM_JOB_ALERTS_ENABLED`,
`TELEGRAM_GMAIL_DRAFT_ENABLED`, `TELEGRAM_OPERATOR_CONTROL_ENABLED`, `AUTOMATION_SCHEDULER_ENABLED`,
`AUTOMATION_AUTO_PREPARE_ENABLED`, Gmail/follow-up cycles, company-research auto. The launcher
`scripts/run_operator_ui.py` forces all of them off for its server process. With sending off the send
endpoints answer 503 (observed in the acceptance test).

## Limitations (read before relying on it)

* Only the Bundesagentur API and XING alert e-mails are collected. No LinkedIn, Indeed, StepStone, career pages.
* Remote search keeps only postings the employer flagged `homeofficemoeglich`; a fully remote posting without the flag is not found by it.
* The UI fit-tier / applied / work-mode filters and the review queue read the assessment cache. After any
  code update (the cache key includes a hash of the rule modules) the cache is stale until you run the
  assessment step (`scripts/stage21_assess_vacancies.py --apply`, with a backup). Stale rows are left out of those filters.
* The UI has no user-selectable sort (vacancies newest first, review queue by a fixed key).
* The REST collector response does not include the completeness status; a partial search is logged
  (`bundesagentur_partial_results`) and the refresh CLI prints `COMPLETE`/`PARTIAL_*` per search.
* Drafts are deterministic templates (no LLM): correct and traceable, but generic. Read and edit every letter.
* The CV draft carries a `CONTACT_DATA_NOT_MODELED` warning; final PDFs are produced and checked by the operator.
* Scoring `jobs.score/recommendation` can be older than the fit tier; **trust `fit_tier`**, not `decision`.

## Known non-critical issues (full list: `docs/FINAL_ISSUES_REGISTER.md`)

No P0 is open. Open P1 items are about unattended operation and hygiene, not about data loss or
unapproved sending in manual use: OPS-02/ARC-07 (no alerting or liveness signal for background
workers), OPS-03 (no scheduled backup registered; script exists), DB-02 (backup without schedule),
DB-01 (5 known duplicate source-reference groups, no unique index yet), UI-01, FLOW-15, PERF-02
(remainder), DEP-01 (Linux pins), API-03 / ARC-03 (partial). These are accepted for a manually
operated v1.0.0 and are **not** claimed as closed. P2/P3 items are tracked in the register.

## Requirements

* Windows 10/11, PowerShell, Python >= 3.11 (developed on 3.13), project virtualenv in `.venv`.
* `pip install` from `pyproject.toml` / `constraints.txt`; SQLite (runtime DB) or PostgreSQL (CI/Docker).
* `.env` in the project folder with a strong `API_KEY` and `BUNDESAGENTUR_API_KEY`; never commit it.
* Runtime DB `runtime\job_triage_runtime.db` (not in git). Check the schema: `/api/v1/ready` must say `ready`.

## Start

```powershell
cd E:\Projects\job-triage
.\.venv\Scripts\python.exe scripts\run_operator_ui.py `
    --database runtime/job_triage_runtime.db `
    --snapshot "$env:USERPROFILE\JobTriage-Backups"
```

Open <http://127.0.0.1:8000/ui/>, log in with `API_KEY`, stop with Ctrl+C. API without UI: see
`docs/DAILY_OPERATIONS.md` section 1.

## Backup and restore

```powershell
# backup (online-safe, verified, keeps the newest 14)
.\.venv\Scripts\python.exe scripts\backup_sqlite.py --db runtime\job_triage_runtime.db `
    --dest "$env:USERPROFILE\JobTriage-Backups" --keep 14
# restore into a NEW file first and check it
.\.venv\Scripts\python.exe scripts\restore_sqlite.py --backup "<backup file>" --target runtime\restored_check.db
```

Replacing the live file needs the app stopped and `--replace` (and `--allow-production` for the production path);
read `docs/BACKUP_AND_ROLLBACK.md` first. Both commands were run on a copy during the acceptance test.

## Manual search and preparing a Bewerbung

See `docs/DAILY_OPERATOR_WORKFLOW.md` (step by step) and `docs/DAILY_OPERATIONS.md` (reference).

## Feature freeze (FEATURE_FREEZE = TRUE)

Allowed: fixes for blocking bugs, security fixes, regression fixes, real search problems, data-loss fixes.
Not allowed: new collectors, Telegram/Gmail/AI functions, architecture rewrites, cosmetic refactoring,
new Stage documents, new dependencies without need, API changes without a confirmed defect. P3 items are not
a reason to break the freeze.
