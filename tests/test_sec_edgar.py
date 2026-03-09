"""
Tests for sec_edgar.py -- SEC EDGAR Form 4 insider-trading data client.

Uses monkeypatch for all mocking; no real HTTP requests.
"""

import types
import xml.etree.ElementTree as ET

import pytest

# --------------- Sample Form 4 XML fixtures ---------------

SAMPLE_BUY_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-03-01</periodOfReport>
  <issuer><issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Jensen Huang</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-03-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>142.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

SAMPLE_SELL_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-03-02</periodOfReport>
  <issuer><issuerTradingSymbol>TSLA</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Robyn Denholm</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-03-02</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>50000</value></transactionShares>
        <transactionPricePerShare><value>340.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

SAMPLE_DERIVATIVE_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-02-28</periodOfReport>
  <issuer><issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Colette Kress</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>CFO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable/>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Stock Option</value></securityTitle>
      <transactionDate><value>2026-02-28</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>95.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>
"""

SAMPLE_EMPTY_TRANSACTIONS_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-03-01</periodOfReport>
  <issuer><issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Test Person</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>0</isOfficer>
    </reportingOwnerRelationship>
  </reportingOwner>
</ownershipDocument>
"""

# Preloaded CIK map for tests (avoids HTTP for ticker resolution)
_TEST_CIK_MAP = {"NVDA": "1045810", "TSLA": "1318605"}


# --------------- Fixtures ---------------


@pytest.fixture(autouse=True)
def _clear_sec_cache(monkeypatch, tmp_path):
    """Clear singleton _holder and ensure disk caches point to tmp_path."""
    import sec_edgar

    # Reset singleton holder so each test starts fresh
    sec_edgar._holder.clear()

    # Point CACHE_DIR at tmp so no real filesystem cache is read
    monkeypatch.setattr(sec_edgar, "CACHE_DIR", tmp_path / "sec_cache")
    (tmp_path / "sec_cache").mkdir(exist_ok=True)


@pytest.fixture
def _mock_sec_client(monkeypatch):
    """Return a SECEdgarClient with pre-loaded CIK map (no HTTP needed)."""
    import sec_edgar

    # Prevent __init__ from hitting SEC for company_tickers.json
    monkeypatch.setattr(
        sec_edgar.SECEdgarClient, "_load_cik_map", lambda self: None
    )
    client = sec_edgar.SECEdgarClient()
    client._cik_map = dict(_TEST_CIK_MAP)
    return client


# --------------- Helpers ---------------


def _make_response(text="", json_data=None, status_code=200, headers=None):
    """Create a mock response using types.SimpleNamespace."""
    resp = types.SimpleNamespace()
    resp.text = text
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json = lambda: json_data
    resp.raise_for_status = lambda: None
    return resp


def _submissions_json(forms, accessions, filing_dates, report_dates, primary_docs):
    """Build a mock SEC submissions JSON structure."""
    return {
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": accessions,
                "filingDate": filing_dates,
                "reportDate": report_dates,
                "primaryDocument": primary_docs,
            }
        }
    }


# ======================================================================
# TestSecEdgarCIK
# ======================================================================


class TestSecEdgarCIK:
    """Tests for ticker -> CIK mapping."""

    def test_cik_lookup(self, _mock_sec_client):
        """Known ticker returns its CIK string."""
        assert _mock_sec_client.get_cik("NVDA") == "1045810"
        assert _mock_sec_client.get_cik("TSLA") == "1318605"

    def test_cik_not_found(self, _mock_sec_client):
        """Unknown ticker returns None."""
        assert _mock_sec_client.get_cik("ZZZZZZ") is None

    def test_cik_case_insensitive(self, _mock_sec_client):
        """CIK lookup is case-insensitive (internally uppercased)."""
        assert _mock_sec_client.get_cik("nvda") == "1045810"
        assert _mock_sec_client.get_cik("Nvda") == "1045810"
        assert _mock_sec_client.get_cik("NVDA") == "1045810"


# ======================================================================
# TestSecEdgarForm4XML
# ======================================================================


