"""补跑（`--date D`）的宏观票必须是 D 的（v0.45.366）

背景（2026-09-28 只读实测，真值 = 新拉的 CBOE `VIX_History.csv`，交易日按 `is_trading_day` 数）：
- 补跑 VIX 取快照 `market.json` 的 `vix_spot`，而云端 17:05 ET 抓它时 CSV 还没追加 D 行 ⇒
  `cloud_snapshot_cboe` 日报 3/3（08-27/28/31）、`market.json` 08-26~09-11 12/12 都落后一场；
  快照不记观测日，09-14 起变成当日值后也无从分辨。补跑是事后跑，D 的官方收盘早在 CSV 里。
- 快照模式下 `vix_term` 被 `load_market` 剔除（兜底段）或 market.json 缺失 ⇒ 旧代码落到实时
  `get_vix_observation()`，把**运行当天**的 VIX 贴到 D 上、标 `cboe`、`vix_stale=False`（离线复现，历史 0 次）。
- GuardBee 的 VIX 期限结构（±2 票）与 FOMC 距离直接取实时；板块轮动 `period="5d"` 取最近 5 天。

用户决定（2026-09-28）：修法 (a) 取 CSV 的 D 行、补跑 VIX 照实时口径计票、同形泄漏同版修。
本文件守：值属于 D、Guard 照常计票、每条退路都不回落实时、实时口径逐票不变。

v0.45.654：宏观报价（^TNX / ^FVX / DX-Y.NYB / ^GSPC / TLT / GLD，及 CBOE 全缺时的 ^VIX）补齐板块轮动早有的
「末根必须正是 D」——Yahoo 整根缺 D 又没有事件时 `_asof_history` 返回止于更早一场的日线，旧代码拿那场冒充 D。
同版：缺 10Y 时不再拿兜底常量 4.5 去减真 2Y 算曲线（否则「当没取到」会把几 bp 的错放大成假倒挂）。
"""
import ast
from datetime import date, datetime
from unittest.mock import patch

import pytest

import cboe_vix
import economic_calendar
import fred_macro as fm
from is_trading_day import is_trading_day

# 模块加载早于任何 fixture：此刻抓到的是真函数（下面有条测试要在桩之后换回真日历）
_REAL_UPCOMING = economic_calendar.get_upcoming_events

