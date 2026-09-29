"""v0.45.383：日线完整性校验的世代边界（登记在 09-28，作废 0 条）——印记、等价证据、证据文件本身。

守四件事：
1. **印记**：`details.hv_gap_checked` 字面量 True，Oracle 每条返回路径都写（含日报合成回退），经 Queen 进归档后判别器认得出。
2. **等价判据**：读冻结的外部证据，按（日期，标的）查；证据里没有 / 状态不是「无缺口」/ 文件读不出 ⇒ 不等价（举证责任在放宽一侧）。
3. **证据文件**：随仓库发布的 `experiments/hv_gap_equivalence_20260928.json` 内部自洽（状态与数据相符、按天推断的前提成立、30 只齐）。
4. **审计脚本**：`classify` / `audit_day` 的分类规则（verified / ambiguous / mismatch / 按天推断）——这套规则是「不作废 30 条」的论据，
   规则错了证据就是废的。

⚠️ 全部离线、不用 skip：合成数据、yfinance 由假模块顶替。
"""
from __future__ import annotations

import ast
import copy
import json
import sys
import types

import pytest

import bars_integrity as bi
import ic_rerun_readiness as rr
from pheromone_board import PheromoneBoard
from tests.test_gex_oracle_bear_neutralized import REPO_ROOT, TICKER, _options_stub, _run_oracle



