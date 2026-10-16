"""
部署链路里 git 失败必须有人会红（v0.45.210）

固化一次持续半年的静默故障：`report_deployer`（前身 `auto_commit_and_notify`）的测试推送分支
下发 `git checkout` / `git reset --hard`，自 2026-03-01 `GitHubTool` 白名单引入起
就被拒绝——`run_git_cmd` 只 return 不出声，调用方又一律不看返回值，其后
无条件 log「本地 main 已恢复至 origin/main（测试数据不污染生产）」。

实际后果不是「测试环境没更新」，是**测试数据进了生产**：回滚没执行 ⇒ 规则引擎
提交留在本地 main ⇒ 下一次生产推送把它一起送上 origin/main。白名单之后触发 7 次，
origin/main 上恰好 7 个无 `swarm_metadata` 的日报提交（取证全文见
`report_deployer.deploy_and_notify` docstring）。

四组，按「谁会红？」各堵一处：
  1. 生产代码里每个 `run_git_cmd("git <子命令> …")` 都必须在白名单内（静态，AST）
     + 白名单里不许出现破坏性子命令（防有人按「加两项」修回去）
  2. 白名单拒绝 / subprocess 自己炸，`run_git_cmd` 必须出声
  3. 真 git 沙箱：部署（生产与否）不许在本地 main 造提交、不许推任何远端、不许动工作区
     （v0.45.402 阶段 6 起提交 / 推送链已退役；此前这一组是「非生产扫描不许造提交」+「推送失败原因要传到 results」）
  4. 调用方不许丢弃部署函数的返回值

⚠️ 沙箱里**必须**配 `test` remote：生产机上真配着。不配的话旧代码走
`test remote 不存在` 短路，第 3 组对旧 bug 就是恒绿的。
"""

import ast
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_toolbox  # noqa: E402
import report_deployer as rd  # noqa: E402
from _repo_files import own_python_files  # noqa: E402
from agent_toolbox import GitHubTool  # noqa: E402


