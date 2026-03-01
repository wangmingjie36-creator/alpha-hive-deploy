"""
Yahoo Finance 热搜榜情绪模块（完全免费，无需注册）

数据源：Yahoo Finance Trending Tickers
- 每小时实时更新，反映散户关注度
- 覆盖美股 + 加密货币 + ETF
- 无 API Key，无速率限制

信号解读：
- 上榜（前25）→ 市场关注度高，散户情绪活跃
- 排名越靠前 → 关注度越强
- 注意：高关注≠方向，需结合其他指标判断
"""

import json
import logging as _logging
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from hive_logger import atomic_json_write, read_json_cache

_log = _logging.getLogger("alpha_hive.yahoo_trending")

try:
    import requests as _req
except ImportError:
    _req = None

_CACHE_PATH = Path(__file__).parent / "cache" / "yahoo_trending.json"
try:
    from config import CACHE_CONFIG as _CC
    _CACHE_TTL = _CC["ttl"].get("yahoo_trending", 900)
except (ImportError, KeyError):
    _CACHE_TTL = 900
_lock = threading.Lock()


def get_trending_tickers(count: int = 25) -> List[str]:
    """获取 Yahoo Finance 美股热搜榜（返回 ticker 列表，按热度降序）"""
    with _lock:
        cached = read_json_cache(_CACHE_PATH, _CACHE_TTL)
        if cached is not None:
            return cached

        if _req is None:
            return []

        try:
            resp = _req.get(
                f"https://query2.finance.yahoo.com/v1/finance/trending/US?count={count}",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                timeout=8,
            )
            if not resp.ok:
                return []

            quotes = resp.json()["finance"]["result"][0]["quotes"]
            tickers = [q["symbol"] for q in quotes if "symbol" in q]

            try:
                atomic_json_write(_CACHE_PATH, tickers)
            except (OSError, TypeError):
                pass

            return tickers

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.debug("Yahoo Trending 获取失败: %s", e)
            return []


def get_ticker_attention(ticker: str) -> Dict:
    """
    获取指定标的的 Yahoo 热搜关注度

    返回：{
        ticker, is_trending: bool, rank: int|None,
        attention_score: 0-10,  # 10=最热
        is_real_data: bool,
        description: str
    }
    """
    trending = get_trending_tickers()
    ticker_upper = ticker.upper()

    if not trending:
        return _default_result(ticker)

    # 查找排名（从 1 开始）
    rank = None
    for i, t in enumerate(trending, 1):
        if t == ticker_upper:
            rank = i
            break

    if rank is None:
        return {
            "ticker": ticker_upper,
            "is_trending": False,
            "rank": None,
            "attention_score": 4.5,   # 稍低于中性（未上榜）
            "is_real_data": True,
            "description": "未上 Yahoo 热搜",
            "timestamp": datetime.now().isoformat(),
        }

    # 评分：排名越靠前分越高
    if rank <= 3:
        score = 8.5
    elif rank <= 8:
        score = 7.5
    elif rank <= 15:
        score = 6.5
    else:
        score = 6.0

    return {
        "ticker": ticker_upper,
        "is_trending": True,
        "rank": rank,
        "attention_score": score,
        "is_real_data": True,
        "description": f"Yahoo热搜 #{rank}/25",
        "timestamp": datetime.now().isoformat(),
    }


def _default_result(ticker: str) -> Dict:
    return {
        "ticker": ticker.upper(),
        "is_trending": False,
        "rank": None,
        "attention_score": 5.0,
        "is_real_data": False,
        "description": "Yahoo Trending 不可用",
        "timestamp": datetime.now().isoformat(),
    }
