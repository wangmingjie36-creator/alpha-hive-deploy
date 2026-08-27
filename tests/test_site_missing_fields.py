"""网站补全字段的渲染守卫（v0.45.52）。

背景
----
IV-RV 价差 / 30 日实现波动率 / IV Skew 比值 / SPY 同期基准，**数据一直都在**
（2026-08-26 实测 30/30 覆盖），只是从未渲染到日报页与仪表板。

守什么
------
1. 有数据就渲染，**缺就整行省略** —— 不用 0 或 5.0 兜底。
   0 价差是「IV 恰等于 RV」，0 基准是「大盘恰好没动」，
   与「没算出来」完全是两回事（v0.45.42「缺失值不许冒充 0」同一原则）。
2. IV Rank 必须同行标 `iv_rank_source`：`hv_proxy` 与真实 IV 历史算出来的
   是两个东西（见 MEMORY「IV Rank 口径」），不标就分不出来。
3. SPY 基准的样本数必须与 `total_checked` 同口径（同 cutoff、同模糊样本排除）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_formatters as rf  # noqa: E402


def _row(**over):
    det = {
        "iv_rank": 47.41, "iv_rank_source": "hv_proxy",
        "put_call_ratio": 0.48, "gamma_exposure": 9.111,
        "iv_rv_spread": 0.83, "iv_rv_signal": "fair",
        "iv_rv_detail": {"rv_30d": 25.6, "iv_rv_spread": 0.83, "iv_rv_signal": "fair"},
        "iv_skew_ratio": 1.09, "iv_skew_signal": "neutral",
    }
    det.update(over)
    return [("ABBV", {"agent_details": {"OracleBeeEcho": {"discovery": "d", "details": det}}})]


def _md(**over):
    return "\n".join(rf._build_market_expectations(_row(**over)))


# ══════════════════════════════════════════════════════════════════
# 日报页：市场隐含预期
# ══════════════════════════════════════════════════════════════════

def test_all_new_fields_rendered():
    m = _md()
    assert "IV-RV 价差：+0.8pp（定价合理）" in m
    assert "30 日实现波动率（RV30）：25.6%" in m
    assert "IV Skew 比值：1.09（neutral）" in m


def test_iv_rank_carries_source():
    """`hv_proxy` 与真实 IV 历史是两个东西，不标就分不出来。"""
    assert "IV Rank：47.41（来源 hv_proxy）" in _md()


@pytest.mark.parametrize("missing", ["iv_rv_spread", "iv_skew_ratio"])
def test_missing_field_omits_line_not_zero(missing):
    """缺就整行省略，不许出现 0.0 / 0.00 之类的兜底值。"""
    over = {missing: None}
    if missing == "iv_rv_spread":
        over["iv_rv_detail"] = {"rv_30d": 25.6}
    else:
        over["iv_skew_detail"] = {}
    m = _md(**over)
    label = {"iv_rv_spread": "IV-RV 价差", "iv_skew_ratio": "IV Skew 比值"}[missing]
    assert label not in m, f"缺失时仍渲染了 {label}"
    assert "+0.0pp" not in m and "：0.00" not in m


def test_missing_rv30_omits_line():
    m = _md(iv_rv_detail={"iv_rv_spread": 0.83, "iv_rv_signal": "fair"})
    assert "30 日实现波动率" not in m
    assert "IV-RV 价差" in m, "不该因为 RV30 缺失就连带丢掉 IV-RV"


# ══════════════════════════════════════════════════════════════════
# 日报页：SPY 同期基准
# ══════════════════════════════════════════════════════════════════

_BT = {"overall_accuracy": 0.534, "total_checked": 118, "correct_count": 63,
       "avg_return": -0.25, "by_ticker": {}}


def test_spy_benchmark_rendered_with_excess():
    st = dict(_BT, spy_avg_return=-0.566, spy_sample_n=118)
    m = "\n".join(rf._build_backtest(st))
    assert "SPY 同期基准**：-0.57%（118 条同口径样本）" in m
    assert "超额**：+0.32pp" in m or "超额**：+0.31pp" in m


def test_spy_absent_omits_line_not_zero():
    """取不到就整行省略 —— 0 是「大盘恰好没动」，不是「没算出来」。"""
    m = "\n".join(rf._build_backtest(dict(_BT, spy_avg_return=None)))
    assert "SPY 同期基准" not in m
    assert "超额" not in m


def test_spy_zero_is_a_real_value_and_renders():
    """反向：真的算出 0.00% 时必须照常渲染，不能被当成缺失。"""
    m = "\n".join(rf._build_backtest(dict(_BT, spy_avg_return=0.0, spy_sample_n=50)))
    assert "SPY 同期基准**：+0.00%（50 条同口径样本）" in m


# ══════════════════════════════════════════════════════════════════
# 仪表板卡片
# ══════════════════════════════════════════════════════════════════

def test_dashboard_detail_exposes_new_fields():
    import dashboard_renderer as dr
    sd = {"ABBV": {"agent_details": {"OracleBeeEcho": {"details": {
        "iv_rank": 47.4, "iv_rv_spread": 0.83, "iv_rv_signal": "fair",
        "iv_rv_detail": {"rv_30d": 25.6}}}}}}
    d = dr._detail("ABBV", sd)
    assert d["iv_rv_spread"] == "+0.8pp"
    assert d["rv_30d"] == "25.6%"


def test_dashboard_detail_missing_shows_dash():
    import dashboard_renderer as dr
    d = dr._detail("ZZZZ", {"ZZZZ": {"agent_details": {"OracleBeeEcho": {"details": {}}}}})
    assert d["iv_rv_spread"] == "-" and d["rv_30d"] == "-", "缺失时应显示 '-' 而非 0"
