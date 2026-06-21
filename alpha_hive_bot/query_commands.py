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

# ── v0.31 付费分层 ───────────────────────────────────────
PRICE_MONTHLY = "¥128 / 月"
PRICE_YEARLY = "¥998 / 年（约 6.5 折）"
FREE_MAX_WATCH, PAID_MAX_WATCH = 3, 30
FREE_MAX_ALERTS, PAID_MAX_ALERTS = 1, 20

_UPGRADE_PROMPT = (
    "🔒 <b>此为 Pro 功能</b>\n\n"
    "升级 Pro 解锁：完整简报 + /scan 全维（雷达/蜂群/ML）+ /top 全榜 + "
    "/swarm 七蜂分歧 + /trend 走势 + /movers 变动榜 + 关注30/告警20。\n\n"
    f"价格：{PRICE_MONTHLY} · {PRICE_YEARLY}\n"
    "升级方式：发送 /upgrade 查看付款方式。\n\n"
    "（Pro 提供的是更广/更深/更主动的<b>研究数据访问</b>，不构成投资建议、不保证收益）"
)

def _effective_tier(ctx, user_id: int) -> str:
    """有效 tier。管理员视为 paid（最高权限）。"""
    cfg = ctx.application.bot_data["cfg"]
    db = ctx.application.bot_data["db"]
    if user_id in cfg.admin_user_ids:
        return "paid"
    return db.get_tier(user_id)

async def _require_paid(update: Update, ctx) -> bool:
    """Pro-only 命令守卫。非 paid → 发升级提示，返回 False。"""
    user = update.effective_user
    if not user:
        return False
    if _effective_tier(ctx, user.id) == "paid":
        return True
    await update.message.reply_text(_UPGRADE_PROMPT, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)
    return False

def _norm_ticker(arg: str) -> str:
    return arg.strip().lstrip("$").upper()

def _index_by_ticker(data) -> dict:
    """search_index → {ticker: row}，跳过非 dict 条目（远程 gh-pages JSON 无 schema 校验，
    坏元素会让 x.get 抛 AttributeError；与本文件 scores/fg_history 等字段的 isinstance 守卫一致）。"""
    return {x.get("ticker"): x for x in ((data or {}).get("search_index") or []) if isinstance(x, dict)}

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
    si = _index_by_ticker(data)
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
    # Pro 才显示 5维雷达 / 7蜂投票 / ML 报告链接；免费层给摘要 + 升级提示
    if _effective_tier(ctx, update.effective_user.id) == "paid":
        if radar_line:
            lines.append(f"📊 {radar_line}")
        if votes:
            lines.append(vote_line)
        lines.append(f'📄 <a href="{_ml_link(cfg.report_base_url, ticker, date)}">ML 增强报告 →</a>')
    else:
        lines.append("<i>🔒 升级 Pro 看 5维雷达 + 7蜂投票 + ML报告（/upgrade）</i>")
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
    si = _index_by_ticker(data)
    sd = data.get("swarm_divergence") or {}
    is_paid = _effective_tier(ctx, update.effective_user.id) == "paid"
    # 免费层固定前 3、不显示共振/方向分布；Pro 全榜 + 共振 + 方向分布
    if is_paid:
        limit = 99
        if ctx.args:
            try:
                limit = max(1, int(ctx.args[0]))
            except ValueError:
                pass
        dc = (list(data.get("dir_counts") or []) + [0, 0, 0])[:3]  # 容忍缺失/短数组
        head = (f"🐝 <b>当日机会榜</b> — {_html.escape(date)}\n"
                f"方向分布：{dc[0]}多 / {dc[1]}空 / {dc[2]}中\n")
    else:
        limit = 3
        head = f"🐝 <b>当日机会榜 Top 3</b> — {_html.escape(date)}\n"
    rows = []
    for i, item in enumerate(scores[:limit], 1):
        if not isinstance(item, list) or len(item) < 2:
            continue
        tk, sc = item[0], item[1]
        direction = (si.get(tk) or {}).get("direction", "")
        badge = DIR_BADGE.get(direction, "⚪")
        reso = (" ⚡" if (sd.get(tk) or {}).get("resonance") else "") if is_paid else ""
        rows.append(f"{i}. <b>{_html.escape(str(tk))}</b>  {_fmt_num(sc)}  {badge}{reso}")
    body = "\n".join(rows) if rows else "（当日无数据）"
    tip = "\n\n点 /scan &lt;代码&gt; 看单只标的详情"
    if not is_paid:
        tip += "\n<i>🔒 升级 Pro 看完整机会榜（全标的 + 共振 + 方向分布，/upgrade）</i>"
    await update.message.reply_text(head + body + tip + _FOOT, parse_mode=ParseMode.HTML)


