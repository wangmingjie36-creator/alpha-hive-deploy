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
        """同 ticker 后续发布（不同方向）应衰减已有条目"""
        board.publish(_entry(agent="First", score=8.0, direction="bullish"))
        first_strength = board.get_top_signals("NVDA")[0].pheromone_strength
        # 用不同方向避免共振增强（+0.2），确保只有衰减效果
        board.publish(_entry(ticker="NVDA", agent="Second", score=6.0, direction="bearish"))
        # 找回第一条目（agent_id="First"）检查其强度
        all_nvda = board.get_top_signals("NVDA", n=10)
        original = [e for e in all_nvda if e.agent_id == "First"][0]
        assert original.pheromone_strength < first_strength

    def test_publish_resonance_increments_support(self, board):
        board.publish(_entry(agent="Agent1"))
        board.publish(_entry(agent="Agent2"))
        signals = board.get_top_signals("NVDA")
        # 第二次发布应增加第一条的 support_count
        support_counts = [s.support_count for s in signals]
        assert max(support_counts) >= 1

    def test_publish_evicts_at_max(self, board):
        # 插入超过 MAX_ENTRIES 条，验证截断生效
        for i in range(PheromoneBoard.MAX_ENTRIES + 10):
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

    def test_cross_ticker_no_decay(self, board):
        """发布不同 ticker 的条目不应衰减已有条目（ticker 隔离衰减）"""
        board.publish(_entry(ticker="NVDA", score=8.0))
        nvda_before = board.get_top_signals("NVDA")[0].pheromone_strength
        board.publish(_entry(ticker="TSLA", score=6.0))
        nvda_after = board.get_top_signals("NVDA")[0].pheromone_strength
        assert nvda_after == nvda_before, "跨 ticker 发布不应衰减 NVDA 条目"

    def test_same_ticker_decay_accumulates(self, board):
        """同 ticker 多次发布（不同方向）应累积衰减"""
        board.publish(_entry(ticker="NVDA", agent="Target", score=8.0, direction="bullish"))
        initial = board.get_top_signals("NVDA")[0].pheromone_strength
        # 用 bearish 方向发布避免共振增强
        for i in range(6):
            board.publish(_entry(ticker="NVDA", agent=f"Agent{i + 1}", score=7.0, direction="bearish"))
        all_nvda = board.get_top_signals("NVDA", n=20)
        target = [e for e in all_nvda if e.agent_id == "Target"][0]
        assert target.pheromone_strength < initial, "同 ticker 多次发布应累积衰减"

    def test_entry_survives_full_scan(self, board):
        """首条目在完整 9-ticker 扫描后应存活（跨 ticker 不衰减）"""
        board.publish(_entry(ticker="NVDA", agent="ScoutBeeNova", score=8.0))
        # 模拟 8 个其他 ticker，每个 7 个 Agent = 56 次跨 ticker 发布
        for i, ticker in enumerate(["TSLA", "MSFT", "AMD", "QCOM", "META", "AMZN", "JNJ", "COIN"]):
            for j in range(7):
                board.publish(_entry(ticker=ticker, agent=f"Agent{i}_{j}", score=5.0 + j * 0.5))
        nvda_signals = board.get_top_signals("NVDA")
        assert len(nvda_signals) > 0, "NVDA 条目应在跨 ticker 发布后存活"
        assert nvda_signals[0].pheromone_strength >= board.MIN_STRENGTH


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

    def test_bearish_resonance_includes_contrarian(self, board):
        """看空共振中 BearBee(contrarian) 应计入维度数"""
        for agent in ["ScoutBeeNova", "OracleBeeEcho", "BearBeeContrarian"]:
            board.publish(_entry(agent=agent, direction="bearish", score=3.0))
        res = board.detect_resonance("NVDA")
        assert res["resonance_detected"], "3 维度看空（含 contrarian）应触发共振"
        assert res["direction"] == "bearish"
        assert "contrarian" in res["resonant_dimensions"]
        assert res["cross_dim_count"] >= 3

    def test_bullish_resonance_excludes_contrarian(self, board):
        """看多共振中 BearBee(contrarian) 不应计入维度数"""
        for agent in ["ScoutBeeNova", "OracleBeeEcho", "BearBeeContrarian"]:
            board.publish(_entry(agent=agent, direction="bullish", score=8.0))
        res = board.detect_resonance("NVDA")
        # 仅 2 个有效维度（signal + odds），contrarian 被排除
        assert not res["resonance_detected"], "看多方向下 contrarian 不应计入共振"
        assert res["cross_dim_count"] == 2

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
        assert {"a", "t", "d", "s", "p", "c"}.issubset(compact[0].keys())

    def test_compact_snapshot_includes_details(self, board):
        """D1: compact_snapshot 应包含白名单 details 字段"""
        e = _entry()
        e.details = {"pc_ratio": 1.35, "iv_rank": 72.0, "ignored_key": "nope"}
        board.publish(e)
        compact = board.compact_snapshot()
        assert "x" in compact[0]
        assert compact[0]["x"]["pc_ratio"] == 1.35
        assert compact[0]["x"]["iv_rank"] == 72.0
        assert "ignored_key" not in compact[0]["x"]

    def test_compact_snapshot_no_x_when_empty_details(self, board):
        """无 details 时不应有 x 键"""
        board.publish(_entry())
        compact = board.compact_snapshot()
        assert "x" not in compact[0]

    def test_compact_snapshot_direction_no_collision(self, board):
        """bullish/bearish 方向编码不应碰撞"""
        board.publish(_entry(direction="bullish", agent="A"))
        board.publish(_entry(direction="bearish", agent="B"))
        board.publish(_entry(direction="neutral", agent="C"))
        compact = board.compact_snapshot()
        dirs = {c["d"] for c in compact}
        assert len(dirs) == 3, f"方向编码碰撞: {dirs}"
        assert "+" in dirs  # bullish
        assert "-" in dirs  # bearish
        assert "0" in dirs  # neutral


