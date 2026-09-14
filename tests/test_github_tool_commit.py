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


def _staged(repo):
    return set(_git(repo, "diff", "--cached", "--name-only").stdout.split())


class TestOnlyTheWhitelistIsCommitted:
    """v0.45.236：白名单只管「我们暂存什么」，而 `git commit -m` 提交的是**整个索引**。

    生产 checkout 是多个 session 共用的（2026-09-14 05:20–05:56 别的 session 在里面 pull / commit 了 6 次）：
    谁在里面 `git add` 了代码还没提交，日报部署就把它当成「日报」提交、推上 main，`left_artifacts=0`、零告警
    ——2026-07-30 事故同形（当时是 `add -A`，这次是共享索引）。修法：只提交白名单内已暂存的**确切文件名**。
    """

    @pytest.fixture
    def foreign_staged(self, repo):
        (repo / "code.py").write_text("v1")
        (repo / "CHANGELOG.md").write_text("# log")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "code")
        (repo / "code.py").write_text("v2 另一个 session 的半成品")
        (repo / "CHANGELOG.md").write_text("# 另一个 session 在改")
        _git(repo, "add", "code.py", "CHANGELOG.md")
        return repo

    def test_code_another_session_staged_stays_out_of_the_report_commit(self, foreign_staged):
        repo = foreign_staged
        (repo / "index.html").write_text("report-0914")
        r = GitHubTool(repo_path=str(repo)).commit("Alpha Hive 蜂群日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is True
        assert _committed(repo) == {"index.html"}, "别的 session 暂存的代码被卷进了日报提交"
        assert _staged(repo) == {"code.py", "CHANGELOG.md"}, "别人的暂存不许被动：留给它自己提交"

    def test_no_report_change_commits_nothing_even_if_the_index_is_not_empty(self, foreign_staged):
        """白名单内没有改动 ⇒ 不许跑裸 `git commit`：旧代码此时把别人暂存的代码提交成「日报」且回 success=True。"""
        repo = foreign_staged
        before = _head(repo)
        r = GitHubTool(repo_path=str(repo)).commit("Alpha Hive 蜂群日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is False and _head(repo) == before, r
        assert _staged(repo) == {"code.py", "CHANGELOG.md"}

    def test_a_snapshot_git_would_pair_as_a_rename_is_committed_whole(self, repo):
        """`--name-only` 默认做改名检测，只列新名字 ⇒ 旧文件的删除留在索引里没提交。内容相近的快照
        （同一标的前后两天）正是会被配成改名的形状。"""
        snap = repo / "report_snapshots"
        snap.mkdir()
        (snap / "XOM_2026-09-10.json").write_text('{"t": "XOM", "v": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "snap")
        (snap / "XOM_2026-09-10.json").rename(snap / "XOM_2026-09-11.json")
        # 正对照：git 真的会把这对配成改名
        _git(repo, "add", "--", "report_snapshots/")
        assert _git(repo, "diff", "--cached", "--name-only", "--", "report_snapshots/").stdout.split() == \
            ["report_snapshots/XOM_2026-09-11.json"]

        r = GitHubTool(repo_path=str(repo)).commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is True
        assert _staged(repo) == set(), f"改名的删除一侧没进提交，还留在索引里：{_staged(repo)}"
        assert _git(repo, "status", "--porcelain").stdout == ""

    def test_directory_pathspec_that_matches_nothing_git_knows_does_not_break_the_commit(self, repo):
        """`git add -- vrp_state/`（目录里只有被忽略的文件）回 0，但 `git commit -- vrp_state/` 报
        「did not match any file(s) known to git」整个提交失败 ⇒ 提交用的必须是确切文件名，不是 pathspec。"""
        (repo / ".gitignore").write_text("vrp_state/*.tmp\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        (repo / "vrp_state").mkdir()
        (repo / "vrp_state" / "x.tmp").write_text("t")
        assert _git(repo, "add", "--", "vrp_state/").returncode == 0, "正对照：这条 add 确实回 0"
        (repo / "index.html").write_text("report-0914")

        r = GitHubTool(repo_path=str(repo)).commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is True and _committed(repo) == {"index.html"}, r

    def test_staged_listing_failure_is_reported_not_committed_around(self, repo, monkeypatch):
        (repo / "index.html").write_text("report-0914")
        g = GitHubTool(repo_path=str(repo))
        real = g.run_git_cmd
        monkeypatch.setattr(g, "run_git_cmd", lambda cmd: (
            {"success": False, "error": "git diff timed out after 30 seconds"}
            if cmd.startswith("git diff --cached") else real(cmd)))
        before = _head(repo)
        r = g.commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is False and "timed out" in r.get("error", ""), r
        assert _head(repo) == before, "列不出白名单内暂存了什么，就不许提交（否则只能退回提交整个索引）"


class TestForeignRenamesAcrossTheWhitelistBoundaryAreNotSplit:
    """v0.45.248：`_staged_names` 的 `--no-renames` + 白名单 pathspec 过滤有个副作用——别的 session
    已暂存一个 rename、旧路径在白名单目录内、新路径不在时，pathspec 只放行旧路径（孤立的 `D`）。
    照单全收会把这半个 rename 当「日报」提交掉：旧路径的删除进了跟对方无关的提交，新路径仍留着
    孤零零地暂存着——对方的原子操作被拦腰斩断。"""

    @pytest.fixture
    def foreign_rename_out(self, repo):
        (repo / "hedge_state").mkdir()
        (repo / "hedge_state" / "positions.json").write_text('{"v": 1}')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "hedge state")
        (repo / "data_root").mkdir()
        _git(repo, "mv", "hedge_state/positions.json", "data_root/hedge_positions.json")
        return repo

    def test_rename_out_of_whitelist_is_left_alone(self, foreign_rename_out):
        repo = foreign_rename_out
        before = _head(repo)
        (repo / "index.html").write_text("report-0914")

        r = GitHubTool(repo_path=str(repo)).commit("Alpha Hive 蜂群日报", paths=rd.REPORT_ARTIFACT_PATHS)

        assert r["success"] is True
        assert _committed(repo) == {"index.html"}, "别人 rename 的删除半边被当成日报提交掉了"
        assert _head(repo) != before
        staged = _git(repo, "diff", "--cached", "--name-status").stdout
        assert "hedge_state/positions.json" in staged and "data_root/hedge_positions.json" in staged, (
            f"对方的 rename 被拆开了，只剩一半还暂存着：{staged!r}")
        # 正对照：真的是同一个 rename，不是两条独立记录（--find-renames 默认视角应配对成 R）
        paired = _git(repo, "diff", "--cached", "--name-status").stdout
        assert paired.split()[0].startswith("R"), f"正对照没立住，git 没把它俩配成 rename：{paired!r}"

    def test_report_change_alone_in_that_scenario_is_not_swallowed_as_nothing_to_commit(self, foreign_rename_out):
        """排除了 rename 源之后，我们自己确实改了 index.html ⇒ 不该落进「白名单内无改动」的分支。"""
        repo = foreign_rename_out
        (repo / "index.html").write_text("report-0914")
        r = GitHubTool(repo_path=str(repo)).commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is True and _committed(repo) == {"index.html"}

    def test_only_the_foreign_rename_staged_commits_nothing(self, foreign_rename_out):
        """本次日报没有任何产物改动，工作区里唯一的白名单内暂存就是那个孤立的 rename 源 ⇒
        排除后 names 为空 ⇒ 必须走「nothing to commit」，不许退回裸提交。"""
        repo = foreign_rename_out
        before = _head(repo)
        r = GitHubTool(repo_path=str(repo)).commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is False and _head(repo) == before
        staged = _git(repo, "diff", "--cached", "--name-status").stdout
        assert "R" in staged.split()[0], f"对方的 rename 应当原样留着：{staged!r}"

    def test_rename_fully_inside_the_whitelist_is_still_committed_whole(self, repo):
        """正对照：新旧路径都在白名单内时（既有场景），不该被本条新逻辑误伤。"""
        (repo / "hedge_state").mkdir()
        (repo / "hedge_state" / "a.json").write_text('{"v": 1}')
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "init2")
        _git(repo, "mv", "hedge_state/a.json", "hedge_state/b.json")
        (repo / "index.html").write_text("report-0914")

        r = GitHubTool(repo_path=str(repo)).commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is True
        # `git show --name-only`（默认改名检测）把配对完整的 rename 折叠成一行新路径，不是两条
        assert _committed(repo) == {"index.html", "hedge_state/b.json"}
        assert _staged(repo) == set()

    def test_rename_into_the_whitelist_commits_the_addition_only(self, repo):
        """反方向：旧路径不在白名单、新路径在——无害，提交新路径当作一次新增即可，不牵扯旧路径。"""
        (repo / "scratch").mkdir()
        (repo / "scratch" / "a.json").write_text('{"v": 1}')
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "init3")
        (repo / "hedge_state").mkdir()
        _git(repo, "mv", "scratch/a.json", "hedge_state/a.json")
        (repo / "index.html").write_text("report-0914")

        r = GitHubTool(repo_path=str(repo)).commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is True
        assert _committed(repo) == {"index.html", "hedge_state/a.json"}, "旧路径不该被牵扯进我们的提交"
        staged = _git(repo, "diff", "--cached", "--name-status").stdout
        assert "scratch/a.json" in staged, f"旧路径的删除是对方的暂存，我们没动它就该原样留着：{staged!r}"


class TestRenameScanFailureIsReportedNotSwallowed:
    """v0.45.248：`_staged_names` 现在下发两条 `git diff` 命令——列白名单内暂存的名字，
    再列全量 name-status 找跨界 rename。第二条失败时不许假装『没有要排除的』就往下提交。"""

    def test_second_diff_call_failing_blocks_the_commit(self, repo, monkeypatch):
        (repo / "index.html").write_text("report-0914")
        g = GitHubTool(repo_path=str(repo))
        real = g.run_git_cmd

        def fail_second_diff(cmd):
            if cmd == "git diff --cached --name-status -z":
                return {"success": False, "error": "git diff timed out after 30 seconds"}
            return real(cmd)

        monkeypatch.setattr(g, "run_git_cmd", fail_second_diff)
        before = _head(repo)
        r = g.commit("日报", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"] is False and "timed out" in r.get("error", ""), r
        assert _head(repo) == before, "列不出有没有跨界 rename 要排除，就不许提交"
