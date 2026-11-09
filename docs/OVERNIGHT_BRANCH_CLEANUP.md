# Overnight branch cleanup

Status: **SUCCESS**. Final `main`: `3ebc40ae69975b165c25058bcb88cf902cc8b265`; main CI: PASS (run 42714027655).

## Counts

| | Before | After |
|---|---|---|
| Local branches | 57 | 9 |
| Remote branches (excl. HEAD) | 53 | 7 |
| Distinct branch names | 57 | 9 |
| Worktrees | 29 (3 prunable/missing) | 1 (`main`) |

Deleted: **48 local** (`git branch -d`, never `-D`), **46 remote** (`git push origin --delete`, one explicit
branch each, no force, no pattern deletion). 26 clean worktrees removed without `--force`; 3 stale scratch worktrees pruned.

## Rule applied

A branch was deleted only if, at the moment of deletion, `git rev-list origin/main..branch` = 0 and
`git cherry origin/main branch` showed 0 patch-unique commits (so a PR's closed state was never used as proof),
it was not checked out in a worktree (all were clean), and it was not in the keep list.
All PR #32–46 audit branches and review-01…26 branches had 0 unique commits: included in main.

## Kept (9) and why

| Branch | Reason |
|---|---|
| main | default |
| backup/pre-clean-integration (local only) | KEEP — UNMERGED WORK: 11 commits (2 patch-unique) not in main; recovery branch |
| docs/post-pr30-stabilization-report | KEEP — UNMERGED WORK: 1 commit; open PR #33 |
| docs/pr30-production-rollout-report | KEEP — UNMERGED WORK: 1 commit; open PR #31 |
| feat/shortlist-fit-safety | open PR #34 (content already in main; PR is stale — close it to allow deletion) |
| fix/audit-auth-hardening | open PR #37 (content in main; stale PR) |
| fix/audit-flow-reliability | open PR #38 (content in main; stale PR) |
| integration/audit-2026-11-06-release | release branch (PR #47 head); content in main; kept per "release branch" rule |
| integration/audit-2026-11-06 (local only) | 0 unique commits but `git branch -d` refused it; left alone (safe default) |

Deleting the heads of open PRs #31/#33/#34/#37/#38 would auto-close them, which was not authorised. Suggested
follow-up for the operator: close #34/#37/#38 as superseded by #47, then the three branches can be removed.

## Deleted branches

Full list, `L` = local, `R` = remote: see the recorded list below.

- L docs/audit-2026-11-06-remediation
- R docs/audit-2026-11-06-remediation
- L feat/manual-review-queue
- R feat/manual-review-queue
- L feat/nationwide-remote-discovery
- R feat/nationwide-remote-discovery
- L fix/application-idempotency
- R fix/application-idempotency
- L fix/audit-ba-identity
- R fix/audit-ba-identity
- L fix/audit-backup-tool
- R fix/audit-backup-tool
- L fix/audit-correlation-perf
- R fix/audit-correlation-perf
- L fix/audit-dep-constraints
- R fix/audit-dep-constraints
- L fix/audit-input-bounds
- R fix/audit-input-bounds
- L fix/audit-ops-ci
- R fix/audit-ops-ci
- L fix/audit-sec01-html-linear
- R fix/audit-sec01-html-linear
- L fix/audit-status-models
- R fix/audit-status-models
- L fix/audit-telegram-start-retry
- R fix/audit-telegram-start-retry
- L fix/hard-review-integration
- R fix/hard-review-integration
- L fix/job-matching-shortlist-quality
- R fix/job-matching-shortlist-quality
- L fix/junior-scoring-enrichment
- R fix/junior-scoring-enrichment
- L fix/post-pr30-role-family-inflections
- R fix/post-pr30-role-family-inflections
- L fix/remote-fulltime-filter
- L fix/review-01-auth
- R fix/review-01-auth
- L fix/review-02-secrets
- R fix/review-02-secrets
- L fix/review-03-recipient
- R fix/review-03-recipient
- L fix/review-04-sessions
- R fix/review-04-sessions
- L fix/review-05-email
- R fix/review-05-email
- L fix/review-06-xing
- R fix/review-06-xing
- L fix/review-07-collectors
- R fix/review-07-collectors
- L fix/review-08-async
- R fix/review-08-async
- L fix/review-09-transactions
- R fix/review-09-transactions
- L fix/review-10-modules
- R fix/review-10-modules
- L fix/review-11-target-role
- R fix/review-11-target-role
- L fix/review-12-naming
- R fix/review-12-naming
- L fix/review-13-scripts
- R fix/review-13-scripts
- L fix/review-14-rate-limiter
- R fix/review-14-rate-limiter
- L fix/review-15-comments
- R fix/review-15-comments
- L fix/review-16-ci
- R fix/review-16-ci
- L fix/review-17-tests
- R fix/review-17-tests
- L fix/review-18-docs
- R fix/review-18-docs
- L fix/review-19-final-audit
- R fix/review-19-final-audit
- L fix/review-20-xing-identity
- R fix/review-20-xing-identity
- L fix/review-21-gmail-sender
- R fix/review-21-gmail-sender
- L fix/review-22-collector-locking
- R fix/review-22-collector-locking
- L fix/review-23-shared-state
- R fix/review-23-shared-state
- L fix/review-24-residual-debt
- R fix/review-24-residual-debt
- L fix/review-25-green-suite
- R fix/review-25-green-suite
- L fix/review-26-final-audit
- R fix/review-26-final-audit
- L fix/work-time-classification
- R fix/work-time-classification
- L fix/work-time-gate-confirmation
- R fix/work-time-gate-confirmation
- L integration/audit-2026-11-06-final
- L perf/manual-review-queue
- R perf/manual-review-queue

## Verification

- `git fetch --all --prune`, `git worktree list` (only main), `git fsck --no-reflogs`: no errors (only dangling/unreachable objects from old history; no gc run).
- `git status`: only the operator's pre-existing modified/untracked files (CLAUDE.md, postman/, .postman/, review docs).
- No unique commits lost: every deleted branch had 0 commits outside main; unmerged work is retained in the kept branches.
- Production SQLite: **UNCHANGED** (SHA-256 prefix `aef7383ef194fd6a` before and after).
