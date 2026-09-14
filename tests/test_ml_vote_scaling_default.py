"""ML 票重缩放里「没有乘数的投票蜂」取当期平均乘数，不取 1.0（v0.45.228）

缺陷
----
`_effective_conf` 乘 `ml_adjustments.get(维度, 1.0)`。设计隐含「1.0 = 平均重要性」，
但乘数由 `_compute_ml_weight_adjustments` 算出，**被下限 0.5 主导**：793 份 JSON 里
odds 82.8%、catalyst 75.2%、signal 67.2%、risk_adj 96.3% 的时候恰为 0.5，中位数 0.500
（12 个特征里 3 个不映射任何维度，单个特征的重要性份额远低于 1/5 的维度基准）。
于是 RivalBee（`ml_auxiliary` 不在表里）与 CodeExec（没有维度）的 1.0 实际是
**平均水平的 1.5–2.0 倍**（逐月 1.63×–2.00×）。

这两只正好是 623/552 条方向单里 85.9%/85.3% 看多、扣 SPY 命中 48.2%/52.0% 的蜂。

另一个消费者（维度权重）乘完会**归一化**，乘数的绝对水平在那里本来就不起作用 ——
豁免只存在于投票这一处，所以修法只动投票：缺乘数的投票蜂取**当期所有乘数的均值**。
等价于「只保留乘数之间的相对信息，水平归一」；**全部压在下限时 ≡ 不缩放**。

⚠️ 结果证据是零效应，且**点估计略微不利于本修复**（预注册两语料三指标 p 0.45–0.87，
池化均值全为负；被拿掉的看多跑赢 SPY 55.2%/60.9%，同日基准 53.7%/54.5%，匹配抽样 p≈0.13）。
本版的理由是结构性的，详见 CHANGELOG v0.45.228。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FLOOR = {"signal": 0.5, "catalyst": 0.5, "sentiment": 0.5, "odds": 0.5, "risk_adj": 0.5}


def _r(source, direction, confidence=0.6):
    return {"source": source, "direction": direction, "confidence": confidence, "score": 5.0,
            "discovery": "t", "data_quality": {"t": "real"}, "details": {}}


def _queen(board, adjustments):
    from swarm_agents import QueenDistiller
    q = QueenDistiller(board)
    q.ml_feedback_enabled = bool(adjustments)
    q.ml_adjustments = dict(adjustments)
    return q


def _vote(q, res):
    return q._compute_direction_vote("MLV", res, res, 6.0)


# 单票上限 = 投票蜂置信度之和 × 0.4。只有两三只蜂时上限会咬住、把被测的票重先削平，
# 断言就不再测乘数（初稿两条就是这样在旧代码上「因为错的原因」红了）。补两只中性蜂垫高总和。
_PAD = [_r("BuzzBeeWhisper", "neutral", 0.5), _r("ChronosBeeHorizon", "neutral", 0.5)]


class TestUntrackedVotersGetTheMeanMultiplier:

    @pytest.mark.parametrize("untracked", ["RivalBeeVanguard", "CodeExecutorAgent"])
    def test_exact_weight_uses_mean_multiplier(self, board, untracked):
        """判据本体：缺乘数的投票蜂 = 置信度 × 当期乘数均值（非均匀乘数，均值≠最小≠最大）。"""
        adj = dict(FLOOR, signal=1.3)                 # 均值 = (1.3+0.5×4)/5 = 0.66
        q = _queen(board, adj)
        dv = _vote(q, [_r("ScoutBeeNova", "bullish", 0.5), _r(untracked, "bearish", 0.5)] + _PAD)
        assert dv["direction_vote_weights"]["bullish"] == pytest.approx(0.5 * 1.3, abs=1e-3)
        assert dv["direction_vote_weights"]["bearish"] == pytest.approx(0.5 * 0.66, abs=1e-3), (
            f"{untracked} 的票重应乘当期平均乘数 0.66（旧行为是 1.0）"
        )

    @pytest.mark.parametrize("res", [
        [_r("ScoutBeeNova", "bullish", 0.8), _r("RivalBeeVanguard", "bearish", 0.8)],
        [_r("OracleBeeEcho", "bullish", 0.7), _r("BuzzBeeWhisper", "bullish", 0.6),
         _r("RivalBeeVanguard", "bearish", 0.9), _r("CodeExecutorAgent", "bearish", 0.9)],
        [_r("ChronosBeeHorizon", "bullish", 0.6), _r("ScoutBeeNova", "bullish", 0.6),
         _r("OracleBeeEcho", "bullish", 0.6), _r("RivalBeeVanguard", "neutral", 0.9),
         _r("CodeExecutorAgent", "bearish", 0.9)],
    ])
    def test_uniform_floor_is_the_same_as_no_scaling(self, board, res):
        """乘数全部压在下限（生产 14.8% 的日子）时，缩放不得改变任何人的相对票重。

        旧行为下此时豁免蜂恰好是 2 倍 —— 这是它最极端、也最常见的形态。
        """
        with_ml = _vote(_queen(board, FLOOR), res)
        without = _vote(_queen(board, {}), res)
        w1, w0 = with_ml["direction_vote_weights"], without["direction_vote_weights"]
        t1, t0 = sum(w1.values()), sum(w0.values())
        assert with_ml["rule_direction"] == without["rule_direction"]
        for k in ("bullish", "bearish", "neutral"):
            assert w1[k] / t1 == pytest.approx(w0[k] / t0, abs=1e-3), f"{k} 票重占比被缩放改变：{w1} vs {w0}"

    def test_relative_ml_information_is_still_applied(self, board):
        """正对照：追踪维度之间的相对差异照旧生效 —— 防「干脆关掉缩放」这种实现。"""
        q = _queen(board, dict(FLOOR, signal=1.3))
        dv = _vote(q, [_r("ScoutBeeNova", "bullish", 0.6), _r("OracleBeeEcho", "bearish", 0.6)] + _PAD)
        w = dv["direction_vote_weights"]
        assert w["bullish"] > w["bearish"], f"signal 1.3 vs odds 0.5 应拉开票重：{w}"

    def test_no_ml_means_raw_confidence(self, board):
        q = _queen(board, {})
        dv = _vote(q, [_r("RivalBeeVanguard", "bullish", 0.7), _r("ScoutBeeNova", "bearish", 0.6)] + _PAD)
        assert dv["direction_vote_weights"] == {"bullish": 0.7, "bearish": 0.6, "neutral": 1.0}


class TestCohortBoundary:

    def test_entry_registered(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        hits = [r for d, v, r in _COHORT_HISTORY if v == "v0.45.228"]
        assert len(hits) == 1 and "ml_adjustments" in hits[0]

    def test_extends_the_v0_45_212_label(self):
        """与 v0.45.212 同标签：09-13 边界之后到本版落地时 predictions 0 条，作废 0 条。"""
        from ic_rerun_readiness import _COHORT_HISTORY
        by_ver = {v: d for d, v, _ in _COHORT_HISTORY}
        assert by_ver["v0.45.228"] == by_ver["v0.45.212"]
