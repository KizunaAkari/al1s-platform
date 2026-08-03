[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "backup", "restore", "export", "import", "verify", "logs")]
    [string]$Command = "start",

    [Parameter(Position = 1)]
    [string]$PackagePath = "",

    [switch]$NoImage,
    [switch]$KeepLocalEnv,
    [switch]$NoBrowser,
    [switch]$NoDockerAutoStart,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$BackupsRoot = Join-Path $ProjectRoot "backups"
$ScriptsRoot = Join-Path $ProjectRoot "scripts"
$VolumeName = "maa-platform-data"
$ImageName = "maa-test-platform:demo"
$ServiceName = "control-center"
$PlatformUrl = "http://127.0.0.1:8000"
$PackageFormat = 1
$PlatformVersion = (Get-Content (Join-Path $ProjectRoot "VERSION") -Raw -Encoding UTF8).Trim()

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter(Mandatory)] [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Test-DockerEngine {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return $false
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell converts native stderr into an ErrorRecord when
        # ErrorActionPreference is Stop, so probe the daemon in Continue mode.
        $ErrorActionPreference = "Continue"
        & docker info 1>$null 2>$null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Get-DockerDesktopPath {
    $candidates = @()
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\Docker Desktop.exe"
    }
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe"
    }
    return $candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}

function Start-DockerDesktopAndWait {
    $dockerDesktopPath = Get-DockerDesktopPath
    if (-not $dockerDesktopPath) {
        throw "Docker CLI is installed, but Docker Desktop was not found. Start Docker Desktop manually, then retry."
    }

    $desktopProcess = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
    if (-not $desktopProcess) {
        Write-Host "Docker Engine is unavailable; starting Docker Desktop..." -ForegroundColor Yellow
        Start-Process -FilePath $dockerDesktopPath -WindowStyle Hidden | Out-Null
    }
    else {
        Write-Host "Docker Desktop is running; waiting for the Linux Engine..." -ForegroundColor Yellow
    }

    for ($attempt = 1; $attempt -le 60; $attempt++) {
        if (Test-DockerEngine) {
            Write-Host "Docker Engine is ready." -ForegroundColor Green
            return
        }
        if ($attempt % 5 -eq 0) {
            Write-Host "Still waiting for Docker Engine ($($attempt * 2)s)..." -ForegroundColor DarkGray
        }
        Start-Sleep -Seconds 2
    }

    throw "Docker Desktop started, but the Linux Engine was not ready after 120 seconds. Open Docker Desktop and inspect its status or WSL error message."
}

function Assert-Docker {
    param([switch]$AutoStart)

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI was not found. Install Docker Desktop and reopen PowerShell."
    }
    if (Test-DockerEngine) {
        return
    }
    if ($AutoStart -and $env:OS -eq "Windows_NT") {
        Start-DockerDesktopAndWait
        return
    }
    throw "Docker Engine is not running. Start Docker Desktop, or run '.\platform.ps1 start' to start it automatically."
}

function Get-AbsolutePath {
    param([string]$Value)
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $Value))
}

