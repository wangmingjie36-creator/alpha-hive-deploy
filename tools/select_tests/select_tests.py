"""开发期选测：改了哪些文件 ⇒ 该跑哪些测试（第一档，v0.45.615）。**只是开发期快检，收尾 / CI 仍然整套。**

    /usr/local/bin/python3 tools/select_tests/select_tests.py              # 打印方案与命令（不跑）
    /usr/local/bin/python3 tools/select_tests/select_tests.py --run        # 按方案跑，并顺带测量（见下）
    /usr/local/bin/python3 tools/select_tests/select_tests.py --update     # 测量被改的测试文件并并入登记表
    /usr/local/bin/python3 tools/select_tests/select_tests.py --build DUMP # 由「整套 + 审计插件」的 dump 重建登记表
    /usr/local/bin/python3 tools/select_tests/select_tests.py --check-drift DUMP   # CI：dump 里有登记表没有的读 ⇒ 红

为什么不能「更新了哪些就跑哪些」一刀切（实测，auto-memory `alpha-hive-test-suite-speed`）
────────────────────────────────────────────────────────────────────────────────────────
本仓有一批「元守卫」逐个**读别的测试文件**（`test_tests_use_running_interpreter` 等），按「被测模块」圈测试永远圈不到它们；
v0.45.439、v0.45.592→596 都因此让 main 变红。所以选测不能按 import 图猜，要靠**实测**：用 `sys.addaudithook('open')` 整套跑一次，
记下每个测试文件打开过哪些文件，存成登记表 `tests/_must_run.json`。

第一档覆盖什么（其余一律不猜）
────────────────────────────
| 改动 | 选什么 | 退出码 |
|---|---|---|
| `tests/test_*.py` | 读全部测试文件的元守卫 ∪ `EXTRA_ALWAYS` ∪ 被改文件自己 ∪ 读过它的「窄读者」∪ 直接或间接 import 它的测试（AST，含经 `tests/_*` 帮手中转） | 0 |
| 被跟踪的非代码文件（CHANGELOG / 脚本 / 文档…） | 登记表里打开过它的测试 | 0 |
| 登记表里**没有读者**的非代码文件（新文件 / 漏登 / 只在子进程里读） | **回退整套**（v0.45.661 前是「不选」） | 10 |
| `tests/_must_run.json`（登记表自己） | `tests/test_select_tests.py` | 0 |
| 核心文件（`conftest.py`、`tests/_*`、`config.py`、`hive_logger.py`、pytest 配置、`.github/`、`tools/clockshift|select_tests/`…） | **回退整套** | 10 |
| 生产 `.py`（第二步才做） / 认不出的 | **不猜，提示整套** | 3 |

任何一处读不了登记表（缺 / 坏 / 版本不对）⇒ 回退整套，绝不静默选少。

随跑测量（登记表怎么不过期）
──────────────────────────
`--run` 拉起的 pytest 自带 `audit_plugin`，顺带看被改的测试文件**现在**读了什么，与登记表对比：多出登记表没有的测试读测试 ⇒ 打印
「登记表过期」并以退出码 4 结束（测试本身红时退出码仍是 pytest 的）；只多出非代码文件的读 ⇒ 只警告。`--update` 把这些读并进登记表（只并不删：偏保守）。
CI 在整套命令上加同一个插件，`--check-drift` 比对——**测试读测试的漏登就红**（含该归入读全部的元守卫）；**非代码文件漏登只警告**
（照样列出、给修法、在 Actions 里打 `::warning`，不挡 main——理由见 `Gap`，v0.45.661）。比登记表少不算：CI 上有的测试会 skip。
`--run` 的随跑测量用同一分级（`report_gaps`）。
`--build` 用整套的 dump 全量重建（按「读了 ≥ 一半测试文件」认「读全部」那一类；三处判定共用 `counts_as_read_all`，
分母是仓库测试文件总数——不是这一轮收集了几个，`--update` 只收集被改的文件，见 `counts_as_read_all`）。

登记表不存「测试文件清单」：新增一个不读别的文件的测试，不需要改登记表（并行 session 各加测试时不会撞同一个文件）。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[2]
REGISTRY_REL = "tests/_must_run.json"
REGISTRY_VERSION = 1
PLUGIN_DIR_REL = "tools/select_tests"

#: 靠子进程（ruff）读全仓测试文件的元守卫：`sys.addaudithook` 看不到子进程里的读，只能手工登记。
#: 改任何测试文件都跑它（`test_no_undefined_names` 用 ruff 扫全仓，F821 在新测试里也会红）。
EXTRA_ALWAYS = ("tests/test_no_undefined_names.py",)
#: 登记表自己的守卫测试（改登记表 ⇒ 只跑它）。
REGISTRY_GUARD_TEST = "tests/test_select_tests.py"
#: 读了 ≥ 这个比例的测试文件，就归入「读全部测试文件」那一类（实测：这一类读 372/373，其余最多读 3 个）。
#: 分母是**仓库里的测试文件总数**（`repo_test_count`），不是 dump 的 `n_collected_files`——见 `counts_as_read_all`。
READ_ALL_FRACTION = 0.5

#: 影响面不可圈的核心文件（整份回退整套）。
CORE_EXACT = frozenset({"config.py", "hive_logger.py", "conftest.py", "sitecustomize.py",
                        "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"})
CORE_PREFIXES = (".github/", "tools/clockshift/", PLUGIN_DIR_REL + "/", "requirements")

EXIT_OK, EXIT_USAGE, EXIT_UNCOVERED, EXIT_STALE, EXIT_FULL = 0, 2, 3, 4, 10


class RegistryError(Exception):
    """登记表缺失 / 损坏 / 版本不对。上层一律当成「不能选测」处理，不是「没有要选的」。"""


# ---------------------------------------------------------------- 登记表 -----

def empty_registry() -> dict:
    return {"version": REGISTRY_VERSION, "commit": "", "reads_all_tests": [], "reads_tests": {}, "nonpy_readers": {}}


def load_registry(path: Path) -> dict:
    try:
        reg = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RegistryError(f"登记表不存在：{path}（用 --build 由整套的 dump 生成）") from e
    except (OSError, ValueError) as e:
        raise RegistryError(f"登记表读不了 / 不是合法 JSON：{path}：{e}") from e
    if not isinstance(reg, dict) or reg.get("version") != REGISTRY_VERSION:
        raise RegistryError(f"登记表版本不对（要 {REGISTRY_VERSION}）：{path}")
    for k, t in (("reads_all_tests", list), ("reads_tests", dict), ("nonpy_readers", dict)):
        if not isinstance(reg.get(k), t):
            raise RegistryError(f"登记表字段 {k} 缺失或类型不对：{path}")
    return reg


def _list_block(name: str, items) -> str:
    if not items:
        return f'  "{name}": []'
    rows = ",\n".join(f"    {json.dumps(x, ensure_ascii=False)}" for x in sorted(items))
    return f'  "{name}": [\n{rows}\n  ]'


def save_registry(path: Path, reg: dict) -> None:
    """每个键 / 每个列表元素一行、排序：并行 session 各自改不同的读者时，git 能按行合并。"""
    def block(name: str, mapping: dict) -> str:
        if not mapping:
            return f'  "{name}": {{}}'
        rows = ",\n".join(f"    {json.dumps(k, ensure_ascii=False)}: {json.dumps(sorted(v), ensure_ascii=False)}"
                          for k, v in sorted(mapping.items()))
        return f'  "{name}": {{\n{rows}\n  }}'
    text = ("{\n"
            f'  "version": {reg["version"]},\n'
            f'  "commit": {json.dumps(reg.get("commit", ""))},\n'
            + _list_block("reads_all_tests", reg["reads_all_tests"]) + ",\n"
            + block("reads_tests", reg["reads_tests"]) + ",\n"
            + block("nonpy_readers", reg["nonpy_readers"]) + "\n}\n")
    json.loads(text)                                   # 写之前自检：写出去的必须是合法 JSON
    tmp = Path(f"{path}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def repo_test_count(repo: Path = REPO) -> int:
    """仓库里的测试文件总数（`tests/test_*.py`，与审计插件认「别的测试文件」同一个范围）——「读全部测试文件」的分母。"""
    return sum(1 for _ in (Path(repo) / "tests").glob("test_*.py"))


def counts_as_read_all(n_read: int, n_tests: int) -> bool:
    """读了 `n_read` 个别的测试文件，算不算「读全部测试文件」的元守卫。`n_tests` = 仓库测试文件总数（`repo_test_count`）。

    **分母不能取 dump 的 `n_collected_files`**（v0.45.619）：`--update` / `--run` 只收集被改 / 被选的几个文件，
    那个数小到 1，只读了一个别的测试文件的测试（`test_alphabot_chat` 读 `test_alphabot`）就被当成元守卫、
    挪出 `reads_tests`。被读的文件是从磁盘上读的，与这一轮收集了谁无关，所以分母也只能是磁盘上有多少。
    分母 ≤ 0 只可能是仓库路径错了：抛，不当成「谁都不算读全部」（那会把真元守卫降成窄读者，改新测试时漏选它）。"""
    if n_tests <= 0:
        raise ValueError(f"测试文件总数 = {n_tests}——分母不对（仓库路径错了？），不能判「读全部测试文件」")
    return n_read >= READ_ALL_FRACTION * n_tests


def build_registry(dump: dict, commit: str = "", *, n_tests: int) -> dict:
    """由「整套 + 审计插件」的 dump 全量重建。`n_tests` = 仓库测试文件总数（`repo_test_count`）。"""
    by_file = dump.get("by_file", {})
    n = int(dump.get("n_collected_files") or 0)
    if n <= 0 or not by_file:
        raise ValueError("dump 里没有收集到测试文件 / 没有任何读记录——这不是一次有效的整套测量")
    reg = empty_registry()
    reg["commit"] = commit
    for f, kinds in sorted(by_file.items()):
        if not (f.startswith("tests/test_") and f.endswith(".py")):
            continue
        others = kinds.get("other_test", [])
        if counts_as_read_all(len(others), n_tests):
            reg["reads_all_tests"].append(f)
        elif others:
            reg["reads_tests"][f] = sorted(others)
        for p in kinds.get("nonpy", []):
            reg["nonpy_readers"].setdefault(p, []).append(f)
    reg["reads_all_tests"].sort()
    reg["nonpy_readers"] = {p: sorted(v) for p, v in reg["nonpy_readers"].items()}
    return reg


def _registry_view(reg: dict, f: str):
    """登记表对某个测试文件的记录：(读全部?, 它读的测试文件集合, 它读的非代码文件集合)。"""
    reads_all = f in reg["reads_all_tests"]
    others = set(reg["reads_tests"].get(f, ()))
    nonpy = {p for p, rs in reg["nonpy_readers"].items() if f in rs}
    return reads_all, others, nonpy


class Gap(NamedTuple):
    """一处漏登。`kind`：`reads_all`（该归入读全部测试文件）/ `other_test`（读了登记表没有的测试文件）/ `nonpy`（非代码文件）。

    **分级（v0.45.661）**：测试读测试的漏登**报红**——漏的是元守卫 / 窄读者，正是 v0.45.439、v0.45.592 那类「只跑相关测试、
    没跑元守卫，main 变红」的事故，且新元守卫少见、不造噪声。非代码文件漏登**只警告**：它几乎都是「加了被现有测试按目录通读的
    静态文件 / 新测试读文件、忘了 `--update`」，10-22 → 10-27 让 main 的 `tests` 有 48% 的红是它、真失败夹在其中；
    而 `make_plan` 对**没有登记读者**的非代码文件回退整套，漏登不会让新文件漏跑。剩下的小缺口：已有读者的文件又多了新读者
    （新测试读 `hedge.js`），只改它时少选那个新读者——收尾 / CI 整套兜底。"""
    kind: str
    text: str

    @property
    def blocking(self) -> bool:
        return self.kind != "nonpy"


def report_gaps(gaps: list, *, hint: str) -> bool:
    """打印漏登（`--check-drift` 与 `--run` 共用，本地与 CI 同一口径）；返回有没有要报红的。
    非代码文件漏登照样逐条列出、给修法，在 GitHub Actions 里另打 `::warning` 注记（出现在运行摘要里，不挡 main）。"""
    blocking = [g for g in gaps if g.blocking]
    advisory = [g for g in gaps if not g.blocking]
    if blocking:
        print(f"[select_tests] ❌ 登记表过期：{len(blocking)} 处测试读测试没有被登记（{hint}）")
        for g in blocking:
            print("  - " + g.text)
    if advisory:
        print(f"[select_tests] ⚠️ 非代码文件漏登 {len(advisory)} 处——只警告、不报红"
              f"（没有登记读者的非代码文件改动时选测回退整套；{hint}）")
        for g in advisory:
            print("  - " + g.text)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            for g in advisory:
                msg = g.text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                print(f"::warning title=选测登记表：非代码文件漏登::{msg}")
    return bool(blocking)


def observed_gaps(reg: dict, dump: dict, files=None, *, n_tests: int) -> list[Gap]:
    """dump 里观测到、登记表里没有的读（只看 `files`，缺省全部）。空 = 登记表覆盖了这次观测。
    只查「多出来的」：CI 上有些测试会 skip，观测到的比登记表少是正常的。`n_tests` 同 `counts_as_read_all`。
    报不报红由 `Gap.blocking` 定（见 `Gap`）。"""
    out = []
    for f, kinds in sorted(dump.get("by_file", {}).items()):
        if files is not None and f not in files:
            continue
        if not (f.startswith("tests/test_") and f.endswith(".py")):
            continue
        reads_all, others, nonpy = _registry_view(reg, f)
        obs_others = set(kinds.get("other_test", ()))
        if not reads_all:
            if counts_as_read_all(len(obs_others), n_tests):
                out.append(Gap("reads_all", f"{f}：现在读了 {len(obs_others)} 个测试文件（≥ 一半），"
                                            "应归入 reads_all_tests（改任何测试文件都要跑它）"))
            else:
                extra = sorted(obs_others - others)
                if extra:
                    out.append(Gap("other_test", f"{f}：读了登记表没有的测试文件 {extra[:5]}{' …' if len(extra) > 5 else ''}"))
        extra_np = sorted(set(kinds.get("nonpy", ())) - nonpy)
        if extra_np:
            out.append(Gap("nonpy", f"{f}：读了登记表没有的非代码文件 {extra_np[:5]}{' …' if len(extra_np) > 5 else ''}"))
    return out


def merge_observation(reg: dict, dump: dict, files=None, *, n_tests: int) -> dict:
    """把 dump 里（`files` 范围内）观测到的读**并进**登记表。只并不删——偏保守。`n_tests` 同 `counts_as_read_all`。"""
    out = json.loads(json.dumps(reg))
    for f, kinds in dump.get("by_file", {}).items():
        if files is not None and f not in files:
            continue
        if not (f.startswith("tests/test_") and f.endswith(".py")):
            continue
        others = set(kinds.get("other_test", ()))
        if f in out["reads_all_tests"]:
            pass
        elif counts_as_read_all(len(others), n_tests):
            out["reads_all_tests"] = sorted(set(out["reads_all_tests"]) | {f})
            out["reads_tests"].pop(f, None)
        elif others:
            out["reads_tests"][f] = sorted(set(out["reads_tests"].get(f, ())) | others)
        for p in kinds.get("nonpy", ()):
            out["nonpy_readers"][p] = sorted(set(out["nonpy_readers"].get(p, ())) | {f})
    return out


# ---------------------------------------------------------------- 方案 -------

def classify(path: str) -> str:
    """test | registry | core | prod | nonpy。"""
    if path == REGISTRY_REL:
        return "registry"
    if path.startswith("tests/"):
        rest = path[len("tests/"):]
        return "test" if "/" not in rest and rest.startswith("test_") and rest.endswith(".py") else "core"
    if path in CORE_EXACT or any(path.startswith(p) for p in CORE_PREFIXES):
        return "core"
    if path.endswith(".py"):
        return "prod"
    return "nonpy"


@dataclass
class Plan:
    mode: str = "none"                  # none | subset | full | uncovered
    selected: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    changed_tests: list = field(default_factory=list)     # 被改（且还存在）的测试文件，用来随跑测量

    @property
    def exit_code(self) -> int:
        return {"full": EXIT_FULL, "uncovered": EXIT_UNCOVERED}.get(self.mode, EXIT_OK)


def _imported_tests_modules(text: str) -> set[str]:
    """一份源码 import 了 `tests/` 下哪些模块（模块名）：`import tests.X` / `import X` / `from tests.X import …` / `from X import …` /
    `from tests import X, Y` / 相对 import，函数体里的 import 也算（AST，注释与文档字符串里的字样不算）。
    语法错 ⇒ 按字样从宽认（出现过的 `test_*` / `_*` 名字都算）：多选不漏选，选上了它的语法错就在这次跑里直接红。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set(re.findall(r"\b(test_\w+|_\w+)\b", text))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                out.add(parts[1] if parts[0] == "tests" and len(parts) > 1 else parts[0])
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split(".") if node.module else []
            if parts[:1] == ["tests"] and parts[1:]:                   # from tests.X(.y) import …
                out.add(parts[1])
            elif parts == ["tests"] or (node.level and not parts):     # from tests import X / from . import X
                out.update(a.name for a in node.names)
            elif parts:                                                # from X(.y) import … / from .X(.y) import …
                out.add(parts[0])
    return out


