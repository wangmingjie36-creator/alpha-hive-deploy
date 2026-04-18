#!/usr/bin/env python3
"""
💰 Alpha Hive — $50K 股票现货策略回测（Portfolio-Level Backtest）

从 pheromone.db 的 191 条已验证 T+7 预测记录，模拟真实组合运营：
- $50,000 起始资金
- 每笔仓位 = NAV × position_size_pct（默认 10%）
- 最多同时持仓 max_concurrent（默认 5）
- 入场门槛：bull ≥ 6.5 / bear ≤ 3.5
- 出场：Sprint 1 已算好的路径依赖退出（SL/TP/T7_CLOSE）
- 成本：Sprint 1 已算好的 net_return_t7（含滑点+佣金+借券费）
- 对照：SPY 同期买入持有

口径说明：
- 标的是股票现货（bull→买入，bear→融券卖空），不是期权
- 所有收益数据来自 Sprint 1 backfill（path-dependent + trading costs）

用法:
    python3 portfolio_backtest.py                  # 默认参数
    python3 portfolio_backtest.py --capital 50000  # 指定起始资金
    python3 portfolio_backtest.py --max-pos 5      # 最多同时持仓 5
    python3 portfolio_backtest.py --min-score-bull 7.0 --min-score-bear 3.0
    python3 portfolio_backtest.py --all            # 不筛选，全部入场
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BacktestConfig:
    """
    v0.22.1 方案 A — 放宽筛选（扩样本从 11 笔→ 预期 50-80 笔）

    Bootstrap + FF 归因发现：过严筛选把原始信号的 α (+166% p=0.015)
    剃成了低 beta 中性化组合（α -12% p=0.53）。本次放宽：
      max_agent_std: 1.5 → 2.5    (允许分歧信号 = alpha 源)
      min_score_bull: 6.5 → 5.5   (不再只要"共识最强"票)
      min_score_bear: 4.5 → 5.5   (镜像，看空也放宽)
      accept_neutral: False → True(中性 40 笔可能含真 alpha)
      max_concurrent: 5 → 15      ($50K × 10% × 15 = 150% 受 gross_exposure 保护)
    """
    initial_capital: float = 50_000.0
    position_size_pct: float = 0.10        # 每笔 = NAV × 10%
    horizon: int = 7                       # v0.22.2: 持仓期 1/7/30，对比"固定 T+7 是否太死"
    max_concurrent: int = 15               # v0.22.1: 5→15（gross_exposure 已防杠杆）
    min_score_bull: float = 5.5            # v0.22.1: 6.5→5.5（放宽看多门槛）
    min_score_bear: float = 5.5            # v0.22.1: 4.5→5.5（镜像对称）
    accept_neutral: bool = True            # v0.22.1: False→True（中性含 alpha）
    take_all: bool = False                 # 不筛选，所有预测都入场
    benchmark_ticker: str = "SPY"
    # ── 升级 1: Agent 共识门控 ──
    max_agent_std: float = 2.5             # v0.22.1: 1.5→2.5（允许分歧信号）
    # ── 升级 3: 方向不对称仓位 ──
    bull_size_pct: float = 0.08            # 看多仓位 = NAV × 8%
    bear_size_pct: float = 0.12            # 看空仓位 = NAV × 12%
    # ── 升级 4: 宏观门控 ──
    macro_gate: bool = True                # 启用宏观政体门控
    spy_ma_days: int = 20                  # SPY 均线天数
    spy_below_ma_pct: float = 3.0          # SPY < MA × (1 - pct%) → risk-off


# ══════════════════════════════════════════════════════════════════════════════
# 数据加载
# ══════════════════════════════════════════════════════════════════════════════

def _find_db() -> Path:
    """定位 pheromone.db"""
    base = Path(__file__).parent
    for name in ["pheromone.db", "hive_predictions.db"]:
        p = base / name
        if p.exists():
            return p
    raise FileNotFoundError("找不到 pheromone.db")


def load_verified_predictions(horizon: int = 7) -> List[Dict]:
    """加载已验证的预测记录，按 horizon 决定使用哪个 price_tN / return_tN 列

    horizon=7:  使用 price_t7/return_t7/net_return_t7（已有回填，路径依赖 SL/TP）
    horizon=1:  使用 price_t1/return_t1，动态计算 net_return_t1（无路径依赖，纯 T+1 收盘）
    horizon=30: 使用 price_t30/return_t30，动态计算 net_return_t30（样本 76 笔，2-3 月初）

    统一输出字段：`return_t7`、`net_return_t7` 等，让下游 portfolio_backtest 逻辑不变
    （只是"t7"字段里装的是 horizon 日后的真实数据）
    """
    db = _find_db()
    if horizon == 7:
        # 原有逻辑：使用 Sprint 1 回填的路径依赖 SL/TP/T7_CLOSE 数据
        query = """
            SELECT id, date, ticker, direction, final_score,
                   price_at_predict, return_t7, net_return_t7,
                   exit_reason, exit_date, exit_price,
                   holding_days, cost_breakdown, spy_return_t7,
                   dimension_scores
            FROM predictions
            WHERE checked_t7 = 1 AND net_return_t7 IS NOT NULL
            ORDER BY date ASC, id ASC
        """
    elif horizon == 1:
        query = """
            SELECT id, date, ticker, direction, final_score,
                   price_at_predict,
                   return_t1 AS return_t7,
                   price_t1 AS _price_tN,
                   dimension_scores
            FROM predictions
            WHERE checked_t1 = 1 AND return_t1 IS NOT NULL AND price_t1 IS NOT NULL
            ORDER BY date ASC, id ASC
        """
    elif horizon == 30:
        query = """
            SELECT id, date, ticker, direction, final_score,
                   price_at_predict,
                   return_t30 AS return_t7,
                   price_t30 AS _price_tN,
                   dimension_scores
            FROM predictions
            WHERE checked_t30 = 1 AND return_t30 IS NOT NULL AND price_t30 > 0
            ORDER BY date ASC, id ASC
        """
    else:
        raise ValueError(f"不支持的 horizon={horizon}（仅支持 1/7/30）")

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query).fetchall()
    preds = [dict(r) for r in rows]

    # horizon != 7 时：动态计算 net_return + 伪造 exit 字段（让下游逻辑不变）
    if horizon != 7:
        from trading_costs import apply_costs
        from datetime import datetime as _dt, timedelta as _td

        # v0.23.2 修复 #6：预拉一次 SPY 同期收益，替代原 spy_return_t7=0.0 硬编码
        # 原实现让 Alpha vs SPY 永远等于 strategy_return 本身，严重误导结果
        spy_same_period: Dict[tuple, float] = {}  # (entry_date, horizon) → spy_return_pct
        try:
            import yfinance as _yf
            if preds:
                min_date = min(p["date"] for p in preds if p.get("date"))
                max_date = max(p["date"] for p in preds if p.get("date"))
                _end_dt = _dt.strptime(max_date, "%Y-%m-%d") + _td(days=int(horizon * 1.6) + 5)
                spy_hist = _yf.Ticker("SPY").history(
                    start=_dt.strptime(min_date, "%Y-%m-%d").date(),
                    end=_end_dt.date(),
                    auto_adjust=True,
                )
                spy_closes = {idx.strftime("%Y-%m-%d"): float(row["Close"])
                              for idx, row in spy_hist.iterrows()}
                sorted_spy = sorted(spy_closes.keys())
                for p in preds:
                    ed = p.get("date")
                    if not ed or ed not in spy_closes:
                        continue
                    # 找 ed 之后 horizon 个交易日
                    fut = [d for d in sorted_spy if d > ed]
                    if len(fut) < 1:
                        continue
                    target = fut[min(horizon - 1, len(fut) - 1)]
                    spy_same_period[(ed, horizon)] = (
                        (spy_closes[target] - spy_closes[ed]) / spy_closes[ed] * 100.0
                    )
        except Exception as _spy_e:
            import logging as _lg
            _lg.getLogger(__name__).debug(f"SPY horizon={horizon} 同期收益拉取失败: {_spy_e}")

        for p in preds:
            direction = (p.get("direction") or "").strip().lower()
            raw_ret = float(p.get("return_t7") or 0)  # 已 alias 为 horizon 日收益

            # 方向调整的毛收益
            if "bear" in direction:
                gross_adj = -raw_ret
            else:
                gross_adj = raw_ret

            # 应用交易成本（按 horizon 天数）
            cost_result = apply_costs(
                gross_return_pct=gross_adj,
                direction=direction if direction in ("bullish", "bearish") else "neutral",
                ticker=p.get("ticker", ""),
                holding_days=horizon,
            )
            p["net_return_t7"] = cost_result["net_return_pct"]
            p["exit_reason"] = f"T{horizon}_CLOSE"
            p["holding_days"] = horizon
            # v0.23.2 修复 #6-2：exit_date 解析失败时降级到 entry_date + horizon 自然日
            # 且避免赋空串（会导致下游 WINDOW_CUTOFF 静默丢 PnL）
            try:
                entry_dt = _dt.strptime(p["date"], "%Y-%m-%d")
                exit_dt = entry_dt + _td(days=int(round(horizon * 1.4)))
                p["exit_date"] = exit_dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError, KeyError):
                # 无法解析时强制丢弃这条（return None 让调用方跳过）
                p["exit_date"] = None
                continue
            p["exit_price"] = float(p.get("_price_tN") or 0)
            p["cost_breakdown"] = json.dumps(cost_result.get("breakdown", {}))
            # v0.23.2 修复 #6：用真实 SPY 同期替代硬编码 0
            p["spy_return_t7"] = spy_same_period.get((p.get("date"), horizon), 0.0)

        # 过滤掉 exit_date=None 的（无法解析 entry_date）
        preds = [p for p in preds if p.get("exit_date") is not None]
    return preds


def _calc_dim_std(pred: Dict) -> Optional[float]:
    """从 dimension_scores JSON 计算 5 维分值标准差"""
    raw = pred.get("dimension_scores")
    if not raw:
        return None
    try:
        scores = json.loads(raw) if isinstance(raw, str) else raw
        vals = [float(v) for v in scores.values() if v is not None]
        if len(vals) < 3:
            return None
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return math.sqrt(var)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SPY 基准
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_spy_prices(start_date: str, end_date: str) -> Dict[str, float]:
    """获取 SPY 在 [start-40d, end+buffer] 区间的每日收盘价（多拉40天用于计算MA）"""
    try:
        import yfinance as yf
        spy = yf.Ticker("SPY")
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=60)  # 多拉60天给MA
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=10)
        hist = spy.history(start=start_dt.strftime("%Y-%m-%d"),
                          end=end_dt.strftime("%Y-%m-%d"))
        if hist is None or hist.empty:
            return {}
        return {idx.strftime("%Y-%m-%d"): float(row["Close"])
                for idx, row in hist.iterrows()}
    except Exception:
        return {}


def _spy_ma(spy_prices: Dict[str, float], as_of: str, ma_days: int = 20) -> Optional[float]:
    """计算 as_of 日期的 SPY N日均线（用 as_of 当日及之前的收盘价）"""
    sorted_dates = sorted(d for d in spy_prices.keys() if d <= as_of)
    if len(sorted_dates) < ma_days:
        return None
    recent = sorted_dates[-ma_days:]
    return sum(spy_prices[d] for d in recent) / ma_days


def _is_risk_off(spy_prices: Dict[str, float], as_of: str,
                 ma_days: int = 20, below_pct: float = 3.0) -> bool:
    """判断 as_of 日是否处于 risk-off（SPY < MA × (1 - pct%)）"""
    ma = _spy_ma(spy_prices, as_of, ma_days)
    if ma is None or ma <= 0:
        return False
    spy_close = spy_prices.get(as_of)
    if spy_close is None:
        # 找最近的交易日
        recent = [d for d in sorted(spy_prices.keys()) if d <= as_of]
        if not recent:
            return False
        spy_close = spy_prices[recent[-1]]
    threshold = ma * (1 - below_pct / 100.0)
    return spy_close < threshold


# ══════════════════════════════════════════════════════════════════════════════
# 虚拟持仓追踪
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LivePosition:
    pred_id: int
    ticker: str
    direction: str
    entry_date: str
    exit_date: str           # 来自 Sprint 1 计算
    holding_days: int
    size_usd: float          # 建仓名义金额
    gross_return_pct: float  # return_t7（raw 价格变动）
    net_return_pct: float    # net_return_t7（扣成本后）
    exit_reason: str
    score: float
    spy_return_pct: float    # 同期 SPY 回报


@dataclass
class DailySnapshot:
    date: str
    cash: float
    invested: float          # 在仓名义金额
    nav: float               # cash + invested（mark-to-market 简化为成本基础）
    active_positions: int
    trades_opened: int       # 当日新开
    trades_closed: int       # 当日平仓
    realized_pnl: float      # 当日实现损益
    cum_realized_pnl: float  # 累计实现损益
    spy_price: float         # SPY 当日收盘


# ══════════════════════════════════════════════════════════════════════════════
# 回测引擎
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(cfg: BacktestConfig) -> Dict:
    """执行 $50K 组合回测，返回完整结果"""

    preds = load_verified_predictions(horizon=cfg.horizon)
    if not preds:
        return {"error": "无已验证预测数据"}

    # 按日期分组
    by_date: Dict[str, List[Dict]] = defaultdict(list)
    for p in preds:
        by_date[p["date"]].append(p)

    entry_dates = sorted(by_date.keys())
    first_date = entry_dates[0]
    # all_dates 必须延伸到最后一个 exit_date（否则 T+30 的仓位会被 WINDOW_CUTOFF 全吃）
    # price_t{N} 是真实已观测价，不存在 look-ahead
    last_exit = max(
        (p.get("exit_date") or p["date"]) for p in preds
    )
    last_date = max(entry_dates[-1], last_exit)

    # 生成每日序列（从 first_date 到 last_date 的所有交易日近似 — 用 entry_dates 和 exit_dates 并集）
    exit_set = set()
    for p in preds:
        ed = p.get("exit_date")
        if ed:
            exit_set.add(ed)
    all_dates = sorted(set(entry_dates) | exit_set)

    # SPY 基准
    spy_prices = _fetch_spy_prices(first_date, last_date)

    # ── 状态变量 ──
    cash = cfg.initial_capital
    active: List[LivePosition] = []       # 当前持仓
    closed: List[LivePosition] = []       # 已平仓
    daily_snapshots: List[DailySnapshot] = []
    cum_realized = 0.0

    # 跳过 / 入场统计
    stats = {
        "total_predictions": len(preds),
        "skipped_score_filter": 0,
        "skipped_neutral": 0,
        "skipped_cash_limit": 0,
        "skipped_max_positions": 0,
        "skipped_duplicate_ticker": 0,
        "skipped_agent_disagreement": 0,   # 升级1
        "skipped_macro_gate": 0,           # 升级4
        "entered": 0,
    }

    # ── 按日循环 ──
    for date in all_dates:
        day_preds = by_date[date]
        trades_opened = 0
        trades_closed = 0
        day_realized = 0.0

        # Step 1: 检查现有仓位是否到期（按 exit_date）
        still_active = []
        for pos in active:
            # 用 holding_days 推算到期日
            entry_dt = datetime.strptime(pos.entry_date, "%Y-%m-%d")
            est_exit = entry_dt + timedelta(days=max(pos.holding_days, 1) + 2)  # +2 buffer 周末
            # 更精确：用 exit_date 字段（Sprint 1 已填充）
            if pos.exit_date and pos.exit_date <= date:
                # 平仓
                pnl = pos.size_usd * (pos.net_return_pct / 100.0)
                cash += pos.size_usd + pnl   # 归还本金 + 损益
                cum_realized += pnl
                day_realized += pnl
                trades_closed += 1
                closed.append(pos)
            else:
                still_active.append(pos)
        active = still_active

        # Step 2: 按 score 排序，高优先吃资金
        day_preds.sort(key=lambda p: abs(float(p.get("final_score") or 5) - 5), reverse=True)

        for p in day_preds:
            direction = (p.get("direction") or "").strip().lower()
            score = float(p.get("final_score") or 5.0)
            net_ret = p.get("net_return_t7")
            if net_ret is None:
                continue

            # ── 升级 1: Agent 共识门控 ──
            if not cfg.take_all and cfg.max_agent_std > 0:
                dim_std = _calc_dim_std(p)
                if dim_std is not None and dim_std > cfg.max_agent_std:
                    stats["skipped_agent_disagreement"] += 1
                    continue

            # ── 入场筛选（score + direction）──
            if not cfg.take_all:
                if "bull" in direction:
                    if score < cfg.min_score_bull:
                        stats["skipped_score_filter"] += 1
                        continue
                elif "bear" in direction:
                    if score > cfg.min_score_bear:
                        stats["skipped_score_filter"] += 1
                        continue
                elif "neutral" in direction:
                    if not cfg.accept_neutral:
                        stats["skipped_neutral"] += 1
                        continue
                else:
                    stats["skipped_neutral"] += 1
                    continue

            # ── 升级 4: 宏观政体门控（take_all 模式下跳过）──
            if not cfg.take_all and cfg.macro_gate and "bull" in direction:
                if _is_risk_off(spy_prices, date, cfg.spy_ma_days, cfg.spy_below_ma_pct):
                    stats["skipped_macro_gate"] += 1
                    continue

            # 不重复同一 ticker
            active_tickers = {pos.ticker for pos in active}
            if p["ticker"] in active_tickers:
                stats["skipped_duplicate_ticker"] += 1
                continue

            # 仓位上限
            if len(active) >= cfg.max_concurrent:
                stats["skipped_max_positions"] += 1
                continue

            # ── 升级 3: 方向不对称仓位 ──
            # 修复 Bug #15：NAV 用已实现 PnL 更新的真实值，而非"建仓成本 + 现金"
            # 旧实现复利下仓位占比漂移：赚了 20% 后新仓还按初始 NAV 的 8% 开
            nav_est = cfg.initial_capital + cum_realized  # 已实现口径 MTM
            if "bear" in direction:
                size_usd = nav_est * cfg.bear_size_pct
            elif "bull" in direction:
                size_usd = nav_est * cfg.bull_size_pct
            else:
                size_usd = nav_est * cfg.position_size_pct
            # 总敞口保护：防止 bear 12% × max_concurrent 10 = 120% 杠杆
            gross_exposure = sum(pos.size_usd for pos in active) + size_usd
            if gross_exposure > nav_est * 1.0:  # 不允许净敞口 > NAV
                stats["skipped_cash_limit"] += 1
                continue
            if size_usd > cash:
                stats["skipped_cash_limit"] += 1
                continue

            # ── 建仓 ──
            holding = int(p.get("holding_days") or 7)
            exit_date_str = p.get("exit_date") or ""
            if not exit_date_str:
                # fallback：推算
                entry_dt = datetime.strptime(date, "%Y-%m-%d")
                exit_dt = entry_dt + timedelta(days=holding)
                exit_date_str = exit_dt.strftime("%Y-%m-%d")

            # gross return：return_t7 是原始价格变动（bearish 场景下未取反）
            gross_ret = float(p.get("return_t7") or 0)
            # 需要方向调整：bearish 的 return_t7 存的是"原始价格变动"（正=标的涨），
            # 但实际空头收益 = -return_t7
            if "bear" in direction:
                dir_adj_gross = -gross_ret
            else:
                dir_adj_gross = gross_ret

            spy_ret = float(p.get("spy_return_t7") or 0)

            pos = LivePosition(
                pred_id=p["id"],
                ticker=p["ticker"],
                direction=direction,
                entry_date=date,
                exit_date=exit_date_str,
                holding_days=holding,
                size_usd=round(size_usd, 2),
                gross_return_pct=round(dir_adj_gross, 4),
                net_return_pct=float(net_ret),
                exit_reason=p.get("exit_reason") or "T7_CLOSE",
                score=score,
                spy_return_pct=spy_ret,
            )
            active.append(pos)
            cash -= size_usd
            trades_opened += 1
            stats["entered"] += 1

        # Step 3: 日终快照
        invested = sum(pos.size_usd for pos in active)
        nav = cash + invested  # 简化 mark-to-market = cost basis（真实 MTM 需日内价格）
        spy_px = spy_prices.get(date, 0)

        daily_snapshots.append(DailySnapshot(
            date=date,
            cash=round(cash, 2),
            invested=round(invested, 2),
            nav=round(nav, 2),
            active_positions=len(active),
            trades_opened=trades_opened,
            trades_closed=trades_closed,
            realized_pnl=round(day_realized, 2),
            cum_realized_pnl=round(cum_realized, 2),
            spy_price=spy_px,
        ))

    # ── 收尾：强平剩余仓位 ── 修复 Bug #16
    # 旧实现：用预计算 net_return_pct 结算（这是完整 T+7 到期收益，属于未来信息）
    # 新实现：若仓位 exit_date > last_date，按回测窗口最后一天的已实现口径处理：
    #   (a) 回测窗口内 pos 已持有 N 天但未到 exit_date → 按比例线性估算（最保守：0% PnL）
    #   (b) 完整到期的仓位（exit_date <= last_date）应该已在 Step 1 平掉；这里兜底
    last_date = all_dates[-1] if all_dates else None
    for pos in active:
        if pos.exit_date and last_date and pos.exit_date <= last_date:
            # 正常到期但被遗漏（兜底）
            pnl = pos.size_usd * (pos.net_return_pct / 100.0)
        else:
            # 未到期仓位：按"回测结束日为 cutoff"处理，PnL = 0（保守不计未来收益）
            # 严格符合"no look-ahead"，而非虚增 final_nav
            pnl = 0.0
            pos.exit_reason = "WINDOW_CUTOFF"
            pos.net_return_pct = 0.0
            pos.exit_date = last_date or pos.entry_date
        cash += pos.size_usd + pnl
        cum_realized += pnl
        closed.append(pos)
    active = []

    # ══════════════════════════════════════════════════════════════════════════
    # 统计
    # ══════════════════════════════════════════════════════════════════════════

    final_nav = cash
    total_return_pct = (final_nav - cfg.initial_capital) / cfg.initial_capital * 100

    # SPY 买入持有
    spy_start = spy_prices.get(first_date, 0)
    spy_end = spy_prices.get(last_date, spy_start)
    spy_bh_pct = ((spy_end - spy_start) / spy_start * 100) if spy_start > 0 else 0
    spy_bh_end_nav = cfg.initial_capital * (1 + spy_bh_pct / 100)

    # 胜率
    winners = [t for t in closed if t.net_return_pct > 0]
    losers = [t for t in closed if t.net_return_pct <= 0]
    win_rate = len(winners) / len(closed) * 100 if closed else 0

    # 平均收益
    avg_win = sum(t.net_return_pct for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t.net_return_pct for t in losers) / len(losers) if losers else 0

    # Profit Factor
    total_wins = sum(t.size_usd * t.net_return_pct / 100 for t in winners)
    total_losses = abs(sum(t.size_usd * t.net_return_pct / 100 for t in losers))
    profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

    # Sharpe — 修复 Bug #8：T+7 采样频率是"7 交易日"，不是"7 自然日"。
    # 一年 252 交易日 / 7 = 36 次采样（而非 52 周）。旧值 52 让 √n 放大系数错位，
    # 系统性高估 Sharpe ~20% (√52/√36 = 1.20)
    from trading_costs import sharpe_ratio
    net_rets = [t.net_return_pct for t in closed]
    T7_PERIODS_PER_YEAR = 36  # 252 交易日 / 7 交易日 per T+7 采样
    sharpe = sharpe_ratio(net_rets, periods_per_year=T7_PERIODS_PER_YEAR) if net_rets else None

    # Max Drawdown（基于 daily NAV）
    peak = cfg.initial_capital
    max_dd_pct = 0.0
    for snap in daily_snapshots:
        if snap.nav > peak:
            peak = snap.nav
        dd = (peak - snap.nav) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd

    # 按标的统计
    ticker_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "net_rets": []})
    for t in closed:
        ts = ticker_stats[t.ticker]
        ts["trades"] += 1
        if t.net_return_pct > 0:
            ts["wins"] += 1
        ts["pnl"] += t.size_usd * t.net_return_pct / 100
        ts["net_rets"].append(t.net_return_pct)

    # 按方向统计
    dir_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in closed:
        ds = dir_stats[t.direction]
        ds["trades"] += 1
        if t.net_return_pct > 0:
            ds["wins"] += 1
        ds["pnl"] += t.size_usd * t.net_return_pct / 100

    # 按退出类型统计
    exit_stats = defaultdict(lambda: {"count": 0, "avg_net": 0.0, "total_pnl": 0.0})
    for t in closed:
        es = exit_stats[t.exit_reason]
        es["count"] += 1
        es["total_pnl"] += t.size_usd * t.net_return_pct / 100
    for reason, es in exit_stats.items():
        trades_of_type = [t for t in closed if t.exit_reason == reason]
        es["avg_net"] = sum(t.net_return_pct for t in trades_of_type) / len(trades_of_type)

    # 持仓天数分布
    avg_holding = sum(t.holding_days for t in closed) / len(closed) if closed else 0

    # 月度收益
    monthly = defaultdict(float)
    for t in closed:
        month = t.exit_date[:7] if t.exit_date else t.entry_date[:7]
        monthly[month] += t.size_usd * t.net_return_pct / 100

    # ══════════════════════════════════════════════════════════════════════════
    # Equity Curve（按交易结算日排列）
    # ══════════════════════════════════════════════════════════════════════════

    equity_points = []
    running_nav = cfg.initial_capital
    sorted_closed = sorted(closed, key=lambda t: t.exit_date or t.entry_date)
    for t in sorted_closed:
        pnl = t.size_usd * t.net_return_pct / 100
        running_nav += pnl
        equity_points.append({
            "date": t.exit_date or t.entry_date,
            "ticker": t.ticker,
            "direction": t.direction,
            "exit_reason": t.exit_reason,
            "net_ret_pct": t.net_return_pct,
            "pnl_usd": round(pnl, 2),
            "nav": round(running_nav, 2),
            "nav_pct": round((running_nav - cfg.initial_capital) / cfg.initial_capital * 100, 2),
        })

    return {
        "config": {
            "initial_capital": cfg.initial_capital,
            "position_size_pct": cfg.position_size_pct,
            "bull_size_pct": cfg.bull_size_pct,
            "bear_size_pct": cfg.bear_size_pct,
            "max_concurrent": cfg.max_concurrent,
            "min_score_bull": cfg.min_score_bull,
            "min_score_bear": cfg.min_score_bear,
            "accept_neutral": cfg.accept_neutral,
            "take_all": cfg.take_all,
            "max_agent_std": cfg.max_agent_std,
            "macro_gate": cfg.macro_gate,
        },
        "period": {"start": first_date, "end": last_date, "trading_days": len(all_dates)},
        "portfolio": {
            "initial_nav": cfg.initial_capital,
            "final_nav": round(final_nav, 2),
            "total_return_pct": round(total_return_pct, 2),
            "total_pnl_usd": round(final_nav - cfg.initial_capital, 2),
        },
        "benchmark": {
            "spy_start_price": round(spy_start, 2),
            "spy_end_price": round(spy_end, 2),
            "spy_return_pct": round(spy_bh_pct, 2),
            "spy_end_nav": round(spy_bh_end_nav, 2),
        },
        "alpha": round(total_return_pct - spy_bh_pct, 2),
        "risk_metrics": {
            "sharpe_ratio": sharpe,
            "profit_factor": round(profit_factor, 3),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "win_rate_pct": round(win_rate, 1),
            "avg_win_pct": round(avg_win, 3),
            "avg_loss_pct": round(avg_loss, 3),
            "win_loss_ratio": round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None,
            "avg_holding_days": round(avg_holding, 1),
        },
        "trade_stats": {
            "total_trades": len(closed),
            "winners": len(winners),
            "losers": len(losers),
        },
        "filter_stats": stats,
        "by_direction": {
            d: {"trades": ds["trades"],
                "win_rate": round(ds["wins"]/ds["trades"]*100, 1) if ds["trades"] else 0,
                "pnl_usd": round(ds["pnl"], 2)}
            for d, ds in dir_stats.items()
        },
        "by_exit_reason": {
            r: {"count": es["count"],
                "avg_net_pct": round(es["avg_net"], 3),
                "total_pnl_usd": round(es["total_pnl"], 2)}
            for r, es in exit_stats.items()
        },
        "by_ticker": {
            t: {"trades": ts["trades"],
                "win_rate": round(ts["wins"]/ts["trades"]*100, 1) if ts["trades"] else 0,
                "pnl_usd": round(ts["pnl"], 2),
                "avg_net_pct": round(sum(ts["net_rets"])/len(ts["net_rets"]), 3) if ts["net_rets"] else 0}
            for t, ts in sorted(ticker_stats.items(), key=lambda x: -x[1]["pnl"])
        },
        "monthly_pnl": {m: round(v, 2) for m, v in sorted(monthly.items())},
        "equity_curve": equity_points,
        "all_trades": [
            {
                "id": t.pred_id, "ticker": t.ticker, "dir": t.direction,
                "entry": t.entry_date, "exit": t.exit_date,
                "hold": t.holding_days, "score": t.score,
                "gross_pct": t.gross_return_pct, "net_pct": t.net_return_pct,
                "exit_reason": t.exit_reason,
                "pnl_usd": round(t.size_usd * t.net_return_pct / 100, 2),
                "size_usd": t.size_usd,
            }
            for t in sorted_closed
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 控制台输出
# ══════════════════════════════════════════════════════════════════════════════

def print_report(result: Dict):
    """在终端漂亮输出回测结果"""
    if "error" in result:
        print(f"❌ {result['error']}")
        return

    c = result["config"]
    p = result["portfolio"]
    b = result["benchmark"]
    r = result["risk_metrics"]
    t = result["trade_stats"]
    period = result["period"]

    print()
    print("=" * 70)
    print("  💰 Alpha Hive — $50K 股票现货策略回测")
    print("=" * 70)
    print()
    print(f"  📅 回测区间：{period['start']} → {period['end']}（{period['trading_days']} 个交易日）")
    print(f"  ⚙️  配置：${c['initial_capital']:,.0f} | Bull {c.get('bull_size_pct',0.1)*100:.0f}%仓 "
          f"Bear {c.get('bear_size_pct',0.1)*100:.0f}%仓 | 最多 {c['max_concurrent']} 仓")
    print(f"  🎯 门槛：Bull≥{c['min_score_bull']} Bear≤{c['min_score_bear']} | "
          f"Agent std≤{c.get('max_agent_std', 'off')} | "
          f"宏观门控={'开' if c.get('macro_gate') else '关'}")
    if c["take_all"]:
        print(f"  ⚠️  模式：全入场（无筛选）")
    print()

    # ── 总览 ──
    print("┌─────────────────────── 总 览 ───────────────────────┐")
    pnl_sign = "+" if p["total_pnl_usd"] >= 0 else ""
    nav_emoji = "📈" if p["total_return_pct"] >= 0 else "📉"
    print(f"  {nav_emoji} 组合终值：${p['final_nav']:>10,.2f}（{pnl_sign}{p['total_return_pct']:.2f}%）")
    print(f"     PnL：{pnl_sign}${p['total_pnl_usd']:,.2f}")
    spy_sign = "+" if b["spy_return_pct"] >= 0 else ""
    print(f"  📊 SPY 基准：${b['spy_end_nav']:>10,.2f}（{spy_sign}{b['spy_return_pct']:.2f}%）")
    alpha = result["alpha"]
    alpha_emoji = "🏆" if alpha > 0 else "❌"
    alpha_sign = "+" if alpha >= 0 else ""
    print(f"  {alpha_emoji} Alpha vs SPY：{alpha_sign}{alpha:.2f}%")
    print("└─────────────────────────────────────────────────────┘")
    print()

    # ── 风控指标 ──
    print("┌─────────────────── 风控指标 ──────────────────────┐")
    sharpe_str = f"{r['sharpe_ratio']:.3f}" if r['sharpe_ratio'] is not None else "N/A"
    print(f"  Sharpe Ratio：{sharpe_str}    Profit Factor：{r['profit_factor']:.3f}")
    print(f"  Max Drawdown：-{r['max_drawdown_pct']:.2f}%    Win Rate：{r['win_rate_pct']:.1f}%")
    print(f"  Avg Win：+{r['avg_win_pct']:.3f}%    Avg Loss：{r['avg_loss_pct']:.3f}%")
    wl = r.get("win_loss_ratio")
    wl_str = f"{wl:.2f}" if wl else "N/A"
    print(f"  Win/Loss Ratio：{wl_str}    Avg Holding：{r['avg_holding_days']:.1f}d")
    print("└─────────────────────────────────────────────────────┘")
    print()

    # ── 筛选统计 ──
    fs = result["filter_stats"]
    print(f"  📋 预测总数：{fs['total_predictions']}  →  入场：{fs['entered']}")
    print(f"     跳过：Agent分歧={fs.get('skipped_agent_disagreement',0)}  "
          f"宏观门控={fs.get('skipped_macro_gate',0)}  "
          f"score={fs['skipped_score_filter']}  中性={fs['skipped_neutral']}")
    print(f"     　　  满仓={fs['skipped_max_positions']}  重复={fs['skipped_duplicate_ticker']}  "
          f"现金不足={fs['skipped_cash_limit']}")
    print()

    # ── 按方向 ──
    print("┌──────────────── 按方向 ────────────────────────────┐")
    for d, ds in result["by_direction"].items():
        pnl_s = f"+${ds['pnl_usd']:,.2f}" if ds["pnl_usd"] >= 0 else f"-${abs(ds['pnl_usd']):,.2f}"
        print(f"  {d:>10s}：{ds['trades']}笔  胜率 {ds['win_rate']:.1f}%  PnL {pnl_s}")
    print("└─────────────────────────────────────────────────────┘")
    print()

    # ── 按退出类型 ──
    print("┌──────────────── 按退出类型 ─────────────────────────┐")
    for reason, es in result["by_exit_reason"].items():
        pnl_s = f"+${es['total_pnl_usd']:,.2f}" if es["total_pnl_usd"] >= 0 else f"-${abs(es['total_pnl_usd']):,.2f}"
        print(f"  {reason:>10s}：{es['count']}笔  平均净收益 {es['avg_net_pct']:+.3f}%  总PnL {pnl_s}")
    print("└─────────────────────────────────────────────────────┘")
    print()

    # ── 按标的（Top 10）──
    print("┌──────────────── 按标的 Top10 ──────────────────────┐")
    print(f"  {'Ticker':>8s}  {'Trades':>6s}  {'WinRate':>7s}  {'AvgNet':>8s}  {'PnL':>12s}")
    print(f"  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*8}  {'─'*12}")
    for i, (ticker, ts) in enumerate(result["by_ticker"].items()):
        if i >= 10:
            break
        pnl_s = f"+${ts['pnl_usd']:,.2f}" if ts["pnl_usd"] >= 0 else f"-${abs(ts['pnl_usd']):,.2f}"
        print(f"  {ticker:>8s}  {ts['trades']:>6d}  {ts['win_rate']:>6.1f}%  {ts['avg_net_pct']:>+7.3f}%  {pnl_s:>12s}")
    print("└─────────────────────────────────────────────────────┘")
    print()

    # ── 月度 PnL ──
    if result["monthly_pnl"]:
        print("┌──────────────── 月度 PnL ──────────────────────────┐")
        for month, pnl in result["monthly_pnl"].items():
            bar_len = int(abs(pnl) / 50)
            bar = "█" * min(bar_len, 30)
            sign = "+" if pnl >= 0 else ""
            color_bar = f"{'🟢' if pnl >= 0 else '🔴'} {bar}"
            print(f"  {month}  {sign}${pnl:>8,.2f}  {color_bar}")
        print("└─────────────────────────────────────────────────────┘")
    print()

    # ── 交易明细（最后 10 笔）──
    trades = result["all_trades"]
    if trades:
        print(f"┌──────────── 最近 10 笔交易 ─────────────────────────┐")
        print(f"  {'Ticker':>6s} {'Dir':>7s} {'Entry':>10s} {'Exit':>10s} {'Hold':>4s} {'Score':>5s} "
              f"{'Net%':>7s} {'PnL$':>8s} {'Reason':>6s}")
        for t in trades[-10:]:
            pnl_s = f"${t['pnl_usd']:+,.0f}"
            print(f"  {t['ticker']:>6s} {t['dir']:>7s} {t['entry']:>10s} {t['exit'] or 'N/A':>10s} "
                  f"{t['hold']:>4d}d {t['score']:>5.1f} {t['net_pct']:>+6.3f}% {pnl_s:>8s} {t['exit_reason']:>6s}")
        print(f"└─────────────────────────────────────────────────────┘")

    print()
    print("⚠️  口径说明：股票现货策略（bull→买入/bear→融券卖空），非期权策略。")
    print("    成本含：双边滑点 + 佣金 + 借券费（空头）。SL -5% / TP +10% / T+7 平仓。")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Alpha Hive $50K Portfolio Backtest")
    # 所有 default 值同步到 BacktestConfig.__init__（v0.22.1 方案 A 放宽筛选后的新基线）
    _d = BacktestConfig()
    parser.add_argument("--capital", type=float, default=_d.initial_capital)
    parser.add_argument("--size-pct", type=float, default=_d.position_size_pct)
    parser.add_argument("--max-pos", type=int, default=_d.max_concurrent, help=f"最多同时持仓（默认 {_d.max_concurrent}）")
    parser.add_argument("--min-score-bull", type=float, default=_d.min_score_bull, help=f"看多最低分（默认 {_d.min_score_bull}）")
    parser.add_argument("--min-score-bear", type=float, default=_d.min_score_bear, help=f"看空最高分（默认 {_d.min_score_bear}）")
    parser.add_argument("--accept-neutral", action="store_true", default=_d.accept_neutral, help=f"允许中性方向入场（默认 {_d.accept_neutral}）")
    parser.add_argument("--reject-neutral", dest="accept_neutral", action="store_false", help="显式拒绝中性")
    parser.add_argument("--all", action="store_true", help="全入场，不筛选 score/direction")
    parser.add_argument("--max-std", type=float, default=_d.max_agent_std, help=f"Agent 共识门控阈值（默认 {_d.max_agent_std}）")
    parser.add_argument("--no-macro-gate", action="store_true", help="禁用宏观政体门控")
    parser.add_argument("--bull-size", type=float, default=_d.bull_size_pct)
    parser.add_argument("--bear-size", type=float, default=_d.bear_size_pct)
    parser.add_argument("--horizon", type=int, choices=[1, 7, 30], default=_d.horizon,
                        help="持仓期天数：1/7/30（默认 7）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（供其他脚本消费）")
    parser.add_argument("--save", type=str, default=None, help="保存完整结果到 JSON 文件")
    args = parser.parse_args()

    cfg = BacktestConfig(
        initial_capital=args.capital,
        position_size_pct=args.size_pct,
        max_concurrent=args.max_pos,
        min_score_bull=args.min_score_bull,
        min_score_bear=args.min_score_bear,
        accept_neutral=args.accept_neutral,
        take_all=args.all,
        max_agent_std=args.max_std,
        macro_gate=not args.no_macro_gate,
        bull_size_pct=args.bull_size,
        bear_size_pct=args.bear_size,
        horizon=args.horizon,
    )

    result = run_backtest(cfg)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_report(result)

    if args.save:
        save_path = Path(args.save)
        save_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 完整结果已保存到 {save_path}")


if __name__ == "__main__":
    main()
