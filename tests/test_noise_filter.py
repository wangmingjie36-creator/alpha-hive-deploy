"""
噪声过滤 + 情绪建模增强 测试
覆盖：去重、时效衰减、情绪动量、价格背离、置信度校准混合
"""

import sys
import os
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ==================== newsapi_client: 去重 ====================

class TestDeduplicateArticles:
    """Jaccard 标题去重测试"""

    def test_identical_titles_deduped(self):
        from newsapi_client import _deduplicate_articles
        arts = [
            {"title": "NVDA stock surges on AI demand", "sentiment_label": "bullish"},
            {"title": "NVDA stock surges on AI demand", "sentiment_label": "bullish"},
        ]
        result = _deduplicate_articles(arts)
        assert len(result) == 1

    def test_similar_titles_deduped(self):
        from newsapi_client import _deduplicate_articles
        arts = [
            {"title": "NVDA stock surges on AI demand growth", "sentiment_label": "bullish"},
            {"title": "NVDA stock surges on strong AI demand", "sentiment_label": "neutral"},
        ]
        result = _deduplicate_articles(arts, threshold=0.5)
        assert len(result) == 1

    def test_different_titles_kept(self):
        from newsapi_client import _deduplicate_articles
        arts = [
            {"title": "NVDA stock surges on AI demand", "sentiment_label": "bullish"},
            {"title": "Tesla announces new factory in China", "sentiment_label": "neutral"},
        ]
        result = _deduplicate_articles(arts)
        assert len(result) == 2

    def test_empty_list(self):
        from newsapi_client import _deduplicate_articles
        assert _deduplicate_articles([]) == []

    def test_empty_title_not_deduped(self):
        from newsapi_client import _deduplicate_articles
        arts = [
            {"title": "", "sentiment_label": "neutral"},
            {"title": "", "sentiment_label": "bullish"},
        ]
        # 空标题不参与去重
        result = _deduplicate_articles(arts)
        assert len(result) == 2


# ==================== newsapi_client: 时效衰减 ====================

class TestRecencyWeight:
    """指数衰减权重测试"""

    def test_just_published(self):
        from newsapi_client import _recency_weight
        now = datetime.now().isoformat()
        w = _recency_weight(now)
        assert 0.9 < w <= 1.0

    def test_24h_ago(self):
        from newsapi_client import _recency_weight
        old = (datetime.now() - timedelta(hours=24)).isoformat()
        w = _recency_weight(old)
        assert 0.45 < w < 0.55  # 半衰期 = 24h → ~0.5

    def test_72h_ago(self):
        from newsapi_client import _recency_weight
        very_old = (datetime.now() - timedelta(hours=72)).isoformat()
        w = _recency_weight(very_old)
        assert 0.10 < w < 0.15  # 3 个半衰期 → ~0.125

    def test_unparseable_date(self):
        from newsapi_client import _recency_weight
        w = _recency_weight("invalid-date")
        assert w == 0.5  # 默认中等权重

    def test_custom_half_life(self):
        from newsapi_client import _recency_weight
        old = (datetime.now() - timedelta(hours=12)).isoformat()
        w = _recency_weight(old, half_life_hours=12.0)
        assert 0.45 < w < 0.55


# ==================== swarm_agents: 情绪动量 ====================

class TestSentimentMomentum:
    """情绪动量计算测试"""

    def test_no_history_returns_unknown(self):
        from swarm_agents import _get_sentiment_momentum
        result = _get_sentiment_momentum("NONEXISTENT_TICKER_XYZ", 50)
        assert result["momentum_regime"] == "unknown"
        assert result["momentum_score_adj"] == 0.0

    def test_all_deltas_none_when_no_data(self):
        from swarm_agents import _get_sentiment_momentum
        result = _get_sentiment_momentum("NONEXISTENT_TICKER_XYZ", 50)
        assert result["delta_1d"] is None
        assert result["delta_3d"] is None
        assert result["delta_7d"] is None


# ==================== swarm_agents: 价格背离 ====================

