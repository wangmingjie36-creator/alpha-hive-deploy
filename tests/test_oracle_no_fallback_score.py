"""v0.45.765：OracleBee 取不到数据时不再拿兜底常量进分；世代边界并入 11-09（作废 0 条，等价论证）。

用户 2026-11-11：「我不想要假评分会污染综合评分」，选「两条都改」。

守五件事：
1. **异常流兜底不进分**：`detect_unusual_flow` 的兜底 dict（data_source=fallback、unusual_score 恒 5.0）与检测器抛异常（flow 为空）
   两条「没取到」的路径同分、都只用期权分；真取到的（哪怕 5.0）照旧混入。（逐路径的分数断言在 `test_oracle_unusual_flow_visibility`）
2. **期权链取不到 ⇒ 弃权**：error 形状、不上板、不调 LLM；经真实 Queen：**弃权 ≡ 缺席**（final_score / 方向与 Oracle 根本不在逐位相同），
   odds 维记 error、缺席原因可读。
3. **印记**：`details.fallback_not_scored` 字面量 True，每条返回路径都写（含弃权与日报合成回退），经 Queen 进归档后判别器认得出。
4. **等价判据与真实行为一致**（差分）：同一组输入，判据说「旧代码输出不变」⇔ 新 Oracle 的分 = 旧融合公式复刻的分且没弃权。
5. **登记**：`_COHORT_HISTORY` 同日并入 11-09、作用域、印记 / 判据、ML 世代标签、H1 不截断、冻结守卫放行；判别器的三种判定。

⚠️ 全部离线、不用 skip：Oracle 的 OptionsAgent / 异常流都用 `test_gex_oracle_bear_neutralized` 的同一套桩。
"""
from __future__ import annotations

import ast
import copy
import json

import pytest

from pheromone_board import PheromoneBoard
from swarm_agents import oracle_bee as ob
from swarm_agents.queen_distiller import QueenDistiller
from tests.test_gex_oracle_bear_neutralized import REPO_ROOT, TICKER, _options_stub
from swarm_agents.oracle_bee import OracleBeeEcho

_V = "v0.45.765"
MARK = "fallback_not_scored"
assert ob.NO_FALLBACK_SCORE_MARKER == MARK

#: 异常流的三种形状（生产 `detect_unusual_flow` 的真实返回形状 / 抛异常）
_FLOW_OBSERVED = {"unusual_score": 7.5, "unusual_direction": "neutral", "signals": [], "summary": "Call 主导",
                  "data_source": "yfinance_chain", "fetch_status": "ok", "chains_total": 4, "chains_failed": 0}
_FLOW_FALLBACK = {"unusual_score": 5.0, "unusual_direction": "neutral", "signals": [], "summary": "期权链全部取数失败",
                  "data_source": "fallback", "fetch_status": "failed", "chains_total": 4, "chains_failed": 4}
_FLOWS = {"observed": _FLOW_OBSERVED, "fallback": _FLOW_FALLBACK, "raised": ConnectionError("桩：检测器挂了")}

#: 期权链的三种情形
_CHAINS = {
    "chain_ok": lambda: _options_stub(None),
    "sample_chain_early_exit": lambda: _options_stub(None, extra={"data_quality": "unavailable", "options_score": 5.0}),
    "options_agent_raised": lambda: _options_stub(None, raise_exc=ConnectionError("桩：链不可用")),
}


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    import llm_service
    monkeypatch.setattr(llm_service, "is_available", lambda: False)


def _run(monkeypatch, chain="chain_ok", flow="observed", *, stock=None, ticker=TICKER, board=None):
    import options_analyzer as oa
    import unusual_options
    monkeypatch.setattr(oa, "OptionsAgent", _CHAINS[chain]())
    f = _FLOWS[flow]

    def _detect(t, stock_price=None):
        if isinstance(f, Exception):
            raise f
        return dict(f)
    monkeypatch.setattr(unusual_options, "detect_unusual_flow", _detect)
    board = board or PheromoneBoard()
    bee = OracleBeeEcho(board)
    bee._prefetched_stock[ticker] = dict(stock or {"price": 100.0, "momentum_5d": 1.0})
    bee._prefetched_context[ticker] = ""
    return bee.analyze(ticker), board


