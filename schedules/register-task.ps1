# Windows 任务计划程序 — 每日 AI 资讯流水线
#
# 以管理员身份运行 PowerShell，执行：
#   .\schedules\register-task.ps1
#
# 或在「任务计划程序」中手动创建：
#   程序: powershell.exe
#   参数: -ExecutionPolicy Bypass -File "E:\project\PersonalKnowledgeBase\ai-news-pipeline\schedules\run-daily.ps1"
#   触发器: 每天 08:00

param(
    [string]$TaskName = "AI-News-Daily-Pipeline",
    [string]$RunAt = "08:00"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $ScriptDir "run-daily.ps1"
$ProjectRoot = Split-Path -Parent $ScriptDir

if (-not (Test-Path $RunScript)) {
    Write-Error "找不到 run-daily.ps1: $RunScript"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`"" `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $RunAt

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Description "每日采集 AI 资讯、处理、生成 brief，供 wechat-publisher 写作发布" `
    -Force

Write-Host "已注册计划任务: $TaskName (每天 $RunAt)"
Write-Host "手动测试: Start-ScheduledTask -TaskName '$TaskName'"
