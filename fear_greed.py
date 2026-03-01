"""
Fear & Greed Index 模块（Alternative.me 免费 API，无需 API Key）

数据：市场整体情绪指数 0-100
  0-25:  Extreme Fear（极度恐惧）
  25-45: Fear（恐惧）
  45-55: Neutral（中性）
  55-75: Greed（贪婪）
  75-100: Extreme Greed（极度贪婪）

API：https://api.alternative.me/fng/
"""

import json
import logging as _logging
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict

from hive_logger import atomic_json_write, read_json_cache

_log = _logging.getLogger("alpha_hive.fear_greed")

try:
    import requests as _req
except ImportError:
    _req = None

_CACHE_PATH = Path(__file__).parent / "cache" / "fear_greed.json"
_CACHE_TTL = 3600  # 1 小时（指数每天更新）
_lock = threading.Lock()


def get_fear_greed() -> Dict:
    """
    获取当前市场 Fear & Greed Index

    返回：{
        value: int (0-100),
        classification: str,
        sentiment_score: float (0-10),
        is_real_data: bool,
        timestamp: str
    }
    """
    with _lock:
        cached = read_json_cache(_CACHE_PATH, _CACHE_TTL)
        if cached is not None:
            return cached

        if _req is None:
            return _default_result()

        try:
            resp = _req.get(
                "https://api.alternative.me/fng/?limit=1",
                headers={"User-Agent": "AlphaHive/1.0"},
                timeout=8,
            )
            if not resp.ok:
                return _default_result()

            data = resp.json().get("data", [{}])[0]
            value = int(data.get("value", 50))
            classification = data.get("value_classification", "Neutral")

            # 映射到 0-10（越贪婪分越高）
            sentiment_score = round(max(1.0, min(10.0, value / 10.0)), 1)

            result = {
                "value": value,
                "classification": classification,
                "sentiment_score": sentiment_score,
                "is_real_data": True,
                "timestamp": datetime.now().isoformat(),
            }

            try:
                atomic_json_write(_CACHE_PATH, result)
            except (OSError, TypeError):
                pass

            return result

        except (ConnectionError, TimeoutError, OSError, ValueError) as e:
            _log.debug("Fear & Greed 获取失败: %s", e)
            return _default_result()


def _default_result() -> Dict:
    return {
        "value": 50,
        "classification": "Neutral",
        "sentiment_score": 5.0,
        "is_real_data": False,
        "timestamp": datetime.now().isoformat(),
    }
