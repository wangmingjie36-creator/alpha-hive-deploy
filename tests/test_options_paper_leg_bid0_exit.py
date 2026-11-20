"""v0.45.787 期权纸面腿：平仓只问「成交得了吗」，不借盯市的 `quote_ok`。

NVDA 2026-11-18 / 19：short 的看涨腿深度虚值、bid=0，`_quote_ok`（要 mid>0、bid>0）把整个平仓挡死，等到期。
账本的成交约定本来就是 short 按 ask 买回——平仓不需要 mid。放宽的只有「一条腿恰好 bid=0、ask>0」的 short；
没有 ask 仍然等（不编价），long 不动，`_quote_ok` 不动。

夹具与桩复用 `tests/test_options_paper_leg.py`（`state` 是它的 autouse 夹具：状态目录重定向 + 钉预算）。
"""

import logging

import pytest

import options_paper_leg as opl
from tests.test_options_paper_leg import (  # noqa: F401  (state = autouse 夹具，必须在本模块命名空间里)
    AS_OF, CALL, PUT, _none_quotes, _q, _quotes_fn, _sig, _state_files_finite, state,
)


def _q0(ask, bid=0.0, ok=False):
    """bid=0 的腿：CBOE 照给 ask，quote_ok 为假、mid 为 None（`_qs_contract` 的真实形状）。"""
    return {"bid": bid, "ask": ask, "mid": None, "quote_ok": ok, "role": "held"}


GOOD_PUT = _q(2.9, 3.1)


def _open_short():
    r = opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="rich")])    # short 2× @1.8
    assert r["positions"][0]["side"] == "short"


def _run(day, table):
    return opl.run_for_date(day, quotes_fn=_quotes_fn(table), signals=[])


class TestShortWithBidZeroLeg:
    def test_closes_same_day_at_ask(self):
        _open_short()
        r = _run("2026-09-21", {CALL: _q0(0.05), PUT: GOOD_PUT})
        assert r["positions"] == [] and r["exit_blocked"] == []
        (t,) = r["closed_today"]
        assert t["exit_reason"] == "post_event" and t["mark_source"] == "cboe_mid"
        assert t["exit_call"] == 0.05 and t["exit_put"] == 3.1 and t["exit_premium"] == pytest.approx(3.15)   # 都按 ask
        assert t["pnl_usd"] == pytest.approx((1.8 - 3.15) * 100 * 2)
        assert t["exit_bid0_legs"] == ["call"] and "bid=0" in t["rationale"]
        assert r["cash"] == pytest.approx(20_000 + 360 - 630)
        _state_files_finite()

    def test_both_legs_bid0_close_too(self):
        _open_short()
        (t,) = _run("2026-09-21", {CALL: _q0(0.05), PUT: _q0(0.02)})["closed_today"]
        assert t["exit_bid0_legs"] == ["call", "put"] and t["exit_premium"] == pytest.approx(0.07)

    def test_expiry_buffer_exit_uses_the_same_path(self, monkeypatch):
        monkeypatch.setitem(opl.CONFIG, "exit_after_event", False)
        _open_short()
        (t,) = _run("2026-09-30", {CALL: _q0(0.05), PUT: _q(0.9, 1.1)})["closed_today"]
        assert t["exit_reason"] == "expiry_buffer" and t["exit_bid0_legs"] == ["call"]

    def test_ordinary_close_is_untouched_and_carries_no_new_key(self):
        """两腿都有正常双边报价 ⇒ 平仓记录与此前逐字段相同：没有 exit_bid0_legs 键、rationale 没多出字。"""
        _open_short()
        (t,) = _run("2026-09-21", {CALL: _q(0.9, 1.1), PUT: GOOD_PUT})["closed_today"]
        assert "exit_bid0_legs" not in t and "bid=0" not in t["rationale"]
        assert t["exit_premium"] == pytest.approx(1.1 + 3.1)
        assert "exit_bid0_legs" not in opl.CLOSED_FILE.read_text(encoding="utf-8")
        assert "exit_blocked" not in opl.POSITIONS_FILE.read_text(encoding="utf-8")


