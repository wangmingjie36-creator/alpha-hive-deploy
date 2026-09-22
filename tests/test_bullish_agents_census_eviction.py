"""real_data_sources.get_bullish_agents_count 把「排行榜」当「普查」用（v0.45.279，P2）

背景
----
v0.45.163 修 GuardBee 的 `consistency`/`avg_score` 时已经点名过本仓另外 3 个还在用
`get_top_signals`（排行榜、受 `MAX_ENTRIES` 溢出淘汰影响）的调用方，其中一个正是
`real_data_sources.py:261`（当时的行号）——`get_bullish_agents_count(ticker, board)`，
Scout `crowding.comp.consensus_strength` 的唯一数据来源（`(bullish_agents/6)*100`）。
那次备注是「各自有独立的量测，不搭本版的车」，之后一直没人接手。

为什么同一缺陷值得在这里重复量测（而不是引用 v0.45.163 就算数）
----------------------------------------------------------------
`get_top_signals` 的排行榜/淘汰语义是共享的，但**消费方式不同**：GuardBee 用它算
均值与一致性，本文件的 `get_bullish_agents_count` 只是数「方向==bullish 的条数」，
且**从不按身份过滤**——阈值、分母、失败兜底都要单独钉，不能只搬结论。

三个独立缺陷
------------
① **溢出淘汰**：`_entries` 满时按 `nlargest(MAX_ENTRIES, key=(self_score,...))` 截断，
   先扔分最低的 ⇒ 缺失与被测量的量反相关（低分/看空先消失，与 v0.45.156/163 同形）。
② **无身份过滤**：`get_top_signals(ticker, n=10)` 数的是"板上这个 ticker 强度前 10 的
   条目"，不区分是不是那 4 个真正该数的 Phase-1 同伴
   （`signal_archive._PHASE1_DIRS` 排除 ScoutBeeNova 后剩 OracleBeeEcho /
   BuzzBeeWhisper / ChronosBeeHorizon / CodeExecutorAgent）——同一天万一有别的
   agent_id 占了这个 ticker 的高强度位，会被误记成一票。
③ **哨兵语义**：`board is None` 或任何异常时硬编码返回 `3`（"6 个里 3 个看多"），
   从未产生 `None`。v0.45.50 已经给 `crowding_detector` 修好「缺失分量给 None、
   不参与合成」的通路，但对这个分量从来没生效过——它的输入永远是个合法数字。

量测局限（如实记录，不假装测过）
--------------------------------
这是 race-condition 依赖的读取：生产没有任何快照能重建"Scout 读板那一刻板上
到底有哪些条目"（`pheromone_compact` 是 Queen distill 时才拍的全局终态，晚于
Scout 的读取）。本文件只能证明**机制存在、会产生错误结果**，不能像 v0.45.163
那样给出生产历史的翻转率——那个数字不存在，不编。
"""

import pytest

from pheromone_board import PheromoneBoard, PheromoneEntry
from real_data_sources import get_bullish_agents_count, _PHASE1_PEERS


def _entry(agent, ticker="TEST", score=5.0, direction="neutral"):
    return PheromoneEntry(
        agent_id=agent, ticker=ticker, discovery="x", source="test",
        self_score=score, direction=direction,
    )


@pytest.fixture
def board():
    b = PheromoneBoard()
    yield b
    b.clear()


# Scout 自己不算在内——signal_archive._PHASE1_DIRS 的口径。
PEERS_ROUND = [
    ("OracleBeeEcho",     "bullish", 8.48),   # 高分，洪水后应存活
    ("BuzzBeeWhisper",    "bullish", 4.25),   # 低分，洪水后应被挤掉
    ("ChronosBeeHorizon", "bearish", 4.00),
    ("CodeExecutorAgent", "neutral", 5.00),
]
TRUE_BULLISH_COUNT = 2   # Oracle + Buzz


