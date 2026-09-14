"""共振只数独立来源：GuardBeeSentinel 不进 Queen 的共振检测（v0.45.235）

`detect_resonance` 的触发条件是「同向 Agent 覆盖 ≥3 个不同数据维度」，docstring 原话是
**「真正的多源独立印证」**，旧逻辑被替换的理由正是「多个 Agent 基于相同数据的虚假放大」。
Guard 的方向是其余六只多数的复述（v0.45.209：普查口径后 90/90），它在板上带着维度
`risk_adj` —— 复述多数时就给同向方**凭空多出一个维度**，还同时抬高 consistency 的分子与分母。
v0.45.212 已让它退出方向计票，共振这条复述通道当时登记为「未测未动」。

实测（真实代码逐位重放，先用旧代码复现 09-08~09-11 共 48 份记录的 final_score/方向 48/48）：

| | 历史原样（681 行） | 现行规则重演（681 行） |
|---|---|---|
| 分数改变 | 36.4%，中位 −0.32 | 11.3%，中位 −0.30 |
| 方向改变 | 0 | 0（共振只进分数，不进计票） |
| final_score 逐日横截面 IC 变化 | −0.003（周聚类 p=0.89） | −0.001（p=0.77） |
| 纸面组合入场资格变化 | 失去 5 行（这 5 行方向只对 20%） | 0 |

⇒ 这条通道**对收益没有可测影响**，只是在抬分数。理由是结构性的：复述票不是独立来源。

⚠️ 本版**不动** BearBeeContrarian 在看空共振里的维度（现有设计明确计入，见
`test_queen_distiller.py::test_bearish_resonance_with_bearbee`）。同批测过把它也拿掉：
现行规则下共振判定变 72 行、新获 10 笔看空入场 —— 那是另一个设计决定，留给用户。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GUARD = "GuardBeeSentinel"
FIELDS = ("resonance_detected", "direction", "supporting_agents", "cross_dim_count",
          "consistency", "resonant_dimensions", "confidence_boost")


def _board_with(board, ticker, entries):
    from pheromone_board import PheromoneEntry
    for agent, direction in entries:
        board.publish(PheromoneEntry(agent_id=agent, ticker=ticker, discovery=f"t {agent}",
                                     source="test", self_score=6.0, direction=direction))
    return board.detect_resonance(ticker)


def _fresh_board():
    from pheromone_board import PheromoneBoard
    return PheromoneBoard()


class TestGuardDoesNotCountInResonance:

    @pytest.mark.parametrize("peers", [
        [("OracleBeeEcho", "bullish"), ("RivalBeeVanguard", "bullish")],                          # 旧逻辑下 Guard 凑出第 3 维
        [("ScoutBeeNova", "bullish"), ("BuzzBeeWhisper", "bullish"), ("OracleBeeEcho", "bullish"),
         ("ChronosBeeHorizon", "bearish")],                                                        # 本就共振：Guard 改 boost 与 consistency
        [("ScoutBeeNova", "bearish"), ("OracleBeeEcho", "bearish"), ("BuzzBeeWhisper", "neutral")],
        [("ChronosBeeHorizon", "bullish"), ("RivalBeeVanguard", "neutral")],
    ])
    @pytest.mark.parametrize("guard_dir", ["bullish", "bearish", "neutral"])
    def test_guard_entry_does_not_change_resonance(self, peers, guard_dir):
        """判据本体：Guard 投什么方向，共振结果的每个字段都不变。"""
        without = _board_with(_fresh_board(), "RSN", peers)
        with_g = _board_with(_fresh_board(), "RSN", peers + [(GUARD, guard_dir)])
        diff = {k: (without[k], with_g[k]) for k in FIELDS if without[k] != with_g[k]}
        assert not diff, f"Guard（{guard_dir}）改变了共振：{diff}"

    def test_guard_can_no_longer_supply_the_third_dimension(self, board):
        """旧行为的显式反例：odds + ml_auxiliary 两维同向，Guard 复述后曾凑成 3 维触发共振。"""
        res = _board_with(board, "RSG", [("OracleBeeEcho", "bullish"), ("RivalBeeVanguard", "bullish"), (GUARD, "bullish")])
        assert res["resonance_detected"] is False
        assert "risk_adj" not in res["resonant_dimensions"]
        assert res["supporting_agents"] == 2

    def test_three_independent_dimensions_still_resonate(self, board):
        """正对照：三只观测蜂三个维度同向照旧触发 —— 防「共振整个关掉」这种实现。"""
        res = _board_with(board, "RSI", [("ScoutBeeNova", "bullish"), ("OracleBeeEcho", "bullish"), ("BuzzBeeWhisper", "bullish")])
        assert res["resonance_detected"] is True
        assert res["cross_dim_count"] == 3

    def test_bearbee_still_counts_in_bearish_resonance(self, board):
        """登记现状：本版只排除 Guard，BearBee 的 contrarian 维度照旧计入看空共振。

        这条变红 = 有人把 BearBee 也排除了 —— 那是另一个设计决定（实测改动更大，见模块 docstring）。
        """
        res = _board_with(board, "RSB", [("ScoutBeeNova", "bearish"), ("OracleBeeEcho", "bearish"), ("BearBeeContrarian", "bearish")])
        assert res["resonance_detected"] is True
        assert "contrarian" in res["resonant_dimensions"]

    def test_census_view_for_guard_is_untouched(self, board):
        """Guard 自己读同伴用的普查视图（get_live_signals）不在本版范围内，照旧返回全部已发布的蜂。"""
        _board_with(board, "RSC", [("ScoutBeeNova", "bullish"), (GUARD, "bullish")])
        assert {e.agent_id for e in board.get_live_signals("RSC")} == {"ScoutBeeNova", GUARD}


class TestCohortBoundary:

    def test_entry_registered(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        hits = [r for d, v, r in _COHORT_HISTORY if v == "v0.45.235"]
        assert len(hits) == 1 and "detect_resonance" in hits[0]

    def test_extends_the_2026_09_13_label(self):
        """与 v0.45.212 / v0.45.228 同标签：该边界之后到本版落地 predictions 0 条，作废 0 条。"""
        from ic_rerun_readiness import _COHORT_HISTORY
        by_ver = {v: d for d, v, _ in _COHORT_HISTORY}
        assert by_ver["v0.45.235"] == by_ver["v0.45.212"]
