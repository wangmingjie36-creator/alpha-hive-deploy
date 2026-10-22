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
v0.45.608 补 `MIN_TICKERS=10` 后共 26 天命中（补之前 29 天，其中 3 天是标的数 <10 的小样本，见下勘误）。近 4 周（09-22 起）命中 3 天：09-24 / 09-25 / 10-14——都是已知故障日（10-12 已被整日重放补算，存档里是好数据），其余日子 0 误报。
更早 23 天命中里，2026-03 那批（2 字段）多半是当时代码还不写这些字段，**待验证**；06~08 月的 4~5 字段日多半是 yfinance 限流日，**待验证**。
近 4 周约每 7 个扫描日 1 个故障日——每一个都是真故障，但频率不低；若之后出现「P1 天天响」要回来看是不是门槛太低。

## 不是什么
- **不改评分、不改任何检验口径**：只给每一天一个判定，读者各自决定怎么用。
- **不落盘新产物**：判定是 `.swarm_results_<日期>.json` 的纯函数，重放整天改写那份文件后判定自动跟着变（落一份副本会和它分叉）。
  扫描时算一次进 `scan_timing.extra.day_health`（→ status.json → `alert_manager._check_day_health` 出 P1）；前瞻检验加载器
  需要历史判定时直接对每个日期重算（`summarize_days`）。
- 判不了（结果文件不在 / 不可解析）⇒ `available=False` + 原因，**不当成 ok**。

## 前瞻检验的敏感性分析（修订 3，2026-10-22，用户决定：10-14 不排除、主分析不变）
维度 IC 与共振加成两份预注册检验，检视时在主分析**之外**并列报告一份「去掉 `source_outage` 日」的结果（`sensitivity_plan` 给出
哪些天去掉）。主分析的样本、判定、动作一概不变；敏感性不触发任何动作（协议 §7 的动作只看主分析）。
**规则被冻结**：「`source_outage` 日」的定义 = 本模块 `VERSION=1` + `MIN_OUTAGE_FIELDS=2` + `scan_coverage_gate.FIELDS` 登记时的内容，
其指纹 `definition_fingerprint()` 钉在 `REGISTERED_FINGERPRINT`。FIELDS 加 / 改 / 删字段、改阈值或分族 ⇒ 指纹变 ⇒ 执行器的敏感性块
报「定义已变，不可比」（**看得见，不是静默照算**）、`tests/test_day_health.py::TestSensitivityRuleIsFrozen` 变红——
要么新开一条修订，要么还原定义。⚠️ 指纹不覆盖闸的 `_present` / `_dig` 语义与结果文件布局，改它们同样要走修订。
**同日勘误（v0.45.608，2026-10-22，登记后数小时、10-14 结果仍不存在）**：二次检查发现定义漏了小样本下限——只有 1 只标的的结果文件里，
两个合法缺失的字段（无分析师覆盖 / 暂无催化剂）就是 0/1，被判 `source_outage`。补 `MIN_TICKERS=10`（并入指纹：`76bd554125c07a4e` → `38f150222c6a81d7`）。真实日子的影响（对 `.swarm_results_*.json` 现存 130 个文件逐日对照新旧定义）：**窗口内（2026-10-12 起）每天都是 30 只，判定不变，仍只有 2026-10-14 命中**；历史上有 12 天标的数 <10，新定义下判不了——其中 3 天（2026-06-15、07-28、07-30）原判 `source_outage`，回测命中数由 29 降为 26。（此处初版写成「现存 128 天逐日对照相同」，是错的，v0.45.608 当日更正。）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

#: 同一数据源族里降级字段数达到这个值 ⇒ 该源 `source_outage`
MIN_OUTAGE_FIELDS = 2
#: 判定所需的最少标的数。覆盖线 0.70 是按约 30 只设计的（「30 只里超过 9 只同时失败，只可能是上游整体挂了」）；
#: 样本太小时一次合法缺失（无分析师覆盖 / 暂无催化剂）就是 0/1，会把正常日判成故障日。（v0.45.608 二次检查补）
MIN_TICKERS = 10
#: 判定口径版本：改 `MIN_OUTAGE_FIELDS` / 分族规则 / 判定名即加一（读者据此知道历史判定是哪一版算的）
VERSION = 1

OK, DEGRADED, SOURCE_OUTAGE = "ok", "degraded", "source_outage"

#: 修订 3（2026-10-22）登记的「`source_outage` 日」定义指纹（见模块 docstring）。改它 = 改预注册，必须新开修订；
#: 同一个值另钉在 `tests/test_day_health.py` 与两份协议正文里——改这里而不改那两处，测试会红。
REGISTERED_FINGERPRINT = "38f150222c6a81d7"


def family(source: str) -> str:
    """`scan_coverage_gate.FIELDS[*]["source"]` 的第一个词：「yfinance 日K（hv_proxy 期）」→ `yfinance`。
    与闸里 `likely_network_layer` 的分族规则同一个（`r["source"].split()[0]`）。"""
    parts = str(source).split()
    return parts[0] if parts else "?"