def _production_sources():
    """被跟踪的**生产** .py。tests/ 排除在外：本文件等测试会故意喂被拒的命令。"""
    files, _ = own_python_files(_ROOT)
    out = []
    for p in files:
        rel = p.relative_to(_ROOT)
        if rel.parts[0] == "tests":
            continue
        try:
            out.append((rel.as_posix(), p.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _callee_name(call: ast.Call):
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


# ═════════════════════════════ 1. 调用点 × 白名单 ═════════════════════════════

def _git_subcommand(arg):
    """`run_git_cmd` 首参的 git 子命令；不是字面量或看不出子命令时返回 None。"""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        prefix, is_fstring = arg.value, False
    elif (isinstance(arg, ast.JoinedStr) and arg.values
          and isinstance(arg.values[0], ast.Constant)):
        prefix, is_fstring = arg.values[0].value, True
    else:
        return None
    parts = prefix.split()
    if len(parts) < 2 or parts[0] != "git":
        return None
    # f"git pu{x}"：第二个词被插值截断，看不出子命令
    if is_fstring and len(parts) == 2 and not prefix[-1:].isspace():
        return None
    return parts[1]


def _pull_call_texts(source: str):
    """`run_git_cmd` 首参为 `git pull …` 的调用：[(行号, 字面量文本)]（f-string 取常量段拼接）。"""
    out = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and _callee_name(node) == "run_git_cmd" and node.args):
            continue
        if _git_subcommand(node.args[0]) != "pull":
            continue
        arg = node.args[0]
        text = arg.value if isinstance(arg, ast.Constant) else "".join(
            v.value for v in arg.values if isinstance(v, ast.Constant))
        out.append((node.lineno, text))
    return out


def _run_git_cmd_sites(source: str):
    """[(行号, 子命令或 None)]"""
    return [
        (node.lineno, _git_subcommand(node.args[0] if node.args else None))
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _callee_name(node) == "run_git_cmd"
    ]


class TestRunGitCmdCallSitesAreWhitelisted:

    def test_every_production_call_site_is_allowed(self):
        allowed = GitHubTool._ALLOWED_GIT_CMDS
        bad = [
            f"{rel}:{line} → {sub!r}"
            for rel, src in _production_sources()
            for line, sub in _run_git_cmd_sites(src)
            if sub not in allowed
        ]
        assert not bad, (
            "这些 run_git_cmd 调用会被白名单拒绝（None = 不是字面量，无法静态核对）：\n  "
            + "\n  ".join(bad)
            + "\n被拒绝的命令不会执行。v0.45.210 前这正是测试推送分支坏了半年的原因。"
            "\n⚠️ 别往白名单里加 checkout/reset 来让它变绿——见 GitHubTool._ALLOWED_GIT_CMDS 注释。"
        )

    def test_scanner_actually_sees_the_live_call_sites(self):
        """正对照：扫描器真读到了生产文件、真解析得了 f-string。
        缺了这条，上一条的「零违规」可能只是一个空扫描。"""
        hits = {(rel, sub) for rel, src in _production_sources()
                for _, sub in _run_git_cmd_sites(src)}
        assert ("production_sync.py", "fetch") in hits     # 字面量（扫描前快进链路）
        assert ("production_sync.py", "pull") in hits      # `git pull --ff-only --no-rebase`
        assert ("production_sync.py", "merge-base") in hits
        # v0.45.402：`push` / `merge-tree` 随 push_main 退役；不许再出现见 TestCommitPushChainStaysRetired
        # v0.45.403：agent_toolbox 里的 add / commit / status / diff 调用点随 GitHubTool.commit/status 退役，
        # 生产代码经 run_git_cmd 下发的子命令现在只剩 production_sync 的快进那几条

    def test_scanner_flags_the_shapes_that_broke_the_test_branch(self):
        """有牙：v0.45.210 前 report_deployer 里的原句必须被抓到。"""
        src = (
            "git.run_git_cmd(f'git checkout -b {_tmp}')\n"
            "git.run_git_cmd('git reset --hard origin/main')\n"
            "git.run_git_cmd(cmd)\n"
            "git.run_git_cmd(f'git {sub} x')\n"
            "git.run_git_cmd(f'git pu{sh}')\n"
            "git.run_git_cmd(f'git branch -D {_tmp}')\n"
        )
        assert [s for _, s in _run_git_cmd_sites(src)] == [
            "checkout", "reset", None, None, None, "branch"]

    def test_whitelist_holds_no_destructive_subcommand(self):
        """「怕它变大」那一侧。变小由第一条管（删了在用的子命令，调用点就红）。

        白名单只按子命令判：放行 `reset` 就放行了 `reset --hard`，放行 `checkout`
        就放行了 `checkout -- <path>`。而生产工作区里常驻未提交的账本
        （hedge_state/ 等，丢了无法回溯重取，2026-09-04 被一次 reset --hard 清掉过）。
        """
        destructive = {"checkout", "reset", "restore", "clean", "switch", "rebase", "update-ref",
                       "merge", "cherry-pick", "revert", "am"}
        assert not (GitHubTool._ALLOWED_GIT_CMDS & destructive), (
            "白名单里出现了会丢弃工作区/改写当前分支的子命令："
            f"{sorted(GitHubTool._ALLOWED_GIT_CMDS & destructive)}。"
            "v0.45.210 已判定不这样修，理由见 report_deployer.deploy_and_notify docstring。"
        )

    def test_production_pull_is_fast_forward_only(self):
        """白名单只看子命令，`pull` 在表里 ⇒ `pull --rebase` / 默认合并式 pull 也会被放行
        （另一 session 实测：打桩 subprocess 后两者都原样下发）。上一条管不到这个马甲。

        冲突时仓库会停在 rebase/merge 进行中而无人在场，账本还在工作区里。
        生产调用点只许 `--ff-only`（v0.45.214 `production_sync.sync_before_scan`）。"""
        bad = [f"{rel}:{line} → {text!r}"
               for rel, src in _production_sources()
               for line, text in _pull_call_texts(src)
               if "--ff-only" not in text.split() or "--rebase" in text.split()]
        assert not bad, "这些 pull 不是只快进：\n  " + "\n  ".join(bad)

    def test_pull_scanner_sees_the_live_call_and_has_teeth(self):
        live = [(rel, t) for rel, src in _production_sources() for _, t in _pull_call_texts(src)]
        assert any(rel == "production_sync.py" for rel, _ in live), f"正对照没扫到：{live}"
        src = ("git.run_git_cmd('git pull --rebase origin main')\n"
               "git.run_git_cmd('git pull origin main')\n"
               "git.run_git_cmd('git pull --ff-only --no-rebase origin main')\n")
        assert [t for _, t in _pull_call_texts(src)] == [
            "git pull --rebase origin main", "git pull origin main",
            "git pull --ff-only --no-rebase origin main"]


# ═════════════════════════════ 2. run_git_cmd 必须出声 ═════════════════════════════

def _records(caplog, logger, min_level):
    return [r for r in caplog.records if r.name == logger and r.levelno >= min_level]


class TestRunGitCmdFailuresAreLoud:

    def test_whitelist_rejection_logs_error(self, tmp_path, caplog):
        g = GitHubTool(repo_path=str(tmp_path))
        with caplog.at_level(logging.WARNING):
            r = g.run_git_cmd("git checkout main")
        assert r == {"success": False, "error": "Git subcommand not allowed: checkout"}
        errs = _records(caplog, "alpha_hive.agent_toolbox", logging.ERROR)
        assert errs and "checkout" in errs[0].getMessage(), \
            "白名单拒绝没出声 —— 调用方不看返回值时，拒绝就等于没发生过"

    def test_non_git_command_logs_error(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            r = GitHubTool(repo_path=str(tmp_path)).run_git_cmd("rm -rf .")
        assert r["success"] is False
        assert _records(caplog, "alpha_hive.agent_toolbox", logging.ERROR)

    def test_allowed_command_stays_quiet(self, tmp_path, caplog):
        """正对照：不是「什么都打 error」。"""
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
        with caplog.at_level(logging.DEBUG):
            r = GitHubTool(repo_path=str(tmp_path)).run_git_cmd("git rev-parse --git-dir")
        assert r["success"] is True
        assert not _records(caplog, "alpha_hive.agent_toolbox", logging.WARNING)

    def test_subprocess_failure_logs_warning(self, tmp_path, monkeypatch, caplog):
        """第二种失败形状（抛异常 ⇒ 只有 error 键）同样要出声。"""
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git fetch", timeout=30)
        # 替换模块内引用，不碰全局 subprocess.run
        monkeypatch.setattr(agent_toolbox, "subprocess", SimpleNamespace(
            run=boom, SubprocessError=subprocess.SubprocessError))
        with caplog.at_level(logging.WARNING):
            r = GitHubTool(repo_path=str(tmp_path)).run_git_cmd("git fetch origin main")
        assert r["success"] is False and "timed out" in r["error"]
        warns = _records(caplog, "alpha_hive.agent_toolbox", logging.WARNING)
        assert warns and "git fetch origin main" in warns[0].getMessage()

    def test_non_utf8_output_raises_instead_of_returning_a_failure(self, tmp_path):
        """已知行为（钉住，不是赞成）：git 输出里有非 UTF-8 字节时，`run_git_cmd` **抛** `UnicodeDecodeError`
        （`text=True` 解码失败，不在它 `except (SubprocessError, OSError)` 的范围内），不是返回 `success=False`。
        所以调用方不能假设「它永远返回 dict」。v0.45.403 退役 `GitHubTool.status()` 时这条从它的测试里迁来
        （原先只被 `status` 的测试顺带钉着；`status` 当时靠自己兜住它，现在没人兜了）。
        若哪天 `run_git_cmd` 改成自己兜住，本条会先红，让人知道调用方的假设变了。
        v0.45.413：白名单收窄后 `status` 不再被允许，改用 `rev-parse --abbrev-ref HEAD`——让 HEAD 指向一个**名字含
        非 UTF-8 字节**的分支。APFS 不许建这种松散 ref 文件，所以直接写 `packed-refs` 与 `HEAD`（git 照常读）。
        生产上碰不到：`production_sync` 的命令输出里分支名是 `main`，git 对路径默认 quotepath——但这是「碰不到」，
        不是「不会抛」。"""
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            env.pop(var, None)

        def git(*a, **kw):
            return subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True, env=env, **kw)
        git("init", "-q", ".")
        sha = git("commit-tree", git("mktree", input=b"").stdout.strip().decode(), "-m", "x").stdout.strip().decode()
        (tmp_path / ".git" / "packed-refs").write_bytes(
            b"# pack-refs with: peeled fully-peeled sorted \n" + sha.encode() + b" refs/heads/bad\xffbr\n")
        (tmp_path / ".git" / "HEAD").write_bytes(b"ref: refs/heads/bad\xffbr\n")
        # 正对照：输出里真有非 UTF-8 字节
        assert b"\xff" in git("rev-parse", "--abbrev-ref", "HEAD").stdout
        with pytest.raises(UnicodeDecodeError):
            GitHubTool(repo_path=str(tmp_path)).run_git_cmd("git rev-parse --abbrev-ref HEAD")


# ═════════════════════════════ 3/4. 真 git 沙箱 ═════════════════════════════

NON_PRODUCTION = {"system_status": "✅ 完成", "opportunities": [{"ticker": "NVDA"}]}
PRODUCTION = {"system_status": "✅ 蜂群协作完成", "swarm_metadata": {"tickers_analyzed": 1}}
LEFTOVER = "alpha-hive-daily-2026-03-13.json"

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    # 若 pytest 从 git hook 里被拉起，继承的 GIT_DIR 会让下面每条 git 命令打到真仓库上
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)

    origin, test, repo = tmp_path / "origin.git", tmp_path / "test.git", tmp_path / "repo"

    def git(*a, cwd=repo, check=True):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check)

    for bare in (origin, test):
        git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "index.html").write_text("prod")
    git("add", "-A")
    git("commit", "-qm", "init")
    git("remote", "add", "origin", str(origin))
    git("remote", "add", "test", str(test))        # 生产机上真配着，见模块 docstring
    git("push", "-q", "origin", "main")

    ghpages = []
    monkeypatch.setattr(rd, "deploy_static_to_ghpages", lambda reporter: ghpages.append(1))
    reporter = SimpleNamespace(
        agent_helper=SimpleNamespace(git=GitHubTool(repo_path=str(repo))),
        date_str="2026-03-13",
    )

    def origin_log():
        return git("--git-dir", str(origin), "log", "--format=%s", "main").stdout.split("\n")[:-1]

    return SimpleNamespace(repo=repo, origin=origin, test=test, git=git,
                           reporter=reporter, ghpages=ghpages, origin_log=origin_log)


