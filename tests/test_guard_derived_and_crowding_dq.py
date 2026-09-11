"""GuardBeeSentinel 是派生蜂：方向 100% 复述同伴；crowding 降级被上报成 real（v0.45.209）

一、方向没有独立信息（实测，非推断）
------------------------------------
Guard 的 direction 只在四处被赋值：`resonance["direction"]`、census 的
bull/bear 多数三分支、以及一条 LLM 分支（项目禁用 LLM，台账五个月零命中）。
**每一个输入都是别的蜂的输出**，它自己不观测任何东西。

按 `session_id` 分组反解 `agent_memory`（一次扫描 = 一个 session；
按 (date,ticker) 分组会被重扫的重复行污染 —— 35.3% 的组合有多行）：

    2026-09-07 起（v0.45.163/164 普查口径）：用「其余六只的多数」预测 Guard 方向
        复现 **100.0%（90/90）**，打乱配对的对照 38.9%
        共振支 15/15、信号分散支 75/75，**零例外**
    2026-09-07 之前（n=5 排行榜口径）：复现 85.5%，对照 70.7%

顺带**验证了 v0.45.163/164 的修复确实生效**：`一致性` 是 Guard 当时看见了什么的
指纹，修复后六只口径逐位吻合 100%（n=90）、五只口径 0%；修复前正相反
（六只 8.3% / 五只 46.8%），且修复前 68.6% 的一致性取值分母只能是 5。

二、于是它在投票里是什么
------------------------
`risk_adj` 权重自 v0.45.172 归零，且 v0.45.176 断开旁路后**生产实测确实是 0**
（2026-09-10 起 12/12 份 JSON 的 `swarm.dimension_weights.risk_adj == 0.0000`）
⇒ Guard 的**分数**对加权维度分毫无贡献，它对 `final_score` 唯一活着的通道
就是那张方向票 —— 而那张票是其余六只多数方向的复述，
`_compute_direction_vote` 把它当第 7 张独立票再数一遍。

去掉 Guard 那一票，782 份 JSON 里看多票 >=3 的 628 行中有 **110 行（17.5%）**
跌破 `BULLISH_GATE_CONFIG(min_agents=3)`（对比：v0.45.201 修 Oracle 是 7.4%）。

⚠️ **本版不动这张票。** 它不是看多偏斜（修复后 41 多 / 37 空 / 12 中性，
它放大的是「多数」而非「看多」），但它让共识看起来比证据更强。
要不要让派生蜂投票是设计决定，留给用户。

三、本版真正改掉的那一条
------------------------
`data_quality["crowding"]` 是**硬编码字面量 `"real"`**，而 v0.45.50 给拥挤度
加了降级路径（全分量不可得 ⇒ `get_adjustment_factor(None)` 返回 1.0 中性）。
**1.0 在正常三档（1.2 / 0.95 / 0.70）里不存在**，所以它是降级的精确指纹：

    台账 1710 行里 40 行（2.3%）adj_factor == 1.00，
    这 40 行**全部**向 DQ 机器上报 `crowding = "real"`（`REAL_SOURCES`，满分 1.0）。

正是 CLAUDE.md 的头号硬检查项：**「这个失败，下游怎么知道？」** —— 答案是
「不知道」：`_log.warning` 只进日志，`_apply_triple_penalty` 的 `data_real_pct`
拿到的是满分。改为降级时上报 `"unavailable"`（`PROXY_SOURCES`，0.7 档）——
取既有档位而非新字面量，否则按契约会落进「其他 = 0.0 分」，
把「这一项降级」升格成「这一项全废」（同 v0.45.191 选 `fallback` 的理由）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swarm_agents.queen_distiller import QueenDistiller


class TestCrowdingDegradationIsVisibleDownstream:
    """降级必须让下游看得见 —— 这一组是本版的判据本体。"""

    def _dq(self, bee_src: str) -> dict:
        import re
        m = re.search(r'data_quality=\{(.*?)\},', bee_src, re.S)
        assert m, "没找到 data_quality 字面量"
        return m.group(1)

    def _guard_src(self):
        import inspect
        from swarm_agents.guard_bee import GuardBeeSentinel
        return inspect.getsource(GuardBeeSentinel.analyze)

    def test_crowding_marker_is_not_a_hardcoded_real(self):
        """核心回归：修复前这里是 `"crowding": "real"` 写死的。"""
        body = self._dq(self._guard_src())
        assert '"crowding": "real"' not in body, (
            "crowding 又被写死成 real —— 拥挤度不可得时下游拿到的仍是满分"
        )

    def test_degraded_marker_is_a_known_proxy_source(self):
        """降级值必须落在既有档位里，否则按契约算 0 分（= 把降级升格成全废）。"""
        src = self._guard_src()
        import re
        used = set(re.findall(r'_crowd_source = "([a-z_]+)"', src))
        assert used, "没找到 _crowd_source 的取值"
        known = QueenDistiller.REAL_SOURCES | QueenDistiller.PROXY_SOURCES
        unknown = used - known
        assert not unknown, (
            f"{unknown} 不在 REAL_SOURCES/PROXY_SOURCES 里 ⇒ _apply_triple_penalty 按 0 分计"
        )
        assert used & QueenDistiller.PROXY_SOURCES, "降级档没有落在 PROXY_SOURCES"

    def test_real_is_still_reachable(self):
        """正对照：没有这一条，一个恒报 unavailable 的实现也能满足上面两条。"""
        src = self._guard_src()
        assert '_crowd_source = "real"' in src, "拥挤度正常时已经报不出 real 了"

    def test_the_two_tiers_are_different(self):
        """两档必须真的不同分，否则这个标记对 data_real_pct 毫无作用。"""
        assert "real" in QueenDistiller.REAL_SOURCES
        assert "unavailable" in QueenDistiller.PROXY_SOURCES
        assert "unavailable" not in QueenDistiller.REAL_SOURCES, (
            "unavailable 被挪进 REAL_SOURCES 了 —— 那降级与正常同分，标记白加"
        )


class TestGuardHasNoIndependentDirection:
    """登记实测结论：Guard 的方向是其余蜂的函数，不含新信息。

    这一组**不锁行为**（是否让派生蜂投票是设计决定），只锁「输入集合」——
    哪天 Guard 真的接了一个独立观测源，它会红，提醒回来重测派生性。
    """

    def test_direction_inputs_are_all_peer_derived(self):
        import inspect
        from swarm_agents.guard_bee import GuardBeeSentinel
        src = inspect.getsource(GuardBeeSentinel.analyze)
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        head = code.split("direction = ")[0]
        # 方向的两个来源都必须来自板
        assert "self.board.detect_resonance" in head, "共振来源变了"
        assert "self._read_census" in head, "census 来源变了"

    def test_risk_adj_weight_is_zero_in_config(self):
        """Guard 分数的通道：config 里 risk_adj 归零（v0.45.172）。

        它变红不代表出错 —— 代表有人给 Guard 的**分数**恢复了权重，
        那时「Guard 只剩一张复述票」这个结论就不再成立，回来重测。
        """
        from config import EVALUATION_WEIGHTS
        assert EVALUATION_WEIGHTS["risk_adj"] == 0.0, (
            f"risk_adj 权重不再是 0（现 {EVALUATION_WEIGHTS['risk_adj']}）—— "
            "回来重测 Guard 的派生性结论"
        )

    def test_guard_is_registered_as_dissent_agent(self):
        """现状锚点：Guard 被列为异议蜂、异议时权重 ×1.5。

        实测它对**自己读的那六只**的多数**零异议**（0/90），
        所以这条加成对 Guard 而言基本是空的（BearBee 才是真异议方：异议率 66.6%）。
        """
        from config import CONFLICT_ARBITRATION_CONFIG as cfg
        assert "GuardBeeSentinel" in cfg["dissent_agents"]
        assert cfg["dissent_boost"] > 1.0


class TestCohortBoundary:
    """DQ 标记进 `data_real_pct` → `quality_factor` → `rule_score` ⇒ 口径变了，必须登记。"""

    def test_entry_registered(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        hits = [(d, v, r) for d, v, r in _COHORT_HISTORY if v == "v0.45.209"]
        assert len(hits) == 1, "v0.45.209 应恰好登记一条"
        assert "crowding" in hits[0][2]

    def test_it_extends_the_same_label(self):
        """与 v0.45.201 同日 —— 实测 09-10 起降级 0 次，本代样本逐位相同，作废 0 条。

        ⚠️ 只引用**具名的两条**，不绑队尾也不绑「前一条」：
        v0.45.201 那次就是因为绑了队尾/前一条，被同日落地的另一 session 改红两回。
        """
        from ic_rerun_readiness import _COHORT_HISTORY
        by_ver = dict((v, d) for d, v, _ in _COHORT_HISTORY)
        assert by_ver["v0.45.209"] == by_ver["v0.45.201"], (
            "本条应与 v0.45.201 共用同一日期标签，而不是新开一个空分区"
        )
