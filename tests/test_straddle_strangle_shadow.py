"""影子宽跨记录（v0.45.586）与预注册检验模块的守卫。每组回答一句「谁会红」：

  · 记录如实：25Δ 两腿原样抄、取不到写原因、历史涨跌点时点、入场 / 平仓按账本约定算（手算对拍）
    （`TestQuoteRecord` / `TestEntryRecord` / `TestExitRecord` / `TestConventionsMatchLedger`）；
  · **不影响账本**：有 / 无 `strangle_quote` 两次跑出的开仓、平仓、现金、净值逐项相同；记录函数抛了账本照开照平
    （`TestShadowChangesNothing`）；
  · 只记录：全仓只有登记过的文件提到 `strangle_quote` / `shadow_strangle`，且在账本里只出现在记录它的函数里（AST，含种病灶自证）
    （`TestReaders`）；
  · 失败在源头登记：记录没写成 ⇒ `stats()["errors"]` > 0 ⇒ scan_timing 计数 ⇒ alert_manager P2；不可成交 / 平仓无价是数据、不报警
    （`TestFailuresAreVisible`）；
  · 检验：单位筛选（登记前 / 多头 / 不可成交 / 平仓无价各有名）、进度只给计数、块符号翻转对拍暴力实现且「块」有牙、
    未就绪拒跑、只冻结一次（`TestUnitsAndProgress` / `TestSignFlipTest` / `TestFreeze`）；
  · 盲期：Alpha Bot 的接口里没有 `shadow_strangle` / 影子盈亏，进度里没有任何一笔的盈亏（`TestBlinding`）。
全部离线。
"""
from __future__ import annotations

import ast
import itertools
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

import alert_manager as am
import earnings_vol_signal as evs
import options_paper_leg as opl
import straddle_strangle_shadow as SS
from alphabot import strangle_compare as SC
from tests._repo_files import own_python_files
from tests.test_earnings_vol_signal import AS_OF, EARN, EXPIRY, UP, _qs, _stats, _write_snap

REPO = Path(__file__).resolve().parent.parent
EXIT_DAY = "2026-09-21"                        # 财报日（09-20）之后第一个交易日
ATM_C, ATM_P = "XYZ261002C00100000", "XYZ261002P00100000"
SH_C, SH_P = "XYZ261002C00110000", "XYZ261002P00090000"        # 夹具里的 25Δ 两腿（110 / 90，bid 1.0 / ask 1.2）
#: 该票过去 8 次财报的涨跌 + 一条「事后才发生」的（财报日 = EARN，不得进 as_of 当天的历史）
MOVES = [{"earnings_date": d, "move_pct": m} for d, m in
         (("2026-06-20", 4.0), ("2026-03-20", -13.0), ("2025-12-20", 2.5), ("2025-09-20", 3.0), ("2025-06-20", -2.0),
          ("2025-03-20", 14.0), ("2024-12-20", 1.5), ("2024-09-20", -3.5), (EARN, 50.0))]


@pytest.fixture(autouse=True)
def _early_start(monkeypatch):
    """夹具日期是 2026-09，早于协议 start_date（2026-10-21）：钉到更早，让单位能进检验；「登记前排除」有单独的测试。"""
    monkeypatch.setitem(SS.PREREG, "start_date", "2026-01-01")


def _signal(*, with_quote=True, qs=None):
    qs = qs or _qs(d25_spread=0.1667)
    stats = {**_stats(), "moves": MOVES}
    sig = evs.compute_signal("XYZ", AS_OF, qs, UP, stats, None)
    if with_quote:
        sig["strangle_quote"] = evs.strangle_quote(qs, stats, "XYZ", AS_OF)
    return sig


