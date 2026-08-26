#!/usr/bin/env python3
"""
🐝 Alpha Hive — 横截面 rank-IC 统计功效计算 (v0.44.0)
=====================================================
回答一个具体问题：**标的池 10 → 30 之后，得到统计结论需要的日历时间缩短了几倍？**

为什么需要这个工具
------------------
2026-08-16 的会话里给出过一个估计：N_eff 从 3.25 涨到 13.8，横截面 IC 的标准误
∝ 1/√N_eff，所以精度提升约 2 倍、时间缩短约 **4 倍**。那个数字是在两个 N_eff
实测值上做的心算，有两个未经检验的假设：

  A. 用 N_eff 还是 (N_eff − 1)？前者给 13.8/3.25 = 4.25×，后者给
     12.8/2.25 = 5.69× —— 相差 34%，光靠心算分辨不出来。
  B. **更要紧**：那个推算隐含假设「周度 IC 的波动全部来自横截面抽样噪音」。
     现实里 IC 还有真实的时间变异（因子的有效性本身随时间变）。若记
        Var(IC_week) = σ_t²(时间变异，不可约) + σ_cs²(横截面抽样，∝1/N_eff)
     那么扩池只能压第二项。**σ_t² 一旦不可忽略，4 倍就会塌下来** ——
     极端情形 σ_t² 占主导时，扩池对日历时间几乎没有帮助。

本工具把 A 算清楚，并用真实数据估 B。

方法
----
1. **N_eff**：拉两个池子的日收益，取上三角平均两两相关 ρ̄，
   `N_eff = n / (1 + (n−1)·ρ̄)`（等相关近似）。会顺手复核记忆里的 3.25 / 13.8。
2. **σ_IC 实测**：从 pheromone.db 取不重叠周度 IC 序列，直接算样本方差
   —— 这一步不含任何模型假设，是 10 只时代的真实读数。
3. **σ_cs² 实测**：置换检验。保留真实的日期骨架、横截面宽度、以及**真实收益的
   截面相关结构**，只把分数换成随机数（与 `ic_diagnostics.noise_floor` 同法），
   得到 H0 下周度 IC 的方差。比理论式 1/(N_eff−1) 可靠，因为它不需要等相关假设。
4. **分解**：σ_t² = max(0, σ_IC² − σ_cs²)。
5. **外推到 30 只**：σ_cs² 按 (N_eff₁₀−1)/(N_eff₃₀−1) 缩放 —— 这是全流程**唯一**
   的建模步骤（30 只时代还没有到期的 T+7 样本，无法实测）。同时给出按原始 n
   缩放的对照，作为增益下界。
6. **功效**：W = ((z_{1−α/2} + z_β) · σ_IC / |IC|)²，α=0.05 双侧、power=80%。

⚠️ 已知精度限制：σ_IC² 只由约 20 个不重叠周估出，样本方差自身的相对标准误
   ≈ √(2/(n−1)) ≈ 32%。所以本工具的倍数结论应读作量级，不是三位有效数字。

用法
----
    /usr/local/bin/python3 experiments/ic_power_analysis.py
    /usr/local/bin/python3 experiments/ic_power_analysis.py --json
    /usr/local/bin/python3 experiments/ic_power_analysis.py --draws 500
    /usr/local/bin/python3 experiments/ic_power_analysis.py --no-network  # 用记忆里的 N_eff
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path
from statistics import NormalDist
from typing import Dict, List, Optional, Tuple

ALPHAHIVE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ALPHAHIVE_DIR))

from ic_diagnostics import (  # noqa: E402  - 必须在 sys.path 注入后导入
    DIMS,
    HORIZONS,
    _load_prices,
    load_daily_ic,
    spearman,
    subsample_non_overlapping,
)

DB_PATH = ALPHAHIVE_DIR / "pheromone.db"

# 两个标的池（唯一真相 = alpha_hive_daily_report.py --tickers 的 default）
POOL_10 = ["NVDA", "TSLA", "MSFT", "QCOM", "VKTX", "META", "BILI", "AMZN", "RKLB", "CRCL"]
POOL_30 = POOL_10 + [
    "CVX", "VZ", "JNJ", "XOM", "COST", "BRK-B", "AMC", "ABBV", "T", "DELL",
    "DE", "CRM", "MU", "WMT", "TMO", "TMUS", "ENPH", "NFLX", "NEE", "SNOW",
]

# 记忆里的实测值（v0.42.9 扩池时算的），用于交叉复核本工具的 N_eff
NEFF_ON_RECORD = {10: 3.25, 30: 13.8}

ALPHA = 0.05        # 双侧显著性水平
POWER = 0.80        # 目标功效
WEEKS_PER_YEAR = 52  # T+7 不重叠取样 = 每周一个观测

# 用于功效表的候选真实 IC。0.090 = 系统综合分实测；0.135 = 20 日动量基准
IC_GRID = [0.05, 0.077, 0.090, 0.135, 0.20]


# ────────────────────────────────────────────────────────────────────────────
# 第 1 步：N_eff
# ────────────────────────────────────────────────────────────────────────────

def _pairwise_corr_mean(returns: Dict[str, Dict[str, float]]) -> Tuple[float, int, int]:
    """上三角平均两两 Pearson 相关。返回 (ρ̄, 参与标的数, 配对数)。

    ⚠️ 必须**按日期取交集**，不能靠截尾长度对齐。实测本池 30 只的有效观测
    天数是 247/249/250 三种（DE 247、VKTX 249、其余 250），用 `a[-n:]` 那种
    截尾会把涉及 DE/VKTX 的配对错位最多 3 天 —— 相关系数被无声地算错，
    而 ρ̄ 直接决定 N_eff、N_eff 直接决定本工具的头条倍数。

    也不用"全池共同日期"的全局交集：那会被覆盖最差的单只标的拖累所有配对。
    逐对取交集，各对用各自最大的可用样本。
    """
    names = sorted(returns)
    vals: List[float] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ra, rb = returns[names[i]], returns[names[j]]
            common = sorted(set(ra) & set(rb))
            if len(common) < 30:
                continue
            xa = [ra[d] for d in common]
            xb = [rb[d] for d in common]
            try:
                c = statistics.correlation(xa, xb)
            except (statistics.StatisticsError, ValueError, ZeroDivisionError):
                continue
            if math.isfinite(c):
                vals.append(c)
    if not vals:
        return float("nan"), len(names), 0
    return statistics.fmean(vals), len(names), len(vals)


def n_eff(n: int, rho_bar: float) -> float:
    """等相关近似下的有效独立标的数：n / (1 + (n−1)·ρ̄)。

    ρ̄ ≤ 0 时公式会给出 > n 的荒谬值（分母 < 1），钳到 n —— 独立标的数
    不可能超过标的数本身。
    """
    denom = 1.0 + (n - 1) * rho_bar
    if denom <= 0:
        return float(n)
    return min(float(n), n / denom)


def compute_neff(start: str, end: str) -> Optional[Dict]:
    """拉行情算两个池子的 N_eff。任何失败返回 None（调用方降级到 --no-network）。"""
    px = _load_prices(POOL_30, start, end)
    if px is None:
        return None

    # 防御 yfinance 的形状陷阱：多标的时 ["Close"] 给 DataFrame（列=ticker），
    # 单标的时给 Series。这里恒为多标的，但仍显式检查而非假设
    # （本项目 v0.43.12 一天内因这个坑连撞三次，见 MEMORY yfinance-multiindex）。
    if not hasattr(px, "columns"):
        print("⚠️  行情返回不是 DataFrame（单标的形状？），N_eff 跳过", file=sys.stderr)
        return None

    # {ticker: {日期: 收益}} —— 必须带日期，供逐对取交集（见 _pairwise_corr_mean）
    returns: Dict[str, Dict[str, float]] = {}
    for t in POOL_30:
        if t not in px.columns:
            continue
        col = px[t].dropna()
        if len(col) < 31:
            continue
        dates = [str(d)[:10] for d in col.index]
        closes = [float(v) for v in col.tolist()]
        rets: Dict[str, float] = {}
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                # 用后一日日期给收益打标 —— 只要全标的口径一致即可
                rets[dates[i]] = closes[i] / closes[i - 1] - 1.0
        if len(rets) >= 30:
            returns[t] = rets

    out: Dict = {"window": [start, end], "n_with_data": len(returns)}
    for label, pool in (("pool_10", POOL_10), ("pool_30", POOL_30)):
        sub = {t: r for t, r in returns.items() if t in pool}
        rho, n_names, n_pairs = _pairwise_corr_mean(sub)
        out[label] = {
            "n_requested": len(pool),
            "n_with_data": n_names,
            "n_pairs": n_pairs,
            "rho_bar": rho,
            "n_eff": n_eff(n_names, rho) if math.isfinite(rho) else float("nan"),
        }
    return out


# ────────────────────────────────────────────────────────────────────────────
# 第 2~3 步：σ_IC 实测 + σ_cs² 置换实测
# ────────────────────────────────────────────────────────────────────────────

def observed_weekly_var(db_path: Path, horizon: str = "t7",
                        min_width: int = 5) -> Dict[str, Dict]:
    """各维度不重叠周度 IC 序列的样本方差（实测，无模型假设）。"""
    target_col, checked_col, _, period = HORIZONS[horizon]
    ic_by_dim, n_rows, widths = load_daily_ic(
        db_path, target_col, checked_col, min_width=min_width,
        target="price", horizon=horizon,
    )
    out: Dict[str, Dict] = {}
    for dim in DIMS:
        series = ic_by_dim.get(dim, {})
        weekly = subsample_non_overlapping(series, period)
        if len(weekly) < 3:
            out[dim] = {"n_weeks": len(weekly), "var": float("nan"),
                        "mean_ic": float("nan")}
            continue
        out[dim] = {
            "n_weeks": len(weekly),
            "var": statistics.variance(weekly),
            "sd": statistics.stdev(weekly),
            "mean_ic": statistics.fmean(weekly),
        }
    return {
        "by_dim": out,
        "n_rows": n_rows,
        "n_days": len(widths),
        "mean_width": statistics.fmean(widths) if widths else float("nan"),
        "period": period,
    }


def _load_day_pairs(db_path: Path, horizon: str, dim: str,
                    min_width: int = 5) -> Dict[str, List[Tuple[float, float]]]:
    """{date: [(维度分, 前瞻收益), ...]} —— 置换检验的骨架。

    自己读库而不复用 build_benchmark_panel：后者返回的是「因子名 → 面板」的
    全量字典（含 47 个 signal_archive 因子），这里只要一个维度，省一次全表扫。
    """
    import sqlite3
    from collections import defaultdict

    price_col = f"price_{horizon}"
    _, checked_col, _, _ = HORIZONS[horizon]
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT date, dimension_scores, price_at_predict, {price_col} AS p_end "
            f"FROM predictions WHERE {checked_col}=1 AND {price_col} IS NOT NULL "
            f"  AND price_at_predict > 0 AND dimension_scores IS NOT NULL"
        ).fetchall()
    finally:
        con.close()

    by_day: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for r in rows:
        try:
            ds = json.loads(r["dimension_scores"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ds, dict) or dim not in ds:
            continue
        p0, p1 = r["price_at_predict"], r["p_end"]
        if not p0 or not p1 or p0 <= 0:
            continue
        try:
            score = float(ds[dim])
        except (TypeError, ValueError):
            continue
        by_day[r["date"][:10]].append((score, (p1 / p0 - 1.0) * 100.0))

    # 过滤条件必须与 `ic_diagnostics.load_daily_ic` **逐字一致**，否则 σ_cs²
    # 与 σ_IC² 算在不同的天集合上，分解就没有意义。那边除了 min_width 还要求
    # **当日该维度至少 2 个不同分数**（`len(set(...)) < 2 → continue`）——
    # 对 catalyst 这种高度并列的维度，缺这一条会多纳入若干"全员同分"的日子。
    return {
        d: p for d, p in by_day.items()
        if len(p) >= min_width and len({s for s, _ in p}) >= 2
    }


def _distinct_ratio(by_day: Dict[str, List[Tuple[float, float]]]) -> float:
    """分数的去重比 = distinct(分数) / 总数。<0.25 表示大量并列。

    记录它是因为并列程度直接决定 σ_cs² 的大小 —— 这是本工具最初用
    `noise_floor` 的随机分数时算错的根源，留在输出里便于复核。
    """
    all_scores = [s for pairs in by_day.values() for s, _ in pairs]
    if not all_scores:
        return float("nan")
    return len(set(all_scores)) / len(all_scores)


def permuted_weekly_var(by_day: Dict[str, List[Tuple[float, float]]],
                        period: str, draws: int) -> Dict:
    """置换零分布下周度 IC 的方差 = 横截面抽样方差 σ_cs²（实测）。

    关键：**打乱真实分数向量本身**，保留日期骨架、每日宽度、当日真实收益向量，
    以及分数的边际分布（含并列结构）。因此标的间收益相关性与分数并列都被自然
    保留 —— 不需要等相关假设。

    ⚠️ 为什么不复用 `ic_diagnostics.noise_floor`：它把分数换成 `rng.random()`
    ——**无并列值**的连续均匀分布。真实维度分大量并列（catalyst 有 55% 恰为
    6.0，多个信号 `distinct_ratio<0.25`），而并列会压低 Spearman 的方差。
    实测差别足以翻转结论：用 rng.random() 得 σ_cs²=0.127，比 4/5 个维度的
    实测总方差还大，分解必然得到 σ_t²=0（被钳）；改成置换真实分数后才可比。
    noise_floor 的做法对它自己的用途（跨异质因子的统一地板）是对的，
    对方差分解则会系统性高估。
    """
    variances: List[float] = []
    for i in range(draws):
        rng = random.Random(20260816 + i)
        ic_by_day: Dict[str, float] = {}
        for d, pairs in by_day.items():
            shuffled = [s for s, _ in pairs]
            rng.shuffle(shuffled)
            c = spearman(shuffled, [ret for _, ret in pairs])
            if c is not None:
                ic_by_day[d] = c
        weekly = subsample_non_overlapping(ic_by_day, period)
        if len(weekly) >= 3:
            variances.append(statistics.variance(weekly))
    if not variances:
        return {"n_draws": 0, "var": float("nan")}
    variances.sort()
    return {
        "n_draws": len(variances),
        "var": statistics.fmean(variances),
        "var_p05": variances[int(0.05 * len(variances))],
        "var_p95": variances[min(len(variances) - 1, int(0.95 * len(variances)))],
    }


# ────────────────────────────────────────────────────────────────────────────
# 第 6 步：功效
# ────────────────────────────────────────────────────────────────────────────

def weeks_for_power(ic_true: float, sd_ic: float,
                    alpha: float = ALPHA, power: float = POWER) -> float:
    """检出 |IC|=ic_true 所需的不重叠周数。

    单样本均值检验：W = ((z_{1−α/2} + z_β) · σ_IC / IC)²
    """
    if ic_true <= 0 or not math.isfinite(sd_ic) or sd_ic <= 0:
        return float("nan")
    nd = NormalDist()
    z_a = nd.inv_cdf(1 - alpha / 2)
    z_b = nd.inv_cdf(power)
    return ((z_a + z_b) * sd_ic / ic_true) ** 2


# ────────────────────────────────────────────────────────────────────────────
# 输出
# ────────────────────────────────────────────────────────────────────────────

def _f(x, spec="+.4f"):
    return "  n/a  " if x is None or not math.isfinite(x) else format(x, spec)


def main() -> int:
    ap = argparse.ArgumentParser(description="横截面 rank-IC 统计功效计算")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--horizon", choices=["t7", "t30"], default="t7")
    ap.add_argument("--draws", type=int, default=300,
                    help="置换检验次数（默认 300）")
    ap.add_argument("--min-width", type=int, default=5)
    ap.add_argument("--start", default="2025-08-16", help="N_eff 行情窗口起点")
    ap.add_argument("--end", default="2026-08-16", help="N_eff 行情窗口终点")
    ap.add_argument("--no-network", action="store_true",
                    help="不拉行情，直接用记忆里的 N_eff (3.25 / 13.8)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ 找不到 {db_path}", file=sys.stderr)
        return 2

    result: Dict = {"horizon": args.horizon, "alpha": ALPHA, "power": POWER}

    # ── 第 1 步：N_eff ──────────────────────────────────────────────────
    if args.no_network:
        neff = {
            "source": "on_record",
            "pool_10": {"n_eff": NEFF_ON_RECORD[10], "n_with_data": 10},
            "pool_30": {"n_eff": NEFF_ON_RECORD[30], "n_with_data": 30},
        }
    else:
        measured = compute_neff(args.start, args.end)
        if measured is None:
            print("⚠️  行情不可用，降级到记忆里的 N_eff", file=sys.stderr)
            neff = {
                "source": "on_record_fallback",
                "pool_10": {"n_eff": NEFF_ON_RECORD[10], "n_with_data": 10},
                "pool_30": {"n_eff": NEFF_ON_RECORD[30], "n_with_data": 30},
            }
        else:
            measured["source"] = "measured"
            neff = measured
    result["neff"] = neff

    neff10 = neff["pool_10"]["n_eff"]
    neff30 = neff["pool_30"]["n_eff"]

    # ── 第 2 步：实测周度 IC 方差 ───────────────────────────────────────
    obs = observed_weekly_var(db_path, args.horizon, args.min_width)
    result["observed"] = obs

    # ── 第 3 步：置换实测 σ_cs²（逐维度：并列结构因维度而异）────────────
    perm_by_dim: Dict[str, Dict] = {}
    for dim in DIMS:
        by_day = _load_day_pairs(db_path, args.horizon, dim, args.min_width)
        p = permuted_weekly_var(by_day, obs["period"], args.draws)
        p["skeleton_days"] = len(by_day)
        p["distinct_ratio"] = _distinct_ratio(by_day)
        perm_by_dim[dim] = p
    result["permuted"] = perm_by_dim

    # ── 第 4~5 步：分解与外推 ───────────────────────────────────────────
    # 两种缩放：N_eff（主口径）与原始 n（增益下界对照）
    scale_neff = ((neff10 - 1) / (neff30 - 1)) if neff30 > 1 else float("nan")
    n10 = neff["pool_10"].get("n_with_data", 10)
    n30 = neff["pool_30"].get("n_with_data", 30)
    scale_raw = (n10 - 1) / (n30 - 1)

    decomp: Dict[str, Dict] = {}
    for dim in DIMS:
        var_obs = obs["by_dim"][dim].get("var", float("nan"))
        var_cs_10 = perm_by_dim[dim]["var"]
        if not math.isfinite(var_obs) or not math.isfinite(var_cs_10):
            continue
        var_t = max(0.0, var_obs - var_cs_10)
        # 模型方差：σ_IC² = σ_t² + σ_cs²。因 var_t 在 0 处被截断，
        # var_t + var_cs_10 == max(var_obs, var_cs_10) —— 对 var_obs < var_cs 的
        # 维度这是保守取值（用较大的零分布方差，不用偏小的实测值）。
        # 倍数必须分子分母同走模型，否则「实测/模型」混用会系统性低估增益。
        var_model_10 = var_t + var_cs_10
        row = {
            "var_obs": var_obs,
            "var_cs_10": var_cs_10,
            "var_t": var_t,
            "var_model_10": var_model_10,
            "censored": var_obs < var_cs_10,   # σ_t² 撞到 0 边界
            "t_share": var_t / var_model_10 if var_model_10 > 0 else float("nan"),
            "sd_ic_10": math.sqrt(var_model_10),
            "sd_ic_10_obs": math.sqrt(var_obs),
        }
        for tag, sc in (("neff", scale_neff), ("raw_n", scale_raw)):
            if not math.isfinite(sc):
                continue
            var30 = var_t + var_cs_10 * sc
            row[f"sd_ic_30_{tag}"] = math.sqrt(var30)
            row[f"multiplier_{tag}"] = (
                (var_model_10 / var30) if var30 > 0 else float("nan")
            )
        decomp[dim] = row
    result["decomposition"] = decomp
    result["scales"] = {"neff": scale_neff, "raw_n": scale_raw}

    # ── 第 6 步：功效表 ─────────────────────────────────────────────────
    # 两个口径对比：
    #   optimistic = 假设 σ_t²=0（IC 波动全是横截面噪音）→ 扩池增益的**上界**
    #   实测       = 用该维度分解出的 σ_t²                → 现实增益
    power_tbl: Dict[str, Dict] = {}
    for dim, row in decomp.items():
        sd10 = row["sd_ic_10"]
        sd30 = row.get("sd_ic_30_neff", float("nan"))
        v_cs = row["var_cs_10"]
        sd10_opt = math.sqrt(v_cs)
        sd30_opt = (math.sqrt(v_cs * scale_neff)
                    if math.isfinite(scale_neff) else float("nan"))
        power_tbl[dim] = {
            str(ic): {
                "weeks_10": weeks_for_power(ic, sd10),
                "weeks_30": weeks_for_power(ic, sd30),
                "weeks_10_optimistic": weeks_for_power(ic, sd10_opt),
                "weeks_30_optimistic": weeks_for_power(ic, sd30_opt),
            }
            for ic in IC_GRID
        }
    result["power"] = power_tbl

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    # ── 表格输出 ────────────────────────────────────────────────────────
    print("━" * 74)
    print("🐝 Alpha Hive · 横截面 rank-IC 统计功效计算")
    print("━" * 74)
    _draws_done = max((p.get("n_draws", 0) for p in perm_by_dim.values()), default=0)
    print(f"  horizon={args.horizon}  α={ALPHA}  power={POWER:.0%}  "
          f"置换次数={_draws_done}")
    print()

    print("【第 1 步】N_eff —— 有效独立标的数")
    print(f"  来源: {neff['source']}")
    if neff["source"] == "measured":
        print(f"  行情窗口: {neff['window'][0]} → {neff['window'][1]}"
              f"  (拿到数据的标的 {neff['n_with_data']}/30)")
        for label, tag in (("pool_10", "原核心 10 只"), ("pool_30", "扩池 30 只")):
            d = neff[label]
            print(f"  {tag}: n={d['n_with_data']}  ρ̄={_f(d['rho_bar'], '+.4f')}  "
                  f"配对={d['n_pairs']}  →  N_eff = {_f(d['n_eff'], '.2f')}"
                  f"   (记忆值 {NEFF_ON_RECORD[10 if label == 'pool_10' else 30]})")
    else:
        print(f"  pool_10 N_eff={neff10}   pool_30 N_eff={neff30}  (未实测)")
    print()

    print("【第 2~3 步】周度 IC 方差：实测 vs 置换零分布")
    print(f"  DB 覆盖: {obs['n_days']} 个业务日, 日均横截面宽度 "
          f"{_f(obs['mean_width'], '.1f')}, 共 {obs['n_rows']} 行")
    print("  σ_cs² 用**置换真实分数**得到（保留并列结构），逐维度分别算")
    print()
    print("  维度        周数  去重比  实测方差   σ_cs²(置换)  σ_t²(不可约)  时间变异占比")
    print("  " + "─" * 76)
    for dim in DIMS:
        o = obs["by_dim"][dim]
        d = decomp.get(dim)
        p = perm_by_dim.get(dim, {})
        if not d:
            print(f"  {dim:11s} {o['n_weeks']:>4d}      —— 样本不足")
            continue
        mark = " ⚠钳0" if d["censored"] else ""
        print(f"  {dim:11s} {o['n_weeks']:>4d}  {_f(p.get('distinct_ratio'), '.3f')}  "
              f"{_f(d['var_obs'], '.5f')}   {_f(d['var_cs_10'], '.5f')}    "
              f"{_f(d['var_t'], '.5f')}      {_f(d['t_share'], '.1%')}{mark}")
    print()
    n_cens = sum(1 for d in decomp.values() if d["censored"])
    if n_cens:
        print(f"  ⚠钳0 = 实测方差 < 置换零分布方差 ({n_cens}/{len(decomp)} 个维度)，")
        print("       σ_t² 的点估计撞在 0 边界上 → 读作「检测不到时间变异」，")
        print("       而非「已证明时间变异为零」。20 余个周的方差自身误差就有 ~32%。")
        print()

    print("【第 4~5 步】扩池后的时间倍数（同一置信度所需周数之比）")
    print(f"  σ_cs² 缩放系数: N_eff 口径 ×{_f(scale_neff, '.4f')}  "
          f"/ 原始 n 口径 ×{_f(scale_raw, '.4f')}")
    print()
    print("  维度        σ_IC(10只)  σ_IC(30只)   倍数(N_eff口径)  倍数(原始n口径)")
    print("  " + "─" * 70)
    for dim in DIMS:
        d = decomp.get(dim)
        if not d:
            continue
        print(f"  {dim:11s}  {_f(d['sd_ic_10'], '.4f')}     "
              f"{_f(d.get('sd_ic_30_neff'), '.4f')}       "
              f"{_f(d.get('multiplier_neff'), '.2f')}×           "
              f"{_f(d.get('multiplier_raw_n'), '.2f')}×")
    print()

    print("【第 6 步】达到 80% 功效所需的不重叠周数（≈ 日历周，需每周至少一次扫描）")
    print("            ┌── σ_t²=0 最乐观 ──┐  ┌── 含实测 σ_t² ──┐")
    print("  真实|IC|    10只      30只       10只      30只     30只年数")
    print("  " + "─" * 66)
    def _med(key: str, ic_key: str) -> float:
        """各维度取中位数，避免被单一维度带偏（σ_cs² 现在逐维度不同）。"""
        vals = [v[ic_key][key] for v in power_tbl.values()
                if math.isfinite(v[ic_key][key])]
        return statistics.median(vals) if vals else float("nan")

    ref = next(iter(power_tbl.values())) if power_tbl else {}
    for ic in IC_GRID:
        k = str(ic)
        if k not in ref:
            continue
        o10 = _med("weeks_10_optimistic", k)
        o30 = _med("weeks_30_optimistic", k)
        w10 = _med("weeks_10", k)
        w30 = _med("weeks_30", k)
        yrs = w30 / WEEKS_PER_YEAR if math.isfinite(w30) else float("nan")
        note = ""
        if ic == 0.090:
            note = "  ← 系统综合分实测"
        elif ic == 0.135:
            note = "  ← 20日动量基准"
        elif ic == 0.077:
            note = "  ← 噪音地板"
        print(f"   {ic:<8.3f} {_f(o10, '7.0f')}  {_f(o30, '7.0f')}    "
              f"{_f(w10, '7.0f')}  {_f(w30, '7.0f')}   {_f(yrs, '6.1f')}{note}")
    print()
    print("━" * 74)
    print("⚠️  σ_IC² 仅由 ~20 个不重叠周估出，其自身相对标准误 ≈ "
          f"{_f(math.sqrt(2 / max(1, min(o['n_weeks'] for o in obs['by_dim'].values()) - 1)), '.0%')}"
          " —— 倍数结论应读作量级")
    print("━" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
