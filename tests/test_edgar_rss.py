"""
Tests for edgar_rss module -- SEC EDGAR Form 4 Atom RSS real-time alerts
"""

import types
import time
import json
import pytest


# ==================== Sample Atom XML ====================

SAMPLE_ATOM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>SEC Filing</title>
  <entry>
    <title>4 - NVIDIA CORP (1045810) (9999999)</title>
    <link href="https://www.sec.gov/Archives/edgar/data/1045810/000104581025000001.htm"/>
    <updated>2026-03-08T10:30:00-05:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0001045810-25-000001</id>
  </entry>
  <entry>
    <title>4 - APPLE INC (320193) (1234567)</title>
    <link href="https://www.sec.gov/Archives/edgar/data/320193/000032019325000002.htm"/>
    <updated>2026-03-08T11:00:00-05:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-25-000002</id>
  </entry>
  <entry>
    <title>4 - TESLA INC (1318605) (7777777)</title>
    <link href="https://www.sec.gov/Archives/edgar/data/1318605/000131860525000003.htm"/>
    <updated>2026-03-07T09:00:00-05:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0001318605-25-000003</id>
  </entry>
</feed>
"""

EMPTY_ATOM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>SEC Filing</title>
</feed>
"""

MALFORMED_XML = "<not valid xml!!! <><>"


# ==================== autouse fixture ====================

@pytest.fixture(autouse=True)
def _clear_edgar_rss(tmp_path, monkeypatch):
    """Clear singleton client, reset global health counters, and redirect
    disk cache to tmp_path before each test."""
    import edgar_rss

    # Redirect disk cache path to tmp_path
    cache_path = tmp_path / "sec_cache" / "edgar_rss.json"
    monkeypatch.setattr(edgar_rss, "_CACHE_PATH", cache_path)

    # Reset singleton
    edgar_rss._client = None

    # Reset health tracking globals
    edgar_rss._rss_fail_count = 0
    edgar_rss._rss_degraded = False

    yield

    # Teardown: reset again
    edgar_rss._client = None
    edgar_rss._rss_fail_count = 0
    edgar_rss._rss_degraded = False


# ==================== helpers ====================

def _make_rss_response(xml_text, status_code=200, ok=True):
    """Build a SimpleNamespace mimicking requests.Response for RSS feed."""
    resp = types.SimpleNamespace()
    resp.status_code = status_code
    resp.ok = ok
    resp.text = xml_text
    return resp


def _mock_session(monkeypatch, response):
    """Patch get_session to return a fake session whose .get() returns *response*.
    Returns a call_count dict so tests can inspect how many HTTP calls were made."""
    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        return response

    session = types.SimpleNamespace(get=fake_get)
    monkeypatch.setattr("edgar_rss.get_session", lambda source: session)
    return call_count


def _mock_session_raising(monkeypatch, exc):
    """Patch get_session to return a session whose .get() always raises *exc*."""
    def failing_get(url, **kwargs):
        raise exc

    session = types.SimpleNamespace(get=failing_get)
    monkeypatch.setattr("edgar_rss.get_session", lambda source: session)


# ==================== TestParseAtom ====================

class TestParseAtom:
    """Tests for EdgarRSSClient._parse_atom XML parsing."""

    def test_valid_atom_parses_entries(self):
        """Valid Atom XML with 3 entries should produce 3 parsed dicts."""
        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        entries = client._parse_atom(SAMPLE_ATOM_XML)

        assert len(entries) == 3

    def test_parsed_fields_correct(self):
        """Check that all fields are extracted correctly from the first entry."""
        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        entries = client._parse_atom(SAMPLE_ATOM_XML)
        first = entries[0]

        assert first["company_name"] == "NVIDIA CORP"
        assert first["cik"] == "1045810"
        assert first["title"] == "4 - NVIDIA CORP (1045810) (9999999)"
        assert first["filing_date"] == "2026-03-08"
        assert first["updated_ts"] == "2026-03-08T10:30:00-05:00"
        assert "000104581025000001" in first["feed_url"]
        assert first["accession_number"] == "0001045810-25-000001"

    def test_empty_feed_returns_empty_list(self):
        """An Atom feed with no <entry> elements returns an empty list."""
        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        entries = client._parse_atom(EMPTY_ATOM_XML)

        assert entries == []

    def test_malformed_xml_returns_empty_list(self):
        """Unparseable XML should return empty list (not raise)."""
        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        entries = client._parse_atom(MALFORMED_XML)

        assert entries == []

    def test_entry_missing_elements_skips_gracefully(self):
        """An entry with missing sub-elements should still produce a dict
        with empty-string defaults rather than crashing."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>4 - MYSTERY CORP (5555555) (6666666)</title>
  </entry>
</feed>
"""
        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        entries = client._parse_atom(xml)

        assert len(entries) == 1
        e = entries[0]
        assert e["company_name"] == "MYSTERY CORP"
        assert e["cik"] == "5555555"
        # Missing updated/link/id should yield empty strings
        assert e["filing_date"] == ""
        assert e["feed_url"] == ""
        assert e["accession_number"] == ""


