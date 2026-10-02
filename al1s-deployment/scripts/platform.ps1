param(
    [ValidateSet("start", "start-tls", "stop", "stop-tls", "restart", "restart-tls", "status", "logs", "config", "config-tls", "test", "kernel-test", "execution-test", "import-scripts")]
    [string]$Command = "start"
)

$ErrorActionPreference = "Stop"
$DeploymentRoot = Split-Path -Parent $PSScriptRoot
$WorkspaceRoot = Split-Path -Parent $DeploymentRoot
$ComposeFile = Join-Path $DeploymentRoot "compose\compose.unified.yaml"
$TlsComposeFile = Join-Path $DeploymentRoot "compose\compose.tls.yaml"
$EnvFile = Join-Path $DeploymentRoot ".env"
$ExampleEnvFile = Join-Path $DeploymentRoot ".env.example"
$TlsEnvFile = Join-Path $DeploymentRoot ".env.tls"
$ExampleTlsEnvFile = Join-Path $DeploymentRoot ".env.tls.example"

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker failed with exit code $LASTEXITCODE"
    }
}

function Initialize-LocalEnv {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Copy-Item -LiteralPath $ExampleEnvFile -Destination $EnvFile
        Write-Warning "Created .env from local-development defaults. Change credentials before non-local use."
    }
}

function Initialize-TlsEnv {
    if (-not (Test-Path -LiteralPath $TlsEnvFile)) {
        Copy-Item -LiteralPath $ExampleTlsEnvFile -Destination $TlsEnvFile
        Write-Warning "Created .env.tls. Set its public DNS name/ports to match the generated server certificate."
    }
}

function Assert-TlsAssets {
    foreach ($Name in @("ca.crt", "server.crt", "server.key")) {
        $Path = Join-Path $DeploymentRoot "compose\tls\generated\$Name"
        if (-not (Test-Path -LiteralPath $Path)) {
            throw "Missing TLS asset: $Path. Run scripts\Initialize-Tls.ps1 first."
        }
    }
}

Set-Location -LiteralPath $WorkspaceRoot
Invoke-Docker info *> $null
Initialize-LocalEnv
$ComposeArgs = @("compose", "--env-file", $EnvFile, "-f", $ComposeFile)
$TlsComposeArgs = @(
    "compose", "--env-file", $EnvFile, "--env-file", $TlsEnvFile,
    "-f", $ComposeFile, "-f", $TlsComposeFile
)

switch ($Command) {
    "start" {
        Invoke-Docker @ComposeArgs "up" "-d" "--build" "--wait"
        Invoke-Docker @ComposeArgs "ps"
        Write-Host "AL-1S Next: http://localhost:8180"
    }
    "start-tls" {
        Initialize-TlsEnv
        Assert-TlsAssets
        Invoke-Docker @TlsComposeArgs "up" "-d" "--build" "--wait"
        Invoke-Docker @TlsComposeArgs "ps"
        Write-Host "AL-1S Next TLS: https://localhost:8443"
    }
    "stop" { Invoke-Docker @ComposeArgs "down" }
    "stop-tls" {
        Initialize-TlsEnv
        Invoke-Docker @TlsComposeArgs "down"
    }
    "restart" {
        Invoke-Docker @ComposeArgs "down"
        Invoke-Docker @ComposeArgs "up" "-d" "--build" "--wait"
    }
    "restart-tls" {
        Initialize-TlsEnv
        Assert-TlsAssets
        Invoke-Docker @TlsComposeArgs "down"
        Invoke-Docker @TlsComposeArgs "up" "-d" "--build" "--wait"
    }
    "status" { Invoke-Docker @ComposeArgs "ps" }
    "logs" { Invoke-Docker @ComposeArgs "logs" "--tail" "200" "-f" }
    "config" { Invoke-Docker @ComposeArgs "config" }
    "config-tls" {
        Initialize-TlsEnv
        Invoke-Docker @TlsComposeArgs "config"
    }
    "test" {
        Invoke-Docker "build" "--target" "test" "-t" "al1s-next-backend-test" "$WorkspaceRoot\backend\al1s-backend"
        Invoke-Docker "run" "--rm" "al1s-next-backend-test"
        Invoke-Docker "build" "--target" "test" "-t" "al1s-next-frontend-test" "$WorkspaceRoot\al1s-frontend"
        Invoke-Docker "run" "--rm" "al1s-next-frontend-test"
    }
    "kernel-test" {
        & (Join-Path $PSScriptRoot "Test-ExecutionKernel.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "Platform kernel tests failed with exit code $LASTEXITCODE"
        }
    }
    "execution-test" {
        & (Join-Path $PSScriptRoot "Test-ExecutionKernel.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "Execution kernel tests failed with exit code $LASTEXITCODE"
        }
    }
    "import-scripts" {
        & (Join-Path $PSScriptRoot "Import-MaaArchive.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "Maa script import failed with exit code $LASTEXITCODE"
        }
    }
}
