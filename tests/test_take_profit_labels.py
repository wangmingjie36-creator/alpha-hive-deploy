"""止盈档位的「涨幅」标签必须等于价格本身蕴含的涨幅（v0.45.134 Step 1）

固化的缺陷（2026-09-06 查明，生产实测）
--------------------------------------
`ProbabilityCalculator.calculate_take_profit_levels` 的价格按
`expected_gain × {0.3, 0.6, 1.0}` 算，但 level_1 / level_2 的 `gain_pct`
**写死成 30 / 60**。而 `generate_ml_report` 把该字段直接渲染进标着「涨幅」
的一列（`+{gain_pct:.0f}%`）。

本机 1045 份已发布 ML 报告里，**998 份**的表是自相矛盾的——价格单调递增，
标签却是 30 → 60 → 20。TMUS 2026-09-04（现价 $181.52）实际渲染出：

    level_1  $192.41   页面写 +30%   实际 +6.0%
    level_2  $203.30   页面写 +60%   实际 +12.0%
    level_3  $217.82   页面写 +20%   实际 +20.0%   ← 只有这档是对的

标签误差中位 24pp。

这里守两条不变式（**与 expected_gain 从哪来无关**，故 Step 2 换数据源后依然有效）：
  1. 每一档的 `gain_pct` == 该档 `price` 相对现价的真实涨幅
  2. 标签的大小顺序必须与价格的大小顺序一致

⚠️ 成对：既断言「标签不许说谎」，也断言「字段必须存在且是数」——
否则「把这一列删掉」也能让第 1 条全绿。
全文件纯算术，不读库、不出网。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_analyzer import ProbabilityCalculator

LEVELS = ("level_1", "level_2", "level_3")

# (现价, 预期涨幅%)；含 AMC 量级的低价股——价格 round(…, 2) 在低价位放大相对误差
CASES = [
    (181.52, 20.0),   # TMUS 2026-09-04 生产实例
    (159.47, 20.0),   # XOM  2026-09-04 生产实例
    (2.64, 20.0),     # AMC  2026-09-04 生产实例（低价股）
    (100.0, 12.0),
    (50.0, 33.0),
    (250.0, 0.0),     # 边界：预期涨幅为 0 —— 三档标签都应是 0.0，不是 30/60/0
]


# 契约是「标签 == **印出去的那个价**所蕴含的涨幅」，所以唯一允许的误差就是标签
# 自己那一位小数（round(…, 1) ⇒ 0.05pp），**与价格高低无关**。
#
# ⚠️ 初版把容差写成 `0.005 / price * 100 + 0.06`（把价格的舍入误差也算了进去），
# 意在同时容纳「按公式重算标签」的实现。mutation check 的等价性证明推翻了这个设计：
# 穷举反例 price=$0.81 / 目标 +6.17% / 30% 档 —— 公式实现标 +1.9%，而印出去的
# $0.82 其实是 +1.2%。那是同一个缺陷的小号版本，不该被容差放行。
_TOL_PP = 0.06


@pytest.mark.parametrize("price,gain", CASES)
def test_label_matches_price(price, gain):
    """不变式 1：标签写的涨幅 == 价格蕴含的涨幅"""
    tp = ProbabilityCalculator().calculate_take_profit_levels(price, gain)
    tol = _TOL_PP
    for key in LEVELS:
        lvl = tp[key]
        implied = (lvl["price"] / price - 1) * 100
        assert abs(lvl["gain_pct"] - implied) <= tol, (
            f"{key}: 页面会印 +{lvl['gain_pct']}%，但 ${lvl['price']} 相对现价 "
            f"${price} 其实是 {implied:+.2f}%（差 {abs(lvl['gain_pct']-implied):.1f}pp）"
        )


@pytest.mark.parametrize("price,gain", CASES)
def test_label_field_present_and_numeric(price, gain):
    """成对：字段必须在、且是数——防「把这一列删掉」式的假修复"""
    tp = ProbabilityCalculator().calculate_take_profit_levels(price, gain)
    for key in LEVELS:
        assert key in tp, f"缺档位 {key}"
        g = tp[key].get("gain_pct")
        assert isinstance(g, (int, float)) and not isinstance(g, bool), (
            f"{key}.gain_pct 必须是数，实得 {g!r}"
        )
        assert isinstance(tp[key].get("price"), (int, float)), f"{key}.price 必须是数"


@pytest.mark.parametrize("price,gain", [c for c in CASES if c[1] > 0])
def test_labels_ordered_like_prices(price, gain):
    """不变式 2：价格递增 ⇒ 标签也必须递增（生产里 998/1045 份是 30→60→20）"""
    tp = ProbabilityCalculator().calculate_take_profit_levels(price, gain)
    prices = [tp[k]["price"] for k in LEVELS]
    labels = [tp[k]["gain_pct"] for k in LEVELS]
    assert prices == sorted(prices), f"价格本身就不递增: {prices}"
    assert labels == sorted(labels), (
        f"价格 {prices} 递增，标签 {labels} 却不递增——页面上就是「更低的价标着更高的涨幅」"
    )
