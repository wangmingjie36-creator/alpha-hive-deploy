"""卖权行权价选择器 · 集成守卫（v0.45.333，SPEC I.7 integration）。

四层模块（levels → candidates → ledger → report）自己的正确性在各自的测试文件里；
本文件只管它们**与仓库其余部分的接缝**，五组：

  1. AST 火墙①（向内）：四个新模块不碰评分——源码里不出现评分符号、不写 `swarm_results[...]`、
     不 import 旧 GEX / Greeks 引擎与蜂群蒸馏；层间依赖只向下；两个纯计算层零 I/O。
  2. AST 火墙②（向外）：全仓只有日报钩子与 MCP 两处生产代码 import `sell_strike_*`。
     两条都带「确实扫到了」的正对照与 tmp 树病灶夹具（has-teeth）。
  3. 钩子非致命：**真跑** `_post_scan_notify`（兄弟钩子全换桩），卖权这一段任一处抛异常
     ⇒ 方法不崩、有 warning、后面的步骤照跑。
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
import json
import logging
import subprocess
import sys
import types
from pathlib import Path

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

    def run(swarm=None):
        r = ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)
        r.date_str = AS_OF
        r.report_dir = tmp_path
        r.memory_store = r._session_id = r.vector_memory = r.slack_notifier = None
        r._submit_bg = lambda *a, **k: None
        report = {"markdown_report": "# 日报正文\n", "opportunities": []}
        r._post_scan_notify(types.SimpleNamespace(board=None, targets=[]),
                            _swarm() if swarm is None else swarm, report, 1.0)
        return report, r
    return run


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
        """整轮 0 行可记（CBOE 全线不可得 / 快照模式）⇒ warning 带取数原因计数。变异：删掉那段 warning。"""
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (None, "snapshot_mode_no_raw_chain"))
        with caplog.at_level(logging.INFO):
            notify()
        warns = [m for m in _messages(caplog, logging.WARNING) if "0 行可记" in m]
        assert len(warns) == 1 and "snapshot_mode_no_raw_chain" in warns[0], _messages(caplog, logging.WARNING)

    def test_zero_row_alarm_reports_row_reasons_not_fetch_reasons(self, notify, monkeypatch, caplog):
        """取数成功但窗口里没有到期日（只挂着 60 DTE 的链）⇒ 两档 0 行。告警给的必须是**行上**的原因
        （no_expiry_in_window），不是取数那一步的 `{'ok': 1}`——后者在「取数好、后面坏」时自相矛盾。
        变异：0 行告警改回打印 `fetch_reasons`。"""
        monkeypatch.setattr(LG, "_default_fetch", lambda t, *, as_of: (
            (None, "stale_vintage") if t == "BBB" else (_raw(t, as_of, dtes=(60,)), None)))
        with caplog.at_level(logging.INFO):
            notify()
        warns = [m for m in _messages(caplog, logging.WARNING) if "0 行可记" in m]
        assert len(warns) == 1, _messages(caplog, logging.WARNING)
        assert "no_expiry_in_window" in warns[0] and "'ok'" not in warns[0], warns[0]
        assert "monthly" in warns[0] and "weekly" in warns[0], "按档给原因"


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
