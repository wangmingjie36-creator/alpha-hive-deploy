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

所有 git 调用共用一个时间预算（`TOTAL_BUDGET`，钩子配置的 timeout 比它大）；改工作区的 merge 从不被杀（自己的进程组、输出进临时文件），预算内没做完就让它在后台做完。

输出走 stdout（SessionStart 钩子的 stdout 进会话上下文）。CLAUDE.md 是在钩子之前还是之后载入的**没有核实**——
所以快进后把 diff 直接给出来，哪种顺序会话都看得到变化。退出码恒为 0：钩子不许挡住会话启动。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

#: 整个钩子的时间预算（秒）。`.claude/settings.json` 里钩子的 timeout 必须留出余量地大于它（守卫
#: `TestHookIsRegistered`）：被宿主按超时杀掉的钩子什么也告诉不了会话（v0.45.467 二次检查：初版各调用各给超时，
#: merge 一个就 60 秒，钩子却只有 30 秒）。
TOTAL_BUDGET = 20
#: 取 origin/main 最多用预算里的多少：断网时 DNS 失败是秒回的，挂住的是半通的网络——不让它吃满预算拖住会话启动
FETCH_TIMEOUT = 10
#: 进会话的 diff 最多多少行（多了截断并给出完整命令）
MAX_DIFF_LINES = 200
#: 预算里给快进后取 diff 留的秒数（merge 最多等到「剩余 − 它」）
DIFF_RESERVE = 3
#: 会话启动时就载入、快进改了也要告诉会话的东西：各级 CLAUDE.md（给 diff）与 `.claude/` 下的设置 / 钩子 / skills（列文件）
SESSION_LOADED = (":(glob)**/CLAUDE.md", ".claude")


class _Budget:
    """从构造起算的剩余时间；每个 git 调用只拿剩下的那部分。"""

    def __init__(self, seconds: float):
        self._end = time.monotonic() + seconds

    def left(self, cap: Optional[float] = None) -> float:
        rem = max(0.0, self._end - time.monotonic())
        return rem if cap is None else min(cap, rem)


