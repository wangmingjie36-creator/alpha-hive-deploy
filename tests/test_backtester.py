"""
tests/test_backtester.py — PredictionStore + Backtester 单元测试

覆盖：建表/索引/迁移、保存预测、待回测查询、更新结果、准确率统计、清理
"""

import sqlite3
import json
import pytest
from datetime import datetime, timedelta


# ==================== PredictionStore 测试 ====================

class TestPredictionStoreInit:
    """测试 PredictionStore 初始化和迁移"""

    def test_creates_table(self, tmp_path):
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        with sqlite3.connect(db) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        assert ("predictions",) in tables

    def test_creates_indexes(self, tmp_path):
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        PredictionStore(db_path=db)
        with sqlite3.connect(db) as conn:
            indexes = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()]
        assert "idx_pred_date" in indexes
        assert "idx_pred_ticker" in indexes

    def test_migrate_options_columns_idempotent(self, tmp_path):
        """多次初始化不报错（列已存在时 ALTER TABLE 被忽略）"""
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        PredictionStore(db_path=db)
        PredictionStore(db_path=db)  # 第二次不应报错


class TestSavePrediction:
    """测试 PredictionStore.save_prediction"""

    @pytest.fixture
    def store(self, tmp_path):
        from backtester import PredictionStore
        return PredictionStore(db_path=str(tmp_path / "test.db"))

    def test_save_basic(self, store):
        ok = store.save_prediction(
            ticker="NVDA", final_score=8.5, direction="bullish", price=142.0
        )
        assert ok is True
        rows = store.get_all_predictions(days=1)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "NVDA"
        assert rows[0]["final_score"] == 8.5
        assert rows[0]["direction"] == "bullish"

    def test_save_with_options_data(self, store):
        ok = store.save_prediction(
            ticker="TSLA", final_score=7.0, direction="bearish", price=340.0,
            options_data={"options_score": 6.5, "iv_rank": 0.72, "put_call_ratio": 1.3}
        )
        assert ok is True
        rows = store.get_all_predictions(days=1)
        assert rows[0]["options_score"] == 6.5
        assert rows[0]["iv_rank"] == 0.72

    def test_save_with_dimension_scores(self, store):
        dims = {"signal": 8.0, "catalyst": 7.0, "sentiment": 6.5}
        ok = store.save_prediction(
            ticker="AMD", final_score=7.5, direction="bullish", price=160.0,
            dimension_scores=dims
        )
        assert ok is True
        rows = store.get_all_predictions(days=1)
        parsed = json.loads(rows[0]["dimension_scores"])
        assert parsed["signal"] == 8.0

    def test_save_with_agent_directions(self, store):
        dirs = {"ScoutBeeNova": "bullish", "OracleBeeEcho": "neutral"}
        ok = store.save_prediction(
            ticker="MSFT", final_score=6.0, direction="neutral", price=420.0,
            agent_directions=dirs
        )
        assert ok is True
        rows = store.get_all_predictions(days=1)
        parsed = json.loads(rows[0]["agent_directions"])
        assert parsed["ScoutBeeNova"] == "bullish"

    def test_upsert_same_date_ticker(self, store):
        """同一天同一标的，第二次 save 覆盖第一次"""
        store.save_prediction(ticker="NVDA", final_score=7.0, direction="bullish", price=140.0)
        store.save_prediction(ticker="NVDA", final_score=9.0, direction="bearish", price=145.0)
        rows = store.get_all_predictions(days=1)
        nvda = [r for r in rows if r["ticker"] == "NVDA"]
        assert len(nvda) == 1
        assert nvda[0]["final_score"] == 9.0

    def test_save_with_pheromone_compact(self, store):
        compact = [{"agent": "ScoutBeeNova", "score": 8.0}]
        ok = store.save_prediction(
            ticker="VKTX", final_score=8.0, direction="bullish", price=48.0,
            pheromone_compact=compact
        )
        assert ok is True
        rows = store.get_all_predictions(days=1)
        parsed = json.loads(rows[0]["pheromone_compact"])
        assert parsed[0]["agent"] == "ScoutBeeNova"


