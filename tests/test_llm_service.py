"""
tests/test_llm_service.py — llm_service 模块单元测试

Mock 策略：mock llm_service.call() — 所有高层函数都走它，一层拦截全部。
"""

import json
import pytest

import llm_service


# ==================== autouse: 每个测试前重置模块全局状态 ====================

@pytest.fixture(autouse=True)
def _reset_llm_state(monkeypatch):
    """重置 _disabled / _client / _token_usage，确保测试隔离"""
    monkeypatch.setattr(llm_service, "_disabled", False)
    monkeypatch.setattr(llm_service, "_client", None)
    monkeypatch.setattr(llm_service, "_api_key", None)
    monkeypatch.setattr(llm_service, "_token_usage", {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_cost_usd": 0.0,
        "call_count": 0,
    })


# ==================== disable / is_available ====================

class TestDisableAndAvailable:
    def test_disable_makes_unavailable(self):
        llm_service.disable()
        assert llm_service.is_available() is False

    def test_is_available_false_when_no_client(self, monkeypatch):
        """无 API key → _get_client() 返回 None → is_available = False"""
        monkeypatch.setattr(llm_service, "_get_client", lambda: None)
        assert llm_service.is_available() is False

    def test_is_available_true_when_client_exists(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(llm_service, "_get_client", lambda: sentinel)
        assert llm_service.is_available() is True


# ==================== get_usage ====================

class TestGetUsage:
    def test_returns_dict_copy(self):
        usage = llm_service.get_usage()
        assert isinstance(usage, dict)
        # 修改返回值不影响内部状态
        usage["call_count"] = 999
        assert llm_service.get_usage()["call_count"] == 0

    def test_initial_values(self):
        usage = llm_service.get_usage()
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["total_cost_usd"] == 0.0
        assert usage["call_count"] == 0


# ==================== call() ====================

class TestCall:
    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(llm_service, "_disabled", True)
        result = llm_service.call("hello")
        assert result is None

    def test_no_client_returns_none(self, monkeypatch):
        monkeypatch.setattr(llm_service, "_get_client", lambda: None)
        result = llm_service.call("hello")
        assert result is None

    def test_over_budget_returns_none(self, monkeypatch):
        """超过每日预算 → 返回 None"""
        from datetime import date
        monkeypatch.setattr(llm_service, "_token_usage", {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_cost_usd": 999.0,  # 远超预算
            "call_count": 0,
        })
        # 设置 _budget_date 为今天，防止 _maybe_reset_daily_budget 清零
        monkeypatch.setattr(llm_service, "_budget_date", date.today())
        # 需要一个 fake client 让它通过 _get_client check
        monkeypatch.setattr(llm_service, "_get_client", lambda: object())
        result = llm_service.call("hello")
        assert result is None

    def test_daily_budget_reset(self, monkeypatch):
        """日期变更 → 自动重置预算计数器"""
        from datetime import date, timedelta
        # 设置昨天的预算日期 + 高消耗
        yesterday = date.today() - timedelta(days=1)
        monkeypatch.setattr(llm_service, "_budget_date", yesterday)
        monkeypatch.setattr(llm_service, "_token_usage", {
            "input_tokens": 50000,
            "output_tokens": 10000,
            "total_cost_usd": 0.80,
            "call_count": 20,
        })
        # 调用 _maybe_reset_daily_budget
        llm_service._maybe_reset_daily_budget()
        # 应该重置为 0
        usage = llm_service.get_usage()
        assert usage["total_cost_usd"] == 0.0
        assert usage["call_count"] == 0
        assert llm_service._budget_date == date.today()

    def test_api_exception_returns_none(self, monkeypatch):
        """API 调用抛异常 → 返回 None"""
        class FakeClient:
            class messages:
                @staticmethod
                def create(**kw):
                    raise ConnectionError("network down")
        monkeypatch.setattr(llm_service, "_get_client", lambda: FakeClient())
        result = llm_service.call("hello")
        assert result is None

    def test_successful_call(self, monkeypatch):
        """正常 API 调用 → 返回文本，用量更新"""
        class FakeBlock:
            text = "Hello world"

        class FakeUsage:
            input_tokens = 10
            output_tokens = 20

        class FakeResponse:
            content = [FakeBlock()]
            usage = FakeUsage()

        class FakeClient:
            class messages:
                @staticmethod
                def create(**kw):
                    return FakeResponse()

        monkeypatch.setattr(llm_service, "_get_client", lambda: FakeClient())
        result = llm_service.call("test prompt")
        assert result == "Hello world"

        usage = llm_service.get_usage()
        assert usage["call_count"] == 1
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 20
        assert usage["total_cost_usd"] > 0


# ==================== call_json() ====================

class TestCallJson:
    def _mock_call(self, monkeypatch, return_text):
        """Helper: mock llm_service.call 返回指定文本"""
        monkeypatch.setattr(llm_service, "call", lambda *a, **kw: return_text)

    def test_direct_json(self, monkeypatch):
        """直接 JSON 文本 → 解析成功"""
        self._mock_call(monkeypatch, '{"score": 7.5}')
        result = llm_service.call_json("prompt")
        assert result == {"score": 7.5}

    def test_markdown_code_block(self, monkeypatch):
        """markdown 代码块中的 JSON"""
        text = '```json\n{"score": 8.0}\n```'
        self._mock_call(monkeypatch, text)
        result = llm_service.call_json("prompt")
        assert result == {"score": 8.0}

    def test_brace_extraction(self, monkeypatch):
        """文本中嵌入 JSON（前后有杂文）"""
        text = 'Here is the result: {"score": 6.0, "label": "ok"} end.'
        self._mock_call(monkeypatch, text)
        result = llm_service.call_json("prompt")
        assert result["score"] == 6.0
        assert result["label"] == "ok"

    def test_invalid_json_returns_none(self, monkeypatch):
        """完全无效 JSON → None"""
        self._mock_call(monkeypatch, "This is not JSON at all")
        result = llm_service.call_json("prompt")
        assert result is None

    def test_call_returns_none(self, monkeypatch):
        """call() 返回 None → call_json 也返回 None"""
        self._mock_call(monkeypatch, None)
        result = llm_service.call_json("prompt")
        assert result is None

    def test_code_block_without_json_tag(self, monkeypatch):
        """无 json 标签的代码块"""
        text = '```\n{"key": "value"}\n```'
        self._mock_call(monkeypatch, text)
        result = llm_service.call_json("prompt")
        assert result == {"key": "value"}


# ==================== distill_with_reasoning() ====================

class TestDistillWithReasoning:
    def test_normal_return(self, monkeypatch):
        fake_result = {
            "final_score": 8.0,
            "direction": "bullish",
            "reasoning": "因为信号一致",
            "key_insight": "强共振",
            "risk_flag": "波动率高",
            "confidence": 0.8,
            "narrative": "叙事摘要",
            "bull_bear_synthesis": "短期看多",
            "contrarian_view": "看空观点",
        }
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake_result)
        result = llm_service.distill_with_reasoning(
            ticker="NVDA",
            agent_results=[{"source": "ScoutBee", "score": 7.0, "direction": "bullish", "dimension": "signal"}],
            dim_scores={"signal": 7.0},
            resonance={"resonance_detected": True, "supporting_agents": 4, "confidence_boost": 10},
            rule_score=7.5,
            rule_direction="bullish",
        )
        assert result["final_score"] == 8.0
        assert result["direction"] == "bullish"

    def test_with_bear_result(self, monkeypatch):
        """含 bear_result 参数"""
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: {"final_score": 6.0, "direction": "neutral"})
        result = llm_service.distill_with_reasoning(
            ticker="TSLA",
            agent_results=[],
            dim_scores={},
            resonance={"resonance_detected": False, "supporting_agents": 0, "confidence_boost": 0},
            rule_score=5.0,
            rule_direction="neutral",
            bear_result={
                "details": {"bear_score": 7.0, "bearish_signals": ["overvalued"]},
                "llm_thesis": "看空论点",
                "llm_key_risks": ["高估值"],
                "llm_contrarian_insight": "市场忽视了...",
            },
        )
        assert result is not None

    def test_call_fails_returns_none(self, monkeypatch):
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: None)
        result = llm_service.distill_with_reasoning(
            ticker="TEST",
            agent_results=[],
            dim_scores={},
            resonance={},
            rule_score=5.0,
            rule_direction="neutral",
        )
        assert result is None


