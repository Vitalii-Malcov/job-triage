# Master execution status

Living hand-off file: the exact state of the Stage 21/22 work so a later
session can continue safely. Facts only; every number names its commit.
Last update: 2026-10-30.

## Ground rules (unchanged)

- Production SQLite (`runtime/job_triage_runtime.db`) is never migrated,
  backfilled or written by development work. Every check runs on a copy.
- No PR is merged, no Bewerbung / Gmail / Telegram is sent, no application
  status is changed without the operator's explicit approval for that step.
- Code and schema move together: the CLIs refuse a database that is not at
  the code's Alembic head (`DATABASE_NOT_AT_MIGRATION_HEAD` / `REFUSED`).

## Where things are

| Item | State |
|---|---|
| `main` | `4cdaacf` (Stage 20 completion, PR #17) |
| Production DB | Alembic `b6c1e9a4d2f7`, integrity ok, not migrated |
| Production code (working copy) | `main@4cdaacf` |
| PR #18 Stage 21 cache | open, `stage21/vacancy-cache@4a3647c`, CI green, Codex ACCEPT, UI checked on a migrated copy |
| PR #19 Stage 22 P0-3 repost guard | open, stacked on #18, `stage22/repost-guard@79e54e7`, CI green, Codex ACCEPT |
| Stage 22 P0-1/P0-2 | branch `stage22/p0-ba-reference`, stacked on #19 (see below) |

Merge plan for #18/#19 (operator-approved strategy, not yet executed):
merge commit for #18, then retarget #19 to `main` (verified locally: 11
files / 6 commits, no force-push needed), then the same for later stacked PRs.

## Stages

| Stage | Developed | In `main` | In production | Notes |
|---|---|---|---|---|
| 1–20 | yes | yes | yes (code `4cdaacf`, DB `b6c1e9a4d2f7`) | |
| 21 vacancy assessment cache | yes | no (PR #18) | no | migration `c7e2a9d4f1b3` |
| 22 P0-3 repost guard | yes | no (PR #19) | no | no migration |
| 22 P0-1 BA reference | yes | no | no | migration `d5f1a7c3e9b2`, backfill script |
| 22 P0-2 BA liveness | yes | no | no | read-only CLI |
| 22 P1 geography / matcher / remote / identity | no | — | — | planned, see audit plan |
| 22 P2 operations | no | — | — | planned |

## Repository clean-up (2026-10-30)

Deleted (content verified in `main`: branch tree identical to its squash
merge commit, or patch-equivalent by `git cherry`); SHAs kept for restore
(`git push origin <sha>:refs/heads/<name>`):

| Branch | SHA | Evidence |
|---|---|---|
| feat/stage-17-candidate-search-config | 6de8e7b | tree = PR #11 merge b94f0e0 |
| maintenance/night-audit-2026-10-27-pr | 4c693cd | tree = PR #12 merge bdae3cd |
| stage19/operational-validation-pr | 5007266 | tree = PR #13 merge b745aa4 |
| stage20/completion | e49e165 | tree = PR #17 merge 4cdaacf |
| stage20/operator-ui-release | 9f28e34 | tree = PR #15 merge 28a8b5f |
| fix/follow-up-heartbeat-test-stability | e760a34 | patch-equivalent in main |
| fix/stage12-canonical-candidate-skills | a332537 | patch-equivalent in main |
| local fix/ui-long-words | 657f1c4 | both changed files identical in main |

Three clean worktrees of an earlier session's scratchpad were removed
(their branches are on `origin` or merged).

Kept, need an operator decision:

| Item | Why kept |
|---|---|
| PR #1 `chore/agent-workflow-v2`, PR #14 `docs/stage19-release-report`, PR #16 `docs/stage20-release-verification` | open docs PRs, possibly superseded — close or merge? |
| local `local/new-laptop-setup` (2 commits) | one doc differs from `main`; may contain machine-specific notes — never push |
| local `stage20/operator-dashboard-foundation` (11 commits) | 5 files differ from `main` (pre-squash history) — likely obsolete |
| worktrees `job-triage-stage21`, `job-triage-stage22`, `job-triage-p0` | branches of open / stacked PRs |

Secret scan of the whole history (GitHub/AWS/Anthropic/OpenAI/Telegram/Slack
token shapes, private keys): only a test fixture string. Tracked env files are
`.env.example` / `.env.stage10.example` only.

## Next steps (in order)

1. Codex review of P0-1/P0-2, fix to ACCEPT, open the stacked PR.
2. P1 geography (BA coordinates, workplace vs. headquarters, `LOCATION_UNVERIFIED`).
3. P1 matcher (role relevance, German level, experience, education, tiers).
4. P1 remote (no `arbeitszeit=ho`; detail-based home-office flags).
5. P1 vacancy identity: dry-run merge report only.
6. P2 operations; documentation; final QA.
7. Operator approvals: merge #18 → #19 → Stage 22 PRs; then one production
   window (backup, `alembic upgrade head`, source-reference backfill, cache
   backfill, smoke tests).
