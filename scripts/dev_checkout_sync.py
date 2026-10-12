#!/usr/bin/env python3
"""开发检出会话启动时把 main 快进到 origin/main（SessionStart 钩子，v0.45.467）。

为什么要有它
------------
从 `~/Desktop/Alpha Hive` 根启动的会话（含周 / 月定时任务：它们的会话 cwd 就是这里，定时任务工具改不了）
读的是**这份工作区里**的 CLAUDE.md / `.claude/` 设置。阶段 8 之前生产 = 这份检出，每个扫描日前
production_sync 顺带把它快进；阶段 8 把生产搬进独立克隆后，它只在哪个会话碰巧 pull / 提交时才前进——
周末没人动，周日的周任务就读两天前的 CLAUDE.md（10-11 那轮实测落后约 5 个提交，含一处 CLAUDE.md 改动）。
新鲜度原先是别人的副作用，副作用搬走了，依赖就没人管了。这里让**读者（会话启动）自己**刷新。

不让生产顺带快进这份检出：那会把阶段 8 拆开的耦合接回去（这里脏了 / 分叉了会拖累生产步骤，v0.45.436 用户否决）。

做什么
------
只在**开发检出根**（独立仓库的主工作区、不是 linked worktree、不是生产克隆）且在 main 上时动手：

  落后、工作区干净、本地无未推送提交  ⇒ `fetch` + `merge --ff-only`，并把 **CLAUDE.md 的变化**输出给会话
  有未提交改动 / 未推送提交 / 不在 main ⇒ 不碰，只说「落后 N 个提交，为什么没快进」
  fetch 失败                           ⇒ 说出来（iCloud 重名 ref 造成的给出诊断命令）
  worktree / 生产克隆 / 不是 git 仓库    ⇒ 静默（生产克隆另提醒一句：不要在那里开会话）

输出走 stdout（SessionStart 钩子的 stdout 进会话上下文）。CLAUDE.md 是在钩子之前还是之后载入的**没有核实**——
所以快进后把 diff 直接给出来，哪种顺序会话都看得到变化。退出码恒为 0：钩子不许挡住会话启动。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

#: CLAUDE.md 的 diff 最多输出多少行（多了截断并给出完整命令）
MAX_DIFF_LINES = 200
FETCH_TIMEOUT = 20


def _git(repo: Path, *args: str, timeout: float = 10, strip: bool = True) -> Tuple[int, str, str]:
    """`strip=False` 给 `status --porcelain`：首行的前导空格是状态列，strip 会把文件名吃掉一个字。"""
    try:
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", f"{type(e).__name__}: {e}"
    return r.returncode, (r.stdout.strip() if strip else r.stdout.rstrip("\n")), r.stderr.strip()


def _production_clone() -> Optional[Path]:
    """生产克隆的位置——唯一真相 `production_clone.default_dest()`；取不到就当没有（只少一句提醒）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        import production_clone
        return production_clone.default_dest().resolve()
    except Exception:  # noqa: BLE001 —— 钩子不许因此失败
        return None


def _main_checkout(start: Path) -> Optional[Path]:
    """`start` 所在仓库的工作区根——仅当它是独立仓库的**主工作区**（不是 linked worktree）。"""
    rc, top, _ = _git(start, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return None
    top_p = Path(top)
    rc1, gd, _ = _git(top_p, "rev-parse", "--absolute-git-dir")
    rc2, cd, _ = _git(top_p, "rev-parse", "--git-common-dir")
    if rc1 or rc2:
        return None
    common = (top_p / cd).resolve() if not os.path.isabs(cd) else Path(cd).resolve()
    return top_p.resolve() if Path(gd).resolve() == common else None


def run(start: Path) -> List[str]:
    """判断并（可能）快进 ⇒ 要告诉会话的行；空表 = 不出声。"""
    repo = _main_checkout(start)
    if repo is None:
        return []
    clone = _production_clone()
    if clone is not None and repo == clone:
        return [f"⚠️ 这里是生产克隆 {repo}：不要在这里开会话、改文件、提交（CLAUDE.md「生产代码 = 独立克隆」）。"]

    rc, branch, _ = _git(repo, "symbolic-ref", "-q", "--short", "HEAD")
    if rc != 0 or branch != "main":
        return [f"开发检出当前不在 main（{branch or 'detached HEAD'}）：不自动快进，CLAUDE.md 是这个分支上的版本。"]

    rc, _, err = _git(repo, "fetch", "-q", "origin", "main", timeout=FETCH_TIMEOUT)
    if rc != 0:
        hint = ""
        if "bad object refs/" in err:
            hint = "（多半是 iCloud 造的带空格重名 ref：find .git/refs -type f -name \"* *\"，清理前读 CLAUDE.md 那一节）"
        return [f"⚠️ 开发检出没能取 origin/main（{err[-300:]}）{hint}：CLAUDE.md 可能落后，本次没自动快进。"]

    rc, counts, err = _git(repo, "rev-list", "--left-right", "--count", "main...origin/main")
    if rc != 0:
        return [f"⚠️ 开发检出比较 main 与 origin/main 失败（{err[-300:]}）：本次没自动快进。"]
    ahead, behind = (int(x) for x in counts.split())
    if behind == 0:
        return []
    if ahead:
        return [f"开发检出 main 有 {ahead} 个未推送提交、落后 origin/main {behind} 个：不自动快进"
                "（先推送或整理本地提交）；CLAUDE.md 可能落后。"]
    rc, dirty, _ = _git(repo, "status", "--porcelain", "--untracked-files=no", strip=False)
    if rc != 0 or dirty:
        files = ", ".join(ln[3:] for ln in dirty.splitlines()[:5]) + (" …" if len(dirty.splitlines()) > 5 else "")
        return [f"开发检出 main 落后 origin/main {behind} 个提交，但工作区有未提交改动（{files}）：不自动快进；"
                "CLAUDE.md 可能落后。"]

    _, old, _ = _git(repo, "rev-parse", "--short", "HEAD")
    rc, _, err = _git(repo, "merge", "--ff-only", "-q", "origin/main", timeout=60)
    if rc != 0:
        return [f"⚠️ 开发检出快进失败（{err[-300:]}）：仍停在 {old}，落后 {behind} 个提交；CLAUDE.md 可能落后。"]
    _, new, _ = _git(repo, "rev-parse", "--short", "HEAD")
    out = [f"开发检出 main 已快进 {old}..{new}（{behind} 个提交）。"]
    _, diff, _ = _git(repo, "diff", f"{old}..{new}", "--", "CLAUDE.md")
    if diff:
        lines = diff.splitlines()
        out.append("其中 CLAUDE.md 有变化——本会话开头载入的可能是快进前的版本，以下面的变化为准"
                   "（或重新读一遍 CLAUDE.md）：")
        out += lines[:MAX_DIFF_LINES]
        if len(lines) > MAX_DIFF_LINES:
            out.append(f"……（截断，共 {len(lines)} 行；完整：git diff {old}..{new} -- CLAUDE.md）")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    start = Path(argv[0] if argv else (os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()))
    try:
        lines = run(start)
    except Exception as e:  # noqa: BLE001 —— 钩子不许挡住会话：意外也只说出来
        lines = [f"⚠️ dev_checkout_sync 出错（{type(e).__name__}: {e}）：开发检出没有自动快进，CLAUDE.md 可能落后。"]
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
