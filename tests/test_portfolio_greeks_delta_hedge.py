"""逐票 Delta 对冲接进 `portfolio_greeks`（v0.45.526）的集成守卫。离线：价 / 报价 / β 一律注入，三本账与对冲账都在 tmp。

每组回答一句「谁会红」：
  · shadow（缺省）只算不成交：不写任何逐票账本文件、不进聚合，审计里有 `delta_hedge` 一节（`TestShadow`）；
  · paper 成交：成交 / 持仓 / 现金 / 净值快照，SPY 带看到的是对冲**之后**的残余暴露（`TestPaper`）；
  · 同日重跑幂等、带内不动、跨式平仓后清仓、盘中价拒绝成交（`TestPaperLifecycle`）；
  · 缺数据逐标的隔离、不连坐别的标的（`TestPerNameIsolation`）；
  · 历史闸放行 / 拦截、ledger_view、日报小节、告警（`TestReadSide`）；
  · 失败可见：坏模式拼写直接抛、坏持仓行判未知（`TestFailsLoud`）。
"""
from __future__ import annotations

import json
import math

import pytest

import options_paper_leg as opl
import paper_portfolio as pp
import portfolio_greeks as pg

AS_OF = "2026-09-03"
NEXT = "2026-09-04"
EXPIRY = "2026-10-02"
SPY = 650.0


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
    monkeypatch.setitem(pg.CONFIG, "delta_hedge_mode", "shadow")
    monkeypatch.setitem(pg.CONFIG, "delta_hedge_band_pct", 3.0)
    monkeypatch.setitem(pg.CONFIG, "delta_hedge_rebalance_to", "center")
    pg._BARS_CACHE.clear()
    return tmp_path


def _jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _straddle(ticker, side="long", contracts=10):
    call, put = f"{ticker}261002C00100000", f"{ticker}261002P00100000"
    return {"ticker": ticker, "side": side, "entry_date": "2026-09-01", "expiry": EXPIRY, "strike": 100.0,
            "call_symbol": call, "put_symbol": put, "contracts": contracts, "entry_call": 4.0, "entry_put": 3.8,
            "entry_premium": 7.8, "entry_underlying": 100.0, "earnings_date": "2026-09-20", "signal_ratio": 0.5,
            "label": "cheap", "size_usd": 7.8 * 100 * contracts, "last_mark": 7.8, "last_mark_date": "2026-09-01",
            "mark_source": "cboe_mid"}


def _q(delta, ok=True):
    return {"bid": 3.9, "ask": 4.1, "mid": 4.0, "iv": 0.3, "delta": delta, "gamma": 0.02, "vega": 0.15, "theta": -0.05,
            "dte": 29, "quote_ok": ok, "role": "held"}


def _quotes_for(ticker, call_delta, put_delta, ok=True):
    return {f"{ticker}261002C00100000": _q(call_delta, ok), f"{ticker}261002P00100000": _q(put_delta, ok)}


def _quotes(table):
    def fn(ticker, symbols):
        return {s: table.get(s) for s in symbols}
    return fn


def _closes(table):
    def fn(ticker, as_of):
        return table.get(ticker)
    return fn


def _betas(table):
    def fn(ticker, as_of):
        b = table.get(ticker, 1.0)
        return (b, "ols60") if b is not None else (None, None)
    return fn


def _seed(straddles, nav_each=100_000.0, day=AS_OF):
    _jsonl(pp.EQUITY_FILE, [{"date": day, "nav": nav_each}])
    _jsonl(opl.EQUITY_FILE, [{"date": day, "nav": nav_each}])
    _jsonl(opl.POSITIONS_FILE, straddles)


def _run(closes, quotes, betas=None, day=AS_OF, **kw):
    return pg.run_for_date(day, closes_fn=closes, quotes_fn=_quotes(quotes), beta_fn=betas or _betas({}), **kw)


def _names(res):
    return {d["ticker"]: d for d in res["delta_hedge"]["names"]}


# 一笔多头跨式：S=100，10 张（qty ±1000 股），call Δ 0.6 + put Δ −0.4 ⇒ 净 +0.2 × 1000 × 100 = +$20,000（NAV 20 万的 10%）
def _long_x(ticker="XYZ"):
    return [_straddle(ticker)], _quotes_for(ticker, 0.6, -0.4), {ticker: 100.0, "SPY": SPY}


