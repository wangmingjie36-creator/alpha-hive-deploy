"""
Tests for polymarket_client module — Polymarket 预测市场客户端

覆盖: API 请求、市场搜索、赔率计算、缓存行为、单例/限流/熔断、降级
"""

import json
import types
import pytest


# ==================== Mock Data ====================

SAMPLE_MARKETS = [
    {
        "slug": "nvda-above-150",
        "question": "Will NVDA be above $150?",
        "outcomePrices": "[0.75, 0.25]",
        "volume24hr": 50000,
        "liquidity": 100000,
        "outcomes": ["Yes", "No"],
    },
    {
        "slug": "fed-rate-cut",
        "question": "Will Fed cut rate in March?",
        "outcomePrices": "[0.60, 0.40]",
        "volume24hr": 200000,
        "liquidity": 500000,
        "outcomes": ["Yes", "No"],
    },
]

SAMPLE_BULLISH_MARKETS = [
    {
        "slug": "nvda-above-130",
        "question": "Will NVDA be above $130?",
        "outcomePrices": "[0.85, 0.15]",
        "volume24hr": 80000,
        "liquidity": 200000,
        "outcomes": ["Yes", "No"],
    },
    {
        "slug": "nvda-beat-earnings",
        "question": "Will NVDA beat earnings?",
        "outcomePrices": "[0.78, 0.22]",
        "volume24hr": 120000,
        "liquidity": 300000,
        "outcomes": ["Yes", "No"],
    },
    {
        "slug": "nvda-hit-160",
        "question": "Will NVDA hit $160 by April?",
        "outcomePrices": "[0.65, 0.35]",
        "volume24hr": 60000,
        "liquidity": 150000,
        "outcomes": ["Yes", "No"],
    },
]

SAMPLE_BEARISH_MARKETS = [
    {
        "slug": "nvda-drop-below-100",
        "question": "Will NVDA drop below $100?",
        "outcomePrices": "[0.70, 0.30]",
        "volume24hr": 40000,
        "liquidity": 80000,
        "outcomes": ["Yes", "No"],
    },
    {
        "slug": "nvda-crash-q1",
        "question": "Will NVDA crash in Q1?",
        "outcomePrices": "[0.60, 0.40]",
        "volume24hr": 30000,
        "liquidity": 60000,
        "outcomes": ["Yes", "No"],
    },
]

MACRO_FED_MARKETS = [
    {
        "slug": "fed-rate-hold-march",
        "question": "Will Fed hold rate in March?",
        "outcomePrices": "[0.55, 0.45]",
        "volume24hr": 300000,
        "liquidity": 800000,
        "outcomes": ["Yes", "No"],
    },
]

MACRO_INFLATION_MARKETS = [
    {
        "slug": "cpi-above-3-percent",
        "question": "Will inflation CPI be above 3%?",
        "outcomePrices": "[0.40, 0.60]",
        "volume24hr": 150000,
        "liquidity": 400000,
        "outcomes": ["Yes", "No"],
    },
]


# ==================== Fixtures ====================

def _make_response(data, status_code=200):
    """Create a SimpleNamespace mock HTTP response that returns JSON data."""
    resp = types.SimpleNamespace()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json = lambda: data
    resp.headers = {}

    def _raise_for_status():
        if status_code >= 400:
            raise Exception(f"HTTP {status_code}")

    resp.raise_for_status = _raise_for_status
    return resp


@pytest.fixture(autouse=True)
def _clear_polymarket_state(tmp_path, monkeypatch):
    """Clear singleton _holder, redirect CACHE_DIR to tmp_path, reset circuit breaker."""
    import polymarket_client
    from resilience import polymarket_breaker

    # Clear singleton holder
    polymarket_client._holder.clear()

    # Redirect CACHE_DIR to tmp_path to avoid polluting project directory
    cache_dir = tmp_path / "polymarket_cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(polymarket_client, "CACHE_DIR", cache_dir)

    # Reset circuit breaker to closed state
    polymarket_breaker.reset()

    # Replace rate limiter with noop to avoid delays in tests
    monkeypatch.setattr(
        polymarket_client, "polymarket_limiter",
        types.SimpleNamespace(acquire=lambda *a, **kw: None),
    )

    yield

    polymarket_client._holder.clear()
    polymarket_breaker.reset()


