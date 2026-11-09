# Final integration report: audit 2026-11-06

Branch `integration/audit-2026-11-06-release`, based on `main` d0f576e. Nothing is merged to `main`; production data, migrations, cache and `.env` were not touched.

## 1. Integration HEAD

See the PR description for the exact commit. The branch is `85e889a` (the earlier combined branch, which already contained every PR head below except #44/#45 docs) plus the docs PR #46 and ten new commits.

Production control values were re-read on 2026-11-07 (read-only) and match the nightly audit: 3 234 jobs, 25 applications, 54 application events, Alembic `d6e9a2b4c8f1`, 3 234 `job_fit_assessments` rows. The production file was last modified 2026-11-06 09:14, before this work.

## 2. Included PRs

Verified with `git merge-base --is-ancestor`: every head below is an ancestor of the integration branch. Dependencies #34 on #32, #38 on #34, #37 on #36 hold, and no commit is applied twice.

| PR | Head | Subject |
|---|---|---|
| #32 | 932d96a | developer role filtering |
| #34 | 900cb1c | shortlist fit-tier gate |
| #35 | e5b7d94 | SEC-01 linear `html_to_text` |
| #36 | 643cc6e | API id/size bounds, linear regexes |
| #37 | 9aacc9f | template secrets, auth lock-out |
| #38 | a2e7ad2 | notifications, BA outage, CV dedupe, HTML-only mail, no-reply |
| #39 | 3d8312d | scheduler key check, LF scripts, CI limits |
| #40 | e7c151f | confirmed outcome ends follow-ups |
| #41 | 650832d | one BA vacancy per reference number |
| #42 | fb65333 | verified SQLite backup tool |
| #43 | b2a9eec | Telegram start retry and unwind |
| #44 | d7555d4 | `constraints.txt` |
| #45 | 2338658 | message normalised once |
| #46 | dd1f394 | audit docs |

Not included: #31 and #33 (rollout/stabilization docs, unrelated to code).

New commits on top:

- `25a202e` readiness and component health (OPS-02, ARC-07)
- `13bc7cb` UI assessment bounds tests (UI-01)
- `21402f2` Telegram notice kinds (FLOW-15)
- `a34d046` constraints in CI and Docker (DEP-01)
- `1f81388` shared reads in mail matching (PERF-02)
- `c62508e` restore tool and backup task wrapper (OPS-03)
- `eb055cf` end-to-end shortlist report
- `7dbdeb9`, `609b129`, `3ea3b2f` fixes from the full run and from Codex review

There are no changes to `app/db/models` and no new Alembic revision. The head stays `d6e9a2b4c8f1`.

## 3. P0/P1 status

| ID | Status | What was done |
|---|---|---|
| SEC-01 (P0) | fixed (#35) | linear HTML parser |
| OPS-02 / ARC-07 | fixed | `/ready` is 503 while a configured bot is starting, retrying, failed or stopped, or after its poll task dies (restarted automatically). No token = DISABLED, readiness unaffected. `/health` stays pure liveness. `GET /health/components` (API key required) reports database, telegram, scheduler (stale schedule), collector leases (lapsed RUNNING leases). A scheduler failure degrades the report but does not fail the web container's readiness (separate process). |
| UI-01 | verified, tests added | UI handlers are plain `def` (worker pool, not the event loop), computed filters stop at the scan budget, cached pages assess only the returned rows, page views write nothing (file hash unchanged). No code change was needed. The uncached dashboard path can still run up to 200 assessments (about 3 s) in a worker thread; it is the pre-cache fallback only. |
| FLOW-15 | fixed | every operator notice has a kind (ok, transient, invalid data, not authorized, already processed, forbidden). Final kinds may not contain a retry hint; the check is a test. One ambiguous answer ("gerade nicht möglich") and one redisplay hint were reworded. Not-authorized callbacks already produce no answer. |
| DEP-01 | fixed | Docker and both CI installs use `-c constraints.txt`. Built the image: `pip freeze` equals the pins except `uvloop`, now pinned; `pip check` clean. Clean Windows venv install clean. Wheels exist for the pins on Python 3.11, 3.12, 3.14 (availability only; the suite ran on 3.13). |
| PERF-02 | fixed | dry runs (inbox view) scan candidates once per batch and read each application's events once; applying keeps fresh reads per message. Results equal uncached reads (test). The dashboard still recomputes the inbox on every view; a cache needs a freshness policy and was left alone. |
| OPS-03 | done, not enabled | `scripts/restore_sqlite.py` (verified, never clobbers, keeps the previous file), `scripts/scheduled_backup.ps1`, `docs/BACKUP_AND_ROLLBACK.md`. No schedule is registered. Restore was tested on scratch databases only. |

## 4. Remaining items (need an operator decision, not implemented)

- FLOW-01: resend after a failed first delivery needs a stored "announced" flag, so a schema change.
- FLOW-02 (reverse direction): `JobRecord.status = APPLIED` blocks a Bewerbung and there is no way to correct a wrong `/status`. Policy choice.
- FLOW-04: Reply-To handling needs stored data (migration). No-reply senders are already refused.
- DB-01: five BA duplicate pairs (job ids 105/2356, 644/1787, 363/2353, 1156/2328, 169/2354), all NEW. Job 169 has an application, 105/169/363 have Telegram reviews, all have matches and cached assessments. Merging needs a keep/redirect decision per pair, then the unique index (migration).
- ARC-03: scheduler key normalisation needs a migration of the stored key.
- About 55 P2/P3 items from the audit were not analysed.
- Hybrid titles such as "IT-Administrator / Softwareentwickler" (job 1834) are classified PARTIAL_MATCH because the role names a developer. It is not in the automatic shortlist only because its recommendation is SKIP.

## 5. Full pytest

Final run on the final code HEAD: **0 failed, 8301 passed, 7 skipped** (42 min, SQLite plus PostgreSQL integration at head, code HEAD 3ea3b2f). Ruff check and format: clean.

Phase B (PostgreSQL migration-cycle tests, run separately as in CI): 36 passed, 0 failed, 0 skipped.

Skips (all environmental, Windows): symlink creation not permitted (4 tests in backup, Stage 12 and Stage 21 modules), one test needs a local canonical database that is absent. In CI, Linux runs them.

The first full run on the integration before the final commits had 2 failures, both caused by new files (script registry test and a guard that every non-probe route needs the API key). Both were real, fixed in `7dbdeb9`.

## 6. PostgreSQL

Dedicated `postgres:16` container on a private port, one pytest process, schema reset and `alembic upgrade head` first. The `tests/integration` modules ran in phase A (`TEST_POSTGRES_URL` set) and the migration-cycle modules in phase B. `alembic current` equals the single head.

## 7. Codex

Three rounds, read-only.

- Round 1: CHANGES_REQUIRED. Five findings, all valid and fixed: restore could clobber a target created during the copy, report `--json` could overwrite a database, report assessed before preparing the match, the bot poller dying after start was invisible to readiness, `jobtriage restore` needed the repo as working directory.
- Round 2: CHANGES_REQUIRED. Two valid findings on the poller fix (PTB keeps `updater.running` true when its polling task dies, and `stop()` then refuses). Fixed with a polling-task check and a best-effort teardown.
- Round 3: no findings in the fixes. Verdict stayed CHANGES_REQUIRED only because Codex cannot run pytest in its sandbox; the full suite result above covers that.

Codex limitations: no pytest, no live Gmail/Telegram, no SQLite/PostgreSQL concurrency runs, no restore rehearsal, no Docker build. Those were done here (suite, PostgreSQL, restore tests, image build); concurrency beyond the existing tests and anything live were not.

## 8. Shortlist end to end

Run on a consistent copy of production (`scripts/shortlist_e2e_report.py`, 3 234 vacancies, default thresholds: match score 80, up to 10 per run). The copy was written to; production was not. Thresholds come from defaults, not from the operator's `.env`.

| Measure | Count |
|---|---|
| fit tier NOT_SUITABLE | 2 148 |
| NEEDS_OPERATOR (missing data) | 935 |
| STRETCH | 140 |
| PARTIAL_MATCH | 11 |
| GOOD_MATCH | 0 |
| eligible by status (NEW/SAVED) and recommendation (APPLY/MAYBE) | 241 |
| of these: below the match threshold | 174 |
| of these: blocked by tier (NOT_SUITABLE 136, NEEDS_OPERATOR 89, STRETCH 13; overlaps with the threshold) | 238 |
| of these: need a manual decision (NEEDS_OPERATOR or STRETCH) | 102 |
| eligible without a match score | 0 (matches already existed) |
| enter the automatic shortlist | 2 (job 212 Datenanalyst, job 772 DevOps Engineer; both PARTIAL_MATCH, scores 100 and 87) |
| drafts prepared for them (copy only) | 2 CV + 2 Bewerbung drafts, nothing sent |

Junior developer titles (160 in the table, 14 eligible): 0 enter the shortlist. The two explicit Junior Python/Backend vacancies (705, 752) are held as NEEDS_OPERATOR because the car route is unverified (about 122 km straight line). Others are blocked by a stated German C1/C2 requirement (NOT_SUITABLE), an existing application (195), or missing extracted skills.

Admin/support: 232 titles match an independent pattern (administrator, systemadmin, IT support, helpdesk, 1st/2nd level, systemintegrat...). 153 of those are NOT_SUITABLE; 0 reach the automatic shortlist. NOT_SUITABLE in the shortlist: 0. The script exits non-zero if either ever happens, and a test covers it.

Observation for the operator: the two shortlisted vacancies are adjacent roles (data analyst, DevOps), not Python/backend development; the rules allow them as PARTIAL_MATCH.

## 9. Data preservation

On a fresh consistent copy: `integrity_check` ok, `foreign_key_check` 0 violations, 50 tables with row counts equal to production, 25 applications (8 RECEIVED, 9 REJECTED, 8 SUBMITTED), 54 events with no orphans, no dangling application-to-job links, 1 Telegram Bewerbung approval intact (references a preparation), Gmail tables empty in production (0 messages, 0 threads). The pre-existing 3 234 cache rows are unchanged on the copy. Production file untouched.

Not done: a cache-refill idempotency run on a copy. The Stage 21 writer already has tests for resume and repeat runs, and the full suite includes them, but I did not run it against the production copy.

## 10. Risks of rolling out

- `/ready` now depends on the bot when a token is set. A Telegram outage makes the web container unhealthy (Docker marks it unhealthy; compose does not restart on health alone, but anything watching health will react). Remove the token or accept this.
- The 3 234 cached assessments are stale until the writer is run; the dashboard shows them as stale, not under their old tier.
- Pins in `constraints.txt` were validated on 3.13 only.
- Reading private PTB attribute `_Updater__polling_task`; a PTB upgrade could change it (the code then falls back to the `running` flag).
- No schema change, so rollback is a redeploy of the previous image.

## 11. Merge order, production update, permissions

See `docs/AUDIT_FINAL_RELEASE_PLAN.md`.
