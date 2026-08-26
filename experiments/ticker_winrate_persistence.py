#!/usr/bin/env python3
"""
标的历史胜率的持续性检验 (v1.0, 2026-08-25) —— P1 决策依据

问题
────
系统里有两处按「标的历史胜率」调节行为的机制：

  A. queen_distiller + config.TICKER_ACCURACY_FEEDBACK
     trailing 胜率 < 50% → reliability = max(0.5, wr/0.5)
     → final_score = 5 + (score-5) × reliability（向中性压缩）
     压缩后可能跌破 paper_portfolio 的 entry_score_bull=6.5 → **直接否决入场**

  B. paper_portfolio.CONFIG["win_rate_multiplier"]
     标的历史胜率 < 45% → 仓位 ×0.5；≥60% 且 n≥10 → ×1.2

两者共用同一个前提：**标的的历史胜率能预测它的前向胜率**。
本脚本检验该前提。

方法
────
走查（walk-forward），杜绝前视偏差：对每条预测，只用**严格早于当日**的
同标的已验证样本计算 trailing 胜率，再看该预测的前向结果。口径与
queen_distiller 对齐（累计全历史、非滚动窗口、纯符号判定）。

样本来自 pheromone.db，已应用 v0.45.9 的 ambiguous 修正
（|return| <= 1% 的噪音样本剔除，否则容差会污染胜率）。

用法
────
    /usr/local/bin/python3 experiments/ticker_winrate_persistence.py
    /usr/local/bin/python3 experiments/ticker_winrate_persistence.py --horizon t1

2026-08-25 结论（T+7，597 方向样本 / 456 条具备 trailing）
────────────────────────────────────────────────────────
  折扣触发（trailing<50%）  n=112  前向胜率 52.7%  均收益 +0.50%
  未触发（trailing>=50%）   n=344  前向胜率 51.5%  均收益 +0.68%

  按 trailing 胜率五分层，前向胜率非单调，且**最差的 Q1 前向表现最好**：
    Q1  0-48%  → 58.2%  +1.30%   ← 折扣正打在这一层
    Q2 48-53%  → 46.2%  -0.69%
    Q3 53-58%  → 47.3%  +0.77%
    Q4 59-67%  → 52.7%  +0.99%
    Q5 67-100% → 54.3%  +0.80%

  另做前后半段分割检验（2026-05-03 为界，10 只样本≥15 的标的）：
    前后半胜率 Spearman = -0.273（负相关）
    AMZN 83.9% → 27.3%；META 38.7% → 58.8%

  判定：**无证据支持标的胜率可外推**；点估计方向与机制假设相反。
  故 A 关闭、B 中性化。数据积累后可重跑本脚本复核。
"""

import os
import sys
import math
import glob
import sqlite3
import argparse
import statistics as st
from collections import defaultdict


def find_db():
    env = os.environ.get("ALPHA_HIVE_PHEROMONE_DB")
    if env and os.path.exists(env):
        return env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(here, "pheromone.db")
    if os.path.exists(local):
        return local
    hits = glob.glob("/sessions/*/mnt/Alpha Hive/pheromone.db")
    return hits[0] if hits else None


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0, 100.0)
    p = k / n
    d = 1 + z * z / n
    ctr = (p + z * z / (2 * n)) / d
    sp = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / d
    return (round(p * 100, 1), round(max(0, ctr - sp) * 100, 1), round(min(1, ctr + sp) * 100, 1))


def load(db, horizon, min_prior, threshold):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT date, ticker, direction, final_score, return_{h}, correct_{h} AS ok "
        "FROM predictions WHERE checked_{h} = 1 "
        "AND COALESCE(ambiguous_{h}, 0) = 0 "
        "AND direction IN ('bullish','bearish') ORDER BY date".format(h=horizon)
    )]
    conn.close()

    hist = defaultdict(list)
    fired, not_fired = [], []
    for r in rows:
        prior = [c for d, c in hist[r["ticker"]] if d < r["date"]]
        if len(prior) >= min_prior:
            wr = sum(prior) / len(prior)
            ret = r["return_%s" % horizon]
            rec = {
                "wr": wr,
                "ok": r["ok"],
                "pnl": ret if r["direction"] == "bullish" else -ret,
                "ticker": r["ticker"],
                "date": r["date"],
            }
            (fired if wr < threshold else not_fired).append(rec)
        hist[r["ticker"]].append((r["date"], r["ok"]))
    return rows, fired, not_fired


