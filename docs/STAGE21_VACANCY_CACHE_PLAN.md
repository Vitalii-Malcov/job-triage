# Stage 21 — Persistent Vacancy Assessment Cache (plan)

Status: **plan only**. No migration, no backfill and no production change has
been made. Implementation needs the operator's go; every production step below
needs its own explicit approval.

## 1. Problem (measured on the real database, 29.10.2026)

Stage 20 computes the fit tier on every request (`assess_job`, ~17 ms per
vacancy) with a scan budget of 200 per request:

| Observation (2 888 vacancies, read-only UI) | Effect |
|---|---|
| Dashboard fit-tier counts | `UNKNOWN` — more than 200 vacancies in the 14-day window |
| `Python` + `GOOD_MATCH` | 0 on page 1 after ~4.2 s, then "continue scanning" |
| Full catalogue | ~50 s of assessment if scanned completely — never per request |

The operator cannot answer "which vacancies fit me?" in one view.

## 2. What the fit tier depends on (from `app/services/job_fit_assessment.py`)

`assess_job(session, job_id)` reads, and nothing else (no clock: no criterion
uses the current date):

| Input | Where | Changes when |
|---|---|---|
| Vacancy row | `jobs`: title, company, location, description, url, skill lists; the duplicate check (`application_guard.vacancy_findings`) also reads `fingerprint` and `source` | re-collection updates a posting, a fingerprint is corrected |
| Candidate profile + search configuration | `candidate_profiles.profile_version` | every profile or config change (CAS `+1`, `candidate_profile_repository` / `candidate_search_repository.apply_config_plan`) |
| Latest matcher analysis | `candidate_job_matches` (latest per job) | a new match run |
| Latest commute check | `job_commute_checks` (latest per job) | a new route check |
| Duplicates / already applied | `applications` + `application_events` (all) | any application or event |
| Rules | the code of `job_fit_assessment.py` **and every `app.*` module it imports** (e.g. `app/agents/job_fit_signals.py`, `application_guard`, the search-config models) | a code change |

Note: the existing `compute_job_snapshot_fingerprint` covers title,
description and skills only — **not location, fingerprint or source**, which the
location and duplicate criteria use. The cache needs its own content hash.

## 3. Design

### 3.1 Table `job_fit_assessments` (new, one current row per vacancy)

| Column | Meaning |
|---|---|
| `job_id` | PK, FK → `jobs.id` (`ON DELETE CASCADE`) |
| `input_key` | SHA-256 over all inputs of §2 (see 3.2) |
| `rules_hash` | the rules hash (3.2) at computation |
| `profile_version` | profile version used |
| `fit_tier` | `GOOD_MATCH` / `PARTIAL_MATCH` / `NEEDS_OPERATOR` / `NOT_SUITABLE` (CHECK constraint) |
| `decision` | as `FitAssessment.decision` |
| `criteria_json` | every criterion: name, status, reason, evidence — the same data the detail page shows |
| `tier_reasons_json` | the reasons list |
| `work_mode`, `applied` | the two other computed list filters |
| `computed_at` | UTC |

Indexes: `(fit_tier, job_id DESC)`, `(input_key)`. Alembic migration with a
real `downgrade()` (drop table). No change to existing tables.

### 3.2 Freshness key — a stale tier is never shown as current

`input_key = sha256(rules_hash | profile_version | job_row_hash |
latest_match_id + its snapshot fingerprint | latest_commute_check_id |
applications_watermark)`

- `job_row_hash`: **every** column of the `jobs` row except an explicit
  exclusion list, today only `last_seen_at` (it changes on every collection
  run and no criterion reads it). A test reads the source of the assessment's
  import closure and fails if an excluded column is ever used there, and a
  test changes each included column alone and expects the row to go stale
  (including `fingerprint` and `source`, Codex plan review).
- `applications_watermark`: `count(applications)` + `max(application_events.id)`.
  Any application change marks **all** rows stale. That is deliberate: there are
  15 applications, changes are rare, and a full recompute is ~50 s offline.
  Per-vacancy dependency tracking would be more code for little gain.
