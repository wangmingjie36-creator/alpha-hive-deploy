"""异常期权流对共享 yfinance 令牌桶的限速（v0.45.429）。

设计：全进程内本模块的两次 yfinance 请求至少隔 `1 / (桶速率 × PACE_BUCKET_SHARE)` 秒（默认 4s ⇒ 占桶的一半）。
预约式：多线程各领一个递增的发送时刻。**不减请求、不改评分**；缓存命中不排队；重试那一次也排队。
全部离线：时钟与睡眠都换成假的，yfinance 换成桩。
"""
from __future__ import annotations

import datetime as dt
import sys
import threading
import types

import pandas as pd
import pytest

import unusual_options as UO


class _Clock:
    """假时钟：睡眠只推进时间，不真睡。"""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            return self.t

    def sleep(self, s):
        with self._lock:
            self.sleeps.append(round(s, 6))
            self.t += s


@pytest.fixture
def clk(monkeypatch):
    c = _Clock()
    from resilience import yfinance_limiter
    monkeypatch.setattr(yfinance_limiter, "_rate", 0.5)      # 测试会话把桶速率调高过，这里钉回生产值
    monkeypatch.setattr(UO, "_pace_clock", c.now)
    monkeypatch.setattr(UO, "_pace_sleep", c.sleep)
    monkeypatch.setattr(UO, "_pace_next", 0.0)
    monkeypatch.setattr(UO, "PACE_BUCKET_SHARE", 0.5)
    monkeypatch.setattr(UO, "PACE_MAX_WAIT_S", 60.0)
    monkeypatch.setattr(UO, "_sleep", lambda s: None)
    monkeypatch.setattr(UO, "_retry_exhausted", 0)
    return c


class TestInterval:
    def test_half_of_the_shared_bucket(self, clk):
        assert UO._pace_interval() == pytest.approx(4.0), "0.5 req/s 的桶、占一半 ⇒ 0.25 req/s ⇒ 4s 一个"

    def test_follows_the_bucket_rate_not_a_second_number(self, clk, monkeypatch):
        from resilience import yfinance_limiter
        monkeypatch.setattr(yfinance_limiter, "_rate", 2.0)
        assert UO._pace_interval() == pytest.approx(1.0)

    def test_share_zero_disables(self, clk, monkeypatch):
        monkeypatch.setattr(UO, "PACE_BUCKET_SHARE", 0.0)
        assert UO._pace_interval() == 0.0 and UO._pace() == 0.0 and clk.sleeps == []


class TestPaceReservation:
    def test_first_request_is_free_then_spaced(self, clk):
        iv = UO._pace_interval()
        assert UO._pace() == 0.0
        w = UO._pace()
        assert w == pytest.approx(iv)
        assert clk.sleeps == [pytest.approx(iv)]

    def test_idle_gap_longer_than_interval_costs_nothing(self, clk):
        iv = UO._pace_interval()
        UO._pace()
        clk.t += iv * 3
        assert UO._pace() == 0.0

    def test_backlog_is_capped(self, clk, monkeypatch):
        monkeypatch.setattr(UO, "PACE_MAX_WAIT_S", 5.0)
        waits = [UO._pace() for _ in range(5)]      # 若不封顶，第 5 个要等 4×4=16s
        assert max(waits) <= 5.0 + 1e-9

    def test_concurrent_threads_get_distinct_spaced_slots(self, clk):
        """多线程同时来：各领一个递增时刻，彼此至少隔一个间隔（FIFO、不互相抢）。"""
        iv = UO._pace_interval()
        stamps = []
        gate = threading.Barrier(4)

        def worker():
            gate.wait()
            UO._pace()
            stamps.append(clk.now())

        ths = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        # 假睡眠把共享时钟往前推：4 个请求总共至少推进 3 个间隔
        assert clk.t - 1000.0 >= iv * 3 - 1e-6
        assert sum(clk.sleeps) >= iv * 3 - 1e-6


def _exp(days):
    return (dt.datetime.now() + dt.timedelta(days=days)).strftime("%Y-%m-%d")


