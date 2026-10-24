"""逐票 Delta 对冲决策核心（v0.45.526，`delta_hedge.py`）的守卫。纯函数，离线。

每组回答一句「谁会红」：
  · 规则算术：带 / 拉回 0 / 拉回带边 / 整股取整，用手工数字独立复算（`TestRules`）；
  · 跨式没了清仓、缺数据逐标的判、坏持仓不当 0（`TestExitAndUnknown`）；
  · 原因码唯一一张表：写出的每个码都登记、表里无死键（AST 双向，`TestReasonTable`）；
  · 纯函数：不 import 对冲引擎、不读写文件、不联网（`TestPurity`）；
  · 成本估算（v0.45.638）：只加字段、不改任何决策；坏 bps 当场红；未决 / 带内 / 无仓不算「免费成交」（`TestCosts`）。
"""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

import delta_hedge as D

REPO = Path(__file__).resolve().parent.parent
NAV = 200_000.0


def _mark(px, at_close=True, stale=False):
    return {"price": None if stale else px, "seen_price": px, "stale": stale, "session": "2026-10-15",
            "source": "cboe_close", "at_close": at_close}


def _st(dd, reason=None):
    return {"dd": dd, "reason": reason}


class TestRules:
    def test_round_away_is_symmetric(self):
        # Python 的 round 是银行家舍入：round(0.5)=0、round(1.5)=2、round(-0.5)=0，正负不对称
        assert [D.round_away(x) for x in (0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 0.49, -0.49, 0.0)] == [1, 2, 3, -1, -2, -3, 0, 0, 0]

    def test_inside_band_does_nothing(self):
        d = D.decide({"X": _st(5_000.0)}, {}, {"X": _mark(100.0)}, nav=NAV, band_pct=3.0)[0]     # 带 = 6,000
        assert d["status"] == "inside" and d["trade_shares"] == 0 and d["breach"] is False
        assert d["target_shares"] == -50          # 无状态目标照样给：全抵掉要持有 −50 股

    def test_breach_center_goes_to_zero(self):
        d = D.decide({"X": _st(20_000.0)}, {}, {"X": _mark(100.0)}, nav=NAV, band_pct=3.0)[0]
        assert d["status"] == "hedge" and d["trade_shares"] == -200 and d["action"] == "sell"
        assert d["net_pct_nav"] == pytest.approx(10.0)

    def test_net_includes_existing_hedge(self):
        """已持有 −150 股（$−15,000）：净 = 20,000 − 15,000 = 5,000 在带内（带 6,000）⇒ 不动。"""
        d = D.decide({"X": _st(20_000.0)}, {"X": -150.0}, {"X": _mark(100.0)}, nav=NAV, band_pct=3.0)[0]
        assert d["status"] == "inside" and d["net_dd"] == pytest.approx(5_000.0) and d["trade_shares"] == 0
        d = D.decide({"X": _st(20_000.0)}, {"X": -100.0}, {"X": _mark(100.0)}, nav=NAV, band_pct=3.0)[0]
        assert d["status"] == "hedge" and d["trade_shares"] == -100        # 净 10,000 > 6,000 ⇒ 再卖 100 股回 0

    def test_breach_edge_pulls_back_to_the_band_edge_only(self):
        d = D.decide({"X": _st(20_000.0)}, {}, {"X": _mark(100.0)}, nav=NAV, band_pct=3.0, rebalance_to="edge")[0]
        assert d["trade_shares"] == -math.ceil((20_000 - 6_000) / 100)      # 向外取整：不留半股在带外
        d = D.decide({"X": _st(-20_000.0)}, {}, {"X": _mark(100.0)}, nav=NAV, band_pct=3.0, rebalance_to="edge")[0]
        assert d["trade_shares"] == +140 and d["action"] == "buy"

    def test_edge_rounds_outward_when_the_shares_do_not_divide_evenly(self):
        """超出带边 $14,000、价 30 ⇒ 466.67 股：向外取整 467（截断成 466 会留 $20 在带外，第二天再来一笔 1 股的小交易）。"""
        d = D.decide({"X": _st(20_000.0)}, {}, {"X": _mark(30.0)}, nav=NAV, band_pct=3.0, rebalance_to="edge")[0]
        assert d["trade_shares"] == -467
        after_net = 20_000.0 + d["trade_shares"] * 30.0
        assert abs(after_net) <= 6_000.0 + 1e-9                      # 成交后确实回到带内

    def test_bad_rebalance_to_raises(self):
        with pytest.raises(ValueError):
            D.decide({}, {}, {}, nav=NAV, band_pct=3.0, rebalance_to="middle")

    def test_deterministic(self):
        args = ({"A": _st(9_000.0), "B": _st(-30_000.0)}, {"B": 10.0}, {"A": _mark(50.0), "B": _mark(20.0)})
        assert D.decide(*args, nav=NAV, band_pct=3.0) == D.decide(*args, nav=NAV, band_pct=3.0)

    def test_straddle_delta_sums_both_legs_and_flags_unknown(self):
        rows = [{"ticker": "X", "symbol": "XC", "dollar_delta": 12_000.0}, {"ticker": "X", "symbol": "XP", "dollar_delta": -4_000.0},
                {"ticker": "Y", "symbol": "YC", "dollar_delta": 1_000.0}, {"ticker": "Y", "symbol": "YP", "dollar_delta": None}]
        out = D.straddle_delta_by_name(rows)
        assert out["X"]["dd"] == 8_000.0 and out["X"]["reason"] is None
        assert out["Y"]["dd"] is None and out["Y"]["reason"] == "option_delta_unknown" and out["Y"]["unknown_symbols"] == ["YP"]


