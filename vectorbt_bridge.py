#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vectorbt_bridge.py — VectorBT Integration for Accelerated Backtesting

连接 Alpha Hive 报告快照与 VectorBT 向量化回测引擎，实现快速、大规模的策略评估。
支持信号准备、快速回测、阈值优化、策略对比、tearsheet 生成。
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
import warnings

from hive_logger import get_logger

logger = get_logger(__name__)

# Try to import optional dependencies
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    logger.warning("pandas not found, some features disabled")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    logger.warning("numpy not found, some features disabled")

try:
    import vectorbt as vbt
    HAS_VECTORBT = True
except ImportError:
    HAS_VECTORBT = False
    logger.warning("vectorbt not found, using fallback pure-Python backtest")


class VectorBTBridge:
    """VectorBT 向量化回测引擎集成"""

    def __init__(self, snapshots_dir: str = "report_snapshots"):
        """
        初始化 VectorBT 桥接器

        Args:
            snapshots_dir: 报告快照目录路径
        """
        self.snapshots_dir = Path(snapshots_dir)
        self.snapshots: List[Dict[str, Any]] = []
        self.signals_df: Optional[Any] = None  # pandas DataFrame or None
        self._load_snapshots()

        if HAS_VECTORBT:
            logger.info("VectorBT available for accelerated backtesting")
        else:
            logger.info("Using fallback pure-Python backtest (pip install vectorbt --break-system-packages for 10-50x speedup)")

    def _load_snapshots(self) -> None:
        """从快照目录加载所有报告快照"""
        if not self.snapshots_dir.exists():
            logger.warning(f"Snapshots directory not found: {self.snapshots_dir}")
            return

        for snapshot_file in sorted(self.snapshots_dir.glob("*.json")):
            try:
                with open(snapshot_file, 'r', encoding='utf-8') as f:
                    snapshot = json.load(f)
                    if snapshot.get('outcomes', {}).get('t7_price') is not None:
                        self.snapshots.append(snapshot)
            except Exception as e:
                logger.warning(f"Failed to load snapshot {snapshot_file}: {e}")

        logger.info(f"Loaded {len(self.snapshots)} snapshots for vectorized backtesting")

    def prepare_signals_dataframe(self) -> Any:
        """
        准备信号 DataFrame，转换为 VectorBT 兼容格式

        Returns:
            pandas DataFrame 或 dict（如果 pandas 不可用）
            Columns: date, ticker, direction, composite_score, entry_price, agent_votes
        """
        if not HAS_PANDAS:
            logger.warning("pandas not available, returning dict instead of DataFrame")
            return self._prepare_signals_dict()

        records = []
        for snapshot in self.snapshots:
            record = {
                'date': snapshot.get('report_date', ''),
                'ticker': snapshot.get('ticker', 'UNKNOWN'),
                'direction': 'LONG' if snapshot.get('composite_score', 5) >= 5 else 'SHORT',
                'composite_score': snapshot.get('composite_score', 5),
                'entry_price': snapshot.get('entry_price', 0),
                't7_price': snapshot.get('outcomes', {}).get('t7_price'),
                't7_return': self._calculate_return(
                    snapshot.get('entry_price', 0),
                    snapshot.get('outcomes', {}).get('t7_price')
                ),
            }

            # Flatten agent votes
            for agent_name, agent_data in snapshot.get('agent_votes', {}).items():
                record[f"vote_{agent_name}"] = agent_data.get('score', 0) if isinstance(agent_data, dict) else agent_data

            records.append(record)

        df = pd.DataFrame(records)
        self.signals_df = df
        logger.info(f"Prepared signals DataFrame: {len(df)} rows")
        return df

    def _prepare_signals_dict(self) -> Dict[str, List[Any]]:
        """
        Pure Python 版本信号准备（当 pandas 不可用时）

        Returns:
            Dict mapping column names to lists
        """
        data = {
            'date': [],
            'ticker': [],
            'direction': [],
            'composite_score': [],
            'entry_price': [],
            't7_price': [],
            't7_return': []
        }

        for snapshot in self.snapshots:
            data['date'].append(snapshot.get('report_date', ''))
            data['ticker'].append(snapshot.get('ticker', 'UNKNOWN'))

            composite = snapshot.get('composite_score', 5)
            data['direction'].append('LONG' if composite >= 5 else 'SHORT')
            data['composite_score'].append(composite)
            data['entry_price'].append(snapshot.get('entry_price', 0))

            t7_price = snapshot.get('outcomes', {}).get('t7_price')
            data['t7_price'].append(t7_price)
            data['t7_return'].append(self._calculate_return(snapshot.get('entry_price', 0), t7_price))

        return data

    def _calculate_return(self, entry_price: float, exit_price: Optional[float]) -> Optional[float]:
        """
        计算百分比收益率

        Args:
            entry_price: 入场价格
            exit_price: 出场价格

        Returns:
            百分比收益率或 None
        """
        if entry_price <= 0 or exit_price is None:
            return None
        return ((exit_price - entry_price) / entry_price * 100)

    def run_vectorized_backtest(self, signals_df: Optional[Any] = None, initial_capital: float = 100000) -> Dict[str, Any]:
        """
        运行向量化回测

        Args:
            signals_df: pandas DataFrame 信号（若为 None 则自动准备）
            initial_capital: 初始资本

        Returns:
            回测指标字典：total_return, sharpe, sortino, max_drawdown, win_rate
        """
        if signals_df is None:
            signals_df = self.prepare_signals_dataframe()

        if not isinstance(signals_df, dict) and not (HAS_PANDAS and isinstance(signals_df, pd.DataFrame)):
            raise ValueError("signals_df must be pandas DataFrame or dict")

        if HAS_VECTORBT and isinstance(signals_df, pd.DataFrame):
            return self._vectorbt_backtest(signals_df, initial_capital)
        else:
            return self._fallback_backtest(signals_df, initial_capital)

    def _vectorbt_backtest(self, signals_df: Any, initial_capital: float) -> Dict[str, Any]:
        """
        使用 VectorBT 进行向量化回测（快速）

        Args:
            signals_df: pandas DataFrame
            initial_capital: 初始资本

        Returns:
            回测指标字典
        """
        if not HAS_PANDAS or not HAS_VECTORBT:
            return self._fallback_backtest(signals_df, initial_capital)

        try:
            # 提取关键列
            entries = signals_df['direction'] == 'LONG'
            exits = signals_df['direction'] == 'SHORT'
            prices = signals_df['t7_price'].fillna(signals_df['entry_price'])

            # 创建 Portfolio
            portfolio = vbt.Portfolio.from_signals(
                close=prices.values if hasattr(prices, 'values') else prices,
                entries=entries.values if hasattr(entries, 'values') else entries,
                exits=exits.values if hasattr(exits, 'values') else exits,
                init_cash=initial_capital
            )

            # 提取指标
            total_return = float(portfolio.total_return()) * 100
            sharpe = float(portfolio.sharpe_ratio())
            sortino = float(portfolio.sortino_ratio())
            max_dd = float(portfolio.max_drawdown()) * 100
            win_rate = self._calculate_win_rate(signals_df['t7_return'].dropna())

            logger.info(f"VectorBT backtest complete: {total_return:.2f}% return, {sharpe:.2f} Sharpe")

            return {
                'total_return': total_return,
                'sharpe': sharpe,
                'sortino': sortino,
                'max_drawdown': max_dd,
                'win_rate': win_rate,
                'num_trades': len(signals_df),
                'engine': 'vectorbt'
            }
        except Exception as e:
            logger.warning(f"VectorBT backtest failed: {e}, falling back to pure Python")
            return self._fallback_backtest(signals_df, initial_capital)

    def _fallback_backtest(self, signals_df: Any, initial_capital: float) -> Dict[str, Any]:
        """
        Pure Python 向量化回测（降级模式）

        Args:
            signals_df: pandas DataFrame 或 dict
            initial_capital: 初始资本

        Returns:
            回测指标字典
        """
        if isinstance(signals_df, dict):
            returns = [r for r in signals_df.get('t7_return', []) if r is not None]
        else:
            returns = [r for r in signals_df['t7_return'].dropna().values] if HAS_PANDAS else []

        if not returns:
            logger.warning("No valid returns for fallback backtest")
            return {
                'total_return': 0.0,
                'sharpe': 0.0,
                'sortino': 0.0,
                'max_drawdown': 0.0,
                'win_rate': 0.0,
                'num_trades': 0,
                'engine': 'fallback'
            }

        # 计算基础指标
        total_return = sum(returns)
        win_rate = self._calculate_win_rate(returns)
        avg_return = sum(returns) / len(returns) if returns else 0

        # Sharpe 计算（无风险率 0%）
        if len(returns) > 1:
            variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
            std_dev = variance ** 0.5
            sharpe = (avg_return / std_dev * (252 ** 0.5)) if std_dev > 0 else 0.0
        else:
            sharpe = 0.0

        # Sortino 计算（仅负向波动）
        negative_returns = [r for r in returns if r < 0]
        if negative_returns:
            neg_variance = sum((r - avg_return) ** 2 for r in negative_returns) / len(returns)
            neg_std_dev = neg_variance ** 0.5
            sortino = (avg_return / neg_std_dev * (252 ** 0.5)) if neg_std_dev > 0 else 0.0
        else:
            sortino = sharpe  # 无负回报时，Sortino = Sharpe

        # Max Drawdown 计算
        cumulative_returns = [0]
        for r in returns:
            cumulative_returns.append(cumulative_returns[-1] + r)

        peak = cumulative_returns[0]
        max_dd = 0
        for cum_ret in cumulative_returns:
            if cum_ret > peak:
                peak = cum_ret
            dd = (peak - cum_ret) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        logger.info(f"Fallback backtest complete: {total_return:.2f}% return, {sharpe:.2f} Sharpe")

        return {
            'total_return': total_return,
            'sharpe': sharpe,
            'sortino': sortino,
            'max_drawdown': max_dd,
            'win_rate': win_rate,
            'num_trades': len(returns),
            'engine': 'fallback'
        }

    def _calculate_win_rate(self, returns: List[float]) -> float:
        """
        计算胜率

        Args:
            returns: 收益率列表

        Returns:
            胜率百分比
        """
        if not returns:
            return 0.0
        wins = sum(1 for r in returns if r > 0)
        return (wins / len(returns) * 100)

    def optimize_score_threshold(self, min_score: float = 5.0, max_score: float = 9.0, step: float = 0.5) -> Dict[str, Any]:
        """
        优化信号 score 阈值，找到最优 Sharpe

        Args:
            min_score: 最小 score 阈值
            max_score: 最大 score 阈值
            step: 阈值步长

        Returns:
            优化结果，包含最优阈值和相应指标
        """
        signals_df = self.prepare_signals_dataframe()

        if isinstance(signals_df, dict):
            logger.warning("Threshold optimization requires pandas")
            return {'optimal_threshold': 5.0, 'reason': 'pandas_required'}

        results = []
        threshold = min_score

        while threshold <= max_score:
            filtered_df = signals_df[signals_df['composite_score'] >= threshold].copy()

            if len(filtered_df) == 0:
                threshold += step
                continue

            metrics = self.run_vectorized_backtest(filtered_df)
            results.append({
                'threshold': threshold,
                'metrics': metrics,
                'num_signals': len(filtered_df)
            })

            threshold += step

        if not results:
            logger.warning("No valid thresholds tested")
            return {'optimal_threshold': min_score, 'results': []}

        # 选择最优 Sharpe
        optimal = max(results, key=lambda x: x['metrics']['sharpe'])

        logger.info(f"Optimal threshold: {optimal['threshold']:.1f} (Sharpe: {optimal['metrics']['sharpe']:.2f})")

        return {
            'optimal_threshold': optimal['threshold'],
            'optimal_metrics': optimal['metrics'],
            'num_signals_at_optimal': optimal['num_signals'],
            'all_thresholds': results
        }

    def compare_strategies(self, strategies_config: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        对比多个策略配置

        Args:
            strategies_config: 策略配置 dict，若为 None 使用默认配置
                Example:
                {
                    "all_signals": {"min_score": 0},
                    "score_gt_7": {"min_score": 7},
                    "score_gt_8": {"min_score": 8}
                }

        Returns:
            策略对比结果字典
        """
        if strategies_config is None:
            strategies_config = {
                "all_signals": {"min_score": 0},
                "score_gt_7": {"min_score": 7},
                "score_gt_8": {"min_score": 8}
            }

        signals_df = self.prepare_signals_dataframe()

        if isinstance(signals_df, dict):
            logger.warning("Strategy comparison requires pandas")
            return {'reason': 'pandas_required'}

        results = {}

        for strategy_name, config in strategies_config.items():
            min_score = config.get('min_score', 0)
            filtered_df = signals_df[signals_df['composite_score'] >= min_score].copy()

            if len(filtered_df) == 0:
                results[strategy_name] = {'num_signals': 0, 'metrics': None}
                continue

            metrics = self.run_vectorized_backtest(filtered_df)
            results[strategy_name] = {
                'num_signals': len(filtered_df),
                'metrics': metrics
            }

        logger.info(f"Strategy comparison complete: {len(results)} strategies tested")

        return results

    def generate_tearsheet(self, output_path: Optional[str] = None) -> str:
        """
        生成回测 tearsheet (HTML 或 JSON)

        Args:
            output_path: 输出文件路径，若为 None 使用默认名称

        Returns:
            输出文件路径
        """
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"backtest_tearsheet_{timestamp}.html"

        signals_df = self.prepare_signals_dataframe()
        backtest_metrics = self.run_vectorized_backtest(signals_df)
        optimization_results = self.optimize_score_threshold()
        comparison_results = self.compare_strategies()

        html_content = self._generate_tearsheet_html(
            backtest_metrics,
            optimization_results,
            comparison_results
        )

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        logger.info(f"Tearsheet written to {output_path}")
        return output_path

    def _generate_tearsheet_html(self, backtest: Dict, optimization: Dict, comparison: Dict) -> str:
        """
        生成 tearsheet HTML

        Args:
            backtest: 回测指标
            optimization: 优化结果
            comparison: 对比结果

        Returns:
            HTML 字符串
        """
        html_parts = []
        html_parts.append('<!DOCTYPE html>')
        html_parts.append('<html>')
        html_parts.append('<head>')
        html_parts.append('<meta charset="utf-8">')
        html_parts.append('<title>Alpha Hive Backtest Tearsheet</title>')
        html_parts.append('<style>')
        html_parts.append('body { font-family: Arial, sans-serif; margin: 20px; }')
        html_parts.append('table { border-collapse: collapse; width: 100%; margin: 20px 0; }')
        html_parts.append('th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }')
        html_parts.append('th { background-color: #4CAF50; color: white; }')
        html_parts.append('h2 { color: #333; }')
        html_parts.append('.metric { font-weight: bold; }')
        html_parts.append('</style>')
        html_parts.append('</head>')
        html_parts.append('<body>')

        # 标题
        html_parts.append('<h1>Alpha Hive Backtest Tearsheet</h1>')
        html_parts.append(f'<p>Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>')

        # 基础回测指标
        html_parts.append('<h2>Overall Performance (T+7)</h2>')
        html_parts.append('<table>')
        html_parts.append('<tr><th>Metric</th><th>Value</th></tr>')
        html_parts.append(f'<tr><td>Total Return</td><td class="metric">{backtest.get("total_return", 0):.2f}%</td></tr>')
        html_parts.append(f'<tr><td>Sharpe Ratio</td><td class="metric">{backtest.get("sharpe", 0):.2f}</td></tr>')
        html_parts.append(f'<tr><td>Sortino Ratio</td><td class="metric">{backtest.get("sortino", 0):.2f}</td></tr>')
        html_parts.append(f'<tr><td>Max Drawdown</td><td class="metric">{backtest.get("max_drawdown", 0):.2f}%</td></tr>')
        html_parts.append(f'<tr><td>Win Rate</td><td class="metric">{backtest.get("win_rate", 0):.1f}%</td></tr>')
        html_parts.append(f'<tr><td>Number of Trades</td><td>{backtest.get("num_trades", 0)}</td></tr>')
        html_parts.append(f'<tr><td>Engine</td><td>{backtest.get("engine", "unknown")}</td></tr>')
        html_parts.append('</table>')

        # 优化结果
        if optimization.get('optimal_threshold') is not None:
            html_parts.append('<h2>Threshold Optimization</h2>')
            html_parts.append(f'<p><strong>Optimal Threshold:</strong> {optimization.get("optimal_threshold", 0):.1f}</p>')
            html_parts.append(f'<p><strong>Signals at Optimal:</strong> {optimization.get("num_signals_at_optimal", 0)}</p>')

            if optimization.get('optimal_metrics'):
                opt_metrics = optimization['optimal_metrics']
                html_parts.append('<table>')
                html_parts.append('<tr><th>Metric</th><th>Value</th></tr>')
                html_parts.append(f'<tr><td>Return</td><td>{opt_metrics.get("total_return", 0):.2f}%</td></tr>')
                html_parts.append(f'<tr><td>Sharpe</td><td>{opt_metrics.get("sharpe", 0):.2f}</td></tr>')
                html_parts.append('</table>')

        # 策略对比
        if comparison:
            html_parts.append('<h2>Strategy Comparison</h2>')
            html_parts.append('<table>')
            html_parts.append('<tr><th>Strategy</th><th>Signals</th><th>Return %</th><th>Sharpe</th><th>Win Rate %</th></tr>')

            for strat_name, strat_data in comparison.items():
                if strat_data['metrics']:
                    m = strat_data['metrics']
                    html_parts.append(f'<tr><td>{strat_name}</td><td>{strat_data["num_signals"]}</td>')
                    html_parts.append(f'<td>{m.get("total_return", 0):.2f}</td>')
                    html_parts.append(f'<td>{m.get("sharpe", 0):.2f}</td>')
                    html_parts.append(f'<td>{m.get("win_rate", 0):.1f}</td></tr>')
                else:
                    html_parts.append(f'<tr><td>{strat_name}</td><td>0</td><td>-</td><td>-</td><td>-</td></tr>')

            html_parts.append('</table>')

        html_parts.append('</body>')
        html_parts.append('</html>')

        return '\n'.join(html_parts)

    def inject_vbt_metrics_to_report(self, report_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        将 VectorBT 指标注入到报告字典

        Args:
            report_dict: 报告字典

        Returns:
            包含 VectorBT 指标的更新后的报告字典
        """
        backtest_metrics = self.run_vectorized_backtest()

        report_dict['vectorbt_backtest'] = backtest_metrics
        report_dict['vectorbt_optimization'] = self.optimize_score_threshold()
        report_dict['vectorbt_comparison'] = self.compare_strategies()

        return report_dict


def main():
    """演示脚本"""
    print("VectorBT Bridge Demo")
    print("-" * 80)

    bridge = VectorBTBridge()

    if bridge.snapshots:
        print(f"\nLoaded {len(bridge.snapshots)} snapshots\n")

        # 准备信号
        print("1. Preparing signals dataframe...")
        signals = bridge.prepare_signals_dataframe()
        print(f"   Prepared {len(signals) if isinstance(signals, dict) else len(signals)} signals\n")

        # 运行回测
        print("2. Running vectorized backtest...")
        backtest = bridge.run_vectorized_backtest()
        print(f"   Total Return: {backtest.get('total_return', 0):.2f}%")
        print(f"   Sharpe Ratio: {backtest.get('sharpe', 0):.2f}")
        print(f"   Win Rate: {backtest.get('win_rate', 0):.1f}%")
        print(f"   Engine: {backtest.get('engine', 'unknown')}\n")

        # 优化阈值
        print("3. Optimizing score threshold...")
        optimization = bridge.optimize_score_threshold()
        print(f"   Optimal Threshold: {optimization.get('optimal_threshold', 0):.1f}\n")

        # 对比策略
        print("4. Comparing strategies...")
        comparison = bridge.compare_strategies()
        for strat_name, strat_data in comparison.items():
            if strat_data['metrics']:
                print(f"   {strat_name:20} | Signals: {strat_data['num_signals']:3} | Return: {strat_data['metrics']['total_return']:6.2f}% | Sharpe: {strat_data['metrics']['sharpe']:5.2f}")

        # 生成 Tearsheet
        print("\n5. Generating tearsheet...")
        tearsheet_path = bridge.generate_tearsheet()
        print(f"   Tearsheet written to: {tearsheet_path}\n")

    else:
        print("No snapshots found. Please ensure report_snapshots/ directory exists with JSON files.\n")


if __name__ == '__main__':
    main()
