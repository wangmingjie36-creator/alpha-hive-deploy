"""
部署链路里 git 失败必须有人会红（v0.45.210）

固化一次持续半年的静默故障：`report_deployer.auto_commit_and_notify` 的测试推送分支
下发 `git checkout` / `git reset --hard`，自 2026-03-01 `GitHubTool` 白名单引入起
就被拒绝——`run_git_cmd` 只 return 不出声，调用方又一律不看返回值，其后
无条件 log「本地 main 已恢复至 origin/main（测试数据不污染生产）」。

实际后果不是「测试环境没更新」，是**测试数据进了生产**：回滚没执行 ⇒ 规则引擎
提交留在本地 main ⇒ 下一次生产推送把它一起送上 origin/main。白名单之后触发 7 次，
origin/main 上恰好 7 个无 `swarm_metadata` 的日报提交（取证全文见
`report_deployer.auto_commit_and_notify` docstring）。

四组，按「谁会红？」各堵一处：
  1. 生产代码里每个 `run_git_cmd("git <子命令> …")` 都必须在白名单内（静态，AST）
     + 白名单里不许出现破坏性子命令（防有人按「加两项」修回去）
  2. 白名单拒绝 / subprocess 自己炸，`run_git_cmd` 必须出声
  3. 真 git 沙箱：非生产扫描不许在本地 main 造提交、不许推任何远端
     （v0.45.213：「下一次推送不带非生产内容」改从 CLI 入口进，不再 xfail）
  4. 生产分支两种失败形状的原因都要传到 results 与 warning；调用方不许丢弃返回值

⚠️ 沙箱里**必须**配 `test` remote：生产机上真配着。不配的话旧代码走
`test remote 不存在` 短路，第 3 组对旧 bug 就是恒绿的。
"""

import ast
import logging
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
        assert ("report_deployer.py", "push") in hits      # 字面量
        assert ("agent_toolbox.py", "add") in hits         # f"git add -- {…}"
        assert ("agent_toolbox.py", "commit") in hits      # f"git commit -m {…}"

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
        destructive = {"checkout", "reset", "restore", "clean", "switch", "rebase", "update-ref"}
        assert not (GitHubTool._ALLOWED_GIT_CMDS & destructive), (
            "白名单里出现了会丢弃工作区/改写当前分支的子命令："
            f"{sorted(GitHubTool._ALLOWED_GIT_CMDS & destructive)}。"
            "v0.45.210 已判定不这样修，理由见 report_deployer.auto_commit_and_notify docstring。"
        )


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
            r = GitHubTool(repo_path=str(tmp_path)).run_git_cmd("git status --porcelain")
        assert r["success"] is True
        assert not _records(caplog, "alpha_hive.agent_toolbox", logging.WARNING)

    def test_subprocess_failure_logs_warning(self, tmp_path, monkeypatch, caplog):
        """第二种失败形状（抛异常 ⇒ 只有 error 键）同样要出声。"""
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git push", timeout=30)
        # 替换模块内引用，不碰全局 subprocess.run
        monkeypatch.setattr(agent_toolbox, "subprocess", SimpleNamespace(
            run=boom, SubprocessError=subprocess.SubprocessError))
        with caplog.at_level(logging.WARNING):
            r = GitHubTool(repo_path=str(tmp_path)).run_git_cmd("git push origin main")
        assert r["success"] is False and "timed out" in r["error"]
        warns = _records(caplog, "alpha_hive.agent_toolbox", logging.WARNING)
        assert warns and "git push origin main" in warns[0].getMessage()


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


