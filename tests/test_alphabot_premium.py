"""Alpha Bot「溢价」页（v0.45.492）：`alphabot/premium.py` + `AlphaBotService.premium` + `/api/premium/{ticker}`。

全离线（合成链 `alphabot.synthetic` + 手搓日线 / 本地文件夹具），无 skip。每条「降级数据 ⇒ 显示不可得」的测试
都配一个健康对照，证明翻转的是被注入的那处缺陷，不是夹具本来就坏。
"""
from __future__ import annotations

import ast
import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import bars_integrity
import iv_history
import sell_strike_report as R
import vrp_signal
from alphabot import premium as PM
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService, BadRequest

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
AS_OF = "2026-10-09"                       # 周五
NOW = datetime(2026, 10, 9, 17, 0, tzinfo=ET)
TODAY = NOW.date()


def _deps():
    return AlphaBotService()._premium_deps()


def _trading_bars(end: str, n: int, *, start_px=100.0, drop=(), step=0.012):
    """真实交易日历上的 n 根日线（末根 = end），收盘价确定性锯齿。drop 里的日期不出现（造缺口）。"""
    days = bars_integrity.trading_days(date.fromisoformat(end) - timedelta(days=int(n * 1.6) + 10),
                                       date.fromisoformat(end))[-n:]
    out, px = [], start_px
    for i, d in enumerate(days):
        px *= math.exp(step if i % 3 else -1.7 * step)
        if d.isoformat() not in drop:
            out.append({"date": d.isoformat(), "close": round(px, 4)})
    return out


def _bars(rows, available=True):
    return {"available": available, "source": "test", "bars": rows}


def _demo_raw(t="NVDA"):
    return syn.demo_payload(t, AS_OF)


def _blocks(deps=None):
    return PM.unavailable_blocks(PM._why("demo_mode"), ticker="NVDA", as_of=AS_OF, deps=deps or _deps())


def _build(raw=None, bars=None, **kw):
    deps = kw.pop("deps", None) or _deps()
    blocks = _blocks(deps) | kw
    return PM.build(raw or _demo_raw(), bars=bars if bars is not None else _bars(_trading_bars("2026-10-08", 120)),
                    today=TODAY, deps=deps, **blocks)


def _svc(**kw):
    kw.setdefault("fetch_fn", lambda t, as_of=None: (syn.demo_payload(t, AS_OF), None))
    kw.setdefault("bars_fn", lambda t: _bars(_trading_bars("2026-10-08", 120)))
    kw.setdefault("now_fn", lambda: NOW)
    return AlphaBotService(**kw)


def _client(svc, port=8765):
    from starlette.testclient import TestClient
    from alphabot.server import create_app
    return TestClient(create_app(svc, port=port), base_url=f"http://127.0.0.1:{port}")


