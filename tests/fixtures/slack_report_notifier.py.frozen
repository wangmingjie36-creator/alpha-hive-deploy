#!/usr/bin/env python3
"""
💬 Alpha Hive Slack 报告通知器

CLAUDE.md「Slack 通知精简规则」：v0.45.571 起**没有任何生产代码发 Slack**——本模块只剩发送原语
（`send_plain_text` / `_send_via_api` / `_send_slack_message_payload`）与配置探测（`enabled`，
`run_daily_scan.py --dry-run` 与本文件的 `__main__` 读它），零生产调用方。
（原唯一一类「富文本日报」`push_report_to_slack.py --force` → `send_rich_daily_report` 与其格式化
`_format_rich_daily_mrkdwn` 已于 v0.45.571 删除：编排器 Step 7 不传 --force、每天只 exit 2，
手动 --force 生产日志里从未出现过（v0.45.343 记 Bot 不在 #alpha-hive，现状待验证）；原第二类 `pre_scan_notify.py` v0.45.514 已删。）

v0.45.341 删掉了零生产调用方的发送方法：`send_risk_alert` / `send_opportunity_alert` /
`send_scan_progress` / `send_x_thread` / `send_daily_report` 及其 blocks 构建器与
`_send_slack_message`、连接测试 `test_connection`、以及从未被读过的失败重试队列
（`retry_failed` / `_enqueue_failed`：队列只活在单次进程的内存里，而两个调用方都是
发一条就退出的一次性进程 ⇒ 结构上不可能被重试）。想加新的消息类型，先改 CLAUDE.md 的
规则（用户决定），再改 `tests/test_slack_send_whitelist.py` 的白名单 —— 那里把这些
删掉的名字留作墓碑，把旧调用块原样放回会红。
"""

import os
import requests
from resilience import get_session
from typing import Dict, Optional
from hive_logger import get_logger

_log = get_logger("slack_report_notifier")

try:
    from config import SLACK_CHANNEL_ID as _SLACK_CH
except ImportError:
    _SLACK_CH = "C0AGUUWJXJS"


