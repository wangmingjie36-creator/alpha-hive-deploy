"""回测批量取价（v0.45.120）

固化的事实：2026-09-04 回测段 342s——待检预测 32 条全是同一预测日的 30 只票，
逐条各发 `yf.Ticker().history()`（t7 一条发 4 次），每次过 0.5 req/s 闸门。

这里守四条：
  1. 多票 download 帧拆分：MultiIndex 两种层序 / 单票平列名 / NaN 对齐行 / 全 NaN 票
  2. 缓存切片 = `Ticker.history(start, end)` 的 [start, end) 语义
  3. 窗口外、未缓存、未预取 → 走原来的逐票 history（退化路径就是改动前的路径）
  4. **等价性**：同一份数据，批量路径与逐票路径跑 run_backtest 写库结果逐字段相同，
     且批量路径 download 恰好 1 次、Ticker.history 0 次
"""

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtester as B


# ───────────────────────────────────────────── 合成数据
def _ohlc(start: str, periods: int, base: float, step: float, tz="America/New_York") -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq="B", tz=tz)
    close = [base + step * i for i in range(periods)]
    return pd.DataFrame({
        "Open": [c - 0.5 for c in close],
        "High": [c + 1.0 for c in close],
        "Low": [c - 1.0 for c in close],
        "Close": close,
        "Volume": [1_000_000] * periods,
    }, index=idx)


def _multi(frames: dict, ticker_level_first=True) -> pd.DataFrame:
    df = pd.concat(frames, axis=1)          # 列 = (ticker, field)
    if not ticker_level_first:
        df = df.swaplevel(axis=1).sort_index(axis=1)
    return df


@pytest.fixture(autouse=True)
def _no_exchange_clock(monkeypatch):
    import data_pipeline
    monkeypatch.setattr(data_pipeline, "_exchange_now", lambda: None)
    monkeypatch.setattr(B, "_pdt_today", lambda: "2026-08-20")


