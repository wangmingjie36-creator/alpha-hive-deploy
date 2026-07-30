"""
ChronosBee 看空方向修复测试（v0.43.0）

固化一个结构性缺陷：ChronosBee **永远无法输出 bearish**
（实测 pheromone.db 里 950 条 ChronosBee 记录 bearish = 0 条），
是"信息素多5/空0 自我强化看多"的源头之一。

两处成因：
1. 评分块 `score = 5.5` 起步且三个分支全是 `score += ...`（只增不减），
   于是 `elif score <= 4.5: direction = "bearish"` **结构上不可达**
2. 唯一带方向的证据源 PEAD（财报后价格漂移）虽然写了
   `direction = "bearish"`，但它位于评分块**之前**，
   被后面的 `score = base` 与 direction 三分支无条件覆盖 —— 是死代码

修复：把 PEAD 方向证据抽成 `_apply_pead_direction()`，在评分块**之后**调用。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swarm_agents.chronos_bee import ChronosBeeHorizon


@pytest.fixture
def bee():
    return ChronosBeeHorizon.__new__(ChronosBeeHorizon)


def _pead(bias, adj=0.5):
    return {"bias": bias, "adj": adj, "drift_mean": 2.0,
            "winrate": 0.25 if bias == "bearish" else 0.75, "sample_count": 8}


class TestPeadDirectionApplied:

    def test_neutral_plus_bearish_pead_yields_bearish(self, bee):
        """核心回归：修复前此处恒为 neutral，ChronosBee 从不看空"""
        d, _ = bee._apply_pead_direction("T", "neutral", 6.0, _pead("bearish"))
        assert d == "bearish"

    def test_neutral_plus_bullish_pead_yields_bullish(self, bee):
        d, _ = bee._apply_pead_direction("T", "neutral", 6.0, _pead("bullish"))
        assert d == "bullish"

    def test_high_confidence_bullish_not_flipped(self, bee):
        """评分块已判 bullish（强催化剂）时，历史漂移不得翻转它"""
        d, _ = bee._apply_pead_direction("T", "bullish", 8.2, _pead("bearish"))
        assert d == "bullish", "高置信 bullish 不应被 PEAD 覆盖"

    def test_existing_bearish_preserved(self, bee):
        d, _ = bee._apply_pead_direction("T", "bearish", 4.0, _pead("bullish"))
        assert d == "bearish"

    @pytest.mark.parametrize("pending", [{}, {"bias": "neutral"}, {"bias": "unknown"}])
    def test_no_evidence_is_noop(self, bee, pending):
        d, s = bee._apply_pead_direction("T", "neutral", 6.0, pending)
        assert (d, s) == ("neutral", 6.0)


class TestScoreAdjustmentGated:
    """分数调整默认关闭 —— 保护 catalyst 维度 784 条历史样本的口径可比性"""

    ENV = "ALPHA_HIVE_CHRONOS_PEAD_SCORE_ADJUST"

    def test_score_unchanged_by_default(self, bee, monkeypatch):
        monkeypatch.delenv(self.ENV, raising=False)
        _, s = bee._apply_pead_direction("T", "neutral", 6.0, _pead("bearish", 0.5))
        assert s == 6.0, "默认不得改动 catalyst 分数（会破坏与历史样本的可比性）"

    def test_score_adjusted_when_enabled(self, bee, monkeypatch):
        monkeypatch.setenv(self.ENV, "1")
        _, s_bear = bee._apply_pead_direction("T", "neutral", 6.0, _pead("bearish", 0.5))
        _, s_bull = bee._apply_pead_direction("T", "neutral", 6.0, _pead("bullish", 0.4))
        assert s_bear == pytest.approx(5.5)
        assert s_bull == pytest.approx(6.4)

    def test_score_clamped_to_range(self, bee, monkeypatch):
        monkeypatch.setenv(self.ENV, "1")
        _, lo = bee._apply_pead_direction("T", "neutral", 0.2, _pead("bearish", 0.8))
        _, hi = bee._apply_pead_direction("T", "neutral", 9.8, _pead("bullish", 0.8))
        assert 0.0 <= lo <= 10.0 and 0.0 <= hi <= 10.0


class TestSourceOrderGuard:
    """源码级护栏：PEAD 方向必须在评分块**之后**施加

    这是该 bug 的本质——不是逻辑写错，是**执行顺序**错。
    只要有人把 `_apply_pead_direction` 的调用移回评分块之前，
    它就再次变成死代码，而单元测试仍会全绿（因为函数本身没坏）。
    故必须在源码顺序上加断言。
    """

    def _src(self):
        import swarm_agents.chronos_bee as m
        with open(m.__file__, encoding="utf-8") as f:
            return f.read()

    def test_pead_applied_after_scoring_block(self):
        src = self._src()
        i_score = src.find("score = base")
        i_apply = src.find("self._apply_pead_direction(")
        assert i_score > 0 and i_apply > 0, "找不到关键锚点"
        assert i_apply > i_score, (
            "_apply_pead_direction 必须在 `score = base` 之后调用；"
            "否则会被评分块无条件覆盖，重新变成死代码"
        )

    def test_pead_block_does_not_assign_direction_directly(self):
        """PEAD 采集段只能暂存证据，不得直接赋值 direction"""
        src = self._src()
        i_start = src.find("_pead_bias = _pead_data.get")
        i_end = src.find("except Exception as _e_pead")
        assert 0 < i_start < i_end
        seg = src[i_start:i_end]
        code = "\n".join(ln for ln in seg.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert 'direction = ' not in code, (
            "PEAD 采集段不得直接赋值 direction（会被评分块覆盖）——"
            "应写入 _pead_pending 由 _apply_pead_direction 施加"
        )

    def test_unreachable_branch_is_documented(self):
        """`elif score <= 4.5` 仍不可达，必须有注释说明，避免误以为它在工作"""
        src = self._src()
        i = src.find("elif score <= 4.5:")
        assert i > 0
        following = src[i:i + 400]
        assert "不可达" in following, "该分支不可达的事实必须在代码里写明"