class SlackReportNotifier:
    """Slack 报告通知器（支持 User Token 和 Webhook 双模式）"""

    CHANNEL_ID = _SLACK_CH  # #alpha-hive（来源：config.SLACK_CHANNEL_ID）

    def __init__(self, webhook_url: Optional[str] = None):
        """
        初始化 Slack 报告通知器
        优先使用 User Token（以用户身份发送），降级到 Webhook
        """
        self.user_token = self._read_user_token()
        self.webhook_url = webhook_url or self._read_webhook_from_file()
        self.use_user_token = bool(self.user_token)

        # Webhook 存活检测：格式合法后做 HEAD 请求验证
        self._webhook_alive = False
        if self.webhook_url and self._is_valid_webhook(self.webhook_url):
            self._webhook_alive = self._check_webhook_alive(self.webhook_url)

        self.enabled = bool(self.user_token) or self._webhook_alive

    @staticmethod
    def _is_valid_webhook(url: str) -> bool:
        """校验 Slack Webhook URL 格式"""
        return bool(url and url.startswith("https://hooks.slack.com/"))

    @staticmethod
    def _check_webhook_alive(url: str) -> bool:
        """HEAD 请求验证 webhook 是否仍然有效（404 = 已失效）"""
        try:
            resp = requests.head(url, timeout=5)
            if resp.status_code == 404:
                _log.warning(
                    "Slack Webhook 已失效 (404)，自动禁用。"
                    "请到 Slack App 管理页面重新生成 Webhook URL。"
                )
                return False
            # 2xx/3xx/405 均视为存活（Slack webhook 对 HEAD 可能返回 405）
            return True
        except (requests.ConnectionError, requests.Timeout) as e:
            _log.warning("Slack Webhook 连接失败: %s，自动禁用", e)
            return False

    def _read_user_token(self) -> Optional[str]:
        """读取 Slack Token（xoxp- User Token 或 xoxb- Bot Token）"""
        # 0. config.get_secret（集中管理优先）
        try:
            from config import get_secret
            for secret_name in ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"):
                tok = get_secret(secret_name)
                if tok and tok.startswith(("xoxp-", "xoxb-")):
                    return tok
        except ImportError:
            pass
        # 1. 环境变量
        for env_key in ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"):
            env_tok = os.environ.get(env_key, "").strip()
            if env_tok and (env_tok.startswith("xoxp-") or env_tok.startswith("xoxb-")):
                return env_tok
        # 2. Token 文件（Bot Token 优先于 User Token）
        for token_path in (
            "~/.alpha_hive_slack_bot_token",
            "~/.alpha_hive_slack_user_token",
        ):
            try:
                with open(os.path.expanduser(token_path), 'r') as f:
                    tok = f.read().strip()
                    if tok.startswith(("xoxp-", "xoxb-")):
                        return tok
            except FileNotFoundError:
                pass
        return None

    def _read_webhook_from_file(self) -> Optional[str]:
        """从 config.get_secret > 环境变量 > 文件安全读取 Webhook URL"""
        try:
            from config import get_secret
            url = get_secret("SLACK_WEBHOOK_URL")
            if url:
                return url
        except ImportError:
            pass
        env_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if env_url:
            return env_url
        webhook_file = os.path.expanduser("~/.alpha_hive_slack_webhook")
        try:
            with open(webhook_file, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            return None

    def send_plain_text(self, text: str, channel: Optional[str] = None) -> bool:
        """发送纯文本消息到 `channel`（缺省 #alpha-hive）。发不进去就返回 False，不换目的地。"""
        if not self.enabled:
            _log.warning("Slack 未配置")
            return False

        if self.use_user_token:
            return self._send_via_api(text, channel or self.CHANNEL_ID)
        # 降级到 webhook
        return self._send_slack_message_payload({"text": text})

    def _send_via_api(self, text: str, channel: str) -> bool:
        """通过 Slack API 发到**指定的** `channel` —— 只发这一处，每条路径都返回 bool。

        v0.45.343 删掉了「频道回 not_in_channel / channel_not_found ⇒ 自动改发私信用户」的降级
        （原 `config.SLACK_DM_FALLBACK`）。Bot 从未被邀请进 #alpha-hive，于是这条降级：
          * 让 `push_report_to_slack --force`（v0.45.571 已删）每次把日报**实发到私信**，调用方只拿到 True，
            照样记「✅ 日报已成功推送到 Slack #alpha-hive」—— 目的地变了，下游无从得知；
          * 是 v0.45.339 那批被禁告警漏成私信的机制；
          * 频道与私信都回 channel 类错误时循环跑空，返回 None。
        2026-03-13 已判定它「与频道推送意图不符」，当时只把脚本改成默认跳过，降级本身留着。
        要发私信就显式把用户 ID 当 `channel` 传进来（已删的 `pre_scan_notify` 当年就是自己这么发的）。
        """
        try:
            from resilience import slack_breaker
            if not slack_breaker.allow_request():
                _log.warning("Slack circuit breaker OPEN, skipping")
                return False
        except ImportError:
            slack_breaker = None

        try:
            response = get_session("slack").post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self.user_token}"},
                json={"channel": channel, "text": text, "unfurl_links": False},
                timeout=15,
            )
            data = response.json()
        except requests.exceptions.RequestException as e:
            if slack_breaker:
                try:
                    slack_breaker.record_failure()
                except Exception as e2:
                    _log.debug("circuit_breaker.record_failure() failed: %s", e2)
            _log.error("Slack 发送失败: %s", e)
            return False

        if data.get("ok"):
            if slack_breaker:
                slack_breaker.record_success()
            _log.info("Slack 消息发送成功（用户身份 → %s）", channel)
            return True
        err = data.get("error", "unknown")
        if err in ("not_in_channel", "channel_not_found"):
            _log.warning("Slack 发送失败（→ %s）：%s —— Bot 不在该频道或频道不存在；"
                         "不会改发私信，把 Bot 邀请进频道后重试", channel, err)
        else:
            _log.warning("Slack API 错误（→ %s）: %s", channel, err)
        return False

    def _send_slack_message_payload(self, payload: Dict) -> bool:
        """发送 Slack 消息（webhook 模式）"""

        if not self.webhook_url:
            return False

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

            if response.status_code == 200:
                _log.info("Slack 消息发送成功")
                return True
            else:
                _log.warning("Slack 返回状态码: %s", response.status_code)
                return False

        except requests.exceptions.RequestException as e:
            try:
                from resilience import slack_breaker as _sb
                _sb.record_failure()
            except ImportError:
                pass
            _log.error("Slack 发送失败: %s", e)
            return False


if __name__ == "__main__":
    # v0.45.339：手动入口**只报配置状态，不发任何消息**。此前这里连发连接测试 /
    # 机会告警 / 风险告警 / 扫描进度四条 —— 后三类正是 CLAUDE.md「Slack 通知精简
    # 规则」明令禁止的类型。v0.45.571 起白名单为空（没有任何模块许发起发送）；
    # 要加回一类消息先由用户改规则。守卫 tests/test_slack_send_whitelist.py。
    notifier = SlackReportNotifier()
    mode = "Token" if notifier.use_user_token else ("Webhook" if notifier.enabled else "无")
    print(f"Slack 通知器：enabled={notifier.enabled}  模式={mode}  频道={notifier.CHANNEL_ID}")