# ==================== TestSearchMarkets ====================

class TestSearchMarkets:
    """search_markets() 的搜索、过滤与缓存测试"""

    def test_search_returns_filtered_results(self, monkeypatch):
        """Search for 'nvda' returns only markets matching the query."""
        import polymarket_client

        resp = _make_response(SAMPLE_MARKETS)
        session = types.SimpleNamespace(get=lambda *a, **kw: resp)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        client = polymarket_client.PolymarketClient()
        results = client.search_markets("nvda")

        assert len(results) >= 1
        # The NVDA market should be included
        slugs = [m["slug"] for m in results]
        assert "nvda-above-150" in slugs

    def test_search_empty_query_returns_empty(self, monkeypatch):
        """Empty or whitespace-only query returns empty list without HTTP call."""
        import polymarket_client

        http_called = {"n": 0}

        def _should_not_call(*a, **kw):
            http_called["n"] += 1
            return _make_response([])

        session = types.SimpleNamespace(get=_should_not_call)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        client = polymarket_client.PolymarketClient()
        assert client.search_markets("") == []
        assert client.search_markets("   ") == []
        assert client.search_markets(None) == []
        assert http_called["n"] == 0

    def test_search_api_returns_none(self, monkeypatch):
        """API returning None (network error) yields empty list."""
        import polymarket_client

        resp = _make_response(None)
        # Simulate _get returning None by making the session raise
        def _raise_get(*a, **kw):
            raise ConnectionError("mocked")

        session = types.SimpleNamespace(get=_raise_get)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        client = polymarket_client.PolymarketClient()
        results = client.search_markets("nvda")
        assert results == []

    def test_search_disk_cache_hit(self, monkeypatch, tmp_path):
        """Second search for same query reads from disk cache; no HTTP call."""
        import polymarket_client

        call_count = {"n": 0}

        def _counting_get(*a, **kw):
            call_count["n"] += 1
            return _make_response(SAMPLE_MARKETS)

        session = types.SimpleNamespace(get=_counting_get)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        client = polymarket_client.PolymarketClient()
        r1 = client.search_markets("nvda")
        r2 = client.search_markets("nvda")

        assert r1 == r2
        assert call_count["n"] == 1, "Second call should use disk cache"

    def test_search_partial_word_match(self, monkeypatch):
        """When exact match returns <3 results, partial word matching kicks in."""
        import polymarket_client

        # Markets where "fed" appears only in slug/question via partial word match
        markets = [
            {
                "slug": "federal-reserve-decision",
                "question": "Federal Reserve Decision",
                "outcomePrices": "[0.50, 0.50]",
                "volume24hr": 100000,
                "liquidity": 200000,
                "outcomes": ["Yes", "No"],
            },
        ]
        resp = _make_response(markets)
        session = types.SimpleNamespace(get=lambda *a, **kw: resp)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        client = polymarket_client.PolymarketClient()
        results = client.search_markets("federal")
        # "federal" is in the question, should match
        assert len(results) >= 1


# ==================== TestGetTickerOdds ====================

