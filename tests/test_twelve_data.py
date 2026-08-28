"""Twelve Data 日K（v0.45.61）

补上整条链最后一处非 yfinance 不可的依赖：逐标的 30 日收盘
（`rv_30d` / `iv_rv_spread` / hv_proxy 口径 `iv_rank` 全由它派生）。
2026-08-27 yfinance 687 次 429，这三项各 0/30。

其余候选全部实打验证过不可用：Finnhub `/stock/candle` 403、
Alpha Vantage 25/天被 newsapi 占满、Stooq 上了 JS 反爬、FRED 只有指数。
"""

import json

import pytest

import twelve_data as td


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    td.reset_stats()
    monkeypatch.setattr(td, "_limiter", None)
    yield
    td.reset_stats()


def _resp(payload):
    return json.dumps(payload).encode()


_OK = {"values": [{"datetime": f"2026-08-{d:02d}", "close": str(100 + d)}
                  for d in range(1, 28)]}


def _patch_http(monkeypatch, payload_or_exc):
    import http_gate

    def _open(*a, **k):
        if isinstance(payload_or_exc, Exception):
            raise payload_or_exc
        return payload_or_exc

    monkeypatch.setattr(http_gate, "urlopen_gated", _open)


class TestUnconfigured:
    """没 key 时必须安静地让路，不报错、不阻断降级链。"""

    def test_all_entrypoints_return_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "")
        assert td.is_configured() is False
        assert td.fetch_daily_closes("NVDA") is None
        assert td.realized_vol("NVDA") is None

    def test_makes_no_request(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "")
        import http_gate

        def _boom(*a, **k):
            raise AssertionError("未配 key 却发了请求")

        monkeypatch.setattr(http_gate, "urlopen_gated", _boom)
        assert td.fetch_daily_closes("NVDA") is None


class TestErrorHandling:
    def test_http_200_with_error_status_is_not_data(self, monkeypatch):
        """免费档超额时 **HTTP 仍是 200**，错误在 body 里。

        只看状态码就会把错误当数据 —— 正是「看着成功其实早废了」的形状。
        """
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(
            {"code": 429, "message": "You have run out of API credits",
             "status": "error"}))
        assert td.fetch_daily_closes("NVDA") is None

    def test_network_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, ConnectionError("down"))
        assert td.fetch_daily_closes("NVDA") is None

    def test_non_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, b"<html>nope</html>")
        assert td.fetch_daily_closes("NVDA") is None

    def test_too_few_bars_returns_none_not_partial(self, monkeypatch):
        """数据不足要报不可得。返回半截序列会让 RV 算在错误的窗口上。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(
            {"values": [{"close": "100"}, {"close": "101"}]}))
        assert td.fetch_daily_closes("NVDA") is None

    def test_exception_is_caught_by_network_errors(self):
        """新造的异常必须继承既有网络异常族。

        v0.45.56 在 `YFRateLimited` 上踩过：不继承就会穿透所有降级路径。
        """
        from resilience import NETWORK_ERRORS
        assert issubclass(td.TwelveDataUnavailable, NETWORK_ERRORS)
        assert issubclass(td.TwelveDataUnavailable, OSError)


class TestParsing:
    def test_returns_ascending_closes(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(_OK))
        cs = td.fetch_daily_closes("NVDA")
        assert len(cs) == 27
        assert cs[0] == 101.0 and cs[-1] == 127.0

    def test_drops_bad_rows_without_zero_filling(self, monkeypatch):
        """坏行剔除，**不补 0.0** —— 0.0 会让 np.std 算出假的低波动。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        bad = {"values": [{"close": "0"}, {"close": "-5"}, {"close": None},
                          {"close": "abc"}] + _OK["values"]}
        _patch_http(monkeypatch, _resp(bad))
        cs = td.fetch_daily_closes("NVDA")
        assert all(c > 0 for c in cs)
        assert len(cs) == 27

    def test_counts_credits(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(_OK))
        td.fetch_daily_closes("NVDA")
        td.fetch_daily_closes("TSLA")
        assert td.stats()["daily_used"] == 2


class TestRealizedVol:
    def test_matches_upstream_formula(self, monkeypatch):
        """口径必须与 `calculate_iv_rv_spread` 一致：对数收益 / ddof=1 / ×√252×100。

        口径不一致的话，同一只标的会因为走了哪条源而得到不同的 RV，
        那种差异会被误读成波动率变化。
        """
        import math
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(_OK))
        rv = td.realized_vol("NVDA", lookback=26)

        closes = [101.0 + i for i in range(27)]
        rets = [math.log(b / a) for a, b in zip(closes[:-1], closes[1:])]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        expect = math.sqrt(var) * math.sqrt(252) * 100
        assert rv == pytest.approx(expect, rel=1e-6)

    def test_rejects_absurd_values(self, monkeypatch):
        """HV30 > 300% 视为数据污染 —— 与上游同一条 sanity check。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        wild = {"values": [{"close": str(100 * (3 ** i))} for i in range(30)]}
        _patch_http(monkeypatch, _resp(wild))
        assert td.realized_vol("NVDA") is None

    def test_no_data_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "")
        assert td.realized_vol("NVDA") is None


class TestIntegrationWithMarketIntelligence:
    def test_prefers_twelve_data_when_configured(self, monkeypatch):
        import market_intelligence as mi
        monkeypatch.setattr(td, "api_key", lambda: "k")
        monkeypatch.setattr(td, "realized_vol", lambda *a, **k: 36.14)

        import yfinance as yf

        def _boom(*a, **k):
            raise AssertionError("Twelve Data 可用时不该打 yfinance")

        monkeypatch.setattr(yf, "download", _boom)
        d = mi.calculate_iv_rv_spread("NVDA", 48.61)
        assert d["rv_30d"] == 36.14
        assert d["source"] == "twelve_data"
        assert d["iv_rv_signal"] == "expensive"     # 48.61-36.14 = +12.47 > 10

    def test_falls_back_to_yfinance_when_unavailable(self, monkeypatch):
        """Twelve Data 返回 None 时必须原样落回 yfinance，不能把整项打空。"""
        import market_intelligence as mi
        monkeypatch.setattr(td, "api_key", lambda: "k")
        monkeypatch.setattr(td, "realized_vol", lambda *a, **k: None)

        import yfinance as yf
        calls = {"n": 0}

        def _dl(*a, **k):
            calls["n"] += 1
            raise ConnectionError("no net")

        monkeypatch.setattr(yf, "download", _dl)
        d = mi.calculate_iv_rv_spread("NVDA", 48.61)
        assert calls["n"] >= 1, "未回落到 yfinance"
        assert d["rv_30d"] is None and d["data_available"] is False
