"""接货型卖 put 纸面账本（v0.45.720，`sell_put_wheel.py`）的守卫。离线：期权链 / 收盘一律注入，账本在 tmp。

每组回答一句「谁会红」：
  · 选合约的纯规则：行权价 = 不高于接货价的最高合格档、点差 / OI / 无 bid 各有原因码（`TestPickPut` / `TestPickCall`）；
  · 到期边界：收盘 == 行权价 ⇒ 不接货；== call 行权价 ⇒ 不被叫走（`TestSettleBoundaries`）；
  · 账本算术：现金 / 持股 / 负债 / 净值用手工数字独立复算，一圈的盈亏（`TestLifecycle`）；
  · 到期当天不结、取不到那一场收盘就延期、**绝不拿前一天的价顶替**（`TestSettlementTiming`）；
  · 额度：单只 / 总担保上限、同时可开多只时按年化权利金从高到低（`TestCaps`）；
  · 同日重跑逐字节幂等、坏 meta 抛、非有限数进不了账本（`TestLedgerSafety`）；
  · 原因码唯一一张表：写出的每个码都登记、表里无死键（`TestReasonTable`）。
"""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pytest

import ledger_io
import sell_put_wheel as W

REPO = Path(__file__).resolve().parent.parent
D0 = "2026-11-04"          # 开仓日
PUT_EXP = "2027-01-15"     # DTE 72（窗口 55~90 内）
CALL_EXP = "2026-12-11"    # 假定接货后的下一张 call 的到期日


@pytest.fixture(autouse=True)
def pinned_config(monkeypatch):
    """把 CONFIG 钉成测试要的值（缺省值改了测试不该跟着变）。"""
    cfg = dict(W.CONFIG)
    cfg.update(universe=["AAA"], accept_price={"AAA": None}, placeholder_otm_pct=10.0, starting_capital=100_000.0,
               contracts=1, per_name_cap_pct=20.0, total_cap_pct=60.0, max_spread_pct=0.15, min_oi=100,
               put_dte_range=(55, 90), put_dte_target=70, call_dte_range=(25, 50), call_dte_target=35,
               call_target_delta=0.25)
    monkeypatch.setattr(W, "CONFIG", cfg)
    return cfg


def put(strike, bid, ask=None, oi=500, exp=PUT_EXP, delta=-0.2, sym=None):
    ask = ask if ask is not None else round(bid * 1.04, 2)
    return {"symbol": sym or f"AAA{exp.replace('-', '')}P{int(strike * 1000):08d}", "expiry": exp, "cp": "P",
            "strike": float(strike), "bid": bid, "ask": ask, "oi": oi, "delta": delta}


def call(strike, bid, ask=None, oi=500, exp=CALL_EXP, delta=0.25, sym=None):
    ask = ask if ask is not None else round(bid * 1.04, 2)
    return {"symbol": sym or f"AAA{exp.replace('-', '')}C{int(strike * 1000):08d}", "expiry": exp, "cp": "C",
            "strike": float(strike), "bid": bid, "ask": ask, "oi": oi, "delta": delta}


def chain(spot, contracts):
    return {"underlying_price": spot, "contracts": contracts}


def contracts_for(table):
    """table: {日期: {ticker: chain | None}}，没登记的日期沿用最近一个更早的。"""
    def fn(tk, as_of):
        days = sorted(d for d in table if d <= as_of)
        c = (table[days[-1]] if days else {}).get(tk)
        return (c, None) if c else (None, "unavailable")
    return fn


def closes_for(table):
    """table: {结算日: 收盘}；返回自报场次的 dict（与 `_default_close` 同形）。"""
    def fn(tk, settle_date, as_of=None):
        px = table.get(settle_date)
        return {"price": px, "session": settle_date if px is not None else None, "source": "injected",
                "reason": None if px is not None else "bars_unavailable"}
    return fn


STD_PUTS = [put(90, 2.0), put(85, 1.2), put(95, 3.0, ask=3.1)]


# ───────────────────────────── 选合约 ─────────────────────────────

