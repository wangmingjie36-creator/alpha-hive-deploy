#!/usr/bin/env python3
"""CHANGELOG 完整性 + 步骤输出契约的 git hook（v0.45.216；契约闸 v0.45.355）

在**提交**与**推送到 main** 之前跑 `tests/test_changelog_entry_integrity.py`
（冲突标记 / 重号 / 空标题）。只挡「动了 CHANGELOG.md」的那些，其余一律放行。
推送到 main 时另有一道**契约闸**（见文末「契约闸」一节），与 CHANGELOG 部分互不影响。

    /usr/local/bin/python3 changelog_guard.py --install-hook   # 装 / 重装两个钩子（幂等）

为什么要它
----------
那个测试一直在全套里，但全套只在 CI 里跑，而 CI 在推上 main **之后**才跑。
`4b5e4d9` 是一份含冲突标记、真被推上 main 的 CHANGELOG（存活约 3 分钟）；
2026-09-13 v0.45.211 收尾时同一形状又来一次：解冲突脚本的自检被散文误伤、正确地失败了，
但后面的 `git add` / `rebase --continue` 接在 `;` 上照跑 ⇒ 带标记的 CHANGELOG 被提交，
是推之前手动跑这个测试才抓到的。**「记得跑」不是护栏。**

为什么是两个钩子（实测，git 2.x，本机）
--------------------------------------
| 操作 | 触发 pre-commit？ |
|---|---|
| `git commit` / `commit -a` / `commit -- <路径>` | 是 |
| merge 冲突解完后 `git commit` | 是 |
| `git cherry-pick --continue` | 是 |
| linked worktree 里提交 | 是 |
| **`git rebase --continue`** | **否**（只触发 post-rewrite） |

v0.45.211 那次走的恰恰是最后一行 ⇒ **只装 pre-commit 接不住它**。
pre-push 与提交是怎么造出来的无关（rebase、`--no-verify` 都绕不过），所以它是兜底；
pre-commit 只是更早报错。

拦 / 放的分界
-------------
* **pre-commit**：本次提交**动了** CHANGELOG.md 才跑测试；没动（例如每日日报的白名单提交）
  直接放行、不跑。例行拦截无关提交会把人养成 `--no-verify` 的习惯，那比没有护栏更糟
  （同 memory 库索引行守卫的判据）。
* **pre-push**：只看推往远端 `refs/heads/main` 的更新，且**被推的区间动了** CHANGELOG.md
  才跑。推别的分支、推 gh-pages、每日日报推送（区间不含 CHANGELOG）都不跑。
  远端那个提交本地没有时，区间起点退回上次 fetch 的 `<remote>/main`（理由见 `_range_base`：
  生产推送 v0.45.214 的抢推竞态）；连它也没有才按「动了」处理——证明不了没动。

⚠️ 测试读的是**工作区**里的 CHANGELOG.md，而要守的是**提交 / 推送里的**那一份
----------------------------------------------------------------------------
两者不一致时测试结果不代表要守的内容，所以**直接拒绝**，不拿工作区的结果冒充：
* pre-commit：暂存版本 ≠ 工作区（部分暂存）⇒ 拒。
* pre-push：被推提交里的 CHANGELOG.md ≠ 工作区 ⇒ 拒。
最危险的正是「暂存 / 被推的那份坏了、工作区那份是好的」——不拒就会测好的、放坏的。

跑测试前清掉 git 注入的定位变量
------------------------------
钩子运行时 git 会设 `GIT_INDEX_FILE`（实测每次 pre-commit 都有），linked worktree 里还有
`GIT_DIR`。原样传给 pytest，测试里对**临时仓库**跑的 git 命令会打到**真仓库**的索引上。

`.git/hooks` 里只放薄调用，逻辑全在本文件
----------------------------------------
钩子正文的唯一真相是下面的 `_SHIM` 模板，别手改 `.git/hooks/pre-commit` / `pre-push`
（会被下次 `--install-hook` 覆盖）。`.git/hooks` 不被跟踪，重新 clone 后要重装，**且没人会知道**。

⚠️ 本仓的 `.git/hooks` 是**主 checkout、生产 checkout 与所有 worktree 共用**的。
所以薄调用找不到本文件时（该 checkout 停在 v0.45.216 之前的提交上，例如尚未同步的
生产 checkout 本地 main）**只提醒、不拦**——拦了会误伤并发 session 与每日日报。
这不留实质缺口：会造出冲突标记的 merge / rebase 都发生在合入 main 之后，那时本文件已在工作区里。
解释器不可用则一律拦（那是环境坏了，不是版本旧）。

绕过：`git commit --no-verify` / `git push --no-verify`。只在确认 CHANGELOG 无误、
且本守卫本身出了问题时用，并把问题记下来。

契约闸（v0.45.355，只在 pre-push、只看推往 `refs/heads/main` 的更新）
--------------------------------------------------------------------
守的是编排器各步骤工具的输出契约（`step_contract.py`：`--out` 外壳、退出码 3 = 崩溃）。
为什么放在钩子里而不是只靠 CI：main 的 CI 已经连续红了很久（最近 30 次 0 次成功），
一条只在 CI 里跑的契约测试红了也不会有人看见 —— 那等于没有。

* **跑什么**：被推提交里匹配 `CONTRACT_TEST_GLOBS` 的文件（glob ⇒ 新增
  `tests/test_step_contract_<x>.py` 自动入闸，不用改本文件）∪ `CONTRACT_REQUIRED_TESTS`。
  **一个都没有 ⇒ 拦**：「没东西可测」不是「测过了」。不跑全套。
* **必选下限** `CONTRACT_REQUIRED_TESTS`：glob 只会「多选」，不会发现「少了」——删掉两个契约文件、
  剩下的照跑照绿（v0.45.355 复核实测 rc 0）。必选文件缺一个（被删、或被 export-ignore 挡在导出树外）
  ⇒ 拦，且不跑 pytest。别的检查要入闸，往这个常量**追加**路径即可（登记即入闸，不必匹配 glob）。
* **在哪跑**：`git archive <被推提交>` 导出到临时目录、在那里跑。CHANGELOG 部分用「与工作区
  不一致就拒」解决「测的不是推的」，这里做不到 —— 契约测试要 import 整个代码树，工作区里
  未跟踪的 `.py`（iCloud 的「xxx 2.py」副本、还没 `git add` 的新模块）会让测试在本地绿、
  推上去的提交里却缺文件。导出后测的就是推的，与工作区干不干净无关。实测导出 ~1s。
  ⚠️ 「导出树 = 被推提交」**要核对，不能假设**：`git archive` 认 `.gitattributes`（及
  `$GIT_DIR/info/attributes`）的 `export-ignore`，被标记的文件不进导出树 ⇒ glob 少选一个契约文件、
  或 conftest / 代码缺一块，闸照样绿（复核实测：export-ignore 两个契约文件 + 改一行 `.py` ⇒ rc 0）。
  所以导出后逐个核对被推提交的每个 blob 都在导出树里（`_export_gaps`），缺了 ⇒ 拦。
* **什么时候跑**：被推区间动了 `CONTRACT_TRIGGER_PATHSPECS`（代码 / 测试 / scripts/ / pytest 配置 /
  `.gitattributes`）才跑；算不出区间（远端新建分支、直推 URL）按「动了」处理。
  `.gitattributes` 在内是因为只改它就能改变导出树（见上），而它不是代码。**只动数据的推送不跑** ——
  每日日报推送（`production_sync.push_main`，无人值守）只含报告产物，它若被拦，网站就停更，
  而拦下它什么也修不好：区间没动代码 ⇒ 契约测试的结果与已被守过的 base 相同。
  这条「日报产物永不触发」由测试对着 `report_deployer` 的白名单常量逐条核对，会红。
* **什么算过**：pytest 退出码 0 **且**结果报告（本闸自带的小插件写）里没有失败、没有
  收集错误、**没有 skip（含整文件的 `pytest.skip(allow_module_level=True)` / `importorskip`——
  它们发生在收集期，pytest 退出码照样是 0）、没有被 `-m` 摘掉的**（契约测试带 `network`/`integration`
  标记 ⇒ 离线闸里跑不到 ⇒ 等于没有；离线本身由 `tests/conftest.py::_offline_transport` 在传输层执行），
  **且每个被选中的文件至少 1 条通过**——空文件、被 conftest 的 `pytest_collection_modifyitems`
  悄悄删掉的测试，都不产生任何失败 / skip 记录，只能按文件数「通过」来抓。
* 子进程环境：清掉 git 定位变量（同上）、`PYTEST_ADDOPTS`（外部塞 `-k` 能静默摘掉测试）、
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD`（`pyproject` 的 `--timeout` 要 pytest-timeout 插件）、
  `PYTHONPATH` / `PYTHONHOME` / `PYTHONSTARTUP`（继承的 `PYTHONPATH` 若指向工作区，被推提交里
  缺的模块会从工作区 import 进来 ⇒ 「测的就是推的」落空；复核实测 rc 0）。
  `PYTHONPATH` 只放本闸的结果插件。
* **CHANGELOG 部分拦了也照跑**（含「CHANGELOG.md 与工作区不同」那条）：一次推送把两件事都报出来，
  省一轮「修一个、推、再被另一个拦」。
* 绕过：同上，`git push --no-verify`（会连 CHANGELOG 检查一起跳过）。只在确认契约无误、
  是本闸自身出了问题时用，并把问题记下来。

⚠️ 薄调用（`_SHIM`）**没有变**：已装的 pre-push 钩子就是 `exec python changelog_guard.py --pre-push`，
读的是当前 checkout 里的本文件 ⇒ 契约闸随本文件合入即生效，**不需要重装钩子**。
`tests/test_changelog_guard_contract_gate.py` 钉住了薄调用的内容：谁改了它，谁就得让所有
checkout 重跑 `--install-hook`（而已装的旧钩子不会告诉任何人它旧了）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHANGELOG = "CHANGELOG.md"
INTEGRITY_TEST = "tests/test_changelog_entry_integrity.py"
GUARDED_REMOTE_REF = "refs/heads/main"
HOOK_NAMES = ("pre-commit", "pre-push")

#: 契约闸选哪些测试（相对仓库根的 glob，在**被推提交的导出树**里求值）。
#: ⚠️ 本闸自己的测试 `tests/test_changelog_guard_contract_gate.py` 不能匹配这里——
#: 它会在临时仓库里推送、再触发本闸，放进来就是每次推送多跑 ~20s 的自我递归。
CONTRACT_TEST_GLOBS = ("tests/test_step_contract*.py", "tests/test_orchestrator_steps.py")

#: **必选**契约测试（相对仓库根的路径）：触发本闸的每个推往 main 的提交，其**导出树**里都必须有这些文件，
#: 且每个至少 1 条通过；缺一个（被删、或被 export-ignore 挡掉）⇒ 拦。glob 只会多选、发现不了「少了」，
#: 这里是只许变大的下限。
#: ⚠️ 要让别的检查入闸（例如编排器 `scripts/alpha-hive-orchestrator.sh` 的大括号变量测试），往这里
#: **追加**它的路径即可——登记即入闸，不必匹配 `CONTRACT_TEST_GLOBS`，也不用改别处。
#: 同样不许放本闸自己的测试（自我递归，见上）；`tests/test_changelog_guard_contract_gate.py` 钉着
#: 「至少含这四个」与「真仓库里都在」。
CONTRACT_REQUIRED_TESTS = (
    "tests/test_step_contract.py",
    "tests/test_step_contract_ic_rerun.py",
    "tests/test_step_contract_producers.py",
    "tests/test_orchestrator_steps.py",
)

#: 被推区间动了这些路径（git pathspec；无魔法时 `*` 跨目录）才跑契约闸。
#: 只收「改了就可能改变契约测试结果」的：代码、测试（含夹具数据）、scripts/（编排器）、pytest 配置、
#: 仓内的 SKILL.md（周度任务是契约的消费方，将来入仓后同样在守的范围里）、
#: `.gitattributes`（任何一层；它的 export-ignore 能让导出树缺文件——只改它不动代码也会改变闸的结果）。
#: ⚠️ 不许匹配日报产物（`report_deployer` 白名单）——无人值守的日报推送被拦 = 网站停更；有测试逐条核对。
CONTRACT_TRIGGER_PATHSPECS = ("*.py", "*.sh", "tests/", "scripts/", "*.toml", "*.ini", "*.cfg", "*SKILL.md",
                              ".gitattributes", "*/.gitattributes")

#: 整个 pytest 子进程的上限（单条测试另有 pyproject 的 `--timeout`）。超时 ⇒ 拦（未被检查 ≠ 通过）。
CONTRACT_TIMEOUT_S = 300

#: 契约闸 pytest 子进程要额外清掉的变量（理由见模块 docstring「契约闸」一节）。
_PYTEST_ENV_VARS = ("PYTEST_ADDOPTS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD")
#: 同上，解释器层面的：继承的 `PYTHONPATH` 指向工作区时，被推提交里缺的模块会从工作区 import 进来。
#: `PYTHONPATH` 由 `_contract_env` 重设为只含本闸的结果插件。
_PYTHON_ENV_VARS = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")

_BYPASS_NOTE = ("紧急情况下（仅限确认契约无误、是本闸自身出了问题）可 `git push --no-verify` 绕过——"
                "这会连 CHANGELOG 检查一起跳过；绕过后把问题记下来。")

#: 契约闸 pytest 的结果报告插件（写进临时目录、`-p` 加载）。不从输出文本里抠「FAILED …」——
#: pytest 改个输出格式，抠不到就会变成「没失败」；插件拿的是结构化的 report 对象。
#: 收集期的 skip（`pytest.skip(allow_module_level=True)` / `importorskip`）不产生任何 runtest 报告、
#: pytest 退出码照样是 0 ⇒ 只看 runtest 的 skipped 会把「整个文件没跑」当成通过，所以单独记。
#: `item_files`（节点 → 相对 cwd 的文件路径）供闸核对「每个被选中的文件至少 1 条通过」。
_REPORT_PLUGIN = '''\
import json, os
_OUT = os.environ["ALPHA_HIVE_CONTRACT_GATE_REPORT"]
_S = {"selected": [], "item_files": {}, "deselected": [], "passed": [], "failed": [], "skipped": [],
      "collect_errors": [], "collect_skipped": [], "exitstatus": None}
def _rel(path):
    return os.path.relpath(os.path.realpath(str(path)), os.path.realpath(os.getcwd())).replace(os.sep, "/")
def pytest_deselected(items):
    _S["deselected"].extend(i.nodeid for i in items)
def pytest_collection_finish(session):
    _S["selected"] = [i.nodeid for i in session.items]
    _S["item_files"] = {i.nodeid: _rel(i.path) for i in session.items}
def pytest_collectreport(report):
    if report.failed:
        _S["collect_errors"].append(report.nodeid or "<collection>")
    elif report.skipped:
        why = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        _S["collect_skipped"].append(f"{report.nodeid or '<collection>'}（{why}）")
def pytest_runtest_logreport(report):
    if report.failed:
        _S["failed"].append(f"{report.nodeid}（{report.when}）")
    elif report.skipped:
        _S["skipped"].append(report.nodeid)
    elif report.when == "call":
        _S["passed"].append(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    _S["exitstatus"] = int(exitstatus)
    with open(_OUT, "w", encoding="utf-8") as f:
        json.dump(_S, f, ensure_ascii=False)
'''

#: 钩子运行时 git 注入的「仓库在哪」类变量。只清这些——作者、编辑器等变量与定位无关。
_GIT_LOCATION_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
)

_SHIM_MARKER = "# changelog-guard shim"

_SHIM = """#!/bin/sh
{marker}（由 changelog_guard.py --install-hook 生成，勿手改：会被下次安装覆盖）
# 逻辑全在被跟踪的 changelog_guard.py 里；本文件只负责找到它。
PY="${{ALPHA_HIVE_HOOK_PYTHON:-/usr/local/bin/python3}}"
ROOT=$(git rev-parse --show-toplevel) || exit 1
SCRIPT="$ROOT/changelog_guard.py"
if [ ! -f "$SCRIPT" ]; then
    # 本 checkout 在 v0.45.216 之前的提交上。.git/hooks 是所有 worktree 与生产 checkout
    # 共用的，这里拦会误伤并发 session 与每日日报；合入 origin/main 后自然生效。
{missing}
    exit 0
