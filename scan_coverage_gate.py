#!/usr/bin/env python3
"""
🐝 Alpha Hive — 扫描字段覆盖率闸 (v0.45.42)
===========================================
把「这次扫描的数据是不是大面积没抓到」从**事后翻 JSON 才发现**，
变成扫完当场可判。

为什么需要
----------
2026-08-26 14:10 那次扫描，yfinance 全线返回空：

    rv_30d        30/30 → 1/30
    iv_rank       29/30 → 1/30
    iv_rv_spread  30/30 → 1/30
    ChronosBee    详情整个是 {}（催化剂全丢）

而**扫描退出码是 0，日报照常生成、照常推 Slack、照常上站**。
每个字段的失败都被 `except → return _empty` 老实接住了，没有一个是 bug；
问题是**没有任何一层在看"老实降级"发生了多少次**。
这正是 MEMORY「静默降级三件套」记的第三条：编排器只看退出码。

它不做什么
----------
- 不发通知（与 scan_continuity 一致，通知与否由编排器决定）
- 不阻止报告生成 —— 数据缺失是事实，报告该出还得出，只是必须**可见**
- 不重试取数 —— 重试是取数层的事（http_gate），这里只负责报告事实

退出码
------
    0  覆盖率健康
    1  检出降级（某字段覆盖率低于阈值）
    3  无法判定（结果文件不存在/不可解析）—— 3 而非 2，编排器把 2 留给"脚本不存在"

用法
----
    /usr/local/bin/python3 scan_coverage_gate.py                    # 判当日
    /usr/local/bin/python3 scan_coverage_gate.py --date 2026-08-26
    /usr/local/bin/python3 scan_coverage_gate.py --quiet --out cov.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).parent

# ── 受监视字段 ──────────────────────────────────────────────────────
# min_coverage：低于该比例即判降级。
# 阈值不是「可接受的坏」，是「低于此必然是系统性故障而非个别标的没数据」。
# 定在 0.70：单只标的偶发取数失败很常见（新上市/停牌/低流动性），
# 但 30 只里超过 9 只同时失败，只可能是上游整体挂了。
FIELDS: List[Dict[str, Any]] = [
    {"key": "rv_30d", "path": "OracleBeeEcho.rv_30d",
     "min_coverage": 0.70, "source": "yfinance 日K",
     "note": "IV Rank(hv_proxy) 与 IV-RV 价差都由它派生，丢它等于丢三个指标"},
    {"key": "iv_rank", "path": "OracleBeeEcho.iv_rank",
     "min_coverage": 0.70, "source": "yfinance 日K（hv_proxy 期）",
     "note": "ML 模型头号特征（实测 importance 0.267）"},
    {"key": "iv_current", "path": "OracleBeeEcho.iv_current",
     "min_coverage": 0.70, "source": "CBOE 期权链",
     "note": "走 CBOE，与上面两项**不同源**——同时挂说明是网络层而非单一数据源"},
    {"key": "iv_skew_ratio", "path": "OracleBeeEcho.iv_skew_ratio",
     "min_coverage": 0.70, "source": "CBOE 期权链"},
    {"key": "put_call_ratio", "path": "OracleBeeEcho.put_call_ratio",
     "min_coverage": 0.70, "source": "CBOE 期权链"},
    {"key": "iv_rv_spread", "path": "OracleBeeEcho.iv_rv_spread",
     "min_coverage": 0.70, "source": "yfinance 日K（派生自 rv_30d）"},
    {"key": "catalysts", "path": "ChronosBeeHorizon.catalysts",
     "min_coverage": 0.40, "source": "yfinance 财报日历",
     "note": "阈值低于其余项：并非每只标的在任意时点都有已知催化剂，"
             "0.40 是经验下限（8/25 实测 21/30=0.70）"},
]


def _dig(tr: Dict, dotted: str) -> Any:
    """agent_details.<Agent>.details.<field> 的简写路径"""
    agent, field = dotted.split(".", 1)
    det = ((tr.get("agent_details") or {}).get(agent) or {}).get("details") or {}
    return det.get(field)


def _present(v: Any) -> bool:
    """有值 = 非 None、非空容器。0 与 False 算有值（它们是合法读数）"""
    if v is None:
        return False
    if isinstance(v, (list, dict, str)):
        return len(v) > 0
    return True


def check(date: str, results_path: Optional[Path] = None) -> Dict[str, Any]:
    path = results_path or (ROOT / f".swarm_results_{date}.json")
    if not path.exists():
        return {"date": date, "determinable": False,
                "reason": f"结果文件不存在：{path.name}"}
    try:
        results = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        return {"date": date, "determinable": False,
                "reason": f"结果文件不可解析：{type(e).__name__}: {e}"}
    if not isinstance(results, dict) or not results:
        return {"date": date, "determinable": False,
                "reason": "结果文件为空或格式异常"}

    n = len(results)
    rows = []
    for spec in FIELDS:
        have = sum(1 for tr in results.values()
                   if isinstance(tr, dict) and _present(_dig(tr, spec["path"])))
        cov = have / n if n else 0.0
        rows.append({
            "field": spec["key"], "source": spec["source"],
            "have": have, "total": n, "coverage": round(cov, 4),
            "min_coverage": spec["min_coverage"],
            "degraded": cov < spec["min_coverage"],
            "note": spec.get("note", ""),
        })

    degraded = [r for r in rows if r["degraded"]]
    # 多个**不同数据源**同时降级 ⇒ 大概率是网络/闸门层，不是某个源挂了
    srcs = {r["source"].split()[0] for r in degraded}
    return {
        "date": date, "determinable": True, "tickers": n,
        "healthy": not degraded, "fields": rows,
        "degraded_fields": [r["field"] for r in degraded],
        "likely_network_layer": len(srcs) > 1,
    }


# ── 价格可信度交叉核验（v0.45.45，需网络，默认不跑）─────────────────
# 2026-08-26 实测暴露两种独立的 price_at_predict 污染：
#   ① **补跑窗口漂移**：为业务日 D 重跑扫描，但运行时刻已经越过 D 的交易时段，
#      现拉的价格早已不代表 D。实测 23:57 PDT（= 8/27 ET 凌晨 2:57）重跑，
#      NVDA 写进 219.53 而 8/26 真实收盘是 209.66（+4.71%），30 只里 8 只 >1%。
#      期权快照因为是**冻结**的反而干净（中位 0.14% vs DB 0.36%）——
#      「冻结」在这件事上是防护而不是风险。
#   ② **数据源单点乱码**：CRM 8/26 写进 232.93，而其近一个月最高仅 209.17，
#      且同一份快照的期权支撑位在 160/190（对应 ~$205 的股票）。
#      该值在任何日期都不存在，是 CBOE 当次读数本身坏了。8/24、8/25 都正确。
# 两者都只能靠**与外部收盘价对照**发现——内部自洽性检查抓不到。
_PRICE_DEV_WARN_PCT = 1.0    # 单只偏差超过即列出
_PRICE_DEV_BAD_PCT = 5.0     # 超过即判定为坏读数（几乎不可能是正常时点差）


def check_prices(date: str, db_path: str = "pheromone.db") -> Dict[str, Any]:
    """把 predictions.price_at_predict 与该日真实收盘对照。

    需要网络。取不到收盘价时返回 determinable=False —— 不猜。
    """
    import os
    import sqlite3
    if not os.path.exists(db_path):
        return {"determinable": False, "reason": f"{db_path} 不存在"}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db = {t: p for t, p in conn.execute(
        "SELECT ticker, price_at_predict FROM predictions WHERE date=?", (date,))}
    if not db:
        return {"determinable": False, "reason": f"{date} 无预测记录"}
    try:
        import warnings as _w
        _w.filterwarnings("ignore")
        import yfinance as yf
        from datetime import date as _d, timedelta as _td
        _s = _d.fromisoformat(date) - _td(days=4)
        _e = _d.fromisoformat(date) + _td(days=2)
        hist = yf.download(sorted(db), start=_s.isoformat(), end=_e.isoformat(),
                           interval="1d", progress=False, auto_adjust=False,
                           group_by="column")
        cl = hist["Close"]
        idx = [d for d in cl.index if str(d)[:10] == date]
        if not idx:
            return {"determinable": False, "reason": f"取不到 {date} 的收盘价"}
        row = cl.loc[idx[0]]
    except Exception as e:  # noqa: BLE001
        return {"determinable": False, "reason": f"收盘价取数失败：{type(e).__name__}: {e}"}

    devs, warn, bad = [], [], []
    for tk, px in db.items():
        try:
            real = float(row[tk])
        except Exception:  # noqa: BLE001
            continue
        if real != real or real <= 0 or not px:
            continue
        d = (px - real) / real * 100
        devs.append(abs(d))
        rec = {"ticker": tk, "recorded": round(px, 2),
               "actual_close": round(real, 2), "deviation_pct": round(d, 2)}
        if abs(d) >= _PRICE_DEV_BAD_PCT:
            bad.append(rec)
        elif abs(d) >= _PRICE_DEV_WARN_PCT:
            warn.append(rec)
    if not devs:
        return {"determinable": False, "reason": "无可比对样本"}
    devs.sort()
    return {
        "determinable": True, "checked": len(devs),
        "median_dev_pct": round(devs[len(devs) // 2], 3),
        "max_dev_pct": round(devs[-1], 2),
        "warn": sorted(warn, key=lambda r: -abs(r["deviation_pct"])),
        "bad": sorted(bad, key=lambda r: -abs(r["deviation_pct"])),
        "healthy": not bad,
    }


def _render_prices(pr: Dict[str, Any]) -> str:
    if not pr.get("determinable"):
        return f"⚠️  价格核验无法判定：{pr.get('reason')}"
    out = [f"价格核验 · {pr['checked']} 只 · 中位偏差 {pr['median_dev_pct']}% · "
           f"最大 {pr['max_dev_pct']}%"]
    for r in pr["bad"]:
        out.append(f"  ❌ {r['ticker']:6} 记录 {r['recorded']:>9.2f}  实际收盘 "
                   f"{r['actual_close']:>9.2f}  {r['deviation_pct']:+.2f}%  ← 坏读数")
    for r in pr["warn"]:
        out.append(f"  ⚠️  {r['ticker']:6} 记录 {r['recorded']:>9.2f}  实际收盘 "
                   f"{r['actual_close']:>9.2f}  {r['deviation_pct']:+.2f}%")
    out.append("✅ 价格可信" if pr["healthy"] else "❌ 检出坏价格 —— 这些标的的入场价不可用于收益计算")
    return "\n".join(out)


def _render(res: Dict[str, Any]) -> str:
    if not res.get("determinable"):
        return f"⚠️  无法判定 {res['date']}：{res['reason']}"
    out = [f"扫描字段覆盖率 · {res['date']} · {res['tickers']} 只标的"]
    for r in res["fields"]:
        mark = "❌" if r["degraded"] else "✅"
        out.append(f"  {mark} {r['field']:16} {r['have']:2}/{r['total']:2} "
                   f"({r['coverage']*100:5.1f}%  闸 {r['min_coverage']*100:.0f}%)  ← {r['source']}")
        if r["degraded"] and r["note"]:
            out.append(f"       ↳ {r['note']}")
    if res["healthy"]:
        out.append("✅ 覆盖率健康")
    else:
        out.append(f"❌ 降级字段：{', '.join(res['degraded_fields'])}")
        if res["likely_network_layer"]:
            out.append("   多个**不同数据源**同时降级 ⇒ 疑为网络/闸门层故障，"
                       "而非单一数据源不可用")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描字段覆盖率闸")
    ap.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD（默认 PDT 当日）")
    ap.add_argument("--file", default=None, help="直接指定 .swarm_results_*.json")
    ap.add_argument("--quiet", action="store_true", help="只输出结论行")
    ap.add_argument("--out", default=None, help="把完整结果写成 JSON")
    ap.add_argument("--check-prices", action="store_true",
                    help="额外核验 price_at_predict 与真实收盘（需网络，较慢）")
    args = ap.parse_args()

    date = args.date
    if not date:
        try:
            from timezone_utils import pdt_today
            date = pdt_today().isoformat()
        except Exception:  # noqa: BLE001
            from datetime import date as _d
            date = _d.today().isoformat()

    res = check(date, Path(args.file) if args.file else None)

    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2))
    if not args.quiet:
        print(_render(res))
    elif not res.get("determinable"):
        print(f"⚠️  {res['reason']}")
    elif not res["healthy"]:
        print(f"❌ {res['date']} 降级字段：{', '.join(res['degraded_fields'])}")

    if args.check_prices:
        pr = check_prices(date)
        res["price_check"] = pr
        if args.out:
            Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2))
        if not args.quiet:
            print()
            print(_render_prices(pr))
        elif pr.get("determinable") and not pr["healthy"]:
            print(f"❌ {date} 坏价格：{', '.join(r['ticker'] for r in pr['bad'])}")
        # 坏价格与字段缺失同级：都判 1（检出降级）
        if pr.get("determinable") and not pr["healthy"]:
            return 1

    if not res.get("determinable"):
        return 3
    return 0 if res["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