function New-StagingDirectory {
    New-Item -ItemType Directory -Path $BackupsRoot -Force | Out-Null
    $target = Join-Path $BackupsRoot (".stage-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $target | Out-Null
    return $target
}

function Remove-StagingDirectory {
    param([string]$Target)
    $resolvedBackups = [System.IO.Path]::GetFullPath($BackupsRoot).TrimEnd('\') + '\'
    $resolvedTarget = [System.IO.Path]::GetFullPath($Target)
    if (-not $resolvedTarget.StartsWith($resolvedBackups, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove staging directory outside backups: $resolvedTarget"
    }
    if (Test-Path -LiteralPath $resolvedTarget) {
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
}

function Test-ImageExists {
    & docker image inspect $ImageName *> $null
    return $LASTEXITCODE -eq 0
}

function Ensure-Image {
    if (-not (Test-ImageExists)) {
        Invoke-NativeCommand docker @("compose", "build", $ServiceName)
    }
}

function Test-VolumeExists {
    & docker volume inspect $VolumeName *> $null
    return $LASTEXITCODE -eq 0
}

function Ensure-Volume {
    if (-not (Test-VolumeExists)) {
        Invoke-NativeCommand docker @("volume", "create", $VolumeName)
    }
}

function Test-ServiceRunning {
    $services = & docker compose ps --status running --services 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    return @($services) -contains $ServiceName
}

function Wait-PlatformHealth {
    for ($attempt = 1; $attempt -le 45; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "$PlatformUrl/api/health" -TimeoutSec 2
            if ($health.ok) {
                Write-Host "Platform is healthy: $PlatformUrl (v$($health.version))" -ForegroundColor Green
                return
            }
        }
        catch { }
        Start-Sleep -Seconds 1
    }
    & docker compose logs --tail 80 $ServiceName
    throw "Platform did not become healthy in time."
}

function Start-Platform {
    param([switch]$SkipBuild, [switch]$OpenBrowser)
    $arguments = @("compose", "up", "-d")
    if ($SkipBuild) {
        $arguments += "--no-build"
    }
    else {
        $arguments += "--build"
    }
    Invoke-NativeCommand docker $arguments
    Wait-PlatformHealth
    if ($OpenBrowser) {
        Start-Process $PlatformUrl
    }
}

function Invoke-VolumeArchiveTool {
    param(
        [ValidateSet("backup", "restore")] [string]$Operation,
        [string]$ArchivePath
    )
    Ensure-Image
    $archiveDirectory = Split-Path -Parent $ArchivePath
    $archiveName = Split-Path -Leaf $ArchivePath
    $volumeMount = if ($Operation -eq "backup") { "${VolumeName}:/volume:ro" } else { "${VolumeName}:/volume" }
    $packageMount = if ($Operation -eq "backup") { "${archiveDirectory}:/package" } else { "${archiveDirectory}:/package:ro" }
    Invoke-NativeCommand docker @(
        "run", "--rm", "--entrypoint", "python",
        "-v", $volumeMount,
        "-v", "${ScriptsRoot}:/tools:ro",
        "-v", $packageMount,
        $ImageName,
        "/tools/volume_archive.py", $Operation,
        "--root", "/volume",
        "--archive", "/package/$archiveName"
    )
}

function New-DataArchive {
    param([string]$ArchivePath, [switch]$AllowNewVolume)
    if (-not (Test-VolumeExists)) {
        if (-not $AllowNewVolume) {
            throw "Docker volume '$VolumeName' does not exist. Start the platform before creating a backup."
        }
        Ensure-Volume
    }
    $wasRunning = Test-ServiceRunning
    try {
        if ($wasRunning) {
            Invoke-NativeCommand docker @("compose", "stop", $ServiceName)
        }
        Invoke-VolumeArchiveTool "backup" $ArchivePath
    }
    finally {
        if ($wasRunning) {
            Invoke-NativeCommand docker @("compose", "start", $ServiceName)
            Wait-PlatformHealth
        }
    }
}

function Restore-DataArchive {
    param([string]$ArchivePath)
    Ensure-Volume
    Ensure-Image
    New-Item -ItemType Directory -Path $BackupsRoot -Force | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $safetyArchive = Join-Path $BackupsRoot "pre-restore-$timestamp-platform-data.tar.gz"
    $wasRunning = Test-ServiceRunning
    try {
        if ($wasRunning) {
            Invoke-NativeCommand docker @("compose", "stop", $ServiceName)
        }
        Invoke-VolumeArchiveTool "backup" $safetyArchive
        try {
            Invoke-VolumeArchiveTool "restore" $ArchivePath
        }
        catch {
            Write-Warning "Restore failed; rolling back from $safetyArchive"
            Invoke-VolumeArchiveTool "restore" $safetyArchive
            throw
        }
    }
    finally {
        Invoke-NativeCommand docker @("compose", "up", "-d", "--no-build")
    }
    Wait-PlatformHealth
    Write-Host "Pre-restore safety backup: $safetyArchive" -ForegroundColor Yellow
}

function Write-Checksums {
    param([string]$Directory)
    $checksumPath = Join-Path $Directory "checksums.sha256"
    $lines = @(Get-ChildItem -LiteralPath $Directory -File |
        Where-Object { $_.Name -ne "checksums.sha256" } |
        Sort-Object Name |
        ForEach-Object { "{0}  {1}" -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $_.Name })
    $content = if ($lines.Count) { ($lines -join "`n") + "`n" } else { "" }
    [System.IO.File]::WriteAllText($checksumPath, $content, [System.Text.UTF8Encoding]::new($false))
}

function Test-Checksums {
    param([string]$Directory)
    $checksumPath = Join-Path $Directory "checksums.sha256"
    if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
        throw "Package is missing checksums.sha256"
    }
    foreach ($line in Get-Content -LiteralPath $checksumPath -Encoding UTF8) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '\s+', 2
        if ($parts.Count -ne 2) { throw "Invalid checksum line: $line" }
        $fileName = $parts[1].Trim()
        if ([System.IO.Path]::GetFileName($fileName) -ne $fileName) {
            throw "Unsafe package file name in checksum list: $fileName"
        }
        $target = Join-Path $Directory $fileName
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "Package file is missing: $fileName"
        }
        $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $parts[0].ToLowerInvariant()) {
            throw "Checksum mismatch: $fileName"
        }
    }
}

