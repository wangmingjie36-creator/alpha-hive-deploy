#!/usr/bin/env python3
"""
蜂群扫描预通知 + LLM 模式确认
编排脚本在 Step 2 前调用此脚本，等待用户确认是否启用 LLM 模式。

用法:
    python3 pre_scan_notify.py [--wait MINUTES] [--tickers TICKER...]

确认 LLM 模式的方式（任选其一）:
    1. 在 Slack 私信中回复 "llm" / "yes" / "y"
    2. touch /tmp/alpha_hive_use_llm_YYYYMMDD
    3. 在 ~/.alpha_hive_llm_mode 中写入 "always"

退出码:
    0 = 使用 --no-llm（规则引擎）
    42 = 使用 --use-llm（LLM 混合模式）
"""

import os
import sys
import time
import subprocess
import argparse
from datetime import datetime
from pathlib import Path
from hive_logger import get_logger

_log = get_logger("pre_scan_notify")

# Slack 通知发到用户私信（用 User ID，Bot 会自动建立 DM）
_DM_TARGET = "U0AGQK74NKV"

# Slack 回复中匹配这些关键词 → 启用 LLM
_LLM_KEYWORDS = {"llm", "yes", "y", "是", "确认", "ok", "use-llm"}
# 回复这些 → 不用 LLM，直接用规则引擎
_NO_KEYWORDS = {"no", "n", "否", "不", "skip", "free", "规则"}


def _get_slack_token() -> str | None:
    """读取 Bot Token 或 User Token"""
    try:
        from config import get_secret
        for secret_name in ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"):
            tok = get_secret(secret_name)
            if tok and tok.startswith(("xoxb-", "xoxp-")):
                return tok
    except ImportError:
        pass
    for env_key in ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"):
        tok = os.environ.get(env_key, "").strip()
        if tok.startswith(("xoxb-", "xoxp-")):
            return tok
    for token_path in ("~/.alpha_hive_slack_bot_token", "~/.alpha_hive_slack_user_token"):
        try:
            with open(os.path.expanduser(token_path)) as f:
                tok = f.read().strip()
            if tok.startswith(("xoxb-", "xoxp-")):
                return tok
        except FileNotFoundError:
            pass
    return None


def send_slack_notification(message: str, channel: str = _DM_TARGET) -> tuple[bool, str | None, str | None]:
    """
    发送 Slack 通知，返回 (成功?, message_ts, actual_channel_id)。
    actual_channel_id 是实际 DM 频道 ID（用于轮询回复）。

    三级 fallback：SlackReportNotifier → 裸 token → Webhook。
    同一个 token 只尝试一次，避免重复发送。
    """
    import requests
    _used_token: str | None = None  # 记录方式一已使用的 token，防止方式二重复

    # 1. 尝试通过 slack_report_notifier
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from slack_report_notifier import SlackReportNotifier
        notifier = SlackReportNotifier()
        if notifier.enabled and notifier.use_user_token:
            _used_token = notifier.user_token
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {notifier.user_token}"},
                json={"channel": channel, "text": message},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                return True, data.get("ts"), data.get("channel")
            _log.warning("Slack API ok=False (方式一): %s", data.get("error"))
    except Exception as e:
        _log.debug("SlackReportNotifier send failed: %s", e)

    # 2. 尝试裸 token 文件（仅当与方式一不同 token 时才尝试）
    tok = _get_slack_token()
    if tok and tok != _used_token:
        try:
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {tok}"},
                json={"channel": channel, "text": message},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                return True, data.get("ts"), data.get("channel")
            _log.warning("Slack API ok=False (方式二): %s", data.get("error"))
        except Exception as e:
            _log.debug("Slack token send failed: %s", e)

    # 3. 尝试 webhook（无法获取 message_ts 和 channel）
    try:
        webhook_path = os.path.expanduser("~/.alpha_hive_slack_webhook")
        if os.path.exists(webhook_path):
            with open(webhook_path) as f:
                url = f.read().strip()
            if url:
                resp = requests.post(url, json={"text": message}, timeout=10)
                if resp.ok and resp.text == "ok":
                    return True, None, None
    except Exception as e:
        _log.debug("Slack webhook send failed: %s", e)

    return False, None, None


