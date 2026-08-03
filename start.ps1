& (Join-Path $PSScriptRoot "platform.ps1") start
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
