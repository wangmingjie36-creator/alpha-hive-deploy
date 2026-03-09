"""
Tests for earnings_watcher.py -- EarningsWatcher class and convenience helpers.

Covers:
- get_earnings_date (cache hit, fresh fetch, yf unavailable, empty calendar)
- fetch_earnings_results (full data, minimal data, yf unavailable, empty income stmt)
- get_all_earnings_dates (batch)
- get_today_earnings (today / yesterday matching)
- get_catalysts_for_calendar (AMC / BMO time mapping)
- update_report_with_earnings (markdown injection + idempotency)
- check_and_update (full auto-check flow)
- Singleton: get_watcher / auto_check_earnings
"""

import json
import types
import threading
from datetime import datetime, date, timedelta
from pathlib import Path

import pytest

import earnings_watcher as ew
from earnings_watcher import EarningsWatcher


# ==================== Autouse fixture: isolate singleton + CACHE_DIR + mock yfinance ====================

@pytest.fixture(autouse=True)
def _reset_watcher_and_cache(tmp_path, monkeypatch):
    """Reset the module-level singleton and redirect CACHE_DIR to tmp_path."""
    # Clear singleton
    monkeypatch.setattr(ew, "_watcher", None)

    # Redirect CACHE_DIR
    cache_dir = tmp_path / "earnings_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(ew, "CACHE_DIR", cache_dir)

    # Mock yfinance_limiter so acquire() is a no-op
    fake_limiter = types.SimpleNamespace(acquire=lambda: True)
    monkeypatch.setattr(ew, "yfinance_limiter", fake_limiter)

    yield


# ==================== Helpers ====================

def _make_fake_yf(monkeypatch, calendar_data=None, income_data=None,
                  earnings_history=None, fast_info=None):
    """Install a fake yf module whose Ticker returns controlled data."""

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker
            self.calendar = calendar_data
            self.quarterly_income_stmt = income_data
            self.fast_info = fast_info or types.SimpleNamespace(pe_ratio=25.0)
            self.earnings_history = earnings_history
            self.analyst_price_targets = None

    fake_yf = types.SimpleNamespace(Ticker=FakeTicker)
    monkeypatch.setattr(ew, "yf", fake_yf)
    return fake_yf


def _make_income_df(revenue=10_000_000_000, net_income=2_000_000_000,
                    gross_profit=6_000_000_000, prev_revenue=8_000_000_000,
                    quarter_date=None):
    """Build a minimal pandas-like DataFrame stand-in for quarterly_income_stmt."""
    import pandas as pd

    if quarter_date is None:
        quarter_date = datetime(2026, 3, 1)

    # 5 columns: latest quarter + 4 previous (only col 0 and col 4 used)
    data = {}
    labels = []
    latest_values = []
    prev_values = []

    if revenue is not None:
        labels.append("Total Revenue")
        latest_values.append(revenue)
        prev_values.append(prev_revenue if prev_revenue is not None else 0)
    if net_income is not None:
        labels.append("Net Income")
        latest_values.append(net_income)
        prev_values.append(net_income * 0.8)
    if gross_profit is not None:
        labels.append("Gross Profit")
        latest_values.append(gross_profit)
        prev_values.append(gross_profit * 0.8)

    cols = [quarter_date]
    for i in range(1, 5):
        cols.append(quarter_date - timedelta(days=90 * i))

    df_data = {}
    df_data[cols[0]] = latest_values
    for i in range(1, 4):
        df_data[cols[i]] = [0] * len(labels)
    df_data[cols[4]] = prev_values

    return pd.DataFrame(df_data, index=labels)


def _make_earnings_history_df(eps_actual=1.50, eps_estimate=1.30):
    """Build a minimal pandas DataFrame for stock.earnings_history."""
    import pandas as pd
    return pd.DataFrame([{
        "epsActual": eps_actual,
        "epsEstimate": eps_estimate,
    }])


# ==================== TestGetEarningsDate ====================

