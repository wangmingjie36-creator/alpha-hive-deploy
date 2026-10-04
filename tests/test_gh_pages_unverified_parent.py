"""gh-pages 重试「假成功」（v0.45.351，2026-09-25 事故）。

事故序列（`~/.claude/logs/orchestrator-2026-09-25.log` 14:46:41–43，GitHub 不可达）：
  attempt 1：fetch 失败 ⇒ 父 = 本地 gh-pages（未校验）⇒ commit-tree 316ba1d ⇒
             **push 之前**就 `update-ref` 本地 gh-pages ⇒ push 失败
  attempt 2：fetch 失败 ⇒ 父 = 本地 ref = 自己没推上去的 316ba1d ⇒ tree 相同 ⇒
             「远端已经是目标状态」捷径 ⇒ success=True，日志「gh-pages 部署成功」
             + 「CDN 验证跳过……gh-pages 已推送成功」。网站停在 09-24 两天，零告警。

不变式（本文件守的）：**`success=True` 只有两条来路——fetch 校验过的远端真头已是
目标 tree，或本轮一次 `git push` 被远端接受。**

网络故障用**真 git** 造：把 `origin` 指到一个不存在的路径，fetch / ls-remote / push
全部真失败（非零退出），不 mock subprocess。只有「fetch 坏、push 好」这种真实环境里
难以稳定复现的半故障，才打桩 `resolve_gh_pages_parent`。

`git_transport_probe` 的裸 socket 由 conftest `_block_git_transport_probe` 钉死；
测探测本身的用例在测试体里覆盖。
"""
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import git_transport_probe as gtp
import report_deployer as rd

_ROOT = Path(__file__).resolve().parent.parent


# ───────────────────────────── 真 git 夹具 ─────────────────────────────

def _git(*args, cwd) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout.strip()


def _rev(ref, cwd):
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=str(cwd),
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def _init(tmp_path, name="repo"):
    bare = tmp_path / f"{name}_origin.git"
    repo = tmp_path / name
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", str(bare), cwd=repo)
    (repo / "README.md").write_text("x")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo, bare


def _tree(repo, filename, content):
    """独立 index 建一棵单文件 tree（不碰仓库主 index）。"""
    (repo / filename).write_text(content)
    blob = _git("hash-object", "-w", filename, cwd=repo)
    env = dict(os.environ, GIT_INDEX_FILE=str(repo / ".git" / f"idx-{filename}"))
    subprocess.run(["git", "update-index", "--add", "--cacheinfo", "100644", blob, filename],
                   cwd=str(repo), env=env, check=True)
    tree = subprocess.run(["git", "write-tree"], cwd=str(repo), env=env,
                          capture_output=True, text=True, check=True).stdout.strip()
    os.remove(env["GIT_INDEX_FILE"])
    return tree


def _remote_head(bare):
    return _rev("refs/heads/gh-pages", bare)


def _offline(repo, tmp_path):
    """真断网的 git 形状：origin 指向不存在的路径 ⇒ fetch / ls-remote / push 全部非零退出。"""
    _git("remote", "set-url", "origin", str(tmp_path / "unreachable" / "gone.git"), cwd=repo)


def _online(repo, bare):
    _git("remote", "set-url", "origin", str(bare), cwd=repo)


@pytest.fixture
def no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    return calls


@pytest.fixture
def deployed(tmp_path, no_sleep):
    """day0 已正常部署（fetch 校验、推送成功）：远端与本地 gh-pages 都在 c0。"""
    repo, bare = _init(tmp_path)
    tree0 = _tree(repo, "day0.html", "d0")
    r0 = rd.commit_and_push_gh_pages(str(repo), tree0, lambda n: "day0")
    assert r0["success"] and r0["parent_verified"] is True, r0
    c0 = r0["commit"]
    assert _remote_head(bare) == c0 and _rev("refs/heads/gh-pages", repo) == c0
    return SimpleNamespace(repo=repo, bare=bare, c0=c0, tree0=tree0, sleeps=no_sleep)


