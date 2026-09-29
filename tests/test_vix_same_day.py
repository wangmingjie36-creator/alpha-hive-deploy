"""日报 VIX：当日收盘优先 / 陈旧可见 / 变动与数值同出一对观测（v0.45.357）

背景（2026-09-28 实测，只读）：
- CBOE `VIX_History.csv` 要到美东约 20:30 才追加当日行（09-25 那行 `Last-Modified`
  20:30:54 ET），日报扫描 17:00 ET ⇒ 19 份 CBOE 日报里 12 份的 `vix` 落后一场、
  09-24/25 两份（下载失败读过期缓存，只打了一行 INFO）落后两三场，标签照写 `cboe`。
- `vix_change_pct` 取自 yfinance 那条腿、是当日变动 ⇒ 与 `vix` 错位一天。
- 滞后值经 GuardBee `vix<15` 票进评分：按归档票数重放，293 行里 38 行宏观政体会不同。

本文件守：报价只在收盘后、且时间戳证明是这一场收盘时才被采用（盘中值绝不冒充收盘）；
拒收 / 陈旧 / 核对不符都有会红的观测点；GuardBee 不拿陈旧 VIX 投票。
"""
from datetime import date, datetime, time
from unittest.mock import patch

import pytest

import cboe_vix
import fred_macro as fm
from is_trading_day import is_trading_day, session_close_et


def _et(y, mo, d, h, mi, s=0):
    """美东朴素时间（被测函数对 aware 一律先 `.replace(tzinfo=None)`）。"""
    return datetime(y, mo, d, h, mi, s)