def _fingerprint(root: Path):
    return {str(p.relative_to(root)): (p.is_dir(), p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


# ─────────────────────────────── RV

class TestRealizedVol:
    def test_matched_n_clamps(self):
        assert [PM.matched_n(x) for x in (0, 3, 10, 25, 60, 61, 300)] == [10, 10, 10, 25, 60, 60, 60]

    def test_window_matches_repo_formula(self):
        rows, _ = PM.clean_bars(_trading_bars("2026-10-08", 80))
        got = PM.rv_window(rows, 30, missing=frozenset(), rv_fn=vrp_signal.realized_vol_from_closes)
        want = vrp_signal.realized_vol_from_closes([c for _d, c in rows[-31:]]) / 100.0
        assert got["reason"] is None and got["value"] == pytest.approx(want)
        assert got["start"] == rows[-31][0] and got["end"] == rows[-1][0]

    def test_gap_inside_window_is_unavailable_not_stretched(self):
        full = _trading_bars("2026-10-08", 80)
        hole = full[-10]["date"]
        deps = _deps()
        ok = PM.bars_context(_bars(full), today=TODAY, deps=deps)
        bad = PM.bars_context(_bars([b for b in full if b["date"] != hole]), today=TODAY, deps=deps)
        rv = lambda ctx, n: PM.rv_window(ctx[1], n, missing=ctx[2], rv_fn=deps.rv_from_closes)  # noqa: E731
        assert rv(ok, 20)["value"] is not None                                   # 健康对照
        assert rv(bad, 20)["value"] is None and rv(bad, 20)["reason"] == f"gap_in_window:{hole}"
        assert rv(bad, 5)["value"] is not None                                   # 缺口在更短窗口之外：照常

    def test_split_sized_jump_is_unavailable(self):
        rows, _ = PM.clean_bars(_trading_bars("2026-10-08", 40))
        split = [(d, c / 2.0 if i >= len(rows) - 5 else c) for i, (d, c) in enumerate(rows)]
        f = vrp_signal.realized_vol_from_closes
        assert PM.rv_window(rows, 20, missing=frozenset(), rv_fn=f)["value"] is not None
        got = PM.rv_window(split, 20, missing=frozenset(), rv_fn=f)
        assert got["value"] is None and got["reason"].startswith("suspect_jump:")

    def test_too_few_bars(self):
        rows, _ = PM.clean_bars(_trading_bars("2026-10-08", 12))
        got = PM.rv_window(rows, 20, missing=frozenset(), rv_fn=vrp_signal.realized_vol_from_closes)
        assert (got["value"], got["reason"]) == (None, "insufficient_bars:12/21")

    def test_stale_bars_make_everything_unavailable(self):
        deps = _deps()
        fresh = PM.bars_context(_bars(_trading_bars("2026-10-08", 60)), today=TODAY, deps=deps)[0]
        stale = PM.bars_context(_bars(_trading_bars("2026-10-01", 60)), today=TODAY, deps=deps)[0]
        assert fresh["available"] and fresh["lag_sessions"] == 0
        assert not stale["available"] and stale["reason"].startswith("bars_stale:")

    def test_bars_unavailable_never_becomes_zero(self):
        full = _build(bars=_bars([], available=False) | {"reason": "twelve_data_not_configured"})
        assert full["data_available"] and full["rows"]
        assert all(r["rv"] is None and r["iv_over_rv"] is None and r["iv_minus_rv_pts"] is None for r in full["rows"])
        assert full["headline"]["rv30"]["value"] is None
        assert full["headline"]["rv30"]["reason"] == "twelve_data_not_configured"
        assert all(e["rv"] is None and e["atm_iv_over_rv"] is None for e in full["expiries"])
        json.dumps(full, allow_nan=False)

    def test_rv_window_follows_each_expirys_trading_days(self):
        # 2026-11-20 → 11-27：11-26 感恩节休市 ⇒ 4 个交易日 ⇒ 夹到 10
        raw = {"as_of": "2026-11-20", "underlying_price": 100.0, "contracts": [
            {"expiry": e, "dte": dte, "cp": "P", "strike": 95.0, "iv": 0.3, "delta": -0.2, "bid": 1.0, "ask": 1.05,
             "oi": 500} for e, dte in (("2026-11-27", 7), ("2027-01-15", 56))]}
        full = PM.build(raw, bars=_bars(_trading_bars("2026-11-19", 120)), today=date(2026, 11, 20), deps=_deps(),
                        **_blocks())
        ex = {e["expiry"]: e for e in full["expiries"]}
        assert ex["2026-11-27"]["n_td"] == 4 and ex["2026-11-27"]["rv_n"] == 10
        assert ex["2027-01-15"]["n_td"] == len(bars_integrity.trading_days(date(2026, 11, 21), date(2027, 1, 15)))
        assert ex["2027-01-15"]["rv_n"] == ex["2027-01-15"]["n_td"]
        assert ex["2026-11-27"]["rv"] != ex["2027-01-15"]["rv"]   # 不是一个固定的 RV30


# ─────────────────────────────── 事件扭结

def _atm_term(dtes, base=0.30, events=()):
    """平值 IV 期限结构；events = {段序号: 一次性波动}，加进该段及之后的总方差。"""
    out = []
    for i, dte in enumerate(dtes):
        T = dte / 365.0
        V = base * base * T + sum(m * m for j, m in events if i >= j)
        out.append({"expiry": (date.fromisoformat(AS_OF) + timedelta(days=dte)).isoformat(), "dte": dte,
                    "atm_iv": math.sqrt(V / T)})
    return out


class TestEventKink:
    def test_flat_term_structure_flags_nothing(self):
        got = PM.forward_kinks(_atm_term([7, 14, 21, 28, 35]), as_of=AS_OF, deps=_deps())
        assert all(r["status"] == "evaluated" and r["suspect"] is False and r["event_inside"] is False for r in got.values())

    def test_weekly_event_is_flagged_and_propagates(self):
        term = _atm_term([7, 14, 21, 28, 35], events=[(2, 0.08)])
        got = PM.forward_kinks(term, as_of=AS_OF, deps=_deps())
        flags = [got[e["expiry"]]["suspect"] for e in term]
        inside = [got[e["expiry"]]["event_inside"] for e in term]
        assert flags == [False, False, True, False, False]
        assert inside == [False, False, True, True, True]
        assert got[term[2]["expiry"]]["implied_event_move"] == pytest.approx(0.08, abs=0.005)
        assert [got[e["expiry"]]["event_move"] for e in term[:2]] == [None, None]
        assert all(got[e["expiry"]]["event_move"] == pytest.approx(0.08, abs=0.005) for e in term[2:])

    def test_small_monthly_event_is_the_documented_blind_spot(self):
        term = _atm_term([30, 60, 90, 120], events=[(1, 0.03)])
        got = PM.forward_kinks(term, as_of=AS_OF, deps=_deps())
        assert not any(r["suspect"] for r in got.values())
        big = PM.forward_kinks(_atm_term([30, 60, 90, 120], events=[(1, 0.10)]), as_of=AS_OF, deps=_deps())
        assert [r["suspect"] for r in big.values()] == [False, True, False, False]   # 对照：大事件照样判得出

    def test_inverted_term_does_not_crash(self):
        term = [{"expiry": (date.fromisoformat(AS_OF) + timedelta(days=d)).isoformat(), "dte": d, "atm_iv": iv}
                for d, iv in ((7, 0.9), (14, 0.4), (21, 0.35))]
        got = PM.forward_kinks(term, as_of=AS_OF, deps=_deps())
        assert got[term[1]["expiry"]]["reason"] == "inverted_term_structure"
        assert got[term[1]["expiry"]]["suspect"] is None                    # 数据问题 = 判不了，不是「判过、干净」

    def test_too_few_expiries_is_unknown_not_false(self):
        got = PM.forward_kinks(_atm_term([7]) + [{"expiry": "2026-10-11", "dte": 2, "atm_iv": 0.3}],
                               as_of=AS_OF, deps=_deps())
        assert all(r["status"] == "insufficient_expiries" and r["event_inside"] is None for r in got.values())

    def test_skipped_expiry_inside_suspect_segment_is_unknown(self):
        term = _atm_term([7, 14, 16, 21, 28], events=[(3, 0.08)])
        got = PM.forward_kinks(term, as_of=AS_OF, deps=_deps())
        assert got[term[2]["expiry"]]["status"] == "not_evaluated"          # 离 14 天不到 min_gap
        assert got[term[3]["expiry"]]["suspect"] is True
        assert got[term[2]["expiry"]]["event_inside"] is None               # 事件在 (14, 21] 里，16 判不了


# ─────────────────────────────── 偏度

def _pts(S, ks, ivf):
    return sorted((k, math.log(k / S), ivf(k)) for k in ks)


class TestSkew:
    def test_linear_smile_gives_zero_and_bump_is_measured(self):
        S = 100.0
        ivf = lambda k: 0.30 - 0.5 * math.log(k / S)   # noqa: E731
        ks = [96, 97, 98, 99, 100, 101, 102]
        assert PM.smile_reference(_pts(S, ks, ivf), 99, S)["iv_ref"] == pytest.approx(ivf(99))
        bumped = [(k, kk, iv + (0.03 if k == 99 else 0)) for k, kk, iv in _pts(S, ks, ivf)]
        ref = PM.smile_reference(bumped, 99, S)
        assert (ivf(99) + 0.03 - ref["iv_ref"]) * 100 == pytest.approx(3.0)   # 自身被排除在参照之外
        assert (ref["lo"], ref["hi"]) == (98, 100)

    def test_edges_and_far_neighbours(self):
        S = 100.0
        pts = _pts(S, [90, 99, 100, 101], lambda k: 0.3)
        assert PM.smile_reference(pts, 90, S)["reason"] == "no_bracketing_neighbors"
        assert PM.smile_reference(pts, 99, S)["reason"] == "neighbors_too_far"
        assert PM.smile_reference(pts, 100, S)["iv_ref"] == pytest.approx(0.3)


# ─────────────────────────────── 与卖权页 / 期限页对拍（三个 tab 的数对得上）

class TestParityWithOtherTabs:
    def test_yield_matches_structure_quote(self):
        raw = _demo_raw()
        S = raw["underlying_price"]
        full = _build(raw)
        expiry = next(e["expiry"] for e in full["expiries"] if 25 <= e["dte"] <= 35)
        ladder = R.C.build_ladder(raw["contracts"], S, expiry)
        by = {(r["expiry"], r["side"], r["strike"]): r for r in full["rows"]}
        checked = 0
        for side in ("put", "call"):
            q = R.C.structure_quote(ladder, f"short_{side}", 0.20)
            K = ladder[side]["0.20"]["short"]["strike"]
            row = by.get((expiry, side, K))
            if q["quotable"] and row is not None:
                assert row["yield_raw"] == pytest.approx(q["yield_raw"], abs=2e-6)
                assert row["yield_ann"] == pytest.approx(q["yield_annualized"], abs=2e-5)
                assert row["collateral"] == pytest.approx(q["collateral"])
                checked += 1
        assert checked == 2

    def test_delta_and_spread_are_the_sell_tabs(self):
        raw = _demo_raw()
        S = raw["underlying_price"]
        full = _build(raw)
        by_sym = {c["symbol"]: c for c in raw["contracts"]}
        for r in full["rows"][:50]:
            leg = R.C._leg(by_sym[r["symbol"]], S, R.L.RISK_FREE_RATE)
            assert (r["delta"], r["spread_pct"], r["sigma_distance"]) == (leg["delta"], leg["spread_pct"], leg["sigma_distance"])

    def test_atm_iv_is_the_term_tabs(self):
        svc = _svc(demo=True)
        live = {e["expiry"]: e["atm_iv"] for e in svc.live("NVDA")["expiries"]}
        prem = svc.premium("NVDA")
        assert prem["expiries"]
        for e in prem["expiries"]:
            assert e["atm_iv"] == pytest.approx(live[e["expiry"]])

    def test_spread_gate_boundary_is_the_sell_tabs(self):
        mk = lambda bid, ask: {"symbol": f"X{ask}", "expiry": "2026-11-06", "dte": 28, "cp": "P", "strike": 95.0,  # noqa: E731
                               "iv": 0.3, "delta": -0.2, "bid": bid, "ask": ask, "oi": 500}
        raw = {"as_of": AS_OF, "underlying_price": 100.0, "contracts": [mk(0.35, 0.45), mk(0.35, 0.4501), mk(0.0, 0.05)]}
        full = _build(raw)
        assert [r["ask"] for r in full["rows"]] == [0.45]                      # 恰好 25% 通过
        n = {s["stage"]: s["n"] for s in full["funnel_fixed"]}
        assert (n["priced"], n["quote_gate"], n["spread_gate"]) == (3, 2, 1)
        assert R.C.MAX_SPREAD_PCT == 0.25

    @pytest.mark.parametrize("earn", ["2026-10-09", "2026-10-20", "2026-11-06", "2026-11-07", "2027-03-01"])
    def test_earnings_flag_matches_classify_earnings(self, earn):
        deps = _deps()
        e = PM.earnings_state({"scan_date": AS_OF, "earnings_date": earn, "earnings_status": "after_expiry",
                               "source": "chronos_bee_catalyst"}, None, as_of=AS_OF, deps=deps)
        full = _build(earnings=e)
        for x in full["expiries"]:
            want = R.C.classify_earnings({"earnings_date": earn}, AS_OF, x["expiry"])
            assert x["earnings_before_expiry"] is (want == "before_expiry")
        assert deps.classify_earnings is R.C.classify_earnings


# ─────────────────────────────── 三份本地数据的判定

class TestEarningsState:
    ok = {"scan_date": "2026-10-08", "earnings_date": "2026-10-28", "earnings_status": "after_expiry",
          "source": "chronos_bee_catalyst"}

    def _s(self, entry, err=None):
        return PM.earnings_state(entry, err, as_of=AS_OF, deps=_deps())

    def test_known(self):
        s = self._s(self.ok)
        assert (s["status"], s["date"], s["scan_date"], s["lag_sessions"], s["source"]) == \
            ("known", "2026-10-28", "2026-10-08", 1, "chronos_bee_catalyst")

    @pytest.mark.parametrize("patch,status,reason", [
        ({"scan_date": "2026-09-25"}, "unknown", "scan_too_old:10"),
        ({"earnings_status": "unknown", "earnings_date": None}, "unknown", "scan_had_no_earnings_info"),
        ({"earnings_status": None}, "unknown", "scan_had_no_earnings_info"),
        ({"earnings_date": "[]"}, "unknown", "unparseable_date:'[]'"),
        ({"earnings_date": None, "earnings_status": "none"}, "none", None),       # 扫描视野内确实没有
        ({"earnings_date": "2026-10-01", "earnings_status": "none"}, "none", None),  # 记下的那次已过
    ])
    def test_other_states_are_named(self, patch, status, reason):
        assert self._s(self.ok)["status"] == "known"                        # 对照
        s = self._s({**self.ok, **patch})
        assert (s["status"], s["reason"]) == (status, reason)

    def test_missing_and_unreadable(self):
        assert self._s(None)["reason"] == "not_in_recent_scans"
        assert self._s(None, "scan_ledger_unreadable:OSError")["reason"] == "scan_ledger_unreadable:OSError"

    @pytest.mark.parametrize("patch", [{"earnings_date": None, "earnings_status": "none"},          # 视野内没有
                                       {"earnings_date": "2026-10-01", "earnings_status": "none"}])  # 记下的那次已过
    def test_no_next_date_means_unknown_for_every_expiry(self, patch):
        """扫描只知道**下一次**财报日。下一次不知道时，到期日一律判不了（None），**不是**「到期前没有」（False）：
        本页 DTE 可到 365 天，120 天的到期日必然含下一次财报（v0.45.492 二次检查 #1）。"""
        wide = {"dte_min": 0, "dte_max": 365, "delta_min": 0.0, "delta_max": 1.0, "oi_min": 0, "side": "both",
                "sort": "iv_over_rv", "expiry": None}
        full = _build(earnings=self._s({**self.ok, **patch}))
        assert self._s({**self.ok, **patch})["status"] == "none"
        assert {e["earnings_before_expiry"] for e in PM.select(full, wide)["expiries"]} == {None}
        assert all(r["flags"]["earnings_before_expiry"] is None for r in full["rows"])

    def test_known_next_date_splits_expiries(self):
        """对照：知道下一次在 d ⇒ d 之前到期 False、之后 True（长期限同样 True）。"""
        full = _build(earnings=self._s({**self.ok, "earnings_date": "2026-10-28"}))
        for e in full["expiries"]:
            assert e["earnings_before_expiry"] is (e["expiry"] >= "2026-10-28"), e["expiry"]


class TestIvRank:
    def _index(self, n, end=AS_OF, flat=False):
        days = bars_integrity.trading_days(date.fromisoformat(end) - timedelta(days=200), date.fromisoformat(end))[-n:]
        return {d.isoformat(): (30.0 if flat else 30.0 + (i * 7 % 11)) for i, d in enumerate(days)}

    def test_insufficient_then_ready(self):
        deps = _deps()
        lo = PM.iv_rank_block(self._index(62), None, as_of=AS_OF, deps=deps)
        assert (lo["state"], lo["n"], lo["min_days"], lo["rank"]) == ("insufficient", 62, 63, None)
        idx = self._index(63)
        hi = PM.iv_rank_block(idx, None, as_of=AS_OF, deps=deps)
        vals = [idx[d] for d in sorted(idx)]
        assert hi["state"] == "ready" and (hi["rank"], hi["percentile"]) == iv_history.iv_rank_from_history(vals[-1], vals)
        assert hi["current"] == pytest.approx(vals[-1] / 100)                 # 拿历史自己的最新一条，小数

    def test_stale_flat_missing(self):
        deps = _deps()
        st = PM.iv_rank_block(self._index(70, end="2026-09-25"), None, as_of=AS_OF, deps=deps)
        assert st["state"] == "stale" and st["rank"] is None and st["lag_sessions"] == 10
        assert PM.iv_rank_block(self._index(70, flat=True), None, as_of=AS_OF, deps=deps)["state"] == "degenerate"
        assert PM.iv_rank_block(None, "no_index_file", as_of=AS_OF, deps=deps)["reason"] == "no_index_file"


class TestVrpBlock:
    def _b(self, rows):
        return PM.vrp_block(rows, None, ticker="NVDA", as_of=AS_OF, deps=_deps())

    def test_states(self):
        base = {"ticker": "NVDA", "date": "2026-10-08", "iv": 40.0, "rv_30d": 30.0, "vrp_ex_ante": 10.0, "n_obs": 40,
                "ready": False, "ts_pct": None, "label": None, "reason": None}
        assert self._b([base])["state"] == "insufficient"
        r = self._b([base, {**base, "date": "2026-10-09", "ready": True, "ts_pct": 0.9, "label": "rich", "n_obs": 70}])
        assert (r["state"], r["date"], r["ts_pct"], r["label"], r["lag_sessions"]) == ("ready", "2026-10-09", 0.9, "rich", 0)
        assert self._b([{**base, "reason": "rv_30d_missing"}])["state"] == "missing_inputs"
        assert self._b([{**base, "ticker": "TSLA"}])["reason"] == "no_rows_for_ticker"


class TestParams:
    def test_defaults(self):
        assert PM.parse_params({}) == PM.DEFAULTS | {"expiry": None}

    @pytest.mark.parametrize("q", [{"bogus": "1"}, {"dte_min": "x"}, {"dte_min": "-1"}, {"dte_max": "999"},
                                   {"dte_min": "30", "dte_max": "10"}, {"delta_max": "1.5"}, {"delta_min": "nan"},
                                   {"delta_min": "0.4", "delta_max": "0.2"}, {"oi_min": "-5"}, {"side": "both2"}])
    def test_rejects(self, q):
        with pytest.raises(PM.ParamError):
            PM.parse_params(q)


# ─────────────────────────────── 服务层与接口

class TestService:
    def test_defaults_are_honoured(self):
        r = _svc().premium("NVDA")
        assert r["data_available"] and r["rows"] and not r["truncated"]
        assert r["params"] == PM.DEFAULTS | {"expiry": None}
        for x in r["rows"]:
            assert 7 <= x["dte"] <= 60 and 0.10 <= x["abs_delta"] <= 0.35 and x["oi"] >= 100
            assert x["spread_pct"] <= R.C.MAX_SPREAD_PCT + 1e-9 and x["bid"] > 0
        ivrv = [x["iv_over_rv"] for x in r["rows"]]
        assert ivrv == sorted(ivrv, reverse=True)
        assert [s["n"] for s in r["funnel"]] == sorted((s["n"] for s in r["funnel"]), reverse=True)
        json.dumps(r, allow_nan=False)

    def test_filters_change_result_monotonically(self):
        svc = _svc()
        both = svc.premium("NVDA")["rows_total"]
        puts = svc.premium("NVDA", {"side": "put"})
        assert puts["rows"] and all(x["side"] == "put" for x in puts["rows"]) and puts["rows_total"] < both
        assert svc.premium("NVDA", {"oi_min": "5000"})["rows_total"] < both
        empty = svc.premium("NVDA", {"dte_min": "300", "dte_max": "365"})
        assert empty["rows"] == [] and empty["empty_reason"] == "dte"
        assert svc.premium("NVDA", {"oi_min": "10000000"})["empty_reason"] == "oi"

    def test_cache_and_force(self):
        calls = []
        now = [0.0]

        def fetch(t, as_of=None):
            calls.append(t)
            return syn.demo_payload(t, AS_OF), None
        svc = _svc(fetch_fn=fetch, clock=lambda: now[0])
        svc.premium("NVDA")
        svc.premium("NVDA", {"side": "call"})                 # 换参数 = 同一份缓存
        assert len(calls) == 1
        now[0] = 61.0
        svc.premium("NVDA")
        assert len(calls) == 2
        svc.premium("NVDA", {"force": "1"})
        assert len(calls) == 3

    def test_fetch_failure_is_reported_and_briefly_cached(self):
        calls, now = [], [0.0]

        def fetch(t, as_of=None):
            calls.append(t)
            return None, "stale_vintage"
        svc = _svc(fetch_fn=fetch, clock=lambda: now[0])
        r = svc.premium("NVDA")
        assert (r["data_available"], r["reason"]) == (False, "stale_vintage")
        svc.premium("NVDA")
        assert len(calls) == 1
        now[0] = 16.0
        svc.premium("NVDA")
        assert len(calls) == 2

    def test_does_not_invalidate_the_cboe_process_cache(self, monkeypatch):
        hit = []
        monkeypatch.setattr(R, "_invalidate_cached_payload", lambda t: hit.append(t))
        _svc().premium("NVDA", {"force": "1"})
        assert hit == []

    def test_http(self):
        c = _client(_svc())
        assert c.get("/api/premium/NVDA").status_code == 200
        for bad in ("/api/premium/bad$", "/api/premium/NVDA?side=x", "/api/premium/NVDA?dte_min=9&dte_max=3",
                    "/api/premium/NVDA?bogus=1"):
            assert c.get(bad).status_code == 400, bad
        with pytest.raises(BadRequest):
            _svc().premium("NVDA", {"oi_min": "-1"})

    def test_demo_reads_no_local_data(self):
        r = _svc(demo=True).premium("NVDA")
        assert r["demo"] and r["earnings"]["status"] == "unknown"
        assert r["history"]["iv_rank"]["state"] == r["history"]["vrp"]["state"] == "unavailable"


def _ledger(root: Path, spec):
    """合成卖权账本。spec = [(date, ticker, status, earnings_info)]；recorded 行用真的 build_tenor_row 造
    （日报钩子同一个函数，earnings_* 三列由它按 earnings_info 填）；其余行照扫描失败那天的样子只留 status。"""
    from collections import defaultdict
    import sell_strike_levels as L
    LG = R.LG
    shards = defaultdict(list)
    for d, t, status, info in spec:
        raw = syn.demo_payload(t, d)
        lm = L.level_map(raw["contracts"], raw["underlying_price"])
        for tenor in LG.TENORS:
            row = LG.build_tenor_row(raw, lm, tenor, as_of=d, earnings_info=info, ticker=t)
            if status != "recorded":
                row.update({"status": status, "earnings_date": None, "earnings_status": None, "earnings_source": None})
            shards[LG._shard(tenor, d, root)].append(row)
    for path, rows in shards.items():
        LG._write_shard(path, rows)
    return root


def _populate(root: Path, ticker="NVDA", n_iv=63, vrp_text=None):
    _ledger(root / "ledger", [(AS_OF, ticker, "recorded", {"earnings_date": "2026-11-17", "source": "chronos_bee_catalyst"})])
    (root / "cache").mkdir()
    days = bars_integrity.trading_days(date(2026, 4, 1), date.fromisoformat(AS_OF))[-n_iv:]
    (root / "cache" / f"iv_history_{ticker}.jsonl").write_text(
        "".join(json.dumps({"date": d.isoformat(), "iv": 30.0 + i % 9}) + "\n" for i, d in enumerate(days)), encoding="utf-8")
    rel = vrp_signal.SIGNALS_FILE.relative_to(vrp_signal.BASE_DIR)
    (root / rel).parent.mkdir(parents=True)
    (root / rel).write_text(vrp_text if vrp_text is not None else json.dumps(
        {"ticker": ticker, "date": "2026-10-08", "iv": 40.0, "rv_30d": 30.0, "vrp_ex_ante": 10.0, "n_obs": 70,
         "ready": True, "ts_pct": 0.82, "label": "rich", "reason": None}) + "\n", encoding="utf-8")


class TestLocalDataIsReadOnly:
    def test_reads_all_three_and_writes_nothing(self, tmp_path):
        _populate(tmp_path)
        before = _fingerprint(tmp_path)
        r = _svc(premium_root=tmp_path, ledger_dir=tmp_path / "ledger").premium("NVDA")
        assert r["earnings"]["status"] == "known" and r["earnings"]["date"] == "2026-11-17"
        assert r["history"]["iv_rank"]["state"] == "ready" and r["history"]["iv_rank"]["n"] == 63
        assert r["history"]["vrp"]["state"] == "ready" and r["history"]["vrp"]["label"] == "rich"
        assert any(e["earnings_before_expiry"] for e in r["expiries"])
        by_exp = {e["expiry"]: e for e in r["expiries"]}
        assert any(x["flags"]["earnings_before_expiry"] for x in r["rows"])      # 逐合约的标记跟着到期日走
        for x in r["rows"]:
            assert x["flags"]["earnings_before_expiry"] is by_exp[x["expiry"]]["earnings_before_expiry"]
            assert x["flags"]["event_inside"] is by_exp[x["expiry"]]["event_inside"]
        assert _fingerprint(tmp_path) == before
        assert not list(tmp_path.rglob(".iv_index_migrated_*"))          # load_iv_history 会写的那个标记

    def test_empty_root_creates_nothing(self, tmp_path):
        root = tmp_path / "data_root"            # tmp_path 本身已有 conftest 的沙箱条目
        root.mkdir()
        r = _svc(premium_root=root, ledger_dir=root / "ledger").premium("NVDA")
        assert list(root.iterdir()) == []
        assert r["earnings"]["reason"] == "not_in_recent_scans"
        assert r["history"]["iv_rank"]["reason"] == "no_index_file"
        assert r["history"]["vrp"]["reason"] == "no_rows_for_ticker"

    def test_canary_paths_cache_dir_does_mkdir(self, tmp_path, monkeypatch):
        """反向自证：PATHS.cache_dir 一读就建目录——所以服务层另写了不 mkdir 的解析。"""
        from hive_logger import PATHS
        target = tmp_path / "would_be_created"
        monkeypatch.setenv("ALPHA_HIVE_CACHE_DIR", str(target))
        assert AlphaBotService()._premium_cache_dir() == target and not target.exists()
        assert PATHS.cache_dir == target and target.exists()

    def test_cache_dir_rule_matches_paths(self, monkeypatch):
        from hive_logger import PATHS
        monkeypatch.delenv("ALPHA_HIVE_CACHE_DIR", raising=False)
        assert AlphaBotService()._premium_cache_dir() == PATHS.cache_dir

    def test_corrupt_vrp_ledger_is_reported(self, tmp_path):
        _populate(tmp_path, vrp_text="{not json\n")
        r = _svc(premium_root=tmp_path).premium("NVDA")
        assert r["history"]["vrp"]["state"] == "unavailable" and "LedgerCorrupt" in r["history"]["vrp"]["reason"]
        assert r["history"]["iv_rank"]["state"] == "ready"                  # 其余照常

    def test_brk_dot_b_reads_scan_side_names(self, tmp_path):
        _populate(tmp_path, ticker="BRK-B")
        rel = vrp_signal.SIGNALS_FILE.relative_to(vrp_signal.BASE_DIR)
        (tmp_path / rel).write_text((tmp_path / rel).read_text(encoding="utf-8").replace('"NVDA"', '"BRK-B"'), encoding="utf-8")
        r = _svc(premium_root=tmp_path, ledger_dir=tmp_path / "ledger").premium("BRK.B")
        assert r["earnings"]["status"] == "known" and r["history"]["iv_rank"]["n"] == 63


# ─────────────────────────────── 纯度与火墙

PREMIUM = ROOT / "alphabot" / "premium.py"
ALLOWED_IMPORTS = {"__future__", "bisect", "math", "dataclasses", "datetime", "typing"}
FORBIDDEN_NAMES = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "Path", "os", "time", "random",
                   "mkdir", "makedirs", "unlink", "glob", "iterdir", "__import__", "import_module", "urlopen", "requests"}
#: 读时钟只认属性形状（`date.today()` / `datetime.now()`）——`today` 本身是注入进来的参数名
CLOCK_ATTRS = {"now", "today", "utcnow"}


def _purity_violations(src: str):
    tree = ast.parse(src)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names if a.name.split(".")[0] not in ALLOWED_IMPORTS]
        elif isinstance(n, ast.ImportFrom):
            if (n.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                out.append(n.module)
        elif isinstance(n, ast.Name) and n.id in FORBIDDEN_NAMES:
            out.append(n.id)
        elif isinstance(n, ast.Attribute) and n.attr in FORBIDDEN_NAMES | CLOCK_ATTRS:
            out.append(n.attr)
    return out


class TestPurity:
    def test_premium_module_is_pure(self):
        src = PREMIUM.read_text(encoding="utf-8")
        assert _purity_violations(src) == []

    @pytest.mark.parametrize("inject", ["import os\n", "from pathlib import Path\n", "import sell_strike_levels\n",
                                        "x = open('f', 'w')\n", "y = date.today()\n", "z = datetime.now()\n"])
    def test_has_teeth(self, inject):
        assert _purity_violations(PREMIUM.read_text(encoding="utf-8") + "\n" + inject)


# ─────────────────────────────── 接线：tab、帮助页、常量出处

class TestWiring:
    def test_help_constants_come_from_api_method(self):
        m = _client(_svc()).get("/api/method").json()
        rules = m["premium"]
        assert "rules_error" not in rules
        help_js = (ROOT / "alphabot/static/pages/help.js").read_text(encoding="utf-8")
        sec = help_js.split('  ["premium", "', 1)[1].split('\n  ["', 1)[0]
        used = set(re.findall(r"\bp\.(\w+)", sec)) - {"rules_error"}     # 取不到常量时的提示位，不是常量
        assert used and used <= set(rules), used - set(rules)
        assert set(re.findall(r"\bd\.(\w+)", sec)) <= set(rules["defaults"])

    def test_tab_and_help_registered(self):
        tj = (ROOT / "alphabot/static/pages/ticker.js").read_text(encoding="utf-8")
        assert '["premium", "溢价"]' in tj and 'premium: "premium"' in tj and 'from "./premium.js"' in tj
        pj = (ROOT / "alphabot/static/pages/premium.js").read_text(encoding="utf-8")
        assert "/api/premium/" in pj and "innerHTML" not in pj

    def test_response_echoes_the_same_rules(self):
        svc = _svc()
        assert svc.premium("NVDA")["rules"] == svc.meta_texts()["premium"]


# ─────────────────────────────── 修复 1：参数被拒时带上是哪个参数（前端只撤销那一个）

class TestRejectedParamIsNamed:
    @pytest.mark.parametrize("q,param", [
        ("dte_min=30&dte_max=20", "dte_min"), ("dte_max=3", "dte_max"), ("delta_min=0.5", "delta_min"),
        ("side=x", "side"), ("sort=bogus", "sort"), ("oi_min=-1", "oi_min"), ("bogus=1", "bogus"),
        ("expiry=2026-13-01", "expiry"), ("expiry=1999-01-01", "expiry"),
    ])
    def test_400_names_the_param(self, q, param):
        r = _client(_svc()).get(f"/api/premium/NVDA?{q}")
        assert r.status_code == 400 and r.json()["param"] == param and r.json()["error"]

    def test_other_400s_carry_no_param(self):
        r = _client(_svc()).get("/api/premium/bad$")
        assert r.status_code == 400 and "param" not in r.json()


# ─────────────────────────────── 修复 2：先筛、再排、最后截断（排序 / 到期日不再在截断之后的前端做）

WIDE = {"dte_min": "0", "dte_max": "365", "delta_min": "0", "delta_max": "1", "oi_min": "0"}


class TestSortAndExpiryBeforeTruncation:
    def test_chain_is_big_enough_to_truncate(self):
        assert _svc().premium("NVDA", WIDE)["truncated"]                    # 对照：确实截断了，下面才有意义

    @pytest.mark.parametrize("sort", PM.SORTS)
    def test_top_rows_are_the_global_top(self, sort):
        svc = _svc()
        r = svc.premium("NVDA", WIDE | {"sort": sort})
        full = svc._premium_full("NVDA", False)
        allv = sorted((x[sort] for x in full["rows"] if x[sort] is not None), reverse=True)
        got = [x[sort] for x in r["rows"] if x[sort] is not None]
        assert got == allv[:len(got)] and len(r["rows"]) == PM.MAX_ROWS

    def test_picked_expiry_is_never_empty_when_its_count_is_not(self):
        svc = _svc()
        r = svc.premium("NVDA", WIDE)
        shown: dict = {}
        for x in r["rows"]:
            shown[x["expiry"]] = shown.get(x["expiry"], 0) + 1
        cut = [e for e in r["expiries"] if e["n_rows"] > shown.get(e["expiry"], 0)]
        assert cut                                                          # 对照：有到期日的行被截断掉了一部分
        e = cut[0]
        one = svc.premium("NVDA", WIDE | {"expiry": e["expiry"]})
        assert one["rows_total"] == e["n_rows"] and len(one["rows"]) == min(e["n_rows"], PM.MAX_ROWS)
        assert len(one["rows"]) > shown.get(e["expiry"], 0)                # 截断前筛 ⇒ 被截掉的那些回来了
        assert {x["expiry"] for x in one["rows"]} == {e["expiry"]}
        assert one["funnel"][-1] == {"stage": "expiry", "n": e["n_rows"]}
        exp_counts = {e["expiry"]: e["n_rows"] for e in one["expiries"]}
        assert exp_counts == {e["expiry"]: e["n_rows"] for e in r["expiries"]}   # 到期日表的计数不受「只看一个」影响


# ─────────────────────────────── 修复 3：生产那条取数路径（真解析 CBOE 原始格式，不出网）

def _occ(root, expiry, cp, K):
    return f"{root}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{cp}{int(round(K * 1000)):08d}"


def _cboe_payload(root="NVDA", S=100.0):
    """CBOE 原始 JSON 的形状（同 tests/test_sell_strike_candidates.py 的夹具）：多个到期日、两侧、真实价差。"""
    rows = []
    for dte in (10, 17, 31, 59):
        exp = (date.fromisoformat(AS_OF) + timedelta(days=dte)).isoformat()
        for K in range(80, 121, 5):
            for cp in ("P", "C"):
                m = math.log(K / S)
                iv = 0.32 - 0.4 * m + 0.8 * m * m
                sign = -1 if cp == "P" else 1
                otm = (cp == "P" and K < S) or (cp == "C" and K > S)
                delta = sign * (0.5 - 0.45 * min(1.0, abs(m) * 6)) if otm else sign * 0.6
                mid = max(0.2, 3.0 * (1 - abs(m) * 4)) if otm else 6.0
                rows.append({"option": _occ(root, exp, cp, float(K)), "iv": iv, "delta": delta, "gamma": 0.02,
                             "vega": 0.1, "theta": -0.05, "theo": mid, "open_interest": 500, "volume": 20,
                             "bid": round(mid * 0.97, 2), "ask": round(mid * 1.03, 2),
                             "last_trade_time": f"{AS_OF}T15:59:00"})
    return {"symbol": root, "close": S, "current_price": S, "last_trade_time": f"{AS_OF}T16:00:00", "iv30": 31.0,
            "options": rows}


class TestProductionFetchPath:
    def _wire(self, monkeypatch):
        import cboe_options as C
        seen = {"payload": [], "invalidated": []}

        def fake_payload(ticker, timeout, *, retries=3, on_stale="none"):
            seen["payload"].append((ticker, on_stale))
            return _cboe_payload(ticker)
        monkeypatch.setattr(C, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(C, "_fetch_cboe_payload", fake_payload)
        monkeypatch.setattr(C, "_et_now", lambda: NOW)                    # 不读真钟
        monkeypatch.setattr(C, "invalidate_payload_cache", lambda t=None: seen["invalidated"].append(t))
        return seen

    def _prod_svc(self):
        return AlphaBotService(fetch_fn=None, bars_fn=lambda t: _bars(_trading_bars("2026-10-08", 120)),
                               now_fn=lambda: NOW, premium_root=Path("/nonexistent-premium-root"))

    def test_real_parser_feeds_premium(self, monkeypatch):
        seen = self._wire(monkeypatch)
        svc = self._prod_svc()
        assert svc._premium_fetch() is R.LG._default_fetch                  # 生产走的就是卖权页那一个
        r = svc.premium("NVDA")
        assert r["data_available"], r.get("reason")
        assert r["chain"]["as_of"] == AS_OF and r["rows"]
        assert seen["payload"] == [("NVDA", "raise")] and seen["invalidated"] == []
        assert all(x["iv_over_rv"] is not None and x["bid"] > 0 for x in r["rows"])

    def test_default_fetch_called_with_the_documented_signature(self, monkeypatch):
        calls = []

        def fake(t, *, as_of=None):
            calls.append((t, as_of))
            return syn.demo_payload(t, AS_OF), None
        monkeypatch.setattr(R.LG, "_default_fetch", fake)
        assert self._prod_svc().premium("NVDA")["data_available"]
        assert calls == [("NVDA", None)]


# ─────────────────────────────── 修复 4：到期日解析不了的合约不再让整页崩

class TestBadExpiryIsCountedNotFatal:
    def test_one_bad_row(self):
        raw = _demo_raw()
        good = _build(raw)
        bad_raw = dict(raw, contracts=raw["contracts"] + [dict(raw["contracts"][100], expiry="garbage", symbol="BAD")])
        bad = _build(bad_raw)
        assert bad["data_available"] and len(bad["rows"]) == len(good["rows"])
        ng = {s["stage"]: s["n"] for s in good["funnel_fixed"]}
        nb = {s["stage"]: s["n"] for s in bad["funnel_fixed"]}
        assert nb["chain"] - ng["chain"] == 1 and nb["valid_expiry"] == ng["valid_expiry"]


# ─────────────────────────────── 修复 5：「判不了」与「疑似」同样向后传播

class TestUnjudgedPropagates:
    def test_inverted_segment_makes_later_expiries_unknown(self):
        dtes = [7, 14, 21, 28, 35]
        T = [d / 365.0 for d in dtes]
        base = 0.30 ** 2
        V = [base * T[0], base * T[1], base * T[1] * 0.9]                 # 第 3 段总方差下降 ⇒ 倒挂
        V += [V[2] + base * (T[3] - T[2])]
        V += [V[3] + base * (T[4] - T[3])]                                # 其后各段正常衔接（不造出假鼓包）
        term = [{"expiry": (date.fromisoformat(AS_OF) + timedelta(days=d)).isoformat(), "dte": d,
                 "atm_iv": math.sqrt(v / t)} for d, v, t in zip(dtes, V, T)]
        got = PM.forward_kinks(term, as_of=AS_OF, deps=_deps())
        inside = [got[e["expiry"]]["event_inside"] for e in term]
        assert got[term[2]["expiry"]]["reason"] == "inverted_term_structure"
        assert inside[:2] == [False, False] and inside[2:] == [None, None, None]
        flat = PM.forward_kinks(_atm_term(dtes), as_of=AS_OF, deps=_deps())     # 对照：去掉倒挂就全部判得了
        assert [flat[e["expiry"]]["event_inside"] for e in term] == [False] * 5

    def test_detection_limit_is_the_rule(self):
        """帮助页的「检出下限」就是判据本身：比下限大一点判得出，小一点判不出。"""
        deps = _deps()
        lim = PM.kink_detect_limit(0.30, 7, 14, deps)
        for move, want in ((lim * 1.05, True), (lim * 0.95, False)):
            term = _atm_term([7, 14, 21, 28], events=[(1, move)])
            got = PM.forward_kinks(term, as_of=AS_OF, deps=deps)
            assert got[term[1]["expiry"]]["suspect"] is want, (move, lim)
        rules = PM.public_rules(deps)
        assert rules["kink_limit_weekly_pct"] == pytest.approx(lim * 100)
        assert rules["kink_limit_weekly_pct"] < rules["kink_limit_monthly_pct"]


# ─────────────────────────────── 修复 6：原因码只有一张表，双向对账

SERVICE = ROOT / "alphabot" / "service.py"


def _why_literals(path: Path):
    out = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) == "_why":
            assert n.args and isinstance(n.args[0], ast.Constant), f"{path.name}:{n.lineno} _why 的码须是字面量"
            out.add(n.args[0].value)
    return out


def _is_str_literal(v) -> bool:
    return isinstance(v, ast.JoinedStr) or (isinstance(v, ast.Constant) and isinstance(v.value, str))


def _bare_reason_literals(path: Path):
    """绕过 `_why` 直接写进 reason 的字符串字面量（dict 里的 "reason": "x" / x["reason"] = "x"）。"""
    bad = []
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Dict):
            bad += [n.lineno for k, v in zip(n.keys, n.values)
                    if isinstance(k, ast.Constant) and k.value == "reason" and _is_str_literal(v)]
        elif isinstance(n, ast.Assign) and _is_str_literal(n.value) and any(
                isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "reason"
                for t in n.targets):
            bad.append(n.lineno)
    return bad


