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

    def test_log_names_the_straddle_without_a_fake_stock_gap(self, state, monkeypatch, caplog):
        """v0.45.703：缺省口径下日志只说「跨式 N 笔」，不再写「股票仓 0 笔，按建仓价估 不可估」（读着像缺数据）。"""
        monkeypatch.setitem(pg.CONFIG, "hedge_exclude_tickers", ["BILI"])
        _seed_navs(state)
        _jsonl(pp.POSITIONS_FILE, [_stock("BILI", "bullish", 100)])
        _jsonl(opl.POSITIONS_FILE, [_straddle("short", 1, ticker="BILI")])
        with caplog.at_level("WARNING"):
            pg.compute_day(AS_OF, closes_fn=_closes({"SPY": SPY}), quotes_fn=_quotes({}), beta_fn=_betas({}))
        (msg,) = [m for m in caplog.messages if "hedge_exclude_tickers" in m]
        assert "BILI（跨式 1 笔）" in msg and "股票" not in msg and "不可估" not in msg

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


# ── v0.45.704：纸面组合坏了也不挡跨式对冲（用户 2026-11-03 定）──────────────────────────
# 股票账只剩 NAV 分母一处依赖；读不了 ⇒ 用最近一份审计里记下的股票账净值顶上，跨式照常对冲，并且 ERROR + P2 可见。

def _audit(state, day, stock_nav, fallback=None):
    nav = {"components": {"stock_book": stock_nav, "straddle_leg": 100_000.0, "hedge_overlay": 0.0}}
    if fallback:
        nav["stock_book_fallback"] = fallback
    (pg.STATE_DIR).mkdir(parents=True, exist_ok=True)
    (pg.STATE_DIR / f"greeks_{day}.json").write_text(__import__("json").dumps({"as_of": day, "nav": nav}),
                                                    encoding="utf-8")


def _straddle_day(state, *, corrupt=False, missing=False):
    _seed_navs(state)
    if corrupt:
        pp.EQUITY_FILE.write_text('{"date": "2026-09-03", "nav": 1\n{broken\n', encoding="utf-8")
    if missing:
        pp.EQUITY_FILE.unlink()
    _jsonl(opl.POSITIONS_FILE, [_straddle("long", 1)])
    return dict(closes_fn=_closes({"XYZ": 100.0, "SPY": SPY}), quotes_fn=_straddle_quotes(), beta_fn=_betas({"XYZ": 1.0}))


