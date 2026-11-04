"""Alpha Bot「接货 · 筛选」的数据面（v0.45.722）：把我们手工做过的那张表放进程序里——30 只标的里挑「卖 put 好不好做」的客观事实。

⚠️ **只并排事实，不给推荐、不给综合分、不排名**（测试钉住键名）。「愿不愿意持有」是估值判断，永远是用户的：这里只存用户给的名单与接货价，
并显示现价离它多远、最接近且不高于它的合格行权价是多少。重放用的是过去两年（多数标的在涨）、30 天窗口只有约 20 来个互不重叠的样本，
**偏乐观且样本很小**——页面首行写明，别当成证据。

两层数据（纯函数，**本模块不读文件、不联网**；数据由 `alphabot/service.py` 取来传进来）：
  · 离线层 = 卖权账本最近一日的 put 梯子（每只标的约 30 天一档，|delta| 0.20 / 0.30 两档）：点差、OI、年化、单张占资金、财报是否在到期前。
    账本的行只读它的梯子与财报日，**不碰任何结算 / 路由字段**（盲期）；
  · 行情层 = 日线统计（回撤 / 波动 / 过去 21 日的最差与跌超 10% 的频率）与「权利金重放」；
  · 取链层 = 对一只标的现取全链，按**接货账本同一条选合约规则**（`sell_put_wheel.pick_put`：期限窗口 + 不高于接货价的最高合格档）选出那一张，
    并给被行权净成本、单张占资金。选合约规则只有那一份，这里不另抄。

口径（和我们手工分析时一致，写在这里免得读者猜）：
  · 回撤 = 收盘距「过去 252 / 63 个交易日最高收盘」的跌幅；波动 = 近 252 个日对数收益的标准差 × √252；
  · 「21 日最差 / 跌超 10% 的频率」= 过去所有滚动 21 个交易日窗口（重叠窗口，不独立）；
  · 权利金重放：把今天这张 put 的 bid 放回过去每一个同长度窗口，持有到期，净收益 = (bid − max(行权价 − 到期价, 0)) ÷ 行权价；
    到期价 = 今天现价 × 该窗口的实际涨跌。它回答「如果过去两年这样走，今天的权利金够不够补」，**不是预测**。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence

import sell_put_wheel as W

CONTRACT = 100
RUNGS = ("0.20", "0.30")
TD_PER_YEAR = 252
DD52_WINDOW, DD3M_WINDOW, VOL_WINDOW, FWD_WINDOW = 252, 63, 252, 21
#: 日线少于这个数就不给统计（回撤 / 波动都需要一年左右的历史）
MIN_CLOSES = 120
#: 重放至少要这么多个窗口才给（太少时均值 / 分位没有意义）
MIN_REPLAY_WINDOWS = 20
MAX_WATCH = 40
MAX_ACCEPT = 60

#: 「这只算不出来」的原因码（唯一一张表；`tests/test_alphabot_wheel_screen.py` 双向核对）
REASONS = {
    "too_few_closes": "日线不够一年左右（不到 120 个交易日），回撤 / 波动 / 重放都不给",
    "no_ladder": "账本最近一日没有这只标的的 put 梯子",
    "no_replay_windows": "重放用的窗口太少",
    "no_chain": "现取期权链失败",
    "no_price": "取不到现价",
    "rate_limited": "Yahoo 日线被限流",
    "unavailable": "日线取不到",
    "demo": "演示模式不读账本 / 不联网（合成数据不是这些标的的记录）",
}


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v) -> Optional[float]:
    f = _num(v)
    return f if (f is not None and f > 0) else None


def _series(closes: Mapping[str, float], as_of: Optional[str] = None) -> List[float]:
    """`{日期: 收盘}` → 升序的有效收盘列表（≤ as_of；非正 / 非有限的点丢掉）。"""
    out = []
    for d in sorted(closes):
        if as_of is not None and d > as_of:
            continue
        c = _pos(closes[d])
        if c is not None:
            out.append(c)
    return out


# ═══════════════════════════ 行情统计 ═══════════════════════════

def price_stats(closes: Mapping[str, float], as_of: Optional[str] = None) -> dict:
    """日线统计。样本不够 ⇒ `{"ok": False, "reason": "too_few_closes", "n": n}`，不拿短样本凑数。"""
    v = _series(closes, as_of)
    n = len(v)
    if n < MIN_CLOSES:
        return {"ok": False, "reason": "too_few_closes", "n": n}
    last = v[-1]
    dates = [d for d in sorted(closes) if (as_of is None or d <= as_of) and _pos(closes[d]) is not None]
    hi52, hi3m = max(v[-DD52_WINDOW:]), max(v[-DD3M_WINDOW:])
    rets = [math.log(v[i] / v[i - 1]) for i in range(max(1, n - VOL_WINDOW), n)]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * math.sqrt(TD_PER_YEAR) * 100
    fwd = [v[i + FWD_WINDOW] / v[i] - 1.0 for i in range(n - FWD_WINDOW)]
    return {
        "ok": True, "n": n, "last": round(last, 4), "last_date": dates[-1],
        "dd52_pct": round((last / hi52 - 1) * 100, 2), "dd3m_pct": round((last / hi3m - 1) * 100, 2),
        "ret1m_pct": round((last / v[-22] - 1) * 100, 2) if n > 22 else None,
        "ret3m_pct": round((last / v[-64] - 1) * 100, 2) if n > 64 else None,
        "vol1y_pct": round(vol, 1),
        "worst21_pct": round(min(fwd) * 100, 2) if fwd else None,
        "p21_le_m10_pct": round(sum(x <= -0.10 for x in fwd) / len(fwd) * 100, 1) if fwd else None,
        "n_windows21": len(fwd),
    }


def replay_put(closes: Mapping[str, float], spot: float, strike: float, bid: float, dte: int,
               as_of: Optional[str] = None) -> dict:
    """权利金重放（见模块头口径）。持有天数 = dte × 252 / 365 个交易日（至少 1）。窗口太少 ⇒ 不给。"""
    v = _series(closes, as_of)
    hold = max(1, round(dte * TD_PER_YEAR / 365.0))
    n = len(v) - hold
    if n < MIN_REPLAY_WINDOWS:
        return {"ok": False, "reason": "no_replay_windows", "n_windows": max(n, 0), "hold_days": hold}
    net = sorted((bid - max(strike - spot * (v[i + hold] / v[i]), 0.0)) / strike * 100.0 for i in range(n))
    return {"ok": True, "hold_days": hold, "n_windows": n, "mean_net_pct": round(sum(net) / n, 2),
            "win_pct": round(sum(x > 0 for x in net) / n * 100, 1), "p05_net_pct": round(net[int(n * 0.05)], 2),
            "worst_net_pct": round(net[0], 2),
            "independent_windows": max(1, n // hold)}


# ═══════════════════════════ 账本梯子（离线层） ═══════════════════════════

def ladder_rungs(row: Optional[Mapping]) -> dict:
    """卖权账本一行（full 形状）里的 put 梯子 → `{档: 紧凑报价}`。只读梯子与到期 / 财报日，不碰路由 / 结算字段。"""
    out: Dict[str, Optional[dict]] = {k: None for k in RUNGS}
    lad = (row or {}).get("ladder") or {}
    dte = lad.get("dte") if lad.get("dte") is not None else (row or {}).get("dte")
    spot = _pos(lad.get("underlying_price")) or _pos((row or {}).get("underlying_price"))
    for k in RUNGS:
        s = (((lad.get("put") or {}).get(k)) or {}).get("short")
        if not s or _pos(s.get("strike")) is None or _num(s.get("bid")) is None:
            continue
        bid, ask, K = _num(s["bid"]), _num(s.get("ask")), _pos(s["strike"])
        mid = _num(s.get("mid"))
        out[k] = {"strike": K, "dist_pct": round((K / spot - 1) * 100, 1) if spot else None, "bid": bid, "ask": ask,
                  "spread_pct": round(_num(s.get("spread_pct")) * 100, 1) if _num(s.get("spread_pct")) is not None else None,
                  "oi": int(_num(s.get("oi")) or 0), "delta": _num(s.get("delta")),
                  "annual_yield_pct": round(W.annual_yield(bid, K, int(dte)), 1) if (dte and bid > 0) else None,
                  "capital_usd": round(K * CONTRACT, 0), "mid": mid}
    return {"dte": dte, "expiry": lad.get("expiry") or (row or {}).get("expiry"), "spot": spot, "rungs": out,
            "earnings_date": (row or {}).get("earnings_date"), "earnings_status": (row or {}).get("earnings_status")}


def earnings_before(earnings_date: Optional[str], as_of: Optional[str], expiry: Optional[str]) -> Optional[bool]:
    """财报日落在 (as_of, 到期日] 内 ⇒ True；日期缺 / 解析不了 ⇒ None（待验证，不说成 False）。"""
    try:
        e, a, x = (date.fromisoformat(str(v)[:10]) for v in (earnings_date, as_of, expiry))
    except (TypeError, ValueError):
        return None
    return a < e <= x


# ═══════════════════════════ 按接货价选合约（取链层） ═══════════════════════════

def pick_for_accept(contracts: Sequence[dict], spot: float, accept: Optional[float], as_of: str,
                    *, closes: Optional[Mapping[str, float]] = None, earnings_date: Optional[str] = None) -> dict:
    """现取全链后：用**接货账本同一条选合约规则**选出那一张，并给净成本 / 占资金 / 重放。"""
    c, why, info = W.pick_put(list(contracts), spot, accept, as_of)
    out = {"accept_price": info.get("accept_price"), "accept_price_source": info.get("accept_price_source"),
           "spot": spot, "distance_pct": round((info["accept_price"] / spot - 1) * 100, 1) if info.get("accept_price") else None}
    if c is None:
        return {**out, "ok": False, "reason": why}
    dte = W._dte(as_of, c["expiry"])
    bid, ask = float(c["bid"]), float(c["ask"])
    K = float(c["strike"])
    row = {**out, "ok": True, "symbol": c["symbol"], "strike": K, "expiry": c["expiry"], "dte": dte, "bid": bid, "ask": ask,
           "spread_pct": round(W._spread_pct(c) * 100, 1), "oi": int(_num(c.get("oi")) or 0), "delta": _num(c.get("delta")),
           "strike_dist_pct": round((K / spot - 1) * 100, 1),
           "annual_yield_pct": round(W.annual_yield(bid, K, dte), 1), "net_cost": round(K - bid, 4),
           "net_discount_pct": round(((K - bid) / spot - 1) * 100, 1), "capital_usd": round(K * CONTRACT, 0),
           "earnings_in_window": earnings_before(earnings_date, as_of, c["expiry"])}
    if closes is not None:
        row["replay"] = replay_put(closes, spot, K, bid, dte, as_of)
    return row


# ═══════════════════════════ 设置（名单 / 接货价）的校验 ═══════════════════════════

def clean_watch(raw, *, norm=lambda t: str(t).strip().upper()) -> List[str]:
    """名单：去重保序、最多 `MAX_WATCH` 只；格式不对抛 ValueError（调用方转 400）。"""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("wheel_watch 须为数组")
    seen: List[str] = []
    for t in raw:
        n = norm(t)
        if not n or len(n) > 12:
            raise ValueError(f"标的代码不合格：{t!r}")
        if n not in seen:
            seen.append(n)
    if len(seen) > MAX_WATCH:
        raise ValueError(f"名单最多 {MAX_WATCH} 只（每只取链约 4–7 秒、串行）")
    return seen


def clean_accept(raw, *, norm=lambda t: str(t).strip().upper()) -> Dict[str, Optional[float]]:
    """接货价：`{标的: 正数 | None}`；None = 清掉（回到占位）。非正 / 非有限 / 非数字抛 ValueError。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("wheel_accept 须为对象 {代码: 价格}")
    if len(raw) > MAX_ACCEPT:
        raise ValueError(f"接货价最多 {MAX_ACCEPT} 条")
    out: Dict[str, Optional[float]] = {}
    for k, v in raw.items():
        t = norm(k)
        if v is None or v == "":
            out[t] = None
            continue
        f = _pos(v) if not isinstance(v, str) else _pos(v.strip())
        if f is None or f > 1_000_000:
            raise ValueError(f"{k} 的接货价不合格：{v!r}（要正数）")
        out[t] = round(f, 4)
    return out