class TestGetPendingChecks:
    """测试 PredictionStore.get_pending_checks"""

    @pytest.fixture
    def store_with_old_prediction(self, tmp_path):
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        # 插入一条 30 天前的预测
        old_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        with sqlite3.connect(db) as conn:
            conn.execute(f"""
                INSERT INTO {ps.TABLE} (date, ticker, final_score, direction, price_at_predict)
                VALUES (?, 'NVDA', 8.0, 'bullish', 140.0)
            """, (old_date,))
            conn.commit()
        return ps

    def test_returns_unchecked_old_predictions(self, store_with_old_prediction):
        pending = store_with_old_prediction.get_pending_checks("t7")
        assert len(pending) == 1
        assert pending[0]["ticker"] == "NVDA"

    def test_excludes_already_checked(self, store_with_old_prediction):
        # 标记为已检查
        db = store_with_old_prediction.db_path
        with sqlite3.connect(db) as conn:
            conn.execute(f"UPDATE {store_with_old_prediction.TABLE} SET checked_t7 = 1")
            conn.commit()
        pending = store_with_old_prediction.get_pending_checks("t7")
        assert len(pending) == 0

    def test_empty_table_returns_empty(self, tmp_path):
        from backtester import PredictionStore
        ps = PredictionStore(db_path=str(tmp_path / "test.db"))
        assert ps.get_pending_checks("t7") == []

    def test_today_prediction_not_pending(self, tmp_path):
        """今天的预测不应出现在待检查列表中"""
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        ps.save_prediction(ticker="AAPL", final_score=7.0, direction="bullish", price=180.0)
        pending = ps.get_pending_checks("t1")
        # 今天的预测需要至少 T+1 才会被检查
        tickers = [p["ticker"] for p in pending]
        assert "AAPL" not in tickers


