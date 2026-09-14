"""收盘后拿到「盘中生成」的 CBOE payload：close 不是官方收盘价（v0.45.234）

v0.45.39 的 vintage 只比**日期**，v0.45.46 的 official_price 只看**本机钟**。
CDN 发一份当天 12:10 ET 生成的文件，到 17:00 ET 两道都放行，`close`（此刻就是
12:10 的成交价）被当官方收盘写进 `_snapshot_stock_price` 与 `price_at_predict`。

2026-09-10/11 实测（yfinance 1m K 线找该价最后一次成交的时刻）：
    T     09-11  snapshot 26.225   official 26.06   → 最后成交 12:10 ET
    TMUS  09-10  snapshot 175.18   official 177.16  → 12:07 ET
    ABBV  09-10  snapshot 252.45   official 255.00  → 14:00 ET
    VKTX  09-11  snapshot 32.3441  official 31.98   → 14:36 ET
⚠️ 下面夹具里的 last_trade_time 是**据此重建**的（快照不存原始 payload），
   不是当时实拉的原文；价格与收盘价是实测值。

「新鲜」一侧的边界来自 2026-09-14 盘前实拉 30/30 只（09-11 那场已收完）：
last_trade_time 全部落在 15:59:56 ~ 16:00:00。
"""

from datetime import date, datetime, time as dtime

import pytest

import cboe_options as co
from cboe_options import STALE_INTRADAY_SOURCE, official_price
from is_trading_day import session_close_et

SCAN_ET = datetime(2026, 9, 11, 17, 31)       # 09-11 扫描写 T 快照的时刻（14:31 PDT）


@pytest.fixture(autouse=True)
def _clean_stats():
    co.reset_payload_stats()
    yield
    co.reset_payload_stats()


def _p(close, last_trade, symbol="T"):
    return {"symbol": symbol, "close": close, "current_price": close + 0.3,
            "last_trade_time": last_trade}


class TestMeasuredCases:
    @pytest.mark.parametrize("sym,close,last_trade,now", [
        ("T", 26.225, "2026-09-11T12:10:00", SCAN_ET),
        ("VKTX", 32.3441, "2026-09-11T14:36:00", datetime(2026, 9, 11, 17, 20)),
        ("TMUS", 175.18, "2026-09-10T12:07:00", datetime(2026, 9, 10, 17, 36)),
        ("ABBV", 252.45, "2026-09-10T14:00:00", datetime(2026, 9, 10, 17, 30)),
    ])
    def test_mid_session_payload_after_close_is_labelled(self, sym, close, last_trade, now):
        px, src = official_price(_p(close, last_trade, sym), now)
        assert src == STALE_INTRADAY_SOURCE, f"{sym} 盘中 payload 被当成了官方收盘"
        assert px == close, "价格本身照返回：链内计算要的是与链同一时刻的价"

    def test_fresh_payload_same_ticker_is_official_close(self):
        px, src = official_price(_p(26.06, "2026-09-11T15:59:59"), SCAN_ET)
        assert (px, src) == (26.06, "cboe_close")


class TestFreshBoundary:
    @pytest.mark.parametrize("hms", ["15:59:56", "15:59:57", "15:59:59", "16:00:00"])
    def test_measured_fresh_range_passes(self, hms):
        """09-14 实拉 30/30 的范围 —— 任何一只被判陈旧就是误杀健康标的"""
        assert official_price(_p(50.0, f"2026-09-11T{hms}"), SCAN_ET)[1] == "cboe_close"

    def test_just_outside_tolerance_is_stale(self):
        # 延迟报价滞后 ~15 分钟：16:14 生成的文件 last_trade 在 15:58:59
        assert official_price(_p(50.0, "2026-09-11T15:58:59"), SCAN_ET)[1] == STALE_INTRADAY_SOURCE

    def test_tz_aware_last_trade_is_converted(self):
        # 19:59:59Z = 15:59:59 ET（EDT）
        assert official_price(_p(50.0, "2026-09-11T19:59:59+00:00"), SCAN_ET)[1] == "cboe_close"
        assert official_price(_p(50.0, "2026-09-11T16:10:00+00:00"), SCAN_ET)[1] == STALE_INTRADAY_SOURCE