class TestExitAndUnknown:
    def test_straddle_gone_clears_the_hedge_regardless_of_band_or_rebalance_mode(self):
        for mode in D.REBALANCE_TO:
            d = D.decide({}, {"X": -30.0}, {"X": _mark(100.0)}, nav=NAV, band_pct=3.0, rebalance_to=mode)[0]
            assert d["status"] == "exit" and d["trade_shares"] == 30 and d["has_straddle"] is False
            assert d["target_shares"] == 0

    def test_no_straddle_no_holding_is_flat(self):
        assert D.decide({}, {"X": 0.0}, {}, nav=NAV, band_pct=3.0) == []        # 空持仓行不产生决策

    def test_unknown_is_per_name_and_does_not_infect_others(self):
        out = {d["ticker"]: d for d in D.decide(
            {"A": _st(20_000.0), "B": _st(None, "option_delta_unknown"), "C": _st(20_000.0)}, {},
            {"A": _mark(100.0), "B": _mark(100.0), "C": _mark(100.0, stale=True)}, nav=NAV, band_pct=3.0)}
        assert out["A"]["status"] == "hedge"
        assert out["B"]["status"] == "unknown" and out["B"]["reason"] == "option_delta_unknown" and out["B"]["trade_shares"] == 0
        assert out["C"]["status"] == "unknown" and out["C"]["reason"] == "price_stale"

    def test_missing_price_and_nav(self):
        d = D.decide({"X": _st(20_000.0)}, {}, {"X": _mark(None, stale=False) | {"price": None}}, nav=NAV, band_pct=3.0)[0]
        assert d["status"] == "unknown" and d["reason"] == "price_missing"
        d = D.decide({"X": _st(20_000.0)}, {}, {"X": _mark(100.0)}, nav=None, band_pct=3.0)[0]
        assert d["status"] == "unknown" and d["reason"] == "nav_unavailable"

    def test_unreadable_holding_is_unknown_not_zero(self):
        """持仓账里某行 shares 读不出来（None / NaN）：按 0 算会把「有仓」当「没仓」再多买一遍。"""
        for bad in (None, float("nan")):
            out = D.decide({"X": _st(20_000.0)}, {"X": bad}, {"X": _mark(100.0)}, nav=NAV, band_pct=3.0)
            assert [d["status"] for d in out] == ["unknown"] and out[0]["reason"] == "held_shares_unknown"
            assert out[0]["trade_shares"] == 0
        out = D.decide({}, {"GONE": None}, {"GONE": _mark(100.0)}, nav=NAV, band_pct=3.0)
        assert out and out[0]["status"] == "unknown"       # 没有跨式、持仓又读不出 ⇒ 也不能当 flat 略过

    def test_summarize_counts_and_lists_undecided(self):
        out = D.decide({"A": _st(20_000.0), "B": _st(2_000.0), "C": _st(None, "option_delta_unknown")}, {"OLD": 5.0},
                       {"A": _mark(100.0), "B": _mark(100.0), "C": _mark(100.0), "OLD": _mark(10.0)}, nav=NAV, band_pct=3.0)
        s = D.summarize(out)
        assert (s["n_hedge"], s["n_inside"], s["n_undecided"]) == (2, 1, 1)        # A 要对冲 + OLD 清仓
        assert s["undecided"][0]["ticker"] == "C" and s["undecided"][0]["detail"] == D.REASONS["option_delta_unknown"]


