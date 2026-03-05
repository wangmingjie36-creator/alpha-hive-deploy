"""PheromoneBoard 单元测试"""

import pytest
from pheromone_board import PheromoneBoard, PheromoneEntry


def _entry(ticker="NVDA", direction="bullish", score=7.0, agent="TestAgent"):
    return PheromoneEntry(
        agent_id=agent, ticker=ticker, discovery="test",
        source="test", self_score=score, direction=direction,
    )


class TestPublish:
    def test_publish_adds_entry(self, board):
        board.publish(_entry())
        assert board.get_entry_count() == 1

    def test_publish_decays_existing(self, board):
        board.publish(_entry(score=8.0))
        first_strength = board.get_top_signals("NVDA")[0].pheromone_strength
        board.publish(_entry(ticker="TSLA", score=6.0))
        after_strength = board.get_top_signals("NVDA")[0].pheromone_strength
        assert after_strength < first_strength

    def test_publish_resonance_increments_support(self, board):
        board.publish(_entry(agent="Agent1"))
        board.publish(_entry(agent="Agent2"))
        signals = board.get_top_signals("NVDA")
        # 第二次发布应增加第一条的 support_count
        support_counts = [s.support_count for s in signals]
        assert max(support_counts) >= 1

    def test_publish_evicts_at_max(self, board):
        for i in range(25):
            board.publish(_entry(agent=f"Agent{i}", score=float(i % 10)))
        assert board.get_entry_count() <= PheromoneBoard.MAX_ENTRIES

    def test_publish_removes_weak_entries(self, board):
        """低强度条目在衰减后应被清除"""
        # 新条目在 <5min 内衰减 0.05/次，需要 >=17 次才能从 1.0 降到 < 0.2
        for _ in range(20):
            board.publish(_entry(ticker="FILLER"))
        # 最早的条目 1.0 - 19*0.05 = 0.05 < MIN_STRENGTH(0.2)，应被清除
        count = board.get_entry_count()
        assert count < 20

    def test_same_agent_no_double_support(self, board):
        """同一 Agent 重复发布同 ticker 不应增加 support_count"""
        board.publish(_entry(agent="ScoutBeeNova"))
        board.publish(_entry(agent="ScoutBeeNova"))  # 同 agent 同 ticker
        signals = board.get_top_signals("NVDA")
        # support_count 应该只有初始的 0（第一条自带 agent_id 在 supporting_agents）
        # 第二次发布时检测到同 agent，不增加
        assert signals[0].support_count == 0


class TestGetTopSignals:
    def test_returns_sorted_by_strength(self, board):
        board.publish(_entry(agent="A", score=9.0))
        board.publish(_entry(ticker="TSLA", agent="B", score=3.0))
        top = board.get_top_signals(n=2)
        assert top[0].pheromone_strength >= top[-1].pheromone_strength

    def test_filter_by_ticker(self, board):
        board.publish(_entry(ticker="NVDA"))
        board.publish(_entry(ticker="TSLA"))
        nvda = board.get_top_signals(ticker="NVDA")
        assert all(s.ticker == "NVDA" for s in nvda)

    def test_empty_board_returns_empty(self, board):
        assert board.get_top_signals() == []


class TestResonance:
    def test_no_resonance_below_threshold(self, board):
        board.publish(_entry(agent="A"))
        board.publish(_entry(agent="B"))
        res = board.detect_resonance("NVDA")
        assert not res["resonance_detected"]

    def test_resonance_at_three_agents(self, board):
        # P2a: 需要来自 ≥3 个不同数据维度的真实 Agent 名才触发共振
        # signal(ScoutBeeNova) + odds(OracleBeeEcho) + sentiment(BuzzBeeWhisper) = 3 维
        for agent in ["ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper"]:
            board.publish(_entry(agent=agent))
        res = board.detect_resonance("NVDA")
        assert res["resonance_detected"]
        assert res["direction"] == "bullish"
        assert res["cross_dim_count"] >= 3

    def test_confidence_boost_capped(self, board):
        for i in range(10):
            board.publish(_entry(agent=f"Agent{i}"))
        res = board.detect_resonance("NVDA")
        assert res["confidence_boost"] <= 20


class TestSnapshot:
    def test_snapshot_returns_all(self, board):
        board.publish(_entry(agent="A"))
        board.publish(_entry(agent="B"))
        snap = board.snapshot()
        assert len(snap) == 2
        assert all("agent_id" in s for s in snap)

    def test_compact_snapshot_has_short_keys(self, board):
        board.publish(_entry())
        compact = board.compact_snapshot()
        assert len(compact) == 1
        assert set(compact[0].keys()) == {"a", "t", "d", "s", "p", "c"}


class TestClear:
    def test_clear_empties_board(self, board):
        board.publish(_entry())
        board.clear()
        assert board.get_entry_count() == 0
