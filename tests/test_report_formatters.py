"""tests/test_report_formatters.py — report_formatters 模块单元测试"""

from types import SimpleNamespace
from datetime import datetime

import pytest

from report_formatters import (
    format_score_adjustments,
    generate_swarm_markdown_report,
    _build_summary,
    _build_smart_money,
    _build_market_expectations,
    _build_sentiment,
    _build_catalysts,
    _build_competitive,
    _build_bear_contrarian,
    _build_composite_judgment,
    _build_concentration,
    _build_cross_ticker,
    _build_macro,
    _build_backtest,
    DISCLAIMER_FULL,
)


# ---------- fixtures ----------

def _make_reporter():
    return SimpleNamespace(
        date_str="2026-03-09",
        timestamp=datetime(2026, 3, 9, 8, 0, 0),
    )


def _base_ticker_data(ticker="AAPL", score=8.0, direction="bullish"):
    """最小完整 ticker 数据，满足所有 builder 的字段要求。"""
    return (ticker, {
        "final_score": score,
        "direction": direction,
        "narrative": f"{ticker} narrative",
        "resonance": {"resonance_detected": True, "details": []},
        "agent_breakdown": {"bullish": 4, "bearish": 1, "neutral": 2},
        "data_real_pct": 85.0,
        "agent_details": {
            "ScoutBeeNova": {
                "discovery": f"{ticker} scout discovery",
                "details": {
                    "insider": {
                        "sentiment": "bullish",
                        "dollar_bought": 500000,
                        "dollar_sold": 0,
                        "filings": 3,
                        "notable_trades": [
                            {"insider": "CEO", "code_desc": "Purchase", "shares": 10000}
                        ],
                    },
                    "crowding_score": 45,
                },
            },
            "OracleBeeEcho": {
                "discovery": f"{ticker} oracle discovery",
                "details": {
                    "iv_rank": 72,
                    "put_call_ratio": 0.85,
                    "gamma_exposure": "positive",
                    "unusual_activity": [
                        {"type": "sweep", "strike": "150", "volume": 5000, "bullish": True}
                    ],
                },
            },
            "BuzzBeeWhisper": {
                "discovery": f"{ticker} buzz discovery",
                "details": {
                    "sentiment_pct": 68,
                    "momentum_5d": 3.2,
                    "volume_ratio": 1.5,
                    "reddit_mentions": 120,
                },
            },
            "ChronosBeeHorizon": {
                "discovery": f"{ticker} chronos discovery",
                "details": {
                    "next_earnings": "2026-04-15",
                    "upcoming_events": [
                        {"date": "2026-04-01", "event": "Product launch"}
                    ],
                    "recent_events": [
                        {"description": "FDA approval received"}
                    ],
                },
            },
            "RivalBeeVanguard": {
                "discovery": f"{ticker} rival discovery",
                "details": {
                    "ml_prediction": {"direction": "bullish", "confidence": 0.78},
                    "peer_comparison": ["MSFT", "GOOG"],
                },
            },
            "GuardBeeSentinel": {
                "discovery": f"{ticker} guard cross-validation passed",
                "details": {},
            },
            "BearBeeContrarian": {
                "discovery": f"{ticker} bear discovery",
                "direction": "bearish",
                "details": {
                    "bear_score": 6.5,
                    "bearish_signals": ["Overvaluation risk", "Insider selling trend"],
                    "data_sources": {"sec": "sec_api", "options": "options_api"},
                },
                "llm_thesis": "Valuation stretched",
                "llm_key_risks": ["Multiple compression", "Earnings miss"],
                "llm_contrarian_insight": "Market ignoring headwinds",
            },
        },
    })


def _make_swarm_results(n=3):
    """生成 n 个标的的 swarm_results dict。"""
    tickers = ["AAPL", "TSLA", "NVDA", "AMZN", "META"][:n]
    scores = [8.5, 7.2, 6.0, 5.5, 4.0][:n]
    results = {}
    for ticker, score in zip(tickers, scores):
        _, data = _base_ticker_data(ticker, score)
        results[ticker] = data
    return results


def _sorted(swarm_results):
    return sorted(swarm_results.items(), key=lambda x: x[1]["final_score"], reverse=True)


# ---------- full report ----------

