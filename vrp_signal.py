#!/usr/bin/env python3
"""
🐝 Alpha Hive — 方差风险溢价（VRP）信号：只记账、不下注（v0.45.102）
=====================================================================
期权路线图第 4 步（第 1~3 步：v0.45.99 `quote_set` 真实报价 → v0.45.100
波动率目标仓位 → v0.45.101 财报跨式纸面腿）。

VRP 是什么
----------
方差风险溢价 = 隐含波动率（IV）系统性高于**随后**实现的波动率（RV）。
期权卖方长期赚的就是这个差：买保险的人愿意多付一点。文献里指数层面的
VRP 稳定为正，个股层面弱得多、且随时间大幅波动 —— 所以"IV 比 RV 高"
本身不是信号，**"今天的 IV−RV 相对这只票自己的历史处在哪个分位"**才是。

为什么必须逐票时序、绝不横截面池化
----------------------------------
IV 的横截面跨度（AMC 90% vs KO 15%）是时序跨度的 ~6 倍。把 30 只票的
IV−RV 倒进一个池子算分位，阈值测出来的是"你是哪只票"，不是"今天贵不贵"
（MEMORY：alpha-hive-cross-sectional-pooling，v0.45.50 那 7 条机器失效
条件 28/30 只从未触发、COST 却 20% 在响，就是这么来的）。本模块的
`ts_pct` 只拿**该票自己**过去的 `vrp_ex_ante` 做分母，不与任何其他票比较。

为什么要闸门（63 条 ≈ 3 个月扫描日）
------------------------------------
真实 IV 观测（`iv_history.py`）自 2026-08-15 起才开始自攒，眼下每票只有
~7–11 条。用 10 条历史算分位，分辨率 1/10，"80% 分位"意味着只比 8 个数大 ——
那不是分位，是噪音。与 `iv_history.IV_RANK_MIN_DAYS` 同一下限：63 条以下
`ts_pct`/`label` 一律 None，`ready=False`，诚实说"还在攒"。`assess()` 用
**实测积累速率**（`iv_history._observed_accrual_rate`）外推 ETA，不按日历
日猜 —— 条目只在有扫描且抓到真实 IV 的日子才产生，扫描日覆盖率远低于 100%。

结算（settlement）定义
----------------------
每条记录的 `vrp_ex_ante = iv − rv_30d`（百分点，事前）。**21 个交易日**
（`forward_days`）之后，用记录日（含，作基准）起到第 21 个交易日的收盘价算
`rv_forward`，口径**逐字复刻** `twelve_data.realized_vol`：对数收益、
`ddof=1`、×√252×100、剔除 |log_ret| ≥ 0.5、至少 5 个收益率。
`vrp_realized = iv − rv_forward`（事后：卖方到底赚没赚到这个差）。
事前与事后必须同一口径，否则差值会被误读成溢价变化。只用 ≤ `as_of` 的
K 线，不偷看未来。

重跑不得抹掉结算（v0.45.104）
----------------------------
`record_day` 同日重跑会整日重写。**重写必须把 `settle()` 已经填好的字段
原样搬过来**（`_SETTLEMENT_FIELDS`），否则日报 `--date` 回填路径重跑一次旧
日期，就把当天所有行的 `rv_forward`/`vrp_realized`/`settled_on` 抹成 None，
而日志照旧打印成功行。只要基准日还落在 `_default_bars` 的 120 根窗口里，
下次 settle 还能补回来 —— 出了窗口就是永久丢失，且全程无声。

补跑快照（backfilled）是单向的 —— 已修
--------------------------------------
真实补跑文件名是 `options_snapshot_WMT_2026-08-28_backfilled-2026-08-29.json`：
业务日在中间、跑批日在末尾。`_iter_snapshots` 的 glob 与 `_SNAP_RE` 都要求
`_YYYY-MM-DD.json` 结尾，补跑文件是 `-YYYY-MM-DD.json`，两处都匹配不上 ——
**这是对的**（否则会把业务日记到跑批日名下）。但 `_snapshot_rv` 从前也只开
精确文件名，于是补跑日进得了 IV 索引、进不了 VRP 历史：实测索引 272 条 vs
可用历史 212 条，每票整整差 2 条。`_snapshot_rv` 现在会退到同业务日的最新
补跑文件（IV 本来就取自同一份快照，口径一致）。

本版**不做**的事
----------------
- 不下注：没有纸面腿、没有仓位、不影响任何评分。
- 不做横截面排名（理由见上）。
- 不做期限结构调整：`iv` 取 ~30 天 ATM，`rv` 取 30 日 —— 一个到期、一个窗口。
- 不回填历史行：`record_day` 只写 `as_of` 当日；不过算 `n_obs` 时会把
  IV 索引里已有、且同日快照能提供 `rv_30d` 的旧日期一并计入（启动期多
  十来条历史，纯读取、不落盘）。

状态：`vrp_state/vrp_signals.jsonl`（每票每日一行，按日期幂等重写）。

用法
----
    /usr/local/bin/python3 vrp_signal.py                    # 只读就绪度（默认）
    /usr/local/bin/python3 vrp_signal.py --record --settle  # 日报钩子做的事
    /usr/local/bin/python3 vrp_signal.py --json --out /tmp/vrp.json

退出码：0 = 已就绪（≥ min_ready_tickers 只票各有 ≥ min_obs 条**可用**历史）
        1 = 攒数中（正常态）
        3 = 无法判定（缓存目录里连一个 IV 索引都没有）
        ⚠️ 3 而非 2：编排器 `run_step()` 把 2 保留给"脚本不存在"。
        ⚠️ 退出码只表达就绪度（编排器契约），**不**表达 --record/--settle 的
        成败：那两个的条数在 `--json` 里，写 0 行时另有一条 WARNING。
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from hive_logger import PATHS, get_logger

_log = get_logger("vrp_signal")

CONFIG = {
    "min_obs_per_ticker": 63,      # 与 iv_history.IV_RANK_MIN_DAYS 同一下限（≈3 个月扫描日）
    "min_ready_tickers": 20,       # 至少 20 只票各攒够，模块才算"可用"
    "forward_days": 21,            # 事后实现波动率窗口（交易日）
    "rich_pct": 0.80,              # ts_pct ≥ 0.80 → rich（IV 相对自身历史偏贵）
    "cheap_pct": 0.20,             # ts_pct ≤ 0.20 → cheap
    "iv_source_order": ["iv_raw_observed", "quote_set.iv30"],
    "rv_source": "rv_30d",
    # 结算放弃闸（v0.45.104）：拿到了 K 线却仍算不出 rv_forward 的行，重试这么多次后放弃
    "settle_max_attempts": 5,
    # `_default_bars` 只取 120 根；基准日掉出这个窗口就永远结算不了，别再每天白取一次 K 线
    "settle_window_bars": 120,
    # 工作日是交易日的**上界**，反过来判"掉出窗口"要留出节假日余量，宁可晚放弃不可早放弃
    "settle_window_slack": 15,
}

BASE_DIR = PATHS.home
STATE_DIR = BASE_DIR / "vrp_state"
SIGNALS_FILE = STATE_DIR / "vrp_signals.jsonl"

_SNAP_RE = re.compile(r"options_snapshot_(.+)_(\d{4}-\d{2}-\d{2})\.json$")
_ROW_FIELDS = ("ticker", "date", "iv", "iv_source", "rv_30d", "vrp_ex_ante", "n_obs", "ready",
               "ts_pct", "label", "underlying_price", "rv_forward", "vrp_realized", "settled_on",
               "reason", "settle_attempts", "settle_give_up", "settle_give_up_reason",
               "settle_give_up_on")

# 只由 `settle()` 写、`record_day` 绝不能覆盖的字段。新增结算字段务必同步加进来，
# 否则同日重跑会把它悄悄抹成 None（v0.45.104 修的就是这个）。
_SETTLEMENT_FIELDS = ("rv_forward", "vrp_realized", "settled_on", "settle_attempts",
                      "settle_give_up", "settle_give_up_reason", "settle_give_up_on")


# ---------------------------------------------------------------- 小工具
def _num(v) -> Optional[float]:
    """float 且有限；NaN/inf/None/非数 → None。`bool(nan) is True`，别用 `if x:`。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, nd=4) -> Optional[float]:
    return round(v, nd) if v is not None else None


