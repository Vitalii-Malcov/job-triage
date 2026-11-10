# Remaining work audit (Stage 58)

Base: `main` @ `7c6c61d`. Sources read: the release/idempotency/queue/junior/nationwide/Stage-54 documents,
the overnight release report, `git grep` for TODO/FIXME (none in `app/`, `scripts/`), open PRs/issues
(no GitHub issues are open), the Codex review of the release candidate, and the skipped tests
(every skip is a PostgreSQL integration test that runs in CI; none was disabled by hand).

Legend — type: D defect, F unfinished feature, T technical limit, DA data, TS testing, DOC docs, OP operator decision.

| ID | Problem | Source | Type | Impact | Priority | Resolution |
|---|---|---|---|---|---|---|
| R1 | `Vollzeitnah (30 Std.)` counted as FULL_TIME | Codex P2 | D | auto-gate could admit reduced hours | P2 | **Fixed** (this branch), regression tests |
| R2 | `Nur Teilzeit. Vollzeit eventuell später möglich.` → FULL_TIME | Codex P2 | D | same | P2 | **Fixed** |
| R3 | Source flag `full_time_offered=true` overrode text "Keine Vollzeit möglich" | Codex P2 | D | false FULL_TIME | P2 | **Fixed** → CONFLICT |
| R4 | PRIMARY vacancies held by `WORK_TIME_NOT_CONFIRMED` were in no list | Codex P2 | D | suitable vacancy invisible (2 of 3,234 on the production copy) | P2 | **Fixed**, queue 682 → 684 |
| R5 | Free-form mixes like `Teilzeit, später Vollzeit möglich` still classify FULL_TIME | Stage 58 repro | T | auto-gate still open if auto-prepare is ever ON | P3 | Open, documented; regex is not extended further on purpose. Revisit before enabling auto-prepare |
| R6 | Two workers can leave two identical local letter drafts | Idempotency audit §5 | T | local drafts only, nothing sent; automation cycles are lease-protected, so only auto-cycle + manual API call | P3 | Not changed (see STAGE58_FINAL_AUDIT §Idempotency) |
| R7 | Source aliases (`ba` vs `bundesagentur`) | Idempotency audit §2a | DA | Did not reproduce: only `bundesagentur`, `xing`, `stepstone` exist in code and data | — | No change; `canonical_source` (strip + casefold) covers case/space |
| R8 | 118 XING vacancies have no description | Data | DA | All `NEEDS_ENRICHMENT`; none scores ≥ 60, none is GOOD/PARTIAL | P3 | Correct behaviour; operator opens them manually. Tracking links are never followed (policy) |
| R9 | Cold fit cache makes `/ui/review` slower (1.6 s, 1,263 queries vs 0.5 s, 37) | Measured | T | bounded by the stale budget | P3 | Run `stage21_assess_vacancies.py --apply` after migration (runbook) |
| R10 | Production DB still at Alembic `d6e9a2b4c8f1` | Operator state | OP | code expects `a7c3e5f1b9d2` | — | Operator runs the runbook; rehearsed on a copy in this stage (upgrade OK) |
| R11 | Open PR #31 / #33 (docs-only reports, 1 unique commit each) | GitHub | OP | historical reports | P3 | Left open; operator decides merge/close |
| R12 | PRs #34/#37 auto-closed by GitHub when their base branches were deleted in the branch cleanup; #38 closed here | GitHub | DOC | none — all three tips are ancestors of main (#47 merge `8cf1efa`) | — | Done; disclosed |
| R13 | Codex never gave a formal ACCEPT for PRs #52/#53 | Reports | OP | process | P3 | See Codex verdict in the final audit |
| R14 | `main` has no branch protection / required reviews | GitHub API | OP | process | P3 | Operator decision |
| R15 | Auto-prepare / scheduler / outbound flags are OFF | `.env` | — | deliberate | — | Not a defect |

Not a defect: `NATIONWIDE_REMOTE_DISCOVERY_ENABLED=false` (deliberately off), skipped PostgreSQL tests without a DB.
