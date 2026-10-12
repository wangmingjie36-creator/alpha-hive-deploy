#!/usr/bin/env python3
"""把 `signal_archive` 里命中 `signal_archive.QUARANTINE` 的行删掉（v0.45.472；做法同 v0.45.266 那次）。

隔离名单只在**入库口**拦截（`_rows_for`），库里已有的行要靠这一步清掉——否则分析照读。

    python3 experiments/purge_quarantined_signal_rows.py --expect 382            # dry-run：只数
    python3 experiments/purge_quarantined_signal_rows.py --expect 382 --apply --backup <file.db>

安全：`--apply` 先用 sqlite 在线备份 API 整库备份；再在单个 `BEGIN IMMEDIATE` 事务里**现读现判**
（`is_quarantined()`，不依赖任何早先的快照结果），命中数必须恰好等于 `--expect` 才删，删后核对这些 id
零残留、`integrity_check` 为 ok，全部通过才 COMMIT。
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
    a = ap.parse_args()
    import signal_archive as sa
    from hive_logger import PATHS
    db = Path(a.db) if a.db else Path(PATHS.db)

    def hits(conn):
        return [i for i, d, t, s in conn.execute(f"SELECT id, date, ticker, signal FROM {sa.TABLE}")
                if sa.is_quarantined(d, t, s)]

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as ro:
        n = len(hits(ro))
    print(f"{db}: 命中隔离名单 {n} 行（预期 {a.expect}）")
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
    dst = sqlite3.connect(bk)
    src.backup(dst)
    dst.close()
    assert sqlite3.connect(bk).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    src.isolation_level = None
    src.execute("BEGIN IMMEDIATE")
    try:
        ids = hits(src)
        if len(ids) != a.expect:
            raise RuntimeError(f"事务内命中 {len(ids)} ≠ 预期 {a.expect}")
        before = src.execute(f"SELECT COUNT(*) FROM {sa.TABLE}").fetchone()[0]
        src.executemany(f"DELETE FROM {sa.TABLE} WHERE id=?", [(i,) for i in ids])
        after = src.execute(f"SELECT COUNT(*) FROM {sa.TABLE}").fetchone()[0]
        if before - after != len(ids) or hits(src):
            raise RuntimeError(f"删除核对失败：{before}→{after}，目标 {len(ids)}")
        src.execute("COMMIT")
    except BaseException:
        src.execute("ROLLBACK")
        raise
    assert src.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    print(f"已删 {len(ids)} 行（{before} → {after}）；备份 {bk}")


if __name__ == "__main__":
    main()
