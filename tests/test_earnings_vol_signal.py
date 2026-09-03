"""v0.45.101 财报事件波动率信号（earnings_vol_signal）。全部离线。"""

import json

import pytest

import earnings_vol_signal as evs

AS_OF = "2026-09-03"
EXPIRY = "2026-10-02"     # 29 DTE
EARN = "2026-09-20"


def _leg(sym, cp, strike, bid, ask, quote_ok=True, spread_pct=None):
    mid = round((bid + ask) / 2, 4) if quote_ok else None
    if spread_pct is None and quote_ok:
        spread_pct = round((ask - bid) / mid, 4)
    return {"symbol": sym, "type": cp, "role": f"atm_{'call' if cp == 'C' else 'put'}",
            "strike": strike, "expiry": EXPIRY, "dte": 29, "bid": bid, "ask": ask,
            "mid": mid, "spread_pct": spread_pct if quote_ok else None, "iv": 0.3,
            "delta": 0.5 if cp == "C" else -0.5, "quote_ok": quote_ok}


def _qs(S=100.0, c=(4.9, 5.1), p=(4.9, 5.1), expiry=EXPIRY, dte=29, call_ok=True, put_ok=True,
        call_spread=None, put_spread=None, available=True):
    call = _leg("XYZ261002C00100000", "C", 100.0, *c, quote_ok=call_ok, spread_pct=call_spread)
    put = _leg("XYZ261002P00100000", "P", 100.0, *p, quote_ok=put_ok, spread_pct=put_spread)
    ok = call_ok and put_ok
    straddle = round(call["mid"] + put["mid"], 4) if ok else None
    return {"data_available": available, "source": "cboe", "error": None, "target_dte": 30,
            "selected_expiry": expiry, "selected_dte": dte, "underlying_price": S,
            "iv30": 0.3, "market_open": False, "fetched_at": "2026-09-03T16:10:00",
            "contracts": {"atm_call": call, "atm_put": put, "c25": None, "p25": None},
            "missing_reasons": {}, "atm_straddle_mid": straddle,
            "implied_move_pct": round(straddle / S * 100, 4) if straddle else None}


def _stats(n=8, median=5.0):
    return {"ticker": "XYZ", "n": n, "n_missing": 0, "median_abs_move_pct": median,
            "mean_abs_move_pct": median, "max_abs_move_pct": median * 2, "moves": [],
            "source": "synthetic", "usable": n >= 4}


UP = {"earnings_date": EARN, "earnings_time": "AMC"}


def _no_nan(obj):
    json.dumps(obj, allow_nan=False)   # 有 NaN/Inf 会抛


# ==================== 合格条件 ====================

class TestEligibility:

    def test_happy_path_eligible(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=None)
        assert s["eligible"] is True and s["reason"] is None
        assert s["straddle_move_pct"] == pytest.approx(10.0)
        _no_nan(s)

    def test_event_after_expiry_is_ineligible(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), {"earnings_date": "2026-10-10"}, _stats(), None)
        assert s["eligible"] is False and "not within" in s["reason"]

    def test_event_on_as_of_is_ineligible_but_on_expiry_is_ok(self):
        assert evs.compute_signal("XYZ", AS_OF, _qs(), {"earnings_date": AS_OF}, _stats(), None)["eligible"] is False
        assert evs.compute_signal("XYZ", AS_OF, _qs(), {"earnings_date": EXPIRY}, _stats(), None)["eligible"] is True

    def test_atm_leg_quote_not_ok(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(put_ok=False), UP, _stats(), None)
        assert s["eligible"] is False and s["reason"] == "atm leg quote not ok"

    def test_quote_set_missing_or_unavailable(self):
        assert evs.compute_signal("XYZ", AS_OF, None, UP, _stats(), None)["reason"] == "quote_set unavailable"
        assert evs.compute_signal("XYZ", AS_OF, _qs(available=False), UP, _stats(), None)["eligible"] is False

    def test_no_upcoming_date(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), None, _stats(), None)
        assert s["reason"] == "no upcoming earnings date"

    def test_insufficient_history(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(n=3), None)
        assert s["eligible"] is False and "insufficient earnings history" in s["reason"]
        assert evs.compute_signal("XYZ", AS_OF, _qs(), UP, None, None)["eligible"] is False

    def test_ineligible_still_reports_intermediates(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(n=2), rv_30d=30.0)
        assert s["straddle_move_pct"] == pytest.approx(10.0) and s["hist_n"] == 2
        assert s["ratio"] is None and s["label"] is None


# ==================== 扩散扣除 ====================

