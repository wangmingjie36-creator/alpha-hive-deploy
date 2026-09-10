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

# v0.45.160：`DB_PATH` 现在是**覆盖钩子**，默认 `None` ⇒ 运行时解析 `PATHS.db`。
# 原本是 `os.path.join(os.path.dirname(os.path.abspath(__file__)), "pheromone.db")` —— 那个写法**压根不读任何环境变量**
# （`ALPHA_HIVE_DB_PATH` / `ALPHA_HIVE_HOME` 设成什么都无效，连懒求值都救不了），
# 比「模块级常量冻在 import 期」更彻底，`tests/conftest.py::_isolate_env` 对它完全无效。
# 保留这个名字是因为 `tests/` 有 `monkeypatch.setattr(<mod>, "DB_PATH", ...)` 依赖它。
# ⚠️ 本模块的**默认参数**早在 v0.45.37 就改成 `None` 了（`load_samples` 的
#   `db_path=DB_PATH` 曾绑死默认值，让退化测试变成假守卫）；本版补的是常量自身。
DB_PATH = None


def _db_path() -> str:
    """本模块的库路径。**调用时求值。**

    返回 `str` 而非 `Path`——本模块通篇用 `os.path`，跟着它的惯例走。

    ⚠️ 默认参数与 argparse 的 `default` 一律写 `None`，不要塞这个值——
    两者都在 import 期求值，等于换个地方冻同一个值（同型 v0.45.37 / v0.45.150）。
    """
    if DB_PATH is not None:
        return os.fspath(DB_PATH)
    from hive_logger import PATHS
    return str(PATHS.db)
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


def load_samples(db_path: Optional[str] = None, all_cohorts: bool = False,
                 with_inputs: bool = False) -> Dict:
    """载入干净样本。返回 {'rows': [...], 'cohort_start': str|None, 'notes': [...]}

    ⚠️ `db_path` 默认 **None → 运行时解析 DB_PATH**，不是 `db_path=DB_PATH`。
    后者在 import 时就把值绑死了，`monkeypatch.setattr(rs, "DB_PATH", ...)`
    改不动它——`main()` 会绕开夹具去读真库，测试于是变成了**假守卫**
    （v0.45.37 实测：功效护栏的退化测试自诞生起从未真正生效，
    它在主 checkout 变绿只是因为真库样本量恰好落在「功效不足」区间）。
    """
    db_path = db_path or _db_path()      # v0.45.160：常量本身也不再冻结
    notes: List[str] = []
    cohort = None if all_cohorts else latest_cohort_start()
    if all_cohorts:
        notes.append("⚠️ 跨世代混算：口径不可比，结果只能做相对比较，不能当绝对水平")
    elif cohort:
        notes.append(f"仅用最新世代样本（自 {cohort} 起）")
    else:
        notes.append("⚠️ 取不到世代边界，按全历史处理")

    # 库读不到 → 空样本 + 显式 note，交由 main() 走「无法判定」(3)。
    # 不能让 sqlite3 异常裸奔：抛栈会以退出码 1 结束，而 1 的语义是
    # 「功效不足，正常继续攒」—— 库丢了会被读成一切正常（v0.45.37）。
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        notes.append(f"⛔ 样本库不可读（{db_path}）：{type(e).__name__}: {e}")
        return {"rows": [], "cohort_start": cohort, "notes": notes}
    try:
        sql = ("SELECT date, ticker, final_score, dimension_scores, "
               "price_at_predict, close_t7, dir_ambiguous_t7 "
               "FROM predictions WHERE close_t7 IS NOT NULL AND price_at_predict > 0")
        params: List = []
        if cohort:
            sql += " AND date >= ?"
            params.append(cohort)
        raw = con.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        notes.append(f"⛔ 样本表不可查（{db_path}）：{type(e).__name__}: {e}")
        return {"rows": [], "cohort_start": cohort, "notes": notes}
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

# 单日横截面最少标的数，与 ic_diagnostics.load_daily_ic / final_score_dilution 同口径
MIN_WIDTH = 5