def _load_audit():
    """`experiments/` 不是包：按路径加载（同 test_dim_ic_protocol 的做法）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hv_gap_equivalence_audit",
                                                  REPO_ROOT / "experiments" / "hv_gap_equivalence_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load_audit()
MARK = "hv_gap_checked"
_V = "v0.45.383"
_EVIDENCE = REPO_ROOT / "experiments" / "hv_gap_equivalence_20260928.json"


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
        assert chains, "合成回退里没有写 hv_gap_checked = True"
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
        assert rr._marker_oracle_hv_gap_checked({"swarm_results": out}) is True

    @pytest.mark.parametrize("d", [
        {}, {"swarm_results": {"agent_details": {"OracleBeeEcho": None}}},
        {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {MARK: 1}}}}},
        {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {"options_dq_from_agent": True}}}}}])
    def test_marker_only_literal_true(self, d):
        """只认 `is True`；且**不能**被别的印记（369 的 options_dq_from_agent）顶替。"""
        assert rr._marker_oracle_hv_gap_checked(d) is False


# ════════════════════════════════════════════════════════════════════════════
# 2. 等价边界（证据驱动）
# ════════════════════════════════════════════════════════════════════════════

def _b():
    return next(d for d, v, _r in rr._COHORT_HISTORY if v == _V)


def _shift(date, days):
    import datetime as dt
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


def _rec(*, new: bool, backfill: bool = False):
    det = {"iv_rank": 30.0, "iv_rank_source": "hv_proxy"}
    if new:
        det[MARK] = True
    agents = {"OracleBeeEcho": {"score": 5.0, "direction": "neutral", "details": det}}
    if backfill:
        agents["GuardBeeSentinel"] = {"details": {"vix_term_structure": {"macro_as_of_mode": "backfill"}}}
    return {"agent_details": agents}


def _write_archive(root, date, ticker, rec):
    (root / f"analysis-{ticker}-ml-{date}.json").write_text(json.dumps({"swarm_results": rec}), encoding="utf-8")


def _write_swarm(root, date, recs):
    (root / f".swarm_results_{date}.json").write_text(json.dumps(recs), encoding="utf-8")


def _evidence_file(tmp_path, monkeypatch, per_day):
    """把 `_HV_GAP_EVIDENCE_PATH` 指到临时证据。per_day = {日期: {标的: 状态}}。"""
    doc = {"days": {d: {"tickers": {t: {"status": s} for t, s in tk.items()}} for d, tk in per_day.items()}}
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(rr, "_HV_GAP_EVIDENCE_PATH", p)
    return p


class TestEquivalenceBoundary:

    def test_registered_on_the_09_28_boundary(self):
        assert _b() == "2026-09-28"
        assert _V in rr._BOUNDARY_MARKERS and _V in rr._BOUNDARY_EQUIVALENCE

    def test_before_deploy_covered_day_is_no_evidence_yet(self, tmp_path, monkeypatch):
        """新代码首跑前的真实形状：09-28 旧代码、证据覆盖全部标的 ⇒ 还没证据，**不报警**。"""
        b = _b()
        _evidence_file(tmp_path, monkeypatch, {b: {"AAA": "verified", "BBB": "day_level_inference"}})
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False), "BBB": _rec(new=False)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_after_covered_day_matches(self, tmp_path, monkeypatch):
        """首跑后：印记首见 09-29，其前只有被证据覆盖的旧记录 ⇒ matches。
        变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.383"]`（⇒ boundary_too_early）。"""
        b, nxt = _b(), _shift(_b(), 1)
        _evidence_file(tmp_path, monkeypatch, {b: {"AAA": "verified", "BBB": "not_applicable"}})
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False), "BBB": _rec(new=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        _write_swarm(tmp_path, nxt, {"AAA": _rec(new=True), "BBB": _rec(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == nxt, ev
        assert ev["equivalent_before_marker"] == [b]

    @pytest.mark.parametrize("why", ["ticker_absent", "date_absent", "status_mismatch", "status_none", "file_missing",
                                     "file_garbage"])
    def test_unproven_row_is_too_early(self, tmp_path, monkeypatch, why):
        """证据里没有该（日期，标的）/ 状态不是「无缺口」/ 证据读不出 ⇒ 不等价 ⇒ 照报。
        变红的变异：判据恒真；状态集合混进 mismatch；读不出证据时放行。"""
        b, nxt = _b(), _shift(_b(), 1)
        per = {"ticker_absent": {b: {"OTHER": "verified"}},
               "date_absent": {_shift(b, -1): {"AAA": "verified"}},
               "status_mismatch": {b: {"AAA": "mismatch"}},
               "status_none": {b: {"AAA": None}},
               "file_missing": None, "file_garbage": None}[why]
        if per is not None:
            _evidence_file(tmp_path, monkeypatch, per)
        elif why == "file_missing":
            monkeypatch.setattr(rr, "_HV_GAP_EVIDENCE_PATH", tmp_path / "nope.json")
        else:
            g = tmp_path / "garbage.json"
            g.write_text("{not json", encoding="utf-8")
            monkeypatch.setattr(rr, "_HV_GAP_EVIDENCE_PATH", g)
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == [b], ev
        assert rr.BOUNDARY_ALARM_VERDICTS >= {ev["verdict"]}

    def test_one_unproven_ticker_spoils_the_day(self, tmp_path, monkeypatch):
        """同一天 29 只有证据、1 只没有 ⇒ 这天算不等价（逐只标的判，不按多数）。"""
        b, nxt = _b(), _shift(_b(), 1)
        _evidence_file(tmp_path, monkeypatch, {b: {"AAA": "verified"}})
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False), "ZZZ": _rec(new=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early", ev

    def test_marked_rows_are_new_code_and_skipped(self, tmp_path, monkeypatch):
        """新代码自己的记录（带印记）不是「旧口径」，证据里没有也不算错。变红的变异：删掉 is_new 跳过。"""
        b = _b()
        _evidence_file(tmp_path, monkeypatch, {})
        _write_archive(tmp_path, b, "AAA", _rec(new=True))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=True)})
        assert rr.cohort_boundary_evidence(tmp_path, version=_V)["verdict"] == "matches"

    def test_backfill_row_does_not_pull_first_seen_early(self, tmp_path, monkeypatch):
        """部署后补跑一份**早于边界**的日期，带印记、macro_as_of_mode=backfill ⇒ 不算日期证据（同 v0.45.373 对 369 的处理）。"""
        b, nxt = _b(), _shift(_b(), 1)
        _evidence_file(tmp_path, monkeypatch, {b: {"AAA": "verified"}})
        _write_archive(tmp_path, _shift(b, -3), "AAA", _rec(new=True, backfill=True))
        _write_archive(tmp_path, b, "AAA", _rec(new=False))
        _write_swarm(tmp_path, b, {"AAA": _rec(new=False)})
        _write_archive(tmp_path, nxt, "AAA", _rec(new=True))
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == nxt, ev

    def test_predicate_needs_date_and_ticker(self, tmp_path, monkeypatch):
        """判据只读记录看不出缺口：缺 `_date` / `_ticker` ⇒ False。变红的变异：`_equivalence_scan` 不再把日期标的传进来。"""
        _evidence_file(tmp_path, monkeypatch, {"2026-09-28": {"AAA": "verified"}})
        assert rr._equiv_hv_gap_free({"_date": "2026-09-28", "_ticker": "AAA"}) is True
        assert rr._equiv_hv_gap_free({"swarm_results": {}}) is False
        assert rr._equiv_hv_gap_free({"_date": "2026-09-28"}) is False
        assert rr._equiv_hv_gap_free({"_ticker": "AAA"}) is False

    @pytest.mark.parametrize("status,want", [("verified", True), ("day_level_inference", True), ("not_applicable", True),
                                             ("mismatch", False), ("ambiguous", False), (None, False)])
    def test_status_set(self, tmp_path, monkeypatch, status, want):
        _evidence_file(tmp_path, monkeypatch, {"2026-09-28": {"AAA": status}})
        assert rr._equiv_hv_gap_free({"_date": "2026-09-28", "_ticker": "AAA"}) is want

    def test_scan_passes_date_and_ticker_to_the_predicate(self, tmp_path):
        """写端 ↔ 读端：`_equivalence_scan` 交给判据的 dict 真的带 `_date` / `_ticker`。"""
        _write_swarm(tmp_path, "2026-09-28", {"AAA": _rec(new=False)})
        seen = []
        rr._equivalence_scan(tmp_path, "2026-09-28", None, lambda d: False,
                             lambda d: seen.append((d.get("_date"), d.get("_ticker"))) or True)
        assert seen == [("2026-09-28", "AAA")]

    def test_other_boundaries_still_use_their_own_predicates(self):
        """本版给扫描新增的两个键不改变其它边界的判据（它们只读 `swarm_results`）。"""
        d = {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {"data_quality": "real"}}}},
             "_date": "2026-09-28", "_ticker": "AAA"}
        assert rr._equiv_oracle_chain_available(d) is True