def _peers(scores=None):
    """其余四维 + 两只非维度蜂的合成结果（Buzz / Chronos 给不同分，好让重新归一真起作用）。"""
    scores = scores or {"BuzzBeeWhisper": 7.0, "ChronosBeeHorizon": 4.0}
    dims = {"ScoutBeeNova": "signal", "BuzzBeeWhisper": "sentiment", "ChronosBeeHorizon": "catalyst",
            "GuardBeeSentinel": "risk_adj", "RivalBeeVanguard": "ml_auxiliary", "BearBeeContrarian": "contrarian"}
    return [{"source": a, "dimension": d, "direction": "neutral", "score": scores.get(a, 5.0), "confidence": 0.6,
             "discovery": f"{a} 合成", "data_quality": {"x": "real"},
             "details": {"macro_regime": "neutral"} if a == "GuardBeeSentinel" else {}}
            for a, d in dims.items()]


def _distill(results):
    return QueenDistiller(PheromoneBoard(), enable_llm=False, ml_model=None).distill(
        TICKER, copy.deepcopy(results), dealer_gex={"regime": "unknown"})


# ════════════════════════════════════════════════════════════════════════════
# 1. 异常流兜底不进分（判据函数本身；分数断言见 test_oracle_unusual_flow_visibility）
# ════════════════════════════════════════════════════════════════════════════

class TestUnusualObserved:

    @pytest.mark.parametrize("flow,expect", [
        (_FLOW_OBSERVED, True),
        (dict(_FLOW_OBSERVED, fetch_status="partial"), True),
        (_FLOW_FALLBACK, False),
        (dict(_FLOW_FALLBACK, fetch_status="oi_stale"), False),
        ({}, False),
        (None, False),
    ], ids=["ok", "partial", "failed", "oi_stale", "empty", "none"])
    def test_judge(self, flow, expect):
        assert ob._unusual_observed(flow) is expect

    def test_both_failure_paths_score_identically_on_a_live_chain(self, monkeypatch):
        """变红的变异：融合基数改回 `if unusual_flow`（兜底 5.0 混入 ⇒ fallback 与 raised 分开）。"""
        fb, _ = _run(monkeypatch, flow="fallback")
        rz, _ = _run(monkeypatch, flow="raised")
        ok, _ = _run(monkeypatch, flow="observed")
        assert "error" not in fb and "error" not in rz
        assert (fb["score"], fb["direction"]) == (rz["score"], rz["direction"])
        assert ok["score"] != fb["score"], "夹具自证：取到的异常流（7.5）确实改分"

    def test_fallback_never_enters_even_with_a_non_neutral_score(self, monkeypatch):
        """兜底 dict 今天恒 5.0 ⇒ 调整项 (5.0−5.0)·0.3 恰好为 0，「调整项仍按旧判据」的变异在生产形状上看不出来。
        这里喂一个分数不是 5.0 的兜底（构造出来的形状）：基数与调整项**都**必须不认它。
        变红的变异：调整项的条件改成恒真 / 只认 data_source 而不经 `_unusual_observed`（基数、调整项只改了一处）。"""
        monkeypatch.setitem(_FLOWS, "fallback_hot", dict(_FLOW_FALLBACK, unusual_score=9.0, unusual_direction="neutral"))
        hot, _ = _run(monkeypatch, flow="fallback_hot")
        rz, _ = _run(monkeypatch, flow="raised")
        assert "error" not in hot and hot["score"] == rz["score"]


