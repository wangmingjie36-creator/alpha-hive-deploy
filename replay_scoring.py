#!/usr/bin/env python3
"""
🐝 Alpha Hive — 评分重放 (v0.45.33)
=====================================
把「这样改会不会更准」从**等 25 个不重叠周**（约半年）变成**跑一下**。

为什么需要
----------
`ic_rerun_readiness` 实测：检出 |IC|=0.090 需 25 个不重叠周，当前世代 0/25。
于是每个评分改动都要等半年才知道有没有用，而半年内必然又改了别的 ——
**永远学不到东西**。这是本项目真正的瓶颈，不是缺改进想法。

但并非所有改动都要等：

| 改动类型 | 能否离线重放 | 依据 |
|---|---|---|
| **聚合层**（权重、组合规则、剔除某维度） | ✅ 立刻 | `predictions.dimension_scores` 已存各维度分 |
| **维度计算层**（改 crowding/catalyst 公式） | ✅ v0.45.33 起 | `signal_archive` 现存维度**输入**（v0.45.33 扩展） |
| 换数据源、改抓取逻辑 | ❌ 必须前向累积 | 原始外部数据未归档 |

⚠️ 本工具**不产出「最优权重」建议**。权重自 v0.44.0 起只读，且实测
单维 IC 均不过 Bonferroni（见 MEMORY alpha-hive-final-score-cancellation）。
它的用途是**排除**明显更差的方案、以及量化「某维度到底贡献了什么」。

口径（照抄 MEMORY 的血泪教训，勿改）
------------------------------------
- 前向收益用 `close_t7 / price_at_predict - 1`。**不要用 `return_t7`** ——
  它对 SL/TP 方向单是钳位离场收益、对中性单是原始收益，直接对比即无效
  （见 MEMORY alpha-hive-return-t7-clamp）。
- 剔除 `dir_ambiguous_t7`（|收益| 在噪音带内的模糊样本）。
- **有效样本量是不重叠 ISO 周数，不是行数。** 同日多标的 + 每日重叠的
  T+7 窗口会让 naive n 高估数倍。任何 IC 都必须与周数一起读。
- 跨世代样本口径不可比，默认只用最新世代；`--all-cohorts` 显式放宽，
  结果只能做**相对比较**，不能当绝对水平。

用法
----
    /usr/local/bin/python3 replay_scoring.py                  # 内置情景对比
    /usr/local/bin/python3 replay_scoring.py --all-cohorts    # 放宽到全历史
    /usr/local/bin/python3 replay_scoring.py --json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import sqlite3
import sys
from statistics import mean
from typing import Callable, Dict, List, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pheromone.db")
DIMS = ("signal", "catalyst", "sentiment", "odds", "risk_adj")


# ══════════════════════════════════════════════════════════════════════════
# 统计
# ══════════════════════════════════════════════════════════════════════════

def rank_ic(xs: List[float], ys: List[float]) -> Optional[float]:
    """Spearman 秩相关。**直接用 ic_diagnostics.spearman，不另写一份。**

    ⚠️ v0.45.35 修：初版自己写了个 _rank，给并列值分配**递增秩**而非平均秩。
    后果不是小偏差 —— 构造一组与 y 完全无关、但 x 大量并列的数据，
    正确答案 0.0，初版给出 **+0.2967**：凭空造出相关性。
    而 catalyst 恰恰只有约 6 个不同取值（30 只标的），并列极多，
    正是最容易被这个 bug 放大的维度。
    `ic_diagnostics.spearman` 早就正确处理了并列（平均秩），复制一份等于
    重新引入已被解决的问题 —— 同 v0.45.30 CrowdingDetector 硬编码第二份权重。
    """
    if len(xs) < 10:
        return None
    from ic_diagnostics import spearman
    return spearman(xs, ys)


def _iso_weeks(dates: List[str]) -> int:
    return len({_dt.date.fromisoformat(d).isocalendar()[:2] for d in dates})


# ══════════════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════════════

def latest_cohort_start() -> Optional[str]:
    try:
        from ic_rerun_readiness import _COHORT_HISTORY
        return _COHORT_HISTORY[-1][0] if _COHORT_HISTORY else None
    except Exception:  # noqa: BLE001 - 拿不到就当无边界，但要在输出里说清楚
        return None


def load_samples(db_path: str = DB_PATH, all_cohorts: bool = False,
                 with_inputs: bool = False) -> Dict:
    """载入干净样本。返回 {'rows': [...], 'cohort_start': str|None, 'notes': [...]}"""
    notes: List[str] = []
    cohort = None if all_cohorts else latest_cohort_start()
    if all_cohorts:
        notes.append("⚠️ 跨世代混算：口径不可比，结果只能做相对比较，不能当绝对水平")
    elif cohort:
        notes.append(f"仅用最新世代样本（自 {cohort} 起）")
    else:
        notes.append("⚠️ 取不到世代边界，按全历史处理")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql = ("SELECT date, ticker, final_score, dimension_scores, "
               "price_at_predict, close_t7, dir_ambiguous_t7 "
               "FROM predictions WHERE close_t7 IS NOT NULL AND price_at_predict > 0")
        params: List = []
        if cohort:
            sql += " AND date >= ?"
            params.append(cohort)
        raw = con.execute(sql, params).fetchall()
    finally:
        con.close()

    rows = []
    dropped_amb = dropped_nodim = 0
    for d, t, fs, ds, p0, c7, amb in raw:
        if amb:
            dropped_amb += 1
            continue
        try:
            dim = json.loads(ds) if ds else {}
        except (TypeError, ValueError):
            dim = {}
        if not dim:
            dropped_nodim += 1
            continue
        rows.append({"date": d, "ticker": t, "final_score": fs, "dims": dim,
                     "fwd_return_pct": (c7 / p0 - 1) * 100})
    if dropped_amb:
        notes.append(f"剔除 dir_ambiguous_t7 模糊样本 {dropped_amb} 条")
    if dropped_nodim:
        notes.append(f"剔除无 dimension_scores 的 {dropped_nodim} 条")

    if with_inputs and rows:
        _attach_inputs(rows, db_path)
        notes.append("已附加 signal_archive 维度输入（供维度计算层重放）")

    return {"rows": rows, "cohort_start": cohort, "notes": notes}


def _attach_inputs(rows: List[Dict], db_path: str) -> None:
    """把 signal_archive 的维度输入挂到样本上，键为 inputs。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.execute("SELECT date, ticker, signal, value FROM signal_archive")
        idx: Dict = {}
        for d, t, sig, val in cur:
            idx.setdefault((d, t), {})[sig] = val
    except sqlite3.OperationalError:
        idx = {}
    finally:
        con.close()
    for r in rows:
        r["inputs"] = idx.get((r["date"], r["ticker"]), {})


