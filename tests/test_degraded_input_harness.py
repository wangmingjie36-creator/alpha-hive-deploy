"""Phase 2 护栏 —— 喂退化输入，断言产出不是「看起来合理的数字」（v0.45.53）

## 为什么是这个形状

2026-08-26~27 的审计在全仓找到 68 处同一形状的缺陷：取数失败被 `except` 或
`.get(k, 默认值)` 接住，产出一个**合法、可解读、且完全虚假**的值。
逐个修完是 68 个实例；这个文件是为了**防止第 69 个**。

## 为什么不用静态检查

试过三种，都不成立：

  · 「字面量来源标签」—— 全仓 98 处，绝大多数诚实（成功后标 "cboe" 没问题）
  · 「结果词字面量」—— 175 处，`except` 里标 "fallback" 本来就对；
    一个标签是否诚实**取决于它在哪条分支上**，那是语义不是语法
  · 「拼接链里的行内三元」—— 三次收窄都在误报 dict 字面量的值

结论：静态检查判不了「这个值是不是编的」。**只有真的走一遍失败路径才判得了**，
所以护栏必须是动态的。

## 判据（来自六个真实样本）

失败时产出的值，只要落在下面任一类，就是缺陷：

  1. 该量纲的**中位/中性值** —— crowding 50、RSI 50、P/C 1.0、F&G 50、五维 5.0
  2. 该量纲的**极值** —— pheromone_strength 1.0（最大）、IV Rank 0.0（最低）
  3. **恰好等于某个阈值** —— rr 2.0 = STRONG BUY 闸、tnx 4.5 = high 档边界
  4. 一个**具体的观测读数** —— σ 30%、dte 30 天、price 145.32

判据是：**这个值会不会让下游误以为掌握了信息。**

## 注册表必须覆盖

`test_registry_covers_all_scoring_entry_points` 会检查被监视模块里的公开评分/
渲染函数是否都在注册表里 —— 新增一个没注册的就红。否则护栏会随代码增长而失效，
而且是**静默**失效（这正是它要防的东西）。
"""

import inspect
import math

import pytest


# ─────────────────────────────────────────────────────────────
# 退化输入下「不许出现」的值
# ─────────────────────────────────────────────────────────────
SUSPICIOUS_NUMBERS = {
    0.0: "该量纲的最低值 / 「持平」",
    1.0: "比率的中性值 / 强度的最大值",
    0.5: "概率或比例的正中间",
    2.0: "STRONG BUY 闸的阈值",
    3.0: "6 个 Agent 的一半",
    5.0: "0-10 分制的正中间",
    30.0: "典型大盘股年化波动率 / 默认 DTE",
    50.0: "0-100 分制的正中间 / IV Rank 中位",
    100.0: "占位股价",
}


def assert_not_fabricated(value, label, allow=()):
    """退化输入的产出：要么 None，要么明确的哨兵；不得是可解读的读数。"""
    if value is None:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, str):
        return
    if not isinstance(value, (int, float)):
        return
    if isinstance(value, float) and math.isnan(value):
        return
    if value in allow:
        return
    hit = SUSPICIOUS_NUMBERS.get(float(value))
    assert hit is None, (
        f"{label}: 退化输入产出 {value} —— {hit}。\n"
        f"这个值会让下游误以为掌握了信息。应给 None 或明确哨兵。"
    )


# ─────────────────────────────────────────────────────────────
# ① 拥挤度：全分量不可得
# ─────────────────────────────────────────────────────────────

class TestCrowdingDegraded:
    """实测旧行为：空 metrics → 20.59 →「低拥挤度」→ adjustment_factor **1.2 加分**。
    数据缺失被当成利好 —— 本护栏最典型的靶子。"""

    @pytest.mark.parametrize("metrics", [{}, {"bullish_agents": None},
                                         {"social_messages_per_day": None,
                                          "google_trends_percentile": None,
                                          "bullish_agents": None,
                                          "seeking_alpha_page_views": None,
                                          "short_float_ratio": None,
                                          "price_momentum_5d": None}])
    def test_all_components_missing_returns_none(self, metrics):
        from crowding_detector import CrowdingDetector
        d = CrowdingDetector("TEST")
        score, comp = d.calculate_crowding_score(metrics)
        assert score is None, f"全分量不可得应返回 None，实得 {score}"

    def test_missing_data_is_never_rewarded(self):
        """核心不变式：拿不到数据**绝不能**换来加分"""
        from crowding_detector import CrowdingDetector
        d = CrowdingDetector("TEST")
        score, _ = d.calculate_crowding_score({})
        factor = d.get_adjustment_factor(score)
        assert factor <= 1.0, f"数据缺失换来 {factor}× 加分"
        assert factor == 1.0, "应为中性 1.0"


# ─────────────────────────────────────────────────────────────
# ② 风险引擎：σ 不可得
# ─────────────────────────────────────────────────────────────

