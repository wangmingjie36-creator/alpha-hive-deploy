#!/usr/bin/env python3
"""v0.45.387 阶段 2 的留一验证：用 Twelve Data 补回「被删掉的那一根」，补得准不准？

要回答的问题
------------
`bars_integrity.fill_gaps_from_reference` 声称：用 Twelve Data（未复权）的收盘，乘上邻居 bar 的
（yfinance 复权价 / Twelve Data 收盘）因子，就能把 yfinance 缺的那一根补回**复权口径**，且遇到除息日会拒绝。
这个声称不能靠推理，要靠真数据：

    对 WATCHLIST 每只标的，取 yfinance 1y 复权日线（截至 `LAST`），
    对**最后 21 根**逐根：删掉这一根 → 让填充函数用 Twelve Data 补回 →
        · 补出的收盘 vs 被删掉的真值（相对误差）；
        · 用补出的序列重算生产口径的 (rank, percentile) vs 完整序列的（差）；
        · 补不出的，记原因。

`FILL_RATIO_TOL`（两侧因子允许的相对差 = 可接受的补数误差上界）就是按这里的实测噪声定的，不是拍的。

用法
----
    /usr/local/bin/python3 experiments/hv_gap_second_source_loo.py --out experiments/hv_gap_second_source_loo_20260930.json

⚠️ 联网（yfinance + Twelve Data，后者 7 次/分钟 ⇒ 30 只约 5 分钟）。结果**落盘冻结**：yfinance 1y 窗口以
「运行当下」为终点，隔久重跑会漂移；判定依据看冻结文件，不要现算。
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LAST = "2026-09-29"          # 验证的最后一个日期（之后的当日 bar 可能未完成，不纳入）
LAST_N = 21                  # 与 bars_integrity.DEFAULT_CRITICAL_BARS 同一口径


def _load_audit():
    spec = importlib.util.spec_from_file_location("hv_gap_equivalence_audit",
                                                  Path(__file__).resolve().parent / "hv_gap_equivalence_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(tickers, last: str = LAST) -> dict:
    import yfinance as yf
    import twelve_data as td
    from bars_integrity import fill_gaps_from_reference
    aud = _load_audit()
    out = {"per": [], "noise": {}}
    for t in tickers:
        h = yf.Ticker(t).history(period="1y")
        keep = [i for i, d in enumerate(h.index) if d.strftime("%Y-%m-%d") <= last]
        dates = [h.index[i].strftime("%Y-%m-%d") for i in keep]
        closes = [float(h["Close"].iloc[i]) for i in keep]
        rows = None
        for _ in range(3):                                # Twelve Data 偶发瞬时网络错误（SSL EOF），重试
            rows = td.fetch_bars(t, 120)
            if rows:
                break
            time.sleep(8)
            td.clear_bars_cache()
        if not rows:
            out["per"].append({"t": t, "err": "td_unavailable"})
            print(t, "Twelve Data 不可用", flush=True)
            continue
        rows = [r for r in rows if r["date"] <= last]
        ref = {r["date"]: r["close"] for r in rows}
        have = dict(zip(dates, closes))
        common = [d for d in dates if d in ref][-120:]
        ratios = [have[d] / ref[d] for d in common]
        steps = [abs(ratios[i + 1] / ratios[i] - 1) for i in range(len(ratios) - 1)]
        out["noise"][t] = {"n": len(steps), "max": max(steps), "top3": sorted(steps)[-3:],
                           "median": sorted(steps)[len(steps) // 2]}
        full = aud.rank_pct(closes)
        n = len(closes)
        for k in range(n - LAST_N, n):
            d = dates[k]
            if d not in ref:
                out["per"].append({"t": t, "d": d, "status": "ref_missing"})
                continue
            r = fill_gaps_from_reference(dates[:k] + dates[k + 1:], closes[:k] + closes[k + 1:], rows, [d])
            if d in r["filled"]:
                got = r["closes"][r["dates"].index(d)]
                fp = aud.rank_pct(r["closes"])
                out["per"].append({
                    "t": t, "d": d, "status": "filled", "tail": k == n - 1, "rel_err": got / closes[k] - 1,
                    "d_rank": None if (fp is None or full is None) else fp[0] - full[0],
                    "d_pct": None if (fp is None or full is None) else fp[1] - full[1]})
            else:
                out["per"].append({"t": t, "d": d, "status": "unfilled", "why": r["unfilled"].get(d)})
        print(t, "ok", flush=True)
    return out


def summarize(out: dict) -> dict:
    per = [p for p in out["per"] if not p.get("err")]
    filled = [p for p in per if p["status"] == "filled"]
    q = lambda xs, a: sorted(xs)[max(int(len(xs) * a) - 1, 0)]  # noqa: E731
    re_ = [abs(p["rel_err"]) for p in filled]
    dr = [abs(p["d_rank"]) for p in filled if p["d_rank"] is not None]
    dp = [abs(p["d_pct"]) for p in filled if p["d_pct"] is not None]
    reasons: dict = {}
    for p in per:
        if p["status"] != "filled":
            k = f'{p["status"]}:{p.get("why")}'
            reasons[k] = reasons.get(k, 0) + 1
    return {
        "n_tickers": len({p["t"] for p in out["per"]}), "n_trials": len(per), "n_filled": len(filled),
        "not_filled": reasons, "n_tail_filled": sum(1 for p in filled if p["tail"]),
        "td_unavailable": [p["t"] for p in out["per"] if p.get("err")],
        "rel_err": {"median": statistics.median(re_), "p95": q(re_, .95), "max": max(re_)} if re_ else None,
        "abs_d_rank": {"median": statistics.median(dr), "p95": q(dr, .95), "max": max(dr)} if dr else None,
        "abs_d_pct": {"median": statistics.median(dp), "p95": q(dp, .95), "max": max(dp)} if dp else None,
        "factor_step_noise": {"median_of_medians": statistics.median(v["median"] for v in out["noise"].values()),
                              "max_of_medians": max(v["median"] for v in out["noise"].values())},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import config
    out = run(config.WATCHLIST)
    doc = {"version": "v0.45.387", "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "generator": "experiments/hv_gap_second_source_loo.py", "last_date": LAST,
           "summary": summarize(out), **out}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print(json.dumps(doc["summary"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
