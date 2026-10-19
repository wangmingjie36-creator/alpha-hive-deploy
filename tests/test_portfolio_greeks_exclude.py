"""v0.45.545 `hedge_exclude_tickers`：一只标的长期缺价，不该让整个组合每天都判「数据不全」。

背景（2026-10-15 / 10-16 生产审计）：两天里**只有 BILI 一只**缺价（CBOE 里它的标的价 last_trade 恒落后一天，
Twelve Data 当晚又不给当天那根）⇒ 整个组合 band_status=unknown、「no hedge on partial data」，其余 23 只
价格全好的仓也跟着停摆。用户决定：BILI 不进对冲计算。

写法约束：排除是**少算一块敞口**，所以每条断言都配一个「不排除就红」的对照（`TestControlWithoutExclusion`），
并且**排除必须看得见**——审计 / 日报 / 对冲页都要点名它与估算敞口，否则「排除」就成了把缺失改写成「没发生过」。
全部离线：价格 / 报价 / β 一律注入，三本账的状态目录重定向到 tmp_path。"""

import json

import pytest

import options_paper_leg as opl
import paper_portfolio as pp
import portfolio_greeks as pg
from alphabot import hedge as ahedge

AS_OF = "2026-09-03"
EXPIRY = "2026-10-02"
SPY = 650.0
BILI_ENTRY, BILI_SHARES = 14.43, 168.8064


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    hs = tmp_path / "hedge_state"
    for name, val in (("STATE_DIR", hs), ("POSITIONS_FILE", hs / "positions.jsonl"),
                      ("TRADES_FILE", hs / "trades.jsonl"), ("EQUITY_FILE", hs / "equity_curve.jsonl"),
                      ("META_FILE", hs / "meta.json"), ("BETA_CACHE_FILE", hs / "beta_cache.json")):
        monkeypatch.setattr(pg, name, val)
    ps, os_ = tmp_path / "paper_portfolio_state", tmp_path / "options_paper_state"
    for mod, d in ((pp, ps), (opl, os_)):
        monkeypatch.setattr(mod, "POSITIONS_FILE", d / "positions.jsonl")
        monkeypatch.setattr(mod, "EQUITY_FILE", d / "equity_curve.jsonl")
        monkeypatch.setattr(mod, "META_FILE", d / "meta.json")
    monkeypatch.setitem(pg.CONFIG, "rebalance_to", "edge")
    monkeypatch.setitem(pg.CONFIG, "beta_delta_band_pct", 15.0)
    monkeypatch.setitem(pg.CONFIG, "beta_delta_target_pct", 0.0)
    pg._BARS_CACHE.clear()
    return tmp_path


def _jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _stock(ticker, direction, shares, entry=100.0):
    return {"ticker": ticker, "direction": direction, "entry_date": "2026-08-25", "entry_price": entry,
            "shares": shares, "size_usd": shares * entry, "confidence": "high", "score": 7.5}


def _straddle(ticker):
    return {"ticker": ticker, "side": "long", "entry_date": "2026-09-01", "expiry": EXPIRY, "strike": 100.0,
            "call_symbol": f"{ticker}261002C00100000", "put_symbol": f"{ticker}261002P00100000",
            "contracts": 1, "entry_call": 4.0, "entry_put": 3.8, "entry_premium": 7.8, "entry_underlying": 100.0}


class _Recorder:
    """记下被问过价 / β / 报价的标的——「排除」要求整只不碰，不只是不进汇总。"""

    def __init__(self, prices, betas):
        self.prices, self.betas = prices, betas
        self.price_asked, self.beta_asked, self.quote_asked = [], [], []

    def closes(self, tk, as_of):
        self.price_asked.append(tk)
        return self.prices.get(tk)

    def beta(self, tk, as_of):
        self.beta_asked.append(tk)
        b = self.betas.get(tk)
        return (b, "ols60") if b is not None else (None, None)

    def quotes(self, tk, symbols):
        self.quote_asked.append(tk)
        return {s: None for s in symbols}


def _book(state, *, bili=True, aaa_shares=100.0):
    """AAA 看多（价全好）+ BILI 看空（无价）。AAA 名义 $10k / NAV $100k = 10%（在 ±15% 带内）。"""
    _jsonl(pp.EQUITY_FILE, [{"date": AS_OF, "nav": 100_000.0}])
    _jsonl(opl.EQUITY_FILE, [{"date": AS_OF, "nav": 100_000.0}])
    pos = [_stock("AAA", "bullish", aaa_shares)]
    if bili:
        pos.append(_stock("BILI", "bearish", BILI_SHARES, BILI_ENTRY))
    _jsonl(pp.POSITIONS_FILE, pos)
    return _Recorder({"AAA": 100.0, "SPY": SPY}, {"AAA": 1.0, "BILI": 0.9})   # BILI 无价


