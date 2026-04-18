#!/usr/bin/env python3
"""
🐝 Alpha Hive – Fama-French 6 因子 Alpha 归因分析
===================================================
FF6 = FF5 (Mkt-RF, SMB, HML, RMW, CMA) + Momentum (MOM)

数据源 : Kenneth French Data Library（直接 HTTP 下载 ZIP，24h 本地缓存）
方法   : OLS 时间序列回归（纯 numpy，t 统计量用 scipy.stats.t）
输出   : Jensen Alpha（年化）、6 个因子暴露、t 统计量、R²、IR
集成   : format_attribution_html() → 可直接嵌入 generate_deep_v2.py 报告

CLI 用法:
    python3 factor_attribution.py NVDA
    python3 factor_attribution.py NVDA --lookback 504 --json
    python3 factor_attribution.py NVDA --no-cache
"""

import json
import logging
import math
import time
import urllib.request
import zipfile
import io
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import threading

import numpy as np
import pandas as pd
import yfinance as yf

_log = logging.getLogger("alpha_hive.factor_attribution")
_factor_download_lock = threading.Lock()   # 防止并发写 parquet 缓存
_HOME = Path(__file__).parent
_CACHE_DIR = _HOME / ".factor_cache"
_CACHE_DIR.mkdir(exist_ok=True)

TRADING_DAYS = 252

# ── 因子名称映射 ────────────────────────────────────────────────────────────────
FACTOR_LABELS_ZH = {
    "Mkt-RF": "市场风险溢价",
    "SMB":    "规模因子（小-大）",
    "HML":    "价值因子（高-低B/M）",
    "RMW":    "盈利因子（强-弱盈利）",
    "CMA":    "投资因子（保守-激进）",
    "MOM":    "动量因子（12-1M）",
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. 数据下载与缓存
# ─────────────────────────────────────────────────────────────────────────────

_FF5_URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
            "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip")
_MOM_URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
            "F-F_Momentum_Factor_daily_CSV.zip")

_CACHE_TTL = 86400          # 24 小时（秒），French 库每月更新一次
_DOWNLOAD_TIMEOUT = 20      # HTTP 超时


def _fetch_zip_csv(url: str, timeout: int = _DOWNLOAD_TIMEOUT) -> str:
    """下载 ZIP，返回内部 CSV 文本（UTF-8）。"""
    resp = urllib.request.urlopen(url, timeout=timeout)
    z = zipfile.ZipFile(io.BytesIO(resp.read()))
    return z.read(z.namelist()[0]).decode("utf-8", errors="replace")


def _parse_french_csv(text: str, data_cols: List[str]) -> pd.DataFrame:
    """
    解析 Kenneth French CSV 文本。
    跳过文本头部说明，找到含因子名的标题行，读取后续数字行。
    返回 DataFrame，index 为 pd.Timestamp，列值为小数（除以 100）。
    """
    lines = text.splitlines()
    header_idx = None
    # 找含主要因子列名的行
    for i, ln in enumerate(lines):
        if any(col in ln for col in data_cols[:2]):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("无法解析 French CSV：未找到标题行")

    rows = []
    for ln in lines[header_idx + 1:]:
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        date_str = parts[0].strip()
        if len(date_str) != 8 or not date_str.isdigit():
            break               # 遇到非日期行（如年度数据段），停止
        try:
            dt = pd.Timestamp(date_str)
            vals = [float(x) / 100.0 for x in parts[1:len(data_cols) + 1]]
            if len(vals) == len(data_cols):
                rows.append([dt] + vals)
        except (ValueError, IndexError):
            continue

    df = pd.DataFrame(rows, columns=["Date"] + data_cols)
    df.set_index("Date", inplace=True)
    return df


def _load_cached(path: Path, max_age: int = _CACHE_TTL) -> Optional[pd.DataFrame]:
    """读取 parquet 缓存；超过 max_age 秒则返回 None。"""
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age:
        return None
    try:
        return pd.read_parquet(path)
    except Exception as e:
        _log.debug("缓存读取失败 %s: %s", path.name, e)
        return None


