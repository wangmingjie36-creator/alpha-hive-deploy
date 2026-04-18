#!/usr/bin/env python3
"""
🐝 Alpha Hive — Walk-Forward Validator (v0.22.0)
=================================================
防止 weekly_optimizer 过拟合的 out-of-sample 验证框架。

核心思路
--------
把历史预测按时间切 70/30（或更严格的 k-fold rolling）：
  • Train window: 用这段数据跑 weekly_optimizer → 生成"如果过去这么学"的权重
  • Test  window: 用训练出的权重跑 portfolio_backtest → 纯样本外表现
  • 比对：训练期 vs 测试期 Sharpe/WR/PnL — 差距越大越过拟合

重要：这里没有 lookahead，因为：
  (1) 训练期 WLS 只用训练期快照
  (2) 测试期的"final_score"是原始 bee 输出（已 frozen），未被训练后权重改写
  (3) 对比的"测试期 if trained on train"是理论上限 — 实盘不可能预先知道 train 结束时的权重

用法
----
    # 快速跑默认 70/30 切分
    python3 walk_forward_validator.py

    # 多折 rolling walk-forward（更严格）
    python3 walk_forward_validator.py --folds 3 --train-pct 0.6 --test-pct 0.2

    # JSON 输出（供 CHANGELOG / dashboard 引用）
    python3 walk_forward_validator.py --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger("alpha_hive.walk_forward")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class WalkForwardConfig:
    # v0.23.2 修复 #4：默认 train=0.6 / test=0.2 留出 20% 给 rolling step（覆盖 folds>1 场景）
    # 旧默认 train=0.7 / test=0.3 在 folds>1 时会崩（available=0 → 所有 fold 同一窗口）
    train_pct: float = 0.60        # 训练期占比
    test_pct: float = 0.20         # 测试期占比
    folds: int = 1                 # 几折（>1 时 rolling）
    min_train_samples: int = 40    # 最少训练样本
    min_test_samples: int = 20     # 最少测试样本
    purge_days: int = 0            # 训练/测试之间 purge gap（天）— 防信息泄漏


@dataclass
class FoldResult:
    fold_idx: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_n: int
    test_n: int
    # 训练期统计
    train_wr: float
    train_net_avg: float
    # 测试期统计
    test_wr: float
    test_net_avg: float
    test_sharpe: float
    test_pnl_usd: float
    # 过拟合指标
    overfitting_gap_pp: float      # train_wr - test_wr（正数 = 过拟合）


# ══════════════════════════════════════════════════════════════════════════════
# 数据加载（完整字段，复用 portfolio_backtest 口径）
# ══════════════════════════════════════════════════════════════════════════════

def _load_all_verified() -> List[Dict]:
    """加载全部 checked_t7=1 且 net_return_t7 非空 的预测，按 date 升序"""
    base = Path(__file__).parent
    db = base / "pheromone.db"
    if not db.exists():
        raise FileNotFoundError("pheromone.db not found")
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, date, ticker, direction, final_score,
                   price_at_predict, return_t7, net_return_t7,
                   exit_reason, exit_date, exit_price, holding_days,
                   cost_breakdown, spy_return_t7, dimension_scores
            FROM predictions
            WHERE checked_t7 = 1 AND net_return_t7 IS NOT NULL
            ORDER BY date ASC, id ASC
        """).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# 纯统计（避免重跑完整组合回测，聚焦 per-trade 净收益）
# ══════════════════════════════════════════════════════════════════════════════

