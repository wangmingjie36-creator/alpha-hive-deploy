"""`odds_score` / `risk_adj_score` / `final_score` 的服务路径死读者（v0.45.140）

缺陷
----
`generate_ml_report._prepare_ml_input` 里：

    _rec = analysis.get("recommendation", {})
    _ds  = analysis.get("dimension_scores", {})
    ...
    final_score   = _rec.get("score", 5.0)
    odds_score    = _ds.get("odds", 5.0)
    risk_adj_score= _ds.get("risk_adj", 5.0)

而 `analysis` 就是 `advanced_analyzer.generate_comprehensive_analysis()` 的返回值。
实测 **803/803 份生产 `analysis-*-ml-*.json` 的 `advanced_analysis` 里
没有 `dimension_scores` 键，`recommendation` 里也没有 `score` 键**
⇒ `_ds` 恒为 `{}`、`_rec.get("score")` 恒为 None ⇒ 三个特征恒为字面量 5.0。

与 v0.45.137 那两个死读者的关键区别
-----------------------------------
这三个**已经被 `_ml_input_missing` 如实报出**（生产 118 份 JSON 的
`input_features_missing` 逐字就是 `["final_score","odds_score","risk_adj_score"]`）。
所以它不是「静默降级」——没有人被骗。它是「**诚实地一直缺**」。

但诚实不等于无害，两点：
  1. 喂给模型的仍然是常数 5.0，而 5.0 在训练分布里**不是中性**（见下）；
  2. 账本本身也不准：它说「缺」，可真值一直躺在**同一份 JSON** 的
     `swarm_results` 里（745/803 三个全齐）。「取不到」与「没去取」被
     记成了同一件事。

真值在哪（与训练端逐字节同源）
------------------------------
    odds_score     ← swarm_dimension_scores["odds"]        （参数自 v0.45.135 已接线）
    risk_adj_score ← swarm_dimension_scores["risk_adj"]    （同上）
    final_score    ← swarm_results["final_score"]          （本版新增第三个参数）

训练端 `ml_predictor.build_training_data_from_db` 取的是
`predictions.dimension_scores` 与 `predictions.final_score`，
而 `Backtester.save_predictions(swarm_results)` 正是从
`swarm_results[ticker]["dimension_scores"] / ["final_score"]` 写进去的
⇒ 接线后两端**同一个 dict 的同一个键**，不是抄第二份公式。

实测口径（`build_training_data_from_db(db_path=.../pheromone.db)`，n=497）
------------------------------------------------------------------------
    odds_score      中位 7.540  方差 2.896   5.0 落在 ** 7.6 分位**
    risk_adj_score  中位 5.340  方差 2.388   5.0 落在 **40.2 分位**
    final_score     中位 5.440  方差 0.729   5.0 落在 **16.9 分位**

⇒ 常数 5.0 对 odds / final 是**系统性偏低**，不是「中性」。
   （与 v0.45.137 的 volatility 9.1 分位同一形状。）

实测影响（803 份重放，模型按生产口径在 497 条真实样本上训练）
--------------------------------------------------------------
    |Δprobability| 中位 0.0063、均值 0.0126、max 0.0625
    |Δ| > 0.02 的占 **27.3%**；下移 48.8% / 上移 11.1%
    分布 sd 0.0280 → 0.0333（该维终于开始变化）
    三特征 permutation importance 合计 **0.1357**（odds_score 单项排 5/12）

⚠️ 重放工具本身先栽过一次：`MLPredictionService().train_model()` 内部走默认
   db_path，在 worktree 里返回空集 → 降级到 8 条硬编码样本 → 模型恒输出 0.5
   （sd=0.0000）→ 对**任何**改动都报「无差异」。识别标志不是 Δ 那一列，
   是分布 sd。判据：每个「没有差异」先问对照集多大、工具有没有判别力。

世代边界（复核结论）
--------------------
**IC 重跑闸不需要**，独立复核四条：
  1. `predictions` 表 44 列实测无任何 ML 特征列
     （`final_score` / `dimension_scores` 是**蜂群**写入的，不是 ML 写的）。
  2. 生产走 `--swarm` → `run_swarm_scan`(1300) → `_post_scan_enrichment`(1412)
     → `save_predictions`(812)；`_generate_ml_reports`(2394) →
     `generate_ml_enhanced_report`(2177) → `_prepare_ml_input` **在其之后**，
     且 `generate_ml_report` 不写 pheromone.db。
  3. `ml_adjustment` 来自 `dimension == "ml_auxiliary"`，产出方
     `swarm_agents/rival_bee.py:241`——它在 line 114 **自建** `TrainingData`，
     不经过 `_prepare_ml_input`。
  4. `alpha_hive_daily_report.py:321`（`_analyze_ticker_safe`）虽然也调
     `generate_ml_enhanced_report`，但它在**非 `--swarm`** 的
     `run_daily_scan` 分支上，编排器恒传 `--swarm`（orchestrator.sh:527），
     且其产物进 `self.opportunities`，不进 `save_predictions`。
⇒ 不往 `ic_rerun_readiness._COHORT_HISTORY` 追加（追加会白白作废几个月样本）。

**但 `probability_scorecard.blend_scan` 受影响**：它 `load_ml_probabilities()`
把历史全部 `analysis-*-ml-*.json` 的 `ml_probability` 池化成一条时间序列，
而 2026-09-06 这天估计量被改了两次（v0.45.137 + 本版）。估计量换代而无人记录
＝静默混算。故在记分卡侧登记 `_ML_ESTIMATOR_GENERATIONS`，由 `blend_scan`
在输出里报出样本跨了几代（见 `tests/test_probability_scorecard.py` 同版新增断言）。
"""

