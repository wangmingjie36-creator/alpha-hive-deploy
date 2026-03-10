#!/usr/bin/env python3
"""
📨 推送 Alpha Hive 日报到 Slack #alpha-hive

由 orchestrator Step 7 自动调用，读取当日报告 JSON 并通过
SlackReportNotifier 以富文本格式推送。

用法:
    python3 push_report_to_slack.py [--date YYYY-MM-DD] [--llm]

退出码:
    0 = 推送成功
    1 = 推送失败
    2 = 报告文件不存在（跳过）
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).parent))

from hive_logger import get_logger

_log = get_logger("push_report_to_slack")

PROJECT_DIR = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser(description="推送日报到 Slack")
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="报告日期 (默认: 今天)",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        default=False,
        help="标记为 LLM 增强模式",
    )
    args = parser.parse_args()

    date_str = args.date

    # ── 定位报告 JSON ──
    report_path = PROJECT_DIR / f"alpha-hive-daily-{date_str}.json"
    if not report_path.exists():
        _log.warning("报告文件不存在，跳过推送: %s", report_path)
        sys.exit(2)

    # ── 缓存目录 ──
    cache_dir = str(PROJECT_DIR / "cache")
    data_cache_dir = str(PROJECT_DIR / "data_cache")
    finviz_cache_dir = str(PROJECT_DIR / "finviz_cache")

    # ── 检测 LLM 模式 ──
    # 优先使用命令行参数；否则从报告 JSON 中读取
    llm_mode = args.llm
    if not llm_mode:
        try:
            import json
            with open(report_path, encoding="utf-8") as f:
                report = json.load(f)
            llm_mode = report.get("llm_mode", False)
        except Exception:
            pass

    # ── 初始化通知器 ──
    try:
        from slack_report_notifier import SlackReportNotifier
        notifier = SlackReportNotifier()
    except Exception as e:
        _log.error("SlackReportNotifier 初始化失败: %s", e)
        sys.exit(1)

    if not notifier.enabled:
        _log.warning("Slack 通知未启用（无有效 token）")
        sys.exit(1)

    # ── 推送富文本日报 ──
    _log.info("推送日报到 Slack: %s (LLM=%s)", report_path.name, llm_mode)

    ok = notifier.send_rich_daily_report(
        report_json_path=str(report_path),
        cache_dir=cache_dir,
        data_cache_dir=data_cache_dir,
        finviz_cache_dir=finviz_cache_dir,
        llm_mode=llm_mode,
    )

    if ok:
        _log.info("✅ 日报已成功推送到 Slack #alpha-hive")
        sys.exit(0)
    else:
        _log.error("❌ 日报推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
