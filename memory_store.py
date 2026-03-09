#!/usr/bin/env python3
"""
💾 Alpha Hive Memory Store - 持久化记忆系统
Agent 级别跨会话记忆 + 会话聚合 + 动态权重管理
"""

import sqlite3
import os
import json
import uuid
import atexit
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass
from threading import Lock, local as _thread_local
from hive_logger import get_logger, PATHS, SafeJSONEncoder

_log = get_logger("memory_store")


def _safe_json_truncate(data, max_chars=5000):
    """JSON 安全截断：按 self_score 降序排列后从低分尾部删除（S6 优先级截断）"""
    if not isinstance(data, list):
        s = json.dumps(data, cls=SafeJSONEncoder)
        return s[:max_chars] if len(s) > max_chars else s
    # S6: 按 (self_score, support_count) 降序，高价值信号优先保留
    items = sorted(
        data,
        key=lambda x: (x.get("self_score", 0) if isinstance(x, dict) else 0,
                        x.get("support_count", 0) if isinstance(x, dict) else 0),
        reverse=True,
    )
    while items:
        s = json.dumps(items, cls=SafeJSONEncoder)
        if len(s) <= max_chars:
            return s
        items = items[:-1]  # 删除最低分条目
    return "[]"

@dataclass
class MemoryEntry:
    """Agent 级别跨会话记忆"""
    memory_id: str
    session_id: str
    date: str
    ticker: str
    agent_id: str
    direction: str  # "bullish" / "bearish" / "neutral"
    discovery: str
    source: str
    self_score: float
    pheromone_strength: float = 1.0
    support_count: int = 0
    actual_outcome: Optional[str] = None  # "correct" / "incorrect" / "pending"
    outcome_return_t1: Optional[float] = None
    outcome_return_t7: Optional[float] = None
    outcome_return_t30: Optional[float] = None


