#!/bin/bash
################################################################################
# Alpha Hive 每日定时扫描启动脚本
# 用于 Cron 定时任务
# 功能: 运行蜂群扫描 + 自动推送 Slack
################################################################################

# 明确指定 Python 3.11（cron 的 PATH 只有 /usr/bin，会找到系统 3.9）
PYTHON="/usr/local/bin/python3"

# 超时保护：最多运行 10 分钟（防止进程永久挂起）
TIMEOUT_SECONDS=600

# 设置脚本目录
SCRIPT_DIR="/Users/igg/.claude/reports"
LOG_DIR="/Users/igg/.claude/logs"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOG_FILE="$LOG_DIR/alpha_hive_$TIMESTAMP.log"

# 创建日志目录
mkdir -p "$LOG_DIR"

# 记录开始时间
{
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Alpha Hive 每日扫描 - $TIMESTAMP"
echo "Python: $($PYTHON --version 2>&1)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "启动蜂群扫描..."
echo "时间: $(date '+%Y-%m-%d %H:%M:%S') | 超时: ${TIMEOUT_SECONDS}s"
} >> "$LOG_FILE"

# 切换到脚本目录
cd "$SCRIPT_DIR" || exit 1

# 运行扫描（独立 Python 脚本 + 后台 watchdog 超时保护）
"$PYTHON" -u "$SCRIPT_DIR/run_daily_scan.py" >> "$LOG_FILE" 2>&1 &
SCAN_PID=$!

# 超时 watchdog
( sleep "$TIMEOUT_SECONDS" && kill -9 "$SCAN_PID" 2>/dev/null ) &
WATCHDOG_PID=$!

# 等待扫描完成
wait "$SCAN_PID" 2>/dev/null
PYTHON_EXIT=$?

# 杀掉 watchdog（正常完成不再需要）
kill "$WATCHDOG_PID" 2>/dev/null
wait "$WATCHDOG_PID" 2>/dev/null

# 超时检测（exit code 137 = killed by SIGKILL）
if [ $PYTHON_EXIT -eq 137 ]; then
    echo "TIMEOUT: 进程超过 ${TIMEOUT_SECONDS}s 被强制终止" >> "$LOG_FILE"
    "$PYTHON" -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from slack_report_notifier import SlackReportNotifier
n = SlackReportNotifier()
if n.enabled:
    n.send_risk_alert('Alpha Hive 扫描超时', '进程超过 ${TIMEOUT_SECONDS}s 被强制终止\n日志: $LOG_FILE', 'CRITICAL')
" 2>/dev/null
fi

# 记录结束时间
{
echo ""
echo "完成时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "退出状态: $PYTHON_EXIT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
} >> "$LOG_FILE"

# 清理旧日志（保留最近 30 天的日志）
find "$LOG_DIR" -name "alpha_hive_*.log" -mtime +30 -delete

exit $PYTHON_EXIT