class TestUpdateCheckResult:
    """测试 PredictionStore.update_check_result"""

    @pytest.fixture
    def store_with_prediction(self, tmp_path):
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        with sqlite3.connect(db) as conn:
            conn.execute(f"""
                INSERT INTO {ps.TABLE} (date, ticker, final_score, direction, price_at_predict)
                VALUES (?, 'TSLA', 7.0, 'bearish', 340.0)
            """, (old_date,))
            conn.commit()
        return ps

    def test_updates_price_and_return(self, store_with_prediction):
        ok = store_with_prediction.update_check_result(
            pred_id=1, period="t7", price=320.0, ret=-5.88, correct=True
        )
        assert ok is True
        with sqlite3.connect(store_with_prediction.db_path) as conn:
            row = conn.execute(f"SELECT * FROM {store_with_prediction.TABLE} WHERE id=1").fetchone()
        # price_t7 是第 17 列（0-indexed），但用 row_factory 更安全
        conn2 = sqlite3.connect(store_with_prediction.db_path)
        conn2.row_factory = sqlite3.Row
        row = conn2.execute(f"SELECT * FROM {store_with_prediction.TABLE} WHERE id=1").fetchone()
        conn2.close()
        assert row["price_t7"] == 320.0
        assert row["return_t7"] == -5.88
        assert row["checked_t7"] == 1

    def test_marks_correct(self, store_with_prediction):
        store_with_prediction.update_check_result(
            pred_id=1, period="t7", price=320.0, ret=-5.88, correct=True
        )
        conn = sqlite3.connect(store_with_prediction.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT correct_t7 FROM {store_with_prediction.TABLE} WHERE id=1").fetchone()
        conn.close()
        assert row["correct_t7"] == 1

    def test_marks_incorrect(self, store_with_prediction):
        store_with_prediction.update_check_result(
            pred_id=1, period="t7", price=360.0, ret=5.88, correct=False
        )
        conn = sqlite3.connect(store_with_prediction.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT correct_t7 FROM {store_with_prediction.TABLE} WHERE id=1").fetchone()
        conn.close()
        assert row["correct_t7"] == 0


class TestGetAccuracyStats:
    """测试 PredictionStore.get_accuracy_stats"""

    @pytest.fixture
    def store_with_checked(self, tmp_path):
        """插入几条已回测的预测"""
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        with sqlite3.connect(db) as conn:
            # 3 条预测：2 正确 1 错误
            conn.execute(f"""
                INSERT INTO {ps.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 checked_t7, correct_t7, return_t7)
                VALUES (?, 'NVDA', 8.5, 'bullish', 140.0, 1, 1, 5.0)
            """, (recent,))
            conn.execute(f"""
                INSERT INTO {ps.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 checked_t7, correct_t7, return_t7)
                VALUES (?, 'TSLA', 6.0, 'bearish', 340.0, 1, 1, -3.0)
            """, (recent,))
            conn.execute(f"""
                INSERT INTO {ps.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 checked_t7, correct_t7, return_t7)
                VALUES (?, 'AMD', 7.0, 'bullish', 160.0, 1, 0, -2.0)
            """, (recent,))
            conn.commit()
        return ps

    def test_empty_table_returns_zero(self, tmp_path):
        from backtester import PredictionStore
        ps = PredictionStore(db_path=str(tmp_path / "test.db"))
        stats = ps.get_accuracy_stats("t7")
        assert stats["total_checked"] == 0
        assert stats["overall_accuracy"] == 0

    def test_basic_accuracy(self, store_with_checked):
        stats = store_with_checked.get_accuracy_stats("t7")
        assert stats["total_checked"] == 3
        assert stats["correct_count"] == 2
        assert abs(stats["overall_accuracy"] - 0.667) < 0.01

    def test_by_direction_breakdown(self, store_with_checked):
        stats = store_with_checked.get_accuracy_stats("t7")
        by_dir = stats["by_direction"]
        assert by_dir["bullish"]["total"] == 2
        assert by_dir["bullish"]["correct"] == 1
        assert by_dir["bearish"]["total"] == 1
        assert by_dir["bearish"]["correct"] == 1

    def test_by_ticker_breakdown(self, store_with_checked):
        stats = store_with_checked.get_accuracy_stats("t7")
        by_tk = stats["by_ticker"]
        assert "NVDA" in by_tk
        assert "TSLA" in by_tk
        assert by_tk["NVDA"]["accuracy"] == 1.0
        assert by_tk["AMD"]["accuracy"] == 0.0

    def test_days_window_filtering(self, tmp_path):
        """超出时间窗的记录不计入统计"""
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        old_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        with sqlite3.connect(db) as conn:
            conn.execute(f"""
                INSERT INTO {ps.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 checked_t7, correct_t7, return_t7)
                VALUES (?, 'OLD', 5.0, 'neutral', 100.0, 1, 1, 1.0)
            """, (old_date,))
            conn.commit()
        stats = ps.get_accuracy_stats("t7", days=90)
        assert stats["total_checked"] == 0


class TestGetAllPredictions:
    """测试 PredictionStore.get_all_predictions"""

    def test_returns_recent(self, tmp_path):
        from backtester import PredictionStore
        ps = PredictionStore(db_path=str(tmp_path / "test.db"))
        ps.save_prediction(ticker="NVDA", final_score=8.0, direction="bullish", price=140.0)
        ps.save_prediction(ticker="TSLA", final_score=6.0, direction="bearish", price=340.0)
        rows = ps.get_all_predictions(days=1)
        assert len(rows) == 2

    def test_empty_returns_empty(self, tmp_path):
        from backtester import PredictionStore
        ps = PredictionStore(db_path=str(tmp_path / "test.db"))
        assert ps.get_all_predictions() == []


# ==================== Backtester 高层测试 ====================

class TestBacktesterCleanupPredictions:
    """测试 Backtester.cleanup_old_predictions（来自 Round 2）"""

    def test_cleanup_removes_old(self, tmp_path):
        from backtester import Backtester, PredictionStore
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)
        old_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        with sqlite3.connect(db) as conn:
            conn.execute(f"""
                INSERT INTO {PredictionStore.TABLE}
                (date, ticker, final_score, direction)
                VALUES (?, 'OLD', 5.0, 'neutral')
            """, (old_date,))
            conn.commit()
        deleted = bt.cleanup_old_predictions(days=180)
        assert deleted == 1

    def test_cleanup_keeps_recent(self, tmp_path):
        from backtester import Backtester
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)
        bt.store.save_prediction(ticker="NEW", final_score=8.0, direction="bullish", price=100.0)
        deleted = bt.cleanup_old_predictions(days=180)
        assert deleted == 0
        assert len(bt.store.get_all_predictions(days=1)) == 1


