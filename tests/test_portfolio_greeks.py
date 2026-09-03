"""v0.45.103 组合 Greeks 聚合 + β·Delta 带状对冲 + 压力网格（portfolio_greeks）。
全部离线：价格 / 报价 / β 一律注入，三本账的状态目录全部重定向到 tmp_path。"""

import json
import math

import pytest

import options_paper_leg as opl
import paper_portfolio as pp
import portfolio_greeks as pg
from greeks_engine import bs_price, calculate_single

AS_OF = "2026-09-03"
EXPIRY = "2026-10-02"          # 29 DTE
SPY = 650.0
CALL, PUT = "XYZ261002C00100000", "XYZ261002P00100000"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    hs = tmp_path / "hedge_state"
    monkeypatch.setattr(pg, "STATE_DIR", hs)
    monkeypatch.setattr(pg, "POSITIONS_FILE", hs / "positions.jsonl")
    monkeypatch.setattr(pg, "TRADES_FILE", hs / "trades.jsonl")
    monkeypatch.setattr(pg, "EQUITY_FILE", hs / "equity_curve.jsonl")
    monkeypatch.setattr(pg, "META_FILE", hs / "meta.json")
    monkeypatch.setattr(pg, "BETA_CACHE_FILE", hs / "beta_cache.json")
    ps = tmp_path / "paper_portfolio_state"
    monkeypatch.setattr(pp, "POSITIONS_FILE", ps / "positions.jsonl")
    monkeypatch.setattr(pp, "EQUITY_FILE", ps / "equity_curve.jsonl")
    monkeypatch.setattr(pp, "META_FILE", ps / "meta.json")
    os_ = tmp_path / "options_paper_state"
    monkeypatch.setattr(opl, "POSITIONS_FILE", os_ / "positions.jsonl")
    monkeypatch.setattr(opl, "EQUITY_FILE", os_ / "equity_curve.jsonl")
    monkeypatch.setattr(opl, "META_FILE", os_ / "meta.json")
    monkeypatch.setitem(pg.CONFIG, "rebalance_to", "edge")
    monkeypatch.setitem(pg.CONFIG, "beta_delta_band_pct", 15.0)
    monkeypatch.setitem(pg.CONFIG, "beta_delta_target_pct", 0.0)
    pg._BARS_CACHE.clear()
    return tmp_path


# ── 夹具工厂 ──────────────────────────────────────────────────────────────────

def _jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _stock(ticker, direction, shares, entry=100.0):
    return {"ticker": ticker, "direction": direction, "entry_date": "2026-08-25", "entry_price": entry,
            "sl_price": entry * 0.93, "tp_price": entry * 1.15, "shares": shares, "size_usd": shares * entry,
            "time_stop_date": "2026-09-08", "confidence": "high", "score": 7.5, "rationale": "t"}


def _straddle(side="long", contracts=2, ticker="XYZ", strike=100.0):
    return {"ticker": ticker, "side": side, "entry_date": "2026-09-01", "expiry": EXPIRY, "strike": strike,
            "call_symbol": CALL.replace("XYZ", ticker), "put_symbol": PUT.replace("XYZ", ticker),
            "contracts": contracts, "entry_call": 4.0, "entry_put": 3.8, "entry_premium": 7.8,
            "entry_underlying": 100.0, "earnings_date": "2026-09-20", "signal_ratio": 0.5, "label": "cheap",
            "size_usd": 7.8 * 100 * contracts, "last_mark": 7.8, "last_mark_date": "2026-09-01",
            "mark_source": "cboe_mid"}


def _bs_quote(S, K, T_days, iv, cp):
    """用 greeks_engine 造一张「CBOE 形状」的合约：vega 每 vol 点、theta 每日（与实盘校核一致）。"""
    g = calculate_single(S, K, T_days / 365.0, pg.CONFIG["risk_free"], iv, cp)
    mid = round(g["price"], 4)
    return {"bid": round(mid - 0.05, 4), "ask": round(mid + 0.05, 4), "mid": mid, "iv": iv,
            "delta": g["delta"], "gamma": g["gamma"], "vega": g["vega"], "theta": g["theta"],
            "dte": T_days, "quote_ok": True, "role": "held"}


