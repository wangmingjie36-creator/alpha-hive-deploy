"""v0.45.717：扫描 yfinance 源末根早于「最近一场已收盘交易日」⇒ 本源当取不到，不拿前一场收盘当现价。

事故（2026-11-03，auto-memory `alpha-hive-environment-facts.md`）：冬令时后本机 14:00 = 美东 16:00，扫描在收盘那一刻开跑。
CBOE 29 只是盘中文件 ⇒ 交 yfinance；Yahoo 还没出 11-03 日线 ⇒ 末根是 11-02。`_drop_forming_bar` 只防「末根是今天且未收」，
`_completed_bar_close` 只证「末根已收」，都不问「是不是最近一场」⇒ 30 只的 `price_at_predict` 全等于 11-02 收盘（ENPH −8.2%），
`session_close` 也标成 11-02 ⇒ v0.45.699 的滞后判据看着「对齐」，没有任何告警。

本文件：
  1. 11-03 的形状被拒、交给下一个源；对齐 / 盘中 / 周末 / 提前收盘日照常；
  2. 交易所时钟判不了 ⇒ 放行（旧行为）但记下来；
  3. 不记熔断失败；按标的记最近一次；评分快照带着；
  4. alert_manager P2（拒给 / 判不了各一条），干净时静默，旧计数没这个键静默。
时钟：只读 `data_pipeline._exchange_now`（与 `_drop_forming_bar` 同一只），全部钉住。
"""

import datetime as dt
import json

import pandas as pd
import pytest

import alert_manager as am
import data_pipeline as dp
from bars_integrity import trading_days

from tests.test_fetcher_single_exit import _Fills, _Src, _degraded, _fetcher

D1102, D1103 = dt.date(2026, 11, 2), dt.date(2026, 11, 3)
EARLY = dt.date(2026, 11, 27)          # 感恩节次日，13:00 收


@pytest.fixture(autouse=True)
def _fresh_stats():
    dp.reset_history_fill_stats()
    yield
    dp.reset_history_fill_stats()


@pytest.fixture
def yf_frame(monkeypatch):
    import yfinance
    holder = {"frame": None}

    class _T:
        def __init__(self, *_a, **_k):
            pass

        def history(self, **_k):
            return holder["frame"]

    monkeypatch.setattr(yfinance, "Ticker", _T)
    return holder


def _xnow(monkeypatch, d, hh, mm):
    t = pd.Timestamp(dt.datetime.combine(d, dt.time(hh, mm)), tz="America/New_York")
    monkeypatch.setattr(dp, "_exchange_now", lambda: t)


def _frame(last_day, n=22):
    days = trading_days(last_day - dt.timedelta(days=60), last_day)[-n:]
    assert days[-1] == last_day
    closes = [100.0 + i * 0.7 + (i % 3) * 0.9 for i in range(n)]
    return pd.DataFrame({"Close": closes, "Volume": [1_000_000.0 + (i % 5) * 1e5 for i in range(n)]},
                        index=pd.DatetimeIndex(days).tz_localize("America/New_York"))


