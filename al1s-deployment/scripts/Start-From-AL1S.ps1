param([Parameter(Position = 0)][string]$Uri = '')

$ErrorActionPreference = 'Stop'
function Write-LinkStatus([string]$value) {
    try {
        $path = Join-Path $PSScriptRoot '..\packages\components\runtime\local-vision\link-status.txt'
        [IO.File]::WriteAllText($path, $value)
    } catch { }
}
Write-LinkStatus 'invoked'
try {
    if ($args.Count -ne 0 -or $Uri -inotmatch '\Aal1s-vision://(start|stop)/?\z') {
        throw 'Invalid local vision link'
    }
    $action = $Matches[1].ToLowerInvariant()
    $script = Join-Path $PSScriptRoot 'Control-ComfyUI.ps1'
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        throw 'The existing local vision script was not found'
    }
    & $script -Action $action
    Write-LinkStatus ($action + ' requested')
} catch {
    Write-LinkStatus ('failed: ' + ($_.Exception.Message -replace '[\r\n]', ' '))
    throw
}
