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
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence, Tuple

VERSION = "tail_budget_v1"

#: **`alert_pct` 是用户的风险承受度，不是统计量**（2026-10-25 用户定：纸面跨式账在一次「现货 −10%、IV +20 点」里最多允许亏 20% NAV）。
#: 其余几项（影子闸的另一档 15%、点名个数）是我定的主观常数、未经检验。改任何一项 ⇒ 告警 / 影子记录的含义变了，CHANGELOG 要写；
#: 影子记录按 `version` 分代。**Gamma / Vega 告警线与单仓上限都从 `alert_pct` 推出来**（见 `derived_greek_lines` / `slot_cap_pct`），不另设常数。
CONFIG: Dict[str, object] = {
    "alert_pct": 20.0,                      # 一次压力冲击里最多允许的损失（% NAV）：全组合最差格损失 > 它 ⇒ 告警（P2）
    "shadow_budgets_pct": (15.0, 20.0),     # 影子闸的两档预算（% NAV）：20 = 上面的承受度，15 = 更严的一档
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
    # β 窗口对照（v0.45.662）
    "alt_beta_unavailable": "对照用的长窗口 β 一个都没取到（行情源不可得 / 样本不足）",
    "no_rows": "审计里没有可对照的持仓行",
    "base_unavailable": "现状（60 日 β）的压力表本身算不出来",
}


def derived_greek_lines(alert_pct: float, spot_pct: float, iv_pts: float) -> Dict[str, float]:
    """Gamma / Vega 告警线（% NAV）从尾部预算推出来，不另设常数。

    压力冲击（现货 `spot_pct`%、IV `iv_pts` 点）下的损失，二阶近似：`Gamma(1%)·spot² + Vega·iv`（Delta 项在市场中性的
    账里可忽略，且符号不定）。把预算 `alert_pct` **对半**分给两项：
        gamma 线 = alert_pct/2 ÷ spot²        （alert 20、spot 10 ⇒ 0.10% NAV / 1%）
        vega  线 = alert_pct/2 ÷ iv           （alert 20、iv 20  ⇒ 0.50% NAV / 点）
    两项都顶到线时，损失正好等于预算。**对半分是约定，不是校准**；这两条线只做早期预警与归因（页面上的 ⚠️ 标记，没有
    alert_manager 路径），真正的告警仍是压力网格那条——二阶近似不含 Delta / skew，跟压力表差几个百分点是常态
    （2026-10-23：近似 22.4% vs 压力表 23.1%）。"""
    a, sp, iv = _num(alert_pct), _num(spot_pct), _num(iv_pts)
    if not a or not sp or not iv or a <= 0 or sp <= 0 or iv <= 0:
        raise ValueError(f"推导 Gamma / Vega 线需要正的预算与冲击幅度，收到 {alert_pct!r} / {spot_pct!r} / {iv_pts!r}")
    return {"gamma_alert_pct": a / 2.0 / (sp * sp), "vega_alert_pct": a / 2.0 / iv}


def slot_cap_pct(alert_pct: float, max_open: Optional[int]) -> Optional[float]:
    """单仓上限（% NAV）：预算平分给跨式账的每个名额（`max_open`），`alert_pct ÷ max_open`。
    是规则、不是调出来的：6 个名额各分 20% ÷ 6 ≈ 3.33%。单笔超过它 ⇒ 这一笔一个人就吃掉了不止一个名额的预算。"""
    a, n = _num(alert_pct), _num(max_open)
    return a / n if (a and n and n > 0) else None


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


def _slot(cells: Optional[Dict], nav: float, cap: Optional[float]) -> Optional[dict]:
    """这个标的的**跨式腿单独**最差格损失（% NAV）与单仓上限的对比；没有跨式腿 / 没有上限 ⇒ None。"""
    if not cells or cap is None:
        return None
    loss = _loss_pct(cells, nav)
    return {"loss_pct": _r(loss, 2), "cap_pct": _r(cap, 2), "over_cap": bool(loss > cap),
            "multiple": _r(loss / cap, 2) if cap else None}


