"""
数据根迁移阶段 4 验收测试：gh-pages 发布链改指向 + force-push 父提交陷阱。

覆盖两件验收标准（均用真实 git 仓库/裸仓库，不 mock subprocess，唯一的例外是
`verify_cdn_deployment`——它会打真实网络请求，与本文件要验证的东西无关）：

  ① 数据根（`PATHS.home`，报告文件所在）与 git 仓库根（git plumbing 必须在
     这里跑）彻底分离——`deploy_static_to_ghpages` 从数据根读文件、在仓库根
     做 git 操作，两者可以是完全不同的目录，模拟阶段 5 之后的现实。

  ② `resolve_gh_pages_parent`/`commit_and_push_gh_pages` 不会在别的
     session 已经推过 gh-pages、本地看不到时，把对方的提交静默挤成不可达
     对象（2026-09-11 实测过的 force-push 父提交陷阱，见 auto-memory
     `alpha-hive-ops-info.md` 该节）。附一条反向对照，复现旧实现
     （本地 ref 当父 + `--force`）确实会丢，证明正向测试不是在测稻草人。
"""
import os
import subprocess
from unittest.mock import MagicMock, patch

import report_deployer as rd


def _git(*args, cwd) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout.strip()


def _init_repo_with_origin(tmp_path, name):
    """建一个真实 git 仓库 + 一个本地裸仓库充当 origin（不碰真实 GitHub）。"""
    bare = tmp_path / f"{name}_origin.git"
    repo = tmp_path / name
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git("config", "user.email", "test@test.com", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    _git("remote", "add", "origin", str(bare), cwd=repo)
    (repo / "README.md").write_text("x")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo, bare


def _build_tree(repo, filename, content):
    """手工建一棵只含一个文件的 tree（独立 index，不碰仓库主 index）。"""
    (repo / filename).write_text(content)
    blob = _git("hash-object", "-w", filename, cwd=repo)
    idx = str(repo / ".git" / f"test-index-{filename}")
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = idx
    subprocess.run(["git", "update-index", "--add", "--cacheinfo", "100644", blob, filename],
                    cwd=str(repo), env=env, check=True)
    tree = subprocess.run(["git", "write-tree"], cwd=str(repo), env=env,
                           capture_output=True, text=True, check=True).stdout.strip()
    os.remove(idx)
    return tree


class TestDataRootSplitFullChain:
    """验收标准①：沙箱数据根跑全链到本地 bare 仓库的 gh-pages。"""

    def _make_reporter(self, monkeypatch, tmp_path, repo_path):
        import alpha_hive_daily_report as mod
        for name, val in [
            ("MemoryStore", None), ("CodeExecutorAgent", None),
            ("CODE_EXECUTION_CONFIG", {"enabled": False}),
            ("VectorMemory", None), ("VECTOR_MEMORY_CONFIG", {"enabled": False}),
            ("MetricsCollector", None), ("EarningsWatcher", None),
            ("SlackReportNotifier", None), ("Backtester", None),
        ]:
            monkeypatch.setattr(mod, name, val)
        from alpha_hive_daily_report import AlphaHiveDailyReporter
        reporter = AlphaHiveDailyReporter()
        reporter.agent_helper = MagicMock()
        reporter.agent_helper.git.repo_path = str(repo_path)
        reporter.date_str = "2026-09-18"
        return reporter

    def test_deploy_reads_data_root_writes_via_git_repo(self, tmp_path, monkeypatch):
        """报告文件只写进数据根；git 仓库根里一份都没有——部署仍必须成功，
        且发布到 gh-pages 的内容来自数据根，不是来自 git 仓库根。"""
        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))

        repo, bare = _init_repo_with_origin(tmp_path, "coderepo")
        reporter = self._make_reporter(monkeypatch, tmp_path, repo)

        (data_root / "index.html").write_text("<html>v1</html>")
        (data_root / "dashboard-data.json").write_text('{"_generated_at": "t1"}')

        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        clone = tmp_path / "verify_clone"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone)],
                        check=True, capture_output=True)
        assert (clone / "index.html").read_text() == "<html>v1</html>"
        assert (clone / "dashboard-data.json").exists()
        # git 仓库根自己的工作区没有被写入任何报告文件——证明数据来源确实是 data_root
        assert not (repo / "index.html").exists()
        assert not (repo / "dashboard-data.json").exists()

    def test_second_deploy_picks_up_new_content_from_data_root(self, tmp_path, monkeypatch):
        """两轮部署（模拟两天的扫描），第二轮改的是数据根里的文件，
        gh-pages 上必须看到新内容，且第一轮的提交仍在历史里（非首次部署走
        有父提交的路径）。"""
        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
        repo, bare = _init_repo_with_origin(tmp_path, "coderepo2")
        reporter = self._make_reporter(monkeypatch, tmp_path, repo)

        (data_root / "index.html").write_text("day1")
        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()
        first_tip = _git("rev-parse", "gh-pages", cwd=repo)

        (data_root / "index.html").write_text("day2")
        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        clone = tmp_path / "verify_clone2"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone)],
                        check=True, capture_output=True)
        assert (clone / "index.html").read_text() == "day2"
        second_tip = _git("rev-parse", "gh-pages", cwd=repo)
        assert second_tip != first_tip
        anc = subprocess.run(["git", "merge-base", "--is-ancestor", first_tip, second_tip], cwd=str(repo))
        assert anc.returncode == 0, "第一轮的提交应该仍是第二轮的祖先（非 force 推送、正常快进历史）"


