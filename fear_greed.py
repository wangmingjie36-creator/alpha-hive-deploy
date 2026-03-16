"""
Fear & Greed Index 模块

数据源优先级：
  1. CNN 股票市场 F&G（主要）：production.dataviz.cnn.io — 免费，无 API Key
     反映股票市场真实情绪（动量/VIX/安全资产/期权/宽度/市场强度/市场动量）
  2. Alternative.me 加密 F&G（备用）：api.alternative.me — 免费，无 API Key
     注意：加密 F&G 与股票 F&G 有一定相关性但不等价

数据：情绪指数 0-100
  0-25:  Extreme Fear（极度恐惧）
  26-45: Fear（恐惧）
  46-55: Neutral（中性）
  56-75: Greed（贪婪）
  76-100: Extreme Greed（极度贪婪）
"""

import json
import logging as _logging
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from hive_logger import atomic_json_write, read_json_cache
from resilience import get_session, NETWORK_ERRORS

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
    获取当前股票市场 Fear & Greed Index
    主数据源：CNN（股票市场）；备用：Alternative.me（加密市场）

    返回：{
        value: int (0-100),
        classification: str,
        sentiment_score: float (0-10),
        is_real_data: bool,
        source: str,      # "cnn" | "alternative_me" | "fallback"
        timestamp: str
    }
    """
    with _lock:
        cached = read_json_cache(_CACHE_PATH, _CACHE_TTL)
        if cached is not None:
            return cached

        if _req is None:
            return _default_result()

        # ── 主数据源：CNN 股票市场 F&G ───────────────────────────────────
        result = _fetch_cnn_fg()
        if result is None:
            # ── 备用：Alternative.me（加密 F&G）───────────────────────
            result = _fetch_alternative_me()
        if result is None:
            result = _default_result()

        try:
            atomic_json_write(_CACHE_PATH, result)
        except (OSError, TypeError):
            pass

        return result


def _fetch_cnn_fg() -> Optional[Dict]:
    """从 CNN 非官方 API 获取股票市场 Fear & Greed Index（免费，无需 Key）"""
    try:
        resp = get_session("fear_greed").get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://edition.cnn.com/",
            },
            timeout=10,
        )
        if not resp.ok:
            _log.debug("CNN F&G HTTP %s", resp.status_code)
            return None

        fg = resp.json().get("fear_and_greed", {})
        raw_score = fg.get("score")
        if raw_score is None:
            return None

        value = int(round(float(raw_score)))
        rating = fg.get("rating", "")

        # 统一 classification 命名
        _cls_map = {
            "extreme fear":  "Extreme Fear",
            "fear":          "Fear",
            "neutral":       "Neutral",
            "greed":         "Greed",
            "extreme greed": "Extreme Greed",
        }
        classification = _cls_map.get(rating.lower(), _classify(value))
        sentiment_score = round(max(1.0, min(10.0, value / 10.0)), 1)

        _log.info("CNN F&G: %d (%s)", value, classification)
        return {
            "value": value,
            "classification": classification,
            "sentiment_score": sentiment_score,
            "is_real_data": True,
            "source": "cnn",
            "timestamp": datetime.now().isoformat(),
        }
    except NETWORK_ERRORS as e:
        _log.debug("CNN F&G 获取失败: %s", e)
        return None
    except Exception as e:
        _log.debug("CNN F&G 解析失败: %s", e)
        return None


def _fetch_alternative_me() -> Optional[Dict]:
    """从 Alternative.me 获取加密市场 F&G（备用）"""
    try:
        resp = get_session("fear_greed").get(
            "https://api.alternative.me/fng/?limit=1",
            headers={"User-Agent": "AlphaHive/1.0"},
            timeout=8,
        )
        if not resp.ok:
            return None

        data = resp.json().get("data", [{}])[0]
        value = int(data.get("value", 50))
        classification = data.get("value_classification", "Neutral")
        sentiment_score = round(max(1.0, min(10.0, value / 10.0)), 1)

        _log.info("Alternative.me F&G (crypto备用): %d (%s)", value, classification)
        return {
            "value": value,
            "classification": classification,
            "sentiment_score": sentiment_score,
            "is_real_data": True,
            "source": "alternative_me",
            "timestamp": datetime.now().isoformat(),
        }
    except NETWORK_ERRORS as e:
        _log.debug("Alternative.me F&G 获取失败: %s", e)
        return None


def _classify(value: int) -> str:
    """根据数值返回中英文分类"""
    if value <= 25:  return "Extreme Fear"
    if value <= 45:  return "Fear"
    if value <= 55:  return "Neutral"
    if value <= 75:  return "Greed"
    return "Extreme Greed"


def _default_result() -> Dict:
    return {
        "value": 50,
        "classification": "Neutral",
        "sentiment_score": 5.0,
        "is_real_data": False,
        "timestamp": datetime.now().isoformat(),
    }


def get_vix_term_structure() -> Dict:
    """
    获取 VIX 波动率期限结构（代理到 vix_term_structure.py）。
    GuardBee 宏观分析的核心信号之一。
    """
    try:
        from vix_term_structure import get_vix_term_structure as _get_vts
        return _get_vts()
    except Exception as e:
        _log.debug("VIX term structure unavailable: %s", e)
        return {"structure": "unknown", "signal": "", "source": f"error:{e}"}
