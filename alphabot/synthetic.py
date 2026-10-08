"""合成期权链：`--demo` 模式与测试用（零网络、确定性）。

产出与 CBOE 取数层同形的 payload（顶层 ticker / as_of / vintage_date / underlying_price / … / contracts，
每张合约 symbol / expiry / cp / strike / dte / iv / delta / gamma / oi / volume / bid / ask …），
所以下游走的是和生产完全相同的计算路径——只是链是编出来的。

⚠️ **不是行情**：页面在 demo 模式下整页标注「演示数据」。这里的 BS 只为给合约编出自洽的
delta / gamma / 报价，刻意不 import 卖权计算层（火墙：只有 `alphabot/service.py` 登记为它的调用方）。
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

_R = 0.045
DEMO_DTES = (3, 10, 17, 24, 31, 38, 59, 94)


def _today_et() -> date:
    """演示数据的「今天」= 美东日期（v0.45.428）。服务层的跨式账本与报价都按美东日期造；这里若用本机日期
    （太平洋时间），每晚 21:00–24:00 PT 两边差一天 ⇒ 水平链与持仓合约的到期日对不上。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date()


def _seed(ticker: str) -> int:
    return int(hashlib.sha256(ticker.encode("utf-8")).hexdigest()[:8], 16)


def _rand(seed: int, i: int) -> float:
    """[0,1) 的确定性伪随机数（不用全局 random，免得污染别处的随机状态）。"""
    x = (seed * 2654435761 + i * 40503) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 1274126177) & 0xFFFFFFFF
    return (x & 0xFFFFFF) / float(0x1000000)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs(S: float, K: float, T: float, iv: float, cp: str) -> Tuple[float, float, float]:
    """(价格, delta, gamma)。"""
    vt = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (_R + 0.5 * iv * iv) * T) / vt
    d2 = d1 - vt
    if cp == "C":
        price = S * _ncdf(d1) - K * math.exp(-_R * T) * _ncdf(d2)
        delta = _ncdf(d1)
    else:
        price = K * math.exp(-_R * T) * _ncdf(-d2) - S * _ncdf(-d1)
        delta = _ncdf(d1) - 1.0
    return price, delta, _npdf(d1) / (S * vt)


def spot_for(ticker: str) -> float:
    return round(40.0 + 460.0 * _rand(_seed(ticker), 1), 2)


def _step(S: float) -> float:
    for lim, st in ((25, 0.5), (60, 1.0), (150, 2.5), (400, 5.0)):
        if S < lim:
            return st
    return 10.0


def demo_payload(ticker: str, as_of: Optional[str] = None, *, spot: Optional[float] = None,
                 intraday_shift: float = 0.0) -> dict:
    """某票的合成链。`intraday_shift`（现价的相对偏移）给盘中快照演示用。"""
    t = str(ticker).upper()
    seed = _seed(t)
    d0 = date.fromisoformat(as_of) if as_of else _today_et()
    S = (spot if spot is not None else spot_for(t)) * (1.0 + intraday_shift)
    base_iv = 0.22 + 0.45 * _rand(seed, 2)
    step = _step(S)
    # 两团 OI：call 偏上方、put 偏下方；再按标的调一个 put 权重，让不同票落在不同 gamma 政体
    put_weight = 0.6 + 1.2 * _rand(seed, 3)
    call_center = S * (1.04 + 0.05 * _rand(seed, 4))
    put_center = S * (0.95 - 0.05 * _rand(seed, 5))
    contracts: List[dict] = []
    k0 = math.floor(S * 0.6 / step) * step
    strikes = [round(k0 + i * step, 4) for i in range(int(S * 0.8 / step) + 1)]
    for di, dte in enumerate(DEMO_DTES):
        exp = (d0 + timedelta(days=dte)).isoformat()
        T = max(dte, 0.5) / 365.0
        for ki, K in enumerate(strikes):
            m = math.log(K / S)
            iv = max(0.05, base_iv * (1.0 - 0.9 * m + 1.6 * m * m) * (1.0 + 0.15 / math.sqrt(dte)))
            for cp in ("C", "P"):
                price, delta, gamma = _bs(S, K, T, iv, cp)
                center, w = (call_center, 1.0) if cp == "C" else (put_center, put_weight)
                bump = math.exp(-((K - center) / (S * 0.06)) ** 2) + 0.35 * math.exp(-((K - S) / (S * 0.03)) ** 2)
                round_bonus = 1.8 if abs(K / (step * 4) - round(K / (step * 4))) < 1e-9 else 1.0
                noise = 0.6 + 0.8 * _rand(seed, 1000 + di * 997 + ki * 7 + (0 if cp == "C" else 3))
                oi = float(int(w * 9000 * bump * round_bonus * noise * (1.0 - 0.06 * di)))
                vol = float(int(oi * (0.05 + 0.3 * _rand(seed, 5000 + di * 991 + ki * 5 + (1 if cp == "C" else 2)))))
                if price < 0.01:
                    bid, ask = 0.0, 0.02
                else:
                    half = max(0.01, price * (0.03 + 0.05 * _rand(seed, 9000 + ki + di)))
                    bid, ask = round(max(price - half, 0.0), 2), round(price + half, 2)
                sym = f"{t.replace('.', '').replace('-', '')}{exp[2:4]}{exp[5:7]}{exp[8:10]}{cp}{int(round(K * 1000)):08d}"
                contracts.append({"symbol": sym, "expiry": exp, "cp": cp, "strike": K, "dte": dte, "iv": iv,
                                  "delta": delta, "gamma": gamma, "vega": None, "theta": None, "theo": price,
                                  "oi": oi, "volume": vol, "bid": bid, "ask": ask,
                                  "last_trade_time": f"{d0.isoformat()}T16:00:00"})
    return {"ticker": t, "as_of": d0.isoformat(), "vintage_date": d0.isoformat(),
            "underlying_price": round(S, 2), "underlying_price_source": "demo",
            "session_live": False, "fetched_at": f"{d0.isoformat()}T16:20:00",
            "payload_last_trade_time": f"{d0.isoformat()}T16:00:00", "iv30": round(base_iv * 100, 2),
            "contracts": contracts, "n_raw": len(contracts), "n_dropped_unparseable": 0,
            "n_expired_excluded": 0, "n_expiring_today_excluded": 0}