# ───────────────────────────────────────────── 1. 拆帧
class TestSplitDownloadFrame:
    def test_multiindex_ticker_level0(self):
        raw = _multi({"NVDA": _ohlc("2026-07-01", 5, 100, 1), "SPY": _ohlc("2026-07-01", 5, 500, 1)})
        out = B._split_download_frame(raw, ["NVDA", "SPY"])
        assert set(out) == {"NVDA", "SPY"}
        assert list(out["NVDA"].columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert float(out["NVDA"]["Close"].iloc[0]) == 100.0

    def test_multiindex_field_level0_is_also_understood(self):
        raw = _multi({"NVDA": _ohlc("2026-07-01", 5, 100, 1), "SPY": _ohlc("2026-07-01", 5, 500, 1)},
                     ticker_level_first=False)
        out = B._split_download_frame(raw, ["NVDA", "SPY"])
        assert set(out) == {"NVDA", "SPY"}
        assert float(out["SPY"]["Close"].iloc[-1]) == 504.0

    def test_single_ticker_flat_columns(self):
        out = B._split_download_frame(_ohlc("2026-07-01", 3, 10, 1), ["AMC"])
        assert set(out) == {"AMC"}

    def test_multi_ticker_flat_columns_is_unknown_shape(self):
        """多票却平列名：不猜，整轮回退。"""
        assert B._split_download_frame(_ohlc("2026-07-01", 3, 10, 1), ["AMC", "SPY"]) == {}

    def test_all_nan_ticker_is_not_cached(self):
        good = _ohlc("2026-07-01", 5, 100, 1)
        bad = pd.DataFrame(float("nan"), index=good.index, columns=good.columns)
        raw = _multi({"NVDA": _ohlc("2026-07-01", 5, 100, 1), "XXX": bad})
        out = B._split_download_frame(raw, ["NVDA", "XXX"])
        assert set(out) == {"NVDA"}, "全 NaN 的票不能算「已缓存」，否则下游拿到空切片当成「无数据」"

    def test_nan_alignment_rows_are_dropped(self):
        """两票交易日历不同，download 用 NaN 行对齐；Ticker.history 不会有这些行。"""
        a = _ohlc("2026-07-01", 5, 100, 1)
        b = _ohlc("2026-07-01", 5, 50, 1).drop(index=_ohlc("2026-07-01", 5, 50, 1).index[2])
        raw = _multi({"A": a, "B": b})
        assert raw["B"]["Close"].isna().sum() == 1
        out = B._split_download_frame(raw, ["A", "B"])
        assert len(out["B"]) == 4 and out["B"]["Close"].notna().all()
        assert len(out["A"]) == 5

    def test_empty_or_none(self):
        assert B._split_download_frame(None, ["A"]) == {}
        assert B._split_download_frame(pd.DataFrame(), ["A"]) == {}


# ───────────────────────────────────────────── 2. 切片语义
class TestSliceByDate:
    @pytest.mark.parametrize("tz", ["America/New_York", None])
    def test_half_open_interval(self, tz):
        df = _ohlc("2026-07-01", 10, 1, 1, tz=tz)   # 07-01 .. 07-14 的工作日
        sub = B._slice_by_date(df, date(2026, 7, 3), date(2026, 7, 8))
        got = [d.strftime("%m-%d") for d in sub.index.date]
        assert got == ["07-03", "07-06", "07-07"], "含 start、不含 end、跳过周末"

    def test_empty_when_no_rows(self):
        df = _ohlc("2026-07-01", 3, 1, 1)
        assert B._slice_by_date(df, date(2026, 8, 1), date(2026, 8, 5)).empty


# ───────────────────────────────────────────── 3. _history 的命中与回退
class TestHistory:
    @pytest.fixture
    def bt(self, monkeypatch):
        tk = MagicMock()
        tk.history.return_value = _ohlc("2026-07-06", 2, 999, 0)
        yf = MagicMock(Ticker=MagicMock(return_value=tk))
        monkeypatch.setattr(B, "yf", yf)
        b = B.Backtester.__new__(B.Backtester)
        b.store = MagicMock(); b._spy_entry_cache = {}
        b._ohlc_cache = {"NVDA": _ohlc("2026-07-01", 20, 100, 1)}
        b._ohlc_window = (date(2026, 7, 1), date(2026, 7, 31))
        b._ohlc_stats = {"batch_downloads": 0, "batch_tickers": 0, "cache_hits": 0, "fallback_history": 0}
        b._yf = yf
        return b

    def test_cache_hit_slices_and_never_calls_ticker(self, bt):
        h = bt._history("NVDA", "2026-07-06", "2026-07-08")
        assert [d.day for d in h.index.date] == [6, 7]
        assert bt._ohlc_stats["cache_hits"] == 1
        bt._yf.Ticker.assert_not_called()

    @pytest.mark.parametrize("start,end", [
        ("2026-06-30", "2026-07-08"),   # start 早于窗口
        ("2026-07-06", "2026-08-01"),   # end 晚于窗口
    ])
    def test_out_of_window_falls_back_with_same_args(self, bt, start, end):
        h = bt._history("NVDA", start, end)
        assert float(h["Close"].iloc[0]) == 999.0
        bt._yf.Ticker.assert_called_once_with("NVDA")
        bt._yf.Ticker.return_value.history.assert_called_once_with(start=start, end=end)
        assert bt._ohlc_stats["fallback_history"] == 1 and bt._ohlc_stats["cache_hits"] == 0

    def test_uncached_ticker_falls_back(self, bt):
        bt._history("TSLA", "2026-07-06", "2026-07-08")
        bt._yf.Ticker.assert_called_once_with("TSLA")

    def test_no_prefetch_means_plain_history(self, bt):
        bt._ohlc_window = None
        bt._history("NVDA", "2026-07-06", "2026-07-08")
        bt._yf.Ticker.assert_called_once_with("NVDA")

    def test_instance_built_without_init_behaves_like_no_prefetch(self, monkeypatch):
        """既有测试（test_backtest_forming_bar）与备份脚本用 `Backtester.__new__`
        绕过 __init__；这种实例没有缓存属性，必须原样走逐票 history，不许 AttributeError。
        （首版实现就在这里炸了 8 条既有测试。）"""
        tk = MagicMock()
        tk.history.return_value = _ohlc("2026-07-06", 1, 5, 0)
        yf = MagicMock(Ticker=MagicMock(return_value=tk))
        monkeypatch.setattr(B, "yf", yf)
        bt = B.Backtester.__new__(B.Backtester)
        h = bt._history("AMC", "2026-07-06", "2026-07-07")
        assert float(h["Close"].iloc[0]) == 5.0
        yf.Ticker.assert_called_once_with("AMC")


# ───────────────────────────────────────────── 4. 预取
class TestPrefetch:
    @pytest.fixture
    def bt(self, monkeypatch):
        yf = MagicMock()
        yf.download.return_value = _multi({
            "NVDA": _ohlc("2026-07-01", 30, 100, 1),
            "TSLA": _ohlc("2026-07-01", 30, 300, -1),
            "SPY": _ohlc("2026-07-01", 30, 500, 0.1),
        })
        monkeypatch.setattr(B, "yf", yf)
        b = B.Backtester.__new__(B.Backtester)
        b.store = MagicMock(); b._spy_entry_cache = {}
        b._ohlc_cache = {}; b._ohlc_window = None
        b._ohlc_stats = {"batch_downloads": 0, "batch_tickers": 0, "cache_hits": 0, "fallback_history": 0}
        b._yf = yf
        return b

    def test_one_download_covers_all_tickers_plus_spy(self, bt):
        bt._prefetch_backtest_prices({
            "t1": [{"ticker": "NVDA", "date": "2026-08-10"}, {"ticker": "TSLA", "date": "2026-08-10"}],
            "t7": [{"ticker": "NVDA", "date": "2026-07-20"}],
            "t30": [],
        })
        bt._yf.download.assert_called_once()
        kw = bt._yf.download.call_args.kwargs
        assert kw["tickers"] == ["NVDA", "SPY", "TSLA"], "SPY 必须带上：t7 基准要它"
        assert kw["start"] == "2026-07-20", "窗口起点 = 最早预测日"
        assert kw["end"] == "2026-08-31", "窗口终点 = 今天 + 11（覆盖 _get_price_at_date 的 +10）"
        assert kw["group_by"] == "ticker" and kw["auto_adjust"] is True and kw["threads"] is False
        assert set(bt._ohlc_cache) == {"NVDA", "SPY", "TSLA"}
        assert bt._ohlc_window == (date(2026, 7, 20), date(2026, 8, 31))
        assert bt._ohlc_stats["batch_downloads"] == 1 and bt._ohlc_stats["batch_tickers"] == 3

    def test_download_failure_degrades_silently(self, bt):
        bt._yf.download.side_effect = OSError("429")
        bt._prefetch_backtest_prices({"t1": [{"ticker": "NVDA", "date": "2026-08-10"}]})
        assert bt._ohlc_cache == {} and bt._ohlc_window is None
        assert bt._ohlc_stats["batch_downloads"] == 0

    def test_empty_frame_degrades(self, bt):
        bt._yf.download.return_value = pd.DataFrame()
        bt._prefetch_backtest_prices({"t1": [{"ticker": "NVDA", "date": "2026-08-10"}]})
        assert bt._ohlc_window is None

    def test_no_pending_no_download(self, bt):
        bt._prefetch_backtest_prices({"t1": [], "t7": [], "t30": []})
        bt._yf.download.assert_not_called()


# ───────────────────────────────────────────── 5. 等价性：批量 vs 逐票
class _FakeStore:
    def __init__(self, pending):
        self._pending = pending
        self.calls = []

    def get_pending_checks(self, period):
        return list(self._pending.get(period, []))

    def update_check_result(self, pid, period, price, ret, correct, ambiguous=False):
        self.calls.append(("check", pid, period, round(price, 6), ret, correct, ambiguous))

    def update_t7_path_result(self, **kw):
        kw = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in kw.items()}
        kw.pop("cost_breakdown", None)
        self.calls.append(("t7", tuple(sorted(kw.items()))))


