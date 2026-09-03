#!/usr/bin/env python3
"""
财报历史实际波动（v0.45.101）
==============================
做什么
------
给「财报事件波动率信号」（earnings_vol_signal）提供分母：每只标的过去 N 次财报
**实际**发生的两日跳空幅度的中位数。信号本身拿隐含事件波动（期权价格里"买"到的
那部分）去除以它，得到 rich / cheap 判定。

两日窗口（BMO / AMC 不可分辨时的稳健口径）
------------------------------------------
财报日 D 的实际波动 = `post / pre − 1`，其中
    pre  = D 之前**最后一个**交易日的收盘
    post = D 之后**第一个**交易日的收盘
yfinance 给的财报日不带可靠的盘前/盘后标记（earnings_watcher 一律默认 AMC），
而 BMO 的反应在 D 当天、AMC 的反应在 D+1。两日窗口把 D 当天整个夹在中间：
    BMO：pre=D−1 收盘 → post=D+1 收盘，含 D 当天反应 + 一天后续漂移
    AMC：pre=D−1 收盘 → post=D+1 收盘，含 D 当天一天扩散 + D+1 反应
两种情形拿到的是**同一个数**，不必猜盘前盘后。代价是多掺了一个普通交易日的
扩散波动，这与 earnings_vol_signal 里的隐含事件波动是**同一方向的偏差**
（两边都略高估），比值受影响较小。

诚实降级（项目硬规则）
----------------------
- 取不到财报日期 → `None`，**不是 `[]`**（空列表会被下游当成"查过了、确实没有"）。
- 财报日前后缺 K 线（本地价格索引只有几个月、Twelve Data 未配置）→ 该次事件跳过，
  计入 `n_missing`；不用最近的一根去凑。
- pre→post 跨度超过 `_MAX_WINDOW_CAL_DAYS` 个日历日 → 视为数据有洞而非真实两日窗口，
  跳过。一次假期最多让窗口到 5 天；超过就是 K 线缺了。
- 可用事件 < 4 → `earnings_move_stats` 返回 `None`（原因写进日志与 `_moves.json`
  的 `reason` 字段，`earnings_move_stats_detail` 可读到）。4 个样本算中位数已经很勉强，
  再少就是在拿噪音当分母。
- 任何数字先过 `_num()`：NaN / Inf 一律变 None，`bool(nan) is True` 挡不住它。

缓存
----
`earnings_cache/{T}_history.json`（原始财报日期列表，30 天）
`earnings_cache/{T}_moves.json`（统计结果，30 天）。财报一季一次，30 天足够；
过期后重拉一次 yfinance（走 yf_gate）+ 一次 Twelve Data（800/天的预算里 30 只
每月各 1 次可忽略）。

没做的事
--------
- 不区分 BMO/AMC（见上）；
- 不做隔夜跳空 vs 当日振幅的拆分（要分钟线）；
- 不剔除财报日与其他大事件重叠的样本。
"""
from __future__ import annotations

import bisect
import math
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from hive_logger import PATHS, atomic_json_write, get_logger, pdt_today, read_json_cache

_log = get_logger("earnings_history")

HISTORY_TTL = 30 * 86400      # {T}_history.json
MOVES_TTL = 30 * 86400        # {T}_moves.json
MIN_EVENTS = 4                # 少于 4 个可用事件不给中位数
_MAX_WINDOW_CAL_DAYS = 7      # pre→post 超过 7 个日历日 = 数据有洞
_FETCH_LIMIT_FLOOR = 12       # yfinance 默认给 12 条（含未来），少于它不划算


