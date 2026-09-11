"""CodeExecutorAgent：兜底分支的「数据可用 ⇒ 看多」范畴错误，以及五个死方法（v0.45.191）

背景（`pheromone.db::agent_memory` 台账实测，非推断）
----------------------------------------------------
`analyze()` 第 6 步「如果分析失败，返回原始数据结果」的兜底原本是：

    if price and market_cap:  score = 6.0; direction = "bullish"
    else:                     score = 5.0; direction = "neutral"

**「价格与市值都拿到了」⇒「看多」是范畴错误** —— 数据可用性不含方向信息。
台账（2026-04-06 ~ 09-10，18,120 行）：`code_executor_data` 共 870 条，
**870 条（100%）是 `6.0 / bullish`**，`else` 分支五个月一次没走过
⇒ 它是一张恒定的看多票，不是观测。

v0.43.10（2026-08-12）修的是**上游**——技术分析脚本撞 yfinance MultiIndex 崩溃，
使这条兜底被 100% 走到。台账逐日确认那次修复**属实**（`code_executor_analysis`
在 2026-08-14 之前 0 行、之后 570 行；恒定占比 100% → 3.1%），
但**兜底本身的范畴错误原样留了下来**，2026-08 触发 45 次、2026-09 触发 3 次。

为什么必须配机读标记
--------------------
方向改成 neutral 之后，「兜底的中性」与「真的中性」在特征上同形 ——
正是 v0.45.151「缺失哨兵选中众数」的形状。故用 `data_quality` 打标记，
且值必须落在 `QueenDistiller.PROXY_SOURCES` 里，否则 DQ 汇总会把它当成
0 质量的未知源静默处理（CLAUDE.md 硬检查项：「这个失败，下游怎么知道？」）。

⚠️ 这条改动**会**动 `final_score`：`_compute_direction_vote` 的
`bullish_count` / `bullish_w`（queen_distiller.py:567/595）遍历**全部**
`valid_results`，不看维度 —— CodeExecutorAgent 虽是 `technical` 维（不进五维
加权），它的**方向票照样算**。故本版追加世代边界。
"""

import json

import pytest

from code_executor_agent import CodeExecutorAgent
from pheromone_board import PheromoneBoard
from swarm_agents.queen_distiller import QueenDistiller


class _FakeExecutor:
    """第 1 次调用（取数）成功并返回合法 JSON；第 2 次（技术分析）返回非 JSON。

    这正是生产上走到兜底的那条路：取数成功、技术分析脚本的输出解析失败。
    """

    def __init__(self, fetch_payload, analysis_stdout):
        self.fetch_payload = fetch_payload
        self.analysis_stdout = analysis_stdout
        self.calls = 0

    def execute_python(self, code, *a, **k):
        self.calls += 1
        if self.calls == 1:
            return {"success": True, "stdout": json.dumps(self.fetch_payload),
                    "stderr": ""}
        return {"success": True, "stdout": self.analysis_stdout, "stderr": ""}


def _agent(monkeypatch, fetch_payload, analysis_stdout, price=100.0):
    board = PheromoneBoard()
    a = CodeExecutorAgent(board, executor=_FakeExecutor(fetch_payload, analysis_stdout))
    monkeypatch.setattr(a, "_get_stock_data", lambda t: {"price": price})
    return a, board


_FULL = {"current_price": 100.0, "market_cap": 1_000_000_000}
_PARTIAL = {"current_price": 100.0}          # 没有 market_cap ⇒ 旧代码的 else 支
_BAD_JSON = "not json at all"


def _board_entry(board, ticker="TEST"):
    """取 CodeExecutorAgent 发布的**结论**条目（排除开头那条 5.0 进度标记）。"""
    ents = [e for e in board._entries
            if e.ticker == ticker and e.agent_id == "CodeExecutorAgent"]
    assert ents, "CodeExecutorAgent 一条都没发布"
    return ents[-1]


# ════════════════════════════════════════════════════════════════════════════
# 1. 兜底不再是一张看多票
# ════════════════════════════════════════════════════════════════════════════

class TestFallbackIsNotABullishVote:

    @pytest.mark.parametrize("payload,label", [(_FULL, "价格+市值齐全"),
                                               (_PARTIAL, "只有价格")])
    def test_fallback_direction_is_neutral(self, monkeypatch, payload, label):
        """两条子支都不许给方向 —— 拿没拿到数据只决定说辞，不决定方向。"""
        a, board = _agent(monkeypatch, payload, _BAD_JSON)
        r = a.analyze("TEST")
        assert r["direction"] == "neutral", f"{label}: 数据可用性被当成了方向信息"
        assert _board_entry(board).direction == "neutral", f"{label}: 板上那条仍是方向票"

    def test_fallback_score_is_scale_neutral(self, monkeypatch):
        """6.0 在 0~10 量表上是偏多的一档；无观点就该落中性点 5.0。"""
        a, board = _agent(monkeypatch, _FULL, _BAD_JSON)
        r = a.analyze("TEST")
        assert r["score"] == 5.0
        assert _board_entry(board).self_score == 5.0

    def test_real_analysis_still_emits_direction(self, monkeypatch):
        """**成对**：防「一律 neutral」的偷懒修法 —— 真分析出结论时照常给方向。"""
        a, board = _agent(monkeypatch, _FULL,
                          json.dumps({"sma_20": 90.0, "signal": "超买"}))
        r = a.analyze("TEST")
        assert r["direction"] == "bearish", "真实分析结论的方向被一起抹平了"
        assert r["score"] == 3.0


