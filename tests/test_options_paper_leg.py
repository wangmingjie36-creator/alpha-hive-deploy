"""v0.45.101 期权纸面腿：财报跨式账本（options_paper_leg）。全部离线、状态目录重定向。"""

import json
import math

import pytest

import earnings_vol_signal as evs
import options_paper_leg as opl

AS_OF = "2026-09-03"
EXPIRY = "2026-10-02"
EARN = "2026-09-20"
CALL, PUT = "XYZ261002C00100000", "XYZ261002P00100000"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    sd = tmp_path / "options_paper_state"
    monkeypatch.setattr(opl, "STATE_DIR", sd)
    monkeypatch.setattr(opl, "POSITIONS_FILE", sd / "positions.jsonl")
    monkeypatch.setattr(opl, "CLOSED_FILE", sd / "closed_trades.jsonl")
    monkeypatch.setattr(opl, "EQUITY_FILE", sd / "equity_curve.jsonl")
    monkeypatch.setattr(opl, "META_FILE", sd / "meta.json")
    monkeypatch.setattr(evs, "STATE_DIR", sd)
    monkeypatch.setattr(evs, "SIGNALS_FILE", sd / "earnings_signals.jsonl")
    # 测试自己钉预算（$20k × 2% = $400/笔），与生产校准值解耦——
    # 生产规模按真实跨式价格调整时（v0.45.101 已调到 $100k × 6%），这里的
    # 合约数断言不该跟着漂。
    monkeypatch.setitem(opl.CONFIG, "starting_capital", 20_000.0)
    monkeypatch.setitem(opl.CONFIG, "risk_per_trade_pct", 2.0)
    return sd


def _leg(sym, strike, bid, ask, ok=True):
    mid = round((bid + ask) / 2, 4)
    return {"symbol": sym, "strike": strike, "expiry": EXPIRY, "bid": bid, "ask": ask, "mid": mid,
            "spread_pct": round((ask - bid) / mid, 4) if mid else None, "iv": 0.3, "delta": 0.5, "quote_ok": ok}


def _sig(ticker="XYZ", label="cheap", c=(0.9, 1.1), p=(0.9, 1.1), earnings=EARN, expiry=EXPIRY,
         ratio=None, tradeable=True, eligible=True, S=100.0):
    if ratio is None:
        ratio = 0.5 if label == "cheap" else 2.0
    call, put = _leg(CALL.replace("XYZ", ticker), 100.0, *c), _leg(PUT.replace("XYZ", ticker), 100.0, *p)
    call["expiry"] = put["expiry"] = expiry
    return {"ticker": ticker, "as_of": AS_OF, "eligible": eligible, "reason": None, "label": label,
            "raw_label": label, "tradeable": tradeable, "earnings_date": earnings, "selected_expiry": expiry,
            "dte": 29, "underlying_price": S, "atm_strike": 100.0, "implied_event_move_pct": 8.4,
            "hist_median_abs_move_pct": 5.0, "hist_n": 8, "ratio": ratio, "max_leg_spread_pct": 0.1,
            "event_move_basis": "straddle_minus_diffusion", "quote": {"call": call, "put": put}}


def _q(bid, ask, ok=True, mid=None):
    m = round((bid + ask) / 2, 4) if mid is None else mid
    return {"bid": bid, "ask": ask, "mid": m, "quote_ok": ok, "role": "held"}


def _quotes_fn(table):
    """table: {symbol: quote | None}；缺的符号 → None。"""
    def fn(ticker, symbols):
        return {s: table.get(s) for s in symbols}
    return fn


def _none_quotes(ticker, symbols):
    return {s: None for s in symbols}


