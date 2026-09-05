"""拿不到 composite_score 时必须拒绝，不能用 0 顶替（v0.45.110 回归）

背景
----
`_should_open` 原本用 `float(snapshot.get("composite_score") or 0)` 取分。
这道闸门的**两个分支都是「取反才拒绝」**：

    if "bull" in direction and score < entry_score_bull:  return False
    if "bear" in direction and score > entry_score_bear:  return False

于是任何让比较返回 `False` 的值都会**自动放行**。`or 0` 只是三个洞里最窄的一个：

    缺键 / None / 0  → 0.0，`0 > 4.85` 为假        ⇒ 穿透**看空**侧
    NaN             → 任何比较都是 False          ⇒ **两侧同时**穿透
    +inf            → `inf < 6.5` 为假            ⇒ 以「满分看多」身份进场
    -inf            → `-inf > 4.85` 为假          ⇒ 穿透看空侧

而且三者都带着 `conf=high` 出来——`_infer_confidence` 根本不看分数
（那个函数里的 `score = ...` 是死读，v0.45.110 已删；ruff F841 本可抓到，
但本仓 pyproject 全局 ignore 了它）。

排序键 `_sort_candidates` 有**独立**的同类洞：`float(... or center)` 挡得住
缺键/None（None 是 falsy），挡不住 NaN（truthy）。`abs(nan - center)` 仍是 NaN，
NaN 进排序键 ⇒ 名次未定义（v0.45.93「NaN 进排序函数」原型）。
**排序发生在 `_should_open` 之前**，所以闸门那道守卫救不了排序，两处必须各自堵。

修法
----
统一走 `_snapshot_score()`：缺失 / None / 非数 / 非有限 → `None`，
由调用方显式拒绝。这是 v0.45.3「安全默认值」判据的正例——
问「这个默认值会不会让下游误以为掌握了信息」，`or 0` 会，所以不给默认值。

生产影响：92 天 1051 条 snapshot 里缺失或为 0 的 **0 条**，
故本次是堵潜在陷阱，不改变任何历史行为（A/B 回放逐笔全等已验证）。
"""

import math

import pytest

from paper_portfolio import (
    CONFIG,
    _infer_confidence,
    _should_open,
    _snapshot_score,
    _sort_candidates,
)

# 会让「取反才拒绝」的闸门放行的全部退化取值
DEGENERATE = [
    pytest.param({}, id="缺键"),
    pytest.param({"composite_score": None}, id="None"),
    pytest.param({"composite_score": float("nan")}, id="NaN"),
    pytest.param({"composite_score": float("inf")}, id="+inf"),
    pytest.param({"composite_score": float("-inf")}, id="-inf"),
    pytest.param({"composite_score": "N/A"}, id="非数字符串"),
]
# 真实出现过的分数（2026-06-01~09-03 世代），改后必须仍然开得出来
REAL_BULL, REAL_BEAR = 7.15, 4.60


def _snap(extra, direction):
    return {"ticker": "TEST", "direction": direction, **extra}


class TestSnapshotScoreIsHonest:
    @pytest.mark.parametrize("extra", DEGENERATE)
    def test_degenerate_returns_none(self, extra):
        assert _snapshot_score(_snap(extra, "bearish")) is None

    @pytest.mark.parametrize("raw", [0, 0.0, 3.78, 4.85, 5.0, 6.5, 8.74])
    def test_finite_scores_pass_through(self, raw):
        """0.0 是**合法分数**不是哨兵。本次只区分「拿不到」与「拿到了」，
        不新增分数合理区间——那需要单独的校准依据。
        生产 1051 条里 composite_score 为 0 的有 0 条，此分支实际不触发。"""
        assert _snapshot_score(_snap({"composite_score": raw}, "bearish")) == pytest.approx(raw)


class TestGateRejectsDegenerateScores:
    @pytest.mark.parametrize("extra", DEGENERATE)
    @pytest.mark.parametrize("direction", ["bullish", "bearish"])
    def test_both_directions_rejected(self, extra, direction):
        ok, why = _should_open(_snap(extra, direction), set(), as_of="2026-09-03")
        assert not ok, f"{direction} + {extra} 通过了闸门：{why}"
        assert "composite_score" in why

    def test_nan_would_pass_both_gates_under_old_code(self):
        """反向自证：判据必须能判出旧写法是坏的。
        `or 0` 下 NaN 会同时穿透两侧——这里直接验那两个比较的真值，
        证明夹具选得对，不是碰巧。"""
        nan = float("nan")
        assert not (nan < CONFIG["entry_score_bull"])   # bull 侧不拒绝
        assert not (nan > CONFIG["entry_score_bear"])   # bear 侧也不拒绝
        # 而 `or 0` 对缺分只穿透看空侧
        assert not (0.0 > CONFIG["entry_score_bear"])
        assert 0.0 < CONFIG["entry_score_bull"]

    def test_real_scores_still_open(self):
        """护栏：别把真候选一起拒了"""
        assert _should_open(_snap({"composite_score": REAL_BULL}, "bullish"),
                            set(), as_of="2026-09-03")[0]
        assert _should_open(_snap({"composite_score": REAL_BEAR}, "bearish"),
                            set(), as_of="2026-09-03")[0]


class TestSortKeyNeverGoesNaN:
    """排序在闸门**之前**跑，闸门的守卫救不了它——必须单独测。"""

    @pytest.mark.parametrize("extra", DEGENERATE)
    def test_degenerate_sorts_last(self, extra):
        snaps = [_snap(extra, "bearish") | {"ticker": "BAD"},
                 _snap({"composite_score": 6.5}, "bullish") | {"ticker": "BULL"},
                 _snap({"composite_score": 4.84}, "bearish") | {"ticker": "BEAR"}]
        assert [s["ticker"] for s in _sort_candidates(snaps)][-1] == "BAD"

    def test_no_sort_key_is_nan(self):
        """直接验排序键本身有限——比只看最终顺序更早失败，
        且不依赖 NaN 在某个 CPython 版本下的稳定排序表现。"""
        center = (CONFIG["entry_score_bull"] + CONFIG["entry_score_bear"]) / 2
        for extra in [{}, {"composite_score": None}, {"composite_score": float("nan")},
                      {"composite_score": float("inf")}]:
            score = _snapshot_score(_snap(extra, "bearish"))
            key = 0.0 if score is None else abs(score - center)
            assert math.isfinite(key), f"{extra} 产生了非有限排序键 {key}"


class TestInferConfidenceIgnoresScore:
    """置信度不看分数是**已知设计**（只看 dim_std 与 bear_signals）。
    固化下来，免得后来者看到「缺分快照拿到 high」以为是这里的 bug。"""

    def test_score_does_not_affect_tier(self):
        base = {"ticker": "T", "direction": "bearish", "agent_votes": {}, "bear_signals": []}
        tiers = {_infer_confidence({**base, "composite_score": s})
                 for s in (0.0, 3.78, 5.0, 8.74)}
        assert tiers == {"high"}
        assert _infer_confidence(base) == "high"   # 完全没有 composite_score 也一样
