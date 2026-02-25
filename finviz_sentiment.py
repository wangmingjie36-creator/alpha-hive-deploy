"""
Finviz 新闻情绪分析模块

从 Finviz 抓取新闻标题，通过关键词匹配进行情绪打分：
- 覆盖主流财经媒体（Reuters, Bloomberg, CNBC, Barrons 等）
- 支持多标的批量分析
- 15 分钟缓存

替代 X/Twitter 的免费情绪信号源。
"""

import json
import logging as _logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from hive_logger import atomic_json_write

_log = _logging.getLogger("alpha_hive.finviz_sentiment")

try:
    import requests
except ImportError:
    requests = None

CACHE_DIR = Path(__file__).parent / "finviz_cache"
CACHE_DIR.mkdir(exist_ok=True)

# 情绪关键词
BULLISH_WORDS = [
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
    "upgrade", "upgrades", "bullish", "outperform", "breakout", "record high",
    "buy", "strong buy", "raise", "raises", "boost", "boosted",
    "positive", "optimistic", "growth", "expansion", "momentum",
    "accelerate", "top pick", "upside", "higher", "gain", "gains",
    "exceed", "exceeded", "blowout", "impressive", "robust",
]

BEARISH_WORDS = [
    "miss", "misses", "fall", "falls", "drop", "drops", "crash", "crashes",
    "downgrade", "downgrades", "bearish", "underperform", "breakdown",
    "sell", "selloff", "cut", "cuts", "slash", "slashed",
    "negative", "pessimistic", "decline", "contraction", "headwind",
    "decelerate", "risk", "warning", "lower", "loss", "losses",
    "disappoint", "weak", "lawsuit", "probe", "investigation", "recall",
]


class FinvizSentimentClient:
    """Finviz 新闻情绪客户端"""

    def __init__(self):
        self._last_request = 0.0

    def _throttle(self):
        now = time.time()
        elapsed = now - self._last_request
        if elapsed < 2.0:  # Finviz 限流严格，2 秒间隔
            time.sleep(2.0 - elapsed)
        self._last_request = time.time()

    def get_news_titles(self, ticker: str, max_titles: int = 30) -> List[str]:
        """抓取 Finviz 新闻标题"""
        cache_path = CACHE_DIR / f"{ticker.upper()}_titles.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 900:  # 15 分钟缓存
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as exc:
                    _log.debug("Finviz titles 缓存读取失败 (%s): %s", ticker, exc)

        if requests is None:
            return []

        try:
            self._throttle()
            resp = requests.get(
                f"https://finviz.com/quote.ashx?t={ticker.upper()}&ty=c&p=d&b=1",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                timeout=15,
            )
            if resp.status_code != 200:
                return []

            text = resp.text
            start = text.find('id="news-table"')
            if start == -1:
                return []

            end = text.find("</table>", start)
            news_html = text[start:end]

            titles = re.findall(r'class="tab-link-news"[^>]*>([^<]+)<', news_html)
            titles = [t.strip() for t in titles if t.strip()][:max_titles]

            try:
                atomic_json_write(cache_path, titles)
            except (OSError, TypeError) as exc:
                _log.debug("Finviz titles 缓存写入失败 (%s): %s", ticker, exc)

            return titles

        except (ConnectionError, TimeoutError, OSError, ValueError) as e:
            _log.warning("Finviz 新闻抓取失败 (%s): %s", ticker, e)
            return []

    def analyze_sentiment(self, ticker: str) -> Dict:
        """
        分析指定标的的新闻情绪

        返回: {
            ticker, total_titles, bullish_count, bearish_count, neutral_count,
            sentiment_ratio, news_score: 0-10,
            news_signal: str, top_bullish: [], top_bearish: []
        }
        """
        cache_path = CACHE_DIR / f"{ticker.upper()}_sentiment.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 900:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as exc:
                    _log.debug("Finviz sentiment 缓存读取失败 (%s): %s", ticker, exc)

        titles = self.get_news_titles(ticker)
        if not titles:
            return self._default_result(ticker)

        bullish_count = 0
        bearish_count = 0
        top_bullish = []
        top_bearish = []

        for title in titles:
            title_lower = title.lower()
            b_score = sum(1 for w in BULLISH_WORDS if w in title_lower)
            s_score = sum(1 for w in BEARISH_WORDS if w in title_lower)

            if b_score > s_score:
                bullish_count += 1
                if len(top_bullish) < 3:
                    top_bullish.append(title[:80])
            elif s_score > b_score:
                bearish_count += 1
                if len(top_bearish) < 3:
                    top_bearish.append(title[:80])

        neutral_count = len(titles) - bullish_count - bearish_count

        # 情绪比率 (-1 到 +1)
        if bullish_count + bearish_count > 0:
            sentiment_ratio = (bullish_count - bearish_count) / (bullish_count + bearish_count)
        else:
            sentiment_ratio = 0.0

        # 评分 (0-10)
        score = 5.0 + sentiment_ratio * 3.0  # [-3, +3] 范围调整
        # 新闻数量加分（更多新闻 = 更多关注）
        if len(titles) >= 20:
            score += 0.5
        elif len(titles) <= 5:
            score -= 0.5
        score = max(1.0, min(10.0, score))

        if sentiment_ratio > 0.3:
            signal = f"新闻偏多（{bullish_count}多/{bearish_count}空）"
        elif sentiment_ratio < -0.3:
            signal = f"新闻偏空（{bullish_count}多/{bearish_count}空）"
        else:
            signal = f"新闻中性（{bullish_count}多/{bearish_count}空/{neutral_count}中）"

        result = {
            "ticker": ticker.upper(),
            "total_titles": len(titles),
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "neutral_count": neutral_count,
            "sentiment_ratio": round(sentiment_ratio, 3),
            "news_score": round(score, 1),
            "news_signal": signal,
            "top_bullish": top_bullish,
            "top_bearish": top_bearish,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            atomic_json_write(cache_path, result)
        except (OSError, TypeError) as exc:
            _log.debug("Finviz sentiment 缓存写入失败 (%s): %s", ticker, exc)

        return result

    def _default_result(self, ticker: str) -> Dict:
        return {
            "ticker": ticker.upper(),
            "total_titles": 0,
            "bullish_count": 0, "bearish_count": 0, "neutral_count": 0,
            "sentiment_ratio": 0.0,
            "news_score": 5.0,
            "news_signal": "无新闻数据",
            "top_bullish": [], "top_bearish": [],
            "timestamp": datetime.now().isoformat(),
        }


# ==================== 便捷函数 ====================

_client: Optional[FinvizSentimentClient] = None
_client_lock = threading.Lock()


def get_finviz_sentiment(ticker: str) -> Dict:
    """便捷函数：获取 Finviz 新闻情绪"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = FinvizSentimentClient()
    return _client.analyze_sentiment(ticker)
