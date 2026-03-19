"""
risk_engine.py — Alpha Hive 风险量化引擎 v1.0
==============================================
三层 VaR + Cholesky 组合 VaR + 5大压力测试

Layer 1: 历史 VaR   — report_snapshots 实际收益分布（非参数，真实尾部）
Layer 2: 参数 VaR   — 正态假设，volatility_20d 驱动，解析公式快
Layer 3: MC VaR     — GBM 10,000 路径模拟，含完整收益分布
Bonus:   组合 VaR   — Cholesky 相关矩阵多标的联合模拟
Bonus:   压力测试   — VIX飙升/利率冲击/板块崩盘/COVID/流动性危机

零新依赖：numpy + scipy（已安装）+ yfinance（已安装）

集成接口：
    from risk_engine import run_full_risk_analysis
    result = run_full_risk_analysis("NVDA", stock_data, macro_data)

CLI：
    python risk_engine.py NVDA
    python risk_engine.py NVDA --portfolio NVDA TSLA MSFT --json
"""

import json
import math
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_log = logging.getLogger("alpha_hive.risk_engine")

# ─────────────────────────────────────────────────────────────
# 配置常量
# ─────────────────────────────────────────────────────────────
_HOME = Path(__file__).parent
_SNAPSHOTS_DIR = _HOME / "report_snapshots"
_CACHE_DIR = _HOME / ".risk_cache"
_CACHE_DIR.mkdir(exist_ok=True)

N_SIM = 10_000           # Monte Carlo 模拟次数
TRADING_DAYS = 252       # 年化交易日
RF_ANNUAL = 0.045        # 无风险利率（近似联邦基金利率）
BETA_CACHE_TTL = 86400   # beta 缓存 1 天


# ─────────────────────────────────────────────────────────────
# 数据加载辅助
# ─────────────────────────────────────────────────────────────

def _load_snapshot_returns(ticker: Optional[str] = None,
                           horizon: str = "t1") -> List[float]:
    """
    从 report_snapshots/*.json 加载历史实际收益率（%）
    horizon: "t1" | "t7" | "t30"
    """
    returns = []
    if not _SNAPSHOTS_DIR.exists():
        return returns
    for fp in sorted(_SNAPSHOTS_DIR.glob("*.json")):
        try:
            with open(fp) as fh:
                snap = json.load(fh)
            if ticker and snap.get("ticker") != ticker:
                continue
            entry = snap.get("entry_price", 0)
            price_tn = snap.get("actual_prices", {}).get(horizon)
            if entry and price_tn and float(entry) > 0 and float(price_tn) > 0:
                ret = (float(price_tn) / float(entry) - 1) * 100
                returns.append(ret)
        except Exception:
            pass
    return returns


def _fetch_daily_returns(ticker: str, period: str = "3mo") -> np.ndarray:
    """从 yfinance 获取日收益率序列（小数形式）"""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=period)
        if hist.empty or len(hist) < 10:
            return np.array([])
        returns = hist["Close"].pct_change().dropna().values.astype(float)
        return returns
    except Exception as e:
        _log.debug("_fetch_daily_returns %s: %s", ticker, e)
        return np.array([])


def _fetch_multi_returns(tickers: List[str],
                         period: str = "3mo") -> Dict[str, np.ndarray]:
    """批量获取多标的日收益率（尽量对齐到公共日期）"""
    try:
        import yfinance as yf
        import pandas as pd
        raw = yf.download(tickers, period=period,
                          auto_adjust=True, progress=False)
        if raw.empty:
            raise ValueError("empty")
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
        rets = close.pct_change().dropna()
        result: Dict[str, np.ndarray] = {}
        for t in tickers:
            if t in rets.columns:
                result[t] = rets[t].dropna().values.astype(float)
        return result
    except Exception as e:
        _log.debug("_fetch_multi_returns fallback: %s", e)
        result = {}
        for t in tickers:
            r = _fetch_daily_returns(t, period)
            if len(r) > 0:
                result[t] = r
        return result


# ─────────────────────────────────────────────────────────────
# 统计辅助
# ─────────────────────────────────────────────────────────────

def _skewness(arr: np.ndarray) -> float:
    """Pearson 偏度"""
    s = arr.std()
    return 0.0 if s == 0 else float(((arr - arr.mean()) ** 3).mean() / s ** 3)


def _excess_kurtosis(arr: np.ndarray) -> float:
    """超额峰度（正态 = 0，肥尾 > 0）"""
    s = arr.std()
    return 0.0 if s == 0 else float(((arr - arr.mean()) ** 4).mean() / s ** 4) - 3.0


# ─────────────────────────────────────────────────────────────
# Layer 1: 历史 VaR
# ─────────────────────────────────────────────────────────────

