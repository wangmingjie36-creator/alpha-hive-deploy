"""
Tests for newsapi_client module -- Yahoo Finance + Alpha Vantage news fetching
with sentiment labeling, caching, deduplication, and DataQualityChecker.
"""

import json
import math
import types
import time
import pytest
from unittest.mock import MagicMock


# ==================== Sample API responses ====================

SAMPLE_YF_RESPONSE = {
    "news": [
        {
            "title": "NVDA stock surges on AI demand",
            "publisher": "Reuters",
            "summary": "NVIDIA reported strong growth",
            "providerPublishTime": 1709900000,
            "link": "https://example.com/1",
        },
        {
            "title": "NVDA announces new chip",
            "publisher": "Bloomberg",
            "summary": "New GPU architecture for data centers",
            "providerPublishTime": 1709890000,
            "link": "https://example.com/2",
        },
        {
            "title": "Market decline amid recession fears",
            "publisher": "CNBC",
            "summary": "Stocks fall as investors sell off risky assets",
            "providerPublishTime": 1709880000,
            "link": "https://example.com/3",
        },
    ]
}

SAMPLE_AV_RESPONSE = {
    "feed": [
        {
            "title": "NVDA beats earnings",
            "source": "Bloomberg",
            "summary": "Strong quarter for NVIDIA with record revenue",
            "time_published": "20260308T103000",
            "url": "https://example.com/av1",
            "ticker_sentiment": [
                {"ticker": "NVDA", "ticker_sentiment_score": "0.35"},
            ],
        },
        {
            "title": "NVDA faces headwinds in China",
            "source": "Reuters",
            "summary": "Export restrictions may impact revenue growth",
            "time_published": "20260308T090000",
            "url": "https://example.com/av2",
            "ticker_sentiment": [
                {"ticker": "NVDA", "ticker_sentiment_score": "-0.25"},
            ],
        },
        {
            "title": "NVDA holds steady",
            "source": "WSJ",
            "summary": "Shares unchanged amid mixed signals",
            "time_published": "20260307T160000",
            "url": "https://example.com/av3",
            "ticker_sentiment": [
                {"ticker": "NVDA", "ticker_sentiment_score": "0.05"},
            ],
        },
    ]
}


# ==================== autouse fixture ====================

@pytest.fixture(autouse=True)
def _isolate_newsapi(tmp_path, monkeypatch):
    """
    Redirect _CACHE_DIR to tmp_path, reset circuit breaker, reset AV daily
    quota counter, and mock _load_av_key to return None (prevent AV calls).
    """
    import newsapi_client

    # Redirect cache dir to tmp_path
    cache_dir = tmp_path / "news_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(newsapi_client, "_CACHE_DIR", cache_dir)

    # Reset circuit breaker if present
    if newsapi_client._news_breaker is not None:
        newsapi_client._news_breaker.reset()

    # Reset AV daily quota
    newsapi_client._av_daily["count"] = 0
    newsapi_client._av_daily["date"] = ""

    # Mock _load_av_key to return None by default (prevent real AV calls)
    monkeypatch.setattr(newsapi_client, "_load_av_key", lambda: None)

    yield

    # Teardown: reset quota again
    newsapi_client._av_daily["count"] = 0
    newsapi_client._av_daily["date"] = ""


# ==================== helpers ====================

def _make_response(json_data, ok=True, status_code=200):
    """Build a SimpleNamespace that mimics requests.Response."""
    resp = types.SimpleNamespace()
    resp.ok = ok
    resp.status_code = status_code
    resp.json = lambda: json_data
    resp.raise_for_status = lambda: None
    return resp


def _mock_session(monkeypatch, response):
    """Patch get_session to return a mock session with .get() returning *response*.
    Returns a call tracker dict."""
    call_tracker = {"n": 0, "urls": []}

    def fake_get(url, **kwargs):
        call_tracker["n"] += 1
        call_tracker["urls"].append(url)
        return response

    session = types.SimpleNamespace(get=fake_get)

    import newsapi_client
    if newsapi_client._RESILIENCE_OK:
        monkeypatch.setattr(newsapi_client, "get_session", lambda source: session)
    else:
        monkeypatch.setattr(newsapi_client, "_req", session)

    return call_tracker


def _noop_limiter(monkeypatch):
    """Replace _news_limiter with a no-op that always returns True."""
    import newsapi_client
    monkeypatch.setattr(
        newsapi_client, "_news_limiter",
        types.SimpleNamespace(acquire=lambda **kw: True),
    )


# ==================== TestLabelSentiment ====================

