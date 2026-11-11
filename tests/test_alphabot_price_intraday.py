"""价格页盘中 1 分钟 K 线（v0.45.763）：`alphabot/price_intraday.py` 与 `AlphaBotService.minute_bars`。

全部离线：yfinance 用假模块注入，服务用注入的 `minute_fn` / 钟。逐条都配「违反它就会红」的输入。
"""
from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from alphabot import price_intraday as PI
from alphabot.service import AlphaBotService, MINUTE_TTL_LIVE_SEC

ET = ZoneInfo("America/New_York")
DAY = "2026-11-10"                       # 周二，交易日
NOON = datetime(2026, 11, 10, 12, 0, 30, tzinfo=ET)
REPO = Path(__file__).resolve().parent.parent


def _hist(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(r[0], tz="America/New_York") for r in rows])
    return pd.DataFrame({"Open": [r[1] for r in rows], "High": [r[2] for r in rows], "Low": [r[3] for r in rows],
                         "Close": [r[4] for r in rows], "Volume": [r[5] for r in rows],
                         "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)


GOOD = [(f"{DAY} 11:58", 100.0, 100.4, 99.8, 100.2, 5000), (f"{DAY} 11:59", 100.2, 100.6, 100.1, 100.5, 4000),
        (f"{DAY} 12:00", 100.5, 100.5, 100.5, 100.5, 0)]      # 最后一根是正在走的这一分钟


class TestRows:
    def test_reads_one_day_in_exchange_time(self):
        rows = PI.rows_from_history(_hist(GOOD + [("2026-11-09 15:59", 9.0, 9.5, 8.5, 9.2, 10)]), DAY)
        assert [r["t"] for r in rows] == ["11:58", "11:59", "12:00"]
        assert rows[0] == {"t": "11:58", "open": 100.0, "high": 100.4, "low": 99.8, "close": 100.2, "volume": 5000.0}

    def test_bad_rows_are_skipped_and_missing_volume_is_not_zero(self):
        rows = PI.rows_from_history(_hist([
            (f"{DAY} 09:30", float("nan"), 1.0, 1.0, 1.0, 1),          # 缺开盘
            (f"{DAY} 09:31", 10.0, 9.0, 11.0, 10.0, 1),                # 高低颠倒
            (f"{DAY} 09:32", 10.0, 10.5, 9.5, 10.2, float("nan"))]), DAY)
        assert [r["t"] for r in rows] == ["09:32"] and rows[0]["volume"] is None

    def test_missing_columns_or_none(self):
        assert PI.rows_from_history(None, DAY) == []
        assert PI.rows_from_history(pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex([f"{DAY} 09:30"])), DAY) == []


class TestDropForming:
    ROWS = [{"t": "11:58"}, {"t": "11:59"}, {"t": "12:00"}]

    def test_the_minute_still_running_is_dropped_today(self):
        kept, n = PI.drop_forming(self.ROWS, DAY, NOON)
        assert [r["t"] for r in kept] == ["11:58", "11:59"] and n == 1

    def test_a_past_day_is_left_alone(self):
        kept, n = PI.drop_forming(self.ROWS, "2026-11-09", NOON)
        assert len(kept) == 3 and n == 0


class TestVolumeOutliers:
    def _rows(self, vols):
        return [{"t": f"10:{i:02d}", "volume": v} for i, v in enumerate(vols)]

    def test_cumulative_looking_bars_lose_their_volume_but_keep_the_raw_value(self):
        rows = self._rows([600_000.0] * 30 + [90_000_000.0])
        out, n = PI.drop_volume_outliers(rows)
        assert n == 1 and out[-1]["volume"] is None and out[-1]["volume_raw"] == 90_000_000.0
        assert all(r["volume"] == 600_000.0 and "volume_raw" not in r for r in out[:-1])
        assert rows[-1]["volume"] == 90_000_000.0, "不许改传入的行"

    def test_a_genuinely_heavy_opening_bar_survives(self):
        out, n = PI.drop_volume_outliers(self._rows([6_000_000.0] + [600_000.0] * 30))     # 10 倍：开盘放量，不是异常
        assert n == 0 and out[0]["volume"] == 6_000_000.0

    def test_too_few_bars_to_judge(self):
        out, n = PI.drop_volume_outliers(self._rows([1.0] * (PI.VOLUME_MIN_BARS - 2) + [1e9]))
        assert n == 0 and out[-1]["volume"] == 1e9

    def test_missing_volume_is_ignored_not_counted(self):
        out, n = PI.drop_volume_outliers(self._rows([None] * 5 + [600_000.0] * 25))
        assert n == 0 and out[0]["volume"] is None


def _snap(ts, last_trade, zg, pos, neg):
    return {"ts": ts, "payload_last_trade_time": last_trade,
            "views": {"le_45dte": {"zero_gamma": {"nearest": zg}, "majors": {"net_major_pos_strike": pos, "net_major_neg_strike": neg}}}}


class TestLevelPath:
    def test_points_sit_at_the_quote_time_not_the_write_time(self):
        path = PI.level_path([_snap(f"{DAY}T10:15:00-05:00", f"{DAY}T10:00:03", 171.1, 185.0, 160.0)], DAY)
        assert path == [{"t": "10:00", "zero_gamma": 171.1, "major_pos": 185.0, "major_neg": 160.0}]

    def test_falls_back_to_write_time_when_quote_time_is_missing_or_another_day(self):
        path = PI.level_path([_snap(f"{DAY}T09:40:00-05:00", "2026-11-09T16:00:00", 1.0, 2.0, 3.0),
                              _snap(f"{DAY}T09:50:00-05:00", None, 1.5, 2.0, 3.0)], DAY)
        assert [p["t"] for p in path] == ["09:40", "09:50"]

    def test_other_days_never_leak_in(self):
        # 单日规则：别的日子的快照不许出现在这一天的图上
        assert PI.level_path([_snap("2026-11-09T10:00:00-05:00", "2026-11-09T09:45:00", 1.0, 2.0, 3.0)], DAY) == []

    def test_empty_levels_and_junk_are_skipped(self):
        assert PI.level_path([_snap(f"{DAY}T10:00:00-05:00", None, None, None, None), "junk", {}], DAY) == []


class _FakeYF:
    def __init__(self, hist=None, exc=None):
        self.hist, self.exc, self.calls = hist, exc, []

    def Ticker(self, t):  # noqa: N802 - yfinance 的写法
        outer = self

        class _T:
            ticker = t

            def history(self, **kw):
                outer.calls.append((t, kw))
                if outer.exc:
                    raise outer.exc
                return outer.hist
        return _T()


class TestFetch:
    def test_today_asks_for_one_day_and_drops_the_running_minute(self):
        fake = _FakeYF(_hist(GOOD))
        rows, info = PI.fetch_minutes("NVDA", DAY, now=NOON, yf_module=fake)
        assert [r["t"] for r in rows] == ["11:58", "11:59"]
        assert info == {"n": 2, "reason": None, "dropped_forming": 1, "volume_suspect": 0}
        assert fake.calls == [("NVDA", {"interval": "1m", "auto_adjust": False, "prepost": False, "period": "1d"})]

    def test_a_past_day_asks_for_exactly_that_day(self):
        fake = _FakeYF(_hist([("2026-11-06 15:59", 1.0, 1.2, 0.9, 1.1, 5)]))
        rows, info = PI.fetch_minutes("NVDA", "2026-11-06", now=NOON, yf_module=fake)
        assert len(rows) == 1 and info["dropped_forming"] == 0
        assert fake.calls[0][1] == {"interval": "1m", "auto_adjust": False, "prepost": False,
                                    "start": "2026-11-06", "end": "2026-11-07"}

    @pytest.mark.parametrize("ticker,day,reason", [
        ("NVDA", "2026-11-01", "out_of_range"),       # 9 天前：Yahoo 不给，不发请求
        ("NVDA", "2026-11-11", "future_date"),
        ("_SPX", DAY, "index_not_supported")])
    def test_refusals_never_send_a_request(self, ticker, day, reason):
        fake = _FakeYF(_hist(GOOD))
        assert PI.fetch_minutes(ticker, day, now=NOON, yf_module=fake) == ([], {"n": 0, "reason": reason})
        assert fake.calls == []

    def test_failure_carries_reason_and_never_raises(self):
        rows, info = PI.fetch_minutes("NVDA", DAY, now=NOON, yf_module=_FakeYF(exc=RuntimeError("boom")))
        assert rows == [] and info == {"n": 0, "reason": "exception", "detail": "RuntimeError"}

    def test_empty_answer_is_unavailable(self):
        rows, info = PI.fetch_minutes("NVDA", DAY, now=NOON, yf_module=_FakeYF(_hist([])))
        assert rows == [] and info["reason"] == "unavailable"

    def test_source_is_registered_and_literal(self):
        import yf_daily
        assert "alphabot_price_minute" in yf_daily.SOURCES
        assert 'source="alphabot_price_minute"' in inspect.getsource(PI)

    def test_every_reason_the_module_returns_is_in_the_table(self):
        """原因码双向核对：代码里写出来的字面量都在 `REASONS` 里，`REASONS` 里的都真有出处。"""
        tree = ast.parse(inspect.getsource(PI))
        used = {kv.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
                for k, kv in zip(n.keys, n.values)
                if isinstance(k, ast.Constant) and k.value == "reason" and isinstance(kv, ast.Constant) and kv.value}
        used |= {"unavailable"}                      # `None if rows else "unavailable"` 是条件表达式，不是常量
        assert used == set(PI.REASONS), used ^ set(PI.REASONS)


ROWS = [{"t": "11:58", "open": 100.0, "high": 100.4, "low": 99.8, "close": 100.2, "volume": 5000.0},
        {"t": "11:59", "open": 100.2, "high": 100.6, "low": 100.1, "close": 100.5, "volume": 4000.0}]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _svc(tmp_path, fn, *, now=NOON, clock=None):
    return AlphaBotService(state_dir=tmp_path, bars_fn=lambda t: {"available": False, "bars": []},
                           minute_fn=fn, now_fn=lambda: now, clock=clock or _Clock())


class TestService:
    def test_bars_and_same_day_level_path(self, tmp_path):
        d = tmp_path / "intraday" / DAY
        d.mkdir(parents=True)
        (d / "NVDA.jsonl").write_text(json.dumps(_snap(f"{DAY}T11:50:00-05:00", f"{DAY}T11:35:00", 99.0, 105.0, 95.0)) + "\n", encoding="utf-8")
        other = tmp_path / "intraday" / "2026-11-09"
        other.mkdir(parents=True)
        (other / "NVDA.jsonl").write_text(json.dumps(_snap("2026-11-09T11:50:00-05:00", "2026-11-09T11:35:00", 1.0, 2.0, 3.0)) + "\n", encoding="utf-8")
        s = _svc(tmp_path, lambda t, day, now: (ROWS, {"n": 2, "reason": None, "dropped_forming": 1}))
        res = s.minute_bars("NVDA")
        assert res["available"] and res["date"] == DAY and res["is_today"] and res["interval"] == "1m"
        assert res["session_live"] is True and res["refresh_sec"] == PI.REFRESH_SEC
        assert [b["t"] for b in res["bars"]] == ["11:58", "11:59"]
        assert res["level_path"] == [{"t": "11:35", "zero_gamma": 99.0, "major_pos": 105.0, "major_neg": 95.0}]

    def test_live_requests_are_rate_capped_by_the_ttl(self, tmp_path):
        calls, clock = [], _Clock()

        def fn(t, day, now):
            calls.append(t)
            return ROWS, {"n": 2, "reason": None}
        s = _svc(tmp_path, fn, clock=clock)
        s.minute_bars("NVDA"), s.minute_bars("NVDA")
        assert calls == ["NVDA"] and s.stats["minute_cache_hits"] == 1
        clock.t += MINUTE_TTL_LIVE_SEC + 1
        s.minute_bars("NVDA")
        assert len(calls) == 2, "过了限频窗口要重新取（否则盘中图不会动）"

    def test_a_past_day_is_fetched_once_and_never_refreshes(self, tmp_path):
        calls, clock = [], _Clock()

        def fn(t, day, now):
            calls.append(day)
            return ROWS, {"n": 2, "reason": None}
        s = _svc(tmp_path, fn, clock=clock)
        res = s.minute_bars("NVDA", "2026-11-06")
        clock.t += 10 * 3600
        s.minute_bars("NVDA", "2026-11-06")
        assert calls == ["2026-11-06"] and res["refresh_sec"] is None and res["is_today"] is False

    def test_failure_is_visible_and_not_cached(self, tmp_path):
        calls = []

        def fail(t, day, now):
            calls.append(t)
            return [], {"n": 0, "reason": "rate_limited", "retry_after_sec": 150.0}
        s = _svc(tmp_path, fail)
        res = s.minute_bars("NVDA")
        assert res["available"] is False and res["reason"] == "rate_limited" and res["retry_after_sec"] == 150.0 and res["bars"] == []
        assert s.stats["minute_failures"] == 1
        s.minute_bars("NVDA")
        assert len(calls) == 2

    def test_refresh_failure_keeps_the_last_good_bars_and_says_so(self, tmp_path):
        clock, state = _Clock(), {"ok": True}

        def fn(t, day, now):
            return (ROWS, {"n": 2, "reason": None}) if state["ok"] else ([], {"n": 0, "reason": "rate_limited", "retry_after_sec": 120.0})
        s = _svc(tmp_path, fn, clock=clock)
        s.minute_bars("NVDA")
        state["ok"] = False
        clock.t += MINUTE_TTL_LIVE_SEC + 1
        res = s.minute_bars("NVDA")
        assert res["available"] and len(res["bars"]) == 2, "图不清空"
        assert res["refresh_failed"] == {"reason": "rate_limited", "retry_after_sec": 120.0}
        assert s.stats["minute_failures"] == 1

    def test_bars_fetched_during_the_session_are_refetched_once_it_closes(self, tmp_path):
        # 15:58 取的那份缺收盘前最后几根：16:01 再打开必须重取，不能当成「不在盘中 30 分钟内不用重取」的缓存
        calls, clock, when = [], _Clock(), {"now": datetime(2026, 11, 10, 15, 58, 30, tzinfo=ET)}

        def fn(t, day, now):
            calls.append(now.strftime("%H:%M"))
            return ROWS, {"n": 2, "reason": None}
        s = AlphaBotService(state_dir=tmp_path, bars_fn=lambda t: {"available": False, "bars": []},
                            minute_fn=fn, now_fn=lambda: when["now"], clock=clock)
        s.minute_bars("NVDA")
        when["now"] = datetime(2026, 11, 10, 16, 1, tzinfo=ET)
        clock.t += 150
        s.minute_bars("NVDA")
        assert calls == ["15:58", "16:01"]
        clock.t += 600
        s.minute_bars("NVDA")
        assert len(calls) == 2, "收盘后取过一次就够了（30 分钟内不再取）"

    def test_raising_fn_is_a_reasoned_failure(self, tmp_path):
        def boom(t, day, now):
            raise ValueError("x")
        res = _svc(tmp_path, boom).minute_bars("NVDA")
        assert res["available"] is False and res["reason"] == "exception" and res["detail"] == "ValueError"

    def test_outside_the_session_there_is_no_refresh(self, tmp_path):
        evening = datetime(2026, 11, 10, 18, 0, tzinfo=ET)
        res = _svc(tmp_path, lambda t, day, now: (ROWS, {"n": 2, "reason": None}), now=evening).minute_bars("NVDA")
        assert res["session_live"] is False and res["refresh_sec"] is None

    def test_injected_bars_without_minute_fn_never_reaches_the_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(PI, "fetch_minutes", lambda *a, **k: pytest.fail("不该出网"))
        s = AlphaBotService(state_dir=tmp_path, bars_fn=lambda t: {"available": False, "bars": []}, now_fn=lambda: NOON)
        res = s.minute_bars("NVDA")
        assert res["available"] is False and res["reason"] == "not_wired"

    def test_bad_date_is_rejected(self, tmp_path):
        from alphabot.service import BadRequest
        with pytest.raises(BadRequest):
            _svc(tmp_path, lambda *a: ([], {})).minute_bars("NVDA", "11/10")


class TestDemoAndRoute:
    def test_demo_minutes_are_well_formed(self):
        from alphabot import synthetic as syn
        rows = syn.demo_minutes("NVDA", DAY)
        assert len(rows) == 390 and rows[0]["t"] == "09:30" and rows[-1]["t"] == "15:59"
        assert all(r["low"] <= min(r["open"], r["close"]) <= max(r["open"], r["close"]) <= r["high"] for r in rows)

    def test_route_is_served(self, tmp_path):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        s = _svc(tmp_path, lambda t, day, now: (ROWS, {"n": 2, "reason": None}))
        c = TestClient(create_app(s, port=8765), base_url="http://127.0.0.1:8765")
        r = c.get(f"/api/minute-bars/NVDA?date={DAY}")
        assert r.status_code == 200 and len(r.json()["bars"]) == 2
        assert c.get("/api/minute-bars/NVDA?date=bad").status_code == 400

    def test_page_uses_no_inline_style(self):
        # 本站 CSP 不许行内样式：悬停提示与页面里写了 style= 会被拦掉（颜色丢失、控制台报错）
        for rel in ("alphabot/static/charts.js", "alphabot/static/pages/ticker.js", "alphabot/static/pages/price_minute.js"):
            assert "style=" not in (REPO / rel).read_text(encoding="utf-8"), rel
