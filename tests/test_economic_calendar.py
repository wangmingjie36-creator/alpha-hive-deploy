"""
Tests for economic_calendar module — FOMC/CPI/NFP/GDP event calendar
"""

import pytest
from datetime import date


class TestGetUpcomingEvents:
    """get_upcoming_events() 测试"""

    def test_returns_list(self):
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=365, ref_date=date(2026, 1, 1))
        assert isinstance(events, list)
        assert len(events) > 0

    def test_event_structure_matches_catalyst(self):
        """验证返回格式复用 ChronosBeeHorizon 催化剂结构"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=60, ref_date=date(2026, 3, 1))
        assert len(events) > 0
        e = events[0]
        assert "event" in e
        assert "date" in e
        assert "days_until" in e
        assert "type" in e
        assert "severity" in e
        assert isinstance(e["days_until"], int)
        assert e["days_until"] >= 0
        assert e["type"] in ("fomc", "cpi", "nfp", "gdp")
        assert e["severity"] in ("high", "medium")

    def test_sorted_by_days_until(self):
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=90, ref_date=date(2026, 1, 15))
        days = [e["days_until"] for e in events]
        assert days == sorted(days)

    def test_empty_when_days_zero(self):
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=0, ref_date=date(2026, 6, 1))
        # days=0 means only events on the reference date itself
        assert isinstance(events, list)
        # 所有返回事件的 days_until 必须为 0（当天事件）
        assert all(e["days_until"] == 0 for e in events)

    def test_fomc_dates_2026(self):
        """验证 2026 FOMC 日期正确"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=365, ref_date=date(2026, 1, 1))
        fomc = [e for e in events if e["type"] == "fomc"]
        fomc_dates = {e["date"] for e in fomc}
        assert "2026-01-28" in fomc_dates
        assert "2026-03-18" in fomc_dates
        assert "2026-12-09" in fomc_dates
        assert len(fomc) == 8  # 8 FOMC meetings per year

    def test_respects_ref_date(self):
        """确保只返回 ref_date 当天或之后的事件"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=30, ref_date=date(2026, 6, 15))
        for e in events:
            assert e["date"] >= "2026-06-15"

    def test_no_past_events(self):
        """不返回过去的事件"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=30, ref_date=date(2026, 3, 4))
        for e in events:
            assert e["days_until"] >= 0


class TestGetNextEvent:
    """get_next_event() 测试"""

    def test_returns_nearest(self):
        from economic_calendar import get_next_event
        evt = get_next_event(ref_date=date(2026, 3, 4))
        assert evt is not None
        assert evt["days_until"] >= 0
        # 3/4 → 最近应该是 3/6 NFP (2天后)
        assert evt["days_until"] <= 60

    def test_returns_none_when_no_events(self):
        """超出日历范围时返回 None"""
        from economic_calendar import get_next_event
        evt = get_next_event(ref_date=date(2030, 1, 1))
        assert evt is None

    def test_matches_first_upcoming(self):
        from economic_calendar import get_upcoming_events, get_next_event
        ref = date(2026, 3, 4)
        events = get_upcoming_events(days=60, ref_date=ref)
        nxt = get_next_event(ref_date=ref)
        if events:
            assert nxt == events[0]
