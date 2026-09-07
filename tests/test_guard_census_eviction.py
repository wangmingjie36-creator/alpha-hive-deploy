"""GuardBee 把「排行榜」当「普查」用：get_top_signals(n=5) 双重截断（v0.45.163）

背景（全部为生产 `analysis-*-ml-*.json` 实测，非推断）
------------------------------------------------------
`guard_bee.py` 从 `board.get_top_signals(ticker, n=5)` **同时**导出两个量：
`avg_score`（窗口内 self_score 均值）与 `consistency`（`max(bull,bear)/total`），
二者直接决定 risk_adj 维分——共振时 `7.0 + consistency*2.0`，否则
`avg_score * 0.8 * adj_factor`——而 risk_adj 在 `config.EVALUATION_WEIGHTS` 里
是加权维度（实测权重 ≈0.20），因此流进 `final_score`。
`dimension_scores.risk_adj == agent_details.GuardBeeSentinel.score`，193/193 逐份相等。

窗口大小是**直接观测量**：`guard_bee.py:214` 自己把 `len(top_signals)` 写进
`details.top_signals_count`，不需要模拟。仪器自证：用它 + `consistency` +
`adjustment_factor` + 落盘的全部下游修正（macro_adj / regime / opex /
alpha_decay / llm_conflict_type）逐份重算 GuardBee.score，**191/191 命中**。
第二条自证：`agent_details[蜂].score` 与 `pheromone_compact` 的 `s` 字段比对，
780 对上、16 对不上——16 条**全部**是 CodeExecutorAgent，且形状一致
（落盘 3.0/4.5，快照里只剩 5.0 占位）⇒ 0 条无法解释。

实测（当前世代 = 30 只 watchlist，2026-08-24 起，n=191）
-------------------------------------------------------
Guard 之前发布过的蜂**恒为 6 只**（Phase-1 的 Scout/Oracle/Buzz/Chronos/CodeExec
＋ Phase-1.4 的 Rival，见 `alpha_hive_daily_report.py:442` 的顺序契约），
而 `n=5` 的窗口 **191/191 = 100%** 装不下这 6 只。其中：

  · 窗口连 5 条都取不满：**91/191 = 47.6%**（旧世代 ≤16 只标的时只有 1.8%）
  · 窗口条数分布：1 条 10 份 / 2 条 21 / 3 条 24 / 4 条 36 / 5 条 100

两个缺陷叠在一起，分层后各自可见（Δ = 反事实 − 生产）：

  n=1  10 份  Δ risk_adj **−1.480**   avg_score 被高估 +1.542
  n=2  21 份  Δ risk_adj −1.090       +1.174
  n=3  24 份  Δ risk_adj −0.804       +1.095
  n=4  36 份  Δ risk_adj −0.565       +0.812
  n=5 100 份  Δ risk_adj −0.124       +0.260   ← 窗口填满仍有偏

① **溢出淘汰**：`nlargest(MAX_ENTRIES, key=(self_score, ...))` **先扔分最低的**
   ⇒ 幸存者均值**按构造**偏高，剂量-反应单调（窗口越小偏得越多）。
② **API 口径错配**：`get_top_signals` 是**排行榜**（按 pheromone_strength 取前 N），
   GuardBee 要的是**普查**（均分、方向一致性）。对排行榜取均值不等于对蜂群取均值，
   所以即使板没溢出，n=5 也永远看不全 6 只蜂（n=5 那层 70/100 仍偏高）。

合计：risk_adj 维分 **185/191 = 96.9%** 与反事实不同，Δ 均值 **−0.485**
（生产偏高 160 份 / 偏低 25 份），|Δ|≥1.0 有 27 份、≥2.0 有 3 份。
GuardBee 方向 **50/191 = 26.2%** 不一致，bearish 32→**63**（几乎翻倍）。
`consistency` 被高估 129/191 = 67.5%（均值 +0.116）；生产记成
**1.0「完全一致」的 35 份，反事实无一为 1.0** —— 与 v0.45.156 同一形状：
板不只是丢数据，它**制造了从未存在过的一致**。
传导到 `final_score`（Δrisk_adj × risk_adj 权重）：|Δ| 均值 0.089，
67/191 份 ≥0.1，6 份 ≥0.3，最大 0.404。

顺带一条同源子通道：`guard_bee.py:52` 把窗口里的 `bull` 覆盖进
`real_metrics["bullish_agents"]`，而 `crowding_detector.py:84` 算的是
`bullish_agents / 6 * 100` —— 分母写死 6，分子却被窗口截断到 ≤n。
本版改普查后该分子自动回到真值，无需另改。

契约边界
--------
本版只改 GuardBee 这一条通道（新增 `get_live_signals` 普查读法）。
`get_top_signals` 的排行榜语义**不动**：它另有 3 个调用方
（`bear_bee.py:39/512` n=20、`real_data_sources.py:261` n=10、
`base.py:85` 仅作 `get_agent_entry` 的回退），各自有独立的量测，不搭本版的车。
`snapshot` / `compact_snapshot` 仍受截断，见 CHANGELOG v0.45.163 的独立量测。
"""

