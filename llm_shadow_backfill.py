"""LLM 影子 A/B：把「扫描中途出错、状态文件里没记下逐只结果」的那一天，按预注册原规则补记（v0.45.687）。

背景（2026-10-29）：g2 的第 4 个付费日，蜂群已跑完、27 只标的的付费结果都写进了 `<影子根>/.swarm_results_<日期>.json`，
随后 P4b（跨标的 LLM 步骤）抛了 API 超时，影子入口只记了错误信息就退出——状态文件里 `tickers_scanned` 为空。
v0.45.683 之后同类的日子会在出错当场自动救回（`llm_shadow_scan.salvage_swarm_results`）；这个工具补的是 683 之前
已经发生的那一天。预注册修订 1（`experiments/llm_shadow_prereg.md`）决定：**按预注册原规则判健康**（不含 683 之后才有的
`scan_error` 规则，那条自 10-30 起适用），所以这里重算质量时**不传 `error`**。

用法（缺省只看、不写）：
    /usr/local/bin/python3 llm_shadow_backfill.py --day 2026-10-29            # 看计划
    /usr/local/bin/python3 llm_shadow_backfill.py --day 2026-10-29 --apply    # 真写

不变式：
  ① 只写**影子根**（拒绝影子根与生产根相同 / 嵌套）；不花钱、不碰生产库。
  ② 只补「出错、且逐只结果为空」的付费日；已经有逐只结果的日子、已经补记过的日子、正常没出错的日子一律拒绝——
     这个工具不是用来改写历史的。
  ③ 原状态文件先原样留一份 `<日期>.json.orig`（不匹配读者的 `????-??-??.json`，不会被当成另一天），补记过的状态里带
     `backfill` 一节写明来源、规则和时间。
  ④ 质量判定调用的就是 `llm_shadow_scan.quality_report`——不在这里另写一份规则；判出来不健康就如实写不健康。
  ⑤ 落盘顺序：先写 swarm_results 副本、再留 `.orig`、最后改状态文件——中途失败不会留下「状态已改、数据没到位」的一天。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import llm_shadow_scan as sh

#: swarm_results 文件的修改时间必须落在「这次扫描的起止」附近（起点 = 状态写入时刻 − 耗时）；
#: 超出说明它可能是另一次扫描写的，不能当成这天的结果
MTIME_SLACK_SEC = 120.0

QUALITY_RULE_NOTE = "预注册原规则（不含 scan_error；该条自 2026-10-30 起适用，见预注册修订 1）"


def orig_path(shadow_root: Path, day: str) -> Path:
    p = sh.status_path(shadow_root, day, "run")
    return p.with_name(p.name + ".orig")


def plan_backfill(shadow_root: Path, day: date, *, now: Optional[datetime] = None) -> Dict:
    """只读：返回 {"problems": [...], "new_status": {...}|None, "swarm_results": {...}, "summary": {...}}。
    `problems` 非空 ⇒ 不可补记（其余字段仍尽量给，便于看清原因）。"""
    import ledger_io
    shadow_root = Path(shadow_root)
    d = day.isoformat()
    problems: List[str] = []
    st_path = sh.status_path(shadow_root, d, "run")
    out: Dict = {"problems": problems, "new_status": None, "swarm_results": {}, "summary": {}}
    if not st_path.exists():
        problems.append(f"{st_path.name} 不存在")
        return out
    status = ledger_io.load_json(st_path)
    if status.get("mode") != "run":
        problems.append(f"状态文件 mode={status.get('mode')!r}，只补付费日（run）")
    if status.get("backfill") or orig_path(shadow_root, d).exists():
        problems.append("这天已经补记过了（`backfill` 一节或 .orig 已存在）——不重复补")
    if not status.get("error"):
        problems.append("状态文件里没有 error——这天不是「扫描中途出错」，没有可补的")
    if status.get("tickers_scanned"):
        problems.append(f"状态文件里已有逐只结果（{len(status['tickers_scanned'])} 只）——不改写已有数据")
    sw_copy = sh.swarm_results_path(shadow_root, d, "run")
    if sw_copy.exists():
        problems.append(f"{sw_copy.name} 已存在——不覆盖")

    src = shadow_root / f".swarm_results_{d}.json"
    swarm: Dict = {}
    if not src.exists():
        problems.append(f"{src.name} 不存在，无从补记")
    else:
        try:
            doc = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"读 {src.name} 失败：{type(e).__name__}: {e}")
            doc = None
        if doc is not None:
            if not isinstance(doc, dict) or not doc:
                problems.append(f"{src.name} 内容为空或不是对象")
            else:
                swarm = doc
        if swarm:
            try:
                written = datetime.fromisoformat(status["written_at"]).timestamp()
                started = written - float(status.get("elapsed_sec") or 0.0)
                mt = src.stat().st_mtime
                if not (started - MTIME_SLACK_SEC <= mt <= written + MTIME_SLACK_SEC):
                    problems.append(f"{src.name} 的修改时间不在这次扫描的起止之内——可能是另一次扫描写的")
            except (KeyError, ValueError, TypeError, OSError) as e:
                problems.append(f"没法核对 {src.name} 的修改时间：{type(e).__name__}: {e}")
    requested = list(status.get("tickers_requested") or [])
    extra = sorted(set(swarm) - set(requested)) if swarm else []
    if extra:
        problems.append(f"swarm_results 里有不在请求名单里的标的：{extra}")

    per_ticker = sh.per_ticker_from_swarm_results(swarm, requested)
    usage = status.get("usage") or {}
    quality = sh.quality_report(usage, per_ticker, llm_on=True)          # 不传 error：预注册原规则
    scanned = [tk for tk, v in per_ticker.items() if v.get("present")]
    out["swarm_results"] = swarm
    out["summary"] = {
        "date": d, "requested": len(requested), "present": len(scanned),
        "missing": [tk for tk in requested if tk not in scanned],
        "healthy": quality["healthy"], "reasons": quality["reasons"],
        "failed_call_rate": quality["failed_call_rate"], "queen_llm_rate": quality["queen_llm_rate"],
    }
    if not problems:
        new = copy.deepcopy(status)
        new["per_ticker"] = per_ticker
        new["tickers_scanned"] = scanned
        new["quality"] = quality
        new["salvaged"] = True
        new["backfill"] = {
            "at": (now or datetime.now()).isoformat(timespec="seconds"),
            "tool": "llm_shadow_backfill.py",
            "source": src.name,
            "quality_rule": QUALITY_RULE_NOTE,
            "original_status": orig_path(shadow_root, d).name,
            "original_error": status.get("error"),
        }
        out["new_status"] = new
    return out


def apply_backfill(shadow_root: Path, day: date, plan: Dict) -> List[Path]:
    """按 `plan_backfill` 的计划落盘（plan 有 problems 时拒绝）。返回写下的文件。顺序见模块 docstring ⑤。"""
    import ledger_io
    if plan["problems"] or plan["new_status"] is None:
        raise ValueError(f"计划里有问题，不落盘：{plan['problems']}")
    shadow_root = Path(shadow_root)
    d = day.isoformat()
    st_path = sh.status_path(shadow_root, d, "run")
    original = ledger_io.load_json(st_path)
    written: List[Path] = []
    sw_path = sh.swarm_results_path(shadow_root, d, "run")
    ledger_io.write_json(sw_path, plan["swarm_results"])
    written.append(sw_path)
    op = orig_path(shadow_root, d)
    ledger_io.write_json(op, original)
    written.append(op)
    ledger_io.write_json(st_path, plan["new_status"])
    written.append(st_path)
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="补记「扫描中途出错、逐只结果没进状态文件」的付费日（缺省只看、不写）")
    ap.add_argument("--day", required=True, help="要补记的日期 YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="真写（缺省只打印计划）")
    ap.add_argument("--shadow-root", default=None, help="影子根；缺省 = 生产根的同级 alpha-hive-llm-shadow")
    args = ap.parse_args(argv)
    try:
        day = date.fromisoformat(args.day)
    except ValueError:
        print(f"❌ --day 不是 YYYY-MM-DD：{args.day!r}")
        return sh.EXIT_USAGE
    import hive_logger
    prod_root = Path(hive_logger.resolve_data_root())
    shadow_root = Path(args.shadow_root) if args.shadow_root else sh.default_shadow_root(prod_root)
    try:
        sh.check_roots(prod_root, shadow_root)
    except ValueError as e:
        print(f"❌ {e}")
        return sh.EXIT_USAGE
    plan = plan_backfill(shadow_root, day)
    sm = plan["summary"]
    if sm:
        print(f"{sm['date']}：请求 {sm['requested']} 只，救回 {sm['present']} 只，缺 {sm['missing']}")
        print(f"  按预注册原规则：健康={sm['healthy']}（失败率 {sm['failed_call_rate']}，走 LLM {sm['queen_llm_rate']}）"
              f"{' 理由 ' + str(sm['reasons']) if sm['reasons'] else ''}")
    if plan["problems"]:
        for p in plan["problems"]:
            print(f"❌ 不能补记：{p}")
        return sh.EXIT_USAGE
    if not args.apply:
        print("（只看，没写。确认无误后加 --apply）")
        return sh.EXIT_OK
    for p in apply_backfill(shadow_root, day, plan):
        print(f"已写 {p}")
    return sh.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
