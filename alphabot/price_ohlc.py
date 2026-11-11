"""标的「价格」页 K 线的开 / 高 / 低（v0.45.762）——只给那张图画影线用。

为什么另取、不从共享日线里拿
----------------------------
价格页的日线主干是 Twelve Data 共享入口 `twelve_data.fetch_bars`，它的行只有 `{date, close, vol}`。接口本身返回开高低，
但那个解析函数在输入层代码指纹守卫范围内、又有十来个生产读者，不为一张展示图去动它。开高低改从 yfinance 取
（经全仓唯一日线入口 `yf_daily`、限流闸 `yf_gate`，与 `beta_long` / `wheel_prices` 同一套做法），按日期拼到主干上。

口径
----
· 主干不变：收盘、成交量、有哪几天，仍只认 Twelve Data；yfinance 多出来的日子（含当天没收完的那根）一律不用；
· `auto_adjust=False`：Twelve Data 缺省只按拆股复权、不按分红复权，对得上的是 yfinance **未做分红复权**的那套价；
· 逐根对账：yfinance 当日收盘与主干收盘相差超过 `CLOSE_TOL` ⇒ 这根不拼开高低（两个源说的不是同一个价，
  拼出来的影线是假的），计入 `close_mismatch`；
· 影线两端把主干收盘包进去（高 ≥ 收、低 ≤ 收）——容差内的小差异不该画出「收盘在影线外」的柱子；
· 取不到 / 被限流都**带原因返回**，页面照实写并退回「前收 → 收盘」的涨跌柱，不当没发生。

**只展示**：不进账本、不进任何交易决定、不进评分。
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

from alphabot import beta_long as BL

#: 主干是 120 根日线（约 5.7 个月），多取一段给节假日留余量
PERIOD = "9mo"
#: 两个源的收盘允许差多少（相对值）。同一口径下两边只差在小数位；差到这个量级以上就是口径不同（分红 / 拆股没对齐）
CLOSE_TOL = 0.005
SOURCE = "yfinance"
_FIELDS = ("Open", "High", "Low", "Close")


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def rows_from_history(hist) -> Dict[str, Dict[str, float]]:
    """yfinance 日线 DataFrame → `{YYYY-MM-DD: {open, high, low, close}}`。四个价有一个缺 / 非正 / 高低颠倒的行整行跳过。"""
    out: Dict[str, Dict[str, float]] = {}
    cols = getattr(hist, "columns", ())
    if hist is None or not hasattr(hist, "index") or any(f not in cols for f in _FIELDS):
        return out
    for ts, o, h, lo, c in zip(hist.index, *(hist[f] for f in _FIELDS)):
        o, h, lo, c = _num(o), _num(h), _num(lo), _num(c)
        if None in (o, h, lo, c) or h < lo:
            continue
        out[str(ts)[:10]] = {"open": o, "high": h, "low": lo, "close": c}
    return out


def merge(bars: List[dict], ohlc: Dict[str, Dict[str, float]], tol: float = CLOSE_TOL) -> Tuple[List[dict], Dict[str, int]]:
    """把开高低按日期拼到主干日线上（纯函数，不改传入的行）。返回 `(新行, {matched, missing, close_mismatch})`。

    只给对得上账的那几根加 `open` / `high` / `low`；其余行原样——页面按「这根有没有开高低」逐根决定怎么画。"""
    out: List[dict] = []
    n = {"matched": 0, "missing": 0, "close_mismatch": 0}
    for r in bars:
        row = dict(r)
        c = _num(row.get("close"))
        y = ohlc.get(str(row.get("date"))[:10])
        if c is None or y is None:
            n["missing"] += 1
        elif abs(y["close"] / c - 1.0) > tol:
            n["close_mismatch"] += 1
        else:
            row["open"] = y["open"]
            row["high"] = max(y["high"], y["open"], c)
            row["low"] = min(y["low"], y["open"], c)
            n["matched"] += 1
        out.append(row)
    return out, n


def _fetch(yf_module, ticker: str, ensure: Callable[[], None]):
    import yf_daily
    ensure()
    # 末根是不是公司行动空行不用管：只按主干的日期取用（登记的策略 = ignore）。source 必须是字面量（守卫按字面量核对注册表）
    hist, _ = yf_daily.history(yf_module.Ticker(ticker), source="alphabot_price_ohlc", period=PERIOD, auto_adjust=False)
    return rows_from_history(hist)


def fetch_ohlc(ticker: str, *, yf_module=None) -> Tuple[Dict[str, Dict[str, float]], dict]:
    """`({日期: {open, high, low, close}}, {n, reason, retry_after_sec?})`。`reason` 为 None = 取到了。永不抛。
    `yf_module` 只给测试注入（注入时不装限流闸）。"""
    t = str(ticker or "")
    if t.startswith("_"):                 # 指数（_SPX / _XSP）：Yahoo 的写法没验证过，不猜
        return {}, {"n": 0, "reason": "index_not_supported"}
    ensure: Callable[[], None] = lambda: None  # noqa: E731
    try:
        if yf_module is None:
            import yfinance as yf_module  # noqa: PLC0415
            try:
                from yf_gate import ensure  # noqa: PLC0415
            except Exception:  # noqa: BLE001 - 闸门不可得不阻断（与 beta_long / wheel_prices 同）
                pass
        rows = _fetch(yf_module, t, ensure)
    except Exception as exc:  # noqa: BLE001 - 取不到只是没有影线，页面照实写
        if BL._rate_limited(exc):
            return {}, {"n": 0, "reason": "rate_limited", "retry_after_sec": BL._retry_after()}
        return {}, {"n": 0, "reason": f"exception:{type(exc).__name__}"}
    return rows, {"n": len(rows), "reason": None if rows else "unavailable"}