# ══════════════════════════════════════════════════════════════════════════
# 评估
# ══════════════════════════════════════════════════════════════════════════

def evaluate(name: str, score_fn: Callable[[Dict], Optional[float]],
             rows: List[Dict]) -> Dict:
    """对一个打分方案求 rank-IC。score_fn(row) 返回 None 表示该样本弃权。"""
    xs, ys, dates = [], [], []
    for r in rows:
        v = score_fn(r)
        if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        xs.append(float(v))
        ys.append(r["fwd_return_pct"])
        dates.append(r["date"])
    ic = rank_ic(xs, ys)
    weeks = _iso_weeks(dates) if dates else 0
    return {"name": name, "ic": ic, "n": len(xs), "weeks": weeks,
            "coverage_pct": round(100 * len(xs) / len(rows), 1) if rows else 0.0}


def required_weeks(target_ic: float = 0.090) -> int:
    try:
        from ic_rerun_readiness import _WEEKS_REQUIRED
        return _WEEKS_REQUIRED.get(target_ic, 25)
    except Exception:  # noqa: BLE001
        return 25


# ══════════════════════════════════════════════════════════════════════════
# 内置情景
# ══════════════════════════════════════════════════════════════════════════

def _weights() -> Dict[str, float]:
    try:
        from config import EVALUATION_WEIGHTS
        return dict(EVALUATION_WEIGHTS)
    except Exception:  # noqa: BLE001
        return {d: 0.2 for d in DIMS}