# ════════════════════════════════════════════════════════════════════════════
# 2. 期权链取不到 ⇒ 弃权
# ════════════════════════════════════════════════════════════════════════════

class TestAbstainWhenChainUnavailable:

    @pytest.mark.parametrize("chain,label", [("sample_chain_early_exit", "unavailable"),
                                             ("options_agent_raised", "fallback")])
    @pytest.mark.parametrize("flow", sorted(_FLOWS))
    def test_error_shaped_and_off_the_board(self, monkeypatch, chain, label, flow):
        """不管异常流取没取到，链不可用就弃权——取到的异常流也不单独撑起一个 odds 分。
        变红的变异：弃权分支只在异常流也没取到时才走；或弃权前已经 `_publish`。"""
        out, board = _run(monkeypatch, chain, flow)
        assert out.get("error", "").startswith(ob.OPTIONS_UNAVAILABLE_ERROR), out.get("error")
        assert out["data_quality"] == {"options": label}
        assert out["confidence"] == 0.0 and out["dimension"] == "odds"
        assert board.get_agent_entry(TICKER, "OracleBeeEcho") is None, "弃权不该上信息素板"
        assert board.get_entry_count() == 0
        det = out["details"]
        assert det.get(MARK) is True
        assert "unusual_flow_ok" in det and "unusual_flow_status" in det, "取数状态照记（覆盖率闸读它）"

    def test_llm_not_consulted_when_abstaining(self, monkeypatch):
        """没有期权数据可解读：弃权路径不调 LLM（`is_available` 打开也不调）。变红的变异：把弃权分支挪到 LLM 块之后。"""
        import llm_service
        calls = []
        monkeypatch.setattr(llm_service, "is_available", lambda: True)
        monkeypatch.setattr(llm_service, "interpret_options_flow", lambda *a, **k: calls.append(a) or None)
        out, _ = _run(monkeypatch, "sample_chain_early_exit", "observed")
        assert out.get("error") and calls == []

    def test_chain_ok_still_scores_and_publishes(self, monkeypatch):
        """反向：链在 ⇒ 照旧出分、上板（板上带 real 标签，BearBee 照抄）。"""
        out, board = _run(monkeypatch, "chain_ok", "fallback")
        assert "error" not in out and out["data_quality"] == {"options": "real"}
        entry = board.get_agent_entry(TICKER, "OracleBeeEcho")
        assert entry is not None and (entry.details or {}).get(ob.OPTIONS_DQ_BOARD_KEY) == "real"


