"""`volatility` / `market_sentiment` 的服务路径必须与训练路径同源（v0.45.137）

缺陷
----
`generate_ml_report._prepare_ml_input` 里两条注释声称的修复从未生效：

    # BUG-6 修复：volatility 从 swarm BuzzBee details 提取，fallback 才用 5.0
    _buzz_details = (
        self._swarm_cache.get(metrics.get("_ticker", ""), {})
        ...
    ) if hasattr(self, "_swarm_cache") else {}

`self._swarm_cache` **全仓从未被赋值过**——AST 实测赋值点 0 个、读取点 1 个，
类属性里也没有，`hasattr(实例, "_swarm_cache")` 恒为 False。
（`tests/conftest.py` 的 `from swarm_agents import cache as _swarm_cache`
是同名局部变量，与本属性无关。）于是 `_buzz_details` 恒为 `{}`。

叠在上面的第二个独立缺陷：即便 `_swarm_cache` 存在，查表键
`metrics.get("_ticker", "")` 在生产 `realtime_metrics` 里也不存在
（生产用的是 `"ticker"`），查表会用空串落空。

后果：两个特征在生产上退化成常数。

实测（803 份生产 `analysis-*-ml-*.json`）
------------------------------------------
  · `ml_prediction.input.volatility`       = **5.0**，802/803（99.9%）
  · `ml_prediction.input.market_sentiment` = **0.0**，802/803（99.9%）
    （剩下 1 份是 v2 之前的旧 schema，无这两个键）
  · 而所需数据一直就在**同一份文件**里：743/803 有 BuzzBee details，
    其中 `sentiment_pct` 743 份齐全、`volatility_20d` 730 份齐全

volatility 的四级 fallback 链**每一级都是死的**：
  `_yf.get("volatility_20d")` / `_yf.get("atr_pct")` —— 生产
    `realtime_metrics.sources.yahoo_finance` 只有 current_price /
    price_change_5d / change_pct 三个键
  `_buzz_details.get(...)` —— 上述死读者
  `analysis["options_analysis"]["historical_volatility"]` —— 该键在
    803/803 份里**不存在**（真实字段名是 `rv_30d`，655 份有值）
只有字面量 `5.0` 会触发。

为什么不能照注释说的接 BuzzBee 的真实波动率
--------------------------------------------
生产训练集（`ml_predictor.build_training_data_from_db`，实测 n=497）里
这个特征槽装的**不是**波动率：

    volatility = max(1.0, (10.0 - risk_adj) * 2.5)      # 中位 11.65，方差 14.4
    market_sentiment = (sentiment - 5.0) * 20.0         # 中位 0.40，方差 355

而 BuzzBee 的 `volatility_20d` 是真实年化波动率（中位 39.66，与
`options_analysis.rv_30d` ρ=+0.787），与训练口径 **ρ=+0.068（≈ 无关）**、
scale 差 ~4×。把它接进服务端 = 用 A 训练、拿 B 服务，
制造一个和 v0.45.135 同species 的 train/serve skew，比现在的常数更糟。

`sentiment` 侧没有这个问题：蜂群 sentiment 维分与 BuzzBee `sentiment_pct`
ρ=**+0.987**，是同一个量的两种刻度；取维分可与训练路径**逐字节**同源。

⇒ 两个特征都改由 `swarm_dimension_scores` 派生，公式与训练路径共用同一个
   函数（`ml_predictor.volatility_from_risk_adj` /
   `market_sentiment_from_score`），不是抄第二份常数。

⚠️ 特征槽名叫 volatility、内容是 risk_adj 的反转代理——这一条**没修**，
   改它要动训练口径（`predictions` 表无波动率列 ⇒ 需要前向累积 + 世代边界）。
   本版只保证两端同源。

影响面（决定要不要世代边界）
----------------------------
独立复核 v0.45.135 的结论，三条：
  1. `predictions` 表列清单里**无** ML 特征列（无 volatility / sentiment /
     ml_probability）；ML 相关的 `iv_rank` / `put_call_ratio` / `options_score`
     来自蜂群与期权路径，不经 `_prepare_ml_input`。
  2. `Backtester.save_predictions(swarm_results)` 在
     `alpha_hive_daily_report.py:812` 执行，ML 报告在 line 2394 才生成，
     且 `generate_ml_report` 不写 pheromone.db。
  3. 蜂群 `final_score` 里的 `ml_adjustment` 来自 `dimension == "ml_auxiliary"`
     的结果，产出方是 `RivalBeeVanguard` / `CodeExecutorAgent`
     （`parallel_agent_runner.py:296/299`），**不经过** `_prepare_ml_input`
     ——`_prepare_ml_input` 全仓只有一个生产调用点。
⇒ 不进 IC 测量管道，不需要往 `ic_rerun_readiness._COHORT_HISTORY` 加世代边界。

实测影响（803 份重放，模型按生产路径训练）
------------------------------------------
`probability` 有 **52.4%** 的样本变动 > 0.02；唯一值 102 → 213。
常数 5.0 落在训练 volatility 分布的 **9.1 分位**——不是中性，是系统性偏低。
"""

