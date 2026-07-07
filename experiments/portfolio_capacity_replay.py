"""P3 (v0.38.2): 组合资金利用率参数网格回放 — 只出报告，不改生产配置

背景：信号层 288 个可执行方向单胜率 55.9%、单均 +2.82%，但组合 4 个月仅
+1.38%——根因是资金利用率（单笔 1.5-2.5% NAV、开仓门槛 6.5 严于信号口径
6.0、在场上限 30% 且常年只用到个位数）。本实验在沙盒里全历史重放 36 个
参数组合，按 Calmar（年化/最大回撤）找性价比拐点。

网格：entry_score_bull {6.5, 6.0} × 仓位倍数 {×1, ×2, ×3}
      × max_deployed_pct {30, 60, 80} × tp_pct {10, 15}

运行：/usr/local/bin/python3 experiments/portfolio_capacity_replay.py
输出：experiments/portfolio_capacity_report.md
"""
from __future__ import annotations

import json
import math
import shutil
import statistics
import sys
import tempfile
from itertools import product
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import paper_portfolio as pp  # noqa: E402

OUT = Path(__file__).parent / "portfolio_capacity_report.md"

TICKERS = ["NVDA", "TSLA", "MSFT", "QCOM", "VKTX", "META", "BILI", "AMZN", "RKLB", "CRCL", "SPY"]

GRID = {
    "entry_score_bull": [6.5, 6.0],
    "size_mult": [1, 2, 3],
    "max_deployed_pct": [30.0, 60.0, 80.0],
    "tp_pct": [10.0, 15.0],
}

BASE_TIER = {"high": 2.5, "mid": 1.5, "low": 0.0}


def metrics(equity: list, closed: list, n_days: int) -> dict:
    if not equity:
        return {}
    navs = [e["nav"] for e in sorted(equity, key=lambda x: x["date"])]
    start_cap = 50_000.0
    total_ret = (navs[-1] / start_cap - 1) * 100

    # 年化（按快照跨度日历天数）
    d0 = sorted(equity, key=lambda x: x["date"])[0]["date"]
    d1 = sorted(equity, key=lambda x: x["date"])[-1]["date"]
    from datetime import datetime as dt
    cal_days = max(1, (dt.strptime(d1, "%Y-%m-%d") - dt.strptime(d0, "%Y-%m-%d")).days)
    ann_ret = ((navs[-1] / start_cap) ** (365.0 / cal_days) - 1) * 100

    # 日度收益 → Sharpe（快照非严格日频，近似按序列频率年化）
    rets = [(navs[i] / navs[i - 1] - 1) for i in range(1, len(navs)) if navs[i - 1] > 0]
    if len(rets) >= 2 and statistics.stdev(rets) > 0:
        periods_per_year = 252 * len(rets) / max(1, cal_days / 365 * 252)
        # 简化：按序列条数折算年频
        freq = len(rets) / (cal_days / 365.0)
        sharpe = statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(freq)
    else:
        sharpe = 0.0

    # MaxDD
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)

    calmar = (ann_ret / abs(mdd)) if mdd < -0.01 else float("inf")

    wins = sum(1 for t in closed if t.get("pnl_usd", 0) > 0)
    deployed_pcts = [e.get("deployed", 0) / e["nav"] * 100 for e in equity if e.get("nav")]

    return {
        "final_nav": navs[-1], "total_ret": total_ret, "ann_ret": ann_ret,
        "sharpe": sharpe, "mdd": mdd, "calmar": calmar,
        "trades": len(closed), "win_rate": wins / len(closed) * 100 if closed else 0.0,
        "avg_deployed": statistics.mean(deployed_pcts) if deployed_pcts else 0.0,
    }


def combo_label(e, m, d, t):
    return f"bull≥{e}/×{m}/在场{d:.0f}%/TP{t:.0f}%"


def run_grid(dates, sandbox_root: Path):
    results = []
    combos = list(product(GRID["entry_score_bull"], GRID["size_mult"],
                          GRID["max_deployed_pct"], GRID["tp_pct"]))
    for i, (e, m, d, t) in enumerate(combos, 1):
        overrides = {
            "entry_score_bull": e,
            "size_pct_by_tier": {k: v * m for k, v in BASE_TIER.items()},
            "max_deployed_pct": d,
            "tp_pct": t,
        }
        state = sandbox_root / f"combo_{i:02d}"
        shutil.rmtree(state, ignore_errors=True)
        r = pp.run_replay(overrides, state, dates)
        met = metrics(r["equity"], r["closed"], len(dates))
        met.update({"combo": combo_label(e, m, d, t),
                    "e": e, "m": m, "d": d, "t": t})
        results.append(met)
        print(f"[{i}/{len(combos)}] {met['combo']}: NAV=${met['final_nav']:,.0f} "
              f"ret={met['total_ret']:+.2f}% mdd={met['mdd']:.2f}% "
              f"calmar={met['calmar']:.2f} trades={met['trades']}")
    return results