def _quote(price=15.84, ltt="2026-09-11T16:15:00", prev=17.84):
    return {"symbol": "^VIX", "current_price": price, "close": price,
            "prev_day_close": prev, "last_trade_time": ltt}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path, stub_cboe_vix):
    """报价与 CSV 都不出网；账本落 tmp。每条测试自己再覆盖需要的桩。"""
    monkeypatch.setattr(cboe_vix, "_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(cboe_vix, "_CACHE_PATH", tmp_path / "vix.csv")
    yield


def _quote_calls(monkeypatch, q):
    calls = []

    def _fake():
        calls.append(1)
        return q
    monkeypatch.setattr(cboe_vix, "_download_quote", _fake)
    return calls


# ────────── A. 报价什么时候才算「这一场的收盘」 ──────────

class TestSessionCloseGate:

    def test_calendar_preconditions(self):
        """下面的用例依赖这些日历事实 —— 先断言，别心算。"""
        assert is_trading_day(date(2026, 9, 11))[0]
        assert not is_trading_day(date(2026, 9, 12))[0]
        assert session_close_et(date(2026, 11, 27)) == time(13, 0)   # 感恩节次日提前收盘

    def test_before_vix_close_does_not_even_fetch(self, monkeypatch):
        """16:10 ET：股票收了但 VIX 还在算（16:15 停） ⇒ 不取、不用。"""
        calls = _quote_calls(monkeypatch, _quote())
        obs, why = cboe_vix.get_vix_session_close(_et(2026, 9, 11, 16, 10))
        assert (obs, why) == (None, "before_close") and calls == []

    def test_intraday_is_never_a_close(self, monkeypatch):
        calls = _quote_calls(monkeypatch, _quote(ltt="2026-09-11T11:00:00"))
        assert cboe_vix.get_vix_session_close(_et(2026, 9, 11, 11, 0)) == (None, "before_close")
        assert calls == []

    def test_non_trading_day(self, monkeypatch):
        _quote_calls(monkeypatch, _quote())
        assert cboe_vix.get_vix_session_close(_et(2026, 9, 12, 17, 0)) == (None, "not_trading_day")

    def test_after_close_with_close_stamped_quote_is_used(self, monkeypatch):
        _quote_calls(monkeypatch, _quote())
        obs, why = cboe_vix.get_vix_session_close(_et(2026, 9, 11, 17, 10))
        assert why == "ok"
        assert (obs["close"], obs["as_of"], obs["prev_close_quote"]) == (15.84, "2026-09-11", 17.84)

    @pytest.mark.parametrize("ltt", [
        "2026-09-11T12:10:00",   # CDN 发的盘中陈旧文件（cboe_options v0.45.234 实测同形）
        "2026-09-10T16:15:00",   # 昨天的
        "2026-09-11T20:20:00",   # 晚于收盘窗口：已不是这一场的收盘值
        None,                    # 没时间戳：判不了就不用
    ])
    def test_quote_not_stamped_at_this_close_is_rejected(self, monkeypatch, ltt):
        _quote_calls(monkeypatch, _quote(ltt=ltt))
        assert cboe_vix.get_vix_session_close(_et(2026, 9, 11, 17, 10)) == (None, "quote_vintage_mismatch")

    def test_early_close_day_uses_its_own_close(self, monkeypatch):
        _quote_calls(monkeypatch, _quote(ltt="2026-11-27T13:15:00"))
        assert cboe_vix.get_vix_session_close(_et(2026, 11, 27, 13, 10))[1] == "before_close"
        assert cboe_vix.get_vix_session_close(_et(2026, 11, 27, 13, 20))[1] == "ok"

    @pytest.mark.parametrize("q,why", [(None, "quote_unavailable"),
                                       (_quote(price=0.0), "quote_bad_value"),
                                       (_quote(price="n/a"), "quote_bad_value")])
    def test_bad_or_missing_quote(self, monkeypatch, q, why):
        _quote_calls(monkeypatch, q)
        assert cboe_vix.get_vix_session_close(_et(2026, 9, 11, 17, 10)) == (None, why)


# ────────── B. 自动核对账本 ──────────

class TestQuoteLedger:

    def test_pending_is_verified_once_csv_publishes_that_session(self):
        cboe_vix._record_quote_close("2026-09-11", 15.84, "2026-09-11T16:15:00", "x")
        assert cboe_vix.verify_quote_ledger([("2026-09-10", 17.84)])["pending"] == 1
        r = cboe_vix.verify_quote_ledger([("2026-09-10", 17.84), ("2026-09-11", 15.84)])
        assert (r["verified"], r["pending"], r["mismatches"]) == (1, 0, [])

    def test_mismatch_is_loud_and_disables_the_quote(self, monkeypatch, caplog):
        cboe_vix._record_quote_close("2026-09-11", 15.90, None, "x")
        with caplog.at_level("WARNING", logger="alpha_hive.cboe_vix"):
            r = cboe_vix.verify_quote_ledger([("2026-09-11", 15.84)])
        assert r["mismatches"] == [{"session": "2026-09-11", "quote": 15.90, "csv": 15.84}]
        assert any("不符" in m for m in caplog.messages)
        calls = _quote_calls(monkeypatch, _quote(ltt="2026-09-14T16:15:00"))
        assert cboe_vix.get_vix_session_close(_et(2026, 9, 14, 17, 10)) == (None, "disabled_after_mismatch")
        assert calls == []                         # fail-closed：连取都不取

    def test_old_mismatch_ages_out(self, monkeypatch):
        cboe_vix._record_quote_close("2026-08-20", 15.90, None, "x")
        cboe_vix.verify_quote_ledger([("2026-08-20", 15.84)])
        _quote_calls(monkeypatch, _quote(ltt="2026-09-14T16:15:00"))
        assert cboe_vix.get_vix_session_close(_et(2026, 9, 14, 17, 10))[1] == "ok"


# ────────── C. 陈旧判定 ──────────

class TestStaleness:

    @pytest.mark.parametrize("as_of,now,expect", [
        ("2026-09-11", _et(2026, 9, 11, 17, 10), (0, False)),   # 当日收盘
        ("2026-09-10", _et(2026, 9, 11, 17, 10), (1, False)),   # CSV 常态：落后一场不算陈旧
        ("2026-09-22", _et(2026, 9, 24, 17, 10), (2, True)),    # 09-24 日报实况
        ("2026-09-22", _et(2026, 9, 25, 17, 10), (3, True)),    # 09-25 日报实况
        ("2026-09-25", _et(2026, 9, 28, 6, 49), (0, False)),    # 周一盘前：最新一场就是上周五
        ("2026-09-04", _et(2026, 9, 8, 17, 10), (1, False)),    # 跨劳动节 09-07
        ("2026-09-10", _et(2026, 9, 11, 16, 5), (0, False)),    # VIX 未停算：最新已收完的是昨天
    ])
    def test_cases(self, as_of, now, expect):
        assert cboe_vix.vix_staleness(as_of, now) == expect

    def test_unknown_as_of_is_unknown_not_fresh(self):
        assert cboe_vix.vix_staleness(None, _et(2026, 9, 11, 17, 10)) == (None, None)


# ────────── D. 组装观测（fred_macro 的唯一入口） ──────────

def _csv(monkeypatch, rows):
    monkeypatch.setattr(cboe_vix, "get_vix_history",
                        lambda max_days=None, force_refresh=False: rows[-max_days:] if max_days else rows)


class TestObservation:

    def test_after_close_quote_wins_and_is_booked(self, monkeypatch):
        _csv(monkeypatch, [("2026-09-09", 16.46), ("2026-09-10", 17.84)])
        _quote_calls(monkeypatch, _quote())
        o = cboe_vix.get_vix_observation(_et(2026, 9, 11, 17, 10))
        assert (o["vix"], o["as_of"], o["feed"], o["quote_reason"]) == (15.84, "2026-09-11", "delayed_quote", "ok")
        assert (o["prev_close"], o["prev_as_of"]) == (17.84, "2026-09-10")     # 前一收盘取 CSV 官方值
        assert (o["lag_sessions"], o["stale"]) == (0, False)
        assert cboe_vix._read_ledger()["2026-09-11"]["status"] == "pending"

    def test_quote_prev_close_disagreeing_with_csv_is_rejected(self, monkeypatch):
        _csv(monkeypatch, [("2026-09-09", 16.46), ("2026-09-10", 17.84)])
        _quote_calls(monkeypatch, _quote(prev=17.10))
        o = cboe_vix.get_vix_observation(_et(2026, 9, 11, 17, 10))
        assert (o["feed"], o["quote_reason"], o["vix"], o["as_of"]) == \
            ("history_csv", "quote_prev_close_disagrees", 17.84, "2026-09-10")
        assert cboe_vix._read_ledger() == {}

    def test_before_close_falls_back_to_csv_and_says_why(self, monkeypatch):
        _csv(monkeypatch, [("2026-09-09", 16.46), ("2026-09-10", 17.84)])
        o = cboe_vix.get_vix_observation(_et(2026, 9, 11, 15, 0))
        assert (o["feed"], o["quote_reason"], o["vix"], o["lag_sessions"], o["stale"]) == \
            ("history_csv", "before_close", 17.84, 0, False)

    def test_stale_cache_is_visible(self, monkeypatch):
        """09-24 实况：下载失败 → 过期缓存（末行 09-22）→ 取数方式与陈旧都得说出来。"""
        (cboe_vix._cache_path()).write_text(
            "DATE,OPEN,HIGH,LOW,CLOSE\n09/21/2026,1,1,1,14.87\n09/22/2026,1,1,1,14.21\n", encoding="utf-8")
        import os
        os.utime(cboe_vix._cache_path(), (0, 0))                 # 远超 6h TTL
        o = cboe_vix.get_vix_observation(_et(2026, 9, 24, 17, 10))
        assert (o["history_fetch"], o["vix"], o["as_of"], o["stale"], o["lag_sessions"]) == \
            ("cache_stale", 14.21, "2026-09-22", True, 2)

    def test_empty_everything_keeps_all_keys(self, monkeypatch):
        _csv(monkeypatch, [])
        o = cboe_vix.get_vix_observation(_et(2026, 9, 11, 17, 10))
        assert o["vix"] is None and o["stale"] is None and o["quote_reason"] == "quote_unavailable"
        assert set(o) >= {"vix", "as_of", "feed", "quote_reason", "history_fetch",
                          "lag_sessions", "stale", "quote_check"}


# ────────── E. fred_macro：vix_change_pct 与 vix 同出一对观测 ──────────

class TestFredMacroAlignment:

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, stub_yfinance, stub_fred):
        fm.set_macro_snapshot(None)
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        monkeypatch.setattr(fm, "_asof_history", lambda *a, **k: None)
        import cboe_options
        monkeypatch.setattr(cboe_options, "_et_now", lambda: _et(2026, 9, 11, 17, 10))
        yield
        # v0.45.366：teardown 也卸（此前只在 setup 卸，`test_no_pair_means_none_not_zero` 装的快照
        # 会留给下一条；Guard 读这个全局，conftest `_no_leaked_macro_snapshot` 现在会把它报出来）
        fm.set_macro_snapshot(None)
        fm._CACHE, fm._CACHE_TS = {}, 0.0

    def _same_day(self, monkeypatch, vix_leg=None):
        d = {"TNX": {"last": 4.96, "prev": 4.96, "change_pct": 0.0}}
        if vix_leg:
            d["VIX"] = vix_leg
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (d, {"TNX": "treasury_gov@2026-09-11"}))

    def test_csv_path_change_is_its_own_pair_not_the_yfinance_leg(self, monkeypatch):
        """09-11 实况：vix=17.84（09-10 收盘）配 −11.21%（09-11 当日）⇒ 错位。修后取 17.84/16.46−1。"""
        _csv(monkeypatch, [("2026-09-09", 16.46), ("2026-09-10", 17.84)])
        self._same_day(monkeypatch, {"last": 15.84, "prev": 17.84, "change_pct": -11.21})
        r = fm.get_macro_context()
        assert (r["vix"], r["vix_as_of"], r["vix_feed"]) == (17.84, "2026-09-10", "history_csv")
        assert r["vix_change_pct"] == round((17.84 / 16.46 - 1) * 100, 2)

    def test_quote_path_is_same_day_and_change_matches_it(self, monkeypatch):
        _csv(monkeypatch, [("2026-09-09", 16.46), ("2026-09-10", 17.84)])
        _quote_calls(monkeypatch, _quote())
        self._same_day(monkeypatch, {"last": 15.84, "prev": 17.84, "change_pct": -11.21})
        r = fm.get_macro_context()
        assert (r["vix"], r["vix_as_of"], r["vix_feed"], r["vix_stale"]) == (15.84, "2026-09-11", "delayed_quote", False)
        assert r["vix_change_pct"] == -11.21 and r["vix_regime"] == "moderate"

    def test_spike_headwind_names_the_two_sessions(self, monkeypatch):
        _csv(monkeypatch, [("2026-09-09", 14.00), ("2026-09-10", 17.84)])
        self._same_day(monkeypatch)
        hw = " ".join(fm.get_macro_context()["macro_headwinds"])
        assert "VIX 单日飙升 +27.4%（2026-09-09→2026-09-10" in hw

    def test_no_pair_means_none_not_zero(self, monkeypatch):
        """快照 VIX 没有配对的前一收盘：旧实现给 yfinance 的当日值或 0.0，现在是 None。"""
        _csv(monkeypatch, [])
        self._same_day(monkeypatch)
        fm.set_macro_snapshot("2026-08-27", {"cboe": {"vix_term": {"vix_spot": 15.21}}})
        r = fm.get_macro_context()
        assert r["vix_source"] == "cloud_snapshot_cboe" and r["vix_change_pct"] is None
        assert not any("单日飙升" in h for h in r["macro_headwinds"])

    def test_yfinance_only_keeps_its_own_change(self, monkeypatch):
        _csv(monkeypatch, [])
        self._same_day(monkeypatch, {"last": 15.84, "prev": 17.84, "change_pct": -11.21})
        r = fm.get_macro_context()
        assert (r["vix_source"], r["vix"], r["vix_change_pct"]) == ("yfinance", 15.84, -11.21)

    def test_partial_path_09_25_shape(self, monkeypatch):
        """09-25 实况：其余宏观全灭、CBOE 读到过期缓存（末行 09-22）。"""
        import cboe_options
        monkeypatch.setattr(cboe_options, "_et_now", lambda: _et(2026, 9, 25, 17, 10))
        _csv(monkeypatch, [("2026-09-21", 14.87), ("2026-09-22", 14.21)])
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: ({}, {}))
        r = fm.get_macro_context()
        assert r["data_source"] == "fallback" and r["vix_source"] == "cboe"
        assert (r["vix_stale"], r["vix_lag_sessions"], r["vix_as_of"]) == (True, 3, "2026-09-22")
        assert r["vix_change_pct"] == round((14.21 / 14.87 - 1) * 100, 2)   # 旧：键都没有

    def test_full_fallback_has_every_key_as_none(self, monkeypatch):
        _csv(monkeypatch, [])
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: ({}, {}))
        r = fm.get_macro_context()
        assert r["vix_source"] == "fallback"
        for k in ("vix_change_pct", *fm._VIX_OBS_KEYS):
            assert k in r and r[k] is None, k


