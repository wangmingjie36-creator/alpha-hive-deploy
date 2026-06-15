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


def format_for_telegram(md: str, date: str) -> str:
    """将完整 Markdown 简报转为 Telegram HTML，截短到单消息上限 + 头部/免责声明。

    用 HTML parse mode（而非已废弃的 legacy Markdown）：先按字符预算截断，
    再 html.escape 整个 body（杜绝任意 < > & 以及 _ * 触发的解析崩溃——
    legacy Markdown 下 ticker/数字里的单个 _ * 会导致 BadRequest 不发送），
    最后在已转义文本上做几处安全的 markdown→HTML 美化（无注入风险）。
    """
    import html as _html

    head = f"🐝 <b>Alpha Hive 每日简报</b> — {_html.escape(date)}\n\n"
    foot = (
        f"\n\n{DISCLAIMER}\n\n"
        "📊 完整 dashboard: https://wangmingjie36-creator.github.io/alpha-hive-deploy"
    )

    # 保守预算（HTML 转义会膨胀长度，预留 buffer 确保最终 < 4096）
    budget = 3000
    body = md.strip().replace("```", "")  # 去代码围栏标记

    truncated = False
    if len(body) > budget:
        body = body[:budget].rsplit("\n", 1)[0]
        truncated = True

    body = _html.escape(body)  # 杜绝解析崩溃（核心防御）
    # 安全美化（在已转义文本上正则，内部无 < > &，不会注入）
    body = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", body)   # 标题行加粗
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)          # **粗体** → <b>
    if truncated:
        body += "\n\n… (内容已截断，完整版见 dashboard)"

    return head + body + foot


async def push_to_all(
    bot,  # telegram.Bot — 运行时类型，避免顶部强依赖
    db: SubscriberDB,
    text: str,
    *,
    per_message_delay: float = 0.05,
) -> dict:
    """遍历推送 active 订阅者。返回 {sent, failed, deactivated}"""
    from telegram.constants import ParseMode
    from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError

    sent = 0
    failed = 0
    deactivated = 0
    chat_ids = db.list_active_chat_ids()
    log.info("push 开始：%d 个 active 订阅者", len(chat_ids))

    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            sent += 1
        except RetryAfter as e:
            log.warning("rate limit, 等 %ds", e.retry_after)
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
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

    text = format_for_telegram(md, date)
    own_bot = bot is None
    if own_bot:
        from telegram import Bot as _Bot  # lazy import
        bot = _Bot(token=cfg.bot_token)
    try:
        result = await push_to_all(bot, db, text)
        result["skipped"] = False
        result["date"] = date
        return result
    finally:
        if own_bot:
            # python-telegram-bot 21+ Bot 是 async context；此处简单收尾
            pass
