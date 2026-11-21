"""Alpha Bot「接货」页的数据面（v0.45.721）：接货型卖 put 纸面账本——持仓 / 到期 / 被行权 / covered call / 与两条基准并排。

读什么（全部只读，**本模块不读文件**）
------------------------------------
输入是 `sell_put_wheel.load_state()` 的结果（账本属主的只读出口，由 `alphabot/service.py` 取来传进来）；本模块只做排版前的整理：
把原因码翻成人话、算每笔持仓离到期几天、把已结算的 put 与「限价买入」「一直持有」两条基准并排。不联网、不写任何东西。

⚠️ 它是**测量账本的展示，不是检验**：几只标的、几个月的样本证明不了优势（`experiments/sell_put_wheel_prereg.md`）。页面上
不给任何「推荐」「综合分」；接货价现在是**占位**（现价 × 90%），记录里带 `accept_price_source`，页面照实标出，别让人当成估值判断。

基准口径（预注册 §4，**都在到期那一场的收盘上比，不含 covered call**）：
  · 卖 put 这一腿的盈亏 = 权利金 ×100 ×张数；若被行权，再加（到期收盘 − 行权价）×100 ×张数（接货后按到期收盘记）；
  · 限价买入 = 在同一行权价挂限价买 100 股，开仓后任一天的收盘 ≤ 行权价（只有收盘价，所以是保守的成交判据）就算成交，
    成交则盈亏 = （到期收盘 − 行权价）×100，没成交则 0（钱没动）；
  · 一直持有 = 开仓当天现价买入 100 股，持有到到期，盈亏 = （到期收盘 − 开仓现价）×100。
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

import sell_put_wheel as W

CONTRACT = 100
EVENTS_SHOWN = 60
EQUITY_SHOWN = 60

EVENT_ZH = {
    "put_sold": "卖出 put", "put_expired": "put 作废（权利金落袋）", "put_assigned": "put 被行权（接货）",
    "call_sold": "卖出 covered call", "call_expired": "call 作废（继续持股）", "call_called_away": "call 被叫走（一圈结束）",
}
#: 结算延期的原因码（`options_paper_leg._default_close` / `_resolve_close` 给的）→ 人话；没登记的原样给出，不编
DEFER_ZH = {
    "twelve_data_unconfigured": "没配 Twelve Data 的 key", "bars_unavailable": "没取到日线（限流 / 断网 / 接口错）",
    "lagging": "数据源还没给出那一场的定稿收盘（结算日当晚恒如此）", "bar_missing": "日线里缺那一根",
    "bad_close": "那一根的收盘无效", "no_close": "没拿到收盘", "wrong_session": "拿到的收盘不是结算那一场的",
}
STATE_ZH = {"put": "已卖 put，等到期", "shares": "持股，等卖 call", "shares_call": "持股 + 已卖 call"}


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _days_to(as_of: Optional[str], expiry: Optional[str]) -> Optional[int]:
    try:
        return (date.fromisoformat(str(expiry)[:10]) - date.fromisoformat(str(as_of)[:10])).days
    except (TypeError, ValueError):
        return None


def public_rules() -> dict:
    """页面与帮助页用的规则常量（取自账本属主 `sell_put_wheel.CONFIG`，前端不另抄）。"""
    return W.public_rules()


def _lot_view(lot: dict, as_of: Optional[str], prices: dict) -> dict:
    n = int(lot.get("contracts") or 1)
    out = {"ticker": lot.get("ticker"), "cycle": lot.get("cycle"), "contracts": n,
           "state": lot.get("state"), "opened_on": lot.get("opened_on"),
           "premiums_collected_usd": round((_num(lot.get("premiums")) or 0.0) * CONTRACT * n, 2)}
    sh, put, call = lot.get("shares"), lot.get("put"), lot.get("call")
    out["state_zh"] = STATE_ZH["put"] if put else (STATE_ZH["shares_call"] if call else STATE_ZH["shares"])
    for key, leg in (("put", put), ("call", call)):
        if not leg:
            out[key] = None
            continue
        out[key] = {k: leg.get(k) for k in ("symbol", "strike", "expiry", "premium", "delta", "sold_on", "last_mark",
                                            "last_mark_date", "stale_days", "accept_price", "accept_price_source",
                                            "annual_yield_pct", "dte_at_open", "spot_at_open", "iv_pct", "rv_pct", "iv_rv",
                                            "rule_version", "trigger", "close", "close_session",
                                            "settle_deferred_since", "settle_deferred_reason")}
        out[key]["days_to_expiry"] = _days_to(as_of, leg.get("expiry"))
        out[key]["settle_deferred_reason_zh"] = DEFER_ZH.get(leg.get("settle_deferred_reason"), leg.get("settle_deferred_reason"))
        mark = _num(leg.get("last_mark"))
        out[key]["unrealized_usd"] = round((_num(leg.get("premium")) - mark) * CONTRACT * n, 2) \
            if (mark is not None and _num(leg.get("premium")) is not None) else None
    if sh:
        px = _num(sh.get("last_price")) or _num(prices.get(lot.get("ticker"))) or _num(sh.get("assigned_price"))
        ap = _num(sh.get("assigned_price"))
        out["shares"] = {"n": sh.get("n"), "assigned_price": ap, "assigned_on": sh.get("assigned_on"),
                         "basis": sh.get("basis"), "last_price": px,
                         # v0.45.746：收盘涨到这里（接货价 × (1 + call_min_rise_pct%)）才卖 covered call——门槛只在账本算
                         "call_trigger": W.call_rise_ok(ap, None)[1] if ap is not None else None,
                         "unrealized_usd": round((px - ap) * (sh.get("n") or 0), 2) if (px is not None and ap is not None) else None}
    else:
        out["shares"] = None
    return out


def _benchmarks(events: List[dict], equity: List[dict]) -> List[dict]:
    """已结算的 put 逐笔与两条基准并排（只看到期那一场，不含 covered call）。"""
    opened = {e["cycle"]: e for e in events if e.get("event") == "put_sold"}
    out = []
    for e in events:
        if e.get("event") not in ("put_expired", "put_assigned"):
            continue
        o = opened.get(e.get("cycle"))
        close, strike = _num(e.get("close")), _num(e.get("strike"))
        if o is None or close is None or strike is None:
            continue
        n = int(e.get("contracts") or 1)
        prem = (_num(e.get("premium")) or 0.0) * CONTRACT * n
        assigned = e["event"] == "put_assigned"
        spot0 = _num(o.get("spot"))
        wheel = prem + ((close - strike) * CONTRACT * n if assigned else 0.0)
        closes = [_num((r.get("prices") or {}).get(e["ticker"])) for r in equity
                  if o["date"] <= r.get("date", "") <= e.get("expiry", "9999")]
        filled = close <= strike or any(c is not None and c <= strike for c in closes)
        limit_buy = (close - strike) * CONTRACT * n if filled else 0.0
        hold = (close - spot0) * CONTRACT * n if spot0 is not None else None
        out.append({"ticker": e["ticker"], "cycle": e["cycle"], "opened": o["date"], "expiry": e.get("expiry"),
                    "strike": strike, "premium": _num(e.get("premium")), "close_at_expiry": close,
                    "spot_at_open": spot0, "outcome": "assigned" if assigned else "expired",
                    "wheel_put_pnl_usd": round(wheel, 2), "limit_buy_filled": filled,
                    "limit_buy_pnl_usd": round(limit_buy, 2),
                    "hold_pnl_usd": round(hold, 2) if hold is not None else None})
    return sorted(out, key=lambda r: r["expiry"] or "", reverse=True)


def build_overview(state: dict, *, rules: Optional[dict] = None) -> dict:
    """页面的全部数据。账本目录不存在 ⇒ available False（说「账本还没开」，不说成「账本为空」）。"""
    if not state.get("available"):
        return {"available": False, "state_reason": state.get("state_reason") or state.get("reason"), "rules": rules or {}}
    meta, equity, events = state.get("meta") or {}, state.get("equity") or [], state.get("events") or []
    last = equity[-1] if equity else {}
    as_of = meta.get("last_run_date") or last.get("date")
    prices = last.get("prices") or {}
    start = _num(meta.get("starting_capital"))
    nav = _num(last.get("nav"))
    run = meta.get("last_run") or {}
    skipped = [{**s, "reason_zh": W.SKIP_REASONS.get(s.get("reason"), s.get("reason"))} for s in run.get("skipped") or []]
    return {
        "available": True, "as_of": as_of, "rules": rules or {},
        "account": {"nav": nav, "starting_capital": start,
                    "return_pct": round((nav / start - 1) * 100, 3) if (nav is not None and start) else None,
                    "cash": _num(meta.get("cash")), "reserved_puts": last.get("reserved_puts"),
                    "shares_value": last.get("shares_value"), "liability": last.get("liability"), "n_lots": last.get("n_lots")},
        "lots": [_lot_view(l, as_of, prices) for l in state.get("lots") or []],
        "events": [{**e, "event_zh": EVENT_ZH.get(e.get("event"), e.get("event"))}
                   for e in sorted(events, key=lambda e: (e.get("date", ""), e.get("ticker", "")), reverse=True)[:EVENTS_SHOWN]],
        "benchmarks": _benchmarks(events, equity),
        "skipped": skipped,
        "deferred": [{**x, "reason_zh": DEFER_ZH.get(x.get("reason"), x.get("reason"))} for x in run.get("deferred") or []],
        "chain_errors": run.get("chain_errors") or {},
        "equity": [{k: r.get(k) for k in ("date", "nav", "cash", "n_lots")} for r in equity[-EQUITY_SHOWN:]],
        "kpis": W.kpis(state),
    }


# ── 此刻浮动（v0.45.801）：账本持仓 × 现取的 CBOE 报价 —— 只显示，不写账本、不改任何开仓 / 结算决定 ─────────────────

#: 原因码 → 人话（某条腿 / 某笔持股此刻算不出来的原因；`quote_held` 自己的原因码另由 `QUOTE_REASON_ZH` 翻）
LIVE_REASONS = {
    "no_quote": "这只票的报价没取到", "contract_missing": "报价文件里没有这张合约",
    "no_mid": "没有合格双边报价（bid 为 0 或 ask < bid），与账本盯市同一判据", "no_price": "报价里没有标的现价",
    "expired_unsettled": "已过到期日、等下一次扫描按到期那一场的官方收盘结算（合约已不在报价文件里）",
}
QUOTE_REASON_ZH = {
    "stale_vintage": "CBOE 的文件还停在上一个交易日", "payload_unavailable": "CBOE 没返回期权链",
    "vintage_unverifiable": "判断不了报价是哪一天的", "price_unavailable": "报价里没有标的现价",
    "snapshot_mode": "快照模式没有此刻报价", "no_symbols": "没有要报价的合约",
}


def _leg_live(leg: Optional[dict], q: Optional[dict], quote_ok: bool, n: int, today: Optional[str] = None) -> Optional[dict]:
    if not leg:
        return None
    prem, mark = _num(leg.get("premium")), _num(leg.get("last_mark"))
    # 过了到期日还挂在账本里 = 等结算（账本到期日当天不结、次一个扫描日才结，周末 / 扫描失败会挂好几天）：
    # 合约已从报价文件消失，别报成「文件里没有这张合约」；也不拿此刻现价估内在价值（结算按到期那一场的收盘）
    expired = bool(today and leg.get("expiry") and str(leg["expiry"]) < today)
    c = ((q or {}).get("quotes") or {}).get(leg.get("symbol")) if (quote_ok and not expired) else None
    mid = W.mark_of(c)
    reason = None if mid is not None else ("expired_unsettled" if expired else
                                           "no_quote" if not quote_ok else ("contract_missing" if c is None else "no_mid"))
    out = {"symbol": leg.get("symbol"), "strike": _num(leg.get("strike")), "expiry": leg.get("expiry"),
           "premium": prem, "close_mark": mark, "close_mark_date": leg.get("last_mark_date"),
           "bid": _num((c or {}).get("bid")), "ask": _num((c or {}).get("ask")), "mid": mid, "reason": reason,
           "close_unrealized_usd": round((prem - mark) * CONTRACT * n, 2) if (prem is not None and mark is not None) else None}
    out["unrealized_usd"] = round((prem - mid) * CONTRACT * n, 2) if (mid is not None and prem is not None) else None
    out["since_close_usd"] = round((mark - mid) * CONTRACT * n, 2) if (mid is not None and mark is not None) else None
    return out


def live_view(state: dict, quotes: Dict[str, dict], *, today: Optional[str] = None) -> dict:
    """账本（`sell_put_wheel.load_state()`）× 每只票一份 `cboe_options.quote_held` 结果 → 此刻浮动。纯函数。

    口径与账本收盘那一遍相同（`sell_put_wheel._nav_estimate`）：净值 = 现金 + 持股 × 现价 − 未到期 put / call × mid × 100 × 张数；
    mid 用账本同一个判据 `mark_of`。某条腿 / 某笔持股此刻算不出来 ⇒ 那一行写原因、**合计净值不给**（不拿收盘 mark 顶上）。"""
    if not state.get("available"):
        return {"available": False, "state_reason": state.get("state_reason")}
    meta, equity = state.get("meta") or {}, state.get("equity") or []
    last = equity[-1] if equity else {}
    cash, close_nav, start = _num(meta.get("cash")), _num(last.get("nav")), _num(meta.get("starting_capital"))
    rows, missing = [], []
    shares_value = liability = 0.0
    for l in state.get("lots") or []:
        tk, n = l.get("ticker"), int(l.get("contracts") or 1)
        q = (quotes or {}).get(tk)
        quote_ok = bool((q or {}).get("available"))
        row = {"ticker": tk, "cycle": l.get("cycle"), "contracts": n, "state": l.get("state"),
               "quote_reason": None if quote_ok else ((q or {}).get("reason") or "no_quote"),
               "put": _leg_live(l.get("put"), q, quote_ok, n, today), "call": _leg_live(l.get("call"), q, quote_ok, n, today),
               "shares": None}
        for key in ("put", "call"):
            leg = row[key]
            if leg is None:
                continue
            if leg["mid"] is None:
                missing.append(f"{tk} {key}")
            else:
                liability += leg["mid"] * CONTRACT * n
        sh = l.get("shares")
        if sh:
            ap, cnt, close_px = _num(sh.get("assigned_price")), int(sh.get("n") or 0), _num(sh.get("last_price"))
            px = _num((q or {}).get("underlying_price")) if quote_ok else None
            row["shares"] = {"n": cnt, "assigned_price": ap, "close_price": close_px, "price": px,
                             "reason": None if px is not None else ("no_quote" if not quote_ok else "no_price"),
                             "unrealized_usd": round((px - ap) * cnt, 2) if (px is not None and ap is not None) else None,
                             "since_close_usd": round((px - close_px) * cnt, 2) if (px is not None and close_px is not None) else None}
            if px is None:
                missing.append(f"{tk} 持股")
            else:
                shares_value += px * cnt
        row["unrealized_usd"] = _sum_or_none([row[k]["unrealized_usd"] for k in ("put", "call", "shares") if row[k]])
        row["since_close_usd"] = _sum_or_none([row[k]["since_close_usd"] for k in ("put", "call", "shares") if row[k]])
        rows.append(row)
    nav = round(cash + shares_value - liability, 2) if (cash is not None and not missing) else None
    sessions = {tk: {k: (q or {}).get(k) for k in ("available", "reason", "session_live", "vintage_date",
                                                     "payload_last_trade_time", "underlying_price", "expected_date")}
                for tk, q in (quotes or {}).items()}
    return {"available": True, "as_of": meta.get("last_run_date") or last.get("date"), "rows": rows, "missing": missing,
            "nav": nav, "close_nav": close_nav, "starting_capital": start,
            "return_pct": round((nav / start - 1) * 100, 3) if (nav is not None and start) else None,
            "since_close_usd": round(nav - close_nav, 2) if (nav is not None and close_nav is not None) else None,
            "unrealized_usd": _sum_or_none([r["unrealized_usd"] for r in rows]) if rows else 0.0,
            "quotes": sessions, "reasons": LIVE_REASONS, "quote_reasons": QUOTE_REASON_ZH}


def live_symbols(state: dict) -> Dict[str, List[str]]:
    """每只票要报价的合约（未到期的 put / call 的 OCC 符号）；只有持股的票给空表（只要标的现价）。纯函数。"""
    out: Dict[str, List[str]] = {}
    for l in (state.get("lots") or []) if state.get("available") else []:
        tk = l.get("ticker")
        if not tk:
            continue
        syms = out.setdefault(tk, [])
        for key in ("put", "call"):
            s = (l.get(key) or {}).get("symbol")
            if s and s not in syms:
                syms.append(s)
    return out


def _sum_or_none(vals) -> Optional[float]:
    vals = list(vals)
    return round(sum(vals), 2) if vals and all(v is not None for v in vals) else None