import ast
import inspect
import math
import pathlib

import pytest

from generate_ml_report import MLEnhancedReportGenerator

REPO = pathlib.Path(__file__).resolve().parent.parent


def _gen():
    """跳过重量级 __init__——`_prepare_ml_input` 不依赖任何实例状态"""
    return MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)


def _metrics():
    return {"ticker": "XOM",
            "sources": {"yahoo_finance": {"current_price": 110.0,
                                          "price_change_5d": 1.2}}}


def _analysis():
    """生产实测形状：`advanced_analysis` 里既无 dimension_scores，
    `recommendation` 里也无 score —— 803/803 份都是这样。"""
    return {"recommendation": {"rating": "BUY", "action": "分批建仓"},
            "probability_analysis": {"win_probability_pct": 65.0},
            "options_analysis": {}}


def _dims(**kw):
    d = {"catalyst": 5.9, "sentiment": 5.0, "risk_adj": 4.2,
         "signal": 5.0, "odds": 7.6}
    d.update(kw)
    return d


def _prep(dims=None, final=None, analysis=None):
    return _gen()._prepare_ml_input(
        "XOM", _metrics(), analysis if analysis is not None else _analysis(),
        swarm_dimension_scores=dims, swarm_direction="bullish",
        swarm_final_score=final,
    )


# ───────────────────────────── 第 1 层：单元 ─────────────────────────────

class TestScoresComeFromSwarm:
    """三个特征必须取蜂群原值，不做任何变换（训练端也是原值）。"""

    @pytest.mark.parametrize("odds", [2.16, 5.0, 7.54, 10.0])
    def test_odds_is_the_raw_dimension_value(self, odds):
        assert _prep(_dims(odds=odds), final=5.4).odds_score == pytest.approx(odds)

    @pytest.mark.parametrize("risk_adj", [2.24, 5.0, 5.34, 10.0])
    def test_risk_adj_is_the_raw_dimension_value(self, risk_adj):
        assert _prep(_dims(risk_adj=risk_adj), final=5.4).risk_adj_score == pytest.approx(risk_adj)

    @pytest.mark.parametrize("final", [3.80, 5.0, 5.44, 8.74])
    def test_final_score_is_the_raw_swarm_value(self, final):
        assert _prep(_dims(), final=final).final_score == pytest.approx(final)

    def test_not_the_old_constant(self):
        """反向断言：喂非 5.0 的蜂群值时，三个特征都不许还是 5.0。

        旧实现读 `analysis["dimension_scores"]` / `recommendation["score"]`，
        两者在生产 `advanced_analysis` 里都不存在 ⇒ 恒落到字面量 5.0。
        """
        td = _prep(_dims(odds=8.8, risk_adj=3.3), final=7.7)
        assert (td.odds_score, td.risk_adj_score, td.final_score) == \
            pytest.approx((8.8, 3.3, 7.7))

    def test_advanced_analysis_keys_are_not_consulted(self):
        """即便 `advanced_analysis` 里**有**这些键（历史 schema 或第三方注入），
        也必须以蜂群参数为准——两端同源的唯一真相是蜂群，不是分析器。"""
        poisoned = _analysis()
        poisoned["dimension_scores"] = {"odds": 1.0, "risk_adj": 1.0}
        poisoned["recommendation"]["score"] = 1.0
        td = _prep(_dims(odds=8.8, risk_adj=3.3), final=7.7, analysis=poisoned)
        assert (td.odds_score, td.risk_adj_score, td.final_score) == \
            pytest.approx((8.8, 3.3, 7.7))