class TestQueenTreatsAbstainAsAbsent:

    @pytest.mark.parametrize("chain", ["sample_chain_early_exit", "options_agent_raised"])
    def test_abstain_equals_oracle_absent(self, monkeypatch, chain):
        """弃权 ≡ 缺席：final_score、方向、维度覆盖率与 Oracle 根本不在逐位相同 ⇒ 没有任何兜底值漏进综合分。
        变红的变异：弃权结果去掉 `error` 键（Queen 把它的 5.0 当成 odds 分）。"""
        oracle, _ = _run(monkeypatch, chain, "observed")
        with_abstain = _distill(_peers() + [oracle])
        absent = _distill(_peers())
        for k in ("final_score", "direction", "dimension_coverage_pct"):
            assert with_abstain[k] == absent[k], (k, with_abstain[k], absent[k])
        assert with_abstain["dimension_status"]["odds"] == "error"
        assert with_abstain["dimension_missing_reason"]["odds"].startswith(ob.OPTIONS_UNAVAILABLE_ERROR[:20])
        # 落进归档的那一份：error 透出、details（含印记）照留
        ad = with_abstain["agent_details"]["OracleBeeEcho"]
        assert ad["error"] and ad["details"].get(MARK) is True

    @pytest.mark.parametrize("chain", ["sample_chain_early_exit", "options_agent_raised"])
    def test_alphabot_deep_page_sees_abstain_as_failed(self, monkeypatch, chain):
        """v0.45.767（765 二次检查）：Alpha Bot 深度页按 discovery 前缀（Error / ❌）认出错蜂——老记录没有 error 键，所以它不读那个键。
        765 弃权的 discovery 原本不带前缀 ⇒ 深度页把 Oracle 数成一张中性票（扫描本身没数它）、也不标出错。
        这里走真实 Oracle → 真实 Queen → 真实 `deep_report`：投票与「Oracle 根本不在」相同，Oracle 被点名为出错、odds 不进分。
        变红的变异：discovery 去掉 "Error: " 前缀。"""
        from alphabot import deep_report as DR
        oracle, _ = _run(monkeypatch, chain, "observed")
        with_abstain = _distill(_peers() + [oracle])
        absent = _distill(_peers())
        v, v0 = DR._votes(with_abstain), DR._votes(absent)
        assert (v["counts"], v["voters"]) == (v0["counts"], v0["voters"]), (v, v0)
        assert v["failed"] == ["OracleBeeEcho"] and v0["failed"] == []
        ag = {a["name"]: a for a in DR._agents(with_abstain)}["OracleBeeEcho"]
        assert ag["failed"] is True and ag["votes"] is False and ag["in_score"] is False
        # 网站价格回退 1 从 Oracle discovery 抠 `$数字`：前缀不能挡住它
        import re
        assert re.search(r'\$(\d+(?:\.\d+)?)', with_abstain["agent_details"]["OracleBeeEcho"]["discovery"]).group(1) == "100.0"

    def test_fixture_bites_a_scoring_oracle_moves_the_final_score(self, monkeypatch):
        """夹具自证：链在时 Oracle 的分确实进综合分（否则上一条「相同」证明不了什么）。"""
        oracle, _ = _run(monkeypatch, "chain_ok", "observed")
        assert oracle["score"] != pytest.approx(5.0)
        assert _distill(_peers() + [oracle])["final_score"] != _distill(_peers())["final_score"]


# ════════════════════════════════════════════════════════════════════════════
# 3. 印记
# ════════════════════════════════════════════════════════════════════════════

class TestMarkerOnEveryPath:

    @pytest.mark.parametrize("chain", sorted(_CHAINS))
    @pytest.mark.parametrize("flow", sorted(_FLOWS))
    def test_success_and_abstain(self, monkeypatch, chain, flow):
        out, _ = _run(monkeypatch, chain, flow)
        assert out["details"].get(MARK) is True

    def test_literal_overrides_upstream(self, monkeypatch):
        """变红的变异：把字面量挪到 `**(result or {})` 之前。"""
        import options_analyzer as oa
        out, _ = _run(monkeypatch)
        monkeypatch.setattr(oa, "OptionsAgent", _options_stub(None, extra={MARK: False}))
        bee = OracleBeeEcho(PheromoneBoard())
        bee._prefetched_stock[TICKER] = {"price": 100.0, "momentum_5d": 1.0}
        bee._prefetched_context[TICKER] = ""
        assert bee.analyze(TICKER)["details"].get(MARK) is True

    def test_error_fallback(self, monkeypatch):
        """`_get_stock_data` 没有 price ⇒ KeyError ⇒ make_error_result。变红的变异：`_mark_gex_out_of_score` 不写本印记。"""
        out, _ = _run(monkeypatch, stock={"momentum_5d": 1.0})
        assert out.get("error") and not out["error"].startswith(ob.OPTIONS_UNAVAILABLE_ERROR)
        assert out["details"].get(MARK) is True

    def test_invalid_ticker(self, monkeypatch):
        out, _ = _run(monkeypatch, ticker="bad ticker!")
        assert out.get("error") == "invalid_ticker" and out["details"].get(MARK) is True

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

    @pytest.mark.parametrize("chain", ["chain_ok", "sample_chain_early_exit"])
    def test_marker_survives_queen_into_archive_shape(self, monkeypatch, chain):
        """写端 ↔ 读端：真实 Oracle 输出（成功 / 弃权）经 Queen 进 agent_details 后，判别器认得出；缺键的旧记录认不出。"""
        import ic_rerun_readiness as rr
        oracle, _ = _run(monkeypatch, chain, "observed")
        rec = {"swarm_results": _distill(_peers() + [oracle])}
        _desc, judge = rr._BOUNDARY_MARKERS[_V]
        assert judge(rec) is True and rr._oracle_row_has_no_fallback_marker(rec) is True
        legacy = copy.deepcopy(rec)
        legacy["swarm_results"]["agent_details"]["OracleBeeEcho"]["details"].pop(MARK)
        assert judge(legacy) is False