# ==================== PredictionStore.get_dimension_accuracy 测试 ====================

class TestGetDimensionAccuracy:
    """测试 PredictionStore.get_dimension_accuracy"""

    def test_no_checked_predictions_returns_zero_samples(self, tmp_path):
        """没有已回测预测时，所有维度的样本数应为 0"""
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        result = ps.get_dimension_accuracy("t7", days=90)
        # 应返回 5 个维度，每个维度样本为 0
        expected_dims = {"signal", "catalyst", "sentiment", "odds", "risk_adj"}
        assert set(result.keys()) == expected_dims
        for dim in expected_dims:
            assert result[dim]["samples"] == 0

    def test_with_agent_directions_data(self, tmp_path):
        """有 agent_directions JSON 的已回测记录时，维度应有数据"""
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

        agent_dirs = json.dumps({
            "ScoutBeeNova": "bullish",
            "OracleBeeEcho": "bullish",
            "BuzzBeeWhisper": "bearish",
            "ChronosBeeHorizon": "neutral",
            "GuardBeeSentinel": "bullish",
        })
        with sqlite3.connect(db) as conn:
            # 插入两条已回测记录，return_t7 > 0（上涨）
            conn.execute(f"""
                INSERT INTO {ps.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 agent_directions, checked_t7, correct_t7, return_t7)
                VALUES (?, 'NVDA', 8.5, 'bullish', 140.0, ?, 1, 1, 5.0)
            """, (recent, agent_dirs))
            conn.execute(f"""
                INSERT INTO {ps.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 agent_directions, checked_t7, correct_t7, return_t7)
                VALUES (?, 'TSLA', 6.0, 'bearish', 340.0, ?, 1, 0, -3.0)
            """, (recent, agent_dirs))
            conn.commit()

        result = ps.get_dimension_accuracy("t7", days=90)
        # signal 维度来自 ScoutBeeNova，两条记录都有数据
        assert result["signal"]["samples"] == 2
        assert result["odds"]["samples"] == 2
        assert result["sentiment"]["samples"] == 2
        assert result["catalyst"]["samples"] == 2
        assert result["risk_adj"]["samples"] == 2

    def test_accuracy_calculation_bullish_correct(self, tmp_path):
        """bullish 方向 + 正收益(>-1%) 应算正确"""
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        agent_dirs = json.dumps({"ScoutBeeNova": "bullish"})
        with sqlite3.connect(db) as conn:
            conn.execute(f"""
                INSERT INTO {ps.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 agent_directions, checked_t7, correct_t7, return_t7)
                VALUES (?, 'AAPL', 7.0, 'bullish', 180.0, ?, 1, 1, 3.0)
            """, (recent, agent_dirs))
            conn.commit()

        result = ps.get_dimension_accuracy("t7", days=90)
        assert result["signal"]["samples"] == 1
        assert result["signal"]["correct"] == 1
        assert result["signal"]["accuracy"] == 1.0

    def test_suggested_weights_present_when_data_exists(self, tmp_path):
        """有数据时，结果中应包含 suggested_weight"""
        from backtester import PredictionStore
        db = str(tmp_path / "test.db")
        ps = PredictionStore(db_path=db)
        recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        agent_dirs = json.dumps({
            "ScoutBeeNova": "bullish",
            "OracleBeeEcho": "bullish",
            "BuzzBeeWhisper": "bullish",
            "ChronosBeeHorizon": "bullish",
            "GuardBeeSentinel": "bullish",
        })
        with sqlite3.connect(db) as conn:
            conn.execute(f"""
                INSERT INTO {ps.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 agent_directions, checked_t7, correct_t7, return_t7)
                VALUES (?, 'NVDA', 8.5, 'bullish', 140.0, ?, 1, 1, 5.0)
            """, (recent, agent_dirs))
            conn.commit()

        result = ps.get_dimension_accuracy("t7", days=90)
        for dim in result:
            assert "suggested_weight" in result[dim]