def poll_slack_reply(channel: str, thread_ts: str, after_ts: str) -> str | None:
    """
    轮询 Slack DM，检查用户回复。
    返回: "yes" = 启用 LLM, "no" = 规则引擎, None = 无回复
    """
    tok = _get_slack_token()
    if not tok:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from slack_report_notifier import SlackReportNotifier
            notifier = SlackReportNotifier()
            if notifier.use_user_token:
                tok = notifier.user_token
        except Exception as e:
            _log.debug("SlackReportNotifier fallback failed: %s", e)
    if not tok:
        return None

    import requests

    headers = {"Authorization": f"Bearer {tok}"}

    def _check_messages(messages):
        for msg in messages:
            if msg.get("ts") == thread_ts:
                continue
            if msg.get("bot_id") or msg.get("subtype"):
                continue
            text = msg.get("text", "").strip().lower()
            if text in _LLM_KEYWORDS:
                return "yes"
            if text in _NO_KEYWORDS:
                return "no"
        return None

    # 检查方式 1: 线程回复
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.replies",
            headers=headers,
            params={"channel": channel, "ts": thread_ts, "oldest": after_ts},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            result = _check_messages(data.get("messages", []))
            if result:
                return result
    except Exception as e:
        _log.debug("Slack thread reply poll failed: %s", e)

    # 检查方式 2: DM 中的新消息
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.history",
            headers=headers,
            params={"channel": channel, "oldest": after_ts, "limit": 10},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            result = _check_messages(data.get("messages", []))
            if result:
                return result
    except Exception as e:
        _log.debug("Slack DM history poll failed: %s", e)

    return None


def send_macos_notification(title: str, message: str) -> bool:
    """通过 macOS 通知中心发送通知"""
    try:
        _esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{_esc(message)}" with title "{_esc(title)}" sound name "Glass"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
        return True
    except Exception as e:
        _log.debug("macOS notification failed: %s", e)
        return False


def _check_confirm(trigger_file: str, can_poll: bool, channel: str | None, poll_after: str) -> str | None:
    """
    检查用户是否确认。
    返回: "yes" = 用 LLM, "no" = 规则引擎, None = 无回复
    """
    if os.path.exists(trigger_file):
        print("LLM_MODE=yes (触发文件确认)")
        os.remove(trigger_file)
        return "yes"
    if can_poll and channel:
        reply = poll_slack_reply(channel, poll_after, poll_after)
        if reply:
            print(f"Slack 回复: {reply}")
            return reply
    return None


