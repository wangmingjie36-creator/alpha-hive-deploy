"""
Reddit 社交情绪数据采集模块（通过 ApeWisdom API）

使用 ApeWisdom 免费 API（无需 API Key）：
- 覆盖 WallStreetBets, r/stocks, r/investing, r/options 等主流子版
- 提供每个 ticker 的提及次数、24h 变化量、upvote 数
- 通过提及量变化推算市场关注度和情绪方向

限制：无文档限速，建议不超过 10 req/min
"""

import json
import logging as _logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from hive_logger import atomic_json_write

_log = _logging.getLogger("alpha_hive.reddit_sentiment")

try:
    import requests
except ImportError:
    requests = None

# 缓存目录
CACHE_DIR = Path(__file__).parent / "reddit_cache"
CACHE_DIR.mkdir(exist_ok=True)

# ApeWisdom API
APEWISDOM_BASE = "https://apewisdom.io/api/v1.0"
try:
    from config import CACHE_CONFIG as _CC
    _REDDIT_MEM_TTL = _CC["ttl"].get("reddit_memory", 300)
    _REDDIT_DISK_TTL = _CC["ttl"].get("reddit", 600)
except (ImportError, KeyError):
    _REDDIT_MEM_TTL = 300
    _REDDIT_DISK_TTL = 600


