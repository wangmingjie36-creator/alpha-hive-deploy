"""v0.45.804 跨式账本的状态不变式：平仓条件触发后仍在账上 ⇒ 超期 ≥ 2 个交易日报 P2。

NVDA 2026-11-18~20 卡了两天、是人工核对才发现的：每个「平不了」的原因此前都只在日志里留一行。这里**不按原因测**，按结果测——
应平日由持久化状态（财报日、到期日）+ 交易日历推出，与平仓路径互相独立，所以平仓路径自己坏了（算出非有限值被拒、没走到）也报得出来。

夹具与桩复用 `tests/test_options_paper_leg.py`（`state` 是它的 autouse 夹具）。夹具日期：09-03 入场、财报 09-20（周日）、
到期 10-02 ⇒ 应平日 = 09-21（周一）。没有任何一处读真实钟：`as_of` 全由调用方传入，交易日算术走 `is_trading_day.add_trading_days`。
"""

import json
from datetime import date, timedelta

import pytest

import alert_manager as am
import options_paper_leg as opl
import scan_timing
from is_trading_day import is_trading_day
from tests.test_options_paper_leg import (  # noqa: F401  (state = autouse 夹具，必须在本模块命名空间里)
    AS_OF, CALL, PUT, _none_quotes, _q, _quotes_fn, _sig, state,
)

GOOD = {CALL: _q(0.9, 1.1), PUT: _q(2.9, 3.1)}


@pytest.fixture(autouse=True)
def _fresh_stats():
    opl.reset_stats()
    yield
    opl.reset_stats()


def _open_short():
    r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="rich")])
    assert r["positions"][0]["side"] == "short"


def _day(d, table=None):
    return opl.run_for_date(d, quotes_fn=_quotes_fn(table) if table is not None else _none_quotes, signals=[])


def _stuck_until(last_day):
    """开仓后每个交易日都没有报价：09-21（应平日）起一路平不了。"""
    _open_short()
    out = None
    for d in ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"):
        out = _day(d)
        if d == last_day:
            break
    return out


# ==================== 应平日：持久化状态 + 交易日历 ====================

class TestExitDueDate:
    def test_post_event_is_the_first_session_after_earnings(self):
        assert opl.exit_due_date("2026-11-17", "2026-12-18") == ("2026-11-18", "post_event")

    def test_friday_earnings_is_due_on_monday(self):
        assert opl.exit_due_date("2026-11-20", "2026-12-18") == ("2026-11-23", "post_event")

    def test_holiday_is_skipped(self):
        """感恩节 2026-11-26 休市：周三财报 ⇒ 应平日是周五 11-27，不是 11-26。"""
        assert opl.exit_due_date("2026-11-25", "2026-12-18") == ("2026-11-27", "post_event")

    def test_earnings_with_a_timestamp_is_read_by_its_first_ten_chars(self):
        assert opl.exit_due_date("2026-11-17T20:00:00", "2026-12-18") == ("2026-11-18", "post_event")

    def test_expiry_buffer_rolls_forward_to_a_session(self, monkeypatch):
        """exit_after_event 关着、到期 2026-12-01（周二）、buffer 2 ⇒ 边界日 11-29 是周日 ⇒ 当天扫描不存在，顺延到周一 11-30。"""
        monkeypatch.setitem(opl.CONFIG, "exit_after_event", False)
        assert opl.exit_due_date("2026-11-17", "2026-12-01") == ("2026-11-30", "expiry_buffer")

    def test_the_earlier_of_the_two_wins(self):
        # 财报日 11-19、到期 11-20、buffer 2 ⇒ 到期强平 11-18 先到（TSLA 型：入场闸 v0.45.602 之后不再产生，存量单仍可能有）
        assert opl.exit_due_date("2026-11-19", "2026-11-20") == ("2026-11-18", "expiry_buffer")

    def test_a_same_day_tie_reports_post_event_like_the_exit_path(self):
        assert opl.exit_due_date("2026-11-17", "2026-11-20") == ("2026-11-18", "post_event")

    def test_unusable_inputs(self):
        assert opl.exit_due_date(None, "garbage") == (None, None)
        assert opl.exit_due_date("2026-11-17", "garbage") == ("2026-11-18", "post_event")      # 只有财报日可用
        assert opl.exit_due_date(None, "2026-11-20") == ("2026-11-18", "expiry_buffer")          # 只有到期日可用


