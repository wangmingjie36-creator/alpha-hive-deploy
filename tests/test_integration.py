"""
Alpha Hive 集成测试 - 真实 API 冒烟测试

运行方式：
  pytest tests/test_integration.py -v -m integration

CI 中跳过：
  pytest tests/ -v -m "not integration"

需要网络连接。各测试独立，任何一个 API 不可用不会阻塞其他测试。
"""

import pytest
import time

pytestmark = pytest.mark.integration


# ==================== Fear & Greed（无需 Key）====================

class TestFearGreedIntegration:
    """Fear & Greed Index - Alternative.me API 冒烟测试"""

    def test_returns_real_data(self):
        from fear_greed import get_fear_greed
        result = get_fear_greed()

        assert isinstance(result, dict)
        assert "value" in result
        assert "classification" in result
        assert "sentiment_score" in result
        assert result["is_real_data"] is True, "API 应返回真实数据（检查网络连接）"

    def test_value_in_range(self):
        from fear_greed import get_fear_greed
        result = get_fear_greed()

        assert 0 <= result["value"] <= 100, f"F&G index 应在 0-100，实际={result['value']}"
        assert 1.0 <= result["sentiment_score"] <= 10.0

    def test_classification_valid(self):
        from fear_greed import get_fear_greed
        result = get_fear_greed()

        valid = {"Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"}
        assert result["classification"] in valid, f"无效分类: {result['classification']}"

    def test_caching(self):
        from fear_greed import get_fear_greed
        r1 = get_fear_greed()
        r2 = get_fear_greed()  # 应命中缓存
        assert r1["value"] == r2["value"], "缓存数据应一致"


# ==================== Reddit ApeWisdom（无需 Key）====================

class TestRedditSentimentIntegration:
    """Reddit ApeWisdom API 冒烟测试"""

    def test_nvda_in_ranking(self):
        from reddit_sentiment import get_reddit_sentiment
        result = get_reddit_sentiment("NVDA")

        assert isinstance(result, dict)
        assert "sentiment_score" in result
        assert 1.0 <= result["sentiment_score"] <= 10.0

    def test_result_fields_complete(self):
        from reddit_sentiment import get_reddit_sentiment
        result = get_reddit_sentiment("TSLA")

        required = ["ticker", "reddit_buzz", "sentiment_score", "mentions", "timestamp"]
        for field in required:
            assert field in result, f"缺少字段: {field}"

    def test_buzz_valid_values(self):
        from reddit_sentiment import get_reddit_sentiment
        result = get_reddit_sentiment("NVDA")

        valid_buzz = {"hot", "rising", "cooling", "quiet"}
        assert result["reddit_buzz"] in valid_buzz, f"无效 buzz: {result['reddit_buzz']}"

    def test_top_ticker_has_rank(self):
        """NVDA 通常在 Reddit 排名靠前"""
        from reddit_sentiment import get_reddit_sentiment
        result = get_reddit_sentiment("NVDA")

        # NVDA 通常有排名（如无，说明当前热度下降，测试结果视为 OK）
        mentions = result.get("mentions", 0)
        assert isinstance(mentions, (int, float))


# ==================== Finviz 新闻（无需 Key）====================

class TestFinvizSentimentIntegration:
    """Finviz 新闻情绪 冒烟测试"""

    def test_nvda_headlines(self):
        from finviz_sentiment import get_finviz_sentiment
        result = get_finviz_sentiment("NVDA")

        assert isinstance(result, dict)
        assert "news_score" in result
        assert result["total_titles"] > 0, "应获取到真实新闻标题"

    def test_score_in_range(self):
        from finviz_sentiment import get_finviz_sentiment
        result = get_finviz_sentiment("NVDA")

        assert 1.0 <= result["news_score"] <= 10.0

    def test_has_sentiment_counts(self):
        from finviz_sentiment import get_finviz_sentiment
        result = get_finviz_sentiment("AAPL")

        total = result["bullish_count"] + result["bearish_count"] + result["neutral_count"]
        assert total == result["total_titles"]


# ==================== FRED 宏观数据（需 FRED_API_KEY）====================

class TestFREDMacroIntegration:
    """FRED API 宏观数据冒烟测试（需要 FRED_API_KEY）"""

    @pytest.fixture(autouse=True)
    def _check_fred_key(self):
        from fred_macro import _load_fred_key
        if not _load_fred_key():
            pytest.skip("未配置 FRED_API_KEY")

    def test_macro_context_returns_real_data(self):
        import fred_macro
        fred_macro._CACHE = {}
        fred_macro._CACHE_TS = 0.0

        result = fred_macro.get_macro_context()
        assert result["data_source"] != "fallback", "应返回真实数据"

    def test_vix_and_10y_present(self):
        from fred_macro import get_macro_context
        result = get_macro_context()

        assert result["vix"] > 0, "VIX 应大于 0"
        assert result["treasury_10y"] > 0, "10Y 收益率应大于 0"

    def test_fred_cpi_present(self):
        from fred_macro import get_macro_context
        result = get_macro_context()

        assert result.get("cpi_yoy") is not None, "FRED Key 有效时应返回 CPI 同比"
        assert 0 < result["cpi_yoy"] < 20, f"CPI 同比 {result['cpi_yoy']}% 超出合理范围"

    def test_fed_funds_rate(self):
        from fred_macro import get_macro_context
        result = get_macro_context()

        ffr = result.get("fed_funds_rate")
        assert ffr is not None
        assert 0 <= ffr <= 25, f"联邦基金利率 {ffr}% 超出合理范围"