def reverse_import_graph(test_texts: dict[str, str]) -> dict[str, set[str]]:
    """{`tests/<模块>.py`: 直接 import 它的文件}。`test_texts` 含 `tests/_*.py` 帮手（间接 import 常经它们中转）。"""
    rev: dict[str, set[str]] = {}
    for p, t in test_texts.items():
        for m in _imported_tests_modules(t):
            if m.startswith(("test_", "_")):
                rev.setdefault(f"tests/{m}.py", set()).add(p)
    return rev


def static_importers(rev: dict[str, set[str]], path: str) -> set[str]:
    """**直接或间接** import 了 `path` 的测试文件（只返回 `tests/test_*.py`，帮手只当中转点）。`rev` = `reverse_import_graph(...)`。

    v0.45.624 前只认直接 import 的两种正则写法，漏了 `from tests import X` 与间接 import（经别的测试模块、经 `tests/_*` 帮手）。
    那时审计插件的 import 过滤从不生效，测到的 import 读被登进登记表，碰巧替它补上了 5 条（逐条见 CHANGELOG 0.45.624）；
    过滤修好后登记表不再记 import 读，这里必须自己认全。⚠️ 别在本文件里写具体测试模块的名字当例子：有守卫按字样扫全部非测试源码。"""
    seen: set[str] = set()
    todo = [path]
    while todo:
        for imp in rev.get(todo.pop(), ()):
            if imp not in seen:
                seen.add(imp)
                todo.append(imp)
    return {p for p in seen if Path(p).name.startswith("test_") and p != path}


