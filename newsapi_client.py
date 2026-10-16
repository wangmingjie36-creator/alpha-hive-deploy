"""
P4: 免费新闻全文/摘要获取客户端

渠道优先级：
1. Alpha Vantage NEWS_SENTIMENT（AV_API_KEY 或 ~/.alpha_hive_av_key）
   - 免费 25 次/天，含预处理情绪评分 + 每篇文章摘要
2. Yahoo Finance 新闻搜索（免费，无需注册，常规备用）
   - 返回标题 + 摘要片段，关键词打标

输出供 BuzzBeeWhisper 使用，经 DataQualityChecker 清洗。

关于 async：本模块刻意保持同步。BuzzBeeWhisper 在 ThreadPoolExecutor
线程中调用，IO 已由线程并发覆盖，换 async 仅增加复杂度无实质收益。
"""

from __future__ import annotations

import collections
import json
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from hive_logger import PATHS, atomic_json_write, get_logger, pdt_today

_log = get_logger("newsapi")

try:
    import requests as _req
except ImportError:
    _req = None

# 弹性层：熔断器 + 限流器 + 连接池
try:
    from resilience import get_session, CircuitBreaker, RateLimiter, NETWORK_ERRORS
    _news_breaker = CircuitBreaker("newsapi", failure_threshold=5, recovery_timeout=120.0)
    _news_limiter = RateLimiter(rate=1.0, burst=2)  # 1 req/s（AV 每日限额 25 次）
    _RESILIENCE_OK = True
except ImportError:
    _news_breaker = None
    _news_limiter = None
    _RESILIENCE_OK = False
    NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError, ValueError, KeyError)

# 导入清洗工具（与 Agent 层保持一致）
try:
    from models import clean_score
    _MODELS_OK = True
except ImportError:
    _MODELS_OK = False

_CACHE_DIR = Path(PATHS.home) / "cache" / "news"
_CACHE_TTL = 1800   # 30 分钟（AV 每天 25 次，不能太频繁）
_lock = threading.Lock()

# AV 每日配额追踪（免费 25 次/天，进程内计数，重启归零）
_AV_DAILY_LIMIT = 25
_av_daily: Dict = {"date": "", "count": 0}
_av_daily_lock = threading.Lock()


def _av_quota_ok() -> bool:
    """检查并递增 AV 日配额计数器。配额用尽返回 False。

    NOTE: AV 服务实际 reset 时区不确定（推测 UTC midnight）；项目内部按 PDT 美股交易日
    配额跟单日扫描节奏对齐更直观，可能与 AV 实际配额窗口有 ±7h 偏差。
    """
    today = pdt_today()  # v0.28.0: 美股交易日
    with _av_daily_lock:
        if _av_daily["date"] != today:
            _av_daily["date"] = today
            _av_daily["count"] = 0
        if _av_daily["count"] >= _AV_DAILY_LIMIT:
            return False
        _av_daily["count"] += 1
        return True


# v0.45.444：本进程每次真去取（不含缓存命中）时**主源**这一步的结局计数 + 拒绝原文
# （v0.45.445 起主源是 Massive，`config.NEWS_SOURCE_CONFIG["primary"]` 可回滚到 AV）。
# 降级到 Yahoo 本身不报错，但 Yahoo 关键词打标与主源的逐文章模型标签是两个分类器，同一只票的
# news_signal 系统性低约 20–27 分（2026-10-06 全 30 只降级、10-08 校准实测）——「谁会红？」靠这里经
# `summarize_news_sources` → `scan_timing.extra.news_sources` → `alert_manager` P1。
_primary_run_stats: Dict = {"status": {}, "messages": {}}
_primary_run_stats_lock = threading.Lock()
_MESSAGE_MAX = 300


def _record_primary_attempt(status: str, message: Optional[str] = None) -> None:
    with _primary_run_stats_lock:
        counts = _primary_run_stats["status"]
        counts[status] = counts.get(status, 0) + 1
        if message:
            msgs = _primary_run_stats["messages"]
            msgs[message] = msgs.get(message, 0) + 1


def get_primary_run_stats() -> Dict:
    """本进程主源这一步的结局（副本）：`status` 计数 + `messages`（拒绝原文，按出现次数降序）。"""
    with _primary_run_stats_lock:
        return {
            "status": dict(_primary_run_stats["status"]),
            "messages": [{"text": t, "count": c}
                         for t, c in sorted(_primary_run_stats["messages"].items(), key=lambda kv: -kv[1])],
        }


