& (Join-Path $PSScriptRoot "platform.ps1") stop
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
