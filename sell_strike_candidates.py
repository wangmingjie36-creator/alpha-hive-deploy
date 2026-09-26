"""卖权行权价选择器 · 候选构造层（v0.45.333）：到期日 / delta 梯子 / 结构报价 / 环境路由 / 财报标注。

零 I/O，只 import sell_strike_levels / math / typing / datetime。输入是
`cboe_options.fetch_cboe_raw_contracts` 产出的合约列表（iv 为小数、dte 为纯 date 相减的日历日）
与 `sell_strike_levels.level_map` 的 zero_gamma 视图。

分工（为什么这一层不碰 GEX 的数值）：
  · **选行权价**只用 delta / N(d2) / σ 距离 —— 证据等级 A 的那部分（method-evidence.md）。
  · **GEX 只做路由**：`route()` 决定「取梯子的哪一档」，不产出任何加减分数字，也不改梯子本身。
    所以置换 route 标签后，反事实结果可以直接从同一份梯子重算（预注册检验靠的就是这个）。

口径约定：
  · 报价闸同 `cboe_options._qs_contract`：quote_ok = bid>0 且 ask≥bid（bid=0 的 mid 是「一半的 ask」，
    不是市价）。
  · 入场成交同 `options_paper_leg.entry_fills`：**卖按 bid、买按 ask**。
  · 所有金额为**每股**口径（×100 才是每张）。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Optional, Tuple

import sell_strike_levels as L

CANDIDATES_SCHEMA_VERSION = 1
# 周度 [7,21)、月度 [21,45]：dte=21 只属于月度，两档不共享任何到期日。
# 预注册文档（experiments/sell_strike_routing_prereg.md）引用同一组值，测试钉住。
TENORS = {"monthly": {"dte_lo": 21, "dte_hi": 45, "hi_inclusive": True,  "target_dte": 30},
          "weekly":  {"dte_lo": 7,  "dte_hi": 21, "hi_inclusive": False, "target_dte": 14}}
LADDER_DELTAS = (0.10, 0.16, 0.20, 0.25, 0.30)   # |Δ|
# 占档顺序（登记前定稿，2026-09-23）：主档 0.20（BASE_RUNG = 预注册唯一结果档）与远档 0.10
# （FAR_RUNG）先占，辅档 0.16/0.25/0.30 让位。按 LADDER_DELTAS 升序占时，同一张合约若同时是
# 0.16 与 0.20 的最近者会被 0.16 抢走 ⇒ 0.20 给 None。合成链实测抢占率周度 26.9%、月度 11.9%，
# 且强烈依赖 IV（17DTE：IV 20% 时 48.8%、60% 时 3.4%）—— flag 与 IV 相关 ⇒ 检验结果档的缺失率
# 两臂不同，是选择偏差，不是噪声。
LADDER_FILL_ORDER = (0.20, 0.10, 0.16, 0.25, 0.30)
if sorted(LADDER_FILL_ORDER) != sorted(LADDER_DELTAS):
    # 漏排的档永远不会被填、也不会有 reason —— 静默空档。import 时就炸。
    raise RuntimeError(f"LADDER_FILL_ORDER {LADDER_FILL_ORDER} 必须是 LADDER_DELTAS 的一个排列")
DELTA_TOL = 0.05
# 价差保护腿：离短腿 ≥ 0.5×(S·σ_short·√T) 的最近挂牌行权价（更价外方向）
WING_WIDTH_SIGMA = 0.5
# 短腿点差/中价 > 25% ⇒ 该档 unquotable（与 quote_ok 分开记原因）
MAX_SPREAD_PCT = 0.25
ROUTE_RULE_VERSION = 1
ROUTE_VIEW = "le_45dte"
FLIP_BUFFER_PCT = 3.0
# 扫描最少合约数（登记前定稿，2026-09-23，并入 route 规则 v1）：ROUTE_VIEW 视图里进了 zero gamma
# 扫描的合约数（有 IV 且 OI>0 = zero_gamma["n_contracts"]）低于它 ⇒ route 判 unavailable。
# 评审实测：只剩 2 张合约有 IV 时扫描照样给出符号与零点、行照样进检验 —— 两三张合约的
# 「政体」不是政体。20 张 ≈ 一个到期日上 10 个行权价的 call+put。
MIN_SWEEP_CONTRACTS = 20
BASE_RUNG = 0.20
FAR_RUNG = 0.10
STRUCTURES = ("short_put", "short_call", "bull_put_spread", "bear_call_spread",
              "strangle", "iron_condor")
# 报价输出的小数位：金额（credit / max_loss / collateral / breakevens，每股）4 位、收益率 6 位。
# bid/ask 相减的浮点噪声（1.45 − 0.13 = 1.3199999999999998，实测）原样外露到 MCP / 报告；
# 更要紧的是 credit 的正负判定要在取整**之后**：铁鹰两翼恰好相抵时浮点和可能是 +1.4e-17，
# 不取整就成了「可报价、credit≈0」的结构。
MONEY_DP = 4
YIELD_DP = 6

_SIDES = {"put": "P", "call": "C"}
# 「最接近」≠「够近」：边界上 |Δ|=0.15 对 0.10 算出 0.05000000000000002，直接比会误拒。
_TOL_EPS = 1e-9
_STRUCT_LEGS = {
    "short_put": (("put", "short"),),
    "short_call": (("call", "short"),),
    "bull_put_spread": (("put", "short"), ("put", "wing")),
    "bear_call_spread": (("call", "short"), ("call", "wing")),
    "strangle": (("put", "short"), ("call", "short")),
    "iron_condor": (("put", "short"), ("put", "wing"), ("call", "short"), ("call", "wing")),
}


def _num(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(x) -> Optional[float]:
    f = _num(x)
    return f if (f is not None and f > 0) else None


def rung_key(rung) -> str:
    """档位 → 两位小数字符串键（"0.10"）。不是 LADDER_DELTAS 里的值 ⇒ ValueError（拼错要当场炸）。"""
    f = _num(rung)
    if f is None:
        raise ValueError(f"rung 必须是数值，收到 {rung!r}")
    key = f"{f:.2f}"
    if key not in {f"{d:.2f}" for d in LADDER_DELTAS}:
        raise ValueError(f"rung {rung!r} 不在 LADDER_DELTAS {LADDER_DELTAS} 里")
    return key


# ─────────────────────────────── 到期日

def _tenor_cfg(tenor: str) -> dict:
    if tenor not in TENORS:
        raise ValueError(f"tenor 只接受 {tuple(TENORS)}，收到 {tenor!r}")
    return TENORS[tenor]


def in_tenor(dte, tenor: str) -> bool:
    """dte 是否落在该档窗口内（上界开闭按 TENORS[tenor]["hi_inclusive"]）。"""
    cfg = _tenor_cfg(tenor)
    d = _num(dte)
    if d is None or d < cfg["dte_lo"]:
        return False
    return d <= cfg["dte_hi"] if cfg["hi_inclusive"] else d < cfg["dte_hi"]


def select_expiry(contracts: List[dict], tenor: str) -> Optional[Tuple[str, int]]:
    """窗口内的到期日取 |dte − target_dte| 最小者，平手取**更远**的（同 `select_quote_set`：
    更远的 theta 更平缓）。窗口里一个到期日都没有 ⇒ None。未知 tenor ⇒ ValueError。"""
    cfg = _tenor_cfg(tenor)
    cands = set()
    for c in contracts or []:
        if not isinstance(c, dict) or not c.get("expiry"):
            continue
        d = _num(c.get("dte"))
        if d is None or d != int(d) or not in_tenor(d, tenor):
            continue
        cands.add((str(c["expiry"]), int(d)))
    if not cands:
        return None
    return min(cands, key=lambda ed: (abs(ed[1] - cfg["target_dte"]), -ed[1], ed[0]))


# ─────────────────────────────── 单腿

def quote_leg(c: dict) -> dict:
    """报价闸：quote_ok = bid>0 且 ask≥bid（同 `_qs_contract`）；mid / spread_pct 仅 quote_ok 时算。"""
    bid, ask = _num((c or {}).get("bid")), _num((c or {}).get("ask"))
    ok = bid is not None and ask is not None and bid > 0 and ask >= bid
    mid = (bid + ask) / 2.0 if ok else None
    spread = (ask - bid) / mid if (ok and mid) else None
    return {"bid": bid, "ask": ask, "mid": mid, "spread_pct": spread, "quote_ok": ok}


def _contract_delta(c: dict, S: float, T: Optional[float], r: float) -> Tuple[Optional[float], str]:
    """(Δ, 来源)。CBOE delta 优先；为 None / 0 / 与方向符号矛盾（put 给了正 Δ）⇒ BS 兜底；都没有 ⇒ (None, "none")。

    CBOE 的 delta 带股息与美式行权（BS 这里 q=0），股息股选 delta 应该信它；兜底只在它缺失时用。
    """
    cp = c.get("cp")
    d = _num(c.get("delta"))
    if d is not None and d != 0.0 and ((cp == "C" and d > 0) or (cp == "P" and d < 0)):
        return d, "cboe"
    iv = _pos(c.get("iv"))
    if iv is not None and T is not None:
        bs = L.bs_delta(S, c.get("strike"), T, r, iv, cp)
        if bs is not None:
            return bs, "bs"
    return None, "none"


def _leg(c: dict, S: float, r: float) -> dict:
    K = _pos(c.get("strike"))
    cp = c.get("cp")
    T = L.year_fraction(c.get("dte"))
    iv = _pos(c.get("iv"))
    delta, dsrc = _contract_delta(c, S, T, r)
    q = quote_leg(c)
    return {
        "symbol": c.get("symbol"),
        "strike": K,
        "cp": cp,
        "delta": delta,
        "delta_source": dsrc,
        "iv": iv,
        "gamma": _num(c.get("gamma")),
        "theta": _num(c.get("theta")),
        "oi": _num(c.get("oi")),
        "bid": q["bid"],
        "ask": q["ask"],
        "mid": q["mid"],
        "spread_pct": q["spread_pct"],
        "quote_ok": q["quote_ok"],
        # 到期 ITM 的**风险中性**概率 N(d2)/N(−d2)，不是 |Δ|（见 sell_strike_levels.itm_probability）
        "itm_prob": L.itm_probability(S, K, T, r, iv, cp) if iv is not None else None,
        "sigma_distance": L.sigma_distance(S, K, T, iv) if iv is not None else None,
    }


def _side_book(contracts: List[dict], expiry: str, cp: str) -> Dict[float, dict]:
    """该到期日该方向的 {strike: 合约}。同行权价重复行取 OI 最大的（同 `_qs_pick_row`）。"""
    book: Dict[float, dict] = {}
    for c in contracts or []:
        if not isinstance(c, dict) or c.get("expiry") != expiry or c.get("cp") != cp:
            continue
        K = _pos(c.get("strike"))
        if K is None:
            continue
        if K not in book or (_num(c.get("oi")) or 0.0) > (_num(book[K].get("oi")) or 0.0):
            book[K] = c
    return book


def _wing_for(book: Dict[float, dict], side: str, short: dict, S: float, T: Optional[float],
              r: float) -> Tuple[Optional[dict], Optional[str]]:
    """沿更价外方向，离短腿 ≥ WING_WIDTH_SIGMA·S·σ_short·√T 的**最近**挂牌行权价。"""
    iv = _pos(short.get("iv"))
    K_s = _pos(short.get("strike"))
    if iv is None or T is None or K_s is None:
        return None, "no_short_iv_for_wing_width"
    width = WING_WIDTH_SIGMA * S * iv * math.sqrt(T)
    if side == "put":
        pool = [k for k in book if K_s - k >= width - _TOL_EPS]
        pick = max(pool) if pool else None
    else:
        pool = [k for k in book if k - K_s >= width - _TOL_EPS]
        pick = min(pool) if pool else None
    if pick is None:
        return None, "no_wing_strike"
    return _leg(book[pick], S, r), None


def build_ladder(contracts: List[dict], S, expiry: str, *, r: float = L.RISK_FREE_RATE) -> dict:
    """put / call 两侧 × LADDER_DELTAS 各档：|Δ| 最接近目标且差 ≤ DELTA_TOL 的合约 + 它的保护腿。

    · Δ 取 CBOE，None 时 BS 兜底（记 delta_source）；两者都没有的合约不参与选档。
    · 同一张合约不得占两档：按 LADDER_FILL_ORDER 占档（主档 0.20、远档 0.10 先占，
      辅档让位——理由见该常量注释），后来的那档给 None + reason `same_contract_as_<先占的档>`
      （不改选次优——该档没有新增信息，硬塞次优会让两档看起来都有观测，其实是同一个点附近的
      两次取样）。输出的档位键顺序仍按 LADDER_DELTAS。
    · 平手（|Δ| 与目标等距）取更价外的那张（|Δ| 更小）。
    返回 `{"expiry", "dte", "underlying_price", "put": {"0.10": {"short","wing","reasons"}, ...}, "call": {...}}`。
    `expiry/dte/underlying_price` 三个顶层键是 SPEC 之外加的：`structure_quote` 要 dte 年化、
    short_call 要 S 作备兑抵押，不带上就得让调用方再传一遍、两处口径可能不一。
    """
    Sv = _pos(S)
    dte = None
    for c in contracts or []:
        if isinstance(c, dict) and c.get("expiry") == expiry and _num(c.get("dte")) is not None:
            dte = int(_num(c.get("dte")))
            break
    T = L.year_fraction(dte) if dte is not None else None
    out = {"expiry": expiry, "dte": dte, "underlying_price": Sv}
    for side, cp in _SIDES.items():
        book = _side_book(contracts, expiry, cp)
        priced = []                       # (|Δ|, strike, leg)
        if Sv is not None:
            for K, c in book.items():
                d, _src = _contract_delta(c, Sv, T, r)
                if d is not None:
                    priced.append((abs(d), K, c))
        # 键按 LADDER_DELTAS 顺序建好，再按 LADDER_FILL_ORDER 填：展示顺序与占档优先级是两回事
        rungs: Dict[str, dict] = {f"{d:.2f}": {"short": None, "wing": None, "reasons": []}
                                  for d in LADDER_DELTAS}
        taken: Dict[float, str] = {}      # strike → 先占的档
        for target in LADDER_FILL_ORDER:
            key = f"{target:.2f}"
            slot = rungs[key]
            if Sv is None:
                slot["reasons"].append("underlying_price_unavailable")
                continue
            if not priced:
                slot["reasons"].append("no_delta_available")
                continue
            dist, absd, K, c = min(((abs(a - target), a, k, cc) for a, k, cc in priced),
                                   key=lambda t: (t[0], t[1]))
            if dist - DELTA_TOL > _TOL_EPS:
                slot["reasons"].append(f"no_delta_within_tol:nearest={absd:.4f}")
                continue
            if K in taken:
                slot["reasons"].append(f"same_contract_as_{taken[K]}")
                continue
            taken[K] = key
            short = _leg(c, Sv, r)
            slot["short"] = short
            wing, why = _wing_for(book, side, short, Sv, T, r)
            slot["wing"] = wing
            if why:
                slot["reasons"].append(why)
        out[side] = rungs
    return out


# ─────────────────────────────── 结构报价

def _norm_rungs(rung) -> Optional[Dict[str, str]]:
    """rung 可以是单个档位（两侧同档）或 {"put": x, "call": y}（route 两侧可能不同档）。None ⇒ None。"""
    if rung is None:
        return None
    if isinstance(rung, dict):
        return {s: (rung_key(rung[s]) if rung.get(s) is not None else None) for s in _SIDES}
    k = rung_key(rung)
    return {"put": k, "call": k}


def _rnd(x: Optional[float], nd: int) -> Optional[float]:
    """四舍五入到 nd 位；None 原样；`+ 0.0` 把 −0.0 归一成 0.0（JSON 里不出现 -0.0）。"""
    return None if x is None else round(float(x), nd) + 0.0


def _empty_quote(structure, rung, reasons, legs=None) -> dict:
    return {"structure": structure, "rung": rung, "legs": legs or [], "credit": None,
            "max_loss": None, "collateral": None, "yield_raw": None, "yield_annualized": None,
            "breakevens": [], "quotable": False, "reason": reasons[0] if reasons else None,
            "reasons": list(reasons), "notes": []}


def structure_quote(ladder: dict, structure: str, rung) -> dict:
    """按入场口径组合一个结构（**卖按 bid、买按 ask**），每股口径。

    返回 `{"structure","rung","legs","credit","max_loss","collateral","yield_raw",
    "yield_annualized","breakevens","quotable","reason","reasons","notes"}`：
      short_put        credit=bid；collateral=K（现金担保）；max_loss=K−credit；BE=K−credit
      short_call       credit=bid；collateral=S（备兑口径）；max_loss=None（裸卖无上限）；BE=K+credit
      bull_put_spread  credit=short.bid−wing.ask；max_loss=collateral=width−credit
      bear_call_spread 同上，call 侧
      strangle         credit=put.bid+call.bid；collateral=K_put（call 侧备兑另计）；max_loss=None；BE 两个
      iron_condor      两个价差之和；collateral=max_loss=max(width_put, width_call)−credit
      yield_raw = credit/collateral；yield_annualized = yield_raw·365/dte
    取整：credit 先取 `MONEY_DP` 位，再据它算 max_loss / collateral / BE（同样取 `MONEY_DP` 位）、
    判 credit ≤ 0；收益率用取整后的金额算、按全精度年化，最后各取 `YIELD_DP` 位。
    不可报价（quotable=False，reason 给第一条、reasons 给全部）：
      · 腿缺失（`missing_leg:<side>_<role>`）或任一腿 quote_ok=False（`quote_not_ok:<side>_<role>`）
        ⇒ 所有金额为 None（bid=0 的腿算不出真实成交）；
      · 短腿 spread_pct > MAX_SPREAD_PCT（`short_spread_too_wide:<side>`）⇒ 金额照算（供展示），但不可报价；
      · credit ≤ 0（`non_positive_credit`）⇒ credit 照记，max_loss/collateral/收益/BE 为 None。
    `rung=None`（route 判 unavailable）⇒ quotable=False，reason `rung_unavailable`。
    未知 structure / 非法 rung ⇒ ValueError（拼错不许静默变成「不可报价」）。
    """
    if structure not in _STRUCT_LEGS:
        raise ValueError(f"structure 只接受 {STRUCTURES}，收到 {structure!r}")
    rk = _norm_rungs(rung)
    rung_out = rung if not isinstance(rung, dict) else dict(rung)
    if rk is None:
        return _empty_quote(structure, rung_out, ["rung_unavailable"])

    reasons: List[str] = []
    legs: List[dict] = []
    picked: Dict[Tuple[str, str], dict] = {}
    for side, role in _STRUCT_LEGS[structure]:
        key = rk.get(side)
        if key is None:
            reasons.append(f"rung_unavailable:{side}")
            continue
        slot = ((ladder or {}).get(side) or {}).get(key) or {}
        leg = slot.get(role)
        if not isinstance(leg, dict):
            reasons.append(f"missing_leg:{side}_{role}")
            continue
        picked[(side, role)] = leg
        action = "sell" if role == "short" else "buy"
        legs.append({"side": side, "role": role, "action": action, "rung": key,
                     "symbol": leg.get("symbol"), "strike": leg.get("strike"), "cp": leg.get("cp"),
                     "fill": leg.get("bid") if action == "sell" else leg.get("ask")})
        if not leg.get("quote_ok"):
            reasons.append(f"quote_not_ok:{side}_{role}")
    if reasons:
        return _empty_quote(structure, rung_out, reasons, legs)

    for side, role in _STRUCT_LEGS[structure]:
        if role == "short":
            sp = _num(picked[(side, role)].get("spread_pct"))
            if sp is not None and sp > MAX_SPREAD_PCT:
                reasons.append(f"short_spread_too_wide:{side}")

    def K(side, role):
        return float(picked[(side, role)]["strike"])

    def bid(side, role):
        return float(picked[(side, role)]["bid"])

    def ask(side, role):
        return float(picked[(side, role)]["ask"])

    S = _pos((ladder or {}).get("underlying_price"))
    notes: List[str] = []
    max_loss = collateral = None
    bes: List[float] = []
    if structure == "short_put":
        credit = _rnd(bid("put", "short"), MONEY_DP)
        collateral = K("put", "short")
        max_loss = collateral - credit
        bes = [K("put", "short") - credit]
    elif structure == "short_call":
        credit = _rnd(bid("call", "short"), MONEY_DP)
        collateral = S
        notes += ["naked_call_max_loss_unbounded", "collateral_is_covered_call_notional_S"]
        bes = [K("call", "short") + credit]
    elif structure == "bull_put_spread":
        credit = _rnd(bid("put", "short") - ask("put", "wing"), MONEY_DP)
        width = abs(K("put", "short") - K("put", "wing"))
        max_loss = collateral = width - credit
        bes = [K("put", "short") - credit]
    elif structure == "bear_call_spread":
        credit = _rnd(bid("call", "short") - ask("call", "wing"), MONEY_DP)
        width = abs(K("call", "wing") - K("call", "short"))
        max_loss = collateral = width - credit
        bes = [K("call", "short") + credit]
    elif structure == "strangle":
        credit = _rnd(bid("put", "short") + bid("call", "short"), MONEY_DP)
        collateral = K("put", "short")
        notes += ["naked_call_side_max_loss_unbounded",
                  "collateral_counts_cash_secured_put_only_call_side_covered_separately"]
        bes = [K("put", "short") - credit, K("call", "short") + credit]
    else:  # iron_condor
        credit = _rnd((bid("put", "short") - ask("put", "wing"))
                      + (bid("call", "short") - ask("call", "wing")), MONEY_DP)
        w_put = abs(K("put", "short") - K("put", "wing"))
        w_call = abs(K("call", "wing") - K("call", "short"))
        # 到期时至多一侧 ITM ⇒ 最大亏损由宽的那一翼决定
        max_loss = collateral = max(w_put, w_call) - credit
        bes = [K("put", "short") - credit, K("call", "short") + credit]

    max_loss, collateral = _rnd(max_loss, MONEY_DP), _rnd(collateral, MONEY_DP)
    bes = [_rnd(b, MONEY_DP) for b in bes]
    if credit <= 0:
        reasons.append("non_positive_credit")
        max_loss = collateral = None
        bes = []
    y_raw = credit / collateral if (credit > 0 and collateral is not None and collateral > 0) else None
    dte = _num((ladder or {}).get("dte"))
    y_ann = y_raw * 365.0 / dte if (y_raw is not None and dte is not None and dte > 0) else None
    y_raw, y_ann = _rnd(y_raw, YIELD_DP), _rnd(y_ann, YIELD_DP)   # 年化用全精度 y_raw 算，最后才取整
    if collateral is None and structure == "short_call" and credit > 0:
        notes.append("underlying_price_unavailable_for_collateral")
    return {"structure": structure, "rung": rung_out, "legs": legs, "credit": credit,
            "max_loss": max_loss, "collateral": collateral, "yield_raw": y_raw,
            "yield_annualized": y_ann, "breakevens": sorted(bes), "quotable": not reasons,
            "reason": reasons[0] if reasons else None, "reasons": reasons, "notes": notes}


def _intrinsic(cp: str, K: float, S_T: float) -> float:
    return max(K - S_T, 0.0) if cp == "P" else max(S_T - K, 0.0)


def structure_pnl_at_expiry(quote: dict, expiry_close) -> Optional[float]:
    """到期每股盈亏 = credit − Σ内在价值(short) + Σ内在价值(wing)。

    只判**到期收盘**，不判期间触及（日线只有收盘价）。不可报价的结构 ⇒ None：
    它在入场那天就成交不了，给它记盈亏等于记一笔不存在的交易。
    """
    S_T = _pos(expiry_close)
    if not isinstance(quote, dict) or not quote.get("quotable") or S_T is None:
        return None
    credit = _num(quote.get("credit"))
    if credit is None:
        return None
    pnl = credit
    for leg in quote.get("legs") or []:
        K, cp = _pos(leg.get("strike")), leg.get("cp")
        if K is None or cp not in ("C", "P"):
            return None
        iv = _intrinsic(cp, K, S_T)
        pnl += -iv if leg.get("role") == "short" else iv
    return pnl


def pnl_over_credit(quote: dict, expiry_close) -> Optional[float]:
    """pnl / credit（credit>0 时）；预注册度量。1.0 = 全额收下权利金，负值 = 亏损超过所收权利金的倍数。"""
    pnl = structure_pnl_at_expiry(quote, expiry_close)
    credit = _num((quote or {}).get("credit"))
    if pnl is None or credit is None or credit <= 0:
        return None
    return pnl / credit


# ─────────────────────────────── 环境路由（冻结，ROUTE_RULE_VERSION=1）

def route(zero_gamma: dict) -> dict:
    """GEX 环境 → 每侧取梯子的哪一档。**先看符号、再看距离**。

      insufficient_contracts / sign_at_spot 为 None ⇒ 两侧 unavailable
      扫描合约数 n_contracts < MIN_SWEEP_CONTRACTS（或缺失）⇒ 两侧 unavailable，
        reason `sweep_too_few_contracts:<n>`（先于符号判：几张合约的符号不是政体）
      现价处净 gamma 为负（含 all_negative）⇒ 两侧 far（flag=True）
      正 / 零：下方零点距离 zg_below_pct ≤ FLIP_BUFFER_PCT ⇒ put far（flag_put=True），否则 put base；
               call 一律 base
    旧规则只看「离 flip 多远」：远离零点的负 gamma 会被判成安全 —— 正是负 gamma 放大波动最该退后的情形。
    确定性函数：只决定档位，不改梯子，所以反事实（置换 flag）可以从梯子重算。
    """
    zg = zero_gamma if isinstance(zero_gamma, dict) else {}
    base = {"rule_version": ROUTE_RULE_VERSION, "view": ROUTE_VIEW}
    sign = zg.get("sign_at_spot")
    unavailable = {"put": "unavailable", "call": "unavailable", "flag_put": None, "flag_call": None,
                   **base}
    if zg.get("curve_state") == "insufficient_contracts" or sign is None:
        return {**unavailable, "reasons": [f"zero_gamma_{zg.get('curve_state') or 'missing'}"]}
    n = _num(zg.get("n_contracts"))
    if n is None or n < MIN_SWEEP_CONTRACTS:
        # 缺键同样判不可得：route 的输入契约是 zero_gamma_sweep 的输出（必有此键），
        # 缺了说明调用方喂错了东西，当成「够」就是把判不了写成判过了。
        shown = int(n) if (n is not None and n == int(n)) else zg.get("n_contracts")
        return {**unavailable, "reasons": [f"sweep_too_few_contracts:{shown}"]}
    if sign == "negative":
        return {"put": "far", "call": "far", "flag_put": True, "flag_call": True, **base,
                "reasons": [f"negative_gamma_at_spot:{zg.get('curve_state')}"]}
    reasons = [f"{sign}_gamma_at_spot:{zg.get('curve_state')}"]
    below = _num(zg.get("zg_below_pct"))
    if below is not None and below <= FLIP_BUFFER_PCT:
        put, flag_put = "far", True
        reasons.append(f"zero_gamma_below_within_{FLIP_BUFFER_PCT}pct:{below:.2f}")
    else:
        put, flag_put = "base", False
    return {"put": put, "call": "base", "flag_put": flag_put, "flag_call": False, **base,
            "reasons": reasons}


def rung_for(route_label: Optional[str]) -> Optional[float]:
    """base → BASE_RUNG、far → FAR_RUNG、unavailable → None；其它值 ⇒ ValueError。"""
    table = {"base": BASE_RUNG, "far": FAR_RUNG, "unavailable": None}
    if route_label not in table:
        raise ValueError(f"route 标签只接受 {tuple(table)}，收到 {route_label!r}")
    return table[route_label]


# ─────────────────────────────── 财报标注

def _as_date(x) -> Optional[date]:
    if isinstance(x, date):
        return x
    if not isinstance(x, str) or len(x) < 10:
        return None
    try:
        return date.fromisoformat(x[:10])
    except ValueError:
        return None


def classify_earnings(info, as_of, expiry) -> str:
    """`upcoming_fn(ticker)` 的**真实返回形状**（`_earnings_date_from_swarm`）→ 财报冲突标签。

      None（兜底失败 / EarningsWatcher 取不到）       ⇒ "unknown"
      {"earnings_date": None, ...}（视野内确实没有）  ⇒ "none"
      日期 d：as_of ≤ d ≤ expiry ⇒ "before_expiry"；d > expiry ⇒ "after_expiry"；d < as_of ⇒ "none"
      dict 里没有 earnings_date 键 / 日期解析失败 / as_of、expiry 非法 ⇒ "unknown"
    "unknown" 与 "before_expiry" 一样被独立单位排除（两臂同等）——判不了就不能当「没有」。
    """
    if not isinstance(info, dict) or "earnings_date" not in info:
        return "unknown"
    raw = info.get("earnings_date")
    a, e = _as_date(as_of), _as_date(expiry)
    if a is None or e is None:
        return "unknown"
    if raw is None:
        return "none"
    d = _as_date(raw) if not isinstance(raw, str) else _as_date(raw.strip())
    if d is None:
        return "unknown"
    if d < a:
        return "none"
    return "before_expiry" if d <= e else "after_expiry"
