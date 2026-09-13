"""
`GitHubTool.status()` 的解析与失败形状（v0.45.211）

它是在产方法：`report_deployer._git_modified_files` 读它，决定当天日报提交与否、
警告里列出哪些文件被跳过、非生产扫描报告哪些产物残留在工作区。
v0.45.204 实测删掉整个 `def status` **全套零红**——本文件之前没有任何测试碰它。

三组，各堵一个实测复现过的形状（全部在真 git 仓库里造，不打桩）：

  1. 解析：含空格 / 中文 / 引号 / 改名的改动，路径必须逐字回来。
     旧 `line.split()[-1]` 把 `?? "report 2.json"` 解析成 `2.json"`、
     `日报.md` 解析成 `"\\346\\227\\245\\346\\212\\245.md"`。
  2. 失败 ≠ 干净：`run_git_cmd` 的两种失败形状各造一次**真实**失败，结果必须
     `success is False`、`error` 非空、且**没有** `modified_files` 键。
     旧实现在子进程自己炸时返回 `{"error": None}`——连原因都丢了。
  3. 抛出路径：`-z` 按原始字节输出路径，非 UTF-8 的索引条目会让 `run_git_cmd`
     抛 `UnicodeDecodeError`。`status()` 必须收成失败返回，不许抛出去。

每组都先断言夹具确实走到了想测的那条分支（正对照），否则「全绿」不说明任何事。
调用方一侧（失败不许被报成「工作目录干净」）的守卫在
`tests/test_git_failures_are_visible.py::TestProductionFailuresCarryTheirReason`。
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_deployer as rd  # noqa: E402
from agent_toolbox import GitHubTool  # noqa: E402


@pytest.fixture(autouse=True)
def _no_inherited_git_dir(monkeypatch):
    # 若 pytest 从 git hook 里被拉起，继承的 GIT_DIR 会让每条 git 命令打到真仓库上
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)


def _git(repo, *args, **kw):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, **kw)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "index.html").write_text("base")
    (r / "old.txt").write_text("old")
    (r / "tracked name.md").write_text("v1")
    (r / "sub dir").mkdir()
    (r / "sub dir" / "a b.txt").write_text("v1")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


# ═════════════════════════════ 1. 解析 ═════════════════════════════

class TestPathsComeBackVerbatim:

    def test_clean_repo_is_success_with_empty_list(self, repo):
        r = GitHubTool(repo_path=str(repo)).status()
        assert r["success"] is True
        assert r["modified_files"] == []

    def test_quoted_paths_are_not_mangled(self, repo):
        (repo / "report 2.json").write_text("{}")          # iCloud 副本的名字形状
        (repo / "日报.md").write_text("x")                  # 非 ASCII → 旧输出八进制转义
        (repo / 'quo"te.txt').write_text("x")              # 引号 → 旧输出反斜杠转义
        (repo / "tracked name.md").write_text("v2")        # 已跟踪、改动、带空格
        (repo / "sub dir" / "a b.txt").write_text("v2")    # 目录与文件名都带空格

        # 正对照：这些名字在无 -z 的输出里确实被加了引号 —— 否则本条测不到旧 bug
        plain = _git(repo, "-c", "core.quotepath=true", "status", "--porcelain", text=True).stdout
        assert '"report 2.json"' in plain and "\\346" in plain, plain

        files = GitHubTool(repo_path=str(repo)).status()["modified_files"]
        assert sorted(files) == sorted([
            "report 2.json", "日报.md", 'quo"te.txt', "tracked name.md", "sub dir/a b.txt",
        ])

    def test_rename_source_is_not_a_separate_entry(self, repo):
        """`-z` 下改名条目是 `R  新\\0旧\\0`：原路径单独占一段，必须被跳过。"""
        _git(repo, "mv", "old.txt", "new.txt")
        raw = _git(repo, "status", "--porcelain", "-z").stdout
        assert raw.startswith(b"R  new.txt\0old.txt\0"), raw   # 正对照：真出现了改名条目

        files = GitHubTool(repo_path=str(repo)).status()["modified_files"]
        assert files == ["new.txt"], (
            f"{files}：改名的原路径被当成了独立条目（或新路径丢了）")

    def test_skip_warning_agrees_with_what_the_whitelist_commits(self, repo):
        """解析结果的真实消费者：`report_deployer` 用它列「跳过、不会自动提交」的文件。

        旧解析把 iCloud 副本 `alpha-hive-daily-… 2.json` 读成 `2.json"` ⇒ 判为非产物
        ⇒ 警告说它被跳过；而白名单 glob `alpha-hive-daily-*.json` 实际把它提交了。
        不变式：**警告里说跳过的 == 实际没被提交的**。

        ⚠️ 右边必须用本测试自己造的文件名当真值，不能用 `set(changed) - committed`：
        那样两边都出自被测解析器，旧解析的 `2.json"` 永远不在 committed 里 ⇒ 两边恒等。
        变异实测过：第一版就是那么写的，解析退回 `split()[-1]` 时本条照样绿。
        """
        artifact_dup = "alpha-hive-daily-2026-09-11 2.json"
        code_dup = "backtester 2.py"
        (repo / artifact_dup).write_text("{}")
        (repo / code_dup).write_text("# 半成品")
        (repo / "index.html").write_text("updated")
        touched = {artifact_dup, code_dup, "index.html"}

        g = GitHubTool(repo_path=str(repo))
        said_skipped = {f for f in g.status()["modified_files"] if not rd._is_report_artifact(f)}

        assert g.commit("日报测试", paths=rd.REPORT_ARTIFACT_PATHS)["success"]
        committed = set(_git(repo, "show", "--name-only", "-z", "--format=", "HEAD",
                             text=True).stdout.split("\0")) - {""}

        assert artifact_dup in committed, "正对照：白名单 glob 应当提交了带空格的产物副本"
        assert said_skipped == touched - committed, (
            f"警告说跳过 {sorted(said_skipped)}，实际没提交的是 {sorted(touched - committed)}")

    def test_parser_rejects_an_unrecognized_entry(self):
        """格式不认识时要抛（再由 `status()` 收成失败），不许吐出半截路径冒充结果。"""
        with pytest.raises(ValueError):
            GitHubTool._parse_porcelain_z("garbage\0")


# ═════════════════════════════ 2. 失败 ≠ 干净 ═════════════════════════════

def _assert_failure_not_clean(r, reason_fragment):
    assert r.get("success") is False, f"失败没被标成失败：{r}"
    assert isinstance(r.get("error"), str) and r["error"].strip(), \
        f"失败原因丢了（旧实现在子进程异常时返回 {{'error': None}}）：{r}"
    assert reason_fragment in r["error"], r
    assert "modified_files" not in r, (
        f"失败结果里带了 modified_files={r['modified_files']!r} —— 调用方靠这个键"
        "区分「失败」与「干净」，带上它（哪怕是空列表）就等于冒充干净")


class TestFailureIsNotClean:

    def test_git_nonzero_exit_shape(self, tmp_path, monkeypatch):
        """形状一：git 自己非零退出，原因在 stderr。"""
        not_repo = tmp_path / "not_a_repo"
        not_repo.mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # 不许往上找到别的仓库
        g = GitHubTool(repo_path=str(not_repo))

        raw = g.run_git_cmd("git status --porcelain -z")
        assert raw["success"] is False and raw.get("returncode") and "stderr" in raw, \
            f"正对照：夹具没走到「git 非零退出」形状：{raw}"

        _assert_failure_not_clean(g.status(), "not a git repository")

    def test_git_nonzero_exit_inside_a_real_repo(self, repo):
        """同一形状，但发生在真仓库里（索引损坏）—— 比「目录不是仓库」更接近生产能遇到的。"""
        (repo / ".git" / "index").write_bytes(b"garbage")
        _assert_failure_not_clean(GitHubTool(repo_path=str(repo)).status(), "index")

    def test_subprocess_exception_shape(self, tmp_path):
        """形状二：子进程自己炸（这里是 cwd 不存在 ⇒ OSError），只有 `error` 键。"""
        g = GitHubTool(repo_path=str(tmp_path / "does_not_exist"))

        raw = g.run_git_cmd("git status --porcelain -z")
        assert raw["success"] is False and "error" in raw and "stderr" not in raw, \
            f"正对照：夹具没走到「只有 error 键」形状：{raw}"

        _assert_failure_not_clean(g.status(), "No such file")


# ═════════════════════════════ 3. 抛出路径 ═════════════════════════════

class TestStatusDoesNotRaise:

    def test_non_utf8_index_entry_becomes_a_failure_not_an_exception(self, repo):
        # APFS 不许建非 UTF-8 文件名，但索引条目可以（别的系统提交进来的就是这样）
        blob = _git(repo, "hash-object", "-w", "--stdin", input=b"x").stdout.strip()
        _git(repo, b"update-index", b"--add", b"--cacheinfo", b"100644," + blob + b",bad\xffname.txt")

        # 正对照 ×2：输出里真有非 UTF-8 字节，且 run_git_cmd 真的会抛 ——
        # 若哪天 run_git_cmd 改成自己兜住，本条就不再测抛出路径，应当先红出来让人知道
        assert b"\xff" in _git(repo, "status", "--porcelain", "-z").stdout
        g = GitHubTool(repo_path=str(repo))
        with pytest.raises(UnicodeDecodeError):
            g.run_git_cmd("git status --porcelain -z")

        _assert_failure_not_clean(g.status(), "UnicodeDecodeError")
