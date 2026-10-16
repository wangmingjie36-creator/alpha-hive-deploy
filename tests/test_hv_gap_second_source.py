"""日线缺口阶段 2：重取仍缺 ⇒ 向 Twelve Data 借缺的那一根，换算回复权口径（v0.45.387）。

为什么不整段换成 Twelve Data：yfinance 是**复权**收盘、Twelve Data 是**未复权**收盘，分红标的上 HV Rank 会差
1~3 点（2026-09-30 实测 ABBV：复权 23.42 / 未复权 21.65）。所以只借缺的那一根，并按邻居 bar 的
（复权价 / 第二源收盘）因子换算；两侧因子不一致 ⇒ 缺口夹着除息日 ⇒ 放弃，退回 v0.45.383 的置空。

本文件的夹具**自带分红**：yfinance 帧 = 未复权价 × 因子（除息日之前），Twelve Data 行 = 未复权价。
补出来的 HV 必须与「完整的复权序列」逐位相等——不带分红的夹具证明不了换算对不对
（因子恒为 1 时，乘不乘都一样）。

全离线：yfinance 由 `_FakeYF` 顶替，Twelve Data 由 monkeypatch `twelve_data.fetch_bars` 顶替。
日期相对真实今天构造，不是时间炸弹。
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import pytest

import bars_integrity as bi
import options_analyzer as oa
from options_analyzer import OptionsDataFetcher
from tests.test_hv_gap_integrity import _FakeYF, _drop, _frame, _recent_sessions, _reference_hv

DIV = 0.99          # 除息日之前的复权因子（1% 分红）


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    oa.reset_hv_gap_stats()
    import yf_gate
    monkeypatch.setattr(yf_gate, "ensure", lambda *a, **k: None)
    yield
    oa.reset_hv_gap_stats()


class _FakeTD:
    """顶替 `twelve_data.fetch_bars`：按未复权帧给行，记录被调用的参数。"""

    def __init__(self, monkeypatch, unadjusted: pd.DataFrame, *, missing=(), result="rows"):
        import twelve_data
        self.calls = []
        self.frame = unadjusted
        self.missing = {m.isoformat() for m in missing}
        self.result = result
        monkeypatch.setattr(twelve_data, "fetch_bars", self)

    def __call__(self, ticker, days=120, end_date=None):
        self.calls.append((ticker, days, end_date))
        if self.result == "none":
            return None
        if self.result == "raise":
            raise RuntimeError("boom")
        return [{"date": d.strftime("%Y-%m-%d"), "close": float(c), "vol": 1.0}
                for d, c in zip(self.frame.index, self.frame["Close"]) if d.strftime("%Y-%m-%d") not in self.missing][-days:]


def _adjusted(unadj: pd.DataFrame, ex_div_idx: int) -> pd.DataFrame:
    """yfinance 口径：除息日之前的收盘乘 DIV，除息日及之后原样。"""
    adj = unadj.copy()
    adj.iloc[:ex_div_idx, adj.columns.get_loc("Close")] *= DIV
    return adj


def _setup(tmp_path, monkeypatch, yf_frames, unadj, *, td_missing=(), td_result="rows"):
    f = OptionsDataFetcher(cache_dir=str(tmp_path))
    fake_yf = _FakeYF(yf_frames)
    monkeypatch.setattr(oa, "yf", fake_yf)
    fake_td = _FakeTD(monkeypatch, unadj, missing=td_missing, result=td_result)
    return f, fake_yf, fake_td


# ── ① 纯函数 ───────────────────────────────────────────────────────────────

def _series(n=30, seed=3):
    days = [d.isoformat() for d in _recent_sessions(n)]
    u = _frame([dt.date.fromisoformat(d) for d in days], seed=seed)["Close"].tolist()
    return days, u


def _rows(days, u):
    return [{"date": d, "close": c, "vol": 1.0} for d, c in zip(days, u)]


class TestFillGapsFromReference:
    def test_interior_gap_is_filled_in_adjusted_units(self):
        days, u = _series()
        adj = [c * DIV if i < 25 else c for i, c in enumerate(u)]      # 除息日在缺口之后
        k = 20
        r = bi.fill_gaps_from_reference(days[:k] + days[k + 1:], adj[:k] + adj[k + 1:], _rows(days, u), [days[k]])
        assert r["filled"] == [days[k]] and r["unfilled"] == {}
        assert r["closes"][r["dates"].index(days[k])] == pytest.approx(adj[k], rel=1e-12)
        assert r["dates"] == days, "补出的 bar 必须插回原位、序列仍升序"

    def test_gap_on_the_ex_dividend_day_is_refused(self):
        """缺口正好夹着除息日：前一根因子 DIV、后一根因子 1 ⇒ 换算没有根据，宁可缺也不编。"""
        days, u = _series()
        adj = [c * DIV if i < 20 else c for i, c in enumerate(u)]      # 除息日 = 缺口那天
        k = 20
        r = bi.fill_gaps_from_reference(days[:k] + days[k + 1:], adj[:k] + adj[k + 1:], _rows(days, u), [days[k]])
        assert r["filled"] == [] and r["unfilled"] == {days[k]: "ratio_mismatch"}
        assert len(r["dates"]) == len(days) - 1, "没补成就不许往序列里塞东西"

    def test_vendor_noise_below_tolerance_still_fills(self):
        days, u = _series()
        ref = _rows(days, u)
        ref[21]["close"] *= 1 + bi.FILL_RATIO_TOL / 2                   # 后一根的两源差半个容差
        k = 20
        r = bi.fill_gaps_from_reference(days[:k] + days[k + 1:], u[:k] + u[k + 1:], ref, [days[k]])
        assert r["filled"] == [days[k]]

    def test_vendor_noise_above_tolerance_is_refused(self):
        days, u = _series()
        ref = _rows(days, u)
        ref[21]["close"] *= 1 + bi.FILL_RATIO_TOL * 3
        k = 20
        r = bi.fill_gaps_from_reference(days[:k] + days[k + 1:], u[:k] + u[k + 1:], ref, [days[k]])
        assert r["unfilled"] == {days[k]: "ratio_mismatch"}

    def test_single_tail_gap_uses_factor_one_even_with_pending_dividend(self):
        """末根复权价 ≡ 未复权价。除息日就是缺的那天时，前一根因子是 DIV，但缺的那根因子是 1。"""
        days, u = _series()
        adj = [c * DIV if i < len(u) - 1 else c for i, c in enumerate(u)]
        r = bi.fill_gaps_from_reference(days[:-1], adj[:-1], _rows(days, u), [days[-1]])
        assert r["filled"] == [days[-1]]
        assert r["closes"][-1] == pytest.approx(adj[-1], rel=1e-12)

    def test_two_tail_gaps_are_refused_when_a_dividend_is_pending(self):
        days, u = _series()
        adj = [c * DIV if i < len(u) - 2 else c for i, c in enumerate(u)]
        r = bi.fill_gaps_from_reference(days[:-2], adj[:-2], _rows(days, u), days[-2:])
        assert r["filled"] == [] and set(r["unfilled"].values()) == {"tail_ambiguous"}

    def test_two_tail_gaps_are_filled_when_no_dividend_is_pending(self):
        days, u = _series()
        r = bi.fill_gaps_from_reference(days[:-2], u[:-2], _rows(days, u), days[-2:])
        assert r["filled"] == days[-2:]

    def test_adjacent_interior_gaps_both_filled(self):
        days, u = _series()
        adj = [c * DIV if i < 25 else c for i, c in enumerate(u)]
        keep = [i for i in range(len(days)) if i not in (18, 19)]
        r = bi.fill_gaps_from_reference([days[i] for i in keep], [adj[i] for i in keep],
                                        _rows(days, u), [days[18], days[19]])
        assert r["filled"] == [days[18], days[19]]
        assert r["closes"] == pytest.approx(adj, rel=1e-12)

    def test_reference_without_that_day_is_refused(self):
        days, u = _series()
        ref = [x for x in _rows(days, u) if x["date"] != days[20]]
        r = bi.fill_gaps_from_reference(days[:20] + days[21:], u[:20] + u[21:], ref, [days[20]])
        assert r["unfilled"] == {days[20]: "ref_missing"}

    def test_neighbor_too_far_away_is_refused(self):
        days, u = _series()
        ref = [x for x in _rows(days, u) if x["date"] not in set(days[8:20])]      # 12 个交易日 ≈ 17 个日历日 > 7
        r = bi.fill_gaps_from_reference(days[:20] + days[21:], u[:20] + u[21:], ref, [days[20]])
        assert r["unfilled"] == {days[20]: "no_prev_neighbor"}

    def test_later_bar_present_but_missing_in_reference_is_refused(self):
        """后面有 bar、第二源却一根都没有 ⇒ 验不了因子，不能按末端处理（那会把因子当 1）。"""
        days, u = _series()
        ref = [x for x in _rows(days, u) if x["date"] <= days[20]]
        r = bi.fill_gaps_from_reference(days[:20] + days[21:], u[:20] + u[21:], ref, [days[20]])
        assert r["unfilled"] == {days[20]: "no_next_neighbor"}

    def test_nearest_later_neighbor_with_reference_is_used_and_still_catches_dividends(self):
        days, u = _series()
        ref = [x for x in _rows(days, u) if x["date"] != days[21]]           # 紧邻的后一根参考源没有
        k = 20
        plain = bi.fill_gaps_from_reference(days[:k] + days[k + 1:], u[:k] + u[k + 1:], ref, [days[k]])
        assert plain["filled"] == [days[k]], "改用再后面最近的一根验因子"
        adj = [c * DIV if i < 22 else c for i, c in enumerate(u)]            # 除息日落在缺口与「再后面那根」之间
        r = bi.fill_gaps_from_reference(days[:k] + days[k + 1:], adj[:k] + adj[k + 1:], ref, [days[k]])
        assert r["unfilled"] == {days[k]: "ratio_mismatch"}

    def test_present_days_are_ignored_and_inputs_not_mutated(self):
        days, u = _series()
        d0, c0 = list(days), list(u)
        r = bi.fill_gaps_from_reference(days, u, _rows(days, u), [days[5]])
        assert r["filled"] == [] and r["unfilled"] == {}
        assert days == d0 and u == c0

    def test_garbage_reference_rows_are_skipped_not_crashed(self):
        days, u = _series()
        ref = _rows(days, u) + [{"date": "2026-01-01"}, {"date": "2026-01-02", "close": "x"},
                                {"date": "2026-01-03", "close": float("nan")}, {"date": "2026-01-04", "close": -1}]
        k = 20
        r = bi.fill_gaps_from_reference(days[:k] + days[k + 1:], u[:k] + u[k + 1:], ref, [days[k]])
        assert r["filled"] == [days[k]]


# ── ② fetch_historical_hv 集成 ─────────────────────────────────────────────

class TestFetchHistoricalHvSecondSource:
    def _world(self, n=260, seed=1, ex_div_from_end=5):
        days = _recent_sessions(n)
        unadj = _frame(days, seed=seed)
        adj = _adjusted(unadj, len(days) - ex_div_from_end)
        return days, unadj, adj

    def test_interior_gap_is_filled_and_hv_equals_the_complete_adjusted_series(self, tmp_path, monkeypatch):
        days, unadj, adj = self._world()
        f, fake_yf, fake_td = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10])], unadj)
        hv = f.fetch_historical_hv("XYZ")
        assert hv == pytest.approx(_reference_hv(adj), rel=1e-9), "补出的 HV 必须与完整复权序列一致（分红换算对了才成立）"
        assert not f.hist_hv_untrusted() and f.last_hist_hv_gaps == []
        assert f.last_hist_hv_filled == [days[-10].isoformat()]
        st = oa.hv_gap_stats()
        assert (st["filled"], st["degraded"], st["repaired"], st["clean"]) == (1, 0, 0, 0)
        assert st["tickers"]["XYZ"]["filled"] == [days[-10].isoformat()]
        assert fake_yf.calls == 2, "先重取一次，仍缺才借第二源"
        assert fake_td.calls == [("XYZ", 120, None)], "用 fetch_bars 的共享窗口，才能命中扫描里已有的缓存、不多花配额"
        assert (tmp_path / "options_XYZ_hist_hv_v5.json").exists(), "补齐并复核过的序列可以缓存"

    def test_without_the_conversion_the_answer_would_be_wrong(self, tmp_path, monkeypatch):
        """反证：若把未复权的收盘直接塞进去，HV 会偏——证明本文件的夹具真在考换算。"""
        days, unadj, adj = self._world()
        wrong = _drop(adj, days[-10]).copy()
        wrong.loc[pd.Timestamp(days[-10].isoformat()), "Close"] = float(unadj.loc[pd.Timestamp(days[-10].isoformat()), "Close"])
        wrong = wrong.sort_index()
        assert _reference_hv(wrong) != pytest.approx(_reference_hv(adj), rel=1e-6)

    def test_gap_on_the_ex_dividend_day_falls_back_to_null(self, tmp_path, monkeypatch):
        days, unadj, adj = self._world(ex_div_from_end=10)            # 除息日 = days[-10] = 缺的那天
        f, _, _ = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10])], unadj)
        f.fetch_historical_hv("XYZ")
        assert f.hist_hv_untrusted() and f.last_hist_hv_gaps == [days[-10].isoformat()]
        assert f.last_hist_hv_filled == []
        st = oa.hv_gap_stats()
        assert (st["filled"], st["degraded"]) == (0, 1)
        assert st["tickers"]["XYZ"]["unfilled"] == [f"{days[-10].isoformat()}:ratio_mismatch"], "没补成要说清为什么"
        assert not (tmp_path / "options_XYZ_hist_hv_v5.json").exists()

    def test_tail_gap_is_filled(self, tmp_path, monkeypatch):
        days, unadj, adj = self._world(ex_div_from_end=1)            # 除息日就是缺的最后一根
        f, _, _ = _setup(tmp_path, monkeypatch, [_drop(adj, days[-1])], unadj)
        hv = f.fetch_historical_hv("XYZ")
        assert hv == pytest.approx(_reference_hv(adj), rel=1e-9)
        assert oa.hv_gap_stats()["filled"] == 1

    def test_second_source_unavailable_degrades_and_says_so(self, tmp_path, monkeypatch):
        days, unadj, adj = self._world()
        f, _, _ = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10])], unadj, td_result="none")
        f.fetch_historical_hv("XYZ")
        assert f.hist_hv_untrusted()
        assert oa.hv_gap_stats()["tickers"]["XYZ"]["unfilled"] == [f"{days[-10].isoformat()}:td_unavailable"]

    def test_transient_network_failure_is_retried_once(self, tmp_path, monkeypatch):
        """2026-09-29 扫描 Twelve Data 4/35 次真实请求是瞬时网络错误；补数路径重试一次才不至于白白退回置空。"""
        import twelve_data
        days, unadj, adj = self._world()
        f, _, fake_td = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10])], unadj)
        real, state = fake_td.__call__, {"n": 0}

        def flaky(ticker, days_=120, end_date=None):
            state["n"] += 1
            return None if state["n"] == 1 else real(ticker, days_, end_date)
        monkeypatch.setattr(twelve_data, "fetch_bars", flaky)
        monkeypatch.setattr(twelve_data, "bars_cache_stats", lambda: {"failed": {"XYZ": "network:URLError"}})
        f.fetch_historical_hv("XYZ")
        assert state["n"] == 2 and f.last_hist_hv_filled == [days[-10].isoformat()]

    def test_non_network_failures_are_not_retried(self, tmp_path, monkeypatch):
        import twelve_data
        days, unadj, adj = self._world()
        f, _, _ = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10])], unadj)
        state = {"n": 0}

        def limited(ticker, days_=120, end_date=None):
            state["n"] += 1
            return None
        monkeypatch.setattr(twelve_data, "fetch_bars", limited)
        monkeypatch.setattr(twelve_data, "bars_cache_stats", lambda: {"failed": {"XYZ": "api_error:429"}})
        f.fetch_historical_hv("XYZ")
        assert state["n"] == 1, "限流 / 404 重试只会再吃一个限流名额"
        assert f.hist_hv_untrusted()

    def test_second_source_crash_degrades_with_error_log(self, tmp_path, monkeypatch, caplog):
        days, unadj, adj = self._world()
        f, _, _ = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10])], unadj, td_result="raise")
        with caplog.at_level(logging.ERROR):
            f.fetch_historical_hv("XYZ")
        assert f.hist_hv_untrusted(), "补数自身出错不能让缺口序列冒充好数据"
        assert any(r.levelno >= logging.ERROR and "RuntimeError" in r.getMessage() for r in caplog.records)
        assert oa.hv_gap_stats()["tickers"]["XYZ"]["unfilled"][0].endswith("fill_error:RuntimeError")

    def test_partial_fill_is_all_or_nothing(self, tmp_path, monkeypatch):
        """两根缺口只补得出一根 ⇒ 序列仍有缺口 ⇒ 整体退回置空，不许拿半补的序列算 HV。"""
        days, unadj, adj = self._world()
        two_gaps = _drop(_drop(adj, days[-10]), days[-3])
        f, _, _ = _setup(tmp_path, monkeypatch, [two_gaps], unadj, td_missing=[days[-3]])
        f.fetch_historical_hv("XYZ")
        assert f.hist_hv_untrusted()
        st = oa.hv_gap_stats()
        assert st["filled"] == 0 and st["degraded"] == 1
        assert f.last_hist_hv_filled == []

    def test_recheck_after_fill_failing_degrades(self, tmp_path, monkeypatch):
        """补完还要复核一遍：填充函数声称补好了、序列里却仍缺 ⇒ 不信，退回置空。"""
        days, unadj, adj = self._world()
        f, _, _ = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10])], unadj)
        monkeypatch.setattr(bi, "fill_gaps_from_reference", lambda d, c, r, t, **k: {
            "dates": list(d), "closes": list(c), "filled": list(t), "unfilled": {}})   # 谎称补好
        f.fetch_historical_hv("XYZ")
        assert f.hist_hv_untrusted()
        assert oa.hv_gap_stats()["tickers"]["XYZ"]["unfilled"] == [f"{days[-10].isoformat()}:recheck_failed"]

    def test_filled_list_does_not_leak_into_the_next_call(self, tmp_path, monkeypatch):
        days, unadj, adj = self._world()
        f, fake_yf, _ = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10]), _drop(adj, days[-10]), adj], unadj)
        f.fetch_historical_hv("XYZ")
        assert f.last_hist_hv_filled
        (tmp_path / "options_XYZ_hist_hv_v5.json").unlink()      # 绕开缓存，让第二次真取数
        f.fetch_historical_hv("XYZ")
        assert f.last_hist_hv_filled == [], "上一次补过的日期残留到下一次 ⇒ 下一次的结果会被误标成「有补数」"

    def test_clean_series_never_touches_the_second_source(self, tmp_path, monkeypatch):
        days, unadj, adj = self._world()
        f, _, fake_td = _setup(tmp_path, monkeypatch, [adj], unadj)
        f.fetch_historical_hv("XYZ")
        assert fake_td.calls == [], "没缺口就不该碰 Twelve Data（7 次/分钟的串行队列，扫描时间预算很紧）"
        assert oa.hv_gap_stats()["clean"] == 1

    def test_retry_that_repairs_does_not_touch_the_second_source(self, tmp_path, monkeypatch):
        days, unadj, adj = self._world()
        f, _, fake_td = _setup(tmp_path, monkeypatch, [_drop(adj, days[-10]), adj], unadj)
        f.fetch_historical_hv("XYZ")
        assert fake_td.calls == []
        assert oa.hv_gap_stats()["repaired"] == 1

    def test_nan_close_row_is_treated_as_a_gap_and_filled(self, tmp_path, monkeypatch):
        """2026-08-28 那种「日期在、Close 是 NaN」：`pct_change().dropna()` 会连后一根的收益一起丢掉，
        与缺一根 bar 同样错位，而 `find_gaps` 只看日期从前看不见。"""
        days, unadj, adj = self._world(ex_div_from_end=1)
        nan_frame = adj.copy()
        nan_frame.loc[pd.Timestamp(days[-8].isoformat()), "Close"] = float("nan")
        f, _, fake_td = _setup(tmp_path, monkeypatch, [nan_frame], unadj)
        hv = f.fetch_historical_hv("XYZ")
        assert hv == pytest.approx(_reference_hv(adj), rel=1e-9)
        assert f.last_hist_hv_filled == [days[-8].isoformat()] and fake_td.calls

    def test_nan_close_row_without_second_source_is_degraded_not_silent(self, tmp_path, monkeypatch):
        days, unadj, adj = self._world()
        nan_frame = adj.copy()
        nan_frame.loc[pd.Timestamp(days[-8].isoformat()), "Close"] = float("nan")
        f, _, _ = _setup(tmp_path, monkeypatch, [nan_frame], unadj, td_result="none")
        f.fetch_historical_hv("XYZ")
        assert f.hist_hv_untrusted() and f.last_hist_hv_gaps == [days[-8].isoformat()]


# ── ③ analyze 结果与摘要行 ─────────────────────────────────────────────────

@pytest.fixture
def _offline_sources(stub_cboe_payload, stub_yfinance):
    """同 test_hv_gap_integrity：analyze 的取数支路显式钉死。"""


class TestAnalyzeAndSummary:
    def _agent(self, tmp_path, monkeypatch, gaps, filled):
        from options_analyzer import OptionsAgent
        agent = OptionsAgent()
        monkeypatch.setattr(agent.fetcher, "cache_dir", str(tmp_path))
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: {
            "calls": [{"strike": 100, "openInterest": 500, "impliedVolatility": 0.40, "gamma": 0.04, "volume": 100}],
            "puts": [{"strike": 95, "openInterest": 400, "impliedVolatility": 0.42, "gamma": 0.03, "volume": 80}],
            "expirations": ["2026-09-18"], "source": "real"})

        def _hv(t, days=252):
            agent.fetcher.last_hist_hv_gaps = list(gaps)
            agent.fetcher.last_hist_hv_filled = list(filled)
            return [20.0 + i for i in range(30)]
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv", _hv)
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda t, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda t: None)
        return agent

    def test_filled_gap_keeps_rank_and_names_the_filled_day(self, tmp_path, monkeypatch, _offline_sources):
        r = self._agent(tmp_path, monkeypatch, [], ["2026-09-22"]).analyze("NVDA", stock_price=100.0)
        assert r["iv_rank"] is not None and r["iv_percentile"] is not None, "补齐了就不该置空"
        assert r["hv_gap"] == [] and r["hv_gap_filled"] == ["2026-09-22"]
        assert r["iv_rank_source"] == "hv_proxy", "仍不能新增取值：signal_archive 把非 hv_proxy 一律当成真实 IV"

    def test_no_fill_reports_empty_list(self, tmp_path, monkeypatch, _offline_sources):
        r = self._agent(tmp_path, monkeypatch, [], []).analyze("NVDA", stock_price=100.0)
        assert r["hv_gap_filled"] == []

    def test_summary_marks_filled_tickers_and_reasons_for_the_rest(self):
        import scan_timing
        line = scan_timing.summary_line({"phases": {}, "counters": {"hv_gap": {
            "checked": 30, "clean": 27, "repaired": 0, "filled": 1, "degraded": 2, "minor": 0, "check_errors": 0,
            "tickers": {"AMC": {"critical": ["2026-09-22"], "minor": [], "filled": ["2026-09-22"], "unfilled": []},
                        "DE": {"critical": ["2026-09-22"], "minor": [], "filled": [],
                               "unfilled": ["2026-09-22:td_unavailable"]}}}}})
        assert "TD补1/置空2" in line
        assert "AMC:2026-09-22已补" in line and "DE:2026-09-22[td_unavailable]" in line

    def test_summary_shows_up_when_only_a_fill_happened(self):
        import scan_timing
        line = scan_timing.summary_line({"phases": {}, "counters": {"hv_gap": {
            "checked": 30, "clean": 29, "repaired": 0, "filled": 1, "degraded": 0, "minor": 0, "check_errors": 0,
            "tickers": {"AMC": {"critical": ["2026-09-22"], "minor": [], "filled": ["2026-09-22"], "unfilled": []}}}}})
        assert "日线缺口" in line and "AMC:2026-09-22已补" in line