import ast
import datetime
import inspect
import pathlib

import pytest

from generate_ml_report import MLEnhancedReportGenerator
from ml_predictor import market_sentiment_from_score, volatility_from_risk_adj

REPO = pathlib.Path(__file__).resolve().parent.parent


def _gen():
    """跳过重量级 __init__——`_prepare_ml_input` 不依赖任何实例状态"""
    return MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)


def _real_ledger_size():
    """真实概率账本的字节数；不存在记 -1。用于自证测试没碰生产状态。"""
    led = REPO / "probability_scorecard_state" / "published.jsonl"
    return led.stat().st_size if led.exists() else -1


def _metrics():
    """生产实测形状：没有 `_ticker` 键，yahoo_finance 只有三个价格字段"""
    return {"ticker": "XOM",
            "sources": {"yahoo_finance": {"current_price": 110.0,
                                          "price_change_5d": 1.2}}}


def _analysis():
    """生产实测形状：`advanced_analysis` 里没有 dimension_scores 键"""
    return {"recommendation": {"rating": "BUY", "action": "分批建仓"},
            "probability_analysis": {"win_probability_pct": 65.0},
            "options_analysis": {}}


def _dims(**kw):
    d = {"catalyst": 5.9, "sentiment": 5.0, "risk_adj": 5.0,
         "signal": 5.0, "odds": 5.0}
    d.update(kw)
    return d


class TestVolatilitySource:
    """volatility 必须由蜂群 risk_adj 维分按训练口径派生"""

    @pytest.mark.parametrize("risk_adj", [2.24, 4.0, 5.0, 7.24, 10.0])
    def test_derived_from_risk_adj(self, risk_adj):
        td = _gen()._prepare_ml_input(
            "XOM", _metrics(), _analysis(),
            swarm_dimension_scores=_dims(risk_adj=risk_adj))
        assert td.volatility == volatility_from_risk_adj(risk_adj), (
            f"risk_adj={risk_adj} 应派生 {volatility_from_risk_adj(risk_adj)}，"
            f"实得 {td.volatility}")

    def test_not_the_old_constant(self):
        """旧实现对任何输入都给 5.0。这条单独钉死那个常数。"""
        vals = {_gen()._prepare_ml_input(
                    "XOM", _metrics(), _analysis(),
                    swarm_dimension_scores=_dims(risk_adj=r)).volatility
                for r in (2.24, 5.0, 7.24, 10.0)}
        assert len(vals) == 4, f"volatility 应随 risk_adj 变化，实测取值集合 {vals}"
        assert 5.0 not in vals or len(vals) > 1

    def test_yahoo_volatility_20d_does_not_leak_in(self):
        """真实年化波动率与训练口径 ρ=+0.068、scale 差 4×，不得进这个槽。

        旧链首级是 `_yf.get("volatility_20d")`——若保留，任何提供该字段的
        调用方都会静默制造 train/serve skew。
        """
        m = _metrics()
        m["sources"]["yahoo_finance"]["volatility_20d"] = 39.66
        m["sources"]["yahoo_finance"]["atr_pct"] = 41.0
        td = _gen()._prepare_ml_input("XOM", m, _analysis(),
                                      swarm_dimension_scores=_dims(risk_adj=7.24))
        assert td.volatility == volatility_from_risk_adj(7.24), (
            f"yahoo 的真实波动率不得覆盖训练口径，实得 {td.volatility}")