# ════════════════════════════════════════════════════════════════════════════
# 3. 随仓库发布的证据文件
# ════════════════════════════════════════════════════════════════════════════

class TestShippedEvidenceFile:
    DOC = json.loads(_EVIDENCE.read_text(encoding="utf-8"))
    DAY = DOC["days"]["2026-09-28"]

    def test_covers_the_30_tickers_of_that_day(self):
        assert self.DAY["n_tickers"] == len(self.DAY["tickers"]) == 30

    def test_summary_matches_row_statuses(self):
        from collections import Counter
        assert dict(Counter(v["status"] for v in self.DAY["tickers"].values())) == self.DAY["summary"]

    def test_no_mismatch_and_only_accepted_statuses(self):
        assert {v["status"] for v in self.DAY["tickers"].values()} <= rr._HV_GAP_EQUIV_STATUSES

    def test_loader_reads_the_shipped_file_for_every_ticker(self):
        ev = rr._load_hv_gap_evidence()["2026-09-28"]
        assert set(ev) == set(self.DAY["tickers"]) and all(s in rr._HV_GAP_EQUIV_STATUSES for s in ev.values())

    def test_verified_rows_reproduce_stored_and_have_no_alternative_explanation(self):
        for t, v in self.DAY["tickers"].items():
            if v["status"] != "verified":
                continue
            assert abs(v["stored"][0] - v["complete"][0]) <= audit.TOL, t
            assert abs(v["stored"][1] - v["complete"][1]) <= audit.TOL, t
            assert v["ambiguous_drops"] == [], t

    def test_day_level_inference_premise_holds_and_is_labelled(self):
        """按天推断是**概率推断**，前提必须成立且写明依据：同日 0 只 mismatch、唯一证明的 ≥ MIN_DAY_VERIFIED。"""
        n_ver = self.DAY["summary"].get("verified", 0)
        for t, v in self.DAY["tickers"].items():
            if v["status"] == "day_level_inference":
                assert v["ambiguous_drops"], f"{t} 没有歧义却被标成按天推断"
                assert v["basis"], f"{t} 按天推断必须写明依据"
                assert n_ver >= audit.MIN_DAY_VERIFIED and self.DAY["summary"].get("mismatch", 0) == 0

    def test_provenance_recorded(self):
        assert self.DOC["version"] == _V and self.DOC["generator"] == "experiments/hv_gap_equivalence_audit.py"
        assert self.DOC["tolerance"] == audit.TOL and self.DOC["critical_bars"] == audit.CRITICAL_BARS == bi.DEFAULT_CRITICAL_BARS


