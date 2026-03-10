"""QueenDistiller 集成测试 - 5 维加权评分 + 共振 + confidence + 三增强"""

import pytest
from unittest.mock import MagicMock
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

    # ── 方案9: data_quality_grade 测试 ─────────────────────────────
    def test_data_quality_grade_normal(self, queen):
        """5/5 维度 → grade=normal"""
        results = [
            _make_result("signal", 7.0), _make_result("catalyst", 6.0),
            _make_result("sentiment", 8.0), _make_result("odds", 7.0),
            _make_result("risk_adj", 5.0),
        ]
        out = queen.distill("NVDA", results)
        assert out["data_quality_grade"] == "normal"

    def test_data_quality_grade_degraded(self, queen):
        """2/5 维度(40%) → 应低于 60% 阈值但 >= 40% → degraded"""
        results = [_make_result("signal", 7.0), _make_result("catalyst", 6.0)]
        out = queen.distill("NVDA", results)
        assert out["dimension_coverage_pct"] == 40.0
        assert out["data_quality_grade"] == "degraded"

    def test_data_quality_grade_critical(self, queen):
        """1/5 维度(20%) → 应低于 40% 阈值 → critical"""
        results = [_make_result("signal", 7.0)]
        out = queen.distill("NVDA", results)
        assert out["dimension_coverage_pct"] == 20.0
        assert out["data_quality_grade"] == "critical"

    def test_data_quality_grade_boundary_60(self, queen):
        """3/5 维度(60%) → 恰好 60% 边界 → normal"""
        results = [
            _make_result("signal", 7.0), _make_result("catalyst", 6.0),
            _make_result("sentiment", 8.0),
        ]
        out = queen.distill("NVDA", results)
        assert out["dimension_coverage_pct"] == 60.0
        assert out["data_quality_grade"] == "normal"


