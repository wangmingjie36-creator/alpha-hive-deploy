"""
方案12: outcome_utils 单元测试 — 验证统一正确性判定逻辑

v0.45.9 P0 更新：方向判定由「单边亏损豁免」改为「双边模糊带」。
旧语义 看多 correct if return > -1.0（亏 0.9% 记为判对）；
新语义 |return| <= tolerance → "ambiguous"，超出容差后才按符号判对错。
本文件相应改写了所有落在容差带内的断言，并新增 triplet 测试。
"""

import pytest
from outcome_utils import (
    determine_correctness,
    determine_correctness_bool,
    determine_outcome_triplet,
)


class TestDetermineCorrectness:
    """验证 determine_correctness 函数"""

    # --- 看多方向 ---
    def test_bullish_positive_return_correct(self):
        assert determine_correctness("bullish", 5.0) == "correct"

    def test_long_positive_return_correct(self):
        """Long / bullish 均应被识别"""
        assert determine_correctness("Long", 5.0) == "correct"

    def test_bullish_small_loss_within_tolerance(self):
        """看多，-0.5% 落在 ±1% 模糊带内 → ambiguous（v0.45.9 前为 correct）"""
        assert determine_correctness("bullish", -0.5) == "ambiguous"

    def test_bullish_small_gain_within_tolerance(self):
        """看多，+0.5% 同样在模糊带内 → ambiguous（双边对称）"""
        assert determine_correctness("bullish", 0.5) == "ambiguous"

    def test_bullish_loss_beyond_tolerance(self):
        """看多，-2% 超出模糊带且方向相反 → incorrect"""
        assert determine_correctness("bullish", -2.0) == "incorrect"

    def test_bullish_exact_boundary(self):
        """看多，恰好 -1.0% 落在闭区间边界 → ambiguous"""
        assert determine_correctness("bullish", -1.0) == "ambiguous"
        assert determine_correctness("bullish", 1.0) == "ambiguous"

    # --- 看空方向 ---
    def test_bearish_negative_return_correct(self):
        assert determine_correctness("bearish", -5.0) == "correct"

    def test_short_negative_return_correct(self):
        assert determine_correctness("Short", -5.0) == "correct"

    def test_bearish_small_gain_within_tolerance(self):
        """看空，+0.5% 落在模糊带内 → ambiguous（v0.45.9 前为 correct）"""
        assert determine_correctness("bearish", 0.5) == "ambiguous"

    def test_bearish_gain_beyond_tolerance(self):
        """看空，+2% 超出模糊带且方向相反 → incorrect"""
        assert determine_correctness("bearish", 2.0) == "incorrect"

    def test_bearish_exact_boundary(self):
        """看空，恰好 ±1.0% 落在闭区间边界 → ambiguous"""
        assert determine_correctness("bearish", 1.0) == "ambiguous"
        assert determine_correctness("bearish", -1.0) == "ambiguous"

    # --- 中性方向（v0.38.1: 带宽 3% → 5%，依据 neutral_band_replay 回放） ---
    def test_neutral_small_move_correct(self):
        """中性，±2% 在 5% 容差内 → correct"""
        assert determine_correctness("neutral", 2.0) == "correct"

    def test_neutral_mid_move_correct(self):
        """中性，±4% 在 5% 容差内 → correct（v0.38.1 前为 incorrect）"""
        assert determine_correctness("neutral", 4.0) == "correct"
        assert determine_correctness("neutral", -4.0) == "correct"

    def test_neutral_large_move_incorrect(self):
        """中性，±7% 超出 5% 容差 → incorrect"""
        assert determine_correctness("neutral", 7.0) == "incorrect"

    def test_neutral_exact_boundary(self):
        """中性，5.0% 不满足 < 5.0 → incorrect"""
        assert determine_correctness("neutral", 5.0) == "incorrect"

    def test_neutral_negative_large_move_incorrect(self):
        assert determine_correctness("neutral", -6.0) == "incorrect"

    # --- None / 未知 ---
    def test_none_return_is_neutral(self):
        assert determine_correctness("bullish", None) == "neutral"

    def test_unknown_direction_is_neutral(self):
        assert determine_correctness("sideways", 5.0) == "neutral"

    # --- 自定义容差 ---
    def test_custom_tolerance_0(self):
        """零容差：看多 -0.1% → incorrect（模糊带宽度为 0）"""
        assert determine_correctness("bullish", -0.1, tolerance_pct=0.0) == "incorrect"

    def test_custom_tolerance_5(self):
        """5% 模糊带：看多 -4% 落在带内 → ambiguous（v0.45.9 前为 correct）"""
        assert determine_correctness("bullish", -4.0, tolerance_pct=5.0) == "ambiguous"
        assert determine_correctness("bullish", -6.0, tolerance_pct=5.0) == "incorrect"
        assert determine_correctness("bullish", 6.0, tolerance_pct=5.0) == "correct"

    def test_custom_neutral_tolerance(self):
        """自定义中性容差 5%: ±4% → correct"""
        assert determine_correctness("neutral", 4.0, neutral_tolerance_pct=5.0) == "correct"


