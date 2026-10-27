"""v0.45.653：行情三字段算出非有限数（真 K 线缺收盘 / 缺量，2026-08-28 型）不再进评分——生产与维度 IC 冻结通道层同口径。

背景
----
2026-08-29（周六）补跑 08-28 时，yfinance 给的 08-28 那根 K 线成交量正常（1.94 亿）、收盘却是 NaN：当天成交了，只是 Yahoo 还没填收盘。
补跑路径 v0.45.69 起就丢 NaN 收盘、再要求末行是目标日；实时路径没管——末根 / 倒数第 5 根收盘为 NaN ⇒ 动量 NaN ⇒ Buzz
`max(-10, min(10, nan))` = 10 ⇒ 动量通道满分 100、来源标 `5d_real`、出口回落只认 None 也绕过；末根成交量 NaN ⇒ 量比 NaN ⇒ 量能通道最低档 20。
冻结层原样复刻，所以此前没单改生产（v0.45.639 / 645）。用户 10-27 选「根治」：两层一起改成「非有限 ⇒ 取不到」，交给出口回落。

本文件钉住
----------
1. `history_price_metrics` 返回的数一定是有限数或 None；三种形状（末根收盘缺 / 倒数第 5 根收盘缺 / 末根成交量缺）各自只让那一项变 None。
2. 三条实时路径 + cache.py 旧回落：动量 / 量比是 None（不再是 NaN）、标签不冒充 yfinance；旧回落末根收盘缺 ⇒ 标不可用（NaN 能穿过下游 `price <= 0`）。
3. 冻结层同判（差分对拍在 `tests/test_buzz_v1_channels.py`，这里钉形状），映射到通道是中性 50 而不是 100 / 20。
4. 看得见：计数 → scan_timing `price_nonfinite` → 摘要行；评分时就有 ⇒ P2，评分后才有 ⇒ INFO，计数缺失 ⇒ checks_skipped。

全部离线：yfinance 在测试里打桩。
"""
from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path

import numpy as np
import pytest

import alert_manager as am
import buzz_v1_channels as fc
import observation_layer as ol
import price_metrics as pm
import scan_timing as stt

_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = _ROOT / "tests" / "fixtures" / "yf_daily_event_row_meta_20261021.json"


def _real_bars():
    """真实形状（v0.45.645 的夹具，去掉第 0 行公司行动空行）：22 根合成值 K 线，列顺序 Close 在前。"""
    return ol.frame_from_payload(json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]).iloc[1:]


def _shape(kind):
    df = _real_bars().copy()
    if kind == "close_last":          # 2026-08-28 型：当天成交了、收盘没填
        df.iloc[-1, df.columns.get_loc("Close")] = np.nan
    elif kind == "close_5th_last":
        df.iloc[-5, df.columns.get_loc("Close")] = np.nan
    elif kind == "volume_last":
        df["Volume"] = df["Volume"].astype("float64")
        df.iloc[-1, df.columns.get_loc("Volume")] = np.nan
    return df


@pytest.fixture
def yf_stub(monkeypatch):
    import data_pipeline as dp
    import yfinance
    box = {"frame": None}

    class _T:
        def __init__(self, *_a, **_k):
            pass

        def history(self, **_k):
            return box["frame"].copy()
    monkeypatch.setattr(yfinance, "Ticker", _T)
    monkeypatch.setattr(dp, "_exchange_now", lambda: None)
    return box


class TestFormula:

    @pytest.mark.parametrize("kind,none_field", [("close_last", "momentum_5d"), ("close_5th_last", "momentum_5d"),
                                                  ("volume_last", "volume_ratio")])
    def test_only_that_field_goes_missing(self, kind, none_field):
        """变红的变异：去掉非有限检查（又是 NaN）；或整组置 None（连带丢掉算得出的项）。"""
        good = pm.history_price_metrics(_real_bars())
        got = pm.history_price_metrics(_shape(kind), "META")
        assert got[none_field] is None
        for k, v in got.items():
            assert v is None or math.isfinite(v), (k, v)
        # 均量跳过缺的那根（pandas mean，现行口径未动）⇒ volume_last 时与完好那份不等；波动率丢掉缺收盘两侧的收益 ⇒ close_* 时不等
        others = {k for k in good if k != none_field and k != "volatility_20d"
                  and not (kind == "volume_last" and k == "avg_volume")}
        assert {k: got[k] for k in others} == {k: good[k] for k in others}
        assert got["volatility_20d"] is not None, "中间 / 末根缺一个收盘，波动率照算（fill_method=None 丢掉两侧收益）"
        if none_field == "volume_ratio":
            assert got["avg_volume"] is not None, "均量照给（不含缺的那根），只有量比缺"
        st = pm.nonfinite_stats()
        assert st[none_field] == 1 and st["items"][0]["ticker"] == "META" and st["items"][0]["field"] == none_field

    def test_clean_bars_are_bit_identical_and_uncounted(self):
        out = pm.history_price_metrics(_real_bars(), "META")
        assert all(v is not None and math.isfinite(v) for v in out.values())
        assert pm.nonfinite_stats()["momentum_5d"] == pm.nonfinite_stats()["volume_ratio"] == 0


