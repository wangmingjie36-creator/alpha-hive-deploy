"""v0.45.448：账本写入只经 `ledger_io`（静态守卫）——不许再手写会造出坏行的写法。

坏行的三个代码来源（`ledger_io` 模块文档）：裸追加写到一半（半行粘行）、读-改-写不加锁（丢更新）、不拒 NaN。
`ledger_io` 一次堵死；这里守的是「别处不许再造一份」：
  · **裸追加**：`open(x, "a")` / `x.open("a")`（含 "ab" / "a+" 及 `mode=` 关键字）——写到一半崩溃就是半行；
  · **手写 JSONL 读写助手**：名字以 `_jsonl` 结尾、却不经 `ledger_io` 的函数（各写一份 = 各漏各的口子）。
命中按「文件::函数」记。**现存的**逐条登记在 `DEBT`，写明它是什么、为什么还没迁（迁一个删一条）；
两头都守：新增命中红（怕它变大），登记了却不再命中也红（怕它过期——过期条目是给下一个写者留的后门）。

清单走共享 `tests/_repo_files.own_python_files`（git ls-files；v0.45.442 教训：裸 rglob 扫进 iCloud 副本假红）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _repo_files import own_python_files  # noqa: E402

#: 「文件::函数」→ 它是什么、为什么还在。v0.45.448 第一版迁走了 portfolio_greeks / paper_portfolio；其余逐版迁。
#: 「待迁移」= 写的是会被读回参与计算 / 判断的数据，坏一行就有后果；「保留」= 只给人看的日志 / 只读展示，写明理由。
_TODO = "待迁移（数据，会被读回）："
DEBT: dict = {
    # ── 账本模块（与已迁的两个同形，下一版起迁）
    "options_paper_leg.py::_append_jsonl": _TODO + "跨式腿 closed_trades 裸追加",
    "options_paper_leg.py::_load_jsonl": _TODO + "跨式腿账本读取（坏行行为未统一）",
    "options_paper_leg.py::_write_jsonl": _TODO + "跨式腿持仓 / 净值重写（原子、清洗 NaN，但不校验对象、无锁）",
    "earnings_vol_signal.py::_load_jsonl": _TODO + "财报事件波动信号账本读取",
    "earnings_vol_signal.py::_write_jsonl": _TODO + "财报事件波动信号账本重写（原子，但不拒 NaN、无锁）",
    "vrp_signal.py::_load_jsonl": _TODO + "VRP 信号账本读取",
    "vrp_signal.py::_write_jsonl": _TODO + "VRP 信号账本重写（原子，但不拒 NaN、无锁）",
    "probability_scorecard.py::record_published": _TODO + "概率记分卡账本裸追加",
    "ibkr_sync.py::import_ibkr_statement": _TODO + "real_fills.jsonl 真实成交裸追加",
    "iv_history.py::append_observation": _TODO + "IV 观测索引（进 iv_rank）",
    "price_history.py::append_observation": _TODO + "本地价格索引",
    "weekly_optimizer.py::append_history": _TODO + "weight_history.jsonl 权重审计轨迹",
    "ml_model_guard.py::snapshot_model_file": _TODO + "模型快照 manifest.jsonl（守卫读回）",
    "data_migrations/runner.py::append_record": _TODO + "数据迁移账本（据此判哪些迁移已执行）",
    "pheromone_board.py::PheromoneBoard._save_fallback_batch": _TODO + "信息素板写库失败时的兜底批次",
    "alphabot/service.py::AlphaBotService.take_snapshot": _TODO + "Alpha Bot 盘中快照（页面读回展示）",
    "data_backup/run_backup.py::_append_history": _TODO + "备份历史（Step 15 连续性检查 backup_continuity 读回）",
    "report_deployer.py::_append_gh_pages_deploy_log": _TODO + "gh-pages 部署结局日志（report_deployer 自己读回）",
    # ── 保留
    "alphabot/launcher.py::_open_server_log": "保留（日志）：Alpha Bot 服务进程 stdout/stderr 文本日志，只给人看、不被解析",
    "code_executor.py::CodeExecutor._write_audit_log": "保留（日志）：代码执行沙箱审计日志，只原样读回给人看、不进任何计算",
    "alphabot/straddle.py::read_jsonl": "保留（只读展示）：坏行计数随结果返回并显示在页面上（不吞、也不因外部损坏让整页崩）",
}


def _append_mode(call: ast.Call, method: bool) -> bool:
    """这次 open 调用是不是追加模式。`open(path, mode)` 的 mode 是第 2 个实参；`path.open(mode)` 是第 1 个。"""
    idx = 0 if method else 1
    mode = None
    if len(call.args) > idx and isinstance(call.args[idx], ast.Constant):
        mode = call.args[idx].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and "a" in mode


def _uses_ledger_io(fn: ast.AST) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and n.id == "ledger_io":
            return True
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "ledger_io":
            return True
    return False


def scan_source(src: str) -> dict:
    """一段源码里的命中：{"函数限定名": {"raw_append" / "jsonl_helper", …}}；模块级命中记在 "<module>"。"""
    tree = ast.parse(src)
    hits: dict = {}

    def visit(node, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{qual}.{child.name}" if qual != "<module>" else child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.endswith("_jsonl") \
                        and not _uses_ledger_io(child):
                    hits.setdefault(q, set()).add("jsonl_helper")
                visit(child, q)
                continue
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Name) and f.id == "open" and _append_mode(child, method=False):
                    hits.setdefault(qual, set()).add("raw_append")
                elif isinstance(f, ast.Attribute) and f.attr == "open" and _append_mode(child, method=True):
                    hits.setdefault(qual, set()).add("raw_append")
            visit(child, qual)

    visit(tree, "<module>")
    return hits


def _scan_repo() -> dict:
    found = {}
    files, _how = own_python_files(ROOT)
    for p in files:
        rel = p.relative_to(ROOT)
        if (rel.parts and rel.parts[0] == "tests") or rel.as_posix() == "ledger_io.py":
            continue
        try:
            hits = scan_source(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for qual, kinds in hits.items():
            found[f"{rel.as_posix()}::{qual}"] = kinds
    return found


def unregistered(found: dict, debt: dict) -> dict:
    return {k: v for k, v in found.items() if k not in debt}


def stale(found: dict, debt: dict) -> list:
    return sorted(k for k in debt if k not in found)


class TestLedgerWritesGoThroughLedgerIo:

    def test_no_new_hand_rolled_ledger_write(self):
        new = unregistered(_scan_repo(), DEBT)
        assert not new, (f"这些地方手写了会造出坏行的账本写法：{new}。改用 ledger_io（append_jsonl / write_jsonl / "
                         "load_jsonl / locked）；确属不进任何计算的日志，再登记进 DEBT 并写明理由。")

    def test_debt_has_no_stale_entries(self):
        gone = stale(_scan_repo(), DEBT)
        assert not gone, f"DEBT 里这些条目已经不命中了（迁完了？），删掉——过期条目是后门：{gone}"


class TestScannerHasTeeth:
    """反向自证：上面两条对真仓库为空，不能是因为扫描器是瞎的、比对是关着的。"""

    def test_flags_raw_appends_in_every_spelling(self):
        assert scan_source("def f(p):\n    with open(p, 'a') as fh:\n        fh.write('x')\n") == {"f": {"raw_append"}}
        assert scan_source("def f(p):\n    p.open('a', encoding='utf-8')\n") == {"f": {"raw_append"}}
        assert scan_source("def f(p):\n    open(p, mode='ab')\n") == {"f": {"raw_append"}}
        assert scan_source("class C:\n    def g(self, p):\n        open(p, 'a+')\n") == {"C.g": {"raw_append"}}
        assert scan_source("open('x.log', 'a')\n") == {"<module>": {"raw_append"}}

    def test_ignores_reads_writes_and_delegating_helpers(self):
        assert scan_source("def f(p):\n    open(p)\n    open(p, 'r')\n    open(p, 'w')\n    p.open('rb')\n") == {}
        assert scan_source("import ledger_io\ndef _write_jsonl(p, r):\n    ledger_io.write_jsonl(p, r)\n") == {}

    def test_flags_hand_rolled_jsonl_helpers(self):
        assert scan_source("import json\ndef _load_jsonl(p):\n    return [json.loads(l) for l in open(p)]\n") \
            == {"_load_jsonl": {"jsonl_helper"}}

    def test_both_comparisons_can_go_red(self):
        found = {"a.py::f": {"raw_append"}, "b.py::g": {"jsonl_helper"}}
        assert set(unregistered(found, {"a.py::f": "日志"})) == {"b.py::g"}
        assert unregistered(found, {"a.py::f": "x", "b.py::g": "y"}) == {}
        assert stale(found, {"a.py::f": "x", "c.py::h": "z"}) == ["c.py::h"]
        assert stale(found, {"a.py::f": "x"}) == []

    def test_migrated_modules_are_clean(self):
        """第一版迁走的两个模块：一个命中都不许有（它们的账本写入现在全经 ledger_io）。"""
        found = _scan_repo()
        left = {k: v for k, v in found.items() if k.split("::")[0] in ("portfolio_greeks.py", "paper_portfolio.py")}
        assert left == {}, left