def _warnings(caplog):
    return [r.getMessage() for r in _records(caplog, "alpha_hive.report_deployer", logging.WARNING)]


class TestDeployNeverTouchesTheCodeRepo:
    """阶段 6（v0.45.402）：日报部署只推 gh-pages，**不提交、不推送代码仓库**。

    v0.45.402 前这里是两组：非生产扫描不许造提交 / 推远端（2026-03-13 事故：回滚从未执行，测试数据进了生产），
    以及生产分支两种推送失败形状的原因要传到 results 与 warning。提交 / 推送链整体退役后两组的前提都不在了，
    换成它们共同的上位不变式：无论生产与否，部署都不许改本地 HEAD、不许推任何远端、不许动工作区。
    ⚠️ 勿重建提交链：数据在数据根，往公开仓库提交数据正是阶段 6 要终结的事（见 `deploy_and_notify` docstring）。
    """

    def _assert_repo_untouched(self, sandbox, head):
        assert sandbox.git("rev-parse", "HEAD").stdout == head, "部署在本地 main 上造了提交"
        assert sandbox.git("ls-remote", str(sandbox.test)).stdout == ""
        assert sandbox.origin_log() == ["init"], f"部署往 origin/main 推了东西：{sandbox.origin_log()}"

    def test_non_production_deploys_nothing(self, sandbox, caplog):
        (sandbox.repo / LEFTOVER).write_text('{"system_status": "✅ 完成"}')
        head = sandbox.git("rev-parse", "HEAD").stdout
        with caplog.at_level(logging.INFO):
            res = rd.deploy_and_notify(sandbox.reporter, NON_PRODUCTION)
        self._assert_repo_untouched(sandbox, head)
        assert not sandbox.ghpages
        assert res["deploy_env"] == "none"
        assert res["gh_pages"] == {"success": False, "skipped": "non_production"}
        assert "git_push" not in res and "git_commit" not in res, \
            "返回里又出现了提交 / 推送字段：下游若还在读它们，退役就只做了一半"
        assert not any("已恢复" in r.getMessage() for r in caplog.records), \
            "又出现了「已恢复」式的无条件成功日志"

    @pytest.mark.parametrize("report", [
        PRODUCTION,
        {"distill_mode": "llm_enhanced"},
        {"swarm_results": {"NVDA": {"distill_mode": "llm_enhanced"}}},
    ], ids=["swarm", "llm", "llm-per-ticker"])
    def test_production_deploys_ghpages_only(self, sandbox, report):
        """正对照：生产分支仍部署 gh-pages（网站不受影响），但不提交、不推送。"""
        (sandbox.repo / "index.html").write_text("prod-2")
        head = sandbox.git("rev-parse", "HEAD").stdout
        res = rd.deploy_and_notify(sandbox.reporter, report)
        assert res["deploy_env"] == "production"
        assert sandbox.ghpages == [1], "生产部署没去部署 gh-pages"
        self._assert_repo_untouched(sandbox, head)
        assert "git_push" not in res and "git_commit" not in res

    def test_does_not_touch_the_working_tree(self, sandbox):
        """部署不许靠清工作区做任何事（那就是 reset --hard）：进行中的改动与未提交文件原样留着。"""
        (sandbox.repo / "hedge_state").mkdir()
        (sandbox.repo / "hedge_state" / "trades.jsonl").write_text("leg\n")
        (sandbox.repo / "index.html").write_text("进行中的改动")
        for report in (NON_PRODUCTION, PRODUCTION):
            rd.deploy_and_notify(sandbox.reporter, report)
        assert (sandbox.repo / "hedge_state" / "trades.jsonl").read_text() == "leg\n"
        assert (sandbox.repo / "index.html").read_text() == "进行中的改动"
        assert "index.html" in sandbox.git("status", "--porcelain").stdout, "改动应仍是未提交状态"

    def test_ghpages_outcome_key_is_always_present(self, sandbox, monkeypatch):
        """`gh_pages` 键始终存在（编排器用 scan_timing.extra.gh_pages 的存在当「main() 已跑到最后一步」的证据）：
        部署函数返回非 dict、或抛异常，也各给一个 success=False 的结局，不许缺键。"""
        monkeypatch.setattr(rd, "deploy_static_to_ghpages", lambda reporter: None)
        res = rd.deploy_and_notify(sandbox.reporter, PRODUCTION)
        assert res["gh_pages"]["success"] is False and "未返回结局" in res["gh_pages"]["error"]

        def boom(reporter):
            raise OSError("disk gone")
        monkeypatch.setattr(rd, "deploy_static_to_ghpages", boom)
        res = rd.deploy_and_notify(sandbox.reporter, PRODUCTION)
        assert res["gh_pages"]["success"] is False and "disk gone" in res["gh_pages"]["error"]


