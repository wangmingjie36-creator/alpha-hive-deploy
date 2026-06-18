"""Alpha Hive Bot · 每日简报推送

数据流：
  pdt_today() → fetch gh-pages /alpha-hive-daily-{date}.md →
  截取/格式化（Telegram 限制 4096 字符）→ 加免责声明 →
  遍历 active 订阅者推送

设计：
- 容错：fetch 失败重试 3 次（429/network）
- Telegram rate limit：每用户 1 msg/s，全局 ~30 msg/s，加 sleep
- 推送失败（user blocked bot / chat not found）→ 自动转 unsubscribed
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Optional

import httpx

from .config import BotConfig, DISCLAIMER, pdt_today
from .subscriber_db import SubscriberDB

if TYPE_CHECKING:
    from telegram import Bot


log = logging.getLogger("alpha_hive_bot.push")

# Telegram message limit 4096，留 buffer
MAX_MESSAGE_CHARS = 3800


async def fetch_daily_md(report_base_url: str, date: str, retries: int = 3) -> Optional[str]:
    """从 gh-pages 拉当日 Markdown 简报。失败返回 None。"""
    url = f"{report_base_url.rstrip('/')}/alpha-hive-daily-{date}.md"
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.text.strip():
                    return resp.text
                if resp.status_code == 404:
                    log.warning("daily.md 404 (date=%s)，扫描可能未完成或被护栏拦截", date)
                    return None
                log.warning("fetch daily.md attempt %d/%d: HTTP %d", attempt, retries, resp.status_code)
        except (httpx.RequestError, httpx.HTTPError) as e:
            log.warning("fetch daily.md attempt %d/%d: %s", attempt, retries, e)
        await asyncio.sleep(2 ** attempt)
    return None


async def fetch_latest_md(
    report_base_url: str, start_date: str, max_back_days: int = 7
) -> tuple[Optional[str], Optional[str]]:
    """从 start_date 往回找最近一份可用简报（容忍当日扫描未完成/日期边界）。

    返回 (date_str, md)；全部 404 返回 (None, None)。
    用于 /push_now：管理员手动触发时总是推送"最近一份"，而非死板要求当日。
    """
    from datetime import datetime as _dt, timedelta as _td

    try:
        d0 = _dt.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return None, None
    for i in range(max_back_days + 1):
        d = (d0 - _td(days=i)).strftime("%Y-%m-%d")
        md = await fetch_daily_md(report_base_url, d, retries=1)
        if md:
            if i > 0:
                log.info("当日(%s)无简报，回退到最近一份: %s", start_date, d)
            return d, md
    return None, None


# v0.31 分层：免费层简报截断更短并附升级 CTA；Pro 享完整版
FREE_BUDGET = 900
PAID_BUDGET = 3000
_UPGRADE_CTA = (
    "\n\n🔒 <b>免费层简报为摘要版</b>。升级 Pro 解锁完整简报 + /scan 全维 + "
    "/swarm 七蜂分歧 + /trend 走势 + 关注30/告警20。\n发送 /upgrade 查看（¥128/月 · ¥998/年）。"
)


def _clamp_html(s: str, max_len: int) -> str:
    """把已转义+美化的 HTML 片段硬截到 max_len，避免切在半个实体(&amp;)或标签(<b>)中间，
    并补齐被截断的未闭合 <b>（beautify 只产生非嵌套、成对的 <b>…</b>，故最多补 1 个）。"""
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    amp, semi = cut.rfind("&"), cut.rfind(";")
    if amp > semi:          # 末尾有未闭合实体 → 从 & 处断开
        cut = cut[:amp]
    lt, gt = cut.rfind("<"), cut.rfind(">")
    if lt > gt:             # 末尾有未闭合标签 → 从 < 处断开
        cut = cut[:lt]
    unclosed = cut.count("<b>") - cut.count("</b>")
    if unclosed > 0:
        cut += "</b>" * unclosed
    return cut


def format_for_telegram(md: str, date: str, tier: str = "paid") -> str:
    """将完整 Markdown 简报转为 Telegram HTML，截短到单消息上限 + 头部/免责声明。

    用 HTML parse mode（而非已废弃的 legacy Markdown）：先按字符预算截断，
    再 html.escape 整个 body（杜绝任意 < > & 以及 _ * 触发的解析崩溃——
    legacy Markdown 下 ticker/数字里的单个 _ * 会导致 BadRequest 不发送），
    最后在已转义文本上做几处安全的 markdown→HTML 美化（无注入风险）。

    tier='free' → 短预算 + 升级 CTA；tier='paid' → 完整预算。
    """
    import html as _html

    head = f"🐝 <b>Alpha Hive 每日简报</b> — {_html.escape(date)}\n\n"
    foot = (
        f"\n\n{DISCLAIMER}\n\n"
        "📊 完整 dashboard: https://wangmingjie36-creator.github.io/alpha-hive-deploy"
    )

    is_free = tier != "paid"
    # 保守预算（HTML 转义会膨胀长度，预留 buffer 确保最终 < 4096）
    budget = FREE_BUDGET if is_free else PAID_BUDGET
    body = md.strip().replace("```", "")  # 去代码围栏标记

    truncated = False
    if len(body) > budget:
        body = body[:budget].rsplit("\n", 1)[0]
        truncated = True

    body = _html.escape(body)  # 杜绝解析崩溃（核心防御）
    # 安全美化（在已转义文本上正则，内部无 < > &，不会注入）
    body = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", body)   # 标题行加粗
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)          # **粗体** → <b>

    tail = _UPGRADE_CTA if is_free else ""
    # 硬上限：html.escape 会膨胀长度（&→&amp; 等），极端高特殊字符占比的简报转义后仍可能
    # 超 Telegram 4096 → BadRequest 整条丢弃。按 MAX_MESSAGE_CHARS 对 body 二次钳制（保实体/
    # 标签边界），扣除 head/tail/foot + 截断提示余量。正常简报(实测最长 3367)不会触发。
    notice = "\n\n… (内容已截断，完整版见 dashboard)"
    overhead = len(head) + len(tail) + len(foot) + len(notice) + 8
    body = _clamp_html(body, MAX_MESSAGE_CHARS - overhead)
    if truncated and not is_free:
        body += notice

    return head + body + tail + foot


async def push_to_all(
    bot,  # telegram.Bot — 运行时类型，避免顶部强依赖
    db: SubscriberDB,
    text: Optional[str] = None,
    *,
    free_text: Optional[str] = None,
    paid_text: Optional[str] = None,
    cfg: Optional[BotConfig] = None,
    per_message_delay: float = 0.05,
) -> dict:
    """遍历推送 active 订阅者。返回 {sent, failed, deactivated}

    单文案模式：传 text（所有人同一份）。
    分层模式：传 free_text + paid_text（+ cfg 用于把管理员视为 paid），
    按每个订阅者的有效 tier 选文案。
    """
    from telegram.constants import ParseMode
    from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError

    tiered = free_text is not None and paid_text is not None
    sent = 0
    failed = 0
    deactivated = 0

    if tiered:
        subs = db.list_active_subscribers()
    else:
        subs = [{"user_id": None, "chat_id": cid} for cid in db.list_active_chat_ids()]
    log.info("push 开始：%d 个 active 订阅者（tiered=%s）", len(subs), tiered)

    for sub in subs:
        chat_id = sub["chat_id"]
        if tiered:
            uid = sub["user_id"]
            eff = "paid" if (cfg and uid in cfg.admin_user_ids) else db.get_tier(uid)
            msg = paid_text if eff == "paid" else free_text
        else:
            msg = text
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            sent += 1
        except RetryAfter as e:
            log.warning("rate limit, 等 %ds", e.retry_after)
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
                sent += 1
            except TelegramError as e2:
                log.error("重试后仍失败 chat=%d: %s", chat_id, e2)
                failed += 1
        except Forbidden:
            log.info("用户 chat=%d 屏蔽 bot，自动 unsubscribe", chat_id)
            # 通过 chat_id 反查 user_id 比较麻烦，简单做：直接更新 status
            with db._conn() as c:
                c.execute(
                    "UPDATE subscribers SET status='unsubscribed' WHERE chat_id=?",
                    (chat_id,),
                )
            deactivated += 1
        except BadRequest as e:
            log.warning("chat=%d BadRequest: %s", chat_id, e)
            failed += 1
        except TelegramError as e:
            log.error("chat=%d TelegramError: %s", chat_id, e)
            failed += 1
        await asyncio.sleep(per_message_delay)

    log.info("push 完成：sent=%d failed=%d deactivated=%d", sent, failed, deactivated)
    return {"sent": sent, "failed": failed, "deactivated": deactivated}


async def run_daily_push(cfg: BotConfig, db: SubscriberDB, bot=None, fallback: bool = False) -> dict:
    """触发一次推送。

    fallback=False（定时任务）：严格推当日简报，缺失则跳过（不重复推旧报）。
    fallback=True（/push_now 手动）：当日缺失时回退到最近一份可用简报。
    """
    today = pdt_today()
    if fallback:
        date, md = await fetch_latest_md(cfg.report_base_url, today)
    else:
        date = today
        md = await fetch_daily_md(cfg.report_base_url, date)
    if md is None:
        log.warning("date=%s 简报不可用（未生成/护栏拦截/回退耗尽），跳过推送", today)
        return {"sent": 0, "failed": 0, "deactivated": 0, "skipped": True, "date": today}

    # v0.31 分层：免费层摘要版 + 升级 CTA；Pro 完整版
    free_text = format_for_telegram(md, date, tier="free")
    paid_text = format_for_telegram(md, date, tier="paid")
    own_bot = bot is None
    if own_bot:
        from telegram import Bot as _Bot  # lazy import
        bot = _Bot(token=cfg.bot_token)
    try:
        result = await push_to_all(bot, db, free_text=free_text, paid_text=paid_text, cfg=cfg)
        result["skipped"] = False
        result["date"] = date
        return result
    finally:
        if own_bot:
            # python-telegram-bot 21+ Bot 是 async context；此处简单收尾
            pass
