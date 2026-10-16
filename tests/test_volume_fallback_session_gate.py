"""Twelve Data 量比兜底按「最近一场已收盘」验收（v0.45.505）

事故形状（2026-09-15，归档里 TD 兜底仅有的两次）：yfinance 日线失败 ⇒ `_fill_volume_from_twelvedata`
补 `volume_ratio`。Twelve Data 的当日那根当晚是临时值，被 `twelve_data._drop_forming_bar` 规则 ① 丢掉
（对的，勿改），于是 17:15 ET 的生产扫描拿到的是**截至 D-1** 的比值，而 yfinance 主路径含 D：
NVDA 报 1.0108（yfinance D-1 1.0132 / D 0.676）——一个 0.68× 的缩量日被说成量能正常。

本文件的夹具按那天的形状造：D-1 比值 ≈1.01、D 比值 ≈0.69，差得足够大，取错一天一眼可见。
走真实的 HTTP 解析 + `_drop_forming_bar` + 缓存，只替换 HTTP 层。

⚠️ 被测路径读**两只钟**：`twelve_data._et_today`（规则 ① 丢当日那根、缓存键）与 `portfolio_greeks._et_now`
（`_latest_closed_session`）。`_freeze` 两只一起钉在同一刻——只钉一只，夹具就会和真实钟比
（auto-memory `alpha-hive-twelve-data-same-day-bar.md`）。
"""

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import pytest

import data_pipeline as dp
import portfolio_greeks as pg
import twelve_data as td
from bars_integrity import trading_days

ET = ZoneInfo("America/New_York")
BASE = 100_000_000
V_PREV = 101_000_000   # D-1：窗口均量 ≈ 基准 ⇒ 比值 ≈1.01
V_D = 68_000_000       # D：缩量日 ⇒ 比值 ≈0.69


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


def _serve(monkeypatch, last_day: dt.date, n=40):
    """Twelve Data 返回截至 `last_day`（含）的 n 个交易日；末两根是 D-1 / D 的成交量。"""
    import http_gate
    days = trading_days(last_day - dt.timedelta(days=90), last_day)[-n:]
    assert days[-1] == last_day and len(days) == n
    vals = []
    for i, day in enumerate(days):
        vol = V_D if i == n - 1 else V_PREV if i == n - 2 else BASE
        vals.append({"datetime": day.isoformat(), "close": str(100 + i), "volume": str(vol)})
    body = json.dumps({"values": vals}).encode()
    monkeypatch.setattr(http_gate, "urlopen_gated", lambda *a, **k: body)
    return [x.isoformat() for x in days]


def _ratio_ending(vols):
    return vols[-1] / (sum(vols[-20:]) / 20)


RATIO_PREV = _ratio_ending([BASE] * 19 + [V_PREV])          # 截至 D-1
RATIO_D = _ratio_ending([BASE] * 18 + [V_PREV, V_D])         # 截至 D


def test_fixture_days_really_differ():
    assert RATIO_PREV == pytest.approx(1.0095, abs=1e-3)
    assert RATIO_D == pytest.approx(0.6907, abs=1e-3)


def _fill(ticker="NVDA"):
    data = dp.StockData(price=200.0, momentum_5d=1.0, source_name="test")
    dp._fill_history_fields(ticker, data, "source:test")
    return data


# ───────────────────────────── 收盘后的生产扫描：TD 止于 D-1 ⇒ 拒收，且有人会红
class TestAfterCloseRejects:
    def test_production_scan_time_rejects_lagged_ratio(self, monkeypatch, caplog):
        _freeze(monkeypatch, 2026, 9, 15, 17, 15)
        _serve(monkeypatch, dt.date(2026, 9, 15))   # TD 当晚把 D 的临时 bar 也给了
        assert dp._latest_closed_session() == "2026-09-15"
        v = td.fetch_volume_ratio("NVDA")
        assert v["as_of"] == "2026-09-14" and v["volume_ratio"] == pytest.approx(RATIO_PREV)

        with caplog.at_level(logging.WARNING, logger="alpha_hive.data_pipeline"):
            data = _fill()
        assert data.volume_ratio is None and data.volume_source is None and data.volume_as_of is None
        msg = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "NVDA" in msg and "2026-09-14" in msg and "2026-09-15" in msg
        # 谁会红：still_missing ⇒ alert_manager P2
        hf = dp.history_fill_stats()
        assert hf["still_missing"]["volume"] == ["NVDA"]
        assert hf["by_path"]["source:test"]["volume"] == {"needed": 1, "filled": 0, "errors": 0}

    def test_all_sources_dead_branch_is_gated_too(self, monkeypatch):
        """全源失败那一支也经同一出口回落（v0.45.473），不另开口子。"""
        _freeze(monkeypatch, 2026, 9, 15, 17, 15)
        _serve(monkeypatch, dt.date(2026, 9, 15))
        monkeypatch.setattr(dp, "_fill_momentum_from_index", lambda t, out, session_close=None: out)

        class _Dead:
            def fetch(self, ticker):
                return None

        f = dp.MultiSourceFetcher()
        f._sources = [_Dead(), _Dead()]
        out = f.fetch("NVDA")
        assert out["_data_unavailable"] is True
        assert out["volume_ratio"] is None and out["volume_as_of"] is None

    def test_early_close_day_after_1300_rejects(self, monkeypatch):
        """感恩节次日 13:00 收盘：14:00 时应有场次已是当天，TD 仍丢当天那根 ⇒ 拒收。"""
        _freeze(monkeypatch, 2026, 11, 27, 14, 0)
        _serve(monkeypatch, dt.date(2026, 11, 27))
        assert dp._latest_closed_session() == "2026-11-27"
        assert _fill().volume_ratio is None


