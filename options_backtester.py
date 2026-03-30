"""
Alpha Hive - 期权策略回溯测试模块 (OptionsBacktester)
=========================================================

对 Scout/Oracle/Bear 推荐信号的期权策略回溯测试框架。
使用历史数据验证推荐的策略表现，计算胜率、收益率、夏普比、最大回撤等指标。

核心功能：
  1. 策略定义与回溯（长call、长put、牛市call、熊市put等）
  2. Black-Scholes 估价期权P&L
  3. 信号→策略映射（分数、方向、IV环境）
  4. 政体分类与最优策略推荐
  5. 与 feedback_loop 集成，注入准确度报告

不依赖外部期权数据，使用 greeks_engine.py 的 Black-Scholes 估价模块。
"""

import json
import os
import math
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
from statistics import mean, stdev, median

try:
    from hive_logger import PATHS, get_logger, atomic_json_write
except ImportError:
    def get_logger(name):
        return logging.getLogger(name)
    def atomic_json_write(path, data, **kwargs):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, **kwargs)
    class PATHS:
        cache_dir = "cache"

try:
    from greeks_engine import bs_price, calculate_single
except ImportError:
    bs_price = None
    calculate_single = None

_log = get_logger("options_backtester")


# ============================================================================
# 枚举 / 数据类定义
# ============================================================================

class StrategyType(Enum):
    """期权策略类型"""
    LONG_CALL = "long_call"           # 长 call
    LONG_PUT = "long_put"             # 长 put
    BULL_CALL_SPREAD = "bull_call_spread"       # 牛市 call 价差
    BEAR_PUT_SPREAD = "bear_put_spread"         # 熊市 put 价差
    IRON_CONDOR = "iron_condor"       # 铁秃鹰
    STRADDLE = "straddle"             # 跨式


class MarketRegime(Enum):
    """市场政体分类"""
    LOW_IV_BULL = "low_iv_bull"       # 低IV + 多头
    LOW_IV_BEAR = "low_iv_bear"       # 低IV + 空头
    HIGH_IV_BULL = "high_iv_bull"     # 高IV + 多头
    HIGH_IV_BEAR = "high_iv_bear"     # 高IV + 空头
    NEUTRAL = "neutral"               # 中性


@dataclass
class StrategyResult:
    """单笔交易的策略结果"""
    strategy: StrategyType
    entry_price: float              # 入场股价
    entry_date: str                 # 入场日期
    exit_price: float               # 出场股价（T+7 或最后已知价格）
    exit_date: str                  # 出场日期
    dte_entry: int                  # 入场时剩余DTE
    dte_exit: int                   # 出场时剩余DTE
    iv_entry: float                 # 入场IV
    iv_exit: float                  # 出场IV
    strike_call: float              # Call 行权价（如适用）
    strike_put: float               # Put 行权价（如适用）
    pnl_dollar: float               # 绝对P&L（美元）
    pnl_pct: float                  # 百分比P&L
    max_drawdown: float             # 最大回撤（百分比）
    win: bool                       # 是否盈利
    ticker: str                     # 股票代码
    regime: str                     # 交易时政体


@dataclass
class StrategyBacktestResult:
    """策略回溯结果汇总"""
    strategy: StrategyType
    trades: List[StrategyResult] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        """胜率"""
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.win)
        return (wins / len(self.trades)) if self.trades else 0.0

    @property
    def avg_return(self) -> float:
        """平均收益率（百分比）"""
        if not self.trades:
            return 0.0
        return mean(t.pnl_pct for t in self.trades) if self.trades else 0.0

    @property
    def sharpe_ratio(self) -> float:
        """夏普比（假设252交易日，无风险率0%）"""
        if len(self.trades) < 2:
            return 0.0
        returns = [t.pnl_pct for t in self.trades]
        std = stdev(returns) if len(returns) > 1 else 0.0
        if std == 0:
            return 0.0
        daily_return = mean(returns) / 100.0  # 转化为小数
        annual_return = daily_return * 252
        annual_vol = (std / 100.0) * math.sqrt(252)
        return annual_return / annual_vol if annual_vol > 0 else 0.0

    @property
    def max_drawdown(self) -> float:
        """最大回撤（百分比）"""
        if not self.trades:
            return 0.0
        max_dd = min(t.max_drawdown for t in self.trades) if self.trades else 0.0
        return max_dd

    @property
    def profit_factor(self) -> float:
        """利润因子（总盈利/总亏损）"""
        if not self.trades:
            return 0.0
        gross_profit = sum(t.pnl_dollar for t in self.trades if t.pnl_dollar > 0)
        gross_loss = abs(sum(t.pnl_dollar for t in self.trades if t.pnl_dollar < 0))
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def total_pnl(self) -> float:
        """总P&L"""
        return sum(t.pnl_dollar for t in self.trades)

    @property
    def num_trades(self) -> int:
        """交易笔数"""
        return len(self.trades)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "strategy": self.strategy.value,
            "num_trades": self.num_trades,
            "win_rate": round(self.win_rate, 4),
            "avg_return_pct": round(self.avg_return, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "max_drawdown_pct": round(self.max_drawdown, 4),
            "profit_factor": round(self.profit_factor, 4),
            "total_pnl_dollar": round(self.total_pnl, 2),
            "trades": [asdict(t) for t in self.trades]
        }


