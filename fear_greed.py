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

import logging as _logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import observation_archive as _obs
import observation_layer as _obs_layer
from resilience import get_session, NETWORK_ERRORS

_log = _logging.getLogger("alpha_hive.fear_greed")

try:
    import requests as _req
except ImportError:
    _req = None

# v0.45.160：`_CACHE_PATH` 现在是**覆盖钩子**，默认 `None` ⇒ 运行时解析 `PATHS.*`。
# 原本是 `Path(__file__).parent / "cache" / "fear_greed.json"` —— 它**压根不读任何环境变量**，
# 比「模块级常量冻在 import 期」更彻底：`ALPHA_HIVE_HOME` / `ALPHA_HIVE_CACHE_DIR`
# 设成什么都无效，`tests/conftest.py::_isolate_env` 对它完全无效。
# 保留这个名字是因为 `tests/` 有 `monkeypatch.setattr(<mod>, "_CACHE_PATH", ...)` 依赖它。
_CACHE_PATH = None


def _cache_path() -> Path:
    """本模块的缓存落点。**调用时求值**（别求值成模块级常量或默认参数）。"""
    if _CACHE_PATH is not None:
        return Path(_CACHE_PATH)
    from hive_logger import PATHS
    # v0.45.591：缓存存原始观测（`observation_layer.cache_*`），文件名带观测层版本 ⇒ 不再是旧的 fear_greed.json
    return _obs_layer.cache_path(PATHS.cache_dir, "fear_greed")
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
        # v0.45.591：缓存里是上次取数的结局（原始观测 + 取数时刻），命中时用同一个解释函数重算、顺手补档——
        # 没开档的进程（MCP 等）填的缓存被扫描用上时，档案里照样有那份观测
        hit = _obs_layer.cache_read(_cache_path(), _CACHE_TTL)
        if hit is not None:
            try:
                return _from_cache(hit)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                _log.warning("F&G 缓存内容解释不了，重新取数: %s", e)

        if _req is None:
            return _default_result()

        # ── 主数据源：CNN 股票市场 F&G ───────────────────────────────────
        result = _fetch_cnn_fg()
        if result is None:
            # v0.45.568：没拿到内容 ⇒ 原始观测档案记失败事件（拿到了的已在取数处落档）
            _obs.fetch_failed("fear_greed.cnn", "unavailable", request={"url": _CNN_URL})
            # ── 备用：Alternative.me（加密 F&G）───────────────────────
            result = _fetch_alternative_me()
            if result is None:
                _obs.fetch_failed("fear_greed.alternative_me", "unavailable", request={"url": _ALT_URL})
        if result is None:
            result = _default_result()

        observed = result.pop(_obs_layer.CARRIER_KEY, None) or {}
        _obs_layer.cache_write(_cache_path(), kind=observed.get("kind"), payload=observed.get("payload"),
                               fetched_at=result.get("timestamp"))
        return result


def _from_cache(hit: Dict) -> Dict:
    """缓存里的取数结局 → 结果：与新取数同一个解释函数；时间戳沿用当初取数的时刻（BuzzBee 把它记成 fetched_at）。"""
    kind = hit["kind"]
    if kind is None:
        result = _default_result()
        result["timestamp"] = hit["fetched_at"]
        return result
    result = _INTERPRET[kind](hit["payload"], hit["fetched_at"])
    result[_obs.REF_KEY] = hit["ref"]
    return result


_CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_ALT_URL = "https://api.alternative.me/fng/?limit=1"


def _fetch_cnn_fg() -> Optional[Dict]:
    """从 CNN 非官方 API 获取股票市场 Fear & Greed Index（免费，无需 Key）"""
    try:
        resp = get_session("fear_greed").get(
            _CNN_URL,
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
        if fg.get("score") is None:
            return None
        payload = {"fear_and_greed": fg}
        # v0.45.591：解释拆成 `_from_cnn`（缓存命中走同一个函数）；解释得了才落档（解释不了 = 这次没拿到，走备用源）
        result = _from_cnn(payload, datetime.now().isoformat())
        # v0.45.568：CNN 的 fear_and_greed 对象原样进观测档案（graphdata 其余是历史序列，不是今天的观测）
        result[_obs.REF_KEY] = _obs.observe("fear_greed.cnn", payload, request={"url": _CNN_URL})
        result[_obs_layer.CARRIER_KEY] = {"kind": "fear_greed.cnn", "payload": payload}
        _log.info("CNN F&G: %d (%s)", result["value"], result["classification"])
        return result
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
            _ALT_URL,
            headers={"User-Agent": "AlphaHive/1.0"},
            timeout=8,
        )
        if not resp.ok:
            return None

        data = resp.json().get("data", [{}])[0]
        payload = {"data": data}
        result = _from_alternative_me(payload, datetime.now().isoformat())
        result[_obs.REF_KEY] = _obs.observe("fear_greed.alternative_me", payload, request={"url": _ALT_URL})
        result[_obs_layer.CARRIER_KEY] = {"kind": "fear_greed.alternative_me", "payload": payload}
        _log.info("Alternative.me F&G (crypto备用): %d (%s)", result["value"], result["classification"])
        return result
    except NETWORK_ERRORS as e:
        _log.debug("Alternative.me F&G 获取失败: %s", e)
        return None


# ── 解释：原始观测 → 结果（新取数与缓存命中共用；`timestamp` = 取数时刻，由调用方给）──────────────

_CLS_MAP = {
    "extreme fear":  "Extreme Fear",
    "fear":          "Fear",
    "neutral":       "Neutral",
    "greed":         "Greed",
    "extreme greed": "Extreme Greed",
}


def _from_cnn(payload: Dict, fetched_at: str) -> Dict:
    fg = payload["fear_and_greed"]
    value = int(round(float(fg.get("score"))))
    rating = fg.get("rating", "")
    # 统一 classification 命名
    classification = _CLS_MAP.get(rating.lower(), _classify(value))
    sentiment_score = round(max(1.0, min(10.0, value / 10.0)), 1)
    return {
        "value": value,
        "classification": classification,
        "sentiment_score": sentiment_score,
        "is_real_data": True,
        "source": "cnn",
        "timestamp": fetched_at,
    }


def _from_alternative_me(payload: Dict, fetched_at: str) -> Dict:
    data = payload["data"]
    value = int(data.get("value", 50))
    classification = data.get("value_classification", "Neutral")
    sentiment_score = round(max(1.0, min(10.0, value / 10.0)), 1)
    return {
        "value": value,
        "classification": classification,
        "sentiment_score": sentiment_score,
        "is_real_data": True,
        "source": "alternative_me",
        "timestamp": fetched_at,
    }


_INTERPRET = {"fear_greed.cnn": _from_cnn, "fear_greed.alternative_me": _from_alternative_me}


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
