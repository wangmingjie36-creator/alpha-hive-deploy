"""v0.45.433 期权纸面腿：内在价值结算只认结算那一场的 Twelve Data 收盘，取不到就延期（不拿更早的收盘顶替）。

旧实现（v0.45.101~432）`_default_close` 取「≤ 结算日、5 个日历日内最后一根」，Twelve Data 没给再退本地价格索引。
本源当晚不给当日那根（`twelve_data._drop_forming_bar` 规则 ①，v0.45.438），于是到期当晚跑会**静默**按前一交易日
收盘结算，什么都不留。

写法约束（变异检验 M0 拿改动前的 options_paper_leg.py 跑这些测试）
-----------------------------------------------------------------
行为测试只走改动前就有的入口：`run_for_date` 的**默认**取收盘（不注入 closes_fn）。桩只打在数据边界——
`twelve_data._fetch_rows` / `is_configured` / `_et_today`、`price_history.load_price_history`；`fetch_bars` 的缓存层是真的。
每条行为测试的**第一条**断言是行为（仓位还在 / 结算价是哪一场的），所以在旧文件上红的理由就是「按旧收盘结了」，
不是「新字段不存在」。不依赖 `twelve_data._et_now` / `_session_settled`（v0.45.438 已删）。
"""

import json
import logging
from types import SimpleNamespace

import pytest

import options_paper_leg as opl
import price_history
import twelve_data as td
from is_trading_day import is_trading_day
from datetime import date, timedelta

TK = "XYZ"
ENTRY, EARN, EXPIRY = "2026-09-14", "2026-10-13", "2026-10-16"     # 周一 / 周二 / 周五
CALL, PUT = "XYZ261016C00100000", "XYZ261016P00100000"
PREV = "2026-10-15"                                                  # 结算场次的前一交易日
FINAL = {PREV: 103.0, EXPIRY: 107.0}                                 # 定稿收盘（与基线序列明显不同）
PROVISIONAL_EXPIRY = 106.4                                           # 到期当晚本源给的临时值


def _trading_days(start, end):
    d, out = date.fromisoformat(start), []
    while d <= date.fromisoformat(end):
        if is_trading_day(d)[0]:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _none_quotes(ticker, symbols):
    return {s: None for s in symbols}


@pytest.fixture
def world(tmp_path, monkeypatch):
    sd = tmp_path / "options_paper_state"
    for name, val in (("STATE_DIR", sd), ("POSITIONS_FILE", sd / "positions.jsonl"),
                      ("CLOSED_FILE", sd / "closed_trades.jsonl"), ("EQUITY_FILE", sd / "equity_curve.jsonl"),
                      ("META_FILE", sd / "meta.json")):
        monkeypatch.setattr(opl, name, val)
    monkeypatch.setitem(opl.CONFIG, "starting_capital", 20_000.0)

    w = SimpleNamespace(published=PREV, skip=set(), vol={}, close={}, provisional={}, real_rule=False,
                        fetches=[], index_calls=[], index={}, configured=True, down=False)

    def series():
        rows = []
        for i, d in enumerate(_trading_days("2026-05-01", "2027-07-30")):
            if d > w.published or d in w.skip:
                continue
            c = w.provisional.get(d, w.close.get(d, FINAL.get(d, round(90.0 + 0.01 * i, 2))))
            rows.append({"date": d, "close": c, "vol": w.vol.get(d, 1e6)})
        return rows

    def fetch_rows(ticker, days, end_date=None):
        w.fetches.append((ticker, end_date))
        if w.down:
            return None
        rows = [r for r in series() if end_date is None or r["date"] <= end_date][-max(days, 10):]
        return td._drop_forming_bar(rows, ticker) if w.real_rule else rows

    def load_index(ticker, cache_dir, *a, **k):
        w.index_calls.append(ticker)
        return sorted(w.index.items())

    monkeypatch.setattr(td, "is_configured", lambda: w.configured)
    monkeypatch.setattr(td, "_fetch_rows", fetch_rows)
    monkeypatch.setattr(price_history, "load_price_history", load_index)
    w.et_today = lambda d: monkeypatch.setattr(td, "_et_today", lambda: d)
    w.et_today("2099-01-01")                      # 缺省：所有日期都已是过去（规则 ① 不生效）

    def book(expiry=EXPIRY, last_mark_date="2026-10-08"):
        sd.mkdir(parents=True, exist_ok=True)
        opl.POSITIONS_FILE.write_text(json.dumps({
            "ticker": TK, "side": "long", "entry_date": ENTRY, "expiry": expiry, "strike": 100.0,
            "call_symbol": CALL, "put_symbol": PUT, "contracts": 1, "entry_call": 1.1, "entry_put": 1.1,
            "entry_premium": 2.2, "entry_underlying": 100.0, "earnings_date": EARN, "signal_ratio": 0.5,
            "label": "cheap", "size_usd": 220.0, "last_mark": 2.0, "last_mark_date": last_mark_date,
            "mark_source": "cboe_mid", "rationale": "test", "stale_days": 0}) + "\n", encoding="utf-8")
        opl.META_FILE.write_text(json.dumps({"cash": 19_780.0, "starting_capital": 20_000.0,
                                             "starting_date": ENTRY}), encoding="utf-8")
    w.book = book

    def run(as_of, caplog=None):
        td.clear_bars_cache()
        return opl.run_for_date(as_of, quotes_fn=_none_quotes, signals=[])
    w.run = run

    td.clear_bars_cache()
    yield w
    td.clear_bars_cache()


