"""v0.45.311：`chart.umd.min.js` 这个文件名此前独立硬编码在三处
（`report_deployer.CODE_SHIPPED_STATIC_ASSETS`、`report_web_assets.write_pwa_files`
的 Service Worker 预缓存清单、`templates/dashboard.html` 的 `<script src=...>`），
改成全仓库唯一字面量 `report_deployer.CHART_JS_FILENAME`，其余两处改为引用它。

本文件只验证"引用"这件事是真的——不是巧合地写了同一个字符串。用
`monkeypatch.setattr("report_deployer.CHART_JS_FILENAME", ...)` 把常量改成一个
容易辨认的哨兵值，断言两条下游（sw.js 预缓存清单 / 渲染出的 index.html）都
跟着变；不跟着变就说明还在用自己那份硬编码副本，而不是真的读了这个常量。
"""
from types import SimpleNamespace

import pytest

import report_deployer as rd


_SENTINEL = "chart.SENTINEL-v0.45.311.js"


class TestServiceWorkerPrecacheFollowsConstant:
    def test_sw_js_precache_list_contains_current_constant(self, tmp_path):
        import report_web_assets as rwa
        reporter = SimpleNamespace(report_dir=tmp_path)
        rwa.write_pwa_files(reporter)
        sw_content = (tmp_path / "sw.js").read_text(encoding="utf-8")
        assert rd.CHART_JS_FILENAME in sw_content
        assert f"'{rd.CHART_JS_FILENAME}'" in sw_content, (
            "预缓存清单里应该是一个带引号的 JS 字符串字面量")

    def test_mutation_sw_js_precache_follows_renamed_constant(self, tmp_path, monkeypatch):
        """变异检验：把常量改名，sw.js 里必须跟着变——不跟着变就是
        report_web_assets.py 自己攥了一份硬编码副本，没有真的引用常量。"""
        import report_web_assets as rwa
        monkeypatch.setattr(rd, "CHART_JS_FILENAME", _SENTINEL)
        reporter = SimpleNamespace(report_dir=tmp_path)
        rwa.write_pwa_files(reporter)
        sw_content = (tmp_path / "sw.js").read_text(encoding="utf-8")
        assert _SENTINEL in sw_content, (
            "改名后哨兵值应该出现在 sw.js 里——没出现说明预缓存清单没有真的读 "
            "report_deployer.CHART_JS_FILENAME")
        assert "chart.umd.min.js" not in sw_content, (
            "改名后旧文件名不该再出现——出现了说明这里还留着一份旧的硬编码")


class TestDashboardTemplateFollowsConstant:
    # render_dashboard_html 会走宏观数据 + 逐票补价两条取数腿（见
    # test_dashboard_renderer.py 同名夹具的注释），本文件只关心渲染出的
    # <script src> 是否跟着常量走，与行情无关——显式钉死成离线。
    @pytest.fixture(autouse=True)
    def _offline_sources(self, stub_yfinance, stub_cboe_vix):
        pass

    def _render(self, tmp_path):
        from dashboard_renderer import render_dashboard_html
        report = {"opportunities": [], "swarm_metadata": {}, "system_status": ""}
        return render_dashboard_html(
            report=report, date_str="2026-09-22", report_dir=tmp_path, opportunities=[],
        )

    def test_rendered_index_html_references_current_constant(self, tmp_path):
        html = self._render(tmp_path)
        assert f'<script defer src="{rd.CHART_JS_FILENAME}">' in html
        assert "{{ chart_js_filename }}" not in html, (
            "Jinja 占位符不该原样出现在渲染产物里——说明渲染时没有传这个变量")

    def test_mutation_rendered_html_follows_renamed_constant(self, tmp_path, monkeypatch):
        """变异检验：把常量改名，渲染出的 index.html 里的 <script src> 必须跟着变
        ——不跟着变就是 dashboard_renderer.py 没有真的把常量传给模板。"""
        monkeypatch.setattr(rd, "CHART_JS_FILENAME", _SENTINEL)
        html = self._render(tmp_path)
        assert f'<script defer src="{_SENTINEL}">' in html
        assert "chart.umd.min.js" not in html, (
            "改名后旧文件名不该再出现在渲染产物里")
