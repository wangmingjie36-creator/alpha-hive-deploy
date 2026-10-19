"""落盘写入 / 读取的 AST 扫描器——`test_persistent_writes_have_readers.py` 的量具（v0.45.543）。

拆成独立模块只为一件事：登记表很长，量具与登记表放一起会把「判据」淹没在几百行条目里。
判据的有牙测试在那个测试文件的 `TestScannerHasTeeth`，本模块不单独测。

两个维度
--------
**表**（SQLite）：生产源码里的 SQL 字符串常量 / f-string / `"…".format(t=X)`。
  · 写 = `CREATE TABLE [IF NOT EXISTS] t` / `INSERT [OR …] INTO t` / `REPLACE INTO t` / `UPDATE t SET`（关键字只认大写——
    仓内 SQL 全是大写，小写 `update` 多半是散文）；
  · 读 = 含 `SELECT` 的字符串里的 `FROM t` / `JOIN t`（`DELETE FROM` 不算读）。
  · 表名里的占位符 `{TABLE}` / `{self.TABLE}` / `{PredictionStore.TABLE}` / `.format(t=TABLE)` 按**同文件**里
    `TABLE = "…"` 的赋值解析（类属性或模块常量）；解析不了 ⇒ 表名原样带花括号，登记表里没有它 ⇒ 红。
  · docstring（语句位置的裸字符串）不算：`backtester.py` / `paper_portfolio.py` 的文档里就写着 `CREATE TABLE`。
  · 函数体里按名字引用的**模块级**字符串常量（`_SQL = "SELECT …"`）归到引用它的函数。

**文件**：写入落点（不看函数叫什么、也不看路径怎么拼），沿用 `test_ledger_writes_go_through_ledger_io.py` 的
六类（裸追加 / json.dump / dumps 直写 / 手写原子替换 / dumps 后写文件 / 手写 *_jsonl 助手），再加：
`open(…, "w"/"x"/"wb")`、`write_text` / `write_bytes`、`pickle|joblib|yaml.dump`、`to_csv` / `to_parquet` /
`to_pickle` / `savefig`、`ledger_io.write_* / append_*`、`atomic_json_write`、`shutil.copy*/move/copytree`。
落点按「文件::限定名」认（嵌套函数记在自己名下；lambda 记在外层），与那个守卫同一套作用域规则。

读者核对（`reads_token` / `reads_table`）是**成员粒度**：在登记的那个函数自己的源码里找——
文件看令牌（文件名片段出现在字符串常量 / f-string 片段里，或 `PATHS.<属性>` 访问），表看 SELECT。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ast_cache import parse, read_text  # noqa: E402
from _repo_files import own_python_files  # noqa: E402
from test_ledger_writes_go_through_ledger_io import _is_open, _own_calls, _scope_kinds  # noqa: E402

EXCLUDED_TOP = ("tests", "experiments")

# ─────────────────────────────── 作用域

def scopes(tree: ast.Module):
    """[(限定名, 节点)]：模块记 "<module>"，类与函数按嵌套拼名（同 ledger 守卫）。"""
    out = [("<module>", tree)]

    def visit(node, qual):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = ch.name if qual == "<module>" else f"{qual}.{ch.name}"
                out.append((q, ch))
                visit(ch, q)
    visit(tree, "<module>")
    return out


def _own_nodes(scope: ast.AST):
    """作用域自己的节点：不进嵌套 def / class，进 lambda（同 `_own_calls`）。"""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


# ─────────────────────────────── 文件写入落点

_EXTRA_DUMP_MODULES = ("pickle", "joblib", "yaml")
_FRAME_WRITERS = ("to_csv", "to_parquet", "to_pickle", "savefig")
_SHUTIL = ("copy", "copy2", "copyfile", "copytree", "move")


def file_write_kinds(scope: ast.AST) -> set:
    kinds = set(_scope_kinds(scope))
    for c in _own_calls(scope):
        mode = _is_open(c)
        if mode and any(ch in mode for ch in "wx"):
            kinds.add("open_write")
        f = c.func
        if isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else ""
            if f.attr in ("write_text", "write_bytes"):
                kinds.add("write_text")
            elif f.attr == "dump" and base in _EXTRA_DUMP_MODULES:
                kinds.add("binary_dump")
            elif f.attr in _FRAME_WRITERS:
                kinds.add("frame_write")
            elif base == "ledger_io" and f.attr.startswith(("write", "append")):
                kinds.add("ledger_io")
            elif f.attr == "atomic_json_write":
                kinds.add("atomic_json_write")
            elif base == "shutil" and f.attr in _SHUTIL:
                kinds.add("shutil")
        elif isinstance(f, ast.Name) and f.id == "atomic_json_write":
            kinds.add("atomic_json_write")
    return kinds


# ─────────────────────────────── 字符串（含 f-string 与 .format）

def _render(node: ast.AST, consts: dict) -> str | None:
    """字符串表达式 → 文本；f-string 的占位按 `consts` 解析（`{self.TABLE}` 认最后一段属性名），解析不了留 `{原样}`。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                parts.append(_resolve_placeholder(v.value, consts))
        return "".join(parts)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"
            and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)):
        text = node.func.value.value
        for kw in node.keywords:
            if kw.arg:
                text = text.replace("{" + kw.arg + "}", _resolve_placeholder(kw.value, consts))
        return text
    return None


