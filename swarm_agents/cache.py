"""yfinance 数据缓存 + 工具函数（_safe_score, ticker 有效性检测等）"""

import time as _time
import threading as _threading
from collections import deque as _deque
from typing import Dict, Optional

import math
from resilience import yfinance_limiter, yfinance_breaker, NETWORK_ERRORS
from swarm_agents._config import _log, _AS


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


#: v0.45.639：下面旧回落被走到的次数 / 阶段 / 原因。走到 = data_pipeline 坏了（`import` 期缺依赖，或取数途中延迟 import
#: 失败——此前两种都被一个 `except ImportError: pass` 吞掉、不留一行日志），而且坏的时候 30 只会**全部**改走这条只用 yfinance
#: 的路（没有未完成 K 线护栏、没有出口回落链、不进 `history_fill_stats`）。读者：`legacy_fallback_stats()` ⇒ scan_timing 的
#: `stock_fetch_legacy` ⇒ `alert_manager._check_stock_fetch_legacy` 非零即 P1。
_legacy_lock = _threading.Lock()
_LEGACY_MAX_ERRORS = 5
_legacy_stats: Dict = {"count": 0, "stages": {}, "tickers": [], "errors": []}
#: 评分用的那份行情取完那一刻的快照（`mark_legacy_fallback_scoring`，与 data_pipeline 的 `mark_history_fill_scoring` 同一时点）；
#: 没标记过 ⇒ None。告警据此区分「评分用的就是降级取数」与「评分之后的取数才走到」——同 v0.45.625 对历史字段回落的处置
_legacy_at_scoring: Optional[Dict] = None


def _record_legacy_fallback(ticker: str, stage: str, exc: BaseException) -> None:
    """登记一次旧回落。stage：``import``（data_pipeline 导入就失败）/ ``call``（导入成功、取数途中抛 ImportError）。
    每种不同的原因只打一条 ERROR（30 只同一个原因不刷屏），次数照数。"""
    msg = f"{stage}: {type(exc).__name__}: {exc}"
    with _legacy_lock:
        _legacy_stats["count"] += 1
        _legacy_stats["stages"][stage] = _legacy_stats["stages"].get(stage, 0) + 1
        if ticker not in _legacy_stats["tickers"]:
            _legacy_stats["tickers"].append(ticker)
        new_msg = msg not in _legacy_stats["errors"]
        if new_msg and len(_legacy_stats["errors"]) < _LEGACY_MAX_ERRORS:
            _legacy_stats["errors"].append(msg)
    if new_msg:
        _log.error("[%s] data_pipeline 不可用（%s）—— 改走 cache.py 旧回落（只用 yfinance，无未完成 K 线护栏 / 出口回落链）；"
                   "本进程第 %d 次", ticker, msg, _legacy_stats["count"])


def legacy_fallback_stats() -> Dict:
    """旧回落计数的副本：``{"count", "stages", "tickers", "errors", "at_scoring"}``。count == 0 = 本进程一次都没走过（正常）。
    `at_scoring` = 评分那一刻的 ``{"count", "tickers"}``（没标记过 ⇒ None）。"""
    with _legacy_lock:
        return {"count": _legacy_stats["count"], "stages": dict(_legacy_stats["stages"]),
                "tickers": list(_legacy_stats["tickers"]), "errors": list(_legacy_stats["errors"]),
                "at_scoring": None if _legacy_at_scoring is None else
                {"count": _legacy_at_scoring["count"], "tickers": list(_legacy_at_scoring["tickers"])}}


def mark_legacy_fallback_scoring() -> None:
    """评分用的那份行情此刻取完（`alpha_hive_daily_report._init_scan_context`，紧挨 `mark_history_fill_scoring`）：拍快照。"""
    global _legacy_at_scoring
    with _legacy_lock:
        _legacy_at_scoring = {"count": _legacy_stats["count"], "tickers": list(_legacy_stats["tickers"])}


def _reset_legacy_fallback_stats() -> None:
    """测试用：清零计数与快照（模块级状态跨测试残留会互相污染；conftest 每条测试前后都清）。"""
    global _legacy_at_scoring
    with _legacy_lock:
        _legacy_stats.update({"count": 0, "stages": {}, "tickers": [], "errors": []})
        _legacy_at_scoring = None


