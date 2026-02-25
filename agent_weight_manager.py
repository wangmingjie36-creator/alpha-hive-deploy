#!/usr/bin/env python3
"""
⚖️ Alpha Hive Agent Weight Manager - 动态权重管理
根据 T+7/30 准确率动态调整 Agent 权重，实现自适应蜂群
"""

import logging as _logging
from typing import Dict, Optional
from datetime import datetime
import threading

_log = _logging.getLogger("alpha_hive.agent_weight_manager")

try:
    import numpy as np
except ImportError:
    np = None


class AgentWeightManager:
    """Agent 动态权重管理器"""

    # 权重约束
    MIN_WEIGHT = 0.3
    MAX_WEIGHT = 3.0

    # 最小样本数（样本不足时保持平等权重）
    MIN_SAMPLES_FOR_DYNAMIC = 10

    # 准确率对权重的影响系数
    ACCURACY_WEIGHT_COEFFICIENT = 2.0

    # 6 个 Agent 默认权重
    DEFAULT_AGENTS = [
        "ScoutBeeNova",
        "OracleBeeEcho",
        "BuzzBeeWhisper",
        "ChronosBeeHorizon",
        "RivalBeeVanguard",
        "GuardBeeSentinel"
    ]

    def __init__(self, memory_store):
        """
        初始化权重管理器

        Args:
            memory_store: MemoryStore 实例
        """
        self.memory_store = memory_store
        self._weights_cache: Dict[str, float] = {}
        self._cache_timestamp = None
        self._cache_ttl_seconds = 3600  # 1 小时缓存
        self._lock = threading.RLock()

        # 初始化权重缓存
        self._refresh_weights_cache()

    def _refresh_weights_cache(self) -> None:
        """从 DB 刷新权重缓存"""
        try:
            weights = self.memory_store.get_agent_weights()
            with self._lock:
                self._weights_cache = weights or {agent: 1.0 for agent in self.DEFAULT_AGENTS}
                self._cache_timestamp = datetime.now()
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.error("_refresh_weights_cache 失败: %s", e, exc_info=True)
            # Fallback 到默认权重
            with self._lock:
                self._weights_cache = {agent: 1.0 for agent in self.DEFAULT_AGENTS}

    def get_weights(self) -> Dict[str, float]:
        """
        获取所有 Agent 的当前权重（可能使用缓存）

        Returns:
            {agent_id: adjusted_weight}
        """
        with self._lock:
            # 检查缓存是否过期
            if self._cache_timestamp:
                age_seconds = (datetime.now() - self._cache_timestamp).total_seconds()
                if age_seconds > self._cache_ttl_seconds:
                    # 缓存过期，刷新
                    pass
                else:
                    # 缓存有效，返回
                    return self._weights_cache.copy()

        # 刷新缓存
        self._refresh_weights_cache()

        with self._lock:
            return self._weights_cache.copy()

    def get_weight(self, agent_id: str) -> float:
        """
        获取单个 Agent 的权重

        Args:
            agent_id: Agent ID

        Returns:
            权重值 (default 1.0)
        """
        weights = self.get_weights()
        return weights.get(agent_id, 1.0)

    def weighted_average_score(self, agent_results: list) -> float:
        """
        计算加权平均分

        Args:
            agent_results: Agent 结果列表 [{"score": float, "source": str}, ...]

        Returns:
            加权平均分
        """
        weights = self.get_weights()

        total_score = 0.0
        total_weight = 0.0

        for result in agent_results:
            if result and "error" not in result:
                score = result.get("score", 5.0)
                source = result.get("source", "Unknown")
                weight = weights.get(source, 1.0)

                total_score += score * weight
                total_weight += weight

        if total_weight > 0:
            return total_score / total_weight
        else:
            return 5.0

    def recalculate_all_weights(self) -> Dict[str, float]:
        """
        根据 T+7 准确率重新计算所有 Agent 权重

        权重公式：
        - base_weight = 1.0
        - adjusted = clip(1.0 + (accuracy - 0.5) * COEFFICIENT, MIN_WEIGHT, MAX_WEIGHT)
        - accuracy=0.5(随机)->1.0 | accuracy=0.8->1.6 | accuracy=0.3->0.6

        Returns:
            更新后的权重字典
        """
        new_weights = {}

        for agent_id in self.DEFAULT_AGENTS:
            try:
                # 获取 Agent 的准确率统计
                accuracy_stats = self.memory_store.get_agent_accuracy(agent_id, period="t7")

                sample_count = accuracy_stats.get("sample_count", 0)
                accuracy = accuracy_stats.get("accuracy", 0.5)

                # 样本不足时保持默认权重
                if sample_count < self.MIN_SAMPLES_FOR_DYNAMIC:
                    adjusted_weight = 1.0
                else:
                    # 应用权重公式
                    # 如果准确率 > 0.5，权重 > 1.0；如果 < 0.5，权重 < 1.0
                    adjusted = 1.0 + (accuracy - 0.5) * self.ACCURACY_WEIGHT_COEFFICIENT
                    adjusted_weight = np.clip(adjusted, self.MIN_WEIGHT, self.MAX_WEIGHT) if np else max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, adjusted))

                new_weights[agent_id] = round(adjusted_weight, 3)

                # 更新数据库
                self.memory_store.update_agent_weight(agent_id, adjusted_weight)

                print(f"✅ {agent_id}: accuracy={accuracy:.2%}, samples={sample_count}, weight={adjusted_weight:.2f}x")

            except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
                _log.error("recalculate_all_weights(%s) 失败: %s", agent_id, e, exc_info=True)
                new_weights[agent_id] = 1.0

        # 更新缓存
        with self._lock:
            self._weights_cache = new_weights
            self._cache_timestamp = datetime.now()

        return new_weights

    def print_weight_summary(self) -> None:
        """打印权重摘要"""
        weights = self.get_weights()

        print("\n📊 Agent 权重摘要")
        print("=" * 60)

        for agent_id in self.DEFAULT_AGENTS:
            weight = weights.get(agent_id, 1.0)
            indicator = "🔥" if weight > 1.2 else ("❄️" if weight < 0.8 else "📊")
            print(f"{indicator} {agent_id:20s} {weight:6.2f}x")

        print("=" * 60)
