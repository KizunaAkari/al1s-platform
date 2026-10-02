param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}\.local$')]
    [string]$TerminalHostname,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]+$')]
    [string]$ContainerName,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$address = 'remove'
$discoveryFailed = $false
if (-not $Remove) {
    try {
        $addresses = @(Resolve-DnsName $TerminalHostname -LlmnrOnly -QuickTimeout -ErrorAction Stop |
            Where-Object { $_.IPAddress -match '^\d+\.\d+\.\d+\.\d+$' } |
            Select-Object -ExpandProperty IPAddress -Unique)
        if ($addresses.Count -ne 1) { throw 'Expected one LAN IPv4 address' }
        $address = $addresses[0]
        if ($address -notmatch '^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)') {
            throw 'Refusing non-RFC1918 address'
        }
    } catch {
        $address = 'remove'
        $discoveryFailed = $true
    }
}
$script = Join-Path (Split-Path -Parent $PSScriptRoot) 'tools/container_lan_hosts.py'
Get-Content -LiteralPath $script -Raw -Encoding UTF8 |
    docker exec -i --user 0 $ContainerName python - $TerminalHostname $address
if ($LASTEXITCODE -ne 0) { throw 'Container LAN mapping update failed' }
if ($discoveryFailed) { throw 'LAN discovery failed; managed mapping removed' }
