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
$componentSelectors = Join-Path $deployment 'component-images.env'
if (Test-Path -LiteralPath $componentSelectors) {
    $composeArgs += @('--env-file', $componentSelectors,
                     '--env-file', (Join-Path $deployment '.env.component-host'),
                     '-f', (Join-Path $deployment 'compose/compose.components.yaml'))
    $services += 'component-maintenance'
}
$consolidationPath = Join-Path $deployment '.env.consolidation'
if (Test-Path -LiteralPath $consolidationPath) {
    $consolidationValues = Get-Content -LiteralPath $consolidationPath -Raw
    $composeArgs += @('--env-file', $consolidationPath)
    if ($consolidationValues -match '(?m)^AL1S_OPS_LAYOUT=combined\r?$') {
        $composeArgs += @('-f', (Join-Path $deployment 'compose/compose.ops.yaml'))
        $services = @($services | Where-Object { $_ -notin @('bot-control','component-maintenance') }) + 'ops'
    }
    if ($consolidationValues -match '(?m)^AL1S_STORAGE_LAYOUT=bundle\r?$') {
        $composeArgs += @('-f', (Join-Path $deployment 'compose/compose.storage.yaml'))
        $services = @($services | Where-Object { $_ -notin @('seaweed-master','seaweed-volume','seaweed-filer','seaweed-s3') }) + 'storage'
    }
}
switch ($Action) {
    'Status' { & docker @composeArgs ps -a }
    'Validate' { & docker @composeArgs config --quiet }
    # Existing initialized test environment only. No builds or migrations here.
    'Start' {
        & docker @composeArgs up -d --no-build --no-deps log-collector
        if ($LASTEXITCODE -ne 0) { throw 'Log collector did not start' }
        & docker @composeArgs up -d --no-build --no-deps @services
    }
    'Stop' {
        if (Test-Path -LiteralPath $componentSelectors) {
            $componentOperations = Join-Path $deployment 'packages/components/runtime/state/operations'
            if (-not (Test-Path -LiteralPath $componentOperations -PathType Container)) {
                throw 'Cannot verify component operation state; refusing platform stop.'
            }
            foreach ($componentRecord in Get-ChildItem -LiteralPath $componentOperations -File -Filter '*.json' -ErrorAction Stop) {
                $componentState = Get-Content -LiteralPath $componentRecord.FullName -Raw | ConvertFrom-Json
                if ($componentState.state -notin @('succeeded','failed_safe')) {
                    throw 'Reconcile the unresolved component operation before stopping the platform.'
                }
            }
        }
        & docker @composeArgs stop @services
    }
}
if ($LASTEXITCODE -ne 0) { throw "Platform operation failed: $Action" }
