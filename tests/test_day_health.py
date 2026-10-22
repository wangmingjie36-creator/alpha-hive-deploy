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
        _derived = {"day_health", "sensitivity_excl_outage_days"}      # 这两项本来就由判定派生；主分析的每个键必须逐字相同
        assert {k: v for k, v in a.items() if k not in _derived} == {k: v for k, v in b.items() if k not in _derived}

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
        _derived = {"day_health", "sensitivity_excl_outage_days"}
        assert {k: v for k, v in a.items() if k not in _derived} == {k: v for k, v in b.items() if k not in _derived}


# ── 3b. 敏感性分析（修订 3，2026-10-22）：主分析不变、并列报告「去掉 source_outage 日」 ──────────────

_REGISTERED_FP = "76bd554125c07a4e"    # 与 day_health.REGISTERED_FINGERPRINT、两份协议正文里的同一个值：三处都要改才算改预注册
_FORBIDDEN_NESTED = {"ic", "t", "p", "ci95", "se_nw", "t_iid", "descriptive", "verdicts", "result", "stats"}


def _all_keys(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _all_keys(v)


class TestSensitivityRuleIsFrozen:
    def test_fingerprint_is_pinned_in_three_places(self):
        assert dh.REGISTERED_FINGERPRINT == _REGISTERED_FP
        assert dh.definition_fingerprint() == _REGISTERED_FP, \
            "「source_outage 日」的定义变了（FIELDS / 阈值 / 分族 / 判定版本）——这是改预注册：新开修订，或还原定义"
        assert _REGISTERED_FP in (_ROOT / "experiments/dim_ic_preregistration.md").read_text(encoding="utf-8")
        assert _REGISTERED_FP in (_ROOT / "experiments/resonance_boost_forward_test.py").read_text(encoding="utf-8")

    def test_both_protocols_record_the_amendment(self):
        doc = (_ROOT / "experiments/dim_ic_preregistration.md").read_text(encoding="utf-8")
        assert "### 修订 3（2026-10-22" in doc and "不排除" in doc and "不触发任何动作" in doc
        src = (_ROOT / "experiments/resonance_boost_forward_test.py").read_text(encoding="utf-8")
        assert "修订 3（v0.45.605，2026-10-22" in src and "不触发任何动作" in src

    @pytest.mark.parametrize("mutate", ["add_field", "threshold", "version", "family", "min_coverage"])
    def test_any_definition_change_moves_the_fingerprint(self, monkeypatch, mutate):
        """正对照：每一种「改定义」都必须让指纹变——否则冻结是假的。"""
        if mutate == "add_field":
            monkeypatch.setattr(G, "FIELDS", G.FIELDS + [{"key": "x", "path": "OracleBeeEcho.x", "min_coverage": 0.7,
                                                           "source": "yfinance 新字段"}])
        elif mutate == "threshold":
            monkeypatch.setattr(dh, "MIN_OUTAGE_FIELDS", 3)
        elif mutate == "version":
            monkeypatch.setattr(dh, "VERSION", 2)
        elif mutate == "family":
            monkeypatch.setattr(G, "FIELDS", [{**f, "source": "CBOE 期权链"} if f["key"] == "rv_30d" else f for f in G.FIELDS])
        else:
            monkeypatch.setattr(G, "FIELDS", [{**f, "min_coverage": 0.5} if f["key"] == "catalysts" else f for f in G.FIELDS])
        assert dh.definition_fingerprint() != _REGISTERED_FP

    def test_cosmetic_changes_do_not_move_it(self, monkeypatch):
        """注释 / note 文案不是定义：改了不该让敏感性作废。"""
        monkeypatch.setattr(G, "FIELDS", [{**f, "note": "改个说法"} for f in G.FIELDS])
        assert dh.definition_fingerprint() == _REGISTERED_FP

    def test_plan_lists_days_and_counts_undetermined(self):
        s = {"available": True, "outage": [{"date": "2026-10-16", "sources": {}}, {"date": "2026-10-14", "sources": {}}],
             "degraded": ["2026-10-15"], "undetermined": [{"date": "2026-10-20", "reason": "x"}]}
        assert dh.sensitivity_plan(s) == {"available": True, "rule_version": dh.VERSION,
                                          "days": ["2026-10-14", "2026-10-16"], "undetermined": 1}

    def test_plan_is_unavailable_never_silently_empty(self, monkeypatch):
        assert dh.sensitivity_plan({"available": False, "reason": "boom"})["available"] is False
        assert dh.sensitivity_plan(None)["available"] is False
        monkeypatch.setattr(dh, "MIN_OUTAGE_FIELDS", 3)
        p = dh.sensitivity_plan({"available": True, "outage": [], "degraded": [], "undetermined": []})
        assert p["available"] is False and "不可比" in p["reason"] and "days" not in p


class TestDimIcSensitivity:
    def _run(self, tmp_path, monkeypatch, days, outage, name, today="2026-10-30"):
        work = tmp_path / name
        work.mkdir()
        rows = [r for d in days for r in _day_rows(d)]
        monkeypatch.setattr(G, "ROOT", work)
        for d in days:
            (work / f".swarm_results_{d}.json").write_text(
                json.dumps(_results(broken=_OCT14 if d in outage else ())), encoding="utf-8")
        repo = _config_repo(work, [("2026-09-09", dict(P.FROZEN_WEIGHTS))])
        return fx.run(db_path=str(_make_db(work, rows)), today=today, repo_root=repo, history=[])

    def test_sensitivity_equals_running_without_that_day(self, tmp_path, monkeypatch):
        """端到端：敏感性块 = 在「根本没有 10-14」的库上跑同一条链。故障日被去掉的是行，不是别的什么。"""
        days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
        with_day = self._run(tmp_path, monkeypatch, days, {"2026-10-14"}, "a")
        without = self._run(tmp_path, monkeypatch, [d for d in days if d != "2026-10-14"], set(), "b")
        sb = with_day["sensitivity_excl_outage_days"]
        assert sb["available"] and sb["excluded_days"] == ["2026-10-14"] and sb["n_rows_removed"] == 25
        assert (sb["h1_weeks"], sb["status"]) == (without["h1_weeks"], without["status"])
        assert without["sensitivity_excl_outage_days"].get("same_as_primary") is True
        # 主分析：含 10-14 的那次与「没有 10-14」的那次，h1 合格周数口径一致地由全样本决定（不被敏感性改写）
        assert with_day["h1_weeks"] == self._run(tmp_path, monkeypatch, days, set(), "c")["h1_weeks"]

    def test_primary_identical_whether_or_not_there_is_an_outage_day(self, tmp_path, monkeypatch):
        """主分析不变的证明：同一份样本，仅「10-14 是不是故障日」不同 ⇒ 除两个派生键外逐字相同。"""
        days = ["2026-10-12", "2026-10-13", "2026-10-14"]
        a = self._run(tmp_path, monkeypatch, days, {"2026-10-14"}, "p1")
        b = self._run(tmp_path, monkeypatch, days, set(), "p2")
        _derived = {"day_health", "sensitivity_excl_outage_days"}
        assert {k: v for k, v in a.items() if k not in _derived} == {k: v for k, v in b.items() if k not in _derived}
        assert a["sensitivity_excl_outage_days"] != b["sensitivity_excl_outage_days"]

    def test_primary_chain_gets_every_row_and_only_the_sensitivity_chain_drops_the_outage_day(self, tmp_path, monkeypatch):
        """会咬人的探针：用桩替换判定链，让它返回「拿到了几个有效日」。主分析必须拿到 3 天，敏感性那条只拿到 2 天。
        （直接比 `h1_weeks` 不行：10-19 是仓库登记的世代边界，窗口里只剩 W42 一个合格周，去掉一天不改周数——
        「主分析被悄悄改成敏感性的样本」这类变异在周数口径下是看不出来的，这里看进链的行。）"""
        seen = []

        def _probe(h1_weeks, h2_weeks, P_, **k):
            n_days = sum(d for _w, _v, d in h1_weeks)
            seen.append(n_days)
            return {"status": "not_ready", "h1_weeks": n_days, "next_look_at": 26, "looks_done": [], "verdicts": {}}
        monkeypatch.setattr(fx, "decide", _probe)
        # 日子要避开：10-12（协议排除日）、10-14（H1 的价格输入已按 §13.8 隔离、当天横截面宽度不足）、10-19 起（世代边界截断）
        days = ["2026-10-13", "2026-10-15", "2026-10-16"]
        res = self._run(tmp_path, monkeypatch, days, {"2026-10-16"}, "probe")
        assert res["h1_weeks"] == 3, "主分析的链必须拿到全部 3 个有效日（含故障日）"
        assert res["sensitivity_excl_outage_days"]["h1_weeks"] == 2, "敏感性的链只拿到去掉故障日后的 2 天"
        assert seen == [3, 2], seen

    def test_blinded_period_exposes_only_counts(self, tmp_path, monkeypatch):
        res = self._run(tmp_path, monkeypatch, ["2026-10-12", "2026-10-13", "2026-10-14"], {"2026-10-14"}, "blind")
        sb = res["sensitivity_excl_outage_days"]
        assert res["verdicts"] == {} and "verdicts" not in sb
        assert not (_FORBIDDEN_NESTED & set(_all_keys(sb))), sorted(_FORBIDDEN_NESTED & set(_all_keys(sb)))

    def test_verdicts_revealed_only_when_primary_has_a_conclusion(self):
        """用桩链单测露出规则：revealed=False 不带 verdicts；True 才带——盲化口径与主分析一致。"""
        dhs = {"available": True, "outage": [{"date": "2026-10-14", "sources": {}}], "degraded": [], "undetermined": []}
        rows = [{"date": "2026-10-13"}, {"date": "2026-10-14"}]
        def stub(rs):
            return {"h1_weeks": len(rs), "status": "concluded", "verdicts": {"H1": {"result": "x"}},
                    "descriptive": {"signal": {}}}
        hidden = fx.sensitivity_block(dhs, rows, stub, revealed=False)
        shown = fx.sensitivity_block(dhs, rows, stub, revealed=True)
        assert "verdicts" not in hidden and "descriptive" not in hidden and hidden["n_rows_removed"] == 1
        assert shown["verdicts"] == {"H1": {"result": "x"}} and "descriptive" in shown

    def test_broken_summary_or_frozen_definition_is_visible(self, monkeypatch):
        rows = [{"date": "2026-10-14"}]
        assert fx.sensitivity_block({"available": False, "reason": "boom"}, rows, lambda r: {}, revealed=False)["available"] is False
        dhs = {"available": True, "outage": [{"date": "2026-10-14", "sources": {}}], "degraded": [], "undetermined": []}
        monkeypatch.setattr(dh, "REGISTERED_FINGERPRINT", "0" * 16)
        out = fx.sensitivity_block(dhs, rows, lambda r: {}, revealed=False)
        assert out["available"] is False and "不可比" in out["reason"]

    def test_chain_failure_is_reported_not_swallowed(self):
        dhs = {"available": True, "outage": [{"date": "2026-10-14", "sources": {}}], "degraded": [], "undetermined": []}

        def boom(_rs):
            raise RuntimeError("chain broke")
        out = fx.sensitivity_block(dhs, [{"date": "2026-10-14"}], boom, revealed=True)
        assert out["available"] is False and "chain broke" in out["reason"]


class TestResonanceSensitivity:
    def _home(self, tmp_path, outage):
        sr = {"ticker": "NVDA", "agent_details": {"ScoutBeeNova": {"score": 5.0}}, "data_quality": {"x": 1},
              "base_score_before_resonance": 5.0, "gex_regime_mod": {}, "final_score": 5.0, "direction": "neutral"}
        for d in ("2026-10-13", "2026-10-14", "2026-10-15"):
            (tmp_path / f"analysis-NVDA-ml-{d}.json").write_text(json.dumps({"swarm_results": sr}), encoding="utf-8")
            (tmp_path / f".swarm_results_{d}.json").write_text(
                json.dumps(_results(broken=_OCT14 if d in outage else ())), encoding="utf-8")
        (tmp_path / "p.db").write_text("", encoding="utf-8")
        return tmp_path

    def _run(self, tmp_path, monkeypatch, status="not_ready", outage=("2026-10-14",)):
        home = self._home(tmp_path, set(outage))
        monkeypatch.setattr(fwd, "load_excess_returns", lambda db: {})
        seen = []

        def _eval(rows, excess, **k):
            seen.append(sorted(r["date"] for r in rows))
            return {"status": status, "weeks": len(rows), "next_look_at": 26, "n_reports": len(rows),
                    "stats": {"mean": 0.1, "p": 0.2}, "look": "中检", "alpha": 0.01}
        monkeypatch.setattr(fwd, "evaluate", _eval)
        return fwd.run(home=home, db_path=str(home / "p.db"), today="2026-10-30"), seen

    def test_second_evaluation_runs_without_the_outage_day(self, tmp_path, monkeypatch):
        res, seen = self._run(tmp_path, monkeypatch)
        assert seen == [["2026-10-13", "2026-10-14", "2026-10-15"], ["2026-10-13", "2026-10-15"]]
        sb = res["sensitivity_excl_outage_days"]
        assert sb["available"] and sb["excluded_days"] == ["2026-10-14"] and sb["n_rows_removed"] == 1 and sb["n_reports"] == 2
        assert res["n_reports"] == 3, "主分析仍含 10-14"

    def test_blinded_until_primary_concludes(self, tmp_path, monkeypatch):
        res, _ = self._run(tmp_path, monkeypatch, status="not_ready")
        sb = res["sensitivity_excl_outage_days"]
        assert "result" not in sb and not (_FORBIDDEN_NESTED & set(_all_keys(sb)))
        (tmp_path / "x").mkdir()
        res2, _ = self._run(tmp_path / "x", monkeypatch, status="confirmed")
        assert res2["sensitivity_excl_outage_days"]["result"]["stats"] == {"mean": 0.1, "p": 0.2}

    def test_no_outage_day_means_no_second_evaluation(self, tmp_path, monkeypatch):
        res, seen = self._run(tmp_path, monkeypatch, outage=())
        assert len(seen) == 1 and res["sensitivity_excl_outage_days"]["same_as_primary"] is True

    def test_primary_unchanged_by_the_sensitivity(self, tmp_path, monkeypatch):
        a, _ = self._run(tmp_path, monkeypatch, outage=("2026-10-14",))
        (tmp_path / "y").mkdir()
        b, _ = self._run(tmp_path / "y", monkeypatch, outage=())
        _derived = {"day_health", "sensitivity_excl_outage_days"}
        assert {k: v for k, v in a.items() if k not in _derived} == {k: v for k, v in b.items() if k not in _derived}

    def test_insample_review_skips_it(self, tmp_path, monkeypatch):
        home = self._home(tmp_path, {"2026-10-14"})
        monkeypatch.setattr(fwd, "load_excess_returns", lambda db: {})
        monkeypatch.setattr(fwd, "evaluate", lambda rows, excess, **k: {"status": "insample", "weeks": 0})
        assert "sensitivity_excl_outage_days" not in fwd.run(home=home, db_path=str(home / "p.db"), insample=True)


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