class TestEviction:
    def test_bad_timestamp_does_not_break_eviction(self, board):
        """时间戳解析失败的条目不应阻止整个清理流程"""
        # 手动插入一条正常 + 一条坏时间戳条目
        good = _entry(agent="GoodAgent")
        bad = _entry(agent="BadAgent")
        bad.timestamp = "not-a-date"
        board._entries.extend([good, bad])
        assert board.get_entry_count() == 2
        # publish 触发清理，坏时间戳条目应被安全移除
        board.publish(_entry(agent="Trigger"))
        # 坏时间戳条目应被移除（视为过期）
        remaining_agents = {e.agent_id for e in board._entries}
        assert "BadAgent" not in remaining_agents, "坏时间戳条目应在清理中移除"

    def test_normal_entries_survive_eviction(self, board):
        """正常条目在 60 分钟内应存活"""
        board.publish(_entry(agent="Survivor", score=8.0))
        board.publish(_entry(agent="Trigger", score=5.0, direction="bearish"))
        agents = {e.agent_id for e in board._entries}
        assert "Survivor" in agents


class TestEmptyBoardResonance:
    def test_empty_board_returns_neutral(self, board):
        """空板共振检测应返回 neutral 方向"""
        res = board.detect_resonance("NVDA")
        assert not res["resonance_detected"]
        assert res["direction"] == "neutral"
        assert res["supporting_agents"] == 0
        assert res["cross_dim_count"] == 0

    def test_single_entry_returns_correct_direction(self, board):
        """仅 1 条 bearish 条目应返回 bearish 方向（而非默认 bullish）"""
        board.publish(_entry(agent="ScoutBeeNova", direction="bearish"))
        res = board.detect_resonance("NVDA")
        assert res["direction"] == "bearish"
        assert not res["resonance_detected"]


class TestClear:
    def test_clear_empties_board(self, board):
        board.publish(_entry())
        board.clear()
        assert board.get_entry_count() == 0


# ==================== 方案13: 输入验证测试 ====================


class TestValidateEntry:
    """方案13: 验证 _validate_entry() 防垃圾数据"""

    def test_score_clamped_to_10(self, board):
        """self_score > 10 → clamp 到 10"""
        e = _entry(score=15.0)
        board.publish(e)
        assert e.self_score == 10.0

    def test_score_clamped_to_0(self, board):
        """self_score < 0 → clamp 到 0"""
        e = _entry(score=-3.0)
        board.publish(e)
        assert e.self_score == 0.0

    def test_score_nan_becomes_5(self, board):
        """self_score = NaN → 设为 5.0"""
        e = _entry(score=float("nan"))
        board.publish(e)
        assert e.self_score == 5.0

    def test_score_inf_becomes_5(self, board):
        """self_score = Inf → 设为 5.0"""
        e = _entry(score=float("inf"))
        board.publish(e)
        assert e.self_score == 5.0

    def test_score_normal_passes(self, board):
        """正常 self_score 不变"""
        e = _entry(score=7.5)
        board.publish(e)
        assert e.self_score == 7.5

    def test_invalid_direction_becomes_neutral(self, board):
        """无效 direction → 'neutral'"""
        e = PheromoneEntry(
            agent_id="TestAgent", ticker="NVDA", discovery="test",
            source="test", self_score=5.0, direction="SUPER_BULLISH"
        )
        board.publish(e)
        assert e.direction == "neutral"

    def test_empty_direction_becomes_neutral(self, board):
        """空 direction → 'neutral'"""
        e = PheromoneEntry(
            agent_id="TestAgent", ticker="NVDA", discovery="test",
            source="test", self_score=5.0, direction=""
        )
        board.publish(e)
        assert e.direction == "neutral"

    def test_valid_directions_pass(self, board):
        """合法 direction 不变"""
        for d in ("bullish", "bearish", "neutral"):
            e = PheromoneEntry(
                agent_id="TestAgent", ticker="NVDA", discovery="test",
                source="test", self_score=5.0, direction=d
            )
            board.publish(e)
            assert e.direction == d

    def test_pheromone_strength_clamped_high(self, board):
        """pheromone_strength > 1 → clamp 到 1"""
        e = PheromoneEntry(
            agent_id="TestAgent", ticker="NVDA", discovery="test",
            source="test", self_score=5.0, direction="bullish",
            pheromone_strength=2.5
        )
        board.publish(e)
        assert e.pheromone_strength <= 1.0

    def test_pheromone_strength_clamped_low(self, board):
        """pheromone_strength < 0 → clamp 到 0"""
        e = PheromoneEntry(
            agent_id="TestAgent", ticker="NVDA", discovery="test",
            source="test", self_score=5.0, direction="bullish",
            pheromone_strength=-0.5
        )
        board.publish(e)
        assert e.pheromone_strength == 0.0

    def test_pheromone_strength_nan_becomes_1(self, board):
        """pheromone_strength = NaN → 设为 1.0"""
        e = PheromoneEntry(
            agent_id="TestAgent", ticker="NVDA", discovery="test",
            source="test", self_score=5.0, direction="bullish",
            pheromone_strength=float("nan")
        )
        board.publish(e)
        assert e.pheromone_strength == 1.0