class TestMissingIsNone:
    """取不到就是 None，不挑兜底值——让 `input_features_missing` 与
    `imputed_features` / `feature_completeness` 两套账目对上。"""

    def test_all_missing_becomes_none(self):
        td = _prep(None, final=None)
        assert td.odds_score is None
        assert td.risk_adj_score is None
        assert td.final_score is None

    @pytest.mark.parametrize("bad", [None, True, False, float("nan"), "7.5"])
    def test_unusable_values_rejected(self, bad):
        """`bool` 是 `int` 子类、NaN 对任何比较都为假却是 truthy——
        本仓统一走 `_usable_dim`，不自己发明类型闸（v0.45.121 教训）。"""
        td = _prep(_dims(odds=bad), final=bad)
        assert td.odds_score is None
        assert td.final_score is None

    def test_legit_zero_is_kept(self):
        """0.0 是合法分（最强看空），不得被当成缺失——
        成对断言的另一半，少了它 `if not x` 型偷懒修法也会全绿。"""
        td = _prep(_dims(odds=0.0, risk_adj=0.0), final=0.0)
        assert td.odds_score == 0.0
        assert td.risk_adj_score == 0.0
        assert td.final_score == 0.0


class TestMissingLedgerIsAccurate:
    """`_ml_input_missing` 必须反映**蜂群**取不取得到，而不是
    `advanced_analysis` 里有没有那个键（旧实现恒报三个缺）。"""

    def test_present_scores_are_not_flagged(self):
        g = _gen()
        g._prepare_ml_input("XOM", _metrics(), _analysis(),
                            swarm_dimension_scores=_dims(),
                            swarm_direction="bullish", swarm_final_score=5.4)
        for name in ("odds_score", "risk_adj_score", "final_score"):
            assert name not in g._ml_input_missing, \
                f"{name} 可得却被记成缺失——旧实现在这里恒报缺"

    def test_missing_scores_are_flagged(self):
        g = _gen()
        g._prepare_ml_input("XOM", _metrics(), _analysis(),
                            swarm_dimension_scores=None,
                            swarm_direction="bullish", swarm_final_score=None)
        assert {"odds_score", "risk_adj_score", "final_score"} <= set(g._ml_input_missing)

    #: 两套账目对同一特征用的不同名字（诊断字段，无程序读者，不改名以保历史可比）。
    #: `catalyst_quality → catalyst` 是 v0.45.147 修完 direction 之后**测试自己抓出来的**
    #: 第三对：此前 catalyst 两边都「说缺」，命名差异被同向掩盖着看不见。
    #: `crowding_score → crowding` 同理（v0.45.146）：报告端用 TrainingData 字段名，
    #: `ml_predictor._FEATURE_SOURCES` 用短名。
    LEDGER_ALIAS = {"market_sentiment": "sentiment", "direction": "direction_encoded",
                    "catalyst_quality": "catalyst", "crowding_score": "crowding"}

    #: **已知仍未对上的特征**——它们缺失时喂的仍是合法字面量，于是
    #: `_ml_input_missing` 说缺、`ml_predictor._missing_features` 说不缺。
    #: 写成会失效的断言而不是注释（v0.45.113 判据）：新增缺口会红，
    #: 修好某一个也会红（提醒把它从这张表里删掉）。
    #:   iv_rank / put_call_ratio → 兜底 50.0 / 1.0（v0.45.50 起如此）。
    #:     与已修的几个不同：这两个的兜底只在**键缺失**时触发，而生产 803 份里
    #:     键都存在（值为 None ⇒ `.get(k, 默认)` 返回 None，两账恰好一致），
    #:     所以是**潜在**缺口而非实际发生的。留作已知，不在 v0.45.147 范围内。
    #: 已修并从本表移除：
    #:   catalyst_quality 兜底 "B"（v0.45.147；"B" 是生产众数 57.4%）
    #:   direction_encoded 兜底 0.0（v0.45.147；0.0 正是 "neutral"）
    KNOWN_LEDGER_GAPS = {"iv_rank", "put_call_ratio"}

    #: v0.45.146：agent_agreement 的真值来源。与 `dims` 同步给/不给——
    #: 生产里它们同取自一个 `swarm_data[ticker]`，一有俱有、一无俱无。
    AGENT_DIRECTIONS = {"ScoutBeeNova": "bullish", "OracleBeeEcho": "bullish",
                        "BuzzBeeWhisper": "neutral", "BearBeeContrarian": "bearish"}

    def _ledgers(self, dims, final):
        from ml_predictor import _missing_features
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores=dims,
                                 swarm_direction="bullish" if dims else None,
                                 swarm_final_score=final,
                                 swarm_agent_directions=(self.AGENT_DIRECTIONS
                                                         if dims else None))
        mine = {self.LEDGER_ALIAS.get(n, n) for n in g._ml_input_missing}
        return mine, set(_missing_features(td))

    #: 缺失时值为 `None`（而非字面量）的特征——两套账目在这些维上必须完全一致。
    #: v0.45.137 三个 + v0.45.141 一个 + v0.45.146 两个 + v0.45.147 两个。
    #: 名字里不写数量：`FIVE` 装了七个就是「列名说谎」，本仓已为此付过账；
    #: 数量写进注释也一样会过期，故只列版本、不写总数。
    #: ⚠️ 这张表漏一项 = 那一维的账目差异**不会有人红**（下面那条只比交集）。
    #: 全 12 维的兜底由 `test_remaining_ledger_gaps_are_exactly_the_known_ones` 兜。
    NONE_BACKED_FEATURES = {"volatility", "sentiment", "final_score", "odds_score",
                            "risk_adj_score", "crowding", "agent_agreement",
                            "catalyst", "direction_encoded"}

    def test_two_ledgers_agree_on_none_backed_features(self):
        """改成 `None` 的那批特征必须两套账目完全对上。

        生产现存 118 份记录两者当面矛盾（前者说缺三个、后者说 12/12 齐全），
        根因就是喂了字面量 5.0。
        """
        F = self.NONE_BACKED_FEATURES
        assert F, "集合为空则以下断言在空集上恒真"
        for dims, final in ((_dims(), 5.4), (None, None)):
            mine, theirs = self._ledgers(dims, final)
            assert mine & F == theirs & F, (
                f"None 化特征上两套账目不一致："
                f"我 {sorted(mine & F)} vs ml_predictor {sorted(theirs & F)}")

    def test_remaining_ledger_gaps_are_exactly_the_known_ones(self):
        """全 12 维对账：不一致的集合必须**恰好**等于已知缺口。

        `!=` 两个方向都测：多出新缺口会红（回归），少了某个也会红
        （有人修好了 → 该更新这张表，而不是让它继续说谎）。
        ⚠️ 早先我只比对三个 score 特征就宣称「803/803 两套账目一致」——
        那个过滤把 catalyst_quality / direction / 命名不一致全挡在视野外。
        对账测试若先筛掉一半特征，它证明的就不是「账目对上了」。
        """
        mine, theirs = self._ledgers(None, None)   # 全缺场景，缺口全部现形
        assert not (theirs - mine), \
            f"ml_predictor 说缺、_ml_input_missing 没说：{sorted(theirs - mine)}"
        assert (mine - theirs) == self.KNOWN_LEDGER_GAPS, (
            f"实际缺口 {sorted(mine - theirs)} ≠ 已知缺口 "
            f"{sorted(self.KNOWN_LEDGER_GAPS)}——新增了缺口，或修好了某个"
            f"（修好了就把它从 KNOWN_LEDGER_GAPS 删掉）")

    def test_no_gap_when_everything_is_available(self):
        """成对断言的另一半：数据齐全时两套账目都应为空。
        少了它，「无条件报缺」也能让上面那条全绿。"""
        mine, theirs = self._ledgers(_dims(), 5.4)
        assert theirs == set(), f"ml_predictor 报了缺失：{sorted(theirs)}"
        assert mine <= {"iv_rank", "put_call_ratio"}, (
            f"数据齐全却报缺（iv/pcr 由夹具的空 options_analysis 造成）：{sorted(mine)}")