- `rules_hash`: computed at runtime, not maintained by hand. It is the SHA-256
  over the source files of `app.services.job_fit_assessment` and every
  `app.*` module in its transitive import closure (the same import-graph
  walk `tests/test_stage16_operator_notifications.py` uses), in sorted
  order. Any change in imported logic (e.g. `extract_german_requirement` in
  `app/agents/job_fit_signals.py`) changes the hash, so every cached row
  becomes stale and nothing old looks current. A broad change, such as a model
  file, also invalidates everything; a full recompute is ~50 s offline. A test
  changes a dependency-only module (in a temporary copy) and expects a new
  hash (Codex plan review).

The UI computes the current key per row with SQL only (job columns, profile
version, two indexed lookups, one global watermark). It **never** runs
`assess_job` for the list. A row whose stored key differs is shown as
`STALE (recompute pending)`, never as its old tier; a vacancy without a row is
`NOT_ASSESSED`. Counts on the dashboard come from fresh rows only and state
how many are stale or missing.

### 3.3 Writer — offline, explicit, resumable

`scripts/stage21_assess_vacancies.py --database <file> [--apply] [--only-stale] [--limit N]`

- Default is a **dry run**: it computes and prints counts (would insert,
  update, unchanged) and writes nothing.
- `--apply` upserts in batches of 200, one transaction per batch, and
  prints progress (`done/total`, rate, ETA). Ctrl+C keeps every finished
  batch. Re-running continues with stale and missing rows only (idempotent;
  the upsert compares `input_key`).
- It refuses a backup file and applies the Stage 17 database-role rules like the
  other tracker CLIs; the production run is preceded by a backup-API
  snapshot (`--snapshot DIR`).
- It runs no collector and no matcher, sends nothing, and changes no vacancy,
  application or profile. It writes only `job_fit_assessments`.

The Web UI stays read-only (`query_only` / `default_transaction_read_only`).
Progress of a running backfill is visible on the dashboard as
`fresh / stale / missing` counts, read from the table.

### 3.4 UI changes (read-only)

- Dashboard: fit-tier counts over all vacancies (and the 14-day window) from
  fresh rows, plus `stale` / `not assessed` counts. There is no `UNKNOWN`
  because of a budget.
- Vacancy list: `fit_tier`, `applied` and `mode` filters become SQL filters on the
  cache (keyset pagination as today); the scan budget remains only as a
  fallback when the table is absent (old schema) and says so.
- Vacancy detail keeps computing live (one vacancy, ~17 ms), so the operator
  always sees the current evidence; it shows whether the cached row is fresh.

## 4. Tests

| Area | Test |
|---|---|
| Equivalence | for a fixture catalogue, cached tier/decision/criteria == `assess_job` |
| Invalidation | each input of §2 changed alone → row becomes stale (every included `jobs` column incl. `fingerprint` and `source`, profile version, new match, new commute check, new application, new event, rules hash) |
| Never stale-as-current | a stale `GOOD_MATCH` row is listed as `STALE`, not in the `GOOD_MATCH` filter, not counted |
| Writer | dry run writes nothing; batches commit; interruption resumes; idempotent second run; refuses backup files; role rules |
| UI read-only | the UI never writes the cache (existing write-guard tests extended) |
| Rules hash | covers the whole import closure; a dependency-only change yields a new hash; excluded `jobs` columns are never read by the closure (§3.2) |
| PostgreSQL | migration up/down on real PG; concurrent writers (two backfills) leave one consistent row per job; the UI on a `default_transaction_read_only` connection; added to the CI zero-skip gate |
| Migration | `alembic upgrade head` from the Stage 20 head on SQLite and PostgreSQL; CI head literal updated |

## 5. Rollout (each production step needs explicit approval)

1. Implement on a branch; full pytest, Ruff, PostgreSQL gate, Codex review, PR.
2. On a **copy** of the production database: migrate, dry run, `--apply`
   backfill, measure the time, and compare tiers with live `assess_job` for a
   sample.
3. With approval: backup snapshot → `alembic upgrade head` on production →
   dry run → `--apply` backfill → verify counts (2 888 rows, 0 stale).
