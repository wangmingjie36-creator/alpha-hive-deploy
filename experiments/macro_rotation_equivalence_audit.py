#!/usr/bin/env python3
"""v0.45.652 世代边界的等价证据：某天的板块轮动表是不是 11 只齐全、预取包是不是全覆盖。

为什么需要这份证据
------------------
v0.45.652 把行情三字段以外的 yfinance 日线读者收进唯一入口 `yf_daily`（取到即去掉公司行动空行）。进评分的只有两条：

① 板块轮动（`fred_macro._fetch_sector_rotation`，`period="5d"`，`Close.iloc[0]` 当起点）：首行空行让那只 ETF 被丢。
   11 只 SPDR 板块 ETF **同一天除息**，所以一旦漏进来就是 11 只一起丢、整张表变空——不会只丢几只。
   新代码只在这种日子改变输出（把丢掉的 ETF 加回来）；轮动表经 GuardBee 宏观票 → `RegimeWeightAdjuster` 进 final_score。
② Rival 直连回退（`swarm_agents.base._yf_history`）：只在预取包缺票时走；首行空行逐位不变（真函数对拍），
   只有末行空行会变——需要「事件日期晚于末根 K 线」，收盘后的生产扫描（14:00 PDT）碰不到。

边界想登记在 2026-10-19（与 v0.45.537 / 538 / 559 / 639 同日、扩同一标签），新代码 10-26 才首跑，前提是**证明 10-19 起
那几天新旧代码输出相同**（照 v0.45.383 先例，见 `ic_rerun_readiness._BOUNDARY_EQUIVALENCE`）。旧代码不记「轮动表里有几只」
在每只标的的记录里——只有当天报告的 `macro_context.sector_rotation.full` 有——所以证据按天从外部读出来、落盘冻结。

判据（每个日期）
----------------
    equivalent                    轮动表 11 只齐全（没有任何 ETF 被丢 ⇒ 新代码加不回任何东西）且当天每轮扫描的预取包都是全覆盖
                                  （Rival 没走直连回退 ⇒ 新代码碰不到它）
    equivalent_no_yfinance_data   当天整轮扫描 yfinance **一次成功响应都没有**：`yf_gate` 计数 `calls == rate_limited`
                                  （每一次进入 yfinance 的调用都以 429 收场，其余的被冷却挡在门外）⇒ 没有任何日线到手，
                                  就不可能有空行，新旧代码走同一条失败路径。证据取当天的 `logs/scan_timing.json`——它每天被覆盖，
                                  只能在当天之后、下一轮扫描之前冻结（2026-10-26 就是这么取到的）
    not_proven                    其余（轮动表缺 / 不全、预取包缺、日志已轮转读不到）——**证不出等价就不算等价**

同时记下 09-28 起每天的情况，作为「`guard.macro_adj` 不点名」的依据（见 ic_rerun_readiness 同版条目）：
轮动表不全的日子要么是部分缺失（同日除息 ⇒ 不是空行），要么 5 日窗口的边界日不是除息日。

用法（只读数据根，只写 --out）
----
    /usr/local/bin/python3 experiments/macro_rotation_equivalence_audit.py --home ~/alpha-hive-data \\
        --from 2026-09-28 --to 2026-10-23 --out experiments/macro_rotation_equivalence_20261019.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

#: 11 只 SPDR 板块 ETF（与 `fred_macro._SECTOR_ETFS` 同一组）最近四次除息日：2026-10-25 用 yfinance `Ticker(etf).dividends`
#: 逐只读出，11 只完全相同。
SPDR_EX_DATES = ("2025-12-22", "2026-03-23", "2026-06-22", "2026-09-21")
N_SECTOR_ETFS = 11
_BUNDLE = re.compile(r"市场数据包：日线 (\d+)/(\d+) 只")
_PT = ZoneInfo("America/Los_Angeles")


def _rotation_full(home: Path, day: str) -> Optional[int]:
    try:
        with open(home / f"alpha-hive-daily-{day}.json", encoding="utf-8") as fh:
            rep = json.load(fh)
    except (OSError, ValueError):
        return None
    sr = (rep.get("macro_context") or {}).get("sector_rotation") if isinstance(rep, dict) else None
    full = sr.get("full") if isinstance(sr, dict) else None
    return len(full) if isinstance(full, dict) else None


def _bundle_runs(home: Path) -> Dict[str, List[str]]:
    """{太平洋日期: ["a/b", ...]}——结构化日志里「市场数据包：日线 a/b 只」每轮一条。"""
    out: Dict[str, List[str]] = {}
    for path in sorted(glob.glob(str(home / "logs" / "alpha_hive_structured.jsonl*"))):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "市场数据包" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                m = _BUNDLE.search(str(rec.get("msg") or ""))
                ts = rec.get("ts")
                if not (m and ts):
                    continue
                day = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(_PT).date().isoformat()
                out.setdefault(day, []).append(f"{m.group(1)}/{m.group(2)}")
    return out


def _yfinance_gate(home: Path, day: str) -> Optional[Dict]:
    """当天那轮扫描的 `yf_gate` 计数（`logs/scan_timing.json` 的 `counters.yfinance`）；文件已是别的日期 / 读不出 ⇒ None。"""
    try:
        with open(home / "logs" / "scan_timing.json", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or st.get("date") != day:
        return None
    yf = (st.get("counters") or {}).get("yfinance")
    return dict(yf) if isinstance(yf, dict) else None


def audit(home: Path, start: str, end: str, prior: Optional[Dict[str, Dict]] = None) -> Dict[str, Dict]:
    """`prior`：上一版证据的 days。`scan_timing.json` 每天被覆盖，旧日子的 `yfinance_gate` 只能沿用当时冻结的那份。"""
    bundles = _bundle_runs(home)
    days: Dict[str, Dict] = {}
    for f in sorted(home.glob(".swarm_results_*.json")):
        m = re.search(r"_(\d{4}-\d{2}-\d{2})\.json$", f.name)
        if not m or not (start <= m.group(1) <= end):
            continue
        day = m.group(1)
        full = _rotation_full(home, day)
        runs = bundles.get(day)
        complete = bool(runs) and all(a == b for a, b in (r.split("/") for r in runs))
        gate = _yfinance_gate(home, day) or ((prior or {}).get(day) or {}).get("yfinance_gate")
        no_yf_data = (isinstance(gate, dict) and isinstance(gate.get("calls"), int)
                      and gate.get("calls") == gate.get("rate_limited"))
        if full == N_SECTOR_ETFS and complete:
            status, reason = "equivalent", ""
        elif no_yf_data:
            status, reason = ("equivalent_no_yfinance_data",
                              f"yfinance 整轮 0 次成功响应（calls={gate['calls']} = rate_limited；"
                              f"冷却挡下 {gate.get('blocked_by_cooldown')} 次）⇒ 没有日线到手")
        elif full != N_SECTOR_ETFS:
            status, reason = "not_proven", f"轮动表 {full} 只（不是 {N_SECTOR_ETFS}）"
        else:
            status, reason = "not_proven", ("日志里没有当天的预取包记录" if not runs else f"预取包没有全覆盖：{runs}")
        days[day] = {"sector_rotation_full": full, "prefetch_bundle": runs, "yfinance_gate": gate,
                     "status": status, "reason": reason}
    return days


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", required=True, help="生产数据根（含 alpha-hive-daily-<日期>.json 与 logs/）")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    prior = None
    try:
        with open(a.out, encoding="utf-8") as fh:
            prior = json.load(fh).get("days")
    except (OSError, ValueError, AttributeError):
        pass
    days = audit(Path(os.path.expanduser(a.home)), a.start, a.end, prior)
    doc = {
        "version": "v0.45.652",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "generator": "experiments/macro_rotation_equivalence_audit.py",
        "spdr_ex_dates": list(SPDR_EX_DATES),
        "n_sector_etfs": N_SECTOR_ETFS,
        "days": days,
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    for d, v in days.items():
        print(d, v["status"], v["sector_rotation_full"], v["prefetch_bundle"], v["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