class TestReconcilesWithTheExitPredicate:
    """逐日双向对账：「as_of ≥ 应平日」⟺「账本这天会触发平仓」。应平日这一份推导和 `_run_for_date` 里的两个触发条件
    （`as_of > earnings_date` / `expiry_buffer_hit`）若各自漂移，这里红——否则告警会对着一个不存在的日期报。"""

    COMBOS = [("2026-11-17", "2026-11-20"), ("2026-11-25", "2026-12-18"), ("2026-11-20", "2026-11-27"),
              ("2026-11-30", "2026-12-01"), ("2026-12-24", "2026-12-31"), ("2026-11-19", "2026-11-20")]

    @pytest.mark.parametrize("earnings,expiry", COMBOS)
    @pytest.mark.parametrize("after_event", [True, False])
    def test_due_iff_the_ledger_would_exit(self, earnings, expiry, after_event, monkeypatch):
        monkeypatch.setitem(opl.CONFIG, "exit_after_event", after_event)
        due, _ = opl.exit_due_date(earnings, expiry)
        d, end, checked = date(2026, 11, 2), date(2027, 1, 8), 0
        while d <= end:
            if is_trading_day(d)[0]:
                iso = d.isoformat()
                triggers = (after_event and iso > earnings) or opl.expiry_buffer_hit(iso, expiry)
                assert (iso >= due) == bool(triggers), (earnings, expiry, iso, due)
                checked += 1
            d += timedelta(days=1)
        assert checked > 40


class TestSessionsAfter:
    @pytest.mark.parametrize("due,as_of,n", [
        ("2026-11-18", "2026-11-17", 0),        # as_of 早于应平日
        ("2026-11-18", "2026-11-18", 0),        # 应平日当天
        ("2026-11-18", "2026-11-19", 1),
        ("2026-11-18", "2026-11-20", 2),
        ("2026-11-18", "2026-11-23", 3),        # 跨周末：11-21 / 11-22 不算
        ("2026-11-25", "2026-11-27", 1),        # 跨感恩节：11-26 不算
    ])
    def test_counts_trading_sessions_only(self, due, as_of, n):
        assert opl._sessions_after(due, as_of) == n


# ==================== 不变式本身：卡在账上多少个交易日 ====================

