"""
🐝 Alpha Hive 线程安全测试
验证 PheromoneBoard 和 ConfigLoader 在并发场景下不会竞态/死锁
"""

import json
import os
import threading
import time

import pytest

from pheromone_board import PheromoneBoard, PheromoneEntry


# ==================== helpers ====================

def _entry(agent: str = "ScoutBeeNova", ticker: str = "NVDA",
           score: float = 7.0, direction: str = "bullish") -> PheromoneEntry:
    return PheromoneEntry(
        agent_id=agent, ticker=ticker,
        discovery=f"{agent} test signal for {ticker}",
        source="test", self_score=score, direction=direction,
    )


# ==================== PheromoneBoard 并发测试 ====================

class TestPheromoneBoardConcurrency:

    @pytest.fixture(autouse=True)
    def _board(self, tmp_path):
        self.board = PheromoneBoard()
        yield
        self.board.clear()

    def test_concurrent_publish(self):
        """10 线程 × 20 发布，无异常，entry_count ≤ MAX"""
        errors = []
        barrier = threading.Barrier(10)

        def _worker(thread_id):
            try:
                barrier.wait(timeout=5)
                for i in range(20):
                    agent = f"Agent{thread_id}"
                    ticker = f"T{i % 5}"
                    self.board.publish(_entry(agent, ticker, score=5.0 + i * 0.1))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, f"并发发布出现异常: {errors}"
        assert self.board.get_entry_count() <= PheromoneBoard.MAX_ENTRIES

    def test_concurrent_publish_and_snapshot(self):
        """发布 + compact_snapshot 并行，无崩溃"""
        errors = []
        stop = threading.Event()

        def _publisher():
            try:
                for i in range(50):
                    self.board.publish(_entry("Pub", f"T{i % 3}", score=6.0))
            except Exception as exc:
                errors.append(exc)
            finally:
                stop.set()

        def _reader():
            try:
                while not stop.is_set():
                    self.board.compact_snapshot("T0")
                    self.board.compact_snapshot()  # all tickers
            except Exception as exc:
                errors.append(exc)

        t_pub = threading.Thread(target=_publisher)
        t_read = threading.Thread(target=_reader)
        t_pub.start()
        t_read.start()
        t_pub.join(timeout=15)
        t_read.join(timeout=15)
        assert not errors, f"并发 publish+snapshot 出现异常: {errors}"

    def test_concurrent_publish_and_resonance(self):
        """发布 + detect_resonance 并行，无崩溃"""
        errors = []
        stop = threading.Event()

        def _publisher():
            try:
                for i in range(50):
                    self.board.publish(_entry(f"A{i % 5}", "NVDA", score=7.0))
            except Exception as exc:
                errors.append(exc)
            finally:
                stop.set()

        def _resonance_checker():
            try:
                while not stop.is_set():
                    result = self.board.detect_resonance("NVDA")
                    assert "resonance_detected" in result
            except Exception as exc:
                errors.append(exc)

        t_pub = threading.Thread(target=_publisher)
        t_res = threading.Thread(target=_resonance_checker)
        t_pub.start()
        t_res.start()
        t_pub.join(timeout=15)
        t_res.join(timeout=15)
        assert not errors, f"并发 publish+resonance 出现异常: {errors}"

    def test_concurrent_flush_writes(self):
        """多线程 flush_writes，无异常"""
        errors = []
        # 先填充一些数据
        for i in range(10):
            self.board.publish(_entry("A", f"T{i}", score=5.0))

        barrier = threading.Barrier(5)

        def _flusher():
            try:
                barrier.wait(timeout=5)
                self.board.flush_writes()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_flusher) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors, f"并发 flush_writes 出现异常: {errors}"

    def test_shutdown_no_deadlock(self):
        """publish + _shutdown，10s 内完成（无死锁）"""
        board = PheromoneBoard()

        def _publish_burst():
            for i in range(30):
                try:
                    board.publish(_entry("Burst", f"T{i % 3}"))
                except RuntimeError:
                    break  # executor shut down 后正常退出

        t = threading.Thread(target=_publish_burst)
        t.start()
        time.sleep(0.01)  # 让 publisher 启动
        board._shutdown()  # 应在 10s 内完成
        t.join(timeout=10)
        assert not t.is_alive(), "_shutdown 在 10s 内未完成（可能死锁）"

    def test_shutdown_copies_futures_under_lock(self):
        """验证 _shutdown 在 lock 内复制 pending futures"""
        # 检查修复：_pending_futures 在 lock 内读取
        board = PheromoneBoard()
        # 发布几条确保有 pending
        for i in range(5):
            board.publish(_entry("A", "NVDA"))
        # _shutdown 不应死锁或抛异常
        board._shutdown()
        # executor 关闭后不应再接受新提交
        assert board._executor._shutdown


