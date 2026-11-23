"""v0.45.803 到期日收盘后，当天到期的跨式腿 Greeks 记 0、mid 记锁定的内在价值（portfolio_greeks）。

事故：2026-11-20 NVDA 空跨式（行权价 222.5、11-20 到期）当晚，看涨腿深度虚值（收盘 125.53）。CBOE 到期当天收盘后
把要到期合约的 iv 记成 0（同一晚看跌腿 iv=0.0），看涨腿 bid=0 过不了 `bid_zero_greeks_usable` ⇒ 判「缺报价」⇒
整组 β·$Delta unknown ⇒ 当天对冲决定做不出来（「1 quote missing」）。

二次检查（用户 11-23 定）：跨式账本到期按**这一场收盘的内在价值现金结算**，收盘后实值腿对标的也已没有敞口——
CBOE 给实值看跌腿的 delta −1.0 会让 SPY 覆盖继续对冲一笔已锁定的仓位。所以规则是：as_of == 到期日、S 是这一场的官方
收盘 ⇒ 当天到期的腿**不论报价能不能用**，Greeks 全 0、mid = 内在价值，行上标 `expiry_intrinsic`。
S 不是收盘 / 没有价 / 不是到期日 ⇒ 照旧（报价可用就用、不可用就算缺）。全部离线：价格 / 报价 / β 一律注入。"""

import pathlib
import re

import pytest

import portfolio_greeks as pg
from alphabot import hedge as hedge_page
from alphabot import straddle as sd

AS_OF = EXPIRY = "2026-11-20"           # 周五、交易日（到期日当晚）
K = 222.5
CALL, PUT = "XYZ261120C00222500", "XYZ261120P00222500"
_ROOT = pathlib.Path(pg.__file__).resolve().parent


def _position(contracts=3, expiry=EXPIRY):
    return {"ticker": "XYZ", "side": "short", "entry_date": "2026-10-21", "expiry": expiry, "strike": K,
            "call_symbol": CALL, "put_symbol": PUT, "contracts": contracts, "entry_call": 9.9, "entry_put": 9.65,
            "entry_premium": 19.55, "entry_underlying": 221.97, "earnings_date": "2026-11-17",
            "signal_ratio": 3.8, "label": "rich", "size_usd": 5865.0, "last_mark": 68.575,
            "last_mark_date": "2026-11-17", "mark_source": "stale"}


def _zeroed():
    """到期当晚 CBOE 给的虚值腿：bid 0、iv 0（Greeks 全零占位）——`quote_ok` 假、`bid_zero_greeks_usable` 也假。"""
    return {"bid": 0.0, "ask": 0.0, "mid": None, "iv": 0.0, "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
            "dte": 0, "quote_ok": False, "role": "held"}


def _usable(delta=-1.0, mid=97.025):
    return {"bid": mid - 0.1, "ask": mid + 0.1, "mid": mid, "iv": 0.0, "delta": delta, "gamma": 0.0, "vega": 0.0,
            "theta": 0.0, "dte": 0, "quote_ok": True, "role": "held"}


def _rows(call_q, put_q=None, *, S=125.53, mark=None, position=None, as_of=AS_OF):
    table = {CALL: call_q, PUT: put_q if put_q is not None else _usable()}
    closes = (lambda tk, d: mark) if mark is not None else (lambda tk, d: S)   # 裸数字 = 调用方担保是这一场的收盘
    return pg.option_exposures(as_of, positions=[position or _position()],
                               quotes_fn=lambda tk, syms: {s: table.get(s) for s in syms},
                               closes_fn=closes, beta_fn=lambda tk, d: (1.6, "ols60"))


def _leg(rows, cp):
    return next(r for r in rows if r["cp"] == cp)


class TestExpiryDayIntrinsic:
    def test_otm_call_is_delta_zero_not_missing(self):
        """11-20 那一晚的形状：虚值看涨腿报价被清零 ⇒ Delta 0，整组不再 unknown。"""
        rows = _rows(_zeroed())
        c = _leg(rows, "call")
        assert c["quote_missing"] is False and c["expiry_intrinsic"] is True
        assert c["delta"] == 0.0 and c["mid"] == 0.0 and c["dollar_delta"] == 0.0 and c["beta_dollar_delta"] == 0.0
        agg = pg.aggregate(rows, nav=140_000.0)
        assert agg["coverage"]["n_quote_missing"] == 0 and agg["coverage"]["n_expiry_intrinsic"] == 2
        assert agg["partial"] is False and agg["band_status"] != "unknown"

    def test_itm_leg_with_usable_quote_is_also_zero(self):
        """实值看跌腿 CBOE 报价能用、给 delta −1：收盘已锁定、现金结算 ⇒ 照样记 0，mid = 内在价值。"""
        p = _leg(_rows(_zeroed(), put_q=_usable(delta=-1.0, mid=97.025)), "put")
        assert p["expiry_intrinsic"] is True and p["delta"] == 0.0 and p["dollar_delta"] == 0.0
        assert p["mid"] == pytest.approx(K - 125.53)
        c = _leg(_rows(_zeroed(), S=230.0), "call")
        assert c["delta"] == 0.0 and c["mid"] == pytest.approx(230.0 - K)

    def test_close_exactly_at_strike_is_zero_too(self):
        """收盘恰在行权价上：现金结算的内在价值是 0，没有歧义。"""
        c = _leg(_rows(_zeroed(), S=K), "call")
        assert c["expiry_intrinsic"] is True and c["delta"] == 0.0 and c["mid"] == 0.0

    def test_not_expiry_day_unchanged(self):
        """到期日之前：报价不可用照旧算缺、可用照用 CBOE（行为与修改前相同）。"""
        rows = _rows(_zeroed(), put_q=_usable(delta=-0.9), position=_position(expiry="2026-11-27"))
        c, p = _leg(rows, "call"), _leg(rows, "put")
        assert c["dte"] == 7 and c["quote_missing"] is True and not c.get("expiry_intrinsic")
        assert p["delta"] == -0.9 and not p.get("expiry_intrinsic")

    def test_price_not_the_official_close_unchanged(self):
        """S 是盘中实时价而不是这一场的收盘 ⇒ 还没到期结算，照旧。"""
        live = {"price": 125.53, "session": AS_OF, "source": "cboe_live", "at_close": False, "live": True}
        rows = _rows(_zeroed(), mark=live)
        c, p = _leg(rows, "call"), _leg(rows, "put")
        assert c["price"] == 125.53 and c["quote_missing"] is True and not c.get("expiry_intrinsic")
        assert p["delta"] == -1.0 and not p.get("expiry_intrinsic")

    def test_no_price_unchanged(self):
        c = _leg(_rows(_zeroed(), mark={"price": None, "session": None, "source": "unavailable",
                                          "at_close": False, "live": False}), "call")
        assert c["quote_missing"] is True and not c.get("expiry_intrinsic")


