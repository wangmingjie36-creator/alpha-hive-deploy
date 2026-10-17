"""Alpha Bot「对冲」页的逐票 Delta 对冲面板（v0.45.526）。离线。

每组回答一句「谁会红」：
  · 数据面：决策取最近一份放行审计的 `delta_hedge` 一节；没有这一节如实说没有；paper 的持仓 / 成交 / 净值只在开账后出现，
    且成交经历史闸（`TestDeltaOverview`）；
  · 点名缺价的标的：审计里的 stale / unpriced 一路到页面（`TestNamesTheCulprit`）；
  · 前端接线：原因码 / 状态表与引擎登记的双向一致、% NAV 不在前端算、帮助页的常量取自服务端（`TestFrontendWiring`）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

import delta_hedge as DH
import portfolio_greeks as pg
from alphabot import hedge as HG
from alphabot import synthetic as syn
from alphabot.service import ET, AlphaBotService

NOW = datetime(2026, 10, 15, 20, 0, tzinfo=ET)
STATIC = Path(__file__).resolve().parent.parent / "alphabot" / "static"
DIRNAME = "hedge_state"

NAME_T = {"ticker": "T", "status": "hedge", "reason": None, "option_dd": 40014.0, "held_shares": 0.0, "target_shares": -1703,
          "trade_shares": -1703, "action": "sell", "net_dd": 40014.0, "net_pct_nav": 26.6, "price": 23.5,
          "price_session": "2026-10-15", "price_source": "cboe_close", "price_at_close": True, "breach": True,
          "traded_today": 0.0}
NAME_BAD = {**NAME_T, "ticker": "BILI", "status": "unknown", "reason": "price_stale", "option_dd": None, "target_shares": None,
            "trade_shares": 0, "action": "hold"}


def _section(mode="shadow", names=None, **kw):
    names = names if names is not None else [NAME_T, NAME_BAD]
    s = {"mode": mode, "band_pct": 3.0, "rebalance_to": "center", "nav": 150380.19, "names": names, "executed": [],
         "blocked": {}, "n_names": len(names), "n_hedge": 1, "n_inside": 0, "n_undecided": 1,
         "undecided": [{"ticker": "BILI", "reason": "price_stale", "detail": DH.REASONS["price_stale"]}]}
    s.update(kw)
    return s


def _audit(day, *, delta=None, stale=None):
    a = {"as_of": day, "nav": {"nav": 150380.19}, "spy_price": 771.79, "spy_mark": {"source": "cboe_close", "session": day},
         "aggregate": {"band": {"band_pct": 15.0}, "band_status": "unknown", "pct_nav": {"beta_dollar_delta": -18.2},
                       "coverage": {"n_rows": 24}, "partial": True},
         "aggregate_after": {"pct_nav": {"beta_dollar_delta": -18.2}, "band_status": "unknown"},
         "recommendation": {"action": "hold", "spy_shares": 0}, "executed": False, "executed_trade": None,
         "price_check": {"as_of": day, "n_stale": 1, "gaps": [], "spy": {"live": False},
                         "hedge_undecided": "partial data — no hedge on partial data (1 price missing)",
                         "stale": stale or [], "unpriced": []},
         "rows": [{"kind": "stock", "ticker": "NVDA", "price_source": "cboe_close"}]}
    if delta is not None:
        a["delta_hedge"] = delta
    return a


def _write(root: Path, audits: dict, *, ledger=None) -> Path:
    d = root / DIRNAME
    d.mkdir(parents=True)
    (d / "positions.jsonl").write_text("", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"cash": 0.0, "starting_date": "2026-09-04", "last_run_date": "2026-10-15"}), encoding="utf-8")
    (d / "equity_curve.jsonl").write_text("", encoding="utf-8")
    (d / "trades.jsonl").write_text("", encoding="utf-8")
    for day, a in audits.items():
        (d / f"greeks_{day}.json").write_text(json.dumps(a), encoding="utf-8")
    for fname, recs in (ledger or {}).items():
        if fname.endswith(".json"):
            (d / fname).write_text(json.dumps(recs), encoding="utf-8")
        else:
            (d / fname).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return root


def _svc(root):
    return AlphaBotService(fetch_fn=syn.demo_fetch, straddle_root=root, now_fn=lambda: NOW)


class TestDeltaOverview:
    def test_shadow_shows_the_latest_audit_section_and_no_ledger(self, tmp_path):
        root = _write(tmp_path / "d", {"2026-10-14": _audit("2026-10-14", delta=_section(names=[NAME_T])),
                                       "2026-10-15": _audit("2026-10-15", delta=_section())})
        D = _svc(root).hedge()["delta"]
        assert D["available"] and D["latest_date"] == "2026-10-15" and D["mode"] == "shadow"
        assert [n["ticker"] for n in D["names"]] == ["T", "BILI"]                          # 取最近一份，不是第一份
        assert D["opened"] is False and D["positions"] == [] and D["trades"] == [] and D["equity"] == []
        assert D["n_undecided"] == 1 and D["undecided"][0]["ticker"] == "BILI"

    def test_pct_nav_is_computed_server_side(self, tmp_path):
        root = _write(tmp_path / "d", {"2026-10-15": _audit("2026-10-15", delta=_section())})
        n = {x["ticker"]: x for x in _svc(root).hedge()["delta"]["names"]}
        assert n["T"]["option_pct_nav"] == pytest.approx(40014.0 / 150380.19 * 100, abs=1e-3)
        assert n["BILI"]["option_pct_nav"] is None                                          # 未决的没有 Delta ⇒ 不可得，不是 0

    def test_nav_basis_reaches_the_page_and_the_front_end_discloses_it(self, tmp_path):
        sec = _section(nav_basis="excluding_unmarked_delta_overlay", nav_unmarked=["BBB"])
        root = _write(tmp_path / "d", {"2026-10-15": _audit("2026-10-15", delta=sec)})
        D = _svc(root).hedge()["delta"]
        assert D["nav_basis"] == "excluding_unmarked_delta_overlay" and D["nav_unmarked"] == ["BBB"]
        js = (STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")
        assert 'D.nav_basis !== "full"' in js and "带宽用的 NAV 不含" in js

    def test_audit_without_the_section_says_so(self, tmp_path):
        root = _write(tmp_path / "d", {"2026-10-15": _audit("2026-10-15")})
        D = _svc(root).hedge()["delta"]
        assert D["available"] is False and D["names"] == [] and D["latest_date"] is None

    def test_paper_ledger_positions_and_gated_trades(self, tmp_path):
        trade_ok = {"date": "2026-10-15", "ticker": "T", "shares": -1703, "price": 23.5, "action": "sell", "fill": "close",
                    "reason": "hedge", "shares_after": -1703, "price_session": "2026-10-15", "price_source": "cboe_close",
                    "price_at_close": True}
        trade_unproven = {"date": "2026-10-14", "ticker": "VZ", "shares": -9, "price": 44.0, "action": "sell"}
        ledger = {"delta_positions.jsonl": [{"ticker": "T", "shares": -1703, "avg_price": 23.5, "last_price": 23.5,
                                             "last_mark_date": "2026-10-15", "entry_date": "2026-10-15"}],
                  "delta_meta.json": {"cash": 40020.5, "starting_date": "2026-10-15"},
                  "delta_trades.jsonl": [trade_unproven, trade_ok],
                  "delta_equity_curve.jsonl": [{"date": "2026-10-15", "nav": 0.5, "cash": 40020.5, "market_value": -40020.0,
                                                "n_positions": 1, "marks_complete": True, "marks_at_close": True}]}
        root = _write(tmp_path / "d", {"2026-10-15": _audit("2026-10-15", delta=_section(mode="paper"))}, ledger=ledger)
        D = _svc(root).hedge()["delta"]
        assert D["opened"] and D["positions"][0]["ticker"] == "T" and D["cash"] == pytest.approx(40020.5)
        assert [t["date"] for t in D["trades"]] == ["2026-10-15"]                          # 无定价证明的那笔被闸拦下
        assert D["gate"]["excluded_total"]["trades"] == 1
        assert "2026-10-14" not in json.dumps({k: v for k, v in D.items() if k != "gate"})  # 被拦的只给计数，不出现在别处
        assert [e["date"] for e in D["equity"]] == ["2026-10-15"]

    def test_unreadable_delta_positions_surface_as_an_error(self, tmp_path):
        root = _write(tmp_path / "d", {"2026-10-15": _audit("2026-10-15", delta=_section(mode="paper"))},
                      ledger={"delta_meta.json": {"cash": 0.0}})                            # 开了账、持仓文件却不在
        D = _svc(root).hedge()["delta"]
        assert "delta_positions" in D["errors"]

    def test_the_old_overview_keys_are_untouched(self, tmp_path):
        root = _write(tmp_path / "d", {"2026-10-15": _audit("2026-10-15", delta=_section())})
        d = _svc(root).hedge()
        assert {"account", "latest", "days", "equity", "trades", "gate", "freshness"} <= set(d)

    def test_demo_mode_has_no_crash_and_a_delta_key(self):
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, now_fn=lambda: NOW)
        d = svc.hedge()
        assert d["data_available"] is True and "delta" in d


class TestNamesTheCulprit:
    def test_stale_and_unpriced_names_reach_the_page_data(self, tmp_path):
        stale = [{"ticker": "BILI", "kind": "stock", "symbol": None, "seen_price": 14.3201, "session": "2026-10-15",
                  "source": "cboe_stale_intraday"}]
        root = _write(tmp_path / "d", {"2026-10-15": _audit("2026-10-15", stale=stale)})
        pc = _svc(root).hedge()["latest"]["price_check"]
        assert pc["stale"] == [{"ticker": "BILI", "kind": "stock", "seen_price": 14.3201, "session": "2026-10-15",
                                "source": "cboe_stale_intraday"}]
        assert pc["unpriced"] == []

    def test_front_end_writes_the_names_into_the_warning(self):
        js = (STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")
        assert "缺价的是" in js and "pc.stale" in js and "pc.unpriced" in js


class TestFrontendWiring:
    JS = (STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")

    @staticmethod
    def _js_object_keys(js, name):
        body = re.search(rf"const {name} = \{{(.*?)\n?\}};", js, re.S).group(1)
        return set(re.findall(r"^\s*([a-z_]+):", body, re.M)) | set(re.findall(r",\s*([a-z_]+):", body))

    def test_reason_table_matches_the_engine_registry_both_ways(self):
        keys = self._js_object_keys(self.JS, "D_REASON")
        assert keys == set(DH.REASONS), (keys ^ set(DH.REASONS))

    def test_status_table_covers_every_status_the_engine_can_emit(self):
        keys = self._js_object_keys(self.JS, "D_STATUS")
        out = DH.decide({"A": {"dd": 20000.0, "reason": None}, "B": {"dd": 100.0, "reason": None},
                         "C": {"dd": None, "reason": "option_delta_unknown"}}, {"OLD": 5.0, "FLAT": 0.0},
                        {t: {"price": 100.0, "session": "s", "source": "x", "at_close": True} for t in ("A", "B", "C", "OLD", "FLAT")},
                        nav=200000.0, band_pct=3.0)
        assert {d["status"] for d in out} | {"flat"} <= keys

    def test_panel_is_mounted_and_does_no_nav_arithmetic(self):
        assert "deltaPanel(d.delta, rules)" in self.JS
        panel_src = self.JS[self.JS.index("function deltaPanel"):self.JS.index("function gatePanel")]
        assert "/ D.nav" not in panel_src and "* 100" not in panel_src, "% NAV 在服务端算（option_pct_nav），前端不写公式"

    def test_help_constants_come_from_the_server_not_literals(self):
        help_js = (STATIC / "pages" / "help.js").read_text(encoding="utf-8")
        start = help_js.index("逐票 Delta 对冲（跨式腿）")
        sect = help_js[start:help_js.index("什么时候不对冲", start)]
        assert len(sect) > 400, "切片取空了——测试在空转"
        for key in ("delta_hedge_band_pct", "delta_hedge_rebalance_to", "delta_hedge_mode"):
            assert key in sect, key
        assert not re.search(r"\d+(\.\d+)?\s*%\s*才", sect), "带宽写成了字面量"

    def test_rules_reach_the_page_from_the_engine_config(self):
        v = pg.ledger_view(Path("/nonexistent-root-for-test"))
        assert {"delta_hedge_mode", "delta_hedge_band_pct", "delta_hedge_rebalance_to"} <= set(v["rules"])
        assert HG.build_overview({**v, "exists": False}, now_et=NOW)["data_available"] is False
