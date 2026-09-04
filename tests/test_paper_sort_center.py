"""候选排序中心必须跟着两个闸门走，不能是写死的常数（v0.45.109 回归）

背景
----
`run_for_date` 里候选按 `abs(composite_score - center)` 倒序抢资金，
`center` 原本硬编码为 `5`。那个 5 从来不是「名义中性点」——
它精确地等于当时的两闸中点 `(entry_score_bull + entry_score_bear) / 2
= (6.5 + 3.5) / 2 = 5.0`，是一个**从闸门导出的常数**。

v0.45.108 把 `entry_score_bear` 3.5→4.85，两闸中点随之移到 5.675，
硬编码的 5 却留在原地 —— 输入变了、导出值没跟着变，于是静默错位。

后果（92 个快照日实测）：C=5.0 下
    bull 候选距心域 [1.50, 3.74]
    bear 候选距心域 [0.16, 1.22]
**完全不重叠**。这不是「多头天然优先」，是每一个多头候选都压过每一个
空头候选，P(多头排在空头前) = 100.0%，与信号强弱无关。
旧配置下这测不出来：`entry_score_bear=3.5` 在全部 92 天里放行的空头候选
是 0 条，两方向从未同场竞争，这个中心从未被真实检验过。

修法与反例
----------
中心改为导出（`_candidate_sort_center()`）。C = 两闸中点时，
`abs(s - C)` 的排序**恒等于**「超出自己那侧闸门多少」的排序——
两侧的距离都只差同一个常数 (bull-bear)/2，故序不变（下有随机配对自证）。

⚠️ 不要改成 final_score 的实测中位：中位与闸门位置无关，修不动这个偏置。
   实测 P(多头排在空头前)：C=5.00 → 100.0%；C=5.35（06-01 世代中位）→ 95.9%；
   C=5.60（全期中位）→ 77.9%；C=5.675（两闸中点）→ 61.4%。
   同 v0.45.108「几何对称的 4.12 只放行 2.1%，等于没改」的教训。
"""

import random

import pytest

from paper_portfolio import CONFIG, _candidate_sort_center, _sort_candidates

# ── 生产观测夹具：2026-03-09~09-03 report_snapshots 里**过分数闸**的候选分数 ──
# （bull ≥ 6.5 共 238 条 / bear ≤ 4.85 共 45 条，此处取实测极值与若干真实值）
OBS_BULL_MIN, OBS_BULL_MAX = 6.50, 8.74
OBS_BEAR_MIN, OBS_BEAR_MAX = 3.78, 4.84
# 2026-09-03 当天真实同场竞争的两个候选（排序中心决定了谁拿到最后一格）
REAL_BULL_9_03 = ("META", 7.15)
REAL_BEAR_9_03 = ("BILI", 3.84)


def _snap(ticker, score, direction):
    return {"ticker": ticker, "composite_score": score, "direction": direction}


@pytest.fixture(autouse=True)
def _restore_center():
    """每条测试后把中心恢复成默认导出，避免串味"""
    orig = CONFIG.get("candidate_sort_center")
    yield
    CONFIG["candidate_sort_center"] = orig


class TestCenterIsDerived:
    def test_center_equals_gate_midpoint(self):
        assert _candidate_sort_center() == pytest.approx(
            (CONFIG["entry_score_bull"] + CONFIG["entry_score_bear"]) / 2.0)

    def test_center_tracks_gate_changes(self):
        """把看空闸改回旧值，中心必须自己回到 5.0——这才叫「导出」。
        写死的常数在这条上会失败。"""
        orig = CONFIG["entry_score_bear"]
        try:
            CONFIG["entry_score_bear"] = 3.5
            assert _candidate_sort_center() == pytest.approx(5.0)
        finally:
            CONFIG["entry_score_bear"] = orig

    def test_center_is_not_hardcoded_five(self):
        """现行闸门下中心必须已经离开 5——还原成硬编码 5 这条立刻变红"""
        assert _candidate_sort_center() != pytest.approx(5.0), (
            "排序中心仍是 5，但两闸中点是 "
            f"{(CONFIG['entry_score_bull'] + CONFIG['entry_score_bear']) / 2}；"
            "这是 v0.45.108 遗留的静默错位"
        )


