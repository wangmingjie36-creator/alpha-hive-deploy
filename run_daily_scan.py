#!/usr/bin/env python3
"""Alpha Hive 每日扫描入口（由编排器或手动调用）

用法:
    python3 run_daily_scan.py                      # 正常蜂群扫描
    python3 run_daily_scan.py --dry-run             # 仅验证环境，不执行扫描
    python3 run_daily_scan.py --tickers NVDA TSLA   # 自定义标的
"""

import argparse
import json
import logging as _logging
import sys
import os
import time
from datetime import datetime
from pathlib import Path

_log = _logging.getLogger("alpha_hive.run_daily_scan")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_TICKERS = [
    "NVDA", "TSLA", "MSFT", "QCOM", "VKTX",
    "META", "BILI", "AMZN", "RKLB", "CRCL",
]


def _run_dry_run(tickers: list[str]) -> bool:
    """Dry-run 模式：验证 import、API 连通性、yfinance 可用，不跑完整扫描"""
    print("🔍 Dry-run 模式：验证环境...\n")
    ok = True

    # 1. 核心模块 import 检查
    checks = [
        ("alpha_hive_daily_report", "AlphaHiveDailyReporter"),
        ("swarm_agents", "BeeAgent"),
        ("slack_report_notifier", "SlackReportNotifier"),
        ("dashboard_renderer", "render_dashboard_html"),
        ("pheromone_board", "PheromoneBoard"),
        ("llm_service", None),
    ]
    for mod_name, cls_name in checks:
        try:
            mod = __import__(mod_name)
            if cls_name and not hasattr(mod, cls_name):
                print(f"  ⚠️  {mod_name}: 模块存在但缺少 {cls_name}")
                ok = False
            else:
                print(f"  ✅ {mod_name}")
        except ImportError as e:
            print(f"  ❌ {mod_name}: {e}")
            ok = False

    # 2. yfinance 可用性
    print()
    try:
        import yfinance as yf
        ticker = yf.Ticker(tickers[0])
        info = ticker.info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        if price:
            print(f"  ✅ yfinance: {tickers[0]} = ${price}")
        else:
            print(f"  ⚠️  yfinance: {tickers[0]} 无实时价格（可能盘后）")
    except Exception as e:
        print(f"  ❌ yfinance: {e}")
        ok = False

    # 3. Slack 连通性
    print()
    try:
        from slack_report_notifier import SlackReportNotifier
        notifier = SlackReportNotifier()
        if notifier.enabled:
            print(f"  ✅ Slack: 已启用（{'User Token' if notifier.use_user_token else 'Webhook'}）")
        else:
            print("  ⚠️  Slack: 未启用（无 token/webhook 或 webhook 失效）")
    except Exception as e:
        print(f"  ❌ Slack: {e}")

    # 4. 关键目录/文件检查
    print()
    project_dir = Path(__file__).parent
    key_files = [
        "templates/dashboard.html",
        "templates/dashboard.js",
        "templates/dashboard.css",
    ]
    for f in key_files:
        fp = project_dir / f
        if fp.exists():
            print(f"  ✅ {f}")
        else:
            print(f"  ❌ {f} 不存在")
            ok = False

    # 5. 汇总
    print(f"\n{'='*50}")
    if ok:
        print("✅ Dry-run 通过：环境就绪，可以执行扫描")
    else:
        print("⚠️  Dry-run 发现问题，请修复后再运行")
    print(f"{'='*50}")
    return ok


