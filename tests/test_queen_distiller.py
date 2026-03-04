"""QueenDistiller 集成测试 - 5 维加权评分 + 共振 + confidence"""

import pytest
from swarm_agents import QueenDistiller


def _make_result(dim, score, direction="bullish", confidence=0.8, source="TestAgent"):
    return {
        "score": score,
        "direction": direction,
        "confidence": confidence,
        "discovery": f"test {dim}",
        "source": source,
        "dimension": dim,
        "data_quality": {"test": "real"},
    }


class TestDistill:
    def test_basic_distill(self, queen):
        results = [
            _make_result("signal", 8.0),
            _make_result("catalyst", 7.0),
            _make_result("sentiment", 6.0),
            _make_result("odds", 7.5),
            _make_result("risk_adj", 8.0),
        ]
        out = queen.distill("NVDA", results)

        assert out["ticker"] == "NVDA"
        assert 0.0 <= out["final_score"] <= 10.0
        assert out["direction"] in ("bullish", "bearish", "neutral")
        assert out["supporting_agents"] == 5

    def test_output_has_required_fields(self, queen):
        results = [_make_result("signal", 7.0)]
        out = queen.distill("NVDA", results)

        required = [
            "ticker", "final_score", "direction", "resonance",
            "supporting_agents", "agent_breakdown", "dimension_scores",
            "dimension_confidence", "dimension_weights", "data_quality",
            "data_real_pct", "distill_mode",
        ]
        for field in required:
            assert field in out, f"缺少字段: {field}"

    def test_confidence_weighting(self, queen):
        """低 confidence 应将评分拉向 5.0"""
        high_conf = [_make_result("signal", 9.0, confidence=1.0)]
        low_conf = [_make_result("signal", 9.0, confidence=0.1)]

        out_high = queen.distill("NVDA", high_conf)
        out_low = queen.distill("NVDA", low_conf)

        # 高 confidence 时评分更接近原始 9.0
        # 低 confidence 时评分被拉向 5.0
        assert out_high["final_score"] > out_low["final_score"]

    def test_majority_vote_bullish(self, queen):
        results = [
            _make_result("signal", 8.0, direction="bullish"),
            _make_result("catalyst", 7.0, direction="bullish"),
            _make_result("sentiment", 6.0, direction="bearish"),
            _make_result("odds", 7.0, direction="bullish"),
            _make_result("risk_adj", 7.0, direction="neutral"),
        ]
        out = queen.distill("NVDA", results)
        assert out["direction"] == "bullish"
        assert out["agent_breakdown"]["bullish"] == 3

    def test_majority_vote_bearish(self, queen):
        results = [
            _make_result("signal", 3.0, direction="bearish"),
            _make_result("catalyst", 4.0, direction="bearish"),
            _make_result("sentiment", 3.0, direction="bearish"),
            _make_result("odds", 5.0, direction="neutral"),
            _make_result("risk_adj", 4.0, direction="neutral"),
        ]
        out = queen.distill("NVDA", results)
        assert out["direction"] == "bearish"

    def test_resonance_boosts_score(self, board):
        """共振应提升评分（对比无共振基线）"""
        from pheromone_board import PheromoneEntry

        results = [_make_result("signal", 7.0)]

        # 无共振的基线
        queen_no_res = QueenDistiller(board)
        out_baseline = queen_no_res.distill("NVDA", results)

        # 制造共振：4 个不同维度的同向 bullish 信号（P2a：需要 ≥3 不同维度）
        for agent in ["ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon"]:
            board.publish(PheromoneEntry(
                agent_id=agent, ticker="NVDA",
                discovery="test", source="test",
                self_score=8.0, direction="bullish",
            ))

        queen_res = QueenDistiller(board)
        out_boosted = queen_res.distill("NVDA", results)

        assert out_boosted["resonance"]["resonance_detected"]
        # 共振后评分 > 无共振基线
        assert out_boosted["final_score"] > out_baseline["final_score"]

    def test_ml_auxiliary_adjustment(self, queen):
        """ML 辅助分应调整最终评分"""
        base_results = [_make_result("signal", 7.0)]
        ml_high = base_results + [_make_result("ml_auxiliary", 9.0)]
        ml_low = base_results + [_make_result("ml_auxiliary", 2.0)]

        out_high = queen.distill("NVDA", ml_high)
        out_low = queen.distill("NVDA", ml_low)

        assert out_high["final_score"] > out_low["final_score"]

    def test_data_quality_aggregation(self, queen):
        results = [
            _make_result("signal", 7.0, source="ScoutBeeNova"),
            _make_result("sentiment", 6.0, source="BuzzBeeWhisper"),
        ]
        out = queen.distill("NVDA", results)

        assert out["data_real_pct"] > 0
        assert "ScoutBeeNova" in out["data_quality"]

    def test_handles_empty_results(self, queen):
        out = queen.distill("NVDA", [])
        assert out["final_score"] == pytest.approx(5.0, abs=0.2)  # 默认中性
        assert out["direction"] == "neutral"

    def test_handles_none_results(self, queen):
        out = queen.distill("NVDA", [None, None])
        assert out["final_score"] == pytest.approx(5.0, abs=0.2)

    def test_handles_error_results(self, queen):
        results = [{"error": "API timeout", "source": "Scout", "score": 5.0, "dimension": "signal"}]
        out = queen.distill("NVDA", results)
        # error 结果应被过滤，不参与加权
        assert out["supporting_agents"] == 0

    # ── NA3：置信度加权投票 ──────────────────────────────────────────────
    def test_na3_weighted_vote_high_conf_wins(self, queen):
        """2个高置信度看多应胜过3个低置信度看空（NA3）"""
        results = [
            _make_result("signal",    8.5, direction="bullish", confidence=0.9),
            _make_result("catalyst",  8.0, direction="bullish", confidence=0.85),
            _make_result("sentiment", 3.0, direction="bearish", confidence=0.2),
            _make_result("odds",      3.5, direction="bearish", confidence=0.2),
            _make_result("risk_adj",  4.0, direction="bearish", confidence=0.25),
        ]
        out = queen.distill("NVDA", results)
        vw = out["direction_vote_weights"]
        # 加权后多方应占优
        assert vw["bullish"] > vw["bearish"], "高置信度看多应比低置信度看空权重更高"
        assert out["rule_direction"] == "bullish"

    def test_na3_vote_weights_present(self, queen):
        """返回结果应包含 direction_vote_weights 字段（NA3）"""
        out = queen.distill("NVDA", [_make_result("signal", 7.0)])
        assert "direction_vote_weights" in out
        vw = out["direction_vote_weights"]
        assert set(vw.keys()) == {"bullish", "bearish", "neutral"}

    # ── NA4：GuardBeeSentinel 风险关门 ─────────────────────────────────
    def test_na4_guard_penalty_triggered(self, queen):
        """GuardBee score < 4.0 时应触发折扣（NA4）"""
        results = [
            _make_result("signal",   9.0, direction="bullish"),
            _make_result("risk_adj", 1.5, direction="neutral"),
        ]
        out = queen.distill("NVDA", results)
        assert out["guard_penalty_applied"] is True
        assert out["guard_penalty"] > 0.0

    def test_na4_guard_no_penalty_above_threshold(self, queen):
        """GuardBee score >= 4.0 时不应触发折扣（NA4）"""
        results = [
            _make_result("signal",   8.0, direction="bullish"),
            _make_result("risk_adj", 5.0, direction="neutral"),
        ]
        out = queen.distill("NVDA", results)
        assert out["guard_penalty_applied"] is False
        assert out["guard_penalty"] == 0.0

    def test_na4_guard_penalty_scales_with_score(self, queen):
        """GuardBee 分越低，折扣越大（NA4）"""
        def _penalty(guard_score):
            r = [_make_result("signal", 8.0), _make_result("risk_adj", guard_score)]
            return queen.distill("TEST", r)["guard_penalty"]

        assert _penalty(3.0) < _penalty(1.0), "guard=1.0 应比 guard=3.0 折扣更大"

    # ── NA1：维度状态 ────────────────────────────────────────────────────
    def test_na1_dimension_status_present(self, queen):
        """有效结果对应维度应为 present（NA1）"""
        out = queen.distill("NVDA", [_make_result("signal", 7.0)])
        assert out["dimension_status"]["signal"] == "present"
        assert out["dimension_coverage_pct"] > 0

    def test_na1_dimension_status_absent(self, queen):
        """未返回维度应为 absent（NA1）"""
        out = queen.distill("NVDA", [_make_result("signal", 7.0)])
        # catalyst/odds/sentiment/risk_adj 均未提供
        assert out["dimension_status"]["catalyst"] == "absent"
        assert out["dimension_status"]["odds"] == "absent"
