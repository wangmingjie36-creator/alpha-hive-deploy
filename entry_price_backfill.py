#!/usr/bin/env python3
"""
🐝 Alpha Hive — 入场价为 0 的账本行回填 (v0.45.360)
====================================================
`predictions.price_at_predict` 为 0 / NULL 的行用**该场官方收盘**补上，
来源必须**两个独立家族互相印证**才写。默认 dry-run。

为什么需要单独一个工具
----------------------
0 是本仓「没有这个价」的标记：全仓统计一律 `price_at_predict > 0`，
所以这些行对全部收益 / IC 计算**不可见**。而现成的 `close_correction.py`
是修「有价但被盘后价污染」的，`load_rows` 只选 `> 0` —— 恰好把这一类整个排除。
2026-09-24 / 09-25 各 30/30 行即如此（CBOE 整源停更 + yfinance 限流；本机 DNS 失败），
另有 08-12 / 08-14 各 1 行 BRK-B（ticker 正则事故遗留，v0.45.50 记过）。

为什么入场价 = 该场官方收盘
--------------------------
扫描在收盘后跑（14:0x PDT = 17:0x ET），v0.45.46 起入场价口径就是该场官方收盘；
`close_correction` 也按这个口径校正。补回来的值与「当天取价成功」时应得的值同口径。

来源与归属自证（三个家族，互相独立）
------------------------------------
  yf    yfinance 日线 `Close`，**交易日必须命中当天、不许回退**（复用
        `close_correction.official_closes` / `_resolve_close`，yfinance 缺覆盖时它
        自己会走 Twelve Data 补——那种情况下该值记为 td 家族，不重复计票）
  cboe  云端快照（`origin/cloud-snapshots`）：
          · 当日快照 `close_verdict == official` 且场次 == 该日 → `price_at_fetch`
          · 其后第一份快照的 `prev_day_close`，`prev_close_session` 自述归属 == 该日
        两者都有时**彼此也必须一致**，否则整个 cboe 家族判分歧、不计票。
        归属全部由 payload 的 `last_trade_time` 自述（与数据管道同一份判据），
        不按目录名、不按抓取时刻推 —— 见 auto-memory cboe-stale-intraday。
        ⚠️ 两个子来源分别取、不经 `cloud_snapshot_loader.load_official_close`：
        后者只返回**一个**（且取价顺序另有版本在调），这里要的是两个都拿来互验。
  td    Twelve Data 日线（独立配额）。只对缺 cboe 的行调，逐只 ~8.6 秒。

判决（每行一个）
----------------
  backfill        ≥2 个家族有值、且两两偏差 ≤ DISPUTE_TOL → 写入（取 yf，缺则 cboe）
  disputed        有家族之间偏差 > DISPUTE_TOL → 不写，列出来给人看
  single_source   只有一个家族 → 不写（`--allow-single-source` 才写，标签照实带上）
  no_source       一个都没有 → 不写

写入什么
--------
  price_at_predict        ← 官方收盘
  price_at_predict_raw    ← COALESCE(原 raw, 原值, 0.0)  —— 0.0 就是「原来没有价」的留痕
  close_corrected_at      ← 此刻
  close_correction_source ← "entry_backfill:<参与印证的家族>"
**不动派生列**：这些行 `checked_t1/t7/t30` 仍为 0，下一次 `run_backtest` 会按正常流程回测它们。
UPDATE 带 `AND (price_at_predict IS NULL OR price_at_predict <= 0)` —— 只补仍为 0 的行，
重复运行幂等，也不会覆盖此间被别处写好的价。`--apply` 前先用 sqlite `backup()` 存一份。

⚠️ 补回来的行会**进入统计**。补之前先问：那天的**分数**本身能不能用？
（09-24/25 的字段覆盖率 0/30、全部数据源 FALLBACK —— 入场价为 0 反而把
这些降级输入的分数挡在了统计外。）所以用 `--date` 逐日选，别默认全补。

用法
----
    /usr/local/bin/python3 entry_price_backfill.py                       # 全部不可用行，dry-run
    /usr/local/bin/python3 entry_price_backfill.py --date 2026-08-12 --date 2026-08-14
    /usr/local/bin/python3 entry_price_backfill.py --db /path/snap.db --out diff.json
    /usr/local/bin/python3 entry_price_backfill.py --date 2026-09-24 --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import sqlite3
import sys
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_log = logging.getLogger("alpha_hive.entry_price_backfill")

# 与 close_correction 同一个分歧容差：两源超过它就拒写
DISPUTE_TOL = 0.002

FAM_YF, FAM_CBOE, FAM_TD = "yf", "cboe", "td"

V_BACKFILL = "backfill"
V_DISPUTED = "disputed"
V_SINGLE = "single_source"
V_NONE = "no_source"

# 覆盖钩子（同 close_correction.DB_PATH）：None ⇒ 调用时解析 PATHS.db
DB_PATH = None


def _db_path() -> str:
    if DB_PATH is not None:
        return os.fspath(DB_PATH)
    from hive_logger import PATHS
    return str(PATHS.db)


def _ok(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0)


def load_unusable(conn: sqlite3.Connection, dates: Optional[List[str]] = None,
                  since: Optional[str] = None) -> List[dict]:
    """入场价不可用的行（NULL / ≤0 / 非有限）。与 scan_coverage_gate._usable_price 同一口径。"""
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, date, ticker, price_at_predict, price_at_predict_raw "
           "FROM predictions WHERE 1=1")
    args: list = []
    if dates:
        sql += f" AND date IN ({','.join('?' * len(dates))})"
        args += list(dates)
    if since:
        sql += " AND date >= ?"
        args.append(since)
    sql += " ORDER BY date, ticker"
    return [dict(r) for r in conn.execute(sql, args) if not _ok(r["price_at_predict"])]


# ── 来源 ────────────────────────────────────────────────────────────

def cboe_snapshot_candidates(date: str, ticker: str, *, ref: Optional[str] = None,
                             repo: Optional[str] = None,
                             later_dates: Optional[List[str]] = None) -> Dict[str, float]:
    """云端快照里**归属自证为 date** 的官方收盘 → {"snap_close"?: 价, "next_prev_close"?: 价}。

    两个子来源分别判，都判不出就返回空 dict。判据全在 cboe_options（与数据管道同一份）。
    """
    import cboe_options as co
    import cloud_snapshot_loader as csl
    out: Dict[str, float] = {}
    snap = csl.load_ticker(date, ticker, ref=ref, repo=repo)
    if snap:
        verdict, session = co.close_verdict(
            {"last_trade_time": snap.get("last_trade_time_et")}, csl._fetched_at(snap))  # noqa: SLF001
        px = snap.get("price_at_fetch")
        if (verdict == co.CLOSE_OFFICIAL and session is not None
                and session.isoformat() == date and _ok(px)):
            out["snap_close"] = float(px)
    if later_dates is None:
        later_dates = csl.available_dates(ref, repo)
    later = [d for d in later_dates if d > date]
    if later:
        nxt = csl.load_ticker(later[0], ticker, ref=ref, repo=repo)
        if nxt:
            pc = co.prev_close_session({"last_trade_time": nxt.get("last_trade_time_et"),
                                        "prev_day_close": nxt.get("prev_day_close")})
            if pc and pc[0].isoformat() == date and _ok(pc[1]):
                out["next_prev_close"] = float(pc[1])
    return out


def _rel(a: float, b: float) -> float:
    return abs(a / b - 1)


def decide(cands: Dict[str, Optional[float]], cboe_parts: Optional[Dict[str, float]] = None,
           allow_single: bool = False) -> dict:
    """一行的判决。纯函数（测试直接喂）。

    `cands`：{家族: 价 | None}；`cboe_parts`：cboe 家族的子来源（两者都有时须自洽）。
    """
    fams = {k: float(v) for k, v in cands.items() if _ok(v)}
    note = ""
    parts = cboe_parts or {}
    if len(parts) >= 2:
        vs = list(parts.values())
        if max(_rel(a, b) for a in vs for b in vs) > DISPUTE_TOL:
            fams.pop(FAM_CBOE, None)
            note = "cboe 两个子来源彼此不一致，cboe 不计票：" + json.dumps(parts)
    if not fams:
        return {"verdict": V_NONE, "value": None, "families": [], "note": note}
    keys = sorted(fams)
    worst = max((_rel(fams[a], fams[b]) for a in keys for b in keys), default=0.0)
    if len(fams) >= 2 and worst > DISPUTE_TOL:
        return {"verdict": V_DISPUTED, "value": None, "families": keys,
                "max_dev_pct": round(worst * 100, 4), "note": note}
    value = fams.get(FAM_YF) or fams.get(FAM_CBOE) or fams.get(FAM_TD)
    if len(fams) == 1 and not allow_single:
        return {"verdict": V_SINGLE, "value": None, "candidate": value, "families": keys,
                "note": note}
    return {"verdict": V_BACKFILL, "value": value, "families": keys,
            "max_dev_pct": round(worst * 100, 4), "note": note}


def plan(rows: List[dict], *,
         yf_closes: Callable[[List[str], str, str], Dict] = None,
         cboe: Callable[[str, str], Dict[str, float]] = None,
         td_closes: Callable[[List[str], str, str], Dict] = None,
         allow_single: bool = False) -> List[dict]:
    """逐行给判决。三个来源都可注入（测试不出网）；缺省走真实实现。"""
    if not rows:
        return []
    import close_correction as cc
    if yf_closes is None:
        yf_closes = _yf_only_closes
    if td_closes is None:
        td_closes = cc._twelve_data_closes  # noqa: SLF001
    if cboe is None:
        import cloud_snapshot_loader as csl
        _later = csl.available_dates()
        cboe = lambda d, t: cboe_snapshot_candidates(d, t, later_dates=_later)  # noqa: E731

    tickers = sorted({r["ticker"] for r in rows})
    lo, hi = min(r["date"] for r in rows), max(r["date"] for r in rows)
    yf = yf_closes(tickers, lo, hi) or {}
    avail = sorted({d for d, _ in yf})

    cb: Dict[tuple, Dict[str, float]] = {}
    for r in rows:
        cb[(r["date"], r["ticker"])] = cboe(r["date"], r["ticker"]) or {}

    # Twelve Data 只补「没有 cboe」的行——它的用处是给这些行凑第二个独立家族
    need_td = sorted({r["ticker"] for r in rows if not cb[(r["date"], r["ticker"])]})
    if need_td:
        # 下面 Twelve Data 自己的日志会说「yfinance 缺覆盖」——在这里不是：是给无快照的行凑第二来源
        _log.info("无 cboe 快照的 %d 只改问 Twelve Data 作第二独立来源：%s",
                  len(need_td), ", ".join(need_td))
    td = td_closes(need_td, lo, hi) if need_td else {}
    td_avail = sorted({d for d, _ in td})

    out = []
    for r in rows:
        d, t = r["date"], r["ticker"]
        y, y_day = cc._resolve_close(yf, avail, d, t)          # noqa: SLF001 交易日必须命中当天
        tv, td_day = cc._resolve_close(td, td_avail, d, t)     # noqa: SLF001
        parts = cb[(d, t)]
        cboe_v = parts.get("snap_close") or parts.get("next_prev_close")
        dec = decide({FAM_YF: y, FAM_CBOE: cboe_v, FAM_TD: tv}, parts, allow_single)
        if y is not None and y_day != d:
            dec["note"] = (dec.get("note") or "") + f" yf 取自 {y_day}（非交易日样本）"
        out.append({"id": r["id"], "date": d, "ticker": t,
                    "current": r["price_at_predict"], "yf": y, "cboe": parts or None,
                    "td": tv, **dec})
    return out


def _yf_only_closes(tickers: List[str], lo: str, hi: str) -> Dict:
    """yfinance 日线**本身**（不带 close_correction 的 Twelve Data 兜底）。

    official_closes 在 yfinance 缺覆盖时会悄悄换成 Twelve Data 的值——那样同一个
    Twelve Data 价会以 yf 与 td 两个家族各计一票，「两源印证」就成了一源自证。
    """
    import close_correction as cc
    orig = cc._twelve_data_closes  # noqa: SLF001
    cc._twelve_data_closes = lambda *a, **k: {}  # noqa: SLF001
    try:
        return cc.official_closes(tickers, lo, hi)
    finally:
        cc._twelve_data_closes = orig  # noqa: SLF001


# ── 落笔 ────────────────────────────────────────────────────────────

def apply_plan(conn: sqlite3.Connection, decisions: List[dict], now: Optional[str] = None) -> int:
    """只写 verdict == backfill 的行；只写**仍然不可用**的行。返回实际改动行数。"""
    now = now or dt.datetime.now().isoformat(timespec="seconds")
    n = 0
    for x in decisions:
        if x["verdict"] != V_BACKFILL or not _ok(x.get("value")):
            continue
        cur = conn.execute(
            "UPDATE predictions SET "
            "price_at_predict_raw = COALESCE(price_at_predict_raw, price_at_predict, 0.0), "
            "price_at_predict = ?, close_corrected_at = ?, close_correction_source = ? "
            "WHERE id = ? AND (price_at_predict IS NULL OR price_at_predict <= 0)",
            (x["value"], now, "entry_backfill:" + "+".join(x["families"]), x["id"]))
        n += cur.rowcount
    conn.commit()
    return n


def _render(decisions: List[dict]) -> str:
    out = [f"{'date':10} {'ticker':6} {'verdict':14} {'→ value':>10} {'yf':>10} "
           f"{'cboe(snap/next)':>21} {'td':>10}  dev%"]
    for x in decisions:
        cb = x.get("cboe") or {}
        cbs = f"{cb.get('snap_close', '—')}/{cb.get('next_prev_close', '—')}"
        f = lambda v: f"{v:.4f}" if isinstance(v, (int, float)) else "—"  # noqa: E731
        out.append(f"{x['date']:10} {x['ticker']:6} {x['verdict']:14} {f(x.get('value')):>10} "
                   f"{f(x.get('yf')):>10} {cbs:>21} {f(x.get('td')):>10}  "
                   f"{x.get('max_dev_pct', '')}{('  ' + x['note']) if x.get('note') else ''}")
    tally: Dict[str, int] = {}
    for x in decisions:
        tally[x["verdict"]] = tally.get(x["verdict"], 0) + 1
    out.append("合计：" + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return "\n".join(out)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="入场价为 0 的 predictions 行，用两源印证的官方收盘回填")
    ap.add_argument("--db", default=None, help="库路径（默认 PATHS.db）")
    ap.add_argument("--date", action="append", default=None, help="只处理这些日期（可重复）")
    ap.add_argument("--since", default=None)
    ap.add_argument("--allow-single-source", action="store_true",
                    help="只有一个来源也写（默认不写）")
    ap.add_argument("--out", default=None, help="把逐行判决写成 JSON")
    ap.add_argument("--apply", action="store_true", help="落笔（默认 dry-run）")
    args = ap.parse_args()

    db = args.db or _db_path()
    if not os.path.exists(db):                 # sqlite 打开不存在的路径会建空库
        _log.error("库不存在：%s", db)
        return 3
    uri = f"file:{db}" + ("" if args.apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = load_unusable(conn, args.date, args.since)
        if not rows:
            print("没有入场价不可用的行")
            return 0
        decisions = plan(rows, allow_single=args.allow_single_source)
        print(_render(decisions))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(decisions, fh, ensure_ascii=False, indent=2)
        if not args.apply:
            print("\n（dry-run：未改动。落笔加 --apply；之后下一次 run_backtest 会自然回测这些行）")
            return 0
        bak = f"{db}.bak-entry-backfill-{dt.datetime.now():%Y%m%d-%H%M%S}"
        dst = sqlite3.connect(bak)
        conn.backup(dst)
        dst.close()
        n = apply_plan(conn, decisions)
        print(f"\n已写入 {n} 行（备份：{bak}）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
