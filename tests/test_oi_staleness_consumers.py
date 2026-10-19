"""OI 陈旧链：所有读 OI 的 yfinance 入口与下游都按「不可得」处理（v0.45.542，`oi_staleness`）。

v0.45.512 只把主链的 P/C / total_oi / GEX 记成 None；同一条链上读 OI 的还有：异动检测（陈旧日每个有成交合约 OI 0 被当成
1 ⇒ 全成「扫单」、看涨条数进 options_score——10-12 BILI 实测旧逻辑 9 条异动 / 5 条看涨 / 9 条扫单，新逻辑 1 / 0 / 0）、
关键价位（挑出的正是 $5 put 22,065 / $8 call 74 那几档冻结旧数）、call 流分类第一票、gamma 日历的近月退路、全链 OI 的
yfinance 降级（10-12 记过全链 P/C 0.0）、Oracle 的 `unusual_options`（10-12 录制 30/30 判陈旧）、MCP 快照。另：P/C 在
OI 全零时退到成交量（另一个量照样叫 put_call_ratio）——一并去掉。

每条都配健康链对照（同一夹具 OI 正常 ⇒ 照旧），证明变化来自判据、不是夹具碰巧缺数据。
"""
from __future__ import annotations

import asyncio
import json
import logging
import types

import pandas as pd
import pytest

import oi_staleness
import options_analyzer as oa
import unusual_options as UO
from tests.test_yfinance_oi_stale import PRICE, RECENT, _chain_frames, _exp, _fetcher
from tests.test_yahoo_iv_placeholders import cache_writes, iv_agent  # noqa: F401  (夹具)

A = oa.OptionsAnalyzer()


# ═══════════════════════════════ 共用判据 ═══════════════════════════════

class TestSharedJudge:
    def test_options_analyzer_uses_the_shared_module(self):
        """只有一份判据：options_analyzer 的名字是 oi_staleness 的别名（改一处全仓生效）。"""
        assert oa.assess_oi_staleness is oi_staleness.assess
        assert oa._oi_staleness_rows is oi_staleness.rows_from_frame

    def test_assess_frames_pools_all_frames(self):
        frames = [f for pair in _chain_frames(stale=True) for f in pair]
        assert oi_staleness.assess_frames(frames)["stale"] is True
        healthy = [f for pair in _chain_frames(stale=False) for f in pair]
        assert oi_staleness.assess_frames(healthy)["stale"] is False


# ═══════════════════════════════ P/C 不再退到成交量 ═══════════════════════════════

def test_put_call_ratio_has_no_volume_fallback():
    legs = lambda oi, vol: [{"openInterest": oi, "volume": vol, "dte_weight": 1.0}]  # noqa: E731
    assert A.calculate_put_call_ratio(legs(0, 40), legs(0, 20)) is None
    assert A.calculate_put_call_ratio(legs(100, 0), legs(50, 0)) == pytest.approx(0.5)


# ═══════════════════════════════ 异动检测 / call 流 ═══════════════════════════════

class TestUnusualActivity:
    def _legs(self, oi):
        return [{"strike": 16.0, "volume": 300, "openInterest": oi, "lastPrice": 0.5, "expiry": _exp(30)}]

    def test_unreliable_oi_skips_vol_oi_conditions(self):
        """OI 不可信 ⇒ 不判「Vol/OI 扫单」「OTM 投机」，条目 oi / ratio 为 None。变红的变异：忽略 oi_reliable。"""
        assert A.detect_unusual_activity(self._legs(0), [], PRICE) and \
            "sweep" in A.detect_unusual_activity(self._legs(0), [], PRICE)[0]["type"]
        assert A.detect_unusual_activity(self._legs(0), [], PRICE, oi_reliable=False) == []

    def test_volume_only_conditions_still_fire(self):
        big = [{"strike": 16.0, "volume": 20000, "openInterest": 0, "lastPrice": 0.5, "expiry": _exp(30)}]
        out = A.detect_unusual_activity(big, [], PRICE, oi_reliable=False)
        assert len(out) == 1 and out[0]["oi"] is None and out[0]["ratio"] is None and "sweep" not in out[0]["type"]

    def test_entry_records_real_oi_not_one(self):
        """原 `or 1` 把 OI 0 记成 1（只是展示，比率仍按 max(oi,1)）。"""
        assert A.detect_unusual_activity(self._legs(0), [], PRICE)[0]["oi"] == 0


