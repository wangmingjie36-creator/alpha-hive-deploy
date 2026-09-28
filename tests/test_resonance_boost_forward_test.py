"""共振加成前瞻检验（v0.45.242）的守卫。

`experiments/resonance_boost_forward_test.py` 是一份**预注册**：它的价值全在「看结果之前写死」。
所以这里守的不是「算得对不对」这一件事，而是四件：

1. 预注册常量没被悄悄改掉（改了就得同时改这里，diff 里看得见）。
2. 检视按**时间顺序最先攒到的**前 N 周算，而且未到检视点时**返回值里没有效应量**（盲化在数据结构上）。
3. 自证是前提：重放复现不了生产就「无法判定」，而不是算出一个没意义的数。
   —— 并且重放真的等价于现行 `QueenDistiller.distill()`（含「生产删掉加成」那个 B3），
   评分链一改这里先红。
4. 承载物 `ic_rerun_readiness` 把进度放进**同一行**摘要，检验出错渲染成可见的一行，不改退出码。
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

import ic_rerun_readiness as rr

_ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "resonance_boost_forward_test", _ROOT / "experiments" / "resonance_boost_forward_test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fwd = _load_module()

_EFFECT_KEYS = {"stats", "mean", "t", "p", "delta", "ic_b0", "ic_b3", "pooled_delta_by_rows",
                "mean_ic_b0", "mean_ic_b3", "weeks_used", "gate_changes"}


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_keys(v)


# ── 1. 预注册常量 ─────────────────────────────────────────────────────────────
class TestPreregistrationFrozen:
    def test_constants_are_the_registered_ones(self):
        assert fwd.FORWARD_START == "2026-09-15"
        assert fwd.MIN_CS == 5
        assert fwd.LOOKS == ((10, 0.01, "中期"), (20, 0.045, "终期"))
        assert fwd.SELFPROOF_MIN_RATE == 0.95
        assert fwd.SCORE_TOL == 0.011


# ── 2. 统计与判定 ─────────────────────────────────────────────────────────────
class TestStatistics:
    def test_spearman_matches_scipy_with_ties(self):
        from scipy import stats
        x = [1, 2, 2, 3, 5, 5, 5, 8]
        y = [0.3, -1.0, 2.0, 2.0, 0.1, 4.0, -2.0, 1.0]
        assert fwd.spearman(x, y) == pytest.approx(stats.spearmanr(x, y).statistic, abs=1e-12)

    def test_spearman_constant_side_is_none(self):
        assert fwd.spearman([1, 1, 1], [1, 2, 3]) is None
        assert fwd.spearman([1, 2, 3], [4, 4, 4]) is None

    def test_one_sided_t_matches_scipy(self):
        from scipy import stats
        v = [0.05, -0.02, 0.07, 0.01, 0.03, -0.01, 0.04]
        got = fwd.one_sided_t(v)
        ref = stats.ttest_1samp(v, 0.0, alternative="greater")
        assert got["t"] == pytest.approx(ref.statistic)
        assert got["p"] == pytest.approx(ref.pvalue)


class TestWeeklyDeltas:
    def _pairs(self, date, n, b3_better=True):
        tks = [f"T{i}" for i in range(n)]
        pairs = {(date, t): (float(n - i), float(i)) if b3_better else (float(i), float(n - i))
                 for i, t in enumerate(tks)}
        excess = {(date, t): float(i) for i, t in enumerate(tks)}
        return pairs, excess

    def test_delta_is_ic_b3_minus_ic_b0(self):
        pairs, excess = self._pairs("2026-09-15", 6)
        weeks = fwd.weekly_deltas(pairs, excess)
        assert len(weeks) == 1
        assert weeks[0]["delta"] == pytest.approx(2.0)          # IC(B3)=+1, IC(B0)=−1

    def test_days_below_min_cs_do_not_count(self):
        pairs, excess = self._pairs("2026-09-15", fwd.MIN_CS - 1)
        assert fwd.weekly_deltas(pairs, excess) == []
        pairs, excess = self._pairs("2026-09-15", fwd.MIN_CS)
        assert len(fwd.weekly_deltas(pairs, excess)) == 1

    def test_unmatured_rows_are_not_in_ic(self):
        pairs, excess = self._pairs("2026-09-15", 6)
        for k in list(excess)[:2]:                                  # 6 → 4 只到期，低于 MIN_CS
            del excess[k]
        assert fwd.weekly_deltas(pairs, excess) == []

    def test_same_iso_week_is_averaged_and_weeks_are_chronological(self):
        p1, e1 = self._pairs("2026-09-22", 6, b3_better=True)       # W39 +2
        p2, e2 = self._pairs("2026-09-24", 6, b3_better=False)      # W39 −2
        p3, e3 = self._pairs("2026-09-15", 6, b3_better=True)       # W38 +2
        weeks = fwd.weekly_deltas({**p1, **p2, **p3}, {**e1, **e2, **e3})
        assert [w["week"] for w in weeks] == ["2026-W38", "2026-W39"]
        assert weeks[0]["delta"] == pytest.approx(2.0)
        assert weeks[1]["delta"] == pytest.approx(0.0)


def _weeks(deltas):
    return [{"week": f"W{i:02d}", "delta": d, "days": []} for i, d in enumerate(deltas)]


class TestDecide:
    def test_interim_uses_the_first_ten_weeks_only(self):
        strong = [0.10, 0.12, 0.09, 0.11, 0.10, 0.13, 0.08, 0.12, 0.10, 0.11]
        res = fwd.decide(_weeks(strong + [-0.5] * 5))              # 后 5 周很差也不影响中期
        assert res["status"] == "confirmed" and res["look"] == "中期"
        assert res["weeks_used"] == [f"W{i:02d}" for i in range(10)]

    def test_interim_not_crossed_reports_no_effect(self):
        weak = [0.10, -0.08, 0.09, -0.07, 0.10, -0.05, 0.08, -0.06, 0.07, 0.02]
        assert fwd.one_sided_t(weak)["p"] >= 0.01                   # 前提：中期确实没过界
        res = fwd.decide(_weeks(weak + [0.05] * 3))
        assert res["status"] == "not_ready"
        assert res["looks_passed_without_verdict"] == ["中期"] and res["next_look_at"] == 20
        assert not (_EFFECT_KEYS & set(_all_keys(res)))

    def test_final_look_can_confirm(self):
        weak = [0.10, -0.08, 0.09, -0.07, 0.10, -0.05, 0.08, -0.06, 0.07, 0.02]
        later = [0.05, 0.06, 0.04, 0.05, 0.07, 0.03, 0.05, 0.06, 0.04, 0.05]
        assert fwd.one_sided_t(weak)["p"] >= 0.01
        assert fwd.one_sided_t(weak + later)["p"] < 0.045           # 前提：非空转
        res = fwd.decide(_weeks(weak + later + [-1.0] * 4))
        assert res["status"] == "confirmed" and res["look"] == "终期"

    def test_final_look_without_effect_is_not_confirmed(self):
        null = [0.05, -0.05] * 10
        res = fwd.decide(_weeks(null))
        assert res["status"] == "not_confirmed" and res["look"] == "终期"

    def test_below_first_look_is_progress_only(self):
        res = fwd.decide(_weeks([0.3] * 9))
        assert res == {"status": "not_ready", "weeks": 9, "next_look_at": 10,
                       "looks_passed_without_verdict": []}


# ── 3. 自证与盲化（evaluate 层，注入假重放）─────────────────────────────────
def _rows_and_excess(n_weeks, per_day=6, b3_better=True, start="2026-09-15"):
    import datetime as dt
    rows, excess = [], {}
    d0 = dt.date.fromisoformat(start)
    for w in range(n_weeks):
        date = (d0 + dt.timedelta(weeks=w)).isoformat()
        for i in range(per_day):
            t = f"T{i}"
            b0 = float(per_day - i)
            b3 = float(i) if b3_better else b0
            rows.append({"date": date, "ticker": t,
                         "sr": {"ticker": t, "final_score": b0, "direction": "bullish", "_b3": b3}})
            excess[(date, t)] = float(i) + (0.3 if w % 2 else 0.0) * (i % 2)
    return rows, excess


def _fake_replay(sr, drop_boost, base=None):
    return {"final": sr["_b3"] if drop_boost else sr["final_score"], "direction": "bullish"}


class TestSelfProofAndBlinding:
    def test_not_ready_result_carries_no_effect_even_in_json(self):
        rows, excess = _rows_and_excess(3)
        res = fwd.evaluate(rows, excess, replay_fn=_fake_replay)
        assert res["status"] == "not_ready" and res["weeks"] == 3
        assert not (_EFFECT_KEYS & set(_all_keys(json.loads(json.dumps(res, default=str)))))

    def test_same_fixture_does_produce_effect_once_a_look_is_reached(self):
        """非空转对照：上一条不是因为「根本算不出数」才过的。"""
        rows, excess = _rows_and_excess(10)
        res = fwd.evaluate(rows, excess, replay_fn=_fake_replay)
        assert res["status"] == "confirmed"
        assert {"stats", "pooled_delta_by_rows", "mean_ic_b0", "mean_ic_b3"} <= set(res)

    def test_low_reproduction_rate_is_cannot_judge(self):
        rows, excess = _rows_and_excess(10)
        for r in rows[:7]:                                          # 7/60 ≈ 11.7% 不一致
            r["sr"]["final_score"] += 1.0
            r["sr"]["_replay_off"] = True

        def replay_off(sr, drop_boost, base=None):
            out = _fake_replay(sr, drop_boost)
            if sr.get("_replay_off") and not drop_boost:
                out["final"] -= 1.0
            return out
        res = fwd.evaluate(rows, excess, replay_fn=replay_off)
        assert res["status"] == "cannot_judge"
        assert res["selfproof"]["mismatch"] == 7

    def test_few_mismatches_are_excluded_and_counted(self):
        rows, excess = _rows_and_excess(10, per_day=10)            # 100 行
        for r in rows[:4]:
            r["sr"]["_replay_off"] = True

        def replay_off(sr, drop_boost, base=None):
            out = _fake_replay(sr, drop_boost)
            if sr.get("_replay_off") and not drop_boost:
                out["final"] += 0.5
            return out
        res = fwd.evaluate(rows, excess, replay_fn=replay_off)
        assert res["status"] != "cannot_judge"
        assert res["selfproof"]["mismatch"] == 4 and res["n_matured_pairs"] == 96

    def test_direction_mismatch_also_fails_selfproof(self):
        rows, excess = _rows_and_excess(2, per_day=10)

        def wrong_dir(sr, drop_boost, base=None):
            return {**_fake_replay(sr, drop_boost), "direction": "bearish"}
        assert fwd.evaluate(rows, excess, replay_fn=wrong_dir)["status"] == "cannot_judge"

    def test_replay_exceptions_are_counted_not_swallowed(self):
        rows, excess = _rows_and_excess(10, per_day=10)
        bad = {id(r["sr"]) for r in rows[:6]}

        def boom(sr, drop_boost, base=None):
            if id(sr) in bad:
                raise KeyError("agent_details")
            return _fake_replay(sr, drop_boost)
        res = fwd.evaluate(rows, excess, replay_fn=boom)
        assert res["status"] == "cannot_judge"                      # 94% < 95%
        assert res["selfproof"]["exception"] == 6
        assert any("KeyError" in e for e in res["fail_examples"])


# ── 3b. 重放 ≡ 现行 QueenDistiller.distill()（真代码，离线）───────────────────
_DIMS = {"ScoutBeeNova": "signal", "OracleBeeEcho": "odds", "BuzzBeeWhisper": "sentiment",
         "ChronosBeeHorizon": "catalyst", "RivalBeeVanguard": "ml_auxiliary",
         "CodeExecutorAgent": "technical", "GuardBeeSentinel": "risk_adj", "BearBeeContrarian": "contrarian"}


def _agent_results(directions, base_scores=None):
    score_of = {"bullish": 6.8, "neutral": 5.2, "bearish": 3.9}
    out = []
    for agent, dim in _DIMS.items():
        d = directions.get(agent, "neutral")
        out.append({"source": agent, "dimension": dim, "direction": d,
                    "score": (base_scores or {}).get(agent, score_of[d]), "confidence": 0.7,
                    "discovery": f"{agent} 合成", "data_quality": {"x": "real"}, "details": {}})
    return out


class _NoBoostBoard:
    """「生产里把共振加成删掉」：共振照常检测，只是加成为 0。"""
    def __init__(self, board):
        self._b = board

    def detect_resonance(self, ticker):
        z = dict(self._b.detect_resonance(ticker))
        z["confidence_boost"] = 0
        return z

    def __getattr__(self, name):
        return getattr(self._b, name)


def _distill(ticker, results, no_boost=False, gex=None):
    from pheromone_board import PheromoneBoard, PheromoneEntry
    from swarm_agents.queen_distiller import QueenDistiller
    board = PheromoneBoard()
    for r in results:
        board.publish(PheromoneEntry(agent_id=r["source"], ticker=ticker, discovery=r["discovery"], source="test",
                                     self_score=float(r["score"]), direction=r["direction"], details={}))
    q = QueenDistiller(board, enable_llm=False, ml_model=None)
    if no_boost:
        q.board = _NoBoostBoard(board)
    return q.distill(ticker, copy.deepcopy(results), dealer_gex=copy.deepcopy(gex or {"regime": "unknown"}))


_CASES = {
    "看多共振": {a: "bullish" for a in ("ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon")},
    "看空共振": {a: "bearish" for a in ("OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon", "BearBeeContrarian")},
    "无共振": {"ScoutBeeNova": "bullish", "OracleBeeEcho": "bearish"},
}


class TestReplayMatchesRealDistill:
    @pytest.mark.parametrize("case", list(_CASES))
    @pytest.mark.parametrize("gex", [None, {"regime": "negative_gex", "stock_price": 100.0, "gex_flip": 102.0,
                                            "vanna_stress": {"can_flip_gex": False}}])
    def test_b0_and_b3_replays_reproduce_production(self, case, gex):
        results = _agent_results(_CASES[case])
        prod_b0 = _distill("SYN", results, gex=gex)
        prod_b3 = _distill("SYN", results, no_boost=True, gex=gex)
        detected = prod_b0["resonance"]["resonance_detected"]
        assert detected == (case != "无共振")                       # 前提：夹具真的造出了共振
        # v0.45.334：负 GEX 用例的记录是**新链**（GEX 只算不加、`applied=False`）。前提断言：
        # 诊断值确实非零 —— 否则「重放跳过 GEX 步骤」与「GEX 本来就是 0」同形，这个参数化
        # 就退化成和 gex=None 一样的用例。旧链（缺 applied 键）的复现见 TestReplayFollowsRecordedGexApplied。
        if gex is not None:
            mod = prod_b0["gex_regime_mod"]
            assert mod["gex_regime"] == "negative_gex" and mod["applied"] is False, mod
            assert abs(mod["gex_adjustment"]) > 0.01, mod
        r0 = fwd.replay(prod_b0, drop_boost=False)
        r3 = fwd.replay(prod_b0, drop_boost=True)
        assert (r0["final"], r0["direction"]) == (pytest.approx(prod_b0["final_score"], abs=fwd.SCORE_TOL),
                                                  prod_b0["direction"])
        assert (r3["final"], r3["direction"]) == (pytest.approx(prod_b3["final_score"], abs=fwd.SCORE_TOL),
                                                  prod_b3["direction"])
        if detected:                                                # 非空转：两个变体确实不同
            assert abs(prod_b0["final_score"] - prod_b3["final_score"]) > 0.05
        else:
            assert prod_b0["final_score"] == prod_b3["final_score"]


# ── 3b'. 修订 1（v0.45.334）：重放按记录的 applied 标记复现生产链 ────────────────
_NEG_GEX = {"regime": "negative_gex", "stock_price": 100.0, "gex_flip": 102.0,
            "vanna_stress": {"can_flip_gex": False}}


def _distill_legacy(monkeypatch, ticker, results, no_boost=False, gex=None):
    """v0.45.334 **之前**的生产链：方向投票之后把 GexRegimeModifier 的调整加进 rule_score，
    且记录里**没有** `applied` 键。

    做法：包一层 `_compute_direction_vote`，在它返回后原样执行旧步骤 4.5 的那三行
    （`abs(g) > 0.01` 才加、clamp 到 [0,10]、round 2）。它是 distill 里唯一的调用点、
    旧步骤 4.5 紧跟其后，中间没有别的步骤 ⇒ 位置与旧代码逐步相同。g 用**真实**
    dealer_gex 算（旧生产就是这样），而重放用的是从记录反建的输入 —— 两条路不同源，
    所以「旧记录能被精确复现」不是同义反复。
    """
    from gex_regime import GexRegimeModifier
    from swarm_agents.queen_distiller import QueenDistiller
    gex_in = copy.deepcopy(gex or {"regime": "unknown"})
    orig = QueenDistiller._compute_direction_vote

    def old_vote(self, *a, **k):
        dv = orig(self, *a, **k)
        g = GexRegimeModifier().compute(gex_in, direction=dv["rule_direction"])["gex_adjustment"]
        if abs(g) > 0.01:
            dv = dict(dv, rule_score=round(max(0.0, min(10.0, dv["rule_score"] + g)), 2))
        return dv

    with monkeypatch.context() as m:
        m.setattr(QueenDistiller, "_compute_direction_vote", old_vote)
        out = _distill(ticker, results, no_boost=no_boost, gex=gex)
    assert out["gex_regime_mod"].pop("applied") is False   # 现行代码写的；旧记录没有这个键
    return out


class TestReplayFollowsRecordedGexApplied:
    """修订 1：B0 重放对**新旧两种记录**都要逐位复现生产（两个方向各一条变异）。

    新记录（`applied=False`）：生产没加 GEX ⇒ 重放也不能加。
    旧记录（缺 `applied`）：生产加了 GEX ⇒ 重放必须加。
    任一方向错了，窗口里就有一半的行自证失败，检验判「无法判定」。
    """

    @pytest.mark.parametrize("case", list(_CASES))
    def test_new_record_is_replayed_without_gex(self, case):
        """变红的变异：`replay()` 忽略 applied 标记、无条件走 GEX 步骤（即修订前的写法）。"""
        results = _agent_results(_CASES[case])
        prod_b0 = _distill("SYN", results, gex=_NEG_GEX)
        prod_b3 = _distill("SYN", results, no_boost=True, gex=_NEG_GEX)
        mod = prod_b0["gex_regime_mod"]
        g = mod["gex_adjustment"]
        # 前提：这笔诊断值若被施加，改分幅度**超出自证容差**（非零、不被 [0,10] 夹掉）——
        # 否则「错加了 GEX」也落在容差内，本条对那个变异没有牙。
        assert mod["applied"] is False, mod
        for rec in (prod_b0, prod_b3):
            assert abs(round(max(0.0, min(10.0, rec["final_score"] + g)), 2)
                       - rec["final_score"]) > 3 * fwd.SCORE_TOL, (rec["final_score"], g)
        r0 = fwd.replay(prod_b0, drop_boost=False)
        r3 = fwd.replay(prod_b0, drop_boost=True)
        # 「一致」= 预注册的自证判据（容差 SCORE_TOL）：重放从记录里取整到两位的
        # base_score_before_resonance 起算，乘共振加成后与生产的未取整链可差 0.01。
        assert (r0["final"], r0["direction"]) == (pytest.approx(prod_b0["final_score"], abs=fwd.SCORE_TOL),
                                                  prod_b0["direction"])
        assert (r3["final"], r3["direction"]) == (pytest.approx(prod_b3["final_score"], abs=fwd.SCORE_TOL),
                                                  prod_b3["direction"])
        assert r0["gex_applied"] is False and r3["gex_applied"] is False

    @pytest.mark.parametrize("case", list(_CASES))
    def test_legacy_record_is_replayed_with_gex(self, monkeypatch, case):
        """变红的变异：`gex_was_applied` 把缺键当成「没施加」（`.get("applied", False)`）——
        v0.45.334 之前的整段窗口会全部自证失败。"""
        results = _agent_results(_CASES[case])
        old_b0 = _distill_legacy(monkeypatch, "SYN", results, gex=_NEG_GEX)
        old_b3 = _distill_legacy(monkeypatch, "SYN", results, no_boost=True, gex=_NEG_GEX)
        new_b0 = _distill("SYN", results, gex=_NEG_GEX)
        assert "applied" not in old_b0["gex_regime_mod"]
        # 前提：旧链与新链在这条夹具上确实分得开、且超出自证容差（否则两个方向的测试是同一条）
        assert abs(old_b0["final_score"] - new_b0["final_score"]) > 3 * fwd.SCORE_TOL
        r0 = fwd.replay(old_b0, drop_boost=False)
        r3 = fwd.replay(old_b0, drop_boost=True)
        assert (r0["final"], r0["direction"]) == (pytest.approx(old_b0["final_score"], abs=fwd.SCORE_TOL),
                                                  old_b0["direction"])
        assert (r3["final"], r3["direction"]) == (pytest.approx(old_b3["final_score"], abs=fwd.SCORE_TOL),
                                                  old_b3["direction"])
        assert r0["gex_applied"] is True

    def test_only_literal_false_skips_gex(self):
        """只认生产会写的字面量 `False`。`None` / `0` 不是生产会写的值，不许被悄悄读成「没施加」。

        变红的变异：把 `is not False` 改成真值判断（`bool(...get("applied", True))`）。
        """
        assert fwd.gex_was_applied({"gex_regime_mod": {"gex_regime": "negative_gex"}}) is True
        assert fwd.gex_was_applied({"gex_regime_mod": {"applied": False}}) is False
        assert fwd.gex_was_applied({"gex_regime_mod": {"applied": None}}) is True
        assert fwd.gex_was_applied({"gex_regime_mod": {"applied": 0}}) is True
        assert fwd.gex_was_applied({}) is True                     # 连 gex_regime_mod 都没有 ⇒ 旧记录

    def test_mixed_window_selfproof_is_complete_and_chain_is_counted(self, monkeypatch):
        """边界前后混在同一个窗口：真 replay 下两种记录都算「复现」，且两段各计数可见。

        变红的变异：上面两条任一；或删掉 `_evaluate` 里的 `gex_chain` 计数。
        """
        results = _agent_results(_CASES["看空共振"])
        old = _distill_legacy(monkeypatch, "OLD", results, gex=_NEG_GEX)
        new = _distill("NEW", results, gex=_NEG_GEX)
        rows = [{"date": "2026-09-23", "ticker": "OLD", "sr": old},
                {"date": "2026-09-24", "ticker": "NEW", "sr": new}]
        res = fwd.evaluate(rows, {})
        assert res["selfproof"] == {"total": 2, "reproduced": 2}, res["selfproof"]
        assert res["gex_chain"] == {"applied": 1, "not_applied": 1}
        assert res["status"] == "not_ready"
        assert not (_EFFECT_KEYS & set(_all_keys(json.loads(json.dumps(res, default=str)))))


# ── 3c. 取数与 run() 的「永远说还没样本」防线 ─────────────────────────────────
def _write_report(home: Path, ticker: str, date: str, **over):
    sr = {"ticker": ticker, "agent_details": {"ScoutBeeNova": {"score": 5.0, "direction": "neutral"}},
          "data_quality": {}, "base_score_before_resonance": 5.0, "gex_regime_mod": {},
          "final_score": 5.0, "direction": "neutral"}
    sr.update(over)
    (home / f"analysis-{ticker}-ml-{date}.json").write_text(json.dumps({"swarm_results": sr}), encoding="utf-8")


class TestLoadingAndRunGuards:
    def test_forward_window_starts_at_registration_next_day(self, tmp_path):
        _write_report(tmp_path, "AAA", "2026-09-14")
        _write_report(tmp_path, "AAA", "2026-09-15")
        got = fwd.load_reports(tmp_path, since=fwd.FORWARD_START)
        assert [r["date"] for r in got["rows"]] == ["2026-09-15"]

    def test_missing_fields_and_bad_names_are_counted(self, tmp_path):
        _write_report(tmp_path, "AAA", "2026-09-16")
        sr_bad = {"ticker": "BBB", "final_score": 5.0}
        (tmp_path / "analysis-BBB-ml-2026-09-16.json").write_text(json.dumps({"swarm_results": sr_bad}))
        (tmp_path / "analysis-CCC-ml-2026-09-16_backfilled.json").write_text("{}")
        got = fwd.load_reports(tmp_path, since=fwd.FORWARD_START)
        assert len(got["rows"]) == 1
        assert sum(v for k, v in got["skipped"].items() if k.startswith("缺字段")) == 1
        assert sum(v for k, v in got["skipped"].items() if k.startswith("文件名不合规")) == 1

    def test_directory_without_any_report_is_cannot_judge(self, tmp_path):
        res = fwd.run(home=tmp_path, db_path=str(tmp_path / "absent.db"))
        assert res["status"] == "cannot_judge" and "路径" in res["reason"]

    def test_only_old_reports_is_progress_zero_without_touching_db(self, tmp_path):
        _write_report(tmp_path, "AAA", "2026-09-11")
        res = fwd.run(home=tmp_path, db_path=str(tmp_path / "absent.db"), today="2026-09-20")
        assert res["status"] == "not_ready" and res["weeks"] == 0 and not res.get("stale")
        assert fwd.status_line(res).startswith("⏳")

    def test_long_silence_is_flagged_not_reported_as_normal_progress(self, tmp_path):
        _write_report(tmp_path, "AAA", "2026-09-11")
        res = fwd.run(home=tmp_path, db_path=str(tmp_path / "absent.db"), today="2026-10-20")
        assert res["stale"] is True
        assert fwd.status_line(res).startswith("⚠️")

    def test_excess_return_prefers_raw_entry_price_and_skips_unmatured(self, tmp_path):
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT, close_t7 REAL, "
                    "price_at_predict_raw REAL, price_at_predict REAL, spy_return_t7 REAL)")
        con.executemany("INSERT INTO predictions VALUES (?,?,?,?,?,?)", [
            ("2026-09-15", "RAW", 110.0, 100.0, 50.0, 2.0),    # 用 raw：+10% − 2 = 8
            ("2026-09-15", "FB", 55.0, None, 50.0, 1.0),       # 退回 price_at_predict：+10% − 1 = 9
            ("2026-09-15", "OPEN", None, 100.0, 100.0, 1.0),   # 未到期
            ("2026-09-15", "NOSPY", 110.0, 100.0, 100.0, None),
        ])
        con.commit()
        con.close()
        got = fwd.load_excess_returns(str(db))
        assert got == {("2026-09-15", "RAW"): pytest.approx(8.0), ("2026-09-15", "FB"): pytest.approx(9.0)}


# ── 4. 承载物：ic_rerun_readiness 的摘要行 ──────────────────────────────────────
def _stub_assess(ready):
    def _assess(**_kw):
        return {"pool_note": None, "ready": ready, "weeks_accrued": 3, "weeks_required": 25,
                "eta_date": None, "eta_calendar_weeks": None, "n_ripe_samples": 0}
    return _assess


class TestCarriedByReadiness:
    def _run(self, monkeypatch, argv):
        import sys as _s
        monkeypatch.setattr(_s, "argv", ["ic_rerun_readiness.py", *argv])
        return rr.main()

    @pytest.mark.parametrize("ready,code", [(False, 1), (True, 0)])
    def test_quiet_is_one_line_carrying_forward_status_and_exit_code_unchanged(
            self, monkeypatch, tmp_path, capsys, ready, code):
        db = tmp_path / "p.db"
        db.write_bytes(b"")
        monkeypatch.setattr(rr, "assess", _stub_assess(ready))
        rc = self._run(monkeypatch, ["--db", str(db), "--quiet"])
        out = capsys.readouterr().out.strip().splitlines()
        assert rc == code
        assert len(out) == 1
        assert "IC 重跑" in out[0] and "共振加成前瞻检验" in out[0]

    def test_forward_failure_is_rendered_not_swallowed(self, monkeypatch, tmp_path, capsys):
        db = tmp_path / "p.db"
        db.write_bytes(b"")
        monkeypatch.setattr(rr, "assess", _stub_assess(False))
        monkeypatch.setattr(rr, "ALPHAHIVE_DIR", tmp_path / "nowhere")  # 实验脚本加载失败
        rc = self._run(monkeypatch, ["--db", str(db), "--quiet"])
        line = capsys.readouterr().out.strip()
        assert rc == 1
        assert "共振加成前瞻检验无法判定" in line

    def test_json_payload_has_forward_key(self, monkeypatch, tmp_path, capsys):
        db = tmp_path / "p.db"
        db.write_bytes(b"")
        monkeypatch.setattr(rr, "assess", _stub_assess(False))
        self._run(monkeypatch, ["--db", str(db), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["resonance_forward_test"]) == {"status", "line"}
