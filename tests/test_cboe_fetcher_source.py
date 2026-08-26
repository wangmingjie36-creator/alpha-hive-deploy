"""cboe_fetcher 数据来源标注守卫（v0.45.29）。

守什么：
1. VIX 期限结构的 vix_1m/vix_3m 必须来自真实 VX 期货（vixcentral M1/M3），
   **不再**是 VIXY ETF 股价 × 0.5 / spot × 1.10 的合成口径。
2. 任何兜底路径产出的 dict 必须带 source='default_fallback'——
   兜底常量冒充观测值（v0.43.24 同款）是本项目反复出现的静默降级形态，
   本文件的测试全部按「喂退化数据看它红」构造。
3. v0.45.29 之前的旧缓存（无 source 键）必须被视为过期重抓，
   否则 VIXY 垃圾口径会经由当日缓存再活一天。
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
