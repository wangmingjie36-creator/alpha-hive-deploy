"""日线完整性校验：缺交易日 ⇒ 重取一次，仍缺 ⇒ iv_rank 置空并计数（v0.45.383）。

背景（2026-09-23）：yfinance 的 1y 日线缺了 09-22 那根，8 只标的的 `iv_rank` 静默错位（DE 差近 10 点），
BRK-B 的 `rv_30d` 也是同一根。`fetch_historical_hv` 返回的 HV 列表不带日期，少一根 bar 窗口整体后移一天，
产出的仍是「合理的数字」，没有任何东西会红。

全离线：yfinance 由 `_FakeYF` 顶替；日期全部相对「真实今天」构造（`_recent_sessions`），
所以本文件不是时间炸弹——不写死任何会过期的日期（少数写死日期的用例只测纯函数、与时钟无关）。
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import pytest

import bars_integrity as bi
import options_analyzer as oa
from options_analyzer import OptionsDataFetcher


# ── 构造工具 ────────────────────────────────────────────────────────────────

def _recent_sessions(n: int) -> list:
    """截至「上一个已完成交易日」的最近 n 个交易日（升序）。相对真实今天，不写死日期。"""
    end = bi.previous_completed_session()
    out, d = [], end
    while len(out) < n:
        if d not in bi.AD_HOC_CLOSURES and bi.trading_days(d, d):
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def _frame(dates: list, seed: int = 1) -> pd.DataFrame:
    """确定性的随机游走收盘价，DatetimeIndex 与 yfinance 日线同形。"""
    import random
    rng = random.Random(seed)
    px, closes = 100.0, []
    for _ in dates:
        px *= 1 + rng.uniform(-0.03, 0.03)
        closes.append(px)
    return pd.DataFrame({"Close": closes}, index=pd.to_datetime([d.isoformat() for d in dates]))


def _drop(frame: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    return frame[frame.index.date != day]


def _reference_hv(frame: pd.DataFrame) -> list:
    """旧实现的公式原样抄一遍（20 日滚动、×100×√252、去 NaN、取尾 252）——正对照的基准。"""
    r = frame["Close"].pct_change().dropna()
    return (r.rolling(window=20).std() * 100 * (252 ** 0.5)).dropna().tolist()[-252:]


class _FakeYF:
    """顶替 `options_analyzer.yf`。`frames` 按调用顺序给出；用尽后重复最后一个。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def Ticker(self, _t):          # noqa: N802 - 与 yfinance 同名
        outer = self

        class _T:
            def history(self, period="1y", **_k):
                f = outer.frames[min(outer.calls, len(outer.frames) - 1)]
                outer.calls += 1
                return f
        return _T()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    oa.reset_hv_gap_stats()
    import yf_gate
    monkeypatch.setattr(yf_gate, "ensure", lambda *a, **k: None)   # 不吃限流闸门的等待
    yield
    oa.reset_hv_gap_stats()


def _fetcher(tmp_path, monkeypatch, frames) -> tuple:
    f = OptionsDataFetcher(cache_dir=str(tmp_path))
    fake = _FakeYF(frames)
    monkeypatch.setattr(oa, "yf", fake)
    return f, fake


# ── ① 纯函数 ───────────────────────────────────────────────────────────────

