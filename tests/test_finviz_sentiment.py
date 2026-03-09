"""
Tests for finviz_sentiment module — Finviz 新闻情绪分析

覆盖: HTML 解析、情绪打分、失败降级、缓存行为、单例/限流/熔断
"""

import types
import pytest


# ==================== Mock HTML ====================

MOCK_FINVIZ_HTML = '''
<html><body>
<table id="news-table">
<tr><td><a class="tab-link-news" href="#">NVDA stock surges on strong earnings beat</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Analysts upgrade NVDA target price</a></td></tr>
<tr><td><a class="tab-link-news" href="#">NVDA faces regulatory headwinds in China</a></td></tr>
</table>
</body></html>
'''

MOCK_BULLISH_HTML = '''
<table id="news-table">
<tr><td><a class="tab-link-news" href="#">Stock surges after earnings beat</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Analysts upgrade to strong buy</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Record growth exceeds expectations</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Bullish momentum rally continues</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Profit soars on expansion wins</a></td></tr>
</table>
'''

MOCK_BEARISH_HTML = '''
<table id="news-table">
<tr><td><a class="tab-link-news" href="#">Stock crashes on earnings miss</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Analysts downgrade sell rating</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Massive decline after weak guidance</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Bearish drop continues as losses mount</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Company cuts layoff warning issued</a></td></tr>
</table>
'''

MOCK_MIXED_HTML = '''
<table id="news-table">
<tr><td><a class="tab-link-news" href="#">Stock surges on strong earnings beat</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Stock crashes on weak guidance</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Company announces new product launch</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Analysts upgrade after rally</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Investors fear decline risk</a></td></tr>
<tr><td><a class="tab-link-news" href="#">Market trends show neutral stance</a></td></tr>
</table>
'''

MOCK_NO_NEWS_TABLE_HTML = '''
<html><body>
<table id="other-table"><tr><td>No news here</td></tr></table>
</body></html>
'''

MANY_TITLES_HTML_TEMPLATE = '<table id="news-table">{rows}</table>'


# ==================== Fixtures ====================

def _make_response(text="", status_code=200):
    """Create a SimpleNamespace mock HTTP response."""
    return types.SimpleNamespace(text=text, status_code=status_code)


def _make_session(response):
    """Create a SimpleNamespace mock session with .get() returning response."""
    return types.SimpleNamespace(get=lambda *args, **kwargs: response)


@pytest.fixture(autouse=True)
def _clear_finviz_cache(tmp_path, monkeypatch):
    """Clear singleton _holder dict and redirect disk cache to tmp_path."""
    import finviz_sentiment
    finviz_sentiment._holder.clear()

    # Redirect CACHE_DIR to tmp_path to avoid polluting project directory
    cache_dir = tmp_path / "finviz_cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(finviz_sentiment, "CACHE_DIR", cache_dir)

    yield

    finviz_sentiment._holder.clear()


@pytest.fixture
def mock_finviz_session(monkeypatch):
    """Helper fixture: patches get_session and finviz_limiter for basic Finviz mocking.

    Returns a callable that accepts HTML text and wires up the mock session.
    """
    import finviz_sentiment

    def _setup(html_text, status_code=200):
        resp = _make_response(text=html_text, status_code=status_code)
        session = _make_session(resp)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=lambda *a, **kw: True))
        # Ensure circuit breaker allows requests
        from resilience import finviz_breaker
        finviz_breaker.reset()
        return resp

    return _setup


# ==================== TestFinvizNewsTitles ====================

