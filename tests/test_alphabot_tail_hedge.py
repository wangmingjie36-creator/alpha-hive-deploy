"""Alpha Bot 对冲页「尾部对冲候选」（v0.45.643）：`alphabot/tail_hedge.py` + `AlphaBotService.tail_hedge` + `/api/hedge/tail`。

每组回答一句「谁会红」：

  · 数学手算（`TestPayoff` / `TestCost` / `TestUnitCells` / `TestGreeks`）：到期盈亏的最大亏损点与盈亏平衡点是精确解、
    成交价卖 bid 买 ask、Greeks 单位——数字用整数手算，不拿被测函数的输出当期望；
  · 基线与规模（`TestBaseline` / `TestSizing`）：审计当天已成交的 SPY 要折进压力格（否则基线与「交易后」Greeks 口径不一）、
    规模只对**下行侧**最差格定（上行侧最差格认沽管不到——实测 2026-10-23 就是这样，对错的格放大曾把 −21% 放大到 −128%）、
    两道上限、`offset_achieved` 永远给；
  · 诚实（`TestHonesty`）：没有推荐 / 综合分 / 胜出字段，候选顺序是固定网格、不随效果变（有牙：喂一个效果相反的基线，顺序不变）；
  · 原因码 / 旗标表双向对账（`TestTables`）；
  · 只读（`TestService`）：打接口前后数据根目录指纹不变；取链失败如实带原因返回、不吞；进程内缓存与 `force`；
  · 纯度 + 前端接线（`TestPurity` / `TestFrontend`）：模块不读写文件、不联网；前端不另抄文案、不按效果给候选排序。
全部离线。
"""
from __future__ import annotations

import ast
import json
import math
import re
from pathlib import Path

import pytest

from alphabot import synthetic as syn
from alphabot import tail_hedge as TH
from alphabot.service import AlphaBotService
from tests.test_alphabot_hedge import DIRNAME, NOW, _client, _fingerprint, _write

STATIC = Path(__file__).resolve().parent.parent / "alphabot" / "static"
MODULE = Path(TH.__file__)


def intrinsic(S, K, T, r, sigma, cp="put"):
    """没有时间价值的 BS 替身：手算盈亏时「冲击后的价」就是内在价值。"""
    return max(K - S, 0.0) if cp == "put" else max(S - K, 0.0)


DEPS = TH.Deps(bs_price=intrinsic, risk_free=0.045, max_spread_pct=0.25)


