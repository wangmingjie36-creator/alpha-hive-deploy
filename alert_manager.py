#!/usr/bin/env python3
"""
🔔 Alpha Hive 智能告警系统
实时异常检测 + 多渠道通知 + 智能优先级排序
"""

import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict
from enum import Enum

from hive_logger import PATHS, get_logger

_log = get_logger("alerts")


class AlertLevel(Enum):
    """告警级别"""
    CRITICAL = "CRITICAL"  # P0: 系统完全失败
    HIGH = "HIGH"          # P1: 关键步骤失败
    MEDIUM = "MEDIUM"      # P2: 性能下降/低分报告
    INFO = "INFO"          # 信息提示


# 中文告警消息
ALERT_MESSAGES_CN = {
    'pipeline_failed': '完整流程失败',
    'step_failed': '步骤失败',
    'performance_degradation': '性能异常',
    'no_report': '未生成报告',
    'low_scores': '机会评分偏低',
    'very_low_top': '最高分过低',
    'deployment_failed': 'GitHub 部署失败'
}


class Alert:
    """告警对象"""

    def __init__(self, level: AlertLevel, message: str, details: Dict = None, tags: List[str] = None):
        self.level = level
        self.message = message
        self.details = details or {}
        self.tags = tags or []
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> Dict:
        return {
            "level": self.level.value,
            "message": self.message,
            "details": self.details,
            "tags": self.tags,
            "timestamp": self.timestamp
        }

    def to_slack_format(self) -> Dict:
        """转换为 Slack 消息格式"""
        color_map = {
            AlertLevel.CRITICAL: "#FF0000",
            AlertLevel.HIGH: "#FF9900",
            AlertLevel.MEDIUM: "#FFCC00",
            AlertLevel.INFO: "#0099FF"
        }

        emoji_map = {
            AlertLevel.CRITICAL: "🚨",
            AlertLevel.HIGH: "⚠️ ",
            AlertLevel.MEDIUM: "⏱️ ",
            AlertLevel.INFO: "ℹ️ "
        }

        return {
            "color": color_map[self.level],
            "pretext": f"{emoji_map[self.level]} {self.level.value}",
            "title": self.message,
            "fields": [
                {"title": key, "value": str(value), "short": True}
                for key, value in self.details.items()
            ],
            "ts": int(datetime.fromisoformat(self.timestamp).timestamp())
        }