class TestFullReport:
    def test_full_report_structure(self):
        """完整报告包含所有必需 section header"""
        reporter = _make_reporter()
        sr = _make_swarm_results(3)
        report = generate_swarm_markdown_report(reporter, sr)
        assert "## 1) 今日摘要" in report
        assert "## 2) 今日聪明钱动向" in report
        assert "## 3) 市场隐含预期" in report
        assert "## 4) X 情绪汇总" in report
        assert "## 5) 财报/事件催化剂" in report
        assert "## 6) 竞争格局分析" in report
        assert "## 6.5) 看空对冲观点" in report
        assert "## 7) 综合判断 & 信号强度" in report
        assert "## 8) 数据来源 & 免责声明" in report

    def test_disclaimer_present(self):
        """免责声明始终存在"""
        reporter = _make_reporter()
        sr = _make_swarm_results(1)
        report = generate_swarm_markdown_report(reporter, sr)
        assert DISCLAIMER_FULL in report


# ---------- individual builders ----------

class TestBuildSummary:
    def test_summary_top3(self):
        sr = _make_swarm_results(5)
        lines = _build_summary(_sorted(sr), len(sr))
        text = "\n".join(lines)
        assert "扫描标的：5 个" in text
        # top3 by score: AAPL(8.5), TSLA(7.2), NVDA(6.0)
        assert "**AAPL**" in text
        assert "**TSLA**" in text
        assert "**NVDA**" in text

    def test_summary_resonance_count(self):
        sr = _make_swarm_results(2)
        lines = _build_summary(_sorted(sr), len(sr))
        text = "\n".join(lines)
        # all have resonance_detected=True
        assert "共振信号：2/2" in text


class TestBuildSmartMoney:
    def test_insider_details(self):
        sr = _make_swarm_results(1)
        lines = _build_smart_money(_sorted(sr))
        text = "\n".join(lines)
        assert "内幕交易情绪" in text
        assert "$500,000" in text
        assert "拥挤度" in text


class TestBuildMarketExpectations:
    def test_options_data(self):
        sr = _make_swarm_results(1)
        lines = _build_market_expectations(_sorted(sr))
        text = "\n".join(lines)
        assert "IV Rank" in text
        assert "Put/Call Ratio" in text
        assert "异常活动" in text


class TestBuildSentiment:
    def test_buzz_data(self):
        sr = _make_swarm_results(1)
        lines = _build_sentiment(_sorted(sr))
        text = "\n".join(lines)
        assert "看多情绪：68%" in text
        assert "5 日动量" in text
        assert "Reddit 热度" in text


class TestBuildCatalysts:
    def test_chronos_events(self):
        sr = _make_swarm_results(1)
        lines = _build_catalysts(_sorted(sr))
        text = "\n".join(lines)
        assert "下次财报：2026-04-15" in text
        assert "Product launch" in text
        assert "[已发生]" in text


class TestBuildCompetitive:
    def test_rival_ml(self):
        sr = _make_swarm_results(1)
        lines = _build_competitive(_sorted(sr))
        text = "\n".join(lines)
        assert "ML 预测方向" in text
        assert "同业对标" in text


class TestBuildBearContrarian:
    def test_bear_thesis(self):
        sr = _make_swarm_results(1)
        lines = _build_bear_contrarian(_sorted(sr))
        text = "\n".join(lines)
        assert "看空警告" in text
        assert "Overvaluation risk" in text
        assert "AI看空论点" in text
        assert "Valuation stretched" in text
        assert "反对洞察" in text

    def test_no_bearish_signals(self):
        """无看空信号时显示默认文本"""
        _, data = _base_ticker_data("TEST", 7.0)
        data["agent_details"]["BearBeeContrarian"] = {
            "discovery": "", "direction": "neutral",
            "details": {"bear_score": 0, "bearish_signals": [], "data_sources": {}},
        }
        lines = _build_bear_contrarian([("TEST", data)])
        text = "\n".join(lines)
        assert "未发现显著看空信号" in text


class TestBuildCompositeJudgment:
    def test_judgment_table(self):
        sr = _make_swarm_results(2)
        lines = _build_composite_judgment(_sorted(sr))
        text = "\n".join(lines)
        assert "| **AAPL**" in text
        assert "| **TSLA**" in text
        assert "交叉验证详情" in text

    def test_score_adjustments_inline(self):
        """评分调整在 composite 中使用模块级 format_score_adjustments"""
        _, data = _base_ticker_data("ADJ", 7.0)
        data["bear_cap_applied"] = True
        data["bear_strength"] = 8.0
        data["rule_score"] = 6.5
        lines = _build_composite_judgment([("ADJ", data)])
        text = "\n".join(lines)
        assert "评分调整说明" in text
        assert "反对蜂看空" in text


