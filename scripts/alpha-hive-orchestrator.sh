#!/bin/bash

# ================================================================
# 🐝 Alpha Hive 自动化编排脚本（Orchestrator）
# 唯一的 Cron 入口点，协调所有自动化流程
# 版本：2.0 | 作者：Claude Code | 激活日期：2026-02-24
# ================================================================

set -uo pipefail
# 注意：不使用 set -e，因为各 Step 失败时应继续执行后续步骤

# ================================================================
# 进程锁（防止 LaunchAgent 或手动重复执行）
# mkdir 是原子操作，比 flock 更跨平台（macOS 无 flock 命令）
# ================================================================
LOCKDIR="/tmp/alpha_hive_orchestrator.lock"
_cleanup_lock() { rm -rf "$LOCKDIR" 2>/dev/null; }  # rm -rf：锁目录含 pid 文件，rmdir 无法删非空目录
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    # 检查持有锁的进程是否还活着（防止僵尸锁）
    if [ -f "$LOCKDIR/pid" ]; then
        OLD_PID=$(cat "$LOCKDIR/pid" 2>/dev/null)
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] 🔒 另一个编排实例正在运行 (PID $OLD_PID)，退出" >&2
            exit 0
        fi
        # 旧进程已死，清理僵尸锁
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] 🔓 清理僵尸锁 (PID $OLD_PID 已退出)" >&2
        _cleanup_lock
        mkdir "$LOCKDIR" 2>/dev/null || { echo "无法获取锁" >&2; exit 0; }
    else
        # 无 PID 文件但锁目录存在（异常情况），清理后重试
        _cleanup_lock
        mkdir "$LOCKDIR" 2>/dev/null || { echo "无法获取锁" >&2; exit 0; }
    fi
fi
echo $$ > "$LOCKDIR/pid"
trap '_cleanup_lock' EXIT

# ================================================================
# 配置
# ================================================================
SCRIPTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="/Users/igg/Desktop/Alpha Hive"   # Python 源码目录（代码检出，git 仓库）
# 数据根迁移阶段 5（v0.45.322 起）：生产数据在 DATA_DIR，不在代码检出里。
# export 给本脚本拉起的每个 Python 步骤——hive_logger.PATHS.home 读它；git 仓库根
# (PATHS.git_repo_root) 不读它，仍按 __file__ 落在 PROJECT_DIR。回退 = 删掉这两行
# 并把下面 8 处 $DATA_DIR 改回 $PROJECT_DIR（先跑 migrate_data_root.py unretire）。
DATA_DIR="/Users/igg/alpha-hive-data"
export ALPHA_HIVE_HOME="$DATA_DIR"
LOGDIR="/Users/igg/.claude/logs"
REPORTDIR="/Users/igg/.claude/reports"         # 状态/输出文件目录
TIMESTAMP=$(date +"%Y-%m-%d_%H%M%S")
DATE_STR=$(date +"%Y-%m-%d")
LOGFILE="$LOGDIR/orchestrator-$DATE_STR.log"

# 提升文件描述符上限（防止 Too many open files 导致 save_report/gh-pages 部署失败）
ulimit -n 4096 2>/dev/null || ulimit -n 2048 2>/dev/null || true

# 显式 Python 路径（避免 cron 环境使用 CommandLineTools python3 无 FDA）
PYTHON3="/usr/local/bin/python3"
if [ ! -x "$PYTHON3" ]; then
    PYTHON3="$(which python3 2>/dev/null || echo /usr/bin/python3)"
fi

# 默认标的（可通过 $1 参数覆盖）
# v0.42.9 扩池：10 → 30 只。原 10 只全是高相关科技股（平均两两相关 0.230），
# 有效独立标的数 N_eff 仅 3.25 —— 这是横截面 IC 统计功效的真正瓶颈。
# 新增 20 只按"最大化 N_eff"贪心选出，优先低相关行业（Energy/Communication/
# Consumer 与核心池相关分别为 −0.180 / −0.007 / +0.048），N_eff → 13.8。
# 成本（v0.45.89 更新）：上面 v0.42.9 那条「71 + 4.9×标的数 秒，30 只约 219s，
# 占 STEP2_TIMEOUT 12%」已作废——那是限流时代的数，429 秒回拒绝所以显得快。
# 2026-08-31 实测：规则模式 20 只跑满 1800s = **90s/只**（CBOE 全链 OI 主导），
# 30 只需 ≈2700s。STEP2_TIMEOUT 已改为按只数算（见下方 Step 2 处），不再是常数。
# 注意 Step 2 的 ML 报告已限流到前 12 名（ALPHA_HIVE_ML_REPORT_MAX），
# 故取价调用量不随标的数线性增长。
# v0.45.6：名单真相源移到 config.WATCHLIST，这里只读不存。
# 此前两边各维护一份、内容早已漂移——config 里 24 只、这里 30 只、重合仅 13 只，
# 且 config 里有 11 只从未被扫过。改 config 以为生效，扫描其实纹丝不动。
# 兜底：config 读不出来（语法错/import 失败）时退回下面硬编码的 30 只，
# 扫描绝不能因为配置问题起不来。
DEFAULT_TICKERS_FALLBACK=(
    "NVDA" "TSLA" "MSFT" "QCOM" "VKTX" "META" "BILI" "AMZN" "RKLB" "CRCL"
    "CVX" "VZ" "JNJ" "XOM" "COST" "BRK-B" "AMC" "ABBV" "T" "DELL"
    "DE" "CRM" "MU" "WMT" "TMO" "TMUS" "ENPH" "NFLX" "NEE" "SNOW"
)
_WL_RAW="$(cd "$PROJECT_DIR" && "$PYTHON3" -c 'from config import WATCHLIST; print(" ".join(WATCHLIST))' 2>/dev/null)"
# 只认"非空 且 全部是合法 ticker 形状"的结果，否则视同读取失败。
# 半截输出（例如 import 时打了日志）比读不出来更危险——会静默缩小扫描池。
if [ -n "$_WL_RAW" ] && [ -z "$(printf '%s' "$_WL_RAW" | tr ' ' '\n' | grep -vE '^[A-Z][A-Z0-9.-]{0,6}$')" ]; then
    IFS=' ' read -ra DEFAULT_TICKERS <<< "$_WL_RAW"
else
    DEFAULT_TICKERS=("${DEFAULT_TICKERS_FALLBACK[@]}")
    echo "[WARN] 无法从 config.WATCHLIST 读取标的名单，退回内置 ${#DEFAULT_TICKERS[@]} 只" >&2
fi
unset _WL_RAW
TICKERS_INPUT="${1:-}"

# ================================================================
# 初始化
# ================================================================
mkdir -p "$LOGDIR" "$REPORTDIR"

# ================================================================
# 全局超时看门狗（90 分钟硬限制 — 防止 launchd 永远不触发下一次）
# ================================================================
ORCHESTRATOR_TIMEOUT=90000  # 25h（兼容 pre_scan_notify 最长等 24h 用户确认）
(
    sleep $ORCHESTRATOR_TIMEOUT
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] 🔴 全局超时！编排器运行超过 ${ORCHESTRATOR_TIMEOUT}s，强制退出（PID $$）" >> "$LOGDIR/orchestrator-$(date +%Y-%m-%d).log"
    kill -TERM $$ 2>/dev/null
    sleep 15
    kill -9 $$ 2>/dev/null
) &
_GLOBAL_WATCHDOG_PID=$!

# 日志函数
log() {
    local level=$1
    shift
    local message="$@"
    local log_time=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$log_time] [$level] $message" | tee -a "$LOGFILE"
}

# 整体状态：**只升不降**（v0.45.66）
# 此前是 9 处直接赋值，谁最后跑谁说了算 —— 而「最后跑」和「最严重」没有关系。
# 实证 2026-08-28：Step 2 超时导致零产出（failed），**随后 Step 3 也超时
# 把它盖成 partial** —— 于是 alert_manager 的 CRITICAL P0 永远不会响。
# 这个 bug 是 v0.45.66 二次检查时发现的：failed 是本版新引入的更高档位，
# 而旧代码里所有 partial 赋值都是无条件的，两者相遇必然出事。
_status_rank() {
    case "$1" in
        success) echo 0 ;;
        partial) echo 1 ;;
        failed)  echo 2 ;;
        *)       echo 1 ;;   # 未知值按 partial 计，宁可高估不低估
    esac
}
set_status() {
    # 先归一化：未知值原样存进去会让 status.json 出现 alert_manager 不认识的
    # 字符串（它只判 'failed'），等于把一次告警悄悄丢掉。
    # 注释说「按 partial 计」，那就真的要存 partial —— 二次检查时发现
    # 这里存的是原值，注释与行为不符，是同一类标签谎报。
    local _new="$1"
    case "$_new" in
        success|partial|failed) ;;
        *) log "WARN" "set_status 收到未知状态「${_new}」，按 partial 计"; _new="partial" ;;
    esac
    if [ "$(_status_rank "$_new")" -gt "$(_status_rank "$OVERALL_STATUS")" ]; then
        OVERALL_STATUS="$_new"
    fi
}

# 运行脚本（带超时 + 存在性检查 + TCC 权限检测）
# 用法：run_step [--timeout SECONDS] script [args...]
# 返回码：0=成功 | 1=失败 | 2=跳过（脚本不存在）| 124=超时
run_step() {
    local timeout=0
    if [ "$1" = "--timeout" ]; then
        timeout="$2"
        shift 2
    fi
    local script="$1"
    shift
    if [ ! -f "$script" ]; then
        log "WARN" "⏭️  脚本不存在，跳过：$script"
        return 2  # 返回 2 = 跳过（区别于 0=成功 / 1=失败）
    fi

    local rc=0
    if [ "$timeout" -gt 0 ]; then
        # 后台运行 Python，设置超时看门狗
        "$PYTHON3" "$script" "$@" &
        local child_pid=$!
        (
            sleep "$timeout"
            if kill -0 "$child_pid" 2>/dev/null; then
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] ⏰ 超时！$(basename "$script") 超过 ${timeout}s，正在终止 (PID $child_pid)..." >> "$LOGFILE"
                # 先温柔 TERM，等 10s，再暴力 KILL
                kill -TERM "$child_pid" 2>/dev/null
                sleep 10
                kill -9 "$child_pid" 2>/dev/null
            fi
        ) &
        local watchdog_pid=$!
        wait "$child_pid" 2>/dev/null
        rc=$?
        # 清理看门狗（Python 正常退出时取消计时器）
        kill "$watchdog_pid" 2>/dev/null
        wait "$watchdog_pid" 2>/dev/null
        # SIGTERM(143) / SIGKILL(137) → 视为超时，返回 124（与 GNU timeout 一致）
        if [ $rc -eq 143 ] || [ $rc -eq 137 ]; then
            return 124
        fi
    else
        "$PYTHON3" "$script" "$@"
        rc=$?
    fi

    # 检测 macOS TCC 权限拒绝（Operation not permitted）
    if [ $rc -ne 0 ] && [ $rc -ne 124 ]; then
        "$PYTHON3" -c "open('$script').close()" 2>/dev/null
        if [ $? -ne 0 ]; then
            log "ERROR" "❌ macOS 权限被拒绝！请授予 Full Disk Access："
            log "ERROR" "   系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加 cron 和 $PYTHON3"
            log "ERROR" "   或将项目移出 Desktop 文件夹"
            return 1
        fi
    fi
    return $rc
}

# ================================================================
# 主流程
# ================================================================
log "INFO" "🐝 Alpha Hive 自动编排启动"
log "INFO" "📅 日期：$DATE_STR"
log "INFO" "⏰ 时间：$TIMESTAMP"
log "INFO" "📂 项目目录：$PROJECT_DIR"