class TestGetEarningsDate:
    """Tests for EarningsWatcher.get_earnings_date()."""

    def test_fresh_fetch_returns_dict(self, monkeypatch):
        """A successful yfinance call returns a well-formed dict with cached=False."""
        cal = {"Earnings Date": [datetime(2026, 4, 15)], "Revenue Avg": 10_000_000_000}
        _make_fake_yf(monkeypatch, calendar_data=cal)

        watcher = EarningsWatcher()
        result = watcher.get_earnings_date("NVDA")

        assert result is not None
        assert result["ticker"] == "NVDA"
        assert result["earnings_date"] == "2026-04-15"
        assert result["source"] == "yfinance"
        assert result["cached"] is False
        assert "fetched_at" in result

    def test_cached_hit(self, monkeypatch, tmp_path):
        """Second call within TTL returns cached=True without calling yf again."""
        cal = {"Earnings Date": [datetime(2026, 5, 1)]}
        _make_fake_yf(monkeypatch, calendar_data=cal)

        watcher = EarningsWatcher()
        first = watcher.get_earnings_date("AAPL")
        assert first is not None
        assert first["cached"] is False

        # The on-disk cache was written; next call should hit it
        second = watcher.get_earnings_date("AAPL")
        assert second is not None
        assert second["cached"] is True
        assert second["earnings_date"] == "2026-05-01"

    def test_yf_none_returns_none(self, monkeypatch):
        """When yfinance is not installed (yf is None), returns None gracefully."""
        monkeypatch.setattr(ew, "yf", None)

        watcher = EarningsWatcher()
        result = watcher.get_earnings_date("TSLA")
        assert result is None

    def test_empty_calendar_returns_none(self, monkeypatch):
        """An empty dict calendar from yfinance results in None."""
        _make_fake_yf(monkeypatch, calendar_data={})

        watcher = EarningsWatcher()
        result = watcher.get_earnings_date("XYZ")
        assert result is None

    def test_calendar_none_returns_none(self, monkeypatch):
        """calendar=None from yfinance results in None."""
        _make_fake_yf(monkeypatch, calendar_data=None)

        watcher = EarningsWatcher()
        result = watcher.get_earnings_date("XYZ")
        assert result is None


# ==================== TestFetchEarningsResults ====================

class TestFetchEarningsResults:
    """Tests for EarningsWatcher.fetch_earnings_results()."""

    def test_full_data(self, monkeypatch):
        """With complete income + EPS data, returns a 'good' completeness result."""
        income_df = _make_income_df(
            revenue=30_000_000_000,
            net_income=5_000_000_000,
            gross_profit=18_000_000_000,
            prev_revenue=24_000_000_000,
        )
        eh_df = _make_earnings_history_df(eps_actual=1.89, eps_estimate=1.60)

        _make_fake_yf(monkeypatch, income_data=income_df, earnings_history=eh_df)

        watcher = EarningsWatcher()
        result = watcher.fetch_earnings_results("NVDA")

        assert result is not None
        assert result["ticker"] == "NVDA"
        assert result["revenue_actual"] == 30_000_000_000
        assert result["eps_actual"] == 1.89
        assert result["eps_estimate"] == 1.60
        assert result["eps_beat"] is True
        assert result["gross_margin"] is not None
        assert result["yoy_revenue_growth"] == pytest.approx(0.25, abs=0.01)
        assert result["data_completeness"] == "good"
        assert result["source"] == "yfinance"

    def test_minimal_data(self, monkeypatch):
        """With only revenue (no EPS, no gross profit), completeness is 'minimal'."""
        income_df = _make_income_df(
            revenue=5_000_000_000,
            net_income=None,
            gross_profit=None,
            prev_revenue=None,
        )
        _make_fake_yf(monkeypatch, income_data=income_df, earnings_history=None)

        watcher = EarningsWatcher()
        result = watcher.fetch_earnings_results("SMCI")

        assert result is not None
        assert result["ticker"] == "SMCI"
        assert result["revenue_actual"] == 5_000_000_000
        assert result["eps_actual"] is None
        assert result["eps_beat"] is None
        # Only revenue is filled => 1 of 5 => minimal
        assert result["data_completeness"] == "minimal"

    def test_yf_none_returns_none(self, monkeypatch):
        """When yfinance is unavailable, returns None."""
        monkeypatch.setattr(ew, "yf", None)

        watcher = EarningsWatcher()
        result = watcher.fetch_earnings_results("AAPL")
        assert result is None

    def test_empty_income_stmt_returns_none(self, monkeypatch):
        """When quarterly_income_stmt is empty, returns None."""
        import pandas as pd
        empty_df = pd.DataFrame()
        _make_fake_yf(monkeypatch, income_data=empty_df)

        watcher = EarningsWatcher()
        result = watcher.fetch_earnings_results("ZZZ")
        assert result is None


# ==================== TestEarningsWatcher (integration-level) ====================