class TestCrossDirectionInterleaving:
    def test_distance_ranges_overlap(self):
        """两侧距心域必须重叠，否则一侧对另一侧是绝对压制、排序退化成方向筛选"""
        c = _candidate_sort_center()
        bull = [abs(OBS_BULL_MIN - c), abs(OBS_BULL_MAX - c)]
        bear = [abs(OBS_BEAR_MAX - c), abs(OBS_BEAR_MIN - c)]
        assert not (min(bull) > max(bear) or min(bear) > max(bull)), (
            f"中心 {c}：bull 距心域 {bull}、bear 距心域 {bear} 不重叠——"
            "每个多头候选都压过每个空头候选，与信号强弱无关"
        )

    def test_strong_bear_outranks_marginal_bull(self):
        """2026-09-03 真实场景：BILI(bear,3.84) 超出看空闸 1.01，
        META(bull,7.15) 超出看多闸 0.65 —— 前者该排在前面。"""
        snaps = [_snap(*REAL_BULL_9_03, "bullish"), _snap(*REAL_BEAR_9_03, "bearish")]
        assert [s["ticker"] for s in _sort_candidates(snaps)] == ["BILI", "META"]

    def test_old_center_would_fail_this_guard(self):
        """反向自证：夹具与判据必须能判出旧中心 5.0 是坏的。
        少了这条，上面两条可能只是碰巧成立。"""
        CONFIG["candidate_sort_center"] = 5.0
        snaps = [_snap(*REAL_BULL_9_03, "bullish"), _snap(*REAL_BEAR_9_03, "bearish")]
        assert [s["ticker"] for s in _sort_candidates(snaps)] == ["META", "BILI"]
        bull = [abs(OBS_BULL_MIN - 5.0), abs(OBS_BULL_MAX - 5.0)]
        bear = [abs(OBS_BEAR_MAX - 5.0), abs(OBS_BEAR_MIN - 5.0)]
        assert min(bull) > max(bear)   # 旧中心下确实不重叠

    def test_median_center_would_also_fail(self):
        """实测中位（06-01 世代 5.35）同样修不动——不要退回这个方案"""
        CONFIG["candidate_sort_center"] = 5.35
        snaps = [_snap(*REAL_BULL_9_03, "bullish"), _snap(*REAL_BEAR_9_03, "bearish")]
        assert [s["ticker"] for s in _sort_candidates(snaps)] == ["META", "BILI"]


class TestWithinDirectionOrderUnchanged:
    """改中心只改多空交错，不得改同方向内部排序（换中心是零成本的前提）"""

    @pytest.mark.parametrize("center", [5.0, 5.35, 5.60, 5.675, 6.0])
    def test_same_direction_order_invariant(self, center):
        bulls = [_snap(f"B{i}", s, "bullish") for i, s in enumerate([6.52, 7.15, 8.74, 6.90])]
        bears = [_snap(f"R{i}", s, "bearish") for i, s in enumerate([4.84, 3.78, 4.60, 4.20])]
        CONFIG["candidate_sort_center"] = center
        out = [s["ticker"] for s in _sort_candidates(list(bulls + bears))]
        assert [t for t in out if t.startswith("B")] == ["B2", "B1", "B3", "B0"]  # 分数降序 8.74/7.15/6.90/6.52
        assert [t for t in out if t.startswith("R")] == ["R1", "R3", "R2", "R0"]  # 分数升序 3.78/4.20/4.60/4.84


class TestGateMidpointEqualsGateExcess:
    def test_identical_ordering_random_pairs(self):
        """C=两闸中点 时，`abs(s-C)` 排序 ≡ 「超出自己那侧闸门多少」排序。
        这是把中心定在两闸中点的**理由**，不是巧合。"""
        bull_gate, bear_gate = CONFIG["entry_score_bull"], CONFIG["entry_score_bear"]
        c = _candidate_sort_center()
        rng = random.Random(20260904)

        def draw():
            if rng.random() < 0.5:
                return "bullish", rng.uniform(bull_gate, OBS_BULL_MAX)
            return "bearish", rng.uniform(OBS_BEAR_MIN, bear_gate)

        def excess(d, s):
            return (s - bull_gate) if d == "bullish" else (bear_gate - s)

        for _ in range(5000):
            (d1, s1), (d2, s2) = draw(), draw()
            assert (abs(s1 - c) > abs(s2 - c)) == (excess(d1, s1) > excess(d2, s2))


class TestMissingScoreSortsLast:
    """兜底值必须**等于中心**：它的语义是「没分数就排最后」，只有 center 能让
    距离取到 0。

    ⚠️ 这组测试必须跨多个中心取值。只在现行中心 5.675 下断言是**恒真的**：
    真候选的最小距心是 min(6.50-5.675, 5.675-4.84) = 0.825，而兜底写死 5
    也只给出 0.675 < 0.825，无分快照照样排最后——把兜底改回 5 测试仍全绿
    （已实测：M3 变异 14 passed）。这正是 v0.45.71「守卫自己恒真」的形态，
    判据只能来自 mutation check，不能来自断言看起来对不对。
    """

    # 中心取值须避开夹具里的实际分数：center=6.50 与 OBS_BULL_MIN 重合，
    # 双方距心都是 0，稳定排序下退化成"看谁原本在前"，测的不再是兜底值。
    @pytest.mark.parametrize("center", [4.85, 5.675, 6.4])
    def test_scoreless_snapshot_goes_last(self, center):
        CONFIG["candidate_sort_center"] = center
        snaps = [_snap("NOSCORE", None, "bearish"),
                 _snap("BULL", OBS_BULL_MIN, "bullish"),
                 _snap("BEAR", OBS_BEAR_MAX, "bearish")]
        assert _sort_candidates(snaps)[-1]["ticker"] == "NOSCORE", (
            f"中心 {center}：无分快照没有排在最后——兜底值与中心脱钩了"
        )