def demo_fetch(ticker: str, *, as_of: Optional[str] = None):
    """`fetch_fn` 形状：`(payload, None)`。"""
    return demo_payload(ticker, as_of), None


def _vega_theta(S: float, K: float, T: float, iv: float, cp: str) -> Tuple[float, float]:
    """(每 1 个波动率点的 vega, 每日历日的 theta)——与 CBOE 报价的单位相同（portfolio_greeks 模块头）。"""
    vt = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (_R + 0.5 * iv * iv) * T) / vt
    d2 = d1 - vt
    decay = -S * _npdf(d1) * iv / (2.0 * math.sqrt(T))
    carry = _R * K * math.exp(-_R * T) * (_ncdf(d2) if cp == "C" else -_ncdf(-d2))
    return S * _npdf(d1) * math.sqrt(T) / 100.0, (decay - carry) / 365.0


def _held(chain: dict, symbols: List[str]) -> Dict[str, Optional[dict]]:
    """合成链里按符号挑合约，整理成 `cboe_options._qs_contract` 的形状（vega / theta 用 BS 补上）。"""
    S = chain["underlying_price"]
    by = {c["symbol"]: c for c in chain["contracts"]}
    out: Dict[str, Optional[dict]] = {}
    for s in symbols:
        c = by.get(s)
        if c is None:
            out[s] = None
            continue
        ok = c["bid"] > 0 and c["ask"] >= c["bid"]
        mid = round((c["bid"] + c["ask"]) / 2.0, 4) if ok else None
        vega, theta = _vega_theta(S, c["strike"], max(c["dte"], 0.5) / 365.0, c["iv"], c["cp"])
        out[s] = {"symbol": s, "type": c["cp"], "role": "held", "strike": c["strike"], "expiry": c["expiry"],
                  "dte": c["dte"], "bid": c["bid"], "ask": c["ask"], "mid": mid,
                  "spread_pct": round((c["ask"] - c["bid"]) / mid, 4) if mid else None, "iv": c["iv"],
                  "delta": c["delta"], "gamma": c["gamma"], "vega": vega, "theta": theta, "oi": c["oi"],
                  "volume": c["volume"], "quote_ok": ok}
    return out


def demo_quote_held(ticker: str, symbols: List[str], as_of: Optional[str] = None) -> dict:
    """`cboe_options.quote_held` 的形状（演示用，零网络）。"""
    chain = demo_payload(ticker, as_of)
    return {"available": True, "reason": None, "underlying_price": chain["underlying_price"],
            "underlying_price_source": "demo", "session_live": False, "vintage_date": chain["vintage_date"],
            "payload_last_trade_time": chain["payload_last_trade_time"], "quotes": _held(chain, symbols)}