# ───────────────────────────── TD 末根就是应有场次 ⇒ 照收，并带 volume_as_of
class TestMatchingSessionAccepts:
    def _accept(self, monkeypatch, frozen, served_last, want_as_of, want_ratio):
        _freeze(monkeypatch, *frozen)
        _serve(monkeypatch, served_last)
        assert dp._latest_closed_session() == want_as_of
        data = _fill()
        assert data.volume_ratio == pytest.approx(want_ratio)
        assert data.volume_source == "twelvedata"
        assert data.volume_as_of == want_as_of
        assert data.to_dict()["volume_as_of"] == want_as_of
        assert dp.history_fill_stats()["still_missing"]["volume"] == []

    def test_intraday_expected_is_previous_session(self, monkeypatch):
        # 盘中 TD 给了 D 的半根（丢掉）⇒ 末根 D-1 == 应有场次
        self._accept(monkeypatch, (2026, 9, 15, 11, 0), dt.date(2026, 9, 15), "2026-09-14", RATIO_PREV)

    def test_premarket(self, monkeypatch):
        self._accept(monkeypatch, (2026, 9, 15, 8, 0), dt.date(2026, 9, 14), "2026-09-14", RATIO_D)

    def test_after_midnight_gets_settled_day(self, monkeypatch):
        # 过了美东午夜 D 那根已定稿、不再是「当日」⇒ 收 D 的比值（缩量日如实报出）
        self._accept(monkeypatch, (2026, 9, 16, 0, 30), dt.date(2026, 9, 15), "2026-09-15", RATIO_D)

    def test_weekend_expects_friday(self, monkeypatch):
        self._accept(monkeypatch, (2026, 9, 19, 10, 0), dt.date(2026, 9, 18), "2026-09-18", RATIO_D)

    def test_holiday_expects_previous_session(self, monkeypatch):
        # 感恩节（11-26）休市 ⇒ 应有场次 11-25
        self._accept(monkeypatch, (2026, 11, 26, 18, 0), dt.date(2026, 11, 25), "2026-11-25", RATIO_D)

    def test_early_close_day_before_1300(self, monkeypatch):
        self._accept(monkeypatch, (2026, 11, 27, 12, 30), dt.date(2026, 11, 27), "2026-11-25", RATIO_PREV)


# ───────────────────────────── 判不了就拒收，不猜
class TestUnjudgeable:
    def test_clock_unavailable_rejects(self, monkeypatch, caplog):
        _freeze(monkeypatch, 2026, 9, 15, 11, 0)
        _serve(monkeypatch, dt.date(2026, 9, 15))

        def _boom():
            raise RuntimeError("no tzdata")

        monkeypatch.setattr(pg, "_et_now", _boom)
        with caplog.at_level(logging.WARNING, logger="alpha_hive.data_pipeline"):
            assert dp._latest_closed_session() is None
            assert _fill().volume_ratio is None
        assert any("NVDA" in r.getMessage() for r in caplog.records)

    def test_missing_as_of_rejects(self, monkeypatch):
        """旧形状（没有 as_of）的结果说不清自己是哪天的 ⇒ 不收。"""
        monkeypatch.setattr(dp, "_latest_closed_session", lambda: "2026-09-15")
        monkeypatch.setattr(td, "fetch_volume_ratio",
                            lambda t: {"volume_ratio": 1.0, "avg_volume": 1, "recent_volume": 1})
        assert dp._fill_volume_from_twelvedata("NVDA", None) is None
