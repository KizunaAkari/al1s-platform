param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [Parameter(Mandatory=$true)][string]$BackendRoot,
    [switch]$RegisterLogonTask
)
$ErrorActionPreference = 'Stop'
$componentPython = (Resolve-Path -LiteralPath $PythonPath).Path
$componentConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$componentBackend = (Resolve-Path -LiteralPath $BackendRoot).Path
$componentSettingsValue = Get-Content -LiteralPath $componentConfig -Raw | ConvertFrom-Json
if ($RegisterLogonTask -and ($componentSettingsValue.project -ne 'al1s-platform' -or
    $componentSettingsValue.container_name_prefix -ne 'al1s-platform-')) {
    throw 'Persistent installation must preserve the registered platform names'
}
if (-not (Test-Path -LiteralPath (Join-Path $componentBackend 'al1s/components/host/__main__.py'))) {
    throw 'Verified component host package is missing'
}
$componentArguments = @('-m', 'al1s.components.host', '--config', ('"' + $componentConfig + '"'))
if ($RegisterLogonTask) {
    $componentAction = New-ScheduledTaskAction -Execute $componentPython `
        -Argument ('-m al1s.components.host --config "' + $componentConfig + '"') `
        -WorkingDirectory $componentBackend
    $componentTrigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $componentSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'AL1S-Component-Maintenance' -Action $componentAction `
        -Trigger $componentTrigger -Settings $componentSettings -Description 'Registered AL-1S component maintenance' -Force | Out-Null
    Start-ScheduledTask -TaskName 'AL1S-Component-Maintenance'
    $componentProcess = $null
    for ($componentAttempt = 0; $componentAttempt -lt 20 -and -not $componentProcess; $componentAttempt++) {
        Start-Sleep -Milliseconds 500
        $componentProcess = Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq 'python.exe' -and $_.CommandLine -match ' -m al1s\.components\.host --config ' -and
            $_.CommandLine.Contains($componentConfig)
        } | Select-Object -First 1
    }
    if (-not $componentProcess) { throw 'Scheduled component executor did not start' }
    $componentProcessId = $componentProcess.ProcessId
} else {
    $componentProcess = Start-Process -FilePath $componentPython -ArgumentList $componentArguments `
        -WorkingDirectory $componentBackend -WindowStyle Hidden -PassThru
    $componentProcessId = $componentProcess.Id
}
@{ process_id = $componentProcessId; registered_logon_task = [bool]$RegisterLogonTask } | ConvertTo-Json -Compress
