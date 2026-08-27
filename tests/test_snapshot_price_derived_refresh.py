"""快照里价格历史派生字段必须可重算（v0.45.43）

事故：2026-08-26 14:13 抓期权快照时 yfinance 正好挂了，29/30 份快照把
`rv_30d: null` / `iv_rank: null` 一起冻了进去。当晚 yfinance 早已恢复，
**重跑扫描却原样复现失败** —— 快照命中即 return，根本没再去算。
一次瞬时故障被快照机制升级成了当日永久缺失。

根因是快照混着两类数据：
  · 期权链（IV/OI/strike）—— 接口只有实时快照、无历史，错过即永久丢失，必须冻结
  · 价格历史派生（rv_30d / iv_rv_* / hv_proxy 口径 iv_rank）—— 日K 随时可重拉

本文件锁住这条分界：**日K 派生的可重算，期权链的绝不许重算**（后者重算=伪造）。
"""

import json

import pytest

from options_analyzer import OptionsAgent


@pytest.fixture
def agent():
    return OptionsAgent()


def _snap(**over):
    d = {
        "_snapshot_ticker": "NVDA",
        "_snapshot_timestamp": "2026-08-26T14:13:23",
        "iv_current": 48.61,
        "iv_rank": None,
        "iv_rank_source": "hv_proxy",
        "rv_30d": None,
        "iv_rv_spread": None,
        "iv_rv_signal": "unknown",
        # 期权链字段 —— 任何情况下都不许被改动
        "total_oi": 1234567,
        "put_call_ratio": 0.93,
        "iv_skew_ratio": 0.98,
        "key_levels": {"support": [{"strike": 200}]},
    }
    d.update(over)
    return d


class TestRecomputesWhenMissing:
    def test_rv_recomputed(self, agent, monkeypatch):
        monkeypatch.setattr(
            "market_intelligence.calculate_iv_rv_spread",
            lambda t, iv, **k: {"rv_30d": 36.14, "iv_rv_spread": 12.47,
                                "iv_rv_signal": "expensive", "data_available": True},
        )
        c = _snap()
        assert agent._refresh_price_derived(c, "NVDA") is True
        assert c["rv_30d"] == 36.14
        assert c["iv_rv_spread"] == 12.47
        assert c["iv_rv_signal"] == "expensive"

    def test_iv_rank_recomputed_for_hv_proxy(self, agent, monkeypatch):
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv",
                            lambda t, days=252: [30.0, 35.0, 40.0, 37.0])
        monkeypatch.setattr(agent.fetcher, "last_hist_hv_is_sample", False)
        monkeypatch.setattr(agent.analyzer, "calculate_iv_rank",
                            lambda cur, hist: (37.81, None))
        monkeypatch.setattr(agent.analyzer, "calculate_iv_percentile",
                            lambda cur, hist: 62.0)
        c = _snap()
        agent._refresh_price_derived(c, "NVDA")
        assert c["iv_rank"] == 37.81
        assert c["iv_percentile"] == 62.0


class TestDoesNotOverreach:
    def test_option_chain_fields_never_touched(self, agent, monkeypatch):
        """期权链字段错过即永久丢失，重算等于伪造 —— 必须原样不动"""
        monkeypatch.setattr(
            "market_intelligence.calculate_iv_rv_spread",
            lambda t, iv, **k: {"rv_30d": 36.1, "iv_rv_spread": 1.0,
                                "iv_rv_signal": "fair", "data_available": True},
        )
        c = _snap()
        frozen = {k: c[k] for k in ("total_oi", "put_call_ratio",
                                    "iv_skew_ratio", "key_levels", "iv_current")}
        agent._refresh_price_derived(c, "NVDA")
        for k, v in frozen.items():
            assert c[k] == v, f"期权链字段 {k} 被改动了"

    def test_real_iv_source_not_recomputed(self, agent, monkeypatch):
        """real_iv_* 口径来自自攒 IV 观测库，不是日K 派生。
        用 HV 冒充它正是 v0.43.19 修掉的失真，绝不许在这里复活。"""
        called = []
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv",
                            lambda t, days=252: called.append(t) or [30.0, 40.0])
        c = _snap(iv_rank_source="real_iv_90d")
        agent._refresh_price_derived(c, "NVDA")
        assert c["iv_rank"] is None, "real_iv 口径缺失就是真缺，不该用 HV 顶上"
        assert not called, "不该为 real_iv 口径去拉 HV"

    def test_healthy_snapshot_makes_no_network_call(self, agent, monkeypatch):
        """快照健康时不该多打一次外网"""
        def _boom(*a, **k):
            raise AssertionError("健康快照不该触发重算")
        monkeypatch.setattr("market_intelligence.calculate_iv_rv_spread", _boom)
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv", _boom)
        c = _snap(rv_30d=30.0, iv_rank=45.0)
        assert agent._refresh_price_derived(c, "NVDA") is False