def _redact(text: str, api_key: Optional[str]) -> str:
    text = str(text)
    if api_key:
        text = text.replace(api_key, "***")
    return text.strip()[:_MESSAGE_MAX]


def _av_refusal_text(data: Dict, api_key: Optional[str]) -> str:
    """AV 限速 / 错误响应的原文（`Information` / `Note` / `Error Message`），去 key、截断。

    原文是分辨撞的是哪种限额（每日 25 次 vs 每分钟 5 次）的唯一依据；v0.45.444 前只记键名。
    """
    for k in ("Information", "Note", "Error Message", "Error"):
        if data.get(k):
            return _redact(data[k], api_key)
    return ""


def _primary_fail(ticker: str, status: str, message: Optional[str] = None) -> Dict:
    """主源这一步没拿到数据：结构同 `_fallback`，外加 `primary_status` / `primary_message`（由 `get_ticker_news` 取走）。"""
    r = _fallback(ticker)
    r["primary_status"] = status
    if message:
        r["primary_message"] = message
    return r


# ==================== 主源选择（v0.45.445） ====================

_NEWS_CFG_DEFAULT = {"primary": "massive", "massive_calls_per_minute": 5,
                     "massive_acquire_timeout_s": 40.0, "massive_request_timeout_s": 10.0}


def _news_cfg() -> Dict:
    """`config.NEWS_SOURCE_CONFIG`，调用时读（测试可 monkeypatch）。"""
    try:
        from config import NEWS_SOURCE_CONFIG
        return {**_NEWS_CFG_DEFAULT, **NEWS_SOURCE_CONFIG}
    except ImportError:
        return dict(_NEWS_CFG_DEFAULT)


def news_primary_source() -> str:
    """当前配置的新闻主源名（`massive` / `alpha_vantage`）。BuzzBee 写进 details、汇总按它算降级比例。"""
    return str(_news_cfg().get("primary") or "massive")


def _load_massive_key() -> Optional[str]:
    """Massive API key：只走 `config.get_secret`（环境变量 > `~/.alpha_hive_massive_key`，文件须 0600）。"""
    try:
        from config import get_secret
        return get_secret("MASSIVE_API_KEY") or None
    except ImportError:
        return None


class _SlidingWindowLimiter:
    """`max_calls` 次 / `period` 秒的滑动窗口（Massive 免费档：5 次/分钟）。

    `acquire(timeout)` 拿到名额返回 True；`timeout` 内不可能空出名额就**立即**返回 False
    （调用方降级 Yahoo、记 `limiter_timeout`）——不白等。`timeout` 必须小于 Phase-1 等 Buzz 的 60s，
    否则 Buzz 整个超时、连 Yahoo 都拿不到。
    """

    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = int(max_calls)
        self.period = float(period)
        self._ts: "collections.deque[float]" = collections.deque()
        self._lock = threading.Lock()

    def acquire(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                now = time.monotonic()
                while self._ts and now - self._ts[0] >= self.period:
                    self._ts.popleft()
                if len(self._ts) < self.max_calls:
                    self._ts.append(now)
                    return True
                wait = self.period - (now - self._ts[0])
            if now + wait > deadline:
                return False
            time.sleep(wait + 0.01)


_massive_limiter = _SlidingWindowLimiter(_news_cfg().get("massive_calls_per_minute", 5), 60.0)


def _massive_refusal_text(data, api_key: Optional[str]) -> str:
    """Massive 错误响应原文（`error` / `message`，退而求其次 `status`），去 key、截断。"""
    if not isinstance(data, dict):
        return ""
    for k in ("error", "message", "status"):
        if data.get(k):
            return _redact(data[k], api_key)
    return ""

_YF_NEWS_URL = "https://query2.finance.yahoo.com/v1/finance/search"
_AV_NEWS_URL = "https://www.alphavantage.co/query"
_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
}

# AV 情绪阈值（官方文档：>0.15 bullish, <-0.15 bearish）
_AV_BULL_THRESHOLD = 0.15
_AV_BEAR_THRESHOLD = -0.15

# 情绪关键词集合（统一词库，来自 config.SENTIMENT_KEYWORDS）
try:
    from config import SENTIMENT_KEYWORDS as _SK
    _BULLISH_KWS = _SK["bullish"]
    _BEARISH_KWS = _SK["bearish"]