def _all_floats_finite(obj, path="$"):
    if isinstance(obj, float):
        assert math.isfinite(obj), f"{path} = {obj!r}"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _all_floats_finite(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _all_floats_finite(v, f"{path}[{i}]")


def _state_files_finite():
    for p in (opl.POSITIONS_FILE, opl.CLOSED_FILE, opl.EQUITY_FILE):
        if p.exists():
            for line in p.read_text().splitlines():
                if line.strip():
                    _all_floats_finite(json.loads(line), p.name)
    _all_floats_finite(json.loads(opl.META_FILE.read_text()), "meta")


# ==================== 成交约定 / 仓位 ====================

class TestFills:

    def test_long_buys_at_ask(self):
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap", c=(0.9, 1.1), p=(0.9, 1.1))])
        assert len(r["positions"]) == 1
        pos = r["positions"][0]
        assert pos["side"] == "long"
        assert pos["entry_call"] == 1.1 and pos["entry_put"] == 1.1 and pos["entry_premium"] == pytest.approx(2.2)
        assert pos["contracts"] == 1                       # floor(400/220)
        assert pos["size_usd"] == pytest.approx(220.0)
        assert r["cash"] == pytest.approx(20_000 - 220.0)
        assert pos["last_mark"] == pytest.approx(2.0)      # mid 之和
        assert r["nav"] == pytest.approx(20_000 - 220 + 200)   # 立刻付掉点差

    def test_short_sells_at_bid(self):
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="rich", c=(0.9, 1.1), p=(0.9, 1.1))])
        pos = r["positions"][0]
        assert pos["side"] == "short"
        assert pos["entry_call"] == 0.9 and pos["entry_premium"] == pytest.approx(1.8)
        assert pos["contracts"] == 2                       # floor(400/180)
        assert pos["size_usd"] == pytest.approx(360.0)
        assert r["cash"] == pytest.approx(20_000 + 360.0)
        assert r["nav"] == pytest.approx(20_000 + 360 - 400)

    def test_fill_helpers_directly(self):
        c, p = _leg(CALL, 100, 0.9, 1.1), _leg(PUT, 100, 1.9, 2.1)
        assert opl.entry_fills("long", c, p) == (1.1, 2.1)
        assert opl.entry_fills("short", c, p) == (0.9, 1.9)
        assert opl.exit_fills("long", c, p) == (0.9, 1.9)
        assert opl.exit_fills("short", c, p) == (1.1, 2.1)


class TestSizing:

    def test_contracts_floor_and_short_cap(self):
        n, why = opl.size_contracts("long", 1.8, 20_000)
        assert (n, why) == (2, None)
        n, why = opl.size_contracts("short", 1.8, 20_000)
        assert n == 2 and 1.8 * 100 * n <= 400
        n, _ = opl.size_contracts("short", 1.35, 20_000)   # 400/135 = 2.96 → 2
        assert n == 2 and 135 * n <= 400

    def test_one_contract_exceeds_budget_skips(self):
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap", c=(2.4, 2.6), p=(2.4, 2.6))])
        assert r["positions"] == []
        assert r["skipped"][0]["ticker"] == "XYZ" and "exceeds risk budget" in r["skipped"][0]["reason"]
        assert r["cash"] == 20_000
        assert json.loads(opl.META_FILE.read_text())["skipped_entries"][0]["reason"].startswith("1 contract")

    def test_size_contracts_rejects_nan(self):
        assert opl.size_contracts("long", float("nan"), 20_000)[0] == 0
        assert opl.size_contracts("long", 2.0, float("nan"))[0] == 0

    def test_max_open_and_no_duplicate_ticker(self, monkeypatch):
        monkeypatch.setitem(opl.CONFIG, "max_open", 1)
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes,
                             signals=[_sig("AAA", "cheap"), _sig("AAA", "rich"), _sig("BBB", "cheap")])
        assert len(r["positions"]) == 1

    def test_fair_and_untradeable_never_enter(self):
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes,
                             signals=[_sig("AAA", "fair"), _sig("BBB", "untradeable", tradeable=False),
                                      _sig("CCC", "rich", eligible=False)])
        assert r["positions"] == []

    def test_toggle_disables_side(self, monkeypatch):
        monkeypatch.setitem(opl.CONFIG, "trade_rich", False)
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig("AAA", "rich"), _sig("BBB", "cheap")])
        assert [p["ticker"] for p in r["positions"]] == ["BBB"]


# ==================== 盯市 ====================

