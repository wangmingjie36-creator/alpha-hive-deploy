#!/usr/bin/env python3
"""
Alpha Hive - 指标收集器 (Week 4 可观测性)

收集、持久化和查询蜂群扫描性能/质量指标。
支持 SLO 检查和异常自动告警。

用法：
    from metrics_collector import MetricsCollector
    mc = MetricsCollector()
    mc.record_scan(ticker_count=5, duration=3.2, agent_count=6, ...)
    mc.check_slo()  # 返回违规列表
    summary = mc.get_summary(days=7)
"""

import json
import os
import sqlite3
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from hive_logger import PATHS, get_logger

_log = get_logger("metrics")


# ==================== SLO 定义 ====================

DEFAULT_SLO = {
    "scan_latency_p95_seconds": 30.0,    # 95% 扫描耗时 < 30s
    "agent_error_rate_max": 0.05,        # Agent 错误率 < 5%
    "data_real_pct_min": 50.0,           # 真实数据占比 > 50%
    "min_supporting_agents": 3,          # 最少支持 Agent 数
    "max_consecutive_failures": 3,       # 最大连续失败数
}


class MetricsCollector:
    """指标收集、持久化和 SLO 检查"""

    def __init__(self, db_path: str = None, slo: Dict = None):
        self._db_path = db_path or str(PATHS.home / "metrics.db")
        self._slo = slo or dict(DEFAULT_SLO)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """创建指标表"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    session_id TEXT,
                    scan_mode TEXT DEFAULT 'swarm',
                    ticker_count INTEGER DEFAULT 0,
                    agent_count INTEGER DEFAULT 0,
                    duration_seconds REAL DEFAULT 0.0,
                    prefetch_seconds REAL DEFAULT 0.0,
                    avg_score REAL DEFAULT 5.0,
                    max_score REAL DEFAULT 5.0,
                    min_score REAL DEFAULT 5.0,
                    agent_errors INTEGER DEFAULT 0,
                    agent_total INTEGER DEFAULT 0,
                    data_real_pct REAL DEFAULT 0.0,
                    resonance_count INTEGER DEFAULT 0,
                    llm_calls INTEGER DEFAULT 0,
                    llm_cost_usd REAL DEFAULT 0.0,
                    memory_mb REAL DEFAULT 0.0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticker_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    session_id TEXT,
                    ticker TEXT NOT NULL,
                    final_score REAL DEFAULT 5.0,
                    direction TEXT DEFAULT 'neutral',
                    supporting_agents INTEGER DEFAULT 0,
                    data_real_pct REAL DEFAULT 0.0,
                    resonance_detected INTEGER DEFAULT 0,
                    analysis_seconds REAL DEFAULT 0.0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS slo_violations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    slo_name TEXT NOT NULL,
                    threshold REAL,
                    actual REAL,
                    details TEXT
                )
            """)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ==================== 记录指标 ====================

    def record_scan(
        self,
        ticker_count: int,
        duration_seconds: float,
        agent_count: int = 6,
        prefetch_seconds: float = 0.0,
        avg_score: float = 5.0,
        max_score: float = 5.0,
        min_score: float = 5.0,
        agent_errors: int = 0,
        agent_total: int = 0,
        data_real_pct: float = 0.0,
        resonance_count: int = 0,
        llm_calls: int = 0,
        llm_cost_usd: float = 0.0,
        session_id: str = "",
        scan_mode: str = "swarm",
    ):
        """记录一次完整扫描的指标"""
        now = datetime.now().isoformat()
        memory_mb = self._get_memory_mb()

        with self._lock:
            with self._connect() as conn:
                conn.execute("""
                    INSERT INTO scan_metrics (
                        timestamp, session_id, scan_mode,
                        ticker_count, agent_count, duration_seconds, prefetch_seconds,
                        avg_score, max_score, min_score,
                        agent_errors, agent_total, data_real_pct,
                        resonance_count, llm_calls, llm_cost_usd, memory_mb
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now, session_id, scan_mode,
                    ticker_count, agent_count, duration_seconds, prefetch_seconds,
                    avg_score, max_score, min_score,
                    agent_errors, agent_total, data_real_pct,
                    resonance_count, llm_calls, llm_cost_usd, memory_mb,
                ))
                conn.commit()

        _log.info(
            "metrics: scan %s | %d tickers %.1fs | err=%d/%d | real=%.0f%%",
            scan_mode, ticker_count, duration_seconds,
            agent_errors, agent_total, data_real_pct,
        )

    def record_ticker(
        self,
        ticker: str,
        final_score: float,
        direction: str = "neutral",
        supporting_agents: int = 0,
        data_real_pct: float = 0.0,
        resonance_detected: bool = False,
        analysis_seconds: float = 0.0,
        session_id: str = "",
    ):
        """记录单个 ticker 的分析指标"""
        now = datetime.now().isoformat()
        with self._lock:
            with self._connect() as conn:
                conn.execute("""
                    INSERT INTO ticker_metrics (
                        timestamp, session_id, ticker,
                        final_score, direction, supporting_agents,
                        data_real_pct, resonance_detected, analysis_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now, session_id, ticker,
                    final_score, direction, supporting_agents,
                    data_real_pct, int(resonance_detected), analysis_seconds,
                ))
                conn.commit()

    # ==================== SLO 检查 ====================

    def check_slo(self, days: int = 1) -> List[Dict]:
        """
        检查最近 N 天的 SLO 违规

        Returns:
            违规列表 [{slo_name, threshold, actual, details}]
        """
        violations = []
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM scan_metrics WHERE timestamp > ? ORDER BY timestamp DESC",
                (cutoff,)
            ).fetchall()

        if not rows:
            return violations

        # 1. P95 延迟检查
        durations = sorted(r["duration_seconds"] for r in rows)
        if durations:
            p95_idx = int(len(durations) * 0.95)
            p95 = durations[min(p95_idx, len(durations) - 1)]
            threshold = self._slo["scan_latency_p95_seconds"]
            if p95 > threshold:
                violations.append({
                    "slo_name": "scan_latency_p95",
                    "threshold": threshold,
                    "actual": round(p95, 2),
                    "details": f"P95 延迟 {p95:.1f}s > {threshold:.0f}s",
                })

        # 2. Agent 错误率
        total_errors = sum(r["agent_errors"] for r in rows)
        total_agents = sum(r["agent_total"] for r in rows)
        if total_agents > 0:
            error_rate = total_errors / total_agents
            threshold = self._slo["agent_error_rate_max"]
            if error_rate > threshold:
                violations.append({
                    "slo_name": "agent_error_rate",
                    "threshold": threshold,
                    "actual": round(error_rate, 4),
                    "details": f"错误率 {error_rate:.1%} > {threshold:.0%} ({total_errors}/{total_agents})",
                })

        # 3. 数据真实率
        avg_real_pct = sum(r["data_real_pct"] for r in rows) / len(rows)
        threshold = self._slo["data_real_pct_min"]
        if avg_real_pct < threshold:
            violations.append({
                "slo_name": "data_real_pct",
                "threshold": threshold,
                "actual": round(avg_real_pct, 1),
                "details": f"真实数据 {avg_real_pct:.0f}% < {threshold:.0f}%",
            })

        # 持久化违规记录
        if violations:
            now = datetime.now().isoformat()
            with self._lock:
                with self._connect() as conn:
                    for v in violations:
                        conn.execute(
                            "INSERT INTO slo_violations (timestamp, slo_name, threshold, actual, details) VALUES (?,?,?,?,?)",
                            (now, v["slo_name"], v["threshold"], v["actual"], v["details"]),
                        )
                    conn.commit()

        return violations

    # ==================== 查询/汇总 ====================

    def get_summary(self, days: int = 7) -> Dict:
        """获取最近 N 天的指标汇总"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row

            scans = conn.execute(
                "SELECT * FROM scan_metrics WHERE timestamp > ? ORDER BY timestamp DESC",
                (cutoff,)
            ).fetchall()

            tickers = conn.execute(
                "SELECT * FROM ticker_metrics WHERE timestamp > ? ORDER BY timestamp DESC",
                (cutoff,)
            ).fetchall()

            violations = conn.execute(
                "SELECT * FROM slo_violations WHERE timestamp > ? ORDER BY timestamp DESC",
                (cutoff,)
            ).fetchall()

        if not scans:
            return {
                "period_days": days,
                "total_scans": 0,
                "total_tickers": 0,
                "avg_duration": 0.0,
                "p95_duration": 0.0,
                "avg_score": 5.0,
                "error_rate": 0.0,
                "slo_violations": 0,
            }

        durations = [r["duration_seconds"] for r in scans]
        scores = [r["avg_score"] for r in scans]
        total_errors = sum(r["agent_errors"] for r in scans)
        total_agents = sum(r["agent_total"] for r in scans)

        p95_idx = int(len(durations) * 0.95)
        p95 = sorted(durations)[min(p95_idx, len(durations) - 1)]

        return {
            "period_days": days,
            "total_scans": len(scans),
            "total_tickers": len(tickers),
            "avg_duration": round(sum(durations) / len(durations), 2),
            "p95_duration": round(p95, 2),
            "max_duration": round(max(durations), 2),
            "avg_score": round(sum(scores) / len(scores), 2),
            "max_score": round(max(r["max_score"] for r in scans), 2),
            "error_rate": round(total_errors / total_agents, 4) if total_agents > 0 else 0.0,
            "total_llm_calls": sum(r["llm_calls"] for r in scans),
            "total_llm_cost": round(sum(r["llm_cost_usd"] for r in scans), 4),
            "resonance_count": sum(r["resonance_count"] for r in scans),
            "avg_memory_mb": round(sum(r["memory_mb"] for r in scans) / len(scans), 1),
            "slo_violations": len(violations),
            "violation_details": [dict(v) for v in violations[:10]],
        }

    def get_ticker_history(self, ticker: str, days: int = 30) -> List[Dict]:
        """获取单个 ticker 的历史指标"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM ticker_metrics WHERE ticker = ? AND timestamp > ? ORDER BY timestamp DESC",
                (ticker, cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    def cleanup(self, retention_days: int = 90):
        """清理过期数据"""
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        with self._lock:
            with self._connect() as conn:
                for table in ("scan_metrics", "ticker_metrics", "slo_violations"):
                    conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                conn.commit()
        _log.info("metrics cleanup: removed entries older than %d days", retention_days)

    # ==================== 内部工具 ====================

    @staticmethod
    def _get_memory_mb() -> float:
        """获取当前进程内存使用（MB）"""
        try:
            import resource
            rusage = resource.getrusage(resource.RUSAGE_SELF)
            return rusage.ru_maxrss / (1024 * 1024)  # macOS: bytes -> MB
        except Exception:
            return 0.0