def main():
    parser = argparse.ArgumentParser(description="蜂群扫描 LLM 模式确认")
    parser.add_argument("--wait", type=int, default=10, help="首次等待分钟数 (默认 10)")
    parser.add_argument("--remind-interval", type=int, default=60, help="重复提醒间隔分钟数 (默认 60)")
    parser.add_argument("--max-wait", type=int, default=1440, help="最大等待分钟数 (默认 1440=24h)")
    parser.add_argument("--tickers", nargs="+", default=[], help="标的列表")
    args = parser.parse_args()

    date_str = datetime.now().strftime("%Y%m%d")
    trigger_file = f"/tmp/alpha_hive_use_llm_{date_str}"
    config_file = os.path.expanduser("~/.alpha_hive_llm_mode")

    # ── 检查永久配置 ──
    if os.path.exists(config_file):
        mode = Path(config_file).read_text().strip().lower()
        if mode == "always":
            print("LLM_MODE=always (配置文件)")
            sys.exit(42)
        elif mode == "never":
            print("LLM_MODE=never (配置文件)")
            sys.exit(0)
        # mode == "ask" → 继续询问

    # ── 检查今日触发文件是否已存在 ──
    if os.path.exists(trigger_file):
        print("LLM_MODE=yes (触发文件已存在)")
        os.remove(trigger_file)
        sys.exit(42)

    # ── 发送首次通知 ──
    tickers_str = " ".join(args.tickers) if args.tickers else "默认标的"

    def _build_slack_msg(reminder_num: int = 0) -> str:
        now_str = datetime.now().strftime("%H:%M")
        prefix = "" if reminder_num == 0 else f"⏰ *第 {reminder_num} 次提醒*\n\n"
        return (
            f"{prefix}🐝 *Alpha Hive 蜂群扫描等待确认*\n\n"
            f"📌 标的：{tickers_str}\n"
            f"⏰ 时间：{now_str}\n\n"
            f"*是否启用 LLM 模式？* （预估费用 ~$0.20）\n"
            f"> 回复 `llm` 或 `yes` → 启用 LLM 蜂群模式\n"
            f"> 回复 `no` → 使用规则引擎（免费）\n"
            f"> 不回复 → 每 {args.remind_interval // 60} 小时提醒一次"
        )

    slack_ok, msg_ts, actual_channel = send_slack_notification(_build_slack_msg(0))
    send_macos_notification("Alpha Hive 🐝", f"蜂群扫描等待确认，回复 Slack 或 touch {trigger_file}")

    can_poll_slack = slack_ok and msg_ts is not None and actual_channel is not None
    poll_after = msg_ts or str(time.time())

    if slack_ok:
        print(f"通知已发送: Slack ✅ (channel={actual_channel}, ts={msg_ts})")
    else:
        print(f"通知已发送: macOS 通知中心")

    def _handle_reply(reply: str | None) -> None:
        """处理回复结果，确认则 exit(42)，拒绝则 exit(0)"""
        if reply == "yes":
            send_slack_notification("✅ *LLM 模式已启用*，蜂群扫描开始执行...")
            sys.exit(42)
        elif reply == "no":
            send_slack_notification("👌 收到，使用 *规则引擎模式*（免费）开始扫描...")
            sys.exit(0)

    # ── 首次短等待（默认 5 分钟快速响应窗口）──
    first_deadline = time.time() + args.wait * 60
    print(f"首次等待 {args.wait} 分钟...")

    while time.time() < first_deadline:
        reply = _check_confirm(trigger_file, can_poll_slack, actual_channel, poll_after)
        _handle_reply(reply)
        time.sleep(10)

    # ── 循环提醒（每 remind_interval 分钟重发一次，直到回复或超时）──
    final_deadline = time.time() + (args.max_wait - args.wait) * 60
    reminder_count = 0

    print(f"未收到回复，进入循环提醒模式（每 {args.remind_interval} 分钟）...")

    while time.time() < final_deadline:
        reminder_count += 1
        print(f"发送第 {reminder_count} 次提醒...")

        slack_ok2, msg_ts2, ch2 = send_slack_notification(_build_slack_msg(reminder_count))
        send_macos_notification("Alpha Hive 🐝", f"第 {reminder_count} 次提醒：回复 Slack 确认 LLM 模式")

        # 更新轮询参考点
        if slack_ok2 and msg_ts2 and ch2:
            poll_after = msg_ts2
            actual_channel = ch2
            can_poll_slack = True

        # 在提醒间隔内持续轮询
        next_remind = time.time() + args.remind_interval * 60
        while time.time() < min(next_remind, final_deadline):
            reply = _check_confirm(trigger_file, can_poll_slack, actual_channel, poll_after)
            _handle_reply(reply)
            time.sleep(15)

    # ── 24 小时超时，放弃 ──
    print("LLM_MODE=no (24 小时超时，今日不执行扫描)")
    send_slack_notification("⏱️ *24 小时未收到确认，今日蜂群扫描已跳过*")
    sys.exit(0)


if __name__ == "__main__":
    main()
