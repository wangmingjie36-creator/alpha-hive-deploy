"""MemoryStore 持久化测试"""

import pytest


class TestSchemaSetup:
    def test_schema_creates_tables(self, memory_store):
        import sqlite3
        conn = sqlite3.connect(memory_store.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        assert "agent_memory" in tables
        assert "reasoning_sessions" in tables
        assert "agent_weights" in tables

    def test_wal_mode_enabled(self, memory_store):
        import sqlite3
        conn = sqlite3.connect(memory_store.db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_default_weights_initialized(self, memory_store):
        weights = memory_store.get_agent_weights()
        assert len(weights) >= 6
        assert "ScoutBeeNova" in weights
        assert weights["ScoutBeeNova"] == 1.0


class TestAgentMemory:
    def test_save_and_retrieve(self, memory_store):
        from datetime import date as _d
        today = _d.today().isoformat()
        entry = {
            "date": today,
            "ticker": "NVDA",
            "agent_id": "ScoutBeeNova",
            "direction": "bullish",
            "discovery": "测试发现",
            "source": "test",
            "self_score": 8.0,
            "pheromone_strength": 1.0,
            "support_count": 0,
        }
        memory_id = memory_store.save_agent_memory(entry, "test_session")
        assert memory_id is not None

        memories = memory_store.get_recent_memories("NVDA", days=1)
        assert len(memories) >= 1
        assert memories[0]["ticker"] == "NVDA"

    def test_filter_by_agent(self, memory_store):
        from datetime import date as _d
        today = _d.today().isoformat()
        for agent in ["ScoutBeeNova", "OracleBeeEcho"]:
            memory_store.save_agent_memory({
                "date": today, "ticker": "NVDA",
                "agent_id": agent, "direction": "bullish",
                "discovery": f"by {agent}", "source": "test",
                "self_score": 7.0,
            }, "test_session")

        scout_only = memory_store.get_recent_memories("NVDA", agent_id="ScoutBeeNova")
        assert all(m["agent_id"] == "ScoutBeeNova" for m in scout_only)


class TestSession:
    def test_save_session(self, memory_store):
        sid = memory_store.generate_session_id("test")
        ok = memory_store.save_session(
            session_id=sid, date="2026-02-25", run_mode="test",
            tickers=["NVDA"], swarm_results={"NVDA": {"final_score": 7.5}},
            pheromone_snapshot=[], duration=1.5,
        )
        assert ok

    def test_session_id_format(self, memory_store):
        sid = memory_store.generate_session_id("swarm")
        assert "swarm" in sid
        assert "2026" in sid


class TestOutcome:
    def test_update_outcome(self, memory_store):
        entry = {
            "date": "2026-02-25", "ticker": "NVDA",
            "agent_id": "TestAgent", "direction": "bullish",
            "discovery": "test", "source": "test", "self_score": 7.0,
        }
        mid = memory_store.save_agent_memory(entry, "test_session")

        updated = memory_store.update_memory_outcome(mid, "correct", t1=2.5, t7=5.0)
        assert updated


class TestWeights:
    def test_update_weight(self, memory_store):
        ok = memory_store.update_agent_weight("ScoutBeeNova", 1.5)
        assert ok

        weights = memory_store.get_agent_weights()
        assert weights["ScoutBeeNova"] == 1.5

    def test_accuracy_empty(self, memory_store):
        acc = memory_store.get_agent_accuracy("ScoutBeeNova")
        assert acc["sample_count"] == 0
        assert acc["accuracy"] == 0.5  # 默认


class TestConnectionReuse:
    """M1: 线程本地连接复用测试"""

    def test_same_thread_reuses_connection(self, memory_store):
        """同一线程内多次 _connect() 应返回同一连接对象"""
        conn1 = memory_store._connect()
        conn2 = memory_store._connect()
        assert conn1 is conn2

    def test_close_invalidates_connection(self, memory_store):
        """close() 后应创建新连接"""
        conn1 = memory_store._connect()
        memory_store.close()
        conn2 = memory_store._connect()
        assert conn1 is not conn2

    def test_multiple_operations_without_error(self, memory_store):
        """连续多次操作不应因连接复用而出错"""
        from datetime import date as _d
        today = _d.today().isoformat()
        for i in range(5):
            memory_store.save_agent_memory({
                "date": today, "ticker": f"T{i}",
                "agent_id": "TestAgent", "direction": "bullish",
                "discovery": f"test {i}", "source": "test", "self_score": 5.0,
            }, "test_session")
        # 读取应正常
        for i in range(5):
            mems = memory_store.get_recent_memories(f"T{i}", days=1)
            assert len(mems) >= 1

    def test_cross_thread_gets_own_connection(self, memory_store):
        """不同线程应获得独立连接"""
        import threading
        main_conn = memory_store._connect()
        thread_conn = [None]

        def worker():
            thread_conn[0] = memory_store._connect()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert thread_conn[0] is not None
        assert thread_conn[0] is not main_conn

    def test_details_column_saved(self, memory_store):
        """D5: details 列应正确保存和读取"""
        from datetime import date as _d
        today = _d.today().isoformat()
        import json
        details_json = json.dumps({"pc_ratio": 1.35, "iv_rank": 72})
        mid = memory_store.save_agent_memory({
            "date": today, "ticker": "NVDA",
            "agent_id": "OracleBeeEcho", "direction": "bullish",
            "discovery": "test", "source": "test",
            "self_score": 7.0, "details": details_json,
        }, "test_session")
        assert mid is not None

        mems = memory_store.get_recent_memories("NVDA", days=1)
        assert len(mems) >= 1
        assert mems[0]["details"] == details_json
