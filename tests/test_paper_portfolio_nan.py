"""NaN 不得进入 PaperPortfolio 的状态（v0.45.97）。

真实事故：2026-08-28 全站 HTTPS 断掉那天，TMUS 的 Close 是 NaN，却照样
触发了 TIME 止损 —— `_check_exit` 返回 ("TIME", NaN, d) → gross_pct NaN
→ pnl_usd NaN → `cash += size_usd + pnl` → **cash 从此永久 NaN**。
之后 8/28、8/31、9/1、9/2 四个扫描日的 NAV 全是 NaN，6 条新仓的
shares/size_usd 也全是 NaN（NaN 穿过了 `size_usd <= 1` 和
`entry_price <= 0` 两道守卫——NaN 的任何比较都返回 False）。
"""

import math
import types
import pytest

import paper_portfolio as pp


@pytest.fixture(autouse=True)
def _clear_caches():
    pp._PRICE_CACHE.clear()
    pp._OHLC_FULL.clear()
    yield
    pp._PRICE_CACHE.clear()
    pp._OHLC_FULL.clear()


def _fake_hist(rows):
    """构造一个最小的、能被 _fetch_ohlc 的 iterrows() 消费的对象。"""
    import pandas as pd
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows]},
        index=idx,
    )


def _patch_yf(monkeypatch, hist):
    fake = types.SimpleNamespace(
        Ticker=lambda t: types.SimpleNamespace(history=lambda **kw: hist))
    import sys
    monkeypatch.setitem(sys.modules, "yfinance", fake)


class TestFetchOhlcDropsNaN:

    def test_nan_bar_is_dropped(self, monkeypatch):
        _patch_yf(monkeypatch, _fake_hist([
            ("2026-08-27", 100.0, 101.0, 99.0, 100.5),
            ("2026-08-28", float("nan"), float("nan"), float("nan"), float("nan")),
            ("2026-08-31", 102.0, 103.0, 101.0, 102.5),
        ]))
        out = pp._fetch_ohlc("TMUS", "2026-08-26", "2026-09-01")
        assert "2026-08-28" not in out, "含 NaN 的 bar 必须被丢掉"
        assert "2026-08-27" in out and "2026-08-31" in out, "好的 bar 不能误伤"

    def test_partial_nan_also_dropped(self, monkeypatch):
        """只有 Close 是 NaN 也要整根丢 —— SL/TP 判据读的是 Low/High。"""
        _patch_yf(monkeypatch, _fake_hist([
            ("2026-08-28", 100.0, 101.0, 99.0, float("nan")),
            ("2026-08-31", 102.0, 103.0, 101.0, 102.5),
        ]))
        out = pp._fetch_ohlc("TMUS", "2026-08-26", "2026-09-01")
        assert "2026-08-28" not in out
        assert len(out) == 1

    def test_all_finite_passes_through(self, monkeypatch):
        _patch_yf(monkeypatch, _fake_hist([
            ("2026-08-27", 100.0, 101.0, 99.0, 100.5),
            ("2026-08-28", 101.0, 102.0, 100.0, 101.5),
        ]))
        out = pp._fetch_ohlc("TMUS", "2026-08-26", "2026-09-01")
        assert len(out) == 2
        assert all(math.isfinite(v) for bar in out.values() for v in bar.values())


class TestExitDoesNotFireOnNaNDay:

    def test_time_stop_defers_to_next_real_price(self, monkeypatch):
        """NaN 日不出场，顺延到有真实价格那天 —— 不是漏掉，是推迟。"""
        pos = pp.Position(
            ticker="TMUS", direction="bullish", entry_date="2026-08-14",
            entry_price=100.0, sl_price=93.0, tp_price=107.0,
            shares=10.0, size_usd=1000.0, time_stop_date="2026-08-28",
            confidence="high", score=7.0, rationale="test",
        )
        ohlc_nan_day = {                      # 8/28 已被 _fetch_ohlc 丢掉
            "2026-08-27": {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5},
        }
        assert pp._check_exit(pos, "2026-08-28", ohlc_nan_day) is None, \
            "没有真实价格时不得出场"

        ohlc_next = dict(ohlc_nan_day)
        ohlc_next["2026-08-31"] = {"Open": 102.0, "High": 103.0, "Low": 101.0, "Close": 102.5}
        res = pp._check_exit(pos, "2026-08-31", ohlc_next)
        assert res is not None, "有价格的下一天必须补触发，不能永久漏掉"
        reason, price, date = res
        assert reason == "TIME" and date == "2026-08-31"
        assert math.isfinite(price) and price == 102.5


class TestSizingGuardsRejectNaN:

    def _snap(self):
        return {"ticker": "TSLA", "direction": "bullish",
                "entry_price": 367.95, "composite_score": 7.8}

    def test_nan_nav_yields_no_position(self, monkeypatch):
        """NAV 为 NaN 时不得开出 NaN 仓位。

        修复前：`size_usd <= 1` 放 NaN 过去 → shares = NaN/367.95 = NaN。
        """
        monkeypatch.setattr(pp, "_compute_position_size",
                            lambda *a, **k: (float("nan"), "tier"))
        got = pp._open_position(self._snap(), float("nan"), "2026-08-31", {}, [])
        assert got is None

    def test_nan_entry_price_yields_no_position(self, monkeypatch):
        monkeypatch.setattr(pp, "_compute_position_size", lambda *a, **k: (1000.0, "tier"))
        snap = self._snap()
        snap["entry_price"] = float("nan")
        got = pp._open_position(snap, 50000.0, "2026-08-31", {}, [])
        assert got is None

    def test_healthy_inputs_still_open(self, monkeypatch):
        """回归护栏：修复只能挡 NaN，不能把正常开仓一起挡掉。"""
        monkeypatch.setattr(pp, "_compute_position_size", lambda *a, **k: (1000.0, "tier"))
        got = pp._open_position(self._snap(), 50000.0, "2026-08-31", {}, [])
        assert got is not None
        assert math.isfinite(got.shares) and got.shares > 0
        assert math.isfinite(got.size_usd)
