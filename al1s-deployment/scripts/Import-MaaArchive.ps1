param(
    [string]$ArchivePath
)

$ErrorActionPreference = "Stop"
$DeploymentRoot = Split-Path -Parent $PSScriptRoot
$WorkspaceRoot = Split-Path -Parent $DeploymentRoot
$ComposeFile = Join-Path $DeploymentRoot "compose\compose.yaml"
$EnvFile = Join-Path $DeploymentRoot ".env"
$MigrationRoot = Join-Path $WorkspaceRoot "backups\migration"
$ExpectedLogicalSha256 = "969a673bef2832df497575d3820f61cec12aa2c37c01ff4e3aeee1bcd0173795"

if ([string]::IsNullOrWhiteSpace($ArchivePath)) {
    $ArchivePath = Join-Path $MigrationRoot "al1s-script-archive-v1.zip"
}
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Script archive does not exist: $ArchivePath"
}
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Missing deployment environment file: $EnvFile"
}

$ResolvedArchive = (Resolve-Path -LiteralPath $ArchivePath).Path
$ResolvedMigrationRoot = (Resolve-Path -LiteralPath $MigrationRoot).Path.TrimEnd('\')
if (-not $ResolvedArchive.StartsWith("$ResolvedMigrationRoot\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Archive must be inside the controlled migration directory: $ResolvedMigrationRoot"
}

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker failed with exit code $LASTEXITCODE"
    }
}

Set-Location -LiteralPath $WorkspaceRoot
Invoke-Docker info *> $null
$ComposeArgs = @("compose", "--env-file", $EnvFile, "-f", $ComposeFile)
Invoke-Docker @ComposeArgs "up" "-d" "--wait" "postgres" "seaweed-s3"
Invoke-Docker @ComposeArgs "build" "s3-init" "migrate"
Invoke-Docker @ComposeArgs "run" "--rm" "--no-deps" "s3-init"
Invoke-Docker @ComposeArgs "run" "--rm" "--no-deps" "migrate"
Invoke-Docker @ComposeArgs "build" "backend"
$Volume = "${ResolvedArchive}:/import/archive.zip:ro"
Invoke-Docker @ComposeArgs "run" "--rm" "--no-deps" "--volume" $Volume "backend" `
    "python" "-m" "al1s.maa.cli" "/import/archive.zip" `
    "--expected-logical-sha256" $ExpectedLogicalSha256
