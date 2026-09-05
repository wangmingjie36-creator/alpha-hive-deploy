"""五维全缺时雷达图不得伪造成「正常形状」（v0.45.113 回归）

背景
----
`_radar_data` 原本是 `if dim: ... else: ...` 两支。v0.45.54 为**缺一部分维度**
定过调子并只改了 `if` 支：

    5.0 会画出一个「不好不坏」的正常形状，与真实的中性评分完全同形。
    雷达图画不出「—」，所以缺失维度画 0 —— 0 是一个塌陷的角，视觉上看得出。

**全缺走的 else 分支原封不动**：修的是缺一部分，漏的是全缺，恰好是最该管的那半。

实测（`.swarm_results_*.json` 100 份 / 1401 条目命中 7 条，全是 BRK-B，
2026-08-04~08-14）旧 else 分支产出 `[50.0, 50.0, 50.0, 66.7, 50.0]` ——
一个毫无异常的正常五边形，而背后**没有一个数字是观测值**：

    signal / catalyst  读 `self_score`，该键在两个数据源合计 **4294 条里存在 0 次**
                       （真实键名是 `score`）⇒ 恒取默认 5.0 —— 也就是说这条
                       fallback 从写下那天起就读不到真数据
    sentiment          正则 `情绪 N%` 匹配 discovery，而 discovery 是错误消息
                       ⇒ 恒取默认 50.0
    odds               读 `put_call_ratio`，键不存在 ⇒ 默认 1.0 → 66.7
    risk_adj           读 BearBee `score`，键**在**、值 5.0 —— 但那是
                       `swarm_agents/base.py` 无效 ticker 分支的硬编码常量
                       （同条目 7 只蜂全是 dimension='validation'/confidence=0.0/score=5.0）

三条本该报警的通道当时全哑：① 雷达图看起来正常 ② `dim_data_quality` 五项全 None
⇒ `_build_dim_dq_html` 返回空串、整行不渲染 ③ `_log.debug` 只覆盖缺一部分。

修法：`dimension_scores` 为空直接返回五个 0 并 `_log.warning`，不再从
`agent_details` 重建。根因（`_RE_TICKER` 不接受 `BRK-B` 的连字符）已在别处修掉，
但本分支仍可达，且它伪造数据的方式与根因无关。

⚠️ 判别力靠**成对**
------------------
只断言「全缺要画 0」不够——一个 `return [0]*5` 的粗暴修法也能全绿，
但它会把正常数据一起抹平。必须同时断言正常条目原样通过、部分缺失保持
v0.45.54 行为。
"""

import pytest

from dashboard_renderer import _build_dim_dq_html, _radar_data

# ── 生产夹具：.swarm_results_2026-08-04.json 的 BRK-B（已裁剪到相关字段）──
# 保留 agent_details 的真实形状，好证明「就算这些字段都在，也不该拿来重建」。
_VALIDATION_BEE = {"score": 5.0, "confidence": 0.0, "dimension": "validation",
                   "direction": "neutral",
                   "discovery": "无效 ticker 格式: 'BRK-B'（需 1~5 位大写字母）"}
PROD_ALL_MISSING = {
    "ticker": "BRK-B",
    "dimension_scores": {},                       # ← 空 dict，正是走 else 的条件
    "dim_data_quality": {"signal": None, "catalyst": None, "sentiment": None,
                         "odds": None, "risk_adj": None},
    "agent_details": {b: dict(_VALIDATION_BEE) for b in
                      ("ScoutBeeNova", "ChronosBeeHorizon", "BuzzBeeWhisper",
                       "OracleBeeEcho", "GuardBeeSentinel", "RivalBeeVanguard",
                       "BearBeeContrarian")},
}
# 旧 else 分支在上面这份数据上的产出，逐项算得（见模块 docstring）
OLD_FABRICATED = [50.0, 50.0, 50.0, 66.7, 50.0]

# ⚠️ dimension_scores 是 **0~10** 量表，`_d()` 再 ×10 映射到雷达图的 0~100。
# 初稿按 0~1 写夹具（0.539）导致这组测试变红——是夹具错不是代码错。
PROD_NORMAL = {   # .swarm_results_2026-09-04.json 的 MSFT 真值
    "dimension_scores": {"signal": 5.39, "catalyst": 5.90, "sentiment": 3.97,
                         "odds": 9.81, "risk_adj": 6.46},
}