class TestReasonRegistry:
    def test_every_emitted_code_is_registered_and_vice_versa(self):
        emitted = _why_literals(PREMIUM) | _why_literals(SERVICE)
        assert emitted - set(PM.REASONS) == set()
        assert set(PM.REASONS) - emitted == set()                            # 登记了却没人产出 = 过期的条目
        assert _bare_reason_literals(PREMIUM) == []

    def test_external_codes_really_come_from_their_module(self):
        for code, (src, _label) in PM.EXTERNAL_REASONS.items():
            assert re.search(rf'["\']{code}["\':]', (ROOT / src).read_text(encoding="utf-8")), (code, src)

    def test_teeth(self, tmp_path):
        fake = tmp_path / "m.py"
        fake.write_text('x = _why("not_registered")\nout = {"reason": "bare_code"}\n', encoding="utf-8")
        assert _why_literals(fake) - set(PM.REASONS) == {"not_registered"}
        assert _bare_reason_literals(fake) == [2]

    def test_rules_ship_the_table(self):
        rules = _svc().premium("NVDA")["rules"]
        assert set(rules["reasons"]) == set(PM.REASONS) | set(PM.EXTERNAL_REASONS)
        assert set(rules["stages"]) == set(PM.STAGES)
        js = (ROOT / "alphabot/static/pages/premium.js").read_text(encoding="utf-8")
        assert "rules?.reasons" in js and "（未登记）" in js and "STAGE_LABEL" not in js

    def test_every_funnel_stage_has_a_label(self):
        svc = _svc()
        r = svc.premium("NVDA", {"expiry": svc.premium("NVDA")["expiries"][3]["expiry"]})
        assert {s["stage"] for s in r["funnel"]} <= set(PM.STAGES)


