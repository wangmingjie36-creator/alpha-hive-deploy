#!/usr/bin/env python3
"""
🐝 Alpha Hive — 收盘价校正 (v0.45.41)
======================================
`price_at_predict` 被**盘后价**污染，用官方收盘回填修正。

为什么需要
----------
扫描跑在 14:00 PDT = **17:00 ET**，正处盘后时段（16:00–20:00 ET）正中间。
CBOE payload 的 `current_price` **跟着盘后交易走**，而 `last_trade_time`
钉死在 16:00 收盘 —— 所以 v0.45.39 的 vintage 校验对它**完全无效**：
日期是今天、成交时刻是收盘，一切「合规」，价格却是盘后价。
校验的判据和被污染的字段根本不是同一个东西。

2026-08-26 实测（云端快照 30 只对账官方收盘）：**16 只偏离 >0.2%**，
其中 CRM 偏 **+13.80%**（快照 234.0 / 官方收盘 205.62）—— 当天 CRM 发财报
（yfinance `Earnings Date: 2026-08-26` 确认），盘后涨约 12%。

**它恰好在财报日最狠**，而那正是信息量最大的日子：评分用的是财报前的数据，
价格基准却已吃掉财报后那根 K 线，`close_t7/price_at_predict − 1` 的起点
含了本该被测量的那段收益 —— 评分与收益的联系在最该成立的日子里断掉。

判据来源：两个独立源互相印证
-----------------------------
- **yfinance 官方收盘**：批量下载，30 只一次请求。与逐标的调用是两回事，
  不会触发限流雪崩（2026-08-26 扫描逐标的调用触发 487 次限流，
  同一天批量下载全程正常）。
- **CBOE `prev_day_close`**：仅 T+1 可得。实测与官方收盘分毫不差
  （CRM 205.69 = yfinance 8/25 收盘）。取用前**必须过 vintage 校验** ——
  CDN 卡死的符号（实测 TMO 卡 44.5 小时）其 `prev_day_close` 指的是更早的日子。

两源都在且分歧 > `DISPUTE_TOL` → **不修**，记入 disputed 待查。
只有一个 → 修，但 source 标明单源。一个都没有 → 不动。

留痕
----
原值存进 `price_at_predict_raw`（仅首次校正时写入，重复运行不会覆盖），
`close_corrected_at` / `close_correction_source` 记录来源。幂等。

⚠️ 校正后必须重算派生列
-----------------------
`return_t7` / `dir_correct_t7` / `net_return_t7` 等都以 `price_at_predict`
为起点。本脚本**只改基准价，不动派生列** —— 跑完请接：

    /usr/local/bin/python3 backfill_dir_accuracy.py --all

用法
----
    python3 close_correction.py                    # dry-run，只报不改
    python3 close_correction.py --apply            # 落笔
    python3 close_correction.py --apply --since 2026-08-01
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_log = logging.getLogger("alpha_hive.close_correction")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pheromone.db")

# 低于此偏离视为同一个价，不动（浮点/复权噪声）
CORRECT_TOL = 0.001
# 两个来源分歧超过此值 → 拒绝校正，留给人看
DISPUTE_TOL = 0.002

_NEW_COLUMNS = (
    ("price_at_predict_raw", "REAL"),
    ("close_corrected_at", "TEXT"),
    ("close_correction_source", "TEXT"),
)


def ensure_columns(conn: sqlite3.Connection) -> None:
    """幂等加列。与 backtester._migrate_options_columns 同模式。"""
    for col, typ in _NEW_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass


def load_rows(conn: sqlite3.Connection, since: Optional[str] = None) -> List[dict]:
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, date, ticker, price_at_predict, price_at_predict_raw, "
           "close_corrected_at FROM predictions WHERE price_at_predict > 0")
    args: List = []
    if since:
        sql += " AND date >= ?"
        args.append(since)
    return [dict(r) for r in conn.execute(sql, args)]


def official_closes(tickers: List[str], lo: str, hi: str) -> Dict[Tuple[str, str], float]:
    """yfinance 批量官方收盘 → {(date, ticker): close}。失败返回空表（诚实缺失）。

    刻意用 `yf.download` 批量而非逐标的 `Ticker.history`：一次请求覆盖全部标的，
    这正是它在扫描被限流的同一天仍然可用的原因。
    """
    import datetime as dt
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError as e:
        _log.error("yfinance/pandas 不可得：%s", e)
        return {}
    end = (dt.date.fromisoformat(hi) + dt.timedelta(days=1)).isoformat()
    try:
        h = yf.download(sorted(set(tickers)), start=lo, end=end,
                        progress=False, auto_adjust=False)["Close"]
    except Exception as e:  # noqa: BLE001 - 拿不到就空表，调用方据此不动数据
        _log.error("yfinance 批量下载失败：%s: %s", type(e).__name__, e)
        return {}
    if hasattr(h, "to_frame") and not hasattr(h, "columns"):
        h = h.to_frame(name=sorted(set(tickers))[0])
    out: Dict[Tuple[str, str], float] = {}
    for d, row in h.iterrows():
        ds = d.strftime("%Y-%m-%d")
        for t in h.columns:
            v = row[t]
            if v is not None and not pd.isna(v):
                out[(ds, t)] = float(v)
    return out


def cboe_prev_closes(tickers: List[str]) -> Dict[str, float]:
    """CBOE `prev_day_close` → {ticker: close}。**只对上一个交易日有效**。

    走 `_fetch_cboe_payload`，因此自动继承 v0.45.39 的 vintage 校验 ——
    CDN 卡死的符号会返回 None 而不是它那份陈旧的 prev_day_close。
    """
    out: Dict[str, float] = {}
    try:
        import cboe_options as co
    except ImportError:
        return out
    for t in sorted(set(tickers)):
        try:
            p = co._fetch_cboe_payload(t, 15)  # noqa: SLF001 - 见 docstring
        except Exception:  # noqa: BLE001
            continue
        v = (p or {}).get("prev_day_close")
        if v:
            out[t] = float(v)
    return out


def correct(conn: sqlite3.Connection, *, since: Optional[str] = None,
            apply: bool = False, use_cboe: bool = True) -> dict:
    ensure_columns(conn)
    rows = load_rows(conn, since)
    if not rows:
        return {"rows": 0}

    tickers = sorted({r["ticker"] for r in rows})
    lo, hi = min(r["date"] for r in rows), max(r["date"] for r in rows)
    closes = official_closes(tickers, lo, hi)
    if not closes:
        _log.error("拿不到任何官方收盘 —— 不做任何改动")
        return {"rows": len(rows), "aborted": "no_official_closes"}

    # CBOE 交叉印证只对「上一个交易日」有效 —— 因此**只抓那天有样本的标的**，
    # 而不是全部 52 只（全抓会串行拉 52 次大 JSON，白等 5 分钟）
    prev_td = _prev_trading_day()
    _pt = sorted({r["ticker"] for r in rows if r["date"] == prev_td}) if prev_td else []
    cboe = cboe_prev_closes(_pt) if (use_cboe and _pt) else {}
    if _pt:
        _log.info("CBOE 交叉印证：%s 有 %d 只样本，已取回 %d 只的 prev_day_close",
                  prev_td, len(_pt), len(cboe))

    stats = {"rows": len(rows), "corrected": 0, "already_ok": 0,
             "no_source": 0, "disputed": 0, "skipped_done": 0,
             "cross_checked": 0, "worst": []}
    cur = conn.cursor()
    import datetime as dt
    now = dt.datetime.now().isoformat(timespec="seconds")

    for r in rows:
        # ⚠️ 「要不要动」看**当前值**，不看 raw。raw 是留痕用的原值，校正后
        # 它必然仍偏离官方收盘 —— 拿它做判据会让重跑永远报「需校正 N 条」，
        # 数据没写错（COALESCE 护住了 raw），但报告在撒谎。
        current = r["price_at_predict"]
        raw = r["price_at_predict_raw"] if r["price_at_predict_raw"] else current
        truth = closes.get((r["date"], r["ticker"]))
        if truth is None:
            stats["no_source"] += 1
            continue
        src = "yfinance_close"
        # 交叉印证：仅当该行日期恰是上一个交易日
        if r["date"] == prev_td and r["ticker"] in cboe:
            other = cboe[r["ticker"]]
            if abs(other / truth - 1) > DISPUTE_TOL:
                stats["disputed"] += 1
                _log.warning("两源分歧，拒绝校正 %s %s：yfinance=%.4f CBOE=%.4f",
                             r["date"], r["ticker"], truth, other)
                continue
            src = "yfinance_close+cboe_prev"
            stats["cross_checked"] += 1

        if abs(current / truth - 1) <= CORRECT_TOL:
            if r["close_corrected_at"]:
                stats["skipped_done"] += 1
            else:
                stats["already_ok"] += 1
            continue
        dev = raw / truth - 1          # 展示用：原值相对官方收盘偏了多少
        stats["corrected"] += 1
        stats["worst"].append((abs(dev), r["date"], r["ticker"], raw, truth, dev * 100))
        if apply:
            cur.execute(
                "UPDATE predictions SET price_at_predict_raw = COALESCE(price_at_predict_raw, ?), "
                "price_at_predict = ?, close_corrected_at = ?, close_correction_source = ? "
                "WHERE id = ?",
                (current, truth, now, src, r["id"]))
    if apply:
        conn.commit()
    stats["worst"].sort(reverse=True)
    stats["worst"] = stats["worst"][:15]
    return stats


def _prev_trading_day() -> Optional[str]:
    try:
        import datetime as dt
        from is_trading_day import is_trading_day
        d = dt.date.today() - dt.timedelta(days=1)
        for _ in range(10):
            if is_trading_day(d)[0]:
                return d.isoformat()
            d -= dt.timedelta(days=1)
    except Exception:  # noqa: BLE001
        pass
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="用官方收盘校正被盘后价污染的 price_at_predict")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--since", default=None, help="只处理该日期及之后（YYYY-MM-DD）")
    ap.add_argument("--apply", action="store_true", help="真的写入（默认 dry-run）")
    ap.add_argument("--no-cboe", action="store_true", help="跳过 CBOE 交叉印证")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        _log.error("⛔ 样本库不存在：%s", args.db)
        return 3
    conn = sqlite3.connect(args.db)
    try:
        st = correct(conn, since=args.since, apply=args.apply, use_cboe=not args.no_cboe)
    finally:
        conn.close()

    if st.get("aborted"):
        _log.error("⛔ 中止：%s", st["aborted"])
        return 3
    if not st["rows"]:
        _log.info("无样本")
        return 0

    mode = "已写入" if args.apply else "DRY-RUN（未改动，加 --apply 落笔）"
    _log.info("\n📐 收盘价校正 —— %s", mode)
    _log.info("  样本 %d 条 | 需校正 %d | 本就正确 %d | 已校正过 %d | 无来源 %d | 两源分歧拒改 %d",
              st["rows"], st["corrected"], st["already_ok"],
              st["skipped_done"], st["no_source"], st["disputed"])
    _log.info("  其中经 CBOE 交叉印证：%d 条", st["cross_checked"])
    if st["worst"]:
        _log.info("\n  偏离最大的 %d 条：", len(st["worst"]))
        _log.info(f"  {'日期':11s}{'标的':7s}{'原值':>11s}{'官方收盘':>11s}{'偏离':>9s}")
        for _, d, t, b, tr, pc in st["worst"]:
            _log.info(f"  {d:11s}{t:7s}{b:>11.2f}{tr:>11.2f}{pc:>8.2f}%")
    if st["corrected"] and args.apply:
        _log.info("\n⚠️ 派生列（return_t7 / dir_correct_t7 / net_return_t7）以 price_at_predict "
                  "为起点，现已过时。请接着跑：")
        _log.info("   /usr/local/bin/python3 backfill_dir_accuracy.py --all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
