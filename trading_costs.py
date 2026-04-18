#!/usr/bin/env python3
"""
💰 Alpha Hive - 交易成本模型 (Sprint 1 / P0-2)

把"纸面收益"翻译成"真实可交易收益"：
- 滑点 (slippage)：进/出各一次，按标的流动性分档
- 佣金 (commission)：IB 标准 ~0.5bp/边
- 借券费 (borrow cost)：仅看空承担，按年化费率 × 持仓天数
- SEC/FINRA fee：微小，合并进 commission

使用示例：
    from trading_costs import apply_costs
    result = apply_costs(
        gross_return_pct=11.06,
        direction="bearish",
        ticker="BILI",
        holding_days=7,
    )
    # result = {"net_return_pct": 10.38, "cost_pct": 0.68, "breakdown": {...}}
"""

from __future__ import annotations

import logging as _logging
from typing import Dict, Optional

_log = _logging.getLogger("alpha_hive.trading_costs")

try:
    import config as _config
    _COST_CFG = getattr(_config, "TRADING_COSTS_CONFIG", {})
except Exception:
    _COST_CFG = {}


def _get_slippage_bps(ticker: str) -> float:
    """按标的返回单边滑点（bp）。"""
    per_ticker = _COST_CFG.get("slippage_bps_by_ticker", {}) or {}
    return float(per_ticker.get(ticker, _COST_CFG.get("slippage_bps_default", 10)))


def _get_borrow_rate(ticker: str) -> float:
    """按标的返回年化借券费率（%）。"""
    rates = _COST_CFG.get("borrow_rates", {}) or {}
    return float(rates.get(ticker, _COST_CFG.get("borrow_rate_default", 3.0)))


def apply_costs(
    gross_return_pct: float,
    direction: str,
    ticker: str,
    holding_days: int,
    override_slippage_bps: Optional[float] = None,
    holding_calendar_days: Optional[int] = None,
) -> Dict:
    """把毛收益扣成本得到净收益。

    Args:
        gross_return_pct: 方向调整后的毛收益率（百分比，如 11.06）
        direction: "bullish" / "bearish" / "neutral"
        ticker: 标的代码（用于查滑点/借券率）
        holding_days: 持仓"交易日"（通常 7；backtester 按 OHLC bar 递增）
        override_slippage_bps: 覆盖滑点（例如 intraday 止损触发时要加 exit 滑点）
        holding_calendar_days: 持仓"自然日"（可选；若 None 则按 trading_days × 1.4 换算）
            修复 Bug #17：借券费按自然日计，旧实现用交易日导致费用低估 30-40%

    Returns:
        {
            "net_return_pct": float,      # 扣成本后净收益
            "cost_pct": float,            # 总成本（百分比）
            "breakdown": {
                "slippage_pct": float,
                "commission_pct": float,
                "borrow_pct": float,
            }
        }
    """
    if not _COST_CFG.get("enabled", True):
        return {
            "net_return_pct": gross_return_pct,
            "cost_pct": 0.0,
            "breakdown": {"slippage_pct": 0.0, "commission_pct": 0.0, "borrow_pct": 0.0},
        }

    _dir = (direction or "").strip().lower()

    # ── 1) 滑点：进 + 出 双边 ──
    # 1 bp = 0.01 %
    one_side_bps = override_slippage_bps if override_slippage_bps is not None else _get_slippage_bps(ticker)
    slippage_pct = 2.0 * one_side_bps / 100.0  # 双边

    # ── 2) 佣金 (pct, 双边) ──
    commission_pct = float(_COST_CFG.get("commission_pct_per_side", 0.01))

    # ── 3) 借券费（仅 short）── 修复 Bug #17：按自然日而非交易日
    borrow_pct = 0.0
    if _dir == "bearish":
        annual_rate = _get_borrow_rate(ticker)
        # 交易日 → 自然日换算：1 周 5 交易日 = 7 自然日，比例 1.4
        # 若调用方传了 calendar_days 则优先使用
        if holding_calendar_days is not None and holding_calendar_days > 0:
            cal_days = int(holding_calendar_days)
        else:
            cal_days = max(int(round(holding_days * 1.4)), 1)
        borrow_pct = annual_rate * cal_days / 365.0

    total_cost = slippage_pct + commission_pct + borrow_pct
    net_ret = gross_return_pct - total_cost

    return {
        "net_return_pct": round(net_ret, 4),
        "cost_pct": round(total_cost, 4),
        "breakdown": {
            "slippage_pct": round(slippage_pct, 4),
            "commission_pct": round(commission_pct, 4),
            "borrow_pct": round(borrow_pct, 4),
        },
    }


def sharpe_ratio(
    returns_pct: list,
    periods_per_year: int = 36,
    risk_free_pct: Optional[float] = None,
) -> Optional[float]:
    """年化 Sharpe ratio（返回百分比形式）。

    修复 Bug #8：T+7 采样是"7 交易日"而非"7 自然日"，一年 252/7=36 次采样（不是 52 周）。
    旧默认值 52 让 √n 放大系数错位，系统性高估 Sharpe ~20% (√52/√36 = 1.20)

    Args:
        returns_pct: 每笔/每周期收益率（百分比）
        periods_per_year: T+7 策略 ≈ 36 / T+1 ≈ 252 / 日度 ≈ 252
        risk_free_pct: 年化无风险利率（%）
    """
    if not returns_pct or len(returns_pct) < 2:
        return None
    if risk_free_pct is None:
        risk_free_pct = float(_COST_CFG.get("risk_free_rate_pct", 4.5))

    import statistics as _stats
    mean_r = _stats.mean(returns_pct)
    std_r = _stats.pstdev(returns_pct)
    if std_r <= 0:
        return None

    period_rf = risk_free_pct / periods_per_year
    excess = mean_r - period_rf
    sharpe_period = excess / std_r
    return round(sharpe_period * (periods_per_year ** 0.5), 3)


if __name__ == "__main__":
    # 自测
    import json as _json
    print("【BILI 看空 7 天 +11.06% 毛收益】")
    print(_json.dumps(apply_costs(11.06, "bearish", "BILI", 7), indent=2, ensure_ascii=False))
    print("\n【NVDA 看多 7 天 +5.00% 毛收益】")
    print(_json.dumps(apply_costs(5.00, "bullish", "NVDA", 7), indent=2, ensure_ascii=False))
    print("\n【VKTX 看空 7 天 +8.00% 毛收益】")
    print(_json.dumps(apply_costs(8.00, "bearish", "VKTX", 7), indent=2, ensure_ascii=False))
