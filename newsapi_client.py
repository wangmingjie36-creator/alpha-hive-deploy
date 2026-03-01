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

import json
import logging as _logging
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from hive_logger import PATHS, atomic_json_write

_log = _logging.getLogger("alpha_hive.newsapi")

try:
    import requests as _req
except ImportError:
    _req = None

# 导入清洗工具（与 Agent 层保持一致）
try:
    from models import clean_score
    _MODELS_OK = True
except ImportError:
    _MODELS_OK = False

_CACHE_DIR = Path(PATHS.home) / "cache" / "news"
_CACHE_TTL = 1800   # 30 分钟（AV 每天 25 次，不能太频繁）
_lock = threading.Lock()

_YF_NEWS_URL = "https://query2.finance.yahoo.com/v1/finance/search"
_AV_NEWS_URL = "https://www.alphavantage.co/query"
_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
}

# AV 情绪阈值（官方文档：>0.15 bullish, <-0.15 bearish）
_AV_BULL_THRESHOLD = 0.15
_AV_BEAR_THRESHOLD = -0.15

# 情绪关键词集合（用于 Yahoo Finance 标注）
_BULLISH_KWS = {
    "surge", "soar", "rally", "beat", "record", "upgrade", "buy", "growth",
    "profit", "expand", "win", "strong", "bullish", "upbeat", "exceeds",
    "outperform", "positive", "breakthrough", "gains", "rises", "jumped",
    "climbed", "boosted", "optimistic", "raised", "accelerates", "momentum",
}
_BEARISH_KWS = {
    "drop", "fall", "miss", "downgrade", "sell", "loss", "weak", "decline",
    "cut", "warning", "layoff", "recession", "bearish", "disappoints",
    "underperform", "negative", "crash", "plunge", "fell", "tumbled",
    "slumped", "fears", "risk", "concern", "lowered", "slowdown", "probe",
}

_VALID_LABELS = {"bullish", "bearish", "neutral"}


# ==================== API Key 加载 ====================

def _load_av_key() -> Optional[str]:
    """加载 AV API Key：环境变量 > ~/.alpha_hive_av_key 文件"""
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

    # 1. 优先 Alpha Vantage（有 Key 时质量最高，含逐文章情绪分）
    av_key = _load_av_key()
    if av_key and _req is not None:
        result = _fetch_av_news(ticker, av_key, max_articles)
        if result.get("is_real_data"):
            _safe_cache(cache_path, result)
            return result

    # 2. Yahoo Finance 免费备用
    result = _fetch_yf_news(ticker, max_articles)
    _safe_cache(cache_path, result)
    return result


def _safe_cache(path: Path, data: Dict):
    try:
        atomic_json_write(path, data)
    except (OSError, TypeError):
        pass


# ==================== Yahoo Finance ====================

def _fetch_yf_news(ticker: str, max_articles: int = 10) -> Dict:
    """通过 Yahoo Finance 搜索 API 获取新闻"""
    if _req is None:
        return _fallback(ticker)

    try:
        params = {
            "q": ticker,
            "newsCount": max_articles,
            "enableFuzzyQuery": "false",
            "enableEnhancedTrivialQuery": "true",
        }
        resp = _req.get(_YF_NEWS_URL, headers=_YF_HEADERS, params=params, timeout=8)
        if not resp.ok:
            return _fallback(ticker)

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

    except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
        _log.debug("YF news fetch failed for %s: %s", ticker, e)
        return _fallback(ticker)


# ==================== Alpha Vantage ====================

def _fetch_av_news(ticker: str, api_key: str, max_articles: int = 10) -> Dict:
    """通过 Alpha Vantage NEWS_SENTIMENT API 获取新闻（含预处理情绪分）"""
    try:
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": ticker,
            "sort": "LATEST",
            "limit": max_articles,
            "apikey": api_key,
        }
        resp = _req.get(_AV_NEWS_URL, params=params, timeout=10)
        if not resp.ok:
            return _fallback(ticker)

        data = resp.json()
        # AV 限速/错误响应检测
        if "Information" in data or "Note" in data or "Error" in data:
            _log.debug("AV API rate-limited or error for %s: %s",
                       ticker, list(data.keys()))
            return _fallback(ticker)

        feed = data.get("feed", [])
        if not feed:
            return _fallback(ticker)

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

        return _build_result(ticker, raw_articles, source="alpha_vantage")

    except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
        _log.debug("AV news fetch failed for %s: %s", ticker, e)
        return _fallback(ticker)


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

    # ── Step 2: 从清洗后的标签重新计数（防止标签被改后计数不一致）──
    bullish = sum(1 for a in articles if a["sentiment_label"] == "bullish")
    bearish = sum(1 for a in articles if a["sentiment_label"] == "bearish")
    neutral = len(articles) - bullish - bearish
    total = len(articles)

    # 一致性检查（理论上此处不会触发，但留作防御）
    dq_issues.extend(_check_count_consistency(bullish, bearish, neutral, total))

    # ── Step 3: 计算并清洗情绪分 ──
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
