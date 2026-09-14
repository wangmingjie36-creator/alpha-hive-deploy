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
