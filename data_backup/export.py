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
  db_snapshots/ sell_strike_state/`
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
    # v0.45.342：与 main 对齐。阶段 5 前这些由代码仓库 git 跟踪、每天随日报推上 main，main 兼任异地副本；
    # 阶段 5 后代码检出里那份冻结，新内容只写数据根 ⇒ 不进这里就零异地副本。
    # `report_snapshots/` 是 weekly_optimizer / self_analyst 的 T+7 样本，连 gh-pages 都没有它。
    "report_snapshots", "paper_portfolio_state_backup", "reports",
    # v0.45.333 卖权行权价前向账本（月度/周度 jsonl 分片 + 本地 md 报告）。攒数期数据、丢了无法回溯
    # 重取（当日的 CBOE 双边报价与 OI 事后拿不到），但用户要求**不上公开网站**：它被 .gitignore
    # 忽略、也**不在** report_deployer.REPORT_ARTIFACT_PATHS 里（那是自动提交进代码仓库）——
    # 耐久性只靠这里的私有备份。
    "sell_strike_state",
]
ROOT_FILE_GLOBS: list[str] = [
    "weight_history.jsonl", "pheromone_fallback.jsonl",
    "ml_model.json", "ml_model_cache.json", "ml_model_extended.json",
    # v0.45.342：与 main 对齐（理由同上）——日报 / ML 报告 / 站点文件 / 参数优化产物。
    "alpha-hive-daily-*.json", "alpha-hive-daily-*.md", "alpha-hive-thread-*.txt",
    "alpha-hive-*-ml-enhanced-*.html", "deep-*.html", "*_raw.json",
    "index.html", "dashboard-data.json", "manifest.json", "sw.js", "rss.xml",
    "paper_portfolio_card.html", "param_optimization_results.json", "param_optimization_report.html",
    "watchlist_override.yaml", "watchlist_override.json",
]
#: 数据根里有、但**刻意不进**数据仓库的项，键 = `migrate_data_root` 的 MOVE 规则原文，值 = 理由。
#: 不变式（`tests/test_data_backup.py::TestExportScopeCoversMoveRules`）：MOVE 规则的每一项
#: 要么被 DBS / STATE_DIRS / ROOT_FILE_GLOBS 覆盖，要么在这里写明理由——新增数据产物时
#: 两份表不许各自漂移（v0.45.342 前正是漂移：「已被代码仓库跟踪」这条排除理由被阶段 5 悄悄作废）。
EXCLUDED_FROM_THIS_PASS: dict[str, str] = {
    "chroma_db": "向量索引，可从 agent_memory 重建；BLOB 转十六进制文本会显著放大体积",
    ".swarm_results_*.json": "体量大（~70 MB），不可重取，是否进数据仓库**用户未定**（需先定压缩方案）",
    "analysis-*-ml-*.json": "体量大（~100 MB），不可重取，是否进数据仓库**用户未定**（需先定压缩方案）",
    "logs": "日志；被跟踪的只有 4~5 月旧健康快照，历史已在 main",
    "db_backups": "本机每日轮转备份；数据仓库本身就是它的异地版本",
    "realtime_metrics.json": "每轮扫描覆盖的中间产物",
    "cache": "可重建缓存", "data_cache": "可重建缓存", "earnings_cache": "可重建缓存",
    "finviz_cache": "可重建缓存", "sec_cache": "可重建缓存", "reddit_cache": "可重建缓存",
    ".factor_cache": "可重建缓存（Ken French 因子 parquet，二进制）", ".risk_cache": "可重建缓存（beta）",
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


_STATE_DIR_TEXT_SUFFIXES = (".json", ".jsonl", ".md", ".txt", ".yaml", ".yml")


def copy_state_dir(src_root: Path, rel_dir: str, out_dir: Path) -> tuple[list[dict], list[dict]]:
    """把 `rel_dir` 下的文本状态文件原样拷进导出产物。

    返回 `(copied, skipped)`：
    - `copied`：实际拷贝的文件记录 `{"rel", "sha256", "size"}`（原有形状不变——
      `write_manifest_and_sums` 靠它生成 `SHA256SUMS`，`restore.py` 不读这份
      逐文件记录，只按目录名重新遍历导出产物）。
    - `skipped`（v0.45.307 补，二次检查发现）：因后缀不在文本白名单里被跳过
      的文件 `{"rel", "size"}`。旧代码这里完全静默——今天只漏了 `.gitkeep`
      和一份旧 `.db` 快照，影响近零，但以后新增 `.csv`/`.pkl` 之类文件会在
      MANIFEST 里连痕迹都没有，没人能从产物本身看出"这个文件本该被备份却
      没有"（同项目 CLAUDE.md「这个失败，下游怎么知道？」）。只记相对路径+
      大小，不读取/不拷贝内容——本来就该被排除在导出范围外，"记录"不等于
      "纳入产物"。
    """
    src = src_root / rel_dir
    copied: list[dict] = []
    skipped: list[dict] = []
    if not src.is_dir():
        return copied, skipped
    dst_dir = out_dir / rel_dir
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root)
        if p.suffix not in _STATE_DIR_TEXT_SUFFIXES:
            skipped.append({"rel": str(rel), "size": p.stat().st_size})
            continue
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        copied.append({"rel": str(rel), "sha256": sha256_file(dst), "size": dst.stat().st_size})
    return copied, skipped


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


def _code_git_head(code_repo: Path | None) -> str:
    """产出这份数据的**代码**版本（`git rev-parse HEAD`），拿不到时返回 `unavailable: <原因>`。

    数据根迁移阶段 5 前（v0.45.322）这里是 `git -C <src_root>`：那时数据根就是代码仓库根，
    碰巧能用。迁移后 `src_root` 是 `~/alpha-hive-data`（没有 `.git`），git 退出码 128、
    stdout 为空，而旧写法只读 stdout、不看退出码 ⇒ `source_git_head` **静默变成空串**，
    不抛异常、也不进 except。现在：仓库位置走 `PATHS.git_repo_root`（阶段 4 与数据根拆开的那个），
    且非零退出码一律写成 `unavailable: ...`——空串永远不再出现在清单里。
    """
    try:
        if code_repo is None:
            # 延迟 import 也放进 try：从仓库根以外起跑（sys.path 里没有 hive_logger）时，
            # 一个只作记录的字段不许把整轮导出搞崩——落成 `unavailable:`（v0.45.335）。
            from hive_logger import PATHS
            code_repo = PATHS.git_repo_root
        r = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(code_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except Exception as e:  # noqa: BLE001 —— 只是记录用，git 不可用不该挡导出
        return f"unavailable: {e}"
    head = r.stdout.strip()
    if r.returncode != 0 or not head:
        return f"unavailable: git rc={r.returncode} in {code_repo}: {r.stderr.strip()[:200]}"
    return head


def run_export(src_root: Path, out_dir: Path, code_repo: Path | None = None) -> dict:
    """跑一整轮导出，返回 manifest dict（调用方负责写文件/扫描/提交）。

    `code_repo`：记录 `source_git_head` 用的代码仓库；默认 `PATHS.git_repo_root`（见 `_code_git_head`）。
    """
    src_root = Path(src_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "db_exports").mkdir(exist_ok=True)

    t0 = dt.datetime.now()
    manifest: dict = {
        "created_at": t0.isoformat(timespec="seconds"),
        "source_root": str(src_root),
        "excluded_from_this_pass": EXCLUDED_FROM_THIS_PASS,
        "databases": {}, "state_dirs": {}, "state_dirs_skipped": {}, "root_files": [],
    }
    manifest["source_git_head"] = _code_git_head(code_repo)

    for db_key, rel_path in DBS.items():
        info = export_db(src_root, db_key, rel_path, out_dir / "db_exports")
        manifest["databases"][db_key] = info
        print(f"DB  {db_key} ({rel_path}): {info['open_strategy']}, "
              f"{len(info['tables'])} 表, {sum(info['row_counts'].values())} 行")

    for d in STATE_DIRS:
        recs, skipped = copy_state_dir(src_root, d, out_dir)
        manifest["state_dirs"][d] = recs
        manifest["state_dirs_skipped"][d] = skipped
        skip_note = f"（跳过 {len(skipped)} 个非文本文件，见 MANIFEST.json state_dirs_skipped.{d}）" if skipped else ""
        print(f"DIR {d}: {len(recs)} 文件{skip_note}")

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