import pytest

from pheromone_board import PheromoneBoard, PheromoneEntry


# `alpha_hive_daily_report.py:442` 的顺序契约：Phase-1 并行 → Rival → Guard → Bear。
# 因此 Guard 读板时，板上恰好是下列 6 只蜂（CodeExec 两条：占位 + 真实结论）。
PRE_GUARD_ROUND = [
    ("CodeExecutorAgent", "neutral", 5.0),      # code_executor_agent.py:75 无条件占位
    ("ScoutBeeNova",      "bullish", 6.26),
    ("OracleBeeEcho",     "bullish", 8.48),
    ("BuzzBeeWhisper",    "bearish", 4.25),
    ("ChronosBeeHorizon", "bearish", 4.00),     # 「无近期催化剂」恒落 4.0，全板最低档
    ("CodeExecutorAgent", "bearish", 2.00),     # 真实结论（比占位低 3 分 ⇒ 先被挤掉）
    ("RivalBeeVanguard",  "bearish", 3.10),
]

# 普查真值：每蜂最新一条 ⇒ CodeExec 只算 2.00 那条
CENSUS_TRUTH = {
    "CodeExecutorAgent": ("bearish", 2.00),
    "ScoutBeeNova":      ("bullish", 6.26),
    "OracleBeeEcho":     ("bullish", 8.48),
    "BuzzBeeWhisper":    ("bearish", 4.25),
    "ChronosBeeHorizon": ("bearish", 4.00),
    "RivalBeeVanguard":  ("bearish", 3.10),
}
# 4 空 / 2 多 ⇒ 方向 bearish，一致性 4/6
TRUE_CONSISTENCY = 4 / 6
TRUE_AVG = sum(s for _, s in CENSUS_TRUTH.values()) / len(CENSUS_TRUTH)


@pytest.fixture(autouse=True)
def _offline_sources(stub_yfinance, stub_reddit, stub_http_gate,
                     stub_cboe_payload, stub_cboe_vix):
    """本文件跑 GuardBee.analyze() 全链（拥挤度 / 宏观 / market_intelligence），
    每个外部源在源头钉成它自己的「取不到」契约，而不是靠 `_offline_transport`
    在传输层兜底。"""


@pytest.fixture
def stock_stub(monkeypatch):
    """`swarm_agents.cache._fetch_stock_data` 的替身。

    ⚠️ 签名必须是 `(ticker, target_date=None)`：`base._get_stock_data` 在
    prefetch miss 时透传目标日期（v0.43.28）。conftest 的 `mock_stock_data`
    只收 1 个参数，会让 GuardBee 的拥挤度分支整段抛 TypeError 被 except 吞掉
    —— 那样 `test_guard_passes_true_bull_count_to_crowding` 永远到不了被测代码。
    """
    data = {"price": 100.0, "volume": 1_000_000, "avg_volume": 900_000,
            "market_cap": 1e11, "shortPercentOfFloat": 0.03,
            "history": [], "info": {}}
    from swarm_agents import cache as _swarm_cache
    monkeypatch.setattr(_swarm_cache, "_fetch_stock_data",
                        lambda ticker, target_date=None: dict(data))
    return data


