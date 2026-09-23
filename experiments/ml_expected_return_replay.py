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
  （由**当时**的 rival_bee 硬编码 catalyst_quality="B+"、crowding_score=50.0
   代入旧公式所得，已在 1057 个配对样本上零反例验证。⚠️ v0.44.3 起这四个特征
   已改读信息素板真实值——本脚本是历史重放，勿据此描述现状）
· 新公式：`mag("B+")=1.0`、crowding=50 ⇒ tilt=0 ⇒ `expected_7d = 0.8 × momentum_5d`
· 真实收益：由 `predictions.price_at_predict` 与 T+7 **收盘价** `close_t7` 算；
  终点列唯一真相 `ic_diagnostics.FORWARD_CLOSE_COL["t7"]`，调用时查表，不在本文件写死

  ⚠️ v0.45.326 更正：此前这里读 `price_t7`，理由是「不用 `return_t7`，它路径依赖、
  被 SL/TP 截断」——理由对，列选错了。`price_t7` 存的是
  `backtester._simulate_trade_path` 的 `exit_price`（**离场价**，v16.0 / 2026-04-15 起），
  与 `return_t7` 一样截断：2026-05 起 100% 等于 `exit_price`，SL/TP 行 87.6% 等于它。
  另有 CRWD 两行 `price_at_predict` 已按 07-02 的 4:1 拆股复权、`price_t7` 仍是
  未复权价 ⇒ 旧口径读成 +325% / +340%。与 `ic_diagnostics`（v0.45.19）、
  `signal_archive.load_panel`（v0.45.321）是**同一个误解**。
  **v0.45.326 之前本脚本的全部输出（含 `ml_expected_return_report.md` 的 697 条回放）
  都是对着截断收益算的。**

· 信号强度与判定（v0.45.329 起）：标准横截面口径，照抄 `experiments/final_score_dilution.py`
  的 `weekly_ic`——日度横截面 rank-IC（当日 ≥ `MIN_WIDTH` 只、信号至少 2 个不同取值）
  → 每 ISO 周取第一个可用交易日 → 对周序列做 **t(n−1)** 检验（那边是正态近似，这里不抄）。
  「v0.44.2 方向准确率 − 恒定看多」同样先在**同一天、同一批行**上配对相减，再走同一条周序列。
  判定只看 p < `ALPHA`，不看 IC / 准确率差的绝对大小。

  ⚠️ 此前（≤ v0.45.328）是**全样本池化**：跨全部日期与标的一次性 Spearman，再按写死的
  IC ±0.02、准确率 ±1pp 印「负相关 / 短期反转」「优于 / 持平 / 差于」。池化 IC 主要在测
  「哪天涨」而不是「同一天该挑哪只」（memory `alpha-hive-cross-sectional-pooling`，第三例）；
  两个阈值都在噪音之下——v0.45.326 只换收益列，两条印出的判定就一起翻了，而前后都不显著。
  池化 IC 仍输出（`pooled_across_dates`），只作对照、不参与判定。
  **去池化要去到同一天**：周内几天合在一起算一个 IC 仍在测「哪天涨」（v0.45.326 复核时
  实测造出过假 p=0.05）。

用法
----
    /usr/local/bin/python3 experiments/ml_expected_return_replay.py
    /usr/local/bin/python3 experiments/ml_expected_return_replay.py --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from scipy import stats as _scipy_stats  # t(n−1)，与 dim_ic_forward_test / resonance_boost_forward_test 同一实现

ALPHAHIVE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ALPHAHIVE_DIR))

# v0.45.260（数据根迁移阶段 2）：此前是 `ALPHAHIVE_DIR / "pheromone.db"`
# （`ALPHAHIVE_DIR` 是 `__file__` 派生）——不读 `ALPHA_HIVE_HOME`。改读
# `PATHS.db`；`ALPHAHIVE_DIR` 本身保留，只喂上面的 `sys.path.insert`。
from hive_logger import PATHS as _PATHS  # noqa: E402
# 前瞻终点列唯一真相 FORWARD_CLOSE_COL；横截面口径的 spearman / subsample_non_overlapping
import ic_diagnostics as _icd  # noqa: E402
DB_PATH = Path(_PATHS.db)

# ── 横截面周序列口径（v0.45.329）：数值照抄 final_score_dilution.py，改一处见 weekly_t_test ──
MIN_WIDTH = 5     # = final_score_dilution.MIN_WIDTH：当日横截面不足 5 只不算
MIN_WEEKS = 3     # = final_score_dilution.stat 的 `n < 3 → None`
ALPHA = 0.05      # 两侧。本脚本两条 IC + 一条准确率差，均为探索性、不做族校正（输出里写明）
IC_METHOD = ("日度横截面 rank-IC（当日 ≥ %d 只）→ 每 ISO 周第一个可用交易日 → 周序列 t(n−1) 检验"
             % MIN_WIDTH)