function New-PlatformPackage {
    param([string]$OutputPath, [switch]$IncludeImage)
    $staging = New-StagingDirectory
    try {
        New-DataArchive (Join-Path $staging "platform-data.tar.gz")
        $environmentFile = Join-Path $ProjectRoot ".env"
        $hasEnvironment = Test-Path -LiteralPath $environmentFile -PathType Leaf
        if ($hasEnvironment) {
            Copy-Item -LiteralPath $environmentFile -Destination (Join-Path $staging "platform.env")
        }
        Copy-Item -LiteralPath (Join-Path $ProjectRoot "VERSION") -Destination (Join-Path $staging "VERSION")
        if ($IncludeImage) {
            Ensure-Image
            Invoke-NativeCommand docker @("save", "--output", (Join-Path $staging "platform-image.tar"), $ImageName)
        }
        $manifest = [ordered]@{
            package_format = $PackageFormat
            platform = "maa-test-platform"
            version = $PlatformVersion
            created_at = (Get-Date).ToUniversalTime().ToString("o")
            volume = $VolumeName
            image = if ($IncludeImage) { $ImageName } else { $null }
            environment_included = $hasEnvironment
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $staging "manifest.json"),
            (($manifest | ConvertTo-Json) + "`n"),
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Checksums $staging

        $absoluteOutput = Get-AbsolutePath $OutputPath
        New-Item -ItemType Directory -Path (Split-Path -Parent $absoluteOutput) -Force | Out-Null
        if (Test-Path -LiteralPath $absoluteOutput) {
            throw "Output package already exists: $absoluteOutput"
        }
        Invoke-NativeCommand tar @("-czf", $absoluteOutput, "-C", $staging, ".")
        Write-Host "Platform package created: $absoluteOutput" -ForegroundColor Green
        Write-Warning "The package contains the platform database and notification encryption key; store it as a secret."
        if ($hasEnvironment) {
            Write-Warning "The package also contains platform.env."
        }
        return $absoluteOutput
    }
    finally {
        Remove-StagingDirectory $staging
    }
}

function Test-PackageEntries {
    param([string]$ArchivePath)
    $entries = & tar -tzf $ArchivePath
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read package archive: $ArchivePath"
    }
    foreach ($entry in $entries) {
        $normalized = ([string]$entry).Replace('\', '/').TrimStart('.', '/')
        if (
            $entry -match '^[\\/]' -or
            $entry -match '^[A-Za-z]:' -or
            $normalized -match '(^|/)\.\.(/|$)'
        ) {
            throw "Package contains an unsafe path: $entry"
        }
    }
}

