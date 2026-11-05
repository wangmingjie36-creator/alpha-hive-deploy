"""Alpha Bot 正在跑哪份代码、磁盘上的代码更新了没有（v0.45.726）

事故：2026-11-04 Alpha Bot 09:16 启动后一直跑旧代码；09:51 合入的 v0.45.713（启动时探代理）18:01 已进生产克隆，
进程没重启 ⇒ NVDL 备用源当天 19 次全被 Yahoo 限流。页脚的 0.45.411 是 .app 版本号、不跟代码走，没有任何提示要重启。

本文件锁住：
  ① 启动时记下 HEAD；磁盘上的 HEAD 变了 ⇒ `stale` + 新了几个提交（真 git 临时仓库）；
  ② 判不了（不是检出 / 找不到 git / 超时 / 输出不认识 / 之后读不到 / 数不出个数）⇒ 如实报，`stale` 不冒充 False；
  ③ 只读 git（子命令白名单），结果有缓存；
  ④ `create_app` 建服务那一刻记下、之后每次 `/api/meta` 只比对（不在请求里重新「记下」——那样永远不 stale）；
  ⑤ 前端只读后端真有的键、横幅只在 `stale === true` 时挂，帮助页说的与横幅同一个词。
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from alphabot import code_version as cvm
from alphabot.code_version import CodeVersion

ROOT = Path(__file__).resolve().parent.parent
_GIT_ENV_FLAGS = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def _git(repo, *args):
    return subprocess.run(["git", *_GIT_ENV_FLAGS, "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, msg):
    _git(repo, "commit", "--allow-empty", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "code"
    r.mkdir()
    _git(r, "init", "-q")
    return r


class _Recorder:
    """包一层真 `subprocess.run`：记下每次 git 子命令。"""

    def __init__(self):
        self.subcommands = []

    def __call__(self, cmd, **kw):
        assert cmd[:3] == ["git", "-C", cmd[2]], cmd
        self.subcommands.append(cmd[3])
        return subprocess.run(cmd, **kw)


# ═══════════════════════════════ ① 真 git ═══════════════════════════════

class TestRealGit:
    def test_unchanged_checkout_is_not_stale(self, repo):
        a = _commit(repo, "v0.45.1 第一版")
        st = CodeVersion(repo, ttl=0).status()
        assert st["available"] is True and st["stale"] is False
        assert st["running_full"] == a and st["running"] == a[:7] and st["on_disk"] == a[:7]
        assert st["running_subject"] == "v0.45.1 第一版" and st["behind"] is None and st["reason"] is None

    def test_fast_forward_after_start_is_stale_with_count(self, repo):
        """生产克隆在扫描日被快进的形状：启动后又来了 2 个提交 ⇒ stale、新了 2 个、带磁盘上最新那次的说明。
        变红的变异：比较写反 / 不数个数 / 每次请求重新记下启动 HEAD。"""
        a = _commit(repo, "旧")
        cv = CodeVersion(repo, ttl=0)
        _commit(repo, "中")
        c = _commit(repo, "v0.45.726 新")
        st = cv.status()
        assert st["stale"] is True and st["behind"] == 2
        assert st["running_full"] == a and st["on_disk"] == c[:7] and st["on_disk_subject"] == "v0.45.726 新"

    def test_head_moved_to_unrelated_history_is_still_stale(self, repo):
        """切到一段不含启动提交的历史（开发检出里换分支）：只比提交号、不要求是快进 ⇒ 仍 stale。"""
        _commit(repo, "a")
        cv = CodeVersion(repo, ttl=0)
        _git(repo, "checkout", "-q", "--orphan", "other")
        _commit(repo, "孤立分支")
        st = cv.status()
        assert st["stale"] is True and st["available"] is True
        assert st["behind"] == 1   # 新分支上有 1 个启动提交里没有的提交

    def test_head_moved_back_is_stale_with_zero_new(self, repo):
        """退回到更早的提交：磁盘上「新了 0 个」，但代码确实变了 ⇒ stale；前端对 0 写「与正在运行的不同」。"""
        a = _commit(repo, "a")
        _commit(repo, "b")
        cv = CodeVersion(repo, ttl=0)
        _git(repo, "reset", "-q", "--hard", a)
        st = cv.status()
        assert st["stale"] is True and st["behind"] == 0 and st["on_disk"] == a[:7]

    def test_reads_only(self, repo):
        _commit(repo, "a")
        rec = _Recorder()
        cv = CodeVersion(repo, run=rec, ttl=0)
        _commit(repo, "b")
        cv.status()
        assert set(rec.subcommands) <= {"rev-parse", "log", "rev-list"}, rec.subcommands
        assert "rev-list" in rec.subcommands, "探针没走到数个数那一步（这条会对空集恒绿）"


# ═══════════════════════════════ ② 判不了 ═══════════════════════════════

class _FakeRun:
    """假 git：`script` 按子命令给 (returncode, stdout, stderr) 或要抛的异常；记录调用。"""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, cmd, **kw):
        sub = cmd[3]
        self.calls.append(sub)
        r = self.script[sub]
        r = r() if callable(r) else r
        if isinstance(r, BaseException):
            raise r
        rc, out, err = r
        return subprocess.CompletedProcess(cmd, rc, out, err)


SHA_A, SHA_B = "a" * 40, "b" * 40


class TestCannotJudge:
    def test_not_a_checkout(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="alpha_hive.alphabot.code_version"):
            st = CodeVersion(tmp_path, ttl=0).status()
        assert st["available"] is False and st["stale"] is None and st["running"] is None
        assert "启动时没读到" in st["reason"] and "rev-parse" in st["reason"]
        assert any("读不到代码版本" in r.getMessage() for r in caplog.records), "启动时读不到要进日志"

    @pytest.mark.parametrize("exc, needle", [
        (FileNotFoundError("git"), "找不到 git"),
        (subprocess.TimeoutExpired(["git"], 5), "超时"),
        (PermissionError("沙箱"), "跑不起来"),
    ])
    def test_git_unavailable(self, exc, needle):
        st = CodeVersion(Path("/x"), run=_FakeRun({"rev-parse": exc}), ttl=0).status()
        assert st["available"] is False and st["stale"] is None and needle in st["reason"]

    def test_garbage_output_is_not_a_commit(self):
        run = _FakeRun({"rev-parse": (0, "ref: refs/heads/main\n", ""), "log": (0, "x", "")})
        st = CodeVersion(Path("/x"), run=run, ttl=0).status()
        assert st["available"] is False and "不是提交号" in st["reason"]

    def test_unreadable_later_is_not_reported_as_unchanged(self):
        heads = iter([(0, SHA_A + "\n", ""), (128, "", "fatal: not a git repository")])
        run = _FakeRun({"rev-parse": lambda: next(heads), "log": (0, "s", "")})
        st = CodeVersion(Path("/x"), run=run, ttl=0).status()
        assert st["running"] == SHA_A[:7] and st["available"] is False and st["stale"] is None
        assert "现在读不到" in st["reason"] and "not a git repository" in st["reason"]

    def test_count_failure_keeps_stale_and_says_why(self):
        heads = iter([(0, SHA_A, ""), (0, SHA_B, "")])
        run = _FakeRun({"rev-parse": lambda: next(heads), "log": (0, "新", ""),
                        "rev-list": (128, "", "fatal: bad revision")})
        st = CodeVersion(Path("/x"), run=run, ttl=0).status()
        assert st["stale"] is True and st["behind"] is None and "数不出" in st["reason"]


# ═══════════════════════════════ ③ 缓存 ═══════════════════════════════

class TestCache:
    def test_within_ttl_no_git_call_after_ttl_rechecks(self):
        t = [100.0]
        run = _FakeRun({"rev-parse": (0, SHA_A, ""), "log": (0, "s", "")})
        cv = CodeVersion(Path("/x"), run=run, now=lambda: t[0], ttl=30.0)
        n0 = len(run.calls)
        cv.status()
        cv.status()
        t[0] += 29.0
        cv.status()
        assert run.calls[n0:] == ["rev-parse"], run.calls
        t[0] += 2.0
        cv.status()
        assert run.calls[n0:] == ["rev-parse", "rev-parse"]

    def test_returned_dict_is_a_copy(self):
        """命中缓存时返回的也是副本（调用方改了它不污染缓存）。⚠️ 要改的是**命中路径**返回的那份：
        第一次调用走未命中、本来就是新算的 dict，改它测不到缓存（首版就这么写，变异「命中返回本体」跑绿）。"""
        cv = CodeVersion(Path("/x"), run=_FakeRun({"rev-parse": (0, SHA_A, ""), "log": (0, "s", "")}),
                         now=lambda: 100.0, ttl=30.0)
        cv.status()                              # 未命中：算一次、进缓存
        cv.status()["stale"] = "被调用方改了"     # 命中
        assert cv.status()["stale"] is False


# ═══════════════════════════════ ④ 服务接线 ═══════════════════════════════

def _client(**kw):
    from starlette.testclient import TestClient

    from alphabot import synthetic as syn
    from alphabot.server import create_app
    from alphabot.service import AlphaBotService
    svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, state_dir=tempfile.mkdtemp(prefix="alphabot-cv-"))
    return TestClient(create_app(svc, **kw), base_url="http://127.0.0.1:8765")


class TestServerWiring:
    def test_snapshot_taken_once_at_app_creation(self, monkeypatch):
        """建服务时记一次；之后每次 /api/meta 只问 status()。变异：在 meta 里新建 CodeVersion（拿现在比现在，永不 stale）。"""
        import alphabot.server as server
        made, asked = [], []

        class _Fake:
            def __init__(self, *a, **k):
                made.append(1)

            def status(self):
                asked.append(1)
                return {"stale": True, "running": "abc1234"}

        monkeypatch.setattr(server, "CodeVersion", _Fake)
        c = _client()
        for _ in range(3):
            assert c.get("/api/meta").json()["code"] == {"stale": True, "running": "abc1234"}
        assert made == [1] and len(asked) == 3

    def test_default_reports_this_checkout(self):
        """不注入时读本检出（开发检出 / worktree / CI 都是 git 检出）：运行中的提交 = 此刻 HEAD。"""
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
        code = _client().get("/api/meta").json()["code"]
        assert code["available"] is True and code["running_full"] == head and code["stale"] is False
        assert Path(code["repo"]) == ROOT == cvm.code_dir()


# ═══════════════════════════════ ⑤ 前端 ═══════════════════════════════

class TestFrontend:
    JS = (ROOT / "alphabot/static/app.js")

    def test_page_reads_only_keys_the_backend_has(self, repo):
        """`cv.<键>` 在 app.js 里出现的每个键都得是后端真给的（改名两边一起改）；并且确实读到了 stale / behind。"""
        _commit(repo, "a")
        keys = set(CodeVersion(repo, ttl=0).status())
        used = set(re.findall(r"\bcv\.([a-z_]+)", self.JS.read_text(encoding="utf-8")))
        assert {"stale", "behind", "running", "on_disk", "reason", "available"} <= used, used
        assert used <= keys, used - keys

    def test_banner_only_on_strict_true_and_mount_point_exists(self):
        js = self.JS.read_text(encoding="utf-8")
        html = (ROOT / "alphabot/static/index.html").read_text(encoding="utf-8")
        assert 'id="code-banner"' in html
        assert "cv.stale === true" in js, "判不了时 stale 是 null，不许被当成要挂横幅 / 不挂横幅以外的东西"

    def test_help_uses_the_same_words_as_the_banner(self):
        js = self.JS.read_text(encoding="utf-8")
        help_js = (ROOT / "alphabot/static/pages/help.js").read_text(encoding="utf-8")
        assert "代码已更新" in js and "「代码已更新」横幅" in help_js