D = "2026-08-27"
# CBOE 官方收盘（2026-09-28 拉的 CSV）：08-26 15.21 / 08-27 14.51 / 08-28 14.43
CSV_LATER = [("2026-08-26", 15.21), ("2026-08-27", 14.51), ("2026-08-28", 14.43)]
# 云端 08-27 17:05 ET 抓的 vix_spot = 15.21 = 08-26 收盘（落后一场）
MARKET_827 = {"cboe": {"vix_term": {"vix_spot": 15.21, "vix_1m": 17.15, "vix_3m": 19.6,
                                    "term_structure": "contango", "source": "vx_futures"}}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path, stub_cboe_vix):
    monkeypatch.setattr(cboe_vix, "_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(cboe_vix, "_CACHE_PATH", tmp_path / "vix.csv")
    fm.set_macro_snapshot(None)
    yield
    fm.set_macro_snapshot(None)


def _csv(monkeypatch, rows, refreshed=None):
    """`get_vix_history` 的桩；`refreshed` 给定时，`force_refresh=True` 返回它（模拟重下之后 CSV 追上了）。"""
    calls = []

    def _hist(max_days=None, force_refresh=False):
        calls.append(force_refresh)
        r = refreshed if (force_refresh and refreshed is not None) else rows
        return r[-max_days:] if max_days else r
    monkeypatch.setattr(cboe_vix, "get_vix_history", _hist)
    return calls


def _after_close_clock(monkeypatch):
    """钟拨到运行当天收盘后 —— 此刻若有人走实时报价路径，报价桩会被调到。"""
    import cboe_options
    monkeypatch.setattr(cboe_options, "_et_now", lambda: datetime(2026, 9, 28, 17, 10))


def _bars(*rows):
    """真 pandas 日线（`_fetch_macro_data` 要 `.empty` / `.index.date` / `["Close"].iloc`）。"""
    import pandas as pd
    return pd.DataFrame({"Close": [c for _d, c in rows]},
                        index=pd.DatetimeIndex([pd.Timestamp(d) for d, _c in rows]))


ON_D = (("2026-08-26", 400.0), (D, 402.0))                # 末根是 D：+0.5% ⇒ 黄金 rising
OFF_D = (("2026-08-25", 400.0), ("2026-08-26", 412.0))    # Yahoo 整根缺 D：止于 08-26（冒充成 D 就是 +3%「飙升」）


def test_calendar_preconditions():
    """下面的用例依赖这些日历事实 —— 先断言，别心算。"""
    assert all(is_trading_day(date(2026, 8, d))[0] for d in (26, 27, 28))
    assert not is_trading_day(date(2026, 9, 7))[0]          # 劳动节（CSV 里却有一行 15.30）
    assert is_trading_day(date(2026, 9, 4))[0] and is_trading_day(date(2026, 9, 8))[0]
    assert is_trading_day(date(2026, 9, 28))[0]


# ────────── A. cboe_vix.get_vix_observation_asof ──────────

class TestAsOfObservation:

    def test_takes_row_d_not_the_last_row(self, monkeypatch):
        """补跑在 D 之后跑，CSV 最后一行是更晚的日子。
        变红的变异：取 `hist[-1]`（会拿到 08-28 的 14.43）。"""
        _csv(monkeypatch, CSV_LATER)
        o = cboe_vix.get_vix_observation_asof(D)
        assert (o["vix"], o["as_of"], o["feed"]) == (14.51, D, "history_csv_asof")
        assert (o["prev_close"], o["prev_as_of"]) == (15.21, "2026-08-26")

    def test_freshness_is_judged_against_d_not_the_clock(self, monkeypatch):
        """D 是一个月前，但观测日就是 D ⇒ 不陈旧、落后 0 场（否则 Guard 一票不投）。
        变红的变异：改用 `vix_staleness(as_of)`（按「此刻」判 ⇒ stale=True）。"""
        _after_close_clock(monkeypatch)
        _csv(monkeypatch, CSV_LATER)
        o = cboe_vix.get_vix_observation_asof(D)
        assert (o["lag_sessions"], o["stale"]) == (0, False)

    def test_prev_close_is_the_previous_trading_day_not_the_previous_row(self, monkeypatch):
        """CSV 有 09-07 劳动节行；09-08 的前一收盘是 09-04（交易日），不是 CSV 的上一行。
        变红的变异：前一收盘取 CSV 上一行（会拿到 09-07 的 15.30）。"""
        _csv(monkeypatch, [("2026-09-03", 14.32), ("2026-09-04", 14.53),
                           ("2026-09-07", 15.30), ("2026-09-08", 15.72)])
        o = cboe_vix.get_vix_observation_asof("2026-09-08")
        assert (o["vix"], o["prev_as_of"], o["prev_close"]) == (15.72, "2026-09-04", 14.53)

    def test_missing_row_forces_one_refresh(self, monkeypatch):
        """缓存是 D 当晚 CSV 追加之前下的 ⇒ 缺 D 行 ⇒ 重下一次就有了。
        变红的变异：删掉 `force_refresh=True` 那次重试。"""
        calls = _csv(monkeypatch, CSV_LATER[:1], refreshed=CSV_LATER)
        o = cboe_vix.get_vix_observation_asof(D)
        assert o["vix"] == 14.51 and calls == [False, True]

    def test_row_still_missing_is_none_and_loud(self, monkeypatch, caplog):
        """重下之后仍缺 ⇒ 说取不到，绝不拿别的行冒充。
        变红的变异：缺行时退回最后一行（`hist[-1]`）。"""
        _csv(monkeypatch, [("2026-08-26", 15.21)])
        with caplog.at_level("WARNING", logger="alpha_hive.cboe_vix"):
            o = cboe_vix.get_vix_observation_asof(D)
        assert o["vix"] is None and o["as_of"] is None and o["quote_reason"] == "asof_row_missing"
        assert any("没有 2026-08-27" in m for m in caplog.messages)
        assert set(o) >= {"vix", "as_of", "feed", "quote_reason", "history_fetch",
                          "lag_sessions", "stale", "quote_check"}

    def test_never_touches_the_live_quote(self, monkeypatch):
        """报价只代表「此刻最新一场」。钟已过运行当天收盘，走实时路径就会调到报价桩。
        变红的变异：在 as-of 函数里调 `get_vix_session_close` / `get_vix_observation`。"""
        _after_close_clock(monkeypatch)
        _csv(monkeypatch, CSV_LATER)
        calls = []
        monkeypatch.setattr(cboe_vix, "_download_quote", lambda: calls.append(1))
        cboe_vix.get_vix_observation_asof(D)
        assert calls == []


# ────────── B. fred_macro 补跑口径 ──────────

class TestFredMacroBackfill:

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, stub_yfinance, stub_fred):
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        monkeypatch.setattr(fm, "_asof_history", lambda *a, **k: None)
        monkeypatch.setattr(fm, "_fetch_sector_rotation", lambda *a, **k: {"hot": [], "cold": [], "full": {}})
        _after_close_clock(monkeypatch)
        # 实时入口：快照模式下一次都不许走到
        self.live_calls = []
        monkeypatch.setattr(cboe_vix, "get_vix_observation",
                            lambda now_et=None: (self.live_calls.append(1),
                                                 {"vix": 16.04, "as_of": "2026-09-28", "stale": False})[1])
        yield
        fm._CACHE, fm._CACHE_TS = {}, 0.0

    def _data(self, monkeypatch, empty=False):
        d = {} if empty else {"TWO": {"last": 4.2, "prev": 4.2, "change_pct": 0.0}}
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (d, {"TWO": f"treasury_gov@{D}"} if d else {}))

    def test_csv_row_d_beats_the_snapshot_spot(self, monkeypatch):
        """08-27 实况：快照 15.21（08-26 收盘）vs CSV 的 08-27 官方收盘 14.51。
        变红的变异：先信快照 `vix_spot`（恢复 v0.45.59 的顺序）。"""
        self._data(monkeypatch)
        _csv(monkeypatch, CSV_LATER)
        fm.set_macro_snapshot(D, MARKET_827)
        r = fm.get_macro_context()
        assert (r["vix"], r["vix_source"], r["vix_feed"], r["vix_as_of"]) == (14.51, "cboe", "history_csv_asof", D)
        assert (r["vix_stale"], r["vix_lag_sessions"], r["as_of_mode"]) == (False, 0, "backfill")
        assert r["vix_change_pct"] == round((14.51 / 15.21 - 1) * 100, 2)
        assert r["vix_regime"] == "low" and self.live_calls == []

    def test_csv_without_row_d_falls_back_to_the_snapshot_not_live(self, monkeypatch):
        self._data(monkeypatch)
        _csv(monkeypatch, [])
        fm.set_macro_snapshot(D, MARKET_827)
        r = fm.get_macro_context()
        assert (r["vix"], r["vix_source"], r["vix_feed"], r["vix_feed_note"]) == \
            (15.21, "cloud_snapshot_cboe", "cloud_snapshot", "asof_row_missing")
        assert r["vix_as_of"] is None and self.live_calls == []

    @pytest.mark.parametrize("market", [{"cboe": {}}, None], ids=["vix_term_dropped", "market_json_missing"])
    def test_snapshot_mode_never_falls_through_to_live_vix(self, monkeypatch, market):
        """离线复现过的隐患 B/C：旧代码此处把运行当天的 16.04 贴到 08-27、标 cboe、vix_stale=False。
        变红的变异：去掉实时分支上的 `and not _snap`。"""
        self._data(monkeypatch)
        _csv(monkeypatch, [])
        fm.set_macro_snapshot(D, market)
        r = fm.get_macro_context()
        assert self.live_calls == [], "快照模式走到了实时 VIX"
        assert r["vix"] != 16.04 and r["vix_source"] == "fallback"

    def test_default_fallback_spot_is_not_an_observation(self, monkeypatch):
        """`cboe_fetcher` 拿不到期货时整组落 15.0/15.75/16.5、标 default_fallback。生产路径上
        `load_market` 先剔除；直接喂未过滤 market 的调用方也不许把 15.0 当观测。
        变红的变异：去掉 `source != "default_fallback"` 那一判（旧代码只判 `> 0`）。"""
        self._data(monkeypatch)
        _csv(monkeypatch, [])
        fm.set_macro_snapshot(D, {"cboe": {"vix_term": {"vix_spot": 15.0, "vix_1m": 15.75, "vix_3m": 16.5,
                                                        "source": "default_fallback"}}})
        r = fm.get_macro_context()
        assert r["vix_source"] != "cloud_snapshot_cboe" and r["vix"] != 15.0

    def test_partial_path_does_not_relabel_the_snapshot_as_cboe(self, monkeypatch):
        """隐患 D：快照值 + 其余宏观全灭 ⇒ 旧代码写死 `vix_source="cboe"` ⇒ Guard 拿无观测日的值计票。
        变红的变异：部分降级路径写回字面量 "cboe"。"""
        self._data(monkeypatch, empty=True)
        _csv(monkeypatch, [])
        fm.set_macro_snapshot(D, MARKET_827)
        r = fm.get_macro_context()
        assert r["data_source"] == "fallback" and r["vix"] == 15.21
        assert r["vix_source"] == "cloud_snapshot_cboe"

    def test_partial_path_with_row_d_keeps_its_date(self, monkeypatch):
        self._data(monkeypatch, empty=True)
        _csv(monkeypatch, CSV_LATER)
        fm.set_macro_snapshot(D, MARKET_827)
        r = fm.get_macro_context()
        assert (r["vix"], r["vix_source"], r["vix_as_of"], r["vix_stale"]) == (14.51, "cboe", D, False)

    def test_sector_rotation_is_asked_for_d(self, monkeypatch):
        """变红的变异：`_fetch_macro_data` 调 `_fetch_sector_rotation(yf)` 不带 `as_of`。"""
        self._data(monkeypatch)
        _csv(monkeypatch, CSV_LATER)
        seen = []
        monkeypatch.setattr(fm, "_fetch_sector_rotation",
                            lambda yf=None, as_of=None: (seen.append(as_of), {"hot": [], "cold": [], "full": {}})[1])
        fm.set_macro_snapshot(D, MARKET_827)
        fm.get_macro_context()
        fm.set_macro_snapshot(None)
        fm.get_macro_context()
        assert seen == [D, None]

    # ── v0.45.654：宏观报价的末根必须正是 D（同板块轮动那支） ──

    def _quotes(self, monkeypatch, per_sym):
        seen = []
        monkeypatch.setattr(fm, "_asof_history",
                            lambda yf, sym, as_of: (seen.append((sym, as_of)), per_sym(sym))[1])
        return seen

    def test_quote_with_bar_on_d_is_used(self, monkeypatch):
        """正对照：末根是 D ⇒ 照用（last = D 收盘、prev = 前一根），`not_on_as_of` 为空。"""
        self._data(monkeypatch)
        _csv(monkeypatch, CSV_LATER)
        seen = self._quotes(monkeypatch, lambda s: _bars(*ON_D) if s == "GLD" else None)
        fm.set_macro_snapshot(D, MARKET_827)
        r = fm.get_macro_context()
        assert ("GLD", D) in seen
        assert (r["gold_price"], r["gold_change_pct"], r["gold_trend"]) == (402.0, 0.5, "rising")
        assert r["not_on_as_of"] == []

    def test_quote_without_bar_on_d_is_missing_not_backdated(self, monkeypatch, caplog):
        """Yahoo 整根缺 D 又没有事件（不产生空行）⇒ `_asof_history` 返回止于 08-26 的日线。
        旧代码拿 08-26 收盘冒充 D：+3% ⇒ 黄金「飙升」⇒ Guard risk_off +2。现在当没取到、点名、WARNING。
        变红的变异：去掉 `hist.index.date[-1] != _as_of_d` 那一判。"""
        self._data(monkeypatch)
        _csv(monkeypatch, CSV_LATER)
        self._quotes(monkeypatch, lambda s: _bars(*OFF_D) if s == "GLD" else None)
        fm.set_macro_snapshot(D, MARKET_827)
        with caplog.at_level("WARNING", logger="alpha_hive.fred_macro"):
            r = fm.get_macro_context()
        assert (r["gold_price"], r["gold_trend"]) == (None, "stable")
        assert r["not_on_as_of"] == ["GLD"]
        assert any(D in m and "GLD=GLD" in m for m in caplog.messages), caplog.messages

    @pytest.mark.parametrize("market,csv_rows,vix", [
        (MARKET_827, CSV_LATER, 14.51),   # CBOE 给得出 D 的 VIX ⇒ 部分降级路径
        ({"cboe": {}}, [], 20.0),         # VIX 也没有 ⇒ 整份兜底
    ], ids=["partial", "base"])
    def test_every_symbol_off_d_is_named_on_the_degraded_paths(self, monkeypatch, market, csv_rows, vix):
        """符号全被挡下 ⇒ data 空 ⇒ 降级成 fallback —— 正是这时最需要知道为什么。
        变红的变异：去掉日期核对（不降级）；部分降级 / 兜底两条返回路径之一不带 `not_on_as_of`（参数化各红一条）。"""
        self._data(monkeypatch, empty=True)
        _csv(monkeypatch, csv_rows)
        self._quotes(monkeypatch, lambda s: _bars(*OFF_D))
        fm.set_macro_snapshot(D, market)
        r = fm.get_macro_context()
        assert (r["data_source"], r["vix"]) == ("fallback", vix)
        assert r["not_on_as_of"] == sorted(["VIX", "TNX", "FVX", "DXY", "SPX", "TLT", "GLD"])

    def test_realtime_output_has_no_such_key(self, monkeypatch):
        """实时口径输出不变（同板块轮动：`not_on_as_of` 只在补跑带）。变红的变异：不看 `_as_of` 恒带。"""
        self._data(monkeypatch)
        fm.set_macro_snapshot(None)
        assert "not_on_as_of" not in fm.get_macro_context()

    @pytest.mark.parametrize("tnx_bars,curve,spread", [
        ((("2026-08-26", 5.20), (D, 5.24)), "normal", 49.0),                   # 10Y 有 D 那根：5.24 − 4.75
        ((("2026-08-25", 5.22), ("2026-08-26", 5.24)), "unknown", None),       # 缺 D ⇒ 没有 10Y
    ], ids=["tnx_on_d", "tnx_off_d"])
    def test_curve_needs_an_observed_10y(self, monkeypatch, tnx_bars, curve, spread):
        """10-12 补跑的形状：财政部在债市假日没有 D 行 ⇒ 10Y 走 yfinance ^TNX、2Y 走 FRED。
        ^TNX 缺 D 被当没取到后，`tnx` 落到兜底常量 4.5 ⇒ 4.5 − 4.75 = −25bp「倒挂」⇒ Guard risk_off +2。
        2Y 是真观测，照记。变红的变异：去掉 `_set_curve` 里的 `_tnx_seen` 一判（tnx_off_d 会得到 inverted）。"""
        self._data(monkeypatch, empty=True)           # 财政部没有 D 行
        _csv(monkeypatch, CSV_LATER)
        bars = {"^TNX": tnx_bars, "GLD": ON_D}        # GLD 只为让 data 非空（否则走整份降级，曲线键都没有）
        self._quotes(monkeypatch, lambda s: _bars(*bars[s]) if s in bars else None)
        monkeypatch.setattr(fm, "_load_fred_key", lambda: "k")
        monkeypatch.setattr(fm, "_fetch_fred_series", lambda key, vintage=None: {"treasury_2y": 4.75})
        fm.set_macro_snapshot(D, MARKET_827)
        r = fm.get_macro_context()
        assert (r["yield_curve"], r["yield_spread"]) == (curve, spread)
        assert (r["treasury_2y"], r["treasury_2y_source"]) == (4.75, "fred")

    # ── v0.45.657：FRED 按 D 的时点取（迷你 ALFRED 见 G 节） ──

    def _holiday_d12(self, monkeypatch, fake):
        """10-12 补跑的形状：财政部没有 D 行；^TNX 有 D 那根 5.24；2Y 只能走 FRED。"""
        self._data(monkeypatch, empty=True)
        _csv(monkeypatch, CSV_LATER)
        bars = {"^TNX": (("2026-10-09", 5.20), (D12, 5.24)), "GLD": (("2026-10-09", 400.0), (D12, 402.0))}
        self._quotes(monkeypatch, lambda s: _bars(*bars[s]) if s in bars else None)
        monkeypatch.setattr(fm, "_load_fred_key", lambda: "k")
        _plug_fred(monkeypatch, fake)

    def test_fred_2y_on_the_curve_is_as_of_d(self, monkeypatch):
        """进曲线（⇒ Guard 宏观票）的 FRED 2Y 是 D 那天已发布的 4.75@10-08，不是补跑运行时的最新值。
        旧代码晚跑会拿到 5.40 ⇒ −16bp 倒挂 ⇒ Guard risk_off +2。变红的变异：调 `_fetch_fred_series` 不带 `vintage`。"""
        fake = _FakeFred()
        self._holiday_d12(monkeypatch, fake)
        fm.set_macro_snapshot(D12, MARKET_827)
        r = fm.get_macro_context()
        assert (r["treasury_2y"], r["treasury_2y_source"]) == (4.75, "fred")
        assert (r["yield_spread"], r["yield_curve"]) == (49.0, "normal")
        assert (r["fred_extras"]["vintage"], r["fred_extras"]["treasury_2y_date"]) == (D12, "2026-10-08")
        assert fake.calls and all(_pinned(c, D12) for c in fake.calls), fake.calls

    def test_realtime_fred_is_not_pinned(self, monkeypatch):
        """实时口径照取最新（变红的变异：实时也传一个日期，如 `vintage=_as_of or _realtime_as_of()`）。"""
        fake = _FakeFred()
        self._data(monkeypatch)
        monkeypatch.setattr(fm, "_load_fred_key", lambda: "k")
        _plug_fred(monkeypatch, fake)
        fm.set_macro_snapshot(None)
        r = fm.get_macro_context()
        assert fake.calls and not any("realtime_end" in c for c in fake.calls), fake.calls
        assert "vintage" not in r["fred_extras"]