def _weighted(w: Dict[str, float]) -> Callable:
    def _f(r: Dict) -> Optional[float]:
        dims = r["dims"]
        num = den = 0.0
        for k, wt in w.items():
            v = dims.get(k)
            if isinstance(v, (int, float)):
                num += wt * v
                den += wt
        return num / den if den > 0 else None
    return _f


def builtin_scenarios() -> List:
    w = _weights()
    out = [("现行权重（config）", _weighted(w)),
           ("等权五维", _weighted({d: 0.2 for d in DIMS})),
           ("落库的 final_score", lambda r: r.get("final_score"))]
    for d in DIMS:
        out.append((f"单维：{d}", (lambda k: lambda r: r["dims"].get(k))(d)))
    for d in DIMS:
        rest = {k: v for k, v in w.items() if k != d}
        out.append((f"剔除 {d}（其余重归一化）", _weighted(rest)))
    return out


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="评分重放：离线评估聚合层改动")
    ap.add_argument("--all-cohorts", action="store_true",
                    help="放宽到全历史（跨世代，仅可相对比较）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--target-ic", type=float, default=0.090)
    args = ap.parse_args()

    data = load_samples(all_cohorts=args.all_cohorts)
    rows = data["rows"]
    need = required_weeks(args.target_ic)

    if not rows:
        print("❌ 无可用样本（世代内还没有已验证的 T+7 结果）")
        print("   这是正常状态 —— 见 ic_rerun_readiness.py 的进度")
        return 3

    results = [evaluate(n, f, rows) for n, f in builtin_scenarios()]
    weeks = max((r["weeks"] for r in results), default=0)
    powered = weeks >= need

    if args.json:
        print(json.dumps({"results": results, "weeks": weeks,
                          "weeks_required": need, "powered": powered,
                          "cohort_start": data["cohort_start"],
                          "notes": data["notes"]}, ensure_ascii=False, indent=1))
        return 0 if powered else 1

    bar = "━" * 72
    print(bar)
    print("🐝 Alpha Hive · 评分重放")
    print(bar)
    for n in data["notes"]:
        print(f"  {n}")
    print(f"  样本 {len(rows)} 条 · 不重叠 ISO 周 {weeks}")
    print()
    if not powered:
        print(f"  ⛔ **功效不足：{weeks}/{need} 个不重叠周**（检出 |IC|={args.target_ic} 所需）")
        print("     下表**不足以支持任何改动决定**，只能用来排除明显更差的方案。")
        print("     naive n 会高估数倍：同日多标的 + 每日重叠的 T+7 窗口并非独立。")
    else:
        print(f"  ✅ 不重叠周 {weeks} ≥ {need}，达到检出 |IC|={args.target_ic} 的功效")
    print()
    print(f"  {'情景':<26} {'rank-IC':>9}  {'n':>5} {'周':>4} {'覆盖':>7}")
    print(f"  {'-'*26} {'-'*9}  {'-'*5} {'-'*4} {'-'*7}")
    for r in sorted(results, key=lambda x: -(x["ic"] or -9)):
        ic = f"{r['ic']:+.4f}" if r["ic"] is not None else "   n/a"
        print(f"  {r['name']:<26} {ic:>9}  {r['n']:>5} {r['weeks']:>4} {r['coverage_pct']:>6.1f}%")
    print()
    print("  ⚠️ 本表不产出「最优权重」建议：权重自 v0.44.0 只读，且实测单维 IC")
    print("     均不过 Bonferroni（见 experiments/final_score_dilution_report.md）。")
    print(bar)
    return 0 if powered else 1


if __name__ == "__main__":
    sys.exit(main())
