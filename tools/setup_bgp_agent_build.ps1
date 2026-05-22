# 一次性准备 Windows 本地编译 bgp_agent（WSL + Ubuntu）
# 需管理员 PowerShell；安装后通常需重启一次。
# 用法: powershell -ExecutionPolicy Bypass -File tools\setup_bgp_agent_build.ps1

$ErrorActionPreference = "Stop"

Write-Host "=== 安装 WSL 2 + Ubuntu 24.04 ===" -ForegroundColor Cyan
winget install -e --id Microsoft.WSL --accept-package-agreements --accept-source-agreements
winget install -e --id Canonical.Ubuntu.2404 --source winget --accept-package-agreements --accept-source-agreements

wsl --update
wsl --set-default-version 2
wsl --install -d Ubuntu-24.04

Write-Host ""
Write-Host "若提示启用「虚拟机平台」或重启，请重启 Windows 后执行:" -ForegroundColor Yellow
Write-Host '  $env:MTR_BGP_AGENT_BUILD_REMOTE="0"'
Write-Host '  $env:MTR_BGP_AGENT_WSL_DISTRO="Ubuntu-24.04"'
Write-Host '  $env:MTR_BGP_AGENT_WSL_USER="root"'
Write-Host '  python tools/bgp_agent_build.py'
Write-Host ""
Write-Host "有 Docker 时可跳过 WSL:" -ForegroundColor Yellow
Write-Host '  python tools/bgp_agent_build.py --docker'