def historical_var(
        ticker: Optional[str] = None,
        horizon: str = "t1",
        confidence_levels: Tuple = (0.90, 0.95, 0.99)) -> Dict:
    """
    历史模拟法 VaR

    数据源优先级：
      1. report_snapshots 实际 T+N 收益（最真实，反映蜂群预测的真实分布）
      2. yfinance 日收益率 × sqrt(T)（samples 不足时降级）

    优点：非参数，自动捕捉肥尾/偏态
    缺点：样本量有限（取决于 snapshot 数量）
    """
    days_map = {"t1": 1, "t7": 7, "t30": 30}
    horizon_days = days_map.get(horizon, 1)

    # ── 尝试从 snapshots 加载 ──
    returns = _load_snapshot_returns(ticker, horizon)

    # ── 样本不足：降级到 yfinance 真实 T日滚动收益（非 sqrt(T) 缩放） ──
    if len(returns) < 5:
        if ticker:
            # 直接计算真实 T日持有期收益（避免 sqrt(T) 低估厚尾）
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period="1y")
                if not hist.empty and len(hist) > horizon_days + 5:
                    closes = hist["Close"].values.astype(float)
                    # 滚动 T日收益：r_t = (P_{t+T} / P_t) - 1
                    roll_returns = [
                        (closes[i + horizon_days] / closes[i] - 1) * 100
                        for i in range(len(closes) - horizon_days)
                    ]
                    returns = roll_returns
                    _log.debug("historical_var: 用 yfinance %dd 滚动收益 (n=%d)",
                               horizon_days, len(returns))
            except Exception as _e:
                _log.debug("historical_var: yfinance 滚动收益失败: %s", _e)
        if len(returns) < 5:
            # 最终降级：sqrt(T) 缩放（最保守，会低估厚尾，标注警告）
            daily = _fetch_daily_returns(ticker or "SPY", "6mo")
            if len(daily) > 10:
                returns = list(daily * 100 * math.sqrt(horizon_days))
            _log.debug("historical_var: 最终降级用 sqrt(T) 缩放 (n=%d)", len(returns))

    if not returns:
        return {"method": "historical", "horizon": horizon, "error": "数据不足"}

    arr = np.array(returns, dtype=float)

    result: Dict = {
        "method": "historical",
        "horizon": horizon,
        "sample_size": len(arr),
        "mean_return": round(float(arr.mean()), 2),
        "std_return": round(float(arr.std()), 2),
        "skewness": round(_skewness(arr), 3),
        "excess_kurtosis": round(_excess_kurtosis(arr), 3),
        "max_loss": round(float(arr.min()), 2),
        "best_case": round(float(arr.max()), 2),
        "win_rate_pct": round(float((arr > 0).mean() * 100), 1),
        "var": {},
        "cvar": {},
    }

    for cl in confidence_levels:
        pct = (1 - cl) * 100
        var_val = float(np.percentile(arr, pct))
        tail = arr[arr <= var_val]
        cvar_val = float(tail.mean()) if len(tail) > 0 else var_val
        key = str(int(cl * 100))
        result["var"][key] = round(var_val, 2)
        result["cvar"][key] = round(cvar_val, 2)

    return result


# ─────────────────────────────────────────────────────────────
# Layer 2: 参数 VaR（正态假设）
# ─────────────────────────────────────────────────────────────

def parametric_var(
        stock_data: Dict,
        horizon_days: int = 7,
        confidence_levels: Tuple = (0.90, 0.95, 0.99)) -> Dict:
    """
    参数法 VaR（Delta-Normal）

    公式：
      VaR_T  = -(μ_T + z_α × σ_T)     [z_α < 0 for left tail]
      CVaR_T = -(μ_T - σ_T × φ(z_α) / (1 - α))

    其中：
      μ_T = μ_daily × T
      σ_T = σ_daily × √T
      μ_daily ≈ momentum_5d / 5 / 100
      σ_daily = volatility_20d / 100 / √252
    """
    from scipy.stats import norm

    sigma_annual = float(stock_data.get("volatility_20d", 30.0)) / 100.0
    mom_5d_pct   = float(stock_data.get("momentum_5d", 0.0))

    sigma_daily = sigma_annual / math.sqrt(TRADING_DAYS)
    mu_daily    = mom_5d_pct / 5.0 / 100.0   # 5日动量 → 日均收益

    # 缩放到目标 horizon
    sigma_T = sigma_daily * math.sqrt(horizon_days)
    mu_T    = mu_daily * horizon_days

    result: Dict = {
        "method": "parametric",
        "horizon_days": horizon_days,
        "sigma_annual_pct": round(sigma_annual * 100, 2),
        "sigma_daily_pct": round(sigma_daily * 100, 4),
        "mu_daily_pct": round(mu_daily * 100, 4),
        "var": {},
        "cvar": {},
    }

    for cl in confidence_levels:
        z_alpha = norm.ppf(1.0 - cl)               # 负值（左尾）
        var_pct  = (mu_T + z_alpha * sigma_T) * 100
        # CVaR 解析公式（标准正态）
        cvar_pct = (mu_T - sigma_T * norm.pdf(-z_alpha) / (1.0 - cl)) * 100
        key = str(int(cl * 100))
        result["var"][key]  = round(float(var_pct), 2)
        result["cvar"][key] = round(float(cvar_pct), 2)

    return result


# ─────────────────────────────────────────────────────────────
# Layer 3: Monte Carlo VaR（GBM 单资产）
# ─────────────────────────────────────────────────────────────

def monte_carlo_var(
        ticker: str,
        stock_data: Dict,
        n_sim: int = N_SIM,
        horizon_days: int = 30,
        confidence_levels: Tuple = (0.90, 0.95, 0.99),
        seed: Optional[int] = None) -> Dict:
    """
    Monte Carlo VaR — 几何布朗运动（解析解版，比逐步模拟快10x）

    模型：
      S_T = S₀ × exp((μ - ½σ²)T + σ√T × Z)
      Z ~ N(0,1)，n_sim 次独立模拟

    参数：
      μ_annual  ≈ momentum_5d / 5 × 252  （粗略年化漂移）
      σ_annual  = volatility_20d / 100    （历史波动率）

    输出：
      VaR/CVaR 在多置信水平 + 完整收益分布分位数 + 三档价格目标
    """
    if seed is not None:
        np.random.seed(seed)

    S0 = float(stock_data.get("price", 100.0))
    if S0 <= 0:
        return {"method": "monte_carlo_gbm", "error": "price 无效 (≤0)"}

    sigma_annual = float(stock_data.get("volatility_20d", 30.0)) / 100.0
    mom_5d_pct   = float(stock_data.get("momentum_5d", 0.0))
    mu_annual    = mom_5d_pct / 5.0 / 100.0 * TRADING_DAYS   # 年化漂移

    T = horizon_days / TRADING_DAYS  # 年

    # ── 解析解（对数正态）──
    Z = np.random.standard_normal(n_sim)
    log_drift     = (mu_annual - 0.5 * sigma_annual ** 2) * T
    log_diffusion = sigma_annual * math.sqrt(T) * Z
    S_T = S0 * np.exp(log_drift + log_diffusion)
    pct_returns = (S_T / S0 - 1.0) * 100.0

    result: Dict = {
        "method": "monte_carlo_gbm",
        "ticker": ticker,
        "horizon_days": horizon_days,
        "n_sim": n_sim,
        "entry_price": round(S0, 2),
        "sigma_annual_pct": round(sigma_annual * 100, 2),
        "mu_annual_pct": round(mu_annual * 100, 2),
        "expected_return": round(float(pct_returns.mean()), 2),
        "std_return": round(float(pct_returns.std()), 2),
        "max_loss": round(float(pct_returns.min()), 2),
        "best_case": round(float(pct_returns.max()), 2),
        "var": {},
        "cvar": {},
        # 收益分布分位数（供图表渲染）
        "percentiles": {
            "p1":  round(float(np.percentile(pct_returns, 1)),  2),
            "p5":  round(float(np.percentile(pct_returns, 5)),  2),
            "p10": round(float(np.percentile(pct_returns, 10)), 2),
            "p25": round(float(np.percentile(pct_returns, 25)), 2),
            "p50": round(float(np.percentile(pct_returns, 50)), 2),
            "p75": round(float(np.percentile(pct_returns, 75)), 2),
            "p90": round(float(np.percentile(pct_returns, 90)), 2),
            "p95": round(float(np.percentile(pct_returns, 95)), 2),
            "p99": round(float(np.percentile(pct_returns, 99)), 2),
        },
        # 三档价格目标
        "price_targets": {
            "bear_case": round(float(S0 * (1 + np.percentile(pct_returns, 10) / 100)), 2),
            "base_case": round(float(S0 * (1 + np.percentile(pct_returns, 50) / 100)), 2),
            "bull_case": round(float(S0 * (1 + np.percentile(pct_returns, 90) / 100)), 2),
        },
    }

    for cl in confidence_levels:
        pct_cut = (1.0 - cl) * 100
        var_val  = float(np.percentile(pct_returns, pct_cut))
        tail     = pct_returns[pct_returns <= var_val]
        cvar_val = float(tail.mean()) if len(tail) > 0 else var_val
        key = str(int(cl * 100))
        result["var"][key]  = round(var_val, 2)
        result["cvar"][key] = round(cvar_val, 2)

    return result


