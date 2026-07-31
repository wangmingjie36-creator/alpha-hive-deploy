"""
日报自动提交白名单测试（v0.43.4）

固化一次真实事故：2026-07-30 定时日报走 `git add -A` 全量提交，
把当时工作区里进行中的 10 个版本代码改动（backtester / chronos_bee /
parallel_agent_runner / weekly_optimizer / 6 个测试文件…）全部卷进了
一次名为「Alpha Hive 蜂群日报 14:02」的提交（commit 68aad61）。

后果：提交历史失真、无法单独回滚代码、半成品代码会被自动推上生产分支。

自动化边界必须按**白名单**定义：漏一项 → 产物不进 git（下次即可见）；
黑名单漏一项 → 半成品被自动提交（无人知晓）。
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_deployer as rd
from agent_toolbox import GitHubTool


# commit 68aad61 里的真实非代码文件 = 必须被提交的产物
REAL_ARTIFACTS = [
    "alpha-hive-daily-2026-07-30.json",
    "alpha-hive-daily-2026-07-30.md",
    "alpha-hive-thread-2026-07-30-1.txt",
    "dashboard-data.json", "index.html", "rss.xml", "sw.js",
    "weight_history.jsonl",
    "report_snapshots/AMZN_2026-06-15.json",
    "paper_portfolio_state/meta.json",
    ".factor_cache/ff5_daily.parquet",
    # 那次恰好没生成，但属于日报产物
    "alpha-hive-NVDA-ml-enhanced-2026-07-30.html",
    "analysis-NVDA-ml-2026-07-30.json",
]

# 那次被误提交的代码文件 = 必须被跳过
REAL_CODE = [
    "backtester.py", "feedback_loop.py", "health_check.py",
    "parallel_agent_runner.py", "weekly_optimizer.py", "ic_diagnostics.py",
    "alpha_hive_daily_report.py", "swarm_agents/chronos_bee.py",
    "tests/test_backtester.py", "CHANGELOG.md", "signal_archive.py",
    "config.py",
]


class TestArtifactClassification:
    @pytest.mark.parametrize("path", REAL_ARTIFACTS)
    def test_artifacts_are_committed(self, path):
        assert rd._is_report_artifact(path), f"{path} 是日报产物，漏了会导致网站不更新"

    @pytest.mark.parametrize("path", REAL_CODE)
    def test_code_is_skipped(self, path):
        assert not rd._is_report_artifact(path), \
            f"{path} 是代码，不得被定时任务自动提交（2026-07-30 事故文件）"

    def test_whitelist_is_non_empty_and_declarative(self):
        assert rd.REPORT_ARTIFACT_PATHS
        assert all(isinstance(p, str) for p in rd.REPORT_ARTIFACT_PATHS)


class TestWhitelistCommitInRealGit:
    """在真实 git 仓库里验证 —— 分类函数对了不代表 git add 行为对"""

    @pytest.fixture
    def repo(self, tmp_path):
        def run(*a):
            subprocess.run(a, cwd=tmp_path, check=True,
                           capture_output=True, text=True)
        run("git", "init", "-q")
        run("git", "config", "user.email", "t@t")
        run("git", "config", "user.name", "t")
        (tmp_path / "index.html").write_text("base")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "init")
        return tmp_path

    def test_only_artifacts_get_committed(self, repo):
        (repo / "report_snapshots").mkdir()
        (repo / "alpha-hive-daily-2026-07-30.json").write_text("{}")
        (repo / "index.html").write_text("updated")
        (repo / "report_snapshots" / "NVDA_2026-07-30.json").write_text("{}")
        # 模拟工作区里进行中的代码
        (repo / "backtester.py").write_text("# 半成品")
        (repo / "config.py").write_text("# 改到一半")

        g = GitHubTool(repo_path=str(repo))
        r = g.commit("日报测试", paths=rd.REPORT_ARTIFACT_PATHS)
        assert r["success"], r

        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True).stdout.split()
        assert "alpha-hive-daily-2026-07-30.json" in committed
        assert "index.html" in committed
        assert "report_snapshots/NVDA_2026-07-30.json" in committed
        assert "backtester.py" not in committed, "代码被误提交 —— 事故复现"
        assert "config.py" not in committed, "代码被误提交 —— 事故复现"

        left = subprocess.run(["git", "status", "--porcelain"],
                              cwd=repo, capture_output=True, text=True).stdout
        assert "backtester.py" in left and "config.py" in left, \
            "代码应完好留在工作区"

    def test_default_still_stages_everything(self, repo):
        """不传 paths 时保持 `git add -A`（向后兼容，其他调用方不受影响）"""
        (repo / "anything.py").write_text("x")
        g = GitHubTool(repo_path=str(repo))
        assert g.commit("全量")["success"]
        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True).stdout
        assert "anything.py" in committed

    def test_no_matching_paths_reports_failure(self, repo):
        """白名单一个都没匹配上时必须显式失败，而非静默创建空提交"""
        (repo / "only_code.py").write_text("x")
        g = GitHubTool(repo_path=str(repo))
        r = g.commit("空", paths=["nonexistent-pattern-*.xyz"])
        assert not r["success"]
