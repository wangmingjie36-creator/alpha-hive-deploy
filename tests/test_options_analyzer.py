"""OptionsAnalyzer + OptionsAgent 单元测试"""

import pytest
from options_analyzer import OptionsAnalyzer, OptionsAgent, OptionsDataFetcher


# ==================== OptionsAnalyzer 纯计算测试 ====================

class TestIVRank:
    def test_basic_iv_rank(self):
        analyzer = OptionsAnalyzer()
        hist = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]
        rank, iv = analyzer.calculate_iv_rank(0.40, hist)
        # (0.40 - 0.20) / (0.65 - 0.20) = 0.2/0.45 ≈ 44.44
        assert 44 <= rank <= 45
        assert iv == 0.40

    def test_iv_rank_at_extremes(self):
        analyzer = OptionsAnalyzer()
        hist = [0.20] * 10 + [0.80] * 10
        rank_low, _ = analyzer.calculate_iv_rank(0.20, hist)
        rank_high, _ = analyzer.calculate_iv_rank(0.80, hist)
        assert rank_low == 0.0
        assert rank_high == 100.0

    def test_iv_rank_insufficient_data(self):
        analyzer = OptionsAnalyzer()
        rank, _ = analyzer.calculate_iv_rank(0.30, [0.25, 0.35])
        assert rank == 50.0  # 数据不足，返回中立值

    def test_iv_rank_flat_history(self):
        analyzer = OptionsAnalyzer()
        rank, _ = analyzer.calculate_iv_rank(0.30, [0.30] * 20)
        assert rank == 50.0  # max == min 时返回中立


class TestIVPercentile:
    def test_basic_percentile(self):
        analyzer = OptionsAnalyzer()
        hist = list(range(10, 110, 10))  # [10, 20, 30, ..., 100]
        pct = analyzer.calculate_iv_percentile(55, hist)
        # 有 5 个低于 55: [10,20,30,40,50] → 50%
        assert pct == 50.0

    def test_percentile_at_min(self):
        analyzer = OptionsAnalyzer()
        hist = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        pct = analyzer.calculate_iv_percentile(10, hist)
        assert pct == 0.0  # 没有比 10 更低的

    def test_percentile_insufficient_data(self):
        analyzer = OptionsAnalyzer()
        pct = analyzer.calculate_iv_percentile(50, [40, 60])
        assert pct == 50.0


class TestPutCallRatio:
    def test_basic_ratio(self):
        analyzer = OptionsAnalyzer()
        calls = [{"openInterest": 1000}, {"openInterest": 500}]
        puts = [{"openInterest": 800}, {"openInterest": 400}]
        ratio = analyzer.calculate_put_call_ratio(calls, puts)
        # put_oi=1200, call_oi=1500 → ratio=0.8
        assert abs(ratio - 0.8) < 0.01

    def test_zero_calls(self):
        analyzer = OptionsAnalyzer()
        calls = [{"openInterest": 0}]
        puts = [{"openInterest": 100}]
        ratio = analyzer.calculate_put_call_ratio(calls, puts)
        assert ratio >= 0  # 不应崩溃

    def test_empty_chains(self):
        analyzer = OptionsAnalyzer()
        ratio = analyzer.calculate_put_call_ratio([], [])
        assert ratio == 1.0  # 默认中立


class TestGammaExposure:
    def test_basic_gex(self):
        analyzer = OptionsAnalyzer()
        calls = [{"strike": 150, "openInterest": 1000, "gamma": 0.05}]
        puts = [{"strike": 140, "openInterest": 800, "gamma": 0.03}]
        gex = analyzer.calculate_gamma_exposure(calls, puts, 145.0)
        assert isinstance(gex, float)

    def test_gex_empty_chains(self):
        analyzer = OptionsAnalyzer()
        gex = analyzer.calculate_gamma_exposure([], [], 145.0)
        assert gex == 0.0


class TestIVSkew:
    def test_skew_with_data(self):
        analyzer = OptionsAnalyzer()
        # 构造 OTM puts（strike < stock_price）和 OTM calls（strike > stock_price）
        calls = [{"strike": 160, "impliedVolatility": 0.30, "openInterest": 100}]
        puts = [{"strike": 140, "impliedVolatility": 0.45, "openInterest": 100}]
        result = analyzer.calculate_iv_skew(calls, puts, 150.0)
        assert "skew_ratio" in result
        # puts IV > calls IV → skew > 1
        assert result["skew_ratio"] > 1.0

    def test_skew_empty_data(self):
        analyzer = OptionsAnalyzer()
        result = analyzer.calculate_iv_skew([], [], 150.0)
        assert result.get("skew_ratio") is None or result.get("skew_signal") == ""