except (ImportError, KeyError):
    _BULLISH_KWS = {"surge", "soar", "rally", "beat", "record", "upgrade", "buy", "growth",
                     "profit", "expand", "win", "strong", "bullish", "outperform", "positive"}
    _BEARISH_KWS = {"drop", "fall", "miss", "downgrade", "sell", "loss", "weak", "decline",
                     "cut", "warning", "bearish", "underperform", "negative", "crash"}

_VALID_LABELS = {"bullish", "bearish", "neutral"}


# ==================== API Key 加载 ====================

def _load_av_key() -> Optional[str]:
    """加载 AV API Key：config.get_secret > 环境变量 > ~/.alpha_hive_av_key 文件"""
    try:
        from config import get_secret
        key = get_secret("AV_API_KEY") or get_secret("ALPHA_VANTAGE_KEY")
        if key:
            return key
    except ImportError:
        pass
    key = (
        os.environ.get("AV_API_KEY", "").strip()
        or os.environ.get("ALPHA_VANTAGE_KEY", "").strip()
    )
    if key:
        return key
    key_file = os.path.expanduser("~/.alpha_hive_av_key")
    try:
        with open(key_file) as f:
            key = f.read().strip()
            if key:
                return key
    except (OSError, UnicodeDecodeError):
        pass
    return None


# ==================== DataQualityChecker 集成 ====================

def _clean_sentiment_score(value) -> float:
    """
    清洗情绪评分：期望范围 1.0-10.0，处理 None/NaN/越界。
    使用 models.clean_score（0-10）然后夹到 [1.0, 10.0]。
    """
    if _MODELS_OK:
        raw = clean_score(value)          # 处理 None/NaN/Inf → 返回 5.0
    else:
        try:
            raw = float(value)
        except (TypeError, ValueError):
            raw = 5.0
        if math.isnan(raw) or math.isinf(raw):
            raw = 5.0
        raw = max(0.0, min(10.0, raw))
    # 夹到 [1.0, 10.0]（情绪分不允许为 0）
    return max(1.0, min(10.0, round(raw, 2)))


def _clean_label(label: str) -> str:
    """清洗 sentiment_label：不合法值 → neutral"""
    if isinstance(label, str) and label.lower() in _VALID_LABELS:
        return label.lower()
    return "neutral"


def _clean_articles(articles: List[Dict]) -> tuple:
    """
    清洗文章列表：
    - 修复非法 sentiment_label
    - 过滤空标题
    - 截断过长摘要

    返回: (cleaned_articles, issues_list)
    """
    cleaned = []
    issues = []
    for i, art in enumerate(articles):
        title = (art.get("title") or "").strip()
        if not title:
            issues.append(f"article[{i}]: 空标题，已跳过")
            continue

        label = _clean_label(art.get("sentiment_label", "neutral"))
        if label != art.get("sentiment_label"):
            issues.append(
                f"article[{i}]: sentiment_label '{art.get('sentiment_label')}' → '{label}'"
            )

        cleaned.append({
            **art,
            "title": title[:200],
            "summary": (art.get("summary") or "")[:300],
            "publisher": (art.get("publisher") or "")[:100],
            "sentiment_label": label,
        })

    return cleaned, issues


def _check_count_consistency(
    bullish: int, bearish: int, neutral: int, total: int
) -> List[str]:
    """检查计数一致性"""
    issues = []
    if bullish < 0:
        issues.append(f"bullish_count={bullish} < 0")
    if bearish < 0:
        issues.append(f"bearish_count={bearish} < 0")
    if neutral < 0:
        issues.append(f"neutral_count={neutral} < 0")
    if (bullish + bearish + neutral) != total:
        issues.append(
            f"count mismatch: {bullish}+{bearish}+{neutral}="
            f"{bullish+bearish+neutral} ≠ total={total}"
        )
    return issues


# ==================== 主入口 ====================

