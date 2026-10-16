"""凡是往 `signal_archive` 写行的代码，都必须问一句 `is_quarantined`（v0.45.511）。

隔离名单（`signal_archive.QUARANTINE`）只在正式入库口 `archive()`（经 `_rows_for`）拦截。绕开它直写 SQL 的脚本
各自负责——而「各自负责」实测靠不住：10-13 10-12 重放写回「整日删除再插入」不查名单，把 v0.45.499 清掉的
BILI 假 IV 插了回去（v0.45.502 补）；`backfill_fear_greed_legacy.py` 从 v0.45.267 起也一直直写不查（v0.45.511 补）。
下一个回填 / 写回脚本照样会漏，所以把规则变成会红的断言。

判据（逐文件，AST）：
  · 写入者 = 字符串字面量（含 f-string）里有 `INSERT/REPLACE … INTO signal_archive`，或目标是占位符（`{TABLE}` / `{t}` …）
    且该文件提到 `signal_archive`（占位符静态解析不了，宁可多认、靠豁免表逐条说明）；
  · 写入者必须引用 `is_quarantined`。粒度是文件不是函数——够抓「整份脚本根本没想到这件事」，抓不住「想到了但
    某条分支漏了」，后者靠各脚本自己的行为测试（如 `test_writeback_1012_quarantine_guard.py`）。
看不见的：不经 SQL 字面量的写入（`executescript` 灌导出文件 —— `data_backup/restore.py` 整库重建，忠实还原、不过滤，
改为在结果里报出命中行数，见 `tests/test_data_backup.py::TestRestoreReportsQuarantinedRows`）。
"""

import ast
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_WRITE = re.compile(r"\b(?:INSERT|REPLACE)\b(?:\s+OR\s+\w+)?\s+INTO\s+[\"'`]?(\{[^}]*\}|\w+)", re.I)

#: 被扫成「候选写入者」、但目标其实不是 signal_archive 的文件。双向：这里每一项都必须仍被扫到（否则过期 ⇒ 删掉它）。
NOT_SIGNAL_ARCHIVE = {
    "backtester.py": "`{self.TABLE}` 是 Backtester.TABLE = \"predictions\"；文件提到 signal_archive 只为读联表",
    "data_backup/export.py": "`INSERT INTO \"{table}\"` 是生成 .sql 导出文本、不写库；导回走 restore.py（见模块 docstring）",
}


def _tracked_py():
    r = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"git ls-files 失败（本测试依赖受跟踪文件清单）：{r.stderr}"
    return [p for p in r.stdout.split() if not p.startswith("tests/")]


def _scan_source(src: str):
    """→ (写入目标集合, 是否引用 is_quarantined)。"""
    tree = ast.parse(src)
    targets = set()
    refs = False
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            targets.update(m.group(1) for m in _WRITE.finditer(n.value))
        elif isinstance(n, ast.JoinedStr):
            targets.update(m.group(1) for m in _WRITE.finditer(ast.unparse(n)))
        elif isinstance(n, ast.Name) and n.id == "is_quarantined":
            refs = True
        elif isinstance(n, ast.Attribute) and n.attr == "is_quarantined":
            refs = True
        elif isinstance(n, ast.ImportFrom) and any(a.name == "is_quarantined" for a in n.names):
            refs = True
    return targets, refs


def _is_candidate(src: str, targets) -> bool:
    return "signal_archive" in targets or ("signal_archive" in src and any(t.startswith("{") for t in targets))


def _scan_repo():
    cands = {}
    for rel in _tracked_py():
        src = (ROOT / rel).read_text(encoding="utf-8")
        targets, refs = _scan_source(src)
        if targets and _is_candidate(src, targets):
            cands[rel] = refs
    return cands


def test_every_signal_archive_writer_checks_quarantine():
    cands = _scan_repo()
    bad = sorted(f for f, refs in cands.items() if not refs and f not in NOT_SIGNAL_ARCHIVE)
    assert not bad, (
        f"这些文件往 signal_archive 写行却从不问 is_quarantined：{bad}\n"
        "写之前逐行 `signal_archive.is_quarantined(date, ticker, signal)`，命中的剔除并计数（或整体中止）；"
        "若目标其实不是 signal_archive，加进 NOT_SIGNAL_ARCHIVE 并写明理由。")


def test_positive_control_scanner_sees_known_writers():
    """扫描器瞎了这条就红：正式入库口与已知直写脚本必须被认作写入者、且引用了 is_quarantined。"""
    cands = _scan_repo()
    for f in ("signal_archive.py", "backfill_fear_greed_legacy.py",
              "experiments/restore_signal_rows_from_backup.py", "experiments/writeback_1012_replay_v0484.py"):
        assert cands.get(f) is True, (f, cands.get(f))


def test_exemptions_are_not_stale():
    cands = _scan_repo()
    stale = sorted(set(NOT_SIGNAL_ARCHIVE) - set(cands))
    assert not stale, f"豁免项已不再被扫成候选写入者，删掉：{stale}"


@pytest.mark.parametrize("src,flagged", [
    ('c.execute("INSERT OR REPLACE INTO signal_archive (date,ticker,signal,value) VALUES (?,?,?,?)", r)', True),
    ('T = "signal_archive"\nc.execute(f"INSERT INTO {T} VALUES (?)", r)', True),
    ('c.execute("REPLACE INTO signal_archive VALUES (?)", r)', True),
    ('from signal_archive import is_quarantined\nc.execute("INSERT INTO signal_archive VALUES (?)", r)', False),
    ('import signal_archive as sa\nrows=[x for x in rows if not sa.is_quarantined(*x[:3])]\n'
     'c.execute(f"INSERT INTO {sa.TABLE} VALUES (?)", rows)', False),
    ('c.execute("INSERT INTO predictions VALUES (?)", r)', False),
])
def test_scanner_has_teeth(src, flagged):
    targets, refs = _scan_source(src)
    assert (bool(targets) and _is_candidate(src, targets) and not refs) is flagged
