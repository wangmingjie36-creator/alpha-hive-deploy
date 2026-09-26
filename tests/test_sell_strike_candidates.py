"""卖权行权价选择器 · 取数视图与候选构造层守卫（v0.45.333）。

覆盖 `cboe_options.fetch_cboe_raw_contracts`（注入 payload，不出网）与 `sell_strike_candidates`。
**纪律。** 每条断言的 docstring 写明什么变异会让它变红。夹具全是内存 dict，无 skip。
"""

import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import cboe_options as C
import sell_strike_candidates as SC

ET = ZoneInfo("America/New_York")
AFTER_CLOSE = datetime(2026, 9, 23, 17, 5, tzinfo=ET)      # 周三 14:05 PT 扫描时刻
INTRADAY = datetime(2026, 9, 23, 11, 0, tzinfo=ET)


# ───────────────────────────── payload 夹具

def _occ(expiry, cp, K, root="XYZ"):
    return f"{root}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{cp}{int(round(K * 1000)):08d}"


def _row(expiry, cp, K, **kw):
    r = {"option": _occ(expiry, cp, K), "iv": 0.30, "delta": (0.3 if cp == "C" else -0.3),
         "gamma": 0.02, "vega": 0.1, "theta": -0.05, "theo": 1.0, "open_interest": 100,
         "volume": 10, "bid": 1.0, "ask": 1.1, "last_trade_time": "2026-09-23T15:59:00"}
    r.update(kw)
    return r


def _payload(rows, last_trade="2026-09-23T16:00:00", close=100.0, current=101.0):
    p = {"symbol": "XYZ", "close": close, "current_price": current, "options": rows}
    if last_trade is not None:
        p["last_trade_time"] = last_trade
    return p


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(C, "_SNAPSHOT_PROVIDER", None)
    C.reset_raw_contracts_stats()
    C.reset_payload_stats()          # official_price 按 symbol 记陈旧 / 判不了（进程级集合），别漏给别的文件
    yield
    C.reset_raw_contracts_stats()
    C.reset_payload_stats()


def _serve(monkeypatch, payload):
    """把 `_fetch_cboe_payload` 换成返回给定 payload 的假货；记下 on_stale 参数。"""
    seen = {}

    def fake(ticker, timeout, *, retries=3, on_stale="none"):
        seen["on_stale"] = on_stale
        return payload

    monkeypatch.setattr(C, "_fetch_cboe_payload", fake)
    return seen


# ───────────────────────────── 1. 日历 DTE

def test_raw_contracts_use_pure_calendar_dte(monkeypatch):
    """as_of=2026-09-23、17:05 ET 抓取、到期 2026-09-30 ⇒ dte==7，且落进周度窗口。

    变红的变异：用带时分秒的 now 相减（`(datetime(expiry) − now).days`）⇒ 6，掉出周度窗口。
    """
    seen = _serve(monkeypatch, _payload([_row("2026-09-30", "P", 95.0), _row("2026-09-30", "C", 105.0)]))
    res, why = C.fetch_cboe_raw_contracts("XYZ", as_of="2026-09-23", now_et=AFTER_CLOSE)
    assert why is None
    assert seen["on_stale"] == "raise"
    assert {c["dte"] for c in res["contracts"]} == {7}
    assert SC.select_expiry(res["contracts"], "weekly") == ("2026-09-30", 7)
    assert res["underlying_price"] == 100.0 and res["underlying_price_source"] == "cboe_close"
    assert res["as_of"] == res["vintage_date"] == "2026-09-23"
    assert C.raw_contracts_stats()["ok"] == 1


# ───────────────────────────── 2. 收盘后当天到期

def test_expiring_today_dropped_after_close_kept_intraday(monkeypatch):
    """收盘后 dte==0 的合约已结算 ⇒ 排除并计 n_expiring_today_excluded；盘中还活着 ⇒ 保留。
    顺带：已到期（dte<0）与 OCC 解析失败各自计数。

    变红的变异：去掉 dte==0 排除（收盘后 0.5 天下限的 gamma 会独占近月视图）；
    或不看 is_market_open 一律排除（盘中也丢）；或把三种丢弃折进一个计数。
    """
    rows = [_row("2026-09-23", "C", 100.0), _row("2026-09-30", "C", 105.0),
            _row("2026-09-22", "P", 95.0), {"option": "GARBAGE", "iv": 0.3}]
    _serve(monkeypatch, _payload(rows))
    res, _ = C.fetch_cboe_raw_contracts("XYZ", as_of="2026-09-23", now_et=AFTER_CLOSE)
    assert [c["expiry"] for c in res["contracts"]] == ["2026-09-30"]
    assert (res["n_raw"], res["n_expiring_today_excluded"], res["n_expired_excluded"],
            res["n_dropped_unparseable"]) == (4, 1, 1, 1)

    _serve(monkeypatch, _payload(rows, last_trade="2026-09-23T10:44:00"))
    res2, _ = C.fetch_cboe_raw_contracts("XYZ", as_of="2026-09-23", now_et=INTRADAY)
    assert sorted(c["dte"] for c in res2["contracts"]) == [0, 7]
    assert res2["n_expiring_today_excluded"] == 0
    assert res2["underlying_price_source"] == "cboe_intraday"


def test_numeric_cleaning_zero_greeks_are_none_and_missing_oi_is_zero(monkeypatch):
    """CBOE 对零流动合约给 delta/gamma=0、iv=0 —— 不是观测值 ⇒ None；oi 缺失 ⇒ 0.0；非有限 bid ⇒ None。

    变红的变异：`_qs_num` 原样透传 0（下游把 0 当 gamma 求和、把 0 当 Δ 选档）；oi 缺失给 None。
    """
    row = _row("2026-10-23", "P", 90.0, delta=0, gamma=0.0, iv=0, bid="nan", ask=None)
    row.pop("open_interest")
    _serve(monkeypatch, _payload([row]))
    res, _ = C.fetch_cboe_raw_contracts("XYZ", as_of="2026-09-23", now_et=AFTER_CLOSE)
    c = res["contracts"][0]
    assert (c["delta"], c["gamma"], c["iv"], c["oi"], c["bid"], c["ask"]) == (None, None, None, 0.0, None, None)
    assert (c["cp"], c["strike"], c["dte"]) == ("P", 90.0, 30)