class TestSecEdgarForm4XML:
    """Tests for Form 4 XML parsing."""

    def test_parse_buy_transaction(self, _mock_sec_client):
        """XML with transactionCode='P' is detected as a buy."""
        result = _mock_sec_client._parse_xml_content(SAMPLE_BUY_XML)
        assert result is not None
        assert len(result["transactions"]) == 1
        txn = result["transactions"][0]
        assert txn["code"] == "P"
        assert txn["acquired_disposed"] == "A"
        assert txn["is_derivative"] is False

    def test_parse_sell_transaction(self, _mock_sec_client):
        """XML with transactionCode='S' is detected as a sell."""
        result = _mock_sec_client._parse_xml_content(SAMPLE_SELL_XML)
        assert result is not None
        txn = result["transactions"][0]
        assert txn["code"] == "S"
        assert txn["acquired_disposed"] == "D"

    def test_parse_insider_info(self, _mock_sec_client):
        """Insider name, title, is_officer, is_director extracted correctly."""
        result = _mock_sec_client._parse_xml_content(SAMPLE_BUY_XML)
        assert result["insider_name"] == "Jensen Huang"
        assert result["insider_title"] == "CEO"
        assert result["is_officer"] is True
        assert result["is_director"] is False

        result_sell = _mock_sec_client._parse_xml_content(SAMPLE_SELL_XML)
        assert result_sell["insider_name"] == "Robyn Denholm"
        assert result_sell["is_director"] is True
        assert result_sell["is_officer"] is False

    def test_parse_transaction_values(self, _mock_sec_client):
        """Shares and price values parsed correctly as float."""
        result = _mock_sec_client._parse_xml_content(SAMPLE_BUY_XML)
        txn = result["transactions"][0]
        assert txn["shares"] == 10000.0
        assert txn["price"] == 142.50
        assert txn["security"] == "Common Stock"
        assert txn["date"] == "2026-03-01"

    def test_invalid_xml(self, _mock_sec_client):
        """Invalid XML text returns None."""
        assert _mock_sec_client._parse_xml_content("NOT VALID XML <><>") is None
        assert _mock_sec_client._parse_xml_content("") is None

    def test_empty_transactions(self, _mock_sec_client):
        """Valid XML with no transaction elements yields empty transactions list."""
        result = _mock_sec_client._parse_xml_content(SAMPLE_EMPTY_TRANSACTIONS_XML)
        assert result is not None
        assert result["transactions"] == []
        assert result["insider_name"] == "Test Person"

    def test_derivative_transaction(self, _mock_sec_client):
        """Derivative transaction parsed with is_derivative=True."""
        result = _mock_sec_client._parse_xml_content(SAMPLE_DERIVATIVE_XML)
        assert result is not None
        assert len(result["transactions"]) == 1
        txn = result["transactions"][0]
        assert txn["is_derivative"] is True
        assert txn["code"] == "M"
        assert txn["security"] == "Stock Option"
        assert txn["shares"] == 5000.0
        assert txn["price"] == 95.00


# ======================================================================
# TestSecEdgarFilings
# ======================================================================


class TestSecEdgarFilings:
    """Tests for get_recent_form4_filings()."""

    def test_get_recent_filings(self, _mock_sec_client, monkeypatch):
        """Mock submissions JSON returns correct filing list."""
        sub_data = _submissions_json(
            forms=["4", "10-Q", "4", "8-K"],
            accessions=["0001-23-000001", "0001-23-000002", "0001-23-000003", "0001-23-000004"],
            filing_dates=["2026-03-01", "2026-02-28", "2026-02-25", "2026-02-20"],
            report_dates=["2026-03-01", "2026-02-28", "2026-02-25", "2026-02-20"],
            primary_docs=["doc1.xml", "doc2.htm", "doc3.xml", "doc4.htm"],
        )
        mock_resp = _make_response(json_data=sub_data)
        monkeypatch.setattr(_mock_sec_client, "_request_get", lambda *a, **kw: mock_resp)

        filings = _mock_sec_client.get_recent_form4_filings("NVDA", limit=10)
        # Only form "4" entries should be returned
        assert len(filings) == 2
        assert filings[0]["accessionNumber"] == "0001-23-000001"
        assert filings[1]["accessionNumber"] == "0001-23-000003"
        assert filings[0]["cik"] == "1045810"

    def test_no_cik_returns_empty(self, _mock_sec_client):
        """Unknown ticker (no CIK) returns empty list."""
        result = _mock_sec_client.get_recent_form4_filings("ZZZZZZ")
        assert result == []

    def test_filings_cached(self, _mock_sec_client, monkeypatch, tmp_path):
        """Second call uses cache -- _request_get called only once."""
        import sec_edgar

        sub_data = _submissions_json(
            forms=["4"],
            accessions=["0001-23-000010"],
            filing_dates=["2026-03-01"],
            report_dates=["2026-03-01"],
            primary_docs=["doc.xml"],
        )

        call_count = {"n": 0}

        def _counting_get(*args, **kwargs):
            call_count["n"] += 1
            return _make_response(json_data=sub_data)

        monkeypatch.setattr(_mock_sec_client, "_request_get", _counting_get)

        # First call: hits _request_get
        r1 = _mock_sec_client.get_recent_form4_filings("NVDA")
        assert call_count["n"] == 1
        assert len(r1) == 1

        # Second call: should use file cache, no additional HTTP
        r2 = _mock_sec_client.get_recent_form4_filings("NVDA")
        assert call_count["n"] == 1
        assert r2 == r1

    def test_api_failure(self, _mock_sec_client, monkeypatch):
        """Network error during submissions fetch returns empty list."""
        def _failing_get(*args, **kwargs):
            raise ConnectionError("network down")

        monkeypatch.setattr(_mock_sec_client, "_request_get", _failing_get)
        result = _mock_sec_client.get_recent_form4_filings("NVDA")
        assert result == []


