"""v0.45.699：自攒索引回落的 momentum 窗口滞后要看得见——不管 CBOE 有没有核实收盘。

事故（2026-11-02，auto-memory `alpha-hive-fallback-not-reached.md` 同族）：14:31~14:33 CBOE 熔断期里处理的 13 只
（NVDA TSLA MSFT QCOM VKTX META BILI AMZN RKLB CRCL CVX VZ JNJ）没有核实收盘（`session_close is None`），
自攒索引窗口停在 10-30，其余 17 只（14:33:50 之后，CBOE 恢复）窗口到 11-02。旧的 `window_behind` 判据是
`session_close is not None and as_of != session_close[0]`——没有 session_close 就恒「没滞后」，`window_behind_session`
恒为 0、没有告警。重算后这 13 只与对齐窗口平均差 3.45 个点（RKLB 12.4、BILI 8.0），其余 17 只差 ≤ 0.17。

本文件：
  1. 两种滞后（没核实收盘 / 核实了却没进窗口）都进 `history_fill_stats()["window_behind"]`；
  2. 对齐、周末、日历判不了都不进（不猜）；
  3. 同一标的后一次出口对齐 ⇒ 清掉；
  4. 进 alert_manager P2；评分快照 `at_scoring` 里带着；干净时静默。
时钟：`yf_daily._now_et` 是 `latest_completed_session` 唯一读钟处，全部测试钉住它。
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import alert_manager as am
import data_pipeline as dp
import yf_daily

from tests.test_fetcher_single_exit import _Fills, _Src, _degraded, _fetcher


@pytest.fixture(autouse=True)
def _fresh_stats():
    dp.reset_history_fill_stats()
    yield
    dp.reset_history_fill_stats()


def _pin_now(monkeypatch, y, m, d, hh=16, mm=30):
    ts = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(yf_daily, "_now_et", lambda: ts)


MON_AFTER_CLOSE = (2026, 11, 2)       # 周一 16:30 ET：最近一场已收盘 = 11-02


def _fetch_unverified(monkeypatch, ticker, as_of):
    _Fills(monkeypatch, as_of=as_of)
    _fetcher([_Src("finnhub", _degraded("finnhub"))]).fetch(ticker)


class TestWindowBehindRecorded:
    def test_no_verified_close_and_window_one_session_behind(self, monkeypatch):
        """11-02 的形状：CBOE 没给核实收盘、窗口停在上周五。"""
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _fetch_unverified(monkeypatch, "NVDA", "2026-10-30")
        s = dp.history_fill_stats()
        assert s["window_behind"] == {"NVDA": {"as_of": "2026-10-30", "expected": "2026-11-02", "verified": False}}
        assert s["window_behind_session"] == 0, "旧计数看不见这一种——这正是它没报警的原因"

    def test_verified_close_that_did_not_enter_the_window(self, monkeypatch):
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _Fills(monkeypatch, as_of="2026-10-30")
        _fetcher([_Src("cboe", _degraded("cboe", session_close=("2026-11-02", 202.81)))]).fetch("NVDA")
        s = dp.history_fill_stats()
        assert s["window_behind"]["NVDA"] == {"as_of": "2026-10-30", "expected": "2026-11-02", "verified": True}
        assert s["window_behind_session"] == 1

    def test_aligned_window_not_recorded(self, monkeypatch):
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _fetch_unverified(monkeypatch, "XOM", "2026-11-02")
        assert dp.history_fill_stats()["window_behind"] == {}

    def test_weekend_scan_window_ending_friday_is_not_behind(self, monkeypatch):
        """周日补跑：最近一场已收盘 = 周五 10-30，窗口到 10-30 就是对齐的。"""
        _pin_now(monkeypatch, 2026, 11, 1, 12, 0)
        _fetch_unverified(monkeypatch, "NVDA", "2026-10-30")
        assert dp.history_fill_stats()["window_behind"] == {}

    def test_before_the_close_expected_is_previous_session(self, monkeypatch):
        """周一 15:00 ET 还没收盘：应有场次 = 10-30，窗口到 10-30 不算滞后。"""
        _pin_now(monkeypatch, 2026, 11, 2, 15, 0)
        _fetch_unverified(monkeypatch, "NVDA", "2026-10-30")
        assert dp.history_fill_stats()["window_behind"] == {}

    def test_calendar_failure_is_not_guessed(self, monkeypatch):
        monkeypatch.setattr(yf_daily, "latest_completed_session", lambda now=None: None)
        _fetch_unverified(monkeypatch, "NVDA", "2026-10-30")
        assert dp.history_fill_stats()["window_behind"] == {}

    def test_momentum_from_yfinance_is_not_judged(self, monkeypatch):
        """momentum 不是回落给的（`momentum_source` 不是 price_index）⇒ 不判。"""
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _Fills(monkeypatch, as_of="2026-10-30")
        _fetcher([_Src("finnhub", _degraded("finnhub", momentum_5d=1.5))]).fetch("NVDA")
        assert dp.history_fill_stats()["window_behind"] == {}

    def test_later_aligned_exit_clears_the_ticker(self, monkeypatch):
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _fetch_unverified(monkeypatch, "NVDA", "2026-10-30")
        _fetch_unverified(monkeypatch, "TSLA", "2026-10-30")
        _fetch_unverified(monkeypatch, "NVDA", "2026-11-02")
        assert sorted(dp.history_fill_stats()["window_behind"]) == ["TSLA"]

    def test_reset_clears(self, monkeypatch):
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _fetch_unverified(monkeypatch, "NVDA", "2026-10-30")
        dp.reset_history_fill_stats()
        _fetch_unverified(monkeypatch, "TSLA", "2026-11-02")
        assert dp.history_fill_stats()["window_behind"] == {}

    def test_scoring_snapshot_freezes_the_set(self, monkeypatch):
        """评分之后的重取改不了评分那一刻的集合（同 v0.45.625 的 still_missing 口径）。"""
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _fetch_unverified(monkeypatch, "NVDA", "2026-10-30")
        dp.mark_history_fill_scoring()
        _fetch_unverified(monkeypatch, "NVDA", "2026-11-02")
        s = dp.history_fill_stats()
        assert s["window_behind"] == {}
        assert list(s["at_scoring"]["window_behind"]) == ["NVDA"]

    def test_empty_snapshot_template_has_the_key(self):
        """没有解析过任何标的就拍快照：模板也带这个键（读者按键取）。"""
        dp.mark_history_fill_scoring()
        dp._record_history_fill("X", "p", {}, False)
        assert dp.history_fill_stats()["at_scoring"]["window_behind"] == {}


def _analyze(tmp_path, counter):
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": {"history_fill": counter}, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return [x for x in a.alerts if "动量窗口滞后" in x.message]


class TestAlert:
    def test_raises_p2_naming_the_tickers(self, monkeypatch, tmp_path):
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        for t in ("NVDA", "JNJ"):
            _fetch_unverified(monkeypatch, t, "2026-10-30")
        _fetch_unverified(monkeypatch, "XOM", "2026-11-02")
        dp.mark_history_fill_scoring()
        (hit,) = _analyze(tmp_path, dp.history_fill_stats())
        assert hit.level == am.AlertLevel.MEDIUM and "2 只" in hit.message
        assert "NVDA" in hit.details["窗口滞后"] and "JNJ" in hit.details["窗口滞后"]
        assert "XOM" not in hit.details["窗口滞后"]
        assert "2026-10-30 → 应有 2026-11-02" in hit.details["窗口末端"]
        assert "NVDA" in hit.details["其中没核实收盘"] and hit.details["其中核实了收盘却没进窗口"] == "无"

    def test_uses_the_scoring_snapshot_not_later_refetch(self, monkeypatch, tmp_path):
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _fetch_unverified(monkeypatch, "NVDA", "2026-10-30")
        dp.mark_history_fill_scoring()
        _fetch_unverified(monkeypatch, "NVDA", "2026-11-02")        # 评分之后对齐了
        (hit,) = _analyze(tmp_path, dp.history_fill_stats())
        assert "NVDA" in hit.details["窗口滞后"], "评分用的是滞后那份"

    def test_clean_is_silent(self, monkeypatch, tmp_path):
        _pin_now(monkeypatch, *MON_AFTER_CLOSE)
        _fetch_unverified(monkeypatch, "XOM", "2026-11-02")
        dp.mark_history_fill_scoring()
        assert _analyze(tmp_path, dp.history_fill_stats()) == []

    def test_old_counter_without_the_key_is_silent(self, tmp_path):
        old = {"resolutions": 1, "by_path": {}, "window_behind_session": 0,
               "still_missing": {"momentum": [], "volume": [], "volatility": []}}
        assert _analyze(tmp_path, old) == []
