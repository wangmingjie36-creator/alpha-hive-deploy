"""概率与止盈两节改读真实历史频率（v0.45.134 Step 2 / Step 4）

固化的事实（2026-09-06 生产实测，803 份 ML analysis JSON + pheromone.db 945 条）
------------------------------------------------------------------------------
`calculate_win_probability` = base 0.55 + 拥挤度常数 + 催化剂常数：
  · 生产 803 份报告里 **81% 恒等于 65.0**；34 只标的中 **31 只六个月一动不动**
  · 真实同方向命中率实测跨度 **37.0% ~ 81.8%**，11 个够样本的组合里 4 个 < 50%
    （VKTX 看多印 70~73%，实测 **37.0%**）
`_estimate_expected_gain` = NVDA 15 / VKTX 25 / 其他 12 + 拥挤度调整：
  · 生产 **76% 恒等于 20.0%**；真实 T+7 中位跨度 −5.67% ~ +6.82%（中位数 0.01%）
两者的拥挤度入参在当前流水线里**恒为 0** —— `realtime_metrics` 从不含
`crowding_input`（唯一生产它的 `data_fetcher.collect_all_metrics()` 无生产调用点），
而 0 落进两个函数的**最看多档**：一条数据都没拿到被当成了利好。

守四类不变式：
  1. 命中率：真值透传；缺失 / None / NaN / bool 一律 None，**不挑兜底值**
  2. 止盈分位按方向取：多头 P50/P75/P90、空头 P50/P25/P10；中性不可得
  3. 评级：命中率不可得 → UNRATED，**不是** HOLD（旧默认 50 恰好卡在 HOLD 闸上）
  4. 接线：不可得时 take_profit 是 None 而非 {}（`{}` 会被渲染成「有这节、只是空的」）

全文件不读库、不出网：直接喂 expected_returns 字典。
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_analyzer import AdvancedAnalyzer


@pytest.fixture
def a():
    """不跑 __init__ —— 它会构造 OptionsAgent / DealerGEX，那些要出网。"""
    return AdvancedAnalyzer.__new__(AdvancedAnalyzer)


def _er(**kw):
    base = {"basis": "same_direction", "sample_size": 40,
            "expected_7d": {"median": 0.0, "p10": -9.0, "p25": -4.0,
                            "p75": 5.0, "p90": 10.0}}
    base.update(kw)
    return base


# ── 1. 命中率守卫 ────────────────────────────────────────────────────
def test_hit_rate_passes_through_real_value(a):
    assert a._history_hit_rate("NVDA", _er(hit_rate_pct=50.8)) == 50.8


def test_hit_rate_zero_is_a_conclusion_not_missing(a):
    """成对：0.0% 命中率是「一次没赢过」这个明确结论，必须照常透传"""
    assert a._history_hit_rate("X", _er(hit_rate_pct=0.0)) == 0.0


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True, False, "50", {}])
def test_hit_rate_rejects_non_finite_and_bool(a, bad):
    """bool 是 int 的子类 —— True 会变成 1.0 混进来（v0.45.121 的教训）"""
    assert a._history_hit_rate("X", _er(hit_rate_pct=bad)) is None


def test_hit_rate_missing_key_is_none(a):
    assert a._history_hit_rate("X", _er()) is None
    assert a._history_hit_rate("X", {}) is None
    assert a._history_hit_rate("X", None) is None


# ── 2. 止盈分位按方向取 ──────────────────────────────────────────────
def test_take_profit_quantiles_bullish(a):
    gains, keys = a._take_profit_gains("X", _er(), "bullish")
    assert keys == ["median", "p75", "p90"]
    assert gains == [0.0, 5.0, 10.0]


def test_take_profit_quantiles_bearish_are_mirrored(a):
    """空头必须走 P50/P25/P10 —— 走 P90 会把**最差**情况当成最终止盈档"""
    gains, keys = a._take_profit_gains("X", _er(), "bearish")
    assert keys == ["median", "p25", "p10"]
    assert gains == [0.0, -4.0, -9.0]


@pytest.mark.parametrize("d", ["neutral", None, "", "long"])
def test_take_profit_needs_a_direction(a, d):
    assert a._take_profit_gains("X", _er(), d) is None


@pytest.mark.parametrize("key", ["median", "p75", "p90"])
def test_take_profit_any_missing_quantile_kills_the_section(a, key):
    """部分缺失比全缺更危险：少画一档和「这档没有」在页面上长得一样（v0.45.114）"""
    e7 = dict(_er()["expected_7d"])
    e7[key] = None
    assert a._take_profit_gains("X", _er(expected_7d=e7), "bullish") is None


def test_take_profit_rejects_non_monotonic_profit(a):
    """分位数乱序说明上游算坏了——宁可整节不可得，也不印「越靠后越不赚」的阶梯"""
    e7 = {"median": 5.0, "p75": 1.0, "p90": 10.0, "p25": -4.0, "p10": -9.0}
    assert a._take_profit_gains("X", _er(expected_7d=e7), "bullish") is None


def test_take_profit_rejects_bool_quantile(a):
    e7 = dict(_er()["expected_7d"]); e7["p75"] = True
    assert a._take_profit_gains("X", _er(expected_7d=e7), "bullish") is None


# ── 3. 评级：不可得不许卡在闸上 ──────────────────────────────────────
def test_unknown_hit_rate_is_unrated_not_hold(a):
    """旧实现 `.get("win_probability_pct", 50)` 的 50 恰好满足 HOLD 闸 `prob >= 50`"""
    out = a._generate_recommendation("X", {"probability_analysis":
                                           {"hit_rate_pct": None, "sample_size": 7}})
    assert out["rating"] == "UNRATED"
    assert out["confidence"] is None
    assert "n=7" in out["rationale"]


def test_unknown_rr_does_not_upgrade_rating(a):
    """成对：命中率够 STRONG BUY，但 rr 未知 ⇒ 只能停在 HOLD（v0.45.50）"""
    pa = {"hit_rate_pct": 81.8, "risk_reward_ratio": None,
          "sample_size": 22, "basis": "same_direction"}
    assert a._generate_recommendation("X", {"probability_analysis": pa})["rating"] == "HOLD"
    pa2 = dict(pa, risk_reward_ratio=2.5)
    assert a._generate_recommendation("X", {"probability_analysis": pa2})["rating"] == "STRONG BUY"


def test_low_hit_rate_is_avoid(a):
    """VKTX 看多实测 37.0% —— 旧实现印 70~73% 并给 HOLD"""
    pa = {"hit_rate_pct": 37.0, "risk_reward_ratio": 0.84,
          "sample_size": 46, "basis": "same_direction"}
    out = a._generate_recommendation("X", {"probability_analysis": pa})
    assert out["rating"] == "AVOID"
    assert "37.0%" in out["rationale"] and "n=46" in out["rationale"]


def test_rationale_never_calls_it_a_probability(a):
    """文案不许说「赚钱概率」——那是前瞻断言，这个数是样本内历史频率"""
    pa = {"hit_rate_pct": 60.0, "risk_reward_ratio": 1.8,
          "sample_size": 30, "basis": "same_direction"}
    r = a._generate_recommendation("X", {"probability_analysis": pa})["rationale"]
    assert "赚钱概率" not in r
    assert "命中率" in r and "n=30" in r