class TestLivePaths:

    def test_close_last_0828_shape(self, yf_stub):
        """2026-08-28 型落在实时路径：动量不再是 NaN（改 None，CBOE 那一支随后交给出口回落）；yfinance 源现价是 NaN ⇒ 取数出口的
        `price > 0` 本就把这一源跳过（这里只钉动量不再是 NaN）。"""
        import data_pipeline as dp
        yf_stub["frame"] = _shape("close_last")
        out = dp._fetch_history_metrics("META")
        assert "momentum_5d" not in out or out["momentum_5d"] is None
        src = dp.YFinanceSource().fetch("META")
        assert src.momentum_5d is None and src.momentum_source == "unavailable"

    def test_volume_last_label_not_yfinance(self, yf_stub):
        import data_pipeline as dp
        yf_stub["frame"] = _shape("volume_last")
        out = dp._fetch_history_metrics("META")
        assert out["volume_ratio"] is None and out["avg_volume"] is not None
        src = dp.YFinanceSource().fetch("META")
        assert src.volume_ratio is None and src.volume_source is None, "量比缺时不标 yfinance（变红的变异：照旧无条件标）"

    def test_legacy_cache_close_last_is_unavailable(self, monkeypatch):
        """cache.py 旧回落：末根收盘缺 ⇒ 不给现价、标不可用（NaN 穿得过下游所有 `price <= 0`）。"""
        from swarm_agents import cache
        state = {"hist": _shape("close_last")}

        class _Ticker:
            def __init__(self, ticker):
                pass

            def history(self, period="1mo"):
                return state["hist"].copy()
        monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
        monkeypatch.setitem(sys.modules, "data_pipeline", None)
        monkeypatch.setattr(cache.yfinance_limiter, "acquire", lambda *a, **k: None)
        monkeypatch.setattr(cache.yfinance_breaker, "allow_request", lambda *a, **k: True)
        monkeypatch.setattr(cache.yfinance_breaker, "record_success", lambda *a, **k: None)
        try:
            with cache._yf_lock:
                cache._yf_cache.clear()
                cache._yf_cache_ts.clear()
                cache._yf_insert_order.clear()
            out = cache._fetch_stock_data("ZZNF")
            state["hist"] = _shape("close_5th_last")
            with cache._yf_lock:
                cache._yf_cache.clear()
                cache._yf_cache_ts.clear()
                cache._yf_insert_order.clear()
            out5 = cache._fetch_stock_data("ZZNG")
        finally:
            with cache._yf_lock:
                cache._yf_cache.clear()
                cache._yf_cache_ts.clear()
                cache._yf_insert_order.clear()
        assert out["_data_unavailable"] is True and out["price"] == 0.0 and out["momentum_5d"] is None
        assert math.isfinite(out5["price"]) and out5["momentum_5d"] is None


class TestFrozenLayer:

    @pytest.mark.parametrize("kind,field,channel", [("close_last", "momentum_5d", "momentum_signal"),
                                                    ("close_5th_last", "momentum_5d", "momentum_signal"),
                                                    ("volume_last", "volume_ratio", "volume_signal")])
    def test_same_rule_maps_to_neutral(self, kind, field, channel):
        """冻结层同判：非有限 ⇒ None ⇒ 通道中性 50（此前 100 / 20）。变红的变异：冻结层去掉检查（差分对拍也会红）。"""
        payload = ol.frame_payload("META", {"period": "1mo"}, _shape(kind))
        f = fc.price_fields_from_daily(payload, None, True)
        assert f[field] is None
        mapping = {"momentum_signal": fc.momentum_channel, "volume_signal": fc.volume_channel}
        assert mapping[channel](f[field]) == 50
        assert mapping[channel](float("nan")) in (100.0, 20), "正对照：NaN 进映射就是 100 / 20"


def _analyze(tmp_path, counter):
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": {"price_nonfinite": counter}, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    if counter is None:
        del status["scan_timing"]["counters"]["price_nonfinite"]
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return [x for x in a.alerts if "非有限" in x.message], a


_ITEM = {"field": "momentum_5d", "ticker": "T", "last_bar": "2026-10-26", "close_last": False, "volume_last": True,
         "close_5th_last": True}
_ZERO = {"momentum_5d": 0, "volume_ratio": 0, "items": []}


class TestVisibility:

    def test_counter_and_summary(self):
        pm.history_price_metrics(_shape("close_last"), "META")
        c = stt.counters()
        assert c["price_nonfinite"]["momentum_5d"] == 1
        assert "行情非有限改缺失 动量 1 次/量比 0 次" in stt.summary_line({"phases": {}, "counters": c})
        pm._reset_nonfinite_stats()
        assert "行情非有限" not in stt.summary_line({"phases": {}, "counters": stt.counters()})

    def test_at_scoring_is_p2(self, tmp_path):
        """变红的变异：analyze() 不调 `_check_price_nonfinite`；级别不是 P2。"""
        c = {"momentum_5d": 1, "volume_ratio": 0, "items": [_ITEM]}
        hits, _ = _analyze(tmp_path, {**c, "at_scoring": dict(c)})
        (h,) = hits
        assert h.level == am.AlertLevel.MEDIUM and "末根收盘缺" in h.details["明细"]

    def test_after_scoring_only_is_info(self, tmp_path):
        hits, _ = _analyze(tmp_path, {"momentum_5d": 1, "volume_ratio": 0, "items": [_ITEM], "at_scoring": dict(_ZERO)})
        (h,) = hits
        assert h.level == am.AlertLevel.INFO

    def test_zero_is_silent_and_missing_is_skipped(self, tmp_path):
        hits, a = _analyze(tmp_path, {**_ZERO, "at_scoring": dict(_ZERO)})
        assert hits == [] and not any("行情非有限" in s for s in a.checks_skipped)
        hits, a = _analyze(tmp_path, None)
        assert hits == [] and any("行情非有限" in s for s in a.checks_skipped)

    def test_scan_marks_the_scoring_snapshot(self):
        src = (_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        assert src.index("mark_event_row_scoring()") < src.index("mark_nonfinite_scoring()") < src.index('_timing.record("prefetch"')
