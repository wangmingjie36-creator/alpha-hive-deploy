"""
market_intelligence.py — Alpha Hive 市场智能扩展模块
=======================================================
集中实现六项高价值分析框架：
  ③ 时间周期意识      get_cycle_context()
  ④ 市场政体识别      detect_market_regime()
  ⑤ Gamma到期日历    calculate_gamma_expiry_calendar()
  ⑥ 供应链相对强弱    get_supply_chain_signals()
  ⑦ 信号拥挤度指数    calculate_signal_crowding()
  ⑧ 论点失效检测      check_thesis_breaks()

所有函数均静默降级：数据不可用时返回带 "unavailable" 标记的空结构，
绝不抛出异常影响主流程。
"""

from __future__ import annotations
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BASE = Path(__file__).parent

# ─────────────────────────────────────────────────────────────────────────────
# ③ 时间周期意识 — get_cycle_context()
# ─────────────────────────────────────────────────────────────────────────────

def _third_friday(year: int, month: int) -> date:
    """返回给定年月的第三个周五（美国月度期权到期日）。"""
    d = date(year, month, 1)
    # 找到第一个周五
    days_to_fri = (4 - d.weekday()) % 7
    first_fri = d + timedelta(days=days_to_fri)
    return first_fri + timedelta(weeks=2)


def get_cycle_context(
    today: Optional[date] = None,
    earnings_date: Optional[date] = None,
    fomc_dates: Optional[List[date]] = None,
) -> Dict[str, Any]:
    """
    返回当前交易日所处的时间周期上下文。

    Parameters
    ----------
    today         : 基准日期，默认 date.today()
    earnings_date : 最近一次财报日期（已发布）
    fomc_dates    : FOMC 会议日期列表（未来）

    Returns
    -------
    {
      "is_opex_week"        : bool,     # 本周是否为月度期权到期周（第三周五）
      "days_to_opex"        : int,      # 距本月 Opex 的自然日
      "is_weekly_opex_day"  : bool,     # 今天是否为每周五期权到期日
      "post_earnings_days"  : int|None, # 距最近财报已过天数（None 表示未知）
      "is_post_earnings_window": bool,  # 是否处于财报后 10 天漂移窗口
      "days_to_fomc"        : int|None, # 距下次 FOMC 的自然日（None 表示未知）
      "is_fomc_week"        : bool,     # 本周是否含 FOMC 会议
      "is_month_end_window" : bool,     # 是否处于月末再平衡窗口（最后 3 个交易日）
      "cycle_regime"        : str,      # "opex_pin" | "post_earnings" | "fomc_risk"
                                        #   | "month_end" | "normal"
      "cycle_description"   : str,      # 供 LLM 引用的自然语言描述（1-2句）
    }
    """
    today = today or date.today()
    result: Dict[str, Any] = {}

    # ── Opex 分析 ────────────────────────────────────────────────────────────
    this_opex = _third_friday(today.year, today.month)
    days_to_opex = (this_opex - today).days
    if days_to_opex < 0:
        # 本月 opex 已过，取下月
        m2 = today.month % 12 + 1
        y2 = today.year + (1 if today.month == 12 else 0)
        this_opex = _third_friday(y2, m2)
        days_to_opex = (this_opex - today).days

    is_opex_week = days_to_opex <= 5
    is_weekly_opex_day = today.weekday() == 4  # 每周五
    result["days_to_opex"] = days_to_opex
    result["is_opex_week"] = is_opex_week
    result["is_weekly_opex_day"] = is_weekly_opex_day

    # ── 财报后漂移窗口 ────────────────────────────────────────────────────────
    post_earnings_days: Optional[int] = None
    is_post_earnings_window = False
    if earnings_date:
        post_earnings_days = (today - earnings_date).days
        is_post_earnings_window = 0 <= post_earnings_days <= 10
    result["post_earnings_days"] = post_earnings_days
    result["is_post_earnings_window"] = is_post_earnings_window

    # ── FOMC 分析 ─────────────────────────────────────────────────────────────
    days_to_fomc: Optional[int] = None
    is_fomc_week = False
    if fomc_dates:
        upcoming = [d for d in fomc_dates if d >= today]
        if upcoming:
            next_fomc = min(upcoming)
            days_to_fomc = (next_fomc - today).days
            is_fomc_week = days_to_fomc <= 5
    result["days_to_fomc"] = days_to_fomc
    result["is_fomc_week"] = is_fomc_week

    # ── 月末再平衡窗口（最后 3 个自然日）────────────────────────────────────
    import calendar
    last_day = calendar.monthrange(today.year, today.month)[1]
    days_to_month_end = last_day - today.day
    is_month_end_window = days_to_month_end <= 2
    result["is_month_end_window"] = is_month_end_window

    # ── 综合政体 ──────────────────────────────────────────────────────────────
    if is_opex_week:
        cycle_regime = "opex_pin"
        desc = (f"本周为月度期权到期周（Opex {this_opex}，距今 {days_to_opex} 天），"
                "做市商 Delta/Gamma 对冲产生 Pin 效应，价格倾向于被高 OI 行权价吸引；"
                "大方向行情通常在 Opex 后次周重启。")
    elif is_post_earnings_window and post_earnings_days is not None:
        cycle_regime = "post_earnings"
        desc = (f"当前处于财报后第 {post_earnings_days} 天，属于 PEAD（财报后价格漂移）窗口；"
                "机构仓位重置和 IV Crush 效应主导短期走势，而非新增信息驱动。")
    elif is_fomc_week:
        cycle_regime = "fomc_risk"
        desc = (f"本周含 FOMC 会议（距今 {days_to_fomc} 天），近端 VIX 溢价通常在会前 2-3 天"
                "达峰然后快速均值回归；会议后 24h 内波动率会压缩。")
    elif is_month_end_window:
        cycle_regime = "month_end"
        desc = ("当前处于月末 3 日再平衡窗口；机构被动再平衡产生与估值无关的单向流动，"
                "流动性较月中偏薄，价格行为可能失真。")
    else:
        cycle_regime = "normal"
        desc = (f"当前处于普通交易窗口（距月度 Opex {days_to_opex} 天"
                + (f"，距 FOMC {days_to_fomc} 天" if days_to_fomc else "") + "），"
                "无明显周期性扭曲，信号解读可按常规权重处理。")

    result["cycle_regime"] = cycle_regime
    result["cycle_description"] = desc
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ④ 市场政体识别 — detect_market_regime()
# ─────────────────────────────────────────────────────────────────────────────