class TestShadow:
    def test_shadow_computes_but_writes_no_ledger(self, state):
        st, q, px = _long_x()
        _seed(st)
        r = _run(_closes(px), q)
        d = _names(r)["XYZ"]
        assert r["delta_hedge"]["mode"] == "shadow" and d["status"] == "hedge"
        assert d["option_dd"] == pytest.approx(20_000.0) and d["target_shares"] == -200
        assert r["delta_hedge"]["executed"] == [] and r["delta_hedge"]["n_hedge"] == 1
        files = {p.name for p in pg.STATE_DIR.glob("*")}
        assert not {f for f in files if f.startswith("delta_")}, files          # 一个逐票账本文件都不许出现
        audit = json.loads((pg.STATE_DIR / f"greeks_{AS_OF}.json").read_text(encoding="utf-8"))
        assert audit["delta_hedge"]["mode"] == "shadow" and audit["delta_hedge"]["names"][0]["ticker"] == "XYZ"

    def test_shadow_does_not_change_the_spy_decision_or_aggregate(self, state):
        """shadow 与 off 的聚合 / SPY 建议逐位相同：它只多写一节审计，不碰决策。"""
        st, q, px = _long_x()
        _seed(st)
        shadow = _run(_closes(px), q, execute=False)
        pg.CONFIG["delta_hedge_mode"] = "off"
        off = _run(_closes(px), q, execute=False)
        assert off["delta_hedge"] is None
        for k in ("aggregate", "recommendation", "nav", "stress"):
            assert shadow[k] == off[k], k
        assert {r["kind"] for r in shadow["rows"]} == {"option"}

    def test_off_writes_no_section(self, state):
        st, q, px = _long_x()
        _seed(st)
        pg.CONFIG["delta_hedge_mode"] = "off"
        _run(_closes(px), q)
        audit = json.loads((pg.STATE_DIR / f"greeks_{AS_OF}.json").read_text(encoding="utf-8"))
        assert audit["delta_hedge"] is None
        assert audit["price_check"]["delta_hedge"]["mode"] == "off"

    def test_price_check_summary_is_present_in_shadow(self, state):
        st, q, px = _long_x()
        _seed(st)
        r = _run(_closes(px), q)
        s = r["price_check"]["delta_hedge"]
        assert s["mode"] == "shadow" and s["n_hedge"] == 1 and s["n_undecided"] == 0
        assert pg.price_check_stats()["delta_hedge"]["n_hedge"] == 1


class TestPaper:
    @pytest.fixture(autouse=True)
    def paper(self, state):
        pg.CONFIG["delta_hedge_mode"] = "paper"

    def test_executes_and_books_cash_positions_and_trade_with_provenance(self, state):
        st, q, px = _long_x()
        _seed(st)
        r = _run(_closes(px), q)
        assert [t["ticker"] for t in r["delta_hedge"]["executed"]] == ["XYZ"]
        pos = pg._load_jsonl(pg.STATE_DIR / "delta_positions.jsonl")
        assert pos[0]["ticker"] == "XYZ" and pos[0]["shares"] == -200 and pos[0]["avg_price"] == 100.0
        meta = json.loads((pg.STATE_DIR / "delta_meta.json").read_text(encoding="utf-8"))
        assert meta["cash"] == pytest.approx(20_000.0)                      # 卖 200 股 × 100
        tr = pg._load_jsonl(pg.STATE_DIR / "delta_trades.jsonl")
        assert len(tr) == 1 and tr[0]["shares"] == -200 and tr[0]["fill"] == "close"
        assert tr[0]["price_session"] == AS_OF and tr[0]["price_at_close"] is True and "price_source" in tr[0]
        snap = r["delta_equity_snapshot"]
        assert snap["nav"] == pytest.approx(0.0) and snap["marks_complete"] and snap["marks_at_close"]   # 现金 +2 万 − 空头市值 2 万

    def test_spy_band_sees_the_residual_after_the_hedge(self, state):
        """跨式 β·Δ = +$20,000，逐票对冲 −200 股 × β1.0 = −$20,000 ⇒ 聚合的 β·Δ ≈ 0；SPY 带不再对着它动手。"""
        st, q, px = _long_x()
        _seed(st)
        r = _run(_closes(px), q)
        agg = r["aggregate"]
        assert agg["sums"]["beta_dollar_delta"] == pytest.approx(0.0, abs=1.0)
        assert agg["band_status"] == "inside" and r["recommendation"]["action"] == "hold"
        assert agg["by_kind"]["delta_hedge"] == 1
        pg.CONFIG["delta_hedge_mode"] = "shadow"
        shadow = _run(_closes(px), q, execute=False)
        assert shadow["aggregate"]["sums"]["beta_dollar_delta"] == pytest.approx(20_000.0)     # 对照：不对冲时是 +2 万

    def test_nav_gains_the_delta_overlay_component_once_the_ledger_is_open(self, state):
        st, q, px = _long_x()
        _seed(st)
        r1 = _run(_closes(px), q)
        # paper 模式第一次跑时账还没开：分量也在（值 0.0），否则第一次与同日重跑的 NAV 分量不一样
        assert r1["nav"]["components"]["delta_overlay"] == pytest.approx(0.0)
        r2 = _run(_closes(px), q, day=NEXT)
        assert r2["nav"]["components"]["delta_overlay"] == pytest.approx(0.0)
        assert r2["nav"]["missing"] == []

    def test_nav_without_an_open_ledger_has_no_delta_component(self, state):
        st, q, px = _long_x()
        _seed(st)
        pg.CONFIG["delta_hedge_mode"] = "shadow"
        r = _run(_closes(px), q, execute=False)
        assert "delta_overlay" not in r["nav"]["components"]                    # 与旧行为逐字节相同

    def test_stress_treats_delta_hedge_rows_like_stock_with_beta(self, state):
        st, q, px = _long_x()
        _seed(st)
        r = _run(_closes(px), q)
        assert "delta_hedge:" not in " ".join(r["stress"].get("excluded") or [])
        assert r["stress"]["n_used"] >= 3                                         # 两个期权行 + 一个逐票对冲行