@pytest.fixture
def no_resonance(monkeypatch):
    """把共振钉成「未触发」，隔离出 `avg_score * 0.8 * adj_factor` 那条分支。

    共振分支本身是 v0.45.156 的通道（`detect_resonance` 已改读 `_live_agent_entries`），
    不在本版契约内；不钉住它，走哪条分支就取决于夹具凑巧的方向分布。
    """
    monkeypatch.setattr(
        PheromoneBoard, "detect_resonance",
        lambda self, ticker: {
            "resonance_detected": False, "direction": "neutral",
            "supporting_agents": 0, "cross_dim_count": 0, "consistency": 0.0,
            "resonant_dimensions": [], "confidence_boost": 0,
        },
    )


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


def _publish_pre_guard(board, ticker="TEST"):
    for agent, direction, score in PRE_GUARD_ROUND:
        board.publish(_entry(agent, ticker=ticker, score=score, direction=direction))


def _flood(board, score=7.0):
    """把板灌到 MAX_ENTRIES 溢出，复刻生产形状（30 只标的 × 9 条 ≈ 270 条）。

    ⚠️ 每条填充必须用**不同的 ticker**。`publish` 的衰减是 ticker-scoped
    （`if not self._ticker_scoped or e.ticker == entry.ticker`）—— 填充若全用
    同一个 ticker，它们会把彼此衰减到 `MIN_STRENGTH` 以下先行消失，板根本涨不到
    MAX_ENTRIES，溢出淘汰不触发，整组测试**假绿**。故配 `test_flood_*` 反向自证。

    7.0 刻意居中：PRE_GUARD_ROUND 里 8.48 应存活、其余多数被挤掉，
    而不是把被测标的**整只**清空（那样测的就是「板空了」而非「谁被挤掉」）。
    """
    for i in range(PheromoneBoard.MAX_ENTRIES + 5):
        board.publish(_entry("Filler", ticker=f"FLOOD{i}", score=score))


# ══════════════════════════════════════════════════════════════════════════
# 夹具反向自证：_flood 没真挤掉低分条目的话，下面每一条溢出测试都是假绿
# ══════════════════════════════════════════════════════════════════════════

def test_flood_helper_actually_evicts(board):
    _publish_pre_guard(board)
    before = len([e for e in board._entries if e.ticker == "TEST"])
    assert before == len(PRE_GUARD_ROUND), \
        f"未灌洪时本应 {len(PRE_GUARD_ROUND)} 条全在，实为 {before}"
    _flood(board)
    after = len([e for e in board._entries if e.ticker == "TEST"])
    assert after < before, "_flood 未触发 MAX_ENTRIES 溢出淘汰 —— 本文件溢出测试全部失去意义"


def test_flood_evicts_low_scores_first(board):
    """自证之二：被挤掉的确实是低分那些（淘汰键的方向）。"""
    _publish_pre_guard(board)
    _flood(board)
    survived = {e.agent_id for e in board._entries if e.ticker == "TEST"}
    assert "OracleBeeEcho" in survived, "8.48 分的最高分条目不该被挤掉"
    assert "ChronosBeeHorizon" not in survived, "4.00 分的最低分条目本应先被挤掉"


def test_eviction_keeps_stub_and_drops_real_verdict(board):
    """自证之三：CodeExec 的 5.0 占位比 2.00 真实结论分高 ⇒ 淘汰键留下的是**桩**。

    生产 16/796 条 `agent_details.score` 与快照 `s` 对不上，全部是这个形状。

    填充分取 4.5（介于 2.00 与 5.0 之间）才复刻得出这个形状：默认的 7.0 会把
    CodeExec **两条都**挤掉，测到的就成了「这只蜂整个消失」而非「留下的是桩」。
    """
    _publish_pre_guard(board)
    _flood(board, score=4.5)
    ce = [e for e in board._entries if e.ticker == "TEST" and e.agent_id == "CodeExecutorAgent"]
    assert [e.self_score for e in ce] == [5.0], \
        f"本应只剩 5.0 占位、2.00 真实结论被挤掉，实为 {[e.self_score for e in ce]}"


