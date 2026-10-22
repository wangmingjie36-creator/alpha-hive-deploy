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
#: 逐票 Delta 对冲（v0.45.526）给页面的字段
DELTA_NAME_KEYS = ("ticker", "status", "reason", "option_dd", "held_shares", "target_shares", "trade_shares", "action",
                   "net_dd", "net_pct_nav", "price", "price_session", "price_source", "price_at_close", "breach",
                   "traded_today")
DELTA_TRADE_KEYS = ("date", "ticker", "shares", "price", "action", "reason", "shares_after", "net_dd_before",
                    "target_shares", "price_source", "price_session", "fill")
DELTA_TRADES_SHOWN = 60
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
        # 被 hedge_exclude_tickers 挡在对冲外的标的与估算敞口（v0.45.545）：页面要说出来，不能让排除变成看不见
        "excluded_from_hedge": [{k: x.get(k) for k in ("ticker", "n_stock_positions", "n_straddles",
                                                       "stock_usd_est", "stock_pct_nav_est", "basis")}
                                for x in ((a.get("excluded_from_hedge") or {}).get("names") or [])
                                if isinstance(x, dict)],
        "spy_price": _num(a.get("spy_price")),
        "spy_mark": a.get("spy_mark"),
        # 审计文件里的 price_check 没有 error 键：异常中断的那一轮根本不写审计（审计最后写），页面靠「最近审计不是
        # 账本最后一天」/ 陈旧来报它（v0.45.459 二次检查：初版读一个永远不存在的 error，那条黄条恒不亮）
        # stale / unpriced 点名到标的（v0.45.526）：此前页面只说「1 price missing」，不说是哪一只
        # bid_zero（v0.45.620）：买价为 0 的腿用的是 CBOE 模型 Greeks、没有 mid，已计入带位——不是缺，但要让人看得见
        "price_check": {"n_stale": pc.get("n_stale"), "gaps": pc.get("gaps") or [],
                        "n_bid_zero": pc.get("n_bid_zero") or 0, "bid_zero": list(pc.get("bid_zero") or []),
                        "hedge_undecided": pc.get("hedge_undecided"),
                        "stale": [{k: x.get(k) for k in ("ticker", "kind", "seen_price", "session", "source")}
                                  for x in pc.get("stale") or [] if isinstance(x, dict)],
                        "unpriced": [{k: x.get(k) for k in ("ticker", "kind", "source")}
                                     for x in pc.get("unpriced") or [] if isinstance(x, dict)]},
    }


def _pct_nav(dd, nav) -> Optional[float]:
    d, n = _num(dd), _num(nav)
    return round(d / n * 100.0, 4) if (d is not None and n) else None


