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
import tempfile
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
        ("Backtester", None),
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
        # `commit_and_push_gh_pages` 走 `git commit-tree`（plumbing 命令），同
        # `_init_repo_with_origin` 里 `repo` 的既有约定一样，必须显式配置本地
        # 身份——不能依赖 git 从系统用户名/主机名猜身份这条隐式回落路径：
        # macOS 的 Apple Git 会静默猜出一个可用身份，但 CI（Ubuntu 跑者，GECOS
        # 全名字段常年为空）猜出的姓名部分是空字符串，新版 git 对此硬拒绝
        # （`fatal: empty ident name ... not allowed`），导致本条测试只在
        # 本机能过、CI 上必现失败——这不是环境噪音，是测试自己漏配了身份。
        _git("config", "user.email", "single-branch-clone@test.com", cwd=clone)
        _git("config", "user.name", "single-branch-clone", cwd=clone)
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


class TestResolveGhPagesParentNetworkTimeout:
    """v0.45.311：`resolve_gh_pages_parent` 里真正触网的 `git fetch`/
    `git ls-remote` 此前不设超时——网络真的挂起（不是报错，是没反应）时会
    无限期阻塞，而本函数被包在 `commit_and_push_gh_pages` 最多 4 次的重试
    循环里，一次挂起会被放大成整条部署流水线长时间卡死。

    用一个只 accept 连接、从不回应任何字节的裸 TCP 监听器模拟"网络挂起但不
    报错"的真实故障：`git://` 协议的客户端连上后会一直阻塞等服务端发送第一行
    ref 广播，永远等不到——这正是"挂起"而非"报错"的真实形态，`--reject`/
    连不上端口这类立即失败的场景不需要超时也能正常工作，不是本次要测的东西。
    """

    def test_hung_remote_times_out_instead_of_blocking_forever(self, tmp_path, monkeypatch):
        import socket
        import threading
        import time

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def _accept_and_hang():
            srv.settimeout(1.0)
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                # 接了连接就什么都不发、不关闭——让对端的 git 客户端一直
                # 阻塞在等第一行响应上，直到我们主动收尾。
                stop.wait(30)
                conn.close()

        t = threading.Thread(target=_accept_and_hang, daemon=True)
        t.start()
        try:
            repo = tmp_path / "hung_repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _git("config", "user.email", "t@t.com", cwd=repo)
            _git("config", "user.name", "t", cwd=repo)
            _git("remote", "add", "origin", f"git://127.0.0.1:{port}/nonexistent.git", cwd=repo)

            monkeypatch.setattr(rd, "_GH_PAGES_NETWORK_TIMEOUT", 2)

            start = time.monotonic()
            parent, verified = rd.resolve_gh_pages_parent(str(repo))
            elapsed = time.monotonic() - start

            assert elapsed < 15, (
                f"耗时 {elapsed:.1f}s——超时设置没有生效，函数被两次网络调用"
                "（fetch 挂起后还要再等 ls-remote 挂起一次）的默认（无限期）"
                "超时卡住了，不是设置的 2s×2 上限")
            assert verified is False, "网络挂起应该判定为未经校验，不能冒充已验证"
        finally:
            stop.set()
            srv.close()
            t.join(timeout=5)

    def test_mutation_without_timeout_hangs(self, tmp_path, monkeypatch):
        """变异测试：把 `_run_git_network` 换回不设超时的裸 `subprocess.run`，
        必须复现"明显更久（逼近或超过我们设的安全上限）"——证明上面那条测试
        测的是真问题，不是因为 git 本身连接失败得很快。用一个短但可观测的
        安全上限（5s）而非真的等到 git 默认超时（可能几分钟），避免这条
        对照测试本身拖垮测试套件。"""
        import socket
        import subprocess as _sp
        import threading
        import time

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def _accept_and_hang():
            srv.settimeout(1.0)
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                stop.wait(30)
                conn.close()

        t = threading.Thread(target=_accept_and_hang, daemon=True)
        t.start()

        def _no_timeout_run(args, repo):
            # 旧实现：不传 timeout=
            try:
                return _sp.run(args, cwd=repo, capture_output=True, text=True)
            except OSError:
                return None

        monkeypatch.setattr(rd, "_run_git_network", _no_timeout_run)
        try:
            repo = tmp_path / "hung_repo_mut"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _git("config", "user.email", "t@t.com", cwd=repo)
            _git("config", "user.name", "t", cwd=repo)
            _git("remote", "add", "origin", f"git://127.0.0.1:{port}/nonexistent.git", cwd=repo)

            def _call_with_own_timeout():
                rd.resolve_gh_pages_parent(str(repo))

            start = time.monotonic()
            call_thread = threading.Thread(target=_call_with_own_timeout, daemon=True)
            call_thread.start()
            call_thread.join(timeout=5)
            elapsed = time.monotonic() - start
            assert call_thread.is_alive(), (
                f"还原成不设超时的旧实现后，函数应该仍卡在网络调用上（{elapsed:.1f}s "
                "还没返回）——如果它这时候已经返回了，说明这条对照测试没有测到"
                "真问题，上面的正向测试就没有对照价值")
        finally:
            stop.set()
            srv.close()
            t.join(timeout=5)
            # call_thread 是 daemon 线程，挂起的子进程会在进程退出时被清理；
            # 这里不等它了（它本来就卡住，join 只会拖慢测试收尾）。