def detect_market_regime(ticker: str = "NVDA") -> Dict[str, Any]:
    """
    三层政体识别：宏观 → 板块 → 个股。

    Returns
    -------
    {
      "macro_regime"  : "bull"|"bear"|"neutral",   # SPX vs 200MA
      "macro_detail"  : str,
      "sector_regime" : "bull"|"bear"|"neutral",   # SOXX（SOX ETF）20MA趋势
      "sector_detail" : str,
      "stock_regime"  : "bull"|"bear"|"neutral",   # 个股 20MA vs 50MA
      "stock_detail"  : str,
      "overall_regime": "risk_on"|"risk_off"|"mixed",
      "regime_score_adj": float,     # 对蜂群最终评分的建议调整（-0.5~+0.5）
      "regime_description": str,     # 供 LLM 引用的 1-2 句描述
    }
    """
    _empty = {
        "macro_regime": "neutral", "macro_detail": "数据不可用",
        "sector_regime": "neutral", "sector_detail": "数据不可用",
        "stock_regime": "neutral", "stock_detail": "数据不可用",
        "overall_regime": "mixed", "regime_score_adj": 0.0,
        "regime_description": "政体识别数据不可用，信号按中性权重处理。",
        "source": "unavailable",
    }
    try:
        import yfinance as yf
        import numpy as np

        def _get_ma(sym: str, period: int = 200, window: int = max(200, 60)) -> Tuple[float, float]:
            """返回 (最新收盘价, N日均线) 或 (nan, nan)。"""
            hist = yf.download(sym, period=f"{window+20}d", interval="1d",
                               progress=False, auto_adjust=True)
            if hist.empty or len(hist) < period:
                return float("nan"), float("nan")
            closes = hist["Close"].dropna().values.flatten()
            ma = float(np.mean(closes[-period:]))
            return float(closes[-1]), ma

        # ── 宏观：SPX vs 200MA ───────────────────────────────────────────────
        spx_price, spx_ma200 = _get_ma("^GSPC", 200, 220)
        if math.isnan(spx_price):
            macro_regime, macro_detail = "neutral", "SPX 数据不可用"
        elif spx_price > spx_ma200 * 1.01:
            macro_regime = "bull"
            pct = (spx_price / spx_ma200 - 1) * 100
            macro_detail = f"SPX ${spx_price:.0f} 高于 200MA ${spx_ma200:.0f} (+{pct:.1f}%)，宏观处于牛市政体"
        elif spx_price < spx_ma200 * 0.99:
            macro_regime = "bear"
            pct = (spx_price / spx_ma200 - 1) * 100
            macro_detail = f"SPX ${spx_price:.0f} 低于 200MA ${spx_ma200:.0f} ({pct:.1f}%)，宏观处于熊市政体"
        else:
            macro_regime = "neutral"
            macro_detail = f"SPX ${spx_price:.0f} 在 200MA ${spx_ma200:.0f} 附近震荡，宏观政体中性"

        # ── 板块：SOXX 20MA 趋势 ─────────────────────────────────────────────
        soxx_price, soxx_ma20 = _get_ma("SOXX", 20, 40)
        if math.isnan(soxx_price):
            sector_regime, sector_detail = "neutral", "SOXX 数据不可用"
        elif soxx_price > soxx_ma20 * 1.01:
            sector_regime = "bull"
            sector_detail = f"SOXX ${soxx_price:.0f} 站上 20MA ${soxx_ma20:.0f}，半导体板块趋势向上"
        elif soxx_price < soxx_ma20 * 0.99:
            sector_regime = "bear"
            sector_detail = f"SOXX ${soxx_price:.0f} 跌破 20MA ${soxx_ma20:.0f}，半导体板块趋势向下"
        else:
            sector_regime = "neutral"
            sector_detail = f"SOXX 在 20MA 附近震荡（${soxx_price:.0f} vs ${soxx_ma20:.0f}），板块政体中性"

        # ── 个股：20MA vs 50MA 金叉/死叉 ────────────────────────────────────
        stk_price, stk_ma50 = _get_ma(ticker, 50, 70)
        _, stk_ma20 = _get_ma(ticker, 20, 40)
        if math.isnan(stk_price) or math.isnan(stk_ma20):
            stock_regime, stock_detail = "neutral", f"{ticker} 个股政体数据不可用"
        elif stk_ma20 > stk_ma50 * 1.005:
            stock_regime = "bull"
            stock_detail = (f"{ticker} 20MA ${stk_ma20:.0f} 站上 50MA ${stk_ma50:.0f}（金叉），"
                            f"短期动量向上，趋势偏多")
        elif stk_ma20 < stk_ma50 * 0.995:
            stock_regime = "bear"
            stock_detail = (f"{ticker} 20MA ${stk_ma20:.0f} 跌破 50MA ${stk_ma50:.0f}（死叉），"
                            f"短期动量向下，趋势偏空")
        else:
            stock_regime = "neutral"
            stock_detail = (f"{ticker} 20MA(${stk_ma20:.0f}) ≈ 50MA(${stk_ma50:.0f})，"
                            f"个股政体中性，方向待定")

        # ── 综合政体 ─────────────────────────────────────────────────────────
        bull_count = [macro_regime, sector_regime, stock_regime].count("bull")
        bear_count = [macro_regime, sector_regime, stock_regime].count("bear")
        if bull_count >= 2:
            overall = "risk_on"
            adj = +0.3
        elif bear_count >= 2:
            overall = "risk_off"
            adj = -0.3
        else:
            overall = "mixed"
            adj = 0.0

        desc_parts = [macro_detail, sector_detail, stock_detail]
        regime_desc = (
            f"三层政体检测：宏观{'↑' if macro_regime=='bull' else '↓' if macro_regime=='bear' else '→'}"
            f" · 板块{'↑' if sector_regime=='bull' else '↓' if sector_regime=='bear' else '→'}"
            f" · 个股{'↑' if stock_regime=='bull' else '↓' if stock_regime=='bear' else '→'}，"
            f"综合判定为「{overall}」政体，蜂群评分基础调整 {adj:+.1f}。"
        )

        return {
            "macro_regime": macro_regime, "macro_detail": macro_detail,
            "sector_regime": sector_regime, "sector_detail": sector_detail,
            "stock_regime": stock_regime, "stock_detail": stock_detail,
            "overall_regime": overall,
            "regime_score_adj": adj,
            "regime_description": regime_desc,
            "source": "yfinance",
        }

    except Exception as e:
        _empty["regime_description"] = f"政体识别失败（{e}），按中性处理。"
        return _empty


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ Gamma 到期日历 — calculate_gamma_expiry_calendar()
# ─────────────────────────────────────────────────────────────────────────────

