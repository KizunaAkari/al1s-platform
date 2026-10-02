param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}\.local$')]
    [string]$TerminalHostname,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]+$')]
    [string]$ContainerName,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$taskName = "AL1S-LAN-$ContainerName-$TerminalHostname"
$syncScript = Join-Path $PSScriptRoot 'Sync-TerminalLanAddress.ps1'
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $taskName
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    & $syncScript -TerminalHostname $TerminalHostname -ContainerName $ContainerName -Remove
    exit
}
# Validate the actual path before installing an unattended refresh.
& $syncScript -TerminalHostname $TerminalHostname -ContainerName $ContainerName
$launcher = Join-Path $PSScriptRoot 'Run-TerminalLanDiscovery.vbs'
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
if (-not (Test-Path -LiteralPath $launcher) -or -not (Test-Path -LiteralPath $wscript)) {
    throw 'Windowless task launcher is unavailable; no visible-console fallback is installed.'
}
$arguments = '//B //NoLogo "{0}" "{1}" "{2}"' -f $launcher, $TerminalHostname, $ContainerName
$action = New-ScheduledTaskAction -Execute $wscript -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 50) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'AL-1S scoped LAN discovery for Docker Desktop TUN compatibility; no credentials stored.' -Force | Out-Null
Write-Output "Installed $taskName (current logged-in user, every minute)"
