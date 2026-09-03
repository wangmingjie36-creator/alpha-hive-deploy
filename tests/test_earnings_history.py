"""v0.45.101 财报历史实际波动（earnings_history）。

全部离线：财报日期取数打桩、K 线手工合成、缓存目录用 tmp_path。
"""

import json
import os
import time

import pytest

import earnings_history as eh


def _bars(rows):
    return [{"date": d, "close": c} for d, c in rows]


# 2026-04-20（周一）～ 04-29 连续交易日；04-22 周三是财报日
WEEK = _bars([
    ("2026-04-20", 100.0), ("2026-04-21", 100.0), ("2026-04-22", 103.0),
    ("2026-04-23", 108.0), ("2026-04-24", 107.0), ("2026-04-27", 110.0),
    ("2026-04-28", 109.0), ("2026-04-29", 111.0),
])


# ==================== 两日窗口 ====================

class TestTwoDayWindow:

    def test_pre_is_last_bar_before_and_post_is_first_bar_after(self):
        out = eh.realized_earnings_moves("XYZ", ["2026-04-22"], WEEK)
        assert len(out) == 1
        m = out[0]
        assert m["pre_date"] == "2026-04-21" and m["post_date"] == "2026-04-23"
        assert m["move_pct"] == pytest.approx(8.0)
        assert m["abs_move_pct"] == pytest.approx(8.0)

    def test_bmo_and_amc_same_date_give_identical_result(self):
        """窗口只看日期，不看盘前盘后——同一 D 只可能算出同一个数（按构造）。"""
        bmo = eh.realized_earnings_moves("XYZ", ["2026-04-22"], WEEK)[0]
        amc = eh.realized_earnings_moves("XYZ", ["2026-04-22"], WEEK)[0]
        assert bmo == amc
        # 且 D 当天的收盘（103）根本不参与
        assert 103.0 not in (bmo["pre_close"], bmo["post_close"])

    def test_friday_event_spans_weekend(self):
        out = eh.realized_earnings_moves("XYZ", ["2026-04-24"], WEEK)
        assert out[0]["pre_date"] == "2026-04-23" and out[0]["post_date"] == "2026-04-27"
        assert out[0]["move_pct"] == pytest.approx((110.0 / 108.0 - 1) * 100, abs=1e-3)

    def test_negative_move_abs(self):
        bars = _bars([("2026-04-21", 100.0), ("2026-04-23", 90.0)])
        m = eh.realized_earnings_moves("XYZ", ["2026-04-22"], bars)[0]
        assert m["move_pct"] == pytest.approx(-10.0) and m["abs_move_pct"] == pytest.approx(10.0)

    def test_skips_dates_without_bars(self):
        # 03-10：全在 K 线之前；04-29：有 pre 无 post；05-15：全在之后
        out = eh.realized_earnings_moves("XYZ", ["2026-03-10", "2026-04-29", "2026-05-15", "2026-04-22"], WEEK)
        assert [m["earnings_date"] for m in out] == ["2026-04-22"]

    def test_skips_when_window_spans_a_data_hole(self):
        bars = _bars([("2026-04-01", 100.0), ("2026-04-30", 130.0)])   # 29 天的洞
        assert eh.realized_earnings_moves("XYZ", ["2026-04-15"], bars) == []

    def test_ignores_nan_and_nonpositive_bars(self):
        bars = _bars([("2026-04-21", float("nan")), ("2026-04-20", 100.0),
                      ("2026-04-23", 0.0), ("2026-04-24", 105.0)])
        m = eh.realized_earnings_moves("XYZ", ["2026-04-22"], bars)
        assert len(m) == 1
        assert m[0]["pre_date"] == "2026-04-20" and m[0]["post_date"] == "2026-04-24"

    def test_output_newest_first(self):
        bars = _bars([(f"2026-0{m}-1{d}", 100.0 + m) for m in (3, 4, 5, 6) for d in range(0, 6)])
        out = eh.realized_earnings_moves("XYZ", ["2026-03-12", "2026-06-12", "2026-04-12"], bars)
        assert [m["earnings_date"] for m in out] == ["2026-06-12", "2026-04-12", "2026-03-12"]


# ==================== 财报日期缓存 / 闸门 ====================