# ── /swarm <TICKER> ──────────────────────────────────────
async def cmd_swarm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    if not await _require_paid(update, ctx):  # Pro-only
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
        si = _index_by_ticker(data)
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
    # vs SPY 超额：优先用 realistic 组合买入持有口径（portfolio_backtest，与 SPY buy-and-hold 同基准），
    # 而非净值曲线"每笔 $5K 累加重叠窗口"口径（方法偏弱、与基准不可比）。realistic 缺失时回退。
    _alpha_disp = (ts.get('realistic') or {}).get('alpha_vs_spy', ts.get('alpha_vs_spy'))
    lines.append(
        f"  最大回撤 {_fmt_num(ts.get('max_dd_net_pct'))}% · vs SPY 超额 {_fmt_num(_alpha_disp)}%"
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


_DIR_EN2CN = {"bullish": "🟢看多", "bearish": "🔴看空", "neutral": "⚪中性"}


# ── /trend <TICKER>（Pro）──────────────────────────────────
async def cmd_trend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    if not await _require_paid(update, ctx):
        return
    if not ctx.args:
        await update.message.reply_text("用法：/trend <代码>，例如 /trend NVDA")
        return
    ticker = _norm_ticker(ctx.args[0])
    data = await fetch_dashboard(cfg.report_base_url)
    if not data:
        await update.message.reply_text("⚠️ 暂时拉取不到数据，请稍后再试。")
        return
    series = (data.get("trend_data") or {}).get(ticker) or []
    pts = [(p.get("date"), p.get("score")) for p in series
           if isinstance(p, dict) and p.get("score") is not None]
    if len(pts) < 2:
        await update.message.reply_text(
            f"❌ <code>{_html.escape(ticker)}</code> 暂无足够趋势数据（需至少 2 个交易日）。",
            parse_mode=ParseMode.HTML)
        return
    vals = [v for _, v in pts]
    spark = _sparkline(vals)
    first, last = vals[0], vals[-1]
    chg = last - first
    arrow = "↑" if chg > 0 else ("↓" if chg < 0 else "→")
    lines = [
        f"📈 <b>{_html.escape(ticker)} 综合分趋势</b>（近 {len(pts)} 个交易日）",
        f"{spark}",
        f"起 {_fmt_num(first)} → 现 {_fmt_num(last)}  {arrow}{_fmt_num(abs(chg))}",
        f"区间 {_fmt_num(min(vals))}–{_fmt_num(max(vals))} · {_html.escape(str(pts[0][0]))}~{_html.escape(str(pts[-1][0]))}",
        "\n（系统综合分历史走势，研究参考，非买卖建议）",
    ]
    await update.message.reply_text("\n".join(lines) + _FOOT, parse_mode=ParseMode.HTML)


# ── /movers（Pro）── 昨日→今日 分数/方向变动榜 ──────────────
async def cmd_movers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    if not await _require_paid(update, ctx):
        return
    data = await fetch_dashboard(cfg.report_base_url)
    if not data:
        await update.message.reply_text("⚠️ 暂时拉取不到数据，请稍后再试。")
        return
    hf = data.get("hist_full") or {}
    dates = sorted(hf.keys())
    if len(dates) < 2:
        await update.message.reply_text("⚠️ 历史数据不足（需至少两个交易日），无法计算变动。")
        return
    d_prev, d_cur = dates[-2], dates[-1]
    def _mp(rows):
        return {r.get("ticker"): r for r in (rows or []) if isinstance(r, dict)}
    prev, cur = _mp(hf.get(d_prev)), _mp(hf.get(d_cur))
    deltas, flips = [], []
    for tk, c in cur.items():
        p = prev.get(tk)
        if not p:
            continue
        try:
            dlt = float(c.get("score")) - float(p.get("score"))
        except (TypeError, ValueError):
            continue
        deltas.append((tk, dlt, c.get("score")))
        if c.get("direction") != p.get("direction") and p.get("direction") and c.get("direction"):
            flips.append((tk, p.get("direction"), c.get("direction")))
    deltas.sort(key=lambda x: -x[1])
    lines = [f"📊 <b>变动榜</b> {_html.escape(d_prev)} → {_html.escape(d_cur)}"]
    ups = [d for d in deltas if d[1] > 0.05][:3]
    downs = [d for d in deltas if d[1] < -0.05][-3:]
    if ups:
        lines.append("\n📈 <b>涨幅</b>：" + " · ".join(
            f"{_html.escape(tk)} +{_fmt_num(dl)}" for tk, dl, _ in ups))
    if downs:
        lines.append("📉 <b>跌幅</b>：" + " · ".join(
            f"{_html.escape(tk)} {_fmt_num(dl)}" for tk, dl, _ in downs))
    if flips:
        lines.append("🔄 <b>方向翻转</b>：" + " · ".join(
            f"{_html.escape(tk)} {_DIR_EN2CN.get(a, a)}→{_DIR_EN2CN.get(b, b)}" for tk, a, b in flips[:5]))
    if not ups and not downs and not flips:
        lines.append("\n（今日较上一交易日无显著变动）")
    lines.append("\n（系统综合分日变动，研究参考，非买卖建议）")
    await update.message.reply_text("\n".join(lines) + _FOOT, parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════
# v0.3 个人关注列表 + 阈值告警
# ═══════════════════════════════════════════════════════════
import re as _re

_VALID_OPS = (">=", "<=", ">", "<")  # 注意顺序：先长后短，避免 >= 被 > 抢匹配
# 额度上限按 tier 取（见 _watch_cap / _alert_cap）；常量定义在文件上方付费分层段


def _watch_cap(ctx, user_id: int) -> int:
    return PAID_MAX_WATCH if _effective_tier(ctx, user_id) == "paid" else FREE_MAX_WATCH


def _alert_cap(ctx, user_id: int) -> int:
    return PAID_MAX_ALERTS if _effective_tier(ctx, user_id) == "paid" else FREE_MAX_ALERTS


def _eval_op(value: float, op: str, threshold: float) -> bool:
    if op == ">":  return value > threshold
    if op == "<":  return value < threshold
    if op == ">=": return value >= threshold
    if op == "<=": return value <= threshold
    return False


def _parse_alert_spec(args: list) -> Optional[tuple]:
    """解析 /alert 参数 → (ticker, metric, op, threshold)。失败返回 None。
    支持：/alert NVDA score>7 · /alert NVDA >7 · /alert NVDA score >= 7 · /alert nvda <4"""
    if not args:
        return None
    ticker = _norm_ticker(args[0])
    rest = "".join(args[1:]).lower().replace(" ", "")
    if not rest:
        return None
    m = _re.match(r"^(score)?(>=|<=|>|<)([0-9]+(?:\.[0-9]+)?)$", rest)
    if not m:
        return None
    metric = m.group(1) or "score"
    op = m.group(2)
    try:
        threshold = float(m.group(3))
    except ValueError:
        return None
    if metric != "score" or not (0 <= threshold <= 10):
        return None  # 目前只支持 score（0~10）
    return ticker, metric, op, threshold


def _scores_map(data: dict) -> dict:
    """从 dashboard-data.json 取 {ticker: score}。"""
    out = {}
    for item in (data.get("scores") or []):
        if isinstance(item, list) and len(item) >= 2:
            try:
                out[item[0]] = float(item[1])
            except (TypeError, ValueError):
                continue
    return out


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    if not ctx.args:
        await update.message.reply_text("用法：/watch <代码>，例如 /watch NVDA")
        return
    user = update.effective_user
    ticker = _norm_ticker(ctx.args[0])
    if not _re.match(r"^[A-Z0-9.\-]{1,12}$", ticker):
        await update.message.reply_text("代码格式不对（仅字母数字，≤12 位）")
        return
    cap = _watch_cap(ctx, user.id)
    if len(db.get_watch(user.id)) >= cap:
        extra = "" if _effective_tier(ctx, user.id) == "paid" else "（升级 Pro 可关注 30 只，/upgrade）"
        await update.message.reply_text(f"关注列表已满（上限 {cap}），先 /unwatch 一些。{extra}")
        return
    added = db.add_watch(user.id, ticker)
    await update.message.reply_text(
        f"{'✓ 已关注' if added else 'ℹ️ 已在关注列表'}: <code>{_html.escape(ticker)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    if not ctx.args:
        await update.message.reply_text("用法：/unwatch <代码>")
        return
    user = update.effective_user
    ticker = _norm_ticker(ctx.args[0])
    ok = db.remove_watch(user.id, ticker)
    await update.message.reply_text(
        f"{'✓ 已移除' if ok else 'ℹ️ 不在关注列表'}: <code>{_html.escape(ticker)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_mywatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    user = update.effective_user
    watch = db.get_watch(user.id)
    if not watch:
        await update.message.reply_text("你的关注列表为空。用 /watch <代码> 添加。")
        return
    data = await fetch_dashboard(cfg.report_base_url)
    sc = _scores_map(data) if data else {}
    si = _index_by_ticker(data)
    date = data.get("_date", "") if data else ""
    lines = [f"📌 <b>你的关注列表</b>（{len(watch)}）" + (f" — {_html.escape(date)}" if date else "")]
    for tk in watch:
        if tk in sc:
            d = (si.get(tk) or {}).get("direction", "")
            badge = DIR_BADGE.get(d, "")
            lines.append(f"  <b>{_html.escape(tk)}</b>  {_fmt_num(sc[tk])}  {badge}")
        else:
            lines.append(f"  <b>{_html.escape(tk)}</b>  <i>（未在当日扫描范围）</i>")
    lines.append("\n/scan &lt;代码&gt; 看详情 · /unwatch &lt;代码&gt; 移除")
    await update.message.reply_text("\n".join(lines) + _FOOT, parse_mode=ParseMode.HTML)


async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    user = update.effective_user
    parsed = _parse_alert_spec(ctx.args)
    if not parsed:
        await update.message.reply_text(
            "用法：/alert &lt;代码&gt; score&gt;7（或 &lt;4 / &gt;=6 等）\n"
            "例：/alert NVDA score&gt;7 · /alert TSLA &lt;4\n"
            "（目前支持 score 综合分 0~10；触发条件满足时主动推送）",
            parse_mode=ParseMode.HTML,
        )
        return
    ticker, metric, op, threshold = parsed
    cap = _alert_cap(ctx, user.id)
    if len(db.get_alerts(user.id)) >= cap:
        extra = "" if _effective_tier(ctx, user.id) == "paid" else "\n🔒 升级 Pro 可设 20 条告警（/upgrade）"
        await update.message.reply_text(
            f"告警规则已满（上限 {cap}），先 /alerts 查看 + /unalert 删除。{extra}")
        return
    added = db.add_alert(user.id, ticker, metric, op, threshold)
    if added:
        await update.message.reply_text(
            f"✓ 已订阅告警：<code>{_html.escape(ticker)}</code> {metric} {_html.escape(op)} {_fmt_num(threshold)}\n"
            "每日新数据后命中即推送（研究信号，非买卖建议）。\n"
            "⏰ 注意：每日盘后评估一次，非盘中实时。",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("ℹ️ 该规则已存在。/alerts 查看全部。")


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    user = update.effective_user
    rules = db.get_alerts(user.id)
    if not rules:
        await update.message.reply_text("你没有告警规则。用 /alert <代码> score>7 添加。")
        return
    lines = ["🔔 <b>你的告警规则</b>："]
    for r in rules:
        lines.append(
            f"  <code>#{r['id']}</code> {_html.escape(r['ticker'])} "
            f"{r['metric']} {_html.escape(r['op'])} {_fmt_num(r['threshold'])}"
        )
    lines.append("\n/unalert &lt;编号&gt; 删除（如 /unalert 3）")
    await update.message.reply_text("\n".join(lines) + _FOOT, parse_mode=ParseMode.HTML)


async def cmd_unalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    user = update.effective_user
    if not ctx.args:
        await update.message.reply_text("用法：/unalert <编号>（编号见 /alerts）")
        return
    try:
        rid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("编号必须是整数（见 /alerts）")
        return
    ok = db.remove_alert(user.id, rid)
    await update.message.reply_text(f"{'✓ 已删除告警 #' + str(rid) if ok else '❌ 未找到该编号的规则'}")


async def evaluate_alerts(bot, cfg: BotConfig, db: SubscriberDB) -> dict:
    """每日推送后评估告警规则，边沿触发（false→true 才推），返回 {pushed, checked}。"""
    from telegram.constants import ParseMode as _PM
    from telegram.error import TelegramError as _TE

    data = await fetch_dashboard(cfg.report_base_url)
    if not data:
        log.warning("evaluate_alerts: dashboard 不可用，跳过")
        return {"pushed": 0, "checked": 0, "skipped": True}
    sc = _scores_map(data)
    date = data.get("_date", "?")
    rules = db.list_active_alerts()
    # v0.31: 按当前有效 tier 限制每用户评估的告警条数（最早创建的优先）。
    # 防止 Pro 过期后仍永久享受 20 条告警容量——降级后只评估免费额度内的规则。
    from collections import defaultdict
    by_user = defaultdict(list)
    for r in rules:
        by_user[r["user_id"]].append(r)
    eligible_ids = set()
    for uid, urules in by_user.items():
        is_paid = uid in cfg.admin_user_ids or db.get_tier(uid) == "paid"
        cap = PAID_MAX_ALERTS if is_paid else FREE_MAX_ALERTS
        for r in sorted(urules, key=lambda x: x["id"])[:cap]:
            eligible_ids.add(r["id"])
    pushed = 0
    checked = 0
    for r in rules:
        if r["id"] not in eligible_ids:
            continue  # 超出当前 tier 额度的规则（如 Pro 过期），本轮不评估
        checked += 1
        val = sc.get(r["ticker"])
        if val is None:
            continue  # 该标的当日未扫描，不评估（不重置状态）
        new_state = 1 if _eval_op(val, r["op"], float(r["threshold"])) else 0
        if new_state and not r["last_state"]:
            # 边沿：刚满足 → 推送。仅在推送成功后才写 last_state=1（消费边沿），
            # 否则 TelegramError 时保持 last_state=0，下轮重试推送 + 行为试用（不静默错失）。
            try:
                await bot.send_message(
                    chat_id=r["chat_id"],
                    text=(f"🔔 <b>告警</b> — {_html.escape(date)}\n"
                          f"<b>{_html.escape(r['ticker'])}</b> {r['metric']} = {_fmt_num(val)} "
                          f"已突破你的阈值 {r['metric']} {_html.escape(r['op'])} {_fmt_num(float(r['threshold']))}\n"
                          f"（研究信号，非买卖建议）{_FOOT}"),
                    parse_mode=_PM.HTML,
                )
                pushed += 1
                # 行为锚定 trial：免费用户告警命中 → 解锁 7 天 Pro 体验（每人一次）
                # 仅限"从未有过任何 Pro 窗口"的纯免费用户：expires 为 None（real /grant
                # 与历史 trial 都会写入 expires）+ trial_used=0，避免流失付费用户/已用过试用者再领。
                uid = r["user_id"]
                _ti = db.get_tier_info(uid)
                if (uid not in cfg.admin_user_ids and _ti["effective"] != "paid"
                        and not _ti["trial_used"] and _ti["expires"] is None):
                    from datetime import datetime as _dt, timedelta as _td
                    exp = (_dt.utcnow() + _td(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                    db.set_tier(uid, "paid", exp)
                    db.mark_trial_used(uid)
                    try:
                        await bot.send_message(
                            chat_id=r["chat_id"],
                            text=("🎁 <b>你的告警命中了！</b>\n已为你解锁 <b>7 天 Pro 全功能体验</b>"
                                  "（完整简报 + /scan 全维 + /swarm + /trend + /movers + 关注30/告警20）。\n"
                                  "无需付款，到期自动回到免费层。/mytier 查看剩余天数。"),
                            parse_mode=_PM.HTML,
                        )
                    except _TE:
                        pass  # 试用通知失败不影响主告警已成功送达
                db.set_alert_state(r["id"], 1)  # 推送成功，消费边沿
            except _TE as e:
                log.warning("alert push 失败 chat=%s: %s（保持 last_state=0 下轮重试）", r.get("chat_id"), e)
        elif new_state != r["last_state"]:
            # true→false 复位（无需推送）：总是写库，使下次再满足能再次触发
            db.set_alert_state(r["id"], new_state)
    log.info("evaluate_alerts: checked=%d pushed=%d (rules=%d)", checked, pushed, len(rules))
    return {"pushed": pushed, "checked": checked, "skipped": False}


# ── v0.31 付费命令 ───────────────────────────────────────
async def cmd_upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    user = update.effective_user
    if _effective_tier(ctx, user.id) == "paid":
        info = db.get_tier_info(user.id)
        exp = info.get("expires") or "—"
        await update.message.reply_text(
            f"✅ 你已是 <b>Pro</b>，到期：{_html.escape(str(exp))} (UTC)。/mytier 查看。",
            parse_mode=ParseMode.HTML)
        return
    msg = (
        "💎 <b>升级 Alpha Hive Pro</b>\n\n"
        "Pro 解锁：\n"
        "· 完整每日简报（免费为截断版）\n"
        "· /scan 全维（5维雷达 + 7蜂投票 + ML报告）\n"
        "· /top 全榜 + 共振 + 方向分布\n"
        "· /swarm 七蜂分歧 · /trend 走势 · /movers 变动榜\n"
        "· 关注 30 只 / 告警 20 条\n\n"
        f"💰 价格：<b>{PRICE_MONTHLY}</b> · <b>{PRICE_YEARLY}</b>\n"
        "💳 付款：支付宝（联系管理员）。付款后把你的 user_id 发给管理员开通：\n"
        f"你的 user_id：<code>{user.id}</code>\n\n"
        "（Pro 提供更广/更深/更主动的<b>研究数据访问</b>，"
        "不构成投资建议、不保证收益）"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    # 通知管理员有人想升级
    uname = f"@{user.username}" if user.username else "(无用户名)"
    for admin_id in cfg.admin_user_ids:
        ac = None
        try:
            with db._conn() as c:
                row = c.execute("SELECT chat_id FROM subscribers WHERE user_id=?", (admin_id,)).fetchone()
                ac = row["chat_id"] if row else None
        except Exception:
            ac = None
        if ac:
            try:
                await ctx.application.bot.send_message(
                    chat_id=ac,
                    text=(f"💰 升级请求：<code>{user.id}</code> {_html.escape(uname)} 想升级 Pro。\n"
                          f"收款后用 /grant {user.id} &lt;月数&gt; 开通。"),
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass


async def cmd_mytier(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db: SubscriberDB = ctx.application.bot_data["db"]
    if not await _gate(update, db):
        return
    user = update.effective_user
    eff = _effective_tier(ctx, user.id)
    info = db.get_tier_info(user.id)
    if user.id in ctx.application.bot_data["cfg"].admin_user_ids:
        await update.message.reply_text("👑 管理员（等效 Pro，无限期）")
        return
    if eff == "paid":
        exp = info.get("expires") or "—"
        await update.message.reply_text(
            f"💎 当前：<b>Pro</b>\n到期：{_html.escape(str(exp))} (UTC)\n续费发 /upgrade",
            parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(
            "🆓 当前：<b>免费层</b>\n升级 Pro 发 /upgrade", parse_mode=ParseMode.HTML)


async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """管理员手动开通 Pro：/grant <user_id> <月数>。"""
    cfg: BotConfig = ctx.application.bot_data["cfg"]
    db: SubscriberDB = ctx.application.bot_data["db"]
    user = update.effective_user
    if not user or user.id not in cfg.admin_user_ids:
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("用法：/grant <user_id> <月数>，例如 /grant 12345678 1")
        return
    try:
        target = int(ctx.args[0]); months = int(ctx.args[1])
        if months <= 0 or months > 60:
            raise ValueError
    except ValueError:
        await update.message.reply_text("参数错误：user_id 整数 + 月数 1~60")
        return
    if db.get_status(target) is None:
        # set_tier 是 UPDATE，对不存在的行无效果 → 先建白名单行再开通
        await update.message.reply_text(
            f"⚠️ user_id {target} 不在订阅库，已自动加白名单（对方仍需 /start 才能收推送）。")
        db.add_whitelist(target)
    from datetime import datetime as _dt, timedelta as _td
    exp = (_dt.utcnow() + _td(days=30 * months)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_tier(target, "paid", exp)
    await update.message.reply_text(f"✓ 已开通 Pro：user_id={target}，{months} 个月，到期 {exp} (UTC)")
    # 通知被开通用户
    try:
        with db._conn() as c:
            row = c.execute("SELECT chat_id FROM subscribers WHERE user_id=?", (target,)).fetchone()
        if row and row["chat_id"]:
            await ctx.application.bot.send_message(
                chat_id=row["chat_id"],
                text=f"🎉 你的 Alpha Hive <b>Pro</b> 已开通，有效期 {months} 个月！/mytier 查看。",
                parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ── 注册 ─────────────────────────────────────────────────
def register(app: Application) -> None:
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("swarm", cmd_swarm))
    app.add_handler(CommandHandler("scorecard", cmd_scorecard))
    app.add_handler(CommandHandler("fg", cmd_fg))
    app.add_handler(CommandHandler("trend", cmd_trend))      # Pro
    app.add_handler(CommandHandler("movers", cmd_movers))    # Pro
    # v0.3 关注列表 + 告警
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("mywatch", cmd_mywatch))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("unalert", cmd_unalert))
    # v0.31 付费
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("mytier", cmd_mytier))
    app.add_handler(CommandHandler("grant", cmd_grant))