def _download_ff5(force: bool = False) -> pd.DataFrame:
    """返回 FF5 日频因子 DataFrame (Mkt-RF, SMB, HML, RMW, CMA, RF)，百分比 → 小数。"""
    cache_path = _CACHE_DIR / "ff5_daily.parquet"
    if not force:
        df = _load_cached(cache_path)
        if df is not None:
            return df

    with _factor_download_lock:
        # 二次检查：可能被另一线程抢先下载完毕
        if not force:
            df = _load_cached(cache_path)
            if df is not None:
                return df
        _log.info("factor_attribution: 下载 FF5 日频数据…")
        text = _fetch_zip_csv(_FF5_URL)
        cols = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
        df = _parse_french_csv(text, cols)
        df.to_parquet(cache_path)
        _log.info("factor_attribution: FF5 已缓存 (%d 行)", len(df))
    return df


def _download_mom(force: bool = False) -> pd.DataFrame:
    """返回 MOM 日频因子 DataFrame，百分比 → 小数。"""
    cache_path = _CACHE_DIR / "mom_daily.parquet"
    if not force:
        df = _load_cached(cache_path)
        if df is not None:
            return df

    with _factor_download_lock:
        # 二次检查：可能被另一线程抢先下载完毕
        if not force:
            df = _load_cached(cache_path)
            if df is not None:
                return df
        _log.info("factor_attribution: 下载 MOM 日频数据…")
        text = _fetch_zip_csv(_MOM_URL)
        df = _parse_french_csv(text, ["Mom"])
        df = df.rename(columns={"Mom": "MOM"})   # 统一大写
        df.to_parquet(cache_path)
        _log.info("factor_attribution: MOM 已缓存 (%d 行)", len(df))
    return df


def load_ff6_factors(start: pd.Timestamp, end: pd.Timestamp,
                     force: bool = False) -> pd.DataFrame:
    """
    合并 FF5 + MOM → FF6 因子 DataFrame，过滤到 [start, end]。
    列：Mkt-RF, SMB, HML, RMW, CMA, MOM, RF（均为小数）
    """
    ff5 = _download_ff5(force)
    mom = _download_mom(force)
    ff6 = ff5.join(mom, how="inner")          # 取交集日期
    ff6 = ff6.loc[start:end].copy()
    ff6 = ff6.dropna()
    return ff6


# ─────────────────────────────────────────────────────────────────────────────
# 2. 股票收益率
# ─────────────────────────────────────────────────────────────────────────────

def _get_stock_returns(ticker: str, start: pd.Timestamp,
                       end: pd.Timestamp) -> pd.Series:
    """从 yfinance 获取日收益率（小数），index 为 pd.Timestamp。"""
    hist = yf.Ticker(ticker).history(start=start.date(), end=end.date(), auto_adjust=True)
    if hist.empty or len(hist) < 20:
        raise ValueError(f"yfinance 数据不足：{ticker} ({len(hist)} 行)")
    prices = hist["Close"].astype(float)
    rets = prices.pct_change().dropna()
    # 统一转为 tz-naive 日期（去掉时区偏移，只保留日期部分）
    # tz_convert(None) 会保留时间偏移量（如05:00:00），导致与 FF6 日期索引对不上
    # 正确做法：取 .date() 后重建 DatetimeIndex，保证 00:00:00
    rets.index = pd.DatetimeIndex([pd.Timestamp(d.date()) for d in rets.index])
    rets.name = ticker
    return rets


# ─────────────────────────────────────────────────────────────────────────────
# 3. OLS 回归（纯 numpy + scipy.stats.t）
# ─────────────────────────────────────────────────────────────────────────────

