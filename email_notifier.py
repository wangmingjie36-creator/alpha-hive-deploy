#!/usr/bin/env python3
"""
📧 邮件告警通知器
将 Alpha Hive 告警发送到邮箱（支持汇总）
"""

import logging as _logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, List
from alert_manager import Alert, AlertLevel
from datetime import datetime

_log = _logging.getLogger("alpha_hive.email_notifier")


class EmailNotifier:
    """邮件通知器"""

    def __init__(self, config: dict = None):
        """
        初始化邮件通知器

        config 格式:
        {
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "sender_email": "alerts@example.com",
            "sender_password": "xxxx",  # 或从文件读取
            "recipient_emails": ["user@example.com"],
            "use_tls": True
        }
        """
        self.config = config or self._load_config_from_file()
        self.alert_queue: List[Alert] = []

    def _load_config_from_file(self) -> dict:
        """从文件加载配置（实现待定）"""
        # 这里可以从 config.py 或环境变量读取
        return {}

    def send(self, alert: Alert) -> bool:
        """
        发送告警到邮箱

        对于 CRITICAL 和 HIGH 级别告警立即发送
        对于 MEDIUM 级别告警排队等待汇总

        Args:
            alert: Alert 对象

        Returns:
            是否发送成功
        """
        if not self._validate_config():
            print("⚠️  Email config not configured")
            return False

        # 根据级别决定是否立即发送
        if alert.level in [AlertLevel.CRITICAL, AlertLevel.HIGH]:
            return self._send_immediately(alert)
        else:
            # 中等和低级告警排队
            self.alert_queue.append(alert)
            return True

    def _validate_config(self) -> bool:
        """验证配置是否完整"""
        required_keys = ['smtp_server', 'smtp_port', 'sender_email', 'recipient_emails']
        return all(key in self.config for key in required_keys)

    def _send_immediately(self, alert: Alert) -> bool:
        """立即发送告警邮件"""
        try:
            subject = f"[{alert.level.value}] {alert.message}"
            body = self._build_email_body(alert)

            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.config['sender_email']
            msg['To'] = ', '.join(self.config['recipient_emails'])

            # 添加纯文本和 HTML 版本
            text_part = MIMEText(body, 'plain')
            html_part = MIMEText(self._build_html_body(alert), 'html')

            msg.attach(text_part)
            msg.attach(html_part)

            # 发送邮件
            self._smtp_send(msg)
            return True

        except (smtplib.SMTPException, ConnectionError, TimeoutError, OSError) as e:
            _log.error("Email send failed: %s", e, exc_info=True)
            return False

    def _smtp_send(self, msg: MIMEMultipart) -> None:
        """通过 SMTP 发送邮件"""
        server = smtplib.SMTP(self.config['smtp_server'], self.config['smtp_port'])

        if self.config.get('use_tls', True):
            server.starttls()

        server.login(self.config['sender_email'], self.config['sender_password'])
        server.sendmail(
            self.config['sender_email'],
            self.config['recipient_emails'],
            msg.as_string()
        )
        server.quit()

    def send_digest(self) -> bool:
        """
        发送汇总邮件（包含所有排队的告警）

        通常由定时任务调用（例如每小时）
        """
        if not self.alert_queue:
            return True

        try:
            subject = f"[Alpha Hive] Daily Alert Digest ({len(self.alert_queue)} alerts)"
            body = self._build_digest_body()

            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.config['sender_email']
            msg['To'] = ', '.join(self.config['recipient_emails'])

            text_part = MIMEText(body, 'plain')
            html_part = MIMEText(self._build_digest_html(), 'html')

            msg.attach(text_part)
            msg.attach(html_part)

            # 发送并清空队列
            self._smtp_send(msg)
            self.alert_queue.clear()
            return True

        except (smtplib.SMTPException, ConnectionError, TimeoutError, OSError) as e:
            _log.error("Digest send failed: %s", e, exc_info=True)
            return False

    def _build_email_body(self, alert: Alert) -> str:
        """构建邮件纯文本内容"""
        return f"""
Alert Level: {alert.level.value}
Message: {alert.message}
Time: {alert.timestamp}

Details:
{chr(10).join(f"  {k}: {v}" for k, v in alert.details.items())}

Tags: {', '.join(alert.tags) if alert.tags else 'N/A'}

---
Alpha Hive Alert System
"""

    def _build_html_body(self, alert: Alert) -> str:
        """构建邮件 HTML 内容"""
        color_map = {
            AlertLevel.CRITICAL: "#FF0000",
            AlertLevel.HIGH: "#FF9900",
            AlertLevel.MEDIUM: "#FFCC00",
            AlertLevel.INFO: "#0099FF"
        }

        color = color_map.get(alert.level, "#0099FF")

        details_html = "".join([
            f"<tr><td><strong>{k}</strong></td><td>{v}</td></tr>"
            for k, v in alert.details.items()
        ])

        tags_html = "".join([
            f"<span style='background: #E0E0E0; padding: 2px 8px; margin: 0 4px; border-radius: 3px; font-size: 12px;'>{tag}</span>"
            for tag in alert.tags
        ]) if alert.tags else "N/A"

        return f"""
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .alert-box {{ border-left: 4px solid {color}; padding: 15px; background: #f9f9f9; margin: 20px 0; }}
        .alert-title {{ font-size: 20px; font-weight: bold; color: {color}; margin-bottom: 10px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
        td {{ padding: 8px; border: 1px solid #ddd; }}
        .footer {{ color: #999; font-size: 12px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="alert-box">
        <div class="alert-title">{alert.level.value}: {alert.message}</div>
        <table>
            <tr><td><strong>Time</strong></td><td>{alert.timestamp}</td></tr>
            {details_html}
        </table>
        <p><strong>Tags:</strong> {tags_html}</p>
    </div>
    <p class="footer">Alpha Hive Alert System</p>
</body>
</html>
"""

    def _build_digest_body(self) -> str:
        """构建汇总邮件纯文本内容"""
        body = f"Alpha Hive Daily Alert Digest\n"
        body += f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        body += f"Total Alerts: {len(self.alert_queue)}\n"
        body += "=" * 60 + "\n\n"

        for i, alert in enumerate(self.alert_queue, 1):
            body += f"{i}. [{alert.level.value}] {alert.message}\n"
            body += f"   Time: {alert.timestamp}\n"
            for k, v in alert.details.items():
                body += f"   {k}: {v}\n"
            body += "\n"

        return body

    def _build_digest_html(self) -> str:
        """构建汇总邮件 HTML 内容"""
        alerts_html = ""
        for alert in self.alert_queue:
            color_map = {
                AlertLevel.CRITICAL: "#FF0000",
                AlertLevel.HIGH: "#FF9900",
                AlertLevel.MEDIUM: "#FFCC00",
                AlertLevel.INFO: "#0099FF"
            }
            color = color_map.get(alert.level, "#0099FF")

            details = "".join([
                f"<li><strong>{k}:</strong> {v}</li>"
                for k, v in alert.details.items()
            ])

            alerts_html += f"""
            <div style='border-left: 4px solid {color}; padding: 10px; margin: 10px 0; background: #f9f9f9;'>
                <div style='color: {color}; font-weight: bold;'>{alert.level.value}</div>
                <div style='font-size: 16px; margin: 5px 0;'>{alert.message}</div>
                <div style='color: #666; font-size: 12px;'>{alert.timestamp}</div>
                <ul style='margin: 5px 0; padding-left: 20px;'>{details}</ul>
            </div>
            """

        return f"""
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .digest-header {{ font-size: 24px; font-weight: bold; margin-bottom: 20px; }}
        .summary {{ background: #f0f0f0; padding: 15px; border-radius: 5px; margin: 20px 0; }}
        .footer {{ color: #999; font-size: 12px; margin-top: 30px; border-top: 1px solid #ddd; padding-top: 15px; }}
    </style>
</head>
<body>
    <div class="digest-header">🐝 Alpha Hive Daily Alert Digest</div>
    <div class="summary">
        <p><strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p><strong>Total Alerts:</strong> {len(self.alert_queue)}</p>
        <p><strong>Critical:</strong> {sum(1 for a in self.alert_queue if a.level == AlertLevel.CRITICAL)}</p>
        <p><strong>High:</strong> {sum(1 for a in self.alert_queue if a.level == AlertLevel.HIGH)}</p>
        <p><strong>Medium:</strong> {sum(1 for a in self.alert_queue if a.level == AlertLevel.MEDIUM)}</p>
    </div>
    {alerts_html}
    <div class="footer">Alpha Hive Alert System</div>
</body>
</html>
"""


def main():
    """命令行测试"""
    print("Email Notifier Test")
    print("=" * 50)
    print("\nTo enable email notifications:")
    print("1. Add email config to config.py:")
    print("""
    ALERT_CONFIG = {
        'email_enabled': True,
        'email_config': {
            'smtp_server': 'smtp.gmail.com',
            'smtp_port': 587,
            'sender_email': 'your-email@gmail.com',
            'sender_password': 'your-app-password',
            'recipient_emails': ['your-email@gmail.com'],
            'use_tls': True
        }
    }
    """)


if __name__ == "__main__":
    main()
