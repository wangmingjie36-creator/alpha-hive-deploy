"""detect_resonance 被 MAX_ENTRIES 溢出淘汰系统性删掉低分蜂（v0.45.156）

背景（全部为 803 份生产 `analysis-*-ml-*.json` 实测，非推断）
------------------------------------------------------------
`swarm_results.pheromone_compact` 就是 `board.compact_snapshot(ticker)`，与
`detect_resonance` 同在一次 `distill()` 调用内（`queen_distiller.py:334` 取共振、
`:1161` 取快照、`:1230` 才发布 Queen 自己的条目 ⇒ 快照不含 Queen）。因此
**共振当时实际看到几条**是直接观测量，不需要模拟。

三条仪器自证（缺一条结论不成立）：

1. 用快照重算 `detect_resonance`，**712/725 = 98.21% 逐字段复现**生产记录。
   剩下 13 份的偏差方向一致（生产看到的比快照多 1 条）——那是并发标的在
   `:334` 与 `:1161` 之间又挤掉了一条，属同一现象而非反例；主测量只取可复现的 712 份。
2. 快照必须是「实际发布集」的子多重集：**0/725 违反**（截断只会删，不会造）。
3. 第二个独立仪器：反解 `supporting_agents / consistency` == `len(快照)`，
   **712/725 一致**，与自证 1 命中同一批样本。

满编一轮是 **9 条**不是 8 条：`CodeExecutorAgent` 在 `analyze` 开头无条件发一条
`5.0 / neutral` 占位（`code_executor_agent.py:75`），随后才发真实结论
（生产快照里 592/745 份可见两条 `CodeExec`）。

实测（当前世代 = 30 只 watchlist，2026-08-24 起，n=191）
-------------------------------------------------------
条目层面丢 **628/1719 = 36.53%**，且**缺失与方向强相关**：

  · bullish 丢 74/588 = **12.6%**
  · bearish 丢 279/550 = **50.7%**   ← 看空/看多淘汰率之比 **4.0×**
  · neutral 丢 275/581 = 47.3%

后果不是「偶尔少数一票」，而是**共振结论本身被改写**：

  · 共振方向翻转 **29/191 = 15.2%**
  · `resonance_detected` 翻转 **40/191 = 20.9%**（生产判 81 次，真值 61 次）
  · `consistency` 被高估 113/191 = 59.2%，均值 **+0.301**、最大 +0.667
  · **39/191 = 20.4%** 的样本把 `consistency` 记成 **1.0（完全一致）**，
    而那一轮其实有条目被挤掉

最刺眼的一份 `analysis-AMC-ml-2026-09-03.json`：9 只蜂里 **5 只看空**
（Buzz / CodeExec / Chronos / Rival / Bear），板上只剩 Oracle / Scout / Guard
三条**全部看多** ⇒ 生产记成 `direction=bullish, consistency=1.0,
resonance_detected=True, confidence_boost=+15`，`final_score` 6.64。
无截断时应为 `direction=bearish`。

这正是 `swarm_agents/chronos_bee.py:405` 记的「信息素多5/空0 自我强化看多」——
淘汰键 `nlargest(MAX_ENTRIES, key=(self_score, support_count, pheromone_strength))`
**先扔分最低的**，而低分与看空/中性相关，于是板不只是丢数据，它**制造了
从未存在过的一致**。

契约边界
--------
本版只改 `detect_resonance`。`_entries` / `get_top_signals` / `snapshot` /
`compact_snapshot` 仍受截断，**`MAX_ENTRIES` 的值本版不动**（改它会一次性
改变每一个板消费方，其中 GuardBee 的 `get_top_signals(ticker, n=5)` 通道
在当前世代 **70/191 = 36.6%** 的标的上连 5 条都取不满——那条通道有独立的
测量与独立的世代边界，不该搭本版的车）。
"""

from datetime import datetime, timedelta

import pytest

from pheromone_board import PheromoneBoard, PheromoneEntry


