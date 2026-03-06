#!/usr/bin/env python3
"""
💬 Alpha Hive Slack 报告通知器 - Phase 3 P6
专门用于将投资简报和各类信息推送到 Slack
替代 Gmail，提供实时、富文本的通知体验
"""

import json
import logging
import os
import time
import hashlib
import requests
from pathlib import Path
from resilience import get_session
from collections import deque
from typing import Any, Dict, List, Optional
from datetime import datetime

_log = logging.getLogger("alpha_hive.slack_report_notifier")

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
        self._failed_queue: deque = deque(maxlen=50)
        self._sent_hashes: Dict[str, float] = {}  # hash → timestamp, 去重用

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
        """读取 Slack User Token（xoxp-...）"""
        env_tok = os.environ.get("SLACK_USER_TOKEN", "").strip()
        if env_tok and env_tok.startswith("xoxp-"):
            return env_tok
        token_file = os.path.expanduser("~/.alpha_hive_slack_user_token")
        try:
            with open(token_file, 'r') as f:
                tok = f.read().strip()
                if tok.startswith("xoxp-"):
                    return tok
        except FileNotFoundError:
            pass
        return None

    def _read_webhook_from_file(self) -> Optional[str]:
        """从环境变量或文件安全读取 Webhook URL"""
        env_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if env_url:
            return env_url
        webhook_file = os.path.expanduser("~/.alpha_hive_slack_webhook")
        try:
            with open(webhook_file, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
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
            _log.warning("Slack 通知已禁用")
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

    # ------------------------------------------------------------------
    # 富文本日报（March-3 格式）
    # ------------------------------------------------------------------

    def send_rich_daily_report(
        self,
        report_json_path: str,
        cache_dir: str,
        data_cache_dir: str,
        finviz_cache_dir: str,
        dashboard_url: str = "https://igg-wang748.github.io/alpha-hive-dashboard/",
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
            metrics = self._load_json(
                os.path.join(cache_dir, f"metrics_{ticker}_{date_str}.json")
            )
            if metrics:
                yf = metrics.get("sources", {}).get("yahoo_finance", {})
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

    def send_plain_text(self, text: str, channel: Optional[str] = None) -> bool:
        """发送纯文本消息（02-27 格式，优先用 User Token）"""
        if not self.enabled:
            _log.warning("Slack 未配置")
            return False

        if self.use_user_token:
            return self._send_via_api(text, channel or self.CHANNEL_ID)
        # 降级到 webhook
        return self._send_slack_message_payload({"text": text})

    def _send_via_api(self, text: str, channel: str) -> bool:
        """通过 Slack API 以用户身份发送"""
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
            if data.get("ok"):
                if slack_breaker:
                    slack_breaker.record_success()
                _log.info("Slack 消息发送成功（用户身份）")
                return True
            else:
                _log.warning("Slack API 错误: %s", data.get("error", "unknown"))
                return False
        except requests.exceptions.RequestException as e:
            if slack_breaker:
                try:
                    slack_breaker.record_failure()
                except Exception:
                    pass
            self._enqueue_failed("api", text)
            _log.error("Slack 发送失败: %s", e)
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
            text = payload.get("text", "")
            if text:
                self._enqueue_failed("webhook", text)
            _log.error("Slack 发送失败: %s", e)
            return False

    def _enqueue_failed(self, method: str, text: str):
        """将失败消息加入重试队列（去重：5 分钟内相同内容不重复入队）"""
        h = hashlib.md5(text[:200].encode()).hexdigest()
        now = time.time()
        if h in self._sent_hashes and now - self._sent_hashes[h] < 300:
            return  # 5 分钟内重复，跳过
        self._sent_hashes[h] = now
        self._failed_queue.append({"method": method, "text": text, "ts": now, "hash": h})

    def retry_failed(self) -> int:
        """重试队列中的失败消息，返回成功数"""
        if not self._failed_queue:
            return 0
        succeeded = 0
        remaining = deque(maxlen=50)
        while self._failed_queue:
            item = self._failed_queue.popleft()
            # 超过 1 小时的消息丢弃
            if time.time() - item["ts"] > 3600:
                continue
            ok = False
            if item["method"] == "api" and self.use_user_token:
                ok = self._send_via_api(item["text"], self.CHANNEL_ID)
            elif item["method"] == "webhook":
                ok = self._send_slack_message_payload({"text": item["text"]})
            if ok:
                succeeded += 1
                self._sent_hashes[item["hash"]] = time.time()
            else:
                remaining.append(item)
        self._failed_queue = remaining
        return succeeded

    def test_connection(self) -> bool:
        """测试 Slack 连接"""

        if not self.enabled:
            _log.error("Slack 未配置")
            return False

        mode = "User Token" if self.use_user_token else "Webhook"
        msg = f"✅ Alpha Hive Slack 连接测试成功！\n模式：{mode}\n🐝 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        if self.use_user_token:
            success = self._send_via_api(msg, self.CHANNEL_ID)
        else:
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": msg}}
            ]
            success = self._send_slack_message(blocks)

        if success:
            _log.info("Slack 连接测试通过（%s）", mode)
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