class TestFinvizNewsTitles:
    """get_news_titles() 的 HTML 解析与异常测试"""

    def test_parse_titles_from_html(self, mock_finviz_session):
        """Mock Finviz HTML with news-table, verify titles extracted."""
        mock_finviz_session(MOCK_FINVIZ_HTML)

        from finviz_sentiment import FinvizSentimentClient
        client = FinvizSentimentClient()
        titles = client.get_news_titles("NVDA")

        assert len(titles) == 3
        assert "NVDA stock surges on strong earnings beat" in titles
        assert "Analysts upgrade NVDA target price" in titles
        assert "NVDA faces regulatory headwinds in China" in titles

    def test_no_news_table(self, mock_finviz_session):
        """HTML without news-table returns empty list."""
        mock_finviz_session(MOCK_NO_NEWS_TABLE_HTML)

        from finviz_sentiment import FinvizSentimentClient
        client = FinvizSentimentClient()
        titles = client.get_news_titles("AAPL")

        assert titles == []

    def test_api_failure(self, monkeypatch):
        """ConnectionError returns empty list (graceful degradation)."""
        import finviz_sentiment

        def _raise(*args, **kwargs):
            raise ConnectionError("mocked network failure")

        mock_session = types.SimpleNamespace(get=_raise)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: mock_session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=lambda *a, **kw: True))
        from resilience import finviz_breaker
        finviz_breaker.reset()

        client = finviz_sentiment.FinvizSentimentClient()
        titles = client.get_news_titles("FAIL")

        assert titles == []

    def test_cache_hit(self, monkeypatch):
        """Second call should hit disk cache; only 1 HTTP request made."""
        import finviz_sentiment

        call_count = {"n": 0}

        def _mock_get(*args, **kwargs):
            call_count["n"] += 1
            return _make_response(text=MOCK_FINVIZ_HTML)

        mock_session = types.SimpleNamespace(get=_mock_get)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: mock_session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=lambda *a, **kw: True))
        from resilience import finviz_breaker
        finviz_breaker.reset()

        client = finviz_sentiment.FinvizSentimentClient()
        titles1 = client.get_news_titles("NVDA")
        titles2 = client.get_news_titles("NVDA")

        assert titles1 == titles2
        assert len(titles1) == 3
        assert call_count["n"] == 1, "Expected only 1 HTTP request; second should be cached"

    def test_max_titles_limit(self, monkeypatch):
        """Verify max_titles parameter caps the number of returned titles."""
        import finviz_sentiment

        # Generate HTML with 10 titles
        rows = "\n".join(
            f'<tr><td><a class="tab-link-news" href="#">Title number {i}</a></td></tr>'
            for i in range(10)
        )
        html = MANY_TITLES_HTML_TEMPLATE.format(rows=rows)

        resp = _make_response(text=html)
        session = _make_session(resp)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=lambda *a, **kw: True))
        from resilience import finviz_breaker
        finviz_breaker.reset()

        client = finviz_sentiment.FinvizSentimentClient()
        titles = client.get_news_titles("TEST", max_titles=5)

        assert len(titles) == 5


# ==================== TestFinvizSentiment ====================

class TestFinvizSentiment:
    """analyze_sentiment() 的评分逻辑测试"""

    def test_bullish_titles(self, mock_finviz_session):
        """All bullish titles -> news_score > 6.5 and sentiment_ratio > 0.3."""
        mock_finviz_session(MOCK_BULLISH_HTML)

        from finviz_sentiment import FinvizSentimentClient
        client = FinvizSentimentClient()
        result = client.analyze_sentiment("BULL")

        assert result["news_score"] > 6.5
        assert result["sentiment_ratio"] > 0.3
        assert result["bullish_count"] > result["bearish_count"]

    def test_bearish_titles(self, mock_finviz_session):
        """All bearish titles -> news_score < 4.0 and sentiment_ratio < -0.3."""
        mock_finviz_session(MOCK_BEARISH_HTML)

        from finviz_sentiment import FinvizSentimentClient
        client = FinvizSentimentClient()
        result = client.analyze_sentiment("BEAR")

        assert result["news_score"] < 4.0
        assert result["sentiment_ratio"] < -0.3
        assert result["bearish_count"] > result["bullish_count"]

    def test_mixed_titles(self, mock_finviz_session):
        """Mixed titles -> score approximately 5.0 (within 2.0 of neutral)."""
        mock_finviz_session(MOCK_MIXED_HTML)

        from finviz_sentiment import FinvizSentimentClient
        client = FinvizSentimentClient()
        result = client.analyze_sentiment("MIX")

        assert 3.0 <= result["news_score"] <= 7.0
        assert -0.5 <= result["sentiment_ratio"] <= 0.5

    def test_no_titles_default(self, mock_finviz_session):
        """No titles (empty news table) -> default result with score=5.0."""
        mock_finviz_session(MOCK_NO_NEWS_TABLE_HTML)

        from finviz_sentiment import FinvizSentimentClient
        client = FinvizSentimentClient()
        result = client.analyze_sentiment("EMPTY")

        assert result["news_score"] == 5.0
        assert result["total_titles"] == 0
        assert result["bullish_count"] == 0
        assert result["bearish_count"] == 0
        assert result["neutral_count"] == 0
        assert result["sentiment_ratio"] == 0.0

    def test_top_bullish_bearish_limit(self, monkeypatch):
        """Verify top_bullish and top_bearish contain at most 3 items each."""
        import finviz_sentiment

        # Generate 10 bullish + 10 bearish titles
        bullish_rows = "\n".join(
            f'<tr><td><a class="tab-link-news" href="#">Stock surges rally growth beat {i}</a></td></tr>'
            for i in range(10)
        )
        bearish_rows = "\n".join(
            f'<tr><td><a class="tab-link-news" href="#">Stock crashes decline drop sell {i}</a></td></tr>'
            for i in range(10)
        )
        html = MANY_TITLES_HTML_TEMPLATE.format(rows=bullish_rows + bearish_rows)

        resp = _make_response(text=html)
        session = _make_session(resp)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=lambda *a, **kw: True))
        from resilience import finviz_breaker
        finviz_breaker.reset()

        client = finviz_sentiment.FinvizSentimentClient()
        result = client.analyze_sentiment("MANY")

        assert len(result["top_bullish"]) <= 3
        assert len(result["top_bearish"]) <= 3

    def test_score_clamped(self, monkeypatch):
        """Verify news_score is clamped to [1.0, 10.0]."""
        import finviz_sentiment

        # Extreme bullish: score = 5.0 + 1.0*3.0 - 0.5 (few titles) = 7.5
        # To push above 10, we need many extreme titles + volume bonus
        # Actually the formula caps at max 5.0 + 3.0 + 0.5 = 8.5
        # So test that it doesn't exceed 10.0 and doesn't go below 1.0

        # Test lower bound: extreme bearish with few titles
        bearish_rows = "\n".join(
            f'<tr><td><a class="tab-link-news" href="#">crash decline drop sell weak {i}</a></td></tr>'
            for i in range(3)
        )
        html = MANY_TITLES_HTML_TEMPLATE.format(rows=bearish_rows)
        resp = _make_response(text=html)
        session = _make_session(resp)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=lambda *a, **kw: True))
        from resilience import finviz_breaker
        finviz_breaker.reset()

        client = finviz_sentiment.FinvizSentimentClient()
        result = client.analyze_sentiment("CLAMP")

        assert 1.0 <= result["news_score"] <= 10.0

    def test_result_keys(self, mock_finviz_session):
        """Verify all required keys are present in the result dict."""
        mock_finviz_session(MOCK_FINVIZ_HTML)

        from finviz_sentiment import FinvizSentimentClient
        client = FinvizSentimentClient()
        result = client.analyze_sentiment("NVDA")

        required_keys = {
            "ticker", "total_titles", "bullish_count", "bearish_count",
            "neutral_count", "sentiment_ratio", "news_score", "news_signal",
            "top_bullish", "top_bearish", "timestamp",
        }
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - result.keys()}"
        )
        assert result["ticker"] == "NVDA"
        assert isinstance(result["top_bullish"], list)
        assert isinstance(result["top_bearish"], list)