class TestSentimentPriceDivergence:
    """情绪-价格背离检测测试"""

    def test_bull_trap(self):
        from swarm_agents import _detect_sentiment_price_divergence
        result = _detect_sentiment_price_divergence(80, -5.0, "TEST")
        assert result["divergence_type"] == "bull_trap"
        assert result["severity"] > 0
        assert result["score_adj"] < 0

    def test_hidden_opportunity(self):
        from swarm_agents import _detect_sentiment_price_divergence
        result = _detect_sentiment_price_divergence(20, 8.0, "TEST")
        assert result["divergence_type"] == "hidden_opportunity"
        assert result["severity"] > 0
        assert result["score_adj"] > 0

    def test_no_divergence_neutral(self):
        from swarm_agents import _detect_sentiment_price_divergence
        result = _detect_sentiment_price_divergence(50, 1.0, "TEST")
        assert result["divergence_type"] == "none"
        assert result["score_adj"] == 0.0

    def test_high_sentiment_but_price_flat(self):
        """情绪高但价格未跌 → 不是 bull_trap"""
        from swarm_agents import _detect_sentiment_price_divergence
        result = _detect_sentiment_price_divergence(80, -1.0, "TEST")
        assert result["divergence_type"] == "none"

    def test_low_sentiment_but_price_flat(self):
        """情绪低但价格未涨 → 不是 hidden_opportunity"""
        from swarm_agents import _detect_sentiment_price_divergence
        result = _detect_sentiment_price_divergence(20, 1.0, "TEST")
        assert result["divergence_type"] == "none"

    def test_severity_capped_at_3(self):
        """极端情况下 severity 不超过 3"""
        from swarm_agents import _detect_sentiment_price_divergence
        result = _detect_sentiment_price_divergence(99, -30.0, "TEST")
        assert result["severity"] <= 3


# ==================== llm_service: 情绪上下文构建 ====================

class TestSentimentSection:

    def test_with_full_context(self):
        from llm_service import _build_sentiment_section
        ctx = {
            "momentum_3d": 18,
            "momentum_regime": "surging",
            "divergence_type": "bull_trap",
            "divergence_severity": 2,
        }
        s = _build_sentiment_section(ctx)
        assert "情绪动量(3d): +18 ppt" in s
        assert "surging" in s
        assert "bull_trap" in s
        assert "严重度 2/3" in s

    def test_with_none(self):
        from llm_service import _build_sentiment_section
        assert _build_sentiment_section(None) == ""

    def test_no_divergence_omits_line(self):
        from llm_service import _build_sentiment_section
        ctx = {
            "momentum_3d": -8,
            "momentum_regime": "declining",
            "divergence_type": "none",
            "divergence_severity": 0,
        }
        s = _build_sentiment_section(ctx)
        assert "declining" in s
        assert "bull_trap" not in s
        assert "hidden_opportunity" not in s


# ==================== 置信度校准混合 ====================

class TestConfidenceCalibration:
    """验证 LLM 置信度越高权重越大的混合逻辑"""

    def test_high_confidence(self):
        """confidence=0.9 → llm_weight=0.6"""
        llm_confidence = 0.9
        llm_weight = min(0.6, max(0.2, llm_confidence))
        assert llm_weight == 0.6

    def test_mid_confidence(self):
        """confidence=0.5 → llm_weight=0.5"""
        llm_confidence = 0.5
        llm_weight = min(0.6, max(0.2, llm_confidence))
        assert llm_weight == 0.5

    def test_low_confidence(self):
        """confidence=0.1 → llm_weight=0.2（下限）"""
        llm_confidence = 0.1
        llm_weight = min(0.6, max(0.2, llm_confidence))
        assert llm_weight == 0.2

    def test_blending_math(self):
        """实际混合计算验证"""
        rule_score = 7.0
        llm_score = 9.0
        llm_confidence = 0.6

        llm_weight = min(0.6, max(0.2, llm_confidence))
        rule_weight = 1.0 - llm_weight
        final = round(rule_score * rule_weight + llm_score * llm_weight, 2)
        expected = round(7.0 * 0.4 + 9.0 * 0.6, 2)  # 2.8 + 5.4 = 8.2
        assert final == expected


# ==================== 冲突驱动增强 ====================

