#!/usr/bin/env python3
"""
💬 Alpha Hive Slack 报告通知器

CLAUDE.md「Slack 通知精简规则」只许两类消息，本模块只服务这两处：
  ① `pre_scan_notify.py`（LLM 模式确认）—— 只借用构造出的 token / 状态，自己发
  ② `push_report_to_slack.py --force`（富文本日报）—— `send_rich_daily_report`

v0.45.341 删掉了零生产调用方的发送方法：`send_risk_alert` / `send_opportunity_alert` /
`send_scan_progress` / `send_x_thread` / `send_daily_report` 及其 blocks 构建器与
`_send_slack_message`、连接测试 `test_connection`、以及从未被读过的失败重试队列
（`retry_failed` / `_enqueue_failed`：队列只活在单次进程的内存里，而两个调用方都是
发一条就退出的一次性进程 ⇒ 结构上不可能被重试）。想加新的消息类型，先改 CLAUDE.md 的
规则（用户决定），再改 `tests/test_slack_send_whitelist.py` 的白名单 —— 那里把这些
删掉的名字留作墓碑，把旧调用块原样放回会红。
"""

import json
import os
import requests
from resilience import get_session
from typing import Any, Dict, List, Optional
from datetime import datetime
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

    # ------------------------------------------------------------------
    # 富文本日报（March-3 格式）
    # ------------------------------------------------------------------

    def send_rich_daily_report(
        self,
        report_json_path: str,
        cache_dir: str,
        data_cache_dir: str,
        finviz_cache_dir: str,
        dashboard_url: str = "https://wangmingjie36-creator.github.io/alpha-hive-deploy/",
        *,
        llm_mode: bool = True,
    ) -> bool:
        """
        读取报告 JSON + 各级缓存，生成 March-3 富文本格式并推送到 Slack。

        Args:
            report_json_path: alpha-hive-daily-YYYY-MM-DD.json 的绝对路径
            cache_dir:        cache/ 目录（含 metrics_*.json, fear_greed.json）
            data_cache_dir:   data_cache/ 目录（含 social_*.json, short_*.json）
            finviz_cache_dir: finviz_cache/ 目录（含 *_sentiment.json）
            dashboard_url:    GitHub Pages URL
            llm_mode:         是否为 LLM 增强模式
        Returns:
            是否发送成功
        """
        if not self.enabled:
            _log.warning("Slack 通知未启用，跳过富文本日报")
            return False

        try:
            text = self._format_rich_daily_mrkdwn(
                report_json_path, cache_dir, data_cache_dir,
                finviz_cache_dir, dashboard_url, llm_mode=llm_mode,
            )
        except Exception as exc:
            _log.error("构建富文本日报失败: %s", exc, exc_info=True)
            return False

        return self.send_plain_text(text)

    # ---- 内部：构建 mrkdwn 纯文本 ----

    @staticmethod
    def _load_json(path: str) -> Any:
        """安全加载 JSON，失败返回 None"""
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _format_rich_daily_mrkdwn(
        self,
        report_json_path: str,
        cache_dir: str,
        data_cache_dir: str,
        finviz_cache_dir: str,
        dashboard_url: str,
        *,
        llm_mode: bool = True,
    ) -> str:
        """
        构建 March-3 风格 Slack mrkdwn 文本。

        返回一段完整的 Slack 消息文本（纯 mrkdwn，无 Block Kit）。
        """
        report = self._load_json(report_json_path)
        if not report:
            raise FileNotFoundError(f"无法加载报告: {report_json_path}")

        project_dir = os.path.dirname(os.path.abspath(report_json_path))
        date_str = report.get("date", datetime.now().strftime("%Y-%m-%d"))
        opps = sorted(
            report.get("opportunities", []),
            key=lambda x: x.get("opp_score", 0),
            reverse=True,
        )
        total_tickers = len(opps)
        mode_label = "LLM 增强模式" if llm_mode else "规则引擎模式"

        # ── 加载宏观 ──
        fg_data = self._load_json(os.path.join(cache_dir, "fear_greed.json")) or {}
        fg_value = fg_data.get("value", "?")
        fg_class = fg_data.get("classification", "?")

        # ── 加载每个标的的补充数据 ──
        ticker_extras: Dict[str, Dict] = {}
        all_tickers = [o["ticker"] for o in opps]

        for ticker in all_tickers:
            extras: Dict[str, Any] = {}

            # 价格 / 5d 变动
            # 优先从扫描时写入的 analysis-{ticker}-ml-{date}.json 读取实时价格
            # （避免 metrics 凌晨快照价格过时；ScoutBeeNova 在扫描时拉取真实价格）
            analysis_json = self._load_json(
                os.path.join(project_dir, f"analysis-{ticker}-ml-{date_str}.json")
            )
            if analysis_json:
                _scout_price = (
                    analysis_json.get("swarm_results", {})
                    .get("agent_details", {})
                    .get("ScoutBeeNova", {})
                    .get("details", {})
                    .get("price")
                )
                if _scout_price:
                    extras["price"] = _scout_price
            # {ticker}_raw.json 仅在手动 collect_data.py 运行时生成，优先级高于 analysis
            raw_json = self._load_json(os.path.join(project_dir, f"{ticker}_raw.json"))
            if raw_json and raw_json.get("price"):
                extras["price"] = raw_json["price"]
            metrics = self._load_json(
                os.path.join(cache_dir, f"metrics_{ticker}_{date_str}.json")
            )
            if metrics:
                yf = metrics.get("sources", {}).get("yahoo_finance", {})
                if not extras.get("price"):
                    extras["price"] = yf.get("current_price")
                extras["chg_5d"] = yf.get("price_change_5d")

            # 社交情绪
            social = self._load_json(
                os.path.join(data_cache_dir, f"social_{ticker}.json")
            )
            if social:
                extras["bullish_pct"] = social.get("bullish_pct")

            # 空头比例
            short_data = self._load_json(
                os.path.join(data_cache_dir, f"short_{ticker}.json")
            )
            if short_data:
                extras["short_pct"] = short_data.get("short_pct_float")

            ticker_extras[ticker] = extras

        # ── 计算共振（score >= 6.0 且方向非中性）──
        resonance_count = sum(
            1 for o in opps
            if o.get("opp_score", 0) >= 6.0 and o.get("direction", "中性") != "中性"
        )

        # ── 宏观情绪标签 ──
        if isinstance(fg_value, (int, float)):
            if fg_value <= 25:
                macro_tag = "RISK_OFF"
            elif fg_value >= 75:
                macro_tag = "RISK_ON"
            else:
                macro_tag = "NEUTRAL"
            fg_emoji = "🔴" if fg_value <= 25 else ("🟢" if fg_value >= 75 else "🟡")
        else:
            macro_tag = "N/A"
            fg_emoji = "⚪"

        # ── 构建消息 ──
        lines: List[str] = []

        # Header
        lines.append(
            f"🐝 *【{date_str}】Alpha Hive 蜂群日报*  {mode_label}"
        )
        lines.append(
            f"今日摘要 | {total_tickers} 标的 | 共振 {resonance_count}/{total_tickers}"
        )
        lines.append("─────────────────────────")

        # 标的评分列表
        lines.append("*📊 标的评分*")
        lines.append("")
        for opp in opps:
            tk = opp["ticker"]
            score = opp.get("opp_score", 0)
            direction = opp.get("direction", "中性")
            opt_sig = opp.get("options_signal", "")
            ex = ticker_extras.get(tk, {})

            # 方向 emoji
            if direction == "看多":
                dir_label = "BULLISH 📈"
            elif direction == "看空":
                dir_label = "BEARISH 📉"
            else:
                dir_label = "中性"

            # 共振标记
            resonance_mark = ""
            if score >= 6.0 and direction != "中性":
                resonance_mark = "  共振"

            # 价格片段
            price_frag = ""
            if ex.get("price"):
                price_frag = f"  ${ex['price']}"
                if isinstance(ex.get("chg_5d"), (int, float)):
                    price_frag += f" (5d {ex['chg_5d']:+.1f}%)"

            # short 片段
            short_frag = ""
            if ex.get("short_pct") and ex["short_pct"] > 0.05:
                short_frag = f" | short {ex['short_pct']*100:.1f}%"

            line = f"• *{tk}*  `{score:.1f}/10`  {dir_label}{resonance_mark}{price_frag} | {opt_sig}{short_frag}"
            lines.append(line)

        # 板块分布
        lines.append("")
        lines.append("*🔀 板块分布*")
        try:
            from config import WATCHLIST
            sector_map: Dict[str, List[str]] = {}
            for tk in all_tickers:
                sector = WATCHLIST.get(tk, {}).get("sector", "Other")
                sector_map.setdefault(sector, []).append(tk)
            sector_parts = [f"{s}: {' '.join(ts)}" for s, ts in sector_map.items()]
            lines.append(" | ".join(sector_parts))
        except ImportError:
            lines.append("N/A")

        # 宏观环境
        lines.append("")
        lines.append(f"*🌡️ 宏观环境*  `{macro_tag}`")
        lines.append(f"Fear & Greed: *{fg_value}* ({fg_class} {fg_emoji})")

        # 社交情绪
        lines.append("")
        lines.append("*📡 社交情绪*")
        social_parts = []
        for tk in all_tickers:
            bp = ticker_extras.get(tk, {}).get("bullish_pct")
            if bp is not None:
                flag = " 🟢" if bp >= 80 else (" 🔴" if bp <= 40 else "")
                social_parts.append(f"{tk} {bp:.0f}%{flag}")
        lines.append(" | ".join(social_parts) if social_parts else "N/A")

        # 报告链接
        lines.append("")
        lines.append(f"📎 <{dashboard_url}|完整报告>")
        lines.append(
            "_⚠️ 非投资建议。蜂群 AI 分析，所有交易决策需自行判断和风控。_"
        )

        return "\n".join(lines)

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
          * 让 `push_report_to_slack --force` 每次把日报**实发到私信**，调用方只拿到 True，
            照样记「✅ 日报已成功推送到 Slack #alpha-hive」—— 目的地变了，下游无从得知；
          * 是 v0.45.339 那批被禁告警漏成私信的机制；
          * 频道与私信都回 channel 类错误时循环跑空，返回 None。
        2026-03-13 已判定它「与频道推送意图不符」，当时只把脚本改成默认跳过，降级本身留着。
        要发私信就显式把用户 ID 当 `channel` 传进来（`pre_scan_notify` 就是自己这么发的）。
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
    # 规则」明令禁止的类型。真要验证发送链路，用白名单内的
    # `push_report_to_slack.py --force`（富文本日报）。守卫 tests/test_slack_send_whitelist.py。
    notifier = SlackReportNotifier()
    mode = "Token" if notifier.use_user_token else ("Webhook" if notifier.enabled else "无")
    print(f"Slack 通知器：enabled={notifier.enabled}  模式={mode}  频道={notifier.CHANNEL_ID}")
