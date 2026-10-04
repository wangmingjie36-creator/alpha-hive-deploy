#!/usr/bin/env python3
"""
🚀 Agent Toolbox —— 日报流程用的 Git 封装

唯一在产的能力是 `GitHubTool`（子命令白名单 + 无 shell 模式）：`production_sync.py` 扫描前快进用
`run_git_cmd`，`report_deployer.py` 的 gh-pages 部署经 `AgentHelper.git.repo_path` 取仓库路径。

⚠️ **v0.45.403 退役了 `GitHubTool.commit()` / `status()` 及其全部辅助**（`_staged_names` /
`_rename_sources_pointing_outside` / `_add_pathspec` / `_failure_reason` / `_parse_porcelain_z` /
`_ADD_RETRY_DELAY_S`）与 `AgentHelper.summary()` / 本文件的 `main()` 演示。唯一读者 `report_deployer`
的日报白名单提交链已在 v0.45.402（数据根迁移阶段 6 ④）退役；往公开仓库提交数据正是阶段 6 要终结的事，
**勿把提交能力接回来**（墓碑 + AST 守卫：`tests/test_git_failures_are_visible.py::TestCommitPushChainStaysRetired`）。
判死的两条证据（照下面 v0.45.204 的做法）：
  1. 静态：`git ls-files | xargs grep`——`.status(` / `.commit(` 作为 `GitHubTool` 成员的读者只剩两个测试文件
     （`test_github_tool_status.py` / `test_github_tool_commit.py`，随本版删除）与本文件自己的演示；仓库外
     （`~/.claude/scripts/`、`~/.claude/scheduled-tasks/`、LaunchAgents、`mcp-servers/`）零命中。
  2. 运行期：给 `GitHubTool` 全部方法挂探针，跑部署 + 同步相关 155 条测试，生产代码实际调到的**只有**
     `run_git_cmd`（`production_sync.py` 66 次，正对照）；`status` / `commit` 零次。

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
（当时 `report_deployer.py:389` 在用）同样零红——本仓测试根本没覆盖 `status`/`push`/`diff`。
能变红的正对照是 `run_git_cmd` 与 `commit`（各 3 红）。判死靠的是上面两条证据，
不是测试。（v0.45.211 起 `status` 有了 `tests/test_github_tool_status.py`，删它现在会红；
上面这句是 v0.45.204 当时的实测，留作「零红对死活零信息量」的校准记录。）

要加 MCP 工具 → `alpha_hive_mcp.py`；要发 Slack → `slack_report_notifier.py`。
"""

import shlex
import subprocess
from typing import Dict, Any

from hive_logger import PATHS, get_logger

_log = get_logger("agent_toolbox")

# ==================== GitHub 工具 ====================