class TestOverduePositions:
    def test_due_day_itself_is_not_overdue(self):
        r = _stuck_until("2026-09-21")
        assert r["overdue"] == [] and len(r["exit_blocked"]) == 1
        assert opl.stats()["overdue_alert"] is False

    def test_one_session_late_is_recorded_but_does_not_alert(self):
        r = _stuck_until("2026-09-22")
        (o,) = r["overdue"]
        assert (o["kind"], o["due"], o["due_reason"], o["overdue_sessions"]) == ("overdue", "2026-09-21", "post_event", 1)
        assert o["reason"].startswith("平不了（自 2026-09-21）")
        s = opl.stats()
        assert s["overdue_n"] == 1 and s["max_overdue_sessions"] == 1 and s["overdue_alert"] is False

    def test_two_sessions_late_alerts(self):
        r = _stuck_until("2026-09-23")
        assert r["overdue"][0]["overdue_sessions"] == 2
        s = opl.stats()
        assert s["max_overdue_sessions"] == 2 and s["overdue_alert"] is True

    def test_it_clears_once_the_position_closes(self):
        _stuck_until("2026-09-22")
        r = _day("2026-09-23", GOOD)
        assert r["overdue"] == [] and len(r["closed_today"]) == 1
        assert opl.stats()["overdue_n"] == 0 and opl.stats()["overdue_alert"] is False

    def test_settle_deferred_is_named_as_the_reason(self):
        """到期后拿不到结算那一场的收盘 ⇒ 延期；原因取账本已留的痕。"""
        _open_short()
        for d in ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"):
            r = opl.run_for_date(d, quotes_fn=_none_quotes, signals=[],
                                 closes_fn=lambda tk, dd: {"price": None, "session": None, "source": None, "reason": "no_final_bar"})
        assert r["settle_deferred"], "夹具应让它走到「结算延期」"
        o = [x for x in r["overdue"] if x["kind"] == "overdue"][0]
        assert o["reason"].startswith("结算延期") and o["overdue_sessions"] >= 2

    def test_a_cause_the_detector_was_not_written_for_still_shows_up(self, monkeypatch):
        """守卫要有牙：账本平仓路径自己坏了（算出非有限值 ⇒ 「拒绝入账、继续持有」，只有一条 ERROR 日志），既没有 `exit_blocked`
        也没有 `settle_deferred`——报价是好的、也不是 bid=0。检查只看结果，所以照样报，且原因写「未知」（最危险的一种）。"""
        real_close = opl._close

        def _bad_close(*a, **k):
            rec, _delta = real_close(*a, **k)
            return rec, float("nan")

        monkeypatch.setattr(opl, "_close", _bad_close)
        _open_short()
        for d in ("2026-09-21", "2026-09-22", "2026-09-23"):
            r = _day(d, GOOD)
        assert r["closed_today"] == [] and r["exit_blocked"] == [] and r["settle_deferred"] == []
        (o,) = r["overdue"]
        assert o["overdue_sessions"] == 2 and o["reason"].startswith("原因未知")
        assert opl.stats()["overdue_alert"] is True

    def test_log_level_follows_whether_anything_else_left_a_trace(self, monkeypatch, caplog):
        """已知原因（平不了 / 结算延期）是设计内的等待、各自已有 WARNING，P2 才是响的那一道 ⇒ 这里 WARNING；
        「原因未知」没有任何别的痕迹 ⇒ ERROR。（`test_options_paper_leg_settle_close` 钉着「设计内的次日结算不该报 ERROR」。）"""
        import logging
        with caplog.at_level(logging.INFO, logger=opl._log.name):
            _stuck_until("2026-09-23")
        mine = [r for r in caplog.records if "该平仓却还在账上" in r.getMessage()]
        assert mine and all(r.levelno == logging.WARNING for r in mine)
        caplog.clear()
        real_close = opl._close
        monkeypatch.setattr(opl, "_close", lambda *a, **k: (real_close(*a, **k)[0], float("nan")))
        state_dir_files = [opl.POSITIONS_FILE, opl.CLOSED_FILE, opl.EQUITY_FILE, opl.META_FILE]
        for p in state_dir_files:                                   # 换一本干净的账：未知原因场景
            p.unlink(missing_ok=True)
        _open_short()
        with caplog.at_level(logging.INFO, logger=opl._log.name):
            for d in ("2026-09-21", "2026-09-22", "2026-09-23"):
                _day(d, GOOD)
        unknown = [r for r in caplog.records if "该平仓却还在账上" in r.getMessage()]
        assert unknown and all(r.levelno == logging.ERROR for r in unknown)

    def test_undeterminable_due_date_is_flagged_immediately(self):
        _open_short()
        p = opl.StraddlePosition.from_record({**_day("2026-09-04")["positions"][0], "expiry": "garbage", "earnings_date": None})
        (o,) = opl.overdue_positions([p], "2026-09-04")
        assert o["kind"] == "undeterminable" and o["due"] is None
        opl._note_run([o])
        assert opl.stats()["undeterminable_n"] == 1 and opl.stats()["overdue_alert"] is True

    def test_one_broken_position_does_not_hide_the_others(self, monkeypatch):
        _stuck_until("2026-09-23")
        pos = opl.StraddlePosition.from_record(opl._load_jsonl(opl.POSITIONS_FILE)[0])
        broken = opl.StraddlePosition.from_record({**pos.to_dict(), "ticker": "BAD", "earnings_date": "BOOM"})
        real = opl.exit_due_date

        def _due(earnings, expiry):
            if earnings == "BOOM":
                raise RuntimeError("calendar exploded")
            return real(earnings, expiry)

        monkeypatch.setattr(opl, "exit_due_date", _due)
        got = {o["ticker"]: o for o in opl.overdue_positions([broken, pos], "2026-09-23")}
        assert got["BAD"]["kind"] == "undeterminable" and "RuntimeError" in got["BAD"]["reason"]
        assert got["XYZ"]["kind"] == "overdue" and got["XYZ"]["overdue_sessions"] == 2