def _num(v) -> Optional[float]:
    """float 且有限 → float；否则 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _resolve_cache_dir(cache_dir) -> Path:
    """相对路径挂到 PATHS.home（conftest 会把它重定向到 tmp_path）。"""
    p = Path(cache_dir) if cache_dir is not None else Path("earnings_cache")
    if not p.is_absolute():
        p = PATHS.home / p
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- 财报日期
def _gate_ensure() -> None:
    """单独拆出来方便测试打桩；生产里就是 yf_gate.ensure()。"""
    import yf_gate
    yf_gate.ensure()


def _fetch_earnings_dates_raw(ticker: str, limit: int) -> Optional[List[str]]:
    """打 yfinance（经闸门）拿财报日期，ISO 字符串、去重、降序；失败 None。

    走 `Ticker.get_earnings_dates()` 方法而非 `.earnings_dates` 属性：
    yf_gate 只包了方法表里的这一个名字，属性在内部也是调它，但直接调方法
    能让 `limit` 生效，少拉几行。
    """
    try:
        _gate_ensure()
        import yfinance as yf
        df = yf.Ticker(ticker).get_earnings_dates(limit=limit)
    except Exception as exc:  # noqa: BLE001 - 含 YFRateLimited；一律降级为 None
        _log.warning("[%s] 财报日期历史获取失败: %s", ticker, exc)
        return None
    if df is None:
        return None
    try:
        index = list(df.index)
    except Exception:  # noqa: BLE001
        return None
    out = set()
    for ts in index:
        try:
            if ts is None or (hasattr(ts, "strftime") and str(ts) == "NaT"):
                continue
            s = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            datetime.strptime(s, "%Y-%m-%d")
        except (ValueError, TypeError, AttributeError):
            continue
        out.add(s)
    return sorted(out, reverse=True) if out else None


def get_past_earnings_dates(ticker: str, n: int = 8, today: Optional[str] = None,
                            cache_dir=None) -> Optional[List[str]]:
    """严格早于 `today` 的财报日（ISO，最新在前，最多 n 个）；取不到 → None。

    缓存存**原始**列表（含未来日期），过滤在读取时做，所以同一份缓存对任何
    `today` 都正确。
    """
    ticker = ticker.upper()
    today = today or pdt_today()
    cdir = _resolve_cache_dir(cache_dir)
    path = cdir / f"{ticker}_history.json"
    cached = read_json_cache(path, HISTORY_TTL)
    dates: Optional[List[str]] = None
    if isinstance(cached, dict) and isinstance(cached.get("dates"), list):
        dates = [d for d in cached["dates"] if isinstance(d, str)]
    if not dates:
        dates = _fetch_earnings_dates_raw(ticker, max(_FETCH_LIMIT_FLOOR, n + 4))
        if not dates:
            return None
        try:
            atomic_json_write(path, {"ticker": ticker, "dates": dates,
                                     "source": "yfinance",
                                     "fetched_at": datetime.now().isoformat()})
        except (OSError, TypeError) as exc:
            _log.debug("[%s] 财报日期缓存写入失败: %s", ticker, exc)
    past = sorted((d for d in dates if d < today), reverse=True)
    return past[:n] if past else None


# ---------------------------------------------------------------- 两日窗口
def realized_earnings_moves(ticker: str, dates: List[str], bars: List[dict]) -> List[dict]:
    """每个财报日的两日窗口实际波动；缺 pre/post 任一根的事件直接跳过。

    `bars` 是升序 `[{date, close}]`。返回按财报日降序。
    """
    clean: List[Tuple[str, float]] = []
    for b in bars or []:
        try:
            d = str(b.get("date") or "")[:10]
            c = _num(b.get("close"))
        except AttributeError:
            continue
        if len(d) == 10 and c is not None and c > 0:
            clean.append((d, c))
    clean.sort()
    bar_dates = [d for d, _ in clean]
    out: List[dict] = []
    for ed in sorted({d for d in dates if isinstance(d, str)}, reverse=True):
        i_pre = bisect.bisect_left(bar_dates, ed) - 1        # 最后一个 < ed
        i_post = bisect.bisect_right(bar_dates, ed)          # 第一个 > ed
        if i_pre < 0 or i_post >= len(bar_dates):
            continue
        pre_d, pre_c = clean[i_pre]
        post_d, post_c = clean[i_post]
        try:
            span = (datetime.strptime(post_d, "%Y-%m-%d") - datetime.strptime(pre_d, "%Y-%m-%d")).days
        except ValueError:
            continue
        if span > _MAX_WINDOW_CAL_DAYS:
            _log.debug("[%s] %s 两日窗口跨 %d 天（%s→%s），视为缺 K 线跳过",
                       ticker, ed, span, pre_d, post_d)
            continue
        move = (post_c / pre_c - 1.0) * 100.0
        if not math.isfinite(move):
            continue
        out.append({
            "earnings_date": ed,
            "pre_date": pre_d, "pre_close": round(pre_c, 4),
            "post_date": post_d, "post_close": round(post_c, 4),
            "move_pct": round(move, 4),
            "abs_move_pct": round(abs(move), 4),
        })
    return out


# ---------------------------------------------------------------- K 线来源
def _load_bars(ticker: str) -> Tuple[Optional[List[dict]], str]:
    """Twelve Data（已配置时，约 2 年）→ 本地价格索引（几个月）；都没有 → (None, "none")。"""
    try:
        import twelve_data
        if twelve_data.is_configured():
            rows = twelve_data._fetch_rows(ticker, 600)
            if rows:
                return [{"date": r.get("date"), "close": r.get("close")} for r in rows], "twelve_data"
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] Twelve Data 日线获取失败，退回本地索引: %s", ticker, exc)
    try:
        from price_history import load_price_history
        hist = load_price_history(ticker, str(PATHS.cache_dir))
        if hist:
            return [{"date": d, "close": c} for d, c in hist], "price_history"
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] 本地价格索引读取失败: %s", ticker, exc)
    return None, "none"


# ---------------------------------------------------------------- 统计
def _stats_from_moves(ticker: str, dates: List[str], moves: List[dict],
                      source: str) -> dict:
    abs_moves = [m["abs_move_pct"] for m in moves]
    usable = len(abs_moves) >= MIN_EVENTS
    if usable:
        s = sorted(abs_moves)
        k = len(s)
        median = s[k // 2] if k % 2 else (s[k // 2 - 1] + s[k // 2]) / 2.0
        mean = sum(s) / k
        mx = s[-1]
        reason = None
    else:
        median = mean = mx = None
        reason = f"only {len(abs_moves)} usable events (< {MIN_EVENTS})"
    return {
        "ticker": ticker,
        "n": len(abs_moves),
        "n_missing": max(0, len(dates) - len(abs_moves)),
        "usable": usable,
        "reason": reason,
        "median_abs_move_pct": round(median, 4) if median is not None else None,
        "mean_abs_move_pct": round(mean, 4) if mean is not None else None,
        "max_abs_move_pct": round(mx, 4) if mx is not None else None,
        "moves": moves,
        "source": source,
        "computed_at": datetime.now().isoformat(),
    }


def earnings_move_stats_detail(ticker: str, n: int = 8, today: Optional[str] = None,
                               cache_dir="earnings_cache",
                               bars_fn: Optional[Callable[[str], Tuple[Optional[List[dict]], str]]] = None,
                               ) -> Optional[dict]:
    """完整统计 dict（含 `usable=False` 的），硬失败（无日期 / 无 K 线）→ None。"""
    ticker = ticker.upper()
    cdir = _resolve_cache_dir(cache_dir)
    path = cdir / f"{ticker}_moves.json"
    cached = read_json_cache(path, MOVES_TTL)
    if isinstance(cached, dict) and "n" in cached:
        return cached

    dates = get_past_earnings_dates(ticker, n=n, today=today, cache_dir=cdir)
    if not dates:
        _log.info("[%s] 无历史财报日期，跳过实际波动统计", ticker)
        return None
    bars, source = (bars_fn or _load_bars)(ticker)
    if not bars:
        _log.info("[%s] 无 K 线可用（source=%s），跳过实际波动统计", ticker, source)
        return None
    moves = realized_earnings_moves(ticker, dates, bars)
    stats = _stats_from_moves(ticker, dates, moves, source)
    if not stats["usable"]:
        _log.info("[%s] 财报实际波动样本不足: %s（n_missing=%d, source=%s）",
                  ticker, stats["reason"], stats["n_missing"], source)
    try:
        atomic_json_write(path, stats)
    except (OSError, TypeError) as exc:
        _log.debug("[%s] moves 缓存写入失败: %s", ticker, exc)
    return stats


def earnings_move_stats(ticker: str, n: int = 8, today: Optional[str] = None,
                        cache_dir="earnings_cache", bars_fn=None) -> Optional[dict]:
    """`{ticker, n, n_missing, median_abs_move_pct, mean_abs_move_pct, max_abs_move_pct,
    moves, source, computed_at}`；可用事件 < MIN_EVENTS 或硬失败 → None。"""
    d = earnings_move_stats_detail(ticker, n=n, today=today, cache_dir=cache_dir, bars_fn=bars_fn)
    if not d or not d.get("usable"):
        return None
    return d


if __name__ == "__main__":
    import json
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    print(json.dumps(earnings_move_stats_detail(tk), ensure_ascii=False, indent=2))