class TestSyncGhpagesResilientToSingleFileFailure:
    """v0.45.311：`_sync_ghpages`（`generate_ml_report.py`）此前把整段
    hash-object/commit/push 包在一个大 `try/except` 里——任何一个文件 hash
    失败会让整次同步全部放弃，不像 `report_deployer.deploy_static_to_ghpages`
    早就是逐文件容错（单个文件失败只跳过该文件、记警告，其它文件照常部署）。
    改成同样的逐文件容错后，本文件验证"一个坏文件不该拖垮整次同步"。
    """

    def test_one_bad_file_does_not_abort_the_whole_sync(self, tmp_path, monkeypatch):
        import generate_ml_report as gmr
        import subprocess as _sp

        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_resilience")
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(repo))

        (data_root / "index.html").write_text("<html>good</html>")
        (data_root / "dashboard-data.json").write_text('{"_generated_at": "t1"}')

        real_check_output = _sp.check_output

        def _flaky_check_output(args, **kwargs):
            if (args[:2] == ["git", "hash-object"]
                    and str(args[-1]).endswith("dashboard-data.json")):
                raise _sp.CalledProcessError(1, args, output=b"", stderr=b"simulated hash failure")
            return real_check_output(args, **kwargs)

        with patch("subprocess.check_output", side_effect=_flaky_check_output):
            gmr._sync_ghpages(["AAPL"], successful_count=1)

        clone = tmp_path / "verify_clone_resilience"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone)],
                        check=True, capture_output=True)
        assert (clone / "index.html").read_text() == "<html>good</html>", (
            "一个文件 hash-object 失败不该让整次同步全部放弃——其它文件应该照常部署")
        assert not (clone / "dashboard-data.json").exists(), (
            "hash 失败的那个文件不该出现在部署结果里——它本身就没能生成 blob")

    def test_mutation_reverting_per_file_resilience_aborts_whole_sync(self, tmp_path, monkeypatch):
        """变异测试：把 hash-object 循环还原成"整段包一个大 try/except、单个
        文件失败直接抛出"的旧写法，必须复现"一个坏文件拖垮整次同步"——证明
        上面那条测试测的是真问题。"""
        import generate_ml_report as gmr
        import subprocess as _sp

        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_resilience_mut")
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(repo))

        (data_root / "index.html").write_text("<html>good</html>")
        (data_root / "dashboard-data.json").write_text('{"_generated_at": "t1"}')

        real_check_output = _sp.check_output

        def _flaky_check_output(args, **kwargs):
            if (args[:2] == ["git", "hash-object"]
                    and str(args[-1]).endswith("dashboard-data.json")):
                raise _sp.CalledProcessError(1, args, output=b"", stderr=b"simulated hash failure")
            return real_check_output(args, **kwargs)

        # 旧写法：把整段 hash-object 循环搬进外层 try/except 之内、不做逐文件
        # 捕获——一个文件 hash 失败会直接从循环里抛出，被外层大 except 接住，
        # 整次同步不建任何提交。
        def _old_style_sync_ghpages(tickers, successful_count):
            import os
            from report_deployer import (
                CODE_SHIPPED_STATIC_ASSETS as _assets,
                CORE_STATIC_FILES as _core,
                apply_code_shipped_fallback as _apply,
            )
            if successful_count == 0:
                return
            data_root_ = str(gmr.PATHS.home)
            repo_ = str(gmr.PATHS.git_repo_root)
            _CORE = _core | _assets
            files = [f for f in os.listdir(data_root_) if f in _CORE]
            file_source = {f: data_root_ for f in files}
            if not files:
                return
            _apply(files, file_source, data_root_, repo_, "同步")
            idx = os.path.join(repo_, ".git", "gh-pages-index")
            if os.path.exists(idx):
                os.remove(idx)
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = idx
            try:
                for f in sorted(files):
                    blob = _sp.check_output(
                        ["git", "hash-object", "-w", os.path.join(file_source[f], f)], cwd=repo_
                    ).decode().strip()
                    _sp.run(["git", "update-index", "--add", "--cacheinfo",
                             "100644", blob, f], env=env, cwd=repo_, check=True)
                tree = _sp.check_output(["git", "write-tree"], env=env, cwd=repo_).decode().strip()
                from report_deployer import commit_and_push_gh_pages
                commit_and_push_gh_pages(repo_, tree, lambda n: "old style")
            except Exception:
                pass
            finally:
                if os.path.exists(idx):
                    os.remove(idx)

        with patch("subprocess.check_output", side_effect=_flaky_check_output):
            _old_style_sync_ghpages(["AAPL"], successful_count=1)

        # 旧写法下 gh-pages 分支应该压根没建出来（首次部署、整段异常被吞掉）。
        precheck = subprocess.run(["git", "ls-remote", "--heads", str(bare), "gh-pages"],
                                   capture_output=True, text=True)
        assert not precheck.stdout.strip(), (
            "如果这条断言失败，说明旧写法在这个环境里没有复现'一个坏文件拖垮整次同步'——"
            "上面的正向测试就没有对照价值了")


