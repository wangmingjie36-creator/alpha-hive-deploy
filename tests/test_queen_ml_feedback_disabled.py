"""蜂后 ML 反馈只算不施加（v0.45.537）

缺陷
----
`QueenDistiller.__init__` 拿每日重训 HGB 的特征重要性乘维度权重，`_compute_direction_vote`
拿同一组乘数缩放每只蜂的票重 —— 两处都**没有 OOS 闸**（Rival 那一项有 `_ml_oos_trust_factor`）。
09-10 → 10-16 的 25 个扫描日：场内模型 OOS 均值 51.5%、8 天 < 50%；09-18（OOS 46.3%）把 sentiment
从 0.325 推到 0.549；signal 乘数常 1.5–2.0 ⇒ 分数权重已归零的 Scout 一票顶 Chronos/Oracle 四票，
离线重放约 9.5% 的方向因此改变。函数形式本身也有偏（训练集上的重要性、按特征数取均值 ⇒ odds 25/25 天压在下限）。

修法（用户 2026-10-19 选方案 A）：两个消费者都关，乘数照算进 `ml_weight_adjustments_shadow`。
记录里 `ml_feedback_enabled=False` / `ml_weight_adjustments={}` —— **如实记录没施加**，
这样共振加成前瞻检验的重放对新旧记录都能复现（它按记录的这两个键 + 当时的 config 开关复现投票）。
"""

import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = Path(__file__).resolve().parent.parent


class _StrongModel:
    """重要性全压在 crowding(→signal) 与 momentum(→sentiment) 上：旧代码会大幅改权重与票重。"""

    is_trained = True

    def get_feature_importance(self):
        w = {"crowding": 0.45, "momentum": 0.40, "catalyst": 0.02, "sentiment": 0.02,
             "volatility": 0.02, "iv_rank": 0.02, "put_call_ratio": 0.02, "odds_score": 0.02,
             "risk_adj_score": 0.01, "final_score": 0.01, "agent_agreement": 0.005,
             "direction_encoded": 0.005}
        return {k: {"weight": v, "coefficient": v, "direction": "positive"} for k, v in w.items()}


def _r(source, dim, direction, score=6.0, confidence=0.6):
    return {"source": source, "dimension": dim, "direction": direction, "score": score,
            "confidence": confidence, "discovery": "t", "data_quality": {"t": "real"}, "details": {}}


# 方向票有分歧、且 Scout 与其余蜂反向：旧代码下 signal 乘数会把 Scout 的票放大到能翻方向的程度
_RESULTS = [
    _r("ScoutBeeNova", "signal", "bearish", 4.0, 0.9),
    _r("ChronosBeeHorizon", "catalyst", "bullish", 7.0, 0.6),
    _r("BuzzBeeWhisper", "sentiment", "bullish", 6.5, 0.6),
    _r("OracleBeeEcho", "odds", "bearish", 4.5, 0.6),
]


def _queens(board):
    from swarm_agents import QueenDistiller
    return (QueenDistiller(board, enable_llm=False, ml_model=_StrongModel()),
            QueenDistiller(board, enable_llm=False, ml_model=None))


class TestNotApplied:

    def test_shadow_is_computed_positive_control(self, board):
        """前提：模型确实接上了、影子乘数确实想改 —— 否则下面的「没改」证明不了什么。"""
        q, _ = _queens(board)
        assert q.ml_adjustments_shadow, "影子乘数为空 ⇒ 夹具没接上，本文件其余断言都是空转"
        assert q.ml_adjustments_shadow["signal"] > 1.5
        assert q.ml_adjustments_shadow["odds"] == 0.5

    def test_dimension_weights_equal_config(self, board):
        """变红的变异：把 __init__ 里的乘法 + 归一化放回去。"""
        import config
        q, q0 = _queens(board)
        assert q.DIMENSION_WEIGHTS == q0.DIMENSION_WEIGHTS
        for d, w in config.EVALUATION_WEIGHTS.items():
            assert q.DIMENSION_WEIGHTS[d] == pytest.approx(w, abs=1e-12), d

    def test_flags_record_not_applied(self, board):
        """变红的变异：`self.ml_adjustments = 影子` 或 `ml_feedback_enabled = True`。"""
        q, _ = _queens(board)
        assert q.ml_feedback_enabled is False
        assert q.ml_adjustments == {}

    def test_direction_vote_unchanged(self, board):
        """票重与无模型时逐项相等（变异：让投票读影子乘数）。"""
        q, q0 = _queens(board)
        dv = q._compute_direction_vote("T", _RESULTS, _RESULTS, 6.0)
        dv0 = q0._compute_direction_vote("T", _RESULTS, _RESULTS, 6.0)
        assert dv["direction_vote_weights"] == dv0["direction_vote_weights"]
        assert dv["rule_direction"] == dv0["rule_direction"]

    def test_distill_output_fields(self, board):
        import config
        q, q0 = _queens(board)
        out = q.distill("T", list(_RESULTS))
        out0 = q0.distill("T", list(_RESULTS))
        assert out["ml_feedback_enabled"] is False
        assert out["ml_weight_adjustments"] == {}
        assert out["ml_weight_adjustments_shadow"] == q.ml_adjustments_shadow
        assert out0["ml_weight_adjustments_shadow"] == {}
        assert out["dimension_weights_base"] == {k: v for k, v in config.EVALUATION_WEIGHTS.items()}
        assert out["final_score"] == out0["final_score"]
        assert out["direction"] == out0["direction"]