# ───────────────────── 第 2 层：源码守卫（AST，非子串）─────────────────────

class TestNoDeadAnalysisReaders:
    """死读者必须**删掉**，不是留着当 fallback。

    判据（v0.45.113）：fallback 若在生产上从没读到过真值，该删不该修。
    实测 803/803 份 `advanced_analysis` 无 `dimension_scores`、
    `recommendation` 无 `score` ⇒ 这两个读点从未产出过观测值。

    取 AST 而非子串：子串守卫会被解释这件事的注释自己触发（v0.45.137 教训）。
    """

    @staticmethod
    def _fn_ast():
        src = inspect.getsource(MLEnhancedReportGenerator._prepare_ml_input)
        return ast.parse(inspect.cleandoc(src))

    def test_no_dimension_scores_lookup_on_analysis(self):
        """`analysis.get("dimension_scores")` 这个读点必须不复存在。"""
        hits = [n for n in ast.walk(self._fn_ast())
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "get"
                and getattr(n.func.value, "id", "") == "analysis"
                and n.args and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "dimension_scores"]
        assert not hits, "advanced_analysis 里没有 dimension_scores 键，读它是死码"

    def test_no_recommendation_score_lookup(self):
        """`_rec.get("score")` 必须不复存在。"""
        hits = [n for n in ast.walk(self._fn_ast())
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "get"
                and n.args and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "score"]
        assert not hits, "advanced_analysis.recommendation 里没有 score 键，读它是死码"

    def test_no_literal_five_fallback_on_the_three_scores(self):
        """三个 keyword 实参都不许再写 `X.get(k, 5.0)` 型字面量兜底。"""
        bad = []
        for call in ast.walk(self._fn_ast()):
            if not isinstance(call, ast.Call):
                continue
            for kw in call.keywords:
                if kw.arg not in ("odds_score", "risk_adj_score", "final_score"):
                    continue
                for sub in ast.walk(kw.value):
                    if isinstance(sub, ast.Constant) and sub.value == 5.0:
                        bad.append(kw.arg)
        assert not bad, f"这些实参里仍有字面量 5.0 兜底：{sorted(set(bad))}"

    def test_the_three_features_are_assigned_exactly_once(self):
        """各只有一个赋值点——防「新写一份、旧的没删」（v0.45.80 同族）。"""
        tree = self._fn_ast()
        for name in ("_odds_raw", "_final_raw"):
            n = sum(1 for t in ast.walk(tree) if isinstance(t, ast.Name)
                    and isinstance(t.ctx, ast.Store) and t.id == name)
            assert n == 1, f"{name} 赋值 {n} 次，应为 1"