class TestNoExecutablePriceStillWaits:
    @pytest.mark.parametrize("call_q", [
        _q0(None), _q0(0.0), _q0(-0.01),                               # 没有卖价 / 卖价 0 / 负数：没有对手方，不编价
        {"bid": None, "ask": 0.05, "mid": None, "quote_ok": False},    # 买价缺失 ≠ 买价为 0（坏数据）
        {"bid": False, "ask": 0.05, "mid": None, "quote_ok": False},   # 布尔不是 0
        {"bid": -0.01, "ask": 0.05, "mid": None, "quote_ok": False},   # 负买价是坏数据
        {"bid": 0.2, "ask": 0.1, "mid": None, "quote_ok": False},      # 倒挂（ask<bid）：不是「只缺买价」
        None,                                                          # 整条腿没有报价
    ])
    def test_waits_and_says_so(self, call_q):
        _open_short()
        r = _run("2026-09-21", {CALL: call_q, PUT: GOOD_PUT})
        assert r["closed_today"] == [] and len(r["positions"]) == 1
        (b,) = r["exit_blocked"]
        assert b["ticker"] == "XYZ" and b["since"] == "2026-09-21" and b["exit_reason"] == "post_event"
        assert "call" in b["reason"] and "put" not in b["reason"]
        p = r["positions"][0]
        assert p["exit_blocked_since"] == "2026-09-21" and p["exit_blocked_reason"] == b["reason"]
        _state_files_finite()

    def test_blocked_is_visible_in_report_and_escalates_after_a_day(self, caplog):
        _open_short()
        with caplog.at_level(logging.INFO, logger=opl._log.name):
            _run("2026-09-21", {CALL: _q0(None), PUT: GOOD_PUT})
        first = [x for x in caplog.records if "没有可成交的价" in x.getMessage()]
        assert first and first[0].levelno == logging.INFO           # 第一天：INFO
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=opl._log.name):
            r = _run("2026-09-22", {CALL: _q0(None), PUT: GOOD_PUT})
        second = [x for x in caplog.records if "没有可成交的价" in x.getMessage()]
        assert second and second[0].levelno == logging.WARNING      # 连续第二天：WARNING
        assert r["exit_blocked"][0]["since"] == "2026-09-21", "since 是这一段连续平不了的第一天"
        assert "该平仓却没有可成交的价" in opl.render_markdown("2026-09-22")

    def test_blocked_clears_once_it_can_close(self):
        _open_short()
        _run("2026-09-21", {CALL: _q0(None), PUT: GOOD_PUT})
        r = _run("2026-09-22", {CALL: _q0(0.05), PUT: GOOD_PUT})
        assert r["exit_blocked"] == [] and len(r["closed_today"]) == 1
        assert "exit_blocked" not in opl.POSITIONS_FILE.read_text(encoding="utf-8")

    def test_same_day_rerun_keeps_since(self):
        _open_short()
        _run("2026-09-21", {CALL: _q0(None), PUT: GOOD_PUT})
        r = _run("2026-09-21", {CALL: _q0(None), PUT: GOOD_PUT})
        assert r["exit_blocked"][0]["since"] == "2026-09-21"


