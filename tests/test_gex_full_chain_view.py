"""Dealer GEX 专用全链视图的守卫（v0.45.197）。

**背景。** GEX 此前与 IV / skew / 期限结构共用 `fetch_cboe_chain` 的主链，而那条链的
过滤器是为后者设计的（近月 theta 扭曲，剔掉是对的）；GEX 的需求恰好相反 ——
gamma ∝ 1/(S·σ·√T)，峰值就在近月。

⚠️ **但「加上近月」并不够，这是实测推翻的一个前提。** 净 GEX 是带符号求和，任何对
到期日集合的截断都可能翻转符号。26 只标的实测「取最近 K 个到期日捕获的 net GEX 占
全链百分比」：K=4 → 中位 63.9%、**最低 −73.3%**；K=8 → 73.6% / −29.8%；
K=12 → 96.3% / 20.6%；K=16 → 100% / 72.3%；K=24 → 100% / 99.4%。
负百分比 = 部分和与全链**符号相反** ⇒ `total_gex` 只有在全链上才是良定义的量。

**纪律。** 每条断言的 docstring 写明什么变异会让它变红。
本文件零外部依赖（夹具全是内存 dict），结构上没有写 `skip` 的余地。
"""

from datetime import datetime

import pytest

import cboe_options as C
from advanced_analyzer import DealerGEXAnalyzer

FRIDAY_1330 = datetime(2026, 9, 11, 13, 30)
# 含：已到期(−1)、当天(0)、3 日历日、7 日历日、以及一串远月
EXPIRIES = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-18",
            "2026-09-21", "2026-09-25", "2026-10-02", "2026-10-16"]


def _by_expiry(expiries=EXPIRIES, oi=100.0):
    return {e: {"C": [{"strike": 100.0, "openInterest": oi}],
                "P": [{"strike": 100.0, "openInterest": oi}]}
            for e in expiries}


@pytest.fixture(autouse=True)
def _reset():
    C.reset_gex_view_stats()
    yield
    C.reset_gex_view_stats()


# ───────────────────────────── 选择器

def test_gex_view_keeps_the_seven_calendar_day_expiry():
    """主链丢掉的那个 7 日历日到期日，GEX 视图必须留着——这是本次改动的直接目的。

    变红的变异：把 `_select_expiries_for_gex` 换成 `_select_expiries`，
    或给它加回 `dte >= 7` / `dte >= 3` 之类的下限。
    """
    gex_chosen, _ = C._select_expiries_for_gex(_by_expiry(), FRIDAY_1330)
    main_chosen, _ = C._select_expiries(_by_expiry(), FRIDAY_1330, 4)
    assert "2026-09-18" in gex_chosen, "7 日历日之外的到期日必须在 GEX 视图里"
    assert "2026-09-18" not in main_chosen, "夹具自检：主链确实丢了它（丢了才有对比意义）"
    assert "2026-09-14" in gex_chosen, "3 日历日的也要（gamma 峰值在这里）"


def test_gex_view_uses_calendar_dte_not_the_buggy_one():
    """GEX 选择器用**日历**口径，不复制主链那条差一天。

    变红的变异：把 `.date() - today.date()` 改回 `- today`
    —— 那样 2026-09-11（当天，日历 DTE 0）会被算成 −1 而被 `dte >= 0` 排除。
    """
    chosen, _ = C._select_expiries_for_gex(_by_expiry(["2026-09-11"]), FRIDAY_1330)
    assert chosen == ["2026-09-11"], "当天到期（日历 DTE 0）不该被判成 −1 而落选"


def test_gex_view_excludes_already_expired():
    """已到期（日历 DTE<0）的必须排除——它们的 OI 是历史残留，算进去是纯噪声。

    变红的变异：把 `if dte >= 0` 改成 `if True` 或去掉该判断。
    """
    chosen, _ = C._select_expiries_for_gex(_by_expiry(), FRIDAY_1330)
    assert "2026-09-10" not in chosen


def test_gex_view_returns_empty_near_set():
    """GEX 视图里没有「被排除的近月」这个概念 ⇒ `near_expiry_set` 必须为空。

    非空会让下游 `_calc_total_oi` 去排除本来就该算进来的合约。

    变红的变异：让它返回 `_select_expiries` 那样的 `near_set`。
    """
    _, near = C._select_expiries_for_gex(_by_expiry(), FRIDAY_1330)
    assert near == []


def test_gex_view_cap_is_counted_not_silent():
    """超出上限被砍掉的到期日数要计数——否则「24 够不够」永远是个假设。

    变红的变异：删掉 `_gex_view_stats["capped_expiries"] += dropped`。
    """
    many = [f"2026-{m:02d}-{d:02d}" for m in (10, 11, 12) for d in (2, 9, 16, 23, 30)]
    chosen, _ = C._select_expiries_for_gex(_by_expiry(many), FRIDAY_1330, max_expiries=4)
    assert len(chosen) == 4
    assert C.gex_view_stats()["capped_expiries"] == len(many) - 4


def _occ(root, expiry, cp, strike):
    y, m, d = expiry.split("-")
    return f"{root}{y[2:]}{m}{d}{cp}{int(round(strike * 1000)):08d}"


