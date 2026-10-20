"""
ML 预期收益与概率的**可达性**不变式（v0.44.1）

为什么这组测试此前不存在
------------------------
旧公式 `expected_7d = catalyst_bonus + momentum_bonus - crowding_penalty` 里
`catalyst_bonus` 恒 ≥5、`crowding_penalty` 恒 ≤10，于是预期收益结构性恒正
（实测 96.7% 为正、中位数 **+8.00%**，而真实 7 日收益中位数 **−0.21%**）。

它逃过了所有既有测试，原因很具体：**单测喂 `catalyst_quality="A+"` 会得到一个
大的正数，符合预期**。没有任何测试问过反方向的问题 ——
「**有没有输入能让它输出负数？**」

这就是本文件的全部主题。同理适用于 `probability`：它的八个分项中性点都在 0.5
之上，结构性地板 0.3610（实测最小 0.3500），99.6% > 0.5，无法表达"强烈看空"。

⚠️ 新增任何"预测"类输出时，都要配一条这样的可达性断言。断言"给好输入得到好
输出"只证明了乐观路径存在；证明不了悲观路径**没有被结构性焊死**。
"""

import math

import pytest

import ml_predictor as mp
from ml_predictor import TrainingData


_CROWDING_TYPICAL = 23.30   # 拥挤度实测中位数（2026-08-16, n=1057）；仅作夹具取值


def _data(**kw):
    """构造 TrainingData，只覆盖关心的字段。

    `crowding_score` 取 23.30（v0.44.2 的实测中位数）。`expected_returns` 不读拥挤度，取值只为夹具像真数据；
    v0.45.558 前这里引用生产常数 `mp.CROWDING_NEUTRAL`，该常数随 Rival 模型退役已删。
    """
    base = dict(
        ticker="TEST", date="2026-08-14",
        crowding_score=_CROWDING_TYPICAL, catalyst_quality="B+", momentum_5d=0.0,
        volatility=30.0, market_sentiment=0.0,
        actual_return_3d=0.0, actual_return_7d=0.0, actual_return_30d=0.0,
        win_3d=False, win_7d=False, win_30d=False,
    )
    base.update(kw)
    return TrainingData(**base)


# ════════════════════════════════════════════════════════════════════════════
# 可达性：负预期收益必须可能出现
# ════════════════════════════════════════════════════════════════════════════

class TestNegativeExpectedReturnIsReachable:

    def test_negative_momentum_yields_negative_expectation(self):
        """最基本的一条：跌了就该预期继续跌，不需要跌超 10% 才算。

        旧实现在 rival_bee 的硬编码特征下等价于 `8.0 + 0.8*momentum`，
        需要 momentum < −10% 才转负。−2% 这种常见跌幅会得到 **+6.4%**。
        """
        out = mp.expected_returns(_data(momentum_5d=-2.0))
        assert out["expected_7d"] < 0, (
            f"动量 −2% 竟得到预期收益 {out['expected_7d']:+.2f}%"
        )

    def test_small_negative_momentum_still_negative(self):
        """−0.5% 也必须为负 —— 门槛不能藏在某个常数里。"""
        assert mp.expected_returns(_data(momentum_5d=-0.5))["expected_7d"] < 0

    def test_zero_signal_is_exactly_neutral(self):
        """无动量 + 中性拥挤度 ⇒ 预期收益恰为 0，不是 +8%。

        这是整个 bug 的核心：旧实现在"什么都不知道"时输出 +8%。
        动量为 0 的样本在近 12 个扫描日占 **63.7%**。
        """
        out = mp.expected_returns(
            _data(momentum_5d=0.0, crowding_score=_CROWDING_TYPICAL))
        for k in ("expected_3d", "expected_7d", "expected_30d"):
            assert out[k] == pytest.approx(0.0, abs=1e-9), f"{k} = {out[k]}"

    def test_catalyst_quality_never_flips_sign(self):
        """催化剂质量只调**幅度**不调**方向**。

        它编码"影响多大"，不编码"影响好坏" —— 与 ChronosBee 同族的教训。
        """
        for q in ("A+", "A", "B+", "B", "C", "unknown-grade", None):
            up = mp.expected_returns(_data(catalyst_quality=q, momentum_5d=+3.0))
            dn = mp.expected_returns(_data(catalyst_quality=q, momentum_5d=-3.0))
            assert up["expected_7d"] > 0, f"{q}: 正动量却非正"
            assert dn["expected_7d"] < 0, f"{q}: 负动量却非负 —— 质量项翻转了符号"

    def test_best_catalyst_cannot_rescue_a_falling_stock(self):
        """A+ 催化剂 + 下跌动量必须仍为负。

        旧实现下 A+ 需要跌超 25% 才转负。
        """
        out = mp.expected_returns(_data(catalyst_quality="A+", momentum_5d=-5.0))
        assert out["expected_7d"] < 0

    def test_catalyst_quality_scales_magnitude_monotonically(self):
        """更好的催化剂 → 更大的幅度（同符号）。"""
        mags = [
            abs(mp.expected_returns(
                _data(catalyst_quality=q, momentum_5d=2.0))["expected_7d"])
            for q in ("C", "B", "B+", "A", "A+")
        ]
        assert mags == sorted(mags), f"幅度非单调: {mags}"


