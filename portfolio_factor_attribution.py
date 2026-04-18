#!/usr/bin/env python3
"""
🐝 Alpha Hive — 组合级 Fama-French 因子归因 (v0.22.0)
======================================================
单 ticker FF6 归因（`factor_attribution.py`）的升级版 — 对**整个交易策略**做归因。

核心问题
--------
"Alpha vs SPY = +1.08%" 是真 alpha 还是只是因子暴露（size/momentum/value）?
剥离 FF5+MOM 6 因子后，Jensen α 才是 stock-picking 的真实能力。

方法
----
1. 从 pheromone.db 读所有 checked_t7=1 记录 → 构造每笔交易的"持仓期日度贡献"
2. 对每个交易日，累加当日在仓所有交易的 $P&L，除以当日 NAV → 得策略日度收益率序列
3. 下载 Kenneth French FF5 + MOM（`load_ff6_factors` 复用）
4. OLS 回归：R_strategy = α + β_mkt·Mkt-RF + β_smb·SMB + β_hml·HML + β_rmw·RMW + β_cma·CMA + β_mom·MOM + ε
5. 输出：
   • **Jensen α**（年化，t-stat，p-value）— 真正的 "可持续 alpha"
   • **Information Ratio** (α / tracking_error)
   • **因子暴露** — 你真正在赌什么？
   • **残差自相关** — 看策略是否被未捕捉的因子驱动

约束与降级
---------
• 最少 25 个交易日观测才能做 FF6 完整回归（6 因子 + 截距 + 误差自由度）
• 观测不足时自动降级到 FF3（Mkt/SMB/HML）
• 观测 < 15 报错

用法
----
    python3 portfolio_factor_attribution.py
    python3 portfolio_factor_attribution.py --source all_trades  # 所有 210 笔
    python3 portfolio_factor_attribution.py --source filtered    # 仅 portfolio_backtest 过筛选
    python3 portfolio_factor_attribution.py --json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_log = logging.getLogger("alpha_hive.portfolio_factor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TRADING_DAYS = 252

# ══════════════════════════════════════════════════════════════════════════════
# 1. 交易数据 → 日度策略收益率序列
# ══════════════════════════════════════════════════════════════════════════════

def _load_trades(source: str = "all_trades") -> List[Dict]:
    """加载交易记录"""
    db = Path(__file__).parent / "pheromone.db"
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT date, ticker, direction, return_t7, net_return_t7,
                   exit_date, holding_days
            FROM predictions
            WHERE checked_t7 = 1 AND net_return_t7 IS NOT NULL
              AND exit_date IS NOT NULL AND holding_days IS NOT NULL
            ORDER BY date ASC
        """).fetchall()
    trades = [dict(r) for r in rows]
    _log.info(f"加载 {len(trades)} 笔交易")
    return trades