def test_call_flow_vote_a_not_cast_on_stale_oi():
    calls = [{"expiry": _exp(120), "openInterest": 5000}, {"expiry": _exp(10), "openInterest": 10}]
    assert A.classify_call_flow(calls, [], PRICE)["votes"]["A"] == "hedge"
    assert A.classify_call_flow(calls, [], PRICE, oi_reliable=False)["votes"]["A"] == "unknown"


# ═══════════════════════════════ analyze 端到端 ═══════════════════════════════

def _busy_frames(stale):
    """端到端用的链：近价合约有成交（300 手 ⇒ 旧逻辑在陈旧日会把它们判成扫单），远月（第 4 个到期日）再放一个
    OI 冻结的大 call（⇒ 旧逻辑的 call 流第一票会被它投成「对冲」）。健康链同形，只是 OI 正常。"""
    from tests.test_yfinance_oi_stale import OLD_CALL, _rows
    frames = []
    for i, (c, p) in enumerate(_chain_frames(stale)):
        c, p = c.copy(), p.copy()
        c["volume"], p["volume"] = 300, 300
        if i == 3:
            c = pd.concat([c, pd.DataFrame(_rows([9.0], [50000], [OLD_CALL]))], ignore_index=True)
        frames.append((c, p))
    return frames


def _analyze(agent, monkeypatch, stale):
    import cboe_options
    monkeypatch.setattr(cboe_options, "fetch_cboe_chain_for_gex", lambda *a, **k: None)  # 走近月退路
    chain = _fetcher(monkeypatch, frames=_busy_frames(stale)).fetch_options_chain("BILI")
    monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: chain)
    return agent.analyze("BILI", stock_price=PRICE)


class TestAnalyzeDownstream:
    def test_stale_chain_all_oi_derived_values_unavailable(self, iv_agent, monkeypatch):  # noqa: F811
        """变红的变异：关键价位 / gamma 日历 / call 流 / 异动任一处仍读陈旧 OI。"""
        r = _analyze(iv_agent, monkeypatch, stale=True)
        assert r["key_levels"] == {}
        assert r["gamma_calendar"] == {}
        assert r["call_flow_classification"]["votes"]["A"] == "unknown"
        assert all(u["oi"] is None and "sweep" not in u["type"] for u in r["unusual_activity"])

    def test_healthy_chain_unchanged(self, iv_agent, monkeypatch):  # noqa: F811
        r = _analyze(iv_agent, monkeypatch, stale=False)
        assert r["key_levels"].get("resistance") and r["key_levels"].get("support")
        assert r["gamma_calendar"]

    def test_the_busy_fixture_really_trips_the_old_logic(self, iv_agent, monkeypatch):  # noqa: F811
        """夹具自检：陈旧链上若照旧读 OI，会出扫单、call 流第一票会投出来——否则上面那条绿得没意义。"""
        chain = _fetcher(monkeypatch, frames=_busy_frames(True)).fetch_options_chain("BILI")
        assert chain["oi_check"]["stale"]
        old = A.detect_unusual_activity(chain["calls"], chain["puts"], PRICE)
        assert any("sweep" in u["type"] and u["bullish"] for u in old)
        assert A.classify_call_flow(chain["calls"], chain["puts"], PRICE)["votes"]["A"] != "unknown"


# ═══════════════════════════════ 全链 OI 的 yfinance 降级 ═══════════════════════════════

class TestFullChainOiFallback:
    def _run(self, monkeypatch, stale):
        import cboe_options
        monkeypatch.setattr(cboe_options, "fetch_cboe_full_chain_oi", lambda *a, **k: None)
        _fetcher(monkeypatch, stale=stale)          # 装好 oa.yf 的假链
        oa.reset_snapshot_slot_stats()
        return oa.OptionsAgent._fetch_full_chain_oi("BILI", PRICE)

    def test_stale_is_unavailable_and_counted(self, monkeypatch, caplog):
        """10-12 记过全链 P/C 0.0。变红的变异：降级路径不过判据。"""
        with caplog.at_level(logging.WARNING):
            out = self._run(monkeypatch, stale=True)
        assert out == {}
        assert oa.snapshot_slot_stats()["oi_stale_chains"] == 1
        assert any("全链 OI（yfinance 降级）陈旧" in m for m in caplog.messages)
        oa.reset_snapshot_slot_stats()

    def test_healthy_fallback_unchanged(self, monkeypatch):
        out = self._run(monkeypatch, stale=False)
        assert out and out["full_pc_ratio"] > 0 and out["expiry_count"] == 4
        oa.reset_snapshot_slot_stats()


# ═══════════════════════════════ Oracle 的 unusual_options ═══════════════════════════════

