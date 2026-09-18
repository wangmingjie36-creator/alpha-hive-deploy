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
（当时 `report_deployer.py:389` 在用）同样零红——本仓测试根本没覆盖 `status`/`push`/`diff`。
能变红的正对照是 `run_git_cmd` 与 `commit`（各 3 红）。判死靠的是上面两条证据，
不是测试。（v0.45.211 起 `status` 有了 `tests/test_github_tool_status.py`，删它现在会红；
上面这句是 v0.45.204 当时的实测，留作「零红对死活零信息量」的校准记录。）

要加 MCP 工具 → `alpha_hive_mcp.py`；要发 Slack → `slack_report_notifier.py`。
"""

import os
import shlex
import subprocess
import time
from typing import Dict, List, Optional, Any

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

    def status(self) -> Dict[str, Any]:
        """工作区改动清单（`git status --porcelain -z`）

        返回契约 —— 调用方靠 `"modified_files" in r` 区分「失败」与「干净」
        （`report_deployer._git_modified_files`）：

          成功 `{"success": True,  "modified_files": [路径…], "status": …}`，空列表才是「干净」
          失败 `{"success": False, "error": 非空说明}`，**没有** `modified_files` 键

        **不抛异常**：`run_git_cmd` 的两种失败形状、以及下面第 3 条的解码失败，一律走返回值。

        v0.45.211 修了三处，均实测复现（守卫 `tests/test_github_tool_status.py`）：

        1. 失败时原先返回 `{"error": result.get("stderr")}`。`run_git_cmd` 的第二种失败形状
           （子进程自己炸）只有 `error` 键 ⇒ 得到 `{"error": None}`，失败原因整个丢了。
        2. 原先用 `line.split()[-1]` 解析无 `-z` 的输出。git 会给含空格 / 非 ASCII / 引号的
           路径加引号并八进制转义：`?? "report 2.json"` → `2.json"`，`日报.md` →
           `"\\346\\227\\245…"`。本机 ~/Desktop 在 iCloud 下持续造「xxx 2.json」式副本，
           不是理论情形；解析出的名字进警告与 `results`，曾把**实际已被白名单提交**的
           副本报成「跳过」。`-z` 下 git 不加引号、不转义，且与 `core.quotepath` 无关。
        3. `-z` 的代价：路径以原始字节输出，`run_git_cmd` 是 text 模式严格解码，索引里有
           非 UTF-8 路径时抛 `UnicodeDecodeError`（APFS 建不出这种文件，但别的系统提交进
           索引的条目可以）。旧写法输出纯 ASCII 不会抛 —— 这条抛出路径是换 `-z` 新引入的，
           在此收成失败返回，不让它越过调用方的「失败 / 干净」判断。
        """
        try:
            result = self.run_git_cmd("git status --porcelain -z")
            if result["success"]:
                files = self._parse_porcelain_z(result["stdout"])
                return {"success": True, "modified_files": files,
                        "status": "✅ Clean" if not files else "⚠️ Dirty"}
        except ValueError as e:  # 含 UnicodeDecodeError；解析器遇到不认识的条目也抛它
            _log.warning("git status 输出无法解析：%s: %s", type(e).__name__, e)
            return {"success": False,
                    "error": f"git status 输出无法解析：{type(e).__name__}: {e}"}
        reason = (result.get("stderr") or result.get("error") or "").strip()
        return {"success": False,
                "error": reason or f"git status 失败（returncode={result.get('returncode')}，无错误输出）"}

    @staticmethod
    def _failure_reason(result: Dict[str, Any]) -> str:
        """`run_git_cmd` 的失败结果 → 一行原因，两种失败形状都认（`stderr` / 只有 `error`）。

        只取首个非空行：锁错误后面跟五行通用建议，带路径与「File exists」的是首行。
        """
        text = (result.get("stderr") or result.get("error") or "").strip()
        if not text:
            return f"returncode={result.get('returncode')}，无错误输出"
        return text.splitlines()[0].strip()

    @staticmethod
    def _parse_porcelain_z(out: str) -> List[str]:
        """`git status --porcelain -z`（v1）→ 路径列表。

        每条是 `XY 路径\\0`。改名 / 复制条目后面**另跟一段**原路径：`R  新\\0旧\\0`
        （与无 `-z` 时的 `旧 -> 新` 顺序相反）。只收新路径；漏了跳过原路径这一步，
        原路径会被当成一条以它自己前两个字符为状态码的独立条目。
        """
        files: List[str] = []
        fields = iter(out.split("\0"))
        for entry in fields:
            if not entry:
                continue  # 末尾 NUL 之后的空段
            if len(entry) < 4 or entry[2] != " ":
                raise ValueError(f"不认识的 porcelain 条目：{entry!r}")
            files.append(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                next(fields, None)
        return files

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

        失败返回 `{"success": False, "error": 原因}`。v0.45.225 前白名单分支把**所有** add
        失败都当成「该产物本次未生成」容忍，一条没暂存上就回「白名单未匹配到任何文件」——
        残留 `.git/index.lock` 时也是这句（git 先拿锁再匹配 pathspec，锁在时每条都是锁错误）。
        现在只容忍 pathspec 未匹配；别的失败的原因原样回给调用方（进 status.json 与告警）。

        **部分失败**（v0.45.227）：别的进程只是**短暂**占着索引锁（刷新索引的 `git status`，
        生产克隆上实测持锁 55–72ms）时，只挂撞上的那一条，其余照常暂存、提交照样成功。
        v0.45.225 这里写着「实测想不出触发条件，锁是整库级的」——那是推理不是实测，且把
        「锁一直在」（全挂、提交也挂、会告警）和「锁占一下」（只挂一条、报成功、零告警）混成了一件事。
        现在：失败的条目等 `_ADD_RETRY_DELAY_S` 后统一重试一次；重试后仍失败的，提交照做，
        原因放进 `add_errors`（去重后的首行列表）。「提交成功但产物没全进 git」这件事本身由
        `report_deployer` 提交后再看一次工作区来判，不靠这里列举失败原因。

        **只提交白名单**（v0.45.236）：上面的白名单只管「我们暂存什么」，而裸 `git commit -m` 提交的是
        **整个索引**。生产 checkout 被多个 session 共用，别人 `git add` 了还没提交的代码会被当成日报提交、
        推上 main（2026-07-30 事故同形，换了条路；那时 `left_artifacts=0`、零告警，
        `skipped_non_artifacts` 还说它被「跳过」了）。现在按 `_staged_names` 列出的确切文件名
        `git commit -- <名字>`（--only 语义：别人的暂存原样留在索引里；pre-commit 钩子看到的是只含
        这些名字的临时索引）。白名单内没有已暂存的改动时**不跑** `git commit`——退回裸提交正是那条漏洞。
        不传 `paths` 的 `add -A` 分支本来就是全量，不受影响。
        """
        add_reasons: List[str] = []
        if paths:
            # 逐条暂存：pathspec 无匹配是预期失败（该产物本次未生成），容忍；别的失败要带原因回去
            staged_any = False
            failed: Dict[str, str] = {}
            for p in paths:
                staged, reason = self._add_pathspec(p)
                staged_any |= staged
                if reason:
                    failed[p] = reason
            if failed:
                _log.warning("白名单 git add 失败 %d 条，%.1fs 后重试一次：%s", len(failed),
                             self._ADD_RETRY_DELAY_S, "；".join(dict.fromkeys(failed.values())))
                time.sleep(self._ADD_RETRY_DELAY_S)
                for p in list(failed):
                    staged, reason = self._add_pathspec(p)
                    staged_any |= staged
                    if reason:
                        failed[p] = reason
                    else:
                        del failed[p]
                if failed:
                    _log.warning("白名单 git add 重试后仍失败 %d 条（不会进本次提交）：%s",
                                 len(failed), ", ".join(failed))
            # 同一把锁对每条 pathspec 报同一行，去重后才放得进 status.json 的 300 字
            add_reasons = list(dict.fromkeys(failed.values()))
            if not staged_any:
                if add_reasons:
                    return {"success": False, "error": "git add 失败：" + "；".join(add_reasons)}
                return {"success": False, "error": "白名单未匹配到任何文件"}
            # v0.45.236：只提交白名单内已暂存的确切文件名。裸 `git commit -m` 提交的是整个索引——
            # 生产 checkout 是多个 session 共用的，别人 `git add` 了没提交的代码会被当成日报推上 main。
            names, list_error = self._staged_names(paths)
            if names is None:
                return {"success": False, "error": f"列白名单内已暂存文件失败（未提交）：{list_error}",
                        **({"add_errors": add_reasons} if add_reasons else {})}
            if not names:
                # 不许退回裸 `git commit`：那正是会把别人的暂存提交掉的那条路
                result = {"success": False, "message": "nothing to commit（白名单内没有已暂存的改动）"}
                if add_reasons:
                    result["add_errors"] = add_reasons
                return result
            # 整条命令写成一个 f-string：拼接式（`+`）会让 test_git_failures_are_visible 的白名单 AST 核对读不出子命令
            name_args = " ".join(shlex.quote(n) for n in names)
            commit = self.run_git_cmd(f"git commit -m {shlex.quote(message)} -- {name_args}")
        else:
            stage = self.run_git_cmd("git add -A")
            if not stage["success"]:
                return {"success": False, "error": f"git add 失败：{self._failure_reason(stage)}"}
            commit = self.run_git_cmd(f"git commit -m {shlex.quote(message)}")
        result = {
            "success": commit["success"],
            "message": commit.get("stdout") or commit.get("stderr"),
            "details": commit
        }
        if add_reasons:
            result["add_errors"] = add_reasons
        return result

    # 白名单 add 失败（pathspec 未匹配除外）后等多久重试。要长过别的进程占锁的时长：
    # 普通 `git status` 只在写回刷新后的索引时持锁：生产 checkout 的 APFS 克隆上实测 55–72ms
    # （即使所有文件 mtime 都变了、status 本身跑 0.7–1.5s，持锁也不超过 72ms）。
    _ADD_RETRY_DELAY_S = 1.0

    def _staged_names(self, paths: List[str]):
        """白名单 pathspec 内、相对 HEAD 已暂存的**确切文件名** → `(名字列表, None)`；失败 `(None, 原因)`。

        四处都实测过（`tests/test_github_tool_commit.py::TestOnlyTheWhitelistIsCommitted`）：
        - 用确切名字而不是 pathspec 去提交：`git add -- vrp_state/`（目录里只有被忽略的文件）回 0，
          `git commit -- vrp_state/` 却报「did not match any file(s) known to git」、整个提交失败。
        - `--no-renames`：默认的改名检测让 `--name-only` 只列新名字，内容相近的前后两天快照会被配成改名
          ⇒ 旧文件的删除留在索引里没提交。
        - `-z`：iCloud 副本名带空格，不加引号、不转义。
        - **排除「跨出白名单」的 rename 源**（v0.45.248）：上面的 `--no-renames` + pathspec 过滤有个副作用——
          别的 session 已暂存一个 rename、旧路径在白名单目录内、新路径不在（如数据根迁移把 `hedge_state/x.json`
          挪到 `data_root/x.json`）时，pathspec 只放行旧路径（一条孤立的 `D`），看不到新路径。若照单全收，
          我们会把这半个 rename 当成「日报」提交掉——旧路径的删除进了跟对方毫无关系的提交，新路径仍留着孤零零
          地暂存着，对方的原子操作被我们拦腰斩断。方向反过来（旧路径不在白名单、新路径在）无害：我们会把新路径
          当成一次全新的 add 提交，不牵扯旧路径那半，对方的暂存原样留着。
        """
        spec = " ".join(shlex.quote(p) for p in paths)
        r = self.run_git_cmd(f"git diff --cached --no-renames --name-only -z -- {spec}")
        if not r["success"]:
            return None, self._failure_reason(r)
        names = [n for n in r["stdout"].split("\0") if n]
        if not names:
            return names, None

        rr = self.run_git_cmd("git diff --cached --name-status -z")
        if not rr["success"]:
            return None, self._failure_reason(rr)
        foreign = self._rename_sources_pointing_outside(rr["stdout"], set(names))
        if foreign:
            names = [n for n in names if n not in foreign]
        return names, None

    @staticmethod
    def _rename_sources_pointing_outside(name_status_z: str, in_scope: set) -> set:
        """解析 `git diff --cached --name-status -z`（默认改名检测），找出「旧路径在 `in_scope`
        里、新路径不在」的 rename——这些旧路径要从我们的提交里剔除，见 `_staged_names` 的说明。

        `-z` 下每条记录是 NUL 分隔：普通改动 `<状态><NUL><路径><NUL>`，改名是
        `<R加分数><NUL><旧路径><NUL><新路径><NUL>`——区分靠状态字母，不靠数固定字段数。
        ⚠️ **只认 `R`，不认 `C`（复制）**：实测过——不带 `-C` 时 git 从不报 `C`；带 `-C` 时，
        复制源若在索引里未改动（最常见的复制形状），同样不出现在这份 diff 里（它压根不是「改动」），
        没有旧路径可供剔除。带 `C` 判断会是永远走不到的死分支，故不写——真加 `-C` 探测复制前，
        先想清楚“源未改动的复制”这条主路径要怎么处理，而不是先加个测不到的分支装作已经处理了。
        """
        fields = iter(name_status_z.split("\0"))
        out = set()
        for status in fields:
            if not status:
                continue
            if status[0] == "R":
                old, new = next(fields, None), next(fields, None)
                if old in in_scope and new not in in_scope:
                    out.add(old)
            else:
                next(fields, None)  # 普通改动：跳过它的路径字段
        return out

    def _add_pathspec(self, pathspec: str):
        """`git add -- <pathspec>` → `(暂存成功?, 失败原因)`；pathspec 未匹配是 `(False, None)`。"""
        r = self.run_git_cmd(f"git add -- {shlex.quote(pathspec)}")
        if r["success"]:
            return True, None
        if "did not match any files" in (r.get("stderr") or ""):
            return False, None
        return False, self._failure_reason(r)


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