def _q(bid, ask):
    return {"quote_ok": True, "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4)}


def exit_quotes(table=None):
    t = {ATM_C: _q(5.9, 6.1), ATM_P: _q(5.9, 6.1), SH_C: _q(0.2, 0.3), SH_P: _q(2.8, 3.0)}
    t.update(table or {})
    return lambda tk, syms: {s: t.get(s) for s in syms}


def _paths(tmp_path, monkeypatch, name):
    d = tmp_path / name
    for attr, fname in (("STATE_DIR", ""), ("POSITIONS_FILE", "positions.jsonl"), ("CLOSED_FILE", "closed.jsonl"),
                        ("EQUITY_FILE", "equity.jsonl"), ("META_FILE", "meta.json")):
        monkeypatch.setattr(opl, attr, d / fname if fname else d)
    return d


def _run(day, signals, quotes_fn=None, closes_fn=None):
    return opl.run_for_date(day, quotes_fn=quotes_fn or (lambda t, s: {}), signals=signals,
                            closes_fn=closes_fn or (lambda t, d: None))


def _read_jsonl(p):
    return [json.loads(ln) for ln in Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]


def _closed_row(entry_date="2026-10-22", earnings_date="2026-10-28", pnl=-100.0, side="short", exit_date=None, **sh_over):
    """合成一笔带影子记录的平仓（检验层测试用）：影子盈亏 `shadow_pnl`，跨式盈亏 `pnl`。
    `exit_date` 缺省 = 财报日的次日（事后平仓）；传 `""` = 平仓记录没有平仓日。"""
    sh_pnl = sh_over.pop("shadow_pnl", -150.0)
    if exit_date is None:
        exit_date = (date.fromisoformat(earnings_date) + timedelta(days=1)).isoformat()
    sh = {"schema_version": 1, "recorded_on": entry_date, "side": side, "status": "ok", "reason": None, "premium": 2.0,
          "nav_for_sizing": 100_000.0, "contracts": 30,
          "exit": {"status": "ok", "exit_date": exit_date, "basis": "cboe_mid", "pnl_usd": sh_pnl}}
    sh.update(sh_over)
    row = {"ticker": "T", "side": side, "entry_date": entry_date, "earnings_date": earnings_date, "pnl_usd": pnl,
           "shadow_strangle": sh}
    if exit_date != "":
        row["exit_date"] = exit_date
    return row


def _closed_rows():
    """落盘的平仓记录（`run_for_date` 的返回值剥掉了 `shadow_strangle`——盲期，见 `TestRunForDateIsBlind`；检验与这里读的是文件）。"""
    return _read_jsonl(opl.CLOSED_FILE)


class TestQuoteRecord:
    def test_copies_the_25_delta_legs_and_prior_moves(self):
        r = SS.quote_record(_qs(d25_spread=0.1667), AS_OF, MOVES)
        assert r["available"] and r["schema_version"] == 1 and r["selected_expiry"] == EXPIRY
        assert (r["call"]["strike"], r["put"]["strike"]) == (110.0, 90.0)
        assert (r["call"]["bid"], r["call"]["ask"], r["put"]["bid"], r["put"]["ask"]) == (1.0, 1.2, 1.0, 1.2)
        assert r["underlying_price"] == 100.0 and r["market_open"] is False

    def test_history_is_point_in_time(self):
        """财报日 ≥ as_of 的（含「事后才发生的那次」）不进历史；新 → 旧。"""
        r = SS.quote_record(_qs(d25_spread=0.1667), AS_OF, MOVES)
        assert len(r["hist_moves"]) == 8 and all(m["earnings_date"] < AS_OF for m in r["hist_moves"])
        assert r["hist_moves"][0]["earnings_date"] == "2026-06-20" and 50.0 not in [m["move_pct"] for m in r["hist_moves"]]

    def test_same_rule_as_the_compare_card(self):
        junk = MOVES + ["x", {"earnings_date": "2026-01-01"}, {"earnings_date": "2026-01-02", "move_pct": float("nan")}]
        assert SS.prior_moves(junk, AS_OF) == SC.prior_moves(junk, AS_OF)

    @pytest.mark.parametrize("qs,reason", [
        (None, "no_quote_set"),
        ({**_qs(d25_spread=0.1667), "data_available": False}, "quote_set_unavailable"),
        (_qs(), "missing_leg:call"),                       # 夹具默认没有 25Δ 腿
    ])
    def test_unavailable_says_why_and_never_fabricates_legs(self, qs, reason):
        r = SS.quote_record(qs, AS_OF, MOVES)
        assert r["available"] is False and r["reason"] == reason
        if reason == "no_quote_set":
            assert r["call"] is None and r["put"] is None


class TestEntryRecord:
    @staticmethod
    def _entry(qrec, side="short", nav=100_000.0, size_fn=None, **kw):
        straddle = {"call": {"strike": 100.0, "bid": 4.9, "ask": 5.1, "spread_pct": 0.04},
                    "put": {"strike": 100.0, "bid": 4.9, "ask": 5.1, "spread_pct": 0.04},
                    "premium": 9.8, "contracts": 6, "underlying": 100.0}
        return SS.entry_record(qrec, side, nav, size_fn or opl.size_contracts, straddle, as_of=AS_OF, **kw)

    def test_short_sells_at_bid_and_sizes_with_the_ledgers_rule(self):
        e = self._entry(SS.quote_record(_qs(d25_spread=0.1667), AS_OF, MOVES))
        assert e["status"] == "ok" and e["premium"] == pytest.approx(2.0)                 # 1.0 + 1.0（两腿 bid）
        assert e["nav_for_sizing"] == 100_000.0 and e["contracts"] == 30                  # floor(6000 / 200)
        assert e["contracts"] == opl.size_contracts("short", 2.0, 100_000.0)[0]           # 对拍账本函数本身
        assert e["recorded_on"] == AS_OF and e["exit"] is None

    def test_features_are_point_in_time_and_match_the_compare_card(self):
        qs = _qs(d25_spread=0.1667)
        e = self._entry(SS.quote_record(qs, AS_OF, MOVES))
        f = e["features"]
        assert f["strangle"]["n_hist"] == 8 and f["straddle"]["n_hist"] == 8 and f["straddle"]["contracts"] == 6
        assert f["premium_ratio"] == pytest.approx(2.0 / 9.8)
        card = SC.compare("XYZ", AS_OF, qs=qs, raw_moves=MOVES, nav=100_000.0,
                          size_fn=lambda p, n: opl.size_contracts("short", p, n))["structures"]
        for mine, theirs in ((f["strangle"], card["strangle"]), (f["straddle"], card["straddle"])):
            assert mine["premium"] == pytest.approx(theirs["premium"])
            assert mine["band_lo_pct"] == pytest.approx(theirs["band"]["lo_pct"])
            assert mine["band_hi_pct"] == pytest.approx(theirs["band"]["hi_pct"])
            assert mine["k_outside"] == theirs["hist_outside"]["k"]
            assert mine["max_spread_frac"] == pytest.approx(theirs["max_spread_frac"])

    def test_long_buys_at_ask(self):
        e = self._entry(SS.quote_record(_qs(d25_spread=0.1667), AS_OF, MOVES), side="long")
        assert e["status"] == "ok" and e["premium"] == pytest.approx(2.4)                 # 1.2 + 1.2（两腿 ask）

    def test_unquotable_is_reported_not_substituted(self):
        qs = _qs(d25_spread=0.1667)
        qs["contracts"]["p25"] = {**qs["contracts"]["p25"], "bid": 0.0, "quote_ok": False}
        e = self._entry(SS.quote_record(qs, AS_OF, MOVES))
        assert e["status"] == "unquotable" and e["reason"] == "quote_not_ok:put" and e["premium"] is None and e["contracts"] is None

    @pytest.mark.parametrize("qrec,status", [
        (None, "no_signal_record"),                                    # 部署当天先于新代码落盘的信号
        ({"schema_version": 99}, "no_signal_record"),
        (SS.quote_record(None, AS_OF, None), "unquotable"),
        (SS.quote_record(_qs(), AS_OF, None), "unquotable"),
    ])
    def test_every_non_ok_status_has_a_name(self, qrec, status):
        e = self._entry(qrec)
        assert e["status"] == status and e["reason"]

    def test_size_that_cannot_be_given_is_its_own_status(self):
        e = self._entry(SS.quote_record(_qs(d25_spread=0.1667), AS_OF, MOVES), size_fn=lambda s, p, n: (0, "too big"))
        assert e["status"] == "no_size" and e["contracts"] is None and e["reason"] == "too big"


class TestExitRecord:
    @staticmethod
    def _ok_entry():
        return TestEntryRecord._entry(SS.quote_record(_qs(d25_spread=0.1667), AS_OF, MOVES))

    def test_short_buys_back_at_ask_by_hand(self):
        out = SS.exit_record(self._ok_entry(), exit_date=EXIT_DAY, basis="cboe_mid",
                             quotes={SH_C: _q(0.2, 0.3), SH_P: _q(2.8, 3.0)})
        ex = out["exit"]
        assert ex["status"] == "ok" and ex["basis"] == "cboe_mid" and ex["exit_premium"] == pytest.approx(3.3)   # 0.3 + 3.0
        assert ex["pnl_per_share"] == pytest.approx(2.0 - 3.3)
        assert ex["pnl_usd"] == pytest.approx((2.0 - 3.3) * 100 * 30)                                           # −3,900
        assert out["premium"] == 2.0 and out["contracts"] == 30                                                # 入场部分原样保留

    def test_intrinsic_basis_settles_both_legs_at_the_ledgers_close(self):
        out = SS.exit_record(self._ok_entry(), exit_date="2026-10-02", basis="intrinsic", underlying=115.0)
        ex = out["exit"]                                           # call 110：5.0，put 90：0
        assert ex["status"] == "ok" and ex["exit_premium"] == pytest.approx(5.0)
        assert ex["pnl_usd"] == pytest.approx((2.0 - 5.0) * 100 * 30)

    def test_long_sells_at_bid_and_gains_when_the_legs_got_dearer(self):
        e = TestEntryRecord._entry(SS.quote_record(_qs(d25_spread=0.1667), AS_OF, MOVES), side="long")
        out = SS.exit_record(e, exit_date=EXIT_DAY, basis="cboe_mid", quotes={SH_C: _q(0.2, 0.3), SH_P: _q(2.8, 3.0)})
        assert out["exit"]["exit_premium"] == pytest.approx(3.0) and out["exit"]["pnl_per_share"] == pytest.approx(3.0 - 2.4)

    @pytest.mark.parametrize("name,kw,reason", [
        ("某腿没有 ask", {"basis": "cboe_mid", "quotes": {SH_C: {"quote_ok": True, "bid": 0.1, "ask": None}, SH_P: _q(2.8, 3.0)}},
         "no_exit_quote:call"),
        ("某腿报价整个没有", {"basis": "cboe_mid", "quotes": {SH_C: _q(0.2, 0.3)}}, "no_exit_quote:put"),
        ("两腿都没有", {"basis": "cboe_mid", "quotes": {}}, "no_exit_quote:call+put"),
        ("内在价值缺标的价", {"basis": "intrinsic", "underlying": None}, "no_underlying"),
        ("账本核销", {"basis": "written_off"}, "ledger_written_off"),
    ])
    def test_unpriced_is_named_and_has_no_numbers(self, name, kw, reason):
        ex = SS.exit_record(self._ok_entry(), exit_date=EXIT_DAY, **kw)["exit"]
        assert ex["status"] == "unpriced" and ex["reason"] == reason, name
        assert ex["pnl_usd"] is None and ex["exit_premium"] is None, name       # 平仓无价不是 0

    def test_entry_not_ok_means_not_applicable(self):
        e = TestEntryRecord._entry(None)
        ex = SS.exit_record(e, exit_date=EXIT_DAY, basis="cboe_mid", quotes={})["exit"]
        assert ex["status"] == "not_applicable" and ex["reason"] == "entry_no_signal_record" and ex["pnl_usd"] is None


class TestConventionsMatchLedger:
    @pytest.mark.parametrize("side", ["long", "short"])
    def test_fills_equal_the_ledgers(self, side):
        # 不含布尔价：账本的 `_num` 会把 True 当 1.0（一个从没出现过的输入），影子记录更严（拒绝布尔），不对拍这一点
        legs = [None, {}, {"bid": 0.0, "ask": 1.0}, {"bid": 1.0, "ask": None}, {"bid": 1.0, "ask": 1.2}, {"bid": -1.0, "ask": 2.0},
                {"bid": float("nan"), "ask": 1.0}, {"bid": "1.1", "ask": "x"}]
        for c, p in itertools.product(legs, legs):
            assert SS.entry_fills(side, c, p) == opl.entry_fills(side, c, p), (side, c, p)
            assert SS.exit_fills(side, c, p) == opl.exit_fills(side, c, p), (side, c, p)


class TestShadowChangesNothing:
    def test_signals_identical_except_the_shadow_key(self, tmp_path, monkeypatch):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        monkeypatch.setattr(evs, "STATE_DIR", tmp_path / "s")
        monkeypatch.setattr(evs, "SIGNALS_FILE", tmp_path / "s" / "a.jsonl")
        monkeypatch.setattr(evs, "_today_pdt", lambda: AS_OF)
        monkeypatch.setattr(evs, "_now_et", lambda: datetime(2026, 9, 3, 18, 0, tzinfo=ZoneInfo("America/New_York")))
        cache = tmp_path / "cache"
        cache.mkdir()
        _write_snap(cache, "XYZ", AS_OF, _qs(d25_spread=0.1667), rv=20.0)
        _write_snap(cache, "ABC", AS_OF, _qs(c=(2.0, 2.2), p=(2.0, 2.2)), rv=20.0)
        kw = dict(cache_dir=cache, stats_fn=lambda tk: _stats(), upcoming_fn=lambda tk: {"earnings_date": EARN, "earnings_time": "AMC"})
        on = evs.scan(AS_OF, **kw)
        monkeypatch.setattr(evs, "SIGNALS_FILE", tmp_path / "s" / "b.jsonl")
        monkeypatch.setattr(SS, "quote_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        off = evs.scan(AS_OF, **kw)
        strip = lambda rows: [{k: v for k, v in r.items() if k != "strangle_quote"} for r in rows]  # noqa: E731
        assert strip(on) == strip(off) and len(on) == 2
        assert all(r["strangle_quote"]["schema_version"] == 1 for r in on) and all(r["strangle_quote"] is None for r in off)
        assert SS.stats()["signal_error"] == 2 and SS.stats()["errors"] == 2          # 抛了的那两次在源头登记了

    def _two_ledgers(self, tmp_path, monkeypatch, *, exit_signals_quotes=None, closes_fn=None, exit_day=EXIT_DAY):
        """同一输入、有 / 无 `strangle_quote` 各跑一遍开仓日 + 平仓日，返回两本账本的全部落盘内容。"""
        out = {}
        for name, with_quote in (("on", True), ("off", False)):
            d = _paths(tmp_path, monkeypatch, name)
            opened = _run(AS_OF, [_signal(with_quote=with_quote)])
            mid = {"opened": opened["opened_today"], "positions": _read_jsonl(opl.POSITIONS_FILE)}
            closed = _run(exit_day, [], quotes_fn=exit_signals_quotes or exit_quotes(), closes_fn=closes_fn)
            out[name] = {**mid, "closed_today": closed["closed_today"], "closed": _read_jsonl(opl.CLOSED_FILE),
                         "equity": _read_jsonl(opl.EQUITY_FILE), "meta": json.loads((d / "meta.json").read_text(encoding="utf-8")),
                         "nav": closed["nav"], "cash": closed["cash"]}
        return out

    def test_ledger_opens_closes_and_books_the_same(self, tmp_path, monkeypatch):
        r = self._two_ledgers(tmp_path, monkeypatch)
        on, off = r["on"], r["off"]
        assert on["opened"] and on["closed"], "夹具得真的开过仓、平过仓，否则下面的相等什么也没验证"
        # 正对照：on 真的带了完整记录；off（信号行没有 strangle_quote）只带「没有信号记录」，两边的差异被下面的逐项相等整个盖住
        assert on["opened"][0]["shadow_strangle"]["status"] == "ok"
        assert off["opened"][0]["shadow_strangle"]["status"] == "no_signal_record"
        assert on["closed"][0]["shadow_strangle"]["exit"]["status"] == "ok"
        assert off["closed"][0]["shadow_strangle"]["exit"]["status"] == "not_applicable"
        for k in ("opened", "positions", "closed_today", "closed", "equity", "nav", "cash"):
            assert SS_strip(on[k]) == SS_strip(off[k]), k
        m_on, m_off = dict(on["meta"]), dict(off["meta"])
        assert m_on == m_off                                           # 现金 / 最后一轮 / 跳过原因全相同

    def test_intrinsic_close_is_also_unchanged(self, tmp_path, monkeypatch):
        r = self._two_ledgers(tmp_path, monkeypatch, exit_day=EXPIRY, exit_signals_quotes=lambda t, s: {}, closes_fn=lambda t, d: 105.0)
        on, off = r["on"], r["off"]
        assert on["closed"] and on["closed"][0]["mark_source"] == "intrinsic"
        for k in ("closed_today", "closed", "equity", "nav", "cash"):
            assert SS_strip(on[k]) == SS_strip(off[k]), k

    def test_entry_hook_failure_leaves_the_position_alone(self, tmp_path, monkeypatch):
        _paths(tmp_path, monkeypatch, "base")
        base = _run(AS_OF, [_signal()])["opened_today"][0]
        _paths(tmp_path, monkeypatch, "boom")
        monkeypatch.setattr(SS, "entry_record", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        got = _run(AS_OF, [_signal()])["opened_today"]
        assert got and SS_strip(got[0]) == SS_strip(base) and "shadow_strangle" not in got[0]
        s = SS.stats()
        assert s["entry_error"] == 1 and s["errors"] == 1 and s["items"][0]["ticker"] == "XYZ"

    def test_exit_hook_failure_leaves_the_close_alone(self, tmp_path, monkeypatch):
        _paths(tmp_path, monkeypatch, "ok")
        _run(AS_OF, [_signal()])
        _run(EXIT_DAY, [], quotes_fn=exit_quotes())
        base = _closed_rows()[0]
        _paths(tmp_path, monkeypatch, "boom")
        _run(AS_OF, [_signal()])
        monkeypatch.setattr(SS, "exit_record", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        _run(EXIT_DAY, [], quotes_fn=exit_quotes())
        got = _closed_rows()[0]
        assert SS_strip(got) == SS_strip(base) and got["pnl_usd"] == base["pnl_usd"]
        assert got["shadow_strangle"]["exit"]["status"] == "error"          # 标 error，不留一条看着像没发生过的记录
        assert SS.stats()["exit_error"] == 1

    def test_shadow_quote_failure_is_an_error_but_the_close_is_untouched(self, tmp_path, monkeypatch):
        """账本自己那次报价成功、影子那次报价抛——平仓照常，影子记录标 error 并在源头登记。"""
        _paths(tmp_path, monkeypatch, "q")
        _run(AS_OF, [_signal()])

        def fn(tk, syms):
            if SH_C in syms:
                raise TimeoutError("cboe")
            return exit_quotes()(tk, syms)
        _run(EXIT_DAY, [], quotes_fn=fn)
        got = _closed_rows()
        assert len(got) == 1 and got[0]["mark_source"] == "cboe_mid" and got[0]["shadow_strangle"]["exit"]["status"] == "error"
        assert SS.stats()["exit_error"] == 1

    def test_old_positions_round_trip_byte_for_byte(self):
        old = {"ticker": "X", "side": "short", "entry_date": "2026-10-01", "expiry": "2026-10-30", "strike": 10.0,
               "call_symbol": "a", "put_symbol": "b", "contracts": 1, "entry_call": 1.0, "entry_put": 1.0, "entry_premium": 2.0,
               "entry_underlying": 10.0, "earnings_date": "2026-10-10", "signal_ratio": 1.5, "label": "rich", "size_usd": 200.0,
               "last_mark": 2.0, "last_mark_date": "2026-10-01", "mark_source": "cboe_mid", "rationale": "", "stale_days": 0,
               "settle_deferred_since": None, "settle_deferred_reason": None}
        assert opl.StraddlePosition.from_record(old).to_dict() == old          # 登记前的持仓：整键省略，重写不变

    def test_a_legacy_position_closes_without_a_record_and_without_an_extra_quote_call(self, tmp_path, monkeypatch):
        """登记前入场的持仓（没有 `shadow_strangle`）：平仓时不凭空造影子记录，也不多打一次报价。"""
        _paths(tmp_path, monkeypatch, "legacy")
        monkeypatch.setattr(opl, "_attach_shadow_entry", lambda pos, sig, nav: None)      # 复刻「新代码上线前开的仓」
        _run(AS_OF, [_signal()])
        calls = []

        def fn(tk, syms):
            calls.append(list(syms))
            return exit_quotes()(tk, syms)
        _run(EXIT_DAY, [], quotes_fn=fn)
        got = _closed_rows()[0]
        assert "shadow_strangle" not in got and calls == [[ATM_C, ATM_P]]
        assert SS.stats()["errors"] == 0 and SS.units([got])[0]["status"] == "no_record"

    def test_a_signal_without_the_record_is_named_not_dropped(self, tmp_path, monkeypatch):
        """部署当天先于新代码落盘的信号：新代码开的仓带 `no_signal_record`，平仓不多打报价、不算盈亏，单位里如实报这一类。"""
        _paths(tmp_path, monkeypatch, "nosig")
        op = _run(AS_OF, [_signal(with_quote=False)])["opened_today"][0]
        assert op["shadow_strangle"]["status"] == "no_signal_record"
        calls = []

        def fn(tk, syms):
            calls.append(list(syms))
            return exit_quotes()(tk, syms)
        _run(EXIT_DAY, [], quotes_fn=fn)
        got = _closed_rows()[0]
        assert calls == [[ATM_C, ATM_P]] and got["shadow_strangle"]["exit"]["status"] == "not_applicable"
        assert SS.units([got])[0]["status"] == "entry_no_signal_record"
        s = SS.stats()
        assert s["entry_no_signal_record"] == 1 and s["errors"] == 0


def SS_strip(obj):                                  # noqa: N802 - 模块级递归去掉影子键，供差分比较
    if isinstance(obj, dict):
        return {k: SS_strip(v) for k, v in obj.items() if k != "shadow_strangle"}
    if isinstance(obj, list):
        return [SS_strip(x) for x in obj]
    return obj


class TestEndToEndByHand:
    """从信号到检验单位，用手算的数走一遍真实的账本开 / 平仓。"""

    def test_cboe_mid_exit_numbers(self, tmp_path, monkeypatch):
        _paths(tmp_path, monkeypatch, "e2e")
        op = _run(AS_OF, [_signal()])["opened_today"][0]
        sh = op["shadow_strangle"]
        assert sh["status"] == "ok" and sh["premium"] == pytest.approx(2.0) and sh["contracts"] == 30 and sh["nav_for_sizing"] == 100_000.0
        assert op["contracts"] == 6 and op["entry_premium"] == pytest.approx(9.8)               # 同一份入场的跨式
        _run(EXIT_DAY, [], quotes_fn=exit_quotes())
        c = _closed_rows()[0]
        assert c["pnl_usd"] == pytest.approx((9.8 - 12.2) * 100 * 6)                            # −1,440
        ex = c["shadow_strangle"]["exit"]
        assert ex["pnl_usd"] == pytest.approx((2.0 - 3.3) * 100 * 30)                           # −3,900
        (u,) = SS.units([c])
        assert u["status"] == "ok" and u["d"] == pytest.approx((-3900 - (-1440)) / 100_000.0) and u["block"] == "2026-W38"

    def test_intrinsic_exit_numbers(self, tmp_path, monkeypatch):
        _paths(tmp_path, monkeypatch, "intr")
        _run(AS_OF, [_signal()])
        _run(EXPIRY, [], quotes_fn=lambda t, s: {}, closes_fn=lambda t, d: 105.0)
        c = _closed_rows()[0]
        assert c["mark_source"] == "intrinsic" and c["pnl_usd"] == pytest.approx((9.8 - 5.0) * 100 * 6)
        ex = c["shadow_strangle"]["exit"]
        assert ex["basis"] == "intrinsic" and ex["exit_premium"] == pytest.approx(0.0)          # 105 在 90 与 110 之间：两腿都归零
        assert ex["pnl_usd"] == pytest.approx(2.0 * 100 * 30)

    def test_exit_without_a_shadow_ask_is_unpriced_and_excluded(self, tmp_path, monkeypatch):
        _paths(tmp_path, monkeypatch, "unp")
        _run(AS_OF, [_signal()])
        _run(EXIT_DAY, [], quotes_fn=exit_quotes({SH_C: {"quote_ok": True, "bid": 0.0, "ask": None}}))
        c = _closed_rows()[0]
        assert c["shadow_strangle"]["exit"]["reason"] == "no_exit_quote:call"
        assert SS.units([c])[0]["status"] == "exit_unpriced" and SS.stats()["exit_unpriced"] == 1 and SS.stats()["errors"] == 0


class TestReaders:
    KEYS = ("strangle_quote", "shadow_strangle")
    ALLOWED_FILES = {"earnings_vol_signal.py", "options_paper_leg.py", "straddle_strangle_shadow.py"}
    #: 账本里允许出现这两个键的函数：记录它们的 helper，与两个 dataclass 的 to_dict（整键省略）。开 / 平仓判断函数一个都不在内
    OPL_FUNCS = {"_attach_shadow_entry", "_attach_shadow_exit", "to_dict", "_public_closed"}
    EVS_FUNCS = {"strangle_quote", "_scan_with_info"}

    @classmethod
    def _funcs_mentioning(cls, src: str) -> set:
        tree = ast.parse(src)
        hits = set()
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for n in ast.walk(ast.Module(body=fn.body, type_ignores=[])):
                if (isinstance(n, ast.Constant) and n.value in cls.KEYS) or (isinstance(n, ast.Name) and n.id in cls.KEYS) \
                        or (isinstance(n, ast.Attribute) and n.attr in cls.KEYS):
                    hits.add(fn.name)
        return hits

    def test_only_registered_files_mention_the_keys(self):
        files, _how = own_python_files(REPO)
        out = set()
        for p in files:
            rel = p.relative_to(REPO)
            if rel.parts[0] in {"tests", "experiments"}:
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
            if any(k in txt for k in self.KEYS):
                out.add(rel.as_posix())
        assert out == self.ALLOWED_FILES, out

    def test_ledger_decisions_never_read_them(self):
        src = (REPO / "options_paper_leg.py").read_text(encoding="utf-8")
        got = self._funcs_mentioning(src)
        assert got <= self.OPL_FUNCS, got - self.OPL_FUNCS
        assert {"_attach_shadow_entry", "_attach_shadow_exit"} <= got, "探针要看得见真实的读写点，否则上一条恒真"
        assert "_open_from_signal" not in got and "_run_for_date" not in got and "_close" not in got

    def test_signal_decisions_never_read_them(self):
        src = (REPO / "earnings_vol_signal.py").read_text(encoding="utf-8")
        got = self._funcs_mentioning(src)
        assert got <= self.EVS_FUNCS, got - self.EVS_FUNCS
        assert "_scan_with_info" in got, "探针要看得见真实的写入点，否则上一条恒真"
        assert "compute_signal" not in got

    def test_has_teeth(self):
        """往开仓判断里种一次读取：探针必须把 `_open_from_signal` 抓出来。"""
        src = (REPO / "options_paper_leg.py").read_text(encoding="utf-8")
        planted = src.replace('    label = sig.get("label")\n    if label == "rich":',
                              '    if (sig.get("strangle_quote") or {}).get("available"):\n        return None, "planted"\n'
                              '    label = sig.get("label")\n    if label == "rich":', 1)
        assert planted != src
        assert "_open_from_signal" in self._funcs_mentioning(planted)


class TestFailuresAreVisible:
    def test_unknown_event_kind_raises_instead_of_being_filed_somewhere_else(self):
        with pytest.raises(KeyError):
            SS.note("entry_eror", "XYZ", "typo")

    def test_errors_count_only_failures_not_data(self):
        for k in ("entry_ok", "entry_unquotable", "entry_no_signal_record", "entry_no_size", "exit_ok", "exit_unpriced", "signal_recorded"):
            SS.note(k, "XYZ")
        assert SS.stats()["errors"] == 0 and SS.stats()["items"] == []
        SS.note("exit_error", "XYZ", "ValueError: x")
        s = SS.stats()
        assert s["errors"] == 1 and s["items"] == [{"kind": "exit_error", "ticker": "XYZ", "detail": "ValueError: x"}]
        SS.reset_stats()
        assert SS.stats()["errors"] == 0

    def test_scan_timing_carries_the_counters(self):
        import scan_timing
        assert scan_timing.counters()["strangle_shadow"]["errors"] == 0
        SS.note("entry_error", "XYZ", "x")
        assert scan_timing.counters()["strangle_shadow"]["errors"] == 1

    def _alerts(self, tmp_path, counters):
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a._check_strangle_shadow({"scan_timing": {"counters": counters}})
        return a

    def test_alert_manager_goes_p2_on_errors_only(self, tmp_path):
        ok = self._alerts(tmp_path, {"strangle_shadow": {**{k: 0 for k in SS._ZERO}, "errors": 0, "items": []}})
        assert not ok.alerts and not ok.checks_skipped
        bad = self._alerts(tmp_path, {"strangle_shadow": {"errors": 2, "items": [{"kind": "entry_error", "ticker": "T", "detail": "x"}]}})
        assert len(bad.alerts) == 1 and bad.alerts[0].level == am.AlertLevel.MEDIUM and "P2" in bad.alerts[0].message
        assert "entry_error T" in bad.alerts[0].details["明细"]
        assert not self._alerts(tmp_path, {"strangle_shadow": {"errors": 0, "entry_unquotable": 9, "exit_unpriced": 3, "items": []}}).alerts

    def test_missing_counter_is_skipped_not_green(self, tmp_path):
        a = self._alerts(tmp_path, {"strangle_shadow": None})
        assert not a.alerts and any("影子宽跨" in s for s in a.checks_skipped)


class TestUnitsAndProgress:
    def test_status_of_every_kind_of_closed_trade(self):
        rows = [
            _closed_row(),                                                                     # ok
            _closed_row(entry_date="2025-12-31", earnings_date="2026-01-10"),                  # 登记前（start_date 钉在 2026-01-01）
            {"ticker": "A", "side": "short", "entry_date": "2026-10-22", "earnings_date": "2026-10-28", "pnl_usd": 1.0},   # 没有记录
            _closed_row(side="long"),
            _closed_row(status="unquotable", reason="quote_not_ok:put"),
            _closed_row(status="no_signal_record"),
            _closed_row(exit={"status": "unpriced", "reason": "no_exit_quote:call", "pnl_usd": None}),
            _closed_row(exit={"status": "error", "reason": "ValueError"}),
            _closed_row(schema_version=2),
            _closed_row(nav_for_sizing=None),
        ]
        got = [u["status"] for u in SS.units(rows)]
        assert got == ["ok", "before_start", "no_record", "long_side", "entry_unquotable", "entry_no_signal_record",
                       "exit_unpriced", "exit_error", "schema:2", "unusable"]

    def test_d_is_the_nav_scaled_pnl_difference(self):
        (u,) = SS.units([_closed_row(pnl=-1000.0, shadow_pnl=-3000.0)])
        assert u["d"] == pytest.approx(-2000.0 / 100_000.0)

    def test_start_date_boundary_is_inclusive(self, monkeypatch):
        monkeypatch.setitem(SS.PREREG, "start_date", "2026-10-21")
        s = [u["status"] for u in SS.units([_closed_row(entry_date="2026-10-20"), _closed_row(entry_date="2026-10-21")])]
        assert s == ["before_start", "ok"]

    def test_progress_has_counts_only_and_no_blinded_keys(self):
        pos = [{"entry_date": "2026-10-22", "shadow_strangle": {"status": "ok"}}, {"entry_date": "2026-10-22"},
               {"entry_date": "2025-01-01", "shadow_strangle": {"status": "ok"}}]
        pr = SS.progress([_closed_row(), _closed_row(earnings_date="2026-11-04")], pos)
        assert pr["n_units"] == 2 and pr["n_blocks"] == 2 and pr["n_closed"] == 2 and pr["ready"] is False and pr["frozen"] is False
        assert pr["n_open_recorded"] == 1 and pr["open_by_status"] == {"ok": 1}
        assert pr["need"] == {"min_units": 30, "min_blocks": 8}

        def keys(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield k
                    yield from keys(v)
        assert not (SS.BLINDED_KEYS & set(keys(pr))), set(keys(pr))

    def test_ready_needs_both_gates(self, monkeypatch):
        weeks = ["2026-10-28", "2026-11-04", "2026-11-11", "2026-11-18", "2026-11-25", "2026-12-02", "2026-12-09", "2026-12-16"]
        monkeypatch.setitem(SS.PREREG, "min_units", 8)
        assert SS.progress([_closed_row(earnings_date=w) for w in weeks])["ready"] is True
        assert SS.progress([_closed_row(earnings_date=w) for w in weeks[:7]])["ready"] is False                     # 单位不够
        assert SS.progress([_closed_row(earnings_date="2026-10-28")] * 8)["ready"] is False                        # 块不够
        monkeypatch.setitem(SS.PREREG, "min_blocks", 8)
        assert SS.progress([_closed_row(earnings_date=w) for w in weeks])["ready"] is True


def _brute_p(by_block, obs, n):
    """独立的暴力实现：显式枚举每块的符号。"""
    blocks = list(by_block.values())
    hits = total = 0
    for signs in itertools.product((1, -1), repeat=len(blocks)):
        stat = sum(s * sum(b) for s, b in zip(signs, blocks)) / n
        total += 1
        hits += abs(stat) >= abs(obs) - 1e-12
    return hits / total


class TestSignFlipTest:
    @staticmethod
    def _units(spec):
        return [{"status": "ok", "d": d, "block": b} for b, ds in spec.items() for d in ds]

    def test_matches_brute_force(self):
        spec = {"W1": [0.01, 0.02], "W2": [-0.005], "W3": [0.03, 0.01, 0.02], "W4": [0.015], "W5": [-0.02, 0.04]}
        us = self._units(spec)
        n = len(us)
        obs = sum(d for ds in spec.values() for d in ds) / n
        r = SS.block_signflip_test(us, n_perm=100, seed=1, enum_max_blocks=16)
        assert r["testable"] and r["mode"] == "enumerate" and r["n_units"] == n and r["n_blocks"] == 5
        assert r["observed"] == pytest.approx(obs) and r["p_value"] == pytest.approx(_brute_p(spec, obs, n))

    def test_the_block_is_the_unit_of_flipping(self):
        """块内共用一个符号：三个同号单位的两个块，p = 2 ÷ 4；若按单位翻符号会得到更小的 p（把同周相关当成独立）。"""
        us = self._units({"W1": [0.01, 0.01, 0.01], "W2": [0.01, 0.01, 0.01]})
        r = SS.block_signflip_test(us, n_perm=100, seed=1, enum_max_blocks=16)
        assert r["p_value"] == pytest.approx(2 / 4)
        naive = sum(abs(sum(s * 0.01 for s in sg)) >= abs(6 * 0.01) - 1e-12 for sg in itertools.product((1, -1), repeat=6)) / 64
        assert naive == pytest.approx(2 / 64) and r["p_value"] > naive

    def test_all_same_sign_over_eight_blocks_is_the_resolution_floor(self):
        r = SS.block_signflip_test(self._units({f"W{i}": [0.01] for i in range(8)}), n_perm=10, seed=1, enum_max_blocks=16)
        assert r["p_value"] == pytest.approx(2 / 256) and r["decision"] == "reject_h0" and r["direction"] == "strangle_higher"

    def test_zero_mean_does_not_reject_and_direction_follows_sign(self):
        r = SS.block_signflip_test(self._units({"W1": [0.02], "W2": [-0.02], "W3": [0.01], "W4": [-0.01]}), n_perm=10, seed=1, enum_max_blocks=16)
        assert r["p_value"] == 1.0 and r["decision"] == "fail_to_reject_h0" and r["direction"] == "none"
        neg = SS.block_signflip_test(self._units({f"W{i}": [-0.01] for i in range(8)}), n_perm=10, seed=1, enum_max_blocks=16)
        assert neg["direction"] == "straddle_higher" and neg["decision"] == "reject_h0"

    def test_random_mode_is_reproducible_and_bounded(self):
        us = self._units({f"W{i:02d}": [0.01 * ((-1) ** i), 0.005] for i in range(20)})
        a = SS.block_signflip_test(us, n_perm=2000, seed=7, enum_max_blocks=16)
        b = SS.block_signflip_test(us, n_perm=2000, seed=7, enum_max_blocks=16)
        assert a["mode"] == "random" and a == b and 0 < a["p_value"] <= 1

    def test_only_ok_units_count(self):
        us = self._units({"W1": [0.01]}) + [{"status": "exit_unpriced", "d": None, "block": "W2"}, {"status": "ok", "d": None, "block": "W3"}]
        assert SS.block_signflip_test(us, n_perm=10, seed=1, enum_max_blocks=16)["n_units"] == 1
        assert SS.block_signflip_test([], n_perm=10, seed=1, enum_max_blocks=16) == {"testable": False, "reason": "no_units"}


class TestFreeze:
    @pytest.fixture
    def tiny(self, monkeypatch):
        monkeypatch.setitem(SS.PREREG, "min_units", 3)
        monkeypatch.setitem(SS.PREREG, "min_blocks", 3)
        return [_closed_row(earnings_date=w, pnl=-100.0, shadow_pnl=-100.0 - 500.0 * (i + 1))
                for i, w in enumerate(("2026-10-28", "2026-11-04", "2026-11-11"))]

    def test_default_location_is_the_paths_property_in_the_sandbox(self, tmp_path):
        from hive_logger import PATHS
        assert SS._result_path() == PATHS.straddle_strangle_shadow_result and SS._result_path().is_relative_to(tmp_path)

    def test_not_ready_refuses_and_writes_nothing(self, tmp_path):
        r = SS.run_once([_closed_row()], root=tmp_path)
        assert r["ran"] is False and r["reason"] == "not_ready" and not SS._result_path(tmp_path).exists()

    def test_runs_once_and_never_overwrites(self, tmp_path, tiny):
        first = SS.run_once(tiny, root=tmp_path, today="2026-12-01")
        p = SS._result_path(tmp_path)
        assert first["testable"] and first["direction"] == "straddle_higher" and p.is_file()
        raw = p.read_bytes()
        again = SS.run_once(tiny + [_closed_row(earnings_date="2026-11-18")], root=tmp_path)         # 样本变了也不重算
        assert again["already_frozen"] is True and again["observed"] == first["observed"] and p.read_bytes() == raw
        assert SS.progress(tiny, root=tmp_path)["frozen"] is True
        assert not [x for x in p.parent.iterdir() if ".tmp." in x.name], "临时文件必须清掉"
        assert first["unit_keys"] and all(len(k) == 3 for k in first["unit_keys"])

    def test_first_writer_wins_when_another_process_links_first(self, tmp_path, tiny, monkeypatch):
        p = SS._result_path(tmp_path)

        def lose_race(src, dst):
            Path(dst).write_text(json.dumps({"winner": "other", "observed": 1}), encoding="utf-8")
            raise FileExistsError(dst)
        monkeypatch.setattr(os, "link", lose_race)
        r = SS.run_once(tiny, root=tmp_path)
        assert r["already_frozen"] is True and r["winner"] == "other" and json.loads(p.read_text())["winner"] == "other"
        assert not [x for x in p.parent.iterdir() if ".tmp." in x.name]

    def test_unreadable_result_is_not_recomputed(self, tmp_path, tiny):
        p = SS._result_path(tmp_path)
        p.parent.mkdir(parents=True)
        p.write_text("{not json", encoding="utf-8")
        r = SS.run_once(tiny, root=tmp_path)
        assert r["already_frozen"] is True and "error" in r and p.read_text() == "{not json"

    def test_main_prints_progress_without_the_blinded_numbers(self, tmp_path, monkeypatch, capsys):
        sd = tmp_path / "options_paper_state"
        sd.mkdir()
        (sd / "closed_trades.jsonl").write_text(json.dumps(_closed_row()) + "\n", encoding="utf-8")
        (sd / "positions.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
        assert SS.main([]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["n_units"] == 1 and not (SS.BLINDED_KEYS & set(out))


class TestBlinding:
    def test_alpha_bot_never_exposes_the_shadow_record_or_pnl(self, tmp_path):
        from alphabot import straddle as SD
        from alphabot import synthetic as syn
        from tests.test_alphabot_straddle import AS_OF as BOT_AS_OF, _client, _keys, _svc, _write_ledger
        led = syn.demo_straddle_ledger(BOT_AS_OF)
        sh = _closed_row()["shadow_strangle"]
        for k in ("closed", "positions"):
            for r in led[k]:
                r["shadow_strangle"] = sh
        for s in led["signals"]:
            s["strangle_quote"] = SS.quote_record(_qs(d25_spread=0.1667), BOT_AS_OF, MOVES)
        root = _write_ledger(tmp_path / "data", led)
        body = _client(_svc(root)).get("/api/straddle").json()
        keys = set(_keys(body))
        assert not ({"shadow_strangle", "strangle_quote", "features", "nav_for_sizing", "pnl_per_share"} & keys), keys
        assert body["closed"] and body["positions"], "正对照：账本里确实有带影子记录的行"
        ss = body["strangle_shadow"]
        assert ss["available"] and ss["need"]["min_units"] == 30 and "n_units" in ss and not (SS.BLINDED_KEYS & set(_keys(ss)))
        assert body["rules"]["strangle_prereg"]["doc"].endswith("straddle_strangle_shadow_prereg.md")
        assert SD.BLINDED_KEYS.isdisjoint(keys)

    def test_front_end_panel_reads_counts_only(self):
        js = (REPO / "alphabot" / "static" / "pages" / "straddle.js").read_text(encoding="utf-8")
        a, b = js.index("function strangleShadowPanel"), js.index("export async function renderStraddle")
        code = "\n".join(ln for ln in js[a:b].splitlines() if not ln.lstrip().startswith("//"))
        assert "n_units" in code and "n_blocks" in code, "正对照：截到的确实是影子面板那一段"
        for w in ("pnl", "shadow_strangle", "observed", "p_value"):
            assert w not in code, w

    def test_the_panel_numbers_come_from_the_ledger_not_just_the_keys(self, tmp_path):
        """面板接线有取值断言（二次检查：此前只断言键在，service 里读错字段也全绿）。"""
        from alphabot import synthetic as syn
        from tests.test_alphabot_straddle import AS_OF as BOT_AS_OF, _client, _svc, _write_ledger
        led = syn.demo_straddle_ledger(BOT_AS_OF)
        assert led["closed"], "正对照：演示账本里有已平仓"
        root = _write_ledger(tmp_path / "data", led)
        ss = _client(_svc(root)).get("/api/straddle").json()["strangle_shadow"]
        assert ss["n_closed"] == len(led["closed"]) and ss["start_date"] == SS.PREREG["start_date"] == "2026-01-01"
        assert sum(ss["by_status"].values()) == ss["n_closed"], "每笔平仓恰好一个状态"
        assert ss["prereg_version"] == SS.PREREG["version"] and ss["frozen"] is False


# ══════════════════════════════ v0.45.593 二次检查补的守卫 ══════════════════════════════

class TestRunForDateIsBlind:
    """`run_for_date` 的返回值（= `python options_paper_leg.py <日>` 的回显）不带 `shadow_strangle`：手动补跑平仓日时，
    平仓记录里同时躺着跨式盈亏和影子宽跨盈亏，等于代人把一对数字摆在一起（协议 §7，修订 2）。落盘文件保留完整记录。"""

    def test_return_value_and_cli_echo_carry_no_shadow_block(self, tmp_path, monkeypatch):
        _paths(tmp_path, monkeypatch, "blind")
        _run(AS_OF, [_signal()])
        r = _run(EXIT_DAY, [], quotes_fn=exit_quotes())
        assert len(r["closed_today"]) == 1, "正对照：这一天真的平了仓"
        persisted = _closed_rows()[0]["shadow_strangle"]["exit"]["pnl_usd"]
        assert persisted == pytest.approx(-3900.0), "正对照：落盘的完整记录里确实有影子盈亏"
        echo = json.dumps({k: v for k, v in r.items() if k != "positions"}, ensure_ascii=False)   # 与模块 CLI 的回显同一写法
        assert "shadow_strangle" not in echo and repr(persisted) not in echo
        assert r["closed_today"][0]["pnl_usd"] == pytest.approx(-1440.0), "跨式自己的盈亏照常在"

    def test_the_stripped_copy_leaves_the_stored_row_alone(self):
        row = _closed_row()
        out = opl._public_closed(row)
        assert "shadow_strangle" not in out and "shadow_strangle" in row and out["pnl_usd"] == row["pnl_usd"]


class TestExitBeforeEvent:
    """修订 2：账本平仓日 ≤ 财报日的单位不进检验（协议 §4）。"""

    def test_status_by_exit_date(self):
        rows = [_closed_row(exit_date="2026-10-28"),          # 财报当天平（盘后假设：公告还没进报价）
                _closed_row(exit_date="2026-10-27"),          # 财报前一天
                _closed_row(exit_date="2026-10-29"),          # 次日 ⇒ 事后
                _closed_row(exit_date="")]                    # 平仓记录没有平仓日 ⇒ 不是「事前」，是用不了
        got = SS.units(rows)
        assert [u["status"] for u in got] == ["exit_before_event", "exit_before_event", "ok", "unusable"]
        assert [u["d"] is None for u in got] == [True, True, False, True], "不进检验的单位没有 d"

    def test_progress_counts_them_apart_and_they_do_not_make_the_test_ready(self, monkeypatch):
        monkeypatch.setitem(SS.PREREG, "min_units", 1)
        monkeypatch.setitem(SS.PREREG, "min_blocks", 1)
        pr = SS.progress([_closed_row(exit_date="2026-10-28")])
        assert pr["by_status"] == {"exit_before_event": 1} and pr["n_units"] == 0 and pr["ready"] is False

    def test_ledger_buffer_exit_on_the_earnings_date_is_excluded(self, tmp_path, monkeypatch):
        """真实账本路径：财报日 = 到期日 − 2 的盘后财报，账本在财报当晚按 expiry_buffer 强平（审查里复现过的形状）。"""
        _paths(tmp_path, monkeypatch, "buffer")
        qs, stats = _qs(d25_spread=0.1667), {**_stats(), "moves": MOVES}
        sig = evs.compute_signal("XYZ", AS_OF, qs, {"earnings_date": "2026-09-30", "earnings_time": "AMC"}, stats, None)
        assert sig["eligible"] and sig["tradeable"], "夹具得是账本会开仓的信号，否则下面什么也没验证"
        sig["strangle_quote"] = evs.strangle_quote(qs, stats, "XYZ", AS_OF)
        _run(AS_OF, [sig])
        _run("2026-09-30", [], quotes_fn=exit_quotes())
        (c,) = _closed_rows()
        assert c["exit_reason"] == "expiry_buffer" and c["exit_date"] == c["earnings_date"] == "2026-09-30"
        assert c["shadow_strangle"]["exit"]["status"] == "ok", "影子平得出价——被排除的理由只能是时点，不是无价"
        (u,) = SS.units([c])
        assert u["status"] == "exit_before_event" and u["d"] is None
        # 对照：财报日之后才平的那笔（TestEndToEndByHand）是 ok——同一套夹具，只差平仓日


class TestFrozenResultNotAnObject:
    """合法 JSON 但不是对象（null / [] / 0 ……）的冻结文件：与「读不了」同一条路径——不重算、不覆盖、不抛、CLI 非零。"""

    @pytest.mark.parametrize("content", ["null", "[]", "0", '"x"', "[1]", "true"])
    def test_every_non_object_is_an_error_not_unfrozen(self, tmp_path, content):
        closed = [_closed_row(earnings_date=w) for w in ("2026-10-28", "2026-11-04", "2026-11-11")]
        p = SS._result_path(tmp_path)
        p.parent.mkdir(parents=True)
        p.write_text(content, encoding="utf-8")
        assert "error" in SS.read_result(tmp_path)
        r = SS.run_once(closed, root=tmp_path)
        assert r["already_frozen"] is True and "error" in r and p.read_text(encoding="utf-8") == content
        pr = SS.progress(closed, root=tmp_path)
        assert pr["frozen"] is True and pr["frozen_error"] is True

    def test_a_good_result_is_not_flagged(self, tmp_path):
        p = SS._result_path(tmp_path)
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"decision": "fail_to_reject_h0"}), encoding="utf-8")
        assert SS.progress([], root=tmp_path)["frozen_error"] is False

    def test_cli_exits_nonzero_on_a_damaged_result(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setitem(SS.PREREG, "min_units", 1)
        monkeypatch.setitem(SS.PREREG, "min_blocks", 1)
        sd = tmp_path / "options_paper_state"
        sd.mkdir()
        (sd / "closed_trades.jsonl").write_text(json.dumps(_closed_row()) + "\n", encoding="utf-8")
        (sd / "positions.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
        (sd / SS.RESULT_NAME).write_text("null", encoding="utf-8")
        assert SS._result_path() == sd / SS.RESULT_NAME, "夹具得真的把坏文件放在 CLI 会读的位置"
        assert SS.main(["--run-once"]) == 1
        assert "error" in json.loads(capsys.readouterr().out)


class TestRecordedLegsAreFinite:
    def test_non_finite_numbers_become_none_and_the_rest_is_untouched(self):
        leg = {"symbol": "X", "strike": 110.0, "expiry": EXPIRY, "bid": float("nan"), "ask": float("inf"), "mid": 1.0,
               "spread_pct": float("-inf"), "delta": 0.25, "oi": 10, "quote_ok": True}
        b = SS.leg_brief(leg)
        assert (b["bid"], b["ask"], b["spread_pct"]) == (None, None, None)
        assert (b["mid"], b["delta"], b["oi"], b["quote_ok"], b["symbol"], b["expiry"]) == (1.0, 0.25, 10, True, "X", EXPIRY)
        json.dumps(b, allow_nan=False)

    def test_a_nan_in_the_snapshot_cannot_stop_the_signal_file_from_being_written(self, tmp_path, monkeypatch):
        """账本对信号文件做严格序列化（拒 NaN）。影子报价原样抄快照腿字段时，一个 NaN 会让整个文件写不进去、
        当天所有信号和账本一起丢（审查里复现过）。现行快照生产者清洗过非有限值，这里不再依赖上游。"""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        monkeypatch.setattr(evs, "STATE_DIR", tmp_path / "s")
        monkeypatch.setattr(evs, "SIGNALS_FILE", tmp_path / "s" / "a.jsonl")
        monkeypatch.setattr(evs, "_today_pdt", lambda: AS_OF)
        monkeypatch.setattr(evs, "_now_et", lambda: datetime(2026, 9, 3, 18, 0, tzinfo=ZoneInfo("America/New_York")))
        cache = tmp_path / "cache"
        cache.mkdir()
        qs = _qs(d25_spread=0.1667)
        qs["contracts"]["c25"] = {**qs["contracts"]["c25"], "oi": float("nan"), "delta": float("nan")}
        _write_snap(cache, "XYZ", AS_OF, qs, rv=20.0)
        rows = evs.scan(AS_OF, cache_dir=cache, stats_fn=lambda tk: {**_stats(), "moves": MOVES},
                        upcoming_fn=lambda tk: {"earnings_date": EARN, "earnings_time": "AMC"})
        assert len(rows) == 1 and rows[0]["strangle_quote"]["available"] is True
        assert rows[0]["strangle_quote"]["call"]["oi"] is None and rows[0]["strangle_quote"]["call"]["delta"] is None
        assert (tmp_path / "s" / "a.jsonl").is_file(), "信号文件真的写出来了"
        # 同一趟顺带钉住：生产形状的 stats → 点时点的历史涨跌 接线（_signal() 手工塞 moves，绕过了这条路径）
        assert len(rows[0]["strangle_quote"]["hist_moves"]) == 8
        assert all(m["earnings_date"] < AS_OF for m in rows[0]["strangle_quote"]["hist_moves"])


class TestWiringProbe:
    """接线整批断了（上游角色名漂移 / 快照形状变 / CBOE 链没有 delta）：每个标的都只是「25Δ 腿取不到」这种数据，
    `errors` 恒为 0——所以源头另数「取到 / 缺腿」，一整轮一个都没取到 ⇒ P2（v0.45.593；对标 GEX 影子的「0/N 可用」）。"""

    def _feed(self, available, missing):
        good, bad = _qs(d25_spread=0.1667), _qs()                      # 夹具默认没有 25Δ 腿 ⇒ missing_leg
        for _ in range(available):
            evs.strangle_quote(good, _stats(), "XYZ", AS_OF)
        for _ in range(missing):
            evs.strangle_quote(bad, _stats(), "XYZ", AS_OF)

    def test_the_hook_counts_available_and_missing_leg_apart(self):
        self._feed(2, 3)
        s = SS.stats()
        assert (s["signal_recorded"], s["signal_available"], s["signal_missing_leg"]) == (5, 2, 3) and s["errors"] == 0

    @pytest.mark.parametrize("qs", [None, {**_qs(), "data_available": False}])
    def test_no_quote_set_is_an_upstream_outage_not_a_missing_leg(self, qs):
        evs.strangle_quote(qs, _stats(), "XYZ", AS_OF)
        s = SS.stats()
        assert s["signal_recorded"] == 1 and s["signal_available"] == 0 and s["signal_missing_leg"] == 0

    @pytest.mark.parametrize("available,missing,suspect", [
        (0, 5, True), (0, 30, True),
        (0, 4, False),                 # 样本太少，不下结论
        (1, 29, False),                # 只要有一个取到，接线就是通的
        (0, 0, False)])
    def test_suspect_only_when_many_are_missing_and_none_arrived(self, available, missing, suspect):
        self._feed(available, missing)
        assert SS.stats()["wiring_suspect"] is suspect

    def test_alert_manager_reads_the_flag_and_names_it(self, tmp_path):
        self._feed(0, 6)
        import scan_timing
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a._check_strangle_shadow({"scan_timing": {"counters": scan_timing.counters()}})
        assert len(a.alerts) == 1 and a.alerts[0].level == am.AlertLevel.MEDIUM and "整批" in a.alerts[0].message
        assert "signal_missing_leg=6" in a.alerts[0].details["明细"]

    def test_the_real_analyze_entry_runs_the_shadow_check(self, tmp_path):
        """`analyze()` 才是生产入口：此前只测了 `_check_strangle_shadow` 本身，删掉 analyze 里那一行调用全绿。"""
        import scan_timing
        self._feed(0, 6)
        SS.note("entry_error", "XYZ", "ValueError: x")
        status = {"status": "success", "total_duration_seconds": 1,
                  "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"date": "2026-10-22", "production_sync": {"outcome": "up_to_date"}, "extra": {},
                                  "counters": scan_timing.counters()}}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status), encoding="utf-8")
        got = [x.message for x in am.AlertAnalyzer(report_dir=tmp_path).analyze(p) if "影子宽跨" in x.message]
        assert len(got) == 2 and any("整批" in m for m in got) and any("没写成" in m for m in got), got

    def test_real_quote_set_feeds_the_record_and_the_role_names_agree(self):
        """手搓夹具永远带着 c25 / p25，上游改了角色名这些测试也不会红：这里喂真的 `select_quote_set` 的输出，并对账三份角色名。"""
        import cboe_options
        from tests.test_quote_set import EXP_28, NOW, _payload
        assert set(SS.PREREG["strangle_roles"]) <= set(cboe_options._QS_ROLES)
        assert set(SC.STRUCTURES["strangle"].values()) == set(SS.PREREG["strangle_roles"])
        qs = cboe_options.select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        r = SS.quote_record(qs, "2026-09-03", None)
        assert r["available"] is True and r["call"]["strike"] > r["put"]["strike"], r

    def test_data_outcomes_through_the_real_ledger_never_alert(self, tmp_path, monkeypatch):
        """入场不可成交 / 平仓走完：走真实账本路径也是『数据』，errors = 0、不报 P2。"""
        import scan_timing
        _paths(tmp_path, monkeypatch, "data")
        _run(AS_OF, [_signal(qs=_qs())])                               # 夹具默认没有 25Δ 腿
        _run(EXIT_DAY, [], quotes_fn=exit_quotes())
        s = SS.stats()
        assert s["entry_unquotable"] == 1 and s["signal_missing_leg"] == 1 and s["errors"] == 0
        assert _closed_rows()[0]["shadow_strangle"]["status"] == "unquotable"
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a._check_strangle_shadow({"scan_timing": {"counters": scan_timing.counters()}})
        assert not a.alerts


class TestRevealAfterFreeze:
    """v0.45.599：冻结后自动解锁每笔配对；冻结前（或冻结文件坏了）一个字都不给。钥匙只有一把——冻结文件存在且完好。"""

    @pytest.fixture
    def three(self, monkeypatch):
        monkeypatch.setitem(SS.PREREG, "min_units", 3)
        monkeypatch.setitem(SS.PREREG, "min_blocks", 3)
        return [_closed_row(earnings_date=w, pnl=-100.0 * (i + 1), shadow_pnl=-100.0 * (i + 1) - 500.0 * (i + 1))
                for i, w in enumerate(("2026-10-28", "2026-11-04", "2026-11-11"))]

    def test_nothing_before_the_freeze_even_when_ready(self, tmp_path, three):
        assert SS.progress(three, root=tmp_path)["ready"] is True, "正对照：已就绪但还没冻结"
        assert SS.reveal(three, root=tmp_path) is None

    def test_frozen_result_and_hand_computed_pairs(self, tmp_path, three):
        SS.run_once(three, root=tmp_path, today="2026-12-01")
        rv = SS.reveal(three, root=tmp_path)
        assert rv["result"]["decision"] in ("reject_h0", "fail_to_reject_h0") and rv["result"]["n_units"] == 3
        assert rv["result"]["direction"] == "straddle_higher" and rv["result"]["ready_date"] == "2026-12-01"
        assert "unit_keys" not in rv["result"] and "progress" not in rv["result"]
        p = rv["pairs"]
        assert [x["earnings_date"] for x in p] == ["2026-10-28", "2026-11-04", "2026-11-11"]
        assert (p[1]["straddle_usd"], p[1]["strangle_usd"]) == (-200.0, -1200.0)                 # 第 2 笔：−100·2 与 −200−1000
        assert p[1]["d"] == pytest.approx((-1200.0 - -200.0) / 100_000.0) and p[1]["nav_for_sizing"] == 100_000.0
        assert (p[1]["straddle_contracts"], p[1]["strangle_contracts"]) == (None, 30) and all(x["in_frozen_test"] for x in p)

    def test_units_that_close_after_the_freeze_are_described_not_counted(self, tmp_path, three):
        SS.run_once(three, root=tmp_path)
        late = _closed_row(earnings_date="2026-11-18")
        late["ticker"] = "LATE"
        rv = SS.reveal(three + [late], root=tmp_path)
        assert [x["in_frozen_test"] for x in rv["pairs"]] == [True, True, True, False]
        assert rv["result"]["n_units"] == 3, "冻结的判定不因后来的单位改变"

    def test_only_ok_units_appear(self, tmp_path, three):
        SS.run_once(three, root=tmp_path)
        junk = [_closed_row(side="long"), _closed_row(status="unquotable"), _closed_row(exit_date="2026-10-28")]
        assert len(SS.reveal(three + junk, root=tmp_path)["pairs"]) == 3

    @pytest.mark.parametrize("content", ["null", "[]", "{not json", ""])
    def test_a_damaged_result_file_does_not_unlock(self, tmp_path, three, content):
        p = SS._result_path(tmp_path)
        p.parent.mkdir(parents=True)
        p.write_text(content, encoding="utf-8")
        assert SS.reveal(three, root=tmp_path) is None

    def test_alpha_bot_unlocks_only_with_the_frozen_file(self, tmp_path, three):
        from alphabot import synthetic as syn
        from tests.test_alphabot_straddle import AS_OF as BOT_AS_OF, _client, _keys, _svc, _write_ledger
        led = syn.demo_straddle_ledger(BOT_AS_OF)
        led["closed"] = three
        root = _write_ledger(tmp_path / "data", led)
        before = _client(_svc(root)).get("/api/straddle").json()["strangle_shadow"]
        assert before["ready"] is True and before["frozen"] is False and "revealed" not in before, "正对照：就绪但没冻结 ⇒ 不给"
        SS.run_once(three, root=root, today="2026-12-01")
        body = _client(_svc(root)).get("/api/straddle").json()
        after = body["strangle_shadow"]
        assert after["frozen"] is True and len(after["revealed"]["pairs"]) == 3 and after["revealed"]["result"]["n_units"] == 3
        assert "shadow_strangle" not in set(_keys(body)), "解锁的是配对，不是原始影子记录"

    def test_front_end_unlocks_only_on_the_revealed_key(self):
        js = (REPO / "alphabot" / "static" / "pages" / "straddle.js").read_text(encoding="utf-8")
        a, b = js.index("function strangleShadowPanel"), js.index("export async function renderStraddle")
        panel = "\n".join(ln for ln in js[a:b].splitlines() if not ln.lstrip().startswith("//"))
        assert "s.revealed ? revealedBlock(s.revealed) : null" in panel and "callout lock" in panel
        assert panel.count("revealedBlock(") == 1, "配对块只能经 s.revealed 这一条路进页面"
        assert "function revealedBlock" in js[:a], "配对块的代码在盲期面板之外（面板那段的『不含盈亏字眼』守卫才有意义）"


class TestProtocolConstantsArePinned:
    """决策常量写在测试里：谁改 α / 种子 / 置换次数 / 就绪闸 / start_date，必须同时改这里，也就是必须按协议 §8 记修订。
    （读源码里的字面量，而不是运行时的 PREREG——这个文件的 autouse 夹具把 start_date 钉到了 2026-01-01。）"""

    EXPECTED = {"version": 2, "registered": "2026-10-21", "start_date": "2026-10-21", "quote_schema": 1, "shadow_schema": 1,
                "strangle_roles": ("c25", "p25"), "min_units": 30, "min_blocks": 8, "alpha": 0.05, "enum_max_blocks": 16,
                "n_perm": 20000, "seed": 20261021, "statistic": "mean(d)", "test": "two_sided_block_sign_flip",
                "block": "earnings_iso_week"}

    def test_the_source_literals_match(self):
        tree = ast.parse((REPO / "straddle_strangle_shadow.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "PREREG" for t in n.targets))
        lit = ast.literal_eval(node.value)
        assert {k: lit[k] for k in self.EXPECTED} == self.EXPECTED

    def test_the_protocol_text_names_the_same_numbers(self):
        doc = (REPO / "experiments" / "straddle_strangle_shadow_prereg.md").read_text(encoding="utf-8")
        for needle in ("2026-10-21", "20261021", "20000", "α = 0.05", "≥ 30", "≥ 8", "修订 2"):
            assert needle in doc, needle


class TestPointInTimeBoundaries:
    def test_the_event_day_itself_is_not_history(self):
        got = SS.prior_moves([{"earnings_date": "2026-09-03", "move_pct": 5.0}, {"earnings_date": "2026-09-02", "move_pct": -3.0}],
                             "2026-09-03")
        assert [m["earnings_date"] for m in got] == ["2026-09-02"]
        assert SS.prior_moves([{"earnings_date": "2026-09-03", "move_pct": 5.0}], "2026-09-03") == SC.prior_moves(
            [{"earnings_date": "2026-09-03", "move_pct": 5.0}], "2026-09-03")

    def test_blocks_are_iso_weeks_across_the_year_boundary(self):
        assert SS._block("2026-12-31") == "2026-W53" and SS._block("2027-01-01") == "2026-W53"      # 日历年变了，ISO 周没变
        assert SS._block("2024-12-30") == "2025-W01"
        assert SS._block("2027-01-04") == "2027-W01" and SS._block("garbage") is None