class TestDiffusionMath:

    def test_hand_computed_event_move(self):
        # straddle 10%；rv 20% → diffusion = 0.8·0.20·√(29/252)·100 = 5.42774
        # event = √(100 − 29.4604) = 8.3988；ratio = 8.3988/5 = 1.6798 → rich
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(median=5.0), rv_30d=20.0)
        assert s["diffusion_move_pct"] == pytest.approx(5.4277, abs=1e-3)
        assert s["implied_event_move_pct"] == pytest.approx(8.3988, abs=1e-3)
        assert s["ratio"] == pytest.approx(1.6798, abs=1e-3)
        assert s["event_move_basis"] == "straddle_minus_diffusion"
        assert s["event_move_floor_hit"] is False
        assert s["implied_event_move_pct"] < s["straddle_move_pct"]

    def test_floor_when_diffusion_exceeds_straddle(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=60.0)   # diffusion 16.3 > 10
        assert s["implied_event_move_pct"] == 0.0 and s["event_move_floor_hit"] is True
        # 被压到 0 的事件波动是退化数字：raw 仍算 cheap，但不得进账本
        assert s["raw_label"] == "cheap"
        assert s["label"] == "untradeable" and s["tradeable"] is False
        assert "floored" in s["untradeable_reason"]
        _no_nan(s)

    def test_rv_none_uses_raw_straddle_no_nan(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=None)
        assert s["event_move_basis"] == "raw_straddle"
        assert s["diffusion_move_pct"] is None
        assert s["implied_event_move_pct"] == pytest.approx(10.0)
        _no_nan(s)

    def test_rv_nan_treated_as_missing(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=float("nan"))
        assert s["rv_30d"] is None and s["event_move_basis"] == "raw_straddle"
        _no_nan(s)


# ==================== 标签阈值 / 可交易性 ====================

class TestLabels:

    @pytest.mark.parametrize("median,label", [
        (5.0, "rich"),                          # ratio 2.0
        (10.0 / 1.30, "rich"),                  # 恰好 1.30 → ≥ rich
        (10.0 / 1.29, "fair"),
        (10.0, "fair"),                         # 1.0
        (10.0 / 0.76, "fair"),
        (10.0 / 0.75, "cheap"),                 # 恰好 0.75 → ≤ cheap
        (20.0, "cheap"),                        # 0.5
    ])
    def test_thresholds(self, median, label):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(median=median), None)
        assert s["label"] == s["raw_label"] == label and s["tradeable"] is True

    def test_untradeable_spread_overrides_label(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(call_spread=0.276), UP, _stats(median=5.0), None)
        assert s["eligible"] is True
        assert s["raw_label"] == "rich" and s["label"] == "untradeable"
        assert s["tradeable"] is False and "27.6%" in s["untradeable_reason"]
        assert s["max_leg_spread_pct"] == pytest.approx(0.276)

    def test_spread_at_threshold_is_tradeable(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(call_spread=0.15, put_spread=0.10), UP, _stats(), None)
        assert s["tradeable"] is True and s["max_leg_spread_pct"] == pytest.approx(0.15)


# ==================== scan 幂等 ====================

@pytest.fixture
def state(tmp_path, monkeypatch):
    sd = tmp_path / "options_paper_state"
    monkeypatch.setattr(evs, "STATE_DIR", sd)
    monkeypatch.setattr(evs, "SIGNALS_FILE", sd / "earnings_signals.jsonl")
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache


def _write_snap(cache, ticker, date, qs, rv=None, suffix=""):
    (cache / f"options_snapshot_{ticker}_{date}{suffix}.json").write_text(
        json.dumps({"ticker": ticker, "quote_set": qs, "rv_30d": rv}), encoding="utf-8")


class _Watcher:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def get_earnings_date(self, tk):
        self.calls.append(tk)
        d = self.table.get(tk)
        return {"earnings_date": d, "earnings_time": "AMC"} if d else None


