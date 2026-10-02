[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$dataDirectory = Join-Path $projectRoot "llbot-data"

function ConvertTo-PlainText {
    param([Parameter(Mandatory)][Security.SecureString]$Value)

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Write-SecretFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Value
    )

    $utf8WithoutBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, "$Value`n", $utf8WithoutBom)
}

New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null

$authToken = ConvertTo-PlainText (Read-Host "LuckyLillia Auth Token" -AsSecureString)
$webUiPassword = ConvertTo-PlainText (Read-Host "LLBot WebUI password" -AsSecureString)

if ([string]::IsNullOrWhiteSpace($authToken)) {
    throw "LuckyLillia Auth Token cannot be empty."
}
if ([string]::IsNullOrWhiteSpace($webUiPassword)) {
    throw "LLBot WebUI password cannot be empty."
}

Write-SecretFile -Path (Join-Path $dataDirectory "auth_token.txt") -Value $authToken.Trim()
Write-SecretFile -Path (Join-Path $dataDirectory "webui_token.txt") -Value $webUiPassword.Trim()

Write-Host "LLBot secret files were created under llbot-data/."
Write-Host "They are ignored by Git. Continue with the OneBot 11 WebUI setup in README.md."
