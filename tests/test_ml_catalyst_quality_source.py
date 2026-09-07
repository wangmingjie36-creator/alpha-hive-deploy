"""`catalyst_quality` 的服务路径必须与训练路径同源（v0.45.135）

缺陷
----
`generate_ml_report._prepare_ml_input` 把 ML 特征 `catalyst_quality` 由
**`recommendation.rating`** 反推：

    rating_to_quality = {"STRONG BUY": "A+", "BUY": "A", "HOLD": "B+", "AVOID": "C"}

而 `rating` 并不是催化剂的度量。它的上游链是：

    advanced_analyzer._estimate_catalyst_quality(ticker)   # 硬编码三只票的表
      → ProbabilityCalculator.calculate_win_probability(crowding, grade)
        → _generate_recommendation(prob, rr)               # 阈值切成四档
          → 本函数把 rating 再映射回一个等级

同一个特征槽在**训练**路径（`_build_real_training_data`）里喂的是
ChronosBee 催化剂维度分经 `catalyst_quality_from_score`，与 `rival_bee.py`
一致。两条口径不同 ⇒ **train/serve skew**。

实测（803 份生产 `analysis-*-ml-*.json`，745 份两条口径都能取到）
-----------------------------------------------------------------
  · 等级一致率 **7.1%**，低于按边缘分布独立时的期望 8.1%
  · Spearman ρ = **+0.087**（≈ 无关）
  · 服务路径**从未**产出 "B" / "C"（0 / 745），而真实分布里这两档占 **70.5%**
  · 服务路径 75.6% 落在 "A"（0.85），真实分布 61.9% 落在 "B"（0.55）

即：模型在 0.55 附近训练、在 0.85 附近服务。

为什么长期没人发现
------------------
`generate_ml_report.py:357` 先写了一次 `catalyst_quality`，**43 行后被
line 400 的 `rating_to_quality` 整个覆盖**——那个死赋值把注意力引向
「特征恒为 0.5」这个并不成立的现象（`encode_catalyst_quality` 确实把
"BUY" 之类原文映射成 0.5，但生产从不喂原文）。真正的缺陷不是常数，
是**换了个量**。

影响面（决定要不要世代边界）
----------------------------
`predictions` 表**无** `catalyst_quality` 列；写库的是
`Backtester.save_predictions(swarm_results)`（`alpha_hive_daily_report.py:812`），
而 ML 报告在 line 2383 才生成，且 `generate_ml_report` 不写 pheromone.db。
⇒ 本特征**不进 IC 测量管道**，不需要往 `ic_rerun_readiness._COHORT_HISTORY`
追加世代边界（那条约定守的是 `expected_returns` / `predict_probability` /
RivalBee 特征来源三条进 IC 的路径）。
"""

import ast
import datetime
import pathlib

import pytest

from generate_ml_report import MLEnhancedReportGenerator
from ml_predictor import catalyst_quality_from_score

REPO = pathlib.Path(__file__).resolve().parent.parent


def _gen():
    """跳过重量级 __init__——`_prepare_ml_input` 不依赖任何实例状态"""
    return MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)


def _real_ledger_size():
    """真实概率账本的字节数；不存在记 -1。用于自证测试没碰生产状态。"""
    led = REPO / "probability_scorecard_state" / "published.jsonl"
    return led.stat().st_size if led.exists() else -1


def _metrics():
    return {"ticker": "XOM",
            "sources": {"yahoo_finance": {"current_price": 110.0,
                                          "price_change_5d": 1.2}}}


def _analysis(rating="BUY"):
    """生产实测形状：`advanced_analysis` 里没有 dimension_scores 键"""
    return {"recommendation": {"rating": rating, "action": "分批建仓"},
            "probability_analysis": {"win_probability_pct": 65.0,
                                     "risk_reward_ratio": 2.1},
            "options_analysis": {}}