class TestFindGaps:
    TODAY = dt.date(2026, 9, 30)      # 只用于纯函数：给定 today 后结果与时钟无关

    def _days(self, start, end, skip=()):
        return [d.isoformat() for d in bi.trading_days(dt.date.fromisoformat(start), dt.date.fromisoformat(end))
                if d.isoformat() not in skip]

    def test_clean_series_has_no_gaps(self):
        g = bi.find_gaps(self._days("2025-09-29", "2026-09-29"), today=self.TODAY)
        assert g == {"missing": [], "critical": [], "minor": []}

    def test_gap_in_last_21_bars_is_critical(self):
        """09-23 的原型：中间少一根、后面还有几根。"""
        g = bi.find_gaps(self._days("2025-09-29", "2026-09-29", skip=("2026-09-22",)), today=self.TODAY)
        assert g["critical"] == ["2026-09-22"] and g["minor"] == []

    def test_old_gap_is_minor_not_critical(self):
        g = bi.find_gaps(self._days("2025-09-29", "2026-09-29", skip=("2026-07-15",)), today=self.TODAY)
        assert g["critical"] == [] and g["minor"] == ["2026-07-15"]

    def test_critical_boundary_is_the_21st_bar_from_the_end(self):
        """20 个日收益需要 21 根 bar：倒数第 21 根缺 ⇒ 当前 HV 受影响 ⇒ critical；再往前一根不影响 ⇒ minor。"""
        days = self._days("2025-09-29", "2026-09-29")
        on_cut = bi.find_gaps([d for d in days if d != days[-21]], today=self.TODAY)
        assert on_cut["critical"] == [days[-21]] and on_cut["minor"] == []
        before = bi.find_gaps([d for d in days if d != days[-22]], today=self.TODAY)
        assert before["critical"] == [] and before["minor"] == [days[-22]]

    def test_tail_behind_last_completed_session_is_critical(self):
        days = self._days("2025-09-29", "2026-09-29")
        g = bi.find_gaps(days[:-1], today=self.TODAY)     # 少最后一个已完成交易日
        assert g["critical"] == [days[-1]]

    def test_todays_own_bar_is_not_required(self):
        """收盘后几分钟 Yahoo 还没发当日 bar 是常态——不能因此降级（降级会改分数）。"""
        days = self._days("2025-09-29", "2026-09-29")
        assert bi.find_gaps(days, today=dt.date(2026, 9, 30))["missing"] == []
        assert bi.find_gaps(days, today=dt.date(2026, 9, 30))["critical"] == []

    def test_weekends_and_rule_based_holidays_are_not_gaps(self):
        """2026-09-07 是劳动节（周一）。序列自然不含它 ⇒ 不是缺口。"""
        days = self._days("2026-08-24", "2026-09-29")
        assert "2026-09-07" not in days
        assert bi.find_gaps(days, today=self.TODAY)["missing"] == []

    def test_ad_hoc_closure_is_not_a_gap_but_is_if_table_is_empty(self, monkeypatch):
        """2025-01-09（卡特国葬日）：28/29 只真实标的都没有这天。规则历不知道它，表里得有。"""
        raw = [d.isoformat() for d in bi.trading_days(dt.date(2024, 12, 30), dt.date(2025, 1, 31))
               if d != dt.date(2025, 1, 9)]
        assert bi.find_gaps(raw, today=dt.date(2025, 2, 3))["missing"] == []
        monkeypatch.setattr(bi, "AD_HOC_CLOSURES", {})
        # 表清空后 bi.trading_days 把它当交易日 ⇒ 应报出缺口（证明上一条断言靠的是那张表）
        assert "2025-01-09" in bi.find_gaps(raw, today=dt.date(2025, 2, 3))["missing"]

    def test_empty_series_returns_empty_not_a_verdict(self):
        assert bi.find_gaps([], today=self.TODAY) == {"missing": [], "critical": [], "minor": []}


# ── ② fetch_historical_hv ─────────────────────────────────────────────────

