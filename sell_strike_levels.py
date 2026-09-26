"""卖权行权价选择器 · 纯计算层（v0.45.333）：BS 希腊值 + GEXBot 式水平地图。

零 I/O，只 import math / numpy / typing。输入是 `cboe_options.fetch_cboe_raw_contracts`
产出的合约 dict 列表（键：expiry / cp / strike / dte / iv / delta / gamma / oi …，
iv 为**小数**，缺失为 None）。

为什么不复用 `advanced_analyzer.DealerGEXAnalyzer`（critic.md 已数值实测，三处与本任务冲突）：
  · flip：它找「相邻行权价 net_gex 变号」，ATM 附近 put 主导转 call 主导几乎必然在现价旁
    变号（83 条实测 54% 落在 2% 以内）。本模块做**重定价扫描**：每张合约固定自身 IV，
    在一串假想现价上重算 gamma，找总量过零点（Perfiliev / SpotGamma 口径）。
  · vanna：它多除了 S√T（S=100,K=95,30DTE,σ=.35 实测差 28.67×）。
  · GEX 单位：它是 S¹（百万美元/每 $1），本模块用 GEXBot 的 S²·0.01（美元/每 1% 变动）。
复用等于把已知 bug 一起继承；所以这里独立实现，并由 tests/ 用有限差分钉住。

─── 暴露约定（所有 *_usd_* 字段都按此口径）───────────────────────────────
朴素 OI 口径：call 记 +、put 记 −，等价于假设**做市商多 call、空 put**。
⚠️ 个股上 put 侧符号可能反了（Garleanu-Pedersen-Poteshman 2009：做市商往往两边都净多），
所以 GEX 只作环境参考，不是可交易信号。

  GEX   （每张合约）= sign·γ·OI·100·S²·0.01           USD per 1% move，sign: C=+1, P=−1
  DEX   （每张合约）= Δ·OI·100·S                       USD，**持有者口径**（call Δ>0、put Δ<0）
        ⚠️ 与 GEX 的做市商符号不同，**不得混用 / 池化**。Δ 用 CBOE 原始 delta，None 时 BS 兜底。
  vanna 暴露        = sign·vanna·OI·100·S·0.01          USD delta per 1 vol point（做市商符号）
  charm 暴露        = sign·charm·OI·100·S/365           USD delta per calendar day（做市商符号）

每张合约用**自己的** T（year_fraction(dte)）与 IV 算，再按行权价求和；**禁止**每个行权价
只算一个 greek 再乘 (OI_call − OI_put) —— 同一行权价上不同到期日、call/put 不同 IV 时，
那样算出来的不是同一个量。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

LEVELS_SCHEMA_VERSION = 1
# 与 advanced_analyzer.DealerGEXAnalyzer.RISK_FREE_RATE / cboe_options._BS_RISK_FREE 同值，
# 但刻意是**独立常量**：本模块不得 import advanced_analyzer（见模块 docstring）。
RISK_FREE_RATE = 0.045
# 与 advanced_analyzer.py `_enrich_with_bs_gamma` 的 `max(dte, 0.5)` 同一超短期下限，
# 不造第三种口径。
T_FLOOR_DAYS = 0.5
DAYS_PER_YEAR = 365.0

_SQRT_2PI = math.sqrt(2.0 * math.pi)
_SIGN = {"C": 1.0, "P": -1.0}


# ─────────────────────────────── 标量 BS

def _num(x) -> Optional[float]:
    """有限 float 或 None（bool 不算数）。"""
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(x) -> Optional[float]:
    f = _num(x)
    return f if (f is not None and f > 0) else None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def year_fraction(dte) -> Optional[float]:
    """日历 DTE → 年化时间，带 0.5 天下限。dte 非有限数 ⇒ None。"""
    d = _num(dte)
    if d is None:
        return None
    return max(d, T_FLOOR_DAYS) / DAYS_PER_YEAR


def bs_d1_d2(S, K, T, r, sigma) -> Optional[Tuple[float, float]]:
    """(d1, d2)；S/K/T/σ ≤ 0 或非有限、r 非有限 ⇒ None。"""
    S, K, T, sigma, r = _pos(S), _pos(K), _pos(T), _pos(sigma), _num(r)
    if S is None or K is None or T is None or sigma is None or r is None:
        return None
    vol_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / vol_t
    return d1, d1 - vol_t


def _cp(cp) -> Optional[str]:
    c = str(cp or "").upper()[:1]
    return c if c in ("C", "P") else None


def bs_delta(S, K, T, r, sigma, cp) -> Optional[float]:
    """call N(d1)，put N(d1)−1（q=0）。"""
    d, c = bs_d1_d2(S, K, T, r, sigma), _cp(cp)
    if d is None or c is None:
        return None
    return _norm_cdf(d[0]) if c == "C" else _norm_cdf(d[0]) - 1.0


def bs_gamma(S, K, T, r, sigma) -> Optional[float]:
    """φ(d1)/(Sσ√T)。非法输入返回 None——**不是 0.0**：0 是一个合法的 gamma 值，
    拿它当「算不出来」会被下游当成观测值求和（advanced_analyzer.bs_gamma 就是这么返回的）。"""
    d = bs_d1_d2(S, K, T, r, sigma)
    if d is None:
        return None
    return _norm_pdf(d[0]) / (float(S) * float(sigma) * math.sqrt(float(T)))


def bs_vanna(S, K, T, r, sigma) -> Optional[float]:
    """∂Δ/∂σ = −φ(d1)·d2/σ（σ 为小数）。

    ⚠️ **不除 S√T**。advanced_analyzer._vanna_stress_test 那份除了，
    S=100,K=95,30DTE,σ=.35 下有限差分 −0.4745、它给 −0.01655，比值 28.67 = S√T。
    """
    d = bs_d1_d2(S, K, T, r, sigma)
    if d is None:
        return None
    return -_norm_pdf(d[0]) * d[1] / float(sigma)


def bs_charm(S, K, T, r, sigma, cp, q: float = 0.0) -> Optional[float]:
    """charm = ∂Δ/∂t，t 为**日历时间向前**（τ 减少），单位「Δ 每年」。Wikipedia 形式：

        call:  q·e^{−qτ}N(d1)  − e^{−qτ}φ(d1)·[2(r−q)τ − d2σ√τ]/(2τσ√τ)
        put : −q·e^{−qτ}N(−d1) − e^{−qτ}φ(d1)·[2(r−q)τ − d2σ√τ]/(2τσ√τ)

    q=0 时两式相等 —— **不是 bug**：Δ_put = Δ_call − 1，差一个常数，对时间的导数相同。
    符号约定用有限差分 `(Δ(τ−ε) − Δ(τ))/ε` 钉住（tests/test_sell_strike_levels.py）；
    锚点 S=100,K=95,τ=30/365,r=.045,σ=.35 ⇒ ≈ +0.8607（ITM call 随时间流逝 Δ 走向 1）。
    写成 ∂Δ/∂τ 会整体反号——那正是本仓 market_intelligence 里「charm」误标的那类错。

    d1/d2 用 **r−q** 算（带股息的 BS）：只把 q 写进上面的显式项、d1 仍按 r 算，
    q≠0 时与有限差分对不上（测试用带 q 的独立 Δ 核对）。
    """
    c, qq, rr = _cp(cp), _num(q), _num(r)
    if c is None or qq is None or rr is None:
        return None
    d = bs_d1_d2(S, K, T, rr - qq, sigma)
    if d is None:
        return None
    d1, d2 = d
    T, sigma, r = float(T), float(sigma), float(r)
    sq = math.sqrt(T)
    disc = math.exp(-qq * T)
    common = disc * _norm_pdf(d1) * (2.0 * (r - qq) * T - d2 * sigma * sq) / (2.0 * T * sigma * sq)
    if c == "C":
        return qq * disc * _norm_cdf(d1) - common
    return -qq * disc * _norm_cdf(-d1) - common


def itm_probability(S, K, T, r, sigma, cp) -> Optional[float]:
    """到期 ITM 的风险中性概率：call N(d2)，put N(−d2)。

    **绝不是 |Δ|**：|Δ| = N(d1) 是对冲比率，比 N(d2) 多了 σ√T 的偏移。16Δ put、45DTE
    在 IV 30/50/80% 下真实 ITM 概率约 18.7/20.6/23.8%，IV 越高偏得越多。
    ⚠️ 这是**风险中性**概率；卖方有 VRP 时实际 ITM 频率预期低于它。
    """
    d, c = bs_d1_d2(S, K, T, r, sigma), _cp(cp)
    if d is None or c is None:
        return None
    return _norm_cdf(d[1]) if c == "C" else _norm_cdf(-d[1])


def sigma_distance(S, K, T, sigma) -> Optional[float]:
    """ln(K/S)/(σ√T)：行权价离现价几个（对数）σ。put 侧为负。"""
    S, K, T, sigma = _pos(S), _pos(K), _pos(T), _pos(sigma)
    if S is None or K is None or T is None or sigma is None:
        return None
    return math.log(K / S) / (sigma * math.sqrt(T))


def expected_move_1sigma(S, sigma, dte) -> Optional[float]:
    """1σ 期望波动 = S·σ·√T（T = year_fraction(dte)）。

    ⚠️ 不是「0.85×ATM 跨式」：r=0 时 ATM 跨式 ≈ √(2/π)·Sσ√T ≈ 0.798·Sσ√T，
    所以 1σ ≈ 1.2533×跨式；0.85×跨式只对应约 ±0.68σ（约 50% 区间），不是 68%。
    """
    S, sigma, T = _pos(S), _pos(sigma), year_fraction(dte)
    if S is None or sigma is None or T is None:
        return None
    return S * sigma * math.sqrt(T)


# ─────────────────────────────── 逐合约暴露

def _contract_T(c: dict) -> Optional[float]:
    d = _num(c.get("dte"))
    if d is None or d < 0:
        return None
    return year_fraction(d)


def _contract_basics(c) -> Optional[Tuple[float, str, float]]:
    """(strike, cp, sign) 或 None（形状非法）。"""
    if not isinstance(c, dict):
        return None
    K, cp = _pos(c.get("strike")), _cp(c.get("cp"))
    if K is None or cp is None:
        return None
    return K, cp, _SIGN[cp]


def strike_profile(contracts: List[dict], S, *, r: float = RISK_FREE_RATE) -> dict:
    """按行权价聚合的 GEX / DEX / vanna / charm（每张合约自己的 T 与 IV，见模块 docstring）。

    gamma 取数：CBOE gamma（非 None 且 >0；负 gamma 不是任何多头期权能有的值，按缺失处理）
    优先——它对应的就是当前价，不必重算；否则 BS（需 IV）。
    两者都没有 ⇒ 排除并计 `excluded_no_gamma`；`oi ≤ 0` ⇒ `excluded_no_oi`。
    vanna / charm 只能 BS 算：有 gamma 但没 IV 的合约照样进 GEX，但不进这两项，
    计 `excluded_no_iv_for_greeks`。Δ 没有（CBOE None 且无 IV）⇒ 不进 DEX，delta_source 记 none。
    形状非法（缺 strike / cp / dte）的计 `excluded_bad_contract`；S 非法时每张合约都
    计进这一项（全部被排除、n_contracts_used=0，下游看得见，不返回一份假装正常的空表）。
    """
    Sv = _pos(S)
    counts = {"n_contracts_used": 0,
              "gamma_source": {"cboe": 0, "bs": 0},
              "delta_source": {"cboe": 0, "bs": 0, "none": 0},
              "excluded_no_gamma": 0, "excluded_no_oi": 0,
              "excluded_no_iv_for_greeks": 0, "excluded_bad_contract": 0}
    agg: Dict[float, Dict[str, float]] = {}
    for c in contracts or []:
        basics = _contract_basics(c)
        T = _contract_T(c) if basics else None
        if basics is None or T is None or Sv is None:
            counts["excluded_bad_contract"] += 1
            continue
        K, cp, sign = basics
        oi = _num(c.get("oi"))
        if oi is None or oi <= 0:
            counts["excluded_no_oi"] += 1
            continue
        iv = _pos(c.get("iv"))
        g = _pos(c.get("gamma"))
        if g is not None:
            g_src = "cboe"
        else:
            g = bs_gamma(Sv, K, T, r, iv) if iv is not None else None
            g_src = "bs"
            if g is None:
                counts["excluded_no_gamma"] += 1
                continue
        counts["gamma_source"][g_src] += 1
        counts["n_contracts_used"] += 1

        dlt = _num(c.get("delta"))
        if dlt is not None and dlt != 0.0:
            counts["delta_source"]["cboe"] += 1
        else:
            dlt = bs_delta(Sv, K, T, r, iv, cp) if iv is not None else None
            counts["delta_source"]["bs" if dlt is not None else "none"] += 1

        row = agg.setdefault(K, {"call_gex": 0.0, "put_gex": 0.0, "dex": 0.0,
                                 "vanna": 0.0, "charm": 0.0, "call_oi": 0.0, "put_oi": 0.0})
        gex = sign * g * oi * 100.0 * Sv * Sv * 0.01
        row["call_gex" if cp == "C" else "put_gex"] += gex
        row["call_oi" if cp == "C" else "put_oi"] += oi
        if dlt is not None:
            row["dex"] += dlt * oi * 100.0 * Sv
        if iv is None:
            counts["excluded_no_iv_for_greeks"] += 1
            continue
        va = bs_vanna(Sv, K, T, r, iv)
        ch = bs_charm(Sv, K, T, r, iv, cp)
        if va is not None:
            row["vanna"] += sign * va * oi * 100.0 * Sv * 0.01
        if ch is not None:
            row["charm"] += sign * ch * oi * 100.0 * Sv / DAYS_PER_YEAR

    rows = []
    for K in sorted(agg):
        a = agg[K]
        rows.append({
            "strike": K,
            "call_gex_usd_per_1pct": a["call_gex"],
            "put_gex_usd_per_1pct": a["put_gex"],
            "net_gex_usd_per_1pct": a["call_gex"] + a["put_gex"],
            "net_dex_usd": a["dex"],
            "net_vanna_usd_per_volpt": a["vanna"],
            "net_charm_usd_per_day": a["charm"],
            "call_oi": a["call_oi"],
            "put_oi": a["put_oi"],
        })
    totals = {
        "net_gex_usd_per_1pct": sum(x["net_gex_usd_per_1pct"] for x in rows),
        "net_dex_usd": sum(x["net_dex_usd"] for x in rows),
        "net_vanna_usd_per_volpt": sum(x["net_vanna_usd_per_volpt"] for x in rows),
        "net_charm_usd_per_day": sum(x["net_charm_usd_per_day"] for x in rows),
        "call_oi": sum(x["call_oi"] for x in rows),
        "put_oi": sum(x["put_oi"] for x in rows),
    }
    return {"rows": rows, "totals": totals, "counts": counts}


# ─────────────────────────────── zero gamma（重定价扫描）

def _sweep_arrays(contracts, r):
    """扫描用的逐合约数组 + 排除计数。只收有 IV 且 OI>0 的合约。"""
    K, sig, T, sign, oi = [], [], [], [], []
    n_no_iv = n_no_oi = n_bad = 0
    for c in contracts or []:
        basics = _contract_basics(c)
        t = _contract_T(c) if basics else None
        if basics is None or t is None:
            n_bad += 1
            continue
        o = _num(c.get("oi"))
        if o is None or o <= 0:
            n_no_oi += 1
            continue
        iv = _pos(c.get("iv"))
        if iv is None:
            n_no_iv += 1
            continue
        K.append(basics[0])
        sign.append(basics[2])
        sig.append(iv)
        T.append(t)
        oi.append(o)
    arr = (np.asarray(K, float), np.asarray(sig, float), np.asarray(T, float),
           np.asarray(sign, float), np.asarray(oi, float))
    return arr, n_no_iv, n_no_oi, n_bad


def _total_gex_at(prices: np.ndarray, arr, r: float) -> np.ndarray:
    """Σ sign·γ(p)·OI·100·p²·0.01，合约 × 价格网格一次算完（numpy 广播）。"""
    K, sig, T, sign, oi = arr
    P = prices[:, None]                       # (G, 1)
    vol_t = sig * np.sqrt(T)                  # (N,)
    with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
        d1 = (np.log(P / K) + (r + 0.5 * sig * sig) * T) / vol_t
        gamma = np.exp(-0.5 * d1 * d1) / (_SQRT_2PI * P * vol_t)
        per = sign * gamma * oi * 100.0 * P * P * 0.01
    per = np.where(np.isfinite(per), per, 0.0)
    return per.sum(axis=1)


def _crossings(grid: np.ndarray, tot: np.ndarray) -> List[float]:
    """曲线 `tot(grid)` 的过零点（升序）。

    · 相邻两个非零点异号 ⇒ 线性插值；
    · 恰为 0 的点（或连续一段 0）⇒ 只有**两侧最近的非零点异号**才算一个过零点
      （单点就是该点本身，一段取段中点），不重复计。

    ⚠️ 偏离 SPEC 字面「恰为 0 的点算过零点」的理由：γ(p) ∝ exp(−d1²/2)，
    近月低 IV 合约在 ±20% 网格边缘会**下溢成精确的 0.0**（1DTE、IV 10%：
    d1≈−43 ⇒ e^{−911} < 最小双精度）。照字面，一条「全段为正、只是尾巴下溢」的曲线
    会在网格边缘报出假过零点，`all_positive` 变成 `crosses`、nearest_below 落在 0.8S。
    SPEC 那句话要防的是「根恰好落在网格点上被数两次」，本实现保留这个语义：
    两侧异号的精确零点照算、只算一次；两侧同号（相切）或落在网格边缘（下溢尾巴）的不算。
    """
    xs, vs = grid.tolist(), tot.tolist()
    n = len(vs)
    out: List[float] = []
    i = 0
    while i < n:
        if vs[i] == 0.0:
            j = i
            while j + 1 < n and vs[j + 1] == 0.0:
                j += 1
            left = vs[i - 1] if i > 0 else None
            right = vs[j + 1] if j + 1 < n else None
            if left is not None and right is not None and (left > 0) != (right > 0):
                out.append((xs[i] + xs[j]) / 2.0)
            i = j + 1
            continue
        if i + 1 < n and vs[i + 1] != 0.0 and (vs[i] > 0) != (vs[i + 1] > 0):
            a, b = vs[i], vs[i + 1]
            out.append(xs[i] - a * (xs[i + 1] - xs[i]) / (b - a))
        i += 1
    return sorted(out)


def zero_gamma_sweep(contracts: List[dict], S, *, r: float = RISK_FREE_RATE,
                     band_pct: float = 0.20, grid_points: int = 81) -> dict:
    """zero gamma = 总 GEX 作为**假想现价**函数的过零点（重定价扫描，不是逐行权价变号）。

    网格 `linspace((1−band)S, (1+band)S, grid_points)`，另单独算 p=S 这一点。每张合约
    **固定自身 IV**、T=year_fraction(dte)，在每个 p 上用 BS 重算 γ(p)：
      total(p) = Σ sign·γ(p)·OI·100·p²·0.01
    **不用 CBOE gamma**：那个值只对应当前价，混进扫描曲线会让 p=S 这一点与其余点口径不一；
    路由只读这条曲线，所以它必须内部自洽。无 IV（`excluded_no_iv`）或 OI≤0
    （`excluded_no_oi`）的合约不进扫描——SPEC 写的是两者都计进 `excluded_no_iv`，
    这里拆成两个键：「CBOE 没给 IV」与「没人持仓」是两种性质不同的缺失，折成一个数
    就分不出是数据源坏了还是远翼本来没人。

    过零点：相邻网格点异号处线性插值；恰为 0 的网格点只在两侧异号时算一个
    （不重复计，下溢尾巴不算——理由见 `_crossings`）。
    状态：
      crosses / all_positive / all_negative —— **全段同号是最强的信号，不是 unavailable**；
      insufficient_contracts —— 只在可用合约为 0 或整条曲线全为 0 时（sign_at_spot=None）。
    """
    Sv = _pos(S)
    try:
        band = float(band_pct)
        gp = int(grid_points)
    except (TypeError, ValueError):
        band, gp = float("nan"), 0
    arr, n_no_iv, n_no_oi, n_bad = _sweep_arrays(contracts, r)
    n = int(arr[0].size)
    out = {"curve_state": "insufficient_contracts", "sign_at_spot": None, "total_at_spot": None,
           "crossings": [], "n_crossings": 0, "nearest": None,
           "nearest_below": None, "nearest_above": None,
           "zg_below_pct": None, "zg_above_pct": None,
           "grid_lo": None, "grid_hi": None, "grid_points": gp,
           "n_contracts": n, "excluded_no_iv": n_no_iv,
           "excluded_no_oi": n_no_oi, "excluded_bad_contract": n_bad}
    if Sv is None or not (math.isfinite(band) and 0 < band < 1) or gp < 2:
        return out
    grid = np.linspace((1.0 - band) * Sv, (1.0 + band) * Sv, gp)
    out["grid_lo"], out["grid_hi"] = float(grid[0]), float(grid[-1])
    if n == 0:
        return out
    tot = _total_gex_at(grid, arr, float(r))
    at_spot = float(_total_gex_at(np.asarray([Sv]), arr, float(r))[0])
    if not np.any(tot != 0.0) and at_spot == 0.0:
        return out

    crossings = _crossings(grid, tot)
    below = [x for x in crossings if x <= Sv]
    above = [x for x in crossings if x >= Sv]
    nb = max(below) if below else None
    na = min(above) if above else None
    nonzero = tot[tot != 0.0]
    if crossings:
        state = "crosses"
    elif nonzero.size and np.all(nonzero > 0):
        state = "all_positive"
    else:
        state = "all_negative"
    out.update({
        "curve_state": state,
        "sign_at_spot": "positive" if at_spot > 0 else ("negative" if at_spot < 0 else "zero"),
        "total_at_spot": at_spot,
        "crossings": crossings,
        "n_crossings": len(crossings),
        "nearest": min(crossings, key=lambda x: abs(x - Sv)) if crossings else None,
        "nearest_below": nb,
        "nearest_above": na,
        "zg_below_pct": (Sv - nb) / Sv * 100.0 if nb is not None else None,
        "zg_above_pct": (na - Sv) / Sv * 100.0 if na is not None else None,
    })
    return out


# ─────────────────────────────── majors / 分期限视图

def majors(rows: List[dict]) -> dict:
    """四个位各自独立计算：

      net_major_pos    净 GEX 最大且 >0 的行权价（GEXBot 的 Major Positive）
      net_major_neg    净 GEX 最小且 <0 的行权价（GEXBot 的 Major Negative）
      call_side_extreme  call_gex 最大且 >0（旧 `largest_call_wall` 的口径）
      put_side_extreme   put_gex 最小且 <0（旧 `largest_put_wall` 的口径）

    旧 wall 是单边极值、GEXBot Major 是净值极值——put 侧实测 54/83 对不上
    （AMC 全链没有一个净 GEX 为负的行权价照样报出 put wall）。**改名**就是为了
    不让同名字段装两个量。并列取行权价较低者（rows 已按 strike 升序）。
    """
    def _pick(key, want_pos):
        best = None
        for x in rows or []:
            v = _num(x.get(key))
            if v is None or (v <= 0 if want_pos else v >= 0):
                continue
            if best is None or (v > best[1] if want_pos else v < best[1]):
                best = (x.get("strike"), v)
        return best if best else (None, None)

    pk, pv = _pick("net_gex_usd_per_1pct", True)
    nk, nv = _pick("net_gex_usd_per_1pct", False)
    ck, cv = _pick("call_gex_usd_per_1pct", True)
    qk, qv = _pick("put_gex_usd_per_1pct", False)
    return {"net_major_pos_strike": pk, "net_major_pos_gex": pv,
            "net_major_neg_strike": nk, "net_major_neg_gex": nv,
            "call_side_extreme_strike": ck, "call_side_extreme_gex": cv,
            "put_side_extreme_strike": qk, "put_side_extreme_gex": qv}


def _view(contracts: List[dict], S, r: float) -> dict:
    prof = strike_profile(contracts, S, r=r)
    return {"expiries": sorted({c.get("expiry") for c in contracts
                                if isinstance(c, dict) and c.get("expiry")}),
            "n_contracts": len(contracts),
            "profile": prof,
            "zero_gamma": zero_gamma_sweep(contracts, S, r=r),
            "majors": majors(prof["rows"])}


def term_views(contracts: List[dict], S, *, r: float = RISK_FREE_RATE) -> dict:
    """三个期限视图：next_expiry（最小 dte 的那个到期日）/ le_45dte（dte ≤ 45）/ full。

    GEX 是带符号求和，对到期日集合截断可能翻号（v0.45.197 实测），所以三个视图并列给出、
    各自自洽，路由固定读 le_45dte（见 sell_strike_candidates.ROUTE_VIEW）。空视图给
    n_contracts=0 与 insufficient_contracts，不抛。
    """
    allc = list(contracts or [])
    valid = [c for c in allc if isinstance(c, dict)
             and _num(c.get("dte")) is not None and _num(c.get("dte")) >= 0]
    next_exp = None
    if valid:
        next_exp = min(valid, key=lambda c: (_num(c.get("dte")), str(c.get("expiry"))))["expiry"]
    return {
        "next_expiry": _view([c for c in valid if c.get("expiry") == next_exp], S, r),
        "le_45dte": _view([c for c in valid if _num(c.get("dte")) <= 45], S, r),
        # full 收**原始全集**（含 dte 缺失/为负/形状非法的行）：它们在 profile 与扫描里
        # 各自计进 excluded_bad_contract —— 先在这里滤掉就没有任何地方会数到它们。
        "full": _view(allc, S, r),
    }


def level_map(contracts: List[dict], S, *, r: float = RISK_FREE_RATE) -> dict:
    return {
        "schema_version": LEVELS_SCHEMA_VERSION,
        "chain_view": "sell_strike_raw_payload",
        "units": {"gex": "USD per 1% move", "dex": "USD (holder sign)",
                  "vanna": "USD delta per 1 vol point", "charm": "USD delta per calendar day",
                  "zg_pct": "percent of spot"},
        "sign_convention": "naive_oi_dealer_long_calls_short_puts",
        "views": term_views(contracts, S, r=r),
    }