class TestCatalystQualitySource:
    """服务路径的等级必须由 ChronosBee 催化剂分决定"""

    @pytest.mark.parametrize("catalyst,grade", [
        (9.2, "A+"), (8.5, "A+"),      # 阈值上沿
        (8.0, "A"), (7.5, "A"),
        (7.0, "B+"), (6.5, "B+"),
        (5.9, "B"), (5.5, "B"),        # 生产中位档——旧实现从未产出过
        (3.0, "C"), (0.0, "C"),        # 旧实现同样从未产出过
    ])
    def test_grade_comes_from_chronos_catalyst_score(self, catalyst, grade):
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores={"catalyst": catalyst})
        assert td.catalyst_quality == grade, (
            f"催化剂分 {catalyst} 应得 {grade}，实得 {td.catalyst_quality}")
        # 与训练路径 / rival_bee 用的是同一个函数，不是抄来的第二套阈值
        assert td.catalyst_quality == catalyst_quality_from_score(catalyst)

    def test_rating_does_not_move_the_grade(self):
        """同一催化剂分下，四种 rating 必须给出同一等级。

        旧实现在这里给出四个**不同**的等级（A+/A/B+/C），因为它读的是 rating。
        """
        g = _gen()
        grades = {
            r: g._prepare_ml_input("XOM", _metrics(), _analysis(r),
                                   swarm_dimension_scores={"catalyst": 5.9}
                                   ).catalyst_quality
            for r in ("STRONG BUY", "BUY", "HOLD", "AVOID")
        }
        assert set(grades.values()) == {"B"}, (
            f"rating 不应影响催化剂等级，实测 {grades}")

    def test_missing_catalyst_is_flagged_not_silently_neutral(self):
        """取不到催化剂分时必须**报出来**——否则「没测到」被渲染成「测了是中性」。

        与同函数 v0.45.50 的 `_ml_input_missing` 同一机制。
        """
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores=None)
        assert "catalyst_quality" in g._ml_input_missing, (
            f"缺催化剂分必须进 _ml_input_missing，实测 {g._ml_input_missing}")
        # v0.45.147：缺失约定由 "B" 改为 `None`。v0.45.135 选 "B" 的理由
        # （"B+" 是基准档、会与"质量正好中等"同形）方向对，但**选中了众数**——
        # 生产实测 "B" 占真实等级 57.4%（461/803），58 份缺失与它完全同形，
        # 且 "B" 是合法枚举值 ⇒ `ml_predictor._missing_features` 不算它缺。
        assert td.catalyst_quality is None, "缺失必须是 None，不是任何一个合法等级"

    @pytest.mark.parametrize("bad", [None, "n/a", float("nan"), True])
    def test_non_numeric_catalyst_is_treated_as_missing(self, bad):
        """`True` 也在内：bool 是 int 子类，本仓已有 5 处守卫显式排除它。"""
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores={"catalyst": bad})
        assert "catalyst_quality" in g._ml_input_missing, f"{bad!r} 应视为缺失"
        assert td.catalyst_quality is None      # v0.45.147：曾是 "B"

    def test_present_catalyst_is_not_flagged_missing(self):
        """成对断言：合法值必须**不**被标成缺失。

        少了这半边，「无条件把 catalyst_quality 塞进 _ml_input_missing」
        的偷懒实现也能全绿。
        """
        g = _gen()
        g._prepare_ml_input("XOM", _metrics(), _analysis(),
                            swarm_dimension_scores={"catalyst": 8.0})
        assert "catalyst_quality" not in g._ml_input_missing


class TestParamThreadsThrough:
    """运行时穿透：`generate_ml_enhanced_report` → `_prepare_ml_input`。

    上面 `TestProductionWiring` 用 AST 守调用点、`TestCatalystQualitySource`
    直接调被调函数——两头都测了，**中间这一跳没人测**。v0.45.126 的
    `inject_prefetched` 少传一参抛了六个月 TypeError，正是因为所有测试都直接
    调被调函数（`run_agent`），没有一条走完整条链。
    """

    def _wire(self, monkeypatch, captured):
        # ⚠️ 走完整条链就会碰到链上的副作用：v0.45.134 起
        # `generate_ml_enhanced_report` 会往**真实**的
        # `probability_scorecard_state/published.jsonl` 落一行账。本测试第一版
        # 就真写进去了一条 XOM——同 v0.45.131「在测试里 new 一个通知器对象＝
        # 一次对外动作」。穿透测试的价值来自完整，隔离必须显式做。
        #
        # 打 **源模块**的属性（不是消费方模块的）——被测代码是函数内
        # `from probability_scorecard import record_published`，调用时才求值源
        # 模块属性，所以打得中；实测确认过。反过来打消费方模块打不中
        # （MEMORY v0.45.72 记的正是那个方向）。
        import probability_scorecard as _ps
        ledger = []
        monkeypatch.setattr(_ps, "record_published",
                            lambda **kw: ledger.append(kw) or True)
        self._ledger = ledger
        gen = _gen()

        class _Analyzer:
            def generate_comprehensive_analysis(self, ticker, metrics, direction=None):
                return _analysis()

        class _Service:
            def predict_for_opportunity(self, data):
                captured.append(data)
                return {"probability": 0.5}

        gen.analyzer, gen.ml_service = _Analyzer(), _Service()
        gen.timestamp = datetime.datetime(2026, 9, 6, 12, 0, 0)  # 报告头用，钉死不取挂钟
        gen._training_data_source = "real"
        return gen

    def test_dimension_scores_reach_the_model_input(self, monkeypatch):
        before = _real_ledger_size()
        captured = []
        gen = self._wire(monkeypatch, captured)
        gen.generate_ml_enhanced_report(
            "XOM", _metrics(), swarm_direction="bullish",
            swarm_dimension_scores={"catalyst": 8.6})
        assert captured, "predict_for_opportunity 未被调用"
        assert captured[0].catalyst_quality == "A+", (
            f"催化剂分 8.6 应穿透成 A+，实得 {captured[0].catalyst_quality}")
        # 隔离自证：桩确实截住了，真账本一个字节没长
        assert self._ledger, "record_published 未被调用——桩没打中，隔离结论不成立"
        assert _real_ledger_size() == before, "测试写进了真实的 published.jsonl"

    def test_omitting_the_param_degrades_honestly(self, monkeypatch):
        """不传时不得崩，且必须标为缺失（旧调用方式仍可用）。"""
        captured = []
        gen = self._wire(monkeypatch, captured)
        gen.generate_ml_enhanced_report("XOM", _metrics())
        assert captured[0].catalyst_quality is None    # v0.45.147：曾是 "B"
        assert "catalyst_quality" in gen._ml_input_missing