# ─────────────────────────────────────────────────────────────
# Bonus: 多标的组合 VaR（Cholesky 相关矩阵）
# ─────────────────────────────────────────────────────────────

def portfolio_var(
        tickers: List[str],
        weights: Optional[List[float]] = None,
        horizon_days: int = 30,
        n_sim: int = N_SIM) -> Dict:
    """
    多标的组合 VaR — Cholesky 相关矩阵模拟

    步骤：
      1. 拉取各标的 60 日日收益率矩阵
      2. 计算相关矩阵，Cholesky 分解 (L: C = LL')
      3. 生成相关标准正态：Z_corr = Z @ L'
      4. 缩放到 horizon 波动率：return_i = Z_corr_i × σ_i × √T
      5. 按权重合成组合收益，计算 VaR/CVaR
      6. 计算多元化收益 = undiversified_VaR - portfolio_VaR
    """
    n = len(tickers)
    if n < 2:
        return {"error": "至少需要 2 个标的做组合分析"}

    # 等权 fallback
    if weights is None:
        weights = [1.0 / n] * n
    w = np.array(weights, dtype=float)
    w /= w.sum()  # 归一化（允许非等权输入）

    # 获取历史日收益
    daily_returns = _fetch_multi_returns(tickers, "3mo")
    available = [t for t in tickers if t in daily_returns and len(daily_returns[t]) > 20]

    if len(available) < 2:
        return {"error": "有效标的不足 2 个", "available": available}

    # 对齐权重到可用标的
    avail_idx = [tickers.index(t) for t in available if t in tickers]
    w_avail = w[avail_idx]
    w_avail /= w_avail.sum()

    # 构建收益矩阵（对齐到最短序列）
    min_len = min(len(daily_returns[t]) for t in available)
    R = np.array([daily_returns[t][-min_len:] for t in available], dtype=float)  # (n_assets, T)

    # 相关矩阵 + Cholesky
    corr = np.corrcoef(R)
    vol  = R.std(axis=1)  # 日波动率

    # 数值稳定性：确保正定（添加微小对角扰动）
    min_eig = float(np.linalg.eigvalsh(corr).min())
    if min_eig < 1e-8:
        corr += np.eye(len(available)) * (1e-8 - min_eig + 1e-6)

    try:
        L = np.linalg.cholesky(corr)
    except np.linalg.LinAlgError:
        _log.warning("portfolio_var: Cholesky 失败，使用单位矩阵（独立假设）")
        L = np.eye(len(available))

    # 模拟相关标准正态 → 缩放到 horizon 波动率
    Z = np.random.standard_normal((n_sim, len(available)))
    Z_corr = Z @ L.T                              # 相关化
    sigma_T = vol * math.sqrt(horizon_days)        # horizon 波动率
    asset_rets_sim = Z_corr * sigma_T             # (n_sim, n_assets)

    # 组合收益（百分比）
    port_rets = (asset_rets_sim @ w_avail) * 100.0

    # 无多元化 VaR（各资产独立 VaR 加权和）—— 对比用
    undiv_var_99 = float(sum(
        w_avail[i] * abs(np.percentile(asset_rets_sim[:, i] * 100, 1))
        for i in range(len(available))
    ))

    port_var_95  = float(np.percentile(port_rets, 5))
    port_var_99  = float(np.percentile(port_rets, 1))
    tail_99      = port_rets[port_rets <= port_var_99]
    port_cvar_99 = float(tail_99.mean()) if len(tail_99) > 0 else port_var_99
    tail_95      = port_rets[port_rets <= port_var_95]
    port_cvar_95 = float(tail_95.mean()) if len(tail_95) > 0 else port_var_95

    # 相关性对摘要
    corr_pairs = [
        (available[i], available[j], round(float(corr[i, j]), 3))
        for i in range(len(available))
        for j in range(i + 1, len(available))
    ]
    corr_pairs.sort(key=lambda x: abs(x[2]), reverse=True)

    div_benefit = round(undiv_var_99 - abs(port_var_99), 2)

    return {
        "method": "portfolio_cholesky",
        "tickers": available,
        "weights": [round(float(x), 4) for x in w_avail],
        "horizon_days": horizon_days,
        "n_sim": n_sim,
        "portfolio_var_95": round(port_var_95, 2),
        "portfolio_var_99": round(port_var_99, 2),
        "portfolio_cvar_95": round(port_cvar_95, 2),
        "portfolio_cvar_99": round(port_cvar_99, 2),
        "undiversified_var_99": round(-undiv_var_99, 2),
        "diversification_benefit_pct": div_benefit,
        "expected_return": round(float(port_rets.mean()), 2),
        "portfolio_std_pct": round(float(port_rets.std()), 2),
        "max_corr_pair": corr_pairs[0] if corr_pairs else None,
        "min_corr_pair": corr_pairs[-1] if corr_pairs else None,
        "all_correlations": corr_pairs[:15],
    }


