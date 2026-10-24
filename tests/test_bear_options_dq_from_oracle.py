"""v0.45.639：BearBee 的 `data_quality["options"]` 照抄 OracleBee 发到板上的判定，不再「读到条目就记 real」。

此前 Oracle 的板上条目不带自己的期权数据质量判定，Bear 读到条目（有 discovery）就一律记 "real"——Oracle 走样本链早退（自己标
unavailable）或 OptionsAgent 抛异常（fallback）时，同一条记录里两只蜂对同一份期权数据一个说降级、一个说真实（生产 10-14 一行）。
Bear 自己的回落分支也是 `if result:` ⇒ "options_api"，判不出样本链早退的**非空** dict（同 Oracle 在 v0.45.369 修掉的形状）。

守四件事：
1. 真实 Oracle 三种情形（取到链 / 样本链早退 / OptionsAgent 抛异常）都把 `options_dq` 发上板，且等于它自己的 data_quality.options。
2. 真实 Oracle → 同一块板 → 真实 Bear：Bear 的标签与 Oracle 的逐情形相同（端到端不变式，不靠手写板上条目）。
3. 板上没有这个键 / 取值认不出 ⇒ Bear 记 "unavailable"（判不了就不报真实）。
4. Bear 回落分支：样本链早退 ⇒ "unavailable"；取到 ⇒ "options_api"。

⚠️ 全部离线、不用 skip。Oracle 走 `test_gex_oracle_bear_neutralized` 的同一套桩。
"""
from __future__ import annotations

import pytest

from pheromone_board import PheromoneBoard
from swarm_agents import BearBeeContrarian
from swarm_agents.oracle_bee import OPTIONS_DQ_BOARD_KEY
from tests.test_gex_oracle_bear_neutralized import TICKER, _options_stub, _run_oracle

#: 情形 → (OptionsAgent 桩, Oracle 应给的标签)
_SCENARIOS = {
    "chain_ok": (lambda: _options_stub(None), "real"),
    "sample_chain_early_exit": (lambda: _options_stub(None, extra={"data_quality": "unavailable"}), "unavailable"),
    "options_agent_raised": (lambda: _options_stub(None, raise_exc=ConnectionError("桩")), "fallback"),
}


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    import llm_service
    monkeypatch.setattr(llm_service, "is_available", lambda: False)


def _oracle_board_entry(board):
    """经 Bear 自己用的读法（`BeeAgent._read_peer`）取 Oracle 的条目——测的就是 Bear 实际看到的那一份。"""
    entry = BearBeeContrarian(board)._read_peer(TICKER, "OracleBeeEcho")
    assert entry is not None, "Oracle 没有发布到板上——本测试测不到读板路径"
    return entry


def _bear_options_label(board, monkeypatch, opt_stub=None):
    """真实 BearBee 的期权一项（只跑 `_assess_options_puts`，与 test_bear_bee_census_eviction 同一入口）。"""
    import options_analyzer
    if opt_stub is not None:
        monkeypatch.setattr(options_analyzer, "OptionsAgent", opt_stub)
    bear = BearBeeContrarian(board)
    ds: dict = {}
    bear._assess_options_puts(TICKER, 100.0, [], ds)
    return ds.get("options")


class TestOraclePublishesItsLabel:

    @pytest.mark.parametrize("case", sorted(_SCENARIOS))
    def test_board_carries_the_same_label_as_data_quality(self, monkeypatch, case):
        """变红的变异：上板那行挪回 `if result:` 里（抛异常那种情形板上就没有）；板上与 data_quality 各算一份。"""
        stub, expect = _SCENARIOS[case]
        out, board = _run_oracle(monkeypatch, stub())
        assert out["data_quality"]["options"] == expect, out["data_quality"]
        entry = _oracle_board_entry(board)
        assert (entry.details or {}).get(OPTIONS_DQ_BOARD_KEY) == expect


class TestBearCopiesOracleEndToEnd:

    @pytest.mark.parametrize("case", sorted(_SCENARIOS))
    def test_bear_label_equals_oracle_label(self, monkeypatch, case):
        """真实 Oracle 写板 → 真实 Bear 读板：两只蜂对同一份期权数据的标签一致。
        变红的变异：Bear 改回「读到条目就记 real」（后两种情形红）。"""
        stub, expect = _SCENARIOS[case]
        out, board = _run_oracle(monkeypatch, stub())
        assert _bear_options_label(board, monkeypatch) == out["data_quality"]["options"] == expect

    def test_bear_confidence_counts_real_only_when_oracle_had_the_chain(self, monkeypatch):
        """Bear confidence 的「real 源数」项随之如实：Oracle 降级那天少数一个 real。"""
        _, ok_board = _run_oracle(monkeypatch, _SCENARIOS["chain_ok"][0]())
        _, bad_board = _run_oracle(monkeypatch, _SCENARIOS["sample_chain_early_exit"][0]())
        ok, bad = _bear_options_label(ok_board, monkeypatch), _bear_options_label(bad_board, monkeypatch)
        assert (ok == "real") and (bad != "real")


