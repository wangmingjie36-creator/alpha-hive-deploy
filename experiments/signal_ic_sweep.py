#!/usr/bin/env python3
"""
🐝 Alpha Hive — 全信号 IC 普查（干净口径） (v0.45.19)
====================================================
回答一个具体问题：**`signal_archive` 里 49 个信号，哪些真的能预测前瞻收益？**

为什么要重扫一遍
----------------
`ic_diagnostics.py` 只覆盖 5 个 `dimension_scores` 维度，且在 v0.45.17 之前
所有口径都建立在**被截断的收益**上（`return_t7` 是路径依赖离场收益；
`price_t7` 自 2026-05 起等于 `exit_price`，同样截断）。真正未截断的
T+7 收盘价是 `close_t7`（v0.45.17 新增）。

换成干净口径后，5 个维度里谁是主角**直接反转**了：
    污染口径：risk_adj 四口径全过、sentiment 无口径通过
    干净口径：sentiment 三口径过 (Bonf p=0.008)、risk_adj 掉到 p=0.063 且 jackknife 失效
所以有必要把全部信号在干净口径下重扫，而不是沿用任何旧排名。

方法（每一条都是为了不自欺）
----------------------------
1. **目标**：`(close_t7 − price_at_predict) / price_at_predict`，无截断。
2. **日度横截面 → 每周取一天**（与 `ic_diagnostics.subsample_non_overlapping`
   同法，勿自创）。先按**单个交易日**算 Spearman rank-IC——只有同一天出发的
   预测才共享同一个收益区间，跨日混进一个横截面排序是错的（我第一版就这么写，
   把 `dim.sentiment` 从 t=+3.16 压成查不出来）。再每 ISO 周取第一个可用交易日，
   近似消除 T+7 前瞻收益的重叠。
3. **Bonferroni**：一次扫 N 个信号，α=0.05 下期望有 N×0.05 个假阳性。
   不校正就是在挑噪音。校正后 p 值直接打印，**不要只看未校正的**。
4. **稳健性**：剔除 IC 绝对值最大的 3 周后重算 t —— 检验是否被少数几周驱动。
5. **regime 一致性**：上涨期 vs 下跌震荡期符号是否一致。符号翻转的信号
   （如 catalyst）全样本 IC≈0 往往是两个相反效应相消的假象。

⚠️ 已知局限
   - N_eff ≈ 20 余周，样本方差自身相对标准误 ≈ 32%，IC 值应读作量级。
   - IC 只衡量**排序能力**，不等于扣除成本后可盈利。
   - 本工具**不做**权重建议。改权重前须走 `weekly_optimizer` 的既有闸
     （v0.44.0 起为只读诊断，见 [[alpha-hive-weight-learning-loop]]）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev

DB = Path(__file__).resolve().parent.parent / "pheromone.db"
MIN_WIDTH = 5        # 单周横截面最少标的数
MIN_WEEKS = 8        # 少于此周数不下结论
DROP_EXTREME = 3     # 稳健性检验剔除的极端周数
UP_MONTHS = {"2026-04", "2026-05", "2026-06"}
DOWN_MONTHS = {"2026-02", "2026-03", "2026-07"}


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None

    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else None


def load(db_path: Path):
    """返回 {signal: {week: [(value, ret), ...]}} 与月份映射。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        preds = con.execute("""
            SELECT date, ticker, price_at_predict, close_t7, final_score, dimension_scores
            FROM predictions
            WHERE checked_t7 = 1 AND close_t7 IS NOT NULL AND price_at_predict > 0
        """).fetchall()
        sigs = con.execute("SELECT date, ticker, signal, value FROM signal_archive").fetchall()
    finally:
        con.close()

    ret_map = {}
    for r in preds:
        ret_map[(r["date"], r["ticker"])] = (r["close_t7"] / r["price_at_predict"] - 1) * 100

    data: dict = defaultdict(lambda: defaultdict(list))
    weeks_month: dict = {}

    def add(date, ticker, name, val):
        key = (date, ticker)
        if key not in ret_map or val is None:
            return
        try:
            v = float(val)
        except (TypeError, ValueError):
            return
        weeks_month.setdefault(date, date[:7])
        data[name][date].append((v, ret_map[key]))   # 按**交易日**归集，不是按周

    for r in sigs:
        add(r["date"], r["ticker"], r["signal"], r["value"])
    # 顺带把 final_score 与 5 个维度也纳入同一套统计
    for r in preds:
        add(r["date"], r["ticker"], "predictions.final_score", r["final_score"])
        try:
            ds = json.loads(r["dimension_scores"] or "{}")
        except (json.JSONDecodeError, TypeError):
            ds = {}
        if isinstance(ds, dict):
            for k, v in ds.items():
                add(r["date"], r["ticker"], f"dim.{k}", v)
    return data, weeks_month