def _git(repo: Path, *args: str, budget: _Budget, cap: float = 10, strip: bool = True) -> Tuple[int, str, str]:
    """只读 / 只动远端跟踪 ref 的 git 调用（写工作区的 merge 不走这里，见 `_merge_detached`）。

    - `--no-optional-locks`：`status` 不顺手回写索引（那要拿 index.lock）；
    - 超时先 SIGTERM（git 收到它会删掉自己的锁文件），3 秒后才 SIGKILL——直接 SIGKILL 可能把 `*.lock` 留在 `.git` 里；
    - `strip=False` 给 `status --porcelain`：首行的前导空格是状态列，strip 会把文件名吃掉一个字。
    """
    timeout = budget.left(cap)
    if timeout <= 0:
        return 1, "", "时间预算用完"
    try:
        p = subprocess.Popen(["git", "--no-optional-locks", *args], cwd=repo,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        return 1, "", f"{type(e).__name__}: {e}"
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
        return 1, "", f"超时（{timeout:.0f} 秒）"
    return p.returncode, (out.strip() if strip else out.rstrip("\n")), err.strip()


def _merge_detached(repo: Path, target: str, wait: float) -> Tuple[Optional[int], str]:
    """`merge --ff-only target` ⇒ (退出码 | None = 仍在后台进行, stderr)。**从不被杀**。

    它改工作区与索引：杀在半路会留下 index.lock 或半新半旧的工作区，之后这份检出里的 git 全部报错。所以：
    跑在自己的会话（进程组）里——宿主超时杀钩子时波及不到；本脚本等到预算用完也只是不再等。输出写进临时文件
    而不是管道：本脚本先走了，管道读端关闭，git 再写就会被 SIGPIPE 杀在半路。
    """
    err_f = tempfile.TemporaryFile(mode="w+")
    try:
        p = subprocess.Popen(["git", "merge", "--ff-only", "-q", target], cwd=repo, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=err_f, start_new_session=True)
    except OSError as e:
        return 1, f"{type(e).__name__}: {e}"
    try:
        rc = p.wait(timeout=max(wait, 0.0))
    except subprocess.TimeoutExpired:
        return None, ""
    err_f.seek(0)
    return rc, err_f.read().strip()


def _production_clone() -> Optional[Path]:
    """生产克隆的位置——唯一真相 `production_clone.default_dest()`；取不到就当没有（只少一句提醒）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        import production_clone
        return production_clone.default_dest().resolve()
    except Exception:  # noqa: BLE001 —— 钩子不许因此失败
        return None


def _main_checkout(start: Path, budget: _Budget) -> Optional[Path]:
    """`start` 所在仓库的工作区根——仅当它是独立仓库的**主工作区**（不是 linked worktree）。"""
    rc, top, _ = _git(start, "rev-parse", "--show-toplevel", budget=budget)
    if rc != 0 or not top:
        return None
    top_p = Path(top)
    rc1, gd, _ = _git(top_p, "rev-parse", "--absolute-git-dir", budget=budget)
    rc2, cd, _ = _git(top_p, "rev-parse", "--git-common-dir", budget=budget)
    if rc1 or rc2:
        return None
    common = (top_p / cd).resolve() if not os.path.isabs(cd) else Path(cd).resolve()
    return top_p.resolve() if Path(gd).resolve() == common else None


def _session_loaded_changes(repo: Path, old: str, new: str, budget: _Budget) -> List[str]:
    """快进范围里会话启动时就载入的东西变了什么：各级 CLAUDE.md 给 diff，`.claude/` 下的列文件。

    v0.45.467 二次检查：初版只给根 CLAUDE.md 的 diff；`.claude/settings.json`（钩子 / 权限）、skills、子目录的
    CLAUDE.md 同一次快进里变了，会话不知道自己仍按旧的跑。
    """
    rc, names, _ = _git(repo, "diff", "--name-only", f"{old}..{new}", "--", *SESSION_LOADED, budget=budget)
    if rc != 0 or not names:
        return []
    files = names.splitlines()
    md = [f for f in files if f == "CLAUDE.md" or f.endswith("/CLAUDE.md")]
    cfg = [f for f in files if f not in md]
    out: List[str] = []
    if md:
        _, diff, _ = _git(repo, "diff", f"{old}..{new}", "--", *md, budget=budget)
        lines = diff.splitlines()
        out.append(f"其中 CLAUDE.md 有变化（{'、'.join(md)}）——本会话开头载入的可能是快进前的版本，以下面的变化为准"
                   "（或重新读一遍）：")
        out += lines[:MAX_DIFF_LINES]
        if len(lines) > MAX_DIFF_LINES:
            out.append(f"……（截断，共 {len(lines)} 行；完整：git diff {old[:8]}..{new[:8]} -- {' '.join(md)}）")
    if cfg:
        out.append(f".claude/ 下有变化（{'、'.join(cfg[:10])}{' …' if len(cfg) > 10 else ''}）：设置 / 钩子 / skills "
                   "在会话开头就已载入，本会话仍按旧的跑，下次会话才生效。")
    return out


def run(start: Path, budget: Optional[_Budget] = None) -> List[str]:
    """判断并（可能）快进 ⇒ 要告诉会话的行；空表 = 不出声。"""
    budget = budget or _Budget(TOTAL_BUDGET)
    repo = _main_checkout(start, budget)
    if repo is None:
        return []
    clone = _production_clone()
    if clone is not None and repo == clone:
        return [f"⚠️ 这里是生产克隆 {repo}：不要在这里开会话、改文件、提交（CLAUDE.md「生产代码 = 独立克隆」）。"]

    rc, branch, _ = _git(repo, "symbolic-ref", "-q", "--short", "HEAD", budget=budget)
    if rc != 0 or branch != "main":
        return [f"开发检出当前不在 main（{branch or 'detached HEAD'}）：不自动快进，CLAUDE.md 是这个分支上的版本。"]

    rc, _, err = _git(repo, "fetch", "-q", "origin", "main", budget=budget, cap=FETCH_TIMEOUT)
    if rc != 0:
        hint = ""
        if "bad object refs/" in err:
            hint = "（多半是 iCloud 造的带空格重名 ref：find .git/refs -type f -name \"* *\"，清理前读 CLAUDE.md 那一节）"
        return [f"⚠️ 开发检出没能取 origin/main（{err[-300:]}）{hint}：CLAUDE.md 可能落后，本次没自动快进。"]

    rc, target, err = _git(repo, "rev-parse", "origin/main", budget=budget)
    rc2, counts, err2 = _git(repo, "rev-list", "--left-right", "--count", f"main...{target}", budget=budget)
    if rc or rc2:
        return [f"⚠️ 开发检出比较 main 与 origin/main 失败（{(err or err2)[-300:]}）：本次没自动快进。"]
    ahead, behind = (int(x) for x in counts.split())
    if behind == 0:
        return []
    if ahead:
        return [f"开发检出 main 有 {ahead} 个未推送提交、落后 origin/main {behind} 个：不自动快进"
                "（先推送或整理本地提交）；CLAUDE.md 可能落后。"]
    rc, dirty, err = _git(repo, "status", "--porcelain", "--untracked-files=no", budget=budget, strip=False)
    if rc != 0 or dirty:
        files = ", ".join(ln[3:] for ln in dirty.splitlines()[:5]) + (" …" if len(dirty.splitlines()) > 5 else "")
        why = f"工作区有未提交改动（{files}）" if dirty else f"查不了工作区状态（{err[-200:]}）"
        return [f"开发检出 main 落后 origin/main {behind} 个提交，但{why}：不自动快进；CLAUDE.md 可能落后。"]

    _, old, _ = _git(repo, "rev-parse", "HEAD", budget=budget)
    rc, err = _merge_detached(repo, target, budget.left() - DIFF_RESERVE)
    if rc is None:
        out = [f"开发检出 main 正在后台快进 {old[:8]} → {target[:8]}（{behind} 个提交；时间预算内没做完，没有打断它）。"]
    elif rc != 0:
        return [f"⚠️ 开发检出快进失败（{err[-300:]}）：仍停在 {old[:8]}，落后 {behind} 个提交；CLAUDE.md 可能落后。"]
    else:
        out = [f"开发检出 main 已快进 {old[:8]}..{target[:8]}（{behind} 个提交）。"]
    # diff 只依赖两端提交、不依赖 merge 做没做完：后台快进时也照样给出
    return out + _session_loaded_changes(repo, old, target, budget)


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
