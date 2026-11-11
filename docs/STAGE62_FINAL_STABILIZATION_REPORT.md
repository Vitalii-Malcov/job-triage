# Stage 62 — Final stabilization report

Branch `fix/stage62-final-stabilization` (from `origin/main` `11dae541b5977f59b0fea92e5e735b1ebb76faf5`).
Code change is deliberately small: two confirmed defects fixed, everything else verified or
documented. No new features, no migration, no production write.

## Verification state

| Item | Value |
|---|---|
| GitHub main SHA | `11dae541b5977f59b0fea92e5e735b1ebb76faf5` |
| Production Alembic revision | `a7c3e5f1b9d2` (head, unchanged) |
| Production DB checks | integrity `ok`, foreign_key_check empty (on a copy) |
| Bewerbungen / events / jobs | 25 / 54 / 3 234 (statuses: SUBMITTED 8, RECEIVED 8, REJECTED 9) |
| Production writes by Stage 62 | **none** (all work on copies in `E:\JobTriage-Temp\s62`) |
| Production vs `stage61-final` backup | `applications`, `application_events`, `alembic_version` identical; `jobs` and `job_fit_assessments` differ. The file was last modified 22:03 local (21:03 UTC), before Stage 62 started (first server log 21:11 UTC), so this is not from this stage. Probably a Stage 61 step after the backup; **the operator should confirm** |
| Safety flags (`startup_safety_state`) | outbound, Gmail draft, Telegram (all), scheduler, auto-prepare, Gmail/follow-up cycles, company-research auto: all `false` |
| API key | `api_key_strength=weak` (operator's `.env`, not changed; rotation plan in the runbook §8) |

## Fixes

| Defect | Cause | Reproduction | Fix | Test | Result |
|---|---|---|---|---|---|
| S62-01 gate admitted an internship | `automatic_block_reason` never read `posting_type`/title; BASF "Praktikum" #3203 was gate-open | live gate over a production copy | block `SELF_EMPLOYMENT`/`APPRENTICESHIP` and Praktikum/Werkstudent/thesis titles (`NOT_EMPLOYMENT_POSTING`); trainee programmes stay admissible | `test_stage62_internship_posting_never_opens_the_gate` | gate-open 6 → 5 |
| S62-02 public API schema | FastAPI default `/docs`, `/redoc`, `/openapi.json` | unauthenticated GET | off by default, `API_DOCS_ENABLED` opt-in | `tests/test_stage62_route_auth_matrix.py` (every route needs credentials; docs off by default) | fixed |

Reviewed, not changed: S62-04 (duplicate local drafts need a schema change → separate PR),
S62-05/06 (product/cosmetic). Full table: `STAGE62_ISSUE_INVENTORY.md`.

## Real search (copy of production, live Bundesagentur, read-only GET)

* Dry run: 1 412 postings found, 1 112 already stored, **276 new**, 28 reference conflicts (not merged), 2 004 stored BA rows not returned today (8 with an application; not evidence of closing). Pagination truncated 2 searches (3 951 results not fetched).
* Apply on the copy: 276 created, 47 tables digest-checked, applications/events unchanged, integrity ok. After matching and assessing: 72 NEEDS_OPERATOR, 17 STRETCH, 187 NOT_SUITABLE, **0 GOOD/PARTIAL**.
* Junior Python/Backend among the new: "Quereinsteiger als Junior Python-Entwickler" (#3263) is correctly blocked by duplicate protection (`ALREADY_APPLIED`, same BA reference as application 25). Others are C#/Embedded and wait for review.
* Existing queue: 684 items, 8 Junior Python/Backend (FERCHAU Karlsruhe ×2 about 122 km, 5 XING stubs needing enrichment, one PHP-only title). None is auto-admissible. Truly suitable right now: none without a manual read; the gate-open list is in `STAGE62_GATE_OPEN_CANDIDATES.md` (#551 and #212 the most plausible).

## Security

Auth fail-closed and constant-time; cross-origin login → 403; wrong key → 401; all 72 routes need credentials except health/ready/login/static; cookie `HttpOnly; SameSite=Strict; Path=/ui; Secure`; no CORS; path traversal → 404; no secrets/DB/.env tracked; Docker and launcher bind 127.0.0.1; outbound controls all off. UI review queue: 28 pages, ~1–1.5 s each (cold/warm same by design: freshness hashing).

## CI / tests

* Ruff check and format: pass.
* Targeted (57 modules: auth, security, UI, review, gate, work-time, Stage 20/22/60/16, config, scheduler, outbound, module structure): **1 896 passed, 4 skipped, 0 failed** (before the last gate tweak); after it, the affected modules (221 tests) pass.
* Full suite is 9 024 tests (~6 h locally on this machine), PostgreSQL integration and Docker run in GitHub CI on the PR; results are recorded on the PR, not here.
* Codex (read-only): 4 rounds. Round 1 four P2 (trainee over-blocking, weak matrix, two runbook errors) → all fixed. Round 2 two P2 (title check tied to posting type, matrix still tolerant) → fixed. Round 3: no code finding; its only remark was that its sandbox cannot run pytest. No P0/P1 raised.

## Operations

JobTriage is ready for daily **manual** search. Start: `scripts\run_operator_ui.py --database runtime\job_triage_runtime.db --snapshot E:\JobTriage-Backups`, open `http://127.0.0.1:8000/ui/review`. New vacancies: refresh → match → assess, exact commands in `STAGE62_DAILY_OPERATION_RUNBOOK.md` (production write; run it only with the operator's go-ahead, after a `backup_sqlite.py` backup). Needs the operator's permission: any production write, key rotation, enabling auto-prepare/scheduler/outbound, every Bewerbung submission.

## Remaining blockers / follow-ups

Rotate the weak API key; confirm why production `jobs`/fit-cache changed after the final Stage 61 backup; second off-disk backup; separate PR for a draft-uniqueness constraint; decide on `EARLIER_REQUESTED` start dates and DevOps tiers.
