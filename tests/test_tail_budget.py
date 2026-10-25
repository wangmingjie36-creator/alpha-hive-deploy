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


class TestPurity:
    def test_module_imports_only_math_and_typing(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert imported <= {"__future__", "math", "typing"}, imported
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in {"open", "print", "exec", "eval"}]

    def test_config_is_documented_as_subjective_and_budgets_are_ordered(self):
        assert "主观" in MODULE.read_text(encoding="utf-8")
        b = TB.CONFIG["shadow_budgets_pct"]
        assert list(b) == sorted(b) and all(x > 0 for x in b)
