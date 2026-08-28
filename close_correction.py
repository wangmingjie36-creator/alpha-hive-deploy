#!/usr/bin/env python3
"""
🐝 Alpha Hive — 收盘价校正 (v0.45.41)
======================================
`price_at_predict` 被**盘后价**污染，用官方收盘回填修正。

本工具在防线里的位置（三者互补，别混为一谈）
--------------------------------------------
| | 管什么 | 时机 |
|---|---|---|
| `cboe_options.official_price`（v0.45.46） | **源头**：收盘后取 `close` 不取 `current_price` | 写入前 |
| `scan_coverage_gate.check_prices`（v0.45.45） | **单日闸**：当天入库价 vs 真实收盘 | 写入后当天 |
| **本工具**（v0.45.41） | **历史补救**：跨全库校正已污染的行，双源交叉、原值留痕 | 事后 |

v0.45.46 堵住源头之后，新数据不会再被污染；本工具处理的是**它上线之前**
已经写进库的那批（实测 1017 条里 95 条）。⚠️ 与 `check_prices` 的
yfinance 取数逻辑目前各有一份实现（并行发明，非抄袭）—— 可合并，
但那要动另一条线的文件，留作后续。

为什么需要
----------
扫描跑在 14:00 PDT = **17:00 ET**，正处盘后时段（16:00–20:00 ET）正中间。
CBOE payload 的 `current_price` **跟着盘后交易走**，而 `last_trade_time`
钉死在 16:00 收盘 —— 所以 v0.45.39 的 vintage 校验对它**完全无效**：
日期是今天、成交时刻是收盘，一切「合规」，价格却是盘后价。
校验的判据和被污染的字段根本不是同一个东西。

2026-08-26 实测（云端快照 30 只对账官方收盘）：**16 只偏离 >0.2%**，
其中 CRM 偏 **+13.80%**（快照 234.0 / 官方收盘 205.62）—— 当天 CRM 发财报
（yfinance `Earnings Date: 2026-08-26` 确认），盘后涨约 12%。

**它恰好在财报日最狠**，而那正是信息量最大的日子：评分用的是财报前的数据，
价格基准却已吃掉财报后那根 K 线，`close_t7/price_at_predict − 1` 的起点
含了本该被测量的那段收益 —— 评分与收益的联系在最该成立的日子里断掉。

判据来源：两个独立源互相印证
-----------------------------
- **yfinance 官方收盘**：批量下载，30 只一次请求。与逐标的调用是两回事，
  不会触发限流雪崩（2026-08-26 扫描逐标的调用触发 487 次限流，
  同一天批量下载全程正常）。
- **CBOE `prev_day_close`**：仅 T+1 可得。实测与官方收盘分毫不差
  （CRM 205.69 = yfinance 8/25 收盘）。取用前**必须过 vintage 校验** ——
  CDN 卡死的符号（实测 TMO 卡 44.5 小时）其 `prev_day_close` 指的是更早的日子。

两源都在且分歧 > `DISPUTE_TOL` → **不修**，记入 disputed 待查。
只有一个 → 修，但 source 标明单源。一个都没有 → 不动。

留痕
----
原值存进 `price_at_predict_raw`（仅首次校正时写入，重复运行不会覆盖），
`close_corrected_at` / `close_correction_source` 记录来源。幂等。

⚠️ 校正后必须重算派生列
-----------------------
`return_t7` / `dir_correct_t7` / `net_return_t7` 等都以 `price_at_predict`
为起点。本脚本**只改基准价，不动派生列** —— 跑完请接：

    /usr/local/bin/python3 backfill_dir_accuracy.py --all

用法
----
    python3 close_correction.py                    # dry-run，只报不改
    python3 close_correction.py --apply            # 落笔
    python3 close_correction.py --apply --since 2026-08-01
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_log = logging.getLogger("alpha_hive.close_correction")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pheromone.db")

# 低于此偏离视为同一个价，不动（浮点/复权噪声）
CORRECT_TOL = 0.001
# 两个来源分歧超过此值 → 拒绝校正，留给人看
DISPUTE_TOL = 0.002

_NEW_COLUMNS = (
    ("price_at_predict_raw", "REAL"),
    ("close_corrected_at", "TEXT"),
    ("close_correction_source", "TEXT"),
)


def ensure_columns(conn: sqlite3.Connection) -> None:
    """幂等加列。与 backtester._migrate_options_columns 同模式。"""
    for col, typ in _NEW_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass


def load_rows(conn: sqlite3.Connection, since: Optional[str] = None) -> List[dict]:
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, date, ticker, price_at_predict, price_at_predict_raw, "
           "close_corrected_at FROM predictions WHERE price_at_predict > 0")
    args: List = []
    if since:
        sql += " AND date >= ?"
        args.append(since)
    return [dict(r) for r in conn.execute(sql, args)]


def official_closes(tickers: List[str], lo: str, hi: str) -> Dict[Tuple[str, str], float]:
    """yfinance 批量官方收盘 → {(date, ticker): close}。失败返回空表（诚实缺失）。

    刻意用 `yf.download` 批量而非逐标的 `Ticker.history`：一次请求覆盖全部标的，
    这正是它在扫描被限流的同一天仍然可用的原因。
    """
    import datetime as dt
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError as e:
        _log.error("yfinance/pandas 不可得：%s", e)
        return {}
    # ⚠️ 起点**往前垫 10 个自然日**：非交易日样本要回退到「该日之前最近的
    # 交易日」，若下载区间恰好从那个非交易日开始，前一交易日就不在数据里 ——
    # 该行会被静默记成「无来源」而不是被校正。实测触发条件：
    # `--since 2026-03-01`（周日）。全量跑靠「最早预测日 2/27 早于最早周日 3/01」
    # 侥幸安全，不能依赖。
    start = (dt.date.fromisoformat(lo) - dt.timedelta(days=10)).isoformat()
    end = (dt.date.fromisoformat(hi) + dt.timedelta(days=1)).isoformat()
    try:
        h = yf.download(sorted(set(tickers)), start=start, end=end,
                        progress=False, auto_adjust=False)["Close"]
    except Exception as e:  # noqa: BLE001 - 拿不到就空表，调用方据此不动数据
        _log.error("yfinance 批量下载失败：%s: %s", type(e).__name__, e)
        return {}
    if hasattr(h, "to_frame") and not hasattr(h, "columns"):
        h = h.to_frame(name=sorted(set(tickers))[0])
    out: Dict[Tuple[str, str], float] = {}
    for d, row in h.iterrows():
        ds = d.strftime("%Y-%m-%d")
        for t in h.columns:
            v = row[t]
            # ⚠️ 必须 `> 0`：0 不是「零元」，是**没有这个价**。
            # 它会一路当成合法收盘价流到 `base / truth` 造成 ZeroDivisionError
            # （构造检验确认）。与 v0.45.42「缺失值不许冒充 0」同一条原则。
            if v is not None and not pd.isna(v) and float(v) > 0:
                out[(ds, t)] = float(v)
    return out


_TRADING_DAY_CACHE: Dict[str, bool] = {}


def _is_trading_date(d: str) -> bool:
    """该日期是不是美股交易日。判不了一律按**是**处理 —— 更严格的方向。

    按交易日处理意味着「必须命中当天收盘」，取不到就不动数据；
    反过来按非交易日处理会允许回退到更早的收盘，那才是危险的默认值。
    """
    if d not in _TRADING_DAY_CACHE:
        try:
            import datetime as _dt
            from is_trading_day import is_trading_day
            _TRADING_DAY_CACHE[d] = bool(is_trading_day(_dt.date.fromisoformat(d))[0])
        except Exception:  # noqa: BLE001 - 见 docstring
            _TRADING_DAY_CACHE[d] = True
    return _TRADING_DAY_CACHE[d]


def _resolve_close(closes: Dict, avail: List[str], date: str, ticker: str):
    """取该预测日该用的收盘价 → `(close, 取自哪天)`；取不到返回 `(None, None)`。

    **交易日：必须命中当天，不许回退。** yfinance 偶发缺一天时回退会静默把
    前一日收盘当成当日收盘 —— 正是本工具要治的那种污染，方向还反了。

    **非交易日：取该日之前最近一个交易日的收盘。** 周日样本是已退役的
    `sample-accumulator` 的产物（周日 18:00 跑），它当时能拿到的最新价本来就是
    上周五收盘。这也是 `backfill_dir_accuracy.py:196`（`_close_at_or_before`）
    一直在用的口径 —— 两处对齐，不是新发明。

    实测（110 条非交易日样本）：108 条本就与前一交易日收盘一致，
    只有 CRWD 的 2 条不符 —— 那是 2026-07-02 的 4:1 拆股，库存的是拆股前
    未复权价（448.13 / 527.77），而 `close_t7` 是复权序列算的。
    """
    # ⚠️ 判据是 `> 0` 而不是 `is not None`：0 不是「零元」，是**没有这个价**。
    # 纵深防御 —— `official_closes` 已在取数层滤过，但本函数也可能被喂进
    # 别处来的 closes 表；漏过去会一路流到 `current / truth` 直接除零。
    exact = closes.get((date, ticker))
    if exact is not None and exact > 0:
        return exact, date
    if _is_trading_date(date):
        return None, None                     # 交易日缺数 → 不猜
    for d in reversed([x for x in avail if x < date]):
        v = closes.get((d, ticker))
        if v is not None and v > 0:
            return v, d
    return None, None


def _trading_day_before(d: str) -> Optional[str]:
    """`d` 之前最近的一个交易日。判不了返回 None。"""
    try:
        import datetime as _dt
        from is_trading_day import is_trading_day
        x = _dt.date.fromisoformat(d) - _dt.timedelta(days=1)
        for _ in range(10):
            if is_trading_day(x)[0]:
                return x.isoformat()
            x -= _dt.timedelta(days=1)
    except Exception:  # noqa: BLE001
        pass
    return None


def _session_of_close(cdn_ts_utc: str) -> Optional[str]:
    """CBOE payload 的 `close` 属于哪个交易日 —— 由 CDN 生成时刻决定。

    规则（2026-08-27 实拉四只标的验证，见 tests）：
    **`close` = 文件生成时刻「最近一个已经收盘的交易日」的官方收盘价。**

        NVDA 文件 08-27 08:31 ET（盘前） → close=209.66 = **8/26** 收盘
        TMO  文件 08-26 21:18 ET（盘后） → close=633.71 = **8/26** 收盘

    刻意**不用** `last_trade_time` 定这个日期：盘前它仍停在上一场的最后成交，
    无法区分「8/27 盘前的新文件」与「8/26 的旧文件」——初版就栽在这。
    也刻意**不用** `prev_day_close`：它是再往前一天，归属同样要靠本函数推，
    多绕一层没有收益（v0.45.47 两次修错都源于此）。
    """
    try:
        import datetime as _dt
        from is_trading_day import is_trading_day
        ts = _dt.datetime.strptime(cdn_ts_utc, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_dt.timezone.utc)
        et = ts.astimezone(ZoneInfo("America/New_York"))
        d = et.date()
        if is_trading_day(d)[0] and et.time() >= _dt.time(16, 0):
            return d.isoformat()
        return _trading_day_before(d.isoformat())
    except Exception:  # noqa: BLE001 - 推不出就不做交叉印证，不猜
        return None


def cboe_official_closes(tickers: List[str]) -> Dict[str, Tuple[str, float]]:
    """CBOE `close` → `{ticker: (该收盘价所属交易日, close)}`。

    与 yfinance 官方收盘互为独立来源，用于交叉印证。日期由 payload 的 CDN
    生成时刻自述（`_session_of_close`），**不假设它就是「今天的前一交易日」**：
    CDN 对不同符号刷新进度不同（2026-08-26 实测 TMO 落后一整个 session）。

    走 `_fetch_cboe_payload`，因此继承 v0.45.39 的 vintage 校验。
    """
    out: Dict[str, Tuple[str, float]] = {}
    try:
        import cboe_options as co
        import urllib.request as _u
        import json as _j
    except ImportError:
        return out
    for t in sorted(set(tickers)):
        # 只发一次请求：顶层 `timestamp` 定 session，`data` 过 v0.45.39 的
        # vintage 闸。走 co._fetch_cboe_payload 会丢掉顶层字段且要再发一次。
        try:
            raw = _u.urlopen(_u.Request(
                f"https://cdn.cboe.com/api/global/delayed_quotes/options/"
                f"{co._cboe_symbol(t.upper())}.json",  # noqa: SLF001
                headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read()
            j = _j.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        data = j.get("data") or {}
        try:
            if co._payload_is_stale(t, data):  # noqa: SLF001 - 复用既有闸，不自写
                continue
        except Exception:  # noqa: BLE001
            pass
        px = data.get("close")
        sess = _session_of_close(j.get("timestamp") or "")
        if px and sess:
            out[t] = (sess, float(px))
    return out


def correct(conn: sqlite3.Connection, *, since: Optional[str] = None,
            apply: bool = False, use_cboe: bool = True) -> dict:
    ensure_columns(conn)
    rows = load_rows(conn, since)
    if not rows:
        return {"rows": 0}

    tickers = sorted({r["ticker"] for r in rows})
    lo, hi = min(r["date"] for r in rows), max(r["date"] for r in rows)
    closes = official_closes(tickers, lo, hi)
    # yfinance 批量下载会**部分失败**（实测同一天两次运行：一次全covered，
    # 一次 102 条无来源）。覆盖率必须报出来 —— 否则「无来源 N 条」看起来
    # 像数据本身的性质，而不是这一次下载没拿全。
    _got = {t for _, t in closes}
    stats_cov = (len(_got), len(tickers))
    if _got and len(_got) < len(tickers):
        _log.warning("⚠️ 官方收盘仅覆盖 %d/%d 只标的（yfinance 批量下载部分失败）——"
                     "本次「无来源」条数受此影响，不代表数据缺失；可稍后重跑",
                     len(_got), len(tickers))
    if not closes:
        _log.error("拿不到任何官方收盘 —— 不做任何改动")
        return {"rows": len(rows), "aborted": "no_official_closes"}

    # CBOE 交叉印证只对「上一个交易日」有效 —— 因此**只抓那天有样本的标的**，
    # 而不是全部 52 只（全抓会串行拉 52 次大 JSON，白等 5 分钟）
    prev_td = _prev_trading_day()
    _pt = sorted({r["ticker"] for r in rows if r["date"] == prev_td}) if prev_td else []
    cboe = cboe_official_closes(_pt) if (use_cboe and _pt) else {}
    if _pt and use_cboe:
        _log.info("CBOE 交叉印证：%s 有 %d 只样本，已取回 %d 只的官方收盘（CBOE close）",
                  prev_td, len(_pt), len(cboe))
    elif _pt:
        # --no-cboe 时别打「已取回 0 只」——那看起来像试过且失败了
        _log.info("CBOE 交叉印证：已按 --no-cboe 跳过（%s 有 %d 只样本可印证）",
                  prev_td, len(_pt))

    avail = sorted({d for d, _ in closes})
    stats = {"rows": len(rows), "corrected": 0, "already_ok": 0,
             "no_source": 0, "disputed": 0, "skipped_done": 0,
             "cross_checked": 0, "prior_close_used": 0, "worst": []}
    cur = conn.cursor()
    import datetime as dt
    now = dt.datetime.now().isoformat(timespec="seconds")

    for r in rows:
        # ⚠️ 「要不要动」看**当前值**，不看 raw。raw 是留痕用的原值，校正后
        # 它必然仍偏离官方收盘 —— 拿它做判据会让重跑永远报「需校正 N 条」，
        # 数据没写错（COALESCE 护住了 raw），但报告在撒谎。
        current = r["price_at_predict"]
        raw = r["price_at_predict_raw"] if r["price_at_predict_raw"] else current
        truth, truth_date = _resolve_close(closes, avail, r["date"], r["ticker"])
        if truth is None:
            stats["no_source"] += 1
            continue
        src = "yfinance_close"
        if truth_date != r["date"]:
            # 非交易日样本：如实记下这个价取自哪一天，别让它看起来像当日收盘
            src = f"yfinance_close@{truth_date}"
            stats["prior_close_used"] += 1
        # ⚠️ 先判「要不要动」，再做交叉印证。反过来的话，一条**本就正确**的行
        # 遇到 CBOE 分歧会被打出「拒绝校正」警告并计入 disputed —— 可它压根
        # 没有待校正的内容，这条警告是假的（构造检验确认）。
        if abs(current / truth - 1) <= CORRECT_TOL:
            if r["close_corrected_at"]:
                stats["skipped_done"] += 1
            else:
                stats["already_ok"] += 1
            continue

        # 交叉印证：只在 CBOE 那个价**确实属于本行日期**时才做（见 cboe_official_closes）
        _c = cboe.get(r["ticker"])
        if _c and _c[0] == r["date"]:
            other = _c[1]
            if abs(other / truth - 1) > DISPUTE_TOL:
                stats["disputed"] += 1
                _log.warning("两源分歧，拒绝校正 %s %s：yfinance=%.4f CBOE=%.4f",
                             r["date"], r["ticker"], truth, other)
                continue
            src = f"{src}+cboe_close"      # 保留 @日期 后缀，别把来源信息盖掉
            stats["cross_checked"] += 1
        dev = raw / truth - 1          # 展示用：原值相对官方收盘偏了多少
        stats["corrected"] += 1
        stats["worst"].append((abs(dev), r["date"], r["ticker"], raw, truth, dev * 100))
        if apply:
            cur.execute(
                "UPDATE predictions SET price_at_predict_raw = COALESCE(price_at_predict_raw, ?), "
                "price_at_predict = ?, close_corrected_at = ?, close_correction_source = ? "
                "WHERE id = ?",
                (current, truth, now, src, r["id"]))
    if apply:
        conn.commit()
    stats["close_coverage"] = stats_cov
    stats["worst"].sort(reverse=True)
    stats["worst"] = stats["worst"][:15]
    return stats


def _prev_trading_day() -> Optional[str]:
    try:
        import datetime as dt
        from is_trading_day import is_trading_day
        d = dt.date.today() - dt.timedelta(days=1)
        for _ in range(10):
            if is_trading_day(d)[0]:
                return d.isoformat()
            d -= dt.timedelta(days=1)
    except Exception:  # noqa: BLE001
        pass
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="用官方收盘校正被盘后价污染的 price_at_predict")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--since", default=None, help="只处理该日期及之后（YYYY-MM-DD）")
    ap.add_argument("--apply", action="store_true", help="真的写入（默认 dry-run）")
    ap.add_argument("--no-cboe", action="store_true", help="跳过 CBOE 交叉印证")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        _log.error("⛔ 样本库不存在：%s", args.db)
        return 3
    conn = sqlite3.connect(args.db)
    try:
        st = correct(conn, since=args.since, apply=args.apply, use_cboe=not args.no_cboe)
    finally:
        conn.close()

    if st.get("aborted"):
        _log.error("⛔ 中止：%s", st["aborted"])
        return 3
    if not st["rows"]:
        _log.info("无样本")
        return 0

    mode = "已写入" if args.apply else "DRY-RUN（未改动，加 --apply 落笔）"
    _log.info("\n📐 收盘价校正 —— %s", mode)
    _log.info("  样本 %d 条 | 需校正 %d | 本就正确 %d | 已校正过 %d | 无来源 %d | 两源分歧拒改 %d",
              st["rows"], st["corrected"], st["already_ok"],
              st["skipped_done"], st["no_source"], st["disputed"])
    _log.info("  其中经 CBOE 交叉印证：%d 条 | 非交易日取前一交易日收盘：%d 条",
              st["cross_checked"], st.get("prior_close_used", 0))
    _cov = st.get("close_coverage")
    if _cov and _cov[0] < _cov[1]:
        _log.info("  ⚠️ 官方收盘覆盖 %d/%d 只标的 —— 「无来源」受此影响，可稍后重跑",
                  _cov[0], _cov[1])
    if st["worst"]:
        _log.info("\n  偏离最大的 %d 条：", len(st["worst"]))
        _log.info(f"  {'日期':11s}{'标的':7s}{'原值':>11s}{'官方收盘':>11s}{'偏离':>9s}")
        for _, d, t, b, tr, pc in st["worst"]:
            _log.info(f"  {d:11s}{t:7s}{b:>11.2f}{tr:>11.2f}{pc:>8.2f}%")
    if st["corrected"] and args.apply:
        _log.info("\n⚠️ 派生列（return_t7 / dir_correct_t7 / net_return_t7）以 price_at_predict "
                  "为起点，现已过时。请接着跑：")
        _log.info("   /usr/local/bin/python3 backfill_dir_accuracy.py --all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
