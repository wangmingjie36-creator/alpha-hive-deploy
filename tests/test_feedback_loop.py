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
        snap = ReportSnapshot("TSLA", "2026-01-01")
        w = snap.weights_used
        assert abs(w["signal"] - 0.30) < 1e-9
        assert abs(w["catalyst"] - 0.20) < 1e-9
        assert abs(w["sentiment"] - 0.20) < 1e-9
        assert abs(w["odds"] - 0.15) < 1e-9
        assert abs(w["risk_adj"] - 0.15) < 1e-9
        # 总和 = 1.0
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
