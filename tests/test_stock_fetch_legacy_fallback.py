"""v0.45.639：`swarm_agents/cache.py` 的旧回落（data_pipeline 不可用时）——修假值 + 走到它会红。

此前：① 一个 `except ImportError: pass` 同时包住 `from data_pipeline import …` 与取数调用本身，导入期缺依赖、取数途中延迟 import
失败都悄悄改走旧回落，不留一行日志；② 旧回落是公式的第四份副本，均量 NaN / ≤ 0 时把分母换成 1.0 ⇒ avg_volume = 1、
volume_ratio = 当天原始成交量（几千万），Buzz 读成「巨量」——data_pipeline 主路径 v0.45.54 / 473 修过的同一个 bug。

守四件事：
1. 两种走法（import / call）都走到旧回落，且各自登记（次数 / 阶段 / 标的 / 原因），同一个原因只打一条 ERROR。
2. 旧回落用唯一实现 `price_metrics`：均量算不出 ⇒ 量比与均量留缺失（不再是分母 1.0）；正常日线与主路径公式逐位相同。
3. scan_timing 带出 `stock_fetch_legacy`，摘要行写明；alert_manager：count > 0 ⇒ P1，0 ⇒ 不报，计数缺失 ⇒ checks_skipped。
4. data_pipeline 好好的 ⇒ 旧回落一次都不走、计数为 0（不误报）。

⚠️ 全部离线、不用 skip：yfinance / data_pipeline 都在 sys.modules 层打桩。
"""
from __future__ import annotations

import json
import logging
import sys
import types

import pytest

pd = pytest.importorskip("pandas")

import alert_manager as am
import price_metrics as pm
import scan_timing as stt
from swarm_agents import cache


def _hist(n=25, vols=None):
    idx = pd.date_range("2026-09-01", periods=n, freq="B")
    closes = [100.0 * (1.004 ** i) * (1 + 0.01 * (-1) ** i) for i in range(n)]
    return pd.DataFrame({"Close": closes, "Volume": vols if vols is not None else [1e6 + 1e4 * i for i in range(n)]},
                        index=idx)


