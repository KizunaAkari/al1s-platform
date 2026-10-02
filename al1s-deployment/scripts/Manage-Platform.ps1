param(
    [ValidateSet('Status', 'Validate', 'Start', 'Stop')]
    [string]$Action = 'Status'
)
$ErrorActionPreference = 'Stop'
$deployment = Split-Path $PSScriptRoot -Parent
$composeArgs = @(
    'compose', '-p', 'al1s-platform',
    '--env-file', (Join-Path $deployment '.env'),
    '--env-file', (Join-Path $deployment '.env.tls'),
    '--env-file', (Join-Path $deployment 'packages/linux/platform-host-manager.env'),
    '--env-file', (Join-Path $deployment 'platform.env'),
    '-f', (Join-Path $deployment 'compose/compose.unified.yaml'),
    '-f', (Join-Path $deployment 'compose/compose.tls.yaml'),
    '-f', (Join-Path $deployment 'compose/compose.host-management.yaml'),
    '-f', (Join-Path $deployment 'compose/compose.platform.yaml')
    '-f', (Join-Path $deployment 'compose/compose.logging.yaml')
)
$services = @('postgres','seaweed-master','seaweed-volume','seaweed-filer','seaweed-s3','mosquitto','backend','tls-gateway','bot-control')
switch ($Action) {
    'Status' { & docker @composeArgs ps -a }
    'Validate' { & docker @composeArgs config --quiet }
    # Existing initialized test environment only. No builds or migrations here.
    'Start' {
        & docker @composeArgs up -d --no-build --no-deps log-collector
        if ($LASTEXITCODE -ne 0) { throw 'Log collector did not start' }
        & docker @composeArgs up -d --no-build --no-deps @services
    }
    'Stop' { & docker @composeArgs stop @services }
}
if ($LASTEXITCODE -ne 0) { throw "Platform operation failed: $Action" }
