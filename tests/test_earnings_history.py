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

    def test_bmo_and_amc_reactions_give_the_same_window_move(self):
        """两日窗口的全部意义：反应落在 D（BMO）还是 D+1（AMC）都算出同一个数。

        旧版本拿同样的参数把同一个函数调了两遍再断言相等——同义反复，把实现换成
        `return []` 之外的任何东西都过。这里改成造两条**真实反应位置不同**的价格序列。
        """
        ed = "2026-04-22"
        bmo = eh.realized_earnings_moves("XYZ", [ed], _bars([          # 反应在 D 当天
            ("2026-04-21", 100.0), ("2026-04-22", 108.0), ("2026-04-23", 108.0)]))[0]
        amc = eh.realized_earnings_moves("XYZ", [ed], _bars([          # 反应在 D+1
            ("2026-04-21", 100.0), ("2026-04-22", 100.0), ("2026-04-23", 108.0)]))[0]
        assert bmo["move_pct"] == pytest.approx(amc["move_pct"]) == pytest.approx(8.0)
        assert (bmo["pre_date"], bmo["post_date"]) == (amc["pre_date"], amc["post_date"])
        # 且 D 当天的收盘根本不参与（两条序列在 D 上差了 8 块，结果一样）
        assert bmo["pre_close"] == amc["pre_close"] == 100.0
        assert bmo["post_close"] == amc["post_close"] == 108.0

    def test_friday_event_spans_weekend(self):
        out = eh.realized_earnings_moves("XYZ", ["2026-04-24"], WEEK)
        assert out[0]["pre_date"] == "2026-04-23" and out[0]["post_date"] == "2026-04-27"
        assert out[0]["move_pct"] == pytest.approx((110.0 / 108.0 - 1) * 100, abs=1e-3)

    def test_negative_move_abs(self):
        bars = _bars([("2026-04-21", 100.0), ("2026-04-22", 95.0), ("2026-04-23", 90.0)])
        m = eh.realized_earnings_moves("XYZ", ["2026-04-22"], bars)[0]
        assert m["move_pct"] == pytest.approx(-10.0) and m["abs_move_pct"] == pytest.approx(10.0)

    def test_skips_dates_without_bars(self):
        # 03-10：全在 K 线之前；04-29：有 pre 无 post；05-15：全在之后
        out = eh.realized_earnings_moves("XYZ", ["2026-03-10", "2026-04-29", "2026-05-15", "2026-04-22"], WEEK)
        assert [m["earnings_date"] for m in out] == ["2026-04-22"]

    def test_skips_when_window_spans_a_data_hole(self):
        bars = _bars([("2026-04-01", 100.0), ("2026-04-30", 130.0)])   # 29 天的洞
        assert eh.realized_earnings_moves("XYZ", ["2026-04-15"], bars) == []

    @pytest.mark.parametrize("bad", [float("nan"), 0.0, -3.0, float("inf")])
    def test_ignores_nan_and_nonpositive_bars(self, bad):
        # 坏值就贴在事件前一天：不过滤的话 pre 会选中它，move 变 NaN/Inf 或荒谬值。
        bars = _bars([("2026-04-21", 100.0), ("2026-04-22", bad),
                      ("2026-04-23", 103.0), ("2026-04-24", 110.0)])
        m = eh.realized_earnings_moves("XYZ", ["2026-04-23"], bars)
        assert len(m) == 1
        assert m[0]["pre_date"] == "2026-04-21" and m[0]["post_date"] == "2026-04-24"
        assert m[0]["move_pct"] == pytest.approx(10.0)

    def test_skips_when_bars_are_not_adjacent_to_the_event(self):
        # M5 回归：只有 04-08 与 04-14 两根、财报 04-13。日历跨度 6 天 ≤ 7，
        # 日历闸放行，把 4 个交易日的漂移记成 12.0% 的"两日跳空"。
        holed = _bars([("2026-04-08", 100.0), ("2026-04-14", 112.0)])
        assert eh.realized_earnings_moves("XYZ", ["2026-04-13"], holed) == []
        # 同样的 pre/post、同样的日历跨度，只是补上了 04-13 那根 → 正常收下。
        # （即两条用例只有索引距离不同，所以本断言只可能被索引闸区分开。）
        whole = _bars([("2026-04-08", 100.0), ("2026-04-13", 105.0), ("2026-04-14", 112.0)])
        out = eh.realized_earnings_moves("XYZ", ["2026-04-13"], whole)
        assert len(out) == 1 and out[0]["move_pct"] == pytest.approx(12.0)
        assert (out[0]["pre_date"], out[0]["post_date"]) == ("2026-04-08", "2026-04-14")

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

        FULL = ["2026-10-20", "2026-07-21", "2026-04-22", "2026-01-28", "2025-10-29"]

        def fake(tk, limit):
            calls.append(tk)
            return FULL                               # 够长才进缓存（见 L2 那条测试）
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", fake)
        a = eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        b = eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        assert a == b == ["2026-07-21", "2026-04-22", "2026-01-28", "2025-10-29"]
        assert calls == ["XYZ"]                       # 第二次命中缓存
        path = tmp_path / "XYZ_history.json"
        assert json.loads(path.read_text())["dates"] == FULL
        # 缓存过期 → 重拉
        old = time.time() - eh.HISTORY_TTL - 60
        os.utime(path, (old, old))
        eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        assert calls == ["XYZ", "XYZ"]

    def test_cache_is_today_independent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw",
                            lambda tk, limit: ["2026-10-20", "2026-07-21", "2026-04-22",
                                               "2026-01-28", "2025-10-29"])
        assert eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)[:2] == ["2026-07-21", "2026-04-22"]
        # 同一份缓存，today 往后挪 → 10-20 变成过去
        assert eh.get_past_earnings_dates("XYZ", today="2026-11-01", cache_dir=tmp_path)[0] == "2026-10-20"

    def test_truncated_but_successful_fetch_is_not_cached(self, tmp_path, monkeypatch):
        """L2 回归：只拿到 3 条的一次"成功"取数不该封 30 天缓存。

        封了就是把这只票变成整整一个月的"没有历史"，长得和真的没有一模一样。
        """
        calls = []

        def short(tk, limit):
            calls.append(tk)
            return ["2026-07-21", "2026-04-22", "2026-01-28"]     # 3 < MIN_EVENTS + 1
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", short)
        a = eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        b = eh.get_past_earnings_dates("XYZ", today="2026-09-03", cache_dir=tmp_path)
        assert a == b == ["2026-07-21", "2026-04-22", "2026-01-28"]   # 结果照给，不降级
        assert calls == ["XYZ", "XYZ"]                                 # 但每次都重试
        assert not (tmp_path / "XYZ_history.json").exists()

        # 反面：够长的一份仍然照常进缓存（否则这条修复就变成"永不缓存"）
        long_calls = []

        def long(tk, limit):
            long_calls.append(tk)
            return ["2026-10-20", "2026-07-21", "2026-04-22", "2026-01-28", "2025-10-29"]
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", long)
        eh.get_past_earnings_dates("ABC", today="2026-09-03", cache_dir=tmp_path)
        eh.get_past_earnings_dates("ABC", today="2026-09-03", cache_dir=tmp_path)
        assert long_calls == ["ABC"] and (tmp_path / "ABC_history.json").exists()

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
        # 财报日当天那根必须在（真实日线序列里它就在）：M5 要求 pre/ed/post 索引相邻
        rows += [(pre, 100.0), (d, 100.0), (post, 100.0 * (1 + jump / 100.0))]
        dates.append(d)
    return dates, _bars(rows)


