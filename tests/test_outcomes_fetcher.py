"""
Tests for OutcomesFetcher - T+1/T+7/T+30 实际价格回填
"""

import json
import os
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timedelta

from outcomes_fetcher import OutcomesFetcher


# ==================== Fixtures ====================

@pytest.fixture
def snap_dir(tmp_path):
    """创建临时 report_snapshots 目录"""
    d = tmp_path / "report_snapshots"
    d.mkdir()
    return str(d)


@pytest.fixture
def make_snapshot(snap_dir):
    """工厂函数：创建一个快照 JSON 文件"""
    def _make(ticker="AAPL", date="2025-12-01", entry_price=150.0,
              t1=None, t7=None, t30=None, direction="Long"):
        data = {
            "ticker": ticker,
            "date": date,
            "composite_score": 7.5,
            "direction": direction,
            "price_target": 170.0,
            "stop_loss": 140.0,
            "entry_price": entry_price,
            "agent_votes": {},
            "weights_used": {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20,
                             "odds": 0.15, "risk_adj": 0.15},
            "actual_prices": {"t1": t1, "t7": t7, "t30": t30},
            "created_at": datetime.now().isoformat(),
        }
        fname = os.path.join(snap_dir, f"{ticker}_{date}.json")
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return fname
    return _make


# ==================== TestScanPending ====================

class TestScanPending:
    """_scan_pending 扫描逻辑测试"""

    def test_scan_empty_dir(self, snap_dir):
        """空目录 → 空列表"""
        fetcher = OutcomesFetcher(snap_dir)
        pending = fetcher._scan_pending()
        assert pending == []

    def test_scan_nonexistent_dir(self, tmp_path):
        """不存在的目录 → 空列表（不抛异常）"""
        fetcher = OutcomesFetcher(str(tmp_path / "no_such_dir"))
        assert fetcher._scan_pending() == []

    def test_scan_filters_already_filled(self, snap_dir, make_snapshot):
        """已填充的快照被跳过"""
        make_snapshot(t1=151.0, t7=155.0, t30=162.0)
        fetcher = OutcomesFetcher(snap_dir)
        pending = fetcher._scan_pending()
        assert len(pending) == 0

    def test_scan_filters_future_dates(self, snap_dir, make_snapshot):
        """目标日期未到 → 跳过"""
        future_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        make_snapshot(date=future_date)
        fetcher = OutcomesFetcher(snap_dir)
        pending = fetcher._scan_pending()
        assert len(pending) == 0

    def test_scan_finds_pending_snapshots(self, snap_dir, make_snapshot):
        """发现需要回填的快照"""
        # 60 天前的快照，所有价格为 null
        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        make_snapshot(date=old_date)
        fetcher = OutcomesFetcher(snap_dir)
        pending = fetcher._scan_pending()
        assert len(pending) == 1
        fpath, snap, missing = pending[0]
        assert snap.ticker == "AAPL"
        assert 1 in missing and 7 in missing and 30 in missing

    def test_scan_respects_max_snapshots(self, snap_dir, make_snapshot):
        """最多返回 max_snapshots 个"""
        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        for i in range(5):
            d = (datetime.now() - timedelta(days=60+i)).strftime("%Y-%m-%d")
            make_snapshot(ticker=f"T{i}", date=d)
        fetcher = OutcomesFetcher(snap_dir, max_snapshots=2)
        pending = fetcher._scan_pending()
        assert len(pending) == 2


# ==================== TestFetchPrice ====================

class TestFetchPrice:
    """_fetch_price 价格获取测试"""

    @patch("outcomes_fetcher._yf", None)
    def test_fetch_price_no_yfinance(self, snap_dir):
        """yfinance 不可用 → None"""
        fetcher = OutcomesFetcher(snap_dir)
        assert fetcher._fetch_price("AAPL", "2025-12-01", 7) is None

    @patch("outcomes_fetcher._yf")
    def test_fetch_price_mock_yfinance(self, mock_yf, snap_dir):
        """mock yfinance 返回正确价格"""
        import pandas as pd
        mock_hist = pd.DataFrame({"Close": [155.50]})
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_hist
        mock_yf.Ticker.return_value = mock_ticker

        fetcher = OutcomesFetcher(snap_dir)
        price = fetcher._fetch_price("AAPL", "2025-12-01", 7)
        assert price == 155.50
        mock_yf.Ticker.assert_called_once_with("AAPL")


# ==================== TestUpdateSnapshot ====================