# ══════════════════════════════════════════════════════════════════════════
# 主张 1：板需要一个「普查」读法，不受 MAX_ENTRIES 溢出淘汰影响
# ══════════════════════════════════════════════════════════════════════════

def test_get_live_signals_survives_eviction(board):
    _publish_pre_guard(board)
    _flood(board)
    got = {e.agent_id for e in board.get_live_signals("TEST")}
    assert got == set(CENSUS_TRUTH), f"普查视图漏了 {set(CENSUS_TRUTH) - got}"


def test_get_live_signals_collapses_duplicate_publishes(board):
    """同一只蜂发两条只算**最新**那条（CodeExec 的 5.0 占位不得当成一票）。"""
    _publish_pre_guard(board)
    got = {e.agent_id: e.self_score for e in board.get_live_signals("TEST")}
    assert len(got) == len(CENSUS_TRUTH)
    assert got["CodeExecutorAgent"] == 2.00, \
        f"CodeExec 应取真实结论 2.00，实为 {got['CodeExecutorAgent']}（占位 5.0 混进普查）"


def test_get_live_signals_is_ticker_scoped(board):
    _publish_pre_guard(board, ticker="TEST")
    _publish_pre_guard(board, ticker="OTHER")
    assert all(e.ticker == "TEST" for e in board.get_live_signals("TEST"))
    assert board.get_live_signals("NOSUCH") == []


def test_get_live_signals_identical_under_any_max_entries(monkeypatch, board):
    """核心不变式：同一发布序列下，普查结论与 MAX_ENTRIES 取值**无关**。

    这条同时挡住「把 MAX_ENTRIES 调大就算修好了」—— 调大只是让当前 watchlist
    恰好不溢出，标的数再涨一次就复发。
    """
    def census(max_entries):
        monkeypatch.setattr(PheromoneBoard, "MAX_ENTRIES", max_entries)
        b = PheromoneBoard()
        try:
            _publish_pre_guard(b)
            _flood(b)
            return sorted((e.agent_id, e.self_score, e.direction)
                          for e in b.get_live_signals("TEST"))
        finally:
            b.clear()

    assert census(80) == census(400) == census(10000)


# ══════════════════════════════════════════════════════════════════════════
# 主张 2：GuardBee 走普查（接线测试 —— 测的是 analyze() 而非 helper）
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def guard(board, stock_stub):
    from swarm_agents import GuardBeeSentinel
    return GuardBeeSentinel(board)


def _guard_details(guard, ticker="TEST"):
    r = guard.analyze(ticker)
    assert "error" not in r, f"GuardBee 分析失败: {r.get('error')}"
    return r, r["details"]


def test_guard_counts_all_six_bees_even_when_evicted(guard, board):
    """窗口不得被 n=5 或溢出淘汰截断：Guard 之前有 6 只蜂发布，就要数到 6 只。"""
    _publish_pre_guard(board)
    _flood(board)
    _, det = _guard_details(guard)
    assert det["top_signals_count"] == len(CENSUS_TRUTH), (
        f"Guard 只看到 {det['top_signals_count']} 只蜂，实际发布过 {len(CENSUS_TRUTH)} 只"
    )


def test_guard_consistency_not_fabricated_by_truncation(guard, board):
    """consistency 不得因截断被抬高（生产 35 份被记成 1.0「完全一致」）。"""
    _publish_pre_guard(board)
    _flood(board)
    _, det = _guard_details(guard)
    assert det["consistency"] == pytest.approx(TRUE_CONSISTENCY, abs=1e-6), (
        f"一致性 {det['consistency']:.3f} != 真值 {TRUE_CONSISTENCY:.3f} —— 低分条目被挤掉了"
    )
    assert det["consistency"] < 1.0, "板上只剩同向条目就报「完全一致」＝伪造一致"


