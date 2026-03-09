"""
Tests for reddit_sentiment module -- ApeWisdom API Reddit sentiment scoring
"""

import types
import pytest


# ==================== autouse fixture ====================

@pytest.fixture(autouse=True)
def _clear_reddit_cache(tmp_path, monkeypatch):
    """Clear singleton _holder dict AND RedditSentimentClient._ranking_cache
    before each test; redirect CACHE_DIR to tmp_path to avoid disk pollution."""
    import reddit_sentiment
    from resilience import reddit_breaker

    # Redirect disk cache to tmp_path
    cache_dir = tmp_path / "reddit_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(reddit_sentiment, "CACHE_DIR", cache_dir)

    # Reset singleton holder
    reddit_sentiment._holder.clear()

    # Reset circuit breaker so it starts CLOSED
    reddit_breaker.reset()

    yield

    # Teardown: clear again
    reddit_sentiment._holder.clear()


# ==================== helpers ====================

def _make_apewisdom_response(results):
    """Build a SimpleNamespace that mimics requests.Response for ApeWisdom API."""
    resp = types.SimpleNamespace()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.json = lambda: {"results": results}
    return resp


def _mock_session(monkeypatch, response):
    """Patch get_session to return a SimpleNamespace with .get() returning *response*.
    Returns the mock session so tests can inspect call counts."""
    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        return response

    session = types.SimpleNamespace(get=fake_get)
    monkeypatch.setattr("reddit_sentiment.get_session", lambda source: session)
    return call_count


def _noop_limiter(monkeypatch):
    """Replace reddit_limiter.acquire with a no-op that always returns True."""
    monkeypatch.setattr(
        "reddit_sentiment.reddit_limiter",
        types.SimpleNamespace(acquire=lambda **kw: True),
    )


SAMPLE_RESULTS = [
    {"ticker": "NVDA", "rank": 1, "mentions": 500, "mentions_24h_ago": 300, "upvotes": 1200},
    {"ticker": "TSLA", "rank": 2, "mentions": 400, "mentions_24h_ago": 350, "upvotes": 800},
    {"ticker": "AAPL", "rank": 3, "mentions": 300, "mentions_24h_ago": 310, "upvotes": 600},
    {"ticker": "AMD", "rank": 10, "mentions": 120, "mentions_24h_ago": 100, "upvotes": 250},
    {"ticker": "PLTR", "rank": 25, "mentions": 50, "mentions_24h_ago": 80, "upvotes": 90},
    {"ticker": "GME", "rank": 50, "mentions": 20, "mentions_24h_ago": 30, "upvotes": 10},
]


# ==================== TestRedditSentiment ====================