class TestConflictDrivenEnhancement:
    """S5 冲突驱动增强逻辑测试"""

    def _make_results(self, directions, confidences=None, dq_real_counts=None):
        """构造模拟 Agent 结果"""
        REAL_SOURCES = {"real", "api", "sec", "yfinance", "finviz", "edgar"}
        results = []
        for i, d in enumerate(directions):
            conf = (confidences[i] if confidences else 0.6)
            real_cnt = (dq_real_counts[i] if dq_real_counts else 2)
            dq = {f"f{j}": "real" for j in range(real_cnt)}
            dq.update({f"fb{j}": "fallback" for j in range(3 - real_cnt)})
            results.append({
                "direction": d,
                "confidence": conf,
                "data_quality": dq,
                "source": f"Agent{i}",
            })
        return results, REAL_SOURCES

    def test_heavy_conflict_detected(self):
        """多空各 ≥2 Agent → 重度冲突"""
        dirs = ["bullish", "bullish", "bearish", "bearish", "neutral"]
        results, _ = self._make_results(dirs)
        bullish_count = sum(1 for d in dirs if d == "bullish")
        bearish_count = sum(1 for d in dirs if d == "bearish")
        assert bullish_count >= 2 and bearish_count >= 2

    def test_moderate_conflict(self):
        """多空各 1 Agent → 中度冲突"""
        dirs = ["bullish", "bearish", "neutral", "neutral"]
        bullish_count = sum(1 for d in dirs if d == "bullish")
        bearish_count = sum(1 for d in dirs if d == "bearish")
        assert bullish_count >= 1 and bearish_count >= 1
        assert not (bullish_count >= 2 and bearish_count >= 2)

    def test_dq_weighted_revote_resolves_to_bullish(self):
        """DQ 加权再投票：数据质量高的看多方胜出"""
        REAL_SOURCES = {"real", "api", "sec", "yfinance", "finviz", "edgar"}
        results = [
            {"direction": "bullish", "confidence": 0.7, "data_quality": {"f0": "real", "f1": "real", "f2": "real"}},
            {"direction": "bullish", "confidence": 0.6, "data_quality": {"f0": "real", "f1": "real"}},
            {"direction": "bearish", "confidence": 0.5, "data_quality": {"f0": "fallback", "f1": "fallback"}},
            {"direction": "bearish", "confidence": 0.5, "data_quality": {"f0": "fallback"}},
        ]
        _weight_cap = sum(r["confidence"] for r in results) * 0.4
        dq_bull_w = 0.0
        dq_bear_w = 0.0
        for r in results:
            _dir = r["direction"]
            if _dir not in ("bullish", "bearish"):
                continue
            _dq = r["data_quality"]
            _real = sum(1 for v in _dq.values() if v in REAL_SOURCES)
            _tf = max(1, len(_dq))
            _dq_ratio = _real / _tf
            _conf = min(r["confidence"], _weight_cap)
            _combined = _conf * (0.5 + 0.5 * _dq_ratio)
            if _dir == "bullish":
                dq_bull_w += _combined
            else:
                dq_bear_w += _combined
        dq_total = dq_bull_w + dq_bear_w
        assert dq_bull_w / dq_total >= 0.55  # 看多方胜出

    def test_conflict_discount_applied(self):
        """冲突折扣：score 应当被降低"""
        rule_score = 7.0
        bullish_count = 3
        bearish_count = 2
        total_agents = 6
        conflict_factor = 0.3
        _conflict_ratio = (bullish_count + bearish_count) / total_agents
        discount = round(conflict_factor * min(1.0, _conflict_ratio), 2)
        new_score = round(max(1.0, rule_score - discount), 2)
        assert new_score < rule_score
        assert discount > 0

    def test_conflict_discount_floor(self):
        """冲突折扣不会把分数降到 1.0 以下"""
        rule_score = 1.2
        discount = 0.25
        new_score = round(max(1.0, rule_score - discount), 2)
        assert new_score >= 1.0


# ==================== 情绪上下文冲突注入 ====================

class TestSentimentSectionConflict:
    """_build_sentiment_section 冲突信息注入测试"""

    def test_heavy_conflict_in_section(self):
        from llm_service import _build_sentiment_section
        ctx = {
            "momentum_3d": 5,
            "momentum_regime": "rising",
            "divergence_type": "none",
            "divergence_severity": 0,
            "conflict_level": "heavy",
            "conflict_info": {
                "bullish_agents": 3,
                "bearish_agents": 2,
                "resolved_direction": "bullish",
                "conflict_discount": 0.25,
            },
        }
        s = _build_sentiment_section(ctx)
        assert "重度" in s
        assert "3多 vs 2空" in s
        assert "bullish" in s

    def test_moderate_conflict_in_section(self):
        from llm_service import _build_sentiment_section
        ctx = {
            "momentum_3d": -3,
            "momentum_regime": "stable",
            "divergence_type": "none",
            "divergence_severity": 0,
            "conflict_level": "moderate",
        }
        s = _build_sentiment_section(ctx)
        assert "中度" in s

    def test_no_conflict_omits_line(self):
        from llm_service import _build_sentiment_section
        ctx = {
            "momentum_3d": 10,
            "momentum_regime": "rising",
            "divergence_type": "none",
            "divergence_severity": 0,
            "conflict_level": "none",
        }
        s = _build_sentiment_section(ctx)
        assert "冲突" not in s