def get_ticker_news(ticker: str, max_articles: int = 10) -> Dict:
    """
    获取 ticker 相关新闻（带缓存 + DataQualityChecker 清洗）

    返回: {
        ticker, articles, total_articles,
        bullish_count, bearish_count, neutral_count,
        sentiment_score: 1-10, dominant_theme,
        source, is_real_data,
        data_quality: {issues: [...], cleaned_fields: [...]}
    }
    """
    cache_path = _CACHE_DIR / f"{ticker}_news.json"

    with _lock:
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < _CACHE_TTL:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 主源（v0.45.445 起 Massive；`config.NEWS_SOURCE_CONFIG["primary"]` 可回滚 AV）
    # v0.45.444：主源这一步的结局随结果带出（`primary_attempt`，同时计入 `get_primary_run_stats`）。
    # 只多一个键，去向与 news 取值不变；缓存命中不重复计数（那次取数早已记过）。
    primary = news_primary_source()
    if primary == "massive":
        key, fetch = _load_massive_key(), _fetch_massive_news
    elif primary == "alpha_vantage":
        key, fetch = _load_av_key(), _fetch_av_news
    else:
        key, fetch = None, None
    if fetch is None:
        attempt: Dict = {"source": primary, "status": "unknown_primary"}
    elif not key:
        attempt = {"source": primary, "status": "no_key"}
    elif _req is None:
        attempt = {"source": primary, "status": "no_requests"}
    else:
        result = fetch(ticker, key, max_articles)
        status = result.pop("primary_status", None)
        message = result.pop("primary_message", None)
        if result.get("is_real_data"):
            attempt = {"source": primary, "status": "ok"}
        else:
            # 主源回了文章但清洗后一篇不剩：`_build_result` 判 is_real_data=False，没有出口状态
            attempt = {"source": primary, "status": status or "no_usable_articles"}
            if message:
                attempt["message"] = message
        _record_primary_attempt(attempt["status"], attempt.get("message"))
        if result.get("is_real_data"):
            result["primary_attempt"] = attempt
            _safe_cache(cache_path, result)
            return result
    if attempt["status"] in ("no_key", "no_requests", "unknown_primary"):
        _record_primary_attempt(attempt["status"])

    # 2. Yahoo Finance 免费备用
    result = _fetch_yf_news(ticker, max_articles)
    result["primary_attempt"] = attempt
    _safe_cache(cache_path, result)
    return result


def _safe_cache(path: Path, data: Dict):
    try:
        atomic_json_write(path, data)
    except (OSError, TypeError):
        pass