@pytest.fixture
def legacy_env(monkeypatch):
    """yfinance 打桩成固定日线；清掉旧回落自己的缓存与计数；限流 / 熔断放行。返回 setter：set_hist(df)。"""
    state = {"hist": _hist()}

    class _Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, period="1mo"):
            return state["hist"].copy()

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
    monkeypatch.setattr(cache.yfinance_limiter, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(cache.yfinance_breaker, "allow_request", lambda *a, **k: True)
    monkeypatch.setattr(cache.yfinance_breaker, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(cache.yfinance_breaker, "record_failure", lambda *a, **k: None)
    with cache._yf_lock:
        cache._yf_cache.clear()
        cache._yf_cache_ts.clear()
        cache._yf_insert_order.clear()
    cache._reset_legacy_fallback_stats()
    yield lambda df: state.__setitem__("hist", df)
    cache._reset_legacy_fallback_stats()
    with cache._yf_lock:
        cache._yf_cache.clear()
        cache._yf_cache_ts.clear()
        cache._yf_insert_order.clear()


def _break_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "data_pipeline", None)   # ⇒ `from data_pipeline import …` 抛 ImportError


def _break_call(monkeypatch):
    import data_pipeline

    def _boom(ticker, as_of_date=None):
        raise ModuleNotFoundError("No module named 'observation_layer'")
    monkeypatch.setattr(data_pipeline, "fetch_stock_data", _boom)


class TestBothWaysInAreRecorded:

    @pytest.mark.parametrize("stage,breaker", [("import", _break_import), ("call", _break_call)])
    def test_reaches_legacy_and_records(self, legacy_env, monkeypatch, caplog, stage, breaker):
        """变红的变异：恢复成一个 `except ImportError: pass`（计数恒 0）；只接 import 不接 call。"""
        breaker(monkeypatch)
        with caplog.at_level(logging.ERROR):
            a = cache._fetch_stock_data("AAA")
            b = cache._fetch_stock_data("BBB")
        assert a["data_source"] == "real" and b["price"] > 0, "确实走到了旧回落并取到了数"
        st = cache.legacy_fallback_stats()
        assert st["count"] == 2 and st["stages"] == {stage: 2} and st["tickers"] == ["AAA", "BBB"]
        assert len(st["errors"]) == 1 and st["errors"][0].startswith(f"{stage}: ")
        errs = [r for r in caplog.records if r.levelno == logging.ERROR and "旧回落" in r.getMessage()]
        assert len(errs) == 1, "同一个原因只打一条 ERROR（30 只不刷屏），次数照数"

    def test_healthy_pipeline_never_touches_legacy(self, legacy_env, monkeypatch):
        import data_pipeline
        monkeypatch.setattr(data_pipeline, "fetch_stock_data",
                            lambda ticker, as_of_date=None: {"price": 1.0, "data_source": "real"})
        assert cache._fetch_stock_data("AAA")["price"] == 1.0
        assert cache.legacy_fallback_stats()["count"] == 0


class TestLegacyUsesTheOneFormula:

    @pytest.mark.parametrize("vols", [[0.0] * 25, [float("nan")] * 25], ids=["zero", "nan"])
    def test_missing_average_is_none_not_denominator_one(self, legacy_env, monkeypatch, vols):
        """v0.45.639 修的那一行：此前 volume_ratio = 末根成交量 / 1.0、avg_volume = 1。
        变红的变异：把 `avg_vol = 1.0` 兜底写回去。"""
        _break_import(monkeypatch)
        legacy_env(_hist(vols=vols))
        d = cache._fetch_stock_data("AAA")
        assert d["volume_ratio"] is None and d["avg_volume"] == 0
        assert d["momentum_5d"] is not None and d["volatility_20d"] is not None   # 只降级缺的那一项

    def test_values_equal_the_shared_formula(self, legacy_env, monkeypatch):
        _break_import(monkeypatch)
        hist = _hist()
        legacy_env(hist)
        d, m = cache._fetch_stock_data("AAA"), pm.history_price_metrics(hist)
        for k in ("momentum_5d", "avg_volume", "volume_ratio", "volatility_20d"):
            assert d[k] == m[k], k

    def test_single_bar_gives_no_ratio(self, legacy_env, monkeypatch):
        """1 根时「均量」就是它自己（量比恒 1.0）——同主路径，不算。"""
        _break_import(monkeypatch)
        legacy_env(_hist(n=1))
        d = cache._fetch_stock_data("AAA")
        assert d["price"] > 0 and d["volume_ratio"] is None and d["momentum_5d"] is None


class TestScanTimingCarriesIt:

    def test_counter_present_and_summarised(self, legacy_env, monkeypatch):
        _break_import(monkeypatch)
        cache._fetch_stock_data("AAA")
        c = stt.counters()
        assert c["stock_fetch_legacy"]["count"] == 1
        line = stt.summary_line({"phases": {}, "counters": c})
        assert "旧回落 1 次" in line

    def test_zero_is_silent_in_summary(self, legacy_env):
        c = stt.counters()
        assert c["stock_fetch_legacy"] == {"count": 0, "stages": {}, "tickers": [], "errors": []}
        assert "旧回落" not in stt.summary_line({"phases": {}, "counters": c})


def _analyze(tmp_path, counter):
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": {"stock_fetch_legacy": counter}, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    if counter is None:
        del status["scan_timing"]["counters"]["stock_fetch_legacy"]
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return [x for x in a.alerts if "cache.py 旧回落" in x.message], a


class TestAlert:

    def test_nonzero_is_p1(self, tmp_path):
        """变红的变异：analyze() 不调 `_check_stock_fetch_legacy`；或级别降成 P2。"""
        hits, _ = _analyze(tmp_path, {"count": 30, "stages": {"call": 30}, "tickers": [f"T{i}" for i in range(30)],
                                      "errors": ["call: ModuleNotFoundError: No module named 'observation_layer'"]})
        (h,) = hits
        assert h.level == am.AlertLevel.HIGH and "30 次" in h.message
        assert "observation_layer" in h.details["原因"] and "等 30 只" in h.details["标的"]

    def test_zero_is_silent(self, tmp_path):
        hits, a = _analyze(tmp_path, {"count": 0, "stages": {}, "tickers": [], "errors": []})
        assert hits == [] and not any("行情旧回落检查" in s for s in a.checks_skipped)

    def test_missing_counter_is_skipped_not_silent(self, tmp_path):
        hits, a = _analyze(tmp_path, None)
        assert hits == [] and any("行情旧回落检查" in s for s in a.checks_skipped)