class TestUpdateSnapshot:
    """_update_snapshot 幂等更新测试"""

    def test_update_snapshot_writes_prices(self, snap_dir, make_snapshot):
        """写入新价格"""
        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        fpath = make_snapshot(date=old_date)

        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot.load_from_json(fpath)

        fetcher = OutcomesFetcher(snap_dir)
        changed = fetcher._update_snapshot(fpath, snap, {7: 155.0})
        assert changed is True
        assert snap.actual_price_t7 == 155.0

        # 重新加载验证持久化
        snap2 = ReportSnapshot.load_from_json(fpath)
        assert snap2.actual_price_t7 == 155.0

    def test_update_snapshot_idempotent(self, snap_dir, make_snapshot):
        """重复运行不改变已有价格"""
        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        fpath = make_snapshot(date=old_date, t7=999.0)

        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot.load_from_json(fpath)

        fetcher = OutcomesFetcher(snap_dir)
        changed = fetcher._update_snapshot(fpath, snap, {7: 155.0})
        # t7 已有值 999.0，不应被覆盖
        assert changed is False
        assert snap.actual_price_t7 == 999.0

    def test_update_snapshot_empty_prices(self, snap_dir, make_snapshot):
        """空 prices → False"""
        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        fpath = make_snapshot(date=old_date)

        from feedback_loop import ReportSnapshot
        snap = ReportSnapshot.load_from_json(fpath)

        fetcher = OutcomesFetcher(snap_dir)
        assert fetcher._update_snapshot(fpath, snap, {}) is False


# ==================== TestDetermineOutcome ====================

class TestDetermineOutcome:
    """_determine_outcome 方向判定测试"""

    def test_long_positive_return_is_correct(self, snap_dir):
        fetcher = OutcomesFetcher(snap_dir)
        assert fetcher._determine_outcome("Long", 0.05) == "correct"

    def test_long_negative_return_is_incorrect(self, snap_dir):
        fetcher = OutcomesFetcher(snap_dir)
        assert fetcher._determine_outcome("Long", -0.03) == "incorrect"

    def test_short_negative_return_is_correct(self, snap_dir):
        fetcher = OutcomesFetcher(snap_dir)
        assert fetcher._determine_outcome("Short", -0.05) == "correct"

    def test_short_positive_return_is_incorrect(self, snap_dir):
        fetcher = OutcomesFetcher(snap_dir)
        assert fetcher._determine_outcome("Short", 0.03) == "incorrect"

    def test_neutral_direction_is_neutral(self, snap_dir):
        fetcher = OutcomesFetcher(snap_dir)
        assert fetcher._determine_outcome("Neutral", 0.05) == "neutral"

    def test_none_return_is_neutral(self, snap_dir):
        fetcher = OutcomesFetcher(snap_dir)
        assert fetcher._determine_outcome("Long", None) == "neutral"


# ==================== TestRunIntegration ====================

class TestRunIntegration:
    """run() 全流程 mock 测试"""

    @patch("outcomes_fetcher._yf")
    def test_run_full_flow(self, mock_yf, snap_dir, make_snapshot):
        """全流程：扫描 → 回填 → 统计"""
        import pandas as pd
        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        make_snapshot(date=old_date, entry_price=150.0)

        # mock yfinance 每次返回不同价格
        prices = iter([151.0, 155.0, 162.0])
        def _mock_history(**kwargs):
            try:
                p = next(prices)
                return pd.DataFrame({"Close": [p]})
            except StopIteration:
                return pd.DataFrame()

        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = _mock_history
        mock_yf.Ticker.return_value = mock_ticker

        fetcher = OutcomesFetcher(snap_dir, rate_limit=0)
        stats = fetcher.run()

        assert stats["scanned"] == 1
        assert stats["updated"] == 1
        assert stats["errors"] == 0

    def test_run_no_pending(self, snap_dir):
        """无待回填快照 → 正常返回"""
        fetcher = OutcomesFetcher(snap_dir, rate_limit=0)
        stats = fetcher.run()
        assert stats["scanned"] == 0
        assert stats["updated"] == 0

    @patch("outcomes_fetcher._yf")
    def test_run_with_memory_store(self, mock_yf, snap_dir, make_snapshot, memory_store):
        """回填同时更新 MemoryStore"""
        import pandas as pd
        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        make_snapshot(date=old_date, entry_price=150.0, ticker="TSLA")

        mock_hist = pd.DataFrame({"Close": [155.0]})
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_hist
        mock_yf.Ticker.return_value = mock_ticker

        fetcher = OutcomesFetcher(snap_dir, memory_store=memory_store, rate_limit=0)
        stats = fetcher.run()
        # 即使 memory_store 中无匹配记录，也不应报错
        assert stats["errors"] == 0