# ======================================================================
# TestSecEdgarInsiderSummary
# ======================================================================


class TestSecEdgarInsiderSummary:
    """Tests for get_insider_trades() high-level summary."""

    def _make_filing(self, acc="0001-23-000001", date="2026-03-01", doc="doc.xml"):
        return {
            "accessionNumber": acc,
            "filingDate": date,
            "reportDate": date,
            "primaryDocument": doc,
            "cik": "1045810",
        }

    def test_buy_heavy_bullish(self, _mock_sec_client, monkeypatch):
        """Mostly buys -> sentiment='bullish', score > 6."""
        filings = [self._make_filing(acc=f"0001-23-{i:06d}") for i in range(3)]
        monkeypatch.setattr(_mock_sec_client, "get_recent_form4_filings", lambda *a, **kw: filings)
        monkeypatch.setattr(
            _mock_sec_client, "parse_form4_xml",
            lambda *a, **kw: {
                "insider_name": "Jensen Huang",
                "insider_title": "CEO",
                "is_officer": True,
                "is_director": False,
                "transactions": [{
                    "code": "P", "shares": 20000, "price": 142.50,
                    "acquired_disposed": "A", "date": "2026-03-01",
                    "security": "Common Stock", "is_derivative": False,
                }],
            },
        )

        result = _mock_sec_client.get_insider_trades("NVDA", days=30)
        assert result["insider_sentiment"] == "bullish"
        assert result["sentiment_score"] > 6.0
        assert result["net_shares_bought"] > 0
        assert result["dollar_bought"] > 0

    def test_sell_heavy_bearish(self, _mock_sec_client, monkeypatch):
        """Mostly sells -> sentiment='bearish', score < 5."""
        filings = [self._make_filing(acc=f"0001-23-{i:06d}") for i in range(3)]
        monkeypatch.setattr(_mock_sec_client, "get_recent_form4_filings", lambda *a, **kw: filings)
        monkeypatch.setattr(
            _mock_sec_client, "parse_form4_xml",
            lambda *a, **kw: {
                "insider_name": "Robyn Denholm",
                "insider_title": "Director",
                "is_officer": False,
                "is_director": True,
                "transactions": [{
                    "code": "S", "shares": 50000, "price": 340.00,
                    "acquired_disposed": "D", "date": "2026-03-02",
                    "security": "Common Stock", "is_derivative": False,
                }],
            },
        )

        result = _mock_sec_client.get_insider_trades("TSLA", days=30)
        assert result["insider_sentiment"] == "bearish"
        assert result["sentiment_score"] < 5.0
        assert result["net_shares_sold"] > 0

    def test_no_data_neutral(self, _mock_sec_client, monkeypatch):
        """No filings -> neutral sentiment, score=5.0."""
        monkeypatch.setattr(_mock_sec_client, "get_recent_form4_filings", lambda *a, **kw: [])

        result = _mock_sec_client.get_insider_trades("NVDA", days=30)
        assert result["insider_sentiment"] == "neutral"
        assert result["sentiment_score"] == 5.0
        assert result["total_filings"] == 0

    def test_officer_buy_bonus(self, _mock_sec_client, monkeypatch):
        """Officer with code P gets a score boost and sentiment='bullish'."""
        filings = [self._make_filing()]
        monkeypatch.setattr(_mock_sec_client, "get_recent_form4_filings", lambda *a, **kw: filings)

        # A small buy that wouldn't alone trigger bullish via dollar thresholds,
        # but officer P-code should boost score and force bullish sentiment
        monkeypatch.setattr(
            _mock_sec_client, "parse_form4_xml",
            lambda *a, **kw: {
                "insider_name": "Jensen Huang",
                "insider_title": "CEO",
                "is_officer": True,
                "is_director": False,
                "transactions": [{
                    "code": "P", "shares": 100, "price": 142.50,
                    "acquired_disposed": "A", "date": "2026-03-01",
                    "security": "Common Stock", "is_derivative": False,
                }],
            },
        )

        result = _mock_sec_client.get_insider_trades("NVDA", days=30)
        # Officer buy forces bullish even with small $ amounts
        assert result["insider_sentiment"] == "bullish"
        # Score should be above the neutral baseline of 5.0
        assert result["sentiment_score"] > 5.0

    def test_score_clamped(self, _mock_sec_client, monkeypatch):
        """Sentiment score is always within [1.0, 10.0]."""
        # Create many officer buys to push score very high
        filings = [self._make_filing(acc=f"0001-23-{i:06d}") for i in range(5)]
        monkeypatch.setattr(_mock_sec_client, "get_recent_form4_filings", lambda *a, **kw: filings)
        monkeypatch.setattr(
            _mock_sec_client, "parse_form4_xml",
            lambda *a, **kw: {
                "insider_name": "Officer " + str(id(kw)),
                "insider_title": "VP",
                "is_officer": True,
                "is_director": False,
                "transactions": [{
                    "code": "P", "shares": 100000, "price": 200.0,
                    "acquired_disposed": "A", "date": "2026-03-01",
                    "security": "Common Stock", "is_derivative": False,
                }],
            },
        )

        result = _mock_sec_client.get_insider_trades("NVDA", days=30)
        assert 1.0 <= result["sentiment_score"] <= 10.0

    def test_empty_result_structure(self, _mock_sec_client):
        """_empty_result() includes all expected keys."""
        result = _mock_sec_client._empty_result("NVDA", 30)
        expected_keys = {
            "ticker", "total_filings", "period_days",
            "net_shares_bought", "net_shares_sold", "net_dollar_value",
            "dollar_bought", "dollar_sold",
            "notable_trades", "insider_sentiment", "sentiment_score", "summary",
        }
        assert expected_keys == set(result.keys())
        assert result["ticker"] == "NVDA"
        assert result["period_days"] == 30
        assert result["insider_sentiment"] == "neutral"
        assert result["sentiment_score"] == 5.0
        assert result["notable_trades"] == []

    def test_notable_trades_limit(self, _mock_sec_client, monkeypatch):
        """At most 10 notable trades are returned."""
        filings = [self._make_filing(acc=f"0001-23-{i:06d}") for i in range(15)]
        monkeypatch.setattr(_mock_sec_client, "get_recent_form4_filings", lambda *a, **kw: filings)

        call_idx = {"n": 0}

        def _varied_parse(*a, **kw):
            call_idx["n"] += 1
            return {
                "insider_name": f"Insider {call_idx['n']}",
                "insider_title": "VP",
                "is_officer": True,
                "is_director": False,
                "transactions": [{
                    "code": "P",
                    "shares": 20000,
                    "price": 100.0 + call_idx["n"],
                    "acquired_disposed": "A",
                    "date": f"2026-03-{call_idx['n']:02d}" if call_idx["n"] <= 28 else "2026-03-01",
                    "security": "Common Stock",
                    "is_derivative": False,
                }],
            }

        monkeypatch.setattr(_mock_sec_client, "parse_form4_xml", _varied_parse)

        result = _mock_sec_client.get_insider_trades("NVDA", days=30)
        assert len(result["notable_trades"]) <= 10

    def test_dedup_trades(self, _mock_sec_client, monkeypatch):
        """Duplicate trades (same insider/date/code/security) are merged."""
        filings = [
            self._make_filing(acc="0001-23-000001"),
            self._make_filing(acc="0001-23-000002"),
        ]
        monkeypatch.setattr(_mock_sec_client, "get_recent_form4_filings", lambda *a, **kw: filings)

        # Both filings return the same insider, date, code, security
        monkeypatch.setattr(
            _mock_sec_client, "parse_form4_xml",
            lambda *a, **kw: {
                "insider_name": "Jensen Huang",
                "insider_title": "CEO",
                "is_officer": True,
                "is_director": False,
                "transactions": [{
                    "code": "P", "shares": 5000, "price": 142.50,
                    "acquired_disposed": "A", "date": "2026-03-01",
                    "security": "Common Stock", "is_derivative": False,
                }],
            },
        )

        result = _mock_sec_client.get_insider_trades("NVDA", days=30)
        # Two identical trades should merge into one
        matching = [
            t for t in result["notable_trades"]
            if t["insider"] == "Jensen Huang" and t["date"] == "2026-03-01" and t["code"] == "P"
        ]
        assert len(matching) == 1
        # Shares should be summed (5000 + 5000 = 10000)
        assert matching[0]["shares"] == 10000
        # Total value should be summed
        assert matching[0]["total_value"] == pytest.approx(5000 * 142.50 + 5000 * 142.50)