class TestPaperLifecycle:
    @pytest.fixture(autouse=True)
    def paper(self, state):
        pg.CONFIG["delta_hedge_mode"] = "paper"

    def test_same_day_rerun_is_idempotent_down_to_the_audit_bytes(self, state):
        st, q, px = _long_x()
        _seed(st)
        r1 = _run(_closes(px), q)
        snap1 = {p.name: p.read_bytes() for p in sorted(pg.STATE_DIR.glob("*")) if p.is_file()}
        r2 = _run(_closes(px), q)
        snap2 = {p.name: p.read_bytes() for p in sorted(pg.STATE_DIR.glob("*")) if p.is_file()}
        assert snap2 == snap1
        assert len(pg._load_jsonl(pg.STATE_DIR / "delta_trades.jsonl")) == 1
        assert r2["delta_hedge"]["names"] == r1["delta_hedge"]["names"]
        assert r2["recommendation"] == r1["recommendation"]

    def test_inside_band_next_day_does_not_trade(self, state):
        st, q, px = _long_x()
        _seed(st)
        _run(_closes(px), q)
        _seed(st, day=NEXT)
        # 次日 Delta 漂到 +$5,000（带 $6,000）：跨式 5,000 + 对冲 −20,000 ⇒ 净 −15,000 超带——这里改成小漂移：
        q2 = _quotes_for("XYZ", 0.55, -0.45)                       # 净 Δ = 0.1 × 1000 × 100 = +10,000；对冲 −20,000 ⇒ 净 −10,000 > 6,000
        r = _run(_closes(px), q2, day=NEXT)
        assert _names(r)["XYZ"]["status"] == "hedge" and _names(r)["XYZ"]["trade_shares"] == +100
        q3 = _quotes_for("XYZ", 0.575, -0.425)                     # 跨式 +15,000，对冲 −20,000 ⇒ 净 −5,000，带内
        r3 = _run(_closes(px), q3, day="2026-09-08")
        assert _names(r3)["XYZ"]["status"] == "inside" and _names(r3)["XYZ"]["trade_shares"] == 0

    def test_straddle_gone_clears_the_hedge(self, state):
        st, q, px = _long_x()
        _seed(st)
        _run(_closes(px), q)
        _seed([], day=NEXT)                                         # 跨式平仓了：持仓文件空
        r = _run(_closes(px), {}, day=NEXT)
        d = _names(r)["XYZ"]
        assert d["status"] == "exit" and d["trade_shares"] == +200 and d["has_straddle"] is False
        assert pg._load_jsonl(pg.STATE_DIR / "delta_positions.jsonl") == []                 # 清干净，不留孤儿股票
        assert json.loads((pg.STATE_DIR / "delta_meta.json").read_text(encoding="utf-8"))["cash"] == pytest.approx(0.0)

    def test_intraday_price_is_blocked_not_traded(self, state):
        st, q, px = _long_x()
        _seed(st)
        live = {"XYZ": {"price": 100.0, "source": "cboe_intraday", "session": AS_OF, "at_close": False, "live": True},
                "SPY": {"price": SPY, "source": "cboe_intraday", "session": AS_OF, "at_close": False, "live": True}}
        r = _run(_closes(live), q)
        assert r["delta_hedge"]["executed"] == [] and "XYZ" in r["delta_hedge"]["blocked"]
        assert "官方收盘" in r["delta_hedge"]["blocked"]["XYZ"]
        assert r["price_check"]["delta_hedge"]["n_blocked"] == 1
        assert not (pg.STATE_DIR / "delta_trades.jsonl").exists() or pg._load_jsonl(pg.STATE_DIR / "delta_trades.jsonl") == []
        assert r["aggregate"]["sums"]["beta_dollar_delta"] == pytest.approx(20_000.0)         # 没成交 ⇒ 聚合里没有对冲行

    def test_edge_mode_pulls_back_to_the_band_edge(self, state):
        pg.CONFIG["delta_hedge_rebalance_to"] = "edge"
        st, q, px = _long_x()
        _seed(st)
        r = _run(_closes(px), q)
        assert _names(r)["XYZ"]["trade_shares"] == -math.ceil((20_000 - 6_000) / 100)