class TestVisibility:
    def test_price_check_lists_the_leg_and_no_gap(self):
        rows = _rows(_zeroed())
        agg = pg.aggregate(rows, nav=140_000.0)
        rec = pg.hedge_recommendation(agg, 689.81, 140_000.0)
        chk = pg._price_check(AS_OF, rows, {"price": 689.81}, rec)
        assert chk["n_expiry_intrinsic"] == 2 and chk["expiry_intrinsic"] == [CALL, PUT]
        assert chk["n_quote_missing"] == 0 and chk["gaps"] == [] and chk["hedge_undecided"] is None
        b = hedge_page.audit_brief(AS_OF, {"aggregate": agg, "price_check": chk, "recommendation": rec})
        assert b["price_check"]["n_expiry_intrinsic"] == 2 and b["price_check"]["expiry_intrinsic"] == [CALL, PUT]
        old = hedge_page.audit_brief(AS_OF, {"aggregate": {}, "price_check": {"n_stale": 0, "gaps": []}})
        assert old["price_check"]["n_expiry_intrinsic"] == 0 and old["price_check"]["expiry_intrinsic"] == []

    def test_stress_table_excludes_it_with_a_named_reason(self):
        st = pg.stress_table(_rows(_zeroed()))
        assert f"{CALL}(option:expiry-day intrinsic)" in st["excluded"]

    def test_markdown_and_page_say_so(self):
        rows = _rows(_zeroed())
        agg = pg.aggregate(rows, nav=140_000.0)
        res = {"as_of": AS_OF, "aggregate": agg, "rows": rows,
               "recommendation": pg.hedge_recommendation(agg, 689.81, 140_000.0),
               "stress": pg.stress_table(rows), "executed": False}
        assert "已按收盘内在价值锁定" in pg.render_markdown(AS_OF, res)
        js = (_ROOT / "alphabot" / "static" / "pages" / "hedge.js").read_text(encoding="utf-8")
        assert re.search(r"if \(pc\.n_expiry_intrinsic\)\s*warns\.push\([^;]*当天到期的腿", js), "hedge.js 缺到期内在价值提示"


class TestHelper:
    @pytest.mark.parametrize("cp,S,mid", [("call", 130.0, 10.0), ("call", 100.0, 0.0), ("put", 100.0, 20.0),
                                          ("put", 130.0, 0.0), ("call", 120.0, 0.0)])
    def test_expiry_intrinsic(self, cp, S, mid):
        g = pg._expiry_intrinsic(cp, S, 120.0)
        assert g["delta"] == g["gamma"] == g["vega"] == g["theta"] == 0.0 and g["iv"] is None
        assert g["mid"] == pytest.approx(mid)

    @pytest.mark.parametrize("cp,S,K_", [("call", None, 120.0), ("call", 120.0, None), ("x", 130.0, 120.0),
                                         ("put", 0.0, 120.0)])
    def test_undecidable_is_none(self, cp, S, K_):
        assert pg._expiry_intrinsic(cp, S, K_) is None


class TestStraddlePageLedgerView:
    """跨式页账本视图读同一份审计行：按内在价值计的腿要点名，别当成 CBOE 报价默默进合计。"""

    def _view(self, call_q):
        legs = sd._greek_legs({"rows": _rows(call_q)})
        return sd.position_view(_position(), as_of=AS_OF, today=AS_OF, sig_today={"underlying_price": 125.53, "as_of": AS_OF},
                                sig_entry=None, greek_legs=legs)["greeks"]

    def test_intrinsic_leg_is_named(self):
        g = self._view(_zeroed())
        assert g["intrinsic_legs"] == ["call", "put"] and g["degraded_legs"] == [] and g["dollar_delta"] == 0.0

    def test_normal_legs_not_named(self):
        legs = sd._greek_legs({"rows": _rows(_usable(delta=0.0, mid=0.01), position=_position(expiry="2026-11-27"))})
        g = sd.position_view(_position(expiry="2026-11-27"), as_of=AS_OF, today=AS_OF,
                             sig_today={"underlying_price": 125.53, "as_of": AS_OF}, sig_entry=None, greek_legs=legs)["greeks"]
        assert g["intrinsic_legs"] == []

    def test_page_shows_the_note(self):
        js = (_ROOT / "alphabot" / "static" / "pages" / "straddle.js").read_text(encoding="utf-8")
        assert re.search(r"g\.intrinsic_legs\?\.length[^?]*\?\s*h\([^;]*内在价值", js), "straddle.js 缺内在价值腿提示"