class TestEarningsFromScanLedger:
    """财报日取自卖权账本（扫描的 Chronos 日期），不读兜底缓存——根因回归测试在第一条。"""

    def test_scan_date_beats_a_later_cache_date(self, tmp_path):
        """2026-10-13 实测形状：扫描说 NEE 10-21，兜底缓存说 10-27。10-26 到期的合约必须标「财报在到期前」
        （照缓存会判成「到期后」——卖方最怕的那个方向）。"""
        led = _ledger(tmp_path / "ledger", [(AS_OF, "NEE", "recorded", {"earnings_date": "2026-10-21",
                                                                        "source": "chronos_bee_catalyst"})])
        (tmp_path / "earnings_cache").mkdir()
        (tmp_path / "earnings_cache" / "NEE_date.json").write_text(json.dumps(
            {"ticker": "NEE", "earnings_date": "2026-10-27", "fetched_at": "2026-10-01T09:00:00"}), encoding="utf-8")
        r = _svc(premium_root=tmp_path, ledger_dir=led).premium("NEE", WIDE)
        assert (r["earnings"]["status"], r["earnings"]["date"]) == ("known", "2026-10-21")
        ex = {e["expiry"]: e["earnings_before_expiry"] for e in r["expiries"]}
        assert ex["2026-10-26"] is True and ex["2026-10-19"] is False
        assert R.C.classify_earnings({"earnings_date": "2026-10-27"}, AS_OF, "2026-10-26") == "after_expiry"  # 缓存那个日期会漏

    def test_skips_failed_scan_days(self, tmp_path):
        led = _ledger(tmp_path / "ledger", [("2026-10-08", "NVDA", "recorded", {"earnings_date": "2026-11-17"}),
                                            (AS_OF, "NVDA", "unavailable", None)])
        r = _svc(ledger_dir=led).premium("NVDA")
        assert (r["earnings"]["scan_date"], r["earnings"]["date"]) == ("2026-10-08", "2026-11-17")

    def test_only_failed_days_is_not_in_recent_scans(self, tmp_path):
        led = _ledger(tmp_path / "ledger", [(AS_OF, "NVDA", "unavailable", None)])
        assert _svc(ledger_dir=led).premium("NVDA")["earnings"]["reason"] == "not_in_recent_scans"

    def test_chronos_failure_that_day_is_unknown(self, tmp_path):
        led = _ledger(tmp_path / "ledger", [(AS_OF, "NVDA", "recorded", None)])     # earnings_info None ⇒ unknown
        assert _svc(ledger_dir=led).premium("NVDA")["earnings"]["reason"] == "scan_had_no_earnings_info"

    def test_unreadable_ledger_is_named(self, tmp_path, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("disk")
        monkeypatch.setattr(R.LG, "_shard_paths", boom)
        r = _svc(ledger_dir=tmp_path).premium("NVDA")
        assert r["earnings"]["reason"] == "scan_ledger_unreadable:OSError" and r["data_available"]

    def test_ledger_is_left_untouched(self, tmp_path):
        led = _ledger(tmp_path / "ledger", [(AS_OF, "NVDA", "recorded", {"earnings_date": "2026-11-17"})])
        before = _fingerprint(led)
        _svc(ledger_dir=led).premium("NVDA")
        assert _fingerprint(led) == before


# ─────────────────────────────── 二次检查（v0.45.492 第二轮）

class TestEarningsObservationSkipsFailedDays:
    """#5：最新一天 Chronos 失败（recorded 但 earnings_status unknown）不该盖掉前一天拿到的日期。"""

    def test_falls_back_to_previous_informative_scan(self, tmp_path):
        led = _ledger(tmp_path / "ledger", [("2026-10-08", "TSLA", "recorded", {"earnings_date": "2026-10-21"}),
                                            (AS_OF, "TSLA", "recorded", None)])          # None ⇒ unknown
        e = _svc(ledger_dir=led).premium("TSLA")["earnings"]
        assert (e["status"], e["date"], e["scan_date"]) == ("known", "2026-10-21", "2026-10-08")

    def test_all_uninformative_reports_the_latest(self, tmp_path):
        led = _ledger(tmp_path / "ledger", [("2026-10-08", "TSLA", "recorded", None), (AS_OF, "TSLA", "recorded", None)])
        e = _svc(ledger_dir=led).premium("TSLA")["earnings"]
        assert (e["reason"], e["scan_date"]) == ("scan_had_no_earnings_info", AS_OF)

    def test_lookback_is_bounded(self):
        rows = [{"ticker": "X", "status": "recorded", "date": f"2026-09-{d:02d}", "earnings_status": "unknown"}
                for d in range(1, 29)] + [{"ticker": "X", "status": "recorded", "date": "2026-08-01",
                                           "earnings_status": "after_expiry", "earnings_date": "2026-10-30"}]
        got = PM.earnings_observation(rows, ticker="X", lookback=PM.EARNINGS_SCAN_LOOKBACK)
        assert got["scan_date"] == "2026-09-28" and got["earnings_status"] == "unknown"   # 不越过回看上限去捡旧日期
        assert PM.earnings_observation(rows, ticker="X", lookback=100)["scan_date"] == "2026-08-01"   # 对照


class TestShardsAreParsedOnce:
    """#8：每个 tenor 最近两个月分片各解析一次（旧实现按日期逐个 rows_for_date，MB 级分片重复解析几十次）。"""

    def test_load_count(self, tmp_path, monkeypatch):
        days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", AS_OF]
        led = _ledger(tmp_path / "ledger", [(d, "NVDA", "unavailable", None) for d in days])
        calls = []
        real = R.LG._load_shard
        monkeypatch.setattr(R.LG, "_load_shard", lambda p: calls.append(p) or real(p))
        e = _svc(ledger_dir=led).premium("SPY")["earnings"]                      # 不在账本里 ⇒ 最坏情形
        assert e["reason"] == "not_in_recent_scans"
        assert len(calls) == 2 * len(R.LG.TENORS)                                # 两个月 × 两个 tenor，各一次


class TestExternalReasonsAreComplete:
    """#3：外部码表要**全**——逐个枚举产出方真实会给的码，缺一个就红（旧测试只核「表里的码存在」）。"""

    @staticmethod
    def _raw_fail_codes():
        tree = ast.parse((ROOT / "cboe_options.py").read_text(encoding="utf-8"))
        return {n.args[1].value for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "_raw_fail" and len(n.args) > 1 and isinstance(n.args[1], ast.Constant)}

    @staticmethod
    def _reason_prefixes_in(path: Path, func: str):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func)
        out = set()
        for n in ast.walk(fn):
            vals = []
            if isinstance(n, ast.Dict):
                vals = [v for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant) and k.value == "reason"]
            elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                                                   and t.slice.value == "reason" for t in n.targets):
                vals = [n.value]
            for v in vals:
                for c in ast.walk(v):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) and re.match(r"^[a-z][a-z0-9_]*:?$", c.value):
                        out.add(c.value.rstrip(":"))
        return out

    def test_every_cboe_raw_failure_is_registered(self):
        codes = self._raw_fail_codes()
        assert {"stale_vintage", "price_unavailable"} <= codes                    # 对照：扫描真的扫到了
        assert codes - set(PM.EXTERNAL_REASONS) == set()

    def test_every_bars_and_vrp_reason_is_registered(self):
        bars = self._reason_prefixes_in(ROOT / "alphabot/service.py", "_default_bars")
        # v0.45.384 起 reason 由本体 `_record_day_with_info` 写（`record_day` 只是取第一项的薄包装）
        vrp = self._reason_prefixes_in(ROOT / "vrp_signal.py", "_record_day_with_info")
        assert "bars_fetch_exception" in bars and "rv_30d_missing" in vrp         # 对照
        assert (bars | vrp) - set(PM.EXTERNAL_REASONS) == set()

    def test_bars_fetch_exception_has_its_own_code(self, monkeypatch):
        """#7：取日线出错不再借用整页的「exception」（那个前缀读作「计算出错」，会指错层）。"""
        import twelve_data
        from alphabot import service as SV
        monkeypatch.setattr(twelve_data, "is_configured", lambda: True)
        monkeypatch.setattr(twelve_data, "fetch_bars", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("slow")))
        r = SV._default_bars("NVDA")
        assert r["reason"].startswith("bars_fetch_exception:TimeoutError") and not r["reason"].startswith("exception")


