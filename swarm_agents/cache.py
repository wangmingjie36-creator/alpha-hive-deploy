"""yfinance 数据缓存 + 工具函数（_safe_score, ticker 有效性检测等）"""

import time as _time
import threading as _threading
from collections import deque as _deque
from typing import Dict, Optional

import math
from resilience import yfinance_limiter, yfinance_breaker, NETWORK_ERRORS
from models import DataQualityChecker as _DQChecker
from swarm_agents._config import _log, _RE_TICKER, _AS


def _safe_score(value, default: float = 5.0, lo: float = 0.0, hi: float = 100.0, label: str = "") -> float:
    """
    验证数值安全性：非 None、非 NaN、在 [lo, hi] 范围内。
    不满足条件时返回 default 并记录 debug 日志。
    """
    if value is None:
        if label:
            _log.debug("_safe_score: %s 为 None，使用默认值 %s", label, default)
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        if label:
            _log.debug("_safe_score: %s=%r 无法转为 float，使用默认值 %s", label, value, default)
        return default
    if math.isnan(f) or math.isinf(f):
        if label:
            _log.debug("_safe_score: %s=%r 为 NaN/Inf，使用默认值 %s", label, f, default)
        return default
    return max(lo, min(hi, f))


# yfinance 数据缓存（同一次扫描内共享，避免重复请求）
_yf_cache: Dict[str, Dict] = {}
_yf_cache_ts: Dict[str, float] = {}
_yf_lock = _threading.Lock()
_YF_CACHE_TTL = _AS.get("yfinance_cache_ttl", 300)
_YF_MAX_RETRIES = 2
_MAX_CACHE_SIZE = 500  # LRU 上限（9标的×15Agent 无驱逐压力）
_yf_insert_order: _deque = _deque()  # 插入顺序追踪（O(1) 淘汰）

# ── Ticker 有效性缓存（退市/拆股检测，#18）──
_ticker_validity: Dict[str, Dict] = {}
_tv_ts: Dict[str, float] = {}  # ticker → checked_at（LRU 淘汰用）
_tv_lock = _threading.Lock()
_TICKER_VALIDITY_TTL = _AS.get("ticker_validity_ttl", 3600)
_tv_insert_order: _deque = _deque()  # ticker validity 插入顺序追踪


def _evict_oldest(cache: Dict, ts_map: Dict, limit: int = _MAX_CACHE_SIZE, *,
                  order: Optional[_deque] = None) -> None:
    """淘汰最旧条目直到 cache 大小 <= limit（需在锁内调用）

    当提供 order deque 时使用 O(1) 淘汰；否则退回 O(n) min() 方案。
    """
    while len(cache) > limit:
        if order:
            # O(1) 淘汰：从 deque 头部弹出最旧 key
            while order:
                oldest_key = order.popleft()
                if oldest_key in cache:
                    del cache[oldest_key]
                    ts_map.pop(oldest_key, None)
                    break
            else:
                break  # deque 空了但 cache 仍超限 → 安全退出
        elif ts_map:
            # O(n) fallback（向后兼容）
            oldest_key = min(ts_map, key=ts_map.get)  # type: ignore[arg-type]
            del cache[oldest_key]
            del ts_map[oldest_key]
        else:
            break


def check_ticker_validity(ticker: str) -> Dict:
    """
    检测 ticker 是否存在退市/停牌/拆股风险

    返回:
        {
            "valid": bool,          # False = 退市/停牌，应跳过扫描
            "warning": str | None,  # 告警信息（分割/低价等）
            "split_ratio": float | None,  # 近期拆股比例
        }
    """
    now = _time.time()
    with _tv_lock:
        cached = _ticker_validity.get(ticker)
        if cached and (now - cached.get("_checked_at", 0)) < _TICKER_VALIDITY_TTL:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    result: Dict = {"valid": True, "warning": None, "split_ratio": None}

    try:
        import yfinance as _yf
        t = _yf.Ticker(ticker)
        hist = t.history(period="5d")

        # 无交易数据 → 退市/停牌
        if hist.empty:
            result["valid"] = False
            result["warning"] = f"{ticker} 无交易数据（可能已退市或停牌），已跳过扫描"
            _log.warning("⚠️ %s", result["warning"])
            with _tv_lock:
                if ticker not in _ticker_validity:
                    _tv_insert_order.append(ticker)
                _ticker_validity[ticker] = {**result, "_checked_at": now}
                _tv_ts[ticker] = now
                _evict_oldest(_ticker_validity, _tv_ts, order=_tv_insert_order)
            return result

        # 价格极低 → 退市风险
        price = float(hist["Close"].iloc[-1])
        if price < 0.10:
            result["warning"] = f"{ticker} 价格极低 (${price:.4f})，存在退市风险"
            _log.warning("⚠️ %s", result["warning"])

        # 近30天拆股检测
        try:
            splits = t.splits
            if len(splits) > 0:
                cutoff_ts = now - 30 * 86400
                recent = [
                    (str(idx)[:10], float(ratio))
                    for idx, ratio in splits.items()
                    if hasattr(idx, "timestamp") and idx.timestamp() > cutoff_ts
                ]
                if recent:
                    date_str, ratio = recent[-1]
                    result["split_ratio"] = ratio
                    msg = f"{ticker} 近30天股票分割 ({ratio:.2f}x on {date_str})"
                    # 不覆盖退市告警
                    if not result["warning"]:
                        result["warning"] = msg
                    _log.warning("⚠️ %s", msg)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            pass

    except NETWORK_ERRORS as e:
        _log.debug("ticker validity check failed for %s: %s", ticker, e)

    with _tv_lock:
        if ticker not in _ticker_validity:
            _tv_insert_order.append(ticker)
        _ticker_validity[ticker] = {**result, "_checked_at": now}
        _tv_ts[ticker] = now
        _evict_oldest(_ticker_validity, _tv_ts, order=_tv_insert_order)
    return result