# ───────────────────────────── 3. 失败原因分开计数

def _stale_fake(ticker, timeout, *, retries=3, on_stale="none"):
    # 只在调用方显式要求时抛；否则同其它失败一起 return None（真实 `_fetch_cboe_payload` 的契约）
    if on_stale == "raise":
        raise C.CboeStaleVintageError(ticker, "2026-09-22", "2026-09-23")
    return None


@pytest.mark.parametrize("case,reason,key", [
    ("stale", "stale_vintage", "stale_vintage"),
    ("none", "payload_unavailable", "payload_unavailable"),
    ("empty_options", "payload_unavailable", "payload_unavailable"),
    ("mismatch", "vintage_mismatch", "vintage_mismatch"),
    ("unverifiable", "vintage_unverifiable", "vintage_unverifiable"),
    ("snapshot", "snapshot_mode_no_raw_chain", "snapshot_mode"),
    ("no_price", "price_unavailable", "price_unavailable"),
    ("all_dropped", "no_parseable_contracts", "no_parseable_contracts"),
])
def test_each_failure_has_its_own_reason_and_counter(monkeypatch, case, reason, key):
    """五种主要失败（陈旧 / 取不到 / vintage 对不上 / 判不了 vintage / 快照模式）加两种次要失败，
    原因字符串与计数器**各自** +1，其余计数器不动。

    变红的变异：把任意两种折成一个出口（例如 unverifiable 也报 vintage_mismatch）；
    `on_stale` 不传 "raise"（陈旧会落成 payload_unavailable）；给快照模式回落实时抓取。
    """
    good = [_row("2026-10-23", "P", 90.0)]
    if case == "stale":
        monkeypatch.setattr(C, "_fetch_cboe_payload", _stale_fake)
    elif case == "none":
        _serve(monkeypatch, None)
    elif case == "empty_options":
        _serve(monkeypatch, _payload([]))
    elif case == "mismatch":
        _serve(monkeypatch, _payload(good, last_trade="2026-09-22T16:00:00"))
    elif case == "unverifiable":
        _serve(monkeypatch, _payload(good, last_trade=None))
    elif case == "snapshot":
        monkeypatch.setattr(C, "_SNAPSHOT_PROVIDER", lambda t: {"price_at_fetch": 100.0})
        monkeypatch.setattr(C, "_fetch_cboe_payload",
                            lambda *a, **k: pytest.fail("快照模式不许回落实时抓取"))
    elif case == "no_price":
        _serve(monkeypatch, _payload(good, close=None))
    elif case == "all_dropped":
        _serve(monkeypatch, _payload([_row("2026-09-22", "P", 90.0)]))
    res, why = C.fetch_cboe_raw_contracts("XYZ", as_of="2026-09-23", now_et=AFTER_CLOSE)
    assert res is None and why == reason
    stats = C.raw_contracts_stats()
    assert stats[key] == 1
    assert sum(stats.values()) == 1, f"只有 {key} 该 +1：{stats}"


def test_weekday_holiday_is_not_a_live_session(monkeypatch):
    """劳动节 2026-09-07（周一）11:00 ET、payload 是上一交易日 09-04 的：那一场早收了。
    ⇒ 09-04 到期的合约（dte=0）被排除；现价取 close（100.0）而不是盘后 current_price（101.7），
    标签是 cboe_close 而不是 cboe_intraday；session_live=False。
    正对照：同一 payload 在 09-04 当天 11:00（场次进行中）⇒ dte=0 保留、取 current_price。

    变红的变异：`session_live = is_market_open(now)`（只看工作日 + 钟点、不看 vintage 是不是今天）
    ⇒ 休市日判成盘中，09-04 那张以 dte=0 留下、S=101.7 标 cboe_intraday（评审探针实测的形状）；
    或 not live 时仍按 now 取价（S 对了但陈旧判据按「今天」算——见下一条的 stale 用例）。
    """
    from datetime import date as _date
    from is_trading_day import is_trading_day
    assert is_trading_day(_date(2026, 9, 7))[0] is False, "夹具自检：09-07 是休市日"
    assert C.is_market_open(datetime(2026, 9, 7, 11, 0, tzinfo=ET)), "夹具自检：只看钟点会判盘中"

    rows = [_row("2026-09-04", "C", 100.0), _row("2026-09-11", "C", 100.0)]
    _serve(monkeypatch, _payload(rows, last_trade="2026-09-04T16:00:00", close=100.0, current=101.7))
    res, why = C.fetch_cboe_raw_contracts("XYZ", as_of=None, now_et=datetime(2026, 9, 7, 11, 0, tzinfo=ET))
    assert why is None and res["as_of"] == "2026-09-04"
    assert [(c["expiry"], c["dte"]) for c in res["contracts"]] == [("2026-09-11", 7)]
    assert res["n_expiring_today_excluded"] == 1
    assert (res["underlying_price"], res["underlying_price_source"]) == (100.0, "cboe_close")
    assert res["session_live"] is False

    _serve(monkeypatch, _payload(rows, last_trade="2026-09-04T10:44:00", close=100.3, current=101.7))
    live, _ = C.fetch_cboe_raw_contracts("XYZ", as_of=None, now_et=datetime(2026, 9, 4, 11, 0, tzinfo=ET))
    assert sorted(c["dte"] for c in live["contracts"]) == [0, 7]
    assert (live["underlying_price"], live["underlying_price_source"]) == (101.7, "cboe_intraday")
    assert live["session_live"] is True


