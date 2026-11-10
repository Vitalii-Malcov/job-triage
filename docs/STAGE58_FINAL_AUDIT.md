# Stage 58 — final audit

Branch `fix/post-release-quality` on `main` @ `7c6c61d`. Not merged (merge is the operator's decision).
Related: `docs/REMAINING_WORK_AUDIT.md`, `docs/E2E_JOB_DISCOVERY_AUDIT.md`.

## 1. What was unfinished
The four P2 findings of the release-candidate review (work-time classification x3, held vacancies missing
from `/ui/review`), the idempotency/alias residuals, five old PRs, and a performance baseline. Full table:
`REMAINING_WORK_AUDIT.md`.

## 2. Confirmed defects (all reproduced before fixing)
* `Vollzeitnah (30 Stunden)` → FULL_TIME.
* `Nur Teilzeit. Vollzeit eventuell später möglich.` → FULL_TIME.
* Source flag `full_time_offered=true` overrode `Keine Vollzeit möglich` → FULL_TIME.
* Two PRIMARY developer vacancies (#551, #1261 on the production copy) held by `WORK_TIME_NOT_CONFIRMED`
  were in no list.
No P0/P1 defect was found.

## 3. Fixed (4 code commits + 2 doc commits)
* `job_fit_signals.py`: `vollzeitnah` is not full time; an explicit "nur Teilzeit" beats a plain or combined
  full-time remark; new `full_time_denied()`.
* `job_fit_assessment.py`: flag `true` + denied/reduced full time → CONFLICT (not FULL_TIME).
* `review_queue.py`: PRIMARY GOOD/PARTIAL vacancies the gate holds for non-FULL_TIME hours enter the queue
  with reason `WORK_TIME_NOT_CONFIRMED` (read-only, no duplicates, still never clears the gate).
* 8 new regression tests. Before/after on 3,234 jobs: 3 change work time, all stricter; tiers and gate unchanged.

## 4. Did not reproduce / not changed
* Source aliases `ba`/`bundesagentur`: only `bundesagentur`, `xing`, `stepstone` exist in code and data.
* Two-draft race: see §9.

## 5. Needs the operator
Run the production runbook (migration, cache refill); decide on PR #31/#33 (docs-only reports); branch
protection on `main`; whether to keep `integration/audit-2026-11-06*` branches; read the vacancies in
`E2E_JOB_DISCOVERY_AUDIT.md` yourself — none is cleared for automatic preparation.

## 6. E2E discovery
Live nationwide dry run on a copy: 744 processed, 127 new, 265 updates, 0 failures, 0 automatic shortlist.
Local Hessen collector, XING and Stepstone were **not** re-run live. Details in the E2E report. XING: all 118
vacancies are `NEEDS_ENRICHMENT` (descriptions < 200 chars), none scores ≥ 60, none is GOOD/PARTIAL; tracking
links are never followed.

## 7. Manual review queue (production copy, cache warm)
682 → 684 items (the two missing developer vacancies). Membership comes from one function used by UI and CLI;
the page stays read-only (existing read-only-engine tests pass). Sorting/cursor/filter/cold-cache tests in
`test_manual_review_queue.py` pass unchanged.

## 8. Work-time classification
Fixed as in §3. Known limits (accepted, safe direction = held for a human): "Vollzeit (nicht Vollzeitnah)"
and similar contrived negations can become CONFLICT with a true flag; "Vollzeit. Nur Teilzeitoptionen …"
reads as PART_TIME. Open, unsafe direction (P3): mixes such as `Teilzeit, später Vollzeit möglich` still read
FULL_TIME. **Do not enable auto-prepare before that is revisited.**

## 9. Idempotency
No code change. `bewerbung_drafts` has no unique identity on purpose (each regeneration is a new version);
automatic cycles are lease-protected, so the residual race is an automatic cycle plus a manual API call,
producing at most two identical local drafts — nothing sent, no application. A guard would need a migration
and would change the meaning of manual regeneration. Revisit if applications are ever created automatically
from drafts. Concurrent application creation already yields exactly one row (existing tests, SQLite + PG CI).

## 10. Performance (copy of production data, local Windows, SQLite)
| Operation | Before | After |
|---|---:|---:|
| Review first page, warm cache | not measured on `main` | ~0.5 s, 37 SQL |
| Review first page, cold cache | not measured on `main` | ~1.6 s, 1,263 SQL (bounded by stale budget) |
| Full queue (page_size 500) | N/A | ~0.46 s warm |
| Assess 1,000 jobs | N/A | ~31 s (32 jobs/s) |
| Cache refresh, 3,234 jobs | N/A | 102 s |
| Parallel requests | N/A (not measured) | N/A |
The Stage 58 changes add no query; the one added branch is a status comparison.

## 11. SQLite / PostgreSQL
Alembic `d6e9a2b4c8f1 → a7c3e5f1b9d2` rehearsed on a copy of the production snapshot: OK. PostgreSQL integration
runs only in CI (this machine has no PG): see CI below.

## 12. Codex
Round 1 on the Stage 58 diff: REJECT, 3 P2 (variations). Round 2 (after fixing two): REJECT, 1 P2 (my
option-compound exclusion would have turned "nur Teilzeitmodelle" into UNKNOWN — a real regression, reverted).
Round 3: **ACCEPT** for the final commit and for "nothing opens the gate for UNKNOWN/reduced hours". The third
round-1 variation was accepted as a safe-direction limit (§8). That ACCEPT covers the Stage 58 delta only, not
the whole repository.

## 13. CI / tests
* Local, before the last two classifier commits: Ruff clean; full pytest **8,678 passed, 169 skipped, 0 failed**
  (the earlier run had 8,672 / 168; +6 tests, +1 skip, not investigated further — all skips are PG-gated).
* After the last commits: 160 targeted + 400 related tests passed locally.
* GitHub CI on the final head: see the PR (recorded below).

## 14. Pull requests
PR for `fix/post-release-quality` (not merged). Closed as superseded: #38 (and #34, #37, which GitHub had
auto-closed when their base branches were deleted in the branch cleanup — my oversight; content is in main).

## 15. Production
Not touched: no write, no migration, no cache refill, `.env` unchanged, SHA-256 prefix of the runtime DB
`aef7383ef194fd6a` before and after; 25 applications / 54 events (read from the snapshot copy). Automation,
scheduler, outbound and auto-prepare flags stay false. All live and write tests ran on copies in the scratchpad.

## 16. Remaining, by priority
P2/P3: the unsafe-direction classifier mix (§8) before auto-prepare; operator runbook; PR #31/#33; main
protection. Everything else in `REMAINING_WORK_AUDIT.md` is documented, none blocks.

## Totals
* Critical bugs found: 0 (P0/P1) — 4 P2 defects confirmed
* Critical bugs fixed: n/a — all 4 confirmed P2 fixed
* P2 fixed: 4 of 4 (+1 regression of my own caught by Codex and reverted)
* Tests passed: 8,678 full local run (+ targeted re-runs after the last commits); CI on final head below
* PRs created: 1 (not merged)
* Production writes: 0
* Real Bewerbungen sent: 0
* Remaining blockers: none for merge; blocker for enabling auto-prepare: §8 mix
