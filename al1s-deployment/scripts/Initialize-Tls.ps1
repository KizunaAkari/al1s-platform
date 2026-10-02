param(
    [Parameter(Mandatory = $true)]
    [string]$DnsName,
    [string]$IpAddress = "",
    [string]$OutputDirectory = "",
    [string]$OpenSslPath = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$DeploymentRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $DeploymentRoot "compose\tls\generated"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

if (-not $OpenSslPath) {
    $Candidates = @(
        "C:\Program Files\Git\usr\bin\openssl.exe",
        "C:\Program Files\Git\mingw64\bin\openssl.exe"
    )
    $OpenSslPath = $Candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $OpenSslPath) {
        $OpenSslPath = (Get-Command openssl -ErrorAction Stop).Source
    }
}

$ManagedFiles = @("ca.crt", "ca.key", "ca.srl", "server.crt", "server.csr", "server.key")
$Existing = $ManagedFiles | Where-Object { Test-Path -LiteralPath (Join-Path $OutputDirectory $_) }
if ($Existing -and -not $Force) {
    throw "TLS assets already exist in $OutputDirectory. Use -Force only for an intentional CA replacement."
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
if ($Force) {
    foreach ($Name in $ManagedFiles) {
        Remove-Item -LiteralPath (Join-Path $OutputDirectory $Name) -Force -ErrorAction SilentlyContinue
    }
}

$SubjectAlternativeNames = @("DNS:$DnsName")
if ($IpAddress) {
    $ParsedAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($IpAddress, [ref]$ParsedAddress)) {
        throw "IpAddress must be a valid IPv4 or IPv6 address when provided."
    }
    $SubjectAlternativeNames += "IP:$IpAddress"
}

$ExtensionFile = Join-Path $OutputDirectory "server-ext.cnf"
@"
subjectAltName=$($SubjectAlternativeNames -join ',')
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
"@ | Set-Content -LiteralPath $ExtensionFile -Encoding ascii

function Invoke-OpenSsl {
    param([Parameter(Mandatory = $true)][string[]]$OpenSslArguments)
    & $OpenSslPath @OpenSslArguments
    if ($LASTEXITCODE -ne 0) {
        throw "openssl failed with exit code $LASTEXITCODE"
    }
}

Push-Location -LiteralPath $OutputDirectory
try {
    Invoke-OpenSsl -OpenSslArguments @("genrsa", "-out", "ca.key", "3072")
    Invoke-OpenSsl -OpenSslArguments @("req", "-x509", "-new", "-sha256", "-key", "ca.key", "-out", "ca.crt", "-days", "3650", "-subj", "/CN=AL-1S Private CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash")
    Invoke-OpenSsl -OpenSslArguments @("genrsa", "-out", "server.key", "3072")
    Invoke-OpenSsl -OpenSslArguments @("req", "-new", "-sha256", "-key", "server.key", "-out", "server.csr", "-subj", "/CN=$DnsName")
    Invoke-OpenSsl -OpenSslArguments @("x509", "-req", "-sha256", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "server.crt", "-days", "825", "-extfile", "server-ext.cnf")
    Invoke-OpenSsl -OpenSslArguments @("verify", "-x509_strict", "-purpose", "sslserver", "-CAfile", "ca.crt", "server.crt")
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $ExtensionFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $OutputDirectory "server.csr") -Force -ErrorAction SilentlyContinue
}

Write-Host "TLS assets created in $OutputDirectory"
Write-Host "Certificate SAN: $($SubjectAlternativeNames -join ', ')"
Write-Host "Install ca.crt on terminals; never copy ca.key to a terminal or container."
