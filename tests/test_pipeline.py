"""
tests/test_pipeline.py — AlphaHiveDailyReporter 主流程单元测试

难点：__init__() 初始化 10+ 组件 → monkeypatch 把可选组件设 None，避免外部依赖。
"""

import os
import subprocess
import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime


# ==================== _build_swarm_report 测试 ====================

class TestBuildSwarmReport:
    """测试 _build_swarm_report 的核心逻辑（不启动完整 reporter）"""

    @pytest.fixture
    def reporter(self, monkeypatch, tmp_path):
        """创建一个最小化的 AlphaHiveDailyReporter（跳过重组件）"""
        # mock 掉所有可选依赖的导入
        import alpha_hive_daily_report as mod

        monkeypatch.setattr(mod, "MemoryStore", None)
        monkeypatch.setattr(mod, "CalendarIntegrator", None)
        monkeypatch.setattr(mod, "CodeExecutorAgent", None)
        monkeypatch.setattr(mod, "CODE_EXECUTION_CONFIG", {"enabled": False})
        monkeypatch.setattr(mod, "VectorMemory", None)
        monkeypatch.setattr(mod, "VECTOR_MEMORY_CONFIG", {"enabled": False})
        monkeypatch.setattr(mod, "MetricsCollector", None)
        monkeypatch.setattr(mod, "EarningsWatcher", None)
        monkeypatch.setattr(mod, "SlackReportNotifier", None)
        monkeypatch.setattr(mod, "Backtester", None)

        from alpha_hive_daily_report import AlphaHiveDailyReporter
        r = AlphaHiveDailyReporter()
        # 确保可选组件都是 None
        assert r.memory_store is None
        assert r.calendar is None
        assert r.slack_notifier is None
        return r

    def _make_swarm_results(self, tickers_scores):
        """构建 swarm_results 字典

        Args:
            tickers_scores: list of (ticker, score, direction)
        """
        results = {}
        for ticker, score, direction in tickers_scores:
            bull = 4 if direction == "bullish" else (1 if direction == "neutral" else 0)
            bear = 4 if direction == "bearish" else (1 if direction == "neutral" else 0)
            neut = 6 - bull - bear
            results[ticker] = {
                "final_score": score,
                "direction": direction,
                "resonance": {
                    "resonance_detected": score >= 7.0,
                    "supporting_agents": 4 if score >= 7.0 else 2,
                    "confidence_boost": 10 if score >= 7.0 else 0,
                },
                "supporting_agents": 4 if score >= 7.0 else 2,
                "distill_mode": "rule_only",
                "agent_breakdown": {"bullish": bull, "bearish": bear, "neutral": neut},
                "data_real_pct": 80.0,
                "agent_details": {},
            }
        return results

    def test_returns_required_keys(self, reporter):
        from pheromone_board import PheromoneBoard
        board = PheromoneBoard()
        swarm = self._make_swarm_results([("NVDA", 8.5, "bullish"), ("TSLA", 6.0, "bearish")])
        report = reporter._build_swarm_report(swarm, board, agent_count=7)

        required_keys = ["date", "timestamp", "system_status", "phase_completed",
                         "swarm_metadata", "opportunities", "markdown_report", "twitter_threads"]
        for k in required_keys:
            assert k in report, f"Missing key: {k}"

    def test_sorted_by_score_descending(self, reporter):
        from pheromone_board import PheromoneBoard
        board = PheromoneBoard()
        swarm = self._make_swarm_results([
            ("LOW", 4.0, "bearish"),
            ("HIGH", 9.0, "bullish"),
            ("MID", 6.5, "neutral"),
        ])
        report = reporter._build_swarm_report(swarm, board)
        opps = report["opportunities"]
        scores = [o["opp_score"] for o in opps]
        assert scores == sorted(scores, reverse=True)

    def test_direction_chinese_mapping(self, reporter):
        """direction 应转成中文"""
        from pheromone_board import PheromoneBoard
        board = PheromoneBoard()
        swarm = self._make_swarm_results([
            ("BULL", 8.0, "bullish"),
            ("BEAR", 7.0, "bearish"),
            ("NEU", 5.0, "neutral"),
        ])
        report = reporter._build_swarm_report(swarm, board)
        dirs = {o["ticker"]: o["direction"] for o in report["opportunities"]}
        assert dirs["BULL"] == "看多"
        assert dirs["BEAR"] == "看空"
        assert dirs["NEU"] == "中性"

    def test_metadata_correct(self, reporter):
        from pheromone_board import PheromoneBoard
        board = PheromoneBoard()
        swarm = self._make_swarm_results([("NVDA", 8.0, "bullish"), ("TSLA", 5.0, "neutral")])
        report = reporter._build_swarm_report(swarm, board, agent_count=7)
        meta = report["swarm_metadata"]
        assert meta["total_agents"] == 7
        assert meta["tickers_analyzed"] == 2

    def test_empty_results(self, reporter):
        from pheromone_board import PheromoneBoard
        board = PheromoneBoard()
        report = reporter._build_swarm_report({}, board)
        assert report["opportunities"] == []
        assert report["swarm_metadata"]["tickers_analyzed"] == 0

    def test_neutral_direction(self, reporter):
        from pheromone_board import PheromoneBoard
        board = PheromoneBoard()
        swarm = self._make_swarm_results([("TEST", 5.0, "neutral")])
        report = reporter._build_swarm_report(swarm, board)
        assert report["opportunities"][0]["direction"] == "中性"


