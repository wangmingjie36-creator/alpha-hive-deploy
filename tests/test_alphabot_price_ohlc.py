"""价格页 K 线的开 / 高 / 低（v0.45.762）：`alphabot/price_ohlc.py` 与 `AlphaBotService._with_ohlc`。

全部离线：yfinance 用假模块注入，服务用注入的 `bars_fn` / `ohlc_fn`。逐条都配「违反它就会红」的输入。
"""
from __future__ import annotations

import inspect

import pandas as pd
import pytest

from alphabot import price_ohlc as PO
from alphabot.service import AlphaBotService

BARS = [{"date": "2026-11-05", "close": 100.0, "volume": 10},
        {"date": "2026-11-06", "close": 102.0, "volume": 11},
        {"date": "2026-11-09", "close": 101.0, "volume": 12}]
OHLC = {"2026-11-05": {"open": 99.0, "high": 100.5, "low": 98.5, "close": 100.0},
        "2026-11-06": {"open": 100.2, "high": 103.0, "low": 100.0, "close": 102.01},
        "2026-11-09": {"open": 102.5, "high": 103.0, "low": 100.5, "close": 101.0}}


class TestMerge:
    def test_matched_rows_get_open_high_low_and_keep_spine(self):
        out, n = PO.merge(BARS, OHLC)
        assert n == {"matched": 3, "missing": 0, "close_mismatch": 0}
        assert [(r["date"], r["close"], r["volume"]) for r in out] == [(r["date"], r["close"], r["volume"]) for r in BARS]
        assert out[2]["open"] == 102.5 and out[2]["high"] == 103.0 and out[2]["low"] == 100.5
        assert all("open" not in r for r in BARS), "不许改传入的行"

    def test_close_mismatch_is_not_merged(self):
        bad = {**OHLC, "2026-11-06": {**OHLC["2026-11-06"], "close": 102.0 * (1 + PO.CLOSE_TOL * 2)}}
        out, n = PO.merge(BARS, bad)
        assert n == {"matched": 2, "missing": 0, "close_mismatch": 1}
        assert "open" not in out[1] and "high" not in out[1] and "low" not in out[1]

    def test_missing_day_and_extra_day(self):
        part = {k: v for k, v in OHLC.items() if k != "2026-11-05"}
        part["2026-11-10"] = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}     # 主干没有的日子不许冒出来
        out, n = PO.merge(BARS, part)
        assert n["missing"] == 1 and n["matched"] == 2 and len(out) == 3
        assert [r["date"] for r in out] == [r["date"] for r in BARS]

    def test_wick_always_contains_spine_close(self):
        # 容差内的小差异：yfinance 的最高比主干收盘还低一点 ⇒ 影线要把收盘包进去
        odd = {"2026-11-05": {"open": 99.0, "high": 99.9, "low": 98.5, "close": 99.9}}
        out, _ = PO.merge(BARS[:1], odd)
        assert out[0]["high"] >= out[0]["close"] >= out[0]["low"] and out[0]["high"] == 100.0


class TestRowsFromHistory:
    def _frame(self, rows):
        return pd.DataFrame(rows, index=pd.to_datetime([r.pop("d") for r in rows]))

    def test_reads_ohlc_and_skips_bad_rows(self):
        hist = self._frame([
            {"d": "2026-11-05", "Open": 99.0, "High": 100.5, "Low": 98.5, "Close": 100.0},
            {"d": "2026-11-06", "Open": float("nan"), "High": 103.0, "Low": 100.0, "Close": 102.0},   # 缺开盘
            {"d": "2026-11-09", "Open": 102.5, "High": 100.0, "Low": 103.0, "Close": 101.0},          # 高低颠倒
            {"d": "2026-11-10", "Open": 0.0, "High": 1.0, "Low": 0.5, "Close": 0.8}])                 # 非正
        assert PO.rows_from_history(hist) == {"2026-11-05": {"open": 99.0, "high": 100.5, "low": 98.5, "close": 100.0}}

    def test_missing_column_gives_nothing(self):
        assert PO.rows_from_history(self._frame([{"d": "2026-11-05", "Close": 1.0}])) == {}
        assert PO.rows_from_history(None) == {}


class _FakeYF:
    def __init__(self, hist=None, exc=None):
        self.hist, self.exc, self.calls = hist, exc, []

    def Ticker(self, t):  # noqa: N802 - yfinance 的写法
        outer = self

        class _T:
            ticker = t

            def history(self, **kw):
                outer.calls.append((t, kw))
                if outer.exc:
                    raise outer.exc
                return outer.hist
        return _T()