def calculate_gamma_expiry_calendar(
    calls: List[Dict], puts: List[Dict], stock_price: float
) -> Dict[str, Any]:
    """
    按到期日聚合 OI，识别 Pin Risk 到期日与 Charm 压力方向。

    Parameters
    ----------
    calls / puts : 期权链列表，每项含 expiration, strike, openInterest, gamma
    stock_price  : 当前股价

    Returns
    -------
    {
      "expiry_oi": [{"expiry": str, "call_oi": int, "put_oi": int, "total_oi": int,
                     "pc_ratio": float, "net_gamma": float}, ...],  # 按 OI 降序
      "pin_expiry": str|None,          # 最大 OI 到期日（最可能出现 Pin）
      "pin_strike": float|None,        # 该到期日最高 OI 行权价（Pin 目标价）
      "charm_direction": "bullish"|"bearish"|"neutral",  # Charm 流方向
      "charm_note": str,               # 自然语言说明
      "days_to_pin": int|None,         # 距 Pin 到期日天数
    }
    """
    _empty = {
        "expiry_oi": [], "pin_expiry": None, "pin_strike": None,
        "charm_direction": "neutral", "charm_note": "数据不足，Gamma 日历不可用",
        "days_to_pin": None,
    }
    if not calls and not puts:
        return _empty
    try:
        from collections import defaultdict
        today = date.today()

        # 聚合 OI by expiry
        expiry_data: Dict[str, Dict] = defaultdict(
            lambda: {"call_oi": 0, "put_oi": 0, "net_gamma": 0.0,
                     "call_oi_by_strike": defaultdict(int),
                     "put_oi_by_strike": defaultdict(int)}
        )

        for c in calls:
            exp = str(c.get("expiry") or c.get("expiration") or "")
            oi = int(c.get("openInterest", 0) or 0)
            strike = float(c.get("strike", 0) or 0)
            gam = float(c.get("gamma", 0) or 0)
            if not exp:
                continue
            expiry_data[exp]["call_oi"] += oi
            expiry_data[exp]["net_gamma"] += gam * oi * 100
            expiry_data[exp]["call_oi_by_strike"][strike] += oi

        for p in puts:
            exp = str(p.get("expiry") or p.get("expiration") or "")
            oi = int(p.get("openInterest", 0) or 0)
            strike = float(p.get("strike", 0) or 0)
            gam = float(p.get("gamma", 0) or 0)
            if not exp:
                continue
            expiry_data[exp]["put_oi"] += oi
            expiry_data[exp]["net_gamma"] -= gam * oi * 100
            expiry_data[exp]["put_oi_by_strike"][strike] += oi

        # 构建排序列表（仅未来到期日）
        rows = []
        for exp, d in expiry_data.items():
            try:
                exp_date = date.fromisoformat(exp)
            except ValueError:
                continue
            if exp_date < today:
                continue
            total = d["call_oi"] + d["put_oi"]
            pc = d["put_oi"] / d["call_oi"] if d["call_oi"] > 0 else 1.0
            rows.append({
                "expiry": exp,
                "call_oi": d["call_oi"],
                "put_oi": d["put_oi"],
                "total_oi": total,
                "pc_ratio": round(pc, 3),
                "net_gamma": round(d["net_gamma"], 2),
                "days_to": (exp_date - today).days,
                "_c_by_strike": dict(d["call_oi_by_strike"]),
                "_p_by_strike": dict(d["put_oi_by_strike"]),
            })
        rows.sort(key=lambda x: x["total_oi"], reverse=True)

        if not rows:
            return _empty

        # Pin Risk 到期日 = OI 最大的未来到期日
        pin_row = rows[0]
        pin_expiry = pin_row["expiry"]
        days_to_pin = pin_row["days_to"]

        # Pin Strike = 最高总 OI 行权价（call + put OI 合并）
        combined: Dict[float, int] = defaultdict(int)
        for s, o in pin_row.get("_c_by_strike", {}).items():
            combined[s] += o
        for s, o in pin_row.get("_p_by_strike", {}).items():
            combined[s] += o
        pin_strike = max(combined, key=combined.get) if combined else None

        # Charm 方向：net_gamma 正 = call 主导 → 价格上涨时做市商买入（多头 Charm）
        net_g = pin_row["net_gamma"]
        if net_g > 0:
            charm_dir = "bullish"
            charm_note = (f"到期日 {pin_expiry} net_gamma={net_g:+.0f}（Call 主导），"
                          f"随时间衰减做市商 Delta 对冲方向偏多，价格受到上行引力。")
        elif net_g < 0:
            charm_dir = "bearish"
            charm_note = (f"到期日 {pin_expiry} net_gamma={net_g:+.0f}（Put 主导），"
                          f"随时间衰减做市商 Delta 对冲方向偏空，价格受到下行引力。")
        else:
            charm_dir = "neutral"
            charm_note = f"到期日 {pin_expiry} 多空 Gamma 接近均衡，Charm 方向中性。"

        if pin_strike:
            charm_note += f" Pin 目标价 ${pin_strike:.0f}（OI最高行权价），距当前 ${stock_price:.0f} 约 {abs(pin_strike-stock_price)/stock_price*100:.1f}%。"

        # 清理内部字段
        clean_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:5]]

        return {
            "expiry_oi": clean_rows,
            "pin_expiry": pin_expiry,
            "pin_strike": pin_strike,
            "charm_direction": charm_dir,
            "charm_note": charm_note,
            "days_to_pin": days_to_pin,
        }
    except Exception as e:
        _empty["charm_note"] = f"Gamma 日历计算失败：{e}"
        return _empty


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ 供应链相对强弱 — get_supply_chain_signals()
# ─────────────────────────────────────────────────────────────────────────────