def summarize_news_sources(swarm_results: Dict) -> Dict:
    """本轮扫描新闻通道的实际来源分布（v0.45.444；v0.45.445 起按配置的主源算），写进 `scan_timing.extra.news_sources`。

    逐票来源读 BuzzBee `details.news_source` / `details.news_primary_status`；没有来源键的标的
    （Buzz 报错、或缓存是旧版写的）记 `unknown`，**不算进** `non_primary_share` 的分子分母——
    否则 Buzz 整片失败会被误报成「新闻降级」。拒绝原文来自本进程 `get_primary_run_stats()`。

    AV 时代常态 0.20–0.27（30>25 的结构性本地配额让固定 5 只走 Yahoo，另有开跑第一秒的每分钟限速拒绝）；
    Massive 无日上限、5 次/分钟远高于扫描节奏（≈1.2 只/分钟）⇒ 常态应接近 0。
    """
    primary = news_primary_source()
    by_source: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    n_unknown = 0
    for r in (swarm_results or {}).values():
        det = (((r or {}).get("agent_details") or {}).get("BuzzBeeWhisper") or {}).get("details") or {}
        src = det.get("news_source")
        if not src:
            n_unknown += 1
            continue
        by_source[src] = by_source.get(src, 0) + 1
        st = det.get("news_primary_status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
    n_known = sum(by_source.values())
    n_primary = by_source.get(primary, 0)
    run = get_primary_run_stats()
    return {
        "available": n_known > 0,
        "primary_source": primary,
        "n_known": n_known,
        "n_unknown": n_unknown,
        "by_source": by_source,
        "primary_status": by_status,
        "non_primary_share": round((n_known - n_primary) / n_known, 4) if n_known else None,
        "primary_run_status": run["status"],
        "refusal_messages": run["messages"][:5],
    }


# ==================== Yahoo Finance ====================

def _fetch_yf_news(ticker: str, max_articles: int = 10) -> Dict:
    """通过 Yahoo Finance 搜索 API 获取新闻"""
    if _req is None:
        return _fallback(ticker)

    # 熔断检查
    if _news_breaker and not _news_breaker.allow_request():
        _log.warning("newsapi 熔断中，跳过 YF 请求 (%s)", ticker)
        return _fallback(ticker)

    try:
        if _news_limiter and not _news_limiter.acquire(timeout=10):
            _log.warning("newsapi 限流超时，跳过 YF 请求 (%s)", ticker)
            return _fallback(ticker)
        params = {
            "q": ticker,
            "newsCount": max_articles,
            "enableFuzzyQuery": "false",
            "enableEnhancedTrivialQuery": "true",
        }
        _session = get_session("newsapi") if _RESILIENCE_OK else _req
        resp = _session.get(_YF_NEWS_URL, headers=_YF_HEADERS, params=params, timeout=8)
        if not resp.ok:
            if _news_breaker:
                _news_breaker.record_failure()
            return _fallback(ticker)

        if _news_breaker:
            _news_breaker.record_success()

        news_items = resp.json().get("news", [])
        if not news_items:
            return _fallback(ticker)

        raw_articles = []
        for item in news_items[:max_articles]:
            ts = item.get("providerPublishTime", 0)
            raw_articles.append({
                "title": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "summary": item.get("summary", item.get("title", ""))[:300],
                "published_at": (
                    datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                    if ts else ""
                ),
                "link": item.get("link", ""),
                "sentiment_label": "neutral",
            })

        raw_articles = _label_sentiment(raw_articles)
        return _build_result(ticker, raw_articles, source="yahoo_finance")

    except NETWORK_ERRORS as e:
        _log.debug("YF news fetch failed for %s: %s", ticker, e)
        if _news_breaker:
            _news_breaker.record_failure()
        return _fallback(ticker)


# ==================== Alpha Vantage ====================

def _fetch_av_news(ticker: str, api_key: str, max_articles: int = 10) -> Dict:
    """通过 Alpha Vantage NEWS_SENTIMENT API 获取新闻（含预处理情绪分）"""
    # 熔断检查
    # v0.45.444：每个没拿到数据的出口都带上 `primary_status`（原先一律 `_fallback`，事后分不清是哪一种）。
    # 只改记录，不改去向——每个出口照旧降级 Yahoo，news 通道取值与改动前逐字节相同。
    if _news_breaker and not _news_breaker.allow_request():
        _log.warning("newsapi 熔断中，跳过 AV 请求 (%s)", ticker)
        return _primary_fail(ticker, "breaker_open")

    # 每日配额检查（本地计数，见 `_av_quota_ok`；与 AV 服务端的额度窗口不是一回事）
    if not _av_quota_ok():
        _log.warning("AV 每日配额已耗尽 (%d/%d)，降级到 Yahoo Finance (%s)",
                     _av_daily["count"], _AV_DAILY_LIMIT, ticker)
        return _primary_fail(ticker, "local_quota")

    try:
        if _news_limiter and not _news_limiter.acquire(timeout=10):
            _log.warning("newsapi 限流超时，跳过 AV 请求 (%s)", ticker)
            return _primary_fail(ticker, "limiter_timeout")
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": ticker,
            "sort": "LATEST",
            "limit": max_articles,
            "apikey": api_key,
        }
        _session = get_session("newsapi") if _RESILIENCE_OK else _req
        resp = _session.get(_AV_NEWS_URL, params=params, timeout=10)
        if not resp.ok:
            if _news_breaker:
                _news_breaker.record_failure()
            return _primary_fail(ticker, "http_error", f"HTTP {resp.status_code}")

        data = resp.json()
        # AV 限速/错误响应检测（v0.45.444 补 "Error Message"：AV 报错用的是这个键，原先落到下面的空 feed 分支，
        # 去向同样是降级，只是记成了 empty_feed）
        if "Information" in data or "Note" in data or "Error" in data or "Error Message" in data:
            _msg = _av_refusal_text(data, api_key)
            # ⚠️ 前缀保持原样：排查文档 / 历史日志都按「AV API rate-limited or error for」grep
            _log.warning("AV API rate-limited or error for %s: %s | %s",
                         ticker, list(data.keys()), _msg or "（无原文）")
            return _primary_fail(ticker, "server_refused", _msg)

        feed = data.get("feed", [])
        if not feed:
            return _primary_fail(ticker, "empty_feed")

        raw_articles = []
        for item in feed[:max_articles]:
            # 提取当前 ticker 的情绪评分（AV 按 ticker 分别提供）
            ticker_sent = next(
                (s for s in item.get("ticker_sentiment", [])
                 if s.get("ticker") == ticker),
                {}
            )
            try:
                sent_val = float(ticker_sent.get("ticker_sentiment_score", "0"))
            except (ValueError, TypeError):
                sent_val = 0.0

            # AV 官方阈值判断
            if math.isnan(sent_val) or math.isinf(sent_val):
                sent_val = 0.0
            if sent_val > _AV_BULL_THRESHOLD:
                label = "bullish"
            elif sent_val < _AV_BEAR_THRESHOLD:
                label = "bearish"
            else:
                label = "neutral"

            raw_articles.append({
                "title": item.get("title", ""),
                "publisher": item.get("source", ""),
                "summary": item.get("summary", "")[:300],
                "published_at": item.get("time_published", "")[:16],
                "link": item.get("url", ""),
                "sentiment_label": label,
                "sentiment_score_raw": round(sent_val, 4),
            })

        if _news_breaker:
            _news_breaker.record_success()
        return _build_result(ticker, raw_articles, source="alpha_vantage")

    except NETWORK_ERRORS as e:
        _log.debug("AV news fetch failed for %s: %s", ticker, _redact(e, api_key))
        if _news_breaker:
            _news_breaker.record_failure()
        # 异常文本里可能带完整 URL（含 apikey 参数）⇒ 去 key 后才落进记录
        return _primary_fail(ticker, "network_error", _redact(f"{type(e).__name__}: {e}", api_key))