class MemoryStore:
    """持久化记忆存储 - SQLite 后端（WAL 模式 + 连接安全）"""

    DB_PATH = PATHS.db
    TABLE_AGENT_MEMORY = "agent_memory"
    TABLE_SESSIONS = "reasoning_sessions"
    TABLE_WEIGHTS = "agent_weights"

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or self.DB_PATH
        self._lock = Lock()
        self._local = _thread_local()  # M1: 线程本地连接缓存

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        if not self.schema_migrate():
            _log.warning("MemoryStore schema_migrate 失败，但继续运行")

        atexit.register(self.close)

    def _connect(self) -> sqlite3.Connection:
        """获取线程本地数据库连接（WAL模式 + 懒初始化 + 健康检查）

        M1: 每个线程复用同一连接，避免每次操作都 connect/close。
        连接失效（磁盘错误等）时自动重建。
        """
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")  # 健康检查
                return conn
            except sqlite3.Error:
                try:
                    conn.close()
                except Exception as e:
                    _log.debug("conn.close() in health check: %s", e)
                conn = None

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.row_factory = sqlite3.Row  # 全局 Row factory，支持 dict(row) 和 row[i]
        self._local.conn = conn
        return conn

    def close(self) -> None:
        """关闭当前线程的数据库连接"""
        conn = getattr(self._local, 'conn', None)
        if conn:
            try:
                conn.close()
            except Exception as e:
                _log.debug("conn.close() in close(): %s", e)
            self._local.conn = None

    def schema_migrate(self) -> bool:
        """
        幂等建表 + 索引创建 + WAL模式 + 完整性检查

        Returns:
            True 如成功，False 如失败
        """
        try:
            conn = self._connect()
            cursor = conn.cursor()

            # Phase 2: 启动时完整性检查
            integrity = cursor.execute("PRAGMA integrity_check").fetchone()
            if integrity[0] != "ok":
                _log.warning("数据库完整性检查失败: %s", integrity[0])

            # 表 1: agent_memory - Agent 级别跨会话记忆
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_memory (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id           TEXT UNIQUE NOT NULL,
                    session_id          TEXT NOT NULL,
                    date                TEXT NOT NULL,
                    ticker              TEXT NOT NULL,
                    agent_id            TEXT NOT NULL,
                    direction           TEXT NOT NULL,
                    discovery           TEXT NOT NULL,
                    source              TEXT NOT NULL,
                    self_score          REAL NOT NULL,
                    pheromone_strength  REAL DEFAULT 1.0,
                    support_count       INTEGER DEFAULT 0,
                    details             TEXT DEFAULT NULL,
                    actual_outcome      TEXT DEFAULT NULL,
                    outcome_return_t1   REAL DEFAULT NULL,
                    outcome_return_t7   REAL DEFAULT NULL,
                    outcome_return_t30  REAL DEFAULT NULL,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 索引
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_am_ticker ON agent_memory(ticker)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_am_agent_id ON agent_memory(agent_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_am_date ON agent_memory(date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_am_session ON agent_memory(session_id)")

            # Migration: 为旧库添加 details 列（D5 修复）
            try:
                cursor.execute("ALTER TABLE agent_memory ADD COLUMN details TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass  # 列已存在（新建库或已迁移）

            # 表 2: reasoning_sessions - 会话级别聚合
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reasoning_sessions (
                    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id               TEXT UNIQUE NOT NULL,
                    date                     TEXT NOT NULL,
                    run_mode                 TEXT NOT NULL,
                    tickers                  TEXT NOT NULL,
                    agent_count              INTEGER NOT NULL,
                    resonances_detected      INTEGER DEFAULT 0,
                    top_opportunity_ticker   TEXT,
                    top_opportunity_score    REAL,
                    final_report_summary     TEXT,
                    pheromone_snapshot       TEXT,
                    total_duration_seconds   REAL,
                    created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 索引
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rs_date ON reasoning_sessions(date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rs_mode ON reasoning_sessions(run_mode)")

            # 表 3: agent_weights - Agent 动态权重
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_weights (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id        TEXT UNIQUE NOT NULL,
                    base_weight     REAL NOT NULL DEFAULT 1.0,
                    accuracy_t7     REAL DEFAULT NULL,
                    sample_count    INTEGER DEFAULT 0,
                    adjusted_weight REAL NOT NULL DEFAULT 1.0,
                    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 初始化 Agent 权重（如果不存在）—— S5: 新增 BearBee + Queen
            agent_ids = [
                "ScoutBeeNova",
                "OracleBeeEcho",
                "BuzzBeeWhisper",
                "ChronosBeeHorizon",
                "RivalBeeVanguard",
                "GuardBeeSentinel",
                "BearBeeContrarian",
                "QueenDistiller",
            ]

            for agent_id in agent_ids:
                cursor.execute("""
                    INSERT OR IGNORE INTO agent_weights (agent_id, base_weight, adjusted_weight)
                    VALUES (?, 1.0, 1.0)
                """, (agent_id,))

            conn.commit()

            _log.info("MemoryStore schema_migrate 成功")
            return True

        except (sqlite3.Error, OSError) as e:
            _log.error("MemoryStore schema_migrate 失败: %s", e)
            return False

    def save_agent_memory(self, entry: Dict, session_id: str) -> Optional[str]:
        """保存 Agent 记忆到数据库"""
        try:
            conn = self._connect()
            cursor = conn.cursor()

            memory_id = f"{entry['date']}_{entry['ticker']}_{entry['agent_id']}_{uuid.uuid4().hex[:12]}"

            cursor.execute("""
                INSERT INTO agent_memory (
                    memory_id, session_id, date, ticker, agent_id, direction, discovery,
                    source, self_score, pheromone_strength, support_count, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                memory_id, session_id, entry.get('date'), entry.get('ticker'),
                entry.get('agent_id'), entry.get('direction', 'neutral'),
                entry.get('discovery', ''), entry.get('source', ''),
                entry.get('self_score', 5.0), entry.get('pheromone_strength', 1.0),
                entry.get('support_count', 0), entry.get('details')
            ))

            conn.commit()
            return memory_id

        except (sqlite3.Error, OSError, TypeError, ValueError) as e:
            _log.warning("save_agent_memory 失败: %s", e)
            return None

    def save_agent_memories_batch(self, entries: List[Dict], session_id: str) -> int:
        """批量保存 Agent 记忆（单次 COMMIT，替代多次 save_agent_memory 调用）

        Args:
            entries: entry_dict 列表
            session_id: 会话 ID

        Returns:
            成功写入的条目数（0 表示失败）
        """
        if not entries:
            return 0
        try:
            conn = self._connect()
            rows = []
            for entry in entries:
                memory_id = (
                    f"{entry['date']}_{entry['ticker']}_"
                    f"{entry['agent_id']}_{uuid.uuid4().hex[:12]}"
                )
                rows.append((
                    memory_id, session_id, entry.get('date'), entry.get('ticker'),
                    entry.get('agent_id'), entry.get('direction', 'neutral'),
                    entry.get('discovery', ''), entry.get('source', ''),
                    entry.get('self_score', 5.0), entry.get('pheromone_strength', 1.0),
                    entry.get('support_count', 0), entry.get('details'),
                ))
            conn.executemany("""
                INSERT INTO agent_memory (
                    memory_id, session_id, date, ticker, agent_id, direction, discovery,
                    source, self_score, pheromone_strength, support_count, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            return len(rows)
        except (sqlite3.Error, OSError, TypeError, ValueError) as e:
            _log.warning("save_agent_memories_batch 失败 (%d 条): %s", len(entries), e)
            return 0

    def save_session(self, session_id: str, date: str, run_mode: str,
                     tickers: List[str], swarm_results: Dict,
                     pheromone_snapshot: List[Dict], duration: float) -> bool:
        """保存会话级别聚合"""
        try:
            conn = self._connect()
            cursor = conn.cursor()

            top_opp = None
            top_score = None
            if swarm_results and isinstance(swarm_results, dict):
                for ticker, result in swarm_results.items():
                    score = result.get('final_score', 0)
                    if top_score is None or score > top_score:
                        top_opp = ticker
                        top_score = score

            summary = _safe_json_truncate(
                {"top_ticker": top_opp, "top_score": top_score, "total_tickers": len(tickers)},
                500
            )

            cursor.execute("""
                INSERT OR REPLACE INTO reasoning_sessions (
                    session_id, date, run_mode, tickers, agent_count,
                    resonances_detected, top_opportunity_ticker, top_opportunity_score,
                    final_report_summary, pheromone_snapshot, total_duration_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, date, run_mode, json.dumps(tickers, cls=SafeJSONEncoder), len(tickers),
                  len([e for e in pheromone_snapshot if e.get('support_count', 0) >= 3]),
                  top_opp, top_score, summary, _safe_json_truncate(pheromone_snapshot, 5000), duration))

            conn.commit()
            return True

        except (sqlite3.Error, OSError, TypeError, ValueError) as e:
            _log.warning("save_session 失败: %s", e)
            return False

    def get_recent_memories(self, ticker: str, days: int = 30,
                            agent_id: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """获取近期记忆"""
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            if agent_id:
                cursor.execute("""
                    SELECT * FROM agent_memory
                    WHERE ticker = ? AND date >= ? AND agent_id = ?
                    ORDER BY created_at DESC LIMIT ?
                """, (ticker, cutoff_date, agent_id, limit))
            else:
                cursor.execute("""
                    SELECT * FROM agent_memory
                    WHERE ticker = ? AND date >= ?
                    ORDER BY created_at DESC LIMIT ?
                """, (ticker, cutoff_date, limit))

            return [dict(row) for row in cursor.fetchall()]

        except (sqlite3.Error, OSError) as e:
            _log.warning("get_recent_memories 失败: %s", e)
            return []

    VALID_PERIODS = {"t1": "outcome_return_t1", "t7": "outcome_return_t7", "t30": "outcome_return_t30"}

    def get_agent_accuracy(self, agent_id: str, period: str = "t7") -> Dict:
        """获取 Agent 准确率统计"""
        try:
            if period not in self.VALID_PERIODS:
                raise ValueError(f"Invalid period: {period}")
            outcome_col = self.VALID_PERIODS[period]

            conn = self._connect()
            cursor = conn.cursor()

            cursor.execute(f"""
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN actual_outcome = 'correct' THEN 1 ELSE 0 END) as correct_count,
                    AVG({outcome_col}) as avg_return,
                    MIN({outcome_col}) as min_return, MAX({outcome_col}) as max_return
                FROM agent_memory
                WHERE agent_id = ? AND actual_outcome IS NOT NULL
            """, (agent_id,))

            row = cursor.fetchone()
            if row[0] == 0:
                return {"accuracy": 0.5, "sample_count": 0, "avg_return": 0.0}

            return {
                "accuracy": row[1] / row[0] if row[0] > 0 else 0.5,
                "sample_count": row[0], "correct_count": row[1],
                "avg_return": row[2] or 0.0, "min_return": row[3] or 0.0, "max_return": row[4] or 0.0
            }
        except (sqlite3.Error, OSError, ValueError) as e:
            _log.warning("get_agent_accuracy 失败: %s", e)
            return {"accuracy": 0.5, "sample_count": 0, "avg_return": 0.0}

    def update_memory_outcome(self, memory_id: str, outcome: str,
                              t1: Optional[float] = None, t7: Optional[float] = None,
                              t30: Optional[float] = None) -> bool:
        """更新记忆的实际结果（T+1/7/30 回看）"""
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_memory
                SET actual_outcome = ?, outcome_return_t1 = ?, outcome_return_t7 = ?, outcome_return_t30 = ?
                WHERE memory_id = ?
            """, (outcome, t1, t7, t30, memory_id))
            conn.commit()
            return cursor.rowcount > 0
        except (sqlite3.Error, OSError) as e:
            _log.warning("update_memory_outcome 失败: %s", e)
            return False

    def generate_session_id(self, run_mode: str = "swarm") -> str:
        """
        生成会话 ID: {date}_{mode}_{ts_ms}

        Args:
            run_mode: 运行模式

        Returns:
            会话 ID
        """
        now = datetime.now()
        date = now.strftime("%Y-%m-%d")
        ts_ms = int(now.timestamp() * 1000)
        return f"{date}_{run_mode}_{ts_ms}"

    def get_agent_weights(self) -> Dict[str, float]:
        """获取所有 Agent 的当前权重"""
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT agent_id, adjusted_weight FROM agent_weights ORDER BY agent_id")
            return {row[0]: row[1] for row in cursor.fetchall()}
        except (sqlite3.Error, OSError) as e:
            _log.warning("get_agent_weights 失败: %s", e)
            return {}

    def cleanup_old_data(self, retention_days: int = 180) -> int:
        """删除超过 retention_days 的旧记忆和会话记录

        Returns:
            删除的 agent_memory 记录数
        """
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        try:
            conn = self._connect()
            cursor = conn.execute(
                "DELETE FROM agent_memory WHERE date < ?", (cutoff,)
            )
            mem_deleted = cursor.rowcount
            conn.execute(
                "DELETE FROM reasoning_sessions WHERE date < ?", (cutoff,)
            )
            conn.commit()
            if mem_deleted:
                _log.info("清理旧记忆 %d 条（>%d 天）", mem_deleted, retention_days)
            return mem_deleted
        except (sqlite3.Error, OSError) as e:
            _log.warning("cleanup_old_data 失败: %s", e)
            return 0

    def update_agent_weight(self, agent_id: str, adjusted_weight: float) -> bool:
        """更新单个 Agent 权重"""
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_weights
                SET adjusted_weight = ?, last_updated = CURRENT_TIMESTAMP WHERE agent_id = ?
            """, (adjusted_weight, agent_id))
            conn.commit()
            return True
        except (sqlite3.Error, OSError) as e:
            _log.warning("update_agent_weight 失败: %s", e)
            return False