class TestBearishPipelineIntegration:
    """端到端测试：看空信号通过 Phase-1 → 1.5 → 2 → Queen 完整流通"""

    def test_bearish_signal_survives_pipeline(self, board):
        """多个 Agent 看空 + BearBee 强看空 → 最终方向应为 bearish 或分数 < 5.0"""
        from pheromone_board import PheromoneEntry

        # Phase-1: 模拟板上数据（看空为主）
        for agent, score, direction in [
            ("ScoutBeeNova", 4.0, "bearish"),
            ("OracleBeeEcho", 3.5, "bearish"),
            ("BuzzBeeWhisper", 5.0, "neutral"),
            ("ChronosBeeHorizon", 4.5, "bearish"),
            ("RivalBeeVanguard", 4.0, "bearish"),
        ]:
            board.publish(PheromoneEntry(
                agent_id=agent, ticker="BEAR_E2E",
                discovery=f"test {agent}", source="test",
                self_score=score, direction=direction,
            ))

        queen_local = QueenDistiller(board)
        results = [
            _make_result("signal", 4.0, direction="bearish", confidence=0.7, source="ScoutBeeNova"),
            _make_result("odds", 3.5, direction="bearish", confidence=0.6, source="OracleBeeEcho"),
            _make_result("sentiment", 5.0, direction="neutral", confidence=0.5, source="BuzzBeeWhisper"),
            _make_result("catalyst", 4.5, direction="bearish", confidence=0.65, source="ChronosBeeHorizon"),
            _make_result("ml_auxiliary", 4.0, direction="bearish", confidence=0.5, source="RivalBeeVanguard"),
            _make_result("risk_adj", 3.0, direction="bearish", confidence=0.7, source="GuardBeeSentinel"),
            {
                "score": 2.0, "direction": "bearish", "confidence": 0.8,
                "discovery": "Strong insider selling + high P/C ratio",
                "source": "BearBeeContrarian", "dimension": "contrarian",
                "data_quality": {"insider": "real", "options": "real"},
                "details": {"bear_score": 8.0, "signal_count": 4},
            },
        ]

        out = queen_local.distill("BEAR_E2E", results)
        assert out["direction"] == "bearish" or out["final_score"] < 5.0, \
            f"看空信号应流通至最终输出，得到 direction={out['direction']} score={out['final_score']}"
        assert out["agent_breakdown"]["bearish"] >= 3

    def test_bear_cap_limits_bullish(self, board):
        """BearBee 强看空时 bear_cap 应限制最终看多分数"""
        from pheromone_board import PheromoneEntry

        # 在 board 上制造看多共振以提升 rule_score 超过 bear_cap
        for agent in ["ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon"]:
            board.publish(PheromoneEntry(
                agent_id=agent, ticker="CAP_TEST",
                discovery="strong bullish", source="test",
                self_score=9.5, direction="bullish",
            ))
        queen_local = QueenDistiller(board)

        results = [
            _make_result("signal", 9.5, direction="bullish", confidence=0.95, source="ScoutBeeNova"),
            _make_result("catalyst", 9.0, direction="bullish", confidence=0.9, source="ChronosBeeHorizon"),
            _make_result("sentiment", 9.0, direction="bullish", confidence=0.9, source="BuzzBeeWhisper"),
            _make_result("odds", 9.0, direction="bullish", confidence=0.9, source="OracleBeeEcho"),
            _make_result("risk_adj", 7.0, direction="bullish", confidence=0.85, source="GuardBeeSentinel"),
            {
                "score": 1.0, "direction": "bearish", "confidence": 0.9,
                "discovery": "Extreme bearish evidence",
                "source": "BearBeeContrarian", "dimension": "contrarian",
                "data_quality": {"insider": "real", "options": "real"},
                "details": {"bear_score": 9.0, "signal_count": 5},
            },
        ]

        out = queen_local.distill("CAP_TEST", results)
        # bear_strength = 10 - 1.0 = 9.0 > threshold 5.0 → bear_cap = 8.0
        # 高分看多 rule_score 应 > 8.0（共振加成后），因此 cap 应触发
        assert out["bear_cap_applied"] is True, \
            f"bear_cap 应已触发 (bear_strength={out.get('bear_strength')}, rule_score 应 > bear_cap)"
        # 最终分数被限制
        assert out["final_score"] <= 8.5, f"bear_cap 后分数应被限制，实际 {out['final_score']}"

    def test_bearish_resonance_with_bearbee(self, board):
        """看空共振中 BearBee 应参与维度计数"""
        from pheromone_board import PheromoneEntry

        for agent in ["ScoutBeeNova", "OracleBeeEcho", "BearBeeContrarian"]:
            board.publish(PheromoneEntry(
                agent_id=agent, ticker="RES_TEST",
                discovery="bearish signal", source="test",
                self_score=3.0, direction="bearish",
            ))

        res = board.detect_resonance("RES_TEST")
        assert res["resonance_detected"] is True, "3 维度看空（含 contrarian）应触发共振"
        assert "contrarian" in res["resonant_dimensions"]

    def test_single_bearish_agent_can_push_direction(self, board):
        """单个高置信 BearBee 可推动最终方向为 bearish（≥1 Agent + ≥25% 权重）"""
        queen_local = QueenDistiller(board)

        results = [
            _make_result("signal", 5.0, direction="neutral", confidence=0.5, source="ScoutBeeNova"),
            _make_result("catalyst", 5.0, direction="neutral", confidence=0.5, source="ChronosBeeHorizon"),
            _make_result("sentiment", 5.0, direction="neutral", confidence=0.5, source="BuzzBeeWhisper"),
            _make_result("odds", 5.0, direction="neutral", confidence=0.5, source="OracleBeeEcho"),
            _make_result("risk_adj", 5.0, direction="neutral", confidence=0.5, source="GuardBeeSentinel"),
            {
                "score": 2.5, "direction": "bearish", "confidence": 0.85,
                "discovery": "Strong bearish case", "source": "BearBeeContrarian",
                "dimension": "contrarian",
                "data_quality": {"insider": "real"},
                "details": {"bear_score": 7.5, "signal_count": 3},
            },
        ]

        out = queen_local.distill("PUSH_TEST", results)
        # bearish_w = 0.85, total = 5*0.5 + 0.85 = 3.35
        # bearish_w / total = 0.254 ≥ 0.25, bearish_count=1 ≥ 1, bearish_w > bullish_w(0)
        assert out["direction"] == "bearish", \
            f"单个高置信看空 Agent 应推动 bearish，实际 direction={out['direction']}"


# ==================== Queen helper 方法单元测试 ====================