class TestFetchHistoricalHv:
    def test_clean_series_is_bit_identical_to_the_old_formula(self, tmp_path, monkeypatch):
        """正对照：无缺口时输出与旧实现逐位相同——校验不得改变正常路径的任何数值。"""
        days = _recent_sessions(260)
        frame = _frame(days)
        f, fake = _fetcher(tmp_path, monkeypatch, [frame])
        hv = f.fetch_historical_hv("XYZ")
        assert hv == _reference_hv(frame)
        assert fake.calls == 1
        assert f.last_hist_hv_gaps == [] and not f.hist_hv_untrusted()
        st = oa.hv_gap_stats()
        assert (st["checked"], st["clean"], st["repaired"], st["degraded"]) == (1, 1, 0, 0)
        assert (tmp_path / "options_XYZ_hist_hv_v5.json").exists()

    def test_the_gap_really_changes_the_answer(self, tmp_path):
        """夹具自检：缺一根确实让当前 HV 变了——否则下面所有「缺口」用例都可能是空跑。"""
        days = _recent_sessions(260)
        frame = _frame(days)
        gapped = _drop(frame, days[-2])
        assert _reference_hv(frame)[-1] != _reference_hv(gapped)[-1]

    def test_gap_then_clean_on_retry_is_repaired(self, tmp_path, monkeypatch):
        days = _recent_sessions(260)
        good = _frame(days)
        f, fake = _fetcher(tmp_path, monkeypatch, [_drop(good, days[-2]), good])
        hv = f.fetch_historical_hv("XYZ")
        assert fake.calls == 2, "缺口应触发重取"
        assert hv == _reference_hv(good), "重取拿到完整数据后必须用完整数据"
        assert not f.hist_hv_untrusted()
        st = oa.hv_gap_stats()
        assert (st["repaired"], st["degraded"]) == (1, 0)
        assert (tmp_path / "options_XYZ_hist_hv_v5.json").exists()

    def test_persistent_gap_is_degraded_and_never_cached(self, tmp_path, monkeypatch):
        days = _recent_sessions(260)
        bad = _drop(_frame(days), days[-2])
        f, fake = _fetcher(tmp_path, monkeypatch, [bad])
        hv = f.fetch_historical_hv("XYZ")
        assert fake.calls == 2, "重取一次后仍缺才放弃——不能无限重试"
        assert f.last_hist_hv_gaps == [days[-2].isoformat()]
        assert f.hist_hv_untrusted()
        assert hv, "仍返回序列（`min(hist_hv)` 之类只要量级的用法不受影响）"
        assert not (tmp_path / "options_XYZ_hist_hv_v5.json").exists(), "缺口序列进了缓存 ⇒ 5 分钟内会被当好数据读走"
        st = oa.hv_gap_stats()
        assert st["degraded"] == 1 and st["tickers"]["XYZ"]["critical"] == [days[-2].isoformat()]
        # 下一次调用必须重新取，而不是命中缓存
        f.fetch_historical_hv("XYZ")
        assert fake.calls == 4

    def test_old_gap_is_minor_counted_but_not_degraded_and_not_retried(self, tmp_path, monkeypatch):
        days = _recent_sessions(260)
        frame = _drop(_frame(days), days[-100])
        f, fake = _fetcher(tmp_path, monkeypatch, [frame])
        f.fetch_historical_hv("XYZ")
        assert fake.calls == 1, "minor 不重取（只挪 ≤20 个历史 HV 点，不值得多花一次请求）"
        assert not f.hist_hv_untrusted()
        st = oa.hv_gap_stats()
        assert st["minor"] == 1 and st["degraded"] == 0 and st["tickers"]["XYZ"]["minor"] == [days[-100].isoformat()]
        assert (tmp_path / "options_XYZ_hist_hv_v5.json").exists()

    def test_cache_hit_does_not_reverify_or_recount(self, tmp_path, monkeypatch):
        days = _recent_sessions(260)
        f, fake = _fetcher(tmp_path, monkeypatch, [_frame(days)])
        a = f.fetch_historical_hv("XYZ")
        b = f.fetch_historical_hv("XYZ")
        assert a == b and fake.calls == 1
        assert oa.hv_gap_stats()["checked"] == 1

    def test_checker_crash_is_counted_and_logged_not_swallowed(self, tmp_path, monkeypatch, caplog):
        """校验器自己的 bug 不能把全部 iv_rank 置空，但也不能被悄悄当成「没缺口」。"""
        days = _recent_sessions(260)
        f, _ = _fetcher(tmp_path, monkeypatch, [_frame(days)])

        def _boom(*a, **k):
            raise RuntimeError("checker bug")
        monkeypatch.setattr(bi, "find_gaps", _boom)
        with caplog.at_level(logging.ERROR):
            hv = f.fetch_historical_hv("XYZ")
        assert hv and not f.hist_hv_untrusted()
        assert oa.hv_gap_stats()["check_errors"] == 1
        assert any("校验自身出错" in r.getMessage() for r in caplog.records)

    def test_empty_history_still_falls_back_to_sample_as_before(self, tmp_path, monkeypatch):
        f, _ = _fetcher(tmp_path, monkeypatch, [pd.DataFrame({"Close": []})])
        f.fetch_historical_hv("XYZ")
        assert f.last_hist_hv_is_sample and f.hist_hv_untrusted()
        assert f.last_hist_hv_gaps == []
        assert oa.hv_gap_stats()["checked"] == 0, "样本回落路径不是缺口，不该记进缺口计数"


# ── ③ analyze：缺口 ⇒ iv_rank / iv_percentile 置空，并说明缺的是哪天 ─────────

@pytest.fixture
def _offline_sources(stub_cboe_payload, stub_yfinance):
    """同 test_iv_history：analyze 的取数支路显式钉死。"""