FRAMES = {
    "NVDA": _ohlc("2026-07-01", 36, 100, 1.0),      # 一路上涨
    "TSLA": _ohlc("2026-07-01", 36, 300, -1.0),     # 一路下跌
    "SPY": _ohlc("2026-07-01", 36, 500, 0.2),
}


def _close_on(t, d):
    return float(_ohlc_at(t, d)["Close"])


def _ohlc_at(t, d):
    df = FRAMES[t]
    return df[df.index.date == datetime.strptime(d, "%Y-%m-%d").date()].iloc[0]


def _pending():
    return {
        "t1": [
            {"id": 1, "ticker": "NVDA", "date": "2026-08-10", "direction": "bullish",
             "price_at_predict": _close_on("NVDA", "2026-08-10"), "iv_rank": None},
            {"id": 2, "ticker": "TSLA", "date": "2026-08-10", "direction": "bullish",
             "price_at_predict": _close_on("TSLA", "2026-08-10"), "iv_rank": None},
        ],
        "t7": [
            {"id": 3, "ticker": "NVDA", "date": "2026-07-20", "direction": "bullish",
             "price_at_predict": _close_on("NVDA", "2026-07-20")},
            {"id": 4, "ticker": "TSLA", "date": "2026-07-20", "direction": "bearish",
             "price_at_predict": _close_on("TSLA", "2026-07-20")},
        ],
        "t30": [
            {"id": 5, "ticker": "NVDA", "date": "2026-07-01", "direction": "bearish",
             "price_at_predict": _close_on("NVDA", "2026-07-01")},
        ],
    }