def _ols(y: np.ndarray, X: np.ndarray, hac_lag: Optional[int] = None) -> Dict:
    """
    OLS 时间序列回归，支持 Newey-West HAC 标准误修正。

    X 已含截距列（第 0 列全为 1）。

    Args:
        y: 被解释变量 shape (n,)
        X: 自变量矩阵含截距 shape (n, k)
        hac_lag: Newey-West HAC 滞后阶数；None=标准 OLS；
                 建议 lag = floor(4*(n/100)^(2/9))，T+30 日度推荐 5

    返回 dict：beta, se, se_hac, t_stat, p_value, r2, adj_r2, residuals, method
    """
    n, k = X.shape

    # β = (X'X)^{-1} X'y
    XtX = X.T @ X
    Xty = X.T @ y
    try:
        beta = np.linalg.solve(XtX, Xty)
    except np.linalg.LinAlgError:
        beta = np.linalg.lstsq(X, y, rcond=None)[0]

    residuals = y - X @ beta
    ss_res = float(residuals @ residuals)
    ss_tot = float(((y - y.mean()) ** 2).sum())

    r2     = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / (n - k) if n > k else r2

    # ── 标准 OLS 标准误 ──
    s2 = ss_res / max(n - k, 1)
    try:
        XtX_inv = np.linalg.inv(XtX)
        cov_ols = s2 * XtX_inv
        se_ols = np.sqrt(np.maximum(np.diag(cov_ols), 0.0))
    except np.linalg.LinAlgError:
        XtX_inv = None
        se_ols = np.full(k, np.nan)

    # ── Newey-West HAC 标准误（修正序列自相关 + 异方差）──
    se_hac = None
    method = "OLS"
    if hac_lag is not None and hac_lag > 0 and XtX_inv is not None:
        try:
            # Bartlett kernel: w_l = 1 - l/(L+1), l = 0..L
            L = int(hac_lag)
            # S = Σ_l=-L..L w_l · (1/n) Σ_t X_t' e_t e_{t-l} X_{t-l}
            # 等价于：S0 + Σ_l=1..L w_l (Γ_l + Γ_l')
            S = np.zeros((k, k))
            # l=0: Σ_t e_t^2 X_t X_t'
            for t_idx in range(n):
                xt = X[t_idx, :].reshape(-1, 1)
                S += residuals[t_idx] ** 2 * (xt @ xt.T)
            # l=1..L
            for l in range(1, L + 1):
                w_l = 1.0 - l / (L + 1.0)
                G_l = np.zeros((k, k))
                for t_idx in range(l, n):
                    xt = X[t_idx, :].reshape(-1, 1)
                    xt_l = X[t_idx - l, :].reshape(-1, 1)
                    G_l += residuals[t_idx] * residuals[t_idx - l] * (xt @ xt_l.T)
                S += w_l * (G_l + G_l.T)
            # Newey-West var(β̂) = (X'X)^{-1} · S · (X'X)^{-1}
            cov_hac = XtX_inv @ S @ XtX_inv
            se_hac = np.sqrt(np.maximum(np.diag(cov_hac), 0.0))
            method = f"OLS+HAC(lag={L})"
        except Exception as e:
            _log.debug(f"HAC 计算失败，退回 OLS: {e}")
            se_hac = None

    # 用 HAC 优先，否则用 OLS
    se = se_hac if se_hac is not None else se_ols
    t_stat = np.where(se > 1e-12, beta / se, np.nan)

    # p 值（t 分布双尾）
    try:
        from scipy.stats import t as _t_dist
        p_value = 2.0 * (1.0 - _t_dist.cdf(np.abs(t_stat), df=n - k))
    except Exception:
        from scipy.special import erf as _erf
        p_value = 2.0 * (1.0 - 0.5 * (1.0 + _erf(np.abs(t_stat) / math.sqrt(2))))

    return {
        "beta":      beta,
        "se":        se,
        "se_ols":    se_ols,
        "se_hac":    se_hac,
        "t_stat":    t_stat,
        "p_value":   p_value,
        "r2":        r2,
        "adj_r2":    adj_r2,
        "residuals": residuals,
        "n":         n,
        "k":         k,
        "method":    method,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. 主函数：compute_factor_attribution
# ─────────────────────────────────────────────────────────────────────────────

FACTOR_COLS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "MOM"]