# ================================================================
# 出站代理（v0.45.65，2026-08-29）
# ================================================================
# 为什么需要：**launchd 不继承登录 shell 的环境变量**，plist 的
# EnvironmentVariables 此前只有 PATH。于是定时扫描一直走直连，
# 而用户交互环境走本机代理 —— 两条完全不同的链路，此前一直没人发现。
#
# 实测 2026-08-29 同一台机器同一时刻：
#     带代理：CBOE 200  /  yfinance 200
#     直连  ：CBOE 200  /  yfinance **403 Forbidden**
# 这也解释了 8/25~8/27 每天 364 → 487 → 690 次 429 递增：
# 直连出口被 Yahoo 按 IP 掐，而手工排查时走代理，怎么测都是好的。
#
# 为什么探活、而不是写死进 plist：写死会把故障模式变成
# 「代理没开 → 整轮全灭」，比现在「有时直连能过」更集中。
# 探活失败就退回直连，且**必须写进日志**——这条链路的存在与否
# 会直接改变 429 计数的含义，不能静默。
PROXY_URL="${ALPHA_HIVE_PROXY:-http://127.0.0.1:7897}"
_proxy_ok=0
if [ "$PROXY_URL" != "none" ]; then
    # 只探端口不够——端口开着但代理转发是坏的，会把整轮拖死。
    # 这里连一次真实 HTTPS（CBOE 根域，几百字节），8 秒超时。
    if "$PYTHON3" -c '
import os, sys, socket, urllib.request
url = sys.argv[1]
host = url.split("//")[-1].split("/")[0]
h, _, prt = host.partition(":")
s = socket.socket(); s.settimeout(3)
try:
    s.connect((h, int(prt or 80)))
except Exception:
    sys.exit(1)
finally:
    s.close()
op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))
try:
    op.open(urllib.request.Request("https://cdn.cboe.com/",
            headers={"User-Agent": "alpha-hive/1.0"}), timeout=8)
except urllib.error.HTTPError:
    sys.exit(0)          # 4xx/5xx 说明隧道通了，目标站怎么答不重要
except Exception:
    sys.exit(1)
sys.exit(0)
' "$PROXY_URL" 2>/dev/null; then
        _proxy_ok=1
    fi
fi
if [ "$_proxy_ok" = "1" ]; then
    export HTTP_PROXY="$PROXY_URL"  HTTPS_PROXY="$PROXY_URL"
    export http_proxy="$PROXY_URL"  https_proxy="$PROXY_URL"
    export NO_PROXY="localhost,127.0.0.1,::1,.local"
    export no_proxy="$NO_PROXY"
    log "INFO" "🌐 出站走代理 ${PROXY_URL}（探活通过：端口可连 + HTTPS 隧道可用）"
else
    # ⚠️ 必须真的清掉，不能只打日志。
    # launchd 下本来就没有这些变量，但**手工从终端跑**时 shell 里是有的 ——
    # 那样日志会写「退回直连」而实际仍在走代理，两者不一致。
    # 这正是本项目反复出现的「标签宣称的事没发生」，不能在治它的代码里再犯一次。
    unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    if [ "$PROXY_URL" = "none" ]; then
        log "INFO" "🌐 出站直连（ALPHA_HIVE_PROXY=none，显式关闭，非故障）"
    else
        log "WARN" "⚠️ 代理 $PROXY_URL 探活失败 —— 本轮退回直连。"
        log "WARN" "   直连下 yfinance 历来 403/429（rv_30d、催化剂受影响），"
        log "WARN" "   期权链走 CBOE 不受影响。若 429 计数暴涨，先查代理是否在跑。"
    fi
fi
unset _proxy_ok

# 验证项目目录存在
if [ ! -d "$PROJECT_DIR" ]; then
    log "ERROR" "❌ 项目目录不存在：$PROJECT_DIR"
    exit 1
fi

# 验证核心脚本（Step 2 是必须的）
if [ ! -f "$PROJECT_DIR/alpha_hive_daily_report.py" ]; then
    log "ERROR" "❌ 核心脚本不存在：$PROJECT_DIR/alpha_hive_daily_report.py"
    exit 1
fi
log "INFO" "✅ 项目目录验证通过"
log "INFO" "🐍 Python: $PYTHON3 ($($PYTHON3 --version 2>&1))"

# ── TCC 预检：验证 python3 能访问 Desktop 项目文件 ──
if ! "$PYTHON3" -c "open('$PROJECT_DIR/alpha_hive_daily_report.py').close()" 2>/dev/null; then
    log "ERROR" "══════════════════════════════════════════════════════"
    log "ERROR" "❌ macOS 安全限制：python3 无法访问 Desktop 文件夹！"
    log "ERROR" ""
    log "ERROR" "修复方法（任选其一）："
    log "ERROR" "  方法 1：系统设置 → 隐私与安全性 → 完全磁盘访问权限"
    log "ERROR" "          → 添加 /usr/sbin/cron"
    log "ERROR" "  方法 2：将项目从 Desktop 移到 ~/alpha-hive-project"
    log "ERROR" "══════════════════════════════════════════════════════"
    # 写入 status.json 标记失败
    mkdir -p "$REPORTDIR"
    cat > "$REPORTDIR/status.json" << EOFJ
{
  "last_run": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "status": "failed_tcc_permission",
  "error": "macOS TCC blocked python3 from accessing Desktop. Grant Full Disk Access to /usr/sbin/cron",
  "logfile": "$LOGFILE"
}
EOFJ
    exit 1
fi
# v0.45.95：上面那条只证明了「$PYTHON3 能 open 一个已知文件名」。
# 它测不到真正出问题的那一维 —— **bash 能否枚举目录**（readdir）。
# TCC 对 ~/Desktop 恰好是允许 stat、拒绝 readdir，所以这条检查会在
# glob 全线失效的情况下照样打勾。补一条如实的探测，只告警不拦截
# （现已无消费者依赖 glob；留此告警是为了下次有人写 glob 时能看见）。
if [ "$(ls "$PROJECT_DIR"/*.py 2>/dev/null | wc -l | xargs)" -eq 0 ]; then
    log "WARN" "⚠️ bash 无法枚举 ${PROJECT_DIR}（TCC readdir 被拒）——"
    log "WARN" "   本脚本中任何 glob 都会静默返回空，请一律用 [ -f 精确路径 ]"
else
    log "INFO" "✅ Desktop 文件访问权限正常（stat + readdir 均可）"
fi

# ── 交易日检测：跳过周末和美股假日 ──
TRADING_DAY_SCRIPT="$PROJECT_DIR/is_trading_day.py"
if [ -f "$TRADING_DAY_SCRIPT" ]; then
    TRADING_DAY_MSG=$("$PYTHON3" "$TRADING_DAY_SCRIPT" 2>&1)
    TRADING_DAY_RC=$?
    if [ $TRADING_DAY_RC -eq 10 ]; then
        # 退出码 10 = 非交易日，安全跳过
        log "INFO" "📅 $TRADING_DAY_MSG"
        log "INFO" "🛑 今日非交易日，跳过所有扫描任务"
        cat > "$REPORTDIR/status.json" << EOFJ
{
  "last_run": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "status": "skipped_non_trading_day",
  "reason": "$TRADING_DAY_MSG",
  "logfile": "$LOGFILE"
}
EOFJ
        exit 0
    elif [ $TRADING_DAY_RC -ne 0 ]; then
        # 其他非零 = 脚本出错，不跳过，继续执行
        log "WARN" "⚠️ is_trading_day.py 异常（exit=${TRADING_DAY_RC}），按交易日继续执行"
    else
        log "INFO" "✅ $TRADING_DAY_MSG"
    fi
else
    log "WARN" "⚠️ is_trading_day.py 不存在，跳过交易日检测"
fi

# ================================================================
# 补跑闸（v0.45.34）—— 配合 LaunchAgent 的 RunAtLoad=true
# ================================================================
# 背景：plist 原为 RunAtLoad=false，关机错过 14:00 就永久漏掉那天。
# 实测 2026 年 W29/W32/W34 三周完全无扫描，日志里零条记录 = 机器没开。
# 而 IV Rank 的 63 天阈值按**扫描日**计，日覆盖率直接决定它何时可用。
#
# 本闸对定时触发与开机触发**统一生效**，无需区分来源：
#   · 今日已有产出        → 退出 0（幂等，防止每次登录重复全量扫描）
#   · 当前早于收盘后阈值  → 退出 0（**关键**：盘中跑会把盘中价当收盘价
#                            写进 predictions，正是已知的样本污染源）
#   · 否则                → 继续（这就是补跑）
CATCHUP_AFTER_HHMM="1330"   # 本机 PDT/PST；美股 13:00 PT 收盘，留 30 分钟落定
SWARM_MARKER="$DATA_DIR/.swarm_results_${DATE_STR}.json"

if [ -f "$SWARM_MARKER" ]; then
    log "INFO" "✅ 今日（${DATE_STR}）已有扫描产出，跳过 —— $(basename "$SWARM_MARKER")"
    cat > "$REPORTDIR/status.json" << EOFJ
{
  "last_run": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "status": "skipped_already_scanned",
  "reason": "$(basename "$SWARM_MARKER") 已存在",
  "logfile": "$LOGFILE"
}
EOFJ
    exit 0
fi

NOW_HHMM=$(date +"%H%M")
# ⚠️ 必须用 [ -lt ]（按十进制解析）。**不要**改成 (( ))：$NOW_HHMM 在 10 点前
# 带前导零（如 0905），算术上下文会当八进制，9 是非法八进制位 →
# bash: ((: 0905: value too great for base，闸直接失效。实测确认。
if [ "$NOW_HHMM" -lt "$CATCHUP_AFTER_HHMM" ]; then
    log "INFO" "⏳ 现在 ${NOW_HHMM} 早于收盘后阈值 ${CATCHUP_AFTER_HHMM}，跳过"
    log "INFO" "   （盘中跑会把盘中价写成收盘价污染样本；定时任务或稍后开机会补上）"
    cat > "$REPORTDIR/status.json" << EOFJ
{
  "last_run": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "status": "skipped_before_close",
  "reason": "now=${NOW_HHMM} < threshold=${CATCHUP_AFTER_HHMM}",
  "logfile": "$LOGFILE"
}
EOFJ
    exit 0
fi

log "INFO" "🔁 今日尚无产出且已过 ${CATCHUP_AFTER_HHMM}，执行扫描（定时或开机补跑）"

# v0.45.214：扫描前把生产 checkout 只快进到 origin/main（production_sync.py）。
# 必须在扫描（Step 1 起）之前——扫描本身只跑一个代码版本，code_version 记的就是真跑的那版。
# ⚠️ 不是「任何 Python 之前」：同步前已有两处用旧代码——第 80 行读 config.WATCHLIST（下面重读），
#    交易日/补跑闸（不重跑：只在当天日历改动恰好翻转「今天跑不跑」时有差别）。
# 世代边界按日期划分，默认「代码落地当天就在跑」，不同步就会把旧代码的样本记进新世代。
# 做不到快进（工作区改动撞上 / 分叉 / 不在 main / 取不到远端）就沿用现有代码照常扫描，
# 结局写进 logs/production_sync.json → scan_timing → status.json → Step 6 告警。
# 走 $PYTHON3 而不是在 bash 里直接 git：bash 对本目录有 TCC 限制（见上方预检）。
if [ -f "$PROJECT_DIR/production_sync.py" ]; then
    if ! "$PYTHON3" "$PROJECT_DIR/production_sync.py" --date "$DATE_STR" >> "$LOGFILE" 2>&1; then
        log "WARN" "⚠️ 生产代码未快进到 origin/main，本轮沿用现有代码（结局见 status.json 的 scan_timing.production_sync）"
    fi
    # 第 80 行在同步之前用旧代码读过 config.WATCHLIST ⇒ 按同步后的代码重读（同一校验）。
    # 读不出来就保留第 80 行的结果（它已兜过底），只打 WARN——新代码连 config 都 import 不了，扫描大概率也会失败。
    _WL_RAW="$(cd "$PROJECT_DIR" && "$PYTHON3" -c 'from config import WATCHLIST; print(" ".join(WATCHLIST))' 2>/dev/null)"
    if [ -n "$_WL_RAW" ] && [ -z "$(printf '%s' "$_WL_RAW" | tr ' ' '\n' | grep -vE '^[A-Z][A-Z0-9.-]{0,6}$')" ]; then
        IFS=' ' read -ra DEFAULT_TICKERS <<< "$_WL_RAW"
    else
        log "WARN" "⚠️ 同步后重读 config.WATCHLIST 失败，沿用同步前读到的 ${#DEFAULT_TICKERS[@]} 只"
    fi
    unset _WL_RAW