def forward_close_col() -> str:
    """真实 7 日收益的终点价列。**调用时**查 `ic_diagnostics.FORWARD_CLOSE_COL`，
    不在模块层冻成常量——否则表改了这里不跟，测试也证明不了本脚本在读那张表。"""
    return _icd.FORWARD_CLOSE_COL["t7"]


def load_pairs(db_path: Path) -> List[Tuple[str, str, float, float, float]]:
    """(date, ticker, momentum_5d, crowding_score, 真实7日收益%)。

    真实 7 日收益 = `(终点收盘价 / price_at_predict − 1) × 100`，终点列见
    `forward_close_col()`（= close_t7）。**不是 price_t7**：那是 SL/TP 离场价，
    见模块 docstring 的 v0.45.326 更正。

    v0.44.2 起把真实 `crowding.score` 一起取出来 —— RivalBee 不再写死 50.0，
    所以回放必须用真实拥挤度，否则测不出这次改动的效果。
    """
    end_col = forward_close_col()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            f"""
            SELECT p.date, p.ticker, m.value AS mom, c.value AS crd,
                   (p.{end_col} / p.price_at_predict - 1.0) * 100.0 AS ret
            FROM predictions p
            JOIN signal_archive m
              ON m.date = p.date AND m.ticker = p.ticker
             AND m.signal = 'price.momentum_5d'
            JOIN signal_archive c
              ON c.date = p.date AND c.ticker = p.ticker
             AND c.signal = 'crowding.score'
            WHERE p.checked_t7 = 1
              AND p.{end_col} IS NOT NULL
              AND p.price_at_predict > 0
            """
        ).fetchall()
    finally:
        con.close()
    return [(d, t, float(m), float(c), float(r)) for d, t, m, c, r in rows
            if None not in (m, c, r)]


def old_expected(mom: float, crd: float) -> float:
    """旧闭式。**v0.44.3 之前**的 rival_bee 把 catalyst_quality="B+"、
    crowding_score=50.0 写死，代入旧式 `(15 + mom − 5) × 0.8` ⇒ `8.0 + 0.8×mom`
    （真实 crowding 被丢弃）。现已改读信息素板真实值。"""
    return 8.0 + 0.8 * mom


def v441_expected(mom: float, crd: float) -> float:
    """v0.44.1：公式已居中，但**当时**的 rival_bee 仍写死 crowding=50.0
    ⇒ tilt 恒为 0（v0.44.3 已改读真实拥挤度）。"""
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
    """秩相关（并列取平均秩）。v0.45.329 起只用于**全样本池化对照**（`pooled_across_dates`）；
    横截面口径用 `ic_diagnostics.spearman`，与 final_score_dilution 同源。"""
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


# ────────────────────────────────────────────────────────────────────────────
# 横截面周序列口径（v0.45.329）
# ────────────────────────────────────────────────────────────────────────────

#: 一条配对样本在 by_day 里的形状：(momentum_5d, crowding_score, 真实7日收益%)
MOM, CRD, RET = 0, 1, 2


def group_by_day(pairs: List[Tuple[str, str, float, float, float]]
                 ) -> Dict[str, List[Tuple[float, float, float]]]:
    by_day: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
    for d, _t, m, c, r in pairs:
        by_day[d].append((m, c, r))
    return dict(by_day)


def daily_cross_sectional_ic(by_day: Dict[str, List[Tuple[float, float, float]]],
                             col: int) -> Dict[str, float]:
    """逐日横截面 rank-IC。日内过滤规则照抄 `final_score_dilution.weekly_ic`：
    宽度 ≥ MIN_WIDTH、信号至少 2 个不同取值、spearman 可算。**只在同一天内排序**。"""
    daily: Dict[str, float] = {}
    for day, recs in by_day.items():
        if len(recs) < MIN_WIDTH:
            continue
        xs = [rec[col] for rec in recs]
        if len({round(x, 9) for x in xs}) < 2:
            continue
        ic = _icd.spearman(xs, [rec[RET] for rec in recs])
        if ic is not None:
            daily[day] = ic
    return daily


def weekly_t_test(series: List[float]) -> Optional[Dict]:
    """对不重叠周序列做均值 = 0 的两侧 t 检验。

    与 `final_score_dilution.stat` 唯一的不同：p 用 **t(n−1)**，不用 `erfc` 正态近似
    （n≈20 周时正态低估 p 约 3×，见 memory `alpha-hive-t-vs-normal-p`）。
    周数 < MIN_WEEKS 或周序列零方差 ⇒ None（判定为「无法判定」，不是「不显著」）。
    """
    n = len(series)
    if n < MIN_WEEKS:
        return None
    sd = statistics.stdev(series)
    if sd == 0:
        return None
    m = statistics.fmean(series)
    t = m / (sd / math.sqrt(n))
    return {"n_weeks": n, "mean": m, "t": t, "df": n - 1,
            "p": float(2 * _scipy_stats.t.sf(abs(t), n - 1))}


