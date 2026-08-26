#!/usr/bin/env python3
"""
🐝 Alpha Hive — IC 重跑就绪度 (v0.44.4)
========================================
回答一个问题：**v0.44.1~0.44.3 那批修复之后，攒够样本可以重跑 IC 了吗？**

为什么需要它
------------
v0.44.1~0.44.3 修了 ML 预期收益的结构性看多偏斜、并把 RivalBee 的三个硬编码特征
接上真实数据。**接线正确性已验证（单测 + 隔离环境 E2E），但方向是否变准没有验证**
—— 那需要新样本。

问题是"等攒够"这件事**没有承载物**：它不在任何测试里（测试跑的是当下），
不在任何告警里（没有异常），全靠人记着。而按 `experiments/ic_power_report.md`
的实测，攒够要 **~25 个不重叠周**（30 只标的、|IC|=0.090、80% 功效），
折算日历时间约半年 —— **半年后没人会记得这件事。**

本工具就是那个承载物。挂在每周的只读诊断任务上（见
`~/.claude/scheduled-tasks/alpha-hive-weekly-optimizer/SKILL.md`），
每周报一次"还差多少"，够了就明说该跑什么。

为什么不用定时任务/提醒
----------------------
"到期提醒"要求事先知道日期，而这里的到期条件是**数据条件**（攒够不重叠周），
它取决于扫描连续性 —— 而扫描覆盖率实测只有 36.7%，日历时间和样本进度根本不成比例。
所以判据必须读库算，不能拍一个日期。

⚠️ 世代边界（`_COHORT_HISTORY`）是本工具的核心前提：
**任何再次改动 `expected_returns` / `predict_probability` / RivalBee 特征来源的
改动，都必须往 `_COHORT_HISTORY` 追加一条**，否则新旧口径样本会被混算，
而这种混算是静默的 —— 数字照出，只是没有意义。

用法
----
    /usr/local/bin/python3 ic_rerun_readiness.py
    /usr/local/bin/python3 ic_rerun_readiness.py --json
    /usr/local/bin/python3 ic_rerun_readiness.py --target-ic 0.135   # 只想检出更强的信号

退出码
------
    0 = 已就绪（该重跑 IC 了）
    1 = 未就绪（正常状态，继续攒）
    3 = 无法判定（找不到库等）
        ⚠️ 3 而非 2：编排器 `run_step()` 把 2 保留给"脚本不存在"。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Optional, Set

ALPHAHIVE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ALPHAHIVE_DIR))

DB_PATH = ALPHAHIVE_DIR / "pheromone.db"

# ── 样本世代边界 ────────────────────────────────────────────────────────────
# 每条 = (首个受影响的业务日, 版本, 改了什么)。**只追加，不改写**（审计轨迹）。
# 判据取最后一条：它之前的样本与现在的口径不可比。
#
# ⚠️ 再次改动 expected_returns / predict_probability / RivalBee 特征来源时
# **必须追加一条**。漏了会让新旧口径样本被混算，而混算是静默的。
_COHORT_HISTORY = [
    ("2026-08-17", "v0.44.1~0.44.3",
     "expected_returns 去偏 + probability 居中 + RivalBee 三特征接真实数据"),
    ("2026-08-26", "v0.45.30",
     "拥挤度口径变更：删除 polymarket_volatility（原 15% 权重，实测 76% 为常数 20、"
     "其余变化来自 |momentum_5d|*0.8 的动量伪装，属常数稀释+暗中双计），"
     "其余五项按原比例重归一化；缺失分量改为在现存分量间重归一化而非按 0 计。"
     "拥挤度 → ScoutBee signal 维度 → final_score，故为世代边界"),
]

# 达到 80% 功效所需的不重叠周数（30 只标的口径，实测见 experiments/ic_power_report.md）
# key = 真实 |IC|，value = 所需不重叠周数
_WEEKS_REQUIRED = {
    0.050: 82,
    0.077: 35,    # 噪音地板
    0.090: 25,    # 系统综合分实测 —— 默认判据
    0.135: 11,    # 20 日动量基准
    0.200: 5,
}
DEFAULT_TARGET_IC = 0.090

# 标的池漂移容忍：与世代内首批扫描相比，当前池新增标的占比超过此值即视为
# 世代被打断（与 weekly_optimizer.check_ticker_pool_consistency 同一思路）
MAX_POOL_DRIFT = 0.20


def cohort_start() -> Dict:
    """当前有效的世代边界。"""
    date, version, reason = _COHORT_HISTORY[-1]
    return {"date": date, "version": version, "reason": reason,
             "n_generations": len(_COHORT_HISTORY)}


def _iso_weeks(dates) -> Set:
    out = set()
    for d in dates:
        try:
            out.add(dt.date.fromisoformat(str(d)[:10]).isocalendar()[:2])
        except (ValueError, TypeError):
            continue
    return out


def assess(db_path: Path = DB_PATH, target_ic: float = DEFAULT_TARGET_IC,
           max_pool_drift: float = MAX_POOL_DRIFT,
           today: Optional[str] = None) -> Dict:
    """就绪度判定。纯函数，便于测试。"""
    cohort = cohort_start()
    boundary = cohort["date"]
    required = _WEEKS_REQUIRED.get(
        target_ic, _WEEKS_REQUIRED[DEFAULT_TARGET_IC])

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # 世代内**已回填 T+7** 的样本 —— 只有这些能进 IC 计算
        ripe = con.execute(
            "SELECT date, ticker FROM predictions "
            "WHERE date >= ? AND checked_t7 = 1 AND price_t7 IS NOT NULL "
            "  AND price_at_predict > 0",
            (boundary,),
        ).fetchall()
        # 世代内**全部**样本（含未到期）—— 用于看进度与池漂移
        allrows = con.execute(
            "SELECT date, ticker FROM predictions WHERE date >= ?",
            (boundary,),
        ).fetchall()
    finally:
        con.close()

    ripe_dates = [r[0] for r in ripe]
    weeks_ripe = _iso_weeks(ripe_dates)
    weeks_scanned = _iso_weeks(r[0] for r in allrows)

    # 池漂移：世代内最早 3 个扫描日的标的集合 vs 最近 3 个
    by_date: Dict[str, Set[str]] = {}
    for d, t in allrows:
        by_date.setdefault(str(d)[:10], set()).add(t)
    dates_sorted = sorted(by_date)
    pool_drift, pool_note = 0.0, None
    if len(dates_sorted) >= 2:
        early: Set[str] = set()
        for d in dates_sorted[:3]:
            early |= by_date[d]
        late: Set[str] = set()
        for d in dates_sorted[-3:]:
            late |= by_date[d]
        if late:
            pool_drift = len(late - early) / len(late)
            if pool_drift > max_pool_drift:
                pool_note = (
                    f"当前池 {len(late)} 只里有 {len(late - early)} 只"
                    f"（{pool_drift:.0%}）在世代开始时不在池中 —— "
                    f"样本世代已被标的池变动打断，需重设世代边界"
                )

    n_weeks = len(weeks_ripe)
    ready = n_weeks >= required and pool_note is None

    # ETA：按世代内的实际周产出速度外推（不是按日历）
    today_d = dt.date.fromisoformat(today) if today else dt.date.today()
    boundary_d = dt.date.fromisoformat(boundary)
    calendar_weeks = max(1, ((today_d - boundary_d).days // 7) or 1)
    # 已扫描的周 / 已过的日历周 = 有效产出率（T+7 到期滞后不算在内）
    weeks_rate = len(weeks_scanned) / calendar_weeks if calendar_weeks else 0.0
    remaining = max(0, required - n_weeks)
    if weeks_rate > 0:
        eta_cal_weeks = remaining / weeks_rate
        eta_date = (today_d + dt.timedelta(weeks=eta_cal_weeks)).isoformat()
    else:
        eta_cal_weeks, eta_date = float("inf"), None

    return {
        "cohort": cohort,
        "target_ic": target_ic,
        "weeks_required": required,
        "weeks_accrued": n_weeks,
        "weeks_remaining": remaining,
        "n_ripe_samples": len(ripe),
        "n_all_samples": len(allrows),
        "scan_weeks_in_cohort": len(weeks_scanned),
        "calendar_weeks_elapsed": calendar_weeks,
        "weeks_per_calendar_week": round(weeks_rate, 3),
        "eta_calendar_weeks": (None if eta_cal_weeks == float("inf")
                               else round(eta_cal_weeks, 1)),
        "eta_date": eta_date,
        "pool_drift": round(pool_drift, 4),
        "pool_note": pool_note,
        "ready": ready,
        "next_step": (
            "/usr/local/bin/python3 experiments/ml_expected_return_replay.py "
            "&& /usr/local/bin/python3 signal_archive.py --analyze"
        ),
    }


def summary_line(res: Dict) -> str:
    """一行摘要，供周度任务直接引用。"""
    if res["pool_note"]:
        return f"⚠️ IC 重跑就绪度：世代已被打断 —— {res['pool_note']}"
    if res["ready"]:
        return (f"✅ IC 重跑已就绪：世代内已攒 {res['weeks_accrued']}/"
                f"{res['weeks_required']} 个不重叠周（{res['n_ripe_samples']} 条已回填样本）"
                f"，该重跑了")
    eta = (f"，按当前节奏约 {res['eta_calendar_weeks']} 个日历周后到位"
           f"（≈{res['eta_date']}）" if res["eta_date"] else
           "，但世代内还没有扫描产出 —— 先看扫描连续性")
    return (f"⏳ IC 重跑未就绪：{res['weeks_accrued']}/{res['weeks_required']} "
            f"个不重叠周{eta}")


def main() -> int:
    ap = argparse.ArgumentParser(description="IC 重跑就绪度")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--target-ic", type=float, default=DEFAULT_TARGET_IC,
                    choices=sorted(_WEEKS_REQUIRED),
                    help=f"要检出的真实 |IC|（默认 {DEFAULT_TARGET_IC}=系统综合分实测）")
    ap.add_argument("--today", help="覆盖今天的日期（测试用，YYYY-MM-DD）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help=(
        "把 JSON 结果写到该文件（供编排器读）。"
        "刻意提供此参数而不让调用方重定向 stdout："
        "编排器的 log() 用 `tee -a` **会写 stdout**，"
        "`> file` 捕获会在脚本缺失/权限被拒等路径上把日志行混进 JSON。"
        "（与 scan_continuity.py 同一理由）"
    ))
    ap.add_argument("--quiet", action="store_true", help="只输出一行摘要")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"❌ 找不到 {db} —— 无法判定", file=sys.stderr)
        return 3

    res = assess(db_path=db, target_ic=args.target_ic, today=args.today)

    if args.out:
        try:
            Path(args.out).write_text(
                json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            # 写不出去不改变判定 —— 判定在写盘之前就完成了
            print(f"⚠️  无法写入 {args.out}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["ready"] else 1
    if args.quiet:
        print(summary_line(res))
        return 0 if res["ready"] else 1

    c = res["cohort"]
    print("━" * 72)
    print("🐝 Alpha Hive · IC 重跑就绪度")
    print("━" * 72)
    print(f"  样本世代: 自 {c['date']} 起（{c['version']}）")
    print(f"            {c['reason']}")
    print(f"            世代总数 {c['n_generations']}（只追加，不改写）")
    print()
    print(f"  判据: 检出 |IC|={res['target_ic']:.3f} 需 "
          f"**{res['weeks_required']} 个不重叠周**（80% 功效，30 只标的口径）")
    print("        来源 experiments/ic_power_report.md")
    print()
    print(f"  世代内已回填 T+7 样本: {res['n_ripe_samples']} 条"
          f"（世代内总样本 {res['n_all_samples']} 条，其余未到期）")
    print(f"  已攒不重叠周:          {res['weeks_accrued']} / "
          f"{res['weeks_required']}   还差 {res['weeks_remaining']}")
    print(f"  世代内有扫描的周:      {res['scan_weeks_in_cohort']}"
          f"（已过 {res['calendar_weeks_elapsed']} 个日历周，"
          f"产出率 {res['weeks_per_calendar_week']:.2f} 周/周）")
    if res["eta_date"]:
        print(f"  按当前节奏预计到位:    ≈{res['eta_date']}"
              f"（{res['eta_calendar_weeks']} 个日历周后）")
    print()
    if res["pool_note"]:
        print(f"  ⚠️ {res['pool_note']}")
        print()
    print("━" * 72)
    print(summary_line(res))
    if res["ready"]:
        print()
        print("  该跑:")
        print(f"    {res['next_step']}")
    print("━" * 72)
    return 0 if res["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