# ---------- optional sections ----------

class TestBuildConcentration:
    def test_concentration_none(self):
        assert _build_concentration(None) == []

    def test_concentration_with_data(self):
        conc = {
            "sector_breakdown": {"Tech": {"pct": 60, "tickers": ["AAPL", "NVDA"]}},
            "concentration_risk": "high",
            "risk_score": 8.0,
            "correlation_warnings": [
                {"pair": "AAPL-NVDA", "correlation": 0.85, "risk": "high"}
            ],
            "recommendations": ["Diversify into healthcare"],
        }
        lines = _build_concentration(conc)
        text = "\n".join(lines)
        assert "集中度分析" in text
        assert "Tech" in text
        assert "AAPL-NVDA" in text
        assert "Diversify" in text


class TestBuildCrossTicker:
    def test_cross_ticker_none(self):
        assert _build_cross_ticker(None) == []

    def test_cross_ticker_with_data(self):
        ct = {
            "sector_momentum": {"Tech": "leading"},
            "cross_ticker_insights": [
                {"tickers": ["AAPL", "NVDA"], "type": "correlation", "insight": "co-move"}
            ],
            "correlation_warnings": ["High tech concentration"],
            "sector_rotation_signal": "Rotating into value",
            "portfolio_adjustment_hints": ["Reduce tech weight"],
        }
        lines = _build_cross_ticker(ct)
        text = "\n".join(lines)
        assert "跨标的关联分析" in text
        assert "板块动量" in text
        assert "轮动信号" in text


class TestBuildMacro:
    def test_macro_none(self):
        assert _build_macro(None) == []

    def test_macro_fallback_excluded(self):
        assert _build_macro({"data_source": "fallback"}) == []

    def test_macro_with_data(self):
        macro = {
            "data_source": "fred",
            "macro_regime": "risk_on",
            "macro_score": 7.5,
            "vix": 15.2,
            "vix_regime": "low",
            "treasury_10y": 4.25,
            "rate_environment": "stable",
            "spx_change_pct": 1.5,
            "market_trend": "uptrend",
            "dollar_trend": "weakening",
            "macro_headwinds": ["Inflation sticky"],
            "macro_tailwinds": ["Strong earnings"],
        }
        lines = _build_macro(macro)
        text = "\n".join(lines)
        assert "宏观环境" in text
        assert "VIX" in text
        assert "逆风" in text
        assert "顺风" in text


class TestBuildBacktest:
    def test_backtest_none(self):
        assert _build_backtest(None) == []

    def test_backtest_zero_checked(self):
        assert _build_backtest({"total_checked": 0}) == []

    def test_backtest_with_data(self):
        bt = {
            "total_checked": 20,
            "overall_accuracy": 0.75,
            "correct_count": 15,
            "avg_return": 2.3,
            "by_ticker": {
                "AAPL": {"accuracy": 0.8, "total": 5, "avg_return": 3.1},
                "TSLA": {"accuracy": 0.6, "total": 4, "avg_return": 1.2},
            },
            "by_direction": {
                "bullish": {"accuracy": 0.8, "total": 12},
                "bearish": {"accuracy": 0.5, "total": 8},
            },
        }
        lines = _build_backtest(bt)
        text = "\n".join(lines)
        assert "历史预测准确率" in text
        assert "75.0%" in text
        assert "AAPL" in text
        assert "按方向" in text


# ---------- format_score_adjustments ----------

class TestFormatScoreAdjustments:
    def test_empty_data(self):
        assert format_score_adjustments({}) == ""

    def test_bear_cap(self):
        result = format_score_adjustments({
            "bear_cap_applied": True,
            "bear_strength": 8.0,
            "rule_score": 6.5,
        })
        assert "反对蜂看空" in result

    def test_dq_penalty(self):
        result = format_score_adjustments({
            "dq_penalty_applied": True,
            "data_real_pct": 40.0,
            "dq_quality_factor": 0.75,
        })
        assert "数据质量" in result
        assert "40%" in result

    def test_low_coverage(self):
        result = format_score_adjustments({
            "dimension_coverage_pct": 60.0,
            "dimension_status": {"signal": "present", "catalyst": "missing"},
        })
        assert "维度覆盖" in result
        assert "catalyst" in result
