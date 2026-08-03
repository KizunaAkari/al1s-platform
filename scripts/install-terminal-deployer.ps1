param(
    [string]$BoardHost = "192.168.5.21",
    [string]$BoardUser = "root",
    [string]$TargetDir = "/run/media/mmcblk1p8/maa-test-terminal"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$remote = "${BoardUser}@${BoardHost}"
$remoteDeployDir = "${TargetDir}/deploy/terminal"

Write-Host "Uploading terminal deployment service to $remote" -ForegroundColor Cyan
ssh $remote "mkdir -p '$remoteDeployDir'"
if ($LASTEXITCODE -ne 0) { throw "Unable to create the remote terminal deployment directory." }

scp `
    (Join-Path $projectRoot "deploy/terminal/terminal-deployer.py") `
    (Join-Path $projectRoot "deploy/terminal/maa-terminal-deployer.service") `
    (Join-Path $projectRoot "deploy/terminal/env.deployer.example") `
    (Join-Path $projectRoot "deploy/terminal/docker-compose.npu.yml") `
    "${remote}:${remoteDeployDir}/"
if ($LASTEXITCODE -ne 0) { throw "Unable to upload terminal deployment service." }

ssh $remote @"
set -e
cd '$TargetDir'
if test ! -f deploy/terminal/env.deployer; then
  cp deploy/terminal/env.deployer.example deploy/terminal/env.deployer
  chmod 600 deploy/terminal/env.deployer
  echo 'DEPLOYER_ENV_CREATED'
else
  echo 'DEPLOYER_ENV_EXISTS'
fi
chmod 755 deploy/terminal/terminal-deployer.py
cp deploy/terminal/maa-terminal-deployer.service /etc/systemd/system/maa-terminal-deployer.service
systemctl daemon-reload
systemctl enable maa-terminal-deployer
if grep -q 'replace-with-the-platform-AGENT_TOKEN' deploy/terminal/env.deployer; then
  echo 'DEPLOYER_TOKEN_REQUIRED'
else
  systemctl restart maa-terminal-deployer
  systemctl --no-pager --full status maa-terminal-deployer
fi
"@
if ($LASTEXITCODE -ne 0) { throw "Unable to install terminal deployment service." }

Write-Host "Terminal deployment service files installed." -ForegroundColor Green
Write-Host "If DEPLOYER_TOKEN_REQUIRED was reported, edit on the board:" -ForegroundColor Yellow
Write-Host "  $TargetDir/deploy/terminal/env.deployer" -ForegroundColor Yellow
Write-Host "Set DEPLOYER_TOKEN to the platform AGENT_TOKEN, then run:" -ForegroundColor Yellow
Write-Host "  systemctl restart maa-terminal-deployer" -ForegroundColor Yellow
Write-Host "After that, the platform terminal card will show '部署 NPU 容器'." -ForegroundColor Green