# ────────────────────── 第 3 层：运行时穿透（不碰生产状态）──────────────────

class TestNoConstantLeaksToModel:
    """穿透到模型输入：三个特征不得再是恒定常数。"""

    def test_varying_swarm_scores_vary_the_model_input(self):
        seen = set()
        for odds, risk, fin in ((2.2, 2.2, 3.9), (5.0, 5.0, 5.0), (9.9, 8.1, 8.7)):
            td = _prep(_dims(odds=odds, risk_adj=risk), final=fin)
            seen.add((td.odds_score, td.risk_adj_score, td.final_score))
        assert len(seen) == 3, "三组不同的蜂群输入产出了相同的特征——仍是常数"

    def test_none_survives_the_model(self):
        """None 必须走得通全链（HGB 原生支持 NaN；SimpleMLModel 走
        `normalize_feature` 的 `_FEATURE_NEUTRAL`）——不许抛。"""
        from ml_predictor import SimpleMLModel, _missing_features
        td = _prep(None, final=None)
        assert {"odds_score", "risk_adj_score", "final_score"} <= set(_missing_features(td))
        p = SimpleMLModel().predict_probability(td)
        assert 0.0 <= p <= 1.0 and math.isfinite(p)


# ───────────────────────── 第 4 层：生产接线 ─────────────────────────

