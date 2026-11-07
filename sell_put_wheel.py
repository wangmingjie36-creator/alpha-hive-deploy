"""接货型卖 put 纸面账本（v0.45.720，用户 2026-11-04 定；规则冻结在 experiments/sell_put_wheel_prereg.md）。

做什么（每个循环一个状态机；v3.1 / v0.45.747 起一只标的可同时有多个循环——额度内允许加仓，每天每只最多新开一笔）：

    无仓 ──卖 put──▶ put ──到期价外──▶ 无仓（权利金落袋）
                      └─到期价内（收盘 < 行权价）──▶ 接货：持股 ──卖 covered call──▶ 持股 + call
                                                        ▲                              ├─到期未被叫走 ─┘（再卖下一张）
                                                        └──────────────────────────────┴─到期被叫走 ──▶ 无仓（一圈结束）

参考的是「只卖自己愿意持有的公司、行权价 = 愿意买入价、现金担保、被行权就接货」那一类做法——**我对该做法的理解只是公开说法的
大意，规则里每个数都是主观设定**（见预注册）。它与卖权选择器（`sell_strike_*`，按 delta 档 / GEX 选行权价、50% 止盈 / 2× 止损）
是**两套不同的东西**：这里被行权是目的、不止损；不并入那边的预注册检验，也不碰跨式账本。

⚠️ 这是**测量账本，不是检验**：几只标的、几个月，样本小到证明不了任何优势；它的用处是让每一笔真实结果摆在眼前，
并与两条基准（同价位限价买入 / 一直持有）并排。美式提前行权、保证金、分红、手续费都不建模。

成交约定（保守）：卖 put / 卖 call 一律按 **bid** 成交；到期结算按**到期那一场**的官方收盘（`options_paper_leg._default_close`，
自报场次，场次不符 ⇒ 延期，**绝不拿前一天的价顶替**）。未到期的腿按 mid 盯市；取不到合格报价就沿用上次的 mark 并记 stale。

唯一真相：参数在 `CONFIG`；路径只经 `PATHS.sell_put_state`（调用时求值，不存模块级常量）；落盘一律走 `ledger_io`。
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import ledger_io
from hive_logger import PATHS

_log = logging.getLogger(__name__)

# ── 全部参数（主观设定，没有一个经过检验；改任何一个 = 改账本测什么，要在预注册里记修订）──
CONFIG: Dict[str, Any] = {
    "universe": ["XOM", "WMT", "NVDA"],
    # 每只的「接货价」（愿意买的最高价）。None = **占位**：开仓那天取现价 ×(1 − placeholder_otm_pct%)，记录里标 accept_price_source="placeholder"。
    # 用户 2026-11-04 没给具体接货价、明确选「按缺省走」，所以现在三只都是占位——别把占位当成估值判断。
    "accept_price": {"XOM": None, "WMT": None, "NVDA": None},
    "placeholder_otm_pct": 10.0,
    "starting_capital": 100_000.0,
    # v3（用户 2026-11-06 定「总 60%，按筛选条件选出最优标的开仓」）：没有单只上限、张数不固定——
    # 按综合分从高到低，每只开到「put 担保 + 持股市值 ≤ NAV × total_cap_pct」与现金担保都还容得下的最多张数
    "total_cap_pct": 60.0,
    "sizing": "fill_total_cap_by_score",
    # put 到期窗口（日历日）。v2（用户 2026-11-06 定）：30–50 天；窗口内**每个**到期日都参与综合分比较（不再取离目标最近的那一个）
    "put_dte_range": (30, 50),
    # v3 covered call（用户 2026-11-06 定）：接货后最近一个已收盘场次的收盘 ≥ 接货价 × (1 + call_min_rise_pct%) 才卖；
    # 选合约与 put 同一套：窗口内所有到期日 × 行权价 ≥ max(成本底线, 现价) 的合格档、IV/RV ≥ 门槛，取「年化 × IV/RV」最高
    # （年化按现价算：权利金 ÷ 现价 × 365 ÷ 天数）。「行权价 ≥ 现价」是我加的：价内 call 的权利金含内在价值，按年化排会专挑深度价内
    "call_dte_range": (30, 50),
    "call_min_rise_pct": 5.0,
    "max_spread_pct": 0.15,          # (ask − bid) / mid 超过它不开（点差吃权利金，同跨式账本的口径）
    "min_oi": 100,
    # v2：期权要比实际波动「贵」才卖——合约 IV ÷ 标的 RV 低于它不开（主观门槛，未经检验）
    "rv_window": 30,                 # RV = 最近 30 个日收益（31 个分红复权收盘，yfinance）的年化标准差，同 `vrp_signal.realized_vol_from_closes`
    "min_iv_rv": 1.0,
    # v2：综合分 = 年化权利金 × (IV ÷ RV)。同一只里用它在「窗口内到期日 × 不高于接货价的合格行权价」中选那一张；
    # 担保额度不够都开时，也按它从高到低先开（用户 2026-11-06「年化也要考虑进去，要综合评价」；乘法不另设权重，公式本身是主观设定）
    "rank_by": "annual_yield_x_iv_rv",
}

def public_rules() -> dict:
    """页面 / 帮助页用的规则常量（取 CONFIG 当下的值，前端不另抄）。元组转列表以便 JSON。"""
    keys = ("universe", "accept_price", "placeholder_otm_pct", "starting_capital", "total_cap_pct", "sizing",
            "put_dte_range", "call_dte_range", "call_min_rise_pct", "max_spread_pct", "min_oi", "rv_window", "min_iv_rv",
            "rank_by")
    return {k: (list(CONFIG[k]) if isinstance(CONFIG[k], tuple) else (dict(CONFIG[k]) if isinstance(CONFIG[k], dict)
                                                                      else CONFIG[k])) for k in keys}


#: 口径世代（手写常量，只在账本口径变时改）
_VERSION = "0.45.747"          # v3.1：允许加仓（一只多个循环）；0.45.746 = v3：总额度按综合分装满、covered call 涨 5% 才卖且按综合分选；0.45.742 = v2；0.45.720 = v1
CONTRACT = 100


# ═══════════════════════════ 数值小工具 ═══════════════════════════

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


def _iso_day(v) -> Optional[str]:
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _dte(as_of: str, expiry: str) -> Optional[int]:
    a, e = _iso_day(as_of), _iso_day(expiry)
    if a is None or e is None:
        return None
    return (date.fromisoformat(e) - date.fromisoformat(a)).days


def annual_yield(premium: float, strike: float, dte: int) -> float:
    """现金担保口径年化：权利金 ÷ 行权价 × 365 ÷ 天数（%）。"""
    return premium / strike * 365.0 / dte * 100.0


def _spread_pct(c: dict) -> Optional[float]:
    bid, ask = _num(c.get("bid")), _num(c.get("ask"))
    if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid if mid > 0 else None


def _liquid(c: dict) -> Tuple[bool, Optional[str]]:
    """这条合约能不能卖：bid > 0、ask ≥ bid、点差 ≤ 上限、OI ≥ 下限。返回 (能, 不能的原因码)。"""
    bid = _num(c.get("bid"))
    if bid is None or bid <= 0:
        return False, "no_bid"
    sp = _spread_pct(c)
    if sp is None:
        return False, "bad_quote"
    if sp > CONFIG["max_spread_pct"]:
        return False, "wide_spread"
    if (_num(c.get("oi")) or 0.0) < CONFIG["min_oi"]:
        return False, "low_oi"
    return True, None


#: 「没开成」的原因码（唯一一张表；`tests/test_sell_put_wheel.py` 双向核对：写出的每个码都登记、表里无死键）
SKIP_REASONS = {
    "no_chain": "取不到今天的期权链",
    "no_price": "取不到标的现价",
    "no_expiry": "没有落在期限窗口内的到期日",
    "no_strike": "没有不高于接货价的行权价",
    "no_rv": "算不出标的实际波动率（日线取不到或不够 31 根）",
    "no_iv": "合格行权价都没有隐含波动率",
    "iv_below_rv": "隐含波动率 ÷ 实际波动率低于门槛（期权不比实际波动贵）",
    "no_bid": "该行权价没有买价（bid 为 0）",
    "bad_quote": "报价不合格（ask 缺失 / 倒挂）",
    "wide_spread": "点差超过上限",
    "low_oi": "未平仓量低于下限",
    "total_cap": "总担保额度剩下的不够一张",
    "no_cash": "现金不够担保",
    "no_call_strike": "没有不低于成本底线、也不低于现价的 call",
    "no_close": "取不到最近一个已收盘场次的收盘价（判不了涨没涨）",
    "not_risen": "收盘较接货价还没涨够，不卖 call",
    "settle_deferred": "上一腿到期了、还没拿到结算那一场的收盘",
}


# ═══════════════════════════ 选合约（纯函数） ═══════════════════════════

def _expiries(contracts: List[dict], cp: str, as_of: str, rng: Tuple[int, int]) -> List[Tuple[int, str]]:
    seen: Dict[str, int] = {}
    lo, hi = rng
    for c in contracts:
        if c.get("cp") != cp:
            continue
        d = _dte(as_of, c.get("expiry"))
        if d is not None and lo <= d <= hi:
            seen[str(c["expiry"])] = d
    return sorted((d, e) for e, d in seen.items())


def rv_from_closes(closes: Optional[Dict[str, float]], as_of: str) -> Optional[float]:
    """`{YYYY-MM-DD: 收盘}` 里 ≤ as_of 的最后 `rv_window + 1` 根 → 年化实际波动率（%）。不够根数 / 算不出 ⇒ None（不猜）。
    公式只有一份：`vrp_signal.realized_vol_from_closes`（对数收益、ddof=1、×√252）。"""
    from vrp_signal import realized_vol_from_closes
    if not closes:
        return None
    n = int(CONFIG["rv_window"]) + 1
    days = sorted(d for d in closes if str(d)[:10] <= as_of)[-n:]
    if len(days) < n:
        return None
    return realized_vol_from_closes([closes[d] for d in days])


#: `rv_asof` 判不出 RV 的明细码（进 skipped.detail / 页面；不是 SKIP_REASONS 的原因码——那边统一记 `no_rv`）
RV_DETAILS = {
    "session_unknown": "判不了最近一个已收盘的交易日",
    "closes_lag": "日线最后一根早于应有的那一场（数据源还没给出 / 缓存是那一场收盘前取的）",
    "too_few_closes": "截止那一场之前的收盘不够 31 根",
    "no_closes": "没有日线",
}


def expected_session(cut: str) -> Optional[str]:
    """≤ cut 的最近一个美股交易日（含临时休市表）。纯日历，不联网。"""
    from bars_integrity import AD_HOC_CLOSURES
    from is_trading_day import is_trading_day
    try:
        d = date.fromisoformat(str(cut)[:10])
    except ValueError:
        return None
    for _ in range(15):
        if d not in AD_HOC_CLOSURES and is_trading_day(d)[0]:
            return d.isoformat()
        d = date.fromordinal(d.toordinal() - 1)
    return None


def rv_asof(closes: Optional[Dict[str, float]], as_of: str, completed: Optional[str]) -> Tuple[Optional[float], dict]:
    """**账本与筛选页唯一的 RV 入口**（v0.45.744）：哪几根收盘算数、窗口末根对不对，只在这里判。

    `completed` = **取这份日线那一刻**最近一个已收盘的交易日（调用方在取数前记下）。截止场次 = ≤ min(as_of, completed)
    的最近交易日；窗口 = 截止场次及之前的最后 `rv_window + 1` 根。
    ⚠️ 末根日期必须**恰好等于**截止场次：早了（数据源缺最近一场 / 缓存是那一场收盘前取的）⇒ `closes_lag`，不把窗口
    悄悄往前挪一天算一个「看起来正常」的数（v0.45.742 两边各截各的，正是这样对不上的）。
    返回 (RV % | None, {"end", "expected", "n", "close", "why"})；why 为 None = 算出来了。`close` = 截止场次那一根收盘
    （v0.45.746：covered call「涨没涨」只看它——与 RV 同一份日线、同一个场次，末根不对就没有）。"""
    meta: dict = {"end": None, "expected": None, "n": 0, "close": None, "why": None}
    if not completed:
        return None, {**meta, "why": "session_unknown"}
    exp = expected_session(min(str(as_of)[:10], str(completed)[:10]))
    meta["expected"] = exp
    if exp is None:
        return None, {**meta, "why": "session_unknown"}
    days = sorted(d for d in (closes or {}) if str(d)[:10] <= exp)
    meta["n"] = len(days)
    if not days:
        return None, {**meta, "why": "no_closes"}
    meta["end"] = str(days[-1])[:10]
    if meta["end"] != exp:
        return None, {**meta, "why": "closes_lag"}
    meta["close"] = _pos(closes[days[-1]])
    rv = rv_from_closes(closes, exp)
    if rv is None:
        return None, {**meta, "why": "too_few_closes"}
    return rv, meta


def score_put(c: dict, dte: int, rv_pct: float, base: Optional[float] = None) -> Optional[Tuple[float, float, float]]:
    """一张期权的 (综合分, 年化 %, IV/RV)。综合分 = 年化权利金 × (IV ÷ RV)。没有 IV ⇒ None。
    年化的分母 `base`：put 缺省 = 行权价（现金担保）；covered call 传现价（持股市值）。"""
    iv = _pos(c.get("iv"))
    if iv is None:
        return None
    ay = annual_yield(c["bid"], base if base is not None else c["strike"], dte)
    ratio = iv * 100.0 / rv_pct
    return ay * ratio, ay, ratio


def _best_by_score(cands: List[dict], window: Dict[str, int], rv: float,
                   base: Optional[float]) -> Tuple[Optional[Tuple[dict, Tuple[float, float, float]]], Optional[str]]:
    """候选里取综合分最高的一张（put / call 共用）：要合格、有 IV、IV/RV ≥ 门槛；同分取更晚到期、再取更高行权价。
    都不合格 ⇒ (None, 候选顺序里第一张的原因)。"""
    best, best_key, first_reason = None, None, None
    for c in cands:
        ok, why = _liquid(c)
        if not ok:
            first_reason = first_reason or why
            continue
        s = score_put(c, window[c["expiry"]], rv, base)
        if s is None:
            first_reason = first_reason or "no_iv"
            continue
        if s[2] < CONFIG["min_iv_rv"] - 1e-12:
            first_reason = first_reason or "iv_below_rv"
            continue
        key = (round(s[0], 9), window[c["expiry"]], c["strike"])     # 圆整到 1e-9：纸面同分不让浮点尾数决定，交给同分规则
        if best_key is None or key > best_key:
            best, best_key = (c, s), key
    return best, (None if best else first_reason)


def pick_put(contracts: List[dict], spot: float, accept: Optional[float], as_of: str,
             rv_pct: Optional[float] = None) -> Tuple[Optional[dict], Optional[str], dict]:
    """卖哪一张 put（v2，用户 2026-11-06 定）：候选 = 期限窗口内**所有**到期日 × **不高于接货价**的行权价，
    要合格（bid / 点差 / OI）、有 IV、且 IV ÷ RV ≥ 门槛；在候选里取**综合分（年化 × IV/RV）最高**的那一张
    （同分取更晚到期、再取更高行权价，结果与输入顺序无关）。

    返回 (选中的合约 | None, 没选中的原因码 | None, 附加信息)。附加信息带 accept_price / source / rv_pct，选中时另带
    iv_pct / iv_rv / annual_yield_pct / score；选不中也带前几项（页面要解释）。
    接货价缺省（None）⇒ 现价 ×(1 − 占位 %)，来源记 placeholder。都不合格时报「最高行权价、最晚到期」那张的原因。"""
    placeholder = accept is None
    ap = _pos(accept) if not placeholder else spot * (1.0 - CONFIG["placeholder_otm_pct"] / 100.0)
    rv = _pos(rv_pct)
    info = {"accept_price": round(ap, 4) if ap else None, "accept_price_source": "placeholder" if placeholder else "user",
            "rv_pct": round(rv, 2) if rv is not None else None}
    if ap is None:
        return None, "no_strike", info
    window = dict((e, d) for d, e in _expiries(contracts, "P", as_of, CONFIG["put_dte_range"]))
    if not window:
        return None, "no_expiry", info
    cands = sorted((c for c in contracts if c.get("cp") == "P" and c.get("expiry") in window
                    and _pos(c.get("strike")) is not None and _pos(c["strike"]) <= ap + 1e-9),
                   key=lambda c: (-c["strike"], -window[c["expiry"]]))
    if not cands:
        return None, "no_strike", info
    if rv is None:
        return None, "no_rv", info
    best, why = _best_by_score(cands, window, rv, None)
    if best is None:
        return None, why, info
    c, (score, ay, ratio) = best
    return c, None, {**info, "iv_pct": round(_pos(c["iv"]) * 100.0, 2), "iv_rv": round(ratio, 4),
                     "annual_yield_pct": round(ay, 2), "score": round(score, 4)}


def call_rise_ok(assigned_price: float, close: Optional[float]) -> Tuple[Optional[bool], Optional[float]]:
    """v3：covered call 只在「最近一个已收盘场次的收盘 ≥ 接货价 × (1 + call_min_rise_pct%)」时卖。
    返回 (够不够 | None = 没有收盘、判不了, 门槛价)。"""
    trigger = round(assigned_price * (1.0 + CONFIG["call_min_rise_pct"] / 100.0), 4)
    px = _pos(close)
    if px is None:
        return None, trigger
    return px >= trigger - 1e-9, trigger


def pick_call(contracts: List[dict], basis_floor: float, spot: Optional[float], as_of: str,
              rv_pct: Optional[float] = None) -> Tuple[Optional[dict], Optional[str], dict]:
    """卖哪一张 covered call（v3，用户 2026-11-06 定「和卖 put 一样看综合条件」）：候选 = 期限窗口内**所有**到期日 ×
    行权价 ≥ max(成本底线, 现价) 的 call，要合格、有 IV、IV/RV ≥ 门槛；取综合分（年化 × IV/RV，年化按现价）最高的那一张，
    同分规则与 put 相同。返回 (合约 | None, 原因码 | None, 附加信息)。涨没涨的闸在调用方（`call_rise_ok`）。"""
    rv = _pos(rv_pct)
    px = _pos(spot)
    info: dict = {"basis_floor": basis_floor, "rv_pct": round(rv, 2) if rv is not None else None}
    if px is None:
        return None, "no_price", info
    window = dict((e, d) for d, e in _expiries(contracts, "C", as_of, CONFIG["call_dte_range"]))
    if not window:
        return None, "no_expiry", info
    floor = max(basis_floor, px)
    cands = sorted((c for c in contracts if c.get("cp") == "C" and c.get("expiry") in window
                    and _pos(c.get("strike")) is not None and c["strike"] >= floor - 1e-9),
                   key=lambda c: (c["strike"], -window[c["expiry"]]))
    if not cands:
        return None, "no_call_strike", info
    if rv is None:
        return None, "no_rv", info
    best, why = _best_by_score(cands, window, rv, px)
    if best is None:
        return None, why, info
    c, (score, ay, ratio) = best
    return c, None, {**info, "iv_pct": round(_pos(c["iv"]) * 100.0, 2), "iv_rv": round(ratio, 4),
                     "annual_yield_pct": round(ay, 2), "score": round(score, 4)}


def mark_of(c: Optional[dict]) -> Optional[float]:
    """盯市价：合格双边报价的 mid；否则 None（调用方沿用上次 mark 并记 stale）。"""
    if not isinstance(c, dict):
        return None
    bid, ask = _num(c.get("bid")), _num(c.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


# ═══════════════════════════ 结算（纯函数） ═══════════════════════════

def settle_put(strike: float, close: float) -> str:
    """put 到期：收盘 < 行权价 ⇒ 被行权（接货）；否则作废。"""
    return "assigned" if close < strike else "expired"


def settle_call(strike: float, close: float) -> str:
    """call 到期：收盘 > 行权价 ⇒ 被叫走；否则作废。"""
    return "called_away" if close > strike else "expired"


# ═══════════════════════════ 状态读写 ═══════════════════════════

def _state_dir() -> Path:
    return Path(PATHS.sell_put_state)


def _paths() -> Dict[str, Path]:
    d = _state_dir()
    return {"dir": d, "lots": d / "lots.jsonl", "events": d / "events.jsonl", "equity": d / "equity_curve.jsonl",
            "meta": d / "meta.json"}


def _scrub(obj, _path: str = ""):
    """非有限数进不了账本（NaN 的比较全是 False，会悄悄穿过守卫）：落盘前扫一遍，发现就抛。"""
    if isinstance(obj, dict):
        return {k: _scrub(v, f"{_path}.{k}") for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, f"{_path}[]") for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError(f"非有限数不得入账本：{_path} = {obj!r}")
    return obj


def _load_meta() -> Dict:
    m = ledger_io.load_json(_paths()["meta"], default=None)    # 坏了抛 LedgerCorrupt：meta 里存的是现金，不当新账本
    if m is not None:
        return m
    return {"version": _VERSION, "starting_capital": CONFIG["starting_capital"], "starting_date": None,
            "cash": CONFIG["starting_capital"], "last_run_date": None, "config_snapshot": None,
            "last_run": None}


def _config_snapshot() -> Dict:
    return {k: (list(v) if isinstance(v, tuple) else (dict(v) if isinstance(v, dict) else v)) for k, v in CONFIG.items()}


# ═══════════════════════════ 失败登记（源头计数，扫描摘要 / 告警读它） ═══════════════════════════
# 钩子（`alpha_hive_daily_report`）把整轮异常吞成一条 WARNING，账本照样「看起来」只是少了一天——所以失败在这里登记：
# 本轮是否抛了、取不到期权链的标的数、结算延期了几天。`scan_timing.counters()["sell_put"]` 读它 →
# `alert_manager._check_sell_put`（整轮抛 / 全部标的取不到链 / 结算延期 ≥ DEFERRED_ALERT_DAYS 天 ⇒ P2）。
DEFERRED_ALERT_DAYS = 3
_ITEMS_MAX = 10
_LOCK = threading.Lock()
_ZERO = {"runs": 0, "run_errors": 0, "chain_total": 0, "chain_missing": 0, "max_deferred_days": 0,
         "rv_total": 0, "rv_missing": 0}
_STATS: Dict[str, Any] = {**_ZERO, "items": []}


def _note(**inc) -> None:
    with _LOCK:
        for k, v in inc.items():
            if k == "max_deferred_days":
                _STATS[k] = max(_STATS[k], v)
            else:
                _STATS[k] += v


def _note_item(kind: str, detail: Optional[str]) -> None:
    with _LOCK:
        if len(_STATS["items"]) < _ITEMS_MAX:
            _STATS["items"].append({"kind": kind, "detail": detail})


def stats() -> dict:
    """进程内计数。`all_chains_missing`：一整轮宇宙里的标的一只链都没取到（接线 / 数据源整批断了）。
    `all_rv_missing`（v0.45.742；v0.45.746 起含要卖 call 的持股标的）：要取 RV 的标的一只都没算出来——put 与 call 的选合约都离不开 RV，整批取不到 = 这天一张都开不出。"""
    with _LOCK:
        out = {k: _STATS[k] for k in _ZERO}
        out["items"] = list(_STATS["items"])
    out["all_chains_missing"] = out["chain_total"] > 0 and out["chain_missing"] == out["chain_total"]
    out["deferred_alert"] = out["max_deferred_days"] >= DEFERRED_ALERT_DAYS
    out["all_rv_missing"] = out["rv_total"] > 0 and out["rv_missing"] == out["rv_total"]
    return out


def reset_stats() -> None:
    with _LOCK:
        _STATS.clear()
        _STATS.update({**_ZERO, "items": []})


# ═══════════════════════════ 主流程 ═══════════════════════════

def _default_contracts(ticker: str, as_of: str) -> Tuple[Optional[dict], Optional[str]]:
    from cboe_options import fetch_cboe_raw_contracts
    return fetch_cboe_raw_contracts(ticker, as_of=as_of)


def _default_close(ticker: str, settle_date: str, *, as_of: Optional[str] = None) -> Dict:
    import options_paper_leg as opl
    return opl._default_close(ticker, settle_date, as_of=as_of)


def _default_rv(ticker: str, as_of: str) -> Tuple[Optional[float], dict]:
    """标的的实际波动率（%）与 `rv_asof` 的明细。日线经全仓唯一入口 `yf_daily`（分红复权；限流闸 `yf_gate`）；
    **取数之前**记下已收盘场次交给 `rv_asof`——判哪几根算数只在那里。"""
    import yf_daily
    import yfinance as yf
    try:
        from yf_gate import ensure
        ensure()
    except ImportError:
        pass
    done = yf_daily.latest_completed_session()
    hist, latest_missing = yf_daily.history(yf.Ticker(ticker), source="sell_put_rv", period="3mo", auto_adjust=True)
    closes: Dict[str, float] = {}
    if hist is not None and hasattr(hist, "index") and "Close" in getattr(hist, "columns", ()):
        for ts, c in zip(hist.index, hist["Close"]):
            v = _num(c)
            if v is not None:
                closes[str(ts)[:10]] = v
    rv, meta = rv_asof(closes, as_of, done.isoformat() if done else None)
    if latest_missing and rv is not None:           # yf_daily 说最近一场是公司行动空行（没有价）⇒ 同样不算
        return None, {**meta, "why": "closes_lag"}
    return rv, meta


def _settle_session(day: str) -> str:
    import options_paper_leg as opl
    return opl._settle_session(day)


def _resolve_close(raw, session: str) -> Dict:
    import options_paper_leg as opl
    return opl._resolve_close(raw, session)


def run_for_date(as_of: str,
                 contracts_fn: Optional[Callable[[str, str], Tuple[Optional[dict], Optional[str]]]] = None,
                 closes_fn: Optional[Callable[..., Any]] = None,
                 rv_fn: Optional[Callable[[str, str], Tuple[Optional[float], Optional[str]]]] = None) -> Dict:
    """(a) 到期结算 → (b) 卖 call / 卖 put → (c) 盯市与净值。同日重跑幂等。

    整轮持账本目录锁：读 meta / 持仓 → 改 → 写回是一次读-改-写，手动补跑与定时扫描撞上时后写者会用旧快照覆盖先写者。
    `contracts_fn(ticker, as_of)` → (`fetch_cboe_raw_contracts` 形状 | None, 原因)；`closes_fn(ticker, settle_date, as_of=...)`
    → 自报场次的 dict（缺省 `options_paper_leg._default_close`）或裸数字（测试注入，视为「就是那一场的收盘」）；
    `rv_fn(ticker, as_of)` → (实际波动率 % | None, 原因)（缺省 `_default_rv`，yfinance 复权日线）。"""
    d = _paths()["dir"]
    d.mkdir(parents=True, exist_ok=True)
    _note(runs=1)
    try:
        with ledger_io.locked(d):
            return _run_for_date(as_of, contracts_fn or _default_contracts, closes_fn or _default_close,
                                 rv_fn or _default_rv)
    except Exception as exc:  # noqa: BLE001 - 登记后原样抛：调用方（日报钩子）会吞成 WARNING，计数才是红的那个
        _note(run_errors=1)
        _note_item("run_error", f"{type(exc).__name__}: {exc}")
        raise


def _run_for_date(as_of: str, contracts_fn, closes_fn, rv_fn) -> Dict:
    p = _paths()
    meta = _load_meta()
    cash = _num(meta.get("cash"))
    if cash is None:
        _log.error("[SellPutWheel] meta.cash 非有限（%r）——本日不动账", meta.get("cash"))
        return {"as_of": as_of, "error": "cash not finite"}
    if not meta.get("starting_date"):
        meta["starting_date"] = as_of
    # v3.1（v0.45.747，用户 11-06「允许加仓」）：一只可同时有多个循环 ⇒ 持仓按**循环**（`cycle` = 标的-开仓日）索引，不再按标的
    lots: Dict[str, Dict] = {_lot_key(r): r for r in ledger_io.load_jsonl(p["lots"])}
    events: List[Dict] = []
    skipped: List[Dict] = []
    deferred: List[Dict] = []
    chains: Dict[str, Optional[dict]] = {}
    why_no_chain: Dict[str, Optional[str]] = {}

    def chain(tk: str) -> Optional[dict]:
        if tk not in chains:
            try:
                res, why = contracts_fn(tk, as_of)
            except Exception as exc:  # noqa: BLE001
                res, why = None, f"{type(exc).__name__}: {exc}"
            chains[tk], why_no_chain[tk] = res, why
        return chains[tk]

    rv_cache: Dict[str, Tuple[Optional[float], dict, Optional[str]]] = {}

    def fetch_rv(tk: str) -> Tuple[Optional[float], dict, Optional[str]]:
        """(RV | None, rv_asof 明细, 取不到时给人看的原因)。卖 put / 卖 call 共用；一轮里每只只取一次、记一次计数（整批取不到 ⇒ P2）。"""
        if tk not in rv_cache:
            rv_cache[tk] = _fetch_rv_once(tk)
        return rv_cache[tk]

    def _fetch_rv_once(tk: str) -> Tuple[Optional[float], dict, Optional[str]]:
        try:
            rv, rv_meta = rv_fn(tk, as_of)
        except Exception as exc:  # noqa: BLE001 - 单只取不到不连坐其余；计数 + 原因进 skipped，整批取不到 ⇒ P2
            rv, rv_meta = None, {"why": f"{type(exc).__name__}: {exc}"}
        if not isinstance(rv_meta, dict):           # 测试桩可只给 (rv, None | 原因字符串)
            rv_meta = {"why": rv_meta}
        why_rv = rv_meta.get("why") or ("rv_not_positive" if _pos(rv) is None else None)
        if why_rv in RV_DETAILS:
            why_rv = f"{why_rv}（{RV_DETAILS[why_rv]}；末根 {rv_meta.get('end')}，应为 {rv_meta.get('expected')}，{rv_meta.get('n')} 根）"
        _note(rv_total=1, rv_missing=0 if _pos(rv) is not None else 1)
        if _pos(rv) is None:
            _note_item("rv_missing", f"{tk}: {why_rv}")
        return (_pos(rv), rv_meta, why_rv)

    # ── (a) 到期结算 ──
    for key, lot in list(lots.items()):
        tk = lot["ticker"]
        for leg_key in ("put", "call"):
            leg = lot.get(leg_key)
            left = _dte(as_of, leg["expiry"]) if leg else None
            if left is None or left >= 0:
                continue          # 没有这条腿 / 到期日当天及之前不结：结算要到期那一场的定稿收盘，当晚恒取不到（`_drop_forming_bar`），次日起才结
            sess = _settle_session(leg["expiry"])
            try:
                raw = closes_fn(tk, leg["expiry"], as_of=as_of)
            except TypeError:
                raw = closes_fn(tk, leg["expiry"])
            except Exception as exc:  # noqa: BLE001
                _log.warning("[SellPutWheel] %s 结算取收盘失败: %s", tk, exc)
                raw = None
            close = _resolve_close(raw, sess)
            if close["price"] is None:
                leg["settle_deferred_since"] = leg.get("settle_deferred_since") or as_of
                leg["settle_deferred_reason"] = close.get("reason")
                deferred.append({"ticker": tk, "leg": leg_key, "expiry": leg["expiry"], "reason": close.get("reason")})
                continue
            leg.pop("settle_deferred_since", None)
            leg.pop("settle_deferred_reason", None)
            n = int(lot["contracts"])
            if leg_key == "put":
                outcome = settle_put(leg["strike"], close["price"])
                if outcome == "assigned":
                    cash -= leg["strike"] * CONTRACT * n
                    lot["state"] = "shares"
                    lot["shares"] = {"n": CONTRACT * n, "assigned_price": leg["strike"], "assigned_on": as_of,
                                     "basis": round(leg["strike"] - leg["premium"], 4)}
                else:
                    lot["state"] = "closed"
                lot["put"] = None
                events.append({"date": as_of, "ticker": tk, "event": f"put_{outcome}", "strike": leg["strike"],
                               "expiry": leg["expiry"], "close": close["price"], "premium": leg["premium"],
                               "settle_session": sess, "contracts": n, "cycle": lot["cycle"]})
            else:
                outcome = settle_call(leg["strike"], close["price"])
                if outcome == "called_away":
                    cash += leg["strike"] * CONTRACT * n
                    sh = lot["shares"]
                    pnl = ((leg["strike"] - sh["assigned_price"]) * CONTRACT * n
                           + lot["premiums"] * CONTRACT * n)
                    lot["state"] = "closed"
                    lot["shares"] = None
                    events.append({"date": as_of, "ticker": tk, "event": "call_called_away", "strike": leg["strike"],
                                   "expiry": leg["expiry"], "close": close["price"], "premium": leg["premium"],
                                   "settle_session": sess, "contracts": n, "cycle": lot["cycle"],
                                   "cycle_pnl_usd": round(pnl, 2)})
                else:
                    events.append({"date": as_of, "ticker": tk, "event": "call_expired", "strike": leg["strike"],
                                   "expiry": leg["expiry"], "close": close["price"], "premium": leg["premium"],
                                   "settle_session": sess, "contracts": n, "cycle": lot["cycle"]})
                lot["call"] = None
        if lot.get("state") == "closed":
            lots.pop(key, None)

    # ── (b) 卖 call（有股票、没有未到期 call）──
    for key, lot in lots.items():
        tk = lot["ticker"]
        if lot.get("state") != "shares" or lot.get("call"):
            continue
        if any(x["ticker"] == tk for x in deferred):
            skipped.append({"ticker": tk, "leg": "call", "reason": "settle_deferred"})
            continue
        ch = chain(tk)
        if not ch:
            skipped.append({"ticker": tk, "leg": "call", "reason": "no_chain", "detail": why_no_chain.get(tk)})
            continue
        rv, rv_meta, why_rv = fetch_rv(tk)
        if rv is None:
            skipped.append({"ticker": tk, "leg": "call", "reason": "no_rv", "detail": why_rv})
            continue
        close = _pos(rv_meta.get("close"))
        rose, trigger = call_rise_ok(lot["shares"]["assigned_price"], close)
        if rose is None:
            skipped.append({"ticker": tk, "leg": "call", "reason": "no_close", "trigger": trigger,
                            "detail": f"场次 {rv_meta.get('end')}"})
            continue
        if not rose:
            skipped.append({"ticker": tk, "leg": "call", "reason": "not_risen", "close": close,
                            "close_session": rv_meta.get("end"), "trigger": trigger,
                            "detail": f"收盘 {close}（{rv_meta.get('end')}）< 接货价 {lot['shares']['assigned_price']} × "
                                      f"(1 + {CONFIG['call_min_rise_pct']}%) = {trigger}"})
            continue
        c, why, info = pick_call(ch["contracts"], lot["shares"]["basis"], _pos(ch.get("underlying_price")), as_of,
                                 rv_pct=rv)
        if c is None:
            skipped.append({"ticker": tk, "leg": "call", "reason": why, **info})
            continue
        n = int(lot["contracts"])
        cash += c["bid"] * CONTRACT * n
        lot["premiums"] = round(lot["premiums"] + c["bid"], 4)
        extra = {"spot_at_open": _pos(ch.get("underlying_price")), "dte_at_open": _dte(as_of, c["expiry"]),
                 "close": close, "close_session": rv_meta.get("end"), "trigger": trigger,
                 "annual_yield_pct": info["annual_yield_pct"], "iv_pct": info["iv_pct"], "rv_pct": info["rv_pct"],
                 "iv_rv": info["iv_rv"], "score": info["score"], "rv_end": rv_meta.get("end"), "rule_version": _VERSION}
        lot["call"] = {"symbol": c["symbol"], "strike": c["strike"], "expiry": c["expiry"], "premium": c["bid"],
                       "sold_on": as_of, "delta": c.get("delta"), "last_mark": mark_of(c) or c["bid"],
                       "last_mark_date": as_of, "stale_days": 0, **extra}
        events.append({"date": as_of, "ticker": tk, "event": "call_sold", "strike": c["strike"], "expiry": c["expiry"],
                       "premium": c["bid"], "contracts": n, "cycle": lot["cycle"], "delta": c.get("delta"),
                       "basis_floor": lot["shares"]["basis"], "spread_pct": round(_spread_pct(c) * 100, 2), **extra})

    # ── (b') 卖 put（无仓的标的）：按综合分（年化 × IV/RV）从高到低排，逐只判额度 ──
    meta_nav = _nav_estimate(cash, lots, chains, chain)
    cands: List[Tuple[float, str, dict, dict]] = []
    for tk in CONFIG["universe"]:
        # v3.1：已有未到期 put / 持股不再挡（允许加仓）；但同一只**一天最多新开一笔**（同日重跑幂等、循环号 = 标的-日期不撞），
        # 结算延期的标的当天也不加（那一只的收盘还没拿到）
        if any(l["ticker"] == tk and l.get("opened_on") == as_of for l in lots.values()):
            continue
        if any(x["ticker"] == tk for x in deferred):
            skipped.append({"ticker": tk, "leg": "put", "reason": "settle_deferred"})
            continue
        ch = chain(tk)
        if not ch:
            skipped.append({"ticker": tk, "leg": "put", "reason": "no_chain", "detail": why_no_chain.get(tk)})
            continue
        spot = _pos(ch.get("underlying_price"))
        if spot is None:
            skipped.append({"ticker": tk, "leg": "put", "reason": "no_price"})
            continue
        rv, rv_meta, why_rv = fetch_rv(tk)
        c, why, info = pick_put(ch["contracts"], spot, CONFIG["accept_price"].get(tk), as_of, rv_pct=rv)
        if c is None:
            skipped.append({"ticker": tk, "leg": "put", "reason": why, **info,
                            **({"detail": why_rv} if why == "no_rv" else {})})
            continue
        dte = _dte(as_of, c["expiry"])
        cands.append((info["score"], tk, c, {**info, "spot": spot, "dte": dte, "rv_end": rv_meta.get("end")}))
    cands.sort(key=lambda x: -x[0])
    for _score, tk, c, info in cands:
        n_cap, n_cash = put_size(c["strike"], c["bid"], meta_nav["nav"], _reserved_and_shares(lots, meta_nav),
                                 cash, _reserved_puts(lots))
        if n_cap < 1:
            skipped.append({"ticker": tk, "leg": "put", "reason": "total_cap", "strike": c["strike"]})
            continue
        if n_cash < 1:
            skipped.append({"ticker": tk, "leg": "put", "reason": "no_cash", "strike": c["strike"]})
            continue
        n = min(n_cap, n_cash)
        cash += c["bid"] * CONTRACT * n
        cycle = f"{tk}-{as_of}"
        lots[cycle] = {"ticker": tk, "state": "put", "cycle": cycle, "contracts": n, "opened_on": as_of,
                    "premiums": c["bid"], "shares": None, "call": None,
                    "put": {"symbol": c["symbol"], "strike": c["strike"], "expiry": c["expiry"], "premium": c["bid"],
                            "sold_on": as_of, "delta": c.get("delta"), "last_mark": mark_of(c) or c["bid"],
                            "last_mark_date": as_of, "stale_days": 0, "spot_at_open": info["spot"],
                            "accept_price": info["accept_price"], "accept_price_source": info["accept_price_source"],
                            "annual_yield_pct": info["annual_yield_pct"], "dte_at_open": info["dte"],
                            "iv_pct": info["iv_pct"], "rv_pct": info["rv_pct"], "iv_rv": info["iv_rv"],
                            "score": info["score"], "rv_end": info["rv_end"], "rule_version": _VERSION}}
        events.append({"date": as_of, "ticker": tk, "event": "put_sold", "strike": c["strike"], "expiry": c["expiry"],
                       "premium": c["bid"], "contracts": n, "cycle": cycle, "delta": c.get("delta"),
                       "spot": info["spot"], "accept_price": info["accept_price"],
                       "accept_price_source": info["accept_price_source"], "annual_yield_pct": info["annual_yield_pct"],
                       "dte": info["dte"], "spread_pct": round(_spread_pct(c) * 100, 2),
                       "iv_pct": info["iv_pct"], "rv_pct": info["rv_pct"], "iv_rv": info["iv_rv"], "score": info["score"],
                       "rv_end": info["rv_end"], "rule_version": _VERSION})

    # ── (c) 盯市与净值 ──
    nav_info = _nav_estimate(cash, lots, chains, chain, mark=True, as_of=as_of)
    fetched = {tk: chains.get(tk) for tk in CONFIG["universe"] if tk in chains}
    n_missing = sum(1 for v in fetched.values() if v is None)
    max_def = max([(_dte(l[k]["settle_deferred_since"], as_of) or 0) for l in lots.values() for k in ("put", "call")
                   if l.get(k) and l[k].get("settle_deferred_since")] or [0])
    _note(chain_total=len(fetched), chain_missing=n_missing, max_deferred_days=max_def)
    for tk, why in why_no_chain.items():
        if chains.get(tk) is None:
            _note_item("chain_missing", f"{tk}: {why}")
    # 事件先落盘（按 (日期, 标的, 事件, 圈) 去重——同日重跑不重复记），净值行里的事件数按**落盘后**该日的条数算：
    # 数「本次新增」的话，同日重跑那一行会变成 0，账本文件就不再逐字节幂等（测试抓到的）
    prior_events = ledger_io.load_jsonl(p["events"])
    seen = {(e["date"], e["ticker"], e["event"], e.get("cycle")) for e in prior_events}
    for e in events:
        if (e["date"], e["ticker"], e["event"], e.get("cycle")) not in seen:
            ledger_io.append_jsonl(p["events"], _scrub(e))
            seen.add((e["date"], e["ticker"], e["event"], e.get("cycle")))
    n_events_today = sum(1 for e in ledger_io.load_jsonl(p["events"]) if e["date"] == as_of)
    row = {"date": as_of, "cash": round(cash, 2), "nav": round(nav_info["nav"], 2),
           "shares_value": round(nav_info["shares_value"], 2), "liability": round(nav_info["liability"], 2),
           "reserved_puts": round(_reserved_puts(lots), 2), "n_lots": len(lots),
           "states": {key: l["state"] for key, l in lots.items()}, "prices": nav_info["prices"],
           "n_events": n_events_today, "skipped": len(skipped), "deferred": len(deferred)}
    meta.update(cash=round(cash, 2), last_run_date=as_of, version=_VERSION, config_snapshot=_config_snapshot(),
                last_run={"date": as_of, "skipped": skipped, "deferred": deferred,
                          "chain_errors": {tk: w for tk, w in why_no_chain.items() if chains.get(tk) is None}})
    # 写盘顺序：持仓 → （事件已写）→ 净值（按日期去重）→ meta（最后写，meta 是「这天跑完了」的标记）
    ledger_io.write_jsonl(p["lots"], [_scrub(l) for l in lots.values()])
    eq = [r for r in ledger_io.load_jsonl(p["equity"]) if r.get("date") != as_of]
    eq.append(row)
    ledger_io.write_jsonl(p["equity"], [_scrub(r) for r in sorted(eq, key=lambda r: r["date"])])
    ledger_io.write_json(p["meta"], _scrub(meta))
    return {"as_of": as_of, "nav": row["nav"], "cash": row["cash"], "events": events, "skipped": skipped,
            "deferred": deferred, "n_lots": len(lots)}


def put_size(strike: float, bid: float, nav: float, held: float, cash: float, reserved: float) -> Tuple[int, int]:
    """v3 张数（纯函数）：(总额度容得下的张数, 现金担保容得下的张数)，调用方取两者较小。
    总额度：held + 行权价 × 100 × n ≤ NAV × total_cap_pct%。现金：开仓后现金（含收到的权利金）≥ 全部 put 担保。"""
    per = strike * CONTRACT
    room = nav * CONFIG["total_cap_pct"] / 100.0 - held
    n_cap = int(math.floor(room / per + 1e-9)) if per > 0 else 0
    net = per - bid * CONTRACT                 # 每多一张，担保增加 per、现金增加 bid × 100
    free = cash - reserved
    n_cash = int(math.floor(free / net + 1e-9)) if net > 0 else n_cap
    return max(n_cap, 0), max(n_cash, 0)


def _lot_key(lot: Dict) -> str:
    """持仓的索引键 = 循环号（标的-开仓日）。缺循环号的旧行退回标的名（v3.1 之前一只一条）。"""
    return str(lot.get("cycle") or lot["ticker"])


def _reserved_puts(lots: Dict[str, Dict]) -> float:
    return sum(l["put"]["strike"] * CONTRACT * int(l["contracts"]) for l in lots.values() if l.get("put"))


def _reserved_and_shares(lots: Dict[str, Dict], nav_info: Dict) -> float:
    """put 担保 + 持股市值（总担保上限的分子）。持股市值取最近一次盯市的价；还没有就按接货价。"""
    tot = _reserved_puts(lots)
    for l in lots.values():
        if l.get("shares"):
            px = nav_info["prices"].get(l["ticker"]) or l["shares"]["assigned_price"]
            tot += px * l["shares"]["n"]
    return tot


def _nav_estimate(cash: float, lots: Dict[str, Dict], chains: Dict, chain_fn, *, mark: bool = False,
                  as_of: Optional[str] = None) -> Dict:
    """净值 = 现金 + 持股市值 − 未到期 put / call 的负债（按 mid）。

    `mark=True`（收尾那一遍）才更新各腿的 last_mark / stale_days；开仓前那一遍只是估个额度，用已记的 mark。
    股价取 CBOE 链的 underlying_price（盘后快照 = 收盘价）；取不到的持股沿用上次价（prices 里没有就按接货价）并不更新。"""
    shares_value = liability = 0.0
    prices: Dict[str, float] = {}
    for l in lots.values():
        tk = l["ticker"]
        n = int(l["contracts"])
        ch = chains.get(tk) if tk in chains else (chain_fn(tk) if mark else None)
        spot = _pos((ch or {}).get("underlying_price"))
        if spot is not None:
            prices[tk] = spot
        by_symbol = {c["symbol"]: c for c in (ch or {}).get("contracts", [])} if ch else {}
        for key in ("put", "call"):
            leg = l.get(key)
            if not leg:
                continue
            m = mark_of(by_symbol.get(leg["symbol"]))
            if mark and m is not None:
                leg["last_mark"], leg["last_mark_date"], leg["stale_days"] = m, as_of, 0
            elif mark and as_of and leg.get("last_mark_date"):
                leg["stale_days"] = _dte(leg["last_mark_date"], as_of) or 0
            liability += (leg.get("last_mark") if leg.get("last_mark") is not None else leg["premium"]) * CONTRACT * n
        if l.get("shares"):
            px = spot if spot is not None else l["shares"].get("last_price") or l["shares"]["assigned_price"]
            if mark and spot is not None:
                l["shares"]["last_price"] = spot
            shares_value += px * l["shares"]["n"]
    return {"nav": cash + shares_value - liability, "shares_value": shares_value, "liability": liability,
            "prices": prices}


# ═══════════════════════════ 读者侧（只读） ═══════════════════════════

def load_state() -> Dict:
    """只读：meta / 持仓 / 事件 / 净值。目录不存在 ⇒ available False（不说成「账本为空」）。"""
    p = _paths()
    if not p["dir"].is_dir():
        return {"available": False, "state_reason": "no_state_dir"}
    return {"available": True, "meta": ledger_io.load_json(p["meta"], default=None) or {},
            "lots": ledger_io.load_jsonl(p["lots"]), "events": ledger_io.load_jsonl(p["events"]),
            "equity": ledger_io.load_jsonl(p["equity"])}


def kpis(state: Optional[Dict] = None) -> Dict:
    st = state if state is not None else load_state()
    if not st.get("available"):
        return {"available": False, "state_reason": st.get("state_reason")}
    ev = st["events"]
    by = lambda name: [e for e in ev if e["event"] == name]  # noqa: E731 - 局部取数小函数
    prem = sum(e["premium"] * CONTRACT * e["contracts"] for e in ev if e["event"] in ("put_sold", "call_sold"))
    eq = st["equity"]
    nav = eq[-1]["nav"] if eq else None
    start = (st.get("meta") or {}).get("starting_capital")
    return {"available": True, "n_put_sold": len(by("put_sold")), "n_put_expired": len(by("put_expired")),
            "n_assigned": len(by("put_assigned")), "n_call_sold": len(by("call_sold")),
            "n_called_away": len(by("call_called_away")), "premium_collected_usd": round(prem, 2),
            "cycles_closed": len(by("call_called_away")) + len(by("put_expired")),
            "nav": nav, "starting_capital": start,
            "return_pct": round((nav / start - 1) * 100, 3) if (nav is not None and start) else None,
            "last_run_date": (st.get("meta") or {}).get("last_run_date")}


def render_markdown(state: Optional[Dict] = None) -> str:
    st = state if state is not None else load_state()
    if not st.get("available"):
        return f"接货型卖 put 账本：不可得（{st.get('state_reason')}）"
    k = kpis(st)
    L = [f"## 接货型卖 put 账本（只记录，非检验；规则见 experiments/sell_put_wheel_prereg.md）", "",
         f"- 最近运行：{k['last_run_date']} · NAV {k['nav']} · 起始 {k['starting_capital']} · 收益 {k['return_pct']}%",
         f"- 卖 put {k['n_put_sold']} 次 · 作废 {k['n_put_expired']} · 被行权接货 {k['n_assigned']} · 卖 call {k['n_call_sold']} · "
         f"被叫走 {k['n_called_away']} · 累计权利金 ${k['premium_collected_usd']}", ""]
    for l in st["lots"]:
        leg = l.get("put") or l.get("call") or {}
        L.append(f"- {l['ticker']}：{l['state']} · {leg.get('symbol', '—')} · 到期 {leg.get('expiry', '—')} · "
                 f"权利金 {leg.get('premium', '—')}")
    run = (st.get("meta") or {}).get("last_run") or {}
    for s in run.get("skipped") or []:
        L.append(f"- 没开 {s['ticker']} {s['leg']}：{SKIP_REASONS.get(s['reason'], s['reason'])}")
    return "\n".join(L)


if __name__ == "__main__":  # pragma: no cover - 命令行：看进度（只读）或手动补跑某天
    import argparse
    ap = argparse.ArgumentParser(description="接货型卖 put 纸面账本")
    ap.add_argument("--run", metavar="YYYY-MM-DD", help="手动跑某一天（写账本）")
    args = ap.parse_args()
    if args.run:
        print(run_for_date(args.run))
    print(render_markdown())
