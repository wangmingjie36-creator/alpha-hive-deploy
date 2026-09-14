"""谁不进方向投票：BearBeeContrarian（v0.45.212）

为什么 BearBee 不该是陪审员
---------------------------
v0.45.209 发现 GuardBee 的方向票 100% 复述同伴。用真实代码逐位重放后，
它**起决定作用**的历史行里 96% 是 BearBee 以置信度 0.97 投看空 ——
Guard 的复述票在意外地抵消 BearBee。于是先查被抵消的那一方：

1. **方向近乎常数**：623 条有 T+7 超额收益的记录里 **90.5% 看空**。
2. **没有技能**：看空单扣 SPY 命中 **50.5%**（19 个不重叠周，p=0.63）；
   2026-08-12 之后的干净样本 40.0%（仅 3 周，不下结论）。
3. **置信度不含信息**：`confidence = 0.3 + 0.1×看空信号数 + 0.1×读到的 real 源数`
   —— 后一项是**数据可得性**，同 v0.45.191 CodeExec 兜底那个范畴错误。
   451 条 conf≥0.95 的看空命中 49.9%，这些股票平均反而跑赢 SPY +1.41%。
4. **还豁免了 ML 票重缩放**：`_effective_conf` 乘 `ml_adjustments.get(维度, 1.0)`，
   五个核心维度均值 0.50–0.60，`contrarian` 不在表里 ⇒ 默认 **1.0**，相对放大约一倍。

它的本职是**反方陈述**（看空上限 bear_cap、contrarian 视角、简报里的反对观点），
这些通道本版一律不动；拿掉的只是它在**计票**里的那一票。

⚠️ 结果证据是**零效应**，不是改善：预注册比较在历史原样与现行规则重演两个语料、
三种指标下 p 全在 0.12–0.59。本版的理由是「一张无技能、近乎常数、票重被可得性
抬满的票不该进计票」，**不是**「拿掉它能提高准确率」。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BEAR = "BearBeeContrarian"
GUARD = "GuardBeeSentinel"


def _r(source, direction, confidence=0.6, score=5.0, dim="x"):
    return {"source": source, "direction": direction, "confidence": confidence,
            "score": score, "dimension": dim, "discovery": f"test {source}",
            "data_quality": {"test": "real"}, "details": {}}


@pytest.fixture
def queen(board):
    from swarm_agents import QueenDistiller
    q = QueenDistiller(board)
    q.ml_feedback_enabled = False     # 票重只看 confidence，结论不随测试库里有没有 ML 历史变
    q.ml_adjustments = {}
    return q


def _vote(queen, results):
    return queen._compute_direction_vote("NVT", results, results, 6.0)


def _observers(bull=2, bear=1, neutral=2, conf=0.6):
    names = ["ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon",
             "RivalBeeVanguard", "CodeExecutorAgent"]
    dirs = ["bullish"] * bull + ["bearish"] * bear + ["neutral"] * neutral
    return [_r(n, d, conf) for n, d in zip(names, dirs)]


class TestBearBeeDoesNotVote:

    @pytest.mark.parametrize("obs", [
        dict(bull=3, bear=1, neutral=1),     # 看多多数：旧逻辑下 BearBee 0.97 足以把它拉成非看多
        # 全中性：旧逻辑下 BearBee 单票即可推成看空。⚠️ conf 必须是 0.5 —— 0.6 时
        # 0.97/(5×0.6+0.97)=24.4% 恰在 25% 看空门槛下，旧代码也不翻，这一格就测不到东西
        # （初稿就是 0.6，在改代码之前就绿了，是被「先看它红不红」揪出来的）。
        dict(bull=0, bear=0, neutral=5, conf=0.5),
        dict(bull=2, bear=2, neutral=1),     # 平局：旧逻辑下 BearBee 决定胜负
    ])
    def test_bear_direction_cannot_change_rule_direction(self, queen, obs):
        """判据本体：固定观测蜂，BearBee 投什么、多高置信，方向与分数都不变。

        比的是 (方向, rule_score) 而不只是方向：S5 重度冲突的折扣
        `factor × (多+空)/投票蜂数` 会改分数不改方向 —— 只比方向时，
        「分母里还数着 BearBee」这种半截修复是漏网的（变异 M5 实测）。
        """
        base = _observers(**obs)
        seen = {}
        for d in ("bullish", "bearish", "neutral"):
            for c in (0.3, 0.97):
                v = _vote(queen, base + [_r(BEAR, d, c)])
                seen[(d, c)] = (v["rule_direction"], v["rule_score"])
        v0 = _vote(queen, base)
        without = (v0["rule_direction"], v0["rule_score"])
        assert set(seen.values()) == {without}, (
            f"BearBee 的票改变了方向或分数：不含它={without}，含它={seen}"
        )

    @pytest.mark.parametrize("base", [
        _observers(bull=3, bear=1, neutral=1),
        # 单票上限 = 投票蜂置信度之和 × 0.4。这一组让上限**真的咬住**：
        # 0.9 被压到 0.4；若上限按全体算，BearBee 的 0.97 会把它抬到 0.788。
        # 没有这一组，「上限仍按全体算」的半截修复是漏网的（变异 M6 实测）。
        [_r("ScoutBeeNova", "bullish", 0.9), _r("OracleBeeEcho", "neutral", 0.1)],
    ])
    def test_bear_weight_not_in_vote_weights(self, queen, base):
        w0 = _vote(queen, base)["direction_vote_weights"]
        w1 = _vote(queen, base + [_r(BEAR, "bearish", 0.97)])["direction_vote_weights"]
        assert w0 == w1, f"BearBee 的置信度进了票重：{w0} → {w1}"

    def test_conflict_level_counts_voters_only(self, queen):
        """重度冲突要求多空各 ≥2 票；BearBee 不能凑出那第二张看空票。"""
        base = _observers(bull=2, bear=1, neutral=2)
        assert _vote(queen, base + [_r(BEAR, "bearish", 0.97)])["conflict_level"] == "moderate"

    def test_single_observing_bee_can_still_push_bearish(self, queen):
        """正对照：看空门槛（≥1 只 + ≥25% 票重）对**观测蜂**照旧生效。

        旧的 `test_single_bearish_agent_can_push_direction` 用 BearBee 当那一只，
        本版改为观测蜂 —— 没有这一条，「计票里谁都不算」的实现也能让上面几条全绿。
        """
        res = [_r(n, "neutral", 0.5) for n in
               ("OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon", "RivalBeeVanguard")]
        res.append(_r("ScoutBeeNova", "bearish", 0.85))
        assert _vote(queen, res)["rule_direction"] == "bearish"

    def test_bear_is_still_reported(self, queen):
        """展示面不变：报告的 agent_breakdown 与逐蜂方向照旧看全体。"""
        base = _observers(bull=3, bear=1, neutral=1)
        dv = _vote(queen, base + [_r(BEAR, "bearish", 0.97)])
        assert dv["per_agent_directions"][BEAR] == "bearish"
        assert dv["bearish_count"] == 2, "agent_breakdown 的展示口径（全体）不该变"
        assert BEAR in dv["data_quality_summary"]
        assert dv["voting_counts"]["bearish"] == 1, "计票口径应只含投票蜂"

    def test_exclusion_is_machine_readable(self, queen):
        """「这个方向是哪种计票规则产出的」必须能从输出里读出来。"""
        dv = _vote(queen, _observers() + [_r(BEAR, "bearish", 0.97)])
        assert dv["vote_excluded_agents"] == [BEAR]
        dv2 = _vote(queen, _observers())
        assert dv2["vote_excluded_agents"] == [], "没出场的蜂不该出现在排除名单里"

    def test_bull_veto_still_reads_bear(self, queen, monkeypatch):
        """BearBee 的独立通道保留：BullVeto 打开时照旧读它的分数（它本身停用中）。

        ⚠️ `queen` fixture 已先实例化 —— QueenDistiller.__init__ 会 reload(config)，
        在实例化之前 patch 会静默失效（改写 test_arbitration_flip_direction 时实测踩中）。
        """
        import config
        monkeypatch.setitem(config.BEAR_SCORING_CONFIG, "bull_veto_enabled", True)
        monkeypatch.setitem(config.BEAR_SCORING_CONFIG, "bull_veto_bear_score", 7.0)
        base = _observers(bull=4, bear=0, neutral=1, conf=0.8)
        assert _vote(queen, base)["rule_direction"] == "bullish"
        dv = _vote(queen, base + [_r(BEAR, "bearish", 0.9, score=8.0)])
        assert dv["rule_direction"] == "neutral", "BullVeto 读不到 BearBee 了 —— 排除范围过宽"


class TestGuardBeeDoesNotVote:
    """GuardBeeSentinel 的方向票是其余六只多数的复述（v0.45.209：普查口径后 90/90），
    被 `_compute_direction_vote` 当第 7 张独立票再数一遍。

    它此前「有用」只是因为在抵消 BearBee（见模块 docstring）；BearBee 退出计票后，
    预注册比较里再摘 Guard 两语料三指标 p 全在 0.38–0.99 —— **零效应**。
    理由同样是结构性的：一张复述票不是证据。
    """

    @pytest.mark.parametrize("obs", [
        dict(bull=3, bear=1, neutral=1),
        dict(bull=2, bear=1, neutral=2, conf=0.5),
        dict(bull=2, bear=2, neutral=1),
    ])
    def test_guard_cannot_change_direction_or_score(self, queen, obs):
        base = _observers(**obs)
        seen = {}
        for d in ("bullish", "bearish", "neutral"):
            v = _vote(queen, base + [_r(GUARD, d, 0.7)])
            seen[d] = (v["rule_direction"], v["rule_score"])
        v0 = _vote(queen, base)
        assert set(seen.values()) == {(v0["rule_direction"], v0["rule_score"])}, (
            f"GuardBee 的票改变了方向或分数：不含它={(v0['rule_direction'], v0['rule_score'])}，含它={seen}"
        )

    def test_guard_is_still_reported(self, queen):
        dv = _vote(queen, _observers() + [_r(GUARD, "bullish", 0.7)])
        assert dv["per_agent_directions"][GUARD] == "bullish"
        assert GUARD in dv["vote_excluded_agents"]

    def test_guard_score_gate_is_untouched(self, queen):
        """Guard 的**分数**通道（`_apply_triple_penalty` 风险关门）不属于计票，本版不动。

        拿真实的关门跑一遍，而不是 grep 源码：关门是按 `dimension == "risk_adj"` 找 Guard 的，
        不按蜂名 —— 初稿按蜂名 grep，在代码没动的情况下就红了。
        """
        res = [_r(GUARD, "bullish", 0.7, score=2.0, dim="risk_adj")]
        tp = queen._apply_triple_penalty("NVT", 7.0, res)
        assert tp["guard_penalty_applied"] is True, "Guard 分数 2.0 应触发风险关门"

    def test_resonance_no_longer_counts_guard_dimension(self, board):
        """v0.45.235 改写：Guard 复述多数时**不再**给共振多算一个维度。

        v0.45.212 登记的是「仍多算、未测未动」。v0.45.235 测完（对收益无可测影响、只抬分数）
        并修掉，完整判据在 tests/test_resonance_independent_sources.py。
        """
        from pheromone_board import PheromoneEntry
        for agent in ["OracleBeeEcho", "RivalBeeVanguard", GUARD]:
            board.publish(PheromoneEntry(agent_id=agent, ticker="RSG", discovery="x",
                                         source="test", self_score=6.0, direction="bullish"))
        res = board.detect_resonance("RSG")
        assert "risk_adj" not in res["resonant_dimensions"]

class TestDissentAgentsMustVote:
    """异议蜂必须是投票蜂：S4.5 仲裁只遍历计票名单，不投票的异议蜂是一条死配置。"""

    def test_config_dissent_agents_are_voters(self):
        from config import CONFLICT_ARBITRATION_CONFIG as cfg
        from swarm_agents import QueenDistiller
        dead = set(cfg["dissent_agents"]) & QueenDistiller.NON_VOTING_AGENTS
        assert not dead, f"{dead} 被列为异议蜂却不进计票 —— 这条加成永远不会生效"

    def test_code_fallback_matches_config(self):
        """函数里的兜底默认值与 config 必须一致，否则删了 config 键会悄悄复活死成员。"""
        import ast
        import inspect
        import re
        from config import CONFLICT_ARBITRATION_CONFIG as cfg
        from swarm_agents import QueenDistiller
        src = inspect.getsource(QueenDistiller._compute_direction_vote)
        m = re.search(r'_CAC\.get\("dissent_agents", (\[.*?\])\)', src)
        assert m, "没找到 dissent_agents 的兜底默认值"
        assert sorted(ast.literal_eval(m.group(1))) == sorted(cfg["dissent_agents"])

class TestCohortBoundary:

    def test_entry_registered(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        hits = [r for d, v, r in _COHORT_HISTORY if v == "v0.45.212"]
        assert len(hits) == 1
        assert "BearBeeContrarian" in hits[0]
        assert "GuardBeeSentinel" in hits[0]