class TestAllowlistMatchesProductionCallSites:
    """`GitHubTool._ALLOWED_GIT_CMDS` 与生产调用点**精确相等**，且会改本地状态的两个子命令有运行期参数约束（v0.45.413）。

    「怕它变大也怕它变小」两条断言各一条：上面的 `test_every_production_call_site_is_allowed` 是子集方向
    （调用点必须在白名单里——删了现役子命令会红）；这里补**超集方向**（白名单不许有无人调用的项——
    有人顺手加 push / commit 会红）。只有子集那条时，白名单会悄悄积累无人使用的写操作，
    v0.45.402~403 前它就攒了 13 个。
    """

    def test_allowlist_equals_the_subcommands_production_actually_uses(self):
        used = {sub for _, src in _production_sources() for _, sub in _run_git_cmd_sites(src)}
        assert used, "扫描器没读到任何调用点——下面的相等断言会空转"
        assert GitHubTool._ALLOWED_GIT_CMDS == used, (
            f"白名单与生产调用点不一致：只在白名单里（无人调用，删掉）{sorted(GitHubTool._ALLOWED_GIT_CMDS - used)}；"
            f"只在调用点里（会被拒绝）{sorted(used - GitHubTool._ALLOWED_GIT_CMDS)}")

    def test_exact_args_cover_exactly_the_state_changing_subcommands(self):
        """会改本地状态的子命令（fetch 更新远端跟踪 ref，pull 改工作区 / 历史）必须都有精确参数约束；
        约束只能落在白名单内的子命令上。"""
        assert set(GitHubTool._EXACT_GIT_ARGS) == {"fetch", "pull"}
        assert set(GitHubTool._EXACT_GIT_ARGS) <= GitHubTool._ALLOWED_GIT_CMDS

    def test_exact_args_match_what_production_sends(self):
        """约束值必须就是生产下发的那条字符串——否则收窄把现役调用也拒了，扫描前同步会停摆。"""
        sent = {}
        for _, src in _production_sources():
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Call) and _callee_name(node) == "run_git_cmd" and node.args:
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        parts = a.value.split()
                        if len(parts) > 1 and parts[1] in GitHubTool._EXACT_GIT_ARGS:
                            sent[parts[1]] = tuple(parts[2:])
        assert sent == GitHubTool._EXACT_GIT_ARGS, sent

    @pytest.mark.parametrize("cmd", [
        "git pull --rebase origin main", "git pull origin main", "git pull --ff-only origin main",
        "git pull --ff-only --no-rebase origin main --force", "git pull --ff-only --no-rebase origin dev",
        "git fetch origin main:main", "git fetch --prune origin main", "git fetch origin", "git fetch --all",
        "git fetch origin main --force",
    ])
    def test_state_changing_commands_with_other_args_are_refused_and_not_run(self, cmd, tmp_path, monkeypatch, caplog):
        calls = []
        monkeypatch.setattr(agent_toolbox, "subprocess", SimpleNamespace(
            run=lambda *a, **k: calls.append(a), SubprocessError=subprocess.SubprocessError))
        with caplog.at_level(logging.WARNING):
            r = GitHubTool(repo_path=str(tmp_path)).run_git_cmd(cmd)
        assert r["success"] is False and "not allowed" in r["error"], r
        assert calls == [], f"被拒绝的命令仍被执行了：{calls}"
        assert _records(caplog, "alpha_hive.agent_toolbox", logging.ERROR), "参数被拒没出声（调用方不看返回值时等于没发生）"

    @pytest.mark.parametrize("cmd", [
        "git fetch origin main", "git pull --ff-only --no-rebase origin main",
        "git rev-parse --verify HEAD", "git rev-parse --abbrev-ref HEAD",
        "git merge-base --is-ancestor HEAD HEAD", "git rev-list --count HEAD",
    ])
    def test_production_commands_are_still_executed(self, cmd, tmp_path, monkeypatch):
        """正对照：收窄没有把现役命令一并拒了（上一条是拒，这条是放；缺了它，上一条的绿可能只是「什么都拒」）。"""
        calls = []
        monkeypatch.setattr(agent_toolbox, "subprocess", SimpleNamespace(
            run=lambda *a, **k: calls.append(a[0]) or SimpleNamespace(returncode=0, stdout="", stderr=""),
            SubprocessError=subprocess.SubprocessError))
        r = GitHubTool(repo_path=str(tmp_path)).run_git_cmd(cmd)
        assert r["success"] is True and calls == [cmd.split()], (r, calls)

    @pytest.mark.parametrize("sub", [
        "push", "commit", "add", "stash", "branch", "tag", "remote", "status", "log", "diff", "show",
        "merge-tree", "commit-tree", "checkout", "reset", "clean", "restore", "rebase", "merge",
    ])
    def test_every_other_subcommand_is_refused_and_not_run(self, sub, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(agent_toolbox, "subprocess", SimpleNamespace(
            run=lambda *a, **k: calls.append(a), SubprocessError=subprocess.SubprocessError))
        r = GitHubTool(repo_path=str(tmp_path)).run_git_cmd(f"git {sub} x")
        assert r == {"success": False, "error": f"Git subcommand not allowed: {sub}"}
        assert calls == []


class TestCommitPushChainStaysRetired:
    """阶段 6（v0.45.402）退役的日报提交 / 推送链不许被接回去（墓碑 + AST）。

    为什么需要：`GitHubTool._ALLOWED_GIT_CMDS` 仍放行 `push` / `commit`（通用工具类，gh-pages 之外别处可能要用），
    白名单测试拦不住「有人把 `git push origin main` 又写回部署路径」。往公开仓库提交数据正是阶段 6 要终结的事。
    """

    def test_tombstones(self):
        import production_sync
        assert not hasattr(production_sync, "push_main"), "push_main 被接回去了（阶段 6 退役，勿重建）"
        for name in ("commit", "status", "_staged_names", "_add_pathspec", "_parse_porcelain_z", "_failure_reason",
                     "_rename_sources_pointing_outside", "_ADD_RETRY_DELAY_S"):
            assert not hasattr(GitHubTool, name), f"GitHubTool.{name} 被接回去了（v0.45.403 退役，勿重建提交能力）"
        assert not hasattr(agent_toolbox.AgentHelper, "summary"), "AgentHelper.summary 被接回去了"
        for name in ("auto_commit_and_notify", "REPORT_ARTIFACT_PATHS", "_is_report_artifact", "_git_modified_files"):
            assert not hasattr(rd, name), f"report_deployer.{name} 被接回去了（阶段 6 退役，勿重建）"
        import scan_timing
        for name in ("git_push_summary", "git_commit_summary"):
            assert not hasattr(scan_timing, name), f"scan_timing.{name} 被接回去了"

    def test_no_production_code_pushes_via_run_git_cmd(self):
        subs = {sub for _, src in _production_sources() for _, sub in _run_git_cmd_sites(src)}
        # v0.45.403 起 commit / add 也不许有：提交能力随 GitHubTool.commit() 一起退役，
        # 但白名单 `_ALLOWED_GIT_CMDS` 仍放行它们，只有这条 AST 能拦「又写回一个 git commit」
        for banned in ("push", "commit", "add"):
            assert banned not in subs, f"生产代码里出现了 run_git_cmd('git {banned} …')：日报 / 部署不再向代码仓库提交或推送数据"
        # 正对照：扫描器确实读到了别的子命令（空扫描会让上一行恒绿）
        assert {"fetch", "pull"} <= subs


# ═════════════════════════════ 调用方不许丢弃返回值 ═════════════════════════════

def _discarded_deploy_calls(source: str):
    """`deploy_and_notify(...)` 作为裸语句（返回值直接丢弃）的行号。"""
    return [
        node.lineno for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and _callee_name(node.value) == "deploy_and_notify"
    ]


class TestCallersReadTheResult:

    def test_no_production_caller_discards_the_result(self):
        bad = [f"{rel}:{line}" for rel, src in _production_sources()
               for line in _discarded_deploy_calls(src)]
        assert not bad, (
            "这些调用丢弃了 deploy_and_notify 的返回值 —— gh-pages 部署失败时调用方照样报成功：\n  "
            + "\n  ".join(bad))

    def test_scanner_sees_every_caller(self):
        """正对照：CLI 调用点与 reporter 上的委托方法都要被扫到。
        （v0.45.316 前还有 GUI 的 `gui/interactions.py`，已随 gui/ 整体删除。）"""
        sites = {rel for rel, src in _production_sources()
                 for node in ast.walk(ast.parse(src))
                 if isinstance(node, ast.Call) and _callee_name(node) == "deploy_and_notify"}
        assert "alpha_hive_daily_report.py" in sites

    def test_scanner_flags_a_discarded_result(self):
        """有牙：v0.45.210 前 gui/interactions.py（v0.45.316 已删）的原句。"""
        src = ("reporter.deploy_and_notify(report)\n"
               "sync = reporter.deploy_and_notify(report)\n"
               "_p = reporter.deploy_and_notify(report).get('gh_pages')\n")
        assert _discarded_deploy_calls(src) == [1]
