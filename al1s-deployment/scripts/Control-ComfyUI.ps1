param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('start', 'stop')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$stateDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\packages\components\runtime\local-vision'))
$configPath = Join-Path $stateDir 'config.json'
$processPath = Join-Path $stateDir 'process.json'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw 'local_vision_not_installed' }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$comfyRoot = [IO.Path]::GetFullPath([string]$config.comfy_root)
$python = [IO.Path]::GetFullPath([string]$config.python_executable)
$main = Join-Path $comfyRoot 'main.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $main -PathType Leaf)) { throw 'local_vision_installation_missing' }

function Get-OwnedProcess($record) {
    if ($null -eq $record -or $record.pid -isnot [ValueType] -or [int64]$record.pid -le 0) {
        throw 'local_vision_identity_invalid'
    }
    $process = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId = ' + [int64]$record.pid) -ErrorAction Stop
    if ($null -eq $process) { return $null }
    if ([string]$process.ExecutablePath -ine $python -or
        [string]$process.ExecutablePath -ine [string]$record.executable -or
        [string]$process.CommandLine -cne [string]$record.command_line -or
        $process.CreationDate.ToUniversalTime().ToString('o') -ne ([DateTime]$record.created_at).ToUniversalTime().ToString('o') -or
        ([string]$process.CommandLine).IndexOf($main, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'local_vision_identity_mismatch'
    }
    return $process
}

function Start-ManagedComfy {
    if (Test-Path -LiteralPath $processPath -PathType Leaf) {
        $record = Get-Content -LiteralPath $processPath -Raw | ConvertFrom-Json
        if ($null -ne (Get-OwnedProcess $record)) { return }
        Remove-Item -LiteralPath $processPath
    }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8188 -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) { throw 'local_vision_port_occupied' }
    $arguments = @('-s', ('"' + $main + '"'), '--listen', '127.0.0.1', '--port', '8188')
    $launched = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $comfyRoot `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir 'stdout.log') `
        -RedirectStandardError (Join-Path $stateDir 'stderr.log') -PassThru
    $process = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId = ' + $launched.Id) -ErrorAction Stop
    if ($null -eq $process -or [string]$process.ExecutablePath -ine $python -or
        ([string]$process.CommandLine).IndexOf($main, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'local_vision_start_unconfirmed'
    }
    $record = @{ pid = [int64]$process.ProcessId; executable = [string]$process.ExecutablePath;
        command_line = [string]$process.CommandLine; created_at = $process.CreationDate.ToUniversalTime().ToString('o') }
    [IO.File]::WriteAllText($processPath, ($record | ConvertTo-Json -Compress), [Text.Encoding]::UTF8)
}

function Stop-ManagedComfy {
    if (-not (Test-Path -LiteralPath $processPath -PathType Leaf)) { throw 'local_vision_process_unmanaged' }
    $record = Get-Content -LiteralPath $processPath -Raw | ConvertFrom-Json
    $process = Get-OwnedProcess $record
    if ($null -ne $process) {
        Stop-Process -Id $process.ProcessId -ErrorAction Stop
        Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction Stop
    }
    Remove-Item -LiteralPath $processPath
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\AL1S-ComfyUI-Control')
$locked = $false
try {
    $locked = $mutex.WaitOne(0)
    if (-not $locked) { throw 'local_vision_control_busy' }
    if ($Action -eq 'start') { Start-ManagedComfy } else { Stop-ManagedComfy }
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
