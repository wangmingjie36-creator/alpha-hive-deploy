#!/usr/bin/env python3
"""
🐝 Alpha Hive — Bootstrap Confidence Intervals (v0.22.0)
=========================================================
解决"小样本 Sharpe -1.8 到底是不是真的负"的问题。

方法：对历史交易净收益做 N 次有放回重采样（Efron non-parametric bootstrap），
每次重采样计算 Sharpe / WR / Profit Factor / 平均净收益，
得到 95% 置信区间（2.5% / 97.5% 分位数）。

关键洞察
--------
样本 11 笔时 Sharpe -1.8 的 95% CI 可能跨度 [-5.0, +2.0] — 意味着"真实 Sharpe"
可能在正负之间飘忽。只有当 CI 上下限同号时，才能说"统计显著地好/差"。

与 Walk-Forward 的区别
----------------------
• Walk-Forward：评估"学习过程"是否过拟合（同一数据分布不同时间段）
• Bootstrap：评估"当前点估计"的**不确定性**（同一时间段的统计置信度）

用法
----
    # 基于所有 checked_t7=1 记录做 1000 次重采样
    python3 bootstrap_ci.py

    # 跑 portfolio_backtest 拿到 closed trades 后再 bootstrap（更贴近真实组合）
    python3 bootstrap_ci.py --source portfolio_backtest --n 5000

    # JSON 输出
    python3 bootstrap_ci.py --json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sqlite3
import statistics
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger("alpha_hive.bootstrap")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

T7_PERIODS_PER_YEAR = 36   # 修复 #8: 252 交易日 / 7 交易日采样 = 36


# ══════════════════════════════════════════════════════════════════════════════
# 数据源
# ══════════════════════════════════════════════════════════════════════════════

def _load_all_trades() -> List[float]:
    """从 pheromone.db 读所有 net_return_t7（已扣成本的净收益 %）"""
    db = Path(__file__).parent / "pheromone.db"
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute("""
            SELECT net_return_t7 FROM predictions
            WHERE checked_t7 = 1 AND net_return_t7 IS NOT NULL
        """).fetchall()
    return [float(r[0]) for r in rows]


def _load_portfolio_trades() -> List[float]:
    """运行 portfolio_backtest 并取 closed trades 的 net_return_pct（从 all_trades 字段）"""
    try:
        import portfolio_backtest as pb
        cfg = pb.BacktestConfig()
        result = pb.run_backtest(cfg)
        if "error" in result:
            _log.error("portfolio_backtest 返回错误: %s", result["error"])
            return []
        all_trades = result.get("all_trades") or []
        # 过滤掉 WINDOW_CUTOFF（net_pct=0 的强平仓位不代表真实信号质量）
        rets = [float(t["net_pct"]) for t in all_trades if t.get("exit_reason") != "WINDOW_CUTOFF"]
        return rets
    except Exception as e:
        _log.error("portfolio_backtest 导入/运行失败: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 核心指标
# ══════════════════════════════════════════════════════════════════════════════

def _metrics(rets: List[float]) -> Dict:
    """对一组净收益率（%）算 Sharpe / WR / PF / 平均"""
    if not rets:
        return {"n": 0, "sharpe": 0.0, "wr": 0.0, "pf": 0.0, "avg": 0.0}
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    wr = len(wins) / n * 100.0
    avg = sum(rets) / n
    std = statistics.pstdev(rets)
    sharpe = (avg / std) * math.sqrt(T7_PERIODS_PER_YEAR) if std > 1e-9 else 0.0
    total_win = sum(wins)
    total_loss = abs(sum(losses))
    pf = total_win / total_loss if total_loss > 1e-9 else (float("inf") if total_win > 0 else 0.0)
    return {
        "n": n,
        "sharpe": round(sharpe, 3),
        "wr": round(wr, 2),
        "pf": round(pf, 3) if pf != float("inf") else None,
        "avg": round(avg, 3),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Bootstrap
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BootstrapResult:
    n_samples: int
    n_iterations: int
    point_estimate: Dict        # 原始样本的点估计
    ci_95: Dict                 # { metric: {"lo": x, "hi": y} }
    ci_68: Dict
    bias: Dict                  # 点估计 vs bootstrap 均值的偏差
    significant: Dict           # { metric: bool } — CI 是否跨 0（即是否显著）


def run_bootstrap(rets: List[float], n_iter: int = 1000, seed: int = 42) -> BootstrapResult:
    if not rets:
        return BootstrapResult(0, 0, {}, {}, {}, {}, {})

    random.seed(seed)
    n = len(rets)
    # 点估计
    point = _metrics(rets)

    # 重采样
    samples = {"sharpe": [], "wr": [], "pf": [], "avg": []}
    for _ in range(n_iter):
        resample = [random.choice(rets) for _ in range(n)]
        m = _metrics(resample)
        for k in samples:
            v = m.get(k)
            if v is not None:
                samples[k].append(v)

    # 分位数
    def _quantile(lst, q):
        s = sorted(lst)
        idx = max(0, min(len(s) - 1, int(len(s) * q)))
        return s[idx]

    ci_95 = {}
    ci_68 = {}
    bias = {}
    significant = {}
    for k, vals in samples.items():
        if not vals:
            continue
        lo95, hi95 = _quantile(vals, 0.025), _quantile(vals, 0.975)
        lo68, hi68 = _quantile(vals, 0.16), _quantile(vals, 0.84)
        mean = sum(vals) / len(vals)
        ci_95[k] = {"lo": round(lo95, 3), "hi": round(hi95, 3)}
        ci_68[k] = {"lo": round(lo68, 3), "hi": round(hi68, 3)}
        # 显著性：CI 上下限是否同号（对 Sharpe / avg；WR 基准是 50）
        if k in ("sharpe", "avg"):
            significant[k] = (lo95 > 0 and hi95 > 0) or (lo95 < 0 and hi95 < 0)
        elif k == "wr":
            significant[k] = (lo95 > 50 and hi95 > 50) or (lo95 < 50 and hi95 < 50)
        elif k == "pf":
            significant[k] = (lo95 > 1 and hi95 > 1) or (lo95 < 1 and hi95 < 1)
        bias[k] = round(mean - (point.get(k) or 0), 3)

    return BootstrapResult(
        n_samples=n,
        n_iterations=n_iter,
        point_estimate=point,
        ci_95=ci_95,
        ci_68=ci_68,
        bias=bias,
        significant=significant,
    )


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _print_report(r: BootstrapResult, source: str) -> None:
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  Alpha Hive — Bootstrap 置信区间分析 (v0.22.0)       ║")
    print("╚══════════════════════════════════════════════════════╝\n")
    print(f"  数据源：{source}")
    print(f"  样本数：{r.n_samples}   重采样：{r.n_iterations} 次\n")

    if r.n_samples == 0:
        print("  ❌ 无可用样本")
        return

    p = r.point_estimate
    print("  ┌─── 点估计 vs 95% CI ───────────────────────────────┐")
    metrics_order = ["sharpe", "wr", "avg", "pf"]
    labels = {"sharpe": "Sharpe", "wr": "Win Rate %", "avg": "Avg Net %", "pf": "Profit Factor"}
    for k in metrics_order:
        if k not in r.ci_95:
            continue
        pt = p.get(k)
        if pt is None:
            continue
        lo = r.ci_95[k]["lo"]; hi = r.ci_95[k]["hi"]
        sig = "✓ 显著" if r.significant.get(k) else "✗ 不显著（CI 跨零/基准）"
        bias = r.bias.get(k, 0)
        print(f"    {labels[k]:<15} 点估计 {pt:>+8.3f}   95% CI [{lo:>+8.3f}, {hi:>+8.3f}]   {sig}  bias={bias:+.3f}")
    print("  └─────────────────────────────────────────────────────┘\n")

    # 关键结论
    sig_sharpe = r.significant.get("sharpe", False)
    pt_sharpe = p.get("sharpe", 0)
    if sig_sharpe and pt_sharpe > 0:
        verdict = f"🟢 Sharpe {pt_sharpe:+.2f} **统计显著为正** — 系统有稳健 edge"
    elif sig_sharpe and pt_sharpe < 0:
        verdict = f"🔴 Sharpe {pt_sharpe:+.2f} **统计显著为负** — 系统实质亏损"
    else:
        verdict = f"🟡 Sharpe {pt_sharpe:+.2f} **CI 跨零** — 样本太小，真实方向未知"
    print(f"  📊 核心结论：{verdict}\n")

    # 样本量建议
    if r.n_samples < 50:
        needed = 50 - r.n_samples
        print(f"  💡 样本量建议：当前 {r.n_samples} 笔过小，至少扩到 50 笔（还需 {needed} 笔）才能稳健判断")
    elif r.n_samples < 100:
        print(f"  💡 样本量建议：{r.n_samples} 笔勉强可用，100+ 更稳")
    print()


def main():
    parser = argparse.ArgumentParser(description="Alpha Hive Bootstrap CI")
    parser.add_argument("--source", choices=["raw_db", "portfolio_backtest"],
                        default="raw_db",
                        help="raw_db = 所有 checked_t7 记录；portfolio_backtest = 组合回测 closed trades")
    parser.add_argument("--n", type=int, default=1000, help="重采样次数 (默认 1000)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.source == "portfolio_backtest":
        rets = _load_portfolio_trades()
        src_label = "portfolio_backtest (closed trades)"
    else:
        rets = _load_all_trades()
        src_label = "raw_db (全部 checked_t7=1)"

    result = run_bootstrap(rets, n_iter=args.n, seed=args.seed)

    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        _print_report(result, src_label)


if __name__ == "__main__":
    main()