class TestPerNameIsolation:
    @pytest.fixture(autouse=True)
    def paper(self, state):
        pg.CONFIG["delta_hedge_mode"] = "paper"

    def test_one_missing_quote_only_blocks_that_name(self, state):
        st = [_straddle("AAA"), _straddle("BBB")]
        q = {**_quotes_for("AAA", 0.6, -0.4), **_quotes_for("BBB", 0.6, -0.4, ok=False)}
        _seed(st)
        r = _run(_closes({"AAA": 100.0, "BBB": 100.0, "SPY": SPY}), q)
        n = _names(r)
        assert n["AAA"]["status"] == "hedge" and n["BBB"]["status"] == "unknown" and n["BBB"]["reason"] == "option_delta_unknown"
        assert [t["ticker"] for t in r["delta_hedge"]["executed"]] == ["AAA"]               # 别的标的照常成交
        s = r["price_check"]["delta_hedge"]
        assert s["n_undecided"] == 1 and s["undecided"][0]["ticker"] == "BBB"
        assert r["aggregate"]["band_status"] == "unknown"                                    # 聚合本来就缺那一行 ⇒ SPY 带不对冲

    def test_a_stale_price_for_one_name_isolates_it(self, state):
        st = [_straddle("AAA"), _straddle("BBB")]
        q = {**_quotes_for("AAA", 0.6, -0.4), **_quotes_for("BBB", 0.6, -0.4)}
        _seed(st)
        marks = {"AAA": 100.0, "SPY": SPY,
                 "BBB": {"price": 100.0, "source": "cboe_stale_intraday", "session": "2026-09-02", "at_close": False}}
        r = _run(_closes(marks), q)
        n = _names(r)
        assert n["BBB"]["status"] == "unknown" and n["BBB"]["reason"] in ("option_delta_unknown", "price_stale")
        assert n["AAA"]["status"] == "hedge"


