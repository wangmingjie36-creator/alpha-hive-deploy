"""
Tests for CrowdingDetector - 拥挤度检测系统
"""

import pytest
from crowding_detector import CrowdingDetector


# ==================== Helpers ====================

def _low_activity_metrics():
    """Low activity => low crowding score."""
    return {
        "stocktwits_messages_per_day": 500,
        "google_trends_percentile": 10.0,
        "bullish_agents": 1,
        "polymarket_odds_change_24h": 0.5,
        "seeking_alpha_page_views": 2000,
        "short_float_ratio": 0.05,
        "price_momentum_5d": 1.0,
    }


def _high_activity_metrics():
    """High activity => high crowding score."""
    return {
        "stocktwits_messages_per_day": 80000,
        "google_trends_percentile": 95.0,
        "bullish_agents": 6,
        "polymarket_odds_change_24h": 15.0,
        "seeking_alpha_page_views": 200000,
        "short_float_ratio": 0.35,
        "price_momentum_5d": 20.0,
    }


def _medium_activity_metrics():
    """Medium activity => mid-range crowding score."""
    return {
        "stocktwits_messages_per_day": 25000,
        "google_trends_percentile": 45.0,
        "bullish_agents": 3,
        "polymarket_odds_change_24h": 6.0,
        "seeking_alpha_page_views": 55000,
        "short_float_ratio": 0.10,
        "price_momentum_5d": 5.0,
    }


# ==================== TestCalculateCrowdingScore ====================

class TestCalculateCrowdingScore:
    """Tests for CrowdingDetector.calculate_crowding_score"""

    def test_low_activity_produces_low_score(self):
        detector = CrowdingDetector("TEST")
        score, components = detector.calculate_crowding_score(_low_activity_metrics())
        assert score < 30, f"Expected low score (<30) for low activity, got {score}"

    def test_high_activity_produces_high_score(self):
        detector = CrowdingDetector("TEST")
        score, components = detector.calculate_crowding_score(_high_activity_metrics())
        assert score > 60, f"Expected high score (>60) for high activity, got {score}"

    def test_empty_metrics_returns_valid_score(self):
        """Empty metrics dict should not crash and should return a valid score."""
        detector = CrowdingDetector("TEST")
        score, components = detector.calculate_crowding_score({})
        assert 0 <= score <= 100
        assert isinstance(components, dict)

    def test_score_always_in_0_100_range(self):
        """Score is clamped to [0, 100] regardless of input."""
        detector = CrowdingDetector("TEST")
        for metrics in [_low_activity_metrics(), _medium_activity_metrics(), _high_activity_metrics(), {}]:
            score, _ = detector.calculate_crowding_score(metrics)
            assert 0 <= score <= 100, f"Score {score} out of [0, 100] range"

    def test_returns_tuple_of_score_and_dict(self):
        """calculate_crowding_score returns (float, dict)."""
        detector = CrowdingDetector("TEST")
        result = detector.calculate_crowding_score(_low_activity_metrics())
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], (int, float))
        assert isinstance(result[1], dict)

    def test_component_scores_contain_expected_keys(self):
        """Component scores dict should have all weight keys."""
        detector = CrowdingDetector("TEST")
        _, components = detector.calculate_crowding_score(_medium_activity_metrics())
        for key in detector.weights:
            assert key in components, f"Missing component key: {key}"

    def test_medium_activity_between_extremes(self):
        detector = CrowdingDetector("TEST")
        low_score, _ = detector.calculate_crowding_score(_low_activity_metrics())
        mid_score, _ = detector.calculate_crowding_score(_medium_activity_metrics())
        high_score, _ = detector.calculate_crowding_score(_high_activity_metrics())
        assert low_score < mid_score < high_score


# ==================== TestGetCrowdingCategory ====================

class TestGetCrowdingCategory:
    """Tests for CrowdingDetector.get_crowding_category"""

    def test_low_score_returns_low_category(self):
        detector = CrowdingDetector("TEST")
        category, color = detector.get_crowding_category(15.0)
        assert category == "低拥挤度"
        assert color == "green"

    def test_medium_score_returns_medium_category(self):
        detector = CrowdingDetector("TEST")
        category, color = detector.get_crowding_category(45.0)
        assert category == "中等拥挤度"
        assert color == "yellow"

    def test_high_score_returns_high_category(self):
        detector = CrowdingDetector("TEST")
        category, color = detector.get_crowding_category(75.0)
        assert category == "高拥挤度"
        assert color == "red"

    def test_boundary_30_is_medium(self):
        """Score == 30 falls into medium category (< 30 is low, >= 30 is medium)."""
        detector = CrowdingDetector("TEST")
        category, _ = detector.get_crowding_category(30.0)
        assert category == "中等拥挤度"

    def test_boundary_60_is_high(self):
        """Score == 60 falls into high category (< 60 is medium, >= 60 is high)."""
        detector = CrowdingDetector("TEST")
        category, _ = detector.get_crowding_category(60.0)
        assert category == "高拥挤度"

    def test_boundary_just_below_30(self):
        detector = CrowdingDetector("TEST")
        category, color = detector.get_crowding_category(29.99)
        assert category == "低拥挤度"
        assert color == "green"

    def test_boundary_just_below_60(self):
        detector = CrowdingDetector("TEST")
        category, color = detector.get_crowding_category(59.99)
        assert category == "中等拥挤度"
        assert color == "yellow"

    def test_returns_tuple(self):
        detector = CrowdingDetector("TEST")
        result = detector.get_crowding_category(50.0)
        assert isinstance(result, tuple) and len(result) == 2

    def test_zero_score(self):
        detector = CrowdingDetector("TEST")
        category, color = detector.get_crowding_category(0.0)
        assert category == "低拥挤度"

    def test_max_score(self):
        detector = CrowdingDetector("TEST")
        category, color = detector.get_crowding_category(100.0)
        assert category == "高拥挤度"