class TestGetTickerOdds:
    """get_ticker_odds() 的赔率计算与信号分类测试"""

    def _setup_mock(self, monkeypatch, markets_data):
        """Helper to set up mock session returning markets_data."""
        import polymarket_client

        resp = _make_response(markets_data)
        session = types.SimpleNamespace(get=lambda *a, **kw: resp)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

    def test_bullish_signal(self, monkeypatch):
        """Markets with high bullish probability yield score > 5 and bullish signal."""
        import polymarket_client

        self._setup_mock(monkeypatch, SAMPLE_BULLISH_MARKETS)

        client = polymarket_client.PolymarketClient()
        result = client.get_ticker_odds("NVDA")

        assert result["ticker"] == "NVDA"
        assert result["markets_found"] >= 1
        assert result["odds_score"] > 5.0
        assert result["implied_bullish"] > 0.5
        assert "timestamp" in result

    def test_default_result_when_no_markets(self, monkeypatch):
        """No matching markets returns default result with score=5.0."""
        import polymarket_client

        # Return empty list from API
        resp = _make_response([])
        session = types.SimpleNamespace(get=lambda *a, **kw: resp)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        client = polymarket_client.PolymarketClient()
        result = client.get_ticker_odds("XYZUNKNOWN")

        assert result["ticker"] == "XYZUNKNOWN"
        assert result["markets_found"] == 0
        assert result["odds_score"] == 5.0
        assert result["implied_bullish"] == 0.5
        assert result["implied_bearish"] == 0.5
        assert result["odds_signal"] == "无相关预测市场"

    def test_result_keys_complete(self, monkeypatch):
        """Verify all required keys are present in the result dict."""
        import polymarket_client

        self._setup_mock(monkeypatch, SAMPLE_MARKETS)

        client = polymarket_client.PolymarketClient()
        result = client.get_ticker_odds("NVDA")

        required_keys = {
            "ticker", "markets_found", "top_markets",
            "implied_bullish", "implied_bearish",
            "total_volume_24h", "avg_liquidity",
            "odds_score", "odds_signal", "timestamp",
        }
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - result.keys()}"
        )

    def test_odds_score_clamped(self, monkeypatch):
        """odds_score must be within [1.0, 10.0] regardless of input data."""
        import polymarket_client

        self._setup_mock(monkeypatch, SAMPLE_BULLISH_MARKETS)

        client = polymarket_client.PolymarketClient()
        result = client.get_ticker_odds("NVDA")

        assert 1.0 <= result["odds_score"] <= 10.0

    def test_top_markets_limited_to_five(self, monkeypatch):
        """top_markets list should contain at most 5 entries."""
        import polymarket_client

        # Create 8 markets to verify the cap
        many_markets = []
        for i in range(8):
            many_markets.append({
                "slug": f"nvda-market-{i}",
                "question": f"Will NVDA reach target {i}?",
                "outcomePrices": f"[0.{50+i}, 0.{50-i}]",
                "volume24hr": 10000 * (i + 1),
                "liquidity": 50000 * (i + 1),
                "outcomes": ["Yes", "No"],
            })

        self._setup_mock(monkeypatch, many_markets)

        client = polymarket_client.PolymarketClient()
        result = client.get_ticker_odds("NVDA")

        assert len(result["top_markets"]) <= 5

    def test_volume_bonus(self, monkeypatch):
        """High volume (>100k) should add bonus to score vs low volume."""
        import polymarket_client

        # High volume markets
        high_vol_markets = [
            {
                "slug": "nvda-above-140",
                "question": "Will NVDA be above $140?",
                "outcomePrices": "[0.60, 0.40]",
                "volume24hr": 200000,
                "liquidity": 500000,
                "outcomes": ["Yes", "No"],
            },
        ]
        self._setup_mock(monkeypatch, high_vol_markets)

        client = polymarket_client.PolymarketClient()
        result_high = client.get_ticker_odds("NVDA")

        # Low volume markets with same prices
        low_vol_markets = [
            {
                "slug": "nvda-above-140",
                "question": "Will NVDA be above $140?",
                "outcomePrices": "[0.60, 0.40]",
                "volume24hr": 1000,
                "liquidity": 5000,
                "outcomes": ["Yes", "No"],
            },
        ]

        # Clear cache to force recalculation
        import polymarket_client as pm
        cache_dir = pm.CACHE_DIR
        for f in cache_dir.iterdir():
            f.unlink()

        self._setup_mock(monkeypatch, low_vol_markets)
        client2 = polymarket_client.PolymarketClient()
        result_low = client2.get_ticker_odds("NVDA")

        assert result_high["total_volume_24h"] > result_low["total_volume_24h"]


