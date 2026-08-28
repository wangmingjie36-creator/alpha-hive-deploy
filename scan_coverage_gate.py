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
import re
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


# ── 来源标签诚实度（v0.45.53）──────────────────────────────────────
# 静态检查判不了「一个来源标签是不是在撒谎」：`"data_quality": "fallback"`
# 写在 except 块里本来就诚实，写在成功路径上才可疑 —— 同一行字面量，
# 诚实与否**取决于它在哪条分支上**，那是语义不是语法。
# （实测：全仓 98 处字面量来源标签、175 处结果词字面量，静态筛完全是噪音。）
#
# 但运行时判得了，而且判据很硬：
#
#     标签宣称取数成功 ⇒ 它管辖的值必须非空。
#
# 违反即矛盾 —— 要么标签在撒谎，要么值被谁清掉了，两种都该查。
# 这正是 2026-08-27 审计里 `"momentum": "real"` 那一类的运行时对应物：
# 当时动量兜底成 0.0，而质量标签硬编码自称 real。
_SUCCESS_LABELS = {"real", "cboe", "yfinance", "live", "verified",
                   "cboe_close", "cboe_intraday", "yfinance+fred"}

# (标签字段, 它管辖的值字段, 说明)
_LABEL_GOVERNS = [
    ("OracleBeeEcho.iv_rank_source", "OracleBeeEcho.iv_rank",
     "iv_rank_source 宣称有来源，iv_rank 却为空"),
    ("OracleBeeEcho.data_quality", "OracleBeeEcho.iv_current",
     "期权链标 real，iv_current 却为空"),
    ("OracleBeeEcho.data_quality", "OracleBeeEcho.put_call_ratio",
     "期权链标 real，put_call_ratio 却为空"),
]


def check_label_honesty(date: str, results_path: Optional[Path] = None) -> Dict[str, Any]:
    """核对来源标签与它管辖的值是否自洽。纯离线，读扫描结果即可。"""
    path = results_path or (ROOT / f".swarm_results_{date}.json")
    if not path.exists():
        return {"determinable": False, "reason": f"结果文件不存在：{path.name}"}
    try:
        results = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        return {"determinable": False, "reason": f"不可解析：{type(e).__name__}: {e}"}
    if not isinstance(results, dict) or not results:
        return {"determinable": False, "reason": "结果为空"}

    contradictions = []
    checked = 0
    for tk, tr in results.items():
        if not isinstance(tr, dict):
            continue
        for label_path, value_path, why in _LABEL_GOVERNS:
            label = _dig(tr, label_path)
            if not isinstance(label, str) or label.lower() not in _SUCCESS_LABELS:
                continue          # 标签本身就说降级 ⇒ 诚实，跳过
            checked += 1
            if not _present(_dig(tr, value_path)):
                contradictions.append({
                    "ticker": tk, "label_field": label_path, "label": label,
                    "value_field": value_path, "why": why,
                })
    return {
        "determinable": True, "checked": checked,
        "contradictions": contradictions,
        "healthy": not contradictions,
    }


def _render_label_honesty(r: Dict[str, Any]) -> str:
    if not r.get("determinable"):
        return f"⚠️  来源标签核对无法判定：{r.get('reason')}"
    if r["healthy"]:
        return f"来源标签核对 · {r['checked']} 项宣称成功 · ✅ 全部与值自洽"
    out = [f"来源标签核对 · {r['checked']} 项宣称成功 · "
           f"❌ {len(r['contradictions'])} 处矛盾"]
    for c in r["contradictions"][:10]:
        out.append(f"  ❌ {c['ticker']:6} {c['label_field']}={c['label']!r} "
                   f"但 {c['value_field'].split('.')[-1]} 为空 —— {c['why']}")
    return "\n".join(out)


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


def check_rate_limit(date: str, log_dir: str = "") -> Dict[str, Any]:
    """数当日编排器日志里的 yfinance 429，回答「为什么降级」而不只是「降了什么」。

    v0.45.56 起。8/27 的覆盖率报告准确列出了 rv_30d/iv_rank/iv_rv_spread/
    catalysts 各 0/30 —— 但**没说是被限流打空的**，于是那份报告读起来像
    「yfinance 今天没数据」，而真相是「我们把 yfinance 打到拒绝服务」。
    两者的修法完全相反：前者等它恢复，后者必须自己降速。

    证据本来就躺在日志里，不需要新管道：数一下就是了。

    阈值 100 是经验刻度而非实测最优（⚠️ 待验证）：已知 8/25=364 次时数据仍
    全须全尾、8/27=687 次时全空，真正的临界点在两者之间，尚未定位。
    """
    _dir = Path(log_dir) if log_dir else Path.home() / ".claude" / "logs"
    log = _dir / f"orchestrator-{date}.log"
    if not log.exists():
        return {"determinable": False, "reason": f"无 {log.name}", "healthy": True}
    try:
        txt = log.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return {"determinable": False, "reason": f"日志读取失败: {e}", "healthy": True}

    n = txt.count("Too Many Requests")
    # 首末时刻：限流是"一阵子"还是"全程"，决定它是否吃掉了整轮扫描
    stamps = re.findall(r"^(\d{2}:\d{2}:\d{2}).*Too Many Requests", txt, re.M)
    return {
        "determinable": True,
        "count": n,
        "first": stamps[0] if stamps else None,
        "last": stamps[-1] if stamps else None,
        "threshold": 100,
        "healthy": n < 100,
    }