def _cache_dir(cache_dir) -> Path:
    if cache_dir is None:
        return Path(PATHS.cache_dir)
    p = Path(cache_dir)
    return p if p.is_absolute() else BASE_DIR / p


def _load_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out: List[Dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _write_jsonl(path: Path, records: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
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


def load_rows() -> List[Dict]:
    return _load_jsonl(SIGNALS_FILE)


def rows_for_date(as_of: str) -> List[Dict]:
    return [r for r in load_rows() if r.get("date") == as_of]


def _extract(snap: dict, path: str):
    """按点号路径取值：`quote_set.iv30` → snap["quote_set"]["iv30"]；任何一层缺失 → None。"""
    cur = snap
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _iter_snapshots(cache_dir: Path, as_of: str):
    """`as_of` 当日的常规快照。**补跑（`_backfilled-`）文件天然进不来**：它们叫
    `..._{业务日}_backfilled-{跑批日}.json`，glob 与 `_SNAP_RE` 都要求 `_YYYY-MM-DD.json`
    结尾，而补跑文件是 `-YYYY-MM-DD.json`。这正是我们要的 —— 让它们按跑批日进来，
    等于把业务日记到错误的日期名下。（此处原有一行 `"_backfilled-" in p.name` 的
    显式过滤，v0.45.104 删除：它读起来像在承重，实际永远不会触发，
    见 test_backfilled_files_are_excluded_by_glob_and_regex。）"""
    for p in sorted(cache_dir.glob(f"options_snapshot_*_{as_of}.json")):
        m = _SNAP_RE.match(p.name)
        if not m or m.group(2) != as_of:
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError) as exc:
            _log.warning("快照读取失败 %s: %s", p.name, exc)
            continue
        if isinstance(snap, dict):
            yield m.group(1).upper(), snap


def _snapshot_rv(cache_dir: Path, ticker: str, date: str) -> Optional[float]:
    """该票该**业务日**快照里的 `rv_30d`。

    v0.45.104：精确文件名缺失时退到同业务日的补跑文件
    `options_snapshot_{T}_{date}_backfilled-*.json`（同日多份取跑批日最新的一份）。
    这条路径是合法的：`iv_history` 的 IV 本来就取自同一份快照，两边口径一致 ——
    从前只开精确文件名，导致补跑日单向地"能进 IV 索引、进不了 VRP 历史"
    （实测索引 272 条 / 可用 212 条，每票差 2 条）。
    ⚠️ 只在这里认补跑文件；`_iter_snapshots`/`record_day` 仍然不认（理由见那边）。
    """
    p = cache_dir / f"options_snapshot_{ticker}_{date}.json"
    if not p.exists():
        cands = sorted(cache_dir.glob(f"options_snapshot_{ticker}_{date}_backfilled-*.json"))
        if not cands:
            return None
        p = cands[-1]
    try:
        with p.open("r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    return _num(snap.get(CONFIG["rv_source"])) if isinstance(snap, dict) else None


def realized_vol_from_closes(closes: List[float]) -> Optional[float]:
    """年化已实现波动率（%），**逐字复刻** `twelve_data.realized_vol` 的算法段：
    对数收益、剔除 |r| ≥ 0.5、至少 5 个收益率、ddof=1、×√252×100，
    合法区间同样是 `0 < rv <= 300`。

    v0.45.104：从前这里放行 rv == 0，理由是"测试用常数收盘价钉住
    `vrp_realized == iv`"—— 方向反了。21 个交易日收盘价一动不动是数据坏了
    （停牌 / 补齐 / 取错列），不是波动率真的为零；放行它等于凭空造出一条
    **可能的最大**"卖方全赢"观测（`vrp_realized == iv`），再喂进
    `settlement_stats.by_label` 的均值里。口径与上游对齐，测试改用非退化序列。
    """
    rets: List[float] = []
    for a, b in zip(closes[:-1], closes[1:]):
        a, b = _num(a), _num(b)
        if a is not None and b is not None and a > 0 and b > 0:
            r = math.log(b / a)
            if abs(r) < 0.5:
                rets.append(r)
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    rv = math.sqrt(var) * math.sqrt(252) * 100
    return rv if math.isfinite(rv) and 0 < rv <= 300 else None


def _weekdays_after(d0: str, d1: str) -> int:
    """(d0, d1] 之间的工作日数 —— 交易日数的**上界**（节假日只会更少）。
    用作 settle 的零成本预筛：连工作日都不够 forward_days 的行不用去取 K 线。"""
    try:
        a, b = dt.date.fromisoformat(d0), dt.date.fromisoformat(d1)
    except ValueError:
        return 0
    if b <= a:
        return 0
    return sum(1 for i in range(1, (b - a).days + 1) if (a + dt.timedelta(days=i)).weekday() < 5)


# ---------------------------------------------------------------- 记录
def _prior_history(ticker: str, as_of: str, cache_dir: Path, file_rows: List[Dict]) -> Dict[str, float]:
    """该票 `as_of` **之前**每个日期的 vrp_ex_ante：{date: vrp}。

    来源 ① 本文件已有行（含 None 行，None 行只用来标记"该日已处理"）；
    来源 ② IV 索引（`iv_history._read_index`）里有、本文件没有的旧日期 ——
    用同日快照的 rv_30d 现场重建。② 让模块上线时不必从零起步，且只读不写。
    """
    hist: Dict[str, float] = {}
    covered = set()
    for r in file_rows:
        if r.get("ticker") != ticker:
            continue
        d = r.get("date")
        if not d or d >= as_of:
            continue
        covered.add(d)
        v = _num(r.get("vrp_ex_ante"))
        if v is not None:
            hist[d] = v
    try:
        from iv_history import _read_index
        index = _read_index(ticker, str(cache_dir))
    except Exception as exc:  # noqa: BLE001
        _log.debug("[%s] IV 索引不可读: %s", ticker, exc)
        index = {}
    for d, iv in index.items():
        if d >= as_of or d in covered:
            continue
        rv = _snapshot_rv(cache_dir, ticker, d)
        iv = _num(iv)
        if rv is not None and iv is not None:
            hist[d] = iv - rv
    return hist


def _label(ts_pct: float) -> str:
    if ts_pct >= CONFIG["rich_pct"]:
        return "rich"
    if ts_pct <= CONFIG["cheap_pct"]:
        return "cheap"
    return "mid"


def record_day(as_of: str, cache_dir=None) -> List[Dict]:
    """为 `as_of` 的每份常规快照写一行 VRP 记录（同日重跑 = 整日重写）。返回写入的行。

    v0.45.104：重写**不得**抹掉 `settle()` 已经填好的字段。日报 `--date` 回填路径
    （`date_override` → `self.date_str` → VRP 钩子）会拿旧日期重跑 `record_day`，
    从前那一下会把当天所有行的 `rv_forward`/`vrp_realized`/`settled_on` 清成 None，
    日志却照常打印"记录 N 行"。基准日还在 120 根窗口内时下次 settle 能补回来，
    出了窗口就是永久丢失 —— 全程无声。
    """
    cdir = _cache_dir(cache_dir)
    existing = load_rows()
    keep = [r for r in existing if r.get("date") != as_of]
    # 被本次重写顶掉的旧行，按票索引，用来搬运结算字段
    superseded = {r.get("ticker"): r for r in existing if r.get("date") == as_of}
    min_obs = int(CONFIG["min_obs_per_ticker"])

    new_rows: List[Dict] = []
    for ticker, snap in _iter_snapshots(cdir, as_of):
        iv, iv_source = None, None
        for src in CONFIG["iv_source_order"]:
            iv = _num(_extract(snap, src))
            if iv is not None:
                iv_source = src
                break
        rv = _num(snap.get(CONFIG["rv_source"]))

        row: Dict = {k: None for k in _ROW_FIELDS}
        row.update({"ticker": ticker, "date": as_of, "iv": _r(iv), "iv_source": iv_source,
                    "rv_30d": _r(rv), "underlying_price": _r(_num(snap.get("_snapshot_stock_price"))),
                    "ready": False})

        # ── 搬运结算字段（v0.45.104）──────────────────────────────────────
        # settle() 写的东西只有 settle() 能改。重跑同一天不该让"已结算"倒退成"未结算"。
        prev = superseded.get(ticker)
        if prev:
            for k in _SETTLEMENT_FIELDS:
                if prev.get(k) is not None:
                    row[k] = prev[k]
            # iv 被修订过（快照重抓）时顺带重算，维持 vrp_realized == iv − rv_forward
            # 这条不变式；新 iv 缺失就原样留着，宁可留旧值也不要丢数据。
            rv_f, prev_iv = _num(row.get("rv_forward")), _num(prev.get("iv"))
            if rv_f is not None and iv is not None and (prev_iv is None or prev_iv != iv):
                row["vrp_realized"] = _r(iv - rv_f)

        if iv is None or rv is None:
            row["reason"] = ("iv_and_rv_missing" if iv is None and rv is None
                             else "iv_missing" if iv is None else "rv_30d_missing")
            new_rows.append(row)
            continue

        vrp = iv - rv
        row["vrp_ex_ante"] = _r(vrp)
        hist = _prior_history(ticker, as_of, cdir, keep)
        n_obs = len(hist)          # 严格早于 as_of 的不同日期数；今天这行不算
        row["n_obs"] = n_obs
        if n_obs >= min_obs:
            below = sum(1 for v in hist.values() if v < vrp)
            ts_pct = below / n_obs
            row.update({"ready": True, "ts_pct": _r(ts_pct), "label": _label(ts_pct)})
        new_rows.append(row)

    all_rows = keep + new_rows
    all_rows.sort(key=lambda r: (str(r.get("date")), str(r.get("ticker"))))
    _write_jsonl(SIGNALS_FILE, all_rows)
    _log.info("VRP 记录 %d 行（%s，其中 ready %d / 缺数 %d）", len(new_rows), as_of,
              sum(1 for r in new_rows if r["ready"]), sum(1 for r in new_rows if r["reason"]))
    return new_rows


# ---------------------------------------------------------------- 结算
def _default_bars(ticker: str, as_of: Optional[str] = None) -> Optional[List[dict]]:
    """Twelve Data 已配置 → `fetch_bars(t, 120, end_date=as_of)`；否则退回本地价格索引。
    Twelve Data 配置了但这次拿不到（限流/断网）→ None，下次再试，**不**换源：
    价格索引来自快照 `_snapshot_stock_price`，与日线收盘不是一个口径。

    v0.45.105 两处改动，都是为了让同一只票的日线在一次扫描里**只取一次**
    （从前 options_paper_leg / 本模块 / portfolio_greeks 各取一次，
    而 Twelve Data 是串行 7 次/分钟的队列）：

    ① 走 `twelve_data.fetch_bars`（进程内按 `(ticker, end_date)` 记忆）而不是
       裸 `_fetch_rows`；
    ② 传 `end_date=as_of`。另外两个消费方本来就传 as_of，本模块传 `None` 的话
       键不同、缓存不共享 —— 而 `None` 在这里并没有换来任何东西：
       `_forward_closes` 本来就只用 `date <= as_of` 的 K 线，多出来的部分一直是
       被丢掉的。生产上 as_of 就是当天，两者取回的窗口一模一样；补跑历史时
       `end_date=as_of` 反而**更**对——从前是「最新 120 根再截到 as_of」，
       as_of 一旦落在 120 根之前就一根不剩，现在是「截至 as_of 的 120 根」，
       正好对上 `settle_window_bars`(=120) 那条放弃闸量的东西。
    `as_of=None` 仍然合法（老调用方/测试），语义照旧是「最新」。"""
    try:
        import twelve_data
        if twelve_data.is_configured():
            return twelve_data.fetch_bars(
                ticker, twelve_data.SHARED_BARS_WINDOW, end_date=as_of)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] Twelve Data 取 K 线失败: %s", ticker, exc)
        return None
    try:
        import price_history
        series = price_history.load_price_history(ticker, str(_cache_dir(None)))
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] 本地价格索引不可读: %s", ticker, exc)
        return None
    return [{"date": d, "close": c} for d, c in series]


