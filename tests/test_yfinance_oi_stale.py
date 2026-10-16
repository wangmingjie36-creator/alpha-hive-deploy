"""yfinance 链 OI 陈旧 ⇒ P/C / total_oi / 主链 GEX 记不可得（v0.45.512，`options_analyzer.assess_oi_staleness`）。

**事故。** 2026-10-12（哥伦布日）Yahoo 全市场的 openInterest 几乎清零，只剩几个月没成交的旧合约上冻结的旧数。BILI 那天
没有 CBOE 链、走 yfinance：±70% 行权价窗口 + 「按 OI 取前 40」恰好把冻结旧数挑了出来 ⇒ $5 put 22,065 ÷ $8 call 74
= **298.46**（前后几天 ≈1.0），Oracle 被压 0.85、Bear 被抬 1.06。

**判据。** 本链（全部到期日、过滤之前）以最新一笔成交为参照、往前 4 天内有成交的合约里 OI>0 的占比：10-12 录制的
30 只全部 ≤ 0.04；10-16 正常日实测 12 只 0.81–0.99。< 0.5 判陈旧；这类合约不足 20 个判不了（照旧算）。

本文件零网络：yfinance 打桩，成交时刻写死日期（判据只跟本链自己的最新成交比，不读钟）。
"""
from __future__ import annotations

import logging
import types
from datetime import date, timedelta

import pandas as pd
import pytest

import options_analyzer as oa
import scan_timing
from tests.test_yahoo_iv_placeholders import cache_writes, iv_agent  # noqa: F401  (夹具)

PRICE = 15.19
RECENT = pd.Timestamp("2026-10-09 19:59:00", tz="UTC")      # 上一场最后成交
OLD_PUT = pd.Timestamp("2026-05-14 15:00:00", tz="UTC")     # 冻结旧数的那几个合约
OLD_CALL = pd.Timestamp("2026-06-24 15:00:00", tz="UTC")


