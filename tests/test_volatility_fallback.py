"""yfinance 缺 volatility_20d 时，用 Twelve Data **复权**日线按同一公式补上（v0.45.557）

为什么要复权（2026-10-19 实测，auto-memory `alpha-hive-volatility-fallback-adjustment.md`）：生产的波动率出自
yfinance `history()`，缺省 `auto_adjust=True`（分红复权）；Twelve Data 缺省参数给的是**未复权**收盘
（VZ / T / ABBV / DE 除息日前 149/149 根贴 yfinance 未复权 Close），`adjust=all` 才贴复权价。
影子对比（归档 926 行）：正常日 742 行跨档 0、最大差 0.062。

收盘后的生产扫描里 TD 当日那根当晚是临时值、被规则 ① 丢掉（**对的，勿改**）⇒ TD 止于 D-1；窗口末端 D 只能
接源核实过的官方收盘（`session_close`）。末端对不上 / 判不了 ⇒ 拒收、`still_missing` ⇒ P2。

走真实的 HTTP 解析 + `_drop_forming_bar` + 缓存，只替换 HTTP 层。
⚠️ 被测路径读**两只钟**：`twelve_data._et_today`（规则 ①、缓存键）与 `portfolio_greeks._et_now`
（`_latest_closed_session`）。`_freeze` 两只一起钉在同一刻。
"""

import datetime as dt
import json
import logging
import math
import sys
import urllib.parse
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import data_pipeline as dp
import portfolio_greeks as pg
import twelve_data as td
from bars_integrity import trading_days

ET = ZoneInfo("America/New_York")

#: 真实金值：DELL 2026-10-16。收盘价是 10-19 扫描后取的 TD `adjust=all`；生产归档 `price.volatility_20d`
#: = 60.03548236314658（yfinance 主路径），恰好在 60 那一档的上沿之上（Buzz 通道 25）。
DELL_ITEMS = [
    ("2026-08-25", 451.5), ("2026-08-26", 463.82001), ("2026-08-27", 472.26001), ("2026-08-28", 456.23999),
    ("2026-08-31", 456.01001), ("2026-09-01", 425.0), ("2026-09-02", 492.20001), ("2026-09-03", 516.39001),
    ("2026-09-04", 524.14001), ("2026-09-08", 533.88), ("2026-09-09", 535.25), ("2026-09-10", 506.62),
    ("2026-09-11", 567.28998), ("2026-09-14", 534.28003), ("2026-09-15", 543.51001), ("2026-09-16", 563.28998),
    ("2026-09-17", 588.40002), ("2026-09-18", 568.059998), ("2026-09-21", 575.31), ("2026-09-22", 548.91998),
    ("2026-09-23", 549.83002), ("2026-09-24", 536.02002), ("2026-09-25", 562.89001), ("2026-09-28", 543.42999),
    ("2026-09-29", 539.59003), ("2026-09-30", 537.95001), ("2026-10-01", 541.73999), ("2026-10-02", 562.52002),
    ("2026-10-05", 552.28998), ("2026-10-06", 574.0), ("2026-10-07", 578.96002), ("2026-10-08", 574.54999),
    ("2026-10-09", 586.059998), ("2026-10-12", 576.76001), ("2026-10-13", 572.46997), ("2026-10-14", 507.92999),
    ("2026-10-15", 473.39001), ("2026-10-16", 451.70999),
]
DELL_ARCHIVED = 60.03548236314658


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    td.reset_stats()
    td.clear_bars_cache()
    dp.reset_history_fill_stats()
    monkeypatch.setattr(td, "_limiter", None)
    monkeypatch.setattr(td, "api_key", lambda: "k")
    yield
    td.reset_stats()
    td.clear_bars_cache()
    dp.reset_history_fill_stats()


def _freeze(monkeypatch, y, m, d, hh, mm):
    now = dt.datetime(y, m, d, hh, mm, tzinfo=ET)
    monkeypatch.setattr(td, "_et_today", lambda: now.date().isoformat())
    monkeypatch.setattr(pg, "_et_now", lambda: now)
    return now


