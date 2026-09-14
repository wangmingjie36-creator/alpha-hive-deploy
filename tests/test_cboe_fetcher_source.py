"""cboe_fetcher 数据来源标注守卫（v0.45.29）。

守什么：
1. VIX 期限结构的 vix_1m/vix_3m 必须来自真实 VX 期货（vixcentral M1/M3），
   **不再**是 VIXY ETF 股价 × 0.5 / spot × 1.10 的合成口径。
2. 任何兜底路径产出的 dict 必须带 source='default_fallback'——
   兜底常量冒充观测值（v0.43.24 同款）是本项目反复出现的静默降级形态，
   本文件的测试全部按「喂退化数据看它红」构造。
3. v0.45.29 之前的旧缓存（无 source 键）必须被视为过期重抓，
   否则 VIXY 垃圾口径会经由当日缓存再活一天。
4. v0.45.241：P/C / SKEW / VVIX **CBOE 优先**、yfinance 其次、兜底最后。CBOE 拿到时
   **不许碰 yfinance**（云端够不到它，先走它就是 12/12 天兜底的原样）。

⚠️ 本文件 autouse 把两个 CBOE 源钉成「取不到」（契约：`_download_cboe_index_csv` / `_fetch_cboe_payload`
失败都返回 None），于是既有的 yfinance / 兜底用例照旧确定、离线。要 CBOE 真行为的用例在函数体里再 setattr。
"""

import sys
import os
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cboe_fetcher import CBOEDailyFetcher  # noqa: E402


FAKE_VTS = {
    "spot_vix": 15.70,
    "futures": [16.20, 17.05, 17.80, 18.30],  # M1~M4 真期货
    "structure": "contango",
}


@pytest.fixture
def fetcher(tmp_path):
    return CBOEDailyFetcher(cache_dir=str(tmp_path / "cboe_daily"))


@pytest.fixture(autouse=True)
def _cboe_sources_unavailable(monkeypatch, stub_cboe_payload):
    """两个 CBOE 源默认「取不到」—— 见模块 docstring 末段。"""
    import cboe_fetcher as cf
    monkeypatch.setattr(cf, "_download_cboe_index_csv", lambda symbol: None)


def _patch_vts(monkeypatch, fn):
    import vix_term_structure
    monkeypatch.setattr(vix_term_structure, "get_vix_term_structure", fn)


class TestVixTermRealFutures:
    def test_1m_3m_come_from_vx_futures_not_vixy(self, fetcher, monkeypatch):
        _patch_vts(monkeypatch, lambda: dict(FAKE_VTS))
        r = fetcher.fetch_vix_term_structure()
        assert r["source"] == "vx_futures"
        assert r["vix_spot"] == 15.70
        assert r["vix_1m"] == 16.20   # = M1，而非 VIXY 股价 × 0.5
        assert r["vix_3m"] == 17.80   # = M3，而非 spot × 1.10
        assert r["term_structure"] == "contango"
        assert r["contango_pct"] == pytest.approx((16.20 - 15.70) / 15.70 * 100, abs=0.01)

    def test_no_synthetic_3m(self, fetcher, monkeypatch):
        """旧口径 vix_3m = spot × 1.10；若回归，此断言必红。"""
        _patch_vts(monkeypatch, lambda: dict(FAKE_VTS))
        r = fetcher.fetch_vix_term_structure()
        assert r["vix_3m"] != pytest.approx(r["vix_spot"] * 1.10, abs=0.01)

    def test_futures_unavailable_falls_back_labeled(self, fetcher, monkeypatch):
        """喂退化：期货拿不到 → 必须落 default_fallback 且带标注，不得合成。"""
        _patch_vts(monkeypatch, lambda: {"spot_vix": 15.70, "futures": [], "structure": "unknown"})
        import cboe_vix
        monkeypatch.setattr(cboe_vix, "get_vix_spot", lambda: None)
        r = fetcher.fetch_vix_term_structure()
        assert r["source"] == "default_fallback"
        assert (r["vix_spot"], r["vix_1m"], r["vix_3m"]) == (15.0, 15.75, 16.5)

    def test_vts_exception_falls_back_labeled(self, fetcher, monkeypatch):
        def _boom():
            raise RuntimeError("vixcentral down")
        _patch_vts(monkeypatch, _boom)
        r = fetcher.fetch_vix_term_structure()
        assert r["source"] == "default_fallback"

    def test_legacy_cache_without_source_is_refetched(self, fetcher, monkeypatch):
        """v0.45.29 前的旧缓存（无 source）可能是 VIXY 垃圾口径，必须作废。"""
        fetcher._write_cache("vix_term", {"vix_spot": 15.70, "vix_1m": 9.005,
                                         "vix_3m": 17.27, "term_structure": "backwardation",
                                         "contango_pct": -42.64})
        _patch_vts(monkeypatch, lambda: dict(FAKE_VTS))
        r = fetcher.fetch_vix_term_structure()
        assert r["source"] == "vx_futures"
        assert r["vix_1m"] == 16.20  # 重抓的真值，不是缓存里的 VIXY 股价

    def test_labeled_cache_is_reused(self, fetcher):
        fetcher._write_cache("vix_term", {"vix_spot": 14.0, "vix_1m": 14.5, "vix_3m": 15.0,
                                         "term_structure": "contango", "contango_pct": 3.57,
                                         "source": "vx_futures"})
        r = fetcher.fetch_vix_term_structure()
        assert r["vix_spot"] == 14.0 and r["source"] == "vx_futures"


