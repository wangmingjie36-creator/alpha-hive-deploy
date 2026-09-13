#!/usr/bin/env python3
"""CHANGELOG 完整性测试的 git hook（v0.45.216）

在**提交**与**推送到 main** 之前跑 `tests/test_changelog_entry_integrity.py`
（冲突标记 / 重号 / 空标题）。只挡「动了 CHANGELOG.md」的那些，其余一律放行。

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
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

CHANGELOG = "CHANGELOG.md"
INTEGRITY_TEST = "tests/test_changelog_entry_integrity.py"
GUARDED_REMOTE_REF = "refs/heads/main"
HOOK_NAMES = ("pre-commit", "pre-push")

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


def pre_push(root: Path, stdin_text: str, remote: str = "") -> int:
    hook = "pre-push"
    must_check = False
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
        if not base:
            touched = True  # 算不出区间 ⇒ 证明不了没动
        else:
            touched = _differs(hook, base, local_sha, "--", CHANGELOG)
            if touched is None:
                return 1
        if not touched:
            continue
        pushed = _git("rev-parse", "--verify", "--quiet", f"{local_sha}:{CHANGELOG}")
        here = _git("hash-object", "--", CHANGELOG)
        if pushed.returncode != 0 or here.returncode != 0 or pushed.stdout.strip() != here.stdout.strip():
            _say(hook, f"❌ 要推往 main 的提交 {local_sha[:10]} 里的 CHANGELOG.md 与工作区不同。"
                       "完整性测试读的是工作区，结果不代表要推的内容 —— 让工作区与该提交一致后再推。")
            return 1
        must_check = True
    return _run_integrity_test(hook, root) if must_check else 0


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
