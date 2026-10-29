#!/usr/bin/env python3
"""月度自诊断里的两节：「数据质量（本月）」与「预注册检验进度（只计数）」（v0.45.678）。

由来：记忆里定档的「9 月中旬大复盘」过期没结——它的前提（准确率可信）早被证伪，四个子项要么被预注册检验接手
（① 分段胜率 → 维度 IC；④ 纸面组合参数 → 策略层优势）、要么作废（③）、要么退役（② BullVeto，v0.45.677）。
用户 2026-10-29 定「重新定义」：不再设一次性定档复盘（前提被证伪后会无声挂着），改成每月 1 日自诊断里的常设两节。

两条不变式（守卫 `tests/test_monthly_review.py`）：

1. **不读收益 / 结果**。数据质量只看「取数成功没有」；进度只取各检验**自己的盲化接口**里的计数字段——
   某个检验已就绪时也只说「已就绪、去哪看」，不在月报里印效应量（月报后面还要让 Claude 提假说，等于把结果递到改评分的人手里）。
2. **取不到就写取不到**。每个来源单独兜住，失败写成「不可得：原因」行，不省略、不写成 0（「这个失败，下游怎么知道？」）。

只读：不写任何文件、不打网（策略层优势的回测会经 yf_daily 取 SPY 基准，那是它自己的取数）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ─────────────────────────────── 窗口

def report_month(today: date) -> Tuple[date, date]:
    """报告哪个月：昨天所在的自然月，截至昨天（每月 1 日跑 ⇒ 整个上月；月中手动跑 ⇒ 本月到昨天）。"""
    y = today - timedelta(days=1)
    return y.replace(day=1), y


def _today(today: Optional[date]) -> date:
    if today is not None:
        return today
    from hive_logger import pdt_today
    return date.fromisoformat(str(pdt_today())[:10])     # pdt_today() 给的是 "YYYY-MM-DD" 字符串


def _log_dir(log_dir) -> Path:
    """编排器日志目录（代码外、数据根外），调用时求值；同 `scan_coverage_gate.check_rate_limit` 的缺省。"""
    return Path(log_dir) if log_dir else Path.home() / ".claude" / "logs"


# ─────────────────────────────── 数据质量

def data_quality(today: Optional[date] = None, *, home=None, log_dir=None) -> Dict[str, Any]:
    """本月每个交易日：有没有扫描、覆盖率闸逐字段的缺失、日健康判定、yfinance 429 次数。整个函数不抛。"""
    try:
        import day_health
        import scan_coverage_gate
        from is_trading_day import is_trading_day

        start, end = report_month(_today(today))
        if home is None:
            from hive_logger import PATHS
            home = PATHS.home
        home = Path(home)
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        trading = [d.isoformat() for d in days if is_trading_day(d)[0]]

        fields: Dict[str, Dict[str, Any]] = {}
        missing_scan, undetermined, outage, degraded_days = [], [], [], []
        rl_total, rl_days, rl_unknown = 0, [], []
        for d in trading:
            path = home / f".swarm_results_{d}.json"
            if not path.exists():
                missing_scan.append(d)
                continue
            cov = scan_coverage_gate.check(d, path)
            v = day_health.classify(cov)
            if not v.get("available"):
                undetermined.append({"date": d, "reason": v.get("reason")})
            elif v["verdict"] == day_health.SOURCE_OUTAGE:
                outage.append({"date": d, "sources": {k: [f["field"] for f in fs] for k, fs in v["outage_sources"].items()}})
            elif v["verdict"] == day_health.DEGRADED:
                degraded_days.append(d)
            for r in (cov.get("fields") or []) if cov.get("determinable") else []:
                f = fields.setdefault(r["field"], {"field": r["field"], "source": r["source"], "degraded_days": [],
                                                   "missing": 0, "days": 0, "not_recorded_days": 0, "worst": None})
                if r.get("not_recorded"):
                    f["not_recorded_days"] += 1
                    continue
                f["days"] += 1
                miss = (r.get("total") or 0) - (r.get("have") or 0)
                f["missing"] += miss
                if r.get("degraded"):
                    f["degraded_days"].append(d)
                if miss and (f["worst"] is None or miss > f["worst"]["missing"]):
                    f["worst"] = {"date": d, "have": r.get("have"), "total": r.get("total"), "missing": miss}
            rl = scan_coverage_gate.check_rate_limit(d, str(_log_dir(log_dir)))
            if rl.get("determinable"):
                rl_total += rl.get("count") or 0
                if rl.get("count"):
                    rl_days.append((d, rl["count"]))
            else:
                rl_unknown.append(d)
        return {"available": True, "start": start.isoformat(), "end": end.isoformat(),
                "trading_days": len(trading), "scanned": len(trading) - len(missing_scan),
                "missing_scan": missing_scan, "outage": outage, "degraded_days": degraded_days,
                "undetermined": undetermined,
                "fields": sorted(fields.values(), key=lambda f: (-f["missing"], -len(f["degraded_days"]), f["field"])),
                "rate_limit": {"total": rl_total, "days": rl_days, "unknown_days": rl_unknown}}
    except Exception as e:  # noqa: BLE001 —— 观测点自身失败要写进月报，不能拖垮整份月报，也不能写成「无降级」
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}


def _md(d: str) -> str:
    return d[5:]


def render_data_quality(dq: Dict[str, Any]) -> List[str]:
    lines = ["## 三.五、数据质量（本月，不看收益）", ""]
    if not dq.get("available"):
        return lines + [f"> ⚠️ **不可得**：{dq.get('reason')}——本节没算出来，不代表本月没有降级。", ""]
    lines += [
        f"**窗口**：{dq['start']} ~ {dq['end']}　只统计取数成功与否，不涉及任何预测结果，不受预注册盲化约束。",
        "",
        f"- 交易日 {dq['trading_days']} 天：有扫描 {dq['scanned']} 天"
        + (f"，**漏扫 {len(dq['missing_scan'])} 天**（{'、'.join(_md(d) for d in dq['missing_scan'])}）" if dq["missing_scan"] else "，无漏扫"),
    ]
    out = "；".join(f"{_md(o['date'])}（{'、'.join(f'{k}×{len(v)}' for k, v in o['sources'].items())}）" for o in dq["outage"])
    lines.append(f"- 日健康：数据源故障日 {len(dq['outage'])} 天" + (f"：{out}" if out else "")
                 + f"；降级未达故障线 {len(dq['degraded_days'])} 天"
                 + (f"；**判不了 {len(dq['undetermined'])} 天**（{_md(dq['undetermined'][0]['date'])}：{dq['undetermined'][0]['reason']}）"
                    if dq["undetermined"] else ""))
    rl = dq["rate_limit"]
    rl_txt = f"- yfinance 429（编排器日志）：合计 {rl['total']} 次"
    if rl["days"]:
        top = sorted(rl["days"], key=lambda x: -x[1])[:3]
        rl_txt += f"，最多的 {'、'.join(f'{_md(d)} {n} 次' for d, n in top)}"
    if rl["unknown_days"]:
        rl_txt += f"；{len(rl['unknown_days'])} 天没有日志可数（已清理或没跑）"
    lines += [rl_txt, "",
              "按缺失的（日·票）数排序（覆盖率闸 `scan_coverage_gate.FIELDS` 的字段；降级 = 当天覆盖率低于该字段的覆盖线）：", "",
              "| 字段 | 数据源 | 缺失（日·票） | 降级天数 | 最差一天 |",
              "|------|--------|--------------|---------|---------|"]
    for f in dq["fields"]:
        worst = f"{_md(f['worst']['date'])}（{f['worst']['have']}/{f['worst']['total']}）" if f["worst"] else "—"
        deg = f"{len(f['degraded_days'])}" + (f"（{'、'.join(_md(d) for d in f['degraded_days'][:4])}{'…' if len(f['degraded_days']) > 4 else ''}）"
                                               if f["degraded_days"] else "")
        nr = f"；{f['not_recorded_days']} 天未记录" if f["not_recorded_days"] else ""
        lines.append(f"| {f['field']} | {f['source']} | {f['missing']} | {deg}{nr} | {worst} |")
    lines.append("")
    return lines


# ─────────────────────────────── 预注册检验进度（只计数）

def _readiness_json(log_dir, today: date) -> Tuple[Path, Dict[str, Any]]:
    """编排器 Step 11 每天写的 `ic_rerun_readiness-<日期>.json`，取不晚于今天的最新一份。"""
    files = sorted(p for p in _log_dir(log_dir).glob("ic_rerun_readiness-*.json")
                   if p.stem[len("ic_rerun_readiness-"):] <= today.isoformat())
    if not files:
        raise FileNotFoundError(f"{_log_dir(log_dir)} 里没有 ic_rerun_readiness-*.json（Step 11 没跑过或日志已清理）")
    return files[-1], json.loads(files[-1].read_text(encoding="utf-8"))


def _from_readiness(key: str) -> Callable[..., Dict[str, Any]]:
    def _row(log_dir, today: date) -> Dict[str, Any]:
        p, d = _readiness_json(log_dir, today)
        seg = d.get(key)
        if not isinstance(seg, dict) or not seg.get("line"):
            raise KeyError(f"{p.name} 里没有 {key}.line")
        return {"line": seg["line"], "as_of": p.stem[len("ic_rerun_readiness-"):]}
    return _row


def _ic_rerun(log_dir, today: date) -> Dict[str, Any]:
    p, d = _readiness_json(log_dir, today)
    line = (f"{d.get('weeks_accrued')}/{d.get('weeks_required')} 个合格周"
            + (f"，预计 {d['eta_date']}" if d.get("eta_date") else "")
            + ("（已就绪）" if d.get("ready") else ""))
    return {"line": line, "as_of": p.stem[len("ic_rerun_readiness-"):]}


def _strategy_edge(log_dir, today: date) -> Dict[str, Any]:
    from experiments import strategy_edge_test as se
    r = se.assess(as_of=today)
    if r.get("status") == "undetermined":
        raise RuntimeError(f"无法判定：{r.get('reason')}")
    head = f"世代 {r['cohort']['date']} 起 {r['n_units']}/{r['n_units_required']} 周，{r['n_trades']} 笔"
    if r.get("status") == "ready":       # 盲化：月报只说已就绪，不印 assess 算出的效应量
        return {"line": f"{head}——**已就绪**，结果看 `experiments/strategy_edge_test.py`"}
    return {"line": f"{head}（效应量盲化中）"}


def _sell_strike(log_dir, today: date) -> Dict[str, Any]:
    """**不读**：卖权数据在火墙里——只许日报钩子 / MCP / Alpha Bot 服务层 import 卖权模块，连 `sell_strike_state`
    目录也只许卖权模块与定义 / 备份 / 迁移引用（`tests/test_sell_strike_integration.py` 两道火墙）。月报后面要让
    Claude 提评分假说，正是火墙防的那条路，所以这里只指路、不取数；要在月报里看计数得先改火墙白名单（用户定）。"""
    return {"line": "在卖权火墙内，月报不读——进度看 Alpha Bot「账本就绪度」页或当天的卖权本地报告"}


def _straddle_gex(log_dir, today: date) -> Dict[str, Any]:
    import straddle_gex_prereg as sg
    p = sg.progress(sg._load_rows())
    inf, need = p["n_informative"], p["need"]
    return {"line": (f"信号 {p['n_events']}，有 GEX {p['n_with_gex']}，已结算合格 {p['n_eligible_settled']}/{need['min_units']}；"
                     f"信息块里正 / 负 GEX {inf['positive_gex']}/{inf['negative_gex']}（各需 ≥{need['min_per_group']}），"
                     f"块 {inf['blocks']}/{need['min_informative_blocks']}"
                     + ("；**已就绪**" if p["ready"] else "") + ("；已冻结" if p["frozen"] else ""))}


def _strangle_shadow(log_dir, today: date) -> Dict[str, Any]:
    import straddle_strangle_shadow as ss
    p = ss.progress(ss._load(None, "closed_trades.jsonl"), ss._load(None, "positions.jsonl"))
    need = p["need"]
    return {"line": (f"合格单位 {p['n_units']}/{need['min_units']}，块 {p['n_blocks']}/{need['min_blocks']}；"
                     f"已平仓 {p['n_closed']}、在途有影子记录 {p['n_open_recorded']}"
                     + ("；**已就绪**" if p["ready"] else "") + ("；已冻结" if p["frozen"] else ""))}


def _llm_shadow(log_dir, today: date) -> Dict[str, Any]:
    import hive_logger
    import llm_shadow_report as lr
    from llm_shadow_scan import default_shadow_root
    rep = lr.build_report(default_shadow_root(hive_logger.resolve_data_root()), Path(hive_logger.PATHS.db),
                          today=today.isoformat())
    d, t = rep["describe"], rep["thresholds"]
    miss = d.get("missing_run_days") or []
    return {"line": (f"{d.get('generation') or '—'}：健康日 {d['healthy_days']}/{t['min_healthy_days']}，"
                     f"方向不一致且 T+7 已出 {rep['discordant_decided']}/{t['min_discordant']}"
                     + (f"；生产有扫描而没跑 {len(miss)} 天" if miss else "")
                     + f"；累计花费 ${d['spend_usd']:.2f}"
                     + ("；**已就绪**，结果看 `llm_shadow_report.py`" if rep["ready"] else ""))}


#: (名称, 取进度的函数)。函数只许返回 {"line", 可选 "as_of"}；**不许**带出任何效应量 / 盈亏 / 命中率（盲化）。
PROGRESS_SOURCES: List[Tuple[str, Callable[..., Dict[str, Any]]]] = [
    ("维度 IC（H1 / H2）", _from_readiness("dim_ic_forward_test")),
    ("策略层优势", _strategy_edge),
    ("共振加成", _from_readiness("resonance_forward_test")),
    ("F&G 敞口门", _from_readiness("fg_exposure_gate_forward_test")),
    ("卖权路由", _sell_strike),
    ("跨式 × GEX", _straddle_gex),
    ("宽跨影子", _strangle_shadow),
    ("LLM 影子 A/B", _llm_shadow),
    ("IC 重跑就绪度（非预注册，攒样本的闸）", _ic_rerun),
]


#: 就绪度文件比报告日早这么多个日历日 ⇒ 标「陈旧」（Step 11 每个扫描日都写；长周末 + 1 天余量）
STALE_DAYS = 4


def prereg_progress(today: Optional[date] = None, *, log_dir=None, sources=None) -> List[Dict[str, Any]]:
    """逐个检验取进度；某个取不到 ⇒ 那一行写原因，其余照取。"""
    t = _today(today)
    rows = []
    for name, fn in (sources if sources is not None else PROGRESS_SOURCES):
        try:
            r = fn(log_dir, t)
            as_of = r.get("as_of")
            stale = bool(as_of) and (t - date.fromisoformat(as_of)).days > STALE_DAYS
            rows.append({"name": name, "line": str(r["line"]), "as_of": as_of, "stale": stale, "error": None})
        except Exception as e:  # noqa: BLE001 —— 一个检验取不到不拖垮其余行，但要写出来
            rows.append({"name": name, "line": None, "as_of": None, "stale": False, "error": f"{type(e).__name__}: {e}"})
    return rows


def render_progress(rows: List[Dict[str, Any]]) -> List[str]:
    n_err = sum(1 for r in rows if r["error"])
    lines = ["## 三.六、预注册检验进度（只计数）", "",
             "> 各检验自己的盲化接口给的计数：**不含任何效应量**，也不要从计数里推断效应。"
             "已就绪的检验按各自协议跑一次、看各自的报告，不在本月报里看。", ""]
    if n_err:
        lines += [f"> ⚠️ {n_err} 项取不到进度（见表），不代表那几项没有进展。", ""]
    lines += ["| 检验 | 进度 |", "|------|------|"]
    for r in rows:
        if r["error"]:
            txt = f"⚠️ 不可得：{r['error']}"
        else:
            txt = r["line"]
            if r.get("as_of"):
                txt += (f"（⚠️ 陈旧：最新就绪度停在 {r['as_of']}，Step 11 近几个扫描日没写出来？）" if r.get("stale")
                        else f"（{_md(r['as_of'])} 的就绪度）")
        lines.append(f"| {r['name']} | {txt.replace('|', '／')} |")
    lines.append("")
    return lines