else
    log "WARN" "⚠️ 生产 checkout 里没有 production_sync.py（v0.45.214 尚未进生产），跳过扫描前快进"
fi


log "INFO" "=" >> "$LOGFILE" && echo "=" >> "$LOGFILE"

# 解析标的列表
if [ -z "$TICKERS_INPUT" ]; then
    TICKERS=("${DEFAULT_TICKERS[@]}")
    log "INFO" "📌 使用默认标的：${TICKERS[*]}"
else
    # 将输入字符串转换为数组
    IFS=' ' read -ra TICKERS <<< "$TICKERS_INPUT"
    log "INFO" "📌 使用自定义标的：${TICKERS[*]}"
fi

# 初始化步骤结果
STEPS_RESULT='{}'
OVERALL_STATUS="success"

# ================================================================
# Step 1: 数据采集 (Data Fetcher)
# ================================================================
log "INFO" ""
log "INFO" "【Step 1/5】数据采集 - 启动"
STEP1_START=$(date +%s)

run_step --timeout 300 "$PROJECT_DIR/data_fetcher.py" >> "$LOGFILE" 2>&1
STEP1_RC=$?
STEP1_END=$(date +%s)
STEP1_DURATION=$((STEP1_END - STEP1_START))
if [ $STEP1_RC -eq 0 ]; then
    log "INFO" "✅ Step 1 成功（耗时 ${STEP1_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step1_data_fetcher\": {\"status\": \"success\", \"duration_seconds\": $STEP1_DURATION}}")
elif [ $STEP1_RC -eq 124 ]; then
    log "ERROR" "⏰ Step 1 超时（>300s），继续进行"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step1_data_fetcher\": {\"status\": \"timeout\", \"duration_seconds\": $STEP1_DURATION}}")
elif [ $STEP1_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 1 跳过（脚本不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step1_data_fetcher\": {\"status\": \"skipped\"}}")
else
    log "WARN" "⚠️ Step 1 失败，但继续进行（耗时 ${STEP1_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step1_data_fetcher\": {\"status\": \"failed\", \"duration_seconds\": $STEP1_DURATION}}")
fi

# ================================================================
# DB 备份：在蜂群分析前备份 pheromone.db（sqlite 在线备份 + 有下限的轮转）
# ================================================================
# v0.45.233（数据根迁移阶段 0.2）：cp → $PYTHON3 + sqlite 在线备份 API。旧写法三处毛病（2026-09-14 实测）：
#   1. launchd 下 bash 的 cp 被 TCC 拒（Operation not permitted）：09-09/10/11 连败，
#      v0.45.171 记 08-26 起 15 次仅 1 次成功。$PYTHON3 有权限——扫描本身就靠它写这个库。
#   2. pheromone.db 是 WAL 模式，cp 主文件会漏掉 -wal 里未回写的页
#      （09-14 实测：只拷主文件得到的 agent_memory 与真库内容不同，行数却相同——不对内容根本看不出来）。
#   3. `find -name "pheromone_*.db" -mtime +7 -delete` 不看备份成败；模式还会吞掉人工留存的
#      pheromone_pre_*.db（08-25/26/27 三份只剩孤儿 -wal/-shm）；而 launchd 下 bash 枚举不了本目录，
#      这条清理实际只在手工运行时生效——手工跑一次就可能把仅存的好备份清光。
# 现在：
#   - 源库只读打开：WAL 且有 -wal → mode=ro；WAL 但没有 -wal → mode=ro&immutable=1
#     （此时光 mode=ro 会在生产目录新建 -wal/-shm 且删不掉）；备份写 .partial，
#     integrity_check=ok 且逐表行数与同一读事务一致才改名为正式文件，并转 journal_mode=DELETE（单文件自洽）。
#   - 清理只在当日备份成功后才跑，只动 pheromone_YYYY-MM-DD.db（不碰 pheromone_pre_*），
#     删「超过 DB_BACKUP_KEEP_DAYS 天」的，但无论多旧都保留最新 DB_BACKUP_KEEP_MIN 份。
#   - 结局进日志 + status.json 的 steps_result.db_backup。只记录、不升级 OVERALL_STATUS、
#     不触发 Slack（CLAUDE.md「Slack 通知精简规则」只允许两类消息）。
#   - 整段不用 bash 碰本目录（连 [ -f ] 都交给 Python），避开 TCC。
#   - Python 走 heredoc 重定向到临时文件，**不要**改成 "$(... <<'EOF' ...)"：
#     /bin/bash 是 3.2.57，命令替换里的 heredoc 遇到括号/引号会解析错。
# >>> DB_BACKUP_BEGIN（测试按这两行标记抽取本段实跑，改动时保留标记）
DB_FILE="$DATA_DIR/pheromone.db"
BACKUP_DIR="$DATA_DIR/db_backups"
DB_BACKUP_KEEP_DAYS=7
DB_BACKUP_KEEP_MIN=7
_bk_tmp="$(mktemp -t alpha_hive_dbbackup.XXXXXX 2>/dev/null || echo "/tmp/alpha_hive_dbbackup.$$")"
"$PYTHON3" - "$DB_FILE" "$BACKUP_DIR" "$DATE_STR" "$DB_BACKUP_KEEP_DAYS" "$DB_BACKUP_KEEP_MIN" > "$_bk_tmp" 2>&1 <<'PY_DB_BACKUP'
import datetime as dt, os, re, sqlite3, sys, time
from pathlib import Path
from urllib.parse import quote

src, bdir, date_str = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
keep_days, keep_min = int(sys.argv[4]), int(sys.argv[5])


def fail(msg):
    print("FAIL|" + msg.replace("\n", " "))
    sys.exit(1)


t0 = time.time()
try:
    with open(src, "rb") as f:
        hdr = f.read(100)
except FileNotFoundError:
    fail("源库不存在：%s" % src)
except OSError as e:
    fail("读源库失败（TCC？）：%r" % e)
if hdr[:16] != b"SQLite format 3\x00":
    fail("源文件不是 SQLite：%s" % src)
if os.path.exists(src + "-journal"):
    fail("源库存在 hot journal（-journal），只读连接无法回滚，未备份")
if hdr[18] == 2 and not os.path.exists(src + "-wal"):
    uri, how = "file:%s?mode=ro&immutable=1" % quote(src), "immutable"
else:
    uri, how = "file:%s?mode=ro" % quote(src), "ro"
st0 = os.stat(src)

try:
    bdir.mkdir(parents=True, exist_ok=True)
except OSError as e:
    fail("建备份目录失败：%r" % e)
dst = bdir / ("pheromone_%s.db" % date_str)
tmp = bdir / (".pheromone_%s.db.partial" % date_str)


def table_counts(conn):
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {n: conn.execute('SELECT COUNT(*) FROM "%s"' % n.replace('"', '""')).fetchone()[0] for n in names}


try:
    if tmp.exists():
        tmp.unlink()
    s = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=60)
    try:
        s.execute("BEGIN")                       # 计数与备份看同一个读快照
        src_counts = table_counts(s)
        d = sqlite3.connect(str(tmp), isolation_level=None)
        try:
            s.backup(d)
            s.execute("COMMIT")
            d.execute("PRAGMA journal_mode=DELETE")
            integrity = [r[0] for r in d.execute("PRAGMA integrity_check").fetchall()]
            dst_counts = table_counts(d)
        finally:
            d.close()
    finally:
        s.close()
    if integrity != ["ok"]:
        raise RuntimeError("备份库 integrity_check 不是 ok：%s" % integrity[:3])
    if src_counts != dst_counts:
        raise RuntimeError("逐表行数不一致：%s" % {k: (src_counts.get(k), dst_counts.get(k))
                                                  for k in set(src_counts) | set(dst_counts)
                                                  if src_counts.get(k) != dst_counts.get(k)})
    if how == "immutable":
        st1 = os.stat(src)
        if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns) or os.path.exists(src + "-wal"):
            raise RuntimeError("immutable 读期间源库被写（有并发写者），备份作废")
    os.replace(tmp, dst)
except Exception as e:  # noqa: BLE001 —— 任何失败都要带原因出去，且不许轮转
    try:
        tmp.unlink()
    except OSError:
        pass
    fail("%s: %s" % (type(e).__name__, e))

print("OK|%s|%d bytes|%d tables|%d rows|open=%s|%.1fs" % (
    dst.name, dst.stat().st_size, len(dst_counts), sum(dst_counts.values()), how, time.time() - t0))

# 轮转：只到这里（当日备份已成功落盘）才会执行
try:
    today = dt.date.fromisoformat(date_str)
    daily = []
    for p in bdir.iterdir():
        m = re.fullmatch(r"pheromone_(\d{4}-\d{2}-\d{2})\.db", p.name)
        if m:
            try:
                daily.append((dt.date.fromisoformat(m.group(1)), p))
            except ValueError:
                pass
    daily.sort(reverse=True)
    pruned = 0
    for day, p in daily[keep_min:]:
        if (today - day).days > keep_days:
            for q in (p, Path(str(p) + "-wal"), Path(str(p) + "-shm")):
                if q.exists():
                    q.unlink()
            pruned += 1
            print("PRUNED|%s" % p.name)
    print("KEPT|%d" % (len(daily) - pruned))
except Exception as e:  # noqa: BLE001
    print("PRUNE_FAIL|%s: %s" % (type(e).__name__, e))
PY_DB_BACKUP
_bk_rc=$?
_bk_out="$(cat "$_bk_tmp" 2>/dev/null)"
rm -f "$_bk_tmp"
_bk_ok="$(printf '%s\n' "$_bk_out" | grep '^OK|' | head -1)"
if [ "$_bk_rc" -eq 0 ] && [ -n "$_bk_ok" ]; then
    _bk_pruned="$(printf '%s\n' "$_bk_out" | grep '^PRUNED|' | cut -d'|' -f2 | tr '\n' ' ')"
    _bk_kept="$(printf '%s\n' "$_bk_out" | grep '^KEPT|' | cut -d'|' -f2)"
    log "INFO" "💾 数据库已备份（sqlite 在线备份）→ db_backups/$(printf '%s' "${_bk_ok#OK|}" | tr '|' ' ')"
    log "INFO" "   轮转：保留 ${_bk_kept:-?} 份日备份；清理 ${_bk_pruned:-无}"
    _bk_prune_fail="$(printf '%s\n' "$_bk_out" | grep '^PRUNE_FAIL|' | cut -d'|' -f2-)"
    [ -n "$_bk_prune_fail" ] && log "WARN" "   旧备份清理失败（当日备份不受影响）：$_bk_prune_fail"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq --arg detail "${_bk_ok#OK|}" --arg pruned "$_bk_pruned" --arg prune_fail "$_bk_prune_fail" \
        '. + {"db_backup": {"status": "success", "detail": $detail, "pruned": $pruned, "prune_error": $prune_fail}}')
else
    _bk_reason="$(printf '%s\n' "$_bk_out" | grep '^FAIL|' | head -1 | cut -d'|' -f2-)"
    [ -z "$_bk_reason" ] && _bk_reason="$(printf '%s\n' "$_bk_out" | tail -3 | tr '\n' ' ')"
    log "WARN" "数据库备份失败（退出码 ${_bk_rc}），本轮未清理任何旧备份：${_bk_reason:-无输出}"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq --arg reason "${_bk_reason:-无输出}" --argjson rc "$_bk_rc" \
        '. + {"db_backup": {"status": "failed", "rc": $rc, "reason": $reason}}')
fi
unset _bk_tmp _bk_rc _bk_out _bk_ok _bk_pruned _bk_kept _bk_prune_fail _bk_reason
# <<< DB_BACKUP_END