class GitHubTool:
    """GitHub 操作（替代 GitHub MCP）"""

    def __init__(self, repo_path: str = None):
        # 数据根迁移阶段 4：此前默认值读 `ALPHA_HIVE_HOME`（数据根变量），
        # 与 `PATHS.home` 撞在一起——阶段 5 把 `ALPHA_HIVE_HOME` 改指
        # `~/alpha-hive-data` 后，git plumbing 会跟着跑到一个没有 `.git`
        # 的数据目录，main 提交/推送与 gh-pages 部署一起失效。改读
        # `PATHS.git_repo_root`（专用 `ALPHA_HIVE_GIT_REPO`，生产从不设，
        # 兜底 `__file__`）——代码仓库的位置不随数据搬迁改变。
        self.repo_path = repo_path or str(PATHS.git_repo_root)

    # 允许的 git 子命令白名单。
    #
    # ⚠️ **不要按「本类还剩哪些方法」来收窄它。** 它约束的是 `run_git_cmd` 收到的
    # **字符串**，而调用方是直接下发整条命令的：`report_deployer` 就自己传
    # `"git push origin main"` / `"git branch -D …"` / `"git fetch origin"`。
    # v0.45.204 删掉 `push()`/`diff()` 这两个同名方法时，`push`/`diff` 两项**照旧保留**——
    # 方法没了不等于子命令没人用了，跟着删会打断现役 gh-pages / main 部署链路。
    #
    # ⚠️ **不要加 `checkout` / `reset` / `restore` / `clean`。** v0.45.210 处置过一次：
    # `report_deployer` 旧测试推送分支下发 `checkout` 与 `reset --hard`，自 2026-03-01
    # 本表引入起就被拒绝（本表漏列了三天前已存在的调用方，不是有意排除）。修法是
    # **撤掉那条分支**，不是加项——白名单只按子命令判，放行 `reset` 就放行了
    # `reset --hard`，而这个仓库的工作区里常驻着未提交、丢了无法回溯重取的账本
    # （`hedge_state/` 等，2026-09-04 就被一次 `reset --hard` 清掉过）。
    # 守卫：`tests/test_git_failures_are_visible.py`（调用点必须全在表内 + 表内不许出现破坏性子命令）。
    #
    # v0.45.214 加了四项，全部**不动工作区、不移动任何 ref**（`production_sync`
    # 在对象层合并后推送，治生产推送六次 non-fast-forward）：
    #   `rev-list` / `merge-base` 只读；`merge-tree --write-tree` 与 `commit-tree`
    #   只往对象库写树与提交对象，引用照旧只由 `push` 改动远端。
    # ⚠️ `pull` 本来就在表里，而白名单只看子命令 ⇒ `pull --rebase` 也会被放行。
    # 生产调用点只许 `pull --ff-only`，由守卫的 AST 扫描单独核对。
    _ALLOWED_GIT_CMDS = {
        "status", "log", "diff", "branch", "add", "commit", "push",
        "pull", "fetch", "remote", "show", "tag", "stash", "rev-parse",
        "rev-list", "merge-base", "merge-tree", "commit-tree",
    }

    def run_git_cmd(self, cmd: str) -> Dict[str, Any]:
        """执行 Git 命令（白名单 + 无 shell 模式）"""
        try:
            parts = shlex.split(cmd)
            if not parts or parts[0] != "git":
                _log.error("run_git_cmd 拒绝非 git 命令：%r", cmd)
                return {"success": False, "error": "Only git commands allowed"}
            subcmd = parts[1] if len(parts) > 1 else ""
            if subcmd not in self._ALLOWED_GIT_CMDS:
                # 调用方传的都是写死的字符串 ⇒ 被拒绝一定是代码写错了，不是运行期偶发。
                # v0.45.210 前这里只 return、调用方又不看返回值 ⇒ 拒绝等于没发生过：
                # 旧测试推送分支的 checkout/reset 被拒了半年，日志里一个字都没有。
                _log.error("run_git_cmd 拒绝白名单外的子命令 %r（命令未执行）：%s", subcmd, cmd)
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
            # 第二种失败形状：没有 stdout/stderr 键，只有 error。调用方若只读 stderr 会拿到空串。
            _log.warning("run_git_cmd 执行失败：%s（%s: %s）", cmd, type(e).__name__, e)
            return {"success": False, "error": str(e)}


# ==================== Agent 助手 ====================

class AgentHelper:
    """Agent 使用的统一工具集（现仅剩 Git 一条）。

    历史上还挂过 `.fs`（`FilesystemTool`）与 `.notify`（`NotificationTool`），
    v0.45.198 随那两个类一并删除——全仓 14 处 `agent_helper.*` 访问**全部**是
    `.git`，另两个属性从未被读过。新增属性前请先确认它真有读者。

    ⚠️ **动这个 `__init__` 时别指望测试接住你。** v0.45.198 实测：把
    `self.git` 整个拿掉，`tests/test_pipeline.py` + `tests/test_report_deployer_whitelist.py`（后者已于 v0.45.402 删除）
    共 77 条**全绿**——前者 `r.agent_helper = MagicMock()` 把整个 helper 换掉了
    （真 `__init__` 从没执行，`MagicMock` 访问 `.git` 会自动造一个出来），
    后者直接 `GitHubTool(repo_path=...)` 构造。改这里要靠数读者，不是靠跑测试。
    """

    def __init__(self):
        self.git = GitHubTool()
