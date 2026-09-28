"""卖权行权价选择器 · 集成守卫（v0.45.333，SPEC I.7 integration）。

四层模块（levels → candidates → ledger → report）自己的正确性在各自的测试文件里；
本文件只管它们**与仓库其余部分的接缝**，五组：

  1. AST 火墙①（向内）：四个新模块不碰评分——源码里不出现评分符号、不写 `swarm_results[...]`、
     不 import 旧 GEX / Greeks 引擎与蜂群蒸馏；层间依赖只向下；两个纯计算层零 I/O。
  2. AST 火墙②（向外）：全仓只有日报钩子与 MCP 两处生产代码 import `sell_strike_*`。
     两条都带「确实扫到了」的正对照与 tmp 树病灶夹具（has-teeth）。
     2b（评审 G3 补）：日报模块里对卖权模块的引用只许在 `_post_scan_notify` 方法体内（放行的是钩子，
     不是评分所在的整个文件）；不 import 也能碰到的两个入口——账本目录 `sell_strike_state` 与
     CBOE 原始合约视图 `fetch_cboe_raw_contracts`——各有一张**双向**的引用者白名单。
  3. 钩子非致命：**真跑** `_post_scan_notify`（兄弟钩子全换桩），卖权这一段任一处抛异常
     ⇒ 方法不崩、有 warning、后面的步骤照跑；0 行告警**按档**响。开 / 关钩子两遍，report 逐字节相同、
     swarm_results 前后深相等。3b：补跑时今天的链不许以旧日期入账（vintage 更新方向 + as_of 原样下传）。
  4. 不上公开网站：日报 `report["markdown_report"]` 里没有卖权小节（真跑钩子、正对照同时
     证明本地报告确实写出来了）；本地报告 / 账本路径不命中 `report_deployer` 的自动提交判定，
     真跑一遍 gh-pages 部署也不会被带上去；账本目录被 .gitignore 忽略、进私有备份清单。
  5. conftest `_isolate_sell_strike_state` 两道防线确实接上：① setup 核对跑到了本条测试；
     ② teardown 的真身指纹比对**有牙**（直接驱动 fixture 生成器，往两处真身位置的替身里写一笔必红）。

每条断言都写了让它变红的变异；实测记录见 CHANGELOG v0.45.333。全部离线：取链 / 财报 / K 线都注入。
"""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import logging
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import scan_timing
import sell_strike_ledger as LG
import sell_strike_report as R
from tests._repo_files import own_python_files
from tests.test_sell_strike_ledger import _persist, _raw, _ready_ledger_rows

REPO = Path(__file__).resolve().parent.parent        # 指向**代码**，故用 __file__
DAILY = REPO / "alpha_hive_daily_report.py"
AS_OF = "2026-09-23"

#: 层序：靠后的是上层，只能 import 靠前的（SPEC I 的四层单向依赖）
LAYERS = ("sell_strike_levels", "sell_strike_candidates", "sell_strike_ledger", "sell_strike_report")
NEW_MODULES = tuple(f"{m}.py" for m in LAYERS)


# ═════════════════════════════════════════ AST 小工具（两道火墙共用）

