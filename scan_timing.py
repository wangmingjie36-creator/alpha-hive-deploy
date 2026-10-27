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

    `history_fill`（v0.45.473）：`data_pipeline.MultiSourceFetcher` 唯一出口的历史字段回落——按出口
    （哪个源胜出 / 过期缓存 / CBOE 盘中陈旧价 / 全源失败）数缺了几项、回落补上几项，外加最近一次仍缺的标的。
    此前三个出口直接把 None 交出去，连「今天走了哪个出口」都只在 warning 日志里。v0.45.625 起另带 `at_scoring`：
    评分用的那份行情取完那一刻（扫描预取结束）的同一组统计——告警与摘要行按它判，评分之后的重取另列。

    `stock_fetch_legacy`（v0.45.639）：`swarm_agents.cache._fetch_stock_data` 因 data_pipeline 不可用（导入期 / 取数途中的
    ImportError）改走旧回落的次数、阶段、标的与原因，外加评分那一刻的快照 `at_scoring`（同 `history_fill`）。走到它 = 那些标的的行情
    绕开 data_pipeline（无未完成 K 线护栏、无出口回落链、不进上面的 `history_fill`）；此前不留一行日志。count > 0 ⇒ alert_manager P1，
    影响按 `at_scoring` 写（评分用的是不是降级取数）。

    `event_rows`（v0.45.645）：`price_metrics.drop_event_only_rows` 去掉的 yfinance 公司行动空行（开高低收全空、量 0、有分红 / 拆股）——
    调了几次、去掉几行、其中几行是首行（`period="1mo"` 窗口外的边界日漏进来的分红，正常）、几行是末行（当天没有 K 线，那一路行情整体当取不到）、
    不在首行的总数与前 20 条明细（yfinance 只给越界事件补空行 ⇒ 实际是末行；中间位置是防御，见 `price_metrics` 模块说明）。外加评分那一刻的快照 `at_scoring`（同 `history_fill`）。
    不在首行 ⇒ alert_manager P2。

    `yf_daily`（v0.45.652；v0.45.658 起含 `yf.download` 读者）：行情三字段以外的 yfinance 日线读者经唯一入口去掉的公司行动空行——
    调了几次、看过几份单票日线、去掉几行、首段（窗口外边界日，正常）/ 末段分两种：「最近一场已收盘的 K 线缺了」（`last_row`，
    refuse 的调用方当取不到）与「今天那场还没收」（`last_row_pending`，盘前跑，照用）/ 中间（yfinance 1.2.0 不会产生）、
    日历判不了的份数、「缺了」+ 中间的总数与前 20 条明细（带调用点名 `source`）、逐调用点计数。缺了 / 中间 ⇒ alert_manager P2。

    `price_nonfinite`（v0.45.653）：`price_metrics.history_price_metrics` 把算出非有限数的动量 / 量比改成 None 的次数与明细（真 K 线缺收盘 /
    缺量，2026-08-28 型；值交给出口回落）。外加评分那一刻的快照 `at_scoring`。评分时就有 ⇒ alert_manager P2。

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
                                      "ledger_io": None, "history_fill": None,
                                      "strangle_shadow": None, "stock_fetch_legacy": None,
                                      "event_rows": None, "yf_daily": None,
                                      "price_nonfinite": None}
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
    try:
        import data_pipeline
        # v0.45.473：取数出口的 momentum_5d / volume_ratio 回落（各出口缺几项、补上几项、仍缺哪些标的）；
        # 本进程没解析过任何标的 ⇒ None。v0.45.625：评分那一刻的快照 at_scoring.still_missing 非空 ⇒ alert_manager P2，
        # 之后的重取才缺的 ⇒ INFO
        out["history_fill"] = data_pipeline.history_fill_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("history_fill stats 不可得: %s", e)
    try:
        import straddle_strangle_shadow
        # v0.45.586：影子宽跨记录（信号行 / 账本开仓 / 账本平仓三处只记录的挂接点）在源头登记的计数；
        # `errors` > 0（记录没写成）⇒ alert_manager P2。不可导入 ⇒ None（不写 {}：空 dict 会被读成「零故障」）
        out["strangle_shadow"] = straddle_strangle_shadow.stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("strangle_shadow stats 不可得: %s", e)
    try:
        from swarm_agents import cache as _swarm_cache
        # v0.45.639：data_pipeline 坏了时取数改走 cache.py 旧回落的次数（此前一个 `except ImportError: pass` 吞掉、不留痕）；
        # count > 0 ⇒ alert_manager P1。不可导入 ⇒ None（不写 {}：空 dict 会被读成「零次」）
        out["stock_fetch_legacy"] = _swarm_cache.legacy_fallback_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("stock_fetch_legacy stats 不可得: %s", e)
    try:
        import price_metrics
        # v0.45.645：取数后第一步去掉的 yfinance 公司行动空行；不在首行的 ⇒ alert_manager P2。不可导入 ⇒ None
        out["event_rows"] = price_metrics.event_row_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("event_rows stats 不可得: %s", e)
    try:
        import yf_daily
        # v0.45.652：行情三字段以外的日线读者经唯一入口去掉的公司行动空行；末行 / 中间 ⇒ alert_manager P2。不可导入 ⇒ None
        out["yf_daily"] = yf_daily.stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("yf_daily stats 不可得: %s", e)
    try:
        import price_metrics
        # v0.45.653：算出非有限数、改给 None 的动量 / 量比（真 K 线缺收盘 / 缺量）；评分时就有 ⇒ alert_manager P2。不可导入 ⇒ None
        out["price_nonfinite"] = price_metrics.nonfinite_stats()
    except Exception as e:  # noqa: BLE001
        _log.debug("price_nonfinite stats 不可得: %s", e)
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
    adj = (td or {}).get("adjusted") or {}
    if adj.get("fetches"):                                              # v0.45.562：复权日线（波动率回落）另一份缓存
        td_s += f"/复权请求{adj['fetches']}"                            # 的请求单列——它的失败也算在下面的「失败」里
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
        f"/会话不符弃用{os_.get('session_mismatch', '?')}份/盘中快照命中{os_.get('hits_before_close', '?')}份"
        # v0.45.462：只在出过事时占位——现价不可得（不再拿行权价中位数 / 145 冒充）、出口遇到非有限数
        + (f"/现价不可得{os_['stock_price_unavailable']}次" if os_.get("stock_price_unavailable") else "")
        + (f"/非有限数记None {os_['nan_sanitized']}个" if os_.get("nan_sanitized") else "")
        # v0.45.471：当日 ATM IV 无效且无缓存 ⇒ iv_current 记 None（不再兜底成 25.0）
        + (f"/IV不可得{os_['iv_unavailable']}次" if os_.get("iv_unavailable") else "")
        # v0.45.482：yfinance 链里 Yahoo 求解失败的 IV（置 0 的合约数 / ATM 窗口被失败值主导的次数）
        + (f"/Yahoo失败IV{os_['iv_placeholder_contracts']}个" if os_.get("iv_placeholder_contracts") else "")
        + (f"/失败IV主导{os_['iv_placeholder_majority']}次" if os_.get("iv_placeholder_majority") else "")
        # v0.45.512：yfinance 链 OI 陈旧（这一场没发布）⇒ P/C / total_oi / 主链 GEX 记不可得的标的数
        + (f"/OI陈旧链{os_['oi_stale_chains']}只" if os_.get("oi_stale_chains") else "")
        # v0.45.516：P/C 算不出（此前是假中性 1.0）
        + (f"/P/C不可得{os_['pc_unavailable']}只" if os_.get("pc_unavailable") else ""))
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
    dhp = (pg or {}).get("delta_hedge") if isinstance(pg, dict) else None
    if isinstance(dhp, dict) and dhp.get("mode") == "paper" and (dhp.get("n_undecided") or dhp.get("n_blocked")):
        # v0.45.526：逐票 Delta 对冲（纸面成交）有标的决定做不出来 / 拒绝成交——shadow 下只进审计，不占位
        pg_s += (f" | 逐票对冲未决{dhp.get('n_undecided', 0)}只/拒绝成交{dhp.get('n_blocked', 0)}只("
                 + ",".join(u.get("ticker", "?") for u in dhp.get("undecided") or []) + ")")
    pp = c.get("paper_portfolio")
    if pp and pp.get("error"):
        pg_s += f" | 纸面组合异常中断({str(pp['error'])[:80]})"        # v0.45.448
    elif pp and pp.get("skipped"):                                      # v0.45.535
        pg_s += f" | 纸面组合整天跳过(持仓日线全断 {len(pp.get('ohlc_unavailable') or [])} 只)"
    lf = c.get("ledger_io")
    if lf and lf.get("n"):                                              # v0.45.452
        where = ",".join(dict.fromkeys(Path(str(i.get("path"))).name for i in lf.get("items") or []))
        pg_s += f" | 账本读写失败 {lf['n']} 次({where})"
    hf = c.get("history_fill")
    # v0.45.625：按评分用的那份行情取完那一刻的快照报（`at_scoring`）；之后的重取（报告阶段等）另写，不混进来
    scored = (hf or {}).get("at_scoring")
    sm = ((scored if isinstance(scored, dict) else hf) or {}).get("still_missing") or {}
    if sm.get("momentum") or sm.get("volume") or sm.get("volatility"):  # v0.45.473；波动率 v0.45.557
        pg_s += (f" | 回落后仍缺 动量 {len(sm.get('momentum') or [])} 只"
                 f"/量比 {len(sm.get('volume') or [])} 只"
                 f"/波动率 {len(sm.get('volatility') or [])} 只")
    if isinstance(scored, dict):
        after = (hf or {}).get("still_missing") or {}
        later = {f: set(after.get(f) or []) - set(sm.get(f) or []) for f in ("momentum", "volume", "volatility")}
        if any(later.values()):
            pg_s += (f" | 评分后重取仍缺 动量 {len(later['momentum'])} 只"
                     f"/量比 {len(later['volume'])} 只/波动率 {len(later['volatility'])} 只")
    lg = c.get("stock_fetch_legacy")
    if lg and lg.get("count"):                                          # v0.45.639
        _sc = lg.get("at_scoring")
        _sc_s = f"评分时 {_sc.get('count', 0)} 次" if isinstance(_sc, dict) else "评分时未知"
        pg_s += f" | 行情走 cache.py 旧回落 {lg['count']} 次({len(lg.get('tickers') or [])} 只; {_sc_s}; data_pipeline 不可用)"
    ev = c.get("event_rows")
    if ev and ev.get("dropped"):                                        # v0.45.645
        pg_s += (f" | 日线公司行动空行去掉 {ev['dropped']} 行"
                 f"(非首行 {ev.get('elsewhere_total', 0)}, 其中末行 {ev.get('last_row', 0)})")
    yd = c.get("yf_daily")
    if yd and yd.get("dropped"):                                        # v0.45.652
        pg_s += (f" | 其他日线读者去掉公司行动空行 {yd['dropped']} 行"
                 f"(首段 {yd.get('first_row', 0)}, 最近一场缺了 {yd.get('last_row', 0)} 份"
                 f", 今天未收 {yd.get('last_row_pending', 0)} 份, 中间 {yd.get('middle', 0)})")
    nf = c.get("price_nonfinite")
    if nf and (nf.get("momentum_5d") or nf.get("volume_ratio")):        # v0.45.653
        pg_s += f" | 行情非有限改缺失 动量 {nf.get('momentum_5d', 0)} 次/量比 {nf.get('volume_ratio', 0)} 次"
    return ("耗时 " + " | ".join(parts) +
            f" ‖ yfinance {yf_s} | TwelveData {td_s} | CBOE {cb_s} | 期权快照 {os_s}" + hg_s + pg_s)
