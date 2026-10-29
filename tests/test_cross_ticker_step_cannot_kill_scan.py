"""v0.45.683：P4b（跨标的关联分析，LLM）是蜂群跑完之后的可选增强步骤——它抛什么都不许带崩整次扫描。

2026-10-29 影子 A/B 的第 2 代：蜂群已跑完、27 只已付费的结果都写盘了，P4b 的 LLM 调用抛 `APITimeoutError`
（异常类型不在原来的 except 列表里），冒出 `run_swarm_scan`，影子入口把整天记成扫描异常。
"""
import logging

import pytest

import llm_service
from alpha_hive_daily_report import AlphaHiveDailyReporter


def _results(n=3, mode="llm_enhanced"):
    return {f"T{i}": {"final_score": 6.0, "direction": "bullish", "distill_mode": mode} for i in range(n)}


def _run(results):
    # 该方法不读 self 上的任何东西（只用参数与 llm_service），传 None 即可
    return AlphaHiveDailyReporter._compute_cross_ticker(None, results, None)


@pytest.mark.parametrize("exc", [
    "timeout", "connection", "runtime", "key",
])
def test_llm_step_failure_is_skipped_with_a_warning(monkeypatch, caplog, exc):
    import anthropic
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    boom = {
        "timeout": anthropic.APITimeoutError(request=req),
        "connection": anthropic.APIConnectionError(request=req),
        "runtime": RuntimeError("别的什么错"),
        "key": KeyError("x"),
    }[exc]

    def fail(**_):
        raise boom
    monkeypatch.setattr(llm_service, "analyze_cross_ticker_patterns", fail)
    with caplog.at_level(logging.DEBUG):
        assert _run(_results()) == {}                       # 不抛，返回空分析
    if exc != "key":                                         # KeyError 走原来的 debug 路径，保持不变
        recs = [r for r in caplog.records if "P4b" in r.getMessage()]
        assert recs and recs[-1].levelno == logging.WARNING, "跳过必须留一行 WARNING，不能无声（谁会红？）"
        assert type(boom).__name__ in recs[-1].getMessage()


def test_no_llm_enhanced_results_means_no_call(monkeypatch):
    """非 LLM 模式（生产 --no-llm）根本不进这一步。"""
    monkeypatch.setattr(llm_service, "analyze_cross_ticker_patterns",
                        lambda **_: pytest.fail("不该被调用"))
    assert _run(_results(mode="rule_engine")) == {}


def test_success_path_unchanged(monkeypatch):
    monkeypatch.setattr(llm_service, "analyze_cross_ticker_patterns",
                        lambda **_: {"sector_rotation_signal": "risk-on", "cross_ticker_insights": [],
                                     "sector_momentum": {}})
    assert _run(_results()) == {"sector_rotation_signal": "risk-on", "cross_ticker_insights": [],
                                "sector_momentum": {}}
