"""v0.45.629 标的同日收盘价取不到的兜底：Finnhub 第三来源 + 当日晚些时候补跑。

事故（2026-10-22 14:38 PDT = 17:38 ET，取证：日志 + hedge_state/equity_curve.jsonl + 审计文件）
------------------------------------------------------------------------------------
组合 Greeks 取 SPY 收盘：CBOE 拉取 `EOF occurred in violation of protocol`（三次重试几秒内耗尽）；Twelve Data 当晚的
当日那根按设计被丢（临时值）⇒ 只剩 10-21 的价 ⇒ SPY 判陈旧 ⇒ NAV 缺、对冲决定整天做不出来，直到人手动补跑。
补跑那一刻 CBOE 已恢复（SPY 763.96），Finnhub `/quote` 当时给的也是 763.96、`t` = 16:00:00 ET，10 只票与 CBOE 收盘逐分相等。

写法约束
--------
- 离线：Finnhub 在 `fred_macro._finnhub_quote` 这一层打桩（认证逻辑用真的）；CBOE / Twelve Data 复用
  `test_portfolio_greeks_price_session.py` 的 `world` 夹具（twelve_data 那道「丢当日那根」的闸也是真的）。
- 每条 Finnhub 认证条件各配一条「条件不满足 ⇒ 不认」的反例，不只测能通的那条。
"""
from datetime import datetime, timedelta

import pytest

import portfolio_greeks as pg
from tests.test_portfolio_greeks_price_session import AFTER_CLOSE, AS_OF, CLOSE, ET, PREV, _beta1
from tests.test_portfolio_greeks_price_session import world as _price_world  # noqa: F401 - 夹具按别名注册

CLOSE_ET = datetime(2026, 10, 6, 16, 0, tzinfo=ET)


@pytest.fixture
def world(request):
    """复用 `test_portfolio_greeks_price_session.py` 的世界（钟 / CBOE payload / Twelve Data / Finnhub 桩 / 账本沙箱）。"""
    return request.getfixturevalue("_price_world")


def _ts(dt):
    return int(dt.timestamp())


def _quote(last, at=CLOSE_ET):
    return {"last": last, "prev": last, "change_pct": 0.0, "ts": _ts(at)}


# ── _finnhub_close_mark：自证条件逐条 ────────────────────────────────────────

class TestFinnhubCloseCertification:

    def test_stamp_at_the_close_is_certified(self):
        m = pg._finnhub_close_mark("SPY", AS_OF, quote_fn=lambda s: _quote(779.09), now_et=AFTER_CLOSE)
        assert m == {"price": 779.09, "source": "finnhub_close", "session": AS_OF, "at_close": True,
                     "live": False, "last_trade_time": f"{AS_OF}T16:00:00"}
        assert pg._resolve_mark(m, AS_OF)["price"] == 779.09           # 下游判它「可用」

    @pytest.mark.parametrize("offset_s", [-60, 0, 120])
    def test_window_edges_are_inclusive(self, offset_s):
        at = CLOSE_ET + timedelta(seconds=offset_s)
        assert pg._finnhub_close_mark("SPY", AS_OF, quote_fn=lambda s: _quote(1.0, at), now_et=AFTER_CLOSE)

    @pytest.mark.parametrize("offset_s", [-61, 121, 1800])
    def test_stamp_outside_the_window_is_not_certified(self, offset_s):
        """收盘后的盘后成交会把 `t` 推到收盘之后——那时 `c` 可能是盘后价，不能当官方收盘。"""
        at = CLOSE_ET + timedelta(seconds=offset_s)
        assert pg._finnhub_close_mark("SPY", AS_OF, quote_fn=lambda s: _quote(1.0, at), now_et=AFTER_CLOSE) is None

    def test_stamp_from_another_session_is_not_certified(self):
        """昨天 16:00 的价：时刻对、日期不对。"""
        prev_close = datetime(2026, 10, 5, 16, 0, tzinfo=ET)
        assert pg._finnhub_close_mark("SPY", AS_OF, quote_fn=lambda s: _quote(1.0, prev_close),
                                      now_et=AFTER_CLOSE) is None

    def test_session_not_closed_yet_is_not_certified(self):
        """盘中：哪怕 `t` 恰好在窗口里（不可能，但钟判不了就不认）也不给收盘价。"""
        called = []
        m = pg._finnhub_close_mark("SPY", AS_OF, quote_fn=lambda s: called.append(s) or _quote(1.0),
                                   now_et=datetime(2026, 10, 6, 11, 0, tzinfo=ET))
        assert m is None and called == [], "盘中不该白白打一次 Finnhub"

    def test_early_close_day_uses_the_1pm_close(self):
        at = datetime(2026, 11, 27, 13, 0, tzinfo=ET)                  # 感恩节次日 13:00 收盘
        now = datetime(2026, 11, 27, 14, 0, tzinfo=ET)
        assert pg._finnhub_close_mark("SPY", "2026-11-27", quote_fn=lambda s: _quote(1.0, at), now_et=now)
        late = datetime(2026, 11, 27, 16, 0, tzinfo=ET)                # 16:00 的戳在半日市里是盘后
        assert pg._finnhub_close_mark("SPY", "2026-11-27", quote_fn=lambda s: _quote(1.0, late), now_et=now) is None

    @pytest.mark.parametrize("bad", [None, {}, {"last": 5.0}, {"last": 5.0, "ts": 0}, {"last": 5.0, "ts": True},
                                     {"last": 5.0, "ts": "1792699200"}, {"last": 0.0, "ts": 1}, {"last": -3, "ts": 1},
                                     {"last": float("nan"), "ts": 1}, "oops"])
    def test_malformed_quote_is_not_certified(self, bad):
        assert pg._finnhub_close_mark("SPY", AS_OF, quote_fn=lambda s: bad, now_et=AFTER_CLOSE) is None

    def test_quote_fn_raising_is_not_certified_and_is_logged(self, caplog):
        def boom(sym):
            raise OSError("proxy down")
        with caplog.at_level("WARNING"):
            assert pg._finnhub_close_mark("SPY", AS_OF, quote_fn=boom, now_et=AFTER_CLOSE) is None
        assert "Finnhub 收盘价获取失败" in caplog.text and "proxy down" in caplog.text

    def test_hyphenated_tickers_use_finnhubs_dot_spelling(self):
        seen = []
        pg._finnhub_close_mark("BRK-B", AS_OF, quote_fn=lambda s: seen.append(s) or _quote(1.0), now_et=AFTER_CLOSE)
        assert seen == ["BRK.B"]