class TestLabelSentiment:
    """Tests for _label_sentiment keyword-based labeling."""

    def test_bullish_keywords(self):
        """Articles with bullish keywords get labeled bullish."""
        from newsapi_client import _label_sentiment
        articles = [
            {"title": "Stock surges on strong growth", "summary": "Revenue beat expectations", "sentiment_label": "neutral"},
        ]
        result = _label_sentiment(articles)
        assert result[0]["sentiment_label"] == "bullish"

    def test_bearish_keywords(self):
        """Articles with bearish keywords get labeled bearish."""
        from newsapi_client import _label_sentiment
        articles = [
            {"title": "Stock falls on weak earnings", "summary": "Revenue decline and loss reported", "sentiment_label": "neutral"},
        ]
        result = _label_sentiment(articles)
        assert result[0]["sentiment_label"] == "bearish"

    def test_neutral_when_balanced(self):
        """Articles with no sentiment keywords stay neutral."""
        from newsapi_client import _label_sentiment
        articles = [
            {"title": "Company announces quarterly results", "summary": "Management discussed strategy", "sentiment_label": "neutral"},
        ]
        result = _label_sentiment(articles)
        assert result[0]["sentiment_label"] == "neutral"


# ==================== TestFetchNews ====================

class TestFetchNews:
    """Tests for _fetch_yf_news and _fetch_av_news internal functions."""

    def test_yf_success(self, monkeypatch):
        """_fetch_yf_news returns real data with proper structure on success."""
        _noop_limiter(monkeypatch)
        resp = _make_response(SAMPLE_YF_RESPONSE)
        _mock_session(monkeypatch, resp)

        from newsapi_client import _fetch_yf_news
        result = _fetch_yf_news("NVDA", max_articles=10)

        assert result["ticker"] == "NVDA"
        assert result["is_real_data"] is True
        assert result["source"] == "yahoo_finance"
        assert result["total_articles"] > 0
        assert len(result["articles"]) <= 10

    def test_yf_http_failure_returns_fallback(self, monkeypatch):
        """_fetch_yf_news returns fallback when HTTP response is not ok."""
        _noop_limiter(monkeypatch)
        resp = _make_response({}, ok=False, status_code=500)
        _mock_session(monkeypatch, resp)

        from newsapi_client import _fetch_yf_news
        result = _fetch_yf_news("NVDA")

        assert result["is_real_data"] is False
        assert result["source"] == "fallback"
        assert result["sentiment_score"] == 5.0

    def test_yf_network_error_returns_fallback(self, monkeypatch):
        """_fetch_yf_news returns fallback on network exception."""
        _noop_limiter(monkeypatch)

        def raise_error(url, **kwargs):
            raise ConnectionError("mock network failure")

        session = types.SimpleNamespace(get=raise_error)
        import newsapi_client
        if newsapi_client._RESILIENCE_OK:
            monkeypatch.setattr(newsapi_client, "get_session", lambda source: session)
        else:
            monkeypatch.setattr(newsapi_client, "_req", session)

        result = newsapi_client._fetch_yf_news("NVDA")
        assert result["is_real_data"] is False
        assert result["source"] == "fallback"

    def test_av_success(self, monkeypatch):
        """_fetch_av_news returns real data with AV sentiment labels."""
        _noop_limiter(monkeypatch)
        resp = _make_response(SAMPLE_AV_RESPONSE)
        _mock_session(monkeypatch, resp)

        from newsapi_client import _fetch_av_news
        result = _fetch_av_news("NVDA", "test-api-key", max_articles=10)

        assert result["ticker"] == "NVDA"
        assert result["is_real_data"] is True
        assert result["source"] == "alpha_vantage"
        assert result["total_articles"] == 3
        # First article has score 0.35 > 0.15 threshold -> bullish
        labels = [a["sentiment_label"] for a in result["articles"]]
        assert "bullish" in labels
        # Second article has score -0.25 < -0.15 threshold -> bearish
        assert "bearish" in labels

    def test_av_quota_exhausted_returns_fallback(self, monkeypatch):
        """_fetch_av_news returns fallback when daily quota is exhausted."""
        _noop_limiter(monkeypatch)
        import newsapi_client
        # Exhaust quota
        newsapi_client._av_daily["date"] = time.strftime("%Y-%m-%d")
        newsapi_client._av_daily["count"] = newsapi_client._AV_DAILY_LIMIT

        result = newsapi_client._fetch_av_news("NVDA", "test-key", max_articles=5)
        assert result["is_real_data"] is False
        assert result["source"] == "fallback"


# ==================== TestGetTickerNews ====================

