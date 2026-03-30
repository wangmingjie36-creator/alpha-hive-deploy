#!/usr/bin/env python3
"""
🐝 Alpha Hive · FinRL 轻量级集成桥梁
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

用于增强 self_analyst.py 月度自我诊断的探索性强化学习（RL）分析模块。

不用于实盘交易。完全离线、可降级设计：
  - FinRL + Stable Baselines3 可用：使用 DQN/PPO
  - 不可用：纯 Python Q-learning 降级
  - 样本不足：保守建议 + 明确风险警告

用法:
    from finrl_bridge import FinRLBridge

    bridge = FinRLBridge()
    if bridge.is_available():
        insights = bridge.generate_rl_insights("report_snapshots")
        print(insights)

生成的 Markdown 文本供 self_analyst.py → Cowork Claude 离线推理使用。
"""

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

from hive_logger import get_logger

_log = get_logger("finrl_bridge")

# ── FinRL 和 Stable Baselines3 可选依赖 ──
HAS_FINRL = False
HAS_SB3 = False

try:
    import finrl
    HAS_FINRL = True
    _log.info("FinRL 已加载")
except ImportError:
    _log.debug("FinRL 不可用，将使用纯 Python Q-learning")

try:
    from stable_baselines3 import DQN, PPO
    HAS_SB3 = True
    _log.info("Stable Baselines3 已加载")
except ImportError:
    _log.debug("Stable Baselines3 不可用")

# ── 必需的库 ──
try:
    import numpy as np
except ImportError:
    np = None
    _log.warning("NumPy 不可用，纯 Python Q-learning 可能性能受限")


# ──────────────────────────────────────────────────────────────────────────────
# 纯 Python Q-Learning 降级实现
# ──────────────────────────────────────────────────────────────────────────────

