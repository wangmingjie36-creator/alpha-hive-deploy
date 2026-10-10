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

    `portfolio_greeks`（v0.45.423）：组合 Greeks 当天的标的价场次核对（`price_check_stats`）——
    多少行的价不属于 as_of 那一场（陈旧，不进 $Delta）、SPY 价是哪一场的、有没有因此拒绝成交。
    此前同日跑恒用前一交易日的收盘、一个计数都没有；alert_manager 读它报 P2。
    v0.45.435 起还带「缺」（`n_unpriced` / `n_quote_missing` / `n_beta_missing` / `nav_missing` / `gaps`）与
    `hedge_undecided`（对冲决定做不出来的原因）——09-15~25 连续 7 天 unknown 曾零告警。

    `cboe_raw`（v0.45.333）：卖权行权价账本取原始链（`fetch_cboe_raw_contracts`）各出口的次数——
    ok / snapshot_mode / stale_vintage / payload_unavailable / vintage_mismatch / vintage_unverifiable /
    price_unavailable / no_parseable_contracts 分开数，不折叠。此前这组计数只有 cboe_options 自己和
    测试读，账本「今天为什么 0 行」只能翻日志拼。
    """
    out: Dict[str, Optional[dict]] = {"yfinance": None, "twelve_data": None,
                                      "cboe": None, "cboe_chain": None,
                                      "gex_view": None, "cboe_raw": None,
                                      "options_snapshot": None, "hv_gap": None,
                                      "portfolio_greeks": None, "paper_portfolio": None,
                                      "ledger_io": None}
    try:
        import yf_gate
        out["yfinance"] = yf_gate.stats() if yf_gate.is_installed() else None
    except Exception as e:  # noqa: BLE001 - 观测代码不得影响主流程
        _log.debug("yf_gate stats 不可得: %s", e)
    try:
        import options_analyzer
        out["hv_gap"] = options_analyzer.hv_gap_stats()   # v0.45.383：日线缺交易日的校验/重取/降级计数
    except Exception as e:  # noqa: BLE001
        _log.debug("hv_gap stats 不可得: %s", e)
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
        out["cboe_raw"] = cboe_options.raw_contracts_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("cboe stats 不可得: %s", e)
    # v0.45.238：期权快照槽位。`session_mismatch` 非零 = 槽位里躺着别的会话的数据
    # （被弃用重算）；`hits_before_close` 非零 = 本轮期权指标用了盘中冻结的快照。
    # 这两项自 v0.45.249 起按**份数**计（同一份快照一个进程只计一次），hits/writes 仍按调用次数。
    try:
        import options_analyzer
        out["options_snapshot"] = options_analyzer.snapshot_slot_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("options_snapshot stats 不可得: %s", e)
    try:
        import portfolio_greeks
        out["portfolio_greeks"] = portfolio_greeks.price_check_stats()   # 本进程没跑过 ⇒ None
    except Exception as e:  # noqa: BLE001
        _log.debug("portfolio_greeks price_check 不可得: %s", e)
    try:
        import paper_portfolio
        out["paper_portfolio"] = paper_portfolio.run_stats()   # v0.45.448；本进程没跑过 ⇒ None
    except Exception as e:  # noqa: BLE001
        _log.debug("paper_portfolio run_stats 不可得: %s", e)
    try:
        import ledger_io
        # v0.45.452：账本 / 状态文件读写失败在源头登记（调用方把异常吞成 warning 也不影响这里）；n>0 ⇒ P2
        out["ledger_io"] = ledger_io.failures()
    except Exception as e:  # noqa: BLE001
        _log.debug("ledger_io failures 不可得: %s", e)
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


_GH_PAGES_KEYS = ("success", "action", "attempts", "parent_verified", "tree_unchanged",
                  "n_changed", "skipped", "reason", "cdn_verified")


def gh_pages_summary(gh_pages: Optional[dict]) -> Optional[dict]:
    """`results["gh_pages"]` 进 status.json 的精简版（v0.45.351）。

    此前部署函数根本不返回 gh-pages 结局，status.json / 告警对它全盲
    （2026-09-25 推送失败被重试捷径判成「成功」，网站停两天零告警）。
    None 原样返回（「没记录」≠「成功」，告警侧记为未执行的检查）。
    `transport_probe` 只在推送失败时存在（为「是否切 ssh.github.com:443」攒判据），原样保留。
    """
    if not isinstance(gh_pages, dict):
        return None
    out = {k: gh_pages[k] for k in _GH_PAGES_KEYS if k in gh_pages}
    if gh_pages.get("commit"):
        out["commit"] = str(gh_pages["commit"])[:12]
    reason = gh_pages.get("error") or gh_pages.get("last_error")
    if reason:
        out["error"] = str(reason)[:300]
    if gh_pages.get("transport_probe"):
        out["transport_probe"] = gh_pages["transport_probe"]
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
    if td is not None and td.get("failed"):
        # v0.45.363：点名失败的标的与原因——BRK-B 恒 404 曾只活在 WARNING 日志里
        td_s += f"/失败{td.get('failures', '?')}(" + ",".join(
            f"{t}:{r}" for t, r in sorted(td["failed"].items())) + ")"
    cb_s = "—" if cb is None else f"抓取{cb.get('fetches', '?')}/命中{cb.get('hits', '?')}"
    hg = c.get("hv_gap")
    hg_s = ""
    if hg and (hg.get("degraded") or hg.get("repaired") or hg.get("filled") or hg.get("check_errors")):
        # 只在出过事时才占摘要行的位置：缺口已重取修好 / 第二源补齐 / 仍缺而置空 / 校验器自身出错，都点名。
        # v0.45.387：被第二源补齐的标的也带着 `critical`（补之前的缺口），所以名字后要标「已补」，
        # 否则读起来像是置空了；没补成的把原因也带上（td_unavailable / ratio_mismatch …）。
        def _tag(g):
            base = "+".join(g.get("critical") or [])
            if g.get("filled"):
                return base + "已补"
            if g.get("unfilled"):
                return base + "[" + ";".join(u.split(":", 1)[-1] for u in g["unfilled"][:1]) + "]"
            return base
        bad = ",".join(f"{t}:{_tag(g)}" for t, g in sorted((hg.get("tickers") or {}).items())
                       if g.get("critical"))
        hg_s = (f" | 日线缺口 修复{hg.get('repaired', '?')}/TD补{hg.get('filled', 0)}/置空{hg.get('degraded', '?')}"
                f"/校验出错{hg.get('check_errors', '?')}" + (f"({bad})" if bad else ""))
    os_ = c.get("options_snapshot")
    os_s = "—" if os_ is None else (
        f"写入{os_.get('writes', '?')}/命中{os_.get('hits', '?')}"
        f"/会话不符弃用{os_.get('session_mismatch', '?')}份/盘中快照命中{os_.get('hits_before_close', '?')}份")
    pg = c.get("portfolio_greeks")
    pg_s = ""
    if pg and pg.get("error"):
        pg_s = f" | Greeks 异常中断({str(pg['error'])[:80]})"          # v0.45.442
    elif pg and pg.get("hedge_undecided") and not (pg.get("n_stale") or pg.get("n_quote_stale")
                                                  or (pg.get("spy") or {}).get("stale")
                                                  or pg.get("execution_blocked")):
        # v0.45.435：不是陈旧、是缺——对冲决定做不出来（两源取不到价 / 缺报价 / 缺 β / 缺 NAV）
        pg_s = " | Greeks 数据不全不对冲(" + ("；".join(pg.get("gaps") or []) or str(pg["hedge_undecided"])) + ")"
    elif pg and (pg.get("n_stale") or pg.get("n_quote_stale") or (pg.get("spy") or {}).get("stale")
                 or pg.get("execution_blocked")):
        # v0.45.423：同 hv_gap，只在出过事时占位——组合 Greeks 有价不属于当天那一场（不进 $Delta、不对冲）
        names = ",".join(dict.fromkeys(f"{x.get('ticker')}@{x.get('session')}" for x in pg.get("stale") or []))
        spy = pg.get("spy") or {}
        pg_s = (f" | Greeks 陈旧价 {pg.get('n_stale', '?')} 行" + (f"({names})" if names else "")
                + (f"/SPY@{spy.get('session')}" if spy.get("stale") else "")
                + (f"/报价错场 {pg['n_quote_stale']}" if pg.get("n_quote_stale") else "")
                + ("/拒绝成交" if pg.get("execution_blocked") else "")
                + (("/另缺 " + "；".join(pg["gaps"])) if pg.get("gaps") else ""))
    pp = c.get("paper_portfolio")
    if pp and pp.get("error"):
        pg_s += f" | 纸面组合异常中断({str(pp['error'])[:80]})"        # v0.45.448
    lf = c.get("ledger_io")
    if lf and lf.get("n"):                                              # v0.45.452
        where = ",".join(dict.fromkeys(Path(str(i.get("path"))).name for i in lf.get("items") or []))
        pg_s += f" | 账本读写失败 {lf['n']} 次({where})"
    return ("耗时 " + " | ".join(parts) +
            f" ‖ yfinance {yf_s} | TwelveData {td_s} | CBOE {cb_s} | 期权快照 {os_s}" + hg_s + pg_s)
