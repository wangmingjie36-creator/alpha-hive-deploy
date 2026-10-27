"""组合尾部预算（v0.45.649）接进 `portfolio_greeks.compute_day` 与 `alert_manager`：

  · 一致性（`TestWiring`）：`tail` 的最差格 = 同一次 `stress_table` 的最差格；各标的贡献之和 = 全组合最差格；
    最后开仓的那一笔的「加上它之后」= 全组合最差格损失（试算口径与归因口径是同一个账）；板块取自 `config.WATCHLIST`；
  · **只观测**（`TestObservationOnly`）：把尾部预算整个换成抛异常，建议 / 聚合 / 压力表一字不变；跨式账文件字节不变——
    尾部预算不改任何交易行为；
  · 失败要红（`TestFailureIsVisible`）：算尾部预算抛了 ⇒ ERROR 日志 + `available=False, reason=exception:<类>` + 告警，
    不把「没算出来」写成「没有尾部」；
  · 告警（`TestAlert`）：超线 ⇒ P2、线内不响；算不出来 ⇒ P2；旧版扫描没有 `tail` 键 ⇒ 不报。
全部离线：价格 / 报价 / β 一律注入，三本账的状态目录重定向到 tmp_path（`state` 夹具取自 `test_portfolio_greeks`）。
"""
from __future__ import annotations

import logging

import pytest

import options_paper_leg as opl
import paper_portfolio as pp
import portfolio_greeks as pg
import tail_budget as tb
from alert_manager import AlertAnalyzer, AlertLevel
from tests.test_portfolio_greeks import (AS_OF, CALL, PUT, SPY, _all_finite, _betas, _bs_quote, _closes, _jsonl,
                                         _quotes, _seed_navs, _stock, _straddle)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """三本账的状态目录全部重定向到 tmp_path（与 `test_portfolio_greeks.state` 同一套；不导入它——
    导入的 fixture 再被当参数名用会触发 F811）。"""
    hs = tmp_path / "hedge_state"
    for name, f in (("STATE_DIR", hs), ("POSITIONS_FILE", hs / "positions.jsonl"), ("TRADES_FILE", hs / "trades.jsonl"),
                    ("EQUITY_FILE", hs / "equity_curve.jsonl"), ("META_FILE", hs / "meta.json"),
                    ("BETA_CACHE_FILE", hs / "beta_cache.json")):
        monkeypatch.setattr(pg, name, f)
    ps = tmp_path / "paper_portfolio_state"
    for mod, state_dir in ((pp, ps), (opl, tmp_path / "options_paper_state")):
        monkeypatch.setattr(mod, "POSITIONS_FILE", state_dir / "positions.jsonl")
        monkeypatch.setattr(mod, "EQUITY_FILE", state_dir / "equity_curve.jsonl")
        monkeypatch.setattr(mod, "META_FILE", state_dir / "meta.json")
    monkeypatch.setitem(pg.CONFIG, "rebalance_to", "edge")
    monkeypatch.setitem(pg.CONFIG, "beta_delta_band_pct", 15.0)
    monkeypatch.setitem(pg.CONFIG, "beta_delta_target_pct", 0.0)
    pg._BARS_CACHE.clear()
    return tmp_path


def _book(state, *, order=("NVDA", "XOM"), xom_entry=AS_OF):
    """股票账 AAA（未分类）+ 两笔卖跨式：NVDA（技术）早开、XOM（能源）按 `xom_entry` 开。"""
    _seed_navs(state, 100_000.0, 100_000.0)
    _jsonl(pp.POSITIONS_FILE, [_stock("AAA", "bullish", 100)])
    pos = {"NVDA": _straddle("short", 3, "NVDA"), "XOM": {**_straddle("short", 2, "XOM"), "entry_date": xom_entry}}
    _jsonl(opl.POSITIONS_FILE, [pos[t] for t in order])
    table = {}
    for tk in ("NVDA", "XOM"):
        table[CALL.replace("XYZ", tk)] = _bs_quote(100.0, 100.0, 29, 0.4, "call")
        table[PUT.replace("XYZ", tk)] = _bs_quote(100.0, 100.0, 29, 0.4, "put")
    closes = _closes({"AAA": 100.0, "NVDA": 100.0, "XOM": 100.0, "SPY": SPY})
    betas = _betas({"AAA": 1.0, "NVDA": 1.2, "XOM": 0.8})
    return closes, _quotes(table), betas


def _day(state, **kw):
    closes, quotes, betas = _book(state, **{k: v for k, v in kw.items() if k in ("order", "xom_entry")})
    return pg.compute_day(AS_OF, closes_fn=closes, quotes_fn=quotes, beta_fn=betas)


