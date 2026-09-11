"""扫描开始时记录「在跑的是哪一版代码」（v0.45.182）。

为什么要有这个文件
------------------
2026-09-10 事故（CHANGELOG v0.45.181）：一个修复推到了 `main`，而生产 checkout
`~/Desktop/Alpha Hive` **不自动 pull**、落后六个提交，当天扫描照旧跑旧代码，
把刚回灌的 125 条样本又删了一遍。

能查出来纯属**数据消失得够显眼**——对账 `1293+30−125=1198` 正好对上。
换成一个不改变行数的行为差异（评分口径、权重、阈值），就无从发现：
当时「生产在跑的是哪一版代码」在日志与 `status.json` 里**都没有任何记录**。
按 CLAUDE.md 那条「谁会红？」——没人会红。

本模块只做一件事：把 `git rev-parse --short HEAD` + CHANGELOG 顶部版本号
写进日志与 `scan_timing.json`（编排器已用 jq 把后者并进 `status.json`，
见 `alpha-hive-orchestrator.sh::write_status`，故**无需改编排器**）。
**不改任何扫描行为**，任何失败都只降级为 None。

两条设计约束
------------
1. 仓库路径用 `Path(__file__)` 而**不是** `PATHS.home`：这里要的是「代码在哪」，
   不是「数据在哪」（CLAUDE.md「这个路径指向代码还是数据？」一节；同族先例是
   `health_check.PROJECT` 的 `git -C <仓库>`）。且在**函数体内**求值，
   不冻成模块级常量——冻了 `_isolate_env` 那类隔离就对它无效。
2. **缺失一律 None，不写占位字符串。** `"unknown"` / `"0000000"` 会被下游读成
   「有个版本，只是长这样」；None 才是「没测到」。同 v0.45.114 / scan_timing 的判据。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

try:
    from hive_logger import get_logger
    _log = get_logger("code_version")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.code_version")

_GIT_TIMEOUT_S = 10
_CHANGELOG_RE = re.compile(r"^##\s*\[([0-9]+(?:\.[0-9]+)*)\]")


def _repo_dir() -> Path:
    """本模块所在的代码目录 = git 仓库。**调用时求值。**

    ⚠️ 不要改成 `PATHS.home`：那是数据根，测试里会被指到 tmp，
    于是 `git -C` 找不到仓库、版本恒为 None——观测点就此失效且无人会红。
    """
    return Path(__file__).resolve().parent


def _git(*args: str) -> Optional[str]:
    """跑一条 git 命令。**失败返回 None；成功返回 stdout（strip 后，可能是空串）。**

    ⚠️ 不要写成 `return r.stdout.strip() or None` —— 那会把「成功但没有输出」
    （`git status --porcelain` 在干净工作区就是空输出）和「命令失败」混成同一个
    `None`，下游再也分不出「工作区干净」和「没测到」。这正是本仓反复栽的
    0/空值冒充真读数那一族。

    ⚠️ git 的退出码不止「成功/失败」两种：**128 = 这里不是 git 仓库**。
    把它揉进普通失败就是「把一种失败误报成另一种」——本仓 CLAUDE.md
    专门就 `git check-ignore` 的三义退出码记过一次。这里分开打日志。
    """
    try:
        r = subprocess.run(["git", "-C", str(_repo_dir()), *args],
                           capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    except FileNotFoundError:
        _log.warning("code_version: 找不到 git 可执行文件，版本信息记为 None")
        return None
    except (subprocess.SubprocessError, OSError) as e:  # noqa: BLE001
        _log.warning("code_version: git %s 执行失败，记为 None: %s", " ".join(args), e)
        return None
    if r.returncode == 128:
        _log.warning("code_version: %s 不是 git 仓库（git 退出码 128），版本信息记为 None",
                     _repo_dir())
        return None
    if r.returncode != 0:
        _log.warning("code_version: git %s 退出码 %d，记为 None: %s",
                     " ".join(args), r.returncode, (r.stderr or "").strip()[:200])
        return None
    return r.stdout.strip()


def changelog_version() -> Optional[str]:
    """CHANGELOG.md 顶部的版本号（形如 `0.45.182`）。读不到返回 None。

    只认**行首** `## [` ——MEMORY.md 里记过：不锚定行首会把正文里引用的
    版本号也数进去。
    """
    path = _repo_dir() / "CHANGELOG.md"
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = _CHANGELOG_RE.match(line)
                if m:
                    return m.group(1)
    except OSError as e:
        _log.warning("code_version: 读不到 %s，CHANGELOG 版本记为 None: %s", path, e)
        return None
    _log.warning("code_version: %s 里没有行首 `## [x.y.z]`，记为 None", path)
    return None


def resolve() -> dict:
    """返回「这一轮跑的是哪一版代码」。**任何字段取不到就是 None，绝不编。**"""
    sha = _git("rev-parse", "--short", "HEAD") or None
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or None
    # 只看**已跟踪**文件的改动：生产 checkout 常年有未跟踪产物（日报 HTML、
    # iCloud 重名副本），把它们算进 dirty 会让这个字段恒为 True、失去信号。
    porcelain = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "sha": sha,
        "branch": branch,
        # None = 没测到；False = 测了、干净；True = 有未提交改动。三者必须可区分。
        "dirty_tracked": (None if porcelain is None else bool(porcelain)),
        "changelog_version": changelog_version(),
        "repo_dir": str(_repo_dir()),
    }


def summary_line(info: Optional[dict] = None) -> str:
    """一行人读摘要，进日志用。缺的字段显示 `—`，绝不显示占位值。"""
    i = info if info is not None else resolve()
    sha = i.get("sha") or "—"
    ver = i.get("changelog_version") or "—"
    br = i.get("branch") or "—"
    d = i.get("dirty_tracked")
    dirty = "—" if d is None else ("有未提交改动" if d else "干净")
    return f"代码版本 v{ver} | commit {sha} | 分支 {br} | 工作区 {dirty}"


def log_startup(info: Optional[dict] = None) -> dict:
    """扫描开始时调用：解析并打一条 INFO。返回解析结果供写进快照。"""
    i = info if info is not None else resolve()
    _log.info("%s", summary_line(i))
    return i