def _resolve_placeholder(expr: ast.AST, consts: dict) -> str:
    name = expr.attr if isinstance(expr, ast.Attribute) else expr.id if isinstance(expr, ast.Name) else None
    if name is not None and name in consts:
        return consts[name]
    try:
        return "{" + ast.unparse(expr) + "}"
    except Exception:  # pragma: no cover
        return "{?}"


def _docstring_ids(tree: ast.Module) -> set:
    """语句位置的裸字符串（docstring 与散落的字符串语句）——不是 SQL。"""
    return {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, (ast.Constant, ast.JoinedStr))}


def str_constants(tree: ast.Module) -> dict:
    """同文件里 `NAME = "…"`（模块级或类体内）的字符串赋值；同名多值 ⇒ 不收（宁可留占位让它红）。"""
    seen: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    seen.setdefault(t.id, set()).add(n.value.value)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str):
            seen.setdefault(n.target.id, set()).add(n.value.value)
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def _module_level_strings(tree: ast.Module, consts: dict) -> dict:
    """模块顶层 `NAME = <字符串表达式>`：函数按名字引用时把它算进那个函数（`_SQL = "SELECT …"` 的写法）。"""
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) or n.value is None:
                continue
            text = _render(n.value, consts)
            if text is None:
                # 路径常量 `SIGNALS_FILE = STATE_DIR / "vrp_signals.jsonl"`：把其中的字符串片段归给引用它的函数
                pieces = [_render(x, consts) for x in ast.walk(n.value) if isinstance(x, (ast.Constant, ast.JoinedStr))]
                pieces = [p for p in pieces if p]
                text = " ".join(pieces) if pieces else None
            if text is not None:
                out[targets[0].id] = text
    return out


def scope_strings(scope: ast.AST, consts: dict, doc_ids: set, module_strings: dict) -> list:
    texts = []
    nodes = list(_own_nodes(scope))
    # f-string 的常量片段与 `"…".format()` 的接收者已随整体渲染过——单独再算一遍会得到占位符被切掉的残片
    # （`"CREATE TABLE IF NOT EXISTS "` 被读成建一张叫 IF 的表）
    parts = {id(v) for n in nodes if isinstance(n, ast.JoinedStr) for v in n.values}
    parts |= {id(n.func.value) for n in nodes if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "format" and isinstance(n.func.value, ast.Constant)}
    for n in nodes:
        if id(n) in doc_ids or id(n) in parts:
            continue
        if isinstance(n, (ast.Constant, ast.JoinedStr, ast.Call)):
            t = _render(n, consts)
            if t is not None:
                texts.append(t)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in module_strings \
                and not isinstance(scope, ast.Module):
            texts.append(module_strings[n.id])
    return texts


def scope_path_attrs(scope: ast.AST) -> set:
    """作用域里访问过的 `PATHS.<属性>`（文件令牌可以写成 "PATHS.ml_model"）。"""
    return {f"PATHS.{n.attr}" for n in _own_nodes(scope)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "PATHS"}


# ─────────────────────────────── SQL

_T = r'["`\[]?([\w{}.]+)'
_SQL_WRITE = re.compile(r"\b(?:CREATE TABLE(?: IF NOT EXISTS)?|INSERT(?: OR [A-Z]+)? INTO|REPLACE INTO)\s+" + _T
                        + r"|\bUPDATE\s+" + _T + r"\s+SET\b")
_SQL_READ = re.compile(r"(?<!DELETE )\b(?:FROM|JOIN)\s+" + _T)


def sql_tables(texts) -> tuple:
    """(写的表, 读的表)。读只在含 SELECT 的字符串里认。"""
    written, read = set(), set()
    for t in texts:
        for m in _SQL_WRITE.finditer(t):
            written.add(_clean(m.group(1) or m.group(2)))
        if "SELECT" in t:
            for m in _SQL_READ.finditer(t):
                read.add(_clean(m.group(1)))
    written.discard("")
    read.discard("")
    return written, read