def _bucket_key(r: dict) -> Tuple[str, str]:
    kind = str(r.get("kind") or "")
    return str(r.get("ticker") or "?"), kind


def attribution(rows: Sequence[dict], spy_price: Optional[float], nav: Optional[float],
                stress_fn: Callable[[List[dict], Optional[float]], dict],
                sector_of: Callable[[str], Optional[str]], max_open: Optional[int] = None) -> dict:
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
    cap = slot_cap_pct(CONFIG["alert_pct"], max_open)
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
            "slot": _slot(option_cells.get(tk), nav_f, cap),
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
    over = [t["ticker"] for t in out_t if (t.get("slot") or {}).get("over_cap")]
    return {"available": True, "reason": None, "version": VERSION, "nav": _r(nav_f),
            "slot_cap_pct": _r(cap, 2), "max_open": max_open, "n_over_cap": len(over), "over_cap_tickers": over,
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
    over = [{"ticker": t["ticker"], "loss_pct": t["slot"]["loss_pct"], "multiple": t["slot"]["multiple"]}
            for t in attr["by_ticker"] if (t.get("slot") or {}).get("over_cap")]
    return {"available": True, "alert_pct": alert_pct, "loss_pct_nav": loss, "breach": bool(loss > alert_pct),
            "slot_cap_pct": attr.get("slot_cap_pct"), "n_over_cap": len(over), "over_cap": over,
            "worst": attr["worst"], "partial": attr["partial"], "top": top, "top_sectors": sec,
            "gate_n_would_skip": (gate or {}).get("n_would_skip") or {}, "gate_n_entries": (gate or {}).get("n_entries", 0)}


def public(attr: dict) -> dict:
    """落盘 / 返回前去掉内部大字典（逐格全表只给计算用）。"""
    return {k: v for k, v in attr.items() if not k.startswith("_")}


# ══════════════════════════════════════════════════════════════════════════════
# β 窗口对照（v0.45.662，**只展示**）
# ══════════════════════════════════════════════════════════════════════════════
#
# 起因（2026-10-27 只读查证）：压力网格每个标的的现货冲击 = β × 大盘冲击，β 取近 60 个交易日对 SPY 的 OLS。XOM / CVX 的 60 日 β 是
# −1.1 / −0.9（250 日 −0.69 / −0.62，2 年 +0.17 / +0.28）⇒ 「现货 −10%」那一格把能源股推到**上涨** ~10%，下行格里这两笔的损失其实
# 是股价涨造成的。**没有「哪个窗口才对」的真值**——所以不改网格的 β，只把另一套窗口的结果并排摆出来：全网格最差格、下行最差格（SPY 认沽
# 面板的规模目标）、告警是否破线、β·$Delta 与带位（SPY 覆盖建议吃的那个量）、逐标的贡献。
#
# 纯函数：不联网、不读写文件；压力函数 / 板块函数由调用方注入；替代 β 由调用方取好传进来（取数在 `alphabot.beta_long`）。
# **不进审计文件、不进告警、不进任何交易决定**——审计里的压力表、告警、SPY 覆盖账仍只用 60 日 β。

#: 「差异明显」的两条判据：符号相反，或 |差| ≥ 这个值（只用来挑要列出来的标的，不进任何决定）
BETA_GAP_MIN = 0.5
BETA_SENS_TICKERS_SHOWN = 8


def with_beta(rows: Sequence[dict], alt_betas: Optional[Dict[str, Optional[float]]]) -> Tuple[List[dict], List[str], List[str]]:
    """每行换上替代 β，返回 `(新行, 换上了的标的, 沿用原 β 的标的)`；不改入参。

    · 只换**本来就有 β 的**股票 / 期权 / 逐票对冲行：本来没 β 的行在压力表里被剔除，换不换都一样被剔除——不借替代 β 把它们「救回来」，
      否则两套结果的 `partial` / `excluded` 对不上，比较就不是同一批行。
    · SPY 覆盖行（`kind == "hedge"`）β 恒为 1，不动。
    · 替代 β 缺（取不到）的标的沿用原 β，并单列在第三项里——结果是「混合口径」，调用方必须把它摆出来。
    · `beta_dollar_delta` 跟着重算（= `dollar_delta` × β），这是 SPY 覆盖建议用的那个量。"""
    out: List[dict] = []
    used, kept = set(), set()
    for r in rows:
        r2 = dict(r)
        out.append(r2)
        tk = r.get("ticker")
        if r.get("kind") == "hedge" or tk is None or _num(r.get("beta")) is None:
            continue
        b = _num((alt_betas or {}).get(tk))
        if b is None:
            kept.add(tk)
            continue
        used.add(tk)
        r2["beta"] = b
        dd = _num(r.get("dollar_delta"))
        r2["beta_dollar_delta"] = dd * b if dd is not None else None
    return out, sorted(used), sorted(kept - used)


def _beta_dd(rows: Sequence[dict]) -> Optional[float]:
    """组合 β·$Delta 合计（含 SPY 覆盖行；与审计 `aggregate.beta_dollar_delta` 同一个量）。没有任何行带值 ⇒ None。"""
    vals = [_num(r.get("beta_dollar_delta")) for r in rows]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _band_status(pct: Optional[float], band_pct: Optional[float]) -> Optional[str]:
    if pct is None or not band_pct:
        return None
    return "above" if pct > band_pct else "below" if pct < -band_pct else "inside"


def _sens_side(rows: List[dict], spy_price, nav: float, stress_fn, sector_of, max_open, alert_pct: float,
               band_pct: Optional[float]) -> Optional[dict]:
    attr = attribution(rows, spy_price, nav, stress_fn, sector_of, max_open)
    if not attr.get("available"):
        return None
    cells = _cells(stress_fn(list(rows), spy_price))
    bd = _beta_dd(rows)
    bd_pct = _r(bd / nav * 100.0, 2) if bd is not None else None
    return {"loss_pct_nav": attr["loss_pct_nav"], "breach": bool(attr["loss_pct_nav"] > alert_pct),
            "worst": attr["worst"], "downside_worst": attr["downside_worst"], "partial": attr["partial"],
            "beta_dd_pct": bd_pct, "band_status": _band_status(bd_pct, band_pct),
            "cells": [{"spot_pct": k[0], "iv_pts": k[1], "pnl": _r(v), "pct_nav": _r(v / nav * 100.0, 2)}
                      for k, v in sorted(cells.items())],
            "by_ticker": {t["ticker"]: {"at_worst_pct": t["at_worst_pct"], "at_downside_pct": t["at_downside_pct"]}
                          for t in attr["by_ticker"]}}


def beta_sensitivity(rows: Sequence[dict], alt_betas: Optional[Dict[str, Optional[float]]], spy_price: Optional[float],
                     nav: Optional[float], stress_fn: Callable[[List[dict], Optional[float]], dict],
                     sector_of: Callable[[str], Optional[str]], max_open: Optional[int] = None, *,
                     base_label: str = "60 日", alt_label: str = "250 日", alert_pct: Optional[float] = None,
                     band_pct: Optional[float] = None) -> dict:
    """同一份审计行，用「原 β（审计里的，60 日）」与「替代 β（长窗口）」各算一遍压力网格 / 尾部归因，并排给出。

    返回 `{available, reason, base, alt, downside_ratio, flips, by_ticker, missing, partial, ...}`：
      · `base` / `alt`：`{loss_pct_nav, breach, worst, downside_worst, beta_dd_pct, band_status, cells, partial}`；
      · `downside_ratio` = 替代 ÷ 现状 的下行最差格损益（SPY 认沽面板的规模目标按这个比例缩放，未触上限时）；
      · `flips`：持仓标的里 β 符号相反或 |差| ≥ `BETA_GAP_MIN` 的，按持仓净 $Delta 的绝对值降序；
      · `by_ticker`：逐标的两套贡献（%NAV），按两套里较大的 |最差格 / 下行最差| 降序，最多 `BETA_SENS_TICKERS_SHOWN` 个；
      · `missing`：替代 β 缺、沿用原 β 的标的（混合口径，`partial` 为真）。
    纯函数；算不出写原因码，不抛。**没有任何推荐 / 结论字段**——两套都是模型假设，没有哪套是真值。"""
    alert = float(alert_pct if alert_pct is not None else CONFIG["alert_pct"])
    nav_f = _num(nav)
    if not nav_f or nav_f <= 0:
        return _sens_unavailable("nav_unavailable", base_label, alt_label)
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return _sens_unavailable("no_rows", base_label, alt_label)
    alt_rows, used, missing = with_beta(rows, alt_betas)
    if not used:
        return _sens_unavailable("alt_beta_unavailable", base_label, alt_label)
    base = _sens_side(list(rows), spy_price, nav_f, stress_fn, sector_of, max_open, alert, band_pct)
    if base is None:
        return _sens_unavailable("base_unavailable", base_label, alt_label)
    alt = _sens_side(alt_rows, spy_price, nav_f, stress_fn, sector_of, max_open, alert, band_pct)
    if alt is None:
        return _sens_unavailable("stress_unavailable", base_label, alt_label)
    # 逐标的
    net_dd: Dict[str, float] = {}
    base_beta: Dict[str, float] = {}
    for r in rows:
        tk = r.get("ticker")
        if tk is None or r.get("kind") == "hedge":
            continue
        net_dd[tk] = net_dd.get(tk, 0.0) + (_num(r.get("dollar_delta")) or 0.0)
        if _num(r.get("beta")) is not None:
            base_beta.setdefault(tk, _num(r.get("beta")))
    flips = []
    for tk in used:
        b0, b1 = base_beta.get(tk), _num((alt_betas or {}).get(tk))
        if b0 is None or b1 is None:
            continue
        sign_flip = (b0 < 0) != (b1 < 0) and min(abs(b0), abs(b1)) > 0.0
        if sign_flip or abs(b0 - b1) >= BETA_GAP_MIN:
            flips.append({"ticker": tk, "base_beta": _r(b0, 2), "alt_beta": _r(b1, 2), "sign_flip": bool(sign_flip),
                          "net_dollar_delta_pct_nav": _r(net_dd.get(tk, 0.0) / nav_f * 100.0, 2)})
    flips.sort(key=lambda f: (-abs(f["net_dollar_delta_pct_nav"] or 0.0), f["ticker"]))
    names = sorted(set(base["by_ticker"]) | set(alt["by_ticker"]))
    table = []
    for tk in names:
        b, a = base["by_ticker"].get(tk) or {}, alt["by_ticker"].get(tk) or {}
        vals = [abs(x) for x in (b.get("at_worst_pct"), b.get("at_downside_pct"), a.get("at_worst_pct"), a.get("at_downside_pct"))
                if x is not None]
        table.append({"ticker": tk, "base_at_worst_pct": b.get("at_worst_pct"), "alt_at_worst_pct": a.get("at_worst_pct"),
                      "base_at_downside_pct": b.get("at_downside_pct"), "alt_at_downside_pct": a.get("at_downside_pct"),
                      "base_beta": _r(base_beta.get(tk), 2) if tk in base_beta else None,
                      "alt_beta": _r(_num((alt_betas or {}).get(tk)), 2) if tk in used else None,
                      "_key": max(vals) if vals else 0.0})
    table.sort(key=lambda x: (-x["_key"], x["ticker"]))
    for t in table:
        t.pop("_key")
    for side in (base, alt):
        side.pop("by_ticker")
    bd, ad = (base["downside_worst"] or {}).get("pnl"), (alt["downside_worst"] or {}).get("pnl")
    return {"available": True, "reason": None, "version": VERSION, "base_label": base_label, "alt_label": alt_label,
            "alert_pct": alert, "band_pct": band_pct, "base": base, "alt": alt,
            "downside_ratio": _r(ad / bd, 2) if (bd and ad is not None) else None,
            "flips": flips, "by_ticker": table[:BETA_SENS_TICKERS_SHOWN], "n_alt": len(used), "missing": missing,
            "partial": bool(missing) or bool(base["partial"]) or bool(alt["partial"])}


def _sens_unavailable(reason: str, base_label: str = "60 日", alt_label: str = "250 日") -> dict:
    return {"available": False, "reason": reason, "reason_zh": REASONS.get(reason, reason), "version": VERSION,
            "base_label": base_label, "alt_label": alt_label}


def build(rows: Sequence[dict], spy_price: Optional[float], nav: Optional[float],
          stress_fn: Callable[[List[dict], Optional[float]], dict],
          sector_of: Callable[[str], Optional[str]], positions: Sequence[dict], as_of: str,
          max_open: Optional[int] = None) -> dict:
    """主入口：归因 + 告警简报 + 影子闸。永远返回 dict（不可得时 `available=False` + 原因码）。

    `positions`：跨式账持仓（按文件顺序，字段 `ticker` / `side` / `entry_date`）；`max_open`：跨式账的名额数（单仓上限 = 预算 ÷ 它）。"""
    attr = attribution(rows, spy_price, nav, stress_fn, sector_of, max_open)
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


# ════════════════════════════════════════════════════════════════════════
# 压力模型 vs 实际净值（v0.45.655）
# ════════════════════════════════════════════════════════════════════════
#: 不比的原因（唯一一张表；`tests/test_tail_budget.py` 双向核对）
CHECK_SKIPS: Dict[str, str] = {
    "gap_too_long": "两份审计隔得太久（超过 5 个日历日），中间的变化不是「一天的冲击」",
    "no_options": "有一天没有跨式腿的明细行",
    "composition_changed": "两天的跨式持仓不一样（有开仓 / 平仓 / 张数变化）：净值变化里混着进出，不是价格冲击",
    "row_incomplete": "有跨式腿缺 mid / 价 / Greeks / IV（报价缺失或买价为 0），没法重估",
}
#: 页面与文档照抄的口径说明
CHECK_NOTES: Dict[str, str] = {
    "scope": "只比跨式腿（尾部的来源）、只比相邻两份审计之间持仓组成不变的那些天；股票账几乎天天有进出，没法干净比",
    "actual_ledger": "实际 · 账本净值变化 = 跨式账净值曲线的差（账本的标记来自扫描时刻的 CBOE 中间价）",
    "actual_audit": "实际 · 同报价重估 = Σ 张数×100×(今 mid − 昨 mid)，用的是审计自己的报价，和两个预测同一份报价。"
                    "两个口径是否一致看 `summary.ledger_audit_max_diff_usd`：差大 ⇒ 账本标记与审计报价不是同一份",
    "pred_grid": "预测 · 压力网格 = 网格在「SPY 当天涨跌、IV 变化（Vega 加权）」处的插值 − 网格 (0,0) 的模型基差 + θ×日历天数。"
                 "它只建模 β×大盘冲击，抓不到单名自己的跳空（财报跳空正是这类仓位的尾部）",
    "pred_greeks": "预测 · Greeks 展开 = 每条腿用昨天的 delta / gamma / vega / theta 与自己标的的真实涨跌、自己的真实 IV 变化做二阶展开。"
                   "它检验的是「Gamma / Vega → 损失」这层近似本身，不受大盘β假设影响",
    "small_n": "样本只有几对，只是描述，不是检验",
}


def _lagrange(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    tot = 0.0
    for i, (xi, yi) in enumerate(zip(xs, ys)):
        w = 1.0
        for j, xj in enumerate(xs):
            if j != i:
                w *= (x - xj) / (xi - xj)
        tot += yi * w
    return tot


def interp_cell(cells: Dict[Tuple[float, float], float], spot: float, iv: float) -> Optional[float]:
    """压力网格上取 (spot, iv) 处的值：现货方向用最近的三个结点做**二次**插值（网格里的损益对现货是凸 / 凹的，线性插值会把
    Gamma 项放大一个量级），IV 方向在相邻两列之间线性。落在网格之外 ⇒ None（不外推）。"""
    spots = sorted({k[0] for k in cells})
    ivs = sorted({k[1] for k in cells})
    if len(spots) < 3 or len(ivs) < 2 or not (spots[0] <= spot <= spots[-1]) or not (ivs[0] <= iv <= ivs[-1]):
        return None
    # 三个连续结点 = 离 spot 最近的三个（spot 落在哪个区间、靠哪一头；边上取靠里的三个）
    left = next(k for k in range(len(spots) - 1) if spots[k] <= spot <= spots[k + 1])
    i = max(0, min(len(spots) - 3, left - (1 if (spot - spots[left]) < (spots[left + 1] - spot) else 0)))
    xs = spots[i:i + 3]
    j = next(k for k in range(len(ivs) - 1) if ivs[k] <= iv <= ivs[k + 1])
    vals = []
    for ivn in (ivs[j], ivs[j + 1]):
        try:
            vals.append(_lagrange(xs, [cells[(x, ivn)] for x in xs], spot))
        except KeyError:
            return None
    t = (iv - ivs[j]) / (ivs[j + 1] - ivs[j])
    return vals[0] * (1.0 - t) + vals[1] * t


def _days_between(d0: str, d1: str) -> Optional[int]:
    try:
        return (date.fromisoformat(str(d1)) - date.fromisoformat(str(d0))).days
    except (TypeError, ValueError):
        return None


def _opt_rows(a: Optional[dict]) -> List[dict]:
    return [r for r in ((a or {}).get("rows") or []) if isinstance(r, dict) and r.get("kind") == "option"]


def _check_pair(d0: str, a0: dict, d1: str, a1: dict, stress_fn, max_gap_days: int) -> dict:
    rec: dict = {"from": d0, "to": d1, "status": "skipped", "reason": None, "reason_zh": None}

    def skip(code: str) -> dict:
        rec.update(reason=code, reason_zh=CHECK_SKIPS[code])
        return rec

    gap = _days_between(d0, d1)
    if gap is None or gap <= 0 or gap > max_gap_days:
        return skip("gap_too_long")
    o0, o1 = _opt_rows(a0), _opt_rows(a1)
    if not o0 or not o1:
        return skip("no_options")
    if sorted((str(r.get("symbol")), _num(r.get("qty"))) for r in o0) != sorted((str(r.get("symbol")), _num(r.get("qty"))) for r in o1):
        return skip("composition_changed")
    m1 = {str(r.get("symbol")): r for r in o1}
    need0 = ("qty", "price", "delta", "gamma", "vega", "theta", "mid", "iv")
    legs = []
    for r in o0:
        q = m1.get(str(r.get("symbol"))) or {}
        v0 = {k: _num(r.get(k)) for k in need0}
        v1 = {k: _num(q.get(k)) for k in ("price", "mid", "iv")}
        if any(v is None for v in v0.values()) or any(v is None for v in v1.values()):
            return skip("row_incomplete")
        legs.append((r, v0, v1))
    actual_audit = sum(v0["qty"] * (v1["mid"] - v0["mid"]) for _, v0, v1 in legs)
    pred_greeks = 0.0
    wsum = wdiv = 0.0
    for r, v0, v1 in legs:
        d_s = v1["price"] - v0["price"]
        d_iv = (v1["iv"] - v0["iv"]) * 100.0                      # 点
        pred_greeks += v0["qty"] * (v0["delta"] * d_s + 0.5 * v0["gamma"] * d_s * d_s + v0["vega"] * d_iv + v0["theta"] * gap)
        w = abs(_num(r.get("vega_dollar_per_pt")) if _num(r.get("vega_dollar_per_pt")) is not None else v0["qty"] * v0["vega"])
        wsum += w
        wdiv += w * d_iv
    iv_shift = wdiv / wsum if wsum else 0.0
    spy0, spy1 = _num((a0 or {}).get("spy_price")), _num((a1 or {}).get("spy_price"))
    spot = (spy1 / spy0 - 1.0) * 100.0 if spy0 and spy1 else None
    pred_grid = None
    grid_note = None
    if stress_fn is not None and spot is not None:
        cells = _cells(stress_fn([r for r, _, _ in legs], spy0))
        f, f0 = interp_cell(cells, spot, iv_shift), interp_cell(cells, 0.0, 0.0)
        if f is None or f0 is None:
            grid_note = "冲击落在压力网格之外（不外推）"
        else:
            pred_grid = f - f0 + sum(v0["qty"] * v0["theta"] for _, v0, _ in legs) * gap
    elif stress_fn is None:
        grid_note = "没有压力函数"
    else:
        grid_note = "缺 SPY 价"
    n0 = _num((((a0 or {}).get("nav") or {}).get("components") or {}).get("straddle_leg"))
    n1 = _num((((a1 or {}).get("nav") or {}).get("components") or {}).get("straddle_leg"))
    nav0 = _num(((a0 or {}).get("nav") or {}).get("nav"))
    rec.update(status="ok", reason=None, reason_zh=None, days=gap, spot_pct=_r(spot, 2), iv_shift_pts=_r(iv_shift, 2),
               n_legs=len(legs), nav=_r(nav0),
               actual_ledger=_r(n1 - n0) if (n0 is not None and n1 is not None) else None,
               actual_audit=_r(actual_audit), pred_grid=_r(pred_grid), pred_greeks=_r(pred_greeks), grid_note=grid_note)
    return rec


def _stats(pairs: List[dict], pred: str, actual: str) -> Optional[dict]:
    xs = [(p[pred], p[actual], p.get("nav")) for p in pairs if p.get(pred) is not None and p.get(actual) is not None]
    if not xs:
        return None
    n = len(xs)
    err = [a - b for a, b, _ in xs]
    act = [b for _, b, _ in xs]
    mean_act = sum(act) / n
    sst = sum((v - mean_act) ** 2 for v in act)
    mae = sum(abs(e) for e in err) / n
    navs = [c for _, _, c in xs if c]
    return {"n": n, "mean_error_usd": _r(sum(err) / n), "mean_abs_error_usd": _r(mae),
            "mean_abs_actual_usd": _r(sum(abs(v) for v in act) / n),
            "mean_abs_error_pct_nav": _r(mae / (sum(navs) / len(navs)) * 100.0, 3) if navs else None,
            "r2": _r(1.0 - sum(e * e for e in err) / sst, 3) if (n >= 3 and sst > 0) else None}


def model_check(audits: Dict[str, dict], stress_fn: Optional[Callable] = None, max_gap_days: int = 5) -> dict:
    """压力模型 vs 实际净值（只看跨式腿）。纯函数，输入 = 放行的审计（日期 → 审计，须带 `rows` / `nav` / `spy_price`）。

    相邻两份审计、跨式持仓**完全一样**的才比；误差 = 预测 − 实际（正 = 模型比实际乐观）。两个预测、两个实际见 `CHECK_NOTES`。
    `stress_fn`（`portfolio_greeks.stress_table`）缺省 ⇒ 不给压力网格那个预测。样本少是常态，`summary` 只是描述。"""
    days = sorted(audits or {})
    pairs = [_check_pair(d0, audits[d0], d1, audits[d1], stress_fn, max_gap_days) for d0, d1 in zip(days, days[1:])]
    used = [p for p in pairs if p["status"] == "ok"]
    skipped: Dict[str, int] = {}
    for p in pairs:
        if p["status"] != "ok":
            skipped[p["reason"]] = skipped.get(p["reason"], 0) + 1
    summary = {f"{m}_vs_{a}": _stats(used, f"pred_{m}", f"actual_{a}") for m in ("grid", "greeks") for a in ("ledger", "audit")}
    diffs = [abs(p["actual_ledger"] - p["actual_audit"]) for p in used if p.get("actual_ledger") is not None]
    summary["ledger_audit_max_diff_usd"] = _r(max(diffs)) if diffs else None
    return {"version": VERSION, "n_pairs": len(pairs), "n_used": len(used), "skipped_by_reason": skipped,
            "pairs": pairs[-60:], "summary": summary, "notes": dict(CHECK_NOTES), "skip_reasons": dict(CHECK_SKIPS)}
