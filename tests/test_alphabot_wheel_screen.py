"""Alpha Bot「接货 · 筛选」（v0.45.722：`alphabot/wheel_screen.py` + `wheel_prices.py` + 三个端点 + 名单 / 接货价设置）的守卫。离线。

每组回答一句「谁会红」：
  · 行情统计 / 权利金重放：用**手工构造、答案能心算的序列**复算（`TestPriceStats` / `TestReplay`），不是把公式再抄一遍；
  · 账本梯子只读梯子与财报日、边界（财报恰在到期日算在内、恰在当天不算）、缺就是 None 不是 0（`TestLadder`）；
  · 选合约只有一份规则：与 `sell_put_wheel.pick_put` 选出同一张（`TestPickForAccept`）；
  · 设置校验：名单 / 接货价坏输入当场拒、None 清掉（`TestSettings`）；
  · 只并排事实：没有排名 / 推荐 / 综合分字段（`TestFactsOnly`）；
  · 取数：限流后不再逐只去撞、单只出错不连坐、日线缓存 6 小时（`TestPriceLoader` / `TestEndpoints`）；
  · 原因码唯一一张表：写出的每个码都登记、表里无死键（`TestReasonTable`）。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd
import pytest

import sell_put_wheel as W
from alphabot import wheel_prices as WP
from alphabot import wheel_screen as WS
from alphabot.service import AlphaBotService, BadRequest

REPO = Path(__file__).resolve().parent.parent


def _closes(values, start="2025-01-01"):
    days = pd.bdate_range(start=start, periods=len(values))
    return {str(d.date()): float(v) for d, v in zip(days, values)}


# ───────────────────────────── 行情统计 ─────────────────────────────

class TestPriceStats:
    # 100 ×100 天，第 101 天冲到 120，之后 39 天都是 89（共 140 个收盘）
    V = [100.0] * 100 + [120.0] + [89.0] * 39

    def test_hand_checkable_numbers(self):
        s = WS.price_stats(_closes(self.V))
        assert s["ok"] and s["n"] == 140 and s["last"] == 89.0
        assert s["dd52_pct"] == pytest.approx((89 / 120 - 1) * 100, abs=0.01)           # 最高 120 ⇒ −25.83%
        assert s["dd3m_pct"] == pytest.approx(-25.83, abs=0.01)                          # 近 63 日也含那根 120
        assert s["ret1m_pct"] == 0.0 and s["ret3m_pct"] == -11.0                         # v[-22] = 89、v[-64] = 100
        # 21 日窗口共 119 个：i=80..99（89/100 = −11%）20 个 + i=100（89/120 = −25.8%）1 个 ⇒ 21/119 ≤ −10%
        assert s["n_windows21"] == 119 and s["p21_le_m10_pct"] == pytest.approx(21 / 119 * 100, abs=0.05)
        assert s["worst21_pct"] == pytest.approx(-25.83, abs=0.01)

    def test_volatility_is_the_population_std_of_log_returns_annualised(self):
        rets = [0.0] * 139                              # 139 个日收益，只有两个非零
        rets[99] = math.log(120 / 100)                  # 第 100→101 天
        rets[100] = math.log(89 / 120)
        m = sum(rets) / 139
        expect = math.sqrt(sum((r - m) ** 2 for r in rets) / 139) * math.sqrt(252) * 100
        assert WS.price_stats(_closes(self.V))["vol1y_pct"] == pytest.approx(expect, abs=0.06)

    def test_three_month_high_is_not_the_one_year_high(self):
        s = WS.price_stats(_closes([120.0] + [100.0] * 139))            # 120 在 139 天前：只在 52 周窗口里，不在 63 日窗口里
        assert s["dd52_pct"] == pytest.approx(-16.67, abs=0.01) and s["dd3m_pct"] == 0.0

    def test_one_and_three_month_returns_use_the_right_lookback(self):
        s = WS.price_stats(_closes([float(i) for i in range(1, 141)]))   # 1..140：v[-22] = 119、v[-64] = 77
        assert s["ret1m_pct"] == pytest.approx((140 / 119 - 1) * 100, abs=0.01)
        assert s["ret3m_pct"] == pytest.approx((140 / 77 - 1) * 100, abs=0.01)

    def test_exactly_the_minimum_number_of_closes_is_enough(self):
        assert WS.price_stats(_closes([100.0] * WS.MIN_CLOSES))["ok"] is True

    def test_too_few_closes_gives_a_reason_not_a_number(self):
        s = WS.price_stats(_closes([100.0] * (WS.MIN_CLOSES - 1)))
        assert s == {"ok": False, "reason": "too_few_closes", "n": WS.MIN_CLOSES - 1}

    def test_bad_points_are_dropped_and_as_of_cuts_the_future(self):
        c = _closes(self.V)
        keys = sorted(c)
        c[keys[5]] = float("nan")
        c[keys[6]] = -3.0
        s = WS.price_stats(c, as_of=keys[-2])            # 最后一根被截掉
        assert s["n"] == 140 - 2 - 1 and s["last_date"] == keys[-2]


class TestReplay:
    V = [100.0] * 30 + [80.0] * 30              # 60 个收盘；dte=30 ⇒ 持有 round(30×252/365) = 21 个交易日

    def test_hand_checkable_net_returns(self):
        r = WS.replay_put(_closes(self.V), spot=100.0, strike=90.0, bid=2.0, dte=30)
        # 39 个窗口：i=0..8 到期 100（净 +2/90）、i=9..29 到期 80（净 (2−10)/90）、i=30..38 到期 100（净 +2/90）
        up, dn = 2 / 90 * 100, -8 / 90 * 100
        assert r["ok"] and r["hold_days"] == 21 and r["n_windows"] == 39
        assert r["mean_net_pct"] == pytest.approx((18 * up + 21 * dn) / 39, abs=0.01)
        assert r["win_pct"] == pytest.approx(18 / 39 * 100, abs=0.05)
        assert r["worst_net_pct"] == pytest.approx(dn, abs=0.01) and r["p05_net_pct"] == pytest.approx(dn, abs=0.01)
        assert r["independent_windows"] == 1             # 39 // 21：诚实地说互不重叠的只有一个

    def test_loss_is_floored_at_zero_when_the_put_ends_out_of_the_money(self):
        r = WS.replay_put(_closes([100.0] * 60), spot=100.0, strike=90.0, bid=2.0, dte=30)
        assert r["mean_net_pct"] == pytest.approx(2 / 90 * 100, abs=0.01) and r["win_pct"] == 100.0

    def test_p05_is_the_fifth_percentile_not_the_median(self):
        v = [100.0 - 0.5 * i for i in range(60)]                          # 一路缓跌：每个窗口的净收益都不同
        r = WS.replay_put(_closes(v), spot=100.0, strike=90.0, bid=2.0, dte=30)
        n = 60 - 21
        net = sorted((2.0 - max(90.0 - 100.0 * v[i + 21] / v[i], 0.0)) / 90.0 * 100.0 for i in range(n))
        assert net[1] != net[n // 2], "夹具要能分得开 p05 与中位数"
        assert r["p05_net_pct"] == pytest.approx(net[1], abs=0.01) and r["worst_net_pct"] == pytest.approx(net[0], abs=0.01)

    def test_exactly_the_minimum_number_of_windows_is_enough(self):
        r = WS.replay_put(_closes([100.0] * (21 + WS.MIN_REPLAY_WINDOWS)), spot=100.0, strike=90.0, bid=2.0, dte=30)
        assert r["ok"] is True and r["n_windows"] == WS.MIN_REPLAY_WINDOWS

    def test_too_few_windows_gives_a_reason(self):
        r = WS.replay_put(_closes([100.0] * 30), spot=100.0, strike=90.0, bid=2.0, dte=30)
        assert r["ok"] is False and r["reason"] == "no_replay_windows" and r["n_windows"] == 9


# ───────────────────────────── 账本梯子 ─────────────────────────────

def _row(**kw):
    s = lambda K, bid, sp, oi, d: {"short": {"strike": K, "bid": bid, "ask": bid * 1.05, "mid": bid * 1.025,  # noqa: E731
                                              "spread_pct": sp, "oi": oi, "delta": d}}
    row = {"ticker": "AAA", "dte": 31, "expiry": "2026-12-04", "underlying_price": 100.0, "earnings_date": "2026-11-20",
           "earnings_status": "before_expiry",
           "ladder": {"dte": 31, "expiry": "2026-12-04", "underlying_price": 100.0,
                      "put": {"0.20": s(90.0, 2.0, 0.05, 500, -0.2), "0.30": s(95.0, 3.5, 0.20, 50, -0.3)}}}
    row.update(kw)
    return row


class TestLadder:
    def test_rung_numbers_are_recomputed_from_the_row(self):
        L = WS.ladder_rungs(_row())
        r = L["rungs"]["0.20"]
        assert r["dist_pct"] == -10.0 and r["spread_pct"] == 5.0 and r["oi"] == 500 and r["capital_usd"] == 9000.0
        assert r["annual_yield_pct"] == 26.2                       # 2/90 × 365/31 × 100 = 26.165
        assert L["earnings_date"] == "2026-11-20" and L["dte"] == 31

    def test_missing_rung_or_row_is_none_not_zero(self):
        row = _row()
        del row["ladder"]["put"]["0.30"]
        assert WS.ladder_rungs(row)["rungs"]["0.30"] is None
        assert WS.ladder_rungs(None)["rungs"] == {"0.20": None, "0.30": None}

    def test_zero_bid_has_no_yield(self):
        row = _row()
        row["ladder"]["put"]["0.20"]["short"]["bid"] = 0.0
        assert WS.ladder_rungs(row)["rungs"]["0.20"]["annual_yield_pct"] is None

    @pytest.mark.parametrize("earn, want", [("2026-11-04", False), ("2026-11-05", True), ("2026-12-04", True),
                                            ("2026-12-05", False), ("2026-10-01", False), (None, None), ("garbage", None)])
    def test_earnings_window_boundaries(self, earn, want):
        assert WS.earnings_before(earn, "2026-11-04", "2026-12-04") is want

    def test_only_ladder_and_earnings_are_read_never_settlement_or_route(self):
        """盲期：账本行里的结算 / 路由字段，换成毒值也不能改变输出。"""
        a = WS.ladder_rungs(_row())
        poisoned = _row(expiry_close=999.0, settle_status="settled", route={"put": "0.99"}, route_legs={"x": 1})
        assert WS.ladder_rungs(poisoned) == a


class TestPickForAccept:
    CONTRACTS = [{"symbol": "AAA270115P00090000", "expiry": "2027-01-15", "cp": "P", "strike": 90.0, "bid": 2.0, "ask": 2.08,
                  "oi": 500, "delta": -0.2},
                 {"symbol": "AAA270115P00085000", "expiry": "2027-01-15", "cp": "P", "strike": 85.0, "bid": 1.2, "ask": 1.25,
                  "oi": 500, "delta": -0.15}]

    def test_same_contract_as_the_ledgers_own_rule(self):
        out = WS.pick_for_accept(self.CONTRACTS, 100.0, 92.0, "2026-11-04")
        c, _, _ = W.pick_put(self.CONTRACTS, 100.0, 92.0, "2026-11-04")
        assert out["ok"] and out["symbol"] == c["symbol"] and out["strike"] == 90.0
        assert out["net_cost"] == 88.0 and out["net_discount_pct"] == -12.0 and out["capital_usd"] == 9000.0
        assert out["annual_yield_pct"] == pytest.approx(round(2.0 / 90.0 * 365.0 / 72 * 100, 1))   # 72 天
        assert out["dte"] == 72 and out["accept_price_source"] == "user" and out["distance_pct"] == -8.0

    def test_placeholder_is_labelled_when_no_accept_price(self):
        out = WS.pick_for_accept(self.CONTRACTS, 100.0, None, "2026-11-04")
        assert out["accept_price_source"] == "placeholder" and out["accept_price"] == 90.0

    def test_failure_carries_the_ledgers_reason_code(self):
        out = WS.pick_for_accept(self.CONTRACTS, 100.0, 60.0, "2026-11-04")
        _, why, _ = W.pick_put(self.CONTRACTS, 100.0, 60.0, "2026-11-04")
        assert out["ok"] is False and out["reason"] == why and why

    def test_earnings_flag_and_replay_only_when_asked(self):
        out = WS.pick_for_accept(self.CONTRACTS, 100.0, 92.0, "2026-11-04", earnings_date="2026-12-01")
        assert out["earnings_in_window"] is True and "replay" not in out
        out = WS.pick_for_accept(self.CONTRACTS, 100.0, 92.0, "2026-11-04", closes=_closes([100.0] * 120))
        assert out["replay"]["ok"] is True and out["earnings_in_window"] is None          # 日期没给 ⇒ 待验证，不是 False


# ───────────────────────────── 设置 ─────────────────────────────

def _svc(tmp_path, prices=None, now=1000.0):
    clock = {"t": now}
    svc = AlphaBotService(wheel_prices_fn=prices, clock=lambda: clock["t"], state_dir=tmp_path)
    svc._clock_box = clock
    return svc


class TestSettings:
    def test_watch_dedupes_keeps_order_and_caps(self):
        assert WS.clean_watch(["xom", "XOM", " wmt "]) == ["XOM", "WMT"]
        assert WS.clean_watch(None) == []
        with pytest.raises(ValueError):
            WS.clean_watch([f"T{i}" for i in range(WS.MAX_WATCH + 1)])
        with pytest.raises(ValueError):
            WS.clean_watch("XOM")
        with pytest.raises(ValueError):
            WS.clean_watch([""])

    def test_boundaries_are_inclusive_where_documented(self):
        assert len(WS.clean_watch([f"T{i}" for i in range(WS.MAX_WATCH)])) == WS.MAX_WATCH
        assert WS.clean_accept({"XOM": 1_000_000}) == {"XOM": 1_000_000.0}
        with pytest.raises(ValueError):
            WS.clean_accept({"XOM": 1_000_000.5})

    @pytest.mark.parametrize("bad", [0, -1, "abc", float("nan"), float("inf"), 2_000_000, True])
    def test_accept_rejects_non_positive_and_non_finite(self, bad):
        with pytest.raises(ValueError):
            WS.clean_accept({"XOM": bad})

    def test_accept_none_or_empty_string_clears_and_numeric_strings_pass(self):
        assert WS.clean_accept({"xom": None, "WMT": "", "NVDA": "180.5"}) == {"XOM": None, "WMT": None, "NVDA": 180.5}

    def test_service_round_trip_and_clearing(self, tmp_path):
        svc = _svc(tmp_path)
        cur = svc.update_settings({"wheel_watch": ["xom", "wmt"], "wheel_accept": {"XOM": 150, "WMT": 115}})
        assert cur["wheel_watch"] == ["XOM", "WMT"] and cur["wheel_accept"] == {"XOM": 150.0, "WMT": 115.0}
        cur = svc.update_settings({"wheel_accept": {"XOM": None}})                      # 清掉一只，另一只不动
        assert cur["wheel_accept"] == {"WMT": 115.0}
        assert svc.settings()["wheel_watch"] == ["XOM", "WMT"]

    def test_service_rejects_bad_input_with_the_param_name(self, tmp_path):
        svc = _svc(tmp_path)
        with pytest.raises(BadRequest) as e1:
            svc.update_settings({"wheel_accept": {"XOM": -5}})
        assert e1.value.param == "wheel_accept"
        with pytest.raises(BadRequest) as e2:
            svc.update_settings({"wheel_watch": "XOM"})
        assert e2.value.param == "wheel_watch"


# ───────────────────────────── 只并排事实 ─────────────────────────────

def _walk_keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(o, list):
        for x in o:
            yield from _walk_keys(x)


class TestFactsOnly:
    def test_rows_carry_no_rank_score_or_recommendation_keys(self):
        stats = {"AAA": WS.price_stats(_closes(TestPriceStats.V))}
        rows = WS.build_rows(["AAA", "BBB"], {"AAA": _row()}, stats, {"AAA": 92.0}, as_of="2026-11-04", in_universe=["AAA"])
        bad = [k for k in _walk_keys(rows) if re.search(r"rank|score|recommend|pick_|best|rating|综合", k, re.I)]
        assert not bad, bad

    def test_row_facts_and_flags(self):
        stats = {"AAA": WS.price_stats(_closes(TestPriceStats.V))}
        rows = {r["ticker"]: r for r in WS.build_rows(["AAA", "BBB"], {"AAA": _row()}, stats, {"AAA": 92.0},
                                                      as_of="2026-11-04", in_universe=["AAA"])}
        a, b = rows["AAA"], rows["BBB"]
        assert a["in_wheel_universe"] and a["accept_price_source"] == "user" and a["accept_distance_pct"] == -8.0
        r20, r30 = a["ladder"]["rungs"]["0.20"], a["ladder"]["rungs"]["0.30"]
        assert r20["spread_ok"] is True and r20["oi_ok"] is True                       # 5% ≤ 15%、OI 500 ≥ 100
        assert r30["spread_ok"] is False and r30["oi_ok"] is False                     # 20% > 15%、OI 50 < 100
        assert a["earnings_in_ladder_window"] is True                                  # 财报 11-20 在 (11-04, 12-04] 内
        assert b["ladder"] is None and b["ladder_reason"] == "no_ladder" and b["stats"] is None
        assert b["accept_price_source"] == "placeholder" and b["accept_distance_pct"] is None

    def test_flags_are_inclusive_at_exactly_the_ledgers_limits(self):
        row = _row()
        row["ladder"]["put"]["0.20"]["short"].update(spread_pct=W.CONFIG["max_spread_pct"], oi=W.CONFIG["min_oi"])
        r = WS.build_rows(["AAA"], {"AAA": row}, {}, {}, as_of="2026-11-04")[0]["ladder"]["rungs"]["0.20"]
        assert r["spread_ok"] is True and r["oi_ok"] is True

    def test_public_rules_carry_the_ledgers_own_limits(self):
        r = WS.public_rules()
        assert r["max_spread_pct"] == W.CONFIG["max_spread_pct"] and r["min_oi"] == W.CONFIG["min_oi"]
        assert r["put_dte_range"] == list(W.CONFIG["put_dte_range"]) and set(r["reasons"]) == set(WS.REASONS)


# ───────────────────────────── 取数 ─────────────────────────────

class YFRateLimited(Exception):
    """类名是限流判据（`yf_gate.is_rate_limited` / `beta_long._rate_limited` 认类名）。"""


class FakeYF:
    def __init__(self, series, boom=(), limited=()):
        self.series, self.boom, self.limited, self.asked = series, set(boom), set(limited), []

    def Ticker(self, t):                                                                 # noqa: N802
        outer = self

        class _T:
            ticker = t

            def history(self, **kw):
                outer.asked.append(t)
                if t in outer.limited:
                    raise YFRateLimited("429")
                if t in outer.boom:
                    raise TimeoutError("x")
                s = outer.series.get(t)
                cols = ("Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits")
                if not s:
                    return pd.DataFrame({k: [] for k in cols}, index=pd.DatetimeIndex([]))
                c = [s[d] for d in sorted(s)]
                return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000.0, "Dividends": 0.0,
                                     "Stock Splits": 0.0}, index=pd.to_datetime(sorted(s)))
        return _T()


class TestPriceLoader:
    S = _closes(TestPriceStats.V)

    def test_loads_and_reports_per_ticker_outcome(self):
        out, info = WP.fetch_closes(["XOM", "WMT"], yf_module=FakeYF({"XOM": self.S, "WMT": {}}))
        assert len(out["XOM"]) == 140 and info["XOM"] == {"n": 140, "reason": None}
        assert out["WMT"] == {} and info["WMT"] == {"n": 0, "reason": "unavailable"}

    def test_one_failure_does_not_poison_the_rest(self):
        fy = FakeYF({"NVDA": self.S}, boom=["XOM"])
        out, info = WP.fetch_closes(["XOM", "NVDA"], yf_module=fy)
        assert info["XOM"]["reason"] == "exception:TimeoutError" and info["NVDA"]["reason"] is None and len(out["NVDA"]) == 140

    def test_after_a_rate_limit_it_stops_hitting_yahoo_and_says_when_to_retry(self):
        fy = FakeYF({"A": self.S, "C": self.S}, limited=["B"])
        out, info = WP.fetch_closes(["A", "B", "C", "D"], yf_module=fy)
        assert fy.asked == ["A", "B"], "限流之后不该再逐只去撞"
        assert info["A"]["reason"] is None
        for t in ("B", "C", "D"):
            assert info[t]["reason"] == "rate_limited" and info[t]["retry_after_sec"] is not None and out[t] == {}

    def test_never_raises_even_when_yfinance_is_unusable(self):
        out, info = WP.fetch_closes(["XOM"], yf_module=object())
        assert out["XOM"] == {} and info["XOM"]["reason"].startswith("exception:")


# ───────────────────────────── 端点 ─────────────────────────────

LEDGER_ROWS = {"XOM": _row(ticker="XOM"), "WMT": _row(ticker="WMT"), "NVDA": _row(ticker="NVDA")}


def _svc_l(tmp_path, prices=None):
    svc = _svc(tmp_path, prices)
    svc._wheel_ledger = lambda: ("2026-11-03", LEDGER_ROWS, None)
    return svc


def _ok_prices(calls=None):
    def fn(need):
        if calls is not None:
            calls.append(list(need))
        return {t: _closes(TestPriceStats.V) for t in need}, {t: {"n": 140, "reason": None} for t in need}
    return fn


class TestEndpoints:
    def test_offline_layer_is_instant_and_defaults_to_the_ledgers_universe(self, tmp_path):
        d = _svc_l(tmp_path).wheel_screen("watch")
        assert d["available"] and d["tickers"] == list(W.CONFIG["universe"]) and d["stats_cached"] == []
        assert d["rows"][0]["ladder"]["rungs"]["0.20"]["strike"] == 90.0 and d["rows"][0]["stats"] is None

    def test_scope_all_uses_the_default_watchlist_and_bad_scope_is_rejected(self, tmp_path):
        d = _svc_l(tmp_path).wheel_screen("all")
        assert len(d["tickers"]) >= 20 and d["tickers"] != list(W.CONFIG["universe"])
        for bad in ("everything", "x", ""):
            with pytest.raises(BadRequest):
                _svc_l(tmp_path).wheel_screen(bad)

    def test_prices_layer_fetches_only_missing_and_caches_for_six_hours(self, tmp_path):
        calls = []
        svc = _svc_l(tmp_path, _ok_prices(calls))
        p = svc.wheel_screen_prices(["XOM", "WMT"])
        assert calls == [["XOM", "WMT"]] and p["stats"]["XOM"]["ok"] and p["fetched"] == ["XOM", "WMT"]
        svc.wheel_screen_prices(["XOM", "WMT", "NVDA"])
        assert calls[-1] == ["NVDA"], "已缓存的不重取"
        svc._clock_box["t"] += 6 * 3600 + 1
        svc.wheel_screen_prices(["XOM"])
        assert calls[-1] == ["XOM"], "过 6 小时就该重取"
        assert svc.wheel_screen("watch")["stats_cached"] == ["XOM"]                      # 另两只缓存过期了、不再显示旧数

    def test_force_refetches_even_when_fresh(self, tmp_path):
        calls = []
        svc = _svc_l(tmp_path, _ok_prices(calls))
        svc.wheel_screen_prices(["XOM"])
        svc.wheel_screen_prices(["XOM"], force=True)
        assert calls == [["XOM"], ["XOM"]]

    def test_rate_limit_is_reported_with_retry_after_and_existing_stats_still_show(self, tmp_path):
        def fn(need):
            return ({"XOM": _closes(TestPriceStats.V)} if "XOM" in need else {}), {
                t: ({"n": 140, "reason": None} if t == "XOM" else {"n": 0, "reason": "rate_limited", "retry_after_sec": 183.0})
                for t in need}
        p = _svc_l(tmp_path, fn).wheel_screen_prices(["XOM", "WMT"])
        assert p["stats"]["XOM"]["ok"] and p["stats"]["WMT"]["ok"] is False and p["stats"]["WMT"]["reason"] == "rate_limited"
        assert p["retry_after_sec"] == 183.0

    def test_fetch_function_raising_is_reported_not_swallowed(self, tmp_path):
        def boom(need):
            raise RuntimeError("x")
        p = _svc_l(tmp_path, boom).wheel_screen_prices(["XOM"])
        assert p["stats"]["XOM"]["ok"] is False and p["stats"]["XOM"]["reason"] == "unavailable"
        assert "RuntimeError" in p["stats"]["XOM"]["detail"]

    def test_prices_require_tickers(self, tmp_path):
        with pytest.raises(BadRequest):
            _svc_l(tmp_path).wheel_screen_prices([])

    def _chain_svc(self, tmp_path):
        svc = _svc_l(tmp_path, _ok_prices())
        raw = {"underlying_price": 100.0, "as_of": "2026-11-04", "contracts": TestPickForAccept.CONTRACTS,
               "session_live": False, "underlying_price_source": "cboe_close"}
        svc._live_fetch = lambda t: (lambda tk, as_of=None: (raw, None))
        return svc

    def test_chain_layer_uses_the_users_accept_price_and_the_ledger_earnings_date(self, tmp_path):
        svc = self._chain_svc(tmp_path)
        svc.update_settings({"wheel_accept": {"XOM": 92}})
        c = svc.wheel_screen_chain("XOM")
        assert c["ok"] and c["strike"] == 90.0 and c["accept_price_source"] == "user" and c["replay_needs_prices"] is True
        assert "replay" not in c
        assert c["earnings_in_window"] is True                       # 账本财报 2026-11-20 落在 (11-04, 2027-01-15] 内
        svc.wheel_screen_prices(["XOM"])
        c2 = svc.wheel_screen_chain("XOM")
        assert c2["replay_needs_prices"] is False and c2["replay"]["ok"] is True

    def test_chain_without_an_accept_price_says_placeholder(self, tmp_path):
        c = self._chain_svc(tmp_path).wheel_screen_chain("WMT")
        assert c["ok"] and c["accept_price_source"] == "placeholder"

    def test_chain_failure_is_reported_with_the_reason(self, tmp_path):
        svc = _svc_l(tmp_path)
        svc._live_fetch = lambda t: (lambda tk, as_of=None: (None, "stale_vintage"))
        assert svc.wheel_screen_chain("XOM") == {"ok": False, "reason": "no_chain", "detail": "stale_vintage", "ticker": "XOM"}

    def test_demo_mode_reads_nothing(self, tmp_path):
        svc = AlphaBotService(demo=True, state_dir=tmp_path)
        assert svc.wheel_screen()["available"] is False and svc.wheel_screen_prices(["XOM"])["available"] is False
        assert svc.wheel_screen_chain("XOM")["reason"] == "demo"


# ───────────────────────────── 原因码 ─────────────────────────────

class TestReasonTable:
    FILES = ("alphabot/wheel_screen.py", "alphabot/wheel_prices.py")

    def _body(self):
        svc = (REPO / "alphabot" / "service.py").read_text(encoding="utf-8")
        i, j = svc.index("    def wheel_screen(self"), svc.index("    # ── 对冲账本（v0.45.459")
        src = "\n".join((REPO / f).read_text(encoding="utf-8") for f in self.FILES) + svc[i:j]
        k = src.index("REASONS = {")
        return src[:k] + src[src.index("}", k) + 1:]                        # 去掉登记表本身

    def test_every_registered_code_is_written_somewhere(self):
        body = self._body()
        dead = [c for c in WS.REASONS if f'"{c}"' not in body]
        assert not dead, f"登记了却没人写的原因码：{dead}"

    def test_every_literal_reason_written_is_registered(self):
        body = self._body()
        written = set(re.findall(r'"reason":\s*"([a-z_]+)"', body)) | set(re.findall(r'"ladder_reason":[^"\n]*"([a-z_]+)"', body))
        assert written, "扫描空转了"
        assert written <= set(WS.REASONS), written - set(WS.REASONS)       # exception:* 是动态前缀（f-string），扫不到也不该登记