4. After each later collection or profile change, run
   `stage21_assess_vacancies.py --only-stale --apply` (manually or as an
   opt-in scheduler step).

## 6. Effort estimate

| Part | Days |
|---|---|
| Model, migration, repository, key computation | 1 |
| Writer CLI with progress, dry run, roles | 1 |
| UI integration (dashboard, SQL filters, freshness display) | 1 |
| Tests SQLite + PostgreSQL, CI gate | 1.5 |
| Codex rounds, docs, copy-of-production rehearsal | 1 |
| **Total** | **≈ 5.5 working days** |

## 7. Further proposals (separate, not part of the cache)

### A. PDF export CLI (≈ 1–1.5 days)

`scripts/stage21_export_documents.py --database <file> --application <id> --out <dir> [--format pdf|docx]`

- Input: an application whose review package is approved (`review_status ==
  APPROVED`) and a confirmed identity. Otherwise it refuses, naming the
  missing piece.
- Calls the existing `export_package_documents(document_format="pdf")`,
  which writes the PDFs and their `_quelle_html` sources, never over existing
  files.
- Then the operator looks at the PDFs and runs
  `presubmit_check.py --bind`. Even `READY` is not an instruction to send. No
  e-mail, no portal, no Telegram; the command only reads the database.

### B. Gmail inbox for the 15 applications (≈ 1–2 days, mostly an operations runbook)

Today `gmail_messages` = 0, so the inbox page has nothing to show. Existing
tools cover the flow; proposed sequence, each write step only with approval:

1. Backup snapshot of production.
2. Read-only preview (IMAP, `readonly` SELECT, `BODY.PEEK`, writes nothing):
   `stage15_application_tracker.py --database runtime/job_triage_runtime.db process-gmail --source imap --since 2026-10-01`
   This prints correlations against the 15 applications and `NEEDS YOUR DECISION`.
3. With approval: store the messages with `POST /api/v1/gmail/sync`. It writes
   `gmail_messages` / `gmail_threads` only; IMAP stays read-only.
4. The UI inbox then shows the dry run over the stored mail.
5. With approval per batch: `process-gmail --source stored --apply`. It records
   receipts and invitations. Rejections and offers stay operator-confirmed (Stage 15
   rule).
6. Code work: a small `--sync-only` / `--preview` wrapper so steps 2–3 do
   not need the API, and the per-message preview printing codes only.

### C. Browser operator actions (≈ 3–4 days, later)

- POST forms with a per-session CSRF token (in addition to SameSite=Strict
  and the Origin check).
- A two-step confirmation page that shows the exact change; the reason is required.
- The write goes through the existing `ApplicationTracker` (policy table,
  append-only events) with `origin=OPERATOR` and `actor=ui:<session>`.
- A separate read-write engine used only by the action routes; every other
  route stays on the read-only session.
- No action that sends anything (mail, draft, Telegram, portal) — those stay
  outside the browser.
- Tests: CSRF missing/wrong → 403; replay; double submit; audit row present.

## 8. Implementation record (2026-10-29)

Branch `stage21/vacancy-cache` in a separate worktree
(`E:\Projects\job-triage-stage21`): the main working copy doubles as the
production environment, and its CLIs and UI require the database to be at
the code's Alembic head, so a new migration there would make
`presubmit_check.py` / `record-submission` refuse the production database
before its (separately approved) migration.

Baseline (main `4cdaacf`, clean tree apart from the operator's own
untracked files): Ruff check and format clean; full pytest see the handoff
section below. Production SQLite SHA-256 at start: `21a4c93b…` (changed
legitimately on 29.10.2026 by `record-submission` of applications 16–18).