def test_early_close_day_after_the_bell_is_not_a_live_session(monkeypatch):
    """感恩节次日 2026-11-27 13:00 提前收盘；14:00 ET 读当天（13:00 收盘时生成）的 payload：
    那一场已收 ⇒ session_live=False、当天到期合约（dte=0）被排除、S 取 close（100.0）标 cboe_close。
    对照：普通交易日 2026-11-25 14:00 ET 读当天盘中文件 ⇒ session_live=True、dte=0 保留、取 current_price。

    变红的变异：收盘钟点写死 16:00（`is_market_open(now) and vintage == 今天`，2026-09-24 最终评审 R5
    的原形状）⇒ 14:00 判盘中，11-27 那张以 dte=0 留下、S=101.7 标 cboe_intraday。
    """
    from datetime import date as _date, time as _time
    from is_trading_day import is_trading_day, session_close_et
    assert is_trading_day(_date(2026, 11, 27))[0] is True, "夹具自检：11-27 是交易日"
    assert session_close_et(_date(2026, 11, 27)) == _time(13, 0), "夹具自检：11-27 13:00 收盘"
    assert C.is_market_open(datetime(2026, 11, 27, 14, 0, tzinfo=ET)), "夹具自检：只看钟点会判盘中"

    rows = [_row("2026-11-27", "C", 100.0), _row("2026-11-27", "P", 100.0), _row("2026-12-04", "C", 100.0)]
    _serve(monkeypatch, _payload(rows, last_trade="2026-11-27T13:00:00", close=100.0, current=101.7))
    res, why = C.fetch_cboe_raw_contracts("XYZ", as_of=None, now_et=datetime(2026, 11, 27, 14, 0, tzinfo=ET))
    assert why is None and res["as_of"] == "2026-11-27"
    assert res["session_live"] is False
    assert [(c["expiry"], c["dte"]) for c in res["contracts"]] == [("2026-12-04", 7)]
    assert res["n_expiring_today_excluded"] == 2
    assert (res["underlying_price"], res["underlying_price_source"]) == (100.0, "cboe_close")

    rows2 = [_row("2026-11-25", "C", 100.0), _row("2026-12-04", "C", 100.0)]
    _serve(monkeypatch, _payload(rows2, last_trade="2026-11-25T13:44:00", close=100.3, current=101.7))
    live, _ = C.fetch_cboe_raw_contracts("XYZ", as_of=None, now_et=datetime(2026, 11, 25, 14, 0, tzinfo=ET))
    assert live["session_live"] is True
    assert sorted(c["dte"] for c in live["contracts"]) == [0, 9]
    assert (live["underlying_price"], live["underlying_price_source"]) == (101.7, "cboe_intraday")


def test_early_close_falls_back_to_regular_close_when_calendar_unavailable(monkeypatch):
    """日历（`is_trading_day.session_close_et`）不可用 ⇒ 退回平日 16:00 收盘，与 `_generated_mid_session`
    同口径：普通交易日 14:00 仍判盘中、15:59 盘中、16:00 已收；不抛。

    变红的变异：退路写成「判不了就不在场次中」或不接异常（抛出去）。
    """
    import is_trading_day as ITD

    def boom(d):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(ITD, "session_close_et", boom)
    rows = [_row("2026-11-25", "C", 100.0), _row("2026-12-04", "C", 100.0)]
    _serve(monkeypatch, _payload(rows, last_trade="2026-11-25T13:44:00", close=100.3, current=101.7))
    for hh, mm, want in ((14, 0, True), (15, 59, True), (16, 0, False)):
        res, why = C.fetch_cboe_raw_contracts("XYZ", as_of=None, now_et=datetime(2026, 11, 25, hh, mm, tzinfo=ET))
        assert why is None, (hh, mm, why)
        assert res["session_live"] is want, (hh, mm)


def test_not_live_prices_against_the_payload_session_not_today(monkeypatch):
    """不在场次中时，取价时钟是 **payload 那一场**收盘后，不是此刻：payload 是 09-04 盘中 12:10 生成的，
    09-07（休市日）盘中去读 ⇒ 那一场已收、文件是盘中生成的 ⇒ 标 cboe_stale_intraday。

    变红的变异：not live 时仍用 now 取价 —— now=09-07 11:00 被 is_market_open 判成盘中 ⇒
    取 current_price 标 cboe_intraday（把一份三天前的盘中价说成此刻实时价）。
    """
    rows = [_row("2026-09-11", "C", 100.0)]
    _serve(monkeypatch, _payload(rows, last_trade="2026-09-04T12:10:00", close=99.5, current=101.7))
    res, why = C.fetch_cboe_raw_contracts("XYZ", as_of=None, now_et=datetime(2026, 9, 7, 11, 0, tzinfo=ET))
    assert why is None
    assert (res["underlying_price"], res["underlying_price_source"]) == (99.5, C.STALE_INTRADAY_SOURCE)


def test_payload_timeliness_fields_are_passed_through(monkeypatch):
    """`payload_last_trade_time` = payload 自带的 last_trade_time 原文；`iv30` = payload 的 iv30
    （**百分数**原样，32.822 不是 0.32822）；`fetched_at` = 调用时刻（now）。缺 iv30 ⇒ None。

    变红的变异：不透传（ledger 的 iv30 恒 None、MCP 看不出报价是几点的）；iv30 除以 100；
    fetched_at 被当成 payload 时间。
    """
    rows = [_row("2026-09-30", "P", 95.0)]
    p = _payload(rows, last_trade="2026-09-23T15:59:58")
    p["iv30"] = 32.822
    _serve(monkeypatch, p)
    res, _ = C.fetch_cboe_raw_contracts("XYZ", as_of="2026-09-23", now_et=AFTER_CLOSE)
    assert res["payload_last_trade_time"] == "2026-09-23T15:59:58"
    assert res["iv30"] == 32.822
    assert res["fetched_at"] == AFTER_CLOSE.isoformat()
    _serve(monkeypatch, _payload(rows))
    res2, _ = C.fetch_cboe_raw_contracts("XYZ", as_of="2026-09-23", now_et=AFTER_CLOSE)
    assert res2["iv30"] is None and res2["payload_last_trade_time"] == "2026-09-23T16:00:00"


def test_as_of_none_takes_payload_vintage(monkeypatch):
    """MCP 现算（as_of=None）以 payload vintage 为 as_of；给了 as_of 则必须相等。

    变红的变异：as_of=None 时拿本机日期 / 报 vintage_mismatch。
    """
    _serve(monkeypatch, _payload([_row("2026-09-30", "P", 95.0)], last_trade="2026-09-22T16:00:00"))
    res, why = C.fetch_cboe_raw_contracts("XYZ", as_of=None, now_et=AFTER_CLOSE)
    assert why is None and res["as_of"] == "2026-09-22"
    assert res["contracts"][0]["dte"] == 8


# ───────────────────────────── 4–5. 到期日窗口

def _exp_chain(dtes):
    return [{"expiry": f"E{d:03d}", "cp": "P", "strike": 90.0, "dte": d} for d in dtes]


