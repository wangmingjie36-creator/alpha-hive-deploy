"""信息素板的墙钟过期线必须大于 Step 2 超时预算（v0.45.518）。

为什么要把两个相隔很远的数焊在一起
-----------------------------------
`PheromoneBoard` 条目超过 `ENTRY_MAX_AGE_S` 就被当作陈货：`publish` 的存活过滤丢弃它，`get_agent_entry` /
普查视图对它返回 None。Guard / Bear 经 `BeeAgent._read_peer` 读前面蜂的结论，读到 None 就**静默按「没有这个同伴」评分**。

此前这条线是 3600s，恰好等于编排器 Step 2 的超时预算（30 只 × 120s）。于是「能跑完的扫描，条目年龄必小于过期线」
成立——但**是碰巧成立**，没有任何地方写着它、更没有测试守着。预算放宽到 180s/只（5400s）后，拖到后半段的标的
（顺序段逐只处理，条目在 Phase 1 早早发布）就会读不到前面蜂的结论：扫描「跑完了」，质量悄悄变差，而且**没人会红**。
正是 CLAUDE.md 那条「这个失败，下游怎么知道？」的形状。

本文件的判据：过期线 > 预算（含名单再长一点的余量），且板里不再有第二处裸 `3600` 字面量。
"""
import ast
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pheromone_board import PheromoneBoard, PheromoneEntry
from tests._orchestrator import repo_orchestrator_text

REPO = Path(__file__).resolve().parent.parent

#: 名单再多这么多只，预算仍不得越过过期线（WATCHLIST 是会变的，见编排器 TICKER_COUNT 的注释）。
GROWTH_HEADROOM_TICKERS = 10

#: 09-23（CBOE 陈旧级联）外推 30 只需要 ≈4900s。预算低于它，那一类日子又会整天丢失。
#: 这是个**变更探测器**：谁要调低系数，先回去读 CHANGELOG v0.45.518 的取证，再来改这个数。
MIN_BUDGET_FOR_30_TICKERS_S = 4900


def _budget_formula(orch_text: str):
    """→ (每只秒数, 下限秒数, 日志里写的每只秒数)；任何一项找不到或不唯一都抛，不猜。"""
    code = "\n".join(ln for ln in orch_text.splitlines() if not ln.lstrip().startswith("#"))
    coef = re.findall(r"^STEP2_TIMEOUT=\$\(\( TICKER_COUNT \* (\d+) \)\)\s*$", code, re.M)
    floor = re.findall(r'^\[ "\$STEP2_TIMEOUT" -lt (\d+) \] && STEP2_TIMEOUT=(\d+)\s*$', code, re.M)
    logged = re.findall(r"× (\d+)s/只，下限 (\d+)s", code)
    assert len(coef) == 1, f"STEP2_TIMEOUT 的『按只数算』公式找不到或不唯一：{coef}"
    assert len(floor) == 1 and floor[0][0] == floor[0][1], f"STEP2_TIMEOUT 下限行找不到或不自洽：{floor}"
    assert len(logged) == 1, f"超时限制日志行找不到或不唯一：{logged}"
    return int(coef[0]), int(floor[0][0]), int(logged[0][0])


def _budget(n_tickers: int, coef: int, floor: int) -> int:
    return max(n_tickers * coef, floor)


def _n_watchlist() -> int:
    import config
    return len(config.WATCHLIST)


