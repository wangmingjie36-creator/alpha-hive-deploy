#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regime_analyzer.py — Regime-Conditional Accuracy Analysis

分析 Alpha Hive 系统在不同市场政体下的表现差异，识别盲点和甜点。
支持 IV 政体、市场政体、组合政体、按 ticker、按周几的分层精准性分析。
"""

import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple
import statistics

from hive_logger import get_logger

logger = get_logger(__name__)


class RegimeAnalyzer:
    """市场政体条件下的精准性分析引擎"""

    def __init__(self, snapshots_dir: str = "report_snapshots"):
        """
        初始化分析器

        Args:
            snapshots_dir: 报告快照目录路径
        """
        self.snapshots_dir = Path(snapshots_dir)
        self.snapshots: List[Dict[str, Any]] = []
        self._load_snapshots()
        logger.info(f"Loaded {len(self.snapshots)} report snapshots for regime analysis")

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

    def _classify_iv_regime(self, snapshot: Dict[str, Any]) -> str:
        """
        分类 IV 政体

        Args:
            snapshot: 报告快照字典

        Returns:
            "low_iv" (IV Rank < 30) | "medium_iv" (30-60) | "high_iv" (> 60)
        """
        iv_rank = snapshot.get('metrics', {}).get('iv_rank', 50)
        if iv_rank < 30:
            return "low_iv"
        elif iv_rank <= 60:
            return "medium_iv"
        else:
            return "high_iv"

    def _classify_market_regime(self, snapshot: Dict[str, Any]) -> str:
        """
        分类市场政体

        Args:
            snapshot: 报告快照字典

        Returns:
            "bull" | "bear" | "sideways"
        """
        # 使用 composite_score 的趋势或历史数据判断
        composite_score = snapshot.get('composite_score', 5)

        if composite_score >= 7:
            return "bull"
        elif composite_score <= 3:
            return "bear"
        else:
            return "sideways"

    def _classify_combined_regime(self, snapshot: Dict[str, Any]) -> str:
        """
        分类组合政体 (IV × Market)

        Args:
            snapshot: 报告快照字典

        Returns:
            Combined regime string, e.g., "low_iv_bull"
        """
        iv_regime = self._classify_iv_regime(snapshot)
        market_regime = self._classify_market_regime(snapshot)
        return f"{iv_regime}_{market_regime}"

    def _calculate_metrics(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        计算一组结果的统计指标

        Args:
            results: 结果列表，每个包含 'outcome' 和 'return' 字段

        Returns:
            指标字典：count, win_rate, avg_return, sharpe, profit_factor
        """
        if not results:
            return {
                'count': 0,
                'win_rate': 0.0,
                'avg_return': 0.0,
                'sharpe': 0.0,
                'profit_factor': 0.0
            }

        count = len(results)
        wins = sum(1 for r in results if r.get('outcome') == 'win')
        win_rate = (wins / count * 100) if count > 0 else 0.0

        returns = [r.get('return', 0.0) for r in results]
        avg_return = statistics.mean(returns) if returns else 0.0

        # Sharpe 计算（假设无风险率为 0）
        if len(returns) > 1:
            try:
                std_dev = statistics.stdev(returns)
                sharpe = (avg_return / std_dev * (252 ** 0.5)) if std_dev > 0 else 0.0
            except:
                sharpe = 0.0
        else:
            sharpe = 0.0

        # Profit Factor = 盈利总额 / 亏损总额
        positive_returns = sum(r for r in returns if r > 0)
        negative_returns = abs(sum(r for r in returns if r < 0))
        profit_factor = (positive_returns / negative_returns) if negative_returns > 0 else float('inf') if positive_returns > 0 else 0.0

        return {
            'count': count,
            'win_rate': win_rate,
            'avg_return': avg_return,
            'sharpe': sharpe,
            'profit_factor': profit_factor
        }

    def _extract_outcome(self, snapshot: Dict[str, Any], horizon: str) -> Tuple[Optional[str], Optional[float]]:
        """
        从快照中提取指定时间范围的结果

        Args:
            snapshot: 报告快照字典
            horizon: "t1", "t5", "t7", "t10", "t20", "t30"

        Returns:
            (outcome, return_pct) 元组，未找到时返回 (None, None)
        """
        outcomes = snapshot.get('outcomes', {})

        entry_price = snapshot.get('entry_price', 0)
        target_price_key = f"{horizon}_price"
        target_price = outcomes.get(target_price_key)

        if target_price is None or entry_price == 0:
            return None, None

        return_pct = ((target_price - entry_price) / entry_price * 100)
        outcome = 'win' if return_pct > 0 else 'loss'

        return outcome, return_pct

    def accuracy_by_iv_regime(self, horizon: str = "t7") -> Dict[str, Dict[str, Any]]:
        """
        按 IV 政体计算精准性

        Args:
            horizon: 时间范围 ("t1", "t5", "t7", "t10", "t20", "t30")

        Returns:
            按 IV 政体分组的指标字典
        """
        regimes = defaultdict(list)

        for snapshot in self.snapshots:
            iv_regime = self._classify_iv_regime(snapshot)
            outcome, return_pct = self._extract_outcome(snapshot, horizon)

            if outcome is not None:
                regimes[iv_regime].append({
                    'outcome': outcome,
                    'return': return_pct
                })

        return {regime: self._calculate_metrics(results) for regime, results in regimes.items()}

    def accuracy_by_market_regime(self, horizon: str = "t7") -> Dict[str, Dict[str, Any]]:
        """
        按市场政体计算精准性

        Args:
            horizon: 时间范围

        Returns:
            按市场政体分组的指标字典
        """
        regimes = defaultdict(list)

        for snapshot in self.snapshots:
            market_regime = self._classify_market_regime(snapshot)
            outcome, return_pct = self._extract_outcome(snapshot, horizon)

            if outcome is not None:
                regimes[market_regime].append({
                    'outcome': outcome,
                    'return': return_pct
                })

        return {regime: self._calculate_metrics(results) for regime, results in regimes.items()}

    def accuracy_by_combined_regime(self, horizon: str = "t7") -> Dict[str, Dict[str, Any]]:
        """
        按组合政体 (IV × Market) 计算精准性

        Args:
            horizon: 时间范围

        Returns:
            按组合政体分组的指标字典
        """
        regimes = defaultdict(list)

        for snapshot in self.snapshots:
            combined_regime = self._classify_combined_regime(snapshot)
            outcome, return_pct = self._extract_outcome(snapshot, horizon)

            if outcome is not None:
                regimes[combined_regime].append({
                    'outcome': outcome,
                    'return': return_pct
                })

        return {regime: self._calculate_metrics(results) for regime, results in regimes.items()}

    def accuracy_by_ticker(self, horizon: str = "t7") -> Dict[str, Dict[str, Any]]:
        """
        按 ticker 计算精准性

        Args:
            horizon: 时间范围

        Returns:
            按 ticker 分组的指标字典
        """
        tickers = defaultdict(list)

        for snapshot in self.snapshots:
            ticker = snapshot.get('ticker', 'UNKNOWN')
            outcome, return_pct = self._extract_outcome(snapshot, horizon)

            if outcome is not None:
                tickers[ticker].append({
                    'outcome': outcome,
                    'return': return_pct
                })

        return {ticker: self._calculate_metrics(results) for ticker, results in tickers.items()}

    def accuracy_by_day_of_week(self, horizon: str = "t7") -> Dict[str, Dict[str, Any]]:
        """
        按周几计算精准性，检测是否存在周几效应

        Args:
            horizon: 时间范围

        Returns:
            按周几分组的指标字典
        """
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        day_results = defaultdict(list)

        for snapshot in self.snapshots:
            report_date_str = snapshot.get('report_date', '')
            if not report_date_str:
                continue

            try:
                report_date = datetime.fromisoformat(report_date_str)
                day_name = days[report_date.weekday()]
            except:
                continue

            outcome, return_pct = self._extract_outcome(snapshot, horizon)

            if outcome is not None:
                day_results[day_name].append({
                    'outcome': outcome,
                    'return': return_pct
                })

        return {day: self._calculate_metrics(results) for day, results in day_results.items()}

    def find_blind_spots(self, horizon: str = "t7", threshold: float = 50.0) -> List[Dict[str, Any]]:
        """
        识别盲点（win_rate < threshold 的政体）

        Args:
            horizon: 时间范围
            threshold: 盲点阈值（胜率百分比）

        Returns:
            盲点列表，包含政体名称和指标
        """
        combined = self.accuracy_by_combined_regime(horizon)
        blind_spots = []

        for regime, metrics in combined.items():
            if metrics['win_rate'] < threshold and metrics['count'] >= 3:  # 至少 3 个样本
                blind_spots.append({
                    'regime': regime,
                    'metrics': metrics
                })

        return sorted(blind_spots, key=lambda x: x['metrics']['win_rate'])

    def find_sweet_spots(self, horizon: str = "t7", threshold: float = 65.0) -> List[Dict[str, Any]]:
        """
        识别甜点（win_rate > threshold 的政体）

        Args:
            horizon: 时间范围
            threshold: 甜点阈值（胜率百分比）

        Returns:
            甜点列表，包含政体名称和指标
        """
        combined = self.accuracy_by_combined_regime(horizon)
        sweet_spots = []

        for regime, metrics in combined.items():
            if metrics['win_rate'] > threshold and metrics['count'] >= 3:
                sweet_spots.append({
                    'regime': regime,
                    'metrics': metrics
                })

        return sorted(sweet_spots, key=lambda x: x['metrics']['win_rate'], reverse=True)

    def rolling_accuracy(self, window: int = 20, horizon: str = "t7") -> Dict[str, Any]:
        """
        计算滚动精准性，检测精准性趋势

        Args:
            window: 滚动窗口大小
            horizon: 时间范围

        Returns:
            滚动胜率列表及统计
        """
        sorted_snapshots = sorted(self.snapshots, key=lambda x: x.get('report_date', ''))
        rolling_wins = []

        for i in range(len(sorted_snapshots) - window + 1):
            window_snapshots = sorted_snapshots[i:i+window]
            wins = 0
            total = 0

            for snapshot in window_snapshots:
                outcome, _ = self._extract_outcome(snapshot, horizon)
                if outcome is not None:
                    total += 1
                    if outcome == 'win':
                        wins += 1

            if total > 0:
                win_rate = (wins / total * 100)
                rolling_wins.append({
                    'window_start': window_snapshots[0].get('report_date', ''),
                    'window_end': window_snapshots[-1].get('report_date', ''),
                    'win_rate': win_rate,
                    'count': total
                })

        return {
            'rolling_wins': rolling_wins,
            'trend': 'improving' if rolling_wins[-1]['win_rate'] > rolling_wins[0]['win_rate'] else 'degrading' if rolling_wins[-1]['win_rate'] < rolling_wins[0]['win_rate'] else 'stable'
        }

    def detect_accuracy_degradation(self, recent_window: int = 10, threshold: float = 5.0) -> Dict[str, Any]:
        """
        检测精准性是否在下降

        Args:
            recent_window: 用于计算"最近"精准性的样本数
            threshold: 下降判定阈值（百分点）

        Returns:
            包含是否下降的标志及详情的字典
        """
        rolling = self.rolling_accuracy(window=recent_window, horizon="t7")

        if len(rolling['rolling_wins']) < 2:
            return {
                'degradation_detected': False,
                'reason': 'insufficient_data'
            }

        recent = rolling['rolling_wins'][-1]['win_rate']
        historical = rolling['rolling_wins'][0]['win_rate']
        degradation = historical - recent

        return {
            'degradation_detected': degradation > threshold,
            'recent_win_rate': recent,
            'historical_win_rate': historical,
            'degradation_magnitude': degradation,
            'threshold': threshold
        }

    def generate_regime_report(self, horizon: str = "t7") -> Dict[str, Any]:
        """
        生成完整的政体分析报告

        Args:
            horizon: 时间范围

        Returns:
            完整分析报告字典
        """
        return {
            'timestamp': datetime.now().isoformat(),
            'total_snapshots': len(self.snapshots),
            'horizon': horizon,
            'iv_regime_accuracy': self.accuracy_by_iv_regime(horizon),
            'market_regime_accuracy': self.accuracy_by_market_regime(horizon),
            'combined_regime_accuracy': self.accuracy_by_combined_regime(horizon),
            'ticker_accuracy': self.accuracy_by_ticker(horizon),
            'day_of_week_accuracy': self.accuracy_by_day_of_week(horizon),
            'blind_spots': self.find_blind_spots(horizon),
            'sweet_spots': self.find_sweet_spots(horizon),
            'rolling_accuracy': self.rolling_accuracy(window=20, horizon=horizon),
            'degradation_analysis': self.detect_accuracy_degradation()
        }

    def format_regime_html(self, report: Optional[Dict[str, Any]] = None) -> str:
        """
        格式化政体分析为 HTML 表格

        Args:
            report: 报告字典，若为 None 则自动生成

        Returns:
            HTML 表格字符串
        """
        if report is None:
            report = self.generate_regime_report()

        html_parts = []
        html_parts.append('<div class="regime-analysis">')

        # 组合政体表
        html_parts.append('<h3>Combined Regime Accuracy (T+7)</h3>')
        html_parts.append('<table border="1" cellpadding="8" cellspacing="0">')
        html_parts.append('<tr><th>Regime</th><th>Count</th><th>Win Rate %</th><th>Avg Return %</th><th>Sharpe</th></tr>')

        for regime, metrics in sorted(report['combined_regime_accuracy'].items()):
            win_rate_str = f"{metrics['win_rate']:.1f}" if metrics['count'] > 0 else "N/A"
            avg_ret_str = f"{metrics['avg_return']:.2f}" if metrics['count'] > 0 else "N/A"
            sharpe_str = f"{metrics['sharpe']:.2f}" if metrics['count'] > 0 else "N/A"

            html_parts.append(f'<tr><td>{regime}</td><td>{metrics["count"]}</td><td>{win_rate_str}</td><td>{avg_ret_str}</td><td>{sharpe_str}</td></tr>')

        html_parts.append('</table>')

        # 盲点列表
        if report['blind_spots']:
            html_parts.append('<h3>Blind Spots (WinRate < 50%)</h3>')
            html_parts.append('<ul>')
            for bs in report['blind_spots']:
                html_parts.append(f'<li>{bs["regime"]}: {bs["metrics"]["win_rate"]:.1f}% ({bs["metrics"]["count"]} trades)</li>')
            html_parts.append('</ul>')

        # 甜点列表
        if report['sweet_spots']:
            html_parts.append('<h3>Sweet Spots (WinRate > 65%)</h3>')
            html_parts.append('<ul>')
            for ss in report['sweet_spots']:
                html_parts.append(f'<li>{ss["regime"]}: {ss["metrics"]["win_rate"]:.1f}% ({ss["metrics"]["count"]} trades)</li>')
            html_parts.append('</ul>')

        html_parts.append('</div>')
        return '\n'.join(html_parts)

    def print_regime_summary(self, report: Optional[Dict[str, Any]] = None) -> None:
        """
        打印政体分析摘要到控制台

        Args:
            report: 报告字典，若为 None 则自动生成
        """
        if report is None:
            report = self.generate_regime_report()

        print("\n" + "="*80)
        print("REGIME ANALYSIS SUMMARY")
        print("="*80)
        print(f"Total Snapshots: {report['total_snapshots']}")
        print(f"Horizon: {report['horizon']}")
        print()

        print("IV REGIME ACCURACY:")
        for regime, metrics in report['iv_regime_accuracy'].items():
            print(f"  {regime:12} | Count: {metrics['count']:3} | Win%: {metrics['win_rate']:5.1f} | AvgRet%: {metrics['avg_return']:6.2f} | Sharpe: {metrics['sharpe']:6.2f}")
        print()

        print("MARKET REGIME ACCURACY:")
        for regime, metrics in report['market_regime_accuracy'].items():
            print(f"  {regime:12} | Count: {metrics['count']:3} | Win%: {metrics['win_rate']:5.1f} | AvgRet%: {metrics['avg_return']:6.2f} | Sharpe: {metrics['sharpe']:6.2f}")
        print()

        print("COMBINED REGIME ACCURACY (TOP PERFORMERS):")
        sorted_combined = sorted(report['combined_regime_accuracy'].items(), key=lambda x: x[1]['win_rate'], reverse=True)
        for regime, metrics in sorted_combined[:5]:
            print(f"  {regime:25} | Count: {metrics['count']:3} | Win%: {metrics['win_rate']:5.1f} | AvgRet%: {metrics['avg_return']:6.2f}")
        print()

        if report['blind_spots']:
            print(f"BLIND SPOTS ({len(report['blind_spots'])} regimes with <50% win rate):")
            for bs in report['blind_spots'][:5]:
                print(f"  {bs['regime']:25} | Win%: {bs['metrics']['win_rate']:5.1f} ({bs['metrics']['count']} trades)")
        print()

        if report['sweet_spots']:
            print(f"SWEET SPOTS ({len(report['sweet_spots'])} regimes with >65% win rate):")
            for ss in report['sweet_spots'][:5]:
                print(f"  {ss['regime']:25} | Win%: {ss['metrics']['win_rate']:5.1f} ({ss['metrics']['count']} trades)")
        print()

        print("ROLLING ACCURACY TREND:", report['rolling_accuracy']['trend'].upper())
        print()

        degradation = report['degradation_analysis']
        if degradation.get('degradation_detected'):
            print(f"⚠️  ACCURACY DEGRADATION DETECTED: {degradation['degradation_magnitude']:.1f}pp drop")
            print(f"    Recent: {degradation['recent_win_rate']:.1f}% | Historical: {degradation['historical_win_rate']:.1f}%")
        else:
            print("✓ No accuracy degradation detected")

        print("\n" + "="*80)

    def get_regime_insights_for_brief(self) -> str:
        """
        生成用于月度自分析简报的政体见解（Markdown 格式）

        Returns:
            Markdown 格式的见解文本
        """
        report = self.generate_regime_report()

        lines = []
        lines.append("## Regime-Conditional Performance Analysis")
        lines.append("")

        # 快速统计
        total_trades = sum(m['count'] for m in report['combined_regime_accuracy'].values())
        overall_win_rate = sum(m['count'] * m['win_rate'] for m in report['combined_regime_accuracy'].values()) / total_trades if total_trades > 0 else 0

        lines.append(f"**Overall Performance (T+7):** {overall_win_rate:.1f}% win rate across {total_trades} trades")
        lines.append("")

        # 甜点
        if report['sweet_spots']:
            lines.append("### Sweet Spots (>65% accuracy)")
            for ss in report['sweet_spots'][:3]:
                lines.append(f"- **{ss['regime']}**: {ss['metrics']['win_rate']:.1f}% ({ss['metrics']['count']} trades)")

        lines.append("")

        # 盲点
        if report['blind_spots']:
            lines.append("### Blind Spots (<50% accuracy)")
            for bs in report['blind_spots'][:3]:
                lines.append(f"- **{bs['regime']}**: {bs['metrics']['win_rate']:.1f}% ({bs['metrics']['count']} trades)")

        lines.append("")

        # 趋势
        lines.append(f"### Trend: {report['rolling_accuracy']['trend'].upper()}")
        if report['degradation_analysis'].get('degradation_detected'):
            lines.append(f"⚠️ Accuracy declining by {report['degradation_analysis']['degradation_magnitude']:.1f}pp")
        else:
            lines.append("✓ Accuracy stable or improving")

        lines.append("")

        return "\n".join(lines)


def main():
    """演示脚本"""
    print("Regime Analyzer Demo")
    print("-" * 80)

    analyzer = RegimeAnalyzer()

    if analyzer.snapshots:
        analyzer.print_regime_summary()

        # 生成 HTML
        html = analyzer.format_regime_html()
        print("\nGenerated HTML snippet:")
        print(html[:300] + "...\n")

        # 生成 brief 摘要
        brief = analyzer.get_regime_insights_for_brief()
        print("Brief Insights:\n")
        print(brief)
    else:
        print("No snapshots found. Please ensure report_snapshots/ directory exists with JSON files.")


if __name__ == '__main__':
    main()