class TestPickPut:
    def test_highest_qualified_strike_at_or_below_the_accept_price(self):
        c, why, info = W.pick_put(STD_PUTS, 100.0, 92.0, D0)
        assert c["strike"] == 90.0 and why is None
        assert info == {"accept_price": 92.0, "accept_price_source": "user"}

    def test_strike_equal_to_accept_price_is_allowed(self):
        c, _, _ = W.pick_put(STD_PUTS, 100.0, 90.0, D0)
        assert c["strike"] == 90.0

    def test_placeholder_uses_spot_less_ten_percent_and_says_so(self):
        c, why, info = W.pick_put(STD_PUTS, 100.0, None, D0)
        assert c["strike"] == 90.0 and why is None
        assert info == {"accept_price": 90.0, "accept_price_source": "placeholder"}

    def test_walks_down_when_the_top_strike_is_not_tradable(self):
        cs = [put(90, 2.0, ask=3.5), put(85, 1.2)]          # 90 点差 54% ⇒ 往下找 85
        c, _, _ = W.pick_put(cs, 100.0, 92.0, D0)
        assert c["strike"] == 85.0

    @pytest.mark.parametrize("bad, code", [
        (put(90, 0.0, ask=0.2), "no_bid"),
        (put(90, 2.0, ask=1.0), "bad_quote"),               # ask < bid
        (put(90, 2.0, ask=3.0), "wide_spread"),             # 点差 40%
        (put(90, 2.0, oi=50), "low_oi"),
    ])
    def test_reason_codes_when_nothing_qualifies(self, bad, code):
        c, why, _ = W.pick_put([bad], 100.0, 92.0, D0)
        assert c is None and why == code

    def test_no_strike_at_or_below_accept(self):
        assert W.pick_put([put(95, 3.0)], 100.0, 90.0, D0)[1] == "no_strike"

    def test_no_expiry_in_window(self):
        assert W.pick_put([put(90, 2.0, exp="2026-12-04")], 100.0, 92.0, D0)[1] == "no_expiry"   # DTE 30 < 55

    def test_nearest_expiry_to_target_and_later_one_on_a_tie(self):
        a, b = put(90, 2.0, exp="2027-01-08"), put(90, 2.0, exp="2027-01-22")   # DTE 65 / 79：距目标 70 是 5 / 9
        assert W.pick_put([a, b], 100.0, 92.0, D0)[0]["expiry"] == "2027-01-08"
        c, d = put(90, 2.0, exp="2027-01-11"), put(90, 2.0, exp="2027-01-19")   # 68 / 76：距离 2 / 6
        assert W.pick_put([c, d], 100.0, 92.0, D0)[0]["expiry"] == "2027-01-11"
        e, f = put(90, 2.0, exp="2027-01-10"), put(90, 2.0, exp="2027-01-14")   # 67 / 71：距离 3 / 1
        assert W.pick_put([e, f], 100.0, 92.0, D0)[0]["expiry"] == "2027-01-14"

    def test_tie_goes_to_the_later_expiry(self):
        e, f = put(90, 2.0, exp="2027-01-12"), put(90, 2.0, exp="2027-01-16")   # 69 / 73 → 距离 1 / 3，不是平局
        g, h = put(90, 2.0, exp="2027-01-12"), put(90, 2.0, exp="2027-01-14")   # 69 / 71 → 距离都是 1
        assert W.pick_put([g, h], 100.0, 92.0, D0)[0]["expiry"] == "2027-01-14"
        assert W.pick_put([e, f], 100.0, 92.0, D0)[0]["expiry"] == "2027-01-12"