# ─────────────────────── ① 事故原序：两次都失败，不许判成功 ───────────────────────

class TestIncidentSequence:

    def test_fetch_and_push_fail_every_attempt_is_not_success(self, deployed, tmp_path):
        """09-25 原序：每次 fetch 失败、每次 push 失败。旧代码 attempt 2 拿自己没推上去的
        提交当父、tree 相同 ⇒ 捷径判成功（本条在旧代码下红，见 CHANGELOG v0.45.351 变异表）。"""
        d = deployed
        _offline(d.repo, tmp_path)
        tree1 = _tree(d.repo, "day1.html", "d1")

        r = rd.commit_and_push_gh_pages(str(d.repo), tree1, lambda n: "day1")

        assert r["success"] is False, f"推送从未成功却判了成功（09-25 假成功复发）：{r}"
        assert r["action"] is None
        assert r["attempts"] == 4, "应该用尽全部重试，而不是在第 2 次被捷径截停"
        assert r["parent_verified"] is False
        assert r["last_error"], "失败原因丢了"
        assert _remote_head(d.bare) == d.c0, "远端不该变"
        # 修法 b：本地 ref 只在推送成功后前移——失败时仍指向远端最后确认的 c0
        assert _rev("refs/heads/gh-pages", d.repo) == d.c0, (
            "本地 gh-pages 被前移到了没推上去的提交——下一轮 fetch 再失败时它会被当父提交")
        # 每次 attempt 的父提交都是 c0（没有「拿自己的未推送提交当父」）
        assert r["parent"] == d.c0 and r["tree_unchanged"] is False

    def test_two_attempts_exactly_as_logged(self, deployed, tmp_path):
        """与日志逐字对应的 max_attempts=2 版本：attempt 2 的结局必须是失败。"""
        d = deployed
        _offline(d.repo, tmp_path)
        tree1 = _tree(d.repo, "day1.html", "d1")
        r = rd.commit_and_push_gh_pages(str(d.repo), tree1, lambda n: "day1", max_attempts=2)
        assert r["success"] is False and r["attempts"] == 2, r
        assert d.sleeps == [2.0], "attempt 1 失败后应退避 2s 再重试（与 09-25 日志「2s 后重新 fetch」一致）"

    def test_stale_unpushed_local_commit_with_target_tree_is_not_success(self, deployed, tmp_path):
        """修法 a 单独守：本地 gh-pages 已经指向一个**没推上去**、tree 恰为目标的提交
        （旧代码 09-25 留下的正是这个状态：316ba1d）。fetch 与 push 都失败 ⇒ 不许判成功。
        这条不依赖修法 b——即使本地 ref 永不再被提前前移，存量状态照样会触发捷径。"""
        d = deployed
        tree1 = _tree(d.repo, "day1.html", "d1")
        stale = _git("commit-tree", tree1, "-p", d.c0, "-m", "unpushed (like 316ba1d)", cwd=d.repo)
        _git("update-ref", "refs/heads/gh-pages", stale, cwd=d.repo)
        _offline(d.repo, tmp_path)

        r = rd.commit_and_push_gh_pages(str(d.repo), tree1, lambda n: "day1")

        assert r["parent"] == stale and r["tree_unchanged"] is True, "前置条件：本地 = 目标"
        assert r["success"] is False, f"「本地 = 目标」被当成了「远端 = 目标」：{r}"
        assert _remote_head(d.bare) == d.c0

    def test_failed_push_does_not_create_orphan_parent_history(self, deployed, tmp_path):
        """网络恢复后的那一轮：父提交必须是远端真头 c0，历史里不混入没推上去的提交。"""
        d = deployed
        _offline(d.repo, tmp_path)
        tree1 = _tree(d.repo, "day1.html", "d1")
        assert rd.commit_and_push_gh_pages(str(d.repo), tree1, lambda n: "x")["success"] is False
        _online(d.repo, d.bare)
        r = rd.commit_and_push_gh_pages(str(d.repo), tree1, lambda n: "day1")
        assert r["success"] and r["parent_verified"] and r["action"] == "pushed_new_commit", r
        assert _git("rev-parse", f"{r['commit']}^", cwd=d.repo) == d.c0, (
            "新提交的父不是远端真头——没推上去的提交混进了发布历史")