# ────────── F. GuardBee：陈旧 VIX 不投票 ──────────

def _guard(macro):
    from swarm_agents.guard_bee import GuardBeeSentinel
    g = GuardBeeSentinel.__new__(GuardBeeSentinel)
    with patch("fred_macro.get_macro_context", return_value=macro):
        return g._calc_macro_adjustment("AMC")


class TestGuardStaleVix:

    @pytest.fixture(autouse=True)
    def _offline(self, stub_yfinance, stub_vixcentral):
        pass

    _M = {"data_source": "fallback", "vix": 14.21, "vix_regime": "low", "vix_source": "cboe",
          "vix_as_of": "2026-09-22", "vix_feed": "history_csv"}

    def test_stale_vix_casts_no_vote(self):
        """09-25 AMC 实况：票面 {'risk_on': 1} 全来自这张陈旧票 ⇒ 去掉后回到 neutral。"""
        r = _guard({**self._M, "vix_stale": True})
        assert r["regime_votes"]["risk_on"] == 0 and r["regime"] == "neutral" and r["score_adj"] == 0.0
        assert "vix" not in r["details"] and r["details"]["vix_stale"] is True

    @pytest.mark.parametrize("stale", [False, None])
    def test_fresh_or_unknown_still_votes(self, stale):
        r = _guard({**self._M, "vix_stale": stale})
        assert r["regime_votes"]["risk_on"] >= 1 and r["details"]["vix"] == 14.21

    def test_feed_key_is_always_written(self):
        """`vix_feed` 键的有无是世代边界印记 —— 连宏观整个取不到时也要在。"""
        assert "vix_feed" in _guard({})["details"]


