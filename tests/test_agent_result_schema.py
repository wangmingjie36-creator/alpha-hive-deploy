"""
tests/test_agent_result_schema.py — pattern 2 AgentResult 声明式契约测试
"""

from models import AgentResult, AGENT_RESULT_SCHEMA, VALID_DIMENSIONS


def _good():
    return AgentResult(
        score=7.2, direction="bullish", confidence=0.8,
        discovery="内幕买入 $2.3M | 拥挤度 72/100", source="sec_edgar+crowding",
        dimension="signal",
    )


def test_schema_constant_shape():
    assert AGENT_RESULT_SCHEMA["score"] == {"type": "number", "min": 0.0, "max": 10.0}
    assert "neutral" in AGENT_RESULT_SCHEMA["direction"]["values"]
    assert set(VALID_DIMENSIONS) == {
        "signal", "odds", "sentiment", "catalyst", "risk_adj", "ml_auxiliary", "contrarian",
    }


def test_good_result_passes():
    assert _good().validate(strict=True) == []


def test_postinit_still_coerces():
    r = AgentResult(score=99, direction="up", confidence=2.0,
                    discovery="d", source="x", dimension="signal")
    assert r.score == 10.0 and r.direction == "neutral" and r.confidence == 1.0


def test_validate_flags_empty_source():
    r = AgentResult(score=5, direction="neutral", confidence=0.5,
                    discovery="d", source="   ", dimension="signal")
    assert "source is empty" in r.validate()


def test_validate_strict_flags_unknown_dimension():
    r = AgentResult(score=5, direction="neutral", confidence=0.5,
                    discovery="d", source="x", dimension="made_up_dim")
    assert any("unknown dimension" in i for i in r.validate(strict=True))
    # 非 strict 不报维度问题
    assert not any("unknown dimension" in i for i in r.validate(strict=False))


def test_validate_flags_oversized_details():
    big = {f"k{i}": i for i in range(100)}
    r = AgentResult(score=5, direction="neutral", confidence=0.5,
                    discovery="d", source="x", dimension="signal", details=big)
    assert any("details has too many keys" in i for i in r.validate())
