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
_code_version_at_start: Optional[dict] = None


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
    global _code_version_at_start
    with _lock:
        _phases.clear()
        _code_version_at_start = None


# ───────────────────────────────────────────── 计数器
def counters() -> Dict[str, Optional[dict]]:
    """三个取数源的进程内计数，外加一项链构造观测（`cboe_chain`，v0.45.190）。

    每一项要么是那个模块自己的 stats dict，要么是 None（模块不可导入 /
    没装闸门）。**不要把 None 改成 {}**——空 dict 会被下游读成「零调用」。

    `cboe_chain` 与前三项不同：它不数「发了几次请求」，数的是「构出来的链
    把多少近月挡在外面了」。`gex_view`（v0.45.197）数的是 Dealer GEX 专用全链视图
    的可得性——它取不到时**不回退截断链**，所以「今天有几只标的没有 GEX」是这次
    改动唯一的代价，必须可数而不是可估。两者放在这里是因为编排器已把本文件并进
    status.json，挂上即随每轮扫描落盘，无需改编排器（同 `code_version` 的走法）。
    """
    out: Dict[str, Optional[dict]] = {"yfinance": None, "twelve_data": None,
                                      "cboe": None, "cboe_chain": None,
                                      "gex_view": None}
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
        out["cboe_chain"] = cboe_options.chain_selection_stats()
        out["gex_view"] = cboe_options.gex_view_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("cboe stats 不可得: %s", e)
    return out


# ───────────────────────────────────────────── 快照与落盘
def note_code_version(info: Optional[dict]) -> None:
    """扫描启动时把 `code_version.log_startup()` 的结果交给快照（v0.45.223）。"""
    global _code_version_at_start
    with _lock:
        _code_version_at_start = dict(info) if isinstance(info, dict) else None


def code_version() -> Optional[dict]:
    """这一轮跑的是哪一版代码。取不到返回 None（不写 {}——空 dict 会被读成「测过、没版本」）。

    v0.45.182。编排器 `write_status()` 已用 jq 把本文件并进 `status.json`，
    所以挂在这里即可让版本随每轮扫描落进 status.json，**无需改编排器**。

    ⚠️ v0.45.223：优先用**扫描启动时**记下的那份（`note_code_version`）。此前这里在
    快照落盘时（扫描末尾）现解析，而那时部署已经在本地造了日报提交 ⇒ `sha` 是日报提交，
    不是扫描所用的代码（09-11 实测：快照 `1826d3e`，启动日志 `5b6276c`）；扫描中途有人
    手工快进生产，还会记成另一份代码。`resolved_at` 标明取自哪个时点，没有启动记录时
    退回现解析并如实标 `"snapshot"`。
    """
    with _lock:
        start = _code_version_at_start
    if start is not None:
        return {**start, "resolved_at": "scan_start"}
    try:
        import code_version as _cv
        info = _cv.resolve()
        return {**info, "resolved_at": "snapshot"} if isinstance(info, dict) else info
    except Exception as e:  # noqa: BLE001 - 观测代码不得影响主流程
        _log.warning("code_version 不可得（status.json 将缺版本字段）: %s", e)
        return None


def production_sync_result(date_str: str) -> Optional[dict]:
    """本轮扫描前的生产代码同步结果（v0.45.214，编排器在 Step 1 前调 `production_sync.py`）。

    与 `code_version` 同一条路：挂在这里就随 `scan_timing.json` 并进 `status.json`，
    `alert_manager` 据此告警。没跑、写失败、或是别的日期的 ⇒ None（「没测到」）。
    """
    try:
        import production_sync as _ps
        return _ps.load_for_date(date_str)
    except Exception as e:  # noqa: BLE001 - 观测代码不得影响主流程
        _log.warning("production_sync 结果不可得（status.json 将缺该字段）: %s", e)
        return None


_GIT_PUSH_KEYS = ("success", "integration", "behind", "merge_commit", "conflicts",
                  "attempts", "error", "skipped", "fetch_error")


def git_push_summary(git_push: Optional[dict]) -> Optional[dict]:
    """`results["git_push"]` 进 status.json 的精简版（v0.45.214）。

    git 的 `output` 带整段 hint，截到 500 字符；None 原样返回（「没记录」≠「成功」）。
    """
    if not isinstance(git_push, dict):
        return None
    out = {k: git_push[k] for k in _GIT_PUSH_KEYS if k in git_push}
    if git_push.get("output"):
        out["output"] = str(git_push["output"])[:500]
    return out


def git_commit_summary(git_commit: Optional[dict]) -> Optional[dict]:
    """`results["git_commit"]` 进 status.json 的精简版（v0.45.223）。

    `pending_artifacts` 是提交前待提交的日报产物数：0 ⇒ 失败只是「没东西可提交」；
    None（git status 就失败了）⇒ 不知道，按失败看。None 入参原样返回（工作区干净，未尝试提交）。

    v0.45.227：`left_artifacts`（提交后仍没进 git 的日报产物数；None = 提交后那次 git status 失败）
    与 `left_sample` 有才抄——没有这个键 = 旧代码或没走到提交，告警侧据此区分「没核」与「核不了」。
    原因优先 `error`，其次 `add_errors`：提交成功但有 add 重试后仍失败时，`message` 只是提交成功的输出。
    """
    if not isinstance(git_commit, dict):
        return None
    out = {"success": git_commit.get("success"),
           "pending_artifacts": git_commit.get("pending_artifacts")}
    for key in ("left_artifacts", "left_sample"):
        if key in git_commit:
            out[key] = git_commit[key]
    add_errors = git_commit.get("add_errors")
    reason = (git_commit.get("error")
              or ("git add 失败：" + "；".join(add_errors) if add_errors else None)
              or git_commit.get("message"))
    if reason:
        out["reason"] = str(reason)[:300]
    return out


def snapshot(date_str: str, extra: Optional[dict] = None) -> dict:
    snap = {
        "date": date_str,
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "code_version": code_version(),
        "production_sync": production_sync_result(date_str),
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
