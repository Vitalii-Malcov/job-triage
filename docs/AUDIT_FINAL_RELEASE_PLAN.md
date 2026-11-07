# Release plan: audit 2026-11-06 integration

Nothing below has been run. Every step marked **needs approval** touches `main`, production data or a real service.

## Merge order

The integration PR contains everything. Preferred, **needs approval**:

1. Merge the single integration PR (`integration/audit-2026-11-06-release` into `main`) with a merge commit.
2. Close PRs #32, #34 to #46 as superseded (their commits are already in the merged history). Do not merge them separately afterwards.

Alternative if per-PR history is wanted: merge in dependency order #32, #34, #35, #36, #37, #38, #39, #40, #41, #42, #43, #44, #45, #46, then the integration PR (it will then add only the ten new commits). #34 is based on #32, #38 on #34, #37 on #36.

No Alembic revision is added: the head stays `d6e9a2b4c8f1`, which production already has. No production migration is needed.

## Production update

All needs approval.

1. Stop the web process and the scheduler.
2. Back up: `python -m scripts.backup_sqlite --db runtime\job_triage_runtime.db --dest <folder outside the repo> --label pre-release`. Check the `OK` line and the `.sha256` file.
3. Pull `main`, rebuild (`docker compose build`, or reinstall with `pip install -c constraints.txt -e ".[postgres]"`).
4. Check `alembic current` equals `d6e9a2b4c8f1 (head)`. Do not run a migration.
5. Start. Check `/api/v1/health`, then `/api/v1/ready` (200), then `/api/v1/health/components` with the API key: database `ok`, telegram `running` (or `disabled` without a token), scheduler `ok` or `disabled`, collector_leases `ok`.
6. Optional, separate approval: fill the fit cache with the Stage 21 writer on a snapshot (`--snapshot`), then look at the dashboard.
7. Keep the pre-release backup until a few days of operation look normal.

Rollback: stop, redeploy the previous image or commit; the schema did not change, so the database stays. If data was damaged, restore the pre-release backup with `python -m scripts.restore_sqlite --backup ... --target ... --replace --allow-production` (keeps the current file as `.pre-restore-*`).

## Needs your approval or decision

- Merging to `main`, closing the other PRs.
- Any production deployment step above, including the writer for the fit cache.
- Registering the regular backup task (`docs/BACKUP_AND_ROLLBACK.md` has the command; nothing is registered).
- Whether a Telegram outage should make `/ready` fail (current behaviour when a token is set).
- Policy decisions: FLOW-01 (stored announced flag), FLOW-02 reverse direction, FLOW-04 Reply-To, ARC-03 key normalisation, DB-01 merge of five pairs and the unique index (job 169 has an application).
- Whether PARTIAL_MATCH data-analyst and DevOps titles should be allowed into the automatic shortlist.
- Running any real Gmail or Telegram send, or enabling automation: not part of this release.
