"""bootstrap 与点估计必须共用同一套时间衰减（v0.45.144 回归）

修复前的形状：
    compute_new_weights_wls  带 exp(−days/30) 时间衰减
    bootstrap_validate       完全不衰减，838 条等权

于是闸门拿「带衰减的点估计」去比「不带衰减的重采样分布」——**两个不同的
估计量**。闸门红不红反映的是「近期口径与全历史口径差多少」，而不是它
自称的「权重估计稳不稳」。

判别性实测（生产数据 N=838）：
  · 把**不衰减**的点估计送进旧闸 → 5/5 全部落在 CI 内
  · 带衰减的点估计 → catalyst 超上界 1.6×、sentiment 低于下界 2.3× 半宽
  ⇒ 出界 100% 来自口径差，与抽样噪声无关。

还有一条反直觉后果，也是这个 bug 最会误导人的地方：CI 宽 ∝ 1/√N，而
口径差造成的系统性偏移**不随 N 缩小** ⇒ 样本越多闸门越容易红。
"等攒够样本它就转绿"是错的方向。

本文件把「抽样分布必须以点估计为中心」固化成断言，并配一个**靶向**
mutation 证明它有判别力。

⚠️ mutation 必须只打掉 bootstrap 一侧的衰减。改 `TIME_DECAY_TAU_DAYS`
常数是无效 mutation —— 两条路径现在共用它，一起失效后中心仍然对齐，
会得出"检验没有判别力"的假结论（本次开发中真的先踩了这一脚）。
"""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weekly_optimizer as wo

RECENT_DAYS = 5
OLD_DAYS = 200
N_PER_REGIME = 40


class _FakeSnap:
    """最小快照替身：只带被测路径用到的字段。"""

    def __init__(self, entry, t7, votes, date):
        self.direction = "bullish"
        self.entry_price = entry
        self.actual_price_t7 = t7
        self.agent_votes = votes
        self.date = date


class _FakeAnalyzer:
    """替换 BacktestAnalyzer，避免测试碰 report_snapshots/ 与 pheromone.db。"""

    def __init__(self, snaps):
        self.snapshots = list(snaps)


def _votes(i, catalyst_pct, sentiment_pct):
    """构造一组票。价格恒为上涨，故 vote>5 判对、vote<5 判错。

    命中率用 `i % 10` 铺开而不是随机数：夹具本身要确定，随机只留给
    bootstrap 自己。三个基线维度固定 60% —— **不能设成 100%**，否则该维度
    CI 宽度为 0，中心性断言在一个退化分布上恒真（也正是这个退化夹具
    先前逼出了 bootstrap_validate 拿舍入后的界做判定的潜伏缺陷）。
    """
    hit = i % 10

    def vote(pct):
        return 8.0 if hit < pct // 10 else 2.0

    return {
        "ScoutBeeNova": vote(60),         # signal   —— 两个世代同为 60%
        "OracleBeeEcho": vote(60),        # odds
        "GuardBeeSentinel": vote(60),     # risk_adj
        "ChronosBeeHorizon": vote(catalyst_pct),
        "BuzzBeeWhisper": vote(sentiment_pct),
    }