def _cleanup_stale_data(project_dir: Path, max_cache_days: int = 7,
                        max_swarm_days: int = 14, max_db_days: int = 30) -> None:
    """启动时清理过期缓存/结果文件/数据库条目"""
    import sqlite3
    now = time.time()
    cleaned = 0

    # 1. 缓存目录：删除 >max_cache_days 天的文件
    cache_dirs = [
        "cache", "data_cache", "sec_cache", "polymarket_cache",
        "finviz_cache", "reddit_cache", "earnings_cache",
    ]
    for dirname in cache_dirs:
        cache_path = project_dir / dirname
        if not cache_path.is_dir():
            continue
        cutoff = now - max_cache_days * 86400
        for fp in cache_path.iterdir():
            if fp.is_file() and fp.stat().st_mtime < cutoff:
                try:
                    fp.unlink()
                    cleaned += 1
                except OSError:
                    pass

    # 2. .swarm_results_*.json 轮转：删除 >max_swarm_days 天
    cutoff_swarm = now - max_swarm_days * 86400
    for fp in project_dir.glob(".swarm_results_*.json"):
        if fp.stat().st_mtime < cutoff_swarm:
            try:
                fp.unlink()
                cleaned += 1
            except OSError:
                pass

    # 3. pheromone.db 清理：删除 >max_db_days 天的旧条目 + VACUUM
    db_path = project_dir / "pheromone.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            cutoff_iso = datetime.fromtimestamp(now - max_db_days * 86400).isoformat()
            cur = conn.execute(
                "DELETE FROM pheromone_signals WHERE timestamp < ?", (cutoff_iso,)
            )
            db_cleaned = cur.rowcount
            if db_cleaned > 0:
                conn.execute("VACUUM")
                cleaned += db_cleaned
            conn.commit()
            conn.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            _log.debug("pheromone.db 清理跳过: %s", e)

    if cleaned > 0:
        _log.info("清理了 %d 个过期文件/记录", cleaned)


def _run_scan(tickers: list[str]) -> None:
    """执行完整蜂群扫描"""
    from alpha_hive_daily_report import AlphaHiveDailyReporter
    from slack_report_notifier import SlackReportNotifier

    start_time = time.time()
    print(f"\nAlpha Hive 蜂群启动 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 启动前清理过期数据
    _cleanup_stale_data(Path(__file__).parent)

    reporter = AlphaHiveDailyReporter()
    notifier = SlackReportNotifier()

    print(f"扫描标的: {', '.join(tickers)}\n")

    report = reporter.run_swarm_scan(tickers)

    duration = time.time() - start_time
    error_msg = None

    if report and "opportunities" in report:
        opportunities = report["opportunities"]
        print(f"\n{'='*70}")
        print(f"扫描完成！发现 {len(opportunities)} 个机会")
        print(f"{'='*70}\n")
        for i, opp in enumerate(opportunities[:3], 1):
            t = opp.ticker if hasattr(opp, "ticker") else opp.get("ticker", "?")
            d = opp.direction if hasattr(opp, "direction") else opp.get("direction", "?")
            s = opp.opportunity_score if hasattr(opp, "opportunity_score") else opp.get("opp_score", 0)
            print(f"  {i}. {t}: {d} ({s:.1f}/10)")
        n_opps = len(opportunities)
    else:
        n_opps = 0

    # 写入运行状态（Sprint 1.4）
    _write_status(
        status="success",
        duration=duration,
        tickers=tickers,
        n_opportunities=n_opps,
        error=error_msg,
    )

    print("\n蜂群扫描完成！")


def _write_status(
    status: str,
    duration: float,
    tickers: list[str],
    n_opportunities: int = 0,
    error: str | None = None,
) -> None:
    """写入 logs/last_run_status.json（Sprint 1.4）"""
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    status_file = log_dir / "last_run_status.json"

    data = {
        "timestamp": datetime.now().isoformat(),
        "status": status,
        "duration_seconds": round(duration, 1),
        "tickers": tickers,
        "n_tickers": len(tickers),
        "n_opportunities": n_opportunities,
    }
    if error:
        data["error"] = error[:500]

    try:
        status_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        _log.info("状态写入: %s", status_file)
    except OSError as e:
        _log.warning("无法写入状态文件: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Alpha Hive 每日扫描入口")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅验证环境（import、API、yfinance），不执行扫描",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=DEFAULT_TICKERS,
        help=f"扫描标的列表（默认: {' '.join(DEFAULT_TICKERS)}）",
    )
    args = parser.parse_args()

    if args.dry_run:
        ok = _run_dry_run(args.tickers)
        sys.exit(0 if ok else 1)

    try:
        _run_scan(args.tickers)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        _log.error("扫描失败: %s", e, exc_info=True)
        print(f"\n扫描失败: {e}\n")
        import traceback
        traceback.print_exc()

        _write_status(
            status="failed",
            duration=0,
            tickers=args.tickers,
            error=str(e),
        )

        # 扫描失败仅写日志，不发 Slack DM
        sys.exit(1)


if __name__ == "__main__":
    main()