class TestCrowdingStaysOutOfExpectedReturns:
    """v0.44.2 决定：拥挤度**不进入** `expected_returns`。

    v0.44.1 曾把它做成双向倾斜项。用现成的四口径工具
    （`signal_archive.py --analyze`，噪音地板 0.076、需 ≥3/4）复核后移除：

      · `crowding.score`（连续）  IC=**+0.1122**, t=+2.48, **仅 1/4 口径** → 不达标
      · `crowding.adj_factor`（分档）IC=−0.1117, 3/4 口径，但**⚠️稀疏**（仅 6 个取值）

    两者方向一致，且都指向「**高拥挤 → 收益更高**」—— 与检测器
    `get_adjustment_factor`（高拥挤打 30% 折）的设计意图**相反**。
    连续版不达标、达标版有稀疏问题 ⇒ 符号不该由这份证据决定，也不该无视它。

    另外它已在 `predict_probability` 里是**权重最大的特征**（0.18），
    又经 adj_factor 作用于综合分 —— 再加进来是第三次计数。

    ⚠️ 代价已知：含倾斜时回放偏差 +0.19pp，移除后回到 +1.06pp。
    但那 +0.19 是**巧合**（倾斜均值恰好抵消动量带来的正偏），不是符号对。
    宁要可辩护的 +1.06，不要不可辩护的 +0.19。
    """

    @pytest.mark.parametrize("crd", [0.0, 10.0, 23.3, 50.0, 100.0, -50.0, 500.0])
    def test_crowding_does_not_affect_expected_returns(self, crd):
        base = mp.expected_returns(_data(momentum_5d=2.0))["expected_7d"]
        got = mp.expected_returns(
            _data(momentum_5d=2.0, crowding_score=crd))["expected_7d"]
        assert got == pytest.approx(base), (
            f"crowding={crd} 改变了预期收益（{got:+.4f} vs {base:+.4f}）—— "
            "方向未确立的信号不应进入收益预测"
        )

    def test_crowding_is_still_a_probability_feature(self):
        """移除只针对 expected_returns。crowding 在 probability 里是权重最大的
        特征（0.18），**必须**仍然起作用 —— 否则就把真实拥挤度白传了。
        """
        m = mp.SimpleMLModel()
        m.feature_stats = {
            k: {"min": 0.0, "max": 100.0} for k in
            ("crowding", "catalyst", "momentum", "volatility", "sentiment",
             "iv_rank", "put_call_ratio", "final_score", "odds_score",
             "risk_adj_score", "agent_agreement", "direction_encoded")
        }
        lo = m.predict_probability(_data(crowding_score=0.0))
        hi = m.predict_probability(_data(crowding_score=100.0))
        assert lo != pytest.approx(hi), "crowding 对 probability 完全无影响"
        assert lo > hi, "拥挤度越高，概率应越低（与 crowding 权重的 inverse 一致）"

    def test_extreme_crowding_never_crashes(self):
        for bad in (-50.0, 500.0, float("inf"), float("nan"), None, "n/a"):
            out = mp.expected_returns(
                _data(momentum_5d=1.0, crowding_score=bad))["expected_7d"]
            assert math.isfinite(out)


