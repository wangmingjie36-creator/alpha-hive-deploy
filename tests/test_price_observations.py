"""行情原始日线入档（v0.45.580，buzz_v1 根治阶段 1b，`observation_layer`）

背景
----
568 存了四个情绪通道的原始取数；三个行情通道（动量 / 量比 / 波动率）用的是生产行情代码算好的三个数——生产改行情代码，
buzz_v1 的输入就跟着变（窗口期内已改 5 次）。本版把主路径的原始输入（yfinance 日线 + 判末根用的交易所时钟）在取数处
原样落档，由算出字段的那条路径自己登记用了哪几份；回落补上的字段登记「用了但没存」（下一步补）。**只存、不改值。**

本文件钉住
----------
1. DataFrame ⇄ 原始内容逐位往返（列、dtype、带时区时间戳、NaN）。
2. 观测层只取数、只落档：返回的 DataFrame / 时钟与直接调 yfinance 逐位相同；异常照抛并记事件；时钟取不到也是一份观测。
3. 三条路径（CBOE 胜出 / yfinance 源 / `--date` 补跑）各自登记观测；回落补上的字段登记 None；不为登记多发 SPY 请求。
4. **存下的内容足够重算**：拿档案里的日线 + 时钟替身网络重跑同一段代码，三个字段逐位相同——含「末根还在形成」与提前收盘日。
5. BuzzBee 溯源块的 `price` 一项与完整性统计。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

import data_pipeline as dp
import observation_archive as oa
import observation_layer as ol
from bars_integrity import trading_days

D = "2026-10-21"


def _frame(last_day: dt.date, n=22, nan_at=None, int_volume=True, all_nan_volume=False):
    days = trading_days(last_day - dt.timedelta(days=60), last_day)[-n:]
    closes = [100.0 + i * 0.7 + (i % 3) * 0.9 for i in range(n)]
    vols = [1_000_000 + (i % 5) * 150_000 for i in range(n)]
    df = pd.DataFrame({"Open": [c - 0.5 for c in closes], "High": [c + 1 for c in closes],
                       "Low": [c - 1 for c in closes], "Close": closes,
                       "Volume": np.array(vols, dtype="int64" if int_volume else "float64"),
                       "Dividends": [0.0] * n, "Stock Splits": [0.0] * n},
                      index=pd.DatetimeIndex(days, name="Date").tz_localize("America/New_York"))
    if nan_at is not None:
        df.iloc[nan_at, df.columns.get_loc("Close")] = np.nan
    if all_nan_volume:   # 限流日的形状（`_fetch_history_metrics` 的「均量不可得」分支）
        df["Volume"] = np.full(n, np.nan)
    return df


def _et(d: dt.date, hh: int, mm: int):
    return pd.Timestamp(dt.datetime.combine(d, dt.time(hh, mm)), tz="America/New_York")


@pytest.fixture
def yf(monkeypatch):
    """yfinance.Ticker 打桩：日线回 `frame`，SPY 分钟线回 `minute`（或抛）；记下每次请求。"""
    import yfinance
    box = {"frame": None, "minute": None, "exc": None, "calls": []}

    class _T:
        def __init__(self, sym, *_a, **_k):
            self.sym = sym

        def history(self, **kw):
            box["calls"].append((self.sym, kw))
            if box["exc"] is not None:
                raise box["exc"]
            if kw.get("interval") == "1m":
                m = box["minute"]
                if isinstance(m, Exception):
                    raise m
                return m if m is not None else pd.DataFrame()
            return box["frame"]

    monkeypatch.setattr(yfinance, "Ticker", _T)
    monkeypatch.setattr(dp, "_EXCHANGE_NOW_CACHE", None)
    return box


@pytest.fixture
def on():
    oa.enable(D, "live")


def _minute(ts):
    return pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex([ts]))


# ══════════════════════════════════════════════════════════════════════════
# 1. 往返
# ══════════════════════════════════════════════════════════════════════════

class TestFrameRoundTrip:
    @pytest.mark.parametrize("kw", [{}, {"nan_at": 7}, {"int_volume": False}, {"n": 3}, {"all_nan_volume": True}])
    def test_exact_round_trip(self, kw):
        df = _frame(dt.date(2026, 10, 20), **kw)
        back = ol.frame_from_payload(ol.frame_payload("NVDA", {"period": "1mo"}, df))
        pd.testing.assert_frame_equal(back, df, check_exact=True)
        assert list(back.dtypes) == list(df.dtypes) and str(back.index.tz) == "America/New_York"

    def test_empty_frame(self):
        df = pd.DataFrame({"Close": [], "Volume": []})
        p = ol.frame_payload("X", {"period": "1mo"}, df)
        assert p["data"] == [] and len(ol.frame_from_payload(p)) == 0

    def test_payload_is_addressable_even_with_nan(self):
        """NaN 写成 null ⇒ 规范 JSON 拒 NaN 也照样有地址（否则 NaN 日的观测会整份丢）。"""
        p = ol.frame_payload("X", {"period": "1mo"}, _frame(dt.date(2026, 10, 20), nan_at=3))
        assert oa.is_address(oa.address(ol.KIND_DAILY, p))


# ══════════════════════════════════════════════════════════════════════════
# 2. 观测层只取数、只落档
# ══════════════════════════════════════════════════════════════════════════

class TestLayerFetches:
    def test_returns_the_same_frame_and_archives_it(self, yf, on):
        df = _frame(dt.date(2026, 10, 20))
        yf["frame"] = df
        got, ref = ol.yf_history("NVDA", period="1mo")
        assert got is df, "返回的就是 yfinance 给的那个对象，没复制、没改"
        doc = oa.load(ref)
        assert doc["kind"] == ol.KIND_DAILY and doc["payload"]["request"] == {"period": "1mo"}
        pd.testing.assert_frame_equal(ol.frame_from_payload(doc["payload"]), df, check_exact=True)

    def test_exception_is_reraised_and_recorded(self, yf, on):
        yf["exc"] = RuntimeError("Too Many Requests")
        with pytest.raises(RuntimeError):
            ol.yf_history("NVDA", period="1mo")
        ev = [e for e in _events() if e["kind"] == ol.KIND_DAILY]
        assert ev and ev[0]["status"] == "error" and "Too Many Requests" in ev[0]["message"]

    def test_clock_is_an_observation_hit_or_miss(self, yf, on):
        ts = _et(dt.date(2026, 10, 20), 15, 59)
        yf["minute"] = _minute(ts)
        assert ol.spy_last_minute() == ts
        assert oa.load(ol.clock_ref(ts))["payload"] == {"spy_last_minute": ts.isoformat()}
        yf["minute"] = RuntimeError("boom")
        assert ol.spy_last_minute() is None, "异常不外抛，同改前 `_exchange_now` 的 except: pass"
        assert oa.load(ol.clock_ref(None))["payload"] == {"spy_last_minute": None}

    def test_exchange_now_goes_through_the_layer_and_keeps_its_value(self, yf, on):
        ts = _et(dt.date(2026, 10, 20), 15, 59)
        yf["minute"] = _minute(ts)
        assert dp._exchange_now() == ts
        assert any(e["kind"] == ol.KIND_CLOCK and e["status"] == "ok" for e in _events())


def _events():
    p = oa.events_path(D)
    import json
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ══════════════════════════════════════════════════════════════════════════
# 3. 三条路径各自登记
# ══════════════════════════════════════════════════════════════════════════

FIELDS = ("momentum_5d", "volume_ratio", "avg_volume", "volatility_20d")


class TestPathsRegister:
    def test_cboe_path_metrics(self, yf, on):
        day = dt.date(2026, 10, 20)
        yf["frame"], yf["minute"] = _frame(day), _minute(_et(day, 15, 59))
        m = dp._fetch_history_metrics("NVDA")
        obs = m["_observations"]
        assert set(obs) == {"yf_daily", "exchange_clock"} and all(oa.is_address(v) for v in obs.values())
        assert oa.load(obs["exchange_clock"])["payload"]["spy_last_minute"].startswith("2026-10-20T15:59")

    def test_cboe_source_end_to_end(self, yf, on, monkeypatch):
        """生产常态：CBOE 胜出、历史字段来自 yfinance 日线——观测随 StockData 出口进 to_dict。"""
        import cboe_options as co
        from datetime import datetime
        day = dt.date(2026, 10, 20)
        yf["frame"], yf["minute"] = _frame(day), _minute(_et(day, 15, 59))
        monkeypatch.setattr(co, "_fetch_cboe_payload", lambda t, timeout=15: {
            "symbol": "NVDA", "close": 120.0, "current_price": 120.5, "last_trade_time": "2026-10-20T15:59:58"})
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 10, 20, 17, 30))
        f = dp.MultiSourceFetcher()
        f._sources = [dp.CBOESource()]
        out = f.fetch("NVDA")
        assert out["price_source"] == "cboe_close", "前提：CBOE 胜出"
        assert set(out["observations"]) == {"yf_daily", "exchange_clock"}
        assert all(oa.is_address(v) for v in out["observations"].values())

    def test_yfinance_source(self, yf, on):
        day = dt.date(2026, 10, 20)
        yf["frame"], yf["minute"] = _frame(day), _minute(_et(day, 15, 59))
        data = dp.YFinanceSource().fetch("NVDA")
        assert set(data.observations) == {"yf_daily", "exchange_clock"}
        assert data.to_dict()["observations"] == data.observations

    def test_no_extra_spy_request_for_short_history(self, yf, on):
        """不足 3 根时 `_drop_forming_bar` 不看时钟 ⇒ 不登记时钟，也不为登记多发一次 SPY 请求。"""
        yf["frame"] = _frame(dt.date(2026, 10, 20), n=2)
        dp._fetch_history_metrics("NVDA")
        assert [c for c in yf["calls"] if c[0] == "SPY"] == []

    def test_fallback_filled_fields_register_a_gap(self, yf, on, monkeypatch):
        """yfinance 只给 3 根（动量 / 波动率都算不出）⇒ 回落补上的字段登记 None（用了但没存）。"""
        day = dt.date(2026, 10, 20)
        yf["frame"], yf["minute"] = _frame(day, n=3), _minute(_et(day, 15, 59))
        monkeypatch.setattr(dp, "_fill_momentum_from_index", lambda t, out, session_close=None: {
            "momentum_5d": 1.0, "momentum_as_of": "2026-10-20"})
        monkeypatch.setattr(dp, "_fill_volatility_from_twelvedata", lambda t, out, session_close=None: {
            "volatility_20d": 30.0, "volatility_as_of": "2026-10-20"})
        monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda t, out: out)
        f = dp.MultiSourceFetcher()
        f._sources = [dp.YFinanceSource()]
        out = f.fetch("NVDA")
        assert out["observations"]["price_index"] is None and out["observations"]["td_adjusted"] is None
        assert oa.is_address(out["observations"]["yf_daily"])
        blk = oa.buzz_refs(reddit=None, news=None, yahoo=None, fear_greed=None, price=out)["price"]
        assert blk == {"expected": True, "ref": None, "gaps": ["price_index", "td_adjusted"]}

    def test_gap_marking_does_not_leak_into_shallow_copies(self, on, monkeypatch):
        """StockData 在缓存路径上是浅拷贝：登记缺口要换新 dict，不能改到源对象。"""
        import copy
        base = dp.StockData(price=10.0, data_source=dp.DataQuality.DEGRADED, source_name="finnhub",
                            momentum_source="unavailable")
        c = copy.copy(base)
        monkeypatch.setattr(dp, "_fill_momentum_from_index", lambda t, out, session_close=None: {"momentum_5d": 1.0})
        monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda t, out: out)
        monkeypatch.setattr(dp, "_fill_volatility_from_twelvedata", lambda t, out, session_close=None: out)
        dp.reset_history_fill_stats()
        dp._fill_history_fields("X", c, "test")
        dp.reset_history_fill_stats()
        assert c.observations == {"price_index": None} and base.observations == {}

    def test_backfill_path(self, yf, on):
        day = dt.date(2026, 10, 16)
        yf["frame"] = _frame(day, n=44)
        out = dp._fetch_historical_stock_data("NVDA", day.isoformat())
        assert set(out["observations"]) == {"yf_daily"} and oa.is_address(out["observations"]["yf_daily"])
        assert oa.load(out["observations"]["yf_daily"])["payload"]["request"]["start"] == "2026-08-17"
        assert [c for c in yf["calls"] if c[0] == "SPY"] == [], "补跑不看交易所时钟"


# ══════════════════════════════════════════════════════════════════════════
# 4. 存下的内容足够重算
# ══════════════════════════════════════════════════════════════════════════

def _replay(monkeypatch, refs):
    """把网络换成档案内容：日线回 `yf_daily` 那份、时钟固定为 `exchange_clock` 那份。"""
    import yfinance
    frame = ol.frame_from_payload(oa.load(refs["yf_daily"])["payload"])
    clock = None
    if "exchange_clock" in refs:
        iso = oa.load(refs["exchange_clock"])["payload"]["spy_last_minute"]
        clock = pd.Timestamp(iso) if iso else None

    class _T:
        def __init__(self, *_a, **_k):
            pass

        def history(self, **_k):
            return frame

    monkeypatch.setattr(yfinance, "Ticker", _T)
    monkeypatch.setattr(dp, "_exchange_now", lambda: clock)


class TestSufficiency:
    @pytest.mark.parametrize("day, hh, mm, label", [
        (dt.date(2026, 10, 20), 15, 59, "收盘后：末根是当天、保留"),
        (dt.date(2026, 10, 20), 11, 30, "盘中：末根还在形成、丢掉"),
        (dt.date(2026, 11, 27), 12, 59, "提前收盘日收盘后：保留"),
        (dt.date(2026, 11, 27), 11, 0, "提前收盘日盘中：丢掉"),
    ])
    def test_metrics_recomputed_from_the_archive(self, yf, on, monkeypatch, day, hh, mm, label):
        yf["frame"], yf["minute"] = _frame(day, n=23), _minute(_et(day, hh, mm))
        first = dp._fetch_history_metrics("NVDA")
        refs = first["_observations"]
        _replay(monkeypatch, refs)
        again = dp._fetch_history_metrics("NVDA")
        for k in FIELDS + ("_price_from_hist",):
            assert again[k] == first[k], (label, k)

    def test_yfinance_source_recomputed(self, yf, on, monkeypatch):
        day = dt.date(2026, 10, 20)
        yf["frame"], yf["minute"] = _frame(day, n=22, nan_at=None), _minute(_et(day, 15, 59))
        first = dp.YFinanceSource().fetch("NVDA")
        _replay(monkeypatch, first.observations)
        again = dp.YFinanceSource().fetch("NVDA")
        for k in FIELDS + ("price", "session_close"):
            assert getattr(again, k) == getattr(first, k), k

    def test_backfill_recomputed(self, yf, on, monkeypatch):
        day = dt.date(2026, 10, 16)
        yf["frame"] = _frame(day, n=44)
        first = dp._fetch_historical_stock_data("NVDA", day.isoformat())
        _replay(monkeypatch, first["observations"])
        again = dp._fetch_historical_stock_data("NVDA", day.isoformat())
        for k in FIELDS + ("price",):
            assert again[k] == first[k], k


# ══════════════════════════════════════════════════════════════════════════
# 5. 不改值
# ══════════════════════════════════════════════════════════════════════════

class TestValueNeutral:
    def test_archive_on_or_off_same_values(self, yf, monkeypatch):
        day = dt.date(2026, 10, 20)
        yf["frame"], yf["minute"] = _frame(day), _minute(_et(day, 15, 59))
        off = dp._fetch_history_metrics("NVDA")
        monkeypatch.setattr(dp, "_EXCHANGE_NOW_CACHE", None)
        oa.enable(D, "live")
        on_ = dp._fetch_history_metrics("NVDA")
        assert {k: v for k, v in off.items() if k != "_observations"} == \
               {k: v for k, v in on_.items() if k != "_observations"}
        assert off["_observations"] == on_["_observations"], "地址只取决于内容，与开不开档无关"


# ══════════════════════════════════════════════════════════════════════════
# 6. 溯源块与完整性
# ══════════════════════════════════════════════════════════════════════════

class TestBuzzPriceBlock:
    def _refs(self):
        s = oa.reuse(ol.KIND_CLOCK, {"spy_last_minute": None})
        return {"yf_daily": s, "exchange_clock": s}

    def test_complete(self, on):
        blk = oa.buzz_refs(reddit=None, news=None, yahoo=None, fear_greed=None,
                           price={"momentum_5d": 1.0, "observations": self._refs()})["price"]
        assert blk == {"expected": True, "ref": self._refs()}

    def test_no_values_means_not_expected(self):
        blk = oa.buzz_refs(reddit=None, news=None, yahoo=None, fear_greed=None,
                           price={"momentum_5d": None, "volume_ratio": None, "volatility_20d": None})["price"]
        assert blk == {"expected": False, "ref": None}

    def test_summarize_rejects_a_partial_price_ref(self, on):
        """`buzz_refs` 不会写出半截的 ref，但完整性统计不信上游：ref 里有一项不是地址 ⇒ 整块计缺失。"""
        s_ok = self._refs()["yf_daily"]
        tr = {"agent_details": {"BuzzBeeWhisper": {"details": {"observations": {
            "price": {"expected": True, "ref": {"yf_daily": s_ok, "td_adjusted": None}}}}}}}
        assert oa.summarize({"A": tr})["missing_refs"]["price"] == 1

    def test_summarize_counts_price_gaps(self, on):
        def tr(price_obs):
            return {"agent_details": {"BuzzBeeWhisper": {"details": {"observations": {
                "_layer": oa.LAYER_VERSION, "price": price_obs}}}}}
        ok = {"expected": True, "ref": self._refs()}
        gap = {"expected": True, "ref": None, "gaps": ["td_adjusted"]}
        s = oa.summarize({"A": tr(ok), "B": tr(gap)})
        assert s["missing_refs"]["price"] == 1 and s["expected"]["price"] == 2 and not s["ok"]
        assert any("缺 td_adjusted" in e for e in s["examples"])