def test_windows_are_half_open_weekly_and_closed_monthly():
    """周度 [7,21)、月度 [21,45]：dte=21 只属于月度。

    变红的变异：两档都用闭区间（dte=21 同时落进两档，两本账共享到期日）。
    """
    assert SC.select_expiry(_exp_chain([21]), "monthly") == ("E021", 21)
    assert SC.select_expiry(_exp_chain([21]), "weekly") is None
    assert SC.in_tenor(7, "weekly") and not SC.in_tenor(6, "weekly")
    assert SC.in_tenor(45, "monthly") and not SC.in_tenor(46, "monthly")
    assert not SC.in_tenor(20, "monthly") and SC.in_tenor(20, "weekly")
    with pytest.raises(ValueError):
        SC.select_expiry(_exp_chain([30]), "montly")


def test_select_expiry_nearest_target_ties_go_farther():
    """|dte − target| 最小；平手取更远（同 select_quote_set：更远的 theta 更平缓）。

    变红的变异：平手取更近；不看 target 直接取最近到期日。
    """
    assert SC.select_expiry(_exp_chain([28, 32]), "monthly") == ("E032", 32)
    assert SC.select_expiry(_exp_chain([22, 29, 44]), "monthly") == ("E029", 29)
    assert SC.select_expiry(_exp_chain([12, 16]), "weekly") == ("E016", 16)
    assert SC.select_expiry(_exp_chain([8, 13]), "weekly") == ("E013", 13)


# ───────────────────────────── 6. 梯子

def _c(cp, K, delta, iv=0.30, bid=1.0, ask=1.1, dte=30, expiry="2026-10-23", oi=100.0):
    return {"symbol": f"XYZ{cp}{K}", "expiry": expiry, "cp": cp, "strike": float(K), "dte": dte,
            "iv": iv, "delta": delta, "gamma": 0.02, "theta": -0.05, "oi": oi, "bid": bid, "ask": ask}


def test_ladder_respects_tolerance_and_one_contract_per_rung():
    """|Δ| 离目标 > DELTA_TOL 不填；同一张合约不得占两档（后占的 None + `same_contract_as_<先占的档>`）。
    88（Δ=−0.19）同时是 0.16 与 0.20 的最近者 ⇒ 主档 0.20 先占，0.16 让位。

    变红的变异：去掉容差闸（稀疏链上最近的可能是 Δ=0.42）；去掉去重闸（0.16 与 0.20 都挂 88）；
    改回按 LADDER_DELTAS 升序占档（0.16 抢走 88、0.20 空档）。
    """
    chain = [_c("P", 80, -0.09), _c("P", 88, -0.19), _c("P", 95, -0.42),
             _c("C", 120, 0.10)]
    lad = SC.build_ladder(chain, 100.0, "2026-10-23")
    put = lad["put"]
    assert put["0.10"]["short"]["strike"] == 80.0
    assert put["0.20"]["short"] is not None, f"主档空档：{put['0.20']['reasons']}"
    assert put["0.20"]["short"]["strike"] == 88.0
    assert put["0.16"]["short"] is None and put["0.16"]["reasons"] == ["same_contract_as_0.20"]
    assert put["0.25"]["short"] is None
    assert put["0.25"]["reasons"][0].startswith("no_delta_within_tol")
    assert put["0.30"]["short"] is None
    assert lad["call"]["0.10"]["short"]["strike"] == 120.0
    assert (lad["expiry"], lad["dte"], lad["underlying_price"]) == ("2026-10-23", 30, 100.0)
    assert list(put) == ["0.10", "0.16", "0.20", "0.25", "0.30"], "展示顺序仍按 LADDER_DELTAS"


def _bs_abs_delta(S, K, dte, iv, cp, r=0.045):
    """独立 BS |Δ|（只用 math，不经被测模块），给稀疏链夹具当「CBOE delta」。"""
    import math
    T = dte / 365.0
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    n = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return n if cp == "C" else 1.0 - n


def _sparse_chain(S, *, step=5.0, dte=14, iv=0.30):
    out = []
    lo, hi = int(S * 0.6 / step), int(S * 1.4 / step) + 1
    for i in range(lo, hi + 1):
        K = i * step
        for cp in ("P", "C"):
            if (cp == "P" and K >= S) or (cp == "C" and K <= S):
                continue                                   # 只挂价外，同真实卖方关心的一侧
            a = _bs_abs_delta(S, K, dte, iv, cp)
            out.append(_c(cp, K, -a if cp == "P" else a, iv=iv, dte=dte, expiry="2026-10-07"))
    return out


def test_primary_rung_is_never_lost_to_an_auxiliary_rung_on_sparse_chains():
    """稀疏链（$5 间距、14DTE、IV 30%）：只要容差内有合约，主档 0.20 就必须有短腿。
    S=252 的具体例子：put 240（|Δ|≈0.187）同时是 0.16 与 0.20 的最近者 ⇒ 0.20 取 240、0.16 让位。
    再在 S∈[200,300) 每 0.5 扫一遍（两侧），并数出「0.16 与 0.20 争同一张」的 S 个数——
    数为 0 说明夹具没咬到要测的情形。

    变红的变异：改回按 LADDER_DELTAS 升序占档（0.16 先占 ⇒ 0.20 给 `same_contract_as_0.16`，
    正是评审实测周度 26.9% 的缺失；预注册唯一结果档因此在低 IV 票上系统性缺失）。
    """
    S = 252.0
    lad = SC.build_ladder(_sparse_chain(S), S, "2026-10-07")
    abs_d = {c["strike"]: abs(c["delta"]) for c in _sparse_chain(S) if c["cp"] == "P"}
    near = {t: min(abs_d, key=lambda k: (abs(abs_d[k] - t), abs_d[k])) for t in (0.16, 0.20)}
    assert near[0.16] == near[0.20] == 240.0, f"夹具自检：两档应争同一张 240，实为 {near}"
    assert lad["put"]["0.20"]["short"] is not None, f"主档空档：{lad['put']['0.20']['reasons']}"
    assert lad["put"]["0.20"]["short"]["strike"] == 240.0
    assert lad["put"]["0.16"]["short"] is None
    assert lad["put"]["0.16"]["reasons"] == ["same_contract_as_0.20"]
    assert lad["put"]["0.10"]["short"]["strike"] == 235.0

    contested = 0
    for i in range(200):
        S = 200.0 + 0.5 * i
        chain = _sparse_chain(S)
        lad = SC.build_ladder(chain, S, "2026-10-07")
        for side, cp in (("put", "P"), ("call", "C")):
            ds = {c["strike"]: abs(c["delta"]) for c in chain if c["cp"] == cp}
            within = [k for k, a in ds.items() if abs(a - 0.20) <= SC.DELTA_TOL + 1e-9]
            if not within:
                continue
            n16 = min(ds, key=lambda k: (abs(ds[k] - 0.16), ds[k]))
            n20 = min(ds, key=lambda k: (abs(ds[k] - 0.20), ds[k]))
            contested += n16 == n20
            short = lad[side]["0.20"]["short"]
            assert short is not None, f"S={S} {side}: 容差内有合约，0.20 却空档 {lad[side]['0.20']['reasons']}"
            assert short["strike"] == n20
    assert contested > 20, f"夹具自检：争同一张的情形太少（{contested}），测试咬不到"


