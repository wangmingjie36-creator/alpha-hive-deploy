"""
Alpha Hive 升级 — Phase 1.1: 多源数据降级链
============================================
替换 swarm_agents/cache.py 中的 _fetch_stock_data()

问题：当前 yfinance 失败时返回 price=100.0 的硬编码默认值，
      所有下游 Agent 会基于虚假数据生成评分，且无告警标记。

解决方案：
  1. 多源降级链：yfinance → Alpha Vantage → Finnhub → 静态缓存
  2. 每条数据都带 data_source 标记（real / degraded / stale / fallback）
  3. 熔断器状态可观测（日志 + 指标暴露）
  4. 智能缓存：LRU + TTL + 分级 TTL（real=5min, degraded=2min）

用法：
  将本文件放入项目根目录，然后在 swarm_agents/cache.py 中：
  from data_pipeline import MultiSourceFetcher
  _fetcher = MultiSourceFetcher()
  
  替换 _fetch_stock_data:
  def _fetch_stock_data(ticker: str) -> Dict:
      return _fetcher.fetch(ticker)
"""

import os
import time
import math
import logging
import threading
from typing import Dict, Optional, List, Tuple
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

_log = logging.getLogger("alpha_hive.data_pipeline")


# ==================== 数据质量标记 ====================

class DataQuality:
    """数据质量等级常量"""
    REAL = "real"              # API 调用成功，数据新鲜
    DEGRADED = "degraded"      # 降级源（备用 API），数据可能有延迟
    STALE = "stale"            # 缓存数据，超过 TTL 但 API 全部失败
    FALLBACK = "fallback"      # 所有源不可用，返回安全默认值
    ERROR = "error"            # 数据获取失败


@dataclass
class StockData:
    """标准化股票数据结构（所有数据源统一输出格式）"""
    price: float = 0.0
    momentum_5d: float = 0.0
    avg_volume: int = 0
    volume_ratio: float = 1.0
    volatility_20d: float = 0.0
    # 元数据
    data_source: str = DataQuality.FALLBACK
    source_name: str = "none"
    fetch_timestamp: float = 0.0
    is_market_hours: bool = False

    def to_dict(self) -> Dict:
        return {
            "price": self.price,
            "momentum_5d": self.momentum_5d,
            "avg_volume": self.avg_volume,
            "volume_ratio": self.volume_ratio,
            "volatility_20d": self.volatility_20d,
            "data_source": self.data_source,
            "source_name": self.source_name,
            "fetch_timestamp": self.fetch_timestamp,
            "is_market_hours": self.is_market_hours,
        }


# ==================== 熔断器（可观测版）====================

class ObservableCircuitBreaker:
    """带指标暴露的熔断器"""

    def __init__(self, name: str, failure_threshold: int = 3,
                 recovery_timeout: float = 60.0, half_open_max: int = 1):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max

        self._failures = 0
        self._successes = 0
        self._total_calls = 0
        self._state = "closed"  # closed / open / half_open
        self._last_failure_time = 0.0
        self._half_open_calls = 0
        self._lock = threading.Lock()

        # 指标
        self._consecutive_failures = 0
        self._last_error: str = ""
        self._trip_count = 0

    def allow_request(self) -> bool:
        with self._lock:
            now = time.time()
            if self._state == "closed":
                return True
            elif self._state == "open":
                if now - self._last_failure_time >= self.recovery_timeout:
                    self._state = "half_open"
                    self._half_open_calls = 0
                    _log.info("[CB-%s] open → half_open (recovery timeout)", self.name)
                    return True
                return False
            else:  # half_open
                if self._half_open_calls < self.half_open_max:
                    self._half_open_calls += 1
                    return True
                return False

    def record_success(self):
        with self._lock:
            self._successes += 1
            self._total_calls += 1
            self._consecutive_failures = 0
            if self._state == "half_open":
                self._state = "closed"
                self._failures = 0
                _log.info("[CB-%s] half_open → closed (success)", self.name)

    def record_failure(self, error: str = ""):
        with self._lock:
            self._failures += 1
            self._total_calls += 1
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            self._last_error = str(error)[:200]

            if self._state == "half_open":
                self._state = "open"
                self._trip_count += 1
                _log.warning("[CB-%s] half_open → open (failure: %s)", self.name, error)
            elif self._failures >= self.failure_threshold:
                self._state = "open"
                self._trip_count += 1
                _log.warning("[CB-%s] closed → open (threshold %d, error: %s)",
                             self.name, self.failure_threshold, error)

    def get_metrics(self) -> Dict:
        """暴露熔断器指标（供监控/日志使用）"""
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "successes": self._successes,
                "total_calls": self._total_calls,
                "consecutive_failures": self._consecutive_failures,
                "trip_count": self._trip_count,
                "last_error": self._last_error,
                "success_rate": (
                    round(self._successes / self._total_calls * 100, 1)
                    if self._total_calls > 0 else 0.0
                ),
            }


# ==================== 数据源适配器 ====================