# ─────────────────────────────────────────────────────────────
# 压力测试引擎
# ─────────────────────────────────────────────────────────────

_STRESS_SCENARIOS: Dict = {
    "vix_spike": {
        "name": "VIX 飙升 (15→40)",
        "description": "VIX 从正常水平飙升至 40+（类 2018 年 2 月、2020 年 3 月初）",
        "spx_shock_pct": -18.0,      # 对应 SPX 跌幅
        "high_vol_extra_pct": -3.0,  # σ > 45% 时额外惩罚
        "high_vol_threshold": 45.0,
    },
    "rate_shock": {
        "name": "利率冲击 (+100bps)",
        "description": "10 年期国债收益率单月上行 100bps，压制高估值成长股",
        "rate_shock_bps": 100,
        "growth_sensitivity": -18.0,   # 成长股：每 100bps 约 -15~20%
        "value_sensitivity":  -4.0,    # 价值/金融股：影响较小
        "blend_sensitivity":  -10.0,
    },
    "sector_crash": {
        "name": "板块崩盘 (-25%)",
        "description": "所在板块集体下跌 25%（如半导体超级周期反转）",
        "sector_shock_pct": -25.0,
    },
    "covid_2020": {
        "name": "COVID 型崩盘 (2020-02~03)",
        "description": "类 2020 年 2-3 月，SPX 33 天内跌 34%，VIX 峰值 85",
        "spx_shock_pct": -34.0,
        "duration_days": 33,
        "high_beta_extra_per_unit": -4.0,  # beta > 1.5 每单位额外惩罚
        "high_beta_threshold": 1.5,
    },
    "liquidity_crisis": {
        "name": "流动性危机 (2008 型)",
        "description": "信贷冻结 + 强制去杠杆，低流动性标的额外折价",
        "spx_shock_pct": -25.0,
        "illiquidity_pct":   -8.0,   # volume_ratio < 0.5 时额外
        "low_volume_threshold": 0.5,
        "high_vol_extra_pct": -5.0,  # σ > 40% 时额外
        "high_vol_threshold": 40.0,
    },
}


def _estimate_beta(ticker: str, benchmark: str = "SPY",
                   cache_ttl: int = BETA_CACHE_TTL) -> float:
    """
    估算标的对 benchmark 的 60 日 OLS beta，结果缓存 24 小时
    失败时安全返回 1.0（市场中性假设）
    """
    cache_file = _CACHE_DIR / f"beta_{ticker}_{benchmark}.json"
    if cache_file.exists():
        try:
            age = time.time() - cache_file.stat().st_mtime
            if age < cache_ttl:
                return float(json.loads(cache_file.read_text()).get("beta", 1.0))
        except Exception:
            pass

    try:
        import yfinance as yf
        import pandas as pd
        raw = yf.download([ticker, benchmark], period="3mo",
                          auto_adjust=True, progress=False)
        close = raw["Close"]
        if isinstance(close.columns, pd.MultiIndex):
            # yfinance 多标的返回 MultiIndex
            close = close.droplevel(0, axis=1) if hasattr(close.columns, 'droplevel') else close
        rets = close.pct_change().dropna()
        if ticker not in rets.columns or benchmark not in rets.columns:
            return 1.0
        cov_mat = rets[[ticker, benchmark]].cov()
        beta = float(cov_mat.loc[ticker, benchmark] / cov_mat.loc[benchmark, benchmark])
        beta = max(0.0, min(5.0, beta))  # clamp 防止极值
        cache_file.write_text(json.dumps({"beta": round(beta, 4), "ts": time.time()}))
        return round(beta, 3)
    except Exception as e:
        _log.debug("_estimate_beta %s/%s: %s", ticker, benchmark, e)
        return 1.0


def _classify_growth_value(stock_data: Dict) -> str:
    """
    简单成长/价值/混合分类（用波动率和动量代理）
    - growth: 高波动 or 强动量
    - value:  低波动 + 负动量
    - blend:  介于中间
    """
    vol = float(stock_data.get("volatility_20d", 30.0))
    mom = float(stock_data.get("momentum_5d", 0.0))
    if vol > 35 or mom > 5:
        return "growth"
    elif vol < 20 and mom < 0:
        return "value"
    return "blend"


def _severity_label(shock_pct: float) -> str:
    if shock_pct <= -30:
        return "extreme"
    elif shock_pct <= -20:
        return "severe"
    elif shock_pct <= -10:
        return "moderate"
    elif shock_pct <= -5:
        return "mild"
    return "negligible"


