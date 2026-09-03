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

退出码：0 = 已就绪（≥ min_ready_tickers 只票各有 ≥ min_obs 条真实 IV）
        1 = 攒数中（正常态）
        3 = 无法判定（缓存目录里连一个 IV 索引都没有）
        ⚠️ 3 而非 2：编排器 `run_step()` 把 2 保留给"脚本不存在"。
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
}

BASE_DIR = PATHS.home
STATE_DIR = BASE_DIR / "vrp_state"
SIGNALS_FILE = STATE_DIR / "vrp_signals.jsonl"

_SNAP_RE = re.compile(r"options_snapshot_(.+)_(\d{4}-\d{2}-\d{2})\.json$")
_ROW_FIELDS = ("ticker", "date", "iv", "iv_source", "rv_30d", "vrp_ex_ante", "n_obs", "ready",
               "ts_pct", "label", "underlying_price", "rv_forward", "vrp_realized", "settled_on",
               "reason")


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
    for p in sorted(cache_dir.glob(f"options_snapshot_*_{as_of}.json")):
        if "_backfilled-" in p.name:
            continue
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
    p = cache_dir / f"options_snapshot_{ticker}_{date}.json"
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    return _num(snap.get(CONFIG["rv_source"])) if isinstance(snap, dict) else None


