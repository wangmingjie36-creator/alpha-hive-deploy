"""v0.45.609 bid = 0 的深度虚值腿不再让整个组合 β·$Delta 变 unknown（portfolio_greeks）。

事故：2026-10-21 T 的空头看涨腿（比现价高 12%、bid 0.00 / ask 0.03）`quote_ok` 为假 ⇒ 整行被当「缺报价」、
Greeks 全丢 ⇒ 汇总里 1 行 β·$Delta 缺 ⇒ band_status="unknown" ⇒ 当天整个组合不对冲，而 CBOE 其实给了 delta=0.05。
修法只放宽**对冲 Greeks 汇总**这一处：bid = 0、ask 真实、iv > 0、四个 Greeks 齐全 ⇒ Greeks 照进汇总、行上标
`bid_zero`、mid 仍为 None。卖权账本 / 跨式入场用的 `quote_ok`（25% 点差上限等）一个字不碰——下面有断言。

全部离线：价格 / 报价 / β 一律注入。"""

import ast
import inspect

import pytest

import cboe_options
import portfolio_greeks as pg
from greeks_engine import calculate_single

AS_OF = "2026-09-03"
EXPIRY = "2026-10-02"
S, K, IV = 100.0, 100.0, 0.40
CALL, PUT = "XYZ261002C00100000", "XYZ261002P00100000"


def _position(side="short", contracts=39):
    return {"ticker": "XYZ", "side": side, "entry_date": "2026-09-01", "expiry": EXPIRY, "strike": K,
            "call_symbol": CALL, "put_symbol": PUT, "contracts": contracts, "entry_call": 4.0, "entry_put": 3.8,
            "entry_premium": 7.8, "entry_underlying": 100.0, "earnings_date": "2026-09-20",
            "signal_ratio": 0.5, "label": "rich", "size_usd": 7.8 * 100 * contracts, "last_mark": 7.8,
            "last_mark_date": "2026-09-01", "mark_source": "cboe_mid"}


def _good(cp):
    g = calculate_single(S, K, 29 / 365.0, pg.CONFIG["risk_free"], IV, cp)
    mid = round(g["price"], 4)
    return {"bid": round(mid - 0.05, 4), "ask": round(mid + 0.05, 4), "mid": mid, "iv": IV,
            "delta": g["delta"], "gamma": g["gamma"], "vega": g["vega"], "theta": g["theta"],
            "dte": 29, "quote_ok": True, "role": "held"}


def _bid_zero(**over):
    """CBOE 的深度虚值腿：bid 0.00 / ask 0.03，`_qs_contract` 因此 quote_ok=False、mid=None，Greeks 与 iv 仍在。"""
    q = {"bid": 0.0, "ask": 0.03, "mid": None, "iv": 0.3065, "delta": 0.0519, "gamma": 0.094, "vega": 0.0037,
         "theta": -0.0068, "dte": 29, "quote_ok": False, "role": "held"}
    q.update(over)
    return q


def _rows(call_quote, side="short"):
    table = {CALL: call_quote, PUT: _good("put")}
    return pg.option_exposures(AS_OF, positions=[_position(side)], quotes_fn=lambda tk, syms: {s: table.get(s) for s in syms},
                               closes_fn=lambda tk, d: S, beta_fn=lambda tk, d: (1.0, "ols60"))


def _call(rows):
    return next(r for r in rows if r["cp"] == "call")