class TestAllMissingCollapsesToZero:
    @pytest.mark.parametrize("dim", [{}, None], ids=["空dict", "None"])
    def test_empty_dimension_scores(self, dim):
        assert _radar_data("T", {"T": {"dimension_scores": dim}}) == [0.0] * 5

    def test_missing_key_entirely(self):
        assert _radar_data("T", {"T": {}}) == [0.0] * 5

    def test_unknown_ticker(self):
        assert _radar_data("NOPE", {}) == [0.0] * 5

    def test_production_fixture_does_not_fabricate(self):
        """核心断言：拿真实的 BRK-B 全缺条目，必须塌陷成 0，
        而不是旧 else 分支那个看起来正常的五边形。"""
        got = _radar_data("BRK-B", {"BRK-B": PROD_ALL_MISSING})
        assert got == [0.0] * 5
        assert got != OLD_FABRICATED

    def test_agent_details_are_never_used_to_rebuild(self):
        """就算 agent_details 齐全（7 只蜂都在、都有 score），
        dimension_scores 空就是空——不从别处重建。"""
        assert PROD_ALL_MISSING["agent_details"]["BearBeeContrarian"]["score"] == 5.0
        assert _radar_data("BRK-B", {"BRK-B": PROD_ALL_MISSING}) == [0.0] * 5


class TestRealDataStillPassesThrough:
    """成对的另一半。少了这组，粗暴的 `return [0]*5` 也能全绿。"""

    def test_normal_entry_unchanged(self):
        assert _radar_data("MSFT", {"MSFT": PROD_NORMAL}) == [53.9, 59.0, 39.7, 98.1, 64.6]

    def test_partial_missing_keeps_v45_54_behaviour(self):
        """缺一部分：有的维度照常，缺的画 0（不是 50）"""
        sd = {"dimension_scores": {"signal": 7.0, "catalyst": None,
                                   "sentiment": 4.0, "odds": None, "risk_adj": 6.0}}
        assert _radar_data("X", {"X": sd}) == [70.0, 0, 40.0, 0, 60.0]

    def test_bool_is_not_a_score(self):
        """True/False 是 int 子类，必须当缺失处理（v0.45.54 已有的护栏）"""
        sd = {"dimension_scores": {"signal": True, "catalyst": 5.0,
                                   "sentiment": 5.0, "odds": 5.0, "risk_adj": 5.0}}
        assert _radar_data("X", {"X": sd})[0] == 0


class TestOldBranchWasUnsalvageable:
    """把「为什么删掉而不是修好 else 分支」固化下来。"""

    def test_self_score_key_does_not_exist(self):
        """旧分支读 `self_score`，而真实数据里只有 `score`。
        生产两个数据源合计 4294 条 Scout/Chronos 条目里 `self_score` 出现 0 次
        —— 一个永远读不到真数据的 fallback，不是 fallback。"""
        bee = PROD_ALL_MISSING["agent_details"]["ScoutBeeNova"]
        assert "score" in bee
        assert "self_score" not in bee

    def test_bee_scores_are_the_error_constant(self):
        """全缺条目里 7 只蜂的 5.0 是 base.py 无效 ticker 分支的常量，
        机读签名是 dimension='validation' + confidence=0.0。
        旧分支这两个字段一个都不看，所以把常量当成了观测。"""
        for bee in PROD_ALL_MISSING["agent_details"].values():
            assert bee["dimension"] == "validation"
            assert bee["confidence"] == 0.0
            assert bee["score"] == 5.0


class TestDimDqConsumerCannotCatchAllMissing:
    """已核实的事实：消费端接不住全缺，所以雷达图是唯一通道。
    将来若有人修好 dim_dq，这条会变红，提醒同步更新上面的注释。"""

    def test_all_none_renders_nothing(self):
        assert _build_dim_dq_html(PROD_ALL_MISSING["dim_data_quality"]) == ""

    def test_normal_dq_renders_bars(self):
        html = _build_dim_dq_html({"signal": 90.0, "catalyst": 100.0,
                                   "sentiment": 100.0, "odds": 85.0, "risk_adj": 100.0})
        assert "dim-dq-row" in html and "90%" in html