# ════════════════════════════════════════════════════════════════════════════
# 2. 兜底必须机读可辨（否则与「真的中性」同形）
# ════════════════════════════════════════════════════════════════════════════

class TestFallbackIsMachineReadable:

    def test_fallback_is_marked_in_data_quality(self, monkeypatch):
        a, _ = _agent(monkeypatch, _FULL, _BAD_JSON)
        dq = a.analyze("TEST").get("data_quality") or {}
        assert dq.get("technical") == "fallback", (
            f"兜底没留机读痕迹：data_quality={dq}；"
            "「兜底的中性」与「真的中性」就此同形")

    def test_marker_is_a_known_proxy_source(self, monkeypatch):
        """标记值必须是 DQ 汇总认得的档位。

        `QueenDistiller._apply_triple_penalty` 只认 `REAL_SOURCES`(1.0) /
        `PROXY_SOURCES`(0.7)，其余一律按 0 计。写一个它不认的字面量，
        等于把「降级」悄悄升格成「数据全废」——一个没人会红的错。
        """
        a, _ = _agent(monkeypatch, _FULL, _BAD_JSON)
        v = (a.analyze("TEST").get("data_quality") or {}).get("technical")
        assert v in QueenDistiller.PROXY_SOURCES, (
            f"{v!r} 不在 PROXY_SOURCES 里，DQ 汇总会把它当 0 质量")

    def test_successful_analysis_is_not_marked_fallback(self, monkeypatch):
        """**成对**：防「永远打 fallback 标记」的偷懒修法。"""
        a, _ = _agent(monkeypatch, _FULL,
                      json.dumps({"sma_20": 90.0, "signal": "超买"}))
        dq = a.analyze("TEST").get("data_quality") or {}
        assert dq.get("technical") != "fallback"


# ════════════════════════════════════════════════════════════════════════════
# 3. 五个死方法已删
# ════════════════════════════════════════════════════════════════════════════

class TestDeadMethodsRemoved:
    """判据两条独立、各有正对照：

    · 静态：AST 全仓零调用点（`analyze` 的 17 个生产调用点证明扫描器有效）；
    · 运行时：`agent_memory` 台账五个月零执行 —— `code_executor_success` /
      `_fixed` / `_error` 三个独有 source 各 0 行，而同表同 agent 的
      `analyze` 三个 source 共 3,884 行证明台账记得住这只蜂。
    字符串引用 / 仓库外调用者 / 动态派发三个盲区均已查空。
    """

    @pytest.mark.parametrize("name", [
        "execute_and_analyze", "auto_debug", "generate_data_fetch_code",
        "generate_analysis_code", "generate_visualization_code",
    ])
    def test_dead_method_is_gone(self, name):
        assert not hasattr(CodeExecutorAgent, name), (
            f"{name} 仍在；它零调用点且五个月零执行，"
            "且内含「执行成功 ⇒ 8.0 看多」的同款范畴错误")

    def test_live_method_still_exists(self):
        """**正对照**：防「把整个类删空」也能让上面全绿。"""
        assert callable(getattr(CodeExecutorAgent, "analyze", None))


# ════════════════════════════════════════════════════════════════════════════
# 4. 世代边界
# ════════════════════════════════════════════════════════════════════════════

class TestCohortBoundary:
    """方向票直通 `rule_direction` → `final_score` ⇒ 口径变了，必须登记。"""

    def test_this_version_is_registered(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        hits = [(d, v, r) for d, v, r in _COHORT_HISTORY if v == "v0.45.191"]
        assert len(hits) == 1, "v0.45.191 的世代边界应恰好登记一条"
        assert "CodeExecutor" in hits[0][2]

    def test_it_extends_the_same_label_not_a_new_partition(self):
        """边界日期与 v0.45.176 同为 2026-09-10 —— 实测 09-10 起兜底触发 0 次，
        本代已有的 30 条样本改动前后逐位相同，**不该**被这条作废。

        ⚠️ 这里断言的是「**本条**与 v0.45.176 同日」，**不是**「本条在队尾」。
        v0.45.201 实测教训：原写法是 `cohort_start()["date"] == "2026-09-10"`，
        它把一条关于本条目的主张绑在了列表尾部上 —— 此后**任何一条合法的新边界**
        都会让它变红，而红的原因与 CodeExecutor 毫无关系。
        （同族：v0.45.191 当时也正是这样改红了 test_oracle_cboe_source。）
        """
        from ic_rerun_readiness import _COHORT_HISTORY
        by_ver = dict((v, d) for d, v, _ in _COHORT_HISTORY)
        assert by_ver["v0.45.191"] == "2026-09-10"
        assert by_ver["v0.45.191"] == by_ver["v0.45.176"], (
            "本条应与 v0.45.176 共用同一个日期标签，而不是新开一个更晚的空分区"
        )

    def test_history_stays_append_only_and_monotonic(self):
        from ic_rerun_readiness import _COHORT_HISTORY
        dates = [d for d, _, _ in _COHORT_HISTORY]
        assert dates == sorted(dates), "只追加、按时间递增"