def _rate_limit_verdict(rl: Dict[str, Any], fields_healthy: bool) -> str:
    """限流量高**且**字段降了 = 病因；限流量高但字段还全 = 预警。

    两者必须分开说。8/25 实测 364 次限流、数据却全须全尾；8/27 是 687 次、
    全空。把前者也写成「降级的直接原因」是假话 —— 那天没有降级。
    但它同样该报：赤字是那时开始攒的，三天后才还。
    """
    if rl["healthy"]:
        return ""
    if fields_healthy:
        return ("⚠️ 本轮数据仍是全的，但限流量已越闸 —— 这是**早期预警**："
                "8/25=364 次时数据还全，8/27=687 次时全空。")
    return ("yfinance 限流是本轮降级的直接原因；重试会加深它，"
            "应下调 resilience.yfinance_limiter 速率。")


def _render_rate_limit(r: Dict[str, Any], fields_healthy: bool = False) -> str:
    if not r.get("determinable"):
        return f"⚠️  限流检查无法判定：{r['reason']}"
    if r["healthy"]:
        return f"✅ yfinance 限流 {r['count']} 次（闸 {r['threshold']}）"
    return (f"❌ yfinance 限流 {r['count']} 次（闸 {r['threshold']}），"
            f"{r['first']}–{r['last']}\n       ↳ "
            + _rate_limit_verdict(r, fields_healthy))


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描字段覆盖率闸")
    ap.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD（默认 PDT 当日）")
    ap.add_argument("--file", default=None, help="直接指定 .swarm_results_*.json")
    ap.add_argument("--quiet", action="store_true", help="只输出结论行")
    ap.add_argument("--out", default=None, help="把完整结果写成 JSON")
    ap.add_argument("--log-dir", default="",
                    help="编排器日志目录（默认 ~/.claude/logs）；限流检查从这里读")
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

    # v0.45.54 二次检查：`--out` 原先在这里就写盘，而 label_honesty / price_check
    # 是之后才算出来的 —— 默认路径（编排器用的 `--quiet --out`）写出的 JSON
    # **缺 label_honesty 段**，下游永远看不到这项检查的结果。
    # 现统一挪到全部检查之后写一次。
    if not args.quiet:
        print(_render(res))
    elif not res.get("determinable"):
        print(f"⚠️  {res['reason']}")
    elif not res["healthy"]:
        print(f"❌ {res['date']} 降级字段：{', '.join(res['degraded_fields'])}")

    lh = check_label_honesty(date, Path(args.file) if args.file else None)
    res["label_honesty"] = lh
    if not args.quiet:
        print()
        print(_render_label_honesty(lh))
    elif lh.get("determinable") and not lh["healthy"]:
        print(f"❌ {date} 来源标签矛盾 {len(lh['contradictions'])} 处")

    # 限流检查是**诊断项，不进退出码**：
    #   · 字段真降了 → 退出码已经是 1，限流只是补上「为什么」
    #   · 字段还全但 429 越闸 → 那是早期预警（8/25 实测 364 次、数据全须全尾），
    #     把它判成失败会让编排器对一次成功的扫描报错
    # 它的价值在于**回答病因**，不在于多亮一盏红灯。
    rl = check_rate_limit(date, args.log_dir)
    _fields_ok = bool(res.get("healthy"))
    if rl.get("determinable"):
        rl["verdict"] = _rate_limit_verdict(rl, _fields_ok)
    res["rate_limit"] = rl
    if not args.quiet:
        print()
        print(_render_rate_limit(rl, _fields_ok))
    elif rl.get("determinable") and not rl["healthy"]:
        _tail = "降级的直接原因" if not _fields_ok else "早期预警（本轮数据仍全）"
        print(f"❌ {date} yfinance 限流 {rl['count']} 次 —— {_tail}")

    if args.check_prices:
        pr = check_prices(date)
        res["price_check"] = pr
        if not args.quiet:
            print()
            print(_render_prices(pr))
        elif pr.get("determinable") and not pr["healthy"]:
            print(f"❌ {date} 坏价格：{', '.join(r['ticker'] for r in pr['bad'])}")

    # ── v0.45.54 二次检查：写盘与退出码都收敛到这里 ──
    # 原先坏价格那条是**提前 return 1**，会绕过写盘 —— 于是「检出问题」的那次
    # 恰好是 --out 拿不到 JSON 的那次，下游想查都查不了。
    # 退出码语义（与 scan_continuity 一致）：0 健康 / 1 检出降级 / 3 无法判定。
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2))

    if not res.get("determinable"):
        return 3
    _degraded = (
        (not res["healthy"])
        or (lh.get("determinable") and not lh["healthy"])
        or (res.get("price_check", {}).get("determinable")
            and not res.get("price_check", {}).get("healthy", True))
    )
    return 1 if _degraded else 0


if __name__ == "__main__":
    sys.exit(main())