# ════════════════════════════════════════════════════════════════════════════
# 4. 等价判据与真实行为一致（差分）
# ════════════════════════════════════════════════════════════════════════════

def _legacy_score(options_score: float, flow) -> float:
    """v0.45.765 之前的融合公式逐行复刻（`oracle_bee.analyze` 旧代码；本桩无期限结构 / skew / LLM 调整）。
    末尾同 `AgentResult(score=round(score, 2))` 取两位——比的是落盘的那个数。"""
    from config import AGENT_SCORING as AS
    from swarm_agents.utils import clamp_score
    adj = 0.0
    if flow and flow.get("data_source") != "fallback":
        adj = (flow.get("unusual_score", 5.0) - 5.0) * 0.3
    ow, uw = AS.get("oracle_options_weight", 0.55), AS.get("oracle_unusual_weight", 0.10)
    base = flow.get("unusual_score", None) if flow else None
    s = (options_score * ow + base * uw) / (ow + uw) if base is not None else options_score
    return round(clamp_score(s + adj), 2)


def _as_old_record(new_out: dict) -> dict:
    """同一组输入下旧代码会落进归档的那一份：判据读的字段（data_quality.options 标签、details 的取数状态）旧代码同样写，
    只是旧代码不弃权（没有 error）、没有本版印记。"""
    o = copy.deepcopy(new_out)
    o.pop("error", None)
    o["details"].pop(MARK, None)
    return {"swarm_results": {"agent_details": {"OracleBeeEcho": o},
                              "data_quality": {"OracleBeeEcho": o["data_quality"]}}}


class TestJudgeAgreesWithBehaviour:

    @pytest.mark.parametrize("chain", sorted(_CHAINS))
    @pytest.mark.parametrize("flow", sorted(_FLOWS))
    def test_judge_iff_output_unchanged(self, monkeypatch, chain, flow):
        """九种输入组合：判据为真 ⇔ 新代码没弃权且分数 = 旧公式复刻。
        变红的变异：判据恒真 / 不看 unusual_flow_status / 不看 options 标签；Oracle 的融合改了而判据没跟上。"""
        import ic_rerun_readiness as rr
        new, _ = _run(monkeypatch, chain, flow)
        f = _FLOWS[flow]
        flow_dict = {} if isinstance(f, Exception) else f
        if new.get("error"):
            unchanged = False                       # 旧代码在这里出了一个分；新代码弃权
        else:
            unchanged = new["score"] == _legacy_score(new["details"]["options_score"], flow_dict)
        assert rr._equiv_oracle_no_fallback_score(_as_old_record(new)) is unchanged, (chain, flow, new.get("score"))

    def test_both_sides_of_the_judge_are_reached(self, monkeypatch):
        """夹具自证：九种组合里判据真、假两种都出现（否则上一条只验了一半）。"""
        import ic_rerun_readiness as rr
        seen = set()
        for chain in _CHAINS:
            for flow in _FLOWS:
                new, _ = _run(monkeypatch, chain, flow)
                seen.add(rr._equiv_oracle_no_fallback_score(_as_old_record(new)))
        assert seen == {True, False}