# ==================== Backtester.analyze_self_score_bias 测试 ====================

class TestAnalyzeSelfScoreBias:
    """测试 Backtester.analyze_self_score_bias"""

    def test_no_data_returns_all_zero(self, tmp_path):
        """没有数据时，所有 Agent 偏差应为 0.0"""
        from backtester import Backtester
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)
        result = bt.analyze_self_score_bias(period="t1", min_samples=5)
        # 所有值应为 0.0
        for key, val in result.items():
            assert val == 0.0

    def test_expected_agent_abbreviation_keys(self, tmp_path):
        """返回的 dict 应包含预期的 8 字符缩写 key"""
        from backtester import Backtester
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)
        result = bt.analyze_self_score_bias(period="t7", min_samples=5)
        expected_keys = {"ScoutBee", "OracleBe", "BuzzBeeW", "ChronosB", "GuardBee", "RivalBee"}
        assert set(result.keys()) == expected_keys

    def test_with_sufficient_samples_detects_bias(self, tmp_path):
        """有足够数据时，应能检测出 self_score 偏差"""
        from backtester import Backtester, PredictionStore
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)
        recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

        with sqlite3.connect(db) as conn:
            # 插入 6 条记录：3 correct + 3 wrong
            # ScoutBee correct 时 self_score=5，wrong 时 self_score=9 → 正偏差（乐观）
            for i in range(3):
                compact = json.dumps([{"a": "ScoutBee", "s": 5.0}])
                conn.execute(f"""
                    INSERT INTO {PredictionStore.TABLE}
                    (date, ticker, final_score, direction, price_at_predict,
                     pheromone_compact, checked_t7, correct_t7, return_t7)
                    VALUES (?, ?, 7.0, 'bullish', 100.0, ?, 1, 1, 3.0)
                """, (recent, f"T{i}C", compact))
            for i in range(3):
                compact = json.dumps([{"a": "ScoutBee", "s": 9.0}])
                conn.execute(f"""
                    INSERT INTO {PredictionStore.TABLE}
                    (date, ticker, final_score, direction, price_at_predict,
                     pheromone_compact, checked_t7, correct_t7, return_t7)
                    VALUES (?, ?, 7.0, 'bearish', 100.0, ?, 1, 0, -5.0)
                """, (recent, f"T{i}W", compact))
            conn.commit()

        result = bt.analyze_self_score_bias(period="t7", min_samples=3)
        # wrong 均值(9.0) - correct 均值(5.0) = 4.0 → 正偏差
        assert result["ScoutBee"] == 4.0


# ==================== Backtester.adapt_weights 测试 ====================