# ==================== Massive（原 Polygon.io）====================

_MASSIVE_NEWS_URL = "https://api.massive.com/v2/reference/news"
#: insights 的逐票情绪 → 本模块的三档标签（与 AV 的 ±0.15 阈值、Yahoo 的关键词打标走同一个 `_build_result`）
_MASSIVE_LABELS = {"positive": "bullish", "negative": "bearish", "neutral": "neutral"}


def _massive_symbol(ticker: str) -> str:
    """Massive 的份额类写点号：BRK-B → BRK.B（2026-10-08 实测 10 篇；`-` 写法未测）。"""
    return ticker.replace("-", ".")


def _fetch_massive_news(ticker: str, api_key: str, max_articles: int = 10) -> Dict:
    """Massive `/v2/reference/news`：每篇文章带 `insights`（对它提到的每只票一条 positive/negative/neutral + 理由）。

    v0.45.445 起是新闻主源。与 AV 路径同一套出口状态（`_primary_fail`），每个出口照旧降级 Yahoo。
    key 走 `Authorization: Bearer` 头而不放 URL——异常文本 / 日志里不会出现 key。
    文章没有本票的 insight ⇒ 记 neutral，并计入结果的 `massive_no_insight`（2026-10-08 校准 300 篇里 0 篇）。
    `published_at` 与 AV / Yahoo 同键名：三个源在 `_build_result` 里走完全相同的算式。
    """
    cfg = _news_cfg()
    if not _massive_limiter.acquire(timeout=float(cfg["massive_acquire_timeout_s"])):
        _log.warning("Massive 限速名额等待超时，降级到 Yahoo Finance (%s)", ticker)
        return _primary_fail(ticker, "limiter_timeout")
    sym = _massive_symbol(ticker)
    params = {"ticker": sym, "limit": max_articles, "order": "desc", "sort": "published_utc"}
    try:
        _session = get_session("newsapi") if _RESILIENCE_OK else _req
        resp = _session.get(_MASSIVE_NEWS_URL, params=params,
                            headers={"Authorization": f"Bearer {api_key}"},
                            timeout=float(cfg["massive_request_timeout_s"]))
    except NETWORK_ERRORS as e:
        _log.warning("Massive news fetch failed for %s: %s", ticker, _redact(f"{type(e).__name__}: {e}", api_key))
        return _primary_fail(ticker, "network_error", _redact(f"{type(e).__name__}: {e}", api_key))
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code == 429 or (isinstance(data, dict) and data.get("status") not in (None, "OK", "DELAYED")):
        msg = _massive_refusal_text(data, api_key) or f"HTTP {resp.status_code}"
        _log.warning("Massive news refused for %s: HTTP %s | %s", ticker, resp.status_code, msg)
        return _primary_fail(ticker, "server_refused", msg)
    if not resp.ok or not isinstance(data, dict):
        return _primary_fail(ticker, "http_error", f"HTTP {resp.status_code}")
    results = data.get("results") or []
    if not results:
        return _primary_fail(ticker, "empty_feed")

    raw_articles = []
    no_insight = 0
    for item in results[:max_articles]:
        ins = next((x for x in (item.get("insights") or [])
                    if isinstance(x, dict) and x.get("ticker") in (sym, ticker)), None)
        if ins is None:
            no_insight += 1
        raw_articles.append({
            "title": item.get("title", ""),
            "publisher": (item.get("publisher") or {}).get("name", ""),
            "summary": (item.get("description") or "")[:300],
            "published_at": (item.get("published_utc") or "")[:16],
            "link": item.get("article_url", ""),
            "sentiment_label": _MASSIVE_LABELS.get((ins or {}).get("sentiment"), "neutral"),
            "sentiment_score_raw": None,
        })
    result = _build_result(ticker, raw_articles, source="massive")
    result["massive_no_insight"] = no_insight
    return result


