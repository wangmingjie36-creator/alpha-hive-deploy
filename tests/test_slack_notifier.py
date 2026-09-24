"""
tests/test_slack_notifier.py — SlackReportNotifier 单元测试

覆盖：初始化、纯文本发送（mock HTTP）、conftest `_block_slack` 三道闸的自证、
`pre_scan_notify` 的 webhook 兜底。

v0.45.341：blocks 构建器 / `send_risk_alert` 等告警类方法 / 失败重试队列 / 整个
`slack_notifier.py` 已删（零生产调用方），它们的测试随之删除。白名单与墓碑见
`tests/test_slack_send_whitelist.py`。
"""

import logging
import types

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
        assert n.send_plain_text("x") is False
        assert n.send_rich_daily_report("unused.json", "c", "d", "f") is False

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

    def test_send_failure_returns_false_and_logs(self, monkeypatch, caplog):
        """HTTP 失败 ⇒ 返回 False + 一行 error 日志 + 熔断器记一次失败。

        v0.45.341 之前这里还会进一个内存重试队列，但 `retry_failed` 零调用方、
        调用方都是一次性进程 ⇒ 队列结构上不可能被读，已删。失败的出口是返回值
        （`push_report_to_slack` 据此 exit 1）与这行日志。
        """
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        from slack_report_notifier import SlackReportNotifier
        import resilience
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token",
                            lambda self: "xoxp-test-token")
        monkeypatch.setattr(SlackReportNotifier, "_read_webhook_from_file", lambda self: None)
        br = resilience.CircuitBreaker("slack")          # 别动全局 slack_breaker
        monkeypatch.setattr(resilience, "slack_breaker", br)
        import requests
        n = SlackReportNotifier()

        mock_session = MagicMock()
        mock_session.post.side_effect = requests.exceptions.ConnectionError("timeout")
        with patch("slack_report_notifier.get_session", return_value=mock_session), \
                caplog.at_level(logging.ERROR, logger="alpha_hive.slack_report_notifier"):
            ok = n._send_via_api("失败消息", n.CHANNEL_ID)
        assert ok is False
        assert "Slack 发送失败" in caplog.text
        assert br._failure_count == 1


# ==================== conftest._block_slack 的反向自证 ====================

class TestTestsCanNeverReachSlack:
    """三道闸的自证：任何一道被摘掉，这里就红（v0.45.131）。

    ⚠️ 本类**刻意不带** `TestSlackReportNotifierInit._no_disk_creds` 那个
    类级 fixture —— 那个 fixture 自己也把 `_read_user_token` 打成 None，
    带上它就会把闸①的效果掩盖掉，这条自证会退化成永真。
    """

    def test_production_credentials_are_never_used(self):
        """闸①：测试里读到的 user_token 恒为 None，不碰你 Mac 上的真 token。"""
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        assert n._read_user_token() is None

    def test_construction_does_not_probe_the_webhook(self):
        """闸②：构造时那次 requests.head 被换成常量 False。"""
        from slack_report_notifier import SlackReportNotifier
        assert SlackReportNotifier._check_webhook_alive(
            "https://hooks.slack.com/services/T/B/x") is False

    def test_notifier_is_disabled_in_tests(self):
        """①②合起来的效果：enabled 恒 False，发送在守卫处就短路。

        这一条才是真正拦住事故的那句 —— `_try_src_slack_alert` 里的
        `if getattr(n, "enabled", False)`。
        """
        from slack_report_notifier import SlackReportNotifier
        assert SlackReportNotifier().enabled is False

    def test_slack_session_is_the_recorder_not_the_real_one(self):
        """闸③：两个模块的 get_session 都已被换掉。

        只比对象身份、不实际调用 —— 调用会记进 attempts 让 teardown 断言变红。
        """
        import resilience
        import slack_report_notifier
        assert slack_report_notifier.get_session is not resilience.get_session

    def test_the_original_accident_path_sends_nothing(self, monkeypatch):
        """端到端回归：v0.45.131 事故的那条链路，走完不产生任何 Slack 调用。

        `_record_src_failure` 连续失败达阈值 → `_try_src_slack_alert`。
        若闸失效，teardown 的 attempts 断言会带着 URL 变红。
        """
        import real_data_sources as r
        monkeypatch.setattr(r, "_src_fail_counts", {}, raising=False)
        monkeypatch.setattr(r, "_src_degraded", {}, raising=False)
        for _ in range(r._HEALTH_FAIL_THRESHOLD):
            r._record_src_failure("yfinance_short_interest")
        assert r._src_degraded.get("yfinance_short_interest") is True


# ==================== pre_scan_notify 的 webhook 兜底 ====================

class TestPreScanWebhookFallback:
    """`pre_scan_notify.send_slack_notification` 第三级：直接读 `~/.alpha_hive_slack_webhook`。

    v0.45.341 删掉 `slack_notifier.py`（它的 CLI 是唯一「写」这个文件的代码）时补上：
    证明兜底只依赖文件本身，不依赖任何通知器模块。`HOME` 指到 tmp —— 既不读你
    Mac 上的真 webhook，`requests.post` 也换成记录器，**不出网、不发消息**。
    token 两级在 conftest 闸①下读不到 token（`_get_slack_token` 另行打成 None），只剩第三级。
    """

    @pytest.fixture
    def net(self, monkeypatch, tmp_path):
        import requests
        import pre_scan_notify
        calls = []

        def _post(url, *a, **k):
            calls.append((url, k.get("json")))
            return types.SimpleNamespace(ok=True, text="ok", status_code=200,
                                         json=lambda: {"ok": True})

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        monkeypatch.setattr(requests, "post", _post)
        monkeypatch.setattr(pre_scan_notify, "_get_slack_token", lambda: None)
        return types.SimpleNamespace(calls=calls, home=tmp_path, mod=pre_scan_notify)

    def test_webhook_file_is_used(self, net):
        url = "https://hooks.slack.com/services/T0/B0/fallback-by-test"
        (net.home / ".alpha_hive_slack_webhook").write_text(url + "\n", encoding="utf-8")
        assert net.mod.send_slack_notification("LLM 模式确认（测试）") == (True, None, None)
        assert net.calls == [(url, {"text": "LLM 模式确认（测试）"})]

    def test_no_webhook_file_means_no_send(self, net):
        """反对照：文件不在 ⇒ 三级全落空、零发送（上一条的 True 不是恒真）。"""
        assert net.mod.send_slack_notification("x") == (False, None, None)
        assert net.calls == []
