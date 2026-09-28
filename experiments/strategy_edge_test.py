"""策略层优势检验 · 执行器（预注册见 `experiments/strategy_edge_prereg.md`，v0.45.370）。

问的只有一件事：**网站上那套策略，在当前世代里，每笔的期望收益是不是大于 0？**
答「是」之前，任何加仓 / 放大都没有依据（半凯利在优势≈0 时就是≈0 仓位）。

三条设计，全部写死在 `PREREG`（由 `tests/test_strategy_edge_test.py` 与预注册文档逐项对钉）：

1. **单位 = ISO 周**。同一周的交易共享同一段大盘，彼此不独立；每周取入场交易
   方向调整后 gross 收益的均值，作为一个观测。没有入场交易的周不算单位。
2. **样本 = 当前世代的前 `n_weeks` 个单位，一个都不多**。这就是「只跑一次」的实现方式：
   就绪之后再跑多少次，读到的都是同一批周，答案不变 —— 不需要冻结文件，也没有
   「多攒几周再看看」的余地（那正是 optional stopping）。
3. **未就绪时盲化**：只返回周数 / 笔数计数，**不返回任何均值、t、p**。
   `BLINDED_KEYS` 列出的字段在未就绪时出现即违约（测试钉住）。

世代起点取 `ic_rerun_readiness.cohort_start()`（`_COHORT_HISTORY` 最后一条）——
评分逻辑每换一代，计数从零开始。这是有意的：旧世代的收益回答不了新代码有没有优势。

用法
----
    /usr/local/bin/python3 experiments/strategy_edge_test.py          # 人读
    /usr/local/bin/python3 experiments/strategy_edge_test.py --json

退出码：0 = 已就绪并给出判定；1 = 未就绪（正常状态，继续攒）；3 = 无法判定（回测失败等）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev
from typing import Dict, List, Optional

from scipy import stats as _scipy_stats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 代码锚点：import 仓内模块

#: 预注册常量唯一真相（文档 §7 的 prereg-constants 块逐项对钉）
PREREG: Dict = {
    "version": 1,
    "n_weeks": 52,
    "alpha": 0.05,
    "alternative": "greater",
    "null_mean_pct": 0.0,
    "min_trades_per_unit": 1,
    # 一周的周日过后这么多天才算「已结清」可入样本（T+7 结算 + 回填滞后；同维度 IC 协议）。
    # 没有这条，第 52 周可能只结算了一半，就绪后它的均值还会变 —— 「只跑一次」就不成立。
    "settle_grace_days": 21,
    # 被检验的策略 = 网站门面回测的配置（dashboard_renderer 里 _BC(...) 的实参）。
    # 其余字段取 BacktestConfig 默认值 —— 默认值变了由测试逐项对钉抓出。
    "backtest_kwargs": {
        "exclude_nontrading_days": True,
        "apply_trading_costs": False,
    },
}

#: 未就绪时绝不允许出现的字段（盲化）
BLINDED_KEYS = frozenset({"mean_pct", "t", "p", "decision", "weekly_means", "sd_pct"})


def _iso_week(date_str: str) -> str:
    y, w, _ = dt.date.fromisoformat(date_str[:10]).isocalendar()
    return f"{y}-W{w:02d}"


def _week_settled(date_str: str, as_of: dt.date) -> bool:
    d = dt.date.fromisoformat(date_str[:10])
    sunday = d + dt.timedelta(days=6 - d.weekday())
    return sunday + dt.timedelta(days=PREREG["settle_grace_days"]) <= as_of


def weekly_units(trades: List[Dict], cohort_date: str, as_of: dt.date) -> List[Dict]:
    """把入场交易按入场日 ISO 周聚合成单位，按周升序。

    只收 `entry >= cohort_date` 且所在周已结清（`_week_settled`）的交易；`gross_pct` 已是方向调整后的收益（%）。
    `WINDOW_CUTOFF`（回测窗口结束时未到期、按 0 收益强平）不是真实结局，不收 ——
    否则最近几周会被一串假 0 拉向零。
    """
    buckets: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        if (t.get("entry") or "") < cohort_date:
            continue
        if not _week_settled(t["entry"], as_of):
            continue
        if str(t.get("exit_reason") or "").upper() == "WINDOW_CUTOFF":
            continue
        g = t.get("gross_pct")
        if g is None:
            continue
        buckets[_iso_week(t["entry"])].append(float(g))
    units = [{"week": w, "n": len(v), "mean_pct": mean(v)}
             for w, v in buckets.items() if len(v) >= PREREG["min_trades_per_unit"]]
    return sorted(units, key=lambda u: u["week"])


def evaluate(units: List[Dict]) -> Dict:
    """就绪后的检验：前 n_weeks 个单位的周均值做单样本 t（单侧，H1: 均值 > 0）。"""
    n_req = PREREG["n_weeks"]
    sample = units[:n_req]
    xs = [u["mean_pct"] for u in sample]
    m, sd = mean(xs), stdev(xs)
    df = len(xs) - 1
    t = (m - PREREG["null_mean_pct"]) / (sd / math.sqrt(len(xs))) if sd > 0 else math.inf
    p = float(_scipy_stats.t.sf(t, df))
    passed = p < PREREG["alpha"] and m > PREREG["null_mean_pct"]
    return {
        "first_week": sample[0]["week"], "last_week": sample[-1]["week"],
        "n_units": len(xs), "n_trades": sum(u["n"] for u in sample),
        "mean_pct": round(m, 4), "sd_pct": round(sd, 4), "t": round(t, 4), "p": round(p, 6),
        "weekly_means": [round(x, 4) for x in xs],
        "decision": ("检出优势：可按预注册 §6 考虑半凯利放大" if passed
                     else "未检出优势：维持最小仓位，不加仓（不等于证明无优势，见 §5 MDE）"),
    }


def assess(trades: Optional[List[Dict]] = None, cohort: Optional[Dict] = None,
           as_of: Optional[dt.date] = None) -> Dict:
    """就绪闸 + 检验。`trades` / `cohort` / `as_of` 可注入（测试 / 探针）；缺省跑真实回测、世代表与今天。"""
    as_of = as_of or dt.date.today()
    if cohort is None:
        import ic_rerun_readiness
        cohort = ic_rerun_readiness.cohort_start()
    if trades is None:
        import portfolio_backtest as pb
        try:
            result = pb.run_backtest(pb.BacktestConfig(**PREREG["backtest_kwargs"]))
        except Exception as e:   # 找不到库等：显式「无法判定」（退出码 3），不冒充「未就绪」
            return {"status": "undetermined", "reason": f"{type(e).__name__}: {e}", "cohort": cohort}
        if "error" in result:
            return {"status": "undetermined", "reason": result["error"], "cohort": cohort}
        trades = result.get("all_trades") or []

    units = weekly_units(trades, cohort["date"], as_of)
    out = {
        "prereg_version": PREREG["version"],
        "cohort": {"date": cohort["date"], "version": cohort.get("version")},
        "as_of": as_of.isoformat(),
        "n_units": len(units), "n_units_required": PREREG["n_weeks"],
        "n_trades": sum(u["n"] for u in units),
    }
    if len(units) < PREREG["n_weeks"]:
        out["status"] = "not_ready"
        return out          # 盲化：不算、不返回任何效应量
    out["status"] = "ready"
    out.update(evaluate(units))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="策略层优势检验（预注册，只读）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = assess()
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif r["status"] == "not_ready":
        print(f"未就绪：世代 {r['cohort']['date']}（{r['cohort']['version']}）起 "
              f"{r['n_units']}/{r['n_units_required']} 周，{r['n_trades']} 笔。效应量盲化中。")
    elif r["status"] == "ready":
        print(f"已就绪：{r['first_week']}~{r['last_week']} 共 {r['n_units']} 周 / {r['n_trades']} 笔\n"
              f"周均收益 {r['mean_pct']:+.3f}%  t={r['t']:+.2f}  p(单侧)={r['p']:.4f}\n→ {r['decision']}")
    else:
        print(f"无法判定：{r.get('reason')}")
    return {"ready": 0, "not_ready": 1}.get(r["status"], 3)


if __name__ == "__main__":
    sys.exit(main())
