"""RivalBee 的 catalyst_quality：缺失哨兵选中众数 + 缺失本身是被板淘汰造出来的（v0.45.151）

背景（全部为 803 份生产 `analysis-*-ml-*.json` 实测，非推断）
------------------------------------------------------------
`expected_returns()` 是闭式 `mag × momentum_5d × horizon_scale`，而
`expected_30d` 与 `momentum_5d` 双双落在生产 JSON 里 ⇒ 可**反解**出 rival_bee
当时实际用的 `catalyst_quality`，再与同文件 ChronosBee 的真分对照。

当前世代（2026-08-24 起，闭式生效）188 份：

  · 真值≠"B" 的 39 份里，**27 份（69.2%）rival_bee 用的是缺失哨兵 "B"**；
  · 27 份的等级流向**全部**是 `C → B`（无一例外），即
    magnitude 0.7 被当成 0.9，`expected_7d/30d` 幅度系统性**高估 28.6%**；
  · 按 ChronosBee 分数分层完全单调：4.0 分 26/28 读丢、7.45 分 0/10 读丢。

为什么读不到 —— 不是"上游没跑"，是**板把它挤掉了**
--------------------------------------------------
`PheromoneBoard.MAX_ENTRIES = 80`，注释写着「7 Agent × 9 Ticker = 63 条」——
按 **9 只标的**定的容量，而 `config.WATCHLIST` 现为 **30 只**（一轮约 210 条）。
溢出淘汰键是 `(self_score, support_count, pheromone_strength)` 的 `nlargest`，
即**先扔分最低的**。ChronosBee「无近期催化剂」恰好落 4.0，是全板最低的一档，
于是它在 RivalBee（Phase-1.4）读之前就被挤出去了。

⚠️ 缺失与被测量的量**反相关**：越是"没有催化剂"，越读不到。这不是随机缺失。
用生产发布序列跑真实 `PheromoneBoard` 的重放复现了同一形状：
≤16 只标的的日子 0 条丢失，24~30 只的日子丢 3~11 条。

契约边界
--------
`ml_predictor.catalyst_quality_from_score(None)` **仍返回 "B"，本版不动**
（v0.45.147 已在其 docstring 记录理由被推翻的证据）。改的是**调用方**。
"""

import math

import pytest

import ml_predictor as mp
import ml_predictor_extended as mpe
from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents.rival_bee import RivalBeeVanguard


@pytest.fixture(autouse=True)
def _offline_sources(stub_yfinance, stub_reddit):
    """同 test_rival_bee_peer_features：取数支路显式钉死，不依赖传输层兜底。"""


def _entry(agent_id, ticker="TEST", score=5.0, direction="neutral", details=None):
    return PheromoneEntry(
        agent_id=agent_id, ticker=ticker, discovery="x", source="test",
        self_score=score, direction=direction, details=details or {},
    )


@pytest.fixture
def bee():
    b = RivalBeeVanguard.__new__(RivalBeeVanguard)
    b.board = PheromoneBoard()
    return b


def _flood(board, score=9.9):
    """把板灌到 MAX_ENTRIES 溢出，复刻生产形状（30 只标的 × 7 蜂 ≈ 210 条）。

    ⚠️ 每条填充必须用**不同的 ticker**。`publish` 的衰减是 ticker-scoped
    （`if not self._ticker_scoped or e.ticker == entry.ticker`）—— 若填充全用
    同一个 ticker，它们会把彼此衰减到 `MIN_STRENGTH` 以下先行消失，板根本涨
    不到 80 条，溢出淘汰不会触发，于是这组测试会**假绿**（第一版就栽在这）。

    分数 9.9 远高于 ChronosBee「无近期催化剂」的 4.0 ⇒ 淘汰一定先挑它。
    """
    for i in range(PheromoneBoard.MAX_ENTRIES + 5):
        board.publish(_entry("Filler", ticker=f"FLOOD{i}", score=score))


def test_flood_helper_actually_evicts():
    """夹具反向自证：若 `_flood` 没真的挤掉低分条目，本文件的溢出组全是假绿。"""
    board = PheromoneBoard()
    board.publish(_entry("ChronosBeeHorizon", score=4.0))
    _flood(board)
    assert len(board._entries) == PheromoneBoard.MAX_ENTRIES, "板没被灌满"
    assert not any(e.agent_id == "ChronosBeeHorizon" for e in board._entries), (
        "低分条目没有被溢出淘汰挤掉 —— 夹具失效，溢出组的绿是假的")


