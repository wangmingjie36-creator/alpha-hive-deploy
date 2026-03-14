#!/usr/bin/env python3
"""
📨 推送 Alpha Hive 日报到 Slack #alpha-hive

【架构说明 - 2026-03-13】
频道推送改由 Claude Code 内置 Slack MCP（用户 OAuth 账号）完成，
Bot Token (xoxb-) 仅保留用于 DM 交互式通知（pre_scan_notify.py）。

原因：Bot Token 未被邀请进 #alpha-hive 频道，自动降级到 DM，
与频道推送意图不符。Slack MCP 使用用户账号，天然有频道权限。

此脚本保留供手动调试用途；orchestrator Step 7 调用时直接返回 2（跳过），
orchestrator 日志会显示"Step 7 跳过（Slack 推送由 Claude Code MCP 手动执行）"。

用法（手动调试）:
    python3 push_report_to_slack.py [--date YYYY-MM-DD] [--llm] [--force]

退出码:
    0 = 推送成功
    1 = 推送失败
    2 = 跳过（默认；或报告文件不存在）
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
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="强制执行（跳过默认的 skip 模式，供手动调试使用）",
    )
    args = parser.parse_args()

    # ── 默认跳过：频道推送由 Claude Code Slack MCP 完成 ──
    # orchestrator Step 7 调用此脚本时，RC=2 → 日志显示"跳过（Slack 推送由 Claude Code MCP 手动执行）"
    # 手动调试时传入 --force 可绕过此跳过逻辑
    if not args.force:
        _log.info("⏭️  Step 7 跳过：频道推送由 Claude Code Slack MCP 负责（传 --force 可强制执行）")
        sys.exit(2)

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
