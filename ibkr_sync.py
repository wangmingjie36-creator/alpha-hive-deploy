#!/usr/bin/env python3
"""
🌉 Alpha Hive - IBKR Paper Account Sync Bridge (v0.19.0 · Phase 2)

职责：把本地 PaperPortfolio 与 IBKR Paper Account 桥接起来。

流程：
  1) 每日 export_daily_actions(date)
     → 从本地 positions.jsonl / 新 run 结果导出"今日要执行的订单"
     → 写 paper_actions/actions_YYYY-MM-DD.json（IBKR 友好格式：symbol/side/qty/limit/tif）
     → 用户在 TWS Paper Account 手动下单，或用 ibapi 脚本自动下单

  2) IBKR 成交后导出 trades CSV（TWS → Reports → Trade Confirmation）
     → import_ibkr_statement(csv_path) 解析为 real_fills.jsonl

  3) reconcile()
     → 比较本地 paper_portfolio 模拟价 vs IBKR 真实成交价
     → 写 reconcile_reports/reconcile_YYYY-MM-DD.json 给报告卡片展示 slippage/fill diff

设计原则：
  - 仅 JSON + CSV 做 IO，不连 IBKR API（用户手动/半自动对接）
  - 所有写入均 append-only，失败可重放
  - 不做订单执行，只做"建议单 + 事后对账"
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, date as _date
from pathlib import Path
from typing import Dict, List, Optional

_log = logging.getLogger("alpha_hive.ibkr_sync")

# ══════════════════════════════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════════════════════════════

BASE = Path(__file__).resolve().parent / "paper_account"
ACTIONS_DIR = BASE / "actions"           # 每日待执行订单
FILLS_FILE = BASE / "real_fills.jsonl"   # IBKR 真实成交
RECONCILE_DIR = BASE / "reconcile"       # 对账报告
for _d in (ACTIONS_DIR, RECONCILE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ActionOrder:
    """单笔待执行订单 (IBKR friendly)."""
    action_id: str          # 唯一 ID：TICKER_YYYY-MM-DD_OPEN / _CLOSE
    date: str               # YYYY-MM-DD
    ticker: str
    side: str               # BUY / SELL / SSHORT / BTC (buy-to-cover)
    qty: int                # 股数（正整数）
    order_type: str         # MKT / LMT
    limit_price: Optional[float]
    tif: str                # DAY / GTC
    intent: str             # open_long / open_short / close_long / close_short / time_stop / sl_hit / tp_hit
    meta: Dict              # 来源信息：confidence, score, target_price, stop_loss


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2.1 - Export daily actions
# ══════════════════════════════════════════════════════════════════════════════

def export_daily_actions(
    target_date: str,
    new_opens: List[Dict],
    new_closes: List[Dict],
) -> Path:
    """把 paper_portfolio.run_for_date 的结果翻译成 IBKR 订单 JSON。

    Args:
        target_date: YYYY-MM-DD
        new_opens: [{ticker, direction, size_usd, entry_price, sl, tp, confidence, score}, ...]
        new_closes: [{ticker, direction, qty, exit_price, exit_reason}, ...]

    Returns:
        导出文件路径
    """
    orders: List[ActionOrder] = []

    for op in new_opens:
        ticker = op["ticker"]
        direction = (op.get("direction") or "").lower()
        price = float(op.get("entry_price") or 0.0)
        size_usd = float(op.get("size_usd") or 0.0)
        if price <= 0 or size_usd <= 0:
            continue
        qty = max(1, int(size_usd / price))
        side = "BUY" if direction == "bullish" else "SSHORT"
        intent = "open_long" if direction == "bullish" else "open_short"
        orders.append(ActionOrder(
            action_id=f"{ticker}_{target_date}_OPEN",
            date=target_date,
            ticker=ticker,
            side=side,
            qty=qty,
            order_type="LMT",
            limit_price=round(price * (1.002 if side == "BUY" else 0.998), 4),
            tif="DAY",
            intent=intent,
            meta={
                "confidence": op.get("confidence"),
                "score": op.get("score"),
                "target_price": op.get("tp"),
                "stop_loss": op.get("sl"),
                "size_usd": size_usd,
            },
        ))

    for cl in new_closes:
        ticker = cl["ticker"]
        direction = (cl.get("direction") or "").lower()
        qty = int(cl.get("qty") or 0)
        if qty <= 0:
            continue
        side = "SELL" if direction == "bullish" else "BTC"
        intent = cl.get("exit_reason") or ("close_long" if direction == "bullish" else "close_short")
        orders.append(ActionOrder(
            action_id=f"{ticker}_{target_date}_CLOSE",
            date=target_date,
            ticker=ticker,
            side=side,
            qty=qty,
            order_type="MKT",
            limit_price=None,
            tif="DAY",
            intent=intent,
            meta={"exit_price_est": cl.get("exit_price")},
        ))

    out_file = ACTIONS_DIR / f"actions_{target_date}.json"
    payload = {
        "date": target_date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "account_type": "IBKR_PAPER",
        "orders_count": len(orders),
        "orders": [asdict(o) for o in orders],
        "instructions": (
            "在 IBKR TWS Paper Account 按此清单手动下单，"
            "或用 ibapi 脚本批量提交。成交后导出 Trade Confirmation CSV "
            "→ 运行 `python3 ibkr_sync.py import <csv_path>`"
        ),
    }
    with out_file.open("w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    _log.info(f"📤 exported {len(orders)} orders → {out_file.name}")
    return out_file


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2.2 - Import IBKR statement
# ══════════════════════════════════════════════════════════════════════════════

def import_ibkr_statement(csv_path: str) -> int:
    """解析 IBKR Trade Confirmation CSV → append real_fills.jsonl。

    IBKR TWS CSV 列（标准 FlexQuery）：
      Symbol, DateTime, Quantity, T. Price, Proceeds, Commission, Action, ...

    返回导入条数。
    """
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(csv_path)

    count = 0
    existing_ids = set()
    if FILLS_FILE.exists():
        with FILLS_FILE.open() as f:
            for line in f:
                try:
                    existing_ids.add(json.loads(line).get("fill_id"))
                except Exception:
                    pass

    with p.open(newline="") as f, FILLS_FILE.open("a") as out:
        reader = csv.DictReader(f)
        for row in reader:
            # 标准化字段
            ticker = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
            if not ticker:
                continue
            dt = (row.get("DateTime") or row.get("Date/Time") or "").strip()
            qty = float(row.get("Quantity") or row.get("quantity") or 0)
            price = float(row.get("T. Price") or row.get("TradePrice") or row.get("price") or 0)
            commission = float(row.get("Commission") or row.get("IBCommission") or 0)
            action = (row.get("Buy/Sell") or row.get("Action") or ("BUY" if qty > 0 else "SELL")).strip().upper()

            fill_id = f"{ticker}_{dt}_{action}_{abs(qty):.0f}"
            if fill_id in existing_ids:
                continue

            rec = {
                "fill_id": fill_id,
                "ticker": ticker,
                "datetime": dt,
                "action": action,
                "qty": abs(qty),
                "price": price,
                "commission": abs(commission),
                "imported_at": datetime.utcnow().isoformat() + "Z",
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1

    _log.info(f"📥 imported {count} fills from {p.name}")
    return count


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2.3 - Reconcile
# ══════════════════════════════════════════════════════════════════════════════

def reconcile(target_date: Optional[str] = None) -> Dict:
    """对比本地模拟 vs IBKR 真实成交，生成 slippage/fill diff 报告。

    Args:
        target_date: 指定日期；None 则对账所有历史
    """
    # 加载本地 closed_trades
    import paper_portfolio as _pp
    local_trades: List[Dict] = []
    if _pp.CLOSED_FILE.exists():
        with _pp.CLOSED_FILE.open() as f:
            for line in f:
                try:
                    local_trades.append(json.loads(line))
                except Exception:
                    pass

    # 加载真实 fills
    real_fills: List[Dict] = []
    if FILLS_FILE.exists():
        with FILLS_FILE.open() as f:
            for line in f:
                try:
                    real_fills.append(json.loads(line))
                except Exception:
                    pass

    # 按 ticker+date 配对
    matched = []
    unmatched_local = []
    unmatched_real = list(real_fills)

    for lt in local_trades:
        tkr = lt.get("ticker")
        lt_date = lt.get("exit_date") or lt.get("entry_date")
        if target_date and lt_date != target_date:
            continue
        best_match = None
        # 兼容 IBKR 两种 datetime 格式：'20260415;140000' / '2026-04-15 14:00:00'
        exit_d = (lt.get("exit_date") or "").replace("-", "")[:8]  # '20260415'
        exit_d_iso = (lt.get("exit_date") or "")[:10]               # '2026-04-15'
        for rf in unmatched_real:
            if rf["ticker"] != tkr:
                continue
            rf_dt = rf.get("datetime") or ""
            if rf_dt.startswith(exit_d) or rf_dt.startswith(exit_d_iso):
                best_match = rf
                break
        if best_match:
            slip = (best_match["price"] - (lt.get("exit_price") or 0)) / max(abs(lt.get("exit_price") or 1), 0.01) * 100
            matched.append({
                "ticker": tkr,
                "local_exit": lt.get("exit_price"),
                "real_fill": best_match["price"],
                "slippage_pct": round(slip, 3),
                "commission": best_match["commission"],
            })
            unmatched_real.remove(best_match)
        else:
            unmatched_local.append({"ticker": tkr, "date": lt_date})

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "target_date": target_date or "all",
        "matched_count": len(matched),
        "unmatched_local_count": len(unmatched_local),
        "unmatched_real_count": len(unmatched_real),
        "avg_slippage_pct": round(
            sum(m["slippage_pct"] for m in matched) / max(len(matched), 1), 3
        ),
        "matched": matched,
        "unmatched_local": unmatched_local[:20],
        "unmatched_real": [
            {"ticker": r["ticker"], "datetime": r["datetime"], "price": r["price"]}
            for r in unmatched_real[:20]
        ],
    }
    out_file = RECONCILE_DIR / f"reconcile_{target_date or 'all'}.json"
    with out_file.open("w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    _log.info(
        f"🔍 reconcile → matched={len(matched)} "
        f"local_only={len(unmatched_local)} real_only={len(unmatched_real)}"
    )
    return report


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser(description="Alpha Hive IBKR Sync Bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="导出今日订单 JSON（从 paper_portfolio.run_for_date 结果）")
    p_exp.add_argument("--date", default=_date.today().isoformat())

    p_imp = sub.add_parser("import", help="导入 IBKR trade CSV")
    p_imp.add_argument("csv_path")

    p_rec = sub.add_parser("reconcile", help="对账本地模拟 vs 真实成交")
    p_rec.add_argument("--date", default=None)

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "export":
        # 先 run_for_date（幂等），然后从 state 里提取 target_date 的 opens/closes
        import paper_portfolio as _pp
        _pp.run_for_date(args.date, verbose=False)

        # 提取：新开仓 = positions.jsonl 里 entry_date == date
        positions = _pp._load_jsonl(_pp.POSITIONS_FILE)
        new_opens = [
            {
                "ticker": p["ticker"],
                "direction": p["direction"],
                "size_usd": p["size_usd"],
                "entry_price": p["entry_price"],
                "sl": p.get("sl_price"),
                "tp": p.get("tp_price"),
                "confidence": p.get("confidence"),
                "score": p.get("score"),
            }
            for p in positions if p.get("entry_date") == args.date
        ]
        # 平仓 = closed_trades 里 exit_date == date
        closed = _pp._load_jsonl(_pp.CLOSED_FILE)
        new_closes = [
            {
                "ticker": c["ticker"],
                "direction": c["direction"],
                "qty": c.get("qty") or max(1, int((c.get("size_usd") or 0) / max(c.get("entry_price") or 1, 0.01))),
                "exit_price": c.get("exit_price"),
                "exit_reason": c.get("exit_reason"),
            }
            for c in closed if c.get("exit_date") == args.date
        ]
        export_daily_actions(args.date, new_opens, new_closes)
    elif args.cmd == "import":
        n = import_ibkr_statement(args.csv_path)
        print(f"Imported {n} fills.")
    elif args.cmd == "reconcile":
        rep = reconcile(args.date)
        print(json.dumps(rep, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
