#!/usr/bin/env python3
"""卖权行权价选择器 · 账本层（v0.45.333）：run_for_date / record / settle / assess。

只记录与结算，**不开仓、不进评分、不上网站**。每个 (date, ticker, tenor) 一行：当天的水平地图
摘要（env）、环境路由（route）、delta 梯子（ladder），到期后由 `settle` 补上到期收盘。
检验「GEX 环境路由是否挑得出更差的卖权环境」的方法学冻结在
`experiments/sell_strike_routing_prereg.md`（本模块 `PREREG` 与它同值，测试钉住）。

依赖只向下：本模块 import `sell_strike_candidates` / `sell_strike_levels`；
不 import 任何评分组件（蜂群蒸馏、GEX 政体调整、评分权重）与旧 GEX / Greeks 引擎
（`config.WATCHLIST` 只在 CLI 缺省票池时惰性读取）。火墙测试按名字扫源码，这里刻意不写出那些模块名。

─── 路径：不写模块级 BASE_DIR ────────────────────────────────────────────
`_state_dir()` 每次调用时解析 `PATHS.sell_strike_state`。四个老账本的模块级
`BASE_DIR = PATHS.home` 写法已在 `tests/test_paths_not_frozen_at_import.py::KNOWN` 登记为
存量违规（pytest 收集期 import 时就冻成 checkout 根，`_isolate_env` 追不上）。

─── 分片：按「记录月份」 ─────────────────────────────────────────────────
`<state>/<tenor>/<YYYY-MM>.jsonl`。每行 ~5–8KB（梯子 20 条腿），60 行/日，不分片一年 ~100MB
且每天整表原子重写。月度 / 周度两档**分目录**记（用户要求账本分开记）。

─── 结算：只认到期日那根 K 线 ────────────────────────────────────────────
收盘价必须是**日期 == expiry** 的那根日线，缺口 0。**不用** `options_paper_leg._default_close`
的「5 个日历日内最后一根」：到期当天 14:00 PT 扫描时当日 K 线还没有（`_drop_forming_bar` 会丢），
容差会悄悄用前一日收盘结算。所以 `expiry < as_of` 才结算（到期次日起）。

─── 盲化：代码只保证「不算、不显示」（2026-09-24 最终评审如实改写）──────────
`assess()` 在 status != "ready" 时，返回值里**没有**任何效应量 / p 值键——不是「算了不显示」，
是根本不算。**这就是盲化的全部**：route flag 与现价是本产品每天的输出、收盘价是公开行情，
从任何出口（按日期读账本的 MCP、本地报告、账本 jsonl、公开行情）自己把每个单位的结果拼出来
都拼得出——那样做 = 偷看 = 协议变更（预注册文档 §7 / §10）。MCP 行视图在冻结前剔除结算字段
（`sell_strike_report.BLINDED_ROW_FIELDS`）只是少递一把刀，**不等于**盲化。

─── 检验只跑一次：首次就绪即冻结，唯一写者 = 日报钩子（登记前定稿）──────────
`assess(freeze=True)` 从账本读行、首次满足就绪闸时，把检验结果原子写进 `<state>/<tenor>/prereg_result.json`
（先到者赢：`os.link` 不覆盖已存在的文件），此后只读这份、**不重算**。否则日报钩子每天经本地报告
调一次 assess ⇒ 每天重跑一次检验 ⇒ 可选停止（评审模拟：名义 α_each .0125，40 天里任一天拒绝的比例
.060，膨胀约 5 倍）。**只有日报钩子**（`write_local_report(freeze=True)` → `render_markdown`）传
freeze=True；MCP、CLI 缺省 freeze=False——就绪但未冻结时只显示「已就绪，等待日报冻结」，不跑检验、不写文件
（2026-09-24：此前 MCP 也会首次冻结，与工具的 readOnly 声明矛盾、且一次不可逆的写有两个写者）。
冻结的 ready_date = min(as_of, 数据视界)，写路径（run_for_date / settle / 冻结 / CLI）拒绝晚于
PDT 今天的日期：手误写进一个未来的 ready_date 是永久的（改它 = 删冻结文件 = 协议变更）。
`rows=` 注入（测试 / 探针）不读也不写冻结文件。

─── 并发写者：分片读-改-写一律持 tenor 目录锁（2026-09-26 二次检查）─────────────────
手动补跑与定时扫描、CLI `--run` 与 `--settle` 可能同时写同一个分片。原实现整片读 → 改 → 整片重写、
不加锁：后写者用自己的旧快照覆盖先写者 ⇒ 对方刚记的行 / 刚补的结算**静默消失**（探针实测）。
现在 `record` 与 `settle` 的每次读-改-写都在 `_tenor_lock`（对 `<state>/<tenor>/` 目录 fd 加
`fcntl.flock` 排他锁）里做；settle 的取 K 线（网络 I/O）在锁**外**，套用结算时在锁内重读分片、
按 `_row_key` 逐行套用，行已被别人改过（不再 pending / 到期日变了）⇒ 以对方为准、计 `skipped_concurrent`。

─── 版本戳：协议变更前记的行默认不进检验（预注册文档 §10）─────────────────────────
每行带 `schema_version` / `component_versions`（levels / candidates / route_rule / prereg）/
`route.rule_version`。任一项与现行代码不符 ⇒ 该行不进检验（§10「截断，默认不进」），
计入 `progress.n_rows_version_excluded`。声明「不截断」的修订须在 `_version_mismatch` 里显式放行旧戳。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

import cboe_options
import sell_strike_candidates as C
import sell_strike_levels as L
from hive_logger import PATHS, get_logger, pdt_today

_log = get_logger("sell_strike_ledger")

LEDGER_SCHEMA_VERSION = 1

# 预注册常量：与 experiments/sell_strike_routing_prereg.md 的「机器可读常量」块逐项同值，
# tests/test_sell_strike_ledger.py::TestPreregPinned 解析文档核对（两个方向：文档不许漏键、不许多键）。
# 改任何一项 = 协议变更（记修订号、声明截断），不是调参。
PREREG = {"version": 1, "registered": "2026-09-23",
          "primary_rung": 0.20, "sides": ("put", "call"), "tenors": ("monthly", "weekly"),
          "metric": "pnl_over_credit", "n_tests": 4, "alpha_family": 0.05, "alpha_each": 0.0125,
          "n_perm": 5000, "seed": 20260923,
          "min_independent_per_tenor": 60, "min_distinct_expiries": 12, "min_per_group": 10,
          # min_per_group 只数**信息块**（同一记录日里既有 flag 又有 normal 的块）里的单位（登记前定稿，
          # 2026-09-24 最终评审）：分块置换只从这种块取信息，全天同一标签的块对零分布没有贡献。按全体计数时，
          # 「两周全市场负 gamma、其余十周无人被 flag」的账本闸门全过、信息块 0、p 恒 1——而检验只跑一次，
          # 这个没有功效的结果就被冻结了。`_gate_counts` 按本键取计数口径（值改了而代码不认 ⇒ KeyError）。
          "per_group_scope": "informative_blocks",
          # 这些 underlying_price_source 的行不进检验：判据是「报价不是已收盘那一场的收盘后快照」。
          # `cboe_stale_intraday` = 收盘后读到的仍是盘中生成的 CBOE 文件（09-22 生产 30 只里 4 只；
          # 2026-09-24 最终评审）；`cboe_intraday` = 盘中就读了（例如盘中手动跑日报，编排器的 1330 闸管不到；
          # 2026-09-26 delta 评审）——同一份盘中报价只因读取时刻不同就一进一出检验，所以两种都排除，
          # `_eligible_except_earnings` 另外要求 `session_live is not True`。陈旧标签取 cboe_options 的常量；
          # `cboe_intraday` 在 cboe_options 里是 `official_price` 的返回字面量，没有常量可取。
          "excluded_underlying_price_sources": (cboe_options.STALE_INTRADAY_SOURCE, "cboe_intraday"),
          # ⚠️ SPEC 字面是 `earnings_status == "none"`。偏离理由：`after_expiry`（财报在到期日**之后**）
          # 不落在期权存续期内，不污染到期结果；把它也排除掉，按 30 票的财报节奏会砍掉大半独立单位，
          # 就绪闸在可预期的时间内够不着。排除的是「财报冲突（before_expiry）与判不了（unknown）」，
          # 两臂同等——与 SPEC I.8「财报冲突/unknown 排除两臂同等」的表述一致。
          "eligible_earnings_status": ("none", "after_expiry")}

# pending_overdue_days：到期已超过这么多个日历日仍 pending ⇒ run_for_date 打 warning、报告显示。
# 取不到 K 线（Twelve Data key 过期 / 配额耗尽）不计 attempts（设计如此），放弃闸要到取数窗口外
# （约 6.5 个月）才触发——这期间「结算 0」与「没有行到期」在日志里一模一样，只有这个数分得开。
# settle_max_attempts 数的是**天**不是运行次数（2026-09-26 二次检查）：同一个 as_of 至多计一次
# （`settle_last_attempt_on`），K 线源最后一根还没越过到期日（源没追上）不计——否则同一天手动补跑
# 五次、或一个滞后的 K 线源，就把行永久放弃了。
#
# 取数熔断 + 时间预算（2026-09-26 二次检查）：run_for_date 跑在生产扫描**之内**、save_report / 部署
# **之前**，逐票顺序取数且取数层自带重试。DNS 故障实测多花 63 s；若是连接超时，30 票 ×（3×15 s + 2.1 s）
# ≈ 1400 s，可能把整个扫描顶出步骤时限。所以：
#   · fetch_breaker_consecutive_failures：连续这么多只票取数失败、且原因是网络类（`_BREAKER_REASONS`
#     或 `exception:*`）⇒ 其余票不再取，记 `fetch_skipped_breaker`；任何一只取到（或非网络类失败，
#     说明网络是通的）就清零；
#   · fetch_time_budget_sec：从取数循环开始计，超了 ⇒ 其余票不再取，记 `fetch_skipped_time_budget`。
#   两者都计进 fetch_reasons、打 WARNING；跳过的票记 unavailable（同日已 recorded 的旧行照「不降级」保留）。
# ledger_lock_timeout_sec：分片读-改-写等 tenor 目录锁的上限（秒）；超时 ⇒ TimeoutError
# （run_for_date 按档收进 errors + warning，CLI 退出码 1）——锁不住就不写，不退回无锁写。
CONFIG = {"settle_max_attempts": 5, "settle_window_slack_days": 15, "pending_overdue_days": 5,
          "fetch_breaker_consecutive_failures": 3, "fetch_time_budget_sec": 600,
          "ledger_lock_timeout_sec": 60}

# 熔断只数「网络 / 取数层整体不可用」类失败（另加 `exception:*`）。stale_vintage / vintage_mismatch 等
# 说明 payload 取到了、只是内容不对——网络是通的，不计、并清零连续计数。
_BREAKER_REASONS = ("payload_unavailable",)
FETCH_SKIPPED_BREAKER = "fetch_skipped_breaker"
FETCH_SKIPPED_TIME_BUDGET = "fetch_skipped_time_budget"

TENORS = tuple(C.TENORS)          # ("monthly", "weekly")
SIDES = ("put", "call")

# 只由 settle 写的字段。record 同日重写时按 (ticker, tenor, date) 键原样搬运——
# 新增结算字段务必同步加进来，否则同日重跑会把它悄悄抹掉（vrp_signal v0.45.104 修的就是这个）。
# settle_give_up_on：放弃那天的 as_of（assess(as_of) 据此判「那天是否已放弃」，同 settled_on）。
# settle_last_attempt_on：最近一次**计了次**的失败尝试的 as_of（同一天至多计一次，见 CONFIG 注释）。
_SETTLEMENT_FIELDS = ("settle_status", "expiry_close", "expiry_close_date", "expiry_close_source",
                      "settled_on", "settle_attempts", "settle_give_up_reason", "settle_give_up_on",
                      "settle_last_attempt_on")
# assess(as_of) 把「as_of 之后才结算 / 放弃」的行当 pending 时，副本上清掉的字段
# （settle_attempts / settle_last_attempt_on 不清：它们不进任何判定）。
_OUTCOME_FIELDS = ("expiry_close", "expiry_close_date", "expiry_close_source", "settled_on",
                   "settle_give_up_reason", "settle_give_up_on")

_SHARD_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
# 首次就绪时冻结的检验结果（每个 tenor 目录一份；不匹配 _SHARD_RE，不会被当成分片读）
PREREG_RESULT_NAME = "prereg_result.json"
# 预注册度量里「不可观测」的键：assess 未就绪时返回值里一个都不许出现（结构性盲化）。
BLINDED_KEYS = frozenset({"observed", "p", "p_value", "decision", "effect", "mean_flagged",
                          "mean_normal", "null_mean", "test"})


# ─────────────────────────────── 路径（调用时求值）

def _state_dir(state_dir=None) -> Path:
    return Path(state_dir) if state_dir else PATHS.sell_strike_state


def _shard(tenor: str, date_str: str, state_dir=None) -> Path:
    return _state_dir(state_dir) / tenor / f"{date_str[:7]}.jsonl"


@contextlib.contextmanager
def _tenor_lock(tenor: str, state_dir=None):
    """该 tenor 分片读-改-写的排他锁：对 `<state>/<tenor>/` **目录**的 fd 加 `fcntl.flock(LOCK_EX)`。

    为什么锁目录而不是另建 `.lock` 文件：私有备份 `data_backup.export.copy_state_dir` 只拷文本后缀，
    别的文件记进 `skipped`（`test_private_backup_covers_ledger_and_report` 断言卖权目录零跳过）——
    多一个锁文件就是「每天出现在备份跳过清单里的噪声」。目录 fd 的 flock 在 macOS / Linux 本地盘上
    与文件同语义（同进程两个 fd 也互斥），原子替换（tmp + os.replace）不影响目录上的锁。
    等待超过 `CONFIG["ledger_lock_timeout_sec"]` ⇒ TimeoutError（不退回无锁写：那正是要堵的丢数据）。
    fd 关闭即释放锁，进程崩了也不会留下死锁。"""
    d = _state_dir(state_dir) / _check_tenor(tenor)
    d.mkdir(parents=True, exist_ok=True)
    timeout = float(CONFIG["ledger_lock_timeout_sec"])
    fd = os.open(str(d), os.O_RDONLY)
    try:
        t0 = time.monotonic()
        waited = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - t0 >= timeout:
                    raise TimeoutError(f"卖权账本 {d} 的写锁等了 {timeout:.0f}s 仍被占用（另一个写者卡住了？）"
                                       "——本次不写，免得覆盖对方") from None
                waited = True
                time.sleep(0.02)
        if waited:
            _log.info("卖权账本 %s 写锁等待 %.2fs（另一个写者在写）", d, time.monotonic() - t0)
        yield d
    finally:
        os.close(fd)


# ─────────────────────────────── 小工具

def _num(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(x) -> Optional[float]:
    f = _num(x)
    return f if (f is not None and f > 0) else None


def _check_date(s) -> str:
    """YYYY-MM-DD，否则 ValueError（分片名取前 7 位，非法日期会写进一个错的分片）。"""
    if not isinstance(s, str) or len(s) != 10:
        raise ValueError(f"日期必须是 YYYY-MM-DD，收到 {s!r}")
    dt.date.fromisoformat(s)
    return s


def _today() -> str:
    """PDT 今天（`hive_logger.pdt_today`，与日报 `date_str` 缺省同一时区）。测试替换这一个点来拨钟。"""
    return pdt_today()


def _check_not_future(s, what: str) -> str:
    """写路径的日期闸：合法 YYYY-MM-DD 且**不晚于** PDT 今天，否则 ValueError。

    为什么写路径要拦：`--settle --date <未来>` 把 settled_on 盖成未来 ⇒ assess(as_of=今天) 把这些行当 pending；
    冻结时 ready_date 取了未来日期 ⇒ 到那天之前日报一直不给检验结果，而改它只能删冻结文件（= 协议变更）。
    只读的 assess（不冻结）不拦：看一个未来日期不写任何东西。"""
    _check_date(s)
    today = _today()
    if s > today:
        raise ValueError(f"{what}：日期 {s} 晚于今天（PDT {today}）——写路径不接受未来日期（手误？年份写错？）")
    return s


def _check_tenor(tenor: str) -> str:
    if tenor not in C.TENORS:
        raise ValueError(f"tenor 只接受 {TENORS}，收到 {tenor!r}")
    return tenor


def _weekdays_after(d0: str, d1: str) -> int:
    """(d0, d1] 之间的工作日数——交易日数的**上界**（同 vrp_signal._weekdays_after）。"""
    try:
        a, b = dt.date.fromisoformat(d0), dt.date.fromisoformat(d1)
    except (TypeError, ValueError):
        return 0
    if b <= a:
        return 0
    return sum(1 for i in range(1, (b - a).days + 1) if (a + dt.timedelta(days=i)).weekday() < 5)


def _clean(obj):
    """JSON 安全化：非有限浮点 → None；numpy 标量 → Python；tuple → list。
    写盘用 `allow_nan=False`，漏网的 NaN 当场抛，而不是写出一行别的解析器读不了的 JSON。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    return obj