class AlertAnalyzer:
    """告警分析引擎"""

    def __init__(self, report_dir: Path = None, perf_baseline_seconds: float = 5.0):
        self.report_dir = report_dir or PATHS.home
        self.perf_baseline = perf_baseline_seconds
        self.alerts: List[Alert] = []
        # v0.45.47：记录**哪些检查没能执行**。
        # 「零告警」有两种完全不同的成因——「查过了，没问题」与「根本没查成」，
        # 而旧实现把两者都渲染成 "No alerts detected - system healthy"。
        # 告警系统自己静默失效，是最不该发生的一种静默失效。
        self.checks_skipped: List[str] = []

    def analyze(self, status_json_path: Path) -> List[Alert]:
        """分析执行结果并生成告警"""
        self.alerts = []
        self.checks_skipped = []

        try:
            with open(status_json_path, 'r', encoding='utf-8') as f:
                status = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # 无法读取 status.json
            _log.error("Failed to read status.json: %s", e, exc_info=True)
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "Cannot read status.json",
                {"error": str(e)},
                ["system", "file_io"]
            ))
            return self.alerts

        # 1. 检测 P0: 总体失败
        if status.get('status') == 'failed':
            self.alerts.append(Alert(
                AlertLevel.CRITICAL,
                "🚨 【P0 严重】完整流程失败",
                {
                    "系统状态": "失败",
                    "影响范围": "全部步骤",
                    "建议": "立即查看日志，排查根本原因"
                },
                ["critical", "pipeline"]
            ))
            return self.alerts  # P0 优先返回

        # 2. 检测 P1: 步骤失败
        # v0.45.47：`.get('steps_result', {})` 拿到空 dict 时循环直接不执行，
        # 于是「一个失败步骤都没有」与「编排器没写这个字段」产出完全相同的结果。
        steps_result = status.get('steps_result')
        if not isinstance(steps_result, dict) or not steps_result:
            self.checks_skipped.append("步骤失败检查（status.json 缺 steps_result）")
            _log.warning("status.json 无 steps_result —— **步骤失败检查未执行**，"
                         "本次「无告警」不等于「无失败」")
            steps_result = {}
        for step_name, step_result in steps_result.items():
            if step_result.get('status') == 'failed':
                self.alerts.append(Alert(
                    AlertLevel.HIGH,
                    f"⚠️ 【P1 高】步骤失败：{step_name}",
                    {
                        "步骤": step_name,
                        "耗时": f"{step_result.get('duration_seconds', 'N/A')}秒",
                        "状态": "失败"
                    },
                    ["step_failure", step_name]
                ))

        # 3. 检测 P1: 性能异常 (>150% baseline)
        total_duration = status.get('total_duration_seconds', 0)
        if total_duration > self.perf_baseline * 1.5:
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "⚠️ 【P1 高】性能异常",
                {
                    "实际耗时": f"{total_duration}秒",
                    "基线耗时": f"{self.perf_baseline}秒",
                    "性能下降": f"{(total_duration / self.perf_baseline - 1) * 100:.1f}%",
                    "建议": "检查系统负载，优化缓慢的步骤"
                },
                ["performance"]
            ))

        # 4. 检测 P1: 数据异常 (无报告生成)
        report_file = self.report_dir / f"alpha-hive-daily-{datetime.now().strftime('%Y-%m-%d')}.md"
        if not report_file.exists():
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "⚠️ 【P1 高】未生成日报",
                {
                    "预期文件": report_file.name,
                    "当前状态": "文件不存在"
                },
                ["data_quality"]
            ))

        # 5. 检测 P2: 低分报告
        try:
            json_report_file = self.report_dir / f"alpha-hive-daily-{datetime.now().strftime('%Y-%m-%d')}.json"
            if json_report_file.exists():
                with open(json_report_file, 'r', encoding='utf-8') as f:
                    report = json.load(f)

                opportunities = report.get('opportunities', [])
                if opportunities:
                    top_score = opportunities[0].get('opp_score', 0)
                    avg_score = sum(o.get('opp_score', 0) for o in opportunities) / len(opportunities)

                    if avg_score < 6.0:
                        self.alerts.append(Alert(
                            AlertLevel.MEDIUM,
                            "📊 【P2 中】机会评分偏低",
                            {
                                "最高分": f"{top_score:.1f}/10",
                                "平均分": f"{avg_score:.1f}/10",
                                "解释": "当前市场交易机会有限"
                            },
                            ["data_quality", "market"]
                        ))

                    if top_score < 5.0:
                        self.alerts.append(Alert(
                            AlertLevel.MEDIUM,
                            "📉 【P2 中】最高分过低",
                            {
                                "top_score": f"{top_score:.1f}/10",
                                "recommendation": "Consider expanding analysis scope or monitoring period"
                            },
                            ["data_quality"]
                        ))
        except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError) as e:
            # v0.45.47：debug → warning，并记入 checks_skipped。
            # 这个 except 覆盖了 KeyError/ValueError/TypeError —— 日报 JSON 结构
            # 稍有变化（opportunities 从 list 变 dict 等）就会整块跳过 P2 低分检查，
            # 而调用方只看到「零告警」。
            self.checks_skipped.append(f"低分/数据质量检查（报告解析失败：{type(e).__name__}）")
            _log.warning("日报 JSON 解析失败，**低分与数据质量检查未执行**：%s: %s",
                         type(e).__name__, e)

        # 6. 检测 P1/P2: GitHub 部署失败
        if status.get('deploy_status') == 'failed':
            deploy_msg = status.get('deploy_message', 'Unknown error')
            alert_level = AlertLevel.HIGH if 'Authentication' in deploy_msg else AlertLevel.MEDIUM
            self.alerts.append(Alert(
                alert_level,
                "GitHub Deployment Failed",
                {
                    "status": "FAILED",
                    "error": deploy_msg,
                    "recommendation": "Check GitHub token and repository access"
                },
                ["deployment", "github"]
            ))

        return self.alerts

    def get_critical_alerts(self) -> List[Alert]:
        """获取 P0 级别告警"""
        return [a for a in self.alerts if a.level == AlertLevel.CRITICAL]

    def get_high_alerts(self) -> List[Alert]:
        """获取 P1 级别告警"""
        return [a for a in self.alerts if a.level == AlertLevel.HIGH]

    def get_medium_alerts(self) -> List[Alert]:
        """获取 P2 级别告警"""
        return [a for a in self.alerts if a.level == AlertLevel.MEDIUM]

    def save_alerts(self, output_path: Path) -> None:
        """保存告警到文件"""
        alerts_data = {
            "timestamp": datetime.now().isoformat(),
            "total_alerts": len(self.alerts),
            "critical_count": len(self.get_critical_alerts()),
            "high_count": len(self.get_high_alerts()),
            "medium_count": len(self.get_medium_alerts()),
            "alerts": [a.to_dict() for a in self.alerts]
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(alerts_data, f, ensure_ascii=False, indent=2)


class AlertDispatcher:
    """告警分发器（多渠道发送）"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.notifiers = []

        # 动态加载可用的 notifier
        if self.config.get('slack_enabled', False):
            from slack_notifier import SlackNotifier
            self.notifiers.append(SlackNotifier(self.config.get('slack_webhook')))

        if self.config.get('email_enabled', False):
            from email_notifier import EmailNotifier
            self.notifiers.append(EmailNotifier(self.config.get('email_config')))

    def dispatch(self, alerts: List[Alert], test_mode: bool = False) -> Dict:
        """分发告警到所有渠道"""
        results = {
            "dispatched": 0,
            "failed": 0,
            "details": []
        }

        if test_mode:
            _log.info("[TEST MODE] Would dispatch %d alerts", len(alerts))
            for alert in alerts:
                _log.info("  - %s: %s", alert.level.value, alert.message)
            return results

        for alert in alerts:
            for notifier in self.notifiers:
                try:
                    notifier.send(alert)
                    results["dispatched"] += 1
                    results["details"].append({
                        "notifier": notifier.__class__.__name__,
                        "alert": alert.message,
                        "status": "success"
                    })
                except (ConnectionError, TimeoutError, OSError, ValueError, RuntimeError) as e:
                    _log.error("Alert dispatch failed via %s: %s", notifier.__class__.__name__, e, exc_info=True)
                    results["failed"] += 1
                    results["details"].append({
                        "notifier": notifier.__class__.__name__,
                        "alert": alert.message,
                        "status": "failed",
                        "error": str(e)
                    })

        return results


def main():
    """主函数 - 用于命令行调用"""
    import argparse

    parser = argparse.ArgumentParser(description="Alpha Hive Alert Manager")
    parser.add_argument('--status-json', default=str(PATHS.home / 'status.json'))
    parser.add_argument('--output-dir', default=str(PATHS.logs_dir))
    parser.add_argument('--test-mode', action='store_true')
    parser.add_argument('--dispatch', action='store_true', help='Send alerts via configured channels')

    args = parser.parse_args()

    # 1. 分析告警
    analyzer = AlertAnalyzer()
    alerts = analyzer.analyze(Path(args.status_json))

    if not alerts:
        # v0.45.47：只有**全部检查都执行过**才敢说 healthy
        if analyzer.checks_skipped:
            _log.warning("⚠️ 无告警，但有 %d 项检查未能执行 —— 不能判定为健康：\n  · %s",
                         len(analyzer.checks_skipped),
                         "\n  · ".join(analyzer.checks_skipped))
        else:
            _log.info("No alerts detected - system healthy")
        return

    # 2. 保存告警
    output_path = Path(args.output_dir) / f"alerts-{datetime.now().strftime('%Y-%m-%d')}.json"
    analyzer.save_alerts(output_path)
    _log.info("Alerts saved: %s", output_path)
    _log.info("   Critical: %d", len(analyzer.get_critical_alerts()))
    _log.info("   High: %d", len(analyzer.get_high_alerts()))
    _log.info("   Medium: %d", len(analyzer.get_medium_alerts()))

    # 3. 分发告警（可选）
    if args.dispatch:
        from config import ALERT_CONFIG
        dispatcher = AlertDispatcher(ALERT_CONFIG)
        result = dispatcher.dispatch(alerts, test_mode=args.test_mode)
        _log.info("Dispatch result:")
        _log.info("   Sent: %d", result['dispatched'])
        _log.info("   Failed: %d", result['failed'])


if __name__ == "__main__":
    main()
