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
        br = resilience.slack_breaker    # conftest `_reset_circuit_breakers` 保证它此刻是干净的
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


# ==================== 频道发不进去 ≠ 改发私信（v0.45.343）====================

class TestNoSilentDmFallback:
    """发到哪就是哪：频道失败即失败，**不许悄悄改发私信**。

    v0.45.343 之前 `_send_via_api` 在 `not_in_channel` / `channel_not_found` 时自动改发
    私信用户（`config.SLACK_DM_FALLBACK`）。生产上 Bot 从未被邀请进 #alpha-hive，于是：
      * `push_report_to_slack --force` 每次都把日报**实发到私信**，调用方只拿到 `True`，
        照样记「✅ 日报已成功推送到 Slack #alpha-hive」—— 日志说谎；
      * v0.45.339 那批被禁告警，正是经这条降级漏成了私信；
      * 频道与私信都回 channel 类错误时循环跑空、**返回 None**（签名是 bool）。
    桩按生产现实设：频道回 `not_in_channel`，其它目标（私信）成功 —— 旧代码在这里全绿地「发出去」。
    """

    @pytest.fixture
    def slack(self, monkeypatch):
        import slack_report_notifier as srn
        box = types.SimpleNamespace(calls=[], channel_reply={"ok": False, "error": "not_in_channel"},
                                    other_reply={"ok": True, "ts": "1"})

        def _post(url, headers=None, json=None, timeout=None):
            box.calls.append(json["channel"])
            reply = (box.channel_reply if json["channel"] == srn.SlackReportNotifier.CHANNEL_ID
                     else box.other_reply)
            if isinstance(reply, Exception):
                raise reply
            return types.SimpleNamespace(json=lambda: reply)

        monkeypatch.setattr(srn, "get_session", lambda *_a, **_k: types.SimpleNamespace(post=_post))
        monkeypatch.setattr(srn.SlackReportNotifier, "_read_user_token",
                            lambda self: "xoxb-test-not-a-real-token")
        monkeypatch.setattr(srn.SlackReportNotifier, "_read_webhook_from_file", lambda self: None)
        box.channel = srn.SlackReportNotifier.CHANNEL_ID
        return box

    def test_not_in_channel_is_a_failure_not_a_dm(self, slack, caplog):
        from slack_report_notifier import SlackReportNotifier
        with caplog.at_level(logging.WARNING, logger="alpha_hive.slack_report_notifier"):
            ok = SlackReportNotifier().send_plain_text("日报")
        assert slack.calls == [slack.channel], f"改发了别的目标：{slack.calls}"
        assert ok is False
        assert "not_in_channel" in caplog.text

    @pytest.mark.parametrize("channel_reply", [
        {"ok": True, "ts": "1"},
        {"ok": False, "error": "not_in_channel"},
        {"ok": False, "error": "channel_not_found"},
        {"ok": False, "error": "invalid_auth"},
        {"ok": False},
    ], ids=["ok", "not_in_channel", "channel_not_found", "other_error", "no_error_field"])
    def test_every_outcome_is_a_bool(self, slack, channel_reply):
        """签名是 bool 就每条路径都返回 bool —— 旧代码在「私信也回 channel 类错误」时返回 None。"""
        from slack_report_notifier import SlackReportNotifier
        slack.channel_reply = channel_reply
        slack.other_reply = {"ok": False, "error": "channel_not_found"}   # 旧降级目标也失败
        result = SlackReportNotifier().send_plain_text("x")
        assert result is channel_reply.get("ok", False)

    def test_transport_error_is_a_bool_false(self, slack):
        import requests
        from slack_report_notifier import SlackReportNotifier
        slack.channel_reply = requests.exceptions.ConnectionError("down")
        assert SlackReportNotifier().send_plain_text("x") is False
        assert slack.calls == [slack.channel]

    def test_push_report_force_does_not_claim_channel_delivery(self, slack, monkeypatch, caplog):
        """端到端：Bot 不在频道 ⇒ `--force` exit 1，且**不许**出现「已成功推送到 #alpha-hive」。"""
        import json
        import sys
        from hive_logger import PATHS
        import push_report_to_slack
        (PATHS.home / "alpha-hive-daily-2026-09-24.json").write_text(json.dumps(
            {"date": "2026-09-24", "opportunities": [
                {"ticker": "NVDA", "opp_score": 7.1, "direction": "看多"}]}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["push_report_to_slack.py", "--force", "--date", "2026-09-24"])
        with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as ei:
            push_report_to_slack.main()
        assert slack.calls == [slack.channel], f"改发了别的目标：{slack.calls}"
        assert ei.value.code == 1
        assert "已成功推送" not in caplog.text
        assert "日报推送失败" in caplog.text
