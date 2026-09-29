"""编排器的两个位置——**唯一定义**（v0.45.353 起编排器纳入版本控制）。

* `REPO_ORCH`：仓库 `scripts/alpha-hive-orchestrator.sh`，**唯一真相、唯一可改的那份**。
  读编排器原文的测试一律读它 ⇒ 干净检出、worktree、GitHub CI 上都跑得到、都会红。
* `DEPLOYED_ORCH`：`~/.claude/scripts/alpha-hive-orchestrator.sh`，launchd 实际执行的**部署副本**。
  只有「部署副本与仓库是否一致」那一条测试读它（`test_orchestrator_deployed_matches_repo.py`）。
* `DEPLOY_RECORD`：`~/.claude/logs/orchestrator_deploy.json`，阶段 3（v0.45.370）起编排器每轮扫描前的自动部署
  结局（手动部署带 `--out` 也写它）。一致性检查读它区分「合入后待下一轮」与「该部署没部署」。

此前 8 个测试文件各自硬编码部署路径、文件不在就 skip ⇒ 在 CI 上全部恒 skip，
只在碰巧于本机跑全套时可见；v0.45.334 引入的 `$READINESS_JSON）` 就是这样隔了一天才被撞见
（v0.45.348 修）。

锚点选择（CLAUDE.md「这个路径指向代码还是数据？」）：两者都是**代码**，不走 `PATHS.*`。
`DEPLOYED_ORCH` / `DEPLOY_RECORD` 指的恰恰是生产那份，不能被任何测试隔离改绑到沙箱（改绑了，一致性检查就会
对着一个不存在的文件恒 skip）。v0.45.370 起按**账户家目录**（`pwd`）求值、不读 HOME：此前是 import 时读 HOME，
若本模块恰好在某条把 HOME 指向沙箱的用例里**首次**被导入，两者就冻在沙箱里（写 v0.45.370 的测试时实测踩到）。
"""

import os
import pwd
import re
from pathlib import Path

_ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REPO_ORCH = Path(__file__).resolve().parent.parent / "scripts" / "alpha-hive-orchestrator.sh"
DEPLOYED_ORCH = _ACCOUNT_HOME / ".claude" / "scripts" / "alpha-hive-orchestrator.sh"
DEPLOY_RECORD = _ACCOUNT_HOME / ".claude" / "logs" / "orchestrator_deploy.json"


def extract_function(text: str, name: str) -> str:
    """抽出 `name() {` 到其后第一行恰为 `}` 的函数体（编排器里函数体都是 4 空格缩进）。
    （v0.45.370 从 test_orchestrator_step5_gh_pages.py 挪来共用。）"""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    if not m:
        raise LookupError(f"编排器里找不到函数 {name}")
    return m.group(0)


def repo_orchestrator_text() -> str:
    """仓库里的编排器原文。文件随仓库发布 ⇒ 缺失是断言失败，不是 skip。"""
    assert REPO_ORCH.is_file(), (
        f"仓库里没有编排器 {REPO_ORCH} —— v0.45.353 起它受版本控制，缺了就是真缺")
    return REPO_ORCH.read_text(encoding="utf-8")