class TestAdaptWeights:
    """测试 Backtester.adapt_weights"""

    def test_insufficient_samples_returns_none(self, tmp_path):
        """样本不足时应返回 None"""
        from backtester import Backtester
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)
        result = bt.adapt_weights(min_samples=10, period="t7")
        assert result is None

    def test_sufficient_data_returns_five_dimensions(self, tmp_path):
        """有足够数据时，应返回包含 5 个维度 key 的 dict"""
        from backtester import Backtester, PredictionStore
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)
        recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

        agent_dirs = json.dumps({
            "ScoutBeeNova": "bullish",
            "OracleBeeEcho": "bullish",
            "BuzzBeeWhisper": "bullish",
            "ChronosBeeHorizon": "bullish",
            "GuardBeeSentinel": "bullish",
        })
        with sqlite3.connect(db) as conn:
            # 插入 12 条记录（确保超过 min_samples=10），全部 bullish+正收益
            for i in range(12):
                conn.execute(f"""
                    INSERT INTO {PredictionStore.TABLE}
                    (date, ticker, final_score, direction, price_at_predict,
                     agent_directions, checked_t7, correct_t7, return_t7)
                    VALUES (?, ?, 8.0, 'bullish', 100.0, ?, 1, 1, 5.0)
                """, (recent, f"TK{i:02d}", agent_dirs))
            conn.commit()

        result = bt.adapt_weights(min_samples=10, period="t7")
        assert result is not None
        expected_dims = {"signal", "catalyst", "sentiment", "odds", "risk_adj"}
        assert set(result.keys()) == expected_dims

    def test_weights_sum_to_one(self, tmp_path):
        """返回的权重之和应约等于 1.0"""
        from backtester import Backtester, PredictionStore
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)
        recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

        agent_dirs = json.dumps({
            "ScoutBeeNova": "bullish",
            "OracleBeeEcho": "bearish",
            "BuzzBeeWhisper": "neutral",
            "ChronosBeeHorizon": "bullish",
            "GuardBeeSentinel": "bearish",
        })
        with sqlite3.connect(db) as conn:
            for i in range(15):
                ret = 3.0 if i % 2 == 0 else -3.0
                correct = 1 if i % 2 == 0 else 0
                conn.execute(f"""
                    INSERT INTO {PredictionStore.TABLE}
                    (date, ticker, final_score, direction, price_at_predict,
                     agent_directions, checked_t7, correct_t7, return_t7)
                    VALUES (?, ?, 7.0, 'bullish', 100.0, ?, 1, ?, ?)
                """, (recent, f"TK{i:02d}", agent_dirs, correct, ret))
            conn.commit()

        result = bt.adapt_weights(min_samples=10, period="t7")
        assert result is not None
        total = sum(result.values())
        assert abs(total - 1.0) < 0.01, f"Weights sum to {total}, expected ~1.0"


# ==================== Backtester.load_adapted_weights 测试 ====================

class TestLoadAdaptedWeights:
    """测试 Backtester.load_adapted_weights（静态方法）"""

    def test_no_saved_weights_returns_none(self, tmp_path):
        """没有保存过权重时应返回 None"""
        from backtester import Backtester, PredictionStore
        db = str(tmp_path / "test.db")
        # 先初始化 PredictionStore 确保数据库存在
        PredictionStore(db_path=db)
        result = Backtester.load_adapted_weights(db_path=db)
        assert result is None

    def test_save_then_load_roundtrip(self, tmp_path):
        """保存权重后应能正确加载"""
        from backtester import Backtester, PredictionStore
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)

        weights = {"signal": 0.28, "catalyst": 0.22, "sentiment": 0.20, "odds": 0.16, "risk_adj": 0.14}
        accuracy = {"signal": 0.75, "catalyst": 0.60, "sentiment": 0.55, "odds": 0.50, "risk_adj": 0.65}
        bt._save_adapted_weights(weights, accuracy, samples=25, period="t7")

        loaded = Backtester.load_adapted_weights(db_path=db)
        assert loaded is not None
        assert loaded["signal"] == 0.28
        assert loaded["catalyst"] == 0.22
        assert loaded["sentiment"] == 0.20
        assert loaded["odds"] == 0.16
        assert loaded["risk_adj"] == 0.14

    def test_load_prefers_t7_over_t1(self, tmp_path):
        """存在 T+7 和 T+1 权重时，应优先加载 T+7"""
        from backtester import Backtester
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)

        weights_t1 = {"signal": 0.10, "catalyst": 0.20, "sentiment": 0.30, "odds": 0.25, "risk_adj": 0.15}
        weights_t7 = {"signal": 0.35, "catalyst": 0.18, "sentiment": 0.22, "odds": 0.13, "risk_adj": 0.12}

        bt._save_adapted_weights(weights_t1, {}, samples=10, period="t1")
        bt._save_adapted_weights(weights_t7, {}, samples=20, period="t7")

        loaded = Backtester.load_adapted_weights(db_path=db)
        assert loaded is not None
        # 应优先加载 T+7 的权重
        assert loaded["signal"] == 0.35

    def test_load_requires_min_sample_count(self, tmp_path):
        """sample_count < 3 的权重不应被加载"""
        from backtester import Backtester
        db = str(tmp_path / "test.db")
        bt = Backtester(db_path=db)

        weights = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.15, "risk_adj": 0.15}
        bt._save_adapted_weights(weights, {}, samples=2, period="t7")

        loaded = Backtester.load_adapted_weights(db_path=db)
        assert loaded is None