def _publish_peers(board, ticker="TEST"):
    for agent, direction, score in PEERS_ROUND:
        board.publish(_entry(agent, ticker=ticker, score=score, direction=direction))


def _flood(board, score=6.0):
    """灌到 MAX_ENTRIES 溢出，复刻生产形状（同源手法见 test_guard_census_eviction.py）。

    6.0 刻意介于 Buzz 的 4.25/Chronos 的 4.00 与 Oracle 的 8.48 之间：
    Oracle 应存活、Buzz 应被挤掉，测的是「谁被挤掉」而不是「板被清空」。
    """
    for i in range(PheromoneBoard.MAX_ENTRIES + 5):
        board.publish(_entry("Filler", ticker=f"FLOOD{i}", score=score))


# ══════════════════════════════════════════════════════════════════════════
# 夹具反向自证：_flood 没真挤掉低分条目的话，下面的溢出测试全部假绿
# ══════════════════════════════════════════════════════════════════════════

def test_flood_helper_actually_evicts(board):
    _publish_peers(board)
    before = len([e for e in board._entries if e.ticker == "TEST"])
    assert before == len(PEERS_ROUND)
    _flood(board)
    after = len([e for e in board._entries if e.ticker == "TEST"])
    assert after < before, "_flood 未触发 MAX_ENTRIES 溢出淘汰 —— 本文件溢出测试全部失去意义"


def test_flood_evicts_the_low_scoring_bullish_entry(board):
    _publish_peers(board)
    _flood(board)
    survived = {e.agent_id for e in board._entries if e.ticker == "TEST"}
    assert "OracleBeeEcho" in survived, "8.48 分不该被挤掉"
    assert "BuzzBeeWhisper" not in survived, "4.25 分的看多条目本应先被挤掉"


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 ①：溢出淘汰 ⇒ 排行榜口径的计数被腰斩
# ══════════════════════════════════════════════════════════════════════════

class TestLeaderboardUndercountsAfterEviction:

    def test_top_signals_undercounts_after_eviction(self, board):
        """成对断言的第一半：证明现象存在于 `get_top_signals` 本身
        （与 `get_bullish_agents_count` 具体怎么实现无关）。"""
        _publish_peers(board)
        _flood(board)
        counted = sum(1 for e in board.get_top_signals("TEST", n=10)
                      if e.direction == "bullish")
        assert counted < TRUE_BULLISH_COUNT, (
            f"洪水后排行榜应该只剩 1 个看多（Oracle），实数到 {counted} —— "
            "夹具没有复现淘汰，下面对 get_bullish_agents_count 的断言就立不住")

    def test_live_signals_survives_eviction(self, board):
        """成对的另一半：普查视图不受影响 —— 证明修复方向是对的，不只是「现在这样不好」。"""
        _publish_peers(board)
        _flood(board)
        counted = sum(1 for e in board.get_live_signals("TEST")
                      if e.direction == "bullish")
        assert counted == TRUE_BULLISH_COUNT

    def test_get_bullish_agents_count_undercounts_after_eviction(self, board):
        """端到端：真正的调用入口在洪水后应该数错——这条锁定「修复前会红」，
        修复后（改用 get_live_signals）应转绿，下面 TestFixedCensusImplementation
        里有它的镜像断言。"""
        _publish_peers(board)
        _flood(board)
        got = get_bullish_agents_count("TEST", board)
        assert got == TRUE_BULLISH_COUNT, (
            f"get_bullish_agents_count 在洪水后应仍能数对 {TRUE_BULLISH_COUNT}"
            f"（普查口径），实得 {got}（排行榜口径漏掉了被挤掉的 Buzz）")


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 ②：无身份过滤 ⇒ 非 Phase-1 同伴的条目也会被数进去
# ══════════════════════════════════════════════════════════════════════════

