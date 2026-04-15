#!/usr/bin/env python3
"""
🔄 Sprint 1 / P0-2 回填脚本

对 predictions 表中所有 checked_t7=1 且 net_return_t7 IS NULL 的记录：
1. 重新用 _simulate_trade_path 模拟路径（SL/TP 可能改变实际退出价）
2. 应用 trading_costs.apply_costs 计算净收益
3. 拉 SPY 同期基准
4. 更新 return_t7（raw 形式）、net_return_t7、exit_*、cost_breakdown、spy_return_t7

用法:
    python3 backfill_trading_costs.py            # dry-run
    python3 backfill_trading_costs.py --apply    # 实际写入
"""

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("backfill")


def run(apply_changes: bool = False, limit: int = None):
    from backtester import Backtester, PredictionStore
    from trading_costs import apply_costs
    from hive_logger import SafeJSONEncoder

    store = PredictionStore()
    bt = Backtester(store.db_path)

    # 取所有已验证但未扣成本的 T+7 记录
    query = """
        SELECT id, date, ticker, direction, price_at_predict, return_t7, correct_t7
        FROM predictions
        WHERE checked_t7 = 1 AND net_return_t7 IS NULL
        ORDER BY date ASC, id ASC
    """
    if limit:
        query += f" LIMIT {limit}"

    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query).fetchall()

    _log.info("待回填记录: %d", len(rows))
    if not rows:
        return

    stats = {
        "total": len(rows),
        "path_sim_ok": 0,
        "path_sim_fail": 0,
        "exit_tp": 0,
        "exit_sl": 0,
        "exit_close": 0,
        "net_negative_after_costs": 0,
        "direction_flipped_by_sl": 0,
    }
    updates = []

    for i, r in enumerate(rows, 1):
        pid, date, ticker, direction, entry, old_ret, old_correct = (
            r["id"], r["date"], r["ticker"], r["direction"],
            r["price_at_predict"], r["return_t7"], r["correct_t7"],
        )

        if not entry or entry <= 0:
            stats["path_sim_fail"] += 1
            continue

        # 路径模拟
        path = bt._simulate_trade_path(ticker, date, 7, entry, direction)
        if not path:
            # yfinance 拉不到，降级：用 old return_t7 做 fallback（仅扣成本）
            _dir_lc = (direction or "").lower()
            dir_adj_old = -old_ret if _dir_lc == "bearish" else old_ret
            cost_res = apply_costs(dir_adj_old, direction, ticker, 7)
            updates.append({
                "id": pid, "raw_ret": old_ret, "is_correct": bool(old_correct),
                "net_ret": cost_res["net_return_pct"],
                "exit_reason": "T7_CLOSE_FALLBACK",
                "exit_date": None, "exit_price": None, "holding_days": 7,
                "cost": cost_res["breakdown"], "spy": None,
            })
            stats["path_sim_fail"] += 1
            continue

        stats["path_sim_ok"] += 1
        gross = path["gross_return_pct"]  # 方向调整后
        exit_reason = path["exit_reason"]
        holding = path.get("holding_days", 7)

        if exit_reason == "TP":
            stats["exit_tp"] += 1
        elif exit_reason == "SL":
            stats["exit_sl"] += 1
            # 如果老的 T+7 显示正确但 SL 触发，说明中途被打出
            if old_correct and gross < -1.0:
                stats["direction_flipped_by_sl"] += 1
        else:
            stats["exit_close"] += 1

        # 扣成本
        cost_res = apply_costs(gross, direction, ticker, holding)
        net_ret = cost_res["net_return_pct"]

        if net_ret < 0:
            stats["net_negative_after_costs"] += 1

        # SPY 基准
        spy_entry = bt._get_spy_entry_price(date)
        spy_exit = bt._get_price_at_date("SPY", date, 7)
        spy_ret = None
        if spy_entry and spy_exit and spy_entry > 0:
            spy_ret = round((spy_exit - spy_entry) / spy_entry * 100, 4)

        # 方向正确判定（基于新的 path-dependent gross）
        is_correct = gross > -1.0

        # 还原 raw return（用于兼容旧 dashboard 代码）
        _dir_lc = (direction or "").lower()
        raw_ret_new = -gross if _dir_lc == "bearish" else gross

        updates.append({
            "id": pid, "raw_ret": round(raw_ret_new, 3), "is_correct": is_correct,
            "net_ret": net_ret,
            "exit_reason": exit_reason,
            "exit_date": path.get("exit_date"),
            "exit_price": path.get("exit_price"),
            "holding_days": holding,
            "cost": cost_res["breakdown"], "spy": spy_ret,
        })

        if i % 10 == 0:
            _log.info("  进度 %d/%d  TP=%d SL=%d CLOSE=%d",
                      i, len(rows), stats["exit_tp"], stats["exit_sl"], stats["exit_close"])

        time.sleep(0.3)  # rate limit yfinance

    # 统计
    _log.info("=" * 60)
    _log.info("统计: %s", json.dumps(stats, ensure_ascii=False, indent=2))

    if not apply_changes:
        _log.info("DRY RUN — 加 --apply 实际写入")
        _preview_impact(updates)
        return

    # 实际写入
    _log.info("写入 %d 条更新...", len(updates))
    with sqlite3.connect(store.db_path) as conn:
        for u in updates:
            conn.execute("""
                UPDATE predictions
                SET return_t7 = ?, correct_t7 = ?,
                    net_return_t7 = ?, exit_reason = ?, exit_date = ?,
                    exit_price = ?, holding_days = ?, cost_breakdown = ?,
                    spy_return_t7 = ?
                WHERE id = ?
            """, (
                u["raw_ret"], 1 if u["is_correct"] else 0,
                u["net_ret"], u["exit_reason"], u["exit_date"],
                u["exit_price"], u["holding_days"],
                json.dumps(u["cost"] or {}),
                u["spy"],
                u["id"],
            ))
        conn.commit()
    _log.info("✅ 写入完成")
    _preview_impact(updates)


def _preview_impact(updates: list):
    """展示回填前后的 key 指标对比。"""
    import statistics
    if not updates:
        return
    raw_rets = [u["raw_ret"] for u in updates]
    net_rets = [u["net_ret"] for u in updates]
    sl_count = sum(1 for u in updates if u["exit_reason"] == "SL")
    tp_count = sum(1 for u in updates if u["exit_reason"] == "TP")
    correct = sum(1 for u in updates if u["is_correct"])

    _log.info("━━━━━ 影响预览 ━━━━━")
    _log.info("样本数: %d", len(updates))
    _log.info("触发止损 (SL): %d (%.1f%%)", sl_count, 100 * sl_count / len(updates))
    _log.info("触发止盈 (TP): %d (%.1f%%)", tp_count, 100 * tp_count / len(updates))
    _log.info("新准确率: %.1f%%", 100 * correct / len(updates))
    _log.info("毛收益均值 (raw): %.3f%%", statistics.mean(raw_rets) if raw_rets else 0)
    _log.info("净收益均值: %.3f%%", statistics.mean(net_rets) if net_rets else 0)
    _log.info("净亏损笔数: %d", sum(1 for n in net_rets if n < 0))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="实际写入 DB（否则 dry-run）")
    p.add_argument("--limit", type=int, default=None, help="限制处理数量（测试用）")
    args = p.parse_args()
    run(apply_changes=args.apply, limit=args.limit)
