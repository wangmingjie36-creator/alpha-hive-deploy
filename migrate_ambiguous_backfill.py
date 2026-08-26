#!/usr/bin/env python3
"""
P0 迁移脚本 (v0.45.9, 2026-08-25) —— 容差语义修正 + 全表回填

做两件事：
  1. 给 predictions 表加 ambiguous_t1 / ambiguous_t7 / ambiguous_t30 列
  2. 用修正后的 outcome_utils.determine_outcome_triplet 重算全部已验证行的
     correct_{period} 与 ambiguous_{period}（依据存量 return_{period}，不联网）

旧语义（单边亏损豁免）：看多 correct if return > -1.0 → 亏 0.9% 记为判对
新语义（双边模糊带）  ：|return| <= 1.0 → ambiguous，剔除出统计

用法：
    /usr/local/bin/python3 migrate_ambiguous_backfill.py --dry-run   # 只看影响
    /usr/local/bin/python3 migrate_ambiguous_backfill.py             # 实际写入

⚠️ 写入前请先备份 pheromone.db（脚本会自动备份到 db_backups/）。
"""

import os
import sys
import shutil
import sqlite3
import argparse
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from outcome_utils import determine_outcome_triplet  # noqa: E402

PERIODS = ("t1", "t7", "t30")
TABLE = "predictions"


def _resolve_db(explicit=None):
    if explicit:
        return explicit
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "pheromone.db")
    if os.path.exists(cand):
        return cand
    raise FileNotFoundError("找不到 pheromone.db，请用 --db 指定")


def ensure_columns(conn):
    """幂等地补齐 ambiguous_* 列。"""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % TABLE)}
    added = []
    for p in PERIODS:
        col = "ambiguous_%s" % p
        if col not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s INTEGER DEFAULT 0" % (TABLE, col))
            added.append(col)
    return added


def backfill(conn, dry_run=False):
    """按存量 return_* 重算 correct_* / ambiguous_*。返回逐口径统计。"""
    stats = {}
    for p in PERIODS:
        # 幽灵行：checked=1 但 return 为 NULL（回测时取价失败），旧逻辑落成
        # correct=0，等于往分母里塞必错样本。无数据不可评分 → 标 ambiguous。
        ghost = conn.execute(
            "SELECT COUNT(*) FROM {t} WHERE checked_{p} = 1 AND return_{p} IS NULL".format(t=TABLE, p=p)
        ).fetchone()[0]
        if ghost and not dry_run:
            conn.execute(
                "UPDATE {t} SET ambiguous_{p} = 1 WHERE checked_{p} = 1 "
                "AND return_{p} IS NULL".format(t=TABLE, p=p)
            )

        rows = conn.execute(
            "SELECT id, direction, return_{p}, correct_{p} FROM {t} "
            "WHERE checked_{p} = 1 AND return_{p} IS NOT NULL".format(p=p, t=TABLE)
        ).fetchall()

        flipped, marked_amb, unchanged = 0, 0, 0
        old_correct = new_correct = 0
        updates = []

        for pid, direction, ret, old_c in rows:
            correct, ambiguous = determine_outcome_triplet(direction or "", ret)
            old_correct += 1 if old_c else 0
            if ambiguous:
                marked_amb += 1
            else:
                new_correct += 1 if correct else 0
            if bool(old_c) != bool(correct) or ambiguous:
                if bool(old_c) != bool(correct):
                    flipped += 1
            else:
                unchanged += 1
            updates.append((1 if correct else 0, 1 if ambiguous else 0, pid))

        if not dry_run and updates:
            conn.executemany(
                "UPDATE {t} SET correct_{p} = ?, ambiguous_{p} = ? WHERE id = ?".format(t=TABLE, p=p),
                updates,
            )

        # 方向样本（排除 neutral 方向）下的准确率对照
        dir_rows = [(d, r, c) for _, d, r, c in rows
                    if (d or "").lower() in ("bullish", "bearish", "long", "short")]
        old_acc = (sum(1 for _, _, c in dir_rows if c) / len(dir_rows) * 100) if dir_rows else 0
        scored = [(d, r) for d, r, _ in dir_rows
                  if not determine_outcome_triplet(d, r)[1]]
        new_acc = (sum(1 for d, r in scored if determine_outcome_triplet(d, r)[0])
                   / len(scored) * 100) if scored else 0

        stats[p] = {
            "rows": len(rows),
            "dir_rows": len(dir_rows),
            "ambiguous": marked_amb,
            "flipped": flipped,
            "old_acc_dir": round(old_acc, 1),
            "new_acc_dir": round(new_acc, 1),
            "new_n_dir": len(scored),
        }
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    db = _resolve_db(args.db)
    print("DB: %s" % db)

    if not args.dry_run and not args.no_backup:
        bdir = os.path.join(os.path.dirname(db), "db_backups")
        os.makedirs(bdir, exist_ok=True)
        dst = os.path.join(bdir, "pheromone_pre_P0_tolerance_fix_%s.db" % date.today())
        if not os.path.exists(dst):
            shutil.copy2(db, dst)
            print("已备份 → %s" % dst)
        else:
            print("备份已存在 → %s" % dst)

    conn = sqlite3.connect(db)
    try:
        added = ensure_columns(conn) if not args.dry_run else []
        if added:
            print("新增列: %s" % ", ".join(added))
        elif not args.dry_run:
            print("ambiguous_* 列已存在，跳过 ALTER")

        stats = backfill(conn, dry_run=args.dry_run)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    print("\n%-6s %8s %8s %9s %9s  %-22s" %
          ("口径", "已验证", "方向样本", "模糊剔除", "判定翻转", "方向准确率 旧→新"))
    for p in PERIODS:
        s = stats[p]
        print("%-6s %8d %8d %9d %9d  %5.1f%% → %5.1f%% (n=%d)" % (
            p.upper(), s["rows"], s["dir_rows"], s["ambiguous"], s["flipped"],
            s["old_acc_dir"], s["new_acc_dir"], s["new_n_dir"]))

    print("\n%s" % ("[DRY RUN] 未写入任何数据" if args.dry_run else "✅ 回填完成"))


if __name__ == "__main__":
    main()