# ════════════════════════════════════════════════════════════════════════════
# 4. 审计脚本的分类规则（这套规则是「作废 0 条」的论据）
# ════════════════════════════════════════════════════════════════════════════

def _closes(seed, n=300, burst=True, ramp=False):
    """确定性收盘序列。`burst`：早段高波动 ⇒ HV 最大值落在很早的地方，当前 HV 不是最大；
    `ramp`：末段收益**交替正负、幅度逐日放大** ⇒ 20 日滚动 HV 逐日单调上升，当前 HV 就是全年最大
    （rank=100、percentile=(n-1)/n，删最后 21 根里任一根也不变——VKTX 的形状）。"""
    import random
    r = random.Random(seed)
    px, out = 100.0, []
    for i in range(n):
        if ramp and i >= n - 60:
            vol = 0.01 + (i - (n - 60)) * 0.002
            px *= 1 + (vol if i % 2 else -vol)
        else:
            vol = 0.06 if (burst and 40 <= i < 70) else 0.01
            px *= 1 + r.uniform(-vol, vol)
        out.append(px)
    return out


def _dates(n=300, end="2026-09-28"):
    import datetime as dt
    ds = [d for d in bi.trading_days(dt.date.fromisoformat(end) - dt.timedelta(days=int(n * 1.6)),
                                     dt.date.fromisoformat(end))]
    return [d.isoformat() for d in ds[-n:]]


class TestAuditClassify:

    def test_complete_series_is_verified_when_nothing_else_explains_it(self):
        c, ds = _closes(1), _dates()
        stored = audit.rank_pct(c)
        v = audit.classify(stored, c, ds)
        assert v["status"] == "verified" and v["ambiguous_drops"] == [], v

    def test_a_series_missing_one_bar_is_a_mismatch_against_the_complete_one(self):
        """09-23 的形状：存下来的是「缺一根」的输出，完整序列复现不了它 ⇒ mismatch（不能证明等价）。"""
        c, ds = _closes(2), _dates()
        gapped = c[:-3] + c[-2:]
        stored = audit.rank_pct(gapped)
        assert stored != audit.rank_pct(c), "夹具自检：缺一根确实改了输出"
        assert audit.classify(stored, c, ds)["status"] == "mismatch"

    def test_saturated_output_is_ambiguous_not_verified(self):
        """VKTX 的形状：当前 HV 就是全年最大 ⇒ 删哪根输出都一样 ⇒ 只能标 ambiguous，**不能**冒充 verified。
        变红的变异：不做「删一根」的歧义扫描。"""
        c, ds = _closes(3, ramp=True), _dates()
        v = audit.classify(audit.rank_pct(c), c, ds)
        assert v["stored"][0] == 100.0
        assert v["status"] == "ambiguous" and len(v["ambiguous_drops"]) >= 1, v

    def test_tolerance_boundary(self):
        c, ds = _closes(4), _dates()
        r, p = audit.rank_pct(c)
        assert audit.classify((r + 0.01, p), c, ds)["status"] in ("verified", "ambiguous")
        assert audit.classify((r + 0.05, p), c, ds)["status"] == "mismatch"


class _FakeYF(types.ModuleType):
    """顶替 yfinance：每个标的一条固定序列，DatetimeIndex 与真实日线同形。"""

    def __init__(self, series):
        super().__init__("yfinance")
        self._series = series

    def Ticker(self, t):                                     # noqa: N802
        import pandas as pd
        c, ds = self._series[t]

        class _T:
            def history(_self, period="1y", **_k):
                return pd.DataFrame({"Close": c}, index=pd.to_datetime(ds))
        return _T()