# ======================================================================
# TestSecEdgarClient
# ======================================================================


class TestSecEdgarClient:
    """Tests for low-level client helpers and convenience function."""

    def test_request_get_with_breaker(self, _mock_sec_client, monkeypatch):
        """When circuit breaker is open, _request_get returns None."""
        import resilience

        # Force breaker to open state
        monkeypatch.setattr(resilience.sec_breaker, "_state", resilience.CircuitBreaker.OPEN)
        monkeypatch.setattr(resilience.sec_breaker, "_last_failure_time", 0.0)
        # Also set recovery timeout very high so it stays OPEN
        monkeypatch.setattr(resilience.sec_breaker, "_recovery_timeout", 999999.0)

        result = _mock_sec_client._request_get("https://example.com/test")
        assert result is None

    def test_rate_limiter_called(self, _mock_sec_client, monkeypatch):
        """sec_limiter.acquire() is called during _request_get."""
        import sec_edgar
        import resilience

        acquire_called = {"count": 0}

        def _tracking_acquire(*args, **kwargs):
            acquire_called["count"] += 1
            return True

        monkeypatch.setattr(resilience.sec_limiter, "acquire", _tracking_acquire)
        # Ensure breaker is closed
        monkeypatch.setattr(resilience.sec_breaker, "_state", resilience.CircuitBreaker.CLOSED)

        # Mock get_session in sec_edgar's namespace (where it was imported)
        mock_resp = _make_response(text="ok")

        def _mock_session_get(*args, **kwargs):
            return mock_resp

        mock_session = types.SimpleNamespace(get=_mock_session_get)
        monkeypatch.setattr(sec_edgar, "get_session", lambda source: mock_session)

        _mock_sec_client._request_get("https://example.com/test")
        assert acquire_called["count"] >= 1

    def test_code_desc(self, _mock_sec_client):
        """_code_desc returns correct Chinese descriptions."""
        assert _mock_sec_client._code_desc("P") == "买入"
        assert _mock_sec_client._code_desc("S") == "卖出"
        assert _mock_sec_client._code_desc("M") == "行权"
        assert _mock_sec_client._code_desc("G") == "赠与"
        assert _mock_sec_client._code_desc("F") == "税费扣股"
        assert _mock_sec_client._code_desc("A") == "授予"
        assert _mock_sec_client._code_desc("D") == "向公司处置"
        assert _mock_sec_client._code_desc("C") == "衍生品转换"
        # Unknown code returns the code itself
        assert _mock_sec_client._code_desc("X") == "X"

    def test_convenience_function(self, monkeypatch):
        """Module-level get_insider_trades() delegates to singleton client."""
        import sec_edgar

        mock_result = {
            "ticker": "NVDA",
            "insider_sentiment": "neutral",
            "sentiment_score": 5.0,
        }

        # Prevent real client creation
        monkeypatch.setattr(
            sec_edgar.SECEdgarClient, "_load_cik_map", lambda self: None
        )

        # Clear holder to force fresh singleton
        sec_edgar._holder.clear()

        # Mock get_insider_trades on the class
        monkeypatch.setattr(
            sec_edgar.SECEdgarClient, "get_insider_trades",
            lambda self, ticker, days=30: mock_result,
        )

        result = sec_edgar.get_insider_trades("NVDA", days=30)
        assert result == mock_result
        assert result["ticker"] == "NVDA"