def _clean(name: str) -> str:
    return name.strip('"`[]').split(".")[-1] if "{" not in name else name


# ─────────────────────────────── 全仓

class FileFacts:
    """一个文件的扫描结果：每个作用域的写入落点、写 / 读的表、字符串、PATHS 属性。"""

    def __init__(self, rel: str, src: str):
        self.rel = rel
        tree = parse(src)
        consts = str_constants(tree)
        doc_ids = _docstring_ids(tree)
        mod_strings = _module_level_strings(tree, consts)
        self.scopes: dict = {}
        self.nodes: dict = {}
        for qual, node in scopes(tree):
            self.nodes[qual] = node
            texts = scope_strings(node, consts, doc_ids, mod_strings)
            w, r = sql_tables(texts)
            self.scopes[qual] = {
                "write_kinds": file_write_kinds(node),
                "tables_written": w,
                "tables_read": r,
                "texts": texts,
                "paths_attrs": scope_path_attrs(node),
            }


def scan_source(src: str, rel: str = "x.py") -> FileFacts:
    return FileFacts(rel, src)


_REPO_FACTS = None


def repo_facts() -> dict:
    """{相对路径: FileFacts}；只扫本仓 .py（git ls-files），去掉 tests/ 与 experiments/。"""
    global _REPO_FACTS
    if _REPO_FACTS is None:
        out = {}
        files, _how = own_python_files(ROOT)
        for p in files:
            rel = p.relative_to(ROOT)
            if rel.parts and rel.parts[0] in EXCLUDED_TOP:
                continue
            try:
                out[rel.as_posix()] = FileFacts(rel.as_posix(), read_text(p))
            except (SyntaxError, UnicodeDecodeError):
                continue
        _REPO_FACTS = out
    return _REPO_FACTS


def file_write_sites(facts: dict) -> dict:
    """{"文件::限定名": {kind…}}"""
    return {f"{rel}::{q}": s["write_kinds"] for rel, ff in facts.items()
            for q, s in ff.scopes.items() if s["write_kinds"]}


def table_writers(facts: dict) -> dict:
    """{表: {"文件::限定名"…}}"""
    out: dict = {}
    for rel, ff in facts.items():
        for q, s in ff.scopes.items():
            for t in s["tables_written"]:
                out.setdefault(t, set()).add(f"{rel}::{q}")
    return out


def table_readers(facts: dict) -> dict:
    out: dict = {}
    for rel, ff in facts.items():
        for q, s in ff.scopes.items():
            for t in s["tables_read"]:
                out.setdefault(t, set()).add(f"{rel}::{q}")
    return out


def scope_reads_token(facts: dict, site: str, token: str) -> bool:
    rel, qual = site.split("::", 1)
    ff = facts.get(rel)
    if ff is None or qual not in ff.scopes:
        return False
    s = ff.scopes[qual]
    if token.startswith("PATHS."):
        return token in s["paths_attrs"]
    return any(token in t for t in s["texts"])


def scope_exists(facts: dict, site: str) -> bool:
    rel, qual = site.split("::", 1)
    return rel in facts and qual in facts[rel].scopes


def nonpy_reads_token(rel: str, token: str) -> bool | None:
    """非 Python 读者（编排器 .sh / Alpha Bot 静态 JS）：文件级找令牌。文件不在 ⇒ None。"""
    p = ROOT / rel
    if not p.is_file():
        return None
    return token in p.read_text(encoding="utf-8", errors="replace")


_READ_ATTRS = ("read_text", "read_bytes", "load", "loads", "read_csv", "read_parquet", "read_json", "read_pickle",
               "load_json", "load_jsonl", "readlines", "read")


def scope_has_read_call(facts: dict, site: str) -> bool:
    """SELF 缓存的旁证：函数里确有读调用（open 读模式 / read_text / json.load / ledger_io.load_* / pd.read_* …）。"""
    rel, qual = site.split("::", 1)
    node = facts[rel].nodes[qual]
    for c in _own_calls(node):
        mode = _is_open(c)
        f = c.func
        is_open = (isinstance(f, ast.Name) and f.id == "open") or (isinstance(f, ast.Attribute) and f.attr == "open")
        if is_open and (mode is None or not any(ch in mode for ch in "wax")):
            return True
        if isinstance(f, ast.Attribute) and f.attr in _READ_ATTRS:
            return True
        # 缓存读助手：read_json_cache(p) / _load_cached(p) / self._read_cache(k) …（名字里有 read / load 词元）
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        if _READ_HELPER.search(name):
            return True
    return False


_READ_HELPER = re.compile(r"(?:^|_)(?:read|load)(?:_|$)", re.I)