def _payload(expiries=EXPIRIES, root="NVDA", price=100.0):
    """合成一份 CBOE payload（真 OCC 符号），够 `fetch_cboe_chain` 走完整条路。"""
    opts = []
    for e in expiries:
        for cp in ("C", "P"):
            for k in (95.0, 100.0, 105.0):
                opts.append({"option": _occ(root, e, cp, k), "open_interest": 100,
                             "iv": 0.3, "gamma": 0.05, "bid": 1.0, "ask": 1.1,
                             "volume": 10, "last_trade_price": 1.05})
    return {"options": opts, "current_price": price, "close": price}


def test_default_selector_unchanged_by_the_bypass(monkeypatch):
    """不传 `expiry_selector` 时，`fetch_cboe_chain` 选出的到期日必须与
    `_select_expiries` 直接算出来的**完全一致**。守的是「加旁路别顺手改默认行为」。

    ⚠️ 刻意**不用** `inspect.getsource` 去匹配源码文案 —— 本仓 MEMORY
    （`alpha-hive-prediction-retention`）记过：守卫标志物别用源码文案，
    改个变量名就绕过去了。这里比的是真实返回值。

    变红的变异：把 `_select = expiry_selector or _select_expiries` 改成
    `... or _select_expiries_for_gex`；或把 `max_expiries` 默认值改掉。
    """
    monkeypatch.setattr(C, "_fetch_cboe_payload", lambda t, to, **k: _payload())
    monkeypatch.setattr(C, "_pdt_now", lambda: FRIDAY_1330)

    default = C.fetch_cboe_chain("NVDA", 100.0)
    expected, _ = C._select_expiries(_by_expiry(), FRIDAY_1330, 4)
    assert default is not None
    assert default["expirations"] == expected

    gex = C.fetch_cboe_chain_for_gex("NVDA", 100.0)
    gex_expected, _ = C._select_expiries_for_gex(_by_expiry(), FRIDAY_1330)
    assert gex is not None
    assert gex["expirations"] == gex_expected
    assert set(default["expirations"]) < set(gex["expirations"]), (
        "GEX 视图必须是主链的真超集 —— 否则这次改动没解决问题")


# ───────────────────────────── 不回退

def test_analyze_does_not_fall_back_to_truncated_chain(monkeypatch):
    """CBOE 全链视图取不到时，`analyze` 必须返回不可得，**不能**去拿截断链顶上。

    回退等于把「没数据」悄悄换成「错数据」（v0.45.188 `_calc_max_pain` 的同一判据）。

    变红的变异：在 `analyze` 里给 `if not chain:` 加一条回退到
    `OptionsDataFetcher().fetch_options_chain(ticker)` 的分支。
    """
    an = DealerGEXAnalyzer()
    called = []

    def _boom(*a, **k):
        called.append("fallback")
        raise AssertionError("不该走到 options_analyzer 的降级链")

    monkeypatch.setattr("options_analyzer.OptionsDataFetcher.fetch_options_chain", _boom)
    monkeypatch.setattr(an, "_fetch_chain", lambda *a, **k: None)

    out = an.analyze("NVDA", 220.0)
    assert out.get("error"), "取不到必须显式报错，不能返回一个看起来正常的 0"
    assert out["total_gex"] == 0.0
    # 这条才是有牙的那个：加了回退分支 ⇒ `_boom` 触发 ⇒ 变红。
    # 不去断言 error 文案里有没有「不回退」三个字（改一句话就绕过去了）。
    assert not called


def test_unavailable_is_counted(monkeypatch):
    """视图不可得的次数要可数——这是本次改动**唯一的代价**，不能靠估。

    变红的变异：删掉 `fetch_cboe_chain_for_gex` 里的
    `_gex_view_stats[... ] += 1`，或把它改成只在成功时加。
    """
    monkeypatch.setattr(C, "fetch_cboe_chain", lambda *a, **k: None)
    assert C.fetch_cboe_chain_for_gex("NVDA") is None
    assert C.gex_view_stats() == {"ok": 0, "unavailable": 1, "capped_expiries": 0}

    monkeypatch.setattr(C, "fetch_cboe_chain", lambda *a, **k: {"calls": [], "puts": []})
    C.fetch_cboe_chain_for_gex("NVDA")
    assert C.gex_view_stats()["ok"] == 1


def test_analyze_records_which_expiries_it_used(monkeypatch):
    """结果里必须带口径字段——只记数值时，事后分不清「到期日集合变了」和「仓位真变了」。

    v0.45.188 的 NVDA 225→200 误判正是卡在这里。

    变红的变异：删掉 `analyze` 返回值里的 `expiries_used` / `chain_view`。
    """
    an = DealerGEXAnalyzer()
    chain = {
        "calls": [{"strike": 100.0, "openInterest": 10, "gamma": 0.05,
                   "impliedVolatility": 0.3, "dte": 7, "expiry": "2026-09-18"}],
        "puts": [{"strike": 100.0, "openInterest": 5, "gamma": 0.05,
                  "impliedVolatility": 0.3, "dte": 7, "expiry": "2026-09-18"}],
        "expirations": ["2026-09-18", "2026-09-25"],
    }
    monkeypatch.setattr(an, "_fetch_chain", lambda *a, **k: chain)
    out = an.analyze("NVDA", 100.0)
    assert not out.get("error"), out
    assert out["chain_view"] == "cboe_full_expiries"
    assert out["expiries_used"] == ["2026-09-18", "2026-09-25"]
