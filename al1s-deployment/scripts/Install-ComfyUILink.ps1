[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$ComfyRoot,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$ExpectedCommand
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $ComfyRoot).Path
$python = (Resolve-Path -LiteralPath $PythonExecutable).Path
$handler = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'Start-From-AL1S.ps1')).Path
$controller = Join-Path $PSScriptRoot 'Control-ComfyUI.ps1'
if (-not (Test-Path -LiteralPath (Join-Path $root 'main.py') -PathType Leaf) -or
    -not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $controller -PathType Leaf)) { throw 'local_vision_installation_missing' }

$registryPath = 'Software\Classes\al1s-vision\shell\open\command'
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($registryPath, $true)
if ($null -eq $key) { throw 'local_vision_protocol_missing' }
try {
    $previous = [string]$key.GetValue('')
    if ($previous -cne $ExpectedCommand) { throw 'local_vision_protocol_changed' }
    $stateDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\packages\components\runtime\local-vision'))
    $configPath = Join-Path $stateDir 'config.json'
    if ((Test-Path -LiteralPath $configPath) -or (Test-Path -LiteralPath (Join-Path $stateDir 'process.json'))) {
        throw 'local_vision_existing_state'
    }
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $command = '"{0}" -NoProfile -NonInteractive -WindowStyle Hidden -File "{1}" "%1"' -f $powershell, $handler
    if ($PSCmdlet.ShouldProcess('current user al1s-vision protocol', 'update the existing ComfyUI entry')) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $stateDir 'protocol-before.txt'), $previous, [Text.Encoding]::UTF8)
        $config = @{ comfy_root = $root; python_executable = $python } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($configPath, $config, [Text.Encoding]::UTF8)
        if ([string]$key.GetValue('') -cne $previous) { throw 'local_vision_protocol_changed' }
        $key.SetValue('', $command, [Microsoft.Win32.RegistryValueKind]::String)
        if ([string]$key.GetValue('') -cne $command) { throw 'local_vision_protocol_unconfirmed' }
    }
} finally {
    $key.Dispose()
}