def put(strike, delta, bid, ask, *, gamma=0.01, vega=0.1, theta=-0.03, oi=10_000.0, iv=0.20, dte=42,
        expiry="2026-12-04"):
    return {"symbol": f"SPY{int(strike)}", "expiry": expiry, "cp": "P", "strike": float(strike), "dte": dte, "iv": iv,
            "delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "oi": oi, "volume": 100.0, "bid": bid, "ask": ask}


def chain(puts=None, *, S=100.0, vintage="2026-10-23", live=False):
    puts = puts if puts is not None else [
        put(95, -0.30, 3.0, 3.2), put(90, -0.20, 1.8, 2.0), put(85, -0.15, 1.0, 1.1),
        put(80, -0.10, 0.5, 0.6), put(70, -0.05, 0.1, 0.2)]
    return {"underlying_price": S, "vintage_date": vintage, "session_live": live, "contracts": puts}


def audit(cells=None, *, nav=100_000.0, spy=100.0, executed=False, trade=None, pct=None, after_pct=None, partial=False):
    cells = cells if cells is not None else [
        {"spot_pct": sp, "iv_pts": 0, "pnl": p} for sp, p in ((-30, -9000.0), (-10, -6000.0), (0, 0.0), (10, -2000.0))]
    a = {"nav": nav, "spy_price": spy,
         "aggregate": {"nav": nav, "pct_nav": pct or {"beta_dollar_delta": 5.0, "dollar_delta": 6.0, "gamma_dollar_per_1pct": -0.1,
                                                      "vega_dollar_per_pt": -0.4, "theta_dollar_per_day": 0.6}},
         "stress": {"cells": cells, "partial": partial, "excluded": ["X(option:expired 1d)"] if partial else []},
         "executed": executed, "executed_trade": trade}
    if after_pct is not None:
        a["aggregate_after"] = {"pct_nav": after_pct}
    return a


def build(*, a=None, c=None, deps=DEPS, rules=None, day="2026-10-23"):
    return TH.build(day, a if a is not None else audit(), c if c is not None else chain(), deps, rules)


def cand(res, cid):
    return next(x for x in res["candidates"] if x["id"] == cid)


def keys_of(obj):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= keys_of(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= keys_of(v)
    return out


class TestPayoff:
    """卖 1 张 95（+$3.00 bid）、买 2 张 85（−$1.10 ask）：成本 −80；分段线性，结点上取值。"""

    def legs(self):
        s = TH._leg("short", put(95, -0.30, 3.0, 3.2), -1)
        l = TH._leg("long", put(85, -0.15, 1.0, 1.1), 2)
        return [s, l]

    def test_max_loss_at_the_long_strike_and_exact_breakevens(self):
        legs = self.legs()
        cost = TH.unit_cost(legs)["executable"]
        assert cost == pytest.approx(-80.0)
        p = TH.expiry_payoff(legs, cost, 100.0)
        # 85 处：−100×(95−85) + 0 + 80 = −920；单位 100 股
        assert p["max_loss_usd"] == pytest.approx(-920.0) and p["at_spot"] == 85.0
        # 85..95：−100(95−S)+80=0 → S=94.2；85 以下：7580−100S=0 → S=75.8
        assert p["breakevens"] == pytest.approx([75.8, 94.2])
        assert p["upside_usd"] == pytest.approx(80.0)        # 收了净权利金：上行不亏
        assert p["pnl"](100.0) == pytest.approx(80.0) and p["pnl"](85.0) == pytest.approx(-920.0)

    def test_debit_variant_loses_the_debit_on_the_upside(self):
        legs = [TH._leg("short", put(95, -0.30, 1.0, 1.2), -1), TH._leg("long", put(85, -0.15, 1.0, 1.1), 2)]
        cost = TH.unit_cost(legs)["executable"]
        assert cost == pytest.approx(-100.0 + 220.0)
        p = TH.expiry_payoff(legs, cost, 100.0)
        assert p["upside_usd"] == pytest.approx(-120.0)
        assert len(p["breakevens"]) == 1                      # 一路亏到下方才翻正：只有一个盈亏平衡点


class TestCost:
    def test_sell_at_bid_buy_at_ask_and_cross_spread_is_the_gap_to_mid(self):
        legs = [TH._leg("short", put(95, -0.30, 3.0, 3.2), -1), TH._leg("long", put(85, -0.15, 1.0, 1.1), 2)]
        c = TH.unit_cost(legs)
        assert c["executable"] == pytest.approx(-100 * 3.0 + 200 * 1.1)         # −80
        assert c["mid"] == pytest.approx(-100 * 3.1 + 200 * 1.05)               # −100
        assert c["cross_spread"] == pytest.approx(20.0) and c["cross_spread"] >= 0

    @pytest.mark.parametrize("bid,ask", [(0.0, 1.0), (None, 1.0), (1.0, None), (1.2, 1.0)])
    def test_unusable_quote_makes_the_candidate_unavailable_not_zero(self, bid, ask):
        puts = chain()["contracts"]
        puts[0] = put(95, -0.30, bid, ask)
        c = cand(build(c=chain(puts)), "30x15")
        assert c["available"] is False and c["reason"] == "quote_not_ok:short"
        assert "effect" not in c and "sizing" not in c


class TestUnitCells:
    def test_cell_pnl_is_shocked_value_minus_fill_with_intrinsic_stub(self):
        legs = [TH._leg("short", put(95, -0.30, 3.0, 3.2), -1), TH._leg("long", put(85, -0.15, 1.0, 1.1), 2)]
        cost = TH.unit_cost(legs)["executable"]
        cells = TH.unit_cells(legs, 100.0, 42, DEPS, [-30, -10, 0, 10], [0], cost)
        # S1=70：−100×25 + 200×15 + 80 = 580；S1=90：−100×5 + 0 + 80 = −420（亏损谷）；S1=100：+80
        assert cells[(-30, 0)] == pytest.approx(580.0)
        assert cells[(-10, 0)] == pytest.approx(-420.0)
        assert cells[(0, 0)] == pytest.approx(80.0) and cells[(10, 0)] == pytest.approx(80.0)

    def test_iv_shift_and_time_are_passed_to_the_pricer_like_the_baseline_stress_table(self):
        seen = []

        def spy(S, K, T, r, sigma, cp):
            seen.append((round(S, 4), K, round(T, 6), r, round(sigma, 4), cp))
            return 0.0

        legs = [TH._leg("short", put(95, -0.30, 3.0, 3.2, iv=0.20), -1), TH._leg("long", put(85, -0.15, 1.0, 1.1, iv=0.25), 2)]
        TH.unit_cells(legs, 100.0, 42, TH.Deps(bs_price=spy, risk_free=0.045, max_spread_pct=0.25), [-10], [20, -30], 0.0)
        assert (90.0, 95.0, round(42 / 365.0, 6), 0.045, 0.40, "put") in seen          # 每张合约自己的 IV 平移 +20 点
        assert (90.0, 85.0, round(42 / 365.0, 6), 0.045, 0.45, "put") in seen
        floor = [x for x in seen if x[4] == TH.IV_FLOOR]
        assert len(floor) == 2 and all(x[1] in (95.0, 85.0) for x in floor)             # −30 点把 IV 压到地板，同 stress_table


class TestGreeks:
    def test_dollar_greeks_by_hand(self):
        s = TH._leg("short", put(95, -0.30, 3.0, 3.2, gamma=0.02, vega=0.15, theta=-0.05), -1)
        l = TH._leg("long", put(85, -0.14, 1.0, 1.1, gamma=0.012, vega=0.10, theta=-0.03), 2)
        g, why = TH.greeks_dollars([s, l], 100.0)
        assert why is None
        assert g["dollar_delta"] == pytest.approx(-100 * -0.30 * 100 + 200 * -0.14 * 100)     # 3000 − 2800 = +200
        assert g["gamma_dollar_per_1pct"] == pytest.approx(0.5 * -100 * 0.02 * 1.0 + 0.5 * 200 * 0.012 * 1.0)   # −1.0 + 1.2
        assert g["vega_dollar_per_pt"] == pytest.approx(-100 * 0.15 + 200 * 0.10)             # +5
        assert g["theta_dollar_per_day"] == pytest.approx(-100 * -0.05 + 200 * -0.03)         # −1
        assert g["beta_dollar_delta"] == g["dollar_delta"]                                    # SPY 的 β = 1

    @pytest.mark.parametrize("field", ["gamma", "vega", "theta"])
    def test_missing_greek_is_named_not_zeroed(self, field):
        puts = chain()["contracts"]
        puts[0] = {**put(95, -0.30, 3.0, 3.2), field: None}
        c = cand(build(c=chain(puts)), "30x15")
        assert c["available"] is True                              # 金额类读数不依赖它们
        assert c["greeks"] is None and c["greeks_reason"] == f"greeks_missing:short.{field}"
        assert "缺" in c["greeks_reason_zh"]


class TestPicking:
    def test_expiry_is_closest_to_target_inside_the_window(self):
        cs = [put(95, -0.3, 1, 1.1, dte=d, expiry=f"E{d}") for d in (10, 30, 40, 60, 120)]
        assert TH.pick_expiry(cs) == ("E40", 40)                    # 目标 45：|40−45|=5 < |60−45|=15；10 / 120 在窗外
        assert TH.pick_expiry([put(95, -0.3, 1, 1.1, dte=5, expiry="E5")]) is None
        tie = [put(95, -0.3, 1, 1.1, dte=d, expiry=f"E{d}") for d in (40, 50)]
        assert TH.pick_expiry(tie) == ("E40", 40)                   # 并列取更近的到期日

    def test_closest_is_not_close_enough(self):
        book = [put(95, -0.30, 3.0, 3.2), put(85, -0.15, 1.0, 1.1)]
        assert TH.pick_put(book, 0.09, 0.05) is None                # 最接近的 0.15 差 0.06 > 容差 ⇒ 不是「够近」
        assert TH.pick_put(book, 0.10, 0.05)["strike"] == 85.0      # 差恰好 = 容差：含边界（浮点噪声由 1e-9 吃掉）
        assert TH.pick_put(book, 0.11, 0.05)["strike"] == 85.0
        assert TH.pick_put(book, 0.30, 0.05)["strike"] == 95.0

    def test_calls_and_missing_delta_are_ignored(self):
        calls = [{**put(95, 0.30, 3.0, 3.2), "cp": "C"}]
        assert TH._put_book(calls, "2026-12-04") == []
        assert TH._put_book([{**put(95, None, 3.0, 3.2)}], "2026-12-04") == []

    def test_level_without_a_put_is_unavailable_with_its_side(self):
        # 0.05 档：最近的 0.10（差恰好 0.05）还算「够近」，所以两个都去掉，最近的 0.15 差 0.10 > 容差
        puts = [p for p in chain()["contracts"] if p["strike"] not in (70, 80)]
        c = cand(build(c=chain(puts)), "30x05")
        assert c["available"] is False and c["reason"] == "no_put_for_level:long"

    def test_strikes_that_collide_are_rejected(self):
        # 同一行权价同时是 0.30 档与 0.15 档的最近合约
        puts = [put(95, -0.22, 3.0, 3.2)]
        res = build(c=chain(puts), rules={**TH.RULES, "delta_tolerance": 0.10})
        c = cand(res, "30x15")
        assert c["available"] is False and c["reason"] == "strikes_not_ordered"


class TestBaseline:
    def test_cells_worst_and_downside_worst(self):
        b, why = TH.baseline_from_audit("2026-10-23", audit())
        assert why is None
        assert b["worst_cell"]["pnl"] == -9000.0 and b["downside_worst_cell"]["spot_pct"] == -30
        assert b["spots"] == [-30, -10, 0, 10] and b["ivs"] == [0]

    def test_worst_on_the_upside_is_kept_separate_from_the_downside_one(self):
        cells = [{"spot_pct": sp, "iv_pts": 0, "pnl": p} for sp, p in ((-10, -1000.0), (0, 0.0), (10, -5000.0))]
        b, _ = TH.baseline_from_audit("d", audit(cells))
        assert b["worst_cell"]["spot_pct"] == 10 and b["downside_worst_cell"]["spot_pct"] == -10
        res = build(a=audit(cells))
        assert "worst_cell_uncovered" in res["flags"]

    def test_executed_spy_trade_is_folded_into_the_cells_and_greeks_use_after(self):
        a = audit(executed=True, trade={"shares": -4, "price": 100.0}, pct={"beta_dollar_delta": 9.0},
                  after_pct={"beta_dollar_delta": 1.0})
        b, _ = TH.baseline_from_audit("d", a)
        by = {c["spot_pct"]: c["pnl"] for c in b["cells"]}
        # 卖出 4 股 SPY @100：−4×100×sp/100 加到每格——跌 30% 赚 +120，涨 10% 亏 −40
        assert by[-30] == pytest.approx(-9000.0 + 120.0) and by[10] == pytest.approx(-2000.0 - 40.0)
        assert by[0] == 0.0
        assert b["adjusted_for_trade"] is True and b["pct_nav"]["beta_dollar_delta"] == 1.0

    def test_no_trade_no_adjustment(self):
        b, _ = TH.baseline_from_audit("d", audit(after_pct={"beta_dollar_delta": 1.0}))
        assert b["adjusted_for_trade"] is False and b["pct_nav"]["beta_dollar_delta"] == 5.0

    @pytest.mark.parametrize("make,code", [
        (lambda: (None, None), "audit_missing"),
        (lambda: ("d", {"nav": 1.0}), "stress_missing"),
        (lambda: ("d", audit(cells=[{"spot_pct": -10, "iv_pts": 0, "pnl": float("nan")}])), "stress_unreadable"),
        (lambda: ("d", audit(cells=[{"spot_pct": -10, "iv_pts": 0}])), "stress_unreadable"),
        (lambda: ("d", {**audit(), "nav": None, "aggregate": {}}), "nav_missing"),
    ])
    def test_unusable_audit_gives_a_code(self, make, code):
        day, a = make()
        assert TH.baseline_from_audit(day, a) == (None, code)
        res = TH.build(day, a, chain(), DEPS)
        assert res["available"] is False and res["reason"] == code and res["reason_zh"]


class TestSizing:
    """基线下行侧最差格 −9000（跌 30%）；候选 30x15 在该格 1 单位 +580（见 TestUnitCells）。"""

    def test_offset_target_rounds_to_whole_units(self):
        c = cand(build(), "30x15")
        s = c["sizing"]
        assert s["basis"] == "offset_target" and s["n_units"] == round(0.5 * 9000 / 580.0) == 8
        assert s["contracts_short"] == 8 and s["contracts_long"] == 16
        assert s["offset_achieved"] == pytest.approx(8 * 580.0 / 9000.0, abs=1e-4)
        assert s["net_cost_usd"] == pytest.approx(8 * -80.0)
        assert s["short_notional_usd"] == pytest.approx(8 * 100 * 95.0)

    def test_sized_at_the_downside_cell_not_at_an_upside_worst_cell(self):
        """有牙：最差格在上行（认沽帮不上）；规模必须仍按下行侧最差格来——否则会被「上行最差格」放大。"""
        cells = [{"spot_pct": sp, "iv_pts": 0, "pnl": p} for sp, p in ((-30, -4000.0), (0, 0.0), (10, -90_000.0))]
        c = cand(build(a=audit(cells)), "30x15")
        assert c["sizing"]["target_cell"]["spot_pct"] == -30
        assert c["sizing"]["n_units"] == round(0.5 * 4000 / 580.0) == 3

    def test_no_help_at_the_target_cell_falls_back_to_one_unit_with_a_reason(self):
        cells = [{"spot_pct": sp, "iv_pts": 0, "pnl": p} for sp, p in ((-10, -9000.0), (0, 0.0), (10, 0.0))]
        c = cand(build(a=audit(cells)), "30x15")           # −10% 是亏损谷：1 单位 −420
        s = c["sizing"]
        assert s["basis"] == "one_unit_fallback" and s["n_units"] == 1 and s["reason"] == "no_help_at_worst_cell"
        assert s["reason_zh"] and s["offset_achieved"] < 0           # 负的也照实给：它在那一格里是亏的

    def test_baseline_without_a_loss_falls_back_to_one_unit(self):
        cells = [{"spot_pct": sp, "iv_pts": 0, "pnl": 100.0} for sp in (-30, 0, 10)]
        s = cand(build(a=audit(cells)), "30x15")["sizing"]
        assert s["basis"] == "one_unit_fallback" and s["reason"] == "baseline_has_no_loss" and s["offset_achieved"] is None

    def test_notional_cap_binds_and_is_reported(self):
        c = cand(build(a=audit(nav=50_000.0)), "30x15")            # 上限 100% NAV：floor(50000 / 9500) = 5
        assert c["sizing"]["n_units"] == 5 and c["sizing"]["notional_capped"] is True
        assert "notional_capped" in c["flags"]
        assert c["sizing"]["short_notional_pct_nav"] == pytest.approx(5 * 9500 / 50_000 * 100, abs=0.01)
        assert c["sizing"]["offset_achieved"] == pytest.approx(5 * 580 / 9000, abs=1e-4)     # 实际抵消 < 目标 50%

    def test_one_unit_over_the_cap_still_shows_one_unit_and_says_so(self):
        c = cand(build(a=audit(nav=5_000.0)), "30x15")
        assert c["sizing"]["n_units"] == 1 and "notional_over_cap" in c["flags"]

    def test_open_interest_cap_binds(self):
        puts = chain()["contracts"]
        puts[2] = put(85, -0.15, 1.0, 1.1, oi=100.0)               # 买入腿：2% × 100 / 2 张 = 1 单位
        c = cand(build(c=chain(puts)), "30x15")
        assert c["sizing"]["n_units"] == 1 and c["sizing"]["capped"] is True and "size_capped" in c["flags"]

    @pytest.mark.parametrize("oi", [0.0, None])
    def test_missing_or_zero_open_interest_is_unknown_not_a_zero_cap(self, oi):
        """SPY 新到期日的未平仓量 d+1 才发布：0 是「还没有」，不是「没有流动性」——不能把规模压成 1。"""
        puts = chain()["contracts"]
        puts[0], puts[2] = put(95, -0.30, 3.0, 3.2, oi=oi), put(85, -0.15, 1.0, 1.1, oi=oi)
        c = cand(build(c=chain(puts)), "30x15")
        assert c["sizing"]["n_units"] == 8 and "oi_unknown" in c["flags"] and "size_capped" not in c["flags"]


class TestBuild:
    def test_effect_cells_add_up_and_worst_cells_are_reported(self):
        c = cand(build(), "30x15")
        e = c["effect"]
        by = {x["spot_pct"]: x for x in e["cells"]}
        assert by[-30]["base"] == -9000.0 and by[-30]["hedge"] == 8 * 580.0 and by[-30]["after"] == -9000.0 + 8 * 580.0
        assert by[-10]["hedge"] == 8 * -420.0                               # 亏损谷：候选自己在这一格是亏的
        assert e["worst_hedge_cell"] == {"spot_pct": -10, "iv_pts": 0, "pnl": 8 * -420.0}
        assert e["n_cells_worse"] == 1 and e["n_cells"] == 4          # 只有 −10% 那个亏损谷格里候选自己是亏的
        assert e["downside_worst_before"]["pnl"] == -9000.0
        assert e["downside_worst_after"]["pnl"] == min(x["after"] for x in e["cells"] if x["spot_pct"] < 0)
        assert e["worst_before_pct_nav"] == -9.0

    def test_greeks_after_is_baseline_plus_sized_dollars_over_nav(self):
        g = cand(build(), "30x15")["greeks"]
        for k in TH.GREEK_KEYS:
            assert g["after_pct_nav"][k] == pytest.approx(
                g["before_pct_nav"][k] + g["sized_usd"][k] / 100_000.0 * 100.0, abs=2e-3)

    def test_curve_is_the_sized_expiry_payoff(self):
        c = cand(build(), "30x15")
        pts = {p["spot_pct"]: p["pnl"] for p in c["expiry"]["curve"]}
        assert pts[0] == pytest.approx(8 * 80.0) and pts[-15] == pytest.approx(8 * -920.0)
        assert c["expiry"]["max_loss_usd"] == pytest.approx(8 * -920.0)

    def test_chain_flags(self):
        r = build(c=chain(vintage="2026-10-22"))
        assert "chain_not_audit_session" in r["flags"]
        assert "chain_not_audit_session" in build(c=chain(live=True))["flags"]
        r = build(c=chain(S=101.0))
        assert "spot_gap" in r["flags"] and r["chain"]["spot_vs_audit_pct"] == pytest.approx(1.0)
        assert build()["flags"] == []
        assert "baseline_partial" in build(a=audit(partial=True))["flags"]

    def test_wide_spread_leg_is_flagged_not_dropped(self):
        puts = chain()["contracts"]
        puts[0] = put(95, -0.30, 1.0, 2.0)                          # (2−1)/1.5 = 67% > 25%
        c = cand(build(c=chain(puts)), "30x15")
        assert c["available"] is True and "wide_spread:short" in c["flags"]

    @pytest.mark.parametrize("ch,code", [
        (None, "chain_unavailable"), ({}, "chain_empty"), ({"contracts": []}, "chain_empty"),
        ({"contracts": [put(95, -0.3, 1, 1.1)], "underlying_price": None}, "no_spot"),
        ({"contracts": [put(95, -0.3, 1, 1.1, dte=3)], "underlying_price": 100.0}, "no_expiry_in_window"),
    ])
    def test_chain_problems_give_a_code_and_keep_the_baseline(self, ch, code):
        r = TH.build("2026-10-23", audit(), ch, DEPS)
        assert r["available"] is False and r["reason"] == code and r["reason_zh"]
        assert r["baseline"]["worst_cell"]["pnl"] == -9000.0             # 基线仍能给，页面至少看得到「最差格在哪」

    def test_iv_missing_is_unavailable_with_the_leg_name(self):
        puts = chain()["contracts"]
        puts[0] = put(95, -0.30, 3.0, 3.2, iv=None)
        c = cand(build(c=chain(puts)), "30x15")
        assert c["available"] is False and c["reason"] == "iv_missing:short"


class TestHonesty:
    FORBIDDEN = ("recommend", "best", "winner", "score", "rank", "verdict", "pick", "optimal", "suggest")

    def test_no_verdict_style_keys_anywhere_in_the_payload(self):
        for res in (build(), build(c=chain(None, S=100.0)), TH.build(None, None, None, DEPS)):
            bad = [k for k in keys_of(res) if any(f in k.lower() for f in self.FORBIDDEN)]
            assert not bad, bad

    def test_candidate_order_is_the_fixed_grid_and_does_not_follow_the_effect(self):
        """有牙：喂一个让效果完全相反的基线（亏损在上行 / 在下行），顺序必须一字不差。"""
        order = [c["id"] for c in build()["candidates"]]
        assert order == ["30x15", "30x10", "30x05", "20x15", "20x10", "20x05"]
        flipped = [{"spot_pct": sp, "iv_pts": 0, "pnl": p} for sp, p in ((-30, 5000.0), (-10, 0.0), (0, 0.0), (10, -9000.0))]
        assert [c["id"] for c in build(a=audit(flipped))["candidates"]] == order

    def test_caveats_say_it_is_not_advice_and_name_the_valley(self):
        assert "不是" in TH.CAVEATS["not_advice"] and "谷" in TH.CAVEATS["valley"]
        for k in ("parallel_iv_no_skew", "no_margin", "unverified_constants", "american_style"):
            assert k in TH.CAVEATS

    def test_result_survives_json_round_trip_without_nan(self):
        txt = json.dumps(build(), allow_nan=False)
        assert "NaN" not in txt


class TestTables:
    SRC = MODULE.read_text(encoding="utf-8")
    SERVICE = (MODULE.parent / "service.py").read_text(encoding="utf-8")

    def _constants(self, *sources):
        out = []
        for src in sources:
            for n in ast.walk(ast.parse(src)):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    out.append(n.value)
                elif isinstance(n, ast.JoinedStr) and n.values and isinstance(n.values[0], ast.Constant):
                    out.append(str(n.values[0].value))
        return out

    def _used(self, key, tables, *sources):
        """key 在「登记它的那几张表」之外还出现过（精确相等、或 `key:` 开头的 f-string 头）。"""
        hits = sum(1 for c in self._constants(*sources) if c == key or c.startswith(key + ":"))
        return hits - sum(1 for t in tables if key in t)

    def test_every_registered_reason_is_used_somewhere(self):
        dead = [k for k in TH.REASONS if self._used(k, (TH.REASONS,), self.SRC, self.SERVICE) < 1]
        assert not dead, f"表里有死码：{dead}"

    def test_every_code_the_module_writes_is_registered(self):
        written = set(re.findall(r'unavailable\(\s*"([a-z_]+)"', self.SRC))
        written |= set(re.findall(r'"reason":\s*"([a-z_]+)"', self.SRC))
        written |= set(re.findall(r'out\["reason"\]\s*=\s*"([a-z_]+)"', self.SRC))
        written |= set(re.findall(r'return None, "([a-z_]+)"', self.SRC))
        written |= set(re.findall(r'f"([a-z_]+):\{', self.SRC))
        written |= set(re.findall(r'return unavailable\(\s*"([a-z_]+)"', self.SRC))
        written -= set(TH.FLAGS)                                     # `f"wide_spread:{...}"` 是旗标，不是原因码
        assert written, "正则没抓到任何码——这条测试没牙"
        assert written - set(TH.REASONS) == set(), written - set(TH.REASONS)
        assert {"audit_missing", "chain_empty", "no_help_at_worst_cell", "quote_not_ok"} <= written

    def test_every_flag_is_registered_with_a_short_label_and_vice_versa(self):
        assert set(TH.FLAGS) == set(TH.FLAGS_SHORT)
        used = set(re.findall(r'flags\.append\("([a-z_]+)"\)', self.SRC)) | set(re.findall(r'f"([a-z_]+):\{l\[', self.SRC))
        used -= set(TH.REASONS)                                      # `f"greeks_missing:{l['role']}…"` 是原因码，同一个 f-string 形状
        assert used, "正则没抓到任何旗标——这条测试没牙"
        used |= {"size_capped", "oi_unknown", "notional_capped", "notional_over_cap"}        # 经 (key, flag) 元组表追加
        assert used - set(TH.FLAGS) == set()
        dead = [k for k in TH.FLAGS if self._used(k, (TH.FLAGS, TH.FLAGS_SHORT), self.SRC) < 1]
        assert not dead, dead

    def test_reason_zh_resolves_suffix_codes_and_returns_none_for_unknown(self):
        assert "short" in TH.reason_zh("quote_not_ok:short") and TH.reason_zh("nope") is None and TH.reason_zh(None) is None


class TestPurity:
    def test_module_imports_nothing_that_touches_ledgers_or_the_network(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert imported <= {"__future__", "math", "dataclasses", "typing"}, imported
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in {"open", "print", "exec", "eval"}]

    def test_public_rules_round_trip_and_hide_the_curve_axis(self):
        r = TH.public_rules()
        assert r["version"] == TH.VERSION and "curve_spot_pct" not in r["rules"]
        json.dumps(r, allow_nan=False)
        assert math.isfinite(r["rules"]["offset_target"])


# ── 服务层 ─────────────────────────────────────────────────────────────────────────────

def _stress_cells():
    return [{"spot_pct": sp, "iv_pts": ivp, "partial": False, "iv_clamped": False,
             "pnl": round(-300.0 * abs(sp) - 40.0 * ivp - (400.0 if sp < 0 else 0.0), 2)}
            for sp in (-10, -5, 0, 5, 10) for ivp in (-10, 0, 10, 20)]


def _spy_chain(S=778.57, vintage="2026-10-09"):
    puts = [put(round(S * (1 - k), 0), -d, b, a, iv=0.17, oi=5000.0, dte=44, expiry="2026-11-23")
            for k, d, b, a in ((0.05, 0.30, 12.0, 12.2), (0.09, 0.15, 5.5, 5.7), (0.12, 0.10, 3.0, 3.1),
                               (0.17, 0.05, 1.0, 1.1), (0.04, 0.35, 10.0, 10.2), (0.07, 0.20, 8.0, 8.2))]
    return {"underlying_price": S, "vintage_date": vintage, "session_live": False, "contracts": puts}


@pytest.fixture
def root_with_stress(tmp_path):
    root = _write(tmp_path / "data")
    f = root / DIRNAME / "greeks_2026-10-09.json"
    a = json.loads(f.read_text(encoding="utf-8"))
    a["stress"] = {"cells": _stress_cells(), "partial": False, "excluded": []}
    f.write_text(json.dumps(a), encoding="utf-8")
    return root


def _svc(root, fetch):
    return AlphaBotService(fetch_fn=fetch, straddle_root=root, now_fn=lambda: NOW)


class TestService:
    def test_end_to_end_on_a_real_ledger_fixture(self, root_with_stress):
        calls = []

        def fetch(t, *, as_of=None):
            calls.append((t, as_of))
            return _spy_chain(), None

        r = _svc(root_with_stress, fetch).tail_hedge()
        assert r["available"] is True and r["baseline"]["date"] == "2026-10-09"
        assert calls == [("SPY", None)]
        assert r["baseline"]["adjusted_for_trade"] is True            # 夹具里 10-09 那天卖了 4 股 SPY
        assert r["meta"]["version"] == TH.VERSION and "valley" in r["meta"]["caveats"]
        assert [c["id"] for c in r["candidates"]] == ["30x15", "30x10", "30x05", "20x15", "20x10", "20x05"]
        assert any(c["available"] for c in r["candidates"])

    def test_read_only_the_data_root_is_untouched(self, root_with_stress):
        before = _fingerprint(root_with_stress)
        svc = _svc(root_with_stress, lambda t, *, as_of=None: (_spy_chain(), None))
        svc.tail_hedge()
        svc.tail_hedge(force=True)
        assert _fingerprint(root_with_stress) == before

    def test_cache_and_force(self, root_with_stress):
        n = []

        def fetch(t, *, as_of=None):
            n.append(1)
            return _spy_chain(), None

        svc = _svc(root_with_stress, fetch)
        svc.tail_hedge()
        r2 = svc.tail_hedge()
        assert len(n) == 1 and "cache_age_sec" in r2
        svc.tail_hedge(force=True)
        assert len(n) == 2

    @pytest.mark.parametrize("fetch,why", [
        (lambda t, *, as_of=None: (None, "stale_vintage"), "stale_vintage"),
        (lambda t, *, as_of=None: (_ for _ in ()).throw(RuntimeError("boom")), "exception:RuntimeError"),
    ])
    def test_chain_failure_is_reported_not_swallowed(self, root_with_stress, fetch, why):
        r = _svc(root_with_stress, fetch).tail_hedge()
        assert r["available"] is False and r["reason"] == "chain_unavailable" and r["chain_reason"] == why
        assert r["baseline"]["worst_cell"]["pnl"] < 0 and r["meta"]["reasons"]

    def test_no_audit_yet_says_so(self, tmp_path):
        r = _svc(tmp_path / "empty", lambda t, *, as_of=None: (_spy_chain(), None)).tail_hedge()
        assert r["available"] is False and r["reason"] == "audit_missing"

    def test_builder_crash_becomes_a_code_not_a_500(self, root_with_stress, monkeypatch):
        monkeypatch.setattr(TH, "build", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        r = _svc(root_with_stress, lambda t, *, as_of=None: (_spy_chain(), None)).tail_hedge()
        assert r["available"] is False and r["reason"] == "exception:ValueError" and r["reason_zh"]

    def test_route(self, root_with_stress):
        c = _client(_svc(root_with_stress, lambda t, *, as_of=None: (_spy_chain(), None)))
        r = c.get("/api/hedge/tail")
        assert r.status_code == 200 and r.json()["available"] is True
        assert c.get("/api/hedge/tail?force=1").status_code == 200
        assert c.post("/api/hedge/tail").status_code in (403, 404, 405)  # 只读：写方法被应用层挡掉（403）或没有这个方法

    def test_demo_mode_is_self_contained(self, tmp_path):
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, state_dir=tmp_path, now_fn=lambda: NOW)
        r = svc.tail_hedge()
        assert r["available"] is True and r["demo"] is True
        assert any(c["available"] and c["greeks"] for c in r["candidates"])      # 演示链补了 vega / theta

    def test_service_method_never_names_a_ledger_writer(self):
        src = (MODULE.parent / "service.py").read_text(encoding="utf-8")
        body = src[src.index("def tail_hedge("):src.index("def _straddle_quote(")]
        for forbidden in ("write_json", "append_jsonl", "atomic_json_write", "open(", ".write_text", "execute_hedge", "run_for_date"):
            assert forbidden not in body, forbidden


def _cells(*triples):
    return [{"spot_pct": sp, "iv_pts": ivp, "pnl": p} for sp, ivp, p in triples]


class TestSecondReviewFixes:
    """二次检查（独立审查者 + 自查）发现的问题。每条在旧实现上都会红；对应的变异在审查时存活过。"""

    def test_gamma_dollars_use_the_real_spot_not_the_degenerate_one(self):
        """旧测试 S=100 时 (0.01·S)² 恒为 1，把平方写成一次方也绿。S=600 才分得出。"""
        s = TH._leg("short", put(95, -0.30, 3.0, 3.2, gamma=0.02, vega=0.15, theta=-0.05), -1)
        l = TH._leg("long", put(85, -0.14, 1.0, 1.1, gamma=0.012, vega=0.10, theta=-0.03), 2)
        g, _ = TH.greeks_dollars([s, l], 600.0)
        assert g["gamma_dollar_per_1pct"] == pytest.approx(0.5 * -100 * 0.02 * 36.0 + 0.5 * 200 * 0.012 * 36.0)   # −36 + 43.2
        assert g["dollar_delta"] == pytest.approx(-100 * -0.30 * 600 + 200 * -0.14 * 600)                          # +1200

    def test_flat_spot_column_is_inside_the_covered_side(self):
        """现货 0%、IV +20 点是纯波动率冲击：后比例净多 Vega，帮得上——不许标「认沽覆盖不到」，也要拿它定规模。"""
        cells = _cells((-10, 0, -1000.0), (0, 20, -9000.0), (10, 0, -500.0))
        res = build(a=audit(cells))
        assert "worst_cell_uncovered" not in res["flags"]
        s = cand(res, "30x15")["sizing"]
        assert s["target_cell"]["spot_pct"] == 0 and s["target_cell"]["iv_pts"] == 20
        e = cand(res, "30x15")["effect"]                       # 「前 → 后」两端都要在同一类格子里比
        assert e["downside_worst_before"]["spot_pct"] == 0 and e["downside_worst_after"]["spot_pct"] == 0
        assert "worst_cell_uncovered" in build(a=audit(_cells((-10, 0, -1000.0), (10, 20, -9000.0))))["flags"]

    def test_no_cell_at_or_below_zero_means_unsized_not_an_upside_target(self):
        """网格里没有现货 ≤ 0 的格：不许退回去按上行最差格放大（实测旧实现 n=10，上一轮修过的同一类错）。"""
        res = build(a=audit(_cells((5, 0, -500.0), (10, 0, -2000.0), (10, 20, -9000.0))))
        for c in res["candidates"]:
            if not c["available"]:
                continue
            s = c["sizing"]
            assert s["basis"] == "one_unit_fallback" and s["reason"] == "no_downside_cells" and s["n_units"] == 1
            assert s["target_cell"]["spot_pct"] is None and s["offset_achieved"] is None
            assert c["effect"]["downside_worst_before"] is None and "unsized" in c["flags"] and s["reason_zh"]
        assert "worst_cell_uncovered" in res["flags"]

    def test_size_is_at_least_one_unit_when_the_loss_is_tiny(self):
        c = cand(build(a=audit(_cells((-30, 0, -10.0), (0, 0, 0.0)))), "30x15")
        assert c["sizing"]["basis"] == "offset_target" and c["sizing"]["n_units"] == 1      # round(0.5×10/580) = 0 ⇒ 抬到 1

    def test_open_interest_cap_rounds_down(self):
        """买入腿 399 张：2% × 399 / 2 = 3.99 ⇒ 3 单位（向上取整会得到 4，超过上限）。"""
        puts = chain()["contracts"]
        puts[2] = put(85, -0.15, 1.0, 1.1, oi=399.0)
        c = cand(build(c=chain(puts)), "30x15")
        assert c["sizing"]["n_units"] == 3 and c["sizing"]["capped"] is True

    def test_one_unit_already_over_the_open_interest_cap_is_flagged(self):
        puts = chain()["contracts"]
        puts[2] = put(85, -0.15, 1.0, 1.1, oi=2.0)                 # 2% × 2 / 2 = 0.02 ⇒ 上限 0 单位
        c = cand(build(c=chain(puts)), "30x15")
        assert c["sizing"]["n_units"] == 1 and "oi_over_cap" in c["flags"]
        assert "oi_over_cap" not in cand(build(), "30x15")["flags"]

    @pytest.mark.parametrize("dte,inside", [(20, False), (21, True), (90, True), (91, False)])
    def test_expiry_window_is_inclusive_on_both_ends(self, dte, inside):
        got = TH.pick_expiry([put(95, -0.3, 1, 1.1, dte=dte, expiry="E")])
        assert (got == ("E", dte)) is inside and (got is None) is (not inside)

    def test_trade_is_only_folded_in_when_the_audit_says_it_executed(self):
        a = audit(executed=False, trade={"shares": -4, "price": 100.0}, after_pct={"beta_dollar_delta": 1.0})
        b, _ = TH.baseline_from_audit("d", a)
        assert b["adjusted_for_trade"] is False and {c["spot_pct"]: c["pnl"] for c in b["cells"]}[-30] == -9000.0
        assert b["pct_nav"]["beta_dollar_delta"] == 5.0

    def test_equal_delta_gap_picks_the_higher_strike(self):
        # 0.75 / 0.25 离 0.5 都恰好差 0.25（二进制精确，不靠浮点噪声碰巧）：并列取更高的行权价（更近平值）
        book = [put(80, -0.25, 1.0, 1.1), put(90, -0.75, 2.0, 2.1)]
        assert TH.pick_put(book, 0.5, 0.30)["strike"] == 90.0
        assert TH.pick_put(list(reversed(book)), 0.5, 0.30)["strike"] == 90.0          # 与书里的顺序无关

    def test_unsized_flag_marks_every_fallback_and_only_those(self):
        sized = cand(build(), "30x15")
        assert sized["sizing"]["basis"] == "offset_target" and "unsized" not in sized["flags"]
        valley = cand(build(a=audit(_cells((-10, 0, -9000.0), (0, 0, 0.0)))), "30x15")
        assert valley["sizing"]["reason"] == "no_help_at_worst_cell" and "unsized" in valley["flags"]

    def test_chain_brief_carries_the_quote_time(self):
        ch = {**chain(), "payload_last_trade_time": "2026-10-23T16:15:00", "fetched_at": "2026-10-25T09:00:00-04:00"}
        b = build(c=ch)["chain"]
        assert b["quote_time"] == "2026-10-23T16:15:00" and b["fetched_at"] == "2026-10-25T09:00:00-04:00"

    def test_unreadable_ledger_is_not_reported_as_no_records(self, tmp_path):
        n = []
        svc = _svc(tmp_path, lambda t, *, as_of=None: (n.append(1), (_spy_chain(), None))[1])
        svc._hedge_view = lambda now: {"errors": {"history": "LedgerCorrupt: bad line"}, "history": None}
        r = svc.tail_hedge()
        assert r["available"] is False and r["reason"] == "ledger_unreadable" and r["reason_zh"]
        assert r["ledger_errors"] == {"history": "LedgerCorrupt: bad line"} and n == []         # 没去取链

    def test_force_clears_the_cboe_process_cache_and_a_plain_call_does_not(self, root_with_stress, monkeypatch):
        """旧实现：force 只绕过本层 60 秒缓存，底层 CBOE 进程缓存（最长 4 小时）原样命中，页面却写「刚拉取」。"""
        import sell_strike_report as R
        cleared, fetched = [], []
        monkeypatch.setattr(R, "_invalidate_cached_payload", lambda t: cleared.append(t))
        monkeypatch.setattr(R.LG, "_default_fetch", lambda t, as_of=None: (fetched.append(t), (_spy_chain(), None))[1])
        svc = AlphaBotService(straddle_root=root_with_stress, now_fn=lambda: NOW)         # 没有注入 fetch_fn：走生产取链入口
        assert svc.tail_hedge()["available"] is True
        assert cleared == [] and fetched == ["SPY"]
        svc.tail_hedge(force=True)
        assert cleared == ["SPY"] and fetched == ["SPY", "SPY"]

    def test_demo_chain_spot_matches_the_demo_audit_so_no_permanent_spot_gap(self, tmp_path):
        r = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, state_dir=tmp_path, now_fn=lambda: NOW).tail_hedge()
        assert "spot_gap" not in r["flags"] and abs(r["chain"]["spot_vs_audit_pct"]) < 0.01


class TestFrontend:
    HEDGE =(STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")
    TAIL = (STATIC / "pages" / "hedge_tail.js").read_text(encoding="utf-8")

    def test_hedge_page_mounts_the_panel_without_blocking_on_it(self):
        assert 'import { renderTailHedge } from "./hedge_tail.js"' in self.HEDGE
        assert re.search(r"^\s*renderTailHedge\(tailHost, alive\);", self.HEDGE, re.M)          # 没有 await：不拖慢本页
        assert "tailHost," in self.HEDGE

    def test_panel_uses_server_texts_and_never_copies_them(self):
        assert "/api/hedge/tail" in self.TAIL
        for table in (TH.REASONS, TH.FLAGS, TH.CAVEATS):
            for text in table.values():
                assert text not in self.TAIL, text[:30]
        for k in ("flags_short", "caveats", "reasons", "flags"):
            assert f"meta.{k}" in self.TAIL, k

    def test_candidates_are_never_sorted_in_the_browser(self):
        """候选顺序 = 服务端的固定网格；前端只许给现货轴排序（网格行）。"""
        sorts = [l for l in self.TAIL.splitlines() if ".sort(" in l]
        assert sorts and all("spot_pct" in l for l in sorts), sorts

    def test_panel_says_it_is_not_a_recommendation(self):
        assert "不是推荐" in self.TAIL and "不写账本" in self.TAIL

    def test_switching_candidates_releases_the_previous_chart(self):
        """旧实现每点一行新建一个 echarts 实例，旧的留在 live 里直到换页（每次窗口缩放都白白 resize）。"""
        charts = (STATIC / "charts.js").read_text(encoding="utf-8")
        assert re.search(r"export function release\(", charts) and "live.delete(ch)" in charts
        assert "release(chart)" in self.TAIL and "chart = d._after()" in self.TAIL

    def test_a_render_error_does_not_leave_the_panel_stuck_on_loading(self):
        assert re.search(r"try \{\s*renderTailBody\(host, alive, t\);\s*\} catch \(e\) \{\s*fill\(host, panel\(TITLE, \{\}, errorBox\(e\)", self.TAIL)

    def test_ledger_read_errors_and_quote_time_are_shown(self):
        assert "ledger_errors" in self.TAIL and "quote_time" in self.TAIL
