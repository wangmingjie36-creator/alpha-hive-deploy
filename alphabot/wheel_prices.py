"""「接货 · 筛选」的日线取数（v0.45.722）——只给筛选页的行情层用（回撤 / 波动 / 权利金重放）。

和 `beta_long` 同一套做法：经全仓唯一的日线入口 `yf_daily`（限流闸 `yf_gate`），按需取、进程内缓存（由 service 管），
**不进账本、不进任何交易决定、不进评分**。取数失败 / 被限流都**带原因返回**，页面如实写，不当没发生：
被 Yahoo 限流后，本只与其后的标的都不再去撞（每多撞一次只是多一条 429），统一记 `rate_limited` 与 `retry_after_sec`，
已经取到的照常返回（调用方只补缺的）。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Sequence, Tuple

from alphabot import beta_long as BL

#: 回撤要 252 个交易日、重放要再多一段历史 ⇒ 取 2 年
PERIOD = "2y"


def _fetch(yf_module, ticker: str, ensure: Callable[[], None]) -> Dict[str, float]:
    import yf_daily
    ensure()
    # 末根是不是公司行动空行不用管：统计只用已有的收盘，缺最近一根只是少一天（登记的策略 = ignore）；
    # 复权收盘（回撤 / 波动 / 重放都该按总回报口径）。source 必须是字面量（守卫 `tests/test_yf_daily.py` 按字面量核对注册表）
    hist, _ = yf_daily.history(yf_module.Ticker(ticker), source="alphabot_wheel_screen", period=PERIOD, auto_adjust=True)
    return BL._closes_from_history(hist)


def fetch_closes(tickers: Sequence[str], *, yf_module=None) -> Tuple[Dict[str, Dict[str, float]], Dict[str, dict]]:
    """`({标的: {日期: 复权收盘}}, {标的: {n, reason, retry_after_sec?}})`。`reason` 为 None = 取到了。永不抛。
    `yf_module` 只给测试注入（注入时不装限流闸）。"""
    out: Dict[str, Dict[str, float]] = {}
    info: Dict[str, dict] = {}
    ensure: Callable[[], None] = lambda: None  # noqa: E731
    want: List[str] = list(dict.fromkeys(t for t in tickers if t))
    try:
        if yf_module is None:
            import yfinance as yf_module  # noqa: PLC0415
            try:
                from yf_gate import ensure  # noqa: PLC0415
            except Exception:  # noqa: BLE001 - 闸门不可得不阻断（与 beta_long / yf_chain 同）
                pass
        limited = False
        for t in want:
            if limited:
                out[t], info[t] = {}, {"n": 0, "reason": "rate_limited", "retry_after_sec": BL._retry_after()}
                continue
            try:
                closes = _fetch(yf_module, t, ensure)
            except Exception as exc:  # noqa: BLE001 - 单只取不到不连坐其余
                if BL._rate_limited(exc):
                    limited = True
                    out[t], info[t] = {}, {"n": 0, "reason": "rate_limited", "retry_after_sec": BL._retry_after()}
                    continue
                out[t], info[t] = {}, {"n": 0, "reason": f"exception:{type(exc).__name__}"}
                continue
            out[t] = closes
            info[t] = {"n": len(closes), "reason": None if closes else "unavailable"}
    except Exception as exc:  # noqa: BLE001 - 最外层兜底：导入 yfinance 失败等
        for t in want:
            out.setdefault(t, {})
            info.setdefault(t, {"n": 0, "reason": f"exception:{type(exc).__name__}"})
    return out, info
