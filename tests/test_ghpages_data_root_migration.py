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

2026-09-21 二次检查（v0.45.305）新增两组，覆盖两件此前漏掉的缺陷（均含
"退回未修复代码真跑一次、确认转红"的变异检验，不止靠 mock 自证）：

  ③ `CODE_SHIPPED_STATIC_ASSETS`（`.nojekyll`/`chart.umd.min.js`）只存在于
     git 仓库根，从不出现在数据根——`TestCodeShippedStaticAssetsSurviveDeploy`。

  ④ `resolve_gh_pages_parent` 在 `--single-branch` 克隆下不能再依赖"fetch
     顺带更新 origin/gh-pages"——`TestResolveGhPagesParentSingleBranchClone`。

  ⑤ `generate_ml_report._sync_ghpages`（gh-pages 的第二条独立部署路径，与
     ①③同源）也接了同一套 fallback——`TestSyncGhpagesSharesAssetFallback`。
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


def _make_reporter(monkeypatch, tmp_path, repo_path):
    """构造一个可跑 `_deploy_static_to_ghpages()` 的真实 reporter（重活的依赖全 stub 掉）。

    模块级——供本文件多个测试类共用，避免各写一份。
    """
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


class TestDataRootSplitFullChain:
    """验收标准①：沙箱数据根跑全链到本地 bare 仓库的 gh-pages。"""

    _make_reporter = staticmethod(_make_reporter)

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


class TestCodeShippedStaticAssetsSurviveDeploy:
    """2026-09-21 二次检查缺陷①：`.nojekyll`/`chart.umd.min.js` 只存在于
    git 仓库根、不存在于数据根——`os.listdir(data_root)` 选文件的白名单会
    漏掉它们，阶段 5 之后（data_root 真正搬去 `~/alpha-hive-data`）第一次
    部署就会把线上这两个文件删掉。布局刻意做成生产形态：data_root 只放
    运行时产物，两个静态资源只放仓库工作区，不用 `index.html`/
    `dashboard-data.json` 这类合成文件掩盖问题（那正是原有 5 条测试漏掉
    这个缺陷的原因）。
    """

    def test_repo_only_assets_reach_gh_pages(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))

        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_assets")
        # 生产形态：这两个文件随代码仓库提交，从不出现在数据根。
        (repo / ".nojekyll").write_text("")
        (repo / "chart.umd.min.js").write_text("console.log('chart');")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ship static assets"],
                        cwd=str(repo), check=True)

        reporter = _make_reporter(monkeypatch, tmp_path, repo)
        # 数据根只有运行时产物，不含 .nojekyll / chart.umd.min.js。
        (data_root / "index.html").write_text("<html>v1</html>")
        (data_root / "dashboard-data.json").write_text('{"_generated_at": "t1"}')

        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        clone = tmp_path / "verify_clone_assets"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone)],
                        check=True, capture_output=True)
        assert (clone / ".nojekyll").exists(), (
            ".nojekyll 只存在于 git 仓库根、不在数据根，部署后必须仍出现在 gh-pages 上")
        assert (clone / "chart.umd.min.js").read_text() == "console.log('chart');", (
            "chart.umd.min.js 只存在于 git 仓库根，部署后内容必须原样出现在 gh-pages 上")
        # 数据根里的运行时产物照常存在，证明修复没有破坏原有路径。
        assert (clone / "index.html").read_text() == "<html>v1</html>"

    def test_mutation_reverting_fallback_makes_assets_vanish(self, tmp_path, monkeypatch):
        """变异测试：把 `deploy_static_to_ghpages` 退回"只读 data_root"的旧行为
        （不经 `resolve_code_shipped_asset_sources` 退回仓库根），必须复现丢失——
        证明上一条测试测的是真问题，不是巧合过绿。"""
        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))

        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_assets_mut")
        (repo / ".nojekyll").write_text("")
        (repo / "chart.umd.min.js").write_text("console.log('chart');")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ship static assets"],
                        cwd=str(repo), check=True)

        reporter = _make_reporter(monkeypatch, tmp_path, repo)
        (data_root / "index.html").write_text("<html>v1</html>")

        # 旧行为：白名单退回空集合 ⇒ CODE_SHIPPED_STATIC_ASSETS 永远"两处都没有"。
        with patch("report_deployer.resolve_code_shipped_asset_sources", return_value={}), \
             patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        clone = tmp_path / "verify_clone_assets_mut"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone)],
                        check=True, capture_output=True)
        assert not (clone / ".nojekyll").exists(), (
            "还原旧行为后这条断言应该失败才对——如果它通过了，说明修复其实不依赖 "
            "resolve_code_shipped_asset_sources，上面的正向测试没有对照价值")
        assert not (clone / "chart.umd.min.js").exists()