# ==================== TestGetAdjustmentFactor ====================

class TestGetAdjustmentFactor:
    """Tests for CrowdingDetector.get_adjustment_factor"""

    def test_low_score_gives_boost(self):
        detector = CrowdingDetector("TEST")
        factor = detector.get_adjustment_factor(10.0)
        assert factor == 1.2

    def test_medium_score_gives_slight_discount(self):
        detector = CrowdingDetector("TEST")
        factor = detector.get_adjustment_factor(45.0)
        assert factor == 0.95

    def test_high_score_gives_heavy_discount(self):
        detector = CrowdingDetector("TEST")
        factor = detector.get_adjustment_factor(80.0)
        assert factor == 0.70

    def test_boundary_30_is_medium_discount(self):
        """Score == 30: boundary for medium range (>= 30)."""
        detector = CrowdingDetector("TEST")
        factor = detector.get_adjustment_factor(30.0)
        assert factor == 0.95

    def test_boundary_60_is_heavy_discount(self):
        """Score == 60: boundary for high range (>= 60)."""
        detector = CrowdingDetector("TEST")
        factor = detector.get_adjustment_factor(60.0)
        assert factor == 0.70

    def test_boundary_just_below_30(self):
        detector = CrowdingDetector("TEST")
        factor = detector.get_adjustment_factor(29.99)
        assert factor == 1.2

    def test_boundary_just_below_60(self):
        detector = CrowdingDetector("TEST")
        factor = detector.get_adjustment_factor(59.99)
        assert factor == 0.95

    def test_zero_score(self):
        detector = CrowdingDetector("TEST")
        assert detector.get_adjustment_factor(0.0) == 1.2

    def test_max_score(self):
        detector = CrowdingDetector("TEST")
        assert detector.get_adjustment_factor(100.0) == 0.70


# ==================== TestGenerateHtmlSection ====================

class TestGenerateHtmlSection:
    """Tests for CrowdingDetector.generate_html_section"""

    def test_produces_html_string(self):
        detector = CrowdingDetector("NVDA")
        html = detector.generate_html_section(_medium_activity_metrics(), 7.5)
        assert isinstance(html, str)
        assert len(html) > 100

    def test_contains_ticker_name(self):
        detector = CrowdingDetector("AAPL")
        html = detector.generate_html_section(_medium_activity_metrics(), 8.0)
        assert "AAPL" in html

    def test_contains_crowding_section_id(self):
        detector = CrowdingDetector("TSLA")
        html = detector.generate_html_section(_low_activity_metrics(), 6.0)
        assert 'crowding-analysis-TSLA' in html

    def test_contains_crowding_badge(self):
        detector = CrowdingDetector("TEST")
        html = detector.generate_html_section(_high_activity_metrics(), 7.0)
        assert "crowding-badge" in html

    def test_contains_meter_elements(self):
        detector = CrowdingDetector("TEST")
        html = detector.generate_html_section(_medium_activity_metrics(), 7.0)
        assert "meter-fill" in html
        assert "meter-value" in html

    def test_contains_impact_table(self):
        detector = CrowdingDetector("TEST")
        html = detector.generate_html_section(_medium_activity_metrics(), 7.0)
        assert "impact-table" in html

    def test_contains_hedge_recommendations(self):
        detector = CrowdingDetector("TEST")
        html = detector.generate_html_section(_high_activity_metrics(), 8.0)
        assert "hedge-recommendations" in html


# ==================== TestGetHedgeRecommendations ====================

class TestGetHedgeRecommendations:
    """Tests for CrowdingDetector.get_hedge_recommendations"""

    def test_high_crowding_returns_multiple_strategies(self):
        detector = CrowdingDetector("TEST")
        recs = detector.get_hedge_recommendations(75.0)
        assert len(recs) == 3

    def test_medium_crowding_returns_partial_profit(self):
        detector = CrowdingDetector("TEST")
        recs = detector.get_hedge_recommendations(45.0)
        assert len(recs) == 1
        assert "止盈" in recs[0]["strategy"]

    def test_low_crowding_returns_add_position(self):
        detector = CrowdingDetector("TEST")
        recs = detector.get_hedge_recommendations(15.0)
        assert len(recs) == 1
        assert "Add Position" in recs[0]["strategy"]

    def test_recommendations_have_required_keys(self):
        detector = CrowdingDetector("TEST")
        for score in [15.0, 45.0, 75.0]:
            recs = detector.get_hedge_recommendations(score)
            for rec in recs:
                assert "strategy" in rec
                assert "description" in rec
                assert "benefit" in rec
                assert "suitable_for" in rec