class TestQueenHelpers:
    """测试 QueenDistiller 提取后的私有方法"""

    def test_prepare_dim_data_full_coverage(self, queen):
        """5 维度全部返回 → coverage=100%"""
        results = [
            _make_result("signal", 7.0, source="ScoutBeeNova"),
            _make_result("catalyst", 6.0, source="ChronosBeeHorizon"),
            _make_result("sentiment", 8.0, source="BuzzBeeWhisper"),
            _make_result("odds", 7.0, source="OracleBeeEcho"),
            _make_result("risk_adj", 5.0, source="GuardBeeSentinel"),
        ]
        prep = queen._prepare_dimension_data(results)
        assert prep["dimension_coverage_pct"] == 100.0
        assert prep["present_count"] == 5
        assert len(prep["dim_scores"]) == 5
        assert all(s == "present" for s in prep["dim_status"].values())

    def test_prepare_dim_data_partial(self, queen):
        """2/5 维度 → coverage=40%"""
        results = [
            _make_result("signal", 7.0, source="ScoutBeeNova"),
            _make_result("catalyst", 6.0, source="ChronosBeeHorizon"),
        ]
        prep = queen._prepare_dimension_data(results)
        assert prep["dimension_coverage_pct"] == 40.0
        assert prep["present_count"] == 2
        assert prep["dim_status"]["sentiment"] == "absent"

    def test_prepare_dim_data_error(self, queen):
        """含 error 的结果 → dim_status="error" """
        results = [
            _make_result("signal", 7.0, source="ScoutBeeNova"),
            {"dimension": "catalyst", "error": "API timeout", "source": "ChronosBeeHorizon",
             "score": 5.0, "direction": "neutral", "confidence": 0.0,
             "discovery": "", "data_quality": {}},
        ]
        prep = queen._prepare_dimension_data(results)
        assert prep["dim_status"]["catalyst"] == "error"
        assert "API timeout" in prep["dim_missing_reason"]["catalyst"]

    def test_weighted_score_uniform(self, queen):
        """全 8.0 + conf=1.0 → base_score≈8.0"""
        dim_scores = {"signal": 8.0, "catalyst": 8.0, "sentiment": 8.0, "odds": 8.0, "risk_adj": 8.0}
        dim_confidence = {"signal": 1.0, "catalyst": 1.0, "sentiment": 1.0, "odds": 1.0, "risk_adj": 1.0}
        ws = queen._compute_weighted_score("TEST", dim_scores, dim_confidence, 100.0, 5, [])
        assert 7.5 <= ws["base_score"] <= 8.5, f"全 8.0 输入时 base_score 应≈8.0，got {ws['base_score']}"
        assert ws["ml_adjustment"] == 0.0

    def test_weighted_score_low_coverage(self, queen):
        """1/5 维度 → 压缩至中性区间"""
        dim_scores = {"signal": 9.0}
        dim_confidence = {"signal": 1.0}
        ws = queen._compute_weighted_score("TEST", dim_scores, dim_confidence, 20.0, 1, [])
        # 低覆盖度会被压缩到接近 5.0
        assert 4.0 <= ws["base_score"] <= 6.0, f"极低覆盖度时应压缩至中性，got {ws['base_score']}"
        assert ws["coverage_warning"] != ""

    def test_triple_penalty_dq(self, queen):
        """全 proxy 数据 → dq_penalty_applied=True"""
        results = [
            {"score": 8.0, "direction": "bullish", "confidence": 0.8,
             "dimension": "signal", "source": "ScoutBeeNova",
             "data_quality": {"insider": "proxy_volume", "sec": "unavailable"}},
        ]
        tp = queen._apply_triple_penalty("TEST", 8.0, results)
        assert tp["dq_penalty_applied"] is True
        assert tp["rule_score"] < 8.0

    def test_triple_penalty_guard(self, queen):
        """guard_score=2.0 → guard_penalty_applied=True"""
        results = [
            _make_result("risk_adj", 2.0, source="GuardBeeSentinel"),
        ]
        tp = queen._apply_triple_penalty("TEST", 7.0, results)
        assert tp["guard_penalty_applied"] is True
        assert tp["rule_score"] < 7.0

    def test_triple_penalty_combo_cap(self, queen):
        """三重惩罚 > 2.0 → 截断至 -2.0"""
        # 使用无法识别的 data_quality 值触发强 DQ 惩罚 + 低 guard 分 → 组合超 2.0
        results = [
            {"score": 8.0, "direction": "bullish", "confidence": 0.8,
             "dimension": "signal", "source": "ScoutBeeNova",
             "discovery": "test", "data_quality": {"a": "garbage", "b": "garbage"}},
            {"score": 0.5, "direction": "neutral", "confidence": 0.6,
             "dimension": "risk_adj", "source": "GuardBeeSentinel",
             "discovery": "test", "data_quality": {"c": "garbage"}},
            {"score": 1.0, "direction": "bearish", "confidence": 0.9,
             "dimension": "contrarian", "source": "BearBeeContrarian",
             "discovery": "test", "data_quality": {"d": "garbage"}},
        ]
        tp = queen._apply_triple_penalty("TEST", 9.0, results)
        assert tp["combo_cap_applied"] is True, "组合惩罚应触发截断"
        actual_penalty = tp["pre_penalty_score"] - tp["rule_score"]
        assert abs(actual_penalty - 2.0) < 0.01, f"截断后惩罚应 = 2.0，got {actual_penalty}"

    def test_direction_vote_weighted(self, queen):
        """2 高置信 bullish + 3 低置信 bearish → bullish"""
        results = [
            _make_result("signal", 8.0, direction="bullish", confidence=0.9, source="ScoutBeeNova"),
            _make_result("catalyst", 7.0, direction="bullish", confidence=0.8, source="ChronosBeeHorizon"),
            _make_result("sentiment", 4.0, direction="bearish", confidence=0.3, source="BuzzBeeWhisper"),
            _make_result("odds", 4.5, direction="bearish", confidence=0.3, source="OracleBeeEcho"),
            _make_result("risk_adj", 4.0, direction="bearish", confidence=0.3, source="GuardBeeSentinel"),
        ]
        dv = queen._compute_direction_vote("TEST", results, results, 7.0)
        assert dv["rule_direction"] == "bullish"
        assert dv["bullish_count"] == 2
        assert dv["bearish_count"] == 3

    def test_direction_vote_conflict(self, queen):
        """3 bull + 3 bear → conflict_level="heavy" """
        results = [
            _make_result("signal", 8.0, direction="bullish", confidence=0.7, source="ScoutBeeNova"),
            _make_result("catalyst", 7.0, direction="bullish", confidence=0.7, source="ChronosBeeHorizon"),
            _make_result("sentiment", 7.0, direction="bullish", confidence=0.7, source="BuzzBeeWhisper"),
            _make_result("odds", 4.0, direction="bearish", confidence=0.7, source="OracleBeeEcho"),
            _make_result("risk_adj", 3.0, direction="bearish", confidence=0.7, source="GuardBeeSentinel"),
            {"score": 3.0, "direction": "bearish", "confidence": 0.7,
             "dimension": "contrarian", "source": "BearBeeContrarian",
             "data_quality": {"bear": "real"}},
        ]
        dv = queen._compute_direction_vote("TEST", results, results, 7.0)
        assert dv["conflict_level"] == "heavy"
        assert dv["conflict_info"]["bullish_agents"] == 3
        assert dv["conflict_info"]["bearish_agents"] == 3