def test_guard_direction_follows_true_majority(guard, board, no_resonance):
    """4 空 / 2 多 的标的不得被读成看多（生产方向 26.2% 不一致，bearish 32→63）。"""
    _publish_pre_guard(board)
    _flood(board)
    r, _ = _guard_details(guard)
    assert r["direction"] == "bearish", (
        f"4 空 2 多 被读成 {r['direction']} —— 看空条目分低、先被挤掉"
    )


def test_guard_avg_score_not_inflated_by_eviction(guard, board, no_resonance):
    """无共振分支的 `均分` 必须是 6 只蜂的真均值，不是幸存者均值。

    幸存者均值按构造偏高（淘汰键先扔分最低的），生产实测 avg_score
    在 n=1 那层被高估 +1.542。
    """
    _publish_pre_guard(board)
    _flood(board)
    r, det = _guard_details(guard)
    assert not det["resonance"].get("resonance_detected"), \
        "no_resonance 夹具没生效 —— 本条测的是无共振分支"
    import re
    m = re.search(r"均分\s*([0-9.]+)", r["discovery"])
    assert m, f"discovery 里没有「均分」：{r['discovery']}"
    assert float(m.group(1)) == pytest.approx(TRUE_AVG, abs=0.05), (
        f"均分 {m.group(1)} != 真值 {TRUE_AVG:.2f}（幸存者均值被淘汰键抬高）"
    )


def test_guard_identical_with_and_without_eviction(board, stock_stub, no_resonance):
    """成对断言：不溢出时行为**不变**（否则「改坏了也全绿」）。

    与上面几条合起来才有判别力——只断言「溢出时对」，把实现改成恒返回真值
    也能过；只断言「不溢出时不变」，则完全不碰截断也能过。
    """
    from swarm_agents import GuardBeeSentinel

    def verdict(flood: bool):
        b = PheromoneBoard()
        try:
            _publish_pre_guard(b)
            if flood:
                _flood(b)
            r = GuardBeeSentinel(b).analyze("TEST")
            assert "error" not in r
            return (r["direction"], round(r["details"]["consistency"], 6),
                    r["details"]["top_signals_count"])
        finally:
            b.clear()

    assert verdict(False) == verdict(True)


def test_guard_records_census_source(guard, board):
    """`details.top_signals_count` 的语义在本版变了（排行榜窗口 → 普查蜂数）。

    `signal_archive.py:312` 把它归档成时间序列，跨版本比对的人需要一个
    **机读**的口径标记，否则新旧两段数字长得一样、无从分辨。
    """
    _publish_pre_guard(board)
    _, det = _guard_details(guard)
    assert det["census_source"] == "live_agent_view"


def test_guard_falls_back_when_board_lacks_census(board, stock_stub):
    """旧版 board / 测试替身只有 `get_top_signals` 时仍要能跑，并如实标记口径。"""
    from swarm_agents import GuardBeeSentinel

    class LegacyBoard:
        """只暴露旧 API 的板替身。"""
        def __init__(self, inner):
            self._inner = inner

        def get_top_signals(self, ticker, n=5):
            return self._inner.get_top_signals(ticker, n=n)

        def detect_resonance(self, ticker):
            return self._inner.detect_resonance(ticker)

        def publish(self, entry):
            return self._inner.publish(entry)

        def snapshot(self):
            return self._inner.snapshot()

    _publish_pre_guard(board)
    r = GuardBeeSentinel(LegacyBoard(board)).analyze("TEST")
    assert "error" not in r, f"回退路径崩了: {r.get('error')}"
    assert r["details"]["census_source"] == "top_signals_fallback"


