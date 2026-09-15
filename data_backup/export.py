"""阶段 3.2 —— 导出脚本：按表把 SQLite 库导成 SQL 文本 + 拷贝文本状态文件。

范围（本次阶段 3 落地演练的口径，非最终定论——阶段 1/2 的完整普查覆盖全仓
读写者，这里只覆盖「已知不可重取、有生产读者」的核心集合，见 CHANGELOG 里
对 `.swarm_results_*.json`（64 MB/105 个）与 `analysis-*-ml-*.json`
（90 MB/863 个）的排除说明——按 `*_state/` 目录原文不含它们，且体量会让
本地裸仓库演练失焦，留给下次迭代或用户确认是否要收）：

- DB（按表导出 SQL 文本，不提交二进制原库）：
  `pheromone.db`、`metrics.db`、`sentiment_baseline.db`、`hive_predictions.db`
  （均为「阶段 0 现量补充」点名的「不可重取、有生产读者」库；`chroma_db/
  chroma.sqlite3` 显式排除——语义向量索引、可从 `agent_memory` 重建，且 BLOB
  转十六进制文本会显著放大体积，不计入本次范围）
- 状态目录（原样拷贝 jsonl/json，保持仓库相对路径不变）：
  `hedge_state/ paper_portfolio_state/ options_paper_state/ vrp_state/
  probability_scorecard_state/ ml_model_history/ self_analysis_briefs/
  db_snapshots/`
- 根目录文本文件：`weight_history.jsonl pheromone_fallback.jsonl
  ml_model.json ml_model_cache.json ml_model_extended.json`

只读打开策略见 `sqlite_readonly.py`（照抄阶段 0 脚本，未重新发明）。
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from .sqlite_readonly import db_open_uri, sha256_file

# ── 导出范围清单（唯一真相在这里，CHANGELOG/memory 只引用不复制数字）──────
DBS: dict[str, str] = {
    "pheromone": "pheromone.db",
    "metrics": "metrics.db",
    "sentiment_baseline": "sentiment_baseline.db",
    "hive_predictions": "hive_predictions.db",
}
STATE_DIRS: list[str] = [
    "hedge_state", "paper_portfolio_state", "options_paper_state", "vrp_state",
    "probability_scorecard_state", "ml_model_history", "self_analysis_briefs",
    "db_snapshots",
]
ROOT_FILE_GLOBS: list[str] = [
    "weight_history.jsonl", "pheromone_fallback.jsonl",
    "ml_model.json", "ml_model_cache.json", "ml_model_extended.json",
]
EXCLUDED_FROM_THIS_PASS = {
    "chroma_db/chroma.sqlite3": "向量索引，可从 agent_memory 重建；BLOB 转十六进制文本会显著放大体积",
    ".swarm_results_*.json（105 个，64 MB）": "体量大、非 *_state/ 目录，本次范围排除，留待确认",
    "analysis-*-ml-*.json（863 个，90 MB）": "体量大、非 *_state/ 目录，本次范围排除，留待确认",
    "report_snapshots/ 与根目录已跟踪报告 html/json": "已被 alpha-hive-deploy 代码仓库 git 跟踪并推送",
}


def _quote_sql_literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):  # bool 是 int 子类，必须先判
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    s = str(v)
    return "'" + s.replace("'", "''") + "'"


def table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def dump_table_sql(conn: sqlite3.Connection, table: str) -> str:
    """把单张表导成自洽的 SQL 文本：`CREATE TABLE`（原样）+ 按行 `INSERT`。

    行顺序：优先 `ORDER BY rowid`（确定性、对 git diff 友好）；
    `WITHOUT ROWID` 表没有 rowid，退回不排序（数据仍完整，只是 diff 稳定性弱一点）。

    `sqlite_sequence` 是 SQLite 内部表（有 `AUTOINCREMENT` 列的表才会自动生成），
    **不能**手写 `CREATE TABLE`（引擎保留该名字，建表会报
    `object name reserved for internal use`）——它在对应表被建出来的那一刻
    就已经自动存在。照官方 `.dump` 的做法：只落 `DELETE` + `INSERT`，
    且调用方必须保证这份 SQL 在所有其他表都建完之后才执行（见 `restore.py`
    的执行顺序：`sqlite_sequence.sql` 固定排最后）。
    """
    if table == "sqlite_sequence":
        lines = ["-- table: sqlite_sequence (SQLite 内部表，随 AUTOINCREMENT 表自动生成，"
                 "必须在其余表都建完之后才执行)", "DELETE FROM sqlite_sequence;", ""]
        n = 0
        for name, seq in conn.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name"):
            lines.append(f'INSERT INTO sqlite_sequence (name, seq) VALUES '
                         f'({_quote_sql_literal(name)}, {_quote_sql_literal(seq)});')
            n += 1
        lines.append(f"-- rows: {n}")
        return "\n".join(lines) + "\n"

    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    col_list = ", ".join(f'"{c}"' for c in cols)
    lines = [f"-- table: {table}", create_sql.rstrip().rstrip(";") + ";", ""]
    try:
        cur = conn.execute(f'SELECT {col_list} FROM "{table}" ORDER BY rowid')
    except sqlite3.OperationalError:
        cur = conn.execute(f'SELECT {col_list} FROM "{table}"')
    n = 0
    for row in cur:
        vals = ", ".join(_quote_sql_literal(v) for v in row)
        lines.append(f'INSERT INTO "{table}" ({col_list}) VALUES ({vals});')
        n += 1
    lines.append(f"-- rows: {n}")
    return "\n".join(lines) + "\n"


def dump_extra_schema_sql(conn: sqlite3.Connection) -> str:
    """表以外的 schema 对象（索引/触发器/视图），表建完、灌完数据之后再建。"""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('index','trigger','view') AND sql IS NOT NULL "
        "ORDER BY type, name"
    ).fetchall()
    lines = []
    for typ, name, sql in rows:
        lines.append(f"-- {typ}: {name}")
        lines.append(sql.rstrip().rstrip(";") + ";")
    return "\n".join(lines) + ("\n" if lines else "")


def export_db(src_root: Path, db_key: str, rel_path: str, out_dir: Path) -> dict:
    src = src_root / rel_path
    if not src.exists():
        raise FileNotFoundError(f"库不存在：{src}")
    uri, how = db_open_uri(src)
    guard = [src, Path(str(src) + "-wal")]
    before = {p.name: sha256_file(p) for p in guard if p.exists()}

    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        conn.execute("BEGIN")  # 一个读事务：所有表看同一个快照
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts_before = table_row_counts(conn)

        db_out = out_dir / db_key
        db_out.mkdir(parents=True, exist_ok=True)
        for old in db_out.glob("*.sql"):  # 清掉上一轮遗留的表文件（比如表被删掉的情况）
            old.unlink()

        written = {}
        for t in tables:
            sql_text = dump_table_sql(conn, t)
            fp = db_out / f"{t}.sql"
            fp.write_text(sql_text, encoding="utf-8")
            written[t] = fp.stat().st_size

        extra = dump_extra_schema_sql(conn)
        (db_out / "_schema_extra.sql").write_text(extra, encoding="utf-8")

        counts_after = table_row_counts(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()

    after = {p.name: sha256_file(p) for p in guard if p.exists()}
    if counts_before != counts_after:
        raise RuntimeError(f"{rel_path} 导出期间行数变了（有并发写者？）：{counts_before} vs {counts_after}")
    if before != after:
        raise RuntimeError(f"{rel_path} 导出期间源文件变了（有并发写者？）")

    meta = {
        "db_key": db_key, "rel_path": rel_path, "open_strategy": how,
        "tables": tables, "row_counts": counts_after,
        "file_sizes": written, "src_size_bytes": src.stat().st_size,
        "src_unchanged": before == after,
    }
    (db_out / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def copy_state_dir(src_root: Path, rel_dir: str, out_dir: Path) -> list[dict]:
    src = src_root / rel_dir
    records = []
    if not src.is_dir():
        return records
    dst_dir = out_dir / rel_dir
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    for p in sorted(src.rglob("*")):
        if p.is_file():
            if p.suffix not in (".json", ".jsonl", ".md", ".txt", ".yaml", ".yml"):
                continue  # 只拷文本状态文件，非文本（如遗留的 .db）不在本范围
            rel = p.relative_to(src_root)
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            records.append({"rel": str(rel), "sha256": sha256_file(dst), "size": dst.stat().st_size})
    return records


def copy_root_files(src_root: Path, patterns: list[str], out_dir: Path) -> list[dict]:
    records = []
    for pat in patterns:
        for hit in sorted(glob.glob(str(src_root / pat))):
            p = Path(hit)
            rel = p.relative_to(src_root)
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            records.append({"rel": str(rel), "sha256": sha256_file(dst), "size": dst.stat().st_size})
    return records


def run_export(src_root: Path, out_dir: Path) -> dict:
    """跑一整轮导出，返回 manifest dict（调用方负责写文件/扫描/提交）。"""
    src_root = Path(src_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "db_exports").mkdir(exist_ok=True)

    t0 = dt.datetime.now()
    manifest: dict = {
        "created_at": t0.isoformat(timespec="seconds"),
        "source_root": str(src_root),
        "excluded_from_this_pass": EXCLUDED_FROM_THIS_PASS,
        "databases": {}, "state_dirs": {}, "root_files": [],
    }
    try:
        manifest["source_git_head"] = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(src_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:  # noqa: BLE001 —— 只是记录用，git 不可用不该挡导出
        manifest["source_git_head"] = f"unavailable: {e}"

    for db_key, rel_path in DBS.items():
        info = export_db(src_root, db_key, rel_path, out_dir / "db_exports")
        manifest["databases"][db_key] = info
        print(f"DB  {db_key} ({rel_path}): {info['open_strategy']}, "
              f"{len(info['tables'])} 表, {sum(info['row_counts'].values())} 行")

    for d in STATE_DIRS:
        recs = copy_state_dir(src_root, d, out_dir)
        manifest["state_dirs"][d] = recs
        print(f"DIR {d}: {len(recs)} 文件")

    manifest["root_files"] = copy_root_files(src_root, ROOT_FILE_GLOBS, out_dir)
    print(f"根文件: {len(manifest['root_files'])} 个")

    manifest["duration_seconds"] = round((dt.datetime.now() - t0).total_seconds(), 1)
    return manifest


def write_manifest_and_sums(out_dir: Path, manifest: dict) -> None:
    out_dir = Path(out_dir)
    sums = []
    for db_key, info in manifest["databases"].items():
        db_dir = out_dir / "db_exports" / db_key
        for fp in sorted(db_dir.glob("*")):
            if fp.is_file():
                sums.append(f"{sha256_file(fp)}  {fp.relative_to(out_dir)}")
    for _d, recs in manifest["state_dirs"].items():
        for r in recs:
            sums.append(f"{r['sha256']}  {r['rel']}")
    for r in manifest["root_files"]:
        sums.append(f"{r['sha256']}  {r['rel']}")
    (out_dir / "SHA256SUMS").write_text("\n".join(sorted(sums)) + "\n", encoding="utf-8")
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="阶段 3.2 数据导出（按表 SQL + 文本状态）")
    ap.add_argument("--src", default="/Users/igg/Desktop/Alpha Hive")
    ap.add_argument("--out", default=str(Path.home() / "alpha-hive-data" / "_git_backup"))
    args = ap.parse_args(argv)
    manifest = run_export(Path(args.src), Path(args.out))
    write_manifest_and_sums(Path(args.out), manifest)
    print(f"\n导出完成：{args.out}，耗时 {manifest['duration_seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