# ═══════════════════════════ 拼表 ═══════════════════════════

def build_rows(tickers: Sequence[str], ledger_rows: Mapping[str, Mapping], stats: Mapping[str, dict],
               accepts: Mapping[str, Optional[float]], *, as_of: Optional[str], in_universe: Sequence[str] = ()) -> List[dict]:
    """每只标的一行。**没有任何排名 / 推荐 / 综合分字段**；缺的一律带原因，不写 0。"""
    rows = []
    for t in tickers:
        lad = ladder_rungs(ledger_rows.get(t)) if t in ledger_rows else None
        st = stats.get(t)
        acc = _pos(accepts.get(t)) if accepts.get(t) is not None else None
        spot = (lad or {}).get("spot") or (st or {}).get("last")
        row = {"ticker": t, "in_wheel_universe": t in in_universe,
               "accept_price": acc, "accept_price_source": "user" if acc else "placeholder",
               "accept_distance_pct": round((acc / spot - 1) * 100, 1) if (acc and spot) else None,
               "ladder": None if lad is None else {k: v for k, v in lad.items()},
               "ladder_reason": None if lad else "no_ladder", "stats": st}
        if lad:
            ed, dte = lad.get("earnings_date"), lad.get("dte")
            exp = lad.get("expiry")
            row["earnings_in_ladder_window"] = earnings_before(ed, as_of, exp)
            for k in RUNGS:
                r = lad["rungs"].get(k)
                if r:
                    r["spread_ok"] = r["spread_pct"] is not None and r["spread_pct"] <= W.CONFIG["max_spread_pct"] * 100
                    r["oi_ok"] = r["oi"] >= W.CONFIG["min_oi"]
            row["ladder"]["dte"] = dte
        rows.append(row)
    return rows


def public_rules() -> dict:
    return {"rungs": list(RUNGS), "min_closes": MIN_CLOSES, "min_replay_windows": MIN_REPLAY_WINDOWS,
            "max_watch": MAX_WATCH, "dd52_window": DD52_WINDOW, "dd3m_window": DD3M_WINDOW, "vol_window": VOL_WINDOW,
            "fwd_window": FWD_WINDOW, "max_spread_pct": W.CONFIG["max_spread_pct"], "min_oi": W.CONFIG["min_oi"],
            "put_dte_range": list(W.CONFIG["put_dte_range"]), "put_dte_target": W.CONFIG["put_dte_target"],
            "placeholder_otm_pct": W.CONFIG["placeholder_otm_pct"], "reasons": dict(REASONS)}