# ─────────────────────── ② 恢复：网络回来时要真送达 ───────────────────────

class TestRecovery:

    def test_network_returns_mid_retry_and_deploy_really_lands(self, deployed, tmp_path, monkeypatch):
        """attempt 1、2 断网，第 2 次退避期间网络恢复 ⇒ attempt 3 fetch 校验 ⇒ 推送送达。
        旧代码在 attempt 2 就假成功返回，远端永远停在 c0。"""
        d = deployed
        _offline(d.repo, tmp_path)
        tree1 = _tree(d.repo, "day1.html", "d1")
        sleeps = []

        def _sleep(s):
            sleeps.append(s)
            if len(sleeps) == 2:
                _online(d.repo, d.bare)

        monkeypatch.setattr(time, "sleep", _sleep)
        r = rd.commit_and_push_gh_pages(str(d.repo), tree1, lambda n: "day1")

        assert r["success"] and r["attempts"] == 3, r
        assert r["parent_verified"] is True and r["action"] == "pushed_new_commit"
        head = _remote_head(d.bare)
        assert head == r["commit"]
        assert _git("rev-parse", f"{head}^{{tree}}", cwd=d.repo) == tree1, "远端内容不是本次 tree"
        assert _rev("refs/heads/gh-pages", d.repo) == head, "推送成功后本地 ref 应前移"
        assert r["transport_probe"] is not None, "首次推送失败时应跑过一次传输探测"

    def test_unverified_parent_with_target_tree_is_pushed_not_assumed(self, deployed, monkeypatch):
        """半故障：只有 fetch 坏（`resolve_gh_pages_parent` 报未校验）、push 正常。本地 ref 是
        一个 tree 恰为目标、从未推送的提交 ⇒ 不造空提交，把它本身推上去，远端真的收到。
        旧代码：捷径判成功、什么都没推 ⇒ 远端仍是 c0（本条红）。"""
        d = deployed
        tree1 = _tree(d.repo, "day1.html", "d1")
        local = _git("commit-tree", tree1, "-p", d.c0, "-m", "unpushed", cwd=d.repo)
        _git("update-ref", "refs/heads/gh-pages", local, cwd=d.repo)
        monkeypatch.setattr(rd, "resolve_gh_pages_parent", lambda repo: (local, False))

        r = rd.commit_and_push_gh_pages(str(d.repo), tree1, lambda n: "day1")

        assert _remote_head(d.bare) == local, f"判了成功，但远端根本没收到：{r}"
        assert r["success"] and r["action"] == "pushed_existing_local" and r["commit"] == local, r

    def test_unverified_parent_already_on_remote_is_up_to_date_success(self, deployed, monkeypatch):
        """同上半故障，但远端其实已经就是它 ⇒ push 回 up-to-date（退出 0）⇒ 成功是远端给的。"""
        d = deployed
        monkeypatch.setattr(rd, "resolve_gh_pages_parent", lambda repo: (d.c0, False))
        r = rd.commit_and_push_gh_pages(str(d.repo), d.tree0, lambda n: "noop")
        assert r["success"] and r["action"] == "pushed_existing_local", r
        assert _remote_head(d.bare) == d.c0


# ─────────────────────── ③ 合法捷径不许被一并删掉 ───────────────────────

