"""7 个核心 Agent 单元测试（含 BearBeeContrarian）"""

import pytest


# ==================== Agent 返回值通用校验 ====================

REQUIRED_FIELDS = {"score", "direction", "discovery", "source", "dimension", "confidence"}
VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}
VALID_DIMENSIONS = {"signal", "catalyst", "sentiment", "odds", "risk_adj", "ml_auxiliary", "contrarian"}


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


# ==================== BearBeeContrarian ====================

class TestBearBeeContrarian:
    """Phase-2 看空对冲蜂单元测试"""

    def test_analyze_returns_valid_result(self, bear_bee):
        """BearBee 返回值应包含必要字段 + contrarian 维度"""
        result = bear_bee.analyze("NVDA")
        _validate_result(result, "BearBeeContrarian", "contrarian")

    def test_has_bear_score_in_details(self, bear_bee):
        """details 应包含 bear_score 等看空分解指标"""
        result = bear_bee.analyze("NVDA")
        if "error" not in result:
            assert "details" in result
            d = result["details"]
            assert "bear_score" in d
            assert "rule_bear_score" in d
            assert "bearish_signals" in d
            assert isinstance(d["bearish_signals"], list)
            assert 0.0 <= d["bear_score"] <= 10.0

    def test_score_inversely_related_to_bear_score(self, bear_bee):
        """score = 10 - bear_score：bear_score 越高，Agent score 越低"""
        result = bear_bee.analyze("NVDA")
        if "error" not in result and "details" in result:
            bear = result["details"]["bear_score"]
            score = result["score"]
            # score ≈ 10.0 - bear_score（允许 LLM 混合带来 ±0.5 偏差）
            expected = max(1.0, min(10.0, 10.0 - bear))
            assert abs(score - expected) < 0.6, \
                f"score={score} 与 10-bear_score={expected} 偏差过大"

    def test_direction_thresholds(self, bear_bee):
        """方向判定应遵守 bear_score 阈值：≥5.5→bearish, ≥3.5→neutral, <3.5→bullish"""
        result = bear_bee.analyze("NVDA")
        if "error" not in result and "details" in result:
            bear = result["details"]["bear_score"]
            direction = result["direction"]
            if bear >= 5.5:
                assert direction == "bearish", f"bear_score={bear} 应为 bearish"
            elif bear >= 3.5:
                assert direction == "neutral", f"bear_score={bear} 应为 neutral"
            else:
                assert direction == "bullish", f"bear_score={bear} 应为 bullish"

    def test_reads_pheromone_board(self, bear_bee, board, all_agents):
        """BearBee 应从信息素板读取 Phase-1 Agent 的数据"""
        # Phase-1: 让几个 Agent 先发布信号
        all_agents["scout"].analyze("NVDA")
        all_agents["oracle"].analyze("NVDA")
        all_agents["buzz"].analyze("NVDA")
        # Phase-2: BearBee 分析
        pre_count = board.get_entry_count()
        result = bear_bee.analyze("NVDA")
        if "error" not in result:
            # BearBee 也应发布到板上
            assert board.get_entry_count() > pre_count

    def test_has_data_quality(self, bear_bee):
        """应包含 data_quality 字段"""
        result = bear_bee.analyze("NVDA")
        if "error" not in result:
            assert "data_quality" in result
            assert isinstance(result["data_quality"], dict)

    def test_different_tickers_different_results(self, bear_bee):
        """不同 ticker 应产生不同的看空分析"""
        r1 = bear_bee.analyze("NVDA")
        r2 = bear_bee.analyze("TSLA")
        if "error" not in r1 and "error" not in r2:
            # 至少 discovery 或 score 应不同
            assert r1["discovery"] != r2["discovery"] or r1["score"] != r2["score"], \
                "NVDA 和 TSLA 的 BearBee 分析不应完全相同"

    def test_bear_dimensions_present(self, bear_bee):
        """details 应包含 5 个核心看空维度分数"""
        result = bear_bee.analyze("NVDA")
        if "error" not in result and "details" in result:
            d = result["details"]
            for dim in ("insider_bear", "overval_bear", "options_bear",
                        "momentum_bear", "news_bear"):
                assert dim in d, f"缺少维度: {dim}"
                assert isinstance(d[dim], (int, float)), f"{dim} 应为数值"


# ==================== BearBee helper 方法单元测试 ====================