class TestMarkToMarket:

    def test_cboe_mid_mark_and_unrealized(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])   # long 1× @2.2
        r = opl.run_for_date("2026-09-04", quotes_fn=_quotes_fn({CALL: _q(1.4, 1.6), PUT: _q(1.4, 1.6)}), signals=[])
        pos = r["positions"][0]
        assert pos["last_mark"] == pytest.approx(3.0) and pos["mark_source"] == "cboe_mid"
        assert pos["last_mark_date"] == "2026-09-04" and pos["stale_days"] == 0
        assert r["equity_snapshot"]["unrealized"] == pytest.approx((3.0 - 2.2) * 100)
        assert r["nav"] == pytest.approx(20_000 - 220 + 300)

    def test_short_unrealized_sign(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="rich")])   # short 2× @1.8
        r = opl.run_for_date("2026-09-04", quotes_fn=_quotes_fn({CALL: _q(1.4, 1.6), PUT: _q(1.4, 1.6)}), signals=[])
        assert r["equity_snapshot"]["unrealized"] == pytest.approx((1.8 - 3.0) * 100 * 2)

    def test_stale_keeps_last_mark_and_counts_once_per_day(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])
        r = opl.run_for_date("2026-09-04", quotes_fn=_none_quotes, signals=[])
        pos = r["positions"][0]
        assert pos["last_mark"] == pytest.approx(2.0) and pos["last_mark_date"] == AS_OF
        assert pos["mark_source"] == "stale" and pos["stale_days"] == 1
        r = opl.run_for_date("2026-09-04", quotes_fn=_none_quotes, signals=[])     # 同日重跑
        assert r["positions"][0]["stale_days"] == 1
        r = opl.run_for_date("2026-09-08", quotes_fn=_none_quotes, signals=[])
        assert r["positions"][0]["stale_days"] == 2
        assert r["equity_snapshot"]["stale_positions"] == 1

    def test_half_quote_is_stale(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])
        r = opl.run_for_date("2026-09-04", quotes_fn=_quotes_fn({CALL: _q(1.4, 1.6), PUT: _q(0.0, 1.6, ok=False)}), signals=[])
        assert r["positions"][0]["mark_source"] == "stale"


# ==================== 出场 ====================

class TestExits:

    def test_post_event_exit_long_at_bid_positive_pnl(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])   # long 1× @2.2 (ask)
        r = opl.run_for_date("2026-09-20", quotes_fn=_quotes_fn({CALL: _q(2.9, 3.1), PUT: _q(2.9, 3.1)}), signals=[])
        assert len(r["positions"]) == 1                     # as_of == earnings_date：还没过
        r = opl.run_for_date("2026-09-21", quotes_fn=_quotes_fn({CALL: _q(2.9, 3.1), PUT: _q(2.9, 3.1)}), signals=[])
        assert r["positions"] == [] and len(r["closed_today"]) == 1
        t = r["closed_today"][0]
        assert t["exit_reason"] == "post_event" and t["mark_source"] == "cboe_mid"
        assert t["exit_call"] == 2.9 and t["exit_premium"] == pytest.approx(5.8)     # 按 bid 卖
        assert t["pnl_usd"] == pytest.approx((5.8 - 2.2) * 100) and t["pnl_usd"] > 0
        assert t["pnl_pct"] == pytest.approx((5.8 - 2.2) / 2.2 * 100)
        assert r["cash"] == pytest.approx(20_000 - 220 + 580)
        assert r["nav"] == pytest.approx(r["cash"])

    def test_post_event_exit_short_at_ask_negative_pnl(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="rich")])   # short 2× @1.8 (bid)
        r = opl.run_for_date("2026-09-21", quotes_fn=_quotes_fn({CALL: _q(2.9, 3.1), PUT: _q(2.9, 3.1)}), signals=[])
        t = r["closed_today"][0]
        assert t["side"] == "short" and t["exit_premium"] == pytest.approx(6.2)      # 按 ask 买回
        assert t["pnl_usd"] == pytest.approx((1.8 - 6.2) * 100 * 2) and t["pnl_usd"] < 0
        assert r["cash"] == pytest.approx(20_000 + 360 - 1240)

    def test_short_wins_when_straddle_collapses(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="rich")])
        r = opl.run_for_date("2026-09-21", quotes_fn=_quotes_fn({CALL: _q(0.4, 0.6), PUT: _q(0.4, 0.6)}), signals=[])
        assert r["closed_today"][0]["pnl_usd"] == pytest.approx((1.8 - 1.2) * 100 * 2)

    def test_intrinsic_fallback_after_stale_window(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])   # K=100
        closes = {"2026-09-24": 112.0}
        for d in ("2026-09-21", "2026-09-22", "2026-09-23"):
            r = opl.run_for_date(d, quotes_fn=_none_quotes, signals=[], closes_fn=lambda tk, dd: closes.get(dd))
            assert len(r["positions"]) == 1, d                # stale 1..3 ≤ 3：等
        r = opl.run_for_date("2026-09-24", quotes_fn=_none_quotes, signals=[], closes_fn=lambda tk, dd: closes.get(dd))
        assert r["positions"] == []
        t = r["closed_today"][0]
        assert t["mark_source"] == "intrinsic" and t["exit_reason"] == "post_event"
        assert t["exit_premium"] == pytest.approx(12.0) and t["exit_underlying"] == 112.0
        assert t["exit_call"] is None and "INTRINSIC" in t["rationale"]
        assert t["pnl_usd"] == pytest.approx((12.0 - 2.2) * 100)

    def test_intrinsic_needs_a_close_otherwise_hold(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])
        for d in ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"):
            r = opl.run_for_date(d, quotes_fn=_none_quotes, signals=[], closes_fn=lambda tk, dd: None)
        assert len(r["positions"]) == 1 and r["positions"][0]["stale_days"] == 4
        _state_files_finite()

    def test_expiry_buffer_exit(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap", earnings="2026-10-01")])
        r = opl.run_for_date("2026-09-29", quotes_fn=_quotes_fn({CALL: _q(0.9, 1.1), PUT: _q(0.9, 1.1)}), signals=[])
        assert len(r["positions"]) == 1                     # 3 天：不动
        r = opl.run_for_date("2026-09-30", quotes_fn=_quotes_fn({CALL: _q(0.9, 1.1), PUT: _q(0.9, 1.1)}), signals=[])
        assert r["positions"] == [] and r["closed_today"][0]["exit_reason"] == "expiry_buffer"
        assert r["closed_today"][0]["exit_premium"] == pytest.approx(1.8)

    def test_expired_without_quotes_settles_intrinsic_immediately(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap", earnings="2026-10-01")])
        r = opl.run_for_date("2026-10-02", quotes_fn=_none_quotes, signals=[], closes_fn=lambda tk, d: 95.0)
        t = r["closed_today"][0]
        assert t["mark_source"] == "intrinsic" and t["exit_premium"] == pytest.approx(5.0)

    def test_exit_after_event_disabled_holds_until_buffer(self, monkeypatch):
        monkeypatch.setitem(opl.CONFIG, "exit_after_event", False)
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])
        r = opl.run_for_date("2026-09-21", quotes_fn=_quotes_fn({CALL: _q(2.9, 3.1), PUT: _q(2.9, 3.1)}), signals=[])
        assert len(r["positions"]) == 1