def evaluate(name: str, score_fn: Callable[[Dict], Optional[float]],
             rows: List[Dict]) -> Dict:
    """对一个打分方案求**横截面** rank-IC。score_fn(row) 返回 None 表示该样本弃权。

    ⚠️ v0.45.176 修：初版把**所有日期的所有行摊平成一个大 Spearman**
    （`xs.append(...)` 跨日期累积 → `rank_ic(xs, ys)` 一次性池化），ISO 周只用来
    数周数打功效警告。那是**池化 IC**，与本项目其余各处（`ic_diagnostics`、
    `experiments/final_score_dilution.py`）用的横截面 IC 不是一个量：

    - 横截面 IC 问的是「**同一天该挑哪只票**」——这才是评分的用途。
    - 池化 IC 里收益的时序跨度（哪周大盘涨跌）远大于横截面跨度，
      于是它主要在测「哪天分高哪天涨」。

    实测后果（同一份 pheromone.db、同一天跑出的两套数）：

    | 维度 | 池化（旧） | 横截面（新） | 08-25 报告 |
    |---|---|---|---|
    | sentiment | +0.046 | **+0.120** | +0.168 |
    | risk_adj | **+0.047** | **−0.060** | −0.084 |
    | catalyst | +0.075 | +0.097 | +0.001 |
    | signal | −0.069 | −0.078 | −0.088 |

    **risk_adj 符号是反的**，而它的负 IC 正是 v0.45.172 归零它的依据之一 ——
    拿旧口径的本工具复核那个决策会得出相反结论。而 CLAUDE.md 指定本工具做
    聚合层决策的第一站，所以这个偏差污染的是**将来每一个**聚合层决定。

    ⚠️ 别把「`final_score` 是为跨标的可比而设计的」读成「所以能池化」——
    **可比的量不蕴含可池化的相关性**（见 MEMORY `alpha-hive-cross-sectional-pooling`）。

    保留 `ic_pooled` 字段并在两者符号相反时显式告警，是为了让这个坑
    对下一个读代码的人可见，而不是悄悄换掉了事。

    Returns:
        {ic: 周度横截面 IC 均值, t, p, weeks: 不重叠周数, n, coverage_pct,
         ic_pooled: 旧口径值（仅作对照）, sign_conflict: bool}
    """
    from ic_diagnostics import basic_stats, normal_two_sided_p, spearman, subsample_non_overlapping

    by_day: Dict[str, List] = {}
    kept = 0
    for r in rows:
        v = score_fn(r)
        if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        kept += 1
        by_day.setdefault(r["date"], []).append((float(v), r["fwd_return_pct"]))

    daily: Dict[str, float] = {}
    for day, pairs in by_day.items():
        if len(pairs) < MIN_WIDTH:
            continue
        xs = [p[0] for p in pairs]
        if len({round(x, 9) for x in xs}) < 2:   # 全并列 → 无排序信息
            continue
        ic = spearman(xs, [p[1] for p in pairs])
        if ic is not None:
            daily[day] = ic

    weekly = subsample_non_overlapping(daily, "周") if daily else []
    if len(weekly) >= 2:
        m, _se, t, _n = basic_stats(weekly)
        ic_val, t_val = m, t
        p_val = normal_two_sided_p(t)
    else:
        ic_val = t_val = p_val = None

    # 旧口径，仅作对照 —— 不参与排序、不用于决策
    pooled = rank_ic([p[0] for d in by_day.values() for p in d],
                     [p[1] for d in by_day.values() for p in d])
    conflict = (ic_val is not None and pooled is not None
                and ic_val * pooled < 0 and abs(ic_val) > 0.02 and abs(pooled) > 0.02)

    return {"name": name, "ic": ic_val, "t": t_val, "p": p_val,
            "n": kept, "weeks": len(weekly),
            "coverage_pct": round(100 * kept / len(rows), 1) if rows else 0.0,
            "ic_pooled": pooled, "sign_conflict": conflict}


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

    data = load_samples(DB_PATH, all_cohorts=args.all_cohorts)
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
    print("  口径：日度横截面 rank-IC → 每 ISO 周取第一天（近似不重叠）→ 周序列 t 检验")
    print(f"  {'情景':<26} {'rank-IC':>9} {'t':>6} {'p':>6}  {'n':>5} {'周':>4} {'覆盖':>7}")
    print(f"  {'-'*26} {'-'*9} {'-'*6} {'-'*6}  {'-'*5} {'-'*4} {'-'*7}")
    for r in sorted(results, key=lambda x: -(x["ic"] if x["ic"] is not None else -9)):
        ic = f"{r['ic']:+.4f}" if r["ic"] is not None else "   n/a"
        tt = f"{r['t']:+.2f}" if r.get("t") is not None else "   n/a"
        pp = f"{r['p']:.3f}" if r.get("p") is not None else "  n/a"
        flag = " ⚠️符号冲突" if r.get("sign_conflict") else ""
        print(f"  {r['name']:<26} {ic:>9} {tt:>6} {pp:>6}  {r['n']:>5} {r['weeks']:>4} "
              f"{r['coverage_pct']:>6.1f}%{flag}")

    conflicts = [r for r in results if r.get("sign_conflict")]
    if conflicts:
        print()
        print("  ⚠️ **符号冲突**：以下情景的横截面 IC 与旧的池化口径符号相反。")
        print("     池化 IC（跨日期摊平成一个大 Spearman）测的是「哪天分高哪天涨」，")
        print("     不是「同一天该挑哪只票」。**本表用的是横截面口径，池化值仅供对照。**")
        for r in conflicts:
            print(f"       {r['name']:<26} 横截面 {r['ic']:+.4f}  vs  池化 {r['ic_pooled']:+.4f}")
    print()
    print("  ⚠️ 本表不产出「最优权重」建议：权重自 v0.44.0 只读，且实测单维 IC")
    print("     均不过 Bonferroni（见 experiments/final_score_dilution_report.md）。")
    print(bar)
    return 0 if powered else 1


if __name__ == "__main__":
    sys.exit(main())
