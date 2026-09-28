#!/usr/bin/env python3
"""盘中陈旧判据对照：main 的 60s（v0.45.234）vs 未并入分支的 120 分钟（v0.45.88）。

v0.45.354 用它结案 `origin/fix/cboe-*` 两条分支。只读：经 `git show` 读
`origin/cloud-snapshots` 上的全部快照，不写任何文件、不联网。

真值 = **其后第一份快照**的 `prev_day_close`，归属由 `cboe_options.prev_close_session`
自证（隔了交易日就对不上、该行无真值），与 `cloud_snapshot_loader.load_official_close`
第 2 步同一口径——云端 290 份实测 289 份 ≤0.01%，不随当日文件陈旧而错。

用法（仓库根或任意 cwd 均可）：
    /usr/local/bin/python3 experiments/cboe_freeze_criterion_corpus.py
    /usr/local/bin/python3 experiments/cboe_freeze_criterion_corpus.py --err-pct 0.5 --list
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent      # 代码位置 ⇒ __file__ 锚点是对的
sys.path.insert(0, str(_ROOT))

_ET = ZoneInfo("America/New_York")
_SUBDIR = "cloud_snapshots"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(_ROOT), *args], capture_output=True, text=True)


def _show(ref: str, path: str):
    r = _git("show", f"{ref}:{path}")
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None


def load_corpus(ref: str):
    """→ (dates, {(date, ticker): snapshot})。只收 manifest.ok 里的标的。"""
    r = _git("ls-tree", "--name-only", f"{ref}:{_SUBDIR}")
    if r.returncode != 0:
        raise SystemExit(f"读不到 {ref}:{_SUBDIR}（{r.stderr.strip()}）——先 git fetch origin cloud-snapshots")
    dates = sorted(r.stdout.split())
    snaps = {}
    for d in dates:
        man = _show(ref, f"{_SUBDIR}/{d}/manifest.json") or {}
        for t in man.get("ok") or []:
            s = _show(ref, f"{_SUBDIR}/{d}/{t}.json")
            if s:
                snaps[(d, t)] = s
    return dates, snaps


def classify(dates, snaps, branch_min: float):
    import cboe_options as co
    from is_trading_day import session_close_et

    rows = []
    for (d, t), s in sorted(snaps.items()):
        lt_raw = s.get("last_trade_time_et")
        if not lt_raw:
            continue
        lt = datetime.fromisoformat(lt_raw)
        if lt.tzinfo is not None:
            lt = lt.astimezone(_ET).replace(tzinfo=None)
        fetched = s.get("fetched_at_utc")
        fetched = datetime.fromisoformat(fetched) if fetched else None
        lag_s = (datetime.combine(lt.date(), session_close_et(lt.date())) - lt).total_seconds()
        verdict, _ = co.close_verdict({"last_trade_time": lt_raw}, fetched)

        truth = None
        later = [x for x in dates if x > d]
        nxt = snaps.get((later[0], t)) if later else None
        if nxt:
            pc = co.prev_close_session({"last_trade_time": nxt.get("last_trade_time_et"),
                                        "prev_day_close": nxt.get("prev_day_close")})
            if pc and pc[0].isoformat() == d:
                truth = pc[1]
        px = s.get("price_at_fetch")
        err = abs(px / truth - 1) * 100 if (truth and px) else None
        rows.append({
            "date": d, "ticker": t, "last_trade": lt_raw[11:19], "lag_s": lag_s,
            "main_flags": verdict == co.CLOSE_STALE_INTRADAY,
            # 分支口径（6d8f3579 `_freeze_lag_min` / 83bcab5c）：收盘后 last_trade 早于收盘超过 N 分钟
            "branch_flags": lag_s > branch_min * 60,
            "price": px, "truth": truth, "err_pct": err,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="origin/cloud-snapshots")
    ap.add_argument("--branch-min", type=float, default=120.0, help="分支的冻结阈值（分钟）")
    ap.add_argument("--err-pct", type=float, default=0.05, help="与官方收盘偏离多少算「价错」")
    ap.add_argument("--list", action="store_true", help="列出分支漏判的价错行")
    args = ap.parse_args()

    dates, snaps = load_corpus(args.ref)
    rows = classify(dates, snaps, args.branch_min)
    judged = [r for r in rows if r["err_pct"] is not None]
    bad = [r for r in judged if r["err_pct"] > args.err_pct]

    print(f"{args.ref}: {len(dates)} 天，{len(rows)} 份有 last_trade，{len(judged)} 份有次日真值")
    print(f"价错（偏离官方收盘 >{args.err_pct}%）：{len(bad)}")
    print(f"  main 60s 判陈旧：{sum(r['main_flags'] for r in rows)}，其中抓到价错 {sum(r['main_flags'] for r in bad)}")
    print(f"  分支 >{args.branch_min:g}min：{sum(r['branch_flags'] for r in rows)}，"
          f"其中抓到价错 {sum(r['branch_flags'] for r in bad)}")
    print(f"  分支判出的 ⊆ main 判出的：{all(r['main_flags'] for r in rows if r['branch_flags'])}")
    print(f"  main 漏判的价错：{[(r['date'], r['ticker'], r['last_trade'], round(r['err_pct'], 3)) for r in bad if not r['main_flags']]}")
    if args.list:
        for r in bad:
            if not r["branch_flags"]:
                print(f"  分支漏判 {r['date']} {r['ticker']:5s} last_trade={r['last_trade']} "
                      f"lag={r['lag_s'] / 60:.1f}min 价={r['price']} 真值={r['truth']} 偏离={r['err_pct']:.3f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
