"""feedback_loop 模块测试 - ReportSnapshot + BacktestAnalyzer"""

import json
import os
import tempfile
import pytest


class TestReportSnapshot:
    """ReportSnapshot 快照创建 / 保存 / 加载"""

    def test_create_basic(self):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot("AAPL", "2026-03-06")
        assert snap.ticker == "AAPL"
        assert snap.date == "2026-03-06"
        assert snap.report_id == "AAPL_2026-03-06"
        assert snap.composite_score == 0.0
        assert snap.direction == "Neutral"
        assert snap.entry_price == 0.0
        assert snap.actual_price_t1 is None
        assert snap.actual_price_t7 is None
        assert snap.actual_price_t30 is None

    def test_default_weights(self):
        from feedback_loop import ReportSnapshot
        from config import EVALUATION_WEIGHTS as EW
        snap = ReportSnapshot("TSLA", "2026-01-01")
        w = snap.weights_used
        # 契约：weights_used 以 config.EVALUATION_WEIGHTS 为准（会被 weekly_optimizer 自适应调整，
        # 不应硬编码旧教科书默认值 0.30/0.20/…，否则优化器一动测试就红）
        for _k in ("signal", "catalyst", "sentiment", "odds", "risk_adj"):
            assert abs(w[_k] - EW[_k]) < 1e-9
        # 总和 = 1.0（优化器归一化不变式）
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_save_and_load_roundtrip(self):
        from feedback_loop import ReportSnapshot
        with tempfile.TemporaryDirectory() as tmpdir:
            snap = ReportSnapshot("NVDA", "2026-02-15")
            snap.composite_score = 8.5
            snap.direction = "Long"
            snap.entry_price = 150.0
            snap.agent_votes = {"Scout": 8.0, "SentimentBee": 7.5}
            snap.actual_price_t1 = 152.0
            snap.actual_price_t7 = 160.0

            filepath = snap.save_to_json(tmpdir)
            assert os.path.exists(filepath)

            loaded = ReportSnapshot.load_from_json(filepath)
            assert loaded.ticker == "NVDA"
            assert loaded.date == "2026-02-15"
            assert loaded.composite_score == 8.5
            assert loaded.direction == "Long"
            assert loaded.entry_price == 150.0
            assert loaded.agent_votes == {"Scout": 8.0, "SentimentBee": 7.5}
            assert loaded.actual_price_t1 == 152.0
            assert loaded.actual_price_t7 == 160.0
            assert loaded.actual_price_t30 is None

    def test_calculate_returns_long(self):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot("AAPL", "2026-01-01")
        snap.entry_price = 100.0
        snap.actual_price_t1 = 105.0
        snap.actual_price_t7 = 110.0
        snap.actual_price_t30 = 90.0

        returns = snap.calculate_returns()
        assert abs(returns["t1"] - 5.0) < 1e-9
        assert abs(returns["t7"] - 10.0) < 1e-9
        assert abs(returns["t30"] - (-10.0)) < 1e-9

    def test_calculate_returns_empty_when_no_prices(self):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot("AAPL", "2026-01-01")
        snap.entry_price = 100.0
        # actual prices all None
        returns = snap.calculate_returns()
        assert returns == {}

    def test_calculate_returns_no_entry_price(self):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot("AAPL", "2026-01-01")
        snap.entry_price = 0.0
        snap.actual_price_t1 = 105.0
        returns = snap.calculate_returns()
        # entry_price == 0 → falsy → no returns
        assert "t1" not in returns

    def test_direction_accuracy_long_correct(self):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot("AAPL", "2026-01-01")
        snap.entry_price = 100.0
        snap.direction = "Long"
        snap.actual_price_t7 = 110.0  # 上涨 → Long 正确

        acc = snap.check_direction_accuracy()
        assert acc["t7"] is True

    def test_direction_accuracy_long_wrong(self):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot("AAPL", "2026-01-01")
        snap.entry_price = 100.0
        snap.direction = "Long"
        snap.actual_price_t7 = 90.0  # 下跌 → Long 错误

        acc = snap.check_direction_accuracy()
        assert acc["t7"] is False

    def test_direction_accuracy_short(self):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot("AAPL", "2026-01-01")
        snap.entry_price = 100.0
        snap.direction = "Short"
        snap.actual_price_t7 = 90.0  # 下跌 → Short 正确

        acc = snap.check_direction_accuracy()
        assert acc["t7"] is True

    def test_direction_accuracy_neutral(self):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot("AAPL", "2026-01-01")
        snap.entry_price = 100.0
        snap.direction = "Neutral"
        snap.actual_price_t7 = 110.0

        acc = snap.check_direction_accuracy()
        assert acc["t7"] is None  # Neutral 方向不判对错