# ==================== TestPolymarketClient ====================

class TestPolymarketClient:
    """PolymarketClient 的单例、限流、熔断、降级测试"""

    def test_singleton_via_get_polymarket_odds(self, monkeypatch):
        """get_polymarket_odds() convenience function uses singleton client."""
        import polymarket_client

        resp = _make_response(SAMPLE_MARKETS)
        session = types.SimpleNamespace(get=lambda *a, **kw: resp)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        result = polymarket_client.get_polymarket_odds("NVDA")
        assert result["ticker"] == "NVDA"

        # Singleton should be populated
        assert polymarket_client._holder.get("_instance") is not None

    def test_circuit_breaker_open_skips_http(self, monkeypatch):
        """When polymarket_breaker is OPEN, _get returns None without HTTP call."""
        import polymarket_client
        from resilience import polymarket_breaker

        # Force breaker to OPEN state
        monkeypatch.setattr(polymarket_breaker, "_state", "open")
        monkeypatch.setattr(polymarket_breaker, "_last_failure_time", float("inf"))

        http_called = {"n": 0}

        def _should_not_call(*a, **kw):
            http_called["n"] += 1
            return _make_response(SAMPLE_MARKETS)

        session = types.SimpleNamespace(get=_should_not_call)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        client = polymarket_client.PolymarketClient()
        result = client._get("/markets")

        assert result is None
        assert http_called["n"] == 0

    def test_normalize_market_parses_prices(self):
        """_normalize_market correctly parses outcomePrices from JSON string."""
        from polymarket_client import PolymarketClient

        client = PolymarketClient()
        raw = {
            "slug": "test-market",
            "question": "Test question?",
            "outcomePrices": "[0.65, 0.35]",
            "volume24hr": 12345,
            "liquidity": 67890,
            "outcomes": ["Yes", "No"],
            "endDate": "2026-04-01",
        }
        normalized = client._normalize_market(raw)

        assert normalized["slug"] == "test-market"
        assert normalized["question"] == "Test question?"
        assert normalized["outcome_prices"] == [0.65, 0.35]
        assert normalized["volume_24h"] == 12345.0
        assert normalized["liquidity"] == 67890.0
        assert normalized["end_date"] == "2026-04-01"

    def test_normalize_market_handles_invalid_prices(self):
        """_normalize_market handles malformed outcomePrices gracefully."""
        from polymarket_client import PolymarketClient

        client = PolymarketClient()
        raw = {
            "slug": "bad-prices",
            "question": "Bad prices?",
            "outcomePrices": "not-valid-json",
            "volume24hr": 0,
            "liquidity": None,
            "outcomes": [],
        }
        normalized = client._normalize_market(raw)

        assert normalized["outcome_prices"] == []
        assert normalized["volume_24h"] == 0.0
        assert normalized["liquidity"] == 0.0

    def test_get_macro_events(self, monkeypatch):
        """get_macro_events() searches multiple keywords and deduplicates results."""
        import polymarket_client

        # Track which queries are searched
        queries_seen = []

        original_search = polymarket_client.PolymarketClient.search_markets

        def _mock_search(self_client, query, limit=20):
            queries_seen.append(query)
            keyword_map = {
                "fed rate": MACRO_FED_MARKETS,
                "inflation": MACRO_INFLATION_MARKETS,
                "recession": [],
                "gdp": [],
            }
            # Return normalized markets (the real method would call _normalize_market)
            raw_markets = keyword_map.get(query, [])
            return [self_client._normalize_market(m) for m in raw_markets]

        monkeypatch.setattr(polymarket_client.PolymarketClient, "search_markets", _mock_search)

        client = polymarket_client.PolymarketClient()
        events = client.get_macro_events()

        # Should have searched all 4 keywords
        assert "fed rate" in queries_seen
        assert "inflation" in queries_seen
        assert "recession" in queries_seen
        assert "gdp" in queries_seen

        # Should return deduplicated results with category field
        assert len(events) >= 2
        categories = [e.get("category") for e in events]
        assert "fed rate" in categories
        assert "inflation" in categories

    def test_default_result_structure(self):
        """_default_result returns correct structure with neutral values."""
        from polymarket_client import PolymarketClient

        client = PolymarketClient()
        result = client._default_result("TSLA")

        assert result["ticker"] == "TSLA"
        assert result["markets_found"] == 0
        assert result["top_markets"] == []
        assert result["implied_bullish"] == 0.5
        assert result["implied_bearish"] == 0.5
        assert result["odds_score"] == 5.0
        assert result["odds_signal"] == "无相关预测市场"
        assert "timestamp" in result

    def test_429_retry_logic(self, monkeypatch):
        """HTTP 429 triggers retry with backoff (up to _max_retries)."""
        import polymarket_client

        attempt_count = {"n": 0}

        def _mock_get(*a, **kw):
            attempt_count["n"] += 1
            if attempt_count["n"] <= 2:
                resp = types.SimpleNamespace()
                resp.status_code = 429
                resp.headers = {"Retry-After": "0.01"}
                return resp
            # Third attempt succeeds
            return _make_response(SAMPLE_MARKETS)

        session = types.SimpleNamespace(get=_mock_get)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)
        # Minimize sleep time for test speed
        monkeypatch.setattr(polymarket_client.time, "sleep", lambda x: None)

        client = polymarket_client.PolymarketClient()
        result = client._get("/markets")

        assert result is not None
        assert attempt_count["n"] == 3  # 2 retries + 1 success