class TestSessionNotYetClosedIsOutOfScope:
    def test_intraday_branch_unchanged(self):
        """盘中数据本来就滞后 15 分钟，不归这条判据管"""
        px, src = official_price(_p(50.0, "2026-09-11T10:45:00"), datetime(2026, 9, 11, 11, 0))
        assert src == "cboe_intraday" and px == 50.3

    def test_16_00_to_close_plus_tol_boundary(self):
        """收盘那一刻起判据生效（与 is_market_open 的 16:00 同口径）"""
        now = datetime(2026, 9, 11, 16, 0)
        assert official_price(_p(50.0, "2026-09-11T15:30:00"), now)[1] == STALE_INTRADAY_SOURCE


class TestNextMorningAndOlderPayloads:
    def test_premarket_next_day_fresh(self):
        now = datetime(2026, 9, 14, 8, 0)
        assert official_price(_p(26.06, "2026-09-11T15:59:59"), now)[1] == "cboe_close"

    def test_premarket_next_day_mid_session_payload(self):
        """周末挂着一份周五 12:10 的文件，到周一盘前仍然不是收盘价"""
        now = datetime(2026, 9, 14, 8, 0)
        assert official_price(_p(26.225, "2026-09-11T12:10:00"), now)[1] == STALE_INTRADAY_SOURCE

    def test_aware_now_accepted(self):
        now = datetime(2026, 9, 11, 21, 31, tzinfo=co.ZoneInfo("UTC"))   # 17:31 ET
        assert official_price(_p(26.225, "2026-09-11T12:10:00"), now)[1] == STALE_INTRADAY_SOURCE


class TestEarlyClose:
    @pytest.mark.parametrize("d", [
        date(2026, 11, 27),   # 感恩节次日（周五）
        date(2026, 12, 24),   # 周四，交易日
        date(2025, 7, 3),     # 7/4 周五
        date(2024, 7, 3),     # 7/4 周四
        date(2023, 7, 3),     # 7/4 周二
    ])
    def test_early_close_days(self, d):
        assert session_close_et(d) == dtime(13, 0)

    @pytest.mark.parametrize("d", [
        date(2026, 9, 11),
        date(2022, 7, 1),     # 7/4 周一 → 不提前收
        date(2027, 12, 24),   # 周五：圣诞 observed 休市日，不是交易日
        date(2026, 11, 26),   # 感恩节当天（休市，本函数只答几点收）
    ])
    def test_normal_close_days(self, d):
        assert session_close_et(d) == dtime(16, 0)

    def test_half_day_fresh_payload_not_misjudged(self):
        now = datetime(2026, 11, 27, 17, 0)
        assert official_price(_p(50.0, "2026-11-27T12:59:59"), now)[1] == "cboe_close"
        assert official_price(_p(50.0, "2026-11-27T11:00:00"), now)[1] == STALE_INTRADAY_SOURCE


class TestObservable:
    """谁会红？—— 计数必须真的接上，且按标的去重（同一 payload 被链/报价集/管道各调一次）"""

    def test_counted_once_per_symbol(self):
        for _ in range(3):
            official_price(_p(26.225, "2026-09-11T12:10:00", "T"), SCAN_ET)
        official_price(_p(32.3441, "2026-09-11T14:36:00", "VKTX"), SCAN_ET)
        official_price(_p(50.0, "2026-09-11T15:59:59", "NVDA"), SCAN_ET)
        s = co.payload_stats()
        assert s["price_stale_intraday"] == 2
        assert s["price_unverifiable"] == 0

    def test_missing_last_trade_is_fail_open_but_counted(self):
        px, src = official_price({"symbol": "X", "close": 10.0}, SCAN_ET)
        assert (px, src) == (10.0, "cboe_close")
        assert co.payload_stats()["price_unverifiable"] == 1

    def test_reset_clears(self):
        official_price(_p(26.225, "2026-09-11T12:10:00"), SCAN_ET)
        co.reset_payload_stats()
        assert co.payload_stats()["price_stale_intraday"] == 0