def _imports(tree) -> list:
    """`[(模块点分名, 行号, from-import 的名字元组)]`。含 `importlib.import_module("x")` /
    `__import__("x")` 的字面量动态导入——只查静态 import 的火墙，一行 import_module 就能绕过去。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno, ()) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:          # from . import x
                out += [(a.name, node.lineno, ()) for a in node.names]
            else:
                out.append((node.module or "", node.lineno, tuple(a.name for a in node.names)))
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if (name in ("import_module", "__import__") and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                out.append((node.args[0].value, node.lineno, ()))
    return out


def _doc_string_ids(tree) -> set:
    """裸字符串语句（docstring，以及同形的「字符串当注释」）里那个 Constant 的 id——它们是文档不是代码。
    不排除的话，hive_logger / scan_timing 的 docstring 提一句 `sell_strike_state` 就得进白名单，
    白名单就失去意义。"""
    return {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}


def code_refs(tree, token: str, *, prefix: bool = False, names=frozenset(), skip=frozenset()) -> list:
    """`tree` 里**代码**（docstring 与注释不算）对 `token` 的全部引用 `[(行号, 描述)]`。

    只查 import 的火墙，绕过去只要一行 `PATHS.sell_strike_state` 或 `getattr(cboe_options, "...")`
    （评审变异 M17：蒸馏蜂里直接读账本目录，一个 `sell_strike_*` import 都没有，原火墙全绿）。
    认六种写法：Name / Attribute / Import 与 ImportFrom 的点分段与导入名 / 调用关键字名 /
    字符串字面量（含 f-string 片段——`import_module("x")`、`sys.modules["x"]`、`home / "x"` 都靠它）。
    `prefix=True`：标识符按前缀比（`sell_strike_` 命中 `sell_strike_ledger`）；字符串一律按子串比。
    `names`：额外算命中的裸名（import 别名，如 `_ssl`）。`skip`：不看的节点 id（钩子方法体）。
    """
    docs = _doc_string_ids(tree)
    out = []
    for n in ast.walk(tree):
        if id(n) in skip or id(n) in docs:
            continue
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if token in n.value:
                out.append((n.lineno, f"字符串 {n.value[:60]!r}"))
            continue
        if isinstance(n, ast.Name):
            ids = [n.id]
        elif isinstance(n, ast.Attribute):
            ids = [n.attr]
        elif isinstance(n, ast.Import):
            ids = [p for a in n.names for p in a.name.split(".")]
        elif isinstance(n, ast.ImportFrom):
            ids = (n.module or "").split(".") + [a.name for a in n.names]
        elif isinstance(n, ast.keyword) and n.arg:
            ids = [n.arg]
        else:
            continue
        hit = [i for i in ids if (i.startswith(token) if prefix else i == token)
               or (isinstance(n, ast.Name) and i in names)]
        if hit:
            out.append((n.lineno, f"{type(n).__name__} {hit[0]}"))
    return out


# ═════════════════════════════════════════ 1 · 火墙①：四个新模块不碰评分

#: 评分符号：出现即红（含注释与 docstring——模块作者已刻意不写出这些名字，见 ledger docstring）
FORBIDDEN_TOKENS = ("rule_score", "final_score", "EVALUATION_WEIGHTS", "dimension_weights",
                    "adapted_weights", "queen_distiller", "agent_details")
#: 不得 import 的模块（点分名的任一段命中即算）
FORBIDDEN_IMPORTS = ("advanced_analyzer", "greeks_engine", "gex_regime", "swarm_agents", "queen_distiller")
#: 纯计算层零 I/O：import 白名单（SPEC I.2 / I.3）
PURE_WHITELIST = {
    "sell_strike_levels": {"__future__", "math", "typing", "numpy"},
    "sell_strike_candidates": {"__future__", "math", "typing", "datetime", "sell_strike_levels"},
}


def _is_swarm_results(node) -> bool:
    return ((isinstance(node, ast.Name) and node.id == "swarm_results")
            or (isinstance(node, ast.Attribute) and node.attr == "swarm_results"))


def firewall_in_violations(source: str, module: str) -> list:
    """`module`（不带 .py）的源码违反火墙①的地方；空表 = 干净。"""
    hits = [f"出现评分符号 {tok}" for tok in FORBIDDEN_TOKENS if tok in source]
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_swarm_results(node.value):
            hits.append(f"第 {node.lineno} 行读写 swarm_results[...]")
    me = LAYERS.index(module) if module in LAYERS else None
    for name, ln, names in _imports(tree):
        parts = name.split(".")
        if any(p in FORBIDDEN_IMPORTS for p in parts):
            hits.append(f"第 {ln} 行 import {name}")
        if parts[0] == "config" and not (module == "sell_strike_ledger" and names
                                          and set(names) <= {"WATCHLIST"}):
            hits.append(f"第 {ln} 行 import config（只有 ledger 可 `from config import WATCHLIST`）")
        if me is not None and parts[0] in LAYERS and LAYERS.index(parts[0]) > me:
            hits.append(f"第 {ln} 行向上 import {parts[0]}（依赖只能向下）")
        if module in PURE_WHITELIST and parts[0] not in PURE_WHITELIST[module]:
            hits.append(f"第 {ln} 行 import {name}（纯计算层零 I/O，白名单外）")
    return hits


def _scan_new_modules(root=REPO) -> dict:
    return {p.name: firewall_in_violations(p.read_text(encoding="utf-8"), p.stem)
            for p in sorted(Path(root).glob("sell_strike_*.py"))}


class TestFirewallInward:
    def test_scan_actually_reads_the_four_modules(self):
        """自证：扫描确实扫到四个文件、每个都是实打实的源码，而且探针在真源码上看得见 import。
        变异：glob 写错（`sell_strike*.pyx`）/ 文件改名 ⇒ 下一条对空集恒绿。"""
        scanned = _scan_new_modules()
        assert set(scanned) == set(NEW_MODULES), f"扫到的是 {sorted(scanned)}"
        for name in NEW_MODULES:
            tree = ast.parse((REPO / name).read_text(encoding="utf-8"))
            assert _imports(tree), f"{name} 一条 import 都没扫到——探针坏了，不是「没有违规」"
        # ledger 确实 import 了下两层：层序判定有输入可判
        ledger_imports = {n for n, _l, _ in _imports(ast.parse((REPO / "sell_strike_ledger.py")
                                                                .read_text(encoding="utf-8")))}
        assert {"sell_strike_levels", "sell_strike_candidates"} <= ledger_imports

    def test_new_modules_do_not_touch_scoring(self):
        """变异：任一模块里加 `from gex_regime import RegimeWeightAdjuster` / 读 `final_score` /
        `swarm_results[t]` / levels 里 `import sell_strike_ledger` / levels 里 `import os`。"""
        bad = {k: v for k, v in _scan_new_modules().items() if v}
        assert not bad, f"卖权模块越过了火墙（它只记录与结算，不得进评分、不得读评分）：{bad}"

    @pytest.mark.parametrize("module,inject,why", [
        ("sell_strike_candidates", "x = row['final_score']", "final_score"),
        ("sell_strike_candidates", "# 参考 rule_score 的口径", "rule_score"),
        ("sell_strike_report", "from gex_regime import RegimeWeightAdjuster", "gex_regime"),
        ("sell_strike_report", "import advanced_analyzer as aa", "advanced_analyzer"),
        ("sell_strike_ledger", "from swarm_agents import queen_distiller", "queen_distiller"),
        ("sell_strike_ledger", "import importlib\nimportlib.import_module('greeks_engine')", "greeks_engine"),
        ("sell_strike_ledger", "v = swarm_results['NVDA']", "swarm_results"),
        ("sell_strike_report", "v = self.swarm_results[t]", "swarm_results"),
        ("sell_strike_report", "from config import WATCHLIST", "config"),
        ("sell_strike_ledger", "from config import EVALUATION_WEIGHTS", "EVALUATION_WEIGHTS"),
        ("sell_strike_levels", "import sell_strike_candidates", "向上"),
        ("sell_strike_ledger", "from sell_strike_report import render_markdown", "向上"),
        ("sell_strike_levels", "import os", "零 I/O"),
        ("sell_strike_candidates", "from hive_logger import PATHS", "零 I/O"),
    ])
    def test_has_teeth(self, module, inject, why):
        """每种违规注入进**真源码**都必被抓，且理由对（有牙自证：干净 ≠ 探针瞎了）。"""
        src = (REPO / f"{module}.py").read_text(encoding="utf-8")
        assert not firewall_in_violations(src, module), "基线必须干净，注入才有对照意义"
        hits = firewall_in_violations(src + "\n" + inject + "\n", module)
        assert any(why in h for h in hits), f"注入 {inject!r} 没被抓到（得到 {hits}）"


# ═════════════════════════════════════════ 2 · 火墙②：全仓只有两处生产代码 import 卖权模块

#: 允许 import `sell_strike_*` 的生产文件（仓库根相对路径）
ALLOWED_IMPORTERS = {"alpha_hive_daily_report.py", "alpha_hive_mcp.py"}
#: 离线代码：测试与实验本就要直接用它们
_SKIP_TOP = {"tests", "experiments"}


def _imports_sell_strike(name: str, names: tuple) -> bool:
    return (any(p.startswith("sell_strike_") for p in name.split("."))
            or any(n.startswith("sell_strike_") for n in names))


def sell_strike_importers(root=REPO):
    """`({相对路径: [行号]}, 扫过的文件数, 口径, 解析失败清单)`。枚举口径同
    `test_zero_weight_invariant`（`tests._repo_files.own_python_files`：git 跟踪优先、排除嵌套 worktree）。"""
    root = Path(root)
    files, how = own_python_files(root)
    hits, n, unparsable = {}, 0, []
    for py in files:
        rel = py.relative_to(root)
        if rel.parts[0] in _SKIP_TOP:
            continue
        if len(rel.parts) == 1 and rel.name.startswith("sell_strike_"):
            continue                                   # 四层自己互相 import 是正当的
        n += 1
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            unparsable.append(str(rel))                # 看不了 ≠ 没违规：记下来，下面断言它为空
            continue
        for name, ln, names in _imports(tree):
            if _imports_sell_strike(name, names):
                hits.setdefault(rel.as_posix(), []).append(ln)
    return hits, n, how, unparsable


class TestFirewallOutward:
    def test_only_the_hook_and_mcp_import_sell_strike(self):
        """变异：`report_formatters.py`（或任何评分组件）里加一行 `import sell_strike_ledger`。
        正对照：两个允许的文件**必须**被扫到——证明探针看得见真实的 import，也证明钩子与 MCP 确实接上了。"""
        hits, n, how, unparsable = sell_strike_importers()
        assert n > 100, f"只扫到 {n} 个生产文件（口径 {how}）——枚举坏了"
        assert not unparsable, f"这些生产文件解析失败、火墙看不了：{unparsable}"
        assert ALLOWED_IMPORTERS <= set(hits), (
            f"探针没在 {sorted(ALLOWED_IMPORTERS - set(hits))} 里看到卖权 import——"
            "要么钩子 / MCP 工具被拆了，要么探针瞎了")
        bad = {f: ln for f, ln in hits.items() if f not in ALLOWED_IMPORTERS}
        assert not bad, (
            f"这些生产文件 import 了卖权模块：{bad}。卖权选择器只记录与结算、不进评分——"
            "只允许日报钩子（alpha_hive_daily_report._post_scan_notify）与 MCP 工具读它。")

    def test_has_teeth_on_a_planted_tree(self, tmp_path):
        """tmp 树里种病灶：评分组件三种写法（静态 / from / 动态 import_module）必被抓；
        允许的文件、四层自己、tests/、experiments/、嵌套 worktree 都不算。
        变异：去掉 `_imports` 的动态导入分支 / 把 `_SKIP_TOP` 判断删掉 / 枚举换回裸 rglob。"""
        files = {
            "alpha_hive_daily_report.py": "import sell_strike_ledger as _ssl\n",
            "alpha_hive_mcp.py": "def f():\n    import sell_strike_report\n",
            "sell_strike_new_layer.py": "import sell_strike_ledger\n",
            "report_formatters.py": "import sell_strike_report\n",
            "swarm_agents/queen_distiller.py": "from sell_strike_ledger import load_rows\n",
            "gex_regime.py": "import importlib\nm = importlib.import_module('sell_strike_levels')\n",
            "clean_module.py": "import math\n",
            "tests/test_x.py": "import sell_strike_ledger\n",
            "experiments/probe.py": "import sell_strike_ledger\n",
            ".claude/worktrees/stale/report_formatters.py": "import sell_strike_ledger\n",
        }
        for rel, src in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
        hits, n, how, unparsable = sell_strike_importers(tmp_path)
        bad = sorted(f for f in hits if f not in ALLOWED_IMPORTERS)
        assert bad == ["gex_regime.py", "report_formatters.py", "swarm_agents/queen_distiller.py"], \
            (bad, how)
        assert ALLOWED_IMPORTERS <= set(hits)
        assert n == 6 and not unparsable


# ═════════════════════════════════════════ 2b · 火墙②补三道：放行的文件≠放行整个文件；不 import 也能读账本

#: 日报模块里唯一许碰卖权模块的方法
HOOK = "_post_scan_notify"


def daily_refs_outside_hook(source: str) -> list:
    """日报模块里、`_post_scan_notify` 方法体**之外**对卖权模块的一切引用 `[(行号, 描述)]`。

    火墙②把整个 `alpha_hive_daily_report.py` 放行了，而评分就在这个文件里——评审变异 M3：
    `_build_swarm_report` 里 `import sell_strike_ledger`、给 put=far 的票 final_score −0.5，原火墙全绿。
    放行的是**钩子**，不是钩子所在的文件。别名（`import sell_strike_ledger as _ssl` 的 `_ssl`）
    在方法体外出现也算。
    """
    tree = ast.parse(source)
    hooks = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == HOOK]
    assert len(hooks) == 1, f"日报模块里应恰有一个 {HOOK}，找到 {len(hooks)}——火墙的「内」无从界定"
    inside = frozenset(id(n) for n in ast.walk(hooks[0]))
    aliases = frozenset(
        a.asname for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names
        if a.asname and (a.name.startswith("sell_strike_")
                         or (isinstance(n, ast.ImportFrom) and (n.module or "").startswith("sell_strike_"))))
    return code_refs(tree, "sell_strike_", prefix=True, names=aliases, skip=inside)


#: 在代码里引用账本目录 `sell_strike_state` 的生产文件——**恰好**这几处（双向：多一处、少一处都红）。
#: 定义（hive_logger 的 property）/ 唯一解析点（ledger `_state_dir()`）/ 私有备份清单 / 数据根迁移清单。
#: sell_strike_report 经 ledger 的 `_state_dir()` 取目录，自己不引用。tests/、experiments/ 不扫（同火墙②）。
STATE_DIR_REFERENCERS = {"hive_logger.py", "sell_strike_ledger.py",
                         "data_backup/export.py", "data_backup/migrate_data_root.py"}
#: 调 CBOE 原始合约视图的生产文件——**恰好**这一处（双向）。它是卖权专用的全链视图；
#: 评分链要期权数据走 `fetch_cboe_chain` / GEX 视图，拿到这个视图就等于绕过了火墙②。
RAW_FETCH_CALLERS = {"sell_strike_ledger.py"}


def token_refs(token: str, root=REPO):
    """全仓生产代码里对 `token` 的代码引用：`({相对路径: [(行号, 描述)]}, 扫过的文件数, 口径, 解析失败清单)`。
    枚举口径同火墙②（`own_python_files`；跳过 tests/、experiments/）；四层自己也扫——白名单管它们。"""
    root = Path(root)
    files, how = own_python_files(root)
    hits, n, unparsable = {}, 0, []
    for py in files:
        rel = py.relative_to(root)
        if rel.parts[0] in _SKIP_TOP:
            continue
        n += 1
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            unparsable.append(str(rel))
            continue
        refs = code_refs(tree, token)
        if refs:
            hits[rel.as_posix()] = refs
    return hits, n, how, unparsable


class TestFirewallBeyondImports:
    def test_daily_report_touches_sell_strike_only_inside_the_hook(self):
        """变异 M3：`_build_swarm_report` 里 `import sell_strike_ledger` 并按账本改 final_score。
        正对照：同一探针不设「钩子内」豁免时，确实在钩子里看到了卖权 import（探针没瞎、钩子没拆）。"""
        src = DAILY.read_text(encoding="utf-8")
        bad = daily_refs_outside_hook(src)
        assert not bad, (f"日报模块在 {HOOK} 之外引用了卖权模块：{bad}。评分就在这个文件里——"
                         "卖权只记录与结算，只许日报钩子（本方法体内）碰它。")
        tree = ast.parse(src)
        hook = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == HOOK)
        seen = code_refs(tree, "sell_strike_", prefix=True)
        assert any("Import sell_strike_ledger" in w for _l, w in seen), seen
        assert all(hook.lineno <= ln <= hook.end_lineno for ln, _w in seen), seen

    @pytest.mark.parametrize("inject,why", [
        ("def _mut(self, swarm_results):\n    import sell_strike_ledger\n"
         "    for t, r in swarm_results.items():\n        r['final_score'] -= 0.5\n", "Import sell_strike_ledger"),
        ("from sell_strike_report import rows_for_ticker\n", "ImportFrom sell_strike_report"),
        ("import importlib\n_m = importlib.import_module('sell_strike_ledger')\n", "字符串"),
        ("_m = sys.modules.get('sell_strike_ledger')\n", "字符串"),
        ("def _mut():\n    return _ssl.load_rows('monthly')\n", "Name _ssl"),
        ("def _mut():\n    return PATHS.sell_strike_state\n", "Attribute sell_strike_state"),
    ])
    def test_hook_scope_has_teeth(self, inject, why):
        """每种写法注入到真日报源码（钩子外）都必被抓；只在 docstring 里提到不算（反向对照在下一条）。"""
        src = DAILY.read_text(encoding="utf-8")
        hits = daily_refs_outside_hook(src + "\n" + inject)
        assert any(why in w for _l, w in hits), f"注入 {inject!r} 没被抓到（得到 {hits}）"

    def test_hook_scope_ignores_docstrings(self):
        """反向对照：docstring 里写 `sell_strike_ledger` 是文档不是引用——误报会逼人把整个文件加回豁免。"""
        src = DAILY.read_text(encoding="utf-8")
        doc = '\ndef _doc():\n    """见 sell_strike_ledger.run_for_date"""\n'
        assert daily_refs_outside_hook(src + doc) == daily_refs_outside_hook(src)

    def test_ledger_dir_and_raw_fetch_have_exact_referencers(self):
        """变异 M17：`swarm_agents/queen_distiller.py` 里一个函数经 `PATHS.sell_strike_state` 读账本（零 import）；
        评分组件调 `cboe_options.fetch_cboe_raw_contracts`。两张白名单双向：多出来的是越界，
        少了的是被拆 / 改道（例如 ledger 不再经 PATHS 解析目录）——两种都要有人看一眼。"""
        for token, allowed in (("sell_strike_state", STATE_DIR_REFERENCERS),
                               ("fetch_cboe_raw_contracts", RAW_FETCH_CALLERS)):
            hits, n, how, unparsable = token_refs(token)
            assert n > 100, f"只扫到 {n} 个生产文件（口径 {how}）——枚举坏了"
            assert not unparsable, f"这些生产文件解析失败、火墙看不了：{unparsable}"
            extra = {f: v for f, v in hits.items() if f not in allowed}
            assert not extra, (f"这些生产文件在代码里引用了 {token}：{extra}。卖权账本 / 原始链只许卖权模块"
                               "与目录的定义 / 备份 / 迁移碰——评分链读它就是绕过火墙②。")
            assert set(hits) == allowed, (f"{token} 的引用者少了 {sorted(allowed - set(hits))}——"
                                          "被拆了还是改道了？改道就更新白名单并说明理由")

    def test_token_scan_has_teeth_on_a_planted_tree(self, tmp_path):
        """tmp 树种病灶：属性 / 字符串拼路径 / from-import 别名 / getattr 字符串 / 直接调用都抓；
        docstring 提及、函数定义本身、tests/、experiments/、嵌套 worktree 都不算。
        变异：去掉 Constant 分支（字符串拼路径漏抓）/ 不排除 docstring（cboe_options 与 scan_timing 误报）。"""
        files = {
            "hive_logger.py": ("class P:\n    @property\n    def sell_strike_state(self):\n"
                               "        return self.home / 'sell_strike_state'\n"),
            "sell_strike_ledger.py": ("import cboe_options\nfrom hive_logger import PATHS\n"
                                      "def _state_dir():\n    return PATHS.sell_strike_state\n"
                                      "def _default_fetch(t, *, as_of):\n"
                                      "    return cboe_options.fetch_cboe_raw_contracts(t, as_of=as_of)\n"),
            "data_backup/export.py": "STATE_DIRS = ('vrp_state', 'sell_strike_state')\n",
            "data_backup/migrate_data_root.py": "DIRS = ['sell_strike_state']\n",
            "swarm_agents/queen_distiller.py": ("from hive_logger import PATHS\ndef _peek():\n"
                                                "    return sorted((PATHS.sell_strike_state / 'monthly').glob('*'))\n"),
            "report_formatters.py": "import os\np = os.path.join(HOME, 'sell_strike_state', 'monthly')\n",
            "gex_regime.py": "from cboe_options import fetch_cboe_raw_contracts as _raw\n",
            "advanced_analyzer.py": "import cboe_options\nf = getattr(cboe_options, 'fetch_cboe_raw_contracts')\n",
            "oracle_bee.py": "import cboe_options\nx = cboe_options.fetch_cboe_raw_contracts('NVDA', as_of=None)\n",
            "cboe_options.py": ('def fetch_cboe_raw_contracts(ticker, *, as_of):\n'
                                '    """账本写在 sell_strike_state 下"""\n    return None, "x"\n'),
            "scan_timing.py": 'def f():\n    """fetch_cboe_raw_contracts 各出口计数"""\n',
            "tests/test_x.py": "PATHS.sell_strike_state\nfetch_cboe_raw_contracts('A', as_of=None)\n",
            "experiments/probe.py": "PATHS.sell_strike_state\n",
            ".claude/worktrees/stale/gex_regime.py": "from cboe_options import fetch_cboe_raw_contracts\n",
        }
        for rel, src in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
        state, n, how, unparsable = token_refs("sell_strike_state", tmp_path)
        assert n == 11 and not unparsable, (n, how, unparsable)
        assert sorted(f for f in state if f not in STATE_DIR_REFERENCERS) == [
            "report_formatters.py", "swarm_agents/queen_distiller.py"], state
        assert STATE_DIR_REFERENCERS <= set(state), state
        raw, _n, _how, _u = token_refs("fetch_cboe_raw_contracts", tmp_path)
        assert sorted(f for f in raw if f not in RAW_FETCH_CALLERS) == [
            "advanced_analyzer.py", "gex_regime.py", "oracle_bee.py"], raw
        assert set(raw) >= RAW_FETCH_CALLERS


# ═════════════════════════════════════════ 3/4 · 真跑 `_post_scan_notify`

#: 兄弟钩子的桩渲染出的小节——用来证明 `_extra_md` 回填这条路径在本测试里真的跑到了
_STUB_MD = {"options_paper_leg": "\n## 桩 · 期权纸面腿\n", "vrp_signal": "\n## 桩 · VRP\n",
            "portfolio_greeks": "\n## 桩 · 组合 Greeks\n"}
_EARNINGS = "2026-09-30"        # 在两档到期日（10-07 周度 / 10-23 月度）之前 ⇒ before_expiry


def _swarm():
    """本轮「蜂群结果」：只有 ChronosBee 的催化剂——正是 `_earnings_date_from_swarm` 读的那一处。"""
    def chronos(cats):
        return {"agent_details": {"ChronosBeeHorizon": {"details": {"catalysts": cats}}}}
    return {"AAA": chronos([{"type": "earnings", "date": _EARNINGS}]),
            "BBB": chronos([{"type": "product", "date": "2026-10-01"}])}


def _fetch(t, *, as_of):
    if t == "BBB":
        return None, "stale_vintage"
    return _raw(t, as_of), None


class _BoomWatcher:
    """财报兜底 / ledger 缺省财报源都会走到它；它一被构造就说明钩子没用本轮蜂群的财报日。"""
    built = 0

    def __init__(self):
        type(self).built += 1
        raise RuntimeError("测试里不许打 yfinance 取财报日")


@pytest.fixture
def notify(monkeypatch, tmp_path):
    """可真跑 `_post_scan_notify` 的环境：兄弟钩子全换桩（它们各有自己的测试），卖权这一段用真模块，
    只把取链换成合成链。返回 `run(**overrides) -> (report, reporter)`。

    ⚠️ 以后往 `_post_scan_notify` 里加新钩子：若它离线下会出网 / 需要重依赖，在这里给它补一个桩。
    """
    import alpha_hive_daily_report as ahdr

    stubs = {
        "paper_portfolio": {"run_for_date": lambda *a, **k: {}},
        "earnings_vol_signal": {"scan": lambda *a, **k: [], "settle_signals": lambda *a, **k: 0},
        "options_paper_leg": {"run_for_date": lambda *a, **k: {},
                              "render_markdown": lambda *a, **k: _STUB_MD["options_paper_leg"]},
        "vrp_signal": {"record_day": lambda *a, **k: [], "settle": lambda *a, **k: 0,
                       "render_markdown": lambda *a, **k: _STUB_MD["vrp_signal"]},
        "portfolio_greeks": {"run_for_date": lambda *a, **k: {},
                             "render_markdown": lambda *a, **k: _STUB_MD["portfolio_greeks"]},
        "outcomes_fetcher": {"OutcomesFetcher": lambda **k: types.SimpleNamespace(run=lambda: None)},
        "earnings_watcher": {"EarningsWatcher": _BoomWatcher},
    }
    for name, attrs in stubs.items():
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attrs))
    monkeypatch.setattr(_BoomWatcher, "built", 0)
    monkeypatch.setattr(LG, "_default_fetch", _fetch)
    monkeypatch.setattr(scan_timing, "_phases", {})      # 进程级计时表：换一张，测完还原

    def run(swarm=None, report=None):
        r = ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)
        r.date_str = AS_OF
        r.report_dir = tmp_path
        r.memory_store = r._session_id = r.vector_memory = r.slack_notifier = None
        r._submit_bg = lambda *a, **k: None
        if report is None:
            report = {"markdown_report": "# 日报正文\n", "opportunities": []}
        r._post_scan_notify(types.SimpleNamespace(board=None, targets=[]),
                            _swarm() if swarm is None else swarm, report, 1.0)
        return report, r
    return run


#: 真 `_default_fetch`（模块导入时抓住）：`notify` 夹具把它换成了 `_fetch`，要走真取数链的测试换回来
_REAL_DEFAULT_FETCH = LG._default_fetch


def _scored_swarm():
    """带评分字段的蜂群结果：「钩子写回 final_score」这类变异要有东西可改才看得出来。
    ScoutBee 带价 ⇒ 反馈循环快照不去打 yfinance 取入场价（快照写在 tmp 的 report_dir 下）。"""
    sw = _swarm()
    for i, t in enumerate(sorted(sw)):
        sw[t].update(final_score=6.5 - i, rule_score=6.1 - i, direction="bullish", supporting_agents=4,
                     dimension_scores={"signal": 5.0, "catalyst": 6.0, "sentiment": 6.5,
                                       "odds": 5.5, "risk_adj": 5.0})
        sw[t]["agent_details"]["ScoutBeeNova"] = {"score": 6.0, "details": {"price": 100.0 + i}}
    return sw


def _scored_report():
    return {"markdown_report": "# 日报正文\n\n| AAA | 6.5 |\n| BBB | 5.5 |\n",
            "opportunities": [{"ticker": "AAA", "opp_score": 6.5, "direction": "bullish"},
                              {"ticker": "BBB", "opp_score": 5.5, "direction": "bullish"}]}


def _messages(caplog, level):
    return [rec.getMessage() for rec in caplog.records if rec.levelno == level]


class TestHookRecordsWithoutTouchingDailyMarkdown:
    def test_daily_markdown_has_no_sell_strike_section(self, notify, caplog):
        """日报 md 会进 gh-pages 与自动提交白名单；卖权小节只许出现在状态目录的本地报告里。
        正对照（同一条测试里）：本地报告确实写出来了、里面确实有卖权内容 ⇒ md 里「没有」不是因为钩子没跑。
        变异：钩子里 `report["markdown_report"] += _ss_path.read_text()` / 把卖权渲染加进 `_extra_md`。"""
        with caplog.at_level(logging.INFO):
            report, _r = notify()
        md = report["markdown_report"]
        assert md.startswith("# 日报正文\n")
        for stub in _STUB_MD.values():
            assert stub in md, "兄弟小节没回填——本测试没跑到 `_extra_md` 那条路径，下面的「没有」不作数"
        for marker in ("卖权", "sell-strike", "sell_strike", R.DISCLAIMER):
            assert marker not in md, f"日报 markdown 里出现了卖权内容（{marker!r}），它会被部署到公开网站"

        local = R.report_path(AS_OF)
        assert local.is_file(), "本地报告没写出来"
        text = local.read_text(encoding="utf-8")
        assert "卖权行权价候选" in text and "### AAA" in text
        assert any("卖权行权价账本已更新" in m for m in _messages(caplog, logging.INFO))

    def test_hook_on_off_leaves_report_and_swarm_identical(self, notify, monkeypatch, caplog):
        """上一条只 grep 几个标记词——评审变异 M2（往 `_extra_md` 里拼 `"\\n## Short premium ladder\\n"
        + str(per_tenor)`，一个标记词都不含）与 M1（钩子里 `swarm_results[t]["final_score"] = -1.0`）都活了下来。
        这里不猜泄漏长什么样：同一输入跑两遍，一遍关掉卖权钩子（`sell_strike_ledger` import 即失败，
        整段只剩一条 warning），一遍照常——整个 report（含 opportunities）必须逐字节相同；
        照常那遍前后 `swarm_results` 深相等。兄弟钩子的桩都是常量，两遍的差只可能来自卖权钩子。
        正对照：开的那遍本地报告确实写出来了、关的那遍确实走了失败分支、兄弟小节两遍都回填了。"""
        sw_off, sw_on = _scored_swarm(), _scored_swarm()
        before = copy.deepcopy(sw_on)
        with monkeypatch.context() as m:
            m.setitem(sys.modules, "sell_strike_ledger", None)
            with caplog.at_level(logging.INFO):
                rep_off, _r = notify(swarm=sw_off, report=_scored_report())
        assert any("卖权行权价账本更新失败(非致命)" in w for w in _messages(caplog, logging.WARNING)), \
            "关掉的那遍没走失败分支——对照不成立"
        assert not R.report_path(AS_OF).exists(), "关掉的那遍写了本地报告——对照不成立"
        caplog.clear()
        with caplog.at_level(logging.INFO):
            rep_on, _r = notify(swarm=sw_on, report=_scored_report())
        assert any("卖权行权价账本已更新" in m for m in _messages(caplog, logging.INFO)), "开的那遍钩子没跑"
        assert R.report_path(AS_OF).is_file(), "开的那遍没写本地报告——钩子没跑到底"

        assert sw_on == before, "卖权钩子改了 swarm_results——它只记录与结算，不许写回评分"
        assert rep_on["markdown_report"].encode("utf-8") == rep_off["markdown_report"].encode("utf-8"), (
            "开 / 关卖权钩子，日报 markdown 不一样——它会被部署到公开网站。多出来的是："
            f"{rep_on['markdown_report'][len(rep_off['markdown_report']):]!r}")
        assert rep_on == rep_off, "卖权钩子改了 report 的其它字段（opportunities 等）"
        for stub in _STUB_MD.values():
            assert stub in rep_on["markdown_report"], "兄弟小节没回填——`_extra_md` 那条路径没跑到，比较不作数"

    def test_hook_passes_scan_tickers_and_swarm_earnings(self, notify, caplog):
        """钩子用本轮扫描的票、本轮 ChronosBee 的财报日（不另打 yfinance），日志计数与账本一致。
        变异：`tickers=None`（退回 WATCHLIST）/ 去掉 `upcoming_fn`（退回 EarningsWatcher ⇒ unknown）。"""
        with caplog.at_level(logging.INFO):
            notify()
        for tenor in LG.TENORS:
            rows = {r["ticker"]: r for r in LG.rows_for_date(AS_OF, tenor)}
            assert set(rows) == {"AAA", "BBB"}, f"{tenor}：记了 {sorted(rows)}"
            assert rows["AAA"]["status"] == "recorded"
            assert (rows["AAA"]["earnings_date"], rows["AAA"]["earnings_status"]) == (_EARNINGS,
                                                                                     "before_expiry")
            assert rows["BBB"]["unavailable_reason"] == "stale_vintage"
        assert _BoomWatcher.built == 0, "钩子打了 EarningsWatcher——没用本轮蜂群的财报日"
        info = [m for m in _messages(caplog, logging.INFO) if "卖权行权价账本已更新" in m]
        assert len(info) == 1
        assert "月度 记录 1 / 不可得 1 · 周度 记录 1 / 不可得 1 · 结算 0" in info[0], info[0]
        # 放弃 / 取不到 K 线 / 待结算也要打（F2-7）：K 线源坏掉时「结算 0」与「没有行到期」要分得开。
        # 两档各记了 AAA 一行（未到期）⇒ 待结算 2。变异：日志只打 settled。
        assert "放弃 0 · 取不到 K 线 0 · 待结算 2（其中过期 0）" in info[0], info[0]
        # R6：报价来源按档打出来（cboe_stale_intraday 的行照记、不进检验，不数就没人知道样本在变少）。
        # 变异：日志不带 per_tenor 的 price_source。
        assert "报价来源 月度 {'cboe_close': 1} / 周度 {'cboe_close': 1}" in info[0], info[0]
        assert "sell_strike" in scan_timing.phases(), "耗时没进 scan_timing（→ status.json）"

    def test_hook_block_structure(self):
        """结构兜底（行为测试之外再钉一道，防行为夹具日后被桩改瞎）：卖权两段 try——账本一段（唯一 import
        卖权模块的那段）在前、本地报告一段在后（F2-7：报告失败不得吞掉账本日志与 0 行告警）；
        每段都 `except Exception` 兜住、都不碰 `report`；整个日报模块不调用卖权的 `render_markdown`。
        变异：同上两条 + 把 `except Exception` 收窄成 `except ImportError` / 报告写回账本那段里。"""
        tree = ast.parse(DAILY.read_text(encoding="utf-8"))
        notify_fn = next(n for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name == "_post_scan_notify")

        def _imports_ledger(t):
            return any(isinstance(s, ast.Import) and any(a.name == "sell_strike_ledger" for a in s.names)
                       for s in t.body)

        def _uses_sell_strike(t):
            return any(isinstance(n, ast.Name) and n.id in ("_ssl", "_ssr") for s in t.body for n in ast.walk(s))

        tries = [t for t in ast.walk(notify_fn) if isinstance(t, ast.Try)]
        ledger_blocks = [t for t in tries if _imports_ledger(t)]
        assert len(ledger_blocks) == 1, f"_post_scan_notify 里应恰有一段 import 卖权账本的 try，找到 {len(ledger_blocks)}"
        blocks = sorted((t for t in tries if _imports_ledger(t) or _uses_sell_strike(t)), key=lambda t: t.lineno)
        assert len(blocks) == 2 and blocks[0] is ledger_blocks[0], \
            f"应是「账本 try 在前、本地报告 try 在后」两段，找到 {[b.lineno for b in blocks]}"
        report_calls = [[n for s in b.body for n in ast.walk(s)
                         if isinstance(n, ast.Attribute) and n.attr == "write_local_report"] for b in blocks]
        assert not report_calls[0] and report_calls[1], \
            "write_local_report 必须在账本那段 try 之外、单独一段——同段且在前时报告一抛错，账本日志与 0 行告警全被吞"
        for blk in blocks:
            assert any(h.type is None or (isinstance(h.type, ast.Name) and h.type.id == "Exception")
                       for h in blk.handlers), "卖权钩子必须 `except Exception` 兜住——它失败不能拖死日报"
            touched = sorted({n.lineno for n in ast.walk(blk) if isinstance(n, ast.Name) and n.id == "report"})
            assert not touched, f"卖权那段 try 碰了 report（第 {touched} 行）——日报 md 会被部署到公开网站"

        aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                   for a in n.names if a.name == "sell_strike_report"}
        assert aliases, "日报模块里没找到 import sell_strike_report——钩子被拆了？"
        rendered = [n.lineno for n in ast.walk(tree)
                    if (isinstance(n, ast.Attribute) and n.attr == "render_markdown"
                        and isinstance(n.value, ast.Name) and n.value.id in aliases)
                    or (isinstance(n, ast.ImportFrom) and n.module == "sell_strike_report")]
        assert not rendered, f"日报模块渲染了卖权 markdown（第 {rendered} 行）"


class TestHookIsTheOnlyFreezeWriter:
    """R7（2026-09-24 最终评审）：预注册检验的冻结是协议里唯一一次不可逆的写，只许日报钩子写。"""

    def test_hook_freezes_a_ready_ledger(self, notify, caplog):
        """就绪账本：钩子之前 MCP 读一次不冻结（等待日报冻结）；跑一次钩子 ⇒ 冻结文件出现（ready_date =
        当天数据视界）、本地报告写出冻结判定。变异：钩子里 `write_local_report` 不传 freeze=True。"""
        _persist(_ready_ledger_rows(), None)
        path = LG.prereg_result_path("monthly")
        assert R.rows_for_ticker("2026-01-01", "T000")["assess"]["monthly"]["awaiting_freeze"] is True
        assert not path.exists(), "钩子之前就冻结了——正对照不成立"
        with caplog.at_level(logging.INFO):
            notify()
        assert path.is_file(), "日报钩子没有冻结就绪的检验"
        assert json.loads(path.read_text(encoding="utf-8"))["ready_date"] == AS_OF
        assert f"预注册检验已于 {AS_OF} 冻结" in R.report_path(AS_OF).read_text(encoding="utf-8")

    def test_exactly_one_production_call_passes_freeze_true(self):
        """结构兜底：全部允许 import 卖权模块的生产文件里，字面量 `freeze=True` 只出现在日报钩子的
        `write_local_report` 调用上一处（MCP 一处都没有）。变异：MCP 路径传 freeze=True / 钩子不传。"""
        hits = []
        for rel in sorted(ALLOWED_IMPORTERS):
            for n in ast.walk(ast.parse((REPO / rel).read_text(encoding="utf-8"))):
                if isinstance(n, ast.Call) and any(k.arg == "freeze" and isinstance(k.value, ast.Constant)
                                                   and k.value.value is True for k in n.keywords):
                    hits.append((rel, getattr(n.func, "attr", getattr(n.func, "id", "?"))))
        assert hits == [("alpha_hive_daily_report.py", "write_local_report")], hits


class TestHookIsNonFatal:
    @pytest.mark.parametrize("where,needle", [
        ("import", "sell_strike_ledger"),
        ("run_for_date", "boom-run"),
    ])
    def test_failure_is_logged_and_the_rest_still_runs(self, notify, monkeypatch, caplog, where, needle):
        """卖权账本那段在两个位置各抛一次：`_post_scan_notify` 不崩、打 warning（带原因）、
        不去写本地报告（账本没更新，报告只会是旧的）、后面的 `_extra_md` 回填照跑、耗时照记。
        变异：`except` 里改成 `raise` / 删掉整段 try。"""
        if where == "import":
            monkeypatch.setitem(sys.modules, "sell_strike_ledger", None)   # import 即 ImportError
        else:
            monkeypatch.setattr(LG, "run_for_date",
                                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom-run")))
        with caplog.at_level(logging.INFO):
            report, _r = notify()
        warns = [m for m in _messages(caplog, logging.WARNING) if "卖权行权价账本更新失败(非致命)" in m]
        assert len(warns) == 1 and needle in warns[0], _messages(caplog, logging.WARNING)
        assert not any("卖权行权价账本已更新" in m for m in _messages(caplog, logging.INFO))
        assert not R.report_path(AS_OF).exists(), "账本没更新却写了本地报告"
        assert report["markdown_report"].endswith(_STUB_MD["portfolio_greeks"]), \
            "卖权失败后，后面的小节回填没跑到——失败连坐了日报"
        assert "sell_strike" in scan_timing.phases(), "失败的那段耗时也要记（别让失败的阶段消失）"

    def test_report_write_failure_does_not_swallow_ledger_logs_or_zero_row_alarm(self, notify, monkeypatch,
                                                                                 caplog):
        """评审探针原样：CBOE 全线不可得（0 行）**且**本地报告渲染抛错。账本其实已写好——
        状态日志与 0 行告警都必须照打，报告失败单独一条 warning，不许说成「账本更新失败」。
        变异：`write_local_report` 放回账本那段 try、排在日志之前。"""
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (None, "stale_vintage"))
        monkeypatch.setattr(R, "write_local_report",
                            lambda *a, **k: (_ for _ in ()).throw(TypeError("render bug")))
        with caplog.at_level(logging.INFO):
            report, _r = notify()
        warns = _messages(caplog, logging.WARNING)
        assert any("0 行可记" in m and "stale_vintage" in m for m in warns), warns
        rep = [m for m in warns if "本地报告写入失败" in m]
        assert len(rep) == 1 and "render bug" in rep[0] and "账本已更新" in rep[0], warns
        assert not any("卖权行权价账本更新失败" in m for m in warns), "账本写好了，不许报成账本失败"
        assert any("卖权行权价账本已更新" in m for m in _messages(caplog, logging.INFO))
        assert {r["unavailable_reason"] for r in LG.rows_for_date(AS_OF, "monthly")} == {"stale_vintage"}
        assert report["markdown_report"].endswith(_STUB_MD["portfolio_greeks"])
        assert "sell_strike" in scan_timing.phases()

    def test_partial_failure_inside_run_for_date_is_loud(self, notify, monkeypatch, caplog):
        """`run_for_date` 把一档的结算异常收进 per_tenor.errors、不往外抛——钩子必须把它打成 warning。
        变异：删掉钩子里的 `_ss_errors` 那段。"""
        real = LG._settle

        def flaky(as_of, tenor, **kw):
            if tenor == "weekly":
                raise RuntimeError("weekly settle broke")
            return real(as_of, tenor, **kw)
        monkeypatch.setattr(LG, "_settle", flaky)
        with caplog.at_level(logging.INFO):
            notify()
        warns = [m for m in _messages(caplog, logging.WARNING) if "卖权行权价账本部分失败" in m]
        assert len(warns) == 1 and "weekly settle broke" in warns[0]
        assert any("卖权行权价账本已更新" in m for m in _messages(caplog, logging.INFO)), "月度照常记录"

    def test_zero_recorded_rows_is_loud(self, notify, monkeypatch, caplog):
        """整轮 0 行可记（CBOE 全线不可得 / 快照模式）⇒ 每档一条 warning，各带该档的原因计数。
        变异：删掉那段 warning。"""
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (None, "snapshot_mode_no_raw_chain"))
        with caplog.at_level(logging.INFO):
            notify()
        warns = [m for m in _messages(caplog, logging.WARNING) if "0 行可记" in m]
        assert len(warns) == 2, _messages(caplog, logging.WARNING)
        assert all("snapshot_mode_no_raw_chain" in w for w in warns), warns
        assert sorted("月度（monthly）" in w for w in warns) == [False, True], "两档各一条"

    def test_zero_row_alarm_reports_row_reasons_not_fetch_reasons(self, notify, monkeypatch, caplog):
        """取数成功但窗口里没有到期日（只挂着 60 DTE 的链）⇒ 两档 0 行。告警给的必须是**行上**的原因
        （no_expiry_in_window），不是取数那一步的 `{'ok': 1}`——后者在「取数好、后面坏」时自相矛盾。
        变异：0 行告警改回打印 `fetch_reasons`。"""
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (
            (None, "stale_vintage") if t == "BBB" else (_raw(t, as_of, dtes=(60,)), None)))
        with caplog.at_level(logging.INFO):
            notify()
        warns = [m for m in _messages(caplog, logging.WARNING) if "0 行可记" in m]
        assert len(warns) == 2, _messages(caplog, logging.WARNING)
        for w in warns:
            assert "no_expiry_in_window" in w and "stale_vintage" in w and "'ok'" not in w, w
        assert {"月度（monthly）" in warns[0], "周度（weekly）" in warns[0]} == {True, False}, "按档给原因"
        assert ("月度（monthly）" in warns[0]) != ("月度（monthly）" in warns[1]), warns

    @pytest.mark.parametrize("dtes,empty,full", [((14,), "monthly", "weekly"), ((30,), "weekly", "monthly")])
    def test_zero_row_alarm_is_per_tenor(self, notify, monkeypatch, caplog, dtes, empty, full):
        """评审 G3：一档对**全部**票 0 行、另一档照常 ⇒ 空的那一档必须响（档名 + 该档行上的原因），
        照常的那档不响。原判据「两档都 0 行才响」下这里只有一条 INFO——一档账本天天断供没人知道。
        变异：回到 `not any(_v.get("recorded") for _v in per_tenor.values())`。"""
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (
            (None, "stale_vintage") if t == "BBB" else (_raw(t, as_of, dtes=dtes), None)))
        with caplog.at_level(logging.INFO):
            notify()
        label = {"monthly": "月度", "weekly": "周度"}
        warns = [m for m in _messages(caplog, logging.WARNING) if "0 行可记" in m]
        assert len(warns) == 1, _messages(caplog, logging.WARNING)
        assert f"{label[empty]}（{empty}）" in warns[0], warns[0]
        assert full not in warns[0] and label[full] not in warns[0], warns[0]
        assert "no_expiry_in_window" in warns[0] and "stale_vintage" in warns[0], warns[0]
        # 正对照：另一档确实记上了（「只有一条」不是因为另一档也没跑）
        assert [r["ticker"] for r in LG.rows_for_date(AS_OF, full) if r["status"] == "recorded"] == ["AAA"]
        assert not [r for r in LG.rows_for_date(AS_OF, empty) if r["status"] == "recorded"]

    def test_tenor_missing_from_run_result_is_loud(self, notify, monkeypatch, caplog):
        """`run_for_date` 的返回里漏了一档（重构丢键）也按 0 行响，而不是只看返回里有的档。
        变异：按 `per_tenor` 的键迭代、不并上 `TENORS`。"""
        monkeypatch.setattr(LG, "run_for_date", lambda *a, **k: {
            "per_tenor": {"monthly": {"recorded": 2, "unavailable": {}, "errors": []}}})
        with caplog.at_level(logging.INFO):
            notify()
        warns = [m for m in _messages(caplog, logging.WARNING) if "0 行可记" in m]
        assert len(warns) == 1 and "周度（weekly）" in warns[0] and "未返回该档" in warns[0], \
            _messages(caplog, logging.WARNING)


# ═════════════════════════════════════════ 3b · 补跑的 vintage 保护：今天的链不许写到旧日期下
#
# 开机补跑 / CLI 带旧日期时，CBOE 给的是**今天**（更新）的链。原有的 mismatch 用例只喂了**更旧**的
# payload ⇒ 评审变异 M13（`vintage != as_of` → `vintage < as_of`）活了下来——恰好放行真正的风险方向。
# M19（`_default_fetch` 传 `as_of=None`）也活着：钩子测试全都把 `_default_fetch` 换成了桩。

_NEWER = "2026-09-24"                                 # AS_OF（09-23）的下一交易日：补跑发生的那天


@pytest.fixture
def cboe_clean(monkeypatch):
    """真 `fetch_cboe_raw_contracts` 的前置：关快照模式，进程级计数前后清零（同 test_sell_strike_candidates）。"""
    import cboe_options as C
    monkeypatch.setattr(C, "_SNAPSHOT_PROVIDER", None)
    C.reset_raw_contracts_stats()
    C.reset_payload_stats()
    yield C
    C.reset_raw_contracts_stats()
    C.reset_payload_stats()


def _serve_newer(monkeypatch, C):
    """`_fetch_cboe_payload` → 一份 vintage = _NEWER 的 payload（09-24 收盘后抓到的链），不出网。"""
    from tests.test_sell_strike_candidates import _payload, _row
    rows = [_row(exp, cp, float(K), delta=(d if cp == "C" else d - 1.0))
            for exp in ("2026-10-09", "2026-10-23") for K, d in zip(range(80, 125, 5),
                                                                      (0.95, 0.9, 0.8, 0.65, 0.5, 0.35, 0.2, 0.1, 0.05))
            for cp in "CP"]
    payload = _payload(rows, last_trade=f"{_NEWER}T16:00:00")
    monkeypatch.setattr(C, "_fetch_cboe_payload", lambda *a, **k: payload)


class TestCatchUpVintageProtection:
    def test_newer_payload_for_older_as_of_is_refused(self, monkeypatch, cboe_clean):
        """09-24 收盘后补跑 as_of=09-23：payload 是 09-24 的 ⇒ `(None, "vintage_mismatch")`，计数 +1。
        正对照：同一 payload、as_of=09-24 ⇒ 取得到——红只能来自 vintage 判据，不是夹具坏了。
        变异 M13：`vintage != as_of` → `vintage < as_of`（只挡更旧的，放行更新的）。"""
        C = cboe_clean
        _serve_newer(monkeypatch, C)
        now = datetime(2026, 9, 24, 17, 5, tzinfo=ZoneInfo("America/New_York"))   # 补跑：09-24 收盘后
        ok, why = C.fetch_cboe_raw_contracts("XYZ", as_of=_NEWER, now_et=now)
        assert why is None and ok["vintage_date"] == ok["as_of"] == _NEWER and ok["contracts"], why
        assert C.raw_contracts_stats()["vintage_mismatch"] == 0
        res, why = C.fetch_cboe_raw_contracts("XYZ", as_of=AS_OF, now_et=now)
        assert (res, why) == (None, "vintage_mismatch"), (
            f"{_NEWER} 的链被当成 {AS_OF} 的收下了：{why}"
            + (f"，as_of={res.get('as_of')}" if res else ""))
        assert C.raw_contracts_stats()["vintage_mismatch"] == 1

    def test_default_fetch_forwards_as_of_unchanged(self, monkeypatch):
        """`_default_fetch` 把 as_of 原样交给 `fetch_cboe_raw_contracts`（日报钩子的日期 / MCP 现算的 None）。
        变异 M19：传 `as_of=None`（vintage 闸整个失效，补跑把今天的链写到旧日期下）。"""
        import cboe_options
        seen = []

        def fake(ticker, **kw):
            seen.append((ticker, kw.get("as_of", "<缺>")))
            return None, "stub"
        monkeypatch.setattr(cboe_options, "fetch_cboe_raw_contracts", fake)
        assert _REAL_DEFAULT_FETCH("AAA", as_of=AS_OF) == (None, "stub")
        assert _REAL_DEFAULT_FETCH("BBB", as_of=None) == (None, "stub")
        assert seen == [("AAA", AS_OF), ("BBB", None)], seen

    def test_hook_catch_up_with_todays_chain_records_nothing(self, notify, monkeypatch, cboe_clean, caplog):
        """端到端：日报钩子（date_str = 09-23）→ 真 `run_for_date` → 真 `_default_fetch` → 真
        `fetch_cboe_raw_contracts`，CBOE 给的是 09-24 的链 ⇒ 两档一行都不记、原因全是 vintage_mismatch，
        两档各响一条 0 行告警。M13、M19 任一个都会让 09-24 的链以 09-23 的名义落进账本。"""
        _serve_newer(monkeypatch, cboe_clean)
        monkeypatch.setattr(LG, "_default_fetch", _REAL_DEFAULT_FETCH)
        with caplog.at_level(logging.INFO):
            notify()
        for tenor in LG.TENORS:
            rows = {r["ticker"]: r for r in LG.rows_for_date(AS_OF, tenor)}
            got = {t: (r["status"], r.get("unavailable_reason")) for t, r in rows.items()}
            assert set(got) == {"AAA", "BBB"}, got
            assert not [t for t, (st, _why) in got.items() if st == "recorded"], \
                f"{tenor}：{_NEWER} 的链以 {AS_OF} 的名义记进了账本：{got}"
            # 第一只一定真取了数；后面的票允许被取数熔断之类的保护跳过，只要没记
            assert got["AAA"] == ("unavailable", "vintage_mismatch"), got
        warns = [m for m in _messages(caplog, logging.WARNING) if "0 行可记" in m]
        assert len(warns) == 2 and all("vintage_mismatch" in w for w in warns), warns


# ═════════════════════════════════════════ 4 · 不上公开网站：部署 / 自动提交 / git / 备份

def _rel_home(p: Path) -> str:
    from hive_logger import PATHS
    return Path(p).relative_to(PATHS.home).as_posix()


class TestNotPublished:
    def _write_state(self):
        LG.run_for_date(AS_OF, tickers=["AAA"], fetch_fn=_fetch, upcoming_fn=lambda t: None,
                        bars_fn=lambda t: [])
        return R.write_local_report(AS_OF)

    def test_auto_commit_whitelist_does_not_match(self):
        """路径由**真函数**算出（改了文件名 / 目录名这条跟着变）；正对照：日报 md 与期权三本账确实命中。
        变异：把 `sell_strike_state/` 加进 REPORT_ARTIFACT_PATHS / _ARTIFACT_PREFIXES；
        报告改名成 `alpha-hive-daily-sell-strike-*.md`。"""
        import report_deployer as rd
        local = self._write_state()
        rels = [_rel_home(local), _rel_home(LG._shard("monthly", AS_OF)),
                _rel_home(LG._shard("weekly", AS_OF)), local.name]
        assert rd._is_report_artifact("alpha-hive-daily-2026-09-23.md")
        assert rd._is_report_artifact("vrp_state/vrp_signals.jsonl")
        hit = [p for p in rels if rd._is_report_artifact(p)]
        assert not hit, f"卖权产物命中了自动提交白名单（会被推进代码仓库）：{hit}"
        assert not any(e.rstrip("/") == "sell_strike_state" for e in rd.REPORT_ARTIFACT_PATHS)

    def test_real_ghpages_deploy_does_not_carry_it(self, tmp_path, monkeypatch):
        """真跑 `deploy_static_to_ghpages`（本地裸仓库当 origin）：数据根里放了卖权报告——状态目录里一份、
        根目录再放一份同名的（最坏情况：有人把它写到根上）——gh-pages 上一个都不许有。
        正对照：同一次部署里日报 md 确实上去了。变异：部署白名单加 `f.startswith("sell-strike-")`。"""
        from unittest.mock import patch
        from tests.test_ghpages_data_root_migration import _init_repo_with_origin, _make_reporter

        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
        local = self._write_state()
        assert local.is_relative_to(data_root)
        (data_root / local.name).write_text(local.read_text(encoding="utf-8"), encoding="utf-8")
        (data_root / "index.html").write_text("<html>x</html>", encoding="utf-8")
        (data_root / f"alpha-hive-daily-{AS_OF}.md").write_text("# 日报\n", encoding="utf-8")

        repo, bare = _init_repo_with_origin(tmp_path, "coderepo")
        reporter = _make_reporter(monkeypatch, tmp_path, repo)
        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()
        out = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "gh-pages"],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        deployed = out.stdout.split()
        assert f"alpha-hive-daily-{AS_OF}.md" in deployed, f"正对照失败，部署没跑通：{deployed}"
        leaked = [f for f in deployed if "sell-strike" in f or "sell_strike" in f]
        assert not leaked, f"卖权内容被部署到了公开网站：{leaked}"

    def test_state_dir_is_gitignored(self):
        """账本与本地报告被 .gitignore 忽略（否则一次 `git add -A` 就把它推进代码仓库）。
        变异：删掉 .gitignore 里的 `sell_strike_state/`。
        ⚠️ 128（不是 git 仓库）时 skip：「git 仓库在哪些环境里存在」答开发检出 / worktree / CI 全都是，
        只有手工 `git archive` 的树不是（同 test_no_invisible_prod_data_skips 的判据）。"""
        from hive_logger import PATHS
        probes = [_rel_home(R.report_path(AS_OF)), _rel_home(LG._shard("monthly", AS_OF)),
                  _rel_home(LG._shard("weekly", AS_OF))]
        assert all(p.startswith("sell_strike_state/") for p in probes), (probes, PATHS.home)
        for probe in probes:
            out = subprocess.run(["git", "check-ignore", "-q", probe], cwd=REPO, capture_output=True)
            if out.returncode == 128:
                pytest.skip("不在 git 仓库里（git archive 出来的树）——check-ignore 无从判定")
            assert out.returncode in (0, 1), out.stderr.decode(errors="replace")[:200]
            assert out.returncode == 0, f"{probe} 没被 .gitignore 忽略"

    def test_private_backup_covers_ledger_and_report(self, tmp_path):
        """耐久性只靠私有备份：`STATE_DIRS` 里有它，且账本分片与本地报告的后缀都在文本白名单里
        （白名单外的会被 `copy_state_dir` 跳过——那就是「每天写、从不备份」）。
        变异：从 STATE_DIRS 删掉 / 账本改写成白名单外的后缀。"""
        from data_backup import export as export_mod
        from hive_logger import PATHS
        assert "sell_strike_state" in export_mod.STATE_DIRS
        local = self._write_state()
        copied, skipped = export_mod.copy_state_dir(PATHS.home, "sell_strike_state", tmp_path / "out")
        got = {c["rel"] for c in copied}
        assert {_rel_home(local), _rel_home(LG._shard("monthly", AS_OF)),
                _rel_home(LG._shard("weekly", AS_OF))} <= got, got
        assert not skipped, f"备份跳过了卖权账本里的文件：{skipped}"


# ═════════════════════════════════════════ MCP 工具

class TestMcpTool:
    @staticmethod
    def _call(**kw):
        import alpha_hive_mcp as M
        out = asyncio.run(M.alphahive_get_sell_strike_candidates(M.TickerDateInput(**kw)))

        def _no_nan(c):
            raise AssertionError(f"MCP 返回了非法 JSON 常量 {c}")
        return json.loads(out, parse_constant=_no_nan)

    def test_registered_read_only(self):
        """变异：漏写 `@mcp.tool` / 注解写成可写。"""
        import alpha_hive_mcp as M
        tools = {t.name: t for t in asyncio.run(M.mcp.list_tools())}
        t = tools.get("alphahive_get_sell_strike_candidates")
        assert t is not None, sorted(tools)
        assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False

    def test_date_reads_ledger_without_fetching(self, monkeypatch):
        """给 date ⇒ 读账本当日两档该票的行（含结构报价与 assess），**不取链**。
        变异：date 分支也走 compute_live。"""
        LG.run_for_date(AS_OF, tickers=["AAA"], fetch_fn=_fetch, upcoming_fn=lambda t: None,
                        bars_fn=lambda t: [])
        monkeypatch.setattr(LG, "_default_fetch",
                            lambda *a, **k: pytest.fail("给了 date 却去取链了"))
        out = self._call(ticker="aaa", date_str=AS_OF)
        assert out["data_available"] is True and out["source"] == "ledger"
        for tenor in LG.TENORS:
            row = out["tenors"][tenor]
            assert row["status"] == "recorded" and row["route"] and row["ladder"] and row["structures"]
            assert out["assess"][tenor]["status"] in ("accruing", "ready", "undetermined")
        assert out["caveats"] and out["disclaimer"] == R.DISCLAIMER

    def test_live_computes_and_does_not_write_ledger(self, monkeypatch):
        """不给 date ⇒ 现算（as_of = payload vintage），返回 levels 摘要；账本一行都不许多。
        变异：live 分支调 `run_for_date` / `record_rows`。"""
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (_raw(t, AS_OF), None))
        out = self._call(ticker="AAA")
        assert out["data_available"] is True and out["source"] == "live" and out["as_of"] == AS_OF
        assert out["levels"]["views"]["le_45dte"]["zero_gamma"]["curve_state"]
        assert set(out["tenors"]) == set(LG.TENORS)
        assert LG.load_rows("monthly") == [] and LG.load_rows("weekly") == []

    def test_ready_ledger_is_not_frozen_by_the_tool(self, monkeypatch):
        """R7 评审探针 p4 走真 MCP 工具：就绪但未冻结的账本上按日期读、现算各一次，冻结文件都不出现
        （readOnlyHint / 「never writes」属实），assess 显示等待日报冻结。变异：MCP 路径的 assess 走 freeze=True。"""
        _persist(_ready_ledger_rows(), None)
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (_raw(t, AS_OF), None))
        by_date = self._call(ticker="T000", date_str="2026-01-01")
        live = self._call(ticker="T000")
        assert not LG.prereg_result_path("monthly").exists(), "MCP 工具写了冻结文件"
        for out in (by_date, live):
            a = out["assess"]["monthly"]
            assert a["status"] == "ready" and a["awaiting_freeze"] is True and "等待日报冻结" in a["summary"]

    def test_unavailable_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (None, "stale_vintage"))
        out = self._call(ticker="AAA")
        assert out["data_available"] is False and out["reason"] == "stale_vintage"

    def test_yield_definition_reaches_the_client(self, monkeypatch):
        """N2：客户端（LLM）读到的工具描述把 yield_raw 标为主数字、年化标为单利次要；JSON 输出带 yield_note。
        变异：描述回到「annualized yield」一词带过 / 输出漏 yield_note。"""
        import alpha_hive_mcp as M
        desc = next(t for t in asyncio.run(M.mcp.list_tools())
                    if t.name == "alphahive_get_sell_strike_candidates").description
        assert "yield_raw = credit / collateral" in desc
        assert "PRIMARY per-trade return on risk" in desc, "描述没把 yield_raw 标为主数字"
        assert "simple (non-compounded) annualization" in desc, "描述没说年化是单利"
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (_raw(t, AS_OF), None))
        out = self._call(ticker="AAA")
        assert out["yield_note"] == R.YIELD_NOTE
        q = out["tenors"]["monthly"]["structures"]["base"]["short_put"]
        assert q["quotable"] and q["yield_raw"] == round(q["yield_raw"], 6) > 0


# ═════════════════════════════════════════ 5 · conftest 两道防线确实接上

def _isolate_fixture_fn():
    """拿到 conftest 里 `_isolate_sell_strike_state` 的原函数（绕过 pytest 的「不许直接调用」包装）。"""
    conf = next(m for m in list(sys.modules.values())
                if getattr(m, "__file__", None)
                and Path(m.__file__).resolve() == (REPO / "tests" / "conftest.py").resolve())
    fx = conf._isolate_sell_strike_state
    getter = getattr(fx, "_get_wrapped_function", None)       # pytest ≥ 8.4
    return getter() if getter else getattr(fx, "__wrapped__", fx)


class TestConftestDefensesWired:
    def test_autouse_and_setup_check_ran_for_this_test(self, request, tmp_path):
        """防线①：fixture 自动挂在本条测试上，且 setup 核对的正是**本条**的沙箱。
        变异：去掉 autouse / 删掉 setup 核对。"""
        assert "_isolate_sell_strike_state" in request.fixturenames
        want = str(tmp_path / "sell_strike_state")
        assert request.config._alpha_hive_sell_strike_guard_checked == {
            "PATHS.sell_strike_state": want, "sell_strike_ledger._state_dir()": want}

    @pytest.mark.parametrize("arm", ["checkout", "invocation"])
    def test_teardown_fingerprint_has_teeth(self, tmp_path, monkeypatch, arm):
        """防线②：直接驱动 fixture 生成器，真身位置换成 tmp 替身（`hive_logger.__file__` 与
        `invocation_params.dir`，都是 fixture 自己读的输入），往其中一处写一笔 ⇒ teardown 必红；
        什么都不写 / 只写沙箱 ⇒ 不红。变异：`real_dirs` 漏掉某一臂 / 删掉 teardown 断言。"""
        import hive_logger
        fake_root, fake_invoc = tmp_path / "fake_checkout", tmp_path / "fake_invocation"
        fake_root.mkdir()
        fake_invoc.mkdir()
        monkeypatch.setattr(hive_logger, "__file__", str(fake_root / "hive_logger.py"))
        fn = _isolate_fixture_fn()

        def drive(write_to):
            cfg = types.SimpleNamespace(invocation_params=types.SimpleNamespace(dir=fake_invoc))
            gen = fn(_isolate_env=None, request=types.SimpleNamespace(config=cfg), tmp_path=tmp_path)
            next(gen)                                       # setup：防线① 必须放行（PATHS 在 tmp 里）
            assert cfg._alpha_hive_sell_strike_guard_checked
            if write_to is not None:
                d = write_to / "sell_strike_state" / "monthly"
                d.mkdir(parents=True, exist_ok=True)
                (d / "2026-09.jsonl").write_text("{}\n", encoding="utf-8")
            return gen

        # 反向对照：只写沙箱（PATHS 指向的地方）不许误报
        gen = drive(None)
        LG.record_rows(AS_OF, "monthly", [LG.unavailable_row(AS_OF, "AAA", "monthly", "stale_vintage")])
        with pytest.raises(StopIteration):
            next(gen)

        gen = drive(fake_root if arm == "checkout" else fake_invoc)
        with pytest.raises(AssertionError, match="真身"):
            next(gen)

    def test_setup_check_rejects_escaped_state_dir(self, tmp_path, monkeypatch):
        """防线①的判据接在 fixture 上：`ALPHA_HIVE_HOME` 指到沙箱外 ⇒ setup 就红。变异：setup 不再核对。"""
        sandbox, elsewhere = tmp_path / "sandbox", tmp_path / "elsewhere"
        sandbox.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(elsewhere))
        cfg = types.SimpleNamespace(invocation_params=types.SimpleNamespace(dir=tmp_path))
        gen = _isolate_fixture_fn()(_isolate_env=None, request=types.SimpleNamespace(config=cfg),
                                    tmp_path=sandbox)
        with pytest.raises(AssertionError, match="逃出了测试沙箱"):
            next(gen)