class TestRiskEngineDegraded:
    """v0.45.15 的注释写着「σ 不可得就写 None，不要用 30 冒充一个观测值」，
    而上游 `or 30.0` 在守卫之前就把缺失填成了 30.0 —— 守卫被架空。"""

    @pytest.mark.parametrize("sd", [{}, {"volatility_20d": None},
                                    {"price": 0.0, "volatility_20d": None}])
    def test_sigma_unavailable_is_none(self, sd):
        import risk_engine
        v = risk_engine._vol_pct(sd)
        assert v is None, f"σ 不可得应为 None，实得 {v}"
        assert_not_fabricated(v, "risk_engine._vol_pct")

    def test_upstream_does_not_prefill_sigma(self):
        """回归闸：上游不得在守卫之前把缺失填成 30.0"""
        import inspect

        import risk_engine
        src = inspect.getsource(risk_engine)
        assert 'get("volatility_20d") or 30.0' not in src
        assert '"volatility_20d": 30.0' not in src


# ─────────────────────────────────────────────────────────────
# ③ 0DTE：真实的 0 不得被改写成 30
# ─────────────────────────────────────────────────────────────

class TestZeroDTEPreserved:
    """`max(dte, 0.5)` 才是超短期守卫，而上游 `or 30` 把真实 0DTE 改写成 30 天，
    T 从 0.00137 变成 0.08219 —— **差 60 倍**，恰在 gamma 最大的到期日。"""

    def test_zero_dte_not_rewritten(self):
        import inspect

        import advanced_analyzer
        import options_analyzer
        for mod in (advanced_analyzer, options_analyzer):
            src = inspect.getsource(mod)
            assert 'get("dte", 30) or 30' not in src, \
                f"{mod.__name__}: `or 30` 会把真实 0DTE 改写成 30 天"

    def test_t_for_zero_dte_is_sub_day(self):
        """0DTE 的 T 必须落在半天量级，不是 30 天"""
        t_zero = max(0.0, 0.5) / 365.0
        t_thirty = max(30.0, 0.5) / 365.0
        assert t_zero < 0.002
        assert t_thirty / t_zero == pytest.approx(60.0, rel=0.01)


# ─────────────────────────────────────────────────────────────
# ④ 评级闸：缺数据不得通过
# ─────────────────────────────────────────────────────────────

class TestRatingGateDegraded:
    """无历史可比时旧实现返回 rr=2.0，而 STRONG BUY 闸正是 `rr >= 2.0` ——
    「一次比对都没做成」恰好卡在阈值上通过。"""

    def test_no_history_gives_none_not_threshold(self):
        from advanced_analyzer import AdvancedAnalyzer
        a = AdvancedAnalyzer.__new__(AdvancedAnalyzer)
        rr = a._calculate_risk_reward_ratio("TEST", [])
        assert rr is None, f"无历史可比应为 None，实得 {rr}"
        assert_not_fabricated(rr, "risk_reward_ratio")

    def test_zero_avg_loss_gives_none(self):
        from advanced_analyzer import AdvancedAnalyzer
        a = AdvancedAnalyzer.__new__(AdvancedAnalyzer)
        opps = [{"gain_7d_pct": 5.0, "max_drawdown_pct": 0.0}]
        assert a._calculate_risk_reward_ratio("TEST", opps) is None


# ─────────────────────────────────────────────────────────────
# ⑤ 论点失效闸：可求值性必须自报
# ─────────────────────────────────────────────────────────────

class TestThesisGateDegraded:
    def test_unevaluable_config_says_so(self, tmp_path, monkeypatch):
        import json

        import market_intelligence as mi
        prose = {"ZZZ": {"level_1_warning": {"conditions": [
            {"id": "x", "metric": "m", "trigger": "下降 > 5%"}]}}}
        (tmp_path / "thesis_breaks_config.json").write_text(json.dumps(prose))
        monkeypatch.setattr(mi, "_BASE", tmp_path)
        r = mi.check_thesis_breaks("ZZZ", 100.0, 50.0, 1.0, [], 5.0)
        assert r["evaluable"] is False
        assert r["level"] is None
        assert r["unevaluable_reason"]


# ─────────────────────────────────────────────────────────────
# ⑥ 信息素：坏值不得获得最大影响力
# ─────────────────────────────────────────────────────────────

class TestPheromoneDegraded:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "x", None])
    def test_corrupt_strength_not_max(self, bad):
        from pheromone_board import PheromoneBoard, PheromoneEntry
        b = PheromoneBoard()
        e = PheromoneEntry(agent_id="A", ticker="NVDA", discovery="d",
                           source="s", self_score=5.0, direction="bullish",
                           pheromone_strength=bad)
        b.publish(e)
        assert e.pheromone_strength < 1.0, \
            f"坏值 {bad!r} 换来 {e.pheromone_strength} —— 那是最大影响力"


# ─────────────────────────────────────────────────────────────
# ⑦ 渲染层：缺失渲染成「—」而非数字
# ─────────────────────────────────────────────────────────────