def definition_fingerprint() -> str:
    """当前「`source_outage` 日」定义的指纹：判定版本 + 阈值 + 受监视字段（键 / 路径 / 记录键 / 覆盖线 / 源族）。"""
    import hashlib
    import scan_coverage_gate
    spec = {"day_health_version": VERSION, "min_outage_fields": MIN_OUTAGE_FIELDS, "min_tickers": MIN_TICKERS,
            "fields": sorted([f["key"], f["path"], f.get("recorded_key"), f["min_coverage"], family(f["source"])]
                             for f in scan_coverage_gate.FIELDS)}
    return hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def sensitivity_plan(summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """敏感性分析（修订 3）要去掉哪些天。输入 `summarize_days` 的结果。

    `{"available": True, "days": [...], "undetermined": n}` 或 `{"available": False, "reason": ...}`。
    判定不可得、或定义指纹与登记时不同 ⇒ 不可得 + 原因（**不静默退回照算**，也不静默当成「没有故障日」）。
    判不了的日子（结果文件不在 / 不可解析）不去掉（保守：主分析里有它就留着），数量单列。
    """
    if not isinstance(summary, dict) or not summary.get("available"):
        return {"available": False, "reason": f"日健康判定不可得：{(summary or {}).get('reason') if isinstance(summary, dict) else summary!r}"}
    try:
        fp = definition_fingerprint()
    except Exception as e:  # noqa: BLE001 —— 算不出指纹也要说出来
        return {"available": False, "reason": f"定义指纹算不出：{type(e).__name__}: {e}"}
    if fp != REGISTERED_FINGERPRINT:
        return {"available": False,
                "reason": f"日健康定义与修订 3 登记时不同（现 {fp} ≠ 登记 {REGISTERED_FINGERPRINT}）——不可比，须新开修订"}
    return {"available": True, "rule_version": VERSION, "n_days": summary.get("n_days"),
            "days": sorted(o["date"] for o in summary.get("outage") or []),
            "undetermined": len(summary.get("undetermined") or [])}


def sensitivity_block(dh: Optional[Dict[str, Any]], rows: List[Dict[str, Any]], run_on: Callable[[List[Dict[str, Any]]], Dict[str, Any]],
                      *, revealed: bool, counts: Callable[[Dict[str, Any], List[Dict[str, Any]]], Dict[str, Any]],
                      reveal: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """修订 3 的敏感性分析（两份前瞻检验共用的一份实现）：同一条判定链跑在「去掉 `source_outage` 日」的样本上，并列放在主分析旁边。

    `run_on(rows)`：执行器自己的判定链。`counts(结果, 子样本)`：**盲化期也可露出**的计数型键（不含任何检视结论）；
    `reveal(结果)`：只在主分析本次已给出结论（`revealed`）时才露出的键——**包括 `status`**：status 本身就是检视结论
    （confirmed / concluded），未露出时放进去等于盲化旁路（v0.45.608 二次检查）。
    **不改主分析、不触发任何动作。** 规则不可得 / 链抛异常 ⇒ `available=False` + 原因，看得见。
    窗口里没有故障日**且**没有判不了的日子才算 `same_as_primary`；有判不了的日子时不能说「相同」。
    """
    plan = sensitivity_plan(dh)
    if not plan["available"]:
        return plan
    days = set(plan["days"])
    base = {"available": True, "rule_version": plan["rule_version"], "excluded_days": plan["days"],
            "n_days": plan["n_days"], "n_undetermined_days": plan["undetermined"]}
    if not days:
        return {**base, "n_rows_removed": 0, "same_as_primary": plan["undetermined"] == 0}
    kept = [r for r in rows if r["date"] not in days]
    try:
        res_s = run_on(kept)
        out = {**base, "n_rows_removed": len(rows) - len(kept), **counts(res_s, kept)}
        if revealed:
            out.update(reveal(res_s))
    except Exception as e:  # noqa: BLE001 —— 敏感性自己坏了写原因，不拖垮主分析
        return {"available": False, "reason": f"敏感性分析自身失败：{type(e).__name__}: {e}"}
    return out


def describe_sensitivity(sb: Optional[Dict[str, Any]]) -> List[str]:
    """敏感性块的人读行（两份报告共用）。结论行（verdicts / status_line）由各执行器自己接在后面。"""
    if not sb:
        return []
    if not sb.get("available"):
        return [f"敏感性分析（修订 3）不可得：{sb.get('reason')}"]
    n_u, n_d = sb.get("n_undetermined_days") or 0, sb.get("n_days")
    note = f"；另有 {n_u}/{n_d if n_d is not None else '?'} 天判不了（结果文件不在 / 不可解析），不在去掉之列" if n_u else ""
    if not sb.get("excluded_days"):
        if sb.get("same_as_primary"):
            return ["敏感性分析（修订 3）：窗口内没有 source_outage 日，与主分析相同"]
        return [f"敏感性分析（修订 3）：未发现 source_outage 日{note}——不能说与主分析相同"]
    bits = [f"少 {sb.get('n_rows_removed')} 行"]
    if "h1_weeks" in sb:
        bits.append(f"H1 合格周 {sb['h1_weeks']}")
    if "weeks" in sb:
        bits.append(f"合格周 {sb['weeks']}/{sb.get('next_look_at')}")
    return [f"敏感性分析（修订 3，去掉 {', '.join(sb['excluded_days'])}；{'，'.join(bits)}，不触发任何动作）{note}"]


def classify(cov: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """`scan_coverage_gate.check()` 的返回值 → 日健康判定。纯函数，不读盘。"""
    if not isinstance(cov, dict) or not cov.get("determinable"):
        reason = (cov or {}).get("reason") if isinstance(cov, dict) else f"覆盖率结果不是 dict：{type(cov).__name__}"
        return {"available": False, "reason": reason or "覆盖率检查判不了（无原因）"}
    rows = cov.get("fields")
    if not isinstance(rows, list):
        return {"available": False, "reason": "覆盖率结果缺 fields 列表"}
    n = cov.get("tickers")
    if not isinstance(n, int) or n < MIN_TICKERS:
        return {"available": False,
                "reason": f"标的数 {n!r} < {MIN_TICKERS}：覆盖线按约 30 只设计，样本太小判不了（不当 ok，也不当故障日）"}

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
