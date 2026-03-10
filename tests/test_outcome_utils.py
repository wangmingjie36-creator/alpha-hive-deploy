"""
方案12: outcome_utils 单元测试 — 验证统一正确性判定逻辑
"""

import pytest
from outcome_utils import determine_correctness, determine_correctness_bool


class TestDetermineCorrectness:
    """验证 determine_correctness 函数"""

    # --- 看多方向 ---
    def test_bullish_positive_return_correct(self):
        assert determine_correctness("bullish", 5.0) == "correct"

    def test_long_positive_return_correct(self):
        """Long / bullish 均应被识别"""
        assert determine_correctness("Long", 5.0) == "correct"

    def test_bullish_small_loss_within_tolerance(self):
        """看多，-0.5% 在 1% 容差内 → correct"""
        assert determine_correctness("bullish", -0.5) == "correct"

    def test_bullish_loss_beyond_tolerance(self):
        """看多，-2% 超出 1% 容差 → incorrect"""
        assert determine_correctness("bullish", -2.0) == "incorrect"

    def test_bullish_exact_boundary(self):
        """看多，-1.0% 不满足 > -1.0 → incorrect"""
        assert determine_correctness("bullish", -1.0) == "incorrect"

    # --- 看空方向 ---
    def test_bearish_negative_return_correct(self):
        assert determine_correctness("bearish", -5.0) == "correct"

    def test_short_negative_return_correct(self):
        assert determine_correctness("Short", -5.0) == "correct"

    def test_bearish_small_gain_within_tolerance(self):
        """看空，+0.5% 在 1% 容差内 → correct"""
        assert determine_correctness("bearish", 0.5) == "correct"

    def test_bearish_gain_beyond_tolerance(self):
        """看空，+2% 超出 1% 容差 → incorrect"""
        assert determine_correctness("bearish", 2.0) == "incorrect"

    def test_bearish_exact_boundary(self):
        """看空，+1.0% 不满足 < 1.0 → incorrect"""
        assert determine_correctness("bearish", 1.0) == "incorrect"

    # --- 中性方向 ---
    def test_neutral_small_move_correct(self):
        """中性，±2% 在 3% 容差内 → correct"""
        assert determine_correctness("neutral", 2.0) == "correct"

    def test_neutral_large_move_incorrect(self):
        """中性，±5% 超出 3% 容差 → incorrect"""
        assert determine_correctness("neutral", 5.0) == "incorrect"

    def test_neutral_exact_boundary(self):
        """中性，3.0% 不满足 < 3.0 → incorrect"""
        assert determine_correctness("neutral", 3.0) == "incorrect"

    def test_neutral_negative_large_move_incorrect(self):
        assert determine_correctness("neutral", -4.0) == "incorrect"

    # --- None / 未知 ---
    def test_none_return_is_neutral(self):
        assert determine_correctness("bullish", None) == "neutral"

    def test_unknown_direction_is_neutral(self):
        assert determine_correctness("sideways", 5.0) == "neutral"

    # --- 自定义容差 ---
    def test_custom_tolerance_0(self):
        """零容差：看多 -0.1% → incorrect"""
        assert determine_correctness("bullish", -0.1, tolerance_pct=0.0) == "incorrect"

    def test_custom_tolerance_5(self):
        """5% 容差：看多 -4% → correct"""
        assert determine_correctness("bullish", -4.0, tolerance_pct=5.0) == "correct"

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


class TestCaseInsensitive:
    """验证大小写不敏感"""

    def test_upper_long(self):
        assert determine_correctness("LONG", 5.0) == "correct"

    def test_mixed_case_bearish(self):
        assert determine_correctness("Bearish", -5.0) == "correct"

    def test_padded_direction(self):
        assert determine_correctness("  bullish  ", 5.0) == "correct"
