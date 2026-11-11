"""标的「价格」页的盘中 1 分钟 K 线（v0.45.763）——只给那张图用。

数据
----
· Yahoo 1 分钟线，经全仓唯一入口 `yf_daily`、限流闸 `yf_gate`（与 `price_ohlc` / `beta_long` 同一套做法）。**不走 Twelve Data**：
  免费档每分钟 8 次、与每日扫描共用，盘中轮询会挤占扫描。
· **一次只取一个美东日**。Yahoo 的 1 分钟线只留最近几天（2026-11-11 实测：一次请求最多 8 天），早于 `MAX_AGE_DAYS` 的日子不发请求、
  直接回 `out_of_range`。
· **没收完的那根丢掉**：盘中取到的末根是正在走的这一分钟（实测成交量为 0），画出来是一根假的十字线。
· 只取常规交易时段（`prepost=False`），未做分红复权（盘中价就是屏幕上的价）。

水平路径
--------
`level_path` 把**同一天**的盘中快照里的 Zero Gamma / 净 Major± 排成按时刻的点列。**只许单日**：把某天的水平和其后日子的价格画在一起
属于预注册检验冻结前锁住的内容（见 `service.intraday` 的说明）；这里只收时刻落在这一天的快照。点的时刻用快照里行情自己的
「最后成交时刻」（CBOE 延迟约 15 分钟），没有才退回落盘时刻——水平是按那一刻的链算的，画在落盘时刻会比价格晚一截。

**只展示**：不进账本、不进任何交易决定、不进评分。取不到 / 被限流都带原因返回，永不抛。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from alphabot import beta_long as BL

SOURCE = "yfinance"
INTERVAL = "1m"
#: 早于今天这么多个日历日的就不去取了（Yahoo 一次最多给 8 天的 1 分钟线，留一天余量）
MAX_AGE_DAYS = 7
#: 页面开着时多久刷一次（秒）。1 分钟一根，刷得更勤也没有新东西
REFRESH_SEC = 60
#: 成交量异常值：超过当天（有量的）中位数这么多倍的那根，成交量记缺失。2026-11-11 实测 NVDA 207 根里有 7 根是
#: 6700 万～1.66 亿股（正常一根约 60 万、全天才两亿上下），且随时间单调递增——疑似 Yahoo 把当日累计量记进了单根（推断，待验证）。
#: 照画的话副图只剩那几根。开盘第一根真实放量通常是中位数的几倍到十几倍，20 倍留了余量
VOLUME_OUTLIER_X = 20.0
#: 有量的线少于这么多根时不判异常（中位数不稳）
VOLUME_MIN_BARS = 20
#: 原因码唯一表（页面翻成人话用；测试双向核对）
REASONS = {
    "rate_limited": "Yahoo 限流",
    "out_of_range": "超出 Yahoo 可取范围（1 分钟线只留最近几天）",
    "future_date": "这一天还没到",
    "index_not_supported": "指数票暂不取分钟线",
    "unavailable": "Yahoo 没有返回这一天的分钟线（非交易日，或还没开盘）",
    "exception": "取数出错",
}
_FIELDS = ("Open", "High", "Low", "Close")


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def rows_from_history(hist, day: str) -> List[dict]:
    """yfinance 分钟线 DataFrame → `[{t: "HH:MM", open, high, low, close, volume}]`（只留落在 `day` 的行，按时刻升序）。
    四个价有一个缺 / 非正 / 高低颠倒的行跳过；成交量缺记 None（不当 0）。时刻取索引自己的本地时刻（Yahoo 给的是交易所时区）。"""
    out: Dict[str, dict] = {}
    cols = getattr(hist, "columns", ())
    if hist is None or not hasattr(hist, "index") or any(f not in cols for f in _FIELDS):
        return []
    vols = hist["Volume"] if "Volume" in cols else [None] * len(hist.index)
    for ts, o, h, lo, c, v in zip(hist.index, *(hist[f] for f in _FIELDS), vols):
        s = str(ts)
        if s[:10] != day:
            continue
        o, h, lo, c = _num(o), _num(h), _num(lo), _num(c)
        if None in (o, h, lo, c) or h < lo:
            continue
        try:
            vol = float(v)
            vol = vol if math.isfinite(vol) and vol >= 0 else None
        except (TypeError, ValueError):
            vol = None
        o, h, lo, c = (round(x, 4) for x in (o, h, lo, c))      # Yahoo 给的是 float32 转出来的长尾小数，4 位足够
        out[s[11:16]] = {"t": s[11:16], "open": o, "high": max(h, o, c), "low": min(lo, o, c), "close": c, "volume": vol}
    return [out[k] for k in sorted(out)]


def drop_forming(rows: List[dict], day: str, now: datetime) -> Tuple[List[dict], int]:
    """丢掉还没收完的分钟（纯函数）。`now` 是美东此刻；一根 `HH:MM` 的线在 `HH:MM+1` 才收完。返回 `(留下的, 丢了几根)`。
    不是今天 ⇒ 原样（那天早收完了）。"""
    if day != now.date().isoformat():
        return list(rows), 0
    cur = now.strftime("%H:%M")
    kept = [r for r in rows if r["t"] < cur]
    return kept, len(rows) - len(kept)


def drop_volume_outliers(rows: List[dict]) -> Tuple[List[dict], int]:
    """把成交量异常的那几根的 `volume` 记成 None（纯函数，不改传入的行；价格不动）。返回 `(新行, 记缺失了几根)`。
    原值留在 `volume_raw` 里（页面不画，但查的时候看得到）。"""
    vols = sorted(r["volume"] for r in rows if isinstance(r.get("volume"), (int, float)) and r["volume"] > 0)
    if len(vols) < VOLUME_MIN_BARS:
        return [dict(r) for r in rows], 0
    cap = vols[len(vols) // 2] * VOLUME_OUTLIER_X
    out, n = [], 0
    for r in rows:
        row = dict(r)
        v = row.get("volume")
        if isinstance(v, (int, float)) and v > cap:
            row["volume_raw"], row["volume"] = v, None
            n += 1
        out.append(row)
    return out, n


def level_path(snaps: List[dict], day: str, view: str = "le_45dte") -> List[dict]:
    """同一天的盘中快照 → `[{t: "HH:MM", zero_gamma, major_pos, major_neg}]`（按时刻升序；同一分钟留后到的）。纯函数。
    时刻不在 `day` 的快照不收（单日规则）；三个水平都没有的拍不收。"""
    out: Dict[str, dict] = {}
    for s in snaps or []:
        if not isinstance(s, dict):
            continue
        when = str(s.get("payload_last_trade_time") or "")
        if when[:10] != day or len(when) < 16:
            when = str(s.get("ts") or "")
        if when[:10] != day or len(when) < 16:
            continue
        v = ((s.get("views") or {}).get(view)) or {}
        zg, mj = (v.get("zero_gamma") or {}), (v.get("majors") or {})
        pt = {"t": when[11:16], "zero_gamma": _num(zg.get("nearest")),
              "major_pos": _num(mj.get("net_major_pos_strike")), "major_neg": _num(mj.get("net_major_neg_strike"))}
        if any(pt[k] is not None for k in ("zero_gamma", "major_pos", "major_neg")):
            out[pt["t"]] = pt
    return [out[k] for k in sorted(out)]


def _fetch(yf_module, ticker: str, day: str, today: str, ensure: Callable[[], None]):
    import yf_daily
    ensure()
    # 今天 ⇒ period="1d"；过去的某天 ⇒ 只要那一天。source 必须是字面量（守卫按字面量核对注册表）；
    # 非日线不做「最近一场缺没缺」的日历判断，末段由 `drop_forming` 自己处理（登记的策略 = ignore）
    span = {"period": "1d"} if day == today else {
        "start": day, "end": (date.fromisoformat(day) + timedelta(days=1)).isoformat()}
    hist, _ = yf_daily.history(yf_module.Ticker(ticker), source="alphabot_price_minute", interval="1m",
                               auto_adjust=False, prepost=False, **span)
    return rows_from_history(hist, day)


def fetch_minutes(ticker: str, day: str, *, now: datetime, yf_module=None) -> Tuple[List[dict], dict]:
    """`(分钟线, {n, reason, retry_after_sec?, dropped_forming})`。`reason` 为 None = 取到了。永不抛。
    `now` = 美东此刻（决定「今天」是哪天、哪一根还没收完）。`yf_module` 只给测试注入（注入时不装限流闸）。"""
    t = str(ticker or "")
    today = now.date().isoformat()
    if t.startswith("_"):                 # 指数（_SPX / _XSP）：Yahoo 的写法没验证过，不猜
        return [], {"n": 0, "reason": "index_not_supported"}
    if day > today:
        return [], {"n": 0, "reason": "future_date"}
    if (now.date() - date.fromisoformat(day)).days > MAX_AGE_DAYS:
        return [], {"n": 0, "reason": "out_of_range"}
    ensure: Callable[[], None] = lambda: None  # noqa: E731
    try:
        if yf_module is None:
            import yfinance as yf_module  # noqa: PLC0415
            try:
                from yf_gate import ensure  # noqa: PLC0415
            except Exception:  # noqa: BLE001 - 闸门不可得不阻断（与 beta_long / price_ohlc 同）
                pass
        rows = _fetch(yf_module, t, day, today, ensure)
    except Exception as exc:  # noqa: BLE001 - 取不到只是这张图没有，页面照实写
        if BL._rate_limited(exc):
            return [], {"n": 0, "reason": "rate_limited", "retry_after_sec": BL._retry_after()}
        return [], {"n": 0, "reason": "exception", "detail": type(exc).__name__}
    rows, dropped = drop_forming(rows, day, now)
    rows, suspect = drop_volume_outliers(rows)
    return rows, {"n": len(rows), "reason": None if rows else "unavailable", "dropped_forming": dropped,
                  "volume_suspect": suspect}