def make_plan(changes: list[tuple[str, str]], reg: dict, *, exists, test_texts: dict[str, str]) -> Plan:
    """纯函数：`changes` = [(状态, 路径)]，`exists(path)` 问工作区里还在不在，
    `test_texts` = {tests/ 下的测试文件与 `_*.py` 帮手: 源码}（只用来建 import 图，见 `static_importers`）。"""
    plan = Plan()
    core, prod, tests, nonpy, registry = [], [], [], [], []
    for status, path in changes:
        kind = classify(path)
        {"core": core, "prod": prod, "test": tests, "nonpy": nonpy, "registry": registry}[kind].append((status, path))
    if core:
        plan.mode = "full"
        plan.reasons = [f"{p}：核心文件，影响面圈不出来 ⇒ 整套" for _, p in core]
        return plan
    if prod:
        plan.mode = "uncovered"
        plan.reasons = [f"{p}：生产代码——第一档不覆盖（第二步才做），不猜 ⇒ 整套" for _, p in prod]
        return plan
    # 没有登记读者的非代码文件：读者未知（新文件还没测过 / 漏登 / 只在子进程里读）⇒ 整套，不再「什么都不选」。
    # v0.45.661：CI 对非代码漏登改成只警告，靠的就是这一条——新文件漏登了也不会漏跑读它的测试。
    unknown = [p for _, p in nonpy if not reg["nonpy_readers"].get(p)]
    if unknown:
        plan.mode = "full"
        plan.reasons = [f"{p}：登记表里没有测试读过它——读者未知（新文件 / 漏登 / 只在子进程里读）⇒ 整套" for p in unknown]
        return plan

    sel: set[str] = set()
    rev = reverse_import_graph(test_texts) if tests else {}
    if tests:
        sel |= set(reg["reads_all_tests"]) | set(EXTRA_ALWAYS)
        plan.reasons.append(f"改了 {len(tests)} 个测试文件 ⇒ 读全部测试文件的 {len(reg['reads_all_tests'])} 个元守卫 + "
                            f"{', '.join(Path(p).name for p in EXTRA_ALWAYS)}（子进程读，手工登记）")
    for status, path in tests:
        if exists(path):
            sel.add(path)
            plan.changed_tests.append(path)
        readers = {r for r, read in reg["reads_tests"].items() if path in read}
        imps = static_importers(rev, path)
        if readers:
            plan.reasons.append(f"{path}：被 {', '.join(Path(r).name for r in sorted(readers))} 读过")
        if imps:
            plan.reasons.append(f"{path}：被 {', '.join(Path(r).name for r in sorted(imps))} import（含间接）")
        sel |= readers | imps
    for _, path in nonpy:
        readers = set(reg["nonpy_readers"][path])            # 没有读者的已在上面回退整套
        plan.reasons.append(f"{path}：被 {len(readers)} 个测试文件打开过")
        sel |= readers
    if registry:
        sel.add(REGISTRY_GUARD_TEST)
        plan.reasons.append(f"{REGISTRY_REL}：登记表自己 ⇒ 它的守卫测试 {REGISTRY_GUARD_TEST}")
    gone = sorted(p for p in sel if not exists(p))
    if gone:
        plan.notes.append(f"登记表里引用了已不存在的测试文件（已跳过）：{gone[:5]}")
        sel -= set(gone)
    plan.selected = sorted(sel)
    plan.mode = "subset" if plan.selected else "none"
    return plan