_SUPPLY_CHAIN = {
    "TSM":  "台积电（代工，CoWoS 供应）",
    "AMAT": "应用材料（半导体设备）",
    "ASML": "ASML（光刻机，CoWoS 关键）",
    "SOXX": "SOX 半导体ETF（板块基准）",
}


def get_supply_chain_signals(ticker: str = "NVDA", lookback_days: int = 5) -> Dict[str, Any]:
    """
    计算 ticker 与半导体供应链标的的短期相对强弱。

    Returns
    -------
    {
      "peers": [{"symbol": str, "name": str, "return_5d_pct": float,
                 "vs_target_pct": float, "lead_lag": "leading"|"lagging"|"inline"}],
      "supply_chain_signal": "positive"|"negative"|"neutral",  # 供应链整体信号
      "supply_chain_note"  : str,   # 自然语言摘要（供 LLM 引用）
      "source"             : str,
    }
    """
    _empty = {
        "peers": [], "supply_chain_signal": "neutral",
        "supply_chain_note": "供应链数据不可用",
        "source": "unavailable",
    }
    try:
        import yfinance as yf
        period_str = f"{lookback_days + 5}d"
        syms = [ticker] + list(_SUPPLY_CHAIN.keys())
        returns: Dict[str, float] = {}

        for sym in syms:
            try:
                hist = yf.download(sym, period=period_str, interval="1d",
                                   progress=False, auto_adjust=True)
                if hist.empty or len(hist) < 2:
                    continue
                closes = hist["Close"].dropna().values.flatten()
                n = min(lookback_days, len(closes) - 1)
                ret = (float(closes[-1]) / float(closes[-n - 1]) - 1) * 100
                returns[sym] = round(ret, 2)
            except Exception:
                continue

        if ticker not in returns or len(returns) < 2:
            return _empty

        target_ret = returns[ticker]
        peers = []
        for sym, name in _SUPPLY_CHAIN.items():
            if sym not in returns:
                continue
            peer_ret = returns[sym]
            vs = round(peer_ret - target_ret, 2)
            lead_lag = "leading" if vs > 1.5 else ("lagging" if vs < -1.5 else "inline")
            peers.append({
                "symbol": sym, "name": name,
                "return_5d_pct": peer_ret,
                "vs_target_pct": vs,
                "lead_lag": lead_lag,
            })

        leading = [p for p in peers if p["lead_lag"] == "leading"]
        lagging = [p for p in peers if p["lead_lag"] == "lagging"]

        if len(leading) >= 2:
            signal = "positive"
            note = (f"供应链领先信号：{', '.join(p['symbol'] for p in leading)} "
                    f"5日涨幅领先 {ticker} 超 1.5pp——上游需求强劲，有望传导至 {ticker} 订单端。")
        elif len(lagging) >= 2:
            signal = "negative"
            note = (f"供应链滞后警告：{', '.join(p['symbol'] for p in lagging)} "
                    f"5日涨幅落后 {ticker} 超 1.5pp——上游景气度下行，{ticker} 可能存在需求前瞻性透支。")
        else:
            signal = "neutral"
            soxx_ret = returns.get("SOXX", 0)
            note = (f"供应链与 {ticker} 同步波动（{ticker} 5d {target_ret:+.1f}% vs SOXX {soxx_ret:+.1f}%），"
                    "未发现显著领先/滞后背离。")

        return {
            "peers": peers,
            "supply_chain_signal": signal,
            "supply_chain_note": note,
            "source": "yfinance",
        }
    except Exception as e:
        _empty["supply_chain_note"] = f"供应链分析失败：{e}"
        return _empty