class TestMarketSentimentSource:
    """market_sentiment 必须由蜂群 sentiment 维分按训练口径派生"""

    @pytest.mark.parametrize("sentiment", [1.66, 4.0, 5.0, 6.52, 8.1])
    def test_derived_from_sentiment_dim(self, sentiment):
        td = _gen()._prepare_ml_input(
            "XOM", _metrics(), _analysis(),
            swarm_dimension_scores=_dims(sentiment=sentiment))
        assert td.market_sentiment == market_sentiment_from_score(sentiment), (
            f"sentiment={sentiment} 应派生 "
            f"{market_sentiment_from_score(sentiment)}，实得 {td.market_sentiment}")

    @pytest.mark.parametrize("sentiment,expected", [
        (5.02, 0.4),    # 旧三段式量表判别会当成「0~1 概率量表」→ ×100 = 40.0
        (5.20, 4.0),    # 旧判别会当成「0~10 Agent 量表」→ ×10 = 40.0
        (4.80, -4.0),   # 负侧同理
    ])
    def test_near_neutral_is_not_rescaled(self, sentiment, expected):
        """派生值已在 -100~+100，再过一遍「三段式量表自动识别」会被放大 10~100 倍。

        这是本次改动最容易漏的一步：旧代码那段 `abs(_raw)<=1 → *100 /
        `abs(_raw)<=10 → *10` 的启发式，对**已经归好一的**输入是有害的。
        sentiment 维分落在 [4.5, 5.5] 的样本在生产里并不罕见。
        """
        td = _gen()._prepare_ml_input(
            "XOM", _metrics(), _analysis(),
            swarm_dimension_scores=_dims(sentiment=sentiment))
        assert td.market_sentiment == pytest.approx(expected), (
            f"sentiment={sentiment} 应为 {expected}，实得 {td.market_sentiment}"
            "（疑似又过了一遍量表启发式）")

    def test_metrics_sentiment_score_does_not_leak_in(self):
        """旧 fallback `metrics.get("sentiment_score", 0.0)` 不得再生效。"""
        m = _metrics()
        m["sentiment_score"] = 88.0
        td = _gen()._prepare_ml_input("XOM", m, _analysis(),
                                      swarm_dimension_scores=_dims(sentiment=4.0))
        assert td.market_sentiment == market_sentiment_from_score(4.0)


