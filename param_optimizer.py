#!/usr/bin/env python3
"""
Alpha Hive · 参数优化器 v0.19.1
SL/TP/部署上限 网格搜索 → 对比 NAV 曲线 + 最优参数推荐

用法：
    python3 param_optimizer.py              # 跑全量网格
    python3 param_optimizer.py --quick      # 快速 12 组合
    python3 param_optimizer.py --html       # 生成对比 HTML
"""
from __future__ import annotations

import copy
import json
import math
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

# ══════════════════════════════════════════════════════════════════════════════
# 参数网格
# ══════════════════════════════════════════════════════════════════════════════

SL_GRID = [5.0, 7.0, 8.0, 10.0]
TP_GRID = [10.0, 15.0, 20.0]
DEPLOY_GRID = [30.0, 40.0, 50.0, 60.0]

# 快速模式：精选组合
QUICK_COMBOS = [
    (5.0, 10.0, 30.0),   # baseline
    (7.0, 10.0, 30.0),
    (8.0, 15.0, 30.0),
    (10.0, 15.0, 30.0),
    (5.0, 10.0, 50.0),
    (7.0, 10.0, 50.0),
    (7.0, 15.0, 50.0),
    (8.0, 15.0, 50.0),
    (8.0, 20.0, 50.0),
    (10.0, 15.0, 50.0),
    (10.0, 20.0, 50.0),
    (7.0, 15.0, 40.0),
]

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "paper_portfolio_state"
BACKUP_DIR = BASE_DIR / "paper_portfolio_state_backup"
RESULTS_FILE = BASE_DIR / "param_optimization_results.json"


@dataclass
class RunResult:
    sl: float
    tp: float
    deploy: float
    nav: float
    total_return_pct: float
    spy_return_pct: float
    alpha_pct: float
    sharpe: float
    mdd_pct: float
    win_rate_pct: float
    trades_total: int
    trades_wins: int
    avg_pnl_per_trade: float
    profit_factor: float
    equity_curve: List[Tuple[str, float]]

    def to_dict(self) -> Dict:
        return {
            "sl": self.sl, "tp": self.tp, "deploy": self.deploy,
            "nav": self.nav, "total_return_pct": self.total_return_pct,
            "spy_return_pct": self.spy_return_pct, "alpha_pct": self.alpha_pct,
            "sharpe": self.sharpe, "mdd_pct": self.mdd_pct,
            "win_rate_pct": self.win_rate_pct,
            "trades_total": self.trades_total, "trades_wins": self.trades_wins,
            "avg_pnl_per_trade": self.avg_pnl_per_trade,
            "profit_factor": self.profit_factor,
            "equity_curve": self.equity_curve,
        }


def _backup_state():
    if STATE_DIR.exists():
        if BACKUP_DIR.exists():
            shutil.rmtree(BACKUP_DIR)
        shutil.copytree(STATE_DIR, BACKUP_DIR)


def _restore_state():
    if BACKUP_DIR.exists():
        if STATE_DIR.exists():
            shutil.rmtree(STATE_DIR)
        shutil.copytree(BACKUP_DIR, STATE_DIR)


def _clear_state():
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    STATE_DIR.mkdir(exist_ok=True)


def _run_one_combo(sl: float, tp: float, deploy: float) -> RunResult:
    """用指定参数跑一次完整 bootstrap 并返回结果"""
    import paper_portfolio as pp

    # 动态覆写 CONFIG
    pp.CONFIG["sl_pct"] = sl
    pp.CONFIG["tp_pct"] = tp
    pp.CONFIG["max_deployed_pct"] = deploy
    # ⚠️ 优化器必须中立化白名单：避免 live_start_date 后的日期触发白名单过滤
    # 导致多标的 bootstrap 变成单标的测试，不同组合结果不可比
    pp.CONFIG["ticker_whitelist"] = []   # 优化期间全标的
    pp.CONFIG["live_start_date"] = ""    # 禁用两层模式

    # 清空状态
    _clear_state()

    # 清 yfinance 缓存提高一致性
    pp._PRICE_CACHE.clear()

    # Bootstrap
    pp.bootstrap_from_history(verbose=False)

    # KPI
    kpi = pp.compute_kpis()

    # 额外指标：avg PnL per trade, profit factor
    closed = pp._load_jsonl(pp.CLOSED_FILE)
    gross_wins = sum(t["pnl_usd"] for t in closed if t["pnl_usd"] > 0)
    gross_losses = abs(sum(t["pnl_usd"] for t in closed if t["pnl_usd"] < 0))
    avg_pnl = sum(t["pnl_usd"] for t in closed) / max(len(closed), 1)
    pf = gross_wins / gross_losses if gross_losses > 0 else (999.0 if gross_wins > 0 else 0.0)

    # Equity curve
    eq = pp._load_jsonl(pp.EQUITY_FILE)
    eq_sorted = sorted(eq, key=lambda x: x["date"])
    eq_curve = [(e["date"], e["nav"]) for e in eq_sorted]

    return RunResult(
        sl=sl, tp=tp, deploy=deploy,
        nav=kpi["nav"],
        total_return_pct=kpi["total_return_pct"],
        spy_return_pct=kpi["spy_return_pct"],
        alpha_pct=kpi["alpha_pct"],
        sharpe=kpi["sharpe"],
        mdd_pct=kpi["max_drawdown_pct"],
        win_rate_pct=kpi["win_rate_pct"],
        trades_total=kpi["trades_total"],
        trades_wins=kpi["trades_wins"],
        avg_pnl_per_trade=round(avg_pnl, 2),
        profit_factor=round(pf, 2),
        equity_curve=eq_curve,
    )