def _five_events_bars():
    """5 次财报：01-10 3%、04-10 4%、05-10 5%、06-10 6%、08-25 8%。

    全取 → n=5、中位 5.0；只取 2026-08-20 之前 → n=4、中位 (4+5)/2 = 4.5。
    """
    rows, dates = [], []
    for d, jump in [("2026-01-10", 3.0), ("2026-04-10", 4.0), ("2026-05-10", 5.0),
                    ("2026-06-10", 6.0), ("2026-08-25", 8.0)]:
        y, m, day = d.split("-")
        pre = f"{y}-{m}-{int(day) - 1:02d}"
        post = f"{y}-{m}-{int(day) + 1:02d}"
        rows += [(pre, 100.0), (d, 100.0), (post, 100.0 * (1 + jump / 100.0))]
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

    def test_moves_cache_is_refiltered_by_today_no_lookahead(self, tmp_path, monkeypatch):
        """M2 回归：`{T}_moves.json` 按 ticker 存、TTL 30 天，读出来必须按 today 再过滤。

        暖缓存时 today=2026-09-03（5 个事件，中位 5.0），随后按 today=2026-08-20 回补：
        08-25 那次财报当时**还没发生**，混进分母就是 look-ahead（n=5 / 中位 5.0 而不是
        n=4 / 中位 4.5）。
        """
        dates, bars = _five_events_bars()
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", lambda tk, limit: dates)
        bf = lambda tk: (bars, "synthetic")           # noqa: E731
        warm = eh.earnings_move_stats("XYZ", today="2026-09-03", cache_dir=tmp_path, bars_fn=bf)
        assert warm["n"] == 5 and warm["median_abs_move_pct"] == pytest.approx(5.0)
        assert (tmp_path / "XYZ_moves.json").exists()

        back = eh.earnings_move_stats("XYZ", today="2026-08-20", cache_dir=tmp_path, bars_fn=bf)
        assert back["n"] == 4 and back["median_abs_move_pct"] == pytest.approx(4.5)
        assert all(m["earnings_date"] < "2026-08-20" for m in back["moves"])
        assert "2026-08-25" not in back["dates"]
        # 缓存本身没被回补改写：往回看一次不该污染正常路径
        assert eh.earnings_move_stats("XYZ", today="2026-09-03", cache_dir=tmp_path,
                                      bars_fn=bf)["n"] == 5

    def test_moves_cache_in_old_format_is_treated_as_a_miss(self, tmp_path, monkeypatch):
        """没有 dates 字段的旧缓存无法安全重过滤 → 当未命中重算，不猜。"""
        dates, bars = _five_events_bars()
        monkeypatch.setattr(eh, "_fetch_earnings_dates_raw", lambda tk, limit: dates)
        stale = {"ticker": "XYZ", "n": 99, "usable": True, "median_abs_move_pct": 42.0,
                 "moves": [], "source": "old"}
        (tmp_path / "XYZ_moves.json").write_text(json.dumps(stale), encoding="utf-8")
        got = eh.earnings_move_stats("XYZ", today="2026-09-03", cache_dir=tmp_path,
                                     bars_fn=lambda tk: (bars, "synthetic"))
        assert got["n"] == 5 and got["median_abs_move_pct"] == pytest.approx(5.0)

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