class TestVerifiedShortcutStillWorks:

    def test_verified_unchanged_tree_succeeds_without_pushing(self, deployed, monkeypatch):
        """正对照：fetch 校验过、远端已是目标 ⇒ 成功且**不推送**。若把捷径整个删掉，
        会走「推已有提交」支路（action 变）——本条据 action 与推送次数抓它。"""
        d = deployed
        real = rd._run_git_network
        pushes = []

        def spy(args, repo):
            if args[:2] == ["git", "push"]:
                pushes.append(args)
            return real(args, repo)

        monkeypatch.setattr(rd, "_run_git_network", spy)
        r = rd.commit_and_push_gh_pages(str(d.repo), d.tree0, lambda n: "again")
        assert r["success"] and r["action"] == "remote_already_current" and r["commit"] == d.c0, r
        assert pushes == [], "远端已是目标，不该推送"
        assert r["transport_probe"] is None, "成功路径不许跑传输探测（零开销约定）"


# ─────────────────────── ④ 端到端：部署函数 → 部署日志 → Step 5 判定 ───────────────────────

def _make_reporter(repo_path):
    """`deploy_static_to_ghpages` 只读 reporter 的 `agent_helper.git.repo_path` 与 `date_str`
    （`_DEPLOY_BASE_URL` 只给 CDN 验证用，这里 CDN 被替身接住）。不构造真
    `AlphaHiveDailyReporter`——冷启动 26~53s，贴着 60s 超时，且与本文件要验的东西无关。"""
    return SimpleNamespace(
        agent_helper=SimpleNamespace(git=SimpleNamespace(repo_path=str(repo_path))),
        date_str="2026-09-25",
        _DEPLOY_BASE_URL="https://example-user.github.io/alpha-hive-deploy",
    )


def _deploy_log():
    from hive_logger import PATHS      # conftest 用 ALPHA_HIVE_LOGS_DIR 把日志目录隔离到 tmp
    p = PATHS.logs_dir_unmade() / rd.GH_PAGES_DEPLOY_LOG_NAME
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestDeployFunctionEndToEnd:

    def test_offline_deploy_returns_failure_logs_it_and_step5_goes_red(
            self, tmp_path, monkeypatch, no_sleep, caplog):
        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
        repo, bare = _init(tmp_path, "coderepo")
        reporter = _make_reporter(repo)
        cdn = MagicMock(return_value=True)
        monkeypatch.setattr(rd, "verify_cdn_deployment", cdn)

        (data_root / "index.html").write_text("<html>09-24</html>")
        ok = rd.deploy_static_to_ghpages(reporter)
        assert ok["success"] is True and ok["action"] == "pushed_new_commit", ok
        c0 = _remote_head(bare)

        since = time.time() - 1
        (data_root / "index.html").write_text("<html>09-25</html>")
        _offline(repo, tmp_path)
        caplog.clear()                          # 只看断网这一次部署的日志
        with caplog.at_level(logging.INFO, logger="alpha_hive.report_deployer"):
            res = rd.deploy_static_to_ghpages(reporter)

        assert res["success"] is False, res
        assert _remote_head(bare) == c0 and _rev("refs/heads/gh-pages", repo) == c0
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("部署成功" in m for m in msgs), "失败的部署印了「部署成功」"
        assert any("本轮网站未更新" in m for m in msgs), msgs
        assert cdn.call_count == 1, "CDN 验证只该在成功那次调用（它不能替失败背书）"

        last = _deploy_log()[-1]
        assert last["status"] == "failed" and last["parent_verified"] is False, last
        assert last["date_str"] == "2026-09-25" and last["last_error"]

        step5 = rd.gh_pages_step_status(since)
        assert step5["status"] == "failed" and step5["reason"] == "gh_pages_deploy_failed", step5

    def test_empty_data_root_early_return_is_recorded_as_failure(self, tmp_path, monkeypatch):
        """早退（无文件可部署）此前只打 warning、不留记录 ⇒ Step 5 会读成「无记录」。
        现在要留一条 failed 记录并返回失败。v0.45.310 的空树守卫仍在（远端没被推成空）。"""
        data_root = tmp_path / "data_root"
        data_root.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
        repo, bare = _init(tmp_path, "coderepo")
        reporter = _make_reporter(repo)
        since = time.time() - 1
        res = rd.deploy_static_to_ghpages(reporter)
        assert res["success"] is False and res["reason"] == "no_files", res
        assert _remote_head(bare) is None, "空数据根不许推出任何 gh-pages 提交"
        assert _deploy_log()[-1]["reason"] == "no_files"
        assert rd.gh_pages_step_status(since)["status"] == "failed"


