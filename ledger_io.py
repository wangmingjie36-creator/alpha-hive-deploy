"""账本文件读写的**唯一实现**（v0.45.448）：代码层面写不出坏行。

为什么要有它
------------
此前每个模块各写一份 `_write_jsonl` / `_append_jsonl` / `_atomic_write_text`（7 个生产模块各有一份，另有 15 个
模块直接 `open(..., "a")`），各漏各的口子：
  ① 追加是裸 `open("a")`、不 fsync —— 写到一半崩溃 / 断电 / 磁盘满 ⇒ 半行；下一次追加把两条粘成一行坏行；
  ② 读-改-写不加锁 —— 手动补跑与定时扫描撞上 ⇒ 后写者用旧快照覆盖先写者，对方的记录**静默消失**
     （`sell_strike_ledger` 2026-09-26 探针实测过，它自己加了目录锁，别处没有）；
  ③ 有的模块写前不清洗 NaN（`json.dumps` 缺省写出 `NaN` 字面量——不是合法 JSON）；
  ④ 读时坏行被静默跳过 —— 持仓文件坏一行 = 少一个仓位、照样往下算。
这里一次堵死 ①②③：**加锁 + 原子替换 + 严格序列化 + 写后回读校验**。代码写不出坏行之后，读到坏行只可能是
代码之外的损坏（手改 / 磁盘 / 坏备份）——那时 `load_jsonl` 抛 `LedgerCorrupt`、停下来，由调用方的失败观测
报警，从每日异地备份恢复；**不猜、不跳过**。

守卫：`tests/test_ledger_writes_go_through_ledger_io.py`——在本模块之外新写裸追加 / 手写账本读写助手即红；
现存的手写写入方逐条登记为「待迁移」债务（迁一个删一条，过期条目也红）。

约定
----
- 锁：对账本**目录**的 fd 加 `fcntl.flock(LOCK_EX)`（照 `sell_strike_ledger._tenor_lock`：不另建 .lock 文件，
  免得进备份跳过清单）。**同进程可重入**（同一线程嵌套 `locked()` 不会自锁），跨线程 / 跨进程互斥；等待超过
  `timeout` ⇒ `LedgerLockTimeout`（不退回无锁写——那正是要堵的丢数据）。fd 关闭即释放，进程崩了不留死锁。
- 写：先逐条 `json.dumps(allow_nan=False)` 并核对是对象（任何一条不合法 ⇒ `LedgerWriteError`，**文件一个字节都
  不动**）→ 同目录临时文件 → fsync → `os.replace` → fsync 目录 → 读回逐字节比对。
- 追加：持锁读出现有字节、逐行严格校验，再把「原字节 + 新行」整文件原子替换——旧行**逐字节不变**，
  不会因重新序列化改了历史行的写法。
- 读：缺文件 ⇒ `[]`；坏 JSON / 非对象 / NaN·Infinity 字面量 ⇒ `LedgerCorrupt`（带文件与行号）。
- 整文件 JSON 文档（v0.45.452，`meta.json` 这类）：`write_json` / `load_json`，规则同上——顶层必须是对象、
  拒 NaN、原子替换 + 回读；读到坏文档 ⇒ `LedgerCorrupt`，**不**退回缺省值。`meta.json` 不是装饰性文件：
  纸面组合的 `cash` 就存在里面（v0.45.97：`"cash": NaN` 落盘后被 `json.loads` 照单全收，净值烂了四天）。

失败在源头登记（v0.45.452）
--------------------------
调用方大多把账本步骤包在 `except Exception: log.warning(...)` 里（「非致命」）——严格读写把坏数据变成了异常，
异常却又被吞成一行没人看的 warning，等于没修。所以**本模块自己**记下每一次失败（`failures()`：次数 + 前几条的
文件 / 操作 / 原因），扫描收尾经 `scan_timing.counters()` 进 status.json，`alert_manager` 见 n>0 即 P2。
谁吞了异常都不影响这一路会红。
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

DEFAULT_LOCK_TIMEOUT_SEC = 300.0


class LedgerError(Exception):
    """账本读写错误的基类。"""


class LedgerCorrupt(LedgerError):
    """读到的账本文件里有坏行（坏 JSON / 非对象 / NaN 字面量）。代码写不出它 ⇒ 是外部损坏：停下、从备份恢复。"""


class LedgerWriteError(LedgerError):
    """要写的记录不合法（非对象 / NaN / 不可序列化），或写后回读与要写的不一致。文件未被改动（回读不一致除外）。"""


class LedgerLockTimeout(LedgerError, TimeoutError):
    """账本目录的写锁等了 `timeout` 秒仍被别的写者占着。"""


# ─────────────────────────────── 失败登记（源头观测，调用方吞不掉）

_FAILURES_MAX_ITEMS = 20
_FAIL_GUARD = threading.Lock()
_FAILURES: Dict = {"n": 0, "items": []}


def _record_failure(op: str, path, exc: BaseException) -> None:
    if getattr(exc, "_ledger_io_recorded", False):
        return                              # 嵌套调用（append → atomic_write_text）只记一次
    try:
        exc._ledger_io_recorded = True      # type: ignore[attr-defined]
    except AttributeError:
        pass
    with _FAIL_GUARD:
        _FAILURES["n"] += 1
        if len(_FAILURES["items"]) < _FAILURES_MAX_ITEMS:
            _FAILURES["items"].append({"op": op, "path": str(path), "kind": type(exc).__name__,
                                       "error": str(exc)[:300]})


def failures() -> Dict:
    """本进程至今账本读写失败的次数与前几条明细（扫描收尾进 status.json，n>0 ⇒ P2）。"""
    with _FAIL_GUARD:
        return {"n": _FAILURES["n"], "items": [dict(i) for i in _FAILURES["items"]]}


def reset_failures() -> None:
    """清零（测试用；生产每轮扫描是新进程）。"""
    with _FAIL_GUARD:
        _FAILURES["n"] = 0
        _FAILURES["items"] = []


@contextlib.contextmanager
def _observed(op: str, path):
    try:
        yield
    except (LedgerError, OSError) as exc:
        _record_failure(op, path, exc)
        raise


# ─────────────────────────────── 锁（同进程可重入）

_REGISTRY_GUARD = threading.Lock()
_THREAD_LOCKS: Dict[str, threading.RLock] = {}
_DEPTH: Dict[str, int] = {}
_FDS: Dict[str, int] = {}


def _thread_lock(key: str) -> threading.RLock:
    with _REGISTRY_GUARD:
        lk = _THREAD_LOCKS.get(key)
        if lk is None:
            lk = _THREAD_LOCKS[key] = threading.RLock()
        return lk


def _flock_dir(d: Path, timeout: float) -> int:
    fd = os.open(str(d), os.O_RDONLY)
    t0 = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() - t0 >= timeout:
                    raise LedgerLockTimeout(f"账本目录 {d} 的写锁等了 {timeout:.0f}s 仍被占用（另一个写者卡住了？）"
                                            "——本次不写，免得覆盖对方") from None
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def locked(directory, timeout: Optional[float] = None) -> Iterator[Path]:
    """账本目录读-改-写的排他锁。跨进程 / 跨线程互斥；同一线程嵌套可重入。"""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    d = d.resolve()
    key = str(d)
    wait = DEFAULT_LOCK_TIMEOUT_SEC if timeout is None else float(timeout)
    tl = _thread_lock(key)
    if not tl.acquire(timeout=wait):
        exc = LedgerLockTimeout(f"账本目录 {d} 的写锁被本进程另一个线程占着超过 {wait:.0f}s")
        _record_failure("lock", d, exc)
        raise exc
    try:
        depth = _DEPTH.get(key, 0)
        if depth == 0:
            with _observed("lock", d):
                _FDS[key] = _flock_dir(d, wait)
        _DEPTH[key] = depth + 1
        try:
            yield d
        finally:
            _DEPTH[key] -= 1
            if _DEPTH[key] == 0:
                del _DEPTH[key]
                os.close(_FDS.pop(key))
    finally:
        tl.release()


def locked_by(dir_fn: Callable[[], object], timeout: Optional[float] = None):
    """装饰器：整个函数在账本目录锁里跑（读-改-写一次做完）。`dir_fn` **调用时**求值——模块把账本路径放在
    可被测试重定向的全局里（`SIGNALS_FILE` 等），在 import 期求值会锁错目录。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with locked(dir_fn(), timeout=timeout):
                return fn(*args, **kwargs)
        wrapper.__ledger_locked__ = True       # 测试据此断言「这个入口持锁」
        return wrapper
    return deco


