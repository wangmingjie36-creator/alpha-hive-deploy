"""阶段 3.4 —— 恢复演练：从导出产物重建 SQLite 库 + 还原状态文件。

用在"异地恢复"场景：从 `git clone` 出来的 `_git_backup` 工作区（或它的任何
副本）出发，不依赖生产仓库、不依赖 `~/alpha-hive-data` 本机内容，重建一份
可用的 `pheromone.db`（等）与状态目录树。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path


def restore_db(export_dir: Path, db_key: str, dest_path: Path) -> dict:
    """从 `export_dir/db_exports/<db_key>/*.sql` 重建一个新库到 `dest_path`。"""
    db_dir = Path(export_dir) / "db_exports" / db_key
    if not db_dir.is_dir():
        raise FileNotFoundError(f"找不到导出目录：{db_dir}")
    dest_path = Path(dest_path)
    if dest_path.exists():
        dest_path.unlink()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    table_files = sorted(
        p for p in db_dir.glob("*.sql")
        if p.name not in ("_schema_extra.sql", "sqlite_sequence.sql")
    )
    seq_fp = db_dir / "sqlite_sequence.sql"  # 必须最后执行——见 export.dump_table_sql 的说明
    conn = sqlite3.connect(dest_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")  # 顺序按表名字母序灌，不假设外键建表顺序
        for fp in table_files:
            conn.executescript(fp.read_text(encoding="utf-8"))
        if seq_fp.exists():
            conn.executescript(seq_fp.read_text(encoding="utf-8"))
        extra_fp = db_dir / "_schema_extra.sql"
        if extra_fp.exists() and extra_fp.read_text(encoding="utf-8").strip():
            conn.executescript(extra_fp.read_text(encoding="utf-8"))
        conn.commit()
        integrity = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        row_counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()

    return {
        "db_key": db_key, "dest_path": str(dest_path),
        "integrity_check": integrity, "tables": tables, "row_counts": row_counts,
        "rows_total": sum(row_counts.values()),
    }


def restore_state(export_dir: Path, dest_root: Path, state_dirs: list[str], root_files: list[dict]) -> list[dict]:
    """把导出产物里的状态目录/根文件拷回 `dest_root`（保持相对路径）。"""
    export_dir = Path(export_dir)
    dest_root = Path(dest_root)
    records = []
    for d in state_dirs:
        src_dir = export_dir / d
        if not src_dir.is_dir():
            continue
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(export_dir)
                dst = dest_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                records.append({"rel": str(rel)})
    for r in root_files:
        rel = Path(r["rel"])
        src = export_dir / rel
        if src.exists():
            dst = dest_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            records.append({"rel": str(rel)})
    return records


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="阶段 3.4 恢复演练：从导出产物重建库 + 还原状态文件")
    ap.add_argument("--export-dir", required=True, help="导出产物目录（如 git clone 出来的 _git_backup）")
    ap.add_argument("--dest-root", required=True, help="恢复目标根目录")
    ap.add_argument("--dbs", nargs="*", default=["pheromone", "metrics", "sentiment_baseline", "hive_predictions"])
    ap.add_argument("--skip-state", action="store_true", help="只重建库，不还原状态目录/根文件")
    args = ap.parse_args(argv)

    export_dir = Path(args.export_dir)
    dest_root = Path(args.dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    results = {}
    for db_key in args.dbs:
        dest = dest_root / f"{db_key}.db" if db_key != "pheromone" else dest_root / "pheromone.db"
        # 非 pheromone 库沿用其原始文件名（从 _meta.json 读，避免硬编码第二份映射表）
        meta_fp = export_dir / "db_exports" / db_key / "_meta.json"
        if meta_fp.exists():
            orig_rel = json.loads(meta_fp.read_text(encoding="utf-8"))["rel_path"]
            dest = dest_root / orig_rel
        info = restore_db(export_dir, db_key, dest)
        results[db_key] = info
        print(f"RESTORE {db_key} -> {dest}: integrity={info['integrity_check']}, "
              f"{len(info['tables'])} 表, {info['rows_total']} 行")

    if not args.skip_state:
        # 状态目录/根文件清单读 MANIFEST.json（导出那次实际记录了什么），
        # 不读 `export.py` 现在的 STATE_DIRS 常量——恢复的是"那次备份实际包含的东西"，
        # 不是"当前代码认为该包含的东西"，两者在代码演进后可能不同。
        manifest_fp = export_dir / "MANIFEST.json"
        if manifest_fp.exists():
            manifest = json.loads(manifest_fp.read_text(encoding="utf-8"))
            state_dirs = list(manifest.get("state_dirs", {}).keys())
            root_files = manifest.get("root_files", [])
            state_records = restore_state(export_dir, dest_root, state_dirs, root_files)
            results["_state_restored"] = {"file_count": len(state_records)}
            print(f"RESTORE 状态文件: {len(state_records)} 个 "
                  f"（{len(state_dirs)} 个目录 + {len(root_files)} 个根文件）")
        else:
            print("⚠️ 没有 MANIFEST.json，跳过状态文件还原（只重建了库）", file=sys.stderr)

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