# ==================== ConfigLoader 并发测试 ====================

class TestConfigLoaderConcurrency:

    @pytest.fixture(autouse=True)
    def _save_restore_globals(self):
        """保存/恢复 WATCHLIST, CATALYSTS, _last_mtime 防止污染其他测试"""
        from config import WATCHLIST, CATALYSTS, ConfigLoader
        saved_wl = dict(WATCHLIST)
        saved_cat = dict(CATALYSTS)
        saved_mtime = ConfigLoader._last_mtime
        yield
        WATCHLIST.clear()
        WATCHLIST.update(saved_wl)
        CATALYSTS.clear()
        CATALYSTS.update(saved_cat)
        ConfigLoader._last_mtime = saved_mtime

    def test_concurrent_reload(self, tmp_path, monkeypatch):
        """5 线程 reload()，无异常/死锁"""
        from config import ConfigLoader
        errors = []
        barrier = threading.Barrier(5)

        def _reloader():
            try:
                barrier.wait(timeout=5)
                for _ in range(10):
                    ConfigLoader.reload()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_reloader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, f"并发 reload 出现异常: {errors}"

    def test_concurrent_reload_if_changed(self, tmp_path, monkeypatch):
        """5 线程 reload_if_changed()，无死锁"""
        from config import ConfigLoader
        errors = []
        barrier = threading.Barrier(5)

        def _checker():
            try:
                barrier.wait(timeout=5)
                for _ in range(10):
                    ConfigLoader.reload_if_changed()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_checker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, f"并发 reload_if_changed 出现异常: {errors}"

    def test_reload_while_iterating(self, tmp_path, monkeypatch):
        """一线程 reload + 一线程遍历 WATCHLIST，无 RuntimeError"""
        from config import ConfigLoader, WATCHLIST
        # 创建 override 文件
        override = tmp_path / "watchlist_override.json"
        override.write_text(json.dumps({
            "watchlist": {"TEST1": "test1", "TEST2": "test2", "TEST3": "test3"}
        }))
        monkeypatch.setattr(ConfigLoader, "_OVERRIDE_JSON", str(override))
        monkeypatch.setattr(ConfigLoader, "_OVERRIDE_YAML", str(tmp_path / "no.yaml"))
        errors = []
        stop = threading.Event()

        def _reloader():
            try:
                while not stop.is_set():
                    ConfigLoader.reload()
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        def _iterator():
            try:
                for _ in range(200):
                    # 遍历 WATCHLIST — 如果 reload 并发 .clear()/.update() 可能 RuntimeError
                    list(WATCHLIST.keys())
                    list(WATCHLIST.values())
            except Exception as exc:
                errors.append(exc)
            finally:
                stop.set()

        t_reload = threading.Thread(target=_reloader)
        t_iter = threading.Thread(target=_iterator)
        t_reload.start()
        t_iter.start()
        t_iter.join(timeout=15)
        t_reload.join(timeout=15)
        assert not errors, f"reload+iterate 并发出现异常: {errors}"

    def test_reload_if_changed_atomicity(self, tmp_path, monkeypatch):
        """验证 mtime 检查 + 重载是原子操作（不会双重重载）"""
        from config import ConfigLoader
        override = tmp_path / "watchlist_override.json"
        override.write_text(json.dumps({
            "watchlist": {"ATOM1": "a", "ATOM2": "b"}
        }))
        monkeypatch.setattr(ConfigLoader, "_OVERRIDE_JSON", str(override))
        monkeypatch.setattr(ConfigLoader, "_OVERRIDE_YAML", str(tmp_path / "no.yaml"))
        # 重置 mtime
        monkeypatch.setattr(ConfigLoader, "_last_mtime", 0.0)

        results = []
        barrier = threading.Barrier(5)

        def _checker():
            try:
                barrier.wait(timeout=5)
                r = ConfigLoader.reload_if_changed()
                results.append(r)
            except Exception:
                results.append(None)

        threads = [threading.Thread(target=_checker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # 只有 1 个线程应该返回 True（原子性：mtime 检查+重载在同一锁内）
        true_count = sum(1 for r in results if r is True)
        assert true_count == 1, f"预期 1 次重载，实际 {true_count} 次 (results={results})"
