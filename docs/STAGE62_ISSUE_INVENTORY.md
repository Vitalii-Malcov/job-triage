# Stage 62 — Issue inventory

Baseline: `origin/main` = `11dae541b5977f59b0fea92e5e735b1ebb76faf5` (Stage 60/61 code). Production
SQLite untouched; every check ran on copies under `E:\JobTriage-Temp\s62`.
Inputs read: Stage 58/59/60/61 reports, `OVERNIGHT_RELEASE_REPORT.md`, `SECURITY_MODEL.md`,
`MANUAL_REVIEW_QUEUE.md`, `DAILY_OPERATIONS.md`. Open PRs: #31 and #33 (docs only, not mine).
No open GitHub issues. Hypotheses that could not be reproduced are not listed as defects.

| ID | Component | Symptom | Reproduction | Priority | Fix | Test | Status |
|---|---|---|---|---|---|---|---|
| S62-01 | Auto-prepare gate | A trainee/internship posting (#3203 BASF Praktikum, `posting_type=TRAINEE_OR_INTERNSHIP`) passes the gate (gate-open list had 6, one invalid) | Live `automatic_preparation_check` over all jobs of a production copy | P1 | `automatic_block_reason` returns `NOT_EMPLOYMENT_POSTING` for any posting type other than `EMPLOYMENT`/`UNKNOWN` | `test_stage62_internship_posting_never_opens_the_gate` | **FIXED** |
| S62-02 | API surface | `/docs`, `/redoc`, `/openapi.json` answer without a key (SEC-05) | Unauthenticated GET on a test instance | P3 | Off by default; `API_DOCS_ENABLED=true` opts in | `tests/test_stage62_route_auth_matrix.py` (2 tests: every route needs credentials except an allowlist; docs off by default) | **FIXED** |
| S62-03 | Config (`.env`) | Startup diagnostic `api_key_strength=weak`, `APP_ENV=staging` | Stage 61 log | P2 | Operator-owned value; **not changed**. Rotation procedure in `STAGE62_DAILY_OPERATION_RUNBOOK.md` §8 | — | **OPEN — operator action** |
| S62-04 | Drafts | `bewerbung_drafts` has no uniqueness constraint; concurrent manual preparation can leave two local drafts (Stage 55 note) | Reading `bewerbung_repository.create_bewerbung_draft` (always inserts; documented "intentional regeneration"); production copy has 2 drafts, 0 duplicates; `applications` has 0 duplicates per job | P3 | Not fixed: a constraint changes manual-regeneration semantics and needs a migration → separate PR. A draft is local only, never sent | — | **DEFERRED (documented)** |
| S62-05 | Gate / data | `start_date=EARLIER_REQUESTED` (#772) and DevOps titles (#52, #772) pass the gate with PARTIAL tier | Gate-open review | P3 | Product decision; not changed without a larger false-positive set | — | OPEN (operator decision) |
| S62-06 | Review UI | XING rows show "FULL_TIME" in the hours column from the text heuristic although full time is not confirmed; PHP-only title (#328) sits in the "Junior Python/Backend" filter | `/ui/review?profession=junior` | P3 | Not changed (listed for manual review only, never auto-prepared) | — | OPEN (cosmetic) |
| S62-07 | Process | Fit assessment before matching scores a new vacancy as `skills NOT_EVALUATED` | Ran assess before match on discovery copy | P3 | Runbook states the order: refresh → match → assess | — | DOCUMENTED |
| S62-08 | Data | 28 BA reference conflicts; 8 application-linked BA vacancies not returned by BA; 2 157 BA rows without a Vollzeit flag | Stage 22 dry run (28 conflicts reproduced today) | P2 | Never auto-resolved (protected) | — | OPEN (manual) |
| S62-09 | Backup | Second off-disk backup missing | Stage 61 | P2 | Needs another drive | — | OPEN (operator) |

## Verified OK (no defect found)

API key comparison is constant-time and fail-closed; cross-origin login POST → 403, wrong key → 401;
session cookie `HttpOnly; SameSite=Strict; Path=/ui`, `Secure` by default (documented: Chrome/Edge
accept it on 127.0.0.1); unauthenticated `/ui/*` → 303, `/api/*` → 401; path traversal → 404; no CORS
headers; no secrets, `.env`, DB or key material tracked in git; Docker publishes
`127.0.0.1` only and the UI launcher binds `127.0.0.1`; scheduler, auto-prepare, outbound, Telegram
flags all false in `startup_safety_state`; 684 review items unique across 28 pages (~1–1.5 s/page).