class TestScan:

    def test_scan_is_idempotent_per_date_and_ignores_backfilled(self, state):
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        _write_snap(state, "ABC", AS_OF, _qs(), rv=20.0)
        _write_snap(state, "OLD", AS_OF, _qs(), rv=20.0, suffix="_backfilled-2026-09-04")
        # 另一天的行必须保留
        evs.STATE_DIR.mkdir(parents=True)
        evs.SIGNALS_FILE.write_text(json.dumps({"ticker": "XYZ", "as_of": "2026-09-02"}) + "\n")
        w = _Watcher({"XYZ": EARN, "ABC": "2026-12-01"})
        stats_calls = []

        def stats_fn(tk):
            stats_calls.append(tk)
            return _stats()
        r1 = evs.scan(AS_OF, cache_dir=state, watcher=w, stats_fn=stats_fn)
        r2 = evs.scan(AS_OF, cache_dir=state, watcher=w, stats_fn=stats_fn)
        assert {s["ticker"] for s in r1} == {"XYZ", "ABC"}          # OLD 被忽略
        rows = evs.load_signals()
        assert sum(1 for r in rows if r["as_of"] == AS_OF) == 2
        assert sum(1 for r in rows if r["as_of"] == "2026-09-02") == 1
        assert len(r2) == 2
        xyz = next(s for s in r1 if s["ticker"] == "XYZ")
        abc = next(s for s in r1 if s["ticker"] == "ABC")
        assert xyz["eligible"] is True and xyz["label"] == "rich"
        assert abc["eligible"] is False and "not within" in abc["reason"]
        # 财报不在到期日内的票不该去拉历史（省网络）
        assert stats_calls == ["XYZ", "XYZ"]

    def test_scan_skips_network_when_quotes_dead(self, state):
        _write_snap(state, "XYZ", AS_OF, _qs(available=False))
        w = _Watcher({"XYZ": EARN})
        r = evs.scan(AS_OF, cache_dir=state, watcher=w, stats_fn=lambda tk: pytest.fail("should not fetch"))
        assert r[0]["reason"] == "quote_set unavailable" and w.calls == []

    def test_scan_handles_old_snapshot_without_quote_set(self, state):
        (state / f"options_snapshot_XYZ_{AS_OF}.json").write_text(json.dumps({"ticker": "XYZ"}))
        r = evs.scan(AS_OF, cache_dir=state, watcher=_Watcher({}), stats_fn=lambda tk: None)
        assert len(r) == 1 and r[0]["eligible"] is False


# ==================== settle ====================

class TestSettle:

    def test_settle_fills_realized_and_ignores_future(self, state):
        evs.STATE_DIR.mkdir(parents=True)
        past = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(median=5.0), rv_30d=20.0)
        fut = evs.compute_signal("ABC", AS_OF, _qs(), {"earnings_date": "2026-09-30"}, _stats(), None)
        bad = evs.compute_signal("BAD", AS_OF, _qs(put_ok=False), UP, _stats(), None)   # 不合格
        evs._write_jsonl(evs.SIGNALS_FILE, [past, fut, bad])

        bars = {"XYZ": [{"date": "2026-09-18", "close": 100.0}, {"date": "2026-09-21", "close": 104.0}]}
        n = evs.settle_signals("2026-09-25", bars_fn=lambda tk: bars.get(tk))
        assert n == 1
        rows = {r["ticker"]: r for r in evs.load_signals()}
        x = rows["XYZ"]
        assert x["realized_abs_move_pct"] == pytest.approx(4.0)
        assert x["realized_ratio"] == pytest.approx(4.0 / x["implied_event_move_pct"], abs=1e-3)
        assert x["settled_on"] == "2026-09-25"
        assert rows["ABC"]["realized_abs_move_pct"] is None and rows["BAD"]["settled_on"] is None
        # 再跑一次：没有新东西可填
        assert evs.settle_signals("2026-09-26", bars_fn=lambda tk: bars.get(tk)) == 0

    def test_settle_waits_when_post_bar_missing(self, state):
        evs.STATE_DIR.mkdir(parents=True)
        past = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), None)
        evs._write_jsonl(evs.SIGNALS_FILE, [past])
        n = evs.settle_signals("2026-09-21", bars_fn=lambda tk: [{"date": "2026-09-18", "close": 100.0}])
        assert n == 0 and evs.load_signals()[0]["realized_abs_move_pct"] is None

    def test_summary_stats_honest_below_min_n(self, state):
        evs.STATE_DIR.mkdir(parents=True)
        sigs = []
        for i, tk in enumerate(["A", "B"]):
            s = evs.compute_signal(tk, AS_OF, _qs(), UP, _stats(median=5.0), None)
            s["realized_ratio"] = 0.5 + i
            sigs.append(s)
        evs._write_jsonl(evs.SIGNALS_FILE, sigs)
        st = evs.summary_stats()
        assert st["n_settled"] == 2 and st["by_label"]["rich"]["n"] == 2
        assert st["by_label"]["rich"]["mean_realized_ratio"] is None
        assert st["mean_realized_ratio"] is None
        sigs.append(dict(sigs[0], ticker="C", realized_ratio=1.0))   # (0.5+1.5+1.0)/3 = 1.0
        evs._write_jsonl(evs.SIGNALS_FILE, sigs)
        assert evs.summary_stats()["by_label"]["rich"]["mean_realized_ratio"] == pytest.approx(1.0)
