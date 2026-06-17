"""Alpha Hive Bot · v0.2 查询命令（Quick Wins）

/scan /top /swarm /scorecard /fg —— 全部只读 gh-pages 的 dashboard-data.json，
零实时扫描、零主项目依赖（仅 httpx + 标准库）。所有命令限 active 订阅者，
输出统一 HTML parse mode + 免责声明，措辞中立（研究输出，非买卖建议）。
"""
from __future__ import annotations

import html as _html
import logging
import math
from typing import Optional

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import BotConfig, DISCLAIMER
from .subscriber_db import SubscriberDB

log = logging.getLogger("alpha_hive_bot.query")

# radar 5 维顺序（与 dashboard-data.json 一致）
RADAR_DIMS = ["信号", "催化", "情绪", "赔率", "风险"]
# 7 蜂英文名 → 中文
BEE_CN = {
    "Scout": "侦察", "Oracle": "预期", "Buzz": "情绪", "Chronos": "催化",
    "Rival": "竞争", "Guard": "风控", "Bear": "看空",
}
DIR_BADGE = {"中性": "⚪ 中性", "看多": "🟢 看多", "看空": "🔴 看空"}
BEE_DIR = {"bull": "🟢", "bear": "🔴", "neut": "⚪"}
_FOOT = f"\n\n{DISCLAIMER}"


# ── 数据拉取 ─────────────────────────────────────────────
async def fetch_dashboard(base_url: str, retries: int = 2) -> Optional[dict]:
    """拉 gh-pages dashboard-data.json，失败返回 None。"""
    url = f"{base_url.rstrip('/')}/dashboard-data.json"
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(url, params={"nc": "bot"})
                if r.status_code == 200 and r.text.strip():
                    return r.json()  # Python json 接受 NaN，显示层再防
                log.warning("fetch dashboard attempt %d/%d: HTTP %d", attempt, retries, r.status_code)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("fetch dashboard attempt %d/%d: %s", attempt, retries, e)
    return None


# ── 小工具 ───────────────────────────────────────────────
def _require_active(db: SubscriberDB, user_id: int) -> bool:
    return db.get_status(user_id) == "active"

async def _gate(update: Update, db: SubscriberDB) -> bool:
    """非 active 订阅者拦截，返回 True 表示放行。"""
    user = update.effective_user
    if not user:
        return False
    if not _require_active(db, user.id):
        await update.message.reply_text(
            "ℹ️ 此功能仅限已订阅用户。请先 /start 激活（需管理员邀请加白名单）。"
        )
        return False
    return True

def _norm_ticker(arg: str) -> str:
    return arg.strip().lstrip("$").upper()

def _fmt_num(x, nd=1, dash="—") -> str:
    """安全格式化数字，NaN/None → dash。"""
    try:
        if x is None:
            return dash
        xf = float(x)
        if math.isnan(xf) or math.isinf(xf):
            return dash
        return f"{xf:.{nd}f}"
    except (TypeError, ValueError):
        return dash

def _sparkline(values: list) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return blocks[0] * len(nums)
    return "".join(blocks[int((v - lo) / (hi - lo) * (len(blocks) - 1))] for v in nums)

def _ml_link(base_url: str, ticker: str, date: str) -> str:
    return f"{base_url.rstrip('/')}/alpha-hive-{ticker}-ml-enhanced-{date}.html"