class TestGetTickerNews:
    """Tests for the main get_ticker_news entry point."""

    def test_returns_yf_when_no_av_key(self, monkeypatch):
        """Without AV key, get_ticker_news uses Yahoo Finance."""
        _noop_limiter(monkeypatch)
        resp = _make_response(SAMPLE_YF_RESPONSE)
        _mock_session(monkeypatch, resp)

        from newsapi_client import get_ticker_news
        result = get_ticker_news("NVDA")

        assert result["ticker"] == "NVDA"
        assert result["source"] == "yahoo_finance"
        assert result["is_real_data"] is True

    def test_returns_av_when_key_present(self, monkeypatch):
        """With AV key, get_ticker_news uses Alpha Vantage first."""
        _noop_limiter(monkeypatch)
        import newsapi_client
        monkeypatch.setattr(newsapi_client, "_load_av_key", lambda: "test-key")

        resp = _make_response(SAMPLE_AV_RESPONSE)
        _mock_session(monkeypatch, resp)

        result = newsapi_client.get_ticker_news("NVDA")
        assert result["source"] == "alpha_vantage"
        assert result["is_real_data"] is True

    def test_cache_prevents_refetch(self, monkeypatch):
        """Second call within TTL uses cached result; no additional HTTP."""
        _noop_limiter(monkeypatch)
        resp = _make_response(SAMPLE_YF_RESPONSE)
        tracker = _mock_session(monkeypatch, resp)

        from newsapi_client import get_ticker_news
        r1 = get_ticker_news("AAPL")
        first_calls = tracker["n"]

        r2 = get_ticker_news("AAPL")
        second_calls = tracker["n"]

        assert r1["ticker"] == "AAPL"
        assert r2["ticker"] == "AAPL"
        # Second call should NOT make additional HTTP requests
        assert second_calls == first_calls

    def test_fallback_when_yf_returns_empty(self, monkeypatch):
        """When YF returns no news items, result is fallback."""
        _noop_limiter(monkeypatch)
        resp = _make_response({"news": []})
        _mock_session(monkeypatch, resp)

        from newsapi_client import get_ticker_news
        result = get_ticker_news("ZZZZ")

        assert result["is_real_data"] is False
        assert result["source"] == "fallback"
        assert result["total_articles"] == 0


# ==================== TestNewsAPIClient ====================

