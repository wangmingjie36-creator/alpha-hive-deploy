"""Tests for is_trading_day module."""

from datetime import date

import pytest

from is_trading_day import (
    _easter,
    _nth_weekday,
    _observed,
    is_trading_day,
    us_market_holidays,
)


class TestEaster:
    """Verify the Meeus Easter algorithm against known dates."""

    def test_easter_2024(self):
        assert _easter(2024) == date(2024, 3, 31)

    def test_easter_2025(self):
        assert _easter(2025) == date(2025, 4, 20)

    def test_easter_2026(self):
        assert _easter(2026) == date(2026, 4, 5)


class TestObserved:
    """Federal holiday 'observed' rule: Sat->Fri, Sun->Mon, weekday unchanged."""

    def test_saturday_observed_on_friday(self):
        # July 4, 2026 is Saturday -> observed on Friday July 3
        assert _observed(date(2026, 7, 4)) == date(2026, 7, 3)

    def test_sunday_observed_on_monday(self):
        # Jan 1, 2023 is Sunday -> observed on Monday Jan 2
        assert _observed(date(2023, 1, 1)) == date(2023, 1, 2)

    def test_weekday_unchanged(self):
        # July 4, 2025 is Friday -> stays July 4
        assert _observed(date(2025, 7, 4)) == date(2025, 7, 4)


class TestNthWeekday:
    """Verify nth-weekday calculation for known holidays."""

    def test_mlk_day_2025(self):
        # 3rd Monday in January 2025 = Jan 20
        assert _nth_weekday(2025, 1, 0, 3) == date(2025, 1, 20)

    def test_labor_day_2025(self):
        # 1st Monday in September 2025 = Sep 1
        assert _nth_weekday(2025, 9, 0, 1) == date(2025, 9, 1)

    def test_thanksgiving_2025(self):
        # 4th Thursday in November 2025 = Nov 27
        assert _nth_weekday(2025, 11, 3, 4) == date(2025, 11, 27)


class TestUsMarketHolidays:
    """Verify the full holiday set for a given year."""

    def test_2025_has_exactly_10_holidays(self):
        holidays = us_market_holidays(2025)
        assert len(holidays) == 10

    def test_christmas_2025_in_set(self):
        holidays = us_market_holidays(2025)
        # Dec 25, 2025 is Thursday -> observed on Thursday itself
        assert date(2025, 12, 25) in holidays

    def test_july_4_2025_in_set(self):
        holidays = us_market_holidays(2025)
        # July 4, 2025 is Friday -> stays July 4
        assert date(2025, 7, 4) in holidays

    def test_good_friday_2025_in_set(self):
        holidays = us_market_holidays(2025)
        # Easter 2025 = April 20, Good Friday = April 18
        assert date(2025, 4, 18) in holidays


class TestIsTradingDay:
    """End-to-end tests for is_trading_day()."""

    def test_saturday_not_trading(self):
        trading, reason = is_trading_day(date(2025, 3, 8))
        assert trading is False
        assert "周六" in reason

    def test_sunday_not_trading(self):
        trading, reason = is_trading_day(date(2025, 3, 9))
        assert trading is False
        assert "周日" in reason

    def test_christmas_not_trading(self):
        trading, reason = is_trading_day(date(2025, 12, 25))
        assert trading is False
        assert "假日" in reason

    def test_normal_monday_is_trading(self):
        trading, reason = is_trading_day(date(2025, 3, 10))
        assert trading is True
        assert "交易日" in reason

    def test_none_argument_does_not_crash(self):
        # Calling with None should default to today and not raise
        trading, reason = is_trading_day(None)
        assert isinstance(trading, bool)
        assert isinstance(reason, str)