def _opl_records(caplog, level):
    return [r for r in caplog.records if r.name.endswith("options_paper_leg") and r.levelno == level]


# ── 用户点名的那一个：本源只到结算日前一天 ⇒ 旧代码按那一根结算 ──────────────────

class TestNoOlderCloseSubstitutes:

    def test_expired_position_defers_instead_of_settling_at_the_previous_close(self, world, caplog):
        world.book()
        world.published = PREV                       # Twelve Data 只到 10-15
        with caplog.at_level(logging.INFO):
            r = world.run("2026-10-19")
        assert r["closed_today"] == [], f"按旧收盘结了：{r['closed_today']}"
        assert [p["ticker"] for p in r["positions"]] == [TK]
        d = r["settle_deferred"]
        assert len(d) == 1 and d[0]["session"] == EXPIRY and d[0]["reason"] == "lagging"
        assert d[0]["seen_session"] == PREV and d[0]["seen_price"] == FINAL[PREV]
        assert d[0]["write_off_in_days"] == 28                       # 核销在 past_expiry 31（11-16）
        p = r["positions"][0]
        assert p["settle_deferred_since"] == "2026-10-19" and EXPIRY in p["settle_deferred_reason"]
        warns = [x.getMessage() for x in _opl_records(caplog, logging.WARNING)]
        assert any(TK in m and EXPIRY in m and "延期" in m for m in warns), warns

    def test_pre_expiry_stale_is_neither_settled_nor_deferred(self, world):
        """v0.45.449：未到期报价取不到 ⇒ 只等报价 / 等到期，不按内在价值平。v0.45.433 在这里结算日 = as_of，
        生产当晚恒无那一根 ⇒ 天天延期；午夜后补跑恰有那一根 ⇒ 按内在价值（低估时间价值）平了。"""
        world.book(expiry="2026-10-23")
        for as_of, published in (("2026-10-16", PREV), ("2026-10-19", "2026-10-19"), ("2026-10-20", "2026-10-20")):
            world.published = published
            r = world.run(as_of)
            assert r["closed_today"] == [], f"{as_of} 未到期按内在价值平了：{r['closed_today']}"
            assert r["settle_deferred"] == [] and r["positions"][0]["settle_deferred_since"] is None, as_of
        world.published = "2026-10-26"
        r = world.run("2026-10-26")                                 # 到期后：按 10-23 那一场结算
        assert r["closed_today"][0]["exit_underlying"] != FINAL[EXPIRY]
        assert "session=2026-10-23 (twelve_data)" in r["closed_today"][0]["rationale"]

    def test_hole_in_the_series_defers(self, world):
        """该场之后的日线都有了，偏偏缺这一根 ⇒ bar_missing，不拿前一根。"""
        world.book()
        world.published, world.skip = "2026-10-20", {EXPIRY}
        r = world.run("2026-10-21")
        assert r["closed_today"] == [], f"按旧收盘结了：{r['closed_today']}"
        assert r["settle_deferred"][0]["reason"] == "bar_missing"

    @pytest.mark.parametrize("how", ["down", "unconfigured"])
    def test_local_price_index_is_never_a_settlement_source(self, world, how):
        """Twelve Data 取不到 / 没配：旧代码退本地价格索引（期权快照价，分不清盘中还是收盘）。"""
        world.book()
        world.published = EXPIRY
        world.index = {EXPIRY: 150.0}
        setattr(world, "down" if how == "down" else "configured", how == "down")
        r = world.run("2026-10-19")
        assert r["closed_today"] == [], f"用本地价格索引结了：{r['closed_today']}"
        assert world.index_calls == []
        assert r["settle_deferred"][0]["reason"] == (
            "bars_unavailable" if how == "down" else "twelve_data_unconfigured")