# ════════════════════════════════════════════════════════════════════════════
# 1. 同轮 peer 条目不该被 MAX_ENTRIES 溢出淘汰吃掉
# ════════════════════════════════════════════════════════════════════════════

class TestPeerLookupSurvivesOverflow:

    def test_low_scoring_peer_survives_board_overflow(self, bee):
        """生产实测的那条路径：ChronosBee 落 4.0（无近期催化剂）→ 全板最低分
        → 溢出淘汰第一个扔的就是它 → RivalBee 读不到。
        """
        bee.board.publish(_entry("ChronosBeeHorizon", score=4.0))
        _flood(bee.board)
        got = bee._read_peer("TEST", "ChronosBeeHorizon")
        assert got is not None, "低分 peer 被 MAX_ENTRIES 溢出淘汰挤掉了"
        assert got.self_score == pytest.approx(4.0)

    def test_absent_peer_still_returns_none(self, bee):
        """成对：防"永远返回点什么"的偷懒修法。从未发布过 ⇒ 必须 None。"""
        _flood(bee.board)
        assert bee._read_peer("TEST", "ChronosBeeHorizon") is None

    def test_lookup_still_isolates_tickers(self, bee):
        """成对：防"退化成全局最近一条"的偷懒修法。"""
        bee.board.publish(_entry("ChronosBeeHorizon", ticker="AAA", score=4.0))
        _flood(bee.board)
        assert bee._read_peer("BBB", "ChronosBeeHorizon") is None

    def test_latest_entry_wins_for_same_agent_and_ticker(self, bee):
        """同一蜂同一标的重复发布 ⇒ 取**最新**那条，不是第一条。"""
        bee.board.publish(_entry("ChronosBeeHorizon", score=4.0))
        bee.board.publish(_entry("ChronosBeeHorizon", score=8.8))
        _flood(bee.board)
        got = bee._read_peer("TEST", "ChronosBeeHorizon")
        assert got is not None and got.self_score == pytest.approx(8.8)

    def test_broken_board_returns_none_not_crash(self, bee):
        class _Bad:
            def get_top_signals(self, *a, **k):
                raise AttributeError("boom")
        bee.board = _Bad()
        assert bee._read_peer("TEST", "ChronosBeeHorizon") is None


# ════════════════════════════════════════════════════════════════════════════
# 2. 真读不到时给 None，而不是众数 "B"
# ════════════════════════════════════════════════════════════════════════════