def realized_vol_from_closes(closes: List[float]) -> Optional[float]:
    """年化已实现波动率（%），**逐字复刻** `twelve_data.realized_vol` 的算法段：
    对数收益、剔除 |r| ≥ 0.5、至少 5 个收益率、ddof=1、×√252×100。

    与上游唯一的差别：不拒绝 rv == 0（常数收盘价是合法的退化输入，测试用它
    钉住 `vrp_realized == iv`）；> 300% 仍视为坏数据返回 None。
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
    return rv if math.isfinite(rv) and rv <= 300 else None


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
    """为 `as_of` 的每份常规快照写一行 VRP 记录（同日重跑 = 整日重写）。返回写入的行。"""
    cdir = _cache_dir(cache_dir)
    existing = load_rows()
    keep = [r for r in existing if r.get("date") != as_of]
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
def _default_bars(ticker: str) -> Optional[List[dict]]:
    """Twelve Data 已配置 → `_fetch_rows(t, 120)`（每票一次调用）；否则退回本地价格索引。
    Twelve Data 配置了但这次拿不到（限流/断网）→ None，下次再试，**不**换源：
    价格索引来自快照 `_snapshot_stock_price`，与日线收盘不是一个口径。"""
    try:
        import twelve_data
        if twelve_data.is_configured():
            return twelve_data._fetch_rows(ticker, 120)
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


def settle(as_of: str, bars_fn: Optional[Callable[[str], Optional[List[dict]]]] = None) -> int:
    """给已到期的行填 `rv_forward` / `vrp_realized`。每票最多取一次 K 线；没有可结算行的票不取。"""
    bars_fn = bars_fn or _default_bars
    fwd = int(CONFIG["forward_days"])
    rows = load_rows()
    pending: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        d = r.get("date")
        if (r.get("rv_forward") is None and _num(r.get("iv")) is not None and d
                and _weekdays_after(d, as_of) >= fwd):
            pending[r["ticker"]].append(r)
    if not pending:
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
            if closes is None:
                continue
            rv_f = realized_vol_from_closes(closes)
            if rv_f is None:
                continue
            r["rv_forward"] = _r(rv_f)
            r["vrp_realized"] = _r(float(r["iv"]) - rv_f)
            r["settled_on"] = as_of
            n += 1
    if n:
        _write_jsonl(SIGNALS_FILE, rows)
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
    out = {"n_rows": len(rows), "n_settled": len(settled), "by_label": {}}
    for label in sorted(by_label):
        vals = by_label[label]
        out["by_label"][label] = {"n": len(vals),
                                  "mean_vrp_realized": _r(sum(vals) / len(vals)) if len(vals) >= min_n else None}
    out["spearman_within_ticker"] = within_ticker_spearman(rows)
    return out


def assess(cache_dir=None) -> Dict:
    """就绪度判定（纯读取，无副作用：用 `_read_index` 而非 `load_iv_history`，后者会写迁移标记）。"""
    from iv_history import _observed_accrual_rate, _read_index

    cdir = _cache_dir(cache_dir)
    min_obs = int(CONFIG["min_obs_per_ticker"])
    min_ready = int(CONFIG["min_ready_tickers"])
    tickers = sorted(os.path.basename(p)[len("iv_history_"):-len(".jsonl")]
                     for p in glob.glob(os.path.join(str(cdir), "iv_history_*.jsonl")))
    per_ticker = {t: min(len(_read_index(t, str(cdir))), 252) for t in tickers}
    ready_tickers = sorted(t for t, n in per_ticker.items() if n >= min_obs)

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
        "min_obs_per_ticker": min_obs,
        "min_ready_tickers": min_ready,
        "n_tickers": len(tickers),
        "ready_tickers": ready_tickers,
        "n_ready_tickers": len(ready_tickers),
        "per_ticker_obs": per_ticker,
        "median_obs": (sorted(counts)[len(counts) // 2] if counts else 0),
        "accrual_rate_per_trading_day": _r(rate, 3) if rate else None,
        "accrual_observed_days": observed_days,
        "accrual_elapsed_trading_days": elapsed,
        "eta_weeks": eta_weeks,
        "eta_note": eta_note,
        "settlement": settlement_stats(),
    }


def summary_line(res: Dict) -> str:
    s = res["status"]
    if s == "undetermined":
        return f"❓ VRP 信号：无法判定 —— {res['cache_dir']} 里没有任何 IV 索引"
    if s == "ready":
        return (f"✅ VRP 信号已就绪：{res['n_ready_tickers']}/{res['n_tickers']} 只票各有 "
                f"≥{res['min_obs_per_ticker']} 条真实 IV（已结算 {res['settlement']['n_settled']} 行）")
    eta = (f"，按实测速率约 {res['eta_weeks']} 周后到位" if res["eta_weeks"] is not None
           else f"，ETA 无法判定：{res['eta_note']}")
    return (f"⏳ VRP 信号攒数中：{res['n_ready_tickers']}/{res['n_tickers']} 只票达标"
            f"（需 {res['min_ready_tickers']} 只各 ≥{res['min_obs_per_ticker']} 条，"
            f"中位 {res['median_obs']} 条）{eta}")


# ---------------------------------------------------------------- 日报小节
def _fmt(v, nd=2, suffix="") -> str:
    v = _num(v)
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


def render_markdown(as_of: str, cache_dir=None) -> str:
    """日报小节；当日没有记录 → 空串（不占版面）。"""
    rows = rows_for_date(as_of)
    if not rows:
        return ""
    res = assess(cache_dir)
    lines = ["", "## VRP 信号（攒数期，未下注）", ""]
    lines.append(summary_line(res))
    lines += ["", "| 标的 | IV | RV30 | IV−RV | 历史 n | 时序分位 | 标签 |",
              "|------|----|------|-------|--------|----------|------|"]
    for r in sorted(rows, key=lambda x: (-(_num(x.get("vrp_ex_ante")) or float("-inf")), x["ticker"])):
        lines.append(f"| {r['ticker']} | {_fmt(r.get('iv'), 1, '%')} | {_fmt(r.get('rv_30d'), 1, '%')} | "
                     f"{_fmt(r.get('vrp_ex_ante'), 2)} | {r.get('n_obs') if r.get('n_obs') is not None else '—'} | "
                     f"{_fmt(r.get('ts_pct'), 2)} | {r.get('label') or '—'} |")
    st = res["settlement"]
    if st["n_settled"]:
        parts = [f"已结算 {st['n_settled']}/{st['n_rows']} 行（{CONFIG['forward_days']} 个交易日事后 RV）"]
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

    result: Dict = {"date": as_of, "recorded": None, "settled": None}
    if args.record:
        result["recorded"] = len(record_day(as_of, cache_dir=args.cache_dir))
    if args.settle:
        result["settled"] = settle(as_of)
    res = assess(cache_dir=args.cache_dir)
    result["assessment"] = res

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
        print(summary_line(res))
        if res["status"] != "undetermined":
            top = sorted(res["per_ticker_obs"].items(), key=lambda kv: -kv[1])[:10]
            print("  " + ", ".join(f"{t} {n}" for t, n in top))

    return {"ready": 0, "accruing": 1, "undetermined": 3}[res["status"]]


if __name__ == "__main__":
    sys.exit(main())