# ================================================================
# Step 2: Alpha Hive 蜂群分析（执行前 Slack 确认 LLM 模式）
# ================================================================
log "INFO" ""
log "INFO" "【Step 2/5】蜂群分析 - 启动"

TICKERS_ARG="${TICKERS[*]}"
log "INFO" "   标的列表：${TICKERS_ARG}"

# ── 预通知：询问是否使用 LLM 模式 ──
# pre_scan_notify.py: 等待 10 分钟用户回复，超时自动用 --no-llm
LLM_FLAG="--no-llm"
PRE_SCAN_SCRIPT="$PROJECT_DIR/pre_scan_notify.py"

# ── 防重复：如果今天 LLM 模式已成功运行过，降级为 --no-llm ──
LLM_DONE_FILE="/tmp/alpha_hive_llm_done_${DATE_STR}"
if [ -f "$LLM_DONE_FILE" ]; then
    log "INFO" "⚠️ 今日 LLM 模式已成功运行过，本次降级为 --no-llm（防止重复计费）"
    log "INFO" "   如需强制重跑 LLM，请先删除 $LLM_DONE_FILE"
elif [ -f "$PRE_SCAN_SCRIPT" ]; then
    log "INFO" "📨 发送 Slack 确认通知（等待 10 分钟用户回复）..."
    # v0.43.16: max-wait 1440(24h) → 10 分钟，与本段注释的设计意图一致。
    # 24h 等待 × Slack Webhook 已 404 失效 = 每天的运行都挂满 24 小时才降级
    # 执行，形成"昨天启动的进程恰好在今天 14:00 超时接力扫描"的病态节奏：
    # 日志跨天错乱（8/11 内容写进 -08-10.log）、launchd 当日 fire 因实例
    # 已存在被跳过、扫描时刻完全靠上一天的超时点对齐。Webhook 修复前该
    # 确认机制送达不了任何人，等待只有成本没有收益。
    run_step --timeout 900 "$PRE_SCAN_SCRIPT" --wait 5 --remind-interval 60 --max-wait 10 --tickers $TICKERS_ARG >> "$LOGFILE" 2>&1
    NOTIFY_RC=$?
    if [ $NOTIFY_RC -eq 42 ]; then
        LLM_FLAG="--use-llm"
        log "INFO" "✅ LLM 模式已确认启用"
    elif [ $NOTIFY_RC -eq 124 ]; then
        log "WARN" "⏰ Slack 通知超时，默认 --no-llm"
    else
        log "INFO" "⏱️ 使用规则引擎模式（免费）"
    fi
else
    log "WARN" "pre_scan_notify.py 不存在，跳过 LLM 确认，默认 --no-llm"
fi

log "INFO" "   LLM 模式：$LLM_FLAG"
STEP2_START=$(date +%s)

# ── Step 2 超时预算：按标的数计算，不写死 ──
# v0.45.89：原来规则模式写死 1800s（30min），2026-08-31 实测 30 只跑到
# 20/30 就被杀（RC=124）。根因不是网络故障而是节拍变了：v0.45.56/61/66
# 把 429 限流治好之后，每只票**真的会等 CBOE 全链 OI 回来**（15~19 个
# 到期日），实测 20 只 / 1800s = 90s/只；而在限流时代，429 是秒回的拒绝，
# 同样 30 只只要 26 分钟——「修好之后反而变慢」。
# 写死的常数还有第二个问题：WATCHLIST 是会变的（config.WATCHLIST 是唯一
# 真相），名单一加票，常数就再次悄悄过期。故改为按只数算。
# 取 120s/只（实测 90s 上留 33% 余量）；下限沿用原值，保证小名单不缩水。
TICKER_COUNT=$(echo "$TICKERS_ARG" | wc -w | tr -d ' ')
[ "${TICKER_COUNT:-0}" -gt 0 ] 2>/dev/null || TICKER_COUNT=30   # 兜底：算不出就按满编
STEP2_TIMEOUT=$(( TICKER_COUNT * 120 ))
[ "$STEP2_TIMEOUT" -lt 1800 ] && STEP2_TIMEOUT=1800
if [ "$LLM_FLAG" = "--use-llm" ]; then
    # 沿用原设计的差额：LLM 模式比规则模式多 15 分钟（2700-1800）
    STEP2_TIMEOUT=$(( STEP2_TIMEOUT + 900 ))
fi
log "INFO" "   超时限制：${STEP2_TIMEOUT}s（${TICKER_COUNT} 只 × 120s/只，下限 1800s）"

run_step --timeout $STEP2_TIMEOUT "$PROJECT_DIR/alpha_hive_daily_report.py" --swarm $LLM_FLAG --tickers $TICKERS_ARG >> "$LOGFILE" 2>&1
STEP2_RC=$?
STEP2_END=$(date +%s)
STEP2_DURATION=$((STEP2_END - STEP2_START))
if [ $STEP2_RC -eq 0 ]; then
    log "INFO" "✅ Step 2 成功（耗时 ${STEP2_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step2_hive_analysis\": {\"status\": \"success\", \"duration_seconds\": $STEP2_DURATION}}")
elif [ $STEP2_RC -eq 124 ]; then
    log "ERROR" "⏰ Step 2 超时（>${STEP2_TIMEOUT}s），蜂群分析被终止！"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step2_hive_analysis\": {\"status\": \"timeout\", \"duration_seconds\": $STEP2_DURATION}}")
    set_status partial
elif [ $STEP2_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 2 跳过（脚本不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step2_hive_analysis\": {\"status\": \"skipped\"}}")
    set_status partial
else
    log "WARN" "⚠️ Step 2 失败，但继续进行（耗时 ${STEP2_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step2_hive_analysis\": {\"status\": \"failed\", \"duration_seconds\": $STEP2_DURATION}}")
    set_status partial
fi
# ── v0.45.118 预算余量可见化（只报数，不判定；判定闸是下一版的事）──
# 08-26→09-04 Step 2 从 749s 涨到 3342s、中间被杀两次，全程没有一行日志
# 说"离预算还剩多少"。09-04 实际余量 258s（7%），而没人知道。
if [ "${STEP2_TIMEOUT:-0}" -gt 0 ]; then
    STEP2_HEADROOM=$(( STEP2_TIMEOUT - STEP2_DURATION ))
    STEP2_HEADROOM_PCT=$(( STEP2_HEADROOM * 100 / STEP2_TIMEOUT ))
    log "INFO" "   Step 2 预算余量：${STEP2_HEADROOM}s / ${STEP2_TIMEOUT}s（${STEP2_HEADROOM_PCT}%）"
fi
# ── v0.45.4 标的完整性闸：「一只都不能丢」──
# 此前唯一的数量提示是 Step 3 那行 "扫描 ${#TICKERS[@]} 只"，取自**配置数组长度**，
# 从不与实际产出比对。于是 2026-08-12 丢 7 只、08-13 丢 4 只、08-25 丢 2 只
# （COST/DE 撞 AST SystemError 被 except 吞掉），全程打印「✅ 所有步骤成功」。
SWARM_JSON="$DATA_DIR/.swarm_results_${DATE_STR}.json"
if [ -f "$SWARM_JSON" ]; then
    # v0.45.15: 读失败返回 -1 而不是 0——0 会被下面当成「30 只全丢」误报，
    # 把「无法判定」渲染成一个具体且极端的结论，正是本项目要根除的形状。
    ACTUAL_COUNT=$("$PYTHON3" -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$SWARM_JSON" 2>/dev/null || echo -1)
    INTENDED_COUNT=${#TICKERS[@]}
    if [ "$ACTUAL_COUNT" -lt 0 ]; then
        log "ERROR" "🚨 标的完整性无法判定：$SWARM_JSON 读取/解析失败（非空文件但 JSON 损坏？）"
        set_status partial
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"ticker_completeness\": {\"status\": \"unknown\", \"reason\": \"unreadable\"}}")
    elif [ "$ACTUAL_COUNT" -eq 0 ]; then
        # v0.45.66：文件在、JSON 合法、但一只标的都没有。
        # 这不是「部分丢失」，是零产出 —— 与文件不存在同级，故 failed 而非 partial。
        # 必须排在 `-lt INTENDED_COUNT` 之前：0 也满足那个条件，会被吞成「丢失」。
        log "ERROR" "🚨 本轮零产出：$SWARM_JSON 存在但含 0 只标的"
        log "ERROR" "   ⇒ 网站不会更新、日报不会生成、T+7 观测永久缺这一天"
        set_status failed
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"ticker_completeness\": {\"status\": \"failed\", \"count\": 0}}")
    elif [ "$ACTUAL_COUNT" -lt "$INTENDED_COUNT" ]; then
        DROPPED=$("$PYTHON3" -c "
import json,sys
have=set(json.load(open(sys.argv[1])))
print(' '.join(t for t in sys.argv[2:] if t not in have))
" "$SWARM_JSON" "${TICKERS[@]}" 2>/dev/null)
        log "ERROR" "🚨 标的丢失：打算扫 ${INTENDED_COUNT} 只，实际产出 ${ACTUAL_COUNT} 只，缺：${DROPPED}"
        log "ERROR" "   Python 侧已重试 3 轮仍未补齐。查日志里带调用栈的『并行分析失败』定位。"
        set_status partial
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"ticker_completeness\": {\"status\": \"incomplete\", \"intended\": $INTENDED_COUNT, \"actual\": $ACTUAL_COUNT}}")
    else
        log "INFO" "✅ 标的完整性：${ACTUAL_COUNT}/${INTENDED_COUNT} 全部产出"
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"ticker_completeness\": {\"status\": \"complete\", \"count\": $ACTUAL_COUNT}}")
    fi
else
    # v0.45.66：原为 partial。文件根本不存在 = 本轮什么都没产出，
    # 与"丢了几只"不是一个量级 —— 升级为 failed，接上 alert_manager 的
    # CRITICAL P0 路径（该分支入库以来从未触发过，因为编排器只写 success/partial）。
    log "ERROR" "🚨 本轮零产出：$SWARM_JSON 不存在"
    log "ERROR" "   ⇒ 网站不会更新、日报不会生成、T+7 观测永久缺这一天"
    set_status failed
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"ticker_completeness\": {\"status\": \"failed\", \"reason\": \"file_missing\"}}")
fi

# ── LLM 防重复：无论 Step 2 成功/失败/超时，只要启动了 LLM 扫描就打标记 ──
# 原逻辑仅在 RC=0 时创建标记，导致 Step 2 失败后当天仍可再次触发 LLM，造成重复计费
if [ "$LLM_FLAG" = "--use-llm" ] && [ $STEP2_RC -ne 2 ]; then
    touch "$LLM_DONE_FILE"
    log "INFO" "🔒 LLM 防重复标记已创建：${LLM_DONE_FILE}（Step2 RC=${STEP2_RC}）"
fi

# ================================================================
# Step 3: ML 增强报告生成
# v0.41.9: 只补跑 Step 2 内部（_generate_ml_reports）未能生成的标的。
# generate_ml_report.py 对同一批标的做的是完全相同的 CBOE/yfinance 抓取，
# 全量重跑等于把当天的调用量直接翻倍——2026-07-23 深夜限流连锁崩溃
# （10/10 标的 ML 报告在 Step 2 和 Step 3 里同时失败）即因此触发。
# ================================================================
log "INFO" ""
log "INFO" "【Step 3/5】ML 增强报告 - 启动"
STEP3_START=$(date +%s)