def run_stress_tests(ticker: str,
                     stock_data: Dict,
                     macro_data: Optional[Dict] = None,
                     scenarios: Optional[List[str]] = None) -> Dict:
    """
    5 大宏观情景压力测试

    方法论：
      - VIX飙升 / COVID / 流动性：beta × market_shock + 个股特质调整
      - 利率冲击：成长/价值分类 × 利率敏感系数
      - 板块崩盘：sector_beta × sector_shock
    所有冲击限制在 [-90%, 0%]，不模拟正向情景（压力测试侧重下行）
    """
    if scenarios is None:
        scenarios = list(_STRESS_SCENARIOS.keys())

    S0  = float(stock_data.get("price", 100.0))
    vol = float(stock_data.get("volatility_20d", 30.0))
    vol_ratio = float(stock_data.get("volume_ratio", 1.0))

    # 惰性加载 beta，避免重复 API 调用
    _beta_cache: Dict[str, float] = {}

    def beta(benchmark: str = "SPY") -> float:
        if benchmark not in _beta_cache:
            _beta_cache[benchmark] = _estimate_beta(ticker, benchmark)
        return _beta_cache[benchmark]

    results: Dict[str, Dict] = {}

    for scenario_key in scenarios:
        cfg = _STRESS_SCENARIOS.get(scenario_key)
        if cfg is None:
            continue
        try:
            shock_pct = 0.0
            notes: List[str] = []

            # ── VIX 飙升 ──
            if scenario_key == "vix_spike":
                b = beta("SPY")
                shock_pct = cfg["spx_shock_pct"] * b
                notes.append(f"SPX {cfg['spx_shock_pct']:.0f}% × β{b:.2f}")
                if vol > cfg["high_vol_threshold"]:
                    shock_pct += cfg["high_vol_extra_pct"]
                    notes.append(f"高σ额外{cfg['high_vol_extra_pct']:.0f}%")

            # ── 利率冲击 ──
            elif scenario_key == "rate_shock":
                style = _classify_growth_value(stock_data)
                sens_map = {
                    "growth": cfg["growth_sensitivity"],
                    "value":  cfg["value_sensitivity"],
                    "blend":  cfg["blend_sensitivity"],
                }
                sensitivity = sens_map[style]
                # 每 100bps 对应敏感系数（已内嵌在参数里）
                shock_pct = sensitivity * (cfg["rate_shock_bps"] / 100.0)
                notes.append(f"风格:{style}, 利率敏感度{sensitivity:.0f}%/100bps")

            # ── 板块崩盘 ──
            elif scenario_key == "sector_crash":
                try:
                    from fred_macro import get_sector_etf_for_ticker
                    etf = get_sector_etf_for_ticker(ticker) or "SPY"
                except ImportError:
                    etf = "SPY"
                b = beta(etf)
                shock_pct = cfg["sector_shock_pct"] * b
                notes.append(f"板块ETF:{etf}, β{b:.2f}")

            # ── COVID 型崩盘 ──
            elif scenario_key == "covid_2020":
                b = beta("SPY")
                shock_pct = cfg["spx_shock_pct"] * b
                notes.append(f"SPX {cfg['spx_shock_pct']:.0f}% × β{b:.2f} ({cfg['duration_days']}天)")
                if b > cfg["high_beta_threshold"]:
                    extra = (b - cfg["high_beta_threshold"]) * cfg["high_beta_extra_per_unit"]
                    shock_pct += extra
                    notes.append(f"高β超跌补偿{extra:.1f}%")

            # ── 流动性危机 ──
            elif scenario_key == "liquidity_crisis":
                b = beta("SPY")
                shock_pct = cfg["spx_shock_pct"] * b
                notes.append(f"SPX {cfg['spx_shock_pct']:.0f}% × β{b:.2f}")
                if vol_ratio < cfg["low_volume_threshold"]:
                    shock_pct += cfg["illiquidity_pct"]
                    notes.append(f"流动性折价{cfg['illiquidity_pct']:.0f}%")
                if vol > cfg["high_vol_threshold"]:
                    shock_pct += cfg["high_vol_extra_pct"]
                    notes.append(f"高σ额外{cfg['high_vol_extra_pct']:.0f}%")

            # clamp: 最多跌 90%，不模拟正向（压力测试下行专用）
            shock_pct       = max(-90.0, min(0.0, shock_pct))
            stressed_price  = S0 * (1.0 + shock_pct / 100.0)

            results[scenario_key] = {
                "name": cfg["name"],
                "description": cfg["description"],
                "shock_pct": round(float(shock_pct), 2),
                "stressed_price": round(float(stressed_price), 2),
                "entry_price": round(S0, 2),
                "dollar_loss_per_share": round(float(stressed_price - S0), 2),
                "severity": _severity_label(shock_pct),
                "notes": notes,
            }

        except Exception as e:
            _log.debug("Stress scenario %s failed for %s: %s", scenario_key, ticker, e)
            results[scenario_key] = {
                "name": cfg["name"],
                "shock_pct": None,
                "error": str(e),
            }

    # 汇总最坏情景
    valid = {k: v for k, v in results.items() if v.get("shock_pct") is not None}
    worst_key = min(valid, key=lambda k: valid[k]["shock_pct"]) if valid else None
    worst = valid[worst_key] if worst_key else {}

    return {
        "ticker": ticker,
        "entry_price": round(S0, 2),
        "scenarios": results,
        "worst_case": {
            "scenario_key": worst_key,
            "shock_pct": worst.get("shock_pct"),
            "stressed_price": worst.get("stressed_price"),
            "name": worst.get("name"),
        },
        "average_shock_pct": round(
            float(np.mean([v["shock_pct"] for v in valid.values()])), 2
        ) if valid else None,
    }


# ─────────────────────────────────────────────────────────────
# 主入口：完整风险分析
# ─────────────────────────────────────────────────────────────

