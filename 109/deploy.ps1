# 现网 109 一键部署（PowerShell，仓库根目录执行）
# 部署前：复制 109\env.example → 109\env 并填写 MTR_OP_SSH_PASSWORD
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$envFile = Join-Path $PSScriptRoot "env"
if (-not (Test-Path $envFile)) {
    Write-Error "缺少 109\env — 请复制 env.example 并填写密码"
}
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $n, $v = $_ -split '=', 2
    Set-Item -Path "env:$($n.Trim())" -Value $v.Trim()
}

python "$PSScriptRoot\deploy_fresh.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python "$PSScriptRoot\verify.py"
exit $LASTEXITCODE