def run_grid(quick: bool = False) -> List[RunResult]:
    """执行参数网格搜索"""
    if quick:
        combos = QUICK_COMBOS
    else:
        combos = [(sl, tp, dp) for sl in SL_GRID for tp in TP_GRID for dp in DEPLOY_GRID]

    print(f"🔬 参数优化：{len(combos)} 组合")
    print(f"   SL: {sorted(set(c[0] for c in combos))}")
    print(f"   TP: {sorted(set(c[1] for c in combos))}")
    print(f"   Deploy: {sorted(set(c[2] for c in combos))}")
    print()

    # 备份当前状态
    _backup_state()

    results: List[RunResult] = []
    for i, (sl, tp, dp) in enumerate(combos, 1):
        label = f"SL-{sl:.0f}% TP+{tp:.0f}% Deploy{dp:.0f}%"
        t0 = time.time()
        try:
            r = _run_one_combo(sl, tp, dp)
            elapsed = time.time() - t0
            print(f"  [{i:2d}/{len(combos)}] {label:30s}  "
                  f"NAV=${r.nav:,.2f} ({r.total_return_pct:+.2f}%)  "
                  f"α={r.alpha_pct:+.2f}%  S={r.sharpe:.2f}  "
                  f"WR={r.win_rate_pct:.0f}% ({r.trades_wins}/{r.trades_total})  "
                  f"PF={r.profit_factor:.2f}  MDD={r.mdd_pct:.1f}%  "
                  f"[{elapsed:.1f}s]")
            results.append(r)
        except Exception as e:
            print(f"  [{i:2d}/{len(combos)}] {label:30s}  ❌ {e}")

    # 恢复原始状态（数据文件 + CONFIG）
    _restore_state()
    import paper_portfolio as _pp
    _pp.CONFIG["sl_pct"] = 7.0
    _pp.CONFIG["tp_pct"] = 10.0
    _pp.CONFIG["max_deployed_pct"] = 30.0
    _pp.CONFIG["ticker_whitelist"] = ["NVDA"]
    _pp.CONFIG["live_start_date"] = "2026-04-16"

    # 保存结果
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "combos_tested": len(results),
        "results": [r.to_dict() for r in results],
    }
    RESULTS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n💾 结果已保存 → {RESULTS_FILE.name}")

    return results


def rank_results(results: List[RunResult]) -> List[RunResult]:
    """综合排名：多目标加权 (Alpha 40% + Sharpe 25% + ProfitFactor 20% + WinRate 15%)"""
    if not results:
        return []

    # 归一化到 [0, 1]
    def _norm(vals):
        lo, hi = min(vals), max(vals)
        rng = hi - lo if hi != lo else 1.0
        return [(v - lo) / rng for v in vals]

    alphas = _norm([r.alpha_pct for r in results])
    sharpes = _norm([r.sharpe for r in results])
    pfs = _norm([r.profit_factor for r in results])
    wrs = _norm([r.win_rate_pct for r in results])

    scores = []
    for i in range(len(results)):
        s = alphas[i] * 0.40 + sharpes[i] * 0.25 + pfs[i] * 0.20 + wrs[i] * 0.15
        scores.append((s, results[i]))

    scores.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scores]