# ==================== TestFinvizClient ====================

class TestFinvizClient:
    """便捷函数 get_finviz_sentiment() 的单例/限流/熔断测试"""

    def test_singleton_pattern(self, mock_finviz_session):
        """Two calls to get_finviz_sentiment use the same client instance."""
        mock_finviz_session(MOCK_FINVIZ_HTML)

        import finviz_sentiment
        # First call creates singleton
        result1 = finviz_sentiment.get_finviz_sentiment("NVDA")
        assert result1["ticker"] == "NVDA"

        # Clear title cache to force re-analysis (sentiment cache remains)
        # Since both title and sentiment are cached, second call hits cache
        result2 = finviz_sentiment.get_finviz_sentiment("NVDA")
        assert result2["ticker"] == "NVDA"

        # Singleton should be populated
        assert finviz_sentiment._holder.get("_instance") is not None

    def test_rate_limiter_called(self, monkeypatch):
        """Verify finviz_limiter.acquire() is called during get_news_titles."""
        import finviz_sentiment

        acquire_calls = {"n": 0}

        def _mock_acquire(*args, **kwargs):
            acquire_calls["n"] += 1
            return True

        resp = _make_response(text=MOCK_FINVIZ_HTML)
        session = _make_session(resp)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=_mock_acquire))
        from resilience import finviz_breaker
        finviz_breaker.reset()

        client = finviz_sentiment.FinvizSentimentClient()
        client.get_news_titles("RATE")

        assert acquire_calls["n"] == 1, "finviz_limiter.acquire() should be called once"

    def test_circuit_breaker_open(self, monkeypatch):
        """When finviz_breaker.allow_request() returns False, skip HTTP call."""
        import finviz_sentiment
        from resilience import finviz_breaker

        # Force breaker to OPEN state
        monkeypatch.setattr(finviz_breaker, "_state", "open")
        monkeypatch.setattr(finviz_breaker, "_last_failure_time", float("inf"))

        http_called = {"n": 0}

        def _should_not_call(*args, **kwargs):
            http_called["n"] += 1
            return _make_response(text=MOCK_FINVIZ_HTML)

        mock_session = types.SimpleNamespace(get=_should_not_call)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: mock_session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=lambda *a, **kw: True))

        client = finviz_sentiment.FinvizSentimentClient()
        titles = client.get_news_titles("OPEN")

        assert titles == []
        assert http_called["n"] == 0, "HTTP should NOT be called when breaker is open"

    def test_non_200_status_returns_empty(self, monkeypatch):
        """Non-200 HTTP status code returns empty list."""
        import finviz_sentiment

        resp = _make_response(text="Error", status_code=403)
        session = _make_session(resp)
        monkeypatch.setattr(finviz_sentiment, "get_session", lambda source: session)
        monkeypatch.setattr(finviz_sentiment, "finviz_limiter",
                            types.SimpleNamespace(acquire=lambda *a, **kw: True))
        from resilience import finviz_breaker
        finviz_breaker.reset()

        client = finviz_sentiment.FinvizSentimentClient()
        titles = client.get_news_titles("FORBIDDEN")

        assert titles == []
