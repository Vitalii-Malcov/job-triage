# One verified SQLite backup, for a Windows scheduled task (audit OPS-03).
# This script only runs a backup. It does NOT register itself anywhere: see docs/BACKUP_AND_ROLLBACK.md.
#
#   powershell -File scripts\scheduled_backup.ps1 -Db E:\Projects\job-triage\runtime\job_triage_runtime.db `
#       -Dest D:\JobTriage-Backups [-Keep 30]
#
# -Keep 0 (default) never deletes anything. A value > 0 prunes only files this tool created,
# oldest first, beyond the newest N.
param(
    [Parameter(Mandatory = $true)][string]$Db,
    [Parameter(Mandatory = $true)][string]$Dest,
    [int]$Keep = 0,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $venv = Join-Path $repo ".venv\Scripts\python.exe"
    $Python = if (Test-Path $venv) { $venv } else { "python" }
}

Push-Location $repo
try {
    & $Python -m scripts.backup_sqlite --db $Db --dest $Dest --label scheduled --keep $Keep
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
