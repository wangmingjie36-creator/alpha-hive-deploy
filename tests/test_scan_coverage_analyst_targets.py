"""Step 12 覆盖率闸：分析师目标价（`ChronosBeeHorizon.analyst_targets`）。v0.45.443

起因：08-04 起 38 个扫描日里 8 天 0/30（08-04/06/11/12/14、09-17/24/25）、5 天部分缺失（16~22/30），此前没有任何会红的观测点——
yfinance `analyst_price_targets` 失败时返回空 `{}`，与「该票没有分析师覆盖」不可区分；数据质量标签 `unavailable` 只占几十个标签之一。
缺它 ⇒ Chronos confidence 少 0.1、目标价卡片空白。**只观测，不改评分。**

本文件钉住：1) 闸看得见它；2) 判据与**生产者**（真跑 `ChronosBeeHorizon.analyze`）对得上——失败时产出的正是闸判「缺」的那种形状；
3) 阈值与历史实测日对得上；4) 旧结果（有这个键）照常判。
"""
from __future__ import annotations

import json

import pytest

import scan_coverage_gate as G


def _results(n, ok):
    out = {}
    for i in range(n):
        det = {"rv_30d": 1.0, "iv_rank": 1.0, "iv_current": 1.0, "iv_skew_ratio": 1.0, "put_call_ratio": 1.0,
               "iv_rv_spread": 1.0, "unusual_flow_ok": True}
        out[f"T{i}"] = {"agent_details": {
            "OracleBeeEcho": {"details": det},
            "ChronosBeeHorizon": {"details": {"catalysts": [1],
                                              "analyst_targets": {"target_mean": 10.0} if i < ok else {}}}}}
    return out


def _check(tmp_path, results):
    p = tmp_path / ".swarm_results_2026-10-09.json"
    p.write_text(json.dumps(results))
    return G.check("2026-10-09", results_path=p)


def _row(res):
    return next(r for r in res["fields"] if r["field"] == "analyst_targets")


class TestGateSeesAnalystTargets:
    def test_healthy_day(self, tmp_path):
        res = _check(tmp_path, _results(30, 30))
        assert res["healthy"] and not _row(res)["degraded"] and _row(res)["have"] == 30

    @pytest.mark.parametrize("ok", [0, 16, 18])
    def test_historical_bad_day_shapes_are_degraded(self, tmp_path, ok):
        """0/30 = 09-17 / 09-24 / 09-25（及 08 月多天）；16、18/30 = 08-26、08-25 的实测形状。"""
        res = _check(tmp_path, _results(30, ok))
        assert _row(res)["degraded"] and "analyst_targets" in res["degraded_fields"] and not res["healthy"]
        assert G._exit_code(res) == 1
        if G.step_contract:
            assert any("analyst_targets" in str(a) for a in G.contract_attention(res))

    @pytest.mark.parametrize("ok", [22, 21])
    def test_historical_partial_but_acceptable_days_do_not_alarm(self, tmp_path, ok):
        """08-24 / 08-13（22/30）与 08-10（21/30）：低于满额但仍在 0.70 之上——闸不能过敏。"""
        res = _check(tmp_path, _results(30, ok))
        assert not _row(res)["degraded"], (ok, _row(res))

    def test_threshold_is_the_same_as_the_other_yfinance_fields(self):
        spec = next(s for s in G.FIELDS if s["key"] == "analyst_targets")
        assert spec["min_coverage"] == 0.70 and "recorded_key" not in spec, \
            "旧记录一直带这个键（Chronos 始终写），不需要「未记录」豁免"

    def test_rendered_text_names_the_field(self, tmp_path):
        assert "analyst_targets" in G._render(_check(tmp_path, _results(30, 0)))


class TestMatchesTheProducer:
    """闸的判据（`details.analyst_targets` 非空）必须与 Chronos 真实产出的失败 / 成功形状一致，
    否则闸与生产者各说各话（这条链上一次这样出事见 v0.45.421 的 `False` 被当「有值」）。"""

    @pytest.fixture
    def chronos(self, board, monkeypatch):
        import pandas as pd
        import yfinance as yf
        from swarm_agents.chronos_bee import ChronosBeeHorizon

        class _T:
            def __init__(self, *a, **k):
                pass

            @property
            def calendar(self):
                return {}

            @property
            def info(self):
                return {}

            def history(self, *a, **k):
                return pd.DataFrame()

        monkeypatch.setattr(yf, "Ticker", _T)
        bee = ChronosBeeHorizon(board)
        monkeypatch.setattr(bee, "_get_stock_data", lambda t: {"price": 100.0})
        return bee

    def _gate_says_present(self, result):
        tr = {"agent_details": {"ChronosBeeHorizon": {"details": result.get("details")}}}
        return G._present(G._dig(tr, "ChronosBeeHorizon.analyst_targets"))

    @pytest.mark.parametrize("apt", [None, {}, {"mean": 0}, {"low": 1, "high": 2}])
    def test_failure_shapes_are_missing_to_the_gate(self, chronos, monkeypatch, apt):
        """yfinance 失败 / 空 `{}` / 均价 0 ⇒ 产出 `{}` ⇒ 闸判缺。"""
        monkeypatch.setattr(chronos, "_yf_analyst_targets", lambda t: apt)
        r = chronos.analyze("ABBV")
        assert "error" not in r, r.get("error")
        assert r["details"]["analyst_targets"] == {} and not self._gate_says_present(r)

    def test_valid_targets_are_present_to_the_gate(self, chronos, monkeypatch):
        monkeypatch.setattr(chronos, "_yf_analyst_targets",
                            lambda t: {"low": 80.0, "high": 140.0, "mean": 120.0, "median": 118.0})
        r = chronos.analyze("ABBV")
        assert r["details"]["analyst_targets"]["target_mean"] == 120.0 and self._gate_says_present(r)

    def test_no_trusted_price_clears_the_card_and_the_gate_sees_it(self, chronos, monkeypatch):
        """有目标价却没有可信现价 ⇒ 生产者清空（宁可不展示卡片）⇒ 闸同样判缺：卡片没出就是缺。"""
        monkeypatch.setattr(chronos, "_yf_analyst_targets", lambda t: {"mean": 120.0})
        monkeypatch.setattr(chronos, "_get_stock_data", lambda t: {})
        r = chronos.analyze("ABBV")
        assert r["details"]["analyst_targets"] == {} and not self._gate_says_present(r)