class TestMissingCatalystIsNotTheMode:
    """拦截 `MLPredictionService.predict_for_opportunity` 看它**实参**收到什么 ——
    这是"喂进模型的到底是哪个值"唯一的直接证据（v0.45.142：钉最终消费点的实参）。
    """

    def _captured(self, bee, monkeypatch, ticker="TEST"):
        captured = {}

        class _FakeService:
            def predict_for_opportunity(self, data):
                captured["data"] = data
                return {"probability": 0.5, "expected_3d": 0.0,
                        "expected_7d": 0.0, "expected_30d": 0.0}

        monkeypatch.setattr(mpe, "MLPredictionService", _FakeService)
        monkeypatch.setattr(bee, "_get_stock_data",
                            lambda t: {"volatility_20d": 30.0, "momentum_5d": 1.0,
                                       "price": 100.0, "volume_ratio": 1.0})
        monkeypatch.setattr(bee, "_get_history_context", lambda t: "")
        monkeypatch.setattr(bee, "_validate_ticker", lambda t: None)
        monkeypatch.setattr(bee, "_calc_technical_indicators",
                            lambda t: {"tech_score_adj": 0.0,
                                       "tech_direction": "neutral", "summary": ""})
        monkeypatch.setattr(bee, "_assess_eps_revision",
                            lambda t: {"revision_signal": "unknown"})
        monkeypatch.setattr(bee, "_publish", lambda *a, **k: None)
        result = bee.analyze(ticker)
        return captured.get("data"), result

    def test_unreadable_catalyst_is_none_not_the_mode(self, bee, monkeypatch):
        """"B" 是生产**众数**（461/803 = 57.4%）⇒ 拿它当缺失哨兵，
        「读不到」与「质量正好是 B」在特征上完全同形。必须给 None。
        """
        data, _ = self._captured(bee, monkeypatch)
        assert data is not None, "ML 服务未被调用"
        assert data.catalyst_quality is None, (
            f"读不到催化剂却喂了 {data.catalyst_quality!r} —— "
            "那是真实等级里最常见的一档，缺失被伪装成观测")

    def test_readable_catalyst_still_uses_true_grade(self, bee, monkeypatch):
        """成对：防"一律 None"的偷懒修法。"""
        bee.board.publish(_entry("ChronosBeeHorizon", score=8.8))
        data, _ = self._captured(bee, monkeypatch)
        assert data.catalyst_quality == "A+"

    def test_low_score_catalyst_reaches_model_as_C(self, bee, monkeypatch):
        """生产上那 27 份的真身：ChronosBee 4.0 → 应得 "C"（magnitude 0.7），
        实际被记成 "B"（0.9）—— 幅度高估 28.6%。板溢出后仍须读到 "C"。
        """
        bee.board.publish(_entry("ChronosBeeHorizon", score=4.0))
        _flood(bee.board)
        data, _ = self._captured(bee, monkeypatch)
        assert data.catalyst_quality == "C"

    def test_missing_catalyst_is_observable_downstream(self, bee, monkeypatch):
        """CLAUDE.md 硬检查项：「这个失败，下游怎么知道？」

        此前只有一行 `_log.debug` —— 生产日志级别之下，等于没有观测点。
        """
        _, result = self._captured(bee, monkeypatch)
        dq = (result or {}).get("data_quality") or {}
        assert dq.get("catalyst_quality") == "unreadable", (
            f"读不到催化剂没有留下任何机读痕迹：data_quality={dq}")

    def test_readable_catalyst_marks_itself_real(self, bee, monkeypatch):
        """成对：防"永远写 unreadable"的偷懒修法。"""
        bee.board.publish(_entry("ChronosBeeHorizon", score=8.8))
        _, result = self._captured(bee, monkeypatch)
        assert (result["data_quality"] or {}).get("catalyst_quality") == "peer_read"


# ════════════════════════════════════════════════════════════════════════════
# 3. 第四份 catalyst 编码副本（ml_predictor 导入失败时的应急降级）
# ════════════════════════════════════════════════════════════════════════════

class TestExtendedFallbackEncoderMirrorsPrimary:
    """`ml_predictor_extended.SimpleMLModel.encode_catalyst_quality` 是第四份表。

    它只在 `ml_predictor` 导入失败时生效，且**只有 rival_bee 会走**。
    rival_bee 一旦开始传 `None`，这份副本会给 0.5 —— 而 0.5 恰好落在
    C(0.40) 与 B(0.55) **之间**，是任何真实等级都产不出的值，
    树模型却会拿它当一个真实的中间档去切分（同 v0.45.147 对主表的判据）。
    """

    def test_real_grades_match_primary(self):
        m = mpe.SimpleMLModel()
        for g in ("A+", "A", "B+", "B", "C"):
            assert m.encode_catalyst_quality(g) == mp._encode_catalyst(g), g

    def test_none_matches_primary_missing_semantics(self):
        got = mpe.SimpleMLModel().encode_catalyst_quality(None)
        assert math.isnan(got), (
            f"None 被编码成 {got!r}；主表 `_encode_catalyst(None)` 给 NaN，"
            "两份表对缺失的语义必须一致")

    def test_unknown_literal_still_05_in_both(self):
        """成对：未知字面量的旧行为**不动**（本仓无生产路径产得出它）。"""
        assert mpe.SimpleMLModel().encode_catalyst_quality("X") == 0.5
        assert mp._encode_catalyst("X") == 0.5


# ════════════════════════════════════════════════════════════════════════════
# 4. 世代边界
# ════════════════════════════════════════════════════════════════════════════

def test_cohort_boundary_registered():
    """`ml.expected_7d` / `ml.expected_30d` 进 `signal_archive`，而
    `ic_rerun_readiness` 的 docstring 明写「任何再次改动 RivalBee 特征来源的
    改动都必须往 `_COHORT_HISTORY` 追加一条」。本版改的正是它。
    """
    from ic_rerun_readiness import _COHORT_HISTORY
    assert _COHORT_HISTORY[-1][0] >= "2026-09-07", (
        f"最后一条世代边界是 {_COHORT_HISTORY[-1][0]}，本版改了 RivalBee 特征来源却没登记")