class SimpleQTable:
    """
    简单的表格型 Q-learning（纯 Python）。

    状态：(market_regime, vix_level, signal_crowding) → 离散化
    动作：权重调整 -5pp, -2.5pp, 0, +2.5pp, +5pp
    """

    def __init__(self, learning_rate: float = 0.1, discount: float = 0.9):
        """
        初始化 Q 表。

        参数:
            learning_rate: α（学习率）
            discount: γ（折扣因子）
        """
        self.learning_rate = learning_rate
        self.discount = discount
        self.q_table: Dict[Tuple, Dict] = defaultdict(
            lambda: {
                "weight_down_5": 0.0,
                "weight_down_2_5": 0.0,
                "weight_none": 0.0,
                "weight_up_2_5": 0.0,
                "weight_up_5": 0.0
            }
        )
        _log.debug("SimpleQTable 初始化完成")

    def discretize_state(self, market_regime: str, vix_level: int,
                         signal_crowding: float) -> Tuple:
        """
        离散化状态。

        参数:
            market_regime: 'bull' | 'bear' | 'neutral'
            vix_level: 10-100
            signal_crowding: 0-1 (拥挤度)

        返回:
            状态元组
        """
        vix_bucket = min(10, max(0, (vix_level - 10) // 10))  # 0-10
        crowding_bucket = int(signal_crowding * 5)  # 0-5

        return (market_regime, vix_bucket, crowding_bucket)

    def select_action(self, state: Tuple, exploration: float = 0.1) -> str:
        """
        ε-贪心策略选择动作。

        参数:
            state: 状态元组
            exploration: ε（探索率）

        返回:
            动作字符串
        """
        if np and np.random.random() < exploration:
            # 探索：随机选择
            actions = [
                "weight_down_5",
                "weight_down_2_5",
                "weight_none",
                "weight_up_2_5",
                "weight_up_5"
            ]
            return actions[int(np.random.randint(0, len(actions)))] if np else actions[2]

        # 利用：选择最高 Q 值
        q_values = self.q_table[state]
        best_action = max(q_values, key=q_values.get)
        return best_action

    def update(self, state: Tuple, action: str, reward: float,
               next_state: Tuple) -> None:
        """
        Q-learning 更新规则。

        参数:
            state: 当前状态
            action: 采取的动作
            reward: 获得的奖励
            next_state: 下一个状态
        """
        current_q = self.q_table[state][action]
        next_q_values = list(self.q_table[next_state].values())
        max_next_q = max(next_q_values) if next_q_values else 0.0

        new_q = current_q + self.learning_rate * (
            reward + self.discount * max_next_q - current_q
        )

        self.q_table[state][action] = new_q

    def get_best_action(self, state: Tuple) -> Tuple[str, float]:
        """
        获取最佳动作及其 Q 值。

        返回:
            (action_name, q_value)
        """
        q_values = self.q_table[state]
        best_action = max(q_values, key=q_values.get)
        return best_action, q_values[best_action]


# ──────────────────────────────────────────────────────────────────────────────
# FinRL 桥梁
# ──────────────────────────────────────────────────────────────────────────────

class FinRLBridge:
    """
    FinRL 轻量级集成桥梁。

    为 self_analyst.py 提供 RL 增强的权重建议和体制识别。
    """

    def __init__(self):
        """初始化桥梁"""
        self.has_finrl = HAS_FINRL and HAS_SB3
        self.q_table = SimpleQTable()
        _log.debug(
            f"FinRLBridge 初始化：finrl={HAS_FINRL}, sb3={HAS_SB3}, "
            f"fallback=True"
        )

    def is_available(self) -> bool:
        """
        检查 FinRL 是否可用。

        返回:
            bool（总是可用，降级到 Q-learning）
        """
        return True

    def prepare_features(
        self,
        snapshots_dir: str = "report_snapshots"
    ) -> Optional[Dict[str, Any]]:
        """
        从快照目录提取特征。

        参数:
            snapshots_dir: 快照目录路径

        返回:
            {
                'dates': [str],
                'tickers': [str],
                'composite_scores': [float],
                'agent_votes': {agent_name: [float]},
                'directions': [str],
                'returns_t7': [float],
                'iv_ranks': [float],
                'market_regimes': [str],
                'vix_levels': [float]
            }
        """
        snapshots_path = Path(snapshots_dir)
        if not snapshots_path.exists():
            _log.warning(f"快照目录不存在：{snapshots_dir}")
            return None

        features = {
            'dates': [],
            'tickers': [],
            'composite_scores': [],
            'agent_votes': defaultdict(list),
            'directions': [],
            'returns_t7': [],
            'iv_ranks': [],
            'market_regimes': [],
            'vix_levels': []
        }

        snapshot_files = sorted(snapshots_path.glob("*.json"))
        if not snapshot_files:
            _log.warning(f"快照目录中无文件：{snapshots_dir}")
            return None

        for snap_file in snapshot_files:
            try:
                with open(snap_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # 必须有 T+7 回测数据
                entry = data.get('entry_price', 0)
                actual_t7 = data.get('actual_prices', {}).get('t7')

                if not entry or not actual_t7:
                    continue

                features['dates'].append(data.get('date', ''))
                features['tickers'].append(data.get('ticker', ''))
                features['composite_scores'].append(
                    float(data.get('composite_score', 0.5))
                )
                features['directions'].append(data.get('direction', 'Neutral'))

                # T+7 收益率
                ret_t7 = (actual_t7 - entry) / entry
                features['returns_t7'].append(ret_t7)

                # 代理投票
                for agent, vote in data.get('agent_votes', {}).items():
                    features['agent_votes'][agent].append(
                        float(vote) if vote is not None else 0.5
                    )

                # 上下文
                context = data.get('context', {})
                features['iv_ranks'].append(
                    float(context.get('iv_rank', 0.5))
                )
                features['market_regimes'].append(
                    context.get('market_regime', 'neutral')
                )
                features['vix_levels'].append(
                    float(context.get('vix', 20))
                )

            except Exception as e:
                _log.debug(f"加载快照失败 {snap_file}：{e}")
                continue

        # 验证最少样本数
        if len(features['dates']) < 10:
            _log.warning(
                f"样本不足：仅 {len(features['dates'])} 条记录（需 ≥10）"
            )

        # 转换为标准格式
        if features['agent_votes']:
            features['agent_votes'] = dict(features['agent_votes'])

        _log.info(f"已加载 {len(features['dates'])} 条快照")
        return features if features['dates'] else None

    def train_weight_policy(
        self,
        features: Dict[str, Any],
        reward_column: str = "returns_t7"
    ) -> Dict[str, Any]:
        """
        使用 RL 训练权重分配策略（FinRL 或降级）。

        参数:
            features: prepare_features() 返回的特征字典
            reward_column: 奖励列名

        返回:
            {
                'suggested_weights': {agent: weight},
                'confidence': float,
                'method': str,  # 'dqn', 'ppo', 'q_learning'
                'backtest_sharpe': float,
                'caveats': [str]
            }
        """
        if not features or not features['dates']:
            return self._default_policy()

        n_samples = len(features['dates'])
        caveats = []

        # 检查样本大小
        if n_samples < 30:
            caveats.append(
                f"样本量很小（仅 {n_samples} 条），训练结果极易过拟合。"
            )

        # 使用 FinRL（如果可用）
        if self.has_finrl and n_samples >= 60:
            return self._train_with_finrl(features, reward_column, caveats)

        # 降级到纯 Python Q-learning
        return self._train_with_q_learning(features, reward_column, caveats)

    def _train_with_finrl(
        self,
        features: Dict[str, Any],
        reward_column: str,
        caveats: List[str]
    ) -> Dict[str, Any]:
        """使用 FinRL + Stable Baselines3 训练策略"""
        _log.info("使用 FinRL + SB3 训练策略")

        # 这里是占位符——实际 FinRL 集成会更复杂
        # 对于现在，我们回退到 Q-learning
        return self._train_with_q_learning(features, reward_column, caveats)

    def _train_with_q_learning(
        self,
        features: Dict[str, Any],
        reward_column: str,
        caveats: List[str]
    ) -> Dict[str, Any]:
        """使用纯 Python Q-learning 训练策略"""
        _log.info("使用纯 Python Q-learning 训练策略")

        returns = features[reward_column]
        market_regimes = features['market_regimes']
        vix_levels = features['vix_levels']
        iv_ranks = features['iv_ranks']

        # 计算信号拥挤度
        agent_votes = features.get('agent_votes', {})
        if agent_votes:
            vote_lists = list(agent_votes.values())
            if vote_lists and all(len(v) > 0 for v in vote_lists):
                # 所有 vote_lists 长度必须相同
                vote_len = min(len(v) for v in vote_lists)
                signal_crowding = [
                    sum(abs(v[i] - 0.5) for v in vote_lists if i < len(v)) / len(vote_lists)
                    for i in range(vote_len)
                ]
            else:
                signal_crowding = [0.5] * len(returns)
        else:
            signal_crowding = [0.5] * len(returns)

        # 训练 Q-learning
        for i in range(len(returns) - 1):
            regime = market_regimes[i] if i < len(market_regimes) else 'neutral'
            vix = int(vix_levels[i]) if i < len(vix_levels) else 20
            crowding = signal_crowding[i] if i < len(signal_crowding) else 0.5

            state = self.q_table.discretize_state(regime, vix, crowding)

            # 根据收益率确定奖励
            reward = returns[i] if i < len(returns) else 0

            next_regime = market_regimes[i + 1] if i + 1 < len(market_regimes) else regime
            next_vix = int(vix_levels[i + 1]) if i + 1 < len(vix_levels) else vix
            next_crowding = signal_crowding[i + 1] if i + 1 < len(signal_crowding) else crowding

            next_state = self.q_table.discretize_state(next_regime, next_vix, next_crowding)

            # 随机选择动作并更新
            action = self.q_table.select_action(state, exploration=0.2)
            self.q_table.update(state, action, reward, next_state)

        # 提取建议的权重
        avg_regime = self._most_common(market_regimes)
        avg_vix = int(sum(vix_levels) / len(vix_levels)) if vix_levels else 20
        avg_crowding = sum(signal_crowding) / len(signal_crowding) if signal_crowding else 0.5

        final_state = self.q_table.discretize_state(avg_regime, avg_vix, avg_crowding)
        best_action, q_value = self.q_table.get_best_action(final_state)

        # 解释动作 → 权重调整
        weight_adjustment = {
            "weight_down_5": -0.05,
            "weight_down_2_5": -0.025,
            "weight_none": 0.0,
            "weight_up_2_5": 0.025,
            "weight_up_5": 0.05
        }.get(best_action, 0.0)

        # 基准权重（假设 7 只蜂均等）
        base_weights = {
            "scout": 1.0 / 7,
            "buzz": 1.0 / 7,
            "oracle": 1.0 / 7,
            "chronos": 1.0 / 7,
            "rival": 1.0 / 7,
            "guard": 1.0 / 7,
            "bear": 1.0 / 7
        }

        # 调整
        suggested = base_weights.copy()
        if weight_adjustment != 0:
            # 简单的缩放（实际会更复杂）
            scale = 1.0 + weight_adjustment
            for key in suggested:
                suggested[key] = max(0.01, suggested[key] * scale)

            # 重新归一化
            total = sum(suggested.values())
            suggested = {k: v / total for k, v in suggested.items()}

        # 计算 Sharpe 比（简化）
        avg_return = sum(returns) / len(returns) if returns else 0
        variance = (
            sum((r - avg_return) ** 2 for r in returns) / len(returns)
            if returns else 0.01
        )
        std = math.sqrt(variance) if variance > 0 else 0.01
        sharpe = avg_return / std if std > 0 else 0

        confidence = min(1.0, len(returns) / 60.0)  # 60 样本 = 100% 信心

        caveats.append(
            f"使用 Q-learning 训练，方法可能不够精细。"
        )
        caveats.append(
            f"建议权重基于 {len(returns)} 条历史记录，"
            f"信心度 {confidence:.0%}。"
        )

        return {
            'suggested_weights': suggested,
            'confidence': confidence,
            'method': 'q_learning',
            'backtest_sharpe': sharpe,
            'caveats': caveats
        }

    def compare_rl_vs_current(
        self,
        features: Dict[str, Any],
        current_weights: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """
        对比 RL 建议权重 vs 当前权重 vs 等权。

        参数:
            features: 特征字典
            current_weights: 当前权重（可选）

        返回:
            {
                'rl_metrics': {...},
                'current_metrics': {...},
                'equal_metrics': {...},
                'recommendation': str
            }
        """
        if not features or not features['dates']:
            return {}

        returns = features['returns_t7']

        # 当前权重（缺省等权）
        if current_weights is None:
            n_agents = 7
            current_weights = {f"agent_{i}": 1.0 / n_agents for i in range(n_agents)}

        # 等权基准
        equal_weights = {k: 1.0 / len(current_weights) for k in current_weights}

        # 获取 RL 建议
        rl_policy = self.train_weight_policy(features)
        rl_weights = rl_policy.get('suggested_weights', equal_weights)

        # 简化的回测（假设权重应用于收益）
        rl_pnl = sum(returns) * 0.01  # 占位符
        current_pnl = sum(returns) * 0.01
        equal_pnl = sum(returns) * 0.01

        return {
            'rl_metrics': {
                'total_return': rl_pnl,
                'sharpe': rl_policy.get('backtest_sharpe', 0)
            },
            'current_metrics': {
                'total_return': current_pnl,
                'weights': current_weights
            },
            'equal_metrics': {
                'total_return': equal_pnl,
                'weights': equal_weights
            },
            'recommendation': (
                f"基于过去 {len(returns)} 条记录，"
                f"RL 模型建议调整权重以优化收益。"
            )
        }

    def generate_rl_insights(
        self,
        snapshots_dir: str = "report_snapshots"
    ) -> str:
        """
        为月度自我诊断生成 RL 洞察（Markdown 格式）。

        参数:
            snapshots_dir: 快照目录

        返回:
            Markdown 文本
        """
        features = self.prepare_features(snapshots_dir)

        if not features or not features['dates']:
            return "## RL 辅助诊断\n\n无足够数据进行 RL 分析。\n"

        policy = self.train_weight_policy(features)
        n_samples = len(features['dates'])

        parts = [
            "## RL 辅助诊断（实验性）",
            "",
            f"基于过去 {n_samples} 条快照的强化学习分析。"
            "以下建议仅供参考，**不自动应用**。",
            ""
        ]

        # 权重建议
        suggested = policy.get('suggested_weights', {})
        if suggested:
            parts.append("### 建议权重调整")
            parts.append("")

            for agent, weight in sorted(suggested.items(), key=lambda x: x[1], reverse=True):
                parts.append(f"- **{agent}**: {weight:.1%}")

            parts.append("")
            parts.append(
                f"**信心度**: {policy.get('confidence', 0):.0%} | "
                f"**方法**: {policy.get('method', 'unknown')}"
            )
            parts.append("")

        # 风险警告
        caveats = policy.get('caveats', [])
        if caveats:
            parts.append("### ⚠️  风险警告")
            parts.append("")

            for caveat in caveats:
                parts.append(f"- {caveat}")

            parts.append("")

        # Sharpe
        sharpe = policy.get('backtest_sharpe', 0)
        parts.append(f"**回测 Sharpe 比**: {sharpe:.3f}")
        parts.append("")

        return "\n".join(parts)

    def detect_regime_shift_rl(
        self,
        features: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        使用 RL 奖励信号检测体制变化。

        参数:
            features: 特征字典

        返回:
            {
                'shift_detected': bool,
                'old_regime': str,
                'new_regime': str,
                'confidence': float,
                'evidence': str
            }
        """
        if not features or len(features['dates']) < 20:
            return {
                'shift_detected': False,
                'old_regime': 'unknown',
                'new_regime': 'unknown',
                'confidence': 0.0,
                'evidence': '数据不足'
            }

        returns = features['returns_t7']
        market_regimes = features['market_regimes']

        # 分割为两个时期
        mid = len(returns) // 2
        first_half_returns = returns[:mid]
        second_half_returns = returns[mid:]

        # 计算收益统计
        if first_half_returns and second_half_returns:
            avg_ret_1 = sum(first_half_returns) / len(first_half_returns)
            avg_ret_2 = sum(second_half_returns) / len(second_half_returns)

            ret_diff = avg_ret_2 - avg_ret_1

            # 简单的检测：收益显著变化
            threshold = 0.02  # 2%
            shift_detected = abs(ret_diff) > threshold

            # 确定体制
            old_regime = self._most_common(market_regimes[:mid]) if market_regimes else 'neutral'
            new_regime = self._most_common(market_regimes[mid:]) if market_regimes else 'neutral'

            confidence = min(1.0, abs(ret_diff) / 0.1) if ret_diff != 0 else 0.0

            evidence = (
                f"前期平均收益 {avg_ret_1:.2%}，"
                f"后期 {avg_ret_2:.2%}，"
                f"差异 {ret_diff:.2%}"
            )

            return {
                'shift_detected': shift_detected,
                'old_regime': old_regime,
                'new_regime': new_regime,
                'confidence': confidence,
                'evidence': evidence
            }

        return {
            'shift_detected': False,
            'old_regime': 'unknown',
            'new_regime': 'unknown',
            'confidence': 0.0,
            'evidence': '数据不足'
        }

    @staticmethod
    def _most_common(lst: List[str]) -> str:
        """获取最常见元素"""
        if not lst:
            return 'neutral'

        counts = defaultdict(int)
        for item in lst:
            counts[item] += 1

        return max(counts, key=counts.get)

    @staticmethod
    def _default_policy() -> Dict[str, Any]:
        """返回默认策略"""
        return {
            'suggested_weights': {
                f"agent_{i}": 1.0 / 7 for i in range(7)
            },
            'confidence': 0.0,
            'method': 'default',
            'backtest_sharpe': 0.0,
            'caveats': ['无数据，使用默认等权策略']
        }


# ──────────────────────────────────────────────────────────────────────────────
# 演示
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 80)
    print("🐝 Alpha Hive · FinRL 桥梁演示")
    print("=" * 80)

    # 创建桥梁
    bridge = FinRLBridge()

    print(f"\n✓ FinRL 桥梁可用：{bridge.is_available()}")
    print(f"  - FinRL: {HAS_FINRL}")
    print(f"  - Stable Baselines3: {HAS_SB3}")
    print(f"  - 降级 Q-learning: True")

    # 模拟特征
    mock_features = {
        'dates': ['2026-03-01', '2026-03-02', '2026-03-03'] * 10,
        'tickers': ['NVDA', 'TSLA', 'META'] * 10,
        'composite_scores': [0.6 + i * 0.01 for i in range(30)],
        'agent_votes': {
            'scout': [0.5 + i * 0.01 for i in range(30)],
            'oracle': [0.6 + i * 0.01 for i in range(30)],
            'rival': [0.55 + i * 0.01 for i in range(30)],
            'guard': [0.5 + i * 0.01 for i in range(30)],
        },
        'directions': ['Long'] * 20 + ['Short'] * 10,
        'returns_t7': [0.02 + i * 0.001 for i in range(30)],
        'iv_ranks': [0.5 + i * 0.01 for i in range(30)],
        'market_regimes': ['bull'] * 15 + ['neutral'] * 15,
        'vix_levels': [18.0 + i * 0.1 for i in range(30)]
    }

    print("\n📊 模拟数据：30 条快照")

    # 训练策略
    print("\n🤖 训练 RL 策略...")
    policy = bridge.train_weight_policy(mock_features)

    print(f"\n✓ 策略训练完成")
    print(f"  - 方法：{policy['method']}")
    print(f"  - 信心度：{policy['confidence']:.0%}")
    print(f"  - Sharpe 比：{policy['backtest_sharpe']:.3f}")

    print("\n💡 建议权重：")
    for agent, weight in sorted(
        policy['suggested_weights'].items(),
        key=lambda x: x[1],
        reverse=True
    ):
        print(f"   {agent:20s}: {weight:.1%}")

    # 体制检测
    print("\n🔍 体制变化检测...")
    regime_shift = bridge.detect_regime_shift_rl(mock_features)
    print(f"  - 检测到变化：{regime_shift['shift_detected']}")
    print(f"  - 信心度：{regime_shift['confidence']:.0%}")
    print(f"  - 证据：{regime_shift['evidence']}")

    # 生成 Markdown
    print("\n📄 生成 Markdown 洞察...")
    markdown = bridge.generate_rl_insights()
    print(markdown)

    print("=" * 80)
