"""
StockTwits 社交情绪数据模块（免费 API，需 Access Token）

获取免费 Token：
1. 注册：https://stocktwits.com
2. 创建 App：https://stocktwits.com/developers/apps/new
3. 写入 ~/.alpha_hive_stocktwits_token 或设置 STOCKTWITS_TOKEN 环境变量

免费层：200 次请求/小时
"""

import json
import logging as _logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from hive_logger import atomic_json_write

_log = _logging.getLogger("alpha_hive.stocktwits_sentiment")

try:
    import requests as _req
except ImportError:
    _req = None

CACHE_DIR = Path(__file__).parent / "stocktwits_cache"
CACHE_DIR.mkdir(exist_ok=True)

STOCKTWITS_BASE = "https://api.stocktwits.com/api/2"
CACHE_TTL = 300  # 5 分钟


def _load_token() -> Optional[str]:
    """加载 StockTwits Access Token（环境变量 > 文件）"""
    token = os.environ.get("STOCKTWITS_TOKEN", "").strip()
    if token:
        return token
    key_file = os.path.expanduser("~/.alpha_hive_stocktwits_token")
    try:
        with open(key_file) as f:
            t = f.read().strip()
            if t:
                return t
    except (OSError, UnicodeDecodeError):
        pass
    return None


class StockTwitsClient:
    """StockTwits 情绪客户端"""

    def __init__(self):
        self._token = _load_token()
        self._last_request = 0.0
        self._lock = threading.Lock()

    def _throttle(self):
        """限流：最快 18 秒/次（免费层 200 次/小时 ≈ 18s 间隔）"""
        with self._lock:
            elapsed = time.time() - self._last_request
            if elapsed < 18.0:
                time.sleep(18.0 - elapsed)
            self._last_request = time.time()

    def get_symbol_sentiment(self, ticker: str) -> Dict:
        """
        获取 StockTwits 最新消息的情绪分布

        返回：{
            ticker, total_messages, bullish_count, bearish_count, neutral_count,
            bullish_pct, bearish_pct, sentiment_score: 0-10, is_real_data: bool
        }
        """
        if not self._token:
            return self._no_token_result(ticker)

        # 磁盘缓存
        cache_path = CACHE_DIR / f"{ticker.upper()}_sentiment.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < CACHE_TTL:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

        if _req is None:
            return self._no_token_result(ticker)

        try:
            self._throttle()
            params = {"access_token": self._token, "limit": 30}
            resp = _req.get(
                f"{STOCKTWITS_BASE}/streams/symbol/{ticker.upper()}.json",
                params=params,
                headers={"User-Agent": "AlphaHive/1.0"},
                timeout=10,
            )

            if resp.status_code == 429:
                _log.warning("StockTwits 速率限制 for %s", ticker)
                return self._no_token_result(ticker)
            if resp.status_code == 401:
                _log.warning("StockTwits Token 无效，请重新配置")
                self._token = None
                return self._no_token_result(ticker)
            if not resp.ok:
                return self._no_token_result(ticker)

            msgs = resp.json().get("messages", [])
            total = len(msgs)
            bullish = sum(
                1 for m in msgs
                if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish"
            )
            bearish = sum(
                1 for m in msgs
                if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish"
            )
            neutral = total - bullish - bearish

            bullish_pct = (bullish / total * 100) if total > 0 else 50.0
            bearish_pct = (bearish / total * 100) if total > 0 else 50.0

            # 评分：多头占比映射到 0-10
            raw_ratio = (bullish - bearish) / total if total > 0 else 0
            score = 5.0 + raw_ratio * 3.0
            score = max(1.0, min(10.0, score))

            result = {
                "ticker": ticker.upper(),
                "total_messages": total,
                "bullish_count": bullish,
                "bearish_count": bearish,
                "neutral_count": neutral,
                "bullish_pct": round(bullish_pct, 1),
                "bearish_pct": round(bearish_pct, 1),
                "sentiment_score": round(score, 1),
                "is_real_data": True,
                "timestamp": datetime.now().isoformat(),
            }

            try:
                atomic_json_write(cache_path, result)
            except (OSError, TypeError):
                pass

            return result

        except (ConnectionError, TimeoutError, OSError, ValueError) as e:
            _log.warning("StockTwits 请求失败 (%s): %s", ticker, e)
            return self._no_token_result(ticker)

    def _no_token_result(self, ticker: str) -> Dict:
        return {
            "ticker": ticker.upper(),
            "total_messages": 0,
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "bullish_pct": 50.0,
            "bearish_pct": 50.0,
            "sentiment_score": 5.0,
            "is_real_data": False,
            "timestamp": datetime.now().isoformat(),
        }

    @property
    def is_configured(self) -> bool:
        return bool(self._token) and _req is not None


_client: Optional[StockTwitsClient] = None
_client_lock = threading.Lock()


def get_stocktwits_sentiment(ticker: str) -> Dict:
    """便捷函数：获取 StockTwits 情绪"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = StockTwitsClient()
    return _client.get_symbol_sentiment(ticker)


def is_available() -> bool:
    """检查 StockTwits 是否已配置 Token"""
    return bool(_load_token()) and _req is not None