# v0.42.9: Step 2 的 _generate_ml_reports 已限流到分数最高的 N 只
# （ALPHA_HIVE_ML_REPORT_MAX，默认 12）。标的池扩到 30 后，若这里仍按
# **全部 30 只**判"缺失"，会把剩下 18 只在 Step 3 补跑一遍，
# 限流被完全抵消、调用量反而更高。
# 正确语义：Step 3 只补跑"Step 2 本该生成却失败"的，即上限之内的那些。
# 判据改为：已生成数 < 上限 且 该标的排在上限之内。
# 由于无法在 shell 里知道分数排序，这里改用「已生成数是否达到上限」来判断：
# 达到上限即视为 Step 2 正常完成，不补跑。
ML_CAP="${ALPHA_HIVE_ML_REPORT_MAX:-12}"
# v0.45.95：原来这里是 `ls "$PROJECT_DIR"/alpha-hive-*-...html | wc -l`，
# 在 launchd 下**恒为 0** —— 8/27 起每一次运行都打「已生成 0 / 预期 12」，
# 于是 Step 3 天天为另外 12 只补跑一遍，把上面注释里那个 ML_CAP 限流
# 直接翻倍（正是 line 594 记的 2026-07-23 限流连锁崩溃的成因）。
# 根因不是日期也不是路径：macOS TCC 对 ~/Desktop 允许 stat、拒绝 readdir。
# bash 展开 glob 需要 readdir → 无匹配 → ls 收到字面量而报错 →
# `2>/dev/null` 吞掉 → wc 数 0。**报错被静默，计数看起来像个合法的 0。**
# 而同一段里 line 619 的 `[ -f 精确路径 ]` 只需 stat，一直是准的
# （它每次都正确跳过了 Step 2 已生成的那些标的）。故统一改用该机制。
GENERATED_COUNT=0
for _t in "${TICKERS[@]}"; do
    if [ -f "$DATA_DIR/alpha-hive-${_t}-ml-enhanced-${DATE_STR}.html" ]; then
        GENERATED_COUNT=$(( GENERATED_COUNT + 1 ))
    fi
done
EXPECTED_ML=$(( ${#TICKERS[@]} < ML_CAP ? ${#TICKERS[@]} : ML_CAP ))

MISSING_TICKERS=""
if [ "$GENERATED_COUNT" -lt "$EXPECTED_ML" ]; then
    # 确实少生成了——只补跑没有 HTML 的，且总数不超过上限
    NEED=$(( EXPECTED_ML - GENERATED_COUNT ))
    for t in "${TICKERS[@]}"; do
        if [ "$NEED" -le 0 ]; then break; fi
        if [ ! -f "$DATA_DIR/alpha-hive-${t}-ml-enhanced-${DATE_STR}.html" ]; then
            MISSING_TICKERS="$MISSING_TICKERS $t"
            NEED=$(( NEED - 1 ))
        fi
    done
fi
MISSING_TICKERS=$(echo "$MISSING_TICKERS" | xargs)
log "INFO" "   ML 报告：已生成 ${GENERATED_COUNT} / 预期 ${EXPECTED_ML}（扫描 ${#TICKERS[@]} 只，上限 ${ML_CAP}）"

if [ -z "$MISSING_TICKERS" ]; then
    log "INFO" "⏭️  Step 3 跳过（ML 报告已达上限，无需补跑）"
    STEP3_END=$(date +%s)
    STEP3_DURATION=$((STEP3_END - STEP3_START))
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step3_ml_report\": {\"status\": \"skipped\", \"reason\": \"already_generated_by_step2\"}}")
else
    log "INFO" "   Step 2 缺失以下标的的 ML 报告，仅为这些补跑：$MISSING_TICKERS"
    # v0.45.95：日期翻篇就别补了。
    # generate_ml_report.py 用 pdt_today() 取日期、不接受 --date，产出的
    # 文件名是**此刻**的日期而不是本轮的 DATE_STR。当天跑两者相同；但
    # 2026-09-02 那次机器中途没电休眠 9 小时，Step 3 跑到了次日 00:04，
    # 于是补出 12 份标着 2026-09-03 的报告 —— 一个市场还没开盘的日期，
    # 而且 swarm_results 全空（按 9/3 找扫描结果，自然找不到）。
    # 错标日期的报告比缺报告更坏：它会让读者以为那天已有数据。
    _NOW_DATE=$(date +%Y-%m-%d)
    if [ "$_NOW_DATE" != "$DATE_STR" ]; then
        log "WARN" "⏭️  Step 3 跳过：本轮口径 ${DATE_STR}，但现在已是 $_NOW_DATE"
        log "WARN" "   generate_ml_report.py 按当前日期命名，此时补跑会产出错标日期的报告"
        # 用专属退出码 3 交给下面统一处理。**不要在这里设 RC=0** ——
        # 下面 `if [ $STEP3_RC -eq 0 ]` 会打「✅ Step 3 成功」并把这里写的
        # skipped 覆盖成 success，又是一个假标签。
        STEP3_RC=3
    else
    run_step --timeout 600 "$PROJECT_DIR/generate_ml_report.py" --tickers $MISSING_TICKERS >> "$LOGFILE" 2>&1
    STEP3_RC=$?
    fi
    STEP3_END=$(date +%s)
    STEP3_DURATION=$((STEP3_END - STEP3_START))
    if [ $STEP3_RC -eq 3 ]; then
        # v0.45.95：跨日跳过。不计入 OVERALL_STATUS —— 这是主动的正确选择，
        # 不是失败；但也绝不能标成 success。
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step3_ml_report\": {\"status\": \"skipped\", \"reason\": \"date_rolled_over\", \"run_date\": \"$DATE_STR\", \"now\": \"$_NOW_DATE\"}}")
    elif [ $STEP3_RC -eq 0 ]; then
        log "INFO" "✅ Step 3 成功（耗时 ${STEP3_DURATION}s）"
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step3_ml_report\": {\"status\": \"success\", \"duration_seconds\": $STEP3_DURATION}}")
    elif [ $STEP3_RC -eq 124 ]; then
        log "ERROR" "⏰ Step 3 超时（>600s），继续进行"
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step3_ml_report\": {\"status\": \"timeout\", \"duration_seconds\": $STEP3_DURATION}}")
        set_status partial
    elif [ $STEP3_RC -eq 2 ]; then
        log "WARN" "⏭️  Step 3 跳过（脚本不存在）"
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step3_ml_report\": {\"status\": \"skipped\"}}")
    else
        log "WARN" "⚠️ Step 3 失败，但继续进行（耗时 ${STEP3_DURATION}s）"
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step3_ml_report\": {\"status\": \"failed\", \"duration_seconds\": $STEP3_DURATION}}")
        set_status partial
    fi
fi

# ================================================================
# Step 4/5: 仪表板与部署的产出确认
#
# v0.45.316：这里原先 `run_step` 两个**从未存在**的脚本（update_dashboard.py /
# auto_deploy.py），靠 run_step 的 rc=2（脚本不存在）走进下面的判断，并且每天
# 多打两行「脚本不存在，跳过」的 WARN。仪表板与部署都在 Step 2 pipeline 内完成，
# 真正有用的只有 v0.45.66 加的那层判断：Step 2 没跑完 ⇒ 本轮网站不会更新。
# 现直接判 STEP2_RC。status.json 里 step4_dashboard / step5_github_deploy 的取值
# 与原 rc=2 分支逐字相同（Step 6 告警照旧读它们）；ERROR 日志只去掉了
# 「xxx.py 不存在」那半句，其余同原文。
# ================================================================
log "INFO" ""
log "INFO" "【Step 4/5】仪表板更新 + GitHub 部署（由 Step 2 pipeline 完成）- 确认"
if [ $STEP2_RC -eq 0 ]; then
    log "INFO" "⏭️  Step 4 跳过（仪表板已由 Step 2 pipeline 生成）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step4_dashboard\": {\"status\": \"skipped_builtin\"}}")
    log "INFO" "⏭️  Step 5 跳过（部署已由 Step 2 pipeline 完成）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step5_github_deploy\": {\"status\": \"skipped_builtin\"}}")
else
    log "ERROR" "🚨 Step 4 未生成仪表板！仪表板由 Step 2 pipeline 生成，"
    log "ERROR" "   而 Step 2 没跑完（RC=${STEP2_RC}）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step4_dashboard\": {\"status\": \"failed\", \"reason\": \"step2_did_not_complete\", \"step2_rc\": $STEP2_RC}}")
    log "ERROR" "🚨 Step 5 未部署！部署由 Step 2 pipeline 完成，"
    log "ERROR" "   而 Step 2 没跑完（RC=${STEP2_RC}）—— **本轮网站不会更新**"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step5_github_deploy\": {\"status\": \"failed\", \"reason\": \"step2_did_not_complete\", \"step2_rc\": $STEP2_RC}}")
fi

# ================================================================
# 写状态文件（v0.45.66 提取成函数，因为要写两次）
# ================================================================
# 为什么要写两次：Step 6（alert_manager）**读的就是这个文件**，而它此前
# 在第 675 行运行、文件在第 978 行才写 —— 相差 300 行。
# 于是告警分析每一轮读的都是**上一轮**的状态：
# `status == 'failed'` 的 P0 路径、`step.status == 'failed'` 的 P1 路径，
# 判的都是昨天。8/28 那两条 HIGH 告警之所以还对，是因为它们查的是
# "日报文件在不在"（按当天日期），跟 status.json 无关。
write_status() {
    local _end; _end=$(date +%s)
    local _dur=$((_end - STEP1_START))
    local _tmp="$REPORTDIR/.status.json.tmp"
    cat > "$_tmp" <<STATUSEOF
{
  "last_run": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "last_run_date": "$DATE_STR",
  "status": "$OVERALL_STATUS",
  "total_duration_seconds": $_dur,
  "tickers": $(echo "$TICKERS_ARG" | jq -R 'split(" ") | map(select(length > 0))' 2>/dev/null || echo '[]'),
  "steps_result": $STEPS_RESULT,
  "step2_budget": {
    "timeout_seconds": ${STEP2_TIMEOUT:-null},
    "duration_seconds": ${STEP2_DURATION:-null},
    "headroom_seconds": ${STEP2_HEADROOM:-null},
    "headroom_pct": ${STEP2_HEADROOM_PCT:-null}
  },
  "logfile": "$LOGFILE"
}
STATUSEOF
    # v0.45.118：并入 Python 侧写的耗时分解（五阶段 + 三个取数计数器）。
    # 只认**本轮日期**的文件——昨天那份留在 logs/ 里，不能冒充今天的。
    # 没有或日期不对就不加这个键：缺失 ≠ 零（读者按「没测到」处理）。
    local _timing="$DATA_DIR/logs/scan_timing.json"
    if [ -r "$_timing" ] && jq -e --arg d "$DATE_STR" '.date == $d' "$_timing" >/dev/null 2>&1; then
        local _merged
        if _merged=$(jq --slurpfile t "$_timing" '. + {scan_timing: $t[0]}' "$_tmp" 2>/dev/null) && [ -n "$_merged" ]; then
            printf '%s\n' "$_merged" > "$_tmp"
        fi
    fi
    if jq . "$_tmp" > "$REPORTDIR/status.json" 2>/dev/null; then
        rm -f "$_tmp"
    else
        mv "$_tmp" "$REPORTDIR/status.json"    # jq 不可用时原样落盘
    fi
}

# 先写一次，让 Step 6 的告警分析看到**本轮**而不是上一轮
write_status
log "INFO" "📄 已写入本轮状态（供 Step 6 告警分析读取）：status=$OVERALL_STATUS"

# ================================================================
# Step 6: 智能告警分析 (Phase 2 新增)
# ================================================================
log "INFO" ""
log "INFO" "【Step 6/6】智能告警分析 - 启动"
STEP6_START=$(date +%s)

run_step --timeout 300 "$PROJECT_DIR/alert_manager.py" --status-json "$REPORTDIR/status.json" --output-dir "$LOGDIR" >> "$LOGFILE" 2>&1
STEP6_RC=$?
STEP6_END=$(date +%s)
STEP6_DURATION=$((STEP6_END - STEP6_START))
if [ $STEP6_RC -eq 0 ]; then
    log "INFO" "✅ Step 6 成功（耗时 ${STEP6_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step6_alert_analysis\": {\"status\": \"success\", \"duration_seconds\": $STEP6_DURATION}}")
elif [ $STEP6_RC -eq 124 ]; then
    log "ERROR" "⏰ Step 6 超时（>300s），继续进行"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step6_alert_analysis\": {\"status\": \"timeout\", \"duration_seconds\": $STEP6_DURATION}}")