# ── 到期当晚（真的规则 ①）→ 次日按定稿那根结算 ───────────────────────────────

class TestExpiryEveningThenNextDay:

    def test_defer_on_expiry_evening_then_settle_at_the_final_expiry_close(self, world, caplog):
        world.book()
        world.real_rule = True
        world.published, world.provisional = EXPIRY, {EXPIRY: PROVISIONAL_EXPIRY}
        world.et_today(EXPIRY)                     # 到期当晚：本源的当日那根是临时值，规则 ① 丢它
        with caplog.at_level(logging.INFO):
            r = world.run(EXPIRY)
        assert r["closed_today"] == [], f"到期当晚就结了：{r['closed_today']}"
        assert r["settle_deferred"][0]["reason"] == "lagging"

        world.published, world.provisional = "2026-10-19", {"2026-10-19": 111.0}
        world.et_today("2026-10-19")               # 下一个运行日：10-16 那根已定稿
        with caplog.at_level(logging.INFO):
            r = world.run("2026-10-19")
        t = r["closed_today"][0]
        assert t["exit_underlying"] == FINAL[EXPIRY] and t["exit_premium"] == pytest.approx(7.0)
        assert t["mark_source"] == "intrinsic" and t["exit_date"] == "2026-10-19"
        assert t["holding_days"] == 32 and t["settled_late_days"] == 3
        assert f"session={EXPIRY} (twelve_data)" in t["rationale"] and f"deferred since {EXPIRY}" in t["rationale"]
        assert r["settle_deferred"] == [] and r["positions"] == []
        assert _opl_records(caplog, logging.ERROR) == [], "设计内的次日结算不该报 ERROR"

    def test_window_ends_at_as_of_so_a_low_volume_expiry_bar_still_settles(self, world):
        """截至结算日取，到期那根永远是末根，低量（提前收盘日）就被规则 ② 永远丢掉；截至 as_of 取则在窗口中间。"""
        world.book()
        world.real_rule, world.published, world.vol = True, "2026-10-19", {EXPIRY: 1e5}
        r = world.run("2026-10-19")
        assert r["closed_today"] and r["closed_today"][0]["exit_underlying"] == FINAL[EXPIRY]

    def test_long_gap_window_still_reaches_the_expiry_session(self, world):
        """长时间没跑：截至 as_of 的 120 根窗口第一根已晚于到期日 ⇒ 按结算场次补取；否则误报 bar_missing、直接核销。"""
        world.book()
        world.published = "2027-06-01"
        r = world.run("2027-06-01")
        t = r["closed_today"][0]
        assert t["mark_source"] == "intrinsic" and t["exit_underlying"] == FINAL[EXPIRY], t
        assert world.fetches == [(TK, "2027-06-01"), (TK, EXPIRY)]

    def test_one_fetch_shared_with_the_other_consumers(self, world):
        world.book()
        world.published = "2026-10-19"
        world.run("2026-10-19")
        td.fetch_bars(TK, td.SHARED_BARS_WINDOW, end_date="2026-10-19")   # vrp / 组合 Greeks 的取法
        assert len(world.fetches) == 1, world.fetches


# ── 正对照：旧文件上也必须绿 ─────────────────────────────────────────────────

