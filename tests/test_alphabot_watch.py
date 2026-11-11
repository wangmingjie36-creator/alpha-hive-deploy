"""盘中图「盯盘」（v0.45.764）：开着盘中图的那只票每分钟拍一张快照，其余关注票节奏不变。

全部离线：`take_snapshot` 换成记账的假函数（不拉链、不落盘），钟与交易时段注入。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from alphabot import service as S
from alphabot.service import AlphaBotService, IntradayPoller, WATCH_INTERVAL_SEC, WATCH_LEASE_SEC

ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=ET)          # 周二盘中
EVENING = datetime(2026, 11, 10, 18, 0, tzinfo=ET)
LIVE = lambda: {"live": True, "now_et": "x"}             # noqa: E731
CLOSED = lambda: {"live": False}                          # noqa: E731


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _svc(tmp_path, *, now=NOON, focus=("NVDA", "TSLA"), poll=True):
    clock = _Clock()
    s = AlphaBotService(state_dir=tmp_path, bars_fn=lambda t: {"available": False, "bars": []},
                        now_fn=lambda: now, clock=clock)
    s.update_settings({"focus": list(focus), "poll_enabled": poll})
    shots = []
    s.take_snapshot = lambda t, **k: (shots.append(t), {"ticker": t, "recorded": True})[1]
    return s, clock, shots


class TestLease:
    def test_without_a_poller_the_page_is_told_nothing_will_be_shot(self, tmp_path):
        s, _, _ = _svc(tmp_path)
        r = s.watch("nvda")
        assert r["ticker"] == "NVDA" and r["active"] is False and r["reason"] == "no_poller"

    def test_active_with_a_poller_in_session(self, tmp_path):
        s, _, _ = _svc(tmp_path)
        IntradayPoller(s, session_fn=LIVE)
        r = s.watch("NVDA")
        assert r["active"] is True and r["reason"] is None
        assert r["interval_sec"] == WATCH_INTERVAL_SEC and r["lease_sec"] == WATCH_LEASE_SEC and s.watch_ticker() == "NVDA"

    @pytest.mark.parametrize("kw,reason", [({"now": EVENING}, "session_closed"), ({"poll": False}, "poll_disabled")])
    def test_inactive_reasons(self, tmp_path, kw, reason):
        s, _, _ = _svc(tmp_path, **kw)
        IntradayPoller(s, session_fn=LIVE)
        assert s.watch("NVDA")["reason"] == reason

    def test_lease_expires_by_itself(self, tmp_path):
        s, clock, _ = _svc(tmp_path)
        s.watch("NVDA")
        clock.t += WATCH_LEASE_SEC - 1
        assert s.watch_ticker() == "NVDA"
        clock.t += 2
        assert s.watch_ticker() is None, "页面走了没人续期，必须自己停"

    def test_only_one_ticker_at_a_time_and_the_page_is_told(self, tmp_path):
        s, _, _ = _svc(tmp_path)
        s.watch("NVDA")
        r = s.watch("JNJ")
        assert s.watch_ticker() == "JNJ" and r["replaced"] == "NVDA"
        assert s.watch("JNJ")["replaced"] is None

    def test_yahoo_fallback_tickers_are_never_watched(self, tmp_path):
        # NVDL / BILI 盘中现算会改走 Yahoo 的期权链（一次十来个请求）：每分钟一拍会把 Yahoo 打到限流
        s, _, shots = _svc(tmp_path)
        p = IntradayPoller(s, session_fn=LIVE)
        assert S.YF_FALLBACK_TICKERS, "正对照：名单是空的话这条什么也没测"
        for t in sorted(S.YF_FALLBACK_TICKERS):
            r = s.watch(t)
            assert r["active"] is False and r["reason"] == "yf_fallback_ticker"
        assert s.watch_ticker() is None
        p.tick(now_mono=0.0), p.tick(now_mono=61.0)
        assert not set(shots) & S.YF_FALLBACK_TICKERS

    def test_demo_and_index_are_refused_without_setting_a_lease(self, tmp_path):
        s, _, _ = _svc(tmp_path)
        assert s.watch("SPX")["reason"] == "index_not_supported" and s.watch_ticker() is None
        d = AlphaBotService(state_dir=tmp_path, demo=True, bars_fn=lambda t: {"available": False, "bars": []})
        assert d.watch("NVDA")["reason"] == "demo" and d.watch_ticker() is None


class TestPoller:
    def test_watched_ticker_is_shot_every_minute_between_regular_rounds(self, tmp_path):
        s, clock, shots = _svc(tmp_path)
        p = IntradayPoller(s, session_fn=LIVE)
        s.watch("NVDA")
        p.tick(now_mono=0.0)                               # 常规一轮：两只关注票（其中一只正被盯）
        assert shots == ["NVDA", "TSLA"]
        for sec in (20.0, 40.0, 59.0):                     # 常规轮刚拍过它：一分钟内不重复拍
            p.tick(now_mono=sec)
        assert shots == ["NVDA", "TSLA"]
        p.tick(now_mono=61.0)
        p.tick(now_mono=90.0)
        p.tick(now_mono=122.0)
        assert shots == ["NVDA", "TSLA", "NVDA", "NVDA"], "只多拍被盯的那一只，TSLA 节奏不变"
        assert p.status["watch"]["shots"] == 2 and p.status["watch"]["ticker"] == "NVDA"

    def test_starting_to_watch_shoots_at_once(self, tmp_path):
        # 打开盘中图 ⇒ 下一次醒来就拍一张，不等满一分钟（内容没变时 `take_snapshot` 自己去重，不会多出一行）
        s, _, shots = _svc(tmp_path)
        p = IntradayPoller(s, session_fn=LIVE)
        p.tick(now_mono=0.0)
        s.watch("NVDA")
        p.tick(now_mono=5.0)
        assert shots == ["NVDA", "TSLA", "NVDA"]

    def test_a_watched_ticker_outside_the_focus_list_is_shot_too(self, tmp_path):
        s, _, shots = _svc(tmp_path)
        p = IntradayPoller(s, session_fn=LIVE)
        s.watch("JNJ")
        p.tick(now_mono=0.0)
        assert shots == ["NVDA", "TSLA", "JNJ"]
        p.tick(now_mono=30.0)
        assert shots.count("JNJ") == 1
        p.tick(now_mono=61.0)
        assert shots.count("JNJ") == 2

    def test_no_shots_after_the_lease_runs_out(self, tmp_path):
        s, clock, shots = _svc(tmp_path)
        p = IntradayPoller(s, session_fn=LIVE)
        s.watch("NVDA")
        p.tick(now_mono=0.0)
        clock.t += WATCH_LEASE_SEC + 1
        p.tick(now_mono=200.0)
        assert shots == ["NVDA", "TSLA"] and p.status["watch"]["ticker"] is None

    def test_outside_the_session_or_with_polling_off_nothing_is_shot(self, tmp_path):
        s, _, shots = _svc(tmp_path)
        p = IntradayPoller(s, session_fn=CLOSED)
        s.watch("NVDA")
        p.tick(now_mono=0.0), p.tick(now_mono=100.0)
        assert shots == []
        s2, _, shots2 = _svc(tmp_path / "b", poll=False)
        p2 = IntradayPoller(s2, session_fn=LIVE)
        s2.watch("NVDA")
        p2.tick(now_mono=0.0), p2.tick(now_mono=100.0)
        assert shots2 == []

    def test_a_failing_watch_shot_is_counted_and_does_not_kill_the_round(self, tmp_path):
        s, _, _ = _svc(tmp_path)
        p = IntradayPoller(s, session_fn=LIVE)
        s.watch("NVDA")
        p.tick(now_mono=0.0)

        def boom(t, **k):
            raise RuntimeError("x")
        s.take_snapshot = boom
        p.tick(now_mono=61.0)
        assert p.status["errors"] == 1 and p.status["watch"]["last_result"]["reason"] == "exception:RuntimeError"

    def test_switching_the_watched_ticker_shoots_the_new_one_right_away(self, tmp_path):
        s, _, shots = _svc(tmp_path)
        p = IntradayPoller(s, session_fn=LIVE)
        s.watch("NVDA")
        p.tick(now_mono=0.0)
        p.tick(now_mono=61.0)
        s.watch("JNJ")
        p.tick(now_mono=66.0)
        assert shots == ["NVDA", "TSLA", "NVDA", "JNJ"]


class TestRouteAndPage:
    def test_post_needs_the_header_and_reports_the_reason(self, tmp_path):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        s, _, _ = _svc(tmp_path)
        c = TestClient(create_app(s, port=8765), base_url="http://127.0.0.1:8765")
        assert c.post("/api/watch/NVDA").status_code == 403
        r = c.post("/api/watch/NVDA", headers={"X-AlphaBot": "1"})
        assert r.status_code == 200 and r.json()["reason"] == "no_poller"

    def test_every_reason_has_a_label_on_the_page(self):
        import inspect
        import re
        from pathlib import Path
        js = (Path(S.__file__).parent / "static" / "pages" / "price_minute.js").read_text(encoding="utf-8")
        table = js.split("const WATCH_OFF = {")[1].split("};")[0]
        labelled = set(re.findall(r"^\s*(\w+):", table, re.M))
        src = inspect.getsource(AlphaBotService.watch)
        returned = set(re.findall(r'"reason": "(\w+)"', src))
        assert returned and returned <= labelled, returned - labelled
        assert labelled - returned == {"error"}, "页面多出来的只该是它自己的「续期请求失败」"