def _row(*, dq="real", source="yfinance_chain", new=False, oracle="ok", status=True):
    """一只标的的蒸馏结果（只放判据 / 印记读的字段）。oracle：ok / errored（Queen 记的形状：error + details）/ absent。"""
    agents, dqs = {"ScoutBeeNova": {"details": {}}}, {}
    if oracle == "ok":
        det = {"unusual_flow_status": {"status": "ok", "data_source": source}} if status else {}
        if new:
            det[MARK] = True
        agents["OracleBeeEcho"] = {"score": 6.0, "details": det}
        dqs["OracleBeeEcho"] = {"options": dq}
    elif oracle == "errored":
        agents["OracleBeeEcho"] = {"score": 5.0, "error": "KeyError: 'price'",
                                   "details": {MARK: True} if new else {}}
    return {"agent_details": agents, "data_quality": dqs}


class TestJudgeTable:

    @pytest.mark.parametrize("row,expect", [
        (_row(), True),
        (_row(source=None), True),                  # 检测器抛异常（flow 为空）：旧代码那条本来就只用期权分
        (_row(source="fallback"), False),           # ① 异常流兜底：旧代码混了 5.0
        (_row(dq="unavailable"), False),            # ② 样本链早退：新代码弃权
        (_row(dq="fallback"), False),               # ② OptionsAgent 抛异常：新代码弃权
        (_row(status=False), False),                # 缺取数状态记录 ⇒ 证不出
        (_row(oracle="errored"), True),
        (_row(oracle="absent"), True),
    ], ids=["real_observed", "real_detector_raised", "unusual_fallback", "chain_unavailable", "chain_fallback",
            "no_status", "oracle_errored", "no_oracle"])
    def test_judge(self, row, expect):
        import ic_rerun_readiness as rr
        assert rr._equiv_oracle_no_fallback_score({"swarm_results": row}) is expect

    def test_marker_only_on_live_new_rows(self):
        import ic_rerun_readiness as rr
        backfill = _row(new=True)
        backfill["agent_details"]["GuardBeeSentinel"] = {"details": {"vix_term_structure": {"macro_as_of_mode": "backfill"}}}
        assert rr._marker_oracle_fallback_not_scored({"swarm_results": _row(new=True)}) is True
        assert rr._marker_oracle_fallback_not_scored({"swarm_results": backfill}) is False
        assert rr._oracle_row_has_no_fallback_marker({"swarm_results": backfill}) is True, "新代码补跑行仍是新代码（等价扫描要跳过它）"
        assert rr._marker_oracle_fallback_not_scored({"swarm_results": _row()}) is False
        assert rr._marker_oracle_fallback_not_scored({}) is False

    def test_judge_is_not_the_marker(self):
        """判据对绝大多数旧记录成立 ⇒ 不能拿它当印记（当了就把全部旧归档认成新口径）。"""
        import ic_rerun_readiness as rr
        assert rr._equiv_oracle_no_fallback_score({"swarm_results": _row()}) is True
        assert rr._BOUNDARY_MARKERS[_V][1]({"swarm_results": _row()}) is False


# ════════════════════════════════════════════════════════════════════════════
# 5. 登记与判别器
# ════════════════════════════════════════════════════════════════════════════

