#!/usr/bin/env python3
"""把误删的 `signal_archive` 行从备份库原样放回（v0.45.476；`purge_quarantined_signal_rows.py` 的逆操作）。

    python3 experiments/restore_signal_rows_from_backup.py --from <备份.db> --pair 2026-08-12:VKTX --pair ... --expect 25
    （加 --apply --backup <file.db> 才写）

规则：只放回**现在不在隔离名单里**的 (日期, 标的, 信号)——还在名单里就停下（先改名单再恢复）；保留原 id 与 created_at；
目标库已有同 id 或同 (date, ticker, signal) 行就停下，不覆盖；单个 `BEGIN IMMEDIATE` 事务，行数恰为预期才 COMMIT；写前整库在线备份。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True, help="备份库（只读）")
    ap.add_argument("--db", default=None, help="缺省 PATHS.db")
    ap.add_argument("--pair", action="append", required=True, help="日期:标的，可多次")
    ap.add_argument("--expect", type=int, required=True)
    ap.add_argument("--skip-quarantined", action="store_true",
                    help="仍在隔离名单里的行跳过（部分撤回时用，如只放回同一对的其余信号）；缺省遇到就停")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default=None)
    a = ap.parse_args()
    import signal_archive as sa
    from hive_logger import PATHS
    db = Path(a.db) if a.db else Path(PATHS.db)
    pairs = [tuple(x.split(":", 1)) for x in a.pair]
    sigs = sorted({s for q in sa.QUARANTINE for s in q["signals"]})
    with sqlite3.connect(f"file:{a.src}?mode=ro", uri=True) as b:
        rows = [r for d, t in pairs for r in b.execute(
            f"SELECT id, date, ticker, signal, value, created_at FROM {sa.TABLE} "
            f"WHERE date=? AND ticker=? AND signal IN ({','.join('?' * len(sigs))})", (d, t, *sigs))]
    still = [r for r in rows if sa.is_quarantined(r[1], r[2], r[3])]
    if still and a.skip_quarantined:
        print(f"跳过仍在隔离名单里的 {len(still)} 行")
        rows = [r for r in rows if r not in still]
        still = []
    if still:
        raise SystemExit(f"这些行仍在隔离名单里，先改名单：{still[:5]}")
    print(f"备份里找到 {len(rows)} 行（预期 {a.expect}）")
    if len(rows) != a.expect:
        raise SystemExit("行数与预期不符——停下")
    if not a.apply:
        print("dry-run：未写")
        return
    if not a.backup or Path(a.backup).exists():
        raise SystemExit("--apply 必须给一个还不存在的 --backup")
    con = sqlite3.connect(db)
    con.isolation_level = None
    con.execute("BEGIN IMMEDIATE")
    try:
        # 持写锁之后再备份（同 purge 脚本 v0.45.479）
        rd = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(a.backup)
        rd.backup(dst)
        rd.close()
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份库 integrity_check 未通过")
        dst.close()
        for r in rows:
            if con.execute(f"SELECT 1 FROM {sa.TABLE} WHERE id=? OR (date=? AND ticker=? AND signal=?)",
                           (r[0], r[1], r[2], r[3])).fetchone():
                raise RuntimeError(f"目标库已有 {r[:4]}，不覆盖")
            con.execute(f"INSERT INTO {sa.TABLE} (id, date, ticker, signal, value, created_at) VALUES (?,?,?,?,?,?)", r)
        n = sum(1 for r in rows if con.execute(f"SELECT value FROM {sa.TABLE} WHERE id=?", (r[0],)).fetchone() == (r[4],))
        if n != len(rows):
            raise RuntimeError(f"写后核对 {n} ≠ {len(rows)}")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity_check 未通过")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    print(f"已放回 {len(rows)} 行；写前备份 {a.backup}")


if __name__ == "__main__":
    main()