# ---------------------------------------------------------------- git / 跑 ----

def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败（rc={r.returncode}）：{r.stderr.strip()}")
    return r.stdout


def git_changes(base: str) -> list[tuple[str, str]]:
    """自 `merge-base(base, HEAD)` 起的全部改动：已提交 + 已暂存 + 未暂存 + 未跟踪。重命名拆成「删旧 + 增新」。"""
    mb = git("merge-base", base, "HEAD").strip()
    changes: dict[str, str] = {}
    for line in git("diff", "--name-status", "-M", mb).splitlines():
        parts = line.split("\t")
        code = parts[0][0]
        if code in "RC":
            changes[parts[1]] = "D"
            changes[parts[2]] = "A"
        else:
            changes[parts[1]] = code
    for p in git("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if p:
            changes.setdefault(p, "A")
    return sorted((s, p) for p, s in changes.items())


def pytest_cmd(files: list[str], extra: list[str] | None = None) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
            "-m", "not integration", "--timeout=60", *(extra or []), *files]


def child_env(dump_path: str | None = None) -> dict:
    env = dict(os.environ)
    env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")
    if dump_path:
        env["SELECT_TESTS_OUT"] = dump_path
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO / PLUGIN_DIR_REL), env.get("PYTHONPATH", "")]))
    return env