def _run(monkeypatch, *, batch_ok: bool):
    yf = MagicMock()
    if batch_ok:
        yf.download.return_value = _multi(FRAMES)
        yf.Ticker.side_effect = AssertionError("批量路径下不许逐票取价")
    else:
        yf.download.side_effect = OSError("download down")

        def _ticker(t):
            tk = MagicMock()
            tk.history.side_effect = lambda start, end: B._slice_by_date(
                FRAMES[t], datetime.strptime(start, "%Y-%m-%d").date(),
                datetime.strptime(end, "%Y-%m-%d").date())
            return tk
        yf.Ticker.side_effect = _ticker
    monkeypatch.setattr(B, "yf", yf)
    bt = B.Backtester.__new__(B.Backtester)
    bt.store = _FakeStore(_pending())
    bt._spy_entry_cache = {}
    bt._ohlc_cache = {}; bt._ohlc_window = None
    bt._ohlc_stats = {"batch_downloads": 0, "batch_tickers": 0, "cache_hits": 0, "fallback_history": 0}
    results = bt.run_backtest()
    return results, bt.store.calls, yf, bt._ohlc_stats


class TestRunBacktestEquivalence:
    def test_batch_and_per_ticker_paths_write_identical_results(self, monkeypatch):
        res_a, calls_a, yf_a, st_a = _run(monkeypatch, batch_ok=True)
        res_b, calls_b, yf_b, st_b = _run(monkeypatch, batch_ok=False)

        assert calls_a, "夹具没让任何预测被评分，测试自己是空的"
        assert res_a == res_b
        assert calls_a == calls_b, "批量与逐票写库结果必须逐字段相同"

        # 批量路径：download 1 次、Ticker 0 次
        assert yf_a.download.call_count == 1
        assert st_a["batch_downloads"] == 1 and st_a["fallback_history"] == 0
        assert st_a["cache_hits"] >= 5
        # 逐票路径：download 尝试 1 次失败，Ticker.history ≥ 5 次
        assert yf_b.download.call_count == 1
        assert st_b["batch_downloads"] == 0 and st_b["cache_hits"] == 0
        assert st_b["fallback_history"] >= 5

    def test_every_period_actually_scored(self, monkeypatch):
        res, calls, _, _ = _run(monkeypatch, batch_ok=True)
        for p in ("t1", "t7", "t30"):
            assert res[p]["checked"] + res[p].get("ambiguous", 0) >= 1, f"{p} 没评到任何一条"
        assert any(c[0] == "t7" for c in calls) and any(c[0] == "check" for c in calls)