# ==================== generate_bear_thesis() ====================

class TestGenerateBearThesis:
    def test_normal_return(self, monkeypatch):
        fake = {
            "bear_score": 7.0,
            "thesis": "看空论点",
            "key_risks": ["风险1", "风险2", "风险3"],
            "contrarian_insight": "反向洞察",
            "thesis_break": "失效条件",
        }
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake)
        result = llm_service.generate_bear_thesis(
            ticker="NVDA",
            bull_signals=[{"signal": "买入", "score": 8}],
            bear_signals=["高估值"],
        )
        assert result["bear_score"] == 7.0
        assert len(result["key_risks"]) == 3

    def test_optional_params_none(self, monkeypatch):
        """所有可选参数都是 None"""
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: {"bear_score": 5.0})
        result = llm_service.generate_bear_thesis(
            ticker="TEST",
            bull_signals=[],
            bear_signals=[],
            insider_data=None,
            options_data=None,
            news_data=None,
        )
        assert result is not None


# ==================== analyze_cross_ticker_patterns() ====================

class TestAnalyzeCrossTickerPatterns:
    def test_normal_return(self, monkeypatch):
        fake = {
            "sector_momentum": {"Technology": "leading"},
            "cross_ticker_insights": [{"tickers": ["NVDA", "TSLA"], "type": "correlated", "insight": "科技板块轮动"}],
            "correlation_warnings": ["警告1"],
            "sector_rotation_signal": "科技板块领涨",
            "portfolio_adjustment_hints": ["分散配置"],
        }
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake)
        result = llm_service.analyze_cross_ticker_patterns(
            board_snapshot=[{"ticker": "NVDA", "agent_id": "Scout", "direction": "bullish", "self_score": 8}],
            distilled_scores={"NVDA": {"final_score": 8.0, "direction": "bullish"}},
            sector_map={"NVDA": "Technology"},
        )
        assert "sector_momentum" in result
        assert result["sector_momentum"]["Technology"] == "leading"

    def test_empty_scores_returns_none(self, monkeypatch):
        result = llm_service.analyze_cross_ticker_patterns(
            board_snapshot=[],
            distilled_scores={},
            sector_map={},
        )
        assert result is None