def compute_factor_attribution(
    ticker: str,
    lookback_days: int = 252,
    force_refresh: bool = False,
) -> Dict:
    """
    对 ticker 执行 FF6 时间序列 OLS，返回结构化结果 dict：

    Keys:
        ticker, lookback_days, n_obs, date_range
        alpha_daily, alpha_annual, alpha_t, alpha_p, alpha_sig
        factors: { "Mkt-RF": {loading, t_stat, p_value, sig}, … }
        r2, adj_r2
        tracking_error_annual    # 残差年化标准差
        information_ratio        # alpha_annual / tracking_error_annual
        risk_level               # "alpha_generator" / "factor_heavy" / "benchmark_hugging"
        summary_zh               # 一句话中文摘要
        error (可选)             # 失败时设置
    """
    result: Dict = {
        "ticker": ticker,
        "lookback_days": lookback_days,
        "factors": {},
    }

    try:
        end   = pd.Timestamp.today().normalize()
        start = end - pd.Timedelta(days=int(lookback_days * 1.6))   # 日历天 ≥ 交易日

        # ── 加载 FF6 因子 ──────────────────────────────────────────────────────
        ff6 = load_ff6_factors(start, end, force=force_refresh)
        if len(ff6) < 60:
            raise ValueError(f"FF6 因子数据不足（{len(ff6)} 行，需 ≥60）")

        # ── 获取股票超额收益 ───────────────────────────────────────────────────
        stock_rets = _get_stock_returns(ticker, start, end)

        # 对齐日期（取交集）
        common = ff6.index.intersection(stock_rets.index)
        common = common[-lookback_days:]      # 最多取 lookback_days 个交易日
        if len(common) < 60:
            raise ValueError(f"有效对齐交易日不足（{len(common)}，需 ≥60）")

        ff6_aligned  = ff6.loc[common]
        rets_aligned = stock_rets.loc[common].values

        # 超额收益 = 股票收益 - RF（无风险利率）
        rf = ff6_aligned["RF"].values
        y  = rets_aligned - rf

        # 因子矩阵（含截距）
        factor_vals = ff6_aligned[FACTOR_COLS].values
        ones        = np.ones((len(common), 1))
        X           = np.hstack([ones, factor_vals])   # shape (n, 7)

        # ── OLS ───────────────────────────────────────────────────────────────
        ols = _ols(y, X)
        beta     = ols["beta"]      # [alpha, b_mkt, b_smb, b_hml, b_rmw, b_cma, b_mom]
        t_stat   = ols["t_stat"]
        p_value  = ols["p_value"]
        residuals = ols["residuals"]

        # ── 整理结果 ──────────────────────────────────────────────────────────
        alpha_daily  = float(beta[0])
        alpha_annual = alpha_daily * TRADING_DAYS
        alpha_t      = float(t_stat[0])
        alpha_p      = float(p_value[0])
        alpha_sig    = _sig_stars(alpha_p)

        # 追踪误差（残差年化标准差）
        te_daily   = float(np.std(residuals, ddof=1))
        te_annual  = te_daily * math.sqrt(TRADING_DAYS)
        ir         = alpha_annual / te_annual if te_annual > 1e-8 else 0.0

        # 每个因子
        factors_out = {}
        for i, col in enumerate(FACTOR_COLS):
            b   = float(beta[i + 1])
            t   = float(t_stat[i + 1])
            p   = float(p_value[i + 1])
            factors_out[col] = {
                "loading": round(b, 4),
                "t_stat":  round(t, 2),
                "p_value": round(p, 4),
                "sig":     _sig_stars(p),
                "label_zh": FACTOR_LABELS_ZH[col],
            }

        # 风险类型判断
        mkt_beta = abs(factors_out["Mkt-RF"]["loading"])
        other_betas = [abs(factors_out[f]["loading"]) for f in FACTOR_COLS[1:]]
        if abs(alpha_annual) > 0.05 and alpha_p < 0.10:
            risk_level = "alpha_generator"     # 显著 Alpha
        elif mkt_beta > 0.9 and max(other_betas) < 0.3:
            risk_level = "benchmark_hugging"   # 接近指数
        else:
            risk_level = "factor_heavy"        # 因子暴露主导

        result.update({
            "n_obs":          len(common),
            "date_range":     [str(common[0].date()), str(common[-1].date())],
            "alpha_daily":    round(alpha_daily, 6),
            "alpha_annual":   round(alpha_annual, 4),
            "alpha_t":        round(alpha_t, 2),
            "alpha_p":        round(alpha_p, 4),
            "alpha_sig":      alpha_sig,
            "factors":        factors_out,
            "r2":             round(ols["r2"], 4),
            "adj_r2":         round(ols["adj_r2"], 4),
            "tracking_error_annual": round(te_annual, 4),
            "information_ratio":     round(ir, 3),
            "risk_level":     risk_level,
            "summary_zh":     _build_summary(ticker, alpha_annual, alpha_sig,
                                             ols["r2"], factors_out, ir),
        })

    except Exception as e:
        _log.warning("factor_attribution failed for %s: %s", ticker, e)
        result["error"] = str(e)

    return result