class TestBarsLagVersusGaps:
    """#4：「日线落后」与「窗内缺口」是两件事，分开报；落后时 RV 照算但页面要看得见。"""

    def test_lag_is_reported_not_called_a_gap(self):
        deps = _deps()
        ctx, rows, missing = PM.bars_context(_bars(_trading_bars("2026-10-07", 80)), today=TODAY, deps=deps)
        assert ctx["available"] and ctx["lag_sessions"] == 1 and ctx["gaps_in_windows"] == []
        assert PM.rv_window(rows, 20, missing=missing, rv_fn=deps.rv_from_closes)["value"] is not None

    def test_inner_gap_is_reported(self):
        full = _trading_bars("2026-10-08", 80)
        hole = full[-15]["date"]
        ctx, _rows, _m = PM.bars_context(_bars([b for b in full if b["date"] != hole]), today=TODAY, deps=_deps())
        assert ctx["gaps_in_windows"] == [hole] and ctx["lag_sessions"] == 0

    def test_old_gap_outside_every_window_is_not_reported(self):
        full = _trading_bars("2026-10-08", 120)
        hole = full[5]["date"]                                                   # 比最长窗口（61 根）还早
        ctx, _rows, _m = PM.bars_context(_bars([b for b in full if b["date"] != hole]), today=TODAY, deps=_deps())
        assert ctx["gaps_in_windows"] == []

    def test_page_shows_both_facts(self):
        js = (ROOT / "alphabot/static/pages/premium.js").read_text(encoding="utf-8")
        assert "bars.lag_sessions > 0" in js and "gaps_in_windows" in js and "gaps_critical" not in js


