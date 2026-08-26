#!/usr/bin/env python3
"""
🐝 Alpha Hive — 扫描连续性体检 (v0.44.0)
==========================================
回答一个问题：**过去 N 天里，每个交易日都产出扫描了吗？漏了哪几天？**

为什么这是第一优先级的运维指标
------------------------------
`experiments/ic_power_analysis.py` 实测：扩池 10→30 只把出结论所需的日历时间
缩短 **5.18 倍**（|IC|=0.090 从 ~2.4 年降到 ~0.5 年）。但那个增益的计价单位是
**有扫描的 ISO 周数** —— T+7 的不重叠取样单位就是周，一周漏掉就是少一个观测。

换句话说：**扫描连续性是唯一能把扩池增益兑现的东西，也是唯一能把它按比例吃掉的
东西。** 实测历史里 2026-04/05 覆盖率约 95%，2026-06/07 掉到约 40%，中间还有
07-10→07-21（**8 个交易日**）与 07-30→08-07（**7 个交易日**）两个空档。
（单位是交易日，与本工具的输出口径一致。原文写「13 天 / 11 天」且起止日也不对，
v0.45.25 按本工具 `--since 2026-07-01` 的实测输出更正。）

为什么不复用现有的"扫描失败通知"
--------------------------------
CLAUDE.md 明确禁止 Bot 发送扫描失败通知（减噪音，规则本身合理）。但那条规则把
唯一能发现"连续性断了"的信号也一起关掉了 —— 单次失败是噪音，**连续性是聚合信号**。
本工具只输出聚合判断（"过去 5 个交易日只跑了 2 次"），不报单次失败，
因此与那条规则不冲突。

⚠️ 本模块**不发送任何通知**。它只做判定并给出退出码，通知与否由调用方决定
（`--slack` 目前是显式未实现的占位，见 main()）。这是刻意的：对外动作要先确认。

数据源
------
`pheromone.db.predictions` 的 `DISTINCT date` = 扫描确实跑过并写库的业务日。
选它而不是数 `report_snapshots/` 文件，是因为快照数量受 `ALPHA_HIVE_ML_REPORT_MAX`
等限制影响，而写库是扫描主流程的必经步骤。两者都查，不一致时报出来
——不一致本身就是一类静默降级的信号。

用法
----
    /usr/local/bin/python3 scan_continuity.py                 # 近 30 个交易日
    /usr/local/bin/python3 scan_continuity.py --days 60
    /usr/local/bin/python3 scan_continuity.py --json
    /usr/local/bin/python3 scan_continuity.py --since 2026-06-01

退出码
------
    0 = 健康（覆盖率 ≥ --min-coverage 且最长空档 ≤ --max-gap）
    1 = 降级（编排器可据此决定是否补跑）
    3 = 无法判定（找不到库等）

⚠️ 「无法判定」刻意用 3 而非更自然的 2：编排器 `alpha-hive-orchestrator.sh`
   的 `run_step()` 把 **2 保留给「脚本不存在」**（见其 `return 2  # 返回 2 = 跳过`）。
   若这里也用 2，编排器就无法区分"检查器没装"和"检查器跑了但判不了"——
   又是一个"看着跳过了其实是另一回事"的静默降级。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ALPHAHIVE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ALPHAHIVE_DIR))

from is_trading_day import is_trading_day  # noqa: E402

DB_PATH = ALPHAHIVE_DIR / "pheromone.db"
SNAPSHOTS_DIR = ALPHAHIVE_DIR / "report_snapshots"

# 默认门槛。覆盖率 0.80 = 5 个交易日至少 4 天有扫描；空档 3 = 连续 3 个交易日
# 无扫描即降级（对 T+7 周度取样而言，连续 3 天已经威胁到当周的观测）。
DEFAULT_MIN_COVERAGE = 0.80
DEFAULT_MAX_GAP = 3
DEFAULT_DAYS = 30


# ────────────────────────────────────────────────────────────────────────────
# 交易日与实际扫描日
# ────────────────────────────────────────────────────────────────────────────

def trading_days_between(start: dt.date, end: dt.date) -> List[dt.date]:
    """[start, end] 闭区间内的所有美股交易日（含假日剔除）。"""
    out: List[dt.date] = []
    d = start
    while d <= end:
        ok, _ = is_trading_day(d)
        if ok:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def recent_trading_days(n: int, end: Optional[dt.date] = None) -> List[dt.date]:
    """截止 end（含）往前数 n 个交易日。"""
    if end is None:
        end = dt.date.today()
    out: List[dt.date] = []
    d = end
    # 上限防御：n 个交易日最多跨约 2n 个自然日，留 3 倍余量兜住长假
    for _ in range(max(10, n * 3)):
        if len(out) >= n:
            break
        ok, _ = is_trading_day(d)
        if ok:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def scanned_days_from_db(db_path: Path) -> Tuple[Set[str], Dict[str, int]]:
    """从 predictions 取 (有扫描的业务日集合, {业务日: 标的数})。"""
    if not db_path.exists():
        return set(), {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT date, COUNT(DISTINCT ticker) FROM predictions GROUP BY date"
        ).fetchall()
    finally:
        con.close()
    counts = {str(d): int(c) for d, c in rows if d}
    return set(counts), counts


def scanned_days_from_snapshots(snap_dir: Path) -> Dict[str, int]:
    """从 report_snapshots/ 文件名取 {业务日: 快照数}，用于交叉核对。

    文件名形如 `TICKER_YYYY-MM-DD.json`。
    """
    out: Dict[str, int] = {}
    if not snap_dir.is_dir():
        return out
    for f in snap_dir.glob("*_*.json"):
        stem = f.stem
        _, _, datepart = stem.rpartition("_")
        if len(datepart) == 10 and datepart[4] == "-" and datepart[7] == "-":
            out[datepart] = out.get(datepart, 0) + 1
    return out


# ────────────────────────────────────────────────────────────────────────────
# 空档与周覆盖
# ────────────────────────────────────────────────────────────────────────────

def find_gaps(expected: List[dt.date], scanned: Set[str]) -> List[Dict]:
    """连续缺失的交易日区段。返回 [{start, end, n_days}, ...]。"""
    gaps: List[Dict] = []
    run: List[dt.date] = []
    for d in expected:
        if d.isoformat() in scanned:
            if run:
                gaps.append({"start": run[0].isoformat(),
                             "end": run[-1].isoformat(), "n_days": len(run)})
                run = []
        else:
            run.append(d)
    if run:
        gaps.append({"start": run[0].isoformat(),
                     "end": run[-1].isoformat(), "n_days": len(run)})
    return gaps


def week_coverage(expected: List[dt.date], scanned: Set[str]) -> Dict:
    """ISO 周覆盖 —— 这是功效计算里的硬通货单位。

    一个 ISO 周只要有 ≥1 个交易日跑过扫描，就贡献一个不重叠 T+7 观测。
    """
    weeks_expected: Dict[Tuple[int, int], bool] = {}
    for d in expected:
        key = d.isocalendar()[:2]
        weeks_expected.setdefault(key, False)
        if d.isoformat() in scanned:
            weeks_expected[key] = True
    total = len(weeks_expected)
    covered = sum(1 for v in weeks_expected.values() if v)
    missed = sorted(f"{y}-W{w:02d}" for (y, w), v in weeks_expected.items() if not v)
    return {
        "weeks_total": total,
        "weeks_covered": covered,
        "weeks_missed": missed,
        "week_coverage": (covered / total) if total else float("nan"),
    }


def assess(db_path: Path = DB_PATH, snap_dir: Path = SNAPSHOTS_DIR,
           days: int = DEFAULT_DAYS, since: Optional[str] = None,
           end: Optional[str] = None,
           min_coverage: float = DEFAULT_MIN_COVERAGE,
           max_gap: int = DEFAULT_MAX_GAP) -> Dict:
    """连续性判定。纯函数，便于测试。"""
    end_date = dt.date.fromisoformat(end) if end else dt.date.today()
    if since:
        expected = trading_days_between(dt.date.fromisoformat(since), end_date)
    else:
        expected = recent_trading_days(days, end_date)

    scanned_set, db_counts = scanned_days_from_db(db_path)
    snap_counts = scanned_days_from_snapshots(snap_dir)

    exp_iso = [d.isoformat() for d in expected]
    hit = [d for d in exp_iso if d in scanned_set]
    coverage = (len(hit) / len(exp_iso)) if exp_iso else float("nan")
    gaps = find_gaps(expected, scanned_set)
    longest_gap = max((g["n_days"] for g in gaps), default=0)
    wk = week_coverage(expected, scanned_set)

    # 库与快照的不一致：有库无快照 / 有快照无库，两个方向都是静默降级的信号
    db_only = sorted(d for d in exp_iso if d in scanned_set and d not in snap_counts)
    snap_only = sorted(d for d in exp_iso if d in snap_counts and d not in scanned_set)

    healthy = (
        len(exp_iso) > 0
        and coverage >= min_coverage
        and longest_gap <= max_gap
    )

    return {
        "window": {"start": exp_iso[0] if exp_iso else None,
                   "end": exp_iso[-1] if exp_iso else None,
                   "trading_days": len(exp_iso)},
        "scanned_days": len(hit),
        "coverage": coverage,
        "gaps": gaps,
        "longest_gap": longest_gap,
        "missing_days": [d for d in exp_iso if d not in scanned_set],
        **wk,
        "thresholds": {"min_coverage": min_coverage, "max_gap": max_gap},
        "healthy": healthy,
        "consistency": {"db_only": db_only, "snapshot_only": snap_only},
        "per_day_tickers": {d: db_counts.get(d, 0) for d in hit},
    }


def alert_line(res: Dict) -> Optional[str]:
    """聚合告警文案。健康时返回 None（静默）。

    刻意只讲聚合事实，不提单次失败 —— 与 CLAUDE.md 的 Slack 静音规则相容。
    """
    if res["healthy"]:
        return None
    w = res["window"]
    parts = [
        f"⚠️ 扫描连续性降级：过去 {w['trading_days']} 个交易日只跑了 "
        f"{res['scanned_days']} 次（覆盖率 {res['coverage']:.0%}，"
        f"门槛 {res['thresholds']['min_coverage']:.0%}）"
    ]
    if res["longest_gap"] > res["thresholds"]["max_gap"]:
        g = max(res["gaps"], key=lambda x: x["n_days"])
        parts.append(f"最长空档 {res['longest_gap']} 个交易日"
                     f"（{g['start']} → {g['end']}）")
    if res["weeks_missed"]:
        parts.append(f"完全无扫描的周: {', '.join(res['weeks_missed'])}"
                     f"（每周漏一次 = 少一个不重叠 T+7 观测）")
    return "；".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描连续性体检")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--snapshots", default=str(SNAPSHOTS_DIR))
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"回看多少个交易日（默认 {DEFAULT_DAYS}）")
    ap.add_argument("--since", help="改为从该日期起算（YYYY-MM-DD）")
    ap.add_argument("--end", help="窗口终点（默认今天）")
    ap.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)
    ap.add_argument("--max-gap", type=int, default=DEFAULT_MAX_GAP)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help=(
        "把 JSON 结果写到该文件（供编排器读）。"
        "刻意提供此参数而不让调用方重定向 stdout："
        "编排器的 log() 用 `tee -a` **会写 stdout**，"
        "`> file` 捕获会在脚本缺失/TCC 拒绝等路径上把日志行混进 JSON。"
    ))
    ap.add_argument("--quiet", action="store_true",
                    help="只在降级时输出（适合放进编排器）")
    ap.add_argument("--slack", action="store_true",
                    help="（未实现）推送聚合告警。对外动作需先确认，故留占位")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ 找不到 {db_path} —— 无法判定连续性", file=sys.stderr)
        return 3  # 3 而非 2：编排器把 2 保留给"脚本不存在"，见模块 docstring

    res = assess(
        db_path=db_path, snap_dir=Path(args.snapshots),
        days=args.days, since=args.since, end=args.end,
        min_coverage=args.min_coverage, max_gap=args.max_gap,
    )

    if args.out:
        try:
            Path(args.out).write_text(
                json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            # 写不出去不改变判定结果 —— 判定本身已经完成
            print(f"⚠️  无法写入 {args.out}: {e}", file=sys.stderr)

    if args.slack:
        # 刻意不实现：发消息是对外动作，需要用户在对话里明确同意后再接线。
        print("ℹ️  --slack 尚未接线（对外动作需先确认）。"
              "本次仅本地判定。", file=sys.stderr)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["healthy"] else 1

    line = alert_line(res)
    if args.quiet:
        if line:
            print(line)
        return 0 if res["healthy"] else 1

    w = res["window"]
    print("━" * 68)
    print("🐝 Alpha Hive · 扫描连续性体检")
    print("━" * 68)
    print(f"  窗口: {w['start']} → {w['end']}  ({w['trading_days']} 个交易日)")
    print(f"  有扫描: {res['scanned_days']} 天   覆盖率: {res['coverage']:.1%}"
          f"   (门槛 {res['thresholds']['min_coverage']:.0%})")
    print(f"  ISO 周覆盖: {res['weeks_covered']}/{res['weeks_total']}"
          f"  = {res['week_coverage']:.1%}"
          f"   ← 不重叠 T+7 观测的实际产出")
    print(f"  最长空档: {res['longest_gap']} 个交易日"
          f"   (门槛 ≤{res['thresholds']['max_gap']})")
    print()

    if res["gaps"]:
        print("  空档明细:")
        for g in res["gaps"]:
            flag = " ⚠" if g["n_days"] > res["thresholds"]["max_gap"] else ""
            if g["n_days"] == 1:
                print(f"    {g['start']}                 1 个交易日{flag}")
            else:
                print(f"    {g['start']} → {g['end']}   "
                      f"{g['n_days']} 个交易日{flag}")
        print()

    if res["weeks_missed"]:
        print(f"  完全无扫描的 ISO 周: {', '.join(res['weeks_missed'])}")
        print()

    cons = res["consistency"]
    if cons["db_only"] or cons["snapshot_only"]:
        print("  ⚠️ 库与快照不一致（可能是静默降级）:")
        if cons["db_only"]:
            print(f"    写了库但无快照: {', '.join(cons['db_only'])}")
        if cons["snapshot_only"]:
            print(f"    有快照但未写库: {', '.join(cons['snapshot_only'])}")
        print()

    print("━" * 68)
    if res["healthy"]:
        print("✅ 连续性健康")
    else:
        print(alert_line(res))
    print("━" * 68)
    return 0 if res["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