def test_guard_fallback_window_is_not_truncated(board, stock_stub):
    """回退路径也不许退回 n=5。

    回退是为了**不崩**，不是为了复刻旧截断。`_CENSUS_FALLBACK_N = 24` 这个值
    若没人断言，把它改回 5 是一个全绿的变异（mutation M7 实测幸存过一轮）——
    docstring 里写「回退不复刻 n=5」不等于有人在验它。

    回退用的是 `get_top_signals`，它不去重，所以数到的是**条目数 7**
    （CodeExec 占位 + 真实结论各一条）而非普查的 6 —— 回退本就是降级口径，
    这里断言的是「没被 n 截断」，不是「与普查等价」。
    """
    from swarm_agents import GuardBeeSentinel

    class LegacyBoard:
        def __init__(self, inner):
            self._inner = inner

        def get_top_signals(self, ticker, n=5):
            return self._inner.get_top_signals(ticker, n=n)

        def detect_resonance(self, ticker):
            return self._inner.detect_resonance(ticker)

        def publish(self, entry):
            return self._inner.publish(entry)

        def snapshot(self):
            return self._inner.snapshot()

    _publish_pre_guard(board)
    r = GuardBeeSentinel(LegacyBoard(board)).analyze("TEST")
    assert "error" not in r
    assert r["details"]["top_signals_count"] == len(PRE_GUARD_ROUND), (
        f"回退窗口只看到 {r['details']['top_signals_count']} 条，"
        f"本轮实际发布 {len(PRE_GUARD_ROUND)} 条 —— 回退把 n 截断带回来了"
    )


def test_guard_survives_board_that_raises(board, stock_stub):
    """守卫要堵两条失败路径：板**返回不了**和板**抛异常**。

    只判返回值的守卫接不住 `raise`（v0.45.117/119 同一物种）。这里让两个读法
    都抛，GuardBee 必须照常出结果并把口径如实标成 `unavailable` ——
    而不是让异常冲出去把整只蜂打成 error。
    """
    from swarm_agents import GuardBeeSentinel

    class ExplodingBoard:
        def get_live_signals(self, ticker):
            raise TypeError("板挂了")

        def get_top_signals(self, ticker, n=5):
            raise TypeError("板挂了")

        def detect_resonance(self, ticker):
            return {"resonance_detected": False, "direction": "neutral",
                    "supporting_agents": 0, "cross_dim_count": 0,
                    "consistency": 0.0, "resonant_dimensions": [],
                    "confidence_boost": 0}

        def publish(self, entry):
            pass

        def snapshot(self):
            return []

    r = GuardBeeSentinel(ExplodingBoard()).analyze("TEST")
    assert "error" not in r, f"板抛异常不该把整只蜂打成 error: {r.get('error')}"
    assert r["details"]["census_source"] == "unavailable"
    assert r["details"]["top_signals_count"] == 0


def test_guard_passes_true_bull_count_to_crowding(guard, board, monkeypatch):
    """子通道：`crowding_detector` 算 `bullish_agents / 6 * 100`，分母写死 6，
    分子却来自被截断的窗口 ⇒ 窗口越小拥挤度越假低。改普查后分子须为真值。
    """
    import crowding_detector
    seen = {}
    orig = crowding_detector.CrowdingDetector.calculate_crowding_score

    def spy(self, metrics):
        seen["bullish_agents"] = metrics.get("bullish_agents")
        return orig(self, metrics)

    monkeypatch.setattr(crowding_detector.CrowdingDetector,
                        "calculate_crowding_score", spy)
    _publish_pre_guard(board)
    _flood(board)
    guard.analyze("TEST")
    assert "bullish_agents" in seen, (
        "拥挤度路径没被走到 —— 本条测试等于没有。"
        "常见原因：`_fetch_stock_data` 桩的签名不对（见 stock_stub docstring），"
        "GuardBee 的 crowding 分支整段被 except 吞掉。"
    )
    assert seen["bullish_agents"] == 2, (
        f"传给拥挤度的看多蜂数 {seen['bullish_agents']} != 真值 2（Scout+Oracle）"
    )
