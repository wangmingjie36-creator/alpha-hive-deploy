"""
tests/test_slack_notifier.py — SlackReportNotifier 单元测试

覆盖：初始化、blocks 构建、发送（mock HTTP）、重试队列去重
"""

import time
import pytest
from unittest.mock import patch, MagicMock


# ==================== 初始化测试 ====================

class TestSlackReportNotifierInit:
    """测试初始化和配置检测"""

    @pytest.fixture(autouse=True)
    def _no_disk_creds(self, monkeypatch):
        """阻止从磁盘文件读取凭证"""
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token", lambda self: None)
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file",
                            lambda self: None)

    def test_disabled_without_credentials(self, monkeypatch):
        """无 token/webhook 时 disabled"""
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        assert n.enabled is False

    def test_enabled_with_valid_webhook(self, monkeypatch):
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file",
                            lambda self: "https://hooks.slack.com/services/T00/B00/xxx")
        # Mock webhook 存活检测，避免真实网络请求
        monkeypatch.setattr(SlackReportNotifier, "_check_webhook_alive",
                            staticmethod(lambda url: True))
        n = SlackReportNotifier()
        assert n.enabled is True
        assert n.use_user_token is False

    def test_invalid_webhook_rejected(self, monkeypatch):
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file",
                            lambda self: "https://evil.com/hook")
        n = SlackReportNotifier()
        assert n.enabled is False

    def test_enabled_with_user_token(self, monkeypatch):
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token",
                            lambda self: "xoxp-123-456-789")
        n = SlackReportNotifier()
        assert n.enabled is True
        assert n.use_user_token is True


# ==================== blocks 构建测试 ====================

class TestBuildBlocks:
    """测试 Slack blocks 构建逻辑"""

    @pytest.fixture
    def notifier(self, monkeypatch):
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token", lambda self: None)
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file",
                            lambda self: "https://hooks.slack.com/services/T/B/x")
        monkeypatch.setattr(SlackReportNotifier, "_check_webhook_alive",
                            staticmethod(lambda url: True))
        return SlackReportNotifier()

    def test_opportunity_alert_blocks(self, notifier):
        blocks = notifier._build_opportunity_alert_blocks(
            "NVDA", 8.5, "看多", "AI 芯片需求强劲", ["监管风险"]
        )
        assert isinstance(blocks, list)
        assert len(blocks) >= 2
        text = str(blocks)
        assert "NVDA" in text
        assert "8.5" in text

    def test_risk_alert_severity_high(self, notifier):
        """HIGH severity 使用 ⚠️ emoji"""
        # send_risk_alert 内部构建 blocks，我们测试它不报错
        with patch.object(notifier, "_send_slack_message_payload", return_value=True):
            ok = notifier.send_risk_alert("测试告警", "测试消息", "HIGH")
            assert ok is True

    def test_risk_alert_severity_critical(self, notifier):
        with patch.object(notifier, "_send_slack_message_payload", return_value=True) as mock:
            notifier.send_risk_alert("紧急告警", "严重消息", "CRITICAL")
            payload = mock.call_args[0][0]
            assert "🚨" in str(payload)


# ==================== 发送测试（mock HTTP）====================

class TestSendWithMock:
    """测试发送逻辑"""

    def test_send_disabled_returns_false(self, monkeypatch):
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token", lambda self: None)
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file", lambda self: None)
        n = SlackReportNotifier()
        assert n.send_daily_report({"test": True}) is False

    def test_send_via_api_success(self, monkeypatch):
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token",
                            lambda self: "xoxp-test-token")
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file", lambda self: None)
        n = SlackReportNotifier()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp
        with patch("slack_report_notifier.get_session", return_value=mock_session):
            ok = n.send_plain_text("测试消息")
            assert ok is True
            mock_session.post.assert_called_once()

    def test_send_webhook_success(self, monkeypatch):
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token", lambda self: None)
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file",
                            lambda self: "https://hooks.slack.com/services/T/B/x")
        monkeypatch.setattr(SlackReportNotifier, "_check_webhook_alive",
                            staticmethod(lambda url: True))
        n = SlackReportNotifier()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp
        with patch("slack_report_notifier.get_session", return_value=mock_session):
            ok = n._send_slack_message_payload({"text": "test"})
            assert ok is True

    def test_send_failure_enqueues(self, monkeypatch):
        """HTTP 失败时消息进入重试队列"""
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token",
                            lambda self: "xoxp-test-token")
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file", lambda self: None)
        import requests
        n = SlackReportNotifier()

        mock_session = MagicMock()
        mock_session.post.side_effect = requests.exceptions.ConnectionError("timeout")
        with patch("slack_report_notifier.get_session", return_value=mock_session):
            ok = n._send_via_api("失败消息", n.CHANNEL_ID)
            assert ok is False
            assert len(n._failed_queue) == 1


# ==================== 重试队列测试 ====================

class TestRetryQueue:
    """测试失败消息重试队列"""

    @pytest.fixture
    def notifier(self, monkeypatch):
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        from slack_report_notifier import SlackReportNotifier
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token",
                            lambda self: "xoxp-test-token")
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file", lambda self: None)
        return SlackReportNotifier()

    def test_dedup_within_5_minutes(self, notifier):
        notifier._enqueue_failed("api", "same message")
        notifier._enqueue_failed("api", "same message")
        assert len(notifier._failed_queue) == 1

    def test_different_messages_both_queued(self, notifier):
        notifier._enqueue_failed("api", "message A")
        notifier._enqueue_failed("api", "message B")
        assert len(notifier._failed_queue) == 2

    def test_retry_expired_drops(self, notifier):
        """超过 1 小时的消息被丢弃"""
        notifier._failed_queue.append({
            "method": "api", "text": "old msg",
            "ts": time.time() - 7200, "hash": "abc"
        })
        with patch.object(notifier, "_send_via_api", return_value=True):
            retried = notifier.retry_failed()
        assert retried == 0
        assert len(notifier._failed_queue) == 0

    def test_retry_success_removes_item(self, notifier):
        notifier._enqueue_failed("api", "retry me")
        with patch.object(notifier, "_send_via_api", return_value=True):
            retried = notifier.retry_failed()
        assert retried == 1
        assert len(notifier._failed_queue) == 0

    def test_retry_failure_keeps_item(self, notifier):
        notifier._enqueue_failed("api", "still failing")
        with patch.object(notifier, "_send_via_api", return_value=False):
            retried = notifier.retry_failed()
        assert retried == 0
        assert len(notifier._failed_queue) == 1

    def test_empty_queue_returns_zero(self, notifier):
        assert notifier.retry_failed() == 0