class TestIdentityScoping:

    def test_non_phase1_peer_is_not_counted(self, board):
        """板上若混进一条不属于「4 个 Phase-1 同伴」的高分看多条目
        （例如同一 ticker 上一轮遗留、或身份判断失手），不得被计入。
        `signal_archive._PHASE1_DIRS` 排除 ScoutBeeNova 后就是这 4 个，
        RivalBeeVanguard／GuardBeeSentinel／BearBeeContrarian／ScoutBeeNova 自己
        都不该被数。"""
        _publish_peers(board)
        board.publish(_entry("RivalBeeVanguard", direction="bullish", score=9.0))
        board.publish(_entry("ScoutBeeNova", direction="bullish", score=9.0))
        got = get_bullish_agents_count("TEST", board)
        assert got == TRUE_BULLISH_COUNT, (
            f"应只数 4 个 Phase-1 同伴，实得 {got}（混进了非同伴条目）")


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 ③：哨兵语义 —— 缺失应诚实吐 None，不得编一个「3」
# ══════════════════════════════════════════════════════════════════════════

class TestHonestSentinel:

    def test_board_is_none_returns_none(self):
        assert get_bullish_agents_count("TEST", board=None) is None, (
            "board=None 应诚实返回 None（走 crowding_detector 的缺失分量重归一化通路），"
            "不得编一个 3（'6 个里 3 个看多' 是假读数，不是观测）")

    def test_no_entries_for_ticker_is_a_real_zero_not_none(self, board):
        """成对断言：跟上一条区分开——「板存在但这个 ticker 真的一个同伴都没发布」
        是一个真实的 0，不是缺失。别把两种「没有」混成同一个 None。"""
        assert get_bullish_agents_count("NOSUCH", board) == 0


# ══════════════════════════════════════════════════════════════════════════
# 阶段 3：口径标记（caliber marker）—— 记录读板那一刻实际数到的是谁
# ══════════════════════════════════════════════════════════════════════════
#
# 不是为了替代 SIGNAL_UPSTREAM 里 consensus_strength 依赖 phase1 方向的那条边
# （那条边结构上是对的，"3/4 看多"的含义确实会随同伴的方向定义改变而改变）。
# 这里只是让「当时到底数到了谁」变成可核查的观测量，类似 census_source /
# iv_rank_source 的精神——以后要重新评估这条边的成本收益，不用再翻代码猜。

from real_data_sources import get_bullish_agents_detail, get_real_crowding_metrics


class TestConsensusCensusDetail:

    def test_detail_lists_live_and_bullish_peers_separately(self, board):
        _publish_peers(board)
        d = get_bullish_agents_detail("TEST", board)
        assert d["peers_live"] == sorted(a for a, _, _ in PEERS_ROUND)
        assert d["peers_bullish"] == sorted(
            a for a, direction, _ in PEERS_ROUND if direction == "bullish")

    def test_detail_survives_eviction_like_the_count_does(self, board):
        _publish_peers(board)
        _flood(board)
        d = get_bullish_agents_detail("TEST", board)
        assert d["peers_bullish"] == ["BuzzBeeWhisper", "OracleBeeEcho"]

    def test_detail_is_none_when_board_is_none(self):
        assert get_bullish_agents_detail("TEST", board=None) is None

    def test_count_and_detail_agree(self, board):
        """两个函数不能各说各话——detail 的 peers_bullish 数量必须等于 count。"""
        _publish_peers(board)
        d = get_bullish_agents_detail("TEST", board)
        assert len(d["peers_bullish"]) == get_bullish_agents_count("TEST", board)


# ══════════════════════════════════════════════════════════════════════════
# 二次检查（2026-09-22）补的测试缺口 ①：`get_bullish_agents_detail` 的
# except 分支此前零测试——`TestHonestSentinel` 只测了 `board is None`，
# 没有任何测试真正让 `board.get_live_signals(...)` 抛异常。
# ══════════════════════════════════════════════════════════════════════════

