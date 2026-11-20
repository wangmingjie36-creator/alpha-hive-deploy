"""标的 · 成交榜（v0.45.776）——纯函数：一只票整条链的合约按**当日累计成交量**排名。

输入就是「标的」页现算用的那份原始链（`service.live` 顺手留下的，不另取）；筛→排→截断都在这里做，前端只排版。

每一行是**一个合约当天到此刻的成交合计**，不是一笔订单：
- 分不出买卖方向、分不出开仓还是平仓（CBOE 延迟快照没有这些信息）——Call 成交多 ≠ 看涨，价差 / 跨式的每条腿各算各的；
- CBOE 行情约延迟 15 分钟；开盘前、刚开盘时绝大多数合约成交量是 0（是真 0，不是缺数据）；
- 「约时间价值额」= 成交量 ×（现在的中间价 − 内在价值）× 100，**是估算**（v0.45.786 起只算时间价值）：
  内在价值 = 立刻行权能拿到的部分（call 为 max(现价 − 行权价, 0)，put 反之）。不扣掉它，深度实值的长期合约（一张 $190 多、
  几乎全是内在价值，多是替代持股 / 对冲 / 组合的一条腿）会把整侧的「成交额」撑大——2026-11-16 NVDA 实测 Put 侧 $1.01B 里 63%
  来自实值合约，看上去像钱都去了 Put。
  中间价比内在价值还低（延迟的现价与报价对不上、深度实值报价偏低）⇒ 这张的时间价值记 0、计数（`n_time_value_below_intrinsic`）。
  实际成交价不在链里：**当天到期（0DTE）的合约价格一天里变很多**，按此刻的中间价算会和早些时候的成交价差很远——这一项扣不掉，
  汇总里另给「其中当天到期的占比」让人知道有多少受它影响；
- 「成交 / OI」的 OI 是前一交易日收盘的，0 时不算（写「—」，不当无穷大），成交量太小时不参与这一列的排序（噪声）。
只展示，不进评分、不写账本、未经任何检验。

算不出就如实说：链里没有 / 成交量缺失的合约计数（`counts`），不当 0。原因码唯一表 `REASONS`，页面只翻译它。
本模块不 import 卖权模块（只有 `alphabot/service.py` 可以）。
"""
from __future__ import annotations

import math
from typing import Any, List, Mapping, Optional

CONTRACT_MULT = 100.0

RULES = {
    "limit_default": 50,
    "limit_choices": (25, 50, 100, 200),
    "vol_oi_min_volume": 100,       # 成交量低于它的合约不参与「成交 / OI」排序（几手成交对 1 手 OI 的比值没有意义）
}

SORTS = {"volume": "成交量", "time_value": "约时间价值额", "vol_oi": "成交 / OI"}
SIDES = {"all": "全部", "call": "Call", "put": "Put"}
DEFAULTS = {"side": "all", "sort": "volume", "limit": RULES["limit_default"], "expiry": None}

REASONS = {
    "no_chain": "这次现算没有留下原始链（重新拉取一次即可）",
    "underlying_price_unavailable": "链里没有现价",
    "no_contracts": "链里一个合约都没有",
    "no_volume_today": "整条链今天还没有任何成交（开盘前 / 刚开盘时是常态）",
    "expiry_not_in_chain": "链上没有这个到期日",
}

DISCLAIMER = ("每行是一个合约当天到此刻的成交合计，不是一笔订单；分不出买卖方向与开平仓，Call 成交多不等于看涨。"
              "约时间价值额 = 成交量 ×（现在的中间价 − 内在价值）× 100，只算时间价值、是估算，不是实际成交额；"
              "当天到期的合约价格一天里变很多，偏差最大。只展示，不进评分，未经检验。")