class TestPaperPortfolioBrokenDoesNotBlockStraddleHedge:
    def test_corrupt_equity_file_falls_back_to_last_audit(self, state, caplog):
        _audit(state, "2026-09-02", 90_000.0)
        kw = _straddle_day(state, corrupt=True)
        with caplog.at_level("ERROR"):
            res = pg.compute_day(AS_OF, **kw)
        fb = res["nav"]["stock_book_fallback"]
        assert res["nav"]["components"]["stock_book"] == 90_000.0 and res["nav"]["nav"] == pytest.approx(190_000.0)
        assert fb["from_audit"] == "2026-09-02" and fb["origin_date"] == "2026-09-02" and "LedgerCorrupt" in fb["reason"]
        assert res["price_check"]["nav_fallback"] == fb
        assert res["aggregate"]["band_status"] == "inside" and not res["price_check"]["hedge_undecided"]
        assert any("纸面组合净值读不了" in m and "2026-09-02" in m for m in caplog.messages)

    def test_missing_equity_file_falls_back_too(self, state):
        _audit(state, "2026-09-02", 90_000.0)
        res = pg.compute_day(AS_OF, **_straddle_day(state, missing=True))
        assert res["nav"]["stock_book_fallback"]["reason"] == "no_nav_row"
        assert res["nav"]["components"]["stock_book"] == 90_000.0

    def test_import_failure_falls_back(self, state, monkeypatch):
        import sys
        _audit(state, "2026-09-02", 90_000.0)
        kw = _straddle_day(state)
        monkeypatch.setitem(sys.modules, "paper_portfolio", None)        # import 即 ImportError
        res = pg.compute_day(AS_OF, **kw)
        assert "paper_portfolio" in res["nav"]["stock_book_fallback"]["reason"]   # ModuleNotFoundError（ImportError 子类）
        assert res["aggregate"]["band_status"] == "inside"

    def test_newest_audit_wins_and_future_audits_are_ignored(self, state):
        _audit(state, "2026-08-28", 80_000.0)
        _audit(state, "2026-09-02", 90_000.0)
        _audit(state, "2026-09-10", 1.0)                                 # as_of 之后的审计不许用
        res = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        assert res["nav"]["components"]["stock_book"] == 90_000.0

    def test_chained_fallback_keeps_the_original_read_date(self, state):
        _audit(state, "2026-09-02", 90_000.0,
               fallback={"reason": "x", "from_audit": "2026-08-28", "origin_date": "2026-08-28"})
        fb = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))["nav"]["stock_book_fallback"]
        assert fb["from_audit"] == "2026-09-02" and fb["origin_date"] == "2026-08-28"

    def test_audit_without_a_value_is_skipped(self, state):
        _audit(state, "2026-08-28", 80_000.0)
        _audit(state, "2026-09-02", None)
        res = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        assert res["nav"]["components"]["stock_book"] == 80_000.0

    def test_no_audit_to_fall_back_on_stays_undecided_and_says_so(self, state):
        res = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        fb = res["nav"]["stock_book_fallback"]
        assert fb["from_audit"] is None and res["nav"]["nav"] is None
        assert res["price_check"]["hedge_undecided"] and "stock_book" in res["price_check"]["nav_missing"]

    def test_healthy_day_has_no_fallback(self, state):
        _audit(state, "2026-09-02", 90_000.0)
        res = pg.compute_day(AS_OF, **_straddle_day(state))
        assert "stock_book_fallback" not in res["nav"] and res["price_check"]["nav_fallback"] is None
        assert res["nav"]["components"]["stock_book"] == 100_000.0

    def test_control_legacy_mode_still_stops_on_a_corrupt_book(self, state, monkeypatch):
        """对照：股票仓要进对冲时（旧口径），账本坏了照旧停下——回落只在「股票账只剩分母」时成立。"""
        import ledger_io
        monkeypatch.setitem(pg.CONFIG, "hedge_stock_book", True)
        _audit(state, "2026-09-02", 90_000.0)
        with pytest.raises(ledger_io.LedgerCorrupt):
            pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))

    def test_end_to_end_run_trades_and_next_day_chains(self, state):
        """真跑两天：第一天健康（写审计），第二天账本坏了 ⇒ 用第一天审计的值、照常落盘；审计里带回落说明。"""
        kw = _straddle_day(state)
        pg.run_for_date(AS_OF, **kw)
        assert (pg.STATE_DIR / f"greeks_{AS_OF}.json").exists()
        nxt = "2026-09-04"
        _jsonl(opl.EQUITY_FILE, pg._load_jsonl(opl.EQUITY_FILE) + [{"date": nxt, "nav": 100_000.0}])
        pp.EQUITY_FILE.write_text("{broken\n", encoding="utf-8")
        res = pg.run_for_date(nxt, **kw)
        fb = res["nav"]["stock_book_fallback"]
        assert fb["from_audit"] == AS_OF and res["nav"]["components"]["stock_book"] == 100_000.0
        saved = pg._load_audit(nxt)
        assert saved["nav"]["stock_book_fallback"]["origin_date"] == AS_OF


