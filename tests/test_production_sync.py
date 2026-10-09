"""
生产 checkout 与 origin/main 的同步（v0.45.214；v0.45.402 起只剩扫描前快进）

固化 2026-09-01~11 六次 `git push origin main` 被拒（non-fast-forward）背后的根因：各 session 从 worktree
直推 origin/main，生产 checkout 从不 pull ⇒ **生产跑哪版代码是随机的**。取证全文见 `production_sync` 模块 docstring。

两组，全部在**真 git 沙箱**里跑（bare origin + 生产 checkout + 另一个 session 的 clone）：
  1. 扫描前只快进；做不到就保持现有代码并给出结局，绝不动工作区里的未提交改动
  2. 结果真能走到告警：写入者（production_sync / scan_timing）与读者（alert_manager）用同一个形状

v0.45.402（阶段 6）：日报提交 / 推送链（`push_main`）退役，原第 1 组（部署时落后也要推上去）与第 3 组
（合并推送后下一轮快进不卡死）随之删除；原第 4 组里的提交 / 推送告警用例同删，保留的是同步结局与 gh-pages 的告警通路。
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

import production_sync as ps  # noqa: E402
import scan_timing as st  # noqa: E402
from agent_toolbox import GitHubTool  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    # 若 pytest 从 git hook 里被拉起，继承的 GIT_DIR 会让下面每条 git 命令打到真仓库上
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)
    origin, prod, other = tmp_path / "origin.git", tmp_path / "prod", tmp_path / "other"

    def git(*a, cwd=prod, check=True):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check)

    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(prod), cwd=tmp_path)
    for name, text in (("index.html", "report-0911"), ("code.py", "v1"), ("notes.json", "n")):
        (prod / name).write_text(text)
    git("config", "user.email", "prod@t")
    git("config", "user.name", "prod")
    git("add", "-A")
    git("commit", "-qm", "init")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    git("config", "user.email", "session@t", cwd=other)
    git("config", "user.name", "session", cwd=other)

    def session_push(name, text, msg):
        """另一个 session：同步、改一个文件、推 origin/main。返回它推上去的提交。"""
        git("pull", "-q", "--ff-only", "origin", "main", cwd=other)
        (other / name).write_text(text)
        git("commit", "-qam", msg, cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        return git("rev-parse", "HEAD", cwd=other).stdout.strip()

    def origin_rev(ref="main"):
        return git("--git-dir", str(origin), "rev-parse", ref).stdout.strip()

    def origin_show(path):
        return git("--git-dir", str(origin), "show", f"main:{path}").stdout

    def head():
        return git("rev-parse", "HEAD").stdout.strip()

    def is_ancestor(a, b):
        return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    tool = GitHubTool(repo_path=str(prod))
    reporter = SimpleNamespace(agent_helper=SimpleNamespace(git=tool), date_str="2026-09-14")
    return SimpleNamespace(origin=origin, prod=prod, other=other, git=git, tool=tool,
                           reporter=reporter, session_push=session_push, origin_rev=origin_rev,
                           origin_show=origin_show, head=head, is_ancestor=is_ancestor)


# ═════════════════════════════ 2. 扫描前：只快进 ═════════════════════════════

class TestSyncBeforeScan:

    def test_up_to_date(self, world):
        res = ps.sync_before_scan(world.tool, today="2026-09-14")
        assert res["outcome"] == "up_to_date" and res["behind"] == 0

    def test_behind_fast_forwards_and_keeps_unrelated_local_edits(self, world):
        w = world
        before = w.head()
        session = w.session_push("code.py", "v2", "session: 改代码")
        (w.prod / "notes.json").write_text("生产里没提交的改动")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "fast_forwarded", res
        assert (res["before"], res["after"], res["behind"]) == (before, session, 1)
        assert (w.prod / "code.py").read_text() == "v2"
        assert (w.prod / "notes.json").read_text() == "生产里没提交的改动"

    def test_dirty_overlap_is_refused_and_local_edit_survives(self, world):
        """工作区里有未提交改动、恰好被 origin 改过：git 拒绝快进。不许 stash / reset 来硬过。"""
        w = world
        before = w.head()
        w.session_push("code.py", "v2", "session: 改代码")
        (w.prod / "code.py").write_text("生产里手改的")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "ff_refused" and res["detail"]
        assert w.head() == before
        assert (w.prod / "code.py").read_text() == "生产里手改的"
        assert w.git("stash", "list").stdout == "", "拿 stash 硬过了（stash 栈是所有 worktree 共享的）"

    def test_local_ahead_is_not_ok(self, world):
        """本地 main 有没推上去的提交 ⇒ 生产跑的不是 main，要红。"""
        w = world
        (w.prod / "code.py").write_text("生产里直接提交的")
        w.git("commit", "-qam", "直接在生产 checkout 提交")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "local_ahead" and res["ahead"] == 1
        assert res["outcome"] not in ps.OK_OUTCOMES

    def test_diverged_leaves_head_alone(self, world):
        w = world
        (w.prod / "notes.json").write_text("生产里直接提交的")
        w.git("commit", "-qam", "直接在生产 checkout 提交")
        before = w.head()
        w.session_push("code.py", "v2", "session: 改代码")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "diverged" and (res["ahead"], res["behind"]) == (1, 1)
        assert w.head() == before and (w.prod / "code.py").read_text() == "v1"

    def test_not_on_main_is_left_alone(self, world):
        w = world
        w.git("switch", "-q", "-c", "experiment")
        w.session_push("code.py", "v2", "session: 改代码")
        res = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert res["outcome"] == "not_on_main"
        assert (w.prod / "code.py").read_text() == "v1"

    def test_fetch_failure(self, world, tmp_path):
        world.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
        res = ps.sync_before_scan(world.tool, today="2026-09-14")
        assert res["outcome"] == "fetch_failed" and res["detail"]


# ═════════════════════════════ 4. 结果走到告警 ═════════════════════════════

class TestResultReachesAlerts:

    def test_result_file_lives_under_isolated_logs_dir(self, tmp_path):
        """默认位置走 PATHS（调用时求值）⇒ 被 conftest 的 ALPHA_HIVE_LOGS_DIR 隔离住。"""
        target = ps.write_result({"date": "2026-09-14", "outcome": "up_to_date"})
        assert target is not None
        assert target.parent == Path(os.environ["ALPHA_HIVE_LOGS_DIR"])
        assert str(target).startswith(str(tmp_path))

    def test_result_file_is_gitignored_in_the_checkout(self):
        """生产 checkout 里它每轮都写：不忽略就成了未跟踪文件，每次部署都进「跳过 N 个非日报文件」的噪音。

        v0.45.214 首次在生产真跑 CLI 时实测漏了这一条。`git check-ignore` 三个退出码分开处理：
        0=忽略 / 1=未忽略（红）/ 128=这里不是 git 仓库（导出树等，条件为真的是全集减人造特例，skip 正当）。"""
        r = subprocess.run(["git", "check-ignore", "-q", "logs/production_sync.json"],
                           cwd=_ROOT, capture_output=True, text=True)
        if r.returncode == 128:
            pytest.skip(f"不在 git 仓库里：{r.stderr.strip()}")
        assert r.returncode == 0, f"logs/production_sync.json 没被 .gitignore 忽略（exit={r.returncode}）"

    def test_load_only_returns_this_rounds_result(self, tmp_path):
        p = tmp_path / "ps.json"
        assert ps.load_for_date("2026-09-14", p) is None                      # 没有
        p.write_text("{坏的")
        assert ps.load_for_date("2026-09-14", p) is None                      # 坏的
        ps.write_result({"date": "2026-09-11", "outcome": "up_to_date"}, p)
        assert ps.load_for_date("2026-09-14", p) is None, "上一轮的结果不能冒充这一轮"
        ps.write_result({"date": "2026-09-14", "outcome": "ff_refused"}, p)
        assert ps.load_for_date("2026-09-14", p)["outcome"] == "ff_refused"

    def test_cli_exit_code_and_result_file(self, world, monkeypatch):
        # 数据根迁移阶段 4：`GitHubTool()` 默认仓库改读专用的 `ALPHA_HIVE_GIT_REPO`
        # （不再是数据根变量 `ALPHA_HIVE_HOME`）——两者阶段 5 之后会指向不同目录，
        # 这里的沙箱仓库必须挂在新变量上，否则本条测试还在验证一个已被改掉的行为。
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(world.prod))
        assert ps.main(["--date", "2026-09-14"]) == 0
        assert ps.load_for_date("2026-09-14")["outcome"] == "up_to_date"
        world.git("switch", "-q", "-c", "experiment")
        assert ps.main(["--date", "2026-09-14"]) == 1
        assert ps.load_for_date("2026-09-14")["outcome"] == "not_on_main"

    @staticmethod
    def _alerts(tmp_path, snap):
        from alert_manager import AlertAnalyzer
        status = {"status": "success", "total_duration_seconds": 1,
                  "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": snap}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status, ensure_ascii=False))
        a = AlertAnalyzer(report_dir=tmp_path)
        return a, [x.message for x in a.analyze(p)]

    def test_real_failures_reach_alert_manager(self, world, tmp_path):
        """写入者与读者用同一个形状：真沙箱里的真快进拒绝 → 真 snapshot → 真告警。

        v0.45.214 前那条规则读 `deploy_status`（零写入者），这里任何一边改了形状都会红。"""
        w = world
        w.session_push("index.html", "session 重渲染", "session: 重渲染")
        (w.prod / "index.html").write_text("生产里手改的")   # 与 session 的改动重叠 ⇒ 快进被拒
        sync = ps.sync_before_scan(w.tool, today="2026-09-14")
        assert sync["outcome"] not in ps.OK_OUTCOMES
        ps.write_result(sync)

        snap = st.snapshot("2026-09-14", extra={"gh_pages": {"success": True}})
        assert snap["production_sync"]["outcome"] == sync["outcome"]
        _, msgs = self._alerts(tmp_path, snap)
        assert any("生产代码 ≠ origin/main" in m for m in msgs), msgs

    def test_healthy_round_raises_no_sync_or_deploy_alert(self, world, tmp_path):
        """正对照：不是「什么都报」。"""
        w = world
        w.session_push("code.py", "v2", "session: 改代码")
        ps.write_result(ps.sync_before_scan(w.tool, today="2026-09-14"))
        # v0.45.444：健康的一轮也带 news_sources（main() 每轮都写）；缺了它 alert_manager 会把新闻来源检查记进 checks_skipped
        healthy_news = {"available": True, "n_known": 30, "n_unknown": 0,
                        "primary_source": "massive", "by_source": {"massive": 30}, "primary_status": {"ok": 30},
                        "non_primary_share": 0.0, "refusal_messages": []}
        snap = st.snapshot("2026-09-14", extra={"gh_pages": {"success": True}, "news_sources": healthy_news})
        # 同理组合 Greeks（v0.45.423 起 alert_manager 对「本轮没跑到组合 Greeks」记 checks_skipped）：
        # 健康一轮带一份「干净」的价核对计数；本测试自己拼 snapshot，不会经过真的 compute_day
        snap["counters"]["portfolio_greeks"] = {
            "n_stale": 0, "stale": [], "n_quote_stale": 0, "spy": {"stale": False},
            "execution_blocked": None, "hedge_undecided": None, "gaps": []}
        a, msgs = self._alerts(tmp_path, snap)
        assert not any("origin/main（" in m or "同步未执行" in m or "gh-pages 部署失败" in m for m in msgs), msgs
        assert not a.checks_skipped, a.checks_skipped

    @pytest.mark.parametrize("deploy", ["returns", "raises"])
    def test_main_writes_the_gh_pages_result_into_the_timing_snapshot(self, monkeypatch, deploy, tmp_path):
        """上面几条自己拼 snapshot；这条走**真 `main()` 的蜂群路径**，核对 `_timing.write` 真收到了 gh-pages 结局。

        `alpha_hive_daily_report.main` 里捕获 `_gh_pages` 的几行一旦被改坏，只有这条会红。
        v0.45.402：`extra` 里**不再有** `git_push` / `git_commit`（提交 / 推送链已退役，下游若还在读它们就是漏改）。"""
        import alpha_hive_daily_report as adr
        import yf_gate

        failed = {"success": False, "error": "x" * 800, "attempts": 3}

        # v0.45.444：main() 读本轮 `.swarm_results_<date>.json` 汇总新闻来源 ⇒ 桩带一个真目录与一份真文件
        (tmp_path / ".swarm_results_2026-09-14.json").write_text(json.dumps({"NVDA": {"agent_details": {
            "BuzzBeeWhisper": {"details": {"news_source": "yahoo_finance", "news_primary_status": "server_refused"}}}}}))

        class SwarmReporter:
            date_str = "2026-09-14"
            report_dir = tmp_path

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
                return {"gh_pages": failed, "deploy_env": "production"}

        written = []
        monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SwarmReporter)
        monkeypatch.setattr(yf_gate, "install", lambda: False)
        monkeypatch.setattr(adr._timing, "write", lambda d, extra=None, **k: written.append((d, extra)))
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", "--swarm", "--no-llm", "--force"])
        adr.main()

        assert len(written) == 1, written
        extra = written[0][1]
        assert set(extra) == {"gh_pages", "news_sources"}, f"extra 里出现了已退役的字段：{sorted(extra)}"
        ns = extra["news_sources"]
        assert ns["available"] is True and ns["by_source"] == {"yahoo_finance": 1}, ns
        ghp = extra["gh_pages"]
        assert ghp["success"] is False
        if deploy == "returns":
            assert ghp["attempts"] == 3 and len(ghp["error"]) == 300
        else:
            assert "Too many open files" in ghp["error"], "部署抛异常被记成了「没记录」"

    def test_local_ahead_alert_does_not_call_it_old_code(self, tmp_path):
        snap = st.snapshot("2026-09-14", extra={"gh_pages": {"success": True}})
        snap["production_sync"] = {"date": "2026-09-14", "outcome": "local_ahead", "ahead": 1, "behind": 0}
        a, _ = self._alerts(tmp_path, snap)
        hit = [x for x in a.alerts if "origin/main（local_ahead）" in x.message]
        assert hit, [x.message for x in a.alerts]
        assert "旧代码" not in hit[0].message and "不是旧代码" in hit[0].details["含义"]

    def test_missing_sync_result_is_not_silence(self, tmp_path):
        """编排器没调 production_sync.py（或日期对不上）⇒ 不知道跑的是哪版，要出声。"""
        snap = st.snapshot("2026-09-14", extra={"gh_pages": {"success": True}})
        assert snap["production_sync"] is None
        _, msgs = self._alerts(tmp_path, snap)
        assert any("同步未执行" in m for m in msgs), msgs

    def test_missing_gh_pages_record_is_skipped_not_green(self, tmp_path):
        snap = st.snapshot("2026-09-14")
        a, msgs = self._alerts(tmp_path, snap)
        assert not any("gh-pages 部署失败" in m for m in msgs)
        assert any("gh-pages 部署检查" in s for s in a.checks_skipped)

    def test_retired_commit_and_push_records_raise_nothing(self, tmp_path):
        """阶段 6 退役的两个字段即便出现在旧快照里也不再被读：既不告警、也不记成「未执行的检查」。
        （若告警规则被接回去，这条红；若有人把字段又写回 scan_timing，上面的 main 测试红。）"""
        snap = st.snapshot("2026-09-14", extra={"gh_pages": {"success": True},
                                                "git_push": {"success": False, "error": "legacy"},
                                                "git_commit": {"success": False, "pending_artifacts": 3}})
        a, msgs = self._alerts(tmp_path, snap)
        assert not any("推送失败" in m or "日报提交失败" in m for m in msgs), msgs
        assert not any("main 推送" in s for s in a.checks_skipped), a.checks_skipped