# ── _default_mark：取价链里的位置 ────────────────────────────────────────────

def _cboe_down(world, tickers=("SPY",)):
    for tk in tickers:
        world.payloads.pop(tk, None)                      # 夹具里 `payloads` 缺 ⇒ _fetch_cboe_payload 返回 None


class TestDefaultMarkChain:

    def test_the_incident_cboe_down_finnhub_supplies_the_close(self, world, caplog):
        """10-22 的复现：CBOE 取不到、Twelve Data 的当日那根被丢（夹具里是真闸）、Finnhub 给收盘。"""
        world.closed(tickers=("AMZN", "TSLA", "JNJ"))
        _cboe_down(world)
        world.finnhub["SPY"] = _quote(CLOSE["SPY"][1])
        with caplog.at_level("WARNING"):
            m = pg._default_mark("SPY", AS_OF)
        assert (m["price"], m["source"], m["session"], m["at_close"]) == (CLOSE["SPY"][1], "finnhub_close", AS_OF, True)
        assert "改用 Finnhub" in caplog.text, "降级到第三来源必须在日志里看得见"

    def test_without_finnhub_it_falls_back_to_the_old_stale_verdict(self, world):
        """第三来源也没有 ⇒ 老行为不变：交出前一交易日的价，由 `_resolve_mark` 判陈旧。"""
        _cboe_down(world)
        m = pg._default_mark("SPY", AS_OF)
        assert m["session"] == PREV and m["source"] == "twelve_data_bar"
        assert pg._resolve_mark(m, AS_OF)["stale"] is True

    def test_uncertified_finnhub_stamp_does_not_rescue_a_stale_price(self, world):
        _cboe_down(world)
        world.finnhub["SPY"] = _quote(CLOSE["SPY"][1], CLOSE_ET + timedelta(minutes=30))     # 盘后戳
        m = pg._default_mark("SPY", AS_OF)
        assert m["session"] == PREV and pg._resolve_mark(m, AS_OF)["stale"] is True

    def test_cboe_official_close_wins_over_finnhub(self, world):
        """CBOE 给得出官方收盘就用它：Finnhub 的价故意不同，取错一眼看出来。"""
        world.closed()
        world.finnhub["SPY"] = _quote(1.23)
        assert pg._default_mark("SPY", AS_OF)["source"] == "cboe_close"

    def test_finnhub_is_not_even_called_when_cboe_has_the_close(self, world, monkeypatch):
        import fred_macro
        world.closed()
        monkeypatch.setattr(fred_macro, "_finnhub_quote", lambda s: pytest.fail("CBOE 已有收盘，不该打 Finnhub"))
        assert pg._default_mark("SPY", AS_OF)["source"] == "cboe_close"

    def test_stale_intraday_cboe_file_is_rescued_too(self, world):
        """CBOE 给的是盘中陈旧文件（判不了收盘）：以前判陈旧、当天不对冲；现在 Finnhub 的收盘顶上。"""
        world.intraday(tickers=("SPY",))
        world.finnhub["SPY"] = _quote(CLOSE["SPY"][1])
        m = pg._default_mark("SPY", AS_OF)
        assert (m["source"], m["price"]) == ("finnhub_close", CLOSE["SPY"][1])

    def test_end_to_end_incident_day_now_decides_and_trades(self, world):
        """整条链：CBOE 取不到 SPY ⇒ 以前 band unknown / 不成交；现在 SPY 价来自 Finnhub，NAV 出得来、照常成交，
        成交记录自带的场次证明过 `load_history` 放行闸。"""
        world.closed(tickers=("AMZN", "TSLA", "JNJ"))
        _cboe_down(world)
        world.finnhub["SPY"] = _quote(CLOSE["SPY"][1])
        world.book()
        r = pg.run_for_date(AS_OF, beta_fn=_beta1)
        t = r["executed_trade"]
        assert t is not None and t["action"] == "sell_spy" and t["price"] == CLOSE["SPY"][1], r["recommendation"]
        assert (t["price_source"], t["price_session"], t["price_at_close"]) == ("finnhub_close", AS_OF, True)
        eq = pg._load_jsonl(pg.EQUITY_FILE)[-1]
        assert eq["spy_price"] == CLOSE["SPY"][1] and eq["nav"] is not None
        assert pg._load_audit(AS_OF)["price_check"]["hedge_undecided"] is None

    def test_end_to_end_without_finnhub_stays_undecided(self, world):
        """对照：同一天、Finnhub 也取不到 ⇒ 仍是 10-22 的老样子（决定做不出来、不成交、不撒谎）。"""
        world.closed(tickers=("AMZN", "TSLA", "JNJ"))
        _cboe_down(world)
        world.book()
        r = pg.run_for_date(AS_OF, beta_fn=_beta1)
        # 组合本身带外（band above），但 SPY 价陈旧 ⇒ 没有 NAV / 目标 ⇒ hold + hedge_undecided（不是 unknown 带）
        assert r["executed"] is False and not pg.TRADES_FILE.exists()
        assert r["recommendation"]["action"] == "hold" and r["recommendation"]["target_usd"] is None
        assert pg._load_audit(AS_OF)["price_check"]["hedge_undecided"]


