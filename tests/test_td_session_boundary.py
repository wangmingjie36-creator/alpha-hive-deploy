"""v0.45.423：twelve_data 当日那根改按交易所收盘判的世代边界（登记在 09-28，作废 0 条）——印记、等价判据、判别器。

守三件事：
1. **印记**：`details.td_session_aware` 字面量 True，Oracle 每条返回路径都写（含日报合成回退），经 Queen 进归档后判别器认得出。
2. **等价判据** `_equiv_td_session_bar`：本版只在两条 Twelve Data 兜底路径上改分——成交量回落（yfinance 日线抛错时）与
   日线缺口第二源。BuzzBee `volatility_20d` 是有限数（⇒ volume_ratio 来自 yfinance）且缺口全补上 ⇒ 等价；证不出 ⇒ 不等价。
3. **判别器**：新代码首跑前 `no_evidence_yet`、首跑后 `matches`；窗口里有证不出等价的旧记录 ⇒ `boundary_too_early`。

⚠️ 全部离线、不用 skip：合成数据。Oracle 走 `test_gex_oracle_bear_neutralized` 的同一套桩（同 v0.45.383 的测试）。
"""
from __future__ import annotations

import ast
import copy
import json

import pytest

import ic_rerun_readiness as rr
from pheromone_board import PheromoneBoard
from tests.test_gex_oracle_bear_neutralized import REPO_ROOT, TICKER, _options_stub, _run_oracle

MARK = "td_session_aware"
_V = "v0.45.423"


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    import llm_service
    monkeypatch.setattr(llm_service, "is_available", lambda: False)


# ════════════════════════════════════════════════════════════════════════════
# 1. 印记
# ════════════════════════════════════════════════════════════════════════════

class TestMarkerOnEveryPath:

    def _marked(self, out):
        return isinstance(out.get("details"), dict) and out["details"].get(MARK) is True

    def test_success(self, monkeypatch):
        out, _ = _run_oracle(monkeypatch, _options_stub(None))
        assert self._marked(out)

    def test_literal_overrides_upstream(self, monkeypatch):
        """变红的变异：把字面量挪到 `**(result or {})` 之前。"""
        out, _ = _run_oracle(monkeypatch, _options_stub(None, extra={MARK: False}))
        assert self._marked(out)

    def test_agent_exception(self, monkeypatch):
        out, _ = _run_oracle(monkeypatch, _options_stub(None, raise_exc=ConnectionError("桩")))
        assert self._marked(out)

    def test_error_fallback(self, monkeypatch):
        """变红的变异：`_mark_gex_out_of_score` 不再写本印记。"""
        out, _ = _run_oracle(monkeypatch, _options_stub(None), stock={"momentum_5d": 1.0})
        assert out.get("error") and self._marked(out)

    def test_invalid_ticker(self, monkeypatch):
        out, _ = _run_oracle(monkeypatch, _options_stub(None), ticker="bad ticker!")
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
        assert chains, f"合成回退里没有写 {MARK} = True"
        assert any(not ({"If", "Try", "ExceptHandler"} & set(c)) for c in chains), chains

    def test_marker_survives_queen_into_archive_shape(self, monkeypatch):
        """写端 ↔ 读端：Oracle 真输出经 QueenDistiller 进 agent_details 后，判别器认得出。"""
        from swarm_agents.queen_distiller import QueenDistiller
        oracle, _ = _run_oracle(monkeypatch, _options_stub(None))
        dims = {"ScoutBeeNova": "signal", "BuzzBeeWhisper": "sentiment", "ChronosBeeHorizon": "catalyst",
                "GuardBeeSentinel": "risk_adj", "RivalBeeVanguard": "ml_auxiliary", "BearBeeContrarian": "contrarian"}
        results = [{"source": a, "dimension": d, "direction": "neutral", "score": 5.0, "confidence": 0.5,
                    "discovery": f"{a} 合成", "data_quality": {"x": "real"},
                    "details": {"macro_regime": "neutral"} if a == "GuardBeeSentinel" else {}}
                   for a, d in dims.items()] + [oracle]
        out = QueenDistiller(PheromoneBoard(), enable_llm=False, ml_model=None).distill(
            TICKER, copy.deepcopy(results), dealer_gex={"regime": "unknown"})
        assert rr._marker_oracle_td_session_aware({"swarm_results": out}) is True

    @pytest.mark.parametrize("d", [
        {}, {"swarm_results": {"agent_details": {"OracleBeeEcho": None}}},
        {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {MARK: 1}}}}},
        {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {"hv_gap_checked": True}}}}}])
    def test_marker_only_literal_true(self, d):
        """只认 `is True`；且**不能**被别的印记（383 的 hv_gap_checked）顶替。"""
        assert rr._marker_oracle_td_session_aware(d) is False


# ════════════════════════════════════════════════════════════════════════════
# 2. 等价判据
# ════════════════════════════════════════════════════════════════════════════

def _row(*, new=False, vol20=22.5, gaps=(), filled=(), checked=True, has_filled=True, buzz=True):
    """一只标的的蒸馏结果（只放判据读的字段）。默认 = 10-06 生产实拍的形状：volatility_20d 有值、无缺口。"""
    ora = {"iv_rank": 30.0}
    if checked:
        ora["hv_gap_checked"] = True
        ora["hv_gap"] = list(gaps)
        if has_filled:
            ora["hv_gap_filled"] = list(filled)
    if new:
        ora[MARK] = True
    agents = {"OracleBeeEcho": {"score": 5.0, "direction": "neutral", "details": ora}}
    if buzz:
        agents["BuzzBeeWhisper"] = {"details": {"volatility_20d": vol20, "volume_ratio": 1.1}}
    return {"agent_details": agents}