# ==================== Enhancement A: 冲突仲裁 ====================

class TestConflictArbitration:
    """S4.5 冲突仲裁：票差过小时提升 GuardBee/BearBee 异议权重"""

    def test_arbitration_not_triggered_clear_majority(self, queen):
        """多数明确时不触发仲裁"""
        results = [
            _make_result("signal", 8.0, direction="bullish", confidence=0.9, source="ScoutBeeNova"),
            _make_result("catalyst", 7.0, direction="bullish", confidence=0.8, source="ChronosBeeHorizon"),
            _make_result("sentiment", 7.5, direction="bullish", confidence=0.85, source="BuzzBeeWhisper"),
            _make_result("odds", 4.0, direction="bearish", confidence=0.3, source="OracleBeeEcho"),
            _make_result("risk_adj", 5.0, direction="neutral", confidence=0.5, source="GuardBeeSentinel"),
        ]
        out = queen.distill("TEST", results)
        assert out["arbitration_triggered"] is False
        assert out["arbitration_flipped"] is False

    def test_arbitration_triggered_close_vote(self, queen):
        """票差 < 15% 且双方都有票时触发仲裁"""
        # 设计非常接近的票：bullish_w ≈ bearish_w
        results = [
            _make_result("signal", 7.0, direction="bullish", confidence=0.5, source="ScoutBeeNova"),
            _make_result("catalyst", 6.0, direction="bullish", confidence=0.45, source="ChronosBeeHorizon"),
            _make_result("sentiment", 4.0, direction="bearish", confidence=0.5, source="BuzzBeeWhisper"),
            _make_result("odds", 4.5, direction="bearish", confidence=0.45, source="OracleBeeEcho"),
            _make_result("risk_adj", 5.0, direction="neutral", confidence=0.3, source="GuardBeeSentinel"),
        ]
        dv = queen._compute_direction_vote("TEST", results, results, 6.0)
        assert dv["arbitration_triggered"] is True
        assert dv["pre_arbitration_margin"] < 0.15

    def test_arbitration_flip_direction(self, board):
        """仲裁导致方向翻转：GuardBee 看空的 1.5x boost 足以翻转 bullish→bearish"""
        queen_local = QueenDistiller(board)
        # 3 bullish (低 conf) vs 2 bearish (高 conf, 含 GuardBee)
        # 初始 bull_w=1.14 vs bear_w=1.07 → margin≈0.032 < 0.15 → 触发仲裁
        # 仲裁后 GuardBee bear 0.52→0.78 → bear_w=1.33 > bull_w=1.14 → 翻转
        results = [
            _make_result("signal", 7.0, direction="bullish", confidence=0.40, source="ScoutBeeNova"),
            _make_result("catalyst", 6.0, direction="bullish", confidence=0.38, source="ChronosBeeHorizon"),
            _make_result("sentiment", 6.5, direction="bullish", confidence=0.36, source="BuzzBeeWhisper"),
            _make_result("odds", 4.0, direction="bearish", confidence=0.55, source="OracleBeeEcho"),
            # GuardBee 看空 — 仲裁时 dissent_boost 1.5x: 0.52→0.78
            _make_result("risk_adj", 3.0, direction="bearish", confidence=0.52, source="GuardBeeSentinel"),
        ]
        dv = queen_local._compute_direction_vote("TEST", results, results, 6.0)
        assert dv["arbitration_triggered"] is True, \
            f"接近的票差应触发仲裁 (margin={dv['pre_arbitration_margin']:.4f})"
        assert dv["arbitration_flipped"] is True, \
            "GuardBee dissent boost 应导致方向从 bullish 翻转为 bearish"

    def test_arbitration_no_flip(self, queen):
        """仲裁触发但方向不变：GuardBee neutral → 无异议提升，方向维持"""
        # bullish 稍微优势，GuardBee neutral → 无异议提升
        results = [
            _make_result("signal", 7.0, direction="bullish", confidence=0.50, source="ScoutBeeNova"),
            _make_result("catalyst", 6.0, direction="bullish", confidence=0.48, source="ChronosBeeHorizon"),
            _make_result("sentiment", 4.0, direction="bearish", confidence=0.45, source="BuzzBeeWhisper"),
            _make_result("odds", 4.5, direction="bearish", confidence=0.43, source="OracleBeeEcho"),
            _make_result("risk_adj", 5.0, direction="neutral", confidence=0.3, source="GuardBeeSentinel"),
        ]
        dv = queen._compute_direction_vote("TEST", results, results, 6.0)
        assert dv["arbitration_triggered"] is True, \
            f"接近的票差应触发仲裁 (margin={dv['pre_arbitration_margin']:.4f})"
        assert dv["arbitration_flipped"] is False, \
            "GuardBee neutral 无异议提升，方向不应翻转"

    def test_distill_output_has_arbitration_fields(self, queen):
        """distill() 输出包含仲裁字段"""
        results = [_make_result("signal", 7.0)]
        out = queen.distill("TEST", results)
        assert "arbitration_triggered" in out
        assert "arbitration_flipped" in out
        assert "pre_arbitration_margin" in out
        assert isinstance(out["arbitration_triggered"], bool)
        assert isinstance(out["pre_arbitration_margin"], float)


