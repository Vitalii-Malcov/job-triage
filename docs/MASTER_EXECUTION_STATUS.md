# Master execution status

Living hand-off file. Facts only; every number names its commit. Each stage is
reported on four separate axes: developed / merged into `main` / tested / deployed.
Last update: 2026-11-04 (reconciled against `main@78a947b`; the 2026-10-30 text
described PRs #18-#21 as open and is superseded).

## Ground rules (unchanged)

- Production SQLite (`runtime/job_triage_runtime.db`) is never migrated,
  backfilled or written by development work. Every check runs on a copy.
- No PR is merged, no Bewerbung / Gmail / Telegram is sent, no application
  status is changed without the operator's explicit approval for that step.
- Code and schema move together: the CLIs refuse a database that is not at
  the code's Alembic head (`DATABASE_NOT_AT_MIGRATION_HEAD` / `REFUSED`).

## Where things are

| Item | State | Evidence |
|---|---|---|
| `main` | `78a947b` (merge of PR #29); contains PRs #18-#29 (Stages 21-24 and docs PRs #14, #29) | `git log main` |
| Production DB | last recorded: Alembic `e3b7c9d1f4a6` after the 2026-10-31 rollout (code `d5a7d27`) | operator notes; no deployment record in this repository |
| Production code | `d5a7d27` per the same notes; PRs #23-#29 (T6 refresh CLI, fit-evidence gating, Stage 23/24) deployment NOT verified | |
| Remediation of the hard code review | local branch `fix/hard-review-integration` (tasks 01-17 merged, 18-19 pending), not pushed, not merged, not deployed | `docs/HARD_REVIEW_REMEDIATION_STATUS.md` |

Local-only remediation branches add two Alembic revisions after the Stage 22 head
(`f1a8c3d5e7b9`, `a2b7d4f9c1e3`) and change the fit-cache rules hash (source bytes of the scoring
modules): after a deployment the fit cache must be refilled and the schema migrated in an operator
window. Neither has been done in production (the migrations were only run on temporary SQLite
and a local test PostgreSQL).

## Stages

| Stage | Developed | In `main` | Tested | In production |
|---|---|---|---|---|
| 1-20 | yes | yes | CI at merge time | yes (code `4cdaacf`, DB `b6c1e9a4d2f7`) |
| 21 vacancy assessment cache (PR #18) | yes | yes | CI green at PR time; PostgreSQL job covers `c7e2a9d4f1b3` | reported 2026-10-31 |
| 22 P0-P2 (PRs #19-#22): repost guard, BA reference `d5f1a7c3e9b2`, liveness, geography/remote/tiers `e3b7c9d1f4a6`, event time | yes | yes | local full runs recorded in README; PR #19 CI green | reported 2026-10-31 (head `e3b7c9d1f4a6`) |
| 22 P1-2 vacancy identity | dry-run report only; no merge implemented | n/a | | no |
| 22 T6 refresh CLI, fit-evidence gating (PRs #23, #24) | yes | yes | tests in the PRs | not verified |
| 23 BA search params, domain vocabulary (PRs #25, #26) | yes | yes | tests in the PRs | not verified |
| 24 BA discovery, target alignment (PRs #27, #28) | yes | yes | tests in the PRs | not verified |
| Hard-review remediation, 19 planned tasks | tasks 01-17 accepted and merged locally; 18 (this documentation) under review; 19 (final audit) pending | **no** | local pytest/Ruff/PostgreSQL (Docker) per task; GitHub Actions NOT run | **no** |

The merge plan formerly listed here (#18 -> #19 -> #20 -> P1) was executed: the PRs are merged as
merge commits (`fac3829`, `5c46096`, `84407ff`, `c40bafc`, `d5a7d27`, ...).

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

1. Operator review of `fix/hard-review-integration`; then (operator-approved) push, PR and CI.
2. Verify and record what is actually deployed (PRs #23-#29 are not confirmed in production).
3. Production window for the remediation: backup -> `alembic upgrade head`
   (`e3b7` -> `f1a8c3d5e7b9` -> `a2b7d4f9c1e3`) -> fit-cache refill -> smoke tests.
4. Deferred (own approved stage): follow-up snooze / deadline-aware follow-ups (needs a new event
   type on the append-only `application_events` table).
5. Open docs PRs #1 and #16 still need an operator decision (close or merge).
