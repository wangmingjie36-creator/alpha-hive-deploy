"""Alpha Bot「怎么卖」（v0.45.515，`alphabot/howto.py`）的守卫。

每组回答一句「谁会红」：
  · 与表同源：推荐价位里的自然价 / 可报价性就是「六种结构」表里那一份，不是另算一遍（`TestSameQuoteAsTheTable`）；
  · 算术：中价 / 底线 / 止盈 / 止损 / 管理日用手工梯子独立复算，价差类止损失效时如实标 None（`TestArithmetic`）；
  · 提示：财报 / 负 gamma / 点差只在该出现的地方出现（`TestCautions`）；
  · 说明完整：六种结构各一份、字段不空、顺序与 `STRUCTURES` 一致（`TestGuide`）；
  · 纯函数：不 import 卖权模块、不读写文件、不联网（`TestPurity`）；
  · 失败可见：现算抛错 ⇒ `available: False` + 计数 + 日志，不是「这一栏凭空消失」（`TestServiceWiring`）。
全部离线（合成链注入 `fetch_fn`）。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

import sell_strike_candidates as C
from alphabot import howto as HW
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService

REPO = Path(__file__).resolve().parent.parent
AS_OF = "2026-09-01"


def _svc(**kw):
    kw.setdefault("fetch_fn", syn.demo_fetch)
    kw.setdefault("bars_fn", lambda t: {"available": True, "source": "demo", "bars": syn.demo_bars(t, AS_OF)})
    return AlphaBotService(**kw)


def _leg(K, cp, bid, ask, *, delta=0.2, sig=1.0, itm=0.2):
    mid = (bid + ask) / 2.0
    return {"symbol": f"T{cp}{int(K)}", "strike": float(K), "cp": cp, "delta": delta, "bid": bid, "ask": ask,
            "mid": mid, "spread_pct": (ask - bid) / mid, "quote_ok": True, "itm_prob": itm, "sigma_distance": sig}


def _ladder(S=100.0, dte=30):
    """手工梯子：只填 0.20 档（put 短 95 / 保护 90；call 短 105 / 保护 110）。"""
    return {"underlying_price": S, "dte": dte,
            "put": {"0.20": {"short": _leg(95, "P", 1.50, 1.60, delta=-0.2), "wing": _leg(90, "P", 0.50, 0.60, delta=-0.1)}},
            "call": {"0.20": {"short": _leg(105, "C", 1.20, 1.30), "wing": _leg(110, "C", 0.30, 0.40, delta=0.1)}}}


def _detail(ladder, *, tenor="monthly", expiry="2026-10-30", earnings="none", reasons=None, route=0.20):
    S = ladder["underlying_price"]
    row = {"status": "recorded", "ladder": ladder, "expiry": expiry, "dte": ladder["dte"], "earnings_status": earnings,
           "route": {"reasons": reasons or ["positive_gamma_at_spot:crosses"]},
           "structures": {"route_rungs": {"put": route, "call": route}}}
    return {"underlying_price": S, "tenors": {tenor: row},
            "views": {"le_45dte": {"zero_gamma": {"nearest_below": 92.0, "nearest_above": 108.0},
                                   "majors": {"net_major_neg_strike": 90.0, "net_major_pos_strike": 110.0}}},
            "expected_move": {tenor: {"lo": 92.0, "hi": 108.0}}}


def _build(detail):
    return HW.build(detail, C.structure_quote, C.BASE_RUNG)


class TestSameQuoteAsTheTable:
    def test_every_tier_matches_the_structures_table(self):
        """推荐价位的「自然价 / 可报价」就是表里那一份：谁改了一边另一边不动，这里红。"""
        d = _svc().live("NVDA")
        assert d["howto"]["available"] is True
        seen = 0
        for tenor, per in d["howto"]["tenors"].items():
            sts = d["tenors"][tenor]["structures"]
            for st, tiers in per.items():
                for label in ("route", "base"):
                    q, tier = sts[label][st], tiers[label]
                    if tier is None:
                        assert q.get("quotable") is not True or q.get("rung") is None
                        continue
                    assert tier["quotable"] == q["quotable"], (tenor, st, label)
                    if tier["quotable"]:
                        assert tier["limit"]["natural"] == pytest.approx(q["credit"], abs=1e-4), (tenor, st, label)
                        assert tier["payout"]["breakevens"] == q["breakevens"]
                        assert tier["limit"]["yield_start"] >= q["yield_raw"] - 1e-9
                        seen += 1
        assert seen >= 10, "几乎没有可报价的档——这条测试在空转"

    def test_legs_are_the_quote_legs(self):
        d = _svc().live("NVDA")
        ic = d["howto"]["tenors"]["monthly"]["iron_condor"]["route"]
        assert [l["action"] for l in ic["legs"]] == ["sell", "buy", "sell", "buy"]
        assert all(l["symbol"] and l["strike"] for l in ic["legs"])


class TestArithmetic:
    def test_bull_put_spread_numbers(self):
        """卖 95P@bid1.50 / 买 90P@ask0.60 ⇒ 自然 0.90；中价 1.55−0.55=1.00。
        起挂 1.00；底线 = 0.90 + 0.5×(1.00−0.90) = 0.95；止盈 0.50；止损 2.00（宽度 5 > 2）。"""
        det = _detail(_ladder(), route=0.20)
        t = _build(det)["tenors"]["monthly"]["bull_put_spread"]["route"]
        L, X, P = t["limit"], t["exit"], t["payout"]
        assert L["natural"] == pytest.approx(0.90)
        assert L["start"] == pytest.approx(1.00) and L["floor"] == pytest.approx(0.95)
        assert L["start_per_contract"] == pytest.approx(100.0)
        assert X["take_profit_debit"] == pytest.approx(0.50)
        assert X["stop_debit"] == pytest.approx(2.00) and X["stop_loss_per_contract"] == pytest.approx(100.0)
        assert P["max_loss_per_contract"] == pytest.approx((5 - 0.90) * 100)
        assert X["manage"] == {"dte": 21, "date": "2026-10-09"}

    def test_floor_is_between_natural_and_start(self):
        for tiers in _build(_detail(_ladder()))["tenors"]["monthly"].values():
            t = tiers["route"]
            assert t["quotable"]
            assert t["limit"]["natural"] - 1e-9 <= t["limit"]["floor"] <= t["limit"]["start"] + 1e-9

    def test_spread_stop_is_void_when_it_exceeds_the_width(self):
        """宽度 5、权利金 2.6 ⇒ 2×权利金 = 5.2 ≥ 宽度：买回价止损不可能触发，必须是 None + 说明，不是一个看着合理的数。"""
        lad = _ladder()
        lad["put"]["0.20"]["short"] = _leg(95, "P", 3.20, 3.30)
        lad["put"]["0.20"]["wing"] = _leg(90, "P", 0.50, 0.60)
        t = _build(_detail(lad))["tenors"]["monthly"]["bull_put_spread"]["route"]
        assert t["quotable"] and t["exit"]["stop_debit"] is None
        assert "宽度" in t["exit"]["stop_note"]

    def test_unbounded_structures_say_so(self):
        per = _build(_detail(_ladder()))["tenors"]["monthly"]
        assert per["short_call"]["route"]["payout"]["max_loss_unbounded"] is True
        assert per["strangle"]["route"]["payout"]["max_loss_unbounded"] is True
        assert per["iron_condor"]["route"]["payout"]["max_loss_unbounded"] is False

    def test_rules_flow_into_numbers(self, monkeypatch):
        """常量是唯一真相：把止盈比例改成 0.25，推荐价位必须跟着变（不是前端另抄了一份）。"""
        monkeypatch.setitem(HW.RULES, "profit_take", 0.25)
        t = _build(_detail(_ladder()))["tenors"]["monthly"]["bull_put_spread"]["route"]
        assert t["exit"]["take_profit_debit"] == pytest.approx(0.25)

    def test_unquotable_tier_has_no_prices(self):
        lad = _ladder()
        lad["put"]["0.20"]["short"]["quote_ok"] = False
        t = _build(_detail(lad))["tenors"]["monthly"]["short_put"]["route"]
        assert t["quotable"] is False and "limit" not in t and "exit" not in t

    def test_route_unavailable_side_gives_no_route_tier(self):
        det = _detail(_ladder())
        det["tenors"]["monthly"]["structures"]["route_rungs"] = {"put": None, "call": 0.20}
        per = _build(det)["tenors"]["monthly"]
        assert per["short_put"]["route"] is None and per["short_put"]["base"]["quotable"]
        assert per["short_call"]["route"]["quotable"] and per["iron_condor"]["route"] is None

    def test_levels_cover_strikes_breakevens_and_environment(self):
        t = _build(_detail(_ladder()))["tenors"]["monthly"]["iron_condor"]["route"]
        kinds = {lv["kind"] for lv in t["levels"]}
        assert {"short_strike", "wing_strike", "breakeven", "em_edge", "zero_gamma", "major"} <= kinds
        prices = [lv["price"] for lv in t["levels"]]
        assert prices == sorted(prices)
        pcts = {lv["price"]: lv["dist_pct"] for lv in t["levels"]}
        assert pcts[95.0] == pytest.approx(-5.0) and pcts[105.0] == pytest.approx(5.0)


class TestCautions:
    def _codes(self, det, st):
        return {c["code"] for c in _build(det)["tenors"]["monthly"][st]["route"]["cautions"]}

    def test_earnings_before_expiry_and_unknown_warn(self):
        assert "earnings_before_expiry" in self._codes(_detail(_ladder(), earnings="before_expiry"), "short_put")
        assert "earnings_unknown" in self._codes(_detail(_ladder(), earnings="unknown"), "short_put")
        for ok in ("none", "after_expiry"):
            assert not {"earnings_before_expiry", "earnings_unknown"} & self._codes(_detail(_ladder(), earnings=ok), "short_put")

    def test_negative_gamma_only_escalates_for_naked_call_structures(self):
        det = _detail(_ladder(), reasons=["negative_gamma_at_spot:all_negative"])
        assert "negative_gamma_naked" in self._codes(det, "strangle")
        assert "negative_gamma_naked" in self._codes(det, "short_call")
        for st in ("short_put", "bull_put_spread", "bear_call_spread", "iron_condor"):
            codes = self._codes(det, st)
            assert "negative_gamma_naked" not in codes and "negative_gamma" in codes
        assert not {"negative_gamma", "negative_gamma_naked"} & self._codes(_detail(_ladder()), "strangle")

    def test_wide_spread_is_flagged(self):
        lad = _ladder()
        lad["put"]["0.20"]["short"] = _leg(95, "P", 1.00, 1.40)        # 点差约 33%
        assert "wide_spread" in self._codes(_detail(lad), "bull_put_spread")
        assert "wide_spread" not in self._codes(_detail(_ladder()), "short_call")


class TestGuide:
    FIELDS = ("name", "oneline", "stance", "order", "pnl", "manage", "risks", "avoid")

    def test_structures_match_the_engine(self):
        assert HW.STRUCTURES == C.STRUCTURES
        assert set(HW.GUIDE) == set(C.STRUCTURES)

    @pytest.mark.parametrize("st", C.STRUCTURES)
    def test_every_guide_is_complete(self, st):
        g = HW.GUIDE[st]
        for f in self.FIELDS:
            assert g.get(f), (st, f)
        assert all(isinstance(x, str) and x for x in g["order"] + g["pnl"] + g["manage"] + g["risks"])
        assert len(g["order"]) >= 2 and len(g["manage"]) >= 2

    def test_multi_leg_guides_warn_against_legging(self):
        for st in ("bull_put_spread", "bear_call_spread", "iron_condor"):
            assert any("组合单" in x for x in HW.GUIDE[st]["order"]), st

    def test_public_rules_are_json_clean(self):
        import json
        json.dumps(HW.public_rules())


class TestPurity:
    SRC = (REPO / "alphabot" / "howto.py").read_text(encoding="utf-8")

    def test_no_sell_module_import_and_no_io(self):
        tree = ast.parse(self.SRC)
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not {m for m in mods if m.startswith("sell_strike")}, mods
        assert not mods & {"os", "pathlib", "socket", "requests", "urllib", "subprocess", "shutil", "json"}, mods
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"open", "print", "exec", "eval"}, calls

    def test_guard_has_teeth(self):
        bad = ast.parse("import sell_strike_report\nopen('x')")
        mods = {n.names[0].name for n in ast.walk(bad) if isinstance(n, ast.Import)}
        assert any(m.startswith("sell_strike") for m in mods)


class TestServiceWiring:
    def test_live_payload_carries_howto_and_method_carries_rules(self):
        svc = _svc()
        assert svc.live("NVDA")["howto"]["available"] is True
        assert svc.meta_texts()["howto"]["rules"]["profit_take"] == HW.RULES["profit_take"]

    def test_failure_is_visible_not_silent(self, monkeypatch, caplog):
        """现算抛错 ⇒ available False + 原因 + 计数 + warning；而不是 `howto` 键凭空消失、前端当成「没有这栏」。"""
        def boom(*a, **k):
            raise RuntimeError("probe")
        monkeypatch.setattr(HW, "build", boom)
        svc = _svc()
        with caplog.at_level(logging.WARNING):
            d = svc.live("NVDA")
        assert d["howto"]["available"] is False and "RuntimeError" in d["howto"]["reason"]
        assert svc.stats["howto_failures"] == 1
        assert any("怎么卖" in r.getMessage() for r in caplog.records)
        assert d["data_available"] is True        # 失败不连坐整只票

    def test_http_endpoints(self):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        c = TestClient(create_app(_svc(), port=8765), base_url="http://127.0.0.1:8765")
        live = c.get("/api/live/NVDA").json()
        assert live["howto"]["tenors"]["monthly"]["short_put"]["route"]["limit"]["start"] > 0
        assert c.get("/api/method").json()["howto"]["rules"]["stop_mult"] == HW.RULES["stop_mult"]


class TestFrontendWiring:
    STATIC = REPO / "alphabot" / "static"

    def test_ticker_passes_the_server_payload(self):
        t = (self.STATIC / "pages" / "ticker.js").read_text(encoding="utf-8")
        assert 'from "./howto.js"' in t and "howtoPanel({ hw: d.howto" in t

    def test_frontend_has_no_price_formulas(self):
        """页面只排版：不许出现起挂 / 底线 / 止盈 / 止损的运算（那在 howto.py，常量在 RULES）。"""
        js = (self.STATIC / "pages" / "howto.js").read_text(encoding="utf-8")
        for needle in ("* 0.5", "* 2", "0.5 *", "2 *", "mid -", "- mid"):
            assert needle not in js, needle
