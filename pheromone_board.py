#!/usr/bin/env python3
"""
🐝 Alpha Hive 信息素板 - 线程安全的蜂群通信系统
实时信号发布、共振检测、动态衰减
"""

import heapq as _heapq
import json as _json
import logging as _logging
from collections import deque as _deque
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
    details: Dict = field(default_factory=dict)   # S3: 结构化键值对（替代正则解析）


class PheromoneBoard:
    """线程安全的信息素板（蜂群通信中枢）"""

    MAX_ENTRIES = 80  # 7 Agent × 9 Ticker = 63 条，80 条保留完整一轮 + 余量
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
        "BearBeeContrarian": "contrarian",   # 看空对冲（仅在看空共振中参与维度计数）
    }

    _BATCH_SIZE = 20  # 每 20 条刷新一次 DB（63 条 ≈ 4 次 commit，原来 63 次）

    def __init__(self, memory_store=None, session_id=None):
        self._lock = RLock()
        self._entries: List[PheromoneEntry] = []
        self._memory_store = memory_store
        self._session_id = session_id or "default_session"
        # Phase 2: 使用线程池替代 daemon 线程，确保退出时等待写入完成
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pheromone_db")
        self._pending_futures: _deque = _deque(maxlen=32)  # 有界 deque，防止无限增长
        self._write_buffer: List[Dict] = []  # 批量写入缓冲区
        atexit.register(self._shutdown)
        # 可配置衰减率（从 config.PHEROMONE_CONFIG 读取，ImportError 时用默认值）
        try:
            from config import PHEROMONE_CONFIG as _pcfg
            _bdr = _pcfg.get("board_decay_rates", {})
            self._fresh_minutes = _bdr.get("fresh_minutes", 5)
            self._fresh_decay = _bdr.get("fresh_decay", 0.05)
            self._medium_decay = _bdr.get("medium_decay", self.DECAY_RATE)
            self._old_decay = _bdr.get("old_decay", self.DECAY_RATE * 1.5)
            self._ticker_scoped = _pcfg.get("board_ticker_scoped_decay", True)
        except (ImportError, AttributeError):
            self._fresh_minutes = 5
            self._fresh_decay = 0.05
            self._medium_decay = self.DECAY_RATE
            self._old_decay = self.DECAY_RATE * 1.5
            self._ticker_scoped = True

    # 方案13: 输入验证 — 保护核心通信通道
    _VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}

    @staticmethod
    def _validate_entry(entry: 'PheromoneEntry') -> None:
        """
        验证并修正 PheromoneEntry 字段，防止垃圾数据污染信息素板。

        规则：
        - self_score: clamp 到 [0, 10]，NaN/非数字 → 5.0
        - direction: 必须为 bullish/bearish/neutral，其他 → "neutral"
        - pheromone_strength: clamp 到 [0, 1]，NaN → 1.0
        """
        import math
        # self_score
        try:
            s = float(entry.self_score)
            if math.isnan(s) or math.isinf(s):
                _log.warning("PheromoneBoard: self_score 为 NaN/Inf (agent=%s, ticker=%s) → 设为 5.0",
                             entry.agent_id, entry.ticker)
                entry.self_score = 5.0
            else:
                entry.self_score = max(0.0, min(10.0, s))
        except (TypeError, ValueError):
            _log.warning("PheromoneBoard: self_score 不是数字 (agent=%s, ticker=%s) → 设为 5.0",
                         entry.agent_id, entry.ticker)
            entry.self_score = 5.0

        # direction
        if entry.direction not in PheromoneBoard._VALID_DIRECTIONS:
            _log.warning("PheromoneBoard: direction '%s' 无效 (agent=%s, ticker=%s) → 设为 'neutral'",
                         entry.direction, entry.agent_id, entry.ticker)
            entry.direction = "neutral"

        # pheromone_strength
        try:
            p = float(entry.pheromone_strength)
            if math.isnan(p) or math.isinf(p):
                _log.warning("PheromoneBoard: pheromone_strength 为 NaN/Inf → 设为 1.0")
                entry.pheromone_strength = 1.0
            else:
                entry.pheromone_strength = max(0.0, min(1.0, p))
        except (TypeError, ValueError):
            entry.pheromone_strength = 1.0

    def publish(self, entry: PheromoneEntry) -> None:
        """
        发布新发现，自动衰减旧条目

        Args:
            entry: 新的信息素条目
        """
        self._validate_entry(entry)  # 方案13: 入口验证
        with self._lock:
            # 单遍衰减 + 存活过滤（合并两个循环，减少 datetime 解析次数）
            # 仅衰减同 ticker 条目（避免跨 ticker 误杀：TSLA 发布不应衰减 NVDA）
            now = datetime.now()
            _surviving = []
            for e in self._entries:
                # 存活检查 1: 强度过低
                if e.pheromone_strength < self.MIN_STRENGTH:
                    continue
                # 存活检查 2: 时间戳解析 + 超时淘汰（>60min）
                try:
                    _age_s = (now - datetime.fromisoformat(e.timestamp)).total_seconds()
                except (ValueError, TypeError):
                    continue  # 时间戳解析失败 → 视为过期
                if _age_s >= 3600:
                    continue
                # 衰减（仅同 ticker）
                if not self._ticker_scoped or e.ticker == entry.ticker:
                    _age_m = _age_s / 60
                    if _age_m < self._fresh_minutes:
                        e.pheromone_strength -= self._fresh_decay
                    elif _age_m < 30:
                        e.pheromone_strength -= self._medium_decay
                    else:
                        e.pheromone_strength -= self._old_decay
                    # 衰减后二次检查（防止衰减到 MIN 以下）
                    if e.pheromone_strength < self.MIN_STRENGTH:
                        continue
                _surviving.append(e)
            self._entries = _surviving

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

            # 添加新条目（保持最大 MAX_ENTRIES 条）— S6: 按 (score, support, strength) 优先级截断
            if not entry.supporting_agents:
                entry.supporting_agents = [entry.agent_id]
            self._entries.append(entry)
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries = _heapq.nlargest(
                    self.MAX_ENTRIES, self._entries,
                    key=lambda x: (x.self_score, x.support_count, x.pheromone_strength),
                )

            # 批量异步持久化到 DB：先缓冲，达到阈值后批量提交（减少 fsync 次数）
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
                    'details': _json.dumps(entry.details, ensure_ascii=False, default=str) if entry.details else None,
                    'date': datetime.now().strftime("%Y-%m-%d")
                }
                self._write_buffer.append(entry_dict)
                if len(self._write_buffer) >= self._BATCH_SIZE:
                    self._flush_writes()

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

            # 无条目时直接返回 neutral
            if not bullish and not bearish:
                return {
                    "resonance_detected": False,
                    "direction": "neutral",
                    "supporting_agents": 0,
                    "cross_dim_count": 0,
                    "resonant_dimensions": [],
                    "confidence_boost": 0,
                }

            # 平局时双向检查维度数，选择维度覆盖更广的方向（避免一律偏多）
            if len(bullish) == len(bearish) and len(bullish) > 0:
                _bull_dims = {self.AGENT_DIMENSIONS.get(e.agent_id, "unknown") for e in bullish} - {"unknown", "contrarian"}
                _bear_dims = {self.AGENT_DIMENSIONS.get(e.agent_id, "unknown") for e in bearish} - {"unknown"}
                dominant = "bearish" if len(_bear_dims) > len(_bull_dims) else "bullish"
            else:
                dominant = "bullish" if len(bullish) > len(bearish) else "bearish"
            dominant_entries = bullish if dominant == "bullish" else bearish

            # 统计同向 Agent 覆盖的不同数据维度数
            unique_dims = {
                self.AGENT_DIMENSIONS.get(e.agent_id, "unknown")
                for e in dominant_entries
            } - {"unknown"}
            # BearBee contrarian 维度仅在看空共振中计入
            # （看多时排除：BearBee 返回 bullish 仅表示"无看空证据"，非独立看多维度）
            if dominant == "bullish":
                unique_dims -= {"contrarian"}

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
                    "timestamp": e.timestamp,
                    "details": e.details,
                }
                for e in self._entries
            ]

    # D1: details 中对 LLM 决策最有价值的字段白名单（避免传全量）
    _DETAIL_KEYS = {
        "pc_ratio", "iv_rank", "iv_skew", "gex",                    # OracleBee
        "sentiment_score", "reddit_momentum",                        # BuzzBee
        "catalyst_count", "nearest_days", "analyst_upside_pct",      # ChronosBee
        "ml_probability", "expected_7d",                             # RivalBee
        "consistency", "conflict_type", "resonance_detected",        # GuardBee
        "bear_score", "signal_count",                                # BearBee
    }

    def compact_snapshot(self, ticker: str = None) -> List[Dict]:
        """
        紧凑快照：核心字段 + 精选 details，供 LLM 决策

        相比 snapshot() 减少 ~50% 数据量：
        - 去掉 discovery（大文本）、timestamp、source
        - 保留评分、方向、支持数
        - D1: 新增 details 白名单字段（仅数值/布尔，每条约 +40 token）
        """
        with self._lock:
            entries = self._entries
            if ticker:
                entries = [e for e in entries if e.ticker == ticker]
            result = []
            for e in entries:
                item = {
                    "a": e.agent_id[:8],  # 缩写 agent_id
                    "t": e.ticker,
                    "d": {"bullish": "+", "bearish": "-", "neutral": "0"}.get(e.direction, "?"),
                    "s": round(e.self_score, 1),
                    "p": round(e.pheromone_strength, 2),
                    "c": e.support_count,
                }
                # D1: 附加精选 details（白名单过滤，仅有值时添加）
                if e.details:
                    detail_compact = {
                        k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in e.details.items()
                        if k in self._DETAIL_KEYS and v is not None
                    }
                    if detail_compact:
                        item["x"] = detail_compact  # "x" = extra details
                result.append(item)
            return result

    def get_entry_count(self) -> int:
        """获取当前板上的条目数"""
        with self._lock:
            return len(self._entries)

    def _flush_writes(self) -> None:
        """将缓冲区中的条目批量写入 DB（需持有 _lock 调用）"""
        if not self._write_buffer or not self._memory_store:
            return
        batch = list(self._write_buffer)
        self._write_buffer.clear()
        # 清理已完成的 futures + 检查失败
        _still = _deque(maxlen=32)
        for f in self._pending_futures:
            if not f.done():
                _still.append(f)
            elif f.exception() is not None:
                _log.warning("PheromoneBoard 异步写入失败: %s", f.exception())
        self._pending_futures = _still
        # 提交批量写入（通过 callback 确保失败时写入 fallback，防止数据丢失）
        try:
            future = self._executor.submit(
                self._memory_store.save_agent_memories_batch, batch, self._session_id
            )

            def _on_done(f, _batch=batch):
                if f.exception() is not None:
                    _log.warning("PheromoneBoard 异步批量写入失败: %s", f.exception())
                    self._save_fallback_batch(_batch)

            future.add_done_callback(_on_done)
            self._pending_futures.append(future)
        except RuntimeError:
            _log.debug("PheromoneBoard executor shut down, fallback to sync")
            self._save_fallback_batch(batch)

    def flush_writes(self) -> None:
        """公开接口：强制刷新缓冲区（扫描结束时调用）"""
        with self._lock:
            self._flush_writes()

    def _save_fallback(self, entry_dict: dict) -> None:
        """异步写入失败时保存到 fallback JSON 文件，防止数据丢失"""
        self._save_fallback_batch([entry_dict])

    def _save_fallback_batch(self, entries: list) -> None:
        """批量 fallback 写入"""
        from pathlib import Path
        try:
            fb_path = Path(__file__).parent / "pheromone_fallback.jsonl"
            with open(fb_path, "a", encoding="utf-8") as f:
                for entry_dict in entries:
                    f.write(_json.dumps(entry_dict, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            _log.error("PheromoneBoard fallback 写入也失败: %s", e)

    def _shutdown(self) -> None:
        """atexit 处理器：刷新缓冲 + 等待异步写入完成 + 关闭线程池"""
        # 刷新残余缓冲 + 在 lock 内复制 pending（防止与 publish 并发修改 _pending_futures）
        with self._lock:
            self._flush_writes()
            pending = [f for f in self._pending_futures if not f.done()]
        # 在 lock 外等待（callback 可能需要 lock 进行 fallback 写入）
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
