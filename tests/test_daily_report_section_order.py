"""日报小节必须在**产出它们的钩子跑完之后**才渲染（v0.45.104）。

二次复查实测：`run_swarm_scan` 里 `_build_swarm_report`（拼 markdown）比
`_post_scan_notify`（跑 earnings/VRP/Greeks 钩子）**早一行**。v0.45.101~103
把三个小节拼在了 `_build_swarm_report` 里，于是：

  · VRP 小节读 `rows_for_date(today)` —— 当日行要等 `record_day` 才写
    ⇒ 恒返回空串，**小节一天都不会出现**（活生生的「死字段」）
  · 期权纸面腿小节恒显示「扫描 0 个快照」，持仓标记停在昨天
  · 组合 Greeks 只能退回 `run_for_date(execute=False)` 重算一遍，
    且渲染的是对冲成交前的状态，与一分钟后落盘的审计文件对不上

这与 v0.45.57 的失效条件是同一个坑（`alpha_hive_daily_report` 1067 行注释）。
本测试钉住修法：三个 `_*_markdown()` 只能出现在 `_post_scan_notify` 里、
且必须排在三个钩子的 import 之后。把它们挪回 `_build_swarm_report` 即变红。
"""

import ast
import pathlib

import pytest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "alpha_hive_daily_report.py"

#: 三个小节的渲染方法
SECTION_METHODS = ("_options_paper_leg_markdown", "_vrp_markdown", "_portfolio_greeks_markdown")
#: 产出这三个小节数据的钩子模块
HOOK_MODULES = ("earnings_vol_signal", "vrp_signal", "portfolio_greeks")


@pytest.fixture(scope="module")
def methods():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_build_swarm_report", "_post_scan_notify"):
            out[node.name] = node
    assert set(out) == {"_build_swarm_report", "_post_scan_notify"}, \
        f"方法改名了？只找到 {sorted(out)}"
    return out


def _called_names(node):
    """节点里所有被调用的属性名（self._foo() → "_foo"）。"""
    return {c.func.attr for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}


def _import_lines(node, module_names):
    """节点里 `import X as _y` 的行号，按模块名归集。"""
    lines = {}
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in module_names:
                    lines.setdefault(a.name, n.lineno)
    return lines


class TestSectionsRenderAfterTheirHooks:

    def test_build_swarm_report_does_not_render_them(self, methods):
        """拼 markdown 的地方不得调用这三个渲染方法——它比钩子早跑。"""
        called = _called_names(methods["_build_swarm_report"])
        leaked = sorted(set(SECTION_METHODS) & called)
        assert not leaked, (
            f"{leaked} 在 _build_swarm_report 里被调用，而它比 _post_scan_notify 早一行跑；"
            "渲染出来的是钩子跑之前的状态（VRP 小节会恒空）")

    def test_post_scan_notify_renders_them(self, methods):
        called = _called_names(methods["_post_scan_notify"])
        missing = sorted(set(SECTION_METHODS) - called)
        assert not missing, f"{missing} 没有在 _post_scan_notify 里回填，小节不会进日报"

    def test_render_comes_after_every_hook(self, methods):
        """三个渲染调用必须排在三个钩子 import 之后（同一方法内的源码顺序）。"""
        notify = methods["_post_scan_notify"]
        hook_lines = _import_lines(notify, set(HOOK_MODULES))
        missing = sorted(set(HOOK_MODULES) - set(hook_lines))
        assert not missing, f"钩子 {missing} 不在 _post_scan_notify 里"

        render_lines = [c.lineno for c in ast.walk(notify)
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr in SECTION_METHODS]
        assert render_lines, "没找到渲染调用"
        assert min(render_lines) > max(hook_lines.values()), (
            f"渲染调用最早在第 {min(render_lines)} 行，而钩子最晚在第 "
            f"{max(hook_lines.values())} 行——渲染必须排在所有钩子之后")

    def test_markdown_report_is_appended_not_replaced(self, methods):
        """回填必须是**追加**，不能覆盖掉主报告正文。"""
        src = ast.get_source_segment(MODULE.read_text(encoding="utf-8"),
                                     methods["_post_scan_notify"]) or ""
        assert 'report["markdown_report"] = (report.get("markdown_report")' in src, \
            "回填要在既有 markdown_report 之后追加（用 .get 兜住 key 不存在）"