def _fetch_stock_data(ticker: str) -> Dict:
    """
    多源数据降级链：yfinance → Alpha Vantage → Finnhub → 陈旧缓存 → 安全默认值
    委托给 data_pipeline.MultiSourceFetcher（内置熔断器 + LRU缓存 + 分级TTL）

    改动原因：原实现失败时返回 price=100.0（虚假数据），下游 Agent 会基于假数据评分。
    新实现失败时返回 price=0.0 + _data_unavailable=True，触发 WARN-3 跳过分析。
    """
    try:
        from data_pipeline import fetch_stock_data as _dp_fetch
        return _dp_fetch(ticker)
    except ImportError:
        pass

    # data_pipeline 不可用时的保守 fallback（只用 yfinance，失败返回 price=0）
    with _yf_lock:
        cached = _yf_cache.get(ticker)
        if cached and (_time.time() - _yf_cache_ts.get(ticker, 0)) < _YF_CACHE_TTL:
            return dict(cached)

    data: Dict = {
        "price": 0.0,          # 修复：原 100.0 是虚假 fallback，改为 0.0 触发 WARN-3
        "momentum_5d": 0.0,
        "avg_volume": 0,
        "volume_ratio": 1.0,
        "volatility_20d": 0.0,
        "data_source": "fallback",
    }

    if not yfinance_breaker.allow_request():
        data["_data_unavailable"] = True
        return data

    for attempt in range(_YF_MAX_RETRIES + 1):
        try:
            yfinance_limiter.acquire()
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="1mo")
            if hist.empty:
                if attempt < _YF_MAX_RETRIES:
                    _time.sleep(1.0 * (2 ** attempt))
                    continue
                data["_data_unavailable"] = True
                return data

            if len(hist) >= 1:
                data["price"] = float(hist["Close"].iloc[-1])

            if len(hist) >= 5:
                data["momentum_5d"] = (hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100

            if len(hist) >= 2:
                recent_vol = float(hist["Volume"].iloc[-1])
                avg_vol = float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20 else float(hist["Volume"].mean())
                if math.isnan(avg_vol) or avg_vol <= 0:
                    avg_vol = 1.0
                data["avg_volume"] = int(avg_vol)
                data["volume_ratio"] = recent_vol / avg_vol if avg_vol > 0 else 1.0

            if len(hist) >= 20:
                returns = hist["Close"].pct_change().dropna()
                if len(returns) >= 2:
                    _vol = float(returns.std() * (252 ** 0.5) * 100)
                    if not (math.isnan(_vol) or math.isinf(_vol)):
                        data["volatility_20d"] = _vol

            data["data_source"] = "real"
            # 写入缓存（O(1) LRU 淘汰）
            with _yf_lock:
                if ticker not in _yf_cache:
                    _yf_insert_order.append(ticker)
                _yf_cache[ticker] = data
                _yf_cache_ts[ticker] = _time.time()
                _evict_oldest(_yf_cache, _yf_cache_ts, order=_yf_insert_order)
            yfinance_breaker.record_success()
            break

        except NETWORK_ERRORS as e:
            _log.warning("yfinance fetch %s attempt %d failed: %s", ticker, attempt, e)
            if attempt < _YF_MAX_RETRIES:
                _time.sleep(1.0 * (2 ** attempt))
            else:
                yfinance_breaker.record_failure()

    return data


def get_cached_stock_data(ticker: str) -> Optional[Dict]:
    """公开访问器：读取 yfinance 缓存。缓存未命中或过期返回 None。"""
    with _yf_lock:
        cached = _yf_cache.get(ticker)
        if cached and (_time.time() - _yf_cache_ts.get(ticker, 0)) < _YF_CACHE_TTL:
            return dict(cached)
    return None