def read_test_texts() -> dict[str, str]:
    """tests/ 下测试文件与 `_*.py` 帮手的源码——只用来建 import 图（帮手是间接 import 的中转点，自己不会被选中）。"""
    out = {}
    tests_dir = REPO / "tests"
    for p in sorted([*tests_dir.glob("test_*.py"), *tests_dir.glob("_*.py")]):
        try:
            out[f"tests/{p.name}"] = p.read_text(encoding="utf-8")
        except OSError:
            pass
    return out


DATE_LITERAL = re.compile(r"\b20\d\d-\d\d-\d\d\b")


def print_plan(plan: Plan, files_cmd: list[str]) -> None:
    label = {"none": "没有要选的测试", "subset": f"选测：{len(plan.selected)} 个测试文件",
             "full": "回退整套", "uncovered": "第一档不覆盖，请跑整套"}[plan.mode]
    print(f"[select_tests] {label}")
    for r in plan.reasons:
        print(f"  - {r}")
    for n in plan.notes:
        print(f"  ⚠️ {n}")
    if plan.mode == "subset":
        print("  命令：" + shlex.join(files_cmd))
    elif plan.mode in ("full", "uncovered"):
        print("  命令：" + shlex.join(pytest_cmd(["tests/"])))
    print("  （开发期快检；收尾 / CI 仍整套。）")


