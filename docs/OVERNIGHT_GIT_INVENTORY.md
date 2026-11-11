# Overnight git inventory (2026-11-11 → 12)

Start state: main `b60491f` (PR #60). PR #61 merged → main `5834fe7`. Before cleanup: 15 local branches and 13 remote branches (excluding main).
PR states: 61 PRs total; open before: #31, #33, #61; closed-not-merged: #1, #16, #34, #37, #38; rest merged.
Branch protection on main: **none** (API 404) — merges therefore used `--match-head-commit`, no `--admin`.
Stash: empty. Untracked user files in the main worktree were not touched.

| Branch | Last commit | PR | Merged | Unique commits vs main | Uncommitted | Worktree | Safe to delete | Action |
|---|---|---|---|---|---|---|---|---|
| fix/stage65-telegram-opt-in | 03dc21d | #61 | yes (5834fe7) | 0 | none | job-triage-stage65 (clean, only caches) | yes | local+remote deleted, worktree removed |
| fix/stage64-discovery-reliability | 62d9e4f | #60 | yes | 0 | none | stage64 | yes | deleted (local+remote), worktree removed |
| docs/stage63-final-integration | 7f86bbc | #59 | yes | 0 | none | stage63 | yes | deleted, worktree removed |
| fix/stage62-final-stabilization | 8951580 | #58 | yes | 0 | none | stage62 | yes | deleted, worktree removed |
| fix/post-release-quality | aeb562c | #57 | yes | 0 | — | — | yes | deleted |
| docs/overnight-release-report | bb9624c | #56 | yes | 0 | — | — | yes | deleted |
| integration/audit-2026-11-06-release | fcc13bf | #47 | yes | 0 | — | — | yes | deleted (local+remote) |
| integration/audit-2026-11-06 | c963d04 | — | content in main (0 unique) | 0 | — | — | yes | local deleted |
| feat/shortlist-fit-safety | 900cb1c | #34 | closed, **content in main** (0 unique commits) | 0 | — | — | yes | deleted (local+remote) |
| fix/audit-flow-reliability | a2e7ad2 | #38 | closed, content in main | 0 | — | — | yes | deleted (local+remote) |
| fix/audit-auth-hardening | 9aacc9f | #37 | closed, content in main | 0 | — | — | yes | deleted (local+remote) |
| docs/pr30-production-rollout-report | 4ddb211 | #31 open | no | 1 (docs) | — | — | **no** | kept (open PR) |
| docs/post-pr30-stabilization-report | 8cb3378 | #33 open | no | 1 (docs) | — | — | **no** | kept (open PR) |
| docs/stage60-final-status | 43cfa91 | none | no | 1 (docs) | user's untracked files in this worktree | main worktree (current checkout) | **no** | kept; needs operator decision/PR |
| backup/pre-clean-integration | d5ae850 | — | no (2 patch-unique commits) | 11 | — | — | **no** | kept: local-only backup of pre-clean history |
| main | 5834fe7 | | | | | main worktree | — | local main fast-forwarded |

Worktrees: removed stage62–65. Remaining: main worktree (E:\Projects\job-triage, on `docs/stage60-final-status`, dirty with the operator's untracked files),
`C:\Users\V\…\scratchpad\before` (detached ef28fe8, clean; belongs to an earlier Claude session scratch dir, left alone), and `E:\Projects\job-triage-overnight` (this night's branch).
Totals: local 15 → 4 kept + 11 deleted (plus main); remote 13 → 3 kept + 10 deleted (plus main).
