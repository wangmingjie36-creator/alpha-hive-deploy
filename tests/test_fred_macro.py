"""
Tests for fred_macro module — 宏观经济数据 + 收益率曲线 + 板块轮动
"""

import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np


@pytest.fixture(autouse=True)
def _clear_fred_cache():
    """每个测试后自动清理 fred_macro 缓存，防止测试污染"""
    yield
    import fred_macro
    fred_macro._CACHE = {}
    fred_macro._CACHE_TS = 0.0
    fred_macro._etf_cache = {}


def _mock_yf_ticker(symbol, close_values):
    """创建模拟 yfinance Ticker 对象"""
    mock = MagicMock()
    df = pd.DataFrame({
        "Close": close_values,
        "Open": close_values,
        "High": [v * 1.01 for v in close_values],
        "Low": [v * 0.99 for v in close_values],
        "Volume": [1000000] * len(close_values),
    })
    mock.history.return_value = df
    return mock


class TestGetMacroContextFallback:
    """yfinance 不可用时的降级行为"""

    def test_fallback_when_yfinance_missing(self, monkeypatch):
        """yfinance import 失败时返回 fallback"""
        import fred_macro
        # 清除缓存
        fred_macro._CACHE = {}
        fred_macro._CACHE_TS = 0.0

        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "yfinance":
                raise ImportError("mocked")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        result = fred_macro._fetch_macro_data()
        assert result["data_source"] == "fallback"
        assert result["macro_regime"] == "neutral"
        assert result["macro_score"] == 5.0


class TestMacroRiskAdjustment:
    """get_macro_risk_adjustment() 范围测试"""

    def test_risk_off_spike(self):
        from fred_macro import get_macro_risk_adjustment
        adj, desc = get_macro_risk_adjustment({
            "macro_regime": "risk_off",
            "vix_regime": "spike",
            "vix": 45.0,
        })
        assert adj == -2.0
        assert "极度风险厌恶" in desc

    def test_risk_on(self):
        from fred_macro import get_macro_risk_adjustment
        adj, desc = get_macro_risk_adjustment({
            "macro_regime": "risk_on",
            "vix_regime": "low",
            "summary": "VIX 12.0(low)",
        })
        assert adj == +1.0
        assert "顺风" in desc

    def test_neutral_normal(self):
        from fred_macro import get_macro_risk_adjustment
        adj, desc = get_macro_risk_adjustment({
            "macro_regime": "neutral",
            "vix_regime": "moderate",
        })
        assert adj == 0.0
        assert "中性" in desc

    def test_adjustment_range(self):
        """所有返回值应在 [-2.0, +1.5] 范围"""
        from fred_macro import get_macro_risk_adjustment
        test_cases = [
            {"macro_regime": "risk_off", "vix_regime": "spike", "vix": 50},
            {"macro_regime": "risk_off", "vix_regime": "high", "summary": ""},
            {"macro_regime": "risk_on", "vix_regime": "low", "summary": ""},
            {"macro_regime": "neutral", "vix_regime": "high", "vix": 35},
            {"macro_regime": "neutral", "vix_regime": "moderate"},
        ]
        for case in test_cases:
            adj, _ = get_macro_risk_adjustment(case)
            assert -2.0 <= adj <= 1.5, f"adj={adj} out of range for {case}"


