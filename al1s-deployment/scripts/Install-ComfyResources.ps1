param(
    [Parameter(Mandatory = $true)]
    [string]$ComfyRoot
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $ComfyRoot).Path
$customNodes = Join-Path $root 'custom_nodes'
$source = Join-Path $PSScriptRoot '..\comfy-resources\al1s_resources'
$target = Join-Path $customNodes 'al1s_resources'
$files = @('__init__.py', 'bootstrap.py', 'cache.py', 'guard.py', 'inspection.py', 'moss_nodes.py',
           'nodes.py', 'registry.py', 'routes.py', 'safety.py', 'scheduler.py')
$core = @{
    'main.py' = 'baaa16d69ee78e08c490eeec20bf27c56b746c070f76cc05b4aa13cbc616bcef'
    'server.py' = '52618f91c663dc6524a028162212e0434fc9239618ddc0efbf7c605027081c8c'
    'nodes.py' = '776f549a05fc068eca20a9edc1602c6f1b80cfbbf5153bc44ba371a55fbb13be'
    'execution.py' = '25b12805b420804d634dcb505ff5d2e2c485735557dbcfb3f7f5fd093fff1505'
    'comfy\model_management.py' = '63610e41cf06fccb5eeaa153c5fd3799e01a0c645facba844763781d2036989a'
    'comfy_execution\caching.py' = 'f71d43f6196330e73674e13795a9d392a95750ad55be2c9d96cf54355794b862'
    'comfy_extras\nodes_upscale_model.py' = '9c1332b15585c3f88291639bedf32d51c3bf3b2e8d9347018e7df49d92382531'
    'comfy_extras\nodes_flux.py' = 'e6fe6d10e7b538c6bb8d1edbba6688242e2f9b0cb69b9a06bfbde3c4e51d14e6'
    'comfy_extras\nodes_qwen.py' = '61a5e27f90bb786cd4f6e76ee86920582ffef2ee3e65cde7b2278c7171c559e8'
    'comfy_extras\nodes_slg.py' = '745270f54ecfe86526727ec204a9955aa2791e9deca2520d7cf8bb83fe6fca50'
    'comfy_extras\nodes_eps.py' = 'db27a4586856a4f64f221128649f4c9e4094b89e490c25587623086ecb618b9d'
}

foreach ($relative in $core.Keys) {
    $file = Get-Item -LiteralPath (Join-Path $root $relative)
    if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint -or
        (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $core[$relative]) {
        throw 'ComfyUI core differs from the reviewed version.'
    }
}
$custom = Get-Item -LiteralPath $customNodes
if (!$custom.PSIsContainer -or $custom.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'ComfyUI custom_nodes is not a regular directory.'
}
if (Test-Path -LiteralPath $target) {
    throw 'The AL1S resource extension already exists; leave it untouched and inspect it before updating.'
}
foreach ($name in $files) {
    $file = Get-Item -LiteralPath (Join-Path $source $name)
    if ($file.PSIsContainer -or $file.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Resource extension source is not a regular file.'
    }
}

New-Item -ItemType Directory -Path $target | Out-Null
foreach ($name in $files) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $target $name)
}
$tokenPath = Join-Path $target 'resources.token'
$stream = [IO.File]::Open($tokenPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
$stream.Dispose()
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User
$system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
foreach ($principal in @($user, $system)) {
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($principal, 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $tokenPath -AclObject $acl
$bytes = New-Object byte[] 48
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
[IO.File]::WriteAllText($tokenPath, $token, [Text.Encoding]::ASCII)
Write-Output 'AL1S resource extension installed. Restart the existing ComfyUI to load it.'
