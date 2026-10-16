#!/usr/bin/env python3
"""按 NYSE 日历离线重算 v0.45.520 查出的历史 T+N 标签（v0.45.521，用户批准）。

**为什么。** v0.45.520 之前 `backtester` / `outcomes_fetcher` / `backfill_dir_accuracy` 用 pandas 联邦假日表算 T+N：
哥伦布日、退伍军人日 NYSE 开市却被判休，耶稣受难日 NYSE 休市却被判开。取价取「联邦目标日当天或之后第一根 bar」⇒
窗口跨这些日子的标签差一个交易日。520 只修了代码（此后的标签算对），已回填的历史标签由本工具重算。

**改什么**（只改「取价日 ≠ NYSE 应取日」的已回填行）：
- `predictions` T+1 / T+30：`price_tN` / `return_tN` / `correct_tN` / `ambiguous_tN`——与 `backtester` 同式
  （`(价 − price_at_predict) / price_at_predict × 100`，判定 `outcome_utils.determine_outcome_triplet`）。
- `predictions` T+7：`close_t7` / `dir_correct_t7` / `dir_ambiguous_t7`（同上式）。
- `report_snapshots/*.json` 的 `actual_prices.t1/t7/t30`（经 `ReportSnapshot.load_from_json` → `save_to_json`，
  读回再存只改改过的字段，v0.45.491）。

**不改**（需出网 / 需整条价格路径，本工具只离线）：T+7 路径字段（`price_t7` / `return_t7` / `correct_t7` /
`net_return_t7` / `exit_*` / `holding_days`，SL/TP 是否触发要重拉 OHLC）、`spy_return_t7`（本地没有 SPY 价）、
`agent_memory.outcome_return_*`（另有「后一轮回填把前一轮写成 None」的既有缺陷，v0.45.527 修代码；历史按快照重建另议）。数目在报告里列出。

**价格来源**：本地价格索引（`price_history._read_index`）+ `predictions.price_at_predict`，库优先——与
`price_history.load_price_history` 合并口径相同；只读，不触发它的一次性迁移写入。

**闸**（任一不过就不写那一行 / 整次中止）：
1. 自校验：存值必须等于「旧日历取价日」的收盘（±0.2%）——否则说明这行不是本 bug 造成的，跳过并列出，不覆盖；
2. 应取日价格本地必须有，没有就跳过并列出（不出网、不猜）；
3. `--apply` 才写；写前 `backup_before_write`（落 `db_backups/`，归因并发写入者靠它的 mtime）+ 受影响快照逐份复制，备份失败即中止；
4. 写库在一个事务里，`UPDATE ... WHERE id=? AND 旧值=?`，影响行数必须恰为 1，否则整体回滚；前后 `PRAGMA integrity_check`。
每行的新旧值写进审计 JSON（与快照备份同目录）。重跑幂等：存值已精确等于应取日收盘的记「已是应取日值」跳过——
这一判断必须先于自校验（v0.45.521 apply 后实测：两天价差 <0.2% 的 52 处，新值同样「等于」旧取价日收盘，若先自校验会被再计划一遍）。

用法：
    /usr/local/bin/python3 relabel_nyse_calendar.py            # dry-run（缺省）：只报告
    /usr/local/bin/python3 relabel_nyse_calendar.py --apply    # 备份后写入
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import glob
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from is_trading_day import add_trading_days, is_trading_day

VERSION = "v0.45.521"
OFFSETS = {1: "t1", 7: "t7", 30: "t30"}
TOL = 0.002


@functools.lru_cache(maxsize=None)
def _old_bday():
    import pandas as pd  # noqa: F401
    from pandas.tseries.holiday import USFederalHolidayCalendar
    from pandas.tseries.offsets import CustomBusinessDay
    return CustomBusinessDay(calendar=USFederalHolidayCalendar())


@functools.lru_cache(maxsize=None)
def _old_calendar_target(d: dt.date, n: int) -> dt.date:
    """**故意重建**旧的（错的）联邦假日表口径：`d + n × CustomBusinessDay(USFederalHolidayCalendar)`，再取其当天或之后
    第一个 NYSE 交易日（取价语义：窗口里第一根 bar）。只用于自校验「存值就是那天的收盘」——不用于算新值。
    `tests/test_trading_calendar_nyse.py` 的 AST 守卫为此单独登记了本文件。"""
    import pandas as pd
    t = (pd.Timestamp(d) + n * _old_bday()).date()
    while not is_trading_day(t)[0]:
        t += dt.timedelta(days=1)
    return t


def _db_path() -> Path:
    from hive_logger import PATHS
    return Path(PATHS.db)


def _snapshots_dir() -> str:
    from hive_logger import PATHS
    return os.path.join(str(PATHS.home), "report_snapshots")


class Prices:
    """本地收盘价：价格索引 + predictions.price_at_predict（库优先）。只读。"""

    def __init__(self, conn: sqlite3.Connection):
        from hive_logger import PATHS
        self._conn, self._cache_dir, self._by_ticker = conn, str(PATHS.cache_dir), {}

    def close(self, ticker: str, d: dt.date):
        if ticker not in self._by_ticker:
            from price_history import _read_index
            m = dict(_read_index(ticker, self._cache_dir))
            m.update({r[0][:10]: r[1] for r in self._conn.execute(
                "SELECT date, price_at_predict FROM predictions WHERE ticker=? AND price_at_predict > 0", (ticker,))})
            self._by_ticker[ticker] = m
        return self._by_ticker[ticker].get(d.isoformat())


def _same(a, b) -> bool:
    return a is not None and b is not None and b > 0 and abs(a - b) / b <= TOL


def _ret(px: float, p0: float) -> float:
    return (px - p0) / p0 * 100


def plan_db(conn: sqlite3.Connection, prices: Prices):
    """返回 (改动列表, 计数)。改动 = dict(table 行 id, period, 旧值, 新值)。"""
    from outcome_utils import determine_outcome_triplet
    changes, st = [], Counter()
    q = {1: "SELECT id, date, ticker, direction, price_at_predict, price_t1, return_t1, correct_t1, ambiguous_t1 "
            "FROM predictions WHERE checked_t1 = 1 AND price_t1 IS NOT NULL",
         7: "SELECT id, date, ticker, direction, price_at_predict, close_t7, NULL, dir_correct_t7, dir_ambiguous_t7 "
            "FROM predictions WHERE close_t7 IS NOT NULL",
         30: "SELECT id, date, ticker, direction, price_at_predict, price_t30, return_t30, correct_t30, ambiguous_t30 "
             "FROM predictions WHERE checked_t30 = 1 AND price_t30 IS NOT NULL"}
    for n, sql in q.items():
        k = OFFSETS[n]
        for rid, d, tk, direction, p0, stored, old_ret, old_ok, old_amb in conn.execute(sql):
            day = dt.date.fromisoformat(str(d)[:10])
            used, right = _old_calendar_target(day, n), add_trading_days(day, n)
            if used == right:
                continue
            st[f"{k} 受影响"] += 1
            pu, pr = prices.close(tk, used), prices.close(tk, right)
            if pr is not None and stored == pr:
                st[f"{k} 已是应取日值"] += 1          # 先于自校验：两天价差 <0.2% 时新值也会「等于」旧取价日收盘
                continue
            if not _same(stored, pu):
                st[f"{k} 跳过：存值≠旧取价日收盘"] += 1
                continue
            if pr is None:
                st[f"{k} 跳过：应取日本地无价"] += 1
                continue
            if not p0 or p0 <= 0:
                st[f"{k} 跳过：无入场价"] += 1
                continue
            r = _ret(pr, p0)
            ok, amb = determine_outcome_triplet(direction, r)
            new = {"price": pr, "ret": round(r, 3), "correct": int(ok), "ambiguous": int(amb)}
            old = {"price": stored, "ret": old_ret, "correct": old_ok, "ambiguous": old_amb}
            if (old_ok, old_amb) != (new["correct"], new["ambiguous"]):
                st[f"{k} 方向判定改变"] += 1
            changes.append({"id": rid, "date": str(d)[:10], "ticker": tk, "period": k, "used": used.isoformat(),
                            "right": right.isoformat(), "old": old, "new": new})
            st[f"{k} 将重算"] += 1
    return changes, st


def plan_snapshots(snap_dir: str, prices: Prices):
    changes, st = [], Counter()
    for f in sorted(glob.glob(os.path.join(snap_dir, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            st["快照读不了"] += 1
            continue
        base, tk, ap = str(raw.get("date") or "")[:10], raw.get("ticker"), raw.get("actual_prices") or {}
        if not base or not tk or not isinstance(ap, dict):
            continue
        day = dt.date.fromisoformat(base)
        for n, k in OFFSETS.items():
            stored = ap.get(k)
            if stored is None:
                continue
            used, right = _old_calendar_target(day, n), add_trading_days(day, n)
            if used == right:
                continue
            st[f"快照 {k} 受影响"] += 1
            pu, pr = prices.close(tk, used), prices.close(tk, right)
            if pr is not None and stored == pr:
                st[f"快照 {k} 已是应取日值"] += 1
                continue
            if not _same(stored, pu):
                st[f"快照 {k} 跳过：存值≠旧取价日收盘"] += 1
                continue
            if pr is None:
                st[f"快照 {k} 跳过：应取日本地无价"] += 1
                continue
            changes.append({"file": os.path.basename(f), "period": k, "used": used.isoformat(),
                            "right": right.isoformat(), "old": stored, "new": pr})
            st[f"快照 {k} 将重算"] += 1
    return changes, st


def untouched_counts(conn: sqlite3.Connection) -> Counter:
    """不在本工具范围、但同样受影响的列（只计数，进报告）。"""
    st = Counter()
    for rid, d, er, spy in conn.execute(
            "SELECT id, date, exit_reason, spy_return_t7 FROM predictions WHERE checked_t7 = 1"):
        day = dt.date.fromisoformat(str(d)[:10])
        if _old_calendar_target(day, 7) != add_trading_days(day, 7):
            st[f"T+7 路径字段未改（exit_reason={er}）"] += 1
            if spy is not None:
                st["spy_return_t7 未改（需 SPY 价，出网）"] += 1
    return st


def _integrity(db: Path) -> str:
    c = sqlite3.connect(str(db))
    try:
        return c.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        c.close()


def apply(db: Path, snap_dir: str, db_changes, snap_changes, backup_root: Path) -> dict:
    from data_migrations.runner import backup_before_write
    from feedback_loop import ReportSnapshot

    if _integrity(db) != "ok":
        raise SystemExit("写前 integrity_check 不是 ok，中止")
    db_backup = backup_before_write(db, f"pre-{VERSION}-nyse-relabel", backup_dir=db.parent / "db_backups")
    snap_backup = backup_root / f"report_snapshots-pre-{VERSION}-nyse-relabel"
    snap_backup.mkdir(parents=True, exist_ok=False)
    for name in sorted({c["file"] for c in snap_changes}):
        shutil.copy2(os.path.join(snap_dir, name), snap_backup / name)

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("BEGIN IMMEDIATE")
        for c in db_changes:
            k, o, n = c["period"], c["old"], c["new"]
            if k == "t7":
                cur = conn.execute(
                    "UPDATE predictions SET close_t7 = ?, dir_correct_t7 = ?, dir_ambiguous_t7 = ? "
                    "WHERE id = ? AND close_t7 = ?", (n["price"], n["correct"], n["ambiguous"], c["id"], o["price"]))
            else:
                cur = conn.execute(
                    f"UPDATE predictions SET price_{k} = ?, return_{k} = ?, correct_{k} = ?, ambiguous_{k} = ? "
                    f"WHERE id = ? AND price_{k} = ?",
                    (n["price"], n["ret"], n["correct"], n["ambiguous"], c["id"], o["price"]))
            if cur.rowcount != 1:
                raise RuntimeError(f"行 {c['id']} {k} 影响 {cur.rowcount} 行（≠1，可能被并发改过）——整体回滚")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if _integrity(db) != "ok":
        raise SystemExit(f"写后 integrity_check 不是 ok——从 {db_backup} 恢复")

    attr = {"t1": "actual_price_t1", "t7": "actual_price_t7", "t30": "actual_price_t30"}
    for c in snap_changes:
        path = os.path.join(snap_dir, c["file"])
        snap = ReportSnapshot.load_from_json(path)
        cur = getattr(snap, attr[c["period"]])
        if not _same(cur, c["old"]):
            raise RuntimeError(f"{c['file']} {c['period']} 当前值 {cur} ≠ 计划时的 {c['old']}（被并发改过？）——快照备份在 {snap_backup}")
        setattr(snap, attr[c["period"]], c["new"])
        snap.save_to_json(snap_dir)
    return {"db_backup": str(db_backup), "snapshot_backup": str(snap_backup)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="备份后写入（缺省 dry-run）")
    ap.add_argument("--db", default=None)
    ap.add_argument("--snapshots", default=None)
    a = ap.parse_args(argv)
    db = Path(a.db) if a.db else _db_path()
    snap_dir = a.snapshots or _snapshots_dir()

    from data_backup.sqlite_readonly import read_only_connect
    ro = read_only_connect(db)
    prices = Prices(ro)
    db_changes, st_db = plan_db(ro, prices)
    snap_changes, st_snap = plan_snapshots(snap_dir, prices)
    st_untouched = untouched_counts(ro)
    ro.close()

    report = {"version": VERSION, "db": str(db), "snapshots": snap_dir, "mode": "apply" if a.apply else "dry-run",
              "db_stats": dict(st_db), "snapshot_stats": dict(st_snap), "untouched": dict(st_untouched),
              "db_changes": db_changes, "snapshot_changes": snap_changes}
    for title, st in (("predictions", st_db), ("report_snapshots", st_snap), ("未在本工具范围", st_untouched)):
        print(f"== {title}")
        for k, v in sorted(st.items()):
            print(f"   {k}: {v}")
    if not a.apply:
        print("\n(dry-run：未写任何东西；加 --apply 备份后写入)")
        return 0

    backup_root = db.parent / "_manual_backups"
    report.update(apply(db, snap_dir, db_changes, snap_changes, backup_root))
    out = Path(report["snapshot_backup"]) / f"nyse_relabel_{VERSION}_audit.json"
    from hive_logger import atomic_json_write
    atomic_json_write(str(out), report, indent=2, ensure_ascii=False)
    print(f"\n已写入：predictions {len(db_changes)} 处、快照 {len(snap_changes)} 处\n"
          f"库备份 {report['db_backup']}\n快照备份 + 审计 {report['snapshot_backup']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