class TestBoundaryRegistered:

    def test_merged_into_the_current_cohort_start(self):
        """同日并入 11-09（作废 0 条），不开新世代；冻结守卫因此放行（变红的变异：日期写成首跑日 ⇒ TestScoringFreeze 红）。"""
        import ic_rerun_readiness as rr
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V] == ["2026-11-09"]
        assert rr._SCORING_FREEZE["since"] == "2026-11-09"
        assert _V not in rr._FREEZE_EXCEPTIONS
        assert rr.scoring_freeze_violations() == []
        assert _V in rr._BOUNDARY_MARKERS and _V in rr._BOUNDARY_EQUIVALENCE
        assert rr._BOUNDARY_EQUIVALENCE[_V][1] is rr._equiv_oracle_no_fallback_score
        assert rr._BOUNDARY_EQUIVALENCE[_V][2] is rr._oracle_row_has_no_fallback_marker

    def test_signal_scope(self):
        import signal_archive as sa
        assert sa.COHORT_SIGNAL_SCOPE[_V] == ("agent.OracleBeeEcho.*", "bear.options_bear")

    def test_only_the_oracle_signals_move(self):
        """分信号世代起点：只有 Oracle 的分 / 方向与 bear.options_bear 被挪到 11-09（下游此前已因 v0.45.750 在 11-09）。
        变红的变异：作用域漏声明（⇒ 影响全部信号）或多点名。"""
        import ic_rerun_readiness as rr
        import signal_archive as sa
        sigs = set(sa.SIGNAL_EXTRACTORS)
        # 比「765 登上那一刻」的表：表里 765 之前的条目 vs 再加上 765。不用「全表去掉 765」——之后同日并入的条目
        # （v0.45.770 也点名 bear.options_bear）会把它在 before 里挪回 11-09，看起来像 765 没动它。
        idx = [e[1] for e in rr._COHORT_HISTORY].index(_V)
        before = sa.generation_boundaries(sigs, history=rr._COHORT_HISTORY[:idx])
        after = sa.generation_boundaries(sigs, history=rr._COHORT_HISTORY[:idx + 1])
        moved = {s for s in after if (before.get(s) or {}).get("date") != after[s]["date"]}
        assert moved == {"agent.OracleBeeEcho.score", "agent.OracleBeeEcho.direction", "bear.options_bear"}, moved
        assert {after[s]["date"] for s in moved} == {"2026-11-09"}

    def test_does_not_truncate_dim_ic_h1(self):
        import sys
        sys.path.insert(0, str(REPO_ROOT / "experiments"))
        import dim_ic_forward_test as F
        import dim_ic_protocol as P
        import ic_rerun_readiness as rr
        entry = next(e for e in rr._COHORT_HISTORY if e[1] == _V)
        for name, _obj, sigs in P.HYPOTHESES:
            assert F.truncation_point(sigs, P, [entry]) is None, name

    def test_ml_estimator_generation_label_extended(self):
        import probability_scorecard as ps
        assert any(d == "2026-11-09" and _V in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)


def _write(root, day, rows, archive=("AAA",)):
    (root / f".swarm_results_{day}.json").write_text(json.dumps(rows), encoding="utf-8")
    for tk in archive:
        (root / f"analysis-{tk}-ml-{day}.json").write_text(json.dumps({"swarm_results": rows.get(tk, _row())}),
                                                             encoding="utf-8")


class TestBoundaryVerdict:

    def test_before_deploy_is_no_evidence_yet(self, tmp_path):
        """11-09 / 11-10 生产的形状：旧代码、全部等价（Oracle 链在、异常流取到）⇒ 还没证据，但不报 too_early。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-11-09", {"AAA": _row(), "BBB": _row(source=None)})
        _write(tmp_path, "2026-11-10", {"AAA": _row(), "BBB": _row(oracle="errored")})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_on_deploy_day_matches(self, tmp_path):
        """变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.765"]`（⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-11-09", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-11-12", {"AAA": _row(new=True), "BBB": _row(oracle="errored", new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-11-12", ev

    @pytest.mark.parametrize("bad", [_row(source="fallback"), _row(dq="unavailable")], ids=["unusual_fallback", "chain_down"])
    def test_old_code_day_with_a_fallback_row_is_too_early(self, tmp_path, bad):
        """推送到首跑之间旧代码碰上兜底 ⇒ 真混算了 ⇒ 照报（处置见 `_COHORT_HISTORY` 同版条目：须用户点头）。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-11-09", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-11-11", {"AAA": _row(), "BBB": bad})
        _write(tmp_path, "2026-11-12", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-11-11"], ev