class TestOnlyBeforeExpiry:
    """v0.45.789：放宽只补「未到期取不到价就等」的缺口。到期当天设计本来就有答案——到期那一场的收盘算内在价值（真实结算价），
    0.45.787 漏限定：到期当天 bid=0 腿也按 ask 平（探针 8.15 vs 内在价值 8.0），当晚无定稿收盘时还把「延期一天」变成当晚就平。
    夹具：short K=100、到期 2026-10-02；看涨腿 bid=0 / ask=0.05，看跌腿 7.9 / 8.1；到期日收盘 92 ⇒ 内在价值 |92−100| = 8.0。"""

    EXPIRY_DAY, DAY_BEFORE = "2026-10-02", "2026-10-01"
    LEGS = {CALL: _q0(0.05), PUT: _q(7.9, 8.1)}

    @staticmethod
    def _final(tk, d):
        return {"price": 92.0, "session": d, "source": "t", "reason": None}

    @staticmethod
    def _no_final(tk, d):
        return {"price": None, "session": None, "source": None, "reason": "no_final_bar"}

    def _go(self, day, closes_fn, legs=None):
        _open_short()
        return opl.run_for_date(day, quotes_fn=_quotes_fn(legs or self.LEGS), signals=[], closes_fn=closes_fn)

    def test_day_before_expiry_still_closes_at_ask(self):
        r = self._go(self.DAY_BEFORE, self._final)
        (t,) = r["closed_today"]
        assert t["mark_source"] == "cboe_mid" and t["exit_premium"] == pytest.approx(0.05 + 8.1)
        assert t["exit_bid0_legs"] == ["call"]

    def test_expiry_day_goes_to_intrinsic_not_ask(self):
        r = self._go(self.EXPIRY_DAY, self._final)
        (t,) = r["closed_today"]
        assert t["mark_source"] == "intrinsic" and t["exit_premium"] == pytest.approx(8.0)
        assert "exit_bid0_legs" not in t and t["exit_underlying"] == 92.0

    def test_expiry_evening_without_final_close_defers_like_before(self):
        """到期当晚本源无定稿收盘（设计内）：延期一天，不是当晚就按 ask 平。"""
        r = self._go(self.EXPIRY_DAY, self._no_final)
        assert r["closed_today"] == [] and len(r["positions"]) == 1
        assert len(r["settle_deferred"]) == 1 and r["exit_blocked"] == []

    def test_expiry_day_with_two_good_legs_is_unchanged(self):
        """存量行为：到期当天两腿都有正常双边报价照旧按报价平（本版不统一到期当天口径）。"""
        r = self._go(self.EXPIRY_DAY, self._final, legs={CALL: _q(0.04, 0.06), PUT: _q(7.9, 8.1)})
        (t,) = r["closed_today"]
        assert t["mark_source"] == "cboe_mid" and t["exit_premium"] == pytest.approx(0.06 + 8.1)
        assert "exit_bid0_legs" not in t

    def test_allow_bid0_false_falls_back_to_quote_ok(self):
        assert opl._exit_executable("short", _q0(0.05), GOOD_PUT, allow_bid0=False) == (False, [])
        assert opl._exit_executable("short", _q0(0.05), GOOD_PUT, allow_bid0=True) == (True, ["call"])
        assert opl._exit_executable("short", _q(0.9, 1.1), GOOD_PUT, allow_bid0=False) == (True, [])


class TestScopeIsNarrow:
    def test_not_yet_due_is_neither_closed_nor_flagged(self):
        """事件前 bid=0（平仓条件没触发）：不平、不记 blocked——盯市的事，行为与此前相同。"""
        _open_short()
        r = _run("2026-09-10", {CALL: _q0(0.05), PUT: GOOD_PUT})
        assert r["closed_today"] == [] and r["exit_blocked"] == [] and len(r["positions"]) == 1
        assert r["positions"][0]["mark_source"] == "stale", "bid=0 的腿仍不是合格盯市报价（_quote_ok 不动）"

    def test_long_with_bid0_leg_is_unchanged(self):
        """long 的 bid=0 腿成交价是 0，语义另议（现行规则也不开 long）：仍要求 quote_ok，照旧等。"""
        opl.run_for_date(AS_OF, quotes_fn=_none_quotes, signals=[_sig(label="cheap")])
        r = _run("2026-09-21", {CALL: _q0(0.05), PUT: GOOD_PUT})
        assert r["closed_today"] == [] and len(r["positions"]) == 1 and len(r["exit_blocked"]) == 1

    def test_quote_ok_itself_is_not_relaxed(self):
        """盯市 / 入场口径建在 `_quote_ok` 上：bid=0 的腿仍然不合格。放宽只发生在 `_exit_executable`。"""
        assert opl._quote_ok(_q0(0.05)) is False
        assert opl._exit_executable("short", _q0(0.05), GOOD_PUT) == (True, ["call"])
        assert opl._exit_executable("long", _q0(0.05), GOOD_PUT) == (False, [])
        assert opl._exit_executable("short", _q0(None), GOOD_PUT) == (False, [])
        assert opl._exit_executable("short", _q(0.9, 1.1), GOOD_PUT) == (True, [])