class TestNoRatingDerivedCatalyst:
    """源码层守卫：不得再出现「rating → 催化剂等级」的映射表"""

    def test_no_rating_to_quality_table(self):
        src = (REPO / "generate_ml_report.py").read_text(encoding="utf-8")
        assert "rating_to_quality" not in src, (
            "rating 不是催化剂的度量，不得由它反推等级")

    def test_catalyst_quality_assigned_exactly_once(self):
        """死赋值守卫：line 357 曾写一次、line 400 覆盖一次。

        两次赋值让「特征恒为 0.5」这个错误结论看起来成立了半年。
        """
        import inspect
        src = inspect.getsource(MLEnhancedReportGenerator._prepare_ml_input)
        tree = ast.parse("\n".join(line[4:] if line.startswith("    ") else line
                                   for line in src.splitlines()))
        writes = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  for t in n.targets
                  if isinstance(t, ast.Name) and t.id == "catalyst_quality"]
        assert len(writes) == 1, f"catalyst_quality 应只赋值一次，实测 {len(writes)} 次"


class TestProductionWiring:
    """接线守卫：蜂群派生的三个参数必须**成组**出现在每个调用点。

    「测被调函数 ≠ 测接线」——v0.45.126 的 `inject_prefetched` 少传一参
    抛了六个月 TypeError，而所有测试都直接调被调函数，全绿。

    v0.45.140 加入第三个 `swarm_final_score`（此前 `final_score` 特征读
    `advanced_analysis["recommendation"]["score"]`，该键 803/803 份不存在）。
    三者同取自一个 `swarm_data[ticker]`，任何一个漏传都是半接线。
    """

    CALL = "generate_ml_enhanced_report"
    # v0.45.141：第三个参数 swarm_final_score 同样取自 swarm_data[ticker]
    SWARM_KWARGS = {"swarm_direction", "swarm_dimension_scores", "swarm_final_score"}

    def _call_sites(self, rel):
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == self.CALL]

    @pytest.mark.parametrize("rel,total", [
        ("generate_ml_report.py", 1),
        ("alpha_hive_daily_report.py", 2),
    ])
    def test_call_site_count_is_known(self, rel, total):
        """锚点自证：调用点数变了要先来改这条，别让守卫在空集上恒真。"""
        assert len(self._call_sites(rel)) == total

    def test_swarm_kwargs_travel_together(self):
        """三个参数来自同一个 `swarm_data[ticker]`，传一个漏一个就是半接线。"""
        for rel in ("generate_ml_report.py", "alpha_hive_daily_report.py"):
            for call in self._call_sites(rel):
                keys = {k.arg for k in call.keywords}
                got = keys & self.SWARM_KWARGS
                assert got in (set(), self.SWARM_KWARGS), (
                    f"{rel}:{call.lineno} 只传了 {got}，"
                    f"缺 {self.SWARM_KWARGS - got}")

    def test_swarm_path_actually_passes_them(self):
        """至少一个调用点真的传了——否则上一条在「都没传」时恒真。

        生产日扫走 `--swarm` → `run_swarm_scan` → `_generate_ml_reports`，
        那里 `swarm_data[ticker]` 就在下一行被用，拿得到。
        """
        wired = [c for rel in ("generate_ml_report.py", "alpha_hive_daily_report.py")
                 for c in self._call_sites(rel)
                 if {k.arg for k in c.keywords} >= self.SWARM_KWARGS]
        assert len(wired) == 2, (
            f"两个有蜂群数据的调用点都应接线，实测 {len(wired)} 个")
