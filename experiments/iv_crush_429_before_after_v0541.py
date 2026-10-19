#!/usr/bin/env python3
"""v0.45.541：IV-crush 抓取移出扫描前后的 yfinance 429 对照（只读）。

口径与生产计量器 `scan_coverage_gate.check_rate_limit()` 相同：数 `~/.claude/logs/orchestrator-YYYY-MM-DD.log`
里 `Too Many Requests` 的出现次数（编排器日志按日期分文件，不用切 alpha_hive.log 的行号区间）。
另列两个旁证，因为「429 少 ≠ 健康」（断网时连 429 都攒不出来，见 auto-memory alpha-hive-yfinance-rate-limit）：
`SSL` 错误行数、编排器 Step 2 是否成功。

用法：
    /usr/local/bin/python3 experiments/iv_crush_429_before_after_v0541.py            # 全部日志
    /usr/local/bin/python3 experiments/iv_crush_429_before_after_v0541.py --since 2026-10-01

分组：日期 < BOUNDARY 记「前」，≥ BOUNDARY 记「后」。BOUNDARY 是**生产首个跑新代码的扫描日**，
不是推送日（生产克隆在扫描前才快进），上线后按 status.json / 日志核实再改。
"""
from __future__ import annotations

import argparse
import re
import statistics
from pathlib import Path

#: 生产首个跑 v0.45.541 的扫描日。推送于 2026-10-19 约 04:xx PDT，早于当天 14:00 扫描 ⇒ 预期 10-19 即跑新代码；
#: 待核实：`git -C ~/alpha-hive-prod log -1 --format=%h` 在 10-19 扫描后应已含本版提交，否则改成实际首日。
BOUNDARY = "2026-10-19"

LOG_DIR = Path.home() / ".claude" / "logs"
_NAME = re.compile(r"^orchestrator-(\d{4}-\d{2}-\d{2})\.log$")


def scan(since: str | None = None):
    rows = []
    for p in sorted(LOG_DIR.iterdir()):
        m = _NAME.match(p.name)
        if not m or (since and m.group(1) < since):
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        rows.append({
            "date": m.group(1),
            "n429": txt.count("Too Many Requests"),
            "ssl": sum(1 for ln in txt.splitlines() if "SSL" in ln and ("Error" in ln or "EOF" in ln)),
            "step2_ok": "✅ Step 2" in txt,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    args = ap.parse_args()
    rows = scan(args.since)
    print(f"{'date':<12}{'429':>6}{'SSL':>6}  step2")
    for r in rows:
        print(f"{r['date']:<12}{r['n429']:>6}{r['ssl']:>6}  {'ok' if r['step2_ok'] else '—'}")
    for label, op, grp in (("前", "<", [r for r in rows if r["date"] < BOUNDARY]),
                           ("后", "≥", [r for r in rows if r["date"] >= BOUNDARY])):
        if not grp:
            print(f"{label}（{op} {BOUNDARY}）：0 天")
            continue
        v = [r["n429"] for r in grp]
        print(f"{label}（{op} {BOUNDARY}）：{len(v)} 天，429 中位数 {statistics.median(v)}，"
              f"均值 {statistics.mean(v):.1f}，最大 {max(v)}，为 0 的天数 {sum(1 for x in v if x == 0)}")


if __name__ == "__main__":
    main()