# ==================== Enhancement B: 置信度校准 ====================

class TestConfidenceCalibration:
    """Enhancement B: 基于维度分散度计算置信区间"""

    def test_confidence_high_agreement(self, queen):
        """维度一致 → band_width 小 → discrimination="high" """
        results = [
            _make_result("signal", 7.5, confidence=0.8),
            _make_result("catalyst", 7.3, confidence=0.8),
            _make_result("sentiment", 7.4, confidence=0.8),
            _make_result("odds", 7.6, confidence=0.8),
            _make_result("risk_adj", 7.5, confidence=0.8),
        ]
        out = queen.distill("TEST", results)
        cc = out["confidence_calibration"]
        assert cc["discrimination"] == "high"
        assert cc["band_width"] < 0.5
        assert cc["dimension_std"] < 0.5

    def test_confidence_high_disagreement(self, queen):
        """维度分散 → band_width 大 → discrimination 不是 "high" """
        results = [
            _make_result("signal", 9.0, confidence=0.8),
            _make_result("catalyst", 2.0, confidence=0.8),
            _make_result("sentiment", 8.0, confidence=0.8),
            _make_result("odds", 3.0, confidence=0.8),
            _make_result("risk_adj", 7.0, confidence=0.8),
        ]
        out = queen.distill("TEST", results)
        cc = out["confidence_calibration"]
        assert cc["discrimination"] in ("medium", "low")
        assert cc["band_width"] > 0.5
        assert cc["dimension_std"] > 1.0

    def test_confidence_low_coverage_amplifier(self, queen):
        """覆盖维度 < 3 → band_width 被 coverage_amplifier 放大"""
        # 只有 2 个维度 → present_count=2 < low_coverage_threshold=3
        results = [
            _make_result("signal", 9.0, confidence=0.8),
            _make_result("catalyst", 3.0, confidence=0.8),
        ]
        out = queen.distill("TEST", results)
        cc = out["confidence_calibration"]
        # 低覆盖度应放大 band_width
        assert cc["band_width"] > 0.0

    def test_confidence_band_max_cap(self, queen):
        """band_width 不超过 MAX_BAND (2.0)"""
        results = [
            _make_result("signal", 10.0, confidence=0.8),
            _make_result("catalyst", 0.0, confidence=0.8),
        ]
        out = queen.distill("TEST", results)
        cc = out["confidence_calibration"]
        assert cc["band_width"] <= 2.0

    def test_confidence_band_structure(self, queen):
        """confidence_band 是 (lower, upper) 元组，范围在 [0, 10]"""
        results = [_make_result("signal", 7.0)]
        out = queen.distill("TEST", results)
        cc = out["confidence_calibration"]
        assert "confidence_band" in cc
        lo, hi = cc["confidence_band"]
        assert 0.0 <= lo <= hi <= 10.0

    def test_distill_output_has_calibration(self, queen):
        """distill() 输出包含 confidence_calibration 子 dict"""
        results = [_make_result("signal", 7.0)]
        out = queen.distill("TEST", results)
        assert "confidence_calibration" in out
        cc = out["confidence_calibration"]
        assert "confidence_band" in cc
        assert "band_width" in cc
        assert "discrimination" in cc
        assert "dimension_std" in cc


