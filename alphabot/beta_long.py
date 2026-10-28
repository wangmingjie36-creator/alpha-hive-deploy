"""长窗口 β（对 SPY，日收益 OLS）取数——只给对冲页「β 窗口对照」用（v0.45.662）。

为什么在这里、不在扫描里
------------------------
压力网格的 β 是审计里的 60 日值（`portfolio_greeks._default_beta`，日线来自 Twelve Data 的 120 根共享窗口）。要对照 250 日就得有
250+ 根日线——共享窗口里没有，另开一个更大的窗口会让 Twelve Data 每票多一次串行调用（7 次 / 分钟）、拖慢每日扫描。对照只是展示，所以
放在 Alpha Bot 里**按需**取：yfinance 日线（经全仓唯一入口 `yf_daily`，限流闸 `yf_gate`），进程内缓存，页面打开时才算、不阻塞对冲页。
**不进审计文件、不进任何交易决定**（见 `tail_budget` 的「β 窗口对照」一节）。

口径
----
· 日收益 = 复权收盘的逐日涨跌幅；对 SPY 的日收益做 OLS 斜率（与审计的 60 日 β 同一个定义，只是窗口不同）；
· 只用 `as_of`（含）以前的收益，取最近 `window` 个**两边都有**的交易日；样本少于 `MIN_OBS` ⇒ 判不了（None + 原因），不拿短样本冒充 250 日；
· 取数失败 / 样本不足都**带原因返回**，调用方（对照面板）如实写「沿用 60 日 β」，不当没发生。
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

WINDOW = 250
#: 样本少于这个数就判不了：窗口的 80%（250 → 200）。新上市 / 停牌多的票会落在这里，如实写原因
MIN_OBS = 200
BENCH = "SPY"
#: 一次要的日线跨度。250 个交易日 ≈ 1 年，多取半年给节假日 / 停牌留余量
PERIOD = "2y"


def returns_from_closes(closes: Dict[str, float]) -> Dict[str, float]:
    """`{日期: 收盘}` → `{日期: 当日收益}`（日期升序、相邻两个有效收盘之间的涨跌幅；非正 / 非有限的收盘跳过）。纯函数。"""
    out: Dict[str, float] = {}
    prev: Optional[float] = None
    for d in sorted(closes):
        c = closes[d]
        if c is None or not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0:
            prev = None                      # 坏点后不跨它连接：两侧各自断开
            continue
        if prev is not None:
            out[d] = c / prev - 1.0
        prev = float(c)
    return out


def ols_beta(stock_ret: Dict[str, float], bench_ret: Dict[str, float], as_of: str, window: int = WINDOW,
             min_obs: int = MIN_OBS) -> Tuple[Optional[float], int, Optional[str]]:
    """`(β, 样本数, 原因)`。原因：`insufficient_overlap`（两边都有的交易日少于 `min_obs`）/ `zero_variance`。纯函数。"""
    days = sorted(d for d in stock_ret if d in bench_ret and d <= as_of)[-window:]
    n = len(days)
    if n < min_obs:
        return None, n, "insufficient_overlap"
    xs = [bench_ret[d] for d in days]
    ys = [stock_ret[d] for d in days]
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0 or not math.isfinite(var):
        return None, n, "zero_variance"
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = cov / var
    return (b, n, None) if math.isfinite(b) else (None, n, "non_finite")


def _closes_from_history(hist) -> Dict[str, float]:
    """yfinance 日线 DataFrame → `{YYYY-MM-DD: 收盘}`（缺收盘的行跳过）。"""
    out: Dict[str, float] = {}
    if hist is None or not hasattr(hist, "index") or "Close" not in getattr(hist, "columns", ()):
        return out
    for ts, c in zip(hist.index, hist["Close"]):
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out[str(ts)[:10]] = v
    return out


def _fetch_closes(yf_module, ticker: str, ensure: Callable[[], None]) -> Dict[str, float]:
    import yf_daily
    ensure()
    # 末根是不是公司行动空行不用管：按日期对齐、只用 as_of 以前的收益，缺最近一根只是样本少一天（登记的策略 = ignore）
    # source 必须是字面量（守卫 `tests/test_yf_daily.py` 按字面量核对注册表），所以这里写字面量、不用常量
    hist, _ = yf_daily.history(yf_module.Ticker(ticker), source="alphabot_beta_long", period=PERIOD, auto_adjust=True)
    return _closes_from_history(hist)


def fetch_long_betas(tickers: Sequence[str], as_of: str, *, window: int = WINDOW, yf_module=None
                     ) -> Tuple[Dict[str, Optional[float]], Dict[str, dict]]:
    """各标的对 SPY 的 `window` 日 β。返回 `(β 表, 明细表)`；明细 `{ticker: {n, reason}}`，`reason` 为 None = 取到了。永不抛。
    `yf_module` 只给测试注入（注入时不装限流闸）。SPY 自己取不到 ⇒ 全部写 `bench_unavailable`。"""
    betas: Dict[str, Optional[float]] = {}
    info: Dict[str, dict] = {}
    ensure: Callable[[], None] = lambda: None  # noqa: E731
    try:
        if yf_module is None:
            import yfinance as yf_module  # noqa: PLC0415
            try:
                from yf_gate import ensure  # noqa: PLC0415
            except Exception:  # noqa: BLE001 - 闸门不可得不阻断（与 yf_chain 同）
                pass
        want: List[str] = sorted({t for t in tickers if t and t != BENCH})
        try:
            bench = returns_from_closes(_fetch_closes(yf_module, BENCH, ensure))
        except Exception as exc:  # noqa: BLE001
            bench, why = {}, f"exception:{type(exc).__name__}"
        else:
            why = None if bench else "bench_unavailable"
        for t in want:
            if why is not None or not bench:
                betas[t], info[t] = None, {"n": 0, "reason": why or "bench_unavailable"}
                continue
            try:
                b, n, reason = ols_beta(returns_from_closes(_fetch_closes(yf_module, t, ensure)), bench, as_of, window)
            except Exception as exc:  # noqa: BLE001 - 单只取不到不连坐其余
                b, n, reason = None, 0, f"exception:{type(exc).__name__}"
            betas[t], info[t] = b, {"n": n, "reason": reason}
    except Exception as exc:  # noqa: BLE001 - 最外层兜底：导入 yfinance 失败等
        for t in tickers:
            if t and t != BENCH:
                betas.setdefault(t, None)
                info.setdefault(t, {"n": 0, "reason": f"exception:{type(exc).__name__}"})
    return betas, info
