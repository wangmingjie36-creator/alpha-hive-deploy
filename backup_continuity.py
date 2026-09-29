#!/usr/bin/env python3
"""
🐝 Alpha Hive — 数据备份连续性体检 (v0.45.284)
==================================================
回答一个问题：**过去 N 个交易日里，Step 14 数据备份是否每天都真的成功了？
漏了/卡在"未识别或陈旧"状态的是哪几天？**

为什么需要这个（对应编排器 Step 14 注释里留的"已知设计缺口"）
--------------------------------------------------------------
Step 14 刻意不动 `OVERALL_STATUS`、不发 Slack（同 Step 10/12 先例 + 项目
CLAUDE.md「Slack 通知精简规则」——单次备份失败是噪音，不该打扰用户）。
但这也意味着：如果备份连续多天真的失败或卡在"未识别/陈旧"这种边缘状态
（`backup_status.json` 缺失或不是今天写的——脚本本轮可能根本没被
`run_step` 真正调用过），除了人工去翻 `backup_status.json` 或日志，
没有任何东西会主动告诉任何人——与 2026-09-09~11 三次 DB 备份被 TCC 拒、
无人发现的那次事故同构。

本工具照抄 `scan_continuity.py` 判 Step 10 连续性的模式：只输出聚合判断
（"过去 5 个交易日只成功了 2 次"），不报单次失败，因此与 Slack 精简规则
不冲突；判定与告警分离，通知与否留给调用方决定（`--slack` 同样是显式
未接线的占位）。

为什么不能直接复用 `scan_continuity.py` 的数据源
--------------------------------------------------
`scan_continuity.py` 读 `pheromone.db.predictions`——业务表本身天然按业务日
累积历史。备份没有这样的表：`backup_status.json`（`data_backup.run_backup.
run()` 的 `status_file` 参数）每次调用**整份覆盖**，只反映"最近一次"。
因此 `run()` 现在（v0.45.284）在每条退出路径上都额外追加一行到
`history_file`（JSONL），同 `weekly_optimizer.py` 的 `weight_history.jsonl`
审计日志同一模式——本工具读的正是这份跨天累积的记录。

判定单位不是"scanned"而是"当天最终 ok"：某天只要有一次调用最终 `ok: true`
（哪怕先失败、同一天人工重跑成功），就算当天健康——与 `status.json` 本身
"只反映最近一次"的语义一致，不额外发明"当天第一次调用才算数"之类的新规则。

交易日枚举、空档切分、ISO 周覆盖三个纯函数直接复用 `scan_continuity.py`——
两者都是"过去 N 个交易日是否每天都发生了某件事"的同一形状，逻辑没有理由
抄两份（项目 CLAUDE.md「先用装好的工具，再手搓」）。

上线日下限（v0.45.307 修，二次检查发现）：`assess()` 默认窗口（不传 `--since`
时）起点取 max(days 个交易日前, 历史日志里最早一条记录的日期)——本工具
09-18 才首次接入生产，此前压根没有这套备份系统。不设这个下限，默认 30 个
交易日的窗口会在上线后约一个月里，天天把"系统还不存在"的那些日子也算进
覆盖率分母，警报疲劳且掩盖真正的降级（09-18 首跑当天的真实日志就报过
"过去 30 个交易日只成功了 1 次（覆盖率 3%）"）。显式传 `--since` 时不做
这个收紧——那是调用方主动选择的窗口起点。

用法
----
    /usr/local/bin/python3 backup_continuity.py                 # 近 30 个交易日
    /usr/local/bin/python3 backup_continuity.py --days 10
    /usr/local/bin/python3 backup_continuity.py --json
    /usr/local/bin/python3 backup_continuity.py --since 2026-09-01

退出码
------
    0 = 健康（覆盖率 ≥ --min-coverage 且最长空档 ≤ --max-gap）
    1 = 降级（编排器可据此决定是否需要人工介入）
    3 = 无法判定（历史日志文件不存在——比如刚接入生产、一天都还没跑过）

⚠️ 「无法判定」刻意用 3 而非更自然的 2：同 `scan_continuity.py` 的理由——
   编排器 `run_step()` 把 **2 保留给「脚本不存在」**。若这里也用 2，编排器
   就无法区分"检查器没装"和"检查器跑了但判不了"。

输出契约（v0.45.355，`step_contract`）
--------------------------------------
同 `scan_continuity.py`：`--out` / `--json` 写 `step_contract.envelope(...)`，原有键原样留在
顶层（编排器 Step 15 现行内联解析照读）。`date` = `--end`（未给则 `step_contract.business_today()`
= 洛杉矶当日，窗口终点同取这一天；⚠️ 不等于编排器本机时区的 `DATE_STR`，要比新鲜度就显式传 `--end`，
见 `business_today` 的 docstring）；
0 ⇒ ok、1 ⇒ attention、3 ⇒ undetermined（历史文件缺失时**也写** `--out`）；
未捕获异常 ⇒ 退出码 3 + `status: "error"` 外壳（此前 Python 默认 1 = 「降级」）。
`step_contract` 本身导入失败 ⇒ 入口也是退出码 3（不写 `--out`）；**本模块其余的导入期失败仍是 1**
（`run_tool` 兜不到 import，见其 docstring）。
`attention` 由 `contract_attention()` 显式列出（降级判据各一条 warn，附涉及的失败阶段）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ALPHAHIVE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ALPHAHIVE_DIR))

# `step_contract` 本身导入失败 ⇒ 置哨兵，`__main__` 入口见哨兵退出码 3（同 scan_continuity 的理由：
# Python 默认的 1 = 本工具的「降级」）。⚠️ 只兜这一个 import：下面 scan_continuity 等其余导入期失败仍是 1。
try:
    import step_contract  # noqa: E402
    _STEP_CONTRACT_IMPORT_ERROR: Optional[str] = None
except Exception as _e:  # noqa: BLE001 —— 连语法错也要兜：哨兵只记下原因，入口据此退出码 3
    step_contract = None
    _STEP_CONTRACT_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
from scan_continuity import (  # noqa: E402
    find_gaps,
    recent_trading_days,
    trading_days_between,
    week_coverage,
)

_TOOL = "backup_continuity"
#: 退出码 → 外壳 status（v0.45.355）。退出码约定本身不变；崩溃的 3 由 run_tool 写 "error"。
_STATUS_BY_RC = {0: "ok", 1: "attention", 3: "undetermined"}

# 默认历史日志位置。刻意写死绝对路径（同 `data_backup/run_backup.py::main()`
# 的 `--status-file`/`--history-file` 默认值同一约定），**不走** `hive_logger.
# PATHS.home`——阶段 5（`ALPHA_HIVE_HOME=~/alpha-hive-data`）尚未执行，
# `PATHS.home` 今天兜底到代码仓库根，与 Step 14 实际写入的 `~/alpha-hive-data`
# 是两个不同目录；备份子系统从阶段 3 起就有意早于全局迁移直接指向新数据根，
# 这里跟随的是 Step 14 的既有约定，不是 `PATHS`。
HISTORY_FILE = None


def _history_file() -> Path:
    """历史 JSONL 路径，**调用时求值**（同 `scan_continuity._db_path()` 的理由：
    不要冻成模块级常量，否则测试逐条 setenv/传参也覆盖不了）。"""
    if HISTORY_FILE is not None:
        return Path(HISTORY_FILE)
    return Path.home() / "alpha-hive-data" / "logs" / "backup_status_history.jsonl"


DEFAULT_MIN_COVERAGE = 0.80
DEFAULT_MAX_GAP = 3
DEFAULT_DAYS = 30


def read_history(history_path: Path) -> Tuple[Set[str], Dict[str, str]]:
    """解析 JSONL 历史日志。

    返回 (当天最终成功的业务日集合, {业务日: 当天最后一条记录的 stage})。
    同一天可能有多条记录（人工重跑）——"成功"取任一条 `ok: true`；
    `stage` 取最后一条，供告警文案里指出"最近一次卡在哪个阶段"。
    格式错误的行（如写入过程中被中断的半行）跳过，不让整份历史因一行坏
    数据而不可读。
    """
    healthy: Set[str] = set()
    last_stage: Dict[str, str] = {}
    if not history_path.exists():
        return healthy, last_stage
    with open(history_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            date = rec.get("date")
            if not date:
                continue
            if rec.get("ok"):
                healthy.add(date)
            last_stage[date] = rec.get("stage") or "unknown"
    return healthy, last_stage


def assess(history_path: Optional[Path] = None,
           days: int = DEFAULT_DAYS, since: Optional[str] = None,
           end: Optional[str] = None,
           min_coverage: float = DEFAULT_MIN_COVERAGE,
           max_gap: int = DEFAULT_MAX_GAP) -> Dict:
    """连续性判定。纯函数，便于测试。

    ⚠️ `history_path` 默认写 `None`，调用时才解析——不要改成
    `= HISTORY_FILE`，那等于把冻结换个地方（同型 v0.45.160）。
    """
    import datetime as dt

    history_path = Path(history_path) if history_path is not None else _history_file()
    end_date = dt.date.fromisoformat(end) if end else dt.date.today()
    healthy_days, last_stage = read_history(history_path)

    if since:
        expected = trading_days_between(dt.date.fromisoformat(since), end_date)
    else:
        expected = recent_trading_days(days, end_date)
        # 上线日下限（v0.45.307 修，二次检查发现）：默认窗口起点取
        # max(days 个交易日前, 历史日志里最早一条记录的日期)——否则上线后
        # 约一个月，默认 30 个交易日的窗口会把"系统还不存在"的那些日子也
        # 算进覆盖率分母，天天误报"降级"（09-18 首跑当天的真实日志就报过
        # "过去 30 个交易日只成功了 1 次（覆盖率 3%）…完全无成功备份的周
        # W32–W37"——这些周根本没有备份系统，不是真的降级）。
        # `since` 是调用方显式指定的窗口起点，尊重这个主动选择，不做下限收紧
        # （比如人工想连上线前一起看，允许）。`last_stage` 覆盖历史里出现过
        # 的**所有**日期（不论成功失败），取其最小值即"第一次真的跑过"的日子。
        if last_stage:
            launch_date = min(last_stage.keys())
            expected = [d for d in expected if d.isoformat() >= launch_date]

    exp_iso = [d.isoformat() for d in expected]
    hit = [d for d in exp_iso if d in healthy_days]
    coverage = (len(hit) / len(exp_iso)) if exp_iso else float("nan")
    gaps = find_gaps(expected, healthy_days)
    longest_gap = max((g["n_days"] for g in gaps), default=0)
    wk = week_coverage(expected, healthy_days)

    healthy = (
        len(exp_iso) > 0
        and coverage >= min_coverage
        and longest_gap <= max_gap
    )

    return {
        "window": {"start": exp_iso[0] if exp_iso else None,
                   "end": exp_iso[-1] if exp_iso else None,
                   "trading_days": len(exp_iso)},
        "backed_up_days": len(hit),
        "coverage": coverage,
        "gaps": gaps,
        "longest_gap": longest_gap,
        "missing_days": [d for d in exp_iso if d not in healthy_days],
        **wk,
        "thresholds": {"min_coverage": min_coverage, "max_gap": max_gap},
        "healthy": healthy,
        "last_stage_by_day": {d: last_stage[d] for d in exp_iso if d in last_stage},
    }


def alert_line(res: Dict) -> Optional[str]:
    """聚合告警文案。健康时返回 None（静默）。

    刻意只讲聚合事实，不提单次失败 —— 与 CLAUDE.md 的 Slack 静音规则相容。
    """
    if res["healthy"]:
        return None
    w = res["window"]
    parts = [
        f"⚠️ 数据备份连续性降级：过去 {w['trading_days']} 个交易日只成功了 "
        f"{res['backed_up_days']} 次（覆盖率 {res['coverage']:.0%}，"
        f"门槛 {res['thresholds']['min_coverage']:.0%})"
    ]
    if res["longest_gap"] > res["thresholds"]["max_gap"]:
        g = max(res["gaps"], key=lambda x: x["n_days"])
        parts.append(f"最长空档 {res['longest_gap']} 个交易日"
                     f"（{g['start']} → {g['end']}）")
        stages = sorted({res["last_stage_by_day"].get(d) for d in res["missing_days"]
                         if res["last_stage_by_day"].get(d)})
        if stages:
            parts.append(f"涉及阶段: {', '.join(stages)}")
    if res["weeks_missed"]:
        parts.append(f"完全无成功备份的周: {', '.join(res['weeks_missed'])}")
    return "；".join(parts)


def contract_attention(res: Dict) -> List[Dict]:
    """把判定结果**显式**翻成 `step_contract` 的 attention 条目（v0.45.355）。

    同 `scan_continuity.contract_attention`：降级时 `healthy` 的三条判据（窗口非空 /
    覆盖率 / 最长空档）每条不达标的各出一条 warn，无成功备份的 ISO 周另出一条 warn；
    末尾兜底保证「不健康」必有 warn。健康时漏掉的周只作 info（status 仍 ok、退出码仍 0）。
    最长空档那条附上空档内各天**最后一次**卡住的阶段——那是人去查的第一线索。
    """
    items: List[Dict] = []
    healthy = bool(res["healthy"])
    w, th = res["window"], res["thresholds"]
    if not healthy:
        if not w["trading_days"]:
            items.append(step_contract.attention_item(
                f"{_TOOL}.empty_window", "warn",
                "窗口内 0 个交易日 —— 无从计算覆盖率（检查 --since/--end 或历史日志的日期）"))
        elif res["coverage"] < th["min_coverage"]:
            items.append(step_contract.attention_item(
                f"{_TOOL}.coverage_below_threshold", "warn",
                f"过去 {w['trading_days']} 个交易日只成功备份 {res['backed_up_days']} 次"
                f"（覆盖率 {res['coverage']:.0%}，门槛 {th['min_coverage']:.0%}）"))
        if res["longest_gap"] > th["max_gap"] and res["gaps"]:
            g = max(res["gaps"], key=lambda x: x["n_days"])
            stages = sorted({res["last_stage_by_day"].get(d) for d in res["missing_days"]
                             if res["last_stage_by_day"].get(d)})
            tail = f"；涉及阶段: {', '.join(stages)}" if stages else "；空档内无任何历史记录（Step 14 可能根本没跑）"
            items.append(step_contract.attention_item(
                f"{_TOOL}.longest_gap", "warn",
                f"最长空档 {res['longest_gap']} 个交易日（{g['start']} → {g['end']}，"
                f"门槛 ≤{th['max_gap']}）{tail}"))
    if res["weeks_missed"]:
        items.append(step_contract.attention_item(
            f"{_TOOL}.weeks_missed", "info" if healthy else "warn",
            f"完全无成功备份的 ISO 周: {', '.join(res['weeks_missed'])}"))
    if not healthy and not any(a["level"] != "info" for a in items):
        items.append(step_contract.attention_item(
            f"{_TOOL}.degraded", "warn", alert_line(res) or "数据备份连续性降级（判据未归类）"))
    return items


def _emit(args, env: Dict) -> None:
    """`--out` 写外壳；写不出去不改变判定结果（判定本身已经完成）。

    `step_contract.write_out` 原子写、**不替调用方建目录**（父目录不存在 ⇒ `FileNotFoundError`）——与改造前
    `Path.write_text` 同语义；`tests/test_backup_continuity.py::test_out_failure_does_not_change_verdict`
    正是用「父目录不存在」注入写失败，且断言文件不会被建出来。"""
    if not args.out:
        return
    try:
        step_contract.write_out(args.out, env)
    except OSError as e:
        print(f"⚠️  无法写入 {args.out}: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="数据备份连续性体检")
    ap.add_argument("--history", default=None,
                    help="备份历史 JSONL 路径（默认走 ~/alpha-hive-data/logs/backup_status_history.jsonl）")
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

    import datetime as dt
    # v0.45.355：外壳 `date` 与窗口终点取**同一天**——未给 --end 时两者都是洛杉矶当日（business_today）。
    # 此前窗口终点是 `dt.date.today()`（本机 America/Vancouver）：两地目前同日，但 2026-11-01 起温哥华常年 UTC-7，
    # 冬令时每天本机 00:00–01:00 两者差一天——那一小时里缺省窗口从此按洛杉矶日。
    # 编排器的 DATE_STR 是本机日 ⇒ 要比新鲜度就显式传 --end（见 step_contract.business_today）。
    # 非法 --end 在这里抛 ⇒ run_tool 记为崩溃（退出码 3），与改造前「崩溃」同一类。
    run_date = (dt.date.fromisoformat(args.end).isoformat() if args.end
                else step_contract.business_today())

    history_path = Path(args.history) if args.history else _history_file()
    if not history_path.exists():
        msg = f"找不到 {history_path} —— 无法判定连续性（可能是刚接入生产，一天都还没跑过）"
        print(f"❌ {msg}", file=sys.stderr)
        # v0.45.355：这条路径此前不写 --out ⇒ 编排器只能看到「文件不存在」，与「没跑」分不开
        env = step_contract.envelope(
            _TOOL, run_date, _STATUS_BY_RC[3], payload={"reason": msg},
            attention=[step_contract.attention_item(f"{_TOOL}.history_missing", "warn", msg)])
        _emit(args, env)
        if args.json:
            print(json.dumps(env, indent=2, ensure_ascii=False))
        return 3  # 3 而非 2：编排器把 2 保留给"脚本不存在"，见模块 docstring

    res = assess(
        history_path=history_path,
        days=args.days, since=args.since, end=run_date,
        min_coverage=args.min_coverage, max_gap=args.max_gap,
    )
    rc = 0 if res["healthy"] else 1
    env = step_contract.envelope(_TOOL, run_date, _STATUS_BY_RC[rc],
                                 attention=contract_attention(res), payload=res)
    _emit(args, env)

    if args.slack:
        # 刻意不实现：发消息是对外动作，需要用户在对话里明确同意后再接线。
        print("ℹ️  --slack 尚未接线（对外动作需先确认）。"
              "本次仅本地判定。", file=sys.stderr)

    if args.json:
        print(json.dumps(env, indent=2, ensure_ascii=False))
        return rc

    line = alert_line(res)
    if args.quiet:
        if line:
            print(line)
        return rc

    w = res["window"]
    print("━" * 68)
    print("🐝 Alpha Hive · 数据备份连续性体检")
    print("━" * 68)
    print(f"  窗口: {w['start']} → {w['end']}  ({w['trading_days']} 个交易日)")
    print(f"  成功备份: {res['backed_up_days']} 天   覆盖率: {res['coverage']:.1%}"
          f"   (门槛 {res['thresholds']['min_coverage']:.0%})")
    print(f"  ISO 周覆盖: {res['weeks_covered']}/{res['weeks_total']}"
          f"  = {res['week_coverage']:.1%}")
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
        print(f"  完全无成功备份的 ISO 周: {', '.join(res['weeks_missed'])}")
        print()

    print("━" * 68)
    if res["healthy"]:
        print("✅ 连续性健康")
    else:
        print(alert_line(res))
    print("━" * 68)
    return rc


if __name__ == "__main__":
    if step_contract is None:
        # 没有 step_contract 就写不出外壳：只打 stderr，退出码 3（「无法判定」），不写 --out
        print(f"{_TOOL}: 无法导入 step_contract（{_STEP_CONTRACT_IMPORT_ERROR}）—— 退出码 3，按「无法判定」处理；"
              "本次不写 --out", file=sys.stderr)
        sys.exit(3)
    # v0.45.355：未捕获异常 ⇒ 退出码 3 + error 外壳（不再是 Python 默认的 1 = 「降级」）
    sys.exit(step_contract.run_tool(_TOOL, main, date_args=("--end",)))
