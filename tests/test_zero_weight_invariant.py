"""零权重不变式：`config.EVALUATION_WEIGHTS` 里显式归零的维度必须一路保持为零。

v0.45.176 新增。**这组测试存在的理由是「谁会红？」答不上来。**

v0.45.172 把 signal / risk_adj 归零，v0.45.175 才发现生产实际权重里这两维仍占
~20% —— 中间整整一天没有任何东西变红，发现它靠的是人肉比对 `.swarm_results`
的字段。链路上有三层在 config 下游改写它：

    config → adapt_weights(0.2×config + 0.8×学习值) → QueenDistiller 整体替换
           → ML 反馈 ×factor 再归一 → RegimeWeightAdjuster 逐标的政体调整

挑「零维必须仍为零」当不变式的理由：它对下游所有**合法**变换都稳健 ——
乘法保零、政体偏移是相对的（`shift * w[k]`，w=0 时恒为 0）。所以它红了就一定
意味着有人接了一条会**改写** config 的通道，不会被 ML 反馈这类设计内调整误触发。

⚠️ 本文件**不用任何 skip / 模块级 pytestmark**：全部合成数据、零外部依赖，
任何机器上都必须跑得到（见 MEMORY `alpha-hive-failure-propagation` —— 模块级
`pytestmark` 会连坐无关测试，而 skip 把「这条没验」渲染成「这条没问题」）。

每条断言旁边都注明了**能让它变红的变异**，这是加断言的准入条件
（MEMORY：举不出变异先别加）。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from gex_regime import RegimeWeightAdjuster
from pheromone_board import PheromoneBoard
from swarm_agents.queen_distiller import QueenDistiller

# v0.45.172 起 config 的形状：两维显式归零
_ZEROED = ("signal", "risk_adj")
_CFG_SHAPED = {"signal": 0.0, "catalyst": 0.3320, "sentiment": 0.3250,
               "odds": 0.3430, "risk_adj": 0.0}

# 覆盖 RegimeWeightAdjuster 全部分支：宏观 ×3、GEX ×3、IV 三段
_REGIMES = [
    ("neutral", "unknown", None),
    ("risk_off", "unknown", None),
    ("risk_on", "unknown", None),
    ("neutral", "negative_gex", None),
    ("neutral", "positive_gex", None),
    ("risk_off", "negative_gex", 85.0),
    ("risk_on", "positive_gex", 15.0),
    ("neutral", "unknown", 50.0),
]


class TestRegimeAdjusterZeroInvariant:
    """`RegimeWeightAdjuster` 是链路最下游 —— 它不保零，上游怎么改都白搭。"""

    @pytest.mark.parametrize("macro,gex,iv", _REGIMES)
    def test_explicit_zero_stays_zero(self, macro, gex, iv):
        """变红的变异：删掉 `adjust_weights` 里的 `if w[k] <= 0: continue`。

        那正是 v0.45.176 之前的写法 —— `max(0.02, 0)` 把零复活成 2%，
        实测三种政体下 signal/risk_adj 全部落到 1.92%~1.97%。
        """
        w, _desc = RegimeWeightAdjuster().adjust_weights(
            base_weights=dict(_CFG_SHAPED), macro_regime=macro,
            gex_regime=gex, iv_rank=iv)
        for dim in _ZEROED:
            assert w[dim] == 0.0, (
                f"政体({macro}/{gex}/iv={iv})把显式归零的 {dim} 抬回了 {w[dim]:.4f}；"
                "地板 max(0.02,·) 的本意是「别把某维压到没有」，"
                "不是「不许某维为零」—— 0 是意图，不是「太小了」。")

    @pytest.mark.parametrize("macro,gex,iv", _REGIMES)
    def test_weights_still_normalized(self, macro, gex, iv):
        """成对的另一半：保零不能是靠「跳过归一化」实现的。

        变红的变异：把零豁免写成 `return w`（跳过末尾归一化）。
        """
        w, _ = RegimeWeightAdjuster().adjust_weights(
            base_weights=dict(_CFG_SHAPED), macro_regime=macro,
            gex_regime=gex, iv_rank=iv)
        assert abs(sum(w.values()) - 1.0) < 0.01, f"权重和 {sum(w.values()):.4f} != 1.0"

    def test_tiny_nonzero_is_still_floored(self):
        """地板的原有用途必须保留 —— 否则这次修复就是把一个 bug 换成另一个。

        变红的变异：把豁免条件从 `w[k] <= 0` 放宽成 `w[k] < 0.05`
        （那会让「极小但非零」也躲过地板，等于悄悄删掉地板）。
        """
        base = dict(_CFG_SHAPED, signal=0.001)
        w, _ = RegimeWeightAdjuster().adjust_weights(base_weights=base)
        assert w["signal"] > 0.01, (
            f"非零但极小的 0.001 应被地板抬升，实得 {w['signal']:.4f}——地板被误删了")
        assert w["risk_adj"] == 0.0, "同一次调用里，真正的零仍须保持为零"


class _FakeMLModel:
    """特征重要性全压在 crowding(→signal) 上：ML 反馈会试图放大 signal。"""

    def get_feature_importance(self):
        return {"crowding": {"weight": 0.90}, "momentum": {"weight": 0.02},
                "catalyst": {"weight": 0.02}, "iv_rank": {"weight": 0.02},
                "volatility": {"weight": 0.04}}


class TestMLFeedbackZeroInvariant:
    """ML 反馈层是乘法，天然保零 —— 但这件事必须被断言钉住，不能靠「碰巧」。"""

    def test_ml_feedback_cannot_resurrect_zero(self, monkeypatch):
        """变红的变异：把 `self.DIMENSION_WEIGHTS[dim] *= factor` 改成 `+= factor`
        或改成 `= factor`（两者都会让 signal 从 0 变成非零）。
        """
        import config as _cfg
        monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS", dict(_CFG_SHAPED), raising=False)

        queen = QueenDistiller(PheromoneBoard(), ml_model=_FakeMLModel(), enable_llm=False)

        assert queen.ml_feedback_enabled, (
            "本测试的前提是 ML 反馈**确实被激活了** —— 否则它是空跑，"
            "证明不了乘法保零（MEMORY：判「接没接上」不能看输出动没动）")
        assert queen.ml_adjustments.get("signal", 1.0) > 1.0, (
            "前提二：ML 确实在试图**放大** signal，这样保零才是被检验过的")
        for dim in _ZEROED:
            assert queen.DIMENSION_WEIGHTS[dim] == 0.0, (
                f"ML 反馈把归零的 {dim} 抬回了 {queen.DIMENSION_WEIGHTS[dim]}")


class TestAdaptedWeightsNotWiredToProduction:
    """v0.45.176 断线守卫：生产代码不许再把 adapted_weights 传给 QueenDistiller。

    ⚠️ 枚举驱动而非名单驱动（MEMORY：按名字匹配只能证明「匹配到的是对的」）——
    扫全仓每一个非测试 .py，AST 找出所有 `QueenDistiller(...)` 调用，
    断言没有一个带 `adapted_weights=` 关键字。
    """

    @staticmethod
    def _production_calls():
        root = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
        hits = []
        for py in root.rglob("*.py"):
            rel = py.relative_to(root)
            if rel.parts[0] in {"tests", ".git", "experiments"}:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "QueenDistiller"):
                    hits.append((str(rel), node.lineno,
                                 {kw.arg for kw in node.keywords if kw.arg}))
        return hits

    def test_enumeration_actually_found_something(self):
        """先自证探针有效 —— 一个调用点都没扫到时，下一条会恒真地绿。

        变红的变异：把 `rglob("*.py")` 写成 `rglob("*.pyx")`。
        """
        calls = self._production_calls()
        assert calls, ("AST 扫描没找到任何生产侧 QueenDistiller 调用点 —— "
                       "探针坏了，不是「没有违规」")

    def test_no_production_call_passes_adapted_weights(self):
        """变红的变异：把 `alpha_hive_daily_report.py` 里的
        `QueenDistiller(board, enable_llm=...)` 改回 `QueenDistiller(board,
        adapted_weights=_adapted_diag, ...)`（即 v0.45.175 及以前的写法）。
        """
        bad = [(f, ln) for f, ln, kws in self._production_calls()
               if "adapted_weights" in kws]
        assert not bad, (
            f"生产代码把 adapted_weights 传回了 QueenDistiller：{bad}。"
            "该通道自 v0.45.176 起是只读诊断 —— 它学的是「谁更爱说中性」而不是准头，"
            "接回去会整体顶掉 config.EVALUATION_WEIGHTS（理由见 "
            "`Backtester.adapt_weights` 的 docstring）。")


class TestZeroWeightDownstreamBehaviour:
    """真正的零权重会改变下游行为 —— 这个变化要被钉住，不能只在别处「顺手变绿」。

    旧的 `max(0.02, ·)` 地板一直在暗中保证「权重和 > 0」。修掉它之后，
    **只剩零权重维度可用**的标的，`weight_total == 0` ⇒ `base_score` 退回中性 5.0。
    这是配置意图的正确后果（手上只有 config 说不携带信息的维度，就该没话说），
    不是 bug —— 但它确实让 `tests/test_close_t7_production_wiring.py` 那条
    以 `signal` 为载体的测试变过一次红。那条已改用 `sentiment`，
    **本类是那次改动的正面对价**：行为变化在这里有断言，而不是被夹具吞掉。
    """

    def test_only_zeroed_dims_available_yields_neutral(self, monkeypatch):
        """变红的变异：把 `gex_regime` 的零豁免删掉（零被抬回 2% ⇒ 分数不再是 5.0）。"""
        import config as _cfg
        monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS", dict(_CFG_SHAPED), raising=False)
        queen = QueenDistiller(PheromoneBoard(), enable_llm=False)
        out = queen.distill("AAA", [{
            "score": 9.0, "direction": "bullish", "confidence": 0.9,
            "discovery": "只有被归零的维度", "source": "TestAgent",
            "dimension": "signal", "data_quality": {"test": "real"},
        }])
        assert out["final_score"] == pytest.approx(5.0, abs=0.05), (
            f"唯一可用维度权重为 0，分数却是 {out['final_score']} —— "
            "说明某处又把零权重抬回了非零，9.0 分因此漏进了 final_score")

    def test_weighted_dim_still_moves_the_score(self, monkeypatch):
        """成对的另一半：别把「全都中性」当成保零成功。

        变红的变异：把 `_compute_weighted_score` 的
        `base_score = weighted_sum / weight_total if weight_total > 0 else 5.0`
        改成无条件 `5.0`（那会让上一条恒绿，本条变红）。
        """
        import config as _cfg
        monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS", dict(_CFG_SHAPED), raising=False)
        queen = QueenDistiller(PheromoneBoard(), enable_llm=False)
        out = queen.distill("BBB", [{
            "score": 9.0, "direction": "bullish", "confidence": 0.9,
            "discovery": "权重非零的维度", "source": "TestAgent",
            "dimension": "sentiment", "data_quality": {"test": "real"},
        }])
        assert out["final_score"] != pytest.approx(5.0, abs=0.05), (
            "权重非零的维度给了 9.0 分，final_score 却仍是中性 —— "
            "评分链路是死的，上一条测试因此毫无意义")


class TestScanTimeObservationPoint:
    """扫描期观测点自身要有牙 —— 一个永远返回 True 的检查等于没有检查。"""

    def test_detects_the_actual_v0_45_175_incident(self, monkeypatch, caplog):
        """喂 09-09 生产实际权重，必须判违反。

        这些数字是真的：`.swarm_results_2026-09-09.json` 里 30 只标的
        `dimension_weights` 的加权均值。**这条问的是「当时它会不会红」。**

        变红的变异：让 `_assert_config_zeros_survive` 无条件 `return True`。
        """
        import config as _cfg
        from alpha_hive_daily_report import _assert_config_zeros_survive
        monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS", dict(_CFG_SHAPED), raising=False)

        incident = {"signal": 0.2059, "catalyst": 0.1399, "sentiment": 0.2694,
                    "odds": 0.1866, "risk_adj": 0.1982}
        with caplog.at_level(logging.ERROR):
            ok = _assert_config_zeros_survive(incident)
        assert ok is False, "v0.45.175 的实际事故权重没有被判违反 —— 观测点没牙"
        assert any("权重不变式违反" in r.getMessage() for r in caplog.records), \
            "判了违反却没打 error 日志 —— 无人值守的定时扫描里没人会知道"

    def test_passes_on_conforming_weights(self, monkeypatch):
        """成对的另一半：合规权重不许误报。

        变红的变异：让它无条件 `return False`。缺这条的话，
        「无条件 return False」能让上一条全绿。
        """
        import config as _cfg
        from alpha_hive_daily_report import _assert_config_zeros_survive
        monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS", dict(_CFG_SHAPED), raising=False)
        assert _assert_config_zeros_survive(dict(_CFG_SHAPED)) is True
