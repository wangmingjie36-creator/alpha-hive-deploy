"""看空开仓闸门必须落在实际分数分布够得着的地方（v0.45.108 回归）

背景
----
`_should_open` 的两道闸门以 5 分为中性点、各留 1.5 分：
    bullish 需 `composite_score >= entry_score_bull`（6.5）
    bearish 需 `composite_score <= entry_score_bear`（原 3.5）
候选排序也是按 `abs(score - 5)` 倒序，可见「5 = 中性」是设计意图。

但 `final_score` 的实际分布并不以 5 为中心，而是右移且下尾被压短：
生产 `predictions` 表 2026-03-09~09-03 共 1127 条，中位 5.52、81.5% 在 5 分以上，
**下尾最低只到 3.19**（唯一一条，2026-03-13 TSLA，组合成立第 3 天）。
于是 `<= 3.5` 这道闸门六个月里只够到过 1 次，四月起每月都是 0 次——
纸面组合 60 笔（45 已平 + 15 在手）**全是多头，零空单**。

这是「闸门恒假」，与 v0.45.71 记的「守卫恒真」是同一枚硬币的两面：
代码路径没毛病、测试全绿，条件在真实数据上永远取不到。
查这类问题不能读代码，只能拿生产数据数这个分支被走过几次。

修法
----
让两侧闸门在**各自方向的样本里命中率相当**，而不是在分数上几何对称
（几何对称的 4.12 只放行 191 条 bearish 里的 4 条，等于没改）。
`entry_score_bear = 4.85` → 放行 25.1%，对上 bullish 侧的 25.7%。

下面的十分位夹具取自生产 `predictions`（2026-06-01~09-03，中位稳定在 5.25~5.37
的同一世代；四、五月中位 6.19/5.79 属另一世代，不混入）。
"""

import pytest

from paper_portfolio import CONFIG, _should_open

# ── 生产观测夹具：2026-06-01~09-03 各方向分数的十分位（p0,p10,...,p100）──
BULLISH_DECILES = [3.80, 4.72, 4.96, 5.14, 5.29, 5.51, 5.82, 6.20, 6.63, 7.15, 8.74]
BEARISH_DECILES = [3.78, 4.60, 4.78, 4.92, 5.05, 5.14, 5.25, 5.42, 5.68, 6.25, 7.86]
NEUTRAL_CENTER = 5.0          # 设计上的中性点（排序键 abs(score-5) 也用它）
OBSERVED_BEARISH_MIN = 3.78   # 该世代 bearish 的实际最低分


def _hit_rate(deciles, gate, side):
    """闸门在这条分布上放行多少比例（十分位线性插值，返回 %）"""
    for i in range(len(deciles) - 1):
        lo, hi = deciles[i], deciles[i + 1]
        if lo <= gate <= hi:
            frac = 0.0 if hi == lo else (gate - lo) / (hi - lo)
            below = (i + frac) * 10.0
            break
    else:
        below = 0.0 if gate < deciles[0] else 100.0
    return below if side == "bear" else 100.0 - below


class TestBearGateReachable:
    def test_gate_above_observed_floor(self):
        """闸门必须高于观测到的最低分，否则它永远取不到"""
        gate = CONFIG["entry_score_bear"]
        assert gate > OBSERVED_BEARISH_MIN, (
            f"看空闸门 {gate} ≤ 该世代 bearish 实测最低分 {OBSERVED_BEARISH_MIN}，"
            "这道闸门在生产数据上永远为假——空单一笔都开不出来"
        )

    def test_gate_stays_below_neutral(self):
        """另一边的护栏：闸门不能松到中性点以上，
        否则「标 bearish 但分数在看多侧」的样本也会被开空
        （该世代 263 条 bearish 里有 162 条分数 > 5）"""
        assert CONFIG["entry_score_bear"] < NEUTRAL_CENTER


class TestGateSymmetry:
    def test_hit_rates_comparable(self):
        """两侧闸门在各自方向样本里的命中率不应相差超过 10 个百分点。
        这才是「对称」该有的含义——分数上的几何对称在右移+下尾压短的
        分布里会退化成单边策略。"""
        bull = _hit_rate(BULLISH_DECILES, CONFIG["entry_score_bull"], "bull")
        bear = _hit_rate(BEARISH_DECILES, CONFIG["entry_score_bear"], "bear")
        assert abs(bull - bear) <= 10.0, (
            f"看多侧放行 {bull:.1f}%、看空侧放行 {bear:.1f}%，相差 {abs(bull-bear):.1f}pp；"
            "组合会退化成结构性只做多"
        )

    def test_old_threshold_would_fail_this_guard(self):
        """反向自证：夹具与判据必须能判出旧值 3.5 是坏的。
        少了这条，上面两个断言可能只是碰巧成立。"""
        assert _hit_rate(BEARISH_DECILES, 3.5, "bear") == 0.0
        bull = _hit_rate(BULLISH_DECILES, 6.5, "bull")
        assert abs(bull - 0.0) > 10.0


class TestShouldOpenHonorsGate:
    @staticmethod
    def _snap(score, direction):
        # 空 agent_votes + 无 bear_signals → _infer_confidence 返回 "high"，
        # 稳过 entry_conf_min，让这组测试只测分数闸门本身
        return {"ticker": "TEST", "composite_score": score, "direction": direction}

    def test_bear_at_gate_opens(self):
        ok, why = _should_open(self._snap(CONFIG["entry_score_bear"], "bearish"),
                               set(), as_of="2026-09-03")
        assert ok, why

    def test_bear_just_above_gate_rejected(self):
        ok, why = _should_open(self._snap(CONFIG["entry_score_bear"] + 0.01, "bearish"),
                               set(), as_of="2026-09-03")
        assert not ok and "bear" in why

    @pytest.mark.parametrize("score", [4.60, 4.78, 4.85])
    def test_real_observed_bearish_scores_now_tradeable(self, score):
        """夹具里 p10~p20 那几档是真实出现过的分数，改后必须开得出来"""
        ok, _ = _should_open(self._snap(score, "bearish"), set(), as_of="2026-09-03")
        assert ok

    def test_bullish_side_unchanged(self):
        assert _should_open(self._snap(6.5, "bullish"), set(), as_of="2026-09-03")[0]
        assert not _should_open(self._snap(6.49, "bullish"), set(), as_of="2026-09-03")[0]
