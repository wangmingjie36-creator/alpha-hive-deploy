"""6 个核心 Agent 单元测试"""

import pytest


# ==================== Agent 返回值通用校验 ====================

REQUIRED_FIELDS = {"score", "direction", "discovery", "source", "dimension", "confidence"}
VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}
VALID_DIMENSIONS = {"signal", "catalyst", "sentiment", "odds", "risk_adj", "ml_auxiliary"}


def _validate_result(result, expected_source, expected_dimension):
    """通用 Agent 返回值校验"""
    assert isinstance(result, dict), f"返回值应为 dict，实际: {type(result)}"

    # 如果有 error，仍应有基本字段
    if "error" in result:
        assert "source" in result
        assert "score" in result
        return

    for field in REQUIRED_FIELDS:
        assert field in result, f"缺少字段: {field}"

    assert result["source"] == expected_source
    assert result["dimension"] == expected_dimension
    assert isinstance(result["score"], float)
    assert 0.0 <= result["score"] <= 10.0, f"score 越界: {result['score']}"
    assert result["direction"] in VALID_DIRECTIONS, f"无效 direction: {result['direction']}"
    assert isinstance(result["confidence"], float)
    assert 0.0 <= result["confidence"] <= 1.0, f"confidence 越界: {result['confidence']}"
    assert len(result["discovery"]) > 0, "discovery 不能为空"


# ==================== ScoutBeeNova ====================

class TestScoutBeeNova:
    def test_analyze_returns_valid_result(self, all_agents):
        result = all_agents["scout"].analyze("NVDA")
        _validate_result(result, "ScoutBeeNova", "signal")

    def test_has_data_quality(self, all_agents):
        result = all_agents["scout"].analyze("NVDA")
        if "error" not in result:
            assert "data_quality" in result

    def test_publishes_to_board(self, all_agents, board):
        all_agents["scout"].analyze("NVDA")
        assert board.get_entry_count() >= 1


# ==================== OracleBeeEcho ====================

class TestOracleBeeEcho:
    def test_analyze_returns_valid_result(self, all_agents):
        result = all_agents["oracle"].analyze("NVDA")
        _validate_result(result, "OracleBeeEcho", "odds")

    def test_has_options_data(self, all_agents):
        result = all_agents["oracle"].analyze("NVDA")
        if "error" not in result:
            assert "data_quality" in result


# ==================== BuzzBeeWhisper ====================

class TestBuzzBeeWhisper:
    def test_analyze_returns_valid_result(self, all_agents):
        result = all_agents["buzz"].analyze("NVDA")
        _validate_result(result, "BuzzBeeWhisper", "sentiment")

    def test_has_sentiment_details(self, all_agents):
        result = all_agents["buzz"].analyze("NVDA")
        if "error" not in result:
            assert "details" in result
            details = result["details"]
            assert "sentiment_pct" in details
            assert "momentum_5d" in details
            assert "volume_ratio" in details


# ==================== ChronosBeeHorizon ====================

class TestChronosBeeHorizon:
    def test_analyze_returns_valid_result(self, all_agents):
        result = all_agents["chronos"].analyze("NVDA")
        _validate_result(result, "ChronosBeeHorizon", "catalyst")

    def test_has_catalysts_list(self, all_agents):
        result = all_agents["chronos"].analyze("NVDA")
        if "error" not in result and "details" in result:
            assert "catalysts" in result["details"]
            assert isinstance(result["details"]["catalysts"], list)


# ==================== RivalBeeVanguard ====================

class TestRivalBeeVanguard:
    def test_analyze_returns_valid_result(self, all_agents):
        result = all_agents["rival"].analyze("NVDA")
        _validate_result(result, "RivalBeeVanguard", "ml_auxiliary")


# ==================== GuardBeeSentinel ====================

class TestGuardBeeSentinel:
    def test_analyze_returns_valid_result(self, all_agents):
        result = all_agents["guard"].analyze("NVDA")
        _validate_result(result, "GuardBeeSentinel", "risk_adj")

    def test_resonance_info_in_details(self, all_agents):
        result = all_agents["guard"].analyze("NVDA")
        if "error" not in result and "details" in result:
            assert "resonance" in result["details"]
            assert "consistency" in result["details"]

    def test_guard_reads_pheromone_board(self, all_agents, board):
        """GuardBee 应读取信息素板上其他 Agent 的信号"""
        # 先让几个 Agent 发布信号
        all_agents["scout"].analyze("NVDA")
        all_agents["buzz"].analyze("NVDA")
        # 然后让 Guard 分析
        result = all_agents["guard"].analyze("NVDA")
        if "error" not in result and "details" in result:
            assert result["details"]["top_signals_count"] >= 2


# ==================== 跨 Agent 集成 ====================

class TestAllAgents:
    def test_all_agents_produce_valid_output(self, all_agents):
        """所有 Agent 对同一 ticker 都应返回有效结果"""
        for name, agent in all_agents.items():
            result = agent.analyze("NVDA")
            assert isinstance(result, dict), f"{name} 返回类型错误"
            assert "score" in result, f"{name} 缺少 score"
            assert "direction" in result, f"{name} 缺少 direction"

    def test_different_tickers(self, all_agents):
        """Agent 对不同 ticker 应返回不同结果"""
        for name, agent in all_agents.items():
            r1 = agent.analyze("NVDA")
            r2 = agent.analyze("TSLA")
            # 至少 discovery 应不同
            if "error" not in r1 and "error" not in r2:
                assert r1["discovery"] != r2["discovery"] or r1["score"] != r2["score"], \
                    f"{name}: NVDA 和 TSLA 结果完全相同"