def _forward_closes(bars: List[dict], date: str, as_of: str, forward_days: int) -> Optional[List[float]]:
    """从 `date`（含，基准）到其后第 `forward_days` 个交易日的收盘价；只用 ≤ as_of 的 K 线。
    基准 = 日期 ≤ `date` 的最后一根（快照落在非交易日时退到前一收盘）。不够 → None。"""
    usable = sorted((b for b in bars if isinstance(b, dict) and b.get("date") and b["date"] <= as_of),
                    key=lambda b: b["date"])
    base_idx = None
    for i, b in enumerate(usable):
        if b["date"] <= date:
            base_idx = i
        else:
            break
    if base_idx is None or base_idx + forward_days >= len(usable):
        return None
    closes = [_num(b.get("close")) for b in usable[base_idx: base_idx + forward_days + 1]]
    return None if any(c is None for c in closes) else closes


def _give_up(row: Dict, as_of: str, reason: str) -> None:
    row["settle_give_up"] = True
    row["settle_give_up_reason"] = reason
    row["settle_give_up_on"] = as_of
    _log.info("[%s] %s 的 VRP 行放弃结算：%s", row.get("ticker"), row.get("date"), reason)


def settle(as_of: str, bars_fn: Optional[Callable[[str], Optional[List[dict]]]] = None) -> int:
    """给已到期的行填 `rv_forward` / `vrp_realized`。每票最多取一次 K 线；没有可结算行的票不取。

    v0.45.104 放弃闸：从前 `pending` 只看 `rv_forward is None`，于是基准日一旦掉出
    `_default_bars` 的 120 根窗口，这行就**永远**结算不了，却仍然每票每天白取一次
    K 线，而且完全看不见（既不是 settled 也没有任何标记）。现在两种情形会打
    `settle_give_up` 并退出 pending：
      ① `out_of_fetch_window` —— 基准日离 as_of 的工作日数已超过取数窗口（留了
         `settle_window_slack` 的节假日余量：工作日是交易日的上界，宁可晚放弃）；
      ② `max_attempts_exhausted` —— **拿到了 K 线**却仍算不出 rv_forward，累计
         `settle_max_attempts` 次。取不到 K 线（限流/断网）不计次，那是暂时的。
    放弃数在 `settlement_stats()['n_gave_up']` 与日报小节里可见。
    """
    # 默认取数腿要吃到 as_of（见 `_default_bars` 的 v0.45.105 说明）。用闭包而不是
    # 改 `bars_fn(ticker)` 的调用形状——注入版 bars_fn 的签名是外部契约，测试在用。
    bars_fn = bars_fn or (lambda t: _default_bars(t, as_of))
    fwd = int(CONFIG["forward_days"])
    max_attempts = int(CONFIG["settle_max_attempts"])
    window = int(CONFIG["settle_window_bars"]) + int(CONFIG["settle_window_slack"])
    rows = load_rows()
    dirty = False
    pending: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        d = r.get("date")
        if (r.get("rv_forward") is None and not r.get("settle_give_up")
                and _num(r.get("iv")) is not None and d and _weekdays_after(d, as_of) >= fwd):
            if _weekdays_after(d, as_of) > window:
                _give_up(r, as_of, "out_of_fetch_window")
                dirty = True
                continue
            pending[r["ticker"]].append(r)
    if not pending:
        if dirty:
            _write_jsonl(SIGNALS_FILE, rows)
        return 0

    n = 0
    for ticker, trs in pending.items():
        try:
            bars = bars_fn(ticker)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] settle 取 K 线失败: %s", ticker, exc)
            continue
        if not bars:
            continue
        for r in trs:
            closes = _forward_closes(bars, r["date"], as_of, fwd)
            rv_f = realized_vol_from_closes(closes) if closes is not None else None
            if rv_f is None:
                # 有 K 线还算不出来 —— 这才计一次失败（限流/断网走上面的 continue，不计）
                r["settle_attempts"] = int(r.get("settle_attempts") or 0) + 1
                dirty = True
                if r["settle_attempts"] >= max_attempts:
                    _give_up(r, as_of, "max_attempts_exhausted")
                continue
            r["rv_forward"] = _r(rv_f)
            r["vrp_realized"] = _r(float(r["iv"]) - rv_f)
            r["settled_on"] = as_of
            n += 1
    if n or dirty:
        _write_jsonl(SIGNALS_FILE, rows)
    if n:
        _log.info("VRP 结算 %d 行（%s）", n, as_of)
    return n


