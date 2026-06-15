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
    "📖 <b>Alpha Hive Bot 命令</b>\n\n"
    "/start — 开始\n"
    "/status — 订阅状态\n"
    "/unsubscribe — 取消订阅\n"
    "/help — 此帮助\n\n"
    "<b>管理员命令</b>：\n"
    "/invite &lt;user_id&gt; — 邀请用户加入白名单\n"
    "/revoke &lt;user_id&gt; — 移除白名单\n"
    "/list — 查看订阅者\n"
    "/push_now — 立即推送当日简报\n\n"
    f"{DISCLAIMER}"
)