# ==================== TestGetRecentForm4Alerts ====================

class TestGetRecentForm4Alerts:
    """Tests for get_recent_form4_alerts (HTTP fetch, caching, health tracking)."""

    def test_successful_fetch(self, monkeypatch):
        """A successful HTTP response should return parsed entries."""
        resp = _make_rss_response(SAMPLE_ATOM_XML)
        _mock_session(monkeypatch, resp)

        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        entries = client.get_recent_form4_alerts()

        assert len(entries) == 3
        assert entries[0]["company_name"] == "NVIDIA CORP"

    def test_memory_cache_hit(self, monkeypatch):
        """Second call within TTL should use memory cache (no extra HTTP call)."""
        resp = _make_rss_response(SAMPLE_ATOM_XML)
        call_count = _mock_session(monkeypatch, resp)

        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        client.get_recent_form4_alerts()
        assert call_count["n"] == 1

        # Second call -- should hit memory cache
        client.get_recent_form4_alerts()
        assert call_count["n"] == 1  # no additional HTTP call

    def test_force_refresh_bypasses_cache(self, monkeypatch):
        """force_refresh=True should make a new HTTP call even if cached."""
        resp = _make_rss_response(SAMPLE_ATOM_XML)
        call_count = _mock_session(monkeypatch, resp)

        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        client.get_recent_form4_alerts()
        assert call_count["n"] == 1

        client.get_recent_form4_alerts(force_refresh=True)
        assert call_count["n"] == 2

    def test_http_error_returns_stale_cache(self, monkeypatch):
        """Non-ok HTTP response should return existing (possibly empty) cache."""
        resp = _make_rss_response("", status_code=503, ok=False)
        _mock_session(monkeypatch, resp)

        from edgar_rss import EdgarRSSClient
        client = EdgarRSSClient()
        entries = client.get_recent_form4_alerts(force_refresh=True)

        assert entries == []

    def test_network_error_increments_fail_count(self, monkeypatch):
        """Network errors should increment _rss_fail_count."""
        import edgar_rss

        _mock_session_raising(monkeypatch, ConnectionError("mock timeout"))

        client = edgar_rss.EdgarRSSClient()
        client.get_recent_form4_alerts(force_refresh=True)

        assert edgar_rss._rss_fail_count == 1
        assert not edgar_rss._rss_degraded

    def test_degraded_mode_after_threshold(self, monkeypatch):
        """After _RSS_FAIL_THRESHOLD consecutive failures, _rss_degraded should be True."""
        import edgar_rss

        # Suppress the Slack alert call
        monkeypatch.setattr(edgar_rss, "_try_rss_slack_alert", lambda fc: None)

        _mock_session_raising(monkeypatch, ConnectionError("down"))

        client = edgar_rss.EdgarRSSClient()
        for _ in range(edgar_rss._RSS_FAIL_THRESHOLD):
            client.get_recent_form4_alerts(force_refresh=True)

        assert edgar_rss._rss_fail_count == edgar_rss._RSS_FAIL_THRESHOLD
        assert edgar_rss._rss_degraded is True

    def test_success_resets_fail_count(self, monkeypatch):
        """A successful fetch after failures should reset _rss_fail_count to 0."""
        import edgar_rss

        # First: simulate some failures
        edgar_rss._rss_fail_count = 5
        edgar_rss._rss_degraded = True

        resp = _make_rss_response(SAMPLE_ATOM_XML)
        _mock_session(monkeypatch, resp)

        client = edgar_rss.EdgarRSSClient()
        client.get_recent_form4_alerts(force_refresh=True)

        assert edgar_rss._rss_fail_count == 0
        assert edgar_rss._rss_degraded is False

    def test_disk_cache_used_when_fresh(self, tmp_path, monkeypatch):
        """If a fresh disk cache file exists, it should be loaded without HTTP."""
        import edgar_rss

        # Write a pre-populated disk cache
        cache_dir = tmp_path / "sec_cache"
        cache_dir.mkdir(exist_ok=True)
        cache_file = cache_dir / "edgar_rss.json"
        cached_data = [{"company_name": "CACHED CORP", "cik": "111111",
                         "title": "4 - CACHED CORP", "filing_date": "2026-03-08",
                         "updated_ts": "", "feed_url": "", "accession_number": ""}]
        cache_file.write_text(json.dumps(cached_data))

        monkeypatch.setattr(edgar_rss, "_CACHE_PATH", cache_file)

        # Mock session that should NOT be called
        call_count = {"n": 0}

        def fail_get(url, **kwargs):
            call_count["n"] += 1
            raise AssertionError("Should not make HTTP call when disk cache is fresh")

        session = types.SimpleNamespace(get=fail_get)
        monkeypatch.setattr("edgar_rss.get_session", lambda source: session)

        client = edgar_rss.EdgarRSSClient()
        entries = client.get_recent_form4_alerts()

        assert len(entries) == 1
        assert entries[0]["company_name"] == "CACHED CORP"
        assert call_count["n"] == 0


# ==================== TestEdgarRSSClient ====================