| Task | Result | Files |
|---|---|---|
| T1 model + migration | `JobFitAssessmentRecord`, revision `c7e2a9d4f1b3` (down `b6c1e9a4d2f7`), real `downgrade()`; head literals in CI and tests updated | `app/db/models.py`, `alembic/versions/c7e2a9d4f1b3_*.py`, `.github/workflows/ci.yml`, head tests |
| T2 key computation | `app/services/fit_cache.py`: rules hash over the import closure of `job_fit_assessment` + `fit_cache`, job row hash minus `last_seen_at`, latest match / commute keys (same ordering as the live lookups), applications watermark (count, max id, max event id), dialect upsert (`ON CONFLICT DO UPDATE`) | `app/services/fit_cache.py` |
| T3 writer | `scripts/stage21_assess_vacancies.py`: dry run on a `mode=ro` / read-only connection, `--apply` in batches of 200 with progress/ETA, resumable, `--only-stale`, `--limit`, `--snapshot` (mandatory for the production file), Stage 17 database roles + Alembic head | `scripts/stage21_assess_vacancies.py` |
| T4 UI | dashboard tier counts (window + all) from FRESH rows with stale / not-assessed counts; computed list filters as SQL on the cache, page rows still assessed live; detail shows the cached state; Stage 20 budget path kept while the table is absent or empty | `app/ui/queries.py`, 3 templates |
| T5 tests | SQLite: equivalence, per-column and per-input invalidation, rules-hash closure, excluded column never read, never stale-as-current, writer dry run / idempotence / limit / only-stale / interruption / roles / snapshot / missing table, UI cannot write. PostgreSQL (CI gate): constraints, concurrent writers, read-only UI connection, one-step downgrade/upgrade | `tests/test_stage21_vacancy_cache.py`, `tests/integration/test_stage21_vacancy_cache_postgres.py` |
| T6 docs | operator section 1b, this record | `docs/DAILY_OPERATIONS.md`, this file |

Deviations from §3, decided during implementation:

- The UI keeps the Stage 20 scan-budget path not only without the table but
  also while it is empty (before the first backfill), so the dashboard does
  not switch to "everything not assessed" the moment the migration runs.
- The filtered list selects candidates from FRESH cache rows but still
  assesses the (at most 25) page rows live, so the evidence shown is always
  current; a row whose live result disagrees with its filter is dropped.
- `--only-stale` means "existing stale rows only"; the default run already
  skips fresh rows, so it is resumable and idempotent without a flag.

Copy-of-production rehearsal (§5.2, 29.10.2026): backup-API copy of the
production file (opened `mode=ro`) in a temporary directory, deleted
afterwards. `alembic upgrade head` on the copy; dry run 2 888 not assessed;
`--apply` wrote 2 888 rows in 55 s (while a full pytest run shared the
machine); the second dry run reported 2 888 fresh / 0 stale; a random sample
of 300 rows was identical to live `assess_job` (0 mismatches); the cached
dashboard took 0.41 s and showed counts over all vacancies (NEEDS_OPERATOR
2 501, NOT_SUITABLE 387 -- no vacancy has a current matcher analysis, so no
GOOD/PARTIAL match, the same as the live assessment). Production SHA-256
`21a4c93b…` before and after.

Verification:

| Check | Result |
|---|---|
| Baseline full pytest (clean main `4cdaacf`) | 6 634 passed, 127 skipped, 0 failed |
| Full pytest, Stage 21 worktree (before the Codex fixes) | 6 674 passed, 131 skipped, 0 failed |
| Stage 21 + Stage 20 UI tests after the Codex fixes | 70 passed |
| Full pytest after the Codex fixes (final) | 6 675 passed, 131 skipped, 0 failed |
| Ruff check / format | clean (480+ files) |
| PostgreSQL tests (`test_stage21_vacancy_cache_postgres.py`, wired into the CI zero-skip gate) | first session: NOT RUN locally; overnight run: see §10 (Docker is reachable through WSL) |
| Codex review round 1 | REJECT: P1 PG concurrency test began a transaction twice; P2 applications watermark missed edits of older rows |
| Fixes | `session.rollback()` after the reads in the PG test; watermark = hash over every column of every application + max event id; new two-application negative test |
| Codex review round 2 (final) | ACCEPT, no P0/P1 |