# ==================== 状态卫生 ====================

class TestStateHygiene:

    def test_equity_dedup_and_identical_rerun(self):
        sigs = [_sig("AAA", "cheap"), _sig("BBB", "rich", c=(0.9, 1.1), p=(0.9, 1.1))]
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=sigs)
        snap1 = (opl.POSITIONS_FILE.read_text(), opl.EQUITY_FILE.read_text(),
                 json.loads(opl.META_FILE.read_text())["cash"])
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=sigs)
        snap2 = (opl.POSITIONS_FILE.read_text(), opl.EQUITY_FILE.read_text(),
                 json.loads(opl.META_FILE.read_text())["cash"])
        assert snap1 == snap2
        assert len(opl._load_jsonl(opl.EQUITY_FILE)) == 1
        assert len(opl._load_jsonl(opl.POSITIONS_FILE)) == 2

    def test_nan_in_signal_quote_never_reaches_state(self):
        bad = _sig("AAA", "cheap")
        bad["quote"]["call"]["ask"] = float("nan")
        bad["quote"]["call"]["mid"] = float("nan")
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[bad, _sig("BBB", "cheap")])
        assert [p["ticker"] for p in r["positions"]] == ["BBB"]
        assert r["skipped"][0]["reason"] == "fill price unavailable"
        _state_files_finite()

    def test_nan_in_requote_becomes_stale_not_state(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])
        nan_q = {"bid": float("nan"), "ask": 1.6, "mid": float("nan"), "quote_ok": True}
        r = opl.run_for_date("2026-09-04", quotes_fn=_quotes_fn({CALL: nan_q, PUT: _q(1.4, 1.6)}), signals=[])
        assert r["positions"][0]["mark_source"] == "stale" and r["positions"][0]["last_mark"] == pytest.approx(2.0)
        assert math.isfinite(r["nav"])
        _state_files_finite()

    def test_nan_cash_in_meta_refuses_to_run(self):
        opl.STATE_DIR.mkdir(parents=True)
        opl.META_FILE.write_text(json.dumps({"cash": None, "starting_capital": 20000}))
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig()])
        assert r.get("error") == "cash not finite" and not opl.POSITIONS_FILE.exists()

    def test_meta_config_snapshot_refreshed(self, monkeypatch):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[])
        monkeypatch.setitem(opl.CONFIG, "max_open", 9)
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[])
        m = json.loads(opl.META_FILE.read_text())
        assert m["config_snapshot"]["max_open"] == 9 and m["starting_date"] == AS_OF

    def test_quotes_fn_exception_is_stale_not_crash(self):
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])

        def boom(tk, syms):
            raise ConnectionError("cboe down")
        r = opl.run_for_date("2026-09-04", quotes_fn=boom, signals=[])
        assert r["positions"][0]["mark_source"] == "stale"


