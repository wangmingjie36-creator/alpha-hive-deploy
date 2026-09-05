"""Twelve Data 日线一轮扫描只取一次（v0.45.125）

固化的事实：2026-09-04 蜂群段 30 只（`fetch_daily_closes(end_date=None)`）与尾段
17 只（`fetch_bars(end_date=as_of)`）取的是**同一份**数据（`_fetch_rows` 对显式
end_date 也过 `_drop_forming_bar`），却因为缓存键不同各发一次，每次 8.6s 串行。

这里守五条：
  1. 键归一：`None` 与「美东当日」同键；显式过去日期仍是独立键
  2. 请求不变：`None` 调用方发的请求仍不带 end_date 参数（键归一只在缓存层）
  3. fetch_daily_closes / fetch_volume_ratio 走缓存，输出与旧的直接 `_fetch_rows` 逐值相同
  4. 同键并发只发一次请求
  5. 预热：顺序各取一次、单票失败不中断；未配置 key 不开线程；prefetch 会带 SPY 启动它
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import twelve_data as td

TODAY = "2026-09-04"


def _rows(n=120, base=100.0, vol=1_000_000.0):
    import datetime as dt
    d0 = dt.date(2026, 3, 1)
    out, d, i = [], d0, 0
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "close": base + i * 0.5, "vol": vol + (i % 7) * 1000})
            i += 1
        d += dt.timedelta(days=1)
    return out


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    td.clear_bars_cache()
    monkeypatch.setattr(td, "_limiter", None)
    monkeypatch.setattr(td, "_et_today", lambda: TODAY)
    monkeypatch.setattr(td, "api_key", lambda: "k")
    yield
    td.clear_bars_cache()


def _patch_fetch(monkeypatch, rows=None, delay=0.0):
    calls = []
    data = rows if rows is not None else _rows()

    def _f(ticker, days, end_date=None):
        calls.append((ticker, days, end_date))
        if delay:
            time.sleep(delay)
        return [dict(r) for r in data[-days:]]
    monkeypatch.setattr(td, "_fetch_rows", _f)
    return calls


# ───────────────────────────────────────────── 1+2. 键归一、请求不变
class TestKeyNormalisation:
    def test_none_and_today_share_one_fetch_either_order(self, monkeypatch):
        calls = _patch_fetch(monkeypatch)
        assert td.fetch_daily_closes("NVDA", days=60) is not None          # 蜂群段口径
        assert td.fetch_bars("NVDA", 120, end_date=TODAY)                  # 尾段口径
        assert len(calls) == 1, calls
        assert td.bars_cache_stats()["hits"] == 1

        td.clear_bars_cache(); calls.clear()
        assert td.fetch_bars("NVDA", 120, end_date=TODAY)
        assert td.fetch_daily_closes("NVDA", days=60) is not None
        assert len(calls) == 1, "反过来先尾段再蜂群段也只取一次"

    def test_none_request_still_carries_no_end_date(self, monkeypatch):
        calls = _patch_fetch(monkeypatch)
        td.fetch_daily_closes("NVDA")
        assert calls[0][2] is None, "键归一不许改请求：None 调用方仍不带 end_date"

    def test_explicit_past_date_is_a_separate_key(self, monkeypatch):
        calls = _patch_fetch(monkeypatch)
        td.fetch_daily_closes("NVDA")
        td.fetch_bars("NVDA", 120, end_date="2026-08-20")
        assert len(calls) == 2, "补跑的过去窗口末端不是今天，不能与最新窗口共用"

    def test_et_today_unavailable_falls_back_to_none_key(self, monkeypatch):
        monkeypatch.setattr(td, "_et_today", lambda: None)
        assert td._bars_key("NVDA", None) == ("NVDA", None)
        assert td._bars_key("NVDA", TODAY) == ("NVDA", TODAY)


# ───────────────────────────────────────────── 3. 消费方输出不变
class TestConsumersUnchanged:
    def test_fetch_daily_closes_equals_direct_tail(self, monkeypatch):
        rows = _rows(120)
        calls = _patch_fetch(monkeypatch, rows)
        closes = td.fetch_daily_closes("NVDA", days=60)
        assert closes == [r["close"] for r in rows[-60:]]
        assert calls[0][1] >= td.SHARED_BARS_WINDOW, "一次多要到共享窗口，尾段才能直接命中"

    def test_fetch_volume_ratio_equals_direct_formula(self, monkeypatch):
        rows = _rows(120)
        _patch_fetch(monkeypatch, rows)
        v = td.fetch_volume_ratio("NVDA", window=20)
        win = [r["vol"] for r in rows[-20:]]
        assert v["volume_ratio"] == pytest.approx(rows[-1]["vol"] / (sum(win) / 20))
        assert v["avg_volume"] == int(sum(win) / 20) and v["recent_volume"] == int(rows[-1]["vol"])

    def test_realized_vol_then_volume_then_tail_is_one_fetch(self, monkeypatch):
        """09-04 的三类消费方在一个进程里：RV（蜂群）、量比（蜂群）、fetch_bars（尾段）。"""
        calls = _patch_fetch(monkeypatch)
        assert td.realized_vol("NVDA") is not None
        assert td.fetch_volume_ratio("NVDA") is not None
        assert td.fetch_bars("NVDA", 120, end_date=TODAY)
        assert len(calls) == 1 and td.bars_cache_stats()["hits"] == 2

    def test_failure_not_cached_and_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "_fetch_rows", lambda *a, **k: None)
        assert td.fetch_daily_closes("NVDA") is None
        assert td.fetch_volume_ratio("NVDA") is None
        assert td._BARS_CACHE == {}


# ───────────────────────────────────────────── 4. 并发只发一次
class TestInflightDedupe:
    def test_concurrent_same_key_fetches_once(self, monkeypatch):
        calls = _patch_fetch(monkeypatch, delay=0.2)
        results = []

        def _go():
            results.append(td.fetch_bars("NVDA", 120))
        ths = [threading.Thread(target=_go) for _ in range(4)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=5)
        assert len(calls) == 1, f"同键 4 路并发发了 {len(calls)} 次请求"
        assert all(r and len(r) == 120 for r in results)
        s = td.bars_cache_stats()
        assert s["inflight_waits"] >= 1 and s["fetches"] == 1

    def test_different_keys_do_not_block_each_other_into_one(self, monkeypatch):
        calls = _patch_fetch(monkeypatch)
        td.fetch_bars("NVDA", 120); td.fetch_bars("TSLA", 120)
        assert len(calls) == 2


# ───────────────────────────────────────────── 5. 预热
class TestWarmer:
    def test_warm_sequential_once_each_and_survives_failure(self, monkeypatch):
        seen = []

        def _f(ticker, days, end_date=None):
            seen.append(ticker)
            if ticker == "BAD":
                raise OSError("boom")
            return _rows()[-days:]
        monkeypatch.setattr(td, "_fetch_rows", _f)
        out = td.warm_bars_cache(["NVDA", "BAD", "SPY"])
        assert seen == ["NVDA", "BAD", "SPY"]
        assert out == {"warmed": 2, "failed": 1}
        assert td.bars_cache_stats()["warmed"] == 2
        # 预热过的票，消费方零请求
        seen.clear()
        assert td.fetch_daily_closes("NVDA") is not None and seen == []

    def test_start_warmer_returns_thread_and_populates_cache(self, monkeypatch):
        _patch_fetch(monkeypatch)
        th = td.start_bars_warmer(["NVDA", "NVDA", "SPY"])      # 去重
        assert th is not None and th.daemon
        th.join(timeout=5)
        assert {k[0] for k in td._BARS_CACHE} == {"NVDA", "SPY"}

    def test_not_configured_or_empty_does_not_start(self, monkeypatch):
        calls = _patch_fetch(monkeypatch)
        monkeypatch.setattr(td, "api_key", lambda: "")
        assert td.start_bars_warmer(["NVDA"]) is None
        monkeypatch.setattr(td, "api_key", lambda: "k")
        assert td.start_bars_warmer([]) is None
        assert calls == []

    def test_prefetch_shared_data_starts_warmer_with_spy(self, monkeypatch):
        from swarm_agents import base as B
        got = {}
        monkeypatch.setattr(td, "start_bars_warmer", lambda tickers, *a, **k: got.setdefault("t", list(tickers)))
        monkeypatch.setattr(B._cache, "_fetch_stock_data", lambda t, d=None: {"price": 1.0})
        monkeypatch.setattr(B, "prefetch_market_bundle", lambda tickers: {})
        B.prefetch_shared_data(["NVDA", "TSLA"], retriever=None, target_date=None)
        assert got["t"] == ["NVDA", "TSLA", "SPY"]