def significance_verdict(test: Optional[Dict]) -> str:
    """insufficient / not_significant / positive / negative —— 只看 p，不看效应大小。"""
    if test is None:
        return "insufficient"
    if not test["p"] < ALPHA:
        return "not_significant"
    return "positive" if test["mean"] > 0 else "negative"


def weekly_series(daily: Dict[str, float]) -> List[float]:
    """每 ISO 周取第一个可用交易日（`ic_diagnostics.subsample_non_overlapping`，同 final_score_dilution）。
    **不要**把周内几天合成一个横截面、也不要用全部日子当样本：前者仍在测「哪天涨」，
    后者前瞻窗口互相重叠、n 被虚增。"""
    return _icd.subsample_non_overlapping(daily, "周")


def _weekly_block(daily: Dict[str, float]) -> Dict:
    test = weekly_t_test(weekly_series(daily))
    return {
        "n_days": len(daily),
        # 全部有效日的均值：前瞻窗口互相重叠，**只作参照，不做检验**
        "daily_mean_overlapping": statistics.fmean(daily.values()) if daily else float("nan"),
        "weekly": test,
        "verdict": significance_verdict(test),
    }


def cross_sectional_ic(by_day: Dict[str, List[Tuple[float, float, float]]], col: int) -> Dict:
    """标准口径的 IC 块；池化值由调用方另挂 `pooled_across_dates`，不参与判定。"""
    return _weekly_block(daily_cross_sectional_ic(by_day, col))


def accuracy_edge_vs_always_bullish(by_day: Dict[str, List[Tuple[float, float, float]]],
                                    pred_fn: Callable[[float, float], float]) -> Dict:
    """「方向准确率 − 恒定看多准确率」的横截面周序列。

    每行配对：`[模型命中] − [收益 > 0]`，与 `_accuracy` 同样剔除弃权（预测或收益恰为 0）；
    当日有效行 ≥ MIN_WIDTH 才取当日均值。两个准确率在**同一天、同一批行**上相减，
    「哪天大盘涨」对两边同时生效、在差里抵消——然后走与 IC 同一条周序列 t 检验。
    """
    daily: Dict[str, float] = {}
    for day, recs in by_day.items():
        diffs = []
        for m, c, r in recs:
            dp, dr = _direction(pred_fn(m, c)), _direction(r)
            if dp == 0 or dr == 0:
                continue
            diffs.append(float(dp == dr) - float(dr > 0))
        if len(diffs) >= MIN_WIDTH:
            daily[day] = statistics.fmean(diffs)
    return _weekly_block(daily)


