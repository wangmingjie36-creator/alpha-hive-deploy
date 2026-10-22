"""选测登记表的「读审计」pytest 插件（v0.45.615）：记录每个测试文件在跑的时候**打开 / 列目录**了仓库里的哪些文件。

只在 `tools/select_tests/select_tests.py` 自己拉起的 pytest、或 CI 的漂移检查里用 `-p audit_plugin` 加载，
平时的测试会话不加载它（零开销、零行为改变）。

    PYTHONPATH=tools/select_tests SELECT_TESTS_OUT=/tmp/dump.json pytest -p audit_plugin tests/...

**做法**：`sys.addaudithook` 钩 `open` 事件，按「正在跑哪个测试文件」归属（收集期 import 测试模块时的模块级读也算它的）。
只记三类被读的文件，且都只认**被跟踪**的（`git ls-files`；`mcp-servers/` 里的第三方代码、tmp、数据根都不是源码）：

* `other_test`：别的 `tests/test_*.py`（元守卫逐个读测试文件）；
* `nonpy`：非代码文件（CHANGELOG / 编排器脚本 / experiments 文档……）；
* `prod_py`：生产 `.py`（第二步「改生产代码」要用，第一步不读）。

**盲区（写在这里，别让人以为它全知）**：
1. 子进程里的读（测试起 `ruff` / CLI）钩子看不到——`test_no_undefined_names` 靠 `select_tests.EXTRA_ALWAYS` 手工补；
2. C 层直接读（sqlite 等）不触发 Python 的 `open` 事件；
3. import 机制读 `.py`（栈里有 `<frozen importlib…>`）被过滤——它是「依赖」不是「当数据读」，且 `sys.modules` 只让第一个导入者触发，
   本来也记不准；import 耦合由第二步用静态 import 反查补；
4. 归属是「当前跑的测试文件」：后台线程的读会算到当时正在跑的那个文件头上（只会多记，不会漏）。
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = os.path.realpath(os.environ.get("SELECT_TESTS_REPO") or Path(__file__).resolve().parents[2])
OUT = os.environ.get("SELECT_TESTS_OUT")
if not OUT:
    # 加载了插件却没说往哪写 ⇒ 这次测量必然是空的，当场报出来，不要跑完一整套才发现没有产物
    raise RuntimeError("audit_plugin 需要环境变量 SELECT_TESTS_OUT（dump 的输出路径）")

_SKIP_PARTS = ("/.git/", "/__pycache__/", "/.pytest_cache/")


def _tracked() -> set[str]:
    r = subprocess.run(["git", "-C", REPO, "ls-files", "-z"], capture_output=True, check=True)
    return {p for p in r.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


TRACKED = _tracked()
cur = {"who": "<session>"}
data: dict[str, dict[str, set[str]]] = collections.defaultdict(lambda: collections.defaultdict(set))
stats = collections.Counter()


def _norm(p):
    if isinstance(p, int) or p is None:
        return None
    try:
        p = os.fsdecode(os.fspath(p))
    except Exception:
        return None
    return p if os.path.isabs(p) else os.path.abspath(p)


def _via_importlib() -> bool:
    f = sys._getframe(2)
    while f is not None:
        if f.f_code.co_filename.startswith("<frozen importlib"):
            return True
        f = f.f_back
    return False


def _rel(ap: str):
    if not ap.startswith(REPO + "/"):
        return None
    rel = ap[len(REPO) + 1:]
    # 用相对路径判跳过：本仓可能就在 `.claude/worktrees/…` 里，绝对路径上判会把整个仓库当成「被跳过」
    if any(s in "/" + rel for s in _SKIP_PARTS) or rel.endswith(".pyc"):
        return None
    return rel


def _hook(event, args):
    if event != "open":
        return
    ap = _norm(args[0])
    if ap is None:
        return
    rel = _rel(ap)
    if rel is None:
        return
    who = cur["who"]
    if rel == who:
        return
    mode = args[1] if len(args) > 1 and isinstance(args[1], str) else "r"
    if rel.endswith(".py") and "b" in mode and _via_importlib():
        stats["import_open_skipped"] += 1
        return
    if rel.startswith("tests/"):
        name = rel[len("tests/"):]
        if "/" not in name and name.startswith("test_") and name.endswith(".py"):
            data[who]["other_test"].add(rel)
        return
    if rel not in TRACKED:
        return
    data[who]["prod_py" if rel.endswith(".py") else "nonpy"].add(rel)


sys.addaudithook(_hook)


def _file_of(path) -> str | None:
    ap = str(path)
    return ap[len(REPO) + 1:] if ap.startswith(REPO + "/") else None


def pytest_collectstart(collector):
    p = getattr(collector, "path", None)
    if p is not None and str(p).endswith(".py"):
        cur["who"] = _file_of(p) or str(p)


def pytest_collectreport(report):
    cur["who"] = "<session>"


def pytest_runtest_logstart(nodeid, location):
    cur["who"] = nodeid.split("::")[0]


def pytest_runtest_logfinish(nodeid, location):
    cur["who"] = "<session>"


def pytest_collection_finish(session):
    stats["collected_files"] = len({i.nodeid.split("::")[0] for i in session.items})


def pytest_sessionfinish(session, exitstatus):
    by_file = {w: {k: sorted(v) for k, v in d.items()} for w, d in data.items() if w != "<session>"}
    tmp = f"{OUT}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "repo": REPO, "n_collected_files": stats["collected_files"],
                   "by_file": by_file}, f, ensure_ascii=False)
    os.replace(tmp, OUT)
