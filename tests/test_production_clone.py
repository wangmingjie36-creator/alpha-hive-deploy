"""生产代码独立克隆的守卫（数据根迁移阶段 8，v0.45.431）

全部在**真 git 沙箱**里跑（bare origin + 独立克隆 + 另一个会话的克隆），不打桩 git：

  1. 守卫有牙：commit / commit --no-verify / update-ref / merge / rebase / reset / push main 都拦得住，
     且被拦之后 main 原地不动
  2. 正对照：生产真正要做的两件事照常——production_sync 的快进、report_deployer 的 gh-pages 推送
  3. 不该装的地方不装：worktree、挂着 worktree 的开发仓库一个字节都不写；foreign 钩子不覆盖
  4. 体检能看见：origin 不对 / core.hooksPath 改道 / 工作区被手改 / 钩子被改 都让 ok=False
  5. 结局走到告警：production_sync（环境变量开关）→ production_sync.json → scan_timing → alert_manager
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import production_clone as pc  # noqa: E402
import production_sync as ps  # noqa: E402
import scan_timing as st  # noqa: E402
from agent_toolbox import GitHubTool  # noqa: E402

_GIT_ENV_LEAKS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                  "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def _run(*a, cwd, check=True):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check)


@pytest.fixture
def world(tmp_path, monkeypatch):
    # 从 git hook 里被拉起时继承的 GIT_DIR 会让下面每条 git 命令打到真仓库上
    for var in _GIT_ENV_LEAKS:
        monkeypatch.delenv(var, raising=False)
    origin, seed, prod, other = (tmp_path / n for n in ("origin.git", "seed", "prod", "other"))
    _run("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _run("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
    _run("config", "user.email", "seed@t", cwd=seed)
    _run("config", "user.name", "seed", cwd=seed)
    (seed / "code.py").write_text("v1\n")
    (seed / "notes.md").write_text("n\n")
    _run("add", "-A", cwd=seed)
    _run("commit", "-qm", "init", cwd=seed)
    _run("remote", "add", "origin", str(origin), cwd=seed)
    _run("push", "-q", "origin", "main", cwd=seed)
    # gh-pages：一个无父的网站提交（与真仓库同形：完整克隆会带上 origin/gh-pages）
    empty_tree = subprocess.run(["git", "mktree"], cwd=seed, input="", capture_output=True,
                                text=True, check=True).stdout.strip()
    site = _run("commit-tree", empty_tree, "-m", "site", cwd=seed).stdout.strip()
    _run("push", "-q", "origin", f"{site}:refs/heads/gh-pages", cwd=seed)

    _run("clone", "-q", str(origin), str(prod), cwd=tmp_path)
    _run("clone", "-q", str(origin), str(other), cwd=tmp_path)
    for repo in (prod, other):
        _run("config", "user.email", "x@t", cwd=repo)
        _run("config", "user.name", "x", cwd=repo)

    def git(*a, cwd=prod, check=True):
        return _run(*a, cwd=cwd, check=check)

    def session_push(name, text, msg):
        """另一个会话（开发 worktree）：同步、改一个文件、推 origin/main。返回它推上去的提交。"""
        git("pull", "-q", "--ff-only", "origin", "main", cwd=other)
        (other / name).write_text(text)
        git("add", "-A", cwd=other)
        git("commit", "-qm", msg, cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        return git("rev-parse", "HEAD", cwd=other).stdout.strip()

    def head(ref="HEAD"):
        return git("rev-parse", ref).stdout.strip()

    monkeypatch.setattr(pc, "DEFAULT_ORIGIN", str(origin))
    return SimpleNamespace(origin=origin, prod=prod, other=other, git=git, head=head,
                           session_push=session_push, tmp=tmp_path)


@pytest.fixture
def guarded(world):
    g = pc.ensure_guard(world.prod)
    assert g["ok"] is True, g
    assert set(g["actions"]) == set(pc.HOOKS) and set(g["actions"].values()) == {"installed"}, g
    return world


# ═════════════════════════════ 1. 守卫有牙 ═════════════════════════════

class TestDirectChangesToMainAreRefused:
    def test_commit_is_refused(self, guarded):
        w = guarded
        before = w.head()
        (w.prod / "code.py").write_text("hotfix\n")
        w.git("add", "code.py")
        r = w.git("commit", "-qm", "hotfix", check=False)
        assert r.returncode != 0
        assert "生产克隆" in r.stderr, r.stderr
        assert w.head() == before

    def test_commit_no_verify_is_still_refused_by_the_ref_transaction(self, guarded):
        """--no-verify 跳过 pre-commit；reference-transaction 不受它影响——这才是不变式。"""
        w = guarded
        before = w.head()
        (w.prod / "code.py").write_text("hotfix\n")
        w.git("add", "code.py")
        r = w.git("commit", "--no-verify", "-qm", "hotfix", check=False)
        assert r.returncode != 0
        assert "不在 origin/main 的历史里" in r.stderr, r.stderr
        assert w.head() == before

    def test_plumbing_update_ref_is_refused(self, guarded):
        w = guarded
        before = w.head()
        tree = w.git("write-tree").stdout.strip()
        c = w.git("commit-tree", tree, "-p", "HEAD", "-m", "plumbing").stdout.strip()
        r = w.git("update-ref", "refs/heads/main", c, check=False)
        assert r.returncode != 0
        assert w.head("refs/heads/main") == before

    def test_deleting_main_is_refused(self, guarded):
        w = guarded
        w.git("checkout", "-q", "--detach")
        r = w.git("update-ref", "-d", "refs/heads/main", check=False)
        assert r.returncode != 0
        assert w.git("rev-parse", "--verify", "refs/heads/main", check=False).returncode == 0

    def test_merge_commit_is_refused_even_with_no_verify(self, guarded):
        w = guarded
        before = w.head()
        w.git("checkout", "-q", "-b", "side")
        (w.prod / "side.txt").write_text("s\n")
        w.git("add", "side.txt")
        w.git("commit", "--no-verify", "-qm", "side")   # 别的分支不归守卫管（main 才是生产跑的）
        w.git("checkout", "-q", "main")
        for extra in ([], ["--no-verify"]):
            r = w.git("merge", "--no-ff", "-m", "m", *extra, "side", check=False)
            assert r.returncode != 0, (extra, r.stdout, r.stderr)
            w.git("merge", "--abort", check=False)
            assert w.head() == before

    def test_rebase_is_refused(self, guarded):
        w = guarded
        w.session_push("code.py", "v2\n", "session: v2")
        w.git("fetch", "-q", "origin")
        r = w.git("rebase", "origin/main", check=False)
        assert r.returncode != 0
        assert "生产克隆" in r.stderr, r.stderr

    def test_reset_to_a_commit_outside_origin_is_refused(self, guarded):
        w = guarded
        before = w.head()
        tree = w.git("write-tree").stdout.strip()
        c = w.git("commit-tree", tree, "-p", "HEAD", "-m", "stray").stdout.strip()
        r = w.git("reset", "-q", "--hard", c, check=False)
        assert r.returncode != 0
        assert w.head("refs/heads/main") == before

    def test_push_to_main_is_refused(self, guarded):
        w = guarded
        # 先让本地有一个 origin 没有的提交（--no-verify + 另开分支绕开提交守卫），再试图推到 main
        w.git("checkout", "-q", "-b", "side")
        (w.prod / "side.txt").write_text("s\n")
        w.git("add", "side.txt")
        w.git("commit", "--no-verify", "-qm", "side")
        origin_main = _run("--git-dir", str(w.origin), "rev-parse", "main", cwd=w.tmp).stdout.strip()
        r = w.git("push", "origin", "side:refs/heads/main", check=False)
        assert r.returncode != 0 and "只许推 refs/heads/gh-pages" in r.stderr, r.stderr
        assert _run("--git-dir", str(w.origin), "rev-parse", "main", cwd=w.tmp).stdout.strip() == origin_main
        r = w.git("push", "origin", "HEAD:refs/heads/other-branch", check=False)
        assert r.returncode != 0 and "只许推 refs/heads/gh-pages" in r.stderr, r.stderr
        assert _run("--git-dir", str(w.origin), "rev-parse", "--verify", "refs/heads/other-branch",
                    cwd=w.tmp, check=False).returncode != 0

    def test_deleting_remote_gh_pages_is_refused(self, guarded):
        w = guarded
        r = w.git("push", "origin", ":refs/heads/gh-pages", check=False)
        assert r.returncode != 0 and "不许删除" in r.stderr, r.stderr


# ═════════════════════════════ 2. 正对照：生产要做的事照常 ═════════════════════════════

class TestProductionPathsStillWork:
    def test_production_sync_fast_forward_passes_the_hooks(self, guarded):
        """守卫误拦快进 = 生产永远停在旧代码。这条红就别合入。"""
        w = guarded
        new = w.session_push("code.py", "v2\n", "session: v2")
        res = ps.sync_before_scan(GitHubTool(repo_path=str(w.prod)), today="2026-10-08")
        assert res["outcome"] == "fast_forwarded", res
        assert w.head() == new
        assert (w.prod / "code.py").read_text() == "v2\n"

    def test_reset_back_to_an_older_origin_commit_is_allowed(self, guarded):
        """回滚到 origin/main 历史里的旧提交是有意操作（祖先 ⇒ 放行）；守卫只拦「main 上没有的东西」。"""
        w = guarded
        old = w.head()
        w.session_push("code.py", "v2\n", "session: v2")
        ps.sync_before_scan(GitHubTool(repo_path=str(w.prod)), today="2026-10-08")
        r = w.git("reset", "-q", "--hard", old, check=False)
        assert r.returncode == 0, r.stderr
        assert w.head() == old

    def test_gh_pages_deploy_passes_the_hooks(self, guarded):
        """report_deployer 的真实推送路径（fetch 父提交 → commit-tree → 非 force push → update-ref 本地 gh-pages）。"""
        import report_deployer as rd
        w = guarded
        blob =subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=w.prod, input="<html>10-08</html>",
                              capture_output=True, text=True, check=True).stdout.strip()
        tree = subprocess.run(["git", "mktree"], cwd=w.prod, input=f"100644 blob {blob}\tindex.html\n",
                              capture_output=True, text=True, check=True).stdout.strip()
        res = rd.commit_and_push_gh_pages(str(w.prod), tree, lambda n: "deploy 10-08")
        assert res["success"] is True and res["action"] == "pushed_new_commit", res
        remote = _run("--git-dir", str(w.origin), "rev-parse", "refs/heads/gh-pages^{tree}", cwd=w.tmp).stdout.strip()
        assert remote == tree

    def test_ensure_is_idempotent(self, guarded):
        again = pc.ensure_guard(guarded.prod)
        assert again["ok"] is True and again["actions"] == {}, again


# ═════════════════════════════ 3. 不该装的地方不装 ═════════════════════════════

class TestRefusesToTouchDevelopmentRepos:
    def _hooks_written(self, gitdir: Path):
        return sorted(n for n in pc.HOOKS if (gitdir / "hooks" / n).exists())

    def test_repo_with_linked_worktrees_gets_no_hooks(self, world):
        """开发仓库（~/Desktop/Alpha Hive 挂着几十个 worktree）：装进去会拦住所有 worktree 的提交。"""
        w = world
        w.git("worktree", "add", "-q", str(w.tmp / "wt"), "-b", "dev")
        g = pc.ensure_guard(w.prod)
        assert g["ok"] is False and g["standalone"] is False, g
        assert g["actions"] == {}
        assert self._hooks_written(w.prod / ".git") == []
        assert any(p.startswith("has_linked_worktrees") for p in g["problems"]), g["problems"]

    def test_a_worktree_itself_gets_no_hooks(self, world):
        w = world
        wt = w.tmp / "wt"
        w.git("worktree", "add", "-q", str(wt), "-b", "dev")
        g = pc.ensure_guard(wt)
        assert g["ok"] is False and g["standalone"] is False, g
        assert self._hooks_written(w.prod / ".git") == []
        assert any(p.startswith("is_worktree") for p in g["problems"]), g["problems"]

    def test_foreign_hook_is_not_overwritten_but_is_red(self, world):
        w = world
        hook = w.prod / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho someone else's hook\n")
        hook.chmod(0o755)
        g = pc.ensure_guard(w.prod)
        assert g["hooks"]["pre-commit"] == "foreign" and g["ok"] is False, g
        assert "pre-commit" not in g["actions"]
        assert hook.read_text() == "#!/bin/sh\necho someone else's hook\n"
        # 其余钩子照装：reference-transaction 在，提交仍被拦
        assert g["hooks"]["reference-transaction"] == "current"


# ═════════════════════════════ 4. 体检能看见 ═════════════════════════════

class TestInspectionSeesProblems:
    def test_check_is_read_only(self, world):
        r = pc.inspect(world.prod)
        assert set(r["hooks"].values()) == {"missing"} and r["ok"] is False, r
        assert not any((world.prod / ".git" / "hooks" / n).exists() for n in pc.HOOKS)

    def test_edited_hook_is_stale_and_gets_refreshed(self, guarded):
        hook = guarded.prod / ".git" / "hooks" / "reference-transaction"
        hook.write_text(hook.read_text().replace("rc=1", "rc=0"))   # 有人把拦截改成放行
        assert pc.inspect(guarded.prod)["hooks"]["reference-transaction"] == "stale"
        g = pc.ensure_guard(guarded.prod)
        assert g["actions"] == {"reference-transaction": "refreshed"} and g["ok"] is True, g
        assert hook.read_text() == pc.HOOKS["reference-transaction"]

    def test_non_executable_hook_is_stale(self, guarded):
        hook = guarded.prod / ".git" / "hooks" / "pre-push"
        hook.chmod(0o644)
        assert pc.inspect(guarded.prod)["hooks"]["pre-push"] == "stale"
        assert pc.ensure_guard(guarded.prod)["ok"] is True
        assert os.access(hook, os.X_OK)

    def test_hooks_path_override_is_red(self, guarded):
        guarded.git("config", "core.hooksPath", str(guarded.tmp / "elsewhere"))
        r = pc.inspect(guarded.prod)
        assert r["ok"] is False and r["hooks_path_override"], r

    def test_wrong_origin_is_red(self, guarded, monkeypatch):
        monkeypatch.setattr(pc, "DEFAULT_ORIGIN", "git@github.com:someone/else.git")
        r = pc.inspect(guarded.prod)
        assert r["ok"] is False and r["origin_ok"] is False, r

    def test_edited_tracked_file_is_red_untracked_is_not(self, guarded):
        (guarded.prod / "scratch.txt").write_text("untracked\n")
        assert pc.inspect(guarded.prod)["ok"] is True
        (guarded.prod / "code.py").write_text("hand edit\n")
        r = pc.inspect(guarded.prod)
        assert r["ok"] is False and r["dirty_tracked"] == ["code.py"], r

    def test_staged_rename_lists_new_name_without_mangling_the_old(self, guarded):
        guarded.git("mv", "notes.md", "renamed.md")
        r = pc.inspect(guarded.prod)
        assert r["dirty_tracked"] == ["renamed.md"], r

    def test_not_a_repo(self, tmp_path):
        r = pc.inspect(tmp_path)
        assert r["ok"] is False and r["problems"][0].startswith("not_a_repo"), r

    def test_ensure_never_raises(self, guarded, monkeypatch):
        def boom(*a, **k):
            raise PermissionError("hooks 目录不可写")
        monkeypatch.setattr(pc, "_write_hook", boom)
        (guarded.prod / ".git" / "hooks" / "pre-commit").unlink()
        g = pc.ensure_guard(guarded.prod)
        assert g["ok"] is False and any("PermissionError" in p for p in g["problems"]), g


class TestHookTextsLint:
    def test_no_unbraced_variable_before_non_ascii(self):
        """`$new：` 在部分 locale 下被 /bin/sh 读成变量名 `new\\xEF` ⇒ 展开为空、半个汉字漏进 stderr（本版首跑实测，
        与 v0.45.284 编排器 Step 15 同一个坑）。钩子正文全是中文提示，必须一律写 `${var}`。"""
        from orchestrator_lint import find_unbraced
        hits = {name: find_unbraced(text) for name, text in pc.HOOKS.items()}
        assert not any(hits.values()), hits

    def test_lint_has_teeth_on_hook_shaped_text(self):
        from orchestrator_lint import find_unbraced
        assert find_unbraced('echo "移到 $new：它不在"\n'), "正对照：未加括号的写法必须被抓到"


# ═════════════════════════════ setup（cutover 入口） ═════════════════════════════

class TestSetup:
    def test_clones_and_guards(self, world):
        dest = world.tmp / "alpha-hive-prod"
        res = pc.setup(dest, str(world.origin))
        assert res["outcome"] == "ok" and res["cloned"] is True, res
        assert _run("rev-parse", "--verify", "refs/remotes/origin/gh-pages", cwd=dest, check=False).returncode == 0, \
            "完整克隆才有 origin/gh-pages（gh-pages 部署要它）"
        again = pc.setup(dest, str(world.origin))
        assert again["outcome"] == "ok" and again["cloned"] is False and again["guard"]["actions"] == {}, again

    def test_refuses_existing_non_repo(self, world):
        dest = world.tmp / "occupied"
        dest.mkdir()
        (dest / "keep.txt").write_text("x")
        res = pc.setup(dest, str(world.origin))
        assert res["outcome"] == "refused" and (dest / "keep.txt").read_text() == "x", res

    def test_clone_failure_is_reported(self, world):
        res = pc.setup(world.tmp / "nope", str(world.tmp / "missing.git"))
        assert res["outcome"] == "clone_failed" and res["detail"], res
        assert pc.main(["setup", "--dest", str(world.tmp / "nope2"), "--origin", str(world.tmp / "missing.git")]) == 2

    def test_cli_check_exit_codes(self, guarded, capsys):
        assert pc.main(["check", "--repo", str(guarded.prod)]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        (guarded.prod / "code.py").write_text("hand edit\n")
        assert pc.main(["check", "--repo", str(guarded.prod)]) == 1
        assert json.loads(capsys.readouterr().out)["dirty_tracked"] == ["code.py"]

    def test_default_dest_is_evaluated_at_call_time(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert pc.default_dest() == tmp_path / "alpha-hive-prod"


# ═════════════════════════════ 5. 结局走到告警 ═════════════════════════════

class TestProductionSyncIntegration:
    @staticmethod
    def _alerts(tmp_path, snap):
        from alert_manager import AlertAnalyzer
        status = {"status": "success", "total_duration_seconds": 1,
                  "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": snap}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status, ensure_ascii=False))
        a = AlertAnalyzer(report_dir=tmp_path)
        return [x.message for x in a.analyze(p)]

    def test_without_the_env_var_nothing_is_installed(self, world, monkeypatch):
        """切换前 / 回退后（编排器不声明）：production_sync 行为与 v0.45.430 完全相同。"""
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(world.prod))
        monkeypatch.delenv(pc.PRODUCTION_CLONE_ENV, raising=False)
        assert ps.main(["--date", "2026-10-08"]) == 0
        res = ps.load_for_date("2026-10-08")
        assert "clone_guard" not in res, res
        assert not any((world.prod / ".git" / "hooks" / n).exists() for n in pc.HOOKS)

    def test_env_var_installs_guard_before_the_fast_forward(self, world, monkeypatch, tmp_path):
        w = world
        new = w.session_push("code.py", "v2\n", "session: v2")
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(w.prod))
        monkeypatch.setenv(pc.PRODUCTION_CLONE_ENV, "1")
        assert ps.main(["--date", "2026-10-08"]) == 0
        res = ps.load_for_date("2026-10-08")
        assert res["outcome"] == "fast_forwarded" and w.head() == new, res
        assert res["clone_guard"]["ok"] is True and set(res["clone_guard"]["actions"]) == set(pc.HOOKS), res
        snap = st.snapshot("2026-10-08", extra={"gh_pages": {"success": True}})
        assert snap["production_sync"]["clone_guard"]["ok"] is True
        msgs = self._alerts(tmp_path, snap)
        assert not any("生产克隆不合格" in m for m in msgs), msgs

    def test_bad_clone_is_red_but_sync_still_runs(self, world, monkeypatch, tmp_path):
        """守卫不合格不拦同步、不拦扫描——只红。"""
        w = world
        new = w.session_push("code.py", "v2\n", "session: v2")
        (w.prod / "notes.md").write_text("有人在生产克隆里手改\n")
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(w.prod))
        monkeypatch.setenv(pc.PRODUCTION_CLONE_ENV, "1")
        assert ps.main(["--date", "2026-10-08"]) == 0
        res = ps.load_for_date("2026-10-08")
        assert res["outcome"] == "fast_forwarded" and w.head() == new, res
        assert res["clone_guard"]["ok"] is False
        msgs = self._alerts(tmp_path, st.snapshot("2026-10-08", extra={"gh_pages": {"success": True}}))
        assert any("生产克隆不合格" in m for m in msgs), msgs

    def test_env_var_on_a_development_repo_is_red_and_writes_no_hooks(self, world, monkeypatch, tmp_path):
        """有人把编排器指回开发仓库却留着声明：不装钩子（不拦开发），但天天红到改对为止。"""
        w = world
        w.git("worktree", "add", "-q", str(w.tmp / "wt"), "-b", "dev")
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(w.prod))
        monkeypatch.setenv(pc.PRODUCTION_CLONE_ENV, "1")
        ps.main(["--date", "2026-10-08"])
        res = ps.load_for_date("2026-10-08")
        assert res["clone_guard"]["standalone"] is False and res["clone_guard"]["ok"] is False
        assert not any((w.prod / ".git" / "hooks" / n).exists() for n in pc.HOOKS)
        msgs = self._alerts(tmp_path, st.snapshot("2026-10-08", extra={"gh_pages": {"success": True}}))
        assert any("生产克隆不合格" in m for m in msgs), msgs

    def test_guard_failure_does_not_break_sync(self, world, monkeypatch):
        w = world
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(w.prod))
        monkeypatch.setenv(pc.PRODUCTION_CLONE_ENV, "1")
        monkeypatch.setattr(pc, "inspect", lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘炸了")))
        assert ps.main(["--date", "2026-10-08"]) == 0
        res = ps.load_for_date("2026-10-08")
        assert res["outcome"] == "up_to_date"
        assert res["clone_guard"]["ok"] is False and "OSError" in res["clone_guard"]["problems"][0], res