class TestPickedExpiryMustBeInWindow:
    """#6：选中的到期日掉出天数范围 ⇒ 服务端 400（param=expiry），前端据此取消选中并说明——不会卡在空表上。"""

    def test_out_of_window_is_rejected(self):
        svc = _svc()
        exp = next(e for e in svc.premium("NVDA")["expiries"] if e["dte"] > 40)
        assert svc.premium("NVDA", {"expiry": exp["expiry"]})["rows_total"] >= 0          # 对照：窗口内可选
        r = _client(svc).get(f"/api/premium/NVDA?expiry={exp['expiry']}&dte_max=30")
        assert r.status_code == 400 and r.json()["param"] == "expiry"
        js = (ROOT / "alphabot/static/pages/premium.js").read_text(encoding="utf-8")
        assert 'e.param === "expiry" && pick' in js


class TestRulesComputedOnce:
    """#9：规则常量与文案表进程内不变 ⇒ 每个服务实例只算一次（旧实现每个请求重算）。"""

    def test_once(self, monkeypatch):
        calls = []
        real = PM.public_rules
        monkeypatch.setattr(PM, "public_rules", lambda deps: calls.append(1) or real(deps))
        svc = _svc()
        for q in ({}, {"side": "put"}, {"sort": "yield_ann"}):
            assert svc.premium("NVDA", q)["rules"]["max_rows"] == PM.MAX_ROWS
        svc.meta_texts()
        assert len(calls) == 1

    def test_failure_is_not_cached(self, monkeypatch):
        svc = _svc()
        monkeypatch.setattr(PM, "public_rules", lambda deps: 1 / 0)
        assert "rules_error" in svc.premium_rules()
        monkeypatch.undo()
        assert "rules_error" not in svc.premium_rules()                         # 好了以后能恢复
