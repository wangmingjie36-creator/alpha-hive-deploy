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
扩散波动。

⚠️ v0.45.105 更正：这里原先写的是「这与隐含事件波动是同一方向的偏差、比值受影响
较小」——**说反了**。隐含那一侧把 dte 天的扩散**整个减掉**（得到纯事件），而这一侧
**保留**了 2 个交易日的扩散：分子偏小、分母偏大，两个偏差不是抵消而是复利。
蒙特卡洛实测（σ_ann 30%，20 万条路径）本窗口对中位数的虚高：真实事件 sd 3% 时
**+13.57%**、5% 时 +6.18%、8% 时 +2.50%。
窗口本身不改（不猜盘前盘后是刻意的安全选择），改的是隐含那一侧——
`earnings_vol_signal` 自 v0.45.105 起把同样长度（`realized_window_trading_days`）
的扩散加回分子，使两边都等于「事件 + 2 个交易日扩散」。
**改本窗口长度必须同时改那个配置项**，否则两边又错开。

诚实降级（项目硬规则）
----------------------
- 取不到财报日期 → `None`，**不是 `[]`**（空列表会被下游当成"查过了、确实没有"）。
- 财报日前后缺 K 线（本地价格索引只有几个月、Twelve Data 未配置）→ 该次事件跳过，
  计入 `n_missing`；不用最近的一根去凑。
- pre / ed / post 三根 K 线在序列里必须**相邻**（索引距离正好 2）→ 否则视为数据有洞，
  跳过。日历日闸（`_MAX_WINDOW_CAL_DAYS`）挡不住真正的洞：缺两个交易日的窗口
  日历跨度可能只有 6 天，照样会被当成"两日窗口"收下（v0.45.104 修）。两道闸都保留。
- 可用事件 < 4 → `earnings_move_stats` 返回 `None`（原因写进日志与 `_moves.json`
  的 `reason` 字段，`earnings_move_stats_detail` 可读到）。4 个样本算中位数已经很勉强，
  再少就是在拿噪音当分母。
- 任何数字先过 `_num()`：NaN / Inf 一律变 None，`bool(nan) is True` 挡不住它。

缓存
----
`earnings_cache/{T}_history.json`（原始财报日期列表，30 天）
`earnings_cache/{T}_moves.json`（统计结果，30 天）。两份都按 ticker 存、不含日期，
所以**读出来必须按当次的 `today` 再过滤一次**：history 一直是这么做的（存原始、读时滤），
moves 以前不是，回补时会把当时还没发生的财报算进分母（look-ahead，v0.45.104 修）。
财报一季一次，30 天足够；
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
        # L2（v0.45.104）：一次**成功但被截断**的取数（限流/上游只给了两三条）不能
        # 进 30 天缓存——它会把这只票封成"没有历史"整整一个月，长得和真的没有一模一样。
        # 至少要够 MIN_EVENTS 个过去事件 + 1 个未来事件才算一份可用的历史。
        if len(dates) < MIN_EVENTS + 1:
            _log.info("[%s] 财报日期只拿到 %d 条（< %d），不写缓存，下次重试",
                      ticker, len(dates), MIN_EVENTS + 1)
        else:
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
        # M5（v0.45.104）：两日窗口 = pre / ed / post 三根**相邻** K 线，索引距离正好 2。
        # 日历日闸（≤7 天）是近似的，挡不住真实的数据洞：只有 04-08 与 04-14 两根、
        # 财报在 04-13 时跨度 6 天 ≤ 7，会被当成两日窗口收下，实际是 4 个交易日的漂移
        # 被贴上 abs_move_pct 的标签。距离 ≠ 2 有两种成因（ed 当天 K 线缺了、
        # ed 不是交易日），两种都无法证明 pre/post 紧挨着事件，一律跳过。
        if i_post - i_pre != 2:
            _log.debug("[%s] %s 前后 K 线索引距离 %d（应为 2，%s→%s），视为缺 K 线跳过",
                       ticker, ed, i_post - i_pre, bar_dates[i_pre], bar_dates[i_post])
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
        # dates 落盘是 M2 重过滤的前提：只有知道当时用了哪些财报日，才能按新的 today
        # 把未来事件从分子分母里一起摘掉。
        "dates": sorted({d for d in dates if isinstance(d, str)}, reverse=True),
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


def _refilter_for_today(cached: dict, today: str) -> Optional[dict]:
    """把按 ticker 存的 moves 缓存按当次 `today` 再过滤一次；旧格式（无 dates）→ None。

    M2（v0.45.104）：`{T}_moves.json` 存的是**写缓存那天**过滤出来的统计，键里没有日期，
    TTL 30 天。回补（`--date`）或手动重跑时 today 往前挪，直接返回缓存就等于把当时
    还没发生的财报算进了分母——look-ahead。`get_past_earnings_dates` 没这个毛病
    （它缓存原始列表、读时才过滤），坏的只有缓存**已过滤结果**的这一层。
    返回 None = 无法安全重过滤，调用方当缓存未命中处理（不猜、不将就）。
    """
    dates, moves = cached.get("dates"), cached.get("moves")
    if not isinstance(dates, list) or not isinstance(moves, list):
        return None
    past_dates = [d for d in dates if isinstance(d, str) and d < today]
    past_moves = [m for m in moves if isinstance(m, dict)
                  and str(m.get("earnings_date") or "") < today]
    if len(past_dates) == len(dates) and len(past_moves) == len(moves):
        return cached
    out = _stats_from_moves(cached.get("ticker") or "", past_dates, past_moves,
                            cached.get("source") or "cache")
    out["computed_at"] = cached.get("computed_at")
    out["refiltered_for"] = today
    return out


def earnings_move_stats_detail(ticker: str, n: int = 8, today: Optional[str] = None,
                               cache_dir="earnings_cache",
                               bars_fn: Optional[Callable[[str], Tuple[Optional[List[dict]], str]]] = None,
                               ) -> Optional[dict]:
    """完整统计 dict（含 `usable=False` 的），硬失败（无日期 / 无 K 线）→ None。"""
    ticker = ticker.upper()
    today = today or pdt_today()
    cdir = _resolve_cache_dir(cache_dir)
    path = cdir / f"{ticker}_moves.json"
    cached = read_json_cache(path, MOVES_TTL)
    if isinstance(cached, dict) and "n" in cached:
        refiltered = _refilter_for_today(cached, today)
        if refiltered is not None:
            return refiltered
        _log.info("[%s] moves 缓存是旧格式（无 dates），无法按 today=%s 重过滤，重算",
                  ticker, today)

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