def _sig_stars(p: float) -> str:
    """p 值 → 显著性星号。"""
    if p < 0.01:  return "***"
    if p < 0.05:  return "**"
    if p < 0.10:  return "*"
    return ""


def _build_summary(ticker: str, alpha_annual: float, alpha_sig: str,
                   r2: float, factors: Dict, ir: float) -> str:
    """生成一句话中文摘要。"""
    alpha_pct = f"{alpha_annual * 100:+.1f}%"
    alpha_str = f"年化 Alpha {alpha_pct}{alpha_sig}"
    if not alpha_sig:
        alpha_str += "（不显著）"

    # 最大绝对暴露因子
    if not factors:
        return f"{ticker} — {alpha_str} | {r2_str} | {ir_str}"
    top_factor = max(factors.items(), key=lambda x: abs(x[1]["loading"]))
    top_name   = FACTOR_LABELS_ZH[top_factor[0]]
    top_load   = top_factor[1]["loading"]
    exp_str    = f"主要因子暴露：{top_name} ({top_load:+.2f})"

    r2_str = f"模型解释力 R²={r2:.1%}"
    ir_str = f"IR={ir:+.2f}"

    return f"{ticker} — {alpha_str} | {exp_str} | {r2_str} | {ir_str}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. 批量归因（供 generate_deep_v2 调用）
# ─────────────────────────────────────────────────────────────────────────────

def batch_attribution(tickers: List[str],
                      lookback_days: int = 252) -> Dict[str, Dict]:
    """对多个标的并行计算 FF6 归因。复用同一份 FF6 因子数据。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(compute_factor_attribution, t, lookback_days): t
                for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                results[t] = fut.result()
            except Exception as e:
                results[t] = {"ticker": t, "error": str(e)}
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 6. HTML 渲染（暗色主题，与 Alpha Hive 报告风格一致）
# ─────────────────────────────────────────────────────────────────────────────

def format_attribution_html(result: Dict) -> str:
    """
    生成暗色主题 HTML 卡片，可直接嵌入 generate_deep_v2.py 深度报告。
    """
    ticker = result.get("ticker", "?")

    if "error" in result:
        return (
            f'<div style="padding:12px;background:#1e1e2e;border-radius:8px;'
            f'color:#ef4444;font-size:12px;">⚠️ FF6 归因失败：{result["error"]}</div>'
        )

    alpha_ann  = result.get("alpha_annual", 0)
    alpha_sig  = result.get("alpha_sig", "")
    alpha_t    = result.get("alpha_t", 0)
    alpha_p    = result.get("alpha_p", 1)
    r2         = result.get("r2", 0)
    adj_r2     = result.get("adj_r2", 0)
    ir         = result.get("information_ratio", 0)
    te         = result.get("tracking_error_annual", 0)
    n_obs      = result.get("n_obs", 0)
    date_range = result.get("date_range", ["", ""])
    risk_level = result.get("risk_level", "")
    factors    = result.get("factors", {})
    summary    = result.get("summary_zh", "")

    # Alpha 颜色
    alpha_color = "#10b981" if alpha_ann > 0 else "#ef4444"
    alpha_pct_str = f"{alpha_ann * 100:+.2f}%"

    # 风险类型标签
    rl_map = {
        "alpha_generator":   ("Alpha 生成器", "#10b981"),
        "factor_heavy":      ("因子暴露型",   "#f59e0b"),
        "benchmark_hugging": ("指数追踪型",   "#6b7280"),
    }
    rl_label, rl_color = rl_map.get(risk_level, ("未知", "#6b7280"))

    # ── 因子暴露行 ───────────────────────────────────────────────────────────
    factor_rows = ""
    for fname in FACTOR_COLS:
        if fname not in factors:
            continue
        f = factors[fname]
        load = f["loading"]
        t    = f["t_stat"]
        p    = f["p_value"]
        sig  = f["sig"]
        lbl  = f["label_zh"]
        load_color = "#10b981" if load > 0 else "#ef4444"
        # 条形图宽度（最大 100px，对应 |loading|=2.0）
        bar_w = min(100, int(abs(load) * 50))
        bar_color = load_color
        sig_color = "#f59e0b" if sig else "#6b7280"
        factor_rows += f"""
        <tr>
          <td style="padding:5px 8px;color:#94a3b8;font-size:11px;">{lbl}</td>
          <td style="padding:5px 8px;text-align:right;">
            <span style="color:{load_color};font-weight:700;font-size:12px;">{load:+.3f}</span>
            <div style="display:inline-block;margin-left:4px;
                        width:{bar_w}px;height:6px;background:{bar_color};
                        border-radius:3px;vertical-align:middle;opacity:0.6;"></div>
          </td>
          <td style="padding:5px 8px;text-align:right;color:#cbd5e1;font-size:11px;">{t:+.2f}</td>
          <td style="padding:5px 8px;text-align:right;color:{sig_color};font-size:11px;
                     font-weight:700;">{sig if sig else '—'}</td>
        </tr>"""

    # ── 完整卡片 ─────────────────────────────────────────────────────────────
    html = f"""
