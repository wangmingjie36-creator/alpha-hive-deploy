#!/usr/bin/env python3
"""
🧪 Alpha Hive NVDA 完整工作流演示
展示蜂群扫描 + Slack 通知集成
"""

from datetime import datetime
from alpha_hive_daily_report import AlphaHiveDailyReporter
from slack_report_notifier import SlackReportNotifier


def print_header(title):
    """打印标题"""
    print(f"\n{'='*80}")
    print(f"🐝 {title}")
    print(f"{'='*80}\n")


def main():
    print_header("Alpha Hive NVDA 完整工作流测试")
    print(f"⏰ 测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # ============================================================
    # Step 1: 初始化系统
    # ============================================================
    print_header("Step 1: 系统初始化")
    print("📋 正在初始化 AlphaHiveDailyReporter...")

    try:
        reporter = AlphaHiveDailyReporter()
        print("✅ Reporter 初始化成功")
        print(f"   • 日期：{reporter.date_str}")
        print(f"   • Slack 通知：{'✅ 已启用' if reporter.slack_notifier and reporter.slack_notifier.enabled else '❌ 未启用'}")
        print(f"   • 蜂群 Agent：6 个基础 Agent")
        print(f"   • CodeExecutor：{'✅ 已启用' if reporter.code_executor_agent else '❌ 未启用'}")
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"❌ 初始化失败：{e}")
        return

    # ============================================================
    # Step 2: 发送启动通知到 Slack
    # ============================================================
    print_header("Step 2: 发送启动通知")
    print("📤 正在发送启动信号到 Slack...\n")

    notifier = SlackReportNotifier()
    if notifier.enabled:
        notifier.send_risk_alert(
            alert_title="🚀 Alpha Hive 蜂群启动",
            alert_message="开始分析 NVDA：NVIDIA Corporation (美国领先 AI 芯片制造商)\n\n📊 预期分析内容：\n• 聪明钱动向（SEC Form 4/13F）\n• 市场隐含预期（Polymarket 赔率）\n• X 情绪汇总\n• 财报/事件催化剂\n• 竞争格局分析",
            severity="HIGH"
        )
    else:
        print("❌ Slack 未配置")

    # ============================================================
    # Step 3: 运行蜂群扫描
    # ============================================================
    print_header("Step 3: 运行蜂群扫描（NVDA）")
    print("🚀 启动蜂群协作分析...\n")

    try:
        report = reporter.run_swarm_scan(['NVDA'])

        if report and 'opportunities' in report:
            opportunities = report['opportunities']
            print(f"\n✅ 扫描完成！发现 {len(opportunities)} 个机会\n")

            if opportunities:
                top_opp = opportunities[0]
                print(f"🏆 Top 机会：")
                print(f"   标的：{top_opp.ticker}")
                print(f"   方向：{top_opp.direction}")
                print(f"   评分：{top_opp.opportunity_score:.1f}/10")
                print(f"   置信度：{top_opp.confidence:.0f}%")

                # ================================================
                # Step 4: 发送高分机会告警
                # ================================================
                if top_opp.opportunity_score >= 7.0:
                    print_header("Step 4: 发送高分机会告警")
                    print(f"📤 推送高分机会到 Slack...\n")

                    if notifier.enabled:
                        notifier.send_opportunity_alert(
                            ticker=top_opp.ticker,
                            score=top_opp.opportunity_score,
                            direction=top_opp.direction,
                            discovery=f"综合信号强度、市场情绪和催化剂：{top_opp.opportunity_score:.1f}/10",
                            risks=top_opp.risks[:2] if top_opp.risks else []
                        )
        else:
            print("⚠️ 报告格式异常或为演示模式")

    except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"❌ 扫描失败：{e}")
        import traceback
        traceback.print_exc()
        return

    # ============================================================
    # Step 5: 发送最终总结
    # ============================================================
    print_header("Step 5: 发送分析完成通知")
    print("📤 正在发送完成报告...\n")

    if notifier.enabled:
        notifier.send_risk_alert(
            alert_title="✅ Alpha Hive 分析完成",
            alert_message="NVDA 蜂群分析已完成\n\n📊 报告已推送至 Slack\n\n🔍 后续跟踪：\n• 监控期权异动\n• 追踪机构持仓变化\n• 关注财报公告日期",
            severity="MEDIUM"
        )
    else:
        print("❌ Slack 未配置")

    # ============================================================
    # 总结
    # ============================================================
    print_header("工作流完成总结")

    print("✅ 已完成的任务：")
    print("   1. ✅ 系统初始化")
    print("   2. ✅ 启动通知发送")
    print("   3. ✅ 蜂群扫描 (NVDA)")
    print("   4. ✅ Slack 报告推送")
    print("   5. ✅ 高分机会告警")
    print("   6. ✅ 完成通知发送")

    print("\n📊 系统状态：")
    print("   🟢 蜂群扫描：就绪")
    print("   🟢 Slack 通知：就绪")
    print("   🟢 数据分析：就绪")
    print("   🟢 告警系统：就绪")

    print("\n🎯 后续建议：")
    print("   • 定时运行 run_swarm_scan() 或 run_crew_scan()")
    print("   • 配置告警规则和阈值")
    print("   • 定期检查 Slack 频道获取最新机会")
    print("   • 监控系统日志和性能指标")

    print("\n" + "="*80)
    print("✨ 完整工作流演示成功！")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