def _closes(table):
    def fn(ticker, as_of):
        return table.get(ticker)
    return fn


def _betas(table):
    def fn(ticker, as_of):
        b = table.get(ticker)
        return (b, "ols60") if b is not None else (None, None)
    return fn


def _quotes(table):
    def fn(ticker, symbols):
        return {s: table.get(s) for s in symbols}
    return fn


def _seed_navs(tmp, stock_nav=100_000.0, straddle_nav=100_000.0):
    _jsonl(pp.EQUITY_FILE, [{"date": "2026-09-02", "nav": stock_nav - 1}, {"date": AS_OF, "nav": stock_nav}])
    _jsonl(opl.EQUITY_FILE, [{"date": AS_OF, "nav": straddle_nav}])


def _all_finite(obj, path="$"):
    if isinstance(obj, float):
        assert math.isfinite(obj), f"{path} = {obj!r}"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _all_finite(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _all_finite(v, f"{path}[{i}]")


# ── 股票暴露 ──────────────────────────────────────────────────────────────────

class TestStockExposures:
    def test_signs_and_beta_dollar_delta(self):
        rows = pg.stock_exposures(AS_OF, positions=[_stock("AAA", "bullish", 10), _stock("BBB", "bearish", 4)],
                                  closes_fn=_closes({"AAA": 50.0, "BBB": 200.0}),
                                  beta_fn=_betas({"AAA": 1.2, "BBB": 0.5}))
        a, b = rows
        assert a["qty"] == 10 and a["dollar_delta"] == 500.0 and a["beta_dollar_delta"] == 600.0
        assert b["qty"] == -4 and b["dollar_delta"] == -800.0 and b["beta_dollar_delta"] == -400.0
        assert a["beta_source"] == "ols60" and a["gamma_dollar_per_1pct"] == 0.0

    def test_missing_price_and_beta_make_partial_unknown_and_no_hedge(self):
        rows = pg.stock_exposures(AS_OF, positions=[_stock("AAA", "bullish", 1000), _stock("BBB", "bullish", 10)],
                                  closes_fn=_closes({"AAA": 500.0}),           # BBB 无价
                                  beta_fn=_betas({"BBB": 1.0}))                # AAA 无 β
        agg = pg.aggregate(rows, nav=100_000.0)
        assert agg["partial"] is True
        assert agg["coverage"]["n_price_missing"] == 1 and agg["coverage"]["n_beta_missing"] == 1
        assert agg["coverage"]["beta_coverage"] == 0.5
        # AAA 的 $500k 名义如果 β 已知早就 above 了——但我们不知道，所以 unknown
        assert agg["band_status"] == "unknown"
        rec = pg.hedge_recommendation(agg, SPY, 100_000.0)
        assert rec["action"] == "hold" and rec["spy_shares"] == 0
        assert "partial" in rec["reason"] and "beta missing" in rec["reason"] and "price missing" in rec["reason"]

    def test_nan_price_is_missing_not_a_number(self):
        rows = pg.stock_exposures(AS_OF, positions=[_stock("AAA", "bullish", 10)],
                                  closes_fn=_closes({"AAA": float("nan")}), beta_fn=_betas({"AAA": 1.0}))
        assert rows[0]["price"] is None and rows[0]["price_missing"] is True
        assert rows[0]["dollar_delta"] is None and rows[0]["beta_dollar_delta"] is None


# ── 期权暴露 ──────────────────────────────────────────────────────────────────

class TestOptionExposures:
    S, K, IV = 100.0, 100.0, 0.40

    def _quotes_table(self, ticker="XYZ"):
        return {CALL.replace("XYZ", ticker): _bs_quote(self.S, self.K, 29, self.IV, "call"),
                PUT.replace("XYZ", ticker): _bs_quote(self.S, self.K, 29, self.IV, "put")}

    def test_long_atm_straddle_small_delta_positive_gamma_vega_negative_theta(self):
        rows = pg.option_exposures(AS_OF, positions=[_straddle("long", 2)], quotes_fn=_quotes(self._quotes_table()),
                                   closes_fn=_closes({"XYZ": self.S}), beta_fn=_betas({"XYZ": 1.1}))
        assert len(rows) == 2 and all(r["qty"] == 200 for r in rows)
        agg = pg.aggregate(rows, nav=100_000.0)
        s = agg["sums"]
        assert abs(s["dollar_delta"]) < 0.10 * 200 * self.S          # ATM 跨式净 delta ≈ 2N(d1)−1 ≈ +0.07/股
        assert s["gamma_dollar_per_1pct"] > 0 and s["vega_dollar_per_pt"] > 0 and s["theta_dollar_per_day"] < 0
        assert agg["partial"] is False and agg["band_status"] == "inside"

    def test_short_is_mirror_of_long(self):
        kw = dict(quotes_fn=_quotes(self._quotes_table()), closes_fn=_closes({"XYZ": self.S}), beta_fn=_betas({"XYZ": 1.1}))
        lo = pg.aggregate(pg.option_exposures(AS_OF, positions=[_straddle("long", 2)], **kw), 1e5)["sums"]
        sh = pg.aggregate(pg.option_exposures(AS_OF, positions=[_straddle("short", 2)], **kw), 1e5)["sums"]
        for k in pg._GREEK_KEYS:
            assert lo[k] == pytest.approx(-sh[k], abs=0.01), k

    def test_unit_scaling_against_greeks_engine(self):
        """$Vega/pt = qty×vega（每 vol 点）；$Theta/日 = qty×theta；$Delta = qty×delta×S；
        $Gamma(1%) = ½·qty·gamma·(0.01S)²——全部对 calculate_single 的定义逐项核。"""
        g = calculate_single(self.S, self.K, 29 / 365.0, pg.CONFIG["risk_free"], self.IV, "call")
        pos = _straddle("long", 3)
        rows = pg.option_exposures(AS_OF, positions=[pos], quotes_fn=_quotes(self._quotes_table()),
                                   closes_fn=_closes({"XYZ": self.S}), beta_fn=_betas({"XYZ": 1.0}))
        c = next(r for r in rows if r["cp"] == "call")
        qty = 300
        assert c["vega_dollar_per_pt"] == pytest.approx(qty * g["vega"], abs=0.01)
        assert c["theta_dollar_per_day"] == pytest.approx(qty * g["theta"], abs=0.01)
        assert c["dollar_delta"] == pytest.approx(qty * g["delta"] * self.S, abs=0.01)
        assert c["gamma_dollar_per_1pct"] == pytest.approx(0.5 * qty * g["gamma"] * (0.01 * self.S) ** 2, abs=0.01)
        # 一个 vol 点的 P&L 数量级 sanity：IV 0.40→0.41 应约等于 $Vega/pt
        p0 = bs_price(self.S, self.K, 29 / 365.0, pg.CONFIG["risk_free"], 0.40, "call")
        p1 = bs_price(self.S, self.K, 29 / 365.0, pg.CONFIG["risk_free"], 0.41, "call")
        assert qty * (p1 - p0) == pytest.approx(c["vega_dollar_per_pt"], rel=0.02)

    def test_missing_quote_flags_row_and_band_unknown(self):
        table = self._quotes_table()
        table[PUT] = None
        rows = pg.option_exposures(AS_OF, positions=[_straddle("long", 2)], quotes_fn=_quotes(table),
                                   closes_fn=_closes({"XYZ": self.S}), beta_fn=_betas({"XYZ": 1.0}))
        put = next(r for r in rows if r["cp"] == "put")
        assert put["quote_missing"] is True and put["vega_dollar_per_pt"] is None and put["dollar_delta"] is None
        agg = pg.aggregate(rows, 1e5)
        assert agg["coverage"]["n_quote_missing"] == 1 and agg["band_status"] == "unknown"

    def test_nan_in_quote_is_treated_as_missing(self):
        table = self._quotes_table()
        table[CALL]["vega"] = float("nan")
        rows = pg.option_exposures(AS_OF, positions=[_straddle("long", 2)], quotes_fn=_quotes(table),
                                   closes_fn=_closes({"XYZ": self.S}), beta_fn=_betas({"XYZ": 1.0}))
        call = next(r for r in rows if r["cp"] == "call")
        assert call["quote_missing"] is True
        _all_finite(rows)


# ── 带状逻辑 ──────────────────────────────────────────────────────────────────

class TestBandLogic:
    NAV = 200_000.0

    def _agg(self, beta_dd):
        rows = [{"kind": "stock", "ticker": "AAA", "qty": 1, "price": 1.0, "dollar_delta": beta_dd,
                 "beta": 1.0, "beta_source": "ols60", "beta_dollar_delta": beta_dd,
                 "gamma_dollar_per_1pct": 0.0, "vega_dollar_per_pt": 0.0, "theta_dollar_per_day": 0.0}]
        return pg.aggregate(rows, self.NAV)

    def test_inside_holds(self):
        agg = self._agg(0.10 * self.NAV)
        assert agg["band_status"] == "inside"
        rec = pg.hedge_recommendation(agg, SPY, self.NAV)
        assert rec["action"] == "hold" and rec["spy_shares"] == 0 and "inside" in rec["reason"]

    def test_above_sells_spy_to_edge(self):
        agg = self._agg(0.30 * self.NAV)                     # +30% NAV，带边 +15%
        assert agg["band_status"] == "above"
        rec = pg.hedge_recommendation(agg, SPY, self.NAV)
        excess = 0.30 * self.NAV - 0.15 * self.NAV            # $30,000
        assert rec["action"] == "sell_spy"
        assert rec["spy_shares"] < 0
        assert abs(rec["spy_shares"]) == math.ceil(excess / SPY)
        assert rec["excess_usd"] == pytest.approx(excess) and rec["target_usd"] == pytest.approx(0.15 * self.NAV)

    def test_above_sells_spy_to_center(self, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "rebalance_to", "center")
        agg = self._agg(0.30 * self.NAV)
        rec = pg.hedge_recommendation(agg, SPY, self.NAV)
        assert rec["action"] == "sell_spy"
        assert abs(rec["spy_shares"]) == round(0.30 * self.NAV / SPY)
        assert rec["target_usd"] == 0.0

    def test_below_buys_spy(self):
        agg = self._agg(-0.40 * self.NAV)
        assert agg["band_status"] == "below"
        rec = pg.hedge_recommendation(agg, SPY, self.NAV)
        assert rec["action"] == "buy_spy" and rec["spy_shares"] > 0
        assert rec["spy_shares"] == math.ceil(0.25 * self.NAV / SPY)

    def test_edge_rebalance_lands_inside_band(self):
        agg = self._agg(0.30 * self.NAV)
        rec = pg.hedge_recommendation(agg, SPY, self.NAV)
        after = 0.30 * self.NAV + rec["spy_shares"] * SPY
        assert after / self.NAV * 100 <= 15.0 + 1e-9

    def test_no_spy_price_holds_with_reason(self):
        agg = self._agg(0.30 * self.NAV)
        rec = pg.hedge_recommendation(agg, None, self.NAV)
        assert rec["action"] == "hold" and "SPY price unavailable" in rec["reason"]

    def test_alerts(self):
        rows = [{"kind": "option", "ticker": "X", "qty": 100, "price": 1.0, "dollar_delta": 0.0, "beta": 1.0,
                 "beta_dollar_delta": 0.0, "gamma_dollar_per_1pct": 600.0, "vega_dollar_per_pt": 1500.0,
                 "theta_dollar_per_day": -50.0}]
        agg = pg.aggregate(rows, 100_000.0)
        assert agg["vega_alert"] is True and agg["gamma_alert"] is True


# ── 覆盖账本 ──────────────────────────────────────────────────────────────────

def _book_above(tmp, nav_each=100_000.0):
    """股票账：AAA 多头 β=1.0，$Delta = 60% 合并 NAV → above。"""
    _seed_navs(tmp, nav_each, nav_each)
    total = 2 * nav_each
    _jsonl(pp.POSITIONS_FILE, [_stock("AAA", "bullish", shares=0.60 * total / 100.0, entry=100.0)])
    closes = _closes({"AAA": 100.0, "SPY": SPY})
    betas = _betas({"AAA": 1.0})
    return closes, betas, total


def _snapshot_dir(d):
    return {p.name: p.read_bytes() for p in sorted(d.glob("*")) if p.is_file()}


class TestOverlay:
    def test_execute_then_rerun_is_idempotent(self, state):
        closes, betas, total = _book_above(state)
        r1 = pg.run_for_date(AS_OF, closes_fn=closes, quotes_fn=_quotes({}), beta_fn=betas)
        assert r1["executed"] is True and r1["executed_trade"]["shares"] < 0
        expect = math.ceil((0.60 - 0.15) * total / SPY)
        assert r1["executed_trade"]["shares"] == -expect
        assert r1["aggregate"]["band_status"] == "above" and r1["aggregate_after"]["band_status"] == "inside"
        snap1 = _snapshot_dir(pg.STATE_DIR)
        assert {"positions.jsonl", "trades.jsonl", "equity_curve.jsonl", "meta.json", f"greeks_{AS_OF}.json"} <= set(snap1)
        r2 = pg.run_for_date(AS_OF, closes_fn=closes, quotes_fn=_quotes({}), beta_fn=betas)
        assert _snapshot_dir(pg.STATE_DIR) == snap1
        assert len(pg._load_jsonl(pg.TRADES_FILE)) == 1
        assert r2["recommendation"] == r1["recommendation"]        # 交易前视角重建成功
        assert r2["executed_trade"]["shares"] == -expect

    def test_mtm_sign_when_spy_moves(self, state):
        closes, betas, _ = _book_above(state)
        pg.run_for_date(AS_OF, closes_fn=closes, quotes_fn=_quotes({}), beta_fn=betas)
        pos = pg._load_jsonl(pg.POSITIONS_FILE)[0]
        assert pos["shares"] < 0 and pos["avg_price"] == SPY
        eq0 = pg._load_jsonl(pg.EQUITY_FILE)[-1]
        assert eq0["nav"] == pytest.approx(0.0, abs=0.01)            # 成交=收盘，开仓瞬间净值 0
        # 次日 SPY +2%：空头亏钱
        nxt = "2026-09-04"
        _jsonl(pp.EQUITY_FILE, pg._load_jsonl(pp.EQUITY_FILE) + [{"date": nxt, "nav": 100_000.0}])
        _jsonl(opl.EQUITY_FILE, pg._load_jsonl(opl.EQUITY_FILE) + [{"date": nxt, "nav": 100_000.0}])
        up = _closes({"AAA": 100.0, "SPY": SPY * 1.02})
        r = pg.run_for_date(nxt, closes_fn=up, quotes_fn=_quotes({}), beta_fn=betas)
        eq1 = pg._load_jsonl(pg.EQUITY_FILE)[-1]
        assert eq1["date"] == nxt
        assert eq1["nav"] == pytest.approx(pos["shares"] * SPY * 0.02, rel=1e-6)
        assert eq1["nav"] < 0
        assert len(pg._load_jsonl(pg.EQUITY_FILE)) == 2                  # 两天两行

    def test_equity_dedup_same_day(self, state):
        closes, betas, _ = _book_above(state)
        for _ in range(3):
            pg.run_for_date(AS_OF, closes_fn=closes, quotes_fn=_quotes({}), beta_fn=betas)
        assert len(pg._load_jsonl(pg.EQUITY_FILE)) == 1

    def test_nan_never_reaches_state(self, state):
        closes, betas, _ = _book_above(state)
        table = {CALL: _bs_quote(100.0, 100.0, 29, 0.4, "call"), PUT: _bs_quote(100.0, 100.0, 29, 0.4, "put")}
        table[CALL]["mid"] = float("nan")
        table[PUT]["theta"] = float("inf")
        _jsonl(opl.POSITIONS_FILE, [_straddle("long", 1)])
        closes2 = _closes({"AAA": 100.0, "SPY": SPY, "XYZ": 100.0})
        pg.run_for_date(AS_OF, closes_fn=closes2, quotes_fn=_quotes(table), beta_fn=_betas({"AAA": 1.0, "XYZ": 1.0}))
        for p in pg.STATE_DIR.glob("*.json*"):
            txt = p.read_text(encoding="utf-8")
            assert "NaN" not in txt and "Infinity" not in txt, p.name
            if p.suffix == ".json":
                _all_finite(json.loads(txt))
            else:
                for line in txt.splitlines():
                    _all_finite(json.loads(line))

    def test_partial_book_never_trades(self, state):
        _seed_navs(state)
        _jsonl(pp.POSITIONS_FILE, [_stock("AAA", "bullish", 1500.0), _stock("BBB", "bullish", 10.0)])
        r = pg.run_for_date(AS_OF, closes_fn=_closes({"AAA": 100.0, "BBB": 50.0, "SPY": SPY}),
                            quotes_fn=_quotes({}), beta_fn=_betas({"AAA": 1.0}))      # BBB 无 β
        assert r["aggregate"]["band_status"] == "unknown" and r["executed"] is False
        assert not pg.TRADES_FILE.exists() and not pg.POSITIONS_FILE.exists()

    def test_dry_run_writes_no_ledger(self, state):
        closes, betas, _ = _book_above(state)
        r = pg.run_for_date(AS_OF, closes_fn=closes, quotes_fn=_quotes({}), beta_fn=betas, execute=False)
        assert r["recommendation"]["action"] == "sell_spy" and r["executed"] is False
        assert not pg.STATE_DIR.exists() or not any(pg.STATE_DIR.iterdir())


# ── 压力网格 ──────────────────────────────────────────────────────────────────

class TestStress:
    def _stock_rows(self, beta=1.5):
        return pg.stock_exposures(AS_OF, positions=[_stock("AAA", "bullish", 100)],
                                  closes_fn=_closes({"AAA": 100.0}), beta_fn=_betas({"AAA": beta}))

    def _cell(self, st, sp, iv):
        return next(c for c in st["cells"] if c["spot_pct"] == sp and c["iv_pts"] == iv)

    def test_stock_zero_cell_and_linearity_with_beta(self):
        st = pg.stress_table(self._stock_rows(beta=1.5))
        assert self._cell(st, 0, 0)["pnl"] == 0.0 and st["pnl_at_zero"] == 0.0
        # $10,000 名义 × β1.5 × ±5% = ±$750；±10% = ±$1,500（线性）
        assert self._cell(st, 5, 0)["pnl"] == pytest.approx(750.0)
        assert self._cell(st, 10, 0)["pnl"] == pytest.approx(1500.0)
        assert self._cell(st, -10, 20)["pnl"] == pytest.approx(-1500.0)     # 股票对 IV 不敏感
        assert st["partial"] is False and st["worst_cell"]["pnl"] == pytest.approx(-1500.0)
        assert st["worst_cell"]["pnl"] == min(c["pnl"] for c in st["cells"])

    def test_long_straddle_gains_from_iv_up_and_short_mirrors(self):
        table = {CALL: _bs_quote(100.0, 100.0, 29, 0.4, "call"), PUT: _bs_quote(100.0, 100.0, 29, 0.4, "put")}
        kw = dict(quotes_fn=_quotes(table), closes_fn=_closes({"XYZ": 100.0}), beta_fn=_betas({"XYZ": 1.0}))
        lo = pg.stress_table(pg.option_exposures(AS_OF, positions=[_straddle("long", 2)], **kw))
        sh = pg.stress_table(pg.option_exposures(AS_OF, positions=[_straddle("short", 2)], **kw))
        assert self._cell(lo, 0, 10)["pnl"] > 0
        assert self._cell(lo, 10, 0)["pnl"] > 0 and self._cell(lo, -10, 0)["pnl"] > 0     # 长 gamma 两边赢
        for c in lo["cells"]:
            assert c["pnl"] == pytest.approx(-self._cell(sh, c["spot_pct"], c["iv_pts"])["pnl"], abs=0.02)
        # 报价就是 BS 价 → 模型基差 ≈ 0，(0,0) 格 ≈ 0
        assert all(abs(g["gap"]) < 1e-3 for g in lo["bs_vs_mid_gap"])
        assert abs(lo["pnl_at_zero"]) < 0.5

    def test_missing_beta_flags_partial(self):
        rows = pg.stock_exposures(AS_OF, positions=[_stock("AAA", "bullish", 100), _stock("BBB", "bullish", 100)],
                                  closes_fn=_closes({"AAA": 100.0, "BBB": 100.0}), beta_fn=_betas({"AAA": 1.0}))
        st = pg.stress_table(rows)
        assert st["partial"] is True and all(c["partial"] for c in st["cells"])
        assert st["excluded"] == ["BBB(stock:no beta)"] and st["n_used"] == 1
        assert self._cell(st, 10, 0)["pnl"] == pytest.approx(1000.0)      # 只含 AAA

    def test_hedge_row_moves_with_spot(self):
        rows = pg.hedge_exposures(AS_OF, positions=[{"ticker": "SPY", "shares": -10, "avg_price": SPY}], spy_price=SPY)
        st = pg.stress_table(rows, SPY)
        assert self._cell(st, 10, 0)["pnl"] == pytest.approx(-10 * SPY * 0.10)


# ── 合并 NAV ──────────────────────────────────────────────────────────────────

class TestCombinedNav:
    def test_all_present(self, state):
        _seed_navs(state, 50_000.0, 100_000.0)
        d = pg.combined_nav_detail(AS_OF, _closes({"SPY": SPY}))
        assert d["missing"] == [] and d["nav"] == 150_000.0 and d["components"]["hedge_overlay"] == 0.0
        assert pg.combined_nav(AS_OF, _closes({"SPY": SPY})) == 150_000.0

    def test_leg_never_started_counts_as_zero(self, state):
        """跨式腿连净值文件都没有 = 合法零状态：按 0 计、标 not_started，不阻断对冲。"""
        _jsonl(pp.EQUITY_FILE, [{"date": AS_OF, "nav": 50_000.0}])          # 跨式腿从未建账
        assert not opl.EQUITY_FILE.exists()
        d = pg.combined_nav_detail(AS_OF, _closes({"SPY": SPY}))
        assert d["missing"] == [] and d["not_started"] == ["straddle_leg"]
        assert d["components"]["straddle_leg"] == 0.0 and d["nav"] == 50_000.0
        assert pg.combined_nav(AS_OF, _closes({"SPY": SPY})) == 50_000.0

    def test_missing_component_named(self, state):
        """文件存在但 as_of 之前没有一行 = 数据缺失：None 并点名。"""
        _jsonl(pp.EQUITY_FILE, [{"date": AS_OF, "nav": 50_000.0}])
        _jsonl(opl.EQUITY_FILE, [{"date": "2026-09-10", "nav": 100_000.0}])   # 只有未来行
        d = pg.combined_nav_detail(AS_OF, _closes({"SPY": SPY}))
        assert d["nav"] is None and d["missing"] == ["straddle_leg"] and d["not_started"] == []
        assert pg.combined_nav(AS_OF, _closes({"SPY": SPY})) is None

    def test_hedge_overlay_missing_when_has_position_but_no_spy_price(self, state):
        _seed_navs(state)
        _jsonl(pg.POSITIONS_FILE, [{"ticker": "SPY", "shares": -10, "avg_price": SPY}])
        pg._save_meta({"cash": 10 * SPY})
        d = pg.combined_nav_detail(AS_OF, _closes({}))
        assert d["nav"] is None and d["missing"] == ["hedge_overlay"]

    def test_future_equity_rows_ignored(self, state):
        _jsonl(pp.EQUITY_FILE, [{"date": AS_OF, "nav": 50_000.0}, {"date": "2026-09-10", "nav": 99.0}])
        _jsonl(opl.EQUITY_FILE, [{"date": AS_OF, "nav": 100_000.0}])
        assert pg.combined_nav(AS_OF, _closes({"SPY": SPY})) == 150_000.0


# ── β 估计 ────────────────────────────────────────────────────────────────────

class TestBeta:
    def _bars(self, n, slope, seed=7):
        import random
        rnd = random.Random(seed)
        from datetime import date, timedelta
        d0 = date(2026, 9, 3) - timedelta(days=int(n * 1.5))
        dates, s, b = [], 100.0, 400.0
        stock, bench = [], []
        while len(dates) < n:
            if d0.weekday() < 5:
                rb = rnd.gauss(0, 0.01)
                b *= math.exp(rb)
                s *= math.exp(slope * rb)
                dates.append(d0.isoformat())
                stock.append({"date": d0.isoformat(), "close": s})
                bench.append({"date": d0.isoformat(), "close": b})
            d0 += timedelta(days=1)
        return stock, bench

    def test_ols_recovers_slope(self):
        stock, bench = self._bars(90, slope=1.7)
        beta, n = pg._ols_beta(stock, bench, AS_OF, 60)
        assert n == 60 and beta == pytest.approx(1.7, abs=1e-9)

    def test_ols_none_when_too_short(self):
        stock, bench = self._bars(40, slope=1.0)
        assert pg._ols_beta(stock, bench, AS_OF, 60) is None

    def test_default_beta_uses_cache_within_5_trading_days_and_rejects_future(self, monkeypatch):
        stock, bench = self._bars(90, slope=1.3)
        monkeypatch.setattr(pg, "_default_bars", lambda t, d: bench if t == "SPY" else stock)
        b1, s1 = pg._default_beta("AAA", AS_OF)
        assert s1 == "ols60" and b1 == pytest.approx(1.3, abs=1e-4)
        assert pg.BETA_CACHE_FILE.exists()
        b2, s2 = pg._default_beta("AAA", "2026-09-04")
        assert s2 == "cache" and b2 == b1
        b3, s3 = pg._default_beta("AAA", "2026-09-10")            # 5 个交易日后重算
        assert s3 == "ols60"
        b4, s4 = pg._default_beta("AAA", "2026-08-20")            # 缓存来自未来 → 重算
        assert s4 == "ols60"

    def test_default_beta_none_when_bars_missing(self, monkeypatch):
        monkeypatch.setattr(pg, "_default_bars", lambda t, d: None)
        assert pg._default_beta("AAA", AS_OF) == (None, None)


# ── 报告与 CLI ────────────────────────────────────────────────────────────────

class TestReportAndCli:
    def test_render_empty_when_no_positions(self, state):
        _seed_navs(state)
        assert pg.render_markdown(AS_OF, result=pg.run_for_date(AS_OF, closes_fn=_closes({"SPY": SPY}),
                                                                 quotes_fn=_quotes({}), beta_fn=_betas({}),
                                                                 execute=False)) == ""

    def test_render_mentions_partial_and_recommendation(self, state):
        closes, betas, _ = _book_above(state)
        res = pg.run_for_date(AS_OF, closes_fn=closes, quotes_fn=_quotes({}), beta_fn=betas)
        md = pg.render_markdown(AS_OF, result=res)
        assert "## 组合 Greeks 与对冲" in md and "sell_spy" in md and "已执行" in md and "压力网格" in md
        md2 = pg.render_markdown(AS_OF)                                   # 从审计文件读
        assert md2 == md

    def test_cli_dry_run_json_returns_zero_and_writes_nothing(self, state, monkeypatch, capsys):
        closes, betas, _ = _book_above(state)
        monkeypatch.setattr(pg, "_default_close", closes)
        monkeypatch.setattr(pg, "_default_beta", betas)
        monkeypatch.setattr(pg, "_default_quotes", _quotes({}))
        rc = pg.main(["--date", AS_OF, "--dry-run", "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["recommendation"]["action"] == "sell_spy" and out["executed"] is False
        assert not pg.STATE_DIR.exists() or not any(pg.STATE_DIR.iterdir())