# ==================== Enhancement C: ML 反馈权重 ====================

class TestMLFeedbackWeighting:
    """Enhancement C: ML 特征重要性 → 维度权重 + Agent 投票调整"""

    def _make_mock_ml_model(self, importance=None):
        """创建 mock ML model，返回指定的 feature_importance"""
        model = MagicMock()
        if importance is None:
            # 默认：odds 维度最重要，signal 次之
            importance = {
                "crowding": {"weight": 0.15, "coefficient": 0.5, "direction": "positive"},
                "catalyst": {"weight": 0.08, "coefficient": 0.3, "direction": "positive"},
                "momentum": {"weight": 0.06, "coefficient": 0.2, "direction": "positive"},
                "sentiment": {"weight": 0.05, "coefficient": 0.15, "direction": "positive"},
                "volatility": {"weight": 0.04, "coefficient": -0.1, "direction": "negative"},
                "iv_rank": {"weight": 0.20, "coefficient": 0.8, "direction": "positive"},
                "put_call_ratio": {"weight": 0.12, "coefficient": -0.5, "direction": "negative"},
                "odds_score": {"weight": 0.10, "coefficient": 0.4, "direction": "positive"},
                "risk_adj_score": {"weight": 0.05, "coefficient": 0.2, "direction": "positive"},
                "final_score": {"weight": 0.05, "coefficient": 0.15, "direction": "positive"},
                "agent_agreement": {"weight": 0.05, "coefficient": 0.18, "direction": "positive"},
                "direction_encoded": {"weight": 0.05, "coefficient": 0.12, "direction": "positive"},
            }
        model.get_feature_importance.return_value = importance
        model.is_trained = True
        return model

    def test_ml_feedback_no_model(self, board):
        """ml_model=None → 空调整 → 原始权重不变"""
        queen_no_ml = QueenDistiller(board, ml_model=None)
        assert queen_no_ml.ml_feedback_enabled is False
        assert queen_no_ml.ml_adjustments == {}

    def test_ml_feedback_adjustments(self, board):
        """有 ml_model → 维度权重归一化调整"""
        model = self._make_mock_ml_model()
        queen_ml = QueenDistiller(board, ml_model=model)
        assert queen_ml.ml_feedback_enabled is True
        assert len(queen_ml.ml_adjustments) > 0
        # 权重应该已归一化
        total = sum(queen_ml.DIMENSION_WEIGHTS.values())
        assert abs(total - 1.0) < 0.01, f"权重总和应 ≈ 1.0，got {total}"

    def test_ml_feedback_clamp(self, board):
        """调整因子被 clamp 到 [0.5, 2.0]"""
        # 极端 importance：一个维度占 99%
        extreme = {
            "crowding": {"weight": 0.99, "coefficient": 5.0, "direction": "positive"},
            "catalyst": {"weight": 0.002, "coefficient": 0.01, "direction": "positive"},
            "momentum": {"weight": 0.001, "coefficient": 0.005, "direction": "positive"},
            "sentiment": {"weight": 0.001, "coefficient": 0.005, "direction": "positive"},
            "volatility": {"weight": 0.001, "coefficient": 0.005, "direction": "negative"},
            "iv_rank": {"weight": 0.001, "coefficient": 0.005, "direction": "positive"},
            "put_call_ratio": {"weight": 0.001, "coefficient": 0.005, "direction": "negative"},
            "odds_score": {"weight": 0.001, "coefficient": 0.005, "direction": "positive"},
            "risk_adj_score": {"weight": 0.001, "coefficient": 0.005, "direction": "positive"},
        }
        model = self._make_mock_ml_model(importance=extreme)
        queen_ml = QueenDistiller(board, ml_model=model)
        for dim, adj in queen_ml.ml_adjustments.items():
            assert 0.5 <= adj <= 2.0, f"{dim} adjustment {adj} out of clamp range"

    def test_ml_feedback_untrained_model(self, board):
        """未训练模型 → get_feature_importance() 返回空 → 不激活"""
        model = MagicMock()
        model.get_feature_importance.return_value = {}
        model.is_trained = False
        queen_ml = QueenDistiller(board, ml_model=model)
        assert queen_ml.ml_feedback_enabled is False
        assert queen_ml.ml_adjustments == {}

    def test_ml_vote_boost(self, board):
        """有 ML 模型时 Agent 投票权重应受 ml_adjustments 影响"""
        model = self._make_mock_ml_model()
        queen_ml = QueenDistiller(board, ml_model=model)
        assert queen_ml.ml_feedback_enabled is True

        # 使用明确的多数方向（3 bullish vs 1 bearish）避免触发仲裁
        results = [
            _make_result("signal", 8.0, direction="bullish", confidence=0.8, source="ScoutBeeNova"),
            _make_result("catalyst", 7.0, direction="bullish", confidence=0.7, source="ChronosBeeHorizon"),
            _make_result("sentiment", 7.5, direction="bullish", confidence=0.75, source="BuzzBeeWhisper"),
            _make_result("odds", 4.0, direction="bearish", confidence=0.7, source="OracleBeeEcho"),
            _make_result("risk_adj", 5.0, direction="neutral", confidence=0.5, source="GuardBeeSentinel"),
        ]
        dv_ml = queen_ml._compute_direction_vote("TEST", results, results, 7.0)

        # 对比无 ML 的投票
        queen_no_ml = QueenDistiller(board, ml_model=None)
        dv_no_ml = queen_no_ml._compute_direction_vote("TEST", results, results, 7.0)

        # ML boost 使得不同维度的 Agent 有不同的有效置信度
        # signal 维度 adjustment > odds 维度 adjustment → ScoutBeeNova 的权重变化 ≠ OracleBeeEcho
        ml_weights = dv_ml["direction_vote_weights"]
        no_ml_weights = dv_no_ml["direction_vote_weights"]
        # 至少有一个方向的权重应不同
        changed = any(
            abs(ml_weights[d] - no_ml_weights[d]) > 0.001
            for d in ("bullish", "bearish", "neutral")
        )
        assert changed, \
            f"ML 反馈应改变投票权重: ml={ml_weights} vs no_ml={no_ml_weights}"

    def test_distill_output_has_ml_fields(self, queen):
        """distill() 输出包含 ML 反馈字段"""
        results = [_make_result("signal", 7.0)]
        out = queen.distill("TEST", results)
        assert "ml_weight_adjustments" in out
        assert "ml_feedback_enabled" in out
        assert isinstance(out["ml_weight_adjustments"], dict)
        assert isinstance(out["ml_feedback_enabled"], bool)