def test_ladder_leg_fields_itm_prob_and_bs_fallback():
    """leg 带 itm_prob = N(−d2)（不是 |Δ|）与 σ 距离；CBOE Δ 缺失时 BS 兜底并记 delta_source。

    变红的变异：itm_prob 填 |Δ|；Δ 缺失的合约直接跳过（远翼只有 IV 时整档丢失）。
    """
    import sell_strike_levels as L
    chain = [_c("P", 88, None, iv=0.30)]
    T = 30 / 365
    lad = SC.build_ladder(chain, 100.0, "2026-10-23")
    want_delta = L.bs_delta(100.0, 88.0, T, L.RISK_FREE_RATE, 0.30, "P")
    rung = f"{min(SC.LADDER_DELTAS, key=lambda d: abs(d - abs(want_delta))):.2f}"
    leg = lad["put"][rung]["short"]
    assert leg["delta_source"] == "bs" and leg["delta"] == pytest.approx(want_delta)
    assert leg["itm_prob"] == pytest.approx(L.itm_probability(100.0, 88.0, T, L.RISK_FREE_RATE, 0.30, "P"))
    assert abs(leg["itm_prob"] - abs(leg["delta"])) > 0.01
    assert leg["sigma_distance"] < 0
    for k in ("symbol", "strike", "cp", "delta", "delta_source", "iv", "gamma", "theta", "oi",
              "bid", "ask", "mid", "spread_pct", "quote_ok", "itm_prob", "sigma_distance"):
        assert k in leg


def test_wing_is_nearest_listed_strike_beyond_half_sigma():
    """保护腿：更价外方向、离短腿 ≥ 0.5·S·σ_short·√T 的**最近**挂牌行权价；找不到 ⇒ `no_wing_strike`。

    σ=.30、30DTE、S=100 ⇒ 宽度 4.30：短腿 88 ⇒ 84 太近（4.0），合格的最近是 83。
    变红的变异：取紧挨着的下一个行权价（84）；取最远的（70）；往价内方向找。
    """
    chain = [_c("P", 70, None, iv=None), _c("P", 83, None, iv=None), _c("P", 84, None, iv=None),
             _c("P", 88, -0.23), _c("C", 112, 0.23), _c("C", 115, None, iv=None)]
    lad = SC.build_ladder(chain, 100.0, "2026-10-23")
    assert lad["put"]["0.20"]["wing"]["strike"] == 83.0
    assert lad["call"]["0.20"]["wing"] is None
    assert "no_wing_strike" in lad["call"]["0.20"]["reasons"]


# ───────────────────────────── 7–9. 结构报价与到期盈亏

def _leg(K, cp, bid, ask, spread_pct=None):
    q = SC.quote_leg({"bid": bid, "ask": ask})
    if spread_pct is not None:
        q["spread_pct"] = spread_pct
    return {"symbol": f"L{cp}{K}", "strike": float(K), "cp": cp, **q}


def _ladder(put_short=(90, 2.0, 2.1), put_wing=(85, 0.9, 1.0),
            call_short=(110, 1.5, 1.6), call_wing=(120, 0.4, 0.5), dte=30, S=100.0):
    def slot(short, wing, cp):
        return {"short": _leg(short[0], cp, short[1], short[2]) if short else None,
                "wing": _leg(wing[0], cp, wing[1], wing[2]) if wing else None, "reasons": []}
    return {"expiry": "2026-10-23", "dte": dte, "underlying_price": S,
            "put": {"0.20": slot(put_short, put_wing, "P")},
            "call": {"0.20": slot(call_short, call_wing, "C")}}


def test_quote_ok_requires_positive_bid_and_fills_are_bid_for_sells_ask_for_buys():
    """quote_ok = bid>0 且 ask≥bid；卖按 bid、买按 ask（不是 mid）。

    变红的变异：允许 bid=0（mid 成了「一半的 ask」）；成交价按 mid。
    """
    assert SC.quote_leg({"bid": 0.0, "ask": 0.10})["quote_ok"] is False
    assert SC.quote_leg({"bid": 0.0, "ask": 0.10})["mid"] is None
    assert SC.quote_leg({"bid": 1.2, "ask": 1.0})["quote_ok"] is False
    ok = SC.quote_leg({"bid": 1.0, "ask": 1.2})
    assert ok["quote_ok"] and ok["mid"] == pytest.approx(1.1) and ok["spread_pct"] == pytest.approx(0.2 / 1.1)

    q = SC.structure_quote(_ladder(), "bull_put_spread", 0.20)
    assert q["credit"] == pytest.approx(2.0 - 1.0), "短腿按 bid 2.0、保护腿按 ask 1.0"
    assert [(x["action"], x["fill"]) for x in q["legs"]] == [("sell", 2.0), ("buy", 1.0)]

    bad = SC.structure_quote(_ladder(put_wing=(85, 0.0, 0.05)), "bull_put_spread", 0.20)
    assert bad["quotable"] is False and bad["reason"] == "quote_not_ok:put_wing"
    assert bad["credit"] is None


