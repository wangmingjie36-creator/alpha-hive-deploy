"""「本仓自己的 .py 有哪些」——**唯一实现**。

v0.45.186 抽出。此前有两份平行实现，各自为政：

* `test_paths_not_frozen_at_import.py`（v0.45.150 修过）——`git ls-files` 优先，
  回退 rglob 时排除点号开头目录与 vendored 目录。**对的那份。**
* `test_zero_weight_invariant.py`（v0.45.176 新写）——裸 `root.rglob("*.py")`。
  **错的那份**，而且它错得**只有生产 checkout 看得见**。

────────────────────────────────────────────────────────────────────────
⚠️ 为什么必须共享而不是各扫各的：可见性是**反的**
────────────────────────────────────────────────────────────────────────
生产 checkout `~/Desktop/Alpha Hive` 的 `.claude/worktrees/` 下挂着 10 个
**嵌套 git worktree**，每个都是一份完整的仓库副本，停在各自的版本上。
裸 rglob 在那里扫到 **22037** 个 .py（git 跟踪的只有 **338** 个），于是
v0.45.176 那条「生产代码不许传 adapted_weights」的守卫，报的是**别的 worktree**
停留在 v0.45.176 之前的陈旧副本 —— 5 条命中全在 `.claude/worktrees/` 里，
真正的 `alpha_hive_daily_report.py` 是干净的。

后果不只是「扫得慢」（那轮 82 秒，60 秒的 `--timeout` 会把它伪装成 Timeout，
把真正的断言失败盖掉）。真正要命的是：

    这条守卫在 **10 个 worktree 里全是绿的**（worktree 里没有嵌套 worktree），
    只在**跑每日扫描的那一台**上是红的。

写它的人看到的是绿，唯一会红的那台机器没人看 —— 这正是 MEMORY
`alpha-hive-test-writes-production` 记过的「在没有病灶的环境里测防御＝没测」，
只是方向反了过来：不是防御没被测到，是**病灶只长在没人看的地方**。

────────────────────────────────────────────────────────────────────────
两条分支，一道共同过滤
────────────────────────────────────────────────────────────────────────
`git ls-files` 与 rglob 回退的输出都过一遍 `_is_ours()`。看起来 git 分支不需要
（嵌套 worktree 本就不被本仓索引跟踪，实测 `git ls-files '*.py' | grep ^.claude`
为 0 条），但两条分支**必须同口径**：`.claude/` 本身是被跟踪的
（`.claude/launch.json` 在库里，且 `.claude/` 不在 .gitignore），哪天有人提交了
`.claude/hooks/x.py`，git 分支会扫到它、回退分支不会 —— 两条路给出不同答案，
而哪条生效取决于「这台机器有没有装 git」。同一判据两份口径早晚漂移，
漂移的那一刻两边都还是绿的。

⚠️ 已知取舍：git 分支只列**被跟踪**的文件，因此**全新未提交**的生产 .py 扫不到
（已跟踪文件的未提交修改照常扫得到——我们只拿 git 要路径清单，内容从磁盘读）。
这是 v0.45.150 就接受的取舍：口径可复现，胜过把「本地碰巧有什么」算进来。
"""
from __future__ import annotations

from pathlib import Path

#: 第三方代码的目录名。**注意**：`node_modules` 在这里和「点号开头」那道过滤
#: 里各出现一次，看似冗余，其实拦的是不同样本（`libs/site-packages/` 不以点号
#: 开头，`.cache/build/` 不含任何 vendored 关键字）。v0.45.150 实测 M11/M12：
#: 只留一道，另一类样本会全泄漏而断言仍绿。
VENDORED = ("node_modules", "site-packages", "vendor", "third_party")


def _is_ours(rel: Path) -> bool:
    """`rel`（相对仓库根）是不是**本仓自己的**源码。

    两道过滤缺一不可：
      ① 任一路径段是 vendored 目录名 —— 拦 `libs/site-packages/pkg/x.py`
      ② 任一路径段以点号开头 —— 拦 `.venv/`、`.tox/`、`.cache/`，
         以及本次的病灶 `.claude/worktrees/<嵌套副本>/`
    """
    if any(x in rel.parts for x in VENDORED):
        return False
    if any(part.startswith(".") for part in rel.parts):
        return False
    return True


def own_python_files(root) -> tuple[list[Path], str]:
    """返回 (绝对路径清单, 口径名)，口径名是 ``"git"`` 或 ``"rglob"``。

    `root` 是**参数**不是模块常量 —— 调用时求值，测试才能把它指向一棵带病灶的
    tmp 树（MEMORY `alpha-hive-test-writes-production`：路径冻在 import 期，
    pytest 收集期早于任何 fixture，隔离形同不存在）。

    ⚠️ 子进程有**两条**失败路径，各堵一次（v0.45.117/119 同款教训）：
       `git` 不存在会**抛** `FileNotFoundError`，不是仓库会**返回** 128。
       只判返回值的守卫接不住抛，只判异常的接不住返回。
    """
    import subprocess

    root = Path(root)
    try:
        r = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=str(root),
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip("\x00").strip():
            rels = [Path(x) for x in r.stdout.split("\x00") if x]
            return [root / x for x in rels if _is_ours(x)], "git"
    except (OSError, subprocess.SubprocessError):
        pass                                        # 落到 rglob 回退

    files = []
    for p in root.rglob("*.py"):
        if _is_ours(p.relative_to(root)):
            files.append(p)
    return files, "rglob"
