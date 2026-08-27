"""
Tests for ML 真实数据训练 - build_training_data_from_db + train_model 改造
"""

import json
import os
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from ml_predictor import build_training_data_from_db, MLPredictionService, TrainingData


# ==================== Fixtures ====================

@pytest.fixture
def predictions_db(tmp_path):
    """创建带有 predictions 表的临时 SQLite 数据库"""
    db_path = str(tmp_path / "test_backtester.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE predictions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            date               TEXT NOT NULL,
            ticker             TEXT NOT NULL,
            final_score        REAL NOT NULL,
            direction          TEXT NOT NULL,
            price_at_predict   REAL,
            dimension_scores   TEXT,
            agent_directions   TEXT,
            options_score      REAL,
            iv_rank            REAL,
            put_call_ratio     REAL,
            gamma_exposure     REAL,
            flow_direction     TEXT,
            price_t1           REAL,
            return_t1          REAL,
            correct_t1         INTEGER,
            checked_t1         INTEGER DEFAULT 0,
            iv_rank_t1         REAL,
            price_t7           REAL,
            return_t7          REAL,
            correct_t7         INTEGER,
            checked_t7         INTEGER DEFAULT 0,
            price_t30          REAL,
            return_t30         REAL,
            correct_t30        INTEGER,
            checked_t30        INTEGER DEFAULT 0,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, ticker)
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def populated_db(predictions_db):
    """填充 40 条已验证的 T+7 数据"""
    conn = sqlite3.connect(predictions_db)
    for i in range(40):
        date = f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
        ticker = ["AAPL", "NVDA", "TSLA", "MSFT", "GOOG"][i % 5]
        direction = "bullish" if i % 3 != 0 else "bearish"
        ret = 0.05 if i % 2 == 0 else -0.03
        correct = 1 if (direction == "bullish" and ret > 0) or (direction == "bearish" and ret < 0) else 0

        dim_scores = json.dumps({
            "signal": 5.0 + (i % 5),
            "catalyst": 6.0 + (i % 4),
            "sentiment": 4.0 + (i % 6),
            "odds": 5.0 + (i % 3),
            "risk_adj": 5.5 + (i % 3),
        })
        agent_dirs = json.dumps({
            "ScoutBeeNova": direction,
            "OracleBeeEcho": direction,
            "BuzzBeeWhisper": "neutral" if i % 4 == 0 else direction,
        })

        conn.execute(
            "INSERT INTO predictions "
            "(date, ticker, final_score, direction, dimension_scores, agent_directions, "
            " iv_rank, put_call_ratio, return_t7, correct_t7, checked_t7) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (date, ticker, 7.0 + (i % 3), direction, dim_scores, agent_dirs,
             50 + i, 0.8 + (i * 0.01), ret, correct),
        )
    conn.commit()
    conn.close()
    return predictions_db


# ==================== TestBuildTrainingDataFromDB ====================

