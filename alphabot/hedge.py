"""Alpha Bot「对冲」页的数据面（v0.45.459）：组合 Greeks 的纸面 SPY 覆盖账本——持仓 / 现金 / 成交 / 净值 / 每日带位。

读什么（全部只读）
------------------
**只**经 `portfolio_greeks.ledger_view(root)`（账本属主提供的只读视图），本模块不碰账本文件名：
  · 历史（成交 / 净值 / 每日审计）走 `portfolio_greeks.load_history()`——只放行能自证「价属于那一场收盘」的记录。
    2026-09-04~10-07 的记录标的价 / SPY 成交价晚一个交易日、且自己不说（v0.45.423），按用户决定原样保留、不重算；
    这里**不显示**它们，只给被拦下的条数与日期范围（守卫 `tests/test_hedge_history_gate.py`：别处不许绕过它直读）。
  · 当前持仓与现金是「现在」不是历史，`ledger_view` 严格读；读不了就如实报，不当成空仓。

口径
----
起始现金 0，所以 **对冲腿净值 = 现金 + SPY 市值 = 开账以来的累计盈亏**（不是规模）。浮动 = (标记价 − 均价) × 股数，
已实现 = 净值 − 浮动。标记价是扫描日收盘后的 SPY 收盘价（账本自己的 `last_price`），本页不现拉行情。
账目核对：持仓文件算出的净值必须等于同一天净值行里的 `nav`（两个文件由同一轮扫描写出），对不上就报。
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from alphabot import straddle as SD

#: 审计文件里给页面的字段（逐行明细 `rows` 不给：页面只看组合层）
AUDIT_AGG_KEYS = ("band", "band_status", "pct_nav", "sums", "coverage", "partial", "vega_alert", "gamma_alert", "nav")
TRADE_KEYS = ("date", "action", "shares", "price", "price_source", "price_session", "beta_dd_pct_before",
              "beta_dd_usd_before", "target_usd", "excess_usd", "shares_after", "nav", "reason", "fill")
#: 两个文件的净值差在这以内算对得上（各自四舍五入到分）
IDENTITY_TOL_USD = 0.05


def _num(v) -> Optional[float]:
    return SD._num(v)


def _range(dates: List[str]) -> Optional[List[str]]:
    ds = sorted(d for d in dates or [] if d and d != "?")
    return [ds[0], ds[-1]] if ds else None


def audit_brief(day: str, a: dict) -> dict:
    """单日审计 → 页面要的组合层摘要。`after` = 对冲成交之后的带位（没成交时与成交前相同）。"""
    agg = a.get("aggregate") or {}
    after = a.get("aggregate_after") or {}
    pc = a.get("price_check") or {}
    rec = a.get("recommendation") or {}
    trade = a.get("executed_trade") if a.get("executed") else None
    return {
        "date": day,
        "aggregate": {k: agg.get(k) for k in AUDIT_AGG_KEYS},
        "beta_dd_pct": (agg.get("pct_nav") or {}).get("beta_dollar_delta"),
        "beta_dd_pct_after": (after.get("pct_nav") or {}).get("beta_dollar_delta") if after else None,
        "band_status_after": after.get("band_status") if after else None,
        "recommendation": {k: rec.get(k) for k in ("action", "spy_shares", "reason", "target_pct", "excess_usd")},
        "executed": bool(a.get("executed")),
        "executed_shares": _num((trade or {}).get("shares")),
        "execution_blocked": a.get("execution_blocked"),
        "spy_price": _num(a.get("spy_price")),
        "spy_mark": a.get("spy_mark"),
        "price_check": {"n_stale": pc.get("n_stale"), "gaps": pc.get("gaps") or [], "error": pc.get("error"),
                        "hedge_undecided": pc.get("hedge_undecided")},
    }


def account(positions: Optional[list], meta: Optional[dict], equity: List[dict]) -> dict:
    """持仓 + 现金 → 账户卡片；并与同一天的净值行核对。缺项 ⇒ None（不把缺的当 0）。"""
    pos = next((p for p in positions or [] if isinstance(p, dict) and p.get("ticker")), None)
    shares = _num((pos or {}).get("shares")) if pos else (0.0 if positions == [] else None)
    avg, mark = _num((pos or {}).get("avg_price")), _num((pos or {}).get("last_price"))
    cash = _num((meta or {}).get("cash"))
    mv = shares * mark if (shares is not None and mark is not None) else (0.0 if shares == 0 else None)
    nav = cash + mv if (cash is not None and mv is not None) else None
    unreal = (mark - avg) * shares if (shares and mark is not None and avg is not None) else (0.0 if shares == 0 else None)
    mark_date = (pos or {}).get("last_mark_date")
    row = next((e for e in reversed(equity or []) if e.get("date") == mark_date), None) if mark_date else None
    row_nav = _num((row or {}).get("nav"))
    gap = (nav - row_nav) if (nav is not None and row_nav is not None) else None
    return {
        "ticker": (pos or {}).get("ticker"), "shares": shares, "avg_price": avg, "mark": mark,
        "mark_date": mark_date, "entry_date": (pos or {}).get("entry_date"),
        "market_value": mv, "cash": cash, "nav": nav, "unrealized_usd": unreal,
        "realized_usd": (nav - unreal) if (nav is not None and unreal is not None) else None,
        "starting_date": (meta or {}).get("starting_date"), "last_run_date": (meta or {}).get("last_run_date"),
        "ledger_version": (meta or {}).get("version"),
        # 核对：持仓文件算出的净值 vs 同一天净值行（两者同一轮扫描写出）。找不到那一天的放行净值行 ⇒ None（核不了）
        "identity_checked_against": mark_date if row else None,
        "identity_gap_usd": round(gap, 2) if gap is not None else None,
        "identity_ok": (abs(gap) <= IDENTITY_TOL_USD) if gap is not None else None,
    }


def build_overview(view: dict, *, now_et: datetime) -> dict:
    """`portfolio_greeks.ledger_view()`（或演示数据，同形）→ 页面数据。纯函数。"""
    base = {"source": view.get("source", "ledger"), "now_et": now_et.isoformat(timespec="seconds"),
            "state_dir": {"path": view.get("state_dir"), "exists": bool(view.get("exists"))},
            "rules": view.get("rules") or {}, "measurement_start": view.get("measurement_start"),
            "errors": dict(view.get("errors") or {})}
    if not view.get("exists"):
        return {**base, "data_available": False}
    hist = view.get("history") or {}
    equity = sorted(hist.get("equity") or [], key=lambda e: str(e.get("date")))
    trades = sorted(hist.get("trades") or [], key=lambda t: str(t.get("date")))
    audits = hist.get("audits") or {}
    days = [audit_brief(d, audits[d]) for d in sorted(audits)]
    acct = account(view.get("positions"), view.get("meta"), equity)
    as_of = acct.get("last_run_date")
    expected, cal = SD.expected_ledger_date(now_et)
    excluded = hist.get("excluded") or {}
    return {
        **base, "data_available": True, "as_of": as_of,
        "freshness": {"as_of": as_of, "expected_as_of": expected, "calendar": cal,
                      "stale": bool(as_of and as_of < expected)},
        "account": acct,
        "latest": days[-1] if days else None,
        # 最近一份放行的审计不是账本最后一天 ⇒ 那天的审计缺了或被拦了，页面要说
        "latest_is_as_of": bool(days and days[-1]["date"] == as_of),
        "days": days,
        "equity": [{k: e.get(k) for k in ("date", "nav", "cash", "market_value", "spy_shares", "spy_price",
                                          "trades_today", "band_status")} for e in equity],
        "trades": [{k: t.get(k) for k in TRADE_KEYS} for t in reversed(trades)],      # 新的在前
        "gate": {
            "measurement_start": hist.get("measurement_start") or view.get("measurement_start"),
            "first_verified": hist.get("first_verified") or {},
            "excluded": excluded,
            "excluded_total": {k: sum((v or {}).values()) for k, v in excluded.items()},
            "excluded_range": {k: _range(v) for k, v in (hist.get("excluded_dates") or {}).items()},
            "regressions": [list(x) for x in hist.get("regressions") or []],
            "before_measurement_start": [list(x) for x in hist.get("before_measurement_start") or []],
        },
    }
