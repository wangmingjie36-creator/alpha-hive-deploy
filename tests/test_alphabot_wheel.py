"""Alpha Bot「接货」页（v0.45.721，`alphabot/wheel.py` + `/api/wheel` + `static/pages/wheel.js`）的守卫。离线。

每组回答一句「谁会红」：
  · 数据面纯整理：状态说明 / 离到期几天 / 持股浮动用手工数字复算；已结算 put 与两条基准并排的算术（`TestBenchmarks`）；
  · 账本没开 ≠ 账本为空；演示模式不读账本（`TestAvailability`）；
  · 端点：经账本属主的只读出口读（目录在 tmp），失败如实报（`TestEndpoint`）；
  · 前端接线：顶栏 / 路由 / 帮助章节 / 指南表一致；规则常量取自 /api/method 不写字面量；页面不给推荐字段（`TestFrontend`）；
  · 本模块不读文件、不联网、不 import 卖权选择器模块（`TestPurity`）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import sell_put_wheel as W
from alphabot import wheel as WH
from alphabot.service import AlphaBotService

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "alphabot" / "static"


def _state():
    put_open = {"ticker": "AAA", "cycle": "AAA-2026-11-04", "state": "put", "contracts": 1, "opened_on": "2026-11-04",
                "premiums": 2.0, "shares": None, "call": None,
                "put": {"symbol": "AAA270115P00090000", "strike": 90.0, "expiry": "2027-01-15", "premium": 2.0,
                        "last_mark": 1.5, "last_mark_date": "2026-11-05", "stale_days": 0, "accept_price": 90.0,
                        "accept_price_source": "placeholder", "annual_yield_pct": 11.3, "spot_at_open": 100.0,
                        "dte_at_open": 72}}
    held = {"ticker": "BBB", "cycle": "BBB-2026-10-01", "state": "shares", "contracts": 1, "opened_on": "2026-10-01",
            "premiums": 5.0, "put": None,
            "shares": {"n": 100, "assigned_price": 50.0, "assigned_on": "2026-12-01", "basis": 47.0, "last_price": 52.0},
            "call": {"symbol": "BBB270115C00055000", "strike": 55.0, "expiry": "2027-01-15", "premium": 3.0,
                     "last_mark": 2.0, "last_mark_date": "2026-12-02", "stale_days": 0}}
    events = [
        {"date": "2026-10-01", "ticker": "BBB", "event": "put_sold", "strike": 50.0, "expiry": "2026-11-27", "premium": 2.0,
         "contracts": 1, "cycle": "BBB-2026-10-01", "spot": 55.0},
        {"date": "2026-12-01", "ticker": "BBB", "event": "put_assigned", "strike": 50.0, "expiry": "2026-11-27",
         "premium": 2.0, "contracts": 1, "cycle": "BBB-2026-10-01", "close": 46.0},
        {"date": "2026-10-02", "ticker": "CCC", "event": "put_sold", "strike": 80.0, "expiry": "2026-11-27", "premium": 1.0,
         "contracts": 1, "cycle": "CCC-2026-10-02", "spot": 90.0},
        {"date": "2026-12-01", "ticker": "CCC", "event": "put_expired", "strike": 80.0, "expiry": "2026-11-27",
         "premium": 1.0, "contracts": 1, "cycle": "CCC-2026-10-02", "close": 95.0},
        {"date": "2026-10-04", "ticker": "EEE", "event": "put_sold", "strike": 80.0, "expiry": "2026-11-27", "premium": 1.0,
         "contracts": 1, "cycle": "EEE-2026-10-04", "spot": 85.0},
        {"date": "2026-12-01", "ticker": "EEE", "event": "put_expired", "strike": 80.0, "expiry": "2026-11-27",
         "premium": 1.0, "contracts": 1, "cycle": "EEE-2026-10-04", "close": 80.0},
        {"date": "2026-10-03", "ticker": "DDD", "event": "put_sold", "strike": 60.0, "expiry": "2026-11-27", "premium": 1.5,
         "contracts": 1, "cycle": "DDD-2026-10-03", "spot": 70.0},
        {"date": "2026-12-01", "ticker": "DDD", "event": "put_expired", "strike": 60.0, "expiry": "2026-11-27",
         "premium": 1.5, "contracts": 1, "cycle": "DDD-2026-10-03", "close": 66.0},
    ]
    equity = [
        {"date": "2026-10-15", "nav": 100100.0, "cash": 100300.0, "n_lots": 3, "prices": {"DDD": 58.0}},   # DDD 盘中收盘曾跌破 60
        {"date": "2026-12-02", "nav": 100500.0, "cash": 100000.0, "reserved_puts": 9000.0, "shares_value": 5200.0,
         "liability": 200.0, "n_lots": 2, "prices": {"AAA": 101.0, "BBB": 52.0}},
    ]
    meta = {"starting_capital": 100000.0, "cash": 100000.0, "last_run_date": "2026-12-02",
            "last_run": {"skipped": [{"ticker": "CCC", "leg": "put", "reason": "wide_spread"}],
                         "deferred": [{"ticker": "AAA", "leg": "put", "expiry": "2027-01-15", "reason": "lagging"}],
                         "chain_errors": {"EEE": "unavailable"}}}
    return {"available": True, "meta": meta, "lots": [put_open, held], "events": events, "equity": equity}


class TestOverview:
    def test_lot_rows_are_described_in_words_with_the_numbers_recomputed(self):
        v = WH.build_overview(_state(), rules={})
        lots = {l["ticker"]: l for l in v["lots"]}
        a, b = lots["AAA"], lots["BBB"]
        assert a["state_zh"] == WH.STATE_ZH["put"] and a["put"]["days_to_expiry"] == (
            __import__("datetime").date(2027, 1, 15) - __import__("datetime").date(2026, 12, 2)).days
        assert a["put"]["unrealized_usd"] == pytest.approx((2.0 - 1.5) * 100)          # 卖 2.0、现在 1.5 ⇒ 浮盈 $50
        assert a["premiums_collected_usd"] == 200.0
        assert b["state_zh"] == WH.STATE_ZH["shares_call"]
        assert b["shares"]["unrealized_usd"] == pytest.approx((52.0 - 50.0) * 100)
        assert b["call"]["unrealized_usd"] == pytest.approx((3.0 - 2.0) * 100)
        assert b["premiums_collected_usd"] == 500.0

    def test_account_and_skipped_reasons_in_words(self):
        v = WH.build_overview(_state(), rules={"x": 1})
        assert v["account"]["return_pct"] == pytest.approx(0.5) and v["account"]["reserved_puts"] == 9000.0
        assert v["skipped"][0]["reason_zh"] == W.SKIP_REASONS["wide_spread"]
        assert v["deferred"][0]["reason"] == "lagging" and v["deferred"][0]["reason_zh"] == WH.DEFER_ZH["lagging"]
        assert v["chain_errors"] == {"EEE": "unavailable"}
        assert v["rules"] == {"x": 1} and v["as_of"] == "2026-12-02"

    def test_events_newest_first_with_a_word_for_each(self):
        v = WH.build_overview(_state())
        dates = [e["date"] for e in v["events"]]
        assert dates == sorted(dates, reverse=True)
        assert all(e["event_zh"] != e["event"] for e in v["events"])
        assert set(WH.EVENT_ZH) == {"put_sold", "put_expired", "put_assigned", "call_sold", "call_expired", "call_called_away"}


class TestBenchmarks:
    """已结算的 put 与两条基准并排：到期那一场收盘上的手工数字。"""

    def _rows(self):
        return {r["ticker"]: r for r in WH.build_overview(_state())["benchmarks"]}

    def test_assigned_put_leg_gets_premium_plus_the_mark_to_close_loss(self):
        r = self._rows()["BBB"]
        # 卖 put 这一腿 = 权利金 200 + (46 − 50) × 100 = −200；限价买入在 50 成交 ⇒ (46 − 50) × 100 = −400；一直持有 = (46 − 55) × 100 = −900
        assert r["outcome"] == "assigned" and r["wheel_put_pnl_usd"] == -200.0
        assert r["limit_buy_filled"] is True and r["limit_buy_pnl_usd"] == -400.0 and r["hold_pnl_usd"] == -900.0

    def test_expired_put_keeps_only_the_premium_and_the_limit_order_never_fills(self):
        r = self._rows()["CCC"]
        # 收盘 95 > 80 ⇒ 作废，只剩权利金 100；限价没成交 ⇒ 0；一直持有 = (95 − 90) × 100 = +500
        assert r["outcome"] == "expired" and r["wheel_put_pnl_usd"] == 100.0
        assert r["limit_buy_filled"] is False and r["limit_buy_pnl_usd"] == 0.0 and r["hold_pnl_usd"] == 500.0

    def test_limit_order_counts_as_filled_if_an_earlier_close_was_below_the_strike_even_when_the_put_expired(self):
        r = self._rows()["DDD"]
        # 期间 10-15 收盘 58 ≤ 60 ⇒ 限价单成交；到期收盘 66 ⇒ (66 − 60) × 100 = +600；put 本身作废只赚 150
        assert r["outcome"] == "expired" and r["limit_buy_filled"] is True and r["limit_buy_pnl_usd"] == 600.0
        assert r["wheel_put_pnl_usd"] == 150.0 and r["hold_pnl_usd"] == -400.0

    def test_close_exactly_at_the_strike_expires_the_put_but_fills_the_limit_order(self):
        r = self._rows()["EEE"]
        # 收盘 80 == 行权价：put 不接货（settle_put 是严格 <），而限价买单在 80 成交（收盘 ≤ 行权价）；成交价 = 收盘 ⇒ 盈亏 0
        assert r["outcome"] == "expired" and r["limit_buy_filled"] is True and r["limit_buy_pnl_usd"] == 0.0
        assert r["wheel_put_pnl_usd"] == 100.0 and r["hold_pnl_usd"] == -500.0

    def test_unsettled_put_has_no_benchmark_row_yet(self):
        assert "AAA" not in self._rows()


class TestDeferReasons:
    def test_every_code_the_close_helpers_emit_has_a_word(self):
        src = (REPO / "options_paper_leg.py").read_text(encoding="utf-8")
        for code in WH.DEFER_ZH:                       # 登记的码都还在写它的地方——改名了这里就红
            assert f'"{code}"' in src, f"{code} 在 options_paper_leg 里不存在了"
        for code in ("twelve_data_unconfigured", "bars_unavailable", "lagging", "bar_missing", "bad_close", "no_close", "wrong_session"):
            assert code in WH.DEFER_ZH                 # 已知会出现的码都登记了

    def test_unknown_code_is_passed_through_not_invented(self):
        st = _state()
        st["meta"]["last_run"]["deferred"] = [{"ticker": "AAA", "leg": "put", "expiry": "2027-01-15", "reason": "zzz_new"}]
        assert WH.build_overview(st)["deferred"][0]["reason_zh"] == "zzz_new"


class TestAvailability:
    def test_no_state_dir_is_unavailable_not_empty(self):
        v = WH.build_overview({"available": False, "state_reason": "no_state_dir"}, rules={"k": 1})
        assert v == {"available": False, "state_reason": "no_state_dir", "rules": {"k": 1}}

    def test_empty_but_existing_ledger_is_available_with_no_rows(self):
        v = WH.build_overview({"available": True, "meta": {"starting_capital": 100000.0}, "lots": [], "events": [], "equity": []})
        assert v["available"] is True and v["lots"] == [] and v["benchmarks"] == [] and v["account"]["nav"] is None


class TestEndpoint:
    def test_missing_state_dir_reads_as_unavailable(self):
        v = AlphaBotService().wheel()
        assert v["available"] is False and v["state_reason"] == "no_state_dir"
        assert v["rules"]["universe"] == W.CONFIG["universe"]

    def test_demo_mode_never_reads_the_ledger(self):
        svc = AlphaBotService(demo=True)
        v = svc.wheel()
        assert v["available"] is False and v["state_reason"] == "demo"

    def test_real_state_round_trips_through_the_endpoint(self, monkeypatch):
        cfg = dict(W.CONFIG)
        cfg.update(universe=["AAA"], accept_price={"AAA": None})
        monkeypatch.setattr(W, "CONFIG", cfg)
        contracts = [{"symbol": "AAA261211P00090000", "expiry": "2026-12-11", "cp": "P", "strike": 90.0, "bid": 2.0,
                      "ask": 2.08, "oi": 500, "delta": -0.2, "iv": 0.25}]          # 37 天，在 v2 窗口 30–50 内
        W.run_for_date("2026-11-04", contracts_fn=lambda tk, d: ({"underlying_price": 100.0, "contracts": contracts}, None),
                       closes_fn=lambda *a, **k: None, rv_fn=lambda tk, d: (20.0, None))   # RV 注入：测试不打 yfinance
        v = AlphaBotService().wheel()
        assert v["available"] is True and v["lots"][0]["ticker"] == "AAA" and v["lots"][0]["put"]["strike"] == 90.0
        assert v["lots"][0]["put"]["accept_price_source"] == "placeholder"
        assert v["lots"][0]["put"]["iv_rv"] == 1.25 and v["lots"][0]["put"]["rv_pct"] == 20.0
        assert v["events"][0]["event_zh"] == WH.EVENT_ZH["put_sold"]

    def test_method_endpoint_carries_the_rules_for_the_help_page(self):
        m = AlphaBotService().meta_texts() if hasattr(AlphaBotService, "meta_texts") else {}
        assert m.get("wheel", {}).get("universe") == W.CONFIG["universe"]


class TestFrontend:
    PAGE = (STATIC / "pages" / "wheel.js").read_text(encoding="utf-8")
    HELP = (STATIC / "pages" / "help.js").read_text(encoding="utf-8")

    def test_nav_route_and_help_section_are_wired(self):
        assert 'href="#/wheel" data-nav="wheel"' in (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        assert 'from "./pages/wheel.js"' in app and 'page === "wheel"' in app
        assert '["wheel", "接货型卖 put"' in self.HELP and 'helpLink("wheel")' in self.PAGE

    def test_page_says_it_is_not_a_test_and_marks_the_placeholder(self):
        assert "测量账本，不是检验" in self.PAGE and "占位" in self.PAGE and "不给任何推荐" in self.PAGE

    def test_placeholder_is_marked_both_in_the_table_and_in_the_warning(self):
        code = "\n".join(ln for ln in self.PAGE.splitlines() if not ln.lstrip().startswith("//"))
        assert code.count('accept_price_source === "placeholder"') >= 2, "持仓表的「占位」标记或页首黄条丢了"
        assert 'class: "chip warn", title: "用户还没给接货价' in code and "接货价现在是占位" in code

    def test_page_has_no_recommendation_or_score_fields(self):
        code = "\n".join(ln for ln in self.PAGE.splitlines() if not ln.lstrip().startswith("//"))   # 注释里讨论某个词不等于对用户说了它
        for banned in ("recommend", "score", "推荐买入", "综合分", "rank"):
            assert banned not in code, banned

    def test_page_does_no_pnl_arithmetic_of_its_own(self):
        """盈亏 / 基准全由服务端算：前端不写 × 100、(a − b) 这类公式。"""
        body = re.sub(r"//.*", "", self.PAGE)
        assert "* 100" not in body and "* (" not in body

    def test_help_constants_come_from_the_server_not_literals(self):
        sect = self.HELP[self.HELP.index('["wheel", "接货型卖 put"'):self.HELP.index('["ledger", "账本"')]
        assert len(sect) > 800, "切片取空了——测试在空转"
        for key in ("universe", "placeholder_otm_pct", "per_name_cap_pct", "total_cap_pct", "put_dte_range", "rv_window", "min_iv_rv",
                    "call_dte_range", "call_dte_target", "call_target_delta", "max_spread_pct", "min_oi", "starting_capital"):
            assert f"w.{key}" in sect, key
        assert not re.search(r"\b(20|60|70|35)\s*%", sect) and not re.search(r"≤ \d", sect), "规则常量写成了字面量"

    def test_public_rules_are_json_ready_and_match_the_ledger_config(self):
        r = WH.public_rules()
        assert isinstance(r["put_dte_range"], list) and r["universe"] == W.CONFIG["universe"]
        import json
        json.dumps(r)


class TestPurity:
    SRC = (REPO / "alphabot" / "wheel.py").read_text(encoding="utf-8")

    def test_no_file_network_or_ledger_io_in_the_view_module(self):
        tree = ast.parse(self.SRC)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not names & {"open", "ledger_io", "PATHS", "requests", "urlopen", "load_jsonl", "load_json", "write_json",
                            "write_jsonl", "append_jsonl", "read_text", "write_text", "run_for_date"}, names

    def test_does_not_import_the_sell_strike_modules(self):
        tree = ast.parse(self.SRC)
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not [m for m in mods if m.startswith("sell_strike")], mods
