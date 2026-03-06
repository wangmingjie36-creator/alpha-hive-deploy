"""dashboard_renderer 单元测试"""

import pytest
import json
from pathlib import Path


# ==================== 基础导入测试 ====================

class TestDashboardImport:
    def test_module_imports(self):
        """dashboard_renderer 应可正常导入"""
        import dashboard_renderer
        assert hasattr(dashboard_renderer, "render_dashboard_html")

    def test_css_loaded(self):
        """模块级 CSS 应已预加载"""
        from dashboard_renderer import _DASHBOARD_CSS
        assert isinstance(_DASHBOARD_CSS, str)
        assert len(_DASHBOARD_CSS) > 100  # CSS 至少有几百字符


# ==================== render_dashboard_html 测试 ====================

class TestRenderDashboard:

    @pytest.fixture
    def minimal_report(self):
        """最小可运行的 report 结构"""
        return {
            "opportunities": [
                {
                    "ticker": "NVDA",
                    "direction": "bullish",
                    "score": 7.8,
                    "confidence": 0.75,
                    "catalyst": "Q4 财报在即",
                    "risk": "AI 竞争加剧",
                    "thesis_break": "数据中心收入下滑",
                    "dimension_scores": {
                        "signal": 8.0, "catalyst": 7.5,
                        "sentiment": 7.0, "odds": 6.5, "risk_adj": 7.2,
                    },
                },
            ],
            "swarm_metadata": {
                "tickers_analyzed": 1,
                "total_agents": 7,
                "resonances_detected": 1,
            },
        }

    @pytest.fixture
    def report_dir(self, tmp_path):
        """带 swarm_results 文件的临时目录"""
        return tmp_path

    def test_renders_html_string(self, minimal_report, report_dir):
        from dashboard_renderer import render_dashboard_html
        html = render_dashboard_html(
            report=minimal_report,
            date_str="2026-03-06",
            report_dir=report_dir,
            opportunities=minimal_report["opportunities"],
        )
        assert isinstance(html, str)
        assert "<html" in html.lower()
        assert "NVDA" in html

    def test_renders_with_empty_opportunities(self, report_dir):
        from dashboard_renderer import render_dashboard_html
        report = {"opportunities": [], "swarm_metadata": {}}
        html = render_dashboard_html(
            report=report,
            date_str="2026-03-06",
            report_dir=report_dir,
            opportunities=[],
        )
        assert isinstance(html, str)
        assert "<html" in html.lower()

    def test_renders_with_swarm_results_file(self, minimal_report, tmp_path):
        """swarm_results JSON 存在时应读取详细数据"""
        from dashboard_renderer import render_dashboard_html

        # 写入 swarm_results 文件
        sr = {
            "NVDA": {
                "final_score": 7.8,
                "direction": "bullish",
                "agent_details": {},
                "agent_breakdown": {"bullish": 5, "bearish": 1, "neutral": 1},
            }
        }
        sr_path = tmp_path / ".swarm_results_2026-03-06.json"
        sr_path.write_text(json.dumps(sr))

        html = render_dashboard_html(
            report=minimal_report,
            date_str="2026-03-06",
            report_dir=tmp_path,
            opportunities=minimal_report["opportunities"],
        )
        assert "NVDA" in html

    def test_direction_labels(self, report_dir):
        """bullish/bearish/neutral 应正确映射为中文标签"""
        from dashboard_renderer import render_dashboard_html
        report = {
            "opportunities": [
                {"ticker": "TEST", "direction": "bearish", "score": 6.0,
                 "confidence": 0.5, "catalyst": "", "risk": "", "thesis_break": "",
                 "dimension_scores": {}},
            ],
            "swarm_metadata": {},
        }
        html = render_dashboard_html(
            report=report, date_str="2026-03-06",
            report_dir=report_dir, opportunities=report["opportunities"],
        )
        assert "看空" in html

    def test_custom_css(self, minimal_report, report_dir):
        """自定义 CSS 应覆盖默认"""
        from dashboard_renderer import render_dashboard_html
        custom_css = "body { background: red; }"
        html = render_dashboard_html(
            report=minimal_report, date_str="2026-03-06",
            report_dir=report_dir, opportunities=minimal_report["opportunities"],
            dashboard_css=custom_css,
        )
        assert "background: red" in html


# ==================== 模板文件检查 ====================

class TestTemplates:
    def test_template_directory_exists(self):
        from dashboard_renderer import _TPL_DIR
        assert _TPL_DIR.exists(), f"模板目录不存在: {_TPL_DIR}"

    def test_dashboard_css_exists(self):
        from dashboard_renderer import _TPL_DIR
        css_path = _TPL_DIR / "dashboard.css"
        assert css_path.exists(), "dashboard.css 模板文件缺失"
