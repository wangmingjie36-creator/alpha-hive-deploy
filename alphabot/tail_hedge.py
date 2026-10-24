"""Alpha Bot 对冲页「尾部对冲候选」分析（v0.45.643）。纯函数：不读写文件、不联网、不 import 账本 / 卖权模块。

要回答的问题（用户 2026-10-24）：组合最差的压力格是「现货大跌 + IV 上升」（2026-10-23 审计：现货 −10%、IV +20 点，
约 −23% NAV），SPY 股票对冲管不到它（只管 Delta）。**SPY 认沽后比例价差**（卖 1 张较近的认沽、买 `ratio` 张更远的认沽，
买多卖少）在这个角落里是净多 Gamma、净多 Vega——这页把它摆出来：把候选叠到组合的压力网格上，看**最差格、别的格、
Greeks** 各变了多少，以及到期盈亏图与成本。

⚠️ 不要和「前比例价差」（卖多买少）混：那是收权利金的卖波动率结构，下跌时短腿裸露，在这个角落里只会**加重**亏损。

**只读、只摆事实、没有推荐 / 胜出 / 综合分字段**（`tests/test_alphabot_tail_hedge.py` 钉住键名）。理由：
  · 行权价档位、1:2 比例、规模规则全是主观常数（`RULES`），**没有任何前向证据**说哪个更好；
  · 压力网格建模不了它的全部价值，也夸大不了它的全部代价（见 `CAVEATS`）；
  · 候选按固定网格顺序排（先短腿档、再长腿档），**不按效果排序**——按效果排就成了对同一份历史拟合出来的「最优」。
这是分析面板，不是对冲指令：不写账本、不成交、不进评分。要做成纸面成交是 Phase C，另开预注册。

数据来源（均由服务层传入）
------------------------
  · `audit`：对冲账本**最近一份放行的审计**（`portfolio_greeks.load_history` 的 `audits`，由 `ledger_view` 给）——取它的
    `stress`（`portfolio_greeks.stress_table` 的输出：现货 × IV 联合网格的组合 P&L）、`aggregate.pct_nav`（组合 Greeks 占
    NAV 的 %）、NAV、SPY 价。审计当天若已按建议成交了 SPY，基线按成交后算（`baseline.adjusted_for_trade`），与
    `aggregate_after` 同口径；
  · `chain`：SPY 的 CBOE 全链（服务层取链入口给的原始合约列表形状：`contracts` / `underlying_price` / `vintage_date` /
    `session_live`）。候选的行权价、报价、IV、Greeks 都来自它。

读数口径
--------
  · **成交价**：卖出按 bid、买入按 ask（跨点差，与账本约定一致）；`cross_spread_usd` 单列「相对中间价多付了多少」。
  · **压力网格**：与 `portfolio_greeks.stress_table` 同一套——现货冲击 `spot_pct` 是 SPY 本身的涨跌（SPY 的 β = 1），
    IV 冲击是**每张合约自己的 IV 平行平移**，T = dte/365，**瞬时冲击（不走时间）**。候选单格 P&L =
    Σ 张数 × 100 × (BS(冲击后) − 成交价)，所以 (0,0) 格不是 0：它是「成交价 vs BS 价」的模型基差（点差 + 模型差）。
  · **到期盈亏**：Σ 张数 × 100 × max(K − S, 0) − 成交成本，分段线性，最大亏损点与盈亏平衡点是精确解（不是数值扫描）。
  · **规模**：认沽结构只能覆盖**下行侧**，所以只对基线的「下行侧最差格」（现货冲击 < 0 的格里最差的）定规模：
    使候选在该格的盈亏 ≈ 抵消那格亏损的 `RULES["offset_target"]`，四舍五入到整数单位（1 单位 = 卖 1 张 + 买 `ratio` 张）。
    该格候选不赚钱（`no_help_at_worst_cell`）或下行侧本来没亏损 ⇒ 不定规模、按 1 单位显示（`sizing.basis =
    "one_unit_fallback"`）。两道上限：张数 ≤ 该腿未平仓量的 `max_contracts_pct_oi`（未平仓量缺失 / 为 0 ⇒ 视为未知、不套）；
    卖出腿名义额 ≤ NAV 的 `max_short_notional_pct_nav`%。**`sizing.offset_achieved` 永远给**：定完规模、套完上限后，
    候选在目标格实际抵消了多少（可以远低于目标——小账户的 SPY 期权单位太大，这本身就是读数）。
  · **基线最差格在上行一侧**（`worst_cell_uncovered`）：本组合的做空 Gamma / Vega 是双向的——大幅上涨 + IV 上升同样亏；
    认沽结构对那一侧没有帮助，页面把它与下行侧并列，不把「最差格改善了」说成「尾部覆盖了」。
  · Greeks 来自 CBOE（每股）：$Delta = qty×delta×S；$Gamma(1%) = ½·qty·gamma·(0.01·S)²；$Vega/点 = qty×vega；
    $Theta/日 = qty×theta。对冲后 = 基线占 NAV 的 % + 候选 $ ÷ NAV。

判不了的一律是 None + 原因码（`REASONS`），不是 0。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

VERSION = "tail_hedge_v1"

#: 全是**主观常数、未经前向检验**（页面与帮助页原样显示这一句）。改任何一项 ⇒ 候选的含义变了，CHANGELOG 要写。
RULES: Dict[str, object] = {
    "target_dte": 45,             # 到期日取离它最近的（落在 [min_dte, max_dte] 内）
    "min_dte": 21,
    "max_dte": 90,
    "ratio": 2,                   # 买入张数 : 卖出张数 = ratio : 1（后比例，买多卖少）
    "short_delta_levels": (0.30, 0.20),            # 卖出腿：|delta| 最接近这些档
    "long_delta_levels": (0.15, 0.10, 0.05),       # 买入腿：|delta| 最接近这些档（必须比卖出腿更虚）
    "delta_tolerance": 0.05,      # 最接近的合约 |delta| 与目标档差超过它 ⇒ 这一档不可用
    "offset_target": 0.5,         # 规模：在基线「下行侧最差格」抵消这个比例的亏损
    "max_contracts_pct_oi": 0.02,  # 每腿张数 ≤ 该腿未平仓量的这个比例
    "max_short_notional_pct_nav": 100.0,   # 卖出腿名义额（张数×100×行权价）≤ NAV 的这个 %；1 单位就超 ⇒ 仍显示 1 单位并标 notional_over_cap
    "curve_spot_pct": tuple(range(-30, 11)),      # 到期盈亏图的横轴（SPY 涨跌 %）
}

#: 压力表里压 IV 下限，与 `portfolio_greeks.stress_table` 的 `IV_FLOOR` 同值（不 import，纯度）
IV_FLOOR = 0.0001
GREEK_KEYS = ("beta_dollar_delta", "dollar_delta", "gamma_dollar_per_1pct", "vega_dollar_per_pt", "theta_dollar_per_day")

#: 原因码唯一一张表（带 `:` 的码按前缀查，后缀是腿名 / 字段名）。页面只显示后端给的 `reason_zh`，前端不另抄。
REASONS: Dict[str, str] = {
    "audit_missing": "对冲账本里还没有放行的审计记录",
    "stress_missing": "最近一份审计里没有压力网格",
    "stress_unreadable": "审计里的压力网格读不了（格子缺值或不是数）",
    "nav_missing": "审计里没有 NAV",
    "chain_unavailable": "SPY 期权链取不到",
    "chain_empty": "SPY 期权链里没有可用合约",
    "no_spot": "期权链没有 SPY 价",
    "no_expiry_in_window": "没有落在到期窗口内的到期日",
    "no_put_for_level": "没有 |delta| 够接近目标档的认沽",
    "strikes_not_ordered": "买入腿的行权价不比卖出腿更低（档位撞在同一行权价上）",
    "quote_not_ok": "报价不合法（bid = 0 或缺值或 ask < bid）",
    "iv_missing": "合约没有 IV，没法重定价",
    "greeks_missing": "合约缺 Greeks（gamma / vega / theta），组合 Greeks 算不出",
    "no_help_at_worst_cell": "候选在基线最差格里不赚钱，没法按「抵消比例」定规模（按 1 单位显示）",
    "baseline_has_no_loss": "基线网格里没有亏损格，没法定规模（按 1 单位显示）",
    "exception": "计算时出错（详情在 Alpha Bot 日志）",
}
FLAGS: Dict[str, str] = {
    "wide_spread": "有腿的点差超过账本的可交易闸门（与卖权 / 跨式同一把尺）：按可成交价难以成交",
    "size_capped": "张数被该腿未平仓量的比例上限截住了",
    "notional_capped": "张数被卖出腿名义额上限（NAV 的百分比）截住了：实际抵消比例低于目标",
    "notional_over_cap": "1 单位的卖出腿名义额就已超过上限：按 1 单位显示，仅供看形状，不代表账户扛得住",
    "worst_cell_uncovered": "基线最差格在上行一侧（现货上涨 + IV 上升）：认沽结构覆盖不到它，这里只对下行侧最差格定规模",
    "oi_unknown": "有腿的未平仓量缺失或为 0（交易所 d+1 才发布新到期日的未平仓量），流动性上限没法套",
    "chain_not_audit_session": "期权链不是审计那一场的收盘链（盘中或日期不同）：基线是审计日收盘、候选是现在的报价，只能粗看",
    "spot_gap": "期权链的 SPY 价与审计里的 SPY 价差超过 0.5%：基线与候选不在同一个现货上",
    "baseline_partial": "基线压力网格本身是部分数据（有行被剔除）：最差格可能被低估",
}
#: 候选表「说明」列用的短标签（完整文案在 `FLAGS`，悬停显示）
FLAGS_SHORT: Dict[str, str] = {
    "wide_spread": "点差宽", "size_capped": "受未平仓量限", "notional_capped": "受名义额限", "notional_over_cap": "名义额超限",
    "oi_unknown": "未平仓量未知", "chain_not_audit_session": "链非审计场次", "spot_gap": "现货有差",
    "baseline_partial": "基线不全", "worst_cell_uncovered": "最差格在上行",
}
CAVEATS: Dict[str, str] = {
    "not_advice": "这是分析面板，不是对冲指令，也不是推荐：候选按固定网格排序、不按效果排序；没有综合分",
    "unverified_constants": "行权价档位、1:2 比例、到期窗口、抵消比例、流动性上限全是主观常数，没有任何前向证据说哪个更好",
    "parallel_iv_no_skew": "压力网格把每张合约自己的 IV 平行平移，不建模 skew 变化。下跌时远端认沽的 IV 通常涨得比近端多，"
                           "后比例的真实价值可能被低估（方向推断，幅度待验证）；也可能被高估——网格同样不建模流动性在下跌里变差",
    "instantaneous_no_time": "冲击是瞬时的、不走时间：theta 和到期前的路径依赖都不在网格里；到期盈亏图才是持有到期的结果",
    "valley": "后比例有一个亏损谷：到期时 SPY 恰好停在买入腿行权价附近亏得最多（见 `expiry.max_loss_usd`），"
              "不是「只赚不亏」；上行方向只亏掉净成本（若收到净权利金则不亏）",
    "no_margin": "保证金 / 占用资金没有建模：卖出腿是裸卖空认沽的一部分，券商的保证金要求取决于账户类型",
    "american_style": "SPY 期权是美式：卖出腿深度实值时有被提前指派的可能，本页没有建模",
    "execution_prices": "成交价按卖 bid、买 ask 估；实际可能在中间价附近成交，也可能因点差滑动更差",
}

# 网格 / 候选里的数值保留位数
USD_DP = 2
PCT_DP = 4


@dataclass(frozen=True)
class Deps:
    """外部函数由服务层注入（本模块不 import 账本 / 卖权 / greeks 模块）。"""
    bs_price: Callable[..., float]      # greeks_engine.bs_price(S, K, T, r, sigma, option_type)
    risk_free: float                    # portfolio_greeks.CONFIG["risk_free"]：与基线压力表同一个利率
    max_spread_pct: float               # 账本对短腿的点差闸门（小数）


def reason_zh(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    head, _, tail = str(code).partition(":")
    base = REASONS.get(head)
    if base is None:
        return None
    return f"{base}（{tail}）" if tail else base


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, dp=USD_DP) -> Optional[float]:
    f = _num(v)
    return None if f is None else round(f, dp)


def public_rules() -> dict:
    """给页面 / 帮助页的规则常量与文案表（前端不另抄）。"""
    return {"version": VERSION, "rules": {k: (list(v) if isinstance(v, tuple) else v) for k, v in RULES.items()
                                          if k != "curve_spot_pct"},
            "reasons": dict(REASONS), "flags": dict(FLAGS), "flags_short": dict(FLAGS_SHORT), "caveats": dict(CAVEATS),
            "grid_keys": list(GREEK_KEYS)}


def unavailable(code: str, **extra) -> dict:
    return {"available": False, "reason": code, "reason_zh": reason_zh(code), "version": VERSION, **extra}


# ── 基线：最近一份审计 ───────────────────────────────────────────────────────────────

def baseline_from_audit(day: Optional[str], audit: Optional[dict]) -> Tuple[Optional[dict], Optional[str]]:
    """审计 → 基线（压力格 / Greeks 占 NAV % / NAV / SPY 价）。返回 `(基线, None)` 或 `(None, 原因码)`。
    审计当天已成交 SPY（`executed`）⇒ 压力格按成交后调整、Greeks 取 `aggregate_after`（与审计自己的「交易后」同口径）。"""
    if not isinstance(audit, dict) or not day:
        return None, "audit_missing"
    stress = audit.get("stress")
    if not isinstance(stress, dict) or not stress.get("cells"):
        return None, "stress_missing"
    agg = audit.get("aggregate") or {}
    nav = _num(agg.get("nav")) or _num(audit.get("nav"))
    if not nav or nav <= 0:
        return None, "nav_missing"
    spy = _num(audit.get("spy_price"))
    trade = audit.get("executed_trade") if audit.get("executed") else None
    t_sh, t_px = _num((trade or {}).get("shares")), _num((trade or {}).get("price")) or spy
    adjusted = bool(trade) and t_sh is not None and t_px is not None
    cells: List[dict] = []
    for c in stress.get("cells") or []:
        sp, ivp, pnl = (_num((c or {}).get(k)) for k in ("spot_pct", "iv_pts", "pnl"))
        if sp is None or ivp is None or pnl is None:
            return None, "stress_unreadable"
        if adjusted:
            pnl += t_sh * t_px * sp / 100.0
        cells.append({"spot_pct": sp, "iv_pts": ivp, "pnl": pnl})
    if not cells:
        return None, "stress_missing"
    src = (audit.get("aggregate_after") if adjusted and audit.get("aggregate_after") else agg) or {}
    pct = {k: _num((src.get("pct_nav") or {}).get(k)) for k in GREEK_KEYS}
    worst = min(cells, key=lambda c: c["pnl"])
    down = [c for c in cells if c["spot_pct"] < 0]
    down_worst = min(down, key=lambda c: c["pnl"]) if down else None
    return {"date": day, "nav": nav, "spy_price": spy, "cells": cells,
            "spots": sorted({c["spot_pct"] for c in cells}), "ivs": sorted({c["iv_pts"] for c in cells}),
            "worst_cell": dict(worst), "downside_worst_cell": dict(down_worst) if down_worst else None, "pct_nav": pct, "adjusted_for_trade": adjusted,
            "partial": bool(stress.get("partial")), "excluded": list(stress.get("excluded") or [])}, None


# ── 链：选到期日与两条腿 ─────────────────────────────────────────────────────────────

def _is_put(c) -> bool:
    return str((c or {}).get("cp") or "").strip().upper() in ("P", "PUT")


def pick_expiry(contracts: Sequence[dict], rules: Optional[dict] = None) -> Optional[Tuple[str, int]]:
    """落在 [min_dte, max_dte] 内、离 target_dte 最近的到期日（并列取更近的）；窗口内没有 ⇒ None。"""
    r = rules or RULES
    lo, hi, tgt = r["min_dte"], r["max_dte"], r["target_dte"]
    seen: Dict[str, int] = {}
    for c in contracts or []:
        d, e = _num((c or {}).get("dte")), (c or {}).get("expiry")
        if e and d is not None and lo <= d <= hi and _is_put(c):
            seen[str(e)] = int(d)
    if not seen:
        return None
    exp = min(seen, key=lambda e: (abs(seen[e] - tgt), seen[e]))
    return exp, seen[exp]


def _put_book(contracts: Sequence[dict], expiry: str) -> List[dict]:
    return [c for c in contracts or [] if _is_put(c) and str(c.get("expiry")) == expiry
            and _num(c.get("strike")) is not None and _num(c.get("delta")) is not None]


def pick_put(book: Sequence[dict], level: float, tolerance: float) -> Optional[dict]:
    """|delta| 最接近 `level` 的认沽；差超过 `tolerance` ⇒ None（「最接近」≠「够近」）。"""
    best = None
    for c in book:
        gap = abs(abs(_num(c["delta"])) - level)
        if best is None or gap < best[0] or (gap == best[0] and _num(c["strike"]) > _num(best[1]["strike"])):
            best = (gap, c)
    return best[1] if best is not None and best[0] <= tolerance + 1e-9 else None


def _quote(c: dict) -> dict:
    bid, ask = _num(c.get("bid")), _num(c.get("ask"))
    ok = bid is not None and ask is not None and bid > 0 and ask >= bid
    mid = (bid + ask) / 2.0 if ok else None
    return {"bid": bid, "ask": ask, "mid": mid, "ok": ok,
            "spread_pct": (ask - bid) / mid if ok and mid else None}


def _leg(role: str, c: dict, qty_contracts: int) -> dict:
    q = _quote(c)
    return {"role": role, "symbol": c.get("symbol"), "strike": _num(c.get("strike")), "expiry": c.get("expiry"),
            "dte": _num(c.get("dte")), "delta": _num(c.get("delta")), "iv": _num(c.get("iv")),
            "gamma": _num(c.get("gamma")), "vega": _num(c.get("vega")), "theta": _num(c.get("theta")),
            "oi": _num(c.get("oi")), "bid": q["bid"], "ask": q["ask"], "mid": q["mid"], "quote_ok": q["ok"],
            "spread_pct": q["spread_pct"], "qty_contracts": qty_contracts,           # 带符号：卖出为负
            "qty_shares": qty_contracts * 100,
            "fill": (q["bid"] if qty_contracts < 0 else q["ask"]) if q["ok"] else None}


# ── 单个单位的盈亏、Greeks、压力格 ────────────────────────────────────────────────────

def unit_cost(legs: Sequence[dict]) -> Dict[str, float]:
    """1 单位的成本（美元，正 = 付出）：成交价口径、中间价口径、跨点差多付的。"""
    exe = sum(l["qty_shares"] * l["fill"] for l in legs)
    mid = sum(l["qty_shares"] * l["mid"] for l in legs)
    return {"executable": exe, "mid": mid, "cross_spread": exe - mid}


def expiry_payoff(legs: Sequence[dict], cost: float, S0: float) -> dict:
    """到期盈亏（美元，1 单位）：分段线性 ⇒ 最大亏损点与盈亏平衡点取精确解。
    结点 = {0, 各行权价, 1.4·S0}；在结点上算值，最低点即最大亏损，相邻结点变号处线性插值得盈亏平衡点。"""
    def pnl(S: float) -> float:
        return sum(l["qty_shares"] * max(l["strike"] - S, 0.0) for l in legs) - cost

    knots = sorted({0.0, 1.4 * S0, *(l["strike"] for l in legs)})
    vals = [pnl(k) for k in knots]
    i_min = min(range(len(knots)), key=lambda i: vals[i])
    roots: List[float] = []
    for (a, va), (b, vb) in zip(zip(knots, vals), zip(knots[1:], vals[1:])):
        if va == 0.0:
            roots.append(a)
        elif va * vb < 0:
            roots.append(a + (b - a) * (0.0 - va) / (vb - va))
    if vals and vals[-1] == 0.0:
        roots.append(knots[-1])
    return {"pnl": pnl, "max_loss_usd": vals[i_min], "at_spot": knots[i_min],
            "at_spot_pct": (knots[i_min] / S0 - 1.0) * 100.0, "breakevens": sorted(set(roots)),
            "upside_usd": vals[-1], "payoff_at_zero_usd": vals[0]}


def greeks_dollars(legs: Sequence[dict], S: float) -> Tuple[Optional[dict], Optional[str]]:
    """1 单位的组合 Greeks（美元）。缺任一腿的任一字段 ⇒ `(None, 'greeks_missing:<腿>.<字段>')`。"""
    tot = {"dollar_delta": 0.0, "gamma_dollar_per_1pct": 0.0, "vega_dollar_per_pt": 0.0, "theta_dollar_per_day": 0.0}
    for l in legs:
        for f in ("delta", "gamma", "vega", "theta"):
            if l.get(f) is None:
                return None, f"greeks_missing:{l['role']}.{f}"
        q = l["qty_shares"]
        tot["dollar_delta"] += q * l["delta"] * S
        tot["gamma_dollar_per_1pct"] += 0.5 * q * l["gamma"] * (0.01 * S) ** 2
        tot["vega_dollar_per_pt"] += q * l["vega"]
        tot["theta_dollar_per_day"] += q * l["theta"]
    tot["beta_dollar_delta"] = tot["dollar_delta"]          # SPY 的 β = 1
    return tot, None


def unit_cells(legs: Sequence[dict], S: float, dte: float, deps: Deps, spots: Sequence[float],
               ivs: Sequence[float], cost: float) -> Dict[Tuple[float, float], float]:
    """1 单位在每个 (现货冲击, IV 冲击) 格的 P&L（美元）：Σ 张数×100×BS(冲击后) − 成交成本。"""
    T = max(float(dte), 0.0) / 365.0
    out: Dict[Tuple[float, float], float] = {}
    for sp in spots:
        S1 = S * (1.0 + sp / 100.0)
        for ivp in ivs:
            v = 0.0
            for l in legs:
                iv1 = max(l["iv"] + ivp / 100.0, IV_FLOOR)
                v += l["qty_shares"] * deps.bs_price(S1, l["strike"], T, deps.risk_free, iv1, "put")
            out[(sp, ivp)] = v - cost
    return out


# ── 规模 ────────────────────────────────────────────────────────────────────────────

def size_units(target: dict, unit_at_target: Optional[float], legs: Sequence[dict], nav: float,
               rules: Optional[dict] = None) -> dict:
    """规模：在目标格（基线下行侧最差格）使候选盈亏 ≈ 抵消那格亏损的 `offset_target`，整数单位，≥ 1；再套两道上限。
    不能定 ⇒ 1 单位 + 原因（`basis = one_unit_fallback`）。`offset_achieved` 在 `_candidate` 里补（要用最终单位数）。"""
    r = rules or RULES
    w = _num(target.get("pnl"))
    short = next(l for l in legs if l["role"] == "short")
    unit_notional = abs(short["qty_shares"]) * short["strike"]
    out = {"basis": "one_unit_fallback", "n_units": 1, "reason": None, "capped": False, "oi_unknown": False,
           "notional_capped": False, "notional_over_cap": False,
           "target_cell": {"spot_pct": target.get("spot_pct"), "iv_pts": target.get("iv_pts"), "base_pnl": _r(w)},
           "offset_target": r["offset_target"]}
    n_cap_notional = int(math.floor(r["max_short_notional_pct_nav"] / 100.0 * nav / unit_notional))
    if n_cap_notional < 1:
        out["notional_over_cap"] = True
    if w is None or w >= 0:
        out["reason"] = "baseline_has_no_loss"
        return out
    if unit_at_target is None or unit_at_target <= 0:
        out["reason"] = "no_help_at_worst_cell"
        return out
    n = max(1, int(round(r["offset_target"] * abs(w) / unit_at_target)))
    out.update(basis="offset_target", n_units=n)
    caps = []
    for l in legs:
        if l["oi"] is None or l["oi"] <= 0:
            out["oi_unknown"] = True
            continue
        caps.append(int(math.floor(r["max_contracts_pct_oi"] * l["oi"] / abs(l["qty_contracts"]))))
    if caps and n > min(caps):
        out["capped"] = True
        out["n_units"] = n = max(1, min(caps))
    if n > max(n_cap_notional, 1):
        out["notional_capped"] = True
        out["n_units"] = max(n_cap_notional, 1)
    return out


# ── 候选 ────────────────────────────────────────────────────────────────────────────

def _candidate(cid: str, short_level: float, long_level: float, c_short: dict, c_long: dict, S: float, dte: float,
               base: dict, deps: Deps, rules: dict) -> dict:
    ratio = int(rules["ratio"])
    legs = [_leg("short", c_short, -1), _leg("long", c_long, ratio)]
    head = {"id": cid, "ratio": ratio, "short_level": short_level, "long_level": long_level,
            "legs": [{k: (_r(v, 4) if isinstance(v, float) else v) for k, v in l.items()} for l in legs]}
    bad = next((l for l in legs if not l["quote_ok"]), None)
    if bad:
        return {**head, "available": False, "reason": f"quote_not_ok:{bad['role']}", "flags": []}
    bad = next((l for l in legs if not l["iv"] or l["iv"] <= 0), None)
    if bad:
        return {**head, "available": False, "reason": f"iv_missing:{bad['role']}", "flags": []}
    flags: List[str] = [f"wide_spread:{l['role']}" for l in legs
                        if l["spread_pct"] is not None and l["spread_pct"] > deps.max_spread_pct]
    cost = unit_cost(legs)
    pay = expiry_payoff(legs, cost["executable"], S)
    cells_u = unit_cells(legs, S, dte, deps, base["spots"], base["ivs"], cost["executable"])
    target = base["downside_worst_cell"] or base["worst_cell"]
    sizing = size_units(target, cells_u.get((target["spot_pct"], target["iv_pts"])), legs, base["nav"], rules)
    n = sizing["n_units"]
    for key, flag in (("capped", "size_capped"), ("oi_unknown", "oi_unknown"), ("notional_capped", "notional_capped"),
                      ("notional_over_cap", "notional_over_cap")):
        if sizing[key]:
            flags.append(flag)
    tw = _num(target["pnl"])
    achieved = (n * cells_u[(target["spot_pct"], target["iv_pts"])] / abs(tw)) if tw and tw < 0 else None
    short_notional = n * abs(legs[0]["qty_shares"]) * legs[0]["strike"]
    nav = base["nav"]
    cells = []
    for c in base["cells"]:
        hedge = n * cells_u[(c["spot_pct"], c["iv_pts"])]
        cells.append({"spot_pct": c["spot_pct"], "iv_pts": c["iv_pts"], "base": _r(c["pnl"]), "hedge": _r(hedge),
                      "after": _r(c["pnl"] + hedge)})
    worst_after = min(cells, key=lambda c: c["after"])
    worst_hedge = min(cells, key=lambda c: c["hedge"])
    down_cells = [c for c in cells if c["spot_pct"] < 0]
    down_after = min(down_cells, key=lambda c: c["after"]) if down_cells else None
    g_unit, g_reason = greeks_dollars(legs, S)
    greeks = None
    if g_unit is not None:
        greeks = {"per_unit_usd": {k: _r(v) for k, v in g_unit.items()},
                  "sized_usd": {k: _r(n * v) for k, v in g_unit.items()},
                  "before_pct_nav": {k: _r(base["pct_nav"].get(k), PCT_DP) for k in GREEK_KEYS},
                  "after_pct_nav": {k: (_r(base["pct_nav"][k] + n * g_unit[k] / nav * 100.0, PCT_DP)
                                        if base["pct_nav"].get(k) is not None else None) for k in GREEK_KEYS}}
    curve = [{"spot_pct": sp, "pnl": _r(n * pay["pnl"](S * (1.0 + sp / 100.0)))} for sp in rules["curve_spot_pct"]]
    return {
        **head, "available": True, "reason": None, "reason_zh": None, "flags": flags, "dte": int(dte),
        "unit": {"cost_executable_usd": _r(cost["executable"]), "cost_mid_usd": _r(cost["mid"]),
                 "cross_spread_usd": _r(cost["cross_spread"]),
                 "is_credit": cost["executable"] < 0},
        "sizing": {**sizing, "reason_zh": reason_zh(sizing["reason"]), "contracts_short": n,
                   "contracts_long": n * ratio, "offset_achieved": _r(achieved, 4),
                   "short_notional_usd": _r(short_notional), "short_notional_pct_nav": _r(short_notional / nav * 100.0, 2),
                   "net_cost_usd": _r(n * cost["executable"]),
                   "net_cost_pct_nav": _r(n * cost["executable"] / nav * 100.0, PCT_DP)},
        "expiry": {"max_loss_usd": _r(n * pay["max_loss_usd"]), "at_spot": _r(pay["at_spot"]),
                   "at_spot_pct": _r(pay["at_spot_pct"], 2), "breakevens": [_r(b) for b in pay["breakevens"]],
                   "upside_usd": _r(n * pay["upside_usd"]), "curve": curve},
        "effect": {"cells": cells, "worst_before": {k: base["worst_cell"][k] for k in ("spot_pct", "iv_pts")}
                   | {"pnl": _r(base["worst_cell"]["pnl"])},
                   "worst_after": {k: worst_after[k] for k in ("spot_pct", "iv_pts")} | {"pnl": worst_after["after"]},
                   "worst_hedge_cell": {k: worst_hedge[k] for k in ("spot_pct", "iv_pts")} | {"pnl": worst_hedge["hedge"]},
                   "downside_worst_after": (({k: down_after[k] for k in ("spot_pct", "iv_pts")} | {"pnl": down_after["after"]})
                                            if down_after else None),
                   "downside_worst_before": (({k: target[k] for k in ("spot_pct", "iv_pts")} | {"pnl": _r(target["pnl"])})
                                             if base["downside_worst_cell"] else None),
                   "n_cells_worse": sum(1 for c in cells if c["hedge"] < 0), "n_cells": len(cells),
                   "worst_after_pct_nav": _r(worst_after["after"] / nav * 100.0, 2),
                   "worst_before_pct_nav": _r(base["worst_cell"]["pnl"] / nav * 100.0, 2)},
        "greeks": greeks, "greeks_reason": g_reason, "greeks_reason_zh": reason_zh(g_reason),
    }


def build(day: Optional[str], audit: Optional[dict], chain: Optional[dict], deps: Deps,
          rules: Optional[dict] = None) -> dict:
    """主入口：审计基线 × SPY 链 → 候选清单。永远返回 dict（不可得时 `available=False` + 原因码）。
    候选顺序 = 固定网格（短腿档 × 长腿档），**不按效果排序**；没有推荐 / 综合分字段。"""
    r = rules or RULES
    base, why = baseline_from_audit(day, audit)
    if base is None:
        return unavailable(why)
    if not isinstance(chain, dict) or not chain.get("contracts"):
        return unavailable("chain_empty" if isinstance(chain, dict) else "chain_unavailable",
                           baseline=_baseline_brief(base))
    S = _num(chain.get("underlying_price"))
    if not S or S <= 0:
        return unavailable("no_spot", baseline=_baseline_brief(base))
    contracts = chain["contracts"]
    pick = pick_expiry(contracts, r)
    if pick is None:
        return unavailable("no_expiry_in_window", baseline=_baseline_brief(base))
    expiry, dte = pick
    book = _put_book(contracts, expiry)
    flags: List[str] = []
    if chain.get("session_live") or str(chain.get("vintage_date") or "") != str(day):
        flags.append("chain_not_audit_session")
    gap = None
    if base["spy_price"]:
        gap = (S / base["spy_price"] - 1.0) * 100.0
        if abs(gap) > 0.5:
            flags.append("spot_gap")
    if base["partial"]:
        flags.append("baseline_partial")
    if base["worst_cell"]["spot_pct"] >= 0:
        flags.append("worst_cell_uncovered")
    cands: List[dict] = []
    tol = float(r["delta_tolerance"])
    for sl in r["short_delta_levels"]:
        for ll in r["long_delta_levels"]:
            if ll >= sl:
                continue
            cid = f"{int(round(sl * 100)):02d}x{int(round(ll * 100)):02d}"
            cs, cl = pick_put(book, sl, tol), pick_put(book, ll, tol)
            if cs is None or cl is None:
                who = "short" if cs is None else "long"
                cands.append({"id": cid, "ratio": int(r["ratio"]), "short_level": sl, "long_level": ll,
                              "available": False, "reason": f"no_put_for_level:{who}", "flags": []})
                continue
            if not _num(cl["strike"]) < _num(cs["strike"]):
                cands.append({"id": cid, "ratio": int(r["ratio"]), "short_level": sl, "long_level": ll,
                              "available": False, "reason": "strikes_not_ordered", "flags": []})
                continue
            cands.append(_candidate(cid, sl, ll, cs, cl, S, dte, base, deps, r))
    for c in cands:
        c.setdefault("reason_zh", reason_zh(c.get("reason")))
    return {"available": True, "reason": None, "reason_zh": None, "version": VERSION,
            "baseline": _baseline_brief(base),
            "chain": {"underlying": "SPY", "spot": _r(S), "vintage_date": chain.get("vintage_date"),
                      "session_live": bool(chain.get("session_live")), "expiry": expiry, "dte": dte,
                      "spot_vs_audit_pct": _r(gap, 3), "n_puts_in_expiry": len(book)},
            "grid": {"spot_pct": base["spots"], "iv_pts": base["ivs"]},
            "flags": flags, "candidates": cands, "rules": public_rules()["rules"]}


def _baseline_brief(base: dict) -> dict:
    w = base["worst_cell"]
    return {"date": base["date"], "nav": _r(base["nav"]), "spy_price": _r(base["spy_price"]),
            "worst_cell": {"spot_pct": w["spot_pct"], "iv_pts": w["iv_pts"], "pnl": _r(w["pnl"]),
                           "pct_nav": _r(w["pnl"] / base["nav"] * 100.0, 2)},
            "downside_worst_cell": ({"spot_pct": base["downside_worst_cell"]["spot_pct"],
                                     "iv_pts": base["downside_worst_cell"]["iv_pts"],
                                     "pnl": _r(base["downside_worst_cell"]["pnl"]),
                                     "pct_nav": _r(base["downside_worst_cell"]["pnl"] / base["nav"] * 100.0, 2)}
                                    if base["downside_worst_cell"] else None),
            "pct_nav": {k: _r(v, PCT_DP) for k, v in base["pct_nav"].items()},
            "adjusted_for_trade": base["adjusted_for_trade"], "partial": base["partial"],
            "excluded": base["excluded"][:6]}