class TestResolveGhPagesParentSingleBranchClone:
    """2026-09-21 二次检查缺陷②：`--single-branch` 克隆下，`git fetch origin
    gh-pages`（不带显式目标 refspec）会以 exit 0 收场但不更新
    `origin/gh-pages`——旧代码把这个状态误判成"远端还没有这个分支：真·首次
    部署"，导致建无父提交、非 force push 因非快进被拒、4 次重试全部失败，
    且 `parent_verified` 全程撒谎说"已校验"。用真实本地裸仓库当 origin，
    不碰真实 GitHub。
    """

    def _make_single_branch_clone(self, tmp_path, name, *, seed_gh_pages: bool):
        """建一个已有 main（+ 可选 gh-pages）的裸仓库，再用
        `--single-branch --branch main` 克隆它——复现阶段 8"新 clone 生产代码"
        的默认 git 行为。"""
        bare = tmp_path / f"{name}_origin.git"
        seed = tmp_path / f"{name}_seed"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        _git("config", "user.email", "seed@test.com", cwd=seed)
        _git("config", "user.name", "seed", cwd=seed)
        _git("remote", "add", "origin", str(bare), cwd=seed)
        (seed / "README.md").write_text("code")
        _git("add", "-A", cwd=seed)
        _git("commit", "-q", "-m", "init", cwd=seed)
        _git("push", "-q", "origin", "HEAD:main", cwd=seed)

        expected_head = None
        if seed_gh_pages:
            tree = _build_tree(seed, "site.html", "existing site")
            commit = subprocess.run(
                ["git", "commit-tree", tree, "-m", "existing gh-pages"],
                cwd=str(seed), capture_output=True, text=True, check=True,
            ).stdout.strip()
            subprocess.run(["git", "update-ref", "refs/heads/gh-pages", commit],
                            cwd=str(seed), check=True)
            subprocess.run(["git", "push", "-q", "origin", "gh-pages"],
                            cwd=str(seed), check=True, capture_output=True)
            expected_head = commit

        clone = tmp_path / f"{name}_single_branch_clone"
        subprocess.run(
            ["git", "clone", "-q", "--single-branch", "--branch", "main", str(bare), str(clone)],
            check=True, capture_output=True,
        )
        # 前置条件：这确实是个 single-branch 克隆——默认 fetch refspec 只认 main。
        refspec = _git("config", "--get", "remote.origin.fetch", cwd=clone)
        assert refspec == "+refs/heads/main:refs/remotes/origin/main", (
            f"前置条件不成立，不是预期的 single-branch 克隆：{refspec!r}")
        return clone, bare, expected_head

    def test_remote_has_gh_pages_but_single_branch_clone_must_still_see_it(self, tmp_path):
        """远端确实已经有 gh-pages（且非空）——`--single-branch` 克隆不该把这
        误判成"首次部署"，必须拿到真实父提交并标记为已校验。"""
        clone, bare, expected_head = self._make_single_branch_clone(
            tmp_path, "has_ghpages", seed_gh_pages=True)

        parent, verified = rd.resolve_gh_pages_parent(str(clone))

        assert verified is True, "fetch 用显式 refspec 后应该能确认远端状态"
        assert parent == expected_head, (
            f"父提交必须是远端 gh-pages 的真实头 {expected_head}，不是 None/陈旧值，实得 {parent}")

    def test_remote_truly_has_no_gh_pages_is_verified_first_deploy(self, tmp_path):
        """远端确实还没有 gh-pages 分支——这才是唯一该判"真·首次部署"的情形，
        `ls-remote` 正面核实之后必须仍然给 verified=True（不能因为改用显式
        refspec 就退化成"网络失败"的未经校验分支）。"""
        clone, bare, expected_head = self._make_single_branch_clone(
            tmp_path, "no_ghpages", seed_gh_pages=False)
        assert expected_head is None

        parent, verified = rd.resolve_gh_pages_parent(str(clone))

        assert verified is True, "ls-remote 应该能正面核实远端没有这个分支"
        assert parent is None

    def test_full_deploy_succeeds_on_single_branch_clone_with_existing_site(self, tmp_path):
        """端到端：在 single-branch 克隆里跑一次完整的 `commit_and_push_gh_pages`，
        必须成功、必须接在远端真实头后面——不能建出无父提交导致 push 被拒。
        这是旧 bug 实际造成的后果（4 次重试全部因非快进失败，gh-pages 永久
        停更）。"""
        clone, bare, expected_head = self._make_single_branch_clone(
            tmp_path, "e2e", seed_gh_pages=True)

        tree = _build_tree(clone, "today.html", "from single-branch clone")
        result = rd.commit_and_push_gh_pages(str(clone), tree, lambda n: "single-branch deploy")

        assert result["success"], result
        assert result["parent"] == expected_head
        assert result["parent_verified"] is True
        assert result["attempts"] == 1, "父提交本该一次校验成功，不需要任何重试"

        # gh-pages 树本来就是每次整棵重建（不是逐文件合并），"接在旧头后面"
        # 验证的是**提交历史**祖先关系，不是文件内容延续——`_build_tree` 只建了
        # today.html 一个文件的 tree，site.html 不会出现在这次的树里，这是设计
        # 使然，不是 bug。
        is_ancestor = subprocess.run(
            ["git", "--git-dir", str(bare), "merge-base", "--is-ancestor",
             expected_head, "refs/heads/gh-pages"],
        )
        assert is_ancestor.returncode == 0, (
            "远端已有的 gh-pages 头必须仍是新提交的祖先——如果不是，说明新提交是"
            "无父孤儿提交，旧 bug 又回来了")

        verify_clone = tmp_path / "e2e_verify"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(verify_clone)],
                        check=True, capture_output=True)
        assert (verify_clone / "today.html").read_text() == "from single-branch clone"

    def test_mutation_bare_branch_name_fetch_misreads_as_first_deploy(self, tmp_path):
        """变异测试：退回旧实现（`git fetch origin gh-pages`，不带显式目标
        refspec），必须复现"远端明明有 gh-pages、却被误判成首次部署"——证明
        上面几条测试测的是真问题，不是因为本来就会通过。"""
        clone, bare, expected_head = self._make_single_branch_clone(
            tmp_path, "mutation", seed_gh_pages=True)
        assert expected_head is not None

        old_fetch = subprocess.run(["git", "fetch", "origin", "gh-pages"],
                                    cwd=str(clone), capture_output=True, text=True)
        assert old_fetch.returncode == 0, "旧实现的 fetch 本身应该'成功'——这正是问题所在"
        old_rev_parse = subprocess.run(["git", "rev-parse", "origin/gh-pages"],
                                        cwd=str(clone), capture_output=True, text=True)
        assert old_rev_parse.returncode != 0, (
            "如果这条断言失败，说明旧实现在这个环境里没有复现 bug——"
            "上面的正向测试就没有对照价值了")