class _Chain:
    def __init__(self):
        self.calls = pd.DataFrame([[100.0, 1000, 100, 5.0, 0.3]],
                                  columns=["strike", "volume", "openInterest", "lastPrice", "impliedVolatility"])
        self.puts = pd.DataFrame(columns=self.calls.columns)


@pytest.fixture
def yf(clk, monkeypatch):
    UO._CACHE.clear()
    UO._CACHE_TS.clear()
    st = types.SimpleNamespace(opt_script=[], calls=[])

    class T:
        def __init__(self, ticker):
            pass

        @property
        def options(self):
            st.calls.append("options")
            if st.opt_script:
                e = st.opt_script.pop(0)
                if e is not None:
                    raise e
            return [_exp(7), _exp(14), _exp(21)]

        def option_chain(self, exp):
            st.calls.append("chain")
            return _Chain()

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=T))
    yield st
    UO._CACHE.clear()
    UO._CACHE_TS.clear()


def _run(t="AAA"):
    return UO.detect_unusual_flow(t, stock_price=100.0)


class TestPacedDetection:
    def test_every_network_request_is_paced(self, yf, clk):
        r = _run()
        n = len(yf.calls)
        assert n == 4 and r["fetch_status"] == "ok"                      # .options + 3 个到期日
        iv = UO._pace_interval()
        assert clk.sleeps == [pytest.approx(iv)] * (n - 1), "首个请求免排队，其余各隔一个间隔"
        assert r["pace_wait_s"] == pytest.approx((n - 1) * iv, abs=0.1)

    def test_cache_hit_does_not_queue(self, yf, clk):
        _run()
        clk.sleeps.clear()
        n = len(yf.calls)
        r = _run()
        assert len(yf.calls) == n and clk.sleeps == [] and r["fetch_status"] == "ok"

    def test_retry_attempt_is_also_paced(self, yf, clk):
        yf.opt_script = [ConnectionError("等待 yfinance 限流令牌超过 60s（Ticker.options）")]
        r = _run()
        assert r["retries"] == 1 and r["fetch_status"] == "ok"
        assert yf.calls.count("options") == 2
        # .options 两次尝试 + 3 条链 = 5 个请求；除首个外都排过队
        iv = UO._pace_interval()
        assert len([s for s in clk.sleeps if s == pytest.approx(iv)]) == 4

    def test_scoring_is_independent_of_pacing(self, yf, clk, monkeypatch):
        """限速只改取数时机：同样的链，开关限速得到同一份信号 / 分数 / 方向。"""
        paced = _run("BBB")
        UO._CACHE.clear()
        UO._CACHE_TS.clear()
        monkeypatch.setattr(UO, "PACE_BUCKET_SHARE", 0.0)
        unpaced = _run("BBB")
        for k in ("unusual_score", "unusual_direction", "signals", "summary", "fetch_status", "chains_total"):
            assert paced[k] == unpaced[k], k
        assert unpaced["pace_wait_s"] == 0.0 and paced["pace_wait_s"] > 0

    def test_early_return_still_reports_retries_and_pacing(self, yf, clk):
        """`.options` 饥饿到预算用光 ⇒ 早退；此前 retries 在这条路径上恒为 0（v0.45.425 的漏记）。"""
        starve = ConnectionError("等待 yfinance 限流令牌超过 60s（Ticker.options）")
        yf.opt_script = [starve, starve, starve]
        r = _run("CCC")
        assert r["fetch_status"] == "failed" and r["retries"] == 2
        assert r["pace_wait_s"] > 0


class TestOracleStatusCarriesPacing:
    def test_status_has_pace_wait(self):
        from swarm_agents.oracle_bee import _unusual_flow_status
        st = _unusual_flow_status({"fetch_status": "ok", "pace_wait_s": 12.0, "retries": 0})
        assert st["pace_wait_s"] == 12.0
        assert _unusual_flow_status({})["pace_wait_s"] is None


class TestPacingHasTeeth:
    def test_without_pacing_requests_are_not_spaced(self, yf, clk, monkeypatch):
        """反向自证：关掉限速，上面「各隔一个间隔」的断言就该不成立——证明那些断言真在量限速。"""
        monkeypatch.setattr(UO, "PACE_BUCKET_SHARE", 0.0)
        _run("DDD")
        assert clk.sleeps == []