class TestDetailExceptionPathReturnsNone:
    """`except (ValueError, KeyError, TypeError, AttributeError)` 这条白名单本身
    也要钉住——既不能收窄（该接住的没接住，board 抖一下就把整轮扫描炸了），
    也不能悄悄放宽（吞掉不该吞的异常，把真正的编程错误变成「诚实缺失」）。"""

    class _BoardRaises:
        def __init__(self, exc):
            self._exc = exc

        def get_live_signals(self, ticker):
            raise self._exc

    @pytest.mark.parametrize("exc", [ValueError("x"), KeyError("x"),
                                     TypeError("x"), AttributeError("x")])
    def test_whitelisted_exception_yields_none(self, exc):
        got = get_bullish_agents_detail("TEST", self._BoardRaises(exc))
        assert got is None, f"{type(exc).__name__} 应被接住、诚实返回 None"

    def test_whitelisted_exception_also_makes_count_none(self):
        """`get_bullish_agents_count` 转调 `get_bullish_agents_detail`——
        detail 是 None 时 count 不得编一个数字出来。"""
        got = get_bullish_agents_count("TEST", self._BoardRaises(TypeError("x")))
        assert got is None

    def test_unlisted_exception_is_not_swallowed(self):
        """白名单没写的类型必须冒出去——不是本次收窄，是与
        `get_bullish_agents_count` 旧实现同一份异常元组，钉住防止以后
        「顺手」扩成裸 `except Exception` 悄悄吞掉编程错误。"""
        with pytest.raises(RuntimeError):
            get_bullish_agents_detail("TEST", self._BoardRaises(RuntimeError("boom")))


class TestCrowdingMetricsExposesConsensusCensus:
    """`get_real_crowding_metrics` 是 Scout/Guard/Rival 共用的入口——本节验证
    它把 detail 透传出来。

    ⚠️ 2026-09-22 更正：本 docstring 原写「不改变既有的 bullish_agents 数值语义」，
    这只对 **Scout 自己**成立。`_PHASE1_PEERS` 是按「Scout 不数自己」设计的
    （见 `real_data_sources.py` 该常量上方注释），但 Rival（Phase-1.4，此时
    Scout 已发布）与 Guard 共用同一入口——旧的 `get_top_signals(ticker, n=10)`
    不按身份过滤，Scout 自己的条目若分数够高本可能被数进去；新口径按
    `_PHASE1_PEERS` 精确过滤后**结构上不可能**再数到 Scout。对 Rival/Guard
    的读者，这确实是数值语义的改变，不只是「透传一个新字段」。复现与量级见
    `TestRivalConsumptionSemanticsChanged`。是否要让 Rival/Guard 的口径改成
    「排除调用者自己」而非固定排除 Scout，是尚未做的设计决定，本次未改代码。"""

    def test_metrics_carries_consensus_census(self, board, monkeypatch):
        monkeypatch.setattr("real_data_sources.get_social_buzz",
                            lambda ticker: {"messages_per_day": 100, "data_quality": "real"})
        monkeypatch.setattr("real_data_sources.get_short_interest",
                            lambda ticker: {"short_pct_float": 0.05, "data_quality": "real"})
        _publish_peers(board)
        metrics = get_real_crowding_metrics("TEST", {"price": 100.0}, board)
        assert metrics["bullish_agents"] == TRUE_BULLISH_COUNT
        assert metrics["consensus_census"]["peers_bullish"] == sorted(
            a for a, direction, _ in PEERS_ROUND if direction == "bullish")

    def test_data_quality_label_is_honest_about_none(self, monkeypatch):
        """v0.45.279 顺带修复：此前 `"real" if board else "default"` 只看
        board 是不是传了，不看真读出来的值是不是 None——board 传了但读取异常时，
        标签仍会自称 "real"，正是 momentum 那条旁边注释点名过的反面写法。"""
        monkeypatch.setattr("real_data_sources.get_social_buzz",
                            lambda ticker: {"messages_per_day": 100, "data_quality": "real"})
        monkeypatch.setattr("real_data_sources.get_short_interest",
                            lambda ticker: {"short_pct_float": 0.05, "data_quality": "real"})
        monkeypatch.setattr("real_data_sources.get_bullish_agents_count",
                            lambda ticker, board: None)
        monkeypatch.setattr("real_data_sources.get_bullish_agents_detail",
                            lambda ticker, board: None)
        metrics = get_real_crowding_metrics("TEST", {"price": 100.0}, board=object())
        assert metrics["bullish_agents"] is None
        assert metrics["data_quality"]["bullish_agents"] == "unavailable", (
            "board 传了但读数是 None 时，质量标签不该继续自称 real")