Not done here (each needs the operator's explicit approval): production
backup snapshot, `alembic upgrade head` on production and the production
backfill (§5.3).

## 9. Production deployment and rollback runbook

**Status: NOT DEPLOYED.** Every step below needs the operator's explicit
approval; nothing here has been run against production. Commands are
PowerShell in `E:\Projects\job-triage` (the production working copy).

Facts this order depends on:

- Every tracker CLI, `presubmit_check.py` and the UI refuse a database whose
  Alembic revision differs from the code's head
  (`app/core/canonical_runtime.py`: `DATABASE_NOT_AT_MIGRATION_HEAD`). Code
  and schema therefore move **together**: old code (`b6c1e9a4d2f7`) refuses
  the migrated database, new code (`c7e2a9d4f1b3`) refuses the unmigrated
  one. Between steps D2 and D3 no CLI works -- keep that window short.
- The migration only adds `job_fit_assessments`; no existing table or row is
  changed. The cache is derived data and can always be rebuilt from `jobs`,
  `candidate_job_matches`, `job_commute_checks`, `applications` and the
  profile.
- The production file uses `journal_mode=delete` (no WAL); a copy made with
  SQLite's backup API is consistent, a plain file copy of a file being
  written is not.

### Preflight (P1-P6)

1. Code: `git log -1 --oneline` = `4cdaacf` (or the commit the PR was
   merged on top of); `git status` shows only the operator's own files.
2. Revision (read-only):
   `.\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('file:runtime/job_triage_runtime.db?mode=ro',uri=True);print(c.execute('select version_num from alembic_version').fetchone(), c.execute('pragma integrity_check').fetchone())"`
   -> `('b6c1e9a4d2f7',) ('ok',)`.
3. Free disk space: at least 3x the database file (backup + migration +
   cache rows, ~2 MB per 3 000 vacancies).
4. No writer running: stop the operator UI (Ctrl+C in its window), the
   scheduler and Telegram bot if started, and do not run tracker CLIs or a
   collection until V5. `Get-Process python` must show none of them.
5. Pick a moment with no Bewerbung in progress (no form half-filled, no
   pending `record-submission`).
6. Note the SHA-256 of the file: `Get-FileHash runtime\job_triage_runtime.db`.

### Backup (B1-B3)

1. Backup with SQLite's online backup API, outside the repository
   (DAILY_OPERATIONS §11):
   ```powershell
   $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
   $bk = "$env:USERPROFILE\JobTriage-Backups\job_triage_runtime.pre-stage21-migration-$ts.db"
   .\.venv\Scripts\python.exe -c "import sqlite3,sys;s=sqlite3.connect('file:runtime/job_triage_runtime.db?mode=ro',uri=True);d=sqlite3.connect(sys.argv[1]);s.backup(d);d.close();s.close()" $bk
   ```
2. Verify the backup: `PRAGMA integrity_check` = `ok`,
   `PRAGMA foreign_key_check` empty, `alembic_version` = `b6c1e9a4d2f7`,
   row counts of `jobs` / `applications` equal to the production file.
3. Record `$ts`, the backup path and `Get-FileHash $bk` in the operator log.

### Deployment (D1-D6)

1. Merge PR #18 on GitHub (operator decision), then in the production
   working copy: `git fetch origin` and `git merge --ff-only origin/main`.
   Do not discard the operator's uncommitted files.
2. Migrate, with the target named explicitly (`.env` points at a pilot
   database, and a set `POSTGRES_HOST` would override `DATABASE_URL`):
   ```powershell
   $env:DATABASE_ROLE = "production"
   $env:DATABASE_URL = "sqlite:///runtime/job_triage_runtime.db"
   $env:POSTGRES_HOST = ""
   .\.venv\Scripts\python.exe -m alembic current   # must print b6c1e9a4d2f7 -- else STOP
   .\.venv\Scripts\python.exe -m alembic upgrade head
   .\.venv\Scripts\python.exe -m alembic current   # c7e2a9d4f1b3 (head)
   Remove-Item Env:DATABASE_URL, Env:DATABASE_ROLE, Env:POSTGRES_HOST
   ```
3. Dry run (read-only):
   `.\.venv\Scripts\python.exe scripts\stage21_assess_vacancies.py --database runtime/job_triage_runtime.db`
   -> `fresh=0 stale=0 not_assessed=<all vacancies>`.
4. Backfill (takes its own backup copy first; ~1 minute for 2 900
   vacancies):
   `... scripts\stage21_assess_vacancies.py --database runtime/job_triage_runtime.db --apply --snapshot "$env:USERPROFILE\JobTriage-Backups"`
   -> `APPLIED: <n> rows written`, exit 0.
5. Second dry run -> `fresh=<all> stale=0 not_assessed=0 to_assess=0`.
6. Start the UI with the launcher (`scripts/run_operator_ui.py`): the
   dashboard shows tier counts instead of UNKNOWN and `0 stale`.

### Verification and smoke tests (V1-V5)

1. `integrity_check` = `ok`; `select count(*) from job_fit_assessments` =
   number of vacancies; `applications` row count and the newest three
   applications unchanged (compare with P6/B2 counts -- the migration and
   the backfill never write them).
2. Tracker CLI read path works again:
   `scripts\stage15_application_tracker.py --database runtime/job_triage_runtime.db list`.
3. UI: dashboard, `/vacancies?fit_tier=NEEDS_OPERATOR`, one vacancy detail
   page shows "Cached assessment FRESH"; the launcher ends with `UNCHANGED`.
4. V2 is the head-compatibility check: the tracker opens the database
   through the same head-equality check as `presubmit_check.py` and refuses
   it otherwise (`--help` alone would prove nothing).
5. Re-enable normal operation (collection, scheduler if used). After each
   collection or profile change run the script again (only missing/stale
   rows are assessed).

### Rollback

- **Backfill fails or is interrupted (D4):** nothing authoritative is
  affected; re-run D4 (finished batches of 200 are kept, the rest is
  assessed again). A partial cache only means "not assessed" counts.
- **The UI misbehaves with the cache:** empty the cache (it is derived):
  `delete from job_fit_assessments` -- the UI falls back to the Stage 20
  behaviour while the table is empty. Needs approval like any production
  write.
- **Code rollback to Stage 20:** requires the schema back at
  `b6c1e9a4d2f7` (the old code refuses a newer revision):
  `alembic downgrade b6c1e9a4d2f7` (drops only `job_fit_assessments`, with
  the same explicit environment as D2), then
  `git checkout 4cdaacf` in a clean state or revert the merge commit.
- **Migration itself fails or the file is damaged:** stop everything, keep
  the current file as `job_triage_runtime.broken-<ts>.db`, copy the B1
  backup to `runtime\job_triage_runtime.db`, verify as in B2, keep the code
  at `4cdaacf`.
- **Data written after the backup:** restoring the B1 backup loses every
  application, event or collection written after B1. Prefer the downgrade
  (it keeps all authoritative rows); restore only if the file is damaged,
  and then re-record later submissions from their evidence files
  (`submission_evidence/`) via `record-submission`, with approval.

## 10. Overnight verification (2026-10-29/30)

| Check | Result |
|---|---|
| Local PostgreSQL (disposable `postgres:16` container via WSL Docker, `127.0.0.1:55432`, synthetic credentials) | `alembic upgrade head` from empty -> `c7e2a9d4f1b3`; the full CI PostgreSQL list: 126 passed, 0 skipped |
| GitHub Actions, PR #18, run 40963735783 (commit `c086f46`) | `test`: Ruff clean, 6 674 passed / 132 skipped; `scheduler-postgres`: 126 tests, 0 skipped; `docker`: pass |
| Codex audit round 3 (full diff, 15 questions) | REJECT, no P0/P1; two P2 (below) |
| P2 older writer replaces a newer row | fixed: `upsert_assessment(expected_key=...)` is a compare-and-swap (`ON CONFLICT DO UPDATE ... WHERE input_key = observed`, `RETURNING` for the result -- psycopg reports rowcount -1); the writer passes the key it read in the batch transaction. Tests: SQLite CAS unit test, PostgreSQL two-session interleaving test |
| P2 Stage 20 fallback not announced | fixed: `fit_cache.cache_status` (MISSING / EMPTY / IN_USE); the dashboard says when the cache is missing or not filled yet and how to fill it. Tests: status unit test, rendered dashboard test |
| Codex re-review round 4 (fixes only) | REJECT: P2 the writer read the observed row after the inputs (a row committed in between could still be replaced); P3 `presubmit_check.py --help` proves no head compatibility |
| Round 4 fixes | `assess_batch` reads the observed rows before `global_inputs`/`current_keys`; new interleaving test (fails with the old order, passes with the fix); runbook V4 corrected. Not re-reviewed by Codex: the overnight run was limited to two review rounds |

Performance (synthetic SQLite, this laptop, one run each, a ~2 KB
description per vacancy, every row cached and fresh; `scratchpad` only):

| Vacancies | `current_keys` (all) | dashboard | filtered list, page 1 | detail |
|---|---|---|---|---|
| 3 000 | 0.72 s, 6 SQL | 0.96 s, 15 SQL | 1.80 s, 159 SQL | 0.08 s |
| 10 000 | 2.4 s | 3.1 s | 2.0 s | 0.10 s |
| 30 000 | 9.0 s | 8.4 s, peak 20 MB | 2.6 s | 0.10 s |

No N+1 in the key computation (6 statements regardless of size); the cost
is Python hashing of every vacancy row (json + sha256, ~0.1-0.3 ms per
row, no single hotspot in the profile). The filtered list's cost is the
live `assess_job` of the ~50 displayed rows (Stage 20 behaviour, kept on
purpose so the page shows current evidence). The production copy measured
0.41 s for the dashboard with 2 888 vacancies. Linear growth makes the
dashboard slow beyond ~10 000 stored vacancies; the fix is a stored
per-vacancy content hash maintained by `upsert_job` (schema change,
follow-up), not a change of the freshness rule.

### Why there is no GOOD_MATCH (diagnosis, no rule changed)

Read-only analysis of a backup-API copy of production (2 888 vacancies,
profile version 5), imports from the unchanged `job_fit_assessment.py`:

1. **Skills are never evaluated.** 770 matcher analyses exist for 708
   vacancies, all for profile versions 3 or 4; the profile is at version 5,
   so `assess_job` treats every one as out of date ->
   `skills NOT_EVALUATED` for all 2 888 -> at least NEEDS_OPERATOR.
   2 180 vacancies were never analysed at all. The matcher
   (`compute_match`) is deterministic, with no LLM and no network, but it
   runs only on `POST /jobs/{id}/match` (or while preparing a Bewerbung).
   No pipeline step re-runs it after collection or after a profile change.
2. **Location is unverified.** 2 827 vacancies have
   `location DISTANCE_UNVERIFIED`. The car-route limit needs a stored
   commute check, which needs geocoding and routing (external services,
   operator decision).
3. A copy-only experiment re-ran the matcher for all 2 888 vacancies
   (674 s, network blocked in the process). Skills became
   STRONG 227 / PARTIAL 1 163 / WEAK 1 498. Tiers: NEEDS_OPERATOR 2 483,
   NOT_SUITABLE 401, PARTIAL_MATCH 4, GOOD_MATCH 0. 803 vacancies are now
   NEEDS_OPERATOR **only** because of location, 49 of them with STRONG
   skills; those 49 are GOOD_MATCH candidates once the route is verified.
   Other reasons (overlapping): German level to verify 1 053,
   "experienced" wording 1 032, an experience gap 238.
4. Unknown and negative evidence are kept apart: NOT_EVALUATED and
   DISTANCE_UNVERIFIED are verifications (NEEDS_OPERATOR), never barriers;
   NOT_SUITABLE comes only from blockers or confirmed gaps. The UI shows
   the reasons per vacancy.

Outside the Stage 21 acceptance criteria (the cache reproduces the live
assessment exactly). Proposed follow-up, needs approval:
"skills-analysis coverage". It would be an offline, resumable
`--apply`/`--snapshot` script that runs the deterministic matcher for
vacancies without a current analysis, a refresh after profile changes,
and then the Stage 21 cache refresh.
- Acceptance: no network or LLM; the thresholds are unchanged; the count
  of current analyses equals the number of vacancies; the copy reproduces
  the numbers above.
- Commute verification for the 803 location-only vacancies stays a
  separate, operator-approved decision about using the external services.