# v0.45.558：`TestCrowdingCalibrationDrift` / `TestCrowdingNeutralIsNotScaleMidpoint` 已删 —— 它们守的
# `ml_predictor.CROWDING_NEUTRAL` 随 RivalBee 合成样本模型（v0.45.538）失去全部生产读者，常数一并删除。


class TestMissingMomentumHandling:
    """`momentum_5d` 在近 12 个扫描日有 63.7% 恰为 0.0，且两个标的池都高
    （原核心 58.3%、扩池 68.9%）—— 系统性取数失败，不是新标的历史太短。
    旧的 `momentum_bonus = data.momentum_5d` 对 None 会直接 TypeError。
    """

    @pytest.mark.parametrize("missing", [None, float("nan")])
    def test_missing_momentum_is_neutral_not_crash(self, missing):
        out = mp.expected_returns(_data(momentum_5d=missing))
        assert out["expected_7d"] == pytest.approx(0.0)

    def test_non_numeric_momentum_is_neutral_not_crash(self):
        out = mp.expected_returns(_data(momentum_5d="n/a"))
        assert out["expected_7d"] == pytest.approx(0.0)


class TestHorizonsAreScalesNotIndependentForecasts:
    """三个期限是同一个量的缩放，符号恒同。

    钉住这条不是为了固化设计，而是为了让下游别再把它们当独立信号 ——
    `rival_bee` 的 `avg_ret = (expected_7d + expected_30d)/2` 正是这么错的：
    它等于 `expected_7d × 1.0`，看似平均两个期限，实则没有平均任何东西。
    """

    @pytest.mark.parametrize("mom", [-5.0, -0.1, 0.1, 5.0])
    def test_all_horizons_share_sign(self, mom):
        out = mp.expected_returns(_data(momentum_5d=mom))
        signs = {k: (out[k] > 0) for k in
                 ("expected_3d", "expected_7d", "expected_30d")}
        assert len(set(signs.values())) == 1, signs

    def test_avg_of_7d_and_30d_equals_7d_scaled(self):
        out = mp.expected_returns(_data(momentum_5d=3.0))
        avg = (out["expected_7d"] + out["expected_30d"]) / 2
        core = out["expected_7d"] / mp._HORIZON_SCALE["expected_7d"]
        assert avg == pytest.approx(core)


# ════════════════════════════════════════════════════════════════════════════
# 可达性：probability 必须能表达看空
# ════════════════════════════════════════════════════════════════════════════

