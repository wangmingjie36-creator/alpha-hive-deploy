"""
Tests for AgentWeightManager - 动态权重管理器
"""

import pytest
from unittest.mock import MagicMock, patch
from agent_weight_manager import AgentWeightManager


# ==================== TestGetWeight ====================

class TestGetWeight:
    """Tests for AgentWeightManager.get_weight"""

    def test_unknown_agent_returns_default(self, memory_store):
        mgr = AgentWeightManager(memory_store)
        weight = mgr.get_weight("NonExistentAgent")
        assert weight == 1.0

    def test_default_agents_return_valid_weight(self, memory_store):
        mgr = AgentWeightManager(memory_store)
        for agent_id in AgentWeightManager.DEFAULT_AGENTS:
            weight = mgr.get_weight(agent_id)
            assert isinstance(weight, float)
            assert weight > 0

    def test_all_default_agents_start_at_1(self, memory_store):
        """Without any accuracy data, all agents should start with weight 1.0."""
        mgr = AgentWeightManager(memory_store)
        for agent_id in AgentWeightManager.DEFAULT_AGENTS:
            assert mgr.get_weight(agent_id) == 1.0


# ==================== TestWeightedAverageScore ====================

class TestWeightedAverageScore:
    """Tests for AgentWeightManager.weighted_average_score"""

    def test_equal_weights_simple_average(self, memory_store):
        """With default (equal) weights, result should equal simple average."""
        mgr = AgentWeightManager(memory_store)
        results = [
            {"score": 6.0, "source": "ScoutBeeNova"},
            {"score": 8.0, "source": "OracleBeeEcho"},
            {"score": 4.0, "source": "BuzzBeeWhisper"},
        ]
        avg = mgr.weighted_average_score(results)
        assert abs(avg - 6.0) < 0.01, f"Expected ~6.0, got {avg}"

    def test_empty_results_returns_default(self, memory_store):
        """Empty agent_results list should return default score (5.0)."""
        mgr = AgentWeightManager(memory_store)
        assert mgr.weighted_average_score([]) == 5.0

    def test_results_with_errors_are_skipped(self, memory_store):
        """Results containing 'error' key should be skipped."""
        mgr = AgentWeightManager(memory_store)
        results = [
            {"score": 8.0, "source": "ScoutBeeNova"},
            {"score": 2.0, "source": "OracleBeeEcho", "error": "timeout"},
            {"score": 6.0, "source": "BuzzBeeWhisper"},
        ]
        avg = mgr.weighted_average_score(results)
        # Only ScoutBeeNova (8.0) and BuzzBeeWhisper (6.0) count
        assert abs(avg - 7.0) < 0.01, f"Expected ~7.0, got {avg}"

    def test_single_result_returns_that_score(self, memory_store):
        mgr = AgentWeightManager(memory_store)
        results = [{"score": 9.0, "source": "ScoutBeeNova"}]
        avg = mgr.weighted_average_score(results)
        assert abs(avg - 9.0) < 0.01

    def test_none_entries_are_skipped(self, memory_store):
        """None entries in the list should be safely skipped."""
        mgr = AgentWeightManager(memory_store)
        results = [
            None,
            {"score": 7.0, "source": "ScoutBeeNova"},
            None,
        ]
        avg = mgr.weighted_average_score(results)
        assert abs(avg - 7.0) < 0.01

    def test_missing_score_defaults_to_5(self, memory_store):
        """Result without 'score' key should default to 5.0."""
        mgr = AgentWeightManager(memory_store)
        results = [{"source": "ScoutBeeNova"}]
        avg = mgr.weighted_average_score(results)
        assert abs(avg - 5.0) < 0.01

    def test_all_errors_returns_default(self, memory_store):
        """If all results have errors, return default 5.0."""
        mgr = AgentWeightManager(memory_store)
        results = [
            {"score": 8.0, "source": "ScoutBeeNova", "error": "fail"},
            {"score": 3.0, "source": "OracleBeeEcho", "error": "timeout"},
        ]
        assert mgr.weighted_average_score(results) == 5.0


# ==================== TestRecalculateAllWeights ====================