class TestMissingIsFlagged:
    """取不到时必须标为缺失——「没测到」不得渲染成「测了是中性」（v0.45.113 判据）"""

    @pytest.mark.parametrize("dims", [
        None, {}, {"catalyst": 5.9},
        _dims(risk_adj=None, sentiment=None),
        _dims(risk_adj="n/a", sentiment="n/a"),
        _dims(risk_adj=float("nan"), sentiment=float("nan")),
        _dims(risk_adj=True, sentiment=True),   # bool 是 int 子类
    ])
    def test_missing_dims_are_listed(self, dims):
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores=dims)
        assert "volatility" in g._ml_input_missing, (
            f"{dims!r}：volatility 应进 _ml_input_missing，实测 {g._ml_input_missing}")
        assert "market_sentiment" in g._ml_input_missing, (
            f"{dims!r}：market_sentiment 应进 _ml_input_missing，"
            f"实测 {g._ml_input_missing}")
        # 值为 None，好让 ml_predictor 自己的 `_missing_features` 也数得到——
        # 否则 `input_features_missing` 说缺、`feature_completeness` 说 12/12，
        # 两套账目当面矛盾（生产 JSON 里现存 118 份这种自相矛盾的记录）。
        assert td.volatility is None
        assert td.market_sentiment is None

    def test_present_dims_are_not_flagged(self):
        """成对断言：合法值必须**不**被标成缺失。

        少了这半边，「无条件把两个名字塞进 _ml_input_missing」也能全绿。
        """
        g = _gen()
        g._prepare_ml_input("XOM", _metrics(), _analysis(),
                            swarm_dimension_scores=_dims(risk_adj=7.24, sentiment=4.75))
        assert "volatility" not in g._ml_input_missing
        assert "market_sentiment" not in g._ml_input_missing

    def test_legit_zero_and_neutral_are_kept(self):
        """成对断言的第二半：合法的 0 / 中性值不得被当成缺失。

        `sentiment=5.0` 派生出 `0.0`，`risk_adj=10.0` 派生出 `1.0`——
        「`if not 值: 标缺失`」的偷懒写法会在这里翻车。
        """
        g = _gen()
        td = g._prepare_ml_input("XOM", _metrics(), _analysis(),
                                 swarm_dimension_scores=_dims(sentiment=5.0, risk_adj=10.0))
        assert td.market_sentiment == 0.0
        assert td.volatility == 1.0
        assert "market_sentiment" not in g._ml_input_missing
        assert "volatility" not in g._ml_input_missing


class TestNoDeadSwarmCacheReader:
    """源码守卫：死读者与错键必须消失，不能只是绕过去。

    ⚠️ 判据一律取 **AST 节点**，不取源码子串。子串守卫会被**解释这次修复的
    注释**本身触发（本文件与生产注释都必须写出 `_swarm_cache` 这个名字才能说清
    缺陷），于是要么逼着注释绕开事实、要么被改成宽松匹配——两条都是把守卫变
    装饰品。同 v0.45.129「判据取 AST 而非 `__doc__`」与 v0.45.112「数读者时要
    排除自身 def 行与自身错误消息字符串」。
    """

    @staticmethod
    def _module_tree():
        return ast.parse((REPO / "generate_ml_report.py").read_text(encoding="utf-8"))

    def test_no_swarm_cache_attribute_access(self):
        """`self._swarm_cache` 全仓无赋值点，读它恒得 {}——该删不该留。"""
        hits = [n.lineno for n in ast.walk(self._module_tree())
                if isinstance(n, ast.Attribute) and n.attr == "_swarm_cache"]
        assert hits == [], f"仍在读/写 self._swarm_cache，行号 {hits}"

    def test_no_underscore_ticker_string_literal(self):
        """生产 `realtime_metrics` 用 `ticker`；`_ticker` 全仓只有那一处死键。"""
        hits = [n.lineno for n in ast.walk(self._module_tree())
                if isinstance(n, ast.Constant) and n.value == "_ticker"]
        assert hits == [], f"仍有 \"_ticker\" 字面量，行号 {hits}"

    def test_no_hasattr_guard_on_self(self):
        """`hasattr(self, ...)` 恒 False 的守卫会把整段变成死代码。"""
        tree = ast.parse(inspect.getsource(
            MLEnhancedReportGenerator._prepare_ml_input).lstrip())
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "hasattr"
                and n.args and isinstance(n.args[0], ast.Name)
                and n.args[0].id == "self"]
        assert hits == [], "不得用 hasattr(self, ...) 守卫实例状态"

    def test_features_assigned_exactly_once(self):
        """死赋值守卫：v0.45.135 的 catalyst_quality 就是被 43 行后的第二次
        赋值覆盖，才让「特征恒为 0.5」这个错误结论看起来成立了半年。
        """
        src = inspect.getsource(MLEnhancedReportGenerator._prepare_ml_input)
        tree = ast.parse("\n".join(ln[4:] if ln.startswith("    ") else ln
                                   for ln in src.splitlines()))
        for name in ("volatility", "market_sentiment"):
            writes = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      for t in n.targets
                      if isinstance(t, ast.Name) and t.id == name]
            assert len(writes) == 1, f"{name} 应只赋值一次，实测 {len(writes)} 次"


