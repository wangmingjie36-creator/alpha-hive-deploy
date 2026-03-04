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
