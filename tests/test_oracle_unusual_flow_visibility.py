"""v0.45.421：Oracle 异常期权流（yfinance 链）取数失败可见化——区分「取数失败」与「无异常」，不改评分。

起因：2026-10-05 重跑时 28/30 只的异常流整路丢失（Oracle bullish 18→9），而 `unusual_options` 把「每个到期日都取失败」
返回成「无异常期权流信号 / neutral / 5.0 / data_source=yfinance_chain」（还缓存 300 秒），日志里只有 2 条 warning。
本版**只加可见性**：取数状态进输出、全败不缓存且打 WARNING、降级比例进 Step 12 覆盖率闸。评分逐位不变（有专门一条钉住）。
"""
from __future__ import annotations

import datetime as dt
import logging
import sys
import types

import pandas as pd
import pytest

import unusual_options as UO


def _exp(days):
    return (dt.datetime.now() + dt.timedelta(days=days)).strftime("%Y-%m-%d")


class _Chain:
    def __init__(self, calls, puts):
        self.calls, self.puts = calls, puts


def _df(rows):
    return pd.DataFrame(rows, columns=["strike", "volume", "openInterest", "lastPrice", "impliedVolatility"])


class _FakeTicker:
    """`fail` = 取数失败的到期日集合；`options` 可置空模拟 yfinance 吞异常后的空列表；`calls` 记每次取链。"""
    calls_log: list = []

    def __init__(self, ticker, options=None, fail=(), raises=None):
        self.options = options if options is not None else [_exp(7), _exp(14), _exp(30), _exp(45)]
        self._fail, self._raises = set(fail), raises

    def option_chain(self, exp):
        _FakeTicker.calls_log.append(exp)
        if exp in self._fail:
            raise ConnectionError("TLS connect error (fake)")
        # 一笔明显的新建仓大单 call：vol/oi=10 ⇒ 触发 Vol/OI Sweep
        return _Chain(_df([[100.0, 1000, 100, 5.0, 0.3]]), _df([]))


@pytest.fixture
def fake_yf(monkeypatch):
    UO._CACHE.clear()
    UO._CACHE_TS.clear()
    _FakeTicker.calls_log = []
    state = {"options": None, "fail": set(), "raises": None}

    def make(ticker):
        if state["raises"]:
            raise state["raises"]
        return _FakeTicker(ticker, options=state["options"], fail=state["fail"])
    mod = types.SimpleNamespace(Ticker=make)
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    yield state
    UO._CACHE.clear()
    UO._CACHE_TS.clear()