class TestYieldCurve:
    """收益率曲线计算测试"""

    def test_yield_curve_inverted(self, monkeypatch):
        """2Y > 10Y → inverted"""
        import fred_macro
        fred_macro._CACHE = {}
        fred_macro._CACHE_TS = 0.0

        def mock_ticker(sym):
            closes = {
                "^VIX": [20.0, 20.0, 20.0, 20.0, 20.0],
                "^TNX": [3.8, 3.8, 3.8, 3.8, 3.8],       # 10Y = 3.8%
                "^FVX": [4.2, 4.2, 4.2, 4.2, 4.2],       # 5Y = 4.2% → approx 2Y = 4.35%
                "DX-Y.NYB": [104, 104, 104, 104, 104],
                "^GSPC": [5000, 5010, 5020, 5030, 5040],
                "TLT": [90, 90, 90, 90, 90],
            }
            return _mock_yf_ticker(sym, closes.get(sym, [100] * 5))

        mock_yf = MagicMock()
        mock_yf.Ticker = mock_ticker
        monkeypatch.setattr(fred_macro, "_load_fred_key", lambda: "")

        # 直接 patch _fetch_macro_data 内的 yfinance import
        with patch.dict("sys.modules", {"yfinance": mock_yf}):
            # 禁用板块轮动（避免额外 yfinance 调用）
            with patch.object(fred_macro, "_fetch_sector_rotation", return_value={"hot": [], "cold": [], "full": {}}):
                result = fred_macro._fetch_macro_data()

        assert result.get("yield_curve") == "inverted"
        assert result.get("yield_spread") is not None
        assert result["yield_spread"] < 0

    def test_yield_curve_normal(self, monkeypatch):
        """10Y >> 2Y → normal"""
        import fred_macro
        fred_macro._CACHE = {}
        fred_macro._CACHE_TS = 0.0

        def mock_ticker(sym):
            closes = {
                "^VIX": [15.0] * 5,
                "^TNX": [4.5] * 5,       # 10Y = 4.5%
                "^FVX": [3.8] * 5,       # 5Y = 3.8% → approx 2Y = 3.95%
                "DX-Y.NYB": [103] * 5,
                "^GSPC": [5000, 5050, 5100, 5150, 5200],
                "TLT": [88] * 5,
            }
            return _mock_yf_ticker(sym, closes.get(sym, [100] * 5))

        mock_yf = MagicMock()
        mock_yf.Ticker = mock_ticker
        monkeypatch.setattr(fred_macro, "_load_fred_key", lambda: "")

        with patch.dict("sys.modules", {"yfinance": mock_yf}):
            with patch.object(fred_macro, "_fetch_sector_rotation", return_value={"hot": [], "cold": [], "full": {}}):
                result = fred_macro._fetch_macro_data()

        assert result.get("yield_curve") == "normal"
        assert result["yield_spread"] > 20


