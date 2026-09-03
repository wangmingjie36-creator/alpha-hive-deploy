#!/usr/bin/env python3
"""
财报事件波动率信号（v0.45.101，期权路线图第 3 步）
==================================================
做什么
------
把每日 options_snapshot 里的 `quote_set`（v0.45.99，~30 DTE 四合约真实 CBOE 报价）
和财报历史实际波动（earnings_history）放到一起，问一个可证伪的问题：

    期权市场为**即将到来的这次财报**定价的跳空幅度，比这只票过去 8 次财报
    实际跳的中位数，贵还是便宜？

    ratio = implied_event_move_pct / median_abs_move_pct
    ≥ rich_ratio  → "rich"  （卖跨式候选）
    ≤ cheap_ratio → "cheap" （买跨式候选）
    其余          → "fair"

隐含事件波动怎么算
------------------
ATM 跨式中价 / S 是到期前**全部**波动的价格：财报跳空 + 到期前每天的普通扩散。
用 30 日已实现波动把扩散那部分扣掉：
    diffusion_move_pct = 0.8 · σ_rv · √(dte/252) · 100        （0.8σ√T ≈ ATM 跨式）
    implied_event_move_pct = √(max(straddle² − diffusion², 0))
`rv_30d` 缺失时退回 `straddle_move_pct` 本身并标 `event_move_basis="raw_straddle"`：
它**含扩散**，会高估事件波动、把 ratio 往 rich 推——读 ratio 前先看 basis。
（earnings_history 的两日窗口同样多掺一天扩散，两边偏差同向，比值受影响较小。）

合格条件（缺一个就 `eligible=False` 并给 `reason`）
--------------------------------------------------
1. `quote_set.data_available` 且 ATM call/put 两腿 `quote_ok`（bid>0、ask≥bid、有限）
2. 有下次财报日，且 `as_of < earnings_date <= selected_expiry`——跨式必须**跨过**事件，
   到期日在财报前的跨式定价的是别的东西
3. 历史事件 ≥ `min_events`

可交易性单独判：两腿任一 `spread_pct > max_spread_pct` → `label="untradeable"`，
真实标签保留在 `raw_label`。依据 2026-09-03 实盘核对：COST 25Δ 合约点差 27.6%，
NVDA 只有 4.4%——前者的"中价"是两个相隔 28% 的报价的算术平均，按它成交是幻觉。

诚实降级
--------
- 全程 None 代替 NaN（`bool(nan) is True`，比较也全 False，守卫挡不住）。
- 不合格也返回能算出的全部中间量，方便事后看"差在哪一步"。
- 信号落盘 `options_paper_state/earnings_signals.jsonl`，同 (ticker, as_of) 幂等。
- `settle_signals` 在财报过后用同一两日窗口回填实际波动，这是**测量管道**，
  为以后的校准 / IC 研究攒样本；`summary_stats` 在 n<3 时给 None 而不是均值。

没做的事
--------
- 没有 delta 对冲；ATM 跨式入场时近似 delta 中性，之后随价格漂移（已知局限，v1 不处理）。
- 没有把信号接进任何评分 / IC / 纸面股票组合；唯一消费者是 options_paper_leg 的独立账本。
- 阈值 1.30 / 0.75 是先验，不是校准结果——攒够 settle 样本再谈。
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

from hive_logger import PATHS, get_logger

_log = get_logger("earnings_vol_signal")

CONFIG = {
    "rich_ratio": 1.30,       # implied/historical ≥ 1.30 → rich（卖跨式候选）
    "cheap_ratio": 0.75,      # ≤ 0.75 → cheap（买跨式候选）
    "min_events": 4,          # 历史事件下限
    "min_quote_ok": True,     # ATM 两腿必须 quote_ok（关掉 = 允许 bid=0 的半边报价，别关）
    "max_spread_pct": 0.15,   # 任一腿点差 > 15% → untradeable（COST 25Δ 27.6% vs NVDA 4.4%）
    "history_n": 8,           # 取过去 8 次财报
    "diffusion_k": 0.8,       # 0.8σ√T ≈ ATM 跨式
}

BASE_DIR = PATHS.home
STATE_DIR = BASE_DIR / "options_paper_state"
SIGNALS_FILE = STATE_DIR / "earnings_signals.jsonl"

_SNAP_RE = re.compile(r"options_snapshot_(.+)_(\d{4}-\d{2}-\d{2})\.json$")


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, nd=4) -> Optional[float]:
    return round(v, nd) if v is not None else None


def _resolve_dir(p, default_name: str) -> Path:
    path = Path(p) if p is not None else Path(default_name)
    return path if path.is_absolute() else BASE_DIR / path


# ---------------------------------------------------------------- 状态文件
def _load_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _atomic_write_text(path: Path, content: str) -> None:
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
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


def _write_jsonl(path: Path, records: List[Dict]) -> None:
    _atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def load_signals() -> List[Dict]:
    return _load_jsonl(SIGNALS_FILE)


def signals_for_date(as_of: str) -> List[Dict]:
    return [s for s in load_signals() if s.get("as_of") == as_of]


# ---------------------------------------------------------------- 核心计算
def _leg(c: Optional[dict]) -> Optional[dict]:
    """把 quote_set 合约压成账本需要的最小形状；数值全过 _num。"""
    if not isinstance(c, dict):
        return None
    return {
        "symbol": c.get("symbol"),
        "strike": _num(c.get("strike")),
        "expiry": c.get("expiry"),
        "bid": _num(c.get("bid")),
        "ask": _num(c.get("ask")),
        "mid": _num(c.get("mid")),
        "spread_pct": _num(c.get("spread_pct")),
        "iv": _num(c.get("iv")),
        "delta": _num(c.get("delta")),
        "quote_ok": bool(c.get("quote_ok")),
    }


def _precheck_quotes(quote_set: Optional[dict]) -> Optional[str]:
    """只看报价的判死条件（零网络成本）；返回 reason 或 None。"""
    if not isinstance(quote_set, dict) or not quote_set.get("data_available"):
        return "quote_set unavailable"
    contracts = quote_set.get("contracts") or {}
    ac, ap = contracts.get("atm_call"), contracts.get("atm_put")
    if not isinstance(ac, dict) or not isinstance(ap, dict):
        return "atm leg missing"
    if CONFIG["min_quote_ok"] and not (ac.get("quote_ok") and ap.get("quote_ok")):
        return "atm leg quote not ok"
    if _num(quote_set.get("implied_move_pct")) is None or _num(quote_set.get("atm_straddle_mid")) is None:
        return "straddle mid unavailable"
    if not quote_set.get("selected_expiry"):
        return "no selected expiry"
    return None


def _precheck(as_of: str, quote_set: Optional[dict], upcoming: Optional[dict]) -> Optional[str]:
    """不需要历史统计就能判死的条件；返回 reason 或 None（通过）。

    scan() 先跑它，通过了才去拉财报历史——大多数标的当天没有跨过到期日的财报，
    没必要为它们各打一次 yfinance + Twelve Data。
    """
    reason = _precheck_quotes(quote_set)
    if reason:
        return reason
    ed = (upcoming or {}).get("earnings_date") if isinstance(upcoming, dict) else None
    if not ed:
        return "no upcoming earnings date"
    expiry = quote_set.get("selected_expiry")
    if not (as_of < ed <= expiry):
        return f"earnings {ed} not within (as_of, expiry={expiry}]"
    return None


def compute_signal(ticker: str, as_of: str, quote_set: Optional[dict],
                   upcoming: Optional[dict], stats: Optional[dict],
                   rv_30d) -> dict:
    """纯函数：一只票一天的信号，所有中间量都在返回值里；不合格给 reason。"""
    qs = quote_set if isinstance(quote_set, dict) else {}
    contracts = qs.get("contracts") or {}
    call, put = _leg(contracts.get("atm_call")), _leg(contracts.get("atm_put"))
    ed = (upcoming or {}).get("earnings_date") if isinstance(upcoming, dict) else None
    sig: Dict = {
        "ticker": ticker.upper(), "as_of": as_of,
        "eligible": False, "reason": None,
        "label": None, "raw_label": None, "tradeable": None, "untradeable_reason": None,
        "earnings_date": ed,
        "earnings_time": (upcoming or {}).get("earnings_time") if isinstance(upcoming, dict) else None,
        "selected_expiry": qs.get("selected_expiry"), "dte": _num(qs.get("selected_dte")),
        "underlying_price": _num(qs.get("underlying_price")),
        "atm_strike": call.get("strike") if call else None,
        "atm_straddle_mid": _num(qs.get("atm_straddle_mid")),
        "straddle_move_pct": _num(qs.get("implied_move_pct")),
        "rv_30d": _num(rv_30d),
        "diffusion_move_pct": None, "implied_event_move_pct": None,
        "event_move_basis": None, "event_move_floor_hit": False,
        "hist_n": int(stats.get("n") or 0) if isinstance(stats, dict) else 0,
        "hist_median_abs_move_pct": _num((stats or {}).get("median_abs_move_pct")) if isinstance(stats, dict) else None,
        "hist_mean_abs_move_pct": _num((stats or {}).get("mean_abs_move_pct")) if isinstance(stats, dict) else None,
        "hist_source": (stats or {}).get("source") if isinstance(stats, dict) else None,
        "ratio": None, "max_leg_spread_pct": None,
        "quote": {"call": call, "put": put},
        "quote_fetched_at": qs.get("fetched_at"), "market_open": qs.get("market_open"),
        # settle_signals 回填
        "realized_abs_move_pct": None, "realized_ratio": None, "settled_on": None,
    }

    reason = _precheck(as_of, qs, upcoming)
    if reason:
        sig["reason"] = reason
        return sig
    if not isinstance(stats, dict) or sig["hist_n"] < CONFIG["min_events"]:
        sig["reason"] = f"insufficient earnings history (n={sig['hist_n']} < {CONFIG['min_events']})"
        return sig
    median = sig["hist_median_abs_move_pct"]
    if median is None or median <= 0:
        sig["reason"] = "historical median move unavailable"
        return sig

    straddle = sig["straddle_move_pct"]
    dte = sig["dte"]
    rv = sig["rv_30d"]
    if rv is not None and rv > 0 and dte is not None and dte > 0:
        diffusion = CONFIG["diffusion_k"] * (rv / 100.0) * math.sqrt(dte / 252.0) * 100.0
        event2 = straddle * straddle - diffusion * diffusion
        sig["diffusion_move_pct"] = _r(diffusion)
        sig["event_move_floor_hit"] = event2 <= 0
        sig["implied_event_move_pct"] = _r(math.sqrt(max(event2, 0.0)))
        sig["event_move_basis"] = "straddle_minus_diffusion"
    else:
        sig["implied_event_move_pct"] = _r(straddle)
        sig["event_move_basis"] = "raw_straddle"   # 含扩散，高估事件波动

    ratio = sig["implied_event_move_pct"] / median
    if not math.isfinite(ratio):
        sig["reason"] = "ratio not finite"
        return sig
    sig["ratio"] = _r(ratio)
    if ratio >= CONFIG["rich_ratio"]:
        raw = "rich"
    elif ratio <= CONFIG["cheap_ratio"]:
        raw = "cheap"
    else:
        raw = "fair"
    sig["raw_label"] = raw

    spreads = [x for x in (call["spread_pct"], put["spread_pct"]) if x is not None]
    max_spread = max(spreads) if spreads else None
    sig["max_leg_spread_pct"] = _r(max_spread)
    if sig["event_move_floor_hit"]:
        # 跨式价低于纯扩散估计 ⇒ 事件波动被压成 0，ratio 必然 "cheap"。这不是便宜，
        # 是 rv_30d 含了别的跳空把扩散项撑大了——退化数字不能下注，留给校准研究看。
        sig["tradeable"] = False
        sig["untradeable_reason"] = "event move floored (diffusion >= straddle)"
    elif max_spread is None:
        sig["tradeable"] = False
        sig["untradeable_reason"] = "spread unknown"
    elif max_spread > CONFIG["max_spread_pct"]:
        sig["tradeable"] = False
        sig["untradeable_reason"] = f"leg spread {max_spread:.1%} > {CONFIG['max_spread_pct']:.0%}"
    else:
        sig["tradeable"] = True
    sig["label"] = raw if sig["tradeable"] else "untradeable"
    sig["eligible"] = True
    return sig


# ---------------------------------------------------------------- 扫描当日快照
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
        if not isinstance(snap, dict):
            continue
        yield m.group(1).upper(), snap


def scan(as_of: str, cache_dir="cache", earnings_cache_dir="earnings_cache",
         watcher=None, stats_fn: Optional[Callable[[str], Optional[dict]]] = None) -> List[dict]:
    """当日全部常规快照 → 信号列表，并幂等落盘（先删该日旧行再追加）。"""
    cdir = _resolve_dir(cache_dir, "cache")
    ecdir = _resolve_dir(earnings_cache_dir, "earnings_cache")

    def _default_stats(tk: str) -> Optional[dict]:
        import earnings_history
        return earnings_history.earnings_move_stats(tk, n=CONFIG["history_n"], today=as_of,
                                                    cache_dir=ecdir)

    def _upcoming(tk: str) -> Optional[dict]:
        nonlocal watcher
        try:
            if watcher is None:
                from earnings_watcher import EarningsWatcher
                watcher = EarningsWatcher()
            return watcher.get_earnings_date(tk)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 下次财报日获取失败: %s", tk, exc)
            return None

    stats_fn = stats_fn or _default_stats
    signals: List[dict] = []
    for ticker, snap in _iter_snapshots(cdir, as_of):
        qs = snap.get("quote_set")
        rv = snap.get("rv_30d")
        upcoming = None
        stats = None
        # 先用零成本条件判死，通过了才去打网
        if _precheck_quotes(qs) is None:
            upcoming = _upcoming(ticker)
            if _precheck(as_of, qs, upcoming) is None:
                try:
                    stats = stats_fn(ticker)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("[%s] 财报历史统计失败: %s", ticker, exc)
                    stats = None
        try:
            signals.append(compute_signal(ticker, as_of, qs, upcoming, stats, rv))
        except Exception as exc:  # noqa: BLE001 - 单票失败不拖死整轮
            _log.warning("[%s] 信号计算失败: %s", ticker, exc, exc_info=True)

    existing = [s for s in load_signals() if s.get("as_of") != as_of]
    _write_jsonl(SIGNALS_FILE, existing + signals)
    n_el = sum(1 for s in signals if s.get("eligible"))
    _log.info("财报事件波动率信号 %s：快照 %d，合格 %d（%s）", as_of, len(signals), n_el,
              ", ".join(f"{s['ticker']}={s['label']}" for s in signals if s.get("eligible")) or "-")
    return signals


# ---------------------------------------------------------------- 事后回填
def _default_bars(ticker: str) -> Optional[List[dict]]:
    import earnings_history
    bars, _src = earnings_history._load_bars(ticker)
    return bars


def settle_signals(as_of: str, bars_fn: Optional[Callable[[str], Optional[List[dict]]]] = None) -> int:
    """财报已过（`earnings_date < as_of`）且未回填的信号：用同一两日窗口填实际波动。
    post 根还没出来的留到下次。返回本次回填条数。"""
    from earnings_history import realized_earnings_moves
    bars_fn = bars_fn or _default_bars
    rows = load_signals()
    pending = defaultdict(list)
    for s in rows:
        ed = s.get("earnings_date")
        if (s.get("eligible") and ed and ed < as_of and s.get("realized_abs_move_pct") is None
                and _num(s.get("implied_event_move_pct")) is not None):
            pending[s["ticker"]].append(s)
    if not pending:
        return 0
    n = 0
    for ticker, sigs in pending.items():
        try:
            bars = bars_fn(ticker)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] settle 取 K 线失败: %s", ticker, exc)
            continue
        if not bars:
            continue
        moves = {m["earnings_date"]: m for m in
                 realized_earnings_moves(ticker, sorted({s["earnings_date"] for s in sigs}), bars)}
        for s in sigs:
            m = moves.get(s["earnings_date"])
            if not m:
                continue
            implied = _num(s.get("implied_event_move_pct"))
            s["realized_abs_move_pct"] = m["abs_move_pct"]
            s["realized_move_pct"] = m["move_pct"]
            s["realized_ratio"] = _r(m["abs_move_pct"] / implied) if implied and implied > 0 else None
            s["settled_on"] = as_of
            n += 1
    if n:
        _write_jsonl(SIGNALS_FILE, rows)
        _log.info("财报信号回填 %d 条（%s）", n, as_of)
    return n


def summary_stats(min_n: int = 3) -> dict:
    """已回填信号的实际/隐含比值，按 label 分组；n < min_n 的组均值给 None。"""
    rows = load_signals()
    settled = [s for s in rows if s.get("realized_ratio") is not None]
    by: Dict[str, List[float]] = defaultdict(list)
    for s in settled:
        by[s.get("raw_label") or "unknown"].append(float(s["realized_ratio"]))
    out = {"n_signals": len(rows), "n_eligible": sum(1 for s in rows if s.get("eligible")),
           "n_settled": len(settled), "by_label": {}}
    for label, vals in by.items():
        out["by_label"][label] = {
            "n": len(vals),
            "mean_realized_ratio": _r(sum(vals) / len(vals)) if len(vals) >= min_n else None,
        }
    allv = [v for vs in by.values() for v in vs]
    out["mean_realized_ratio"] = _r(sum(allv) / len(allv)) if len(allv) >= min_n else None
    return out


if __name__ == "__main__":
    import sys
    from hive_logger import pdt_today
    d = sys.argv[1] if len(sys.argv) > 1 else pdt_today()
    for s in scan(d):
        print(f"{s['ticker']:6s} eligible={s['eligible']!s:5s} label={s['label']} ratio={s['ratio']} "
              f"reason={s['reason']}")
    print(json.dumps(summary_stats(), ensure_ascii=False, indent=2))
