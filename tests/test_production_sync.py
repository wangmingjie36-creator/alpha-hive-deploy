"""
生产 checkout 与 origin/main 的同步（v0.45.214）

固化 2026-09-01~11 六次 `git push origin main` 被拒（non-fast-forward）：各 session 从 worktree
直推 origin/main，生产 checkout 从不 pull。取证全文见 `production_sync` 模块 docstring。

四组，全部在**真 git 沙箱**里跑（bare origin + 生产 checkout + 另一个 session 的 clone）：
  1. 部署时本地落后 ⇒ 对象层合并后推上去；不动工作区、不动本地 main；冲突不推并列出路径；
     推送竞态只因 origin/main 真动过才重试；`merge-tree` 三个退出码不许揉成两个
  2. 扫描前只快进；做不到就保持现有代码并给出结局，绝不动工作区里的未提交改动
  3. A + B 的闭环：合并推送之后，下一轮扫描前的快进必须能成功（反例：直推被拒 ⇒ 分叉卡死）
  4. 结果真能走到告警：写入者（production_sync / scan_timing）与读者（alert_manager）用同一个形状

第 1 组对 v0.45.210 的代码是红的：那时本地落后时推送直接被拒。
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import production_sync as ps  # noqa: E402
import report_deployer as rd  # noqa: E402
import scan_timing as st  # noqa: E402
from agent_toolbox import GitHubTool  # noqa: E402
from tests.test_github_tool_commit import hold_lock_during_add  # noqa: E402

PRODUCTION = {"system_status": "✅ 蜂群协作完成", "swarm_metadata": {"tickers_analyzed": 1}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    # 若 pytest 从 git hook 里被拉起，继承的 GIT_DIR 会让下面每条 git 命令打到真仓库上
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)
    origin, prod, other = tmp_path / "origin.git", tmp_path / "prod", tmp_path / "other"

    def git(*a, cwd=prod, check=True):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check)

    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(prod), cwd=tmp_path)
    for name, text in (("index.html", "report-0911"), ("code.py", "v1"), ("notes.json", "n")):
        (prod / name).write_text(text)
    git("config", "user.email", "prod@t")
    git("config", "user.name", "prod")
    git("add", "-A")
    git("commit", "-qm", "init")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    git("config", "user.email", "session@t", cwd=other)
    git("config", "user.name", "session", cwd=other)

    def session_push(name, text, msg):
        """另一个 session：同步、改一个文件、推 origin/main。返回它推上去的提交。"""
        git("pull", "-q", "--ff-only", "origin", "main", cwd=other)
        (other / name).write_text(text)
        git("commit", "-qam", msg, cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        return git("rev-parse", "HEAD", cwd=other).stdout.strip()

    def origin_rev(ref="main"):
        return git("--git-dir", str(origin), "rev-parse", ref).stdout.strip()

    def origin_show(path):
        return git("--git-dir", str(origin), "show", f"main:{path}").stdout

    def head():
        return git("rev-parse", "HEAD").stdout.strip()

    def is_ancestor(a, b):
        return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    monkeypatch.setattr(rd, "deploy_static_to_ghpages", lambda reporter: None)
    # 残留锁的用例会等一次 add 重试（v0.45.227）；默认间隔在 test_github_tool_commit 单独核
    monkeypatch.setattr(GitHubTool, "_ADD_RETRY_DELAY_S", 0, raising=False)
    tool = GitHubTool(repo_path=str(prod))
    reporter = SimpleNamespace(agent_helper=SimpleNamespace(git=tool), date_str="2026-09-14")
    return SimpleNamespace(origin=origin, prod=prod, other=other, git=git, tool=tool,
                           reporter=reporter, session_push=session_push, origin_rev=origin_rev,
                           origin_show=origin_show, head=head, is_ancestor=is_ancestor)


def _deploy(w, report_text="report-0914"):
    (w.prod / "index.html").write_text(report_text)
    return rd.auto_commit_and_notify(w.reporter, PRODUCTION)["git_push"]


def _warnings(caplog, logger):
    return [r.getMessage() for r in caplog.records
            if r.name == logger and r.levelno >= logging.WARNING]


# ═════════════════════════════ 1. 部署时：落后也要推上去 ═════════════════════════════

class TestPushLandsWhenBehind:

    def test_behind_origin_lands_via_object_merge_without_touching_worktree(self, world):
        """复刻 09-11：生产 main 落后 origin/main（别的 session 推了代码），扫描出日报。

        v0.45.210：`! [rejected] main -> main (non-fast-forward)`，日报与账本滞留本地。"""
        w = world
        session = w.session_push("code.py", "v2", "session: 改代码")
        (w.prod / "notes.json").write_text("生产里没提交的改动")    # 同 NVDA_raw.json：非日报产物

        push = _deploy(w)
        report_commit = w.head()

        assert push["success"] is True, push
        assert push["integration"] == "merged" and push["behind"] == 1
        merge = push["merge_commit"]
        assert w.origin_rev() == merge
        parents = w.git("rev-list", "--parents", "-n", "1", merge).stdout.split()[1:]
        assert parents == [session, report_commit], "第一父必须是 origin/main，main 的 first-parent 史才连续"
        assert w.origin_show("index.html") == "report-0914"
        assert w.origin_show("code.py") == "v2"
        # 不动工作区、不动本地 main：部署之后编排器还要跑别的 Python 步骤
        assert (w.prod / "code.py").read_text() == "v1", "部署时换了代码 ⇒ 一轮混两个版本"
        assert (w.prod / "notes.json").read_text() == "生产里没提交的改动"
        assert w.git("rev-parse", "main").stdout.strip() == report_commit
        # 本地 main 仍是 origin/main 的祖先 ⇒ 下一轮扫描前可以快进（第 3 组验真快进）
        assert w.is_ancestor(report_commit, w.origin_rev())

    def test_up_to_date_production_still_fast_forwards(self, world):
        """正对照：不落后时就是普通快进推送，不造合并提交。"""
        push = _deploy(world)
        assert push["success"] and push["integration"] == "fast_forward"
        assert push["merge_commit"] is None and push["behind"] == 0
        assert world.origin_rev() == world.head()

    def test_nothing_to_push_makes_no_empty_merge(self, world):
        """这轮没造出日报提交、别人又推过：本地 main 已被 origin/main 包含 ⇒ 什么都不推。

        v0.45.214 初版只判了「origin 是不是本地的祖先」，这里会推上去一个树与 origin/main
        完全相同的双父空合并（跨 session 复核在真沙箱里实测）。"""
        w = world
        session = w.session_push("code.py", "v2", "session: 改代码")
        push = rd.auto_commit_and_notify(w.reporter, PRODUCTION)["git_push"]   # 工作区干净，无日报提交
        assert push["success"] is True, push
        assert push["integration"] == "nothing_to_push" and push["merge_commit"] is None
        assert w.origin_rev() == session, "造了空合并推上去"

    def test_fetch_failing_after_a_rejection_reports_that_rejection(self, world):
        """被拒一轮后 fetch 又失败：报上一轮的拒绝原因，不再退回直推（初版这条分支没有测试）。"""
        w = world
        hook = w.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho '本仓库冻结中' >&2\nexit 1\n")
        hook.chmod(0o755)
        real = w.tool.run_git_cmd
        calls = {"fetch": 0, "push": 0}

        def flaky(cmd):
            if cmd.startswith("git fetch"):
                calls["fetch"] += 1
                if calls["fetch"] > 1:
                    return {"success": False, "stdout": "", "stderr": "ssh: timeout", "returncode": 128}
            if cmd.startswith("git push"):
                calls["push"] += 1
            return real(cmd)

        w.tool.run_git_cmd = flaky
        push = _deploy(w)
        assert push["success"] is False and "本仓库冻结中" in push["output"]
        assert push["integration"] == "fast_forward" and push["attempts"] == 1
        assert calls["push"] == 1, "fetch 失败后又退回直推了一次"

    def test_conflict_is_not_pushed_and_names_the_paths(self, world, caplog):
        """别的 session 重渲染了同一份已发布产物（09-11 真发生过）⇒ 冲突：不推，列出路径。"""
        w = world
        session = w.session_push("index.html", "session 重渲染", "session: 重渲染")
        with caplog.at_level(logging.WARNING):
            push = _deploy(w, "report-0914")
        assert push["success"] is False
        assert push["integration"] == "conflict" and push["conflicts"] == ["index.html"]
        assert w.origin_rev() == session, "有冲突还推了"
        assert any("index.html" in m for m in _warnings(caplog, "alpha_hive.report_deployer"))
        assert (w.prod / "index.html").read_text() == "report-0914", "冲突处理动了工作区"

    def test_push_race_retries_because_origin_moved(self, world):
        """fetch 与 push 之间别的 session 又推了一次：origin/main 真动过 ⇒ 值得重来一轮。"""
        w = world
        real = w.tool.run_git_cmd
        raced = []

        def racing(cmd):
            if cmd.startswith("git push origin") and not raced:
                raced.append(w.session_push("code.py", "v2", "session: 抢先一步"))
            return real(cmd)

        w.tool.run_git_cmd = racing
        push = _deploy(w)
        assert push["success"] is True, push
        assert push["attempts"] == 2 and push["integration"] == "merged"
        assert w.is_ancestor(raced[0], w.origin_rev()) and w.is_ancestor(w.head(), w.origin_rev())

    def test_merge_tree_error_exit_is_neither_conflict_nor_clean(self, world):
        """`merge-tree --write-tree` 退出码 ≥2 是真出错：既不能当干净合并推上去，也不能报成冲突。"""
        w = world
        session = w.session_push("code.py", "v2", "session: 改代码")
        real = w.tool.run_git_cmd

        def broken(cmd):
            if cmd.startswith("git merge-tree"):
                return {"success": False, "stdout": "", "stderr": "fatal: bad object", "returncode": 128}
            return real(cmd)

        w.tool.run_git_cmd = broken
        push = _deploy(w)
        assert push["success"] is False and push["integration"] == "error"
        assert push["conflicts"] is None
        assert "128" in push["error"] and "bad object" in push["error"]
        assert w.origin_rev() == session, "出错了还推了"

    def test_fetch_failure_keeps_the_push_reason_and_says_fetch_failed(self, world, caplog):
        """看不到 origin 就判断不了落没落后：退回直推，由 git 自己拒；两个原因都要进 warning。"""
        w = world
        w.session_push("code.py", "v2", "session: 改代码")
        real = w.tool.run_git_cmd

        def no_fetch(cmd):
            if cmd.startswith("git fetch"):
                return {"success": False, "stdout": "", "stderr": "ssh: Could not resolve host",
                        "returncode": 128}
            return real(cmd)

        w.tool.run_git_cmd = no_fetch
        with caplog.at_level(logging.WARNING):
            push = _deploy(w)
        assert push["success"] is False and push["integration"] == "unchecked"
        assert "rejected" in push["output"] and "Could not resolve host" in push["fetch_error"]
        warns = _warnings(caplog, "alpha_hive.report_deployer")
        assert any("rejected" in m and "Could not resolve host" in m for m in warns), warns


# ═════════════════════════════ 2. 扫描前：只快进 ═════════════════════════════

class TestSyncBeforeScan:

    def test_up_to_date(self, world):
        res = ps.sync_before_scan(world.tool, today="2026-09-14")
        assert res["outcome"] == "up_to_date" and res["behind"] == 0

    def test_behind_fast_forwards_and_keeps_unrelated_local_edits(self, world):
        w = world
        before = w.head()
        session = w.session_push("code.py", "v2", "session: 改代码")
        (w.prod / "notes.json").write_text("生产里没提交的改动")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "fast_forwarded", res
        assert (res["before"], res["after"], res["behind"]) == (before, session, 1)
        assert (w.prod / "code.py").read_text() == "v2"
        assert (w.prod / "notes.json").read_text() == "生产里没提交的改动"

    def test_dirty_overlap_is_refused_and_local_edit_survives(self, world):
        """工作区里有未提交改动、恰好被 origin 改过：git 拒绝快进。不许 stash / reset 来硬过。"""
        w = world
        before = w.head()
        w.session_push("code.py", "v2", "session: 改代码")
        (w.prod / "code.py").write_text("生产里手改的")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "ff_refused" and res["detail"]
        assert w.head() == before
        assert (w.prod / "code.py").read_text() == "生产里手改的"
        assert w.git("stash", "list").stdout == "", "拿 stash 硬过了（stash 栈是所有 worktree 共享的）"

    def test_local_ahead_is_not_ok(self, world):
        """本地 main 有没推上去的提交 ⇒ 生产跑的不是 main，要红。"""
        w = world
        (w.prod / "code.py").write_text("生产里直接提交的")
        w.git("commit", "-qam", "直接在生产 checkout 提交")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "local_ahead" and res["ahead"] == 1
        assert res["outcome"] not in ps.OK_OUTCOMES

    def test_diverged_leaves_head_alone(self, world):
        w = world
        (w.prod / "notes.json").write_text("生产里直接提交的")
        w.git("commit", "-qam", "直接在生产 checkout 提交")
        before = w.head()
        w.session_push("code.py", "v2", "session: 改代码")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "diverged" and (res["ahead"], res["behind"]) == (1, 1)
        assert w.head() == before and (w.prod / "code.py").read_text() == "v1"

    def test_not_on_main_is_left_alone(self, world):
        w = world
        w.git("switch", "-q", "-c", "experiment")
        w.session_push("code.py", "v2", "session: 改代码")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "not_on_main"
        assert (w.prod / "code.py").read_text() == "v1"

    def test_fetch_failure(self, world, tmp_path):
        world.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
        res = ps.sync_before_scan(world.tool, today="2026-09-14")
        assert res["outcome"] == "fetch_failed" and res["detail"]


# ═════════════════════════════ 3. A + B 闭环 ═════════════════════════════

class TestDeployThenNextScanLoop:

    def test_next_scan_fast_forwards_after_a_merged_deploy(self, world):
        """09-11 → 09-14：落后时部署（合并推送）→ 周末 session 又推 → 周一扫描前快进。"""
        w = world
        w.session_push("code.py", "v2", "session: 09-11 13:37 的修复")
        # ⚠️ 内容必须与夹具初值不同：v0.45.214 初版这里写的正是初值 ⇒ 没有日报提交 ⇒
        # 本条靠「空合并」bug 才绿（复核发现该 bug 后修复，本条随之变红才暴露出来）
        assert _deploy(w, "report-0911 14:47")["integration"] == "merged"
        w.session_push("code.py", "v3", "session: 周末的修复")

        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "fast_forwarded", res
        assert w.head() == w.origin_rev()
        assert (w.prod / "code.py").read_text() == "v3"
        assert (w.prod / "index.html").read_text() == "report-0911 14:47"

    def test_without_the_object_merge_the_next_sync_is_stuck(self, world):
        """反例（为什么 B 离不开 A）：沿用 v0.45.210 的直推，被拒后报告提交滞留本地 ⇒
        本地 main 与 origin/main 分叉 ⇒ 下一轮快进做不到，要等人来修。"""
        w = world
        w.session_push("code.py", "v2", "session: 改代码")
        (w.prod / "index.html").write_text("report-0911 14:47")
        w.git("commit", "-qam", "Alpha Hive 蜂群日报 2026-09-11 14:47")
        assert w.git("push", "origin", "main", check=False).returncode != 0, "正对照：直推应被拒"
        assert ps.sync_before_scan(w.tool, today="2026-09-14")["outcome"] == "diverged"


# ═════════════════════════════ 4. 结果走到告警 ═════════════════════════════

class TestResultReachesAlerts:

    def test_result_file_lives_under_isolated_logs_dir(self, tmp_path):
        """默认位置走 PATHS（调用时求值）⇒ 被 conftest 的 ALPHA_HIVE_LOGS_DIR 隔离住。"""
        target = ps.write_result({"date": "2026-09-14", "outcome": "up_to_date"})
        assert target is not None
        assert target.parent == Path(os.environ["ALPHA_HIVE_LOGS_DIR"])
        assert str(target).startswith(str(tmp_path))

    def test_result_file_is_gitignored_in_the_checkout(self):
        """生产 checkout 里它每轮都写：不忽略就成了未跟踪文件，每次部署都进「跳过 N 个非日报文件」的噪音。

        v0.45.214 首次在生产真跑 CLI 时实测漏了这一条。`git check-ignore` 三个退出码分开处理：
        0=忽略 / 1=未忽略（红）/ 128=这里不是 git 仓库（导出树等，条件为真的是全集减人造特例，skip 正当）。"""
        r = subprocess.run(["git", "check-ignore", "-q", "logs/production_sync.json"],
                           cwd=_ROOT, capture_output=True, text=True)
        if r.returncode == 128:
            pytest.skip(f"不在 git 仓库里：{r.stderr.strip()}")
        assert r.returncode == 0, f"logs/production_sync.json 没被 .gitignore 忽略（exit={r.returncode}）"

    def test_load_only_returns_this_rounds_result(self, tmp_path):
        p = tmp_path / "ps.json"
        assert ps.load_for_date("2026-09-14", p) is None                      # 没有
        p.write_text("{坏的")
        assert ps.load_for_date("2026-09-14", p) is None                      # 坏的
        ps.write_result({"date": "2026-09-11", "outcome": "up_to_date"}, p)
        assert ps.load_for_date("2026-09-14", p) is None, "上一轮的结果不能冒充这一轮"
        ps.write_result({"date": "2026-09-14", "outcome": "ff_refused"}, p)
        assert ps.load_for_date("2026-09-14", p)["outcome"] == "ff_refused"

    def test_cli_exit_code_and_result_file(self, world, monkeypatch):
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(world.prod))   # GitHubTool() 默认仓库
        assert ps.main(["--date", "2026-09-14"]) == 0
        assert ps.load_for_date("2026-09-14")["outcome"] == "up_to_date"
        world.git("switch", "-q", "-c", "experiment")
        assert ps.main(["--date", "2026-09-14"]) == 1
        assert ps.load_for_date("2026-09-14")["outcome"] == "not_on_main"

    @staticmethod
    def _alerts(tmp_path, snap):
        from alert_manager import AlertAnalyzer
        status = {"status": "success", "total_duration_seconds": 1,
                  "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": snap}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status, ensure_ascii=False))
        a = AlertAnalyzer(report_dir=tmp_path)
        return a, [x.message for x in a.analyze(p)]

    def test_real_failures_reach_alert_manager(self, world, tmp_path):
        """写入者与读者用同一个形状：真沙箱里的冲突 + 真快进拒绝 → 真 snapshot → 真告警。

        v0.45.214 前那条规则读 `deploy_status`（零写入者），这里任何一边改了形状都会红。"""
        w = world
        w.session_push("index.html", "session 重渲染", "session: 重渲染")
        push = _deploy(w, "report-0914")
        assert push["integration"] == "conflict"
        (w.prod / "index.html").write_text("生产里手改的")   # 让快进也被拒
        sync = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert sync["outcome"] not in ps.OK_OUTCOMES
        ps.write_result(sync)

        snap = st.snapshot("2026-09-14", extra={"git_push": st.git_push_summary(push)})
        assert snap["production_sync"]["outcome"] == sync["outcome"]
        _, msgs = self._alerts(tmp_path, snap)
        assert any("main 推送失败" in m for m in msgs), msgs
        assert any("生产代码 ≠ origin/main" in m for m in msgs), msgs

    def test_healthy_round_raises_neither_alert(self, world, tmp_path):
        """正对照：不是「什么都报」。"""
        w = world
        w.session_push("code.py", "v2", "session: 改代码")
        push = _deploy(w)
        assert push["success"]
        ps.write_result(ps.sync_before_scan(w.tool, today="2026-09-14"))
        snap = st.snapshot("2026-09-14", extra={"git_push": st.git_push_summary(push)})
        a, msgs = self._alerts(tmp_path, snap)
        assert not any("推送失败" in m or "origin/main（" in m or "同步未执行" in m or "提交失败" in m
                       for m in msgs), msgs
        assert not any("推送" in s for s in a.checks_skipped)

    @pytest.mark.parametrize("deploy", ["returns", "raises"])
    def test_main_writes_the_push_result_into_the_timing_snapshot(self, monkeypatch, deploy):
        """上面几条自己拼 snapshot；这条走**真 `main()` 的蜂群路径**，核对 `_timing.write` 真收到了推送结果。

        `alpha_hive_daily_report.main` 里捕获 `_git_push` 那两行一旦被改坏，只有这条会红。"""
        import alpha_hive_daily_report as adr
        import yf_gate

        failed = {"success": False, "integration": "conflict", "conflicts": ["index.html"],
                  "output": "x" * 800}

        class SwarmReporter:
            date_str = "2026-09-14"

            def __init__(self, date_override=None):
                pass

            def run_swarm_scan(self, focus_tickers=None):
                return {"system_status": "✅ 蜂群协作完成", "swarm_metadata": {"tickers_analyzed": 1},
                        "opportunities": [{"ticker": "NVDA"}]}

            def save_report(self, report):
                return "/dev/null"

            def auto_commit_and_notify(self, report):
                if deploy == "raises":
                    raise OSError("Too many open files")
                return {"git_push": failed, "deploy_env": "production",
                        "git_commit": {"success": False, "pending_artifacts": 3, "error": "index.lock"}}

        written = []
        monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SwarmReporter)
        monkeypatch.setattr(yf_gate, "install", lambda: False)
        monkeypatch.setattr(adr._timing, "write", lambda d, extra=None, **k: written.append((d, extra)))
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", "--swarm", "--no-llm", "--force"])
        adr.main()

        assert len(written) == 1, written
        push = written[0][1]["git_push"]
        assert push["success"] is False
        if deploy == "returns":
            assert push["integration"] == "conflict" and push["conflicts"] == ["index.html"]
            assert len(push["output"]) == 500
            assert written[0][1]["git_commit"] == {"success": False, "pending_artifacts": 3,
                                                   "reason": "index.lock"}
        else:
            assert "Too many open files" in push["error"], "部署抛异常被记成了「没记录」"

    # ── v0.45.223：日报提交失败要可见；同步结局按「实际跑的是什么」措辞 ──

    def test_failed_report_commit_alerts_even_when_push_says_success(self, world, tmp_path):
        """复刻：生产 checkout 残留 `.git/index.lock` ⇒ `git add` 全失败 ⇒ 日报没提交；
        同时别人推过 ⇒ `push_main` 报 `nothing_to_push` 成功。推送侧看不出任何问题。"""
        w = world
        w.session_push("code.py", "v2", "session: 改代码")
        (w.prod / ".git" / "index.lock").write_text("")
        (w.prod / "index.html").write_text("report-0914")
        res = rd.auto_commit_and_notify(w.reporter, PRODUCTION)
        assert res["git_push"]["success"] is True and res["git_push"]["integration"] == "nothing_to_push"
        assert res["git_commit"]["success"] is False and res["git_commit"]["pending_artifacts"] == 1

        snap = st.snapshot("2026-09-14", extra={"git_push": st.git_push_summary(res["git_push"]),
                                                "git_commit": st.git_commit_summary(res["git_commit"])})
        a, msgs = self._alerts(tmp_path, snap)
        hit = [x for x in a.alerts if "日报提交失败" in x.message]
        assert hit, msgs
        assert not any("推送失败" in m for m in msgs), "推送确实没失败，别报错地方"
        # v0.45.225：原因一栏必须是真实原因。v0.45.223 这里写的是「白名单未匹配到任何文件」，
        # 与同一条告警「建议：查 index.lock」自相矛盾（`GitHubTool.commit` 吞了 git add 的报错）
        assert "index.lock" in hit[0].details["原因"], hit[0].details

    def test_nothing_to_commit_is_not_a_commit_failure(self, world, tmp_path):
        """正对照：只有非日报产物有改动 ⇒ `commit()` 回 success=False（nothing to commit），但不是故障。"""
        w = world
        (w.prod / "notes.json").write_text("生产里没提交的改动")
        res = rd.auto_commit_and_notify(w.reporter, PRODUCTION)
        assert res["git_commit"]["success"] is False, "正对照没立住：commit() 对「没东西可提交」应回 False"
        assert res["git_commit"]["pending_artifacts"] == 0
        assert res["git_commit"]["left_artifacts"] == 0
        snap = st.snapshot("2026-09-14", extra={"git_commit": st.git_commit_summary(res["git_commit"]),
                                                "git_push": st.git_push_summary(res["git_push"])})
        _, msgs = self._alerts(tmp_path, snap)
        assert not any("提交失败" in m for m in msgs), msgs

    # ── v0.45.227：「提交成功」≠「日报产物全进了 git」 ──

    def _commit_snapshot(self, tmp_path, res):
        snap = st.snapshot("2026-09-14", extra={"git_push": st.git_push_summary(res["git_push"]),
                                                "git_commit": st.git_commit_summary(res["git_commit"])})
        return self._alerts(tmp_path, snap)

    def test_artifact_a_lock_kept_out_alerts_although_commit_succeeded(self, world, tmp_path, monkeypatch):
        """别的进程在 add index.html 那一刻、连重试那一刻都拿着索引锁 ⇒ rss.xml 进了提交、index.html 没有，
        `commit()` 回 success=True。v0.45.225 及以前：零告警，当天网站首页没进 git。"""
        w = world
        (w.prod / "index.html").write_text("report-0914")
        (w.prod / "rss.xml").write_text("<rss/>")
        hold_lock_during_add(monkeypatch, w.tool, w.prod, "index.html", times=2)

        res = rd.auto_commit_and_notify(w.reporter, PRODUCTION)
        gc = res["git_commit"]
        assert gc["success"] is True and gc["pending_artifacts"] == 2, f"正对照：提交确实报成功 {gc}"
        assert w.git("show", "--name-only", "--format=", "HEAD").stdout.split() == ["rss.xml"]
        assert gc["left_artifacts"] == 1 and gc["left_sample"] == ["index.html"], gc

        a, msgs = self._commit_snapshot(tmp_path, res)
        hit = [x for x in a.alerts if "没进 git" in x.message]
        assert hit, msgs
        assert "提交失败" not in hit[0].message, "提交确实成功了，别报成提交失败"
        assert "index.html" in hit[0].details["提交后仍未进 git"], hit[0].details
        assert "index.lock" in hit[0].details["原因"], f"原因栏要是 add 的真实报错，不是提交成功的输出：{hit[0].details}"

    def test_brief_lock_is_absorbed_and_raises_nothing(self, world, tmp_path, monkeypatch):
        """正对照：锁只占一下 ⇒ 重试接住 ⇒ 全进 git、不报。"""
        w = world
        (w.prod / "index.html").write_text("report-0914")
        (w.prod / "rss.xml").write_text("<rss/>")
        hold_lock_during_add(monkeypatch, w.tool, w.prod, "index.html", times=1)

        res = rd.auto_commit_and_notify(w.reporter, PRODUCTION)
        assert res["git_commit"]["success"] is True and res["git_commit"]["left_artifacts"] == 0, res["git_commit"]
        a, msgs = self._commit_snapshot(tmp_path, res)
        assert not any("没进 git" in m or "提交失败" in m for m in msgs), msgs
        assert not any("进 git" in s for s in a.checks_skipped), a.checks_skipped

    def test_unknown_after_commit_state_is_a_skipped_check_not_silence(self, world, tmp_path, monkeypatch):
        """提交后那次 `git status` 失败（真索引损坏）⇒ 不知道产物进没进 git，不许渲染成「全进了」。"""
        w = world
        (w.prod / "index.html").write_text("report-0914")
        real_commit = w.tool.commit

        def commit_then_index_breaks(*a, **k):
            out = real_commit(*a, **k)
            (w.prod / ".git" / "index").write_bytes(b"garbage")
            return out

        monkeypatch.setattr(w.tool, "commit", commit_then_index_breaks)
        res = rd.auto_commit_and_notify(w.reporter, PRODUCTION)
        gc = res["git_commit"]
        assert gc["success"] is True and gc["left_artifacts"] is None, gc
        a, msgs = self._commit_snapshot(tmp_path, res)
        assert not any("没进 git" in m or "提交失败" in m for m in msgs), msgs
        assert any("进 git" in s for s in a.checks_skipped), a.checks_skipped

    # ── v0.45.236：共享索引里别的 session 已暂存的代码，不许被当成日报推上 main ──

    @pytest.mark.parametrize("with_report", [True, False], ids=["with-report", "no-report-change"])
    def test_code_another_session_staged_in_production_is_not_deployed(self, world, with_report):
        """复刻：别的 session 在生产 checkout 里 `git add code.py` 还没提交，此刻扫描部署。
        旧代码：`git commit -m` 提交整个索引 ⇒ code.py 进「日报」提交、推上 origin/main，
        `left_artifacts=0` 零告警；且 `skipped_non_artifacts` 还说它被「跳过」了。"""
        w = world
        (w.prod / "code.py").write_text("v2 另一个 session 的半成品")
        w.git("add", "code.py")
        if with_report:
            (w.prod / "index.html").write_text("report-0914")

        res = rd.auto_commit_and_notify(w.reporter, PRODUCTION)

        assert w.origin_show("code.py") == "v1", "别的 session 暂存的代码被当成日报推上了 main"
        assert w.git("diff", "--cached", "--name-only").stdout.split() == ["code.py"], "别人的暂存不许被动"
        assert "code.py" in res["skipped_non_artifacts"]
        if with_report:
            assert w.origin_show("index.html") == "report-0914" and res["git_commit"]["success"] is True
            assert res["git_commit"]["left_artifacts"] == 0
        else:
            assert res["git_commit"]["success"] is False and res["git_commit"]["pending_artifacts"] == 0

    def test_local_ahead_alert_does_not_call_it_old_code(self, tmp_path):
        snap = st.snapshot("2026-09-14", extra={"git_push": {"success": True}})
        snap["production_sync"] = {"date": "2026-09-14", "outcome": "local_ahead", "ahead": 1, "behind": 0}
        a, _ = self._alerts(tmp_path, snap)
        hit = [x for x in a.alerts if "origin/main（local_ahead）" in x.message]
        assert hit, [x.message for x in a.alerts]
        assert "旧代码" not in hit[0].message and "不是旧代码" in hit[0].details["含义"]

    def test_missing_sync_result_is_not_silence(self, tmp_path):
        """编排器没调 production_sync.py（或日期对不上）⇒ 不知道跑的是哪版，要出声。"""
        snap = st.snapshot("2026-09-14", extra={"git_push": {"success": True}})
        assert snap["production_sync"] is None
        _, msgs = self._alerts(tmp_path, snap)
        assert any("同步未执行" in m for m in msgs), msgs

    def test_missing_push_record_is_skipped_not_green(self, tmp_path):
        snap = st.snapshot("2026-09-14")
        a, msgs = self._alerts(tmp_path, snap)
        assert not any("推送失败" in m for m in msgs)
        assert any("main 推送检查" in s for s in a.checks_skipped)
