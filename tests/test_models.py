"""models 模块测试 - 数据模型 + 数据质量检测"""

import math
import pytest


class TestCleanFunctions:
    def test_clean_score_normal(self):
        from models import clean_score
        assert clean_score(7.5) == 7.5

    def test_clean_score_none(self):
        from models import clean_score
        assert clean_score(None) == 5.0

    def test_clean_score_nan(self):
        from models import clean_score
        assert clean_score(float("nan")) == 5.0

    def test_clean_score_inf(self):
        from models import clean_score
        assert clean_score(float("inf")) == 5.0

    def test_clean_score_negative(self):
        from models import clean_score
        assert clean_score(-3.0) == 0.0

    def test_clean_score_over_10(self):
        from models import clean_score
        assert clean_score(15.0) == 10.0

    def test_clean_score_string(self):
        from models import clean_score
        assert clean_score("not_a_number") == 5.0

    def test_clean_confidence_normal(self):
        from models import clean_confidence
        assert clean_confidence(0.8) == 0.8

    def test_clean_confidence_none(self):
        from models import clean_confidence
        assert clean_confidence(None) == 0.5

    def test_clean_confidence_nan(self):
        from models import clean_confidence
        assert clean_confidence(float("nan")) == 0.5

    def test_clean_confidence_clamped(self):
        from models import clean_confidence
        assert clean_confidence(1.5) == 1.0
        assert clean_confidence(-0.3) == 0.0

    def test_clean_direction_valid(self):
        from models import clean_direction
        assert clean_direction("bullish") == "bullish"
        assert clean_direction("BEARISH") == "bearish"
        assert clean_direction("Neutral") == "neutral"

    def test_clean_direction_invalid(self):
        from models import clean_direction
        assert clean_direction("maybe") == "neutral"
        assert clean_direction(None) == "neutral"
        assert clean_direction(123) == "neutral"

    def test_clean_string_normal(self):
        from models import clean_string
        assert clean_string("hello") == "hello"

    def test_clean_string_none(self):
        from models import clean_string
        assert clean_string(None) == ""
        assert clean_string(None, default="N/A") == "N/A"

    def test_clean_string_truncate(self):
        from models import clean_string
        long = "a" * 600
        assert len(clean_string(long)) == 500


class TestAgentResult:
    def test_from_dict_valid(self):
        from models import AgentResult
        d = {
            "score": 7.5,
            "direction": "bullish",
            "confidence": 0.8,
            "discovery": "test",
            "source": "ScoutBeeNova",
            "dimension": "signal",
        }
        r = AgentResult.from_dict(d)
        assert r is not None
        assert r.score == 7.5
        assert r.direction == "bullish"
        assert r.confidence == 0.8

    def test_from_dict_auto_cleans(self):
        from models import AgentResult
        d = {
            "score": 15.0,  # out of range
            "direction": "MAYBE",  # invalid
            "confidence": -0.5,  # out of range
            "discovery": "test",
            "source": "Test",
            "dimension": "signal",
        }
        r = AgentResult.from_dict(d)
        assert r.score == 10.0
        assert r.direction == "neutral"
        assert r.confidence == 0.0

    def test_from_dict_none(self):
        from models import AgentResult
        assert AgentResult.from_dict(None) is None

    def test_from_dict_error_only(self):
        from models import AgentResult
        assert AgentResult.from_dict({"error": "timeout"}) is None

    def test_from_dict_error_with_score(self):
        from models import AgentResult
        r = AgentResult.from_dict({"error": "partial", "score": 5.0,
                                    "direction": "neutral", "source": "X",
                                    "dimension": "signal", "discovery": ""})
        assert r is not None
        assert r.error == "partial"

    def test_is_valid(self):
        from models import AgentResult
        r = AgentResult(score=7.0, direction="bullish", confidence=0.8,
                        discovery="test", source="Test", dimension="signal")
        assert r.is_valid

    def test_is_valid_with_error(self):
        from models import AgentResult
        r = AgentResult(score=7.0, direction="bullish", confidence=0.8,
                        discovery="test", source="Test", dimension="signal",
                        error="something wrong")
        assert not r.is_valid

    def test_to_dict(self):
        from models import AgentResult
        r = AgentResult(score=7.0, direction="bullish", confidence=0.8,
                        discovery="test", source="Test", dimension="signal")
        d = r.to_dict()
        assert d["score"] == 7.0
        assert isinstance(d, dict)

    def test_nan_score_cleaned(self):
        from models import AgentResult
        r = AgentResult(score=float("nan"), direction="bullish",
                        confidence=0.5, discovery="", source="X",
                        dimension="signal")
        assert r.score == 5.0


class TestDistillOutput:
    def test_basic(self):
        from models import DistillOutput
        o = DistillOutput(ticker="NVDA", final_score=7.5, direction="bullish")
        assert o.ticker == "NVDA"
        assert o.final_score == 7.5

    def test_auto_cleans(self):
        from models import DistillOutput
        o = DistillOutput(ticker="nvda", final_score=15.0, direction="INVALID")
        assert o.ticker == "NVDA"
        assert o.final_score == 10.0
        assert o.direction == "neutral"


class TestDataQualityChecker:
    def test_check_valid_result(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        result = {
            "score": 7.5, "direction": "bullish",
            "source": "Test", "dimension": "signal",
        }
        assert dq.check_agent_result(result) == []

    def test_check_missing_fields(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        issues = dq.check_agent_result({"score": 5.0})
        assert len(issues) == 3  # missing direction, source, dimension

    def test_check_nan_score(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        result = {
            "score": float("nan"), "direction": "bullish",
            "source": "Test", "dimension": "signal",
        }
        issues = dq.check_agent_result(result)
        assert any("nan" in i for i in issues)

    def test_check_none_result(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        issues = dq.check_agent_result(None)
        assert len(issues) > 0

    def test_check_invalid_confidence(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        result = {
            "score": 5.0, "direction": "bullish",
            "source": "T", "dimension": "signal",
            "confidence": 1.5,
        }
        issues = dq.check_agent_result(result)
        assert any("confidence" in i for i in issues)

    def test_clean_valid(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        result = {
            "score": 7.5, "direction": "bullish",
            "source": "Test", "dimension": "signal",
            "confidence": 0.8,
        }
        cleaned = dq.clean_agent_result(result)
        assert cleaned["score"] == 7.5

    def test_clean_nan_score(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        result = {
            "score": float("nan"), "direction": "bullish",
            "source": "Test", "dimension": "signal",
        }
        cleaned = dq.clean_agent_result(result)
        assert cleaned["score"] == 5.0

    def test_clean_error_only(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        assert dq.clean_agent_result({"error": "timeout"}) is None

    def test_clean_none(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        assert dq.clean_agent_result(None) is None

    def test_clean_batch(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        results = [
            {"score": 7.0, "direction": "bullish", "source": "A", "dimension": "signal"},
            None,
            {"error": "timeout"},
            {"score": float("nan"), "direction": "bearish", "source": "B", "dimension": "odds"},
        ]
        cleaned = dq.clean_results_batch(results)
        assert len(cleaned) == 2
        assert cleaned[0]["score"] == 7.0
        assert cleaned[1]["score"] == 5.0  # NaN cleaned

    def test_clean_batch_empty(self):
        from models import DataQualityChecker
        dq = DataQualityChecker()
        assert dq.clean_results_batch([]) == []
        assert dq.clean_results_batch(None) == []
