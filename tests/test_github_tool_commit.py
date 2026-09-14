"""
`GitHubTool.commit()` 的失败原因（v0.45.225）

v0.45.223 给「日报提交失败」加了 P1 告警，「原因」一栏读的就是 `commit()` 回的 `error`。
二次检查实测：它瞄准的那个场景——生产 checkout 残留 `.git/index.lock`——告警写的是
「白名单未匹配到任何文件」，真实原因 `fatal: Unable to create '…/index.lock': File exists.`
被丢了，而同一条告警的「建议」栏又叫人去查 index.lock，两栏自相矛盾。

机制：白名单逐条 `git add`，任何一条失败都被当成「这个产物本次没生成」容忍，
一条都没暂存上时统一回那句话。**git 在匹配 pathspec 之前就去拿索引锁** ⇒
锁在时每一条都是锁错误，一条 pathspec 未匹配都没有（下面的正对照直接断言这一点）。

判据：该容忍的只有那一种预期失败（pathspec 未匹配），别的失败不许借它的说法报出去。

全部在真 git 仓库里造，不打桩。调用方一侧（原因传进 status.json 与告警）的守卫在
`tests/test_production_sync.py::TestResultReachesAlerts::test_failed_report_commit_alerts_even_when_push_says_success`。

v0.45.227 追加 `TestBriefLockDuringOneAdd`：v0.45.225 在 docstring 里断言「部分 add 失败」生产里
触发不了（「锁是整库级的」）——**推理，没测**。锁**一直在**时确实全挂、提交也挂、告警会响；
锁只被别的进程**占一下**（普通 `git status` 写回索引时持锁，生产克隆实测 55–72ms；当时就有一个 Claude
session 的 cwd 在生产 checkout）时只挂那一条，提交照样成功 ⇒ 当天 `index.html` 没进 git，零告警。
那组测试里锁文件是真的、git 的失败是真的，只有「别的进程恰好在那一刻拿着锁」这个时机是造的。
"""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_toolbox  # noqa: E402
import report_deployer as rd  # noqa: E402
from agent_toolbox import GitHubTool  # noqa: E402

# 收集期取一次：下面的 autouse 夹具会把它改成 0，默认值要在改之前拿到
_DEFAULT_RETRY_DELAY = getattr(GitHubTool, "_ADD_RETRY_DELAY_S", None)


@pytest.fixture(autouse=True)
def _no_inherited_git_dir(monkeypatch):
    # 若 pytest 从 git hook 里被拉起，继承的 GIT_DIR 会让每条 git 命令打到真仓库上
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    # 残留锁的用例每条都会等一次重试；默认间隔由 test_retry_waits_long_enough_* 单独核
    monkeypatch.setattr(GitHubTool, "_ADD_RETRY_DELAY_S", 0, raising=False)


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "index.html").write_text("base")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


class TestCommitFailureKeepsGitsReason:

    def test_stale_index_lock_is_the_reason_not_the_whitelist(self, repo):
        (repo / "index.html").write_text("report-0914")
        (repo / ".git" / "index.lock").write_text("")
        before = _head(repo)

        # 正对照：锁在时，连「不存在的 pathspec」报的也是锁错误 —— 旧实现正是把这些
        # 全当成「本次没生成」容忍掉的。这条不成立，本测试就测不到那个 bug。
        miss = _git(repo, "add", "--", "alpha-hive-daily-nope.json", check=False)
        assert miss.returncode != 0 and "index.lock" in miss.stderr, miss
        assert "did not match any files" not in miss.stderr, miss

        r = GitHubTool(repo_path=str(repo)).commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)

        assert r["success"] is False
        assert "index.lock" in r["error"] and "File exists" in r["error"], r
        assert "白名单未匹配" not in r["error"], f"真实原因被「没匹配到文件」盖掉了：{r}"
        # 每条 pathspec 报的是同一行；原因要能放进 status.json 的 300 字截断里，别重复 N 遍，
        # 也别带上 git 附在锁错误后面的五行通用建议
        assert r["error"].count("Unable to create") == 1, r
        assert "\n" not in r["error"] and "Another git process" not in r["error"], r
        assert _head(repo) == before

    def test_nothing_matched_is_still_reported_as_nothing_matched(self, repo):
        """正对照：没有任何产物时，「pathspec 未匹配」仍是被容忍的预期失败，说法不变。
        把 pathspec 未匹配也当成真实错误（判别器写反 / 写成恒真），这条红。"""
        r = GitHubTool(repo_path=str(repo)).commit(
            "日报", paths=["alpha-hive-daily-nope.json", "reports/*.md"])
        assert r == {"success": False, "error": "白名单未匹配到任何文件"}

    def test_stage_all_failure_has_the_same_contract(self, repo):
        """不传 `paths` 的 `git add -A` 分支（生产无调用方）：失败时原先回
        `{"error": "Failed to stage: …"}`，**没有 `success` 键** ⇒ 照 `report_deployer`
        的写法读 `result["success"]` 是 KeyError；且子进程异常形状只有 `error` 键，
        读 `stage['stderr']` 同样 KeyError。"""
        (repo / "index.html").write_text("x")
        (repo / ".git" / "index.lock").write_text("")
        r = GitHubTool(repo_path=str(repo)).commit("全量")
        assert r.get("success") is False, r
        assert "index.lock" in r["error"], r

    def test_stage_all_error_shaped_failure_keeps_its_reason(self, repo, monkeypatch):
        g = GitHubTool(repo_path=str(repo))
        monkeypatch.setattr(g, "run_git_cmd",
                            lambda cmd: {"success": False, "error": "git add timed out after 30 seconds"})
        r = g.commit("全量")
        assert r == {"success": False, "error": "git add 失败：git add timed out after 30 seconds"}