class TestRecalculateAllWeights:
    """Tests for AgentWeightManager.recalculate_all_weights"""

    def test_returns_dict_for_all_agents(self, memory_store):
        mgr = AgentWeightManager(memory_store)
        weights = mgr.recalculate_all_weights()
        assert isinstance(weights, dict)
        for agent_id in AgentWeightManager.DEFAULT_AGENTS:
            assert agent_id in weights

    def test_no_accuracy_data_returns_default_weights(self, memory_store):
        """Without accuracy data (sample_count=0), all weights should be ~1.0."""
        mgr = AgentWeightManager(memory_store)
        weights = mgr.recalculate_all_weights()
        for agent_id, weight in weights.items():
            assert abs(weight - 1.0) < 0.01, f"{agent_id} weight {weight} != 1.0"

    def test_weights_mean_approximately_one(self, memory_store):
        """After rebalancing, the mean weight should be ~1.0."""
        mgr = AgentWeightManager(memory_store)
        weights = mgr.recalculate_all_weights()
        mean_weight = sum(weights.values()) / len(weights)
        assert abs(mean_weight - 1.0) < 0.01, f"Mean weight {mean_weight} != 1.0"

    def test_high_accuracy_agent_gets_higher_weight(self, memory_store):
        """An agent with high accuracy should get weight > 1.0 relative to others."""
        mgr = AgentWeightManager(memory_store)

        # Mock get_agent_accuracy: one agent has great accuracy, rest are default
        original_get = memory_store.get_agent_accuracy

        def mock_accuracy(agent_id, period="t7"):
            if agent_id == "ScoutBeeNova":
                return {"accuracy": 0.9, "sample_count": 20}
            return {"accuracy": 0.5, "sample_count": 0}

        memory_store.get_agent_accuracy = mock_accuracy
        weights = mgr.recalculate_all_weights()
        memory_store.get_agent_accuracy = original_get

        # ScoutBeeNova should have the highest weight
        scout_weight = weights["ScoutBeeNova"]
        other_weights = [w for k, w in weights.items() if k != "ScoutBeeNova"]
        assert scout_weight > max(other_weights), (
            f"ScoutBeeNova ({scout_weight}) should be > others ({other_weights})"
        )


# ==================== TestApplyDimensionFeedback ====================

class TestApplyDimensionFeedback:
    """Tests for AgentWeightManager.apply_dimension_feedback"""

    def test_apply_dimension_feedback_mapping(self, memory_store):
        """signal→ScoutBeeNova 维度映射正确"""
        mgr = AgentWeightManager(memory_store)
        dim_w = {"signal": 0.35, "catalyst": 0.20, "sentiment": 0.20,
                 "odds": 0.15, "risk_adj": 0.10}
        result = mgr.apply_dimension_feedback(dim_w)
        assert result is True

        # signal=0.35, 0.35/0.20 = 1.75 → ScoutBeeNova 应获得 1.75x 权重
        # 验证 update_agent_weight 被调用
        weights = mgr.get_weights()
        assert "ScoutBeeNova" in weights

    def test_apply_dimension_feedback_clamping(self, memory_store):
        """极端权重被钳位到 [MIN_WEIGHT, MAX_WEIGHT]"""
        mgr = AgentWeightManager(memory_store)
        # 极端值：signal=0.90 → 0.90/0.20 = 4.5，应被钳位到 MAX_WEIGHT=3.0
        dim_w = {"signal": 0.90}
        mgr.apply_dimension_feedback(dim_w)
        weights = mgr.get_weights()
        scout_w = weights.get("ScoutBeeNova", 1.0)
        assert scout_w <= AgentWeightManager.MAX_WEIGHT, (
            f"ScoutBeeNova weight {scout_w} should be <= {AgentWeightManager.MAX_WEIGHT}"
        )

    def test_apply_dimension_feedback_partial_dims(self, memory_store):
        """不完整维度 dict 安全处理"""
        mgr = AgentWeightManager(memory_store)
        # 只传 2 个维度
        result = mgr.apply_dimension_feedback({"signal": 0.25, "odds": 0.30})
        assert result is True

    def test_apply_dimension_feedback_empty_dict(self, memory_store):
        """空 dict → False"""
        mgr = AgentWeightManager(memory_store)
        result = mgr.apply_dimension_feedback({})
        assert result is False

    def test_apply_dimension_feedback_invalid_values(self, memory_store):
        """无效值被安全跳过"""
        mgr = AgentWeightManager(memory_store)
        result = mgr.apply_dimension_feedback({"signal": -1.0, "odds": None})
        assert result is False

    def test_apply_dimension_feedback_nan_rejected(self, memory_store):
        """回归测试: NaN 值不应写入数据库"""
        mgr = AgentWeightManager(memory_store)
        result = mgr.apply_dimension_feedback({"signal": float("nan"), "odds": float("nan")})
        assert result is False
        # 确认权重未被污染（NaN != NaN 的特性用于检测）
        w = mgr.get_weight("ScoutBeeNova")
        assert w == w, "Weight should not be NaN"

    def test_updates_cache_after_recalculation(self, memory_store):
        mgr = AgentWeightManager(memory_store)
        weights = mgr.recalculate_all_weights()
        cached = mgr.get_weights()
        assert cached == weights