elif [ $STEP6_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 6 跳过（脚本不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step6_alert_analysis\": {\"status\": \"skipped\"}}")
else
    log "WARN" "⚠️ Step 6 失败，但不影响主流程（耗时 ${STEP6_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step6_alert_analysis\": {\"status\": \"failed\", \"duration_seconds\": $STEP6_DURATION}}")
fi

# ================================================================
# Step 7: 推送每日简报到 Slack (Phase 2 新增)
# ================================================================
log "INFO" ""
log "INFO" "【Step 7/7】推送每日简报到 Slack - 启动"
STEP7_START=$(date +%s)

run_step --timeout 300 "$PROJECT_DIR/push_report_to_slack.py" >> "$LOGFILE" 2>&1
STEP7_RC=$?
STEP7_END=$(date +%s)
STEP7_DURATION=$((STEP7_END - STEP7_START))
if [ $STEP7_RC -eq 0 ]; then
    log "INFO" "✅ Step 7 成功（耗时 ${STEP7_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step7_push_report\": {\"status\": \"success\", \"duration_seconds\": $STEP7_DURATION}}")
elif [ $STEP7_RC -eq 124 ]; then
    log "ERROR" "⏰ Step 7 超时（>300s），继续进行"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step7_push_report\": {\"status\": \"timeout\", \"duration_seconds\": $STEP7_DURATION}}")
elif [ $STEP7_RC -eq 2 ]; then
    log "INFO" "⏭️  Step 7 跳过（Slack 推送由 Claude Code MCP 手动执行）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step7_push_report\": {\"status\": \"skipped_manual\"}}")
else
    log "WARN" "⚠️ Step 7 失败，但不影响主流程（耗时 ${STEP7_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step7_push_report\": {\"status\": \"failed\", \"duration_seconds\": $STEP7_DURATION}}")
fi

# ================================================================
# 写入 status.json
# ================================================================
log "INFO" ""
# ================================================================
# Step 8: 记录性能指标 (Week 2 新增)
# ================================================================
log "INFO" ""
log "INFO" "【Step 8/9】性能指标收集 - 启动"
STEP8_START=$(date +%s)

run_step --timeout 300 "$PROJECT_DIR/metrics_collector.py" --record --status-json "$REPORTDIR/status.json" >> "$LOGFILE" 2>&1
STEP8_RC=$?
STEP8_END=$(date +%s)
STEP8_DURATION=$((STEP8_END - STEP8_START))
if [ $STEP8_RC -eq 0 ]; then
    log "INFO" "✅ Step 8 成功（耗时 ${STEP8_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step8_metrics_collection\": {\"status\": \"success\", \"duration_seconds\": $STEP8_DURATION}}")
elif [ $STEP8_RC -eq 124 ]; then
    log "ERROR" "⏰ Step 8 超时（>300s），继续进行"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step8_metrics_collection\": {\"status\": \"timeout\", \"duration_seconds\": $STEP8_DURATION}}")
elif [ $STEP8_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 8 跳过（脚本不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step8_metrics_collection\": {\"status\": \"skipped\"}}")
else
    log "WARN" "⚠️ Step 8 失败，但不影响主流程（耗时 ${STEP8_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step8_metrics_collection\": {\"status\": \"failed\", \"duration_seconds\": $STEP8_DURATION}}")
fi

# v0.45.316：原 Step 9（pheromone_recorder.py 信息素持久化 + 准确率追踪）已删除——
# 该脚本从未存在，每天只打一行「Step 9 跳过（脚本不存在）」。信息素与预测
# 在 Step 2 蜂群扫描内部落 pheromone.db。

# ================================================================
# Step 10: 扫描连续性体检 (v0.44.0 新增)
#
# 为什么放在最后：它要读今天这轮写进 pheromone.db 的记录，必须在 Step 2
# （蜂群分析，内部完成信息素持久化）之后。
#
# 为什么它**不能**影响 OVERALL_STATUS：连续性反映的是过去 30 个交易日的
# 历史事实，不是今天这轮跑得好不好。把历史空档记成"今天失败"会让 status.json
# 长期停在 failed，等于废掉那个字段。所以此处刻意不碰 OVERALL_STATUS。
#
# 为什么值得单独占一步：扩池 10→30 只把出结论所需的日历时间缩短 5.18 倍
# （实测见 experiments/ic_power_report.md），但计价单位是**有扫描的 ISO 周数**
# ——漏一周就少一个不重叠 T+7 观测，增益按比例被吃掉。连续性是唯一能兑现
# 扩池收益的东西。
#
# 不发 Slack：CLAUDE.md 禁止 Bot 发扫描失败通知。本步只写日志与 status.json。
# ================================================================
log "INFO" ""
log "INFO" "【Step 10】扫描连续性体检 - 启动"
STEP10_START=$(date +%s)

CONTINUITY_JSON="$LOGDIR/scan_continuity-$DATE_STR.json"
# 用 --out 让脚本自己写 JSON，**不要** `> "$CONTINUITY_JSON"` 捕获 stdout：
# 本文件的 log() 用 `tee -a` 会写 stdout，而 run_step 在"脚本不存在"(return 2)
# 与 TCC 权限被拒两条路径上都会 log —— 那些行会混进 JSON 让下游解析失败。
run_step --timeout 120 "$PROJECT_DIR/scan_continuity.py" --days 30 --quiet \
         --out "$CONTINUITY_JSON" >> "$LOGFILE" 2>&1
STEP10_RC=$?
STEP10_END=$(date +%s)
STEP10_DURATION=$((STEP10_END - STEP10_START))

if [ $STEP10_RC -eq 0 ]; then
    log "INFO" "✅ Step 10：连续性健康（耗时 ${STEP10_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step10_scan_continuity\": {\"status\": \"healthy\", \"duration_seconds\": $STEP10_DURATION}}")
elif [ $STEP10_RC -eq 1 ]; then
    # 降级：把聚合事实写进日志，供人事后查，但不动 OVERALL_STATUS
    CONT_SUMMARY=$("$PYTHON3" -c "
import json,sys
try:
    d=json.load(open('$CONTINUITY_JSON'))
except Exception as e:
    print(f'（无法解析连续性 JSON: {e}）'); sys.exit(0)
w=d.get('window',{})
print(f\"过去 {w.get('trading_days','?')} 个交易日跑了 {d.get('scanned_days','?')} 次\"
      f\"（覆盖率 {d.get('coverage',0):.0%}），ISO 周覆盖 \"
      f\"{d.get('weeks_covered','?')}/{d.get('weeks_total','?')}，\"
      f\"最长空档 {d.get('longest_gap','?')} 个交易日\")
miss=d.get('weeks_missed') or []
if miss: print(f\"  完全无扫描的周: {', '.join(miss)} ← 每个都是一个永久丢失的 T+7 观测\")
" 2>/dev/null)
    log "WARN" "⚠️ Step 10：扫描连续性降级 — $CONT_SUMMARY"
    log "WARN" "   （不计入 OVERALL_STATUS：这是历史覆盖率问题，非本轮失败）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step10_scan_continuity\": {\"status\": \"degraded\", \"duration_seconds\": $STEP10_DURATION, \"detail_json\": \"$CONTINUITY_JSON\"}}")
elif [ $STEP10_RC -eq 3 ]; then
    log "WARN" "⚠️ Step 10：无法判定连续性（找不到 pheromone.db）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step10_scan_continuity\": {\"status\": \"undetermined\"}}")
elif [ $STEP10_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 10 跳过（scan_continuity.py 不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step10_scan_continuity\": {\"status\": \"skipped\"}}")
elif [ $STEP10_RC -eq 124 ]; then
    log "ERROR" "⏰ Step 10 超时（>120s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step10_scan_continuity\": {\"status\": \"timeout\"}}")
else
    log "WARN" "⚠️ Step 10 异常（exit=${STEP10_RC}），不影响主流程"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step10_scan_continuity\": {\"status\": \"error\", \"rc\": $STEP10_RC}}")
fi

# ================================================================
# Step 11: IC 重跑就绪度 (v0.44.4 新增)
#
# 为什么放在编排器而不只放在周度任务里：周度任务是 LLM 型的，可能被跳过、
# 被改写、或某周没跑。编排器每个交易日都跑，是更可靠的心跳。两处都有，
# 谁先发现都行 —— 但**都不发 Slack**。
#
# 与 Step 10 同样：**不影响 OVERALL_STATUS**。"样本还没攒够"是正常状态，
# 不是今天这轮失败。
# ================================================================
log "INFO" ""
log "INFO" "【Step 11】IC 重跑就绪度 - 启动"

READINESS_JSON="$LOGDIR/ic_rerun_readiness-$DATE_STR.json"
# 与 Step 10 同一模式：用 --out 写 JSON，**不用** `> FILE` 捕获 stdout
# （本文件的 log() 走 tee 会污染 stdout）。
run_step --timeout 60 "$PROJECT_DIR/ic_rerun_readiness.py" \
         --quiet --out "$READINESS_JSON" >> "$LOGFILE" 2>&1
STEP11_RC=$?