# ────────── G. 看得见：日报表格与仪表板 ──────────

class TestStaleIsRendered:

    _M = {"data_source": "treasury+finnhub+fred", "macro_regime": "neutral", "macro_score": 6.0,
          "vix": 14.21, "vix_regime": "low", "vix_source": "cboe",
          "vix_as_of": "2026-09-22", "vix_prev_as_of": "2026-09-21", "vix_prev_close": 14.87}

    def test_report_table_names_the_session_and_flags_stale(self):
        from report_formatters import _build_macro
        row = next(ln for ln in _build_macro({**self._M, "vix_stale": True}) if ln.startswith("| VIX"))
        assert "2026-09-22 收盘" in row and "陈旧" in row
        row = next(ln for ln in _build_macro({**self._M, "vix_stale": False}) if ln.startswith("| VIX"))
        assert "2026-09-22 收盘" in row and "陈旧" not in row

    def test_dashboard_marks_stale_instead_of_a_blank(self):
        import dashboard_renderer as dr
        out = dr._macro_deltas({**self._M, "vix_stale": True}, None, None, "2026-09-25")
        assert "陈旧" in out["vix"] and "2026-09-22" in out["vix"]
        assert dr._macro_deltas({**self._M, "vix_stale": False}, None, None, "2026-09-25")["vix"] == ""