class TestRedditSentiment:
    """Tests for get_ticker_sentiment via RedditSentimentClient."""

    def test_success_with_mentions(self, monkeypatch):
        """Mock ApeWisdom API and verify mentions/rank/score for NVDA."""
        _noop_limiter(monkeypatch)
        resp = _make_apewisdom_response(SAMPLE_RESULTS)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("NVDA")

        assert result["ticker"] == "NVDA"
        assert result["rank"] == 1
        assert result["mentions"] == 500
        assert result["mentions_24h_ago"] == 300
        assert result["mention_delta"] == 200
        assert result["sentiment_score"] >= 1.0
        assert len(result["sources"]) >= 1

    def test_ticker_not_found(self, monkeypatch):
        """When ticker is absent from rankings, rank=None and buzz='quiet'."""
        _noop_limiter(monkeypatch)
        resp = _make_apewisdom_response(SAMPLE_RESULTS)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("ZZZZ")

        assert result["rank"] is None
        assert result["reddit_buzz"] == "quiet"
        assert result["mentions"] == 0
        assert result["sentiment_score"] == 5.0

    def test_api_failure_fallback(self, monkeypatch):
        """When HTTP call raises ConnectionError, _fetch_ranking returns empty
        and the client falls back to quiet result."""
        _noop_limiter(monkeypatch)

        def failing_get(url, **kwargs):
            raise ConnectionError("mock network failure")

        session = types.SimpleNamespace(get=failing_get)
        monkeypatch.setattr("reddit_sentiment.get_session", lambda source: session)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("NVDA")

        # Should gracefully degrade to quiet result
        assert result["rank"] is None
        assert result["reddit_buzz"] == "quiet"
        assert result["sentiment_score"] == 5.0

    def test_cache_hit(self, monkeypatch):
        """Two consecutive _fetch_ranking calls with same filter should only
        make 1 HTTP request (memory cache)."""
        _noop_limiter(monkeypatch)
        resp = _make_apewisdom_response(SAMPLE_RESULTS)
        call_count = _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()

        # First call: fetches from API (2 HTTP calls: all-stocks + wsb)
        client.get_ticker_sentiment("NVDA")
        first_count = call_count["n"]

        # Second call for different ticker but same rankings: should use cache
        client.get_ticker_sentiment("TSLA")
        second_count = call_count["n"]

        # The second call should NOT make additional HTTP requests
        # because rankings are already cached in memory
        assert second_count == first_count

    def test_high_rank_score(self, monkeypatch):
        """Rank 1 with strong momentum should yield score > 6.5."""
        _noop_limiter(monkeypatch)
        results = [
            {"ticker": "NVDA", "rank": 1, "mentions": 500,
             "mentions_24h_ago": 200, "upvotes": 3000},
        ]
        resp = _make_apewisdom_response(results)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("NVDA")

        # rank=1 (+1.5) + momentum 150% (+1.5) + quality 6.0 (+0.5) = 5+3.5 = 8.5
        assert result["sentiment_score"] > 6.5

    def test_low_rank_score(self, monkeypatch):
        """Rank 50 with weak momentum should yield score around 5.5."""
        _noop_limiter(monkeypatch)
        results = [
            {"ticker": "SLOW", "rank": 50, "mentions": 20,
             "mentions_24h_ago": 18, "upvotes": 30},
        ]
        resp = _make_apewisdom_response(results)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("SLOW")

        # rank=50 (+0) + momentum ~11% (+0.5) + quality 1.5 (+0) = 5.5
        assert 5.0 <= result["sentiment_score"] <= 6.5

    def test_no_rank_score(self, monkeypatch):
        """Ticker not in any ranking should return score=5.0 exactly."""
        _noop_limiter(monkeypatch)
        resp = _make_apewisdom_response([])
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("NONEXIST")

        assert result["sentiment_score"] == 5.0

    def test_sentiment_score_clamped(self, monkeypatch):
        """Score should always be in [1.0, 10.0] regardless of extreme inputs."""
        _noop_limiter(monkeypatch)

        # Extreme high: rank=1, huge momentum, high quality
        extreme_high = [
            {"ticker": "MOON", "rank": 1, "mentions": 10000,
             "mentions_24h_ago": 100, "upvotes": 500000},
        ]
        resp = _make_apewisdom_response(extreme_high)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("MOON")
        assert 1.0 <= result["sentiment_score"] <= 10.0

        # Extreme low: rank far down, negative momentum, low quality
        extreme_low = [
            {"ticker": "DUMP", "rank": 999, "mentions": 1,
             "mentions_24h_ago": 100, "upvotes": 0},
        ]
        client2 = RedditSentimentClient()
        resp2 = _make_apewisdom_response(extreme_low)
        _mock_session(monkeypatch, resp2)
        result2 = client2.get_ticker_sentiment("DUMP")
        assert 1.0 <= result2["sentiment_score"] <= 10.0

    def test_requests_missing(self, monkeypatch):
        """When the requests library is None, _fetch_ranking returns empty list."""
        _noop_limiter(monkeypatch)
        monkeypatch.setattr("reddit_sentiment.requests", None)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("NVDA")

        assert result["rank"] is None
        assert result["reddit_buzz"] == "quiet"

    def test_result_keys(self, monkeypatch):
        """Verify all required keys are present in the result dict."""
        _noop_limiter(monkeypatch)
        resp = _make_apewisdom_response(SAMPLE_RESULTS)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("NVDA")

        required_keys = {
            "ticker", "rank", "mentions", "mentions_24h_ago",
            "mention_delta", "upvotes", "momentum_pct",
            "reddit_buzz", "sentiment_score", "sources",
            "wsb_rank", "timestamp",
        }
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - result.keys()}"
        )

    def test_buzz_hot(self, monkeypatch):
        """rank<=5 and momentum>30% should yield buzz='hot'."""
        _noop_limiter(monkeypatch)
        results = [
            {"ticker": "FIRE", "rank": 3, "mentions": 200,
             "mentions_24h_ago": 100, "upvotes": 600},
        ]
        resp = _make_apewisdom_response(results)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("FIRE")

        # momentum = (200-100)/100 * 100 = 100%, rank=3 -> hot
        assert result["reddit_buzz"] == "hot"

    def test_buzz_cooling(self, monkeypatch):
        """momentum < -20% should yield buzz='cooling'."""
        _noop_limiter(monkeypatch)
        results = [
            {"ticker": "COLD", "rank": 30, "mentions": 50,
             "mentions_24h_ago": 100, "upvotes": 80},
        ]
        resp = _make_apewisdom_response(results)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        result = client.get_ticker_sentiment("COLD")

        # momentum = (50-100)/100 * 100 = -50%, rank=30 -> cooling
        assert result["reddit_buzz"] == "cooling"