# ==================== 情绪标注 ====================

def _label_sentiment(articles: List[Dict]) -> List[Dict]:
    """基于关键词为 Yahoo Finance 文章打情绪标签"""
    for art in articles:
        text = (art.get("title", "") + " " + art.get("summary", "")).lower()
        words = set(text.split())
        bull = len(words & _BULLISH_KWS)
        bear = len(words & _BEARISH_KWS)
        if bull > bear:
            art["sentiment_label"] = "bullish"
        elif bear > bull:
            art["sentiment_label"] = "bearish"
        # else: 保持 neutral
    return articles


# ==================== 噪声过滤：去重 + 时效衰减 ====================

try:
    from config import NEWS_FILTER_CONFIG as _NF_CFG
except ImportError:
    _NF_CFG = {"dedup_jaccard_threshold": 0.5, "recency_half_life_hours": 24.0, "min_articles_for_recency": 3}


def _deduplicate_articles(articles: List[Dict], threshold: float | None = None) -> List[Dict]:
    """
    基于标题 Jaccard 相似度去重。
    同组重复中优先保留有明确情绪标签（非 neutral）的文章。
    """
    if threshold is None:
        threshold = _NF_CFG.get("dedup_jaccard_threshold", 0.5)
    seen: List[set] = []
    result: List[Dict] = []
    for art in articles:
        title_words = set(art.get("title", "").lower().split())
        if not title_words:
            result.append(art)
            continue
        is_dup = False
        for prev_words in seen:
            intersection = len(title_words & prev_words)
            union = len(title_words | prev_words)
            if union > 0 and intersection / union >= threshold:
                is_dup = True
                break
        if not is_dup:
            seen.append(title_words)
            result.append(art)
    if len(result) < len(articles):
        _log.debug("噪声过滤: 去重 %d → %d 篇", len(articles), len(result))
    return result


def _recency_weight(published_str: str, half_life_hours: float | None = None) -> float:
    """
    指数衰减权重：半衰期默认 24 小时。
    刚发布 → ~1.0，24h 前 → ~0.5，72h 前 → ~0.125。
    """
    if half_life_hours is None:
        half_life_hours = _NF_CFG.get("recency_half_life_hours", 24.0)
    try:
        pub_str = published_str.replace("Z", "+00:00")
        pub = datetime.fromisoformat(pub_str)
        now = datetime.now(pub.tzinfo) if pub.tzinfo else datetime.now()
        age_hours = max(0.0, (now - pub).total_seconds() / 3600.0)
        return 0.5 ** (age_hours / half_life_hours)
    except Exception:
        # v0.45.54：0.5 **恰好等于半衰期点的权重** —— 与一篇真的
        # 24 小时前的新闻不可区分。解析不了发布时间就不该参与时效加权，
        # 返回 None 由调用方剔除该条。
        return None


# ==================== 结果构建 + DataQualityChecker ====================

