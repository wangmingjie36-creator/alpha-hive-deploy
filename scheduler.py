#!/usr/bin/env python3
"""
🐝 Alpha Hive - 自动化定时任务调度器
支持定时采集数据和生成报告

P0: 新增 backfill_prices() —— T+1/T+7/T+30 反馈循环价格回填
P1-d: 时区安全（zoneinfo）+ 子进程重试 + 任务防重叠
"""

import schedule
import time
import subprocess
import threading
from datetime import datetime
import logging
import os

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

_PROJECT_ROOT = os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.abspath(__file__)))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(_PROJECT_ROOT, 'scheduler.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== 时区工具 ====================

_ET = ZoneInfo("America/New_York")


def _et_to_local(et_time_str: str) -> str:
    """将 ET（美东）时间字符串转换为本地系统时间（HH:MM）。

    例如：系统在 UTC+8 时，_et_to_local("17:30") → "05:30"（次日）
    """
    today = datetime.now(_ET).date()
    et_dt = datetime.combine(
        today,
        datetime.strptime(et_time_str, "%H:%M").time(),
        tzinfo=_ET,
    )
    local_dt = et_dt.astimezone()  # 转为系统本地时区
    return local_dt.strftime("%H:%M")


# ==================== 子进程重试 ====================

def _run_with_retry(cmd, timeout=60, max_retries=2):
    """带重试的 subprocess 调用（指数退避）。

    Returns:
        subprocess.CompletedProcess 或 None（全部失败后）
    """
    result = None
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                return result
            if attempt < max_retries:
                delay = 5 * (attempt + 1)
                logger.warning(
                    "重试 %d/%d (%s): returncode=%d, 等待 %ds",
                    attempt + 1, max_retries, " ".join(cmd), result.returncode, delay,
                )
                time.sleep(delay)
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning("子进程异常 %d/%d (%s): %s", attempt + 1, max_retries, " ".join(cmd), e)
            if attempt >= max_retries:
                return None
            time.sleep(5 * (attempt + 1))
    return result


# ==================== 任务防重叠 ====================

_running_tasks: set = set()
_task_lock = threading.Lock()


def _guarded(task_name, func):
    """防止同一任务重叠执行。若上次仍在运行则跳过本次。"""
    def wrapper():
        with _task_lock:
            if task_name in _running_tasks:
                logger.warning("跳过 %s（上次仍在运行）", task_name)
                return
            _running_tasks.add(task_name)
        try:
            func()
        finally:
            with _task_lock:
                _running_tasks.discard(task_name)
    return wrapper


# ==================== 调度器核心 ====================

class ReportScheduler:
    """报告生成调度器"""

    def __init__(self):
        self.data_collected = False
        self.report_generated = False
        self._earnings_watcher = None

    def _get_earnings_watcher(self):
        """懒加载 EarningsWatcher"""
        if self._earnings_watcher is None:
            try:
                from earnings_watcher import EarningsWatcher
                self._earnings_watcher = EarningsWatcher()
            except ImportError:
                logger.warning("earnings_watcher 模块不可用")
        return self._earnings_watcher

    def collect_data(self):
        """采集实时数据（带重试）"""
        logger.info("📊 开始采集实时数据...")
        result = _run_with_retry(['python3', 'data_fetcher.py'], timeout=60)
        if result and result.returncode == 0:
            logger.info("✅ 数据采集成功")
            self.data_collected = True
        else:
            stderr = result.stderr if result else "子进程异常"
            logger.error("❌ 数据采集失败: %s", stderr)
            self.data_collected = False

    def generate_reports(self):
        """生成优化报告（带重试）"""
        if not self.data_collected:
            logger.warning("⚠️ 跳过报告生成（数据未采集）")
            return

        logger.info("📝 开始生成优化报告...")
        result = _run_with_retry(
            ['python3', 'generate_report_with_realtime_data.py'], timeout=60,
        )
        if result and result.returncode == 0:
            logger.info("✅ 报告生成成功")
            self.report_generated = True
        else:
            stderr = result.stderr if result else "子进程异常"
            logger.error("❌ 报告生成失败: %s", stderr)
            self.report_generated = False

    def upload_to_github(self):
        """上传报告到 GitHub"""
        if not self.report_generated:
            logger.warning("⚠️ 跳过上传（报告未生成）")
            return

        logger.info("🚀 上传报告到 GitHub...")
        try:
            commands = [
                ['git', 'add', 'alpha-hive-*-realtime-*.html', 'realtime_metrics.json'],
                ['git', 'commit', '-m', f"🔄 实时报告更新 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"],
                ['git', 'push', 'origin', 'main'],
            ]

            for cmd in commands:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode != 0 and 'nothing to commit' not in result.stderr:
                    logger.warning(f"⚠️ Git 操作失败: {result.stderr}")
                    return

            logger.info("✅ 报告已上传到 GitHub")
        except (subprocess.SubprocessError, OSError) as e:
            logger.error(f"❌ 上传异常: {e}", exc_info=True)

    def check_earnings(self):
        """检查今日是否有 watchlist 标的发布财报，若有则抓取结果并更新简报"""
        logger.info("📊 检查今日财报...")
        watcher = self._get_earnings_watcher()
        if watcher is None:
            return

        try:
            from config import WATCHLIST
            tickers = list(WATCHLIST.keys())
            result = watcher.check_and_update(tickers)

            reporting = result.get("reporting_today", [])
            updated = result.get("updated", [])
            if reporting:
                logger.info("📊 今日财报标的: %s", ", ".join(reporting))
                if updated:
                    logger.info("✅ 简报已自动更新: %s", ", ".join(updated))
                    # 更新后自动推送到 GitHub
                    self.upload_to_github()
            else:
                logger.info("📊 今日无 watchlist 财报")
        except (ImportError, OSError, ValueError) as e:
            logger.error(f"❌ 财报检查异常: {e}", exc_info=True)

    def full_pipeline(self):
        """完整的数据采集 -> 报告生成 -> 财报检查 -> 上传流程"""
        logger.info("=" * 60)
        logger.info("🔄 启动完整流程")
        logger.info("=" * 60)

        self.collect_data()
        self.generate_reports()
        self.check_earnings()
        self.upload_to_github()

        logger.info("=" * 60)
        logger.info("✅ 流程完成")
        logger.info("=" * 60)

    def health_check(self):
        """系统健康检查"""
        logger.info("🏥 执行健康检查...")
        try:
            files = [
                'data_fetcher.py',
                'generate_report_with_realtime_data.py',
                'realtime_metrics.json',
            ]

            all_ok = True
            for file in files:
                if os.path.exists(file):
                    logger.info(f"✅ {file} 存在")
                else:
                    logger.warning(f"⚠️ {file} 不存在")
                    all_ok = False

            if all_ok:
                logger.info("✅ 系统健康")
            else:
                logger.warning("⚠️ 部分文件缺失")

        except (OSError, ValueError) as e:
            logger.error(f"❌ 健康检查失败: {e}", exc_info=True)

    # ==================== P0: 反馈循环价格回填 ====================

    def backfill_prices(self):
        """回填 T+1/T+7/T+30 实际价格到历史快照（供反馈循环回测）。

        每日盘后运行一次，扫描 report_snapshots/ 中所有快照：
        - 距今 >= 1 天且 actual_price_t1 为空 → 回填 T+1 收盘价
        - 距今 >= 7 天且 actual_price_t7 为空 → 回填 T+7 收盘价
        - 距今 >= 30 天且 actual_price_t30 为空 → 回填 T+30 收盘价
        """
        try:
            from feedback_loop import ReportSnapshot
            import yfinance as yf
            from datetime import timedelta
        except ImportError:
            logger.debug("feedback_loop 或 yfinance 不可用，跳过价格回填")
            return

        snapshot_dir = os.path.join(_PROJECT_ROOT, "report_snapshots")
        if not os.path.exists(snapshot_dir):
            return

        today = datetime.now().date()
        updated = 0
        for fn in os.listdir(snapshot_dir):
            if not fn.endswith(".json"):
                continue
            try:
                fpath = os.path.join(snapshot_dir, fn)
                snap = ReportSnapshot.load_from_json(fpath)
                snap_date = datetime.strptime(snap.date, "%Y-%m-%d").date()
                days = (today - snap_date).days
                needs_save = False
                ticker_obj = None

                for _tf, min_days, attr in [
                    ("t1", 1, "actual_price_t1"),
                    ("t7", 7, "actual_price_t7"),
                    ("t30", 30, "actual_price_t30"),
                ]:
                    if days >= min_days and getattr(snap, attr) is None:
                        ticker_obj = ticker_obj or yf.Ticker(snap.ticker)
                        start = snap_date + timedelta(days=min_days - 1)
                        end = snap_date + timedelta(days=min_days + 3)
                        hist = ticker_obj.history(start=start, end=end)
                        if not hist.empty:
                            setattr(snap, attr, float(hist["Close"].iloc[0]))
                            needs_save = True

                if needs_save:
                    snap.save_to_json(snapshot_dir)
                    updated += 1
            except Exception as e:
                logger.debug("快照回填失败 %s: %s", fn, e)

        if updated:
            logger.info("反馈循环: 回填 %d 个快照价格", updated)


def setup_scheduler():
    """设置定时任务（所有固定时间均经 ET→本地时区转换）"""
    scheduler = ReportScheduler()

    # 每 5 分钟采集一次数据（高频更新关键指标）
    schedule.every(5).minutes.do(scheduler.collect_data)

    # 每 15 分钟生成一次报告
    schedule.every(15).minutes.do(scheduler.generate_reports)

    # 每 30 分钟上传一次到 GitHub
    schedule.every(30).minutes.do(scheduler.upload_to_github)

    # 每小时执行一次完整流程（防重叠包装）
    schedule.every(1).hours.do(_guarded("full_pipeline", scheduler.full_pipeline))

    # 盘后财报检查（每日 17:30 和 19:00 ET 各检查一次，覆盖 AMC 财报发布窗口）
    schedule.every().day.at(_et_to_local("17:30")).do(scheduler.check_earnings)
    schedule.every().day.at(_et_to_local("19:00")).do(scheduler.check_earnings)

    # 盘前财报检查（每日 07:00 ET，覆盖 BMO 财报）
    schedule.every().day.at(_et_to_local("07:00")).do(scheduler.check_earnings)

    # 反馈循环：盘后价格回填（每日 16:15 ET）
    schedule.every().day.at(_et_to_local("16:15")).do(scheduler.backfill_prices)

    # 每 6 小时执行一次健康检查
    schedule.every(6).hours.do(scheduler.health_check)

    logger.info("✅ 定时任务已配置")
    logger.info("  📊 数据采集: 每 5 分钟")
    logger.info("  📝 报告生成: 每 15 分钟")
    logger.info("  🚀 GitHub 上传: 每 30 分钟")
    logger.info("  🔄 完整流程: 每 1 小时（防重叠）")
    logger.info("  💰 财报检查: 07:00 / 17:30 / 19:00 ET")
    logger.info("  🔁 价格回填: 16:15 ET")
    logger.info("  🏥 健康检查: 每 6 小时")

    return scheduler


def run_scheduler(scheduler):
    """运行调度器（阻塞）"""
    logger.info("🚀 调度器已启动，等待任务触发...")
    logger.info("按 Ctrl+C 停止")

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每 60 秒检查一次待执行任务
    except KeyboardInterrupt:
        logger.info("⏹️ 调度器已停止")


# ==================== 快速脚本 ====================
def run_once():
    """一次性执行完整流程（用于测试或手动触发）"""
    logger.info("🔄 一次性执行完整流程")
    scheduler = ReportScheduler()
    scheduler.full_pipeline()


# ==================== 定时任务（Cron）====================
def print_cron_commands():
    """输出可用的 Cron 命令"""
    print("""
# ==================== Cron 配置示例 ====================
# 编辑 crontab: crontab -e

# 每 5 分钟采集数据
*/5 * * * * cd /Users/igg/.claude/reports && python3 data_fetcher.py >> logs/cron.log 2>&1

# 每 15 分钟生成报告
*/15 * * * * cd /Users/igg/.claude/reports && python3 generate_report_with_realtime_data.py >> logs/cron.log 2>&1

# 每 30 分钟上传到 GitHub
*/30 * * * * cd /Users/igg/.claude/reports && git add alpha-hive-*-realtime-*.html realtime_metrics.json && git commit -m "🔄 自动更新" && git push origin main >> logs/cron.log 2>&1

# 每天早上 6 点执行完整流程
0 6 * * * cd /Users/igg/.claude/reports && python3 -c "from scheduler import run_once; run_once()" >> logs/cron.log 2>&1

# 每天晚上 22 点执行健康检查
0 22 * * * cd /Users/igg/.claude/reports && python3 -c "from scheduler import ReportScheduler; ReportScheduler().health_check()" >> logs/cron.log 2>&1

# ==================== 设置步骤 ====================
# 1. 创建日志目录
#    mkdir -p /Users/igg/.claude/reports/logs

# 2. 编辑 crontab
#    crontab -e

# 3. 粘贴上面的命令

# 4. 保存并验证
#    crontab -l

# ==================== 查看日志 ====================
# tail -f /Users/igg/.claude/reports/logs/cron.log

# ==================== 删除 Cron 任务 ====================
# crontab -r
    """)


# ==================== 主程序 ====================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "once":
            # 一次性执行
            run_once()
        elif sys.argv[1] == "daemon":
            # 后台守护进程模式
            scheduler = setup_scheduler()
            run_scheduler(scheduler)
        elif sys.argv[1] == "cron":
            # 显示 Cron 配置
            print_cron_commands()
        else:
            print("用法:")
            print("  python3 scheduler.py once      # 一次性执行")
            print("  python3 scheduler.py daemon    # 后台运行（推荐）")
            print("  python3 scheduler.py cron      # 显示 Cron 配置")
    else:
        # 默认：后台守护进程模式
        scheduler = setup_scheduler()
        run_scheduler(scheduler)