class TestSkewVvixSourceLabel:
    def test_skew_fallback_labeled(self, fetcher, monkeypatch):
        """喂退化：yfinance 不可用 → skew 兜底必须带 default_fallback。"""
        import cboe_fetcher as cf
        monkeypatch.setattr(cf, "yf", None)
        r = fetcher.fetch_skew_index()
        assert r["skew_value"] == 120.0
        assert r["source"] == "default_fallback"

    def test_vvix_fallback_labeled(self, fetcher, monkeypatch):
        import cboe_fetcher as cf
        monkeypatch.setattr(cf, "yf", None)
        r = fetcher.fetch_vvix()
        assert r["vvix_value"] == 85.0
        assert r["source"] == "default_fallback"

    def test_skew_success_labeled_yfinance(self, fetcher, monkeypatch):
        import cboe_fetcher as cf
        import pandas as pd
        fake = types.SimpleNamespace(
            download=lambda *a, **k: pd.DataFrame({"Close": [143.27]}))
        monkeypatch.setattr(cf, "yf", fake)
        r = fetcher.fetch_skew_index()
        assert r["skew_value"] == pytest.approx(143.27)
        assert r["source"] == "yfinance"
        assert r["signal"] == "elevated"


class TestDegradationCheckSourceFirst:
    def test_source_field_wins_over_values(self):
        from cloud_snapshot_fetch import _degradation_check
        # source 明示降级但数值不是已知常量（比如兜底值以后改了）——仍要命中
        c = {"vix_term": {"vix_spot": 22.2, "source": "default_fallback"},
             "skew": {"skew_value": 143.0, "source": "yfinance"},
             "vvix": {"vvix_value": 92.0, "source": "yfinance"},
             "pcce": {"source": "cboe", "call_volume": 10, "put_volume": 9}}
        d = _degradation_check(c)
        assert d == {"vix_term": "explicit_default_fallback"}

    def test_legacy_data_without_source_still_caught(self):
        from cloud_snapshot_fetch import _degradation_check
        c = {"vix_term": {"vix_spot": 15.0, "vix_1m": 15.75, "vix_3m": 16.5},
             "skew": {"skew_value": 120.0}, "vvix": {"vvix_value": 85.0}}
        d = _degradation_check(c)
        assert set(d) == {"vix_term", "skew", "vvix"}


# ════════════════════════════ v0.45.241：CBOE 优先 ════════════════════════════

_TODAY = __import__("datetime").date(2026, 9, 14)


def _csv(symbol, rows):
    """rows: [(date, value_str)] → CBOE 指数 CSV 原文。"""
    return f"DATE,{symbol}\n" + "".join(f"{d.strftime('%m/%d/%Y')},{v}\n" for d, v in rows)


def _days_ago(n):
    import datetime as _dt
    return _TODAY - _dt.timedelta(days=n)


class _YfSpy:
    """记下 yfinance 被碰了几次；CBOE 拿到数据时它必须一次都不被碰。"""

    def __init__(self, close=None):
        self.calls = []
        self._close = close

    def download(self, *a, **k):
        import pandas as pd
        self.calls.append(("download", a))
        return pd.DataFrame({"Close": [self._close]}) if self._close is not None else pd.DataFrame()

    def Ticker(self, symbol):  # noqa: N802 - 仿 yfinance 接口
        self.calls.append(("Ticker", symbol))
        raise AssertionError("CBOE 已拿到数据，不该再碰 yfinance")


@pytest.fixture
def pinned_today(monkeypatch):
    import cboe_fetcher as cf
    monkeypatch.setattr(cf, "_today_et", lambda: _TODAY)