class TestNonProductionScanIsNotDeployed:

    def test_creates_no_commit_and_pushes_nowhere(self, sandbox, caplog):
        (sandbox.repo / LEFTOVER).write_text('{"system_status": "✅ 完成"}')
        head = sandbox.git("rev-parse", "HEAD").stdout
        with caplog.at_level(logging.INFO):
            res = rd.auto_commit_and_notify(sandbox.reporter, NON_PRODUCTION)

        assert sandbox.git("rev-parse", "HEAD").stdout == head, (
            "非生产扫描在本地 main 上造了提交 —— 下一次生产推送会把它送上 origin/main"
            "（2026-03-04~13 实际发生 7 次）")
        assert sandbox.git("ls-remote", str(sandbox.test)).stdout == ""
        assert sandbox.origin_log() == ["init"]
        assert not sandbox.ghpages
        assert res["deploy_env"] == "none"
        assert res["git_push"] == {"success": False, "skipped": "non_production", "remote": None}
        assert not any("已恢复" in r.getMessage() for r in caplog.records), \
            "又出现了「已恢复」式的无条件成功日志"

    def test_leftover_artifacts_are_named_not_hidden(self, sandbox, caplog):
        """管不到的残留（save_report 已写进工作区）要列出来，而不是装作不存在。

        v0.45.213 起 CLI 不再跑非蜂群扫描、造不出这种残留；本条守的是部署函数
        自身的契约（纵深防御：哪天有调用方把非生产报告递进来，它仍然出声）。
        """
        (sandbox.repo / LEFTOVER).write_text("{}")
        (sandbox.repo / "backtester.py").write_text("# 半成品代码，不算日报产物")
        with caplog.at_level(logging.WARNING):
            res = rd.auto_commit_and_notify(sandbox.reporter, NON_PRODUCTION)
        assert res["uncommitted_report_artifacts"] == [LEFTOVER]
        assert any(LEFTOVER in m for m in _warnings(caplog))

    def test_does_not_touch_the_working_tree(self, sandbox):
        """「不污染本地 main」不许靠清工作区实现 —— 那就是 reset --hard。

        对旧代码本条是等价的（reset 被白名单拒了）；它防的是按甲方案
        「往白名单加 checkout/reset」修回去：那样账本与进行中的改动会被清掉。
        """
        (sandbox.repo / "hedge_state").mkdir()
        (sandbox.repo / "hedge_state" / "trades.jsonl").write_text("leg\n")
        (sandbox.repo / "index.html").write_text("进行中的改动")
        rd.auto_commit_and_notify(sandbox.reporter, NON_PRODUCTION)
        assert (sandbox.repo / "hedge_state" / "trades.jsonl").read_text() == "leg\n"
        assert (sandbox.repo / "index.html").read_text() == "进行中的改动"

    def test_next_production_push_carries_no_non_production_commit(self, sandbox):
        """2026-03-13 事故链：规则引擎跑完 → 下一次蜂群扫描推送。"""
        (sandbox.repo / LEFTOVER).write_text('{"system_status": "✅ 完成"}')
        rd.auto_commit_and_notify(sandbox.reporter, NON_PRODUCTION)
        (sandbox.repo / "index.html").write_text("prod-2026-03-16")
        sandbox.reporter.date_str = "2026-03-16"
        res = rd.auto_commit_and_notify(sandbox.reporter, PRODUCTION)
        assert res["git_push"]["success"], res
        assert len(sandbox.origin_log()) == 2, (
            f"origin/main 上多出了非生产扫描造的提交：{sandbox.origin_log()}")

    def test_next_production_push_carries_no_non_production_content(self, sandbox, monkeypatch):
        """2026-03-13 事故链的**内容**层：非蜂群 CLI 扫描 → 下一次蜂群扫描推送。

        v0.45.210 时本条是 `xfail(strict=True)`，写法是「测试自己往工作区种残留，
        再直调部署函数」。那个形状**上游修好了也永远 XPASS 不了**——残留是测试种的，
        不是扫描写的（v0.45.213 实测：退役落地后它照旧 XFAIL）。所以改从 CLI 入口进：
        残留只能由被测的 `main()` 路由写出来。

        `RuleEngineReporter` 复刻退役前那一串：`run_daily_scan` → `save_report`
        （往工作区写日报 json）→ `auto_commit_and_notify`（非生产，不提交）。
        退役后 `main()` 在构造它之前就退出，于是工作区里没有东西可被带走。
        """
        import alpha_hive_daily_report as adr
        import yf_gate

        class RuleEngineReporter:
            def __init__(self, date_override=None):
                pass

            def run_daily_scan(self, focus_tickers=None):
                return dict(NON_PRODUCTION)

            def save_report(self, report):
                (sandbox.repo / LEFTOVER).write_text('{"system_status": "✅ 完成"}')
                return str(sandbox.repo / LEFTOVER)

            def auto_commit_and_notify(self, report):
                return rd.auto_commit_and_notify(sandbox.reporter, report)

        monkeypatch.setattr(adr, "AlphaHiveDailyReporter", RuleEngineReporter)
        monkeypatch.setattr(yf_gate, "install", lambda: False)
        monkeypatch.setattr(adr._timing, "write", lambda *a, **k: None)
        # --force：不然周末跑测试时交易日护栏先把旧代码挡掉，本条对旧 bug 只在交易日红
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", "--no-llm", "--force"])
        try:
            adr.main()
        except SystemExit:
            pass   # 退役后的正确行为；退出码与「未构造 reporter」见 test_non_swarm_scan_retired

        sandbox.reporter.date_str = "2026-03-16"
        (sandbox.repo / "index.html").write_text("prod-2026-03-16")
        res = rd.auto_commit_and_notify(sandbox.reporter, PRODUCTION)
        assert res["git_push"]["success"] and len(sandbox.origin_log()) == 2, (
            f"正对照没立住：生产推送本身没把提交送上 origin，下面的断言会空转变绿：{res}")
        shown = sandbox.git("--git-dir", str(sandbox.origin), "show", f"main:{LEFTOVER}", check=False)
        assert shown.returncode != 0, f"origin/main 带上了非生产产物：{shown.stdout}"