def run_full_risk_analysis(
        ticker: str,
        stock_data: Optional[Dict] = None,
        macro_data: Optional[Dict] = None,
        include_portfolio: bool = False,
        portfolio_tickers: Optional[List[str]] = None,
        n_sim: int = N_SIM) -> Dict:
    """
    完整风险分析入口（供 generate_deep_v2.py 调用）

    如未传入 stock_data，自动从 yfinance 获取当前价格和波动率。

    Returns:
        包含三层 VaR + 压力测试 + 汇总的完整 dict
    """
    t0 = time.time()

    # ── 自动获取 stock_data ──
    if not stock_data or float(stock_data.get("price", 0)) <= 0:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="1mo")
            if hist.empty:
                raise ValueError("empty history")
            price = float(hist["Close"].iloc[-1])
            rets  = hist["Close"].pct_change().dropna()
            vol   = float(rets.std() * math.sqrt(TRADING_DAYS) * 100)
            mom5  = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100) \
                    if len(hist) >= 5 else 0.0
            vol_r = float(hist["Volume"].iloc[-1] / hist["Volume"].mean()) \
                    if hist["Volume"].mean() > 0 else 1.0
            stock_data = {
                "price": price,
                "volatility_20d": vol,
                "momentum_5d": mom5,
                "volume_ratio": vol_r,
            }
            _log.info("risk_engine: %s 自动获取 price=%.2f σ=%.1f%%", ticker, price, vol)
        except Exception as e:
            _log.warning("risk_engine: %s stock_data 获取失败，使用默认值: %s", ticker, e)
            stock_data = {
                "price": 100.0, "volatility_20d": 30.0,
                "momentum_5d": 0.0,  "volume_ratio": 1.0,
            }

    result: Dict = {
        "ticker": ticker,
        "analysis_date": __import__("datetime").date.today().isoformat(),
        "entry_price": round(float(stock_data.get("price", 100)), 2),
        "sigma_annual_pct": round(float(stock_data.get("volatility_20d", 30)), 2),
    }

    # ── Layer 1: 历史 VaR ──
    try:
        result["historical_var_t1"]  = historical_var(ticker, "t1")
        result["historical_var_t7"]  = historical_var(ticker, "t7")
        result["historical_var_t30"] = historical_var(ticker, "t30")
    except Exception as e:
        _log.debug("historical_var error: %s", e)
        result["historical_var_t1"] = {"error": str(e)}

    # ── Layer 2: 参数 VaR ──
    try:
        result["parametric_var_7d"]  = parametric_var(stock_data, horizon_days=7)
        result["parametric_var_30d"] = parametric_var(stock_data, horizon_days=30)
    except Exception as e:
        _log.debug("parametric_var error: %s", e)
        result["parametric_var_7d"] = {"error": str(e)}

    # ── Layer 3: Monte Carlo VaR ──
    try:
        result["mc_var_7d"]  = monte_carlo_var(ticker, stock_data,
                                                n_sim=n_sim, horizon_days=7)
        result["mc_var_30d"] = monte_carlo_var(ticker, stock_data,
                                                n_sim=n_sim, horizon_days=30)
    except Exception as e:
        _log.debug("monte_carlo_var error: %s", e)
        result["mc_var_7d"] = {"error": str(e)}

    # ── 压力测试 ──
    try:
        result["stress_tests"] = run_stress_tests(ticker, stock_data, macro_data)
    except Exception as e:
        _log.debug("stress_tests error: %s", e)
        result["stress_tests"] = {"error": str(e)}

    # ── 组合 VaR（可选）──
    if include_portfolio and portfolio_tickers and len(portfolio_tickers) >= 2:
        try:
            result["portfolio_var"] = portfolio_var(
                portfolio_tickers, horizon_days=30, n_sim=n_sim)
        except Exception as e:
            _log.debug("portfolio_var error: %s", e)
            result["portfolio_var"] = {"error": str(e)}

    # ── 汇总摘要 ──
    result["summary"] = _build_summary(result, stock_data)
    result["elapsed_seconds"] = round(time.time() - t0, 2)
    return result


def _build_summary(result: Dict, stock_data: Dict) -> Dict:
    """从三层结果提取核心指标，生成汇总卡片"""
    summary: Dict = {}

    # VaR@99% 三法对比（7日）
    var_99_sources: Dict[str, float] = {}

    hv = result.get("historical_var_t1", {})   # t1 是最有数据的 horizon
    if hv.get("var", {}).get("99") is not None:
        var_99_sources["historical"] = hv["var"]["99"]

    pv = result.get("parametric_var_7d", {})
    if pv.get("var", {}).get("99") is not None:
        var_99_sources["parametric"] = pv["var"]["99"]

    mc = result.get("mc_var_7d", {})
    if mc.get("var", {}).get("99") is not None:
        var_99_sources["monte_carlo"] = mc["var"]["99"]

    if var_99_sources:
        summary["var_99_7d_comparison"] = var_99_sources
        summary["var_99_7d_consensus"]  = round(
            float(np.mean(list(var_99_sources.values()))), 2)

    # CVaR@99% 30日（Expected Shortfall，最重要的尾部风险指标）
    mc30 = result.get("mc_var_30d", {})
    if mc30.get("cvar", {}).get("99") is not None:
        summary["cvar_99_30d"] = mc30["cvar"]["99"]

    # Monte Carlo 价格目标（7日）
    if mc.get("price_targets"):
        summary["price_targets_7d"] = mc["price_targets"]

    # 最坏压力情景
    st = result.get("stress_tests", {})
    if st.get("worst_case", {}).get("shock_pct") is not None:
        summary["worst_stress_scenario"] = st["worst_case"]
        summary["avg_stress_shock_pct"]  = st.get("average_shock_pct")

    # 综合风险等级
    sigma      = float(stock_data.get("volatility_20d", 30.0))
    worst_shock = st.get("worst_case", {}).get("shock_pct", -10.0) or -10.0
    cvar_val    = summary.get("cvar_99_30d", -15.0) or -15.0

    if sigma > 60 or worst_shock < -35 or cvar_val < -30:
        risk_level = "extreme"
    elif sigma > 45 or worst_shock < -25 or cvar_val < -20:
        risk_level = "high"
    elif sigma > 30 or worst_shock < -15 or cvar_val < -12:
        risk_level = "moderate"
    elif sigma > 20 or worst_shock < -10:
        risk_level = "low"
    else:
        risk_level = "minimal"

    summary["risk_level"]        = risk_level
    summary["sigma_annual_pct"]  = sigma

    return summary


# ─────────────────────────────────────────────────────────────
# HTML 格式化（供 generate_deep_v2.py 集成）
# ─────────────────────────────────────────────────────────────