class TestReadSide:
    def _paper_day(self):
        pg.CONFIG["delta_hedge_mode"] = "paper"
        st, q, px = _long_x()
        _seed(st)
        return _run(_closes(px), q)

    def test_history_gate_passes_proven_records_and_blocks_unproven(self, state):
        self._paper_day()
        d = pg.load_history()["delta"]
        assert len(d["trades"]) == 1 and len(d["equity"]) == 1 and d["excluded"] == {"trades": {}, "equity": {}}
        # 把成交的定价证明摘掉 ⇒ 不放行并计数
        tf = pg.STATE_DIR / "delta_trades.jsonl"
        rec = json.loads(tf.read_text(encoding="utf-8").splitlines()[0])
        rec.pop("price_session")
        tf.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        d2 = pg.load_history()["delta"]
        assert d2["trades"] == [] and d2["excluded"]["trades"] == {"no_provenance": 1}

    def test_history_gate_blocks_equity_rows_with_intraday_marks(self, state):
        self._paper_day()
        ef = pg.STATE_DIR / "delta_equity_curve.jsonl"
        rec = json.loads(ef.read_text(encoding="utf-8").splitlines()[0])
        rec.update(n_positions=1, marks_at_close=False)
        ef.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        assert pg.load_history()["delta"]["excluded"]["equity"] == {"not_its_session_close": 1}

    def test_existing_history_keys_are_untouched(self, state):
        self._paper_day()
        h = pg.load_history()
        assert {"trades", "equity", "audits", "excluded", "excluded_dates", "first_verified", "regressions"} <= set(h)
        assert set(h["excluded"]) == {"trades", "equity", "audits"}                 # 逐票对冲的拦截另放 history["delta"]

    def test_ledger_view_exposes_the_delta_ledger_only_when_opened(self, state, tmp_path):
        v0 = pg.ledger_view(tmp_path)
        assert v0["delta_opened"] is False and v0["delta_positions"] is None and "delta_positions" not in v0["errors"]
        self._paper_day()
        v1 = pg.ledger_view(tmp_path)
        assert v1["delta_opened"] is True and v1["delta_positions"][0]["ticker"] == "XYZ"
        assert v1["delta_meta"]["cash"] == pytest.approx(20_000.0)
        assert v1["rules"]["delta_hedge_band_pct"] == 3.0 and v1["rules"]["delta_hedge_mode"] == "paper"

    def test_deleted_positions_file_is_an_error_not_an_empty_book(self, state, tmp_path):
        self._paper_day()
        (pg.STATE_DIR / "delta_positions.jsonl").unlink()
        v = pg.ledger_view(tmp_path)
        assert "delta_positions" in v["errors"]

    def test_markdown_has_the_delta_section(self, state):
        r = self._paper_day()
        md = pg.render_markdown(AS_OF, r)
        assert "逐票 Delta 对冲" in md and "| XYZ |" in md and "纸面成交" in md and "没有点差" in md

    def test_markdown_is_unchanged_when_off(self, state):
        st, q, px = _long_x()
        _seed(st)
        pg.CONFIG["delta_hedge_mode"] = "off"
        assert "逐票 Delta 对冲" not in pg.render_markdown(AS_OF, _run(_closes(px), q))

    def test_alert_only_in_paper_mode_when_names_are_undecided(self, state):
        from alert_manager import AlertAnalyzer
        base = {"as_of": AS_OF, "n_stale": 0, "n_quote_stale": 0, "spy": {}, "hedge_undecided": None, "gaps": [],
                "execution_blocked": None}

        def run_check(dh):
            am = AlertAnalyzer.__new__(AlertAnalyzer)
            am.alerts, am.checks_skipped = [], []
            am._check_portfolio_greeks_prices({"scan_timing": {"counters": {"portfolio_greeks": {**base, "delta_hedge": dh}}}})
            return [a for a in am.alerts if "逐票 Delta 对冲" in a.message]
        und = [{"ticker": "BBB", "reason": "price_stale", "detail": "x"}]
        assert len(run_check({"mode": "paper", "n_undecided": 1, "undecided": und, "n_blocked": 0, "blocked": {}})) == 1
        assert len(run_check({"mode": "paper", "n_undecided": 0, "undecided": [], "n_blocked": 1, "blocked": {"X": "y"}})) == 1
        assert run_check({"mode": "shadow", "n_undecided": 1, "undecided": und, "n_blocked": 0, "blocked": {}}) == []
        assert run_check({"mode": "paper", "n_undecided": 0, "undecided": [], "n_blocked": 0, "blocked": {}}) == []


class TestFailsLoud:
    def test_a_misspelled_mode_raises_instead_of_silently_turning_off(self, state):
        st, q, px = _long_x()
        _seed(st)
        pg.CONFIG["delta_hedge_mode"] = "papper"
        with pytest.raises(ValueError):
            _run(_closes(px), q)
        assert pg.price_check_stats()["error"].startswith("ValueError")           # 异常记进 price_check ⇒ 告警通路能红

    def test_unreadable_holding_row_is_unknown_not_a_second_hedge(self, state):
        pg.CONFIG["delta_hedge_mode"] = "paper"
        st, q, px = _long_x()
        _seed(st)
        _jsonl(pg.STATE_DIR / "delta_positions.jsonl", [{"ticker": "XYZ", "shares": None, "avg_price": 100.0}])
        _jsonl(pg.STATE_DIR / "delta_meta.json", [])
        (pg.STATE_DIR / "delta_meta.json").write_text(json.dumps({"cash": 0.0, "starting_date": AS_OF}), encoding="utf-8")
        r = _run(_closes(px), q, execute=False)
        d = _names(r)["XYZ"]
        assert d["status"] == "unknown" and d["reason"] == "held_shares_unknown" and d["trade_shares"] == 0