class TestEarningsWatcher:
    """Integration tests covering batch methods, report updates, and the full flow."""

    def test_get_all_earnings_dates(self, monkeypatch):
        """get_all_earnings_dates returns a dict keyed by ticker."""
        cal = {"Earnings Date": [datetime(2026, 4, 20)]}
        _make_fake_yf(monkeypatch, calendar_data=cal)

        watcher = EarningsWatcher()
        results = watcher.get_all_earnings_dates(["NVDA", "AAPL"])

        assert "NVDA" in results
        assert "AAPL" in results
        assert results["NVDA"]["earnings_date"] == "2026-04-20"

    def test_get_today_earnings(self, monkeypatch):
        """Tickers whose earnings_date matches today or yesterday are returned."""
        today_str = date.today().isoformat()
        cal = {"Earnings Date": [datetime.strptime(today_str, "%Y-%m-%d")]}
        _make_fake_yf(monkeypatch, calendar_data=cal)

        watcher = EarningsWatcher()
        reporting = watcher.get_today_earnings(["TSLA", "NVDA"])

        assert "TSLA" in reporting
        assert "NVDA" in reporting

    def test_get_catalysts_for_calendar_amc(self, monkeypatch):
        """AMC earnings get time_str 16:30."""
        cal = {"Earnings Date": [datetime(2026, 5, 10)]}
        _make_fake_yf(monkeypatch, calendar_data=cal)

        watcher = EarningsWatcher()
        catalysts = watcher.get_catalysts_for_calendar(["NVDA"])

        assert "NVDA" in catalysts
        assert len(catalysts["NVDA"]) == 1
        c = catalysts["NVDA"][0]
        assert c["scheduled_time"] == "16:30"
        assert c["time_zone"] == "US/Eastern"
        assert "Earnings Release" in c["event"]

    def test_update_report_with_earnings(self, tmp_path, monkeypatch):
        """Markdown report is updated with earnings banner + table rows."""
        report = tmp_path / "daily_report.md"
        report.write_text(
            "# Alpha Hive Daily Report\n"
            "\n---\n"
            "\n### NVDA | 看多\n"
            "| 指标 | 数值 |\n"
            "|------|------|\n"
            "| 预期 EPS | $1.60 |\n"
            "待财报验证\n"
            "\n---\n",
            encoding="utf-8",
        )

        earnings_data = {
            "ticker": "NVDA",
            "revenue_actual": 30_000_000_000,
            "eps_actual": 1.89,
            "eps_estimate": 1.60,
            "yoy_revenue_growth": 0.25,
            "gross_margin": 0.60,
            "quarter_end_date": "2026-01-31",
        }

        watcher = EarningsWatcher()
        success = watcher.update_report_with_earnings(str(report), "NVDA", earnings_data)
        assert success is True

        content = report.read_text(encoding="utf-8")
        # Banner inserted
        assert "NVDA 财报已更新" in content
        # Revenue formatted
        assert "$30.0B" in content
        # EPS actual
        assert "$1.89" in content
        # Replaced marker text
        assert "财报已验证" in content
        assert "待财报验证" not in content
        # Table rows added
        assert "实际营收" in content
        assert "实际 EPS" in content
        assert "毛利率" in content

    def test_update_report_idempotent(self, tmp_path, monkeypatch):
        """Calling update_report_with_earnings twice does not duplicate the banner."""
        report = tmp_path / "report.md"
        report.write_text(
            "# Report\n\n---\n\n### AAPL | 中性\n"
            "| Col | Val |\n|-----|-----|\n| x | y |\n\n---\n",
            encoding="utf-8",
        )

        earnings = {
            "ticker": "AAPL",
            "revenue_actual": 90_000_000_000,
            "eps_actual": 2.10,
            "eps_estimate": 2.00,
            "yoy_revenue_growth": 0.05,
            "gross_margin": 0.45,
            "quarter_end_date": "2026-01-31",
        }

        watcher = EarningsWatcher()
        watcher.update_report_with_earnings(str(report), "AAPL", earnings)
        first_content = report.read_text(encoding="utf-8")

        # Second call should be a no-op (banner already present)
        result = watcher.update_report_with_earnings(str(report), "AAPL", earnings)
        second_content = report.read_text(encoding="utf-8")

        # The banner should appear exactly once
        assert second_content.count("AAPL 财报已更新") == 1

    def test_update_report_missing_file(self, tmp_path):
        """Returns False when the report file does not exist."""
        watcher = EarningsWatcher()
        result = watcher.update_report_with_earnings(
            str(tmp_path / "nonexistent.md"), "NVDA", {"revenue_actual": 1e9}
        )
        assert result is False

    def test_check_and_update_no_earnings_today(self, monkeypatch):
        """When no ticker has earnings today, result reflects that."""
        # Set earnings date far in the future
        cal = {"Earnings Date": [datetime(2027, 12, 31)]}
        _make_fake_yf(monkeypatch, calendar_data=cal)

        watcher = EarningsWatcher()
        result = watcher.check_and_update(["NVDA", "AAPL"])

        assert result["checked"] == 2
        assert result["reporting_today"] == []
        assert result["updated"] == []

    def test_singleton_get_watcher(self, monkeypatch):
        """get_watcher returns the same instance on repeated calls."""
        w1 = ew.get_watcher()
        w2 = ew.get_watcher()
        assert w1 is w2
        assert isinstance(w1, EarningsWatcher)

    def test_auto_check_earnings_with_explicit_tickers(self, monkeypatch):
        """auto_check_earnings with explicit tickers runs without error."""
        cal = {"Earnings Date": [datetime(2027, 6, 1)]}
        _make_fake_yf(monkeypatch, calendar_data=cal)

        result = ew.auto_check_earnings(tickers=["NVDA"], report_path="/tmp/fake.md")
        assert "checked" in result
        assert result["checked"] == 1