def _fetch_stock_data(ticker: str, target_date: Optional[str] = None) -> Dict:
    """
    多源数据降级链：yfinance → Alpha Vantage → Finnhub → 陈旧缓存 → 安全默认值
    委托给 data_pipeline.MultiSourceFetcher（内置熔断器 + LRU缓存 + 分级TTL）

    改动原因：原实现失败时返回 price=100.0（虚假数据），下游 Agent 会基于假数据评分。
    新实现失败时返回 price=0.0 + _data_unavailable=True，触发 WARN-3 跳过分析。

    v0.41.6: target_date 透传给 data_pipeline，非当日时锚定该日期真实收盘价
    （`--date` 补跑历史交易日场景），None 或等于当日时行为不变。
    """
    # v0.45.639：导入与调用分开接，两种都登记（此前一个 `except ImportError: pass` 包住两者、不留痕）
    try:
        from data_pipeline import fetch_stock_data as _dp_fetch
    except ImportError as e:
        _record_legacy_fallback(ticker, "import", e)
    else:
        try:
            return _dp_fetch(ticker, as_of_date=target_date)
        except ImportError as e:
            _record_legacy_fallback(ticker, "call", e)

    # data_pipeline 不可用时的保守 fallback（只用 yfinance，失败返回 price=0）
    with _yf_lock:
        cached = _yf_cache.get(ticker)
        if cached and (_time.time() - _yf_cache_ts.get(ticker, 0)) < _YF_CACHE_TTL:
            return dict(cached)

    data: Dict = {
        "price": 0.0,          # 修复：原 100.0 是虚假 fallback，改为 0.0 触发 WARN-3
        # v0.45.2: 原为 0.0——与 v0.43.25 在 ScoutBee 侧拆掉的伪造是同一形状，
        # 只是搬到了上游。下面 `len(hist) >= 5` 不成立时（次新股/停牌/取数残缺）
        # 会带着这个 0.0 一路走到 data_source="real"，且**不置** _data_unavailable，
        # 下游完全无法区分"真持平"和"没数据"。data_pipeline 的契约（P0-2, v0.38.0）
        # 本就是 None=诚实缺数据，这里对齐。
        "momentum_5d": None,
        # v0.45.639：0 → None（「0 股」同样是伪造；data_pipeline 的 StockData.avg_volume 缺省就是 None，与之一致）
        "avg_volume": None,
        # v0.45.3: 与 momentum_5d 同一形状的伪造。
        # volume_ratio=1.0 会被下游当"正常量"消费（data_pipeline.py:533 早有此注释）；
        # volatility_20d=0.0 更危险——σ=0 在风险引擎里等于"零风险"，
        # 把"没查到"渲染成"没问题"。两者都改成 None。
        "volume_ratio": None,
        "volatility_20d": None,
        "data_source": "fallback",
    }

    try:
        import price_metrics   # 只依赖 math；data_pipeline 坏了它照样能导入
    except ImportError as e:
        _log.error("[%s] 旧回落也算不了：price_metrics 不可导入（%s: %s）", ticker, type(e).__name__, e)
        data["_data_unavailable"] = True
        return data

    if not yfinance_breaker.allow_request():
        data["_data_unavailable"] = True
        return data

    for attempt in range(_YF_MAX_RETRIES + 1):
        try:
            yfinance_limiter.acquire()
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="1mo")
            # v0.45.645：公司行动空行不是 K 线，第一步去掉（同 data_pipeline 三处，理由见 price_metrics）；末行是空行 ⇒ 当天 K 线缺了，
            # 不拿前一场冒充今天，标不可用（同主路径）
            hist, _tail_event = price_metrics.drop_event_only_rows(hist, ticker)
            if _tail_event:
                _log.warning("[%s] 旧回落：yfinance 日线末行是公司行动空行（当天没有 K 线）—— 标不可用", ticker)
                data["_data_unavailable"] = True
                return data
            if hist.empty:
                if attempt < _YF_MAX_RETRIES:
                    _time.sleep(1.0 * (2 ** attempt))
                    continue
                data["_data_unavailable"] = True
                return data

            if len(hist) >= 1:
                data["price"] = float(hist["Close"].iloc[-1])

            # v0.45.639：公式改用唯一实现 `price_metrics.history_price_metrics`（同 data_pipeline 三处）。此前这里是第四份副本：
            # 均量 NaN / ≤ 0 时把分母换成 1.0 ⇒ avg_volume = 1、volume_ratio = 当天原始成交量（几千万，Buzz 读成「巨量」）——
            # 主路径 v0.45.54 / 473 修过的同一个 bug；且漏了 v0.45.601 的 `fill_method=None`。现在均量算不出 ⇒ 两个都留 None。
            # 不足 2 根不算（同 data_pipeline 主路径：1 根时「均量」就是它自己，量比恒 1.0）。
            if len(hist) >= 2:
                _m = price_metrics.history_price_metrics(hist)
                data["momentum_5d"] = _m["momentum_5d"]
                if _m["avg_volume"] is not None:
                    data["avg_volume"] = _m["avg_volume"]
                    data["volume_ratio"] = _m["volume_ratio"]
                data["volatility_20d"] = _m["volatility_20d"]

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
