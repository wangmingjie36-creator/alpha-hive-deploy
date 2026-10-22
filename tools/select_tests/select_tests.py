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
| `tests/test_*.py` | 读全部测试文件的元守卫 ∪ `EXTRA_ALWAYS` ∪ 被改文件自己 ∪ 读过它的「窄读者」∪ 静态 import 它的测试 | 0 |
| 被跟踪的非代码文件（CHANGELOG / 脚本 / 文档…） | 登记表里打开过它的测试（没人打开过 ⇒ 不选，并说明子进程里的读看不到） | 0 |
| `tests/_must_run.json`（登记表自己） | `tests/test_select_tests.py` | 0 |
| 核心文件（`conftest.py`、`tests/_*`、`config.py`、`hive_logger.py`、pytest 配置、`.github/`、`tools/clockshift|select_tests/`…） | **回退整套** | 10 |
| 生产 `.py`（第二步才做） / 认不出的 | **不猜，提示整套** | 3 |

任何一处读不了登记表（缺 / 坏 / 版本不对）⇒ 回退整套，绝不静默选少。

随跑测量（登记表怎么不过期）
──────────────────────────
`--run` 拉起的 pytest 自带 `audit_plugin`，顺带看被改的测试文件**现在**读了什么，与登记表对比：多出登记表没有的读 ⇒ 打印「登记表过期」
并以退出码 4 结束（测试本身红时退出码仍是 pytest 的）。`--update` 把这些读并进登记表（只并不删：偏保守）。
CI 在整套命令上加同一个插件，`--check-drift` 比对——**dump 里有登记表没有的读就红**（比登记表少不算：CI 上有的测试会 skip）。
`--build` 用整套的 dump 全量重建（按「读了 ≥ 一半测试文件」认「读全部」那一类）。

登记表不存「测试文件清单」：新增一个不读别的文件的测试，不需要改登记表（并行 session 各加测试时不会撞同一个文件）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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


def build_registry(dump: dict, commit: str = "") -> dict:
    """由「整套 + 审计插件」的 dump 全量重建。"""
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
        if len(others) >= READ_ALL_FRACTION * n:
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


def observed_gaps(reg: dict, dump: dict, files=None) -> list[str]:
    """dump 里观测到、登记表里没有的读（只看 `files`，缺省全部）。空 = 登记表覆盖了这次观测。
    只查「多出来的」：CI 上有些测试会 skip，观测到的比登记表少是正常的。"""
    n = int(dump.get("n_collected_files") or 0)
    out = []
    for f, kinds in sorted(dump.get("by_file", {}).items()):
        if files is not None and f not in files:
            continue
        if not (f.startswith("tests/test_") and f.endswith(".py")):
            continue
        reads_all, others, nonpy = _registry_view(reg, f)
        obs_others = set(kinds.get("other_test", ()))
        if not reads_all:
            if n and len(obs_others) >= READ_ALL_FRACTION * n:
                out.append(f"{f}：现在读了 {len(obs_others)} 个测试文件（≥ 一半），应归入 reads_all_tests（改任何测试文件都要跑它）")
            else:
                extra = sorted(obs_others - others)
                if extra:
                    out.append(f"{f}：读了登记表没有的测试文件 {extra[:5]}{' …' if len(extra) > 5 else ''}")
        extra_np = sorted(set(kinds.get("nonpy", ())) - nonpy)
        if extra_np:
            out.append(f"{f}：读了登记表没有的非代码文件 {extra_np[:5]}{' …' if len(extra_np) > 5 else ''}")
    return out


def merge_observation(reg: dict, dump: dict, files=None) -> dict:
    """把 dump 里（`files` 范围内）观测到的读**并进**登记表。只并不删——偏保守。"""
    n = int(dump.get("n_collected_files") or 0)
    out = json.loads(json.dumps(reg))
    for f, kinds in dump.get("by_file", {}).items():
        if files is not None and f not in files:
            continue
        if not (f.startswith("tests/test_") and f.endswith(".py")):
            continue
        others = set(kinds.get("other_test", ()))
        if f in out["reads_all_tests"]:
            pass
        elif n and len(others) >= READ_ALL_FRACTION * n:
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


def static_importers(test_texts: dict[str, str], module: str) -> set[str]:
    """哪些测试文件 `import <module>` / `from <module> import` / `from tests.<module> import`（模块名 = 被改测试文件的 stem）。"""
    pat = re.compile(rf"^\s*(?:from|import)\s+(?:tests\.)?{re.escape(module)}\b", re.M)
    return {p for p, t in test_texts.items() if pat.search(t)}


def make_plan(changes: list[tuple[str, str]], reg: dict, *, exists, test_texts: dict[str, str]) -> Plan:
    """纯函数：`changes` = [(状态, 路径)]，`exists(path)` 问工作区里还在不在，`test_texts` = {测试文件: 源码}（只用来找静态 import）。"""
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

    sel: set[str] = set()
    if tests:
        sel |= set(reg["reads_all_tests"]) | set(EXTRA_ALWAYS)
        plan.reasons.append(f"改了 {len(tests)} 个测试文件 ⇒ 读全部测试文件的 {len(reg['reads_all_tests'])} 个元守卫 + "
                            f"{', '.join(Path(p).name for p in EXTRA_ALWAYS)}（子进程读，手工登记）")
    for status, path in tests:
        if exists(path):
            sel.add(path)
            plan.changed_tests.append(path)
        readers = {r for r, read in reg["reads_tests"].items() if path in read}
        imps = static_importers(test_texts, Path(path).stem) - {path}
        if readers:
            plan.reasons.append(f"{path}：被 {', '.join(Path(r).name for r in sorted(readers))} 读过")
        if imps:
            plan.reasons.append(f"{path}：被 {', '.join(Path(r).name for r in sorted(imps))} import")
        sel |= readers | imps
    for _, path in nonpy:
        readers = set(reg["nonpy_readers"].get(path, ()))
        if readers:
            plan.reasons.append(f"{path}：被 {len(readers)} 个测试文件打开过")
        else:
            plan.notes.append(f"{path}：登记表里没有测试打开过它（子进程里的读看不到——收尾整套会兜住）")
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
    out = {}
    for p in sorted((REPO / "tests").glob("test_*.py")):
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
    reg = build_registry(dump, commit)
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
    gaps = observed_gaps(reg, dump)
    if gaps:
        print(f"[select_tests] ❌ 登记表过期：{len(gaps)} 处读没有被登记（修法：本地整套带插件跑一次后 --build，或 --update）")
        for g in gaps:
            print("  - " + g)
        return EXIT_STALE
    print(f"[select_tests] ✅ 登记表覆盖本次整套观测（{len(dump['by_file'])} 个测试文件有读记录）")
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
            gaps = observed_gaps(reg, d, files=set(plan.changed_tests) if plan.mode == "subset" else None)
        else:
            print("[select_tests] ⚠️ 审计插件没有产出 dump——这次没能随跑测量", file=sys.stderr)
    if gaps:
        print("[select_tests] ❌ 登记表过期（被改的测试读了登记表没有的文件）：")
        for g in gaps:
            print("  - " + g)
        print("  修法：/usr/local/bin/python3 tools/select_tests/select_tests.py --update，并把 tests/_must_run.json 一起提交")
    if rc != 0:
        return rc
    return EXIT_STALE if gaps else EXIT_OK


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
    new = merge_observation(reg, d, files=set(files))
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