# ─────────────────────── ⑤ Step 5 判定器与 CLI ───────────────────────

class TestGhPagesStepStatus:

    @staticmethod
    def _write(path, *records, raw=()):
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
            for ln in raw:
                f.write(ln + "\n")

    def test_no_file_is_no_deploy_record_not_success(self, tmp_path):
        s = rd.gh_pages_step_status(0, log_path=str(tmp_path / "nope.jsonl"))
        assert s["status"] == "failed" and s["reason"] == "no_deploy_record"

    def test_entries_before_since_are_ignored(self, tmp_path):
        p = tmp_path / "log.jsonl"
        self._write(p, {"timestamp": "2026-09-24T21:53:23.351450Z", "status": "success"})
        since = 1790000000  # 2026-09-21 之后的某刻；上面那条在它之后 ⇒ 用于正对照
        assert rd.gh_pages_step_status(since, str(p))["status"] == "success"
        late = 1790000000 + 10 ** 7
        assert rd.gh_pages_step_status(late, str(p))["reason"] == "no_deploy_record", (
            "昨天的成功记录冒充了今天")

    def test_latest_entry_wins_and_bad_lines_are_skipped(self, tmp_path):
        p = tmp_path / "log.jsonl"
        self._write(p,
                    {"timestamp": "2026-09-25T21:00:00Z", "status": "success"},
                    {"timestamp": "2026-09-25T21:46:43.738300Z", "status": "failed",
                     "last_error": "ssh: connect to host github.com port 22", "attempts": 4},
                    raw=("{broken json", '{"no_timestamp": 1}'))
        s = rd.gh_pages_step_status(0, str(p))
        assert s["status"] == "failed" and s["reason"] == "gh_pages_deploy_failed", s
        assert "port 22" in s["detail"] and s["attempts"] == 4

    def test_unreadable_log_is_failure(self, tmp_path):
        d = tmp_path / "is_a_dir"
        d.mkdir()
        s = rd.gh_pages_step_status(0, str(d))
        assert s["status"] == "failed" and s["reason"] == "deploy_log_unreadable", s

    def test_cli_prints_exactly_one_json_line(self, tmp_path):
        """编排器靠 stdout 的最后一行 JSON；日志必须走 stderr，不许污染 stdout。"""
        home = tmp_path / "home"
        (home / "logs").mkdir(parents=True)
        self._write(home / "logs" / rd.GH_PAGES_DEPLOY_LOG_NAME,
                    {"timestamp": "2026-09-25T21:46:43Z", "status": "failed", "last_error": "x"})
        env = dict(os.environ, ALPHA_HIVE_HOME=str(home))
        env.pop("ALPHA_HIVE_LOGS_DIR", None)   # 生产编排器不设它 ⇒ 日志在 $ALPHA_HIVE_HOME/logs
        p = subprocess.run([sys.executable, str(_ROOT / "report_deployer.py"),
                            "--gh-pages-step-status", "--since", "0"],
                           capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)
        assert p.returncode == 0, p.stderr
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, p.stdout
        assert json.loads(lines[0])["reason"] == "gh_pages_deploy_failed"


# ─────────────────────── ⑥ CDN 检查不替没核过的事背书 ───────────────────────

