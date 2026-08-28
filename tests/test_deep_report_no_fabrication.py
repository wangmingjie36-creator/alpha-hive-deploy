"""深度报告：不可得的量不许渲染成定量结论（v0.45.53 · Phase 3）

深度报告是全系统最像「量化研究」的产出 —— 带概率、带区间、带希腊字母、
带公式展开。正因为如此，它编造的数字最难被识破。

本次审计在这里找到的两处：

  · 情景推演表：curr_price 兜底 100、gain_max or 20、gain_7d or 5、
    drawdown or -10 —— 四个常量撑起「$120/$105/$95/$85，概率加权期望价
    $104.75，+4.75%」外加一行「Σ(概率 × 情景价格) = 25%×$120 + …」
  · 置信区间：区间兜底 ±1.5、σ 兜底 1.5，两者互相自洽（band_w 由区间反推 3.0），
    渲染成「置信区间 [3.50–6.50]，信号不确定性 中等（σ=1.5）」

判据：**读者能不能把它与真实测算结果区分开。** 区分不开就不许渲染。
"""

import pytest

import generate_deep_v2 as g


BASE = {
    "final_score": 5.5,
    "direction": "bullish",
    "iv_skew": 1.0,
    "key_levels": {},
    "ticker": "NVDA",
}


class TestConfidenceBand:
    def test_missing_band_says_unavailable(self):
        html = g._build_scenario_narrative(dict(BASE))
        assert "不可得" in html, "缺维度分布数据时应明说不可得"

    def test_missing_band_does_not_fabricate_interval(self):
        """旧行为会由 score=5.5 反推出 [4.00–7.00] 并配 σ=1.5"""
        html = g._build_scenario_narrative(dict(BASE))
        assert "σ=1.5" not in html, "σ 是写死的常量，不得渲染成统计量"
        assert "4.00–7.00" not in html, "区间不得由 score ±1.5 反推"

    def test_real_band_still_rendered(self):
        """修复不能削弱有数据时的表达"""
        html = g._build_scenario_narrative(
            {**BASE, "confidence_band": [4.2, 6.8], "dimension_std": 0.9})
        assert "[4.20–6.80]" in html
        assert "σ=0.9" in html

    @pytest.mark.parametrize("bad", [None, [], [1], "x", [None, None], {"a": 1}])
    def test_malformed_band_does_not_crash(self, bad):
        html = g._build_scenario_narrative({**BASE, "confidence_band": bad})
        assert isinstance(html, str) and html

    def test_band_without_std_does_not_claim_sigma(self):
        """只有区间、没有 σ 时，不得凭空给一个 σ"""
        html = g._build_scenario_narrative({**BASE, "confidence_band": [4.2, 6.8]})
        assert "σ=1.5" not in html


class TestScenarioTable:
    """`generate_ml_report._ch5_scenarios` —— 同一形状，另一份报告"""

    @staticmethod
    def _render(analysis, swarm=None):
        from generate_ml_report import MLEnhancedReportGenerator as G
        return G._ch5_scenarios(G.__new__(G), analysis, swarm or {})

    def test_no_data_skips_table(self):
        html = self._render({})
        assert "情景推演不可用" in html
        for fake in ("$104.75", "$120.00", "$105.00", "+4.75%"):
            assert fake not in html, f"退化输入渲染出编造的 {fake}"

    def test_no_formula_line_when_unavailable(self):
        """那行「Σ(概率 × 情景价格) = …」最像量化结论，最不该编"""
        html = self._render({})
        assert "Σ(概率" not in html

    def test_real_data_still_renders_table(self):
        html = self._render({
            "historical_analysis": {"expected_returns": {
                "max_gain": {"mean": 18.0},
                "expected_7d": {"mean": 4.2},
                "max_drawdown": {"mean": -8.0, "min": -14.0}}},
            "position_management": {"stop_loss": {"conservative": 194.0}},
        })
        assert "概率加权期望价格" in html
        assert "Σ(概率" in html

    @pytest.mark.parametrize("missing", ["max_gain", "expected_7d", "max_drawdown"])
    def test_any_missing_component_skips_table(self, missing):
        """任一分量缺失即跳过 —— 部分真实 + 部分常量的表比全常量更难识破"""
        exp = {"max_gain": {"mean": 18.0}, "expected_7d": {"mean": 4.2},
               "max_drawdown": {"mean": -8.0}}
        exp.pop(missing)
        html = self._render({
            "historical_analysis": {"expected_returns": exp},
            "position_management": {"stop_loss": {"conservative": 194.0}},
        })
        assert "情景推演不可用" in html

    def test_zero_return_is_not_treated_as_missing(self):
        """`or 20` 会把真实的 0 换成常量；0 是合法的期望收益"""
        html = self._render({
            "historical_analysis": {"expected_returns": {
                "max_gain": {"mean": 0.0},
                "expected_7d": {"mean": 0.0},
                "max_drawdown": {"mean": 0.0}}},
            "position_management": {"stop_loss": {"conservative": 194.0}},
        })
        assert "情景推演不可用" not in html, "真实的 0 不是缺失"
