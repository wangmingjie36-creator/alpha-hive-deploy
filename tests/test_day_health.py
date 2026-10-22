"""日健康判定（v0.45.598，`day_health.py`）。

2026-10-14：代理探活失败 ⇒ 直连 ⇒ yfinance 从第一个请求起 429，`iv_rank` / `unusual_flow` / `analyst_targets` / `catalysts` 同日 0/30。
Step 12 抓到了，但 Step 6 的 alert_manager 在它之前跑、读不到；前瞻检验也把这天当普通日子。本文件钉四件事：

1. 判定本身：同一数据源族 ≥2 个字段同时降级 ⇒ `source_outage`；单字段 / 跨源各一个 ⇒ 只是 `degraded`；判不了 ⇒ `available=False`，**不当 ok**；
2. 告警：每个故障源一条 P1（不按字段出）；`degraded` / `ok` 不报；没统计 / 取值不认识 ⇒ P2（默认会红）；
3. 前瞻检验加载器**只显示**：把判定换成别的内容，其余输出一字不变；判定自己坏了写原因、不写成「0 个故障日」；
4. 扫描接线：`scan_timing.extra.day_health` 由 `alpha_hive_daily_report.main` 写（端到端键集合断言在 `test_production_sync.py`）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import day_health as dh
import scan_coverage_gate as G
import signal_archive  # noqa: F401  (被加载器 import；先导入避免路径差异)
from tests.test_alert_partial_runs import _analyze, _healthy_status, _high, _tagged
from tests.test_dim_ic_forward_test import _config_repo, _day_rows, _make_db

import alert_manager as am

_ROOT = Path(__file__).resolve().parent.parent
_DATE = "2026-10-14"

#: 10-14 实测：这四个字段 0/30（`scan_coverage-2026-10-14.json` 的 degraded_fields）
_OCT14 = {"iv_rank", "unusual_flow", "analyst_targets", "catalysts"}


def _detail_key(spec):
    return spec.get("recorded_key") or spec["path"].split(".", 1)[1]


def _results(n=30, broken=(), unrecorded=()):
    """按 `scan_coverage_gate.FIELDS` 生成 n 只标的的结果：`broken` 里的字段全部取不到，`unrecorded` 里的字段键根本不写（旧代码）。"""
    out = {}
    for i in range(n):
        agents = {}
        for spec in G.FIELDS:
            agent = spec["path"].split(".", 1)[0]
            det = agents.setdefault(agent, {"details": {}})["details"]
            k = _detail_key(spec)
            if spec["key"] in unrecorded:
                continue
            if spec["key"] in broken:
                det[k] = None
            elif spec["key"] == "unusual_flow":
                det[k] = True
            elif spec["key"] == "analyst_targets":
                det[k] = {"target_mean": 10.0}
            elif spec["key"] == "catalysts":
                det[k] = [1]
            else:
                det[k] = 1.0
        out[f"T{i:02d}"] = {"agent_details": agents}
    return out


def _assess(tmp_path, **kw):
    p = tmp_path / f".swarm_results_{_DATE}.json"
    p.write_text(json.dumps(_results(**kw)), encoding="utf-8")
    return dh.assess(_DATE, p)


# ── 1. 判定 ──────────────────────────────────────────────────────────────────

class TestClassify:
    def test_healthy_day_is_ok(self, tmp_path):
        v = _assess(tmp_path)
        assert v["available"] and v["verdict"] == dh.OK and v["outage_sources"] == {} and v["degraded_fields"] == []

    def test_oct14_shape_is_a_yfinance_outage(self, tmp_path):
        """正对照：10-14 的形状必须判 source_outage，且报出 4 个字段与 0/30。"""
        v = _assess(tmp_path, broken=_OCT14)
        assert v["verdict"] == dh.SOURCE_OUTAGE
        assert set(v["outage_sources"]) == {"yfinance"}
        got = {f["field"]: (f["have"], f["total"]) for f in v["outage_sources"]["yfinance"]}
        assert got == {k: (0, 30) for k in _OCT14}

    def test_single_field_is_only_degraded(self, tmp_path):
        """analyst_targets 单独 0/30 出现过 8 天——常态，不能判故障日（否则 P1 天天响）。"""
        v = _assess(tmp_path, broken={"analyst_targets"})
        assert v["verdict"] == dh.DEGRADED and v["outage_sources"] == {} and v["degraded_fields"] == ["analyst_targets"]

    def test_one_field_in_each_of_two_sources_is_not_an_outage(self, tmp_path):
        """规则是「同一源族 ≥2」，不是「总共 ≥2」：yfinance 一个 + CBOE 一个，各自都只是个别字段。"""
        v = _assess(tmp_path, broken={"analyst_targets", "put_call_ratio"})
        assert v["verdict"] == dh.DEGRADED and v["outage_sources"] == {}

    def test_threshold_boundary(self, tmp_path):
        assert dh.MIN_OUTAGE_FIELDS == 2
        assert _assess(tmp_path, broken={"iv_current"})["verdict"] == dh.DEGRADED
        assert _assess(tmp_path, broken={"iv_current", "iv_skew_ratio"})["verdict"] == dh.SOURCE_OUTAGE

    def test_derived_fields_of_one_fetch_count_as_that_source_outage(self, tmp_path):
        """rv_30d / iv_rank / iv_rv_spread 同出一次 yfinance 日 K：日 K 单独挂就是 yfinance 整源（日 K）降级。"""
        v = _assess(tmp_path, broken={"rv_30d", "iv_rank", "iv_rv_spread"})
        assert v["verdict"] == dh.SOURCE_OUTAGE and set(v["outage_sources"]) == {"yfinance"}

    def test_09_24_shape_flags_both_sources(self, tmp_path):
        v = _assess(tmp_path, broken={"rv_30d", "iv_rank", "iv_rv_spread", "analyst_targets", "catalysts",
                                      "iv_current", "iv_skew_ratio", "put_call_ratio"})
        assert set(v["outage_sources"]) == {"yfinance", "CBOE"}

    def test_field_not_recorded_by_old_code_is_not_degraded(self, tmp_path):
        """闸对 `unusual_flow_ok` 键根本不在的旧结果判「未记录」——不能被当成降级拖进故障日。"""
        v = _assess(tmp_path, unrecorded={"unusual_flow"}, broken={"analyst_targets"})
        assert v["verdict"] == dh.DEGRADED and "unusual_flow" not in v["degraded_fields"]

    def test_undeterminable_is_unavailable_not_ok(self, tmp_path):
        """结果文件不在 / 不可解析 ⇒ available=False + 原因；绝不能写成 ok（「没检查」不是「没问题」）。"""
        v = dh.assess(_DATE, tmp_path / "nope.json")
        assert v["available"] is False and v["reason"]
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert dh.assess(_DATE, bad)["available"] is False
        assert dh.classify(None)["available"] is False
        assert dh.classify({"determinable": True, "fields": "x"})["available"] is False

    def test_every_monitored_source_family_is_known(self):
        """新增受监视字段带来新的数据源族时，这条红会逼你去 alert_manager._check_day_health 补建议文案，并想清楚它算不算「同源」。"""
        fams = {dh.family(s["source"]) for s in G.FIELDS}
        assert fams <= {"yfinance", "CBOE"}, fams
        assert dh.family("") == "?"


# ── 2. 告警 ──────────────────────────────────────────────────────────────────

def _status_with(dh_extra):
    s = _healthy_status()
    if dh_extra is None:
        s["scan_timing"]["extra"].pop("day_health", None)
    else:
        s["scan_timing"]["extra"]["day_health"] = dh_extra
    return s


def _outage(tmp_path, **kw):
    return _assess(tmp_path, **kw)


class TestAlert:
    def test_outage_fires_one_p1_per_source_not_per_field(self, tmp_path):
        _a, alerts = _analyze(tmp_path, _status_with(_outage(tmp_path, broken=_OCT14)))
        hit = _tagged(alerts, "day_health")
        assert len(hit) == 1 and hit[0].level == am.AlertLevel.HIGH
        assert "yfinance" in hit[0].message and "4 个字段" in hit[0].message
        assert "iv_rank 0/30" in hit[0].details["字段（有数/总数）"]
        assert hit[0] in _high(alerts)

    def test_two_sources_two_p1s(self, tmp_path):
        v = _outage(tmp_path, broken={"rv_30d", "iv_rank", "iv_rv_spread", "iv_current", "iv_skew_ratio"})
        _a, alerts = _analyze(tmp_path, _status_with(v))
        msgs = sorted(a.message for a in _tagged(alerts, "day_health"))
        assert len(msgs) == 2 and any("CBOE" in m for m in msgs) and any("yfinance" in m for m in msgs)

    @pytest.mark.parametrize("broken", [(), ("analyst_targets",), ("analyst_targets", "put_call_ratio")])
    def test_ok_and_degraded_stay_quiet(self, tmp_path, broken):
        a, alerts = _analyze(tmp_path, _status_with(_outage(tmp_path, broken=broken)))
        assert _tagged(alerts, "day_health") == [] and not [s for s in a.checks_skipped if "日健康" in s]

    @pytest.mark.parametrize("extra", [
        None,                                         # 键根本不在：接线断了
        {"available": False, "reason": "boom"},       # 算不出来
        "garbage",                                    # 不是对象
    ])
    def test_not_computed_is_a_p2_never_silence(self, tmp_path, extra):
        a, alerts = _analyze(tmp_path, _status_with(extra))
        hit = _tagged(alerts, "day_health")
        assert len(hit) == 1 and hit[0].level == am.AlertLevel.MEDIUM and "未统计" in hit[0].message
        assert any("日健康" in s for s in a.checks_skipped)

    def test_unknown_verdict_goes_red_by_default(self, tmp_path):
        _a, alerts = _analyze(tmp_path, _status_with({"available": True, "verdict": "something_new", "version": 9}))
        hit = _tagged(alerts, "day_health")
        assert len(hit) == 1 and "不认识" in hit[0].message

    def test_producer_output_reaches_consumer(self, tmp_path):
        """生产者（真跑闸 + 判定）的产出直接喂 alert_manager——两头字段名对得上，而不是各自对着手写 dict 绿。"""
        v = _outage(tmp_path, broken=_OCT14)
        assert json.loads(json.dumps(v)) == v                 # 能过 scan_timing 的 JSON 往返
        _a, alerts = _analyze(tmp_path, _status_with(json.loads(json.dumps(v))))
        assert len(_tagged(alerts, "day_health")) == 1


# ── 3. 前瞻检验加载器：只显示 ─────────────────────────────────────────────────

def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = _load("dim_ic_forward_test_dh", "experiments/dim_ic_forward_test.py")
P = _load("dim_ic_protocol_dh", "experiments/dim_ic_protocol.py")
fwd = _load("resonance_boost_forward_test_dh", "experiments/resonance_boost_forward_test.py")


class TestSummarize:
    def test_summarize_lists_outage_days(self, tmp_path):
        for d, kw in (("2026-10-13", {}), ("2026-10-14", {"broken": _OCT14}), ("2026-10-15", {"broken": {"analyst_targets"}})):
            (tmp_path / f".swarm_results_{d}.json").write_text(json.dumps(_results(**kw)), encoding="utf-8")
        s = dh.summarize_days(["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"], home=tmp_path)
        assert s["available"] and s["n_days"] == 4
        assert s["outage"] == [{"date": "2026-10-14", "sources": {"yfinance": sorted(_OCT14, key=["iv_rank", "unusual_flow", "analyst_targets", "catalysts"].index)}}]
        assert s["degraded"] == ["2026-10-15"]
        assert [u["date"] for u in s["undetermined"]] == ["2026-10-16"]
        assert "10-14" in dh.describe(s) and "判不了 1 天" in dh.describe(s)

    def test_summarizer_failure_is_reported_not_zero_outage(self, monkeypatch):
        """判定自己坏了必须写原因：写成「0 个故障日」就是把失败改写成了没发生过。"""
        monkeypatch.setattr(dh, "assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        s = dh.summarize_days(["2026-10-14"])
        assert s["available"] is False and "boom" in s["reason"]
        assert "不可得" in dh.describe(s)


class TestDimIcLoaderIsDisplayOnly:
    def _run(self, tmp_path, monkeypatch, summary=None, name="w"):
        tmp_path = tmp_path / name          # 同一用例里跑两遍要各用各的目录（_config_repo 会 mkdir）
        tmp_path.mkdir()
        days = ["2026-10-12", "2026-10-13", "2026-10-14"]
        rows = [r for d in days for r in _day_rows(d)]
        monkeypatch.setattr(G, "ROOT", tmp_path)
        for d in days:
            (tmp_path / f".swarm_results_{d}.json").write_text(
                json.dumps(_results(broken=_OCT14 if d == "2026-10-14" else ())), encoding="utf-8")
        if summary is not None:
            monkeypatch.setattr(dh, "summarize_days", lambda *a, **k: summary)
        repo = _config_repo(tmp_path, [("2026-09-09", dict(P.FROZEN_WEIGHTS))])
        return fx.run(db_path=str(_make_db(tmp_path, rows)), today="2026-10-30", repo_root=repo, history=[])

    def test_reports_the_outage_day(self, tmp_path, monkeypatch):
        res = self._run(tmp_path, monkeypatch)
        assert [o["date"] for o in res["day_health"]["outage"]] == ["2026-10-14"]

    def test_changing_the_summary_changes_nothing_else(self, tmp_path, monkeypatch):
        """口径没动的证明：把判定换成完全不同的内容，除 day_health 外的每个键逐字相同（样本、状态、判定都不读它）。"""
        a = self._run(tmp_path, monkeypatch, name="w1")
        # 对照组必须是「没有故障日」：两边都带故障日的话，「有故障日就改口径」的变异会在两边同时发生、比不出来
        assert a["day_health"]["outage"], "夹具要有真故障日，对照才有意义"
        b = self._run(tmp_path, monkeypatch, name="w2", summary={"available": True, "version": 99, "n_days": 3,
                                                                  "outage": [], "degraded": [], "undetermined": []})
        assert a["day_health"] != b["day_health"]
        assert {k: v for k, v in a.items() if k != "day_health"} == {k: v for k, v in b.items() if k != "day_health"}

    def test_excluded_day_list_unchanged(self, tmp_path, monkeypatch):
        """故障日不等于协议排除日：10-14 照样在样本里（排不排是协议修订，不在本版）。"""
        res = self._run(tmp_path, monkeypatch)
        assert "2026-10-14" not in signal_archive.FORWARD_TEST_EXCLUDED_DAYS
        assert "2026-10-14" not in res["forward_test_excluded_days"]


class TestResonanceLoaderIsDisplayOnly:
    def _home(self, tmp_path):
        sr = {"ticker": "NVDA", "agent_details": {"ScoutBeeNova": {"score": 5.0}}, "data_quality": {"x": 1},
              "base_score_before_resonance": 5.0, "gex_regime_mod": {}, "final_score": 5.0, "direction": "neutral"}
        for d in ("2026-10-13", "2026-10-14"):
            (tmp_path / f"analysis-NVDA-ml-{d}.json").write_text(json.dumps({"swarm_results": sr}), encoding="utf-8")
            (tmp_path / f".swarm_results_{d}.json").write_text(
                json.dumps(_results(broken=_OCT14 if d == "2026-10-14" else ())), encoding="utf-8")
        (tmp_path / "p.db").write_text("", encoding="utf-8")
        return tmp_path

    def _run(self, tmp_path, monkeypatch, summary=None):
        home = self._home(tmp_path)
        monkeypatch.setattr(fwd, "load_excess_returns", lambda db: {})
        monkeypatch.setattr(fwd, "evaluate", lambda rows, excess, **k: {"status": "not_ready", "weeks": 0,
                                                                        "n_reports": len(rows)})
        if summary is not None:
            monkeypatch.setattr(dh, "summarize_days", lambda *a, **k: summary)
        return fwd.run(home=home, db_path=str(home / "p.db"), today="2026-10-30")

    def test_reports_the_outage_day_and_leaves_the_rest(self, tmp_path, monkeypatch):
        a = self._run(tmp_path, monkeypatch)
        assert a["n_reports"] == 2 and [o["date"] for o in a["day_health"]["outage"]] == ["2026-10-14"]
        b = self._run(tmp_path, monkeypatch, summary={"available": False, "reason": "x"})
        assert {k: v for k, v in a.items() if k != "day_health"} == {k: v for k, v in b.items() if k != "day_health"}


# ── 4. 扫描接线 ──────────────────────────────────────────────────────────────

class TestScanWiring:
    def test_daily_report_writes_the_extra_key(self):
        """端到端键集合在 test_production_sync；这里钉住来源：必须经 `day_health.assess`，且失败写带原因的记录。"""
        src = (_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        assert '"day_health": _day_health' in src
        assert "_day_health_mod.assess(" in src
        assert '_day_health = {"available": False, "reason":' in src

    def test_alert_manager_runs_the_check(self):
        src = (_ROOT / "alert_manager.py").read_text(encoding="utf-8")
        assert "self._check_day_health(status)" in src
        # 必须排在兜底 `_check_partial_explained` 之前（兜底要看到本检查产出的 P1）
        assert src.index("self._check_day_health(status)") < src.index("self._check_partial_explained(status)")