class TestAgentContributionScoring:
    """v0.42.2 P0：Agent 贡献度记分不得依赖快照整体 direction

    旧实现经由 check_direction_accuracy()，对 Neutral 返回 None，下游
    `not None → True` 让所有 vote<=5 的票无条件判对（与价格无关）。
    实测 625 个快照里 Neutral 占 202 个（32%），系统性压低所有维度准确率。
    """

    def _snap(self, direction, entry, t7, votes):
        from feedback_loop import ReportSnapshot
        s = ReportSnapshot("AAPL", "2026-01-01")
        s.direction = direction
        s.entry_price = entry
        s.actual_price_t7 = t7
        s.agent_votes = votes
        return s

    def test_neutral_snapshot_not_auto_correct_for_low_votes(self):
        """Neutral + 看空票 + 价格大涨 → 必须判错（旧实现会无条件判对）。"""
        from feedback_loop import BacktestAnalyzer
        a = BacktestAnalyzer.__new__(BacktestAnalyzer)
        a.snapshots = [self._snap("Neutral", 100.0, 120.0, {"ScoutBeeNova": 2.0})
                       for _ in range(5)]
        acc = a.calculate_agent_contribution()
        assert acc["ScoutBeeNova"] == 0.0, "看空票遇大涨必须 0% 准确率"

    def test_neutral_snapshot_credits_correct_bullish_votes(self):
        """Neutral + 看多票 + 价格大涨 → 必须判对（旧实现会无条件判错）。"""
        from feedback_loop import BacktestAnalyzer
        a = BacktestAnalyzer.__new__(BacktestAnalyzer)
        a.snapshots = [self._snap("Neutral", 100.0, 120.0, {"ScoutBeeNova": 9.0})
                       for _ in range(5)]
        acc = a.calculate_agent_contribution()
        assert acc["ScoutBeeNova"] == 100.0, "看多票遇大涨必须 100% 准确率"

    def test_scoring_identical_across_directions(self):
        """同一份 votes + 同一价格路径，换 direction 不得改变 Agent 准确率。"""
        from feedback_loop import BacktestAnalyzer
        results = []
        for d in ("Long", "Short", "Neutral"):
            a = BacktestAnalyzer.__new__(BacktestAnalyzer)
            a.snapshots = [self._snap(d, 100.0, 108.0, {"ScoutBeeNova": 8.0})
                           for _ in range(4)]
            results.append(a.calculate_agent_contribution()["ScoutBeeNova"])
        assert len(set(results)) == 1, f"direction 影响了记分: {results}"

    def test_abstain_votes_excluded_from_denominator(self):
        """vote 恰为 5.0 的中性票应弃权，不计入分母。"""
        from feedback_loop import BacktestAnalyzer
        a = BacktestAnalyzer.__new__(BacktestAnalyzer)
        a.snapshots = [self._snap("Long", 100.0, 110.0,
                                  {"ScoutBeeNova": 5.0, "BuzzBeeWhisper": 9.0})
                       for _ in range(3)]
        acc = a.calculate_agent_contribution()
        assert acc["ScoutBeeNova"] == 0.0      # 无有效样本 → 0.0 兜底
        assert acc["BuzzBeeWhisper"] == 100.0  # 看多 + 涨 = 全对