def _wrap(row, day="2026-10-01", tk="AAA"):
    return {"swarm_results": row, "_date": day, "_ticker": tk}


class TestEquivalenceJudge:

    @pytest.mark.parametrize("row,expect", [
        (_row(), True),                                                   # 正常日：yfinance 供量、无缺口
        (_row(vol20=None), False),                                        # 可能走了成交量回落 ⇒ 证不出
        (_row(vol20=float("nan")), False),
        (_row(vol20=True), False),                                        # bool 不是数
        (_row(buzz=False), False),
        (_row(gaps=["2026-09-30"]), False),                               # 有缺口没补上 ⇒ 本版可能补得上
        (_row(gaps=["2026-09-30"], filled=["2026-09-30"]), True),         # 缺口旧代码已补上 ⇒ 不变
        (_row(gaps=["2026-09-29", "2026-09-30"], filled=["2026-09-30"]), False),
        (_row(has_filled=False), True),                                   # 387 之前（09-29）：没有第二源，无缺口
        (_row(has_filled=False, gaps=["2026-09-26"]), False),             # 387 之前、有缺口 ⇒ 今天的代码会去借 ⇒ 证不出
    ], ids=["normal", "vol20_none", "vol20_nan", "vol20_bool", "no_buzz", "gap_unfilled", "gap_filled",
            "gap_partly_filled", "pre387_no_gap", "pre387_gap"])
    def test_judge(self, row, expect):
        """变红的变异：volatility_20d 判据恒真 / 缺口判据恒真 / 387 前缺 hv_gap_filled 一律放行。"""
        assert rr._equiv_td_session_bar(_wrap(row)) is expect

    def test_pre383_rows_read_the_frozen_383_evidence(self, tmp_path, monkeypatch):
        """383 首跑前（09-28）的记录没有 hv_gap 字段 ⇒ 按（日期，标的）查 383 的冻结证据。"""
        doc = {"days": {"2026-09-28": {"tickers": {"AAA": {"status": "verified"}, "BBB": {"status": "mismatch"}}}}}
        p = tmp_path / "ev.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        monkeypatch.setattr(rr, "_HV_GAP_EVIDENCE_PATH", p)
        old = _row(checked=False)
        assert rr._equiv_td_session_bar(_wrap(old, "2026-09-28", "AAA")) is True
        assert rr._equiv_td_session_bar(_wrap(old, "2026-09-28", "BBB")) is False
        assert rr._equiv_td_session_bar(_wrap(old, "2026-09-28", "CCC")) is False
        assert rr._equiv_td_session_bar(_wrap(_row(checked=False, vol20=None), "2026-09-28", "AAA")) is False


# ════════════════════════════════════════════════════════════════════════════
# 3. 判别器
# ════════════════════════════════════════════════════════════════════════════

def _write(root, day, rows, archive=("AAA",)):
    (root / f".swarm_results_{day}.json").write_text(json.dumps(rows), encoding="utf-8")
    for tk in archive:
        (root / f"analysis-{tk}-ml-{day}.json").write_text(json.dumps({"swarm_results": rows.get(tk, _row())}),
                                                             encoding="utf-8")


class TestBoundaryVerdict:

    def test_registered_on_the_09_28_boundary_with_marker_and_equivalence(self):
        assert next(d for d, v, _r in rr._COHORT_HISTORY if v == _V) == "2026-09-28"
        assert _V in rr._BOUNDARY_MARKERS and _V in rr._BOUNDARY_EQUIVALENCE

    def test_before_deploy_equivalent_window_is_no_evidence_yet(self, tmp_path):
        """上线前的真实形状：09-28 起全是旧代码、全部可证等价 ⇒ 还没证据，**不报警**。"""
        _write(tmp_path, "2026-09-28", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-07", {"AAA": _row(), "BBB": _row(gaps=["2026-10-06"], filled=["2026-10-06"])})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_on_deploy_day_matches(self, tmp_path):
        """首跑后：印记首见 10-08，其前只有可证等价的旧记录 ⇒ matches（作废 0 条）。
        变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.423"]`（⇒ boundary_too_early）。"""
        _write(tmp_path, "2026-09-28", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-08", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-10-08", ev
        assert ev["equivalent_before_marker"] == ["2026-09-28"]

    @pytest.mark.parametrize("bad_row", [_row(vol20=None), _row(gaps=["2026-10-01"])], ids=["volume_fallback", "gap"])
    def test_unprovable_old_row_is_too_early(self, tmp_path, bad_row):
        _write(tmp_path, "2026-10-02", {"AAA": _row(), "BBB": bad_row})
        _write(tmp_path, "2026-10-08", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-10-02"], ev

    def test_archive_without_swarm_row_is_too_early(self, tmp_path):
        """旧代码写过 ML 归档、当日 .swarm_results 里却没有这只票 ⇒ 证不出等价（同 369 的二次审查）。"""
        _write(tmp_path, "2026-10-02", {"AAA": _row()}, archive=("AAA", "ZZZ"))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early", ev
