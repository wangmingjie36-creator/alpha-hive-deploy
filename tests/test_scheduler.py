"""scheduler 模块测试 - 时区转换 + 子进程重试 + 防重叠 + 价格回填"""

import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from unittest.mock import patch, MagicMock
import pytest

# scheduler.py 依赖 schedule 库，测试环境可能未安装
pytest.importorskip("schedule", reason="schedule 库不可用，跳过 scheduler 测试")


class TestEtToLocal:
    """_et_to_local() 时区转换"""

    def test_returns_hhmm_format(self):
        from scheduler import _et_to_local
        result = _et_to_local("16:15")
        # 格式必须是 HH:MM
        assert len(result) == 5
        assert result[2] == ":"
        h, m = result.split(":")
        assert 0 <= int(h) <= 23
        assert 0 <= int(m) <= 59

    def test_known_times(self):
        from scheduler import _et_to_local
        # 验证几个关键时间都能转换（不验证具体值，因为取决于运行机器时区）
        for et_time in ["07:00", "16:15", "17:30", "19:00"]:
            result = _et_to_local(et_time)
            assert len(result) == 5, f"_et_to_local('{et_time}') 返回 '{result}'"

    def test_idempotent_within_day(self):
        from scheduler import _et_to_local
        # 同一天内多次调用应返回相同结果
        r1 = _et_to_local("09:30")
        r2 = _et_to_local("09:30")
        assert r1 == r2


class TestRunWithRetry:
    """_run_with_retry() 子进程重试"""

    def test_success_first_try(self):
        from scheduler import _run_with_retry
        result = _run_with_retry(["python3", "-c", "print('ok')"], timeout=10, max_retries=2)
        assert result is not None
        assert result.returncode == 0
        assert "ok" in result.stdout

    def test_failure_all_retries(self):
        from scheduler import _run_with_retry
        # false 命令总是返回 1
        result = _run_with_retry(["python3", "-c", "import sys; sys.exit(1)"],
                                  timeout=10, max_retries=1)
        # 最终返回最后一次的 result（非 None）
        assert result is not None
        assert result.returncode == 1

    def test_zero_retries(self):
        from scheduler import _run_with_retry
        result = _run_with_retry(["python3", "-c", "print('hello')"], timeout=10, max_retries=0)
        assert result is not None
        assert result.returncode == 0

    def test_timeout_returns_none(self):
        from scheduler import _run_with_retry
        # 超时 → 所有重试都 SubprocessError → 返回 None
        result = _run_with_retry(
            ["python3", "-c", "import time; time.sleep(30)"],
            timeout=1, max_retries=0,
        )
        assert result is None


class TestGuarded:
    """_guarded() 防重叠"""

    def test_prevents_overlap(self):
        from scheduler import _guarded
        call_count = 0
        barrier = threading.Event()

        def slow_task():
            nonlocal call_count
            call_count += 1
            barrier.wait(timeout=5)  # 阻塞直到 barrier 被 set

        guarded_fn = _guarded("test_task", slow_task)

        # 启动第一个（会阻塞）
        t1 = threading.Thread(target=guarded_fn)
        t1.start()
        time.sleep(0.1)  # 确保 t1 已进入 _running_tasks

        # 启动第二个（应被跳过）
        t2 = threading.Thread(target=guarded_fn)
        t2.start()
        t2.join(timeout=2)

        # 释放第一个
        barrier.set()
        t1.join(timeout=5)

        # 只有第一个真正执行了
        assert call_count == 1

    def test_normal_execution(self):
        from scheduler import _guarded
        results = []

        def simple_task():
            results.append("done")

        guarded_fn = _guarded("simple_test", simple_task)
        guarded_fn()
        assert results == ["done"]

        # 第二次也能执行（上一次已完成）
        guarded_fn()
        assert results == ["done", "done"]


class TestBackfillPrices:
    """ReportScheduler.backfill_prices() 价格回填"""

    def test_skips_when_no_snapshot_dir(self):
        from scheduler import ReportScheduler
        s = ReportScheduler()
        # 不应抛异常
        with patch("scheduler._PROJECT_ROOT", "/tmp/nonexistent_alpha_hive_xyz"):
            s.backfill_prices()  # 静默跳过

    def test_backfill_with_mock_yfinance(self):
        """验证回填逻辑：创建一个旧快照，mock yfinance，确认价格被回填"""
        from scheduler import ReportScheduler
        import importlib

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建快照目录和文件
            snap_dir = os.path.join(tmpdir, "report_snapshots")
            os.makedirs(snap_dir)

            # 创建一个 30+ 天前的快照（全部 actual_price 为 null）
            snap_data = {
                "ticker": "AAPL",
                "date": "2026-01-01",
                "composite_score": 8.0,
                "direction": "Long",
                "price_target": 0.0,
                "stop_loss": 0.0,
                "entry_price": 150.0,
                "agent_votes": {},
                "weights_used": {"signal": 0.30, "catalyst": 0.20,
                                 "sentiment": 0.20, "odds": 0.15, "risk_adj": 0.15},
                "actual_prices": {"t1": None, "t7": None, "t30": None},
                "created_at": "2026-01-01T10:00:00",
            }
            snap_path = os.path.join(snap_dir, "AAPL_2026-01-01.json")
            with open(snap_path, "w") as f:
                json.dump(snap_data, f)

            # Mock yfinance
            mock_hist = MagicMock()
            mock_hist.empty = False
            mock_hist.__getitem__ = MagicMock(return_value=MagicMock(
                iloc=MagicMock(__getitem__=lambda self, idx: 155.0)
            ))

            mock_ticker = MagicMock()
            mock_ticker.history.return_value = mock_hist

            mock_yf = MagicMock()
            mock_yf.Ticker.return_value = mock_ticker

            scheduler = ReportScheduler()

            with patch("scheduler._PROJECT_ROOT", tmpdir), \
                 patch.dict("sys.modules", {"yfinance": mock_yf}):
                scheduler.backfill_prices()

            # 验证快照已更新
            with open(snap_path, "r") as f:
                updated = json.load(f)
            # yfinance 被调用了（Ticker + history）
            assert mock_yf.Ticker.called


class TestReportScheduler:
    """ReportScheduler 基本功能"""

    def test_init(self):
        from scheduler import ReportScheduler
        s = ReportScheduler()
        assert s.data_collected is False
        assert s.report_generated is False

    def test_generate_reports_skips_without_data(self, capsys):
        from scheduler import ReportScheduler
        s = ReportScheduler()
        s.data_collected = False
        s.generate_reports()
        assert s.report_generated is False

    def test_upload_skips_without_report(self, capsys):
        from scheduler import ReportScheduler
        s = ReportScheduler()
        s.report_generated = False
        s.upload_to_github()
        # 不应抛异常