class TestControlWithoutExclusion:
    """对照：不排除时，同一本账因 BILI 缺价整天 unknown——证明下面的断言不是「本来就能决定」。"""

    def test_missing_bili_price_makes_the_whole_book_unknown(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", [])
        rec = _book(state)
        r = pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        assert r["aggregate"]["band_status"] == "unknown"
        assert "price missing" in r["recommendation"]["reason"]
        assert r["excluded_from_hedge"] is None          # 没配置排除 ⇒ None（不是空壳，也不是 0）


class TestExcludedTickerDoesNotBlockTheBook:
    def test_band_is_decided_on_the_remaining_rows(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state)
        r = pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        agg = r["aggregate"]
        assert agg["band_status"] == "inside" and agg["partial"] is False
        assert agg["coverage"]["n_price_missing"] == 0
        assert [x["ticker"] for x in r["rows"] if x["kind"] == "stock"] == ["AAA"]
        assert "partial data" not in r["recommendation"]["reason"]
        assert r["price_check"]["hedge_undecided"] is None

    def test_decision_uses_only_priced_rows_so_a_breach_still_trades(self, state, monkeypatch):
        """不是「排除了就一律 hold」：剩下的仓照样能判出带外并给出 SPY 建议。"""
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state, aaa_shares=2000.0)            # $200k / $100k NAV = 200% ⇒ 远超 +15% 带
        r = pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        assert r["aggregate"]["band_status"] == "above"
        assert r["recommendation"]["action"] == "sell_spy" and r["recommendation"]["spy_shares"] < 0

    def test_ticker_match_ignores_case(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["bili"])
        rec = _book(state)
        r = pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        assert r["aggregate"]["band_status"] == "inside"

    def test_other_missing_prices_still_block(self, state, monkeypatch):
        """排除只给 BILI，不是「缺价就放行」：别的标的缺价照旧 unknown。"""
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state)
        _jsonl(pp.POSITIONS_FILE, [_stock("AAA", "bullish", 100.0), _stock("ZZZ", "bullish", 10.0),
                                   _stock("BILI", "bearish", BILI_SHARES, BILI_ENTRY)])
        r = pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)   # ZZZ 无价
        assert r["aggregate"]["band_status"] == "unknown"
        assert "1 price missing" in r["recommendation"]["reason"]


class TestExcludedTickerIsNeverTouched:
    def test_no_price_beta_or_quote_lookup_for_it(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state)
        _jsonl(opl.POSITIONS_FILE, [_straddle("BILI"), _straddle("AAA")])
        pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        assert "BILI" not in rec.price_asked and "BILI" not in rec.beta_asked and "BILI" not in rec.quote_asked
        assert "AAA" in rec.price_asked and "AAA" in rec.quote_asked        # 对照：别的标的照常被问

    def test_straddle_legs_of_it_are_not_rows(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state, bili=False)
        _jsonl(opl.POSITIONS_FILE, [_straddle("BILI")])
        r = pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        assert not [x for x in r["rows"] if x["ticker"] == "BILI"]
        assert r["excluded_from_hedge"]["names"][0]["n_straddles"] == 1


class TestExclusionIsVisibleNotSilent:
    def test_exposure_estimate_is_recorded_signed_and_in_pct_nav(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state)
        r = pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        ex = r["excluded_from_hedge"]
        assert ex["configured"] == ["BILI"]
        (n,) = ex["names"]
        assert n["ticker"] == "BILI" and n["n_stock_positions"] == 1 and n["basis"] == "entry_price"
        assert n["stock_usd_est"] == pytest.approx(-BILI_SHARES * BILI_ENTRY, abs=0.01)      # bearish ⇒ 负
        assert n["stock_pct_nav_est"] == pytest.approx(n["stock_usd_est"] / r["nav"]["nav"] * 100, abs=0.01)

    def test_warning_is_logged_every_run(self, state, monkeypatch, caplog):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state)
        with caplog.at_level("WARNING"):
            pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        assert any("BILI" in m and "hedge_exclude_tickers" in m for m in caplog.messages)

    def test_configured_but_not_held_records_an_empty_list(self, state, monkeypatch):
        """配了排除、今天却没这只持仓：names 为空——「它没起作用」本身也要看得出来。"""
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state, bili=False)
        r = pg.compute_day(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        assert r["excluded_from_hedge"] == {**r["excluded_from_hedge"], "configured": ["BILI"], "names": []}

    def test_audit_file_markdown_and_page_brief_all_name_it(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        rec = _book(state)
        pg.run_for_date(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        audit = json.loads((pg.STATE_DIR / f"greeks_{AS_OF}.json").read_text(encoding="utf-8"))
        assert audit["excluded_from_hedge"]["names"][0]["ticker"] == "BILI"
        md = pg.render_markdown(AS_OF)
        assert "不进对冲计算" in md and "BILI" in md
        brief = ahedge.audit_brief(AS_OF, audit)
        assert brief["excluded_from_hedge"][0]["ticker"] == "BILI"
        assert brief["excluded_from_hedge"][0]["stock_usd_est"] == pytest.approx(-BILI_SHARES * BILI_ENTRY, abs=0.01)

    def test_markdown_has_no_exclusion_line_when_nothing_is_excluded(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", [])
        rec = _book(state, bili=False)
        pg.run_for_date(AS_OF, closes_fn=rec.closes, quotes_fn=rec.quotes, beta_fn=rec.beta)
        assert "不进对冲计算" not in pg.render_markdown(AS_OF)