class TestTrainServeParity:
    """两端必须调**同一个函数**，不是各抄一份常数。

    判据来自本仓 v0.45.109：看到魔数先问它是不是等于别处两个数的组合。
    `(10.0 - risk_adj) * 2.5` 与 `(sentiment - 5.0) * 20.0` 在服务端与训练端
    各写一份的话，改一处漏一处就是下一个 skew。
    """

    HELPERS = {"volatility_from_risk_adj", "market_sentiment_from_score"}

    def _called_names(self, func):
        tree = ast.parse(inspect.getsource(func).lstrip())
        return {n.func.id for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    def test_serving_path_calls_the_shared_helpers(self):
        import generate_ml_report as G
        got = self._called_names(G.MLEnhancedReportGenerator._prepare_ml_input)
        assert self.HELPERS <= got, f"服务端缺 {self.HELPERS - got}"

    def test_training_path_calls_the_shared_helpers(self):
        import ml_predictor as M
        got = self._called_names(M.build_training_data_from_db)
        assert self.HELPERS <= got, f"训练端缺 {self.HELPERS - got}"

    def test_helpers_match_the_measured_production_formula(self):
        """公式本身钉死——生产训练集实测中位 volatility 11.65 / sentiment 0.40，
        对应 risk_adj 中位 5.34 与 sentiment 维分中位 5.02。
        """
        assert volatility_from_risk_adj(5.34) == pytest.approx(11.65)
        assert volatility_from_risk_adj(10.0) == 1.0      # 下限钳位
        assert volatility_from_risk_adj(12.0) == 1.0      # 越界仍钳到 1.0
        assert market_sentiment_from_score(5.02) == pytest.approx(0.4)
        assert market_sentiment_from_score(0.0) == -100.0
        assert market_sentiment_from_score(10.0) == 100.0


class TestParamThreadsThrough:
    """运行时穿透：`generate_ml_enhanced_report` → `_prepare_ml_input`。

    「测被调函数 ≠ 测接线」——AST 守调用点、单测守被调函数，中间这一跳
    要有人走完整条链（v0.45.126 的 `inject_prefetched` 教训）。
    """

    def _wire(self, monkeypatch, captured):
        # 链上有真实副作用：v0.45.134 起 `generate_ml_enhanced_report` 会往真实的
        # `probability_scorecard_state/published.jsonl` 落账。打**源模块**属性
        # （被测代码是函数内 `from probability_scorecard import record_published`，
        # 调用时才求值源模块属性，所以打得中）。
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
        gen.timestamp = datetime.datetime(2026, 9, 6, 12, 0, 0)  # 钉死，不取挂钟
        gen._training_data_source = "real"
        return gen

    def test_both_features_reach_the_model_input(self, monkeypatch):
        before = _real_ledger_size()
        captured = []
        gen = self._wire(monkeypatch, captured)
        gen.generate_ml_enhanced_report(
            "XOM", _metrics(), swarm_direction="bearish",
            swarm_dimension_scores=_dims(risk_adj=7.24, sentiment=4.75))
        assert captured, "predict_for_opportunity 未被调用"
        assert captured[0].volatility == volatility_from_risk_adj(7.24)
        assert captured[0].market_sentiment == market_sentiment_from_score(4.75)
        # 隔离自证：桩确实截住了，真账本一个字节没长
        assert self._ledger, "record_published 未被调用——桩没打中，隔离结论不成立"
        assert _real_ledger_size() == before, "测试写进了真实的 published.jsonl"

    def test_omitting_the_param_degrades_honestly(self, monkeypatch):
        before = _real_ledger_size()
        captured = []
        gen = self._wire(monkeypatch, captured)
        gen.generate_ml_enhanced_report("XOM", _metrics())
        assert captured[0].volatility is None
        assert captured[0].market_sentiment is None
        assert "volatility" in gen._ml_input_missing
        assert "market_sentiment" in gen._ml_input_missing
        assert _real_ledger_size() == before