class TestPickCall:
    CALLS = [call(100, 4.0, delta=0.45), call(105, 2.0, delta=0.27), call(110, 1.0, delta=0.12)]

    def test_nearest_delta_to_target_among_strikes_at_or_above_the_floor(self):
        c, why = W.pick_call(self.CALLS, 95.0, D0)
        assert c["strike"] == 105.0 and why is None

    def test_floor_excludes_lower_strikes_even_when_their_delta_is_closer(self):
        c, _ = W.pick_call(self.CALLS, 108.0, D0)
        assert c["strike"] == 110.0

    def test_floor_equal_to_a_strike_includes_it(self):
        c, _ = W.pick_call(self.CALLS, 105.0, D0)
        assert c["strike"] == 105.0

    def test_no_call_at_or_above_floor(self):
        assert W.pick_call(self.CALLS, 120.0, D0) == (None, "no_call_strike")

    def test_missing_delta_is_a_reason_not_a_guess(self):
        cs = [{**call(105, 2.0), "delta": None}]
        assert W.pick_call(cs, 95.0, D0) == (None, "no_delta")

    def test_illiquid_calls_are_reported_by_their_reason(self):
        assert W.pick_call([call(105, 2.0, oi=10)], 95.0, D0) == (None, "low_oi")

    def test_no_expiry_in_window(self):
        assert W.pick_call([call(105, 2.0, exp="2027-03-19")], 95.0, D0) == (None, "no_expiry")


class TestSettleBoundaries:
    def test_close_equal_to_put_strike_is_not_assigned(self):
        assert W.settle_put(90.0, 90.0) == "expired"
        assert W.settle_put(90.0, 89.99) == "assigned"

    def test_close_equal_to_call_strike_is_not_called_away(self):
        assert W.settle_call(105.0, 105.0) == "expired"
        assert W.settle_call(105.0, 105.01) == "called_away"

    def test_annual_yield_is_cash_secured_basis(self):
        assert W.annual_yield(2.0, 90.0, 72) == pytest.approx(2.0 / 90.0 * 365 / 72 * 100)

    def test_mark_needs_a_two_sided_quote(self):
        assert W.mark_of(put(90, 2.0, ask=2.2)) == pytest.approx(2.1)
        assert W.mark_of(put(90, 0.0, ask=0.2)) is None
        assert W.mark_of(None) is None


# ───────────────────────────── 账本生命周期 ─────────────────────────────

def _state():
    return W.load_state()


def _lot():
    return {l["ticker"]: l for l in _state()["lots"]}


