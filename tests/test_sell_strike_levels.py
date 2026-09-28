"""卖权行权价选择器 · 纯计算层守卫（v0.45.333，`sell_strike_levels`）。

**纪律。** 每条断言的 docstring 写明什么变异会让它变红；所有数值锚点都用**本文件自己的**
BS 实现 + 有限差分 / 二分法独立推出，不拿被测模块的函数去核对它自己
（那样只能证明「实现等于实现」）。本文件零外部依赖（夹具全是内存 dict），无 skip。

独立推导用到的公式（只用 math）：
    d1 = [ln(S/K) + (r − q + σ²/2)τ] / (σ√τ)，d2 = d1 − σ√τ
    Δ_call = e^{−qτ}N(d1)，Δ_put = e^{−qτ}(N(d1) − 1)
    γ = φ(d1) / (Sσ√τ)
"""

import math

import pytest

import sell_strike_levels as L
from advanced_analyzer import DealerGEXAnalyzer

R = 0.045


# ───────────────────────────── 独立 BS（不 import 被测模块的任何函数）

def _N(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d12(S, K, T, r, s, q=0.0):
    d1 = (math.log(S / K) + (r - q + 0.5 * s * s) * T) / (s * math.sqrt(T))
    return d1, d1 - s * math.sqrt(T)


def _delta(S, K, T, r, s, cp, q=0.0):
    d1, _ = _d12(S, K, T, r, s, q)
    disc = math.exp(-q * T)
    return disc * _N(d1) if cp == "C" else disc * (_N(d1) - 1.0)


def _gamma(S, K, T, r, s):
    d1, _ = _d12(S, K, T, r, s)
    return _phi(d1) / (S * s * math.sqrt(T))


def _total_gex(p, contracts, r=R):
    """重定价口径的总 GEX：每张合约固定自身 IV / T，在假想价 p 上重算 γ。"""
    tot = 0.0
    for c in contracts:
        T = max(c["dte"], 0.5) / 365.0
        sign = 1.0 if c["cp"] == "C" else -1.0
        tot += sign * _gamma(p, c["strike"], T, r, c["iv"]) * c["oi"] * 100.0 * p * p * 0.01
    return tot


def _bisect_root(f, lo, hi, n=200):
    flo = f(lo)
    assert flo * f(hi) < 0, "夹具自检：二分区间两端必须异号"
    for _ in range(n):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return 0.5 * (lo + hi)


def _k(cp, K, oi, iv, dte=30, gamma=None, delta=None, expiry="2026-10-23"):
    return {"symbol": f"X{expiry}{cp}{K}", "expiry": expiry, "cp": cp, "strike": float(K),
            "dte": dte, "iv": iv, "delta": delta, "gamma": gamma, "oi": float(oi)}


# ───────────────────────────── 1. vanna

def test_vanna_matches_finite_difference_of_delta_in_sigma():
    """bs_vanna = ∂Δ/∂σ（σ 为小数），用独立 Δ 的中心差分核对，三个行权价。

    变红的变异：照 advanced_analyzer 那份再除以 S√T（该点差 28.67×）；或把 −φ(d1)·d2/σ 的号写反。
    """
    S, T, s, h = 100.0, 30 / 365, 0.35, 1e-5
    for K in (95.0, 100.0, 110.0):
        fd = (_delta(S, K, T, R, s + h, "C") - _delta(S, K, T, R, s - h, "C")) / (2 * h)
        got = L.bs_vanna(S, K, T, R, s)
        assert got == pytest.approx(fd, rel=1e-5, abs=1e-8), f"K={K}"
    # critic.md 的实测锚点：S=100,K=95,30DTE,σ=.35 ⇒ 有限差分 −0.4745
    assert L.bs_vanna(S, 95.0, T, R, s) == pytest.approx(-0.4745, abs=5e-4)


# ───────────────────────────── 2. charm

def test_charm_sign_and_value_match_forward_time_finite_difference():
    """charm = ∂Δ/∂t（日历时间向前、τ 减少）：差分 `(Δ(τ−ε) − Δ(τ))/ε` 同号同值，锚点 ≈ +0.8607。

    变红的变异：整体反号；把差分口径换成 ∂Δ/∂τ（结果正好反号）；q=0 时 put 分支多减一个常数。
    """
    S, K, T, s, eps = 100.0, 95.0, 30 / 365, 0.35, 1e-7
    fd = (_delta(S, K, T - eps, R, s, "C") - _delta(S, K, T, R, s, "C")) / eps
    got = L.bs_charm(S, K, T, R, s, "C")
    assert fd > 0 and got > 0, "ITM call 随时间流逝 Δ 走向 1 ⇒ charm 必须为正"
    assert got == pytest.approx(fd, rel=1e-4)
    assert got == pytest.approx(0.8607, abs=5e-4)
    # q=0 时 put 与 call 相等：Δ_put = Δ_call − 1，差常数，时间导数相同（不是 bug）
    assert L.bs_charm(S, K, T, R, s, "P") == pytest.approx(got, rel=1e-12)


@pytest.mark.parametrize("q", [0.02, 0.05])
@pytest.mark.parametrize("cp", ["C", "P"])
def test_charm_with_dividend_yield_matches_finite_difference(q, cp):
    """带股息 q 时（Wikipedia 形式），call / put 各自与带 q 的独立 Δ 的前向时间差分一致。

    变红的变异：d1/d2 仍按 r 算、只把 q 写进显式项（q=0.02 时差约 1%）；
    put 分支的 −q·e^{−qτ}N(−d1) 写成 +。
    """
    S, K, T, s, eps = 100.0, 95.0, 30 / 365, 0.35, 1e-7
    fd = (_delta(S, K, T - eps, R, s, cp, q) - _delta(S, K, T, R, s, cp, q)) / eps
    assert L.bs_charm(S, K, T, R, s, cp, q=q) == pytest.approx(fd, rel=1e-4)


# ───────────────────────────── 3. ITM 概率

@pytest.mark.parametrize("iv,expected", [(0.30, 0.187), (0.50, 0.206), (0.80, 0.238)])
def test_itm_probability_is_n_minus_d2_not_abs_delta(iv, expected):
    """16Δ put、45DTE：先按**独立** Δ 二分反解 K，再看 itm_probability ≈ 18.7/20.6/23.8%。

    变红的变异：返回 |Δ|（恒为 0.16）；put 返回 N(d2)（互补，约 0.8）；用 d1 代替 d2。
    """
    S, T = 100.0, 45 / 365
    lo, hi = 1.0, 100.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _delta(S, mid, T, R, iv, "P") < -0.16:
            hi = mid
        else:
            lo = mid
    K = 0.5 * (lo + hi)
    assert _delta(S, K, T, R, iv, "P") == pytest.approx(-0.16, abs=1e-9), "夹具自检"
    p = L.itm_probability(S, K, T, R, iv, "P")
    assert p == pytest.approx(expected, abs=0.001)
    assert abs(p - 0.16) > 0.02, "ITM 概率与 |Δ| 必须分得开，否则本测试没有牙"
    # call 侧与独立 N(d2) 一致
    _, d2 = _d12(S, 110.0, T, R, iv)
    assert L.itm_probability(S, 110.0, T, R, iv, "C") == pytest.approx(_N(d2), rel=1e-12)


# ───────────────────────────── 4. 1σ 期望波动

def test_expected_move_is_s_sigma_sqrt_t_and_1_2533_times_atm_straddle():
    """1σ = S·σ·√T；r=0 时约等于 1.2533×ATM 跨式（√(π/2)），**不是** 0.85×跨式。

    跨式价格用独立 BS（call = S·N(d1) − K·N(d2)，r=0 平价）算，不经被测模块。
    变红的变异：返回 0.85×跨式（≈0.68·Sσ√T）；√T 写成 T；忘了 0.5 天下限。
    """
    S, s, dte = 100.0, 0.35, 30
    T = dte / 365
    em = L.expected_move_1sigma(S, s, dte)
    assert em == pytest.approx(S * s * math.sqrt(T), rel=1e-12)
    d1, d2 = _d12(S, S, T, 0.0, s)
    call = S * _N(d1) - S * _N(d2)
    straddle = 2 * call                          # r=0、K=S：put = call
    assert em / straddle == pytest.approx(math.sqrt(math.pi / 2), abs=0.002)
    assert abs(em - 0.85 * straddle) / em > 0.3
    # 0DTE 用 0.5 天下限，与 year_fraction 同口径
    assert L.expected_move_1sigma(S, s, 0) == pytest.approx(S * s * math.sqrt(0.5 / 365), rel=1e-12)
    assert L.year_fraction(0) == pytest.approx(0.5 / 365) and L.year_fraction(3) == pytest.approx(3 / 365)


# ───────────────────────────── 5. GEX 符号与单位 / DEX 持有者口径

def test_gex_sign_and_s_squared_units_and_dex_holder_sign():
    """GEX = sign·γ·OI·100·S²·0.01（call +、put −）；DEX = Δ·OI·100·S（持有者口径，不乘 sign）。

    夹具给定 CBOE gamma / delta（与 S 无关的常数），所以 S 翻倍 ⇒ GEX ×4、DEX ×2。
    变红的变异：put 符号取反；GEX 用线性 S（S·0.01 或 S¹ 的百万美元口径）；DEX 乘了做市商 sign。
    """
    call = _k("C", 100, 100, 0.3, gamma=0.05, delta=0.5)
    put = _k("P", 100, 100, 0.3, gamma=0.05, delta=-0.4)
    p1 = L.strike_profile([call, put], 100.0)
    p2 = L.strike_profile([call, put], 200.0)
    row1, row2 = p1["rows"][0], p2["rows"][0]
    assert row1["call_gex_usd_per_1pct"] == pytest.approx(0.05 * 100 * 100 * 100.0 ** 2 * 0.01)
    assert row1["put_gex_usd_per_1pct"] == pytest.approx(-0.05 * 100 * 100 * 100.0 ** 2 * 0.01)
    assert row2["call_gex_usd_per_1pct"] / row1["call_gex_usd_per_1pct"] == pytest.approx(4.0)
    assert row1["net_dex_usd"] == pytest.approx((0.5 - 0.4) * 100 * 100 * 100.0)
    only_put = L.strike_profile([put], 100.0)["rows"][0]
    assert only_put["net_dex_usd"] == pytest.approx(-0.4 * 100 * 100 * 100.0), "put 的 DEX 为负（持有者口径）"
    assert only_put["net_gex_usd_per_1pct"] < 0, "put 的 GEX 为负（做市商口径）"
    assert p1["counts"]["gamma_source"] == {"cboe": 2, "bs": 0}
    assert p1["counts"]["delta_source"] == {"cboe": 2, "bs": 0, "none": 0}


@pytest.mark.parametrize("cp,cboe_delta,iv,want_src", [
    ("P", 0.25, 0.30, "bs"),        # put 给了正 Δ（符号矛盾）
    ("C", -0.30, 0.30, "bs"),       # call 给了负 Δ
    ("P", 0.0, 0.30, "bs"),
    ("P", None, 0.30, "bs"),
    ("P", 0.25, None, "none"),      # 符号矛盾且无 IV ⇒ 兜底也没有，不进 DEX
    ("P", -0.25, 0.30, "cboe"),
    ("C", 0.30, 0.30, "cboe"),
])
def test_dex_rejects_wrong_sign_or_zero_cboe_delta_same_rule_as_candidates(cp, cboe_delta, iv, want_src):
    """CBOE Δ 与方向符号矛盾 / 为 0 / None ⇒ 当缺失、BS 兜底（无 IV 则不进 DEX）；DEX 用兜底值。
    同一张合约在 `sell_strike_candidates._contract_delta`（选档用）里必须得出同一个来源——
    两处各写一份规则（候选层 import 本模块，这里不能反向 import），本条逐例核对它们没有漂移。

    变红的变异：strike_profile 只判 None / 0（原写法）⇒ put 的 +0.25 原样进 DEX，持有者口径的 put DEX
    成了正数、delta_source 记 cboe；两处规则只改了一处。
    """
    import sell_strike_candidates as SC
    S, T = 100.0, 30 / 365
    K = 95.0 if cp == "P" else 105.0
    c = _k(cp, K, 100, iv, gamma=0.02, delta=cboe_delta)      # 给 CBOE gamma：无 IV 的那例也得进 profile
    prof = L.strike_profile([c], S)
    assert prof["counts"]["n_contracts_used"] == 1, "夹具自检：合约进了 profile"
    assert prof["counts"]["delta_source"][want_src] == 1, prof["counts"]["delta_source"]
    dex = prof["rows"][0]["net_dex_usd"]
    if want_src == "none":
        assert dex == 0.0
    else:
        want = cboe_delta if want_src == "cboe" else _delta(S, K, T, R, iv, cp)
        assert dex == pytest.approx(want * 100 * 100 * S)
        assert (dex > 0) == (cp == "C"), "持有者口径：call 的 DEX 为正、put 为负"
    assert SC._contract_delta(c, S, L.year_fraction(30), L.RISK_FREE_RATE)[1] == want_src, \
        "候选层选档用的 Δ 来源与本模块 DEX 的不一致 —— 两份规则漂移了"


# ───────────────────────────── 6. zero gamma 是重定价扫描

def _flip_chain():
    # put 集中在 95、call 分散在 100/105：逐行权价看 95 负、100 正 ⇒ 老算法报 flip=100（贴着现价）；
    # 重定价后总量的过零点在别处。
    return [_k("P", 95, 1000, 0.30), _k("C", 100, 1500, 0.30), _k("C", 105, 1500, 0.30)]


def test_zero_gamma_is_repricing_root_not_per_strike_sign_change():
    """过零点 = 重定价总量的根（独立二分求出），且与 `DealerGEXAnalyzer._find_gex_flip` 在同一数据上不同。

    变红的变异：把扫描换成「相邻行权价 net_gex 变号」（那样会给出 100，与老算法同一个答案）。
    """
    S = 100.0
    chain = _flip_chain()
    root = _bisect_root(lambda p: _total_gex(p, chain), 85.0, 100.0)

    zg = L.zero_gamma_sweep(chain, S)
    assert zg["curve_state"] == "crosses"
    assert zg["nearest_below"] == pytest.approx(root, abs=0.02)
    assert zg["zg_below_pct"] == pytest.approx((S - root) / S * 100, abs=0.02)
    assert zg["sign_at_spot"] == "positive"
    assert zg["total_at_spot"] == pytest.approx(_total_gex(S, chain), rel=1e-9)

    rows = L.strike_profile(chain, S)["rows"]
    old = DealerGEXAnalyzer()._find_gex_flip(
        [{"strike": x["strike"], "net_gex": x["net_gex_usd_per_1pct"]} for x in rows], S)
    assert old == 100.0, "夹具自检：老算法在这条链上报的是贴着现价的 100"
    assert abs(old - root) > 3.0, "夹具自检：两种定义必须分得开，否则本测试没有牙"
    assert all(abs(x - old) > 1.0 for x in zg["crossings"])


def test_sweep_units_are_s_squared_at_a_spot_other_than_100():
    """扫描曲线的单位是 p²·0.01（USD per 1% move）：行权价 ×2.5、S=250 时，`total_at_spot` 等于
    独立算的 Σ sign·γ·OI·100·S²·0.01；且等于 S=100 那条链的 2.5 倍（γ 缩 1/k、p² 放 k² ⇒ 总量 ×k）。

    上一条只在 S=100 核对 total_at_spot —— 那一点 p²·0.01 与 p 恰好相等，单位写错也看不见。
    变红的变异：扫描里把 `P * P * 0.01` 写成 `P`（线性 p）⇒ S=250 时差 2.5 倍、比值变 1.0。
    """
    k = 2.5
    base = _flip_chain()
    scaled = [dict(c, strike=c["strike"] * k) for c in base]
    zg100 = L.zero_gamma_sweep(base, 100.0)
    zg250 = L.zero_gamma_sweep(scaled, 100.0 * k)
    assert zg250["total_at_spot"] == pytest.approx(_total_gex(100.0 * k, scaled), rel=1e-9)
    assert zg250["total_at_spot"] / zg100["total_at_spot"] == pytest.approx(k, rel=1e-9)
    # 过零点随价格尺度等比缩放（γ·p² 对 (p, K) 同乘 k 是一次齐次）
    assert zg250["nearest_below"] == pytest.approx(k * zg100["nearest_below"], rel=1e-6)


@pytest.mark.parametrize("chain_fn", [
    _flip_chain,
    lambda: [_k("P", 95, 1000, 0.45), _k("C", 105, 1000, 0.25)],     # 第 7 条的 skew 链
], ids=["flip_chain", "skew_chain"])
def test_sweep_ignores_cboe_gamma_even_when_present(chain_fn):
    """扫描**只**用逐合约 IV 重定价，不读 CBOE gamma：同一条链填上 CBOE gamma（现价处的 BS 值，
    正是 CBOE 会报的数；外加一张离谱的 0.5）后，crossings / nearest_below / total_at_spot /
    curve_state 与 gamma=None 版**逐位相同**。

    上面几条的夹具全是 gamma=None，所以「有 CBOE gamma 就直接用」的回归结构上看不见；而真实
    CBOE 链上流动合约几乎都有 gamma（变异检验内存演示：填上后 flip 链从 crosses 塌成 all_positive）。
    变红的变异：扫描里有 CBOE gamma 就用它（不重定价）——把 `_sweep_arrays` 的 gamma 取数
    「统一」成 `strike_profile` 的写法就会写出这种代码。
    """
    S = 100.0
    bare = chain_fn()
    filled = [dict(c, gamma=_gamma(S, c["strike"], c["dte"] / 365.0, R, c["iv"])) for c in bare]
    filled[0]["gamma"] = 0.5
    a, b = L.zero_gamma_sweep(bare, S), L.zero_gamma_sweep(filled, S)
    assert all(c["gamma"] is None for c in bare) and all(c["gamma"] for c in filled), "夹具自检"
    for key in ("curve_state", "sign_at_spot", "crossings", "nearest", "nearest_below",
                "nearest_above", "total_at_spot", "n_contracts"):
        assert b[key] == a[key], f"{key}: 填了 CBOE gamma 后 {b[key]!r} ≠ {a[key]!r}"
    # profile 那边**应当**用 CBOE gamma（它只要现价这一点）—— 证明夹具的 gamma 确实被读到过
    assert L.strike_profile(filled, S)["counts"]["gamma_source"]["cboe"] == len(filled)


# ───────────────────────────── 7. 固定逐合约 IV

def test_zero_gamma_uses_each_contracts_own_iv_under_skew():
    """有 skew（put IV 高、call IV 低）时，用单一 ATM IV 会把过零点挪开；实现必须等于逐合约 IV 的根。

    变红的变异：扫描里所有合约共用一个 IV（ATM / 中位数 / 均值）。
    """
    S = 100.0
    chain = [_k("P", 95, 1000, 0.45), _k("C", 105, 1000, 0.25)]
    flat = [dict(c, iv=0.35) for c in chain]
    root_skew = _bisect_root(lambda p: _total_gex(p, chain), 90.0, 115.0)
    root_flat = _bisect_root(lambda p: _total_gex(p, flat), 90.0, 115.0)
    assert abs(root_skew - root_flat) > 1.0, "夹具自检：skew 必须真的挪动根"
    zg = L.zero_gamma_sweep(chain, S)
    assert zg["nearest"] == pytest.approx(root_skew, abs=0.02)


# ───────────────────────────── 8. 全段同号是显式状态

def test_all_positive_and_all_negative_are_explicit_states():
    """只有 call ⇒ all_positive / sign positive；只有 put ⇒ all_negative / sign negative。
    这是最强的信号，不是 unavailable。只有「没有可用合约」才是 insufficient_contracts。

    变红的变异：无过零点时返回 insufficient_contracts / sign_at_spot=None；
    或把全段为负判成 all_positive。
    """
    pos = L.zero_gamma_sweep([_k("C", 100, 1000, 0.3)], 100.0)
    neg = L.zero_gamma_sweep([_k("P", 100, 1000, 0.3)], 100.0)
    assert (pos["curve_state"], pos["sign_at_spot"]) == ("all_positive", "positive")
    assert (neg["curve_state"], neg["sign_at_spot"]) == ("all_negative", "negative")
    for zg in (pos, neg):
        assert zg["crossings"] == [] and zg["nearest"] is None
        assert zg["zg_below_pct"] is None and zg["zg_above_pct"] is None
    none = L.zero_gamma_sweep([_k("C", 100, 1000, None)], 100.0)
    assert (none["curve_state"], none["sign_at_spot"]) == ("insufficient_contracts", None)
    assert none["excluded_no_iv"] == 1
    assert L.zero_gamma_sweep([], 100.0)["curve_state"] == "insufficient_contracts"


def test_underflowed_tail_is_not_a_zero_crossing():
    """近月低 IV：γ 在网格边缘下溢成**精确的 0.0**。那不是过零点——曲线仍然全段为正。

    （本条是对 SPEC「恰为 0 的点算过零点」的偏离，理由见 `sell_strike_levels._crossings`。）
    变红的变异：照字面把所有精确零点都算过零点 ⇒ 状态变 crosses、nearest_below 落到 0.8S 附近。
    """
    c = _k("C", 100, 1000, 0.08, dte=1)
    T = 1 / 365
    assert _gamma(80.0, 100.0, T, R, 0.08) == 0.0, "夹具自检：网格下沿确实下溢成 0.0"
    zg = L.zero_gamma_sweep([c], 100.0)
    assert zg["curve_state"] == "all_positive"
    assert zg["crossings"] == [] and zg["nearest_below"] is None


# ───────────────────────────── 8b. 多个过零点：每侧取离现价最近的那个

def _multi_root_chain():
    """交替的 gamma 带（IV 8%、30DTE ⇒ 每条带宽约 S·σ·√T ≈ 2.3）：
    现价下方 call 99–102 / put 96–97 / call 91–93 / put 85–88，上方 put 104–105 / call 108–110。
    现价处为正；下方 3 个过零点（≈88.3 / 94.2 / 98.0）、上方 2 个（≈102.2 / 105.8）。
    共 20 张合约 = MIN_SWEEP_CONTRACTS，恰好够路由门槛。"""
    spec = [("C", (99, 100, 101, 102), 1000), ("P", (96, 96.5, 97), 1200),
            ("C", (91, 92, 93), 1500), ("P", (85, 86, 87, 88), 600),
            ("P", (104, 104.5, 105), 1200), ("C", (108, 109, 110), 1500)]
    return [_k(cp, K, oi, 0.08) for cp, strikes, oi in spec for K in strikes]


def _all_roots(f, lo, hi, step=0.05):
    """独立根：按 step 找异号区间、逐个二分（不经被测模块的 81 点网格与线性插值）。"""
    xs = [lo + step * i for i in range(int(round((hi - lo) / step)) + 1)]
    return [_bisect_root(f, a, b) for a, b in zip(xs, xs[1:]) if f(a) * f(b) < 0]


def test_nearest_crossing_on_each_side_is_the_closest_not_the_outermost():
    """现价两侧各有 ≥2 个过零点时：nearest_below = 下方**最大**的根、nearest_above = 上方**最小**的根，
    zg_below_pct / zg_above_pct 按它们算；crossings 与独立二分逐个对上。

    此前所有夹具每侧至多一个过零点 ⇒ 下面两个变异在全部测试上都是绿的。
    变红的变异：nearest_below 取 min(below)（报 ≈88.3 / 11.7%，而不是 ≈98.0 / 2.0%）；
    nearest_above 取 max(above)（≈105.8 而不是 ≈102.2）。
    """
    S = 100.0
    chain = _multi_root_chain()
    roots = _all_roots(lambda p: _total_gex(p, chain), 80.0, 120.0)
    below = [x for x in roots if x < S]
    above = [x for x in roots if x > S]
    assert len(below) >= 2 and len(above) >= 2, f"夹具自检：两侧都要 ≥2 个过零点，实为 {roots}"
    assert max(below) - min(below) > 5.0 and max(above) - min(above) > 3.0, "夹具自检：根要分得开"

    zg = L.zero_gamma_sweep(chain, S)
    assert (zg["curve_state"], zg["sign_at_spot"]) == ("crosses", "positive")
    assert zg["crossings"] == pytest.approx(roots, abs=0.02)
    assert zg["nearest_below"] == pytest.approx(max(below), abs=0.02)
    assert zg["nearest_above"] == pytest.approx(min(above), abs=0.02)
    assert zg["zg_below_pct"] == pytest.approx((S - max(below)) / S * 100, abs=0.02)
    assert zg["zg_above_pct"] == pytest.approx((min(above) - S) / S * 100, abs=0.02)
    assert zg["nearest"] == pytest.approx(min(roots, key=lambda x: abs(x - S)), abs=0.02)


def test_route_put_flag_follows_the_nearest_crossing_below():
    """路由层：同一条多根链走真实 level_map 的路由视图 ⇒ 下方最近零点 ≈98.0（2.0% ≤ 3%）⇒ put far、
    flag_put=True。下方其余的根都在 3% 外——扫描若报任何一个非最近的根，flag 就翻成 False
    （下面把 zg_below_pct 换成那些根的距离逐个演示）。

    变红的变异：`zero_gamma_sweep` 的 nearest_below 取 min(below) ⇒ zg_below_pct≈11.7 ⇒ put base / flag False。
    """
    import sell_strike_candidates as SC
    S = 100.0
    chain = _multi_root_chain()
    zg = L.level_map(chain, S)["views"][SC.ROUTE_VIEW]["zero_gamma"]
    assert zg["n_contracts"] >= SC.MIN_SWEEP_CONTRACTS, "夹具自检：扫描合约数够路由门槛"
    below = [x for x in _all_roots(lambda p: _total_gex(p, chain), 80.0, 120.0) if x < S]
    nearest, others = max(below), sorted(below)[:-1]
    assert (S - nearest) / S * 100 <= SC.FLIP_BUFFER_PCT, "夹具自检：最近的根在缓冲内"
    assert others and all((S - x) / S * 100 > SC.FLIP_BUFFER_PCT for x in others), \
        "夹具自检：其余的根都在缓冲外（否则取错根也判 far，本测试没有牙）"

    r = SC.route(zg)
    assert (r["put"], r["flag_put"], r["call"], r["flag_call"]) == ("far", True, "base", False)
    for x in others:
        alt = SC.route(dict(zg, zg_below_pct=(S - x) / S * 100))
        assert (alt["put"], alt["flag_put"]) == ("base", False)


# ───────────────────────────── 9. majors 四值独立

def test_majors_are_net_extremes_independent_of_side_extremes():
    """净 GEX 极值与单边极值各自独立：100 行权价 call 最大但被同价 put 抵消，净值只有 1。

    变红的变异：net_major_* 取单边极值（旧 wall 口径）；side_extreme 取净值。
    """
    rows = [
        {"strike": 90.0, "call_gex_usd_per_1pct": 0.0, "put_gex_usd_per_1pct": -3.0, "net_gex_usd_per_1pct": -3.0},
        {"strike": 100.0, "call_gex_usd_per_1pct": 10.0, "put_gex_usd_per_1pct": -9.0, "net_gex_usd_per_1pct": 1.0},
        {"strike": 110.0, "call_gex_usd_per_1pct": 5.0, "put_gex_usd_per_1pct": 0.0, "net_gex_usd_per_1pct": 5.0},
    ]
    m = L.majors(rows)
    assert (m["net_major_pos_strike"], m["net_major_pos_gex"]) == (110.0, 5.0)
    assert (m["net_major_neg_strike"], m["net_major_neg_gex"]) == (90.0, -3.0)
    assert (m["call_side_extreme_strike"], m["call_side_extreme_gex"]) == (100.0, 10.0)
    assert (m["put_side_extreme_strike"], m["put_side_extreme_gex"]) == (100.0, -9.0)
    # AMC 形状：没有一个净值为负的行权价 ⇒ net_major_neg 为 None，put 单边极值照样有
    amc = L.majors([dict(rows[1]), dict(rows[2])])
    assert amc["net_major_neg_strike"] is None and amc["put_side_extreme_strike"] == 100.0


# ───────────────────────────── 10. 逐合约聚合

def test_profile_aggregates_each_contract_with_its_own_t_and_iv():
    """同一行权价、两个到期日（T 不同）、call/put IV 不同：净 GEX / vanna / charm 必须是逐合约求和。

    变红的变异：每个行权价只算一个 γ（取第一张合约的 T/IV），再乘 (OI_call − OI_put)。
    """
    S = 100.0
    # call 的 OI 压在远月低 IV（γ 小）、put 的 OI 压在近月高 IV（γ 大）：
    # 净 OI 为正，但逐合约净 γ 几乎抵消 —— 两种算法差 5 倍以上。
    chain = [_k("C", 100, 1400, 0.20, dte=60, expiry="2026-11-22"),
             _k("C", 100, 100, 0.25, dte=5, expiry="2026-09-28"),
             _k("P", 100, 1000, 0.45, dte=5, expiry="2026-09-28"),
             _k("P", 100, 100, 0.35, dte=60, expiry="2026-11-22")]
    want_gex = want_vanna = want_charm = 0.0
    for c in chain:
        T = c["dte"] / 365
        sign = 1.0 if c["cp"] == "C" else -1.0
        want_gex += sign * _gamma(S, 100.0, T, R, c["iv"]) * c["oi"] * 100 * S * S * 0.01
        h = 1e-5
        vanna = (_delta(S, 100.0, T, R, c["iv"] + h, c["cp"]) - _delta(S, 100.0, T, R, c["iv"] - h, c["cp"])) / (2 * h)
        want_vanna += sign * vanna * c["oi"] * 100 * S * 0.01
        eps = 1e-7
        charm = (_delta(S, 100.0, T - eps, R, c["iv"], c["cp"]) - _delta(S, 100.0, T, R, c["iv"], c["cp"])) / eps
        want_charm += sign * charm * c["oi"] * 100 * S / 365
    row = L.strike_profile(chain, S)["rows"][0]
    assert row["net_gex_usd_per_1pct"] == pytest.approx(want_gex, rel=1e-9)
    assert row["net_vanna_usd_per_volpt"] == pytest.approx(want_vanna, rel=1e-4)
    assert row["net_charm_usd_per_day"] == pytest.approx(want_charm, rel=1e-3)
    lumped = _gamma(S, 100.0, 60 / 365, R, 0.20) * (1500 - 1100) * 100 * S * S * 0.01
    assert abs(lumped - want_gex) / abs(want_gex) > 2.0, "夹具自检：单 γ×净 OI 必须明显不同"


# ───────────────────────────── 11. 非法输入返回 None

@pytest.mark.parametrize("args", [
    (0.0, 100.0, 0.1, R, 0.3), (100.0, 0.0, 0.1, R, 0.3), (100.0, 100.0, 0.0, R, 0.3),
    (100.0, 100.0, 0.1, R, 0.0), (100.0, 100.0, 0.1, R, -0.2), (float("nan"), 100.0, 0.1, R, 0.3),
    (100.0, float("inf"), 0.1, R, 0.3), (100.0, 100.0, 0.1, float("nan"), 0.3), (None, 100.0, 0.1, R, 0.3),
])
def test_invalid_inputs_return_none_not_zero(args):
    """S/K/T/σ ≤ 0 或非有限 ⇒ None。0.0 是合法的 greek 值，拿它当「算不出」会被下游当观测值求和。

    变红的变异：照 advanced_analyzer.bs_gamma 的写法返回 0.0。
    """
    S, K, T, r, s = args
    assert L.bs_d1_d2(S, K, T, r, s) is None
    assert L.bs_gamma(S, K, T, r, s) is None
    assert L.bs_vanna(S, K, T, r, s) is None
    for cp in ("C", "P"):
        assert L.bs_delta(S, K, T, r, s, cp) is None
        assert L.bs_charm(S, K, T, r, s, cp) is None
        assert L.itm_probability(S, K, T, r, s, cp) is None


def test_invalid_scalar_edge_cases_return_none():
    """cp 非法、sigma_distance / expected_move 的非法输入同样是 None。

    变红的变异：cp 非 C/P 时按 put 算；expected_move 在 σ≤0 时返回 0.0。
    """
    assert L.bs_delta(100, 100, 0.1, R, 0.3, "X") is None
    assert L.itm_probability(100, 100, 0.1, R, 0.3, None) is None
    assert L.sigma_distance(100, 100, 0.1, 0.0) is None
    assert L.sigma_distance(-1, 100, 0.1, 0.3) is None
    assert L.expected_move_1sigma(100, 0.0, 30) is None
    assert L.expected_move_1sigma(100, 0.3, None) is None
    assert L.year_fraction(float("nan")) is None


# ───────────────────────────── 视图与形状

def test_term_views_split_and_empty_view_does_not_raise():
    """next_expiry = 最小 dte 的到期日；le_45dte = dte≤45；full = 全部（含坏行，由计数器数到）。

    变红的变异：next_expiry 取最大 dte；le_45dte 用 < 45；full 先滤掉坏行导致 excluded_bad_contract 恒 0；
    空视图抛异常。
    """
    chain = [_k("C", 100, 100, 0.3, dte=7, expiry="2026-09-30"),
             _k("P", 95, 100, 0.3, dte=45, expiry="2026-11-07"),
             _k("C", 110, 100, 0.3, dte=90, expiry="2026-12-22"),
             {"expiry": "2026-10-01", "cp": "C", "strike": 100.0, "dte": None, "iv": 0.3, "oi": 5.0}]
    v = L.term_views(chain, 100.0)
    assert v["next_expiry"]["expiries"] == ["2026-09-30"]
    assert v["le_45dte"]["expiries"] == ["2026-09-30", "2026-11-07"]
    assert v["full"]["n_contracts"] == 4
    assert v["full"]["profile"]["counts"]["excluded_bad_contract"] == 1
    assert v["full"]["zero_gamma"]["excluded_bad_contract"] == 1
    empty = L.term_views([], 100.0)
    for name in ("next_expiry", "le_45dte", "full"):
        assert empty[name]["n_contracts"] == 0
        assert empty[name]["zero_gamma"]["curve_state"] == "insufficient_contracts"
    lm = L.level_map(chain, 100.0)
    assert lm["schema_version"] == L.LEVELS_SCHEMA_VERSION == 1
    assert lm["sign_convention"] == "naive_oi_dealer_long_calls_short_puts"
    assert set(lm["views"]) == {"next_expiry", "le_45dte", "full"}


def test_next_expiry_skips_rows_without_a_usable_expiry():
    """dte 最小的行缺 expiry 键 ⇒ 曾在 `min(...)["expiry"]` 抛 KeyError；expiry=None ⇒ next_exp=None，
    整个 next_expiry 视图变成「没有到期日的那几行」。现在：缺键 / None / 空串的行都不参选、不进 next_expiry，
    计 `excluded_no_expiry`；照样进 le_45dte / full（按 dte 截，用不到到期日）。

    变红的变异：在全部 dte 合法的行里选 next_expiry（原写法）⇒ 缺键那组抛 KeyError、None 那组
    expiries==[]；不计数（键缺失或恒 0）；顺手把这些行也从 le_45dte 滤掉。
    """
    good = [_k("C", 100, 100, 0.3, dte=7, expiry="2026-09-30"),
            _k("P", 95, 100, 0.3, dte=7, expiry="2026-09-30"),
            _k("C", 105, 100, 0.3, dte=14, expiry="2026-10-07")]
    no_key = {k: v for k, v in _k("C", 101, 100, 0.3, dte=2).items() if k != "expiry"}
    none_exp = _k("P", 99, 100, 0.3, dte=3, expiry=None)
    blank_exp = _k("P", 98, 100, 0.3, dte=1, expiry="")
    for bad in ([no_key], [none_exp], [no_key, none_exp, blank_exp]):
        v = L.term_views(bad + good, 100.0)
        nx = v["next_expiry"]
        assert nx["expiries"] == ["2026-09-30"] and nx["n_contracts"] == 2, nx["expiries"]
        assert nx["excluded_no_expiry"] == len(bad)
        for name in ("le_45dte", "full"):
            assert v[name]["n_contracts"] == len(bad) + 3
            assert v[name]["profile"]["counts"]["n_contracts_used"] == len(bad) + 3
    only_bad = L.term_views([no_key, none_exp], 100.0)["next_expiry"]
    assert only_bad["n_contracts"] == 0 and only_bad["excluded_no_expiry"] == 2
    assert only_bad["zero_gamma"]["curve_state"] == "insufficient_contracts"
    assert L.term_views(good, 100.0)["next_expiry"]["excluded_no_expiry"] == 0


def test_frozen_constants():
    """与 advanced_analyzer 同值、独立常量；改了就要同步预注册文档。

    变红的变异：改 T_FLOOR_DAYS / RISK_FREE_RATE / DAYS_PER_YEAR 任一。
    """
    assert (L.RISK_FREE_RATE, L.T_FLOOR_DAYS, L.DAYS_PER_YEAR) == (0.045, 0.5, 365.0)
    assert L.RISK_FREE_RATE == DealerGEXAnalyzer.RISK_FREE_RATE
