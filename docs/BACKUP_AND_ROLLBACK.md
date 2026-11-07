# Backup and rollback (SQLite)

Covers audit OPS-03. Nothing here is scheduled or run automatically; the commands are for the operator.

## Make a backup

```
python -m scripts.backup_sqlite --db runtime\job_triage_runtime.db --dest D:\JobTriage-Backups --label pre-deploy
```

The source is read-only, the copy is verified (`integrity_check`, key row counts) and gets a
`.sha256` sidecar. The destination must be outside the repository. Existing files are never
overwritten. `--keep N` is optional and prunes only this tool's own verified files; leave it out
to keep everything.

## Regular backup (operator decision)

`scripts/scheduled_backup.ps1` wraps one backup run for a Windows scheduled task. It is not
registered by this repository. To enable a daily run, the operator can execute once:

```
$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -File E:\Projects\job-triage\scripts\scheduled_backup.ps1 -Db E:\Projects\job-triage\runtime\job_triage_runtime.db -Dest D:\JobTriage-Backups"
$trigger = New-ScheduledTaskTrigger -Daily -At 03:30
Register-ScheduledTask -TaskName "JobTriage backup" -Action $action -Trigger $trigger
```

Check the first run's `OK` line and the sidecar before relying on it. Remove with
`Unregister-ScheduledTask -TaskName "JobTriage backup"`.

## Restore

Stop the web and scheduler processes first, then rehearse on a scratch path:

```
python -m scripts.restore_sqlite --backup D:\JobTriage-Backups\job_triage-<stamp>.db --target D:\scratch\restored.db
```

The backup's name, sidecar digest and `integrity_check` are verified; the result is re-checked
against the backup's row counts. To put it in place of a database use `--replace`: the current
file is copied to `<name>.pre-restore-<stamp>` first and nothing is deleted. The production
database additionally needs `--allow-production`. A target with a `-wal`/`-shm`/`-journal` file
is refused as still in use.

## Rollback of a release

1. Stop web/scheduler. 2. Back up the current database (above). 3. Check out the previous
image or tag. 4. If a migration ran, restore the pre-deploy backup taken in step 2 of the
deployment plan (`docs/AUDIT_FINAL_RELEASE_PLAN.md`) with `--replace`; migrations are not
downgraded on production. 5. Start, then check `/api/v1/ready` and `/api/v1/health/components`.