function Test-IsPlatformPackage {
    param([string]$ArchivePath)
    $entries = & tar -tzf $ArchivePath
    if ($LASTEXITCODE -ne 0) { return $false }
    $names = @($entries | ForEach-Object { ([string]$_).Replace('\', '/').TrimStart('.', '/') })
    return ($names -contains "manifest.json") -and ($names -contains "platform-data.tar.gz")
}

function Expand-PlatformPackage {
    param([string]$InputPath)
    $absoluteInput = Get-AbsolutePath $InputPath
    if (-not (Test-Path -LiteralPath $absoluteInput -PathType Leaf)) {
        throw "Package does not exist: $absoluteInput"
    }
    Test-PackageEntries $absoluteInput
    $staging = New-StagingDirectory
    try {
        Invoke-NativeCommand tar @("-xzf", $absoluteInput, "-C", $staging)
        foreach ($required in @("platform-data.tar.gz", "VERSION", "manifest.json", "checksums.sha256")) {
            if (-not (Test-Path -LiteralPath (Join-Path $staging $required) -PathType Leaf)) {
                throw "Package is missing $required"
            }
        }
        Test-Checksums $staging
        $manifest = Get-Content -LiteralPath (Join-Path $staging "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([int]$manifest.package_format -ne $PackageFormat -or [string]$manifest.platform -ne "maa-test-platform") {
            throw "Unsupported platform package format."
        }
        $packageVersion = (Get-Content -LiteralPath (Join-Path $staging "VERSION") -Raw -Encoding UTF8).Trim()
        if ($packageVersion -ne $PlatformVersion) {
            Write-Warning "Package version is $packageVersion; local platform source is $PlatformVersion."
        }
        return $staging
    }
    catch {
        Remove-StagingDirectory $staging
        throw
    }
}

function Confirm-DestructiveRestore {
    if ($Yes) { return }
    $answer = Read-Host "This will replace the current platform data volume. Type RESTORE to continue"
    if ($answer -ne "RESTORE") {
        throw "Restore cancelled."
    }
}

function Import-Environment {
    param([string]$Staging)
    $imported = Join-Path $Staging "platform.env"
    if (-not (Test-Path -LiteralPath $imported -PathType Leaf)) { return }
    if ($KeepLocalEnv) {
        Write-Host "Keeping the current .env file." -ForegroundColor Yellow
        return
    }
    $localEnvironment = Join-Path $ProjectRoot ".env"
    if (Test-Path -LiteralPath $localEnvironment -PathType Leaf) {
        New-Item -ItemType Directory -Path $BackupsRoot -Force | Out-Null
        $saved = Join-Path $BackupsRoot ("pre-import-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".env")
        Copy-Item -LiteralPath $localEnvironment -Destination $saved
        Write-Host "Existing .env saved to: $saved" -ForegroundColor Yellow
    }
    Copy-Item -LiteralPath $imported -Destination $localEnvironment -Force
}

function Restore-PlatformPackage {
    param([string]$InputPath, [switch]$LoadImage)
    Confirm-DestructiveRestore
    $staging = Expand-PlatformPackage $InputPath
    try {
        Import-Environment $staging
        $imageArchive = Join-Path $staging "platform-image.tar"
        if ($LoadImage -and (Test-Path -LiteralPath $imageArchive -PathType Leaf)) {
            Invoke-NativeCommand docker @("load", "--input", $imageArchive)
        }
        elseif (-not (Test-ImageExists)) {
            Invoke-NativeCommand docker @("compose", "build", $ServiceName)
        }
        Restore-DataArchive (Join-Path $staging "platform-data.tar.gz")
    }
    finally {
        Remove-StagingDirectory $staging
    }
}

Push-Location $ProjectRoot
try {
    if ($Command -eq "verify") {
        if (-not $PackagePath) { throw "verify requires a package path" }
        $verifiedStaging = Expand-PlatformPackage $PackagePath
        try {
            Get-Content -LiteralPath (Join-Path $verifiedStaging "manifest.json") -Encoding UTF8
            Write-Host "Package verification succeeded: $(Get-AbsolutePath $PackagePath)" -ForegroundColor Green
        }
        finally {
            Remove-StagingDirectory $verifiedStaging
        }
        return
    }
    Assert-Docker -AutoStart:($Command -eq "start" -and -not $NoDockerAutoStart)
    switch ($Command) {
        "start" {
            Start-Platform -OpenBrowser:(-not $NoBrowser)
        }
        "stop" {
            Invoke-NativeCommand docker @("compose", "down")
        }
        "status" {
            Invoke-NativeCommand docker @("compose", "ps")
            try {
                $health = Invoke-RestMethod -Uri "$PlatformUrl/api/health" -TimeoutSec 3
                $health | ConvertTo-Json -Compress
            }
            catch {
                Write-Warning "Platform health endpoint is unavailable."
            }
        }
        "logs" {
            Invoke-NativeCommand docker @("compose", "logs", "--tail", "200", $ServiceName)
        }
        "backup" {
            $target = if ($PackagePath) { $PackagePath } else { "backups/maa-platform-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss').tar.gz" }
            New-PlatformPackage $target | Out-Null
        }
        "export" {
            $target = if ($PackagePath) { $PackagePath } else { "backups/maa-platform-export-$(Get-Date -Format 'yyyyMMdd-HHmmss').tar.gz" }
            New-PlatformPackage $target -IncludeImage:(-not $NoImage) | Out-Null
        }
        "restore" {
            if (-not $PackagePath) { throw "restore requires a package path" }
            $restorePath = Get-AbsolutePath $PackagePath
            if (-not (Test-Path -LiteralPath $restorePath -PathType Leaf)) { throw "Backup does not exist: $restorePath" }
            if (Test-IsPlatformPackage $restorePath) {
                Restore-PlatformPackage $restorePath
            }
            else {
                Confirm-DestructiveRestore
                Restore-DataArchive $restorePath
            }
        }
        "import" {
            if (-not $PackagePath) { throw "import requires a package path" }
            Restore-PlatformPackage $PackagePath -LoadImage
        }
    }
}
finally {
    Pop-Location
}