# ==================== 登记（进程内）====================

class TestStats:
    def test_never_ran_is_all_zero_and_not_a_fault(self):
        s = opl.stats()
        assert s["runs"] == 0 and s["run_errors"] == 0 and s["overdue_n"] == 0 and s["items"] == []
        assert s["overdue_alert"] is False

    def test_a_rerun_overwrites_instead_of_accumulating(self):
        _stuck_until("2026-09-23")
        _day("2026-09-23")                                   # 同日重跑
        s = opl.stats()
        assert s["runs"] == 5 and s["overdue_n"] == 1 and len(s["items"]) == 1 and s["max_overdue_sessions"] == 2

    def test_a_whole_run_that_raises_is_registered_and_re_raised(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("ledger exploded")
        monkeypatch.setattr(opl, "_run_for_date", _boom)
        with pytest.raises(RuntimeError, match="ledger exploded"):
            opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[])
        s = opl.stats()
        assert s["run_errors"] == 1 and s["items"][0]["kind"] == "run_error" and "ledger exploded" in s["items"][0]["reason"]
        assert s["overdue_alert"] is False, "整轮抛是另一类，不混进 overdue_alert"

    def test_items_are_capped(self):
        opl._note_run([{"kind": "overdue", "overdue_sessions": 2, "ticker": f"T{i}"} for i in range(40)])
        assert len(opl.stats()["items"]) == opl._ITEMS_MAX and opl.stats()["overdue_n"] == 40


# ==================== 告警（alert_manager P2）====================

class TestAlert:
    @staticmethod
    def _alerts(tmp_path, counters):
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a._check_options_paper({"scan_timing": {"counters": counters}})
        return a

    def test_quiet_when_nothing_is_overdue(self, tmp_path):
        a = self._alerts(tmp_path, {"options_paper": opl.stats()})
        assert not a.alerts and not a.checks_skipped

    def test_one_session_late_does_not_alert(self, tmp_path):
        _stuck_until("2026-09-22")
        assert not self._alerts(tmp_path, {"options_paper": opl.stats()}).alerts

    def test_two_sessions_late_alerts_p2_with_the_reason(self, tmp_path):
        _stuck_until("2026-09-23")
        a = self._alerts(tmp_path, {"options_paper": opl.stats()})
        assert len(a.alerts) == 1 and a.alerts[0].level == am.AlertLevel.MEDIUM and "P2" in a.alerts[0].message
        assert "XYZ short" in a.alerts[0].details["明细"] and "平不了" in a.alerts[0].details["明细"]

    def test_undeterminable_and_run_errors_alert(self, tmp_path):
        base = {"overdue_alert": False, "max_overdue_sessions": 0, "overdue_n": 0, "items": []}
        assert len(self._alerts(tmp_path, {"options_paper": {**base, "undeterminable_n": 1, "overdue_alert": True}}).alerts) == 1
        assert len(self._alerts(tmp_path, {"options_paper": {**base, "undeterminable_n": 0, "run_errors": 2}}).alerts) == 1

    def test_missing_counter_is_skipped_not_green(self, tmp_path):
        a = self._alerts(tmp_path, {"options_paper": None})
        assert not a.alerts and any("跨式账本超期检查" in s for s in a.checks_skipped)

    def test_scan_timing_carries_the_counters(self):
        _stuck_until("2026-09-23")
        c = scan_timing.counters()["options_paper"]
        assert c["runs"] >= 1 and c["overdue_alert"] is True

    def test_the_real_analyze_entry_runs_the_check(self, tmp_path):
        """`analyze()` 才是生产入口：只测 `_check_options_paper` 本身时，删掉 analyze 里那一行调用全绿。"""
        _stuck_until("2026-09-23")
        status = {"status": "success", "total_duration_seconds": 1,
                  "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"date": "2026-09-23", "production_sync": {"outcome": "up_to_date"}, "extra": {},
                                  "counters": scan_timing.counters()}}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status), encoding="utf-8")
        got = [x.message for x in am.AlertAnalyzer(report_dir=tmp_path).analyze(p) if "跨式纸面账本" in x.message]
        assert len(got) == 1 and "超期 2 个交易日" in got[0], got
