#!/usr/bin/env python3
"""
🐝 Alpha Hive — 回填「纯方向」准确率字段 (v0.45.17)
==================================================
把历史 T+7 样本的 `close_t7` / `dir_correct_t7` / `dir_ambiguous_t7` 补齐。

为什么需要这个
--------------
`predictions.correct_t7` 是由**路径依赖的离场收益**算出来的：T+7 回测走
`_simulate_trade_path`，一旦触发 SL/TP 就提前离场，收益被钳在止损/止盈档位
（库里 `-10.04` / `+9.95` 反复出现即此故）。于是：

- 对**方向单**，`correct_t7` 实际回答的是「这笔交易赚钱了吗」；
- 对**中性单**，它从不建仓、从无 SL/TP，回答的是「价格真的没大动吗」。

两者混进同一个分母报成「整体准确率」，是苹果比橘子。网站上
52.6%(869) / 56.2%(54/96) / 51.1%(256/501) 三张卡都建立在这个混合口径上。

更麻烦的是 `price_t7` 也不可靠：自 2026-05 起它 100% 等于 `exit_price`
（同样被路径截断）。所以**库里根本没有存方向单的真实 T+7 收盘价**，
必须重新取数才能算真方向精度。

方法
----
1. 按标的批量 `yf.download`（**不用 `Ticker().history()`**——实测在本机
   间歇性抛 `TypeError: 'NoneType' object is not subscriptable`）。
   `yf.download` 返回 MultiIndex 列，按 [[alpha-hive-yfinance-multiindex]] 的标准修法解包。
2. T+7 用 pandas `CustomBusinessDay(US)` 算真实交易日偏移，与
   `backtester._get_price_at_date` 同一口径。
3. 方向判定走 `outcome_utils.determine_outcome_triplet`（与主流程同源，
   不自写评分逻辑）。
4. **自校验**：对 `exit_reason='T7_CLOSE'` 的行，没触发 SL/TP，
   其 `price_t7` 本就应当等于真实 T+7 收盘价。若我算出的价与之偏离过大，
   说明取价口径错了 —— 脚本会报出偏离率，超阈值直接中止而不是硬写。

用法
----
    /usr/local/bin/python3 backfill_dir_accuracy.py --dry-run   # 只看不写
    /usr/local/bin/python3 backfill_dir_accuracy.py             # 实写

⚠️ 写库前请自行备份（惯例：`db_backups/pheromone_pre_*.db`）。
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_log = logging.getLogger("backfill_dir_accuracy")

DB = Path(__file__).resolve().parent / "pheromone.db"
HOLD_TRADING_DAYS = 7
# 自校验判据：护栏要防的是**系统性偏移**（交易日数算错、复权口径不一致），
# 这类错误必然体现在**中位偏离**上；而尾部零星偏离来自原始跑批当时的数据毛刺
# （实测 543 条 T7_CLOSE 中 484 条偏离恰为 0.000pp，59 条有偏离，
#  其中 18 条连入场价都跟库里对不上——是当时存价就有问题，不是本次取价错）。
# 所以门用中位数把关，尾部只报不拦；再加一道**方向翻转率**，因为本脚本
# 只产出方向标签，符号才是真正要紧的量。
SELF_CHECK_MEDIAN_MAX_PP = 0.10   # 中位偏离超此值 → 系统性错误，中止
SELF_CHECK_FLIP_MAX = 0.15        # 收益符号翻转率超此值 → 中止
SELF_CHECK_REPORT_PP = 0.5        # 仅用于报告尾部条数


def _load_rows(conn: sqlite3.Connection, only_missing: bool) -> list[dict]:
    where = "WHERE checked_t7 = 1 AND price_at_predict > 0"
    if only_missing:
        where += " AND dir_correct_t7 IS NULL AND dir_ambiguous_t7 IS NULL"
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"""
        SELECT id, date, ticker, direction, price_at_predict,
               price_t7, exit_reason
        FROM predictions {where} ORDER BY ticker, date
    """)]


def _fetch_closes(tickers: list[str], start: str, end: str) -> dict:
    """按标的批量取收盘价。返回 {ticker: pandas.Series(index=日期)}。"""
    import pandas as pd
    import yfinance as yf

    out: dict = {}
    # 分批，避免单次请求过宽被限流
    for i in range(0, len(tickers), 10):
        chunk = tickers[i:i + 10]
        try:
            # auto_adjust=False：只要**拆股复权**，不要分红复权。
            # 方向精度衡量的是交易者看到的真实价格走势，不是总回报；
            # 库里 price_at_predict / price_t7 存的也是真实成交价。
            # 用 auto_adjust=True 会把分红也复权掉，高股息标的在除息日附近
            # 凭空多出 0.5~2% 的偏离（实测自校验失败率 8.8% → 12.3%）。
            df = yf.download(chunk, start=start, end=end,
                             progress=False, auto_adjust=False, threads=False)
        except Exception as exc:                       # noqa: BLE001
            _log.warning("批量取价失败 %s: %s", chunk, exc)
            continue
        if df is None or df.empty:
            continue
        # MultiIndex 解包：单标的与多标的返回结构不同，必须分开处理
        if isinstance(df.columns, pd.MultiIndex):
            for tk in chunk:
                try:
                    s = df["Close"][tk].dropna()
                except (KeyError, IndexError):
                    continue
                if not s.empty:
                    out[tk] = s
        else:
            s = df["Close"].dropna()
            if not s.empty:
                out[chunk[0]] = s
    return out


def _close_after(series, target_date):
    """取 target_date 当天或之后第一个有效收盘价。"""
    import pandas as pd
    idx = series.index
    tgt = pd.Timestamp(target_date)
    if getattr(idx, "tz", None) is not None:
        tgt = tgt.tz_localize(idx.tz)
    hit = series[idx >= tgt]
    return (float(hit.iloc[0]), hit.index[0].date().isoformat()) if len(hit) else (None, None)


def _close_at_or_before(series, target_date):
    """取 target_date 当天或之前最后一个有效收盘价（用于入场基准）。"""
    import pandas as pd
    idx = series.index
    tgt = pd.Timestamp(target_date)
    if getattr(idx, "tz", None) is not None:
        tgt = tgt.tz_localize(idx.tz)
    hit = series[idx <= tgt]
    return float(hit.iloc[-1]) if len(hit) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--all", action="store_true", help="重算全部（默认只补空缺）")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    import pandas as pd
    from pandas.tseries.holiday import USFederalHolidayCalendar
    from pandas.tseries.offsets import CustomBusinessDay
    from outcome_utils import determine_outcome_triplet

    us_bday = CustomBusinessDay(calendar=USFederalHolidayCalendar())

    conn = sqlite3.connect(args.db)
    # 确保新列存在（幂等；正常由 backtester 的迁移建好）
    for col, typ in (("close_t7", "REAL"), ("dir_correct_t7", "INTEGER"),
                     ("dir_ambiguous_t7", "INTEGER")):
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass

    rows = _load_rows(conn, only_missing=not args.all)
    if not rows:
        _log.info("无待回填样本")
        return 0

    by_ticker = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)
    lo = min(r["date"] for r in rows)
    hi = max(r["date"] for r in rows)
    hi_pad = (pd.Timestamp(hi) + 25 * us_bday).date().isoformat()
    _log.info("待回填 %d 条 / %d 只标的 / %s → %s",
              len(rows), len(by_ticker), lo, hi)

    closes = _fetch_closes(sorted(by_ticker), lo, hi_pad)
    _log.info("取到价格序列 %d 只", len(closes))

    updates, self_check, misses = [], [], 0
    for tk, group in by_ticker.items():
        series = closes.get(tk)
        if series is None:
            misses += len(group)
            continue
        for r in group:
            target = (pd.Timestamp(r["date"]) + HOLD_TRADING_DAYS * us_bday).date()
            close, _ = _close_after(series, target)
            if close is None or close <= 0:
                misses += 1
                continue
            # ⚠️ 拆股：yfinance 返回的是**复权价**，而库里 `price_at_predict`
            # 是预测当日存下的**未复权价**。二者相减，遇到拆股就是垃圾
            # （实测 CRWD 2026-04-26 4:1 拆股，库存 476.53 vs 复权 119.13，偏离 75%）。
            # 故收益一律用**同一条复权序列的两端**算，拆股因子自动抵消。
            p0_adj = _close_at_or_before(series, r["date"])
            if not p0_adj or p0_adj <= 0:
                misses += 1
                continue
            raw_ret = (close - p0_adj) / p0_adj * 100
            ok, amb = determine_outcome_triplet(r["direction"], raw_ret)
            updates.append((close, 1 if ok else 0, 1 if amb else 0, r["id"]))
            # 自校验：未触发 SL/TP 的行，price_t7 应≈真实收盘价
            # 自校验也必须在复权空间内做：拿库里 price_t7/price_at_predict 的
            # **收益**（同为未复权，比值消掉拆股）对比重算收益。
            if (r["exit_reason"] == "T7_CLOSE" and r["price_t7"]
                    and r["price_at_predict"]):
                db_ret = (r["price_t7"] - r["price_at_predict"]) / r["price_at_predict"] * 100
                self_check.append((abs(raw_ret - db_ret), raw_ret, db_ret))

    if self_check:
        import statistics
        devs = [d for d, _, _ in self_check]
        med = statistics.median(devs)
        tail = sum(1 for d in devs if d > SELF_CHECK_REPORT_PP)
        flips = sum(1 for _, a, b in self_check
                    if (a > 0) != (b > 0) and min(abs(a), abs(b)) > 0.5)
        flip_rate = flips / len(self_check)
        _log.info("自校验(收益口径)：T7_CLOSE %d 条 | 中位偏离 %.3fpp | "
                  ">%.1fpp 的 %d 条 (%.1f%%) | 符号翻转 %d 条 (%.1f%%)",
                  len(self_check), med, SELF_CHECK_REPORT_PP, tail,
                  tail / len(self_check) * 100, flips, flip_rate * 100)
        if med > SELF_CHECK_MEDIAN_MAX_PP:
            _log.error("中位偏离 %.3fpp 超阈值 %.2fpp —— 存在系统性口径错误"
                       "（交易日数/复权），已中止未写库。", med, SELF_CHECK_MEDIAN_MAX_PP)
            return 2
        if flip_rate > SELF_CHECK_FLIP_MAX:
            _log.error("符号翻转率 %.1f%% 超阈值 %.0f%% —— 已中止未写库。",
                       flip_rate * 100, SELF_CHECK_FLIP_MAX * 100)
            return 2
    _log.info("可回填 %d 条，取价失败 %d 条", len(updates), misses)

    if args.dry_run:
        _log.info("--dry-run：未写库")
        return 0
    conn.executemany("""UPDATE predictions
                        SET close_t7 = ?, dir_correct_t7 = ?, dir_ambiguous_t7 = ?
                        WHERE id = ?""", updates)
    conn.commit()
    _log.info("已写入 %d 条", len(updates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