def render_html(results: List[RunResult]) -> str:
    """生成参数对比 HTML 报告"""
    ranked = rank_results(results)
    if not ranked:
        return "<p>无结果</p>"

    best = ranked[0]
    baseline = next((r for r in results if r.sl == 5.0 and r.tp == 10.0 and r.deploy == 30.0), None)

    # ── Top 10 排行表 ──
    table_rows = ""
    for i, r in enumerate(ranked[:15]):
        is_best = (i == 0)
        is_base = (r.sl == 5.0 and r.tp == 10.0 and r.deploy == 30.0)
        row_style = "background:rgba(34,197,94,0.08);" if is_best else ("background:rgba(251,191,36,0.08);" if is_base else "")
        badge = " 👑" if is_best else (" 📌 baseline" if is_base else "")
        alpha_col = "#10b981" if r.alpha_pct >= 0 else "#ef4444"
        table_rows += (
            f'<tr style="{row_style}">'
            f'<td style="padding:6px 10px;font-weight:600;">#{i+1}{badge}</td>'
            f'<td style="padding:6px 10px;">SL-{r.sl:.0f}%</td>'
            f'<td style="padding:6px 10px;">TP+{r.tp:.0f}%</td>'
            f'<td style="padding:6px 10px;">Deploy {r.deploy:.0f}%</td>'
            f'<td style="padding:6px 10px;text-align:right;font-weight:600;">${r.nav:,.2f}</td>'
            f'<td style="padding:6px 10px;text-align:right;">{r.total_return_pct:+.2f}%</td>'
            f'<td style="padding:6px 10px;text-align:right;color:{alpha_col};font-weight:600;">{r.alpha_pct:+.2f}%</td>'
            f'<td style="padding:6px 10px;text-align:right;">{r.sharpe:.2f}</td>'
            f'<td style="padding:6px 10px;text-align:right;">{r.win_rate_pct:.0f}% ({r.trades_wins}/{r.trades_total})</td>'
            f'<td style="padding:6px 10px;text-align:right;">{r.profit_factor:.2f}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#ef4444;">{r.mdd_pct:.1f}%</td>'
            f'</tr>'
        )

    # ── SL×TP Heatmap (Alpha) for each Deploy level ──
    heatmaps_html = ""
    deploy_levels = sorted(set(r.deploy for r in results))
    for dp in deploy_levels:
        subset = [r for r in results if r.deploy == dp]
        sls = sorted(set(r.sl for r in subset))
        tps = sorted(set(r.tp for r in subset))
        lookup = {(r.sl, r.tp): r for r in subset}

        # find range for coloring
        alphas = [r.alpha_pct for r in subset]
        a_min, a_max = min(alphas) if alphas else 0, max(alphas) if alphas else 1
        a_rng = a_max - a_min if a_max != a_min else 1

        hdr = "".join(f'<th style="padding:6px 10px;text-align:center;">TP+{tp:.0f}%</th>' for tp in tps)
        rows = ""
        for sl in sls:
            cells = ""
            for tp in tps:
                r = lookup.get((sl, tp))
                if r:
                    # green(+) to red(-) gradient
                    t = (r.alpha_pct - a_min) / a_rng
                    if r.alpha_pct >= 0:
                        bg = f"rgba(34,197,94,{0.1 + t*0.4:.2f})"
                    else:
                        bg = f"rgba(239,68,68,{0.1 + (1-t)*0.4:.2f})"
                    cells += (f'<td style="padding:8px;text-align:center;background:{bg};'
                              f'font-weight:600;font-size:13px;">'
                              f'{r.alpha_pct:+.2f}%<br>'
                              f'<span style="font-size:10px;font-weight:400;opacity:0.7;">'
                              f'S={r.sharpe:.1f} WR={r.win_rate_pct:.0f}%</span></td>')
                else:
                    cells += '<td style="padding:8px;text-align:center;color:#64748b;">—</td>'
            rows += f'<tr><td style="padding:6px 10px;font-weight:600;">SL-{sl:.0f}%</td>{cells}</tr>'

        heatmaps_html += (
            f'<div style="margin:20px 0;">'
            f'<div style="font-size:13px;font-weight:700;color:var(--text2);margin-bottom:8px;">'
            f'Deploy {dp:.0f}% — Alpha Heatmap (SL × TP)</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:12px;">'
            f'<thead><tr><th style="padding:6px 10px;"></th>{hdr}</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
        )

    # ── NAV Curves (top 5 + baseline) ──
    curve_colors = ["#22c55e", "#3b82f6", "#f59e0b", "#8b5cf6", "#ec4899", "#64748b"]
    curves_to_plot = ranked[:5]
    if baseline and baseline not in curves_to_plot:
        curves_to_plot.append(baseline)

    # Build SVG
    all_navs = [n for r in curves_to_plot for _, n in r.equity_curve]
    if all_navs:
        y_min = min(all_navs) * 0.999
        y_max = max(all_navs) * 1.001
    else:
        y_min, y_max = 49000, 51000
    y_rng = y_max - y_min if y_max > y_min else 1
    svg_w, svg_h = 700, 280

    svg_paths = ""
    legend_items = ""
    for ci, r in enumerate(curves_to_plot):
        color = curve_colors[ci % len(curve_colors)]
        if not r.equity_curve:
            continue
        pts = []
        n = len(r.equity_curve)
        for j, (d, nav) in enumerate(r.equity_curve):
            x = j / max(n - 1, 1) * svg_w
            y = svg_h - ((nav - y_min) / y_rng) * svg_h
            pts.append(f"{x:.1f},{y:.1f}")
        path = "M" + " L".join(pts)
        is_base = (r.sl == 5.0 and r.tp == 10.0 and r.deploy == 30.0)
        stroke_dash = 'stroke-dasharray="6,4"' if is_base else ""
        svg_paths += f'<path d="{path}" stroke="{color}" stroke-width="{"1.5" if is_base else "2"}" fill="none" {stroke_dash}/>'
        label = f"SL-{r.sl:.0f} TP+{r.tp:.0f} D{r.deploy:.0f}"
        badge = " (baseline)" if is_base else (" 👑" if ci == 0 else "")
        legend_items += (
            f'<span style="display:inline-flex;align-items:center;margin-right:14px;font-size:11px;">'
            f'<span style="display:inline-block;width:16px;height:3px;background:{color};'
            f'margin-right:4px;{"border-top:1px dashed " + color if is_base else ""}"></span>'
            f'{label}{badge}</span>'
        )

    # Y-axis labels
    y_labels_html = ""
    for frac in [0, 0.25, 0.5, 0.75, 1.0]:
        val = y_min + frac * y_rng
        y_pos = svg_h - frac * svg_h
        y_labels_html += (
            f'<text x="-5" y="{y_pos:.0f}" text-anchor="end" '
            f'fill="#64748b" font-size="10" dominant-baseline="middle">'
            f'${val:,.0f}</text>'
            f'<line x1="0" y1="{y_pos:.0f}" x2="{svg_w}" y2="{y_pos:.0f}" '
            f'stroke="#2e3348" stroke-width="0.5"/>'
        )

    nav_svg = (
        f'<div style="margin:20px 0;">'
        f'<div style="font-size:13px;font-weight:700;color:var(--text2);margin-bottom:8px;">NAV 曲线对比 (Top 5 + Baseline)</div>'
        f'<svg width="{svg_w + 70}" height="{svg_h + 20}" viewBox="-65 -5 {svg_w + 70} {svg_h + 20}" '
        f'style="display:block;margin:0 auto;">'
        f'{y_labels_html}{svg_paths}'
        f'</svg>'
        f'<div style="margin-top:8px;text-align:center;">{legend_items}</div>'
        f'</div>'
    )

    # ── Recommendation card ──
    b = best
    rec_html = (
        f'<div style="margin:20px 0;padding:16px;background:linear-gradient(135deg,rgba(34,197,94,0.08),rgba(59,130,246,0.05));'
        f'border:2px solid rgba(34,197,94,0.4);border-radius:12px;">'
        f'<div style="font-size:14px;font-weight:700;color:#22c55e;margin-bottom:10px;">👑 推荐参数</div>'
        f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px;">'
        f'<div style="text-align:center;"><div style="font-size:24px;font-weight:800;color:var(--text1);">SL -{b.sl:.0f}%</div>'
        f'<div style="font-size:11px;color:var(--text3);">止损宽度</div></div>'
        f'<div style="text-align:center;"><div style="font-size:24px;font-weight:800;color:var(--text1);">TP +{b.tp:.0f}%</div>'
        f'<div style="font-size:11px;color:var(--text3);">止盈目标</div></div>'
        f'<div style="text-align:center;"><div style="font-size:24px;font-weight:800;color:var(--text1);">Deploy {b.deploy:.0f}%</div>'
        f'<div style="font-size:11px;color:var(--text3);">部署上限</div></div></div>'
        f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;font-size:12px;">'
        f'<div>Alpha <strong style="color:#10b981;">{b.alpha_pct:+.2f}%</strong></div>'
        f'<div>Sharpe <strong>{b.sharpe:.2f}</strong></div>'
        f'<div>胜率 <strong>{b.win_rate_pct:.0f}%</strong> ({b.trades_wins}/{b.trades_total})</div>'
        f'<div>盈亏比 <strong>{b.profit_factor:.2f}</strong></div></div>'
    )
    if baseline:
        delta_alpha = b.alpha_pct - baseline.alpha_pct
        delta_sharpe = b.sharpe - baseline.sharpe
        delta_wr = b.win_rate_pct - baseline.win_rate_pct
        rec_html += (
            f'<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border2);font-size:11px;color:var(--text3);">'
            f'vs Baseline (SL-5 TP+10 D30): Alpha {delta_alpha:+.2f}%  '
            f'Sharpe {delta_sharpe:+.2f}  胜率 {delta_wr:+.1f}pp</div>'
        )
    rec_html += '</div>'

    # ── Assemble ──
    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<title>Alpha Hive · 参数优化报告</title>'
        '<style>'
        ':root{--bg1:#0f1119;--bg2:#1a1d2e;--bg3:#252840;--border1:#2e3348;--border2:#3a4055;'
        '--text1:#e2e8f0;--text2:#94a3b8;--text3:#64748b;}'
        'body{background:var(--bg1);color:var(--text1);font-family:system-ui,-apple-system,sans-serif;'
        'padding:24px;max-width:900px;margin:0 auto;line-height:1.6;}'
        'h1{font-size:20px;color:#22c55e;border-bottom:1px solid var(--border1);padding-bottom:12px;}'
        'table{border-collapse:collapse;width:100%;font-size:12px;}'
        'thead{border-bottom:2px solid var(--border1);}'
        'th{padding:8px 10px;text-align:left;color:var(--text3);font-weight:600;}'
        'td{padding:6px 10px;border-bottom:1px solid var(--border2);}'
        'tr:hover{background:rgba(255,255,255,0.02);}'
        '</style></head><body>'
        '<h1>🔬 Alpha Hive · 参数优化报告</h1>'
        f'<div style="font-size:12px;color:var(--text3);margin-bottom:20px;">'
        f'测试组合数: {len(results)} · 数据期间: {results[0].equity_curve[0][0] if results and results[0].equity_curve else "N/A"}'
        f' → {results[0].equity_curve[-1][0] if results and results[0].equity_curve else "N/A"}'
        f' · 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>'
        f'{rec_html}'
        f'{nav_svg}'
        '<h2 style="font-size:16px;color:var(--text2);margin-top:30px;">📊 排行榜 (Top 15)</h2>'
        '<table><thead><tr>'
        '<th>#</th><th>SL</th><th>TP</th><th>Deploy</th><th>NAV</th><th>Return</th>'
        '<th>Alpha</th><th>Sharpe</th><th>WinRate</th><th>PF</th><th>MDD</th>'
        '</tr></thead><tbody>'
        f'{table_rows}</tbody></table>'
        f'{heatmaps_html}'
        '<div style="margin-top:30px;padding:12px;background:var(--bg2);border-radius:8px;font-size:11px;color:var(--text3);">'
        '⚠️ 警告：37 天 / 12 笔交易的回测样本极小，参数优化存在过拟合风险。'
        '推荐参数仅供参考，建议先用 IBKR Paper Account 跑 30 天 out-of-sample 验证后再调整生产参数。'
        '</div>'
        '</body></html>'
    )
    return html


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser(description="Alpha Hive 参数优化器")
    p.add_argument("--quick", action="store_true", help="快速模式 (12 组合)")
    p.add_argument("--html", action="store_true", help="从已有结果生成 HTML")
    args = p.parse_args()

    if args.html:
        if not RESULTS_FILE.exists():
            print("❌ 找不到 param_optimization_results.json，先跑一次 grid")
            sys.exit(1)
        data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        results = []
        for d in data["results"]:
            results.append(RunResult(**d))
        html = render_html(results)
        out = BASE_DIR / "param_optimization_report.html"
        out.write_text(html, encoding="utf-8")
        print(f"✅ HTML 报告 → {out}")
        return

    results = run_grid(quick=args.quick)

    # 生成 HTML
    html = render_html(results)
    out = BASE_DIR / "param_optimization_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"\n✅ HTML 报告 → {out}")

    # 打印推荐
    ranked = rank_results(results)
    if ranked:
        b = ranked[0]
        print(f"\n👑 推荐参数：SL-{b.sl:.0f}%  TP+{b.tp:.0f}%  Deploy {b.deploy:.0f}%")
        print(f"   NAV ${b.nav:,.2f} ({b.total_return_pct:+.2f}%)  Alpha {b.alpha_pct:+.2f}%  Sharpe {b.sharpe:.2f}")
        print(f"   胜率 {b.win_rate_pct:.0f}% ({b.trades_wins}/{b.trades_total})  PF {b.profit_factor:.2f}  MDD {b.mdd_pct:.1f}%")


if __name__ == "__main__":
    main()