class _HTTP:
    """只换 HTTP 层：每次请求记下查询参数，回给定的日线（成交量恒定，规则 ② 不误伤）。"""

    def __init__(self, monkeypatch, items, fail=False):
        import http_gate
        self.params = []
        body = json.dumps({"values": [{"datetime": d, "close": str(c), "volume": "5000000"}
                                      for d, c in items]}).encode()

        def _open(req, *_a, **_k):
            self.params.append(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query)))
            if fail:
                return json.dumps({"status": "error", "code": 429, "message": "quota"}).encode()
            return body

        monkeypatch.setattr(http_gate, "urlopen_gated", _open)


def _items(last_day: dt.date, n=40, seed=1.7):
    """截至 `last_day`（含）的 n 个交易日的合成收盘（确定性的起伏，窗口边界上一根就能改变结果）。"""
    days = trading_days(last_day - dt.timedelta(days=90), last_day)[-n:]
    assert days[-1] == last_day and len(days) == n
    px, out = 100.0, []
    for i, day in enumerate(days):
        px *= 1 + 0.02 * math.sin(i * seed)
        out.append((day.isoformat(), round(px, 4)))
    return out


def _pandas_vol(closes):
    """生产 yfinance 主路径的写法（`returns.std()` 缺省 ddof=1）。"""
    return float(pd.Series(closes).pct_change().dropna().std() * (252 ** 0.5) * 100)


def _window(items, lo, s, end_close=None):
    closes = [c for d, c in items if lo < d <= s]
    if end_close is not None:
        closes = [c for d, c in items if lo < d < s] + [end_close]
    return closes


def _fill(ticker="NVDA", session_close=None):
    data = dp.StockData(price=100.0, momentum_5d=1.0, volume_ratio=1.0, source_name="test",
                        session_close=session_close)
    dp._fill_history_fields(ticker, data, "source:test")
    return data


# ───────────────────────────── 1. 与主路径同一个量
class TestSameQuantityAsMainPath:
    def test_formula_equals_yfinance_main_path_on_same_closes(self, monkeypatch):
        """同一串收盘喂给生产 `_fetch_history_metrics`（yfinance 主路径）与回落：逐位一致（1e-9）。"""
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)                     # 盘中：应有场次 10-15，TD 末根即 10-15
        items = _items(dt.date(2026, 10, 15))
        _HTTP(monkeypatch, items)
        data = _fill()
        closes = _window(items, "2026-09-15", "2026-10-15")

        import yfinance
        frame = pd.DataFrame({"Close": closes, "Volume": [1e6] * len(closes)},
                             index=pd.to_datetime([d for d, _ in items if "2026-09-15" < d <= "2026-10-15"]))

        class _T:
            def __init__(self, *_a, **_k):
                pass

            def history(self, **_k):
                return frame

        monkeypatch.setattr(yfinance, "Ticker", _T)
        monkeypatch.setattr(dp, "_exchange_now", lambda: None)
        main = dp._fetch_history_metrics("NVDA")["volatility_20d"]
        assert data.volatility_20d == pytest.approx(main, abs=1e-9)
        assert data.volatility_20d == pytest.approx(_pandas_vol(closes), abs=1e-9)

    def test_real_dell_day_reproduces_production(self, monkeypatch):
        """真实金值：10-16 收盘后的生产扫描时刻，TD 止于 10-15、接核实的 10-16 收盘 ⇒ 等于生产归档的 yfinance 值。"""
        _freeze(monkeypatch, 2026, 10, 16, 17, 15)
        _HTTP(monkeypatch, DELL_ITEMS)                                # TD 当晚连 10-16 的临时那根也给了（规则 ① 丢）
        data = _fill("DELL", session_close=("2026-10-16", 451.70999))
        assert data.volatility_20d == pytest.approx(DELL_ARCHIVED, abs=1e-5)
        assert data.volatility_20d > 60 and DELL_ARCHIVED > 60, "同在 60 以上那一档（Buzz 波动率通道 25）"
        assert (data.volatility_source, data.volatility_as_of) == ("twelvedata_adjusted", "2026-10-16")
        d = data.to_dict()
        assert (d["volatility_source"], d["volatility_as_of"]) == ("twelvedata_adjusted", "2026-10-16")