# ── retry_if_undecided ──────────────────────────────────────────────────────

class TestRetryIfUndecided:

    def _broken_day(self, world):
        world.closed(tickers=("AMZN", "TSLA", "JNJ"))
        _cboe_down(world)
        world.book()
        pg.run_for_date(AS_OF, beta_fn=_beta1)
        assert pg._undecided_reason(AS_OF), "前提：第一次跑没做出决定"

    def _retry(self, **kw):
        sleeps = []
        out = pg.retry_if_undecided(AS_OF, sleep=sleeps.append, **kw)
        return out, sleeps

    def test_not_needed_when_the_decision_was_made_and_it_touches_nothing(self, world):
        world.closed()
        world.book()
        pg.run_for_date(AS_OF, beta_fn=_beta1)
        out, sleeps = self._retry(run=lambda a: pytest.fail("决定已做出，不该再跑"))
        assert out["outcome"] == "not_needed" and out["attempts_made"] == 0 and sleeps == []

    def test_recovers_when_the_source_comes_back_between_attempts(self, world):
        """第一次补跑 CBOE 仍断、第二次恢复：隔一次再来才是「更晚的重试」的意义。"""
        self._broken_day(world)
        calls = []

        def run(as_of):
            calls.append(as_of)
            if len(calls) == 2:
                world.closed(tickers=("SPY",))               # CBOE 恢复
            return pg.run_for_date(as_of, beta_fn=_beta1)
        out, sleeps = self._retry(run=run, attempts=3, interval_s=7)
        assert out["outcome"] == "recovered" and out["attempts_made"] == 2 and out["reason_after"] is None
        assert sleeps == [7], "两次之间要等，且恢复后不再多等"
        assert pg.TRADES_FILE.exists() and pg._load_jsonl(pg.TRADES_FILE)[-1]["price"] == CLOSE["SPY"][1]

    def test_still_undecided_after_all_attempts_is_reported_not_hidden(self, world):
        self._broken_day(world)
        out, sleeps = self._retry(run=lambda a: pg.run_for_date(a, beta_fn=_beta1), attempts=2, interval_s=5)
        assert out["outcome"] == "still_undecided" and out["attempts_made"] == 2 and out["reason_after"]
        assert sleeps == [5]

    def test_every_attempt_raising_is_an_error_with_the_messages(self, world):
        self._broken_day(world)

        def boom(a):
            raise RuntimeError("ledger locked")
        out, _ = self._retry(run=boom, attempts=2)
        assert out["outcome"] == "error" and len(out["errors"]) == 2 and "ledger locked" in out["errors"][0]

    def test_a_raise_then_success_still_recovers(self, world):
        self._broken_day(world)
        world.closed(tickers=("SPY",))
        n = []

        def flaky(a):
            n.append(1)
            if len(n) == 1:
                raise OSError("blip")
            return pg.run_for_date(a, beta_fn=_beta1)
        out, _ = self._retry(run=flaky, attempts=2)
        assert out["outcome"] == "recovered" and out["errors"] and "blip" in out["errors"][0]

    def test_no_audit_means_the_first_run_never_happened_and_nothing_is_run(self, world):
        """整次扫描失败的那天没有审计文件：在没更新的账本上补一笔对冲会占掉「今天已成交」，之后完整补跑反而不再对冲。"""
        world.closed()
        world.book()
        assert pg._load_audit(AS_OF) is None
        out, sleeps = self._retry(run=lambda a: pytest.fail("没有审计文件，不该补跑"))
        assert out["outcome"] == "no_audit" and out["attempts_made"] == 0 and sleeps == []
        assert not pg.TRADES_FILE.exists()

    def test_negative_interval_does_not_crash_the_sleep(self, world):
        self._broken_day(world)
        out, sleeps = self._retry(run=lambda a: pg.run_for_date(a, beta_fn=_beta1), attempts=2, interval_s=-5)
        assert out["outcome"] == "still_undecided" and sleeps == [0.0]

    def test_session_still_open_is_not_retried(self, world):
        """as_of 那一场还没收盘：现在补跑也拿不到官方收盘，别白跑。"""
        self._broken_day(world)
        world.at(datetime(2026, 10, 6, 11, 0, tzinfo=ET))
        out, _ = self._retry(run=lambda a: pytest.fail("盘中不该补跑"))
        assert out["outcome"] == "session_open" and out["attempts_made"] == 0

    def test_missing_audit_file_counts_as_undecided(self, world):
        """覆盖层今天根本没跑出结果（进程崩 / 没写审计）也要补。"""
        assert pg._load_audit(AS_OF) is None
        reason = pg._undecided_reason(AS_OF)
        assert reason and "审计文件缺失" in reason

    def test_unreadable_or_shapeless_audit_counts_as_undecided(self, world):
        pg.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (pg.STATE_DIR / f"greeks_{AS_OF}.json").write_text("{not json", encoding="utf-8")
        assert pg._undecided_reason(AS_OF)
        (pg.STATE_DIR / f"greeks_{AS_OF}.json").write_text('{"nav": 1}', encoding="utf-8")
        assert "price_check" in pg._undecided_reason(AS_OF)

    def test_a_decision_not_to_hedge_is_not_undecided(self, world):
        """inside 带 = 决定不对冲，不是做不出来：不能因为「没成交」就每天补跑。"""
        world.closed()
        world.book(stocks=(("AMZN", 1),), straddle=False)
        r = pg.run_for_date(AS_OF, beta_fn=_beta1)
        assert r["executed"] is False and pg._undecided_reason(AS_OF) is None


class TestRetryCli:

    def test_exit_codes_and_json(self, world, monkeypatch, capsys):
        import json
        outcomes = {"not_needed": 0, "session_open": 0, "recovered": 0, "still_undecided": 1, "no_audit": 3, "error": 3}
        for outcome, rc in outcomes.items():
            monkeypatch.setattr(pg, "retry_if_undecided",
                                lambda as_of, attempts, interval_s, _o=outcome: {"as_of": as_of, "outcome": _o})
            assert pg.main(["--retry-undecided", "--date", AS_OF]) == rc, outcome
            assert json.loads(capsys.readouterr().out)["outcome"] == outcome

    def test_refuses_dry_run(self, world):
        with pytest.raises(SystemExit):
            pg.main(["--retry-undecided", "--dry-run", "--date", AS_OF])