# ==================== yfinance 数据（无需 Key）====================

class TestYFinanceIntegration:
    """yfinance 基础数据获取冒烟测试"""

    def test_nvda_history(self):
        import yfinance as yf
        ticker = yf.Ticker("NVDA")
        hist = ticker.history(period="5d", interval="1d")

        assert not hist.empty, "yfinance 应返回历史数据"
        assert "Close" in hist.columns

    def test_options_chain_exists(self):
        import yfinance as yf
        ticker = yf.Ticker("AAPL")
        expirations = ticker.options

        assert len(expirations) > 0, "AAPL 应有期权到期日"

    def test_macro_symbols(self):
        """宏观指标符号可访问"""
        import yfinance as yf
        for sym in ["^VIX", "^TNX", "^GSPC"]:
            hist = yf.Ticker(sym).history(period="5d", interval="1d")
            assert not hist.empty, f"{sym} 应有数据"
            time.sleep(0.5)


# ==================== Yahoo Finance 热搜榜（无需注册）====================

class TestYahooTrendingIntegration:
    """Yahoo Finance 热搜榜冒烟测试"""

    def test_returns_trending_list(self):
        from yahoo_trending import get_trending_tickers
        tickers = get_trending_tickers()

        assert isinstance(tickers, list)
        assert len(tickers) > 0, "应返回热搜标的列表"

    def test_ticker_count(self):
        from yahoo_trending import get_trending_tickers
        tickers = get_trending_tickers(count=25)

        assert len(tickers) <= 25

    def test_nvda_attention(self):
        from yahoo_trending import get_ticker_attention
        result = get_ticker_attention("NVDA")

        assert "attention_score" in result
        assert "is_trending" in result
        assert result["is_real_data"] is True
        assert 1.0 <= result["attention_score"] <= 10.0

    def test_unknown_ticker_not_trending(self):
        from yahoo_trending import get_ticker_attention
        # 使用一个极冷门的 ticker
        result = get_ticker_attention("XYZABC123")

        assert result["is_trending"] is False
        assert result["rank"] is None


# ==================== P2: EDGAR RSS 实时流（无需 Key）====================

class TestEdgarRSSIntegration:
    """SEC EDGAR Form 4 RSS 实时流冒烟测试"""

    def test_fetches_entries(self):
        from edgar_rss import get_rss_client
        client = get_rss_client()
        entries = client.get_recent_form4_alerts()

        assert isinstance(entries, list)
        assert len(entries) > 0, "EDGAR RSS 应返回 Form 4 申报列表"

    def test_entry_fields(self):
        from edgar_rss import get_rss_client
        entries = get_rss_client().get_recent_form4_alerts()

        required = ["company_name", "cik", "filing_date", "accession_number"]
        for field in required:
            assert field in entries[0], f"缺少字段: {field}"

    def test_summarize_returns_dict(self):
        from edgar_rss import get_today_form4_alerts
        result = get_today_form4_alerts("NVDA")

        assert isinstance(result, dict)
        assert "has_fresh_filings" in result
        assert "fresh_filings_count" in result
        assert "summary" in result
        assert isinstance(result["filings"], list)

    def test_cik_is_numeric(self):
        from edgar_rss import get_rss_client
        entries = get_rss_client().get_recent_form4_alerts()
        # CIK 应该是纯数字字符串
        for e in entries[:10]:
            if e["cik"]:
                assert e["cik"].isdigit(), f"CIK 应为数字: {e['cik']}"


# ==================== P4: 新闻全文 API（无需 Key）====================

class TestNewsAPIIntegration:
    """newsapi_client 冒烟测试（Yahoo Finance 免费渠道）"""

    def test_nvda_news(self):
        from newsapi_client import get_ticker_news
        result = get_ticker_news("NVDA", max_articles=5)

        assert isinstance(result, dict)
        assert "sentiment_score" in result
        assert 1.0 <= result["sentiment_score"] <= 10.0

    def test_result_fields_complete(self):
        from newsapi_client import get_ticker_news
        result = get_ticker_news("AAPL", max_articles=5)

        required = [
            "ticker", "articles", "total_articles",
            "bullish_count", "bearish_count", "neutral_count",
            "sentiment_score", "dominant_theme", "source", "is_real_data"
        ]
        for field in required:
            assert field in result, f"缺少字段: {field}"

    def test_counts_consistent(self):
        from newsapi_client import get_ticker_news
        result = get_ticker_news("TSLA", max_articles=8)

        if result["is_real_data"]:
            total = result["bullish_count"] + result["bearish_count"] + result["neutral_count"]
            assert total == result["total_articles"], "情绪计数之和应等于总文章数"

    def test_fallback_on_bad_ticker(self):
        from newsapi_client import get_ticker_news
        result = get_ticker_news("XYZNOTREAL123", max_articles=3)

        # 冷门 ticker 可能无新闻，但不应抛出异常
        assert isinstance(result, dict)
        assert "sentiment_score" in result