class TestGoldTrend:
    """黄金趋势计算测试"""

    def test_gold_surging(self, monkeypatch):
        """金价日涨 >1% → surging，加入逆风"""
        import fred_macro

        def mock_ticker(sym):
            closes = {
                "^VIX": [15.0] * 5,
                "^TNX": [4.5] * 5,
                "^FVX": [3.8] * 5,
                "DX-Y.NYB": [103] * 5,
                "^GSPC": [5000, 5050, 5100, 5150, 5200],
                "TLT": [88] * 5,
                "GLD": [200.0, 200.0, 200.0, 200.0, 204.0],  # +2%
            }
            return _mock_yf_ticker(sym, closes.get(sym, [100] * 5))

        mock_yf = MagicMock()
        mock_yf.Ticker = mock_ticker
        monkeypatch.setattr(fred_macro, "_load_fred_key", lambda: "")

        with patch.dict("sys.modules", {"yfinance": mock_yf}):
            with patch.object(fred_macro, "_fetch_sector_rotation", return_value={"hot": [], "cold": [], "full": {}}):
                result = fred_macro._fetch_macro_data()

        assert result.get("gold_trend") == "surging"
        assert result.get("gold_change_pct") > 1.0
        hw_text = " ".join(result.get("macro_headwinds", []))
        assert "黄金飙升" in hw_text

    def test_gold_rising(self, monkeypatch):
        """金价日涨 0.3-1% → rising，加入逆风"""
        import fred_macro

        def mock_ticker(sym):
            closes = {
                "^VIX": [15.0] * 5,
                "^TNX": [4.5] * 5,
                "^FVX": [3.8] * 5,
                "DX-Y.NYB": [103] * 5,
                "^GSPC": [5000, 5050, 5100, 5150, 5200],
                "TLT": [88] * 5,
                "GLD": [200.0, 200.0, 200.0, 200.0, 201.5],  # +0.75%
            }
            return _mock_yf_ticker(sym, closes.get(sym, [100] * 5))

        mock_yf = MagicMock()
        mock_yf.Ticker = mock_ticker
        monkeypatch.setattr(fred_macro, "_load_fred_key", lambda: "")

        with patch.dict("sys.modules", {"yfinance": mock_yf}):
            with patch.object(fred_macro, "_fetch_sector_rotation", return_value={"hot": [], "cold": [], "full": {}}):
                result = fred_macro._fetch_macro_data()

        assert result.get("gold_trend") == "rising"
        assert 0.3 < result.get("gold_change_pct", 0) <= 1.0
        hw_text = " ".join(result.get("macro_headwinds", []))
        assert "黄金走强" in hw_text

    def test_gold_falling(self, monkeypatch):
        """金价日跌 >1% → falling，加入顺风"""
        import fred_macro

        def mock_ticker(sym):
            closes = {
                "^VIX": [15.0] * 5,
                "^TNX": [4.5] * 5,
                "^FVX": [3.8] * 5,
                "DX-Y.NYB": [103] * 5,
                "^GSPC": [5000, 5050, 5100, 5150, 5200],
                "TLT": [88] * 5,
                "GLD": [204.0, 204.0, 204.0, 204.0, 200.0],  # -1.96%
            }
            return _mock_yf_ticker(sym, closes.get(sym, [100] * 5))

        mock_yf = MagicMock()
        mock_yf.Ticker = mock_ticker
        monkeypatch.setattr(fred_macro, "_load_fred_key", lambda: "")

        with patch.dict("sys.modules", {"yfinance": mock_yf}):
            with patch.object(fred_macro, "_fetch_sector_rotation", return_value={"hot": [], "cold": [], "full": {}}):
                result = fred_macro._fetch_macro_data()

        assert result.get("gold_trend") == "falling"
        assert result.get("gold_change_pct") < -1.0
        tw_text = " ".join(result.get("macro_tailwinds", []))
        assert "黄金回落" in tw_text

    def test_gold_stable(self, monkeypatch):
        """金价波动 <0.3% → stable，不参与顺逆风"""
        import fred_macro

        def mock_ticker(sym):
            closes = {
                "^VIX": [15.0] * 5,
                "^TNX": [4.5] * 5,
                "^FVX": [3.8] * 5,
                "DX-Y.NYB": [103] * 5,
                "^GSPC": [5000, 5050, 5100, 5150, 5200],
                "TLT": [88] * 5,
                "GLD": [200.0, 200.0, 200.0, 200.0, 200.2],  # +0.1%
            }
            return _mock_yf_ticker(sym, closes.get(sym, [100] * 5))

        mock_yf = MagicMock()
        mock_yf.Ticker = mock_ticker
        monkeypatch.setattr(fred_macro, "_load_fred_key", lambda: "")

        with patch.dict("sys.modules", {"yfinance": mock_yf}):
            with patch.object(fred_macro, "_fetch_sector_rotation", return_value={"hot": [], "cold": [], "full": {}}):
                result = fred_macro._fetch_macro_data()

        assert result.get("gold_trend") == "stable"
        # stable 时不应出现黄金相关的顺逆风
        all_winds = " ".join(result.get("macro_headwinds", []) + result.get("macro_tailwinds", []))
        assert "黄金" not in all_winds


