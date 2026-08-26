"""
RivalBeeVanguard 的跨蜂特征读取与阶段顺序（v0.44.3）

背景
----
RivalBee 的 ML 特征里有三个属别的蜂的产出：
  · `catalyst_quality` ← ChronosBee 的催化剂分
  · `iv_rank` / `put_call_ratio` ← OracleBee 的期权 details（键名 `iv_rank` / `pc_ratio`）

它原先在 **Phase-1 并行**里跑，读不到这些（同批蜂互相看不见），于是写死了
`"B+"` / `50.0` / `1.0` 三个常量 —— "ML 预测"实际上只由动量驱动
（见 `experiments/ml_expected_return_report.md`）。v0.44.3 把它移到 **Phase-1.5**。

为什么必须有这组测试
--------------------
移完之后全量套件依然全绿 —— 因为**没有任何既有测试覆盖阶段顺序**。
「跑通了」和「读到了」是两件事，本项目的招牌缺陷正是前者掩盖后者。
所以这里逐条钉住：读得到时用真值、读不到时**回落值可与真值区分**、
以及顺序契约本身（源码级断言）。
"""

import inspect
import re
from pathlib import Path

import pytest

import ml_predictor as mp
from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents.rival_bee import RivalBeeVanguard

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _entry(agent_id, ticker="TEST", score=5.0, direction="neutral", details=None):
    return PheromoneEntry(
        agent_id=agent_id, ticker=ticker,
        discovery="x", source="test",
        self_score=score, direction=direction,
        details=details or {},
    )


@pytest.fixture
def bee():
    b = RivalBeeVanguard.__new__(RivalBeeVanguard)   # 跳过重量级 __init__
    b.board = PheromoneBoard()
    return b


# ════════════════════════════════════════════════════════════════════════════
# _read_peer：读取同轮其他蜂的条目
# ════════════════════════════════════════════════════════════════════════════

class TestReadPeer:

    def test_finds_the_requested_agent(self, bee):
        bee.board.publish(_entry("ChronosBeeHorizon", score=8.8))
        got = bee._read_peer("TEST", "ChronosBeeHorizon")
        assert got is not None
        assert got.self_score == pytest.approx(8.8)

    def test_returns_none_when_absent(self, bee):
        bee.board.publish(_entry("ScoutBeeNova"))
        assert bee._read_peer("TEST", "OracleBeeEcho") is None

    def test_does_not_leak_across_tickers(self, bee):
        bee.board.publish(_entry("OracleBeeEcho", ticker="AAA"))
        assert bee._read_peer("BBB", "OracleBeeEcho") is None

    def test_lookup_window_covers_a_full_phase1_round(self, bee):
        """`get_top_signals` 默认 n=5 会截断 —— Phase-1 有 5~6 只蜂发布，
        目标蜂可能恰好排在第 6 位。这条钉住放大后的窗口真的够用。
        """
        for i in range(8):
            bee.board.publish(_entry(f"Filler{i}", score=9.9))
        bee.board.publish(_entry("OracleBeeEcho", score=0.1,
                                 details={"iv_rank": 42.0}))
        got = bee._read_peer("TEST", "OracleBeeEcho")
        assert got is not None, "目标条目被 get_top_signals 的 n 截断了"

    def test_broken_board_returns_none_not_crash(self, bee):
        class _Bad:
            def get_top_signals(self, *a, **k):
                raise AttributeError("boom")
        bee.board = _Bad()
        assert bee._read_peer("TEST", "OracleBeeEcho") is None


# ════════════════════════════════════════════════════════════════════════════
# 催化剂等级：分数 → 等级的单一真相
# ════════════════════════════════════════════════════════════════════════════