class TestBearBeeHelpers:
    """测试 BearBeeContrarian 提取后的私有方法"""

    def test_assess_insider_selling_from_board(self, bear_bee, board):
        """信息素板有 ScoutBee 条目时 → insider_bear > 0"""
        from pheromone_board import PheromoneEntry
        board.publish(PheromoneEntry(
            agent_id="ScoutBeeNova", ticker="NVDA",
            discovery="内幕卖出 $5,000,000 内幕买入 $100,000",
            source="test", self_score=8.0, direction="bearish",
            details={"insider_sold_usd": 5000000, "insider_bought_usd": 100000},
        ))
        sigs, srcs = [], {}
        score, data = bear_bee._assess_insider_selling("NVDA", sigs, srcs)
        assert score > 0, "有大额内幕卖出时 insider_bear 应 > 0"
        assert data is not None
        assert srcs.get("insider") == "real"
        assert len(sigs) > 0

    def test_assess_insider_selling_no_data(self, bear_bee):
        """空板 + SEC 返回中性数据 → score == 0"""
        sigs, srcs = [], {}
        score, _data = bear_bee._assess_insider_selling("ZZZZ", sigs, srcs)
        assert score == 0.0
        assert len(sigs) == 0

    def test_assess_valuation_high_momentum(self, bear_bee):
        """mom_5d=20 → overval_bear >= 8"""
        sigs, srcs = [], {}
        score = bear_bee._assess_valuation("NVDA", {}, 20.0, 100.0, sigs, srcs)
        assert score >= 8.0
        assert any("暴涨" in s for s in sigs)

    def test_assess_options_puts_from_board(self, bear_bee, board):
        """OracleBee 条目 pc_ratio=1.6 → options_bear >= 8"""
        from pheromone_board import PheromoneEntry
        board.publish(PheromoneEntry(
            agent_id="OracleBeeEcho", ticker="NVDA",
            discovery="P/C Ratio 1.60",
            source="test", self_score=7.0, direction="bearish",
            details={"pc_ratio": 1.6, "iv_rank": 85},
        ))
        sigs, srcs = [], {}
        score, data = bear_bee._assess_options_puts("NVDA", 100.0, sigs, srcs)
        assert score >= 8.0
        assert data is not None
        assert srcs.get("options") == "real"

    def test_assess_momentum_decay_volume(self, bear_bee):
        """volume_ratio=0.3 → momentum_bear >= 5"""
        stock = {"volume_ratio": 0.3, "volatility_20d": 0}
        sigs, srcs = [], {}
        score = bear_bee._assess_momentum_decay(stock, 0.0, sigs, srcs)
        assert score >= 5.0
        assert any("萎缩" in s for s in sigs)

    def test_assess_news_bearish(self, bear_bee, board):
        """BuzzBee 条目 sentiment_score=25 → news_bear >= 7"""
        from pheromone_board import PheromoneEntry
        board.publish(PheromoneEntry(
            agent_id="BuzzBeeWhisper", ticker="NVDA",
            discovery="sentiment 25%",
            source="test", self_score=7.0, direction="bearish",
            details={"sentiment_score": 25},
        ))
        sigs, srcs = [], {}
        score, entry = bear_bee._assess_news_sentiment("NVDA", sigs, srcs)
        assert score >= 7.0
        assert entry is not None
        assert any("悲观" in s for s in sigs)

    def test_compute_bear_score_multi_dims(self, bear_bee):
        """5 个活跃维度 → 验证加权均值 + 广度加分"""
        dims = {"insider": 7.0, "valuation": 6.0, "options": 8.0,
                "momentum": 5.0, "news": 6.5, "chronos": 0, "ml": 0, "guard": 0}
        sigs = ["sig1", "sig2", "sig3"]
        score = bear_bee._compute_bear_score(dims, sigs, 100.0, 5.0)
        assert 5.0 <= score <= 10.0, f"多维度活跃时评分应在 5-10 范围，got {score}"

    def test_compute_bear_score_no_signals(self, bear_bee):
        """全零维度 + 空 signals → 低分"""
        dims = {"insider": 0, "valuation": 0, "options": 0,
                "momentum": 0, "news": 0, "chronos": 0, "ml": 0, "guard": 0}
        sigs = []
        score = bear_bee._compute_bear_score(dims, sigs, 100.0, 1.0)
        assert score <= 5.0, f"无信号时评分不应过高，got {score}"

    def test_generate_llm_thesis_disabled(self, bear_bee):
        """LLM disabled → final_bear_score == rule_bear_score"""
        sigs, srcs = ["test signal"], {}
        out = bear_bee._generate_llm_bear_thesis(
            "NVDA", 6.5, sigs, None, None, None, srcs)
        assert out["final_bear_score"] == 6.5
        assert out["llm_thesis"] == ""

    def test_assess_catalyst_risk_near_event(self, bear_bee, board):
        """ChronosBee 催化剂 3 天内到来 → chronos_bear >= 5"""
        from pheromone_board import PheromoneEntry
        board.publish(PheromoneEntry(
            agent_id="ChronosBeeHorizon", ticker="NVDA",
            discovery="earnings in 3 days",
            source="test", self_score=6.0, direction="neutral",
            details={"nearest_days": 3},
        ))
        sigs, srcs = [], {}
        score = bear_bee._assess_catalyst_risk("NVDA", sigs, srcs)
        assert score >= 5.0
        assert any("催化剂" in s for s in sigs)


# ==================== 跨 Agent 集成 ====================

class TestAllAgents:
    @pytest.mark.slow
    @pytest.mark.timeout(90)
    def test_all_agents_produce_valid_output(self, all_agents):
        """所有 Agent 对同一 ticker 都应返回有效结果"""
        for name, agent in all_agents.items():
            result = agent.analyze("NVDA")
            assert isinstance(result, dict), f"{name} 返回类型错误"
            assert "score" in result, f"{name} 缺少 score"
            assert "direction" in result, f"{name} 缺少 direction"

    @pytest.mark.slow
    @pytest.mark.timeout(90)
    def test_different_tickers(self, all_agents):
        """Agent 对不同 ticker 应返回不同结果"""
        for name, agent in all_agents.items():
            r1 = agent.analyze("NVDA")
            r2 = agent.analyze("TSLA")
            # 至少 discovery 应不同
            if "error" not in r1 and "error" not in r2:
                assert r1["discovery"] != r2["discovery"] or r1["score"] != r2["score"], \
                    f"{name}: NVDA 和 TSLA 结果完全相同"