class TestCdnCheckDoesNotVouch:

    def test_dns_failure_returns_none_and_claims_nothing(self, tmp_path, monkeypatch, caplog):
        (tmp_path / "dashboard-data.json").write_text(json.dumps({"_generated_at": "2026-09-25T14:46"}))
        reporter = SimpleNamespace(_DEPLOY_BASE_URL="https://example-user.github.io/alpha-hive-deploy")

        def _gai(*a, **k):
            raise socket.gaierror(8, "nodename nor servname provided, or not known")

        monkeypatch.setattr(socket, "getaddrinfo", _gai)
        before = socket.getdefaulttimeout()
        with caplog.at_level(logging.INFO, logger="alpha_hive.report_deployer"):
            out = rd.verify_cdn_deployment(reporter, str(tmp_path))
        assert out is None, "解析不了域名 = 没验证，不能返回 True（与「验证通过」同形）"
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "已推送成功" not in text and "沙箱" not in text, text
        assert "未执行" in text
        assert socket.getdefaulttimeout() == before, "进程级默认 socket 超时被改了没复原"


# ─────────────────────── ⑦ 结局进 results / scan_timing / 告警 ───────────────────────

class TestResultReachesStatusAndAlerts:

    @staticmethod
    def _alerts(tmp_path, extra):
        import alert_manager as am
        status = {"status": "success", "total_duration_seconds": 1,
                  "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"date": "2026-09-25", "production_sync": {"outcome": "up_to_date"},
                                  "extra": extra}}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status, ensure_ascii=False))
        a = am.AlertAnalyzer(report_dir=tmp_path)
        return a, a.analyze(p)

    def test_failed_gh_pages_raises_p1(self, tmp_path):
        import scan_timing as st
        ghp = st.gh_pages_summary({
            "success": False, "attempts": 4, "parent_verified": False, "commit": "316ba1dd" * 5,
            "last_error": "ssh: connect to host github.com port 22: Undefined error: 0",
            "transport_probe": {"verdict": "dns_failed", "meaning": gtp.VERDICT_MEANING["dns_failed"]}})
        a, alerts = self._alerts(tmp_path, {"gh_pages": ghp})
        hit = [x for x in alerts if "gh-pages 部署失败" in x.message]
        assert hit and hit[0].level.name == "HIGH", [x.message for x in alerts]
        assert "port 22" in hit[0].details["原因"] and "dns_failed" in hit[0].details["传输探测"]

    def test_missing_gh_pages_is_a_skipped_check_not_a_pass(self, tmp_path):
        a, alerts = self._alerts(tmp_path, {})
        assert not any("gh-pages" in x.message for x in alerts)
        assert any("gh-pages" in s for s in a.checks_skipped), a.checks_skipped

    def test_successful_gh_pages_is_quiet(self, tmp_path):
        a, alerts = self._alerts(tmp_path, {"gh_pages": {"success": True, "action": "pushed_new_commit"}})
        assert not any("gh-pages" in x.message for x in alerts)
        assert not any("gh-pages" in s for s in a.checks_skipped)

    @pytest.mark.parametrize("behaviour", ["returns_failure", "raises", "returns_none"])
    def test_deploy_and_notify_records_gh_pages(self, tmp_path, monkeypatch, behaviour):
        """部署结局必须出现在 `results["gh_pages"]`，三种形状（返回失败 / 抛 / 返回 None）都不许丢。"""

        def _deploy(reporter):
            if behaviour == "raises":
                raise RuntimeError("Too many open files")
            if behaviour == "returns_none":
                return None
            return {"success": False, "last_error": "ssh: port 22"}

        monkeypatch.setattr(rd, "deploy_static_to_ghpages", _deploy)
        reporter = SimpleNamespace(agent_helper=SimpleNamespace(git=MagicMock()), date_str="2026-09-25")
        res = rd.deploy_and_notify(reporter, {"swarm_metadata": {"x": 1}})
        assert res["gh_pages"]["success"] is False, res
        if behaviour == "raises":
            assert "Too many open files" in res["gh_pages"]["error"]

    @pytest.mark.parametrize("deploy", ["returns", "raises"])
    def test_main_writes_gh_pages_into_timing_snapshot(self, monkeypatch, deploy):
        """走真 `main()` 的蜂群路径，核对 `_timing.write` 真收到了 gh-pages 结局
        （`main` 里捕获 `_gh_pages` 那几行被改坏，只有这条会红）。"""
        import alpha_hive_daily_report as adr
        import yf_gate

        class SwarmReporter:
            date_str = "2026-09-25"

            def __init__(self, date_override=None):
                pass

            def run_swarm_scan(self, focus_tickers=None):
                return {"system_status": "✅ 蜂群协作完成", "swarm_metadata": {"tickers_analyzed": 1},
                        "opportunities": [{"ticker": "NVDA"}]}

            def save_report(self, report):
                return "/dev/null"

            def deploy_and_notify(self, report):
                if deploy == "raises":
                    raise OSError("Too many open files")
                return {"deploy_env": "production",
                        "gh_pages": {"success": False, "attempts": 4, "last_error": "port 22"}}

        written = []
        monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SwarmReporter)
        monkeypatch.setattr(yf_gate, "install", lambda: False)
        monkeypatch.setattr(adr._timing, "write", lambda d, extra=None, **k: written.append((d, extra)))
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", "--swarm", "--no-llm", "--force"])
        adr.main()
        ghp = written[0][1]["gh_pages"]
        assert ghp is not None and ghp["success"] is False, written
        if deploy == "returns":
            assert ghp["attempts"] == 4 and ghp["error"] == "port 22"
        else:
            assert "Too many open files" in ghp["error"]