# ==================== analyze_news_sentiment() ====================

class TestAnalyzeNewsSentiment:
    def test_normal_return(self, monkeypatch):
        fake = {"sentiment_score": 7.5, "sentiment_label": "bullish", "key_theme": "利好", "reasoning": "推理"}
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake)
        result = llm_service.analyze_news_sentiment("NVDA", ["NVDA beats earnings", "AI demand surge"])
        assert result["sentiment_score"] == 7.5

    def test_empty_headlines_returns_none(self):
        result = llm_service.analyze_news_sentiment("NVDA", [])
        assert result is None

    def test_none_headlines_returns_none(self):
        result = llm_service.analyze_news_sentiment("NVDA", None)
        assert result is None


# ==================== interpret_insider_trades() ====================

class TestInterpretInsiderTrades:
    def test_normal_return(self, monkeypatch):
        fake = {"intent_score": 8.0, "intent_label": "accumulation", "intent_reasoning": "推理", "red_flags": []}
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake)
        result = llm_service.interpret_insider_trades(
            "NVDA",
            {"total_filings": 5, "dollar_bought": 100000, "dollar_sold": 0, "insider_sentiment": "bullish", "summary": "CEO买入"},
            {"price": 140.0, "momentum_5d": 3.0},
        )
        assert result["intent_score"] == 8.0

    def test_empty_data_returns_none(self):
        result = llm_service.interpret_insider_trades("NVDA", {}, {"price": 100})
        assert result is None

    def test_zero_filings_returns_none(self):
        result = llm_service.interpret_insider_trades("NVDA", {"total_filings": 0}, {"price": 100})
        assert result is None