class TestBacktestAnalyzer:
    """BacktestAnalyzer 回测分析"""

    def _make_snapshot(self, ticker, date, direction, entry, t7_price, agent_votes=None):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot(ticker, date)
        snap.direction = direction
        snap.entry_price = entry
        snap.actual_price_t7 = t7_price
        snap.composite_score = 7.0
        snap.agent_votes = agent_votes or {}
        return snap

    def test_empty_directory(self):
        from feedback_loop import BacktestAnalyzer
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzer = BacktestAnalyzer(tmpdir)
            assert analyzer.snapshots == []
            assert analyzer.calculate_accuracy("t7") == {}

    def test_nonexistent_directory(self):
        from feedback_loop import BacktestAnalyzer
        analyzer = BacktestAnalyzer("/tmp/nonexistent_alpha_hive_test_dir_xyz")
        assert analyzer.snapshots == []

    def test_load_and_accuracy(self):
        from feedback_loop import BacktestAnalyzer, ReportSnapshot
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建 3 个快照：2 个正确，1 个错误
            s1 = self._make_snapshot("AAPL", "2026-01-01", "Long", 100.0, 110.0)
            s2 = self._make_snapshot("TSLA", "2026-01-02", "Long", 200.0, 210.0)
            s3 = self._make_snapshot("NVDA", "2026-01-03", "Long", 150.0, 140.0)  # 错误

            s1.save_to_json(tmpdir)
            s2.save_to_json(tmpdir)
            s3.save_to_json(tmpdir)

            analyzer = BacktestAnalyzer(tmpdir)
            assert len(analyzer.snapshots) == 3

            result = analyzer.calculate_accuracy("t7")
            assert result["total_trades"] == 3
            # 2/3 正确 ≈ 66.67%
            assert abs(result["direction_accuracy"] - 66.66666666666667) < 0.01
            assert abs(result["win_rate"] - 66.66666666666667) < 0.01

    def test_suggest_weight_adjustments_normalized(self):
        from feedback_loop import BacktestAnalyzer
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建足够多的快照并带新版 agent_votes
            votes = {
                "ScoutBeeNova": 8.0, "BuzzBeeWhisper": 7.0, "OracleBeeEcho": 6.0,
                "ChronosBeeHorizon": 7.5, "RivalBeeVanguard": 8.5,
                "GuardBeeSentinel": 7.0,
            }
            for i in range(5):
                s = self._make_snapshot(
                    "AAPL", f"2026-01-{i+1:02d}", "Long", 100.0,
                    110.0 if i < 3 else 90.0, agent_votes=votes,
                )
                s.save_to_json(tmpdir)

            analyzer = BacktestAnalyzer(tmpdir)
            result = analyzer.suggest_weight_adjustments()

            assert result, "有 agent_votes 数据时 suggest_weight_adjustments 不应返回空"
            new_w = result.get("new_weights", {})
            assert new_w, "new_weights 不应为空"
            total = sum(new_w.values())
            assert abs(total - 1.0) < 0.01, f"权重总和应为 1.0, 实际 {total}"
            # 5 维都应存在
            for dim in ("signal", "catalyst", "sentiment", "odds", "risk_adj"):
                assert dim in new_w, f"缺少维度 {dim}"

    def test_suggest_weight_adjustments_legacy_agent_names(self):
        """旧版 agent_votes 名称（兼容性测试）"""
        from feedback_loop import BacktestAnalyzer
        with tempfile.TemporaryDirectory() as tmpdir:
            # 使用旧版名称，验证兼容映射
            legacy_votes = {
                "Scout": 8.0, "SentimentBee": 7.0, "OddsBee": 6.0,
                "CatalystBee": 7.5, "CrossBee": 8.5, "ValidatorBee": 7.0,
            }
            for i in range(5):
                s = self._make_snapshot(
                    "TSLA", f"2026-02-{i+1:02d}", "Long", 200.0,
                    220.0 if i < 3 else 180.0, agent_votes=legacy_votes,
                )
                s.save_to_json(tmpdir)

            analyzer = BacktestAnalyzer(tmpdir)
            result = analyzer.suggest_weight_adjustments()

            assert result, "旧名称通过兼容映射后也应产生有效结果"
            new_w = result.get("new_weights", {})
            assert new_w, "旧名称映射后 new_weights 不应为空"
            total = sum(new_w.values())
            assert abs(total - 1.0) < 0.01, f"权重总和应为 1.0, 实际 {total}"

    def test_calculate_sharpe_edge_cases(self):
        from feedback_loop import BacktestAnalyzer
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzer = BacktestAnalyzer(tmpdir)

            # 样本 < 2 → 返回 0.0
            assert analyzer._calculate_sharpe([1], 5.0) == 0.0

            # 全同值 → 返回 0.0
            assert analyzer._calculate_sharpe([1, 1, 1], 5.0) == 0.0

    def test_get_snapshots_by_ticker(self):
        from feedback_loop import BacktestAnalyzer
        with tempfile.TemporaryDirectory() as tmpdir:
            s1 = self._make_snapshot("AAPL", "2026-01-01", "Long", 100.0, 110.0)
            s2 = self._make_snapshot("TSLA", "2026-01-02", "Short", 200.0, 190.0)
            s3 = self._make_snapshot("AAPL", "2026-01-03", "Long", 105.0, 115.0)

            s1.save_to_json(tmpdir)
            s2.save_to_json(tmpdir)
            s3.save_to_json(tmpdir)

            analyzer = BacktestAnalyzer(tmpdir)
            aapl = analyzer.get_snapshots_by_ticker("AAPL")
            assert len(aapl) == 2
            assert all(s.ticker == "AAPL" for s in aapl)