class TestFetchStatusIsSeparateFromNoUnusual:
    def test_all_chains_ok(self, fake_yf):
        r = UO.detect_unusual_flow("AAA", stock_price=100.0)
        assert r["fetch_status"] == "ok" and r["data_source"] == "yfinance_chain"
        assert r["chains_total"] == 4 and r["chains_failed"] == 0 and r["failure_reason"] == ""
        assert r["unusual_direction"] == "bullish" and r["signals"]

    def test_some_chains_failed_is_partial_and_warns(self, fake_yf, caplog):
        fake_yf["fail"] = {_exp(14), _exp(30)}
        with caplog.at_level(logging.WARNING, logger="alpha_hive.unusual_options"):
            r = UO.detect_unusual_flow("AAA", stock_price=100.0)
        assert r["fetch_status"] == "partial" and r["chains_failed"] == 2 and r["data_source"] == "yfinance_chain"
        assert "2/4" in r["failure_reason"]
        assert any("部分期权链取数失败" in m for m in caplog.messages), caplog.messages

    def test_all_chains_failed_is_a_failure_not_a_quiet_neutral(self, fake_yf, caplog):
        fake_yf["fail"] = {_exp(7), _exp(14), _exp(30), _exp(45)}
        with caplog.at_level(logging.WARNING, logger="alpha_hive.unusual_options"):
            r = UO.detect_unusual_flow("AAA", stock_price=100.0)
        assert r["fetch_status"] == "failed" and r["data_source"] == "fallback"
        assert r["chains_total"] == 4 and r["chains_failed"] == 4
        assert "全部 4 个到期日取数失败" in r["failure_reason"] and "TLS" in r["failure_reason"]
        assert any("全部 4 个到期日期权链取数失败" in m for m in caplog.messages), caplog.messages

    def test_all_failed_is_not_cached(self, fake_yf):
        """旧行为：全败的「无异常」被缓存 300 秒，同一轮里后来的读者拿到的还是它。"""
        fake_yf["fail"] = {_exp(7), _exp(14), _exp(30), _exp(45)}
        UO.detect_unusual_flow("AAA", stock_price=100.0)
        n1 = len(_FakeTicker.calls_log)
        UO.detect_unusual_flow("AAA", stock_price=100.0)
        assert len(_FakeTicker.calls_log) == 2 * n1, "全败的结果被缓存了：第二次没有重新取数"

    def test_success_is_still_cached(self, fake_yf):
        UO.detect_unusual_flow("AAA", stock_price=100.0)
        n1 = len(_FakeTicker.calls_log)
        UO.detect_unusual_flow("AAA", stock_price=100.0)
        assert len(_FakeTicker.calls_log) == n1

    def test_no_expiration_list_is_failed_with_reason(self, fake_yf):
        fake_yf["options"] = []
        r = UO.detect_unusual_flow("AAA", stock_price=100.0)
        assert r["fetch_status"] == "failed" and r["data_source"] == "fallback" and "yfinance 无法区分" in r["failure_reason"]

    def test_ticker_exception_is_failed_with_reason(self, fake_yf):
        fake_yf["raises"] = RuntimeError("boom")
        r = UO.detect_unusual_flow("AAA", stock_price=100.0)
        assert r["fetch_status"] == "failed" and "RuntimeError" in r["failure_reason"]

    def test_failure_shape_is_unchanged_for_scoring(self, fake_yf):
        """评分逐位不变的前提：全败结果的评分相关字段与旧的「无异常」完全相同（5.0 / neutral / 无信号）。"""
        fake_yf["fail"] = {_exp(7), _exp(14), _exp(30), _exp(45)}
        r = UO.detect_unusual_flow("AAA", stock_price=100.0)
        assert (r["unusual_score"], r["unusual_direction"], r["signals"]) == (5.0, "neutral", [])


class TestOracleRecordsTheStatusWithoutChangingTheScore:
    @staticmethod
    def _stub_options(monkeypatch):
        """OptionsAgent 会真去取 CBOE / Yahoo 链（测试默认离线）——桩成固定结果，本文件只关心异常流这一路。"""
        import options_analyzer

        class _Agent:
            def analyze(self, ticker, stock_price=0.0):
                return {"options_score": 6.0, "signal_summary": "平衡", "data_quality": "real"}
        monkeypatch.setattr(options_analyzer, "OptionsAgent", _Agent)

    @classmethod
    def _run(cls, oracle, monkeypatch, flow):
        cls._stub_options(monkeypatch)
        monkeypatch.setattr(UO, "detect_unusual_flow", lambda ticker, stock_price=0.0: dict(flow))
        return oracle.analyze("NVDA")

    LEGACY_QUIET = {"unusual_score": 5.0, "unusual_direction": "neutral", "signals": [],
                    "summary": "无异常期权流信号", "data_source": "yfinance_chain"}
    NEW_FAILED = {"unusual_score": 5.0, "unusual_direction": "neutral", "signals": [], "summary": "期权链全部取数失败",
                  "data_source": "fallback", "fetch_status": "failed", "failure_reason": "全部 4 个到期日取数失败：x",
                  "chains_total": 4, "chains_failed": 4}

    def test_score_and_direction_identical_to_the_legacy_quiet_result(self, all_agents, monkeypatch):
        a = self._run(all_agents["oracle"], monkeypatch, self.LEGACY_QUIET)
        b = self._run(all_agents["oracle"], monkeypatch, self.NEW_FAILED)
        assert "error" not in a and "error" not in b
        assert (a["score"], a["direction"]) == (b["score"], b["direction"]), "只加可见性：评分必须逐位不变"

    def test_details_record_failure_as_none_and_success_as_true(self, all_agents, monkeypatch):
        ok = dict(self.LEGACY_QUIET, fetch_status="ok", chains_total=4, chains_failed=0, failure_reason="")
        d_ok = self._run(all_agents["oracle"], monkeypatch, ok)["details"]
        d_bad = self._run(all_agents["oracle"], monkeypatch, self.NEW_FAILED)["details"]
        assert d_ok["unusual_flow_ok"] is True and d_ok["unusual_flow_status"]["status"] == "ok"
        assert "unusual_flow_ok" in d_bad and d_bad["unusual_flow_ok"] is None, \
            "失败必须是 None（覆盖率闸把 False 当有值），且键必须在（键不在 = 旧代码产出）"
        assert d_bad["unusual_flow_status"]["status"] == "failed" and "取数失败" in d_bad["unusual_flow_status"]["reason"]

    def test_partial_counts_as_data_obtained(self, all_agents, monkeypatch):
        part = dict(self.LEGACY_QUIET, fetch_status="partial", chains_total=4, chains_failed=1, failure_reason="1/4")
        assert self._run(all_agents["oracle"], monkeypatch, part)["details"]["unusual_flow_ok"] is True

    def test_detector_never_ran_is_recorded_not_silent(self, all_agents, monkeypatch):
        def boom(ticker, stock_price=0.0):
            raise ConnectionError("down")
        self._stub_options(monkeypatch)
        monkeypatch.setattr(UO, "detect_unusual_flow", boom)
        d = all_agents["oracle"].analyze("NVDA")["details"]
        assert d["unusual_flow_ok"] is None and d["unusual_flow_status"]["status"] == "not_run"


