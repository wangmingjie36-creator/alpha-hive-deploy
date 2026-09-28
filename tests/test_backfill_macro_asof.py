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
"""
from datetime import date, datetime
from unittest.mock import patch

import pytest

import cboe_vix
import economic_calendar
import fred_macro as fm
from is_trading_day import is_trading_day

# 模块加载早于任何 fixture：此刻抓到的是真函数（下面有条测试要在桩之后换回真日历）
_REAL_NEXT_EVENT = economic_calendar.get_next_event

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
        monkeypatch.setattr(economic_calendar, "get_next_event",
                            lambda ref_date=None: (self.cal_refs.append(ref_date), None)[1])

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
        """变红的变异：`get_next_event()` 不带 `ref_date`（按运行当天数）。"""
        fm.set_macro_snapshot(D, MARKET_827)
        _guard_live_macro(self._snap_macro())
        assert self.cal_refs == [date(2026, 8, 27)]

    def test_fomc_vote_uses_the_real_calendar_on_d(self, monkeypatch):
        """真日历正对照：取 D = 某次 FOMC 前 2 天、且真日历说「D 的下一个事件就是这次 FOMC」
        （`get_next_event` 只给最近一个事件，CPI / GDP 抢在前面时 Guard 本就不投 FOMC 票）⇒ risk_off +1。"""
        from datetime import timedelta
        monkeypatch.setattr(economic_calendar, "get_next_event", _REAL_NEXT_EVENT)
        ref = None
        for f in economic_calendar._parse_dates(economic_calendar._FOMC):
            cand = f - timedelta(days=2)
            nxt = _REAL_NEXT_EVENT(ref_date=cand)
            if (date(2026, 1, 1) <= cand < date(2026, 9, 1) and is_trading_day(cand)[0]
                    and nxt and nxt["type"] == "fomc" and nxt["days_until"] == 2):
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
        monkeypatch.setattr(economic_calendar, "get_next_event",
                            lambda ref_date=None: (self.cal_refs.append(ref_date), None)[1])

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

    @pytest.mark.parametrize("snap", [None, D], ids=["realtime", "backfill"])
    def test_real_guard_output_carries_the_marker_in_both_modes(self, snap):
        """正对照拿**生产类的真输出**。实时口径也必须带：本条只改补跑，只在补跑行写的印记会让每份实时归档
        都被判「边界之后无印记」⇒ 恒报 boundary_too_early。
        变红的变异：只在补跑时写 `macro_as_of_mode`；或把 guard_bee.py 里的键改名。"""
        import ic_rerun_readiness as rr
        fm.set_macro_snapshot(snap, MARKET_827 if snap else None)
        body = _guard_body(_guard_live_macro({"vix": 15.0, "vix_source": "cboe"})["details"])
        assert rr._marker_guard_macro_as_of_mode(body) is True

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