#: (代码, 方向, 当前 mark 相对入场权利金的倍数)：两笔赚、两笔亏
_DEMO_BOOK = (("NVDA", "short", 0.86), ("JNJ", "short", 1.09), ("TSLA", "long", 0.82), ("AMZN", "long", 1.12))
_DEMO_CLOSED = (("COST", "short", 1.34, 3.47, 2.00, 0.46), ("MU", "long", 0.70, 4.40, 3.03, -0.31),
                ("META", "short", 1.52, 4.20, 4.90, -0.18))
_DEMO_EVENTS = (("XOM", 1.05, 2.40, 1.70), ("CRM", 1.41, 6.10, 7.30), ("ORCL", 0.72, 5.20, 8.40))


def demo_straddle_ledger(as_of: Optional[str] = None) -> dict:
    """合成的财报跨式账本，形状同 `alphabot.straddle.load_ledger` 的返回（演示与测试用，零网络、确定性）。"""
    d0 = date.fromisoformat(as_of) if as_of else _today_et()
    iso = d0.isoformat
    start, risk = 100_000.0, 6_000.0
    positions, signals, greeks_rows = [], [], []
    for i, (t, side, drift) in enumerate(_DEMO_BOOK):
        chain = demo_payload(t, iso())
        S = chain["underlying_price"]
        exp = (d0 + timedelta(days=24)).isoformat()
        strikes = sorted({c["strike"] for c in chain["contracts"] if c["expiry"] == exp})
        K = min(strikes, key=lambda k: abs(k - S * (1.0 + 0.012 * (i - 1.5))))
        cs = next(c["symbol"] for c in chain["contracts"] if c["expiry"] == exp and c["strike"] == K and c["cp"] == "C")
        ps = next(c["symbol"] for c in chain["contracts"] if c["expiry"] == exp and c["strike"] == K and c["cp"] == "P")
        q = _held(chain, [cs, ps])
        mark = round(q[cs]["mid"] + q[ps]["mid"], 4)
        entry = round(mark / drift, 2)
        n = max(1, int(risk // (entry * 100.0)))
        label, ratio = ("rich", 1.38 + 0.1 * i) if side == "short" else ("cheap", 0.64 + 0.04 * i)
        entry_date = (d0 - timedelta(days=9 + i)).isoformat()
        ed = (d0 + timedelta(days=19)).isoformat()
        positions.append({"ticker": t, "side": side, "entry_date": entry_date, "expiry": exp, "strike": K,
                          "call_symbol": cs, "put_symbol": ps, "contracts": n,
                          "entry_call": round(entry * 0.5, 2), "entry_put": round(entry * 0.5, 2),
                          "entry_premium": entry, "entry_underlying": round(S * (1 - 0.01 * (i - 1.5)), 2),
                          "earnings_date": ed, "signal_ratio": ratio, "label": label, "size_usd": round(entry * 100 * n, 2),
                          "last_mark": mark, "last_mark_date": iso(), "mark_source": "cboe_mid", "stale_days": 0,
                          "rationale": f"演示：{label} ratio={ratio}"})
        implied = round(4.0 * ratio, 4)
        for day in (entry_date, iso()):
            signals.append({"ticker": t, "as_of": day, "eligible": True, "label": label, "raw_label": label,
                            "tradeable": True, "earnings_date": ed, "selected_expiry": exp, "ratio": ratio,
                            "implied_event_move_pct": implied, "hist_median_abs_move_pct": 4.0, "hist_n": 8,
                            "max_leg_spread_pct": 0.04, "event_move_basis": "straddle_minus_diffusion_plus_window",
                            "underlying_price": S, "quote_fetched_at": f"{day}T17:20:00-04:00", "market_open": False,
                            "realized_abs_move_pct": None, "realized_ratio": None})
        for sym, cp in ((cs, "call"), (ps, "put")):
            leg = q[sym]
            greeks_rows.append({"kind": "option", "ticker": t, "symbol": sym, "cp": cp, "side": side,
                                "delta": leg["delta"], "gamma": leg["gamma"], "vega": leg["vega"],
                                "theta": leg["theta"], "iv": leg["iv"], "mid": leg["mid"], "quote_missing": False})
    closed = []
    for j, (t, side, ratio, implied, realized, ret) in enumerate(_DEMO_CLOSED):
        entry_date = (d0 - timedelta(days=40 - 9 * j)).isoformat()
        ed = (d0 - timedelta(days=22 - 8 * j)).isoformat()
        size = 4_800.0 + 500.0 * j
        closed.append({"ticker": t, "side": side, "label": "rich" if side == "short" else "cheap",
                       "entry_date": entry_date, "exit_date": (d0 - timedelta(days=21 - 8 * j)).isoformat(),
                       "expiry": (d0 - timedelta(days=17 - 8 * j)).isoformat(), "strike": 100.0 + 50 * j,
                       "contracts": 2, "entry_premium": round(size / 200.0, 2),
                       "exit_premium": round(size / 200.0 * (1 - ret if side == "short" else 1 + ret), 2),
                       "earnings_date": ed, "signal_ratio": ratio, "size_usd": size,
                       "pnl_usd": round(size * ret, 2), "pnl_pct": round(ret * 100.0, 4), "exit_reason": "post_event",
                       "mark_source": "cboe_mid", "holding_days": 18, "rationale": "演示"})
        signals.append({"ticker": t, "as_of": entry_date, "eligible": True, "label": closed[-1]["label"],
                        "raw_label": closed[-1]["label"], "tradeable": True, "earnings_date": ed, "ratio": ratio,
                        "implied_event_move_pct": implied, "realized_abs_move_pct": realized,
                        "realized_ratio": round(realized / implied, 4)})
    for k, (t, ratio, implied, realized) in enumerate(_DEMO_EVENTS):
        signals.append({"ticker": t, "as_of": (d0 - timedelta(days=35 - 6 * k)).isoformat(), "eligible": True,
                        "label": "fair", "raw_label": "rich" if ratio >= 1.3 else ("cheap" if ratio <= 0.75 else "fair"),
                        "tradeable": False, "earnings_date": (d0 - timedelta(days=20 - 6 * k)).isoformat(),
                        "ratio": ratio, "implied_event_move_pct": implied, "realized_abs_move_pct": realized,
                        "realized_ratio": round(realized / implied, 4)})
    signals.append({"ticker": "WMT", "as_of": iso(), "eligible": False, "reason": "no upcoming earnings date"})
    realized_total = sum(c["pnl_usd"] for c in closed)
    unreal = sum((p["last_mark"] - p["entry_premium"]) * 100 * p["contracts"] * (1 if p["side"] == "long" else -1)
                 for p in positions)
    equity, n_days = [], 24
    for k in range(n_days):
        d = (d0 - timedelta(days=n_days - 1 - k)).isoformat()
        real = sum(c["pnl_usd"] for c in closed if c["exit_date"] <= d)
        u = unreal * (k / (n_days - 1)) + 300.0 * math.sin(k / 3.0) * (1 - k / (n_days - 1))
        equity.append({"date": d, "nav": round(start + real + u, 2), "unrealized": round(u, 2),
                       "open_premium_at_risk": round(sum(p["size_usd"] for p in positions), 2),
                       "positions": len(positions), "stale_positions": 0})
    mark_value = sum(p["last_mark"] * 100 * p["contracts"] * (1 if p["side"] == "long" else -1) for p in positions)
    nav = round(start + realized_total + unreal, 2)
    equity[-1].update({"nav": nav, "unrealized": round(unreal, 2), "cash": round(nav - mark_value, 2)})
    meta = {"version": "demo", "starting_capital": start, "starting_date": equity[0]["date"],
            "cash": round(nav - mark_value, 2), "last_run_date": iso(),
            "config_snapshot": {"starting_capital": start, "risk_per_trade_pct": 6.0, "max_open": 6},
            "skipped_entries": []}
    return {"ledger_dir": None, "exists": True, "source": "demo", "meta": meta, "meta_error": None,
            "positions": positions, "closed": closed, "equity": equity, "signals": signals, "bad_lines": {},
            "greeks": {"as_of": iso(), "rows": greeks_rows}, "greeks_error": None}


def demo_bars(ticker: str, as_of: Optional[str] = None, days: int = 120) -> List[Dict[str, float]]:
    """合成日线（只到 as_of 当日，收盘价随机游走收敛到合成现价）。"""
    t = str(ticker).upper()
    seed = _seed(t)
    d_end = date.fromisoformat(as_of) if as_of else _today_et()
    closes, px = [], spot_for(t)
    for i in range(days):
        closes.append(px)
        px = px / (1.0 + 0.022 * (_rand(seed, 20000 + i) - 0.5) * 2.0)
    closes.reverse()
    out, d, i = [], d_end, len(closes) - 1
    while i >= 0:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "close": round(closes[i], 2)})
            i -= 1
        d -= timedelta(days=1)
    return list(reversed(out))
