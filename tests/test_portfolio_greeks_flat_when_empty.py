"""v0.45.814 已没有东西可对冲 ⇒ SPY 覆盖整笔平掉（portfolio_greeks，用户 2026-11-24 定「空仓就全平」）。

事故：2026-11-23 NVDA 跨式结算出账后跨式账本为空，账上只剩 SPY 覆盖空 94 股（β·$Delta −52% NAV）。带规则「拉回带边」
只买回 68 股、留下 −26 股（≈ −14.5% NAV，刚好进带）——这笔空头不对冲任何东西，只要不出带就会一直挂着。
规则：除覆盖自己的 SPY 行外没有任何带敞口的行（没有股票行 / 逐票对冲行；期权行没有或全是到期锁定的）⇒ 不论带内带外，
target 0、整笔平掉。数据不全（unknown）照旧不下单；跨式持仓文件不存在不当作空仓。全部离线：价格 / β 注入。"""

import json

import pytest

import options_paper_leg as opl
import paper_portfolio as pp
import portfolio_greeks as pg

AS_OF = "2026-11-23"
SPY = 722.29


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    hs = tmp_path / "hedge_state"
    for attr, name in (("STATE_DIR", None), ("POSITIONS_FILE", "positions.jsonl"), ("TRADES_FILE", "trades.jsonl"),
                       ("EQUITY_FILE", "equity_curve.jsonl"), ("META_FILE", "meta.json"),
                       ("BETA_CACHE_FILE", "beta_cache.json")):
        monkeypatch.setattr(pg, attr, hs / name if name else hs)
    for mod, d in ((pp, tmp_path / "paper_portfolio_state"), (opl, tmp_path / "options_paper_state")):
        monkeypatch.setattr(mod, "POSITIONS_FILE", d / "positions.jsonl")
        monkeypatch.setattr(mod, "EQUITY_FILE", d / "equity_curve.jsonl")
        monkeypatch.setattr(mod, "META_FILE", d / "meta.json")
    monkeypatch.setitem(pg.CONFIG, "rebalance_to", "edge")
    monkeypatch.setitem(pg.CONFIG, "beta_delta_band_pct", 15.0)
    monkeypatch.setitem(pg.CONFIG, "beta_delta_target_pct", 0.0)
    monkeypatch.setitem(pg.CONFIG, "hedge_stock_book", False)
    monkeypatch.setitem(pg.CONFIG, "delta_hedge_mode", "shadow")
    pg._BARS_CACHE.clear()
    return tmp_path


def _jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _book(shares=-26.0, cash=23_435.43, straddle_positions_file=True):
    """11-23 收盘后那本账的形状：跨式账本空、SPY 覆盖空 `shares` 股。"""
    _jsonl(pg.POSITIONS_FILE, [{"ticker": "SPY", "shares": shares, "avg_price": 755.1183,
                                "entry_date": "2026-09-04", "last_price": 689.81, "last_mark_date": "2026-11-20"}])
    pg.META_FILE.write_text(json.dumps({"cash": cash, "starting_date": "2026-09-04"}), encoding="utf-8")
    _jsonl(pp.EQUITY_FILE, [{"date": AS_OF, "nav": 49_014.79}])
    _jsonl(opl.EQUITY_FILE, [{"date": AS_OF, "nav": 76_159.0}])
    if straddle_positions_file:
        _jsonl(opl.POSITIONS_FILE, [])


def _run():
    return pg.run_for_date(AS_OF, closes_fn=lambda tk, d: {"SPY": SPY}.get(tk),
                           quotes_fn=lambda tk, syms: {}, beta_fn=lambda tk, d: (1.6, "ols60"))


def _hedge_row(qty, price=SPY):
    return {"kind": "hedge", "ticker": "SPY", "qty": qty, "price": price, "beta": 1.0,
            "dollar_delta": round(qty * price, 2), "beta_dollar_delta": round(qty * price, 2)}


def _agg(rows, nav=130_000.0):
    return pg.aggregate(rows, nav)


class TestNothingToHedge:
    def test_hedge_rows_only(self):
        assert pg._nothing_to_hedge([_hedge_row(-26)]) is True
        assert pg._nothing_to_hedge([]) is True

    def test_locked_expiry_legs_do_not_count(self):
        locked = {"kind": "option", "expiry_intrinsic": True, "beta_dollar_delta": 0.0}
        assert pg._nothing_to_hedge([locked, locked, _hedge_row(-94)]) is True

    @pytest.mark.parametrize("row", [{"kind": "option", "beta_dollar_delta": 100.0},
                                     {"kind": "option", "expiry_intrinsic": False},
                                     {"kind": "stock", "beta_dollar_delta": 0.0},
                                     {"kind": pg.DELTA_ROW_KIND}])
    def test_any_live_row_counts(self, row):
        assert pg._nothing_to_hedge([row, _hedge_row(-26)]) is False


