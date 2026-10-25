"""组合尾部预算（v0.45.649）：压力最差格告警 + 逐标的 / 板块尾部贡献表 + 卖跨式「若按预算会不会跳过」影子记录。

**纯函数**：不读写文件、不联网、不 import 账本 / 对冲模块；压力函数（`portfolio_greeks.stress_table`）与板块函数由调用方注入。
接线在 `portfolio_greeks.compute_day`（它是唯一同时看得到股票账、跨式账、SPY 覆盖三本账的地方）；结果进当天审计文件
`greeks_<日期>.json` 的 `tail` 一节、`price_check.tail` 简报（→ `scan_timing` → `alert_manager`），Alpha Bot 对冲页只读显示。

为什么要这个（2026-10-25 查证，2026-10-23 审计、NAV $144,857）
------------------------------------------------------------
组合的压力最差格（现货 −10%、IV +20 点）约 −23% NAV，其中**卖跨式腿 −24.7%、股票账 −0.2%、SPY 覆盖 +1.8%**——尾部几乎全部来自那
6 笔卖跨式（净空 Gamma / Vega，双向都亏），XOM + CVX 两个能源股合计约 −10.5%。而现有的 Gamma / Vega 告警按「1% 移动」标度
（gamma 线 0.5% NAV / 1%）：损失随移动幅度**平方**放大，10% 的移动要损失约 50% NAV 才响——当天实测 −23% 时两条告警都是静音的。

三件事（全部**只观测，不改任何交易行为**）
-----------------------------------------
1. **压力告警**：全组合压力网格最差格损失 > `CONFIG["alert_pct"]`% NAV ⇒ `breach`（alert_manager 报 P2）。
2. **尾部贡献表**：每个标的（及 SPY 覆盖）在两个参考格——全网格最差格、下行 / 平盘侧最差格——上贡献多少，按标的与板块汇总；
   各标的贡献之和 = 全组合该格损益（压力表逐行独立可加；残差 `residual_usd` 单列，不抹平）。
3. **影子闸**：今天新开的每笔卖跨式，按**当天实际的账**（股票账 + SPY 覆盖 + 此前已开的跨式）试算：加上它之后最差格损失会不会超过
   各档预算（`shadow_budgets_pct`）⇒ 记 `would_skip`。**不真跳过**——真闸会改变哪些单开出来，而宽跨影子预注册
   （`experiments/straddle_strangle_shadow_prereg.md`，起点 2026-10-21）的单位就是真开出来的仓。影子闸攒够真实开仓当天的数据后，
   再决定要不要真拦截。

⚠️ 诚实边界
-----------
· **预算数值是主观的**（15% / 20% / 告警 20%），没有任何证据说该是多少——见 `CONFIG`。
· **影子闸不是反事实回测**：它回答「给定当天实际已有的账，这一笔会不会被挡」，**不**模拟「前一笔被挡后，后一笔就放得进来」。
· **只在 `portfolio_greeks` 的每日运行里评估**（入场当天收盘后，用当天标记价），不在 `options_paper_leg` 的入场代码里：那里只看得到
  跨式账自己，看不到股票账与 SPY 覆盖，没法算「组合最差格」；硬塞要让账本 import 对冲模块并在入场时给所有持仓重新报价。
  代价：评估用的是当天收盘标记价，不是入场那一刻的报价；入场当天的审计没跑成 ⇒ 这笔没有记录（`n_unevaluated`，不补记）。
· 压力网格的口径同 `portfolio_greeks.stress_table`：IV 逐合约平行平移、瞬时冲击、不建模 skew；SPY 覆盖按**审计当天成交前**的持仓。
· 被剔除的行（缺价 / 缺报价 / 已到期）不进压力表 ⇒ 最差格可能被**低估**，`partial` 照实带出，不当没发生。
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

VERSION = "tail_budget_v1"

#: 全是**主观常数、未经检验**。改任何一项 ⇒ 告警 / 影子记录的含义变了，CHANGELOG 要写；影子记录按 `version` 分代。
CONFIG: Dict[str, object] = {
    "alert_pct": 20.0,                      # 全组合压力最差格损失 > 它（% NAV）⇒ 告警（P2）
    "shadow_budgets_pct": (15.0, 20.0),     # 影子闸的两档预算（% NAV）
    "top_n": 3,                             # 告警简报里点名的标的个数
}
UNCLASSIFIED = "未分类"
HEDGE_LABEL = "SPY 覆盖"
KINDS = ("stock", "option", "hedge", "delta_hedge")

#: 原因码唯一一张表（`tests/test_tail_budget.py` 双向核对：每个写出的码都登记、表里无死码）
REASONS: Dict[str, str] = {
    "nav_unavailable": "NAV 不可得，百分比算不出来",
    "stress_unavailable": "压力网格不可得（没有任何格子）",
    "no_grid_for_ticker": "这只标的的跨式腿没有进压力表（缺价 / 缺报价 / 已到期被剔除），没法试算",
    "not_in_book_order": "这只标的不在今天的跨式持仓里（持仓文件与审计行对不上）",
}


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, dp: int = 2) -> Optional[float]:
    f = _num(v)
    return None if f is None else round(f, dp)


def _cells(stress: Optional[dict]) -> Dict[Tuple[float, float], float]:
    out: Dict[Tuple[float, float], float] = {}
    for c in (stress or {}).get("cells") or []:
        sp, iv, pnl = _num(c.get("spot_pct")), _num(c.get("iv_pts")), _num(c.get("pnl"))
        if sp is not None and iv is not None and pnl is not None:
            out[(sp, iv)] = pnl
    return out


def _add(a: Dict, b: Dict) -> Dict:
    return {k: a.get(k, 0.0) + b.get(k, 0.0) for k in set(a) | set(b)}


def _worst(cells: Dict) -> Optional[Tuple[Tuple[float, float], float]]:
    if not cells:
        return None
    k = min(cells, key=lambda x: (cells[x], x))
    return k, cells[k]


def _worst_down(cells: Dict) -> Optional[Tuple[Tuple[float, float], float]]:
    """下行 / 平盘侧（现货冲击 ≤ 0）最差格：认沽结构与「下跌」叙事看的那一半（0% 列是纯 IV 冲击）。"""
    return _worst({k: v for k, v in cells.items() if k[0] <= 0})


def _loss_pct(cells: Dict, nav: float) -> float:
    """最差格损失（% NAV，正数；没有亏损格 ⇒ 0）。"""
    w = _worst(cells)
    return max(0.0, -w[1]) / nav * 100.0 if w else 0.0


def _cell(k: Optional[Tuple[float, float]], pnl: Optional[float], nav: float) -> Optional[dict]:
    if k is None:
        return None
    return {"spot_pct": k[0], "iv_pts": k[1], "pnl": _r(pnl), "pct_nav": _r(pnl / nav * 100.0, 2)}


def _bucket_key(r: dict) -> Tuple[str, str]:
    kind = str(r.get("kind") or "")
    return str(r.get("ticker") or "?"), kind


def attribution(rows: Sequence[dict], spy_price: Optional[float], nav: Optional[float],
                stress_fn: Callable[[List[dict], Optional[float]], dict],
                sector_of: Callable[[str], Optional[str]]) -> dict:
    """全组合压力最差格 + 逐标的 / 板块的贡献。`stress_fn(rows, spy_price)` ⇒ `stress_table` 形状的 dict。

    每个 (标的, 种类) 桶各算一次压力表再按格相加——压力表逐行独立，所以各桶之和 = 全组合；
    `residual_usd` = 全组合最差格损益 − 各标的在该格贡献之和（理论上 0，只可能是四舍五入；不为 0 就是有行被两边不同对待）。
    """
    nav_f = _num(nav)
    if not nav_f or nav_f <= 0:
        return {"available": False, "reason": "nav_unavailable", "reason_zh": REASONS["nav_unavailable"]}
    total_t = stress_fn(list(rows), spy_price)
    total = _cells(total_t)
    w, wd = _worst(total), _worst_down(total)
    if w is None:
        return {"available": False, "reason": "stress_unavailable", "reason_zh": REASONS["stress_unavailable"]}
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for r in rows:
        groups.setdefault(_bucket_key(r), []).append(r)
    by_ticker: Dict[str, dict] = {}
    option_cells: Dict[str, Dict] = {}
    for (tk, kind), sub in groups.items():
        c = _cells(stress_fn(list(sub), spy_price))
        if kind == "option":
            option_cells[tk] = c
        ent = by_ticker.setdefault(tk, {"cells": {}, "by_kind": {}, "n_rows": 0})
        ent["cells"] = _add(ent["cells"], c)
        ent["by_kind"][kind] = {"at_worst": _r(c.get(w[0], 0.0)), "at_downside": _r(c.get(wd[0], 0.0)) if wd else None}
        ent["n_rows"] += len(sub)
    out_t: List[dict] = []
    for tk, ent in by_ticker.items():
        cc = ent["cells"]
        ow = _worst(cc)
        is_hedge = set(ent["by_kind"]) == {"hedge"}
        sector = HEDGE_LABEL if is_hedge else (sector_of(tk) or UNCLASSIFIED)
        at_w, at_d = cc.get(w[0], 0.0), (cc.get(wd[0], 0.0) if wd else None)
        out_t.append({
            "ticker": tk, "sector": sector, "n_rows": ent["n_rows"],
            "at_worst": _r(at_w), "at_worst_pct": _r(at_w / nav_f * 100.0, 2),
            "at_downside": _r(at_d), "at_downside_pct": _r(at_d / nav_f * 100.0, 2) if at_d is not None else None,
            "own_worst": _cell(ow[0], ow[1], nav_f) if ow else None,
            "by_kind": ent["by_kind"],
        })
    out_t.sort(key=lambda x: (x["at_worst"], x["ticker"]))
    sectors: Dict[str, dict] = {}
    for t in out_t:
        s = sectors.setdefault(t["sector"], {"sector": t["sector"], "tickers": [], "at_worst": 0.0, "at_downside": 0.0})
        s["tickers"].append(t["ticker"])
        s["at_worst"] += t["at_worst"] or 0.0
        s["at_downside"] += t["at_downside"] or 0.0
    out_s = sorted(({**s, "at_worst": _r(s["at_worst"]), "at_worst_pct": _r(s["at_worst"] / nav_f * 100.0, 2),
                     "at_downside": _r(s["at_downside"]), "at_downside_pct": _r(s["at_downside"] / nav_f * 100.0, 2)}
                    for s in sectors.values()), key=lambda s: (s["at_worst"], s["sector"]))
    residual = w[1] - sum(t["at_worst"] or 0.0 for t in out_t)
    return {"available": True, "reason": None, "version": VERSION, "nav": _r(nav_f),
            "worst": _cell(w[0], w[1], nav_f), "downside_worst": _cell(wd[0], wd[1], nav_f) if wd else None,
            "loss_pct_nav": _r(_loss_pct(total, nav_f), 2),
            "partial": bool(total_t.get("partial")), "excluded": list(total_t.get("excluded") or [])[:8],
            "by_ticker": out_t, "by_sector": out_s, "residual_usd": _r(residual, 2),
            "_option_cells": option_cells}


def _budget_key(b: float) -> str:
    return f"{b:g}"


def gate_shadow(attr: dict, rest_cells: Dict, order: Sequence[str], entries: Sequence[dict], nav: float,
                budgets: Optional[Sequence[float]] = None) -> dict:
    """今天新开的每笔跨式：加上它之后组合最差格损失超没超各档预算（**只记录，不跳过**）。

    `order`：今天的跨式持仓标的，按持仓文件顺序（= 账本开仓顺序）；`entries`：今天新开的 `{ticker, side, entry_date}`；
    `rest_cells`：非跨式行（股票账 + SPY 覆盖 + 逐票对冲）的压力格；`attr["_option_cells"]`：逐标的跨式腿压力格。
    试算的账 = rest + 此前已开的全部跨式（实际存在的，含同日更早的）+ 这一笔。多头跨式（买入）只增加凸性、不适用这个闸
    ⇒ `applies=False`、`would_skip` 全 False。
    """
    budgets = tuple(budgets if budgets is not None else CONFIG["shadow_budgets_pct"])
    per = attr.get("_option_cells") or {}
    out: List[dict] = []
    for e in entries:
        tk, side = str(e.get("ticker")), str(e.get("side") or "")
        rec = {"ticker": tk, "side": side, "entry_date": e.get("entry_date"), "applies": side == "short",
               "sector": e.get("sector") or UNCLASSIFIED}
        if tk not in order:
            rec.update(evaluable=False, reason="not_in_book_order", reason_zh=REASONS["not_in_book_order"])
            out.append(rec)
            continue
        if tk not in per or not per[tk]:
            rec.update(evaluable=False, reason="no_grid_for_ticker", reason_zh=REASONS["no_grid_for_ticker"])
            out.append(rec)
            continue
        before = dict(rest_cells)
        for t in order[:order.index(tk)]:
            before = _add(before, per.get(t, {}))
        after = _add(before, per[tk])
        lb, la = _loss_pct(before, nav), _loss_pct(after, nav)
        rec.update(evaluable=True, reason=None, loss_before_pct=_r(lb, 2), loss_after_pct=_r(la, 2),
                   marginal_pct=_r(la - lb, 2),
                   would_skip={_budget_key(b): bool(rec["applies"] and la > b) for b in budgets})
        out.append(rec)
    return {"version": VERSION, "budgets_pct": [_budget_key(b) for b in budgets], "entries": out,
            "n_entries": len(out), "n_unevaluated": sum(1 for x in out if not x["evaluable"]),
            "n_would_skip": {_budget_key(b): sum(1 for x in out if x["evaluable"] and x["would_skip"][_budget_key(b)])
                             for b in budgets},
            "basis": "当天收盘标记价下的实际账（股票账 + SPY 覆盖成交前持仓 + 此前已开跨式）；不是入场那一刻的报价，也不是反事实"}


def brief(attr: dict, gate: Optional[dict], alert_pct: Optional[float] = None) -> dict:
    """给 `price_check.tail` → `scan_timing` → `alert_manager` 的小简报（完整表在审计文件的 `tail` 一节）。"""
    alert_pct = float(alert_pct if alert_pct is not None else CONFIG["alert_pct"])
    if not attr.get("available"):
        return {"available": False, "reason": attr.get("reason"), "alert_pct": alert_pct, "breach": False}
    loss = attr["loss_pct_nav"]
    top = [{"ticker": t["ticker"], "sector": t["sector"], "at_worst_pct": t["at_worst_pct"]}
           for t in attr["by_ticker"][:int(CONFIG["top_n"])] if (t["at_worst"] or 0.0) < 0]
    sec = [{"sector": s["sector"], "at_worst_pct": s["at_worst_pct"]} for s in attr["by_sector"][:2] if (s["at_worst"] or 0.0) < 0]
    return {"available": True, "alert_pct": alert_pct, "loss_pct_nav": loss, "breach": bool(loss > alert_pct),
            "worst": attr["worst"], "partial": attr["partial"], "top": top, "top_sectors": sec,
            "gate_n_would_skip": (gate or {}).get("n_would_skip") or {}, "gate_n_entries": (gate or {}).get("n_entries", 0)}


def public(attr: dict) -> dict:
    """落盘 / 返回前去掉内部大字典（逐格全表只给计算用）。"""
    return {k: v for k, v in attr.items() if not k.startswith("_")}


def build(rows: Sequence[dict], spy_price: Optional[float], nav: Optional[float],
          stress_fn: Callable[[List[dict], Optional[float]], dict],
          sector_of: Callable[[str], Optional[str]], positions: Sequence[dict], as_of: str) -> dict:
    """主入口：归因 + 告警简报 + 影子闸。永远返回 dict（不可得时 `available=False` + 原因码）。

    `positions`：跨式账持仓（按文件顺序，字段 `ticker` / `side` / `entry_date`）。"""
    attr = attribution(rows, spy_price, nav, stress_fn, sector_of)
    if not attr.get("available"):
        return {"version": VERSION, "available": False, "reason": attr["reason"], "reason_zh": attr["reason_zh"],
                "alert_pct": CONFIG["alert_pct"], "brief": brief(attr, None)}
    nav_f = float(nav)
    order = [str(p.get("ticker")) for p in positions or []]
    entries = [{"ticker": str(p.get("ticker")), "side": p.get("side"), "entry_date": p.get("entry_date"),
                "sector": sector_of(str(p.get("ticker"))) or UNCLASSIFIED}
               for p in positions or [] if str(p.get("entry_date") or "") == str(as_of)]
    rest_rows = [r for r in rows if r.get("kind") != "option"]
    rest_cells = _cells(stress_fn(list(rest_rows), spy_price))
    gate = gate_shadow(attr, rest_cells, order, entries, nav_f)
    out = public(attr)
    out["alert_pct"] = CONFIG["alert_pct"]
    out["gate_shadow"] = gate
    out["brief"] = brief(attr, gate)
    out["as_of"] = as_of
    return out
