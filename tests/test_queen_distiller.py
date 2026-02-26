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
        from swarm_agents import QueenDistiller

        results = [_make_result("signal", 7.0)]

        # 无共振的基线
        queen_no_res = QueenDistiller(board)
        out_baseline = queen_no_res.distill("NVDA", results)

        # 制造共振：4 个同向 bullish 信号
        for i in range(4):
            board.publish(PheromoneEntry(
                agent_id=f"Agent{i}", ticker="NVDA",
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
        assert out["final_score"] == 5.0  # 默认中性
        assert out["direction"] == "neutral"

    def test_handles_none_results(self, queen):
        out = queen.distill("NVDA", [None, None])
        assert out["final_score"] == 5.0

    def test_handles_error_results(self, queen):
        results = [{"error": "API timeout", "source": "Scout", "score": 5.0, "dimension": "signal"}]
        out = queen.distill("NVDA", results)
        # error 结果应被过滤，不参与加权
        assert out["supporting_agents"] == 0
