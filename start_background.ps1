# 后台启动 Bot：关掉当前 PowerShell 窗口后仍继续运行
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$pidFile = Join-Path $PSScriptRoot "bot.pid"
$logFile = Join-Path $PSScriptRoot "bot.log"

if (Test-Path $pidFile) {
    $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "Bot 已在运行，PID=$oldPid"
        exit 0
    }
}

$proc = Start-Process -FilePath $python `
    -ArgumentList "bot.py" `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError $logFile `
    -PassThru

$proc.Id | Set-Content $pidFile
Write-Host "Bot 已在后台启动，PID=$($proc.Id)"
Write-Host "日志: $logFile"
Write-Host "停止: 运行 .\stop_background.ps1"