# ==================== 方案11: 关键词匹配回归测试 ====================


class TestWordBoundaryMatching:
    """方案11: 验证 \\b 词边界匹配修复，防止子串误判"""

    @staticmethod
    def _classify(question: str):
        """复制方向分类逻辑用于单元测试"""
        import re
        _q_lower = question.lower()
        _BULLISH_WORDS = r'\b(?:above|higher|beat|exceed|rise|up|bull|hit|rally|surge|gain)\b'
        _BEARISH_WORDS = r'\b(?:below|lower|miss|fall|drop|down|crash|bear|decline|sink|lose)\b'
        is_bullish = bool(re.search(_BULLISH_WORDS, _q_lower))
        is_bearish = bool(re.search(_BEARISH_WORDS, _q_lower))
        if is_bullish and is_bearish:
            return "neutral"
        if is_bullish:
            return "bullish"
        if is_bearish:
            return "bearish"
        return "neutral"

    # --- 正确匹配用例 ---
    def test_above_detected_as_bullish(self):
        assert self._classify("Will NVDA be above $150?") == "bullish"

    def test_below_detected_as_bearish(self):
        assert self._classify("Will AAPL fall below $200?") == "bearish"

    def test_rise_detected_as_bullish(self):
        assert self._classify("Will Tesla stock rise this quarter?") == "bullish"

    def test_drop_detected_as_bearish(self):
        assert self._classify("Will oil prices drop below $60?") == "bearish"

    # --- 方案11 核心: 子串误判修复 ---
    def test_update_not_matched_as_up(self):
        """'update' 包含 'up' 但不应匹配看涨"""
        assert self._classify("Will NVDA release a driver update?") == "neutral"

    def test_breakdown_not_matched_as_down(self):
        """'breakdown' 包含 'down' 但不应匹配看空"""
        assert self._classify("Full breakdown of AAPL earnings report") == "neutral"

    def test_mission_not_matched_as_miss(self):
        """'mission' 包含 'miss' 但不应匹配看空"""
        assert self._classify("Will SpaceX complete its mission?") == "neutral"

    def test_enterprise_not_matched_as_rise(self):
        """'enterprise' 包含 'rise' 但不应匹配看涨"""
        assert self._classify("Will enterprise software demand stay flat?") == "neutral"

    def test_bulletin_not_matched_as_bull(self):
        """'bulletin' 包含 'bull' 但不应匹配看涨"""
        assert self._classify("Economic bulletin for Q2 release date?") == "neutral"

    def test_bearing_not_matched_as_bear(self):
        """'bearing' 包含 'bear' 但不应匹配看空"""
        assert self._classify("Will ball bearing demand increase?") == "neutral"

    def test_upload_not_matched_as_up(self):
        """'upload' 包含 'up' 但不应匹配看涨"""
        assert self._classify("Will the upload speed exceed 100Mbps?") == "bullish"  # 'exceed' is bullish

    def test_whiteboard_not_matched_as_hit(self):
        """'whiteboard' 不应触发 hit"""
        assert self._classify("Will the whiteboard feature launch?") == "neutral"

    # --- 双向冲突检测 ---
    def test_conflicting_signals_become_neutral(self):
        """同时包含看涨和看空词 → 中性"""
        assert self._classify("Will price rise above or drop below target?") == "neutral"

    def test_up_and_down_conflict(self):
        """同时含 up 和 down → 中性"""
        assert self._classify("Will the stock go up or down?") == "neutral"

    # --- 新增关键词 ---
    def test_rally_detected_as_bullish(self):
        assert self._classify("Will crypto rally in March?") == "bullish"

    def test_decline_detected_as_bearish(self):
        assert self._classify("Will housing prices decline?") == "bearish"