class TestDetermineCorrectnessBool:
    """验证布尔版本"""

    def test_bullish_correct_true(self):
        assert determine_correctness_bool("bullish", 5.0) is True

    def test_bullish_incorrect_false(self):
        assert determine_correctness_bool("bullish", -5.0) is False

    def test_neutral_within_tolerance_true(self):
        assert determine_correctness_bool("neutral", 1.0) is True

    def test_neutral_beyond_tolerance_false(self):
        assert determine_correctness_bool("neutral", 5.0) is False

    def test_unknown_direction_false(self):
        """未知方向 → neutral → not correct → False"""
        assert determine_correctness_bool("unknown", 5.0) is False

    def test_ambiguous_is_false(self):
        """v0.45.9：模糊样本在 bool 版本下返回 False，需 triplet 才能区分"""
        assert determine_correctness_bool("bullish", -0.5) is False


class TestDetermineOutcomeTriplet:
    """v0.45.9 P0：落库用的 (correct, ambiguous) 二元组"""

    def test_clear_win(self):
        assert determine_outcome_triplet("bullish", 5.0) == (True, False)

    def test_clear_loss(self):
        assert determine_outcome_triplet("bullish", -5.0) == (False, False)

    def test_bearish_clear_win(self):
        assert determine_outcome_triplet("bearish", -5.0) == (True, False)

    def test_bearish_clear_loss(self):
        assert determine_outcome_triplet("bearish", 5.0) == (False, False)

    def test_small_loss_is_ambiguous_not_correct(self):
        """核心回归：亏 0.9% 绝不能记为判对"""
        assert determine_outcome_triplet("bullish", -0.9) == (False, True)

    def test_small_gain_is_ambiguous(self):
        assert determine_outcome_triplet("bullish", 0.9) == (False, True)

    def test_bearish_small_adverse_move_is_ambiguous(self):
        assert determine_outcome_triplet("bearish", 0.9) == (False, True)

    def test_neutral_direction_never_ambiguous(self):
        """中性预测走自己的带宽判定，不产生 ambiguous"""
        assert determine_outcome_triplet("neutral", 2.0) == (True, False)
        assert determine_outcome_triplet("neutral", 9.0) == (False, False)

    def test_none_return(self):
        assert determine_outcome_triplet("bullish", None) == (False, False)


class TestCaseInsensitive:
    """验证大小写不敏感"""

    def test_upper_long(self):
        assert determine_correctness("LONG", 5.0) == "correct"

    def test_mixed_case_bearish(self):
        assert determine_correctness("Bearish", -5.0) == "correct"

    def test_padded_direction(self):
        assert determine_correctness("  bullish  ", 5.0) == "correct"