class TestCatalystGradeConversion:

    @pytest.mark.parametrize("score,grade", [
        (10.0, "A+"), (8.5, "A+"), (8.4, "A"), (7.5, "A"),
        (7.4, "B+"), (6.5, "B+"), (6.4, "B"), (5.5, "B"),
        (5.4, "C"), (0.0, "C"),
    ])
    def test_thresholds_match_project_convention(self, score, grade):
        """阈值沿用项目既有约定 —— 历史 predictions 的 catalyst_quality 都按这套
        生成，改了会让新旧样本不可比。"""
        assert mp.catalyst_quality_from_score(score) == grade

    @pytest.mark.parametrize("bad", [None, float("nan"), "n/a", object()])
    def test_missing_score_gives_B_not_Bplus(self, bad):
        """缺失必须给 "B"（magnitude 0.9）而**不是** "B+"（1.0 基准档）——
        否则"拿不到数据"与"质量正好中等"不可区分。
        """
        assert mp.catalyst_quality_from_score(bad) == "B"

    def test_grade_is_defined_in_exactly_one_place(self):
        """阈值此前在至少三处各写一份嵌套 `_cat_qual`，与 expected_returns 曾经的
        三重复制同一个反模式。钉住不再增殖。
        """
        hits = []
        for path in PROJECT_ROOT.glob("*.py"):
            src = path.read_text(encoding="utf-8", errors="ignore")
            # 数"独立定义了 8.5→A+ 阈值"的地方，注释不算
            code = "\n".join(ln for ln in src.splitlines()
                             if not ln.lstrip().startswith("#"))
            if re.search(r'8\.5.*?["\']A\+["\']', code):
                hits.append(path.name)
        assert hits == ["ml_predictor.py"], (
            f"催化剂等级阈值出现在多处: {hits} —— 应只在 ml_predictor 定义"
        )


# ════════════════════════════════════════════════════════════════════════════
# 特征真的被读进 TrainingData
# ════════════════════════════════════════════════════════════════════════════

class TestFeaturesReachTrainingData:
    """拦截 `MLPredictionService.predict_for_opportunity`，检查它收到的
    `TrainingData` —— 这是"读到了"唯一的直接证据。
    """

    def _captured(self, bee, ticker="TEST", monkeypatch=None):
        captured = {}

        class _FakeService:
            def predict_for_opportunity(self, data):
                captured["data"] = data
                return {"probability": 0.5, "expected_3d": 0.0,
                        "expected_7d": 0.0, "expected_30d": 0.0}

        import ml_predictor_extended as mpe
        monkeypatch.setattr(mpe, "MLPredictionService", _FakeService)
        # 让重量级依赖安静下来
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
        bee.analyze(ticker)
        return captured.get("data")

    def test_reads_real_catalyst_and_options_features(self, bee, monkeypatch):
        bee.board.publish(_entry("ChronosBeeHorizon", score=8.8))
        bee.board.publish(_entry("OracleBeeEcho",
                                 details={"iv_rank": 77.0, "pc_ratio": 1.9}))
        data = self._captured(bee, monkeypatch=monkeypatch)
        assert data is not None, "ML 服务未被调用"
        assert data.catalyst_quality == "A+", "催化剂分 8.8 应转成 A+"
        assert data.iv_rank == pytest.approx(77.0)
        assert data.put_call_ratio == pytest.approx(1.9)

    def test_falls_back_distinguishably_when_peers_absent(self, bee, monkeypatch):
        """板上空 ⇒ 回落值必须与"读到中性真值"可区分。

        `catalyst_quality` 回落 "B"（不是 "B+"）正是为此。
        """
        data = self._captured(bee, monkeypatch=monkeypatch)
        assert data.catalyst_quality == "B"
        assert data.iv_rank == pytest.approx(50.0)
        assert data.put_call_ratio == pytest.approx(1.0)

    def test_partial_oracle_details_are_tolerated(self, bee, monkeypatch):
        """OracleBee 的 details 是条件填充的（`if result:` 之下逐键 set），
        缺键必须各自独立回落，不能一个缺就全丢。"""
        bee.board.publish(_entry("OracleBeeEcho", details={"iv_rank": 12.0}))
        data = self._captured(bee, monkeypatch=monkeypatch)
        assert data.iv_rank == pytest.approx(12.0)
        assert data.put_call_ratio == pytest.approx(1.0)

    @pytest.mark.parametrize("junk", [None, float("nan"), "high", [1]])
    def test_junk_option_values_fall_back(self, bee, monkeypatch, junk):
        bee.board.publish(_entry("OracleBeeEcho",
                                 details={"iv_rank": junk, "pc_ratio": junk}))
        data = self._captured(bee, monkeypatch=monkeypatch)
        assert data.iv_rank == pytest.approx(50.0)
        assert data.put_call_ratio == pytest.approx(1.0)

    def test_real_crowding_is_still_passed(self, bee, monkeypatch):
        """v0.44.2 的改动不能被 v0.44.3 回退掉。"""
        import real_data_sources as rds
        import crowding_detector as cd

        monkeypatch.setattr(rds, "get_real_crowding_metrics",
                            lambda t, s, b: {"stocktwits_messages_per_day": 1})

        class _Det:
            def __init__(self, ticker):
                pass

            def calculate_crowding_score(self, metrics):
                return 41.5, {}

        monkeypatch.setattr(cd, "CrowdingDetector", _Det)
        bee.board = PheromoneBoard()
        data = self._captured(bee, monkeypatch=monkeypatch)
        assert data.crowding_score == pytest.approx(41.5)