class TestForcePushParentTrap:
    """验收标准②：force-push 父提交陷阱不会静默挤掉别人推的提交。"""

    def test_foreign_commit_stays_reachable_after_deploy(self, tmp_path):
        """别的 session 已经把提交推上了 origin/gh-pages，我方本地从未见过
        （连本地 gh-pages ref 都不存在——旧 bug 的必要条件）。我方部署完之后，
        对方那条提交必须仍然可达（是我方新提交的祖先），不能被 force 挤成孤儿。"""
        repo_a, bare = _init_repo_with_origin(tmp_path, "session_a")
        repo_b = tmp_path / "session_b"
        subprocess.run(["git", "clone", "-q", str(bare), str(repo_b)], check=True, capture_output=True)
        _git("config", "user.email", "b@test.com", cwd=repo_b)
        _git("config", "user.name", "b", cwd=repo_b)

        # session B：独立部署一条 gh-pages 提交（真实首次部署，无父提交）
        tree_b = _build_tree(repo_b, "foreign.html", "from-session-b")
        foreign_commit = subprocess.run(
            ["git", "commit-tree", tree_b, "-m", "session B deploy"],
            cwd=str(repo_b), capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "update-ref", "refs/heads/gh-pages", foreign_commit],
                        cwd=str(repo_b), check=True)
        subprocess.run(["git", "push", "origin", "gh-pages"], cwd=str(repo_b),
                        check=True, capture_output=True)

        # 前置条件：session A 本地确实看不到 gh-pages（没 fetch 过）
        precheck = subprocess.run(["git", "rev-parse", "gh-pages"], cwd=str(repo_a),
                                   capture_output=True, text=True)
        assert precheck.returncode != 0, "前置条件不成立：session A 不该在本地看到 gh-pages"

        # session A 建自己的今日报告 tree（不含 foreign.html，代表它完全不知道 B 的存在）
        tree_a = _build_tree(repo_a, "today.html", "from-session-a")

        result = rd.commit_and_push_gh_pages(str(repo_a), tree_a, lambda n: "session A deploy")
        assert result["success"], result
        assert result["parent_verified"] is True, "fetch 应该成功，父提交应该是校验过的"
        assert result["parent"] == foreign_commit, (
            "父提交必须是 fetch 到的远端真头（session B 的提交），不是空值/本地陈旧值")

        is_ancestor = subprocess.run(
            ["git", "--git-dir", str(bare), "merge-base", "--is-ancestor",
             foreign_commit, "refs/heads/gh-pages"],
        )
        assert is_ancestor.returncode == 0, (
            "session B 的提交在远端被挤成了不可达对象——正是本次要修的那个 bug")

        clone = tmp_path / "verify_final"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone)],
                        check=True, capture_output=True)
        assert (clone / "today.html").read_text() == "from-session-a"

    def test_old_local_ref_parent_would_have_orphaned_it(self, tmp_path):
        """反向对照：复现旧实现（`git rev-parse gh-pages` 取**本地** ref 当父提交，
        再 `--force` 推），证明它确实会把 session B 的提交挤成不可达对象。
        没有这条对照，上面那条正向测试证明不了它测的是一个真问题。"""
        repo_a, bare = _init_repo_with_origin(tmp_path, "session_a2")
        repo_b = tmp_path / "session_b2"
        subprocess.run(["git", "clone", "-q", str(bare), str(repo_b)], check=True, capture_output=True)
        _git("config", "user.email", "b@test.com", cwd=repo_b)
        _git("config", "user.name", "b", cwd=repo_b)

        tree_b = _build_tree(repo_b, "foreign.html", "from-session-b")
        foreign_commit = subprocess.run(
            ["git", "commit-tree", tree_b, "-m", "session B deploy"],
            cwd=str(repo_b), capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "update-ref", "refs/heads/gh-pages", foreign_commit],
                        cwd=str(repo_b), check=True)
        subprocess.run(["git", "push", "origin", "gh-pages"], cwd=str(repo_b),
                        check=True, capture_output=True)

        # 旧实现：本地 rev-parse gh-pages（session A 从未 fetch，取不到 ⇒ parent=None）
        old_parent_probe = subprocess.run(["git", "rev-parse", "gh-pages"], cwd=str(repo_a),
                                           capture_output=True, text=True)
        assert old_parent_probe.returncode != 0
        old_parent_args = []  # 旧代码对应：`git rev-parse` 失败就 pass，parent_args 保持空

        tree_a = _build_tree(repo_a, "today.html", "from-session-a")
        old_commit = subprocess.run(
            ["git", "commit-tree", tree_a] + old_parent_args + ["-m", "old style deploy"],
            cwd=str(repo_a), capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "update-ref", "refs/heads/gh-pages", old_commit],
                        cwd=str(repo_a), check=True)
        push = subprocess.run(["git", "push", "origin", "gh-pages", "--force"],
                               cwd=str(repo_a), capture_output=True, text=True)
        assert push.returncode == 0, "旧实现的 --force 推送本身应该会成功（这正是问题所在）"

        is_ancestor = subprocess.run(
            ["git", "--git-dir", str(bare), "merge-base", "--is-ancestor",
             foreign_commit, "refs/heads/gh-pages"],
        )
        assert is_ancestor.returncode != 0, (
            "如果这条断言失败，说明旧实现居然没复现 bug——上面那条正向测试就没有对照价值了")

    def test_conflict_during_push_triggers_refetch_retry(self, tmp_path, monkeypatch):
        """真实竞态：我方 fetch 之后、push 之前，另一个 session 抢先推送了一条
        我方完全不知道的提交。第一次 push 必须被 git 自己拒绝（非快进），
        重试循环重新 fetch 后必须把它当父提交接上——不能死等同一次必然被拒的推送。"""
        import time as _time_mod
        monkeypatch.setattr(_time_mod, "sleep", lambda *_: None)

        repo_a, bare = _init_repo_with_origin(tmp_path, "race_a")
        repo_b = tmp_path / "race_b"
        subprocess.run(["git", "clone", "-q", str(bare), str(repo_b)], check=True, capture_output=True)
        _git("config", "user.email", "b@test.com", cwd=repo_b)
        _git("config", "user.name", "b", cwd=repo_b)

        # day0：session A 自己先部署一次，建立一个非空的 gh-pages 起点
        tree0 = _build_tree(repo_a, "day0.html", "d0")
        result0 = rd.commit_and_push_gh_pages(str(repo_a), tree0, lambda n: "day0")
        assert result0["success"], result0

        real_run = subprocess.run
        state = {"sneaky_done": False}

        def sneaky_run(cmd, **kw):
            if cmd[:2] == ["git", "push"] and not state["sneaky_done"]:
                state["sneaky_done"] = True
                # 我方已经 fetch+build 完毕、正要 push 的这一刻，B 抢先推了一条提交
                real_run(["git", "fetch", "origin", "gh-pages"], cwd=str(repo_b),
                          check=True, capture_output=True)
                tree_sneaky = _build_tree(repo_b, "sneaky.html", "sneaky")
                parent_b = real_run(["git", "rev-parse", "origin/gh-pages"], cwd=str(repo_b),
                                     capture_output=True, text=True, check=True).stdout.strip()
                sneaky_commit = real_run(
                    ["git", "commit-tree", tree_sneaky, "-p", parent_b, "-m", "sneaky"],
                    cwd=str(repo_b), capture_output=True, text=True, check=True,
                ).stdout.strip()
                real_run(["git", "update-ref", "refs/heads/gh-pages", sneaky_commit],
                          cwd=str(repo_b), check=True)
                real_run(["git", "push", "origin", "gh-pages"], cwd=str(repo_b),
                          check=True, capture_output=True)
                state["sneaky_sha"] = sneaky_commit
            return real_run(cmd, **kw)

        tree1 = _build_tree(repo_a, "day1.html", "d1")
        with patch("subprocess.run", side_effect=sneaky_run):
            result1 = rd.commit_and_push_gh_pages(str(repo_a), tree1, lambda n: "day1")

        assert result1["success"], result1
        assert result1["attempts"] >= 2, "第一次 push 应该被 git 拒绝（非快进），必须重试才能成功"
        assert "sneaky_sha" in state, "sneaky_run 没有被真正触发——测试没测到东西"
        is_anc = subprocess.run(
            ["git", "--git-dir", str(bare), "merge-base", "--is-ancestor",
             state["sneaky_sha"], result1["commit"]],
        )
        assert is_anc.returncode == 0, "竞态期间抢跑的提交被挤掉了"