class TestFallbackIsVisible:
    @staticmethod
    def _analyze(tmp_path, counters):
        import json as _json

        import alert_manager as am
        status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"counters": counters, "extra": {"gh_pages": {"success": True}},
                                  "production_sync": {"outcome": "up_to_date"}}}
        p = tmp_path / "status.json"
        p.write_text(_json.dumps(status, ensure_ascii=False), encoding="utf-8")
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a.analyze(p)
        return [x for x in a.alerts if "纸面组合净值读不了" in x.message]

    def test_fallback_raises_p2(self, state, tmp_path):
        import alert_manager as am
        _audit(state, "2026-09-02", 90_000.0)
        pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        (hit,) = self._analyze(tmp_path, {"portfolio_greeks": pg.price_check_stats()})
        assert hit.level == am.AlertLevel.MEDIUM and "2026-09-02" in hit.message and "LedgerCorrupt" in hit.details["原因"]

    def test_healthy_day_is_silent(self, state, tmp_path):
        _audit(state, "2026-09-02", 90_000.0)
        pg.compute_day(AS_OF, **_straddle_day(state))
        assert self._analyze(tmp_path, {"portfolio_greeks": pg.price_check_stats()}) == []

    def test_page_payload_and_page_carry_it(self, state):
        from alphabot import hedge as ahedge
        _audit(state, "2026-09-02", 90_000.0)
        res = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        brief = ahedge.audit_brief(AS_OF, {"price_check": res["price_check"], "nav": res["nav"]})
        assert brief["price_check"]["nav_fallback"] == {k: res["price_check"]["nav_fallback"][k]
                                                        for k in ("reason", "from_audit", "origin_date")}
        assert ahedge.audit_brief(AS_OF, {"price_check": {}})["price_check"]["nav_fallback"] is None
        js = (_ROOT / "alphabot" / "static" / "pages" / "hedge.js").read_text(encoding="utf-8")
        assert "pc.nav_fallback?.from_audit" in js


class TestFallbackSurvivesBadAudits:
    """v0.45.706 二次检查：回落读的是旧审计——它们坏了只能让回落找不到值，不能让整轮对冲崩掉。"""

    def _write(self, day, text):
        pg.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (pg.STATE_DIR / f"greeks_{day}.json").write_text(text, encoding="utf-8")

    @pytest.mark.parametrize("text", [
        "[1, 2]",                                                        # 不是对象
        '{"as_of": "2026-09-02", "nav": [1]}',                           # nav 不是对象
        '{"as_of": "2026-09-02", "nav": {"components": [1]}}',           # components 不是对象
        '{"as_of": "2026-09-02", "nav": {"components": {"stock_book": "x"}}}',
        "{broken",                                                       # 坏 JSON
        '{"as_of": "2026-08-01", "nav": {"components": {"stock_book": 1.0}}}',   # 文件名与内容日期不符
    ])
    def test_bad_newer_audit_is_skipped_and_older_good_one_used(self, state, text):
        _audit(state, "2026-08-28", 80_000.0)
        self._write("2026-09-02", text)
        res = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        assert res["nav"]["components"]["stock_book"] == 80_000.0
        assert res["nav"]["stock_book_fallback"]["from_audit"] == "2026-08-28"

    def test_weird_previous_fallback_record_does_not_crash(self, state):
        _audit(state, "2026-09-02", 90_000.0, fallback={"origin_date": 123})
        fb = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))["nav"]["stock_book_fallback"]
        assert fb["origin_date"] == "2026-09-02"

    def test_unreadable_audit_dir_means_no_fallback_not_a_crash(self, state, monkeypatch):
        def boom(as_of):
            raise PermissionError("denied")
        monkeypatch.setattr(pg, "_audit_dates_desc", boom)
        res = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        fb = res["nav"]["stock_book_fallback"]
        assert fb["from_audit"] is None and "审计目录读不了" in fb["reason"] and res["nav"]["nav"] is None

    def test_no_fallback_gap_says_why(self, state):
        res = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        assert any("纸面组合净值读不了" in g and "LedgerCorrupt" in g for g in res["price_check"]["gaps"])

    def test_daily_report_marks_the_fallback(self, state):
        _audit(state, "2026-09-02", 90_000.0)
        res = pg.compute_day(AS_OF, **_straddle_day(state, corrupt=True))
        assert "纸面组合净值读不了，股票账用 2026-09-02 的旧值" in pg.render_markdown(AS_OF, res)
        healthy = pg.compute_day(AS_OF, **_straddle_day(state))
        assert "纸面组合净值读不了" not in pg.render_markdown(AS_OF, healthy)
