"""P/C 算不出 ⇒ None，不再是假中性 1.0（v0.45.516，`OptionsAnalyzer.calculate_put_call_ratio`）。

此前链空、或 call 侧 OI（与成交量）为 0 时返回 1.0「默认中立」——与真实的 1.00 同形，进了 `signal_archive`：
2026-06-17 VKTX（call OI 0、put OI 13）、CRCL（call OI 0、put OI 97）两行 P/C = 1.0 就是它（快照 key_levels 为证）。
归档里另 17 行 1.0 两侧 OI 正常、量级相当，是真实值四舍五入。

下游沿用 v0.45.512（OI 陈旧）已有的 None 处理：flow 分取中点 1.5、flow_direction "unknown"、data_quality 不标 real
（否则 scan_coverage_gate 的「real 却 P/C 为空」会红）。计数 `pc_unavailable` 按标的去重进 status.json / 摘要行。
"""
from __future__ import annotations

import logging

import pytest

import options_analyzer as oa
import scan_timing
from tests.test_yahoo_iv_placeholders import cache_writes, iv_agent  # noqa: F401  (夹具)

A = oa.OptionsAnalyzer()


def _legs(oi, vol=0):
    return [{"openInterest": o, "volume": vol, "dte_weight": 1.0} for o in oi]


class TestFunction:
    @pytest.mark.parametrize("calls,puts", [
        ([], []), ([], _legs([10])), (_legs([10]), []),
        (_legs([0, 0, 0]), _legs([10, 2, 1])),             # VKTX 06-17 形状
        (_legs([0, 0, 0]), _legs([96, 1, 0])),             # CRCL 06-17 形状
        (_legs([0, 0]), _legs([0, 0])),                     # OI 与成交量全 0
        (_legs([float("nan")]), _legs([5])),
        (_legs([5]), _legs([float("inf")])),               # 非有限比值
    ])
    def test_unavailable_is_none_not_one(self, calls, puts):
        """变红的变异：任一早退改回 `return 1.0`。"""
        assert A.calculate_put_call_ratio(calls, puts) is None

    def test_real_ratio_unchanged(self):
        assert A.calculate_put_call_ratio(_legs([1000, 500]), _legs([800, 400])) == pytest.approx(0.8)

    def test_a_genuine_one_is_still_one(self):
        """真实的 1.00 照旧（区分的是「算不出」与「算出来恰好 1」）。"""
        assert A.calculate_put_call_ratio(_legs([500]), _legs([500])) == 1.0

    def test_volume_fallback_still_used_when_both_oi_zero(self):
        assert A.calculate_put_call_ratio(_legs([0], vol=40), _legs([0], vol=20)) == pytest.approx(0.5)


def _chain(call_oi, put_oi=3000):
    calls = [{"strike": k, "openInterest": call_oi, "impliedVolatility": 0.4, "gamma": 0.05,
              "volume": 0, "expiry": "2026-11-20", "dte_weight": 1.0} for k in (280.0, 300.0, 320.0)]
    puts = [{"strike": k, "openInterest": put_oi, "impliedVolatility": 0.38, "gamma": 0.04,
             "volume": 0, "expiry": "2026-11-20", "dte_weight": 1.0} for k in (280.0, 300.0, 320.0)]
    return {"calls": calls, "puts": puts, "expirations": ["2026-11-20"], "source": "real"}


def _analyze(agent, monkeypatch, call_oi, ticker="VKTX"):
    monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(call_oi))
    return agent.analyze(ticker, stock_price=300.0)


class TestAnalyze:
    def test_no_call_oi_is_unavailable_and_not_real(self, iv_agent, monkeypatch, caplog):  # noqa: F811
        """变红的变异：DQ 降级只认 OI 陈旧（这里仍标 real）；不计数；flow_direction 冒充 neutral。"""
        with caplog.at_level(logging.WARNING):
            r = _analyze(iv_agent, monkeypatch, call_oi=0)
        assert r["put_call_ratio"] is None
        assert r["flow_direction"] == "unknown"
        assert r["data_quality"] == "degraded"
        assert oa.snapshot_slot_stats()["pc_unavailable"] == 1
        assert any("P/C 算不出" in rec.getMessage() for rec in caplog.records)

    def test_counted_once_per_ticker(self, iv_agent, monkeypatch):  # noqa: F811
        _analyze(iv_agent, monkeypatch, call_oi=0)
        _analyze(iv_agent, monkeypatch, call_oi=0)
        assert oa.snapshot_slot_stats()["pc_unavailable"] == 1

    def test_score_uses_the_midpoint_not_the_old_neutral(self, iv_agent, monkeypatch):  # noqa: F811
        """旧假中性 1.0 落在 [1.0, 1.5) 档得 1 分；现在 None 取中点 1.5。"""
        s_none, _ = A.generate_options_score(50.0, None, None, [])
        s_one, _ = A.generate_options_score(50.0, 1.0, None, [])
        assert s_none - s_one == pytest.approx(0.5)

    def test_healthy_chain_unchanged(self, iv_agent, monkeypatch):  # noqa: F811
        r = _analyze(iv_agent, monkeypatch, call_oi=3000)
        assert r["put_call_ratio"] == 1.0 and r["data_quality"] == "real" and r["flow_direction"] == "neutral"
        assert oa.snapshot_slot_stats()["pc_unavailable"] == 0


def test_summary_line_only_when_nonzero():
    base = {"writes": 30, "hits": 0, "session_mismatch": 0, "hits_before_close": 0}
    line = scan_timing.summary_line({"phases": {}, "counters": {"options_snapshot": {**base, "pc_unavailable": 2}}})
    assert "P/C不可得2只" in line
    line0 = scan_timing.summary_line({"phases": {}, "counters": {"options_snapshot": {**base, "pc_unavailable": 0}}})
    assert "P/C不可得" not in line0