READINESS_LINE=$("$PYTHON3" -c "
import json,sys
try:
    d=json.load(open('$READINESS_JSON'))
except Exception:
    print('（无法解析就绪度 JSON）'); sys.exit(0)
c=d.get('cohort',{})
print(f\"世代自 {c.get('date','?')}（{c.get('version','?')}）起，\"
      f\"已攒 {d.get('weeks_accrued','?')}/{d.get('weeks_required','?')} 个不重叠周\"
      f\"（{d.get('n_ripe_samples','?')} 条已回填样本）\", end='')
if d.get('eta_date'): print(f\"，预计 ≈{d['eta_date']} 到位\", end='')
if d.get('pool_note'): print(f\" ⚠️ {d['pool_note']}\", end='')
print()
" 2>/dev/null)

# v0.45.334（2026-09-26）：世代边界核对结果。此前 Step 11 只读就绪度，边界写错（代码推晚了 /
# 推早了 / 核不了）时唯一会红的判别器只在人读模式里跑，`--quiet --out` 拿不到 ⇒ 自动流程里
# 从没人看。现在 ic_rerun_readiness 把它恒写进 JSON 的 `cohort_boundary_evidence` 键，这里：
#   ① 每天把判别结果（含正常的 ⏳/✅）写进 status.json 的 step11 条目 —— 事后可查哪天开始不对；
#   ② alarm 为真时打 WARN（🚨 行），指明处理方式（追加更正条目，只追加不改写）。
# 与就绪度同理**不改 OVERALL_STATUS**：边界核对不过是数据口径问题，不是本轮扫描失败。
# 取不到这个键（旧代码 / JSON 坏）⇒ 记 null，不假装「正常」。
# 回滚：删掉本段与下方「世代边界核对」段；原版 alpha-hive-orchestrator.sh.bak-20260926_pre-v0.45.334-step11
BOUNDARY_JSON=$("$PYTHON3" -c "
import json,sys
try:
    d=json.load(open('$READINESS_JSON'))
except Exception:
    print('null'); sys.exit(0)
b=d.get('cohort_boundary_evidence')
if not isinstance(b,dict):
    print('null'); sys.exit(0)
keep=('version','boundary','verdict','marker_first_seen','unmarked_after_boundary','alarm','line','error')
print(json.dumps({k:b.get(k) for k in keep if k in b}, ensure_ascii=False))
" 2>/dev/null)
# 任何非法 JSON 都退成 null：jq --argjson 吃到坏值会让下面整条 STEPS_RESULT 变空
if ! printf '%s' "$BOUNDARY_JSON" | jq -e . >/dev/null 2>&1; then
    BOUNDARY_JSON=null
fi

if [ $STEP11_RC -eq 0 ]; then
    log "INFO" "🎯 Step 11：IC 重跑**已就绪** — $READINESS_LINE"
    log "INFO" "   → 该跑: experiments/ml_expected_return_replay.py 与 signal_archive.py --analyze"
    log "INFO" "   （刻意不自动跑：IC 分析要人看结果并做判断）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step11_ic_rerun_readiness\": {\"status\": \"ready\"}}")
elif [ $STEP11_RC -eq 1 ]; then
    log "INFO" "⏳ Step 11：$READINESS_LINE"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step11_ic_rerun_readiness\": {\"status\": \"accruing\"}}")
elif [ $STEP11_RC -eq 3 ]; then
    log "WARN" "⚠️ Step 11：无法判定 IC 重跑就绪度"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step11_ic_rerun_readiness\": {\"status\": \"undetermined\"}}")
elif [ $STEP11_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 11 跳过（ic_rerun_readiness.py 不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step11_ic_rerun_readiness\": {\"status\": \"skipped\"}}")
else
    log "WARN" "⚠️ Step 11 异常（exit=${STEP11_RC}），不影响主流程"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step11_ic_rerun_readiness\": {\"status\": \"error\", \"rc\": $STEP11_RC}}")
fi

# ── 世代边界核对（v0.45.334，说明见上方 BOUNDARY_JSON 处）──
if [ "$BOUNDARY_JSON" = "null" ]; then
    log "INFO" "   Step 11 世代边界核对：就绪度 JSON 里没有 cohort_boundary_evidence（代码早于 v0.45.334 或 JSON 不可读），本轮未核对"
elif printf '%s' "$BOUNDARY_JSON" | jq -e '.alarm == true' >/dev/null 2>&1; then
    log "WARN" "🚨 Step 11：世代边界核对未通过 — $(printf '%s' "$BOUNDARY_JSON" | jq -r '.line // .verdict')"
    log "WARN" "   → 处理：按 ic_rerun_readiness.py 顶部「更正条目」流程**追加**一条更正（新标签 + _CORRECTS），不改写原条目"
    log "WARN" "   （不计入 OVERALL_STATUS：口径问题，非本轮失败；完整判别在 ${READINESS_JSON}）"
else
    log "INFO" "   Step 11 世代边界核对：$(printf '%s' "$BOUNDARY_JSON" | jq -r '.line // .verdict')"
fi
_S11_NEW=$(printf '%s' "$STEPS_RESULT" | jq --argjson b "$BOUNDARY_JSON" \
    '.step11_ic_rerun_readiness = ((.step11_ic_rerun_readiness // {}) + {cohort_boundary: $b})' 2>/dev/null)
if [ -n "$_S11_NEW" ]; then
    STEPS_RESULT="$_S11_NEW"
else
    log "WARN" "⚠️ Step 11：世代边界核对结果写入 status 失败（jq），STEPS_RESULT 保持原样"
fi


# ================================================================
# Step 12：扫描字段覆盖率闸（v0.45.36）
# ----------------------------------------------------------------
# 2026-08-26 14:10 那次扫描 yfinance 全线返回空：rv_30d 30/30→1/30、
# iv_rank 29/30→1/30、催化剂 21/30→0/30。而**退出码 0，日报照常上站**。
# 每处降级都被 except 老实接住了，没有一个是 bug —— 缺的是有人在看
# "老实降级"发生了多少次。这一步就是那个看的人。
#
# 刻意**不阻断**：数据缺失是事实，报告该出还得出，只是必须可见。
# 退出码 1 = 检出降级（写日志，不动 OVERALL_STATUS）；3 = 无法判定。
# ================================================================
log "INFO" ""
log "INFO" "【Step 12】扫描字段覆盖率 - 启动"

COVERAGE_JSON="$LOGDIR/scan_coverage-$DATE_STR.json"
# 同 Step 10/11：用 --out 写 JSON，**不用** `> FILE` 捕获 stdout
run_step --timeout 60 "$PROJECT_DIR/scan_coverage_gate.py" \
         --quiet --out "$COVERAGE_JSON" >> "$LOGFILE" 2>&1
STEP12_RC=$?

if [ $STEP12_RC -eq 0 ]; then
    log "INFO" "✅ Step 12：字段覆盖率健康 + 来源标签自洽"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step12_scan_coverage\": {\"status\": \"healthy\"}}")
elif [ $STEP12_RC -eq 1 ]; then
    COV_SUMMARY=$("$PYTHON3" -c "
import json,sys
try:
    d=json.load(open('$COVERAGE_JSON'))
except Exception:
    print('（无法解析覆盖率 JSON）'); sys.exit(0)
bad=[f for f in d.get('fields',[]) if f.get('degraded')]
print('; '.join(f\"{f['field']} {f['have']}/{f['total']}\" for f in bad), end='')
if d.get('likely_network_layer'):
    print('  ⚠️ 多个不同数据源同时降级，疑为网络/闸门层', end='')
print()
" 2>/dev/null)
    # v0.45.51：退出码 1 现在也可能来自**来源标签矛盾**
    # （标签宣称取数成功，它管辖的值却为空）—— 见 scan_coverage_gate.check_label_honesty
    log "WARN" "⚠️ Step 12：检出字段降级或来源标签矛盾 — $COV_SUMMARY"
    log "WARN" "   → 本次报告里这些指标会显示「—」；若需补数据，重跑当日扫描"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step12_scan_coverage\": {\"status\": \"degraded\"}}")
elif [ $STEP12_RC -eq 3 ]; then
    log "WARN" "⚠️ Step 12：无法判定字段覆盖率（扫描结果文件缺失或不可解析）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step12_scan_coverage\": {\"status\": \"undetermined\"}}")
elif [ $STEP12_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 12 跳过（scan_coverage_gate.py 不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step12_scan_coverage\": {\"status\": \"skipped\"}}")
else
    log "WARN" "⚠️ Step 12 异常（exit=${STEP12_RC}），不影响主流程"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step12_scan_coverage\": {\"status\": \"error\", \"rc\": $STEP12_RC}}")
fi


# ================================================================
# Step 13：上游宏观日程发布监视（v0.45.67）
# ----------------------------------------------------------------
# economic_calendar.py 的四张表是硬编码的，只能覆盖官方**已发布**的范围。
# 2026-08-29 核对时 BLS 的 2027 CPI/NFP 日程、BEA 的 2027 GDP 日期都还没发布，
# 所以那三张表只到 2026-12。v0.45.65 的地平线告警只会说「快见底了」，
# 说不出「上游发了没」—— 这一步替人去源站看一眼。
#
# 为什么放编排器而不是做成周度 LLM 任务（与 Step 11 同一理由，外加成本）：
#   · 周度 LLM 任务可能被跳过、被改写、某周没跑；编排器每个交易日都跑，心跳更可靠
#   · 这个检查是纯机械的（抓四个页面 + 比日期），不需要 LLM，**零 API 费用**
#   · 脚本自带 7 天节流，所以"每交易日跑"实际是"每周联网一次"
#
# 刻意**不阻断**、**不动 OVERALL_STATUS**：日历还没坏，只是需要有人去抄。
# 退出码 0=健康且上游无新；1=要人动手（有新日程可抄，或本地日历将见底）；
# 3=无法判定（抓取失败/页面改版 —— 这一档绝不能被当成"没有新的"）。
# ================================================================
log "INFO" ""
log "INFO" "【Step 13】上游宏观日程监视 - 启动"

CALWATCH_JSON="$LOGDIR/economic_calendar_watch-$DATE_STR.json"
# 同 Step 10/11/12：用 --out 写 JSON，**不用** `> FILE` 捕获 stdout
run_step --timeout 120 "$PROJECT_DIR/economic_calendar_watch.py" \
         --quiet --timeout 15 --out "$CALWATCH_JSON" >> "$LOGFILE" 2>&1
STEP13_RC=$?

if [ $STEP13_RC -eq 0 ]; then
    log "INFO" "✅ Step 13：宏观日历健康，上游无新日程"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step13_calendar_watch\": {\"status\": \"healthy\"}}")
elif [ $STEP13_RC -eq 1 ]; then
    CALWATCH_SUMMARY=$("$PYTHON3" -c "
import json,sys
try:
    d=json.load(open('$CALWATCH_JSON'))
except Exception:
    print('（无法解析监视器 JSON）'); sys.exit(0)
h=d.get('calendar_health',{})
new=d.get('new_schedule_tables') or []
if new:
    print('上游已发布新日程：' + '、'.join(new) + ' —— 待人工抄录', end='')
    for k in new:
        u=d.get('upstream',{}).get(k,{})
        items=u.get('new_items') or []
        print(f\"；{k} 新增 {len(items)} 条（{u.get('source','')}）\", end='')
    print()
else:
    # v0.45.68：「上游暂无新日程」只有四个源都查通了才有资格说。
    # 少一个源就只能说不知道 —— 否则抓取失败会被这句话整个吃掉。
    undet = d.get('undeterminable_tables') or []
    tail = ('上游暂无新日程可抄' if d.get('upstream_conclusive')
            else f\"⚠️ 上游 {len(undet)} 个源无法判定（{'、'.join(undet)}），本次无法确认上游有无新日程\")
    print(f\"本地日历 {h.get('status')}：{h.get('binding_table')} 表只到 \"
          f\"{h.get('binding_last_date')}（剩 {h.get('binding_horizon_days')} 天），{tail}\")
" 2>/dev/null)
    log "WARN" "⚠️ Step 13：$CALWATCH_SUMMARY"
    log "WARN" "   → 抄录必须人工：只抄官方已发布日程，禁止按「每月第二周/第一个周五」推算"
    log "WARN" "   → 抄完同步上移 economic_calendar._TABLE_SPECS 里该表的 verified_through"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step13_calendar_watch\": {\"status\": \"action_required\", \"detail_json\": \"$CALWATCH_JSON\"}}")
elif [ $STEP13_RC -eq 3 ]; then
    log "WARN" "⚠️ Step 13：无法判定上游日程（抓取失败或页面改版）"
    log "WARN" "   → 这**不等于**「上游没有新日程」；需人工看一眼源站，必要时修解析器"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step13_calendar_watch\": {\"status\": \"undetermined\"}}")
elif [ $STEP13_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 13 跳过（economic_calendar_watch.py 不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step13_calendar_watch\": {\"status\": \"skipped\"}}")
else
    log "WARN" "⚠️ Step 13 异常（exit=${STEP13_RC}），不影响主流程"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step13_calendar_watch\": {\"status\": \"error\", \"rc\": $STEP13_RC}}")
fi


# ================================================================
# Step 14：数据备份上线（v0.45.264 设计，2026-09-18 用户批准接入生产）
# ----------------------------------------------------------------
# 刻意不动 OVERALL_STATUS（同 Step 10/12 的先例）：备份状态不是"今天
# 的扫描"本身，纳入会把备份的偶发抖动误报成扫描失败。
# 刻意不发 Slack：按项目 CLAUDE.md「Slack 通知精简规则」，扫描失败/
# 数据质量类事件本就不发 DM，本模块同理，失败只写 status.json。
#
# 已处理（v0.45.284）：单次 push_failed/stale_or_missing 仍然不阻断、不发
# Slack（同上——单次失败是噪音）；但 run_backup.py 现在每次调用都追加一行
# 到 backup_status_history.jsonl，Step 15（backup_continuity.py）在本 Step
# 之后读这份历史，判"连续多天卡在同一种失败/陈旧状态"，同 Step 10 的模式。
# ================================================================
log "INFO" ""
log "INFO" "【Step 14】数据备份上线 - 启动"

BACKUP_STATUS_JSON="$HOME/alpha-hive-data/logs/backup_status.json"
BACKUP_HISTORY_JSONL="$HOME/alpha-hive-data/logs/backup_status_history.jsonl"
run_step --timeout 300 "$PROJECT_DIR/run_data_backup.py" \
         --src "$DATA_DIR" \
         --backup-dir "$HOME/alpha-hive-data/_git_backup" \
         --remote origin --branch main \
         --status-file "$BACKUP_STATUS_JSON" \
         --history-file "$BACKUP_HISTORY_JSONL" >> "$LOGFILE" 2>&1
STEP14_RC=$?

if [ $STEP14_RC -eq 0 ]; then
    log "INFO" "✅ Step 14：数据备份已推送"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step14_data_backup\": {\"status\": \"ok\", \"detail_json\": \"$BACKUP_STATUS_JSON\"}}")