# ───────────────────────────── 2. 收盘后的生产扫描：TD 止于 D-1，末端只能接核实收盘
class TestAfterCloseProductionScan:
    D = dt.date(2026, 10, 16)

    def test_uses_verified_close_not_td_provisional_bar(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 17, 15)
        items = _items(self.D)
        items[-1] = (items[-1][0], round(items[-1][1] * 1.05, 4))   # TD 当晚那根是临时值：故意差 5%
        _HTTP(monkeypatch, items)
        official = _items(self.D)[-1][1]
        data = _fill(session_close=("2026-10-16", official))
        expect = _pandas_vol(_window(items, "2026-09-16", "2026-10-16", end_close=official))
        assert data.volatility_20d == pytest.approx(expect, abs=1e-9)
        assert data.volatility_as_of == "2026-10-16"
        assert dp.history_fill_stats()["by_path"]["source:test"]["volatility"] == {"needed": 1, "filled": 1, "errors": 0}

    def test_without_verified_close_rejects_and_someone_goes_red(self, monkeypatch, caplog):
        _freeze(monkeypatch, 2026, 10, 16, 17, 15)
        _HTTP(monkeypatch, _items(self.D))
        with caplog.at_level(logging.WARNING, logger="alpha_hive.data_pipeline"):
            data = _fill()
        assert data.volatility_20d is None and data.volatility_source is None and data.volatility_as_of is None
        msg = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "NVDA" in msg and "2026-10-15" in msg and "2026-10-16" in msg and "拒收" in msg
        hf = dp.history_fill_stats()
        assert hf["still_missing"]["volatility"] == ["NVDA"]
        assert hf["by_path"]["source:test"]["volatility"] == {"needed": 1, "filled": 0, "errors": 0}

    def test_verified_close_of_another_day_rejects(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 17, 15)
        _HTTP(monkeypatch, _items(self.D))
        assert _fill(session_close=("2026-10-15", 100.0)).volatility_20d is None

    def test_td_missing_previous_session_rejects(self, monkeypatch):
        """TD 缺 D-1（末根 D-2）：接上 D 会让窗口少一根、跨两天算一个收益 ⇒ 拒收。"""
        _freeze(monkeypatch, 2026, 10, 16, 17, 15)
        _HTTP(monkeypatch, _items(dt.date(2026, 10, 14)))
        assert _fill(session_close=("2026-10-16", 100.0)).volatility_20d is None

    def test_early_close_after_holiday(self, monkeypatch):
        """感恩节（11-26 休市）次日 13:00 收盘：14:00 应有场次 11-27，TD 止于 11-25 = 前一交易日 ⇒ 接核实收盘。"""
        _freeze(monkeypatch, 2026, 11, 27, 14, 0)
        items = _items(dt.date(2026, 11, 27))
        _HTTP(monkeypatch, items)
        data = _fill(session_close=("2026-11-27", items[-1][1]))
        assert data.volatility_as_of == "2026-11-27"
        assert data.volatility_20d == pytest.approx(_pandas_vol(_window(items, "2026-10-27", "2026-11-27")), abs=1e-9)

    def test_suspected_unapplied_split_is_rejected(self, monkeypatch, caplog):
        """v0.45.562：复权历史接原始核实收盘的前提是 TD 当晚已把 S 当天的公司行动计入前面的价（待验证）。没计入时拆股
        造出 −50% 的假收益（波动率 ~190%、跨到最高档）——接上那天变动 > 30% 就拒收。"""
        _freeze(monkeypatch, 2026, 10, 16, 17, 15)
        items = _items(self.D)
        _HTTP(monkeypatch, items)
        prev = items[-2][1]                                           # TD 止于 10-15（10-16 临时那根被规则 ① 丢）
        with caplog.at_level(logging.WARNING, logger="alpha_hive.data_pipeline"):
            assert _fill(session_close=("2026-10-16", round(prev * 0.5, 4))).volatility_20d is None
        assert any("公司行动" in r.getMessage() for r in caplog.records)
        assert _fill("TSLA", session_close=("2026-10-16", round(prev * 1.25, 4))).volatility_20d is not None, \
            "+25% 的真行情照收（阈值是 30%）"

    def test_request_carries_adjust_all_and_shared_bars_do_not(self, monkeypatch):
        """复权只在这条请求上；共享日线（close_correction 等读者对官方原始收盘）的请求逐字不变。"""
        _freeze(monkeypatch, 2026, 10, 16, 17, 15)
        http = _HTTP(monkeypatch, _items(self.D))
        _fill(session_close=("2026-10-16", 100.0))
        assert http.params[-1]["adjust"] == "all" and http.params[-1]["outputsize"] == str(td.ADJUSTED_WINDOW)
        td.fetch_bars("NVDA")
        assert "adjust" not in http.params[-1]
        assert td.bars_cache_stats()["entries"] == 1 and td.bars_cache_stats()["adjusted"]["entries"] == 1, \
            "两份缓存互不串（未复权那份不被复权值占位）"