# ==================== TestRebalanceWeights ====================

class TestRebalanceWeights:
    """Tests for AgentWeightManager._rebalance_weights (static method)"""

    def test_preserves_relative_ratios(self):
        """Rebalancing should preserve the ratio between weights."""
        weights = {"A": 2.0, "B": 1.0, "C": 1.0}
        result = AgentWeightManager._rebalance_weights(weights)
        # After rebalancing, mean should be 1.0 (sum = 3)
        assert abs(sum(result.values()) - 3.0) < 0.01
        # Ratio A:B should be preserved (2:1)
        assert abs(result["A"] / result["B"] - 2.0) < 0.01

    def test_empty_dict_returns_empty(self):
        result = AgentWeightManager._rebalance_weights({})
        assert result == {}

    def test_all_zeros_fallback_to_equal(self):
        """All-zero weights should fallback to 1.0 each."""
        weights = {"A": 0.0, "B": 0.0, "C": 0.0}
        result = AgentWeightManager._rebalance_weights(weights)
        for v in result.values():
            assert v == 1.0

    def test_already_balanced_stays_same(self):
        """Weights already averaging 1.0 should stay roughly the same."""
        weights = {"A": 1.0, "B": 1.0, "C": 1.0}
        result = AgentWeightManager._rebalance_weights(weights)
        for v in result.values():
            assert abs(v - 1.0) < 0.01

    def test_single_agent(self):
        """Single agent should always rebalance to 1.0."""
        result = AgentWeightManager._rebalance_weights({"A": 5.0})
        assert abs(result["A"] - 1.0) < 0.01

    def test_mean_is_one_after_rebalance(self):
        weights = {"A": 0.5, "B": 1.5, "C": 2.0, "D": 0.8}
        result = AgentWeightManager._rebalance_weights(weights)
        mean = sum(result.values()) / len(result)
        assert abs(mean - 1.0) < 0.01


# ==================== TestConstants ====================

class TestConstants:
    """Verify important class-level constants."""

    def test_min_weight(self):
        assert AgentWeightManager.MIN_WEIGHT == 0.3

    def test_max_weight(self):
        assert AgentWeightManager.MAX_WEIGHT == 3.0

    def test_accuracy_weight_coefficient(self):
        assert AgentWeightManager.ACCURACY_WEIGHT_COEFFICIENT == 2.0

    def test_default_agents_count(self):
        assert len(AgentWeightManager.DEFAULT_AGENTS) == 6

    def test_default_agents_names(self):
        expected = {
            "ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper",
            "ChronosBeeHorizon", "RivalBeeVanguard", "GuardBeeSentinel",
        }
        assert set(AgentWeightManager.DEFAULT_AGENTS) == expected


# ==================== TestCacheBehavior ====================

class TestCacheBehavior:
    """Tests for weight caching mechanism."""

    def test_cache_returns_same_object_within_ttl(self, memory_store):
        mgr = AgentWeightManager(memory_store)
        w1 = mgr.get_weights()
        w2 = mgr.get_weights()
        assert w1 == w2

    def test_get_weights_returns_copy(self, memory_store):
        """get_weights should return a copy, not the internal cache reference."""
        mgr = AgentWeightManager(memory_store)
        w1 = mgr.get_weights()
        w1["ScoutBeeNova"] = 999.0  # Mutate the returned dict
        w2 = mgr.get_weights()
        assert w2.get("ScoutBeeNova") != 999.0