def _load_shard(path: Path) -> Tuple[List[dict], List[str]]:
    """(行, 坏行原文)。坏行**不丢**：重写分片时原样保留在末尾，并在 assess 里计数——
    静默跳过再整表重写，等于把「读不懂」改写成「从没有过」。

    按**字节**读、逐行解码（2026-09-26 二次检查）：原实现以文本模式迭代，一个非 UTF-8 字节在 json 的 try
    **之外**抛 UnicodeDecodeError，整个 tenor 读不了（assess / settle / record 全挂）。现在解不了码的行
    也只是坏行：以 `surrogateescape` 解码保存，`_write_shard` 以同一方式编码写回 ⇒ 逐字节原样。"""
    if not path.exists():
        return [], []
    rows: List[dict] = []
    bad: List[str] = []
    n_undecodable = 0
    for raw in path.read_bytes().split(b"\n"):
        if not raw.strip():
            continue
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(raw.decode("utf-8", "surrogateescape"))
            n_undecodable += 1
            continue
        try:
            rec = json.loads(s)
        except ValueError:
            bad.append(s)
            continue
        if isinstance(rec, dict):
            rows.append(rec)
        else:
            bad.append(s)
    if bad:
        _log.warning("卖权账本分片 %s 有 %d 行无法解析（其中非 UTF-8 %d 行；已原样保留，不会被重写抹掉）",
                     path, len(bad), n_undecodable)
    return rows, bad