class TestFinalScoreParamThreadsThrough:
    """新增的第三个参数必须真的从生产调用点走到模型输入。

    「测被调函数 ≠ 测接线」——v0.45.126 的 `inject_prefetched` 少传一参
    抛了六个月 TypeError，而所有测试都直接调被调函数，全绿。
    """

    def test_signature_accepts_the_param(self):
        for fn in (MLEnhancedReportGenerator.generate_ml_enhanced_report,
                   MLEnhancedReportGenerator._prepare_ml_input):
            assert "swarm_final_score" in inspect.signature(fn).parameters, \
                f"{fn.__name__} 缺 swarm_final_score 参数"

    def test_it_reaches_the_model_input(self, monkeypatch):
        """从 `generate_ml_enhanced_report` 入口进，看 `_prepare_ml_input`
        的产物里 final_score 是不是那个值。"""
        g = _gen()
        captured = {}

        class _FakeAnalyzer:
            def generate_comprehensive_analysis(self, ticker, metrics, direction=None):
                return _analysis()

        class _FakeSvc:
            def predict_for_opportunity(self, ml_input):
                captured["td"] = ml_input
                return {"prediction": {"probability": 0.5}}

        g.analyzer = _FakeAnalyzer()
        g.ml_service = _FakeSvc()
        g.timestamp = __import__("datetime").datetime(2026, 9, 6, 12, 0, 0)
        g._training_data_source = "real"
        monkeypatch.setattr(g, "_combine_recommendations",
                            lambda a, m: {}, raising=False)
        # 概率账本：本测试不得写生产状态
        import probability_scorecard as _ps
        monkeypatch.setattr(_ps, "record_published", lambda **kw: None)

        g.generate_ml_enhanced_report(
            "XOM", _metrics(), swarm_direction="bullish",
            swarm_dimension_scores=_dims(odds=8.8, risk_adj=3.3),
            swarm_final_score=7.77,
        )
        td = captured["td"]
        assert td.final_score == pytest.approx(7.77)
        assert td.odds_score == pytest.approx(8.8)
        assert td.risk_adj_score == pytest.approx(3.3)

    def test_production_call_sites_pass_it(self):
        """AST 核对生产调用点真的传了第三个参数（不是只改签名）。"""
        wired = 0
        for rel in ("generate_ml_report.py", "alpha_hive_daily_report.py"):
            tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
            for call in ast.walk(tree):
                if not (isinstance(call, ast.Call)
                        and getattr(call.func, "attr", "") == "generate_ml_enhanced_report"):
                    continue
                if "swarm_final_score" in {k.arg for k in call.keywords}:
                    wired += 1
        assert wired == 2, f"两个有蜂群数据的调用点都应传，实测 {wired} 个"

# ═══════════════════════════════════════════════════════════════════════
#  v0.45.147：两套账目最后两个真缺口
# ═══════════════════════════════════════════════════════════════════════