def hold_lock_during_add(monkeypatch, tool, repo, pathspec, times):
    """别的 git 进程在我们 `git add -- <pathspec>` 的那一刻正拿着索引锁，随后释放。

    锁文件是真的、git 的失败是真的；造的只是时机。`times` = 连续几次 add 这条 pathspec 时锁都在
    （1 = 重试时已释放；2 = 连重试也撞上）。返回一个计数器，记这条 pathspec 被 add 了几次。"""
    real = tool.run_git_cmd
    lock = repo / ".git" / "index.lock"
    seen = {"adds": 0}

    def run(cmd):
        if cmd != f"git add -- {pathspec}":
            return real(cmd)
        seen["adds"] += 1
        if seen["adds"] > times:
            return real(cmd)
        lock.write_text("")
        try:
            return real(cmd)
        finally:
            lock.unlink()

    monkeypatch.setattr(tool, "run_git_cmd", run)
    return seen


@pytest.fixture
def three_artifacts(repo):
    for name in ("rss.xml", "dashboard-data.json"):
        (repo / name).write_text("base")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "more artifacts")
    for name in ("index.html", "rss.xml", "dashboard-data.json"):
        (repo / name).write_text("report-0914")
    return repo


def _committed(repo):
    return set(_git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())


class TestBriefLockDuringOneAdd:

    def test_retry_lands_the_artifact_a_brief_lock_blocked(self, three_artifacts, monkeypatch, caplog):
        repo = three_artifacts
        g = GitHubTool(repo_path=str(repo))
        seen = hold_lock_during_add(monkeypatch, g, repo, "index.html", times=1)

        with caplog.at_level("WARNING"):
            r = g.commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)

        assert seen["adds"] == 2, f"正对照：index.html 应当先被锁挡一次、再重试一次（实际 {seen['adds']} 次）"
        assert r["success"] is True
        assert _committed(repo) == {"index.html", "rss.xml", "dashboard-data.json"}, (
            "锁只被占了一下，index.html 却没进提交 —— v0.45.225 及以前就是这样，且报成功")
        assert "add_errors" not in r, r
        assert any("重试" in m.getMessage() and "index.lock" in m.getMessage() for m in caplog.records), \
            "重试成功也要留痕：锁争用发生过这件事本身要看得见"

    def test_lock_that_outlasts_the_retry_is_reported_not_swallowed(self, three_artifacts, monkeypatch):
        repo = three_artifacts
        g = GitHubTool(repo_path=str(repo))
        hold_lock_during_add(monkeypatch, g, repo, "index.html", times=2)

        r = g.commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)

        assert r["success"] is True and _committed(repo) == {"rss.xml", "dashboard-data.json"}, \
            "正对照：其余产物照常提交（部分失败不该拖垮整次提交）"
        assert len(r.get("add_errors") or []) == 1 and "index.lock" in r["add_errors"][0], r

    def test_healthy_commit_does_not_wait(self, three_artifacts, monkeypatch):
        slept = []
        # 只换 agent_toolbox 手里的 time：全局 time.sleep 会被 subprocess 等子进程时调用
        monkeypatch.setattr(agent_toolbox, "time", SimpleNamespace(sleep=slept.append), raising=False)
        r = GitHubTool(repo_path=str(three_artifacts)).commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is True and slept == [], "只有 pathspec 未匹配时不许等（每天都有产物本次没生成）"

    def test_retry_waits_long_enough_for_a_git_status_to_finish(self, three_artifacts, monkeypatch):
        """默认间隔要长过别的进程占锁的时长：普通 `git status` 在生产克隆上实测持锁 55–72ms。"""
        assert _DEFAULT_RETRY_DELAY is not None and _DEFAULT_RETRY_DELAY >= 0.5, _DEFAULT_RETRY_DELAY
        monkeypatch.setattr(GitHubTool, "_ADD_RETRY_DELAY_S", _DEFAULT_RETRY_DELAY)
        slept = []
        # 只换 agent_toolbox 手里的 time：全局 time.sleep 会被 subprocess 等子进程时调用
        monkeypatch.setattr(agent_toolbox, "time", SimpleNamespace(sleep=slept.append), raising=False)
        g = GitHubTool(repo_path=str(three_artifacts))
        # 两条都撞锁：只挂一条时「每条等一次」与「共用一次」长得一样
        hold_lock_during_add(monkeypatch, g, three_artifacts, "index.html", times=1)
        hold_lock_during_add(monkeypatch, g, three_artifacts, "rss.xml", times=1)
        r = g.commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is True and "add_errors" not in r, f"正对照：两条都该被重试接住 {r}"
        assert slept == [_DEFAULT_RETRY_DELAY], "所有失败条目共用一次等待，不是每条等一次"