def _make_home(tmp_path, monkeypatch, specs):
    """specs = {标的: (closes, override_stored 或 None, iv_rank_source)}；写 .swarm_results 并装假 yfinance。"""
    ds = _dates()
    series, recs = {}, {}
    for t, (c, override, src) in specs.items():
        series[t] = (c, ds)
        rp = override or audit.rank_pct(c)
        recs[t] = {"agent_details": {"OracleBeeEcho": {"details": {
            "iv_rank": rp[0], "iv_percentile": rp[1], "iv_rank_source": src}}}}
    (tmp_path / ".swarm_results_2026-09-28.json").write_text(json.dumps(recs), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "yfinance", _FakeYF(series))
    return tmp_path


class TestAuditDay:

    def _specs(self, n_ver, *, extra=None):
        """挑 n_ver 个**确实**能被唯一证明的种子当「干净标的」（夹具编排；分类规则本身由 TestAuditClassify 单独钉）。"""
        ds, picked, seed = _dates(), [], 100
        while len(picked) < n_ver:
            c = _closes(seed)
            if audit.classify(audit.rank_pct(c), c, ds)["status"] == "verified":
                picked.append(c)
            seed += 1
        specs = {f"V{i:02d}": (c, None, "hv_proxy") for i, c in enumerate(picked)}
        specs.update(extra or {})
        return specs

    def test_ambiguous_row_becomes_day_level_inference_when_the_day_is_clean(self, tmp_path, monkeypatch):
        specs = self._specs(audit.MIN_DAY_VERIFIED, extra={"SAT": (_closes(9, ramp=True), None, "hv_proxy")})
        day = audit.audit_day(_make_home(tmp_path, monkeypatch, specs), "2026-09-28", sleep=0)
        assert day["summary"] == {"verified": audit.MIN_DAY_VERIFIED, "day_level_inference": 1}, day["summary"]
        assert "basis" in day["tickers"]["SAT"]

    def test_one_mismatch_on_the_day_forbids_day_level_inference(self, tmp_path, monkeypatch):
        """同日只要有一只复现不了，「缺口按天发生、其余都干净」的前提就塌了 ⇒ 歧义行也降为 mismatch。
        变红的变异：把 `n_mis == 0` 条件删掉。"""
        c = _closes(7)
        gapped = audit.rank_pct(c[:-3] + c[-2:])
        specs = self._specs(audit.MIN_DAY_VERIFIED, extra={"SAT": (_closes(9, ramp=True), None, "hv_proxy"),
                                                           "BAD": (c, gapped, "hv_proxy")})
        day = audit.audit_day(_make_home(tmp_path, monkeypatch, specs), "2026-09-28", sleep=0)
        assert day["tickers"]["BAD"]["status"] == "mismatch"
        assert day["tickers"]["SAT"]["status"] == "mismatch", "前提塌了，歧义行不得仍算按天推断"

    def test_too_few_verified_rows_forbids_day_level_inference(self, tmp_path, monkeypatch):
        """按天推断要**足够多**的干净标的做依据：太少 ⇒ 不推断。变红的变异：把 MIN_DAY_VERIFIED 门槛去掉。"""
        specs = self._specs(audit.MIN_DAY_VERIFIED - 1, extra={"SAT": (_closes(9, ramp=True), None, "hv_proxy")})
        day = audit.audit_day(_make_home(tmp_path, monkeypatch, specs), "2026-09-28", sleep=0)
        assert day["tickers"]["SAT"]["status"] == "mismatch"

    def test_non_hv_proxy_row_is_not_applicable(self, tmp_path, monkeypatch):
        """真实 IV 历史口径不经过 fetch_historical_hv ⇒ 新代码是空操作，不需要日线证据。"""
        specs = self._specs(2, extra={"REAL": (_closes(11), (50.0, 50.0), "real_iv_40d")})
        day = audit.audit_day(_make_home(tmp_path, monkeypatch, specs), "2026-09-28", sleep=0)
        assert day["tickers"]["REAL"]["status"] == "not_applicable"

    def test_null_archived_rank_is_not_silently_ok(self, tmp_path, monkeypatch):
        """归档里 iv_rank 为空（含被旧代码置空）⇒ 证不出 ⇒ mismatch，不能被略过。"""
        specs = self._specs(2, extra={"NUL": (_closes(12), (None, None), "hv_proxy")})
        home = _make_home(tmp_path, monkeypatch, specs)
        assert audit.audit_day(home, "2026-09-28", sleep=0)["tickers"]["NUL"]["status"] == "mismatch"
