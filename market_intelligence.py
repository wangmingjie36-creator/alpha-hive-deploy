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

# v0.45.44：本模块此前无 logger，所有降级都是静默的。
try:
    from hive_logger import get_logger
    _log = get_logger("market_intel")
except Exception:  # pragma: no cover - 独立运行/测试时退化到标准库
    import logging as _logging
    _log = _logging.getLogger("alpha_hive.market_intel")

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
            """返回 (最新收盘价, N日均线) 或 (nan, nan)。

            ⚠️ **必须校验返回的确实是 `sym` 的数据**（v0.45.52）。
            yfinance 限流时会返回**上一次成功请求的缓存帧**，而原来的守卫只查
            `empty` 与 `len < period` —— 一份完整的**别家**数据两条都过。

            2026-08-26 实测（那次扫描 yfinance 限流 487 次）：板块层的
            `_get_ma("SOXX", 20, 40)` 与个股层的 `_get_ma(ticker, 20, 40)`
            用的是**同一个 period 字符串**（"60d"），于是 SOXX 的均线泄漏进了
            NVDA / MSFT / TSLA / VKTX 的个股政体 —— 四只标的的 20MA 全是
            $528（SOXX 真值 529），而 NVDA 自己的 20MA 是 215.56。
            个股金叉/死叉判断因此建立在半导体 ETF 的均线上。

            校验不通过返回 `(nan, nan)` → 调用方走「个股政体数据不可用」，
            诚实缺失好过安静地用别人的数据。
            """
            hist = yf.download(sym, period=f"{window+20}d", interval="1d",
                               progress=False, auto_adjust=True)
            if hist.empty or len(hist) < period:
                return float("nan"), float("nan")
            if getattr(hist.columns, "nlevels", 1) > 1:
                got = {str(x).upper() for x in hist.columns.get_level_values(-1)}
                if got and sym.upper() not in got:
                    _log.warning("yfinance 请求 %s 却返回了 %s 的数据 —— 弃用"
                                 "（限流时返回缓存帧，见本函数 docstring）",
                                 sym, ",".join(sorted(got)))
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
            # v0.45.54：SOXX 取不到时 `returns.get("SOXX", 0)` 给 0，
            # 于是摘要里印出「vs SOXX +0.0%」—— 一个被观测到的周涨跌幅，
            # 而实际是取数失败。这句话会进 discovery 与报告正文。
            _soxx = returns.get("SOXX")
            if isinstance(_soxx, (int, float)) and not isinstance(_soxx, bool):
                note = (f"供应链与 {ticker} 同步波动（{ticker} 5d {target_ret:+.1f}% "
                        f"vs SOXX {_soxx:+.1f}%），未发现显著领先/滞后背离。")
            else:
                _log.warning("%s 供应链分析：SOXX 5 日收益不可得，摘要中不写基准涨跌幅", ticker)
                note = (f"供应链与 {ticker} 同步波动（{ticker} 5d {target_ret:+.1f}%；"
                        "SOXX 基准本次不可得），未发现显著领先/滞后背离。")

        return {
            "peers": peers,
            "supply_chain_signal": signal,
            "supply_chain_note": note,
            # v0.45.54：source 由实际取到的同业数量推导，不再无条件写 "yfinance"
            "source": "yfinance" if peers else "unavailable",
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
    # v0.45.44：`evaluable` 区分「核对过、没触发」与「根本没核对」。
    # 实测（2026-08-26）：本闸**从未触发过一次**——极端输入（price $1 vs
    # $100,000 / IV 200% / P/C 5.0 / score 0 / 6 条看空）在 NVDA 与 WMT 上
    # 一律返回 level=None。根因是 schema 对不上：
    #   配置存的是人读散文 {id, metric, trigger, data_source, current_status}
    #   求值器要的是机器可比 {field, op, value}
    # → `cond.get("value")` 恒为 None → `_eval_condition` 第一行就 return False。
    # 而 level=None 在下游读作「论点完好」。CLAUDE.md 把「任何结论必须附失效
    # 条件」列为硬约束，于是这条硬约束长期是靠一个永不触发的闸在"满足"。
    # 这里先让它**可见**；配置 schema 的迁移是独立决定（且该文件正被其他
    # session 编辑），不在本次改动范围。
    # v0.45.62：新增 `evaluations` —— **每一条**条件的当前值与判定，
    # 不只是触发的那几条。日报 7.5 节要把「阈值 + 当前值 + 判定」一起打出来，
    # 否则读者看到的是一张静态阈值表，分不清「没触发」和「不可能触发」。
    # 它在**每一条 return 路径上都显式出现**，不留给调用方 `.get("evaluations", [])`
    # 去兜——那种默认值恰好在出错时最像正常（本仓库反复踩过的坑）。
    _none = {
        "level": None, "triggered_conditions": [],
        "recommendation": "", "alert_html": "",
        "evaluable": False, "unevaluable_reason": "",
        "evaluations": [],
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

    # ── v0.45.62：逐条求值明细（含未触发的）────────────────────────
    _field_vals_all = {
        'price': current_price, 'iv': iv_current,
        'put_call_ratio': put_call_ratio, 'score': swarm_score,
        'bear_signals_count': len(bear_signals),
    }
    _evaluations: List[Dict[str, Any]] = []
    for _lk in ("level_1_warning", "level_2_stop_loss"):
        for _c in (ticker_cfg.get(_lk, {}) or {}).get("conditions", []) or []:
            if not isinstance(_c, dict):
                continue
            _fld = _c.get("field")
            _val = _c.get("value")
            if not _fld or _val is None:
                # 人读散文条件：求值器碰不到，如实标注而不是当作「未触发」
                _evaluations.append({
                    "level": _lk, "field": None, "op": None, "value": None,
                    "actual": None, "fired": None, "machine": False,
                    "label": _c.get("metric") or _c.get("id") or "",
                })
                continue
            _act = _field_vals_all.get(_fld)
            _evaluations.append({
                "level": _lk, "field": _fld, "op": _c.get("op", ">"), "value": _val,
                "actual": _act,
                "fired": _eval_condition(_c) if _act is not None else None,
                "machine": bool(_c.get("_machine")),
                "label": _c.get("_note") or _fld,
            })

    # ── v0.45.44：先判「这份配置到底可不可求值」──────────────────────
    # 一条 condition 只有同时带 field/op/value 才是机器可比的；
    # 只有 metric/trigger/current_status 的是给人读的散文，求值器碰不到。
    _all_conds = []
    for _lk in ("level_2_stop_loss", "level_1_warning"):
        _all_conds += (ticker_cfg.get(_lk, {}) or {}).get("conditions", []) or []
    _evaluable_conds = [c for c in _all_conds
                        if isinstance(c, dict) and c.get("field") and c.get("value") is not None]
    _is_fallback = ticker not in cfg

    if not _evaluable_conds:
        _reason = (f"{ticker} 的 {len(_all_conds)} 条失效条件全部不可机器求值"
                   f"（缺 field/value，是给人读的散文 schema）"
                   + ("；且该标的无专属配置，用的是 NVDA 兜底" if _is_fallback else ""))
        _log.warning("论点失效闸未执行：%s —— 本次返回「未核对」而非「论点完好」", _reason)
        _out = dict(_none)
        _out["unevaluable_reason"] = _reason
        # v0.45.62：这些条件求值器碰不到，但**它们存在**——明细照样返回，
        # 让渲染层标「人工条件，未自动核对」，而不是整级退回只有阈值。
        _out["evaluations"] = _evaluations
        return _out
    if _is_fallback:
        _log.warning("%s 无专属论点失效配置，回落到 NVDA 的条件（数据中心营收/AMD 竞品/"
                     "中国芯片禁令等），对本标的无意义", ticker)

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
        # 走到这里说明**确实核对过**了，与「没核对」区分开
        _ok = dict(_none)
        _ok["evaluable"] = True
        _ok["evaluations"] = _evaluations
        return _ok

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
        "evaluable": True,
        "unevaluable_reason": "",
        "evaluations": _evaluations,
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
        "data_available": False, "error": "",
    }
    # ── v0.45.61：Twelve Data 优先 ──────────────────────────────────
    # 这是整条链上最后一处非 yfinance 不可的依赖。免费档 800 次/天，
    # 30 只标的 × 1 credit 远在额度内；未配 key 时 `realized_vol` 返回 None，
    # 原样落到下面的 yfinance 路径，不报错、不阻断。
    # 补跑时必须取**目标日**的窗口。`ALPHA_HIVE_TARGET_DATE` 是本项目既有的
    # 补跑信道（options_analyzer 用同一个），这里沿用而不另造。
    #
    # v0.45.61 二次检查发现：`realized_vol` 一开始就支持 `end_date`，但调用处
    # 没传 —— 补跑会拿**最新**窗口冒充目标日。对次日补跑影响小（forming-bar
    # 已丢、窗口末端恰好是目标日），但补跑更早的日子会严重错位。
    # v0.45.90 补：上面那句「窗口末端恰好是目标日」是**要达到的状态**，
    # 而 v0.45.61 传了 `end_date` 之后其实并没达到 —— Twelve Data 的
    # `end_date` 左闭右开，目标日那根不返回，窗口末端一直是目标日的**前**
    # 一个交易日（2026-09-01 实测 NVDA：end_date=8/31 → 末根 8/28）。
    # 差值现在由 `twelve_data._api_end_date` 补掉，这里的语义不变。
    # ⚠️ 下面的 yfinance 兜底路径**仍然**是「最近 N 天」口径，补跑较早日期时
    # 那条路给出的 RV 不属于目标日 —— 已知缺陷，未在本版修（要改成 start/end）。
    _target = ""
    try:
        import os as _os
        import re as _re
        _t = (_os.environ.get("ALPHA_HIVE_TARGET_DATE", "") or "").strip()
        if _t and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", _t):
            _target = _t
    except Exception:  # pragma: no cover
        pass

    try:
        from twelve_data import is_configured as _td_ok, realized_vol as _td_rv
        if _td_ok():
            _rv_td = _td_rv(ticker, lookback=lookback_days, end_date=_target or None)
            if _rv_td is not None:
                _spread_td = iv_current_pct - _rv_td
                _sig_td = ("expensive" if _spread_td > 10
                           else "cheap" if _spread_td < -10 else "fair")
                return {
                    "rv_30d": round(_rv_td, 2),
                    "iv_rv_spread": round(_spread_td, 2),
                    "iv_rv_signal": _sig_td,
                    "iv_rv_note": (f"IV {iv_current_pct:.1f}% vs RV30 {_rv_td:.1f}%，"
                                   f"价差 {_spread_td:+.1f}pp"),
                    "data_available": True,
                    "error": "",
                    "source": ("twelve_data@" + _target) if _target else "twelve_data",
                }
    except Exception as _e_td:  # noqa: BLE001 - 任何失败都退回 yfinance
        _log.debug("[%s] Twelve Data RV 不可用，退回 yfinance: %s", ticker, _e_td)

    try:
        import time

        try:                                    # v0.45.56 限流闸门
            from yf_gate import ensure as _yf_ensure
            _yf_ensure()
        except Exception:                       # pragma: no cover - 闸门不可得不阻断
            pass

        import yfinance as yf
        import numpy as np

        # v0.45.4: 进 http_gate 闸门 + 退避重试。
        # 此前这里是裸 yf.download，不进闸门；失败即返回 _empty，下游
        # `.get("rv_30d", 0.0)` 拿到 **None**（键存在！）再被渲染成 0.0，
        # 与"真的没波动"无法区分。2026-08-24 那批 12 只有 4 只如此。
        # 进闸门的理由是不给 yfinance 限流器加压——那个是实测存在的（8/27 全天 687 次 429）。
        # ⚠️ 原注把这些失败归因于「本机 OpenSSL 1.1.1q 并发 HTTPS 抛 SSLError/EOF」，
        # 该归因 2026-08-25 已证伪、8/24 根因未定（见 http_gate docstring）。
        try:
            from http_gate import https_gate
        except Exception:  # pragma: no cover - 闸门不可得时退化直连
            from contextlib import nullcontext

            def https_gate(*_a, **_k):
                return nullcontext()

        # v0.45.56: 429 与瞬时故障必须分开处理。
        # 旧实现对**所有**失败都退避 0.7s/1.4s 再重试——那是按「瞬时 SSL/超时故障、
        # 错开即恢复」设计的（⚠️ 当时把这类故障归因于本机 OpenSSL 版本，该归因已证伪，
        # 见 http_gate docstring；退避对瞬时故障本身仍成立）。对上
        # yfinance 限流它是反向的：把 1 次请求变成 3 次、间隔不到 2 秒，
        # **在被拒绝时加倍施压**。2026-08-27 全天 687 次 429、rv_30d 0/30，
        # 这个循环是放大器之一。现在：一次 429 就停，不重试。
        try:
            from yf_gate import is_rate_limit_error as _is_rl
        except Exception:  # pragma: no cover - 闸门不可得时退化
            def _is_rl(_e):
                _m = str(_e).lower()
                return "too many requests" in _m or "rate limited" in _m

        hist = None
        _dl_err = None
        for _attempt in range(3):
            try:
                with https_gate():
                    hist = yf.download(ticker, period=f"{lookback_days + 10}d", interval="1d",
                                       progress=False, auto_adjust=True)
                if hist is not None and not hist.empty:
                    break
            except Exception as _e_dl:  # noqa: BLE001 — 瞬时 SSL 错开即恢复
                _dl_err = _e_dl
                if _is_rl(_e_dl):
                    break          # 限流：重试只会加深，直接降级
            if _attempt < 2:
                time.sleep(0.7 * (_attempt + 1))

        if hist is None or hist.empty or len(hist) < lookback_days // 2:
            _empty["error"] = (f"yfinance 历史K线不可用（{type(_dl_err).__name__}:{_dl_err}）"
                               if _dl_err else
                               f"yfinance 历史K线不足（{0 if hist is None else len(hist)} 根，需 ≥{lookback_days // 2}）")
            _empty["iv_rv_note"] = f"RV 数据不可用：{_empty['error']}"
            return _empty

        # yfinance >= 0.2.49 对单 ticker 也返回 MultiIndex columns，需显式取列
        close_col = hist["Close"]
        if hasattr(close_col, "columns"):
            # DataFrame → 取第一列（单 ticker 场景）
            close_col = close_col.iloc[:, 0]
        closes = close_col.dropna().values.flatten().astype(float)

        # 过滤掉无效价格（0/负值/极低 ~1.0 哨兵值均来自 yfinance sample data）。
        # v0.45.4: 旧实现用绝对阈值 `closes > 5`，会把**低价股的全部真实收盘价**
        # 一起滤光——AMC ≈$3 时整条序列清零 → RV 结构性永远不可用（8/24 实测）。
        # 改用相对阈值：哨兵值（~1.0）相对真实价格数量级极小，取中位数的 20% 即可
        # 区分，而同一只股票 30 天内不可能跌到中位数的 1/5。
        closes = closes[closes > 0]
        if len(closes) >= 5:
            _med = float(np.median(closes))
            closes = closes[closes >= _med * 0.2]
        n = min(lookback_days, len(closes) - 1)
        if n < 5:
            _empty["error"] = f"有效收盘价不足（{len(closes)} 根）"
            _empty["iv_rv_note"] = f"RV 数据不可用：{_empty['error']}"
            return _empty

        log_rets = np.log(closes[-n:] / closes[-(n + 1):-1])

        # 过滤异常跳升：单日 |log_ret| > 0.5（≈ e^0.5-1=65% 日涨跌）视为数据污染
        log_rets = log_rets[np.abs(log_rets) < 0.5]
        if len(log_rets) < 5:
            _empty["error"] = f"有效日收益不足（{len(log_rets)} 个，异常跳升已剔除）"
            _empty["iv_rv_note"] = f"RV 数据不可用：{_empty['error']}"
            return _empty

        rv_daily = float(np.std(log_rets, ddof=1))
        rv_annual = rv_daily * math.sqrt(252) * 100  # 转为年化百分比

        # Sanity check：正常股票 HV30 不应超过 300%
        if rv_annual <= 0 or rv_annual > 300:
            _empty["error"] = f"HV30={rv_annual:.1f}% 超出合理区间 (0, 300]"
            _empty["iv_rv_note"] = f"RV 数据异常（HV30={rv_annual:.1f}%），已跳过"
            return _empty

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
            "data_available": True,
            "error": "",
        }
    except Exception as e:
        _empty["error"] = f"{type(e).__name__}:{e}"
        _empty["iv_rv_note"] = f"RV 计算失败：{e}"
        return _empty
