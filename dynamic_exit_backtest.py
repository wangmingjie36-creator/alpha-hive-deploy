#!/usr/bin/env python3
"""
🐝 Alpha Hive — 动态 Exit 历史回测 (v0.23.0)
===============================================
验证"催化剂驱动 exit"假设：是否能把 T+7 的 α -25% 升到 T+30 的 α +49% 之间合理水位。

核心逻辑
--------
对 pheromone.db 每笔 checked_t7=1 的预测：
  1. 从 catalysts.json 加载该 ticker 的催化剂列表
  2. catalyst_exit_planner.plan_exit(ticker, entry_date, catalysts) → hold_days
  3. yfinance 拉 entry_date ~ entry_date + hold_days 的价格
  4. 按实际收盘价计算 gross return，应用 trading_costs 得 net return

对比三组：
  (a) 固定 T+7   — 基线
  (b) 固定 T+21  — 无催化剂也用的默认 hold
  (c) 动态 exit  — 催化剂驱动（本模块核心）

输出：样本数 / Avg Net / WR / Sharpe / Profit Factor / FF Jensen α

用法
----
    python3 dynamic_exit_backtest.py
    python3 dynamic_exit_backtest.py --json
    python3 dynamic_exit_backtest.py --limit 50  # 快速测试
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger("alpha_hive.dynamic_exit")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

T7_PERIODS_PER_YEAR = 36

# ══════════════════════════════════════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════════════════════════════════════

def _load_predictions(limit: Optional[int] = None) -> List[Dict]:
    db = Path(__file__).parent / "pheromone.db"
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        q = """SELECT id, date, ticker, direction, final_score, price_at_predict,
                      return_t7, net_return_t7, exit_reason, holding_days,
                      dimension_scores
               FROM predictions
               WHERE checked_t7 = 1 AND net_return_t7 IS NOT NULL
               ORDER BY date ASC"""
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = conn.execute(q).fetchall()
    return [dict(r) for r in rows]


def _fetch_price_series(ticker: str, start: str, end: str) -> Dict[str, float]:
    """yfinance 拉 [start, end] 区间的收盘价"""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=2)
        hist = yf.Ticker(ticker).history(
            start=start_dt.date(), end=end_dt.date(), auto_adjust=True
        )
        if hist.empty:
            return {}
        return {
            idx.strftime("%Y-%m-%d"): float(row["Close"])
            for idx, row in hist.iterrows()
            if hasattr(idx, "strftime")
        }
    except Exception as e:
        _log.debug(f"{ticker} price fetch failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# 2. 单笔动态 exit 模拟
# ══════════════════════════════════════════════════════════════════════════════

def _simulate_dynamic_trade(
    p: Dict,
    catalysts: List[Dict],
    price_cache: Dict[str, Dict[str, float]],
) -> Optional[Dict]:
    """
    对单笔预测模拟"催化剂驱动 exit"：
      1. plan_exit → hold_days
      2. 取 entry_date + hold_days 交易日后的 close 价
      3. apply_costs 得 net return
    """
    from catalyst_exit_planner import plan_exit
    from trading_costs import apply_costs

    ticker = p["ticker"]
    entry_date = p["date"]
    direction = (p.get("direction") or "").strip().lower()
    entry_price = float(p.get("price_at_predict") or 0)

    if entry_price <= 0 or not entry_date:
        return None

    # 动态规划 hold_days
    hold_days, rationale = plan_exit(ticker, entry_date, catalysts)

    # 从缓存或 yfinance 拉价格
    if ticker not in price_cache:
        start = entry_date
        end_dt = datetime.strptime(entry_date, "%Y-%m-%d") + timedelta(
            days=int(hold_days * 1.6) + 5
        )
        price_cache[ticker] = _fetch_price_series(
            ticker, start, end_dt.strftime("%Y-%m-%d")
        )

    prices = price_cache[ticker]
    if not prices:
        return None

    # 找 entry_date 后的 hold_days 个交易日
    entry_dt = datetime.strptime(entry_date, "%Y-%m-%d")
    # 按交易日顺序取第 hold_days 个
    sorted_dates = sorted(d for d in prices if d > entry_date)
    if len(sorted_dates) < 1:
        return None
    target_idx = min(hold_days - 1, len(sorted_dates) - 1)  # 0-indexed
    exit_date = sorted_dates[target_idx]
    exit_price = prices[exit_date]

    if exit_price <= 0:
        return None

    # 计算毛收益
    raw_ret = (exit_price - entry_price) / entry_price * 100.0
    dir_adj = -raw_ret if "bear" in direction else raw_ret
    dir_for_cost = direction if direction in ("bullish", "bearish") else "neutral"

    cost_result = apply_costs(dir_adj, dir_for_cost, ticker, hold_days)
    net_return = cost_result["net_return_pct"]

    return {
        "id": p["id"],
        "ticker": ticker,
        "direction": direction,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "hold_days": hold_days,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_return_pct": round(dir_adj, 4),
        "net_return_pct": round(net_return, 4),
        "final_score": p.get("final_score"),
        "exit_rationale": rationale,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. 批量回测
# ══════════════════════════════════════════════════════════════════════════════

def run_dynamic_exit_backtest(limit: Optional[int] = None) -> Dict:
    from catalyst_exit_planner import load_catalysts_for_ticker

    preds = _load_predictions(limit=limit)
    _log.info(f"开始回测 {len(preds)} 笔预测（动态 exit）")

    # 每个 ticker 的催化剂列表缓存
    cat_cache: Dict[str, List[Dict]] = {}
    # 每个 ticker 的价格缓存（避免重复拉 yfinance）
    price_cache: Dict[str, Dict[str, float]] = {}

    simulated: List[Dict] = []
    skipped = 0
    for p in preds:
        ticker = p["ticker"]
        if ticker not in cat_cache:
            cat_cache[ticker] = load_catalysts_for_ticker(ticker)
        trade = _simulate_dynamic_trade(p, cat_cache[ticker], price_cache)
        if trade is None:
            skipped += 1
            continue
        simulated.append(trade)

    _log.info(f"完成：模拟 {len(simulated)} 笔，跳过 {skipped} 笔")
    return {
        "n_trades": len(simulated),
        "n_skipped": skipped,
        "trades": simulated,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4. 对比统计
# ══════════════════════════════════════════════════════════════════════════════

def _compute_stats(nets: List[float], periods_per_year: int = 36) -> Dict:
    if not nets:
        return {"n": 0}
    n = len(nets)
    wins = [r for r in nets if r > 0]
    losses = [r for r in nets if r <= 0]
    wr = len(wins) / n * 100.0
    avg = sum(nets) / n
    std = statistics.pstdev(nets)
    sharpe = (avg / std) * math.sqrt(periods_per_year) if std > 1e-9 else 0
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) < 0 else float("inf")
    return {
        "n": n,
        "avg_net": round(avg, 3),
        "wr": round(wr, 2),
        "sharpe": round(sharpe, 3),
        "pf": round(pf, 3) if pf != float("inf") else None,
        "total_pnl_50k_10pct": round(sum(50_000 * 0.10 * (r / 100) for r in nets), 2),
    }


def compare_horizons() -> Dict:
    """跑三组对比：固定 T+7（DB 数据）vs 固定 T+21（YF 重算）vs 动态 exit"""
    # (a) 固定 T+7：直接用 DB 的 net_return_t7（已有）
    preds_t7 = _load_predictions()
    nets_t7 = [float(p["net_return_t7"]) for p in preds_t7]

    # (b) 固定 T+21：用 portfolio_backtest.load_verified_predictions(horizon=...) 风格
    #     但 horizon=21 不在数据库列里，改用 dynamic 模式但强制 default_hold=21，无催化剂用
    from catalyst_exit_planner import plan_exit
    price_cache: Dict[str, Dict[str, float]] = {}
    nets_t21: List[float] = []
    for p in preds_t7:
        # 强制 "无催化剂" → 默认 T+21
        trade = _simulate_dynamic_trade(p, [], price_cache)
        if trade:
            nets_t21.append(trade["net_return_pct"])

    # (c) 动态 exit
    dynamic = run_dynamic_exit_backtest()
    nets_dyn = [t["net_return_pct"] for t in dynamic["trades"]]

    return {
        "fixed_t7": {
            "stats": _compute_stats(nets_t7),
            "nets": nets_t7,
        },
        "fixed_t21": {
            "stats": _compute_stats(nets_t21),
            "nets": nets_t21,
        },
        "dynamic_exit": {
            "stats": _compute_stats(nets_dyn),
            "nets": nets_dyn,
            "trades_sample": dynamic["trades"][:5],
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _print_report(cmp: Dict) -> None:
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  Alpha Hive — 动态 Exit 回测对比 (v0.23.0)          ║")
    print("╚══════════════════════════════════════════════════════╝\n")
    print("  对比三种 exit 策略（同一批历史预测）：\n")
    for label, key in [
        ("固定 T+7   (DB 路径依赖 SL/TP)", "fixed_t7"),
        ("固定 T+21  (无路径依赖，裸持)", "fixed_t21"),
        ("动态 Exit  (催化剂驱动)", "dynamic_exit"),
    ]:
        s = cmp[key]["stats"]
        if s["n"] == 0:
            print(f"  {label:36s}   无数据"); continue
        print(f"  {label:36s}")
        print(f"    n={s['n']:<4}  Avg Net {s['avg_net']:+6.3f}%  "
              f"WR {s['wr']:>5.1f}%  Sharpe {s['sharpe']:+6.3f}  "
              f"PF {s['pf'] if s['pf'] else '∞':>5}  "
              f"$50K 10% → ${s['total_pnl_50k_10pct']:+,.0f}")
        print()

    # 动态 exit 样例
    sample = cmp["dynamic_exit"].get("trades_sample") or []
    if sample:
        print("  ┌─── 动态 Exit 样例（前 5 笔）────────────────────────┐")
        for t in sample:
            print(f"    {t['ticker']:5s} {t['direction']:7s} {t['entry_date']} → {t['exit_date']}  "
                  f"hold={t['hold_days']:>2d}d  net {t['net_return_pct']:+6.3f}%  "
                  f"【{t['exit_rationale']}】")
        print("  └─────────────────────────────────────────────────────┘\n")


def main():
    parser = argparse.ArgumentParser(description="Alpha Hive 动态 Exit 回测")
    parser.add_argument("--limit", type=int, default=None, help="限制预测数（测试用）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cmp = compare_horizons()
    if args.json:
        # 去掉 nets 列表（太长）
        summary = {k: {kk: vv for kk, vv in v.items() if kk != "nets"} for k, v in cmp.items()}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_report(cmp)


if __name__ == "__main__":
    main()