# ══════════════════════════════════════════════════════════════════════════
# 二次检查（2026-09-22）：更正上面 `TestCrowdingMetricsExposesConsensusCensus`
# 旧 docstring「不改变既有的 bullish_agents 数值语义」——这句话只对 Scout 自己
# 成立，对 Rival/Guard 这两个事后读者不成立，见下方复现。
# ══════════════════════════════════════════════════════════════════════════

class TestRivalConsumptionSemanticsChanged:
    """Rival（Phase-1.4）与 Guard 读 `get_real_crowding_metrics` 时，Scout 早已
    发布过自己的条目。旧口径（`get_top_signals(ticker, n=10)`，无身份过滤）
    可能把 Scout 自己的票也数进去；新口径（`_PHASE1_PEERS`）结构上排除它。

    这不是回归：`_PHASE1_PEERS` 一直是按「Scout 不该数自己」设计的
    （`real_data_sources.py` 该常量上方注释）。但对 Rival/Guard 这两个
    "事后"读者而言，同一份板面此前可能数进 Scout 的票、现在结构上不可能——
    得到的数字确实变了，不只是「多透传一个字段」。要不要把 Rival/Guard 的
    口径改成「排除调用者自己」而非固定排除 Scout，是尚未做的设计决定。
    """

    def test_scout_own_vote_no_longer_counted_by_a_downstream_reader(self, board):
        assert "ScoutBeeNova" not in _PHASE1_PEERS, (
            "_PHASE1_PEERS 排除 ScoutBeeNova 是本节其余断言的前提")
        for agent, direction in (("ScoutBeeNova", "bullish"), ("OracleBeeEcho", "bullish"),
                                  ("BuzzBeeWhisper", "bearish"), ("ChronosBeeHorizon", "neutral"),
                                  ("CodeExecutorAgent", "bullish")):
            board.publish(_entry(agent, direction=direction))
        old_view = sum(1 for e in board.get_top_signals("TEST", n=10) if e.direction == "bullish")
        new_view = get_bullish_agents_count("TEST", board)
        assert old_view == 3, "旧口径（无身份过滤）应数到 Scout+Oracle+CodeExec 三票看多"
        assert new_view == 2, "新口径（_PHASE1_PEERS）应只数到 Oracle+CodeExec——Scout 结构上被排除"
        assert old_view != new_view, (
            "Rival/Guard 读到的看多蜂数在这个场景下确实变了，"
            "不是「行为不变、只是多了一个字段」那么简单")


# ══════════════════════════════════════════════════════════════════════════
# 二次检查（2026-09-22）补的测试缺口 ②：`ScoutBeeNova.details.consensus_census`
# 的接线此前零测试——静态契约测试（test_bee_details_contract.py）只能证明
# `AgentResult(details={...})` 字面量里有这个键名，证明不了值取的是
# `metrics.get("consensus_census")` 而不是别的东西（例如打错键名、被别的
# 值覆盖）。真发生过：变异「读错键名」在改前全绿，是本次审计才发现的缺口。
# ══════════════════════════════════════════════════════════════════════════

