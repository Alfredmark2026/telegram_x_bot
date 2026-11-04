# 停止后台 Bot
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$pidFile = Join-Path $PSScriptRoot "bot.pid"
if (-not (Test-Path $pidFile)) {
    Write-Host "未找到 bot.pid，Bot 可能未在后台运行"
    exit 0
}

$oldPid = (Get-Content $pidFile).Trim()
if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
    Stop-Process -Id $oldPid -Force
    Write-Host "已停止 Bot，PID=$oldPid"
} else {
    Write-Host "进程不存在，清理 pid 文件"
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