# ───────────────────────────── 3. 盘中 / 盘前 / 周末 / 午夜后：S 是前一场，TD 已定稿
class TestOffHoursUsesSettledBar:
    @pytest.mark.parametrize("frozen, served_last, want", [
        ((2026, 10, 16, 11, 0), dt.date(2026, 10, 16), "2026-10-15"),    # 盘中：TD 给了半根（丢），末根 10-15
        ((2026, 10, 16, 8, 0), dt.date(2026, 10, 15), "2026-10-15"),     # 盘前
        ((2026, 10, 15, 0, 30), dt.date(2026, 10, 14), "2026-10-14"),    # 周四过了美东午夜：周三那根已不是「当日」
        ((2026, 10, 18, 10, 0), dt.date(2026, 10, 16), "2026-10-16"),    # 周日 ⇒ 周五
    ], ids=["intraday", "premarket", "after_midnight", "weekend"])
    def test_accepts_without_verified_close(self, monkeypatch, frozen, served_last, want):
        _freeze(monkeypatch, *frozen)
        items = _items(served_last)
        _HTTP(monkeypatch, items)
        assert dp._latest_closed_session() == want
        data = _fill()
        lo = (dt.date.fromisoformat(want).replace(month=dt.date.fromisoformat(want).month - 1)).isoformat()
        assert data.volatility_as_of == want
        assert data.volatility_20d == pytest.approx(_pandas_vol(_window(items, lo, want)), abs=1e-9)


