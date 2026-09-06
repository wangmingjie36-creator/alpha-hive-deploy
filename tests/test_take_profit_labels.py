"""止盈档位的口径与标签不变式（v0.45.134）

固化两批事实。

一、标签说谎（Step 1，2026-09-06 生产实测）
------------------------------------------
旧实现价格按 `expected_gain × {0.3, 0.6, 1.0}` 算，`gain_pct` 却写死成 30 / 60，
而 `generate_ml_report` 把它直接渲染进标着「涨幅」的一列。本机 1045 份已发布
ML 报告里 **998 份**因此自相矛盾：价格单调递增、标签却是 30 → 60 → 20。
TMUS 2026-09-04（现价 $181.52）：level_1 $192.41 印 +30%，实为 +6.0%。

二、空头阶梯是倒的（Step 2 换数据源时暴露）
--------------------------------------------
`expected_7d` 的分位数是**原始收益**，没按方向调整。BILI 看空（T+7 中位 −5.67%）
若一律取 P50/P75/P90，level_3 会取到 P90=+3.89% —— 对空头那是**最差**情况。
必须按方向取：多头 P50/P75/P90，空头 P50/P25/P10；两者共同性质是「盈利递增」。

守四条不变式：
  1. `gain_pct` == 该档 `price` 相对现价的真实价格变动（可为负）
  2. `profit_pct` == 按持仓方向折算的盈利（空头 = −gain_pct）
  3. 三档的 `profit_pct` 必须**盈利递增**（多头价格递增、空头价格递减）
  4. 字段齐全且是数 —— 否则「把这一列删掉」也能让前三条全绿（成对断言）

全文件纯算术，不读库、不出网。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_analyzer import ProbabilityCalculator

LEVELS = ("level_1", "level_2", "level_3")

# 契约是「标签 == **印出去的那个价**所蕴含的变动」，唯一允许的误差就是标签自己
# 那一位小数（round(…, 1) ⇒ 0.05pp），**与价格高低无关**。
#
# ⚠️ 初版把容差写成 `0.005 / price * 100 + 0.06`（把价格的舍入误差也算了进去），
# 意在同时容纳「按公式重算标签」的实现。mutation check 的等价性证明推翻了它：
# 穷举反例 price=$0.81 / 目标 +6.17% / 30% 档 —— 公式实现标 +1.9%，而印出去的
# $0.82 其实是 +1.2%。那是同一个缺陷的小号版本，不该被容差放行。
_TOL_PP = 0.06

# (现价, 方向, [三档原始价格变动%])。分位值取自 pheromone.db 实测（2026-09-06）
CASES = [
    (181.52, "bullish", [0.01, 5.86, 10.12]),   # NVDA 看多 n=59
    (40.00, "bullish", [-2.15, 1.00, 4.96]),    # VKTX 看多 n=46，中位为负
    (25.00, "bearish", [-5.67, -7.10, -13.30]), # BILI 看空 n=22，价格递减
    (2.64, "bearish", [-1.00, -3.00, -9.00]),   # AMC 量级低价股 × 看空
    (100.0, "bullish", [0.0, 6.0, 12.0]),
    (250.0, "bullish", [0.0, 0.0, 0.0]),        # 边界：三档同值 ⇒ 标签都是 0.0
]


def _tp(price, direction, gains):
    return ProbabilityCalculator().calculate_take_profit_levels(price, gains, direction)


@pytest.mark.parametrize("price,direction,gains", CASES)
def test_gain_pct_matches_price(price, direction, gains):
    """不变式 1：gain_pct == 印出去的价所蕴含的价格变动"""
    tp = _tp(price, direction, gains)
    for key in LEVELS:
        lvl = tp[key]
        implied = (lvl["price"] / price - 1) * 100
        assert abs(lvl["gain_pct"] - implied) <= _TOL_PP, (
            f"{key}: 页面会印 {lvl['gain_pct']:+}%，但 ${lvl['price']} 相对现价 "
            f"${price} 其实是 {implied:+.2f}%（差 {abs(lvl['gain_pct']-implied):.1f}pp）"
        )


@pytest.mark.parametrize("price,direction,gains", CASES)
def test_profit_pct_is_direction_adjusted(price, direction, gains):
    """不变式 2：空头的价格下跌就是盈利 —— profit_pct 必须翻号"""
    tp = _tp(price, direction, gains)
    sign = 1.0 if direction == "bullish" else -1.0
    for key in LEVELS:
        lvl = tp[key]
        assert lvl["profit_pct"] == pytest.approx(sign * lvl["gain_pct"], abs=0.051), (
            f"{key}: {direction} 的价格变动 {lvl['gain_pct']:+}% 应折算成盈利 "
            f"{sign * lvl['gain_pct']:+}%，实得 {lvl['profit_pct']:+}%"
        )
        assert lvl["direction"] == direction


@pytest.mark.parametrize("price,direction,gains", CASES)
def test_profit_increases_across_levels(price, direction, gains):
    """不变式 3：三档盈利递增（生产旧表是 30→60→20；空头旧阶梯 level_3 取到最差档）"""
    tp = _tp(price, direction, gains)
    profits = [tp[k]["profit_pct"] for k in LEVELS]
    prices = [tp[k]["price"] for k in LEVELS]
    assert profits == sorted(profits), (
        f"{direction} 三档盈利 {profits} 不递增 —— 页面上就是「越靠后的档越不赚」"
    )
    expect = sorted(prices) if direction == "bullish" else sorted(prices, reverse=True)
    assert prices == expect, f"{direction} 的价格阶梯方向错了: {prices}"


@pytest.mark.parametrize("price,direction,gains", CASES)
def test_fields_present_and_numeric(price, direction, gains):
    """不变式 4（成对）：字段必须在、且是数 —— 防「把这一列删掉」式的假修复"""
    tp = _tp(price, direction, gains)
    for key in LEVELS:
        assert key in tp, f"缺档位 {key}"
        for field in ("price", "gain_pct", "profit_pct", "sell_ratio"):
            v = tp[key].get(field)
            assert isinstance(v, (int, float)) and not isinstance(v, bool), (
                f"{key}.{field} 必须是数，实得 {v!r}"
            )
        assert tp[key].get("reason"), f"{key} 缺 reason"


def test_rejects_wrong_arity():
    """档位数不匹配必须炸，不许静默截断/补齐"""
    with pytest.raises(ValueError):
        _tp(100.0, "bullish", [1.0, 2.0])


@pytest.mark.parametrize("bad", ["neutral", "", None, "long"])
def test_rejects_non_directional(bad):
    """中性/未知方向没有盈利方向可言，必须炸而不是默认按多头算"""
    with pytest.raises(ValueError):
        _tp(100.0, bad, [1.0, 2.0, 3.0])