# ────────── C. 板块轮动对齐 D ──────────

class _Hist:
    """最小 DataFrame 替身：`len` / `.index.date`（numpy 数组，同真 pandas）/ `["Close"].iloc`。"""

    def __init__(self, rows):
        import numpy as np
        self._rows = sorted(rows)
        self.index = type("I", (), {"date": np.array([d for d, _ in self._rows])})()

    def __len__(self):
        return len(self._rows)

    def __getitem__(self, k):
        assert k == "Close"
        closes = [c for _, c in self._rows]
        return type("S", (), {"iloc": closes})()


def _days(*ds):
    return [date.fromisoformat(x) for x in ds]


class TestSectorRotationAsOf:

    WINDOW = _days("2026-08-19", "2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27")

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        saved = dict(fm._etf_cache)
        fm._etf_cache.clear()
        yield
        fm._etf_cache.clear()
        fm._etf_cache.update(saved)

    class _NoLiveYF:
        """补跑口径绝不许调 `Ticker().history(period=...)`（那是「最近 5 天」）。"""
        class Ticker:
            def __init__(self, s):
                raise AssertionError(f"补跑口径调了实时 yf.Ticker({s})")

    def _asof(self, monkeypatch, per_etf):
        seen = []

        def _h(yf, sym, as_of):
            seen.append((sym, as_of))
            return per_etf(sym)
        monkeypatch.setattr(fm, "_asof_history", _h)
        return seen

    def test_uses_last_five_bars_ending_on_d(self, monkeypatch):
        """窗口取末 5 根（同实时 `period="5d"` 的宽度）：08-21 → 08-27。
        变红的变异：用 `iloc[0]`（整段 7 根的首根 08-19）。"""
        closes = [100, 101, 102, 103, 104, 105, 110]
        seen = self._asof(monkeypatch, lambda s: _Hist(list(zip(self.WINDOW, closes))))
        r = fm._fetch_sector_rotation(self._NoLiveYF, as_of=D)
        assert {a for _s, a in seen} == {D} and len(seen) == len(fm._SECTOR_ETFS)
        chg = {etf: c for etf, (_n, c) in r["full"].items()}
        assert set(chg.values()) == {round((110 / 102 - 1) * 100, 2)}
        assert r["as_of"] == D and r["not_on_as_of"] == []

    def test_etf_without_bar_on_d_is_left_out_not_backdated(self, monkeypatch):
        """末根是 D 之前的 ⇒ 不计入、点名。变红的变异：去掉末根 == D 的检查。"""
        stale_etf = sorted(fm._SECTOR_ETFS)[0]

        def per(s):
            rows = list(zip(self.WINDOW, [100, 101, 102, 103, 104, 105, 106]))
            return _Hist(rows[:-1] if s == stale_etf else rows)
        self._asof(monkeypatch, per)
        r = fm._fetch_sector_rotation(self._NoLiveYF, as_of=D)
        assert stale_etf not in r["full"] and r["not_on_as_of"] == [stale_etf]

    def test_backfill_neither_reads_nor_writes_the_etf_cache(self, monkeypatch):
        """单 ETF 缓存只按 ETF 键。变红的变异：补跑口径读缓存（会拿到 99.0）/ 写缓存（实时之后读到 D 的值）。"""
        import time as _t
        for etf in fm._SECTOR_ETFS:
            fm._etf_cache[etf] = (_t.time(), (fm._SECTOR_ETFS[etf], 99.0))
        before = dict(fm._etf_cache)
        self._asof(monkeypatch, lambda s: _Hist(list(zip(self.WINDOW, [100, 101, 102, 103, 104, 105, 106]))))
        r = fm._fetch_sector_rotation(self._NoLiveYF, as_of=D)
        assert 99.0 not in {c for _n, c in r["full"].values()}
        assert fm._etf_cache == before


