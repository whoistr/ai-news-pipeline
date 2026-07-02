#!/usr/bin/env bash
# 每日 AI 资讯流水线 — Linux (cron) 执行脚本
#
# 与 schedules/run-daily.ps1 (Windows) 保持一致:
#   采集 → 处理 → 生成日报卡片 (digest),推送到微信草稿箱
#
# crontab 示例:
#   0 8 * * * /path/to/ai-news-pipeline/schedules/run-daily.sh
#
# 说明:本脚本走 digest 线(每日卡片式日报,全自动)。
#   深度文章(plan → Cursor agent 写作)的功能保留,需手动执行:
#       python run.py daily          # collect + process + plan,产出 brief.md 等待人工接手

# 不使用 set -e:每步独立运行,单步失败不中断后续步骤(与 Windows 版一致)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_ROOT/data/logs"
mkdir -p "$LOG_DIR"

# 与 Windows 版一致:采集昨天的内容做日报(GNU date;macOS 的 BSD date 需自行替换)
DATE="$(date -d yesterday +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/$DATE.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=== AI News Daily Pipeline Start ==="
cd "$PROJECT_ROOT"

# 优先使用 venv
if [[ -f .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi
# Force UTF-8 for child processes(与 Windows 版 PYTHONIOENCODING 对齐)
export PYTHONIOENCODING=utf-8

log "Python: $PYTHON"
log "Collecting content for: $DATE (yesterday)"

# 与 Windows 版一致:分步独立运行,单步失败不中断后续步骤。
# 用 PIPESTATUS[0] 准确取 python 退出码(管道末尾是 tee,$? 会拿到 tee 的退出码)。
for step in collect process digest; do
  log "--- Step: $step ---"
  "$PYTHON" run.py --date "$DATE" "$step" 2>&1 | tee -a "$LOG_FILE"
  code=${PIPESTATUS[0]}
  if [[ $code -ne 0 ]]; then
    log "WARNING: step '$step' exited with code $code, continuing..."
  fi
done

log "=== Pipeline Done (date: $DATE) ==="
log "Check drafts at mp.weixin.qq.com"