# ───────────────────────────── 4. 窗口 = (S − 1 个日历月, S]，≥ 20 根
class TestWindow:
    def test_left_end_is_exclusive(self, monkeypatch):
        """S = 10-15 ⇒ 左端 09-15（周二，有那根）不进窗口——它进不进会改变结果（判别）。"""
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        items = _items(dt.date(2026, 10, 15))
        _HTTP(monkeypatch, items)
        got = _fill().volatility_20d
        excl = _pandas_vol(_window(items, "2026-09-15", "2026-10-15"))
        incl = _pandas_vol([c for d, c in items if "2026-09-15" <= d <= "2026-10-15"])
        assert "2026-09-15" in dict(items) and abs(excl - incl) > 1e-3, "前提：左端那根确实在、且会改变结果"
        assert got == pytest.approx(excl, abs=1e-9)

    def test_month_end_clamps_to_last_day_of_previous_month(self, monkeypatch):
        """03-31 ⇒ 2 月没有 31 日，左端取 02-28（不抛、不跳过）。"""
        _freeze(monkeypatch, 2026, 3, 31, 17, 15)
        items = _items(dt.date(2026, 3, 31))
        _HTTP(monkeypatch, items)
        data = _fill(session_close=("2026-03-31", items[-1][1]))
        assert data.volatility_20d == pytest.approx(_pandas_vol(_window(items, "2026-02-28", "2026-03-31")), abs=1e-9)

    def test_month_end_clamp_is_exact_on_a_trading_day(self, monkeypatch):
        """v0.45.562 补判别：12-31 ⇒ 11 月没有 31 日，左端取 11-30（周一、有那根）且不含它。上一条的 02-28 是周六，
        左端取 02-27~03-01 结果都一样、判别不出；这里取错一天（如只试 28 日 ⇒ 11-28）会把 11-30 算进窗口。"""
        _freeze(monkeypatch, 2026, 12, 31, 17, 15)
        items = _items(dt.date(2026, 12, 31))
        _HTTP(monkeypatch, items)
        got = _fill(session_close=("2026-12-31", items[-1][1])).volatility_20d
        excl = _pandas_vol(_window(items, "2026-11-30", "2026-12-31"))
        incl = _pandas_vol([c for d, c in items if "2026-11-30" <= d <= "2026-12-31"])
        assert "2026-11-30" in dict(items) and abs(excl - incl) > 1e-3, "前提：11-30 那根确实在、且会改变结果"
        assert got == pytest.approx(excl, abs=1e-9)

    def test_january_rolls_back_into_previous_year(self, monkeypatch):
        _freeze(monkeypatch, 2026, 1, 15, 17, 15)
        items = _items(dt.date(2026, 1, 15))
        _HTTP(monkeypatch, items)
        got = _fill(session_close=("2026-01-15", items[-1][1])).volatility_20d
        assert got == pytest.approx(_pandas_vol(_window(items, "2025-12-15", "2026-01-15")), abs=1e-9)

    def test_month_with_only_19_sessions_sends_no_request(self, monkeypatch, caplog):
        """v0.45.562：(02-10, 03-10] 只有 19 个交易日（02-16 休市）——yfinance 主路径要 ≥ 20 根，那些天对所有标的
        都不出值（生产 03-10 归档全是旧哨兵 0.0）。回落同样不出值，且**不发请求**（此前 30 只各发一次、全被拒）。"""
        _freeze(monkeypatch, 2026, 3, 10, 17, 15)
        http = _HTTP(monkeypatch, _items(dt.date(2026, 3, 10)))
        with caplog.at_level(logging.WARNING, logger="alpha_hive.data_pipeline"):
            data = _fill(session_close=("2026-03-10", 100.0))
        assert data.volatility_20d is None and http.params == []
        assert any("只有 19 个交易日" in r.getMessage() for r in caplog.records)
        assert dp.history_fill_stats()["still_missing"]["volatility"] == ["NVDA"]

    def test_fewer_than_20_bars_rejects(self, monkeypatch, caplog):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        _HTTP(monkeypatch, _items(dt.date(2026, 10, 15), n=15))
        with caplog.at_level(logging.WARNING, logger="alpha_hive.data_pipeline"):
            assert _fill().volatility_20d is None
        assert any("需 ≥ 20" in r.getMessage() for r in caplog.records)


