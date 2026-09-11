"""扫描耗时可见化（v0.45.118）：五阶段计时 + 三个取数计数器，落成一份 JSON。

为什么要有这个文件
------------------
2026-08-26 → 09-04，规则模式 Step 2 从 749s 涨到 3342s（4.5×），中间还被
超时杀了两次（08-28、08-31）——**全程没有任何东西响**。原因不是没数据：
`yf_gate._stats`、`twelve_data.bars_cache_stats()` 早就在数，只是没人打印；
各阶段的 `time.time()` 差值也都算过，只是散在日志里、隔天就没法对比。

本模块只做三件事，**不改任何取数行为**：
  1. 记各阶段墙钟耗时（`record` / `timed`）
  2. 收三个取数源的计数器（`counters`）——**取不到就写 None，不写 0**：
     0 是「测过为零」，None 是「没测到」，两者在页面上必须可区分
     （同 v0.45.114 的教训）
  3. 原子写 `logs/scan_timing.json`，编排器 `write_status()` 把它并进
     `status.json`，供 alert_manager / 周度趋势读

阶段名不做枚举约束——调用方记什么就是什么。JSON 里没有的阶段 = 那段没跑到
（比如空扫描护栏早退），读者按缺失处理，不要补零。
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, Optional

try:
    from hive_logger import get_logger, PATHS
    _log = get_logger("scan_timing")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.scan_timing")
    PATHS = None

FILENAME = "scan_timing.json"

_lock = threading.Lock()
_phases: Dict[str, float] = {}


# ───────────────────────────────────────────── 计时
def record(phase: str, seconds: float) -> None:
    """记一段耗时。同名多次调用**累加**（重试轮次、分批都算进同一阶段）。"""
    with _lock:
        _phases[phase] = _phases.get(phase, 0.0) + float(seconds)


@contextmanager
def timed(phase: str) -> Iterator[None]:
    """`with timed("parallel"): ...` —— 异常照样记时再抛出，别让失败的阶段消失。"""
    t0 = time.monotonic()
    try:
        yield
    finally:
        record(phase, time.monotonic() - t0)


def phases() -> Dict[str, float]:
    with _lock:
        return {k: round(v, 3) for k, v in _phases.items()}


def reset() -> None:
    with _lock:
        _phases.clear()


# ───────────────────────────────────────────── 计数器
def counters() -> Dict[str, Optional[dict]]:
    """三个取数源的进程内计数。

    每一项要么是那个模块自己的 stats dict，要么是 None（模块不可导入 /
    没装闸门）。**不要把 None 改成 {}**——空 dict 会被下游读成「零调用」。
    """
    out: Dict[str, Optional[dict]] = {"yfinance": None, "twelve_data": None, "cboe": None}
    try:
        import yf_gate
        out["yfinance"] = yf_gate.stats() if yf_gate.is_installed() else None
    except Exception as e:  # noqa: BLE001 - 观测代码不得影响主流程
        _log.debug("yf_gate stats 不可得: %s", e)
    try:
        import twelve_data
        out["twelve_data"] = twelve_data.bars_cache_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("twelve_data stats 不可得: %s", e)
    try:
        import cboe_options
        out["cboe"] = cboe_options.payload_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("cboe stats 不可得: %s", e)
    return out


# ───────────────────────────────────────────── 快照与落盘
def snapshot(date_str: str, extra: Optional[dict] = None) -> dict:
    snap = {
        "date": date_str,
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "phases": phases(),
        "counters": counters(),
    }
    if extra:
        snap["extra"] = dict(extra)
    return snap


def default_path() -> Path:
    if PATHS is not None:
        return PATHS.logs_dir / FILENAME
    return Path("logs") / FILENAME


def write(date_str: str, path: Optional[Path] = None, extra: Optional[dict] = None) -> Optional[Path]:
    """原子写。失败只记 warning、返回 None——观测文件写不出来不能拖垮扫描。"""
    target = Path(path) if path else default_path()
    snap = snapshot(date_str, extra)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except OSError as e:
        _log.warning("scan_timing 写入失败（不影响扫描）: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    _log.info("扫描耗时分解 → %s", target)
    _log.info("%s", summary_line(snap))
    return target


def _fmt_s(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0f}s"


def summary_line(snap: dict) -> str:
    """一行人读摘要，进日志用。缺的阶段/计数器显示 `—`，绝不显示 0。"""
    p = snap.get("phases") or {}
    c = snap.get("counters") or {}
    order = ["prefetch", "parallel", "enrichment", "backtest_weights",
             "ml_reports", "save_report", "deploy"]
    parts = [f"{k}={_fmt_s(p.get(k))}" for k in order if k in p]
    for k in sorted(set(p) - set(order)):
        parts.append(f"{k}={_fmt_s(p[k])}")

    yf = c.get("yfinance")
    td = c.get("twelve_data")
    cb = c.get("cboe")
    yf_s = "—" if yf is None else f"{yf.get('calls', '?')}次(429×{yf.get('rate_limited', '?')})"
    td_s = "—" if td is None else f"请求{td.get('fetches', '?')}/命中{td.get('hits', '?')}"
    cb_s = "—" if cb is None else f"抓取{cb.get('fetches', '?')}/命中{cb.get('hits', '?')}"
    return ("耗时 " + " | ".join(parts) +
            f" ‖ yfinance {yf_s} | TwelveData {td_s} | CBOE {cb_s}")