def format_risk_html(result: Dict) -> str:
    """
    生成 risk_engine 结果的 HTML 卡片
    风格匹配 Alpha Hive 深度报告黑色主题
    """
    ticker  = result.get("ticker", "?")
    summary = result.get("summary", {})
    mc7     = result.get("mc_var_7d", {})
    mc30    = result.get("mc_var_30d", {})
    st      = result.get("stress_tests", {})

    risk_level  = summary.get("risk_level", "moderate")
    _risk_color = {"minimal": "#22c55e", "low": "#86efac",
                   "moderate": "#fbbf24", "high": "#f97316", "extreme": "#ef4444"}
    _risk_cn    = {"minimal": "极低", "low": "低",
                   "moderate": "中等", "high": "高", "extreme": "极高"}
    rc = _risk_color.get(risk_level, "#fbbf24")
    rl = _risk_cn.get(risk_level, "中等")

    # ── VaR 三法对比表 ──
    var_comp = summary.get("var_99_7d_comparison", {})
    var_rows_html = ""
    for method, val in var_comp.items():
        label = {"historical": "历史模拟", "parametric": "参数法(正态)",
                 "monte_carlo": "蒙特卡洛GBM"}.get(method, method)
        color = "#ef4444" if val < -10 else ("#fbbf24" if val < -5 else "#86efac")
        bar_w = min(100, abs(val) * 3)
        var_rows_html += f"""
        <tr>
          <td style="padding:5px 10px;font-size:12px;color:#94a3b8">{label}</td>
          <td style="padding:5px 10px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="color:{color};font-weight:700;font-size:13px;min-width:52px">{val:+.2f}%</span>
              <div style="flex:1;background:#0f172a;border-radius:3px;height:6px">
                <div style="width:{bar_w}%;background:{color};height:6px;border-radius:3px"></div>
              </div>
            </div>
          </td>
        </tr>"""

    consensus_var = summary.get("var_99_7d_consensus", 0) or 0
    cvar_30       = summary.get("cvar_99_30d", 0) or 0

    # ── 价格目标（7日）──
    pt    = summary.get("price_targets_7d", {})
    entry = result.get("entry_price", 0)
    pt_html = ""
    if pt and entry:
        for label, price_key, color in [
            ("🐻 熊市 P10", "bear_case", "#ef4444"),
            ("⚖️ 基准 P50", "base_case", "#94a3b8"),
            ("🐂 牛市 P90", "bull_case", "#22c55e"),
        ]:
            price = pt.get(price_key, 0)
            chg   = (price / entry - 1) * 100 if entry > 0 and price > 0 else 0
            pt_html += f"""
            <div style="text-align:center;padding:10px;background:#1e293b;border-radius:8px;border:1px solid #334155">
              <div style="font-size:10px;color:#64748b;margin-bottom:4px">{label}</div>
              <div style="font-size:20px;font-weight:700;color:#f1f5f9">${price:.2f}</div>
              <div style="font-size:12px;color:{color};font-weight:600">{chg:+.1f}%</div>
            </div>"""

    # ── 压力测试表格 ──
    sev_color_map = {
        "extreme": "#ef4444", "severe": "#f97316",
        "moderate": "#fbbf24", "mild": "#86efac", "negligible": "#64748b"}

    stress_rows_html = ""
    for _k, sc in st.get("scenarios", {}).items():
        if sc.get("shock_pct") is None:
            continue
        shock = sc["shock_pct"]
        sev   = sc.get("severity", "moderate")
        svc   = sev_color_map.get(sev, "#94a3b8")
        bar_w = min(100, abs(shock) * 1.8)
        stress_rows_html += f"""
        <tr style="border-bottom:1px solid #1e293b">
          <td style="padding:7px 10px;font-size:12px;color:#cbd5e1">{sc.get('name','')}</td>
          <td style="padding:7px 10px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="color:{svc};font-weight:700;font-size:13px;min-width:52px">{shock:+.1f}%</span>
              <div style="flex:1;background:#0f172a;border-radius:3px;height:5px">
                <div style="width:{bar_w}%;background:{svc};height:5px;border-radius:3px"></div>
              </div>
              <span style="color:#475569;font-size:11px;min-width:55px">${sc.get('stressed_price',0):.2f}</span>
            </div>
          </td>
        </tr>"""

    sigma       = result.get("sigma_annual_pct", 0)
    n_sim_shown = mc7.get("n_sim", N_SIM)
    elapsed     = result.get("elapsed_seconds", 0)

    html = f"""
<div style="background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:22px;margin:16px 0;
            font-family:'SF Pro Display',system-ui,-apple-system,sans-serif;color:#f1f5f9">

  <!-- 标题栏 -->
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:18px">
    <div>
      <div style="font-size:15px;font-weight:700;color:#f1f5f9">⚠️ 风险量化分析</div>
      <div style="font-size:11px;color:#475569;margin-top:3px">
        Monte Carlo VaR · 参数法 · 历史模拟 · 5大压力测试  |  {ticker}  |  σ={sigma:.1f}%/年
      </div>
    </div>
    <div style="background:{rc}22;border:1px solid {rc};border-radius:20px;padding:5px 16px">
      <span style="color:{rc};font-weight:700;font-size:13px">风险等级 · {rl}</span>
    </div>
  </div>

  <!-- VaR 核心指标 -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px">
    <div style="background:#1e293b;border-radius:10px;padding:14px;border-left:3px solid #ef4444">
      <div style="font-size:11px;color:#64748b;margin-bottom:6px">7日 VaR@99%（三法共识）</div>
      <div style="font-size:32px;font-weight:800;color:#ef4444;letter-spacing:-1px">{consensus_var:+.2f}%</div>
      <div style="font-size:11px;color:#475569;margin-top:4px">1% 概率下最大单周损失（持仓期）</div>
    </div>
    <div style="background:#1e293b;border-radius:10px;padding:14px;border-left:3px solid #f97316">
      <div style="font-size:11px;color:#64748b;margin-bottom:6px">30日 CVaR@99%（Expected Shortfall）</div>
      <div style="font-size:32px;font-weight:800;color:#f97316;letter-spacing:-1px">{cvar_30:+.2f}%</div>
      <div style="font-size:11px;color:#475569;margin-top:4px">越过 VaR 后的平均尾部损失（更保守）</div>
    </div>
  </div>

  <!-- VaR 三法对比 + 价格目标 -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px">
    <div style="background:#1e293b;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#64748b;margin-bottom:10px">7日 VaR@99% — 三种方法对比</div>
      <table style="width:100%;border-collapse:collapse">
        {var_rows_html}
      </table>
    </div>
    <div style="background:#1e293b;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#64748b;margin-bottom:10px">
        7日 Monte Carlo 价格目标（{n_sim_shown:,} 次模拟）
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
        {pt_html}
      </div>
    </div>
  </div>

  <!-- 压力测试 -->
  <div style="background:#1e293b;border-radius:10px;padding:14px">
    <div style="font-size:11px;color:#64748b;margin-bottom:10px">5大宏观情景压力测试 — 估算最大损失</div>
    <table style="width:100%;border-collapse:collapse">
      <tr style="border-bottom:1px solid #334155">
        <th style="padding:5px 10px;text-align:left;font-size:10px;color:#475569;font-weight:600">情景</th>
        <th style="padding:5px 10px;text-align:left;font-size:10px;color:#475569;font-weight:600">冲击幅度 / 压力价格</th>
      </tr>
      {stress_rows_html}
    </table>
  </div>

  <!-- 注脚 -->
  <div style="margin-top:12px;font-size:10px;color:#334155;display:flex;justify-content:space-between">
    <span>模型：GBM对数正态 · Cholesky相关矩阵 · 历史非参数 · Beta回归</span>
    <span>耗时 {elapsed:.1f}s · 仅供研究参考，不构成投资建议</span>
  </div>

</div>"""
    return html