def analyse(by_day: dict, day_month: dict):
    # ① 日度横截面 IC
    ic_by_day: dict = {}
    for day in sorted(by_day):
        pairs = by_day[day]
        if len(pairs) < MIN_WIDTH:
            continue
        # 全同值的横截面没有排序信息，计入会把 IC 往 0 拉
        if len({p[0] for p in pairs}) < 2:
            continue
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ic_by_day[day] = ic
    # ② 每 ISO 周取第一个可用交易日（同 ic_diagnostics.subsample_non_overlapping）
    ics, months, seen = [], [], set()
    for day in sorted(ic_by_day):
        key = dt.date.fromisoformat(day).isocalendar()[:2]
        if key in seen:
            continue
        seen.add(key)
        ics.append(ic_by_day[day])
        months.append(day_month.get(day, ""))
    n = len(ics)
    if n < MIN_WEEKS or stdev(ics) == 0:
        return None
    m, sd = mean(ics), stdev(ics)
    t = m / (sd / math.sqrt(n))
    p = math.erfc(abs(t) / math.sqrt(2))
    # 稳健性：剔除 |IC| 最大的若干周
    keep = sorted(range(n), key=lambda i: abs(ics[i]))[:max(3, n - DROP_EXTREME)]
    sub = [ics[i] for i in keep]
    t_rob = (mean(sub) / (stdev(sub) / math.sqrt(len(sub)))
             if len(sub) > 2 and stdev(sub) > 0 else 0.0)
    up = [i for i, mo in zip(ics, months) if mo in UP_MONTHS]
    dn = [i for i, mo in zip(ics, months) if mo in DOWN_MONTHS]
    same = (mean(up) * mean(dn) > 0) if len(up) >= 3 and len(dn) >= 3 else None
    return {"n": n, "ic": m, "t": t, "p": p, "t_rob": t_rob, "regime_same": same}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--top", type=int, default=18)
    args = ap.parse_args()

    data, weeks_month = load(Path(args.db))
    results = {}
    for name, weekly in data.items():
        r = analyse(weekly, weeks_month)
        if r:
            results[name] = r
    n_tests = len(results)
    print(f"🐝 全信号 IC 普查 · 干净口径 (close_t7) · 不重叠 ISO 周")
    print(f"   参与检验 {n_tests} 个信号 → Bonferroni 阈值 α=0.05/{n_tests} "
          f"= {0.05/n_tests:.5f}\n")
    ranked = sorted(results.items(), key=lambda kv: -abs(kv[1]["t"]))
    print(f"{'信号':<34}{'周数':>5}{'IC':>9}{'t':>7}{'p':>9}{'Bonf p':>9}"
          f"{'剔极端t':>9}{'regime':>8}")
    print("─" * 92)
    survivors = []
    for name, r in ranked[:args.top]:
        bonf = min(1.0, r["p"] * n_tests)
        reg = {True: "一致", False: "翻转", None: "样本少"}[r["regime_same"]]
        mark = "✓" if bonf < 0.05 else (" " if r["p"] >= 0.05 else "·")
        if bonf < 0.05:
            survivors.append((name, r, bonf))
        print(f"{mark}{name:<33}{r['n']:>5}{r['ic']:>+9.3f}{r['t']:>+7.2f}"
              f"{r['p']:>9.4f}{bonf:>9.4f}{r['t_rob']:>+9.2f}{reg:>8}")
    print("\n  ✓ = 过 Bonferroni（可采信）｜· = 仅过未校正 p（很可能是噪音）")
    print(f"  「剔极端t」= 剔除 |IC| 最大的 {DROP_EXTREME} 周后重算；"
          "与主 t 差距大 = 被少数几周驱动")
    print("  「regime」= 上涨期与下跌期 IC 符号是否一致；翻转 = 全样本 IC 可能是假象")

    # ── 经济幅度：把 IC 换算成可读的三分位价差 ──
    # IC 只说排序能力，不说钱。同一个信号 IC=0.17 与 IC=0.02 的差别，
    # 要看成价差才有决策意义。
    best = ranked[0][0] if ranked else None
    if best and best in data:
        print(f"\n── 头名信号「{best}」的三分位价差（毛口径，持有 T+7）──")
        wk = []
        seen = set()
        for day in sorted(data[best]):
            key = dt.date.fromisoformat(day).isocalendar()[:2]
            if key in seen:
                continue
            pairs = data[best][day]
            if len(pairs) < 9:
                continue
            seen.add(key)
            v = sorted(pairs, key=lambda x: x[0])
            k = len(v) // 3
            lo = mean(x[1] for x in v[:k])
            hi = mean(x[1] for x in v[-k:])
            wk.append((hi - lo, hi - mean(x[1] for x in v)))
        if len(wk) >= MIN_WEEKS:
            for lbl, col in (("多空价差", 0), ("仅做多超额", 1)):
                xs = [w[col] for w in wk]
                n = len(xs)
                m, sd = mean(xs), stdev(xs)
                t = m / (sd / math.sqrt(n))
                print(f"   {lbl:<12}n={n}周  均值{m:+.2f}%  中位"
                      f"{sorted(xs)[n//2]:+.2f}%  t={t:+.2f}  "
                      f"p={math.erfc(abs(t)/math.sqrt(2)):.4f}  "
                      f"正周{sum(1 for x in xs if x > 0)}/{n}")
            print("   ⚠️ 毛口径，未扣佣金/点差/冲击成本；做空腿未验证可借券。")

    print(f"\n过 Bonferroni 的信号：{len(survivors)} 个")
    for name, r, bonf in survivors:
        warn = []
        if abs(r["t_rob"]) < 2.0:
            warn.append("剔极端后失效")
        if r["regime_same"] is False:
            warn.append("regime 翻转")
        print(f"  · {name}  IC={r['ic']:+.3f}  Bonf p={bonf:.4f}"
              + ("  ⚠️ " + "、".join(warn) if warn else "  （稳健）"))
    return 0 if survivors else 3


if __name__ == "__main__":
    raise SystemExit(main())