<div style="background:#1e1e2e;border:1px solid #2a2a3e;border-radius:10px;
            padding:16px;margin:12px 0;font-family:'Segoe UI',system-ui,sans-serif;">

  <!-- 标题行 -->
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
    <div>
      <span style="color:#f1f5f9;font-size:14px;font-weight:700;">
        📐 Fama-French 6 因子归因 · {ticker}
      </span>
      <span style="margin-left:8px;font-size:10px;color:#64748b;">
        {date_range[0]} → {date_range[1]}（{n_obs} 交易日）
      </span>
    </div>
    <span style="background:{rl_color}22;color:{rl_color};padding:3px 8px;
                 border-radius:12px;font-size:10px;font-weight:700;">{rl_label}</span>
  </div>

  <!-- Alpha + 关键指标 -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px;">
    <div style="background:#0f172a;border-radius:8px;padding:10px;text-align:center;">
      <div style="color:#94a3b8;font-size:10px;margin-bottom:4px;">Jensen Alpha（年化）</div>
      <div style="color:{alpha_color};font-size:20px;font-weight:800;">{alpha_pct_str}</div>
      <div style="color:#64748b;font-size:10px;">t={alpha_t:+.2f}{("  " + alpha_sig) if alpha_sig else ""}</div>
    </div>
    <div style="background:#0f172a;border-radius:8px;padding:10px;text-align:center;">
      <div style="color:#94a3b8;font-size:10px;margin-bottom:4px;">R²</div>
      <div style="color:#f1f5f9;font-size:20px;font-weight:800;">{r2:.1%}</div>
      <div style="color:#64748b;font-size:10px;">Adj R²={adj_r2:.1%}</div>
    </div>
    <div style="background:#0f172a;border-radius:8px;padding:10px;text-align:center;">
      <div style="color:#94a3b8;font-size:10px;margin-bottom:4px;">信息比率（IR）</div>
      <div style="color:{'#10b981' if ir > 0.3 else '#f59e0b' if ir > 0 else '#ef4444'};
                  font-size:20px;font-weight:800;">{ir:+.2f}</div>
      <div style="color:#64748b;font-size:10px;">TE={te:.1%}/年</div>
    </div>
    <div style="background:#0f172a;border-radius:8px;padding:10px;text-align:center;">
      <div style="color:#94a3b8;font-size:10px;margin-bottom:4px;">Alpha p值</div>
      <div style="color:{'#10b981' if alpha_p < 0.05 else '#f59e0b' if alpha_p < 0.10 else '#6b7280'};
                  font-size:20px;font-weight:800;">{alpha_p:.3f}</div>
      <div style="color:#64748b;font-size:10px;">
        {'显著 p<0.05' if alpha_p < 0.05 else '边际 p<0.10' if alpha_p < 0.10 else '不显著'}
      </div>
    </div>
  </div>

  <!-- 因子暴露表 -->
  <div style="color:#94a3b8;font-size:11px;margin-bottom:6px;font-weight:600;">
    📊 6 因子暴露  <span style="color:#64748b;font-weight:400;">
    （*** p&lt;0.01  ** p&lt;0.05  * p&lt;0.10）</span>
  </div>
  <table style="width:100%;border-collapse:collapse;">
    <thead>
      <tr style="border-bottom:1px solid #2a2a3e;">
        <th style="padding:5px 8px;color:#64748b;font-size:10px;text-align:left;">因子</th>
        <th style="padding:5px 8px;color:#64748b;font-size:10px;text-align:right;">暴露（β）</th>
        <th style="padding:5px 8px;color:#64748b;font-size:10px;text-align:right;">t统计量</th>
        <th style="padding:5px 8px;color:#64748b;font-size:10px;text-align:right;">显著性</th>
      </tr>
    </thead>
    <tbody>{factor_rows}
    </tbody>
  </table>

  <!-- 摘要 -->
  <div style="margin-top:10px;padding:8px 10px;background:#0f172a;border-radius:6px;
              color:#94a3b8;font-size:11px;line-height:1.5;">
    {summary}
  </div>

  <div style="margin-top:6px;color:#475569;font-size:10px;text-align:right;">
    数据源：Kenneth French Data Library + yfinance · OLS 时间序列回归
  </div>