fi
if [ ! -x "$PY" ]; then
    echo "{hook}: 解释器不可用：$PY —— CHANGELOG 完整性**未被检查**（这不是「检查通过」）" >&2
    exit 1
fi
exec "$PY" "$SCRIPT" --{hook} "$@"
"""

_MISSING_NOTE = {
    "pre-commit": (
        '    git diff --cached --quiet -- CHANGELOG.md || echo "pre-commit: ⚠️ 本 checkout 没有 '
        'changelog_guard.py，这次提交里的 CHANGELOG.md **未被检查**（合入 origin/main 后生效）" >&2'
    ),
    "pre-push": (
        '    echo "pre-push: ⚠️ 本 checkout 没有 changelog_guard.py，推送内容里的 CHANGELOG.md '
        '**未被检查**（合入 origin/main 后生效）" >&2'
    ),
}

HOOKS = {
    name: _SHIM.format(marker=_SHIM_MARKER, hook=name, missing=_MISSING_NOTE[name])
    for name in HOOK_NAMES
}


def _say(hook: str, msg: str) -> None:
    print(f"{hook}: {msg}", file=sys.stderr)


def _git(*args: str) -> subprocess.CompletedProcess:
    # 保留钩子环境：这里的 git 调用**要**看到 GIT_INDEX_FILE（`commit -- <路径>` 用的是临时索引）
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _differs(hook: str, *diff_args: str):
    """`git diff --quiet …` → True（有差异）/ False（无差异）/ None（git 出错，已出声）。"""
    r = _git("diff", "--quiet", *diff_args)
    if r.returncode in (0, 1):
        return r.returncode == 1
    _say(hook, f"git diff {' '.join(diff_args)} 出错（退出码 {r.returncode}）：{r.stderr.strip()}")
    return None


def _is_zero(sha: str) -> bool:
    return set(sha) == {"0"}


def _run_integrity_test(hook: str, root: Path) -> int:
    if not (root / INTEGRITY_TEST).is_file():
        _say(hook, f"缺 {INTEGRITY_TEST} —— CHANGELOG 完整性**未被检查**（这不是「检查通过」）")
        return 1
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")  # 子进程也走 3.11（CLAUDE.md 解释器硬规则）
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--maxfail=50", INTEGRITY_TEST],
        cwd=root, env=env, capture_output=True, text=True,
    )
    lines = (p.stdout + p.stderr).strip().splitlines()
    summary = lines[-1].strip("= ") if lines else "（pytest 无输出）"
    if p.returncode == 0:
        _say(hook, f"CHANGELOG 完整性测试通过（{summary}）")
        return 0
    print("\n".join(lines[-60:]), file=sys.stderr)
    _say(hook, f"❌ CHANGELOG 完整性测试未通过（pytest 退出码 {p.returncode}：{summary}）")
    _say(hook, f"修好 CHANGELOG.md 再{'提交' if hook == 'pre-commit' else '推送'}。")
    return 1


def pre_commit(root: Path) -> int:
    hook = "pre-commit"
    touched = _differs(hook, "--cached", "--", CHANGELOG)
    if touched is None:
        return 1
    if not touched:
        return 0
    unstaged = _differs(hook, "--", CHANGELOG)
    if unstaged is None:
        return 1
    if unstaged:
        _say(hook, "❌ CHANGELOG.md 的暂存版本与工作区不同。完整性测试读的是工作区，"
                   "结果不代表这次要提交的内容 —— 先 `git add CHANGELOG.md`（或撤掉未暂存的改动）再提交。")
        return 1
    return _run_integrity_test(hook, root)


def _range_base(remote: str, remote_sha: str) -> str:
    """被推区间的起点。远端那个提交本地不认识时，退而用上次 fetch 看到的 `<remote>/main`。

    不直接按「动了」处理，是因为生产推送（`production_sync.push_main`，v0.45.214）会撞上这种情况：
    它 fetch 后在对象层合并、再推 `<合并提交>:refs/heads/main`，**工作区停在扫描开始时**。若 fetch 与
    push 之间别的 session 抢推了 main，远端 sha 本地没有 ⇒ 按「动了」就会拿旧工作区比对合并提交里
    较新的 CHANGELOG ⇒ 报「与工作区不同」拒推。真实原因是 non-fast-forward，`push_main` 靠 origin
    又动了来重试，本钩子不该抢先报一个误导性的原因。退回 tracking ref 后该区间不含 CHANGELOG ⇒
    放行 ⇒ 由远端按非快进拒绝。两者都没有（直接推 URL、远端新建分支）时返回空串 ⇒ 按「动了」处理。
    """
    if not _is_zero(remote_sha) and _git("cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0:
        return remote_sha
    tracking = _git("rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/main^{{commit}}")
    return tracking.stdout.strip() if remote and tracking.returncode == 0 else ""


def _select_contract_tests(tree: Path) -> list:
    """`tree` 里匹配 `CONTRACT_TEST_GLOBS` 的测试文件 ∪ 存在的 `CONTRACT_REQUIRED_TESTS`（相对路径，去重、排序）。

    必选文件**缺了**不在这里报——由 `_run_contract_gate` 逐个核对并拦（这里只负责「选」）。"""
    found = set()
    for pattern in CONTRACT_TEST_GLOBS:
        found.update(p.relative_to(tree).as_posix() for p in tree.glob(pattern) if p.is_file())
    found.update(req for req in CONTRACT_REQUIRED_TESTS if (tree / req).is_file())
    return sorted(found)


def _contract_triggered(hook: str, base: str, local_sha: str):
    """被推区间是否动了 `CONTRACT_TRIGGER_PATHSPECS` → True / False / None（git 出错，已出声）。

    算不出区间（`base` 为空）按「动了」处理——证明不了没动（与 CHANGELOG 部分同一判据）。"""
    if not base:
        return True
    return _differs(hook, base, local_sha, "--", *CONTRACT_TRIGGER_PATHSPECS)


def _export_commit(hook: str, sha: str, dest: Path) -> bool:
    """`git archive <sha> | tar -x -C dest`。任何一端失败都出声并返回 False。

    ⚠️ `git archive` 认 `.gitattributes`（及 `$GIT_DIR/info/attributes`）的 `export-ignore`：被标记的文件
    **不进导出树，而本函数照样成功**。这不会自己变成「拦」——少一个契约文件 glob 就少选一个，
    剩下的照跑照绿（v0.45.355 复核实测 rc 0；此前这里写的「缺文件 ⇒ 拦，不会静默放行」是错的）。
    所以导出后必须再用 `_export_gaps` 核对导出树与被推提交逐文件一致，调用方负责。"""
    # git archive 的 stderr 进临时文件而不是管道：管道写满而没人读时它会卡住，tar 又在等它 ⇒ 互等
    with tempfile.TemporaryFile() as err:
        archive = subprocess.Popen(["git", "archive", "--format=tar", sha], stdout=subprocess.PIPE, stderr=err)
        untar = subprocess.run(["tar", "-x", "-f", "-", "-C", str(dest)], stdin=archive.stdout,
                               capture_output=True, text=True)
        archive.stdout.close()
        archive.wait()
        err.seek(0)
        archive_err = err.read().decode("utf-8", "replace").strip()
    if archive.returncode != 0 or untar.returncode != 0:
        _say(hook, f"❌ 导出被推提交 {sha[:10]} 失败（git archive 退出码 {archive.returncode}：{archive_err}；"
                   f"tar 退出码 {untar.returncode}：{untar.stderr.strip()}）—— 契约**未被检查**（这不是「检查通过」）")
        return False
    return True


def _export_gaps(hook: str, sha: str, tree: Path):
    """被推提交 `sha` 里有、导出树 `tree` 里却没有的文件（相对路径，排序）。git 出错 → None（已出声）。

    「在导出树里测 = 测被推的提交」要被执行，不能只是假设：export-ignore 会让两者不一致（见 `_export_commit`）。
    只核对 blob；子模块（gitlink）`git archive` 本来就不导出其内容，契约测试也不依赖。
    大小写不敏感的文件系统上两个只差大小写的路径落成一个文件——两者都「在」，不会误报。"""
    r = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", sha], capture_output=True)
    if r.returncode != 0:
        _say(hook, f"❌ 列不出被推提交 {sha[:10]} 的文件（git ls-tree 退出码 {r.returncode}："
                   f"{r.stderr.decode('utf-8', 'replace').strip()}）—— 契约**未被检查**（这不是「检查通过」）")
        return None
    gaps = []
    for entry in r.stdout.split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        if not path or meta.split()[1:2] != [b"blob"]:
            continue
        rel = os.fsdecode(path)
        if not os.path.lexists(tree / rel):
            gaps.append(rel)
    return sorted(gaps)


def _contract_env(plugin_dir: Path, report_path: Path) -> dict:
    """契约闸 pytest 子进程的环境（清掉哪些、为什么，见模块 docstring「契约闸」一节的「子进程环境」）。"""
    drop = set(_GIT_LOCATION_VARS) | set(_PYTEST_ENV_VARS) | set(_PYTHON_ENV_VARS)
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")      # 子进程也走 3.11（CLAUDE.md 解释器硬规则）
    env["PYTHONPATH"] = str(plugin_dir)                         # 只有结果插件：不许工作区顶替导出树缺的模块
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ALPHA_HIVE_CONTRACT_GATE_REPORT"] = str(report_path)
    return env


def _run_contract_gate(hook: str, sha: str) -> int:
    """在被推提交 `sha` 的导出树里跑契约测试。0 = 放行，1 = 拦（原因已出声）。"""
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="alpha-hive-contract-gate-"))
    try:
        tree, plugin_dir, report_path = work / "tree", work / "plugin", work / "report.json"
        tree.mkdir()
        plugin_dir.mkdir()
        if not _export_commit(hook, sha, tree):
            return 1
        gaps = _export_gaps(hook, sha, tree)
        if gaps is None:
            _say(hook, _BYPASS_NOTE)
            return 1
        files = _select_contract_tests(tree)
        # 跑 pytest 之前就能判定「没被检查」的几种，一次全报（不修一个报一个）
        blockers = []
        if gaps:
            shown = ", ".join(gaps[:10]) + (f" …（共 {len(gaps)} 个）" if len(gaps) > 10 else "")
            blockers.append(f"导出树缺了被推提交里的 {len(gaps)} 个文件（`.gitattributes` / info/attributes 的 "
                            f"export-ignore？在这棵树里测的就不是被推的提交）：{shown}")
        if not files:
            blockers.append(f"被推提交里找不到契约测试（{' / '.join(CONTRACT_TEST_GLOBS)}）"
                            "——「没东西可测」不是「测过了」")
        for req in CONTRACT_REQUIRED_TESTS:
            if not (tree / req).is_file():
                where = "被推提交里有，但被 export-ignore 挡在导出树外" if req in gaps else "被推提交里没有这个文件"
                blockers.append(f"缺必选契约测试（CONTRACT_REQUIRED_TESTS）：{req}（{where}）")
        if blockers:
            _say(hook, f"❌ 被推提交 {sha[:10]} 的契约**未被检查**（这不是「检查通过」）：")
            for b in blockers:
                _say(hook, f"    · {b}")
            _say(hook, _BYPASS_NOTE)
            return 1
        (plugin_dir / "_alpha_hive_contract_gate_report.py").write_text(_REPORT_PLUGIN, encoding="utf-8")
        env = _contract_env(plugin_dir, report_path)
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "-p", "_alpha_hive_contract_gate_report", "--maxfail=1000",
               "-m", "not integration and not network", *files]
        try:
            p = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=CONTRACT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _say(hook, f"❌ 契约测试 {CONTRACT_TIMEOUT_S}s 内没跑完（{', '.join(files)}）"
                       "—— 契约**未被检查**（这不是「检查通过」）。")
            _say(hook, _BYPASS_NOTE)
            return 1
        elapsed = time.monotonic() - started
        problems = []
        try:
            rep = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # pytest 在 sessionfinish 之前就退了（用法错、插件没加载上……）：没有报告 ≠ 没有失败
            rep = None
            problems.append(f"结果报告读不到（{type(e).__name__}: {e}）")
        if rep is not None:
            problems += [f"失败：{n}" for n in rep["failed"]]
            problems += [f"收集出错：{n}" for n in rep["collect_errors"]]
            problems += [f"整个文件被跳过（收集期 skip / importorskip；没跑 ≠ 没问题）：{n}"
                         for n in rep["collect_skipped"]]
            problems += [f"被跳过（没跑 ≠ 没问题）：{n}" for n in rep["skipped"]]
            problems += [f"被 -m 摘掉（契约测试不许带 network / integration 标记——离线闸里跑不到）：{n}"
                         for n in rep["deselected"]]
            # 空文件、被 conftest 悄悄删掉的测试不留任何失败 / skip 记录，只能按文件数「通过」
            passed_files = {rep["item_files"].get(n) for n in rep["passed"]}
            problems += [f"{f}：0 条通过（空文件 / 整文件被跳过 / 测试被插件摘掉，都等于没测）"
                         for f in files if f not in passed_files]
        if p.returncode == 0 and not problems:
            _say(hook, f"契约测试通过（{len(rep['passed'])} passed，{len(files)} 个文件，"
                       f"被推提交 {sha[:10]}，用时 {elapsed:.1f}s）")
            return 0
        lines = (p.stdout + p.stderr).strip().splitlines()
        print("\n".join(lines[-60:]), file=sys.stderr)
        _say(hook, f"❌ 契约测试未通过（被推提交 {sha[:10]}，pytest 退出码 {p.returncode}，用时 {elapsed:.1f}s）：")
        for problem in problems or ["pytest 退出码非 0，但结果报告里没有失败条目——看上面的输出"]:
            _say(hook, f"    · {problem}")
        _say(hook, f"修好再推（本地复现：/usr/local/bin/python3 -m pytest -p no:cacheprovider {' '.join(files)}）。")
        _say(hook, _BYPASS_NOTE)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def pre_push(root: Path, stdin_text: str, remote: str = "") -> int:
    hook = "pre-push"
    rc = 0
    must_check = False
    contract_shas = []
    for line in stdin_text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            _say(hook, f"看不懂 pre-push 输入行：{line!r}")
            return 1
        _local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_ref != GUARDED_REMOTE_REF or _is_zero(local_sha):
            continue
        base = _range_base(remote, remote_sha)
        # 契约闸的触发与下面 CHANGELOG 部分各判各的（CHANGELOG 没动也可能动了代码）
        code_touched = _contract_triggered(hook, base, local_sha)
        if code_touched is None:
            return 1
        if code_touched:
            contract_shas.append(local_sha)
        # 以下 CHANGELOG 部分拦了只记 rc、不提前返回：契约闸照跑（见文末）
        if not base:
            touched = True  # 算不出区间 ⇒ 证明不了没动
        else:
            touched = _differs(hook, base, local_sha, "--", CHANGELOG)
            if touched is None:
                rc = 1
                continue
        if not touched:
            continue
        pushed = _git("rev-parse", "--verify", "--quiet", f"{local_sha}:{CHANGELOG}")
        here = _git("hash-object", "--", CHANGELOG)
        if pushed.returncode != 0 or here.returncode != 0 or pushed.stdout.strip() != here.stdout.strip():
            _say(hook, f"❌ 要推往 main 的提交 {local_sha[:10]} 里的 CHANGELOG.md 与工作区不同。"
                       "完整性测试读的是工作区，结果不代表要推的内容 —— 让工作区与该提交一致后再推。")
            rc = 1
            continue
        must_check = True
    if must_check:
        rc = _run_integrity_test(hook, root) or rc
    # CHANGELOG 部分没过（测试红了、或「与工作区不同」）也照跑契约闸：
    # 一次推送把两件事都报出来，省一轮「修一个、推、再被另一个拦」
    for sha in contract_shas:
        rc = _run_contract_gate(hook, sha) or rc
    return rc


def install_hooks() -> int:
    r = _git("rev-parse", "--git-path", "hooks")
    if r.returncode != 0:
        print(f"找不到 hooks 目录：{r.stderr.strip()}", file=sys.stderr)
        return 1
    hooks_dir = Path(r.stdout.strip())
    if not hooks_dir.is_absolute():
        hooks_dir = Path.cwd() / hooks_dir
    hooks_dir.mkdir(parents=True, exist_ok=True)
    refused = []
    for name, body in HOOKS.items():
        path = hooks_dir / name
        if path.exists():
            current = path.read_text(encoding="utf-8", errors="replace")
            if current == body:
                print(f"{path}：已是最新")
                continue
            if _SHIM_MARKER not in current:
                refused.append(path)
                continue
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        print(f"{path}：已写入")
    for path in refused:
        print(f"{path}：已存在且不是本守卫生成的钩子，**未覆盖**。请人工合并后重跑。", file=sys.stderr)
    return 1 if refused else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pre-commit", action="store_true")
    mode.add_argument("--pre-push", action="store_true")
    mode.add_argument("--install-hook", action="store_true")
    ap.add_argument("hook_args", nargs="*", help="git 传给 pre-push 的 <remote> <url>（未使用）")
    args = ap.parse_args(argv)

    if args.install_hook:
        return install_hooks()
    top = _git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print(f"changelog-guard: 不在 git 工作区里：{top.stderr.strip()}", file=sys.stderr)
        return 1
    root = Path(top.stdout.strip())
    os.chdir(root)
    if args.pre_commit:
        return pre_commit(root)
    return pre_push(root, sys.stdin.read(), remote=args.hook_args[0] if args.hook_args else "")


if __name__ == "__main__":
    sys.exit(main())