# ==================== _deploy_static_to_ghpages 测试 ====================

class TestDeployStaticToGhPages:
    """测试 _deploy_static_to_ghpages 的 git plumbing 逻辑"""

    @pytest.fixture
    def reporter_with_mock_git(self, monkeypatch, tmp_path):
        """创建 reporter + 模拟 git 环境"""
        import alpha_hive_daily_report as mod

        monkeypatch.setattr(mod, "MemoryStore", None)
        monkeypatch.setattr(mod, "CalendarIntegrator", None)
        monkeypatch.setattr(mod, "CodeExecutorAgent", None)
        monkeypatch.setattr(mod, "CODE_EXECUTION_CONFIG", {"enabled": False})
        monkeypatch.setattr(mod, "VectorMemory", None)
        monkeypatch.setattr(mod, "VECTOR_MEMORY_CONFIG", {"enabled": False})
        monkeypatch.setattr(mod, "MetricsCollector", None)
        monkeypatch.setattr(mod, "EarningsWatcher", None)
        monkeypatch.setattr(mod, "SlackReportNotifier", None)
        monkeypatch.setattr(mod, "Backtester", None)

        from alpha_hive_daily_report import AlphaHiveDailyReporter
        r = AlphaHiveDailyReporter()

        # Mock agent_helper.git.repo_path
        r.agent_helper = MagicMock()
        r.agent_helper.git.repo_path = str(tmp_path)

        return r, tmp_path

    def test_no_static_files_skips(self, reporter_with_mock_git):
        """目录中无静态文件 → 日志警告，不调 git"""
        reporter, tmp_path = reporter_with_mock_git
        # tmp_path 是空目录
        with patch("subprocess.check_output") as mock_co, \
             patch("subprocess.run") as mock_run:
            reporter._deploy_static_to_ghpages()
            mock_co.assert_not_called()
            mock_run.assert_not_called()

    def test_deploys_static_files_only(self, reporter_with_mock_git):
        """D2: 白名单核心文件部署，排除 .py 和非核心文件"""
        reporter, tmp_path = reporter_with_mock_git

        # 创建测试文件（白名单核心文件 + 应排除的文件）
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "dashboard-data.json").write_text("{}")
        (tmp_path / "rss.xml").write_text("<rss></rss>")
        (tmp_path / "sw.js").write_text("// sw")
        (tmp_path / "manifest.json").write_text("{}")
        (tmp_path / ".nojekyll").write_text("")
        (tmp_path / "alpha-hive-daily-2026-03-05.json").write_text("{}")
        (tmp_path / "alpha-hive-daily-2026-03-05.md").write_text("# report")
        # 应排除的文件
        (tmp_path / "script.py").write_text("# python")
        (tmp_path / "README.md").write_text("# readme")
        (tmp_path / "realtime_metrics.json").write_text("{}")
        (tmp_path / ".swarm_results_2026-03-05.json").write_text("{}")
        (tmp_path / ".checkpoint_test.json").write_text("{}")

        # 收集 git 命令
        commands = []

        def fake_check_output(cmd, **kw):
            commands.append(cmd)
            if "hash-object" in cmd:
                return b"abc123\n"
            elif "write-tree" in cmd:
                return b"tree456\n"
            elif "rev-parse" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            elif "commit-tree" in cmd:
                return b"commit789\n"
            return b""

        def fake_run(cmd, **kw):
            commands.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess.check_output", side_effect=fake_check_output), \
             patch("subprocess.run", side_effect=fake_run):
            reporter._deploy_static_to_ghpages()

        # 验证 hash-object 只对白名单文件调用
        hash_cmds = [c for c in commands if "hash-object" in c]
        hashed_files = [c[-1] for c in hash_cmds]
        # 核心文件应包含
        assert ".nojekyll" in hashed_files
        assert "index.html" in hashed_files
        assert "dashboard-data.json" in hashed_files
        assert "rss.xml" in hashed_files
        assert "sw.js" in hashed_files
        assert "manifest.json" in hashed_files
        assert "alpha-hive-daily-2026-03-05.json" in hashed_files
        assert "alpha-hive-daily-2026-03-05.md" in hashed_files
        # 非白名单应排除
        assert "script.py" not in hashed_files
        assert "README.md" not in hashed_files
        assert "realtime_metrics.json" not in hashed_files
        assert ".swarm_results_2026-03-05.json" not in hashed_files
        assert ".checkpoint_test.json" not in hashed_files

    def test_nojekyll_included(self, reporter_with_mock_git):
        """.nojekyll 文件应被包含"""
        reporter, tmp_path = reporter_with_mock_git
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / ".nojekyll").write_text("")

        deployed_files = []

        def fake_check_output(cmd, **kw):
            if "hash-object" in cmd:
                deployed_files.append(cmd[-1])
                return b"abc123\n"
            elif "write-tree" in cmd:
                return b"tree456\n"
            elif "rev-parse" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            elif "commit-tree" in cmd:
                return b"commit789\n"
            return b""

        def fake_run(cmd, **kw):
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("subprocess.check_output", side_effect=fake_check_output), \
             patch("subprocess.run", side_effect=fake_run):
            reporter._deploy_static_to_ghpages()

        assert ".nojekyll" in deployed_files

    def test_file_filter_excludes_old_ml_reports(self, reporter_with_mock_git):
        """D2: 超过 3 天的 ML 报告应被排除"""
        reporter, tmp_path = reporter_with_mock_git
        reporter.date_str = "2026-03-05"

        # 核心文件（必须存在否则 files 为空直接返回）
        (tmp_path / "index.html").write_text("<html></html>")
        # 最近的 ML 报告（应包含）
        (tmp_path / "alpha-hive-NVDA-ml-enhanced-2026-03-05.html").write_text("ok")
        (tmp_path / "alpha-hive-NVDA-ml-enhanced-2026-03-03.html").write_text("ok")
        # 过旧的 ML 报告（应排除 — 超过 3 天窗口）
        (tmp_path / "alpha-hive-NVDA-ml-enhanced-2026-03-01.html").write_text("old")
        (tmp_path / "alpha-hive-NVDA-ml-enhanced-2026-02-25.html").write_text("old")

        deployed = []

        def fake_check_output(cmd, **kw):
            if "hash-object" in cmd:
                deployed.append(cmd[-1])
                return b"abc123\n"
            elif "write-tree" in cmd:
                return b"tree456\n"
            elif "rev-parse" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            elif "commit-tree" in cmd:
                return b"commit789\n"
            return b""

        def fake_run(cmd, **kw):
            r = MagicMock(); r.returncode = 0; r.stderr = ""; return r

        with patch("subprocess.check_output", side_effect=fake_check_output), \
             patch("subprocess.run", side_effect=fake_run):
            reporter._deploy_static_to_ghpages()

        assert "alpha-hive-NVDA-ml-enhanced-2026-03-05.html" in deployed
        assert "alpha-hive-NVDA-ml-enhanced-2026-03-03.html" in deployed
        assert "alpha-hive-NVDA-ml-enhanced-2026-03-01.html" not in deployed
        assert "alpha-hive-NVDA-ml-enhanced-2026-02-25.html" not in deployed

    def test_push_retries_on_failure(self, reporter_with_mock_git):
        """D3: push 失败时应重试最多 3 次"""
        reporter, tmp_path = reporter_with_mock_git
        (tmp_path / "index.html").write_text("<html></html>")

        push_calls = []

        def fake_check_output(cmd, **kw):
            if "hash-object" in cmd:
                return b"abc123\n"
            elif "write-tree" in cmd:
                return b"tree456\n"
            elif "rev-parse" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            elif "commit-tree" in cmd:
                return b"commit789\n"
            return b""

        def fake_run(cmd, **kw):
            r = MagicMock()
            if "push" in cmd:
                push_calls.append(cmd)
                # 前 2 次失败，第 3 次成功
                if len(push_calls) < 3:
                    r.returncode = 1
                    r.stderr = "simulated network error"
                else:
                    r.returncode = 0
                    r.stderr = ""
            else:
                r.returncode = 0
                r.stderr = ""
            return r

        with patch("subprocess.check_output", side_effect=fake_check_output), \
             patch("subprocess.run", side_effect=fake_run), \
             patch.object(reporter, "_verify_cdn_deployment", return_value=True):
            reporter._deploy_static_to_ghpages()

        # 应尝试 3 次（2 次失败 + 1 次成功）
        assert len(push_calls) == 3

    def test_push_gives_up_after_max_retries(self, reporter_with_mock_git):
        """D3: push 在 4 次全部失败后放弃"""
        reporter, tmp_path = reporter_with_mock_git
        (tmp_path / "index.html").write_text("<html></html>")

        push_calls = []

        def fake_check_output(cmd, **kw):
            if "hash-object" in cmd:
                return b"abc123\n"
            elif "write-tree" in cmd:
                return b"tree456\n"
            elif "rev-parse" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            elif "commit-tree" in cmd:
                return b"commit789\n"
            return b""

        def fake_run(cmd, **kw):
            r = MagicMock()
            if "push" in cmd:
                push_calls.append(cmd)
                r.returncode = 1
                r.stderr = "permanent failure"
            else:
                r.returncode = 0
                r.stderr = ""
            return r

        with patch("subprocess.check_output", side_effect=fake_check_output), \
             patch("subprocess.run", side_effect=fake_run):
            reporter._deploy_static_to_ghpages()

        # 应尝试 4 次（1 初始 + 3 重试）
        assert len(push_calls) == 4

    def test_sw_no_dashboard_data_in_precache(self, reporter_with_mock_git):
        """D1: sw.js 不应预缓存 dashboard-data.json"""
        reporter, tmp_path = reporter_with_mock_git
        reporter.report_dir = tmp_path

        reporter._write_pwa_files()

        sw_content = (tmp_path / "sw.js").read_text()
        assert "dashboard-data.json" not in sw_content
        # 但仍应包含其他核心文件
        assert "index.html" in sw_content
        assert "manifest.json" in sw_content