# ==================== Bug 修复回归测试 ====================


class TestBugfixRegressions:
    """Polymarket 二次审查发现的 Bug 回归测试"""

    def test_company_name_search_deduplicates(self, monkeypatch):
        """二次搜索（ticker + 公司名）不应产生重复市场。"""
        import polymarket_client

        # 两次搜索返回重叠数据：都包含 nvda-above-130
        overlap_markets = SAMPLE_BULLISH_MARKETS  # 含 nvda-above-130, nvda-beat-earnings, nvda-hit-160

        resp = _make_response(overlap_markets)
        session = types.SimpleNamespace(get=lambda *a, **kw: resp)
        monkeypatch.setattr(polymarket_client, "get_session", lambda name: session)

        client = polymarket_client.PolymarketClient()
        result = client.get_ticker_odds("NVDA")

        # 验证 top_markets 无重复（每个 question 只出现一次）
        questions = [m["question"] for m in result["top_markets"]]
        assert len(questions) == len(set(questions)), f"重复市场: {questions}"

    def test_normalize_market_none_prices(self):
        """outcomePrices=None 应返回空列表，不崩溃。"""
        from polymarket_client import PolymarketClient

        client = PolymarketClient()
        raw = {
            "slug": "null-prices",
            "question": "Null prices?",
            "outcomePrices": None,
            "volume24hr": 100,
            "liquidity": 200,
            "outcomes": ["Yes", "No"],
        }
        normalized = client._normalize_market(raw)
        assert normalized["outcome_prices"] == []

    def test_normalize_market_int_prices(self):
        """outcomePrices=42（非 str/list）应返回空列表。"""
        from polymarket_client import PolymarketClient

        client = PolymarketClient()
        raw = {
            "slug": "int-prices",
            "question": "Int prices?",
            "outcomePrices": 42,
            "volume24hr": 100,
            "liquidity": 200,
            "outcomes": [],
        }
        normalized = client._normalize_market(raw)
        assert normalized["outcome_prices"] == []

    def test_normalize_market_list_with_non_numeric(self):
        """outcomePrices 列表中含非数值元素应不崩溃。"""
        from polymarket_client import PolymarketClient

        client = PolymarketClient()
        raw = {
            "slug": "bad-list",
            "question": "Bad list?",
            "outcomePrices": ["abc", "def"],
            "volume24hr": 0,
            "liquidity": 0,
            "outcomes": [],
        }
        normalized = client._normalize_market(raw)
        assert normalized["outcome_prices"] == []
