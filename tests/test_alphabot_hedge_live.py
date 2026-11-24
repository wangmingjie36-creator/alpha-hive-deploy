"""Alpha Bot 对冲页「此刻浮动」（v0.45.800）的守卫。每组回答一句「谁会红」：

  · 口径：此刻浮动 =（现价 − 均价）× 股数、净值 = 现金 + 股数 × 现价、较收盘变动 =（现价 − 收盘标记）× 股数；
    已实现与收盘口径相同（报价只动浮动，不动已实现）（`TestLiveMarks`）；
  · 算不出来就说算不出来：没报价 / 盘中拿到昨天的报价 / 离谱报价 ⇒ 该行 None + 原因，**不拿收盘标记价顶上**；
    逐票对冲账有一只缺 ⇒ 合计 None，不把缺的当 0（`TestHonestStates`）；
  · 服务端：缓存（同一份报价不连撞）、强制刷新有最小间隔、失败冷却期内不再取、失败后沿用上一份并写明；
    只读——打接口前后账本目录指纹不变（`TestService`）；
  · 接线：路由、页面挂载、页面不写死刷新间隔（`TestWiring`）。
全部离线：报价函数一律注入。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from alphabot import hedge as HG
from alphabot.service import ET, AlphaBotService
from tests.test_alphabot_hedge import POSITIONS, META, STATIC, _client, _fingerprint, _write
from alphabot import synthetic as syn

LIVE_NOW = datetime(2026, 10, 12, 11, 0, tzinfo=ET)        # 周一盘中（账本最后一天是 10-09 周五）
SESSION_LIVE = {"live": True, "date_et": "2026-10-12", "trading_day": True}
SESSION_IDLE = {"live": False, "date_et": "2026-10-11", "trading_day": False}
TS_TODAY = LIVE_NOW.timestamp()
TS_FRIDAY = datetime(2026, 10, 9, 15, 59, tzinfo=ET).timestamp()


def _view(positions=None, meta=None, **extra):
    return {"exists": True, "positions": POSITIONS if positions is None else positions,
            "meta": META if meta is None else meta, "history": {"equity": []}, "errors": {}, **extra}


def _q(price, ts=TS_TODAY, prev=778.57):
    return {"SPY": {"price": price, "prev_close": prev, "time": ts, "state": "REGULAR", "delay_min": 0}}


class TestLiveMarks:
    def test_unrealized_nav_and_since_mark(self):
        r = HG.live_marks(_view(), _q(780.0), session=SESSION_LIVE, tz=ET)
        S = r["spy"]
        assert S["unrealized_usd"] == pytest.approx((780.0 - 763.7475) * 44, abs=0.01)
        assert S["nav"] == pytest.approx(-33515.8498 + 44 * 780.0, abs=0.01)
        assert S["since_mark_usd"] == pytest.approx((780.0 - 778.57) * 44, abs=0.01)
        assert S["row"]["day_change_usd"] == pytest.approx((780.0 - 778.57) * 44, abs=0.01)

    def test_realized_does_not_move_with_the_quote(self):
        a = HG.live_marks(_view(), _q(770.0), session=SESSION_LIVE, tz=ET)["spy"]
        b = HG.live_marks(_view(), _q(790.0), session=SESSION_LIVE, tz=ET)["spy"]
        assert a["realized_usd"] == pytest.approx(b["realized_usd"], abs=0.02)
        assert a["realized_usd"] == pytest.approx(a["close_nav"] - a["close_unrealized_usd"], abs=0.02)

    def test_quote_at_close_mark_reproduces_close_numbers(self):
        S = HG.live_marks(_view(), _q(778.57), session=SESSION_LIVE, tz=ET)["spy"]
        assert S["nav"] == pytest.approx(S["close_nav"], abs=0.02)
        assert S["unrealized_usd"] == pytest.approx(S["close_unrealized_usd"], abs=0.02)
        assert S["since_mark_usd"] == 0

    def test_short_position_sign(self):
        pos = [{**POSITIONS[0], "shares": -10.0}]
        S = HG.live_marks(_view(positions=pos), _q(780.0), session=SESSION_LIVE, tz=ET)["spy"]
        assert S["unrealized_usd"] == pytest.approx((780.0 - 763.7475) * -10, abs=0.01) and S["unrealized_usd"] < 0

    def test_delta_book_rows_and_totals(self):
        dpos = [{"ticker": "NVDA", "shares": -20, "avg_price": 180.0, "last_price": 182.0, "last_mark_date": "2026-10-09"},
                {"ticker": "BRK.B", "shares": 5, "avg_price": 500.0, "last_price": 505.0, "last_mark_date": "2026-10-09"}]
        q = {**_q(780.0), "NVDA": {"price": 185.0, "time": TS_TODAY}, "BRK.B": {"price": 503.0, "time": TS_TODAY}}
        v = _view(delta_positions=dpos, delta_meta={"cash": 1000.0}, delta_opened=True)
        assert HG.live_symbols(v) == ["BRK.B", "NVDA", "SPY"]
        D = HG.live_marks(v, q, session=SESSION_LIVE, tz=ET)["delta"]
        assert D["unrealized_usd"] == pytest.approx(-20 * 5.0 + 5 * 3.0)
        assert D["since_mark_usd"] == pytest.approx(-20 * 3.0 + 5 * -2.0)
        assert D["nav"] == pytest.approx(1000.0 + -20 * 185.0 + 5 * 503.0)


class TestHonestStates:
    def test_no_quote_is_none_not_close_mark(self):
        S = HG.live_marks(_view(), None, session=SESSION_LIVE, tz=ET)["spy"]
        assert S["row"]["reason"] == "no_quote"
        assert S["unrealized_usd"] is None and S["nav"] is None and S["since_mark_usd"] is None
        assert S["close_nav"] is not None, "收盘口径照给，只是不冒充此刻"

    def test_yesterdays_quote_during_session_is_stale(self):
        S = HG.live_marks(_view(), _q(780.0, ts=TS_FRIDAY), session=SESSION_LIVE, tz=ET)["spy"]
        assert S["row"]["reason"] == "stale_quote" and S["nav"] is None

    def test_off_session_last_close_is_used(self):
        S = HG.live_marks(_view(), _q(780.0, ts=TS_FRIDAY), session=SESSION_IDLE, tz=ET)["spy"]
        assert S["row"]["reason"] is None and S["nav"] is not None

    def test_implausible_quote_rejected(self):
        S = HG.live_marks(_view(), _q(778.57 * 1.4), session=SESSION_LIVE, tz=ET)["spy"]
        assert S["row"]["reason"] == "implausible" and S["unrealized_usd"] is None

    @pytest.mark.parametrize("px", [0, -1, None])
    def test_bad_price(self, px):
        S = HG.live_marks(_view(), _q(px), session=SESSION_LIVE, tz=ET)["spy"]
        assert S["row"]["reason"] == "bad_price" and S["nav"] is None

    def test_flat_book_is_zero_not_none(self):
        S = HG.live_marks(_view(positions=[]), None, session=SESSION_LIVE, tz=ET)["spy"]
        assert S["flat"] is True and S["unrealized_usd"] == 0.0 and S["row"] is None

    def test_one_missing_delta_quote_nulls_the_total(self):
        dpos = [{"ticker": "NVDA", "shares": -20, "avg_price": 180.0, "last_price": 182.0},
                {"ticker": "AMD", "shares": 5, "avg_price": 100.0, "last_price": 101.0}]
        q = {**_q(780.0), "NVDA": {"price": 185.0, "time": TS_TODAY}}
        D = HG.live_marks(_view(delta_positions=dpos, delta_opened=True, delta_meta={"cash": 0}), q,
                          session=SESSION_LIVE, tz=ET)["delta"]
        assert [r["reason"] for r in D["rows"]] == [None, "no_quote"]
        assert D["unrealized_usd"] is None and D["nav"] is None

    def test_every_reason_is_labelled(self):
        """live_row 写出的每个原因码都在 LIVE_REASONS 里（页面靠它翻成人话，漏登记就显示原码）。"""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(HG.live_row))
        used = {
            v.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
            for k, v in zip(n.keys, n.values)
            if isinstance(k, ast.Constant) and k.value == "reason" and isinstance(v, ast.Constant) and v.value}
        assert used == set(HG.LIVE_REASONS), used


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Quotes:
    def __init__(self, price=780.0, fail=None):
        self.calls, self.price, self.fail = [], price, fail

    def __call__(self, syms):
        self.calls.append(list(syms))
        if self.fail:
            return None, dict(self.fail)
        return {s: {"price": self.price, "prev_close": 778.57, "time": TS_TODAY, "state": "REGULAR", "delay_min": 0}
                for s in syms}, {"reason": None}


@pytest.fixture
def root(tmp_path):
    return _write(tmp_path / "data")


def _svc(root, quotes, clock=None):
    return AlphaBotService(fetch_fn=syn.demo_fetch, bars_fn=lambda t: {"available": False, "bars": []},
                           straddle_root=root, now_fn=lambda: LIVE_NOW, clock=clock or _Clock(), hedge_quotes_fn=quotes)


class TestService:
    def test_live_numbers_and_readonly(self, root):
        before = _fingerprint(root)
        q = _Quotes()
        d = _client(_svc(root, q)).get("/api/hedge/live").json()
        assert _fingerprint(root) == before
        assert q.calls == [["SPY"]]
        assert d["available"] is True and d["session"]["live"] is True
        assert d["spy"]["unrealized_usd"] == pytest.approx((780.0 - 763.7475) * 44, abs=0.01)
        assert d["refresh_sec"] == AlphaBotService.HEDGE_LIVE_REFRESH_SEC
        assert d["quotes"]["source"] == "yahoo_quote" and d["quotes"]["refresh_failed"] is None

    def test_cache_and_force_min_interval(self, root):
        q, clk = _Quotes(), _Clock()
        s = _svc(root, q, clk)
        s.hedge_live(); s.hedge_live()
        assert len(q.calls) == 1, "TTL 内不重取"
        clk.t += AlphaBotService.HEDGE_LIVE_MIN_SEC - 1
        s.hedge_live(force=True)
        assert len(q.calls) == 1, "强制刷新也有最小间隔"
        clk.t += 2
        s.hedge_live(force=True)
        assert len(q.calls) == 2
        clk.t += AlphaBotService.HEDGE_LIVE_TTL_SEC + 1
        s.hedge_live()
        assert len(q.calls) == 3

    @pytest.mark.parametrize("tk", ["BRK.B", "BRK-B"])
    def test_quote_keys_map_back_to_ledger_ticker(self, root, tk):
        """报价按 Yahoo 写法（BRK-B）回来，要按请求时的映射认回账本代码——账本写 BRK.B 或 BRK-B 都得对上。"""
        s = _svc(root, _Quotes(price=505.0))
        dpos = [{"ticker": tk, "shares": 5, "avg_price": 500.0, "last_price": 505.0, "last_mark_date": "2026-10-09"}]
        s._hedge_view = lambda now: _view(delta_positions=dpos, delta_opened=True, delta_meta={"cash": 0.0})
        row = s.hedge_live()["delta"]["rows"][0]
        assert row["reason"] is None and row["unrealized_usd"] == pytest.approx(25.0)

    def test_failure_keeps_last_and_says_so_then_cools_down(self, root):
        q, clk = _Quotes(), _Clock()
        s = _svc(root, q, clk)
        ok = s.hedge_live()
        q.fail = {"reason": "rate_limited", "retry_after_sec": 120}
        clk.t += AlphaBotService.HEDGE_LIVE_TTL_SEC + 1
        d = s.hedge_live()
        assert d["quotes"]["refresh_failed"]["reason"] == "rate_limited"
        assert d["quotes"]["age_sec"] >= AlphaBotService.HEDGE_LIVE_TTL_SEC
        assert d["spy"]["nav"] == ok["spy"]["nav"], "沿用上一份"
        n = len(q.calls)
        clk.t += 60
        s.hedge_live(force=True)
        assert len(q.calls) == n, "冷却期内不再撞"

    def test_failure_with_nothing_cached_is_no_quote(self, root):
        d = _svc(root, _Quotes(fail={"reason": "fetch_error"})).hedge_live()
        assert d["spy"]["row"]["reason"] == "no_quote" and d["spy"]["nav"] is None
        assert d["quotes"]["refresh_failed"]["reason"] == "fetch_error"

    def test_missing_ledger(self, tmp_path):
        d = _svc(tmp_path / "nope", _Quotes()).hedge_live()
        assert d["available"] is False and d["reason"] == "no_ledger"

    def test_no_default_fetcher_when_bars_injected(self, root):
        s = AlphaBotService(fetch_fn=syn.demo_fetch, bars_fn=lambda t: {"available": False, "bars": []},
                            straddle_root=root, now_fn=lambda: LIVE_NOW)
        d = s.hedge_live()
        assert d["quotes"]["refresh_failed"]["reason"] == "no_fetcher", "注入了日线而没给报价函数 ⇒ 不出网"

    def test_demo_is_synthetic_and_consistent(self):
        s = AlphaBotService(demo=True, now_fn=lambda: LIVE_NOW)
        d = s.hedge_live()
        assert d["available"] and d["quotes"]["source"] == "demo"
        S = d["spy"]
        if S["row"]:
            assert S["row"]["reason"] is None
            assert S["nav"] == pytest.approx(S["cash"] + S["row"]["market_value"], abs=0.02)


class TestWiring:
    def test_route_and_page(self):
        page = (STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")
        assert 'from "./hedge_live.js"' in page and "renderHedgeLive(liveHost, alive)" in page
        live = (STATIC / "pages" / "hedge_live.js").read_text(encoding="utf-8")
        assert "/api/hedge/live" in live and "refresh_sec" in live
        assert "style=" not in live, "本站 CSP 不许行内 style="

    def test_server_route(self):
        src = (Path(__file__).resolve().parent.parent / "alphabot" / "server.py").read_text(encoding="utf-8")
        assert 'Route("/api/hedge/live", hedge_live)' in src