# ─────────────────────── ⑧ 传输探测（为「是否切 ssh.github.com:443」攒判据）───────────────────────

def _stub_net(monkeypatch, dns=None, tcp=None):
    """dns/tcp：{host 或 port: 异常 或 None}。未列出的 = 成功。"""
    def _resolve(host, port):
        exc = (dns or {}).get(host)
        if exc:
            raise exc
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]

    def _connect(family, sockaddr, timeout):
        exc = (tcp or {}).get(sockaddr[1])
        if exc:
            raise exc

    monkeypatch.setattr(gtp, "_resolve", _resolve)
    monkeypatch.setattr(gtp, "_tcp_connect", _connect)


class TestTransportProbe:

    def test_dns_failure_verdict(self, monkeypatch):
        """09-25 那种：DNS 本身失败 ⇒ 切 443 救不了。"""
        err = socket.gaierror(8, "nodename nor servname provided, or not known")
        _stub_net(monkeypatch, dns={"github.com": err, "ssh.github.com": err})
        rec = gtp.probe_github_transport("ssh: connect to host github.com port 22: Undefined error: 0\nfatal: x")
        assert rec["verdict"] == "dns_failed", rec
        t = rec["targets"]["github.com:22"]
        assert t["dns_ok"] is False and t["tcp_ok"] is None and "nodename" in t["dns_error"]
        assert rec["git_stderr_last_line"] == "fatal: x"
        assert rec["at"][:4].isdigit() and "T" in rec["at"], "时间戳要带完整日期"

    def test_port22_blocked_but_443_open_verdict(self, monkeypatch):
        """切 443 能救的那种：DNS 正常、只有 22 不通。"""
        _stub_net(monkeypatch, tcp={22: ConnectionRefusedError(61, "Connection refused")})
        rec = gtp.probe_github_transport("x")
        assert rec["verdict"] == "port22_blocked_443_ok", rec
        assert rec["targets"]["github.com:22"]["tcp_ok"] is False
        assert rec["targets"]["ssh.github.com:443"]["tcp_ok"] is True

    @pytest.mark.parametrize("tcp,verdict", [
        ({22: OSError("x"), 443: OSError("y")}, "both_blocked"),
        ({}, "transport_ok"),
        ({443: OSError("y")}, "port443_blocked_22_ok"),
    ])
    def test_other_verdicts(self, monkeypatch, tcp, verdict):
        _stub_net(monkeypatch, tcp=tcp)
        assert gtp.probe_github_transport(None)["verdict"] == verdict

    def test_hanging_resolver_is_bounded(self, monkeypatch):
        """getaddrinfo 不认 socket 超时——卡住时要按步骤上限放弃，不能把部署拖住。"""
        monkeypatch.setattr(gtp, "_STEP_TIMEOUT", 0.2)
        release = []

        def _hang(host, port):
            while not release:
                time.sleep(0.01)
            raise OSError("released")

        monkeypatch.setattr(gtp, "_resolve", _hang)
        t0 = time.monotonic()
        rec = gtp.probe_github_transport(None)
        release.append(1)
        assert time.monotonic() - t0 < 3, "探测没有被超时兜住"
        assert rec["verdict"] == "dns_failed" and "超时" in rec["targets"]["github.com:22"]["dns_error"]

    def test_probe_bug_never_escapes(self, monkeypatch):
        """纯观测：探测代码自身抛异常，也只变成一条 probe_error 记录。"""
        monkeypatch.setattr(gtp, "_probe_target", MagicMock(side_effect=RuntimeError("bug")))
        rec = gtp.probe_github_transport("x")
        assert rec["verdict"] == "probe_error" and "bug" in rec["probe_error"]

    def test_proxy_env_records_names_not_values(self, monkeypatch):
        _stub_net(monkeypatch)
        for v in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@127.0.0.1:7897")
        rec = gtp.probe_github_transport(None)
        assert rec["proxy_env"] == ["HTTPS_PROXY"]
        assert "secret" not in json.dumps(rec), "代理变量的值（可能含凭据）进了记录"

    def test_probe_failure_does_not_change_deploy_verdict(self, deployed, tmp_path, monkeypatch):
        """探测整个炸掉（连 import 后的调用都抛）⇒ 部署照常判失败、照常返回，不冒泡。"""
        d = deployed
        monkeypatch.setattr(gtp, "probe_github_transport", MagicMock(side_effect=RuntimeError("boom")))
        _offline(d.repo, tmp_path)
        r = rd.commit_and_push_gh_pages(str(d.repo), _tree(d.repo, "day1.html", "d1"), lambda n: "x")
        assert r["success"] is False and r["transport_probe"]["verdict"] == "probe_error", r

    def test_probe_runs_once_per_deploy_and_is_offline_in_tests(self, deployed, tmp_path, request):
        """重试 4 次只探测一次（≤~20s 的上限按「每次部署」算）；conftest 桩确实接住了裸 socket。"""
        d = deployed
        _offline(d.repo, tmp_path)
        r = rd.commit_and_push_gh_pages(str(d.repo), _tree(d.repo, "day1.html", "d1"), lambda n: "x")
        calls = request.node._git_probe_calls
        assert [c[0] for c in calls].count("resolve") == 2, calls   # 两个目标各解析一次，只探一轮
        assert r["transport_probe"]["verdict"] == "dns_failed"
        assert "测试默认离线" in r["transport_probe"]["targets"]["github.com:22"]["dns_error"]


