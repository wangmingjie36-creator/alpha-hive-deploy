"""
Polymarket 预测市场数据客户端

通过 Gamma API 获取预测市场赔率（无需 API Key）：
- 覆盖 Fed 利率决议、财报预测、股价目标等事件
- 返回隐含概率（$0.01-$1.00 = 1%-100%）
- 24h 成交量、流动性、买卖价差

限速：~300 req/10s（远超需求）
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None

from hive_logger import PATHS, get_logger, atomic_json_write
from resilience import polymarket_limiter, polymarket_breaker

_log = get_logger("polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CACHE_DIR = PATHS.home / "polymarket_cache"
CACHE_DIR.mkdir(exist_ok=True)

# 股票/经济事件相关关键词
STOCK_KEYWORDS = [
    "nvda", "nvidia", "tsla", "tesla", "msft", "microsoft",
    "aapl", "apple", "amzn", "amazon", "meta", "goog", "google",
    "amd", "intc", "intel", "pltr", "coin", "spy", "qqq",
]
ECON_KEYWORDS = [
    "fed", "rate", "inflation", "cpi", "gdp", "jobs", "unemployment",
    "recession", "tariff", "earnings", "ipo",
]


class PolymarketClient:
    """Polymarket 预测市场客户端"""

    def __init__(self):
        self._market_cache: Dict[str, Dict] = {}  # slug -> {data, ts}

    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """发送 GET 请求（带限流 + 熔断保护）"""
        if requests is None:
            return None
        if not polymarket_breaker.allow_request():
            _log.warning("Polymarket 熔断器已打开，跳过请求")
            return None
        try:
            polymarket_limiter.acquire()
            resp = requests.get(
                f"{GAMMA_BASE}{endpoint}",
                params=params,
                timeout=15,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            polymarket_breaker.record_success()
            return resp.json()
        except (ConnectionError, TimeoutError, OSError, ValueError) as e:
            polymarket_breaker.record_failure()
            _log.warning("Polymarket API 请求失败: %s", e)
            return None

    def search_markets(self, query: str, limit: int = 20) -> List[Dict]:
        """
        搜索相关预测市场

        query: 搜索词（如 "NVDA", "Fed rate"）
        返回: [{slug, question, outcomes, prices, volume_24h, ...}]
        """
        # 磁盘缓存 15 分钟
        cache_key = f"search_{query.lower().replace(' ', '_')}"
        cache_path = CACHE_DIR / f"{cache_key}.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 900:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    _log.debug("search cache read failed: %s", e)

        data = self._get("/markets", params={
            "limit": limit,
            "active": True,
            "closed": False,
            "order": "volume24hr",
            "ascending": False,
        })

        if not data or not isinstance(data, list):
            return []

        # 客户端侧过滤（Gamma API 搜索功能有限）
        query_lower = query.lower()
        filtered = []
        for m in data:
            q = (m.get("question", "") + " " + m.get("slug", "")).lower()
            if query_lower in q:
                filtered.append(self._normalize_market(m))

        # 如果精确匹配太少，放宽搜索
        if len(filtered) < 3:
            # 尝试用 slug 搜索
            for m in data:
                nm = self._normalize_market(m)
                if nm not in filtered:
                    # 检查关键词部分匹配
                    q = (m.get("question", "") + " " + m.get("slug", "")).lower()
                    for word in query_lower.split():
                        if len(word) >= 3 and word in q:
                            filtered.append(nm)
                            break

        # 保存缓存
        try:
            atomic_json_write(cache_path, filtered[:limit])
        except (OSError, TypeError) as e:
            _log.debug("search cache write failed: %s", e)

        return filtered[:limit]

    def get_ticker_odds(self, ticker: str) -> Dict:
        """
        获取与指定标的相关的预测市场赔率汇总

        返回: {
            ticker, markets_found, top_markets: [...],
            implied_bullish, implied_bearish,
            total_volume_24h, avg_liquidity,
            odds_score: 0-10,
            odds_signal: str
        }
        """
        # 缓存 15 分钟
        cache_path = CACHE_DIR / f"{ticker.upper()}_odds.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 900:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    _log.debug("odds cache read failed: %s", e)

        ticker_upper = ticker.upper()
        ticker_lower = ticker.lower()

        # 搜索相关市场
        markets = self.search_markets(ticker_lower, limit=30)

        # 如果搜索不到具体标的，搜索经济相关事件
        if len(markets) < 2:
            econ_markets = self.search_markets("fed rate", limit=10)
            markets.extend(econ_markets)

        if not markets:
            return self._default_result(ticker)

        # 分析市场数据
        bullish_signals = []
        bearish_signals = []
        total_volume = 0.0
        liquidities = []
        top_markets = []

        for m in markets[:10]:
            question = m.get("question", "").lower()
            prices = m.get("outcome_prices", [])
            volume = m.get("volume_24h", 0)
            liquidity = m.get("liquidity", 0)

            total_volume += volume
            if liquidity > 0:
                liquidities.append(liquidity)

            # 解析赔率方向
            if len(prices) >= 2:
                yes_price = prices[0]
                no_price = prices[1]

                # 判断 YES 代表什么方向
                is_bullish_market = any(
                    w in question for w in
                    ["above", "higher", "beat", "exceed", "rise", "up", "bull", "hit"]
                )
                is_bearish_market = any(
                    w in question for w in
                    ["below", "lower", "miss", "fall", "drop", "down", "crash", "bear"]
                )

                if is_bullish_market:
                    bullish_signals.append(yes_price)
                    bearish_signals.append(no_price)
                elif is_bearish_market:
                    bearish_signals.append(yes_price)
                    bullish_signals.append(no_price)
                else:
                    # 中性市场，取 YES 概率作为事件发生概率
                    if yes_price > 0.5:
                        bullish_signals.append(yes_price)
                    else:
                        bearish_signals.append(1 - yes_price)

            top_markets.append({
                "question": m.get("question", "")[:80],
                "prices": prices,
                "volume_24h": round(volume, 0),
            })

        # 计算综合隐含概率
        avg_bullish = sum(bullish_signals) / len(bullish_signals) if bullish_signals else 0.5
        avg_bearish = sum(bearish_signals) / len(bearish_signals) if bearish_signals else 0.5
        avg_liquidity = sum(liquidities) / len(liquidities) if liquidities else 0.0

        # 计算 odds_score (0-10)
        score = 5.0
        # 强看多信号（平均隐含概率 > 65%）
        if avg_bullish > 0.65:
            score += min(2.5, (avg_bullish - 0.5) * 10)
        # 强看空信号
        elif avg_bearish > 0.65:
            score -= min(2.0, (avg_bearish - 0.5) * 8)
        # 高流动性加分
        if total_volume > 100000:
            score += 0.5
        elif total_volume > 50000:
            score += 0.3
        # 市场数量多 = 更多关注
        if len(markets) >= 5:
            score += 0.5

        score = max(1.0, min(10.0, score))

        # 信号描述
        if score >= 7.0:
            signal = f"预测市场看多（隐含概率 {avg_bullish:.0%}）"
        elif score <= 3.5:
            signal = f"预测市场看空（隐含概率 {avg_bearish:.0%}）"
        elif len(markets) == 0:
            signal = "无相关预测市场"
        else:
            signal = f"预测市场中性（{len(markets)} 个市场）"

        result = {
            "ticker": ticker_upper,
            "markets_found": len(markets),
            "top_markets": top_markets[:5],
            "implied_bullish": round(avg_bullish, 3),
            "implied_bearish": round(avg_bearish, 3),
            "total_volume_24h": round(total_volume, 0),
            "avg_liquidity": round(avg_liquidity, 0),
            "odds_score": round(score, 1),
            "odds_signal": signal,
            "timestamp": datetime.now().isoformat(),
        }

        # 保存缓存
        try:
            atomic_json_write(cache_path, result)
        except (OSError, TypeError) as e:
            _log.debug("odds cache write failed: %s", e)

        return result

    def get_macro_events(self) -> List[Dict]:
        """
        获取宏观经济相关预测市场（Fed、CPI、GDP 等）

        返回: [{question, prices, volume_24h, category}]
        """
        cache_path = CACHE_DIR / "macro_events.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 1800:  # 30 分钟缓存
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    _log.debug("macro cache read failed: %s", e)

        events = []
        for keyword in ["fed rate", "inflation", "recession", "gdp"]:
            markets = self.search_markets(keyword, limit=5)
            for m in markets:
                m["category"] = keyword
                events.append(m)

        # 去重
        seen = set()
        unique = []
        for e in events:
            key = e.get("question", "")
            if key not in seen:
                seen.add(key)
                unique.append(e)

        try:
            atomic_json_write(cache_path, unique)
        except (OSError, TypeError) as e:
            _log.debug("macro cache write failed: %s", e)

        return unique

    def _normalize_market(self, raw: Dict) -> Dict:
        """标准化市场数据格式"""
        prices = raw.get("outcomePrices", "")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (json.JSONDecodeError, ValueError):
                prices = []
        if isinstance(prices, list):
            prices = [float(p) if p else 0.0 for p in prices]

        return {
            "slug": raw.get("slug", ""),
            "question": raw.get("question", ""),
            "outcomes": raw.get("outcomes", []),
            "outcome_prices": prices,
            "volume_24h": float(raw.get("volume24hr", 0) or 0),
            "liquidity": float(raw.get("liquidity", 0) or 0),
            "end_date": raw.get("endDate", ""),
        }

    def _default_result(self, ticker: str) -> Dict:
        """无数据时的默认结果"""
        return {
            "ticker": ticker.upper(),
            "markets_found": 0,
            "top_markets": [],
            "implied_bullish": 0.5,
            "implied_bearish": 0.5,
            "total_volume_24h": 0,
            "avg_liquidity": 0,
            "odds_score": 5.0,
            "odds_signal": "无相关预测市场",
            "timestamp": datetime.now().isoformat(),
        }


# ==================== 便捷函数 ====================

_client: Optional[PolymarketClient] = None
_client_lock = threading.Lock()


def get_polymarket_odds(ticker: str) -> Dict:
    """便捷函数：获取 Polymarket 赔率数据"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = PolymarketClient()
    return _client.get_ticker_odds(ticker)