# ---------------------------------------------------------------- 命令 -------

def registry_path(args) -> Path:
    """登记表路径：`--registry` 优先，缺省 `tests/_must_run.json`（所有读写登记表的命令都从这里取）。"""
    return Path(args.registry) if args.registry else REPO / REGISTRY_REL


def cmd_build(args) -> int:
    dump = json.loads(Path(args.build).read_text(encoding="utf-8"))
    commit = git("rev-parse", "HEAD", check=False).strip()
    reg = build_registry(dump, commit, n_tests=repo_test_count())
    save_registry(registry_path(args), reg)
    print(f"[select_tests] 登记表已重建：{len(reg['reads_all_tests'])} 个读全部测试文件的元守卫、"
          f"{len(reg['reads_tests'])} 个窄读者、{len(reg['nonpy_readers'])} 个被读的非代码文件 → {REGISTRY_REL}")
    return EXIT_OK


def cmd_check_drift(args) -> int:
    reg = load_registry(registry_path(args))
    dump = json.loads(Path(args.check_drift).read_text(encoding="utf-8"))
    if not dump.get("by_file"):
        print("[select_tests] ❌ dump 是空的——测量没生效，不能当「没有漂移」", file=sys.stderr)
        return EXIT_USAGE
    gaps = observed_gaps(reg, dump, n_tests=repo_test_count())
    if report_gaps(gaps, hint="修法：本地整套带插件跑一次后 --build，或 --update"):
        return EXIT_STALE
    print(f"[select_tests] ✅ 测试读测试的登记覆盖本次整套观测（{len(dump['by_file'])} 个测试文件有读记录）")
    return EXIT_OK