elif [ $STEP14_RC -eq 1 ]; then
    # v0.45.307：rc==1 本该专属 secret_scan，但 run_backup.py::main() 顶层
    # try/except 覆盖不到的崩溃（比如 import 期就出错，根本没跑到 main()
    # 内部）在 Python 里同样以默认退出码 1 退出——跟这里的 1 撞车。同 rc==2
    # 分支已有的纪律：先核对 status.json 是不是"今天"写的、stage 确实是
    # secret_scan，不满足就不能说"密钥扫描命中"——那样会让人误以为真的
    # 泄密，实际可能只是脚本崩溃在写 status.json 之前（数据也许早已提交
    # 推送成功）。
    if [ -r "$BACKUP_STATUS_JSON" ] && jq -e --arg d "$DATE_STR" '.date == $d and .stage == "secret_scan"' "$BACKUP_STATUS_JSON" >/dev/null 2>&1; then
        log "ERROR" "🚨 Step 14：密钥扫描命中，已拒绝提交——见 ${BACKUP_STATUS_JSON}（不含密钥值）"
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step14_data_backup\": {\"status\": \"secret_scan_blocked\", \"detail_json\": \"$BACKUP_STATUS_JSON\"}}")
    else
        log "WARN" "⚠️ Step 14：退出码 1，但 $BACKUP_STATUS_JSON 不是今天写的 secret_scan 阶段——不能确认是真的密钥扫描命中（可能是脚本崩溃在写 status.json 之前），不按密钥泄露处理"
        STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step14_data_backup\": {\"status\": \"rc1_unverified_failed\", \"detail_json\": \"$BACKUP_STATUS_JSON\"}}")
    fi
elif [ $STEP14_RC -eq 2 ]; then
    # rc==2 是 init/export/git_error/commit/push 五种失败共用的退出码
    # （run_backup.py::main() 只把 secret_scan 单独映射成 1，其余一律 2）——
    # 不能只凭 rc 猜是哪一种，
    # 唯一真相是 status.json 的 stage 字段（v0.45.269 前这里曾把 rc==2 硬解读
    # 成"已提交但推送失败"，export/commit 失败——根本没提交成功——也被误报成
    # 已提交，见项目 CLAUDE.md「这个失败，下游怎么知道？」）。
    #
    # ⚠️ v0.45.273：光读 stage 不够——run_step() 自己也把 rc=2 当"脚本不存在，
    # 跳过"的哨兵值（跟这里的 rc=2 同一个数字、不同含义）。若脚本本轮根本没被
    # 调用，status.json 会是上一轮遗留的陈旧文件——先核对它的 date 等于今天，
    # 不然会把陈旧的 stage 当成这一轮的结果汇报，原样重演这次要修的误报
    # （同上方 write_status() 里 scan_timing.json 的 `.date == $d` 校验，这里
    # 对齐同一套"只认本轮日期的文件"约定）。
    if [ -r "$BACKUP_STATUS_JSON" ] && jq -e --arg d "$DATE_STR" '.date == $d' "$BACKUP_STATUS_JSON" >/dev/null 2>&1; then
        BACKUP_STAGE=$(jq -r '.stage // "unknown"' "$BACKUP_STATUS_JSON" 2>/dev/null || echo "unknown")
    else
        BACKUP_STAGE="stale_or_missing"
    fi
    case "$BACKUP_STAGE" in
        init)
            log "ERROR" "🚨 Step 14：git 仓库初始化失败，未导出未提交——见 $BACKUP_STATUS_JSON"
            ;;
        export)
            log "ERROR" "🚨 Step 14：数据导出失败，未提交——见 $BACKUP_STATUS_JSON"
            ;;
        git_error)
            log "ERROR" "🚨 Step 14：git 调用异常（如超时），未确认是否已提交/推送——见 $BACKUP_STATUS_JSON"
            ;;
        commit)
            log "ERROR" "🚨 Step 14：git commit 失败，未推送——见 $BACKUP_STATUS_JSON"
            ;;
        push)
            log "WARN" "⚠️ Step 14：已提交但推送失败，下一轮会带着未推送的提交重试——见 $BACKUP_STATUS_JSON"
            ;;
        stale_or_missing)
            log "WARN" "⚠️ Step 14：退出码 2，但 $BACKUP_STATUS_JSON 缺失或不是今天写的——脚本本轮可能根本没真正执行（如 run_step 判定脚本不存在），不代表已提交，不能当 push_failed 处理"
            ;;
        *)
            log "WARN" "⚠️ Step 14：失败（stage=$BACKUP_STAGE 未识别，rc=2）——见 $BACKUP_STATUS_JSON"
            ;;
    esac
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq --arg stage "$BACKUP_STAGE" --arg detail "$BACKUP_STATUS_JSON" \
        '. + {"step14_data_backup": {"status": ($stage + "_failed"), "detail_json": $detail}}')
elif [ $STEP14_RC -eq 124 ]; then
    log "ERROR" "⏰ Step 14 超时（>300s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step14_data_backup\": {\"status\": \"timeout\"}}")
else
    log "WARN" "⚠️ Step 14 失败（rc=${STEP14_RC}），继续进行"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step14_data_backup\": {\"status\": \"error\", \"rc\": $STEP14_RC}}")
fi

# ================================================================
# Step 15：数据备份连续性体检（v0.45.284，照抄 Step 10 的模式）
# ----------------------------------------------------------------
# 回答 Step 14 自己回答不了的问题：连续多天呢？单次 push_failed/
# stale_or_missing 不阻断、不发 Slack（噪音），但连续多天卡在同一种
# 失败/陈旧状态是聚合信号，值得被看见——同 Step 10 的判断，同样
# 刻意不动 OVERALL_STATUS、不发 Slack，只写日志与 status.json。
# ================================================================
log "INFO" ""
log "INFO" "【Step 15】数据备份连续性体检 - 启动"
STEP15_START=$(date +%s)

BACKUP_CONTINUITY_JSON="$LOGDIR/backup_continuity-$DATE_STR.json"
run_step --timeout 60 "$PROJECT_DIR/backup_continuity.py" --days 30 --quiet \
         --history "$BACKUP_HISTORY_JSONL" \
         --out "$BACKUP_CONTINUITY_JSON" >> "$LOGFILE" 2>&1
STEP15_RC=$?
STEP15_END=$(date +%s)
STEP15_DURATION=$((STEP15_END - STEP15_START))

if [ $STEP15_RC -eq 0 ]; then
    log "INFO" "✅ Step 15：备份连续性健康（耗时 ${STEP15_DURATION}s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step15_backup_continuity\": {\"status\": \"healthy\", \"duration_seconds\": $STEP15_DURATION}}")
elif [ $STEP15_RC -eq 1 ]; then
    BACKUP_CONT_SUMMARY=$("$PYTHON3" -c "
import json,sys
try:
    d=json.load(open('$BACKUP_CONTINUITY_JSON'))
except Exception as e:
    print(f'（无法解析连续性 JSON: {e}）'); sys.exit(0)
w=d.get('window',{})
print(f\"过去 {w.get('trading_days','?')} 个交易日只成功了 {d.get('backed_up_days','?')} 次\"
      f\"（覆盖率 {d.get('coverage',0):.0%}），最长空档 {d.get('longest_gap','?')} 个交易日\")
miss=d.get('weeks_missed') or []
if miss: print(f\"  完全无成功备份的周: {', '.join(miss)}\")
" 2>/dev/null)
    log "WARN" "⚠️ Step 15：备份连续性降级 — $BACKUP_CONT_SUMMARY"
    log "WARN" "   （不计入 OVERALL_STATUS：单次失败是噪音，这是聚合信号，需要人工介入排查）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step15_backup_continuity\": {\"status\": \"degraded\", \"duration_seconds\": $STEP15_DURATION, \"detail_json\": \"$BACKUP_CONTINUITY_JSON\"}}")
elif [ $STEP15_RC -eq 3 ]; then
    log "WARN" "⚠️ Step 15：无法判定连续性（找不到 backup_status_history.jsonl，可能是刚接入生产）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step15_backup_continuity\": {\"status\": \"undetermined\"}}")
elif [ $STEP15_RC -eq 2 ]; then
    log "WARN" "⏭️  Step 15 跳过（backup_continuity.py 不存在）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step15_backup_continuity\": {\"status\": \"skipped\"}}")
elif [ $STEP15_RC -eq 124 ]; then
    log "ERROR" "⏰ Step 15 超时（>60s）"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step15_backup_continuity\": {\"status\": \"timeout\"}}")
else
    log "WARN" "⚠️ Step 15 异常（exit=${STEP15_RC}），不影响主流程"
    STEPS_RESULT=$(echo "$STEPS_RESULT" | jq ". + {\"step15_backup_continuity\": {\"status\": \"error\", \"rc\": $STEP15_RC}}")
fi


log "INFO" ""
log "INFO" "【Final】写入系统状态"

TOTAL_END=$(date +%s)
TOTAL_DURATION=$((TOTAL_END - STEP1_START))

# v0.45.66：复用 write_status()（Step 6 之前已写过一次，这里写最终版，
# 含 Step 6~12 的结果）。两次写的是同一个函数，格式不会漂移。
write_status
log "INFO" "✅ 状态已保存：$REPORTDIR/status.json（status=${OVERALL_STATUS}）"

# ================================================================
# 清理旧日志（保留最近 30 天）
# ================================================================
log "INFO" ""
log "INFO" "【Cleanup】清理旧日志"
DELETED_COUNT=$(find "$LOGDIR" -name "orchestrator-*.log" -mtime +30 -delete 2>/dev/null | wc -l || echo 0)
if [ "$DELETED_COUNT" -gt 0 ]; then
    log "INFO" "✅ 清理了 $DELETED_COUNT 个旧日志文件（>30天）"
else
    log "INFO" "✅ 没有需要清理的旧日志"
fi

# ================================================================
# 总结
# ================================================================
log "INFO" ""
log "INFO" "=" >> "$LOGFILE" && echo "=" >> "$LOGFILE"
if [ "$OVERALL_STATUS" = "success" ]; then
    log "INFO" "🎉 编排流程完成"
else
    log "WARN" "🏁 编排流程结束（status=${OVERALL_STATUS}，非成功——详见末尾）"
fi
log "INFO" "📊 总耗时：${TOTAL_DURATION}s"
log "INFO" "📂 日志位置：$LOGFILE"
log "INFO" ""
log "INFO" "✅ Phase 1 流程：Step 1-5（数据采集、蜂群分析、ML 报告、仪表板、部署）"
log "INFO" "✨ Phase 2 Week 1：Step 6-7（智能告警 + Slack 推送）"
log "INFO" "✨ Phase 2 Week 2：Step 8（性能监控系统 - 时序数据库）"
log "INFO" "✨ Phase 2 Week 3：Week 3 内集成（动态蜂群扩展 - AdaptiveSpawner）"
log "INFO" "✨ Phase 2 Week 4：Step 9（信息素持久化 + T+1/T+7/T+30 准确率追踪）"

# 清理全局看门狗
kill "$_GLOBAL_WATCHDOG_PID" 2>/dev/null

# v0.45.66：横幅必须跟 OVERALL_STATUS 一致。
# 此前无论成败，末尾都先打一行「🎉 编排流程完成」（见上方 log），
# 人翻日志第一眼看到的就是它 —— 8/28 那轮零产出，读起来却像正常收工。
# 退出码本来就是对的（partial → 1），坏的一直是给人看的那句话。
if [ "$OVERALL_STATUS" = "success" ]; then
    log "INFO" "✅ 所有步骤成功"
    exit 0
elif [ "$OVERALL_STATUS" = "failed" ]; then
    log "ERROR" "🚨 本轮失败：无扫描产出 —— 网站未更新、日报未生成"
    log "ERROR" "   排查顺序：① 出站代理是否探活通过 ② Step 2 是否超时 ③ 日志里的 SSLEOFError 计数"
    exit 1
else
    log "WARN" "⚠️  部分步骤失败，但已继续完成"
    exit 1
fi
