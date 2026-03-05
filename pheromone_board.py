#!/usr/bin/env python3
"""
🐝 Alpha Hive 信息素板 - 线程安全的蜂群通信系统
实时信号发布、共振检测、动态衰减
"""

import logging as _logging
from dataclasses import dataclass, field
from typing import List, Dict
from threading import RLock
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from datetime import datetime
import atexit

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
    supporting_agents: List[str] = field(default_factory=list)  # 去重：记录支持者
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class PheromoneBoard:
    """线程安全的信息素板（蜂群通信中枢）"""

    MAX_ENTRIES = 20
    DECAY_RATE = 0.1
    MIN_STRENGTH = 0.2

    # Agent → 数据维度映射（用于跨维度共振检测）
    # 真正的共振需要来自不同数据源维度的 Agent 同向，而非同一份数据的多个解读
    AGENT_DIMENSIONS: Dict[str, str] = {
        "ScoutBeeNova":      "signal",       # SEC 披露 + 聪明钱
        "OracleBeeEcho":     "odds",         # 期权 IV + 市场赔率
        "BuzzBeeWhisper":    "sentiment",    # 新闻 + Reddit 情绪
        "ChronosBeeHorizon": "catalyst",     # 催化剂与时间线
        "GuardBeeSentinel":  "risk_adj",     # 交叉验证 + 风险
        "RivalBeeVanguard":  "ml_auxiliary", # ML 预测 + 竞争格局
        "BearBeeContrarian": "contrarian",   # 看空对冲（排除在外）
    }

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
            # 基于年龄的比例衰减：越旧的条目衰减越快
            now = datetime.now()
            for e in self._entries:
                try:
                    age_minutes = (now - datetime.fromisoformat(e.timestamp)).total_seconds() / 60
                except (ValueError, TypeError):
                    age_minutes = 10  # 解析失败时默认 10 分钟
                # 衰减系数：0~5min 内 0.05，5~30min 0.1，>30min 0.15
                if age_minutes < 5:
                    decay = 0.05
                elif age_minutes < 30:
                    decay = self.DECAY_RATE
                else:
                    decay = self.DECAY_RATE * 1.5
                e.pheromone_strength -= decay

            # 清除低强度条目
            self._entries = [e for e in self._entries if e.pheromone_strength >= self.MIN_STRENGTH]

            # 若同 ticker + direction 已有条目，增加支持数（排除同 agent 重复）
            found_resonance = False
            for e in self._entries:
                if e.ticker == entry.ticker and e.direction == entry.direction:
                    # 仅当不同 Agent 时才增加支持
                    if entry.agent_id not in e.supporting_agents:
                        e.support_count += 1
                        e.supporting_agents.append(entry.agent_id)
                        # 强化信息素强度（但不超过 1.0）
                        e.pheromone_strength = min(1.0, e.pheromone_strength + 0.2)
                    found_resonance = True
                    break

            # 添加新条目（保持最大 20 条）
            if not entry.supporting_agents:
                entry.supporting_agents = [entry.agent_id]
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
                    # 清理已完成的 futures + 检查失败的写入
                    _alive = []
                    for f in self._pending_futures:
                        if not f.done():
                            _alive.append(f)
                        elif f.exception() is not None:
                            _log.warning("PheromoneBoard 异步写入失败: %s", f.exception())
                            self._save_fallback(entry_dict)
                    self._pending_futures = _alive
                except RuntimeError:
                    # 执行器已被 atexit 关闭（多次实例化场景），同步写入降级
                    _log.debug("PheromoneBoard executor shut down, fallback to sync")
                    self._save_fallback(entry_dict)

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
        检测信号共振：同向信号来自 >= 3 个不同数据维度时才触发增强

        旧逻辑：同向 Agent 数量 >= 3（存在虚假放大：多个 Agent 基于相同 yfinance 数据）
        新逻辑：同向 Agent 覆盖 >= 3 个不同数据维度（真正的多源独立印证）

        Args:
            ticker: 标的代码

        Returns:
            共振检测结果字典，新增 cross_dim_count / resonant_dimensions 字段
        """
        with self._lock:
            ticker_entries = [e for e in self._entries if e.ticker == ticker]
            bullish = [e for e in ticker_entries if e.direction == "bullish"]
            bearish = [e for e in ticker_entries if e.direction == "bearish"]

            dominant = "bullish" if len(bullish) >= len(bearish) else "bearish"
            dominant_entries = bullish if dominant == "bullish" else bearish

            # 统计同向 Agent 覆盖的不同数据维度数
            # 排除 contrarian（看空蜂不参与正向共振）和 unknown
            unique_dims = {
                self.AGENT_DIMENSIONS.get(e.agent_id, "unknown")
                for e in dominant_entries
            } - {"contrarian", "unknown"}

            cross_dim_count = len(unique_dims)

            # 触发条件：至少 3 个不同数据维度同向（而非 3 个不同 Agent）
            # 例：ScoutBee(signal) + BuzzBee(sentiment) + OracleBee(odds) = 真共振
            # 反例：3 个 Agent 都只看了动量 → 不触发（共 1 个维度）
            resonance_detected = cross_dim_count >= 3

            return {
                "resonance_detected": resonance_detected,
                "direction": dominant,
                "supporting_agents": len(dominant_entries),
                "cross_dim_count": cross_dim_count,
                "resonant_dimensions": sorted(unique_dims),
                "confidence_boost": min(cross_dim_count * 5, 20) if resonance_detected else 0,
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

    def _save_fallback(self, entry_dict: dict) -> None:
        """异步写入失败时保存到 fallback JSON 文件，防止数据丢失"""
        import json
        from pathlib import Path
        try:
            fb_path = Path("pheromone_fallback.jsonl")
            with open(fb_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry_dict, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            _log.error("PheromoneBoard fallback 写入也失败: %s", e)

    def _shutdown(self) -> None:
        """atexit 处理器：等待所有异步写入完成后关闭线程池"""
        pending = [f for f in self._pending_futures if not f.done()]
        if pending:
            _log.debug("PheromoneBoard shutdown: 等待 %d 个异步写入完成…", len(pending))
            done, not_done = _futures_wait(pending, timeout=5)
            if not_done:
                _log.warning(
                    "PheromoneBoard shutdown: %d 个异步写入超时未完成（已放弃）",
                    len(not_done),
                )
                for f in not_done:
                    f.cancel()
            # 检查已完成的 future 是否有异常，失败的写入保存到 fallback
            for f in done:
                try:
                    f.result(timeout=0)
                except Exception as exc:
                    _log.warning("PheromoneBoard shutdown 写入失败: %s", exc)
        self._executor.shutdown(wait=True)

    def clear(self) -> None:
        """清空信息素板"""
        with self._lock:
            self._entries.clear()