def is_locked_here(directory) -> bool:
    """本进程当前是否持有该目录的写锁（测试与断言用）。"""
    try:
        key = str(Path(directory).resolve())
    except OSError:
        return False
    return _DEPTH.get(key, 0) > 0


# ─────────────────────────────── 读

def _reject_constant(name: str):
    raise ValueError(f"非法 JSON 字面量 {name}")


def _parse_line(path: Path, lineno: int, line: str) -> Dict:
    try:
        rec = json.loads(line, parse_constant=_reject_constant)
    except ValueError as e:
        raise LedgerCorrupt(f"{path} 第 {lineno} 行不是合法 JSON（{e}）：{line[:120]!r}") from None
    if not isinstance(rec, dict):
        raise LedgerCorrupt(f"{path} 第 {lineno} 行不是对象（{type(rec).__name__}）：{line[:120]!r}")
    return rec


def load_jsonl(path) -> List[Dict]:
    """严格读：缺文件 ⇒ []；任何坏行 ⇒ `LedgerCorrupt`（带行号）。空行忽略。"""
    path = Path(path)
    with _observed("load", path):
        if not path.exists():
            return []
        text = _read_text(path)
        out: List[Dict] = []
        for i, line in enumerate(text.split("\n"), start=1):
            if line.strip():
                out.append(_parse_line(path, i, line))
        return out


_MISSING = object()