class TestSkewVvixFromCboe:
    @pytest.mark.parametrize("method,key,symbol,value,signal", [
        ("fetch_skew_index", "skew_value", "SKEW", "154.490000", "extreme_tail_risk"),
        ("fetch_vvix", "vvix_value", "VVIX", "91.280000", "normal"),
    ])
    def test_cboe_csv_is_primary_and_yfinance_untouched(self, fetcher, monkeypatch, pinned_today,
                                                        method, key, symbol, value, signal):
        import cboe_fetcher as cf
        spy = _YfSpy(close=999.0)
        monkeypatch.setattr(cf, "yf", spy)
        monkeypatch.setattr(cf, "_download_cboe_index_csv", lambda s: _csv(s, [
            (_days_ago(4), "140.0" if s == "SKEW" else "95.0"),
            (_days_ago(3), value),                      # 最新一行：周五
        ]))
        r = getattr(fetcher, method)()
        assert r["source"] == "cboe_cdn"
        assert r[key] == pytest.approx(float(value))
        assert r["signal"] == signal
        assert r["date"] == _days_ago(3).isoformat(), "date 必须是观测日，不是抓取日"
        assert spy.calls == [], f"CBOE 已拿到，仍去碰了 yfinance：{spy.calls}"

    def test_rows_out_of_order_takes_latest_date(self, fetcher, monkeypatch, pinned_today):
        import cboe_fetcher as cf
        monkeypatch.setattr(cf, "_download_cboe_index_csv", lambda s: _csv(s, [
            (_days_ago(1), "150.1"), (_days_ago(5), "130.0")]))
        assert fetcher.fetch_skew_index()["skew_value"] == pytest.approx(150.1)

    def test_garbage_and_out_of_range_rows_are_skipped(self, fetcher, monkeypatch, pinned_today):
        import cboe_fetcher as cf
        text = _csv("SKEW", [(_days_ago(2), "141.5")]) + f"{_days_ago(1).strftime('%m/%d/%Y')},0.0\nnot-a-date,150\n"
        monkeypatch.setattr(cf, "_download_cboe_index_csv", lambda s: text)
        r = fetcher.fetch_skew_index()
        assert (r["skew_value"], r["date"]) == (pytest.approx(141.5), _days_ago(2).isoformat())

    @pytest.mark.parametrize("lag,expect_cboe", [(7, True), (8, False)])
    def test_stale_boundary(self, fetcher, monkeypatch, pinned_today, lag, expect_cboe):
        """最新一行早于今天 >7 个日历日 ⇒ 弃用 CBOE、退回 yfinance；恰 7 天仍收。"""
        import cboe_fetcher as cf
        monkeypatch.setattr(cf, "yf", _YfSpy(close=133.0))
        monkeypatch.setattr(cf, "_download_cboe_index_csv", lambda s: _csv(s, [(_days_ago(lag), "144.0")]))
        r = fetcher.fetch_skew_index()
        assert r["source"] == ("cboe_cdn" if expect_cboe else "yfinance")

    def test_header_change_falls_back_to_yfinance(self, fetcher, monkeypatch, pinned_today):
        import cboe_fetcher as cf
        monkeypatch.setattr(cf, "yf", _YfSpy(close=133.0))
        monkeypatch.setattr(cf, "_download_cboe_index_csv",
                            lambda s: _csv("CLOSE", [(_days_ago(1), "144.0")]))
        r = fetcher.fetch_vvix()
        assert r["source"] == "yfinance" and r["vvix_value"] == pytest.approx(133.0)

    def test_both_sources_down_is_labeled_fallback(self, fetcher, monkeypatch):
        import cboe_fetcher as cf
        monkeypatch.setattr(cf, "yf", None)
        r = fetcher.fetch_vvix()
        assert (r["vvix_value"], r["source"]) == (85.0, "default_fallback")


_VINTAGE = "2026-09-14T16:15:00"


def _opt(symbol, expiry, cp, volume, strike=500):
    yy, mm, dd = expiry[2:4], expiry[5:7], expiry[8:10]
    return {"option": f"{symbol}{yy}{mm}{dd}{cp}{int(strike * 1000):08d}", "volume": volume}


def _payload(symbol, legs):
    """legs: [(expiry, call_vol, put_vol)]"""
    opts = []
    for expiry, c, p in legs:
        opts += [_opt(symbol, expiry, "C", c), _opt(symbol, expiry, "P", p)]
    return {"options": opts, "last_trade_time": _VINTAGE}