def _dim_ic_forward_start() -> str:
    """维度 IC 预注册的窗口起点。唯一真相 `experiments/dim_ic_protocol.FORWARD_START`；
    按文件加载（同 dim_ic_forward_test），该文件是代码资源，`__file__` 锚点正确。"""
    spec = importlib.util.spec_from_file_location(
        "dim_ic_protocol", Path(__file__).resolve().parent / "dim_ic_protocol.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FORWARD_START

_VERDICT_DEFAULT = {
    "not_significant": ("➖ 未检出方向信息 —— 不能读成「反转」「正相关」或「接近 0」",),
    "insufficient": (f"❔ 有效周 < {MIN_WEEKS} 或周序列零方差 —— 无法判定",),
}


def _print_ic(label: str, block: Dict, significant_lines: Dict[str, Tuple[str, ...]]) -> None:
    wk = block["weekly"]
    if wk:
        print(f"  {label}：周序列 IC {wk['mean']:+.4f}（{wk['n_weeks']} 周，t={wk['t']:+.2f}，"
              f"p={wk['p']:.3f}）   日度均值 {block['daily_mean_overlapping']:+.4f}"
              f"（{block['n_days']} 天，窗口重叠，仅参照）")
    else:
        print(f"  {label}：周序列不可用（有效日 {block['n_days']} 天）")
    lines = significant_lines.get(block["verdict"]) or _VERDICT_DEFAULT[block["verdict"]]
    for ln in lines:
        print(f"    {ln}")


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

    by_day = group_by_day(pairs)
    ic_mom = cross_sectional_ic(by_day, MOM)
    ic_mom["pooled_across_dates"] = _spearman(moms, rets)   # 只作对照，不参与判定
    ic_crd = cross_sectional_ic(by_day, CRD)
    ic_crd["pooled_across_dates"] = _spearman(crds, rets)
    dates = sorted(by_day)
    forward_start = _dim_ic_forward_start()

    result = {
        "n_pairs": len(pairs),
        "date_range": [dates[0], dates[-1]],
        "n_pairs_on_or_after_dim_ic_forward_start": sum(
            len(v) for d, v in by_day.items() if d >= forward_start),
        "dim_ic_forward_start": forward_start,
        "forward_close_col": forward_close_col(),
        "ic_method": IC_METHOD,
        "alpha": ALPHA,
        # v0.45.329：原 `ic_momentum_vs_forward` / `ic_crowding_vs_forward`（全样本池化）
        # 改名拆分——键名变了是有意的：旧读者该报 KeyError，而不是静默读到另一个量。
        "momentum_ic": ic_mom,
        "crowding_ic": ic_crd,
        "new_accuracy_edge_vs_always_bullish": accuracy_edge_vs_always_bullish(by_day, new_expected),
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
    print(f"  配对样本: {len(pairs)} 条（有 momentum_5d 且 T+7 已回填），"
          f"{dates[0]} ~ {dates[-1]}，{len(dates)} 个日期")
    print(f"  真实收益终点列: {result['forward_close_col']}（T+7 收盘价；price_t7 是 SL/TP 离场价，"
          f"见 ic_diagnostics.FORWARD_CLOSE_COL）")
    n_fwd = result["n_pairs_on_or_after_dim_ic_forward_start"]
    if n_fwd:
        print(f"  ⚠️ 含 {n_fwd} 条 ≥ {forward_start}（维度 IC 预注册窗口起点）的样本：本输出只作探索，"
              f"不构成该协议的证据；检视点前别为「看趋势」反复跑（dim_ic_preregistration.md §8）")
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
    print("  （以上准确率是全样本合计，只作描述；与恒定看多「谁更好」的判定见【结论】，按周序列检验）")
    print()
    print("【两个输入信号各自的强度】")
    print(f"  口径：{IC_METHOD}；两侧 α={ALPHA}，未做族校正（探索性）")
    print("  判定只看 p：不显著 = 正、负、零都不能排除，**不是**「接近 0」")
    _print_ic("5 日动量", ic_mom, {
        "negative": ("⚠️ 显著负相关 —— 有短期反转证据。sign(动量) 作方向会系统性地错，",
                     "   新公式虽然无偏，但方向可能比恒定看多更差。见下方结论。"),
        "positive": ("✅ 显著正相关 —— sign(动量) 作方向有正向信号",),
    })
    # v0.45.326：此处原写死「MEMORY 记载 crowding.adj_factor = −0.112, 3/4」——那是
    # v0.45.321 之前 `signal_archive --analyze` 对着截断收益算的数，已整体作废。只留指针。
    _print_ic("拥挤度", ic_crd, {
        "negative": ("⚠️ 显著负相关（四口径判定以 signal_archive.py --analyze 的当期输出为准）",),
        "positive": ("✅ 显著正相关（四口径判定以 signal_archive.py --analyze 的当期输出为准）",),
    })
    print(f"  全样本池化 IC（跨日期混算，主要在测「哪天涨」；**只作对照、不参与判定**）："
          f"动量 {ic_mom['pooled_across_dates']:+.4f} / 拥挤度 {ic_crd['pooled_across_dates']:+.4f}")
    print()
    print("━" * 78)
    print("【结论】")
    print(f"  · 偏差：旧公式高估 {result['bias_old']:+.2f}pp，新公式 {result['bias_new']:+.2f}pp"
          f" —— 幅度问题已修")
    if abs(result["bias_new"]) < abs(result["bias_old"]):
        print("  · ✅ 无偏性改善（这是本次修复的直接目标）")
    edge = result["new_accuracy_edge_vs_always_bullish"]
    wk = edge["weekly"]
    stat_s = (f"周序列 {wk['mean']:+.1%}，{wk['n_weeks']} 周，t={wk['t']:+.2f}，p={wk['p']:.2f}"
              if wk else f"有效周 < {MIN_WEEKS} 或零方差")
    head = {"positive": "✅ 显著优于", "negative": "⚠️ 显著差于",
            "not_significant": "➖ 未检出与", "insufficient": "❔ 无法判定是否不同于"}[edge["verdict"]]
    tail = "有差异" if edge["verdict"] == "not_significant" else ""
    print(f"  · v0.44.2 方向准确率 {head}恒定看多基准{tail}（同日配对差 → {stat_s}）")
    if edge["verdict"] == "not_significant":
        print("    不显著 ≠ 持平：优于、差于、相同都不能排除。")
    if edge["verdict"] != "positive":
        print("    ⚠️ 修复消除了偏斜，但**没有证据**表明带来了方向上的改善。")
        print("       诚实的读法：旧的高准确率来自样本期偏多，不是预测能力。")
    print("━" * 78)
    return 0



if __name__ == "__main__":
    sys.exit(main())
