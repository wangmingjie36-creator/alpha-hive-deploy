"""逐票 Delta 对冲的决策核心（v0.45.526）。**纯函数**：不读写文件、不联网、不 import 对冲引擎。

背景（`portfolio_greeks` 模块头「没做的事」曾写「不做逐票 delta 对冲」）：2026-10-15 的账本里，跨式腿的 β·$Delta
是 −45.9% NAV，股票账只有 +5.1%——带外几乎全来自跨式腿，而且是用单只股票自己的 60 日 OLS β 折成 SPY 暴露：
TSLA β=2.76 把 −8.2% 的原始 Delta 放大成 −22.8%，T / VZ 的 β 为负（−0.25 / −0.28）把 +27% 左右的原始 Delta 翻成负的
β·Delta。用 SPY 去抵这种「单票、β 噪声很大」的暴露，误差大且不可解释。直接用该标的自己的股票对冲跨式 Delta，
没有 β 估计这一层。

做什么（规则全在这里，`portfolio_greeks` 只负责取价 / 取持仓 / 落账）：
  · 对象 = **每个标的的跨式腿净 $Delta（原始值，不乘 β）**。不含股票账里同名仓位——股票账的方向观点不被抵消；
  · 工具 = 该标的自己的股票，整股；
  · 带：|净 $Delta| 超过 NAV 的 `band_pct`% 才动手。净 $Delta = 跨式腿 $Delta + 逐票对冲持仓市值；
  · 拉回：`center` = 回到 0（缺省：对冲跨式的目的是剥离 Delta、只留波动率，不保留方向观点），
    `edge` = 只拉回最近带边（与 SPY 带同一种做法）；
  · 该标的**没有跨式腿了**（财报后平仓）⇒ 目标强制回 0，无条件清掉对冲股票——不留没有跨式的孤儿股票；
  · 缺数据**逐标的**判：这只缺价 / 缺报价 / 缺 NAV，只有这只不对冲、写明原因，别的照做。

⚠️ 这些数字（3% 带、拉回 0）是 v1 的主观常数，**不是校准结果**；没有任何证据说明逐票对冲后更好——它是执行层选择，
不是已验证的优势。没有点差 / 冲击成本**模型**（单只股票比 SPY 贵）；只对冲 Delta，Gamma / Vega 不对冲
（卖出跨式是负 Gamma，Delta 会反复漂移，靠带控制调仓频率）。

成本估算（v0.45.638，`with_costs`）：**只是一个固定 bps 的主观估算**（`delta_hedge_cost_bps`，缺省 5），不是点差 / 冲击模型，
不进决策（带与拉回完全不看它）、不扣现金。用 2026-10-19~23 的 shadow 审计回放：带宽从 3% 放到 8%，日均成交只从 48% NAV
降到 44%——成交量由卖跨式的 Gamma 漂移决定，不由触发次数决定——所以成本要单独看，不能靠调带宽省。

输出 `decide()` 的每个标的一条：
  status  hedge（要成交）/ inside（带内或四舍五入成 0 股，不动）/ exit（跨式没了，清仓）/ flat（无跨式、无持仓，略过）/
          unknown（算不出来，reason 说明）
  target_shares  **无状态**的目标：把跨式 Delta 全抵掉需要持有的股数（= −跨式 $Delta / 价，整股）。shadow 模式没有持仓账，
                 页面与审计看这个字段；trade_shares 才是「相对当前持仓要成交多少」。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional

MODES = ("off", "shadow", "paper")
REBALANCE_TO = ("center", "edge")

#: unknown 的原因码（唯一一张表；`tests/test_delta_hedge.py` 用 AST 双向核对：每个写出的码都登记、表里无死键）
REASONS = {
    "nav_unavailable": "NAV 不可得，带宽算不出来",
    "option_delta_unknown": "跨式腿有一行 $Delta 算不出来（缺报价 / 缺价）",
    "price_missing": "这只标的取不到 as_of 的价",
    "price_stale": "这只标的的价不是 as_of 那一场的",
    "held_shares_unknown": "逐票对冲持仓账里这只的股数读不出来",
}


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


def check_cost_bps(v) -> float:
    """成本假设（bps）只接受有限的非负数；拼错 / 负数 / NaN 当场红，不静默当 0（0 会让成本一栏看起来「免费」）。"""
    f = None if isinstance(v, str) else _num(v)          # 字符串 "5" 也拒：配置写成字符串是写错了，不替它悄悄转换
    if f is None or f < 0:
        raise ValueError(f"delta_hedge_cost_bps 只接受有限的非负数，收到 {v!r}")
    return f


def round_away(x: float) -> int:
    """四舍五入、0.5 远离 0（Python 的 round 是银行家舍入：+0.5 → 0、+1.5 → 2，正负不对称）。"""
    return int(math.copysign(math.floor(abs(x) + 0.5), x)) if x else 0


def straddle_delta_by_name(option_rows: List[Mapping[str, Any]]) -> Dict[str, dict]:
    """跨式腿每张合约一行（一笔跨式两行）→ 每个标的的净 $Delta。

    `dd` = 该标的所有期权行 $Delta 之和；**任何一行 $Delta 为 None ⇒ dd 为 None**（缺的那一行可能大到翻转结论，
    不拿部分和下单）。`reason` 说清是缺报价还是缺价。"""
    out: Dict[str, dict] = {}
    for r in option_rows or []:
        tk = str(r.get("ticker") or "")
        if not tk:
            continue
        d = out.setdefault(tk, {"dd": 0.0, "n_rows": 0, "unknown_symbols": [], "reason": None})
        d["n_rows"] += 1
        v = _num(r.get("dollar_delta"))
        if v is None:
            d["unknown_symbols"].append(str(r.get("symbol") or tk))
            d["reason"] = d["reason"] or "option_delta_unknown"
        elif d["dd"] is not None:
            d["dd"] += v
    for d in out.values():
        if d["unknown_symbols"]:
            d["dd"] = None
        else:
            d["dd"] = round(d["dd"], 2)
    return out


def _mark_fields(mark: Optional[Mapping[str, Any]]) -> dict:
    m = mark or {}
    return {"price": _pos(m.get("price")), "price_session": m.get("session"), "price_source": m.get("source"),
            "price_at_close": bool(m.get("at_close")), "price_stale": bool(m.get("stale"))}


def decide(straddle: Mapping[str, dict], held: Mapping[str, float], marks: Mapping[str, Mapping[str, Any]], *,
           nav: Optional[float], band_pct: float, rebalance_to: str = "center") -> List[dict]:
    """每个出现过的标的（有跨式腿的 ∪ 有对冲持仓的）一条决策，按代码排序。

    straddle：{标的: {"dd": 跨式腿净 $Delta | None, "reason": ...}}（`straddle_delta_by_name`）；
    held：{标的: 当前逐票对冲持仓股数}（shadow 模式没有持仓账 ⇒ 空）；
    marks：{标的: `_resolve_mark` 的结果}（price 为 None ⇒ 缺价或陈旧）。
    纯函数、确定性：同一输入恒给同一输出（同日重跑幂等靠它）。"""
    if rebalance_to not in REBALANCE_TO:
        raise ValueError(f"rebalance_to 只接受 {REBALANCE_TO}，收到 {rebalance_to!r}")
    nav = _pos(nav)
    band_usd = (band_pct / 100.0 * nav) if nav is not None else None
    out: List[dict] = []
    # held 里 None / NaN = 股数读不出来（账本坏行）：未知，不是 0——按 0 算会把「有仓」当成「没仓」
    unknown_held = {t for t, s in held.items() if _num(s) is None}
    for tk in sorted(set(straddle) | unknown_held | {t for t, s in held.items() if (_num(s) or 0.0) != 0.0}):
        st = straddle.get(tk)
        has_straddle = st is not None
        held_sh = _num(held.get(tk)) or 0.0
        mk = _mark_fields(marks.get(tk))
        rec: Dict[str, Any] = {
            "ticker": tk, "has_straddle": has_straddle, "status": "unknown", "reason": None,
            "option_dd": (st or {}).get("dd") if has_straddle else 0.0, "held_shares": held_sh, "held_dd": None,
            "net_dd": None, "net_pct_nav": None, "band_usd": round(band_usd, 2) if band_usd is not None else None,
            "target_shares": None, "trade_shares": 0, "action": "hold", "breach": None, **mk}
        out.append(rec)
        if not has_straddle and held_sh == 0.0 and tk not in unknown_held:
            rec["status"] = "flat"
            continue
        # ── 缺数据：逐标的判，原因码来自 REASONS ──
        why = None
        if tk in unknown_held:
            why = "held_shares_unknown"
        elif nav is None:
            why = "nav_unavailable"
        elif has_straddle and rec["option_dd"] is None:
            why = (st or {}).get("reason") or "option_delta_unknown"
        elif mk["price"] is None:
            why = "price_stale" if mk["price_stale"] else "price_missing"
        if why is not None:
            rec["reason"] = why
            continue
        px = mk["price"]
        opt = rec["option_dd"] or 0.0
        rec["held_dd"] = round(held_sh * px, 2)
        net = opt + held_sh * px
        rec["net_dd"] = round(net, 2)
        rec["net_pct_nav"] = round(net / nav * 100.0, 4)
        rec["target_shares"] = round_away(-opt / px)
        if not has_straddle:
            # 跨式没了：目标 0，不看带、不看拉回方式——清仓
            rec["breach"] = True
            rec.update(status="exit", trade_shares=int(round_away(-held_sh)))
        else:
            rec["breach"] = abs(net) > band_usd + 1e-9
            if not rec["breach"]:
                rec["status"] = "inside"
                continue
            if rebalance_to == "center":
                shares = round_away(-net / px)
            else:                                   # edge：只拉回最近带边，股数向外取整（同 SPY 带，免得留半股在带外）
                excess = abs(net) - band_usd
                shares = int(math.ceil(excess / px - 1e-9))
                shares = -shares if net > 0 else shares
            rec["trade_shares"] = int(shares)
            rec["status"] = "hedge" if shares else "inside"
        rec["action"] = "buy" if rec["trade_shares"] > 0 else ("sell" if rec["trade_shares"] < 0 else "hold")
        if rec["trade_shares"] == 0 and rec["status"] == "exit":
            rec["status"] = "flat"
    return out


def with_costs(decisions: List[dict], cost_bps: float) -> List[dict]:
    """给每条决策补**估算**成交名义与成本（原地写、同时返回）。

    只有 status 为 hedge / exit、且有成交股数与价的才有名义：|成交股数| × 价；成本 = 名义 × bps / 1e4。
    其余（带内 / 未决 / 无跨式无持仓）恒为 0——不拿「没决定」当「免费」之外的任何东西。
    shadow 模式没有持仓账，`trade_shares` 是「若今天从空仓开始」的量，名义也是那个口径（上界，不是日常换手）。"""
    bps = check_cost_bps(cost_bps)
    for d in decisions:
        sh, px = _num(d.get("trade_shares")), _pos(d.get("price"))
        notional = abs(sh) * px if (d.get("status") in ("hedge", "exit") and sh and px) else 0.0
        d["trade_notional_usd"] = round(notional, 2)
        d["est_cost_usd"] = round(notional * bps / 1e4, 2)
    return decisions


def summarize(decisions: List[Mapping[str, Any]], nav: Optional[float] = None) -> dict:
    """计数 + 未决清单（进审计 / price_check / 告警的就是这一份）。
    决策带 `trade_notional_usd` / `est_cost_usd`（`with_costs`）时，再给成交笔数、名义与估算成本的合计；
    给了 NAV 才算占比（None ⇒ None，不拿 0 顶）。"""
    by: Dict[str, int] = {}
    for d in decisions:
        by[d["status"]] = by.get(d["status"], 0) + 1
    undecided = [{"ticker": d["ticker"], "reason": d["reason"], "detail": REASONS.get(d["reason"] or "", d["reason"])}
                 for d in decisions if d["status"] == "unknown"]
    out = {"n_names": sum(n for s, n in by.items() if s != "flat"), "by_status": by,
           "n_hedge": by.get("hedge", 0) + by.get("exit", 0), "n_inside": by.get("inside", 0),
           "n_undecided": len(undecided), "undecided": undecided}
    if any("trade_notional_usd" in d for d in decisions):
        notional = round(sum(_num(d.get("trade_notional_usd")) or 0.0 for d in decisions), 2)
        cost = round(sum(_num(d.get("est_cost_usd")) or 0.0 for d in decisions), 2)
        nv = _pos(nav)
        out.update(n_trades=sum(1 for d in decisions if (_num(d.get("trade_notional_usd")) or 0.0) > 0),
                   trade_notional_usd=notional, est_cost_usd=cost,
                   trade_notional_pct_nav=round(notional / nv * 100.0, 4) if nv else None,
                   est_cost_bps_nav=round(cost / nv * 1e4, 4) if nv else None)
    return out
