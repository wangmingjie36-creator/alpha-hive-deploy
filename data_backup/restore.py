"""阶段 3.4 —— 恢复演练：从导出产物重建 SQLite 库 + 还原状态文件。

用在"异地恢复"场景：从 `git clone` 出来的 `_git_backup` 工作区（或它的任何
副本）出发，不依赖生产仓库、不依赖 `~/alpha-hive-data` 本机内容，重建一份
可用的 `pheromone.db`（等）与状态目录树。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from .export import LEGACY_MANIFEST_NAME, MANIFEST_NAME
from .sqlite_readonly import sha256_file


def find_manifest(export_dir: Path) -> Path | None:
    """导出产物的元数据清单：`BACKUP_MANIFEST.json`；v0.45.430 前的提交里叫 `MANIFEST.json`。都没有 ⇒ None。

    旧名只按**精确拼写**认（`os.listdir`，不用 `.exists()`）：大小写不敏感的盘上 `export_dir / "MANIFEST.json"`
    也能打开网站的 `manifest.json`，新布局的产物缺了新清单时会把 PWA manifest 当成备份清单读。
    按名字认出来的文件读不了、或不是备份清单（没有 `root_files`）⇒ 抛 `ValueError`，**不往下找**
    （二次检查补）：新清单坏了却退回旧名，会拿一份陈旧清单冒充这次备份的内容；旧代码遇到坏清单是直接崩的。
    """
    export_dir = Path(export_dir)
    names = os.listdir(export_dir)
    for name in (MANIFEST_NAME, LEGACY_MANIFEST_NAME):
        if name not in names:
            continue
        fp = export_dir / name
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"{fp} 读不了或不是合法 JSON：{e}") from e
        if not (isinstance(data, dict) and "root_files" in data):
            raise ValueError(f"{fp} 不是备份清单（没有 root_files）")
        return fp
    return None


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


def restore_state(export_dir: Path, dest_root: Path, state_dirs: list[str], root_files: list[dict]) -> dict:
    """把导出产物里的状态目录/根文件拷回 `dest_root`（保持相对路径）。

    返回 `{"restored": [...], "mismatched": [...], "missing": [...]}`。根文件按清单里记的 `sha256` 先核对
    再拷（v0.45.430）：v0.45.430 前的提交里网站 `manifest.json` 被元数据 `MANIFEST.json` 覆盖，大小写不敏感的
    盘上按 `manifest.json` 打开的是那份元数据——旧代码照拷不误，把备份清单「恢复」成了网站 manifest；
    大小写敏感的盘上它干脆不存在，旧代码 `if src.exists()` 静默跳过。现在两种都进返回值、不拷。
    """
    export_dir = Path(export_dir)
    dest_root = Path(dest_root)
    records = []
    mismatched: list[dict] = []
    missing: list[dict] = []
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
        if not src.is_file():
            missing.append({"rel": str(rel)})
            continue
        if "sha256" in r:
            got = sha256_file(src)
            if got != r["sha256"]:
                mismatched.append({"rel": str(rel), "expected": r["sha256"], "got": got})
                continue
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        records.append({"rel": str(rel)})
    return {"restored": records, "mismatched": mismatched, "missing": missing}


def main(argv=None) -> int:
    """退出码：0 = 全部还原；1 = 有根文件对不上清单哈希或缺失，或要还原状态却拿不到可用的清单
    （v0.45.430，此前恒 0）。"""
    import argparse
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

    rc = 0
    if not args.skip_state:
        # 状态目录/根文件清单读元数据清单（导出那次实际记录了什么），
        # 不读 `export.py` 现在的 STATE_DIRS 常量——恢复的是"那次备份实际包含的东西"，
        # 不是"当前代码认为该包含的东西"，两者在代码演进后可能不同。
        try:
            manifest_fp, bad_manifest = find_manifest(export_dir), None
        except ValueError as e:
            manifest_fp, bad_manifest = None, str(e)
        if manifest_fp is not None:
            manifest = json.loads(manifest_fp.read_text(encoding="utf-8"))
            state_dirs = list(manifest.get("state_dirs", {}).keys())
            root_files = manifest.get("root_files", [])
            got = restore_state(export_dir, dest_root, state_dirs, root_files)
            results["_state_restored"] = {
                "manifest": manifest_fp.name, "file_count": len(got["restored"]),
                "mismatched": got["mismatched"], "missing": got["missing"],
            }
            print(f"RESTORE 状态文件: {len(got['restored'])} 个 "
                  f"（{len(state_dirs)} 个目录 + {len(root_files)} 个根文件；清单 {manifest_fp.name}）")
            for kind, rows in (("内容与清单哈希不符", got["mismatched"]), ("产物里不存在", got["missing"])):
                if rows:
                    rc = 1
                    print(f"⚠️ {len(rows)} 个根文件{kind}，未还原：{[r['rel'] for r in rows]}", file=sys.stderr)
            if any(r["rel"] == "manifest.json" for r in got["mismatched"] + got["missing"]) \
                    and manifest_fp.name == LEGACY_MANIFEST_NAME:
                print("   ↳ manifest.json：v0.45.430 前的备份里它被元数据 MANIFEST.json 覆盖（APFS 大小写不敏感），"
                      "这批备份从未含有网站 manifest。它由 report_web_assets 每轮重新生成，gh-pages 上也有一份。",
                      file=sys.stderr)
        else:
            rc = 1   # 要还原状态却拿不到清单 = 没还原，不是成功（v0.45.430 二次检查；此前 rc=0）
            if bad_manifest:
                print(f"🚨 清单不可用，不退回其他清单，状态文件未还原：{bad_manifest}", file=sys.stderr)
            else:
                print(f"⚠️ 没有 {MANIFEST_NAME}（或旧名 {LEGACY_MANIFEST_NAME}），跳过状态文件还原（只重建了库）",
                      file=sys.stderr)

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
