"""`@pytest.mark.network` 的棘轮：**只能减，不能增**（v0.45.196）。

────────────────────────────────────────────────────────────────────────
为什么需要它
────────────────────────────────────────────────────────────────────────
`network` 是**刻意不进** addopts 默认排除的（v0.45.94 的决定：本机有网，
这些测试照常跑、照常有价值）。代价是：标了 `network` 的测试在默认 `pytest`
里**照跑**，而它们又被 `conftest._offline_transport` 豁免 —— 也就是说，
这个 marker 是**唯一**能让一条默认测试合法出网的开关。

marker 原文写的契约是「打真实外部端点，离线必挂」。v0.45.196 用 socket/curl
探针实测了默认选择集（`-m "not integration"`，4002 条），结论是这句**不成立**：

    默认选择集里真正出网的 23 条，在探针 block 模式下**全部 passed**。

它们一条都不「离线必挂」—— 断言全部建立在自己 monkeypatch 出来的值上，
出网是顺路带出来的副作用（典型：`_build_swarm_report` →
`_fetch_report_context` → `fred_macro.get_macro_context()`）。
于是 marker 实际起的作用不是「声明意图」，而是**把这条测试从离线闸里摘出去**，
让它的红绿交给 yahoo 的限流决定：`test_returns_required_keys` 单跑实测 63s
（用户处 36s），而 addopts 的 `--timeout=60` 正卡在中间。
随机变红与真实回归无法区分 —— 信号变成噪音。

v0.45.196 清掉 `test_pipeline.py` 两个类（12 → 0）；
v0.45.207 清掉余下的 `test_macro_snapshot.py`(7) 与
`test_dashboard_renderer.py`(4)。**默认选择集现在 0 条出网。**
本文件的作用是**不让它悄悄长回来**：marker 是默认离线的唯一豁免口，
新增一个就等于新增一条红绿不由代码决定的测试。

────────────────────────────────────────────────────────────────────────
两条断言，方向相反，缺一不可
────────────────────────────────────────────────────────────────────────
（判据见 CLAUDE.md「写白名单时问一句：我怕它变大，还是也怕它变小？」）

  · `_scan() - KNOWN` 非空 → **新增**了 marker。红。
  · `KNOWN - _scan()` 非空 → 有人**清掉**了 marker 却没缩这张表。红。
    只有子集语义时，表会悄悄过期：清干净了它同样不红，于是这张表
    从「当前真相」退化成「某个历史时刻的快照」。

⚠️ 枚举走 `tests/_repo_files.py::own_python_files` —— **不要**改回
`rglob`。生产 checkout 的 `.claude/worktrees/` 下挂着 14 个嵌套 worktree，
裸 rglob 会把它们陈旧副本里的 marker 一起算进来，而这个 bug
**只在跑每日扫描的那台机器上可见**（worktree 里没有嵌套 worktree）。
该行为已由 `test_paths_not_frozen_at_import.py` 覆盖，此处只负责用对。
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__

#: 当前允许打真外网的测试点（`<文件名>::<类或函数名>`）。
#:
#: ⚠️ **只能缩短。** 新测试要出网时，正确做法是给它补显式源桩
#: （`tests/conftest.py` 的 `stub_yfinance` / `stub_cboe_vix` / `stub_cboe_payload`
#: / `stub_http_gate` / `stub_reddit` / `stub_vixcentral`），参考
#: `test_pipeline.py::TestBuildSwarmReport::_offline_sources` 的写法。
#:
#: v0.45.207 起只剩 3 条（v0.45.196 时是 8 条）：
#:   · `test_offline_transport_gate::test_network_marked_tests_are_exempt`
#:     是闸自身的自证，marker 是它的**被测对象**，必须留。
#:   · 另 2 条（options_analyzer / treasury_yields）探针实测**并没有**真的出网
#:     （标错了）。留着不是因为该留，是因为核实它们各自该配什么源桩需要单独一版；
#:     它们不产生 flake，优先级低于已清掉的 23 条。
_KNOWN_NETWORK_MARKED = {
    "test_offline_transport_gate.py::test_network_marked_tests_are_exempt",
    "test_options_analyzer.py::test_analyze_survives_none_gex",
    "test_treasury_yields.py::test_caches_within_ttl",
}


def _is_network_mark(node: ast.expr) -> bool:
    """`@pytest.mark.network` / `@mark.network` / 带参数的 `@...network(...)`。"""
    while isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == "network"


def _scan(root=None) -> set[str]:
    """`root` 是**参数**不是模块常量 —— 调用时求值，变异测试才能指向 tmp 树。"""
    root = Path(root) if root is not None else REPO_ROOT
    files, _mode = own_python_files(root)
    found: set[str] = set()
    for path in files:
        if path.parent.name != "tests":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):       # pragma: no cover - 读不了就不该静默算作"干净"
            raise
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(_is_network_mark(d) for d in node.decorator_list):
                    found.add(f"{path.name}::{node.name}")
    return found


class TestNetworkMarkerRatchet:

    def test_no_new_network_markers(self):
        """新增 marker 即红 —— 它是默认离线闸的唯一豁免口。

        变红的变异：给任意一条测试加上 `@pytest.mark.network`。
        """
        new = _scan() - _KNOWN_NETWORK_MARKED
        assert not new, (
            f"新增了 {len(new)} 个 network marker：{sorted(new)}\n"
            "`network` 是 conftest._offline_transport 的豁免口：标上它，这条测试就\n"
            "会在默认 pytest 里真的出网，红绿取决于对方限流（实测 36~63s，\n"
            "addopts 的 --timeout=60 正卡在中间）。\n"
            "正确做法：给它补显式源桩（tests/conftest.py 的 stub_* 系列），\n"
            "参考 test_pipeline.py::TestBuildSwarmReport::_offline_sources。\n"
            "只有当这条测试**测的就是对接外部系统这件事本身**时才该标 marker，\n"
            "并把它加进 _KNOWN_NETWORK_MARKED。")

    def test_allowlist_has_not_gone_stale(self):
        """清掉了 marker 却没缩表也要红 —— 否则这张表会退化成历史快照。

        变红的变异：从 test_macro_snapshot.py 删掉任意一个
        `@pytest.mark.network`，不动本文件。
        """
        gone = _KNOWN_NETWORK_MARKED - _scan()
        assert not gone, (
            f"_KNOWN_NETWORK_MARKED 里有 {len(gone)} 条已不存在：{sorted(gone)}\n"
            "marker 清掉了是好事，把它们从表里一并删掉即可。\n"
            "留着会让这张表从「当前真相」退化成快照 —— 子集语义不防过期，"
            "清干净了它同样不红（CLAUDE.md：我怕它变大，还是也怕它变小？）。")
