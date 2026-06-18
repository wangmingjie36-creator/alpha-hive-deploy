"""Alpha Hive Bot · Telegram 命令处理器 + 主入口

启动：
  python -m alpha_hive_bot.bot

需要环境变量 BOT_TOKEN + ADMIN_USER_IDS。

定时推送：
  AsyncIO scheduled task，每天 PDT push_hour_pdt 触发 run_daily_push。
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from .config import BotConfig, DISCLAIMER, HELP, WELCOME_ACTIVE, WELCOME_NEW, pdt_today
from .push_job import run_daily_push
from .subscriber_db import SubscriberDB


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("alpha_hive_bot")

_PDT = ZoneInfo("America/Los_Angeles")


# ============================================================
# 命令处理器
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    status = db.activate_if_whitelisted(user.id, chat.id, user.username)
    if status in ("active", "already_active"):
        await update.message.reply_text(WELCOME_ACTIVE, parse_mode=ParseMode.HTML)
    elif status == "not_invited":
        msg = WELCOME_NEW + f"\n\n你的 user_id 是 <code>{user.id}</code>（请发给管理员申请白名单）"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif status == "revoked":
        await update.message.reply_text("❌ 你的访问已被管理员撤销。")
    elif status == "unsubscribed":
        await update.message.reply_text(
            "ℹ️ 你已退订。如需重新订阅请联系管理员重新加白名单。"
        )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db: SubscriberDB = ctx.application.bot_data["db"]
    user = update.effective_user
    if not user:
        return
    st = db.get_status(user.id) or "not_registered"
    label = {
        "active": "✅ 已订阅（每日推送中）",
        "whitelisted": "🟡 已在白名单（请发 /start 激活）",
        "unsubscribed": "⚪ 已退订",
        "revoked": "❌ 访问已撤销",
        "not_registered": "❌ 未注册（请联系管理员）",
    }.get(st, st)
    await update.message.reply_text(f"你的订阅状态：{label}\n\nuser_id: <code>{user.id}</code>", parse_mode=ParseMode.HTML)


async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db: SubscriberDB = ctx.application.bot_data["db"]
    user = update.effective_user
    if not user:
        return
    ok = db.unsubscribe(user.id)
    if ok:
        await update.message.reply_text("✅ 已退订。后续不再推送，但你的记录会保留。")
    else:
        await update.message.reply_text("ℹ️ 你当前不是 active 状态，无需退订。")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


# ── 管理员命令 ─────────────────────────────────────────

def _is_admin(user_id: int, cfg: BotConfig) -> bool:
    return user_id in cfg.admin_user_ids


async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    user = update.effective_user
    if not user or not _is_admin(user.id, cfg):
        return
    if not ctx.args:
        await update.message.reply_text("用法：/invite <user_id>")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是整数")
        return
    added = db.add_whitelist(target)
    await update.message.reply_text(
        f"{'✓ 已加白名单' if added else 'ℹ️ 已存在'}: user_id={target}"
    )


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    user = update.effective_user
    if not user or not _is_admin(user.id, cfg):
        return
    if not ctx.args:
        await update.message.reply_text("用法：/revoke <user_id>")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是整数")
        return
    ok = db.revoke(target)
    await update.message.reply_text(
        f"{'✓ 已撤销' if ok else '❌ 未找到该用户'}: user_id={target}"
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    user = update.effective_user
    if not user or not _is_admin(user.id, cfg):
        return
    rows = db.list_all()
    if not rows:
        await update.message.reply_text("（空）")
        return
    lines = ["订阅者列表："]
    for r in rows[:50]:
        un = _html.escape(f"@{r['username']}") if r["username"] else "—"
        lines.append(f"<code>{r['user_id']}</code> {un} · {r['status']} · {r['updated_at']}")
    if len(rows) > 50:
        lines.append(f"... 共 {len(rows)} 条，仅显示前 50")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_push_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    user = update.effective_user
    if not user or not _is_admin(user.id, cfg):
        return
    await update.message.reply_text("⏳ 触发即时推送中...")
    # 手动触发：当日缺失时回退到最近一份可用简报
    result = await run_daily_push(cfg, db, bot=ctx.application.bot, fallback=True)
    await update.message.reply_text(
        f"推送完成: sent={result.get('sent')} failed={result.get('failed')} "
        f"deactivated={result.get('deactivated')} skipped={result.get('skipped')} "
        f"date={result.get('date', pdt_today())}"
    )


async def cmd_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """管理员预览：只把简报发给自己（不推给其他订阅者），同时给出免费层 + Pro 两个版本。"""
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or not _is_admin(user.id, cfg):
        return
    await update.message.reply_text("⏳ 生成预览（仅发给你，不推给其他订阅者）...")
    from .push_job import fetch_latest_md, format_for_telegram
    date, md = await fetch_latest_md(cfg.report_base_url, pdt_today())
    if md is None:
        await update.message.reply_text("⚠️ gh-pages 上暂无可用简报（最近 7 天都没找到）。")
        return
    bot = ctx.application.bot
    # 标签与正文分开发，避免给 format 后的正文加前缀触发 4096 超限
    await bot.send_message(chat_id=chat.id, text=f"💎 <b>【Pro/管理员 会收到的版本】</b> — {date}",
                           parse_mode=ParseMode.HTML)
    await bot.send_message(chat_id=chat.id, text=format_for_telegram(md, date, tier="paid"),
                           parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    await bot.send_message(chat_id=chat.id, text="🆓 <b>【免费层 会收到的版本】</b>",
                           parse_mode=ParseMode.HTML)
    await bot.send_message(chat_id=chat.id, text=format_for_telegram(md, date, tier="free"),
                           parse_mode=ParseMode.HTML)
    await update.message.reply_text(
        f"✓ 预览完成（date={date}）。以上两条仅你可见，未推送给任何其他订阅者。\n"
        f"确认无误后用 /push_now 广播给全部订阅者。")


# ============================================================
# 定时推送 job
# ============================================================

# 简报未就绪时的重试间隔（秒）。扫描通常 PDT 21:03 才完成，远晚于 push_hour:30，
# 故推送窗口开启后需轮询等待，而非一次失败就放弃。
_POLL_INTERVAL = 1800  # 30 分钟


def _scheduler_decision(now_pdt, today, last_pushed_date, push_hour):
    """纯函数（便于单测）：返回 ('sleep', 秒) 或 ('push', None)。
    - 未到推送窗口（push_hour:30）→ 睡到窗口
    - 今天已成功推送 → 睡到次日 00:05
    - 在窗口内且今天还没成功推送 → 尝试推送
    """
    target = now_pdt.replace(hour=push_hour, minute=30, second=0, microsecond=0)
    if now_pdt < target:
        return ("sleep", (target - now_pdt).total_seconds())
    if last_pushed_date == today:
        tomorrow = (now_pdt + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return ("sleep", (tomorrow - now_pdt).total_seconds())
    return ("push", None)


async def _scheduler_loop(app: Application):
    """每个 PDT 交易日推送窗口（push_hour:30）开启后，轮询等待当日简报就绪即推送一次。

    关键修复（v0.31）：旧逻辑在 13:30 推、但扫描 21:03 才生成简报 → 13:30 fetch 404 →
    skip，却仍把 last_pushed_date 标成今天并睡到次日 → 当日简报永远推不出。
    现改为：仅在「真正推送成功」后才标记 last_pushed_date；简报未就绪则 30 分钟后重试，
    跨午夜 today 自然翻页停止当日重试（无简报的周末/假日不会推）。
    """
    cfg: BotConfig = app.bot_data["cfg"]
    db: SubscriberDB = app.bot_data["db"]
    last_pushed_date = None

    while True:
        try:
            now_pdt = datetime.now(_PDT)
            today = now_pdt.strftime("%Y-%m-%d")
            action, secs = _scheduler_decision(now_pdt, today, last_pushed_date, cfg.push_hour_pdt)
            if action == "push":
                log.info("定时器触发：尝试推送当日简报 %s", today)
                sent_ok = False
                try:
                    # fallback=False：严格只推当日简报，未就绪则返回 skipped（不回退旧报）
                    result = await run_daily_push(cfg, db, bot=app.bot, fallback=False)
                    log.info("定时推送结果: %s", result)
                    if not result.get("skipped"):
                        sent_ok = True
                        last_pushed_date = today  # 仅推送成功才标记，避免 skip 后当日不再重试
                        # v0.3 推送后评估告警规则（边沿触发），仅定时跑、不在 /push_now
                        try:
                            from .query_commands import evaluate_alerts
                            ar = await evaluate_alerts(app.bot, cfg, db)
                            log.info("告警评估结果: %s", ar)
                        except Exception as ae:
                            log.exception("告警评估失败（不影响推送）: %s", ae)
                except Exception as e:
                    log.exception("定时推送失败: %s", e)
                if sent_ok:
                    tomorrow = (now_pdt + timedelta(days=1)).replace(
                        hour=0, minute=5, second=0, microsecond=0
                    )
                    sleep_s = (tomorrow - now_pdt).total_seconds()
                else:
                    log.info("简报 %s 未就绪（扫描通常 PDT 21:03 完成），%d 分钟后重试",
                             today, _POLL_INTERVAL // 60)
                    sleep_s = _POLL_INTERVAL
            else:
                sleep_s = secs
            log.debug("scheduler 睡 %.0fs", sleep_s)
            await asyncio.sleep(max(60, sleep_s))
        except asyncio.CancelledError:
            log.info("scheduler 已取消")
            return
        except Exception as e:
            log.exception("scheduler 异常（继续运行）: %s", e)
            await asyncio.sleep(300)


# ============================================================
# 主入口
# ============================================================

def build_application() -> Application:
    cfg = BotConfig.from_env()
    db = SubscriberDB(cfg.db_path)
    app = Application.builder().token(cfg.bot_token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["db"] = db

    # 用户命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("help", cmd_help))
    # 管理员命令
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("preview", cmd_preview))
    app.add_handler(CommandHandler("push_now", cmd_push_now))
    # v0.2 查询命令（/scan /top /swarm /scorecard /fg）
    from .query_commands import register as _register_query
    _register_query(app)

    # 注册启动时拉起 scheduler
    async def _post_init(app: Application):
        app.bot_data["_scheduler_task"] = asyncio.create_task(_scheduler_loop(app))
        log.info("Alpha Hive Bot 启动 · admin=%s · push_hour=%d PDT", cfg.admin_user_ids, cfg.push_hour_pdt)

    app.post_init = _post_init
    return app


def main():
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
