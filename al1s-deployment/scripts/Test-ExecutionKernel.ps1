$ErrorActionPreference = "Stop"

$DeploymentRoot = Split-Path -Parent $PSScriptRoot
$WorkspaceRoot = Split-Path -Parent $DeploymentRoot
$ComposeFile = Join-Path $DeploymentRoot "compose\compose.yaml"
$EnvFile = Join-Path $DeploymentRoot ".env"

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Missing deployment environment file: $EnvFile"
}

$ComposeArgs = @("compose", "--env-file", $EnvFile, "--file", $ComposeFile)
$DatabaseCommand = 'dropdb --if-exists -U "$POSTGRES_USER" al1s_execution_test && createdb -U "$POSTGRES_USER" al1s_execution_test'
$CleanupCommand = 'dropdb --if-exists -U "$POSTGRES_USER" al1s_execution_test'

Set-Location -LiteralPath $WorkspaceRoot
Invoke-Docker info *> $null
Invoke-Docker @ComposeArgs "up" "-d" "--wait" "postgres" "seaweed-s3" "s3-init"
Invoke-Docker @ComposeArgs "exec" "-T" "postgres" "sh" "-ec" $DatabaseCommand

try {
    Invoke-Docker @ComposeArgs "--profile" "test" "build" "execution-test"
    Invoke-Docker @ComposeArgs "--profile" "test" "run" "--rm" "execution-test"
}
finally {
    Invoke-Docker @ComposeArgs "exec" "-T" "postgres" "sh" "-ec" $CleanupCommand
}