class TestProductionFailuresCarryTheirReason:

    @pytest.mark.parametrize("report", [
        PRODUCTION,
        {"distill_mode": "llm_enhanced"},
        {"swarm_results": {"NVDA": {"distill_mode": "llm_enhanced"}}},
    ], ids=["swarm", "llm", "llm-per-ticker"])
    def test_production_run_commits_pushes_and_syncs_ghpages(self, sandbox, report):
        """正对照：生产分支行为不变。"""
        (sandbox.repo / "index.html").write_text("prod-2")
        res = rd.auto_commit_and_notify(sandbox.reporter, report)
        assert res["deploy_env"] == "production"
        assert res["git_commit"]["success"] and res["git_push"]["success"], res
        assert len(sandbox.origin_log()) == 2 and sandbox.ghpages == [1]

    def test_rejected_push_reason_reaches_warning(self, sandbox, tmp_path, caplog):
        """形状一（git 非零退出，原因在 stderr）。复刻 2026-08~09 六次真实失败：
        本地 main 落后 origin/main ⇒ non-fast-forward。"""
        other = tmp_path / "other"
        sandbox.git("clone", "-q", str(sandbox.origin), str(other), cwd=tmp_path)
        sandbox.git("-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "别的 session 先推了", cwd=other)
        sandbox.git("push", "-q", "origin", "main", cwd=other)

        (sandbox.repo / "index.html").write_text("prod-2")
        with caplog.at_level(logging.WARNING):
            res = rd.auto_commit_and_notify(sandbox.reporter, PRODUCTION)
        assert res["git_push"]["success"] is False
        assert "rejected" in res["git_push"]["output"]
        assert any("rejected" in m for m in _warnings(caplog))

    def test_error_shaped_push_failure_keeps_its_reason(self, sandbox, monkeypatch, caplog):
        """形状二（白名单拒绝 / subprocess 抛异常 ⇒ 只有 error 键）。
        v0.45.210 前 push_result 只抄 stdout/stderr，这里的原因会变成空串。"""
        g = sandbox.reporter.agent_helper.git
        real = g.run_git_cmd

        def fake(cmd):
            if cmd.startswith("git push"):
                return {"success": False, "error": "git push timed out after 30 seconds"}
            return real(cmd)

        monkeypatch.setattr(g, "run_git_cmd", fake)
        (sandbox.repo / "index.html").write_text("prod-2")
        with caplog.at_level(logging.WARNING):
            res = rd.auto_commit_and_notify(sandbox.reporter, PRODUCTION)
        assert res["git_push"]["error"] == "git push timed out after 30 seconds"
        assert any("timed out" in m for m in _warnings(caplog)), _warnings(caplog)

    def test_status_failure_is_not_reported_as_clean(self, sandbox, monkeypatch, caplog):
        """`git status` 挂了 ≠ 工作目录干净。

        假值按 `GitHubTool.status()` 的失败契约写（v0.45.211：`success`+非空 `error`，
        无 `modified_files`）。假值会和真实形状各走各的，所以下一条用真失败再验一遍。"""
        monkeypatch.setattr(sandbox.reporter.agent_helper.git, "status",
                            lambda: {"success": False, "error": "fatal: not a git repository"})
        with caplog.at_level(logging.INFO):
            res = rd.auto_commit_and_notify(sandbox.reporter, PRODUCTION)
        assert not any("工作目录干净" in r.getMessage() for r in caplog.records)
        assert any("git status 失败" in m and "not a git repository" in m
                   for m in _warnings(caplog))
        assert res["git_commit"]["success"] is False

    def test_real_status_failure_is_not_reported_as_clean(self, sandbox, caplog):
        """同上，但不打桩：真 `GitHubTool.status()` 在真仓库里失败（索引损坏）。

        上一条的假值只证明「调用方认得那个假形状」；本条证明它认得 `status()`
        **真实**返回的形状 —— 两边任一侧改了失败形状，只有这条会红。"""
        (sandbox.repo / ".git" / "index").write_bytes(b"garbage")
        raw = sandbox.git("status", "--porcelain", check=False)
        assert raw.returncode != 0, f"正对照：索引损坏后 git status 应当失败：{raw}"

        with caplog.at_level(logging.INFO):
            res = rd.auto_commit_and_notify(sandbox.reporter, PRODUCTION)
        assert not any("工作目录干净" in r.getMessage() for r in caplog.records)
        assert any("git status 失败" in m and "index" in m for m in _warnings(caplog)), \
            _warnings(caplog)
        assert res["git_commit"]["success"] is False


# ═════════════════════════════ 调用方不许丢弃返回值 ═════════════════════════════

def _discarded_deploy_calls(source: str):
    """`auto_commit_and_notify(...)` 作为裸语句（返回值直接丢弃）的行号。"""
    return [
        node.lineno for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and _callee_name(node.value) == "auto_commit_and_notify"
    ]


class TestCallersReadTheResult:

    def test_no_production_caller_discards_the_result(self):
        bad = [f"{rel}:{line}" for rel, src in _production_sources()
               for line in _discarded_deploy_calls(src)]
        assert not bad, (
            "这些调用丢弃了 auto_commit_and_notify 的返回值 —— 推送失败时调用方照样报成功：\n  "
            + "\n  ".join(bad))

    def test_scanner_sees_every_caller(self):
        """正对照：CLI、GUI、reporter 上的委托方法三处都要被扫到。"""
        sites = {rel for rel, src in _production_sources()
                 for node in ast.walk(ast.parse(src))
                 if isinstance(node, ast.Call) and _callee_name(node) == "auto_commit_and_notify"}
        assert {"alpha_hive_daily_report.py", "gui/interactions.py"} <= sites

    def test_scanner_flags_a_discarded_result(self):
        """有牙：v0.45.210 前 gui/interactions.py 的原句。"""
        src = ("reporter.auto_commit_and_notify(report)\n"
               "sync = reporter.auto_commit_and_notify(report)\n"
               "_p = reporter.auto_commit_and_notify(report).get('git_push')\n")
        assert _discarded_deploy_calls(src) == [1]