def main():
    all_dates = [d for d in pp._all_snapshot_dates() if d >= "2026-03-09"]
    print(f"回放范围: {all_dates[0]} ~ {all_dates[-1]}, {len(all_dates)} 个快照日")
    print("预取 OHLC ...")
    pp.prefetch_ohlc(TICKERS, "2026-03-01", "2026-07-10")

    sandbox_root = Path(tempfile.mkdtemp(prefix="pp_capacity_"))
    print(f"沙盒: {sandbox_root}")

    # 全样本网格
    results = run_grid(all_dates, sandbox_root / "full")

    # 基线（现行配置）
    baseline = next(r for r in results if r["e"] == 6.5 and r["m"] == 1
                    and r["d"] == 30.0 and r["t"] == 10.0)

    # 排序：Calmar 降序（inf 视为收益为正但无回撤，排最前但标注）
    ranked = sorted(results, key=lambda r: (-(r["calmar"] if r["calmar"] != float("inf") else 1e9),
                                            -r["total_ret"]))

    # 拐点候选的前后半段稳健性检查（取 Calmar 前 3 且 trades>=30）
    half = len(all_dates) // 2
    d_first, d_second = all_dates[:half], all_dates[half:]
    top = [r for r in ranked if r["trades"] >= 30][:3]
    robust = []
    for r in top:
        overrides = {
            "entry_score_bull": r["e"],
            "size_pct_by_tier": {k: v * r["m"] for k, v in BASE_TIER.items()},
            "max_deployed_pct": r["d"],
            "tp_pct": r["t"],
        }
        s1 = pp.run_replay(overrides, sandbox_root / f"h1_{r['combo']}", d_first)
        s2 = pp.run_replay(overrides, sandbox_root / f"h2_{r['combo']}", d_second)
        m1 = metrics(s1["equity"], s1["closed"], len(d_first))
        m2 = metrics(s2["equity"], s2["closed"], len(d_second))
        robust.append((r["combo"], m1, m2))
        print(f"稳健性 {r['combo']}: 前半 {m1.get('total_ret', 0):+.2f}% / "
              f"后半 {m2.get('total_ret', 0):+.2f}%")

    # ── 报告 ──
    md = ["# 组合资金利用率网格回放（P3 / v0.38.2）", ""]
    md.append(f"回放范围 {all_dates[0]} ~ {all_dates[-1]}（{len(all_dates)} 个快照日，"
              f"约 {len(all_dates)/21:.1f} 个月）。SL 7% / T+10 固定；网格 36 组合。")
    md.append("")
    md.append(f"**基线（现行配置 {baseline['combo']}）**：收益 {baseline['total_ret']:+.2f}% | "
              f"年化 {baseline['ann_ret']:+.1f}% | MaxDD {baseline['mdd']:.2f}% | "
              f"Sharpe {baseline['sharpe']:.2f} | 交易 {baseline['trades']} 笔 | "
              f"平均在场 {baseline['avg_deployed']:.1f}%")
    md.append("")
    md.append("## 全部组合（Calmar 降序）")
    md.append("")
    md.append("| # | 组合 | 收益% | 年化% | MaxDD% | Sharpe | Calmar | 交易数 | 胜率% | 均在场% |")
    md.append("|---|------|-------|-------|--------|--------|--------|--------|-------|---------|")
    for i, r in enumerate(ranked, 1):
        cal = f"{r['calmar']:.2f}" if r["calmar"] != float("inf") else "∞"
        flag = " ⚠️样本不足" if r["trades"] < 30 else ""
        base = " ←基线" if r is baseline else ""
        md.append(f"| {i} | {r['combo']}{flag}{base} | {r['total_ret']:+.2f} | "
                  f"{r['ann_ret']:+.1f} | {r['mdd']:.2f} | {r['sharpe']:.2f} | {cal} | "
                  f"{r['trades']} | {r['win_rate']:.0f} | {r['avg_deployed']:.1f} |")
    md.append("")

    md.append("## 拐点候选稳健性（前半段 vs 后半段）")
    md.append("")
    md.append("| 组合 | 前半收益% | 前半MaxDD% | 后半收益% | 后半MaxDD% | 两段同向? |")
    md.append("|------|----------|-----------|----------|-----------|----------|")
    for combo, m1, m2 in robust:
        same = "✓" if (m1.get("total_ret", 0) > 0) == (m2.get("total_ret", 0) > 0) else "✗"
        md.append(f"| {combo} | {m1.get('total_ret', 0):+.2f} | {m1.get('mdd', 0):.2f} | "
                  f"{m2.get('total_ret', 0):+.2f} | {m2.get('mdd', 0):.2f} | {same} |")
    md.append("")

    # 推荐：trades>=30 且两段同向的 Calmar 最高者
    rec = None
    for combo, m1, m2 in robust:
        if (m1.get("total_ret", 0) > 0) == (m2.get("total_ret", 0) > 0):
            rec = combo
            break
    md.append("## 结论")
    md.append("")
    if rec:
        rec_r = next(r for r in ranked if r["combo"] == rec)
        md.append(f"- **性价比拐点：{rec}** —— 收益 {rec_r['total_ret']:+.2f}%"
                  f"（基线 {baseline['total_ret']:+.2f}%），MaxDD {rec_r['mdd']:.2f}%"
                  f"（基线 {baseline['mdd']:.2f}%），Calmar "
                  f"{rec_r['calmar'] if rec_r['calmar'] != float('inf') else '∞'}。")
    else:
        md.append("- 前 3 候选均未通过前后半段同向检查——**证据不足，建议维持现行配置**，"
                  "积累更长样本后重估。")
    md.append(f"- 全样本仅 ~{len(all_dates)/21:.0f} 个月、行情以 4-5 月上涨段为主，"
              "存在过拟合风险；建议中标配置先跑 4 周再复盘。")
    md.append("- **改不改生产 CONFIG 等用户拍板**——本实验只出报告。")
    md.append("")

    OUT.write_text("\n".join(md), encoding="utf-8")
    print(f"\n报告已写入 {OUT}")
    shutil.rmtree(sandbox_root, ignore_errors=True)


if __name__ == "__main__":
    main()
