#!/usr/bin/env python3
"""
Twelve Data 日K 客户端（v0.45.61）
==================================
补上整条链上**最后一处非 yfinance 不可**的依赖：逐标的 30 日收盘。
`rv_30d` / `iv_rv_spread` / hv_proxy 口径的 `iv_rank` 全部由它派生 ——
2026-08-27 yfinance 687 次 429，这三项各 0/30。

为什么是 Twelve Data
--------------------
2026-08-28 逐个实打验过的结论：

| 源 | 逐标的日K | 判定 |
|---|---|---|
| Finnhub `/stock/candle` | HTTP 403 | 已转付费 |
| Alpha Vantage | 25 次/天 | **已被 newsapi 的新闻情绪占满** |
| Stooq | JS 工作量证明挑战页 | 已上反爬 |
| FRED | 只有指数，无逐标的 | 不适用 |
| **Twelve Data** | **800 次/天、8 次/分** | 30 只 × 1 credit，唯一量级够的 |

配置
----
把 key 写进 `~/.alpha_hive_twelvedata_key`（一行，无引号）：

    echo 'YOUR_KEY' > ~/.alpha_hive_twelvedata_key && chmod 600 ~/.alpha_hive_twelvedata_key

**未配置时本模块直接返回 None**，降级链原样退回 yfinance（已限流到 0.5 req/s），
不报错、不阻断 —— 与 `FinnhubSource` 未配 key 时的行为一致。

⚠️ 限流口径与 yfinance 不同
---------------------------
免费档是 **8 次/分钟**。这不是 yfinance 那种「打到 429 就熔断很久」的软限，
而是明确的每分钟窗口。所以用**独立的**令牌桶（0.13 req/s ≈ 8/分），
不与 `resilience.yfinance_limiter` 共用 —— 共用会让两边互相拖慢，
而它们的配额本来就是分开的。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

try:
    from hive_logger import get_logger
    _log = get_logger("twelve_data")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.twelve_data")

_BASE = "https://api.twelvedata.com/time_series"

# 免费档 8 次/分。取 7/分留一成余量 —— 撞上限的代价是整批失败，
# 而慢一点只是慢一点。
_RATE_PER_SEC = 7.0 / 60.0
_BURST = 2

_limiter = None
_daily_used = 0          # 观测用：免费档 800/天，跨过就该知道
_DAILY_BUDGET = 800


class TwelveDataUnavailable(ConnectionError):
    """未配置 key、限流、或接口不可用。

    继承 `ConnectionError`（⊂ OSError ⊂ resilience.NETWORK_ERRORS），
    确保被现有的 `except NETWORK_ERRORS` 接住 —— v0.45.56 在 `YFRateLimited`
    上踩过这个坑：新造的异常若不继承既有网络异常族，会穿透所有降级路径。
    """


def _get_limiter():
    global _limiter
    if _limiter is None:
        try:
            from resilience import RateLimiter
            _limiter = RateLimiter(rate=_RATE_PER_SEC, burst=_BURST)
        except Exception:  # pragma: no cover
            return None
    return _limiter


def api_key() -> str:
    try:
        from config import get_secret
        return (get_secret("TWELVEDATA_API_KEY") or "").strip()
    except Exception:  # pragma: no cover
        import os
        return os.environ.get("TWELVEDATA_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(api_key())


def stats() -> Dict:
    return {"daily_used": _daily_used, "daily_budget": _DAILY_BUDGET,
            "configured": is_configured()}


def reset_stats() -> None:
    global _daily_used
    _daily_used = 0


def _et_today() -> Optional[str]:
    """美东当日日期。时区换算靠 zoneinfo（绝对时间正确即可），不靠本机 tz 设置。"""
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # pragma: no cover
        return None


def _drop_forming_bar(rows: List[dict], ticker: str = "") -> List[dict]:
    """去掉「当日盘中正在形成」的末根日线。

    2026-08-28 10:09 ET 实测：NVDA 末根 `2026-08-28 close=224.57 volume=103412`，
    而 8/24–8/27 的成交量是 1.2~3.0 亿 —— 开盘才十分钟的半根 bar。
    把它算进 RV30 等于用一个残缺的日收益污染波动率。

    项目里已有 `data_pipeline._drop_forming_bar`，但它靠 yfinance 探 SPY 分钟线
    判断交易所时间 —— 那正是本模块要绕开的东西。这里改用返回体自带的两个信号，
    不额外发请求：

      ① **日期**：末根日期 == 美东当日 → 今天的 bar，收盘前必然未完成
      ② **成交量**：末根 < 窗口中位数的 30% → 几乎只可能是半根

    两道各自独立、任一命中即丢。误丢的代价极小（30 根里少一根），
    漏丢的代价是波动率失真 —— 不对称，所以宁可宽。
    """
    if len(rows) < 5:
        return rows
    last = rows[-1]
    reason = ""

    today_et = _et_today()
    if today_et and last["date"] >= today_et:
        reason = f"日期 {last['date']} 是美东当日"
    else:
        vols = sorted(r["vol"] for r in rows[:-1] if r["vol"] > 0)
        if vols:
            med = vols[len(vols) // 2]
            if med > 0 and last["vol"] < med * 0.30:
                reason = f"成交量 {last['vol']:,.0f} 不足中位 {med:,.0f} 的 30%"

    if reason:
        _log.info("[%s] 丢弃盘中未完成的末根日线（%s）", ticker or "?", reason)
        return rows[:-1]
    return rows


def fetch_daily_closes(ticker: str, days: int = 60,
                       end_date: Optional[str] = None) -> Optional[List[float]]:
    """取日线收盘价，**按日期升序**（最旧 → 最新）。

    Parameters
    ----------
    days : 要多少根。`rv_30d` 需要 ≥31 根才能算 30 个收益率。
    end_date : "YYYY-MM-DD"。给定时取**截至该日**（含）的窗口，用于补跑；
        `None` = 最新。

    Returns
    -------
    收盘价列表，或 None（未配 key / 限流 / 接口异常 / 数据不足）。

    **绝不返回空列表或 0.0 填充** —— 下游 `np.std` 拿到常数列会算出
    `rv=0`，与「波动率真的是 0」不可区分（MEMORY 静默降级三件套）。
    """
    rows = _fetch_rows(ticker, days, end_date)
    if rows is None:
        return None

    closes = [r["close"] for r in rows]
    if len(closes) < 10:
        _log.warning("[%s] Twelve Data 有效收盘价仅 %d 根", ticker, len(closes))
        return None
    return closes


def _fetch_rows(ticker: str, days: int,
                end_date: Optional[str] = None) -> Optional[List[dict]]:
    """共享抓取层：一次请求拿回 `{date, close, vol}` 逐根日线（已剔除半根）。

    `fetch_daily_closes` / `fetch_volume_ratio` 共用同一次限流令牌 + 同一份
    解析逻辑，避免两个口径各发一次请求、各自出一套"哪天算今天"的判断。
    """
    key = api_key()
    if not key:
        return None

    params = {
        "symbol": ticker,
        "interval": "1day",
        "outputsize": str(max(days, 10)),
        "apikey": key,
        "order": "ASC",
    }
    if end_date:
        params["end_date"] = end_date

    rl = _get_limiter()
    if rl is not None and not rl.acquire(timeout=90.0):
        _log.warning("[%s] Twelve Data 限流令牌等待超时", ticker)
        return None

    try:
        from http_gate import urlopen_gated
        req = urllib.request.Request(
            f"{_BASE}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": "alpha-hive/1.0"})
        raw = urlopen_gated(req, timeout=25)
    except Exception as e:  # noqa: BLE001
        _log.warning("[%s] Twelve Data 请求失败: %s", ticker, e)
        return None

    global _daily_used
    _daily_used += 1

    try:
        d = json.loads(raw)
    except ValueError:
        _log.warning("[%s] Twelve Data 返回非 JSON", ticker)
        return None

    # 免费档超额/参数错时返回 {"code":..,"message":..,"status":"error"}，
    # HTTP 仍是 200 —— 只看状态码会把错误当成数据（这正是 8/26 那类
    # 「看着成功其实早废了」的形状）。
    if isinstance(d, dict) and d.get("status") == "error":
        _log.warning("[%s] Twelve Data 错误 %s: %s", ticker,
                     d.get("code"), str(d.get("message"))[:120])
        return None

    values = d.get("values") if isinstance(d, dict) else None
    if not values:
        _log.warning("[%s] Twelve Data 无 values 段", ticker)
        return None

    rows: List[dict] = []
    for row in values:
        try:
            c = float(row.get("close"))
        except (TypeError, ValueError, AttributeError):
            continue
        if c <= 0:
            continue
        try:
            v = float(row.get("volume") or 0)
        except (TypeError, ValueError):
            v = 0.0
        rows.append({"date": str(row.get("datetime") or "")[:10], "close": c, "vol": v})

    return _drop_forming_bar(rows, ticker)


def fetch_volume_ratio(ticker: str, window: int = 20,
                       end_date: Optional[str] = None) -> Optional[Dict]:
    """最新成交量 / 近 `window` 根均量（含当日）。算不出返回 None。

    与 `data_pipeline._fetch_history_metrics` 的 yfinance 口径保持一致：
    均量窗口含最新一根，不是"最新对比前 window 根"——两条腿走不同口径的话，
    同一只标的会因为落到哪条降级路径而算出不同的比值。

    **不兜底 1.0** —— 均量算不出（数据不足/为 0/NaN）时置 None，
    绝不让"量比正常"这个假象混进评分（MEMORY 静默降级三件套）。
    """
    rows = _fetch_rows(ticker, days=window + 10, end_date=end_date)
    if not rows or len(rows) < window:
        return None

    recent_vol = rows[-1]["vol"]
    window_vols = [r["vol"] for r in rows[-window:]]
    avg_vol = sum(window_vols) / len(window_vols)

    if recent_vol <= 0 or avg_vol <= 0:
        _log.warning("[%s] Twelve Data 成交量不可得或为 0，volume_ratio 置 None", ticker)
        return None

    return {
        "volume_ratio": recent_vol / avg_vol,
        "avg_volume": int(avg_vol),
        "recent_volume": int(recent_vol),
    }


def realized_vol(ticker: str, lookback: int = 30,
                 end_date: Optional[str] = None) -> Optional[float]:
    """年化已实现波动率（%）。算不出返回 None。

    与 `market_intelligence.calculate_iv_rv_spread` 的口径保持一致：
    对数收益、`ddof=1`、×√252×100、剔除 |log_ret| > 0.5 的异常跳升。
    口径不一致的话，同一只标的会因为走了哪条源而得到不同的 RV —— 那种
    差异会被误读成波动率变化。
    """
    import math

    closes = fetch_daily_closes(ticker, days=lookback + 30, end_date=end_date)
    if not closes or len(closes) < lookback // 2:
        return None

    rets = []
    for a, b in zip(closes[:-1], closes[1:]):
        if a > 0 and b > 0:
            r = math.log(b / a)
            if abs(r) < 0.5:
                rets.append(r)
    rets = rets[-lookback:]
    if len(rets) < 5:
        return None

    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    rv = math.sqrt(var) * math.sqrt(252) * 100
    # 与上游同一条 sanity check：正常股票 HV30 不该超过 300%
    return rv if 0 < rv <= 300 else None


if __name__ == "__main__":  # pragma: no cover - 手工核对
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    if not is_configured():
        print("未配置 key。写入 ~/.alpha_hive_twelvedata_key 后重试。")
        sys.exit(1)
    cs = fetch_daily_closes(t)
    print(f"{t}: {len(cs) if cs else 0} 根收盘价，末 3 根 {cs[-3:] if cs else None}")
    print(f"{t}: RV30 = {realized_vol(t)}")
    print("stats:", stats())