class TestOptionsScore:
    def test_score_range(self):
        analyzer = OptionsAnalyzer()
        score, summary = analyzer.generate_options_score(
            iv_rank=75, put_call_ratio=0.8, gex=0.001, unusual=[]
        )
        assert 0 <= score <= 10
        assert isinstance(summary, str)


# ==================== OptionsAgent 集成测试 ====================

class TestOptionsAgent:
    def test_analyze_returns_required_keys(self, monkeypatch):
        """OptionsAgent.analyze() 应返回所有必需字段"""
        agent = OptionsAgent()

        # Mock fetch 方法，避免真实 API 调用
        monkeypatch.setattr(
            agent.fetcher, "fetch_options_chain",
            lambda ticker: {
                "calls": [
                    {"strike": 140, "openInterest": 500, "impliedVolatility": 0.35, "gamma": 0.04},
                    {"strike": 150, "openInterest": 800, "impliedVolatility": 0.30, "gamma": 0.05},
                    {"strike": 160, "openInterest": 300, "impliedVolatility": 0.28, "gamma": 0.03},
                ],
                "puts": [
                    {"strike": 130, "openInterest": 400, "impliedVolatility": 0.40, "gamma": 0.03},
                    {"strike": 140, "openInterest": 600, "impliedVolatility": 0.38, "gamma": 0.04},
                    {"strike": 150, "openInterest": 200, "impliedVolatility": 0.32, "gamma": 0.05},
                ],
                "expirations": ["2026-03-20", "2026-04-17"],
                "source": "real",
            }
        )
        monkeypatch.setattr(
            agent.fetcher, "fetch_historical_iv",
            lambda ticker: [0.25 + i * 0.02 for i in range(20)]
        )
        # 隔离缓存：阻止测试写入/读取生产 last_valid_iv 缓存文件
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda ticker, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda ticker: None)

        result = agent.analyze("NVDA", stock_price=145.0)

        required_keys = [
            "ticker", "data_quality", "iv_rank", "iv_percentile",
            "put_call_ratio", "gamma_exposure", "options_score",
            "flow_direction", "iv_skew_ratio",
        ]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

        assert result["ticker"] == "NVDA"
        assert 0 <= result["iv_rank"] <= 100
        assert result["put_call_ratio"] > 0

    def test_analyze_sample_data_quality(self, monkeypatch):
        """样本数据应标记为 data_quality='unavailable'"""
        agent = OptionsAgent()
        monkeypatch.setattr(
            agent.fetcher, "fetch_options_chain",
            lambda ticker: {
                "calls": [{"strike": 150, "openInterest": 100, "impliedVolatility": 0.30, "gamma": 0.05}],
                "puts": [{"strike": 140, "openInterest": 100, "impliedVolatility": 0.35, "gamma": 0.04}],
                "expirations": [],
                "source": "sample",  # 标记为样本数据
            }
        )
        monkeypatch.setattr(
            agent.fetcher, "fetch_historical_iv",
            lambda ticker: [0.30] * 20
        )
        # 隔离缓存：阻止测试写入/读取生产 last_valid_iv 缓存文件
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda ticker, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda ticker: None)

        result = agent.analyze("TEST", stock_price=145.0)
        assert result["data_quality"] == "unavailable"


# ==================== OptionsDataFetcher 缓存测试 ====================

class TestOptionsDataFetcher:
    def test_cache_write_and_read(self, tmp_path):
        fetcher = OptionsDataFetcher(cache_dir=str(tmp_path))
        # 写入缓存
        fetcher._write_cache("NVDA", "chain", {"calls": [], "puts": []})
        # 读取缓存
        cached = fetcher._read_cache("NVDA", "chain")
        assert cached is not None
        assert "calls" in cached

    def test_cache_miss(self, tmp_path):
        fetcher = OptionsDataFetcher(cache_dir=str(tmp_path))
        cached = fetcher._read_cache("NVDA", "nonexistent")
        assert cached is None