# ────────── D. GuardBee 补跑口径 ──────────

def _guard_live_macro(macro):
    from swarm_agents.guard_bee import GuardBeeSentinel
    g = GuardBeeSentinel.__new__(GuardBeeSentinel)
    with patch("fred_macro.get_macro_context", return_value=macro):
        return g._calc_macro_adjustment("AMC")


class TestGuardBackfill:

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, stub_yfinance, stub_vixcentral):
        """实时期限结构：记调用、恒报 contango（补跑不许读到它）。"""
        self.live_vts = []
        import vix_term_structure
        monkeypatch.setattr(vix_term_structure, "get_vix_term_structure",
                            lambda force_refresh=False: (self.live_vts.append(1), {"structure": "contango"})[1])
        self.cal_refs = []
        import economic_calendar
        # v0.45.582：GuardBee 的 FOMC 票改走 get_upcoming_events（看 3 天内全部事件）——桩打在它上面，
        # 否则真日历漏进来：实时口径的测试会在每次 FOMC 前 3 天里多一张 risk_off 票（时间炸弹）
        monkeypatch.setattr(economic_calendar, "get_upcoming_events",
                            lambda days=30, ref_date=None: (self.cal_refs.append(ref_date), [])[1])

    def _snap_macro(self, **vix):
        return {"data_source": f"cloud_snapshot+treasury@{D}", "yield_curve": "normal", "gold_trend": "stable",
                "vix": 14.51, "vix_regime": "low", "vix_source": "cboe", "vix_feed": "history_csv_asof",
                "vix_as_of": D, "vix_stale": False, **vix}

    def test_row_d_vix_votes_like_live(self):
        """用户决定：补跑 VIX 照实时口径计票。08-27 官方收盘 14.51 < 15 ⇒ risk_on +1（旧：不计票）。
        变红的变异：`fred_macro` 给 CSV 行标 `cloud_snapshot_cboe`（白名单外 ⇒ 不计票）。"""
        fm.set_macro_snapshot(D, MARKET_827)
        r = _guard_live_macro(self._snap_macro())
        assert r["details"]["vix"] == 14.51 and r["regime_votes"]["risk_on"] >= 1

    def test_end_to_end_backfill_vix_reaches_the_vote(self, monkeypatch, stub_fred):
        """不打桩 `get_macro_context`：真 fred_macro 的输出喂真 Guard（任一侧改标签只有这条会红）。"""
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        monkeypatch.setattr(fm, "_asof_history", lambda *a, **k: None)
        monkeypatch.setattr(fm, "_fetch_sector_rotation", lambda *a, **k: {"hot": [], "cold": [], "full": {}})
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (
            {"TWO": {"last": 4.2, "prev": 4.2, "change_pct": 0.0}}, {"TWO": f"treasury_gov@{D}"}))
        _csv(monkeypatch, CSV_LATER)
        fm.set_macro_snapshot(D, MARKET_827)
        from swarm_agents.guard_bee import GuardBeeSentinel
        r = GuardBeeSentinel.__new__(GuardBeeSentinel)._calc_macro_adjustment("AMC")
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        assert r["details"]["vix"] == 14.51 and r["details"]["vix_as_of"] == D
        assert r["regime_votes"]["risk_on"] >= 1

    def test_end_to_end_gold_bar_missing_d_does_not_vote(self, monkeypatch, stub_fred):
        """真 fred_macro → 真 Guard（v0.45.654）：GLD 缺 D 那根、止于 08-26（+3%）。
        旧代码判「黄金飙升」⇒ risk_off +2，压过 D 的 VIX 那张 risk_on ⇒ regime risk_off、macro_adj −1.0；
        现在黄金不投票 ⇒ risk_on、+0.5。变红的变异：去掉 `_fetch_macro_data` 的末根 == D 核对。"""
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        monkeypatch.setattr(fm, "_asof_history", lambda yf, sym, as_of: _bars(*OFF_D) if sym == "GLD" else None)
        monkeypatch.setattr(fm, "_fetch_sector_rotation", lambda *a, **k: {"hot": [], "cold": [], "full": {}})
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (
            {"TWO": {"last": 4.2, "prev": 4.2, "change_pct": 0.0}}, {"TWO": f"treasury_gov@{D}"}))
        _csv(monkeypatch, CSV_LATER)
        fm.set_macro_snapshot(D, MARKET_827)
        from swarm_agents.guard_bee import GuardBeeSentinel
        r = GuardBeeSentinel.__new__(GuardBeeSentinel)._calc_macro_adjustment("AMC")
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        assert r["details"]["gold_trend"] == "stable" and r["regime_votes"]["risk_off"] == 0
        assert (r["regime"], r["score_adj"]) == ("risk_on", 0.5)

    def test_snapshot_vix_without_a_date_still_casts_no_vote(self):
        """退路（CSV 缺 D 行）的快照值没有观测日 ⇒ 照旧不计票。"""
        fm.set_macro_snapshot(D, MARKET_827)
        r = _guard_live_macro(self._snap_macro(vix=15.21, vix_source="cloud_snapshot_cboe",
                                               vix_feed="cloud_snapshot", vix_as_of=None, vix_stale=None))
        assert "vix" not in r["details"]

    def test_term_structure_comes_from_the_snapshot(self):
        """变红的变异：补跑仍调实时 `get_vix_term_structure()`（会读到 contango、且调用计数 1）。"""
        fm.set_macro_snapshot(D, {"cboe": {"vix_term": {"term_structure": "backwardation", "source": "vx_futures"}}})
        r = _guard_live_macro(self._snap_macro())
        assert self.live_vts == []
        assert (r["details"]["vix_term"], r["details"]["vix_term_source"]) == ("backwardation", "cloud_snapshot")
        assert r["regime_votes"]["risk_off"] >= 2

    @pytest.mark.parametrize("market", [
        {"cboe": {}},                                                                      # load_market 剔除了兜底段
        {"cboe": {"vix_term": {"term_structure": "backwardation", "source": "default_fallback"}}},
        {},                                                                                # market.json 缺失
    ], ids=["dropped", "default_fallback", "missing"])
    def test_missing_snapshot_term_structure_does_not_vote_or_go_live(self, market):
        """变红的变异：快照缺该段时回落实时函数；或不看 source 就信兜底段。"""
        fm.set_macro_snapshot(D, market)
        r = _guard_live_macro(self._snap_macro())
        assert self.live_vts == [] and "vix_term" not in r["details"]
        assert r["details"]["vix_term_source"] == "cloud_snapshot_unavailable"
        assert r["regime_votes"]["risk_off"] == 0

    def test_fomc_distance_is_counted_from_d(self):
        """变红的变异：FOMC 查询不带 `ref_date`（按运行当天数）。"""
        fm.set_macro_snapshot(D, MARKET_827)
        _guard_live_macro(self._snap_macro())
        assert self.cal_refs == [date(2026, 8, 27)]

    def test_fomc_vote_uses_the_real_calendar_on_d(self, monkeypatch):
        """真日历正对照：取 D = 某次 FOMC 前 2 天（交易日）⇒ risk_off +1、fomc_days = 2。
        （v0.45.582 前还要求「D 的下一个事件就是这次 FOMC」——旧规则只看最近一个事件，CPI / GDP 排在前面会遮住这张票；
        现在看 3 天内全部事件，这个前提不再需要。被遮那几天的专门钉在 tests/test_guard_fomc_vote.py。）"""
        from datetime import timedelta
        monkeypatch.setattr(economic_calendar, "get_upcoming_events", _REAL_UPCOMING)
        ref = None
        for f in economic_calendar._parse_dates(economic_calendar._FOMC):
            cand = f - timedelta(days=2)
            if date(2026, 1, 1) <= cand < date(2026, 9, 1) and is_trading_day(cand)[0]:
                ref = cand
                break
        assert ref is not None, "日历里找不到合适的 FOMC 前两天——本条正对照失效了"
        fm.set_macro_snapshot(ref.isoformat(), MARKET_827)
        r = _guard_live_macro(self._snap_macro())
        assert r["details"].get("fomc_days") == 2 and r["regime_votes"]["risk_off"] >= 1

    def test_mode_key_is_written_in_both_modes(self):
        """`macro_as_of_mode` 是 v0.45.366 世代边界印记：实时与补跑都写。"""
        fm.set_macro_snapshot(D, MARKET_827)
        b = _guard_live_macro(self._snap_macro())["details"]
        fm.set_macro_snapshot(None)
        r = _guard_live_macro(self._snap_macro())["details"]
        assert (b["macro_as_of_mode"], b["macro_ref_date"]) == ("backfill", D)
        assert (r["macro_as_of_mode"], r["macro_ref_date"]) == ("realtime", None)
        assert "macro_as_of_mode" in _guard_live_macro({})["details"]


