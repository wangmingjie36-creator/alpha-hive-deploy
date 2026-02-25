#!/usr/bin/env python3
"""Alpha Hive 每日扫描入口（由 run_alpha_hive_daily.sh 调用）"""

import logging as _logging
import sys
import os
from datetime import datetime

_log = _logging.getLogger("alpha_hive.run_daily_scan")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from alpha_hive_daily_report import AlphaHiveDailyReporter
    from slack_report_notifier import SlackReportNotifier

    print(f"\nAlpha Hive 蜂群启动 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    reporter = AlphaHiveDailyReporter()
    notifier = SlackReportNotifier()

    if notifier.enabled:
        notifier.send_risk_alert(
            alert_title="Alpha Hive 每日扫描开始",
            alert_message=f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n扫描标的: NVDA, TSLA, MSFT, AMD, QCOM, VKTX",
            severity="MEDIUM"
        )

    SCAN_TICKERS = ['NVDA', 'TSLA', 'MSFT', 'AMD', 'QCOM', 'VKTX']
    print(f"扫描标的: {', '.join(SCAN_TICKERS)}\n")

    report = reporter.run_swarm_scan(SCAN_TICKERS)

    if report and 'opportunities' in report:
        opportunities = report['opportunities']
        print(f"\n{'='*70}")
        print(f"扫描完成！发现 {len(opportunities)} 个机会")
        print(f"{'='*70}\n")
        for i, opp in enumerate(opportunities[:3], 1):
            t = opp.ticker if hasattr(opp, 'ticker') else opp.get('ticker', '?')
            d = opp.direction if hasattr(opp, 'direction') else opp.get('direction', '?')
            s = opp.opportunity_score if hasattr(opp, 'opportunity_score') else opp.get('opp_score', 0)
            print(f"  {i}. {t}: {d} ({s:.1f}/10)")

    if notifier.enabled:
        notifier.send_risk_alert(
            alert_title="Alpha Hive 每日扫描完成",
            alert_message=f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n报告已推送 Slack",
            severity="LOW"
        )

    print("\n蜂群扫描完成！")

except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
    _log.error("扫描失败: %s", e, exc_info=True)
    print(f"\n扫描失败: {e}\n")
    import traceback
    traceback.print_exc()

    try:
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        if n.enabled:
            n.send_risk_alert(
                alert_title="Alpha Hive 扫描失败",
                alert_message=f"错误: {str(e)[:200]}\n请检查日志",
                severity="CRITICAL"
            )
    except (ConnectionError, TimeoutError, OSError, ValueError) as exc:
        _log.debug("Slack 失败通知发送失败: %s", exc)

    sys.exit(1)