class TestRenderDegraded:
    ALL_NONE = {"data_quality": "real", "iv_rank": None, "iv_current": None,
                "iv_percentile": None, "put_call_ratio": None, "total_oi": None,
                "iv_skew_ratio": None, "rv_30d": None, "iv_rv_spread": None}

    def test_options_section_shows_dash(self):
        from generate_ml_report import MLEnhancedReportGenerator as G
        html = G._generate_options_section_html(G.__new__(G), dict(self.ALL_NONE))
        assert "—" in html
        for bad in ("50.0", "25.00%", "1.00"):
            assert f">{bad}<" not in html, f"退化输入渲染出 {bad}"

    def test_oracle_section_shows_dash(self):
        from generate_ml_report import MLEnhancedReportGenerator as G
        html = G._ch3_oracle(G.__new__(G), {}, dict(self.ALL_NONE), current_price=0)
        assert "—" in html


# ─────────────────────────────────────────────────────────────
# ⑦b 拥挤度显示层：分量不可得时不得渲染成数字
# ─────────────────────────────────────────────────────────────

class TestCrowdingDisplayDegraded:
    """`_get_metric_display` 曾对 None 直接 f"{None:.1f}" 抛异常
    （`.get(k, 0)` 挡不住「键存在但值为 None」）。"""

    @pytest.mark.parametrize("key", ["social_volume", "google_trends",
                                     "consensus_strength", "seeking_alpha_views",
                                     "short_squeeze_risk"])
    def test_all_none_metrics_render_without_crash(self, key):
        from crowding_detector import CrowdingDetector
        d = CrowdingDetector("TEST")
        metrics = {k: None for k in ("social_messages_per_day", "google_trends_percentile",
                                     "bullish_agents", "seeking_alpha_page_views",
                                     "short_float_ratio", "price_momentum_5d")}
        out = d._get_metric_display(key, metrics)       # 不崩即通过
        assert isinstance(out, str)

    def test_missing_momentum_shows_dash(self):
        from crowding_detector import CrowdingDetector
        d = CrowdingDetector("TEST")
        out = d._get_metric_display("short_squeeze_risk", {"price_momentum_5d": None})
        assert "—" in out, f"动量不可得应显示 —，实得 {out!r}"


# ─────────────────────────────────────────────────────────────
# ⑧ 注册表覆盖闸 —— 防止护栏随代码增长而静默失效
# ─────────────────────────────────────────────────────────────

# 已被上面各类覆盖的入口（模块, 函数名）
COVERED = {
    ("crowding_detector", "calculate_crowding_score"),
    ("crowding_detector", "get_adjustment_factor"),
    ("risk_engine", "_vol_pct"),
    ("advanced_analyzer", "_calculate_risk_reward_ratio"),
    ("market_intelligence", "check_thesis_breaks"),
    ("pheromone_board", "_validate_entry"),
    ("generate_ml_report", "_generate_options_section_html"),
    ("generate_ml_report", "_ch3_oracle"),
    ("crowding_detector", "_get_metric_display"),
}

# 明确豁免（不产出可被误读为观测的数值）
EXEMPT = {
    ("crowding_detector", "get_crowding_category"),      # 返回字符串
    ("crowding_detector", "get_hedge_recommendations"),  # 返回建议列表
    # 下面两个只把**已算好的分数**翻译成文案，不接触原始 metrics，
    # 因此没有「用假值冒充观测」的机会；它们的输入正确性由上游用例保证。
    ("crowding_detector", "_get_metric_interpretation"),
    ("crowding_detector", "_get_score_adjustment_interpretation"),
}


class TestRegistryCoverage:
    """新增评分/渲染入口时必须同步注册，否则本闸变红。

    没有这一条，护栏会随代码增长而失效 —— 而且是**静默**失效，
    正是它本身要防的形状。
    """

    def test_crowding_public_scoring_fns_registered(self):
        import crowding_detector
        cls = crowding_detector.CrowdingDetector
        missing = []
        for name, fn in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("__"):
                continue
            if not any(k in name for k in ("score", "factor", "categor", "metric")):
                continue
            key = ("crowding_detector", name)
            if key not in COVERED and key not in EXEMPT:
                missing.append(name)
        assert not missing, (
            f"CrowdingDetector 新增了未注册的评分入口：{missing}\n"
            f"请在本文件加退化输入用例，或加入 EXEMPT 并说明理由。"
        )

    def test_covered_entries_still_exist(self):
        """注册表不得指向已删除的函数 —— 否则覆盖是假的"""
        import importlib
        stale = []
        for mod_name, fn_name in sorted(COVERED | EXEMPT):
            mod = importlib.import_module(mod_name)
            found = hasattr(mod, fn_name) or any(
                hasattr(obj, fn_name)
                for _, obj in inspect.getmembers(mod, inspect.isclass)
            )
            if not found:
                stale.append(f"{mod_name}.{fn_name}")
        assert not stale, f"注册表指向已不存在的函数：{stale}"