class TestYFinanceSource:
    def test_1103_shape_is_refused(self, monkeypatch, yf_frame):
        """11-03 16:00 ET（SPY 末根 15:59）、Yahoo 末根 11-02 ⇒ 不给。改前这里返回 11-02 收盘当现价。"""
        _xnow(monkeypatch, D1103, 15, 59)
        yf_frame["frame"] = _frame(D1102)
        assert dp.YFinanceSource().fetch("ENPH") is None
        dp._record_history_fill("ENPH", "probe", {}, False)
        assert dp.history_fill_stats()["yf_tail"] == {
            "ENPH": {"last_bar": "2026-11-02", "expected": "2026-11-03",
                     "reason": "latest_session_missing", "refused": True}}

    def test_aligned_bar_is_used(self, monkeypatch, yf_frame):
        _xnow(monkeypatch, D1103, 15, 59)
        hist = _frame(D1103)
        yf_frame["frame"] = hist
        data = dp.YFinanceSource().fetch("ENPH")
        assert data.price == float(hist["Close"].iloc[-1])
        assert data.session_close == ("2026-11-03", data.price)

    def test_intraday_previous_session_is_the_latest(self, monkeypatch, yf_frame):
        """盘中 11:00：形成中的 11-03 被丢掉、末根 11-02 = 最近一场已收盘 ⇒ 照用（不误拒）。"""
        _xnow(monkeypatch, D1103, 11, 0)
        yf_frame["frame"] = _frame(D1103)
        data = dp.YFinanceSource().fetch("ENPH")
        assert data is not None and data.price == float(_frame(D1103)["Close"].iloc[-2])

    def test_weekend_friday_bar_is_the_latest(self, monkeypatch, yf_frame):
        """周末补跑：SPY 末根停在周五 15:59，Yahoo 末根周五 ⇒ 照用。"""
        fri = dt.date(2026, 10, 30)
        _xnow(monkeypatch, fri, 15, 59)
        yf_frame["frame"] = _frame(fri)
        assert dp.YFinanceSource().fetch("ENPH") is not None

    @pytest.mark.parametrize("last, refused", [(EARLY, False), (dt.date(2026, 11, 25), True)])
    def test_early_close_day(self, monkeypatch, yf_frame, last, refused):
        """提前收盘日收盘后（SPY 末根 12:59）：最近一场 = 11-27；末根 11-25（缺 11-27）⇒ 拒。"""
        _xnow(monkeypatch, EARLY, 12, 59)
        yf_frame["frame"] = _frame(last)
        assert (dp.YFinanceSource().fetch("ENPH") is None) is refused

    def test_clock_unavailable_passes_but_is_recorded(self, monkeypatch, yf_frame):
        monkeypatch.setattr(dp, "_exchange_now", lambda: None)
        yf_frame["frame"] = _frame(D1102)
        assert dp.YFinanceSource().fetch("ENPH") is not None, "判不了 ⇒ 旧行为放行"
        dp._record_history_fill("ENPH", "probe", {}, False)
        tail = dp.history_fill_stats()["yf_tail"]["ENPH"]
        assert tail["refused"] is False and tail["reason"] == "calendar_unavailable"

    def test_refusal_does_not_trip_the_breaker(self, monkeypatch, yf_frame):
        """个别票晚出时，连撞 3 只不许把健康标的一起挡掉（同 CBOE 盘中陈旧文件的处置）。"""
        _xnow(monkeypatch, D1103, 15, 59)
        src = dp.YFinanceSource()
        yf_frame["frame"] = _frame(D1102)
        for t in ("A", "B", "C", "D", "E"):
            assert src.fetch(t) is None
        yf_frame["frame"] = _frame(D1103)
        assert src.fetch("F") is not None

    def test_later_aligned_fetch_clears_and_reset_clears(self, monkeypatch, yf_frame):
        _xnow(monkeypatch, D1103, 15, 59)
        src = dp.YFinanceSource()
        yf_frame["frame"] = _frame(D1102)
        src.fetch("ENPH")
        src.fetch("RKLB")
        yf_frame["frame"] = _frame(D1103)
        src.fetch("ENPH")
        dp._record_history_fill("X", "probe", {}, False)
        assert list(dp.history_fill_stats()["yf_tail"]) == ["RKLB"]
        dp.reset_history_fill_stats()
        dp._record_history_fill("X", "probe", {}, False)
        assert dp.history_fill_stats()["yf_tail"] == {}