class TestBacktestAnalyzerCleanT7:
    """BacktestAnalyzer(clean_t7=True) —— close_t7 干净口径共用实现（v0.45.87）

    挪自 weekly_optimizer.py 的 _load_close_t7_map/_apply_clean_t7_prices，
    现在是 generate_deep_v2.py / alpha_hive_daily_report.py /
    swarm_agents/queen_distiller.py 与 weekly_optimizer.py 共用的唯一实现。
    clean_t7 默认 False，必须验证默认行为与此前完全一致（不静默改变既有
    三处生产调用点的行为），以及 True 时的覆盖/丢弃两条分支。
    """

    def _make_snapshot(self, ticker, date, direction, entry, t7_price):
        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot(ticker, date)
        snap.direction = direction
        snap.entry_price = entry
        snap.actual_price_t7 = t7_price
        snap.composite_score = 7.0
        return snap

    def _make_pheromone_db(self, tmp_path, rows):
        import sqlite3
        db_path = tmp_path / "pheromone.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE predictions (ticker TEXT, date TEXT, close_t7 REAL)")
        con.executemany("INSERT INTO predictions VALUES (?, ?, ?)", rows)
        con.commit()
        con.close()
        return db_path

    def test_clean_t7_false_keeps_dirty_value(self, tmp_path):
        """默认 clean_t7=False：行为必须与挪家前完全一致，不做任何覆盖。"""
        from feedback_loop import BacktestAnalyzer
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        s = self._make_snapshot("AAA", "2026-08-14", "Long", 100.0, 999.0)
        s.save_to_json(str(snapshots_dir))

        db_path = self._make_pheromone_db(tmp_path, [("AAA", "2026-08-14", 123.45)])

        analyzer = BacktestAnalyzer(directory=str(snapshots_dir),
                                    close_t7_db_path=db_path)  # clean_t7 缺省 False
        assert analyzer.snapshots[0].actual_price_t7 == 999.0, \
            "clean_t7=False 时即便传了 close_t7_db_path 也不该生效"

    def test_clean_t7_true_overrides_with_matching_row(self, tmp_path):
        from feedback_loop import BacktestAnalyzer
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        s = self._make_snapshot("AAA", "2026-08-14", "Long", 100.0, 999.0)  # 脏值
        s.save_to_json(str(snapshots_dir))

        db_path = self._make_pheromone_db(tmp_path, [("AAA", "2026-08-14", 123.45)])

        analyzer = BacktestAnalyzer(directory=str(snapshots_dir), clean_t7=True,
                                    close_t7_db_path=db_path)
        assert analyzer.snapshots[0].actual_price_t7 == 123.45

    def test_clean_t7_true_drops_sample_without_matching_row(self, tmp_path):
        from feedback_loop import BacktestAnalyzer
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        s = self._make_snapshot("ZZZ", "2026-08-14", "Long", 100.0, 999.0)
        s.save_to_json(str(snapshots_dir))

        db_path = self._make_pheromone_db(tmp_path, [("AAA", "2026-08-14", 123.45)])

        analyzer = BacktestAnalyzer(directory=str(snapshots_dir), clean_t7=True,
                                    close_t7_db_path=db_path)
        assert analyzer.snapshots[0].actual_price_t7 is None

    def test_clean_t7_true_unchanged_when_db_missing(self, tmp_path):
        """库不存在：原样返回，不能把消费者的全部样本清零。"""
        from feedback_loop import BacktestAnalyzer
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        s = self._make_snapshot("AAA", "2026-08-14", "Long", 100.0, 999.0)
        s.save_to_json(str(snapshots_dir))

        analyzer = BacktestAnalyzer(directory=str(snapshots_dir), clean_t7=True,
                                    close_t7_db_path=tmp_path / "_absent.db")
        assert analyzer.snapshots[0].actual_price_t7 == 999.0

    def test_clean_t7_true_defaults_to_isolated_db_in_tests(self, tmp_path):
        """不显式传 close_t7_db_path 时用模块默认值；测试环境下
        conftest 的 _isolate_feedback_loop_close_t7_db 会把它指向不存在的
        路径，所以这里也应该保留脏值——防止这条路径意外打到真实生产库。
        """
        from feedback_loop import BacktestAnalyzer
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        s = self._make_snapshot("AAA", "2026-08-14", "Long", 100.0, 999.0)
        s.save_to_json(str(snapshots_dir))

        analyzer = BacktestAnalyzer(directory=str(snapshots_dir), clean_t7=True)
        assert analyzer.snapshots[0].actual_price_t7 == 999.0