# 一轮生产发布序列（按 alpha_hive_daily_report._analyze_single_ticker 的真实顺序）：
# Phase-1 并行 Scout/Oracle/Buzz/Chronos + CodeExecutor(占位→结论) → Rival → Guard → Bear
ROUND = [
    ("CodeExecutorAgent", "neutral", 5.0),      # code_executor_agent.py:75 无条件占位
    ("ScoutBeeNova", "bullish", 6.26),
    ("OracleBeeEcho", "bullish", 8.48),
    ("BuzzBeeWhisper", "bearish", 4.25),
    ("ChronosBeeHorizon", "bearish", 4.00),     # 「无近期催化剂」恒落 4.0，全板最低档
    ("CodeExecutorAgent", "bearish", 2.00),     # 真实结论
    ("RivalBeeVanguard", "bearish", 3.10),
    ("GuardBeeSentinel", "bullish", 7.30),
    ("BearBeeContrarian", "bearish", 3.50),
]


def _entry(agent, ticker="TEST", score=5.0, direction="neutral", ts=None):
    e = PheromoneEntry(
        agent_id=agent, ticker=ticker, discovery="x", source="test",
        self_score=score, direction=direction,
    )
    if ts is not None:
        e.timestamp = ts
    return e


@pytest.fixture
def board():
    return PheromoneBoard()


def _publish_round(board, ticker="TEST", seq=ROUND):
    for agent, direction, score in seq:
        board.publish(_entry(agent, ticker=ticker, score=score, direction=direction))


def _flood(board, score=7.0):
    """把板灌到 MAX_ENTRIES 溢出，复刻生产形状（30 只标的 × 9 条 ≈ 270 条）。

    ⚠️ 每条填充必须用**不同的 ticker**。`publish` 的衰减是 ticker-scoped
    （`if not self._ticker_scoped or e.ticker == entry.ticker`）—— 若填充全用
    同一个 ticker，它们会把彼此衰减到 `MIN_STRENGTH` 以下先行消失，板根本涨
    不到 MAX_ENTRIES，溢出淘汰不会触发，这一整组测试会**假绿**。
    v0.45.151 的第一版就栽在这里，故本文件同样配 `test_flood_helper_actually_evicts`
    做反向自证。

    默认分 7.0 刻意**居中**：ROUND 里 8.48/7.30 两条应当存活、其余被挤掉，
    复刻生产形状（TMUS 2026-09-04 满编 9 条只剩最高的 3 条）。若用 9.9 会把
    被测标的**整只**清空——那样 `detect_resonance` 走的是「无条目」分支，
    测的就不再是「谁被挤掉」而是「板空了」。
    """
    for i in range(PheromoneBoard.MAX_ENTRIES + 5):
        board.publish(_entry("Filler", ticker=f"FLOOD{i}", score=score))


# ══════════════════════════════════════════════════════════════════════════
# 夹具反向自证：若 _flood 没真的挤掉低分条目，下面每一条溢出测试都是假绿
# ══════════════════════════════════════════════════════════════════════════

def test_flood_helper_actually_evicts(board):
    _publish_round(board)
    before = len([e for e in board._entries if e.ticker == "TEST"])
    assert before == len(ROUND), f"未灌洪时本应 {len(ROUND)} 条全在，实为 {before}"
    _flood(board)
    after = len([e for e in board._entries if e.ticker == "TEST"])
    assert after < before, (
        "_flood 没有触发 MAX_ENTRIES 溢出淘汰 —— 本文件所有溢出测试都失去意义"
    )


def test_flood_evicts_low_scores_first(board):
    """自证之二：被挤掉的确实是**低分**那些（淘汰键的方向）。"""
    _publish_round(board)
    _flood(board)
    survived = {e.agent_id for e in board._entries if e.ticker == "TEST"}
    assert "OracleBeeEcho" in survived, "8.48 分的最高分条目不该被挤掉"
    assert "ChronosBeeHorizon" not in survived, "4.00 分的最低分条目本应先被挤掉"


# ══════════════════════════════════════════════════════════════════════════
# 主张：detect_resonance 不受 MAX_ENTRIES 溢出淘汰影响
# ══════════════════════════════════════════════════════════════════════════