class TestWiring:
    def test_tail_section_matches_the_stress_table_it_is_built_from(self, state):
        res = _day(state)
        t, w = res["tail"], res["stress"]["worst_cell"]
        assert t["available"] is True and t["version"] == tb.VERSION
        assert (t["worst"]["spot_pct"], t["worst"]["iv_pts"]) == (w["spot_pct"], w["iv_pts"])
        assert t["worst"]["pnl"] == pytest.approx(w["pnl"], abs=0.02)
        assert t["loss_pct_nav"] == pytest.approx(-w["pnl"] / res["nav"]["nav"] * 100.0, abs=0.01)
        assert t["worst"]["pnl"] < 0                                    # 卖跨式：确有尾部
        assert sum(x["at_worst"] for x in t["by_ticker"]) == pytest.approx(t["worst"]["pnl"], abs=0.1)
        assert abs(t["residual_usd"]) <= 0.1

    def test_per_ticker_buckets_and_sectors_come_from_the_watchlist(self, state):
        t = _day(state)["tail"]
        by = {x["ticker"]: x for x in t["by_ticker"]}
        assert set(by) == {"NVDA", "XOM", "AAA", "SPY"} or set(by) == {"NVDA", "XOM", "AAA"}
        assert by["NVDA"]["sector"] == "Technology" and by["XOM"]["sector"] == "Energy" and by["AAA"]["sector"] == tb.UNCLASSIFIED
        assert by["NVDA"]["by_kind"].keys() == {"option"} and by["AAA"]["by_kind"].keys() == {"stock"}
        assert by["NVDA"]["at_worst"] < 0 and by["XOM"]["at_worst"] < 0
        secs = {s["sector"]: s for s in t["by_sector"]}
        assert secs["Technology"]["tickers"] == ["NVDA"] and secs["Energy"]["tickers"] == ["XOM"]
        assert sum(s["at_worst"] for s in t["by_sector"]) == pytest.approx(t["worst"]["pnl"], abs=0.1)

    def test_the_last_opened_entry_sees_the_whole_book(self, state):
        """XOM 排在最后：它「加上之后」的账 = 全部持仓 ⇒ loss_after = 全组合最差格损失（两条口径是同一个账）。"""
        t = _day(state)["tail"]
        es = t["gate_shadow"]["entries"]
        assert [e["ticker"] for e in es] == ["XOM"]                        # NVDA 是 09-01 开的，今天只评估今天的入场
        e = es[0]
        assert e["evaluable"] is True and e["applies"] is True and e["sector"] == "Energy"
        assert e["loss_after_pct"] == pytest.approx(t["loss_pct_nav"], abs=0.01)
        assert 0.0 <= e["loss_before_pct"] < e["loss_after_pct"] and e["marginal_pct"] > 0

    def test_open_order_matters_in_the_real_ledger_file_order(self, state):
        """有牙：同一笔 XOM，写在文件里第一 vs 第二，试算前的账不同。"""
        late = _day(state, order=("NVDA", "XOM"))["tail"]["gate_shadow"]["entries"][0]
        early = _day(state, order=("XOM", "NVDA"))["tail"]["gate_shadow"]["entries"][0]
        assert early["loss_before_pct"] < late["loss_before_pct"] and early["loss_after_pct"] < late["loss_after_pct"]

    def test_entries_are_only_those_dated_as_of(self, state):
        assert _day(state, xom_entry="2026-09-01")["tail"]["gate_shadow"]["entries"] == []

    def test_brief_is_attached_to_price_check_and_the_process_stats(self, state):
        res = _day(state)
        assert res["price_check"]["tail"] == res["tail"]["brief"]
        assert pg.price_check_stats()["tail"] == res["tail"]["brief"]
        assert res["tail"]["brief"]["gate_n_entries"] == 1
        _all_finite(res["tail"])

    def test_audit_file_carries_the_tail_section(self, state):
        closes, quotes, betas = _book(state)
        pg.run_for_date(AS_OF, closes_fn=closes, quotes_fn=quotes, beta_fn=betas)
        import json
        audit = json.loads((pg.STATE_DIR / f"greeks_{AS_OF}.json").read_text(encoding="utf-8"))
        assert audit["tail"]["available"] is True and audit["tail"]["gate_shadow"]["n_entries"] == 1
        assert audit["price_check"]["tail"]["alert_pct"] == tb.CONFIG["alert_pct"]