class TestSectorRotation:
    """板块轮动测试"""

    def test_hot_cold_sorting(self):
        """验证 hot/cold 排序正确"""
        from fred_macro import _fetch_sector_rotation

        def mock_ticker(sym):
            perf_map = {
                "XLK": [100, 101, 102, 103, 105],   # +5%
                "XLV": [100, 100, 100, 100, 103],    # +3%
                "XLE": [100, 101, 102, 103, 104],    # +4%
                "XLF": [100, 99, 98, 97, 96],        # -4%
                "XLI": [100, 100, 100, 100, 99],     # -1%
                "XLY": [100, 100, 100, 100, 100],    # 0%
                "XLP": [100, 100, 100, 100, 101],    # +1%
                "XLU": [100, 99, 98, 97, 95],        # -5%
                "XLRE": [100, 99, 99, 98, 97],       # -3%
                "XLC": [100, 100, 101, 101, 102],    # +2%
                "XLB": [100, 100, 100, 100, 100.5],  # +0.5%
            }
            return _mock_yf_ticker(sym, perf_map.get(sym, [100] * 5))

        mock_yf = MagicMock()
        mock_yf.Ticker = mock_ticker

        result = _fetch_sector_rotation(mock_yf)
        assert "hot" in result
        assert "cold" in result
        assert len(result["hot"]) == 3
        assert len(result["cold"]) == 3
        # top3 should be XLK, XLE, XLV (5%, 4%, 3%)
        hot_etfs = [h[0] for h in result["hot"]]
        assert hot_etfs[0] == "XLK"  # highest
        # bottom3 should be XLU, XLF, XLRE (-5%, -4%, -3%)
        cold_etfs = [c[0] for c in result["cold"]]
        assert cold_etfs[-1] == "XLU"  # lowest is last

    def test_empty_when_no_data(self):
        """yfinance 全部失败时返回空"""
        from fred_macro import _fetch_sector_rotation

        mock_yf = MagicMock()
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_yf.Ticker.return_value = mock_ticker

        result = _fetch_sector_rotation(mock_yf)
        assert result["hot"] == []
        assert result["cold"] == []

    def test_few_etfs_no_overlap(self):
        """<6 个 ETF 时 hot/cold 不应重叠"""
        from fred_macro import _fetch_sector_rotation

        def mock_ticker(sym):
            # 只有 3 个 ETF 返回有效数据
            perf_map = {
                "XLK": [100, 101, 102, 103, 105],   # +5%
                "XLF": [100, 99, 98, 97, 96],        # -4%
                "XLV": [100, 100, 100, 100, 101],    # +1%
            }
            if sym in perf_map:
                return _mock_yf_ticker(sym, perf_map[sym])
            # 其余 ETF 返回空数据
            m = MagicMock()
            m.history.return_value = pd.DataFrame()
            return m

        mock_yf = MagicMock()
        mock_yf.Ticker = mock_ticker

        result = _fetch_sector_rotation(mock_yf)
        hot_set = {h[0] for h in result["hot"]}
        cold_set = {c[0] for c in result["cold"]}
        # 关键断言：无重叠
        assert hot_set & cold_set == set(), f"hot/cold 重叠: {hot_set & cold_set}"
        # 确保有输出
        assert len(result["hot"]) >= 1
        assert len(result["cold"]) >= 1


class TestGetSectorEtfForTicker:
    """get_sector_etf_for_ticker() 测试"""

    def test_known_ticker(self, monkeypatch):
        from fred_macro import get_sector_etf_for_ticker
        # NVDA is Technology → XLK
        etf = get_sector_etf_for_ticker("NVDA")
        assert etf == "XLK"

    def test_healthcare_ticker(self):
        from fred_macro import get_sector_etf_for_ticker
        etf = get_sector_etf_for_ticker("VKTX")
        assert etf == "XLV"

    def test_unknown_ticker(self):
        from fred_macro import get_sector_etf_for_ticker
        etf = get_sector_etf_for_ticker("UNKNOWN_TICKER_XYZ")
        assert etf == ""


class TestCacheTTL:
    """缓存 TTL 测试"""

    def test_cache_prevents_refetch(self, monkeypatch):
        """缓存有效期内不重复拉取"""
        import fred_macro
        import time

        fred_macro._CACHE = {"macro_regime": "neutral", "test": True}
        fred_macro._CACHE_TS = time.time()  # 刚刚缓存

        result = fred_macro.get_macro_context()
        assert result.get("test") is True  # 返回缓存版本
        # 清理由 autouse fixture _clear_fred_cache 自动完成