class TestFetch:
    def test_unadjusted_request_and_registered_source(self):
        import yf_daily
        hist = pd.DataFrame({"Open": [99.0], "High": [100.5], "Low": [98.5], "Close": [100.0], "Volume": [1],
                             "Dividends": [0.0], "Stock Splits": [0.0]}, index=pd.to_datetime(["2026-11-05"]))
        fake = _FakeYF(hist)
        rows, info = PO.fetch_ohlc("NVDA", yf_module=fake)
        assert info == {"n": 1, "reason": None} and rows["2026-11-05"]["open"] == 99.0
        # 主干（Twelve Data）不做分红复权 ⇒ 这里必须是未做分红复权的那套价，否则除息日前的收盘全对不上
        assert fake.calls == [("NVDA", {"period": PO.PERIOD, "auto_adjust": False})]
        assert "alphabot_price_ohlc" in yf_daily.SOURCES
        assert 'source="alphabot_price_ohlc"' in inspect.getsource(PO)

    def test_failure_carries_reason_and_never_raises(self):
        rows, info = PO.fetch_ohlc("NVDA", yf_module=_FakeYF(exc=RuntimeError("boom")))
        assert rows == {} and info == {"n": 0, "reason": "exception:RuntimeError"}

    def test_index_tickers_are_not_guessed(self):
        fake = _FakeYF()
        assert PO.fetch_ohlc("_SPX", yf_module=fake) == ({}, {"n": 0, "reason": "index_not_supported"})
        assert fake.calls == []


def _svc(tmp_path, **kw):
    kw.setdefault("bars_fn", lambda t: {"available": True, "source": "twelve_data", "bars": [dict(r) for r in BARS]})
    return AlphaBotService(state_dir=tmp_path, **kw)


class TestServiceWiring:
    def test_bars_get_ohlc_and_say_where_it_came_from(self, tmp_path):
        s = _svc(tmp_path, ohlc_fn=lambda t: (OHLC, {"n": 3, "reason": None}))
        res = s.bars("NVDA")
        assert res["available"] and res["source"] == "twelve_data"
        assert res["ohlc"]["available"] and res["ohlc"]["source"] == "yfinance" and res["ohlc"]["matched"] == 3
        assert all({"open", "high", "low", "close", "volume", "date"} <= set(r) for r in res["bars"])
        assert s.stats["ohlc_failures"] == 0

    def test_ohlc_failure_keeps_bars_and_is_visible(self, tmp_path):
        calls = []

        def fail(t):
            calls.append(t)
            return {}, {"n": 0, "reason": "rate_limited", "retry_after_sec": 120.0}
        s = _svc(tmp_path, ohlc_fn=fail)
        res = s.bars("NVDA")
        assert res["available"] and len(res["bars"]) == 3 and all("open" not in r for r in res["bars"])
        assert res["ohlc"] == {"available": False, "source": "yfinance", "n": 0, "reason": "rate_limited", "retry_after_sec": 120.0}
        assert s.stats["ohlc_failures"] == 1
        s.bars("NVDA")
        assert len(calls) == 2, "失败不入缓存：下次打开要再试"

    def test_success_is_cached_for_the_day(self, tmp_path):
        calls = []

        def ok(t):
            calls.append(t)
            return OHLC, {"n": 3, "reason": None}
        s = _svc(tmp_path, ohlc_fn=ok)
        s.bars("NVDA"), s.bars("NVDA")
        assert calls == ["NVDA"]

    def test_raising_ohlc_fn_does_not_break_bars(self, tmp_path):
        def boom(t):
            raise ValueError("x")
        res = _svc(tmp_path, ohlc_fn=boom).bars("NVDA")
        assert res["available"] and res["ohlc"]["reason"] == "exception:ValueError"

    def test_nothing_matches_is_a_failure_not_a_silent_fallback(self, tmp_path):
        far = {d: {**v, "close": v["close"] * 2} for d, v in OHLC.items()}
        s = _svc(tmp_path, ohlc_fn=lambda t: (far, {"n": 3, "reason": None}))
        res = s.bars("NVDA")
        assert res["ohlc"]["available"] is False and res["ohlc"]["reason"] == "no_bar_matched" and res["ohlc"]["close_mismatch"] == 3
        assert s.stats["ohlc_failures"] == 1

    def test_injected_bars_without_ohlc_fn_never_reaches_the_network(self, tmp_path, monkeypatch):
        # 注入了日线而没注入开高低（现有测试都是这样）⇒ 不许去调缺省的 yfinance 取数
        monkeypatch.setattr(PO, "fetch_ohlc", lambda *a, **k: pytest.fail("不该出网"))
        res = _svc(tmp_path).bars("NVDA")
        assert res["ohlc"] == {"available": False, "reason": "not_wired"}

    def test_demo_bars_carry_their_own_ohlc(self):
        from alphabot import synthetic as syn
        rows = syn.demo_bars("NVDA", "2026-11-10")
        assert all(r["low"] <= min(r["open"], r["close"]) <= max(r["open"], r["close"]) <= r["high"] for r in rows)