# ─────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Alpha Hive 风险量化引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python risk_engine.py NVDA
  python risk_engine.py TSLA --horizon 7
  python risk_engine.py NVDA --portfolio NVDA TSLA MSFT AMZN
  python risk_engine.py NVDA --json > nvda_risk.json
""")
    parser.add_argument("ticker",      nargs="?", default="NVDA",
                        help="主分析标的（默认: NVDA）")
    parser.add_argument("--horizon",   type=int, default=30,
                        help="MC 主模拟天数（默认: 30）")
    parser.add_argument("--sims",      type=int, default=N_SIM,
                        help=f"蒙特卡洛模拟次数（默认: {N_SIM:,}）")
    parser.add_argument("--portfolio", nargs="+",
                        help="组合分析标的列表（e.g. --portfolio NVDA TSLA MSFT）")
    parser.add_argument("--json",      action="store_true",
                        help="输出完整 JSON")
    args = parser.parse_args()

    print(f"\n{'='*55}")
    print(f"  🔬 Alpha Hive 风险量化引擎  —  {args.ticker}")
    print(f"{'='*55}")

    result = run_full_risk_analysis(
        args.ticker,
        include_portfolio=bool(args.portfolio),
        portfolio_tickers=args.portfolio,
        n_sim=args.sims,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    # ── 格式化输出 ──
    S       = result.get("summary", {})
    mc7     = result.get("mc_var_7d", {})
    mc30    = result.get("mc_var_30d", {})
    st      = result.get("stress_tests", {})

    print(f"\n📊 基本信息")
    print(f"   当前价格 : ${result['entry_price']}")
    print(f"   年化波动 : {result['sigma_annual_pct']:.1f}%")
    print(f"   风险等级 : {S.get('risk_level','?').upper()}")

    print(f"\n📉 VaR 分析 — 7日窗口（三法对比）")
    for method, val in S.get("var_99_7d_comparison", {}).items():
        label = {"historical": "历史模拟", "parametric": "参数法",
                 "monte_carlo": "蒙特卡洛"}.get(method, method)
        bar = "█" * int(abs(val) / 2)
        print(f"   {label:14s} VaR@99% = {val:+.2f}% {bar}")
    if S.get("var_99_7d_consensus"):
        print(f"   {'三法共识':14s}        = {S['var_99_7d_consensus']:+.2f}%  ←  关键参考值")

    print(f"\n🔥 尾部风险 — 30日窗口")
    if mc30.get("cvar", {}).get("99"):
        print(f"   CVaR@99% (ES)   = {mc30['cvar']['99']:+.2f}%  (超过VaR后的平均损失)")
    if mc30.get("cvar", {}).get("95"):
        print(f"   CVaR@95%        = {mc30['cvar']['95']:+.2f}%")

    print(f"\n🎯 MC 价格目标 (7日, {mc7.get('n_sim', N_SIM):,} 次模拟)")
    pt = S.get("price_targets_7d", {})
    if pt:
        entry = result["entry_price"]
        for label, key in [("熊市 P10", "bear_case"), ("基准 P50", "base_case"), ("牛市 P90", "bull_case")]:
            p   = pt.get(key, 0)
            chg = (p / entry - 1) * 100 if entry > 0 else 0
            print(f"   {label:12s} : ${p:.2f}  ({chg:+.1f}%)")

    print(f"\n💥 5大宏观情景压力测试")
    for _k, sc in st.get("scenarios", {}).items():
        if sc.get("shock_pct") is not None:
            sev = {"extreme": "🔴", "severe": "🟠",
                   "moderate": "🟡", "mild": "🟢", "negligible": "⚪"}.get(
                sc.get("severity", ""), "⚪")
            print(f"   {sev} {sc['name']:26s}  {sc['shock_pct']:+.1f}%  → ${sc['stressed_price']:.2f}")
    wc = st.get("worst_case", {})
    if wc.get("shock_pct") is not None:
        print(f"\n   ⚠️  最坏情景: {wc.get('name','')}  →  {wc['shock_pct']:+.1f}%  / ${wc['stressed_price']:.2f}")

    if args.portfolio:
        pv = result.get("portfolio_var", {})
        if pv and "error" not in pv:
            print(f"\n📦 组合 VaR — {' + '.join(pv.get('tickers', []))}")
            print(f"   组合 VaR@99% (30日)   = {pv['portfolio_var_99']:+.2f}%")
            print(f"   组合 CVaR@99%         = {pv['portfolio_cvar_99']:+.2f}%")
            print(f"   无分散化 VaR@99%      = {pv['undiversified_var_99']:+.2f}%")
            print(f"   多元化收益            = +{pv['diversification_benefit_pct']:.2f}%")
            if pv.get("max_corr_pair"):
                p = pv["max_corr_pair"]
                print(f"   最高相关: {p[0]}-{p[1]} = {p[2]:.3f}")
            if pv.get("min_corr_pair"):
                p = pv["min_corr_pair"]
                print(f"   最低相关: {p[0]}-{p[1]} = {p[2]:.3f}")

    print(f"\n⏱  总耗时: {result['elapsed_seconds']:.2f}s")
    print()