class TestCosts:
    def _dec(self):
        return D.decide({"A": _st(20_000.0), "B": _st(2_000.0), "C": _st(None, "option_delta_unknown")}, {"OLD": 5.0},
                        {"A": _mark(100.0), "B": _mark(100.0), "C": _mark(100.0), "OLD": _mark(10.0)}, nav=NAV, band_pct=3.0)

    def test_notional_and_cost_for_hedge_and_exit_only(self):
        out = D.with_costs(self._dec(), 5.0)
        by = {d["ticker"]: d for d in out}
        assert by["A"]["trade_notional_usd"] == 20_000.0 and by["A"]["est_cost_usd"] == 10.0      # 200 股 × 100 × 5 bps
        assert by["OLD"]["status"] == "exit" and by["OLD"]["trade_notional_usd"] == 50.0 and by["OLD"]["est_cost_usd"] == 0.03
        for t in ("B", "C"):                                          # 带内 / 未决：没有成交 ⇒ 名义 0，不是「免费成交」
            assert by[t]["trade_notional_usd"] == 0.0 and by[t]["est_cost_usd"] == 0.0

    def test_only_the_status_decides_whether_a_trade_counts(self):
        """决策里就算带着非零 trade_shares，只要 status 不是 hedge / exit（带内 / 未决 / 无仓）就不算成交——名义由 status 把关，
        不靠「这些状态的 trade_shares 碰巧是 0」。"""
        fake = [{"ticker": t, "status": st, "trade_shares": 7, "price": 10.0} for t, st in
                (("I", "inside"), ("U", "unknown"), ("F", "flat"), ("H", "hedge"), ("E", "exit"))]
        by = {d["ticker"]: d["trade_notional_usd"] for d in D.with_costs(fake, 5.0)}
        assert by == {"I": 0.0, "U": 0.0, "F": 0.0, "H": 70.0, "E": 70.0}

    def test_costs_never_change_a_decision(self):
        plain = self._dec()
        costed = D.with_costs(self._dec(), 500.0)
        def strip(ds):
            return [{k: v for k, v in d.items() if k not in ("trade_notional_usd", "est_cost_usd")} for d in ds]
        assert strip(costed) == plain

    def test_summarize_totals_and_percent_of_nav(self):
        s = D.summarize(D.with_costs(self._dec(), 5.0), nav=NAV)
        assert s["n_trades"] == 2 and s["trade_notional_usd"] == 20_050.0 and s["est_cost_usd"] == 10.03
        assert s["trade_notional_pct_nav"] == pytest.approx(10.025) and s["est_cost_bps_nav"] == pytest.approx(0.5015)
        assert D.summarize(D.with_costs(self._dec(), 5.0))["trade_notional_pct_nav"] is None     # 没给 NAV ⇒ None，不拿 0 顶

    def test_summarize_without_costs_keeps_the_old_shape(self):
        assert "est_cost_usd" not in D.summarize(self._dec()) and "n_trades" not in D.summarize(self._dec())

    def test_zero_bps_is_zero_cost_but_not_zero_notional(self):
        s = D.summarize(D.with_costs(self._dec(), 0.0), nav=NAV)
        assert s["est_cost_usd"] == 0.0 and s["trade_notional_usd"] == 20_050.0

    @pytest.mark.parametrize("bad", [None, "5", float("nan"), float("inf"), -1.0, True])
    def test_bad_cost_bps_raises(self, bad):
        with pytest.raises(ValueError):
            D.check_cost_bps(bad)
        with pytest.raises(ValueError):
            D.with_costs(self._dec(), bad)

    def test_zero_is_accepted(self):
        assert D.check_cost_bps(0) == 0.0 and D.check_cost_bps(5) == 5.0


class TestReasonTable:
    SRC = (REPO / "delta_hedge.py").read_text(encoding="utf-8")

    def test_every_written_code_is_registered_and_none_are_dead(self):
        tree = ast.parse(self.SRC)
        strings = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name in ("decide", "straddle_delta_by_name"):
                strings |= {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        suspects = {s for s in strings if s.endswith(("_unknown", "_unavailable", "_missing", "_stale"))}
        assert suspects <= set(D.REASONS), f"写出了没登记的原因码：{sorted(suspects - set(D.REASONS))}"
        assert set(D.REASONS) <= strings, f"登记了但没人写的死键：{sorted(set(D.REASONS) - strings)}"

    def test_scan_has_teeth(self):
        fake = 'def decide():\n    why = "brand_new_missing"\n'
        strings = {n.value for n in ast.walk(ast.parse(fake)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert {s for s in strings if s.endswith("_missing")} - set(D.REASONS)


class TestPurity:
    def test_no_engine_import_no_io_no_network(self):
        tree = ast.parse((REPO / "delta_hedge.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not mods & {"portfolio_greeks", "ledger_io", "os", "pathlib", "socket", "requests", "urllib", "subprocess",
                           "shutil", "json", "hive_logger"}, mods
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"open", "print", "exec", "eval"}, calls