def _regime_split_snapshots(now):
    """近期与远期口径相反：catalyst 近 90%/远 30%，sentiment 近 30%/远 90%。

    不衰减时两者都被摊平到 60%（与基线维度无异）；带衰减时近期几乎独占
    权重（e^−5/30 ≈ 0.85 vs e^−200/30 ≈ 0.0013，约 660:1），两维分开到
    90% / 30%。两种口径的差距远大于抽样噪声，mutation 才打得中。
    """
    import datetime as _dt

    snaps = []
    recent = (now - _dt.timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    old = (now - _dt.timedelta(days=OLD_DAYS)).strftime("%Y-%m-%d")
    for i in range(N_PER_REGIME):
        snaps.append(_FakeSnap(100.0, 110.0, _votes(i, 90, 30), recent))
    for i in range(N_PER_REGIME):
        snaps.append(_FakeSnap(100.0, 110.0, _votes(i, 30, 90), old))
    return snaps


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """把被测的两条路径接到合成快照上，并断言夹具真的非空。"""
    import datetime as _dt

    now = _dt.datetime(2026, 9, 7, 12, 0, 0)
    snaps = _regime_split_snapshots(now)

    # 打**源模块** feedback_loop.BacktestAnalyzer：被测函数用的是函数内
    # 局部 import，打消费方模块 weekly_optimizer 是打不中的（v0.45.135）。
    import feedback_loop
    monkeypatch.setattr(feedback_loop, "BacktestAnalyzer",
                        lambda directory=None, **kw: _FakeAnalyzer(snaps))
    # 干净口径覆写会去查 pheromone.db，查不到就把 actual_price_t7 置 None，
    # 会把整个夹具清空 —— 这里换成恒等，让测试彻底离线。
    monkeypatch.setattr(wo, "_apply_clean_t7_prices", lambda a: a)

    random.seed(20260907)
    return {"now": now, "snaps": snaps, "dir": tmp_path}


def _point_estimate(snaps, now):
    """点估计：与 compute_new_weights_wls 同一套助手，但不过 clamp。"""
    tw = wo._time_decay_weights(snaps, now=now)
    rows = [wo._snapshot_dim_accuracy(s) for s in snaps]
    return wo._weights_from_accuracy(rows, tw)


# ══════════════════════════════════════════════════════════════════════
# A. 助手层：衰减本身是否成立
# ══════════════════════════════════════════════════════════════════════

def test_time_decay_downweights_old_snapshots(wired):
    """远期快照权重必须远小于近期，且 now 可注入（两条路径共用一个钟）。"""
    tw = wo._time_decay_weights(wired["snaps"], now=wired["now"])
    recent = tw[:N_PER_REGIME]
    old = tw[N_PER_REGIME:]
    assert min(recent) > max(old) * 100, (
        f"衰减太弱，近期 {min(recent):.4f} vs 远期 {max(old):.4f}"
    )


def test_time_decay_now_is_injectable(wired):
    """不传 now 会走挂钟；同一份快照在两个 now 下必须给出不同权重。

    这条锁的是「可注入」本身 —— 若哪天有人把 now 参数删掉改回
    datetime.now()，点估计与 bootstrap 就又回到两个钟上。
    """
    import datetime as _dt

    a = wo._time_decay_weights(wired["snaps"], now=wired["now"])
    b = wo._time_decay_weights(wired["snaps"],
                               now=wired["now"] + _dt.timedelta(days=100))
    assert a != b, "now 参数没被用上"


def test_missing_dimension_is_none_not_zero():
    """某维度无有效票时必须是 None，不能是 0.0。

    0.0 的含义是「投了票且全错」，会把缺票伪造成最差表现并进入分母。
    """
    snap = _FakeSnap(100.0, 110.0, {"ScoutBeeNova": 8.0}, "2026-09-01")
    acc = wo._snapshot_dim_accuracy(snap)
    assert acc["signal"] == 1.0
    for dim in ("catalyst", "sentiment", "odds", "risk_adj"):
        assert acc[dim] is None, f"{dim} 应为 None，实为 {acc[dim]!r}"


# ══════════════════════════════════════════════════════════════════════
# B. 核心不变式：抽样分布以点估计为中心
# ══════════════════════════════════════════════════════════════════════

def test_bootstrap_ci_is_centred_on_point_estimate(wired):
    """bootstrap 中位必须贴住点估计，且点估计必须落在 95% CI 内。

    这是「同一个估计量的抽样分布」的定义性质。修复前它是破的。
    """
    point = _point_estimate(wired["snaps"], wired["now"])

    # 夹具自证：口径差必须真的存在，否则本测试测了个寂寞
    assert point["catalyst"] - point["sentiment"] > 0.10, (
        f"夹具没造出世代差异：catalyst={point['catalyst']:.4f} "
        f"sentiment={point['sentiment']:.4f}"
    )

    bs = wo.bootstrap_validate(wired["dir"], point, n_iterations=200)
    assert "error" not in bs, f"bootstrap 未能运行: {bs.get('error')}"
    assert bs["stable"] is True, "同口径下点估计不该出界"

    ci = bs["confidence_95"]
    for dim in wo.DEFAULT_WEIGHTS:
        half = (ci[dim]["hi_95"] - ci[dim]["lo_95"]) / 2
        if half <= 0:
            continue          # 该维度在夹具中无方差
        offset = abs(point[dim] - bs["median_weights"][dim]) / half
        assert offset < 0.5, (
            f"{dim} 抽样分布中心偏离点估计 {offset:.2f}× CI半宽 —— "
            f"两侧口径又不一致了"
        )


def test_mutation_removing_bootstrap_decay_breaks_centring(wired, monkeypatch):
    """靶向 mutation：只打掉 bootstrap 一侧的衰减，上一条断言必须变红。

    没有这一条，`test_bootstrap_ci_is_centred_on_point_estimate` 可能只是
    在一个恒真的条件上全绿。

    ⚠️ 不能改 TIME_DECAY_TAU_DAYS —— 两条路径共用它，一起失效后中心
    仍然对齐，mutation 打不中任何东西。
    """
    point = _point_estimate(wired["snaps"], wired["now"])

    monkeypatch.setattr(wo, "_time_decay_weights",
                        lambda snaps, now=None: [1.0] * len(snaps))
    bs = wo.bootstrap_validate(wired["dir"], point, n_iterations=200)
    assert "error" not in bs, f"bootstrap 未能运行: {bs.get('error')}"

    ci = bs["confidence_95"]
    worst = 0.0
    for dim in wo.DEFAULT_WEIGHTS:
        half = (ci[dim]["hi_95"] - ci[dim]["lo_95"]) / 2
        if half <= 0:
            continue
        worst = max(worst, abs(point[dim] - bs["median_weights"][dim]) / half)

    assert worst > 0.5, (
        f"关掉 bootstrap 的衰减后中心只偏了 {worst:.2f}× 半宽 —— "
        "说明中心性断言没有判别力，先修测试再谈修代码"
    )
    assert bs["stable"] is False, "口径不一致时闸门本应报不稳健"


# ══════════════════════════════════════════════════════════════════════
# C. 退役维度下的闸 1（v0.45.295；v0.45.298 二次检查改为走真实管线）
# ══════════════════════════════════════════════════════════════════════
#
# 事故形状：盒子修好之后，闸 1 对五个维度**全部**出界，与数据无关——
# 提议是「活维度 ≈0.33、退役维度 0」，拿去比不带约束的估计量的 CI（五维各在
# ≈0.2 附近）。同一份数据把**未投影**的原始目标送进去 `stable=True`。
# 闸恒红，比「死在闸前」只好一点点：仍然没有信息量。
# 修法与 v0.45.144 同一原则：CI 必须是**同一个估计量**的抽样分布——
# 有退役维度时，每次重采样也走与提议**同一条**管线。
#
# ⚠️ v0.45.298 二次检查揪出的坑：本节最初的测试用 `_point_estimate` 手搓提议，
# 跳过了 compute_new_weights_wls 里的 _apply_weight_clamps。这个 fixture 恰好让
# catalyst 的原始份额（0.30）超过绝对上限（0.25）——手搓出来的值**生产永远产不出**。
# 结果测试 5/5 全绿，而生产真实路径的提议只有 1/8 判稳定（重采样漏了钳制一步）。
# 与 v0.45.295 想治的是同一个病：夹具让生产值不可达。所以提议一律经
# `_production_proposal` 走真实管线。

_RETIRED_ANCHOR = {"signal": 0.0, "catalyst": 0.332, "sentiment": 0.325,
                   "odds": 0.343, "risk_adj": 0.0}
_N_SEEDS = 6


def _production_proposal(wired):
    """main() 真实会产出的提议：compute_new_weights_wls（内含 _apply_weight_clamps）→ clamp_shifts。

    注意 compute_new_weights_wls 用的是挂钟 now，而 wired 里快照日期锚在固定 now——
    不会变成定时炸弹：时间衰减权重只取决于「近期 vs 远期」的**年龄差**（195 天，恒定），
    加权平均对总量归一，所以两世代的份额之比不随日历前进而变。
    """
    res = wo.compute_new_weights_wls(wired["dir"])
    assert res is not None, "compute_new_weights_wls 未产出（样本被过滤光了？）"
    return wo.clamp_shifts(_RETIRED_ANCHOR, res["new_weights"])


def _stable_over_seeds(wired, proposal, **kw):
    out = []
    for s in range(_N_SEEDS):
        random.seed(s)
        bs = wo.bootstrap_validate(wired["dir"], proposal, n_iterations=200, **kw)
        assert "error" not in bs, bs.get("error")
        out.append(bs["stable"])
    return out


def test_fixture_makes_the_absolute_clamp_bind(wired):
    """夹具自证：钳制这一步必须**真的改变**提议，否则本节所有断言都测不到管线一致性。"""
    raw = _point_estimate(wired["snaps"], wired["now"])
    assert raw["catalyst"] > wo.WEIGHT_CLAMPS["catalyst"][1] + 0.02, (
        f"夹具没让 catalyst 原始份额超过绝对上限：{raw['catalyst']:.3f}"
    )
    with_clamp = wo.clamp_shifts(_RETIRED_ANCHOR, wo._apply_weight_clamps(raw))
    without = wo.clamp_shifts(_RETIRED_ANCHOR, raw)
    assert abs(with_clamp["catalyst"] - without["catalyst"]) > 0.02, "钳制没有改变投影结果"


def test_retired_regime_gate_uses_projected_bootstrap(wired):
    proposal = _production_proposal(wired)
    assert proposal["signal"] == 0.0 and proposal["risk_adj"] == 0.0   # 夹具自证：真的是退役形状

    # (1) 带 anchor：同一条管线 ⇒ 每个种子都稳定；退役维度的 CI 退化为 [0, 0]
    assert all(_stable_over_seeds(wired, proposal, anchor=_RETIRED_ANCHOR)), \
        "生产路径的提议在退役形状下必须被判稳定（同一估计量）"
    random.seed(0)
    ok = wo.bootstrap_validate(wired["dir"], proposal, n_iterations=200, anchor=_RETIRED_ANCHOR)
    for dim in ("signal", "risk_adj"):
        assert ok["confidence_95"][dim]["lo_95"] == 0.0
        assert ok["confidence_95"][dim]["hi_95"] == 0.0
    # 活维度的 CI 不能也是退化区间，否则「稳定」什么都没证明
    assert any(ok["confidence_95"][d]["range_pp"] > 0.5 for d in ("catalyst", "sentiment", "odds"))

    # (2) 不带 anchor（旧行为）：同一提议被判不稳 —— 这就是事故，锁住它才知道 (1) 修的是什么
    assert not any(_stable_over_seeds(wired, proposal)), "无 anchor 时应恒判不稳"
    random.seed(0)
    legacy = wo.bootstrap_validate(wired["dir"], proposal, n_iterations=200)
    for dim in ("signal", "risk_adj"):
        assert legacy["confidence_95"][dim]["lo_95"] > 0.05, "无约束 CI 应远离 0"


def test_retired_regime_gate_still_has_teeth(wired):
    """负对照：带 anchor 时闸依然会红。没有这一条，(1) 可能只是在一个恒真条件上全绿。

    ⚠️ v0.45.299 独立审查指出：本节最初的负对照（catalyst +8pp、复活 signal）都落在**可行盒之外**，
    是 `clamp_shifts` 永远产不出的值——闸拦下一个管线本来就不会给的东西，证明不了它对「管线真能给出、
    但不在重采样中心」的提议有牙。主对照因此改为**盒内**的偏离提议（并断言它确实在盒内）。
    """
    proposal = _production_proposal(wired)
    bounds = wo.merge_bounds(_RETIRED_ANCHOR)

    off_center = dict(proposal)          # 和不变，盒内，只把质量从 catalyst 搬到 sentiment
    off_center["catalyst"] -= 0.06
    off_center["sentiment"] += 0.06
    for k in ("catalyst", "sentiment"):
        assert bounds[k][0] - 1e-9 <= off_center[k] <= bounds[k][1] + 1e-9, \
            f"对照必须是管线真能产出的值（{k} 落在盒外）"
    assert not any(_stable_over_seeds(wired, off_center, anchor=_RETIRED_ANCHOR)), \
        "盒内但偏离重采样中心的提议必须被拦下"

    # 纵深防御：即便投影被绕过（人为 --force 之类），复活退役维度的提议也不该被闸 1 放行
    resurrected = dict(proposal)
    resurrected["signal"] = 0.05
    resurrected["catalyst"] -= 0.05
    assert not any(_stable_over_seeds(wired, resurrected, anchor=_RETIRED_ANCHOR)), \
        "复活退役维度的提议不该被闸 1 放行"
