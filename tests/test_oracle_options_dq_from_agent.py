"""v0.45.369：OracleBee 期权数据「可用」改按 OptionsAgent 自己的 `data_quality` 判；世代边界并入 09-28（等价论证）。

守三件事：
1. **行为**：样本链早退（`data_quality: "unavailable"` 的非空结果）⇒ 标签 `unavailable`、置信度不加 0.3；
   real / degraded ⇒ 照旧 real + 0.3；OptionsAgent 抛异常 ⇒ fallback（同旧）。Oracle 分与方向不随标签变。
2. **印记**：`details.options_dq_from_agent` 字面量 True，每条返回路径都写（含日报合成回退），经 Queen 进归档后判别器认得出。
3. **等价边界**（`ic_rerun_readiness._BOUNDARY_EQUIVALENCE`）：印记首见之前的旧代码记录，逐只标的核「在本版改动下输出不变」；
   不等价（旧代码又碰上断链）照报 boundary_too_early；读不出记为不等价；只对登记了的边界生效；等价判据不当印记用。

⚠️ 本文件不用 skip：全部合成数据、零外部依赖（Oracle 的 OptionsAgent / 异动流都桩掉）。
"""
from __future__ import annotations

import ast
import copy
import json

import pytest

import ic_rerun_readiness as rr
from pheromone_board import PheromoneBoard
from swarm_agents import oracle_bee as ob
from tests.test_gex_oracle_bear_neutralized import REPO_ROOT, TICKER, _options_stub, _run_oracle

MARK = "options_dq_from_agent"
_V = "v0.45.369"


def _stub_with_dq(dq, **extra):
    """在 v0.45.349 那套桩（新代码形状）上加 `data_quality`；`sample=True` 复刻样本链早退的指标全 None。"""
    fields = {"data_quality": dq}
    if dq == "unavailable":
        fields.update({"iv_rank": None, "put_call_ratio": None, "gamma_exposure": None,
                       "gamma_squeeze_risk": "unknown", "options_score": 5.0,
                       "signal_summary": "期权数据不可用（真实链获取失败）"})
    fields.update(extra)
    return _options_stub(None, extra=fields)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    import llm_service
    monkeypatch.setattr(llm_service, "is_available", lambda: False)


# ════════════════════════════════════════════════════════════════════════════
# 1. 行为
# ════════════════════════════════════════════════════════════════════════════

class TestOracleLabelsByAgentDataQuality:

    def test_sample_chain_early_exit_is_not_real(self, monkeypatch):
        """变红的变异：`_options_data_usable` 退回 `bool(result)`（样本链结果非空 ⇒ real + 0.7）。"""
        out, _ = _run_oracle(monkeypatch, _stub_with_dq("unavailable"))
        assert "error" not in out
        assert out["data_quality"]["options"] == "unavailable"
        assert out["confidence"] == pytest.approx(0.4)

    @pytest.mark.parametrize("dq", ["real", "degraded"])
    def test_real_and_degraded_unchanged(self, monkeypatch, dq):
        """degraded（链在、IV 缺）新旧都算取到——本版只动 unavailable 这一个值。"""
        out, _ = _run_oracle(monkeypatch, _stub_with_dq(dq))
        assert out["data_quality"]["options"] == "real" and out["confidence"] == pytest.approx(0.7)

    def test_agent_exception_still_fallback(self, monkeypatch):
        out, _ = _run_oracle(monkeypatch, _options_stub(None, raise_exc=ConnectionError("桩")))
        assert out["data_quality"]["options"] == "fallback" and out["confidence"] == pytest.approx(0.4)

    def test_score_and_direction_do_not_depend_on_the_label(self, monkeypatch):
        """同一份期权结果，只换 `data_quality` 的值：分与方向逐位相同 ⇒ 本版只动标签与置信度。"""
        a, _ = _run_oracle(monkeypatch, _stub_with_dq("real", iv_rank=40.0, put_call_ratio=0.9))
        b, _ = _run_oracle(monkeypatch, _stub_with_dq("unavailable", iv_rank=40.0, put_call_ratio=0.9,
                                                       options_score=a["details"]["options_score"],
                                                       signal_summary=a["details"]["signal_summary"]))
        assert (a["score"], a["direction"]) == (b["score"], b["direction"])
        assert (a["data_quality"]["options"], b["data_quality"]["options"]) == ("real", "unavailable")

    def test_new_label_is_registered_with_the_queen(self):
        """标签必须在 Queen 的两集合里（否则按 0 分静默计入 data_real_pct，见 DQ 标签登记表）。"""
        from swarm_agents.queen_distiller import QueenDistiller
        assert "unavailable" in QueenDistiller.PROXY_SOURCES
        assert "unavailable" not in QueenDistiller.REAL_SOURCES