class TestGuardLiveUnchanged:
    """只改补跑：实时口径的每一票与改前逐一相同。"""

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, stub_yfinance, stub_vixcentral):
        fm.set_macro_snapshot(None)
        self.cal_refs = []
        import economic_calendar
        # v0.45.582：GuardBee 的 FOMC 票改走 get_upcoming_events（看 3 天内全部事件）——桩打在它上面，
        # 否则真日历漏进来：实时口径的测试会在每次 FOMC 前 3 天里多一张 risk_off 票（时间炸弹）
        monkeypatch.setattr(economic_calendar, "get_upcoming_events",
                            lambda days=30, ref_date=None: (self.cal_refs.append(ref_date), [])[1])

    @pytest.mark.parametrize("structure,off", [("backwardation", 2), ("contango", 0), ("flat", 0), ("unknown", 0)])
    def test_live_term_structure_still_read_live(self, monkeypatch, structure, off):
        import vix_term_structure
        monkeypatch.setattr(vix_term_structure, "get_vix_term_structure",
                            lambda force_refresh=False: {"structure": structure})
        r = _guard_live_macro({"vix": 18.0, "vix_source": "cboe", "yield_curve": "normal", "gold_trend": "stable"})
        assert r["details"]["vix_term"] == structure and r["details"]["vix_term_source"] == "live"
        assert r["regime_votes"]["risk_off"] == off and r["regime_votes"]["risk_on"] == 0
        assert self.cal_refs == [None]


# ────────── E. 世代边界印记（ic_rerun_readiness v0.45.366） ──────────

def _guard_body(vt):
    return {"swarm_results": {"agent_details": {"GuardBeeSentinel": {"details": {"vix_term_structure": vt}}}}}


class TestBoundaryMarker:

    @pytest.fixture(autouse=True)
    def _offline(self, stub_yfinance, stub_vixcentral):
        """真 Guard 实时路径还会取 VIX 期限结构（yfinance 现货 + vixcentral 期货）。"""

    @pytest.mark.parametrize("snap,is_evidence", [(None, True), (D, False)], ids=["realtime", "backfill"])
    def test_real_guard_output_marker_is_evidence_only_when_realtime(self, snap, is_evidence):
        """正对照拿**生产类的真输出**。两种口径都写键（本条只改补跑，只在补跑行写会让每份实时归档被判
        「边界之后无印记」）；但**只有实时行算证据**（v0.45.373）：补跑行按目标日落盘，日期不代表代码何时上线。
        变红的变异：只在补跑时写键；把 guard_bee.py 里的键改名；判别器不排除 backfill。"""
        import ic_rerun_readiness as rr
        fm.set_macro_snapshot(snap, MARKET_827 if snap else None)
        det = _guard_live_macro({"vix": 15.0, "vix_source": "cboe"})["details"]
        assert "macro_as_of_mode" in det
        assert rr._marker_guard_macro_as_of_mode(_guard_body(det)) is is_evidence

    @pytest.mark.parametrize("version", ["v0.45.357", "v0.45.366"])
    def test_late_backfill_of_an_older_date_does_not_pull_the_marker_earlier(self, tmp_path, version):
        """部署后补跑 09-25（早于边界 09-28）带着同一印记键 ⇒ 旧判别器报 boundary_too_late（假报警）。
        变红的变异：两个判别器各自去掉 `!= "backfill"`（参数化各红一条）。"""
        import json as _json
        import ic_rerun_readiness as rr
        (tmp_path / "analysis-AAA-ml-2026-09-28.json").write_text(_json.dumps(
            _guard_body({"vix_feed": "delayed_quote", "macro_as_of_mode": "realtime"})), encoding="utf-8")
        (tmp_path / "analysis-BBB-ml-2026-09-25.json").write_text(_json.dumps(
            _guard_body({"vix_feed": "history_csv_asof", "macro_as_of_mode": "backfill"})), encoding="utf-8")
        ev = rr.cohort_boundary_evidence(tmp_path, version=version)
        assert (ev["verdict"], ev["marker_first_seen"]) == ("matches", "2026-09-28"), ev

    def test_357_era_archives_without_the_mode_key_still_count(self):
        """357 时代的归档没有 `macro_as_of_mode` 键 ⇒ 仍是证据（排除的是「显式 backfill」，不是「缺键」）。
        变红的变异：改成 `vt.get("macro_as_of_mode") == "realtime"`。"""
        import ic_rerun_readiness as rr
        assert rr._marker_guard_vix_feed(_guard_body({"vix_feed": "delayed_quote"})) is True

    @pytest.mark.parametrize("d", [
        {}, _guard_body(None),
        _guard_body({"macro_data_source": "fallback", "vix_source": "cboe", "vix": 14.21}),            # 09-25 形状
        _guard_body({"vix_source": "cboe", "vix_feed": "delayed_quote", "vix_as_of": "2026-09-28"}),  # v0.45.357 形状
    ])
    def test_old_shapes_are_not(self, d):
        import ic_rerun_readiness as rr
        assert rr._marker_guard_macro_as_of_mode(d) is False

    def test_boundary_day_matches(self, tmp_path):
        import json as _json
        import ic_rerun_readiness as rr
        b = next(d for d, v, _r in rr._COHORT_HISTORY if v == "v0.45.366")
        (tmp_path / "analysis-ZZZ-ml-2026-09-25.json").write_text(_json.dumps(
            _guard_body({"vix_source": "cboe", "vix_feed": None})), encoding="utf-8")
        (tmp_path / f"analysis-AAA-ml-{b}.json").write_text(_json.dumps(
            _guard_body({"vix_feed": None, "macro_as_of_mode": "realtime"})), encoding="utf-8")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.366")
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == b, ev