# ---------------------------------------------------------------- 就绪度与统计
def _avg_ranks(values: List[float]) -> List[float]:
    """平均秩（同值取平均名次），1..n。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def within_ticker_spearman(rows: List[Dict], min_rows: int = 20) -> Dict:
    """票内 Spearman：先在**每只票内部**把 vrp_ex_ante / vrp_realized 各自转成 [0,1] 秩，
    再把所有票的秩对合并算一个相关。绝不把原始 VRP 跨票混算（横截面陷阱）。
    只收 ≥ min_rows 条已结算行的票；一只都没有 → rho None。"""
    by: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for r in rows:
        x, y = _num(r.get("vrp_ex_ante")), _num(r.get("vrp_realized"))
        if x is not None and y is not None and r.get("ticker"):
            by[r["ticker"]].append((x, y))
    xs: List[float] = []
    ys: List[float] = []
    n_tickers = 0
    for pairs in by.values():
        if len(pairs) < min_rows:
            continue
        n_tickers += 1
        m = len(pairs)
        rx = _avg_ranks([p[0] for p in pairs])
        ry = _avg_ranks([p[1] for p in pairs])
        xs += [(v - 1) / (m - 1) for v in rx]
        ys += [(v - 1) / (m - 1) for v in ry]
    rho = _pearson(xs, ys) if n_tickers else None
    return {"n_tickers": n_tickers, "n_pairs": len(xs), "rho": _r(rho)}


def settlement_stats(rows: Optional[List[Dict]] = None, min_n: int = 10) -> Dict:
    rows = load_rows() if rows is None else rows
    settled = [r for r in rows if _num(r.get("vrp_realized")) is not None]
    by_label: Dict[str, List[float]] = defaultdict(list)
    for r in settled:
        by_label[r.get("label") or "unlabeled"].append(float(r["vrp_realized"]))
    gave_up = [r for r in rows if r.get("settle_give_up")]
    out = {"n_rows": len(rows), "n_settled": len(settled), "n_gave_up": len(gave_up), "by_label": {}}
    for label in sorted(by_label):
        vals = by_label[label]
        out["by_label"][label] = {"n": len(vals),
                                  "mean_vrp_realized": _r(sum(vals) / len(vals)) if len(vals) >= min_n else None}
    out["spearman_within_ticker"] = within_ticker_spearman(rows)
    return out


def _usable_history_counts(cache_dir: Path, tickers: List[str], rows: List[Dict],
                           as_of: str) -> Dict[str, int]:
    """每票**真正能被 `record_day(as_of)` 用上**的历史条数，口径逐字等于 `_prior_history`。

    与 IV 索引条数不是一回事：索引里有 IV、但同业务日快照拿不到 `rv_30d` 的日期
    算不出 `vrp_ex_ante`，进不了分位的分母。`assess()` 从前只数索引，于是能打印
    「✅ 已就绪」而当天每一行都是 `ready=False` —— 闸门量的根本不是它把守的那个数。

    ⚠️ 必须传 `as_of` 并沿用"严格早于"语义：把 as_of 当天那行也数进来，就会在
    边界上刚好多一条，闸门又能在 `n_obs == min_obs - 1` 时说就绪 —— 同一个 bug
    换了个小一号的身量（本条注释由它自己的回归测试逼出来）。
    """
    return {t: len(_prior_history(t, as_of, cache_dir, rows)) for t in tickers}


def assess(cache_dir=None, rows: Optional[List[Dict]] = None, as_of: Optional[str] = None) -> Dict:
    """就绪度判定（纯读取，无副作用：用 `_read_index` 而非 `load_iv_history`，后者会写迁移标记）。

    ⚠️ `rows`（VRP 记账行）来自模块级 `SIGNALS_FILE`，**与 cache_dir 无关** ——
    信号文件是全局状态，不按缓存目录分片。v0.45.104 把它提成显式参数：从前
    `settlement_stats()` 不带参地读全局文件，探针式地 `assess(cache_dir=某临时目录)`
    会把生产结算统计打印成那个临时目录的结果。要探针就传 `rows=[]`。
    """
    from hive_logger import pdt_today
    from iv_history import _observed_accrual_rate, _read_index

    cdir = _cache_dir(cache_dir)
    rows = load_rows() if rows is None else rows
    as_of = as_of or pdt_today()
    min_obs = int(CONFIG["min_obs_per_ticker"])
    min_ready = int(CONFIG["min_ready_tickers"])
    tickers = sorted(os.path.basename(p)[len("iv_history_"):-len(".jsonl")]
                     for p in glob.glob(os.path.join(str(cdir), "iv_history_*.jsonl")))
    per_ticker = {t: min(len(_read_index(t, str(cdir))), 252) for t in tickers}
    per_ticker_usable = _usable_history_counts(cdir, tickers, rows, as_of)
    index_ready_tickers = sorted(t for t, n in per_ticker.items() if n >= min_obs)
    # 就绪判定认**可用**口径，不认索引口径 —— 闸门必须量它把守的那个数
    ready_tickers = sorted(t for t, n in per_ticker_usable.items() if n >= min_obs)

    if not tickers:
        status = "undetermined"
    elif len(ready_tickers) >= min_ready:
        status = "ready"
    else:
        status = "accruing"

    rate, observed_days, elapsed = (None, 0, 0)
    if tickers:
        rate, observed_days, elapsed = _observed_accrual_rate(str(cdir))

    # ETA：第 min_ready 高的票攒到 min_obs 还差几条，按实测速率折成周（5 个交易日/周）
    eta_weeks, eta_note = None, None
    counts = sorted(per_ticker.values(), reverse=True)
    if status == "ready":
        eta_weeks = 0.0
    elif len(index_ready_tickers) >= min_ready:
        # 索引攒够了、可用历史没攒够 —— 瓶颈不是"再等等"，是同日快照缺 rv_30d，
        # 按积累速率外推会给出一个假的「0.0 周后到位」。
        eta_note = (f"IV 索引已有 {len(index_ready_tickers)} 只票达标，但只有 "
                    f"{len(ready_tickers)} 只的历史真能被 record_day 用上"
                    f"（缺同业务日快照的 {CONFIG['rv_source']}）—— 瓶颈不是攒得不够")
    elif len(counts) < min_ready:
        eta_note = f"只有 {len(counts)} 只票在攒 IV 历史，不足 {min_ready} 只 —— 光靠攒攒不到就绪"
    elif not rate:
        eta_note = "实测积累速率无法判定（索引里还没有 ≤ 今天的记录）"
    else:
        need = max(0, min_obs - counts[min_ready - 1])
        eta_weeks = round(need / rate / 5, 1)

    return {
        "status": status,
        "ready": status == "ready",
        "cache_dir": str(cdir),
        "as_of": as_of,
        "min_obs_per_ticker": min_obs,
        "min_ready_tickers": min_ready,
        "n_tickers": len(tickers),
        "ready_tickers": ready_tickers,
        "n_ready_tickers": len(ready_tickers),
        "per_ticker_obs": per_ticker,
        "median_obs": (sorted(counts)[len(counts) // 2] if counts else 0),
        # ↓ 两个口径都报出来：索引条数（上面）vs record_day 真能用的条数（下面）。
        #   ready 的判据是后者；两者之差 = 有 IV 却缺同日 rv_30d 的日期数。
        "per_ticker_usable_obs": per_ticker_usable,
        "n_index_ready_tickers": len(index_ready_tickers),
        "index_ready_tickers": index_ready_tickers,
        "median_usable_obs": (sorted(per_ticker_usable.values())[len(per_ticker_usable) // 2]
                              if per_ticker_usable else 0),
        "n_obs_index_total": sum(per_ticker.values()),
        "n_obs_usable_total": sum(per_ticker_usable.values()),
        "accrual_rate_per_trading_day": _r(rate, 3) if rate else None,
        "accrual_observed_days": observed_days,
        "accrual_elapsed_trading_days": elapsed,
        "eta_weeks": eta_weeks,
        "eta_note": eta_note,
        "settlement": settlement_stats(rows),
    }


def summary_line(res: Dict) -> str:
    s = res["status"]
    if s == "undetermined":
        return f"❓ VRP 信号：无法判定 —— {res['cache_dir']} 里没有任何 IV 索引"
    st = res["settlement"]
    gave_up = f"，放弃 {st['n_gave_up']} 行" if st.get("n_gave_up") else ""
    # 两个口径都写出来：索引条数会系统性高于可用条数，只报前者就是 v0.45.104 修的那个假就绪
    both = (f"可用历史 {res['n_ready_tickers']}/{res['n_tickers']} 只达标"
            f"（IV 索引口径 {res['n_index_ready_tickers']} 只；"
            f"条数中位 可用 {res['median_usable_obs']} / 索引 {res['median_obs']}）")
    if s == "ready":
        return (f"✅ VRP 信号已就绪：{both}，各 ≥{res['min_obs_per_ticker']} 条"
                f"（已结算 {st['n_settled']} 行{gave_up}）")
    eta = (f"，按实测速率约 {res['eta_weeks']} 周后到位" if res["eta_weeks"] is not None
           else f"，ETA 无法判定：{res['eta_note']}")
    return (f"⏳ VRP 信号攒数中：{both}"
            f"，需 {res['min_ready_tickers']} 只各 ≥{res['min_obs_per_ticker']} 条{gave_up}{eta}")


# ---------------------------------------------------------------- 日报小节
def _fmt(v, nd=2, suffix="") -> str:
    v = _num(v)
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


def render_markdown(as_of: str, cache_dir=None) -> str:
    """日报小节；当日没有记录 → 空串（不占版面）。"""
    rows = rows_for_date(as_of)
    if not rows:
        return ""
    res = assess(cache_dir, as_of=as_of)
    lines = ["", "## VRP 信号（攒数期，未下注）", ""]
    lines.append(summary_line(res))
    lines += ["", "| 标的 | IV | RV30 | IV−RV | 历史 n | 时序分位 | 标签 |",
              "|------|----|------|-------|--------|----------|------|"]
    for r in sorted(rows, key=lambda x: (-(_num(x.get("vrp_ex_ante")) or float("-inf")), x["ticker"])):
        lines.append(f"| {r['ticker']} | {_fmt(r.get('iv'), 1, '%')} | {_fmt(r.get('rv_30d'), 1, '%')} | "
                     f"{_fmt(r.get('vrp_ex_ante'), 2)} | {r.get('n_obs') if r.get('n_obs') is not None else '—'} | "
                     f"{_fmt(r.get('ts_pct'), 2)} | {r.get('label') or '—'} |")
    st = res["settlement"]
    if st["n_settled"] or st["n_gave_up"]:
        parts = [f"已结算 {st['n_settled']}/{st['n_rows']} 行（{CONFIG['forward_days']} 个交易日事后 RV）"]
        if st["n_gave_up"]:
            parts.append(f"已放弃 {st['n_gave_up']} 行（基准日出了取数窗口或反复算不出）")
        for label, d in st["by_label"].items():
            parts.append(f"{label}: n={d['n']}, 均值 {_fmt(d['mean_vrp_realized'])}")
        sp = st["spearman_within_ticker"]
        parts.append(f"票内 Spearman ρ={_fmt(sp['rho'], 3)}（{sp['n_tickers']} 只 / {sp['n_pairs']} 对）")
        lines += ["", "**结算**：" + "；".join(parts)]
    lines += ["", "> IV−RV 为百分点；时序分位只与**该票自身**历史比较，从不跨票池化。",
              f"> 历史 < {CONFIG['min_obs_per_ticker']} 条时分位与标签一律留空 —— 这是量表刻度不够，不是中性。",
              "> **仅记录与结算，不影响任何仓位或评分。**", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def main(argv: Optional[List[str]] = None) -> int:
    from hive_logger import pdt_today
    ap = argparse.ArgumentParser(description="VRP 信号：记录 / 结算 / 就绪度")
    ap.add_argument("--date", default=None, help="业务日 YYYY-MM-DD（默认 PDT 今天）")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--record", action="store_true", help="为该日写 VRP 记录")
    ap.add_argument("--settle", action="store_true", help="结算已到期的行")
    ap.add_argument("--assess", action="store_true", help="只读就绪度（无任何标志时的默认）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="把 JSON 结果写到该文件（供编排器读，理由同 ic_rerun_readiness）")
    args = ap.parse_args(argv)
    as_of = args.date or pdt_today()

    # v0.45.104：退出码只表达就绪度（编排器契约），--record/--settle 的成败另走
    # warnings + JSON 计数。从前 `--date D --record` 一行没写，退出码与健康的
    # 「攒数中」逐字节相同 —— 报错和正常态长得一样，等于没报。
    result: Dict = {"date": as_of, "recorded": None, "settled": None, "warnings": []}
    if args.record:
        result["recorded"] = len(record_day(as_of, cache_dir=args.cache_dir))
        if result["recorded"] == 0:
            msg = (f"--record 被要求了，但 {as_of} 一行没写 —— "
                   f"该日在 {_cache_dir(args.cache_dir)} 里没有任何常规期权快照")
            _log.warning(msg)
            result["warnings"].append(msg)
    if args.settle:
        result["settled"] = settle(as_of)
    res = assess(cache_dir=args.cache_dir, as_of=as_of)
    result["assessment"] = res
    if res["settlement"]["n_gave_up"]:
        result["gave_up"] = res["settlement"]["n_gave_up"]

    if args.out:
        try:
            Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"⚠️  无法写入 {args.out}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if args.record:
            print(f"记录 {result['recorded']} 行（{as_of}）")
        if args.settle:
            print(f"结算 {result['settled']} 行（{as_of}）")
        for w in result["warnings"]:
            print(f"⚠️  {w}", file=sys.stderr)
        print(summary_line(res))
        if res["status"] != "undetermined":
            top = sorted(res["per_ticker_obs"].items(), key=lambda kv: -kv[1])[:10]
            print("  " + ", ".join(f"{t} {n}" for t, n in top))

    return {"ready": 0, "accruing": 1, "undetermined": 3}[res["status"]]


if __name__ == "__main__":
    sys.exit(main())