def _stats(trades: List[Dict], periods_per_year: int = 36) -> Dict:
    """
    对一组交易记录计算 WR/avg net/Sharpe/PnL（$50K 等权 10% 仓模拟）
    此处不做严格组合层模拟（避免与 portfolio_backtest 耦合）—
    目标是快速对比 train vs test 的一阶统计差异
    """
    if not trades:
        return {"n": 0, "wr": 0.0, "net_avg": 0.0, "sharpe": 0.0, "pnl_usd": 0.0}
    nets = [float(t["net_return_t7"]) for t in trades]
    n = len(nets)
    wins = sum(1 for r in nets if r > 0)
    wr = wins / n * 100.0
    net_avg = sum(nets) / n
    # Sharpe（年化）
    import statistics
    std = statistics.pstdev(nets)
    sharpe = 0.0
    if std > 1e-8:
        sharpe = (net_avg / std) * (periods_per_year ** 0.5)
    # PnL: $50K × 10% 仓 × 每笔净收益
    pnl_usd = sum(50_000.0 * 0.10 * (r / 100.0) for r in nets)
    return {
        "n": n,
        "wr": round(wr, 2),
        "net_avg": round(net_avg, 3),
        "sharpe": round(sharpe, 3),
        "pnl_usd": round(pnl_usd, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 切分逻辑
# ══════════════════════════════════════════════════════════════════════════════

def _split_by_time(
    trades: List[Dict], train_pct: float, test_pct: float, purge_days: int = 0,
    fold_idx: int = 0, total_folds: int = 1,
) -> Tuple[List[Dict], List[Dict], Tuple[str, str, str, str]]:
    """
    时间序列切分：返回 (train, test, (train_start, train_end, test_start, test_end))
    支持 rolling k-fold：fold_idx=0 是最老的窗口，fold_idx=k-1 是最新的

    示例 3 folds / train=0.6 / test=0.2:
      fold 0: train [0%, 60%]  test [60%, 80%]
      fold 1: train [10%, 70%] test [70%, 90%]
      fold 2: train [20%, 80%] test [80%, 100%]
    """
    n = len(trades)
    if n == 0:
        return [], [], ("", "", "", "")
    # 步长（rolling）
    # v0.23.2 修复 #4：train_pct + test_pct >= 1.0 时 step 退化，k-fold 失去意义
    if total_folds > 1:
        available = 1.0 - (train_pct + test_pct)
        if available <= 0:
            # train+test 铺满整个区间：所有 fold 都在同一窗口，退化为 single-split
            # 强制降为单 fold 避免误导
            offset = 0.0
            if fold_idx > 0:
                return [], [], ("", "", "", "")  # 后续 fold 空返回
        else:
            step = available / max(total_folds - 1, 1)
            offset = step * fold_idx
    else:
        offset = 0.0

    train_lo = int(n * offset)
    train_hi = int(n * (offset + train_pct))
    test_lo = train_hi + int(n * purge_days / max(n, 1))  # purge gap（按交易数近似）
    test_hi = test_lo + int(n * test_pct)

    test_lo = min(test_lo, n)
    test_hi = min(test_hi, n)
    train_lo = max(0, train_lo)
    train_hi = min(n, train_hi)

    train = trades[train_lo:train_hi]
    test = trades[test_lo:test_hi]

    tr_start = trades[train_lo]["date"] if train else ""
    tr_end = trades[train_hi - 1]["date"] if train else ""
    te_start = trades[test_lo]["date"] if test else ""
    te_end = trades[test_hi - 1]["date"] if test else ""
    return train, test, (tr_start, tr_end, te_start, te_end)


# ══════════════════════════════════════════════════════════════════════════════
# 核心：run_walk_forward
# ══════════════════════════════════════════════════════════════════════════════

def run_walk_forward(cfg: WalkForwardConfig) -> Dict:
    trades = _load_all_verified()
    n_total = len(trades)

    if n_total < cfg.min_train_samples + cfg.min_test_samples:
        return {
            "error": f"样本不足：{n_total} < {cfg.min_train_samples + cfg.min_test_samples}",
            "total": n_total,
        }

    fold_results: List[FoldResult] = []
    for fold in range(cfg.folds):
        train, test, (ts, te, vs, ve) = _split_by_time(
            trades,
            train_pct=cfg.train_pct,
            test_pct=cfg.test_pct,
            purge_days=cfg.purge_days,
            fold_idx=fold,
            total_folds=cfg.folds,
        )
        if len(train) < cfg.min_train_samples or len(test) < cfg.min_test_samples:
            _log.warning(
                "Fold %d skip: train=%d test=%d (need %d/%d)",
                fold, len(train), len(test),
                cfg.min_train_samples, cfg.min_test_samples,
            )
            continue

        train_stats = _stats(train)
        test_stats = _stats(test)

        fold_results.append(FoldResult(
            fold_idx=fold,
            train_start=ts, train_end=te,
            test_start=vs, test_end=ve,
            train_n=train_stats["n"],
            test_n=test_stats["n"],
            train_wr=train_stats["wr"],
            train_net_avg=train_stats["net_avg"],
            test_wr=test_stats["wr"],
            test_net_avg=test_stats["net_avg"],
            test_sharpe=test_stats["sharpe"],
            test_pnl_usd=test_stats["pnl_usd"],
            overfitting_gap_pp=round(train_stats["wr"] - test_stats["wr"], 2),
        ))

    if not fold_results:
        return {"error": "所有 fold 样本不足", "total": n_total}

    # 汇总
    avg_test_wr = sum(f.test_wr for f in fold_results) / len(fold_results)
    avg_test_sharpe = sum(f.test_sharpe for f in fold_results) / len(fold_results)
    avg_overfit = sum(f.overfitting_gap_pp for f in fold_results) / len(fold_results)
    # 用绝对值判定"不稳定性"：无论方向，大 gap 都意味着训练期与测试期分布显著差异
    abs_gaps = [abs(f.overfitting_gap_pp) for f in fold_results]
    max_abs_gap = max(abs_gaps)

    # 过拟合 vs 非平稳性：
    #   gap > 0 (train>test)  → 传统过拟合（optimizer 学到训练期噪声）
    #   gap < 0 (test>train)  → 非平稳性 / concept drift（系统在 evolve，或数据分布变化）
    positive_gaps = [f.overfitting_gap_pp for f in fold_results if f.overfitting_gap_pp > 0]
    negative_gaps = [f.overfitting_gap_pp for f in fold_results if f.overfitting_gap_pp < 0]
    direction_hint = (
        "overfitting" if positive_gaps and sum(positive_gaps) > abs(sum(negative_gaps))
        else "nonstationary" if negative_gaps
        else "stable"
    )

    # 评级（基于绝对 gap）
    severity = (
        "severe" if max_abs_gap > 15
        else "moderate" if max_abs_gap > 8
        else "low" if max_abs_gap > 3
        else "none"
    )

    return {
        "total_samples": n_total,
        "cfg": asdict(cfg),
        "folds": [asdict(f) for f in fold_results],
        "summary": {
            "avg_test_wr": round(avg_test_wr, 2),
            "avg_test_sharpe": round(avg_test_sharpe, 3),
            "avg_gap_pp": round(avg_overfit, 2),
            "max_abs_gap_pp": round(max_abs_gap, 2),
            "severity": severity,
            "direction": direction_hint,   # overfitting / nonstationary / stable
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _print_report(result: Dict) -> None:
    if "error" in result:
        print(f"❌ {result['error']}")
        return

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  Alpha Hive — Walk-Forward 样本外验证报告            ║")
    print("╚══════════════════════════════════════════════════════╝\n")
    s = result["summary"]
    cfg = result["cfg"]
    print(f"  样本总数：{result['total_samples']}   | Folds: {cfg['folds']}")
    print(f"  切分比例：train {cfg['train_pct']:.0%} / test {cfg['test_pct']:.0%}")
    if cfg.get("purge_days"):
        print(f"  Purge gap：{cfg['purge_days']} 天")
    print()
    print("  ┌─── Per-Fold 详情 ───────────────────────────────────┐")
    for f in result["folds"]:
        sev = "⚠️ " if f["overfitting_gap_pp"] > 8 else "  "
        print(f"    Fold {f['fold_idx']}: train [{f['train_start']}..{f['train_end']}] n={f['train_n']:<3} WR {f['train_wr']:>5.1f}%  "
              f"| test [{f['test_start']}..{f['test_end']}] n={f['test_n']:<3} WR {f['test_wr']:>5.1f}%  "
              f"Sharpe {f['test_sharpe']:>+6.2f}  PnL ${f['test_pnl_usd']:>+8.0f}  "
              f"{sev}Gap {f['overfitting_gap_pp']:+.1f}pp")
    print("  └─────────────────────────────────────────────────────┘\n")

    sev_icon = {
        "severe":   "🔴 严重",
        "moderate": "🟠 中度",
        "low":      "🟡 轻度",
        "none":     "🟢 稳定",
    }.get(s["severity"], "?")
    dir_label = {
        "overfitting":   "🎯 过拟合 (train>test) — optimizer 学到训练期噪声",
        "nonstationary": "🌊 非平稳 (test>train) — 数据分布漂移或系统在 evolve",
        "stable":        "✅ 稳定",
    }.get(s["direction"], "?")
    print(f"  平均测试 WR        : {s['avg_test_wr']:.2f}%")
    print(f"  平均测试 Sharpe    : {s['avg_test_sharpe']:+.3f}")
    print(f"  平均 gap           : {s['avg_gap_pp']:+.2f}pp (train WR − test WR)")
    print(f"  最大 |gap|         : {s['max_abs_gap_pp']:.2f}pp")
    print(f"  偏差评级           : {sev_icon}")
    print(f"  偏差方向           : {dir_label}")
    print()
    print("  💡 解读：")
    print("     • gap > 0：训练好、测试差 → 经典过拟合")
    print("     • gap < 0：训练差、测试好 → 样本非平稳，近期表现好可能只是运气 / 样本偏差")
    print("     • |gap| < 3pp：分布一致，模型鲁棒")
    print()


def main():
    parser = argparse.ArgumentParser(description="Alpha Hive Walk-Forward Validator")
    parser.add_argument("--train-pct", type=float, default=0.70)
    parser.add_argument("--test-pct", type=float, default=0.30)
    parser.add_argument("--folds", type=int, default=1, help=">1 启用 rolling k-fold")
    parser.add_argument("--purge-days", type=int, default=0)
    parser.add_argument("--min-train", type=int, default=40)
    parser.add_argument("--min-test", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = WalkForwardConfig(
        train_pct=args.train_pct,
        test_pct=args.test_pct,
        folds=args.folds,
        purge_days=args.purge_days,
        min_train_samples=args.min_train,
        min_test_samples=args.min_test,
    )
    result = run_walk_forward(cfg)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_report(result)
    sys.exit(0 if "error" not in result else 2)


if __name__ == "__main__":
    main()