# ════════════════════════════════════════════════════════════════════════════
# 2. 印记
# ════════════════════════════════════════════════════════════════════════════

class TestMarkerOnEveryPath:

    def _marked(self, out):
        return isinstance(out.get("details"), dict) and out["details"].get(MARK) is True

    def test_success(self, monkeypatch):
        out, _ = _run_oracle(monkeypatch, _stub_with_dq("real"))
        assert self._marked(out)

    def test_literal_overrides_upstream(self, monkeypatch):
        """变红的变异：把字面量挪到 `**(result or {})` 之前。"""
        out, _ = _run_oracle(monkeypatch, _stub_with_dq("real", **{MARK: False}))
        assert self._marked(out)

    def test_agent_exception(self, monkeypatch):
        out, _ = _run_oracle(monkeypatch, _options_stub(None, raise_exc=ConnectionError("桩")))
        assert self._marked(out)

    def test_error_fallback(self, monkeypatch):
        """变红的变异：`_mark_gex_out_of_score` 不再写本印记。"""
        out, _ = _run_oracle(monkeypatch, _stub_with_dq("real"), stock={"momentum_5d": 1.0})
        assert out.get("error") and self._marked(out)

    def test_invalid_ticker(self, monkeypatch):
        out, _ = _run_oracle(monkeypatch, _stub_with_dq("real"), ticker="bad ticker!")
        assert out.get("error") == "invalid_ticker" and self._marked(out)

    def test_synthetic_swarm_fallback_marks_unconditionally(self):
        """日报合成回退自己拼 Oracle details：无条件写 `[...] = True`（不在 if / try 里）。"""
        src = (REPO_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_generate_synthetic_swarm_results")
        parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
        chains = []
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value is True
                    and any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                            and t.slice.value == MARK for t in node.targets)):
                chain, cur = [], node
                while cur in parents:
                    cur = parents[cur]
                    chain.append(type(cur).__name__)
                chains.append(chain)
        assert chains, "合成回退里没有写 options_dq_from_agent = True"
        assert any(not ({"If", "Try", "ExceptHandler"} & set(c)) for c in chains), chains

    def test_marker_survives_queen_into_archive_shape(self, monkeypatch):
        """写端 ↔ 读端：Oracle 真输出经 QueenDistiller 进 agent_details 后，判别器认得出；等价判据也读得到 data_quality。"""
        from swarm_agents.queen_distiller import QueenDistiller
        oracle, _ = _run_oracle(monkeypatch, _stub_with_dq("unavailable"))
        dims = {"ScoutBeeNova": "signal", "BuzzBeeWhisper": "sentiment", "ChronosBeeHorizon": "catalyst",
                "GuardBeeSentinel": "risk_adj", "RivalBeeVanguard": "ml_auxiliary", "BearBeeContrarian": "contrarian"}
        results = [{"source": a, "dimension": d, "direction": "neutral", "score": 5.0, "confidence": 0.5,
                    "discovery": f"{a} 合成", "data_quality": {"x": "real"},
                    "details": {"macro_regime": "neutral"} if a == "GuardBeeSentinel" else {}}
                   for a, d in dims.items()] + [oracle]
        out = QueenDistiller(PheromoneBoard(), enable_llm=False, ml_model=None).distill(
            TICKER, copy.deepcopy(results), dealer_gex={"regime": "unknown"})
        wrapped = {"swarm_results": out}
        assert rr._marker_oracle_options_dq_from_agent(wrapped) is True
        # 新代码的记录上等价判据为假（data_quality 仍是 unavailable）——但它有印记，扫描时先认印记、不看等价
        assert rr._equiv_oracle_chain_available(wrapped) is False


# ════════════════════════════════════════════════════════════════════════════
# 3. 等价边界
# ════════════════════════════════════════════════════════════════════════════

def _b():
    return next(d for d, v, _r in rr._COHORT_HISTORY if v == _V)


def _shift(date, days):
    import datetime as dt
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


def _rec(*, new: bool, chain_ok: bool = True):
    det = {"data_quality": "real" if chain_ok else "unavailable", "options_score": 5.0}
    if new:
        det[MARK] = True
    return {"agent_details": {"OracleBeeEcho": {"score": 5.0, "direction": "neutral", "details": det}}}


def _write_archive(root, date, ticker, rec):
    (root / f"analysis-{ticker}-ml-{date}.json").write_text(json.dumps({"swarm_results": rec}), encoding="utf-8")


def _write_swarm(root, date, recs):
    (root / f".swarm_results_{date}.json").write_text(json.dumps(recs), encoding="utf-8")