def test_structure_economics_spreads_condor_and_non_positive_credit():
    """价差 max_loss = collateral = width − credit；铁鹰取**宽的**一翼；credit ≤ 0 不可报价。

    变红的变异：铁鹰取窄翼 / 两翼宽度相加；价差 max_loss 忘了扣 credit；credit ≤ 0 仍 quotable。
    """
    lad = _ladder()
    bps = SC.structure_quote(lad, "bull_put_spread", 0.20)
    assert (bps["credit"], bps["max_loss"], bps["collateral"]) == pytest.approx((1.0, 4.0, 4.0))
    assert bps["breakevens"] == pytest.approx([89.0])
    assert bps["yield_raw"] == pytest.approx(0.25)
    assert bps["yield_annualized"] == pytest.approx(0.25 * 365 / 30)
    bcs = SC.structure_quote(lad, "bear_call_spread", 0.20)
    assert (bcs["credit"], bcs["max_loss"]) == pytest.approx((1.0, 9.0))
    ic = SC.structure_quote(lad, "iron_condor", 0.20)
    assert ic["credit"] == pytest.approx(2.0)
    assert ic["max_loss"] == ic["collateral"] == pytest.approx(10.0 - 2.0)
    assert ic["breakevens"] == pytest.approx([88.0, 112.0])
    sp = SC.structure_quote(lad, "short_put", 0.20)
    assert (sp["credit"], sp["collateral"], sp["max_loss"]) == pytest.approx((2.0, 90.0, 88.0))
    sc = SC.structure_quote(lad, "short_call", 0.20)
    assert sc["max_loss"] is None and sc["collateral"] == 100.0 and sc["breakevens"] == [111.5]
    st = SC.structure_quote(lad, "strangle", 0.20)
    assert (st["credit"], st["collateral"], st["max_loss"]) == (3.5, 90.0, None)
    assert st["breakevens"] == pytest.approx([86.5, 113.5])

    neg = SC.structure_quote(_ladder(put_wing=(85, 2.4, 2.5)), "bull_put_spread", 0.20)
    assert neg["quotable"] is False and neg["reason"] == "non_positive_credit"
    assert neg["credit"] == pytest.approx(-0.5) and neg["max_loss"] is None

    wide = _ladder()
    wide["put"]["0.20"]["short"]["spread_pct"] = 0.30
    w = SC.structure_quote(wide, "short_put", 0.20)
    assert w["quotable"] is False and w["reason"] == "short_spread_too_wide:put"
    assert w["credit"] == 2.0, "点差过宽照样给出金额（供展示），只是不可报价"
    wing_wide = _ladder()
    wing_wide["put"]["0.20"]["wing"]["spread_pct"] = 0.90
    assert SC.structure_quote(wing_wide, "bull_put_spread", 0.20)["quotable"] is True, \
        "点差闸只管短腿"

    assert SC.structure_quote(lad, "short_put", None)["reason"] == "rung_unavailable"
    with pytest.raises(ValueError):
        SC.structure_quote(lad, "butterfly", 0.20)
    with pytest.raises(ValueError):
        SC.structure_quote(lad, "short_put", 0.15)


def test_per_side_rungs_for_route_split():
    """route 可能 put far / call base：strangle 接受 {"put": 0.10, "call": 0.20}。

    变红的变异：dict 形式被忽略、两侧都取同一档。
    """
    lad = _ladder()
    lad["put"]["0.10"] = {"short": _leg(85, "P", 0.8, 0.9), "wing": None, "reasons": ["no_wing_strike"]}
    q = SC.structure_quote(lad, "strangle", {"put": 0.10, "call": 0.20})
    assert [(x["side"], x["rung"], x["strike"]) for x in q["legs"]] == [("put", "0.10", 85.0), ("call", "0.20", 110.0)]
    assert q["credit"] == pytest.approx(0.8 + 1.5)


def test_expiry_pnl_uses_intrinsic_value_for_puts_and_calls():
    """到期每股盈亏 = credit − Σ内在(short) + Σ内在(wing)；put 内在 = max(K−S_T,0)，call = max(S_T−K,0)。

    变红的变异：put 内在写成 S_T−K；保护腿记成负号；不可报价的结构也给盈亏。
    """
    lad = _ladder()
    bps = SC.structure_quote(lad, "bull_put_spread", 0.20)        # 90/85，credit 1.0
    assert SC.structure_pnl_at_expiry(bps, 95.0) == pytest.approx(1.0)
    assert SC.structure_pnl_at_expiry(bps, 87.0) == pytest.approx(1.0 - 3.0)
    assert SC.structure_pnl_at_expiry(bps, 80.0) == pytest.approx(-bps["max_loss"])
    assert SC.pnl_over_credit(bps, 87.0) == pytest.approx(-2.0)
    bcs = SC.structure_quote(lad, "bear_call_spread", 0.20)       # 110/120，credit 1.0
    assert SC.structure_pnl_at_expiry(bcs, 115.0) == pytest.approx(1.0 - 5.0)
    assert SC.structure_pnl_at_expiry(bcs, 130.0) == pytest.approx(-bcs["max_loss"])
    sc = SC.structure_quote(lad, "short_call", 0.20)
    assert SC.structure_pnl_at_expiry(sc, 100.0) == pytest.approx(1.5)
    neg = SC.structure_quote(_ladder(put_wing=(85, 2.4, 2.5)), "bull_put_spread", 0.20)
    assert SC.structure_pnl_at_expiry(neg, 95.0) is None
    assert SC.structure_pnl_at_expiry(bps, None) is None


# ───────────────────────────── 10. 环境路由

def _zg(state, sign, below=None, above=None, n=SC.MIN_SWEEP_CONTRACTS):
    # n 缺省恰在门槛上（≥ 门槛才可用）：顺带钉住边界是 `<` 而不是 `<=`
    return {"curve_state": state, "sign_at_spot": sign, "zg_below_pct": below, "zg_above_pct": above,
            "n_contracts": n}


