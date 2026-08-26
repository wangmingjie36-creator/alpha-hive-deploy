#!/usr/bin/env python3
"""
🐝 ML 预期收益修复的前后回放 (v0.44.1)
=======================================
在真实历史数据上对比**旧公式**与**新公式**，并与真实 7 日收益对照。

为什么必须跑这个
----------------
MEMORY 里的硬规则：「任何评分/权重改动上线前必须跑基准对照并证明相对基准有改善。
**只比「改动前的自己」好是在噪音里挑选。**」

而本次修复有一个必须诚实检验的风险：新公式在 `rival_bee` 的硬编码特征下退化为
`expected_7d = 0.8 × momentum_5d`，方向即 `sign(momentum_5d)`。**短期反转是已知
效应** —— 若 5 日动量与未来 7 日收益负相关，那么新方向会系统性地错，
虽然它是无偏的。「无偏」和「有用」是两件事，本脚本把两者分开报。

口径
----
· 旧公式闭式：`expected_7d = 8.0 + 0.8 × momentum_5d`
  （由 rival_bee 硬编码 catalyst_quality="B+"、crowding_score=50.0 代入旧公式所得，
   已在 1057 个配对样本上零反例验证）
· 新公式：`mag("B+")=1.0`、crowding=50 ⇒ tilt=0 ⇒ `expected_7d = 0.8 × momentum_5d`
· 真实收益：由 `predictions.price_at_predict` 与 `price_t7` 直接算（不用
  `return_t7`，后者是路径依赖的，42.5% 被 SL/TP 截断）

用法
----
    /usr/local/bin/python3 experiments/ml_expected_return_replay.py
    /usr/local/bin/python3 experiments/ml_expected_return_replay.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ALPHAHIVE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ALPHAHIVE_DIR))

DB_PATH = ALPHAHIVE_DIR / "pheromone.db"


def load_pairs(db_path: Path) -> List[Tuple[str, str, float, float, float]]:
    """(date, ticker, momentum_5d, crowding_score, 真实7日收益%)。

    v0.44.2 起把真实 `crowding.score` 一起取出来 —— RivalBee 不再写死 50.0，
    所以回放必须用真实拥挤度，否则测不出这次改动的效果。
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT p.date, p.ticker, m.value AS mom, c.value AS crd,
                   (p.price_t7 / p.price_at_predict - 1.0) * 100.0 AS ret
            FROM predictions p
            JOIN signal_archive m
              ON m.date = p.date AND m.ticker = p.ticker
             AND m.signal = 'price.momentum_5d'
            JOIN signal_archive c
              ON c.date = p.date AND c.ticker = p.ticker
             AND c.signal = 'crowding.score'
            WHERE p.checked_t7 = 1
              AND p.price_t7 IS NOT NULL
              AND p.price_at_predict > 0
            """
        ).fetchall()
    finally:
        con.close()
    return [(d, t, float(m), float(c), float(r)) for d, t, m, c, r in rows
            if None not in (m, c, r)]


def old_expected(mom: float, crd: float) -> float:
    """旧闭式。rival_bee 把 catalyst_quality="B+"、crowding_score=50.0 写死，
    代入旧式 `(15 + mom − 5) × 0.8` ⇒ `8.0 + 0.8×mom`（真实 crowding 被丢弃）。"""
    return 8.0 + 0.8 * mom


def v441_expected(mom: float, crd: float) -> float:
    """v0.44.1：公式已居中，但 rival_bee 仍写死 crowding=50.0 ⇒ tilt 恒为 0。"""
    return 0.8 * mom


def v442_tilt_expected(mom: float, crd: float) -> float:
    """**已否决的方案**：把真实拥挤度做成双向倾斜项。

    保留在此仅为对照，说明为什么否决 —— 它的偏差（+0.19pp）比最终方案
    （+1.06pp）**更好**，但那是巧合：倾斜的均值恰好抵消了动量带来的正偏，
    不是因为符号对。四口径复核显示拥挤度方向未确立（连续版仅 1/4 口径），
    且它已在 probability 里是权重最大的特征。详见 ml_predictor 的长注释。
    """
    neutral, scale, cap = 23.30, 14.92, 5.0
    tilt = (crd - neutral) / scale * cap
    tilt = max(-cap, min(cap, tilt))
    return (mom - tilt) * 0.8


def new_expected(mom: float, crd: float) -> float:
    """v0.44.2 最终方案：公式居中，拥挤度**不进入**（唯一真相 = ml_predictor）。

    真实拥挤度仍然被传给 `TrainingData` —— 它在 `probability` 里是权重最大的
    特征（0.18），那条路径确实用上了。只是不参与收益预测。
    """
    import ml_predictor as mp

    return mom * mp._HORIZON_SCALE["expected_7d"]


def _direction(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _accuracy(preds: List[float], rets: List[float]) -> Dict:
    """方向准确率。**弃权（预测为 0）单独统计，不计入分母** ——
    把弃权算成错会低估一个诚实说"不知道"的模型。"""
    hit = miss = abstain = 0
    for p, r in zip(preds, rets):
        dp, dr = _direction(p), _direction(r)
        if dp == 0 or dr == 0:
            abstain += 1
            continue
        if dp == dr:
            hit += 1
        else:
            miss += 1
    n = hit + miss
    return {
        "n_directional": n, "hit": hit, "miss": miss, "abstain": abstain,
        "accuracy": (hit / n) if n else float("nan"),
    }


def _spearman(x: List[float], y: List[float]) -> float:
    """秩相关（并列取平均秩）。"""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else float("nan")


def _dist(v: List[float]) -> Dict:
    s = sorted(v)
    n = len(s)
    return {
        "n": n,
        "min": s[0], "p5": s[int(0.05 * n)], "median": s[n // 2],
        "p95": s[min(n - 1, int(0.95 * n))], "max": s[-1],
        "mean": statistics.fmean(s),
        "pct_positive": sum(1 for x in s if x > 0) / n,
        "pct_zero": sum(1 for x in s if x == 0) / n,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ML 预期收益修复前后回放")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"❌ 找不到 {db}", file=sys.stderr)
        return 2

    pairs = load_pairs(db)
    if len(pairs) < 30:
        print(f"❌ 配对样本不足（{len(pairs)}）", file=sys.stderr)
        return 2

    moms = [m for _, _, m, _, _ in pairs]
    crds = [c for _, _, _, c, _ in pairs]
    rets = [r for _, _, _, _, r in pairs]
    old = [old_expected(m, c) for m, c in zip(moms, crds)]
    v441 = [v441_expected(m, c) for m, c in zip(moms, crds)]
    tilt = [v442_tilt_expected(m, c) for m, c in zip(moms, crds)]
    new = [new_expected(m, c) for m, c in zip(moms, crds)]

    result = {
        "n_pairs": len(pairs),
        "actual": _dist(rets),
        "old_pred": _dist(old),
        "v441_pred": _dist(v441),
        "rejected_tilt_pred": _dist(tilt),
        "new_pred": _dist(new),
        "old_accuracy": _accuracy(old, rets),
        "v441_accuracy": _accuracy(v441, rets),
        "rejected_tilt_accuracy": _accuracy(tilt, rets),
        "new_accuracy": _accuracy(new, rets),
        "bias_rejected_tilt": statistics.fmean(t - r for t, r in zip(tilt, rets)),
        "mae_rejected_tilt": statistics.fmean(abs(t - r) for t, r in zip(tilt, rets)),
        "ic_momentum_vs_forward": _spearman(moms, rets),
        "ic_crowding_vs_forward": _spearman(crds, rets),
        "always_bullish_accuracy": sum(1 for r in rets if r > 0) / len(rets),
        "bias_old": statistics.fmean(o - r for o, r in zip(old, rets)),
        "bias_v441": statistics.fmean(v - r for v, r in zip(v441, rets)),
        "bias_new": statistics.fmean(n - r for n, r in zip(new, rets)),
        "mae_old": statistics.fmean(abs(o - r) for o, r in zip(old, rets)),
        "mae_v441": statistics.fmean(abs(v - r) for v, r in zip(v441, rets)),
        "mae_new": statistics.fmean(abs(n - r) for n, r in zip(new, rets)),
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    def row(label, d):
        return (f"  {label:14s} n={d['n']:4d}  中位 {d['median']:+7.2f}  "
                f"均值 {d['mean']:+7.2f}  p5 {d['p5']:+7.2f}  "
                f"p95 {d['p95']:+7.2f}  为正 {d['pct_positive']:5.1%}")

    print("━" * 78)
    print("🐝 ML 预期收益修复 · 真实数据前后回放")
    print("━" * 78)
    print(f"  配对样本: {len(pairs)} 条（有 momentum_5d 且 T+7 已回填）")
    print()
    print("【分布对照】单位：百分点")
    print(row("真实 7 日收益", result["actual"]))
    print(row("旧公式(≤0.44.0)", result["old_pred"]))
    print(row("v0.44.1 居中", result["v441_pred"]))
    print(row("[已否决] +拥挤倾斜", result["rejected_tilt_pred"]))
    print(row("v0.44.2 最终", result["new_pred"]))
    print()
    print("【偏差】预测 − 真实 的平均值（越接近 0 越无偏）")
    print(f"  旧公式          {result['bias_old']:+7.2f} pp   MAE {result['mae_old']:6.2f}")
    print(f"  v0.44.1         {result['bias_v441']:+7.2f} pp   MAE {result['mae_v441']:6.2f}")
    print(f"  [已否决] 拥挤倾斜 {result['bias_rejected_tilt']:+7.2f} pp   "
          f"MAE {result['mae_rejected_tilt']:6.2f}   ← 偏差更小，但符号不可辩护")
    print(f"  v0.44.2 最终    {result['bias_new']:+7.2f} pp   MAE {result['mae_new']:6.2f}")
    print()
    print("【方向准确率】弃权（预测恰为 0）不计入分母")
    for tag, key in (("旧公式", "old_accuracy"), ("v0.44.1", "v441_accuracy"),
                     ("[已否决]拥挤倾斜", "rejected_tilt_accuracy"),
                     ("v0.44.2 最终", "new_accuracy")):
        a = result[key]
        acc = a["accuracy"]
        acc_s = f"{acc:.1%}" if math.isfinite(acc) else "n/a"
        print(f"  {tag}: {acc_s}  (命中 {a['hit']}, 错 {a['miss']}, "
              f"弃权 {a['abstain']})")
    print(f"  对照 · 恒定看多: {result['always_bullish_accuracy']:.1%}"
          f"  ← 旧公式实质上就是这个")
    print()
    print("【两个输入信号各自的强度】")
    ic_c = result["ic_crowding_vs_forward"]
    print(f"  拥挤度 vs 未来 7 日收益 的 rank-IC = {ic_c:+.4f}"
          f"   （MEMORY 记载 crowding.adj_factor = −0.112, 3/4 —— 47 个信号里"
          f"仅 2 个达标之一）")
    ic = result["ic_momentum_vs_forward"]
    print(f"  5 日动量 vs 未来 7 日收益 的 rank-IC = {ic:+.4f}")
    if ic < -0.02:
        print("  ⚠️  **负相关** —— 存在短期反转效应。sign(动量) 作方向会系统性地错，")
        print("      新公式虽然无偏，但方向可能比恒定看多更差。见下方结论。")
    elif ic > 0.02:
        print("  ✅ 正相关 —— sign(动量) 作方向有正向信号")
    else:
        print("  ➖ 接近 0 —— 动量在此尺度上无方向信息，新方向≈随机（但无偏）")
    print()
    print("━" * 78)
    print("【结论】")
    print(f"  · 偏差：旧公式高估 {result['bias_old']:+.2f}pp，新公式 {result['bias_new']:+.2f}pp"
          f" —— 幅度问题已修")
    if abs(result["bias_new"]) < abs(result["bias_old"]):
        print("  · ✅ 无偏性改善（这是本次修复的直接目标）")
    if math.isfinite(result["new_accuracy"]["accuracy"]):
        delta = result["new_accuracy"]["accuracy"] - result["always_bullish_accuracy"]
        verdict = "优于" if delta > 0.01 else ("持平" if abs(delta) <= 0.01 else "**差于**")
        print(f"  · 方向准确率 {verdict} 恒定看多基准（差 {delta:+.1%}）")
        if delta < -0.01:
            print("    ⚠️ 修复消除了偏斜，但**没有**带来方向上的改善。")
            print("       诚实的读法：旧的高准确率来自样本期偏多，不是预测能力。")
    print("━" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