def report(label, bucket):
    if not bucket:
        print("  %-26s 无样本" % label)
        return
    k = sum(b["ok"] for b in bucket)
    n = len(bucket)
    m, lo, hi = wilson(k, n)
    print("  %-26s n=%3d  前向胜率 %5.1f%%  CI[%.0f-%.0f]  均收益 %+.2f%%"
          % (label, n, m, lo, hi, st.mean([b["pnl"] for b in bucket])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--horizon", default="t7", choices=["t1", "t7", "t30"])
    ap.add_argument("--min-prior", type=int, default=5,
                    help="对齐 TICKER_ACCURACY_FEEDBACK.min_samples")
    ap.add_argument("--threshold", type=float, default=0.50,
                    help="对齐 TICKER_ACCURACY_FEEDBACK.discount_threshold")
    args = ap.parse_args()

    db = args.db or find_db()
    if not db:
        print("❌ 找不到 pheromone.db")
        return 1

    rows, fired, not_fired = load(db, args.horizon, args.min_prior, args.threshold)
    allb = fired + not_fired

    print("DB: %s" % db)
    print("口径 %s | 方向样本 %d | 具备 trailing 的 %d 条\n"
          % (args.horizon.upper(), len(rows), len(allb)))

    print("===== 走查检验：trailing 胜率能否预测前向表现 =====")
    report("折扣触发（trailing<%.0f%%）" % (args.threshold * 100), fired)
    report("未触发", not_fired)
    report("全体", allb)

    if len(allb) >= 25:
        print("\n  按 trailing 胜率五分层：")
        s = sorted(allb, key=lambda x: x["wr"])
        n = len(s)
        for i in range(5):
            b = s[i * n // 5:(i + 1) * n // 5]
            if not b:
                continue
            k = sum(x["ok"] for x in b)
            print("    Q%d trailing %4.0f-%4.0f%%  n=%3d  前向胜率 %5.1f%%  均收益 %+.2f%%"
                  % (i + 1, b[0]["wr"] * 100, b[-1]["wr"] * 100, len(b),
                     k / len(b) * 100, st.mean([x["pnl"] for x in b])))

    # 前后半段分割检验
    print("\n===== 前后半段分割检验（标的强弱是否持续）=====")
    rs = sorted(rows, key=lambda r: r["date"])
    if rs:
        mid = rs[len(rs) // 2]["date"]
        h1, h2 = defaultdict(list), defaultdict(list)
        for r in rs:
            (h1 if r["date"] < mid else h2)[r["ticker"]].append(r["ok"])
        pairs = [(t, sum(h1[t]) / len(h1[t]), sum(h2[t]) / len(h2[t]))
                 for t in h1 if len(h1[t]) >= 15 and len(h2.get(t, [])) >= 15]
        print("  分界 %s，合格标的 %d 只" % (mid, len(pairs)))
        for t, a, b in sorted(pairs, key=lambda x: -x[1]):
            print("    %-6s 前半 %5.1f%%  后半 %5.1f%%  Δ%+5.1fpp" % (t, a * 100, b * 100, (b - a) * 100))
        if len(pairs) >= 4:
            xs = [p[1] for p in pairs]
            ys = [p[2] for p in pairs]

            def rk(v):
                order = sorted(range(len(v)), key=lambda i: v[i])
                r = [0] * len(v)
                for pos, i in enumerate(order):
                    r[i] = pos
                return r
            rx, ry = rk(xs), rk(ys)
            mx, my = st.mean(rx), st.mean(ry)
            num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
            den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
            rho = num / den if den else 0
            print("  前后半 Spearman = %+.3f  （>0 才说明标的强弱可外推）" % rho)

    print("\n判据：折扣触发组前向表现若未显著劣于未触发组，则该机制无依据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