def test_resonance_counts_evicted_low_score_bee(board):
    """被挤掉的低分蜂仍要进共振计数（生产 AMC 2026-09-03 的形状）。"""
    _publish_round(board)
    _flood(board)
    res = board.detect_resonance("TEST")
    # ROUND 满编：bearish 5（Buzz/Chronos/CodeExec/Rival/Bear）> bullish 3
    assert res["direction"] == "bearish", (
        f"5 空 / 3 多 的标的被读成 {res['direction']} —— 低分看空条目被挤掉了"
    )
    assert res["supporting_agents"] == 5


def test_resonance_does_not_fabricate_unanimity(board):
    """consistency 不得因截断被抬成 1.0（当前世代 20.4% 的样本就是这么来的）。"""
    _publish_round(board)
    _flood(board)
    res = board.detect_resonance("TEST")
    assert res["consistency"] < 1.0, "板上只剩同向条目就报「完全一致」＝伪造一致"
    assert res["consistency"] == pytest.approx(5 / 8, abs=0.001)


def test_resonance_identical_under_any_max_entries(monkeypatch, board):
    """核心不变式：同一发布序列下，共振结论与 MAX_ENTRIES 取值**无关**。

    这条同时挡住「把 MAX_ENTRIES 调大就算修好了」——调大只是让当前 watchlist
    恰好不溢出，标的数再涨一次就复发。
    """
    def verdict(max_entries):
        monkeypatch.setattr(PheromoneBoard, "MAX_ENTRIES", max_entries)
        b = PheromoneBoard()
        _publish_round(b)
        for i in range(max_entries + 5):
            b.publish(_entry("Filler", ticker=f"FLOOD{i}", score=9.9))
        return b.detect_resonance("TEST")

    tight, loose = verdict(3), verdict(4000)
    for k in ("resonance_detected", "direction", "supporting_agents",
              "cross_dim_count", "consistency", "confidence_boost"):
        assert tight[k] == loose[k], f"{k} 随 MAX_ENTRIES 变化：{tight[k]} vs {loose[k]}"


# ══════════════════════════════════════════════════════════════════════════
# 成对断言：防「一律返回全部」「永远返回点什么」
# ══════════════════════════════════════════════════════════════════════════

def test_never_published_agent_is_not_invented(board):
    """没发布过的蜂不得凭空出现（防止修法退化成「把所有蜂都算上」）。"""
    board.publish(_entry("ScoutBeeNova", score=6.0, direction="bullish"))
    board.publish(_entry("OracleBeeEcho", score=8.0, direction="bullish"))
    _flood(board)
    res = board.detect_resonance("TEST")
    assert res["supporting_agents"] == 2, "只发布了 2 条，不该算出更多"
    assert res["cross_dim_count"] == 2


def test_unknown_ticker_still_neutral(board):
    """完全没有条目的标的仍返回 neutral 空结果。"""
    _publish_round(board)
    _flood(board)
    res = board.detect_resonance("NOT_A_TICKER")
    assert res["resonance_detected"] is False
    assert res["direction"] == "neutral"
    assert res["supporting_agents"] == 0


def test_other_ticker_entries_do_not_leak(board):
    """抗淘汰视图必须仍按 ticker 隔离。"""
    _publish_round(board, ticker="AAA")
    _publish_round(board, ticker="BBB", seq=[("ScoutBeeNova", "bullish", 9.0)])
    _flood(board)
    res = board.detect_resonance("BBB")
    assert res["supporting_agents"] == 1
    assert res["direction"] == "bullish"


def test_same_agent_republish_keeps_latest_only(board):
    """同一只蜂重发以最新一条为准（CodeExecutor 的占位→结论就是这个形状）。"""
    board.publish(_entry("ScoutBeeNova", score=5.0, direction="neutral"))
    board.publish(_entry("ScoutBeeNova", score=6.0, direction="bullish"))
    _flood(board)
    res = board.detect_resonance("TEST")
    assert res["supporting_agents"] == 1, "同一只蜂两条不该各算一票"
    assert res["direction"] == "bullish", "应取最新那条的方向"


# ══════════════════════════════════════════════════════════════════════════
# 墙钟过期仍要拦（与 get_agent_entry 同口径）——成对：新鲜的要在，陈的要走
# ══════════════════════════════════════════════════════════════════════════

