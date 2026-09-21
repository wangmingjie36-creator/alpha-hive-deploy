"""dashboard_renderer 单元测试"""

import pytest
import json


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

    # v0.45.207：本类不再标 `network`（接 v0.45.196）。它断言的是渲染出来的
    # HTML（字符串非空、方向标签中文化、自定义 CSS 注入、能读 swarm_results
    # 文件），没有一条依赖实时行情。出网有两条腿，探针实测：
    #   · `render_dashboard_html:2381` → `fred_macro.get_macro_context()`
    #     （只有本类第一条测试付代价，其余吃 `fred_macro._CACHE`）
    #   · `render_dashboard_html` → `_detail()`（dashboard_renderer.py:548）
    #     → `yf_gate` → `yfinance.Ticker(...).history("5d")` 逐票补价，
    #     **每条测试都走**，一条测试里调 4 次
    #
    # 这条曾是全套最慢的一条：`test_renders_html_string` 实测 30.3s，
    # 是下一个要撞上 `--timeout=60` 的候选。
    #
    # `_detail` 的取价包在 `except Exception: pass` 里（注释写明"取价失败绝不能
    # 拖垮仪表板渲染与部署"），所以钉死 yfinance 后它就是产线上限流时的真实形态。
    @pytest.fixture(autouse=True)
    def _offline_sources(self, stub_yfinance, stub_cboe_vix):
        """渲染会走宏观 + 逐票补价两条取数腿，显式钉死成离线。"""

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

    def test_macro_marquee_structure(self, minimal_report, report_dir):
        """宏观条自动滚动（v0.45.232）的结构不变式。

        宽度是否填满只能在浏览器里量（见 dashboard.html 里份数的注释），这里锁住量过之后的结构：
        - 份数为偶数、每半程 ≥ 4 份：少于 4 份时，数值全缺的情况下宽屏会在滚动后段露出空白；
        - 只有第一份给屏幕阅读器读，其余全部 aria-hidden，否则同一组数被朗读 8 遍；
        - 每份条目完全一致，否则 translateX(-50%) 回到起点时画面会跳；
        - sr-only 朗读列表与条目一一对应，改指标时漏改它会让读屏用户读到旧指标。
        """
        import re
        from dashboard_renderer import render_dashboard_html
        html = render_dashboard_html(
            report=minimal_report, date_str="2026-03-06",
            report_dir=report_dir, opportunities=minimal_report["opportunities"],
        )
        track = html.split('<div class="ah-macro-track">', 1)[1].split('<ul class="sr-only ah-macro-sr-list">', 1)
        assert len(track) == 2, "找不到宏观条的滚动轨道或 sr-only 朗读列表"
        track_html, sr_html = track[0], track[1].split("</ul>", 1)[0]

        copies = re.findall(r'<div class="ah-macro-items([^"]*)"([^>]*)>(.*?)\n    </div>', track_html, re.S)
        n = len(copies)
        assert n % 2 == 0 and n // 2 >= 4, (
            f"宏观条共 {n} 份，要求偶数且每半程 ≥ 4 份（理由见 templates/dashboard.html 的份数注释）")

        hidden = ['aria-hidden="true"' in attrs for _, attrs, _ in copies]
        assert hidden == [False] + [True] * (n - 1), "只能第一份给屏幕阅读器读，其余必须 aria-hidden"
        assert all("ah-macro-items--dup" in cls for cls, _, _ in copies[1:]), \
            "副本必须带 ah-macro-items--dup，减弱动效时靠它隐藏"

        names = [re.findall(r'<div class="ah-macro-name">([^<]+)</div>', body) for _, _, body in copies]
        assert names[0], "第一份里没有解析到任何指标名"
        assert all(nm == names[0] for nm in names), f"各份条目不一致：{names}"

        sr_items = re.findall(r"<li>([^<]*)</li>", sr_html)
        assert len(sr_items) == len(names[0]) and all(
            li.startswith(nm) for li, nm in zip(sr_items, names[0])), \
            f"sr-only 朗读列表 {sr_items} 与条目 {names[0]} 对不上"

    def test_macro_marquee_reduced_motion_hides_copies(self):
        """减弱动效时必须只剩一份、可手动横滑：F15 全局规则只把动画缩到 0.01ms，
        不隐藏副本的话用户会看到 8 份重复的指标。"""
        import re
        from dashboard_renderer import _TPL_DIR
        css = (_TPL_DIR / "dashboard.css").read_text(encoding="utf-8")
        blocks = re.findall(r"@media\(prefers-reduced-motion:reduce\)\{(.*?)\n\}", css, re.S)
        marquee = [b for b in blocks if "ah-macro" in b]
        assert marquee, "找不到宏观条的 prefers-reduced-motion 降级规则"
        assert re.search(r"\.ah-macro-items--dup\{display:none\}", marquee[0])
        assert re.search(r"\.ah-macro-viewport\{overflow-x:auto", marquee[0])



# ==================== 模板文件检查 ====================

class TestTemplates:
    def test_template_directory_exists(self):
        from dashboard_renderer import _TPL_DIR
        assert _TPL_DIR.exists(), f"模板目录不存在: {_TPL_DIR}"

    def test_dashboard_css_exists(self):
        from dashboard_renderer import _TPL_DIR
        css_path = _TPL_DIR / "dashboard.css"
        assert css_path.exists(), "dashboard.css 模板文件缺失"
