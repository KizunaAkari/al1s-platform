[CmdletBinding()]
param(
    [string]$Container = "maa-test-platform",
    [string]$Output = "",
    [string]$Database = ""
)

$ErrorActionPreference = "Stop"
$deploymentRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $deploymentRoot
$tool = Join-Path $deploymentRoot "tools\script_archive.py"

if (-not $Output) {
    $archiveDirectory = Join-Path $workspaceRoot "backups\migration"
    $Output = Join-Path $archiveDirectory "al1s-script-archive-v1.zip"
}

$localSnapshot = $null
$containerSnapshot = $null
try {
    if ($Database) {
        $databasePath = (Resolve-Path -LiteralPath $Database).Path
    }
    else {
        $snapshotId = [Guid]::NewGuid().ToString("N")
        $containerSnapshot = "/tmp/al1s-script-export-$snapshotId.db"
        $localSnapshot = Join-Path ([System.IO.Path]::GetTempPath()) "al1s-script-export-$snapshotId.db"
        $backupCode = @'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
'@
        & docker exec $Container python -c $backupCode "/app/data/control-center.db" $containerSnapshot
        if ($LASTEXITCODE -ne 0) { throw "SQLite snapshot failed with exit code $LASTEXITCODE" }
        & docker cp "${Container}:$containerSnapshot" $localSnapshot
        if ($LASTEXITCODE -ne 0) { throw "Snapshot copy failed with exit code $LASTEXITCODE" }
        $databasePath = $localSnapshot
    }

    $outputParent = Split-Path -Parent $Output
    if ($outputParent) { New-Item -ItemType Directory -Force -Path $outputParent | Out-Null }
    & python $tool export --database $databasePath --output $Output
    if ($LASTEXITCODE -ne 0) { throw "Script archive export failed with exit code $LASTEXITCODE" }
    & python $tool validate $Output
    if ($LASTEXITCODE -ne 0) { throw "Script archive validation failed with exit code $LASTEXITCODE" }
}
finally {
    if ($containerSnapshot) {
        $cleanupCode = "import os,sys; p=sys.argv[1]; os.path.exists(p) and os.remove(p)"
        & docker exec $Container python -c $cleanupCode $containerSnapshot 2>$null
    }
    if ($localSnapshot -and (Test-Path -LiteralPath $localSnapshot)) {
        Remove-Item -LiteralPath $localSnapshot -Force
    }
}