class TestAnalyzeNullsRankOnGap:
    def _agent(self, tmp_path, monkeypatch, gaps):
        from options_analyzer import OptionsAgent
        agent = OptionsAgent()
        monkeypatch.setattr(agent.fetcher, "cache_dir", str(tmp_path))
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: {
            "calls": [{"strike": 100, "openInterest": 500, "impliedVolatility": 0.40, "gamma": 0.04, "volume": 100}],
            "puts": [{"strike": 95, "openInterest": 400, "impliedVolatility": 0.42, "gamma": 0.03, "volume": 80}],
            "expirations": ["2026-09-18"], "source": "real",
        })

        def _hv(t, days=252):
            agent.fetcher.last_hist_hv_gaps = list(gaps)
            return [20.0 + i for i in range(30)]
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv", _hv)
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda t, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda t: None)
        return agent

    def test_gap_nulls_rank_and_names_the_missing_day(self, tmp_path, monkeypatch, _offline_sources):
        r = self._agent(tmp_path, monkeypatch, ["2026-09-22"]).analyze("NVDA", stock_price=100.0)
        assert r["iv_rank_source"] == "hv_proxy", "不能新增取值：signal_archive 把非 hv_proxy 一律当成真实 IV"
        assert r["iv_rank"] is None and r["iv_percentile"] is None
        assert r["hv_gap"] == ["2026-09-22"]

    def test_no_gap_keeps_rank_control(self, tmp_path, monkeypatch, _offline_sources):
        r = self._agent(tmp_path, monkeypatch, []).analyze("NVDA", stock_price=100.0)
        assert r["iv_rank"] is not None and r["iv_percentile"] is not None
        assert r["hv_gap"] == []

    def test_gap_falls_to_the_neutral_iv_signal(self, tmp_path, monkeypatch, _offline_sources):
        """None 走既有的「中性 2.0，不奖不罚」分支——置空不能变成一个奇怪的分数。"""
        from options_analyzer import OptionsAnalyzer
        an = OptionsAnalyzer.__new__(OptionsAnalyzer)
        neutral, _ = an.generate_options_score(iv_rank=None, put_call_ratio=0.9, gex=0.0, unusual=[])
        mid, _ = an.generate_options_score(iv_rank=30.0, put_call_ratio=0.9, gex=0.0, unusual=[])  # 20–40 档 = 2.0
        low, _ = an.generate_options_score(iv_rank=10.0, put_call_ratio=0.9, gex=0.0, unusual=[])  # <20 档 = 1.0
        assert neutral == mid and neutral != low

    def test_real_iv_history_branch_is_not_nulled_by_gap_flag(self, tmp_path, monkeypatch, _offline_sources):
        """真实 IV 历史口径不依赖 hist_hv——缺口标志不该动它（v0.43.19 的既有约束）。"""
        import json
        from iv_history import IV_RANK_MIN_DAYS
        for i in range(IV_RANK_MIN_DAYS + 5):
            d = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
            (tmp_path / f"options_snapshot_NVDA_{d}.json").write_text(
                json.dumps({"ticker": "NVDA", "date": d, "iv_raw_observed": 20.0 + (i % 40)}), encoding="utf-8")
        r = self._agent(tmp_path, monkeypatch, ["2026-09-22"]).analyze("NVDA", stock_price=100.0)
        assert r["iv_rank_source"].startswith("real_iv_")
        assert r["iv_rank"] is not None
        assert r["hv_gap"] == [], "hv_gap 只在 hv_proxy 口径下有意义"


# ── ④ scan_timing 摘要：出过事才点名 ──────────────────────────────────────

class TestScanTimingSurface:
    def test_summary_names_degraded_ticker_and_day(self):
        import scan_timing
        line = scan_timing.summary_line({"phases": {}, "counters": {"hv_gap": {
            "checked": 30, "clean": 27, "repaired": 1, "degraded": 2, "minor": 0, "check_errors": 0,
            "tickers": {"DE": {"critical": ["2026-09-22"], "minor": []}}}}})
        assert "日线缺口 修复1/TD补0/置空2/校验出错0(DE:2026-09-22)" in line

    def test_summary_is_silent_when_nothing_happened(self):
        import scan_timing
        line = scan_timing.summary_line({"phases": {}, "counters": {"hv_gap": {
            "checked": 30, "clean": 30, "repaired": 0, "degraded": 0, "minor": 0, "check_errors": 0, "tickers": {}}}})
        assert "日线缺口" not in line

    def test_counters_expose_hv_gap_stats(self):
        import scan_timing
        oa.reset_hv_gap_stats()
        assert scan_timing.counters()["hv_gap"]["checked"] == 0
