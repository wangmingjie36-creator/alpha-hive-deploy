#!/usr/bin/env python3
"""
🚀 Agent Toolbox —— 日报流程用的 Git 封装

唯一在产的能力是 `GitHubTool`（子命令白名单 + 无 shell 模式），经
`AgentHelper.git` 供 `report_deployer.py` 的 gh-pages / main 部署链路调用。

⚠️ **这里不是「预留的 MCP 脚手架」，不要再往这里加通用工具。**
v0.45.198 前本模块的 docstring 自称「Python-native MCP replacement …
后续升级为真正的 MCP 服务器」，并据此保留了零读者的 `FilesystemTool`
与 `NotificationTool`。那个「后续」**早已到来、且没走这条路**：真正的
MCP 服务器是独立另写的 `alpha_hive_mcp.py`（FastMCP + stdio，8 个
`alphahive_*` tool），它从不 import 本模块，暴露的是领域数据而非文件系统
/ 通知原语。两个类因此按「零读者即死」删除（判据见 auto-memory
`alpha-hive-dead-field.md`）。

要加 MCP 工具 → `alpha_hive_mcp.py`；要发 Slack → `slack_report_notifier.py`。
"""

import os
import shlex
import subprocess
from typing import Dict, List, Optional, Any

# ==================== GitHub 工具 ====================

class GitHubTool:
    """GitHub 操作（替代 GitHub MCP）"""

    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path or os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.abspath(__file__)))

    # 允许的 git 子命令白名单
    _ALLOWED_GIT_CMDS = {
        "status", "log", "diff", "branch", "add", "commit", "push",
        "pull", "fetch", "remote", "show", "tag", "stash", "rev-parse",
    }

    def run_git_cmd(self, cmd: str) -> Dict[str, Any]:
        """执行 Git 命令（白名单 + 无 shell 模式）"""
        try:
            parts = shlex.split(cmd)
            if not parts or parts[0] != "git":
                return {"success": False, "error": "Only git commands allowed"}
            subcmd = parts[1] if len(parts) > 1 else ""
            if subcmd not in self._ALLOWED_GIT_CMDS:
                return {"success": False, "error": f"Git subcommand not allowed: {subcmd}"}

            result = subprocess.run(
                parts,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.repo_path,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except (subprocess.SubprocessError, OSError) as e:
            return {"success": False, "error": str(e)}

    def status(self) -> Dict[str, Any]:
        """Git 状态"""
        result = self.run_git_cmd("git status --porcelain")
        if result["success"]:
            files = [line.split()[-1] for line in result["stdout"].split("\n") if line.strip()]
            return {"modified_files": files, "status": "✅ Clean" if not files else "⚠️ Dirty"}
        return {"error": result.get("stderr")}

    def commit(self, message: str,
               paths: Optional[List[str]] = None) -> Dict[str, Any]:
        """创建提交

        Args:
            paths: **白名单** —— 只暂存这些 pathspec（可含 glob）。
                留空则沿用 `git add -A`（全量），保持向后兼容。

        ⚠️ v0.43.4：自动化调用方**必须传 paths**。
        2026-07-30 的教训：定时日报走的是全量 `git add -A`，把当时工作区里
        进行中的 10 个版本的代码改动（backtester / chronos_bee /
        parallel_agent_runner / weekly_optimizer / 6 个测试文件…）全部卷进了
        一次名为「Alpha Hive 蜂群日报 14:02」的提交里。后果：提交历史失真、
        无法单独回滚代码改动、且半成品代码会被自动推上生产分支。

        自动化边界应按**白名单**定义而非黑名单：失败模式从"多提交了不该提交的"
        （不可发现）变成"漏提交了该提交的"（下次运行即可见）。
        """
        if paths:
            # 逐条暂存：某个 pathspec 无匹配时 git 会报错，此处容忍（该产物本次未生成）
            staged_any = False
            for p in paths:
                r = self.run_git_cmd(f"git add -- {shlex.quote(p)}")
                if r["success"]:
                    staged_any = True
            if not staged_any:
                return {"success": False, "error": "白名单未匹配到任何文件"}
        else:
            stage = self.run_git_cmd("git add -A")
            if not stage["success"]:
                return {"error": f"Failed to stage: {stage['stderr']}"}

        commit = self.run_git_cmd(f"git commit -m {shlex.quote(message)}")
        return {
            "success": commit["success"],
            "message": commit.get("stdout") or commit.get("stderr"),
            "details": commit
        }

    def push(self, branch: str = "main") -> Dict[str, Any]:
        """推送到远程"""
        result = self.run_git_cmd(f"git push origin {branch}")
        return {
            "success": result["success"],
            "output": result.get("stdout") or result.get("stderr")
        }

    def create_issue(self, title: str, body: str) -> Dict[str, Any]:
        """创建 GitHub Issue（需要 gh CLI）"""
        try:
            result = subprocess.run(
                ["gh", "issue", "create", "--title", title, "--body", body],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return {"success": True, "issue_url": result.stdout.strip()}
            else:
                return {"success": False, "error": result.stderr}
        except FileNotFoundError:
            return {"error": "GitHub CLI (gh) not installed"}

    def list_branches(self) -> Dict[str, Any]:
        """列出分支"""
        result = self.run_git_cmd("git branch -a")
        branches = [line.strip() for line in result["stdout"].split("\n") if line.strip()]
        return {"branches": branches}

    def diff(self, branch1: str, branch2: str) -> Dict[str, Any]:
        """查看 diff"""
        result = self.run_git_cmd(f"git diff {branch1}...{branch2}")
        return {
            "diff": result["stdout"],
            "stats": self._parse_diff_stats(result["stdout"])
        }

    @staticmethod
    def _parse_diff_stats(diff: str) -> Dict[str, int]:
        """解析 diff 统计"""
        lines = diff.split("\n")
        additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        return {"additions": additions, "deletions": deletions, "total_changes": additions + deletions}


# ==================== Agent 助手 ====================

class AgentHelper:
    """Agent 使用的统一工具集（现仅剩 Git 一条）。

    历史上还挂过 `.fs`（`FilesystemTool`）与 `.notify`（`NotificationTool`），
    v0.45.198 随那两个类一并删除——全仓 14 处 `agent_helper.*` 访问**全部**是
    `.git`，另两个属性从未被读过。新增属性前请先确认它真有读者。

    ⚠️ **动这个 `__init__` 时别指望测试接住你。** v0.45.198 实测：把
    `self.git` 整个拿掉，`tests/test_pipeline.py` + `tests/test_report_deployer_whitelist.py`
    共 77 条**全绿**——前者 `r.agent_helper = MagicMock()` 把整个 helper 换掉了
    （真 `__init__` 从没执行，`MagicMock` 访问 `.git` 会自动造一个出来），
    后者直接 `GitHubTool(repo_path=...)` 构造。改这里要靠数读者，不是靠跑测试。
    """

    def __init__(self):
        self.git = GitHubTool()

    def summary(self) -> str:
        """打印工具摘要"""
        return """
        🚀 Agent Toolbox 已就绪

        🐙 GitHub
           - status() ✓
           - commit(message) ✓
           - push(branch) ✓
           - diff(branch1, branch2) ✓
        """


def main():
    """演示用法（只读：不写文件、不对外发消息）"""
    helper = AgentHelper()
    print(helper.summary())

    # 测试 GitHub
    print("\nGit 状态...")
    status = helper.git.status()
    print(f"✅ {status}")


if __name__ == "__main__":
    main()
