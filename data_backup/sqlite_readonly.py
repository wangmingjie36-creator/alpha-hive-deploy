"""源库只读打开策略——照抄阶段 0 应急快照脚本的实现，不重新发明。

来源：`~/alpha-hive-data/_pre_migration_snapshots/20260914_055601/
make_pre_migration_snapshot.py` 的 `db_open_uri()`（2026-09-14，v0.45.233）。
三条规则，每条都有当时的实测支撑：

1. WAL 模式且 `-wal` sidecar 已存在 → `mode=ro`（读 WAL；只读连接不会新建文件）。
2. WAL 模式但没有 `-wal`（已完全 checkpoint）→ `mode=ro&immutable=1`——
   实测：光 `mode=ro` 会在源目录**新建** -wal/-shm 且关闭后删不掉；
   没有 -wal 就没有未回写的页，一致性不受影响。
3. rollback 模式 → `mode=ro`（读期间持 SHARED 锁挡住写者，不建任何 sidecar 文件）。

`-journal`（hot journal）存在时拒绝打开——只读连接无法回滚，必须人工介入。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class HotJournalError(RuntimeError):
    """源库有 hot journal（`-journal`），只读连接无法回滚，需人工核实。"""


def db_open_uri(path: Path) -> tuple[str, str]:
    """返回 (sqlite URI, 人类可读的打开方式说明)。"""
    path = Path(path)
    with open(path, "rb") as f:
        hdr = f.read(100)
    if hdr[:16] != b"SQLite format 3\x00":
        raise RuntimeError(f"{path} 不是 SQLite 文件")
    wal_mode = hdr[18] == 2
    has_wal = Path(str(path) + "-wal").exists()
    has_journal = Path(str(path) + "-journal").exists()
    if has_journal:
        raise HotJournalError(f"{path} 存在 hot journal（-journal），只读连接无法回滚——停下人工查")
    if wal_mode and not has_wal:
        return f"file:{path}?mode=ro&immutable=1", "WAL/无-wal → mode=ro&immutable=1"
    if wal_mode:
        return f"file:{path}?mode=ro", "WAL/有-wal → mode=ro"
    return f"file:{path}?mode=ro", "rollback → mode=ro"


def read_only_connect(path: Path) -> sqlite3.Connection:
    """按上述策略只读打开一个 SQLite 库，返回已连接的 Connection。"""
    uri, _how = db_open_uri(path)
    return sqlite3.connect(uri, uri=True)


def sha256_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