class TestLifecycle:
    def _open(self, extra=None):
        table = {D0: {"AAA": chain(100.0, STD_PUTS + (extra or []))}}
        r = W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        return table, r

    def test_day_one_sells_the_put_at_bid_and_books_the_premium(self):
        _, r = self._open()
        assert [e["event"] for e in r["events"]] == ["put_sold"]
        e = r["events"][0]
        assert e["strike"] == 90.0 and e["premium"] == 2.0 and e["accept_price_source"] == "placeholder"
        assert e["annual_yield_pct"] == pytest.approx(round(W.annual_yield(2.0, 90.0, 72), 2))
        st = _state()
        assert st["meta"]["cash"] == 100_200.0                       # 100000 + 2.00 × 100
        row = st["equity"][-1]
        # 净值 = 现金 − 负债（mid = (2.0 + 2.08)/2 = 2.04）→ 100200 − 204 = 99996
        assert row["nav"] == pytest.approx(99_996.0) and row["reserved_puts"] == 9_000.0
        assert _lot()["AAA"]["state"] == "put"

    def test_one_lot_per_name_no_second_put_while_one_is_open(self):
        table, _ = self._open()
        r2 = W.run_for_date("2026-11-05", contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        assert r2["events"] == [] and len(_state()["lots"]) == 1

    def test_expired_worthless_keeps_the_premium_and_frees_the_name(self):
        table, _ = self._open()
        d1 = "2027-01-19"
        nxt = [put(90, 1.5, exp="2027-03-19")]                          # 新一轮：DTE 59，在窗口内
        t2 = {**table, d1: {"AAA": chain(105.0, nxt)}}
        r = W.run_for_date(d1, contracts_fn=contracts_for(t2), closes_fn=closes_for({PUT_EXP: 104.0}))
        assert [e["event"] for e in r["events"]] == ["put_expired", "put_sold"]     # 收盘 104 > 90 ⇒ 作废；同一轮无仓 ⇒ 重开
        assert r["events"][0]["premium"] == 2.0 and r["events"][1]["premium"] == 1.5
        # 现金：100000 + 200（第一张权利金，作废不退）+ 150（第二张）；作废没有任何现金流
        assert _state()["meta"]["cash"] == pytest.approx(100_000 + 200 + 150)
        assert _lot()["AAA"]["put"]["expiry"] == "2027-03-19"

    def test_assigned_buys_shares_at_strike_then_sells_a_call_above_the_floor(self):
        table, _ = self._open()
        d1 = "2027-01-19"
        calls = [call(85, 6.0, delta=0.60, exp="2027-02-16"), call(93, 3.0, delta=0.27, exp="2027-02-16"),
                 call(100, 1.0, delta=0.10, exp="2027-02-16")]
        t2 = {**table, d1: {"AAA": chain(86.0, calls)}}
        r = W.run_for_date(d1, contracts_fn=contracts_for(t2), closes_fn=closes_for({PUT_EXP: 88.0}))
        evs = {e["event"]: e for e in r["events"]}
        assert "put_assigned" in evs and evs["put_assigned"]["close"] == 88.0
        # 接货成本 = 行权价 − 权利金 = 90 − 2 = 88；call 行权价必须 ≥ 88，且 delta 最接近 0.25 ⇒ 93
        assert evs["call_sold"]["strike"] == 93.0 and evs["call_sold"]["basis_floor"] == 88.0
        lot = _lot()["AAA"]
        assert lot["state"] == "shares" and lot["shares"]["n"] == 100 and lot["call"]["strike"] == 93.0
        # 现金：100000 + 200（put 权利金）− 9000（接货）+ 300（call 权利金）
        assert _state()["meta"]["cash"] == pytest.approx(100_000 + 200 - 9_000 + 300)

    def test_called_away_closes_the_cycle_with_the_exact_pnl(self):
        table, _ = self._open()
        d1 = "2027-01-19"
        calls = [call(93, 3.0, delta=0.27, exp="2027-02-16")]
        t2 = {**table, d1: {"AAA": chain(86.0, calls)}}
        W.run_for_date(d1, contracts_fn=contracts_for(t2), closes_fn=closes_for({PUT_EXP: 88.0}))
        d2 = "2027-02-17"
        r = W.run_for_date(d2, contracts_fn=contracts_for({**t2, d2: {"AAA": chain(97.0, [])}}),
                           closes_fn=closes_for({"2027-02-16": 97.0}))
        ev = next(e for e in r["events"] if e["event"] == "call_called_away")
        # 一圈盈亏 = (93 − 90) × 100 + (2.0 + 3.0) × 100 = 300 + 500 = 800
        assert ev["cycle_pnl_usd"] == pytest.approx(800.0)
        assert "AAA" not in _lot() or _lot()["AAA"]["state"] == "put"      # 同一轮若有合格 put 会重开；这里链为空 ⇒ 无仓
        # 现金：100000 + 200 − 9000 + 300 + 9300（按 93 被叫走）
        assert _state()["meta"]["cash"] == pytest.approx(100_000 + 200 - 9_000 + 300 + 9_300)

    def test_call_expired_keeps_the_shares_and_sells_the_next_call(self):
        table, _ = self._open()
        d1 = "2027-01-19"
        c1 = [call(93, 3.0, delta=0.27, exp="2027-02-16")]
        t2 = {**table, d1: {"AAA": chain(86.0, c1)}}
        W.run_for_date(d1, contracts_fn=contracts_for(t2), closes_fn=closes_for({PUT_EXP: 88.0}))
        d2 = "2027-02-17"
        c2 = [call(92, 2.5, delta=0.25, exp="2027-03-19")]
        r = W.run_for_date(d2, contracts_fn=contracts_for({**t2, d2: {"AAA": chain(89.0, c2)}}),
                           closes_fn=closes_for({"2027-02-16": 90.0}))
        names = [e["event"] for e in r["events"]]
        assert names == ["call_expired", "call_sold"]
        assert _lot()["AAA"]["call"]["strike"] == 92.0 and _lot()["AAA"]["premiums"] == pytest.approx(2.0 + 3.0 + 2.5)

    def test_nav_identity_holds_with_shares_and_a_short_call(self):
        table, _ = self._open()
        d1 = "2027-01-19"
        c1 = [call(93, 3.0, ask=3.2, delta=0.27, exp="2027-02-16")]
        t2 = {**table, d1: {"AAA": chain(86.0, c1)}}
        r = W.run_for_date(d1, contracts_fn=contracts_for(t2), closes_fn=closes_for({PUT_EXP: 88.0}))
        row = _state()["equity"][-1]
        cash = 100_000 + 200 - 9_000 + 300
        # 持股 100 × 86、call 负债 mid 3.1 × 100
        assert row["nav"] == pytest.approx(cash + 8_600 - 310) and r["nav"] == row["nav"]


# ───────────────────────────── 结算时点 ─────────────────────────────

class TestSettlementTiming:
    def _open(self):
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        return table

    def test_not_settled_on_the_expiry_day_itself(self):
        table = self._open()
        r = W.run_for_date(PUT_EXP, contracts_fn=contracts_for({**table, PUT_EXP: {"AAA": chain(80.0, [])}}),
                           closes_fn=closes_for({PUT_EXP: 80.0}))
        assert [e["event"] for e in r["events"]] == [] and _lot()["AAA"]["state"] == "put"

    def test_missing_close_defers_and_names_the_reason_without_touching_cash(self):
        table = self._open()
        cash0 = _state()["meta"]["cash"]
        r = W.run_for_date("2027-01-19", contracts_fn=contracts_for({**table, "2027-01-19": {"AAA": chain(80.0, [])}}),
                           closes_fn=closes_for({}))
        assert r["deferred"] == [{"ticker": "AAA", "leg": "put", "expiry": PUT_EXP, "reason": "bars_unavailable"}]
        assert _state()["meta"]["cash"] == cash0
        assert _lot()["AAA"]["put"]["settle_deferred_since"] == "2027-01-19"

    def test_a_close_from_the_wrong_session_is_never_used(self):
        table = self._open()

        def prior_day_close(tk, settle_date, as_of=None):
            return {"price": 80.0, "session": "2027-01-14", "source": "x", "reason": None}   # 前一天的价
        r = W.run_for_date("2027-01-19", contracts_fn=contracts_for({**table, "2027-01-19": {"AAA": chain(80.0, [])}}),
                           closes_fn=prior_day_close)
        assert r["deferred"] and r["deferred"][0]["reason"] == "wrong_session"
        assert _lot()["AAA"]["state"] == "put"                      # 没有按 80 接货

    def test_deferred_name_gets_no_new_call_or_put_that_night(self):
        table = self._open()
        W.run_for_date("2027-01-19", contracts_fn=contracts_for({**table, "2027-01-19": {"AAA": chain(80.0, STD_PUTS)}}),
                       closes_fn=closes_for({}))
        assert _lot()["AAA"]["state"] == "put" and len(_state()["events"]) == 1       # 只有开仓那一条

    def test_deferred_then_settled_on_a_later_run(self):
        table = self._open()
        t = {**table, "2027-01-19": {"AAA": chain(80.0, [])}, "2027-01-20": {"AAA": chain(80.0, [])}}
        W.run_for_date("2027-01-19", contracts_fn=contracts_for(t), closes_fn=closes_for({}))
        r = W.run_for_date("2027-01-20", contracts_fn=contracts_for(t), closes_fn=closes_for({PUT_EXP: 80.0}))
        assert [e["event"] for e in r["events"]] == ["put_assigned"]
        assert "settle_deferred_since" not in (_lot()["AAA"].get("put") or {})


# ───────────────────────────── 额度与排序 ─────────────────────────────

class TestCaps:
    def test_per_name_cap_blocks_an_oversized_strike(self, pinned_config):
        pinned_config["per_name_cap_pct"] = 5.0                       # 5% × 10 万 = 5000 < 9000
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        r = W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        assert r["events"] == [] and r["skipped"][0]["reason"] == "per_name_cap"

    def test_total_cap_blocks_the_name_that_does_not_fit(self, pinned_config):
        pinned_config.update(universe=["AAA", "BBB"], accept_price={"AAA": None, "BBB": None}, total_cap_pct=12.0,
                             per_name_cap_pct=20.0)
        bbb = [{**p, "symbol": p["symbol"].replace("AAA", "BBB")} for p in STD_PUTS]
        table = {D0: {"AAA": chain(100.0, STD_PUTS), "BBB": chain(100.0, bbb)}}
        r = W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        assert sum(e["event"] == "put_sold" for e in r["events"]) == 1                # 9000 ≤ 12000，再加 9000 超
        assert any(s["reason"] == "total_cap" for s in r["skipped"])

    def test_higher_annual_yield_gets_the_scarce_budget_first(self, pinned_config):
        pinned_config.update(universe=["AAA", "BBB"], accept_price={"AAA": None, "BBB": None}, total_cap_pct=12.0)
        rich = [{**put(90, 4.0), "symbol": "BBB270115P00090000"}]                # BBB 权利金高一倍
        table = {D0: {"AAA": chain(100.0, [put(90, 2.0)]), "BBB": chain(100.0, rich)}}
        r = W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        sold = [e["ticker"] for e in r["events"] if e["event"] == "put_sold"]
        assert sold == ["BBB"]                                                    # 名额只够一只 ⇒ 年化高的先开

    def test_user_accept_price_overrides_the_placeholder(self, pinned_config):
        pinned_config["accept_price"] = {"AAA": 86.0}
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        r = W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        e = r["events"][0]
        assert e["strike"] == 85.0 and e["accept_price_source"] == "user" and e["accept_price"] == 86.0


# ───────────────────────────── 账本安全 ─────────────────────────────

class TestLedgerSafety:
    def _files(self):
        d = W._state_dir()
        return {p.name: p.read_bytes() for p in sorted(d.glob("*"))}

    def test_same_day_rerun_is_byte_identical(self):
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        first = self._files()
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        assert self._files() == first

    def test_rerun_after_a_crash_between_events_and_the_rest_does_not_duplicate_events(self):
        """事件先落盘、持仓 / 净值 / meta 后写：中途崩了再跑，同一笔事件会被重新生成——去重键必须挡住它。"""
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        d = W._state_dir()
        for name in ("lots.jsonl", "equity_curve.jsonl", "meta.json"):          # 模拟：事件写了，后面三份没写成
            (d / name).unlink()
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        evs = ledger_io.load_jsonl(d / "events.jsonl")
        assert [e["event"] for e in evs] == ["put_sold"]
        assert W.load_state()["meta"]["cash"] == 100_200.0

    def test_chain_failure_is_recorded_not_swallowed(self):
        def boom(tk, as_of):
            raise RuntimeError("cboe down")
        r = W.run_for_date(D0, contracts_fn=boom, closes_fn=closes_for({}))
        assert r["skipped"][0]["reason"] == "no_chain" and "cboe down" in r["skipped"][0]["detail"]
        run = _state()["meta"]["last_run"]
        assert run["skipped"][0]["reason"] == "no_chain" and "AAA" in run["chain_errors"]

    def test_missing_state_dir_reads_as_unavailable_not_empty(self):
        st = W.load_state()
        assert st == {"available": False, "state_reason": "no_state_dir"}
        assert W.kpis()["available"] is False and "不可得" in W.render_markdown()

    def test_corrupt_meta_stops_the_run_instead_of_resetting_cash(self):
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        (W._state_dir() / "meta.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ledger_io.LedgerCorrupt):
            W.run_for_date("2026-11-05", contracts_fn=contracts_for(table), closes_fn=closes_for({}))

    def test_non_finite_numbers_never_reach_the_ledger(self):
        with pytest.raises(ValueError):
            W._scrub({"a": {"b": [1.0, float("nan")]}})
        bad = [{**put(90, 2.0), "bid": float("nan")}]
        r = W.run_for_date(D0, contracts_fn=contracts_for({D0: {"AAA": chain(100.0, bad)}}), closes_fn=closes_for({}))
        assert r["events"] == [] and r["skipped"][0]["reason"] == "no_bid"
        for f in W._state_dir().glob("*.jsonl"):
            assert "NaN" not in f.read_text(encoding="utf-8")

    def test_kpis_and_markdown_describe_the_ledger(self):
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        k = W.kpis()
        assert k["n_put_sold"] == 1 and k["premium_collected_usd"] == 200.0 and k["n_assigned"] == 0
        md = W.render_markdown()
        assert "只记录，非检验" in md and "AAA" in md


# ───────────────────────────── 原因码 ─────────────────────────────

class TestReasonTable:
    @staticmethod
    def _written_codes():
        tree = ast.parse((REPO / "sell_put_wheel.py").read_text(encoding="utf-8"))
        codes = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple) and len(n.value.elts) >= 2:
                e = n.value.elts[1]
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    codes.add(e.value)
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value == "reason" and isinstance(v, ast.Constant) and isinstance(v.value, str):
                        codes.add(v.value)
            if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
                for v in n.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value in W.SKIP_REASONS:
                        codes.add(v.value)
        return codes

    def test_every_written_code_is_registered_and_none_are_dead(self):
        written = self._written_codes()
        assert written, "扫描空转了"
        assert written <= set(W.SKIP_REASONS), written - set(W.SKIP_REASONS)
        dead = set(W.SKIP_REASONS) - written
        assert not dead, f"登记了却没人写的原因码：{dead}"

    def test_scan_has_teeth(self):
        src = 'def f():\n    return None, "zzz_code"\nx = {"reason": "yyy_code"}\n'
        tree = ast.parse(src)
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple):
                found.add(n.value.elts[1].value)
            if isinstance(n, ast.Dict):
                found.update(v.value for k, v in zip(n.keys, n.values) if k.value == "reason")
        assert found == {"zzz_code", "yyy_code"}