class TestEdgarRSSClient:
    """Tests for filtering methods, summarize, singleton, and convenience function."""

    def test_get_today_filings_for_cik(self, monkeypatch):
        """Should return only entries matching today's date and the given CIK."""
        resp = _make_rss_response(SAMPLE_ATOM_XML)
        _mock_session(monkeypatch, resp)

        # Mock datetime.now() to return 2026-03-08 so "today" matches our XML
        import edgar_rss
        monkeypatch.setattr(
            edgar_rss, "datetime",
            types.SimpleNamespace(now=lambda: types.SimpleNamespace(
                strftime=lambda fmt: "2026-03-08"
            )),
        )

        client = edgar_rss.EdgarRSSClient()
        results = client.get_today_filings_for_cik("1045810")

        assert len(results) == 1
        assert results[0]["company_name"] == "NVIDIA CORP"

    def test_get_today_filings_for_cik_no_match(self, monkeypatch):
        """CIK that doesn't exist in feed should return empty list."""
        resp = _make_rss_response(SAMPLE_ATOM_XML)
        _mock_session(monkeypatch, resp)

        import edgar_rss
        monkeypatch.setattr(
            edgar_rss, "datetime",
            types.SimpleNamespace(now=lambda: types.SimpleNamespace(
                strftime=lambda fmt: "2026-03-08"
            )),
        )

        client = edgar_rss.EdgarRSSClient()
        results = client.get_today_filings_for_cik("0000000")

        assert results == []

    def test_get_today_filings_by_name(self, monkeypatch):
        """Should match entries by company name (case-insensitive substring)."""
        resp = _make_rss_response(SAMPLE_ATOM_XML)
        _mock_session(monkeypatch, resp)

        import edgar_rss
        monkeypatch.setattr(
            edgar_rss, "datetime",
            types.SimpleNamespace(now=lambda: types.SimpleNamespace(
                strftime=lambda fmt: "2026-03-08"
            )),
        )

        client = edgar_rss.EdgarRSSClient()
        results = client.get_today_filings_by_name("nvidia")

        assert len(results) == 1
        assert results[0]["cik"] == "1045810"

    def test_summarize_ticker_alerts_with_cik(self, monkeypatch):
        """summarize_ticker_alerts with CIK should return structured summary."""
        resp = _make_rss_response(SAMPLE_ATOM_XML)
        _mock_session(monkeypatch, resp)

        import edgar_rss
        monkeypatch.setattr(
            edgar_rss, "datetime",
            types.SimpleNamespace(now=lambda: types.SimpleNamespace(
                strftime=lambda fmt: "2026-03-08"
            )),
        )

        client = edgar_rss.EdgarRSSClient()
        summary = client.summarize_ticker_alerts("NVDA", cik="1045810")

        assert summary["ticker"] == "NVDA"
        assert summary["fresh_filings_count"] == 1
        assert summary["has_fresh_filings"] is True
        assert len(summary["filings"]) == 1
        assert "1" in summary["summary"]  # mentions count

    def test_summarize_ticker_no_filings(self, monkeypatch):
        """When no filings match, summary should indicate zero filings."""
        resp = _make_rss_response(EMPTY_ATOM_XML)
        _mock_session(monkeypatch, resp)

        import edgar_rss
        monkeypatch.setattr(
            edgar_rss, "datetime",
            types.SimpleNamespace(now=lambda: types.SimpleNamespace(
                strftime=lambda fmt: "2026-03-08"
            )),
        )

        client = edgar_rss.EdgarRSSClient()
        summary = client.summarize_ticker_alerts("ZZZZ")

        assert summary["ticker"] == "ZZZZ"
        assert summary["fresh_filings_count"] == 0
        assert summary["has_fresh_filings"] is False
        assert summary["filings"] == []

    def test_singleton_get_rss_client(self, monkeypatch):
        """get_rss_client() should return the same instance on repeated calls."""
        import edgar_rss

        c1 = edgar_rss.get_rss_client()
        c2 = edgar_rss.get_rss_client()

        assert c1 is c2

    def test_convenience_get_today_form4_alerts(self, monkeypatch):
        """Convenience function get_today_form4_alerts should delegate to singleton."""
        resp = _make_rss_response(SAMPLE_ATOM_XML)
        _mock_session(monkeypatch, resp)

        import edgar_rss
        monkeypatch.setattr(
            edgar_rss, "datetime",
            types.SimpleNamespace(now=lambda: types.SimpleNamespace(
                strftime=lambda fmt: "2026-03-08"
            )),
        )

        result = edgar_rss.get_today_form4_alerts("NVDA", cik="1045810")

        assert result["ticker"] == "NVDA"
        assert result["has_fresh_filings"] is True

    def test_requests_none_returns_empty(self, monkeypatch):
        """When requests module is None, get_recent_form4_alerts returns empty cache."""
        import edgar_rss
        monkeypatch.setattr(edgar_rss, "_req", None)

        client = edgar_rss.EdgarRSSClient()
        entries = client.get_recent_form4_alerts(force_refresh=True)

        assert entries == []