# ─────────────────────── ⑨ 二次检查（v0.45.378）───────────────────────

class TestSecondReview:

    def test_cdn_without_expected_timestamp_is_not_verified(self, tmp_path):
        """dashboard-data.json 没有 `_generated_at` ⇒ 无从比对 ⇒ 没验（None），不是 True。"""
        (tmp_path / "dashboard-data.json").write_text(json.dumps({"x": 1}))
        reporter = SimpleNamespace(_DEPLOY_BASE_URL="https://example-user.github.io/alpha-hive-deploy")
        assert rd.verify_cdn_deployment(reporter, str(tmp_path)) is None

    def test_probe_prefers_ipv4_when_resolver_lists_ipv6_first(self, monkeypatch):
        """AAAA 排在前面、IPv6 不通时，探测不能把它读成「端口不通」。"""
        def _resolve(host, port):
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]

        def _connect(family, sockaddr, timeout):
            if family == socket.AF_INET6:
                raise OSError(65, "No route to host")

        monkeypatch.setattr(gtp, "_resolve", _resolve)
        monkeypatch.setattr(gtp, "_tcp_connect", _connect)
        rec = gtp.probe_github_transport(None)
        assert rec["verdict"] == "transport_ok", rec
        assert rec["targets"]["github.com:22"]["family"] == "ipv4"