@pytest.fixture
def uo(monkeypatch):
    monkeypatch.setattr(UO, "_sleep", lambda s: None)
    monkeypatch.setattr(UO, "_retry_exhausted", 0)
    monkeypatch.setattr(UO, "PACE_BUCKET_SHARE", 0.0)
    UO._CACHE.clear()
    UO._CACHE_TS.clear()

    def install(stale):
        frames = _chain_frames(stale)
        exps = [_exp(d) for d in (7, 14, 30, 45)]
        for c, p in frames:                      # 近价合约放大成交量，健康链才会有真实异动
            c["volume"] = 400
            p["volume"] = 400

        class _T:
            options = tuple(exps)

            def __init__(self, t):
                pass

            def option_chain(self, e):
                c, p = frames[exps.index(e)]
                return types.SimpleNamespace(calls=c.copy(), puts=p.copy())
        import sys
        monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_T))
    yield install
    UO._CACHE.clear()
    UO._CACHE_TS.clear()


class TestUnusualOptionsModule:
    def test_stale_is_a_fallback_not_a_direction(self, uo, caplog):
        """10-12 BILI 的 Oracle「看涨」出自这里。变红的变异：模块不过判据（照样给方向 / 分数）。"""
        uo(stale=True)
        with caplog.at_level(logging.WARNING, logger="alpha_hive.unusual_options"):
            r = UO.detect_unusual_flow("BILI", stock_price=PRICE)
        assert r["fetch_status"] == "oi_stale" and r["data_source"] == "fallback"
        assert r["unusual_direction"] == "neutral" and r["signals"] == [] and r["oi_check"]["stale"]
        assert "BILI" not in UO._CACHE, "陈旧结果不许缓存"
        assert any("OI 陈旧" in m for m in caplog.messages)

    def test_healthy_is_ok(self, uo):
        uo(stale=False)
        r = UO.detect_unusual_flow("BILI", stock_price=PRICE)
        assert r["fetch_status"] == "ok" and r["data_source"] != "fallback"

    def test_oracle_treats_it_as_unavailable(self):
        from swarm_agents.oracle_bee import _unusual_flow_ok
        assert _unusual_flow_ok({"fetch_status": "oi_stale", "data_source": "fallback"}) is None


# ═══════════════════════════════ MCP 快照 ═══════════════════════════════

class TestMcpSnapshot:
    def _call(self, monkeypatch, stale, frames=None):
        import alpha_hive_mcp as m
        frames = frames or _chain_frames(stale)
        exps = [_exp(d) for d in (30, 60, 90, 120)]

        class _T:
            options = tuple(exps)

            def __init__(self, t):
                pass

            def option_chain(self, e):
                c, p = frames[exps.index(e)]
                return types.SimpleNamespace(calls=c.copy(), puts=p.copy())
        monkeypatch.setattr(m.yf, "Ticker", _T)
        fn = getattr(m.alphahive_get_options_snapshot, "fn", m.alphahive_get_options_snapshot)
        return json.loads(asyncio.run(fn(m.TickerInput(ticker="BILI"))))

    def test_stale_oi_fields_are_none(self, monkeypatch):
        out = self._call(monkeypatch, stale=True)
        assert out["oi_stale"] is True
        assert out["put_call_oi_ratio"] is None and out["total_call_oi"] is None and out["highest_oi_put"] is None
        assert out["total_call_volume"] == int(_chain_frames(True)[0][0]["volume"].sum()) > 0   # 成交量照给

    def test_healthy_unchanged(self, monkeypatch):
        out = self._call(monkeypatch, stale=False)
        assert out["oi_stale"] is False and out["put_call_oi_ratio"] and out["highest_oi_call"]

    def test_one_odd_nearest_expiry_is_not_judged_alone(self, monkeypatch):
        """v0.45.544：10-19 SNOW 形状——最近到期日 OI 成片 0（新挂周权 / 发布不全），其余到期日正常 ⇒ 不判陈旧。
        原实现只看最近一个到期日 ⇒ 正常日误判。变红的变异：MCP 只拿最近一个到期日来判。"""
        frames = _chain_frames(stale=False)
        frames[0] = _chain_frames(stale=True)[0]
        out = self._call(monkeypatch, stale=False, frames=frames)
        assert out["oi_stale"] is False and out["highest_oi_call"] is not None


def test_recent_constant_is_shared():
    assert RECENT is not None and oi_staleness.MIN_RECENT == 20 and oi_staleness.MAX_RATIO == 0.25
    assert isinstance(pd.Timestamp(RECENT), pd.Timestamp)
