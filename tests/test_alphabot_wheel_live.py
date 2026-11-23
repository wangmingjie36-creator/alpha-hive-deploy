"""Alpha Bot 接货页「此刻浮动」（v0.45.801）的守卫。离线：报价函数一律注入。每组回答一句「谁会红」：

  · 口径：卖出腿浮动 =（权利金 − 此刻 mid）× 100 × 张数、较收盘 =（收盘 mark − 此刻 mid）× …；持股 =（现价 − 接货价）× 股数；
    此刻净值与账本自己的 `sell_put_wheel._nav_estimate` 同一公式（喂同一份报价，两边逐分钱相等）（`TestLiveView`）；
  · 算不出来就说算不出来：报价没取到 / 合约不在文件里 / 没有合格双边报价（与账本盯市同一判据 `mark_of`）⇒ 那一行写原因、
    合计净值不给，**不拿收盘 mark 顶上**（`TestHonestStates`）；
  · 服务端：每只票一份报价、缓存与「立即刷新」最小间隔、失败也缓存（坏票不被每次重下）、演示不展示、没接取数不出网（`TestService`）；
  · 接线：路由与页面挂载（`TestWiring`）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import sell_put_wheel as W
from alphabot import wheel as WH
from alphabot.service import AlphaBotService
from tests.test_alphabot_wheel import STATIC, _state

PUT, CALL = "AAA270115P00090000", "BBB270115C00055000"


def _q(available=True, quotes=None, px=None, **extra):
    return {"available": available, "reason": None if available else extra.pop("reason", "payload_unavailable"),
            "quotes": quotes or {}, "underlying_price": px, "session_live": True, "vintage_date": "2026-12-03", **extra}


def _quotes(put=(1.0, 1.2), call=(2.5, 2.7), bbb_px=54.0):
    return {"AAA": _q(quotes={PUT: {"bid": put[0], "ask": put[1]}}, px=101.0),
            "BBB": _q(quotes={CALL: {"bid": call[0], "ask": call[1]}}, px=bbb_px)}


def _st():
    s = _state()
    s["meta"] = {**(s.get("meta") or {}), "cash": 100000.0, "starting_capital": 100000.0, "last_run_date": "2026-12-02"}
    return {"available": True, **s}


class TestLiveView:
    def test_leg_and_share_pnl(self):
        v = WH.live_view(_st(), _quotes())
        aaa, bbb = v["rows"]
        assert aaa["put"]["mid"] == pytest.approx(1.1)
        assert aaa["put"]["unrealized_usd"] == pytest.approx((2.0 - 1.1) * 100)
        assert aaa["put"]["since_close_usd"] == pytest.approx((1.5 - 1.1) * 100)
        assert bbb["call"]["unrealized_usd"] == pytest.approx((3.0 - 2.6) * 100)
        assert bbb["shares"]["unrealized_usd"] == pytest.approx((54.0 - 50.0) * 100)
        assert bbb["shares"]["since_close_usd"] == pytest.approx((54.0 - 52.0) * 100)
        assert bbb["unrealized_usd"] == pytest.approx(40.0 + 400.0)
        assert v["nav"] == pytest.approx(100000.0 + 54.0 * 100 - 1.1 * 100 - 2.6 * 100)
        assert v["since_close_usd"] == pytest.approx(v["nav"] - 100500.0)
        assert v["return_pct"] == pytest.approx((v["nav"] / 100000.0 - 1) * 100, abs=1e-3)

    def test_nav_matches_the_ledgers_own_formula(self):
        st, q = _st(), _quotes()
        lots = {l["cycle"]: dict(l) for l in st["lots"]}
        chains = {tk: {"underlying_price": x["underlying_price"],
                       "contracts": [{"symbol": s, **c} for s, c in x["quotes"].items()]} for tk, x in q.items()}
        ref = W._nav_estimate(st["meta"]["cash"], lots, chains, lambda tk: None)
        assert WH.live_view(st, q)["nav"] == pytest.approx(ref["nav"], abs=0.01)

    def test_live_symbols(self):
        assert WH.live_symbols(_st()) == {"AAA": [PUT], "BBB": [CALL]}
        assert WH.live_symbols({"available": False}) == {}


class TestHonestStates:
    def test_ticker_quote_missing(self):
        q = _quotes()
        q["AAA"] = _q(available=False, reason="stale_vintage")
        v = WH.live_view(_st(), q)
        aaa = v["rows"][0]
        assert aaa["quote_reason"] == "stale_vintage" and aaa["put"]["reason"] == "no_quote"
        assert aaa["put"]["mid"] is None and aaa["unrealized_usd"] is None
        assert aaa["put"]["close_unrealized_usd"] == pytest.approx(50.0), "收盘口径照给，只是不冒充此刻"
        assert v["nav"] is None and v["since_close_usd"] is None and "AAA put" in v["missing"]

    def test_contract_not_in_payload(self):
        q = _quotes()
        q["AAA"]["quotes"] = {PUT: None}
        assert WH.live_view(_st(), q)["rows"][0]["put"]["reason"] == "contract_missing"

    @pytest.mark.parametrize("bid,ask", [(0.0, 0.5), (1.2, 1.0), (None, 1.0)])
    def test_no_two_sided_quote_uses_ledger_rule(self, bid, ask):
        v = WH.live_view(_st(), _quotes(put=(bid, ask)))
        assert v["rows"][0]["put"]["reason"] == "no_mid" and v["nav"] is None

    def test_shares_without_price(self):
        v = WH.live_view(_st(), _quotes(bbb_px=None))
        assert v["rows"][1]["shares"]["reason"] == "no_price" and v["nav"] is None

    def test_expired_unsettled_leg_is_named_not_contract_missing(self):
        """过了到期日、还没被扫描结算的腿（周末 / 扫描失败会挂几天）：合约已不在文件里，要说「等结算」，不说「文件里没有」。"""
        st = _st()
        st["lots"][0]["put"]["expiry"] = "2026-12-04"
        q = _quotes(); q["AAA"]["quotes"] = {}
        v = WH.live_view(st, q, today="2026-12-07")
        assert v["rows"][0]["put"]["reason"] == "expired_unsettled" and v["nav"] is None
        assert WH.live_view(st, q, today="2026-12-04")["rows"][0]["put"]["reason"] == "contract_missing", "到期日当天还不算过期"

    def test_every_reason_is_labelled(self):
        v1 = WH.live_view(_st(), {})
        v2 = WH.live_view(_st(), _quotes(put=(0.0, 1.0), bbb_px=None))
        q = _quotes(); q["AAA"]["quotes"] = {}
        v3 = WH.live_view(_st(), q)
        st4 = _st(); st4["lots"][0]["put"]["expiry"] = "2026-12-01"
        v4 = WH.live_view(st4, _quotes(), today="2026-12-03")
        seen = set()
        for v in (v1, v2, v3, v4):
            for r in v["rows"]:
                for k in ("put", "call", "shares"):
                    if r[k] and r[k]["reason"]:
                        seen.add(r[k]["reason"])
        assert seen == set(WH.LIVE_REASONS)

    def test_unavailable_ledger(self):
        assert WH.live_view({"available": False, "state_reason": "no_state_dir"}, {})["available"] is False


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Fn:
    def __init__(self, q=None):
        self.calls, self.q = [], q or _quotes()

    def __call__(self, t, syms):
        self.calls.append((t, list(syms)))
        return self.q[t]


def _svc(monkeypatch, fn=None, clock=None, **kw):
    monkeypatch.setattr(W, "load_state", lambda: _st())
    return AlphaBotService(bars_fn=lambda t: {"available": False, "bars": []}, clock=clock or _Clock(), wheel_quote_fn=fn, **kw)


class TestService:
    def test_one_quote_per_ticker_and_numbers(self, monkeypatch):
        fn = _Fn()
        d = _svc(monkeypatch, fn).wheel_live()
        assert sorted(fn.calls) == [("AAA", [PUT]), ("BBB", [CALL])]
        assert d["available"] and d["nav"] == pytest.approx(100000.0 + 5400.0 - 110.0 - 260.0)
        assert d["refresh_sec"] == AlphaBotService.WHEEL_LIVE_REFRESH_SEC and set(d["fetched"]) == {"AAA", "BBB"}

    def test_cache_and_force_min_interval(self, monkeypatch):
        fn, clk = _Fn(), _Clock()
        s = _svc(monkeypatch, fn, clk)
        s.wheel_live(); s.wheel_live()
        assert len(fn.calls) == 2, "TTL 内不重取"
        clk.t += AlphaBotService.WHEEL_LIVE_MIN_SEC - 1
        s.wheel_live(force=True)
        assert len(fn.calls) == 2, "立即刷新也有最小间隔"
        clk.t += 2
        s.wheel_live(force=True)
        assert len(fn.calls) == 4

    def test_failure_is_cached_and_reported(self, monkeypatch):
        q = _quotes()
        q["AAA"] = _q(available=False, reason="stale_vintage")
        fn = _Fn(q)
        s = _svc(monkeypatch, fn)
        d = s.wheel_live(); s.wheel_live()
        assert sum(1 for c in fn.calls if c[0] == "AAA") == 1, "坏票不被每次重下"
        assert d["quotes"]["AAA"]["reason"] == "stale_vintage" and d["nav"] is None
        assert s.stats["wheel_live_failures"] == 1

    def test_raising_fetcher_is_reported_not_crashing(self, monkeypatch):
        def boom(t, syms):
            raise RuntimeError("x")
        d = _svc(monkeypatch, boom).wheel_live()
        assert d["available"] and d["nav"] is None
        assert d["quotes"]["AAA"]["reason"] == "exception:RuntimeError"

    def test_no_fetcher_when_bars_injected(self, monkeypatch):
        d = _svc(monkeypatch, None).wheel_live()
        assert d["quotes"]["AAA"]["reason"] == "no_fetcher" and d["nav"] is None

    def test_demo_never_reads_the_ledger(self, monkeypatch):
        monkeypatch.setattr(W, "load_state", lambda: pytest.fail("演示模式读了账本"))
        d = AlphaBotService(demo=True).wheel_live()
        assert d["available"] is False and d["state_reason"] == "demo"


class TestWiring:
    def test_route_and_page(self):
        page = (STATIC / "pages" / "wheel.js").read_text(encoding="utf-8")
        assert 'from "./wheel_live.js"' in page and "renderWheelLive(liveHost, alive)" in page
        live = (STATIC / "pages" / "wheel_live.js").read_text(encoding="utf-8")
        assert "/api/wheel/live" in live and "refresh_sec" in live and "style=" not in live
        src = (Path(__file__).resolve().parent.parent / "alphabot" / "server.py").read_text(encoding="utf-8")
        assert 'Route("/api/wheel/live", wheel_live)' in src
