"""墓碑（v0.45.531）：`AgentWeightManager` / `agent_weights` 表已退役，别让它悄悄回来，更别接进评分。

它曾在每日扫描里做两件事，结果写进 `pheromone.db::agent_weights`：
① `recalculate_all_weights`：按 `MemoryStore.get_agent_accuracy` 给每只蜂算 `1 + (acc − 0.5) × 2`；
② `apply_dimension_feedback(adapted)`：把 `Backtester.adapt_weights()` 的五维权重（再与 feedback_loop 建议对半合并）映射成五只蜂的乘数。

退役理由（2026-10-17/18 只读核对生产库，v0.45.531 CHANGELOG 有全文）：
- **没有读者**：`agent_weights` 在本仓、部署脚本、定时任务里都没有评分侧读者，算完就停在表里。
- **① 量的不是蜂**：`agent_memory.actual_outcome` 由快照级 `snapshot.direction` 判，同一 (ticker, date) 下所有蜂共用一个结果
  （有结果的 1,418 组里组内不一致 0 组）⇒ 9 只蜂「准确率」全在 0.146–0.155，全员贴 0.3 下限，信息量为零。
- **② 是已断开的旁路**：表里那些看起来「学出来」的差异全来自 `adapted_weights`——v0.45.176 断开、CLAUDE.md 明令勿接回的第三条权重通道
  （零技能置换检验：5 蜂 4 只技能 Δ 在 ±2.2pp 内且 CI 全跨 0；学的是「谁更爱说中性」）。把 `agent_weights` 接进评分 = 换个入口接回旁路。
- 接进评分还会截断维度 IC 协议 H2、让共振加成前瞻检验自证失败。

要「按蜂的准确率加权」，正确的度量是每只蜂**自己的票**（`feedback_loop.agent_vote_correct`），而它已经验证过是阴性。
生产库里的旧 `agent_weights` 表原样留着（删表 = 改生产数据，需另行批准）；新库不再建。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests import _ast_cache
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
_SELF = Path(__file__).resolve()

RETIRED_MODULE = "agent_weight_manager"
RETIRED_CALLS = {"recalculate_all_weights", "apply_dimension_feedback"}
RETIRED_STORE_API = ("get_agent_weights", "update_agent_weight", "get_agent_accuracy")
# SQL 里把 agent_weights 当表名用（读、写、建都算）：FROM / INTO / UPDATE / TABLE [IF NOT EXISTS] agent_weights
_SQL_TABLE = re.compile(r"(?i)\b(from|into|update|join|table(\s+if\s+not\s+exists)?)\s+agent_weights\b")


def _imports_retired(tree: ast.AST) -> list[int]:
    out = []
    for n in ast.walk(tree):
        names = ([a.name for a in n.names] if isinstance(n, ast.Import)
                 else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
        if any(x.split(".")[0] == RETIRED_MODULE for x in names):
            out.append(n.lineno)
    return out


def _retired_calls(tree: ast.AST) -> list[int]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name in RETIRED_CALLS or name in RETIRED_STORE_API:
                out.append(n.lineno)
    return out


def _sql_on_table(tree: ast.AST) -> list[int]:
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and _SQL_TABLE.search(n.value)]


def _scan(detector) -> list[str]:
    files, _how = own_python_files(REPO_ROOT)
    offenders = []
    for f in files:
        if f.resolve() == _SELF:
            continue
        try:
            tree = _ast_cache.parse_file(f, errors="replace")
        except SyntaxError:
            continue
        offenders += [f"{f.relative_to(REPO_ROOT).as_posix()}:{ln}" for ln in detector(tree)]
    return offenders


class TestAgentWeightManagerStaysRetired:

    def test_module_file_is_gone(self):
        assert not (REPO_ROOT / f"{RETIRED_MODULE}.py").exists(), \
            "agent_weight_manager.py 又回来了（v0.45.531 已删；理由见本文件 docstring）"

    def test_nothing_imports_it(self):
        """AST 认 import（说明文字 / 注释里提它的名字不算）。"""
        assert not _scan(_imports_retired)

    def test_no_calls_to_retired_methods(self):
        """换个类名重写一遍也拦得住：认方法名，不认类名。"""
        assert not _scan(_retired_calls)

    def test_no_sql_reads_or_writes_agent_weights(self):
        """读也拦：接进评分的第一步就是 SELECT 这张表。"""
        assert not _scan(_sql_on_table)

    def test_memory_store_api_removed(self):
        from memory_store import MemoryStore
        back = [a for a in RETIRED_STORE_API if hasattr(MemoryStore, a)]
        assert not back, f"MemoryStore 又长回了 {back}"

    def test_fresh_store_does_not_create_table(self, memory_store):
        import sqlite3
        conn = sqlite3.connect(memory_store.db_path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert "agent_weights" not in tables
        assert "agent_memory" in tables   # 正对照：建表本身照常


class TestDetectorsHaveTeeth:
    """正对照：每个探测器喂一段旧代码的真实形状，必须报出来——否则上面全绿证明不了任何事。"""

    def test_import_detector(self):
        assert _imports_retired(ast.parse("from agent_weight_manager import AgentWeightManager\n"))
        assert _imports_retired(ast.parse("import agent_weight_manager as m\n"))
        assert not _imports_retired(ast.parse("import feedback_loop\n"))

    def test_call_detector(self):
        assert _retired_calls(ast.parse("_wm.recalculate_all_weights()\n_wm.apply_dimension_feedback(adapted)\n"))
        assert _retired_calls(ast.parse("self.memory_store.get_agent_accuracy(a, period='t7')\n"))
        assert not _retired_calls(ast.parse("bt.adapt_weights(min_samples=10, period='t7')\n"))

    def test_sql_detector(self):
        old = ('"""\n    UPDATE agent_weights\n    SET adjusted_weight = ?, last_updated = CURRENT_TIMESTAMP WHERE agent_id = ?\n"""\n')
        assert _sql_on_table(ast.parse(old))
        assert _sql_on_table(ast.parse('"SELECT agent_id, adjusted_weight FROM agent_weights ORDER BY agent_id"\n'))
        assert _sql_on_table(ast.parse('"CREATE TABLE IF NOT EXISTS agent_weights (id INTEGER)"\n'))
        # 不误伤：adapted_weights 是仍在用的审计表；说明文字里提名字不算 SQL
        assert not _sql_on_table(ast.parse('"SELECT * FROM adapted_weights"\n'))
        assert not _sql_on_table(ast.parse('"agent_weights 表已退役"\n'))
