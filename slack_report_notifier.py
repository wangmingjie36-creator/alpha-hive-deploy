#!/usr/bin/env python3
"""
💬 Alpha Hive Slack 报告通知器 - Phase 3 P6
专门用于将投资简报和各类信息推送到 Slack
替代 Gmail，提供实时、富文本的通知体验
"""

import os
import requests
from typing import Dict, List, Optional
from datetime import datetime


class SlackReportNotifier:
    """Slack 报告通知器"""

    def __init__(self, webhook_url: Optional[str] = None):
        """
        初始化 Slack 报告通知器

        Args:
            webhook_url: Slack Webhook URL（如为 None，从文件读取）
        """
        self.webhook_url = webhook_url or self._read_webhook_from_file()
        self.enabled = bool(self.webhook_url) and self._is_valid_webhook(self.webhook_url)

    @staticmethod
    def _is_valid_webhook(url: str) -> bool:
        """校验 Slack Webhook URL 格式"""
        return bool(url and url.startswith("https://hooks.slack.com/"))

    def _read_webhook_from_file(self) -> Optional[str]:
        """从环境变量或文件安全读取 Webhook URL"""
        # 优先使用环境变量
        env_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if env_url:
            return env_url
        # 降级到文件
        webhook_file = os.path.expanduser("~/.alpha_hive_slack_webhook")
        try:
            with open(webhook_file, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            print("⚠️ Slack webhook 文件未找到: ~/.alpha_hive_slack_webhook")
            return None

    def send_daily_report(self, report_data: Dict) -> bool:
        """
        发送每日投资简报到 Slack

        Args:
            report_data: 包含报告信息的字典

        Returns:
            是否发送成功
        """
        if not self.enabled:
            print("⚠️ Slack 通知已禁用")
            return False

        blocks = self._build_daily_report_blocks(report_data)
        return self._send_slack_message(blocks)

    def send_opportunity_alert(self, ticker: str, score: float, direction: str,
                               discovery: str, risks: List[str] = None) -> bool:
        """
        发送高分机会告警

        Args:
            ticker: 股票代码
            score: 综合评分（0-10）
            direction: 方向（看多/看空/中性）
            discovery: 发现摘要
            risks: 风险列表

        Returns:
            是否发送成功
        """
        if not self.enabled:
            return False

        blocks = self._build_opportunity_alert_blocks(ticker, score, direction, discovery, risks)
        return self._send_slack_message(blocks)

    def send_risk_alert(self, alert_title: str, alert_message: str, severity: str = "HIGH") -> bool:
        """
        发送风险告警

        Args:
            alert_title: 告警标题
            alert_message: 告警信息
            severity: 严重级别（CRITICAL/HIGH/MEDIUM/LOW）

        Returns:
            是否发送成功
        """
        if not self.enabled:
            return False

        severity_config = {
            "CRITICAL": {"color": "#FF0000", "emoji": "🚨"},
            "HIGH": {"color": "#FF6600", "emoji": "⚠️"},
            "MEDIUM": {"color": "#FFCC00", "emoji": "⚡"},
            "LOW": {"color": "#0099FF", "emoji": "ℹ️"}
        }

        config = severity_config.get(severity, severity_config["MEDIUM"])

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{config['emoji']} *{alert_title}*\n\n{alert_message}"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🐝 Alpha Hive | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    }
                ]
            }
        ]

        payload = {
            "blocks": blocks,
            "attachments": [
                {
                    "color": config["color"],
                    "footer": "Alpha Hive 风险告警系统"
                }
            ]
        }

        return self._send_slack_message_payload(payload)

    def send_scan_progress(self, targets: List[str], current: int, total: int,
                           status_message: str) -> bool:
        """
        发送扫描进度更新

        Args:
            targets: 目标标的列表
            current: 当前完成数
            total: 总数
            status_message: 状态信息

        Returns:
            是否发送成功
        """
        if not self.enabled:
            return False

        progress_pct = (current / total * 100) if total > 0 else 0

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔄 *Alpha Hive 蜂群扫描进度*\n\n{status_message}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*进度:*\n{current}/{total} ({progress_pct:.0f}%)"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*标的数:*\n{len(targets)}"
                    }
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*追踪标的:*\n{', '.join(targets[:5])}{'...' if len(targets) > 5 else ''}"
                }
            }
        ]

        return self._send_slack_message(blocks)

    def send_x_thread(self, thread_content: str, date_str: str) -> bool:
        """
        发送 X (Twitter) 线程草稿到 Slack

        Args:
            thread_content: X 线程内容（多条推文）
            date_str: 日期

        Returns:
            是否发送成功
        """
        if not self.enabled:
            return False

        # 将多条推文分离
        tweets = [t.strip() for t in thread_content.split('\n') if t.strip() and t.strip().startswith(('【', '#', '1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟'))]

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🐦 *Alpha Hive X 线程草稿* - {date_str}"
                }
            },
            {
                "type": "divider"
            }
        ]

        # 添加前 5 条推文
        for i, tweet in enumerate(tweets[:5], 1):
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{i}️⃣ {tweet[:200]}..."
                }
            })

        blocks.extend([
            {
                "type": "divider"
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"📝 共 {len(tweets)} 条推文 | 🐝 Alpha Hive"
                    }
                ]
            }
        ])

        return self._send_slack_message(blocks)

    def _build_daily_report_blocks(self, report_data: Dict) -> List[Dict]:
        """构建每日报告 Block"""

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "📰 *Alpha Hive 每日投资简报*"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🐝 {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}"
                    }
                ]
            },
            {
                "type": "divider"
            }
        ]

        # 添加机会列表
        opportunities = report_data.get('opportunities', [])
        if opportunities:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*📊 发现 {len(opportunities)} 个投资机会*"
                }
            })

            # Top 3 机会
            for i, opp in enumerate(opportunities[:3], 1):
                blocks.append({
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*#{i} {opp.get('ticker', '?')}*\n{opp.get('direction', '中性')}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*评分*\n{opp.get('opp_score', 0):.1f}/10"
                        }
                    ]
                })

        # 添加风险提示
        risks = report_data.get('risks', [])
        if risks:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*⚠️ 风险提示:*\n{', '.join(risks[:3])}"
                }
            })

        # 添加免责声明
        blocks.extend([
            {
                "type": "divider"
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "📋 本报告为自动化数据分析，不构成投资建议。请根据自身风险承受能力做出投资决策。"
                    }
                ]
            }
        ])

        return blocks

    def _build_opportunity_alert_blocks(self, ticker: str, score: float, direction: str,
                                        discovery: str, risks: List[str]) -> List[Dict]:
        """构建机会告警 Block"""

        direction_emoji = {
            "看多": "📈",
            "看空": "📉",
            "中性": "➡️"
        }
        emoji = direction_emoji.get(direction, "•")

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *高分机会告警: {ticker}*"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*方向*\n{direction}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*评分*\n{score:.1f}/10"
                    }
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*发现*\n{discovery[:200]}"
                }
            }
        ]

        if risks:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*风险*\n• {chr(10).join(risks[:2])}"
                }
            })

        blocks.extend([
            {
                "type": "divider"
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🐝 Alpha Hive | {datetime.now().strftime('%H:%M:%S')}"
                    }
                ]
            }
        ])

        return blocks

    def _send_slack_message(self, blocks: List[Dict]) -> bool:
        """发送 Slack 消息（blocks 格式）"""

        payload = {
            "blocks": blocks,
            "text": "Alpha Hive 通知"  # 备用文本
        }

        return self._send_slack_message_payload(payload)

    def _send_slack_message_payload(self, payload: Dict) -> bool:
        """发送 Slack 消息（完整 payload）"""

        if not self.webhook_url:
            return False

        try:
            from resilience import slack_breaker
            if not slack_breaker.allow_request():
                print("⚠️  Slack circuit breaker OPEN, skipping")
                return False
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=15
            )
            response.raise_for_status()
            slack_breaker.record_success()

            if response.status_code == 200:
                print("✅ Slack 消息发送成功")
                return True
            else:
                print(f"⚠️ Slack 返回状态码: {response.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            try:
                from resilience import slack_breaker as _sb
                _sb.record_failure()
            except ImportError:
                pass
            print(f"❌ Slack 发送失败: {e}")
            return False

    def test_connection(self) -> bool:
        """测试 Slack 连接"""

        if not self.enabled:
            print("❌ Slack 未配置")
            return False

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "✅ *Alpha Hive Slack 连接测试成功！*\n\n🐝 系统已就绪，可以接收投资简报和实时告警。"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🐝 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    }
                ]
            }
        ]

        success = self._send_slack_message(blocks)
        if success:
            print("✅ Slack 连接测试通过")
        return success


if __name__ == "__main__":
    notifier = SlackReportNotifier()

    print("\n" + "="*70)
    print("🧪 Slack 报告通知器测试")
    print("="*70 + "\n")

    # 测试连接
    print("测试 1：连接测试")
    notifier.test_connection()

    # 测试机会告警
    print("\n测试 2：机会告警")
    notifier.send_opportunity_alert(
        ticker="NVDA",
        score=8.5,
        direction="看多",
        discovery="AI 芯片需求强劲，财报指引乐观",
        risks=["监管政策风险", "竞争加剧"]
    )

    # 测试风险告警
    print("\n测试 3：风险告警")
    notifier.send_risk_alert(
        alert_title="市场波动告警",
        alert_message="VIX 指数突破 25，市场风险偏好下降",
        severity="HIGH"
    )

    # 测试扫描进度
    print("\n测试 4：扫描进度")
    notifier.send_scan_progress(
        targets=["NVDA", "TSLA", "MSFT", "AMD", "QCOM"],
        current=3,
        total=5,
        status_message="蜂群正在进行实时分析..."
    )

    print("\n" + "="*70)
    print("✅ 所有测试完成")
    print("="*70 + "\n")
