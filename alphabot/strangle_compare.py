"""Alpha Bot 跨式页「跨式 vs 宽跨」并排对比卡（v0.45.584）。纯函数：不读写文件、不联网、不 import 卖权模块。

要回答的问题（用户 2026-10-21）：财报前，同一只票卖 ATM 跨式、还是卖 25Δ 宽跨更合适？

**本模块只并排事实，不给「选哪个」**——没有综合分、没有推荐 / 胜出字段（`tests/test_alphabot_strangle_compare.py`
钉住键名）。理由：
  · 系统里只有 3 个已结算事件、2 笔平仓，任何「按标的选结构」的规则目前都是在拟合噪声；单只票历史只有 ≤ 8 次财报，
    「超出盈亏平衡带 k/n」的 95% 区间动辄覆盖 7%–59%（T，n=8，k=2），所以页面一律带着区间给；
  · 溢价页同一条原则：不做综合分、权重拍不出依据。检验放在影子记录里（`experiments/straddle_strangle_shadow_prereg.md`
    草案），样本攒够再谈自动选择。

两种结构的取法
-------------
同一份当日快照的 `quote_set`（~30 DTE、同一到期日）：
  · 跨式 = `atm_call` + `atm_put`（账本交易的就是这两条腿）；
  · 宽跨 = `c25` + `p25`（delta 最接近 ±0.25 的那两张，**不是**为这页另选的行权价）。
成交约定同账本（`options_paper_leg`）：卖出按 **bid**、买回按 **ask**；入场要两腿 `quote_ok`（bid > 0 且 ask ≥ bid）。

读数口径
--------
  · `premium` = 两腿 bid 之和（每股）；`round_trip_frac` = (两腿 ask 之和 − bid 之和) ÷ bid 之和：
    「立刻进出一次，要付掉权利金的多少」——宽跨的腿便宜、点差占比大，这是它与跨式最大的结构差别之一；
  · 盈亏平衡带 = `[put 行权价 − premium, call 行权价 + premium]`（到期口径；账本是财报后平仓、不是持有到期，粗略对照）；
  · 历史对照：该票过去财报的 2 日涨跌幅（收盘到收盘，**只取财报日早于 as_of 的**——点时点，不让「事后才发生的那次」
    混进「事前的历史」）落在带外的次数 `k / n` + Wilson 95% 区间；与账本的 `earnings_move_stats` 同一份 `moves` 数据；
  · 定仓：同一条账本规则 `size_contracts`（张数 = floor(风险预算 ÷ (权利金 × 100))）。权利金少的结构张数多——
    **名义风险不一定更小**，这条联动是切换结构时最容易漏掉的，所以并排给出张数与对应股数。
    定仓用的 NAV = 账本当日**开仓前**的净值（由 `straddle.sizing_nav` 从净值曲线还原，来源写在 `nav_basis`）；
    直接取入场日那行净值会少 1 张（那行是开仓之后才写的）。整张卡是**卖方视角**（`seller_view`），不换算买方。
  · 单位：`*_pct`（价格涨跌）= 百分点（5.0 表示 5%）；`*_frac`（点差 / 占比）= 小数（0.15 表示 15%）。

只读、判不了的一律是 None + 原因码（`REASONS`），不是 0。
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional

#: 结构 → 用快照 `quote_set.contracts` 里的哪两个角色
STRUCTURES: Dict[str, Dict[str, str]] = {
    "straddle": {"call": "atm_call", "put": "atm_put"},
    "strangle": {"call": "c25", "put": "p25"},
}
LEG_FIELDS = ("strike", "bid", "ask", "mid", "spread_pct", "delta", "iv", "oi", "volume", "quote_ok")
#: 来自信号行的上下文（白名单拷贝；**不含 GEX 影子字段**——跨式 GEX 预注册的盲期；本模块的源码里连那个字段名都不出现，
#: `tests/test_straddle_gex_shadow.py::TestReaders` 的读者白名单因此不必登记它）
SIGNAL_FIELDS = ("label", "raw_label", "ratio", "implied_event_move_pct", "hist_median_abs_move_pct",
                 "hist_mean_abs_move_pct", "hist_n", "earnings_date", "selected_expiry", "event_move_floor_hit",
                 "max_leg_spread_pct", "tradeable", "untradeable_reason")

#: 原因码唯一一张表（带 `:` 的码按前缀查，后缀是腿名）。页面只显示后端给的 `reason_zh`，前端不另抄。
REASONS: Dict[str, str] = {
    "snapshot_missing": "那天没有期权快照文件",
    "snapshot_unreadable": "快照文件读不了",
    "quote_set_unavailable": "那天的快照里没有可用的四合约报价",
    "no_spot": "没有标的价",
    "missing_leg": "快照里没有这条腿",
    "no_bid": "没有 bid（bid = 0，卖不出去）",
    "quote_not_ok": "报价不合法（缺值或 ask < bid）",
    "no_history": "没有这只票的历史财报涨跌记录",
    "history_unreadable": "历史财报涨跌文件读不了",
    "no_prior_history": "该日期之前没有可用的历史财报涨跌",
}
FLAGS: Dict[str, str] = {
    "wide_spread": "有腿的点差超过账本对 ATM 腿的可交易闸门（同一把尺，宽跨腿也套）：按可成交价难以成交",
}
CAVEATS: Dict[str, str] = {
    "after_hours": "快照是收盘后的报价，点差通常比盘中宽，宽跨的可成交性按它看可能偏悲观；宽多少没有统计——手上只有一个样本"
                   "（2026-09-03 同日 COST 25Δ：盘后 27.6% vs 盘中 9.6%），倍数待验证，不要拿它折算",
    "band_is_expiry": "盈亏平衡带是到期口径，历史涨跌是财报前后 2 日的收盘到收盘——账本是财报后平仓、不是持有到期，只能粗略对照",
    "small_sample": "单只票的历史最多 8 次，「落在带外 k/n」只是描述，不是预测；区间很宽",
    "no_verdict": "本页只并排事实、不给「选哪个」：系统里已结算事件极少，任何选择规则目前都是在拟合；检验在影子记录里攒样本",
    "seller_view": "整张卡是卖方视角：权利金按 bid 收、买回按 ask，张数按空头的账本定仓规则；买入跨式的成本在 ask 一侧，这里没有换算",
}


def reason_zh(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    head, _, tail = str(code).partition(":")
    base = REASONS.get(head)
    if base is None:
        return f"未登记的原因码 {code}"
    return f"{base}（{tail}）" if tail else base


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v) -> Optional[float]:
    f = _num(v)
    return f if (f is not None and f > 0) else None


def nav_basis(info) -> Optional[dict]:
    """定仓 NAV 的来源说明（服务层从 `straddle.sizing_nav` 的结果传进来；本模块不 import 它）。
    `{"kind", "label", "text", "nav_date", "adjust_usd"}`；没给 `info` ⇒ None。文案只在这里写一份，前端不抄。"""
    if not isinstance(info, dict):
        return None
    kind, d = str(info.get("basis") or "none"), info.get("nav_date")
    adj = _num(info.get("adjust_usd")) or 0.0
    if kind == "pre_open":
        label = "当日开仓前 NAV"
        text = ("账本定仓用的是当日开仓前的 NAV（先盯市、再开仓、最后才写净值行）；"
                + (f"这里由 {d} 那行净值加回当日新开仓的入场滑点 ${adj:,.0f} 还原，与账本的 nav_for_sizing 同口径，约有 $1 的取整误差。"
                   if adj else f"当日没有新开仓，{d} 那行净值即开仓前 NAV，与账本的 nav_for_sizing 同口径。"))
    elif kind == "latest_on_or_before":
        label = "最近一行 NAV"
        text = (f"这一天账本没有净值行，用 {d} 的收盘 NAV 近似；账本实际定仓用的是当日开仓前的 NAV，可能差 1 张。")
    elif kind == "post_open_unadjusted":
        label = "当日收盘 NAV（未还原）"
        text = (f"当日有新开仓但还原不了开仓前的 NAV（{info.get('reason') or '—'}），用 {d} 的收盘 NAV；"
                "它比账本定仓用的低「半个点差 × 张数」，张数可能比账本少 1 张。")
    else:
        label, text = None, "没有可用的净值行，定仓张数不给。"
    return {"kind": kind, "label": label, "text": text, "nav_date": d, "adjust_usd": adj}


def wilson(k: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    """比例 k/n 的 Wilson 区间（n 很小时比正态近似可靠）。n ≤ 0 ⇒ None。"""
    if n <= 0:
        return None
    p = k / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / den
    return [max(0.0, centre - half), min(1.0, centre + half)]


def leg_brief(c) -> Optional[dict]:
    return {k: c.get(k) for k in LEG_FIELDS} if isinstance(c, dict) else None


def prior_moves(raw_moves, as_of: str) -> List[dict]:
    """历史财报涨跌（带符号，百分点），只取财报日**早于** as_of 的，新→旧；坏行丢弃。"""
    out = []
    for m in raw_moves or []:
        if not isinstance(m, dict):
            continue
        ed, mv = str(m.get("earnings_date") or ""), _num(m.get("move_pct"))
        if mv is None or not ed or ed >= as_of:
            continue
        out.append({"earnings_date": ed, "move_pct": mv})
    return sorted(out, key=lambda x: x["earnings_date"], reverse=True)


def _hist_block(moves: List[dict]) -> dict:
    absm = sorted(abs(m["move_pct"]) for m in moves)
    n = len(absm)
    med = None if not n else (absm[n // 2] if n % 2 else (absm[n // 2 - 1] + absm[n // 2]) / 2.0)
    return {"n": n, "moves": moves, "median_abs_pct": med,
            "mean_abs_pct": (sum(absm) / n) if n else None, "max_abs_pct": absm[-1] if n else None}


def _structure(name: str, contracts: dict, S: float, moves: List[dict], nav,
               size_fn: Callable, max_spread_frac: Optional[float]) -> dict:
    roles = STRUCTURES[name]
    call, put = contracts.get(roles["call"]), contracts.get(roles["put"])
    out = {"name": name, "roles": dict(roles), "legs": {"call": leg_brief(call), "put": leg_brief(put)},
           "available": False, "reason": None, "reason_zh": None, "premium": None, "premium_frac_of_spot": None,
           "cost_to_close": None, "round_trip_frac": None, "max_spread_frac": None, "band": None,
           "hist_outside": None, "sizing": None, "flags": []}
    for side, leg in (("call", call), ("put", put)):
        if not isinstance(leg, dict):
            out["reason"] = f"missing_leg:{side}"
            break
        if _pos(leg.get("bid")) is None:
            out["reason"] = f"no_bid:{side}"
            break
        if not leg.get("quote_ok"):
            out["reason"] = f"quote_not_ok:{side}"
            break
    if out["reason"]:
        out["reason_zh"] = reason_zh(out["reason"])
        return out
    bid_sum, ask_sum = call["bid"] + put["bid"], call["ask"] + put["ask"]
    kc, kp = float(call["strike"]), float(put["strike"])
    lo, hi = kp - bid_sum, kc + bid_sum
    spreads = [x for x in (_num(call.get("spread_pct")), _num(put.get("spread_pct"))) if x is not None]
    max_sp = max(spreads) if spreads else None
    contracts_n, size_why = size_fn(bid_sum, nav)
    lo_pct, hi_pct = (lo / S - 1.0) * 100.0, (hi / S - 1.0) * 100.0
    outside = [m for m in moves if m["move_pct"] <= lo_pct or m["move_pct"] >= hi_pct]
    k, n = len(outside), len(moves)
    out.update({
        "available": True, "premium": bid_sum, "premium_frac_of_spot": bid_sum / S,
        "cost_to_close": ask_sum, "round_trip_frac": (ask_sum - bid_sum) / bid_sum, "max_spread_frac": max_sp,
        "band": {"lo": lo, "hi": hi, "lo_pct": lo_pct, "hi_pct": hi_pct},
        "hist_outside": {"n": n, "k": k, "frac": (k / n) if n else None, "ci95": wilson(k, n), "outside": outside},
        "sizing": {"contracts": contracts_n, "reason": size_why, "shares": contracts_n * 100,
                   "premium_usd": bid_sum * 100.0 * contracts_n, "notional_usd": contracts_n * 100.0 * S}})
    if max_spread_frac is not None and max_sp is not None and max_sp > max_spread_frac:
        out["flags"].append({"code": "wide_spread", "text": FLAGS["wide_spread"],
                             "value": max_sp, "limit": max_spread_frac})
    return out


def compare(ticker: str, as_of: str, *, qs: Optional[dict], qs_error: Optional[str] = None,
            raw_moves=None, hist_error: Optional[str] = None, signal: Optional[dict] = None,
            nav=None, size_fn: Callable = lambda premium, nav: (0, "sizing unavailable"),
            max_spread_frac: Optional[float] = None, nav_info: Optional[dict] = None) -> dict:
    """一张对比卡的全部数据。`qs` = 快照里的 `quote_set`（取不到就给 `qs_error`）；`raw_moves` = 历史财报涨跌原始行
    （`earnings_cache/<T>_moves.json` 的 `moves`）；`signal` = 当天的信号行（只取白名单字段）；
    `size_fn(premium, nav) -> (张数, 原因)` 由服务层注入账本自己的定仓函数（本模块不 import 它）；`nav_info` = 该 NAV
    的来源说明（`straddle.sizing_nav` 的结果，只用来写 `nav_basis`，不参与计算）。"""
    base = {"ticker": ticker, "as_of": as_of, "available": False, "reason": None, "reason_zh": None,
            "underlying_price": None, "selected_expiry": None, "dte": None, "market_open": None, "fetched_at": None,
            "nav": _pos(nav), "nav_basis": nav_basis(nav_info), "signal": {k: (signal or {}).get(k) for k in SIGNAL_FIELDS} if signal else None,
            "hist": None, "hist_reason": None, "hist_reason_zh": None, "structures": None,
            "premium_ratio": None, "contracts_ratio": None, "caveats": [], "rules": {"max_spread_frac": max_spread_frac}}
    if not isinstance(qs, dict):
        code = qs_error or "snapshot_missing"
        return {**base, "reason": code, "reason_zh": reason_zh(code)}
    if not qs.get("data_available"):
        return {**base, "reason": "quote_set_unavailable",
                "reason_zh": f"{reason_zh('quote_set_unavailable')}：{qs.get('error') or '—'}",
                "selected_expiry": qs.get("selected_expiry")}
    S = _pos(qs.get("underlying_price"))
    if S is None:
        return {**base, "reason": "no_spot", "reason_zh": reason_zh("no_spot")}
    moves = prior_moves(raw_moves, as_of)
    hist_reason = hist_error or (None if raw_moves else "no_history")
    if hist_reason is None and not moves:
        hist_reason = "no_prior_history"
    contracts = qs.get("contracts") or {}
    structs = {nm: _structure(nm, contracts, S, moves, nav, size_fn, max_spread_frac) for nm in STRUCTURES}
    a, b = structs["straddle"], structs["strangle"]
    p_ratio = (b["premium"] / a["premium"]) if (a["available"] and b["available"]) else None
    ca, cb = (a["sizing"] or {}).get("contracts"), (b["sizing"] or {}).get("contracts")
    c_ratio = (cb / ca) if (ca and cb) else None
    caveats = ["band_is_expiry", "small_sample", "seller_view", "no_verdict"]
    if qs.get("market_open") is False:
        caveats.insert(0, "after_hours")
    notes = [{"code": c, "text": CAVEATS[c]} for c in caveats]
    if base["nav_basis"] is not None:                       # 张数是怎么定的（动态文案，不进静态表）
        notes.append({"code": "nav_basis", "text": base["nav_basis"]["text"]})
    return {**base, "available": True, "underlying_price": S, "selected_expiry": qs.get("selected_expiry"),
            "dte": qs.get("selected_dte"), "market_open": qs.get("market_open"), "fetched_at": qs.get("fetched_at"),
            "hist": _hist_block(moves) if moves else None, "hist_reason": hist_reason,
            "hist_reason_zh": reason_zh(hist_reason), "structures": structs, "premium_ratio": p_ratio,
            "contracts_ratio": c_ratio, "caveats": notes}