class TestBuildTrainingDataFromDB:
    """build_training_data_from_db 函数测试"""

    def test_returns_empty_below_threshold(self, predictions_db):
        """< 30 条 → 空列表"""
        # 插入 5 条数据（不足 30）
        conn = sqlite3.connect(predictions_db)
        for i in range(5):
            conn.execute(
                "INSERT INTO predictions (date, ticker, final_score, direction, "
                "return_t7, checked_t7) VALUES (?, ?, ?, ?, ?, 1)",
                (f"2025-01-{i+1:02d}", "AAPL", 7.0, "bullish", 0.05),
            )
        conn.commit()
        conn.close()

        result = build_training_data_from_db(db_path=predictions_db, min_samples=30)
        assert result == []

    def test_converts_predictions_to_training_data(self, populated_db):
        """40 条数据 → 字段映射正确"""
        result = build_training_data_from_db(db_path=populated_db, min_samples=30)
        assert len(result) == 40

        # 验证类型
        for td in result:
            assert isinstance(td, TrainingData)
            assert isinstance(td.ticker, str)
            assert isinstance(td.actual_return_7d, float)
            assert isinstance(td.win_7d, bool)
            assert 0 <= td.agent_agreement <= 1.0

    def test_bearish_negative_return_is_win(self, predictions_db):
        """回归测试: bearish 预测 + 负收益 → win_7d=True（方向正确）"""
        conn = sqlite3.connect(predictions_db)
        for i in range(35):
            direction = "bearish"
            ret = -0.05  # 看空，实际下跌 → 预测正确
            correct = 1   # backtester 标记为正确
            # v0.45.50：补齐 dimension_scores —— 本测试测的是 bearish/win_7d 逻辑，
            # 维度缺失是夹具的偶然属性。缺维度的样本现在会被剔除（不再补 5.0 伪造），
            # 夹具若不带维度就一条都进不来，测不到它真正想测的东西。
            _dims = json.dumps({"signal": 6.0, "catalyst": 5.5, "sentiment": 4.5,
                                "odds": 5.0, "risk_adj": 6.0})
            conn.execute(
                "INSERT INTO predictions "
                "(date, ticker, final_score, direction, dimension_scores, "
                " return_t7, correct_t7, checked_t7) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (f"2025-02-{i+1:02d}", f"B{i}", 7.0, direction, _dims, ret, correct),
            )
        conn.commit()
        conn.close()

        result = build_training_data_from_db(db_path=predictions_db, min_samples=30)
        assert len(result) == 35
        for td in result:
            # 看空 + 下跌 = 正确，win_7d 应为 True
            assert td.win_7d is True, (
                f"{td.ticker}: bearish + negative return should be win_7d=True"
            )
            assert td.actual_return_7d < 0  # 收益确实为负

    def test_missing_dimension_scores_are_skipped_not_fabricated(self, predictions_db):
        """v0.45.50：dimension_scores 缺失的样本**剔除**，不再补 5.0。

        旧断言是 `len(result) == 35` + `crowding_score == 50.0` —— 它把伪造行为
        固化成了不变式。实际后果：一条「某只蜂当天挂了」的记录，会变成一整行
        内部完全自洽的「各维中性、动量 0%、波动率 12.5%、IV Rank 50」正常样本，
        模型不会拒绝它，照常学。

        实测代价可控：910 条训练候选里 96.9% 五维齐全，剔除只损失 28 条（3.1%）。
        """
        conn = sqlite3.connect(predictions_db)
        for i in range(35):
            conn.execute(
                "INSERT INTO predictions (date, ticker, final_score, direction, "
                "dimension_scores, agent_directions, return_t7, checked_t7) "
                "VALUES (?, ?, ?, ?, '{}', '{}', ?, 1)",
                (f"2025-01-{i+1:02d}", f"T{i}", 7.0, "bullish", 0.05),
            )
        conn.commit()
        conn.close()

        result = build_training_data_from_db(db_path=predictions_db, min_samples=30)
        assert result == [], "维度全缺的样本必须被剔除，不得补 5.0 后进入训练集"

    def test_nonexistent_db_returns_empty(self, tmp_path):
        """不存在的数据库 → 空列表"""
        result = build_training_data_from_db(db_path=str(tmp_path / "no.db"))
        assert result == []

    def test_respects_max_rows(self, populated_db):
        """max_rows 限制"""
        result = build_training_data_from_db(
            db_path=populated_db, min_samples=5, max_rows=10,
        )
        assert len(result) == 10


# ==================== TestTrainModelIntegration ====================

class TestTrainModelIntegration:
    """MLPredictionService.train_model 改造后的测试"""

    @patch("ml_predictor.build_training_data_from_db")
    def test_train_model_prefers_real_data(self, mock_build):
        """有真实数据时不用硬编码"""
        # 创建 35 条假的 TrainingData
        real_data = [
            TrainingData(
                ticker="AAPL", date=f"2025-01-{i+1:02d}",
                crowding_score=50, catalyst_quality="A",
                momentum_5d=1.0, volatility=5.0, market_sentiment=10.0,
                actual_return_3d=0.02, actual_return_7d=0.05,
                actual_return_30d=0.12, win_3d=True, win_7d=True, win_30d=True,
            )
            for i in range(35)
        ]
        mock_build.return_value = real_data

        svc = MLPredictionService()
        result = svc.train_model()

        mock_build.assert_called_once()
        assert result.get("status") == "success"

    @patch("ml_predictor.build_training_data_from_db")
    def test_train_model_fallback_to_hardcoded(self, mock_build):
        """无真实数据时降级到硬编码"""
        mock_build.return_value = []

        svc = MLPredictionService()
        result = svc.train_model()

        assert result.get("status") == "success"
        # 确认用了硬编码数据（HistoricalDataBuilder 有 8 条）
        mock_build.assert_called_once()
