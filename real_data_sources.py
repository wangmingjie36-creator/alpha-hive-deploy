"""
Alpha Hive - 真实数据源层
替换 ScoutBeeNova 中所有伪造的拥挤度指标

数据源：
- StockTwits API（免费，无需 API Key，限 200 req/hr）
- yfinance short interest（机构持仓+做空比例）
- 信息素板动态 bullish_agents 计数
"""

import json
import logging as _logging
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from hive_logger import atomic_json_write

_log = _logging.getLogger("alpha_hive.real_data_sources")

try:
    import requests
except ImportError:
    requests = None

CACHE_DIR = Path(__file__).parent / "data_cache"
CACHE_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()
_last_st_request = 0.0
_ST_MIN_INTERVAL = 2.0  # StockTwits: 200 req/hr ≈ 每 18s, 用 2s 保守

# ── 数据源健康追踪（#7）──
_HEALTH_FAIL_THRESHOLD = 3
_src_fail_counts: Dict[str, int] = {}
_src_degraded: Dict[str, bool] = {}


def _record_src_failure(source: str):
    """记录数据源连续失败，达到阈值时发出告警"""
    _src_fail_counts[source] = _src_fail_counts.get(source, 0) + 1
    count = _src_fail_counts[source]
    if count == _HEALTH_FAIL_THRESHOLD:
        _log.warning("⚠️ 数据源 [%s] 连续失败 %d 次，触发降级告警", source, count)
        _src_degraded[source] = True
        _try_src_slack_alert(source, count)
    elif count > _HEALTH_FAIL_THRESHOLD and count % 5 == 0:
        _log.warning("⚠️ 数据源 [%s] 持续降级，累计失败 %d 次", source, count)


def _record_src_success(source: str):
    """记录数据源成功，重置计数器"""
    prev = _src_fail_counts.get(source, 0)
    if prev >= _HEALTH_FAIL_THRESHOLD:
        _log.info("✅ 数据源 [%s] 已恢复（之前连续失败 %d 次）", source, prev)
    _src_fail_counts[source] = 0
    _src_degraded[source] = False


def _try_src_slack_alert(source: str, fail_count: int):
    """尝试通过 Slack 发送降级告警（静默失败）"""
    try:
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        if getattr(n, "enabled", False):
            n.send_risk_alert(
                alert_title=f"数据源降级：{source}",
                alert_message=f"*{source}* 已连续失败 {fail_count} 次，进入降级模式，数据质量受影响。",
                severity="MEDIUM",
            )
    except Exception as _se:
        _log.debug("Slack 数据源降级告警发送失败: %s", _se)


def _st_throttle():
    global _last_st_request
    with _lock:
        now = time.time()
        elapsed = now - _last_st_request
        if elapsed < _ST_MIN_INTERVAL:
            time.sleep(_ST_MIN_INTERVAL - elapsed)
        _last_st_request = time.time()


def _read_cache(name: str, ttl: int = 3600) -> Optional[Dict]:
    """读磁盘缓存"""
    path = CACHE_DIR / f"{name}.json"
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < ttl:
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                _log.debug("缓存读取失败 %s: %s", name, exc)
    return None


def _write_cache(name: str, data: Dict):
    """写磁盘缓存"""
    try:
        atomic_json_write(CACHE_DIR / f"{name}.json", data)
    except (OSError, TypeError) as exc:
        _log.debug("缓存写入失败 %s: %s", name, exc)


# ==================== 社交热度（Reddit ApeWisdom + 成交量代理）====================

def get_social_buzz(ticker: str) -> Dict:
    """
    获取社交热度数据（基于 Reddit ApeWisdom 提及量）

    StockTwits 公开 API 已停用(403)，改用 Reddit 提及量作为社交热度代理。
    提及量映射为等效 messages_per_day，便于 CrowdingDetector 使用。

    返回:
        {
            "messages_per_day": int,     # Reddit 提及量映射为等效日消息量
            "bullish_pct": float,        # 看涨比例 0-100
            "bearish_pct": float,        # 看跌比例 0-100
            "data_quality": "real" | "fallback",
            "source": "reddit_apewisdom"
        }
    """
    cache_key = f"social_{ticker}"
    cached = _read_cache(cache_key, ttl=3600)
    if cached:
        return cached

    fallback = {
        "messages_per_day": 0,
        "bullish_pct": 50.0,
        "bearish_pct": 50.0,
        "data_quality": "fallback",
        "source": "none",
    }

    try:
        from reddit_sentiment import get_reddit_sentiment
        reddit = get_reddit_sentiment(ticker)

        mentions = reddit.get("mentions", 0)
        score = reddit.get("sentiment_score", 5.0)
        rank = reddit.get("rank")

        # 将 Reddit 提及量映射为等效 StockTwits 日消息量
        # Reddit 前 10 名 ≈ 30k-100k StockTwits 消息
        # Reddit 前 50 名 ≈ 5k-30k
        # 不在榜 ≈ 0
        if rank and rank <= 5:
            equiv_messages = max(mentions * 200, 50000)
        elif rank and rank <= 15:
            equiv_messages = max(mentions * 100, 20000)
        elif rank and rank <= 50:
            equiv_messages = max(mentions * 50, 5000)
        elif mentions > 0:
            equiv_messages = mentions * 30
        else:
            equiv_messages = 0

        # Reddit sentiment_score (1-10) → bullish_pct
        bullish_pct = min(100.0, max(0.0, (score - 1) / 8 * 100))

        result = {
            "messages_per_day": equiv_messages,
            "bullish_pct": round(bullish_pct, 1),
            "bearish_pct": round(100.0 - bullish_pct, 1),
            "data_quality": "real",  # API 成功返回即为真实数据，不在榜≠降级
            "source": "reddit_apewisdom",
        }

        _write_cache(cache_key, result)
        _record_src_success("reddit_apewisdom")
        return result

    except (ImportError, ConnectionError, TimeoutError, OSError, ValueError, KeyError) as exc:
        _record_src_failure("reddit_apewisdom")
        _log.debug("get_social_buzz 降级为 fallback (%s): %s", ticker, exc)
        return fallback