class TestPutCallFromCboe:
    LEGS = [
        ("2026-09-11", 10**9, 10**9),   # 已到期、仍挂在文件里 ⇒ 不计
        ("2026-09-14", 100, 150),       # 当日到期（yfinance 的前 3 个到期日同样含当日，09-14 实测）
        ("2026-09-15", 200, 180),
        ("2026-09-16", 300, 270),
        ("2026-09-18", 10**9, 1),       # 第 4 个到期日 ⇒ 不计
    ]

    def _patch_cboe(self, monkeypatch, per_symbol):
        import cboe_options
        monkeypatch.setattr(cboe_options, "_fetch_cboe_payload",
                            lambda ticker, *a, **k: per_symbol.get(ticker))

    def test_cboe_chain_is_primary_nearest_three_expiries(self, fetcher, monkeypatch):
        import cboe_fetcher as cf
        spy = _YfSpy()
        monkeypatch.setattr(cf, "yf", spy)
        self._patch_cboe(monkeypatch, {s: _payload(s, self.LEGS) for s in ("SPY", "QQQ", "IWM")})
        r = fetcher.fetch_equity_putcall_ratio()
        assert r["source"] == "synthetic_cboe_options"
        assert (r["call_volume"], r["put_volume"]) == (3 * 600, 3 * 600)
        assert r["total_pc_ratio"] == pytest.approx(1.0)
        assert r["tickers_used"] == ["SPY", "QQQ", "IWM"]
        assert r["date"] == "2026-09-14"
        assert spy.calls == [], f"CBOE 已拿到，仍去碰了 yfinance：{spy.calls}"

    def test_zero_volume_expiry_still_takes_a_slot(self, fetcher, monkeypatch):
        """近 3 个到期日按**到期日**数，不按「有成交的到期日」数 —— 否则第 4 个被悄悄顶进来。"""
        legs = [("2026-09-14", 100, 150), ("2026-09-15", 0, 0), ("2026-09-16", 300, 270),
                ("2026-09-18", 10**9, 1)]
        self._patch_cboe(monkeypatch, {"SPY": _payload("SPY", legs)})
        r = fetcher.fetch_equity_putcall_ratio()
        assert (r["call_volume"], r["put_volume"]) == (400, 420)

    def test_partial_cboe_keeps_cboe_without_mixing_yfinance(self, fetcher, monkeypatch):
        import cboe_fetcher as cf
        spy = _YfSpy()
        monkeypatch.setattr(cf, "yf", spy)
        self._patch_cboe(monkeypatch, {"SPY": _payload("SPY", self.LEGS)})   # QQQ / IWM 取不到
        r = fetcher.fetch_equity_putcall_ratio()
        assert r["source"] == "synthetic_cboe_options" and r["tickers_used"] == ["SPY"]
        assert spy.calls == []

    def test_zero_volume_everywhere_is_not_a_reading(self, fetcher, monkeypatch):
        """开盘前全链零成交 ⇒ 不是「P/C 算不出」的观测，要往下降级，不许报 0/0。"""
        import cboe_fetcher as cf
        monkeypatch.setattr(cf, "yf", None)
        self._patch_cboe(monkeypatch, {s: _payload(s, [("2026-09-14", 0, 0)]) for s in ("SPY", "QQQ", "IWM")})
        r = fetcher.fetch_equity_putcall_ratio()
        assert (r["source"], r["total_pc_ratio"]) == ("default_fallback", 0.95)

    def test_cboe_down_falls_back_to_yfinance_same_definition(self, fetcher, monkeypatch):
        import cboe_fetcher as cf
        import pandas as pd

        class _Tk:
            options = ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-18")

            def option_chain(self, expiry):
                v = 10**9 if expiry == "2026-09-18" else 100
                return types.SimpleNamespace(calls=pd.DataFrame({"volume": [v]}),
                                             puts=pd.DataFrame({"volume": [50]}))

        monkeypatch.setattr(cf, "yf", types.SimpleNamespace(Ticker=lambda s: _Tk()))
        r = fetcher.fetch_equity_putcall_ratio()        # autouse：CBOE 取不到
        assert r["source"] == "synthetic_yf_options"
        assert (r["call_volume"], r["put_volume"]) == (3 * 300, 3 * 150)

    def test_both_sources_down_is_labeled_fallback(self, fetcher, monkeypatch):
        import cboe_fetcher as cf
        monkeypatch.setattr(cf, "yf", None)
        r = fetcher.fetch_equity_putcall_ratio()
        assert (r["source"], r["total_pc_ratio"], r["call_volume"]) == ("default_fallback", 0.95, 0)
        assert r.get("error") == "yfinance 未安装"


class TestDegradationCheckAcceptsCboeSources:
    def test_new_source_labels_are_not_flagged(self):
        from cloud_snapshot_fetch import _degradation_check
        c = {"vix_term": {"vix_spot": 17.4, "vix_1m": 16.6, "vix_3m": 19.1, "source": "vx_futures"},
             "skew": {"skew_value": 154.49, "source": "cboe_cdn"},
             "vvix": {"vvix_value": 91.28, "source": "cboe_cdn"},
             "pcce": {"source": "synthetic_cboe_options", "call_volume": 2936627, "put_volume": 3444361}}
        assert _degradation_check(c) == {}