class TestSyncGhpagesSharesAssetFallback:
    """`generate_ml_report._sync_ghpages` 是 gh-pages 的第二条独立部署路径
    （与 `report_deployer.deploy_static_to_ghpages` 各自维护过一份内容相同的
    白名单）——验收标准③的修复必须两条路径都接上，不能只改一处。"""

    def test_repo_only_assets_reach_gh_pages_via_sync_ghpages(self, tmp_path, monkeypatch):
        import generate_ml_report as gmr

        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))

        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_sync")
        (repo / ".nojekyll").write_text("")
        (repo / "chart.umd.min.js").write_text("console.log('chart');")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ship static assets"],
                        cwd=str(repo), check=True)
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(repo))

        (data_root / "index.html").write_text("<html>ml v1</html>")

        gmr._sync_ghpages(["AAPL"], successful_count=1)

        clone = tmp_path / "verify_clone_sync"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone)],
                        check=True, capture_output=True)
        assert (clone / ".nojekyll").exists(), (
            "_sync_ghpages 走的是独立于 deploy_static_to_ghpages 的另一条部署路径，"
            "必须同样把只存在于仓库根的静态资源带上")
        assert (clone / "chart.umd.min.js").read_text() == "console.log('chart');"
        assert (clone / "index.html").read_text() == "<html>ml v1</html>"

    def test_mutation_reverting_fallback_makes_assets_vanish(self, tmp_path, monkeypatch):
        """变异测试：`_sync_ghpages` 内部调的是
        `report_deployer.resolve_code_shipped_asset_sources`——把它退回旧行为
        （永远找不到任何 fallback），必须复现丢失。"""
        import generate_ml_report as gmr

        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))

        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_sync_mut")
        (repo / ".nojekyll").write_text("")
        (repo / "chart.umd.min.js").write_text("console.log('chart');")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ship static assets"],
                        cwd=str(repo), check=True)
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(repo))

        (data_root / "index.html").write_text("<html>ml v1</html>")

        with patch("report_deployer.resolve_code_shipped_asset_sources", return_value={}):
            gmr._sync_ghpages(["AAPL"], successful_count=1)

        clone = tmp_path / "verify_clone_sync_mut"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone)],
                        check=True, capture_output=True)
        assert not (clone / ".nojekyll").exists(), (
            "还原旧行为后这条断言应该失败才对——说明修复其实不依赖 "
            "resolve_code_shipped_asset_sources，上面的正向测试没有对照价值")