# ==================== 向后兼容性 ====================

class TestBackwardCompatibility:
    """验证三增强不破坏现有行为"""

    def test_backward_compat_no_ml_model(self, queen):
        """不传 ml_model 时一切照旧（queen fixture 不传 ml_model）"""
        results = [
            _make_result("signal", 8.0, direction="bullish"),
            _make_result("catalyst", 7.0, direction="bullish"),
            _make_result("sentiment", 6.0, direction="bullish"),
        ]
        out = queen.distill("TEST", results)
        # 基本字段仍存在
        assert "final_score" in out
        assert "direction" in out
        assert out["ml_feedback_enabled"] is False
        assert out["ml_weight_adjustments"] == {}
        # 新字段也存在
        assert "arbitration_triggered" in out
        assert "confidence_calibration" in out


class TestDataRealPct:
    """方案22: data_real_pct 回归测试 — 确保数据准确度 >= 95%"""

    @staticmethod
    def _typical_results():
        """构造 7 个 Agent 的典型 data_quality dict（--no-llm, 全 API 可用）"""
        return [
            {"score": 7.0, "direction": "bullish", "confidence": 0.8,
             "dimension": "signal", "source": "ScoutBeeNova",
             "discovery": "test signal",
             "data_quality": {
                 "social_buzz": "real", "google_trends": "proxy_volume",
                 "bullish_agents": "real", "polymarket": "proxy_momentum",
                 "seeking_alpha": "proxy_social", "short_interest": "real",
                 "momentum": "real",
             }},
            {"score": 6.0, "direction": "bullish", "confidence": 0.7,
             "dimension": "odds", "source": "OracleBeeEcho",
             "discovery": "test odds",
             "data_quality": {"options": "real", "polymarket": "unavailable"}},
            {"score": 7.0, "direction": "bullish", "confidence": 0.8,
             "dimension": "sentiment", "source": "BuzzBeeWhisper",
             "discovery": "test sentiment",
             "data_quality": {
                 "momentum": "real", "volume": "real", "volatility": "real",
                 "reddit": "real", "finviz_news": "keyword",
             }},
            {"score": 6.0, "direction": "neutral", "confidence": 0.7,
             "dimension": "catalyst", "source": "ChronosBeeHorizon",
             "discovery": "test catalyst",
             "data_quality": {
                 "yfinance_calendar": "real", "catalysts_json": "loaded",
                 "analyst_targets": "real", "llm_impact": "rule_only",
             }},
            {"score": 5.0, "direction": "neutral", "confidence": 0.6,
             "dimension": "risk_adj", "source": "GuardBeeSentinel",
             "discovery": "test risk",
             "data_quality": {
                 "pheromone_board": "real", "crowding": "real",
                 "llm_conflict": "rule_only",
             }},
            {"score": 6.0, "direction": "bullish", "confidence": 0.6,
             "dimension": "ml_auxiliary", "source": "RivalBeeVanguard",
             "discovery": "test ml",
             "data_quality": {"ml_prediction": "real"}},
            {"score": 4.0, "direction": "bearish", "confidence": 0.7,
             "dimension": "contrarian", "source": "BearBeeContrarian",
             "discovery": "test bear",
             "data_quality": {
                 "insider": "real", "valuation": "yfinance", "options": "real",
                 "momentum": "yfinance", "news": "real", "catalyst": "real",
                 "ml": "real", "guard": "real",
             }},
        ]

    def test_happy_path_above_95(self, queen):
        """典型 --no-llm 扫描（全 API 可用）data_real_pct >= 95%"""
        results = self._typical_results()
        tp = queen._apply_triple_penalty("TEST", 7.0, results)
        assert tp["data_real_pct"] >= 95.0, \
            f"Happy-path data_real_pct should be >= 95%, got {tp['data_real_pct']}"

    def test_empty_calendar_still_high(self, queen):
        """ChronosBee 日历为空时 data_real_pct 仍 >= 93%"""
        results = self._typical_results()
        results[3]["data_quality"]["yfinance_calendar"] = "empty"
        tp = queen._apply_triple_penalty("TEST", 7.0, results)
        assert tp["data_real_pct"] >= 93.0, \
            f"Empty calendar data_real_pct should be >= 93%, got {tp['data_real_pct']}"

    def test_ml_fallback_recognized(self, queen):
        """RivalBee fallback_momentum 应被识别为 PROXY（0.7），非 0"""
        results = self._typical_results()
        results[5]["data_quality"]["ml_prediction"] = "fallback_momentum"
        tp = queen._apply_triple_penalty("TEST", 7.0, results)
        assert tp["data_real_pct"] >= 93.0, \
            f"ML fallback data_real_pct should be >= 93%, got {tp['data_real_pct']}"

    def test_degraded_scenario_above_85(self, queen):
        """多重降级（Reddit + 日历空 + catalysts 缺失 + ML 降级）仍 >= 85%"""
        results = self._typical_results()
        results[2]["data_quality"]["reddit"] = "fallback"
        results[3]["data_quality"]["yfinance_calendar"] = "empty"
        results[3]["data_quality"]["catalysts_json"] = "missing"
        results[5]["data_quality"]["ml_prediction"] = "fallback_momentum"
        tp = queen._apply_triple_penalty("TEST", 7.0, results)
        assert tp["data_real_pct"] >= 85.0, \
            f"Degraded scenario data_real_pct should be >= 85%, got {tp['data_real_pct']}"

    def test_all_known_values_classified(self):
        """所有已知 data_quality 值都必须在 REAL 或 PROXY 中"""
        from swarm_agents.queen_distiller import QueenDistiller
        all_known = {
            # ScoutBee (via real_data_sources)
            "real", "proxy_volume", "proxy_momentum", "proxy_social", "default",
            # OracleBee
            "options_api", "fallback", "unavailable",
            # BuzzBee
            "keyword", "reddit_apewisdom",
            # ChronosBee
            "loaded", "empty", "missing", "llm_enhanced", "rule_only",
            # GuardBee
            "pheromone_board",
            # RivalBee
            "fallback_momentum",
            # BearBee
            "sec_api", "yfinance", "finviz_api",
            # 其他
            "SEC直查", "Finviz", "finviz",
        }
        classified = QueenDistiller.REAL_SOURCES | QueenDistiller.PROXY_SOURCES
        unclassified = all_known - classified
        assert unclassified == set(), \
            f"未分类的 data_quality 值: {unclassified}"
