#!/usr/bin/env python3
"""把 `signal_archive` 里命中 `signal_archive.QUARANTINE` 的行删掉（v0.45.472；做法同 v0.45.266 那次）。

隔离名单只在**入库口**拦截（`_rows_for`），库里已有的行要靠这一步清掉——否则分析照读。

    python3 experiments/purge_quarantined_signal_rows.py --expect 382            # dry-run：只数
    python3 experiments/purge_quarantined_signal_rows.py --expect 382 --apply --backup <file.db>
    python3 experiments/purge_quarantined_signal_rows.py --only-tag v0.45.478 --expect 6   # 只清某一批条目

`--only-tag`（v0.45.478）：只删「命中的隔离条目 reason 里含该标记」的行。库里可能还挂着别的批次
命中但没清的行（别的会话负责、未获批准由本次清），不加这个过滤 `--expect` 就对不上、只能停下。

安全：`--apply` 先用 sqlite 在线备份 API 整库备份；再在单个 `BEGIN IMMEDIATE` 事务里**现读现判**
（`is_quarantined()`，不依赖任何早先的快照结果），命中数必须恰好等于 `--expect` 才删，删后核对这些 id
零残留、`integrity_check` 为 ok，全部通过才 COMMIT（v0.45.479：备份改在持写锁之后、integrity_check 改在 COMMIT 之前且用 raise）。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="缺省 PATHS.db")
    ap.add_argument("--expect", type=int, required=True, help="预期命中行数（不等就中止）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default=None)
    ap.add_argument("--only-tag", default=None,
                    help="只删命中「reason 含该标记」的隔离条目的行（如 v0.45.478）；缺省 = 全部隔离行")
    a = ap.parse_args()
    import signal_archive as sa
    from hive_logger import PATHS
    db = Path(a.db) if a.db else Path(PATHS.db)

    def _tagged(d, t, s):
        # 与 sa.is_quarantined 同一判据，只是限定在 reason 带标记的条目上（现读 QUARANTINE，不缓存）
        return any(q["date"] == d and s in q["signals"]
                   and (q.get("tickers") is None or t in q["tickers"])
                   and a.only_tag in q.get("reason", "")
                   for q in sa.QUARANTINE)

    def hits(conn):
        return [i for i, d, t, s in conn.execute(f"SELECT id, date, ticker, signal FROM {sa.TABLE}")
                if sa.is_quarantined(d, t, s) and (a.only_tag is None or _tagged(d, t, s))]

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as ro:
        n = len(hits(ro))
    print(f"{db}: 命中隔离名单 {n} 行（预期 {a.expect}）" + (f"，限标记 {a.only_tag}" if a.only_tag else ""))
    if n != a.expect:
        raise SystemExit("命中数与预期不符——不删，停下")
    if not a.apply:
        print("dry-run：未删任何行")
        return
    if not a.backup:
        raise SystemExit("--apply 必须给 --backup")
    bk = Path(a.backup)
    if bk.exists():
        raise SystemExit(f"{bk} 已存在，不覆盖")
    src = sqlite3.connect(db)
    src.isolation_level = None
    src.execute("BEGIN IMMEDIATE")
    try:
        # v0.45.479：持写锁**之后**再备份——此刻起别人提交不了，备份就是即将被改的那个状态
        # （v0.45.472 版先备份后加锁，中间若有并发写，备份里就少了它）。备份走另一条只读连接。
        rd = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(bk)
        rd.backup(dst)
        rd.close()
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份库 integrity_check 未通过")
        dst.close()
        ids = hits(src)
        if len(ids) != a.expect:
            raise RuntimeError(f"事务内命中 {len(ids)} ≠ 预期 {a.expect}")
        before = src.execute(f"SELECT COUNT(*) FROM {sa.TABLE}").fetchone()[0]
        src.executemany(f"DELETE FROM {sa.TABLE} WHERE id=?", [(i,) for i in ids])
        after = src.execute(f"SELECT COUNT(*) FROM {sa.TABLE}").fetchone()[0]
        if before - after != len(ids) or hits(src):
            raise RuntimeError(f"删除核对失败：{before}→{after}，目标 {len(ids)}")
        # v0.45.479：integrity_check 在 COMMIT **之前**、用 raise（不是 -O 下会被剥掉的 assert）
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity_check 未通过")
        src.execute("COMMIT")
    except BaseException:
        src.execute("ROLLBACK")
        raise
    print(f"已删 {len(ids)} 行（{before} → {after}）；备份 {bk}")


if __name__ == "__main__":
    main()
