#!/usr/bin/env python3
"""日健康判定（v0.45.598）：这一天是不是「某个数据源整体降级」的故障日。

起因（v0.45.421 / 429 的遗留问题，用户 2026-10-22 定档）：2026-10-14 编排器代理探活失败 ⇒ 直连 ⇒ yfinance 从第一个请求起 429 ⇒
`iv_rank` / `unusual_flow` / `analyst_targets` / `catalysts` 四个字段同日 0/30。Step 12（`scan_coverage_gate`）**抓到了**
（退出码 1、attention），但：

1. Step 6 的 `alert_manager` 在 Step 12 **之前**跑，读不到它 ⇒ `alerts-2026-10-14.json` 里没有一个字提到数据源故障；
2. 前瞻检验（维度 IC / 共振加成）把这一天当普通日子收进去，没有任何地方说「这一天是故障日」。

单把异常流升成 P1 治不了：同一个根因一次打掉四个字段，下次换个字段又漏。本模块把「故障日」做成一个判定：

    同一个数据源族（`source` 字段第一个词：yfinance / CBOE）里，≥ `MIN_OUTAGE_FIELDS` 个字段同时低于各自的覆盖线
    ⇒ 该源 `source_outage`；有降级字段但没有源达线 ⇒ `degraded`；没有 ⇒ `ok`。

为什么要「≥2 个字段」而不是「任一字段」：单字段降级是常态（`analyst_targets` 单独 0/30 就出现过 8 天），同源多字段同时降级
才区分得出「整源挂了」与「个别取数失败」——这也是 `scan_coverage_gate.FIELDS` 里 0.70 阈值本身的设计前提。
⚠️ 字段不独立：`rv_30d` / `iv_rank` / `iv_rv_spread` 同出一次 yfinance 日 K 取数，日 K 单独挂就够 3 个字段——这仍然是
「yfinance 日 K 在 ≥30% 的标的上取不到」，判 `source_outage` 是对的，但不要把字段数读成「独立故障数」。

回测（`/usr/local/bin/python3 day_health.py --all`，用 `.swarm_results_*.json` 现存 128 个可判的日子 × 当前 FIELDS；另 2 个空文件判不了）：
共 29 天命中。近 4 周（09-22 起）命中 3 天：09-24 / 09-25 / 10-14——都是已知故障日（10-12 已被整日重放补算，存档里是好数据），其余日子 0 误报。
更早 26 天命中里，2026-03 那批（2 字段）多半是当时代码还不写这些字段，**待验证**；06~08 月的 4~5 字段日多半是 yfinance 限流日，**待验证**。
近 4 周约每 7 个扫描日 1 个故障日——每一个都是真故障，但频率不低；若之后出现「P1 天天响」要回来看是不是门槛太低。

## 不是什么
- **不改评分、不改任何检验口径**：只给每一天一个判定，读者各自决定怎么用。
- **不落盘新产物**：判定是 `.swarm_results_<日期>.json` 的纯函数，重放整天改写那份文件后判定自动跟着变（落一份副本会和它分叉）。
  扫描时算一次进 `scan_timing.extra.day_health`（→ status.json → `alert_manager._check_day_health` 出 P1）；前瞻检验加载器
  需要历史判定时直接对每个日期重算（`summarize_days`）。
- 判不了（结果文件不在 / 不可解析）⇒ `available=False` + 原因，**不当成 ok**。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

#: 同一数据源族里降级字段数达到这个值 ⇒ 该源 `source_outage`
MIN_OUTAGE_FIELDS = 2
#: 判定口径版本：改 `MIN_OUTAGE_FIELDS` / 分族规则 / 判定名即加一（读者据此知道历史判定是哪一版算的）
VERSION = 1

OK, DEGRADED, SOURCE_OUTAGE = "ok", "degraded", "source_outage"


def family(source: str) -> str:
    """`scan_coverage_gate.FIELDS[*]["source"]` 的第一个词：「yfinance 日K（hv_proxy 期）」→ `yfinance`。
    与闸里 `likely_network_layer` 的分族规则同一个（`r["source"].split()[0]`）。"""
    parts = str(source).split()
    return parts[0] if parts else "?"


def classify(cov: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """`scan_coverage_gate.check()` 的返回值 → 日健康判定。纯函数，不读盘。"""
    if not isinstance(cov, dict) or not cov.get("determinable"):
        reason = (cov or {}).get("reason") if isinstance(cov, dict) else f"覆盖率结果不是 dict：{type(cov).__name__}"
        return {"available": False, "reason": reason or "覆盖率检查判不了（无原因）"}
    rows = cov.get("fields")
    if not isinstance(rows, list):
        return {"available": False, "reason": "覆盖率结果缺 fields 列表"}

    degraded = [r for r in rows if isinstance(r, dict) and r.get("degraded")]
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for r in degraded:
        by_source.setdefault(family(r.get("source", "")), []).append(
            {"field": r.get("field"), "have": r.get("have"), "total": r.get("total"),
             "coverage": r.get("coverage"), "source": r.get("source")})
    outage = {fam: fs for fam, fs in by_source.items() if len(fs) >= MIN_OUTAGE_FIELDS}
    verdict = SOURCE_OUTAGE if outage else (DEGRADED if degraded else OK)
    return {
        "available": True, "version": VERSION, "date": cov.get("date"), "tickers": cov.get("tickers"),
        "verdict": verdict, "min_outage_fields": MIN_OUTAGE_FIELDS,
        "degraded_fields": [r.get("field") for r in degraded],
        "outage_sources": outage,
    }


def assess(date: str, results_path: Optional[Path] = None) -> Dict[str, Any]:
    """某一天的判定：读 `.swarm_results_<date>.json`（或指定文件），走覆盖率闸同一套判据。"""
    import scan_coverage_gate
    return classify(scan_coverage_gate.check(date, results_path))


def summarize_days(dates: Iterable[str], home: Optional[Path] = None) -> Dict[str, Any]:
    """一批日期的判定汇总，给前瞻检验加载器**显示**用（不改任何一行样本）。

    `outage` 里每项 `{date, sources: {源族: [字段…]}}`；`undetermined` 里是判不了的日子与原因。
    整个函数不抛：加载器拿它报「含几个故障日」，它自己坏了也不能拖垮检验——但坏了要写原因，不能写成「0 个故障日」。
    """
    try:
        days = sorted({str(d)[:10] for d in dates})
        outage, degraded, undetermined = [], [], []
        for d in days:
            path = (Path(home) / f".swarm_results_{d}.json") if home is not None else None
            v = assess(d, path)
            if not v.get("available"):
                undetermined.append({"date": d, "reason": v.get("reason")})
            elif v["verdict"] == SOURCE_OUTAGE:
                outage.append({"date": d, "sources": {k: [f["field"] for f in fs]
                                                       for k, fs in v["outage_sources"].items()}})
            elif v["verdict"] == DEGRADED:
                degraded.append(d)
        return {"available": True, "version": VERSION, "n_days": len(days), "outage": outage,
                "degraded": degraded, "undetermined": undetermined}
    except Exception as e:  # noqa: BLE001 —— 观测点自身失败只记原因，不拖垮读者
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}


def describe(s: Dict[str, Any]) -> str:
    """`summarize_days` 的一行人话（加载器的报告头用）。"""
    if not s.get("available"):
        return f"日健康判定不可得：{s.get('reason')}"
    out = "；".join(f"{o['date']}（{'、'.join(f'{k}×{len(v)}' for k, v in o['sources'].items())}）"
                    for o in s["outage"]) or "无"
    tail = f"｜降级但未达故障线 {len(s['degraded'])} 天"
    if s["undetermined"]:
        tail += f"｜判不了 {len(s['undetermined'])} 天（{s['undetermined'][0]['date']}：{s['undetermined'][0]['reason']}）"
    return (f"日健康（v{s['version']}，只显示、未改口径）：窗口 {s['n_days']} 天里数据源故障日 "
            f"{len(s['outage'])} 个：{out}{tail}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="日健康判定（只读）")
    ap.add_argument("--date", help="某一天（YYYY-MM-DD）")
    ap.add_argument("--all", action="store_true", help="数据根下全部 .swarm_results_*.json 逐日判定")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.date and not args.all:
        ap.error("需要 --date 或 --all")
    from hive_logger import PATHS
    if args.date:
        dates = [args.date]
    else:
        dates = sorted(os.path.basename(p)[len(".swarm_results_"):-len(".json")]
                       for p in glob.glob(str(Path(PATHS.home) / ".swarm_results_*.json")))
    rows = [(d, assess(d)) for d in dates]
    if args.json:
        print(json.dumps({d: v for d, v in rows}, ensure_ascii=False, indent=1))
        return 0
    for d, v in rows:
        if not v.get("available"):
            print(f"{d}  判不了：{v.get('reason')}")
        elif v["verdict"] != OK:
            srcs = "；".join(f"{k}: {', '.join(f['field'] for f in fs)}" for k, fs in v["outage_sources"].items())
            print(f"{d}  {v['verdict']:<13} 降级字段 {len(v['degraded_fields'])}  {srcs}")
    n_out = sum(1 for _, v in rows if v.get("available") and v["verdict"] == SOURCE_OUTAGE)
    print(f"共 {len(rows)} 天，source_outage {n_out} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