def load_json(path, default=_MISSING):
    """严格读整文件 JSON 文档（`meta.json` 这类）：缺文件 ⇒ `default`（不给就抛 `FileNotFoundError`）；
    坏 JSON / NaN·Infinity 字面量 / 顶层不是对象 ⇒ `LedgerCorrupt`。**不**把坏文档当成缺文件——
    v0.45.452 前跨式腿的 `_load_meta` 就是这么做的：meta 坏了 ⇒ 「按新账本处理」⇒ 现金重置成起始资金并写回。"""
    path = Path(path)
    with _observed("load", path):
        if not path.exists():
            if default is _MISSING:
                raise FileNotFoundError(str(path))
            return default
        text = _read_text(path)
        try:
            obj = json.loads(text, parse_constant=_reject_constant)
        except ValueError as e:
            raise LedgerCorrupt(f"{path} 不是合法 JSON 文档（{e}）") from None
        if not isinstance(obj, dict):
            raise LedgerCorrupt(f"{path} 顶层不是对象（{type(obj).__name__}）")
        return obj


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise LedgerCorrupt(f"{path} 不是合法 UTF-8（{e}）") from None


# ─────────────────────────────── 写

def _serialize(path: Path, records) -> str:
    lines = []
    for i, r in enumerate(records):
        if not isinstance(r, dict):
            raise LedgerWriteError(f"{path} 第 {i + 1} 条不是对象（{type(r).__name__}）——账本只收对象，本次不写")
        try:
            lines.append(json.dumps(r, ensure_ascii=False, allow_nan=False))
        except (ValueError, TypeError) as e:
            raise LedgerWriteError(f"{path} 第 {i + 1} 条无法写成合法 JSON（{e}）——本次不写") from None
    return "".join(s + "\n" for s in lines)


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass                       # 有的文件系统不许 fsync 目录；替换本身已原子
    finally:
        os.close(fd)


def atomic_write_text(path, content: str, mode: int = 0o644, verify: bool = True) -> None:
    """同目录临时文件 → fsync → `os.replace` → fsync 目录 → 读回逐字节比对。任何一步失败：原文件不变（读回不一致除外，那时抛）。

    `verify=False` 只给**可再生缓存**用（`hive_logger.atomic_json_write`）：缓存不持锁、并发蜂会同时写同一个缓存文件，
    后写者赢是对的，读回比对在那里只会报假警。账本一律保持缺省 True（账本写者持锁，读回不一致就是真问题）。"""
    path = Path(path)
    with _observed("write", path):
        _atomic_write_text(path, content, mode, verify)


def _atomic_write_text(path: Path, content: str, mode: int, verify: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)
    if verify and path.read_text(encoding="utf-8") != content:
        raise LedgerWriteError(f"{path} 写后读回与写入内容不一致（磁盘 / 并发写者？）")


def write_jsonl(path, records) -> None:
    """整文件原子重写。任何一条不合法 ⇒ `LedgerWriteError`，文件不动。调用方负责持锁（读-改-写时）。"""
    path = Path(path)
    with _observed("write", path):
        atomic_write_text(path, _serialize(path, list(records)))


def dumps_document(path, obj, indent: Optional[int] = 2, sort_keys: bool = False) -> str:
    """整文件 JSON 文档的严格序列化（顶层对象、拒 NaN / 不可序列化）；不合法 ⇒ `LedgerWriteError`。"""
    if not isinstance(obj, dict):
        raise LedgerWriteError(f"{path} 顶层不是对象（{type(obj).__name__}）——账本文档只收对象，本次不写")
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=indent, sort_keys=sort_keys) + "\n"
    except (ValueError, TypeError) as e:
        raise LedgerWriteError(f"{path} 无法写成合法 JSON（{e}）——本次不写") from None


def write_json(path, obj, indent: Optional[int] = 2, sort_keys: bool = False) -> None:
    """整文件 JSON 文档原子写（`meta.json` 这类）。不合法 ⇒ `LedgerWriteError`，文件一个字节都不动。
    调用方负责持锁（读-改-写时）。"""
    path = Path(path)
    with _observed("write", path):
        atomic_write_text(path, dumps_document(path, obj, indent, sort_keys))


def append_jsonl(path, record: Dict, timeout: Optional[float] = None) -> None:
    """追加一条：持目录锁、严格校验现有行、原字节 + 新行整文件原子替换（旧行逐字节不变）。
    现有文件有坏行 ⇒ `LedgerCorrupt`（不在坏文件上继续追加）。"""
    path = Path(path)
    with _observed("append", path):
        _append(path, record, timeout)


def _append(path: Path, record: Dict, timeout: Optional[float]) -> None:
    new_line = _serialize(path, [record])
    with locked(path.parent, timeout=timeout):
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        for i, line in enumerate(raw.split("\n"), start=1):
            if line.strip():
                _parse_line(path, i, line)
        if raw and not raw.endswith("\n"):
            raw += "\n"                     # 最后一行完整但缺换行（手改过）：补上，不粘行
        atomic_write_text(path, raw + new_line)