from swarm_agents.scout_bee import ScoutBeeNova  # noqa: E402


def _fake_crowding_metrics(consensus_census):
    """`get_real_crowding_metrics` 的最小合法返回形状——够 `CrowdingDetector.
    calculate_crowding_score` 跑完，不触发任何 None 分量。"""
    return {
        "social_messages_per_day": 100.0, "google_trends_percentile": 50.0,
        "bullish_agents": 1, "consensus_census": consensus_census,
        "seeking_alpha_page_views": 1000.0, "short_float_ratio": 0.05,
        "price_momentum_5d": 1.0,
        "data_quality": {"social_buzz": "real", "google_trends": "proxy_volume",
                          "bullish_agents": "real", "seeking_alpha": "proxy_social",
                          "short_interest": "real", "momentum": "real"},
    }


class TestScoutWiresConsensusCensusIntoDetails:
    """离线跑真实 `ScoutBeeNova.analyze()`，只钉住重量级外部依赖（SEC/国会/供应链/
    板块相对强弱），验证 `metrics.get("consensus_census")` 原样传到了
    `AgentResult.details["consensus_census"]`——而不是只验证键名存在。"""

    @pytest.fixture(autouse=True)
    def _offline_sources(self, monkeypatch):
        import sec_edgar
        import edgar_rss
        import congress_trades_scraper
        import market_intelligence

        class _StubSEC:
            _cik_map: dict = {}

        monkeypatch.setattr(sec_edgar, "get_insider_trades", lambda ticker, days=90: None)
        monkeypatch.setattr(sec_edgar, "SECEdgarClient", _StubSEC)
        monkeypatch.setattr(edgar_rss, "get_today_form4_alerts",
                            lambda ticker, cik=None: {"has_fresh_filings": False})
        monkeypatch.setattr(congress_trades_scraper, "get_congress_trades_for_ticker",
                            lambda ticker, days_back=90: {})
        monkeypatch.setattr(market_intelligence, "get_supply_chain_signals", lambda ticker: {})

    def _bee(self, monkeypatch, metrics):
        bee = ScoutBeeNova(PheromoneBoard())
        monkeypatch.setattr(bee, "_validate_ticker", lambda t: None)
        monkeypatch.setattr(bee, "_get_history_context", lambda t: "")
        monkeypatch.setattr(bee, "_get_stock_data", lambda t: {
            "price": 100.0, "momentum_5d": 1.0, "volume_ratio": 1.0, "volatility_20d": 20.0})
        monkeypatch.setattr(bee, "_assess_sector_relative_strength", lambda t: {"rs_signal": "unknown"})
        monkeypatch.setattr(bee, "_publish", lambda *a, **k: None)
        monkeypatch.setattr("real_data_sources.get_real_crowding_metrics",
                            lambda ticker, stock, board: metrics)
        return bee

    def test_consensus_census_reaches_details_unchanged(self, monkeypatch):
        sentinel = {"peers_live": ["OracleBeeEcho"], "peers_bullish": ["OracleBeeEcho"],
                    "_sentinel": "not-a-real-shape"}
        bee = self._bee(monkeypatch, _fake_crowding_metrics(sentinel))
        result = bee.analyze("TEST")
        assert result["details"]["consensus_census"] == sentinel, (
            "details.consensus_census 应原样等于 get_real_crowding_metrics 返回的值——"
            "不是键名恰好存在就算数")

    def test_none_census_reaches_details_as_none_not_dropped(self, monkeypatch):
        """成对：board 缺失/读取异常时 `consensus_census` 是 None——键必须还在，
        不是被悄悄丢掉（那会让 None 和"从未接线"混成同一种「读不到」）。"""
        bee = self._bee(monkeypatch, _fake_crowding_metrics(None))
        result = bee.analyze("TEST")
        assert "consensus_census" in result["details"]
        assert result["details"]["consensus_census"] is None