@pytest.mark.parametrize("zg,put,call,fp,fc", [
    (_zg("all_negative", "negative"), "far", "far", True, True),
    (_zg("crosses", "negative", below=15.0, above=12.0), "far", "far", True, True),
    (_zg("crosses", "positive", below=2.0, above=9.0), "far", "base", True, False),
    (_zg("crosses", "positive", below=3.0), "far", "base", True, False),
    (_zg("crosses", "positive", below=3.5), "base", "base", False, False),
    (_zg("crosses", "positive", above=1.0), "base", "base", False, False),
    (_zg("all_positive", "positive"), "base", "base", False, False),
    (_zg("crosses", "zero", below=0.0, above=0.0), "far", "base", True, False),
    (_zg("insufficient_contracts", None), "unavailable", "unavailable", None, None),
    (_zg("crosses", None, below=1.0), "unavailable", "unavailable", None, None),
])
def test_route_looks_at_sign_first_then_distance(zg, put, call, fp, fc):
    """先看符号：现价处负 gamma（哪怕零点远在 15% 外）⇒ 两侧 far；正 gamma 且下方零点 ≤3% ⇒ put far。

    变红的变异：只看距离的旧规则（远离零点的负 gamma 判成 base）；边界 3.0 用 <；
    insufficient 判成 base；call 在正 gamma 时也跟着下方零点退后。
    """
    r = SC.route(zg)
    assert (r["put"], r["call"], r["flag_put"], r["flag_call"]) == (put, call, fp, fc)
    assert r["rule_version"] == SC.ROUTE_RULE_VERSION == 1 and r["view"] == "le_45dte"
    assert r["reasons"]


@pytest.mark.parametrize("n,sign,want_reason", [
    (19, "positive", "sweep_too_few_contracts:19"),
    (19, "negative", "sweep_too_few_contracts:19"),
    (0, "positive", "sweep_too_few_contracts:0"),
    (None, "positive", "sweep_too_few_contracts:None"),
    ("missing", "positive", "sweep_too_few_contracts:None"),
])
def test_route_needs_min_sweep_contracts(n, sign, want_reason):
    """扫描合约数 < MIN_SWEEP_CONTRACTS（或缺键）⇒ 两侧 unavailable、flag 为 None、
    reason `sweep_too_few_contracts:<n>`；**先于符号**（19 张合约的负 gamma 也不算政体）。
    门槛上（20）照常路由——边界见 `_zg` 缺省值与上面的参数化用例。

    变红的变异：门槛失效（删掉判断 / 常量改 0）⇒ 19 张的正 gamma 判 base、负 gamma 判 far，
    这些行带着 flag 进检验；缺键当成够。
    """
    zg = _zg("all_positive" if sign == "positive" else "all_negative", sign, n=n)
    if n == "missing":
        zg.pop("n_contracts")
    r = SC.route(zg)
    assert (r["put"], r["call"], r["flag_put"], r["flag_call"]) == ("unavailable", "unavailable", None, None)
    assert r["reasons"] == [want_reason]
    assert SC.rung_for(r["put"]) is None
    ok = SC.route(_zg("all_negative", "negative", n=SC.MIN_SWEEP_CONTRACTS))
    assert (ok["put"], ok["flag_put"]) == ("far", True)


def test_route_gate_reads_the_sweep_count_of_the_route_view():
    """端到端：真实 level_map 的 le_45dte 视图里只有 4 张合约有 IV（其余 36 张无 IV）⇒
    zero_gamma.n_contracts=4 ⇒ route unavailable；补齐 IV 后（40 张）照常路由。

    变红的变异：门槛读的是视图合约总数（`view["n_contracts"]`=40，含无 IV 的）而不是进了
    扫描的数；或门槛失效。
    """
    import sell_strike_levels as L
    # call OI 多于 put：对称 OI 会让朴素净 GEX 恰好抵消成 0（insufficient_contracts），测不到门槛
    chain = [dict(_c(cp, K, None, iv=(0.30 if K in (96, 104) else None), dte=30,
                     oi=(300.0 if cp == "C" else 100.0)), gamma=None)
             for K in range(80, 121, 2) if K != 100 for cp in ("P", "C")]
    assert len(chain) == 40
    view = L.level_map(chain, 100.0)["views"][SC.ROUTE_VIEW]
    assert view["n_contracts"] == 40 and view["zero_gamma"]["n_contracts"] == 4, "夹具自检"
    assert view["zero_gamma"]["sign_at_spot"] == "positive", "夹具自检：没有门槛时它会被正常路由"
    r = SC.route(view["zero_gamma"])
    assert r["put"] == "unavailable" and r["reasons"] == ["sweep_too_few_contracts:4"]

    full = [dict(c, iv=0.30) for c in chain]
    zg = L.level_map(full, 100.0)["views"][SC.ROUTE_VIEW]["zero_gamma"]
    assert zg["n_contracts"] == 40 and zg["sign_at_spot"] == "positive", "夹具自检"
    assert SC.route(zg)["put"] in ("base", "far")


def test_rung_for_maps_route_labels():
    """base → 0.20、far → 0.10、unavailable → None；拼错 ⇒ ValueError。

    变红的变异：far 映射到更**近**的档；未知标签静默给 None。
    """
    assert (SC.rung_for("base"), SC.rung_for("far"), SC.rung_for("unavailable")) == (0.20, 0.10, None)
    with pytest.raises(ValueError):
        SC.rung_for("Far")


# ───────────────────────────── 11. 财报标注（真实返回形状）

def test_classify_earnings_on_real_upcoming_fn_shapes(monkeypatch):
    """用 `_earnings_date_from_swarm` 本身产出三种真实形状：dict（有日期）/ {"earnings_date": None} / None。

    变红的变异：None 当成 "none"（兜底失败被读成「没有财报」、进了独立单位）；
    边界 d==as_of 或 d==expiry 不算 before_expiry；dict 缺键当成 "none"。
    """
    import alpha_hive_daily_report as ahdr

    class _Boom:
        def __init__(self):
            raise RuntimeError("offline")

    monkeypatch.setitem(sys.modules, "earnings_watcher", types.SimpleNamespace(EarningsWatcher=_Boom))

    def swarm(cats):
        return {"agent_details": {"ChronosBeeHorizon": {"details": {"catalysts": cats} if cats else {}}}}

    fn = ahdr.AlphaHiveDailyReporter._earnings_date_from_swarm({
        "HAS": swarm([{"type": "earnings", "date": "2026-10-20"}]),
        "EDGE": swarm([{"type": "earnings", "date": "2026-09-23"}]),
        "NONE": swarm([{"type": "fda", "date": "2026-10-01"}]),
        "FAIL": swarm(None),
    })
    as_of, expiry = "2026-09-23", "2026-10-23"
    assert fn("FAIL") is None, "夹具自检：兜底失败的真实形状是 None"
    assert fn("NONE")["earnings_date"] is None
    assert SC.classify_earnings(fn("HAS"), as_of, expiry) == "before_expiry"
    assert SC.classify_earnings(fn("EDGE"), as_of, expiry) == "before_expiry"
    assert SC.classify_earnings(fn("HAS"), as_of, "2026-10-20") == "before_expiry"
    assert SC.classify_earnings(fn("HAS"), as_of, "2026-10-16") == "after_expiry"
    assert SC.classify_earnings(fn("NONE"), as_of, expiry) == "none"
    assert SC.classify_earnings(fn("FAIL"), as_of, expiry) == "unknown"
    assert SC.classify_earnings({"earnings_date": "2026-09-01"}, as_of, expiry) == "none"
    assert SC.classify_earnings({"earnings_date": "soon"}, as_of, expiry) == "unknown"
    assert SC.classify_earnings({"source": "x"}, as_of, expiry) == "unknown"