def test_stale_entry_excluded_from_resonance(board):
    """上一轮的陈货（>3600s）不进共振——那个判的是时效，该拦。"""
    # ⚠️ 必须放 **2** 条陈的看空：只放 1 条时「1 多 1 空」平局仍判 bullish，
    # 忘记做过期检查也照样绿 —— 断言就失去判别力。
    old = (datetime.now() - timedelta(seconds=PheromoneBoard._AGENT_ENTRY_MAX_AGE_S + 60)).isoformat()
    board.publish(_entry("ScoutBeeNova", score=6.0, direction="bearish", ts=old))
    board.publish(_entry("BuzzBeeWhisper", score=6.0, direction="bearish", ts=old))
    board.publish(_entry("OracleBeeEcho", score=8.0, direction="bullish"))
    _flood(board)
    res = board.detect_resonance("TEST")
    assert res["supporting_agents"] == 1, "两条陈货不该参与投票"
    assert res["direction"] == "bullish"
    assert res["consistency"] == pytest.approx(1.0, abs=0.001)


def test_fresh_entry_included_after_flood(board):
    """成对的另一半：新鲜条目即使被挤出 _entries 也必须在。"""
    board.publish(_entry("ScoutBeeNova", score=0.5, direction="bearish"))
    board.publish(_entry("OracleBeeEcho", score=8.0, direction="bullish"))
    _flood(board)
    res = board.detect_resonance("TEST")
    assert res["supporting_agents"] == 1
    assert res["direction"] == "bullish"
    # 0.5 分的看空条目必被挤出 _entries，但仍要计入分母
    assert res["consistency"] == pytest.approx(0.5, abs=0.001)


def test_unparseable_timestamp_excluded(board):
    """时间戳不可解析 → 视为过期（与 get_agent_entry / publish 存活检查一致）。"""
    board.publish(_entry("ScoutBeeNova", score=6.0, direction="bearish", ts="not-a-time"))
    board.publish(_entry("BuzzBeeWhisper", score=6.0, direction="bearish", ts="not-a-time"))
    board.publish(_entry("OracleBeeEcho", score=8.0, direction="bullish"))
    res = board.detect_resonance("TEST")
    assert res["supporting_agents"] == 1
    assert res["direction"] == "bullish"


def test_clear_resets_resonance(board):
    _publish_round(board)
    _flood(board)
    assert board.detect_resonance("TEST")["supporting_agents"] > 0
    board.clear()
    assert board.detect_resonance("TEST")["supporting_agents"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 未溢出时语义完全不变（回归护栏）
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("seq,expect_dir,expect_det", [
    ([("ScoutBeeNova", "bullish", 6.0), ("OracleBeeEcho", "bullish", 6.0),
      ("BuzzBeeWhisper", "bullish", 6.0)], "bullish", True),
    ([("ScoutBeeNova", "bullish", 6.0), ("OracleBeeEcho", "bullish", 6.0),
      ("BearBeeContrarian", "bullish", 6.0)], "bullish", False),   # contrarian 看多不计维
    ([("ScoutBeeNova", "bearish", 3.0), ("OracleBeeEcho", "bearish", 3.0),
      ("BearBeeContrarian", "bearish", 3.0)], "bearish", True),    # 看空时 contrarian 计维
])
def test_unflooded_semantics_unchanged(board, seq, expect_dir, expect_det):
    _publish_round(board, seq=seq)
    res = board.detect_resonance("TEST")
    assert res["direction"] == expect_dir
    assert res["resonance_detected"] is expect_det


class TestCohortBoundaryAppended:
    """改 detect_resonance ⇒ GuardBee 分与 QueenDistiller 的 confidence_boost 都变
    ⇒ final_score 变 ⇒ 必须追加世代边界。"""

    def test_v0_45_156_entry_present(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        hits = [c for c in _COHORT_HISTORY if c[1] == "v0.45.156"]
        assert len(hits) == 1, "v0.45.156 的世代边界未追加（或重复追加）"
        date, _version, reason = hits[0]
        assert date == "2026-09-07"
        assert "detect_resonance" in reason
        assert "MAX_ENTRIES" in reason

    def test_history_dates_monotonic(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        dates = [c[0] for c in _COHORT_HISTORY]
        assert dates == sorted(dates), "世代边界必须按日期单调追加"
