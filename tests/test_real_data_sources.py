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
    assert "polymarket_odds_change_24h" not in metrics  # v0.45.30 已移除动量伪装代理
    # v0.45.44：由 `== 0.0` 改为 `is None`。
    # 本测试的不变式是**不崩**（见函数名与 docstring），0.0 只是当时的兜底实现，
    # 且与本文件 docstring 自己引用的原则「不可得置 None 勿近似」直接矛盾——
    # 0.0 读作「5 日横盘」，是个合法可解读的假读数，而同 dict 的
    # data_quality["momentum"] 当时还硬编码自称 "real"。
    assert metrics["price_momentum_5d"] is None, "不可得应置 None，不得近似成 0.0"
    assert metrics["data_quality"]["momentum"] == "unavailable", \
        "质量标签必须由数据推导，不能写死 real"

    # 不崩的保证要一路验到消费端（这才是本测试的本意）
    from crowding_detector import CrowdingDetector
    _d = CrowdingDetector("TEST")
    _d.calculate_crowding_score(metrics)                       # 打分不崩
    _disp = _d._get_metric_display("short_squeeze_risk", metrics)  # 显示层不崩
    assert "—" in _disp, f"动量不可得时显示层应给 —，实得 {_disp!r}"


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
    assert "polymarket_odds_change_24h" not in metrics  # v0.45.30 已移除动量伪装代理