# ── /scan <TICKER> ───────────────────────────────────────
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    if not ctx.args:
        await update.message.reply_text("用法：/scan <代码>，例如 /scan NVDA")
        return
    ticker = _norm_ticker(ctx.args[0])
    data = await fetch_dashboard(cfg.report_base_url)
    if not data:
        await update.message.reply_text("⚠️ 暂时拉取不到数据，请稍后再试。")
        return
    date = data.get("_date", "?")
    si = {x.get("ticker"): x for x in (data.get("search_index") or [])}
    if ticker not in si:
        avail = "、".join(sorted(k for k in si if k)) or "（无）"
        await update.message.reply_text(
            f"❌ <code>{_html.escape(ticker)}</code> 不在 {_html.escape(date)} 当日扫描范围内。\n"
            f"当日标的：{_html.escape(avail)}",
            parse_mode=ParseMode.HTML,
        )
        return

    rec = si[ticker]
    score = _fmt_num(rec.get("score"))
    direction = rec.get("direction", "—")
    dir_badge = DIR_BADGE.get(direction, f"⚪ {_html.escape(str(direction))}")
    price = _fmt_num(rec.get("price"), 2)

    radar = (data.get("radar") or {}).get(ticker) or []
    radar_line = ""
    if len(radar) >= 5:
        radar_line = " · ".join(f"{n}{int(round(float(v)))}" for n, v in zip(RADAR_DIMS, radar[:5]))

    sd = (data.get("swarm_divergence") or {}).get(ticker) or {}
    votes = sd.get("votes") or {}
    reso = "⚡ 共振" if sd.get("resonance") else "无共振"
    vote_line = f"🐝 蜂群投票：{votes.get('bullish', 0)}多 / {votes.get('bearish', 0)}空 / {votes.get('neutral', 0)}中 · {reso}"

    lines = [
        f"🐝 <b>{_html.escape(ticker)}</b> — {_html.escape(date)}",
        f"综合分 <b>{score}</b>/10 · {dir_badge} · 现价 ${price}",
    ]
    if radar_line:
        lines.append(f"📊 {radar_line}")
    if votes:
        lines.append(vote_line)
    lines.append(f'📄 <a href="{_ml_link(cfg.report_base_url, ticker, date)}">ML 增强报告 →</a>')
    lines.append("（综合分为系统研究输出，非买卖建议）")
    await update.message.reply_text("\n".join(lines) + _FOOT, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


# ── /top ─────────────────────────────────────────────────
async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    data = await fetch_dashboard(cfg.report_base_url)
    if not data:
        await update.message.reply_text("⚠️ 暂时拉取不到数据，请稍后再试。")
        return
    date = data.get("_date", "?")
    scores = data.get("scores") or []
    si = {x.get("ticker"): x for x in (data.get("search_index") or [])}
    sd = data.get("swarm_divergence") or {}
    limit = 99
    if ctx.args:
        try:
            limit = max(1, int(ctx.args[0]))
        except ValueError:
            pass

    dc = data.get("dir_counts") or [0, 0, 0]
    head = (
        f"🐝 <b>当日机会榜</b> — {_html.escape(date)}\n"
        f"方向分布：{dc[0]}多 / {dc[1]}空 / {dc[2]}中\n"
    )
    rows = []
    for i, item in enumerate(scores[:limit], 1):
        if not isinstance(item, list) or len(item) < 2:
            continue
        tk, sc = item[0], item[1]
        direction = (si.get(tk) or {}).get("direction", "")
        badge = DIR_BADGE.get(direction, "⚪")
        reso = "⚡" if (sd.get(tk) or {}).get("resonance") else "  "
        rows.append(f"{i}. <b>{_html.escape(str(tk))}</b>  {_fmt_num(sc)}  {badge} {reso}")
    body = "\n".join(rows) if rows else "（当日无数据）"
    tip = "\n\n点 /scan &lt;代码&gt; 看单只标的详情"
    await update.message.reply_text(head + body + tip + _FOOT, parse_mode=ParseMode.HTML)


# ── /swarm <TICKER> ──────────────────────────────────────
async def cmd_swarm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    if not ctx.args:
        await update.message.reply_text("用法：/swarm <代码>，例如 /swarm NVDA")
        return
    ticker = _norm_ticker(ctx.args[0])
    data = await fetch_dashboard(cfg.report_base_url)
    if not data:
        await update.message.reply_text("⚠️ 暂时拉取不到数据，请稍后再试。")
        return
    date = data.get("_date", "?")
    sd = (data.get("swarm_divergence") or {}).get(ticker)
    if not sd:
        si = {x.get("ticker"): x for x in (data.get("search_index") or [])}
        avail = "、".join(sorted(k for k in si if k)) or "（无）"
        await update.message.reply_text(
            f"❌ <code>{_html.escape(ticker)}</code> 不在 {_html.escape(date)} 当日扫描范围内。\n"
            f"当日标的：{_html.escape(avail)}",
            parse_mode=ParseMode.HTML,
        )
        return

    votes = sd.get("votes") or {}
    reso = "⚡ 共振（高一致）" if sd.get("resonance") else "无共振（存在分歧）"
    lines = [
        f"🐝 <b>{_html.escape(ticker)} 蜂群分歧透视</b> — {_html.escape(date)}",
        f"投票：{votes.get('bullish', 0)}多 / {votes.get('bearish', 0)}空 / {votes.get('neutral', 0)}中",
        f"共识度 {_fmt_num(sd.get('consensus'))}% · 分歧 std {_fmt_num(sd.get('std'), 2)} · spread {_fmt_num(sd.get('spread'), 1)}",
        f"状态：{reso}",
        "",
        "<b>逐蜂打分</b>：",
    ]
    for b in (sd.get("bees") or []):
        nm = BEE_CN.get(b.get("name"), b.get("name", "?"))
        d = BEE_DIR.get(b.get("dir"), "⚪")
        lines.append(f"  {d} {_html.escape(str(nm))}：{_fmt_num(b.get('score'))}")
    lines.append("\n（揭示综合分背后的内部分歧，供研究参考）")
    await update.message.reply_text("\n".join(lines) + _FOOT, parse_mode=ParseMode.HTML)


# ── /scorecard ───────────────────────────────────────────
async def cmd_scorecard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    data = await fetch_dashboard(cfg.report_base_url)
    if not data:
        await update.message.reply_text("⚠️ 暂时拉取不到数据，请稍后再试。")
        return
    ts = data.get("trading_stats") or {}
    weekly = data.get("acc_weekly") or []

    lines = ["🐝 <b>Alpha Hive 战绩记分牌</b>（系统历史回测，诚实公开对错）", ""]
    # 方向准确率
    labels = data.get("acc_dir_labels") or []
    accs = data.get("acc_dir_accs") or []
    tots = data.get("acc_dir_tots") or []
    if labels and accs:
        lines.append("<b>方向准确率</b>：")
        for lb, ac, to in zip(labels, accs, tots):
            lines.append(f"  {_html.escape(str(lb))}：{_fmt_num(ac)}%（{to} 次）")
        lines.append("")
    # 近 8 周
    if weekly:
        lines.append("<b>近期周度准确率</b>：")
        for w in weekly[-8:]:
            lines.append(
                f"  {_html.escape(str(w.get('week')))}：{_fmt_num(w.get('accuracy'))}% "
                f"（{w.get('total')} 次, 均收益 {_fmt_num(w.get('avg_ret'))}%）"
            )
        lines.append("")
    # 模拟组合
    lines.append("<b>$50K 模拟组合</b>：")
    lines.append(
        f"  净胜率 {_fmt_num(ts.get('net_win_rate'))}% · 夏普 {_fmt_num(ts.get('sharpe_net'), 2)} · "
        f"盈亏比 {_fmt_num(ts.get('profit_factor'), 2)}"
    )
    lines.append(
        f"  最大回撤 {_fmt_num(ts.get('max_dd_net_pct'))}% · vs SPY 超额 {_fmt_num(ts.get('alpha_vs_spy'))}%"
    )
    lines.append("\n（含亏损周与跑输大盘，不修饰；过往表现不预示未来）")
    await update.message.reply_text("\n".join(lines) + _FOOT, parse_mode=ParseMode.HTML)


# ── /fg ──────────────────────────────────────────────────
async def cmd_fg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    data = await fetch_dashboard(cfg.report_base_url)
    if not data:
        await update.message.reply_text("⚠️ 暂时拉取不到数据，请稍后再试。")
        return
    fv = data.get("fv")
    label = data.get("fg_label", "—")
    hist = data.get("fg_history") or []
    # 滤掉 None（远程 JSON 若缺 value，否则下方 min/max 抛 TypeError）
    recent = [h.get("value") for h in hist[-14:] if isinstance(h, dict) and h.get("value") is not None]
    spark = _sparkline(recent)
    lines = [
        f"🐝 <b>市场情绪 · 恐惧贪婪指数</b>",
        f"当前：<b>{_fmt_num(fv, 0)}</b> / 100 — {_html.escape(str(label))}",
    ]
    if spark:
        lines.append(f"近 {len(recent)} 日：{spark}")
        lines.append(f"区间 {_fmt_num(min(recent), 0)}–{_fmt_num(max(recent), 0)}")
    lines.append("\n（0=极度恐惧 100=极度贪婪，市场情绪研究指标）")
    await update.message.reply_text("\n".join(lines) + _FOOT, parse_mode=ParseMode.HTML)


# ── 注册 ─────────────────────────────────────────────────
def register(app: Application) -> None:
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("swarm", cmd_swarm))
    app.add_handler(CommandHandler("scorecard", cmd_scorecard))
    app.add_handler(CommandHandler("fg", cmd_fg))