# ─────────────────────────────────────────────────────────────────────────────
# ⑦ 信号拥挤度指数 — calculate_signal_crowding()
# ─────────────────────────────────────────────────────────────────────────────

def calculate_signal_crowding(
    reddit_rank: Optional[int],
    analyst_consensus: Optional[float],   # 1.0=强买 5.0=强卖
    options_flow: str,                    # "bullish"|"bearish"|"neutral"
    swarm_direction: str,                 # "bullish"|"bearish"|"neutral"
    scout_discovery: str = "",
) -> Dict[str, Any]:
    """
    计算信号拥挤度：当多个独立来源同方向且高热度时，alpha 衰减。

    Returns
    -------
    {
      "crowding_index"     : float,   # 0.0（无拥挤）~ 1.0（极度拥挤）
      "crowding_level"     : "low"|"moderate"|"high"|"extreme",
      "alpha_decay_factor" : float,   # 对评分置信度的折减（1.0=无折减, 0.7=折减30%）
      "crowding_note"      : str,
    }
    """
    score = 0.0
    signals = []

    # Reddit 热度（rank 1-3 = 高拥挤）
    if reddit_rank is not None:
        if reddit_rank <= 2:
            score += 0.35
            signals.append(f"Reddit #{reddit_rank}（极热）")
        elif reddit_rank <= 5:
            score += 0.20
            signals.append(f"Reddit #{reddit_rank}（热门）")

    # 分析师共识（1.0-1.8 = 强力买入，代表共识拥挤）
    if analyst_consensus is not None:
        if analyst_consensus <= 1.5:
            score += 0.30
            signals.append(f"分析师共识 {analyst_consensus:.1f}（强买，可能过度一致）")
        elif analyst_consensus >= 4.0:
            score += 0.30
            signals.append(f"分析师共识 {analyst_consensus:.1f}（强卖，可能过度一致）")

    # 期权流与蜂群方向一致（两者都看涨/看跌）
    if options_flow != "neutral" and options_flow == swarm_direction:
        score += 0.20
        signals.append(f"期权流({options_flow})与蜂群同向，短期共识拥挤")

    # 内幕卖出但市场看多（反向拥挤信号）
    if "内幕" in scout_discovery and "卖出" in scout_discovery and swarm_direction == "bullish":
        score += 0.15
        signals.append("内幕卖出 vs 市场看多，存在信息不对称拥挤")

    score = min(1.0, score)

    if score >= 0.75:
        level = "extreme"
        decay = 0.65
        note = f"信号极度拥挤（{score:.2f}）：{', '.join(signals)}。市场共识高度一致时 alpha 大幅衰减，当前置信度打 35% 折扣。"
    elif score >= 0.50:
        level = "high"
        decay = 0.80
        note = f"信号明显拥挤（{score:.2f}）：{', '.join(signals)}。建议降低仓位或等待拥挤消散后再入场。"
    elif score >= 0.25:
        level = "moderate"
        decay = 0.90
        note = f"信号中度拥挤（{score:.2f}）：{', '.join(signals) if signals else '部分来源同向'}。适当关注但无需大幅调整。"
    else:
        level = "low"
        decay = 1.0
        note = "信号拥挤度低，当前分析具备较高独立 alpha 价值。"

    return {
        "crowding_index": round(score, 3),
        "crowding_level": level,
        "alpha_decay_factor": decay,
        "crowding_note": note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ⑧ 论点失效检测 — check_thesis_breaks()
# ─────────────────────────────────────────────────────────────────────────────

def check_thesis_breaks(
    ticker: str,
    current_price: float,
    iv_current: float,
    put_call_ratio: float,
    bear_signals: List[str],
    swarm_score: float,
) -> Dict[str, Any]:
    """
    对照 thesis_breaks_config.json 检测论点失效条件是否触发。

    Returns
    -------
    {
      "level": None | "warning" | "stop_loss",
      "triggered_conditions": [str],
      "recommendation": str,
      "alert_html": str,   # 渲染用 HTML（空字符串 = 无警报）
    }
    """
    _none = {
        "level": None, "triggered_conditions": [],
        "recommendation": "", "alert_html": "",
    }

    config_path = _BASE / "thesis_breaks_config.json"
    if not config_path.exists():
        return _none

    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return _none

    ticker_cfg = cfg.get(ticker, cfg.get("NVDA", {}))
    if not ticker_cfg:
        return _none

    def _eval_condition(cond: Dict) -> bool:
        """解析单个条件 dict，返回是否触发。"""
        field = cond.get("field", "")
        op = cond.get("op", ">")
        val = cond.get("value")
        if val is None:
            return False

        data_map = {
            "price": current_price,
            "iv": iv_current,
            "put_call_ratio": put_call_ratio,
            "score": swarm_score,
            "bear_signals_count": len(bear_signals),
        }
        actual = data_map.get(field)
        if actual is None:
            return False

        if op == ">":
            return actual > val
        elif op == "<":
            return actual < val
        elif op == ">=":
            return actual >= val
        elif op == "<=":
            return actual <= val
        elif op == "==":
            return actual == val
        return False

    # 检查两个告警级别
    triggered: Optional[str] = None
    triggered_conds: List[str] = []

    for level_key in ["level_2_stop_loss", "level_1_warning"]:
        level_cfg = ticker_cfg.get(level_key, {})
        conditions = level_cfg.get("conditions", [])
        fired = [c for c in conditions if _eval_condition(c)]
        if fired:
            triggered = "stop_loss" if level_key == "level_2_stop_loss" else "warning"
            _field_vals = {
                'price': current_price, 'iv': iv_current,
                'put_call_ratio': put_call_ratio, 'score': swarm_score,
                'bear_signals_count': len(bear_signals),
            }
            triggered_conds = [
                f"{c.get('field')} {c.get('op')} {c.get('value')} "
                f"（实际: {_field_vals.get(c.get('field',''), '?')}）"
                for c in fired
            ]
            break

    if not triggered:
        return _none

    # 构建 HTML 告警卡片
    if triggered == "stop_loss":
        bg = "#3d0b0b"; border = "#f85149"; badge = "🔴 STOP LOSS"
        rec = ticker_cfg.get("level_2_stop_loss", {}).get("name", "论点失效，建议认损离场")
    else:
        bg = "#2d1f07"; border = "#ffa028"; badge = "⚠️ WARNING"
        rec = ticker_cfg.get("level_1_warning", {}).get("name", "预警级别，减仓观察")

    cond_html = "".join(f"<li>{c}</li>" for c in triggered_conds[:3])
    alert_html = f"""<div style="background:{bg};border:1px solid {border};border-radius:8px;padding:14px 16px;margin:16px 0;">
  <div style="color:{border};font-weight:700;font-size:0.9em;margin-bottom:6px;">{badge} 论点失效警报</div>
  <div style="color:#e6edf3;font-size:0.85em;">触发条件：<ul style="margin:4px 0 0 16px;padding:0;">{cond_html}</ul></div>
  <div style="color:#7d8590;font-size:0.8em;margin-top:8px;">建议：{rec}</div>
</div>"""

    return {
        "level": triggered,
        "triggered_conditions": triggered_conds,
        "recommendation": rec,
        "alert_html": alert_html,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ① IV-RV Spread — 独立函数（也供 options_analyzer.py 调用）
# ─────────────────────────────────────────────────────────────────────────────

def calculate_iv_rv_spread(
    ticker: str,
    iv_current_pct: float,
    lookback_days: int = 30,
) -> Dict[str, Any]:
    """
    计算历史已实现波动率（HV30）并与当前 IV 对比。

    Returns
    -------
    {
      "rv_30d"       : float,   # 年化 30 日已实现波动率（%）
      "iv_rv_spread" : float,   # IV - RV（pp），正数=期权贵，负数=期权便宜
      "iv_rv_signal" : "expensive"|"cheap"|"fair",
      "iv_rv_note"   : str,     # 供 LLM 引用的自然语言
    }
    """
    _empty = {
        "rv_30d": None, "iv_rv_spread": None,
        "iv_rv_signal": "unknown", "iv_rv_note": "RV 数据不可用",
    }
    try:
        import yfinance as yf
        import numpy as np

        hist = yf.download(ticker, period=f"{lookback_days + 10}d", interval="1d",
                           progress=False, auto_adjust=True)
        if hist.empty or len(hist) < lookback_days // 2:
            return _empty

        closes = hist["Close"].dropna().values.flatten()
        n = min(lookback_days, len(closes) - 1)
        log_rets = np.log(closes[-n:] / closes[-(n + 1):-1])
        rv_daily = float(np.std(log_rets))
        rv_annual = rv_daily * math.sqrt(252) * 100  # 转为年化百分比

        spread = iv_current_pct - rv_annual

        if spread > 10:
            signal = "expensive"
            note = (f"期权溢价：IV {iv_current_pct:.1f}% vs RV30 {rv_annual:.1f}%，"
                    f"价差 +{spread:.1f}pp（期权偏贵 {spread/rv_annual*100:.0f}%），"
                    "卖方策略（垂直价差/Iron Condor）性价比更高。")
        elif spread < -5:
            signal = "cheap"
            note = (f"期权折价：IV {iv_current_pct:.1f}% vs RV30 {rv_annual:.1f}%，"
                    f"价差 {spread:.1f}pp（期权相对便宜），方向性买权成本合理。")
        else:
            signal = "fair"
            note = (f"期权公允：IV {iv_current_pct:.1f}% vs RV30 {rv_annual:.1f}%，"
                    f"价差 {spread:+.1f}pp，期权定价接近历史实际波动，无明显方向性偏差。")

        return {
            "rv_30d": round(rv_annual, 2),
            "iv_rv_spread": round(spread, 2),
            "iv_rv_signal": signal,
            "iv_rv_note": note,
        }
    except Exception as e:
        _empty["iv_rv_note"] = f"RV 计算失败：{e}"
        return _empty