class TestPurity:
    def test_pick_functions_do_not_touch_disk_or_network(self):
        tree = ast.parse((REPO / "sell_put_wheel.py").read_text(encoding="utf-8"))
        pure = {"pick_expiry", "pick_put", "pick_call", "mark_of", "settle_put", "settle_call", "annual_yield",
                "_liquid", "_spread_pct", "_expiries"}
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in pure):
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            assert not names & {"open", "ledger_io", "PATHS", "requests", "urlopen", "load_jsonl", "write_json"}, (fn.name, names)

    def test_no_import_of_the_sell_strike_modules(self):
        """卖权选择器的火墙：只有 alphabot/service.py 可以 import sell_strike_*；本账本是另一套东西，不碰它。"""
        txt = (REPO / "sell_put_wheel.py").read_text(encoding="utf-8")
        assert "sell_strike" not in txt.replace("sell_strike_*", "").replace("`sell_strike_*`", "")
        assert json  # 保持 json 导入被用到（下面的测试会用）
        assert math


# ───────────────────────────── 失败登记与告警 ─────────────────────────────

class TestFailuresAreVisible:
    """日报钩子把整轮异常吞成一条 WARNING；谁会红？——源头计数 → scan_timing → alert_manager P2。"""

    def test_clean_run_counts_runs_and_chains(self):
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        s = W.stats()
        assert (s["runs"], s["run_errors"], s["chain_total"], s["chain_missing"]) == (1, 0, 1, 0)
        assert s["all_chains_missing"] is False and s["deferred_alert"] is False

    def test_all_chains_missing_is_flagged_but_one_missing_is_not(self, pinned_config):
        pinned_config.update(universe=["AAA", "BBB"], accept_price={"AAA": None, "BBB": None})
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}            # BBB 取不到
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        s = W.stats()
        assert s["chain_missing"] == 1 and s["all_chains_missing"] is False
        W.reset_stats()
        W.run_for_date("2026-11-05", contracts_fn=contracts_for({}), closes_fn=closes_for({}))
        s = W.stats()
        assert s["chain_total"] == 2 and s["chain_missing"] == 2 and s["all_chains_missing"] is True
        assert any(i["kind"] == "chain_missing" and "BBB" in i["detail"] for i in s["items"])

    def test_a_raising_run_is_counted_and_still_raises(self):
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        (W._state_dir() / "meta.json").write_text("{not json", encoding="utf-8")
        W.reset_stats()
        with pytest.raises(ledger_io.LedgerCorrupt):
            W.run_for_date("2026-11-05", contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        s = W.stats()
        assert s["run_errors"] == 1 and s["items"][0]["kind"] == "run_error" and "LedgerCorrupt" in s["items"][0]["detail"]

    def test_deferred_days_alert_only_from_three_calendar_days(self):
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        for day, expect in (("2027-01-19", False), ("2027-01-21", False), ("2027-01-22", True)):
            W.reset_stats()
            W.run_for_date(day, contracts_fn=contracts_for({**table, day: {"AAA": chain(80.0, [])}}), closes_fn=closes_for({}))
            assert W.stats()["deferred_alert"] is expect, day            # 自 01-19 起延期：01-22 满 3 天

    def test_scan_timing_exposes_the_counter(self):
        import scan_timing
        assert scan_timing.counters()["sell_put"]["runs"] == 0
        table = {D0: {"AAA": chain(100.0, STD_PUTS)}}
        W.run_for_date(D0, contracts_fn=contracts_for(table), closes_fn=closes_for({}))
        assert scan_timing.counters()["sell_put"]["runs"] == 1


def _analyze(tmp_path, counter, *, drop=False):
    import alert_manager as am
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": {"sell_put": counter}, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    if drop:
        del status["scan_timing"]["counters"]["sell_put"]
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return [x for x in a.alerts if "接货型卖 put" in x.message], a


class TestAlert:
    CLEAN = {"runs": 1, "run_errors": 0, "chain_total": 3, "chain_missing": 0, "all_chains_missing": False,
             "max_deferred_days": 0, "deferred_alert": False, "items": []}

    def test_clean_counter_raises_nothing(self, tmp_path):
        assert _analyze(tmp_path, self.CLEAN)[0] == []

    @pytest.mark.parametrize("change, phrase", [
        ({"run_errors": 1, "items": [{"kind": "run_error", "detail": "LedgerCorrupt: x"}]}, "整轮抛了"),
        ({"chain_missing": 3, "all_chains_missing": True}, "一只期权链都没取到"),
        ({"max_deferred_days": 3, "deferred_alert": True}, "结算已延期 3 天"),
    ])
    def test_each_failure_shape_is_a_p2(self, tmp_path, change, phrase):
        alerts, _ = _analyze(tmp_path, {**self.CLEAN, **change})
        import alert_manager as am
        assert len(alerts) == 1 and phrase in alerts[0].message and "P2" in alerts[0].message
        assert alerts[0].level == am.AlertLevel.MEDIUM

    def test_one_missing_chain_or_short_deferral_stays_quiet(self, tmp_path):
        quiet = {**self.CLEAN, "chain_missing": 1, "max_deferred_days": 2}
        assert _analyze(tmp_path, quiet)[0] == []

    def test_missing_counter_is_reported_as_skipped_not_as_clean(self, tmp_path):
        alerts, a = _analyze(tmp_path, None, drop=True)
        assert alerts == [] and any("sell_put" in s for s in a.checks_skipped)


class TestHookIsWired:
    def test_daily_report_calls_the_ledger_inside_a_non_fatal_try(self):
        src = (REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        i = src.index("import sell_put_wheel as _spw")
        block = src[i - 200:i + 1300]
        assert "_spw.run_for_date(self.date_str)" in block and "接货型卖 put 账本更新失败(非致命)" in block