class ParamError(ValueError):
    def __init__(self, msg: str, param: str):
        super().__init__(msg)
        self.param = param


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_params(q: Mapping[str, Any]) -> dict:
    """查询参数 → 已校验的参数。未知键 / 取值不对 ⇒ ParamError(param=…)。`expiry` 只校验格式，在不在链上由 `select` 判。"""
    q = dict(q or {})
    unknown = sorted(set(q) - set(DEFAULTS))
    if unknown:
        raise ParamError(f"未知参数：{', '.join(unknown)}", unknown[0])
    out = dict(DEFAULTS)
    side = str(q.get("side") or DEFAULTS["side"])
    if side not in SIDES:
        raise ParamError(f"side 只能是 {' / '.join(SIDES)}", "side")
    sort = str(q.get("sort") or DEFAULTS["sort"])
    if sort not in SORTS:
        raise ParamError(f"sort 只能是 {' / '.join(SORTS)}", "sort")
    raw_limit = q.get("limit")
    if raw_limit in (None, ""):
        limit = DEFAULTS["limit"]
    else:
        try:
            limit = int(str(raw_limit))
        except ValueError:
            raise ParamError("limit 要是整数", "limit") from None
        if limit not in RULES["limit_choices"]:
            raise ParamError(f"limit 只能是 {' / '.join(map(str, RULES['limit_choices']))}", "limit")
    exp = q.get("expiry")
    if exp not in (None, ""):
        exp = str(exp)
        if len(exp) != 10 or exp[4] != "-" or exp[7] != "-" or not (exp[:4] + exp[5:7] + exp[8:]).isdigit():
            raise ParamError("expiry 要是 YYYY-MM-DD", "expiry")
    else:
        exp = None
    out.update(side=side, sort=sort, limit=limit, expiry=exp)
    return out


def _mid(bid, ask) -> Optional[float]:
    if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def intrinsic_value(side: str, strike: float, S: float) -> float:
    """立刻行权能拿到的部分（每股）。"""
    return max(S - strike, 0.0) if side == "call" else max(strike - S, 0.0)


def _row(c: dict, S: float) -> Optional[dict]:
    cp, k, exp = c.get("cp"), _num(c.get("strike")), c.get("expiry")
    if cp not in ("C", "P") or k is None or k <= 0 or not isinstance(exp, str) or not exp:
        return None
    vol = _num(c.get("volume"))
    oi = _num(c.get("oi"))
    bid, ask = _num(c.get("bid")), _num(c.get("ask"))
    mid = _mid(bid, ask)
    side = "call" if cp == "C" else "put"
    # 虚值程度：正 = 虚值（call 在现价上方 / put 在现价下方），负 = 实值
    otm = (k / S - 1.0) if side == "call" else (1.0 - k / S)
    intr = intrinsic_value(side, k, S)
    tv = (mid - intr) if mid is not None else None
    below = tv is not None and tv < 0
    if below:
        tv = 0.0
    return {
        "symbol": c.get("symbol"), "side": side, "expiry": exp, "dte": _num(c.get("dte")), "strike": k,
        "otm_pct": round(otm * 100, 2), "volume": vol, "oi": oi,
        "vol_oi": round(vol / oi, 2) if (vol is not None and oi is not None and oi > 0) else None,
        "bid": bid, "ask": ask, "mid": round(mid, 4) if mid is not None else None,
        "intrinsic": round(intr, 4), "time_value": round(tv, 4) if tv is not None else None,
        "time_value_below_intrinsic": below,
        "tv_notional_est": round(vol * tv * CONTRACT_MULT, 0) if (vol is not None and tv is not None) else None,
        "iv": _num(c.get("iv")), "delta": _num(c.get("delta")), "last_trade_time": c.get("last_trade_time"),
    }


def build(raw: Optional[dict]) -> dict:
    """与查询参数无关的部分（可缓存）：逐合约行 + 整条链的汇总。"""
    if not raw:
        return {"data_available": False, "reason": "no_chain"}
    S = _num(raw.get("underlying_price"))
    if S is None or S <= 0:
        return {"data_available": False, "reason": "underlying_price_unavailable"}
    contracts = [c for c in raw.get("contracts") or [] if isinstance(c, dict)]
    if not contracts:
        return {"data_available": False, "reason": "no_contracts"}
    rows, bad, no_vol = [], 0, 0
    for c in contracts:
        r = _row(c, S)
        if r is None:
            bad += 1
            continue
        if r["volume"] is None:
            no_vol += 1
        rows.append(r)
    traded = [r for r in rows if (r["volume"] or 0) > 0]
    return {
        "data_available": True, "reason": None,
        "chain": {"as_of": raw.get("as_of") or raw.get("vintage_date"), "underlying_price": S,
                  "underlying_price_source": raw.get("underlying_price_source"),
                  "payload_last_trade_time": raw.get("payload_last_trade_time"),
                  "session_live": raw.get("session_live"), "fetched_at": raw.get("fetched_at")},
        "rows": rows,
        "expiries": sorted({r["expiry"] for r in rows}),
        "counts": {"n_contracts": len(contracts), "n_unparseable": bad, "n_volume_missing": no_vol,
                   "n_traded": len(traded)},
    }