# ==================== KPI / Markdown ====================

class TestReporting:

    def test_kpis_and_markdown(self):
        sigs = [_sig("AAA", "cheap"), _sig("BBB", "rich", c=(0.9, 1.1), p=(0.9, 1.1)), _sig("CCC", "fair")]
        evs._write_jsonl(evs.SIGNALS_FILE, sigs)
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=sigs)
        A_C, A_P = CALL.replace("XYZ", "AAA"), PUT.replace("XYZ", "AAA")
        opl.run_for_date("2026-09-21", quotes_fn=_quotes_fn({A_C: _q(2.9, 3.1), A_P: _q(2.9, 3.1)}), signals=[])
        k = opl.compute_kpis()
        assert k["n"] == 1 and k["win_rate"] == 100.0 and k["by_side"]["long"]["n"] == 1
        assert k["by_label"]["cheap"]["total_pnl_usd"] == pytest.approx(360.0)
        assert k["open_positions"] == 1 and k["intrinsic_exits"] == 0
        md = opl.render_markdown(AS_OF)
        assert "## 期权纸面腿：财报跨式（观察项）" in md
        assert "| AAA | cheap |" in md and "| CCC | fair |" in md
        assert "| BBB | short | 2 |" in md
        assert "stale" in md                                   # BBB 9/21 没报价
        assert "无 delta 对冲" in md

    def test_markdown_empty_when_nothing(self):
        assert opl.render_markdown(AS_OF) == ""

    def test_default_signals_read_from_signal_file(self):
        evs._write_jsonl(evs.SIGNALS_FILE, [_sig("AAA", "cheap")])
        r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes)
        assert [p["ticker"] for p in r["positions"]] == ["AAA"]


# ==================== cboe_options.quote_contracts ====================

class TestQuoteContracts:

    def test_returns_held_contracts_and_none_for_missing(self, monkeypatch):
        import cboe_options as co
        from datetime import datetime
        payload = {"options": [
            {"option": CALL, "bid": 1.0, "ask": 1.2, "delta": 0.5, "iv": 0.3, "open_interest": 10},
            {"option": PUT, "bid": 0.0, "ask": 1.2, "delta": -0.5, "iv": 0.3, "open_interest": 10},
        ]}
        monkeypatch.setattr(co, "_fetch_cboe_payload", lambda tk, timeout, **kw: payload)
        monkeypatch.setattr(co, "_pdt_now", lambda: datetime(2026, 9, 3, 10, 0))
        monkeypatch.setattr(co, "_SNAPSHOT_PROVIDER", None)
        out = co.quote_contracts("XYZ", [CALL, PUT, "XYZ261002C00105000"])
        assert out[CALL]["quote_ok"] is True and out[CALL]["mid"] == pytest.approx(1.1)
        assert out[CALL]["role"] == "held" and out[CALL]["strike"] == 100.0
        assert out[CALL]["expiry"] == EXPIRY and out[CALL]["dte"] == 29
        assert out[PUT]["quote_ok"] is False and out[PUT]["mid"] is None
        assert out["XYZ261002C00105000"] is None

    def test_snapshot_mode_all_none(self, monkeypatch):
        import cboe_options as co
        monkeypatch.setattr(co, "_SNAPSHOT_PROVIDER", lambda tk: {})
        monkeypatch.setattr(co, "_fetch_cboe_payload", lambda *a, **k: pytest.fail("must not fetch"))
        assert co.quote_contracts("XYZ", [CALL]) == {CALL: None}

    def test_payload_failure_all_none(self, monkeypatch):
        import cboe_options as co
        monkeypatch.setattr(co, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(co, "_fetch_cboe_payload", lambda tk, timeout, **kw: None)
        assert co.quote_contracts("XYZ", [CALL]) == {CALL: None}