class TestEmptyDataRootDoesNotWipeGhPages:
    """2026-09-22 code-review（对 v0.45.305 的高强度复检）发现的严重回归，
    v0.45.310 已修：「无静态文件可部署」的守卫此前在随代码发布的静态资源
    并入 `files` **之后**才判——`.nojekyll`/`chart.umd.min.js` 几乎总能在
    git 仓库根找到（它们随仓库提交），data_root 整个空掉（配置错误/阶段 5
    迁移中/上游扫描没写出任何东西）时 `files` 也不会是空的，guard 形同虚设，
    会把 gh-pages 整棵重建成只剩这两个文件，等于清空线上网站。

    两条独立部署路径（`deploy_static_to_ghpages`/`_sync_ghpages`）各验一次：
    先正常部署一次建立"线上已有真实内容"的前置状态，再清空 data_root 重跑，
    断言线上内容原样保留、没有被两个静态资源替换掉。
    """

    def test_deploy_static_to_ghpages_skips_when_data_root_empty(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))

        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_empty")
        (repo / ".nojekyll").write_text("")
        (repo / "chart.umd.min.js").write_text("console.log('chart');")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ship static assets"], cwd=str(repo), check=True)

        reporter = _make_reporter(monkeypatch, tmp_path, repo)

        # 先部署一次正常内容，建立"线上已有真实网站"的前置状态。
        (data_root / "index.html").write_text("<html>real site</html>")
        (data_root / "dashboard-data.json").write_text('{"_generated_at": "t1"}')
        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        clone_before = tmp_path / "verify_before"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone_before)],
                        check=True, capture_output=True)
        assert (clone_before / "index.html").read_text() == "<html>real site</html>"

        # data_root 整个清空（模拟配置错误/扫描失败）；仓库根仍有那两个静态资源。
        for f in os.listdir(data_root):
            os.remove(data_root / f)
        assert list(os.listdir(data_root)) == [], "前置条件不成立：data_root 应该已被清空"

        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        clone_after = tmp_path / "verify_after"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone_after)],
                        check=True, capture_output=True)
        assert (clone_after / "index.html").read_text() == "<html>real site</html>", (
            "data_root 清空后必须跳过部署、保留线上原内容——不能把 gh-pages 整棵"
            "重建成只剩 .nojekyll/chart.umd.min.js 两个文件")
        assert (clone_after / "dashboard-data.json").exists()

    def test_sync_ghpages_skips_when_data_root_empty(self, tmp_path, monkeypatch):
        import generate_ml_report as gmr

        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))

        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_empty_sync")
        (repo / ".nojekyll").write_text("")
        (repo / "chart.umd.min.js").write_text("console.log('chart');")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ship static assets"], cwd=str(repo), check=True)
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(repo))

        (data_root / "index.html").write_text("<html>real ml site</html>")
        gmr._sync_ghpages(["AAPL"], successful_count=1)

        clone_before = tmp_path / "verify_before_sync"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone_before)],
                        check=True, capture_output=True)
        assert (clone_before / "index.html").read_text() == "<html>real ml site</html>"

        for f in os.listdir(data_root):
            os.remove(data_root / f)
        assert list(os.listdir(data_root)) == [], "前置条件不成立：data_root 应该已被清空"

        gmr._sync_ghpages(["AAPL"], successful_count=1)

        clone_after = tmp_path / "verify_after_sync"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone_after)],
                        check=True, capture_output=True)
        assert (clone_after / "index.html").read_text() == "<html>real ml site</html>", (
            "data_root 清空后必须跳过同步、保留线上原内容——不能把 gh-pages 整棵"
            "重建成只剩 .nojekyll/chart.umd.min.js 两个文件")