def _summary(rows: List[dict]) -> dict:
    cv = sum(r["volume"] or 0.0 for r in rows if r["side"] == "call")
    pv = sum(r["volume"] or 0.0 for r in rows if r["side"] == "put")
    cn = sum(r["tv_notional_est"] or 0.0 for r in rows if r["side"] == "call")
    pn = sum(r["tv_notional_est"] or 0.0 for r in rows if r["side"] == "put")
    zn = sum(r["tv_notional_est"] or 0.0 for r in rows if r["dte"] is not None and r["dte"] <= 0)
    tot = cv + pv
    return {"call_volume": cv, "put_volume": pv, "total_volume": tot,
            "put_call_volume": round(pv / cv, 2) if cv > 0 else None,
            "call_pct": round(cv / tot * 100, 1) if tot > 0 else None,
            "call_tv_notional_est": cn, "put_tv_notional_est": pn,
            "zero_dte_tv_share_pct": round(zn / (cn + pn) * 100, 1) if (cn + pn) > 0 else None,
            "n_time_value_below_intrinsic": sum(1 for r in rows if r["time_value_below_intrinsic"]
                                                and (r["volume"] or 0) > 0),
            "n_traded": sum(1 for r in rows if (r["volume"] or 0) > 0)}


def _sort_key(sort: str):
    if sort == "time_value":
        return lambda r: (r["tv_notional_est"] is None, -(r["tv_notional_est"] or 0.0), -(r["volume"] or 0.0))
    if sort == "vol_oi":
        lo = RULES["vol_oi_min_volume"]
        return lambda r: (r["vol_oi"] is None or (r["volume"] or 0) < lo, -(r["vol_oi"] or 0.0), -(r["volume"] or 0.0))
    return lambda r: (-(r["volume"] or 0.0), -(r["tv_notional_est"] or 0.0))


def select(full: dict, p: dict) -> dict:
    """筛（方向 / 到期日）→ 只留有成交的 → 排 → 截断。汇总按筛完的范围算（不受截断影响）。
    链上没有选中的到期日 ⇒ ParamError(param="expiry")。"""
    if not full.get("data_available"):
        return {k: v for k, v in full.items() if k != "rows"} | {"params": p}
    if p.get("expiry") and p["expiry"] not in full["expiries"]:
        raise ParamError(REASONS["expiry_not_in_chain"] + f"：{p['expiry']}", "expiry")
    scope = [r for r in full["rows"] if (p["side"] == "all" or r["side"] == p["side"])
             and (not p.get("expiry") or r["expiry"] == p["expiry"])]
    summary = _summary(scope)
    traded = sorted((r for r in scope if (r["volume"] or 0) > 0), key=_sort_key(p["sort"]))
    tot = summary["total_volume"]
    top = traded[: p["limit"]]
    out_rows = [dict(r, rank=i + 1, volume_share_pct=round((r["volume"] or 0) / tot * 100, 2) if tot > 0 else None)
                for i, r in enumerate(top)]
    top_vol = sum(r["volume"] or 0.0 for r in top)
    reason = None if traded else "no_volume_today"
    return {
        "data_available": True, "reason": reason, "chain": full["chain"], "expiries": full["expiries"],
        "counts": full["counts"], "params": p, "summary": summary | {
            "n_listed": len(out_rows), "top_volume_share_pct": round(top_vol / tot * 100, 1) if tot > 0 else None},
        "rows": out_rows,
    }


def public_rules() -> dict:
    return {"sorts": SORTS, "sides": SIDES, "defaults": dict(DEFAULTS), "reasons": REASONS,
            "limit_choices": list(RULES["limit_choices"]), "vol_oi_min_volume": RULES["vol_oi_min_volume"],
            "contract_mult": CONTRACT_MULT}
