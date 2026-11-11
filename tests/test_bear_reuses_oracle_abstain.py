"""v0.45.770：OracleBee 弃权时 BearBee 照抄它的判定、不再自己重取期权链（用户 2026-11-11「沿用」）。

v0.45.765 起 Oracle 期权链整条取不到就弃权、**不上板**（板上一条中性条目会被 Guard 普查当成同伴票）⇒ 765~769 的 Bear 读不到
Oracle 条目、自己再取一次同一条链——Oracle 刚判过这一轮取不到，那次多半白取（每只票约 2~47 秒）。现在 Oracle 把判定登在板上
（`PheromoneBoard.record_abstention`，**不是条目**），Bear 读到就照抄标签、options_bear 留 0、不自取。

守六件事：
1. 板：弃权登记不是条目——不进排行榜 / 定点条目 / 存活视图 / 共振 / 快照 / 条目数；可清除；照条目的墙钟过期。
2. 真实 Oracle：两种降级情形登记的标签 = 它自己的 data_quality.options；取到链不登记；每次开跑先清旧结论（早退路径也清）。
3. 真实 Bear：读到登记 ⇒ 不构造 OptionsAgent、照抄标签、options_bear 0；没登记 / 认不出的值 / 替身板 ⇒ 照旧自取；有 Oracle 条目 ⇒ 读条目。
4. 端到端：真实 Oracle → 同一块板 → 真实 Bear，整只票 OptionsAgent 只取一次（Oracle 那次）。
5. 等价判据与行为一致（差分测试，同 v0.45.765）：新 Bear vs「读不到登记的同一只 Bear」（= 769 行为），判据为真 ⇔ 两版输出相同。
6. 世代登记：11-09 同日并入（冻结期内不开新世代）、印记 / 判据 / 作用域 / ML 标签、合成回退写印记。

⚠️ 全部离线、不用 skip。Oracle 走 `test_gex_oracle_bear_neutralized` 的同一套桩。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents import BearBeeContrarian
from swarm_agents.oracle_bee import OPTIONS_UNAVAILABLE_ERROR
from tests.test_gex_oracle_bear_neutralized import TICKER, _options_stub, _run_oracle

_V = "v0.45.770"
_MARK = "options_no_refetch_on_oracle_abstain"
_ORACLE = "OracleBeeEcho"


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    import llm_service
    monkeypatch.setattr(llm_service, "is_available", lambda: False)


def _counting_options_agent(result=None, raise_exc=None):
    """计数的 OptionsAgent 桩：`calls["n"]` = analyze 被调了几次。result 为 None 且不抛 ⇒ 样本链早退形状。"""
    calls = {"n": 0}

    class _Stub:
        def analyze(self, ticker, stock_price=None):
            calls["n"] += 1
            if raise_exc is not None:
                raise raise_exc
            return dict(result) if result is not None else {"data_quality": "unavailable", "put_call_ratio": None,
                                                              "iv_rank": None}
    return _Stub, calls


def _bear_options(board, monkeypatch, opt_cls):
    """真实 BearBee 的期权一项（只跑 `_assess_options_puts`，同 test_bear_options_dq_from_oracle）。"""
    import options_analyzer
    monkeypatch.setattr(options_analyzer, "OptionsAgent", opt_cls)
    signals, ds = [], {}
    ob, od = BearBeeContrarian(board)._assess_options_puts(TICKER, 100.0, signals, ds)
    return ob, od, ds.get("options"), tuple(signals)


# ════════════════════════════════════════════════════════════════════════════
# 1. 板
# ════════════════════════════════════════════════════════════════════════════

class TestBoardAbstentionRegistry:

    def test_record_get_clear(self):
        b = PheromoneBoard()
        assert b.get_abstention(TICKER, _ORACLE) is None
        b.record_abstention(TICKER, _ORACLE, "fallback")
        assert b.get_abstention(TICKER, _ORACLE) == "fallback"
        assert b.get_abstention(TICKER, "BuzzBeeWhisper") is None and b.get_abstention("OTHER", _ORACLE) is None
        b.record_abstention(TICKER, _ORACLE, None)
        assert b.get_abstention(TICKER, _ORACLE) is None
        b.record_abstention(TICKER, _ORACLE, None)          # 清一个不存在的不抛

    def test_not_an_entry(self):
        """变红的变异：record_abstention 里顺手 publish 一条条目（Guard 普查 / 共振 / 快照就会看见）。"""
        b = PheromoneBoard()
        b.record_abstention(TICKER, _ORACLE, "unavailable")
        assert b.get_entry_count() == 0
        assert b.get_top_signals(TICKER, n=24) == [] and b.get_live_signals(TICKER) == []
        assert b.get_agent_entry(TICKER, _ORACLE) is None
        assert b.snapshot() == [] and b.compact_snapshot(TICKER) == []
        assert not b.detect_resonance(TICKER).get("resonance_detected")

    def test_clear_drops_abstentions(self):
        """`clear()`（清空信息素板）连弃权登记一起清（770 二次检查补；生产不调 clear）。变红的变异：clear 漏清登记。"""
        b = PheromoneBoard()
        b.record_abstention(TICKER, _ORACLE, "fallback")
        b.clear()
        assert b.get_abstention(TICKER, _ORACLE) is None

    def test_expires_like_entries(self):
        """墙钟过期与条目同一个阈值（判「上一轮的陈货」）。变红的变异：去掉过期判断。"""
        b = PheromoneBoard()
        b.record_abstention(TICKER, _ORACLE, "fallback")
        reason, _at = b._abstentions[(TICKER, _ORACLE)]
        b._abstentions[(TICKER, _ORACLE)] = (reason, datetime.now() - timedelta(seconds=b._AGENT_ENTRY_MAX_AGE_S + 1))
        assert b.get_abstention(TICKER, _ORACLE) is None
        assert (TICKER, _ORACLE) not in b._abstentions


# ════════════════════════════════════════════════════════════════════════════
# 2. Oracle 写
# ════════════════════════════════════════════════════════════════════════════

class TestOracleRecordsItsVerdict:

    @pytest.mark.parametrize("stub,expect", [
        (lambda: _options_stub(None, extra={"data_quality": "unavailable"}), "unavailable"),
        (lambda: _options_stub(None, raise_exc=ConnectionError("桩")), "fallback"),
        (lambda: _options_stub(None), None),
    ], ids=["sample_chain", "raised", "chain_ok"])
    def test_verdict_equals_own_label(self, monkeypatch, stub, expect):
        """变红的变异：弃权分支不登记；登记的值另算一份。"""
        out, board = _run_oracle(monkeypatch, stub())
        assert board.get_abstention(TICKER, _ORACLE) == expect
        if expect is not None:
            assert out.get("error") and out["data_quality"]["options"] == expect

    def test_success_clears_a_stale_verdict(self, monkeypatch):
        """同一只票重试时整套蜂重跑：上一次弃权、这一次取到 ⇒ 旧结论必须没了（否则读条目之外的路径会读到它）。"""
        import options_analyzer as oa
        import unusual_options
        from swarm_agents.oracle_bee import OracleBeeEcho
        monkeypatch.setattr(oa, "OptionsAgent", _options_stub(None))
        monkeypatch.setattr(unusual_options, "detect_unusual_flow", lambda t, stock_price=None: {"data_source": "fallback"})
        board = PheromoneBoard()
        board.record_abstention(TICKER, _ORACLE, "fallback")
        bee = OracleBeeEcho(board)
        bee._prefetched_stock[TICKER] = {"price": 100.0, "momentum_5d": 1.0}
        bee._prefetched_context[TICKER] = ""
        assert "error" not in bee.analyze(TICKER)
        assert board.get_abstention(TICKER, _ORACLE) is None

    def test_failed_abstain_construction_leaves_no_verdict(self, monkeypatch):
        """770 二次检查复现：弃权分支先登记、再造结果 ⇒ 造结果抛异常（现价 None ⇒ `:.1f` 抛 TypeError）落进外层 except、
        变成普通出错结果（error 不带弃权前缀），板上却留着弃权判定 ⇒ Bear 照抄、不自取，而等价判据按「Oracle 没弃权」判等价。
        现在结果造好之后才登记。变红的变异：登记挪回造结果之前。"""
        cls, calls = _counting_options_agent()                       # 样本链 ⇒ 走弃权分支
        out, board = _run_oracle(monkeypatch, cls, stock={"price": None, "momentum_5d": 1.0})
        assert out.get("error") and not str(out["error"]).startswith(OPTIONS_UNAVAILABLE_ERROR), out.get("error")
        assert calls["n"] == 1, "前提：Oracle 取过链、走到了弃权分支"
        assert board.get_abstention(TICKER, _ORACLE) is None
        _bear_options(board, monkeypatch, cls)
        assert calls["n"] == 2, "Oracle 在别处出错 ⇒ Bear 照旧自取"

    def test_early_exit_also_clears(self):
        """清在一切早退之前：无效 ticker 那条早退也清。变红的变异：把清挪到 `_validate_ticker` 之后。"""
        from swarm_agents.oracle_bee import OracleBeeEcho
        board = PheromoneBoard()
        board.record_abstention("bad!", _ORACLE, "fallback")
        assert OracleBeeEcho(board).analyze("bad!").get("error")
        assert board.get_abstention("bad!", _ORACLE) is None


# ════════════════════════════════════════════════════════════════════════════
# 3. Bear 读
# ════════════════════════════════════════════════════════════════════════════

class _NoAbstentionBoard:
    """旧版 board / 测试替身：只有定点条目查询，没有弃权登记。"""
    def get_agent_entry(self, ticker, agent_id):
        return None


class _WeirdAbstentionBoard(_NoAbstentionBoard):
    def __init__(self, value):
        self.value = value

    def get_abstention(self, ticker, agent_id):
        return self.value


class TestBearReadsTheVerdict:

    @pytest.mark.parametrize("verdict", ["unavailable", "fallback"])
    def test_verdict_means_no_refetch(self, monkeypatch, verdict):
        """变红的变异：Bear 不读登记（⇒ 自取一次）；读到了也照样自取；标签不照抄。"""
        board = PheromoneBoard()
        board.record_abstention(TICKER, _ORACLE, verdict)
        cls, calls = _counting_options_agent({"data_quality": "real", "put_call_ratio": 1.6, "iv_rank": 90})
        ob, od, label, signals = _bear_options(board, monkeypatch, cls)
        assert calls["n"] == 0
        assert (ob, od, label, signals) == (0.0, None, verdict, ())

    @pytest.mark.parametrize("board", [PheromoneBoard(), _NoAbstentionBoard(), _WeirdAbstentionBoard("real"),
                                       _WeirdAbstentionBoard(1), _WeirdAbstentionBoard(None)],
                             ids=["no_record", "no_method", "unknown_label", "non_string", "none"])
    def test_no_usable_verdict_means_refetch_as_before(self, monkeypatch, board):
        """登记不在 / 认不出 ⇒ 照旧自取（与本版之前相同）。变红的变异：认任何真值为弃权。"""
        cls, calls = _counting_options_agent({"data_quality": "real", "put_call_ratio": 1.6, "iv_rank": 90})
        ob, _od, label, _s = _bear_options(board, monkeypatch, cls)
        assert calls["n"] == 1 and label == "options_api" and ob == 8.0

    def test_oracle_entry_wins_over_a_verdict(self, monkeypatch):
        """有 Oracle 条目就读条目（登记只在没条目时才看）。"""
        board = PheromoneBoard()
        board.publish(PheromoneEntry(agent_id=_ORACLE, ticker=TICKER, discovery="P/C 1.30", source="options",
                                     self_score=5.0, direction="neutral",
                                     details={"pc_ratio": 1.3, "iv_rank": 40, "options_dq": "real"}))
        board.record_abstention(TICKER, _ORACLE, "fallback")
        cls, calls = _counting_options_agent()
        ob, od, label, _s = _bear_options(board, monkeypatch, cls)
        assert calls["n"] == 0 and label == "real" and ob == 6.0 and od is not None


class TestEndToEnd:

    @pytest.mark.parametrize("raise_exc,expect", [(None, "unavailable"), (ConnectionError("桩"), "fallback")],
                             ids=["sample_chain", "raised"])
    def test_one_fetch_per_ticker(self, monkeypatch, raise_exc, expect):
        """真实 Oracle → 同一块板 → 真实 Bear：整只票只取一次链。765~769 是两次。"""
        cls, calls = _counting_options_agent(raise_exc=raise_exc)
        out, board = _run_oracle(monkeypatch, cls)
        assert out.get("error") and calls["n"] == 1
        _ob, _od, label, _s = _bear_options(board, monkeypatch, cls)
        assert calls["n"] == 1 and label == expect == out["data_quality"]["options"]


# ════════════════════════════════════════════════════════════════════════════
# 5. 等价判据 ⇔ 行为（差分）
# ════════════════════════════════════════════════════════════════════════════

#: Bear 自取会拿到什么（旧代码那次重取的结果；新代码不取）
_REFETCH = {
    "usable_bearish": ({"data_quality": "real", "put_call_ratio": 1.6, "iv_rank": 90}, None),
    "usable_quiet": ({"data_quality": "real", "put_call_ratio": 0.8, "iv_rank": 40}, None),
    "sample_chain": (None, None),
    "sample_chain_with_pc": ({"data_quality": "unavailable", "put_call_ratio": 1.3, "iv_rank": None}, None),
    "raised": (None, OSError("桩")),
}
#: Oracle 这一行的状态：(板上登记, agent_details.OracleBeeEcho 的 error)
_ORACLE_STATE = {
    "abstain_unavailable": ("unavailable", f"{OPTIONS_UNAVAILABLE_ERROR}：unavailable"),
    "abstain_fallback": ("fallback", f"{OPTIONS_UNAVAILABLE_ERROR}：fallback"),
    "errored_elsewhere": (None, "TypeError: boom"),
    "absent": (None, None),
}


def _record(oracle_err, ob, label, oracle_present=True):
    """判据读的蒸馏结果字段（落盘前 Bear 的 options_bear 取 round(·, 1)，同 `analyze`）。"""
    ad = {"BearBeeContrarian": {"details": {"options_bear": round(ob, 1)}, "error": None}}
    if oracle_present:
        ad[_ORACLE] = {"details": {}, "error": oracle_err}
    return {"swarm_results": {"agent_details": ad, "data_quality": {"BearBeeContrarian": {"options": label}}}}


class TestJudgeAgreesWithBehaviour:

    @pytest.mark.parametrize("state", sorted(_ORACLE_STATE))
    @pytest.mark.parametrize("refetch", sorted(_REFETCH))
    def test_judge_iff_old_equals_new(self, monkeypatch, state, refetch):
        """变红的变异：判据把 abstain_fallback 判成等价；去掉 options_bear == 0 的条件（sample_chain_with_pc 会假等价）；
        Bear 行为改动（例如弃权时 options_bear 不留 0）而判据没跟。"""
        import ic_rerun_readiness as rr
        verdict, oracle_err = _ORACLE_STATE[state]
        result, exc = _REFETCH[refetch]
        board = PheromoneBoard()
        if verdict is not None:
            board.record_abstention(TICKER, _ORACLE, verdict)
        cls, _calls = _counting_options_agent(result, exc)
        new = _bear_options(board, monkeypatch, cls)
        board.record_abstention(TICKER, _ORACLE, None)            # 旧代码 = 读不到登记的同一只 Bear（765~769）
        old = _bear_options(board, monkeypatch, cls)
        judged = rr._equiv_bear_no_refetch_on_oracle_abstain(
            _record(oracle_err, old[0], old[2], oracle_present=state != "absent"))
        assert judged == (old == new), (state, refetch, old, new)

    @pytest.mark.parametrize("oracle_chain", ["sample_chain", "raised"])
    @pytest.mark.parametrize("refetch", ["usable_bearish", "sample_chain", "raised"])
    def test_judge_on_real_queen_record(self, monkeypatch, oracle_chain, refetch):
        """同上，但判据吃的是**真实 Oracle → 真实 Queen 蒸馏**出来的记录（770 二次检查指出：上一条喂的是手搭的字段）——
        `agent_details.OracleBeeEcho.error` 的格式、`data_quality.BearBeeContrarian.options`、`details.options_bear` 的路径
        哪一处跟 Queen 实际写的对不上都会红。"""
        import ic_rerun_readiness as rr
        from tests.test_oracle_no_fallback_score import _distill, _peers
        o_cls, _ = _counting_options_agent(raise_exc=ConnectionError("桩") if oracle_chain == "raised" else None)
        oracle, board = _run_oracle(monkeypatch, o_cls)
        assert oracle.get("error"), "前提：Oracle 弃权"
        result, exc = _REFETCH[refetch]
        b_cls, _ = _counting_options_agent(result, exc)
        new = _bear_options(board, monkeypatch, b_cls)
        board.record_abstention(TICKER, _ORACLE, None)            # 旧代码 = 读不到登记的同一只 Bear
        old = _bear_options(board, monkeypatch, b_cls)
        bear = {"source": "BearBeeContrarian", "dimension": "contrarian", "direction": "neutral", "score": 5.0,
                "confidence": 0.6, "discovery": "Bear 合成", "details": {"options_bear": round(old[0], 1)},
                "data_quality": {"options": old[2]} if old[2] is not None else {}}
        rec = _distill([r for r in _peers() if r["source"] != "BearBeeContrarian"] + [oracle, bear])
        assert rec["agent_details"][_ORACLE]["error"] == oracle["error"]
        assert rr._equiv_bear_no_refetch_on_oracle_abstain({"swarm_results": rec}) == (old == new), (old, new)

    def test_fixture_bites_both_ways(self, monkeypatch):
        """夹具自证：弃权行里既有判据为真（两版相同）也有为假（两版不同）的组合——否则上一条可能恒真 / 恒假。"""
        import ic_rerun_readiness as rr
        seen = set()
        for state in ("abstain_unavailable", "abstain_fallback"):
            verdict, err = _ORACLE_STATE[state]
            for refetch, (result, exc) in _REFETCH.items():
                cls, _ = _counting_options_agent(result, exc)
                board = PheromoneBoard()
                old = _bear_options(board, monkeypatch, cls)
                seen.add(rr._equiv_bear_no_refetch_on_oracle_abstain(_record(err, old[0], old[2])))
        assert seen == {True, False}

    @pytest.mark.parametrize("bear", [None, {"details": {}, "error": "TypeError: boom"}], ids=["absent", "errored"])
    def test_bear_absent_or_errored_is_equivalent(self, bear):
        import ic_rerun_readiness as rr
        ad = {_ORACLE: {"error": f"{OPTIONS_UNAVAILABLE_ERROR}：fallback"}}
        if bear is not None:
            ad["BearBeeContrarian"] = bear
        assert rr._equiv_bear_no_refetch_on_oracle_abstain({"swarm_results": {"agent_details": ad}}) is True

    def test_missing_fields_are_not_proof(self):
        """Oracle 弃权为 unavailable 但缺 Bear 标签 / options_bear ⇒ 证不出 ⇒ False。"""
        import ic_rerun_readiness as rr
        ad = {_ORACLE: {"error": f"{OPTIONS_UNAVAILABLE_ERROR}：unavailable"}, "BearBeeContrarian": {"details": {}}}
        assert rr._equiv_bear_no_refetch_on_oracle_abstain({"swarm_results": {"agent_details": ad}}) is False

    def test_prefix_copy_matches_oracle(self):
        """判据里抄的弃权前缀 = Oracle 的常量（本模块不 import swarm_agents）。"""
        import ic_rerun_readiness as rr
        assert rr._ORACLE_ABSTAIN_ERROR_PREFIX == OPTIONS_UNAVAILABLE_ERROR


# ════════════════════════════════════════════════════════════════════════════
# 6. 世代登记
# ════════════════════════════════════════════════════════════════════════════

def _row(*, new=False, oracle_err=None, bear_dq="options_api", options_bear=0.0, backfill=False):
    det = {"options_bear": options_bear}
    if new:
        det[_MARK] = True
    agents = {"BearBeeContrarian": {"details": det, "error": None}, _ORACLE: {"details": {}, "error": oracle_err}}
    if backfill:
        agents["GuardBeeSentinel"] = {"details": {"vix_term_structure": {"macro_as_of_mode": "backfill"}}}
    return {"agent_details": agents, "data_quality": {"BearBeeContrarian": {"options": bear_dq}}}


def _write(root, day, rows, archive=("AAA",)):
    import json
    (root / f".swarm_results_{day}.json").write_text(json.dumps(rows), encoding="utf-8")
    for tk in archive:
        (root / f"analysis-{tk}-ml-{day}.json").write_text(json.dumps({"swarm_results": rows.get(tk, _row())}),
                                                             encoding="utf-8")


class TestBoundaryRegistered:

    def test_merged_into_current_generation(self):
        """冻结期内同日并入（作废 0 条），不登例外。变红的变异：日期写成首跑日（冻结期内开新世代、未登例外）。"""
        import ic_rerun_readiness as rr
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V] == ["2026-11-09"]
        assert _V not in rr._FREEZE_EXCEPTIONS
        assert rr.scoring_freeze_violations() == []
        assert rr._BOUNDARY_MARKERS[_V][1] is rr._marker_bear_no_refetch_on_oracle_abstain
        assert rr._BOUNDARY_EQUIVALENCE[_V][1] is rr._equiv_bear_no_refetch_on_oracle_abstain
        assert rr._BOUNDARY_EQUIVALENCE[_V][2] is rr._bear_row_has_no_refetch_marker

    def test_scope_moves_nothing_further(self):
        """作用域点名 bear.options_bear；765 已把它挪到 11-09 ⇒ 本条登上后任何信号的起点都不变。
        变红的变异：作用域漏声明（⇒ 影响全部信号，Scout / Buzz 等被挪到 11-09）。"""
        import ic_rerun_readiness as rr
        import signal_archive as sa
        assert sa.COHORT_SIGNAL_SCOPE[_V] == ("bear.options_bear",)
        sigs = set(sa.SIGNAL_EXTRACTORS)
        idx = [e[1] for e in rr._COHORT_HISTORY].index(_V)
        before = sa.generation_boundaries(sigs, history=rr._COHORT_HISTORY[:idx])
        after = sa.generation_boundaries(sigs, history=rr._COHORT_HISTORY[:idx + 1])
        assert {s for s in after if (before.get(s) or {}).get("date") != after[s]["date"]} == set()

    def test_does_not_truncate_dim_ic(self):
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
        assert any(d == "2026-11-09" and _V in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)

    def test_bear_success_path_writes_the_marker_unconditionally(self):
        """静态核（同 v0.45.639）。变红的变异：删掉那一行 / 挪进 if / 写成 False。"""
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

    def test_marker_only_on_live_new_rows(self):
        import ic_rerun_readiness as rr
        assert rr._marker_bear_no_refetch_on_oracle_abstain({"swarm_results": _row(new=True)}) is True
        for d in ({}, {"swarm_results": _row()}, {"swarm_results": _row(new=True, backfill=True)}):
            assert rr._marker_bear_no_refetch_on_oracle_abstain(d) is False


class TestBoundaryEvidence:

    def test_before_first_run_no_evidence_and_nothing_unmarked(self, tmp_path):
        """生产现状：11-09 / 11-10 旧代码、Oracle 不弃权 ⇒ 全等价。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-11-09", {"AAA": _row(), "BBB": _row(oracle_err="TypeError: boom")})
        _write(tmp_path, "2026-11-10", {"AAA": _row(), "BBB": _row()})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_first_run_matches(self, tmp_path):
        """变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.770"]`（⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-11-09", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-11-12", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-11-12", ev

    def test_old_code_abstain_row_that_refetched_is_too_early(self, tmp_path):
        """11-11 若跑的是 768 代码、Oracle 弃权而 Bear 自取取到了 ⇒ 真混算 ⇒ 照报（处置须用户点头）。
        同一天 Oracle 弃权为 unavailable、Bear 自取也没取到 ⇒ 等价、不报。"""
        import ic_rerun_readiness as rr
        err_u, err_f = (f"{OPTIONS_UNAVAILABLE_ERROR}：unavailable", f"{OPTIONS_UNAVAILABLE_ERROR}：fallback")
        _write(tmp_path, "2026-11-09", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-11-11", {"AAA": _row(), "BBB": _row(oracle_err=err_u, bear_dq="unavailable")})
        _write(tmp_path, "2026-11-12", {"AAA": _row(new=True), "BBB": _row(new=True)})
        assert rr.cohort_boundary_evidence(tmp_path, version=_V)["verdict"] == "matches"
        _write(tmp_path, "2026-11-11", {"AAA": _row(), "BBB": _row(oracle_err=err_f, bear_dq="options_api", options_bear=8.0)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-11-11"], ev