def delta_overview(view: dict, audits: dict) -> dict:
    """逐票 Delta 对冲面板的数据（v0.45.526）。纯函数，只读。
    决策取**最近一份放行审计**里的 `delta_hedge` 一节（shadow 下这就是全部：没有持仓账）；
    持仓 / 成交 / 净值只在开过账（paper）后才有，成交 / 净值同样经历史闸（`history["delta"]`）。"""
    latest_day, sec = None, None
    for day in sorted(audits or {}):
        s = (audits[day] or {}).get("delta_hedge")
        if isinstance(s, dict):
            latest_day, sec = day, s
    hist = (view.get("history") or {}).get("delta") or {}
    rules = view.get("rules") or {}
    errors = {k: v for k, v in (view.get("errors") or {}).items() if k.startswith("delta_")}
    meta = view.get("delta_meta") if isinstance(view.get("delta_meta"), dict) else None
    trades = sorted(hist.get("trades") or [], key=lambda t: str(t.get("date")))
    excluded = hist.get("excluded") or {}
    return {
        "mode": (sec or {}).get("mode") or rules.get("delta_hedge_mode"),
        "available": sec is not None, "latest_date": latest_day,
        "band_pct": (sec or {}).get("band_pct", rules.get("delta_hedge_band_pct")),
        "rebalance_to": (sec or {}).get("rebalance_to", rules.get("delta_hedge_rebalance_to")),
        "nav": _num((sec or {}).get("nav")),
        # 带宽分母的口径（0.45.529）：full = 合并 NAV；excluding_unmarked_delta_overlay = 不含缺价持仓标的的那块盈亏
        "nav_basis": (sec or {}).get("nav_basis"), "nav_unmarked": (sec or {}).get("nav_unmarked") or [],
        "names": [{**{k: n.get(k) for k in DELTA_NAME_KEYS}, "option_pct_nav": _pct_nav(n.get("option_dd"), (sec or {}).get("nav"))}
                  for n in (sec or {}).get("names") or [] if isinstance(n, dict)],
        "n_hedge": (sec or {}).get("n_hedge"), "n_inside": (sec or {}).get("n_inside"),
        "n_undecided": (sec or {}).get("n_undecided"), "undecided": (sec or {}).get("undecided") or [],
        "blocked": (sec or {}).get("blocked") or {},
        "opened": bool(view.get("delta_opened")),
        "cash": _num((meta or {}).get("cash")), "starting_date": (meta or {}).get("starting_date"),
        "positions": [{k: p.get(k) for k in ("ticker", "shares", "avg_price", "last_price", "last_mark_date",
                                              "entry_date")}
                      for p in (view.get("delta_positions") or []) if isinstance(p, dict)],
        "trades": [{k: t.get(k) for k in DELTA_TRADE_KEYS} for t in reversed(trades)][:DELTA_TRADES_SHOWN],
        "equity": [{k: e.get(k) for k in ("date", "nav", "cash", "market_value", "n_positions", "trades_today")}
                   for e in hist.get("equity") or []],
        "gate": {"excluded": excluded, "excluded_total": {k: sum((v or {}).values()) for k, v in excluded.items()}},
        "errors": errors,
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


def _identity_reason(acct: dict, errors: dict, opened: bool) -> Optional[str]:
    """账目核对做不了时的真实原因（v0.45.459 二次检查：初版一律写「找不到同一天的放行净值行」，持仓读不了时也这么说）。"""
    if acct.get("identity_ok") is not None:
        return None
    if errors.get("positions"):
        return "持仓文件读不了"
    if errors.get("meta"):
        return "meta.json 读不了"
    if not opened:
        return "账本还没有记录"
    if acct.get("shares") == 0 and not acct.get("mark_date"):
        return "空仓，没有标记日"
    if acct.get("nav") is None:
        return "净值算不出来（缺现金或标记价）"
    return f"找不到 {acct.get('mark_date')} 的放行净值行"


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
    errors = base["errors"]
    opened = bool(view.get("opened", view.get("meta") is not None))
    as_of = acct.get("last_run_date")
    acct["identity_reason"] = _identity_reason(acct, errors, opened)
    # 持仓标记停在账本最后一天之前：那天以后 SPY 价取不到（生产 09-24 / 09-25 实有其事），市值 / 净值 / 浮动按旧价算
    acct["mark_stale"] = bool(acct.get("mark_date") and as_of and acct["mark_date"] < as_of)
    expected, cal = SD.expected_ledger_date(now_et)
    excluded = hist.get("excluded") or {}
    return {
        **base, "data_available": True, "opened": opened, "as_of": as_of,
        # 账本日期不可得（meta 读不了 / 还没开账）⇒ stale 为 None（判断不了），不是 False（没陈旧）
        "freshness": {"as_of": as_of, "expected_as_of": expected, "calendar": cal,
                      "stale": (as_of < expected) if as_of else None},
        "account": acct,
        "latest": days[-1] if days else None,
        # 最近一份放行的审计不是账本最后一天 ⇒ 那天的审计缺了或被拦了，页面要说。两边有一边不可得 ⇒ None
        "latest_is_as_of": (days[-1]["date"] == as_of) if (days and as_of) else None,
        "days": days,
        "delta": delta_overview(view, audits),
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