class TestBidZeroLegEntersGreeks:
    def test_row_is_not_quote_missing_and_keeps_delta(self):
        c = _call(_rows(_bid_zero()))
        assert c["quote_missing"] is False and c["bid_zero"] is True
        assert c["mid"] is None                                   # bid=0 的 mid 是 ask 的一半，不是市价——不编
        qty = -3900.0
        assert c["qty"] == qty
        assert c["dollar_delta"] == pytest.approx(qty * 0.0519 * S, abs=0.01)
        assert c["vega_dollar_per_pt"] == pytest.approx(qty * 0.0037, abs=0.01)
        assert c["theta_dollar_per_day"] == pytest.approx(qty * -0.0068, abs=0.01)
        assert c["gamma_dollar_per_1pct"] == pytest.approx(0.5 * qty * 0.094 * (0.01 * S) ** 2, abs=0.01)
        assert c["iv"] == pytest.approx(0.3065)

    def test_band_no_longer_unknown_and_sums_include_the_leg(self):
        rows = _rows(_bid_zero())
        agg = pg.aggregate(rows, nav=150_000.0)
        assert agg["coverage"]["n_quote_missing"] == 0 and agg["band_status"] != "unknown"
        put = next(r for r in rows if r["cp"] == "put")
        assert agg["sums"]["dollar_delta"] == pytest.approx(_call(rows)["dollar_delta"] + put["dollar_delta"], abs=0.02)

    def test_price_check_lists_the_bid_zero_leg_without_calling_it_a_gap(self):
        rows = _rows(_bid_zero())
        agg = pg.aggregate(rows, nav=150_000.0)
        rec = pg.hedge_recommendation(agg, 700.0, 150_000.0)
        chk = pg._price_check(AS_OF, rows, {"price": 700.0}, rec)
        assert chk["n_bid_zero"] == 1 and chk["bid_zero"] == [CALL]
        assert chk["n_quote_missing"] == 0 and chk["gaps"] == []   # 不是缺：不进告警清单

    def test_normal_rows_carry_no_bid_zero_key(self):
        rows = _rows(_good("call"))
        assert all("bid_zero" not in r for r in rows)
        assert pg._price_check(AS_OF, rows, {"price": 700.0}, None)["n_bid_zero"] == 0

    def test_stress_still_excludes_it_and_says_why(self):
        """压力表的基准价是 mid；bid=0 没有 mid ⇒ 这一行仍被剔（网格 partial、不编一个 mid），但标签说清是 bid=0。"""
        st = pg.stress_table(_rows(_bid_zero()))
        assert st["partial"] is True
        assert any(CALL in e and "mid(bid=0)" in e for e in st["excluded"])
        assert st["excluded_dd_unknown"] == 0                      # 现在 $Delta 是知道的，剔除规模按毛额记账
        assert st["excluded_dollar_delta"] == pytest.approx(abs(_call(_rows(_bid_zero()))["dollar_delta"]), abs=0.02)


class TestStillMissingWhenGreeksCannotBeTrusted:
    """只放宽「bid=0 且 Greeks 可信」这一种；其他不合格的报价照旧判缺。"""

    @pytest.mark.parametrize("over", [
        {"iv": 0.0},                       # iv ≤ 0：CBOE 的全零占位，不是观测值
        {"iv": None},
        {"ask": 0.0},                      # 没有真实卖价
        {"ask": None},
        {"delta": None},
        {"theta": float("nan")},
        {"bid": 0.05, "ask": 0.03},        # 倒挂（bid>0 的不合格报价不在放宽范围内）
        {"bid": 0.02, "ask": 0.40},        # bid>0 但 quote_ok 被判假（例如点差过宽的其他原因）：不放宽
        {"bid": None},
    ])
    def test_unusable_bid_zero_variants_stay_missing(self, over):
        c = _call(_rows(_bid_zero(**over)))
        assert c["quote_missing"] is True and c["dollar_delta"] is None and "bid_zero" not in c

    def test_quote_ok_false_for_non_bid_zero_reason_stays_missing(self):
        """quote_ok=False 但 bid>0（比如上游另有原因判不合格）——不是 bid=0 那一类，不放宽。"""
        c = _call(_rows(_bid_zero(bid=0.01, ask=0.03)))
        assert c["quote_missing"] is True

    def test_none_quote_stays_missing(self):
        assert _call(_rows(None))["quote_missing"] is True


class TestStrictQuoteOkRuleUntouched:
    """卖权 / 跨式入场共用的 `quote_ok`（bid > 0 …）一个字不动：这次只在 portfolio_greeks 的 Greeks 汇总里放宽。"""

    def test_qs_contract_still_rejects_bid_zero(self):
        q = cboe_options._qs_contract({"bid": 0.0, "ask": 0.03, "iv": 0.3, "delta": 0.05, "gamma": 0.09,
                                       "vega": 0.004, "theta": -0.007}, role="held", cp="C", strike=24.5,
                                      expiry="2026-10-30", dte=8)
        assert q["quote_ok"] is False and q["mid"] is None and q["spread_pct"] is None

    def test_helper_lives_only_in_portfolio_greeks(self):
        """放宽的判据不许被别的账本顺手 import（那会把预注册口径悄悄放松）。"""
        import pathlib
        root = pathlib.Path(pg.__file__).resolve().parent
        users = []
        for path in root.glob("*.py"):
            if path.name == "portfolio_greeks.py":
                continue
            if "_bid_zero_greeks_usable" in path.read_text(encoding="utf-8", errors="ignore"):
                users.append(path.name)
        assert users == []

    def test_helper_is_pure_and_not_importing_alphabot(self):
        src = inspect.getsource(pg._bid_zero_greeks_usable)
        tree = ast.parse(src.lstrip())
        assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