class TestExpiryExceedsBudget:

    def test_formula_is_parseable_and_log_line_does_not_lie(self):
        coef, floor, logged = _budget_formula(repo_orchestrator_text())
        assert logged == coef, f"日志写的每只 {logged}s 与公式里的 {coef}s 不一致——日志说谎"

    def test_expiry_line_is_larger_than_the_step2_budget(self):
        coef, floor, _ = _budget_formula(repo_orchestrator_text())
        n = _n_watchlist() + GROWTH_HEADROOM_TICKERS
        budget = _budget(n, coef, floor)
        assert PheromoneBoard.ENTRY_MAX_AGE_S > budget, (
            f"信息素板过期线 {PheromoneBoard.ENTRY_MAX_AGE_S}s ≤ Step 2 预算 {budget}s（{n} 只 × {coef}s）。"
            "能跑到预算尽头的扫描，后半段标的的 Guard / Bear 会读到「同伴条目已过期」= None，静默按没有同伴评分。"
            "要么放宽 `PheromoneBoard.ENTRY_MAX_AGE_S`，要么降低预算——两个数必须一起改。")

    def test_budget_still_covers_the_0923_extrapolation(self):
        coef, floor, _ = _budget_formula(repo_orchestrator_text())
        assert _budget(30, coef, floor) >= MIN_BUDGET_FOR_30_TICKERS_S, (
            f"30 只预算 {_budget(30, coef, floor)}s < {MIN_BUDGET_FOR_30_TICKERS_S}s：09-23 那类 CBOE 陈旧日又会超时、整天丢失。"
            "先读 CHANGELOG v0.45.518 的取证再改。")

    def test_has_teeth(self):
        """正对照先过（上面几条）；再把两种坏法种进去，必须都被判出来。"""
        text = repo_orchestrator_text()
        coef, floor, _ = _budget_formula(text)
        # 坏法 1：预算越过过期线（系数改大）
        assert _budget(_n_watchlist() + GROWTH_HEADROOM_TICKERS, coef * 10, floor) >= PheromoneBoard.ENTRY_MAX_AGE_S
        # 坏法 2：日志写的和公式不一致
        lied = text.replace("× 180s/只", "× 120s/只")
        if lied != text:
            c2, _f2, l2 = _budget_formula(lied)
            assert l2 != c2


class TestOneExpiryLine:

    def test_no_bare_3600_literal_left_in_the_board(self):
        """两处判定（publish 的存活过滤 / get_agent_entry）必须引用同一个常量：只改一处，同一条目会在排行榜里
        已过期、在定点查询里还活着。AST 认整数字面量（注释 / 说明文字里写 3600 不算）。"""
        tree = ast.parse((REPO / "pheromone_board.py").read_text(encoding="utf-8"))
        hits = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and n.value == 3600
                and not isinstance(n.value, bool)]
        assert not hits, f"pheromone_board.py 里又出现了裸 3600 字面量（行 {hits}）——请引用 ENTRY_MAX_AGE_S"

    def test_agent_entry_threshold_is_the_same_constant(self):
        assert PheromoneBoard._AGENT_ENTRY_MAX_AGE_S == PheromoneBoard.ENTRY_MAX_AGE_S


def _entry(agent, ticker, age_s):
    e = PheromoneEntry(agent_id=agent, ticker=ticker, discovery="x", source="test", self_score=6.0, direction="bullish")
    e.timestamp = (datetime.now() - timedelta(seconds=age_s)).isoformat()
    return e


class TestBehaviourAroundTheLine:
    """成对断言（只断言「放宽了」会放过「全放开」）：旧过期线之后、新过期线之前的条目活着；越过新线的条目照拦。"""

    def test_entry_past_the_old_3600s_line_is_still_readable_after_a_long_scan(self):
        b = PheromoneBoard()
        b.publish(_entry("OracleBeeEcho", "TEST", 3700))          # 旧线 3600 之后、新线之前：长扫描后半段读它
        b.publish(_entry("Filler", "OTHER", 0))                    # 触发 publish 的存活过滤
        got = b.get_agent_entry("TEST", "OracleBeeEcho")
        assert got is not None, "长扫描后半段读不到前面蜂的条目——正是本版要防的静默降级"

    @pytest.mark.parametrize("via", ["get_agent_entry", "publish_filter"])
    def test_entry_past_the_new_line_is_rejected(self, via):
        b = PheromoneBoard()
        old = _entry("OracleBeeEcho", "TEST", PheromoneBoard.ENTRY_MAX_AGE_S + 60)
        b.publish(old)
        if via == "get_agent_entry":
            assert b.get_agent_entry("TEST", "OracleBeeEcho") is None
        else:
            b.publish(_entry("Filler", "OTHER", 0))
            assert not [e for e in b.snapshot() if e["ticker"] == "TEST"], "超龄条目没被 publish 的存活过滤丢掉"
