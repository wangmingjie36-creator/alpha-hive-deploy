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

v0.45.204 续做**方法粒度**：`GitHubTool` 是活类，但类里的方法不全是活的。
删掉零读者的 `push` / `create_issue` / `list_branches` / `diff`（及其私有助手
`_parse_diff_stats`）。两条独立证据、各带正对照：

1. **静态**（`git ls-files | xargs grep`，**别用** `rglob`/`grep -r`——生产 checkout
   的 `.claude/worktrees/` 下有 14 个嵌套仓库副本会把陈旧代码算成读者）：
   全仓 `agent_helper.*` 访问**全部**是 `.git`，只碰 4 个成员——
   `run_git_cmd` 9、`repo_path` 3、`commit` 1+3、`status` 1；被删的四个为 0。
   三个盲区均已复查：方法名作为字符串（全文件类型，零命中）、仓库外调用者
   （`~/.claude/scripts/` 与 `mcp-servers/` 五个 submodule，零命中）、
   动态派发（`report_deployer` / 本模块零 `getattr`/`eval`）。
2. **运行期**（录制真实部署路径上的 dispatch，覆盖生产与测试两条分支）：
   实际被调到的只有 `run_git_cmd` / `status` / `commit`——正对照三个全部出现，
   证明探针真跑到了产线；被删的五个零调用。

`.push()` 不是「暂时没人用」：**有一条并行路径取代了它**——`report_deployer`
推送走的是 `run_git_cmd("git push origin main")`，绕开本方法。留着就是同一件事
两种做法。另两个（`list_branches` / `diff`）还是**坏的**：它们无条件读
`result["stdout"]`，而 `run_git_cmd` 的异常分支返回的 dict 里根本没有这个键
（实测 `KeyError: 'stdout'`），非仓库目录下则静默返回空结果冒充成功。
零读者恰恰是这两个 bug 从没被发现的原因。

⚠️ **别把「删了测试不红」当成删它们的理由。** 实测：删**活的** `status`
（`report_deployer.py:389` 在用）同样零红——本仓测试根本没覆盖 `status`/`push`/`diff`。
能变红的正对照是 `run_git_cmd` 与 `commit`（各 3 红）。判死靠的是上面两条证据，
不是测试。

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

    # 允许的 git 子命令白名单。
    #
    # ⚠️ **不要按「本类还剩哪些方法」来收窄它。** 它约束的是 `run_git_cmd` 收到的
    # **字符串**，而调用方是直接下发整条命令的：`report_deployer` 就自己传
    # `"git push origin main"` / `"git branch -D …"` / `"git fetch origin"`。
    # v0.45.204 删掉 `push()`/`diff()` 这两个同名方法时，`push`/`diff` 两项**照旧保留**——
    # 方法没了不等于子命令没人用了，跟着删会打断现役 gh-pages / main 部署链路。
    #
    # ⚠️ 已知缺口（v0.45.204 实测，未在本版修）：`report_deployer` 测试模式分支下发的
    # `git checkout` 与 `git reset` **不在**表里，会被静默拒绝，而其后那句
    # 「本地 main 已恢复至 origin/main」是无条件 log 的 —— 失败没传导到下游。
    # 这属另一个改动面，已另开任务，勿在此顺手加项（加了会放宽安全边界且无人验证）。
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
           - run_git_cmd(cmd) ✓
           - status() ✓
           - commit(message, paths=…) ✓
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