class TestControls:

    def test_exact_session_close_settles(self, world):
        world.book()
        world.published = EXPIRY
        r = world.run("2026-10-19")
        assert r["closed_today"][0]["exit_underlying"] == FINAL[EXPIRY]

    @pytest.mark.parametrize("expiry,session,as_of", [("2026-10-17", "2026-10-16", "2026-10-19"),
                                                      ("2026-11-26", "2026-11-25", "2026-11-27")])
    def test_non_trading_expiry_uses_the_previous_session(self, world, expiry, session, as_of):
        """到期日落在周六（老式 OCC 到期日）/ 感恩节：按之前最近一个交易日那一场结算。"""
        assert not is_trading_day(date.fromisoformat(expiry))[0]
        world.book(expiry=expiry)
        world.published, world.close = as_of, {session: 104.0}
        r = world.run(as_of)
        t = r["closed_today"][0]
        assert t["exit_underlying"] == 104.0 and t["exit_date"] == as_of


# ── 延期与核销同一条时间线；状态与日报 ───────────────────────────────────────

class TestDeferralLifecycle:

    def test_write_off_after_horizon_names_the_missing_session(self, world):
        world.book()
        world.published = PREV
        r = world.run("2026-10-19")
        assert r["closed_today"] == []
        r = world.run("2026-11-15")                                   # past_expiry 30：还等
        assert r["closed_today"] == [] and r["settle_deferred"][0]["write_off_in_days"] == 1
        assert r["positions"][0]["settle_deferred_since"] == "2026-10-19"
        r = world.run("2026-11-16")                                   # 31：核销
        t = r["closed_today"][0]
        assert t["mark_source"] == "written_off" and t["exit_premium"] == pytest.approx(2.0)
        assert f"no close for session {EXPIRY} (lagging, deferred since 2026-10-19)" in t["rationale"]
        assert "NOT a traded price" in t["rationale"]

    def test_same_day_rerun_is_byte_identical(self, world):
        world.book()
        r = world.run("2026-10-19")
        assert r["closed_today"] == []
        snap = (opl.POSITIONS_FILE.read_text(), opl.EQUITY_FILE.read_text())
        world.run("2026-10-19")
        assert (opl.POSITIONS_FILE.read_text(), opl.EQUITY_FILE.read_text()) == snap

    def test_deferral_ends_when_the_close_arrives(self, world):
        world.book()
        r = world.run("2026-10-19")
        assert r["settle_deferred"][0]["since"] == "2026-10-19"
        world.published = "2026-10-20"
        r = world.run("2026-10-20")
        assert r["closed_today"][0]["exit_underlying"] == FINAL[EXPIRY] and r["settle_deferred"] == []
        assert "deferred since 2026-10-19" in r["closed_today"][0]["rationale"]

    def test_markdown_shows_the_deferral(self, world):
        world.book()
        r = world.run("2026-10-19")
        assert r["closed_today"] == []
        md = opl.render_markdown("2026-10-19")
        assert "⏳ 结算延期（自 2026-10-19）" in md
        assert f"缺 {EXPIRY} 那一场的收盘（lagging）" in md and str(FINAL[PREV]) in md


class TestResolveClose:

    @pytest.mark.parametrize("raw,price,reason", [
        (107.0, 107.0, None),                                                     # 裸数字 = 调用方担保
        ({"price": 107.0, "session": EXPIRY, "source": "twelve_data"}, 107.0, None),
        ({"price": 103.0, "session": PREV, "source": "twelve_data"}, None, "wrong_session"),
        ({"price": 103.0, "session": PREV, "reason": "lagging"}, None, "lagging"),
        (None, None, "no_close"),
        (float("nan"), None, "no_close"),
    ])
    def test_only_the_settlement_session_is_usable(self, raw, price, reason):
        out = opl._resolve_close(raw, EXPIRY)
        assert (out["price"], out["reason"]) == (price, reason)


def test_defer_text_never_prints_none():
    txt = opl._defer_text({"reason": "lagging", "seen_session": PREV, "seen_price": None}, EXPIRY)
    assert "None" not in txt and "收盘无效" in txt
    txt = opl._defer_text({"reason": "lagging", "seen_session": PREV, "seen_price": 103.0}, EXPIRY)
    assert f"{PREV} 收 103.0" in txt
