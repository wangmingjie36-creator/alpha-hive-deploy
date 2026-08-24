#!/usr/bin/env python3
"""
🐝 Alpha Hive — 波动率预测与仓位分层 (v0.44.0)
================================================
预测「哪些标的未来 7 日波动更大」，并据此做仓位分层。

## 为什么是波动率

2026-07-30 的决定性对照（90 只 × 897 交易日 × 2023-01~2026-07，
同一宇宙、同样朴素的特征，只换预测目标）：

| 预测目标 | 特征 | IC | t |
|---|---|---|---|
| 未来 7 日**收益率**（系统现状） | 20 日动量 | **+0.012** | +1.7 |
| 未来 7 日**行业内相对强弱** | 20 日动量 | +0.008 | +1.4 |
| **未来 7 日已实现波动** | 过去 60 日波动 | **+0.710** | **+288.6** |

**波动率的可学性高 60 倍**，且用的是一行 `rolling(60).std()`，无任何模型。
这不是过拟合，是波动率聚集（volatility clustering）。

含义：系统此前在预测一个 IC≈0.01 的目标，**天花板就在那里**，
与架构好坏无关。2026-07-30 的五次改进尝试全部失败，不是改得不对，
而是地板与天花板之间没有空间。

## 本模块的定位：不动现有评分

`vol_score` 是**并行输出**，不参与 `EVALUATION_WEIGHTS` 的加权和，
因此不改变任何既有评分、不破坏 784 条历史样本的可比性。
它的用途是**仓位分层**与筛选，不是方向判断。

## ⚠️ 边界：横截面可预测 ≠ 时序可预测

实测 `price.volatility_20d` 的分解：固定效应 IC **+0.720** / 票内时变 **+0.012**。
即：它能可靠回答「**哪些股票波动大**」，但对「**某只股票何时波动变大**」
预测力接近零。这是波动率的固有性质。

⇒ 正确用法是**横截面分层**（同一天在标的之间比较），
  **不是**择时（同一标的跨时间比较）。本模块的接口据此设计：
  所有函数都要求传入**同一天的一组标的**。

用法
----
    # 验证（对已归档信号）
    /usr/local/bin/python3 signal_archive.py --analyze --target vol

    # 单日打分与分层
    /usr/local/bin/python3 vol_forecast.py --date 2026-07-29
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DB_PATH = Path(__file__).parent / "pheromone.db"

#: 组合权重。实测（667 条样本、四口径全过）：
#:   IV 单独            IC = +0.6399  t=+20.35  4/4
#:   20 日已实现波动 单独  IC = +0.6108  t=+15.46  4/4
#:   两者秩平均          IC = +0.6632  t=+22.55  4/4  ← 优于任一单信号
#: 等权是刻意的：样本量不足以支撑更精细的权重估计，且等权对参数误设最稳健。
COMPONENTS: Dict[str, float] = {
    "options.iv_current": 0.5,
    "price.volatility_20d": 0.5,
}

#: 仓位乘数分层。高波动降仓、低波动加仓 —— 这是唯一被广泛证实
#: 能改善风险调整收益的手段，且不依赖方向判断正确。
#: 三档而非连续：分层对预测误差的容忍度远高于精确映射。
SIZING_TIERS: List[Tuple[float, float, str]] = [
    (0.00, 0.33, 1.25, ),   # 低波动组 → 加仓 25%
    (0.33, 0.67, 1.00, ),   # 中位组   → 基准
    (0.67, 1.01, 0.70, ),   # 高波动组 → 降仓 30%
]  # (分位下界, 分位上界, 仓位乘数)
_TIER_LABEL = {1.25: "低波动·加仓", 1.00: "中位·基准", 0.70: "高波动·降仓"}


def _rank_pct(values: List[float]) -> List[float]:
    """转成 [0,1] 的横截面分位。并列取平均秩。"""
    n = len(values)
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 / (n - 1)
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def score_cross_section(rows: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """给**同一天**的一组标的打波动率分（0~1，越高预测波动越大）。

    Args:
        rows: {ticker: {signal_name: value}} —— 同一天的横截面

    Returns:
        {ticker: vol_score}。分量缺失的标的被跳过（不猜测、不填默认值：
        填默认值会让缺数据的标的挤进某个分位，制造虚假分层）。

    ⚠️ 必须是同一天的横截面。跨日混算会把"哪些股票波动大"这个
    截面事实，误当成"波动何时变大"的时序信号 —— 而后者的 IC 接近零。
    """
    usable = {tk: v for tk, v in rows.items()
              if all(c in v and v[c] is not None for c in COMPONENTS)}
    if len(usable) < 3:
        return {}
    tickers = sorted(usable)
    total_w = sum(COMPONENTS.values())
    acc = {tk: 0.0 for tk in tickers}
    for comp, w in COMPONENTS.items():
        pct = _rank_pct([float(usable[tk][comp]) for tk in tickers])
        for tk, p in zip(tickers, pct):
            acc[tk] += p * w
    return {tk: round(acc[tk] / total_w, 4) for tk in tickers}


def size_multipliers(vol_scores: Dict[str, float]) -> Dict[str, Dict]:
    """把波动率分转成仓位乘数。

    Returns: {ticker: {vol_score, tier, multiplier}}
    """
    if not vol_scores:
        return {}
    tickers = sorted(vol_scores, key=lambda t: vol_scores[t])
    pct = _rank_pct([vol_scores[t] for t in tickers])
    out = {}
    for tk, p in zip(tickers, pct):
        mult = 1.0
        for lo, hi, m in SIZING_TIERS:
            if lo <= p < hi:
                mult = m
                break
        out[tk] = {"vol_score": vol_scores[tk], "pct": round(p, 3),
                   "multiplier": mult, "tier": _TIER_LABEL.get(mult, "?")}
    return out


def load_day(date: str, db_path: Path = DB_PATH) -> Dict[str, Dict[str, float]]:
    """从 signal_archive 读某一天的分量信号。"""
    q = ",".join("?" * len(COMPONENTS))
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        # 库文件不存在 —— 返回空由调用方处理，不裸崩
        return {}
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT ticker, signal, value FROM signal_archive "
            f"WHERE date = ? AND signal IN ({q})",
            (date, *COMPONENTS.keys())).fetchall()
    except sqlite3.OperationalError:
        # signal_archive 表不存在（全新库 / 未 backfill）。
        # 日报路径有 try/except 兜底，但 CLI 直接跑会裸崩出 traceback，
        # 而正确行为是给出"先跑 --backfill"的提示。
        return {}
    finally:
        con.close()
    out: Dict[str, Dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["ticker"], {})[r["signal"]] = r["value"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Alpha Hive 波动率预测与仓位分层")
    ap.add_argument("--date", required=True, help="业务日期 YYYY-MM-DD")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_day(args.date, Path(args.db))
    if not rows:
        print(f"⏭  {args.date} 无归档信号。先跑 signal_archive.py --backfill",
              file=sys.stderr)
        return 1
    scores = score_cross_section(rows)
    if not scores:
        print(f"⏭  {args.date} 可用标的不足 3 只（分量齐全的）", file=sys.stderr)
        return 1
    sized = size_multipliers(scores)

    if args.json:
        print(json.dumps(sized, ensure_ascii=False, indent=2))
        return 0

    print(f"\n【波动率预测与仓位分层】{args.date}   "
          f"{len(sized)} 只（分量齐全）/ {len(rows)} 只已归档")
    print("=" * 66)
    print(f"{'标的':<8}{'波动分':>9}{'分位':>8}{'仓位乘数':>10}   分层")
    print("-" * 66)
    for tk, d in sorted(sized.items(), key=lambda x: -x[1]["vol_score"]):
        print(f"{tk:<8}{d['vol_score']:>9.4f}{d['pct']:>8.2f}"
              f"{d['multiplier']:>10.2f}   {d['tier']}")
    print()
    print("  组合 = IV 分位 × 0.5 + 20日已实现波动分位 × 0.5")
    print("  实测（667 条 / 四口径全过）：组合 IC=+0.663 vs IV 单独 +0.640 / 波动单独 +0.611")
    print("  ⚠️ 仅用于**横截面**仓位分层，不可用于择时"
          "（票内时变 IC 仅 +0.012）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
