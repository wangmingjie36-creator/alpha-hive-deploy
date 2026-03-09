#!/usr/bin/env python3
"""
💬 Slack 告警通知器
将 Alpha Hive 告警推送到 Slack 频道
"""

import os
import requests
from resilience import get_session
from typing import Optional
from alert_manager import Alert, AlertLevel
from hive_logger import get_logger

_log = get_logger("slack_notifier")


class SlackNotifier:
    """Slack 通知器"""

    def __init__(self, webhook_url: Optional[str] = None):
        """
        初始化 Slack Notifier

        Args:
            webhook_url: Slack Webhook URL (如果为 None，则从环境变量或文件读取)
        """
        self.webhook_url = webhook_url
        if not self.webhook_url:
            self.webhook_url = self._read_webhook_from_file()

    def _read_webhook_from_file(self) -> Optional[str]:
        """从 config.get_secret > 环境变量 > 文件安全读取 Webhook URL"""
        try:
            from config import get_secret
            url = get_secret("SLACK_WEBHOOK_URL")
            if url:
                return url
        except ImportError:
            pass
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
            return None

    def send(self, alert: Alert) -> bool:
        """
        发送告警到 Slack

        Args:
            alert: Alert 对象

        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            _log.warning("Slack webhook URL not configured")
            return False

        payload = self._build_payload(alert)

        try:
            from resilience import slack_breaker
            if not slack_breaker.allow_request():
                _log.warning("Slack circuit breaker OPEN, skipping")
                return False
            response = get_session("slack").post(
                self.webhook_url,
                json=payload,
                timeout=15
            )
            response.raise_for_status()
            slack_breaker.record_success()
            return True
        except requests.exceptions.RequestException as e:
            try:
                from resilience import slack_breaker as _sb
                _sb.record_failure()
            except ImportError:
                pass
            _log.error("Slack notification failed: %s", e)
            return False

    def _build_payload(self, alert: Alert) -> dict:
        """构建 Slack 消息负载"""

        # 级别对应的颜色和表情
        config = {
            AlertLevel.CRITICAL: {
                "color": "#FF0000",
                "emoji": "🚨",
                "channel_mention": "<!channel>"
            },
            AlertLevel.HIGH: {
                "color": "#FF9900",
                "emoji": "⚠️ ",
                "channel_mention": None
            },
            AlertLevel.MEDIUM: {
                "color": "#FFCC00",
                "emoji": "⏱️ ",
                "channel_mention": None
            },
            AlertLevel.INFO: {
                "color": "#0099FF",
                "emoji": "ℹ️ ",
                "channel_mention": None
            }
        }

        cfg = config.get(alert.level, config[AlertLevel.INFO])
        mention = f"{cfg['channel_mention']} " if cfg['channel_mention'] else ""

        # 构建字段
        level_map = {
            "CRITICAL": "🚨 严重",
            "HIGH": "⚠️ 高",
            "MEDIUM": "📊 中",
            "INFO": "ℹ️ 信息"
        }

        fields = [
            {
                "title": "告警级别",
                "value": level_map.get(alert.level.value, alert.level.value),
                "short": True
            },
            {
                "title": "时间",
                "value": alert.timestamp[:19],
                "short": True
            }
        ]

        # 添加详情字段
        for key, value in alert.details.items():
            fields.append({
                "title": key,
                "value": str(value),
                "short": len(str(value)) < 30
            })

        # 添加标签
        if alert.tags:
            fields.append({
                "title": "标签",
                "value": " ".join([f"`{tag}`" for tag in alert.tags]),
                "short": False
            })

        return {
            "text": f"{mention}{cfg['emoji']} *{alert.message}*",
            "attachments": [
                {
                    "color": cfg['color'],
                    "fields": fields,
                    "footer": "Alpha Hive Alert System",
                    "ts": int(alert.timestamp.replace('-', '').replace(':', '').replace('T', ''))
                }
            ]
        }

    @staticmethod
    def setup_webhook(webhook_url: str, file_path: str = "~/.alpha_hive_slack_webhook") -> bool:
        """
        设置 Slack Webhook URL

        Usage:
            SlackNotifier.setup_webhook("https://hooks.slack.com/services/...")
        """
        import os
        file_path = file_path.replace("~", os.path.expanduser("~"))

        try:
            # 创建文件并设置权限
            with open(file_path, 'w') as f:
                f.write(webhook_url)
            os.chmod(file_path, 0o600)
            _log.info("Slack webhook saved to %s", file_path)
            return True
        except OSError as e:
            _log.error("Failed to save webhook: %s", e, exc_info=True)
            return False


def main():
    """命令行测试"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python slack_notifier.py <webhook_url>")
        print("  Example: python slack_notifier.py 'https://hooks.slack.com/services/...'")
        return

    webhook_url = sys.argv[1]

    # 设置 webhook
    SlackNotifier.setup_webhook(webhook_url)

    # 测试发送
    notifier = SlackNotifier()
    if notifier.webhook_url:
        from alert_manager import AlertLevel
        test_alert = Alert(
            AlertLevel.HIGH,
            "Test Alert from Alpha Hive",
            {
                "test": "true",
                "timestamp": "2026-02-24T10:00:00Z",
                "message": "This is a test message"
            },
            ["test", "demo"]
        )
        if notifier.send(test_alert):
            print("✅ Test alert sent successfully!")
        else:
            print("❌ Failed to send test alert")
    else:
        print("❌ Webhook URL not configured")


if __name__ == "__main__":
    import os
    main()
