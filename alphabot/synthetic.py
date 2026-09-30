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
    d0 = date.fromisoformat(as_of) if as_of else date.today()
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


def demo_bars(ticker: str, as_of: Optional[str] = None, days: int = 120) -> List[Dict[str, float]]:
    """合成日线（只到 as_of 当日，收盘价随机游走收敛到合成现价）。"""
    t = str(ticker).upper()
    seed = _seed(t)
    d_end = date.fromisoformat(as_of) if as_of else date.today()
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