class TestBearWhenBoardCannotTell:

    @pytest.mark.parametrize("details", [
        {"pc_ratio": 0.9, "iv_rank": 40.0},                                  # 旧形状：没有这个键
        {"pc_ratio": 0.9, "iv_rank": 40.0, OPTIONS_DQ_BOARD_KEY: "maybe"},   # 认不出的取值
        {"pc_ratio": 0.9, "iv_rank": 40.0, OPTIONS_DQ_BOARD_KEY: None},
    ], ids=["key_missing", "unknown_value", "none"])
    def test_unverifiable_is_unavailable(self, monkeypatch, details):
        from pheromone_board import PheromoneEntry
        board = PheromoneBoard()
        board.publish(PheromoneEntry(agent_id="OracleBeeEcho", ticker=TICKER, discovery="期权信号",
                                     source="options", self_score=5.0, direction="neutral", details=details))
        assert _bear_options_label(board, monkeypatch) == "unavailable"


class TestBearFallbackBranch:
    """板上没有 Oracle 条目 ⇒ Bear 自己调 OptionsAgent。"""

    def test_sample_chain_early_exit_is_unavailable(self, monkeypatch):
        """变红的变异：改回 `if result:` ⇒ "options_api"。"""
        stub = _options_stub(None, extra={"data_quality": "unavailable"})
        assert _bear_options_label(PheromoneBoard(), monkeypatch, opt_stub=stub) == "unavailable"

    def test_chain_ok_is_options_api(self, monkeypatch):
        assert _bear_options_label(PheromoneBoard(), monkeypatch, opt_stub=_options_stub(None)) == "options_api"


class TestLabelSetsAgree:

    def test_bear_maps_every_oracle_label_to_itself(self, monkeypatch):
        """Bear 的逐值映射是字面量（给 DQ 标签扫描器看）——这里钉住它与 Oracle 的取值集合一致：
        Oracle 将来加一种取值、Bear 没跟上 ⇒ 红（否则新取值会被 Bear 静默记成 unavailable）。"""
        from pheromone_board import PheromoneEntry
        from swarm_agents.oracle_bee import OPTIONS_DQ_LABELS
        assert OPTIONS_DQ_LABELS == {v for _s, v in _SCENARIOS.values()}, "情形表要覆盖 Oracle 的全部取值"
        for label in sorted(OPTIONS_DQ_LABELS):
            board = PheromoneBoard()
            board.publish(PheromoneEntry(agent_id="OracleBeeEcho", ticker=TICKER, discovery="期权信号",
                                         source="options", self_score=5.0, direction="neutral",
                                         details={OPTIONS_DQ_BOARD_KEY: label}))
            assert _bear_options_label(board, monkeypatch) == label


# ════════════════════════════════════════════════════════════════════════════
# 世代边界登记（用户 2026-10-24 选「边界 10-19 + 等价判据」，同 v0.45.369 / 559 的处置）
# ════════════════════════════════════════════════════════════════════════════

_V = "v0.45.639"
_MARK = "options_dq_from_oracle"


def _row(*, oracle="real", new=False, bear="ok", backfill=False):
    """一只标的的蒸馏结果（只放判据 / 印记读的字段）。bear：ok / errored（生产形状：details == {} + error）/ absent。"""
    agents, dq = {}, {}
    if oracle is not None:
        dq["OracleBeeEcho"] = {"options": oracle}
    if bear == "ok":
        det = {"data_sources": {"options": "real"}}
        if new:
            det[_MARK] = True
        agents["BearBeeContrarian"] = {"score": 5.0, "details": det}
    elif bear == "errored":
        agents["BearBeeContrarian"] = {"score": 5.0, "details": {}, "error": "TypeError: boom"}
    if backfill:
        agents["GuardBeeSentinel"] = {"details": {"vix_term_structure": {"macro_as_of_mode": "backfill"}}}
    return {"agent_details": agents, "data_quality": dq}


def _write(root, day, rows, archive=("AAA",)):
    import json
    (root / f".swarm_results_{day}.json").write_text(json.dumps(rows), encoding="utf-8")
    for tk in archive:
        (root / f"analysis-{tk}-ml-{day}.json").write_text(json.dumps({"swarm_results": rows.get(tk, _row())}),
                                                             encoding="utf-8")