class TestProbabilityIsCentered:

    @pytest.fixture
    def model(self):
        m = mp.SimpleMLModel()
        # 用对称的 feature_stats，使"中位输入"明确对应 norm=0.5
        m.feature_stats = {
            k: {"min": 0.0, "max": 100.0} for k in
            ("crowding", "catalyst", "momentum", "volatility", "sentiment",
             "iv_rank", "put_call_ratio", "final_score", "odds_score",
             "risk_adj_score", "agent_agreement", "direction_encoded")
        }
        return m

    def test_centered_feature_identity_when_influence_is_one(self):
        """influence=1.0 必须等价于恒等映射 —— 旧实现里 catalyst / final_score /
        odds_score / risk_adj_score 四项本来就是裸 x，改动后数值不能变。"""
        for x in (0.0, 0.25, 0.5, 0.75, 1.0):
            assert mp.centered_feature(x, 1.0) == pytest.approx(x)

    def test_centered_feature_neutral_at_half(self):
        for k in (0.3, 0.4, 0.5, 0.7, 1.0):
            assert mp.centered_feature(0.5, k) == pytest.approx(0.5)
            assert mp.centered_feature(0.5, k, inverse=True) == pytest.approx(0.5)

    def test_centered_feature_stays_in_unit_interval(self):
        for k in (0.3, 0.4, 0.5, 0.7, 1.0):
            for x in (0.0, 1.0):
                for inv in (False, True):
                    v = mp.centered_feature(x, k, inverse=inv)
                    assert 0.0 <= v <= 1.0, (x, k, inv, v)

    def test_inverse_mirrors_direct(self):
        for x in (0.0, 0.2, 0.5, 0.8, 1.0):
            d = mp.centered_feature(x, 0.7)
            i = mp.centered_feature(x, 0.7, inverse=True)
            assert d + i == pytest.approx(1.0)

    def test_all_median_features_give_exactly_half(self, model):
        """全特征取中位 ⇒ probability 恰为 0.5。

        旧实现在同样输入下给 0.6905（八个分项的中性点都在 0.5 之上）。
        """
        d = _data(
            crowding_score=50.0, momentum_5d=50.0, volatility=50.0,
            market_sentiment=50.0, iv_rank=50.0, put_call_ratio=50.0,
            final_score=50.0, odds_score=50.0, risk_adj_score=50.0,
            agent_agreement=50.0, direction_encoded=50.0,
            catalyst_quality="B+",
        )
        # catalyst 走 encode_catalyst_quality，单独对齐到 norm=0.5
        model.feature_stats["catalyst"] = {
            "min": model.encode_catalyst_quality("B+") - 1.0,
            "max": model.encode_catalyst_quality("B+") + 1.0,
        }
        assert model.predict_probability(d) == pytest.approx(0.5, abs=1e-9)

    def test_probability_can_go_below_half(self, model):
        """旧实现结构性地板 0.3610、实测 99.6% > 0.5 —— 它无法表达强烈看空。"""
        bearish = _data(
            crowding_score=100.0, momentum_5d=0.0, volatility=100.0,
            market_sentiment=0.0, iv_rank=100.0, put_call_ratio=100.0,
            final_score=0.0, odds_score=0.0, risk_adj_score=0.0,
            agent_agreement=0.0, direction_encoded=0.0, catalyst_quality="C",
        )
        p = model.predict_probability(bearish)
        assert p < 0.5, f"最悲观输入仍得到 p={p:.4f}"

    def test_reachable_range_is_symmetric_about_half(self, model):
        """向下空间必须与向上空间相当。旧实现是 0.139 vs 0.45（不对称 3.2 倍）。

        catalyst 刻意固定在中性档：`encode_catalyst_quality` 的取值本身不关于
        中点对称（A+ 与 C 到中位的距离不等），若让它随两端一起摆动，测出的
        不对称来自催化剂编码而非本次要检验的居中化。把它按住，断言才对准目标。
        """
        neutral_cat = model.encode_catalyst_quality("B+")
        model.feature_stats["catalyst"] = {
            "min": neutral_cat - 1.0, "max": neutral_cat + 1.0,
        }

        def _p(v):
            return model.predict_probability(_data(
                crowding_score=v, momentum_5d=v, volatility=v,
                market_sentiment=v, iv_rank=v, put_call_ratio=v,
                final_score=v, odds_score=v, risk_adj_score=v,
                agent_agreement=v, direction_encoded=v,
                catalyst_quality="B+",
            ))

        hi, lo = _p(100.0), _p(0.0)
        assert hi > 0.5 > lo
        assert (hi - 0.5) == pytest.approx(0.5 - lo, abs=1e-9), (
            f"上行空间 {hi-0.5:.4f} vs 下行空间 {0.5-lo:.4f}"
        )


# ════════════════════════════════════════════════════════════════════════════
# 三个模型类必须共用同一公式；降级实现必须与主实现数值一致
# ════════════════════════════════════════════════════════════════════════════

class TestSingleSourceOfTruth:

    def test_formula_appears_once_in_source(self):
        """旧实现在 SimpleMLModel:390 / SGDMLModel:672 / HGBModel:1197 三处
        逐字重复，docstring 还写着"完全一致" —— 那种一致性靠人肉维护。
        """
        with open(mp.__file__, encoding="utf-8") as f:
            src = f.read()
        # 只数代码里的赋值，注释里的说明不算
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert code.count("catalyst_bonus =") == 0, (
            "旧的 catalyst_bonus 加性公式又出现了"
        )

    @pytest.mark.parametrize("cls_name", ["SimpleMLModel", "SGDMLModel", "HGBModel"])
    def test_all_model_classes_delegate(self, cls_name):
        cls = getattr(mp, cls_name, None)
        if cls is None:
            pytest.skip(f"{cls_name} 不存在（sklearn 缺失？）")
        import inspect
        src = inspect.getsource(cls.predict_return)
        assert "expected_returns(data)" in src, (
            f"{cls_name}.predict_return 没有委托给共享函数"
        )


# v0.45.538：`TestFallbackMirrorsPrimary` 已删 —— 它守的「降级实现 `ml_predictor_extended.SimpleMLModel`
# 与主实现数值一致」随该模块退役失去对象（守卫 tests/test_rival_ml_adjustment_retired.py）。