# ────────── F. 世代边界登记（v0.45.654：并进 10-19 同日标签 + 等价判据） ──────────

_V654 = "v0.45.654"
_MARK654 = "macro_quote_dates_checked"
_LIVE_SRC = "treasury+finnhub+yfinance+fred"      # 10-19 ~ 10-26 实况


def _row654(*, new=False, guard="ok", mode="realtime", source=_LIVE_SRC):
    """`.swarm_results` 里一只票的最小形状（Guard 宏观细节的归档键名是 `vix_term_structure`）。"""
    if guard == "absent":
        return {"agent_details": {}}
    if guard == "errored":
        return {"agent_details": {"GuardBeeSentinel": {"score": 5.0, "details": {}, "error": "TypeError: boom"}}}
    det = {"macro_adj": 0.0}
    if guard == "ok":
        det["vix_term_structure"] = {"macro_as_of_mode": mode, "macro_data_source": source}
    if new:
        det[_MARK654] = True
    return {"agent_details": {"GuardBeeSentinel": {"score": 5.0, "details": det}}}


def _write654(root, day, rows):
    import json as _json
    (root / f".swarm_results_{day}.json").write_text(_json.dumps(rows), encoding="utf-8")
    for tk, r in rows.items():
        (root / f"analysis-{tk}-ml-{day}.json").write_text(_json.dumps({"swarm_results": r}), encoding="utf-8")


class TestBoundary654Registered:

    def test_entry_scope_marker_equivalence(self):
        import ic_rerun_readiness as rr
        import signal_archive as sa
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V654] == ["2026-10-19"]
        assert sa.COHORT_SIGNAL_SCOPE[_V654] == ("agent.GuardBeeSentinel.*",)
        assert _V654 in rr._BOUNDARY_MARKERS and _V654 in rr._BOUNDARY_EQUIVALENCE
        assert rr._BOUNDARY_MARKERS[_V654][1] is not rr._BOUNDARY_MARKERS["v0.45.652"][1]

    def test_invalidates_no_rows(self):
        """「作废 0 条」：`assess()` 只按末条边界的日期切 ⇒ 加这条前后切点相同（10-19）。
        变红的变异：条目日期写成新代码上线日（10-28 ⇒ 10-19 ~ 10-27 的样本被作废）。"""
        import ic_rerun_readiness as rr
        without = [e for e in rr._COHORT_HISTORY if e[1] != _V654]
        assert rr.cohort_start()["date"] == without[-1][0] == "2026-10-19"

    def test_moves_no_signal_generation(self):
        """「0 个信号后移」：加这条前后，归档全部信号的当代起点逐个相同（同 v0.45.652）。
        变红的变异：作用域里加上 `guard.macro_adj`（09-28 → 10-19）。"""
        import signal_archive as sa
        hist = sa._cohort_history()
        assert any(v == _V654 for _d, v, _r in hist)
        sigs = set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)
        without = [e for e in hist if e[1] != _V654]
        before, after = sa.generation_boundaries(sigs, without), sa.generation_boundaries(sigs, hist)
        moved = {s for s in sigs if (before.get(s) or {}).get("date") != (after.get(s) or {}).get("date")}
        assert moved == set(), moved

    def test_does_not_truncate_dim_ic_h1_or_h2(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
        import dim_ic_forward_test as F
        import dim_ic_protocol as P
        import ic_rerun_readiness as rr
        entry = next(e for e in rr._COHORT_HISTORY if e[1] == _V654)
        for name, _obj, sigs in P.HYPOTHESES:
            assert F.truncation_point(sigs, P, [entry]) is None, name

    def test_ml_estimator_generation_label_extended(self):
        import probability_scorecard as ps
        assert any(d == "2026-10-19" and _V654 in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)

    def test_guard_success_path_writes_the_marker_unconditionally(self):
        """GuardBee 的 `AgentResult(details={...})` 字面量里无条件写着印记。变红的变异：删掉那一行 / 挪进 if。"""
        found = _guard_details_literals(_MARK654)
        assert len(found) == 1 and isinstance(found[0], ast.Constant) and found[0].value is True, found


def _guard_details_literals(key):
    """GuardBee 源码里所有 `AgentResult(details={...})` 字面量中 `key` 对应的值节点。"""
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1] / "swarm_agents" / "guard_bee.py").read_text(encoding="utf-8"))
    found = []
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "AgentResult":
            for kw in call.keywords:
                if kw.arg == "details" and isinstance(kw.value, ast.Dict):
                    found += [v for k, v in zip(kw.value.keys, kw.value.values)
                              if isinstance(k, ast.Constant) and k.value == key]
    return found


class TestMarkerAndJudge654:

    @pytest.mark.parametrize("d", [{}, {"swarm_results": _row654()},
                                   {"swarm_results": _row654(new=True, mode="backfill")},
                                   {"swarm_results": _row654(guard="errored")}],
                             ids=["empty", "old_code", "backfill", "errored"])
    def test_marker_only_on_live_new_rows(self, d):
        """变红的变异：日期印记不排补跑行；认 652 的键。"""
        import ic_rerun_readiness as rr
        assert rr._marker_guard_macro_quote_dates(d) is False
        assert rr._marker_guard_macro_quote_dates({"swarm_results": _row654(new=True)}) is True
        old652 = _row654()
        old652["agent_details"]["GuardBeeSentinel"]["details"]["macro_event_rows_checked"] = True
        assert rr._marker_guard_macro_quote_dates({"swarm_results": old652}) is False

    @pytest.mark.parametrize("row,expect", [
        (_row654(), True),                                              # 10-19 ~ 10-26 实况
        (_row654(source="treasury+finnhub+fred"), True),                # 10-14 / 10-26 形状（yfinance 全灭）
        (_row654(source="fallback"), True),                             # 整份降级：不调 _set_curve
        (_row654(source="yfinance+fred"), False),                       # 10Y 走 yfinance 或缺失，分不出
        (_row654(source="finnhub+yfinance"), False),
        (_row654(mode="backfill", source=f"cloud_snapshot+treasury+yfinance+fred@{D}"), False),   # 补跑：① 可能生效
        (_row654(guard="no_macro"), False),                             # 宏观细节读不出
        (_row654(source=None), False),
        (_row654(guard="errored"), True),
        (_row654(guard="absent"), True),
    ], ids=["live_treasury", "live_treasury_no_yf", "live_fallback", "live_no_treasury", "live_finnhub_yf",
            "backfill", "no_macro_details", "no_source", "guard_errored", "guard_absent"])
    def test_judge(self, row, expect):
        """变红的变异：判据恒真 / 不排补跑 / 不认 fallback / 不认 treasury / 宏观细节缺时当等价。"""
        import ic_rerun_readiness as rr
        assert rr._equiv_macro_quote_dates({"swarm_results": row, "_date": "2026-10-21", "_ticker": "AAA"}) is expect

    def test_real_old_code_rows_from_10_19_are_all_equivalent(self):
        """生产 10-19 ~ 10-26 六个旧代码扫描日的真实形状（10-27 只读实测：180/180 行实时、来源含 treasury）——
        判据对这几种字面值都成立。⚠️ 这里抄的是**字面值**，不读生产库（测试不碰数据根）。"""
        import ic_rerun_readiness as rr
        for src in ("treasury+finnhub+yfinance+fred", "treasury+finnhub+fred", "treasury+finnhub+yfinance"):
            assert rr._equiv_macro_quote_dates({"swarm_results": _row654(source=src)}) is True, src