class TestBoundaryRegistered:

    def test_entry_scope_marker_equivalence(self):
        import ic_rerun_readiness as rr
        import signal_archive as sa
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V] == ["2026-10-19"]
        assert sa.COHORT_SIGNAL_SCOPE[_V] == ()
        assert _V in rr._BOUNDARY_MARKERS and _V in rr._BOUNDARY_EQUIVALENCE

    def test_does_not_truncate_dim_ic_h1_or_h2(self):
        """变红的变异：删掉 `COHORT_SIGNAL_SCOPE["v0.45.639"]`（未声明 ⇒ 影响全部 ⇒ 截断 H1）。"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
        import dim_ic_forward_test as F
        import dim_ic_protocol as P
        import ic_rerun_readiness as rr
        entry = next(e for e in rr._COHORT_HISTORY if e[1] == _V)
        for name, _obj, sigs in P.HYPOTHESES:
            assert F.truncation_point(sigs, P, [entry]) is None, name

    def test_ml_estimator_generation_label_extended(self):
        import probability_scorecard as ps
        assert any(d == "2026-10-19" and _V in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)

    def test_bear_success_path_writes_the_marker_unconditionally(self):
        """BearBee 的 `AgentResult(details={...})` 字面量里无条件写着印记。漏写 ⇒ 新代码被判别器当旧代码 ⇒ 部署后误报。
        静态核（真实 `analyze` 要连 SEC / CBOE / Yahoo，离线闸不放行，桩太重）。变红的变异：删掉那一行 / 挪进 if。"""
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).resolve().parents[1] / "swarm_agents" / "bear_bee.py").read_text(encoding="utf-8"))
        found = []
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "AgentResult"):
                continue
            for kw in call.keywords:
                if kw.arg == "details" and isinstance(kw.value, ast.Dict):
                    found += [v for k, v in zip(kw.value.keys, kw.value.values)
                              if isinstance(k, ast.Constant) and k.value == _MARK]
        assert len(found) == 1 and isinstance(found[0], ast.Constant) and found[0].value is True, found

    def test_synthetic_fallback_writes_the_marker(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_generate_synthetic_swarm_results")
        hits = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)
                and any(isinstance(k, ast.Constant) and k.value == _MARK and isinstance(v, ast.Constant) and v.value is True
                        for k, v in zip(n.keys, n.values))]
        assert hits, f"合成回退里没有写 {_MARK}: True"


class TestMarkerAndJudge:

    @pytest.mark.parametrize("d", [{}, {"swarm_results": _row()}, {"swarm_results": _row(new=True, backfill=True)},
                                   {"swarm_results": _row(bear="errored")}],
                             ids=["empty", "old_code", "backfill", "errored"])
    def test_marker_only_on_live_new_rows(self, d):
        import ic_rerun_readiness as rr
        assert rr._marker_bear_options_dq_from_oracle(d) is False
        assert rr._marker_bear_options_dq_from_oracle({"swarm_results": _row(new=True)}) is True

    @pytest.mark.parametrize("row,expect", [
        (_row(oracle="real"), True),
        (_row(oracle="unavailable"), False),     # 本版会把 Bear 的 real 改成 unavailable 的那一种
        (_row(oracle="fallback"), False),
        (_row(oracle=None), False),              # 缺 Oracle 标签 ⇒ 证不出
        (_row(oracle="unavailable", bear="errored"), True),
        (_row(oracle="unavailable", bear="absent"), True),
    ], ids=["oracle_real", "oracle_unavailable", "oracle_fallback", "no_oracle", "bear_errored", "no_bear"])
    def test_judge(self, row, expect):
        """变红的变异：判据恒真 / 拿掉 `error` 分支。"""
        import ic_rerun_readiness as rr
        assert rr._equiv_bear_options_dq({"swarm_results": row}) is expect

    def test_real_queen_record_of_an_errored_bear_is_equivalent(self, queen):
        """出错的 Bear 经真实 Queen 写进 agent_details 的形状是 `details == {}` + error（v0.45.567 的教训：别手写想象的形状）。"""
        import ic_rerun_readiness as rr
        from swarm_agents.utils import make_error_result
        rec = {"swarm_results": queen.distill(TICKER, [make_error_result("BearBeeContrarian", "contrarian", TypeError("x"))])}
        b = rec["swarm_results"]["agent_details"]["BearBeeContrarian"]
        assert b["details"] == {} and b["error"]
        assert rr._equiv_bear_options_dq(rec) is True and rr._bear_row_has_options_dq_marker(rec) is False


class TestBoundaryVerdict:

    def test_before_deploy_is_no_evidence_yet(self, tmp_path):
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-23", {"AAA": _row(), "BBB": _row(bear="errored")})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_on_deploy_day_matches(self, tmp_path):
        """变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.639"]`（⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-26", {"AAA": _row(new=True), "BBB": _row(oracle="unavailable", bear="errored")})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-10-26", ev

    def test_old_code_day_with_oracle_downgraded_is_too_early(self, tmp_path):
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row(oracle="unavailable")})
        _write(tmp_path, "2026-10-26", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-10-19"], ev
