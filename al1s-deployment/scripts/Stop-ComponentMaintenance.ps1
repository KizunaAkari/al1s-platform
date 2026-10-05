param(
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [switch]$RemoveLogonTask,
    [switch]$InterruptActiveOperation
)
$ErrorActionPreference = 'Stop'
$componentConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$componentSettings = Get-Content -LiteralPath $componentConfig -Raw | ConvertFrom-Json
$componentOperations = Join-Path $componentSettings.state_dir 'operations'
if ((Test-Path -LiteralPath $componentOperations) -and -not $InterruptActiveOperation) {
    foreach ($componentRecord in Get-ChildItem -LiteralPath $componentOperations -File -Filter '*.json') {
        $componentState = Get-Content -LiteralPath $componentRecord.FullName -Raw | ConvertFrom-Json
        if ($componentState.state -notin @('succeeded', 'failed_safe')) {
            throw 'A component operation is unresolved. Reconcile it before stopping the executor.'
        }
    }
}
$componentHosts = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe','pythonw.exe') -and
    $_.CommandLine -match ' -m al1s\.components\.host --config ' -and
    $_.CommandLine.Contains($componentConfig)
}
foreach ($componentHost in $componentHosts) { Stop-Process -Id $componentHost.ProcessId }
if ($RemoveLogonTask) { Unregister-ScheduledTask -TaskName 'AL1S-Component-Maintenance' -Confirm:$false -ErrorAction SilentlyContinue }