# ───────────────────────────── 5. 判不了就拒收，不猜
class TestUnjudgeable:
    def test_clock_unavailable_rejects(self, monkeypatch, caplog):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        _HTTP(monkeypatch, _items(dt.date(2026, 10, 15)))

        def _boom():
            raise RuntimeError("no tzdata")

        monkeypatch.setattr(pg, "_et_now", _boom)
        with caplog.at_level(logging.WARNING, logger="alpha_hive.data_pipeline"):
            assert _fill().volatility_20d is None
        assert any("NVDA" in r.getMessage() and "拒收" in r.getMessage() for r in caplog.records)

    def test_previous_session(self, monkeypatch):
        assert dp._previous_session("2026-10-16") == "2026-10-15"
        assert dp._previous_session("2026-10-19") == "2026-10-16"      # 周一 ⇒ 周五
        assert dp._previous_session("2026-11-27") == "2026-11-25"      # 感恩节休市
        monkeypatch.setitem(sys.modules, "is_trading_day", None)        # 日历 import 不进来
        assert dp._previous_session("2026-10-16") is None, "日历不可用时 _expected_session 会原样返回入参——必须先证可用"

    def test_calendar_unavailable_after_close_rejects(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 17, 15)
        _HTTP(monkeypatch, _items(dt.date(2026, 10, 16)))
        monkeypatch.setattr(dp, "_latest_closed_session", lambda: "2026-10-16")
        monkeypatch.setitem(sys.modules, "is_trading_day", None)
        assert _fill(session_close=("2026-10-16", 100.0)).volatility_20d is None

    def test_unconfigured_key_sends_nothing(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        http = _HTTP(monkeypatch, _items(dt.date(2026, 10, 15)))
        monkeypatch.setattr(td, "api_key", lambda: "")
        assert _fill().volatility_20d is None and http.params == []
        assert dp.history_fill_stats()["still_missing"]["volatility"] == ["NVDA"]

    def test_api_error_is_recorded_and_not_cached(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        http = _HTTP(monkeypatch, _items(dt.date(2026, 10, 15)), fail=True)
        assert _fill().volatility_20d is None
        # v0.45.562：带口径前缀——`options_analyzer` 按 `failed[标的]` 的 `network` 前缀决定共享日线重不重试
        assert td.bars_cache_stats()["failed"] == {"NVDA": "adjust=all:api_error:429"}
        assert _fill().volatility_20d is None and len(http.params) == 2, "失败不入缓存，下一次照常重试"

    def test_shared_bars_failure_reason_has_no_prefix(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        _HTTP(monkeypatch, _items(dt.date(2026, 10, 15)), fail=True)
        assert td.fetch_bars("NVDA") is None
        assert td.bars_cache_stats()["failed"] == {"NVDA": "api_error:429"}

    def test_fill_raising_is_counted_and_does_not_block_others(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("broken")

        monkeypatch.setattr(dp, "_fill_volatility_from_twelvedata", _boom)
        data = dp.StockData(price=1.0, momentum_5d=None, volume_ratio=None, source_name="t")
        monkeypatch.setattr(dp, "_fill_momentum_from_index", lambda t, out, session_close=None: {"momentum_5d": 2.0})
        monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda t, out: {"volume_ratio": 0.9, "avg_volume": 1})
        dp._fill_history_fields("NVDA", data, "source:t")
        assert data.momentum_5d == 2.0 and data.volume_ratio == 0.9 and data.volatility_20d is None
        assert dp.history_fill_stats()["by_path"]["source:t"]["volatility"]["errors"] == 1


# ───────────────────────────── 6. 缓存与计数
class TestCacheAndCounters:
    def test_one_request_per_ticker_per_day(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        http = _HTTP(monkeypatch, _items(dt.date(2026, 10, 15)))
        a, b = _fill(), _fill()
        assert a.volatility_20d == b.volatility_20d and len(http.params) == 1
        assert td.bars_cache_stats()["adjusted"] == {"fetches": 1, "hits": 1, "entries": 1}
        td.clear_bars_cache()
        assert td.bars_cache_stats()["adjusted"] == {"fetches": 0, "hits": 0, "entries": 0}

    def test_cache_is_keyed_by_adjust_mode(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        http = _HTTP(monkeypatch, _items(dt.date(2026, 10, 15)))
        td.fetch_adjusted_bars("NVDA", adjust="all")
        td.fetch_adjusted_bars("NVDA", adjust="splits")
        assert [p["adjust"] for p in http.params] == ["all", "splits"]

    def test_summary_line_lists_adjusted_requests(self):
        import scan_timing as stt
        base = {"fetches": 3, "hits": 1, "failures": 0, "failed": {}}
        with_adj = stt.summary_line({"phases": {}, "counters": {
            "twelve_data": {**base, "adjusted": {"fetches": 2, "hits": 0, "entries": 2}}}})
        without = stt.summary_line({"phases": {}, "counters": {
            "twelve_data": {**base, "adjusted": {"fetches": 0, "hits": 0, "entries": 0}}}})
        assert "请求3/命中1/复权请求2" in with_adj and "复权请求" not in without

    def test_returns_copies(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        _HTTP(monkeypatch, _items(dt.date(2026, 10, 15)))
        rows = td.fetch_adjusted_bars("NVDA")
        rows[-1]["close"] = -1.0
        assert td.fetch_adjusted_bars("NVDA")[-1]["close"] > 0

    def test_present_value_is_not_refetched(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 16, 11, 0)
        http = _HTTP(monkeypatch, _items(dt.date(2026, 10, 15)))
        data = dp.StockData(price=1.0, momentum_5d=1.0, volume_ratio=1.0, volatility_20d=33.3,
                            volatility_source="yfinance", source_name="t")
        dp._fill_history_fields("NVDA", data, "source:t")
        assert (data.volatility_20d, data.volatility_source) == (33.3, "yfinance") and http.params == []
        assert dp.history_fill_stats()["still_missing"]["volatility"] == []