# ==================== interpret_catalyst_impact() ====================

class TestInterpretCatalystImpact:
    def test_normal_return(self, monkeypatch):
        fake = {"impact_score": 8.0, "impact_direction": "bullish", "impact_reasoning": "推理", "key_catalyst": "催化剂"}
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake)
        result = llm_service.interpret_catalyst_impact(
            "NVDA",
            [{"event": "earnings", "date": "2026-03-10"}],
            {"price": 140.0, "momentum_5d": 3.0, "volatility_20d": 38.0},
        )
        assert result["impact_score"] == 8.0

    def test_empty_catalysts_returns_none(self):
        result = llm_service.interpret_catalyst_impact("NVDA", [], {"price": 100})
        assert result is None


# ==================== interpret_options_flow() ====================

class TestInterpretOptionsFlow:
    def test_normal_return(self, monkeypatch):
        fake = {"smart_money_score": 7.0, "smart_money_direction": "bullish", "flow_reasoning": "推理", "signal_type": "call_sweep"}
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake)
        result = llm_service.interpret_options_flow(
            "NVDA",
            {"iv_rank": 30, "put_call_ratio": 0.6},
            {"price": 140.0, "momentum_5d": 3.0, "volatility_20d": 38.0},
        )
        assert result["smart_money_score"] == 7.0

    def test_empty_options_returns_none(self):
        result = llm_service.interpret_options_flow("NVDA", {}, {"price": 100})
        assert result is None

    def test_none_options_returns_none(self):
        result = llm_service.interpret_options_flow("NVDA", None, {"price": 100})
        assert result is None


# ==================== synthesize_agent_conflicts() ====================

class TestSynthesizeAgentConflicts:
    def test_normal_return(self, monkeypatch):
        fake = {"risk_score": 3.0, "conflict_type": "minor_divergence", "guard_reasoning": "推理", "recommended_action": "proceed"}
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake)
        result = llm_service.synthesize_agent_conflicts(
            "NVDA",
            [{"agent_id": "Scout", "direction": "bullish", "self_score": 8, "pheromone_strength": 0.9}],
            {"resonance_detected": True, "supporting_agents": 4, "direction": "bullish"},
        )
        assert result["conflict_type"] == "minor_divergence"

    def test_empty_snapshot_returns_none(self):
        result = llm_service.synthesize_agent_conflicts("NVDA", [], {})
        assert result is None

    def test_none_snapshot_returns_none(self):
        result = llm_service.synthesize_agent_conflicts("NVDA", None, {})
        assert result is None


# ==================== find_historical_analogy() ====================

class TestFindHistoricalAnalogy:
    def test_normal_return(self, monkeypatch):
        fake = {
            "analogy_found": True,
            "analogy_date": "2026-01-15",
            "analogy_summary": "类比摘要",
            "historical_outcome": {"t1": "+1.5%", "t7": "+3.0%", "t30": "+5.0%"},
            "similarity_score": 0.85,
            "key_differences": "差异",
            "confidence_adjustment": 0.05,
            "warning": "",
        }
        monkeypatch.setattr(llm_service, "call_json", lambda *a, **kw: fake)
        result = llm_service.find_historical_analogy(
            "NVDA",
            {"direction": "bullish", "final_score": 8.0},
            [{"document": "d1", "similarity": 0.9}, {"document": "d2", "similarity": 0.8}, {"document": "d3", "similarity": 0.7}],
            [{"date": "2026-01-15", "direction": "bullish", "self_score": 7, "outcome_return_t1": 1.5, "outcome_return_t7": 3.0}],
        )
        assert result["analogy_found"] is True

    def test_insufficient_memories_returns_none(self):
        """少于 3 条历史记忆 → None"""
        result = llm_service.find_historical_analogy(
            "NVDA",
            {"direction": "bullish"},
            [{"document": "d1"}, {"document": "d2"}],  # 只有 2 条
            [],
        )
        assert result is None

    def test_none_memories_returns_none(self):
        result = llm_service.find_historical_analogy("NVDA", {}, None, [])
        assert result is None
