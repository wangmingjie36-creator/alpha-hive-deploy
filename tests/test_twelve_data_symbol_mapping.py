"""Twelve Data 类份额代号映射 + 取数失败可观测（v0.45.363）。

2026-09-28 实测：`symbol=BRK-B` → HTTP 404，`symbol=BRK.B` 正常。映射前每条
Twelve Data 兜底对 BRK-B 都 404、只剩一行 WARNING。本文件全离线：HTTP 层由
`_TwelveDataFake` 顶替，它**照真实接口的行为**只认点写法、连字符写法回 404——
所以旧代码（原样发 `ticker`）在这里拿到的是 None，会红。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse

import pytest

import twelve_data as td

_AS_OF = "2026-08-20"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    td.reset_stats()
    td.clear_bars_cache()
    monkeypatch.setattr(td, "_limiter", None)
    monkeypatch.setattr(td, "api_key", lambda: "k")   # conftest 置空了，这里显式给假 key
    yield
    td.reset_stats()
    td.clear_bars_cache()


class _TwelveDataFake:
    """顶替 `http_gate.urlopen_gated`：记下每次请求的 symbol，按真实接口的规矩应答。"""

    KNOWN = {"BRK.B", "NVDA", "SPY"}

    def __init__(self, monkeypatch):
        import http_gate
        self.symbols: list = []
        monkeypatch.setattr(http_gate, "urlopen_gated", self)

    def __call__(self, req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        sym = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["symbol"][0]
        self.symbols.append(sym)
        if sym not in self.KNOWN:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return json.dumps({"values": [
            {"datetime": f"2026-08-{d:02d}", "close": str(500 + d), "volume": "1000000"}
            for d in range(1, 21)]}).encode()


class TestClassShareSymbolMapping:
    def test_brk_b_request_goes_out_as_dot_form(self, monkeypatch):
        http = _TwelveDataFake(monkeypatch)
        td.fetch_bars("BRK-B", 30, end_date=_AS_OF)
        assert http.symbols == ["BRK.B"], "请求里的 symbol 仍是仓内连字符写法 → 真实接口 404"

    def test_brk_b_fallback_actually_returns_bars(self, monkeypatch):
        _TwelveDataFake(monkeypatch)
        rows = td.fetch_bars("BRK-B", 30, end_date=_AS_OF)
        assert rows and rows[-1] == {"date": "2026-08-20", "close": 520.0, "vol": 1000000.0}
        assert td.fetch_daily_closes("BRK-B", days=15, end_date=_AS_OF)[-1] == 520.0

    def test_direct_fetch_rows_is_mapped_too(self, monkeypatch):
        """earnings_history 绕过缓存直接调 `_fetch_rows`——映射必须在那一层，不能只在 fetch_bars。"""
        http = _TwelveDataFake(monkeypatch)
        assert td._fetch_rows("BRK-B", 600)
        assert http.symbols == ["BRK.B"]

    def test_plain_tickers_are_untouched(self, monkeypatch):
        http = _TwelveDataFake(monkeypatch)
        td.fetch_bars("NVDA", 30, end_date=_AS_OF)
        assert http.symbols == ["NVDA"]

    @pytest.mark.parametrize("raw, api, repo", [
        ("BRK-B", "BRK.B", "BRK-B"), ("BRK.B", "BRK.B", "BRK-B"), ("brk-b", "BRK.B", "BRK-B"),
        ("BF-B", "BF.B", "BF-B"), ("NVDA", "NVDA", "NVDA"),
        # 形状之外一律不动：Twelve Data 其它资产类的标点各不相同，不能一刀切
        ("BTC-USD", "BTC-USD", "BTC-USD"), ("EUR/USD", "EUR/USD", "EUR/USD"),
    ])
    def test_mapping_table(self, raw, api, repo):
        assert td.api_symbol(raw) == api
        assert td.repo_ticker(raw) == repo

    def test_every_watchlist_ticker_maps_to_a_hyphen_free_symbol(self):
        """名单里将来再加一只带连字符、却不是「字母-单字母」形状的票，这里先红，
        逼人去实测 Twelve Data 认哪种写法，而不是再静默 404 一轮。"""
        import config
        bad = {t: td.api_symbol(t) for t in config.WATCHLIST if "-" in td.api_symbol(t)}
        assert not bad, f"这些标的发给 Twelve Data 的 symbol 仍含连字符: {bad}"


class TestCacheKeyDoesNotSplit:
    def test_both_spellings_share_one_cache_entry(self, monkeypatch):
        http = _TwelveDataFake(monkeypatch)
        a = td.fetch_bars("BRK-B", 30, end_date=_AS_OF)
        b = td.fetch_bars("BRK.B", 30, end_date=_AS_OF)
        assert a == b
        assert len(http.symbols) == 1, "两种拼法各发了一次请求——缓存键被拆开了"
        st = td.bars_cache_stats()
        assert st["entries"] == 1 and st["hits"] == 1

    def test_cache_key_uses_repo_spelling(self):
        assert td._bars_key("BRK.B", _AS_OF) == ("BRK-B", _AS_OF)


class TestFailureIsObservable:
    """「这个失败，下游怎么知道？」——不再只有一行 WARNING。"""

    def test_404_is_counted_named_and_logged_as_error(self, monkeypatch, caplog):
        http = _TwelveDataFake(monkeypatch)
        http.KNOWN = set()                      # 模拟接口不认这个 symbol
        with caplog.at_level(logging.WARNING):
            assert td.fetch_bars("BRK-B", 30, end_date=_AS_OF) is None
        st = td.bars_cache_stats()
        assert st["failures"] == 1
        assert st["failed"] == {"BRK-B": "http404"}, "失败要记在仓内写法下"
        errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errs and "BRK.B" in errs[0].getMessage(), "404 应点名发出去的 symbol"

    def test_api_error_status_is_recorded(self, monkeypatch):
        import http_gate
        monkeypatch.setattr(http_gate, "urlopen_gated", lambda *a, **k: json.dumps(
            {"status": "error", "code": 429, "message": "quota"}).encode())
        assert td.fetch_bars("NVDA", 30, end_date=_AS_OF) is None
        assert td.bars_cache_stats()["failed"] == {"NVDA": "api_error:429"}

    def test_unconfigured_key_is_not_a_failure(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "")
        assert td.fetch_bars("BRK-B", 30, end_date=_AS_OF) is None
        st = td.bars_cache_stats()
        assert st["failures"] == 0 and st["failed"] == {}

    def test_success_leaves_no_failure_record(self, monkeypatch):
        _TwelveDataFake(monkeypatch)
        assert td.fetch_bars("BRK-B", 30, end_date=_AS_OF)
        assert td.bars_cache_stats()["failures"] == 0

    def test_clear_resets_failures(self, monkeypatch):
        http = _TwelveDataFake(monkeypatch)
        http.KNOWN = set()
        td.fetch_bars("BRK-B", 30, end_date=_AS_OF)
        td.clear_bars_cache()
        st = td.bars_cache_stats()
        assert st["failures"] == 0 and st["failed"] == {}

    def test_scan_timing_summary_names_the_failed_ticker(self, monkeypatch):
        """failed 随 bars_cache_stats 整份进 status.json；摘要行要把它点出来。"""
        import scan_timing
        http = _TwelveDataFake(monkeypatch)
        http.KNOWN = set()
        td.fetch_bars("BRK-B", 30, end_date=_AS_OF)
        line = scan_timing.summary_line({"phases": {}, "counters": {
            "twelve_data": td.bars_cache_stats()}})
        assert "BRK-B:http404" in line
