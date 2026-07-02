# 每日 AI 资讯流水线 — Windows 执行脚本
$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path (Join-Path $ProjectRoot "data") "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Date = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd")
$LogFile = Join-Path $LogDir "$Date.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

Log "=== AI News Daily Pipeline Start ==="

Set-Location $ProjectRoot

# 优先使用 venv
# venv lookup: check ai-news-pipeline/.venv then repo-root .venv
$VenvCandidates = @(
    (Join-Path (Join-Path (Join-Path $ProjectRoot ".venv") "Scripts") "python.exe"),
    (Join-Path (Join-Path (Join-Path (Split-Path -Parent $ProjectRoot) ".venv") "Scripts") "python.exe")
)
$Python = "python"
foreach ($candidate in $VenvCandidates) {
    if (Test-Path $candidate) {
        $Python = $candidate
        break
    }
}

Log "Python: $Python"
# Force UTF-8 for child processes (Windows console defaults to GBK)
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
Log "Collecting content for: $Date (yesterday)"

# Each step runs independently so a failure in one doesn't abort the rest.
$steps = @("collect", "process", "digest", "learn")
foreach ($step in $steps) {
    Log "--- Step: $step ---"
    try {
        & $Python run.py --date $Date $step 2>&1 | ForEach-Object { Log $_ }
        if ($LASTEXITCODE -ne 0) {
            Log "WARNING: step '$step' exited with code $LASTEXITCODE, continuing..."
        }
    } catch {
        Log "ERROR in step '$step': $($_.Exception.Message)"
    }
}

Log "=== Pipeline Done (date: $Date) ==="
Log "Check drafts at mp.weixin.qq.com"