class TestAllHashObjectFailuresDoNotPushEmptyTree:
    """v0.45.312：`/code-review high` 对 v0.45.311（把 `_sync_ghpages` 改成
    逐文件容错）做二次复检时，真实执行发现的严重回归——**不是**v0.45.311
    本身新写的代码，是 `deploy_static_to_ghpages` 里那段批量 hash-object 逻辑
    从更早版本起就缺这道守卫：`cache_entries` 全空时（比如磁盘满/
    `.git/objects` 权限损坏，导致每一个候选文件的 `hash-object` 都失败）此前
    直接落到 `write-tree`——`GIT_INDEX_FILE` 指向一个从没写过内容的空 index，
    `git write-tree` 返回 git 那个众所周知的**空树**哈希，`commit_and_push_
    gh_pages` 照样把这棵空树提交推送上去，把 gh-pages **整站清空**、日志却
    打印"部署成功"。

    两条独立部署路径都验一次（`_sync_ghpages` 在 v0.45.311 已经有这道守卫，
    这里补的是回归覆盖；`deploy_static_to_ghpages` 是本次新补的守卫）。
    """

    def test_deploy_static_to_ghpages_skips_when_all_hash_object_fail(self, tmp_path, monkeypatch):
        import subprocess as _sp

        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_all_hash_fail")
        reporter = _make_reporter(monkeypatch, tmp_path, repo)

        (data_root / "index.html").write_text("<html>real site</html>")
        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        clone_before = tmp_path / "verify_before_allfail"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone_before)],
                        check=True, capture_output=True)
        assert (clone_before / "index.html").read_text() == "<html>real site</html>"

        real_check_output = _sp.check_output

        def _all_hash_object_fail(args, **kwargs):
            if args[:2] == ["git", "hash-object"]:
                raise _sp.CalledProcessError(1, args, output=b"", stderr=b"simulated total failure")
            return real_check_output(args, **kwargs)

        with patch("subprocess.check_output", side_effect=_all_hash_object_fail), \
             patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        clone_after = tmp_path / "verify_after_allfail"
        subprocess.run(["git", "clone", "-q", "--branch", "gh-pages", str(bare), str(clone_after)],
                        check=True, capture_output=True)
        assert (clone_after / "index.html").read_text() == "<html>real site</html>", (
            "全部候选文件 hash-object 失败时必须跳过本次部署、保留线上原内容——"
            "不能把 gh-pages 推成空树（git write-tree 在空 index 上返回的众所周知的"
            "空树哈希，会让 commit_and_push_gh_pages 照常成功提交推送）")

    def test_mutation_reverting_all_hash_fail_guard_pushes_empty_tree(self, tmp_path, monkeypatch):
        """变异测试：还原成没有这道守卫的旧写法（`git show HEAD:report_deployer.py`
        换出——那正是本次修复前的真实代码），必须复现"gh-pages 被清空成空树"。"""
        import subprocess as _sp

        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
        repo, bare = _init_repo_with_origin(tmp_path, "coderepo_all_hash_fail_mut")
        reporter = _make_reporter(monkeypatch, tmp_path, repo)

        (data_root / "index.html").write_text("<html>real site</html>")
        with patch("report_deployer.verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        real_check_output = _sp.check_output

        def _all_hash_object_fail(args, **kwargs):
            if args[:2] == ["git", "hash-object"]:
                raise _sp.CalledProcessError(1, args, output=b"", stderr=b"simulated total failure")
            return real_check_output(args, **kwargs)

        # 旧写法：`if cache_entries:` 只包住 update-index 调用，`write-tree`
        # 在它外面无条件执行——手工复刻这段（不是重新发明，是直接照抄修复前
        # 的真实结构），证明"跳到 write-tree"这条路径真的会产出空树。
        def _old_style_deploy(files, file_source, repo, env):
            import os as _os
            cache_entries = []
            for f in sorted(files):
                try:
                    blob = _sp.check_output(
                        ["git", "hash-object", "-w", _os.path.join(file_source[f], f)], cwd=repo
                    ).decode().strip()
                    cache_entries.append(f"100644 {blob}\t{f}")
                except (_sp.CalledProcessError, OSError):
                    pass
            if cache_entries:
                _idx_input = "\n".join(cache_entries) + "\n"
                _sp.run(["git", "update-index", "--add", "--index-info"],
                         input=_idx_input, env=env, cwd=repo, check=True, text=True)
            tree = _sp.check_output(["git", "write-tree"], env=env, cwd=repo).decode().strip()
            return tree

        idx = str(repo / ".git" / "gh-pages-index-mut")
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = idx
        with patch("subprocess.check_output", side_effect=_all_hash_object_fail):
            tree = _old_style_deploy(["index.html"], {"index.html": str(data_root)}, str(repo), env)
        os.remove(idx)

        empty_tree_sha = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        assert tree == empty_tree_sha, (
            "如果这条断言失败，说明旧写法在这个环境里没有产出空树——"
            "上面的正向测试就没有对照价值了")


class TestApplyCodeShippedFallbackPreconditionSurvivesOptimization:
    """v0.45.312 把 `apply_code_shipped_fallback` 的"调用前 `files` 必须非空"
    前提改成裸 `assert` 强制；v0.45.317 第三轮复检发现这就是新 bug——本仓
    生产模块（非 tests/ 下）从无先例用 `assert` 做不变式强制，`probability_
    scorecard.py` 586-591 行有明文理由：`python -O` 会把 assert 剥掉，一个
    会被剥掉的不变式，正是"把失败改写成没发生过"。

    真实执行验证（不是读代码猜）：用 `/usr/local/bin/python3 -O` 子进程
    真跑同一次空 `files` 调用——`assert` 版本在 `-O` 下不抛任何异常、`files`
    照常被回落结果撑满（v0.45.305/v0.45.310 那个"空 data_root 看起来像有
    内容"的 bug 原样复活）；`raise ValueError` 版本与解释器优化开关无关，
    `-O` 下依然抛出。
    """

    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _PROBE = (
        "import report_deployer as rd\n"
        "files = []\n"
        "try:\n"
        "    rd.apply_code_shipped_fallback(files, {}, '/tmp/ah-nonexistent-data-root', '.', 't')\n"
        "    print('GUARD_DID_NOT_FIRE:' + repr(files))\n"
        "except (ValueError, AssertionError) as e:\n"
        "    print('GUARD_FIRED:' + type(e).__name__)\n"
    )

    def _run_probe(self, extra_args):
        return subprocess.run(
            ["/usr/local/bin/python3", *extra_args, "-c", self._PROBE],
            cwd=self._REPO_ROOT, capture_output=True, text=True,
        )

    def test_empty_files_raises_value_error_under_normal_python(self):
        r = self._run_probe([])
        assert "GUARD_FIRED:ValueError" in r.stdout, (
            f"当前修复版应在普通解释器下也抛 ValueError；实际 stdout={r.stdout!r} "
            f"stderr={r.stderr[-500:]!r}")

    def test_empty_files_still_raises_under_python_dash_O(self):
        """当前修复版（`raise ValueError`）必须在 `-O` 下依然拦得住——
        这是本次修复真正要保证的东西，不是"正常模式下拦住"这件事本身
        （那件事旧的 `assert` 版本也做得到）。"""
        r = self._run_probe(["-O"])
        assert "GUARD_FIRED:ValueError" in r.stdout, (
            "python -O 下守卫失效——回到了 v0.45.312 assert 版本的原始 bug："
            f"stdout={r.stdout!r} stderr={r.stderr[-500:]!r}")

    # v0.45.317 修复本身的最后一个 commit 是 c1aebfe7；它的父提交 61f21d37
    # 是本次修复前的占位提交，report_deployer.py 在那里仍是 v0.45.312 的裸
    # assert 版本。**必须钉死这个具体 SHA，不能用 `HEAD`**——首版测试写的是
    # `git show HEAD:report_deployer.py`，这条前提只在修复提交之前（HEAD 还
    # 指向父提交时）成立；修复一旦提交、成为新 HEAD，这个断言就永远为假，
    # 测试永久变红（本条注释本身就是被这个 bug 逮到后改的：main 上实测过，
    # 提交后立刻用 HEAD 重跑就会失败）。钉 SHA 而不是相对引用，才能让这条
    # 变异测试在任何时候、任何分支上重跑都还原出同一份历史源码。
    _OLD_ASSERT_SHA = "61f21d37"

    def test_mutation_old_assert_guard_is_silently_stripped_under_dash_O(self):
        """变异检验：换回 v0.45.312 的真实旧代码（`assert files, (...)`），
        证明"-O 下守卫消失"不是臆测——用改动前的真实源码真跑确认转红。"""
        old_source = subprocess.run(
            ["git", "show", f"{self._OLD_ASSERT_SHA}:report_deployer.py"],
            cwd=self._REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
        assert "assert files, (" in old_source, (
            f"本条变异测试假定 {self._OLD_ASSERT_SHA} 上 report_deployer.py 是 "
            "v0.45.312 的裸 assert 版本——如果这条断言失败，说明这个钉死的 SHA "
            "选错了或历史被改写，该重新核实是哪个提交引入的 bug，而不是让这条"
            "测试悄悄测不出任何东西")
        with tempfile.TemporaryDirectory() as tmp:
            shadow = os.path.join(tmp, "report_deployer.py")
            with open(shadow, "w", encoding="utf-8") as f:
                f.write(old_source)
            env = os.environ.copy()
            # tmp 排 PYTHONPATH 第一位，确保 `import report_deployer` 命中
            # 影子（旧代码）版本，不是仓库里的真实文件；同时保留仓库根，
            # 因为 report_deployer.py 自己还要 `import production_sync`/
            # `hive_logger`，这两个模块不在影子目录里。
            env["PYTHONPATH"] = (
                tmp + os.pathsep + self._REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
            )
            r = subprocess.run(
                ["/usr/local/bin/python3", "-O", "-c", self._PROBE],
                cwd=tmp, capture_output=True, text=True, env=env,
            )
        assert "GUARD_DID_NOT_FIRE:" in r.stdout, (
            "如果这条断言失败，说明旧 assert 写法在这个环境的 -O 下没有被剥掉——"
            f"上面两条正向测试就没有对照价值了。stdout={r.stdout!r} stderr={r.stderr[-500:]!r}")