class TestNewsAPIClient:
    """Tests for helper functions, data quality, and edge cases."""

    def test_clean_sentiment_score_clamps_to_range(self):
        """_clean_sentiment_score always returns value in [1.0, 10.0]."""
        from newsapi_client import _clean_sentiment_score
        assert _clean_sentiment_score(0.0) == 1.0
        assert _clean_sentiment_score(-5.0) == 1.0
        assert _clean_sentiment_score(15.0) == 10.0
        assert _clean_sentiment_score(5.0) == 5.0
        assert _clean_sentiment_score(None) >= 1.0
        assert _clean_sentiment_score(float("nan")) >= 1.0
        assert _clean_sentiment_score(float("inf")) <= 10.0

    def test_clean_label_validates(self):
        """_clean_label returns valid labels or defaults to neutral."""
        from newsapi_client import _clean_label
        assert _clean_label("bullish") == "bullish"
        assert _clean_label("BEARISH") == "bearish"
        assert _clean_label("neutral") == "neutral"
        assert _clean_label("invalid") == "neutral"
        assert _clean_label("") == "neutral"
        assert _clean_label(None) == "neutral"

    def test_clean_articles_filters_empty_titles(self):
        """_clean_articles removes articles with empty titles."""
        from newsapi_client import _clean_articles
        articles = [
            {"title": "Valid title", "summary": "text", "sentiment_label": "bullish"},
            {"title": "", "summary": "no title", "sentiment_label": "neutral"},
            {"title": "   ", "summary": "whitespace title", "sentiment_label": "bearish"},
        ]
        cleaned, issues = _clean_articles(articles)
        assert len(cleaned) == 1
        assert cleaned[0]["title"] == "Valid title"
        assert len(issues) >= 2  # two empty-title issues

    def test_clean_articles_fixes_invalid_labels(self):
        """_clean_articles converts invalid sentiment_label to neutral."""
        from newsapi_client import _clean_articles
        articles = [
            {"title": "Test", "summary": "ok", "sentiment_label": "SUPER_BULLISH"},
        ]
        cleaned, issues = _clean_articles(articles)
        assert cleaned[0]["sentiment_label"] == "neutral"
        assert any("SUPER_BULLISH" in i for i in issues)

    def test_deduplicate_articles_removes_similar(self):
        """_deduplicate_articles removes articles with high Jaccard similarity."""
        from newsapi_client import _deduplicate_articles
        articles = [
            {"title": "NVDA stock surges on strong AI demand growth"},
            {"title": "NVDA stock surges on strong AI demand growth today"},  # near-dup
            {"title": "Completely different topic about bonds"},
        ]
        result = _deduplicate_articles(articles, threshold=0.5)
        assert len(result) == 2
        assert result[0]["title"].startswith("NVDA")
        assert "bonds" in result[1]["title"]

    def test_av_quota_ok_increments_and_limits(self):
        """_av_quota_ok increments count and returns False when exhausted."""
        import newsapi_client
        newsapi_client._av_daily["date"] = time.strftime("%Y-%m-%d")
        newsapi_client._av_daily["count"] = 0

        # Should succeed for the first _AV_DAILY_LIMIT calls
        for i in range(newsapi_client._AV_DAILY_LIMIT):
            assert newsapi_client._av_quota_ok() is True

        # Next call should fail
        assert newsapi_client._av_quota_ok() is False
        assert newsapi_client._av_daily["count"] == newsapi_client._AV_DAILY_LIMIT

    def test_recency_weight_returns_valid_range(self):
        """_recency_weight returns float in (0, 1] for valid timestamps, 0.5 for invalid."""
        from newsapi_client import _recency_weight
        # Invalid/empty string -> fallback 0.5
        assert _recency_weight("") == 0.5
        assert _recency_weight("not-a-date") == 0.5

        # Very recent date should be close to 1.0
        from datetime import datetime
        now_str = datetime.now().isoformat()
        weight = _recency_weight(now_str, half_life_hours=24.0)
        assert 0.9 <= weight <= 1.0

    def test_fallback_structure(self):
        """_fallback returns correct default structure."""
        from newsapi_client import _fallback
        result = _fallback("TEST")

        assert result["ticker"] == "TEST"
        assert result["is_real_data"] is False
        assert result["sentiment_score"] == 5.0
        assert result["source"] == "fallback"
        assert result["total_articles"] == 0
        assert result["articles"] == []
        assert result["bullish_count"] == 0
        assert result["bearish_count"] == 0
        assert result["neutral_count"] == 0
        assert "data_quality" in result

    def test_breaker_none_path(self, monkeypatch):
        """When _news_breaker is None, fetching still works without error."""
        _noop_limiter(monkeypatch)
        import newsapi_client
        monkeypatch.setattr(newsapi_client, "_news_breaker", None)

        resp = _make_response(SAMPLE_YF_RESPONSE)
        _mock_session(monkeypatch, resp)

        result = newsapi_client._fetch_yf_news("NVDA")
        # Should succeed without breaker check
        assert result["is_real_data"] is True
        assert result["source"] == "yahoo_finance"

    def test_limiter_none_path(self, monkeypatch):
        """When _news_limiter is None, fetching still works without error."""
        import newsapi_client
        monkeypatch.setattr(newsapi_client, "_news_limiter", None)
        monkeypatch.setattr(newsapi_client, "_news_breaker", None)

        resp = _make_response(SAMPLE_YF_RESPONSE)
        _mock_session(monkeypatch, resp)

        result = newsapi_client._fetch_yf_news("NVDA")
        assert result["is_real_data"] is True

    def test_sentiment_score_always_in_range(self, monkeypatch):
        """End-to-end: sentiment_score is always in [1.0, 10.0]."""
        _noop_limiter(monkeypatch)

        # All bullish articles
        bullish_response = {
            "news": [
                {"title": "Stock surges and rallies to record high",
                 "publisher": "Reuters", "summary": "Strong growth beat",
                 "providerPublishTime": 1709900000, "link": "https://e.com/1"},
                {"title": "Upgrade to buy with bullish outlook",
                 "publisher": "Bloomberg", "summary": "Profit expands",
                 "providerPublishTime": 1709890000, "link": "https://e.com/2"},
            ]
        }
        resp = _make_response(bullish_response)
        _mock_session(monkeypatch, resp)

        from newsapi_client import get_ticker_news
        result = get_ticker_news("BULL")
        assert 1.0 <= result["sentiment_score"] <= 10.0

        # All bearish articles (use different ticker to avoid cache)
        bearish_response = {
            "news": [
                {"title": "Stock crash and decline to new lows",
                 "publisher": "CNBC", "summary": "Weak loss and sell off",
                 "providerPublishTime": 1709900000, "link": "https://e.com/3"},
                {"title": "Downgrade to sell with bearish warning",
                 "publisher": "WSJ", "summary": "Negative outlook and cut",
                 "providerPublishTime": 1709890000, "link": "https://e.com/4"},
            ]
        }
        resp2 = _make_response(bearish_response)
        _mock_session(monkeypatch, resp2)
        result2 = get_ticker_news("BEAR")
        assert 1.0 <= result2["sentiment_score"] <= 10.0

    def test_count_consistency(self, monkeypatch):
        """bullish_count + bearish_count + neutral_count == total_articles."""
        _noop_limiter(monkeypatch)
        resp = _make_response(SAMPLE_YF_RESPONSE)
        _mock_session(monkeypatch, resp)

        from newsapi_client import get_ticker_news
        result = get_ticker_news("NVDA")

        total = result["total_articles"]
        assert total == result["bullish_count"] + result["bearish_count"] + result["neutral_count"]
        assert total == len(result["articles"])
