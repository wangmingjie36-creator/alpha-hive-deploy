"""期权快照「槽位日期 vs 数据所属会话」普查（v0.45.238，只读）。

每份常规快照 `options_snapshot_{T}_{D}.json`，按 `_snapshot_timestamp`（naive = 本机时区，
生产机为太平洋时间）推出数据所属的 ET 交易会话 S，分类：

  PREV_SESSION_in_slot   S < D：前一会话的数据占着 D 的槽位（v0.45.238 修的 bug）
  intraday_incomplete    S == D 但冻结于 16:00 ET 前（会话未完成）
  later_session_in_slot  S > D：时间戳晚于槽位日（成因未查，待验证）
  label_non_trading_day  D 本身不是交易日（周末/假日强制跑）
  ok_after_close         S == D 且收盘后冻结

有编排器日志（~/.claude/logs/orchestrator-D.log）的日子，再数这一组文件在 D 日日志里被命中的次数——
命中数 > 0 才说明坏槽位真被那天的正式扫描吃进去了。

    /usr/local/bin/python3 experiments/snapshot_session_census.py [--cache-dir DIR]
"""

import argparse
import collections
import glob
import json
import os
import re
import sys
from datetime import datetime, time, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from is_trading_day import is_trading_day  # noqa: E402

ET = ZoneInfo("America/New_York")
RE = re.compile(r"options_snapshot_(.+)_(\d{4}-\d{2}-\d{2})\.json$")
BAD = ("PREV_SESSION_in_slot", "later_session_in_slot", "intraday_incomplete", "unparseable")


def session_of(et_dt: datetime) -> date:
    d = et_dt.date()
    if is_trading_day(d)[0] and et_dt.time() >= time(9, 30):
        return d
    d -= timedelta(days=1)
    while not is_trading_day(d)[0]:
        d -= timedelta(days=1)
    return d


def classify(path: str):
    m = RE.search(os.path.basename(path))
    if not m:
        return None
    ticker, slot = m.group(1), m.group(2)
    try:
        with open(path, encoding="utf-8") as f:
            ts = json.load(f).get("_snapshot_timestamp") or ""
        et = datetime.fromisoformat(ts).astimezone(ET)
    except (OSError, ValueError):
        return slot, ticker, "unparseable", ""
    s, d = session_of(et), date.fromisoformat(slot)
    if not is_trading_day(d)[0]:
        cls = "label_non_trading_day"
    elif s < d:
        cls = "PREV_SESSION_in_slot"
    elif s > d:
        cls = "later_session_in_slot"
    elif et < datetime.combine(s, time(16, 0), ET):
        cls = "intraday_incomplete"
    else:
        cls = "ok_after_close"
    return slot, ticker, cls, ts[:19]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--logs-dir", default=os.path.expanduser("~/.claude/logs"))
    args = ap.parse_args()
    if args.cache_dir is None:
        from hive_logger import PATHS
        args.cache_dir = str(PATHS.cache_dir)

    rows = [r for r in map(classify, sorted(glob.glob(
        os.path.join(args.cache_dir, "options_snapshot_*.json")))) if r]
    print("total", len(rows), dict(collections.Counter(r[2] for r in rows)))
    groups = collections.defaultdict(list)
    for slot, ticker, cls, ts in rows:
        if cls in BAD:
            groups[(slot, cls)].append((ts, ticker))
    print("\n槽位日       类别                    份数  冻结时刻范围            本组被命中")
    for (slot, cls), items in sorted(groups.items()):
        tss = sorted(ts for ts, _ in items)
        log = os.path.join(args.logs_dir, f"orchestrator-{slot}.log")
        hits = "无日志"
        if os.path.exists(log):
            with open(log, errors="replace") as f:
                text = f.read()
            # 只数**这一组**文件被命中的次数，不是当日全部命中
            hits = sum(text.count(f"期权快照命中: options_snapshot_{t}_{slot}.json ") for _, t in items)
        print(f"{slot}  {cls:22s} {len(tss):4d}  {tss[0][11:]}–{tss[-1][11:]}  {hits}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
