param(
    [ValidateSet('Status','Validate','Start','Stop')][string]$Action='Status',
    [ValidateSet('qq','discord')][string]$Alias='qq'
)
$ErrorActionPreference='Stop'
$deployment=Split-Path $PSScriptRoot -Parent
$botDirectory=Join-Path $deployment 'bots'
$botEnv=if($Alias -eq 'qq'){Join-Path $botDirectory '.env'}else{Join-Path $deployment '.env'}
$arguments=@('compose','-p','al1s-bot','--env-file',$botEnv,
    '--env-file',(Join-Path $botDirectory 'meme-runtime.env'),
    '--env-file',(Join-Path $botDirectory 'bot-images.env'),
    '-f',(Join-Path $botDirectory 'compose.yaml'))
if($Alias -eq 'qq'){$arguments+=@('-f',(Join-Path $botDirectory 'compose.login-local.yaml'))}
$service=if($Alias -eq 'qq'){'llbot'}else{'discord-worker'}
switch($Action){
    'Status' {$arguments+=@('ps',$service)}
    'Validate' {$arguments+=@('config','--quiet')}
    'Start' {$arguments+=@('up','-d','--no-build','--no-deps','--pull','never',$service)}
    'Stop' {$arguments+=@('stop','-t','30',$service)}
}
& docker @arguments
if($LASTEXITCODE -ne 0){throw 'Bot management command failed'}