# ============================================================================
# Black-Scholes 期权定价辅助函数
# ============================================================================

def _fallback_bs_price(S: float, K: float, T: float, r: float, sigma: float,
                       option_type: str = 'call') -> float:
    """
    简化的 Black-Scholes 定价（当 greeks_engine 不可用时）

    Args:
        S: 股价
        K: 行权价
        T: 到期时间（年）
        r: 无风险率
        sigma: 波动率（年化）
        option_type: 'call' 或 'put'

    Returns:
        期权理论价格
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        if option_type == 'call':
            return max(S - K, 0.0)
        else:
            return max(K - S, 0.0)

    # 简化版本：使用内在价值 + IV溢价近似
    if option_type == 'call':
        intrinsic = max(S - K, 0.0)
        # IV 溢价粗估：sigma * sqrt(T) * S * 0.4（固定系数）
        time_value = sigma * math.sqrt(T) * S * 0.4
        return intrinsic + time_value
    else:
        intrinsic = max(K - S, 0.0)
        time_value = sigma * math.sqrt(T) * K * 0.4
        return intrinsic + time_value


def estimate_option_pnl(
    entry_price: float,
    exit_price: float,
    strike: float,
    dte_entry: int,
    dte_exit: int,
    iv_entry: float,
    iv_exit: float,
    option_type: str = 'call',
    use_bs: bool = True
) -> float:
    """
    估算期权 P&L

    使用 Black-Scholes 估价入场和出场时的期权价格，计算差值。

    Args:
        entry_price: 入场时股价
        exit_price: 出场时股价
        strike: 行权价
        dte_entry: 入场时剩余天数
        dte_exit: 出场时剩余天数
        iv_entry: 入场时IV（小数，如0.25=25%）
        iv_exit: 出场时IV
        option_type: 'call' 或 'put'
        use_bs: 是否使用 greeks_engine（True则回退到简化版本）

    Returns:
        期权P&L（百分比）
    """
    # 将DTE转换为年化时间
    T_entry = max(dte_entry / 365.0, 0.001)
    T_exit = max(dte_exit / 365.0, 0.001)
    r = 0.05  # 假设5%无风险率

    # 选择定价函数
    price_fn = bs_price if (use_bs and bs_price) else _fallback_bs_price

    try:
        # 入场期权价格
        option_price_entry = price_fn(entry_price, strike, T_entry, r, iv_entry, option_type)
        # 出场期权价格
        option_price_exit = price_fn(exit_price, strike, T_exit, r, iv_exit, option_type)

        # P&L（百分比）
        if option_price_entry <= 0:
            return 0.0
        pnl_pct = ((option_price_exit - option_price_entry) / option_price_entry) * 100.0
        return pnl_pct
    except Exception as e:
        _log.warning("期权P&L估算失败: %s", e)
        return 0.0


def estimate_spread_pnl(
    entry_price: float,
    exit_price: float,
    strike_long: float,
    strike_short: float,
    dte_entry: int,
    dte_exit: int,
    iv_entry: float,
    iv_exit: float,
    option_type: str = 'call'
) -> float:
    """
    估算价差策略 P&L（例：牛市call价差）

    long_leg - short_leg 的净P&L

    Args:
        entry_price: 入场股价
        exit_price: 出场股价
        strike_long: 长腿行权价（更有利的）
        strike_short: 短腿行权价（更不利的）
        dte_entry: 入场DTE
        dte_exit: 出场DTE
        iv_entry: 入场IV
        iv_exit: 出场IV
        option_type: 'call' 或 'put'

    Returns:
        净P&L百分比
    """
    T_entry = max(dte_entry / 365.0, 0.001)
    T_exit = max(dte_exit / 365.0, 0.001)
    r = 0.05

    price_fn = bs_price if (bs_price) else _fallback_bs_price

    try:
        # Long leg
        long_entry = price_fn(entry_price, strike_long, T_entry, r, iv_entry, option_type)
        long_exit = price_fn(exit_price, strike_short, T_exit, r, iv_exit, option_type)

        # Short leg
        short_entry = price_fn(entry_price, strike_short, T_entry, r, iv_entry, option_type)
        short_exit = price_fn(exit_price, strike_short, T_exit, r, iv_exit, option_type)

        # 净成本（net debit 或 net credit）
        net_entry = long_entry - short_entry
        net_exit = long_exit - short_exit

        if net_entry <= 0:
            return 0.0

        return ((net_exit - net_entry) / net_entry) * 100.0
    except Exception as e:
        _log.warning("价差P&L估算失败: %s", e)
        return 0.0


# ============================================================================
# 主类：OptionsBacktester
# ============================================================================

class OptionsBacktester:
    """
    期权策略回溯测试器

    从 report_snapshots 加载历史推荐信号，模拟各策略的表现。
    """

    def __init__(self, snapshots_dir: str = "report_snapshots"):
        """
        初始化回溯测试器

        Args:
            snapshots_dir: 报告快照目录
        """
        self.snapshots_dir = snapshots_dir
        self.snapshots = self._load_snapshots()
        _log.info("加载 %d 份报告快照", len(self.snapshots))

    def _load_snapshots(self) -> List[Dict[str, Any]]:
        """加载所有快照JSON"""
        snapshots = []

        if not os.path.exists(self.snapshots_dir):
            _log.warning("快照目录不存在: %s", self.snapshots_dir)
            return snapshots

        for filename in sorted(os.listdir(self.snapshots_dir)):
            if not filename.endswith(".json"):
                continue

            try:
                with open(os.path.join(self.snapshots_dir, filename), 'r') as f:
                    data = json.load(f)
                snapshots.append(data)
            except (json.JSONDecodeError, OSError) as e:
                _log.debug("加载快照失败 %s: %s", filename, e)

        return snapshots

    def _classify_regime(self, snapshot: Dict[str, Any]) -> MarketRegime:
        """
        根据快照数据分类市场政体

        简单逻辑：
          - iv_rank > 70 → high_iv，否则 low_iv
          - composite_score > 5.5 且 direction=bearish → 反向判断
          - 最终判断：low_iv_bull/bear 或 high_iv_bull/bear

        Args:
            snapshot: 报告快照

        Returns:
            MarketRegime 枚举
        """
        # 从快照提取数据
        composite = snapshot.get("composite_score", 5.0)
        direction = snapshot.get("direction", "neutral").lower()

        # 简化版：用composite_score的50分位作为IV参考
        # 实际应读取 iv_rank，这里用composite_score近似
        iv_high = composite > 6.0

        # 方向判断
        is_bullish = direction in ["bullish", "long"]

        if iv_high:
            if is_bullish:
                return MarketRegime.HIGH_IV_BULL
            else:
                return MarketRegime.HIGH_IV_BEAR
        else:
            if is_bullish:
                return MarketRegime.LOW_IV_BULL
            else:
                return MarketRegime.LOW_IV_BEAR

    def _map_signal_to_strategy(self, snapshot: Dict[str, Any]) -> Optional[StrategyType]:
        """
        根据信号评分和方向映射到推荐策略

        逻辑：
          - score > 7.5 且 direction=long → long_call 或 bull_call_spread
          - score < 4.0 且 direction=short → long_put 或 bear_put_spread
          - score 5-6 且高IV → iron_condor（卖权利金）
          - catalyst + high_iv → straddle

        Args:
            snapshot: 报告快照

        Returns:
            StrategyType 或 None
        """
        score = snapshot.get("composite_score", 5.0)
        direction = snapshot.get("direction", "neutral").lower()

        if score > 7.5 and direction == "bullish":
            # 强多头：选择 bull_call_spread（风险有限）
            return StrategyType.BULL_CALL_SPREAD
        elif score < 4.0 and direction == "bearish":
            # 强空头：选择 bear_put_spread
            return StrategyType.BEAR_PUT_SPREAD
        elif 4.5 < score < 5.5 and score > 5.0:
            # 高IV中性：铁秃鹰卖权利金
            return StrategyType.IRON_CONDOR
        elif direction == "bullish" and 6.0 <= score <= 7.5:
            # 温和多头
            return StrategyType.LONG_CALL
        elif direction == "bearish" and 4.0 <= score < 6.0:
            # 温和空头
            return StrategyType.LONG_PUT

        return None

    def _estimate_strikes_from_price(
        self,
        stock_price: float,
        strategy: StrategyType
    ) -> Tuple[float, float]:
        """
        根据策略类型估算行权价

        ATM（平价）和 OTM（虚值）行权价的简化计算

        Args:
            stock_price: 当前股价
            strategy: 策略类型

        Returns:
            (strike_call, strike_put) 元组
        """
        # ATM 行权价（接近当前价格的整数）
        atm = round(stock_price / 5.0) * 5.0

        # 根据策略类型确定OTM偏移
        if strategy == StrategyType.BULL_CALL_SPREAD:
            # 买ATM call，卖5% OTM call
            strike_call = atm
            strike_short = atm * 1.05
            return (strike_call, strike_short)
        elif strategy == StrategyType.BEAR_PUT_SPREAD:
            # 买ATM put，卖5% OTM put
            strike_put = atm
            strike_short = atm * 0.95
            return (strike_short, strike_put)
        elif strategy == StrategyType.IRON_CONDOR:
            # 卖ATM 跨式，买 10% OTM 翅膀
            return (atm * 1.10, atm * 0.90)
        elif strategy == StrategyType.STRADDLE:
            # 买ATM call & put
            return (atm, atm)
        else:
            # Long call / Long put
            return (atm, atm)

    def backtest_strategy(
        self,
        strategy: StrategyType,
        predictions: Optional[List[Dict[str, Any]]] = None,
        horizon: str = "t7"
    ) -> StrategyBacktestResult:
        """
        回溯测试单个策略

        Args:
            strategy: 策略类型
            predictions: 预测列表（若为None则使用所有快照）
            horizon: 回溯周期 "t1" / "t7" / "t30"

        Returns:
            StrategyBacktestResult 对象，包含所有交易记录和统计
        """
        result = StrategyBacktestResult(strategy=strategy)

        # 使用所有快照或指定的预测
        data = predictions if predictions else self.snapshots

        for snapshot in data:
            ticker = snapshot.get("ticker", "UNKNOWN")
            date = snapshot.get("date", "")
            entry_price = snapshot.get("entry_price", 0.0)

            if entry_price <= 0:
                continue

            # 读取目标时间段的实际价格
            actual_prices = snapshot.get("actual_prices", {})
            exit_price = actual_prices.get(horizon)

            if exit_price is None or exit_price <= 0:
                continue

            # 计算收益率（股票层面）
            stock_pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
            stock_pnl_dollar = exit_price - entry_price

            # 估算政体和IV
            regime = self._classify_regime(snapshot)
            iv_entry = snapshot.get("composite_score", 5.0) / 10.0  # 简化近似
            iv_exit = iv_entry * (1.0 - 0.1 * (horizon == "t7" and 1 or 0))  # T+7略降

            # 计算DTE
            dte_entry = 21  # 假设每次入场都是3周期权
            dte_exit = max(dte_entry - (1 if horizon == "t1" else 7 if horizon == "t7" else 30), 1)

            # 估算行权价
            strike_call, strike_put = self._estimate_strikes_from_price(entry_price, strategy)

            # 根据策略计算期权P&L
            if strategy == StrategyType.LONG_CALL:
                option_pnl = estimate_option_pnl(
                    entry_price, exit_price, strike_call,
                    dte_entry, dte_exit, iv_entry, iv_exit,
                    option_type='call'
                )
            elif strategy == StrategyType.LONG_PUT:
                option_pnl = estimate_option_pnl(
                    entry_price, exit_price, strike_put,
                    dte_entry, dte_exit, iv_entry, iv_exit,
                    option_type='put'
                )
            elif strategy == StrategyType.BULL_CALL_SPREAD:
                option_pnl = estimate_spread_pnl(
                    entry_price, exit_price, strike_call, strike_put,
                    dte_entry, dte_exit, iv_entry, iv_exit,
                    option_type='call'
                )
            elif strategy == StrategyType.BEAR_PUT_SPREAD:
                option_pnl = estimate_spread_pnl(
                    entry_price, exit_price, strike_put, strike_call,
                    dte_entry, dte_exit, iv_entry, iv_exit,
                    option_type='put'
                )
            elif strategy == StrategyType.IRON_CONDOR:
                # 铁秃鹰：卖出IV溢价
                option_pnl = -iv_entry * 100.0 * 0.5  # 粗估
            elif strategy == StrategyType.STRADDLE:
                # 跨式：长 call + 长 put，受益于大波动
                call_pnl = estimate_option_pnl(
                    entry_price, exit_price, strike_call,
                    dte_entry, dte_exit, iv_entry, iv_exit,
                    option_type='call'
                )
                put_pnl = estimate_option_pnl(
                    entry_price, exit_price, strike_put,
                    dte_entry, dte_exit, iv_entry, iv_exit,
                    option_type='put'
                )
                option_pnl = (call_pnl + put_pnl) / 2.0
            else:
                option_pnl = 0.0

            # 最大回撤（简化：入场到出场的最差情况）
            max_dd = min(0.0, -abs(stock_pnl_pct * 0.3))  # 期权杠杆大约3倍衰减

            # 组织交易记录
            trade = StrategyResult(
                strategy=strategy,
                entry_price=entry_price,
                entry_date=date,
                exit_price=exit_price,
                exit_date="",  # 计算得出
                dte_entry=dte_entry,
                dte_exit=dte_exit,
                iv_entry=iv_entry,
                iv_exit=iv_exit,
                strike_call=strike_call,
                strike_put=strike_put,
                pnl_dollar=option_pnl * entry_price / 100.0,
                pnl_pct=option_pnl,
                max_drawdown=max_dd,
                win=option_pnl > 0,
                ticker=ticker,
                regime=regime.value
            )

            result.trades.append(trade)

        return result

    def backtest_all_strategies(
        self,
        predictions: Optional[List[Dict[str, Any]]] = None,
        horizon: str = "t7"
    ) -> Dict[str, StrategyBacktestResult]:
        """
        回溯测试所有策略

        Args:
            predictions: 预测列表
            horizon: 回溯周期

        Returns:
            字典：{策略名 -> 回溯结果}
        """
        results = {}

        for strategy_type in StrategyType:
            result = self.backtest_strategy(strategy_type, predictions, horizon)
            results[strategy_type.value] = result

        return results

    def find_best_strategy_by_regime(
        self,
        predictions: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """
        按政体分类，找出每个政体下表现最好的策略

        Args:
            predictions: 预测列表

        Returns:
            字典：{政体 -> {best_strategy, metrics}}
        """
        data = predictions if predictions else self.snapshots

        # 按政体分组
        regime_trades = {}
        for snapshot in data:
            regime = self._classify_regime(snapshot)
            strategy = self._map_signal_to_strategy(snapshot)

            if strategy is None:
                continue

            if regime.value not in regime_trades:
                regime_trades[regime.value] = {}
            if strategy.value not in regime_trades[regime.value]:
                regime_trades[regime.value][strategy.value] = []

            # 计算该交易的P&L
            entry_price = snapshot.get("entry_price", 0.0)
            actual_prices = snapshot.get("actual_prices", {})
            exit_price = actual_prices.get("t7")

            if entry_price > 0 and exit_price is not None and exit_price > 0:
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
                regime_trades[regime.value][strategy.value].append(pnl_pct)

        # 计算每个政体的最佳策略
        best_by_regime = {}
        for regime, strategies in regime_trades.items():
            best_strategy = None
            best_winrate = -1.0

            for strat, pnls in strategies.items():
                wins = sum(1 for p in pnls if p > 0)
                winrate = wins / len(pnls) if pnls else 0.0
                avg_return = mean(pnls) if pnls else 0.0

                # 简单评分：胜率权重70%，平均收益权重30%
                score = winrate * 0.7 + (avg_return / 10.0) * 0.3

                if score > best_winrate:
                    best_winrate = score
                    best_strategy = strat

            if best_strategy:
                pnls = strategies[best_strategy]
                best_by_regime[regime] = {
                    "strategy": best_strategy,
                    "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4),
                    "avg_return_pct": round(mean(pnls), 4),
                    "num_trades": len(pnls)
                }

        return best_by_regime

    def generate_strategy_report(self, horizon: str = "t7") -> str:
        """
        生成策略回溯报告

        Args:
            horizon: 回溯周期

        Returns:
            格式化的报告字符串
        """
        results = self.backtest_all_strategies(horizon=horizon)
        regime_best = self.find_best_strategy_by_regime()

        report = []
        report.append("=" * 80)
        report.append("期权策略回溯测试报告")
        report.append(f"生成时间: {datetime.now().isoformat()}")
        report.append(f"回溯周期: {horizon}")
        report.append("=" * 80)
        report.append("")

        # 全策略统计
        report.append("【全策略统计】")
        report.append("-" * 80)

        for strat_name in sorted(results.keys()):
            res = results[strat_name]

            if res.num_trades == 0:
                report.append(f"{strat_name}: 无数据")
                continue

            report.append(f"{strat_name}:")
            report.append(f"  交易数: {res.num_trades}")
            report.append(f"  胜率: {res.win_rate * 100:.2f}%")
            report.append(f"  平均收益: {res.avg_return:.2f}%")
            report.append(f"  夏普比: {res.sharpe_ratio:.4f}")
            report.append(f"  最大回撤: {res.max_drawdown:.2f}%")
            report.append(f"  利润因子: {res.profit_factor:.4f}")
            report.append(f"  总P&L: ${res.total_pnl:.2f}")
            report.append("")

        # 按政体最优策略
        report.append("【按政体最优策略】")
        report.append("-" * 80)

        for regime in sorted(regime_best.keys()):
            info = regime_best[regime]
            report.append(f"{regime}:")
            report.append(f"  推荐策略: {info['strategy']}")
            report.append(f"  胜率: {info['win_rate'] * 100:.2f}%")
            report.append(f"  平均收益: {info['avg_return_pct']:.2f}%")
            report.append(f"  样本数: {info['num_trades']}")
            report.append("")

        return "\n".join(report)

    def inject_strategy_results_to_report(
        self,
        report_dict: Dict[str, Any],
        horizon: str = "t7"
    ) -> Dict[str, Any]:
        """
        将策略回溯结果注入到报告字典中

        供 CH6 场景建议使用

        Args:
            report_dict: 报告字典
            horizon: 回溯周期

        Returns:
            增强后的报告字典
        """
        results = self.backtest_all_strategies(horizon=horizon)
        regime_best = self.find_best_strategy_by_regime()

        strategy_metrics = {}
        for strat_name, res in results.items():
            strategy_metrics[strat_name] = res.to_dict()

        report_dict["strategy_backtest"] = {
            "horizon": horizon,
            "generated_at": datetime.now().isoformat(),
            "all_strategies": strategy_metrics,
            "best_by_regime": regime_best,
            "summary": self.generate_strategy_report(horizon)
        }

        return report_dict


# ============================================================================
# 演示和单元测试
# ============================================================================

if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 实例化回溯测试器
    backtester = OptionsBacktester(snapshots_dir="/sessions/focused-kind-johnson/mnt/Alpha Hive/report_snapshots")

    print("=" * 80)
    print("期权策略回溯测试演示")
    print("=" * 80)
    print("")

    # 1. 回溯所有策略
    print("【执行全策略回溯 (T+7)】")
    print("")
    results = backtester.backtest_all_strategies(horizon="t7")

    for strat_name, res in results.items():
        print(f"{strat_name}:")
        print(f"  交易数: {res.num_trades}")
        if res.num_trades > 0:
            print(f"  胜率: {res.win_rate * 100:.2f}%")
            print(f"  平均收益: {res.avg_return:.2f}%")
            print(f"  利润因子: {res.profit_factor:.4f}")
        print("")

    # 2. 按政体找最优策略
    print("【按政体最优策略】")
    print("")
    regime_best = backtester.find_best_strategy_by_regime()

    for regime, info in sorted(regime_best.items()):
        print(f"{regime}:")
        print(f"  最优策略: {info['strategy']}")
        print(f"  胜率: {info['win_rate'] * 100:.2f}%")
        print(f"  平均收益: {info['avg_return_pct']:.2f}%")
        print("")

    # 3. 生成完整报告
    print("【完整报告】")
    print("")
    report_text = backtester.generate_strategy_report(horizon="t7")
    print(report_text)

    # 4. 保存报告
    report_path = "/sessions/focused-kind-johnson/mnt/Alpha Hive/strategy_backtest_report.txt"
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)
    print(f"\n报告已保存至: {report_path}")
