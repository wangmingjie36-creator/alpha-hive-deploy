"""
v0.41.4 回归测试：get_real_crowding_metrics 对显式 None 值的加固

事故：2026-07-21 14:02 定时扫描，深夜限流导致 yfinance 历史K线拉取失败，
_fetch_history_metrics 按设计把 momentum_5d/volume_ratio 诚实置 None（而非
缺键，见 data_pipeline.py 的 "不可得置 None 勿近似"）。get_real_crowding_metrics
用 `.get(key, default)` 取值——这挡不住显式 None，`(None - 0.5)` 直接
TypeError，ScoutBeeNova 对全部标的返回泛化错误信息（"今日聪明钱动向"整节
显示 "Error: unsupported operand type(s) for -: 'NoneType' and 'float'"）。
"""

from real_data_sources import get_real_crowding_metrics


def test_none_volume_ratio_and_momentum_does_not_crash(monkeypatch):
    monkeypatch.setattr("real_data_sources.get_social_buzz",
                         lambda ticker: {"messages_per_day": 100, "data_quality": "real"})
    monkeypatch.setattr("real_data_sources.get_short_interest",
                         lambda ticker: {"short_pct_float": 0.05, "data_quality": "real"})
    monkeypatch.setattr("real_data_sources.get_bullish_agents_count",
                         lambda ticker, board: 2)

    stock_data = {"momentum_5d": None, "volume_ratio": None, "price": 100.0}
    metrics = get_real_crowding_metrics("TEST", stock_data, board=None)

    assert metrics["google_trends_percentile"] == 36.0  # 中性代理值 (1.0 - 0.5)/2.5*80+20
    assert metrics["polymarket_odds_change_24h"] == 0.0
    assert metrics["price_momentum_5d"] == 0.0


def test_normal_values_unaffected(monkeypatch):
    monkeypatch.setattr("real_data_sources.get_social_buzz",
                         lambda ticker: {"messages_per_day": 100, "data_quality": "real"})
    monkeypatch.setattr("real_data_sources.get_short_interest",
                         lambda ticker: {"short_pct_float": 0.05, "data_quality": "real"})
    monkeypatch.setattr("real_data_sources.get_bullish_agents_count",
                         lambda ticker, board: 2)

    stock_data = {"momentum_5d": 5.0, "volume_ratio": 2.0, "price": 100.0}
    metrics = get_real_crowding_metrics("TEST", stock_data, board=None)

    assert metrics["price_momentum_5d"] == 5.0
    assert metrics["polymarket_odds_change_24h"] == round(abs(5.0) * 0.8, 2)