class TestThroughTheFetcher:
    def test_next_source_supplies_the_price_not_the_previous_close(self, monkeypatch, yf_frame):
        """出口：yfinance 拒给 ⇒ 下一个源（这里是 Finnhub 桩）给价；改前出口是 source:yfinance、价 = 11-02 收盘。"""
        _xnow(monkeypatch, D1103, 15, 59)
        _Fills(monkeypatch, as_of="2026-11-03")
        stale = _frame(D1102)
        yf_frame["frame"] = stale
        fin = _Src("finnhub", _degraded("finnhub", price=30.42))
        out = _fetcher([dp.YFinanceSource(), fin]).fetch("ENPH")
        assert out["price"] == 30.42 and fin.calls == 1
        assert out["price"] != float(stale["Close"].iloc[-1])
        s = dp.history_fill_stats()
        assert "source:finnhub" in s["by_path"] and "source:yfinance" not in s["by_path"]
        assert s["yf_tail"]["ENPH"]["refused"] is True

    def test_scoring_snapshot_freezes_the_set(self, monkeypatch, yf_frame):
        _xnow(monkeypatch, D1103, 15, 59)
        _Fills(monkeypatch, as_of="2026-11-03")
        fin = _Src("finnhub", _degraded("finnhub"))
        yf_frame["frame"] = _frame(D1102)
        _fetcher([dp.YFinanceSource(), fin]).fetch("ENPH")
        dp.mark_history_fill_scoring()
        yf_frame["frame"] = _frame(D1103)
        _fetcher([dp.YFinanceSource(), fin]).fetch("ENPH")
        s = dp.history_fill_stats()
        assert s["yf_tail"] == {} and list(s["at_scoring"]["yf_tail"]) == ["ENPH"]

    def test_empty_snapshot_template_has_the_key(self):
        dp.mark_history_fill_scoring()
        dp._record_history_fill("X", "p", {}, False)
        assert dp.history_fill_stats()["at_scoring"]["yf_tail"] == {}


def _analyze(tmp_path, counter):
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": {"history_fill": counter}, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return [x for x in a.alerts if "yfinance 日线" in x.message]


class TestAlert:
    def test_refused_raises_p2_naming_the_tickers(self, monkeypatch, yf_frame, tmp_path):
        _xnow(monkeypatch, D1103, 15, 59)
        _Fills(monkeypatch, as_of="2026-11-03")
        fin = _Src("finnhub", _degraded("finnhub"))
        yf_frame["frame"] = _frame(D1102)
        for t in ("ENPH", "RKLB"):
            _fetcher([dp.YFinanceSource(), fin]).fetch(t)
        yf_frame["frame"] = _frame(D1103)
        _fetcher([dp.YFinanceSource(), fin]).fetch("XOM")
        dp.mark_history_fill_scoring()
        (hit,) = _analyze(tmp_path, dp.history_fill_stats())
        assert hit.level == am.AlertLevel.MEDIUM and "2 只" in hit.message and "缺最近一场" in hit.message
        assert "ENPH" in hit.details["标的"] and "RKLB" in hit.details["标的"] and "XOM" not in hit.details["标的"]
        assert "末根 2026-11-02 → 应有 2026-11-03" in hit.details["日线末端"]

    def test_unjudged_raises_its_own_p2(self, monkeypatch, yf_frame, tmp_path):
        monkeypatch.setattr(dp, "_exchange_now", lambda: None)
        _Fills(monkeypatch, as_of="2026-11-03")
        yf_frame["frame"] = _frame(D1102)
        _fetcher([dp.YFinanceSource()]).fetch("ENPH")
        dp.mark_history_fill_scoring()
        (hit,) = _analyze(tmp_path, dp.history_fill_stats())
        assert "判不了" in hit.message and "ENPH" in hit.details["标的"]

    def test_clean_is_silent(self, monkeypatch, yf_frame, tmp_path):
        _xnow(monkeypatch, D1103, 15, 59)
        _Fills(monkeypatch, as_of="2026-11-03")
        yf_frame["frame"] = _frame(D1103)
        _fetcher([dp.YFinanceSource()]).fetch("XOM")
        dp.mark_history_fill_scoring()
        assert _analyze(tmp_path, dp.history_fill_stats()) == []

    def test_old_counter_without_the_key_is_silent(self, tmp_path):
        old = {"resolutions": 1, "by_path": {}, "window_behind_session": 0, "window_behind": {},
               "still_missing": {"momentum": [], "volume": [], "volatility": []}}
        assert _analyze(tmp_path, old) == []
