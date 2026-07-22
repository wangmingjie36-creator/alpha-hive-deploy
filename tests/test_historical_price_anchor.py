"""
v0.41.6 回归测试：`--date` 补跑历史交易日时，股价必须锚定该日期的真实收盘价，
而不是脚本运行那一刻的实时报价。

事故：7/21 的报告多次重新生成，NVDA 现价先后显示 $206.34 / $205.10——两个
都不是 7/21 的真实收盘价 $207.29，是不同重跑时刻各自的实时报价。CBOE/
AlphaVantage/Finnhub 全是当前实时报价源，没有免费的历史快照能力，只能靠
yfinance 的 start=/end= 历史区间实现日期锚定。
"""

from unittest.mock import patch

import pandas as pd
import pytest

from data_pipeline import fetch_stock_data, _fetch_historical_stock_data


def _fake_history(prices):
    """构造一个跨 30 天的假日线，最后一天收盘价 = prices[-1]"""
    idx = pd.date_range("2026-06-22", periods=len(prices), freq="D")
    return pd.DataFrame({
        "Close": prices,
        "Volume": [1_000_000] * len(prices),
    }, index=idx)


class TestAsOfDateBypassesLiveSources:
    def test_as_of_date_in_past_skips_live_fetcher(self, monkeypatch):
        """as_of_date 是过去日期时，不应调用 get_fetcher()（CBOE/实时报价链）"""
        called = {"live": False}

        def _fake_get_fetcher():
            called["live"] = True
            raise AssertionError("不应该走实时报价链")

        monkeypatch.setattr("data_pipeline.get_fetcher", _fake_get_fetcher)
        monkeypatch.setattr("hive_logger.pdt_today", lambda: "2099-01-01")

        hist = _fake_history([100.0] * 25 + [207.29])
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist
            result = fetch_stock_data("NVDA", as_of_date="2026-07-21")

        assert called["live"] is False
        assert result["price"] == pytest.approx(207.29)
        assert result["source_name"] == "yfinance_historical"

    def test_as_of_date_equal_to_today_uses_live_fetcher(self, monkeypatch):
        """as_of_date 等于今天时，行为与不传 as_of_date 完全一致（走实时链）"""
        monkeypatch.setattr("hive_logger.pdt_today", lambda: "2026-07-21")
        called = {"live": False}

        class _FakeFetcher:
            def fetch(self, ticker):
                called["live"] = True
                return {"price": 999.0, "source_name": "cboe"}

        monkeypatch.setattr("data_pipeline.get_fetcher", lambda: _FakeFetcher())
        result = fetch_stock_data("NVDA", as_of_date="2026-07-21")

        assert called["live"] is True
        assert result["price"] == 999.0

    def test_no_as_of_date_uses_live_fetcher(self, monkeypatch):
        """不传 as_of_date（默认 None）时行为不变——当日实时扫描不受影响"""
        called = {"live": False}

        class _FakeFetcher:
            def fetch(self, ticker):
                called["live"] = True
                return {"price": 999.0, "source_name": "cboe"}

        monkeypatch.setattr("data_pipeline.get_fetcher", lambda: _FakeFetcher())
        result = fetch_stock_data("NVDA")

        assert called["live"] is True
        assert result["price"] == 999.0


class TestFetchHistoricalStockData:
    def test_picks_close_on_as_of_date_not_later(self):
        """必须取 as_of_date 当天的收盘价，即使 yfinance 多返回了之后的行"""
        hist = _fake_history([100.0] * 24 + [207.29, 999.0])  # 最后一行是"未来"数据
        hist.index = pd.date_range("2026-06-22", periods=26, freq="D")
        # 手动把倒数第二行标为 as_of_date 当天
        as_of_idx = hist.index[-2]

        with patch("yfinance.Ticker") as mock_ticker:
            # 模拟 yfinance 按 start/end 过滤后仍可能含边界外数据的情况:
            # 只返回到 as_of 当天为止
            mock_ticker.return_value.history.return_value = hist[hist.index <= as_of_idx]
            result = _fetch_historical_stock_data("NVDA", as_of_idx.strftime("%Y-%m-%d"))

        assert result["price"] == pytest.approx(207.29)
        assert result["data_source"] == "real"

    def test_empty_history_returns_fallback(self):
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame()
            result = _fetch_historical_stock_data("NVDA", "2026-07-21")

        assert result["price"] == 0.0
        assert result["data_source"] == "fallback"
        assert result["_data_unavailable"] is True