class TestFailureKeepsEmpty:
    def test_recompute_failure_leaves_none_not_zero(self, agent, monkeypatch):
        """重算失败保持「空」——写个假值比缺失更糟"""
        monkeypatch.setattr(
            "market_intelligence.calculate_iv_rv_spread",
            lambda t, iv, **k: {"rv_30d": None, "iv_rv_spread": None,
                                "iv_rv_signal": "unknown", "data_available": False,
                                "error": "yfinance 历史K线不足（0 根，需 ≥15）"},
        )
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv", lambda t, days=252: [])
        c = _snap()
        assert agent._refresh_price_derived(c, "NVDA") is False
        assert c["rv_30d"] is None
        assert c["iv_rank"] is None

    def test_exception_does_not_propagate(self, agent, monkeypatch):
        def _raise(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr("market_intelligence.calculate_iv_rv_spread", _raise)
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv", _raise)
        c = _snap()
        assert agent._refresh_price_derived(c, "NVDA") is False   # 不抛

    def test_sample_hv_rejected(self, agent, monkeypatch):
        """样本数据的 HV 不可信（v0.38.0 守卫），不该用来填 iv_rank"""
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv",
                            lambda t, days=252: [30.0, 40.0, 35.0])
        monkeypatch.setattr(agent.fetcher, "last_hist_hv_is_sample", True)
        c = _snap()
        agent._refresh_price_derived(c, "NVDA")
        assert c["iv_rank"] is None


class TestWriteBack:
    def test_writes_back_and_stamps(self, agent, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "market_intelligence.calculate_iv_rv_spread",
            lambda t, iv, **k: {"rv_30d": 36.1, "iv_rv_spread": 1.0,
                                "iv_rv_signal": "fair", "data_available": True},
        )
        p = tmp_path / "snap.json"
        c = _snap()
        p.write_text(json.dumps(c))
        agent._refresh_price_derived(c, "NVDA", str(p))
        on_disk = json.loads(p.read_text())
        assert on_disk["rv_30d"] == 36.1, "修复必须持久化，否则下次还得重算"
        assert "_price_derived_refreshed_at" in on_disk, "回写必须留痕"

    def test_writeback_failure_still_returns_fixed_value(self, agent, monkeypatch):
        """回写失败不该让本次修复作废"""
        monkeypatch.setattr(
            "market_intelligence.calculate_iv_rv_spread",
            lambda t, iv, **k: {"rv_30d": 36.1, "iv_rv_spread": 1.0,
                                "iv_rv_signal": "fair", "data_available": True},
        )
        c = _snap()
        assert agent._refresh_price_derived(c, "NVDA", "/nonexistent/dir/x.json") is True
        assert c["rv_30d"] == 36.1


class TestWiredIntoSnapshotHit:
    def test_analyze_calls_refresh_on_snapshot_hit(self):
        """回归闸：快照命中路径必须调用重算，否则这个 bug 会原样回来"""
        import inspect
        src = inspect.getsource(OptionsAgent.analyze)
        hit = src.split("期权快照命中")[1][:900]
        assert "_refresh_price_derived" in hit, \
            "快照命中后必须尝试重算价格派生字段，否则瞬时故障会被冻成当日永久缺失"
