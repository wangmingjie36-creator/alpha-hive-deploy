"""Alpha Hive Bot · 配置（环境变量 + 文案）

环境变量：
- BOT_TOKEN: Telegram BotFather 给的 token（必需）
- ADMIN_USER_IDS: 逗号分隔的管理员 TG user_id（必需，至少 1 个）
- DB_PATH: 订阅者 SQLite 路径（默认 ./subscribers.db）
- REPORT_BASE_URL: 简报来源 URL 前缀（默认 gh-pages）
- PUSH_HOUR_PDT: 每日推送小时（PDT 0-23，默认 13 即收盘后 30 分钟）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

_PDT = ZoneInfo("America/Los_Angeles")


def pdt_today() -> str:
    """美股交易日（与项目 hive_logger.pdt_today 同义，但 bot 独立部署不依赖项目代码）"""
    return datetime.now(_PDT).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class BotConfig:
    bot_token: str
    admin_user_ids: frozenset[int]
    db_path: str
    report_base_url: str
    push_hour_pdt: int

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN 未设置，去 https://t.me/BotFather 创建 bot 拿 token")

        admins_raw = os.environ.get("ADMIN_USER_IDS", "").strip()
        if not admins_raw:
            raise RuntimeError(
                "ADMIN_USER_IDS 未设置。给 @userinfobot 发消息拿 user_id，"
                "然后设置 ADMIN_USER_IDS=12345678"
            )
        try:
            admins = frozenset(int(x.strip()) for x in admins_raw.split(",") if x.strip())
        except ValueError as e:
            raise RuntimeError(f"ADMIN_USER_IDS 解析失败（应为逗号分隔整数）: {e}")

        return cls(
            bot_token=token,
            admin_user_ids=admins,
            db_path=os.environ.get("DB_PATH", "./subscribers.db"),
            report_base_url=os.environ.get(
                "REPORT_BASE_URL",
                "https://wangmingjie36-creator.github.io/alpha-hive-deploy",
            ),
            push_hour_pdt=int(os.environ.get("PUSH_HOUR_PDT", "13")),
        )


# ============================================================
# 文案（合规免责声明硬性要求）
# ============================================================

# 注意：所有文案使用 HTML parse mode（ParseMode.HTML）。
# 仅 < > & 需转义；下划线/星号在 HTML 模式下是字面量，不会触发 legacy Markdown 解析崩溃。
# （历史 bug：legacy Markdown 下 "user_id" 的单下划线被当斜体标记 → BadRequest 不回复）

DISCLAIMER = (
    "⚠️ <b>免责声明</b>：本简报由 Alpha Hive 蜂群系统自动生成，"
    "仅供研究参考，不构成任何投资建议、买卖推荐或个性化理财服务。"
    "投资有风险，决策由用户自行判断并承担后果。"
)

WELCOME_NEW = (
    "🐝 <b>欢迎使用 Alpha Hive 投研简报</b>\n\n"
    "本服务目前仅对受邀用户开放。\n"
    "如需订阅，请联系管理员申请白名单。\n\n"
    "命令：\n"
    "/status — 查看订阅状态\n"
    "/help — 命令列表\n\n"
    f"{DISCLAIMER}"
)

WELCOME_ACTIVE = (
    "✅ <b>你已订阅 Alpha Hive 每日简报</b>\n\n"
    "每日美股盘后 PDT 13:30 自动推送（约北京时间 04:30）。\n"
    "/status — 订阅状态\n"
    "/unsubscribe — 取消订阅\n"
    "/help — 命令列表\n\n"
    f"{DISCLAIMER}"
)

HELP = (
    "📖 <b>Alpha Hive Bot · 命令一览</b>\n\n"

    "👤 <b>账户</b>（所有人）\n"
    "/start — 开始 / 激活订阅（需先被邀请）\n"
    "/status — 查看我的订阅状态\n"
    "/mytier — 查看我的会员等级（免费 / Pro）\n"
    "/upgrade — 升级 Pro（¥128/月 · ¥998/年）\n"
    "/unsubscribe — 取消订阅\n"
    "/help — 显示本帮助\n\n"

    "📊 <b>查询</b>（限已订阅用户）\n"
    "/scan &lt;代码&gt; — 单标的：综合分 + 方向（🆓）｜+5维雷达/蜂群投票/ML报告（💎Pro）\n"
    "/top [N] — 机会榜：Top 3（🆓）｜全榜 + 共振 + 方向分布（💎Pro）\n"
    "/scorecard — 系统历史战绩（方向准确率 + 周度 + 模拟组合）\n"
    "/fg — 市场恐惧贪婪指数 + 近期走势\n"
    "/swarm &lt;代码&gt; — 7蜂逐票 + 共识 + 共振分歧 <b>（💎Pro）</b>\n"
    "/trend &lt;代码&gt; — 单标的综合分历史走势 <b>（💎Pro）</b>\n"
    "/movers — 较上一交易日分数变动榜 + 方向翻转 <b>（💎Pro）</b>\n"
    "<i>例：/scan NVDA · /top 5 · /swarm AMZN</i>\n\n"

    "📌 <b>关注列表</b>（限已订阅用户）\n"
    "/watch &lt;代码&gt; — 添加关注（🆓 上限 3 · 💎Pro 30）\n"
    "/unwatch &lt;代码&gt; — 移除关注\n"
    "/mywatch — 查看我的关注（带当日分数）\n\n"

    "🔔 <b>阈值告警</b>（限已订阅用户）\n"
    "/alert &lt;代码&gt; score&gt;7 — 订阅告警（🆓 1 条 · 💎Pro 20 条）\n"
    "/alerts — 查看我的告警规则\n"
    "/unalert &lt;编号&gt; — 删除告警\n"
    "<i>每日盘后评估一次，命中即推送（非盘中实时）。首次命中赠 7 天 Pro 体验 🎁</i>\n\n"

    "💎 <b>Pro 会员</b>\n"
    "Pro 解锁：完整每日简报（免费为摘要版）+ 上面标 💎 的全部功能 + 更高关注/告警额度。\n"
    "价格：¥128 / 月 · ¥998 / 年（约 6.5 折）。/upgrade 查看付款方式。\n"
    "<i>Pro 提供更广/更深/更主动的研究数据访问，不构成投资建议、不保证收益。</i>\n\n"

    "🔑 <b>管理员</b>\n"
    "/invite &lt;user_id&gt; — 邀请用户加白名单\n"
    "/revoke &lt;user_id&gt; — 撤销用户访问\n"
    "/grant &lt;user_id&gt; &lt;月数&gt; — 收款后手动开通 Pro\n"
    "/list — 查看所有订阅者\n"
    "/push_now — 立即推送当日简报\n\n"

    "⏰ <b>自动推送</b>\n"
    "每个美股交易日盘后 PDT 13:30（约北京 04:30）自动推送当日简报给订阅者。\n\n"

    f"{DISCLAIMER}"
)