# ── Step 12 覆盖率闸：降级比例进 FIELDS ──────────────────────────────────
import scan_coverage_gate as G  # noqa: E402


def _results(n, ok, key=True):
    out = {}
    for i in range(n):
        det = {"rv_30d": 1.0, "iv_rank": 1.0, "iv_current": 1.0, "iv_skew_ratio": 1.0, "put_call_ratio": 1.0,
               "iv_rv_spread": 1.0}
        if key:
            det["unusual_flow_ok"] = True if i < ok else None
        out[f"T{i}"] = {"agent_details": {"OracleBeeEcho": {"details": det},
                                          "ChronosBeeHorizon": {"details": {"catalysts": [1]}}}}
    return out


def _check(tmp_path, results):
    import json
    p = tmp_path / ".swarm_results_2026-10-05.json"
    p.write_text(json.dumps(results))
    return G.check("2026-10-05", results_path=p)


def _row(res):
    return next(r for r in res["fields"] if r["field"] == "unusual_flow")


class TestCoverageGateSeesUnusualFlow:
    def test_healthy_day(self, tmp_path):
        res = _check(tmp_path, _results(30, 28))
        assert res["healthy"] and not _row(res)["degraded"] and _row(res)["have"] == 28

    def test_oct5_rerun_shape_is_degraded_and_raises_attention(self, tmp_path):
        res = _check(tmp_path, _results(30, 2))
        assert _row(res)["degraded"] and "unusual_flow" in res["degraded_fields"] and not res["healthy"]
        assert G._exit_code(res) == 1
        att = G.contract_attention(res) if G.step_contract else []
        if G.step_contract:
            assert any("unusual_flow" in str(a) for a in att), att

    def test_all_failed_with_key_present_is_degraded_not_unrecorded(self, tmp_path):
        res = _check(tmp_path, _results(30, 0))
        assert _row(res)["degraded"] and not _row(res).get("not_recorded")

    def test_old_results_without_the_key_are_unrecorded_not_degraded(self, tmp_path):
        res = _check(tmp_path, _results(30, 0, key=False))
        assert _row(res).get("not_recorded") is True and not _row(res)["degraded"] and res["healthy"]
        assert "未记录" in G._render(res)

    def test_threshold_is_the_same_as_the_other_fields(self):
        spec = next(s for s in G.FIELDS if s["key"] == "unusual_flow")
        assert spec["min_coverage"] == 0.70