class TestBoundaryVerdict654:

    def test_before_deploy_is_no_evidence_yet(self, tmp_path):
        import ic_rerun_readiness as rr
        _write654(tmp_path, "2026-10-19", {"AAA": _row654(), "BBB": _row654()})
        _write654(tmp_path, "2026-10-26", {"AAA": _row654(source="treasury+finnhub+fred"), "BBB": _row654(guard="errored")})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V654)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_on_deploy_day_matches(self, tmp_path):
        """变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.654"]`（⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        _write654(tmp_path, "2026-10-19", {"AAA": _row654(), "BBB": _row654()})
        _write654(tmp_path, "2026-10-26", {"AAA": _row654(), "BBB": _row654()})
        _write654(tmp_path, "2026-10-28", {"AAA": _row654(new=True), "BBB": _row654(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V654)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-10-28", ev

    def test_old_code_on_an_unproven_day_is_too_early(self, tmp_path):
        """新代码没赶上某个扫描日、那天财政部又没供上（10Y 来源分不出）⇒ 报出来，不当作等价。"""
        import ic_rerun_readiness as rr
        _write654(tmp_path, "2026-10-19", {"AAA": _row654(), "BBB": _row654()})
        _write654(tmp_path, "2026-10-27", {"AAA": _row654(source="yfinance+fred"), "BBB": _row654()})
        _write654(tmp_path, "2026-10-28", {"AAA": _row654(new=True), "BBB": _row654(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V654)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-10-27"], ev


# ────────── G. FRED 补跑按 D 的时点取（v0.45.657） ──────────
#
# 旧代码补跑也取「运行当天的最新值」：四个历史补跑日都跑得及时，最新值恰好就是 D 那天的样子（10-27 实拉逐项核过，
# 2Y / CPI / 失业率 / 联邦基金利率 4/4 相同，只有只进展示的 HY 利差偏早）；补跑晚跑几周，Treasury 缺 D 行时
# 进曲线的 2Y 就是几周后的，CPI 还会是 D 之后才发布的月份。修法：每条请求带 realtime_start=realtime_end=D。

D12 = "2026-10-12"   # 哥伦布日：债市休市、股市开 ⇒ 财政部没有 D 行，2Y 走 FRED（10-12 补跑实况）


def _cpi_rows():
    """CPIAUCSL：月初观测、次月 15 日发布（发布日是示意）。2025-08 / 2026-08 两个值是 10-27 实拉的
    （同比 3.35 = 10-12 补跑记录值）；其余月份与 2026-09 那条（D 之后才发布）是虚构的。"""
    real = {"2025-08": "323.291", "2026-08": "334.131", "2026-09": "338.0"}   # 末项虚构
    rows, y, m = [], 2025, 8
    for _ in range(14):                                                      # 2025-08 … 2026-09
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        rows.append((f"{y}-{m:02d}-01", real.get(f"{y}-{m:02d}", "330.0"), f"{ny}-{nm:02d}-15"))
        y, m = ny, nm
    return rows


#: 迷你 ALFRED：{序列: [(观测日, 值, 首次发布日)]}。D12 那天已发布的部分取自 10-27 实拉的时点查询
#: （`realtime_start=realtime_end=2026-10-12`：2Y 4.75@10-08、DFF 3.88@10-08、HY 3.11@10-12 / 3.11@10-09、失业率 4.2@09-01）；
#: 发布日是示意，只保证「D12 时点查询看到的最新一条」与实拉一致。标「虚构」的是 D 之后才发布的值——
#: 晚跑的补跑在旧代码下会拿到它们（2Y 5.40 ⇒ 5.24 − 5.40 = −16bp 倒挂，Guard risk_off +2）。
_ALFRED = {
    "CPIAUCSL": _cpi_rows(),
    "UNRATE": [("2026-09-01", "4.2", "2026-10-02"), ("2026-10-01", "4.4", "2026-11-06")],          # 后者虚构
    "DFF": [("2026-10-07", "3.88", "2026-10-08"), ("2026-10-08", "3.88", "2026-10-09"),
            ("2026-11-05", "3.63", "2026-11-06")],                                                   # 后者虚构
    "DGS2": [("2026-10-07", "4.77", "2026-10-08"), ("2026-10-08", "4.75", "2026-10-09"),
             ("2026-10-09", "4.8", "2026-10-13"), ("2026-10-12", ".", "2026-10-13"),
             ("2026-11-05", "5.40", "2026-11-06")],                                                  # 后者虚构
    "BAMLH0A0HYM2": [("2026-10-08", "3.15", "2026-10-08"), ("2026-10-09", "3.11", "2026-10-09"),
                     ("2026-10-12", "3.11", "2026-10-12"), ("2026-11-05", "3.38", "2026-11-05")],    # 后者虚构
}
_FRED_SERIES = sorted(_ALFRED)


class _FredResp:
    def __init__(self, ok, obs=()):
        self.ok, self._obs = ok, list(obs)

    def json(self):
        return {"observations": self._obs}


class _FakeFred:
    """`get_session("fred")` 的替身。带 `realtime_end` ⇒ 只给那天已发布的观测；不带 ⇒ 「今天」，全都发布了。
    `down` 里的序列回非 2xx。每次请求的参数都记下来。"""

    def __init__(self, down=()):
        self.calls, self.down = [], set(down)

    def get(self, url, params=None, timeout=None):
        assert url == "https://api.stlouisfed.org/fred/series/observations"
        self.calls.append(dict(params))
        sid = params["series_id"]
        if sid in self.down:
            return _FredResp(False)
        rt = params.get("realtime_end")
        assert params.get("realtime_start") == rt, "时点查询只用单日（start = end）"
        rows = sorted((r for r in _ALFRED[sid] if rt is None or r[2] <= rt), key=lambda r: r[0], reverse=True)
        return _FredResp(True, [{"date": d, "value": v} for d, v, _p in rows[:int(params["limit"])]])


def _plug_fred(monkeypatch, fake):
    monkeypatch.setattr(fm, "_RESILIENCE_OK", True)
    monkeypatch.setattr(fm, "get_session", lambda name: fake, raising=False)
    monkeypatch.setattr(fm, "_fred_breaker", None)       # 不碰进程级熔断器的状态


def _pinned(call, day):
    return call.get("realtime_start") == day and call.get("realtime_end") == day


class TestFredVintage:

    def test_backfill_asks_every_series_as_of_d(self, monkeypatch):
        """五个序列都按 D 的时点取；拿到的都是 D 那天已发布的值，观测日都 ≤ D。
        变红的变异：任何一条请求不带 `**_pit`（那条拿到 D 之后的值）。"""
        fake = _FakeFred()
        _plug_fred(monkeypatch, fake)
        r = fm._fetch_fred_series("k", vintage=D12)
        assert sorted(c["series_id"] for c in fake.calls) == _FRED_SERIES
        assert all(_pinned(c, D12) for c in fake.calls), fake.calls
        assert (r["treasury_2y"], r["treasury_2y_date"]) == (4.75, "2026-10-08")
        assert (r["cpi_yoy"], r["cpi_date"]) == (3.35, "2026-08-01")
        assert (r["unemployment"], r["unemployment_date"]) == (4.2, "2026-09-01")
        assert (r["fed_funds_rate"], r["fed_funds_rate_date"]) == (3.88, "2026-10-08")
        assert (r["hy_spread_bp"], r["hy_spread_chg_bp"], r["hy_spread_date"]) == (311.0, 0.0, D12)
        assert r["vintage"] == D12
        assert all(r[k] <= D12 for k in r if k.endswith("_date"))

    def test_realtime_is_latest_and_unpinned(self, monkeypatch):
        """实时口径不变：不带时点参数、取最新、结果没有 `vintage`。变红的变异：实时也钉一个日期。"""
        fake = _FakeFred()
        _plug_fred(monkeypatch, fake)
        r = fm._fetch_fred_series("k")
        assert len(fake.calls) == 5
        assert not any("realtime_start" in c or "realtime_end" in c for c in fake.calls), fake.calls
        assert (r["treasury_2y"], r["treasury_2y_date"]) == (5.40, "2026-11-05")
        assert "vintage" not in r

    def test_failed_series_is_missing_named_and_not_retried_as_latest(self, monkeypatch, caplog):
        """时点请求失败 ⇒ 那项没取到、WARNING 点名，**不回落最新值**（回落就又是晚跑拿到未来的值）。
        变红的变异：删掉 WARNING；失败后不带时点重取一次。"""
        fake = _FakeFred(down={"DGS2"})
        _plug_fred(monkeypatch, fake)
        with caplog.at_level("WARNING", logger="alpha_hive.fred_macro"):
            r = fm._fetch_fred_series("k", vintage=D12)
        assert "treasury_2y" not in r and r["cpi_yoy"] == 3.35 and r["vintage"] == D12
        assert len(fake.calls) == 5 and all(_pinned(c, D12) for c in fake.calls), fake.calls
        assert any(D12 in m and "treasury_2y" in m for m in caplog.messages), caplog.messages

    def test_all_failed_is_empty_without_vintage_key(self, monkeypatch, caplog):
        """全没取到 ⇒ `{}`。带着 `vintage` 的「空结果」会被当成有 FRED（`data_source` 写成 `+fred`）。
        变红的变异：不论有没有取到都写 `vintage`。"""
        _plug_fred(monkeypatch, _FakeFred(down=set(_ALFRED)))
        with caplog.at_level("WARNING", logger="alpha_hive.fred_macro"):
            r = fm._fetch_fred_series("k", vintage=D12)
        assert r == {}
        assert any(D12 in m and all(k in m for k in fm._FRED_FIELDS) for m in caplog.messages), caplog.messages

    def test_realtime_failure_log_is_unchanged(self, monkeypatch, caplog):
        """实时口径的日志不变（此前缺一项不报 WARNING，本版不加噪音）。变红的变异：缺项 WARNING 不看 `vintage`。"""
        _plug_fred(monkeypatch, _FakeFred(down={"DGS2"}))
        with caplog.at_level("WARNING", logger="alpha_hive.fred_macro"):
            fm._fetch_fred_series("k")
        assert not [m for m in caplog.messages if "FRED" in m], caplog.messages


# ────────── H. 世代边界登记（v0.45.657：同 654，并进 10-19 同日标签 + 等价判据） ──────────

_V657 = "v0.45.657"
_MARK657 = "macro_fred_vintage_checked"


def _row657(*, new=False, **kw):
    """同 `_row654`；新代码同时写 654 与 657 两个印记。"""
    r = _row654(new=new, **kw)
    if new:
        r["agent_details"]["GuardBeeSentinel"]["details"][_MARK657] = True
    return r


class TestBoundary657Registered:

    def test_entry_scope_marker_equivalence(self):
        import ic_rerun_readiness as rr
        import signal_archive as sa
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V657] == ["2026-10-19"]
        assert sa.COHORT_SIGNAL_SCOPE[_V657] == ("agent.GuardBeeSentinel.*",)
        assert _V657 in rr._BOUNDARY_MARKERS and _V657 in rr._BOUNDARY_EQUIVALENCE
        others = {rr._BOUNDARY_MARKERS[v][1] for v in ("v0.45.652", "v0.45.654")}
        assert rr._BOUNDARY_MARKERS[_V657][1] not in others

    def test_invalidates_no_rows(self):
        """「作废 0 条」：加这条前后 `assess()` 的切点相同（10-19）。变红的变异：条目日期写成新代码上线日。"""
        import ic_rerun_readiness as rr
        without = [e for e in rr._COHORT_HISTORY if e[1] != _V657]
        assert rr.cohort_start()["date"] == without[-1][0] == "2026-10-19"

    def test_moves_no_signal_generation(self):
        """「0 个信号后移」。变红的变异：作用域里加上 `guard.macro_adj`。"""
        import signal_archive as sa
        hist = sa._cohort_history()
        assert any(v == _V657 for _d, v, _r in hist)
        sigs = set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)
        without = [e for e in hist if e[1] != _V657]
        before, after = sa.generation_boundaries(sigs, without), sa.generation_boundaries(sigs, hist)
        moved = {s for s in sigs if (before.get(s) or {}).get("date") != (after.get(s) or {}).get("date")}
        assert moved == set(), moved

    def test_does_not_truncate_dim_ic_h1_or_h2(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
        import dim_ic_forward_test as F
        import dim_ic_protocol as P
        import ic_rerun_readiness as rr
        entry = next(e for e in rr._COHORT_HISTORY if e[1] == _V657)
        for name, _obj, sigs in P.HYPOTHESES:
            assert F.truncation_point(sigs, P, [entry]) is None, name

    def test_ml_estimator_generation_label_extended(self):
        import probability_scorecard as ps
        assert any(d == "2026-10-19" and _V657 in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)

    def test_guard_success_path_writes_the_marker_unconditionally(self):
        """变红的变异：删掉那一行 / 挪进 if。"""
        found = _guard_details_literals(_MARK657)
        assert len(found) == 1 and isinstance(found[0], ast.Constant) and found[0].value is True, found


class TestMarkerAndJudge657:

    @pytest.mark.parametrize("d", [{}, {"swarm_results": _row654()},
                                   {"swarm_results": _row654(new=True)},
                                   {"swarm_results": _row657(new=True, mode="backfill")},
                                   {"swarm_results": _row654(guard="errored")}],
                             ids=["empty", "old_code", "only_654_marker", "backfill", "errored"])
    def test_marker_only_on_live_new_rows(self, d):
        """变红的变异：日期印记不排补跑行；认 654 的键。"""
        import ic_rerun_readiness as rr
        assert rr._marker_guard_fred_vintage(d) is False
        assert rr._marker_guard_fred_vintage({"swarm_results": _row657(new=True)}) is True

    @pytest.mark.parametrize("row,expect", [
        (_row654(), True),                                                   # 10-19 ~ 10-26 实况
        (_row654(source="yfinance+fred"), True),                             # 实时行：FRED 请求逐字不变，与来源无关
        (_row654(source="fallback"), True),
        (_row654(mode="backfill", source=f"cloud_snapshot+yfinance+fred@{D12}"), False),   # 补跑：可能变
        (_row654(mode=None), False),                                         # v0.45.366 之前没有这个键 ⇒ 证不出
        (_row654(guard="no_macro"), False),
        (_row654(guard="errored"), True),
        (_row654(guard="absent"), True),
    ], ids=["live", "live_no_treasury", "live_fallback", "backfill", "no_mode", "no_macro_details",
            "guard_errored", "guard_absent"])
    def test_judge(self, row, expect):
        """变红的变异：判据恒真 / 不排补跑 / 缺口径键当实时 / 宏观细节缺时当等价 / 不认实时行。"""
        import ic_rerun_readiness as rr
        assert rr._equiv_fred_vintage({"swarm_results": row, "_date": "2026-10-21", "_ticker": "AAA"}) is expect


class TestBoundaryVerdict657:

    def test_before_deploy_is_no_evidence_yet(self, tmp_path):
        import ic_rerun_readiness as rr
        _write654(tmp_path, "2026-10-19", {"AAA": _row654(), "BBB": _row654()})
        _write654(tmp_path, "2026-10-26", {"AAA": _row654(source="treasury+finnhub+fred"), "BBB": _row654(guard="errored")})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V657)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_on_deploy_day_matches(self, tmp_path):
        """变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.657"]`（⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        _write654(tmp_path, "2026-10-19", {"AAA": _row654(), "BBB": _row654()})
        _write654(tmp_path, "2026-10-27", {"AAA": _row654(new=True), "BBB": _row654(new=True)})   # 654 上线、657 还没
        _write654(tmp_path, "2026-10-28", {"AAA": _row657(new=True), "BBB": _row657(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V657)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-10-28", ev

    def test_old_code_backfill_after_boundary_is_too_early(self, tmp_path):
        """旧代码补跑了边界之后的某天 ⇒ 那几行可能用了运行时的 FRED 最新值 ⇒ 报出来，不当作等价。"""
        import ic_rerun_readiness as rr
        _write654(tmp_path, "2026-10-19", {"AAA": _row654(), "BBB": _row654()})
        _write654(tmp_path, "2026-10-27", {"AAA": _row654(mode="backfill"), "BBB": _row654()})
        _write654(tmp_path, "2026-10-28", {"AAA": _row657(new=True), "BBB": _row657(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V657)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-10-27"], ev