class TestPastEarningsDates:

    def test_filters_strictly_before_today_newest_first_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw",
                            lambda tk, limit: ["2026-10-20", "2026-09-03", "2026-07-21",
                                               "2026-04-22", "2026-01-28", "2025-10-29"])
        got = eh.get_past_earnings_dates("xyz", n=3, today="2026-09-03", cache_dir=tmp_path)
        assert got == ["2026-07-21", "2026-04-22", "2026-01-28"]   # 09-03 == today 被排除

    def test_cache_roundtrip_and_ttl(self, tmp_path, monkeypatch):
        calls = []

        def fake(tk, limit):
            calls.append(tk)
            return ["2026-07-21", "2026-04-22"]
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", fake)
        a = eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        b = eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        assert a == b == ["2026-07-21", "2026-04-22"]
        assert calls == ["XYZ"]                       # 第二次命中缓存
        path = tmp_path / "XYZ_history.json"
        assert json.loads(path.read_text())["dates"] == ["2026-07-21", "2026-04-22"]
        # 缓存过期 → 重拉
        old = time.time() - eh.HISTORY_TTL - 60
        os.utime(path, (old, old))
        eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        assert calls == ["XYZ", "XYZ"]

    def test_cache_is_today_independent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw",
                            lambda tk, limit: ["2026-10-20", "2026-07-21", "2026-04-22"])
        assert eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path) == ["2026-07-21", "2026-04-22"]
        # 同一份缓存，today 往后挪 → 10-20 变成过去
        assert eh.get_past_earnings_dates("XYZ", today="2026-11-01", cache_dir=tmp_path)[0] == "2026-10-20"

    def test_gate_failure_returns_none_not_empty_list(self, tmp_path, monkeypatch):
        import yf_gate

        def boom():
            raise yf_gate.YFRateLimited("cooldown")
        monkeypatch.setattr(eh, "_gate_ensure", boom)
        assert eh._fetch_earnings_dates_raw("XYZ", 12) is None
        got = eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        assert got is None and got != []
        assert not (tmp_path / "XYZ_history.json").exists()   # 失败不落缓存

    def test_empty_dataframe_returns_none(self, tmp_path, monkeypatch):
        import sys
        import types
        import pandas as pd
        fake_yf = types.SimpleNamespace(
            Ticker=lambda t: types.SimpleNamespace(get_earnings_dates=lambda limit=12: pd.DataFrame()))
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
        monkeypatch.setattr(eh, "_gate_ensure", lambda: None)
        assert eh._fetch_earnings_dates_raw("XYZ", 12) is None

    def test_dataframe_index_parsed_and_deduped(self, monkeypatch):
        import sys
        import types
        import pandas as pd
        idx = pd.to_datetime(["2026-07-21 16:00", "2026-07-21 20:00", "2026-04-22 08:00"]).tz_localize("America/New_York")
        fake_yf = types.SimpleNamespace(
            Ticker=lambda t: types.SimpleNamespace(get_earnings_dates=lambda limit=12: pd.DataFrame({"x": [1, 2, 3]}, index=idx)))
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
        monkeypatch.setattr(eh, "_gate_ensure", lambda: None)
        assert eh._fetch_earnings_dates_raw("XYZ", 12) == ["2026-07-21", "2026-04-22"]


# ==================== 统计 ====================

def _eight_events_bars():
    """8 个财报日，每个前后各一根；跳幅 |2,4,6,8,10,12,14,16|% → 中位 9、均值 9、最大 16。"""
    rows, dates = [], []
    day = 10
    for i, jump in enumerate([2, -4, 6, -8, 10, -12, 14, -16]):
        month = i + 1
        d = f"2026-{month:02d}-{day:02d}"
        pre = f"2026-{month:02d}-{day - 1:02d}"
        post = f"2026-{month:02d}-{day + 1:02d}"
        rows += [(pre, 100.0), (post, 100.0 * (1 + jump / 100.0))]
        dates.append(d)
    return dates, _bars(rows)


class TestMoveStats:

    def test_happy_path_median_mean_max(self, tmp_path, monkeypatch):
        dates, bars = _eight_events_bars()
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", lambda tk, limit: dates)
        s = eh.earnings_move_stats("XYZ", n=8, today="2026-09-03", cache_dir=tmp_path,
                                   bars_fn=lambda tk: (bars, "synthetic"))
        assert s["n"] == 8 and s["n_missing"] == 0
        assert s["median_abs_move_pct"] == pytest.approx(9.0)
        assert s["mean_abs_move_pct"] == pytest.approx(9.0)
        assert s["max_abs_move_pct"] == pytest.approx(16.0)
        assert s["source"] == "synthetic" and s["usable"] is True
        assert (tmp_path / "XYZ_moves.json").exists()

    def test_missing_bars_counted_not_fabricated(self, tmp_path, monkeypatch):
        dates, bars = _eight_events_bars()
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", lambda tk, limit: dates)
        bars = [b for b in bars if b["date"] < "2026-07-01"]      # 只剩前 6 个事件
        s = eh.earnings_move_stats("XYZ", today="2026-09-03", cache_dir=tmp_path,
                                   bars_fn=lambda tk: (bars, "synthetic"))
        assert s["n"] == 6 and s["n_missing"] == 2

    def test_fewer_than_four_events_returns_none_with_reason_in_detail(self, tmp_path, monkeypatch):
        dates, bars = _eight_events_bars()
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", lambda tk, limit: dates)
        bars = [b for b in bars if b["date"] < "2026-04-01"]      # 3 个事件
        assert eh.earnings_move_stats("XYZ", today="2026-09-03", cache_dir=tmp_path,
                                      bars_fn=lambda tk: (bars, "synthetic")) is None
        d = eh.earnings_move_stats_detail("XYZ", today="2026-09-03", cache_dir=tmp_path,
                                          bars_fn=lambda tk: (bars, "synthetic"))
        assert d["usable"] is False and d["n"] == 3
        assert "3 usable events" in d["reason"]
        assert d["median_abs_move_pct"] is None

    def test_no_dates_or_no_bars_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", lambda tk, limit: None)
        assert eh.earnings_move_stats("XYZ", today="2026-09-03", cache_dir=tmp_path,
                                      bars_fn=lambda tk: (None, "none")) is None
        dates, _ = _eight_events_bars()
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", lambda tk, limit: dates)
        assert eh.earnings_move_stats("ABC", today="2026-09-03", cache_dir=tmp_path,
                                      bars_fn=lambda tk: (None, "none")) is None

    def test_stats_cache_hit_skips_recompute(self, tmp_path, monkeypatch):
        dates, bars = _eight_events_bars()
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", lambda tk, limit: dates)
        calls = []

        def bf(tk):
            calls.append(tk)
            return bars, "synthetic"
        eh.earnings_move_stats("XYZ", today="2026-09-03", cache_dir=tmp_path, bars_fn=bf)
        eh.earnings_move_stats("XYZ", today="2026-09-03", cache_dir=tmp_path, bars_fn=bf)
        assert calls == ["XYZ"]