# ════════════════════════════════════════════════════════════════════════════
# 阶段顺序契约（源码级）
# ════════════════════════════════════════════════════════════════════════════

class TestPhaseOrderingContract:
    """顺序是这次修复的**全部前提**：Rival 排在 Chronos/Oracle 之后才读得到，
    排在 Bear 之前 Bear 才看得到它。

    这类缺陷的本质是**执行顺序错而非逻辑错** —— 把 Rival 挪回 Phase-1 并行，
    单元测试仍会全绿（它会安静地回落到三个中性常量），只有源码级断言能拦住。
    与 `test_chronos_bee_direction.py::test_pead_applied_after_scoring_block`
    同一思路。
    """

    @pytest.fixture(scope="class")
    def src(self):
        return (PROJECT_ROOT / "alpha_hive_daily_report.py").read_text(
            encoding="utf-8")

    def test_rival_not_in_phase1_parallel_list(self, src):
        m = re.search(r"phase1_agents = \[(.*?)\]", src, re.S)
        assert m, "找不到 phase1_agents 列表"
        assert "RivalBeeVanguard" not in m.group(1), (
            "RivalBeeVanguard 又回到了 Phase-1 并行列表 —— 它将读不到 "
            "ChronosBee/OracleBee 的产出，静默退回三个中性常量"
        )

    def test_rival_runs_after_phase1_and_before_bear(self, src):
        i_p1 = src.index("with ThreadPoolExecutor(max_workers=len(ctx.phase1_agents))")
        i_rival = src.index("ctx.rival_agent.analyze(ticker)")
        i_guard = src.index("ctx.guard_agent.analyze(ticker)")
        i_bear = src.index("ctx.bear_agent.analyze(ticker)")
        assert i_p1 < i_rival, "Rival 必须在 Phase-1 并行块之后"
        assert i_rival < i_guard < i_bear, (
            f"顺序应为 Phase1 → Rival → Guard → Bear，实测偏移 "
            f"rival={i_rival} guard={i_guard} bear={i_bear}"
        )

    def test_rival_receives_prefetched_data(self, src):
        """漏掉会让 Rival 逐标的直接抓 yfinance —— 本项目三次事故的同一根因。"""
        m = re.search(r"all_agents = phase1_agents \+ \[(.*?)\]", src)
        assert m, "找不到 all_agents 构造"
        assert "rival_agent" in m.group(1), (
            "rival_agent 不在 all_agents 里 ⇒ inject_prefetched 不会注入它，"
            "会退化成逐标的直接抓取（限流风险）"
        )

    def test_read_peer_documents_the_ordering_requirement(self):
        """`_read_peer` 的返回值依赖阶段顺序，这个前提必须写在它自己的
        docstring 里 —— 否则下一个调用方会以为它总能读到。"""
        from swarm_agents.base import BeeAgent
        doc = inspect.getdoc(BeeAgent._read_peer) or ""
        assert "None" in doc
        assert "并行" in doc or "顺序" in doc or "阶段" in doc
