# Stage 21 — security and correctness fix (2026-10-30)

Branch `stage21/vacancy-cache`, PR #18. The fix is commit `bf62764`, on top
of `76e8dc1`. Not merged and not deployed. No production migration or
backfill was run. Production SQLite and `E:\Projects\job-triage` were not
modified. Nothing was sent: no Gmail, no Telegram, no Bewerbungen.

## Status: RELEASE CANDIDATE READY

| Gate | Result |
|---|---|
| P2-1 false FRESH (A → B → A) | fixed, regression tests SQLite + PostgreSQL (two connections) |
| P2-2 cross-worktree production guard bypass | fixed, regression tests on real git worktrees + real cross-worktree path (classified only, never opened) |
| P3 plan §3.2 | rewritten to match the code |
| Codex review | rounds 5–11 REJECT (each finding fixed + tested), **round 12 VERDICT: ACCEPT** |
| Ruff check / format | clean (481 files) |
| Local PostgreSQL 16 (disposable WSL Docker container) | Stage 21 PG module 10 passed; full CI PG list 132 passed, 0 skipped |
| Local full pytest (SQLite) | final (`bf62764` tree): 6 713 passed / 138 skipped / 0 failed (138 = PostgreSQL modules without `TEST_POSTGRES_URL` + 1 symlink test needing a Windows privilege; both run in CI) |
| GitHub CI, PR #18, runs 41025252937 (pull_request) and 41025245974 (push), commit `bf62764` | `test` 6 711 passed / 140 skipped; `scheduler-postgres` 132 passed, zero skips; `docker` pass |

## P2-1 — key and assessment from one state

Cause: pysqlite sends `BEGIN` only before the first write, and PostgreSQL
READ COMMITTED takes a new snapshot for every statement. So the input key
(state A) and `assess_job` (state B) could disagree. After a later return
to A, B's assessment read FRESH. Reproduced before the fix: the stored row
had B's assessment under A's key. A vacancy deleted mid-batch also crashed
with `LookupError`.

Fix (`scripts/stage21_assess_vacancies.py`, `app/services/fit_cache.py`):
- Each batch runs in one transaction that sees one state. SQLite uses
  `BEGIN IMMEDIATE` before the first read, so no other writer can commit
  until the batch commits. PostgreSQL uses REPEATABLE READ: concurrent
  writes to rows the batch writes become serialization failures.
- `fit_cache.require_consistent_reads` refuses any other session.
- Conflicts (SQLite busy, sqlstate 40001/40P01) roll the batch back and
  retry it on a fresh state, up to 3 attempts. After that the batch is
  reported as `CONFLICT` and the run exits with code 1.
- The compare-and-swap is unchanged. Batches are now 50 (≈1 s write lock).

## P2-2 — production identity independent of the checkout

Fix (`app/core/database_target.py`; `classify_path` stays pure: it only
uses realpath and stat). A file counts as PRODUCTION_RUNTIME if any of
these holds:
- It is `runtime/job_triage_runtime.db` of **any worktree of the
  repository**. Worktrees are found from `.git`, `commondir` and
  `worktrees/*/gitdir`, read as raw bytes. A separate git dir's
  `core.worktree` is read by Git itself, with no hand-written parser.
  Paths are compared after resolving them (relative, `..`, symlinks,
  junctions, case) and by file identity (hard links).
- Its file name is `job_triage_runtime.db`, anywhere outside a backup
  location.

The writer checks `--snapshot` before it opens the file. It verifies the
snapshot (integrity check, tables, Alembic revision) before the first
write.

## Accepted residual risks (docs/STAGE21_VACANCY_CACHE_PLAN.md §11)

A hard link to production under **another name** is not recognised in
these cases:
- it is used from a copy of the code that is not a worktree of this
  repository;
- it is used from a separate-git-dir worktree without `core.worktree`;
- it is used without a working `git` binary.

A SQLite batch blocks other writers for about 1 s.

## Next steps (need explicit approval)

Merge PR #18, then the production migration and backfill, following the
runbook in plan §9.
