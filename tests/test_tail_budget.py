"""组合尾部预算（v0.45.649）`tail_budget.py`——纯函数层。每组回答一句「谁会红」：

  · 归因（`TestAttribution`）：各标的在最差格的贡献之和 = 全组合该格损益（残差单列）；下行 / 平盘侧与全网格最差格分开
    （成交 SPY 后最差格会移到上行，认沽管不到）；SPY 覆盖单列成一个桶；板块不在名单里 ⇒「未分类」而不是猜；
  · 影子闸（`TestGateShadow`）：试算的账 = 股票账 + SPY 覆盖 + **此前已开**的跨式 + 这一笔——所以顺序有影响（有牙：交换顺序，
    结果必须变）；超预算是**严格大于**（等于不算）；买入跨式不适用；判不了的写原因码、不写 0；
  · 简报（`TestBrief`）：告警严格大于线才响；只点名亏的标的；
  · 纯度 / 表对账（`TestPurity` / `TestTables`）。
压力函数用替身（每行自带各格损益、按格相加）——数字就是手算的，不借被测函数的输出当期望。全部离线。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import tail_budget as TB

MODULE = Path(TB.__file__)
SPOTS, IVS = (-10, 0, 10), (0, 20)
NAV = 100_000.0


def row(kind, ticker, **cells):
    """cells 的键写成 `m10_20` = 现货 −10%、IV +20；`p10_20` = 现货 +10%、IV +20；`z_20` = 现货 0%、IV +20。"""
    pnl = {}
    for k, v in cells.items():
        sp = {"m10": -10, "z": 0, "p10": 10}[k.split("_")[0]]
        iv = int(k.split("_")[1])
        pnl[(sp, iv)] = float(v)
    return {"kind": kind, "ticker": ticker, "pnl": pnl}


def stress_fn(rows, spy_price=None):
    cells = [{"spot_pct": sp, "iv_pts": iv, "pnl": sum(r["pnl"].get((sp, iv), 0.0) for r in rows)}
             for sp in SPOTS for iv in IVS]
    return {"cells": cells, "partial": False, "excluded": []}


SECTORS = {"A": "Tech", "B": "Tech", "C": "Energy"}
sector_of = SECTORS.get

#: 手算账本：A、B 两笔卖跨式、C 股票、SPY 覆盖。
#:   (−10,+20)：A −6000、B −3000、C +200、SPY +1000 ⇒ −7800；(+10,+20)：A −2000、B −5000、SPY −1000 ⇒ −8000
ROWS = [
    row("option", "A", m10_20=-6000, p10_20=-2000),
    row("option", "B", m10_20=-3000, p10_20=-5000),
    row("stock", "C", m10_20=200),
    row("hedge", "SPY", m10_20=1000, p10_20=-1000),
]


def attr(rows=None, nav=NAV, sec=sector_of):
    return TB.attribution(ROWS if rows is None else rows, 650.0, nav, stress_fn, sec)


class TestAttribution:
    def test_worst_downside_and_hand_numbers(self):
        a = attr()
        assert a["available"] is True
        assert (a["worst"]["spot_pct"], a["worst"]["iv_pts"], a["worst"]["pnl"]) == (10, 20, -8000.0)       # 上行最差
        assert (a["downside_worst"]["spot_pct"], a["downside_worst"]["pnl"]) == (-10, -7800.0)              # 下行 / 平盘侧最差
        assert a["loss_pct_nav"] == 8.0 and a["worst"]["pct_nav"] == -8.0

    def test_contributions_sum_to_the_portfolio_cell(self):
        a = attr()
        by = {t["ticker"]: t for t in a["by_ticker"]}
        assert by["B"]["at_worst"] == -5000.0 and by["A"]["at_worst"] == -2000.0 and by["SPY"]["at_worst"] == -1000.0
        assert by["C"]["at_worst"] == 0.0
        assert sum(t["at_worst"] for t in a["by_ticker"]) == a["worst"]["pnl"] and a["residual_usd"] == 0.0
        # 下行格：A −6000、B −3000、C +200、SPY +1000
        assert by["A"]["at_downside"] == -6000.0 and by["C"]["at_downside"] == 200.0 and by["SPY"]["at_downside"] == 1000.0
        assert sum(t["at_downside"] for t in a["by_ticker"]) == a["downside_worst"]["pnl"]

    def test_sorted_most_negative_first(self):
        assert [t["ticker"] for t in attr()["by_ticker"]] == ["B", "A", "SPY", "C"]

    def test_sector_rollup_hedge_bucket_and_unclassified(self):
        a = attr(sec={"A": "Tech"}.get)                  # B、C 不在名单
        secs = {s["sector"]: s for s in a["by_sector"]}
        assert secs["Tech"]["at_worst"] == -2000.0 and secs["Tech"]["tickers"] == ["A"]
        assert secs[TB.UNCLASSIFIED]["at_worst"] == -5000.0 and set(secs[TB.UNCLASSIFIED]["tickers"]) == {"B", "C"}
        assert secs[TB.HEDGE_LABEL]["at_worst"] == -1000.0 and secs[TB.HEDGE_LABEL]["tickers"] == ["SPY"]
        assert sum(s["at_worst"] for s in a["by_sector"]) == a["worst"]["pnl"]

    def test_one_ticker_with_stock_and_option_is_merged_with_a_kind_split(self):
        rows = ROWS + [row("stock", "A", m10_20=-100, p10_20=300)]
        a = attr(rows)
        # 加了这行股票后全组合最差格从 (+10,20) −8000+300 移到 (−10,20) −7800−100 = −7900
        assert (a["worst"]["spot_pct"], a["worst"]["pnl"]) == (-10, -7900.0)
        t = {x["ticker"]: x for x in a["by_ticker"]}["A"]
        assert t["at_worst"] == -6000.0 - 100.0 and t["n_rows"] == 2
        assert t["by_kind"]["option"]["at_worst"] == -6000.0 and t["by_kind"]["stock"]["at_worst"] == -100.0
        assert sum(x["at_worst"] for x in a["by_ticker"]) == a["worst"]["pnl"]

    def test_own_worst_is_the_tickers_own_worst_cell_not_the_portfolios(self):
        a = {x["ticker"]: x for x in attr()["by_ticker"]}["A"]
        assert (a["own_worst"]["spot_pct"], a["own_worst"]["pnl"]) == (-10, -6000.0)       # A 自己最亏在下行

    @pytest.mark.parametrize("nav", [None, 0, -5.0, "x", float("nan")])
    def test_unusable_nav_is_a_code_not_a_zero(self, nav):
        a = attr(nav=nav)
        assert a["available"] is False and a["reason"] == "nav_unavailable" and a["reason_zh"]

    def test_empty_stress_is_a_code(self):
        a = TB.attribution(ROWS, None, NAV, lambda rows, spy: {"cells": []}, sector_of)
        assert a["available"] is False and a["reason"] == "stress_unavailable"

    def test_partial_and_excluded_pass_through(self):
        def fn(rows, spy):
            return {**stress_fn(rows, spy), "partial": True, "excluded": ["X(option:expired 1d)"]}
        a = TB.attribution(ROWS, None, NAV, fn, sector_of)
        assert a["partial"] is True and a["excluded"] == ["X(option:expired 1d)"]

    def test_no_loss_book_has_zero_loss_not_a_negative_one(self):
        a = attr([row("stock", "C", m10_20=500, p10_20=700)])
        assert a["loss_pct_nav"] == 0.0 and TB.brief(a, None)["breach"] is False

    def test_every_cell_a_gain_is_zero_loss_not_a_negative_one(self):
        """旧用例没设置的格子是 0，把「最差格」托在 0 上，掩盖了符号；这里六格全为正。"""
        r = row("stock", "C", m10_0=100, m10_20=500, z_0=50, z_20=70, p10_0=300, p10_20=700)
        a = attr([r])
        assert a["worst"]["pnl"] == 50.0 and a["loss_pct_nav"] == 0.0

    def test_a_nonzero_residual_is_reported_not_smoothed(self):
        """压力函数若不可加（比如每次调用凭空加 1 美元），各标的之和就对不上全组合——残差必须原样报出来。
        4 个桶各 +1、全组合 +1 ⇒ 最差格 −7999，各桶之和 −7996，残差 −3。"""
        def lumpy(rows, spy):
            t = stress_fn(rows, spy)
            return {**t, "cells": [{**c, "pnl": c["pnl"] + 1.0} for c in t["cells"]]}
        a = TB.attribution(ROWS, None, NAV, lumpy, sector_of)
        assert a["worst"]["pnl"] == -7999.0 and a["residual_usd"] == -3.0

    def test_flat_spot_column_counts_as_the_downside_side(self):
        """现货 0%、IV +20 点是纯波动率冲击：后比例净多 Vega、帮得上，所以归在「下行 / 平盘侧」。"""
        a = attr([row("option", "A", m10_20=-1000, z_20=-9000, p10_20=-500)])
        assert (a["downside_worst"]["spot_pct"], a["downside_worst"]["pnl"]) == (0, -9000.0)
        assert (a["worst"]["spot_pct"], a["worst"]["pnl"]) == (0, -9000.0)


class TestGateShadow:
    """book 顺序 A、B：先 A 后 B。非跨式（C + SPY）：(−10,20) +1200、(10,20) −1000 ⇒ 最差 −1000（1.0%）。"""

    def setup_method(self):
        self.a = attr()
        self.rest = TB._cells(stress_fn([r for r in ROWS if r["kind"] != "option"], None))

    def gate(self, entries, order=("A", "B"), budgets=(5, 8, 10)):
        return TB.gate_shadow(self.a, self.rest, list(order), entries, NAV, budgets)

    def test_second_entry_sees_the_first_and_the_numbers_are_by_hand(self):
        g = self.gate([{"ticker": "B", "side": "short", "entry_date": "d"}])
        e = g["entries"][0]
        # 此前：rest + A：(−10,20) 1200−6000 = −4800；(10,20) −1000−2000 = −3000 ⇒ 4.8%。加上 B：−7800 / −8000 ⇒ 8.0%
        assert e["loss_before_pct"] == 4.8 and e["loss_after_pct"] == 8.0 and e["marginal_pct"] == 3.2
        assert e["would_skip"] == {"5": True, "8": False, "10": False}        # 8.0 > 8 不成立：严格大于
        assert g["n_would_skip"] == {"5": 1, "8": 0, "10": 0} and g["budgets_pct"] == ["5", "8", "10"]

    def test_order_matters_swapping_the_book_order_changes_the_result(self):
        """有牙：同一笔 A，排第一与排第二，试算前的账不同。"""
        first = self.gate([{"ticker": "A", "side": "short"}], order=("A", "B"))["entries"][0]
        second = self.gate([{"ticker": "A", "side": "short"}], order=("B", "A"))["entries"][0]
        assert first["loss_before_pct"] == 1.0 and first["loss_after_pct"] == 4.8        # rest 1.0% + A
        # B 先在：before = rest + B：(−10,20) 1200−3000 = −1800；(10,20) −1000−5000 = −6000 ⇒ 6.0%；加 A ⇒ 8.0%
        assert second["loss_before_pct"] == 6.0 and second["loss_after_pct"] == 8.0
        assert first != second

    def test_equal_to_the_budget_is_not_a_skip_but_a_hair_over_is(self):
        e = self.gate([{"ticker": "B", "side": "short"}], budgets=(7.99, 8.0, 8.01))["entries"][0]
        assert e["would_skip"] == {"7.99": True, "8": False, "8.01": False}

    def test_long_straddle_does_not_apply(self):
        e = self.gate([{"ticker": "B", "side": "long"}])["entries"][0]
        assert e["applies"] is False and e["evaluable"] is True and not any(e["would_skip"].values())
        assert e["loss_after_pct"] == 8.0                                  # 数照算，只是闸不适用

    def test_unevaluable_entries_name_the_reason_and_are_counted(self):
        g = self.gate([{"ticker": "Z", "side": "short"}, {"ticker": "A", "side": "short"}], order=("A", "B"))
        z = g["entries"][0]
        assert z["evaluable"] is False and z["reason"] == "not_in_book_order" and z["reason_zh"] and "would_skip" not in z
        assert g["n_unevaluated"] == 1 and g["n_would_skip"] == {"5": 0, "8": 0, "10": 0}      # A 的 4.8% 不超 5%
        a = dict(self.a)
        a["_option_cells"] = {"B": a["_option_cells"]["B"]}                # A 的跨式腿没进压力表
        g2 = TB.gate_shadow(a, self.rest, ["A", "B"], [{"ticker": "A", "side": "short"}], NAV, (5,))
        assert g2["entries"][0]["reason"] == "no_grid_for_ticker" and g2["n_unevaluated"] == 1

    def test_two_same_day_entries_the_later_one_sees_the_earlier(self):
        g = self.gate([{"ticker": "A", "side": "short"}, {"ticker": "B", "side": "short"}])
        a_, b_ = g["entries"]
        assert a_["loss_after_pct"] == 4.8 and b_["loss_before_pct"] == 4.8 and b_["loss_after_pct"] == 8.0

    def test_no_entries_is_an_empty_but_well_formed_record(self):
        g = self.gate([])
        assert g["entries"] == [] and g["n_entries"] == 0 and g["n_would_skip"] == {"5": 0, "8": 0, "10": 0}


class TestBrief:
    def test_breach_is_strictly_above_the_line(self):
        a = attr()
        assert TB.brief(a, None, alert_pct=8.0)["breach"] is False          # 8.0 不超 8.0
        assert TB.brief(a, None, alert_pct=7.99)["breach"] is True
        assert TB.brief(a, None)["alert_pct"] == TB.CONFIG["alert_pct"]

    def test_top_names_only_the_losers_and_in_order(self):
        b = TB.brief(attr(), None, alert_pct=1.0)
        assert [t["ticker"] for t in b["top"]] == ["B", "A", "SPY"] and all(t["at_worst_pct"] < 0 for t in b["top"])
        assert b["top"][0]["at_worst_pct"] == -5.0 and b["worst"]["pnl"] == -8000.0

    def test_top_never_names_a_ticker_that_did_not_lose(self, monkeypatch):
        """有牙：点名个数放大到比持仓还多，贡献为 0 的 C 也不许进来（只点名亏的）。"""
        monkeypatch.setitem(TB.CONFIG, "top_n", 10)
        b = TB.brief(attr(), None, alert_pct=1.0)
        assert [t["ticker"] for t in b["top"]] == ["B", "A", "SPY"] and "C" not in {t["ticker"] for t in b["top"]}

    def test_unavailable_brief_never_breaches(self):
        b = TB.brief(attr(nav=None), None)
        assert b["available"] is False and b["breach"] is False and b["reason"] == "nav_unavailable"

    def test_partial_is_carried_so_the_alert_can_say_the_loss_may_be_larger(self):
        def fn(rows, spy):
            return {**stress_fn(rows, spy), "partial": True, "excluded": ["x"]}
        assert TB.brief(TB.attribution(ROWS, None, NAV, fn, sector_of), None)["partial"] is True


class TestBuild:
    POS = [{"ticker": "A", "side": "short", "entry_date": "2026-10-20"},
           {"ticker": "B", "side": "short", "entry_date": "2026-10-22"}]

    def test_only_entries_dated_as_of_are_evaluated_and_the_book_order_is_the_file_order(self):
        out = TB.build(ROWS, None, NAV, stress_fn, sector_of, self.POS, "2026-10-22")
        es = out["gate_shadow"]["entries"]
        assert [e["ticker"] for e in es] == ["B"] and es[0]["entry_date"] == "2026-10-22" and es[0]["sector"] == "Tech"
        assert es[0]["loss_before_pct"] == 4.8 and es[0]["loss_after_pct"] == 8.0
        assert TB.build(ROWS, None, NAV, stress_fn, sector_of, self.POS, "2026-10-21")["gate_shadow"]["entries"] == []

    def test_rest_of_the_book_excludes_the_option_rows(self):
        """有牙：若把跨式腿也算进「非跨式」，B 的试算前账会重复计 A / B。"""
        e = TB.build(ROWS, None, NAV, stress_fn, sector_of, self.POS, "2026-10-20")["gate_shadow"]["entries"][0]
        assert e["ticker"] == "A" and e["loss_before_pct"] == 1.0 and e["loss_after_pct"] == 4.8

    def test_output_has_no_private_keys_and_is_strict_json(self):
        out = TB.build(ROWS, None, NAV, stress_fn, sector_of, self.POS, "2026-10-22")
        assert not [k for k in out if k.startswith("_")]
        json.dumps(out, allow_nan=False)
        assert out["brief"]["gate_n_entries"] == 1 and out["version"] == TB.VERSION and out["as_of"] == "2026-10-22"

    def test_unavailable_still_returns_a_brief(self):
        out = TB.build(ROWS, None, None, stress_fn, sector_of, self.POS, "2026-10-22")
        assert out["available"] is False and out["reason"] == "nav_unavailable" and out["brief"]["breach"] is False

    def test_positions_without_a_date_are_never_entries(self):
        out = TB.build(ROWS, None, NAV, stress_fn, sector_of, [{"ticker": "A", "side": "short"}], "2026-10-22")
        assert out["gate_shadow"]["entries"] == []


class TestDerivedLines:
    def test_budget_is_split_in_half_between_gamma_and_vega(self):
        d = TB.derived_greek_lines(20.0, 10.0, 20.0)
        assert d == {"gamma_alert_pct": 0.1, "vega_alert_pct": 0.5}
        # 两条线同时顶到 ⇒ 二阶近似的损失正好等于预算
        assert d["gamma_alert_pct"] * 10.0 ** 2 + d["vega_alert_pct"] * 20.0 == pytest.approx(20.0)

    @pytest.mark.parametrize("alert,spot,iv,gamma,vega", [(10.0, 10.0, 20.0, 0.05, 0.25), (20.0, 5.0, 20.0, 0.4, 0.5),
                                                          (20.0, 10.0, 10.0, 0.1, 1.0), (30.0, 10.0, 20.0, 0.15, 0.75)])
    def test_lines_follow_the_budget_and_the_shock_size(self, alert, spot, iv, gamma, vega):
        d = TB.derived_greek_lines(alert, spot, iv)
        assert d["gamma_alert_pct"] == pytest.approx(gamma) and d["vega_alert_pct"] == pytest.approx(vega)

    @pytest.mark.parametrize("args", [(0, 10, 20), (20, 0, 20), (20, 10, 0), (-1, 10, 20), (None, 10, 20), (20, "x", 20)])
    def test_unusable_inputs_raise_instead_of_dividing_by_zero(self, args):
        with pytest.raises(ValueError):
            TB.derived_greek_lines(*args)


class TestSlotCap:
    def test_budget_divided_by_the_slots(self):
        assert TB.slot_cap_pct(20.0, 6) == pytest.approx(20.0 / 6)
        assert TB.slot_cap_pct(20.0, 4) == 5.0
        assert TB.slot_cap_pct(20.0, None) is None and TB.slot_cap_pct(20.0, 0) is None

    def test_each_straddle_is_compared_with_its_own_cap_strictly(self, monkeypatch):
        a = TB.attribution(ROWS, None, NAV, stress_fn, sector_of, max_open=6)
        by = {t["ticker"]: t for t in a["by_ticker"]}
        # A 跨式腿单独最差格 −6000（6.0% NAV）、B −5000（5.0%）；上限 20/6 = 3.33%
        assert by["A"]["slot"] == {"loss_pct": 6.0, "cap_pct": 3.33, "over_cap": True, "multiple": 1.8}
        assert by["B"]["slot"]["over_cap"] is True and by["B"]["slot"]["multiple"] == 1.5
        assert a["slot_cap_pct"] == 3.33 and a["max_open"] == 6 and a["n_over_cap"] == 2 and a["over_cap_tickers"] == ["B", "A"]
        # 没有跨式腿的标的没有单仓这回事
        assert by["C"]["slot"] is None and by["SPY"]["slot"] is None
        # 边界：上限抬到 5.0%（预算 30 ÷ 6），B 恰好 5.0% ⇒ 不算超（严格大于），A 仍超
        monkeypatch.setitem(TB.CONFIG, "alert_pct", 30.0)
        a2 = TB.attribution(ROWS, None, NAV, stress_fn, sector_of, max_open=6)
        by2 = {t["ticker"]: t for t in a2["by_ticker"]}
        assert by2["B"]["slot"]["over_cap"] is False and by2["A"]["slot"]["over_cap"] is True and a2["over_cap_tickers"] == ["A"]

    def test_unknown_slot_count_gives_no_cap_not_a_zero_cap(self):
        a = TB.attribution(ROWS, None, NAV, stress_fn, sector_of)
        assert a["slot_cap_pct"] is None and a["n_over_cap"] == 0
        assert all(t["slot"] is None for t in a["by_ticker"])

    def test_brief_names_the_over_cap_positions(self):
        b = TB.brief(TB.attribution(ROWS, None, NAV, stress_fn, sector_of, max_open=6), None, alert_pct=1.0)
        assert b["slot_cap_pct"] == 3.33 and b["n_over_cap"] == 2
        assert [(o["ticker"], o["loss_pct"], o["multiple"]) for o in b["over_cap"]] == [("B", 5.0, 1.5), ("A", 6.0, 1.8)]

    def test_build_passes_the_slot_count_through(self):
        out = TB.build(ROWS, None, NAV, stress_fn, sector_of, TestBuild.POS, "2026-10-22", max_open=6)
        assert out["slot_cap_pct"] == 3.33 and out["brief"]["n_over_cap"] == 2


def _audit(spy, nav, leg, rows):
    return {"spy_price": spy, "nav": {"nav": nav, "components": {"straddle_leg": leg}}, "rows": rows}


def opt(symbol="X", qty=-100.0, price=100.0, mid=5.0, iv=0.30, delta=0.5, gamma=0.02, vega=0.10, theta=-0.05):
    return {"kind": "option", "symbol": symbol, "qty": qty, "price": price, "mid": mid, "iv": iv, "delta": delta, "gamma": gamma,
            "vega": vega, "theta": theta, "vega_dollar_per_pt": qty * vega}


def quad_stress(rows, spy):
    """f(s, iv) = −4s + 0.5s² − 2·iv + 7（7 = 模型基差）——对现货二次、对 IV 线性，插值应当**精确**。"""
    cells = [{"spot_pct": s_, "iv_pts": iv, "pnl": -4.0 * s_ + 0.5 * s_ * s_ - 2.0 * iv + 7.0}
             for s_ in (-10, -5, 0, 5, 10) for iv in (-10, 0, 10, 20)]
    return {"cells": cells, "partial": False, "excluded": []}


class TestInterpolation:
    CELLS = {(s_, iv): -4.0 * s_ + 0.5 * s_ * s_ - 2.0 * iv + 7.0 for s_ in (-10, -5, 0, 5, 10) for iv in (-10, 0, 10, 20)}

    @pytest.mark.parametrize("s_,iv", [(1.0, 1.0), (-3.7, 4.2), (9.0, 19.0), (-9.5, -9.5), (0, 0), (5, 10), (2.4, -7.3)])
    def test_exact_for_a_quadratic_in_spot_and_linear_in_iv(self, s_, iv):
        want = -4.0 * s_ + 0.5 * s_ * s_ - 2.0 * iv + 7.0
        assert TB.interp_cell(self.CELLS, s_, iv) == pytest.approx(want, abs=1e-9)

    def test_outside_the_grid_is_none_not_an_extrapolation(self):
        assert TB.interp_cell(self.CELLS, 10.5, 0) is None and TB.interp_cell(self.CELLS, 0, 21) is None
        assert TB.interp_cell(self.CELLS, -10.1, 0) is None and TB.interp_cell(self.CELLS, 0, -10.5) is None

    def test_linear_interpolation_would_be_wrong_here(self):
        """有牙：现货方向若退化成 [0, 5] 两点线性，f(1,0) 会得 5.5（真值 −4+0.5+7 = 3.5）——Gamma 项被放大 5 倍。"""
        assert TB.interp_cell(self.CELLS, 1.0, 0.0) == pytest.approx(3.5)


class TestModelCheck:
    """一条卖出 call：delta 0.5 / gamma 0.02 / vega 0.10 / theta −0.05（每股）、1 张（−100 股）。
    昨天 S=100 mid 5.0 IV 30%，今天 S=102 mid 6.2 IV 31%，SPY +1%，隔 1 个日历日。"""

    D0, D1 = "2026-10-15", "2026-10-16"

    def pair(self, **kw):
        a0 = _audit(600.0, 100_000.0, 1000.0, [opt()])
        a1 = _audit(kw.pop("spy1", 606.0), 99_000.0, 880.0, [opt(**{"price": 102.0, "mid": 6.2, "iv": 0.31, **kw})])
        return {self.D0: a0, self.D1: a1}

    def test_the_two_actuals_and_the_greeks_prediction_by_hand(self):
        (p,) = TB.model_check(self.pair(), quad_stress)["pairs"]
        assert p["status"] == "ok" and p["days"] == 1 and p["n_legs"] == 1
        assert p["actual_audit"] == -120.0                       # −100 × (6.2 − 5.0)
        assert p["actual_ledger"] == -120.0                      # 1000 → 880
        # −100 × (0.5×2 + 0.5×0.02×2² + 0.10×1 + (−0.05)×1) = −100 × 1.09
        assert p["pred_greeks"] == pytest.approx(-109.0)
        assert p["spot_pct"] == pytest.approx(1.0) and p["iv_shift_pts"] == pytest.approx(1.0)

    def test_the_grid_prediction_by_hand_removes_the_basis_and_adds_theta(self):
        (p,) = TB.model_check(self.pair(), quad_stress)["pairs"]
        # f(1,1) = −4 + 0.5 − 2 + 7 = 1.5；f(0,0) = 7（模型基差）；θ：−100 × (−0.05) × 1 = +5 ⇒ 1.5 − 7 + 5
        assert p["pred_grid"] == pytest.approx(-0.5)

    def test_theta_scales_with_the_calendar_days_between_the_audits(self):
        p0 = self.pair()
        a = {"2026-10-16": p0[self.D0], "2026-10-19": p0[self.D1]}        # 周五 → 下周一：3 个日历日
        (p,) = TB.model_check(a, quad_stress)["pairs"]
        assert p["days"] == 3 and p["pred_greeks"] == pytest.approx(-100 * (1 + 0.04 + 0.10 - 0.05 * 3))

    def test_summary_errors_are_prediction_minus_actual(self):
        sm = TB.model_check(self.pair(), quad_stress)["summary"]
        assert sm["grid_vs_ledger"]["mean_error_usd"] == pytest.approx(-0.5 - -120.0)
        assert sm["greeks_vs_ledger"]["mean_error_usd"] == pytest.approx(-109.0 - -120.0)
        assert sm["greeks_vs_audit"]["n"] == 1 and sm["greeks_vs_ledger"]["r2"] is None          # 1 对没有 R²
        assert sm["ledger_audit_max_diff_usd"] == 0.0

    def test_ledger_and_audit_actuals_can_disagree_and_the_gap_is_reported(self):
        a = self.pair()
        a[self.D1]["nav"]["components"]["straddle_leg"] = 850.0
        mc = TB.model_check(a, quad_stress)
        assert mc["pairs"][0]["actual_ledger"] == -150.0 and mc["summary"]["ledger_audit_max_diff_usd"] == 30.0

    def test_missing_ledger_nav_leaves_only_that_actual_empty(self):
        a = self.pair()
        a[self.D1]["nav"]["components"]["straddle_leg"] = None
        (p,) = TB.model_check(a, quad_stress)["pairs"]
        assert p["actual_ledger"] is None and p["actual_audit"] == -120.0
        assert TB.model_check(a, quad_stress)["summary"]["grid_vs_ledger"] is None

    @pytest.mark.parametrize("mutate,code", [
        (lambda a: a[TestModelCheck.D1].update(rows=[opt(qty=-200.0)]), "composition_changed"),
        (lambda a: a[TestModelCheck.D1].update(rows=[opt(symbol="Y")]), "composition_changed"),
        (lambda a: a[TestModelCheck.D1].update(rows=[]), "no_options"),
        (lambda a: a[TestModelCheck.D0].update(rows=[{"kind": "stock", "ticker": "A"}]), "no_options"),
        (lambda a: a[TestModelCheck.D1].update(rows=[{**opt(), "mid": None}]), "row_incomplete"),
        (lambda a: a[TestModelCheck.D0].update(rows=[{**opt(), "gamma": None}]), "row_incomplete"),
        (lambda a: a[TestModelCheck.D1].update(rows=[{**opt(), "iv": None}]), "row_incomplete"),
    ])
    def test_a_pair_that_is_not_a_clean_price_shock_is_skipped_with_its_reason(self, mutate, code):
        a = {self.D0: _audit(600.0, 1e5, 1000.0, [opt()]), self.D1: _audit(606.0, 1e5, 880.0, [opt(price=102.0, mid=6.2, iv=0.31)])}
        mutate(a)
        mc = TB.model_check(a, quad_stress)
        p = mc["pairs"][0]
        assert p["status"] == "skipped" and p["reason"] == code and p["reason_zh"] and "pred_greeks" not in p
        assert mc["n_used"] == 0 and mc["skipped_by_reason"] == {code: 1} and mc["summary"]["greeks_vs_ledger"] is None

    @pytest.mark.parametrize("d1,ok", [("2026-10-20", True), ("2026-10-21", False), ("2026-12-01", False)])
    def test_a_gap_over_five_calendar_days_is_skipped(self, d1, ok):
        a = {self.D0: _audit(600.0, 1e5, 1000.0, [opt()]), d1: _audit(606.0, 1e5, 880.0, [opt(price=102.0, mid=6.2, iv=0.31)])}
        p = TB.model_check(a, quad_stress)["pairs"][0]
        assert (p["status"] == "ok") is ok and (ok or p["reason"] == "gap_too_long")

    def test_a_shock_outside_the_grid_drops_only_the_grid_prediction(self):
        (p,) = TB.model_check(self.pair(spy1=700.0), quad_stress)["pairs"]        # SPY +16.7%
        assert p["status"] == "ok" and p["pred_grid"] is None and "网格之外" in p["grid_note"]
        assert p["pred_greeks"] is not None

    def test_without_a_stress_function_there_is_no_grid_prediction_but_the_rest_stays(self):
        (p,) = TB.model_check(self.pair(), None)["pairs"]
        assert p["pred_grid"] is None and p["grid_note"] == "没有压力函数" and p["pred_greeks"] == pytest.approx(-109.0)

    def test_iv_shift_is_vega_weighted_across_legs(self):
        a0 = _audit(600.0, 1e5, 1000.0, [opt("X", vega=0.10), opt("Y", vega=0.30, mid=2.0)])
        a1 = _audit(606.0, 1e5, 880.0, [opt("X", price=102.0, mid=6.2, iv=0.31), opt("Y", price=102.0, mid=2.5, iv=0.33)])
        (p,) = TB.model_check({self.D0: a0, self.D1: a1}, quad_stress)["pairs"]
        # X：|−100×0.10| = 10 权重、+1 点；Y：|−100×0.30| = 30 权重、+3 点 ⇒ (10×1 + 30×3) / 40 = 2.5
        assert p["iv_shift_pts"] == pytest.approx(2.5)

    def test_r2_and_mean_errors_by_hand(self):
        pairs = [{"nav": 1e5, "pred_greeks": a_ + 10.0, "actual_ledger": a_} for a_ in (-100.0, 50.0, 200.0)]
        st = TB._stats(pairs, "pred_greeks", "actual_ledger")
        # 误差恒 +10；SST = 150² + 0 + 150² = 45000；R² = 1 − 300/45000
        assert st["n"] == 3 and st["mean_error_usd"] == 10.0 and st["mean_abs_error_usd"] == 10.0
        assert st["r2"] == pytest.approx(0.993, abs=1e-3) and st["mean_abs_actual_usd"] == pytest.approx(350.0 / 3, abs=0.01)

    def test_two_pairs_still_have_no_r2(self):
        """有牙：1 对时 SST = 0 会掩盖「n ≥ 3 才给 R²」这条下限；2 对（SST ≠ 0）才分得出。"""
        pairs = [{"nav": 1e5, "pred_greeks": a_ + 5.0, "actual_ledger": a_} for a_ in (-100.0, 200.0)]
        st = TB._stats(pairs, "pred_greeks", "actual_ledger")
        assert st["n"] == 2 and st["r2"] is None and st["mean_error_usd"] == 5.0

    def test_result_is_strict_json_and_documents_itself(self):
        mc = TB.model_check(self.pair(), quad_stress)
        json.dumps(mc, allow_nan=False)
        assert {"scope", "actual_ledger", "actual_audit", "pred_grid", "pred_greeks", "small_n"} <= set(mc["notes"])
        assert "单名自己的跳空" in mc["notes"]["pred_grid"] and "只有几对" in mc["notes"]["small_n"]


class TestTables:
    SRC = MODULE.read_text(encoding="utf-8")

    def test_every_written_reason_is_registered_and_every_registered_one_is_used(self):
        written = set(re.findall(r'reason="([a-z_]+)"', self.SRC)) | set(re.findall(r'"reason": "([a-z_]+)"', self.SRC))
        written |= set(re.findall(r'"reason", "([a-z_]+)"', self.SRC))
        assert {"nav_unavailable", "stress_unavailable", "not_in_book_order", "no_grid_for_ticker"} <= written, written
        assert written - set(TB.REASONS) == set(), written - set(TB.REASONS)
        for k in TB.REASONS:
            uses = [n for n in ast.walk(ast.parse(self.SRC)) if isinstance(n, ast.Constant) and n.value == k]
            assert len(uses) >= 2, f"{k}：登记之外没被用过（死码）"


class TestSkipTable:
    SRC = MODULE.read_text(encoding="utf-8")

    def test_every_skip_code_written_is_registered_and_every_registered_one_is_used(self):
        written = set(re.findall(r'skip\("([a-z_]+)"\)', self.SRC))
        assert {"gap_too_long", "no_options", "composition_changed", "row_incomplete"} <= written, written
        assert written == set(TB.CHECK_SKIPS), written ^ set(TB.CHECK_SKIPS)


class TestPurity:
    def test_module_imports_only_math_and_typing(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert imported <= {"__future__", "math", "typing", "datetime"}, imported
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in {"open", "print", "exec", "eval"}]
        # datetime 只许做日期差（`date.fromisoformat`）：读钟（today / now）会让同一份审计在不同日子算出不同结果
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in {"today", "now", "utcnow", "time"}]

    def test_config_is_documented_as_subjective_and_budgets_are_ordered(self):
        assert "主观" in MODULE.read_text(encoding="utf-8")
        b = TB.CONFIG["shadow_budgets_pct"]
        assert list(b) == sorted(b) and all(x > 0 for x in b)
