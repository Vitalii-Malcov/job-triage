# Stage 60 — Positive full-time evidence for auto-prepare

Status line is at the bottom (updated after CI and merge).

## 1. Root cause
`shortlist_gate.automatic_block_reason` admitted a vacancy when the `work_time` criterion was
`FULL_TIME`. That value is produced by `classify_work_time`, a regex heuristic over free text. Any
wording the heuristic misread ("Teilzeit, später Vollzeit möglich" and its many variants) became
permission for automatic preparation. Stage 59 spent 27 Codex rounds extending the regex; the
problem is architectural: a heuristic was used as a trust decision.

## 2. Old vs new architecture
Old: `text -> classify_work_time -> work_time=FULL_TIME -> gate opens`.
New: `work_time` stays a classification that helps the human. A separate criterion
`work_time_evidence` (`app/services/work_time_evidence.py`) is CONFIRMED only on positive,
uncontradicted evidence, and the gate requires BOTH (`FULL_TIME_NOT_CONFIRMED` otherwise).
A missing criterion (old cache row) is treated as not confirmed.

## 3. Sources of positive evidence
1. Bundesagentur `arbeitszeitVollzeit=true` (`jobs.full_time_offered`, written only by the BA
   collector; job source must be `bundesagentur`).
2. A `Arbeitszeit` / `Beschäftigungsart` / `Anstellungsart` field line with a plain
   Vollzeit/Teilzeit value, located in the description's LEADING metadata block (consecutive
   `Key: value` lines from the start; a field line inside prose is not evidence).
Not evidence: prose "Vollzeit", future/deferred/staff Vollzeit, UNKNOWN, CONFLICT.

## 4. Conflict policy
Contradictions always win over a positive source: flag `False`; classifier PART_TIME; explicit
denial/reduction of full time; any stated weekly hours < 35; any share of working time other than
100 % (next to Stelle/Teilzeit/…); labelled scope (Beschäftigungsgrad, Stellenumfang, …) that is
not exactly `100 %` on its line; Minijob/Werkstudent/Halbtag. Over-blocking is intentional.

## 5. Gate changes
`automatic_block_reason`: after `work_time == FULL_TIME`, `work_time_evidence_confirmed` is
required. All previous checks (tier, hybrid-admin, duplicate CLEAR, …) unchanged. The gate runs on
a live isolated assessment, never the fit cache. `review_queue` keeps held PRIMARY vacancies and
ADJACENT vacancies that read as full time but lack evidence. Manual preparation is untouched.

## 6. Regression scenarios
`tests/test_stage60_work_time_evidence.py` (55 cases): BA flag True/False/absent; field Vollzeit /
Teilzeit; "Teilzeit, später Vollzeit möglich"; "Vollzeit perspektivisch möglich"; "Vollzeit ab 2027,
aktuell Teilzeit"; staff Vollzeit; Teilzeitmöglichkeiten (blocked without flag); text vs flag
contradiction; old cache row; forged criterion; forged field line in prose; reduced hours;
decimal/zero/range shares; labelled scope. Existing fixtures in 6 test files now model confirmed
evidence explicitly.

## 7. Real data (copy of production, migrated to a7c3e5f1b9d2; production untouched)
| Metric | Before | After |
|---|---:|---:|
| Jobs | 3234 | 3234 |
| FULL_TIME / PART_TIME / UNKNOWN | 677 / 144 / 2413 | unchanged |
| Confirmed evidence | n/a | 15 (field lines) |
| Auto-prepare gate open | 1 (#2005) | 0 |
| Manual Review Queue | 684 | 685 |
| Junior Python/Backend | 8 | 8 |
| Applications / events | 25 / 54 | 25 / 54 |

#2005 (Bundesagentur, PARTIAL_MATCH, classifier FULL_TIME) now `FULL_TIME_NOT_CONFIRMED` and
sits in the review queue. Nothing sent. Note: production has no `full_time_offered` values yet
(column added by `a7c3e5f1b9d2`), so zero auto-eligible vacancies follows from the data until a
BA refresh fills the flag.
Not re-run on the copy: #551 / #1261 queue presence (Stage 58 items) and a cache refill; see §14.

## 8. Cache
The fit-cache rules hash covers the import closure of the assessment modules, so the changed code
invalidates cached assessments. The gate never reads the cache. No new migration or column.

## 9. SQLite / PostgreSQL
No schema change. PostgreSQL integration is covered by the CI `scheduler-postgres` job.

## 10. Codex verdict
7 review rounds on the trust boundary. R1 P2: forged field in prose; adjacent held roles vanished
from queue. R2–R6 P2: reduced-hours / share bypasses. Round 7: ACCEPT, no P0/P1/P2. Residual
(accepted by design, Codex agreed): unlabelled prose with unusual reduced-hours wording can still
yield CONFIRMED when the BA flag is true; CONFIRMED only admits drafting, send needs approval.

## 11. CI / tests
- Ruff check and format check: clean.
- Full local pytest on f66e785: 8853 passed, 168 skipped, 0 failed.
- GitHub CI on f66e785: test (Linux), scheduler-postgres, docker all pass.
- Targeted: tests/test_stage60_work_time_evidence.py 55 passed (work-time, gate, review queue,
  shortlist safety, Stage 17 assessment suites in the full run).
CI of the final documentation-only HEAD is checked separately before merge.

## 12. PR merge status
See the final status line.

## 13. Production safety
Production SQLite, `.env`, scheduler, auto-prepare and outbound sending were not touched. All data
work ran on a backup-API copy in the session scratchpad.

## 14. Remaining risks
- Unlabelled prose reduced hours (above). Employer-written leading `Key: value` lines count as the
  posting's own statement.
- BA flag is empty in production until a refresh; #551/#1261 and cache refill not re-validated on
  the copy this stage.

## 15. Controlled rollout
1. Deploy; run the alembic upgrade already planned (`a7c3e5f1b9d2`) with a fresh backup.
2. Refresh BA jobs to populate `full_time_offered`; review the gate-open list by hand.
3. Enable auto-prepare for a small cap only after operator sign-off; approval stays manual.

