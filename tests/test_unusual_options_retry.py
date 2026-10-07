"""异常期权流取数的重试与退避（只重试「令牌等待超时」与瞬时网络错误；429 / 冷却 / 其它不重试）。

依据：yf_gate 的设计「一次 429 的含义是现在就停，不是再试试」；而 2026-10-05 日志里能对上的失败是共享令牌桶排队超过 60s
与 `curl (35) TLS connect error`——错开即恢复型。每只标的共用 2 次预算（10s / 20s），进程内累计 N 只用光后不再重试。
"""
from __future__ import annotations

import datetime as dt
import sys
import types

import pandas as pd
import pytest

import unusual_options as UO

TOKEN = "等待 yfinance 限流令牌超过 60s（Ticker.options）"


def _exp(days):
    return (dt.datetime.now() + dt.timedelta(days=days)).strftime("%Y-%m-%d")


class YFRateLimited(ConnectionError):          # 与 yf_gate 同名：按类名识别 429 路径
    pass


class _Chain:
    def __init__(self):
        self.calls = pd.DataFrame([[100.0, 1000, 100, 5.0, 0.3]],
                                  columns=["strike", "volume", "openInterest", "lastPrice", "impliedVolatility"])
        self.puts = pd.DataFrame(columns=self.calls.columns)


@pytest.fixture
def env(monkeypatch):
    UO._CACHE.clear()
    UO._CACHE_TS.clear()
    monkeypatch.setattr(UO, "_retry_exhausted", 0)
    sleeps = []
    monkeypatch.setattr(UO, "_sleep", lambda s: sleeps.append(s))
    st = types.SimpleNamespace(opt_script=[], chain_script=[], sleeps=sleeps, opt_calls=0, chain_calls=0)

    class T:
        def __init__(self, ticker):
            pass

        @property
        def options(self):
            st.opt_calls += 1
            if st.opt_script:
                e = st.opt_script.pop(0)
                if e is not None:
                    raise e
            return [_exp(7), _exp(14)]

        def option_chain(self, exp):
            st.chain_calls += 1
            if st.chain_script:
                e = st.chain_script.pop(0)
                if e is not None:
                    raise e
            return _Chain()
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=T))
    yield st
    UO._CACHE.clear()
    UO._CACHE_TS.clear()


def _run(t="AAA"):
    return UO.detect_unusual_flow(t, stock_price=100.0)


class TestRetryOnlyForRecoverableKinds:
    def test_token_wait_twice_then_ok(self, env):
        env.opt_script = [ConnectionError(TOKEN), ConnectionError(TOKEN)]
        r = _run()
        assert r["fetch_status"] == "ok" and r["retries"] == 2 and env.sleeps == [10.0, 20.0]

    def test_transient_tls_on_one_chain_then_ok(self, env):
        env.chain_script = [ConnectionError("curl: (35) TLS connect error"), None, None]
        r = _run()
        assert r["fetch_status"] == "ok" and r["retries"] == 1 and r["chains_failed"] == 0 and env.sleeps == [10.0]

    def test_success_never_sleeps(self, env):
        r = _run()
        assert r["retries"] == 0 and env.sleeps == []

    def test_budget_is_shared_and_exhausts(self, env):
        env.chain_script = [ConnectionError(TOKEN)] * 20          # 每次取链都饥饿
        r = _run()
        assert r["fetch_status"] == "failed" and r["chains_failed"] == 2 and r["retries"] == 2
        assert env.sleeps == [10.0, 20.0], "预算只有 2 次：第二个到期日预算已空，应直接失败而不再睡"
        assert "[token_wait]" in r["failure_reason"]

    def test_cooldown_is_not_retried(self, env):
        env.opt_script = [YFRateLimited("yfinance 限流冷却中，还剩 100s")]
        r = _run()
        assert r["fetch_status"] == "failed" and env.sleeps == [] and env.opt_calls == 1
        assert "[cooldown]" in r["failure_reason"]

    def test_rate_limited_429_is_not_retried(self, env):
        env.chain_script = [YFRateLimited("Too Many Requests. Rate limited. Try after a while.")] * 5
        r = _run()
        assert env.sleeps == [] and r["retries"] == 0 and "[rate_limited]" in r["failure_reason"]

    def test_other_errors_are_not_retried(self, env):
        env.chain_script = [ValueError("坏数据")] * 5
        r = _run()
        assert env.sleeps == [] and r["fetch_status"] == "failed" and "[other]" in r["failure_reason"]


class TestCircuitBreaker:
    def test_after_n_tickers_exhaust_the_budget_retries_stop(self, env, monkeypatch):
        monkeypatch.setattr(UO, "RETRY_CIRCUIT_TRIP", 2)
        for tk in ("A1", "A2"):                                   # 两只把预算用光
            env.opt_script = [ConnectionError(TOKEN)] * 3
            _run(tk)
        assert UO._retry_exhausted == 2
        env.sleeps.clear()
        env.opt_script = [ConnectionError(TOKEN)] * 3
        r = _run("A3")
        assert env.sleeps == [] and r["retries"] == 0, "断路后饥饿也不再重试（避免把整轮拖长）"
        env.opt_script = []
        assert _run("A4")["fetch_status"] == "ok", "断路只停重试，不停取数"


class TestFailureKinds:
    @pytest.mark.parametrize("exc,kind", [
        (ConnectionError(TOKEN), "token_wait"),
        (YFRateLimited("yfinance 限流冷却中，还剩 5s"), "cooldown"),
        (YFRateLimited("Too Many Requests"), "rate_limited"),
        (ConnectionError("curl: (35) TLS connect error"), "transient"),
        (TimeoutError("timed out"), "transient"),
        (ValueError("x"), "other"),
    ])
    def test_classification(self, exc, kind):
        assert UO._failure_kind(exc) == kind

    def test_token_message_beats_connection_error_subclass(self):
        """YFRateLimited 继承 ConnectionError：令牌超时的那一条必须被认成可重试，而不是被归进 429。"""
        assert UO._failure_kind(YFRateLimited(TOKEN)) == "token_wait"


def test_oracle_status_carries_the_retry_count():
    from swarm_agents.oracle_bee import _unusual_flow_status
    st = _unusual_flow_status({"fetch_status": "ok", "data_source": "yfinance_chain", "chains_total": 4,
                               "chains_failed": 0, "retries": 2})
    assert st["retries"] == 2
