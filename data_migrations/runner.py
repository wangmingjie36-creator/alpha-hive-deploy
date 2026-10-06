"""数据迁移运行器（阶段 7）。

契约
----
- 迁移 = `data_migrations/versions/NNNN_说明.py`（NNNN 四位、从 0001 连续、不许重号 / 断号）。每个文件导出：
    `DESCRIPTION: str`                     一句话说它改什么
    `apply(ctx) -> {"rows_affected": int}` **必须幂等**：连跑两遍库内容与跑一遍相同
    `build_fixture(conn)`（可选）          给幂等自检造一个够它跑的最小库；真迁移必须有（见 tests）
- `run_pending()` 在编排器里排在 `production_sync` 之后、扫描之前。流程：
    布局核对 → 读记录 → 已应用的文件内容 sha256 不许变（变了 = `tampered`，**什么都不跑**）→
    有待办才在线备份库（备份失败 = 什么都不跑）→ 逐个应用，每个成功追加一行 `applied`，
    失败追加一行 `failed` 并**停下**（后面的不跑；下次重试，所以才要求幂等）。
- 记录 `PATHS.migrations_state/applied.jsonl` 只追加。「已应用」= 该 id 最后一条记录是 `applied`。
- `apply=False`（dry-run）：只报待办，**不备份、不写记录、不碰库**。

「这个失败，下游怎么知道？」：返回值的 `status` 是 ok 以外的任何值都是红——编排器把它并进
`status.json` 的 `steps_result.data_migrations`；`ledger_error` 单独成类：迁移已跑但没记上，
靠幂等重跑兜底，但**必须可见**。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from data_backup.sqlite_readonly import read_only_connect   # 现成的只读打开策略（hot journal 拒绝 / WAL 无 -wal 用 immutable）

LEDGER_NAME = "applied.jsonl"
NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")
VERSIONS_DIR = Path(__file__).resolve().parent / "versions"   # 代码，随代码发布 ⇒ __file__ 锚点才对


class MigrationLayoutError(Exception):
    pass


class LedgerError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    id: str
    path: Path
    sha256: str


@dataclass
class MigrationContext:
    db_path: Path
    home: Path
    log: Callable[[str], None]


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ── 路径（调用时求值，不在 import 期冻结） ────────────────────────────────
def default_ledger_dir() -> Path:
    from hive_logger import PATHS
    return Path(PATHS.migrations_state)


def default_db_path() -> Path:
    from hive_logger import PATHS
    return Path(PATHS.db)


def beside(db_path: Path, name: str) -> Path:
    """备份与记录跟着**这个库**走（它所在的根），不跟环境走：对 /tmp 里的库副本 `--apply`，
    不许把备份堆进生产 `_manual_backups/`、也不许给生产 `applied.jsonl` 加一行描述别的库的记录。"""
    return Path(db_path).resolve().parent / name


# ── 记录 ──────────────────────────────────────────────────────────────────
def read_ledger(ledger_dir: Path) -> list[dict]:
    p = Path(ledger_dir) / LEDGER_NAME
    if not p.exists():
        return []
    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise LedgerError(f"{p} 第 {n} 行不是合法 JSON：{e}") from e
        if not isinstance(rec, dict) or "id" not in rec or "status" not in rec:
            raise LedgerError(f"{p} 第 {n} 行缺 id/status：{line[:120]!r}")
        out.append(rec)
    return out


def append_record(ledger_dir: Path, rec: dict) -> None:
    d = Path(ledger_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / LEDGER_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def applied_state(records: list[dict]) -> dict[str, dict]:
    """id → 该 id 最后一条记录；只有最后一条是 applied 的才算已应用。"""
    last: dict[str, dict] = {}
    for r in records:
        last[r["id"]] = r
    return last


def ensure_history(ledger_dir: Path) -> int:
    """把 `history.HISTORICAL` 里尚未登记的条目补进记录（按 id 幂等）。返回新写条数。"""
    from data_migrations.history import HISTORICAL, HISTORY_SOURCE
    have = {r["id"] for r in read_ledger(ledger_dir)}
    n = 0
    for h in HISTORICAL:
        if h["id"] in have:
            continue
        append_record(ledger_dir, {"id": h["id"], "kind": "historical", "status": "applied", "at": _now(),
                                   "source": HISTORY_SOURCE, **{k: v for k, v in h.items() if k != "id"}})
        n += 1
    return n


def record_tool_run(tool: str, *, rows_affected: Optional[int], backup: Optional[str], args: str = "",
                    ledger_dir: Optional[Path] = None, db_path: Optional[Path] = None) -> bool:
    """手工修复工具真写库之后调用：追加一行 `kind="manual_tool"`。写不进去返回 False 并打 stderr（数据已写，
    不回滚、不改工具退出码——但不许静默）。"""
    import sys
    try:
        if ledger_dir is None:
            ledger_dir = beside(db_path, "migrations_state") if db_path else default_ledger_dir()
        append_record(Path(ledger_dir),
                      {"id": f"T-{tool}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}", "kind": "manual_tool",
                       "status": "applied", "at": _now(), "tool": tool, "args": args,
                       "rows_affected": rows_affected, "backup": backup, "db": str(db_path) if db_path else None})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 数据已写入，但运行记录没写进 {tool} 的 applied.jsonl：{type(e).__name__}: {e}", file=sys.stderr)
        return False


# ── 备份 ──────────────────────────────────────────────────────────────────
def online_backup(db_path: Path, dest: Path) -> Path:
    """sqlite 在线备份（WAL 安全：不是 cp）。写 .partial → integrity_check=ok 才改名。失败抛。"""
    db_path, dest = Path(db_path), Path(dest)
    if not db_path.exists():
        raise FileNotFoundError(f"库不存在：{db_path}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".partial")
    if part.exists():
        part.unlink()
    src = read_only_connect(db_path)    # HotJournalError 照抛：run_pending 记成 backup_failed
    dst = sqlite3.connect(str(part))
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"备份 integrity_check = {ok!r}")
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    os.replace(part, dest)
    return dest


def backup_before_write(db_path: Path, tag: str, backup_dir: Optional[Path] = None) -> Path:
    """给回填 / 修复工具用：真写之前的在线备份，落 `_manual_backups/`（不被每日轮转清理）。失败抛 ⇒ 调用方必须中止。"""
    d = Path(backup_dir) if backup_dir else beside(db_path, "_manual_backups")
    return online_backup(db_path, d / f"pheromone.db.bak-{tag}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")


# ── 发现 / 加载 ───────────────────────────────────────────────────────────
def discover(versions_dir: Optional[Path] = None) -> list[Migration]:
    d = Path(versions_dir) if versions_dir else VERSIONS_DIR
    found: list[Migration] = []
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.name in ("__init__.py", "__pycache__") or p.suffix != ".py":
                continue
            m = NAME_RE.match(p.name)
            if not m:
                raise MigrationLayoutError(f"{p.name} 不符合 NNNN_说明.py（小写字母 / 数字 / 下划线）")
            found.append(Migration(m.group(1), p, sha256_file(p)))
    ids = [m.id for m in found]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise MigrationLayoutError(f"迁移编号重复：{dup}")
    for want, got in zip((f"{i:04d}" for i in range(1, len(ids) + 1)), sorted(ids)):
        if want != got:
            raise MigrationLayoutError(f"迁移编号断号：期望 {want}，实际 {got}（编号必须从 0001 连续）")
    return sorted(found, key=lambda m: m.id)


def _load(m: Migration):
    spec = importlib.util.spec_from_file_location(f"data_migrations._v{m.id}", m.path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("DESCRIPTION", "apply"):
        if not hasattr(mod, attr):
            raise MigrationLayoutError(f"{m.path.name} 缺 {attr}")
    return mod


def db_fingerprint(db_path: Path) -> str:
    """库内容指纹（iterdump 的 sha256）：幂等自检与前后对比用。"""
    con = read_only_connect(db_path)
    try:
        h = hashlib.sha256()
        for line in con.iterdump():
            h.update(line.encode("utf-8"))
        return h.hexdigest()
    finally:
        con.close()


def check_idempotent(m: Migration, workdir: Path) -> dict:
    """在 `build_fixture` 造的最小库上连跑两遍：第二遍后的库指纹必须等于第一遍后的。返回 {"ok", ...}。"""
    mod = _load(m)
    if not hasattr(mod, "build_fixture"):
        return {"ok": False, "reason": "缺 build_fixture，无法自检幂等"}
    db = Path(workdir) / f"idem_{m.id}.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    try:
        mod.build_fixture(con)
        con.commit()
    finally:
        con.close()
    ctx = MigrationContext(db, Path(workdir), lambda s: None)
    mod.apply(ctx)
    f1 = db_fingerprint(db)
    mod.apply(ctx)
    f2 = db_fingerprint(db)
    return {"ok": f1 == f2, "reason": None if f1 == f2 else "第二遍改动了库内容"}


# ── 主流程 ────────────────────────────────────────────────────────────────
def run_pending(*, db_path: Optional[Path] = None, versions_dir: Optional[Path] = None,
                ledger_dir: Optional[Path] = None, backup_dir: Optional[Path] = None,
                apply: bool = True, log: Optional[Callable[[str], None]] = None) -> dict:
    log = log or (lambda s: None)
    rep: dict = {"status": "ok", "dry_run": not apply, "applied": [], "failed": None, "backup": None,
                 "n_discovered": 0, "n_pending": 0, "error": None}
    try:
        if db_path is None:     # 缺省：生产路径（PATHS，调用时求值）
            db_path = default_db_path()
            ledger_dir = Path(ledger_dir) if ledger_dir else default_ledger_dir()
        else:                   # 显式给了库：记录与备份跟着它
            db_path = Path(db_path)
            ledger_dir = Path(ledger_dir) if ledger_dir else beside(db_path, "migrations_state")
        mig = discover(versions_dir)
    except MigrationLayoutError as e:
        rep.update(status="layout_error", error=str(e))
        return rep
    rep["n_discovered"] = len(mig)
    try:
        state = applied_state(read_ledger(ledger_dir))
    except LedgerError as e:
        rep.update(status="ledger_error", error=str(e))
        return rep

    by_id = {m.id: m for m in mig}
    for mid, rec in state.items():
        if rec.get("kind", "migration") != "migration" or rec["status"] != "applied":
            continue
        cur = by_id.get(mid)
        if cur is None:
            rep.update(status="tampered", error=f"已应用的迁移 {mid} 的文件不见了")
            return rep
        if rec.get("sha256") != cur.sha256:
            rep.update(status="tampered", error=f"已应用的迁移 {mid} 文件内容变了（记录 {str(rec.get('sha256'))[:12]} ≠ 现在 {cur.sha256[:12]}）；"
                                                 "改已应用的迁移 = 改历史，新建一个编号更大的迁移")
            return rep

    pending = [m for m in mig if not (m.id in state and state[m.id]["status"] == "applied")]
    rep["n_pending"] = len(pending)
    if not apply:
        rep["pending_ids"] = [m.id for m in pending]
        return rep

    try:
        ensure_history(ledger_dir)
    except Exception as e:  # noqa: BLE001
        rep.update(status="ledger_error", error=f"补记历史失败：{type(e).__name__}: {e}")
        return rep
    if not pending:
        return rep

    try:
        bk = online_backup(db_path, (Path(backup_dir) if backup_dir else beside(db_path, "_manual_backups"))
                           / f"pheromone.db.bak-migrate-{pending[0].id}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
    except Exception as e:  # noqa: BLE001 —— 没有备份就不动库
        rep.update(status="backup_failed", error=f"{type(e).__name__}: {e}；未执行任何迁移")
        return rep
    rep["backup"] = str(bk)

    home = db_path.parent
    for m in pending:
        t0 = time.time()
        try:
            mod = _load(m)
            res = mod.apply(MigrationContext(db_path, home, log))
            if not isinstance(res, dict) or not isinstance(res.get("rows_affected"), int):
                raise TypeError(f"apply() 必须返回含 int rows_affected 的 dict，实际 {res!r}")
        except Exception as e:  # noqa: BLE001
            rep.update(status="failed", failed=m.id, error=f"{type(e).__name__}: {e}")
            try:
                append_record(ledger_dir, {"id": m.id, "kind": "migration", "status": "failed", "at": _now(),
                                           "sha256": m.sha256, "error": rep["error"], "backup": rep["backup"]})
            except Exception as e2:  # noqa: BLE001
                rep["error"] += f"；失败记录也没写进去：{e2}"
            return rep
        try:
            append_record(ledger_dir, {"id": m.id, "kind": "migration", "status": "applied", "at": _now(),
                                       "sha256": m.sha256, "description": getattr(mod, "DESCRIPTION", ""),
                                       "rows_affected": res["rows_affected"], "detail": res.get("detail"),
                                       "duration_s": round(time.time() - t0, 2), "backup": rep["backup"]})
        except Exception as e:  # noqa: BLE001 —— 迁移已跑、没记上：靠幂等重跑兜底，但必须红
            rep.update(status="ledger_error", failed=m.id,
                       error=f"迁移 {m.id} 已执行但运行记录写不进去（{type(e).__name__}: {e}）；下次会重跑（幂等）")
            return rep
        rep["applied"].append(m.id)
        log(f"迁移 {m.id} 已应用：{res['rows_affected']} 行")
    return rep