# ───────────────────────────── data_pipeline：入场价必须拒收盘中陈旧价
class _Src:
    def __init__(self, name, data):
        import data_pipeline as dp
        self.name = name
        self._data = data
        self.breaker = dp.ObservableCircuitBreaker(name)
        self.calls = 0

    def fetch(self, ticker):
        self.calls += 1
        return self._data


def _stock(price, **kw):
    import data_pipeline as dp
    return dp.StockData(price=price, data_source=kw.pop("data_source", dp.DataQuality.REAL),
                        fetch_timestamp=__import__("time").time(), **kw)


class TestPipelineDefers:
    def _fetcher(self, sources):
        import data_pipeline as dp
        f = dp.MultiSourceFetcher.__new__(dp.MultiSourceFetcher)
        dp.MultiSourceFetcher.__init__(f)
        f._sources = sources
        return f

    def test_next_source_wins_over_stale_intraday(self):
        import data_pipeline as dp
        cboe = _Src("cboe", _stock(26.225, source_name="cboe", price_source=STALE_INTRADAY_SOURCE,
                                   data_source=dp.DataQuality.DEGRADED, defer=True))
        yf = _Src("yfinance", _stock(26.06, source_name="yfinance", price_source="yfinance_daily_close"))
        out = self._fetcher([cboe, yf]).fetch("T")
        assert out["price"] == 26.06 and out["source_name"] == "yfinance"
        assert yf.calls == 1

    def test_stale_intraday_used_only_when_all_else_fails_and_stays_labelled(self):
        import data_pipeline as dp
        cboe = _Src("cboe", _stock(26.225, source_name="cboe", price_source=STALE_INTRADAY_SOURCE,
                                   data_source=dp.DataQuality.DEGRADED, defer=True))
        out = self._fetcher([cboe, _Src("yfinance", None), _Src("finnhub", None)]).fetch("T")
        assert out["price"] == 26.225, "比 price=0 让整只标的跳过强"
        assert out["data_source"] == dp.DataQuality.DEGRADED
        assert out["price_source"] == STALE_INTRADAY_SOURCE, "退用时不许冒充官方收盘"
        assert "defer" not in out

    def test_cboe_source_defers_without_tripping_breaker_or_fetching_history(self, monkeypatch):
        import data_pipeline as dp
        payload = _p(26.225, "2026-09-11T12:10:00")
        monkeypatch.setattr(co, "_fetch_cboe_payload", lambda t, timeout=15: payload)
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        hist_calls = []
        monkeypatch.setattr(dp, "_fetch_history_metrics", lambda t: hist_calls.append(t))
        src = dp.CBOESource()
        for _ in range(5):
            d = src.fetch("T")
            assert d is not None and d.defer and d.price_source == STALE_INTRADAY_SOURCE
        assert src.breaker.allow_request(), "盘中陈旧不是源故障，连撞 5 只也不许熔断"
        assert hist_calls == [], "被推迟的数不该白耗 yfinance 配额拉历史K线"

    def test_cboe_source_fresh_close_not_deferred(self, monkeypatch):
        import data_pipeline as dp
        monkeypatch.setattr(co, "_fetch_cboe_payload",
                            lambda t, timeout=15: _p(26.06, "2026-09-11T15:59:59"))
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        monkeypatch.setattr(dp, "_fetch_history_metrics", lambda t: None)
        d = dp.CBOESource().fetch("T")
        assert d.price == 26.06 and not d.defer and d.price_source == "cboe_close"