class TestOldRecordsStillReplay:
    """投票缩放代码与 config 开关是**重放旧记录**的机器，不能顺手删 / 关。"""

    def test_vote_scaling_still_applies_when_record_says_so(self, board):
        """正对照：把旧记录的两个键塞回去，缩放照旧生效（共振检验重放就是这么做的）。"""
        q, q0 = _queens(board)
        q.ml_feedback_enabled = True
        q.ml_adjustments = {"signal": 2.0, "catalyst": 0.5, "sentiment": 0.5,
                            "odds": 0.5, "risk_adj": 0.5}
        dv = q._compute_direction_vote("T", _RESULTS, _RESULTS, 6.0)
        dv0 = q0._compute_direction_vote("T", _RESULTS, _RESULTS, 6.0)
        assert dv["direction_vote_weights"] != dv0["direction_vote_weights"]

    def test_config_switches_stay_true(self):
        """翻成 False ⇒ 重放窗口内旧记录时不施加 ⇒ 复现不出 ⇒ 自证 < 95%，检验 exit 3。"""
        tree = ast.parse((REPO / "config.py").read_text(encoding="utf-8"))
        cfg = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "ML_FEEDBACK_CONFIG" for t in node.targets):
                cfg = ast.literal_eval(node.value)
        assert cfg is not None, "config.py 里找不到 ML_FEEDBACK_CONFIG 字面量（探针失明）"
        assert cfg["enable_dimension_weighting"] is True
        assert cfg["enable_vote_boosting"] is True

    def test_new_record_replays(self, board):
        """新记录经共振检验的重放能复现（分数 + 方向）。"""
        from experiments.resonance_boost_forward_test import replay
        q, _ = _queens(board)
        out = q.distill("T", list(_RESULTS))
        r = replay(out, drop_boost=False)
        assert r["direction"] == out["direction"]
        assert abs(r["final"] - out["final_score"]) <= 0.011


class TestRuntimeProbe:

    def test_equal_is_true(self):
        from alpha_hive_daily_report import _assert_base_weights_equal_config
        cfg = {"signal": 0.0, "catalyst": 0.332, "sentiment": 0.325, "odds": 0.343, "risk_adj": 0.0}
        assert _assert_base_weights_equal_config(dict(cfg), cfg_weights=cfg) is True

    def test_ml_style_reweight_is_red(self, caplog):
        """10-16 生产的基准权重：零维守卫对它恒绿，这条必须红。"""
        from alpha_hive_daily_report import (_assert_base_weights_equal_config,
                                             _assert_config_zeros_survive)
        cfg = {"signal": 0.0, "catalyst": 0.332, "sentiment": 0.325, "odds": 0.343, "risk_adj": 0.0}
        prod_1016 = {"signal": 0.0, "catalyst": 0.2943, "sentiment": 0.4016, "odds": 0.3041,
                     "risk_adj": 0.0}
        assert _assert_config_zeros_survive(prod_1016, cfg_weights=cfg) is True  # 对照：旧守卫看不见
        with caplog.at_level("ERROR"):
            assert _assert_base_weights_equal_config(prod_1016, cfg_weights=cfg) is False
        assert "基准权重偏离 config" in caplog.text

    def test_real_queen_with_model_passes(self, board):
        from alpha_hive_daily_report import _assert_base_weights_equal_config
        q, _ = _queens(board)
        assert _assert_base_weights_equal_config(q.DIMENSION_WEIGHTS) is True

    def test_probe_is_called_in_scan(self):
        """守卫没人调用就是死代码。"""
        tree = ast.parse((REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "_assert_base_weights_equal_config"]
        assert len(calls) == 1


class TestCohortBoundary:

    def test_entry_registered(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        hits = [r for d, v, r in _COHORT_HISTORY if v == "v0.45.537"]
        assert len(hits) == 1 and "ml_weight_adjustments_shadow" in hits[0]

    def test_marker_recognises_new_records_only(self, board):
        """印记判据：新代码的蒸馏结果认得出、旧归档形状（无影子键）认不出、补跑行不算日期证据。

        没有这条时，印记函数里键名拼错 ⇒ 边界证据永远停在 no_evidence_yet，没有任何测试会红（v0.45.540 补）。
        """
        import ic_rerun_readiness as rr
        q, _ = _queens(board)
        out = q.distill("T", list(_RESULTS))
        judge = rr._BOUNDARY_MARKERS["v0.45.537"][1]
        assert judge({"swarm_results": out}) is True
        old = {k: v for k, v in out.items() if k != "ml_weight_adjustments_shadow"}
        assert judge({"swarm_results": old}) is False
        backfill = dict(out, agent_details={"GuardBeeSentinel": {"details": {
            "vix_term_structure": {"macro_as_of_mode": "backfill"}}}})
        assert judge({"swarm_results": backfill}) is False

    def test_ml_generation_entry_names_direction_features(self):
        """蜂后方向变 ⇒ direction_encoded 与 agent_agreement（与蜂群方向一致的占比）都变；初版写反了（v0.45.540）。"""
        import probability_scorecard as ps
        hits = [r for d, v, r in ps._ML_ESTIMATOR_GENERATIONS if "v0.45.537" in v]
        assert len(hits) == 1
        assert "direction_encoded" in hits[0] and "agent_agreement" in hits[0]
        assert "不受影响。" not in hits[0]

    def test_scope_is_empty(self):
        """蜂后层改动：不改任何归档蜂信号 ⇒ 不截断维度 IC 的 H1 / H2（先例 v0.45.228 / 334）。"""
        import signal_archive
        assert signal_archive.COHORT_SIGNAL_SCOPE["v0.45.537"] == ()