def resolve_changes(args) -> list[tuple[str, str]]:
    if args.files:
        return [("M", f[2:] if f.startswith("./") else f) for f in args.files]
    return git_changes(args.base)


def cmd_select(args) -> int:
    changes = resolve_changes(args)
    if not changes:
        print("[select_tests] 没有改动，无事可选。")
        return EXIT_OK
    try:
        reg = load_registry(registry_path(args))
    except RegistryError as e:
        print(f"[select_tests] ⚠️ {e}\n  ⇒ 不能选测，回退整套：" + shlex.join(pytest_cmd(["tests/"])))
        return EXIT_FULL
    plan = make_plan(changes, reg, exists=lambda p: (REPO / p).exists(), test_texts=read_test_texts())
    changed_dates = [p for p in plan.changed_tests if DATE_LITERAL.search((REPO / p).read_text(encoding="utf-8", errors="replace"))]
    if changed_dates:
        plan.notes.append("被改的测试里有日期字面量 ⇒ 另跑拨钟审计：/usr/local/bin/python3 tools/clockshift/audit.py --days 7 --days 45 -- "
                          + " ".join(changed_dates))
    cmd = pytest_cmd(plan.selected)
    print_plan(plan, cmd)
    if not args.run:
        return plan.exit_code
    if plan.mode == "none":
        return EXIT_OK
    with tempfile.TemporaryDirectory(prefix="select_tests_") as td:
        dump = os.path.join(td, "dump.json")
        run_cmd = pytest_cmd(plan.selected if plan.mode == "subset" else ["tests/"], ["-p", "audit_plugin"])
        rc = subprocess.run(run_cmd, cwd=REPO, env=child_env(dump)).returncode
        gaps = []
        if os.path.exists(dump):
            d = json.loads(Path(dump).read_text(encoding="utf-8"))
            gaps = observed_gaps(reg, d, files=set(plan.changed_tests) if plan.mode == "subset" else None,
                                 n_tests=repo_test_count())
        else:
            print("[select_tests] ⚠️ 审计插件没有产出 dump——这次没能随跑测量", file=sys.stderr)
    stale = report_gaps(gaps, hint="修法：/usr/local/bin/python3 tools/select_tests/select_tests.py --update，"
                                   "并把 tests/_must_run.json 一起提交")
    if rc != 0:
        return rc
    return EXIT_STALE if stale else EXIT_OK