# ==================== cleanup 方法测试 ====================

class TestMemoryStoreCleanup:
    """测试 MemoryStore.cleanup_old_data"""

    def test_cleanup_deletes_old_records(self, tmp_path):
        from memory_store import MemoryStore
        store = MemoryStore(db_path=str(tmp_path / "test.db"))

        # 插入一条旧记录和一条新记录
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("""
            INSERT INTO agent_memory (memory_id, session_id, date, ticker, agent_id,
                                      direction, discovery, source, self_score)
            VALUES ('old_1', 's1', '2024-01-01', 'NVDA', 'ScoutBeeNova',
                    'bullish', 'test', 'test', 7.0)
        """)
        conn.execute("""
            INSERT INTO agent_memory (memory_id, session_id, date, ticker, agent_id,
                                      direction, discovery, source, self_score)
            VALUES ('new_1', 's2', '2026-03-01', 'TSLA', 'OracleBeeEcho',
                    'bearish', 'test', 'test', 6.0)
        """)
        conn.commit()
        conn.close()

        deleted = store.cleanup_old_data(retention_days=180)
        assert deleted == 1  # 旧记录被删

        remaining = store.get_recent_memories("TSLA", days=30)
        assert len(remaining) == 1
        assert remaining[0]["memory_id"] == "new_1"

    def test_cleanup_no_records_returns_zero(self, tmp_path):
        from memory_store import MemoryStore
        store = MemoryStore(db_path=str(tmp_path / "test.db"))
        assert store.cleanup_old_data(180) == 0


class TestBacktesterCleanup:
    """测试 Backtester.cleanup_old_predictions"""

    def test_cleanup_deletes_old_predictions(self, tmp_path):
        from backtester import Backtester, PredictionStore
        import sqlite3

        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)

        conn = sqlite3.connect(db)
        conn.execute(f"""
            INSERT INTO {PredictionStore.TABLE} (date, ticker, final_score, direction)
            VALUES ('2024-01-01', 'NVDA', 8.0, 'bullish')
        """)
        conn.execute(f"""
            INSERT INTO {PredictionStore.TABLE} (date, ticker, final_score, direction)
            VALUES ('2026-03-01', 'TSLA', 7.0, 'bearish')
        """)
        conn.commit()
        conn.close()

        deleted = bt.cleanup_old_predictions(days=180)
        assert deleted == 1

        conn = sqlite3.connect(db)
        rows = conn.execute(f"SELECT ticker FROM {PredictionStore.TABLE}").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "TSLA"

    def test_cleanup_empty_table_returns_zero(self, tmp_path):
        from backtester import Backtester
        bt = Backtester(db_path=str(tmp_path / "test.db"))
        assert bt.cleanup_old_predictions(180) == 0
