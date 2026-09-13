"""`changelog_guard.py` 的钩子行为（v0.45.216）

完整性测试本身的牙齿在 `tests/test_changelog_entry_integrity.py` 里。本文件只测**钩子的管道**：
什么时候跑、跑的是不是要守的那份内容、跑不了时怎么出声。

每个用例都在真 git 仓库里走真实命令，钩子由 `--install-hook` 装进去（不直接调函数）。
临时仓库里的 `tests/test_changelog_entry_integrity.py` 是个**桩**：遇到行首冲突标记就红，
并把「跑过一次、看见了哪些 git 定位变量」记到仓库外的 `stub_runs.txt`。

⚠️ 「提交 / 推送被拦」有好几种成因（内容坏了 / 内容与工作区不一致 / 解释器不在 / 脚本不在），
只断言「被拦了」等于把几件事说成一件（memory `alpha-hive-repair-discipline`「夹具把三种被挡
混成了一种」）。所以每条拦截用例都断言**具体原因**，并断言桩跑没跑。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import changelog_guard as cg  # noqa: E402

_STUB = '''
import os, pathlib
def test_stub():
    root = pathlib.Path(__file__).resolve().parent.parent
    leaked = sorted(k for k in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR")
                    if k in os.environ)
    with open(root.parent / "stub_runs.txt", "a") as f:
        f.write(f"{leaked}\\n")
    bad = [l for l in (root / "CHANGELOG.md").read_text().splitlines() if l.startswith("<<<<<<< ")]
    assert not bad, f"STUB-CONFLICT {bad}"
'''

CLEAN = "# log\n\n## [2] — x\n\nbody\n\n## [1] — y\n\nbody\n"
CONFLICTED = "# log\n\n<<<<<<< HEAD\n## [2] — x\n=======\n## [3] — z\n>>>>>>> other\n\nbody\n"


@pytest.fixture(scope="module")
def installed_hooks(tmp_path_factory):
    """真 `--install-hook` 装一次，各用例**硬链接**过去。

    不每例重装是因为 macOS 首次执行一个新写入的可执行文件要 ~1.5s（实测：新文件 1.54s、
    硬链接同一 inode 0.12s、复制 0.59s），每例重装让本文件从几秒涨到 ~20s。
    安装行为本身由 `test_install_is_idempotent_and_leaves_foreign_hooks_alone` 在新文件上验。
    ⚠️ 硬链接共享 inode ⇒ 用例里**不许原地写**这两个文件（要写先 unlink）。"""
    d = tmp_path_factory.mktemp("hook_template")
    env = {k: v for k, v in os.environ.items() if k not in cg._GIT_LOCATION_VARS}
    subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True, env=env)
    r = subprocess.run([sys.executable, str(_ROOT / "changelog_guard.py"), "--install-hook"],
                       cwd=d, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return d / ".git" / "hooks"


@pytest.fixture
def repo(tmp_path, monkeypatch, installed_hooks):
    # 若 pytest 从 git hook 里被拉起，继承的定位变量会让下面每条 git 命令打到真仓库上
    for var in cg._GIT_LOCATION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALPHA_HIVE_HOOK_PYTHON", sys.executable)
    # 只影响临时仓库里那个桩的 pytest（不改钩子的命令）：插件自动加载 0.5s → 0.24s，本文件跑十来次
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    origin, r = tmp_path / "origin.git", tmp_path / "repo"

    def git(*args, check=True, cwd=r, env=None):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           env={**os.environ, **(env or {})})
        if check and p.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{p.stdout}{p.stderr}")
        return p

    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(r), cwd=tmp_path)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    shutil.copy(_ROOT / "changelog_guard.py", r / "changelog_guard.py")
    (r / "tests").mkdir()
    (r / "tests" / "test_changelog_entry_integrity.py").write_text(_STUB)
    (r / "CHANGELOG.md").write_text(CLEAN)
    (r / "report.json").write_text("{}")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")

    for name in cg.HOOKS:
        os.link(installed_hooks / name, r / ".git" / "hooks" / name)
    runs_file = tmp_path / "stub_runs.txt"

    def stub_runs():
        return runs_file.read_text().splitlines() if runs_file.exists() else []

    def origin_main():
        return git("--git-dir", str(origin), "rev-parse", "main", cwd=tmp_path).stdout.strip()

    return type("Repo", (), dict(path=r, git=staticmethod(git), stub_runs=staticmethod(stub_runs),
                                 origin_main=staticmethod(origin_main), tmp=tmp_path, origin=origin))


def _other_session_pushes(repo, name, changelog_extra):
    """另一个 session（无钩子的独立克隆）往 origin/main 推一条 CHANGELOG 改动。"""
    other = repo.tmp / name
    if not other.exists():
        repo.git("clone", "-q", str(repo.origin), str(other), cwd=repo.tmp)
    else:
        repo.git("pull", "-q", "--ff-only", cwd=other)
    cl = other / "CHANGELOG.md"
    cl.write_text(cl.read_text() + changelog_extra)
    repo.git("-c", "user.email=o@o", "-c", "user.name=o", "commit", "-qam", f"{name} 推送", cwd=other)
    repo.git("push", "-q", "origin", "HEAD:main", cwd=other)
    return repo.git("rev-parse", "HEAD", cwd=other).stdout.strip()


def _write(repo, name, text):
    (repo.path / name).write_text(text)


# ═════════════════════════════ pre-commit ═════════════════════════════

class TestPreCommit:

    def test_conflicted_changelog_is_blocked_by_the_test(self, repo):
        head = repo.git("rev-parse", "HEAD").stdout
        _write(repo, "CHANGELOG.md", CONFLICTED)
        repo.git("add", "CHANGELOG.md")
        r = repo.git("commit", "-m", "带标记", check=False)
        assert r.returncode != 0, "带冲突标记的 CHANGELOG 被提交了"
        assert "STUB-CONFLICT" in r.stderr, f"被拦了，但不是被完整性测试拦的：{r.stderr}"
        assert repo.git("rev-parse", "HEAD").stdout == head
        assert len(repo.stub_runs()) == 1

    def test_clean_changelog_passes_and_git_location_vars_do_not_leak(self, repo):
        """正对照：同一流程、内容是好的 ⇒ 放行，且测试真的跑了、没继承 git 定位变量。"""
        _write(repo, "CHANGELOG.md", CLEAN + "\n## [0] — w\n\nbody\n")
        repo.git("add", "CHANGELOG.md")
        r = repo.git("commit", "-m", "好的", check=False)
        assert r.returncode == 0, r.stderr
        assert repo.stub_runs() == ["[]"], (
            f"桩看见的 git 定位变量：{repo.stub_runs()} —— 钩子里 git 会设 GIT_INDEX_FILE，"
            "原样传下去，测试对临时仓库的 git 命令会打到真仓库的索引上")

    def test_commit_not_touching_changelog_does_not_run_the_test(self, repo):
        """每日日报的白名单提交不动 CHANGELOG ⇒ 不跑、不拦。

        工作区里故意放一份**坏的**、未暂存的 CHANGELOG：钩子要是跑了测试，就一定会拦。"""
        _write(repo, "CHANGELOG.md", CONFLICTED)
        _write(repo, "report.json", '{"day": 2}')
        repo.git("add", "report.json")
        r = repo.git("commit", "-m", "日报", check=False)
        assert r.returncode == 0, r.stderr
        assert repo.stub_runs() == []

    def test_staged_version_differing_from_worktree_is_refused(self, repo):
        """最危险的一种：暂存的那份坏了、工作区那份是好的 —— 不拒就会测好的、放坏的。"""
        _write(repo, "CHANGELOG.md", CONFLICTED)
        repo.git("add", "CHANGELOG.md")
        _write(repo, "CHANGELOG.md", CLEAN)
        r = repo.git("commit", "-m", "部分暂存", check=False)
        assert r.returncode != 0, "暂存的是带标记的版本，却被放行了"
        assert "暂存版本与工作区不同" in r.stderr, r.stderr
        assert repo.stub_runs() == [], "应当在跑测试之前就拒绝"


# ═════════════════════════════ pre-push ═════════════════════════════

class TestPrePush:

    def test_rebase_continue_skips_pre_commit_but_the_push_is_blocked(self, repo):
        """复刻 v0.45.211：rebase 解 CHANGELOG 冲突时把标记留在文件里 → `rebase --continue`。"""
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.git("checkout", "-q", "-b", "feat")
        _write(repo, "CHANGELOG.md", "# log\n\n## [3] — z\n\nbody\n" + CLEAN[len("# log\n\n"):])
        repo.git("commit", "-qam", "feat 占号")
        repo.git("checkout", "-q", "main")
        _write(repo, "CHANGELOG.md", "# log\n\n## [4] — q\n\nbody\n" + CLEAN[len("# log\n\n"):])
        repo.git("commit", "-qam", "main 占号")
        repo.git("push", "-q", "origin", "main")
        pushed_main = repo.origin_main()
        assert pushed_main != base

        repo.git("checkout", "-q", "feat")
        assert repo.git("rebase", "main", check=False).returncode != 0, "前提：rebase 应当冲突"
        repo.git("add", "CHANGELOG.md")                      # 标记原样留着
        cont = repo.git("rebase", "--continue", check=False, env={"GIT_EDITOR": "true"})
        assert cont.returncode == 0, (
            "前提变了：rebase --continue 没能带着标记完成（git 若开始在这里跑 pre-commit，"
            f"本用例描述的缺口就不存在了，请更新 changelog_guard 文档）：{cont.stderr}")
        assert "<<<<<<< " in repo.git("show", "HEAD:CHANGELOG.md").stdout

        r = repo.git("push", "origin", "HEAD:main", check=False)
        assert r.returncode != 0, "带冲突标记的 CHANGELOG 被推上了 main"
        assert "STUB-CONFLICT" in r.stderr, f"被拦了，但不是被完整性测试拦的：{r.stderr}"
        assert repo.origin_main() == pushed_main

    def test_clean_push_to_main_passes(self, repo):
        """正对照。"""
        _write(repo, "CHANGELOG.md", CLEAN + "\n## [0] — w\n\nbody\n")
        repo.git("commit", "-qam", "好的")
        runs_before = len(repo.stub_runs())
        r = repo.git("push", "origin", "HEAD:main", check=False)
        assert r.returncode == 0, r.stderr
        assert repo.origin_main() == repo.git("rev-parse", "HEAD").stdout.strip()
        assert len(repo.stub_runs()) == runs_before + 1, "推送时没跑完整性测试"

    def test_push_not_touching_changelog_does_not_run_the_test(self, repo):
        """每日日报推送：区间不含 CHANGELOG ⇒ 不跑、不拦。工作区故意放一份坏的、未提交的。"""
        _write(repo, "report.json", '{"day": 2}')
        repo.git("commit", "-qam", "日报")
        _write(repo, "CHANGELOG.md", CONFLICTED)
        r = repo.git("push", "origin", "HEAD:main", check=False)
        assert r.returncode == 0, r.stderr
        assert repo.stub_runs() == []

    def test_push_to_another_branch_is_not_checked(self, repo):
        _write(repo, "CHANGELOG.md", CONFLICTED)
        repo.git("commit", "-qam", "草稿", "--no-verify")
        r = repo.git("push", "origin", "HEAD:refs/heads/wip", check=False)
        assert r.returncode == 0, r.stderr
        assert repo.stub_runs() == []

    def test_production_push_race_is_left_to_the_remote(self, repo):
        """复刻 v0.45.214 `production_sync.push_main` 的抢推竞态。

        生产 checkout：工作区停在扫描开始时 → fetch → 对象层合并（不动工作区）→ 推合并提交。
        fetch 与 push 之间别的 session 又推了 main ⇒ 远端 sha 本地不认识。
        若按「证明不了没动」处理，就会拿**旧工作区**比对合并提交里**较新**的 CHANGELOG，
        报「与工作区不同」—— 抢在远端之前报一个误导性的原因（`push_main` 靠 origin 又动了来重试）。
        应当放行给远端，由它按非快进拒绝。"""
        _write(repo, "report.json", '{"day": 2}')
        repo.git("commit", "-qam", "日报")
        head = repo.git("rev-parse", "HEAD").stdout.strip()
        o1 = _other_session_pushes(repo, "other", "\n## [7] — a\n\nbody\n")
        repo.git("fetch", "-q", "origin", "main")
        tree = repo.git("merge-tree", "--write-tree", o1, head).stdout.splitlines()[0]
        merge = repo.git("commit-tree", tree, "-p", o1, "-p", head, "-m", "对象层合并").stdout.strip()
        _other_session_pushes(repo, "other", "\n## [8] — b\n\nbody\n")    # 抢推

        assert repo.git("show", f"{merge}:CHANGELOG.md").stdout != (repo.path / "CHANGELOG.md").read_text(), \
            "前提：合并提交里的 CHANGELOG 应当比工作区新（否则测不到误报）"
        r = repo.git("push", "origin", f"{merge}:refs/heads/main", check=False)
        assert r.returncode != 0
        assert "与工作区不同" not in r.stderr, f"钩子抢在远端之前报了误导性的原因：{r.stderr}"
        assert "rejected" in r.stderr, f"应由远端按非快进拒绝：{r.stderr}"
        assert repo.stub_runs() == []

    def test_unknown_remote_sha_still_checks_a_range_that_touches_changelog(self, repo):
        """上一条的退路不许变成漏洞：远端 sha 不认识、但区间确实动了 CHANGELOG ⇒ 照样检查。"""
        _other_session_pushes(repo, "other", "\n## [7] — a\n\nbody\n")   # 远端前进，本地未 fetch
        _write(repo, "CHANGELOG.md", CONFLICTED)
        repo.git("commit", "-qam", "带标记", "--no-verify")
        r = repo.git("push", "origin", "HEAD:main", check=False)
        assert r.returncode != 0
        assert "STUB-CONFLICT" in r.stderr, f"没被完整性测试拦：{r.stderr}"

    def test_pushed_commit_differing_from_worktree_is_refused(self, repo):
        """被推的那份坏了、工作区那份是好的 —— 不拒就会测好的、放坏的。"""
        _write(repo, "CHANGELOG.md", CONFLICTED)
        repo.git("commit", "-qam", "带标记", "--no-verify")
        _write(repo, "CHANGELOG.md", CLEAN)
        before = repo.origin_main()
        r = repo.git("push", "origin", "HEAD:main", check=False)
        assert r.returncode != 0, "被推的提交带标记，却被放行了"
        assert "与工作区不同" in r.stderr, r.stderr
        assert repo.stub_runs() == []
        assert repo.origin_main() == before


# ═════════════════════════════ 薄调用与安装 ═════════════════════════════

class TestShimAndInstall:

    def test_missing_script_warns_but_does_not_block(self, repo):
        """停在引入本守卫之前的 checkout：`.git/hooks` 共用，拦了会误伤并发 session 与每日日报。"""
        (repo.path / "changelog_guard.py").unlink()
        _write(repo, "report.json", '{"day": 2}')
        quiet = repo.git("commit", "-m", "日报", "--", "report.json", check=False)
        assert quiet.returncode == 0 and "未被检查" not in quiet.stderr, quiet.stderr

        _write(repo, "CHANGELOG.md", CONFLICTED)
        loud = repo.git("commit", "-m", "动了 CHANGELOG", "--", "CHANGELOG.md", check=False)
        assert loud.returncode == 0, loud.stderr
        assert "未被检查" in loud.stderr, "动了 CHANGELOG 却没检查，也没出声"
        assert repo.stub_runs() == []

    def test_missing_interpreter_blocks_loudly(self, repo, monkeypatch):
        monkeypatch.setenv("ALPHA_HIVE_HOOK_PYTHON", str(repo.tmp / "no-such-python"))
        _write(repo, "CHANGELOG.md", CLEAN + "\n## [0] — w\n\nbody\n")
        repo.git("add", "CHANGELOG.md")
        r = repo.git("commit", "-m", "x", check=False)
        assert r.returncode != 0
        assert "解释器不可用" in r.stderr and "未被检查" in r.stderr, r.stderr

    def test_install_is_idempotent_and_leaves_foreign_hooks_alone(self, repo):
        hooks = repo.path / ".git" / "hooks"
        for name in cg.HOOKS:
            (hooks / name).unlink()   # 断开夹具的硬链接：下面要原地写这两个文件

        def install():
            return subprocess.run([sys.executable, "changelog_guard.py", "--install-hook"],
                                  cwd=repo.path, capture_output=True, text=True)

        first = install()
        assert first.returncode == 0 and first.stdout.count("已写入") == 2, first.stdout
        for name, body in cg.HOOKS.items():
            assert (hooks / name).read_text() == body
            assert os.access(hooks / name, os.X_OK)

        again = install()
        assert again.returncode == 0 and again.stdout.count("已是最新") == 2, again.stdout

        foreign = "#!/bin/sh\necho 别人的钩子\n"
        (hooks / "pre-push").write_text(foreign)
        refused = install()
        assert refused.returncode != 0 and "未覆盖" in refused.stderr, refused.stderr
        assert (hooks / "pre-push").read_text() == foreign