def _build_daily_returns(
    trades: List[Dict],
    initial_capital: float = 50_000.0,
    position_size_pct: float = 0.10,
) -> pd.Series:
    """
    构造策略日度收益率序列（每个交易日的 $P&L / $NAV）

    假设：等权 $NAV × 10% 每笔，持仓期内线性分摊 net_return_t7 到每个交易日
    （简化：真实 P&L 是非线性的，但此处聚焦"因子暴露"而非精确 NAV 轨迹）
    """
    if not trades:
        return pd.Series(dtype=float)

    # 每个交易日的 $P&L 累加
    daily_pnl: Dict[str, float] = defaultdict(float)

    for t in trades:
        entry = t["date"]
        exit_d = t["exit_date"]
        holding = max(int(t["holding_days"] or 1), 1)
        net_ret = float(t["net_return_t7"])  # 已扣成本的 %

        size_usd = initial_capital * position_size_pct
        total_pnl = size_usd * (net_ret / 100.0)
        daily_pnl_share = total_pnl / holding

        # 分摊到持仓期内的每个交易日
        try:
            entry_dt = datetime.strptime(entry, "%Y-%m-%d")
            exit_dt = datetime.strptime(exit_d, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        cur = entry_dt + timedelta(days=1)
        while cur <= exit_dt:
            # 跳过周末
            if cur.weekday() < 5:
                daily_pnl[cur.strftime("%Y-%m-%d")] += daily_pnl_share
            cur += timedelta(days=1)

    if not daily_pnl:
        return pd.Series(dtype=float)

    # 转 pd.Series, 对齐 NAV
    sorted_dates = sorted(daily_pnl.keys())
    cum_pnl = 0.0
    rows = []
    for d in sorted_dates:
        nav_prev = initial_capital + cum_pnl
        ret = daily_pnl[d] / nav_prev if nav_prev > 0 else 0.0
        cum_pnl += daily_pnl[d]
        rows.append((pd.Timestamp(d), ret))

    idx = pd.DatetimeIndex([r[0] for r in rows])
    ser = pd.Series([r[1] for r in rows], index=idx, name="strategy_ret")
    return ser


# ══════════════════════════════════════════════════════════════════════════════
# 2. FF6 因子数据加载（复用 factor_attribution.py）
# ══════════════════════════════════════════════════════════════════════════════

def _load_factors(start: pd.Timestamp, end: pd.Timestamp):
    """加载 FF6 因子（Mkt-RF, SMB, HML, RMW, CMA, MOM, RF）- Kenneth French"""
    from factor_attribution import load_ff6_factors
    return load_ff6_factors(start, end, force=False)


def _load_etf_factors(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """
    ETF 近似因子（修复 Kenneth French 数据滞后 1-2 个月的问题）
    实时可用于评估最近数据：
        Mkt-RF ≈ SPY - 3M T-bill (近似用 0 简化)
        SMB    ≈ IWM - SPY           (小市值 - 大市值)
        HML    ≈ IWD - IWF           (价值 - 成长)
        MOM    ≈ MTUM - SPY          (动量 - 大盘)
        QUAL   ≈ QUAL - SPY          (quality, 代替 RMW+CMA)
    """
    import yfinance as yf

    etfs = {
        "SPY": "SPY",    # 大盘
        "IWM": "IWM",    # 小市值
        "IWD": "IWD",    # Value
        "IWF": "IWF",    # Growth
        "MTUM": "MTUM",  # 动量
        "QUAL": "QUAL",  # 质量
    }
    data = {}
    for name, ticker in etfs.items():
        try:
            hist = yf.Ticker(ticker).history(
                start=start.date(), end=end.date(), auto_adjust=True
            )
            if hist.empty:
                _log.warning(f"ETF {ticker} 无数据")
                continue
            close = hist["Close"].astype(float)
            rets = close.pct_change().dropna()
            # tz-naive
            rets.index = pd.DatetimeIndex([pd.Timestamp(d.date()) for d in rets.index])
            data[name] = rets
        except Exception as e:
            _log.warning(f"ETF {ticker} 下载失败: {e}")

    if "SPY" not in data:
        raise ValueError("SPY 数据不可用")

    df = pd.DataFrame(data).dropna()

    # 构造因子收益率
    factors = pd.DataFrame(index=df.index)
    factors["Mkt-RF"] = df["SPY"]               # 简化：RF≈0（日度 RF 约 0.0002，影响微乎其微）
    factors["SMB"] = df["IWM"] - df["SPY"] if "IWM" in df else 0.0
    factors["HML"] = df["IWD"] - df["IWF"] if ("IWD" in df and "IWF" in df) else 0.0
    factors["MOM"] = df["MTUM"] - df["SPY"] if "MTUM" in df else 0.0
    factors["QUAL"] = df["QUAL"] - df["SPY"] if "QUAL" in df else 0.0
    factors["RF"] = 0.0  # 日度 RF 近似为 0（4.5%/252 ≈ 0.018% < 噪音）
    return factors


# ══════════════════════════════════════════════════════════════════════════════
# 3. OLS 回归（复用 factor_attribution._ols）
# ══════════════════════════════════════════════════════════════════════════════

def _regress(strategy_rets: pd.Series, ff: pd.DataFrame, model: str = "FF6") -> Dict:
    """
    对 strategy_rets 做因子回归
    model: "FF6" (Kenneth French 6 因子) | "FF3" (降级) | "ETF5" (ETF 近似 5 因子) | "CAPM" (SPY 单因子)
    """
    from factor_attribution import _ols

    # 对齐日期
    common = strategy_rets.index.intersection(ff.index)
    if len(common) < 15:
        return {"error": f"观测日期过少 ({len(common)} < 15)", "n": len(common)}

    ff_al = ff.loc[common]
    stra_al = strategy_rets.loc[common]

    # 超额收益 = 策略收益 - 无风险利率
    rf = ff_al["RF"].values if "RF" in ff_al.columns else np.zeros(len(common))
    y = stra_al.values - rf

    # 因子列选择
    if model == "FF6" and len(common) >= 25:
        factor_cols = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "MOM"]
    elif model == "ETF5" and len(common) >= 22:
        factor_cols = ["Mkt-RF", "SMB", "HML", "MOM", "QUAL"]
    elif model == "CAPM":
        factor_cols = ["Mkt-RF"]
    elif len(common) >= 20:
        factor_cols = ["Mkt-RF", "SMB", "HML"]
        model = "FF3"
    else:
        factor_cols = ["Mkt-RF"]
        model = "CAPM"

    # 过滤 ff_al 中不存在的列（ETF 可能缺失）
    factor_cols = [c for c in factor_cols if c in ff_al.columns]
    if not factor_cols:
        return {"error": "无可用因子列", "n": len(common)}

    ones = np.ones((len(common), 1))
    X = np.hstack([ones, ff_al[factor_cols].values])

    ols = _ols(y, X)
    beta = ols["beta"]
    t_stat = ols["t_stat"]
    p_value = ols["p_value"]

    # Alpha 年化
    alpha_daily = float(beta[0])
    alpha_annual = alpha_daily * TRADING_DAYS
    te_annual = float(np.std(ols["residuals"], ddof=1)) * math.sqrt(TRADING_DAYS)
    ir = alpha_annual / te_annual if te_annual > 1e-8 else 0.0

    factors_out = {}
    for i, fc in enumerate(factor_cols):
        factors_out[fc] = {
            "loading": round(float(beta[i + 1]), 4),
            "t_stat": round(float(t_stat[i + 1]), 3),
            "p_value": round(float(p_value[i + 1]), 4),
            "sig": _sig_stars(float(p_value[i + 1])),
        }

    # 残差自相关（一阶 Ljung-Box lite）
    res = ols["residuals"]
    autocorr_1 = 0.0
    if len(res) > 2:
        try:
            rm = res - res.mean()
            autocorr_1 = float(np.sum(rm[:-1] * rm[1:]) / np.sum(rm * rm))
        except Exception:
            pass

    return {
        "model": model,
        "n_obs": len(common),
        "date_range": [str(common[0].date()), str(common[-1].date())],
        "alpha_daily": round(alpha_daily, 6),
        "alpha_annual": round(alpha_annual, 4),
        "alpha_t": round(float(t_stat[0]), 3),
        "alpha_p": round(float(p_value[0]), 4),
        "alpha_sig": _sig_stars(float(p_value[0])),
        "tracking_error_annual": round(te_annual, 4),
        "information_ratio": round(ir, 3),
        "r2": round(ols["r2"], 4),
        "adj_r2": round(ols["adj_r2"], 4),
        "factors": factors_out,
        "residual_autocorr_1": round(autocorr_1, 4),
    }


def _sig_stars(p: float) -> str:
    if p < 0.01: return "***"
    if p < 0.05: return "**"
    if p < 0.10: return "*"
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# 4. 主函数
# ══════════════════════════════════════════════════════════════════════════════

def run_portfolio_attribution(
    source: str = "all_trades",
    initial_capital: float = 50_000.0,
    position_size_pct: float = 0.10,
    factor_source: str = "auto",
) -> Dict:
    """
    对整个策略做因子归因
    source: "all_trades" (所有 checked_t7) | "filtered" (portfolio_backtest 筛选后)
    factor_source: "auto" (自动选) | "ff6" (Kenneth French) | "etf" (ETF 近似，实时可用)
    """
    # 加载交易
    if source == "filtered":
        try:
            import portfolio_backtest as pb
            cfg = pb.BacktestConfig()
            bt_result = pb.run_backtest(cfg)
            if "error" in bt_result:
                return {"error": bt_result["error"]}
            # 转成统一格式
            trades = [
                {"date": t["entry"], "exit_date": t["exit"],
                 "holding_days": t["hold"], "net_return_t7": t["net_pct"],
                 "ticker": t["ticker"], "direction": t["dir"]}
                for t in bt_result.get("all_trades", [])
                if t.get("exit_reason") != "WINDOW_CUTOFF"
            ]
        except Exception as e:
            return {"error": f"portfolio_backtest 失败: {e}"}
    else:
        trades = _load_trades(source)

    if not trades:
        return {"error": "无可用交易", "source": source}

    # 构造策略日度收益
    strategy_rets = _build_daily_returns(trades, initial_capital, position_size_pct)
    if len(strategy_rets) < 15:
        return {
            "error": f"策略交易日过少 ({len(strategy_rets)}，至少 15)",
            "n_trading_days": len(strategy_rets),
            "n_trades": len(trades),
        }

    # 加载因子（auto: 先试 FF6，日期覆盖不够则降级 ETF 近似）
    start = strategy_rets.index[0] - pd.Timedelta(days=7)
    end = strategy_rets.index[-1] + pd.Timedelta(days=7)
    model_used = "FF6"
    factor_label = "Kenneth French FF6"
    ff = None

    if factor_source in ("auto", "ff6"):
        try:
            ff = _load_factors(start, end)
            # 检查重叠
            common = strategy_rets.index.intersection(ff.index)
            if len(common) < 15 and factor_source == "auto":
                _log.info(f"FF6 重叠日期不足 ({len(common)})，降级到 ETF 近似因子")
                ff = None
        except Exception as e:
            _log.info(f"FF6 下载失败，切 ETF: {e}")
            ff = None

    if ff is None and factor_source in ("auto", "etf"):
        try:
            ff = _load_etf_factors(start, end)
            model_used = "ETF5"
            factor_label = "ETF 近似因子 (SPY/IWM/IWD/IWF/MTUM/QUAL)"
        except Exception as e:
            return {"error": f"ETF 因子下载失败: {e}"}

    if ff is None:
        return {"error": "所有因子数据源都失败"}

    # 回归
    result = _regress(strategy_rets, ff, model=model_used)
    if "error" not in result:
        result["factor_source"] = factor_label

    if "error" not in result:
        result["source"] = source
        result["n_trades"] = len(trades)
        result["initial_capital"] = initial_capital
        result["position_size_pct"] = position_size_pct

        # 简单总结分类
        alpha_p = result.get("alpha_p", 1)
        alpha_annual = result.get("alpha_annual", 0)
        if alpha_p < 0.10 and alpha_annual > 0:
            result["verdict"] = "真 Alpha：剥离 FF6 因子后仍有正收益（统计显著）"
        elif alpha_annual > 0:
            result["verdict"] = "疑似 Alpha：正收益但不显著（样本小或 Alpha 弱）"
        elif alpha_p < 0.10 and alpha_annual < 0:
            result["verdict"] = "负 Alpha：因子暴露带来正收益，stock-picking 实际拖累"
        else:
            result["verdict"] = "无显著 Alpha：收益基本由因子暴露解释"

    return result


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _print_report(r: Dict) -> None:
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  Alpha Hive — 组合级 FF6 因子归因 (v0.22.0)         ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    if "error" in r:
        print(f"  ❌ {r['error']}")
        if "n_trading_days" in r:
            print(f"  策略交易日: {r['n_trading_days']}, 交易笔数: {r['n_trades']}")
        return

    print(f"  数据源：{r.get('source', '?')}   交易笔数：{r.get('n_trades', '?')}")
    print(f"  回归模型：{r['model']}   观测日：{r['n_obs']}   日期：{r['date_range'][0]} ~ {r['date_range'][1]}\n")

    # Alpha
    ap = r["alpha_annual"] * 100
    ap_sig = r["alpha_sig"]
    color = "🟢" if ap > 0 else "🔴"
    sig_tag = f"（{ap_sig} 显著）" if ap_sig else "（不显著）"
    print(f"  ┌─── Jensen α (年化) ────────────────────────────────┐")
    print(f"    α_annual : {color} {ap:+.2f}%  {sig_tag}")
    print(f"    t-stat   : {r['alpha_t']:+.3f}    p-value: {r['alpha_p']:.4f}")
    print(f"    IR       : {r['information_ratio']:+.3f}  (tracking error {r['tracking_error_annual']*100:.2f}%/yr)")
    print(f"    R²       : {r['r2']:.2%}  (Adj R² {r['adj_r2']:.2%})")
    print(f"  └─────────────────────────────────────────────────────┘\n")

    # 因子暴露
    labels = {
        "Mkt-RF": "市场溢价 β_mkt ",
        "SMB":    "规模    β_smb ",
        "HML":    "价值    β_hml ",
        "RMW":    "盈利性  β_rmw ",
        "CMA":    "投资    β_cma ",
        "MOM":    "动量    β_mom ",
    }
    print(f"  ┌─── 因子暴露 ──────────────────────────────────────┐")
    for fc, meta in r["factors"].items():
        ld = meta["loading"]; t = meta["t_stat"]; p = meta["p_value"]; sig = meta["sig"]
        lbl = labels.get(fc, fc)
        sig_str = f"[{sig}]" if sig else "   "
        print(f"    {lbl} {ld:>+7.3f}  t={t:>+6.2f}  p={p:.3f}  {sig_str}")
    print(f"  └─────────────────────────────────────────────────────┘\n")

    # 残差诊断
    ac = r.get("residual_autocorr_1", 0)
    ac_warn = ""
    if abs(ac) > 0.2:
        ac_warn = " ⚠️ 残差一阶自相关显著，可能存在未捕捉的因子"
    print(f"  残差一阶自相关: {ac:+.3f}{ac_warn}\n")

    verdict = r.get("verdict", "")
    print(f"  📊 结论：{verdict}\n")
    if r.get("model") == "FF3":
        print(f"  💡 注：观测 {r['n_obs']} 天不足 25，自动降级到 FF3 (Mkt/SMB/HML)")
    if r.get("model") == "ETF5":
        print(f"  💡 注：使用 ETF 近似因子（Kenneth French 数据滞后，本地实时可用）")

    # 样本量合理性警告
    n_obs = r.get("n_obs", 0)
    alpha_annual_abs = abs(r.get("alpha_annual", 0)) * 100
    if n_obs < 60 and alpha_annual_abs > 50:
        print(f"  ⚠️  样本警告：仅 {n_obs} 个观测日 + 年化 α 绝对值 {alpha_annual_abs:.0f}% 异常大")
        print(f"      原因 1：小样本 → α 估计方差大（需 100+ 日才稳）")
        print(f"      原因 2：日度分摊假设简化，真实 P&L 集中在 entry/exit 点")
        print(f"      建议：关注**方向（α 符号）+ 因子暴露（smb/mom 符号）+ IR 量级**，不要纠结具体 α 数字")
    print()


def main():
    parser = argparse.ArgumentParser(description="Alpha Hive 组合级因子归因")
    parser.add_argument("--source", choices=["all_trades", "filtered"], default="all_trades")
    parser.add_argument("--factor-source", choices=["auto", "ff6", "etf"], default="auto",
                        help="auto: 先试 FF6 再降级 ETF；ff6: Kenneth French（可能滞后）；etf: 实时 ETF 近似")
    parser.add_argument("--capital", type=float, default=50_000.0)
    parser.add_argument("--size-pct", type=float, default=0.10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_portfolio_attribution(
        source=args.source,
        initial_capital=args.capital,
        position_size_pct=args.size_pct,
        factor_source=args.factor_source,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_report(result)


if __name__ == "__main__":
    main()