class RedditSentimentClient:
    """Reddit 社交情绪客户端（基于 ApeWisdom API）"""

    def __init__(self):
        self._ranking_cache: Dict[str, Dict] = {}  # filter -> {data, timestamp}
        self._last_request = 0.0

    def _throttle(self):
        """限流：最快 6 秒一次"""
        now = time.time()
        elapsed = now - self._last_request
        if elapsed < 6.0:
            time.sleep(6.0 - elapsed)
        self._last_request = time.time()

    def _fetch_ranking(self, filter_name: str = "all-stocks") -> List[Dict]:
        """
        获取 Reddit 股票提及排名（前 100 名）

        filter_name: all-stocks | wallstreetbets | stocks | investing | options
        """
        # 内存缓存
        cached = self._ranking_cache.get(filter_name)
        if cached and (time.time() - cached["timestamp"]) < _REDDIT_MEM_TTL:
            return cached["data"]

        # 磁盘缓存
        cache_path = CACHE_DIR / f"ranking_{filter_name}.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < _REDDIT_DISK_TTL:
                try:
                    with open(cache_path) as f:
                        data = json.load(f)
                    self._ranking_cache[filter_name] = {
                        "data": data, "timestamp": time.time()
                    }
                    return data
                except (json.JSONDecodeError, OSError) as exc:
                    _log.debug("Reddit ranking 缓存读取失败 (%s): %s", filter_name, exc)

        if requests is None:
            return []

        try:
            from resilience import reddit_breaker
            if not reddit_breaker.allow_request():
                _log.debug("Reddit circuit breaker OPEN, returning cached")
                return []
        except ImportError:
            reddit_breaker = None

        try:
            self._throttle()
            resp = requests.get(
                f"{APEWISDOM_BASE}/filter/{filter_name}/page/1",
                timeout=15,
            )
            resp.raise_for_status()
            if reddit_breaker:
                reddit_breaker.record_success()
            results = resp.json().get("results", [])

            # 保存缓存
            self._ranking_cache[filter_name] = {
                "data": results, "timestamp": time.time()
            }
            try:
                atomic_json_write(cache_path, results)
            except (OSError, TypeError) as exc:
                _log.debug("Reddit ranking 缓存写入失败 (%s): %s", filter_name, exc)

            return results

        except (ConnectionError, TimeoutError, OSError, ValueError) as e:
            if reddit_breaker:
                reddit_breaker.record_failure()
            _log.warning("获取 Reddit 排名失败 (%s): %s", filter_name, e)
            return []

    def get_ticker_sentiment(self, ticker: str) -> Dict:
        """
        获取指定标的的 Reddit 情绪数据

        返回: {
            ticker, rank, mentions, mentions_24h_ago, mention_delta,
            upvotes, momentum_pct,
            reddit_buzz: "hot" | "rising" | "cooling" | "quiet",
            sentiment_score: 0-10,
            sources: [str]
        }
        """
        # 缓存 10 分钟
        cache_path = CACHE_DIR / f"{ticker}_sentiment.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 600:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as exc:
                    _log.debug("Reddit sentiment 缓存读取失败 (%s): %s", ticker, exc)

        ticker_upper = ticker.upper()

        # 从多个子版获取数据
        all_stocks = self._fetch_ranking("all-stocks")
        wsb = self._fetch_ranking("wallstreetbets")

        # 在排名中查找目标 ticker
        all_match = self._find_ticker(all_stocks, ticker_upper)
        wsb_match = self._find_ticker(wsb, ticker_upper)

        # 合并数据（优先 all-stocks，补充 WSB）
        if all_match:
            primary = all_match
        elif wsb_match:
            primary = wsb_match
        else:
            return self._quiet_result(ticker)

        mentions = primary.get("mentions", 0)
        mentions_24h = primary.get("mentions_24h_ago", 0)
        upvotes = primary.get("upvotes", 0)
        rank = primary.get("rank", 999)

        # 计算提及量变化
        mention_delta = mentions - mentions_24h
        if mentions_24h > 0:
            momentum_pct = (mention_delta / mentions_24h) * 100
        else:
            momentum_pct = 100.0 if mentions > 0 else 0.0

        # 判断热度等级
        if rank <= 5 and momentum_pct > 30:
            buzz = "hot"
        elif momentum_pct > 20 or rank <= 15:
            buzz = "rising"
        elif momentum_pct < -20:
            buzz = "cooling"
        else:
            buzz = "quiet"

        # 评分逻辑：
        # - 排名靠前 → 关注度高（可能拥挤）
        # - 提及量上升 → 动量增加
        # - upvote/mention 比率 → 内容质量
        score = 5.0  # 基准

        # 排名加分（前 10 加分，但不过度）
        if rank <= 3:
            score += 1.5
        elif rank <= 10:
            score += 1.0
        elif rank <= 25:
            score += 0.5

        # 动量加分
        if momentum_pct > 50:
            score += 1.5  # 提及量暴增
        elif momentum_pct > 20:
            score += 1.0
        elif momentum_pct > 0:
            score += 0.5
        elif momentum_pct < -30:
            score -= 1.0  # 降温

        # upvote 质量
        if mentions > 0:
            quality = upvotes / mentions
            if quality > 5:
                score += 0.5  # 高质量讨论
            elif quality < 1:
                score -= 0.5  # 低质量刷屏

        # WSB 额外信号
        if wsb_match:
            wsb_rank = wsb_match.get("rank", 999)
            if wsb_rank <= 5:
                score += 0.5  # WSB 焦点
                buzz = "hot" if buzz != "hot" else buzz

        score = max(1.0, min(10.0, score))

        # 来源列表
        sources = []
        if all_match:
            sources.append(f"Reddit 综合 #{rank}")
        if wsb_match:
            sources.append(f"WSB #{wsb_match.get('rank', '?')}")

        result = {
            "ticker": ticker,
            "rank": rank,
            "mentions": mentions,
            "mentions_24h_ago": mentions_24h,
            "mention_delta": mention_delta,
            "upvotes": upvotes,
            "momentum_pct": round(momentum_pct, 1),
            "reddit_buzz": buzz,
            "sentiment_score": round(score, 1),
            "sources": sources,
            "wsb_rank": wsb_match.get("rank") if wsb_match else None,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            atomic_json_write(cache_path, result)
        except (OSError, TypeError) as exc:
            _log.debug("Reddit sentiment 缓存写入失败 (%s): %s", ticker, exc)

        return result

    def _find_ticker(self, rankings: List[Dict], ticker: str) -> Optional[Dict]:
        """在排名列表中查找 ticker"""
        for item in rankings:
            if item.get("ticker", "").upper() == ticker:
                return item
        return None

    def _quiet_result(self, ticker: str) -> Dict:
        """无数据时返回的默认结果"""
        return {
            "ticker": ticker,
            "rank": None,
            "mentions": 0,
            "mentions_24h_ago": 0,
            "mention_delta": 0,
            "upvotes": 0,
            "momentum_pct": 0,
            "reddit_buzz": "quiet",
            "sentiment_score": 5.0,
            "sources": [],
            "wsb_rank": None,
            "timestamp": datetime.now().isoformat(),
        }


# ==================== 便捷函数 ====================

_client: Optional[RedditSentimentClient] = None
_client_lock = threading.Lock()


def get_reddit_sentiment(ticker: str) -> Dict:
    """便捷函数：获取 Reddit 情绪数据"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = RedditSentimentClient()
    return _client.get_ticker_sentiment(ticker)