class TestEquivalenceBoundary:

    def test_registered_on_the_09_28_boundary(self):
        assert _b() == "2026-09-28"
        assert _V in rr._BOUNDARY_MARKERS and _V in rr._BOUNDARY_EQUIVALENCE

    def test_before_deploy_equivalent_day_is_no_evidence_yet(self, tmp_path):
        """今天（新代码首跑前）的真实形状：09-28 旧代码、全部取到链 ⇒ 还没证据，**不报警**。"""
        b = _b()
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False), "BBB": _rec(new=False)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_after_equivalent_day_matches(self, tmp_path):
        """首跑后：印记首见 09-29，其前只有等价旧记录 ⇒ matches，并列出等价日。
        变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.369"]`（⇒ boundary_too_early）。"""
        b, nxt = _b(), _shift(_b(), 1)
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False), "BBB": _rec(new=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        _write_swarm(tmp_path, nxt, {"AAA": _rec(new=True), "BBB": _rec(new=True, chain_ok=False)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == nxt, ev
        assert ev["equivalent_before_marker"] == [b]
        assert "等价旧记录" in rr._boundary_line(ev)

    def test_old_code_hitting_an_outage_is_too_early(self, tmp_path):
        """新代码没赶上、旧代码又碰上断链 ⇒ 真混算 ⇒ 照报。断链的票**不在 ML 归档里**也要抓到（逐只标的读 .swarm_results）。
        变红的变异：等价核对改读 ML 归档（前 12 只）而不是 .swarm_results；或等价判据恒真。"""
        b, nxt = _b(), _shift(_b(), 1)
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False), "ZZZ": _rec(new=False, chain_ok=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == [b], ev
        assert rr.BOUNDARY_ALARM_VERDICTS >= {ev["verdict"]}

    def test_only_the_window_up_to_first_seen_is_checked(self, tmp_path):
        """窗口是 [边界, 印记首见]：首见**之后**零星的无印记记录不在本判别器的问题范围里（与原有印记逻辑一致——
        原逻辑对首见之后的无印记归档同样不看）。变红的变异：去掉扫描的上界。"""
        b, nxt, later = _b(), _shift(_b(), 1), _shift(_b(), 2)
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        _write_swarm(tmp_path, nxt, {"AAA": _rec(new=True)})
        _write_swarm(tmp_path, later, {"ZZZ": _rec(new=False, chain_ok=False)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["equivalent_before_marker"] == [b], ev

    def test_first_seen_day_merged_with_old_outage_rows_is_too_early(self, tmp_path):
        """印记首见那天也在窗口里：新代码补跑并进旧代码断链那一轮（`merged_swarm.update`），旧的不等价行留在文件里 ⇒ 照报。
        带印记的断链行（新代码自己的）照样跳过。变红的变异：窗口上界退回 `date >= first`；删掉 `if is_new(wrapped): continue`。"""
        b, nxt = _b(), _shift(_b(), 1)
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        _write_swarm(tmp_path, nxt, {"AAA": _rec(new=True, chain_ok=False), "OLD": _rec(new=False, chain_ok=False)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == [nxt], ev
        assert "不等价的旧代码记录" in rr._boundary_line(ev)

    def test_marked_outage_rows_are_skipped(self, tmp_path):
        """新代码自己的断链行（带印记、data_quality 仍是 unavailable）不是「旧口径」。变红的变异：删掉 is_new 跳过。"""
        b = _b()
        _write_archive(tmp_path, b, "AAA", _rec(new=True))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=True, chain_ok=False), "BBB": _rec(new=True, chain_ok=False)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches", ev

    @pytest.mark.parametrize("swarm", ["missing", "empty", "no_such_ticker"])
    def test_unmarked_archive_not_examined_in_swarm_file_is_too_early(self, tmp_path, swarm):
        """旧代码写过 ML 归档，但当日 .swarm_results 缺 / 是 {} / 没有这只票 ⇒ 证不出等价 ⇒ 照报（等价分支不许丢掉原分支看得见的证据）。
        变红的变异：删掉 `_missed` 那段。"""
        b, nxt = _b(), _shift(_b(), 1)
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        if swarm == "empty":
            _write_swarm(tmp_path, b, {})
        elif swarm == "no_such_ticker":
            _write_swarm(tmp_path, b, {"BBB": _rec(new=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == [b], ev
        assert ev["equivalent_before_marker"] == [], ev

    def test_non_dict_swarm_file_is_not_equivalent(self, tmp_path):
        b, nxt = _b(), _shift(_b(), 1)
        (tmp_path / f".swarm_results_{b}.json").write_text("[]", encoding="utf-8")
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == [b], ev

    def test_mixed_day_counts_as_not_equivalent(self, tmp_path):
        """同一天既有等价又有不等价的票 ⇒ 只算不等价，不进等价日列表。变红的变异：返回 `sorted(ok)` 而不是 `ok - bad`。"""
        b, nxt, n2 = _b(), _shift(_b(), 1), _shift(_b(), 2)
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False), "ZZZ": _rec(new=False, chain_ok=False)})
        _write_swarm(tmp_path, nxt, {"AAA": _rec(new=False)})
        _write_archive(tmp_path, n2, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early", ev
        assert ev["unmarked_after_boundary"] == [b] and ev["equivalent_before_marker"] == [nxt], ev

    def test_line_mentions_equivalence_before_any_marker(self, tmp_path):
        """首跑前（今天）Step 11 那一行也要说明 09-28 是按等价放过的，而不是只写「还没证据」。"""
        b = _b()
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["equivalent_before_marker"] == [b]
        assert "等价旧记录" in rr._boundary_line(ev)

    def test_outage_before_any_marker_is_too_early(self, tmp_path):
        b = _b()
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False, chain_ok=False)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == [b], ev

    def test_unreadable_swarm_file_is_not_equivalent(self, tmp_path):
        """证不出等价就不算等价（放宽的一侧负举证责任）。"""
        b, nxt = _b(), _shift(_b(), 1)
        (tmp_path / f".swarm_results_{b}.json").write_text("{坏", encoding="utf-8")
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early", ev

    def test_equivalence_is_not_a_marker(self, tmp_path):
        """边界前大量等价旧记录不许把首见日拉到边界前（那会恒报 boundary_too_late）。"""
        b = _b()
        for i in range(1, 4):
            _write_archive(tmp_path, _shift(b, -i), f"T{i}", _rec(new=False))
            _write_swarm(tmp_path, _shift(b, -i), {f"T{i}": _rec(new=False)})
        _write_archive(tmp_path, b, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == b, ev

    def test_other_boundaries_are_not_relaxed(self, tmp_path):
        """只有登记了等价判据的边界才放宽：v0.45.349 首见晚一天照旧 boundary_too_early。"""
        b349 = next(d for d, v, _r in rr._COHORT_HISTORY if v == "v0.45.349")
        body_old = {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {"gamma_exposure": -0.2}}}}}
        body_new = {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {"gex_signal_in_score": False}}}}}
        (tmp_path / f"analysis-AAA-ml-{b349}.json").write_text(json.dumps(body_old), encoding="utf-8")
        (tmp_path / f"analysis-BBB-ml-{_shift(b349, 1)}.json").write_text(json.dumps(body_new), encoding="utf-8")
        _write_swarm(tmp_path, b349, {"AAA": body_old["swarm_results"]})
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.349")
        assert ev["verdict"] == "boundary_too_early", ev
        assert "equivalent_before_marker" not in ev

    def test_same_day_status_lists_369_without_alarm_before_deploy(self, tmp_path):
        """编排器 Step 11 读的 `boundary_evidence_status`：同日各条都核，369 在首跑前不报警。"""
        b = _b()
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False)})
        st = rr.boundary_evidence_status(tmp_path)
        per = {e["version"]: e for e in st["per_version"]}
        assert per[_V]["verdict"] == "no_evidence_yet" and per[_V]["alarm"] is False

    @pytest.mark.parametrize("det,want", [
        ({"data_quality": "real"}, True), ({"data_quality": "degraded"}, True), ({}, True),
        ({"data_quality": "unavailable"}, False)])
    def test_equivalence_predicate(self, det, want):
        d = {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": det}}}}
        assert rr._equiv_oracle_chain_available(d) is want

    @pytest.mark.parametrize("d", [
        {}, {"swarm_results": {"agent_details": {"OracleBeeEcho": None}}},
        {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {MARK: 1}}}}},
        {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {"data_quality": "real"}}}}}])
    def test_marker_only_literal_true(self, d):
        assert rr._marker_oracle_options_dq_from_agent(d) is False


def test_helper_is_the_single_definition():
    """判据只有一处：Oracle 的置信度与标签都走 `_options_data_usable`（防日后一处改了另一处没改）。"""
    src = (REPO_ROOT / "swarm_agents" / "oracle_bee.py").read_text(encoding="utf-8")
    assert "(bool(result), 0.3)" not in src and '"real" if result else' not in src
    assert ob._options_data_usable({"data_quality": "unavailable", "x": 1}) is False
    assert ob._options_data_usable({}) is False
    assert ob._options_data_usable({"data_quality": "degraded"}) is True