def _write_shard(path: Path, rows: List[dict], bad_lines: Iterable[str] = ()) -> None:
    """原子写（tmp + os.replace，照 vrp_signal._write_jsonl）。好行严格 UTF-8；坏行按 `_load_shard`
    的 `surrogateescape` 编回原始字节（非 UTF-8 的坏行逐字节原样）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            for r in rows:
                f.write((json.dumps(_clean(r), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
            for s in bad_lines:
                f.write(s.encode("utf-8", "surrogateescape") + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _shard_paths(tenor: str, state_dir=None) -> List[Path]:
    d = _state_dir(state_dir) / _check_tenor(tenor)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and _SHARD_RE.match(p.name))


def _row_key(r: dict) -> Tuple[str, str]:
    return str(r.get("date")), str(r.get("ticker"))


def _load_all(tenor: str, state_dir=None) -> Tuple[List[dict], int]:
    rows: List[dict] = []
    n_bad = 0
    for p in _shard_paths(tenor, state_dir):
        rs, bad = _load_shard(p)
        rows.extend(rs)
        n_bad += len(bad)
    rows.sort(key=_row_key)
    return rows, n_bad


def load_rows(tenor: str, state_dir=None) -> List[dict]:
    """该 tenor 全部分片的行，按 (date, ticker) 排序。"""
    return _load_all(tenor, state_dir)[0]


def rows_for_date(as_of: str, tenor: str, state_dir=None) -> List[dict]:
    """某日某 tenor 的行（只读当月分片）。"""
    rows, _bad = _load_shard(_shard(_check_tenor(tenor), _check_date(as_of), state_dir))
    return sorted((r for r in rows if r.get("date") == as_of), key=_row_key)


# ─────────────────────────────── 行构造（纯函数，compute_live 也用）

def _component_versions() -> dict:
    """现行代码的组件版本戳（写进每一行；`_version_mismatch` 拿它与行上的比）。
    `prereg` = `PREREG["version"]`：协议修订（§10）不一定动任何组件版本（例如改 α、改财报排除），
    没有这一项，修订前记的行就无从截断（2026-09-26 二次检查补）。"""
    return {"levels": L.LEVELS_SCHEMA_VERSION, "candidates": C.CANDIDATES_SCHEMA_VERSION,
            "route_rule": C.ROUTE_RULE_VERSION, "prereg": PREREG["version"]}


def _version_mismatch(r: dict) -> List[str]:
    """该行版本戳与现行代码不符的项（空表 = 全符，可进检验）。

    预注册文档 §10：协议变更须声明截断，「变更之前记录的行是否还进检验；**默认不进**」。行上的版本戳是
    「这行是按哪套规则记的」唯一的机器可读记录，所以默认就是：`schema_version`、`component_versions`
    的每一项、`route.rule_version`，任一项 ≠ 现行代码 ⇒ 不进检验。缺戳 = 不符（判不了是哪个版本记的）。
    schema_version 也算在内（保守）：账本布局改了，旧行的字段含义未必还是同一个。
    声明「不截断」的修订须在这里**显式**放行对应的旧戳（可见的代码改动 + 修订节记录），不许靠忘记检查放行。"""
    out: List[str] = []
    if r.get("schema_version") != LEDGER_SCHEMA_VERSION:
        out.append("schema_version")
    cv, cur = r.get("component_versions"), _component_versions()
    if not isinstance(cv, dict):
        out.append("component_versions")
    else:
        out += [f"component_versions.{k}" for k in sorted(set(cur) | set(cv)) if cv.get(k) != cur.get(k)]
    if (r.get("route") or {}).get("rule_version") != C.ROUTE_RULE_VERSION:
        out.append("route.rule_version")
    return out


def _blank_row(as_of: str, ticker: str, tenor: str) -> dict:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "component_versions": _component_versions(),
        "date": as_of, "ticker": ticker, "tenor": tenor,
        "status": None, "unavailable_reason": None,
        "vintage_date": None, "underlying_price": None, "underlying_price_source": None,
        # payload 自带的 last_trade_time 原文——报价**真正**是哪一刻的（`fetched_at` 是调用时刻，
        # 命中进程缓存时可差 4h，所以不记它）。
        "payload_last_trade_time": None,
        # 取数层的场次判定：True ⇒ 当天到期合约保留、现价取盘中价；False ⇒ 按 payload 那一场收盘取价。
        # 日报钩子在收盘后跑，应恒为 False——出现 True 说明那天是盘中跑的（报价不是收盘口径）。
        "session_live": None,
        "iv30": None,                     # payload 的 iv30，**百分数**原样（缺失 = None，不是 0）
        # 取数层丢了多少行（`fetch_cboe_raw_contracts` 的四个计数）。不落账本的话，OCC 格式变更
        # 丢掉 4% 的行（低于 5% 告警线）、恰好是 10Δ 远翼时，梯子 0.10 档悄悄空掉，事后无从查起。
        "fetch_counts": None,
        "expiry": None, "dte": None,
        "earnings_date": None, "earnings_status": None, "earnings_source": None,
        "env": None, "route": None, "ladder": None,
        "settle_status": None, "expiry_close": None, "expiry_close_date": None,
        "expiry_close_source": None, "settled_on": None, "settle_attempts": 0,
        "settle_give_up_reason": None, "settle_give_up_on": None, "settle_last_attempt_on": None,
    }


_FETCH_COUNT_KEYS = ("n_raw", "n_dropped_unparseable", "n_expired_excluded", "n_expiring_today_excluded")


def _fill_raw(row: dict, raw: Optional[dict]) -> None:
    if not isinstance(raw, dict):
        return
    row["vintage_date"] = raw.get("vintage_date")
    row["underlying_price"] = _num(raw.get("underlying_price"))
    row["underlying_price_source"] = raw.get("underlying_price_source")
    row["payload_last_trade_time"] = raw.get("payload_last_trade_time")
    sl = raw.get("session_live")
    row["session_live"] = sl if isinstance(sl, bool) else None
    row["iv30"] = _num(raw.get("iv30"))
    # 缺键记 None（「没测到」），不补 0（「测过为零」）
    row["fetch_counts"] = {k: (int(raw[k]) if _num(raw.get(k)) is not None else None)
                           for k in _FETCH_COUNT_KEYS}


def unavailable_row(as_of: str, ticker: str, tenor: str, reason: str,
                    raw: Optional[dict] = None) -> dict:
    row = _blank_row(as_of, ticker, tenor)
    _fill_raw(row, raw)
    row["status"] = "unavailable"
    row["unavailable_reason"] = str(reason or "unknown")
    return row


def build_tenor_row(raw: dict, level_map: dict, tenor: str, *, as_of: str,
                    earnings_info=None, ticker: Optional[str] = None,
                    r: float = L.RISK_FREE_RATE) -> dict:
    """一只票一个 tenor 的账本行（纯函数，不落盘）。

    窗口里没有到期日 ⇒ `unavailable`（`no_expiry_in_window`）。route 读 `level_map` 的
    `C.ROUTE_VIEW` 视图（le_45dte）；梯子取该 tenor 选中的到期日。
    route 判 unavailable（zero gamma 扫描没有可用合约）的行照样 `recorded`：梯子是有的，
    只是不进检验（`independent_units` 排除）。
    """
    _check_tenor(tenor)
    ticker = str(ticker if ticker is not None else raw.get("ticker"))
    contracts = raw.get("contracts") or []
    S = _pos(raw.get("underlying_price"))
    sel = C.select_expiry(contracts, tenor)
    if sel is None:
        return unavailable_row(as_of, ticker, tenor, "no_expiry_in_window", raw)
    expiry, dte = sel

    view = ((level_map or {}).get("views") or {}).get(C.ROUTE_VIEW) or {}
    zg = view.get("zero_gamma") or {}
    mj = view.get("majors") or {}
    counts = ((view.get("profile") or {}).get("counts") or {})
    rt = C.route(zg)
    ladder = C.build_ladder(contracts, S, expiry, r=r)

    row = _blank_row(as_of, ticker, tenor)
    _fill_raw(row, raw)
    row.update({
        "status": "recorded",
        "expiry": expiry,
        "dte": dte,
        "earnings_date": (earnings_info.get("earnings_date")
                          if isinstance(earnings_info, dict) else None),
        "earnings_status": C.classify_earnings(earnings_info, as_of, expiry),
        "earnings_source": (earnings_info.get("source") if isinstance(earnings_info, dict) else None),
        "env": {
            "view": C.ROUTE_VIEW,
            "curve_state": zg.get("curve_state"),
            "sign_at_spot": zg.get("sign_at_spot"),
            "total_at_spot": zg.get("total_at_spot"),
            "zg_nearest": zg.get("nearest"),
            "zg_below_pct": zg.get("zg_below_pct"),
            "zg_above_pct": zg.get("zg_above_pct"),
            "n_crossings": zg.get("n_crossings"),
            # 扫描实际用了几张合约：route 只读扫描曲线，而 gamma_source_counts 来自 strike_profile
            # （CBOE gamma，生产里大多有值）——扫描只剩两三张有 IV 的合约时，那一栏照样好看。
            # route 另有 MIN_SWEEP_CONTRACTS 门槛；这三个数让事后能查、能剔。
            "zg_n_contracts": zg.get("n_contracts"),
            "zg_excluded_no_iv": zg.get("excluded_no_iv"),
            "zg_excluded_no_oi": zg.get("excluded_no_oi"),
            "net_major_pos_strike": mj.get("net_major_pos_strike"),
            "net_major_neg_strike": mj.get("net_major_neg_strike"),
            "call_side_extreme_strike": mj.get("call_side_extreme_strike"),
            "put_side_extreme_strike": mj.get("put_side_extreme_strike"),
            "gamma_source_counts": counts.get("gamma_source"),
            "delta_source_counts": counts.get("delta_source"),
        },
        "route": rt,
        "ladder": ladder,
        "settle_status": "pending",
    })
    return row


# ─────────────────────────────── 记录

def _record_rows(as_of: str, tenor: str, rows: List[dict], state_dir=None) -> Tuple[List[dict], dict]:
    _check_date(as_of)
    _check_tenor(tenor)
    seen = set()
    for r in rows:
        if r.get("date") != as_of or r.get("tenor") != tenor:
            raise ValueError(f"record_rows({as_of}, {tenor}) 收到了别的日期/期限的行："
                             f"{r.get('date')}/{r.get('tenor')}/{r.get('ticker')}")
        if r.get("ticker") in seen:
            raise ValueError(f"record_rows 同一批里 {r.get('ticker')} 出现两次")
        seen.add(r.get("ticker"))

    path = _shard(tenor, as_of, state_dir)
    stats = {"kept_previous_recorded": 0, "carried_settlement": 0, "expiry_changed": 0}
    if not rows:            # 空批：什么都不改，也别为此造出一个空分片（也不建目录、不拿锁）
        existing, _bad = _load_shard(path)
        return sorted((r for r in existing if r.get("date") == as_of), key=_row_key), stats
    # 读-改-写整段持锁：锁外读、锁内写的话，读与写之间别的写者记的行 / 补的结算会被本次覆盖掉
    with _tenor_lock(tenor, state_dir):
        return _record_rows_locked(as_of, tenor, rows, path, stats)


def _record_rows_locked(as_of: str, tenor: str, rows: List[dict], path: Path,
                        stats: dict) -> Tuple[List[dict], dict]:
    """`_record_rows` 持锁的那一段：重读分片 → 按票合并 → 原子写回。"""
    existing, bad = _load_shard(path)
    old_today = {r.get("ticker"): r for r in existing if r.get("date") == as_of}
    others = [r for r in existing if r.get("date") != as_of]
    # 按票 upsert：本批没带的票，旧行保留（CLI 只跑一只票时不能把当天其余 29 只抹掉——
    # 那是比「unavailable 覆盖 recorded」更彻底的降级）。
    merged = dict(old_today)
    for new in rows:
        t = new.get("ticker")
        old = old_today.get(t)
        old_rec = old is not None and old.get("status") == "recorded"
        if old_rec and new.get("status") != "recorded":
            # 不变式：新的不可得不得降级旧的已记录（开机补跑 / 重跑撞上 CDN 陈旧时最常见）
            merged[t] = old
            stats["kept_previous_recorded"] += 1
            _log.info("[%s] %s %s 重跑不可得（%s），保留已记录的旧行", t, as_of, tenor,
                      new.get("unavailable_reason"))
            continue
        new = dict(new)
        if old_rec:
            if old.get("expiry") == new.get("expiry"):
                for f in _SETTLEMENT_FIELDS:
                    new[f] = old.get(f)
                stats["carried_settlement"] += 1
            else:
                # 同日重跑选中了不同的到期日（链在盘中 / 收盘后被 CBOE 刷新过）：旧结算字段属于旧到期日，
                # 搬过来会把一个到期日的收盘记到另一个到期日名下。
                stats["expiry_changed"] += 1
                _log.warning("[%s] %s %s 重跑选中的到期日由 %s 变为 %s，结算字段不搬运",
                             t, as_of, tenor, old.get("expiry"), new.get("expiry"))
        merged[t] = new
    today = sorted(merged.values(), key=_row_key)
    _write_shard(path, sorted(others + today, key=_row_key), bad)
    return today, stats


def record_rows(as_of: str, tenor: str, rows: List[dict], state_dir=None) -> List[dict]:
    """同日重写该 tenor、当月分片里 date==as_of 的行，返回写入后当天的全部行。

    不变式：
      · 新行 `unavailable` 而旧行 `recorded`（同票）⇒ 保留旧行（不得降级）；
      · 新旧都 recorded 且到期日相同 ⇒ 结算字段（`_SETTLEMENT_FIELDS`）从旧行原样搬运；
      · 本批没带的票，当天旧行保留（按票 upsert，不整日抹掉）。
    """
    return _record_rows(as_of, tenor, rows, state_dir)[0]


# ─────────────────────────────── 结算

def _default_bars(ticker: str, as_of: str) -> Tuple[Optional[List[dict]], str]:
    """(K 线, 来源)。照 `vrp_signal._default_bars`：Twelve Data 已配置 ⇒ 共享缓存入口
    `fetch_bars(t, SHARED_BARS_WINDOW, end_date=as_of)`；配置了但取不到 ⇒ None（下次再试，**不换源**：
    价格索引来自快照，与日线收盘不是一个口径）；未配置 ⇒ 本地价格索引。"""
    try:
        import twelve_data
        if twelve_data.is_configured():
            return (twelve_data.fetch_bars(ticker, twelve_data.SHARED_BARS_WINDOW, end_date=as_of),
                    "twelve_data")
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] Twelve Data 取 K 线失败: %s", ticker, exc)
        return None, "twelve_data"
    try:
        import price_history
        series = price_history.load_price_history(ticker, str(PATHS.cache_dir))
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] 本地价格索引不可读: %s", ticker, exc)
        return None, "price_history_index"
    return [{"date": d, "close": c} for d, c in series], "price_history_index"


def _fetch_window_bars() -> int:
    """取数窗口（根）。与 `twelve_data.SHARED_BARS_WINDOW` 同一个数——读它，不抄一份。"""
    import twelve_data
    return int(twelve_data.SHARED_BARS_WINDOW)


def _call_bars(bars_fn, ticker: str) -> Tuple[Optional[List[dict]], Optional[str]]:
    """注入的 bars_fn(ticker) 可返回 K 线列表（来源记 "injected"）或 (K 线, 来源)。"""
    res = bars_fn(ticker)
    if isinstance(res, tuple) and len(res) == 2:
        return res[0], res[1]
    return res, "injected"


def _close_on(bars: List[dict], day: str) -> Optional[float]:
    """日期**恰好 == day** 的那根收盘；没有 ⇒ None。缺口 0，不找「最近的一根」。"""
    for b in bars or []:
        if isinstance(b, dict) and str(b.get("date") or "")[:10] == day:
            return _pos(b.get("close"))
    return None


def _latest_bar_date(bars: List[dict]) -> Optional[str]:
    """K 线序列里最晚那根的日期（YYYY-MM-DD）；一根有日期的都没有 ⇒ None。"""
    ds = [str(b.get("date") or "")[:10] for b in bars or [] if isinstance(b, dict)]
    ds = [d for d in ds if len(d) == 10]
    return max(ds) if ds else None


def _give_up(row: dict, reason: str, as_of: str) -> None:
    row["settle_status"] = "give_up"
    row["settle_give_up_reason"] = reason
    row["settle_give_up_on"] = as_of
    _log.info("[%s] %s %s 的卖权账本行放弃结算：%s", row.get("ticker"), row.get("date"),
              row.get("tenor"), reason)


def _apply_settle_action(r: dict, act: tuple, as_of: str, max_attempts: int, stats: dict) -> bool:
    """在（锁内重读的）行上套用一个结算动作，返回是否改了行。动作只由 `_settle` 的前两段产出。"""
    kind = act[0]
    if kind == "give_up":
        _give_up(r, act[1], as_of)
        stats["gave_up"][act[1]] += 1
        return True
    if kind == "settle":
        r["expiry_close"] = act[1]
        r["expiry_close_date"] = r["expiry"]
        r["expiry_close_source"] = act[2]
        r["settled_on"] = as_of
        r["settle_status"] = "settled"
        stats["settled"] += 1
        return True
    # "attempt"：有 K 线、且已越过到期日，却没有到期日那一根——这才计一次失败，但同一个 as_of 至多一次
    # （as_of 不晚于上次计次那天 ⇒ 不计：同日重跑 / 倒填日期的补跑都不再消耗放弃额度）
    last = r.get("settle_last_attempt_on")
    if isinstance(last, str) and as_of <= last[:10]:
        stats["attempts_already_counted"] += 1
        stats["pending_remaining"] += 1
        return False
    r["settle_attempts"] = int(r.get("settle_attempts") or 0) + 1
    r["settle_last_attempt_on"] = as_of
    stats["attempts_incremented"] += 1
    if r["settle_attempts"] >= max_attempts:
        _give_up(r, "max_attempts_exhausted", as_of)
        stats["gave_up"]["max_attempts_exhausted"] += 1
    else:
        stats["pending_remaining"] += 1
    return True


def _settle(as_of: str, tenor: str, *, bars_fn=None, state_dir=None) -> dict:
    """三段（2026-09-26 二次检查改）：① 读快照、定出每行该做什么（不加锁：分片原子替换，读到的是某个完整版本）；
    ② 取 K 线（**锁外**：网络 I/O 可能很慢）；③ 持 tenor 锁**重读**分片、按 `_row_key` 逐行套用、写回。
    ③ 时行已不再 pending 或到期日变了（别的写者先动过）⇒ 以对方为准、计 `skipped_concurrent`。
    原实现在 ① 的快照上改、③ 整片写回，其间别人记的行 / 补的结算会被静默覆盖掉（探针实测）。"""
    _check_not_future(as_of, "settle")        # settled_on 会盖成 as_of
    _check_tenor(tenor)
    bars_fn = bars_fn or (lambda t: _default_bars(t, as_of))
    max_attempts = int(CONFIG["settle_max_attempts"])
    window = _fetch_window_bars() + int(CONFIG["settle_window_slack_days"])
    stats = {"settled": 0, "gave_up": Counter(), "attempts_incremented": 0, "attempts_already_counted": 0,
             "bars_unavailable": 0, "bars_lagging": 0, "not_yet_expired": 0, "pending_remaining": 0,
             "skipped_concurrent": 0, "pending": 0, "pending_overdue": 0}

    # ① 快照 + 不需要 K 线就能定的动作
    snap: Dict[Path, List[dict]] = {}
    plan: Dict[Path, Dict[Tuple[str, str], Tuple[object, tuple]]] = defaultdict(dict)  # 分片 → 行键 → (到期日, 动作)
    pending: Dict[str, List[Tuple[Path, dict]]] = defaultdict(list)
    for p in _shard_paths(tenor, state_dir):
        rows, _bad = _load_shard(p)
        snap[p] = rows
        for r in rows:
            if r.get("status") != "recorded" or r.get("settle_status") != "pending":
                continue
            exp = r.get("expiry")
            if not isinstance(exp, str) or len(exp) != 10:
                plan[p][_row_key(r)] = (exp, ("give_up", "no_expiry"))
                continue
            if not exp < as_of:
                # 到期当天不结算：当日 K 线要到收盘后才完整，`_drop_forming_bar` 会丢它
                stats["not_yet_expired"] += 1
                continue
            if _weekdays_after(exp, as_of) > window:
                plan[p][_row_key(r)] = (exp, ("give_up", "out_of_fetch_window"))
                continue
            pending[str(r.get("ticker"))].append((p, r))

    # ② 取 K 线（锁外），每票一次
    for ticker in sorted(pending):
        try:
            bars, src = _call_bars(bars_fn, ticker)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 卖权账本结算取 K 线失败: %s", ticker, exc)
            bars, src = None, None
        if not bars:
            # 取不到 K 线（限流 / 断网）不计次：那是暂时的；真取不到的由 out_of_fetch_window 兜底
            stats["bars_unavailable"] += len(pending[ticker])
            stats["pending_remaining"] += len(pending[ticker])
            continue
        latest = _latest_bar_date(bars)
        for p, r in pending[ticker]:
            close = _close_on(bars, r["expiry"])
            if close is not None:
                plan[p][_row_key(r)] = (r["expiry"], ("settle", close, src))
            elif latest is None or latest <= r["expiry"]:
                # K 线源最后一根还没越过到期日：不是「那一根缺了」，是「源还没追上」——同取不到 K 线，不计次
                # （仍计进 bars_unavailable，过期待结算的 warning 照响）
                stats["bars_unavailable"] += 1
                stats["bars_lagging"] += 1
                stats["pending_remaining"] += 1
            else:
                plan[p][_row_key(r)] = (r["expiry"], ("attempt",))

    # ③ 持锁重读、按行键套用、写回
    fresh: Dict[Path, List[dict]] = {}
    if plan:
        with _tenor_lock(tenor, state_dir):
            for p in sorted(plan):
                rows, bad = _load_shard(p)
                by_key = {_row_key(r): r for r in rows}
                changed = False
                for key, (exp, act) in plan[p].items():
                    r = by_key.get(key)
                    if (r is None or r.get("status") != "recorded" or r.get("settle_status") != "pending"
                            or r.get("expiry") != exp):
                        stats["skipped_concurrent"] += 1
                        continue
                    changed |= _apply_settle_action(r, act, as_of, max_attempts, stats)
                if changed:
                    _write_shard(p, rows, bad)
                fresh[p] = rows
    if stats["skipped_concurrent"]:
        _log.info("卖权账本结算 %s %s：%d 行在取 K 线期间已被别的写者改过，以对方为准", as_of, tenor,
                  stats["skipped_concurrent"])
    stats["pending"], stats["pending_overdue"] = _pending_counts(
        (r for p, rows in snap.items() for r in fresh.get(p, rows)), as_of)
    if stats["settled"] or stats["gave_up"]:
        _log.info("卖权账本结算 %s %s：结算 %d / 放弃 %s", as_of, tenor, stats["settled"],
                  dict(stats["gave_up"]))
    stats["gave_up"] = dict(stats["gave_up"])
    return stats


def _pending_counts(rows: Iterable[dict], ref_date: str) -> Tuple[int, int]:
    """(待结算行数, 其中到期已超过 `pending_overdue_days` 个日历日的行数)。"""
    days = int(CONFIG["pending_overdue_days"])
    ref = dt.date.fromisoformat(ref_date)
    n = overdue = 0
    for r in rows:
        if r.get("status") != "recorded" or r.get("settle_status") != "pending":
            continue
        n += 1
        try:
            exp = dt.date.fromisoformat(str(r.get("expiry"))[:10])
        except ValueError:
            continue          # 无到期日的行下一轮 settle 就 give_up（no_expiry）
        if (ref - exp).days > days:
            overdue += 1
    return n, overdue


def settle(as_of: str, tenor: str, *, bars_fn=None, state_dir=None) -> int:
    """给 `expiry < as_of` 的 pending 行填到期收盘，返回本次结算行数。

    扫全部月分片（上月记的行这月才到期）；每票最多取一次 K 线。
    收盘价只认日期 == expiry 的那根（缺口 0）。放弃闸：
      · `out_of_fetch_window` —— expiry 离 as_of 的工作日数已超过取数窗口 + `settle_window_slack_days`；
      · `max_attempts_exhausted` —— 拿到了**已越过到期日**的 K 线却没有到期日那一根，累计
        `settle_max_attempts` **天**（同一个 as_of 至多计一次，记 `settle_last_attempt_on`）。
    取不到 K 线（限流 / 断网）、或 K 线源最后一根不晚于到期日（源没追上，计 `bars_lagging`）都不计次。
    持 tenor 锁套用，取 K 线在锁外（见 `_settle`）。完整计数见 `run_for_date` 返回的 per_tenor。
    """
    return _settle(as_of, tenor, bars_fn=bars_fn, state_dir=state_dir)["settled"]


# ─────────────────────────────── 一日流程

def _default_fetch(ticker: str, *, as_of: Optional[str]):
    return cboe_options.fetch_cboe_raw_contracts(ticker, as_of=as_of)


def _monotonic() -> float:
    """取数时间预算用的时钟。测试替换这一个点来注入时钟（同 `_today`）。"""
    return time.monotonic()


def _is_network_failure(reason) -> bool:
    """熔断计数的失败：`_BREAKER_REASONS` 或 `exception:*`（取数层抛了——DNS / 连接超时都走这里）。"""
    return isinstance(reason, str) and (reason in _BREAKER_REASONS or reason.startswith("exception:"))


def _default_upcoming_fn() -> Callable[[str], Optional[dict]]:
    """CLI 缺省：惰性构造 EarningsWatcher（同 `earnings_vol_signal.scan` 的 `_upcoming`）。
    日报钩子传 `_earnings_date_from_swarm(<本轮蜂群结果>)`，不走这里。失败 ⇒ None ⇒ earnings "unknown"。"""
    box: list = []

    def _fn(ticker: str):
        try:
            if not box:
                from earnings_watcher import EarningsWatcher
                box.append(EarningsWatcher())
            return box[0].get_earnings_date(ticker)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 财报日获取失败（记 unknown）: %s", ticker, exc)
            return None
    return _fn


def run_for_date(as_of: str, *, tickers=None, upcoming_fn=None, state_dir=None,
                 fetch_fn=None, bars_fn=None) -> dict:
    """每票一次取数 → 一次 level_map → 两个 tenor 各一行 → 分别 record → 分别 settle。

    隔离：每个 tenor 的行构造、record、settle 各自 try/except——一档出错
    （行记 `unavailable` + `exception:<ExcType>`，或 per_tenor.errors）不连坐另一档。
    返回 `{"as_of", "n_tickers", "per_tenor": {tenor: {recorded, unavailable{reason:n},
    kept_previous_recorded, earnings_status{..}, price_source{..}, settled, gave_up, gave_up_reasons,
    settle_bars_unavailable, settle_bars_lagging, pending, pending_overdue, errors[]}}, "fetch_reasons",
    "fetch_aborted", "elapsed_sec"}`。

    `fetch_reasons` 每票一个结果：`ok` = 取数**与**水平地图都成功；取数失败记取数原因
    （`exception:<Exc>` = fetch_fn 抛了）；取数成功但 level_map 抛了记 `levels_error:<Exc>`
    ——此前这里先记了 ok，level_map 全挂时 0 行告警打出来的是 `{'ok': 30}`，自相矛盾。
    `pending_overdue` > 0（到期已超过 `pending_overdue_days` 个日历日仍待结算）⇒ warning。
    `price_source`：本批 recorded 行按 `underlying_price_source` 计数——`cboe_stale_intraday`
    （收盘后读到盘中文件）与 `cboe_intraday`（盘中就读了）的行照记、但不进检验
    （`PREREG["excluded_underlying_price_sources"]` + `session_live`），
    不数出来就没人知道样本在悄悄变少。as_of 晚于 PDT 今天 ⇒ ValueError（写路径日期闸）。

    **取数熔断 + 时间预算**（理由见 `CONFIG` 注释）：连续 `fetch_breaker_consecutive_failures` 只票网络类
    失败 ⇒ 其余票记 `fetch_skipped_breaker`；取数循环累计超过 `fetch_time_budget_sec` ⇒ 其余票记
    `fetch_skipped_time_budget`。两者都进 `fetch_reasons`、打 WARNING，返回值 `fetch_aborted` 给出原因
    （正常为 None）。任何一只取到、或非网络类失败，连续计数清零。
    """
    t0 = time.monotonic()
    _check_not_future(as_of, "run_for_date")
    if tickers is None:
        from config import WATCHLIST
        tickers = WATCHLIST
    tickers = sorted({str(t) for t in tickers})
    fetch_fn = fetch_fn or _default_fetch
    upcoming_fn = upcoming_fn or _default_upcoming_fn()
    breaker_n = int(CONFIG["fetch_breaker_consecutive_failures"])
    budget = float(CONFIG["fetch_time_budget_sec"])

    new_rows: Dict[str, List[dict]] = {t: [] for t in TENORS}
    fetch_reasons: Counter = Counter()
    consecutive_net_fail = 0
    aborted: Optional[str] = None
    t_fetch0 = _monotonic()
    for i, tk in enumerate(tickers):
        raw, fail = None, None
        if aborted is None:
            spent = _monotonic() - t_fetch0
            if consecutive_net_fail >= breaker_n:
                aborted = FETCH_SKIPPED_BREAKER
                _log.warning("卖权账本取数熔断：连续 %d 只票网络类失败（%s），其余 %d 只本轮不再取"
                             "（记 unavailable: %s；同日已记录的行不降级）", consecutive_net_fail,
                             {k: v for k, v in fetch_reasons.items() if _is_network_failure(k)},
                             len(tickers) - i, FETCH_SKIPPED_BREAKER)
            elif spent >= budget:
                aborted = FETCH_SKIPPED_TIME_BUDGET
                _log.warning("卖权账本取数已用 %.0fs ≥ 预算 %.0fs，其余 %d 只本轮不再取（记 unavailable: %s；"
                             "它跑在日报 save_report / 部署之前）", spent, budget, len(tickers) - i,
                             FETCH_SKIPPED_TIME_BUDGET)
        if aborted is not None:
            fail = aborted
        else:
            try:
                raw, reason = fetch_fn(tk, as_of=as_of)
                if raw is None:
                    fail = reason or "fetch_returned_none"
            except Exception as exc:  # noqa: BLE001
                fail = f"exception:{type(exc).__name__}"
                _log.warning("[%s] 卖权账本取数异常: %s", tk, exc)
            consecutive_net_fail = consecutive_net_fail + 1 if _is_network_failure(fail) else 0

        lm = None
        if fail is None:
            try:
                lm = L.level_map(raw.get("contracts") or [], raw.get("underlying_price"))
            except Exception as exc:  # noqa: BLE001
                fail = f"levels_error:{type(exc).__name__}"
                _log.warning("[%s] 水平地图计算异常: %s", tk, exc)
        # 取数与水平地图都走完才计：先记 ok 再挂在 level_map 上，诊断就自相矛盾了
        fetch_reasons[fail or "ok"] += 1

        info = None
        if fail is None:
            try:
                info = upcoming_fn(tk)
            except Exception as exc:  # noqa: BLE001
                _log.warning("[%s] 财报日查询异常（记 unknown）: %s", tk, exc)
                info = None

        for tenor in TENORS:
            if fail is not None:
                new_rows[tenor].append(unavailable_row(as_of, tk, tenor, fail, raw))
                continue
            try:
                new_rows[tenor].append(build_tenor_row(raw, lm, tenor, as_of=as_of,
                                                       earnings_info=info, ticker=tk))
            except Exception as exc:  # noqa: BLE001
                _log.warning("[%s] %s 行构造异常: %s", tk, tenor, exc)
                new_rows[tenor].append(unavailable_row(as_of, tk, tenor,
                                                       f"exception:{type(exc).__name__}", raw))

    per_tenor: Dict[str, dict] = {}
    for tenor in TENORS:
        out = {"recorded": 0, "unavailable": {}, "kept_previous_recorded": 0, "earnings_status": {},
               "price_source": {},
               "settled": 0, "gave_up": 0, "gave_up_reasons": {}, "settle_bars_unavailable": 0,
               "settle_bars_lagging": 0, "pending": None, "pending_overdue": None, "errors": []}
        try:
            today, st = _record_rows(as_of, tenor, new_rows[tenor], state_dir)
            batch = {r["ticker"] for r in new_rows[tenor]}
            mine = [r for r in today if r.get("ticker") in batch]
            out["recorded"] = sum(1 for r in mine if r.get("status") == "recorded")
            out["unavailable"] = dict(Counter(r.get("unavailable_reason") for r in mine
                                              if r.get("status") != "recorded"))
            out["kept_previous_recorded"] = st["kept_previous_recorded"]
            out["earnings_status"] = dict(Counter(r.get("earnings_status") for r in mine
                                                  if r.get("status") == "recorded"))
            out["price_source"] = dict(Counter(r.get("underlying_price_source") for r in mine
                                               if r.get("status") == "recorded"))
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"record:{type(exc).__name__}:{exc}")
            _log.warning("卖权账本 %s %s 记录失败: %s", as_of, tenor, exc)
        try:
            st = _settle(as_of, tenor, bars_fn=bars_fn, state_dir=state_dir)
            out["settled"] = st["settled"]
            out["gave_up"] = sum(st["gave_up"].values())
            out["gave_up_reasons"] = st["gave_up"]
            out["settle_bars_unavailable"] = st["bars_unavailable"]
            out["settle_bars_lagging"] = st["bars_lagging"]       # 其中：K 线源还没越过到期日
            out["pending"], out["pending_overdue"] = st["pending"], st["pending_overdue"]
            if st["pending_overdue"]:
                _log.warning("卖权账本 %s %s：%d 行到期已超过 %d 个日历日仍未结算（本轮取不到 K 线 %d 行；"
                             "取不到 K 线不计次，放弃闸要到取数窗口外才触发——查 K 线源）",
                             as_of, tenor, st["pending_overdue"], CONFIG["pending_overdue_days"],
                             st["bars_unavailable"])
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"settle:{type(exc).__name__}:{exc}")
            _log.warning("卖权账本 %s %s 结算失败: %s", as_of, tenor, exc)
        per_tenor[tenor] = out

    return {"as_of": as_of, "n_tickers": len(tickers), "per_tenor": per_tenor,
            "fetch_reasons": dict(fetch_reasons), "fetch_aborted": aborted,
            "elapsed_sec": round(time.monotonic() - t0, 3)}


# ─────────────────────────────── 独立单位与检验

def _unit_key(r: dict) -> Tuple[str, str]:
    return str(r.get("ticker")), str(r.get("expiry"))


def _eligible_except_earnings(r: dict) -> bool:
    """已记录 + 已结算 + route 两侧可用 + 报价是已收盘那一场的收盘后快照（来源不在排除集合、
    且 `session_live is not True`：盘中读到的与收盘后读到盘中文件的，权利金都不是收盘口径）
    + 版本戳与现行代码全符（`_version_mismatch` 为空：§10 协议变更前记的行默认不进检验）。
    按行判：同单位次日的正常行照样可以当单位的代表行——读取时刻 / 文件陈旧 / 记录时的代码版本
    都是与结局无关的属性，改选次日不偷看结果。"""
    rt = r.get("route") or {}
    return (r.get("status") == "recorded"
            and r.get("settle_status") == "settled"
            and r.get("underlying_price_source") not in PREREG["excluded_underlying_price_sources"]
            and r.get("session_live") is not True
            and rt.get("put") not in (None, "unavailable")
            and rt.get("call") not in (None, "unavailable")
            and not _version_mismatch(r))


def _eligible(r: dict) -> bool:
    return (_eligible_except_earnings(r)
            and r.get("earnings_status") in PREREG["eligible_earnings_status"])


def _earnings_tainted_units(rows: List[dict], earnings_rows: Optional[List[dict]] = None) -> set:
    """该 (ticker, expiry) 的**任一** recorded 行财报状态不合格（before_expiry / unknown / 别的）⇒ 整单位受污染。

    为什么按单位而不是按行（登记前定稿，2026-09-23）：日报钩子的财报日来自 ChronosBee 催化剂，它用
    `datetime.now()` 算 days_until ⇒ 财报**当天**（14:00 PT 扫描）得 −1 被丢；该票若还有别的未来催化剂
    （如分红日），就返回 `{"earnings_date": None}` ⇒ 当天那行判 "none"。而同单位更早的行都是
    before_expiry、被逐行排除，「取最早合格行」恰好选中财报当天那行：报价是财报前的 IV，结算却含
    财报跳空——要排除的污染被系统性地选进来。**不改 ChronosBee**：改它会截断维度 IC 协议 H2。
    判据只用记录时已知、与结局无关的信息（财报日在不在期权存续期内），不偷看结果。

    **再按票补一刀（登记前定稿，2026-09-24 最终评审）**：单位若在财报当天**才第一次**被选中（周度周二、
    月度周一换到新到期日，恰逢周二 / 周一盘后财报），它唯一的行就被误标 none，同单位没有更早的
    before_expiry 行可以连坐它。所以另从**该票所有 recorded 行（`earnings_rows`，两个 tenor 都算）**收集
    已知财报日 D：单位存在 d∈D 使 `首条记录日 ≤ d ≤ expiry` ⇒ 整单位排除。**防后视**：只收集
    `row.date ≤ 该单位首条记录日` 的行里的 earnings_date（X−1 那行记下 earnings_date=X 可以用；首条
    记录之后才冒出来的财报日不用）。`earnings_rows` 缺省 = `rows`。
    """
    ok = PREREG["eligible_earnings_status"]
    tainted = {_unit_key(r) for r in rows
               if r.get("status") == "recorded" and r.get("earnings_status") not in ok}

    first: Dict[Tuple[str, str], str] = {}
    for r in rows:
        if r.get("status") == "recorded":
            k, d = _unit_key(r), str(r.get("date"))
            if k not in first or d < first[k]:
                first[k] = d
    known: Dict[str, List[Tuple[str, str]]] = defaultdict(list)     # ticker → [(记录日, 财报日)]
    for r in (rows if earnings_rows is None else earnings_rows):
        ed = C._as_date(r.get("earnings_date")) if r.get("status") == "recorded" else None
        if ed is not None:
            known[str(r.get("ticker"))].append((str(r.get("date")), ed.isoformat()))
    for (tk, exp), d0 in first.items():
        if any(rd <= d0 <= ed <= exp for rd, ed in known.get(tk, ())):
            tainted.add((tk, exp))
    return tainted


def _independent_units(rows: List[dict], earnings_rows: Optional[List[dict]] = None
                       ) -> Tuple[List[dict], int]:
    """(独立单位, 因财报污染整单位排除、否则本可入选的单位数)。"""
    tainted = _earnings_tainted_units(rows, earnings_rows)
    best: Dict[Tuple[str, str], dict] = {}
    excluded = set()
    for r in sorted((r for r in rows if _eligible_except_earnings(r)), key=_row_key):
        k = _unit_key(r)
        if k in tainted:
            excluded.add(k)
            continue
        if k not in best and _eligible(r):
            best[k] = r
    return sorted(best.values(), key=_row_key), len(excluded)


def independent_units(rows: List[dict], earnings_rows: Optional[List[dict]] = None) -> List[dict]:
    """检验的独立单位：已记录 + 已结算 + route 可用 + 报价来源不在排除集合 + 财报不冲突，
    再按 **(ticker, expiry)** 去重、**取最早 date 的那一行**。

    财报按**单位**判：该 (ticker, expiry) 的所有 recorded 行（不论是否结算、route 是否可用）只要有一行
    不在 `PREREG["eligible_earnings_status"]` 里，整个单位排除；另按票：该票首条记录日当时已知的财报日
    （`earnings_rows` 的 recorded 行，缺省 = `rows`；assess 从账本读时传两个 tenor 的行）落在
    [首条记录日, expiry] 里 ⇒ 整单位排除（理由见 `_earnings_tainted_units`）。
    同一到期日会被连续多天选中（月度约一周、周度约五天），这些行共享同一个到期收盘，
    不是独立观测。取最早那行（照 max_pain 预注册的做法）：离到期最远、路由信息最「事前」。
    """
    return _independent_units(rows, earnings_rows)[0]


def _check_side(side: str) -> str:
    if side not in SIDES:
        raise ValueError(f"side 只接受 {SIDES}，收到 {side!r}")
    return side


def _outcome_or_reason(row: dict, side: str, rung) -> Tuple[Optional[float], Optional[str]]:
    """(单腿 short_<side> 在该档的 pnl_over_credit, None)，或 (None, 缺失原因)。

    原因取 `structure_quote` 的 reason 前缀（冒号前：`quote_not_ok` = bid=0 / ask<bid，
    `short_spread_too_wide` = 点差 > 25%，`non_positive_credit` …）；缺腿（`missing_leg`）再接上梯子该档
    第一条原因的前缀——`missing_leg:no_delta_within_tol` 即「容差内无合约」。§3 列的四类缺失据此分得开。
    原因只看记录时的报价，不看结果的数值。"""
    _check_side(side)
    ladder = row.get("ladder")
    if not isinstance(ladder, dict):
        return None, "no_ladder"
    q = C.structure_quote(ladder, f"short_{side}", rung)
    if not q.get("quotable"):
        why = str(q.get("reason") or "unquotable").split(":", 1)[0]
        if why == "missing_leg":
            slot = (ladder.get(side) or {}).get(C.rung_key(rung)) or {}
            first = next(iter(slot.get("reasons") or []), None)
            if first:
                why = f"missing_leg:{str(first).split(':', 1)[0]}"
        return None, why
    o = C.pnl_over_credit(q, row.get("expiry_close"))
    if o is None:
        return None, ("no_expiry_close" if _pos(row.get("expiry_close")) is None else "pnl_unavailable")
    return o, None


def unit_outcome(row: dict, side: str, rung) -> Optional[float]:
    """单腿 short_<side> 在该档的 `pnl_over_credit`（到期收盘口径）；不可报价 / 未结算 ⇒ None。"""
    return _outcome_or_reason(row, side, rung)[0]


def _side_arrays(units: List[dict], side: str, rung
                 ) -> Tuple[List[str], List[bool], List[float], dict, Dict[str, dict]]:
    """(块, flag, 结果, 跳过计数, 按 flag 分的缺失计数)。

    跳过计数：`no_flag`、`no_outcome`（合计）与 `no_outcome:<原因>`（原因见 `_outcome_or_reason`）。
    按 flag 分（`{"flag": {...}, "normal": {...}}`，键同 `no_outcome:<原因>`）：缺失的**规则**两臂同等，
    缺失**率**未必——§3 / §11 承诺就绪时按侧、按原因报告，差异缺失（flag 组缺得多）才查得出来。
    flag 与报价可否都是记录时已知的，不含结果的数值（§7：进度计数任何时候可看）。"""
    blocks, flags, outs = [], [], []
    skipped = Counter()
    by_flag = {"flag": Counter(), "normal": Counter()}
    # 规范顺序：块内元素的顺序决定随机数落在谁身上 ⇒ 不排序时 p 随调用方给的顺序变（实测 0.81 vs 0.85），
    # 预注册的 seed 就复现不了同一个 p。
    for u in sorted(units, key=lambda u: (str(u.get("date")), str(u.get("ticker")), str(u.get("expiry")))):
        f = (u.get("route") or {}).get(f"flag_{side}")
        if f is None:
            skipped["no_flag"] += 1
            continue
        o, why = _outcome_or_reason(u, side, rung)
        if o is None:
            key = f"no_outcome:{why}"
            skipped["no_outcome"] += 1
            skipped[key] += 1
            by_flag["flag" if bool(f) else "normal"][key] += 1
            continue
        blocks.append(str(u.get("date")))
        flags.append(bool(f))
        outs.append(float(o))
    return blocks, flags, outs, dict(skipped), {k: dict(v) for k, v in by_flag.items()}


def _blocks(blocks: List[str], flags: List[bool]) -> Tuple[Dict[str, List[int]], List[str]]:
    """(块 → 下标, 信息块)。信息块 = 块内 flag 数严格介于 0 与块大小之间（两种标签都有）。
    置换检验与就绪闸共用这一处定义——各写一份，闸门数的就可能不是检验用得上的东西。"""
    by_block: Dict[str, List[int]] = defaultdict(list)
    for i, b in enumerate(blocks):
        by_block[b].append(i)
    informative = [b for b, ix in by_block.items() if 0 < sum(flags[i] for i in ix) < len(ix)]
    return by_block, informative


def _group_counts(units: List[dict], side: str, rung) -> dict:
    """某侧的组计数：全体与**信息块内**各一份（就绪闸按 `PREREG["per_group_scope"]` 取后者）。
    只数标签与「有没有结果」，不碰结果的数值——任何时候可看，不破盲。"""
    blocks, flags, _o, skipped, skipped_by_flag = _side_arrays(units, side, rung)
    by_block, informative = _blocks(blocks, flags)
    inf_ix = [i for b in informative for i in by_block[b]]
    n_flag_inf = sum(flags[i] for i in inf_ix)
    return {"n_flagged": sum(flags), "n_normal": len(flags) - sum(flags),
            "n_units_with_outcome": len(flags), "skipped": skipped, "skipped_by_flag": skipped_by_flag,
            "n_blocks": len(by_block), "n_informative_blocks": len(informative),
            "n_flagged_informative": n_flag_inf, "n_normal_informative": len(inf_ix) - n_flag_inf}


#: 就绪闸的组计数口径 → per_side 里的 (flag 键, normal 键)。PREREG 改成这里没有的值 ⇒ KeyError（不静默退回全体）。
_GATE_COUNT_KEYS = {"informative_blocks": ("n_flagged_informative", "n_normal_informative")}


def block_permutation_p(units: List[dict], side: str, *, n_perm: int = PREREG["n_perm"],
                        seed: int = PREREG["seed"]) -> dict:
    """H1（单侧）：flag 组在基线档（`PREREG["primary_rung"]`=0.20，**不随 route 变**）的
    pnl_over_credit 更差。统计量 = mean(outcome|flag) − mean(outcome|normal)，越负越支持 H1。

    **只在同一记录日（block = date）内打乱 flag 标签**，p = (1 + #{null ≤ observed})/(1 + n_perm)。
    为什么按日分块：GEX 政体与击穿都是**全市场相关**的——同一天的票一起负 gamma、一起跌穿。
    跨日打乱时，零分布把「坏日子」与「好日子」的标签混在一起，而观测值里 flag 与坏日子绑在一起，
    日期效应被算成 flag 效应，p 偏小（横截面池化教训：可比的量不蕴含可池化的相关性）。
    日内打乱保留日期骨架与每日 flag 数，只问「同一天里，被 flag 的票是不是更差」。
    全天同一标签的日子对零分布没有贡献（保守），不耗随机数。

    并列：零分布与观测值由不同求和顺序算出，恰好相等时浮点可能差 1 ulp——
    比较带 1e-12 相对容差，并列算作「≤」（保守方向）。
    """
    _check_side(side)
    n_perm = int(n_perm)
    rung = PREREG["primary_rung"]
    blocks, flags, outs, skipped, skipped_by_flag = _side_arrays(units, side, rung)
    n = len(outs)
    n_flag = sum(flags)
    n_norm = n - n_flag
    by_block, informative = _blocks(blocks, flags)
    out = {"side": side, "rung": rung, "n_units": n, "n_flagged": n_flag, "n_normal": n_norm,
           "n_blocks": len(by_block), "n_informative_blocks": len(informative),
           "n_perm": n_perm, "seed": int(seed), "skipped": skipped, "skipped_by_flag": skipped_by_flag,
           "observed": None, "p": None, "reason": None}
    if n_flag == 0 or n_norm == 0:
        out["reason"] = "empty_group"
        return out
    if n_perm < 1:
        raise ValueError("n_perm 必须 ≥ 1")
    f_arr = np.asarray(flags, dtype=float)
    o_arr = np.asarray(outs, dtype=float)
    total = float(o_arr.sum())
    obs_sflag = float((f_arr * o_arr).sum())
    observed = obs_sflag / n_flag - (total - obs_sflag) / n_norm

    rng = np.random.default_rng(int(seed))
    null_sflag = np.zeros(n_perm)
    for b in sorted(by_block):
        ix = np.asarray(by_block[b])
        fb, ob = f_arr[ix], o_arr[ix]
        if b not in informative:
            null_sflag += float((fb * ob).sum())
            continue
        perm = np.argsort(rng.random((n_perm, ix.size)), axis=1)
        null_sflag += (fb[perm] * ob[None, :]).sum(axis=1)
    null = null_sflag / n_flag - (total - null_sflag) / n_norm
    tol = 1e-12 * max(1.0, abs(observed))
    count = int(np.sum(null <= observed + tol))
    out["observed"] = observed
    out["p"] = (1 + count) / (1 + n_perm)
    return out


def _calibration(units: List[dict]) -> dict:
    """按档：已结算单腿的实际到期 ITM 频率 vs 平均 N(d2)（风险中性）。描述性、不涉 flag、不破盲。
    预期：卖方有 VRP 时实际频率低于风险中性概率。"""
    out: Dict[str, Dict[str, dict]] = {}
    for side in SIDES:
        out[side] = {}
        for d in C.LADDER_DELTAS:
            key = f"{d:.2f}"
            probs, hits = [], []
            for u in units:
                leg = (((u.get("ladder") or {}).get(side) or {}).get(key) or {}).get("short")
                close = _pos(u.get("expiry_close"))
                if not isinstance(leg, dict) or close is None:
                    continue
                K, p = _pos(leg.get("strike")), _num(leg.get("itm_prob"))
                if K is None or p is None:
                    continue
                probs.append(p)
                hits.append(1.0 if (close < K if side == "put" else close > K) else 0.0)
            out[side][key] = {"n": len(probs),
                              "mean_itm_prob_risk_neutral": (sum(probs) / len(probs)) if probs else None,
                              "actual_itm_freq": (sum(hits) / len(hits)) if hits else None}
    return out


def _as_known_on(r: dict, as_of: str) -> dict:
    """as_of 那天看得到的这一行：结算 / 放弃发生在 as_of 之后（或日期缺失、判不了）⇒ 副本当 pending。
    不这样做，assess(as_of) 会用到 as_of 之后才有的到期收盘（评审实测：同一个 as_of，结算前后两次判定
    n_settled 0 → 1），「可复现某日的判定」就不成立，冻结首个就绪日也无从回放。"""
    st = r.get("settle_status")
    when = r.get("settled_on") if st == "settled" else (
        r.get("settle_give_up_on") if st == "give_up" else None)
    if st not in ("settled", "give_up") or (isinstance(when, str) and when[:10] <= as_of):
        return r
    c = dict(r)
    c["settle_status"] = "pending"
    for f in _OUTCOME_FIELDS:
        c[f] = None
    return c


def _data_horizon(rows: List[dict]) -> Optional[str]:
    """这批行里最晚的已知日期（记录日 / 结算日 / 放弃日）。冻结的 ready_date = min(as_of, 它)：
    assess(as_of=它) 看到的正是同一批信息；as_of 给得比数据还晚（手误）也不会把 ready_date 推到未来。"""
    ds = [str(v)[:10] for r in rows for v in (r.get("date"), r.get("settled_on"), r.get("settle_give_up_on"))
          if isinstance(v, str) and len(v) >= 10]
    return max(ds) if ds else None


def _run_prereg_test(units: List[dict]) -> Dict[str, dict]:
    tests = {}
    for side in SIDES:
        res = block_permutation_p(units, side, n_perm=PREREG["n_perm"], seed=PREREG["seed"])
        p = res["p"]
        res["alpha_each"] = PREREG["alpha_each"]
        res["decision"] = (None if p is None else
                           ("reject_h0" if p <= PREREG["alpha_each"] else "fail_to_reject_h0"))
        tests[side] = res
    return tests


def prereg_result_path(tenor: str, state_dir=None) -> Path:
    return _state_dir(state_dir) / _check_tenor(tenor) / PREREG_RESULT_NAME


def load_prereg_result(tenor: str, state_dir=None) -> Optional[dict]:
    """冻结的检验结果；没有 ⇒ None。读不懂 / 版本或 tenor 对不上 ⇒ **抛**（不当成「没有」而重算：
    检验只跑一次，重算就是第二次；本地报告的 `_assess_safe` 会把原因写出来）。"""
    p = prereg_result_path(tenor, state_dir)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"冻结检验结果 {p} 无法解析（{exc}）——不重算，人工核查") from exc
    if not isinstance(d, dict) or d.get("tenor") != tenor or d.get("prereg_version") != PREREG["version"]:
        raise RuntimeError(f"冻结检验结果 {p} 的 tenor / prereg_version 与当前协议不符"
                           f"（{d.get('tenor') if isinstance(d, dict) else d!r} / "
                           f"{d.get('prereg_version') if isinstance(d, dict) else '?'}）——协议改过？人工核查")
    return d


def _freeze_prereg_result(tenor: str, payload: dict, state_dir=None) -> dict:
    """原子、先到者赢：tmp 写好后 `os.link` 到目标名（目标已存在 ⇒ FileExistsError，不覆盖）。
    唯一的调用路径是 `assess(freeze=True)`，唯一传它的是日报钩子；先到者赢兜的是同一天手动补跑与定时扫描撞车。
    返回**读回来的**那份（JSON 往返后的形状），首次与此后每次返回逐字节相同。"""
    p = prereg_result_path(tenor, state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".tmp.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        try:
            os.link(tmp, p)
            _log.info("卖权账本 %s 首次达到检验就绪闸：预注册检验已运行并冻结 → %s（此后只读、不重算）",
                      tenor, p)
        except FileExistsError:
            _log.info("卖权账本 %s 的检验结果已被别的进程先冻结，读它的", tenor)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return load_prereg_result(tenor, state_dir)


def assess(tenor: str, *, rows: Optional[List[dict]] = None, state_dir=None,
           as_of: Optional[str] = None, freeze: bool = False) -> dict:
    """进度 + 就绪闸 + （仅 ready 且已冻结 / 注入时）预注册检验。

    status：`undetermined`（无行）/ `accruing` / `ready`（独立单位 ≥ min_independent_per_tenor、
    不同到期日 ≥ min_distinct_expiries、每侧**信息块内**的 flag / normal 各 ≥ min_per_group——
    `PREREG["per_group_scope"]`，信息块 = 同一记录日里两种标签都有的块，分块置换只从这种块取信息）。
    ⚠️ **ready 只代表可以开始检验，不代表已验证。**
    status != "ready" 时根本不调用 `block_permutation_p`，返回值里没有 `BLINDED_KEYS` 中的任何键
    （代码层面不算、不显示；从别的出口自己拼结果 = 偷看，见预注册文档 §7）。

    `as_of` 给了 ⇒ 只看 date ≤ as_of 的行，且 as_of 之后才结算 / 放弃的行按 pending 算
    （`_as_known_on`）——同一个 as_of，今天判与一个月后判结果相同。

    **检验只跑一次，唯一写者是日报钩子**：从账本读行（`rows is None`）、首次 ready、且 `freeze=True`
    ⇒ 跑检验、冻结进 `<state>/<tenor>/prereg_result.json`（ready_date = min(as_of, 数据视界)，
    as_of 晚于 PDT 今天 ⇒ ValueError）。`freeze=False`（缺省：MCP、CLI）遇到「就绪但未冻结」⇒
    status 仍是 ready、`awaiting_freeze=True`、**不跑检验、不写文件**。冻结之后，只要 as_of 为 None
    或 ≥ 冻结的 ready_date，status 恒为 ready、`test` 恒为冻结的那份（后来的数据不改它；后来的数据
    让闸门跌回也不改——检验已经跑过了，`gates` 如实显示当前数据的闸门）。as_of 早于 ready_date
    ⇒ 不给 test（那是未来的结果），也不重算。`rows=` 注入 ⇒ 不读不写冻结文件，ready 时现算
    （测试 / 探针用；此时 freeze=True ⇒ ValueError）。

    财报按票排除（`_earnings_tainted_units`）要看**两个 tenor** 的行：从账本读时另读全部 tenor 作
    `earnings_rows`；`rows=` 注入时只用注入的行。
    """
    _check_tenor(tenor)
    from_ledger = rows is None
    if freeze and not from_ledger:
        raise ValueError("assess(freeze=True) 只对从账本读行有效：rows= 注入（测试 / 探针）不读不写冻结文件")
    if as_of is not None:
        if freeze:
            _check_not_future(as_of, "assess(freeze=True)")
        else:
            _check_date(as_of)            # 只读：看一个未来日期不写任何东西，不拦
    n_corrupt = 0
    earnings_rows = None
    if from_ledger:
        rows, n_corrupt = _load_all(tenor, state_dir)
        earnings_rows = [r for t in TENORS
                         for r in (rows if t == tenor else _load_all(t, state_dir)[0])]
    rows = list(rows)
    if as_of is not None:
        rows = [_as_known_on(r, as_of) for r in rows if str(r.get("date")) <= as_of]
        if earnings_rows is not None:
            earnings_rows = [r for r in earnings_rows if str(r.get("date")) <= as_of]

    recorded = [r for r in rows if r.get("status") == "recorded"]
    units, n_earnings_excluded = _independent_units(rows, earnings_rows)
    rung = PREREG["primary_rung"]
    per_side = {side: _group_counts(units, side, rung) for side in SIDES}
    f_key, n_key = _GATE_COUNT_KEYS[PREREG["per_group_scope"]]
    n_expiries = len({str(u.get("expiry")) for u in units})
    need = {"min_independent_per_tenor": PREREG["min_independent_per_tenor"],
            "min_distinct_expiries": PREREG["min_distinct_expiries"],
            "min_per_group": PREREG["min_per_group"],
            "per_group_scope": PREREG["per_group_scope"]}
    gates = {
        "independent_units": len(units) >= need["min_independent_per_tenor"],
        "distinct_expiries": n_expiries >= need["min_distinct_expiries"],
        **{f"{side}_groups": (per_side[side][f_key] >= need["min_per_group"]
                              and per_side[side][n_key] >= need["min_per_group"])
           for side in SIDES},
    }
    if not rows:
        status = "undetermined"
    elif all(gates.values()):
        status = "ready"
    else:
        status = "accruing"

    excluded_src = PREREG["excluded_underlying_price_sources"]
    n_pending, n_overdue = _pending_counts(recorded, as_of or _today())
    progress = {
        "n_rows": len(rows),
        "n_recorded": len(recorded),
        "n_unavailable": dict(Counter(r.get("unavailable_reason") for r in rows
                                      if r.get("status") != "recorded")),
        "n_pending": n_pending,
        # 到期已超过 pending_overdue_days 个日历日仍 pending（参照日 = as_of，缺省 PDT 今天）
        "n_pending_overdue": n_overdue,
        "pending_overdue_days": CONFIG["pending_overdue_days"],
        "n_settled": sum(1 for r in recorded if r.get("settle_status") == "settled"),
        "n_give_up": sum(1 for r in recorded if r.get("settle_status") == "give_up"),
        "give_up_reasons": dict(Counter(r.get("settle_give_up_reason") for r in recorded
                                        if r.get("settle_status") == "give_up")),
        "earnings_status": dict(Counter(r.get("earnings_status") for r in recorded)),
        # 报价来源分布；不是收盘后快照的行（来源在排除集合，或 session_live 为真）照记、不进检验
        "price_source": dict(Counter(r.get("underlying_price_source") for r in recorded)),
        "n_rows_price_source_excluded": sum(1 for r in recorded
                                            if r.get("underlying_price_source") in excluded_src
                                            or r.get("session_live") is True),
        # 版本戳与现行代码不符的行（§10：协议变更前记的行默认不进检验）；按不符的项计数
        "n_rows_version_excluded": sum(1 for r in recorded if _version_mismatch(r)),
        "version_mismatch": dict(Counter(k for r in recorded for k in _version_mismatch(r))),
        "n_route_unavailable": sum(1 for r in recorded
                                   if (r.get("route") or {}).get("put") in (None, "unavailable")),
        "n_independent": len(units),
        # 因财报污染整单位排除、否则本可入选的 (ticker, expiry) 数（两臂同等，不涉方向）
        "n_units_earnings_excluded": n_earnings_excluded,
        "n_distinct_expiries": n_expiries,
        # 每侧信息块数（块内两种标签都有）：闸门只数这些块里的单位
        "n_informative_blocks": {side: per_side[side]["n_informative_blocks"] for side in SIDES},
        "per_side": per_side,
        "n_corrupt_lines": n_corrupt,
    }

    frozen = load_prereg_result(tenor, state_dir) if from_ledger else None
    awaiting_freeze = from_ledger and frozen is None and status == "ready"
    if awaiting_freeze and freeze:
        horizon = _data_horizon(rows)
        ready_date = horizon if as_of is None else min(as_of, horizon)
        _check_not_future(ready_date, "冻结的 ready_date（账本里有晚于今天的日期？人工核查）")
        frozen = _freeze_prereg_result(tenor, {
            "tenor": tenor, "prereg_version": PREREG["version"],
            "ready_date": ready_date, "data_horizon": horizon,
            "as_of": as_of, "frozen_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "n_units": len(units),
            "unit_keys": sorted([str(u.get("ticker")), str(u.get("expiry")), str(u.get("date"))]
                                for u in units),
            "need": need, "gates": gates,
            "per_side": _run_prereg_test(units),
            "note": "预注册检验只跑一次：本文件由日报钩子里首次满足就绪闸的 assess(freeze=True) 写入，此后只读。"
                    "删改本文件 = 协议变更（experiments/sell_strike_routing_prereg.md §10）。",
        }, state_dir)
        awaiting_freeze = False
    frozen_applies = frozen is not None and (as_of is None or as_of >= str(frozen.get("ready_date")))
    if frozen_applies:
        status = "ready"
    out = {"tenor": tenor, "status": status, "ready": status == "ready", "as_of": as_of,
           "prereg_version": PREREG["version"], "need": need, "gates": gates,
           "progress": progress, "calibration": _calibration(units),
           "frozen": None, "awaiting_freeze": awaiting_freeze,
           "note": "ready 只代表可以开始检验，不代表已验证；未 ready 时不计算任何效应量；"
                   "检验只跑一次（日报钩子首次看到就绪即冻结）。"}
    if frozen is not None:
        out["frozen"] = {"ready_date": frozen.get("ready_date"), "frozen_at": frozen.get("frozen_at"),
                         "n_units": frozen.get("n_units"), "applies": frozen_applies,
                         "path": str(prereg_result_path(tenor, state_dir))}
    if status != "ready":
        return out
    if frozen_applies:
        out["test"] = frozen["per_side"]
    elif not from_ledger:
        out["test"] = _run_prereg_test(units)
    # 余下两种都不给 test、不重算：① 就绪但尚未冻结（freeze=False：等日报钩子冻结）；
    # ② 冻结结果存在但 as_of 早于冻结日、当日数据也够闸——检验已在更晚的样本上跑过一次
    return out


def _fmt_reasons(c: dict) -> str:
    """{"no_outcome:short_spread_too_wide": 2, ...} → "（short_spread_too_wide 2、…）"；空 ⇒ ""。"""
    items = [(k.split(":", 1)[1] if k.startswith("no_outcome:") else k, v) for k, v in sorted((c or {}).items())]
    return ("（" + "、".join(f"{k} {v}" for k, v in items) + "）") if items else ""


def _missing_line(ps: dict) -> str:
    """就绪时报告的「基线档结果缺失」：按侧、按 flag / normal、按原因（预注册 §3 / §11 的承诺）。
    差异缺失（某组缺得明显多）会让两组可比性打折——这一行让它在冻结判定旁边就看得见。"""
    parts = []
    for s in SIDES:
        side = ps.get(s) or {}
        bf = side.get("skipped_by_flag") or {}
        fl, nm = bf.get("flag") or {}, bf.get("normal") or {}
        no_flag = (side.get("skipped") or {}).get("no_flag", 0)
        parts.append(f"{s} flag {sum(fl.values())}{_fmt_reasons(fl)}/normal {sum(nm.values())}{_fmt_reasons(nm)}"
                     + (f"/无 flag {no_flag}" if no_flag else ""))
    return f"{PREREG['primary_rung']:.2f} 档结果缺失（不进检验）：" + " · ".join(parts)


def summary_line(res: dict) -> str:
    """一行进度（报告 / CLI 共用）。未 ready 时显式标「样本不足，环境路由仅供参考」；
    ready 时追加基线档结果缺失（按侧 × flag/normal × 原因，§3 / §11 承诺就绪时报告）。"""
    tenor = res.get("tenor")
    pg = res.get("progress") or {}
    need = res.get("need") or {}
    ps = pg.get("per_side") or {}
    # 闸门只数信息块（同一记录日里两种标签都有的块）内的单位；全体计数放括号里作对照
    groups = " · ".join(
        f"{s} flag {ps.get(s, {}).get('n_flagged_informative', 0)}"
        f"/normal {ps.get(s, {}).get('n_normal_informative', 0)}"
        f"（信息块 {ps.get(s, {}).get('n_informative_blocks', 0)}；全体 flag {ps.get(s, {}).get('n_flagged', 0)}"
        f"/normal {ps.get(s, {}).get('n_normal', 0)}）"
        for s in SIDES)
    n_src_x = pg.get("n_rows_price_source_excluded", 0)
    n_ver_x = pg.get("n_rows_version_excluded", 0)
    base = (f"独立单位 {pg.get('n_independent', 0)}/{need.get('min_independent_per_tenor')} · "
            f"不同到期日 {pg.get('n_distinct_expiries', 0)}/{need.get('min_distinct_expiries')} · "
            f"{groups}（每组需 ≥{need.get('min_per_group')}，只数信息块）· 已结算 {pg.get('n_settled', 0)} 行"
            f" · 放弃 {pg.get('n_give_up', 0)} 行"
            + (f" · 非收盘后报价不进检验 {n_src_x} 行" if n_src_x else "")
            + (f" · 版本戳与现行协议不符不进检验 {n_ver_x} 行 {pg.get('version_mismatch') or {}}"
               if n_ver_x else "") +
            # 待结算要写出来：取不到 K 线时「已结算 0 · 放弃 0」与「没有行到期」一模一样
            f" · 待结算 {pg.get('n_pending', 0)} 行（其中已过期超过 "
            f"{pg.get('pending_overdue_days', CONFIG['pending_overdue_days'])} 个日历日 "
            f"{pg.get('n_pending_overdue', 0)} 行）")
    if res.get("status") == "undetermined":
        return f"❓ {tenor}：账本里还没有任何行"
    if res.get("status") != "ready":
        return f"◐ {tenor}：样本不足，环境路由仅供参考 —— {base}"
    base += " · " + _missing_line(ps)
    fz = res.get("frozen") or {}
    if fz.get("applies"):
        return (f"✅ {tenor}：已达检验就绪闸，预注册检验已于 {fz.get('ready_date')} 冻结（只跑一次；"
                f"不代表已验证，看判定）—— {base}")
    if res.get("awaiting_freeze"):
        return (f"✅ {tenor}：已就绪，等待日报冻结（预注册检验尚未运行；只有日报钩子会跑它并冻结；"
                f"只代表可以开始检验，不代表已验证）—— {base}")
    return f"✅ {tenor}：已达检验就绪闸（只代表可以开始检验，不代表已验证）—— {base}"


# ─────────────────────────────── CLI

def main(argv: Optional[List[str]] = None) -> int:
    """退出码：0 成功 / 1 部分失败（有不可得行或记录/结算异常；或 --date 非法 / 晚于 PDT 今天）/
    3 无法判定（一行都没记上，或两档都无数据）。
    ⚠️ 3 而非 2：编排器 `run_step()` 把 2 保留给「脚本不存在」（同 vrp_signal）。多个动作取最大码。
    `--assess` 只读、**不冻结**（冻结的唯一写者是日报钩子）：就绪但未冻结时显示「已就绪，等待日报冻结」。"""
    ap = argparse.ArgumentParser(description="卖权行权价账本：记录 / 结算 / 就绪度")
    ap.add_argument("--date", default=None, help="业务日 YYYY-MM-DD（默认 PDT 今天）")
    ap.add_argument("--run", action="store_true", help="run_for_date：取链、记录两档、结算")
    ap.add_argument("--settle", action="store_true", help="只结算已到期的 pending 行")
    ap.add_argument("--assess", action="store_true", help="就绪度（无任何动作标志时的默认）")
    ap.add_argument("--tickers", default=None, help="逗号分隔；缺省 config.WATCHLIST")
    ap.add_argument("--state-dir", default=None, help="账本目录（缺省 PATHS.sell_strike_state）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        # 未来日期一律拒绝（不止写路径）：CLI 的 --date 是人手敲的，年份写错最常见
        as_of = _check_not_future(args.date or _today(), "--date")
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
    do_assess = args.assess or not (args.run or args.settle)
    result: dict = {"date": as_of}
    codes: List[int] = []

    if args.run:
        res = run_for_date(as_of, tickers=tickers, state_dir=args.state_dir)
        result["run"] = res
        rec = sum(v["recorded"] for v in res["per_tenor"].values())
        bad = any(v["unavailable"] or v["errors"] for v in res["per_tenor"].values())
        codes.append(3 if rec == 0 else (1 if bad else 0))
    elif args.settle:
        per = {}
        for tenor in TENORS:
            try:
                per[tenor] = _settle(as_of, tenor, state_dir=args.state_dir)
                codes.append(0)
            except Exception as exc:  # noqa: BLE001
                per[tenor] = {"error": f"{type(exc).__name__}: {exc}"}
                codes.append(1)
        result["settle"] = per
    if do_assess:
        result["assess"] = {t: assess(t, state_dir=args.state_dir, as_of=as_of) for t in TENORS}
        codes.append(3 if all(a["status"] == "undetermined" for a in result["assess"].values()) else 0)

    if args.json:
        print(json.dumps(_clean(result), indent=2, ensure_ascii=False))
    else:
        if "run" in result:
            for tenor, v in result["run"]["per_tenor"].items():
                print(f"{tenor}: 记录 {v['recorded']} / 不可得 {v['unavailable']} / 结算 {v['settled']}"
                      f" / 放弃 {v['gave_up']} / 取不到 K 线 {v['settle_bars_unavailable']}"
                      f" / 待结算 {v['pending']}（过期>{CONFIG['pending_overdue_days']}日 {v['pending_overdue']}）"
                      + (f" / 错误 {v['errors']}" if v["errors"] else ""))
            print(f"取数：{result['run']['fetch_reasons']}（{result['run']['elapsed_sec']}s）")
        if "settle" in result:
            for tenor, v in result["settle"].items():
                print(f"{tenor} 结算：{v}")
        for a in (result.get("assess") or {}).values():
            print(summary_line(a))
    return max(codes) if codes else 0


if __name__ == "__main__":
    sys.exit(main())
