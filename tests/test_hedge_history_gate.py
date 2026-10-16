"""v0.45.440：对冲账本历史只经 `portfolio_greeks.load_history()` 读（静态守卫）。

2026-09-04~10-07 的 `hedge_state/` 记录标的价 / SPY 成交价晚一个交易日，且**自己不说**；按用户决定原样保留、不重算。
`load_history()` 只放行能自证定价场次的记录。这里守的是另一半：**别的模块不许绕过它直读历史**——
否则「日后看对冲带该不该改」那类分析会把脏记录与干净记录静默混算，没有任何东西会红。

判据（AST，非测试的 .py 全扫，含 experiments/、scripts/、alphabot/）：
  · 字符串常量里出现 `hedge_state`（docstring 不算：那是说明，不是读）；
  · 经 `import portfolio_greeks [as x]` / `from portfolio_greeks import …` 碰历史文件路径或读写助手。
命中的文件必须在 `ALLOWED` 里且写明理由；`ALLOWED` 里的文件也必须真的还命中（两头都守：
怕它变大——新读者绕闸；也怕它变小——白名单过期了还挂着，等于给下一个读者留了后门）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _repo_files import own_python_files  # noqa: E402
#: v0.45.442 补 `_load_audit`（按日期读 greeks_<日期>.json，绕过放行判定；独立审阅指出）。
HISTORY_NAMES = frozenset({"STATE_DIR", "TRADES_FILE", "EQUITY_FILE", "POSITIONS_FILE", "META_FILE",
                           "_load_jsonl", "_load_audit"})

#: 文件（相对仓库根）→ 为什么可以直接碰 `hedge_state/`。新增前先问：能不能走 `load_history()`？
ALLOWED = {
    "portfolio_greeks.py": "账本的属主：唯一的写者，`load_history()` 就在这里",
    "data_backup/export.py": "异地备份：整目录逐字节拷贝，不解读记录",
    "data_backup/migrate_data_root.py": "数据根迁移：整目录搬运，不解读记录",
    "alert_manager.py": "告警正文里给人看的排查指引（字符串），不读文件",
    "alphabot/straddle.py": "Alpha Bot 跨式页只读**单日**审计文件 greeks_<日期>.json 展示逐腿 Greeks，不做时间序列",
}


def _docstring_nodes(tree: ast.AST) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def scan_source(src: str) -> list:
    """一段源码里碰 `hedge_state/` 历史的地方（行号 + 形状）；空列表 = 没碰。"""
    tree = ast.parse(src)
    docs = _docstring_nodes(tree)
    aliases = set()
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "portfolio_greeks":
                    aliases.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "portfolio_greeks":
            for a in node.names:
                if a.name in HISTORY_NAMES:
                    hits.append((node.lineno, f"from portfolio_greeks import {a.name}"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "hedge_state" in node.value \
                and id(node) not in docs:
            hits.append((getattr(node, "lineno", 0), "字符串常量含 hedge_state"))
        elif isinstance(node, ast.Attribute) and node.attr in HISTORY_NAMES \
                and isinstance(node.value, ast.Name) and node.value.id in aliases:
            hits.append((node.lineno, f"{node.value.id}.{node.attr}"))
    return hits


def _py_files():
    """本仓自己的 .py——**共享清单** `tests/_repo_files.own_python_files`（git ls-files 优先），测试目录除外。
    v0.45.442：此前手写裸 rglob，会扫进未跟踪文件——iCloud 在 ~/Desktop 下持续造的「portfolio_greeks 2.py」
    这类重名副本含 `hedge_state` ⇒ 不在白名单 ⇒ 假红，且只在有副本的那台机器上红（`_repo_files` 文档里 v0.45.176 同款）。"""
    files, _how = own_python_files(ROOT)
    for p in files:
        rel = p.relative_to(ROOT)
        if rel.parts and rel.parts[0] == "tests":
            continue
        yield rel.as_posix(), p


def _scan_repo() -> dict:
    found = {}
    for rel, p in _py_files():
        try:
            hits = scan_source(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if hits:
            found[rel] = hits
    return found


def unallowed(found: dict, allowed: dict) -> dict:
    """命中了、却不在白名单里的文件（新读者绕闸）。"""
    return {f: h for f, h in found.items() if f not in allowed}


def stale(found: dict, allowed: dict) -> list:
    """白名单里有、却已经不命中的文件（过期条目）。"""
    return sorted(f for f in allowed if f not in found)


class TestOnlyTheGateReadsHedgeHistory:

    def test_no_new_reader_bypasses_load_history(self):
        new = unallowed(_scan_repo(), ALLOWED)
        assert not new, (f"这些文件绕过 `portfolio_greeks.load_history()` 直接碰 hedge_state/ 历史：{new}。"
                         "读历史请走 load_history()（只放行能自证定价场次的记录）；确属搬运 / 展示单日，"
                         "再进 ALLOWED 并写明理由。")

    def test_allowlist_has_no_stale_entries(self):
        gone = stale(_scan_repo(), ALLOWED)
        assert not gone, f"ALLOWED 里这些文件已经不碰 hedge_state/ 了，删掉条目（过期的白名单是后门）：{gone}"


class TestScannerHasTeeth:
    """反向自证：上面两条对真仓库为空，不能是因为扫描器是瞎的。"""

    def test_flags_a_direct_history_read(self):
        assert scan_source("import json\nrows = open('hedge_state/trades.jsonl').read()\n")
        assert scan_source("import portfolio_greeks as pg\nx = pg._load_jsonl(pg.EQUITY_FILE)\n")
        assert scan_source("from portfolio_greeks import TRADES_FILE\n")
        assert scan_source("import portfolio_greeks as pg\na = pg._load_audit('2026-10-08')\n")
        assert scan_source("p = home / f'hedge_state/greeks_{d}.json'\n")

    def test_ignores_docstrings_and_the_gate_itself(self):
        assert scan_source('"""说明：数据在 hedge_state/ 下。"""\nimport portfolio_greeks as pg\nh = pg.load_history()\n') == []

    def test_both_comparisons_can_go_red(self):
        """真仓库上两条比对恒空，所以比对本身要在合成输入上证明会红（v0.45.440 变异 G15：关掉新读者检查曾全绿存活）。"""
        found = {"portfolio_greeks.py": [(1, "x")], "experiments/hedge_band_study.py": [(3, "字符串常量含 hedge_state")]}
        assert set(unallowed(found, ALLOWED)) == {"experiments/hedge_band_study.py"}
        assert unallowed({"portfolio_greeks.py": [(1, "x")]}, ALLOWED) == {}
        assert stale({"portfolio_greeks.py": [(1, "x")]}, ALLOWED) == sorted(set(ALLOWED) - {"portfolio_greeks.py"})
        assert stale({f: [(1, "x")] for f in ALLOWED}, ALLOWED) == []

    def test_untracked_icloud_duplicate_is_not_scanned(self, tmp_path, monkeypatch):
        """v0.45.442：清单走 `git ls-files`——iCloud 造的未跟踪「portfolio_greeks 2.py」含 hedge_state，不能让守卫假红。"""
        import shutil
        import subprocess
        assert shutil.which("git"), "本守卫的清单靠 git ls-files（开发检出 / worktree / CI 都有 git）"
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "alpha.py").write_text("x = 'hedge_state/trades.jsonl'\n", encoding="utf-8")
        subprocess.run(["git", "add", "alpha.py"], cwd=tmp_path, check=True)
        (tmp_path / "portfolio_greeks 2.py").write_text("x = 'hedge_state'\n", encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
        assert {rel for rel, _ in _py_files()} == {"alpha.py"}
        assert set(_scan_repo()) == {"alpha.py"}

    def test_real_repo_scan_sees_the_known_readers(self):
        found = _scan_repo()
        assert {"portfolio_greeks.py", "alphabot/straddle.py"} <= set(found), sorted(found)