# ───────────────────────────── 冻结常量

def test_frozen_constants():
    """预注册文档引用同一组值；改任何一个都是协议变更（要记修订号并声明截断）。

    变红的变异：改任意一个常量。
    """
    assert SC.CANDIDATES_SCHEMA_VERSION == 1
    assert SC.TENORS == {"monthly": {"dte_lo": 21, "dte_hi": 45, "hi_inclusive": True, "target_dte": 30},
                         "weekly": {"dte_lo": 7, "dte_hi": 21, "hi_inclusive": False, "target_dte": 14}}
    assert SC.LADDER_DELTAS == (0.10, 0.16, 0.20, 0.25, 0.30)
    assert SC.LADDER_FILL_ORDER == (0.20, 0.10, 0.16, 0.25, 0.30)
    assert SC.LADDER_FILL_ORDER[:2] == (SC.BASE_RUNG, SC.FAR_RUNG), "主档、远档先占"
    assert (SC.DELTA_TOL, SC.WING_WIDTH_SIGMA, SC.MAX_SPREAD_PCT) == (0.05, 0.5, 0.25)
    assert (SC.ROUTE_RULE_VERSION, SC.ROUTE_VIEW, SC.FLIP_BUFFER_PCT) == (1, "le_45dte", 3.0)
    assert SC.MIN_SWEEP_CONTRACTS == 20
    assert (SC.BASE_RUNG, SC.FAR_RUNG) == (0.20, 0.10)
    assert SC.STRUCTURES == ("short_put", "short_call", "bull_put_spread", "bear_call_spread",
                             "strangle", "iron_condor")


# ───────────────────────────── N1. 报价取整（金额 4 位、收益率 6 位）

def test_quote_amounts_rounded_to_4dp_and_yields_to_6dp():
    """bid/ask 相减的浮点噪声不许原样外露（实测 1.45 − 0.13 = 1.3199999999999998 进了 MCP 输出）。

    夹具：短腿 bid 1.45 − 保护腿 ask 0.13、call 侧 2.00 − 0.68 都是噪声 credit；put 短腿行权价 92.37
    让 BE 自带噪声（92.37 − 1.32 = 91.05000000000001）；S 带 7 位小数（short_call 的 collateral）。
    变红的变异：不取整 / 收益率只取 4 位 / 金额取 2 位 / BE 不取整 / 年化用取整后的 yield_raw 再乘。
    """
    assert (1.45 - 0.13, 92.37 - 1.32) != (1.32, 91.05), "夹具自证：确实带浮点噪声"
    lad = _ladder(put_short=(92.37, 1.45, 1.5), put_wing=(87.37, 0.1, 0.13),
                  call_short=(110, 2.0, 2.1), call_wing=(120, 0.6, 0.68), dte=30, S=100.1234567)
    bps = SC.structure_quote(lad, "bull_put_spread", 0.20)
    assert (bps["credit"], bps["max_loss"], bps["collateral"]) == (1.32, 3.68, 3.68)
    assert bps["breakevens"] == [91.05]
    assert bps["yield_raw"] == round(1.32 / 3.68, 6) == 0.358696
    assert bps["yield_annualized"] == round(1.32 / 3.68 * 365 / 30, 6) == 4.36413, \
        "年化用全精度 yield_raw 乘 365/DTE 再取整（先取整再乘得 4.364135）"
    assert SC.structure_quote(lad, "short_call", 0.20)["collateral"] == 100.1235
    for st in SC.STRUCTURES:
        q = SC.structure_quote(lad, st, 0.20)
        assert q["quotable"], (st, q["reasons"])
        for k in ("credit", "max_loss", "collateral"):
            assert q[k] is None or q[k] == round(q[k], 4), (st, k, q[k])
        assert all(b == round(b, 4) for b in q["breakevens"]), (st, q["breakevens"])
        for k in ("yield_raw", "yield_annualized"):
            assert q[k] == round(q[k], 6), (st, k, q[k])


def test_offsetting_condor_wings_are_non_positive_credit_not_a_tiny_positive_one():
    """铁鹰两翼 credit 恰好相抵：浮点和是 +1.4e-17，credit ≤ 0 的判定若在取整前做，
    就成了「可报价、credit≈0、收益率≈0」的结构。反方向（−2.8e-17）取整得 −0.0，要归一成 0.0。
    变红的变异：判定用取整前的 credit；取整不归一 −0.0（JSON 里出现 -0.0）。
    """
    assert (0.05 - 0.10) + (0.20 - 0.15) > 0, "夹具自证：浮点和确实是正的"
    lad = _ladder(put_short=(90, 0.05, 0.06), put_wing=(85, 0.08, 0.10),
                  call_short=(110, 0.20, 0.21), call_wing=(120, 0.10, 0.15))
    ic = SC.structure_quote(lad, "iron_condor", 0.20)
    assert ic["quotable"] is False and ic["reasons"] == ["non_positive_credit"]
    assert ic["credit"] == 0.0 and str(ic["credit"]) == "0.0"
    assert ic["yield_raw"] is None and ic["max_loss"] is None and ic["breakevens"] == []

    assert (0.30 - 0.20) + (0.10 - 0.20) < 0, "夹具自证：浮点和确实是负的"
    neg = SC.structure_quote(_ladder(put_short=(90, 0.30, 0.31), put_wing=(85, 0.10, 0.20),
                                     call_short=(110, 0.10, 0.11), call_wing=(120, 0.05, 0.20)),
                             "iron_condor", 0.20)
    assert neg["reasons"] == ["non_positive_credit"] and str(neg["credit"]) == "0.0"