</div>"""

    return html


# ─────────────────────────────────────────────────────────────────────────────
# 7. CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli():
    import argparse, sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Fama-French 6 因子 Alpha 归因")
    parser.add_argument("ticker",    nargs="?", default="NVDA")
    parser.add_argument("--lookback", type=int, default=252,
                        help="回溯交易日数（默认252=1年）")
    parser.add_argument("--json",    action="store_true", help="输出 JSON")
    parser.add_argument("--no-cache", action="store_true", help="强制重新下载因子数据")
    parser.add_argument("--batch",   nargs="+", help="批量计算多个标的")
    args = parser.parse_args()

    t0 = time.time()
    if args.batch:
        results = batch_attribution(args.batch, args.lookback)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for tk, r in results.items():
                _print_result(tk, r)
    else:
        result = compute_factor_attribution(
            args.ticker, args.lookback, force_refresh=args.no_cache
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_result(args.ticker, result)

    print(f"\n⏱  总耗时: {time.time() - t0:.2f}s")


def _print_result(ticker: str, r: Dict):
    if "error" in r:
        print(f"\n❌ {ticker}: {r['error']}")
        return

    print(f"""
{'=' * 60}
  📐 FF6 因子归因  —  {ticker}
{'=' * 60}

🎯 Jensen Alpha（年化）: {r['alpha_annual'] * 100:+.2f}%  {r['alpha_sig']}
   t = {r['alpha_t']:+.2f}  |  p = {r['alpha_p']:.4f}  |  IR = {r['information_ratio']:+.2f}
   追踪误差: {r['tracking_error_annual']:.1%}/年

📊 R² = {r['r2']:.1%}  |  Adj R² = {r['adj_r2']:.1%}  |  样本 {r['n_obs']} 日

🏷  风险类型: {r['risk_level']}

📈 因子暴露（β）:""")

    factors = r.get("factors", {})
    for fname in FACTOR_COLS:
        if fname not in factors:
            continue
        f = factors[fname]
        bar = "█" * min(20, int(abs(f["loading"]) * 10))
        sign = "+" if f["loading"] >= 0 else ""
        print(f"   {fname:<8} {sign}{f['loading']:.3f} {bar:<20} "
              f"t={f['t_stat']:+.2f}  {f['sig']}")

    print(f"\n📝 {r.get('summary_zh', '')}")
    print(f"   数据：{r.get('date_range', ['',''])[0]} → {r.get('date_range', ['',''])[1]}")


if __name__ == "__main__":
    _cli()
