# Linux 200 实验室一键部署（PowerShell，仓库根目录执行）
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Get-Content "$PSScriptRoot\lab.env" | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $n, $v = $_ -split '=', 2
    Set-Item -Path "env:$($n.Trim())" -Value $v.Trim()
}

python "$PSScriptRoot\deploy.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python "$PSScriptRoot\reconcile.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python "$PSScriptRoot\verify.py"