class TestObservationOnly:
    def test_replacing_the_whole_tail_computation_changes_nothing_else(self, state, monkeypatch):
        """有牙：尾部预算整个换成抛异常，建议 / 聚合 / 压力表 / 净值 / 对冲决定必须一字不差。"""
        with_tail = _day(state)
        monkeypatch.setattr(tb, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        without = _day(state)
        for key in ("recommendation", "aggregate", "stress", "nav", "today_trade", "delta_hedge", "rows"):
            assert with_tail[key] == without[key], key

    def test_the_straddle_ledger_file_is_not_touched(self, state):
        closes, quotes, betas = _book(state)
        before = opl.POSITIONS_FILE.read_bytes()
        pg.run_for_date(AS_OF, closes_fn=closes, quotes_fn=quotes, beta_fn=betas)
        assert opl.POSITIONS_FILE.read_bytes() == before

    def test_the_gate_never_skips_anything_it_only_records(self, state, monkeypatch):
        monkeypatch.setitem(tb.CONFIG, "shadow_budgets_pct", (0.01, 0.02))        # 任何账都超预算
        res = _day(state)
        e = res["tail"]["gate_shadow"]["entries"][0]
        assert e["would_skip"] == {"0.01": True, "0.02": True}
        assert [p["ticker"] for p in pg._load_jsonl(opl.POSITIONS_FILE)] == ["NVDA", "XOM"]      # 仓还在
        assert {r["ticker"] for r in res["rows"] if r["kind"] == "option"} == {"NVDA", "XOM"}


class TestFailureIsVisible:
    def test_an_exception_is_logged_as_error_and_surfaces_as_a_reason(self, state, monkeypatch, caplog):
        monkeypatch.setattr(tb, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with caplog.at_level(logging.ERROR):
            res = _day(state)
        t = res["tail"]
        assert t["available"] is False and t["reason"] == "exception:RuntimeError"
        assert t["brief"] == {"available": False, "reason": "exception:RuntimeError", "alert_pct": tb.CONFIG["alert_pct"],
                              "breach": False}
        assert any("尾部预算算不出来" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert res["recommendation"] is not None and res["stress"]["cells"]       # 其余部分照常

    def test_unusable_nav_is_unavailable_with_a_code_not_a_zero(self, state):
        sec = pg._tail_section(AS_OF, [], SPY, None)
        assert sec["available"] is False and sec["reason"] == "nav_unavailable" and sec["brief"]["breach"] is False

    def test_breach_is_logged_as_a_warning_naming_the_sources(self, state, monkeypatch, caplog):
        monkeypatch.setitem(tb.CONFIG, "alert_pct", 0.01)
        with caplog.at_level(logging.WARNING):
            res = _day(state)
        assert res["tail"]["brief"]["breach"] is True
        msg = " ".join(r.getMessage() for r in caplog.records if "尾部压力" in r.getMessage())
        assert "超过告警线" in msg and ("NVDA" in msg or "XOM" in msg)


class TestAlertLines:
    """Gamma / Vega 告警线随尾部预算推导（v0.45.655）：预算对半分给两项、除以压力网格最大冲击。"""

    def test_defaults_are_derived_not_hand_picked(self):
        assert pg.CONFIG["gamma_alert_pct"] is None and pg.CONFIG["vega_alert_pct"] is None
        assert pg.alert_lines() == {"gamma_alert_pct": 0.1, "vega_alert_pct": 0.5, "source": "derived"}

    def test_lines_follow_the_budget(self, monkeypatch):
        monkeypatch.setitem(tb.CONFIG, "alert_pct", 40.0)
        assert pg.alert_lines()["gamma_alert_pct"] == pytest.approx(0.2) and pg.alert_lines()["vega_alert_pct"] == pytest.approx(1.0)

    def test_lines_follow_the_stress_grid_extent(self, monkeypatch):
        """有牙：线是「预算 ÷ 网格角点冲击」；网格改小，线必须跟着变（不是写死的 0.1 / 0.5）。"""
        monkeypatch.setitem(pg.CONFIG, "stress_spot_pct", [-5, 0, 5])
        monkeypatch.setitem(pg.CONFIG, "stress_iv_pts", [-10, 0, 10])
        assert pg.alert_lines()["gamma_alert_pct"] == pytest.approx(0.4) and pg.alert_lines()["vega_alert_pct"] == pytest.approx(1.0)

    def test_an_explicit_number_overrides_and_a_half_override_is_labelled_mixed(self, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "gamma_alert_pct", 0.3)
        assert pg.alert_lines() == {"gamma_alert_pct": 0.3, "vega_alert_pct": 0.5, "source": "mixed"}
        monkeypatch.setitem(pg.CONFIG, "vega_alert_pct", 2.0)
        assert pg.alert_lines() == {"gamma_alert_pct": 0.3, "vega_alert_pct": 2.0, "source": "explicit"}

    def test_the_two_lines_together_equal_the_budget_at_the_corner_shock(self):
        ln = pg.alert_lines()
        assert ln["gamma_alert_pct"] * 10 ** 2 + ln["vega_alert_pct"] * 20 == pytest.approx(tb.CONFIG["alert_pct"])

    def test_aggregate_flags_use_the_effective_lines(self, state, monkeypatch):
        res = _day(state)
        g = abs(res["aggregate"]["pct_nav"]["gamma_dollar_per_1pct"])
        v = abs(res["aggregate"]["pct_nav"]["vega_dollar_per_pt"])
        assert g > 0 and v > 0
        agg = lambda: pg.aggregate(res["rows"], res["nav"]["nav"])            # noqa: E731
        assert agg()["gamma_alert"] is (g > 0.1) and agg()["vega_alert"] is (v > 0.5)        # 派生线
        monkeypatch.setitem(pg.CONFIG, "gamma_alert_pct", g - 1e-6)
        monkeypatch.setitem(pg.CONFIG, "vega_alert_pct", v - 1e-6)
        assert agg()["gamma_alert"] is True and agg()["vega_alert"] is True       # 显式线刚好压在读数之下
        monkeypatch.setitem(pg.CONFIG, "gamma_alert_pct", g + 1e-6)
        monkeypatch.setitem(pg.CONFIG, "vega_alert_pct", v + 1e-6)
        assert agg()["gamma_alert"] is False and agg()["vega_alert"] is False     # 压在读数之上

    def test_the_old_constants_were_blind_to_the_tail(self, state):
        """这一条就是改它的理由：二阶近似的损失 ≈ Gamma×10² + Vega×20；旧线（0.5 / 1.0）对应的损失远超预算。"""
        old_loss = 0.5 * 10 ** 2 + 1.0 * 20
        assert old_loss == 70.0 and old_loss > 3 * tb.CONFIG["alert_pct"]
        ln = pg.alert_lines()
        assert ln["gamma_alert_pct"] * 100 + ln["vega_alert_pct"] * 20 == pytest.approx(tb.CONFIG["alert_pct"])

    def test_public_rules_and_the_ledger_view_show_effective_numbers_not_none(self, tmp_path):
        r = pg.public_rules()
        assert r["gamma_alert_pct"] == 0.1 and r["vega_alert_pct"] == 0.5 and r["alert_lines_source"] == "derived"
        assert r["tail_alert_pct"] == tb.CONFIG["alert_pct"] and all(r[k] is not None for k in pg.PUBLIC_RULE_KEYS)
        v = pg.ledger_view(tmp_path / "nope")                                  # 状态目录不存在时规则也在
        assert v["rules"]["gamma_alert_pct"] == 0.1 and v["rules"]["alert_lines_source"] == "derived"

    def test_markdown_names_the_effective_line_when_flagged(self, state, monkeypatch):
        monkeypatch.setitem(pg.CONFIG, "gamma_alert_pct", 0.0001)
        monkeypatch.setitem(pg.CONFIG, "vega_alert_pct", 0.0001)
        md = pg.render_markdown(AS_OF, _day(state))
        assert "⚠️ 超 0.0001%" in md and "None" not in md


class TestSlotCaps:
    """单仓上限 = 尾部预算 ÷ 跨式账名额数（v0.45.655）。"""

    def test_cap_is_the_budget_over_the_ledgers_slot_count(self, state):
        t = _day(state)["tail"]
        assert t["max_open"] == opl.CONFIG["max_open"] and t["slot_cap_pct"] == round(tb.CONFIG["alert_pct"] / opl.CONFIG["max_open"], 2)

    def test_each_straddle_has_a_slot_equal_to_its_own_worst_cell_loss(self, state):
        t = _day(state)["tail"]
        by = {x["ticker"]: x for x in t["by_ticker"]}
        for tk in ("NVDA", "XOM"):
            assert by[tk]["slot"]["loss_pct"] == pytest.approx(-by[tk]["own_worst"]["pct_nav"], abs=0.01)
            assert by[tk]["slot"]["over_cap"] is (by[tk]["slot"]["loss_pct"] > t["slot_cap_pct"])
        assert by["AAA"]["slot"] is None                                        # 只有股票腿：没有「单仓」

    def test_cap_follows_the_slot_count(self, state, monkeypatch):
        monkeypatch.setitem(opl.CONFIG, "max_open", 2)
        t = _day(state)["tail"]
        assert t["slot_cap_pct"] == 10.0 and t["max_open"] == 2
        assert t["n_over_cap"] == len(t["over_cap_tickers"])

    def test_brief_and_alert_carry_the_over_cap_names(self, state, monkeypatch):
        monkeypatch.setitem(opl.CONFIG, "max_open", 200)                        # 上限 0.1%：两笔都必超
        res = _day(state)
        b = res["tail"]["brief"]
        assert b["n_over_cap"] == 2 and {o["ticker"] for o in b["over_cap"]} == {"NVDA", "XOM"} and b["slot_cap_pct"] == 0.1
        assert all(o["loss_pct"] > 0.1 and o["multiple"] > 1 for o in b["over_cap"])
        assert res["price_check"]["tail"] == b


class TestAlert:
    BASE = {"as_of": AS_OF, "n_stale": 0, "n_quote_stale": 0, "spy": {}, "hedge_undecided": None, "gaps": [],
            "execution_blocked": None}

    def run(self, tail, **extra):
        am = AlertAnalyzer.__new__(AlertAnalyzer)
        am.alerts, am.checks_skipped = [], []
        pgc = {**self.BASE, **extra}
        if tail is not None:
            pgc["tail"] = tail
        am._check_portfolio_tail({"scan_timing": {"counters": {"portfolio_greeks": pgc}}})
        return am.alerts

    BREACH = {"available": True, "alert_pct": 20.0, "loss_pct_nav": 23.1, "breach": True, "partial": False,
              "worst": {"spot_pct": -10.0, "iv_pts": 20.0, "pnl": -33469.0, "pct_nav": -23.1},
              "top": [{"ticker": "NVDA", "sector": "Technology", "at_worst_pct": -6.8},
                      {"ticker": "XOM", "sector": "Energy", "at_worst_pct": -5.5}],
              "top_sectors": [{"sector": "Technology", "at_worst_pct": -10.8}],
              "gate_n_would_skip": {"15": 1, "20": 0}, "gate_n_entries": 2}

    def test_breach_is_one_p2_naming_sources_and_the_shadow_gate(self):
        (a,) = self.run(self.BREACH)
        assert a.level == AlertLevel.MEDIUM and "P2" in a.message and "组合尾部" in a.message and "23.1%" in a.message
        assert "NVDA -6.8%" in a.details["主要来源"] and "XOM" in a.details["主要来源"]
        assert "没有真跳过" in a.details["影子闸"] and "15% / 20%" in a.details["影子闸"] and a.details["压力表"] == "完整"
        assert "只观测" in a.details["影响"]

    def test_over_cap_positions_are_named_with_their_multiple(self):
        (a,) = self.run({**self.BREACH, "slot_cap_pct": 3.33, "n_over_cap": 2,
                         "over_cap": [{"ticker": "NVDA", "loss_pct": 6.9, "multiple": 2.1},
                                      {"ticker": "XOM", "loss_pct": 6.1, "multiple": 1.8}]})
        assert "2 笔超过单仓上限 3.33%" in a.details["单仓上限"] and "NVDA 6.9%（2.1×）" in a.details["单仓上限"]
        (b,) = self.run({**self.BREACH, "slot_cap_pct": 3.33, "n_over_cap": 0, "over_cap": []})
        assert b.details["单仓上限"] == "—"

    def test_partial_stress_is_said_in_the_alert(self):
        (a,) = self.run({**self.BREACH, "partial": True})
        assert "可能更大" in a.details["压力表"]

    def test_inside_the_line_is_silent(self):
        assert self.run({**self.BREACH, "breach": False, "loss_pct_nav": 12.0}) == []

    def test_a_computation_failure_is_a_p2_not_silence(self):
        (a,) = self.run({"available": False, "reason": "exception:RuntimeError", "breach": False})
        assert a.level == AlertLevel.MEDIUM and "算不出来" in a.message and "exception:RuntimeError" in a.message

    @pytest.mark.parametrize("tail", [{"available": False, "reason": "nav_unavailable", "breach": False},
                                      {"available": False, "reason": "stress_unavailable", "breach": False}, None, "bad"])
    def test_data_gaps_and_old_scans_are_not_this_checks_business(self, tail):
        assert self.run(tail) == []

    def test_a_run_that_errored_is_reported_elsewhere(self):
        assert self.run(self.BREACH, error="boom") == []

    def test_it_is_wired_into_the_analyzer_run(self):
        import inspect
        src = inspect.getsource(AlertAnalyzer)
        assert "self._check_portfolio_tail(status)" in src