def _exp(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _rows(strikes, oi, when, iv=0.5):
    return {"strike": list(strikes), "openInterest": list(oi), "volume": [12] * len(strikes),
            "impliedVolatility": [iv] * len(strikes), "lastPrice": [1.0] * len(strikes),
            "lastTradeDate": list(when)}


def _chain_frames(stale: bool):
    """BILI 10-12 形状：每个到期日 12 档近价合约（最近有成交）；陈旧链里它们 OI 全 0，健康链里 OI 正常。
    第一个到期日另有三个几个月没成交、OI 冻结的旧合约（$5 put 22,065 / $24 put 21 / $8 call 74）。"""
    near = [11.0 + 0.5 * i for i in range(12)]
    oi = [0] * 12 if stale else [300 + 40 * i for i in range(12)]
    out = []
    for k in range(4):
        c = _rows(near, oi, [RECENT] * 12)
        p = _rows(near, [o // 2 for o in oi] if not stale else oi, [RECENT] * 12)
        if k == 0:
            c = {key: c[key] + v for key, v in _rows([8.0], [74], [OLD_CALL]).items()}
            p = {key: p[key] + v for key, v in _rows([5.0, 24.0], [22065, 21], [OLD_PUT, OLD_PUT]).items()}
        out.append((pd.DataFrame(c), pd.DataFrame(p)))
    return out


def _fetcher(monkeypatch, stale=True, frames=None):
    frames = frames or _chain_frames(stale)
    exps = [_exp(d) for d in (35, 91, 182, 427)]

    class _Ticker:
        options = tuple(exps)

        def __init__(self, t):
            self.fast_info = {"lastPrice": PRICE}

        def option_chain(self, e):
            c, p = frames[exps.index(e)]
            return types.SimpleNamespace(calls=c.copy(), puts=p.copy())

    monkeypatch.setattr(oa, "yf", types.SimpleNamespace(Ticker=_Ticker))
    monkeypatch.setattr(oa, "_opt_cb", None)
    f = oa.OptionsDataFetcher()
    monkeypatch.setattr(f, "_try_cboe", lambda t, p=0.0: None)
    monkeypatch.setattr(f, "_read_cache", lambda *a, **k: None)
    monkeypatch.setattr(f, "_write_cache", lambda *a, **k: None)
    return f


# ═══════════════════════════════ 判据（纯函数） ═══════════════════════════════

def _r(n_recent, n_with_oi, n_old=0, old_oi=22065.0):
    t0 = RECENT.timestamp()
    rows = [(t0, 100.0)] * n_with_oi + [(t0, 0.0)] * (n_recent - n_with_oi)
    rows += [(OLD_PUT.timestamp(), old_oi)] * n_old
    return rows


class TestAssess:
    def test_outage_shape_is_stale_and_old_frozen_rows_do_not_count(self):
        """变红的变异：分母把旧合约也数进来（冻结旧数会把占比抬上去）；或阈值方向反了。"""
        out = oa.assess_oi_staleness(_r(100, 0, n_old=3))
        assert out == {"recent_contracts": 100, "recent_with_oi": 0, "ratio": 0.0, "judgeable": True, "stale": True}

    def test_healthy_lowest_observed_is_not_stale(self):
        """10-16 实测最低的 BILI 0.81。"""
        assert not oa.assess_oi_staleness(_r(108, 87))["stale"]

    def test_threshold_is_strictly_below_half(self):
        assert not oa.assess_oi_staleness(_r(40, 20))["stale"]
        assert oa.assess_oi_staleness(_r(40, 19))["stale"]

    def test_too_few_recent_contracts_is_not_judgeable(self):
        """变红的变异：去掉最少样本数（冷门标的一两个零 OI 新合约就会被判陈旧）。"""
        out = oa.assess_oi_staleness(_r(19, 0))
        assert out["judgeable"] is False and out["stale"] is False and out["recent_contracts"] == 19

    def test_window_is_relative_to_the_chain_not_the_clock(self):
        """参照是本链最新一笔成交——5 天前的合约出窗口，3 天前的在窗口内。"""
        t0 = RECENT.timestamp()
        rows = [(t0, 0.0)] * 20 + [(t0 - 3 * 86400, 0.0)] * 5 + [(t0 - 5 * 86400, 500.0)] * 50
        out = oa.assess_oi_staleness(rows)
        assert out["recent_contracts"] == 25 and out["stale"]

    def test_no_timestamps(self):
        assert oa.assess_oi_staleness([(None, 5.0)] * 50)["judgeable"] is False
        assert oa.assess_oi_staleness([])["judgeable"] is False


class TestRows:
    def test_shapes(self):
        df = pd.DataFrame({"openInterest": [3.0, float("nan"), 7.0], "lastTradeDate": [RECENT, pd.NaT, 1.7e9]})
        rows = oa._oi_staleness_rows(df)
        assert rows == [(RECENT.timestamp(), 3.0), (None, 0.0), (1.7e9, 7.0)]

    def test_missing_columns_or_empty(self):
        assert oa._oi_staleness_rows(pd.DataFrame({"openInterest": [1]})) == []
        assert oa._oi_staleness_rows(pd.DataFrame()) == []
        assert oa._oi_staleness_rows(None) == []


# ═══════════════════════════════ 取链 ═══════════════════════════════

class TestFetch:
    def test_fixture_reproduces_the_298(self, monkeypatch):
        """夹具自检：不加检查时，这条链真的算出 298.46（否则下面「记 None」绿得没意义）。"""
        ch = _fetcher(monkeypatch).fetch_options_chain("BILI")
        assert oa.OptionsAnalyzer().calculate_put_call_ratio(ch["calls"], ch["puts"]) == pytest.approx(298.46)

    def test_stale_chain_flagged_counted_and_warned(self, monkeypatch, caplog):
        oa.reset_snapshot_slot_stats()
        with caplog.at_level(logging.WARNING):
            ch = _fetcher(monkeypatch).fetch_options_chain("BILI")
        oc = ch["oi_check"]
        assert oc["stale"] and oc["judgeable"] and oc["recent_contracts"] == 96 and oc["recent_with_oi"] == 0
        assert oa.snapshot_slot_stats()["oi_stale_chains"] == 1
        assert any("OI 陈旧" in r.getMessage() for r in caplog.records)
        oa.reset_snapshot_slot_stats()

    def test_counted_once_per_ticker(self, monkeypatch):
        oa.reset_snapshot_slot_stats()
        f = _fetcher(monkeypatch)
        f.fetch_options_chain("BILI")
        f.fetch_options_chain("BILI")
        assert oa.snapshot_slot_stats()["oi_stale_chains"] == 1
        oa.reset_snapshot_slot_stats()

    def test_healthy_chain_not_flagged(self, monkeypatch):
        oa.reset_snapshot_slot_stats()
        ch = _fetcher(monkeypatch, stale=False).fetch_options_chain("BILI")
        assert ch["oi_check"]["stale"] is False and ch["oi_check"]["ratio"] == 1.0
        assert oa.snapshot_slot_stats()["oi_stale_chains"] == 0

    def test_judged_before_the_strike_window_and_top40_filters(self, monkeypatch):
        """判据要看过滤**之前**的链：这里近价合约全在 ±70% 窗口外（只有冻结旧数在窗口里）——过滤后再判就一个最近
        成交的合约都看不到 ⇒ 判不了 ⇒ 照旧算出离谱的 P/C。变红的变异：把收集挪到窗口过滤之后。"""
        frames = []
        for c, p in _chain_frames(stale=True):
            for df in (c, p):
                df.loc[df["lastTradeDate"] == RECENT, "strike"] += 100.0     # 推到窗口外
            frames.append((c, p))
        ch = _fetcher(monkeypatch, frames=frames).fetch_options_chain("BILI")
        assert ch["oi_check"]["stale"] is True


# ═══════════════════════════════ analyze 端到端 ═══════════════════════════════

def _analyze(iv_agent, monkeypatch, stale=True):  # noqa: F811
    chain = _fetcher(monkeypatch, stale=stale).fetch_options_chain("BILI")
    monkeypatch.setattr(iv_agent.fetcher, "fetch_options_chain", lambda t: chain)
    return iv_agent.analyze("BILI", stock_price=PRICE)


class TestAnalyze:
    def test_stale_oi_derived_values_are_unavailable(self, iv_agent, monkeypatch):  # noqa: F811
        """变红的变异：analyze 不读 `oi_check`（P/C 298.46 照出）；或只置 P/C、漏 total_oi / GEX（单边）。"""
        r = _analyze(iv_agent, monkeypatch)
        assert r["put_call_ratio"] is None
        assert r["total_oi"] is None and r["total_oi_raw"] is None
        assert r["gamma_exposure"] is None and r["gamma_squeeze_risk"] == "unknown"
        assert r["flow_direction"] == "unknown"
        assert r["oi_check"]["stale"] is True

    def test_stale_chain_is_not_labelled_real(self, iv_agent, monkeypatch):  # noqa: F811
        """IV 照用，但链只剩一半——标 real 会让 scan_coverage_gate 的「real 却 P/C 为空」红。"""
        r = _analyze(iv_agent, monkeypatch)
        assert r["iv_current"] == pytest.approx(50.0)
        assert r["data_quality"] == "degraded"

    def test_flow_score_is_the_neutral_midpoint(self, iv_agent, monkeypatch):  # noqa: F811
        """P/C 不可得 ⇒ flow 1.5（0–3 中点），不是 298 的 0 分。"""
        a = oa.OptionsAnalyzer()
        s_none, _ = a.generate_options_score(50.0, None, None, [])
        s_bear, _ = a.generate_options_score(50.0, 298.46, None, [])
        assert s_none - s_bear == pytest.approx(1.5)

    def test_healthy_chain_unchanged(self, iv_agent, monkeypatch):  # noqa: F811
        r = _analyze(iv_agent, monkeypatch, stale=False)
        assert isinstance(r["put_call_ratio"], float) and r["put_call_ratio"] < 2
        assert isinstance(r["total_oi"], (int, float)) and r["total_oi"] > 0
        assert r["data_quality"] == "real" and r["flow_direction"] != "unknown"
        assert r["oi_check"]["stale"] is False

    def test_chains_without_the_check_are_unchanged(self, iv_agent, monkeypatch):  # noqa: F811
        """CBOE 链 / 旧缓存没有 `oi_check` ⇒ 照旧（这里用陈旧链去掉键来证明：P/C 又算出 298）。"""
        chain = _fetcher(monkeypatch).fetch_options_chain("BILI")
        chain.pop("oi_check")
        monkeypatch.setattr(iv_agent.fetcher, "fetch_options_chain", lambda t: chain)
        r = iv_agent.analyze("BILI", stock_price=PRICE)
        assert r["put_call_ratio"] == pytest.approx(298.46) and r["oi_check"] is None


def test_scan_timing_summary_names_it_only_when_nonzero():
    base = {"writes": 30, "hits": 0, "session_mismatch": 0, "hits_before_close": 0}
    line = scan_timing.summary_line({"phases": {}, "counters": {"options_snapshot": {**base, "oi_stale_chains": 2}}})
    assert "OI陈旧链2只" in line
    line0 = scan_timing.summary_line({"phases": {}, "counters": {"options_snapshot": {**base, "oi_stale_chains": 0}}})
    assert "OI陈旧" not in line0