class YFinanceSource:
    """主数据源：yfinance（免费，实时性好）"""

    def __init__(self):
        self.breaker = ObservableCircuitBreaker("yfinance", failure_threshold=3, recovery_timeout=60)
        self.name = "yfinance"

    def fetch(self, ticker: str) -> Optional[StockData]:
        if not self.breaker.allow_request():
            return None
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="1mo")
            if hist.empty or len(hist) < 2:
                self.breaker.record_failure("empty_history")
                return None

            data = StockData(
                data_source=DataQuality.REAL,
                source_name=self.name,
                fetch_timestamp=time.time(),
            )
            data.price = float(hist["Close"].iloc[-1])

            if len(hist) >= 5:
                data.momentum_5d = (hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100

            recent_vol = float(hist["Volume"].iloc[-1])
            avg_vol = float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20 else float(hist["Volume"].mean())
            if math.isnan(avg_vol) or avg_vol <= 0:
                avg_vol = 1.0
            data.avg_volume = int(avg_vol)
            data.volume_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

            if len(hist) >= 20:
                returns = hist["Close"].pct_change().dropna()
                if len(returns) >= 2:
                    vol = float(returns.std() * (252 ** 0.5) * 100)
                    if not (math.isnan(vol) or math.isinf(vol)):
                        data.volatility_20d = vol

            self.breaker.record_success()
            return data

        except Exception as e:
            self.breaker.record_failure(str(e))
            _log.warning("[yfinance] fetch %s failed: %s", ticker, e)
            return None


class AlphaVantageSource:
    """降级源 1：Alpha Vantage（免费 25次/天，需 API Key）"""

    def __init__(self):
        self.api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        self.breaker = ObservableCircuitBreaker("alpha_vantage", failure_threshold=5, recovery_timeout=120)
        self.name = "alpha_vantage"

    def fetch(self, ticker: str) -> Optional[StockData]:
        if not self.api_key or not self.breaker.allow_request():
            return None
        try:
            import urllib.request
            import json

            url = (
                f"https://www.alphavantage.co/query?"
                f"function=GLOBAL_QUOTE&symbol={ticker}&apikey={self.api_key}"
            )
            with urllib.request.urlopen(url, timeout=10) as resp:
                result = json.loads(resp.read().decode())

            quote = result.get("Global Quote", {})
            price = float(quote.get("05. price", 0))
            if price <= 0:
                self.breaker.record_failure("zero_price")
                return None

            change_pct = float(quote.get("10. change percent", "0").rstrip("%"))
            volume = int(quote.get("06. volume", 0))

            data = StockData(
                price=price,
                momentum_5d=change_pct,  # 近似值：日涨跌幅代替5日动量
                avg_volume=volume,
                volume_ratio=1.0,  # AV 免费版无法计算量比
                data_source=DataQuality.DEGRADED,
                source_name=self.name,
                fetch_timestamp=time.time(),
            )
            self.breaker.record_success()
            return data

        except Exception as e:
            self.breaker.record_failure(str(e))
            _log.warning("[AlphaVantage] fetch %s failed: %s", ticker, e)
            return None


class FinnhubSource:
    """降级源 2：Finnhub（免费 60次/分钟，需 API Key）"""

    def __init__(self):
        self.api_key = os.environ.get("FINNHUB_API_KEY", "")
        self.breaker = ObservableCircuitBreaker("finnhub", failure_threshold=5, recovery_timeout=90)
        self.name = "finnhub"

    def fetch(self, ticker: str) -> Optional[StockData]:
        if not self.api_key or not self.breaker.allow_request():
            return None
        try:
            import urllib.request
            import json

            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={self.api_key}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                quote = json.loads(resp.read().decode())

            current = float(quote.get("c", 0))
            prev_close = float(quote.get("pc", 0))

            if current <= 0:
                self.breaker.record_failure("zero_price")
                return None

            change_pct = ((current / prev_close) - 1) * 100 if prev_close > 0 else 0.0

            data = StockData(
                price=current,
                momentum_5d=change_pct,  # 近似值
                data_source=DataQuality.DEGRADED,
                source_name=self.name,
                fetch_timestamp=time.time(),
            )
            self.breaker.record_success()
            return data

        except Exception as e:
            self.breaker.record_failure(str(e))
            _log.warning("[Finnhub] fetch %s failed: %s", ticker, e)
            return None


# ==================== 多源聚合器 ====================

class MultiSourceFetcher:
    """
    多源数据降级链：yfinance → Alpha Vantage → Finnhub → 陈旧缓存 → 安全默认值

    核心原则：
    1. 永远不返回 price=100 的虚假数据
    2. 每条数据都带质量标记（real/degraded/stale/fallback）
    3. 降级时自动告警
    4. 所有熔断器指标可查询
    """

    # 缓存配置
    CACHE_TTL_REAL = 300       # 5分钟（real 数据）
    CACHE_TTL_DEGRADED = 120   # 2分钟（降级数据，更快刷新）
    CACHE_TTL_STALE = 3600     # 1小时（过期数据作为最后防线）
    MAX_CACHE_SIZE = 500

    def __init__(self):
        self._sources: List = [
            YFinanceSource(),
            AlphaVantageSource(),
            FinnhubSource(),
        ]
        self._cache: Dict[str, StockData] = {}
        self._cache_ts: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._insert_order: deque = deque()

        # 统计
        self._fetch_stats = {
            "total": 0, "real": 0, "degraded": 0,
            "stale": 0, "fallback": 0,
        }

    def fetch(self, ticker: str) -> Dict:
        """
        获取股票数据（自动降级链 + 缓存）
        返回字典格式，兼容现有 _fetch_stock_data 接口
        """
        with self._lock:
            self._fetch_stats["total"] += 1

        # 1. 检查新鲜缓存
        cached = self._get_cache(ticker)
        if cached and cached.data_source in (DataQuality.REAL, DataQuality.DEGRADED):
            ttl = (self.CACHE_TTL_REAL if cached.data_source == DataQuality.REAL
                   else self.CACHE_TTL_DEGRADED)
            if time.time() - cached.fetch_timestamp < ttl:
                return cached.to_dict()

        # 2. 尝试降级链
        for source in self._sources:
            data = source.fetch(ticker)
            if data and data.price > 0:
                self._set_cache(ticker, data)
                with self._lock:
                    self._fetch_stats[data.data_source] += 1
                if data.data_source != DataQuality.REAL:
                    _log.warning(
                        "[DataPipeline] %s 降级到 %s（源：%s）",
                        ticker, data.data_source, data.source_name
                    )
                return data.to_dict()

        # 3. 使用过期缓存（stale）— cached 已是副本，修改不影响缓存
        if cached and cached.price > 0:
            stale_age = time.time() - cached.fetch_timestamp
            if stale_age < self.CACHE_TTL_STALE:
                cached.data_source = DataQuality.STALE  # 安全：修改的是副本
                with self._lock:
                    self._fetch_stats["stale"] += 1
                _log.warning(
                    "[DataPipeline] %s 使用过期缓存（%.0f秒前，源：%s）",
                    ticker, stale_age, cached.source_name
                )
                return cached.to_dict()

        # 4. 最后防线：安全默认值（明确标记为 fallback）
        with self._lock:
            self._fetch_stats["fallback"] += 1
        _log.error(
            "[DataPipeline] %s 所有数据源不可用！返回 FALLBACK 默认值（不可用于交易决策）",
            ticker
        )
        fallback = StockData(
            price=0.0,
            data_source=DataQuality.FALLBACK,
            source_name="none",
            fetch_timestamp=time.time(),
        ).to_dict()
        # 标记数据不可用，下游 Agent 应跳过分析
        fallback["_data_unavailable"] = True
        return fallback

    def _get_cache(self, ticker: str) -> Optional[StockData]:
        with self._lock:
            cached = self._cache.get(ticker)
            if cached is None:
                return None
            # 返回副本，防止 stale 路径修改原缓存对象
            import copy
            return copy.copy(cached)

    def _set_cache(self, ticker: str, data: StockData):
        with self._lock:
            if ticker not in self._cache:
                self._insert_order.append(ticker)
            self._cache[ticker] = data
            self._cache_ts[ticker] = time.time()
            # LRU 淘汰
            while len(self._cache) > self.MAX_CACHE_SIZE and self._insert_order:
                oldest = self._insert_order.popleft()
                self._cache.pop(oldest, None)
                self._cache_ts.pop(oldest, None)

    def get_all_metrics(self) -> Dict:
        """获取所有数据源的熔断器指标 + 缓存统计"""
        metrics = {
            "fetch_stats": dict(self._fetch_stats),
            "cache_size": len(self._cache),
            "sources": {},
        }
        for source in self._sources:
            metrics["sources"][source.name] = source.breaker.get_metrics()
        return metrics

    def get_health_summary(self) -> str:
        """一行健康摘要（供日志/Slack 告警使用）"""
        m = self.get_all_metrics()
        parts = []
        for name, sm in m["sources"].items():
            state = sm["state"]
            rate = sm["success_rate"]
            emoji = "🟢" if state == "closed" else ("🟡" if state == "half_open" else "🔴")
            parts.append(f"{emoji}{name}({state},{rate}%)")
        fs = m["fetch_stats"]
        total = fs["total"] or 1
        real_pct = round(fs["real"] / total * 100, 1)
        return f"数据管道: {' | '.join(parts)} | 真实数据率:{real_pct}% | 缓存:{m['cache_size']}"


# ==================== 集成接口 ====================

# 全局单例
_fetcher: Optional[MultiSourceFetcher] = None
_fetcher_lock = threading.Lock()


def get_fetcher() -> MultiSourceFetcher:
    """获取全局 MultiSourceFetcher 单例"""
    global _fetcher
    if _fetcher is None:
        with _fetcher_lock:
            if _fetcher is None:
                _fetcher = MultiSourceFetcher()
    return _fetcher


def fetch_stock_data(ticker: str) -> Dict:
    """
    直接替换 swarm_agents/cache.py 中的 _fetch_stock_data()
    
    用法（在 swarm_agents/cache.py 中）：
        from data_pipeline import fetch_stock_data as _fetch_stock_data
    """
    return get_fetcher().fetch(ticker)
