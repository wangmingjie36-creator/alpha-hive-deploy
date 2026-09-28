"""部署副本必须是 `origin/main` 上的最新版（v0.45.353，编排器纳入版本控制·阶段 1）

背景
----
v0.45.353 起编排器的唯一真相是仓库 `scripts/alpha-hive-orchestrator.sh`；launchd 执行的是
`~/.claude/scripts/alpha-hive-orchestrator.sh`（部署副本，路径不变——Desktop 有 TCC 限制，
不让 launchd 下的 bash 直接跑仓库里那份，见编排器 production_sync 段注释）。

阶段 1 还没有自动部署：**改编排器 = 改仓库那份 → 合入 origin/main → 手动 `cp -p` 到部署位置**。
两份文件就有了两种坏法，本文件各拦一种：

* **漂移**：部署副本的内容在 `origin/main` 的历史里从没出现过 ⇒ 有人绕过仓库直接改了生产。
  这正是纳入版本控制要消灭的状态；不拦，下一次部署会把那次热修复静默冲掉。
* **未部署**：内容是 main 上的**旧**版本 ⇒ 合入了却忘了 `cp`，新代码永远不跑、没人知道。

⚠️ 阶段 3 接上自动部署后，「未部署」在合入到下一轮扫描之间是**正常状态**，
那时第二条要改成读部署记录，不能照旧恒红。

谁会红：本机跑全套时（部署副本只在装了定时任务的那台 Mac 上，别处按类 skip）。
这仍是「碰巧跑全套才看见」——阶段 2/3 的部署步骤会把同一判定搬进每轮扫描、写进 status.json。
"""

import subprocess

import pytest

from tests._orchestrator import DEPLOYED_ORCH, REPO_ORCH

_ROOT = REPO_ORCH.parent.parent
_REL = REPO_ORCH.relative_to(_ROOT).as_posix()
_REF = "origin/main"


def classify(deployed_blob: str, history_blobs: set, tip_blob: str) -> str:
    """`latest` / `stale`（旧版未部署新版）/ `drift`（内容不在 git 历史里）。"""
    if deployed_blob == tip_blob:
        return "latest"
    return "stale" if deployed_blob in history_blobs else "drift"


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(_ROOT), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} 失败（rc={r.returncode}）：{r.stderr.strip()}"
    return r.stdout.strip()


class TestClassifyHasTeeth:
    """纯函数，任何机器都跑：三种结局各喂一次。"""

    def test_three_outcomes(self):
        hist = {"a1", "b2", "c3"}
        assert classify("c3", hist, "c3") == "latest"
        assert classify("a1", hist, "c3") == "stale"
        assert classify("zz", hist, "c3") == "drift"


class TestDeployedMatchesRepo:
    @pytest.fixture(scope="class")
    def blobs(self):
        if not DEPLOYED_ORCH.is_file():
            pytest.skip("部署副本不在本机（只在装了定时任务的那台 Mac 上）")
        # 仓库与 git 在任何开发检出里都在 ⇒ 下面取不到就是真错，断言不 skip
        raw = _git("log", "--no-abbrev", "--raw", "--format=", _REF, "--", _REL)
        history = {ln.split()[3] for ln in raw.splitlines() if ln.startswith(":")}
        history.discard("0" * 40)
        assert history, f"{_REF} 的历史里没有 {_REL} —— 读错了 ref 或路径，下面的判定会空转"
        tip = _git("rev-parse", f"{_REF}:{_REL}")
        deployed = _git("hash-object", str(DEPLOYED_ORCH))
        return deployed, history, tip

    def test_deployed_copy_is_not_hand_edited(self, blobs):
        deployed, history, tip = blobs
        assert classify(deployed, history, tip) != "drift", (
            f"部署副本 {DEPLOYED_ORCH} 的内容不在 {_REF} 的任何一版 {_REL} 里 —— "
            "有人绕过仓库直接改了生产。把那次改动搬进仓库 scripts/ 提交合入，再重新部署；"
            "别直接覆盖（会丢掉那次改动）。先 `diff` 看清改了什么。")

    def test_latest_main_version_is_deployed(self, blobs):
        deployed, history, tip = blobs
        assert classify(deployed, history, tip) != "stale", (
            f"{_REF} 上的编排器比部署副本新 —— 合入了但没部署，新代码不会跑。"
            f"阶段 1 手动部署：先备份部署副本，再从 main 取出覆盖：\n"
            f"  cp -p {DEPLOYED_ORCH} {DEPLOYED_ORCH}.bak-$(date +%Y%m%d)_pre-<版本>\n"
            f"  git -C '{_ROOT}' show {_REF}:{_REL} > {DEPLOYED_ORCH}.new && "
            f"chmod 755 {DEPLOYED_ORCH}.new && mv {DEPLOYED_ORCH}.new {DEPLOYED_ORCH}")