# ==================== Short Interest (yfinance) ====================

def get_short_interest(ticker: str) -> Dict:
    """
    从 yfinance 获取做空比例数据

    返回:
        {
            "short_ratio": float,        # 做空天数比率（空头回补天数）
            "short_pct_float": float,    # 做空比例占流通股
            "data_quality": "real" | "fallback"
        }
    """
    cache_key = f"short_{ticker}"
    cached = _read_cache(cache_key, ttl=86400)  # 做空数据日更，缓存 24h
    if cached:
        return cached

    fallback = {
        "short_ratio": 0.0,
        "short_pct_float": 0.05,
        "data_quality": "fallback",
    }

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info

        short_ratio = info.get("shortRatio", 0.0) or 0.0
        short_pct = info.get("shortPercentOfFloat", 0.0) or 0.0

        # yfinance 返回的 shortPercentOfFloat 可能是百分比或小数
        if short_pct > 1.0:
            short_pct = short_pct / 100.0

        result = {
            "short_ratio": round(float(short_ratio), 2),
            "short_pct_float": round(float(short_pct), 4),
            "data_quality": "real" if (short_ratio > 0 or short_pct > 0) else "fallback",
        }

        _write_cache(cache_key, result)
        _record_src_success("yfinance_short_interest")
        return result

    except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as exc:
        _record_src_failure("yfinance_short_interest")
        _log.debug("get_short_interest 降级为 fallback (%s): %s", ticker, exc)
        return fallback


# ==================== 动态 Bullish Agents 计数 ====================

def get_bullish_agents_count(ticker: str, board) -> int:
    """
    从信息素板动态计算看涨 Agent 数量

    Args:
        ticker: 股票代码
        board: PheromoneBoard 实例

    Returns:
        当前看涨的 Agent 数量 (0-6)
    """
    if board is None:
        return 3  # 无信息素板时返回中性默认值

    try:
        signals = board.get_top_signals(ticker, n=10)
        bullish = sum(1 for s in signals if s.direction == "bullish")
        return bullish
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        _log.debug("get_bullish_agents_count 降级为默认值 (%s): %s", ticker, exc)
        return 3


# ==================== 统一拥挤度指标获取 ====================

def get_real_crowding_metrics(ticker: str, stock_data: Dict, board=None) -> Dict:
    """
    构建真实的拥挤度指标（替代 ScoutBeeNova 中的伪造数据）

    Args:
        ticker: 股票代码
        stock_data: 来自 _fetch_stock_data() 的 yfinance 数据
        board: PheromoneBoard 实例（用于动态 bullish_agents）

    Returns:
        {
            "stocktwits_messages_per_day": int,    # 真实 StockTwits 数据
            "google_trends_percentile": float,     # 暂用成交量百分位代替（标记降级）
            "bullish_agents": int,                 # 信息素板动态计数
            "polymarket_odds_change_24h": float,   # 暂用价格波动代替（标记降级）
            "seeking_alpha_page_views": int,       # 暂用成交量比率代替（标记降级）
            "short_float_ratio": float,            # 真实 yfinance 做空数据
            "price_momentum_5d": float,            # 真实 yfinance 动量
            "data_quality": {...}                  # 每个指标的数据质量标记
        }
    """
    # 1. 社交热度（Reddit ApeWisdom 提及量）
    st_data = get_social_buzz(ticker)

    # 2. Short Interest（真实 yfinance）
    short_data = get_short_interest(ticker)

    # 3. 动态 bullish_agents（真实信息素板）
    bullish = get_bullish_agents_count(ticker, board)

    # 4. Google Trends — 暂不接入 pytrends（高频使用会被封 IP），
    #    改用成交量异动百分位作为"关注度"代理指标
    vol_ratio = stock_data.get("volume_ratio", 1.0)
    # volume_ratio → 百分位映射: 0.5x=20, 1.0x=50, 2.0x=80, 3.0x=95
    google_proxy = min(100, max(0, (vol_ratio - 0.5) / 2.5 * 80 + 20))

    # 5. Polymarket — 暂不接入（需要搜索 market slug），
    #    改用 5 日动量绝对值作为"赔率变化速度"代理
    poly_proxy = abs(stock_data.get("momentum_5d", 0.0)) * 0.8

    # 6. Seeking Alpha — 无免费 API，
    #    改用 StockTwits 消息量 * 2 作为"页面浏览"代理
    sa_proxy = st_data["messages_per_day"] * 2

    metrics = {
        "stocktwits_messages_per_day": st_data["messages_per_day"],
        "google_trends_percentile": round(google_proxy, 1),
        "bullish_agents": bullish,
        "polymarket_odds_change_24h": round(poly_proxy, 2),
        "seeking_alpha_page_views": sa_proxy,
        "short_float_ratio": short_data["short_pct_float"],
        "price_momentum_5d": stock_data.get("momentum_5d", 0.0),
        "data_quality": {
            "social_buzz": st_data["data_quality"],         # Reddit ApeWisdom 真实数据
            "google_trends": "proxy_volume",                # 成交量代理指标
            "bullish_agents": "real" if board else "default",
            "polymarket": "proxy_momentum",                 # 动量代理指标
            "seeking_alpha": "proxy_social",                # 社交热度代理指标
            "short_interest": short_data["data_quality"],   # yfinance 真实数据
            "momentum": "real",                             # yfinance 真实数据
        }
    }

    return metrics