# ────────── H. 世代边界印记（ic_rerun_readiness v0.45.357） ──────────

def _guard_body(vt):
    return {"swarm_results": {"agent_details": {"GuardBeeSentinel": {"details": {"vix_term_structure": vt}}}}}


class TestBoundaryMarker:

    @pytest.fixture(autouse=True)
    def _offline(self, stub_yfinance, stub_vixcentral):
        """真 Guard 路径还会取 VIX 期限结构（yfinance 现货 + vixcentral 期货）。"""

    def test_real_guard_output_carries_the_marker(self):
        """正对照拿**生产类的真输出**：印记键名与 GuardBee 实际写进归档的一致。
        变红的变异：把 guard_bee.py 里 `details["vix_feed"]` 改名或删掉。"""
        import ic_rerun_readiness as rr
        body = _guard_body(_guard({"vix": 15.0, "vix_source": "cboe", "vix_feed": "delayed_quote"})["details"])
        assert rr._marker_guard_vix_feed(body) is True

    @pytest.mark.parametrize("d", [
        {}, _guard_body(None),
        _guard_body({"macro_data_source": "fallback", "vix_source": "cboe", "vix": 14.21}),   # 09-25 实际归档形状
    ])
    def test_old_shapes_are_not(self, d):
        import ic_rerun_readiness as rr
        assert rr._marker_guard_vix_feed(d) is False

    def test_boundary_day_matches(self, tmp_path):
        import json as _json
        import ic_rerun_readiness as rr
        b = next(d for d, v, _r in rr._COHORT_HISTORY if v == "v0.45.357")
        (tmp_path / "analysis-ZZZ-ml-2026-09-25.json").write_text(_json.dumps(
            _guard_body({"vix_source": "cboe", "vix": 14.21})), encoding="utf-8")
        (tmp_path / f"analysis-AAA-ml-{b}.json").write_text(_json.dumps(
            _guard_body({"vix_feed": None})), encoding="utf-8")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.357")
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == b, ev
