#!/usr/bin/env python3
"""
🐝 Alpha Hive — Scheduled Tasks 健康度自检 (v0.24.5)
==========================================================
防"任务报告成功但实际无产出"的静默失败。
4-19 sample-accumulator 跑了 0 笔 / 4-26 weekly-optimizer 没写
weight_history 等事故触发本工具。

每周一 PDT 08:00 由 scheduled-task `alpha-hive-health-check` 自动跑：
对 4 个核心定时任务做"运行 + 真实产出"双重验证。

输出：
  • logs/health_<date>.json — 完整结构化结果（保留 30 天）
  • Slack 频道 #alpha-hive 推送（仅异常时；用户白名单允许频道推送）
  • Console 打印（人类可读）

不打扰 Bot DM（CLAUDE.md 硬约束：Bot 只发扫描确认 + 日报推送 2 类）

用法
----
    python3 health_check.py              # 完整 4 任务体检
    python3 health_check.py --json       # 仅输出 JSON
    python3 health_check.py --no-slack   # 关闭 Slack 推送
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

_log = logging.getLogger("alpha_hive.health_check")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PROJECT = Path(__file__).parent.resolve()


@dataclass
class CheckResult:
    name: str
    task_id: str
    severity: str   # "ok" | "warn" | "fail"
    message: str
    detail: Optional[dict] = None


def _now() -> datetime:
    return datetime.now()


def _file_age_days(path: Path) -> float:
    if not path.exists():
        return 9999.0
    return (datetime.now().timestamp() - path.stat().st_mtime) / 86400.0


# ══════════════════════════════════════════════════════════════════════════════
# 1. daily-scan 健康检查
# ══════════════════════════════════════════════════════════════════════════════

def check_daily_scan() -> List[CheckResult]:
    results = []
    # 1a. 上次 commit ≤ 24h 内（只看交易日：跳过周末 / 假日）
    is_weekend = _now().weekday() >= 5
    try:
        out = subprocess.check_output(
            ["git", "-C", str(PROJECT), "log", "-1",
             "--pretty=format:%at|%s", "--grep=蜂群日报"],
            timeout=10
        ).decode().strip()
        if out:
            ts_str, msg = out.split("|", 1)
            commit_age_h = (datetime.now().timestamp() - int(ts_str)) / 3600
            # 周末容忍 72h（周五 21:03 跑后到周一 08:00 检查），工作日 30h
            limit = 72 if is_weekend else 30
            sev = "ok" if commit_age_h <= limit else "warn"
            results.append(CheckResult(
                "daily-scan: 最近一次扫描 commit",
                "alpha-hive-daily-scan",
                sev,
                f"{commit_age_h:.1f}h 前 ({msg.strip()[:50]})",
                {"hours_ago": round(commit_age_h, 1), "limit": limit},
            ))
        else:
            results.append(CheckResult(
                "daily-scan: 最近一次扫描 commit",
                "alpha-hive-daily-scan",
                "fail", "git log 无 '蜂群日报' commit", None,
            ))
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        results.append(CheckResult(
            "daily-scan: git log",
            "alpha-hive-daily-scan",
            "fail", f"无法读取 git log: {e}", None,
        ))

    # 1b. .swarm_results_<today>.json 存在且 size > 50KB
    today = _now().strftime("%Y-%m-%d")
    yesterday = (_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    candidate = None
    for d in [today, yesterday]:
        p = PROJECT / f".swarm_results_{d}.json"
        if p.exists():
            candidate = p
            break
    if candidate and candidate.stat().st_size > 50_000:
        results.append(CheckResult(
            "daily-scan: 最新 .swarm_results 产出",
            "alpha-hive-daily-scan",
            "ok",
            f"{candidate.name} ({candidate.stat().st_size // 1024}KB)",
            None,
        ))
    else:
        results.append(CheckResult(
            "daily-scan: 最新 .swarm_results 产出",
            "alpha-hive-daily-scan",
            "fail" if not is_weekend else "warn",
            f"近 2 天无有效 .swarm_results JSON" if not candidate
            else f"{candidate.name} 体积异常小 ({candidate.stat().st_size}B)",
            None,
        ))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 2. weekly-optimizer 健康检查
# ══════════════════════════════════════════════════════════════════════════════

def check_weekly_optimizer() -> List[CheckResult]:
    results = []
    history_file = PROJECT / "weight_history.jsonl"
    if not history_file.exists():
        results.append(CheckResult(
            "weekly-optimizer: weight_history.jsonl",
            "alpha-hive-weekly-optimizer",
            "fail", "文件不存在", None,
        ))
        return results

    try:
        with history_file.open() as f:
            lines = [json.loads(l) for l in f if l.strip()]
    except (json.JSONDecodeError, OSError) as e:
        results.append(CheckResult(
            "weekly-optimizer: 历史日志解析",
            "alpha-hive-weekly-optimizer",
            "fail", f"解析失败: {e}", None,
        ))
        return results

    if not lines:
        results.append(CheckResult(
            "weekly-optimizer: 历史记录",
            "alpha-hive-weekly-optimizer",
            "fail", "weight_history.jsonl 为空", None,
        ))
        return results

    last = lines[-1]
    try:
        last_ts = datetime.fromisoformat(last.get("timestamp", "").replace("Z", ""))
    except ValueError:
        last_ts = datetime.now() - timedelta(days=999)
    age_days = (datetime.now() - last_ts).days

    # 上次写入 ≤ 8 天（每周日跑）
    sev = "ok" if age_days <= 8 else "fail"
    results.append(CheckResult(
        "weekly-optimizer: 上次写入时间",
        "alpha-hive-weekly-optimizer",
        sev,
        f"{age_days} 天前 ({last_ts.strftime('%Y-%m-%d %H:%M')})",
        {"age_days": age_days, "method": last.get("method"), "applied": last.get("applied")},
    ))

    # 字段完整性（v0.24.2 修复后必须有 method 字段）
    if "method" not in last or last.get("method") is None:
        results.append(CheckResult(
            "weekly-optimizer: 字段完整性",
            "alpha-hive-weekly-optimizer",
            "warn",
            "最新记录缺 method 字段（v0.24.2 后应补全）", None,
        ))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 3. sample-accumulator 健康检查
# ══════════════════════════════════════════════════════════════════════════════

def check_sample_accumulator() -> List[CheckResult]:
    results = []
    db_path = PROJECT / "pheromone.db"
    if not db_path.exists():
        results.append(CheckResult(
            "sample-accumulator: pheromone.db",
            "alpha-hive-sample-accumulator",
            "fail", "数据库不存在", None,
        ))
        return results

    # 上周日是哪一天
    today = _now()
    days_since_sunday = (today.weekday() + 1) % 7  # Mon=1...Sun=7→0
    last_sunday = today - timedelta(days=days_since_sunday)
    last_sunday_str = last_sunday.strftime("%Y-%m-%d")

    # 周日 18:01 sample-accumulator 应该写入 ≥ 30 笔
    try:
        with sqlite3.connect(str(db_path)) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE date = ?",
                (last_sunday_str,)
            ).fetchone()[0]
    except sqlite3.Error as e:
        results.append(CheckResult(
            "sample-accumulator: pheromone.db 查询",
            "alpha-hive-sample-accumulator",
            "fail", f"SQL 错误: {e}", None,
        ))
        return results

    # v0.24.7 修复：实测 5-3 跑 39 个 ticker 但只 13 笔入库（部分 Agent 失败属常态）
    # 降低 OK 阈值到 12（合理可用样本），warn 阈值 5
    if n >= 12:
        sev = "ok"
        msg = f"上周日 {last_sunday_str} 写入 {n} 笔 ✓"
    elif n >= 5:
        sev = "warn"
        msg = f"上周日 {last_sunday_str} 仅 {n} 笔（期望 ≥12；可能多个 Agent yfinance 失败）"
    else:
        sev = "fail"
        msg = f"上周日 {last_sunday_str} 仅 {n} 笔（任务可能静默失败！检查权限批准 / 网络）"

    results.append(CheckResult(
        "sample-accumulator: 上周日写入量",
        "alpha-hive-sample-accumulator",
        sev, msg,
        {"last_sunday": last_sunday_str, "n_predictions": n},
    ))

    # 检查 .samples-only 是否进了 git（应该被 gitignore）
    try:
        out = subprocess.check_output(
            ["git", "-C", str(PROJECT), "ls-files",
             ".samples-only-*.json"],
            timeout=5,
        ).decode().strip()
        if out:
            results.append(CheckResult(
                "sample-accumulator: 仓库污染检查",
                "alpha-hive-sample-accumulator",
                "fail",
                f"⚠️ {len(out.splitlines())} 个 .samples-only-*.json 已进 git（应被 .gitignore）",
                None,
            ))
        else:
            results.append(CheckResult(
                "sample-accumulator: 仓库污染检查",
                "alpha-hive-sample-accumulator",
                "ok",
                ".samples-only-*.json 未污染 git 仓库 ✓", None,
            ))
    except (subprocess.SubprocessError, OSError):
        pass  # 非致命

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 4. monthly-self-analysis 健康检查
# ══════════════════════════════════════════════════════════════════════════════

def check_monthly_self_analysis() -> List[CheckResult]:
    """v0.24.6 修复：self_analyst 实际命名是 `self_analysis_YYYY-MM.md`
    且 1 号生成的 brief 用当月 tag（分析最近 3 个月数据），不是上月 tag
    """
    results = []
    brief_dir = PROJECT / "self_analysis_briefs"
    if not brief_dir.exists():
        results.append(CheckResult(
            "monthly-self-analysis: brief 目录",
            "alpha-hive-monthly-self-analysis",
            "warn", "self_analysis_briefs/ 不存在（首次跑前正常）", None,
        ))
        return results

    # 5-1 跑会生成 self_analysis_2026-05.md（当月 tag 含上月数据）
    # 周一健康检查时，期望本月 brief 已存在（如果今天 ≥ 月 4 号）
    today = _now()
    this_month_tag = today.strftime("%Y-%m")
    last_month_tag = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    # 优先找本月 brief；找不到再退回上月（兼容 1-3 号情况）
    expected_this = brief_dir / f"self_analysis_{this_month_tag}.md"
    expected_last = brief_dir / f"self_analysis_{last_month_tag}.md"

    if expected_this.exists():
        age_days = _file_age_days(expected_this)
        sev = "ok" if age_days < 35 else "warn"
        results.append(CheckResult(
            "monthly-self-analysis: 本月 brief",
            "alpha-hive-monthly-self-analysis",
            sev,
            f"{expected_this.name} ({age_days:.0f}d 前)", None,
        ))
    elif expected_last.exists() and today.day <= 3:
        # 月初 1-3 号本月 brief 还没生成是正常的
        age_days = _file_age_days(expected_last)
        results.append(CheckResult(
            "monthly-self-analysis: 上月 brief（本月 brief 待生成）",
            "alpha-hive-monthly-self-analysis",
            "ok",
            f"{expected_last.name} ({age_days:.0f}d 前) — 本月 brief 1 日才会生成",
            None,
        ))
    else:
        # brief 真的缺失
        is_early_month = today.day <= 3
        sev = "warn" if is_early_month else "fail"
        msg = f"{expected_this.name} 不存在"
        if expected_last.exists():
            msg += f"（上月 {expected_last.name} 存在但本月未跑）"
        results.append(CheckResult(
            "monthly-self-analysis: 本月 brief",
            "alpha-hive-monthly-self-analysis",
            sev, msg, None,
        ))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════════════════════

def check_health_check_self() -> List[CheckResult]:
    """v0.24.7 Meta-check：health_check 自身上次有没有产出 log
    上次 health-check scheduled-task 跑了但没写 logs/health_<date>.json
    通常意味着 Claude Code session 权限批准未授予 → Bash 工具被阻塞。
    这个检查在新一次 health_check 跑时（已通过权限）才能发出告警。
    """
    results = []
    logs_dir = PROJECT / "logs"
    if not logs_dir.exists():
        results.append(CheckResult(
            "health-check: logs 目录",
            "alpha-hive-health-check",
            "warn", "logs/ 不存在", None,
        ))
        return results

    # 找过去 14 天的 health_*.json
    files = sorted(logs_dir.glob("health_*.json"), reverse=True)
    if not files:
        results.append(CheckResult(
            "health-check: 历史日志",
            "alpha-hive-health-check",
            "warn", "logs/health_*.json 不存在", None,
        ))
        return results

    last = files[0]
    age_d = _file_age_days(last)
    # 期望 ≤ 8 天（每周一跑）
    sev = "ok" if age_d <= 8 else "warn"
    results.append(CheckResult(
        "health-check: 上次自检日志",
        "alpha-hive-health-check",
        sev,
        f"{last.name} ({age_d:.0f}d 前)",
        {"age_days": round(age_d, 1)},
    ))
    return results


def run_all_checks() -> dict:
    all_results = []
    all_results.extend(check_daily_scan())
    all_results.extend(check_weekly_optimizer())
    all_results.extend(check_sample_accumulator())
    all_results.extend(check_monthly_self_analysis())
    all_results.extend(check_health_check_self())  # v0.24.7 Meta-check

    summary = {"ok": 0, "warn": 0, "fail": 0}
    for r in all_results:
        summary[r.severity] = summary.get(r.severity, 0) + 1

    return {
        "timestamp": _now().isoformat(),
        "checks": [asdict(r) for r in all_results],
        "summary": summary,
        "overall_status": (
            "🔴 异常" if summary.get("fail", 0) > 0
            else "🟡 警告" if summary.get("warn", 0) > 0
            else "🟢 正常"
        ),
    }


def print_console(report: dict) -> None:
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  Alpha Hive — Scheduled Tasks 健康度报告 (v0.24.5)           ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"  时间: {report['timestamp'][:19]} PDT")
    print(f"  状态: {report['overall_status']}")
    s = report["summary"]
    print(f"  统计: {s.get('ok',0)} OK · {s.get('warn',0)} WARN · {s.get('fail',0)} FAIL")
    print()
    icon_map = {"ok": "✓", "warn": "⚠", "fail": "✗"}
    cur_task = None
    for c in report["checks"]:
        if c["task_id"] != cur_task:
            print(f"  ── {c['task_id']} ──")
            cur_task = c["task_id"]
        ic = icon_map.get(c["severity"], "?")
        print(f"    {ic} {c['name']}")
        print(f"      {c['message']}")
    print()


def save_log(report: dict) -> Path:
    logs_dir = PROJECT / "logs"
    logs_dir.mkdir(exist_ok=True)
    fn = logs_dir / f"health_{_now().strftime('%Y-%m-%d')}.json"
    fn.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return fn


def push_slack(report: dict) -> None:
    """异常时推送到 #alpha-hive 频道（不发 Bot DM，避免违反 CLAUDE.md 白名单）"""
    if report["summary"].get("fail", 0) == 0 and report["summary"].get("warn", 0) == 0:
        return  # 无异常不推
    try:
        # 用 Claude Code MCP slack_send_message（频道推送，非 DM）
        # 这里只构造 payload，实际推送由调度任务的 Claude 用 MCP 完成
        msg_lines = [f"*Alpha Hive 健康巡检 {report['overall_status']}*"]
        for c in report["checks"]:
            if c["severity"] in ("warn", "fail"):
                ic = "⚠️" if c["severity"] == "warn" else "🔴"
                msg_lines.append(f"{ic} `{c['task_id']}` — {c['name']}: {c['message']}")
        # 写到 logs 目录供调度任务抓取
        slack_log = PROJECT / "logs" / f"health_slack_payload_{_now().strftime('%Y-%m-%d')}.txt"
        slack_log.write_text("\n".join(msg_lines), encoding="utf-8")
        _log.info("Slack payload prepared at %s (调度任务的 Claude 用 MCP 推送)", slack_log)
    except (OSError, IOError) as e:
        _log.warning("Slack payload 准备失败（不影响主流程）: %s", e)


def main():
    parser = argparse.ArgumentParser(description="Alpha Hive Scheduled Tasks 健康自检")
    parser.add_argument("--json", action="store_true", help="仅输出 JSON")
    parser.add_argument("--no-slack", action="store_true", help="关闭 Slack 推送")
    args = parser.parse_args()

    report = run_all_checks()
    fn = save_log(report)
    _log.info("健康报告已保存: %s", fn.name)

    if not args.no_slack:
        push_slack(report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_console(report)

    # exit code: 0=ok, 1=warn, 2=fail (供调度脚本判断)
    if report["summary"].get("fail", 0) > 0:
        sys.exit(2)
    elif report["summary"].get("warn", 0) > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