def cmd_update(args) -> int:
    changes = resolve_changes(args)
    files = sorted(p for s, p in changes if classify(p) == "test" and s != "D" and (REPO / p).exists())
    if not files:
        print("[select_tests] 没有被改的测试文件，无需测量。")
        return EXIT_OK
    reg = load_registry(registry_path(args))
    with tempfile.TemporaryDirectory(prefix="select_tests_") as td:
        dump = os.path.join(td, "dump.json")
        rc = subprocess.run(pytest_cmd(files, ["-p", "audit_plugin"]), cwd=REPO, env=child_env(dump)).returncode
        if rc != 0:
            print(f"[select_tests] ❌ 被改的测试没跑绿（rc={rc}），不并入登记表", file=sys.stderr)
            return rc
        d = json.loads(Path(dump).read_text(encoding="utf-8"))
    new = merge_observation(reg, d, files=set(files), n_tests=repo_test_count())
    new["commit"] = git("rev-parse", "HEAD", check=False).strip()
    save_registry(registry_path(args), new)
    print(f"[select_tests] 已测量 {len(files)} 个测试文件并并入 {REGISTRY_REL}（只并不删）。请把它一起提交。")
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="开发期选测（第一档）。收尾 / CI 仍整套。")
    ap.add_argument("--base", default="origin/main", help="改动的比较基线（缺省 origin/main）")
    ap.add_argument("--files", nargs="*", help="直接给改动文件（当作 M），不读 git——回放 / 调试用")
    ap.add_argument("--registry", metavar="PATH", help="登记表路径（缺省 tests/_must_run.json；测试用）")
    ap.add_argument("--run", action="store_true", help="按方案执行（缺省只打印）")
    ap.add_argument("--update", action="store_true", help="测量被改的测试文件并并入登记表")
    ap.add_argument("--build", metavar="DUMP", help="由整套 + 审计插件的 dump 重建登记表")
    ap.add_argument("--check-drift", metavar="DUMP", help="CI：dump 里有登记表没有的读 ⇒ 退出码 4")
    args = ap.parse_args(argv)
    try:
        if args.build:
            return cmd_build(args)
        if args.check_drift:
            return cmd_check_drift(args)
        if args.update:
            return cmd_update(args)
        return cmd_select(args)
    except RegistryError as e:
        print(f"[select_tests] ❌ {e}", file=sys.stderr)
        return EXIT_USAGE
    except (RuntimeError, ValueError, OSError) as e:
        print(f"[select_tests] ❌ {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
