"""SPY 覆盖只对冲跨式、不看股票纸面组合（v0.45.701，用户 2026-11-03 定）。

缺省 `CONFIG["hedge_stock_book"] = False`：股票纸面组合的持仓不出行（不进 β·$Delta / 带位 / 压力表 / 尾部预算），
它缺价也不让整天判「数据不全」；合并 NAV 照旧含股票账净值（只换分子、不换分母）。
被 `hedge_exclude_tickers` 排除的标的只有还有跨式时才点名（BILI 只有股票仓时，对冲页那条黄条不该出现）。
其余对冲测试文件钉回 `True` 测通用机制；本文件守缺省口径，并各配一个翻回 `True` 的对照。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import options_paper_leg as opl
import paper_portfolio as pp
import portfolio_greeks as pg
from tests.test_portfolio_greeks import (AS_OF, CALL, PUT, SPY, _betas, _bs_quote, _closes, _jsonl, _quotes,
                                         _seed_navs, _stock, _straddle)

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """三本账的状态目录重定向到 tmp_path（与 `test_portfolio_greeks.state` 同一套；不导入它——
    导入的 fixture 再被当参数名用会触发 F811）。**不**钉 `hedge_stock_book`：本文件测的就是缺省值。"""
    hs = tmp_path / "hedge_state"
    for name, f in (("STATE_DIR", hs), ("POSITIONS_FILE", hs / "positions.jsonl"), ("TRADES_FILE", hs / "trades.jsonl"),
                    ("EQUITY_FILE", hs / "equity_curve.jsonl"), ("META_FILE", hs / "meta.json"),
                    ("BETA_CACHE_FILE", hs / "beta_cache.json")):
        monkeypatch.setattr(pg, name, f)
    for mod, state_dir in ((pp, tmp_path / "paper_portfolio_state"), (opl, tmp_path / "options_paper_state")):
        monkeypatch.setattr(mod, "POSITIONS_FILE", state_dir / "positions.jsonl")
        monkeypatch.setattr(mod, "EQUITY_FILE", state_dir / "equity_curve.jsonl")
        monkeypatch.setattr(mod, "META_FILE", state_dir / "meta.json")
    monkeypatch.setitem(pg.CONFIG, "rebalance_to", "edge")
    monkeypatch.setitem(pg.CONFIG, "beta_delta_band_pct", 15.0)
    monkeypatch.setitem(pg.CONFIG, "beta_delta_target_pct", 0.0)
    pg._BARS_CACHE.clear()
    return tmp_path


def _straddle_quotes():
    return _quotes({CALL: _bs_quote(100.0, 100.0, 29, 0.4, "call"), PUT: _bs_quote(100.0, 100.0, 29, 0.4, "put")})


def _kinds(res):
    return sorted({r["kind"] for r in res["rows"]})


class TestDefault:
    def test_default_is_straddle_only_and_page_can_read_it(self):
        assert pg.CONFIG["hedge_stock_book"] is False
        assert pg.public_rules()["hedge_stock_book"] is False


class TestStockBookDoesNotDriveTheBand:
    def _book(self, state):
        _seed_navs(state)
        # 股票仓 $500k 多头：若算进去，β·$Delta 远超 +15% 带 ⇒ 要卖 SPY
        _jsonl(pp.POSITIONS_FILE, [_stock("AAA", "bullish", 5000)])
        _jsonl(opl.POSITIONS_FILE, [_straddle("long", 1)])
        return _closes({"AAA": 100.0, "XYZ": 100.0, "SPY": SPY}), _betas({"AAA": 1.0, "XYZ": 1.0})

    def test_no_stock_rows_and_no_trade(self, state):
        closes, betas = self._book(state)
        res = pg.compute_day(AS_OF, closes_fn=closes, quotes_fn=_straddle_quotes(), beta_fn=betas)
        assert "stock" not in _kinds(res) and "option" in _kinds(res)
        assert res["aggregate"]["band_status"] == "inside"
        assert int(res["recommendation"].get("spy_shares") or 0) == 0

    def test_control_legacy_mode_would_sell_spy(self, state, monkeypatch):
        """对照：翻回旧口径，同一本账会因股票仓出带卖 SPY——证明上面的「不交易」来自开关，不是夹具没接上。"""
        monkeypatch.setitem(pg.CONFIG, "hedge_stock_book", True)
        closes, betas = self._book(state)
        res = pg.compute_day(AS_OF, closes_fn=closes, quotes_fn=_straddle_quotes(), beta_fn=betas)
        assert "stock" in _kinds(res)
        assert res["aggregate"]["band_status"] == "above" and res["recommendation"]["spy_shares"] < 0

    def test_nav_denominator_still_includes_the_stock_book(self, state):
        closes, betas = self._book(state)
        res = pg.compute_day(AS_OF, closes_fn=closes, quotes_fn=_straddle_quotes(), beta_fn=betas)
        assert res["nav"]["components"]["stock_book"] == 100_000.0
        assert res["nav"]["nav"] == pytest.approx(200_000.0)

    def test_unpriced_stock_no_longer_blocks_the_decision(self, state):
        _seed_navs(state)
        _jsonl(pp.POSITIONS_FILE, [_stock("AAA", "bullish", 10)])
        _jsonl(opl.POSITIONS_FILE, [_straddle("long", 1)])
        res = pg.compute_day(AS_OF, closes_fn=_closes({"XYZ": 100.0, "SPY": SPY}),     # AAA 没价
                             quotes_fn=_straddle_quotes(), beta_fn=_betas({"XYZ": 1.0}))
        assert res["aggregate"]["band_status"] != "unknown"
        assert not res["price_check"]["hedge_undecided"]

    def test_stress_and_tail_have_no_stock_rows(self, state):
        closes, betas = self._book(state)
        res = pg.compute_day(AS_OF, closes_fn=closes, quotes_fn=_straddle_quotes(), beta_fn=betas)
        tickers = {t["ticker"] for t in res["tail"]["by_ticker"]}
        assert "XYZ" in tickers and "AAA" not in tickers          # 先证明表里有东西，再证明股票不在


class TestExcludedNotice:
    def test_excluded_ticker_with_only_stock_is_not_named(self, state):
        _jsonl(pp.POSITIONS_FILE, [_stock("BILI", "bullish", 100)])
        ex = pg.excluded_exposure(150_000.0, option_positions=[])
        assert ex is not None and ex["names"] == []

    def test_excluded_ticker_with_a_straddle_is_still_named_without_stock_part(self, state):
        _jsonl(pp.POSITIONS_FILE, [_stock("BILI", "bullish", 100)])
        ex = pg.excluded_exposure(150_000.0, option_positions=[_straddle("short", 1, ticker="BILI")])
        (n,) = ex["names"]
        assert n["ticker"] == "BILI" and n["n_straddles"] == 1 and n["n_stock_positions"] == 0
        line = pg._excluded_line(ex)
        assert "跨式 1 笔" in line and "股票" not in line

    def test_control_legacy_mode_counts_the_stock(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "hedge_stock_book", True)
        _jsonl(pp.POSITIONS_FILE, [_stock("BILI", "bullish", 100)])
        (n,) = pg.excluded_exposure(150_000.0, option_positions=[])["names"]
        assert n["n_stock_positions"] == 1


class TestReport:
    def test_markdown_says_stock_book_is_not_hedged(self, state):
        _seed_navs(state)
        _jsonl(opl.POSITIONS_FILE, [_straddle("long", 1)])
        res = pg.compute_day(AS_OF, closes_fn=_closes({"XYZ": 100.0, "SPY": SPY}),
                             quotes_fn=_straddle_quotes(), beta_fn=_betas({"XYZ": 1.0}))
        assert "股票纸面组合不进对冲（只对冲跨式）" in pg.render_markdown(AS_OF, res)


class TestPage:
    """前端没有 JS 测试框架；静态核对两处文字随 `rules.hedge_stock_book` 走，且黄条不再无条件点名股票仓。"""
    js = (_ROOT / "alphabot" / "static" / "pages" / "hedge.js").read_text(encoding="utf-8")

    def test_lede_switches_on_the_rule(self):
        assert re.search(r"rules\.hedge_stock_book \? \"把股票纸面组合和跨式腿合起来\" : \"只拿财报跨式腿", self.js)

    def test_excluded_warning_skips_stock_only_names(self):
        block = self.js[self.js.index("excluded_from_hedge || []"):]
        block = block[:block.index("}\n")]
        assert "rules.hedge_stock_book ?" in block and "if (!nStock && !x.n_straddles) continue;" in block