class TestRecommendation:
    def test_inside_band_residual_is_closed(self):
        """−26 股 ≈ −14.4% NAV 在带内：旧规则 hold，新规则整笔买回。"""
        rows = [_hedge_row(-26)]
        agg = _agg(rows)
        assert agg["band_status"] == "inside"
        assert pg.hedge_recommendation(agg, SPY, 130_000.0)["action"] == "hold"        # 旧调用（缺省）不变
        rec = pg.hedge_recommendation(agg, SPY, 130_000.0, nothing_to_hedge=True, hedge_shares=-26)
        assert rec["action"] == "buy_spy" and rec["spy_shares"] == 26 and rec["flatten"] is True
        assert rec["target_usd"] == 0.0 and rec["target_pct"] == 0.0
        assert rec["excess_usd"] == pytest.approx(-26 * SPY)

    def test_out_of_band_closes_all_not_to_edge(self):
        """11-23 原样：−94 股、−52% NAV ⇒ 买回 94，不是拉回带边的 68。"""
        agg = _agg([_hedge_row(-94)], nav=129_829.68)
        assert agg["band_status"] == "below"
        rec = pg.hedge_recommendation(agg, SPY, 129_829.68, nothing_to_hedge=True, hedge_shares=-94)
        assert rec["spy_shares"] == 94 and rec["action"] == "buy_spy"
        long_ = pg.hedge_recommendation(_agg([_hedge_row(40)]), SPY, 130_000.0, nothing_to_hedge=True,
                                        hedge_shares=40)
        assert long_["action"] == "sell_spy" and long_["spy_shares"] == -40

    def test_unknown_still_holds(self):
        """数据不全不下单这条不因「空仓」破例。"""
        agg = _agg([_hedge_row(-26)], nav=None)
        assert agg["band_status"] == "unknown"
        rec = pg.hedge_recommendation(agg, SPY, None, nothing_to_hedge=True, hedge_shares=-26)
        assert rec["action"] == "hold" and rec["spy_shares"] == 0 and "partial data" in rec["reason"]

    def test_no_spy_price_holds(self):
        rec = pg.hedge_recommendation(_agg([_hedge_row(-26)]), None, 130_000.0, nothing_to_hedge=True,
                                      hedge_shares=-26)
        assert rec["action"] == "hold" and "SPY price unavailable" in rec["reason"]

    def test_already_flat_is_empty(self):
        rec = pg.hedge_recommendation(_agg([]), SPY, 130_000.0, nothing_to_hedge=True, hedge_shares=0.0)
        assert rec["action"] == "hold" and rec["reason"] == "no positions"


class TestEndToEnd:
    def test_empty_straddle_book_closes_overlay_and_rerun_is_idempotent(self):
        _book()
        r = _run()
        assert r["recommendation"]["flatten"] is True
        t = r["executed_trade"]
        assert t["shares"] == 26 and t["shares_after"] == 0 and t["price"] == SPY and t["target_usd"] == 0.0
        assert pg._load_jsonl(pg.POSITIONS_FILE) == []                     # 平完不留 0 股行
        meta = json.loads(pg.META_FILE.read_text())
        assert meta["cash"] == pytest.approx(23_435.43 - 26 * SPY)
        eq = pg._load_jsonl(pg.EQUITY_FILE)[-1]
        assert eq["spy_shares"] == 0 and eq["nav"] == pytest.approx(23_435.43 - 26 * SPY, abs=0.01)
        assert r["aggregate_after"]["band_status"] == "empty"
        before = {p.name: p.read_bytes() for p in pg.STATE_DIR.iterdir()}
        r2 = _run()
        assert {p.name: p.read_bytes() for p in pg.STATE_DIR.iterdir()} == before
        assert r2["recommendation"] == r["recommendation"] and len(pg._load_jsonl(pg.TRADES_FILE)) == 1

    def test_missing_straddle_positions_file_is_not_treated_as_flat(self, caplog):
        """持仓文件没了读出来也是空表——那是「文件没了」，不是空仓：照旧带规则（这里在带内 ⇒ hold），ERROR 点名。"""
        _book(straddle_positions_file=False)
        r = _run()
        assert r["recommendation"]["action"] == "hold" and not r["recommendation"].get("flatten")
        assert r["executed"] is False
        assert any("不当作空仓" in m for m in caplog.messages)

    def test_live_straddle_keeps_band_rule(self):
        """跨式还在 ⇒ 不全平（行为与修改前相同）。"""
        _book()
        _jsonl(opl.POSITIONS_FILE, [{"ticker": "XYZ", "side": "short", "expiry": "2026-12-18", "strike": 100.0,
                                     "call_symbol": "XYZ261218C00100000", "put_symbol": "XYZ261218P00100000",
                                     "contracts": 1}])
        q = {"bid": 1.0, "ask": 1.2, "mid": 1.1, "iv": 0.3, "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
             "dte": 25, "quote_ok": True, "role": "held"}
        r = pg.run_for_date(AS_OF, closes_fn=lambda tk, d: {"SPY": SPY, "XYZ": 100.0}.get(tk),
                            quotes_fn=lambda tk, syms: {s: q for s in syms}, beta_fn=lambda tk, d: (1.2, "ols60"))
        assert not r["recommendation"].get("flatten") and r["recommendation"]["action"] == "hold"


class TestRuleIsStated:
    def test_markdown_and_pages_mention_it(self):
        from pathlib import Path
        root = Path(pg.__file__).resolve().parent
        assert "已无跨式可对冲时整笔平掉" in pg.render_markdown(AS_OF, {"as_of": AS_OF, "aggregate": _agg([_hedge_row(-26)]),
                                                                    "rows": [_hedge_row(-26)], "recommendation": {},
                                                                    "stress": pg.stress_table([_hedge_row(-26)]),
                                                                    "executed": False})
        assert "已没有跨式可对冲时整笔平掉" in (root / "alphabot/static/pages/hedge.js").read_text(encoding="utf-8")
        assert "SPY 覆盖整笔平掉" in (root / "alphabot/static/pages/help.js").read_text(encoding="utf-8")