class TestCatalystMissingIsNone:
    """`catalyst_quality` 缺失时不得兜底成 `"B"`。

    v0.45.135 选 `"B"` 的理由是「`B+` 是 magnitude 1.0 的基准档，用它会让
    『拿不到数据』与『质量正好中等』不可区分」——方向对，但选中了**众数**：
    生产实测 `"B"` 占真实等级的 **57.4%**（461/803），是全部五档里最常见的一档。
    于是 58 份缺失与 461 份真实 "B" 完全同形，比用 "B+" 更糟。
    """

    def test_missing_catalyst_is_none_not_b(self):
        td = _prep(_dims(catalyst=None), final=5.4)
        assert td.catalyst_quality is None, \
            f"缺失兜底成了 {td.catalyst_quality!r} —— 与真实等级同形"

    @pytest.mark.parametrize("bad", [None, True, False, float("nan"), "B"])
    def test_unusable_catalyst_scores_rejected(self, bad):
        assert _prep(_dims(catalyst=bad), final=5.4).catalyst_quality is None

    # 切点唯一真相 = `ml_predictor._CATALYST_GRADE_CUTS`（8.5/7.5/6.5/5.5），
    # 逐档实测取值，不凭印象写期望
    @pytest.mark.parametrize("score,grade", [(1.0, "C"), (5.0, "C"), (5.9, "B"),
                                             (6.5, "B+"), (7.5, "A"), (9.5, "A+")])
    def test_real_scores_still_map_normally(self, score, grade):
        """成对断言的另一半：真实分照常出等级，含真实的 "B"。
        少了它，「无条件返回 None」也能让上面全绿。"""
        assert _prep(_dims(catalyst=score), final=5.4).catalyst_quality == grade

    def test_encoder_maps_none_to_nan_not_a_phantom_grade(self):
        """`_encode_catalyst(None)` 必须是 NaN，不是 0.5。

        0.5 落在 C(0.40) 与 B(0.55) **之间**——一个真实等级永远产不出的值，
        树模型却会拿它当一个真实的中间档去切分。改 NaN 后 HGB 走原生缺失处理，
        与其余五个 `None` 特征同一条路。
        """
        from ml_predictor import _encode_catalyst
        assert math.isnan(_encode_catalyst(None))
        for g, v in (("A+", 1.0), ("A", 0.85), ("B+", 0.70), ("B", 0.55), ("C", 0.40)):
            assert _encode_catalyst(g) == pytest.approx(v), f"{g} 的编码被改动了"

    def test_encoding_table_has_one_source_of_truth(self):
        """`ml_predictor` 内三处编码入口必须给出同一答案（含 None）。

        v0.45.142 已为**档位阈值**收掉重复，**编码表**当时还各写一份
        （`SimpleMLModel.encode_catalyst_quality` 与 `_encode_catalyst`），
        改一处漏一处就会静默产生两套口径。
        ⚠️ `ml_predictor_extended.py:567` 还有第三份——它是 `ml_predictor`
        导入失败时的应急降级、只有 rival_bee 会走且从不传 None，本版不动，
        在此登记以免遗忘。
        """
        from ml_predictor import _encode_catalyst, SimpleMLModel, HGBModel
        m = SimpleMLModel()
        for q in ("A+", "A", "B+", "B", "C"):
            assert m.encode_catalyst_quality(q) == _encode_catalyst(q), q
        assert math.isnan(m.encode_catalyst_quality(None)), \
            "SimpleMLModel 仍有自己那份表（None → 0.5）"
        h = HGBModel.__new__(HGBModel)
        assert math.isnan(h.encode_catalyst_quality(None))


class TestDirectionMissingIsNone:
    """`direction_encoded` 缺失时不得兜底成 `0.0` —— 0.0 在方向表里正是
    `"neutral"`，一个**真实类别**。「方向不可得」与「蜂群判中性」因此同形。"""

    def test_missing_direction_is_none_not_zero(self):
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores=_dims(),
                                 swarm_direction=None, swarm_final_score=5.4)
        assert td.direction_encoded is None, \
            f"方向缺失兜底成了 {td.direction_encoded!r} —— 与 neutral 同形"

    @pytest.mark.parametrize("bad", [None, "", "BUY", "STRONG BUY", "Bullish", 1.0])
    def test_unknown_direction_words_are_none(self, bad):
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores=_dims(),
                                 swarm_direction=bad, swarm_final_score=5.4)
        assert td.direction_encoded is None

    @pytest.mark.parametrize("d,v", [("bullish", 1.0), ("neutral", 0.0), ("bearish", -1.0)])
    def test_real_directions_pass_through(self, d, v):
        """成对断言：真实的 `neutral` 必须照常得到 0.0，不能被当成缺失。"""
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores=_dims(),
                                 swarm_direction=d, swarm_final_score=5.4)
        assert td.direction_encoded == pytest.approx(v)
        assert "direction" not in g._ml_input_missing