# ==================== TestRedditSentimentClient ====================

class TestRedditSentimentClient:
    """Tests for convenience function and client lifecycle."""

    def test_singleton_pattern(self, monkeypatch):
        """Two get_reddit_sentiment() calls reuse the same singleton client
        and return consistent structure."""
        _noop_limiter(monkeypatch)
        resp = _make_apewisdom_response(SAMPLE_RESULTS)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import get_reddit_sentiment
        r1 = get_reddit_sentiment("NVDA")
        r2 = get_reddit_sentiment("NVDA")

        # Both calls should produce valid results with same ticker
        assert r1["ticker"] == "NVDA"
        assert r2["ticker"] == "NVDA"
        # Scores should be identical (same cached data)
        assert r1["sentiment_score"] == r2["sentiment_score"]

    def test_rate_limiter_called(self, monkeypatch):
        """Verify reddit_limiter.acquire() is called during _fetch_ranking."""
        acquire_calls = {"n": 0}

        def tracking_acquire(**kwargs):
            acquire_calls["n"] += 1
            return True

        monkeypatch.setattr(
            "reddit_sentiment.reddit_limiter",
            types.SimpleNamespace(acquire=tracking_acquire),
        )
        resp = _make_apewisdom_response(SAMPLE_RESULTS)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()
        client.get_ticker_sentiment("NVDA")

        # _fetch_ranking is called for "all-stocks" and "wallstreetbets"
        assert acquire_calls["n"] >= 2

    def test_multiple_tickers(self, monkeypatch):
        """Querying two different tickers sequentially should yield
        independent, correct results."""
        _noop_limiter(monkeypatch)
        resp = _make_apewisdom_response(SAMPLE_RESULTS)
        _mock_session(monkeypatch, resp)

        from reddit_sentiment import RedditSentimentClient
        client = RedditSentimentClient()

        nvda = client.get_ticker_sentiment("NVDA")
        tsla = client.get_ticker_sentiment("TSLA")

        assert nvda["ticker"] == "NVDA"
        assert tsla["ticker"] == "TSLA"
        assert nvda["rank"] == 1
        assert tsla["rank"] == 2
        # Different positions should generally yield different scores
        # (both are high-rank so scores may be close, but mentions differ)
        assert nvda["mentions"] != tsla["mentions"]