def _build_result(ticker: str, raw_articles: List[Dict], source: str) -> Dict:
    """
    构建标准输出格式，内嵌 DataQualityChecker 清洗：
    1. 清洗每篇文章（sentiment_label 合法化，截断超长字段）
    2. 重新计算计数（防止外部传入的数值不一致）
    3. 清洗 sentiment_score（clamp 到 1.0-10.0，处理 NaN/Inf）
    4. 记录所有 issues 到 data_quality 字段
    """
    if not raw_articles:
        return _fallback(ticker)

    # ── Step 1: 清洗文章列表 ──
    articles, dq_issues = _clean_articles(raw_articles)
    if not articles:
        return _fallback(ticker)

    # ── Step 1.5: 噪声过滤 — 标题去重 ──
    raw_count = len(articles)
    articles = _deduplicate_articles(articles)
    if len(articles) < raw_count:
        dq_issues.append(f"去重: {raw_count} → {len(articles)} 篇")

    # ── Step 2: 从清洗后的标签重新计数（防止标签被改后计数不一致）──
    bullish = sum(1 for a in articles if a["sentiment_label"] == "bullish")
    bearish = sum(1 for a in articles if a["sentiment_label"] == "bearish")
    neutral = len(articles) - bullish - bearish
    total = len(articles)

    # 一致性检查（理论上此处不会触发，但留作防御）
    dq_issues.extend(_check_count_consistency(bullish, bearish, neutral, total))

    # ── Step 3: 计算并清洗情绪分（时效加权）──
    min_for_recency = _NF_CFG.get("min_articles_for_recency", 3)
    if total >= min_for_recency:
        # 时效加权：新文章权重高，旧文章权重低
        # v0.45.54：_recency_weight 现在对「发布时间解析不了」返回 None
        # （旧实现返回 0.5，恰好等于半衰期点的权重，与真的 24 小时前的新闻
        # 不可区分）。这类文章从时效加权里剔除，而不是按中等权重计入。
        def _w(label):
            ws = [w for a in articles if a["sentiment_label"] == label
                  for w in (_recency_weight(a.get("published", "")),)
                  if w is not None]
            return sum(ws), len(ws)

        w_bull, _n_b = _w("bullish")
        w_bear, _n_r = _w("bearish")
        w_neut, _n_n = _w("neutral")
        _n_weighted = _n_b + _n_r + _n_n
        if _n_weighted < total:
            _log.debug("时效加权：%d/%d 篇发布时间不可解析，已从加权中剔除",
                       total - _n_weighted, total)
        w_total = w_bull + w_bear + w_neut
        if w_total > 0 and _n_weighted >= min_for_recency:
            bull_ratio = w_bull / w_total
        else:
            # 可加权样本不足 ⇒ 退回简单计数，而不是给 0.5「多空各半」
            bull_ratio = bullish / total if total > 0 else 0.5
    else:
        # 文章太少时用简单计数（避免单篇文章时效权重失真）
        bull_ratio = bullish / total if total > 0 else 0.5
    raw_score = 1.0 + bull_ratio * 9.0          # 1.0 ~ 10.0
    sentiment_score = _clean_sentiment_score(raw_score)

    if math.isnan(raw_score) or math.isinf(raw_score):
        dq_issues.append(f"raw sentiment_score={raw_score} → 已修正为 {sentiment_score}")

    # ── Step 4: 主题判断 ──
    if bullish > bearish:
        dominant = "看多叙事主导"
    elif bearish > bullish:
        dominant = "看空叙事主导"
    else:
        dominant = "叙事分歧"

    # ── Step 5: 汇总 data_quality ──
    cleaned_fields = []
    if dq_issues:
        cleaned_fields = [i for i in dq_issues if "→" in i]
        _log.debug("NewsAPI DQ issues for %s: %s", ticker, dq_issues)

    return {
        "ticker": ticker,
        "articles": articles,
        "total_articles": total,
        "raw_article_count": raw_count,       # 去重前数量（噪声过滤指标）
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "sentiment_score": sentiment_score,
        "dominant_theme": dominant,
        "source": source,
        "is_real_data": True,
        "data_quality": {
            "issues": dq_issues,
            "cleaned_fields": cleaned_fields,
            "passed": len(dq_issues) == 0,
        },
        "timestamp": datetime.now().isoformat(),
    }


def _fallback(ticker: str) -> Dict:
    return {
        "ticker": ticker,
        "articles": [],
        "total_articles": 0,
        "bullish_count": 0,
        "bearish_count": 0,
        "neutral_count": 0,
        "sentiment_score": 5.0,
        "dominant_theme": "数据不可用",
        "source": "fallback",
        "is_real_data": False,
        "data_quality": {"issues": ["数据源不可用"], "passed": False},
        "timestamp": datetime.now().isoformat(),
    }
