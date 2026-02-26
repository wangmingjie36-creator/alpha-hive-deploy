#!/usr/bin/env python3
"""
🐝 Alpha Hive 信息素板 - 线程安全的蜂群通信系统
实时信号发布、共振检测、动态衰减
"""

import logging as _logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import atexit
import json

_log = _logging.getLogger("alpha_hive.pheromone_board")


@dataclass
class PheromoneEntry:
    """信息素信号单条记录"""
    agent_id: str          # "ScoutBeeNova", "OracleBeeEcho" 等
    ticker: str
    discovery: str         # 一句话发现摘要
    source: str            # 数据来源
    self_score: float      # 0.0~10.0
    direction: str         # "bullish" / "bearish" / "neutral"
    pheromone_strength: float = 1.0  # 初始强度 (0.0~1.0)
    support_count: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class PheromoneBoard:
    """线程安全的信息素板（蜂群通信中枢）"""

    MAX_ENTRIES = 20
    DECAY_RATE = 0.1
    MIN_STRENGTH = 0.2

    def __init__(self, memory_store=None, session_id=None):
        self._lock = RLock()
        self._entries: List[PheromoneEntry] = []
        self._memory_store = memory_store
        self._session_id = session_id or "default_session"
        # Phase 2: 使用线程池替代 daemon 线程，确保退出时等待写入完成
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pheromone_db")
        self._pending_futures = []
        atexit.register(self._shutdown)

    def publish(self, entry: PheromoneEntry) -> None:
        """
        发布新发现，自动衰减旧条目

        Args:
            entry: 新的信息素条目
        """
        with self._lock:
            # 衰减现有条目
            for e in self._entries:
                e.pheromone_strength -= self.DECAY_RATE

            # 清除低强度条目
            self._entries = [e for e in self._entries if e.pheromone_strength >= self.MIN_STRENGTH]

            # 若同 ticker + direction 已有条目，增加支持数
            found_resonance = False
            for e in self._entries:
                if e.ticker == entry.ticker and e.direction == entry.direction:
                    e.support_count += 1
                    # 强化信息素强度（但不超过 1.0）
                    e.pheromone_strength = min(1.0, e.pheromone_strength + 0.2)
                    found_resonance = True
                    break

            # 添加新条目（保持最大 20 条）
            self._entries.append(entry)
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries.sort(key=lambda x: x.pheromone_strength)
                self._entries = self._entries[-self.MAX_ENTRIES:]

            # 异步持久化到 DB（使用线程池，退出时会等待完成）
            if self._memory_store:
                entry_dict = {
                    'agent_id': entry.agent_id,
                    'ticker': entry.ticker,
                    'discovery': entry.discovery,
                    'source': entry.source,
                    'self_score': entry.self_score,
                    'direction': entry.direction,
                    'pheromone_strength': entry.pheromone_strength,
                    'support_count': entry.support_count,
                    'date': datetime.now().strftime("%Y-%m-%d")
                }
                try:
                    future = self._executor.submit(
                        self._memory_store.save_agent_memory, entry_dict, self._session_id
                    )
                    self._pending_futures.append(future)
                    # 清理已完成的 futures（防止内存泄漏）
                    self._pending_futures = [f for f in self._pending_futures if not f.done()]
                except RuntimeError:
                    # 执行器已被 atexit 关闭（多次实例化场景），跳过异步写入
                    _log.debug("PheromoneBoard executor shut down, skipping async DB write")

    def get_top_signals(self, ticker: str = None, n: int = 5) -> List[PheromoneEntry]:
        """
        获取高强度信号，可按 ticker 过滤

        Args:
            ticker: 可选的股票过滤
            n: 返回的信号数

        Returns:
            按强度排序的信号列表
        """
        with self._lock:
            entries = [e for e in self._entries if ticker is None or e.ticker == ticker]
            return sorted(entries, key=lambda x: x.pheromone_strength, reverse=True)[:n]

    def detect_resonance(self, ticker: str) -> Dict:
        """
        检测信号共振：同向信号 >= 3 个则触发增强

        Args:
            ticker: 标的代码

        Returns:
            共振检测结果字典
        """
        with self._lock:
            ticker_entries = [e for e in self._entries if e.ticker == ticker]
            bullish = [e for e in ticker_entries if e.direction == "bullish"]
            bearish = [e for e in ticker_entries if e.direction == "bearish"]

            dominant = "bullish" if len(bullish) >= len(bearish) else "bearish"
            count = max(len(bullish), len(bearish))

            return {
                "resonance_detected": count >= 3,
                "direction": dominant,
                "supporting_agents": count,
                "confidence_boost": min(count * 5, 20)  # 最多 +20% 置信度
            }

    def snapshot(self) -> List[Dict]:
        """
        返回完整板快照（用于 QueenDistiller）

        Returns:
            信息素板的完整记录快照
        """
        with self._lock:
            return [
                {
                    "agent_id": e.agent_id,
                    "ticker": e.ticker,
                    "discovery": e.discovery,
                    "source": e.source,
                    "self_score": e.self_score,
                    "direction": e.direction,
                    "pheromone_strength": round(e.pheromone_strength, 3),
                    "support_count": e.support_count,
                    "timestamp": e.timestamp
                }
                for e in self._entries
            ]

    def compact_snapshot(self, ticker: str = None) -> List[Dict]:
        """
        紧凑快照：仅传递核心字段，避免 Agent 间 token 爆炸

        相比 snapshot() 减少 ~60% 数据量：
        - 去掉 discovery（大文本）、timestamp、source
        - 仅保留评分和方向信号
        """
        with self._lock:
            entries = self._entries
            if ticker:
                entries = [e for e in entries if e.ticker == ticker]
            return [
                {
                    "a": e.agent_id[:8],  # 缩写 agent_id
                    "t": e.ticker,
                    "d": e.direction[0],  # "b"/"n"/"b" (首字母)
                    "s": round(e.self_score, 1),
                    "p": round(e.pheromone_strength, 2),
                    "c": e.support_count,
                }
                for e in entries
            ]

    def get_entry_count(self) -> int:
        """获取当前板上的条目数"""
        with self._lock:
            return len(self._entries)

    def _shutdown(self) -> None:
        """atexit 处理器：等待所有异步写入完成后关闭线程池"""
        # 等待所有 pending futures 完成（最多 10 秒）
        for f in self._pending_futures:
            try:
                f.result(timeout=10)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                _log.debug("pheromone future failed during shutdown: %s", exc)
        self._executor.shutdown(wait=True)

    def clear(self) -> None:
        """清空信息素板"""
        with self._lock:
            self._entries.clear()
