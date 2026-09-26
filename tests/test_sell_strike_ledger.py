"""sell_strike_ledger / sell_strike_report（v0.45.333）：记录 / 结算 / 独立单位 / 分块置换 / 盲化 / 预注册常量。

每条断言都对应一个让它变红的变异（台账见 CHANGELOG v0.45.333）。全部离线：取链、K 线、财报日都注入。
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import logging
import math
import re
import threading
import time
from collections import defaultdict
from pathlib import Path, PurePosixPath

import numpy as np
import pytest

import cboe_options
import sell_strike_candidates as C
import sell_strike_ledger as LG
import sell_strike_levels as L
import sell_strike_report as R

REPO = Path(__file__).resolve().parent.parent
PREREG_DOC = REPO / "experiments" / "sell_strike_routing_prereg.md"

AS_OF = "2026-09-23"
#: 本文件钉住的「PDT 今天」。写路径拒绝晚于今天的日期（2026-09-24 最终评审 R4），而夹具日期散布在
#: 2026-01 ~ 2026-11——不钉的话这些测试随真实时钟变红变绿（时间炸弹）。测拒绝的用例自己再拨。
TODAY = "2027-06-30"
PUT_K = {"0.10": 85.0, "0.16": 88.0, "0.20": 90.0, "0.25": 93.0, "0.30": 95.0}
CALL_K = {"0.10": 115.0, "0.16": 112.0, "0.20": 110.0, "0.25": 107.0, "0.30": 105.0}


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    monkeypatch.setattr(LG, "_today", lambda: TODAY)


# ─────────────────────────────── 夹具：合成行

def _leg(cp, K, *, bid=1.0, ask=1.04, itm_prob=0.2):
    mid = (bid + ask) / 2
    return {"symbol": f"X{cp}{K:g}", "strike": K, "cp": cp, "delta": 0.2 if cp == "C" else -0.2,
            "delta_source": "cboe", "iv": 0.3, "gamma": None, "theta": None, "oi": 100.0,
            "bid": bid, "ask": ask, "mid": mid, "spread_pct": (ask - bid) / mid, "quote_ok": True,
            "itm_prob": itm_prob, "sigma_distance": -0.8 if cp == "P" else 0.8}


def _ladder(expiry, dte=30):
    lad = {"expiry": expiry, "dte": dte, "underlying_price": 100.0, "put": {}, "call": {}}
    for d in C.LADDER_DELTAS:
        k = f"{d:.2f}"
        lad["put"][k] = {"short": _leg("P", PUT_K[k], itm_prob=d + 0.03), "wing": None,
                         "reasons": ["no_wing_strike"]}
        lad["call"][k] = {"short": _leg("C", CALL_K[k], itm_prob=d - 0.02), "wing": None,
                          "reasons": ["no_wing_strike"]}
    return lad


def _put_close(outcome):
    """0.20 档 short put（K=90, credit=bid=1.0）的到期收盘，使 pnl_over_credit == outcome（≤1）。"""
    return 100.0 if outcome >= 1.0 else 90.0 - (1.0 - outcome)


def _row(date, ticker="AAA", expiry="2026-10-23", *, tenor="monthly", flag_put=False, flag_call=False,
         close=100.0, status="recorded", settle_status="settled", earnings_status="none",
         route_ok=True, earnings_date=None, price_source="cboe_close"):
    r = LG._blank_row(date, ticker, tenor)
    if status != "recorded":
        r.update(status=status, unavailable_reason="stale_vintage")
        return r
    route = ({"put": "far" if flag_put else "base", "call": "far" if flag_call else "base",
              "flag_put": flag_put, "flag_call": flag_call}
             if route_ok else {"put": "unavailable", "call": "unavailable",
                               "flag_put": None, "flag_call": None})
    route.update(rule_version=1, view="le_45dte", reasons=[])
    r.update(status="recorded", expiry=expiry, dte=30, underlying_price=100.0,
             underlying_price_source=price_source, earnings_status=earnings_status,
             earnings_date=earnings_date,
             env={"view": "le_45dte", "curve_state": "all_positive", "sign_at_spot": "positive"},
             route=route, ladder=_ladder(expiry), settle_status=settle_status)
    if settle_status == "settled":
        r.update(expiry_close=close, expiry_close_date=expiry, expiry_close_source="injected",
                 settled_on=expiry)
    return r


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_keys(v)


# ─────────────────────────────── 夹具：合成链（run_for_date / compute_live）

def _bs_price(S, K, T, r, s, cp):
    d1, d2 = L.bs_d1_d2(S, K, T, r, s)
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731
    if cp == "C":
        return S * n(d1) - K * math.exp(-r * T) * n(d2)
    return K * math.exp(-r * T) * n(-d2) - S * n(-d1)


def _raw(ticker, as_of, *, S=100.0, dtes=(14, 30), iv=0.35):
    a = dt.date.fromisoformat(as_of)
    contracts = []
    for dte in dtes:
        exp = (a + dt.timedelta(days=dte)).isoformat()
        T = L.year_fraction(dte)
        for K in range(70, 131):
            for cp in "CP":
                px = _bs_price(S, float(K), T, 0.045, iv, cp)
                contracts.append({
                    "symbol": f"{ticker}{exp}{cp}{K}", "expiry": exp, "cp": cp, "strike": float(K),
                    "dte": dte, "iv": iv, "delta": L.bs_delta(S, float(K), T, 0.045, iv, cp),
                    "gamma": None, "vega": None, "theta": None, "theo": px,
                    # call OI 多于 put ⇒ 现价处净 gamma 为正（对称 OI 会让朴素净 GEX 恰好抵消成 0）
                    "oi": 2000.0 if cp == "C" else 500.0, "volume": 10.0,
                    "bid": max(round(px - 0.02, 2), 0.0), "ask": round(px + 0.02, 2),
                    "last_trade_time": None})
    return {"ticker": ticker, "as_of": as_of, "vintage_date": as_of, "underlying_price": S,
            "underlying_price_source": "cboe_close", "fetched_at": f"{as_of}T17:05:00-04:00",
            "contracts": contracts, "n_raw": len(contracts), "n_dropped_unparseable": 0,
            "n_expired_excluded": 0, "n_expiring_today_excluded": 0}


def _no_earnings(_t):
    return {"earnings_date": None, "source": "chronos_bee_no_earnings"}


# ═════════════════════════════════════════ 1–2 · record_rows 的不变式

class TestRecordRows:
    def test_same_day_rerun_carries_settlement_fields(self, tmp_path):
        """同日重跑（同一到期日）必须原样搬运结算字段。变异：不搬运 ⇒ settle_status 回到 pending。"""
        old = _row(AS_OF, expiry="2026-09-24", settle_status="settled", close=97.5)
        old["settle_attempts"] = 2
        LG.record_rows(AS_OF, "monthly", [old], state_dir=tmp_path)
        new = _row(AS_OF, expiry="2026-09-24", settle_status="pending")
        today = LG.record_rows(AS_OF, "monthly", [new], state_dir=tmp_path)
        got = today[0]
        for f in LG._SETTLEMENT_FIELDS:
            assert got[f] == old[f], f"结算字段 {f} 被同日重跑抹掉：{got[f]!r} != {old[f]!r}"
        assert LG.load_rows("monthly", state_dir=tmp_path)[0]["expiry_close"] == 97.5

    def test_expiry_change_does_not_carry_settlement(self, tmp_path):
        """重跑选中了别的到期日 ⇒ 旧结算属于旧到期日，不得搬。变异：无条件搬运。"""
        LG.record_rows(AS_OF, "monthly", [_row(AS_OF, expiry="2026-10-23", close=97.5)],
                       state_dir=tmp_path)
        new = _row(AS_OF, expiry="2026-10-30", settle_status="pending")
        got = LG.record_rows(AS_OF, "monthly", [new], state_dir=tmp_path)[0]
        assert got["expiry"] == "2026-10-30"
        assert got["settle_status"] == "pending" and got["expiry_close"] is None

    def test_unavailable_rerun_does_not_downgrade_recorded(self, tmp_path):
        """新行 unavailable、旧行 recorded ⇒ 保留旧行。变异：直接覆盖。"""
        LG.record_rows(AS_OF, "weekly", [_row(AS_OF, tenor="weekly", settle_status="pending")],
                       state_dir=tmp_path)
        bad = LG.unavailable_row(AS_OF, "AAA", "weekly", "stale_vintage")
        today, st = LG._record_rows(AS_OF, "weekly", [bad], tmp_path)
        assert today[0]["status"] == "recorded" and today[0]["ladder"] is not None
        assert st["kept_previous_recorded"] == 1
        # 反方向：旧的也是 unavailable 时，新的 unavailable 照写（原因更新）
        LG.record_rows(AS_OF, "monthly", [LG.unavailable_row(AS_OF, "BBB", "monthly", "payload_unavailable")],
                       state_dir=tmp_path)
        got = LG.record_rows(AS_OF, "monthly", [LG.unavailable_row(AS_OF, "BBB", "monthly", "stale_vintage")],
                             state_dir=tmp_path)
        assert got[0]["unavailable_reason"] == "stale_vintage"

    def test_rerun_with_subset_keeps_other_tickers(self, tmp_path):
        """按票 upsert：只重跑一只票不得抹掉当天其余票。变异：整日替换。"""
        LG.record_rows(AS_OF, "monthly", [_row(AS_OF, "AAA"), _row(AS_OF, "BBB")], state_dir=tmp_path)
        LG.record_rows(AS_OF, "monthly", [_row(AS_OF, "AAA", settle_status="pending")], state_dir=tmp_path)
        assert [r["ticker"] for r in LG.rows_for_date(AS_OF, "monthly", tmp_path)] == ["AAA", "BBB"]

    def test_nan_is_cleaned_and_corrupt_lines_survive_rewrite(self, tmp_path):
        """NaN 落盘成 null（严格 JSON）；分片里读不懂的行重写后原样保留并在 assess 里计数。
        变异：去掉 _clean（allow_nan=False 当场抛）；重写时丢坏行。"""
        shard = LG._shard("monthly", AS_OF, tmp_path)
        shard.parent.mkdir(parents=True)
        shard.write_text("{this is not json\n", encoding="utf-8")
        r = _row(AS_OF, settle_status="pending")
        r["env"]["total_at_spot"] = float("nan")
        LG.record_rows(AS_OF, "monthly", [r], state_dir=tmp_path)
        lines = shard.read_text(encoding="utf-8").splitlines()
        assert "{this is not json" in lines
        parsed = [json.loads(s, parse_constant=lambda c: pytest.fail(f"非严格 JSON 常量 {c}"))
                  for s in lines if s != "{this is not json"]
        assert parsed[0]["env"]["total_at_spot"] is None
        assert LG.assess("monthly", state_dir=tmp_path)["progress"]["n_corrupt_lines"] == 1

    def test_empty_batch_writes_nothing(self, tmp_path):
        """空批（本轮零只票）不改账本、不造空分片。变异：删空批早退 ⇒ 建出空的 2026-09.jsonl。"""
        assert LG.record_rows(AS_OF, "monthly", [], state_dir=tmp_path) == []
        assert not (tmp_path / "monthly").exists()

    def test_rows_must_match_date_and_tenor(self, tmp_path):
        """别的日期 / 期限的行塞进来 ⇒ ValueError（否则会写进错的分片）。变异：删校验。"""
        with pytest.raises(ValueError):
            LG.record_rows(AS_OF, "monthly", [_row("2026-09-22")], state_dir=tmp_path)
        with pytest.raises(ValueError):
            LG.record_rows(AS_OF, "weekly", [_row(AS_OF, tenor="monthly")], state_dir=tmp_path)


# ═════════════════════════════════════════ 3–4、8 · settle

def _pending(tmp_path, *, date="2026-10-01", expiry="2026-10-16", tenor="monthly", ticker="AAA"):
    LG.record_rows(date, tenor, [_row(date, ticker, expiry, tenor=tenor, settle_status="pending")],
                   state_dir=tmp_path)


class _Bars:
    def __init__(self, bars):
        self.bars, self.calls = bars, []

    def __call__(self, ticker):
        self.calls.append(ticker)
        return self.bars


class TestSettle:
    def test_uses_only_the_expiry_day_bar(self, tmp_path):
        """只给 T−1 的 K 线 ⇒ 结算不了、attempts+1。变异：≤5 天容差（取 ≤expiry 的最后一根）。"""
        _pending(tmp_path)
        bars = _Bars([{"date": "2026-10-14", "close": 80.0}, {"date": "2026-10-15", "close": 81.0},
                      {"date": "2026-10-19", "close": 99.0}])
        assert LG.settle("2026-10-20", "monthly", bars_fn=bars, state_dir=tmp_path) == 0
        r = LG.load_rows("monthly", state_dir=tmp_path)[0]
        assert r["settle_status"] == "pending" and r["settle_attempts"] == 1
        assert r["expiry_close"] is None

    def test_settles_with_expiry_close_not_as_of_close(self, tmp_path):
        """结算价 = 到期日那根收盘，不是 as_of 那根。变异：取 ≤as_of 的最后一根。"""
        _pending(tmp_path)
        bars = _Bars([{"date": "2026-10-15", "close": 81.0}, {"date": "2026-10-16 00:00:00", "close": 88.5},
                      {"date": "2026-10-19", "close": 99.0}])
        assert LG.settle("2026-10-19", "monthly", bars_fn=bars, state_dir=tmp_path) == 1
        r = LG.load_rows("monthly", state_dir=tmp_path)[0]
        assert (r["settle_status"], r["expiry_close"], r["expiry_close_date"]) == ("settled", 88.5, "2026-10-16")
        assert (r["settled_on"], r["expiry_close_source"]) == ("2026-10-19", "injected")

    def test_expiry_day_itself_is_not_settled(self, tmp_path):
        """到期当天不结算（当日 K 线收盘后才完整）。变异：expiry <= as_of。"""
        _pending(tmp_path)
        bars = _Bars([{"date": "2026-10-16", "close": 88.5}])
        assert LG.settle("2026-10-16", "monthly", bars_fn=bars, state_dir=tmp_path) == 0
        r = LG.load_rows("monthly", state_dir=tmp_path)[0]
        assert r["settle_status"] == "pending" and r["settle_attempts"] == 0 and bars.calls == []

    def test_give_up_after_max_attempts(self, tmp_path):
        """拿到**已越过到期日**的 K 线却没有到期日那根，累计 settle_max_attempts **天** ⇒ give_up
        （同一天至多计一次、源没追上不计，见 TestSettleAttemptsCountDays）。变异：> 代替 >= / 删放弃闸。"""
        _pending(tmp_path)
        bars = _Bars([{"date": "2026-10-15", "close": 81.0}, {"date": "2026-10-19", "close": 99.0}])
        n = LG.CONFIG["settle_max_attempts"]
        days = [f"2026-10-{20 + i}" for i in range(n)]
        for d in days[:-1]:
            LG.settle(d, "monthly", bars_fn=bars, state_dir=tmp_path)
        assert LG.load_rows("monthly", state_dir=tmp_path)[0]["settle_status"] == "pending"
        st = LG._settle(days[-1], "monthly", bars_fn=bars, state_dir=tmp_path)
        r = LG.load_rows("monthly", state_dir=tmp_path)[0]
        assert (r["settle_status"], r["settle_give_up_reason"], r["settle_attempts"]) == \
            ("give_up", "max_attempts_exhausted", n)
        assert (r["settle_give_up_on"], r["settle_last_attempt_on"]) == (days[-1], days[-1])
        assert st["gave_up"] == {"max_attempts_exhausted": 1}
        # 放弃后不再取 K 线
        before = len(bars.calls)
        LG.settle("2026-10-30", "monthly", bars_fn=bars, state_dir=tmp_path)
        assert len(bars.calls) == before

    def test_give_up_when_expiry_left_fetch_window(self, tmp_path):
        """expiry 掉出取数窗口 ⇒ 直接 give_up，不再白取 K 线。变异：删窗口闸。"""
        _pending(tmp_path, date="2026-01-02", expiry="2026-01-16")
        bars = _Bars([{"date": "2026-09-01", "close": 1.0}])
        st = LG._settle("2026-09-23", "monthly", bars_fn=bars, state_dir=tmp_path)
        r = LG.load_rows("monthly", state_dir=tmp_path)[0]
        assert (r["settle_status"], r["settle_give_up_reason"]) == ("give_up", "out_of_fetch_window")
        assert bars.calls == [] and st["gave_up"] == {"out_of_fetch_window": 1}

    def test_bars_unavailable_does_not_count_an_attempt(self, tmp_path):
        """取不到 K 线（限流 / 断网）不计次，但计进 bars_unavailable。变异：取不到也 attempts+1。"""
        _pending(tmp_path)
        st = LG._settle("2026-10-20", "monthly", bars_fn=lambda t: None, state_dir=tmp_path)
        r = LG.load_rows("monthly", state_dir=tmp_path)[0]
        assert r["settle_attempts"] == 0 and r["settle_status"] == "pending"
        assert st["bars_unavailable"] == 1

    def test_cross_month_pending_row_is_settled_in_its_own_shard(self, tmp_path):
        """9 月记的行 10 月才到期：settle(10 月) 必须扫到 9 月分片并就地更新。变异：只扫 as_of 当月分片。"""
        _pending(tmp_path, date="2026-09-28", expiry="2026-10-16")
        _pending(tmp_path, date="2026-10-02", expiry="2026-10-16", ticker="BBB")
        sep = LG._shard("monthly", "2026-09-28", tmp_path)
        oct_ = LG._shard("monthly", "2026-10-02", tmp_path)
        assert (sep.name, oct_.name) == ("2026-09.jsonl", "2026-10.jsonl")
        bars = _Bars([{"date": "2026-10-16", "close": 101.0}])
        assert LG.settle("2026-10-19", "monthly", bars_fn=bars, state_dir=tmp_path) == 2
        sep_rows, _ = LG._load_shard(sep)
        assert sep_rows[0]["settle_status"] == "settled" and sep_rows[0]["expiry_close"] == 101.0
        assert sorted(bars.calls) == ["AAA", "BBB"]


# ═════════════════════════════════════════ 5 · 独立单位

class TestIndependentUnits:
    def test_ten_rows_same_ticker_expiry_is_one_unit_the_earliest(self):
        """同一 (ticker, expiry) 连续 10 天 ⇒ 1 个单位，且是最早那行。变异：取最新 / 不去重。"""
        rows = [_row(f"2026-09-{d:02d}", "AAA", "2026-10-23") for d in range(10, 20)]
        rows += [_row("2026-09-15", "BBB", "2026-10-23"), _row("2026-09-16", "AAA", "2026-10-30")]
        units = LG.independent_units(list(reversed(rows)))
        keyed = {(u["ticker"], u["expiry"]): u["date"] for u in units}
        assert keyed == {("AAA", "2026-10-23"): "2026-09-10", ("BBB", "2026-10-23"): "2026-09-15",
                         ("AAA", "2026-10-30"): "2026-09-16"}

    def test_eligibility_filters(self):
        """排除：未结算 / 不可得 / route 不可得 / 财报 before_expiry / unknown；保留 none 与 after_expiry。
        变异：财报集合写成只认 "none"（SPEC 字面）/ 不排除 unknown / 不看 route。"""
        rows = [
            _row("2026-09-01", "OK1"),
            _row("2026-09-01", "OK2", earnings_status="after_expiry"),
            _row("2026-09-01", "E1", earnings_status="before_expiry"),
            _row("2026-09-01", "E2", earnings_status="unknown"),
            _row("2026-09-01", "P1", settle_status="pending"),
            _row("2026-09-01", "G1", settle_status="give_up"),
            _row("2026-09-01", "U1", status="unavailable"),
            _row("2026-09-01", "R1", route_ok=False),
        ]
        assert sorted(u["ticker"] for u in LG.independent_units(rows)) == ["OK1", "OK2"]

    def test_unit_outcome_is_primary_rung_single_leg_pnl_over_credit(self):
        """unit_outcome = 该档单腿 short_<side> 的 pnl/credit；不可报价 ⇒ None。变异：返回 pnl 而非 pnl/credit。"""
        r = _row("2026-09-01", close=_put_close(-0.5))
        r["ladder"]["put"]["0.20"]["short"]["bid"] = 2.0          # credit=2 ⇒ 亏 1.5/2
        r["ladder"]["put"]["0.20"]["short"]["ask"] = 2.1
        r["expiry_close"] = 90.0 - 3.5                          # 内在 3.5 ⇒ pnl = 2 − 3.5 = −1.5
        assert LG.unit_outcome(r, "put", 0.20) == pytest.approx(-0.75)
        assert LG.unit_outcome(r, "call", 0.20) == pytest.approx(1.0)
        r["ladder"]["call"]["0.20"]["short"]["quote_ok"] = False
        assert LG.unit_outcome(r, "call", 0.20) is None


# ═════════════════════════════════════════ 6 · 分块置换

def _units(day_specs, *, seed=0):
    """day_specs: [(n_units, p_flag, mean_outcome)]，一天一个块；outcome = min(1, mean + N(0, .3))。"""
    rng = np.random.default_rng(seed)
    out = []
    base = dt.date(2026, 1, 5)
    for i, (n, p_flag, mu) in enumerate(day_specs):
        day = (base + dt.timedelta(days=i)).isoformat()
        for j in range(n):
            o = float(min(1.0, mu + rng.normal(0, 0.3)))
            out.append(_row(day, f"T{j:02d}", f"x{i}-{j}", flag_put=bool(rng.random() < p_flag),
                            close=_put_close(o)))
    return out


def _global(units):
    return [dict(u, date="ALL") for u in units]


class TestBlockPermutation:
    def test_null_p_values_are_roughly_uniform(self):
        """零假设（flag 与结果无关）下，200 个数据集的 p 近似均匀。变异：不打乱（零分布恒等于观测 ⇒ p≡1）。"""
        ps = []
        for s in range(200):
            units = _units([(8, 0.4, 0.3)] * 12, seed=s)
            ps.append(LG.block_permutation_p(units, "put", n_perm=199, seed=1000 + s)["p"])
        ps = np.sort(np.asarray(ps))
        assert 0.01 <= np.mean(ps <= 0.05) <= 0.11
        assert 0.38 <= np.mean(ps <= 0.5) <= 0.62
        ks = np.max(np.abs(ps - (np.arange(1, 201) / 200)))
        assert ks < 0.13, f"KS={ks:.3f}：零假设下 p 不均匀"

    def test_detects_within_day_effect_in_h1_direction(self):
        """同一天里 flag 组明显更差 ⇒ p 很小；反方向（flag 更好）⇒ p 很大。变异：尾巴取反（null ≥ observed）。"""
        units = _units([(10, 0.5, 0.5)] * 10, seed=7)
        worse = [dict(u, expiry_close=_put_close(-1.0)) if u["route"]["flag_put"] else u for u in units]
        better = [dict(u, expiry_close=_put_close(1.0)) if u["route"]["flag_put"]
                  else dict(u, expiry_close=_put_close(-1.0)) for u in units]
        assert LG.block_permutation_p(worse, "put", n_perm=999, seed=1)["p"] < 0.01
        assert LG.block_permutation_p(better, "put", n_perm=999, seed=1)["p"] > 0.9

    def test_day_confounding_is_not_mistaken_for_flag_effect(self):
        """flag 只与坏日子相关、日内与结果无关：分块置换 p 不小，全局置换 p 很小。变异：忽略分块、全局打乱。"""
        bad_day, good_day = (8, 0.8, -0.5), (8, 0.2, 0.8)
        small_block = 0
        for s in range(20):
            units = _units([bad_day, good_day] * 8, seed=s)
            p_block = LG.block_permutation_p(units, "put", n_perm=499, seed=s)["p"]
            p_global = LG.block_permutation_p(_global(units), "put", n_perm=499, seed=s)["p"]
            assert p_global < 0.01, f"seed={s}：全局置换没抓到日期混杂（p={p_global}），夹具没咬住"
            small_block += p_block < 0.05
        assert small_block <= 4, f"20 个日期混杂数据集里分块置换有 {small_block} 个 p<0.05（应≈1）"

    def test_pure_day_effect_gives_p_one_and_uninformative_blocks_are_counted(self):
        """结果只随日子变（日内全相同）⇒ 日内打乱改变不了统计量 ⇒ p == 1。变异：全局打乱。"""
        units = []
        for i, (mu, p_flag) in enumerate([(-1.0, 0.75), (0.9, 0.25)] * 5):
            day = f"2026-02-{i + 1:02d}"
            for j in range(4):
                units.append(_row(day, f"T{j}", f"e{i}{j}", flag_put=j < int(4 * p_flag),
                                  close=_put_close(mu)))
        units.append(_row("2026-03-01", "Z", "ez", flag_put=True, close=_put_close(0.0)))
        res = LG.block_permutation_p(units, "put", n_perm=499, seed=3)
        assert res["p"] == 1.0
        assert res["n_blocks"] == 11 and res["n_informative_blocks"] == 10
        assert LG.block_permutation_p(_global(units), "put", n_perm=499, seed=3)["p"] < 0.01

    def test_empty_group_returns_no_p(self):
        """某组为空 ⇒ observed / p 为 None + reason，不抛。变异：除零。"""
        units = _units([(5, 0.0, 0.5)] * 3, seed=2)
        res = LG.block_permutation_p(units, "put", n_perm=99, seed=1)
        assert res["p"] is None and res["reason"] == "empty_group" and res["n_flagged"] == 0

    def test_is_deterministic_for_a_seed(self):
        """同一 seed 同一结果（预注册的 seed 才有意义）。变异：不用传入的 seed。"""
        units = _units([(6, 0.5, 0.2)] * 6, seed=4)
        a = LG.block_permutation_p(units, "put", n_perm=299, seed=11)["p"]
        b = LG.block_permutation_p(list(reversed(units)), "put", n_perm=299, seed=11)["p"]
        c = [LG.block_permutation_p(units, "put", n_perm=299, seed=k)["p"] for k in range(12, 20)]
        assert a == b
        assert any(x != a for x in c), "换 seed 结果从不变化——seed 没被用上"


# ═════════════════════════════════════════ 7 · 就绪闸与盲化

def _ready_rows(n_units=60, n_expiries=12, n_flag=10):
    rows = []
    for i in range(n_units):
        e = i % n_expiries
        day = f"2026-{1 + e // 28:02d}-{1 + e % 28:02d}"
        exp = f"2027-{1 + e // 28:02d}-{1 + e % 28:02d}"
        flag = i < n_flag
        o = -0.5 if (i % 3 == 0) else 0.8
        rows.append(_row(day, f"T{i:03d}", exp, flag_put=flag, flag_call=flag, close=_put_close(o)))
    return rows


class TestAssessGateAndBlinding:
    def test_accruing_result_contains_no_effect_keys(self):
        """未就绪时返回值里不得出现任何效应量 / p 值键（结构性盲化）。变异：总是计算检验。"""
        res = LG.assess("monthly", rows=_ready_rows(40, 12, 10))
        assert res["status"] == "accruing"
        leaked = set(_walk_keys(res)) & LG.BLINDED_KEYS
        assert not leaked, f"未就绪却吐出了 {leaked}"
        assert res["progress"]["n_independent"] == 40
        assert res["progress"]["per_side"]["put"]["n_flagged"] == 10

    def test_ready_boundary_and_test_output(self):
        """恰好 60 单位 / 12 到期日 / 每组 10 ⇒ ready 且给出检验；少一个单位 ⇒ accruing。变异：> 代替 >=。"""
        res = LG.assess("monthly", rows=_ready_rows())
        assert res["status"] == "ready", res["gates"]
        for side in LG.SIDES:
            t = res["test"][side]
            assert {"observed", "p", "alpha_each", "decision"} <= set(t)
            assert t["alpha_each"] == LG.PREREG["alpha_each"] and t["n_perm"] == LG.PREREG["n_perm"]
            assert t["decision"] in ("reject_h0", "fail_to_reject_h0")
        assert "不代表已验证" in res["note"]
        assert LG.assess("monthly", rows=_ready_rows(59, 12, 10))["status"] == "accruing"
        assert LG.assess("monthly", rows=_ready_rows(60, 11, 10))["status"] == "accruing"
        assert LG.assess("monthly", rows=_ready_rows(60, 12, 9))["status"] == "accruing"

    def test_undetermined_on_empty_and_as_of_filter(self, tmp_path):
        """无行 ⇒ undetermined；as_of 只看 date ≤ as_of。变异：as_of 过滤写反。"""
        assert LG.assess("weekly", state_dir=tmp_path)["status"] == "undetermined"
        rows = _ready_rows()
        assert LG.assess("monthly", rows=rows, as_of="2025-12-31")["status"] == "undetermined"
        assert LG.assess("monthly", rows=rows, as_of="2026-01-05")["progress"]["n_rows"] == 25

    def test_calibration_is_descriptive_by_rung(self):
        """按档：实际 ITM 频率 vs 平均 N(d2)。变异：put 侧 ITM 判成 close > K。"""
        # 收盘刻意不对称地落在 K 两侧（1 个低于 90、3 个高于）：对称的话 put 判反也恰好同频，测不出来
        rows = [_row(f"2026-03-{i + 1:02d}", f"C{i}", f"c{i}", close=c)
                for i, c in enumerate([80.0, 100.0, 120.0, 130.0])]
        cal = LG.assess("monthly", rows=rows)["calibration"]
        assert cal["put"]["0.20"]["n"] == 4
        assert cal["put"]["0.20"]["actual_itm_freq"] == pytest.approx(0.25)  # 只有 80 < 90
        assert cal["put"]["0.20"]["mean_itm_prob_risk_neutral"] == pytest.approx(0.23)
        assert cal["call"]["0.20"]["actual_itm_freq"] == pytest.approx(0.5)   # 120、130 > 110


# ═════════════════════════════════════════ 9 · 路径

class TestPaths:
    def test_state_dir_resolved_at_call_time(self, tmp_path, monkeypatch):
        """setenv ALPHA_HIVE_HOME 后解析跟着变，写入也跟着走。变异：模块级常量 / 首次调用后缓存。"""
        a, b = tmp_path / "A", tmp_path / "B"
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(a))
        assert LG._state_dir() == a / "sell_strike_state"
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(b))
        assert LG._state_dir() == b / "sell_strike_state"
        LG.record_rows(AS_OF, "weekly", [_row(AS_OF, tenor="weekly")])
        assert (b / "sell_strike_state" / "weekly" / "2026-09.jsonl").is_file()
        assert not a.exists()

    def test_conftest_guard_checked_this_tests_sandbox(self, request, tmp_path):
        """conftest `_isolate_sell_strike_state` 的防线①确实在本条测试 setup 时核对过**本条**的沙箱，
        且两个解析器（PATHS 与账本自己的 `_state_dir`）都核了。变异：删掉 setup 核对 / 只核 PATHS。"""
        want = str(tmp_path / "sell_strike_state")
        got = getattr(request.config, "_alpha_hive_sell_strike_guard_checked", None)
        assert got == {"PATHS.sell_strike_state": want, "sell_strike_ledger._state_dir()": want}

    def test_conftest_sandbox_check_has_teeth(self, sell_strike_state_sandbox_check, tmp_path):
        """防线①的判据：真身路径、相对路径、`..` 逃逸都必须红；沙箱内放行。变异：删包含判定 / 删绝对判定。"""
        check = sell_strike_state_sandbox_check
        assert check({"ok": tmp_path / "sell_strike_state"}, tmp_path) == \
            {"ok": str(tmp_path / "sell_strike_state")}
        # 按「哪一条断言」核：包含判定本身也会拒相对路径，只数 AssertionError 的话删掉绝对判定测不出来
        for bad, why in ((PurePosixPath("sell_strike_state"), "相对路径"),
                         (REPO / "sell_strike_state", "逃出了测试沙箱"),
                         (tmp_path / ".." / ".." / "sell_strike_state", "逃出了测试沙箱")):
            with pytest.raises(AssertionError, match=why):
                check({"bad": bad}, tmp_path)


# ═════════════════════════════════════════ 10 · run_for_date

class TestRunForDate:
    def test_one_tenor_failure_does_not_take_down_the_other(self, tmp_path, monkeypatch):
        """周度行构造抛异常 ⇒ 周度记 exception，月度照常记录；取数失败与异常各自计数；结算计数进返回值。
        变异：两个 tenor 共用一个 try。"""
        _pending(tmp_path, date="2026-09-01", expiry="2026-09-18")

        def fetch(t, *, as_of):
            if t == "BBB":
                return None, "stale_vintage"
            if t == "CCC":
                raise RuntimeError("boom")
            return _raw(t, as_of), None

        real = LG.build_tenor_row

        def flaky(raw, lm, tenor, **kw):
            if tenor == "weekly":
                raise KeyError("weekly broke")
            return real(raw, lm, tenor, **kw)

        monkeypatch.setattr(LG, "build_tenor_row", flaky)
        bars = _Bars([{"date": "2026-09-18", "close": 93.0}])
        res = LG.run_for_date(AS_OF, tickers=["CCC", "AAA", "BBB", "AAA"], upcoming_fn=_no_earnings,
                              fetch_fn=fetch, bars_fn=bars, state_dir=tmp_path)
        m, w = res["per_tenor"]["monthly"], res["per_tenor"]["weekly"]
        assert res["n_tickers"] == 3
        assert res["fetch_reasons"] == {"ok": 1, "stale_vintage": 1, "exception:RuntimeError": 1}
        assert m["recorded"] == 1 and m["unavailable"] == {"stale_vintage": 1, "exception:RuntimeError": 1}
        assert w["recorded"] == 0
        assert w["unavailable"] == {"exception:KeyError": 1, "stale_vintage": 1, "exception:RuntimeError": 1}
        assert (m["settled"], w["settled"], m["errors"], w["errors"]) == (1, 0, [], [])
        assert m["earnings_status"] == {"none": 1}
        rec = [r for r in LG.rows_for_date(AS_OF, "monthly", tmp_path) if r["status"] == "recorded"]
        assert [r["ticker"] for r in rec] == ["AAA"] and rec[0]["route"]["put"] in ("base", "far")
        assert rec[0]["env"]["sign_at_spot"] == "positive"
        assert rec[0]["ladder"]["put"]["0.20"]["short"] is not None

    def test_record_failure_in_one_tenor_is_reported_not_raised(self, tmp_path, monkeypatch):
        """一档写盘失败 ⇒ 进 errors、另一档照常。变异：record 不包 try（异常冒出 run_for_date）。"""
        real = LG._record_rows

        def broken(as_of, tenor, rows, state_dir=None):
            if tenor == "monthly":
                raise OSError("disk full")
            return real(as_of, tenor, rows, state_dir)

        monkeypatch.setattr(LG, "_record_rows", broken)
        res = LG.run_for_date(AS_OF, tickers=["AAA"], upcoming_fn=_no_earnings,
                              fetch_fn=lambda t, as_of: (_raw(t, as_of), None),
                              bars_fn=lambda t: None, state_dir=tmp_path)
        assert res["per_tenor"]["monthly"]["errors"][0].startswith("record:OSError")
        assert res["per_tenor"]["weekly"]["recorded"] == 1

    def test_cli_exit_codes(self, tmp_path, capsys):
        """--assess：空账本 ⇒ 3；有行 ⇒ 0。--settle 成功 ⇒ 0。变异：退出码恒 0。"""
        assert LG.main(["--assess", "--date", AS_OF, "--state-dir", str(tmp_path)]) == 3
        LG.record_rows(AS_OF, "weekly", [_row(AS_OF, tenor="weekly", settle_status="pending")],
                       state_dir=tmp_path)
        assert LG.main(["--date", AS_OF, "--state-dir", str(tmp_path)]) == 0
        assert LG.main(["--settle", "--date", AS_OF, "--state-dir", str(tmp_path), "--json"]) == 0
        out = capsys.readouterr().out
        assert "◐ weekly" in out and '"settle"' in out


# ═════════════════════════════════════════ 预注册常量与文档同值

def _doc_constants():
    text = PREREG_DOC.read_text(encoding="utf-8")
    m = re.search(r"```prereg-constants\n(.*?)```", text, re.S)
    assert m, "预注册文档里找不到 ```prereg-constants 块"
    out = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        name, _, lit = line.partition(" = ")
        assert name and lit, f"无法解析的常量行：{line!r}"
        assert name not in out, f"常量 {name} 在文档里出现两次"
        out[name] = ast.literal_eval(lit)
    return out


def _resolve(name):
    mod, rest = name.split(".", 1)
    obj = {"ledger": LG, "candidates": C, "levels": L}[mod]
    if rest.startswith("PREREG."):
        return obj.PREREG[rest[len("PREREG."):]]
    if "." in rest:                                   # 函数缺省参数，如 zero_gamma_sweep.band_pct
        fn, arg = rest.split(".")
        return inspect.signature(getattr(obj, fn)).parameters[arg].default
    return getattr(obj, rest)


class TestPreregPinned:
    MUST_BE_IN_DOC = {"candidates.ROUTE_RULE_VERSION", "candidates.ROUTE_VIEW", "candidates.FLIP_BUFFER_PCT",
                      "candidates.BASE_RUNG", "candidates.FAR_RUNG", "candidates.LADDER_DELTAS",
                      "candidates.DELTA_TOL", "candidates.MAX_SPREAD_PCT", "candidates.WING_WIDTH_SIGMA",
                      "candidates.TENORS", "levels.zero_gamma_sweep.band_pct",
                      "levels.zero_gamma_sweep.grid_points", "levels.T_FLOOR_DAYS",
                      # 登记前定稿（2026-09-23）加的三项：占档顺序、扫描门槛、冻结文件名
                      "candidates.LADDER_FILL_ORDER", "candidates.MIN_SWEEP_CONTRACTS",
                      "ledger.PREREG_RESULT_NAME"}

    def test_doc_and_code_constants_are_equal(self):
        """文档常量块与代码逐项相等。变异：改代码 PREREG / 路由常量 / 扫描网格而不改文档（或反之）。"""
        doc = _doc_constants()
        mismatched = {k: (v, _resolve(k)) for k, v in doc.items() if _resolve(k) != v}
        assert not mismatched, f"预注册文档与代码不一致（文档值, 代码值）：{mismatched}"

    def test_doc_covers_every_prereg_key_and_route_constant(self):
        """PREREG 每个键都必须在文档里（不许漏、不许多）；路由常量一个都不能少。变异：PREREG 加键不登记。"""
        doc = _doc_constants()
        doc_prereg = {k[len("ledger.PREREG."):] for k in doc if k.startswith("ledger.PREREG.")}
        assert doc_prereg == set(LG.PREREG)
        assert self.MUST_BE_IN_DOC <= set(doc)

    def test_prereg_arithmetic(self):
        """α_each = α_family / n_tests，n_tests = sides × tenors，tenors 与候选层同一组。变异：改 n_tests。"""
        p = LG.PREREG
        assert p["n_tests"] == len(p["sides"]) * len(p["tenors"])
        assert p["alpha_each"] == pytest.approx(p["alpha_family"] / p["n_tests"])
        assert set(p["tenors"]) == set(C.TENORS) and set(p["sides"]) == set(LG.SIDES)
        assert p["primary_rung"] == C.BASE_RUNG


# ═════════════════════════════════════════ 报告适配层

class TestReport:
    def test_no_rows_still_renders_a_reason(self, tmp_path):
        """当日无行 ⇒ 不返回空串，写明「今日无数据」。变异：无行返回 ""。"""
        md = R.render_markdown(AS_OF, state_dir=tmp_path)
        assert "今日无数据" in md and "不构成投资建议" in md and "0 行" in md

    def test_render_with_rows_marks_not_ready_and_shows_route(self, tmp_path):
        """有行：◐ 未就绪提示 + 覆盖率（按原因计数）+ 路由 + 梯子 + 结构表。变异：删掉 ◐ 提示。"""
        LG.record_rows(AS_OF, "monthly", [_row(AS_OF, "AAA", flag_put=True, settle_status="pending"),
                                          LG.unavailable_row(AS_OF, "BBB", "monthly", "stale_vintage")],
                       state_dir=tmp_path)
        md = R.render_markdown(AS_OF, state_dir=tmp_path)
        assert "◐ monthly：样本不足，环境路由仅供参考" in md
        assert "记录 1 / 不可得 1（stale_vintage 1）" in md
        assert "put=far（0.10Δ）" in md and "call=base（0.20Δ）" in md
        assert "| 0.20 | 90.00 |" in md and "铁鹰" in md
        for c in R.CAVEATS:
            assert c in md

    def test_local_report_path_is_private_and_not_deploy_shaped(self, tmp_path):
        """写到 <state>/reports/sell-strike-<日期>.md；名字不带 alpha-hive- 前缀（部署 / 自动提交白名单的形状）。
        变异：文件名改成 alpha-hive-sell-strike-*。"""
        p = R.write_local_report(AS_OF, state_dir=tmp_path)
        assert p == tmp_path / "reports" / f"sell-strike-{AS_OF}.md" and p.is_file()
        assert not p.name.startswith("alpha-hive")
        assert "今日无数据" in p.read_text(encoding="utf-8")

    def test_compute_live_never_raises_and_never_writes(self, tmp_path, monkeypatch):
        """MCP 现算：取数失败 / 抛异常 ⇒ data_available=False；成功 ⇒ 两档行 + 结构，**不写账本**。
        变异：compute_live 顺手 record_rows。"""
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
        assert R.compute_live("aaa", fetch_fn=lambda t, as_of: (None, "stale_vintage")) == \
            {"data_available": False, "ticker": "AAA", "reason": "stale_vintage",
             "caveats": list(R.CAVEATS), "disclaimer": R.DISCLAIMER}

        def boom(t, as_of):
            raise ConnectionError("x")

        bad = R.compute_live("AAA", fetch_fn=boom)
        assert bad["data_available"] is False and bad["reason"].startswith("exception:ConnectionError")
        seen = []
        live = R.compute_live("AAA", fetch_fn=lambda t, as_of: seen.append(as_of) or (_raw(t, AS_OF), None))
        assert seen == [None], "现算必须以 as_of=None 取链（用 payload vintage）"
        assert live["data_available"] and live["as_of"] == AS_OF
        assert live["tenors"]["monthly"]["structures"]["base"]["short_put"]["quotable"]
        assert live["tenors"]["weekly"]["earnings_status"] == "unknown"
        assert live["assess"]["monthly"]["status"] == "undetermined"
        assert not (tmp_path / "sell_strike_state").exists()

    def test_rows_for_ticker_reads_ledger(self, tmp_path):
        """MCP 给了日期 ⇒ 读账本该票两档行（含结构报价）；没有 ⇒ data_available=False + reason。变异：大小写不归一。"""
        LG.record_rows(AS_OF, "weekly", [_row(AS_OF, "AAA", tenor="weekly", settle_status="pending")],
                       state_dir=tmp_path)
        got = R.rows_for_ticker(AS_OF, "aaa", state_dir=tmp_path)
        assert got["data_available"] and got["tenors"]["monthly"] is None
        assert got["tenors"]["weekly"]["structures"]["route"]["short_put"]["quotable"]
        miss = R.rows_for_ticker(AS_OF, "ZZZ", state_dir=tmp_path)
        assert miss["data_available"] is False and miss["reason"] == "no_ledger_rows_for_date"


# ═════════════════════════════════════════ 登记前定稿（2026-09-23）：F2-1 ~ F2-8
# 每条都对应评审里一条探针实测过的缺陷；docstring 写让它变红的变异。

def _persist(rows, state_dir, tenor="monthly"):
    """按记录日分批写进账本（record_rows 一批只收同一天的行）。"""
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    for day in sorted(by_day):
        LG.record_rows(day, tenor, by_day[day], state_dir=state_dir)


def _ready_ledger_rows(n_units=60, n_expiries=12, n_flag=10, *, month=1, tag="T", outcome=None,
                       tenor="monthly"):
    """刚好够就绪闸的一批已结算行，日期真实（结算日 = 到期次日），可写进账本。
    `outcome(i, flag)` 缺省同 `_ready_rows`：每 3 个里 1 个亏 0.5 倍权利金。"""
    rows = []
    for i in range(n_units):
        e = i % n_expiries
        flag = i < n_flag
        o = outcome(i, flag) if outcome else (-0.5 if i % 3 == 0 else 0.8)
        r = _row(f"2026-{month:02d}-{1 + e:02d}", f"{tag}{i:03d}", f"2026-{month + 1:02d}-{1 + e:02d}",
                 tenor=tenor, flag_put=flag, flag_call=flag, close=_put_close(o))
        r["settled_on"] = f"2026-{month + 1:02d}-{2 + e:02d}"
        rows.append(r)
    return rows


def _later_units_that_would_move_the_test():
    """就绪之后才到的 40 个单位：flag 组全亏、normal 组全收——重算的话 put 侧的 observed 必变。"""
    return _ready_ledger_rows(40, 10, 40, month=3, tag="U",
                              outcome=lambda i, flag: -1.0 if i % 2 == 0 else 1.0)


class TestFrozenPreregResult:
    """F2-1：首次就绪即冻结，此后只读不重算（原实现每次 assess 都在更多数据上重跑 ⇒ 可选停止）。"""

    def test_first_ready_freezes_and_later_data_cannot_change_it(self, tmp_path):
        """就绪后追加数据：从账本读的 test 逐字节不变、冻结文件逐字节不变；而同样的全部行现算（rows= 注入）
        确实给出不同的 observed——证明「不变」是冻结的功劳、不是新数据恰好不影响结果。
        变异：ready 时每次重算（`out["test"] = _run_prereg_test(units)`）/ 每次覆盖写冻结文件。"""
        first_rows = _ready_ledger_rows()
        _persist(first_rows, tmp_path)
        res1 = LG.assess("monthly", state_dir=tmp_path, freeze=True)
        assert res1["status"] == "ready", res1["gates"]
        path = LG.prereg_result_path("monthly", tmp_path)
        assert path.name == LG.PREREG_RESULT_NAME and path.is_file()
        frozen_bytes = path.read_bytes()
        d = json.loads(frozen_bytes)
        assert (d["tenor"], d["prereg_version"], d["ready_date"]) == ("monthly", 1, "2026-02-13")
        assert len(d["unit_keys"]) == 60 and ["T000", "2026-02-01", "2026-01-01"] in d["unit_keys"]
        for side in LG.SIDES:
            assert {"observed", "p", "alpha_each", "decision", "n_flagged", "n_normal"} <= set(d["per_side"][side])
        assert res1["test"] == d["per_side"] and res1["frozen"]["applies"] is True

        later = _later_units_that_would_move_the_test()
        _persist(later, tmp_path)
        recomputed = LG.assess("monthly", rows=first_rows + later)["test"]
        assert recomputed["put"]["observed"] != res1["test"]["put"]["observed"], "夹具没咬住：新数据不改结果"

        res2 = LG.assess("monthly", state_dir=tmp_path)
        res3 = LG.assess("monthly", state_dir=tmp_path, as_of="2026-04-30")
        assert res2["progress"]["n_independent"] == 100, "新数据确实进了账本"
        for r in (res2, res3):
            assert json.dumps(r["test"], sort_keys=True) == json.dumps(res1["test"], sort_keys=True)
            assert r["frozen"]["ready_date"] == "2026-02-13"
        assert path.read_bytes() == frozen_bytes

    def test_injected_rows_neither_read_nor_write_the_frozen_file(self, tmp_path):
        """`rows=` 注入（测试 / 探针）：不写冻结文件、也不读已有的那份（现算）。
        变异：注入路径也冻结（探针在生产目录留下一份「检验结果」）/ 注入路径读冻结结果。"""
        res = LG.assess("monthly", rows=_ready_ledger_rows(), state_dir=tmp_path)
        assert res["status"] == "ready" and "test" in res and res["frozen"] is None
        assert not LG.prereg_result_path("monthly", tmp_path).exists()
        _persist(_ready_ledger_rows(), tmp_path)
        frozen = LG.assess("monthly", state_dir=tmp_path, freeze=True)["test"]
        live = LG.assess("monthly", rows=_ready_ledger_rows() + _later_units_that_would_move_the_test(),
                         state_dir=tmp_path)["test"]
        assert live["put"]["observed"] != frozen["put"]["observed"]

    def test_as_of_before_ready_date_gives_no_test_and_does_not_recompute(self, tmp_path):
        """冻结日（as_of=None ⇒ 账本最晚已知日期）之前的 as_of，即使当日数据也够闸：不给 test、不重算、不重写。
        变异：`elif not from_ledger` 写成 `else`（在更早的样本上再跑一次检验）。"""
        _persist(_ready_ledger_rows() + _later_units_that_would_move_the_test(), tmp_path)
        res = LG.assess("monthly", state_dir=tmp_path, freeze=True)
        assert res["frozen"]["ready_date"] == "2026-04-11"
        before = LG.prereg_result_path("monthly", tmp_path).read_bytes()
        early = LG.assess("monthly", state_dir=tmp_path, as_of="2026-02-20")
        assert early["status"] == "ready" and early["frozen"]["applies"] is False
        assert not (set(_walk_keys(early)) & LG.BLINDED_KEYS), "冻结日之前不该吐出检验结果"
        assert "冻结" in R._assess_line(early)
        assert LG.prereg_result_path("monthly", tmp_path).read_bytes() == before

    def test_unreadable_frozen_file_raises_instead_of_rerunning(self, tmp_path):
        """冻结文件读不懂 / 协议版本对不上 ⇒ 抛（报告写出原因），不当成「没有」而重跑第二次。
        变异：解析失败返回 None。"""
        _persist(_ready_ledger_rows(), tmp_path)
        p = LG.prereg_result_path("monthly", tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{truncated", encoding="utf-8")
        with pytest.raises(RuntimeError, match="无法解析"):
            LG.assess("monthly", state_dir=tmp_path, freeze=True)
        p.write_text(json.dumps({"tenor": "monthly", "prereg_version": 99}), encoding="utf-8")
        with pytest.raises(RuntimeError, match="prereg_version"):
            LG.assess("monthly", state_dir=tmp_path)
        assert R._assess_safe("monthly", tmp_path, None)["status"] == "error"


class TestAssessAsOfUsesOnlyWhatWasKnown:
    """F2-2：assess(as_of) 不用 as_of 之后才有的结算 / 放弃。"""

    def test_future_settlement_and_give_up_count_as_pending(self):
        """变异：`_as_known_on` 原样返回行（as_of 只过滤记录日）。"""
        s = _row("2026-10-01", "AAA", "2026-10-30")
        s["settled_on"] = "2026-10-31"
        g = _row("2026-10-01", "BBB", "2026-10-30", settle_status="give_up")
        g.update(settle_give_up_reason="max_attempts_exhausted", settle_give_up_on="2026-11-10")
        early = LG.assess("monthly", rows=[s, g], as_of="2026-10-05")["progress"]
        assert (early["n_settled"], early["n_give_up"], early["n_pending"], early["n_independent"]) == (0, 0, 2, 0)
        mid = LG.assess("monthly", rows=[s, g], as_of="2026-10-31")["progress"]
        assert (mid["n_settled"], mid["n_give_up"], mid["n_pending"], mid["n_independent"]) == (1, 0, 1, 1)
        late = LG.assess("monthly", rows=[s, g], as_of="2026-11-10")["progress"]
        assert (late["n_settled"], late["n_give_up"], late["give_up_reasons"]) == \
            (1, 1, {"max_attempts_exhausted": 1})
        assert s["expiry_close"] == 100.0, "判定在副本上做，账本行本身不许被清"

    def test_outcome_without_a_date_counts_as_pending_under_as_of(self):
        """结算 / 放弃日期缺失 ⇒ 判不了「as_of 那天是否已知」⇒ 按 pending（保守）；as_of=None 时照原状态。
        变异：日期缺失当成已知。"""
        s = _row("2026-10-01", "AAA", "2026-10-30")
        s["settled_on"] = None
        g = _row("2026-10-01", "BBB", "2026-10-30", settle_status="give_up")
        g["settle_give_up_reason"] = "max_attempts_exhausted"      # settle_give_up_on 缺省 None
        pg = LG.assess("monthly", rows=[s, g], as_of="2026-12-31")["progress"]
        assert (pg["n_settled"], pg["n_give_up"], pg["n_pending"], pg["n_independent"]) == (0, 0, 2, 0)
        pg = LG.assess("monthly", rows=[s, g])["progress"]
        assert (pg["n_settled"], pg["n_give_up"], pg["n_pending"]) == (1, 1, 0)

    def test_same_as_of_same_answer_before_and_after_a_later_settle(self, tmp_path):
        """评审探针原样：08-03 记、09-02 到期；09-23 结算前后各判一次 assess(as_of=09-01)，结果必须相同；
        放弃同理（记 settle_give_up_on）。变异：同上 / `_give_up` 不记日期。"""
        _pending(tmp_path, date="2026-08-03", expiry="2026-09-02")
        _pending(tmp_path, date="2026-08-03", expiry="2026-09-03", ticker="BBB")
        before = LG.assess("monthly", state_dir=tmp_path, as_of="2026-09-01")["progress"]
        # BBB：K 线越过了到期日（09-04）却没有 09-03 那根 ⇒ 每天计一次失败（同一天只计一次），第 5 天放弃
        no_expiry_bar = [{"date": "2026-09-01", "close": 1.0}, {"date": "2026-09-04", "close": 1.0}]
        LG.settle("2026-09-20", "monthly", state_dir=tmp_path,
                  bars_fn=lambda t: [{"date": "2026-09-02", "close": 95.0}] if t == "AAA" else no_expiry_bar)
        for i in range(LG.CONFIG["settle_max_attempts"] - 1):
            LG.settle(f"2026-09-{21 + i}", "monthly", state_dir=tmp_path, bars_fn=lambda t: no_expiry_bar)
        rows = {r["ticker"]: r for r in LG.load_rows("monthly", state_dir=tmp_path)}
        assert (rows["AAA"]["settle_status"], rows["BBB"]["settle_status"]) == ("settled", "give_up")
        assert rows["BBB"]["settle_give_up_on"] == "2026-09-24"
        after = LG.assess("monthly", state_dir=tmp_path, as_of="2026-09-01")["progress"]
        assert after == before and before["n_settled"] == 0 and before["n_pending"] == 2
        now = LG.assess("monthly", state_dir=tmp_path, as_of="2026-09-24")["progress"]
        assert (now["n_settled"], now["n_give_up"]) == (1, 1)


class TestEarningsExcludedPerUnit:
    """F2-5：财报按 (ticker, expiry) 单位排除——财报当天被误标 none 的行不得被「取最早合格行」选中。"""

    def test_earnings_day_row_mislabelled_none_is_excluded_with_its_unit(self):
        """AAA：09-10/11 before_expiry、09-12（财报当天，ChronosBee 丢成 −1）误标 none ⇒ 整单位排除；
        DDD：唯一已结算行是 none，但同单位一条**未结算**的行是 unknown ⇒ 同样整单位排除（不论结算与否）。
        BBB（全 none）、CCC（after_expiry）照常入选。变异：只看单行状态（按行排除）。"""
        rows = [_row("2026-09-10", "AAA", earnings_status="before_expiry"),
                _row("2026-09-11", "AAA", earnings_status="before_expiry"),
                _row("2026-09-12", "AAA", earnings_status="none"),
                _row("2026-09-10", "BBB"), _row("2026-09-11", "BBB"),
                _row("2026-09-10", "CCC", earnings_status="after_expiry"),
                _row("2026-09-10", "DDD"),
                _row("2026-09-11", "DDD", earnings_status="unknown", settle_status="pending")]
        units = LG.independent_units(rows)
        assert {(u["ticker"], u["date"]) for u in units} == {("BBB", "2026-09-10"), ("CCC", "2026-09-10")}
        prog = LG.assess("monthly", rows=rows)["progress"]
        assert prog["n_independent"] == 2 and prog["n_units_earnings_excluded"] == 2

    def test_other_expiry_of_the_same_ticker_is_not_tainted(self):
        """排除只到 (ticker, expiry)：同票另一个到期日（财报 09-15 在它首条记录 09-20 之前就过了）不连坐。
        变异：按票整票排除（不看财报日落不落在单位存续期内）。"""
        rows = [_row("2026-09-10", "AAA", "2026-10-23", earnings_status="before_expiry",
                     earnings_date="2026-09-15"),
                _row("2026-09-20", "AAA", "2026-11-20", earnings_status="none")]
        assert [(u["ticker"], u["expiry"]) for u in LG.independent_units(rows)] == [("AAA", "2026-11-20")]


class TestRowDiagnostics:
    """F2-4：取数层与扫描的诊断数落进账本行（事后能查、能剔），缺失记 None 不记 0。"""

    def test_row_carries_fetch_counts_payload_time_and_sweep_counts(self, tmp_path):
        """变异：`_fill_raw` 不拷 fetch_counts / payload_last_trade_time / iv30 / session_live；
        env 不记 zg_n_contracts / zg_excluded_no_iv / zg_excluded_no_oi。"""
        raw = _raw("AAA", AS_OF)
        raw.update(payload_last_trade_time="2026-09-23T16:15:02", iv30=32.8, session_live=False,
                   n_dropped_unparseable=3, n_expired_excluded=5, n_expiring_today_excluded=7)
        for c in raw["contracts"][:9]:
            c["iv"] = None
        for c in raw["contracts"][9:13]:      # 与无 IV 的 9 张错开、数目不同：两个计数不许互换 / 写死 0
            c["oi"] = 0.0
        LG.run_for_date(AS_OF, tickers=["AAA"], fetch_fn=lambda t, as_of: (raw, None),
                        upcoming_fn=_no_earnings, bars_fn=lambda t: None, state_dir=tmp_path)
        zg = L.level_map(raw["contracts"], raw["underlying_price"])["views"]["le_45dte"]["zero_gamma"]
        for tenor in LG.TENORS:
            row = LG.rows_for_date(AS_OF, tenor, tmp_path)[0]
            assert row["status"] == "recorded"
            assert row["fetch_counts"] == {"n_raw": len(raw["contracts"]), "n_dropped_unparseable": 3,
                                           "n_expired_excluded": 5, "n_expiring_today_excluded": 7}
            assert (row["payload_last_trade_time"], row["iv30"], row["session_live"]) == \
                ("2026-09-23T16:15:02", 32.8, False)
            env = row["env"]
            assert (env["zg_n_contracts"], env["zg_excluded_no_iv"], env["zg_excluded_no_oi"]) == \
                (zg["n_contracts"], zg["excluded_no_iv"], zg["excluded_no_oi"])
            assert env["zg_n_contracts"] == len(raw["contracts"]) - 13
            assert (env["zg_excluded_no_iv"], env["zg_excluded_no_oi"]) == (9, 4)

    def test_unavailable_without_raw_keeps_diagnostics_as_none(self):
        """取数失败（没有 raw）⇒ fetch_counts / payload 时间都是 None（没测到），不是 0（测过为零）。
        取数有结果但缺键 ⇒ 该键 None。变异：缺失补 0。"""
        row = LG.unavailable_row(AS_OF, "AAA", "monthly", "stale_vintage")
        assert row["fetch_counts"] is None and row["payload_last_trade_time"] is None and row["iv30"] is None
        partial = LG.unavailable_row(AS_OF, "AAA", "monthly", "x", {"n_raw": 10, "session_live": "yes"})
        assert partial["fetch_counts"] == {"n_raw": 10, "n_dropped_unparseable": None,
                                           "n_expired_excluded": None, "n_expiring_today_excluded": None}
        assert partial["session_live"] is None, "非布尔的 session_live 不许原样落账"


class TestRunForDateObservability:
    """F2-7（账本侧）：level_map 抛异常不记 ok；过期待结算要响、要显示。"""

    def test_levels_error_is_not_counted_as_ok(self, tmp_path, monkeypatch):
        """评审探针：level_map 全挂时 fetch_reasons 曾是 {'ok': 2}。变异：取数成功就先记 ok。"""
        monkeypatch.setattr(L, "level_map", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("numpy")))
        res = LG.run_for_date(AS_OF, tickers=["AAA", "BBB"], upcoming_fn=_no_earnings,
                              fetch_fn=lambda t, as_of: (_raw(t, as_of), None),
                              bars_fn=lambda t: None, state_dir=tmp_path)
        assert res["fetch_reasons"] == {"levels_error:RuntimeError": 2}
        for tenor in LG.TENORS:
            assert res["per_tenor"][tenor]["unavailable"] == {"levels_error:RuntimeError": 2}

    def test_overdue_pending_is_warned_counted_and_shown(self, tmp_path, caplog):
        """到期 6 天仍 pending（取不到 K 线）⇒ run_for_date 打 warning、per_tenor 计数、summary_line 与本地报告显示；
        恰好 5 天的不算过期（> 不是 ≥）。变异：删 warning / summary_line 不写待结算 / 过期判据写成 ≥。"""
        _pending(tmp_path, date="2026-09-01", expiry="2026-09-17")                 # 6 天 ⇒ 过期
        _pending(tmp_path, date="2026-09-01", expiry="2026-09-18", ticker="BBB")   # 5 天 ⇒ 不算
        with caplog.at_level(logging.WARNING):
            res = LG.run_for_date(AS_OF, tickers=[], upcoming_fn=_no_earnings,
                                  fetch_fn=lambda t, as_of: pytest.fail("零只票不该取链"),
                                  bars_fn=lambda t: None, state_dir=tmp_path)
        m = res["per_tenor"]["monthly"]
        assert (m["pending"], m["pending_overdue"], m["settle_bars_unavailable"], m["settled"]) == (2, 1, 2, 0)
        assert res["per_tenor"]["weekly"]["pending_overdue"] == 0
        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("到期已超过 5 个日历日仍未结算" in w and "monthly" in w for w in warns), warns
        a = LG.assess("monthly", state_dir=tmp_path, as_of=AS_OF)
        assert (a["progress"]["n_pending"], a["progress"]["n_pending_overdue"]) == (2, 1)
        line = LG.summary_line(a)
        assert "待结算 2 行（其中已过期超过 5 个日历日 1 行）" in line, line
        assert "待结算 2 行（其中已过期超过 5 个日历日 1 行）" in R.render_markdown(AS_OF, state_dir=tmp_path)

    def test_no_overdue_no_warning(self, tmp_path, caplog):
        """反向对照：没有过期待结算就不响（不然 warning 天天响、没人看）。变异：无条件打 warning。"""
        _pending(tmp_path, date="2026-09-01", expiry="2026-09-18")
        with caplog.at_level(logging.WARNING):
            LG.run_for_date(AS_OF, tickers=[], upcoming_fn=_no_earnings, fetch_fn=None,
                            bars_fn=lambda t: None, state_dir=tmp_path)
        assert not [r for r in caplog.records if "仍未结算" in r.getMessage()]


class TestMcpBlinding:
    """F2-6：MCP 按日期读账本，未就绪时行视图剔除结算字段（route flag + 梯子 + 到期收盘 = 效应量）。"""

    def test_settlement_fields_hidden_until_ready(self, tmp_path):
        """变异：`_row_view` 的 keep 里留着 expiry_close / 不看 blind / `_unblinded` 恒真。"""
        rows = _ready_ledger_rows(40, 12, 10)
        _persist(rows, tmp_path)
        got = R.rows_for_ticker("2026-01-01", "T000", state_dir=tmp_path)
        assert got["assess"]["monthly"]["status"] == "accruing"
        view = got["tenors"]["monthly"]
        assert view["settlement_blinded"] is True and view["settle_status"] == "settled"
        leaked = set(view) & set(R.BLINDED_ROW_FIELDS)
        assert not leaked, f"未就绪却返回了结算字段 {leaked}"
        assert view["route"]["flag_put"] is True and view["ladder"]["put"]["0.20"]["short"]["bid"] == 1.0, \
            "盲化的是结果，不是路由与梯子（它们本身没有方向）"
        assert "expiry_close" not in json.dumps(got)

    def test_settlement_fields_returned_once_frozen(self, tmp_path):
        """冻结（日报钩子那条路径）后解盲：同一行返回到期收盘。**两个 tenor 都冻结**才解盲（跨 tenor 泄露，
        见 TestCrossTenorUnblinding）。变异：永远盲化（检验跑完了还看不到结果）。"""
        _persist(_ready_ledger_rows(), tmp_path)
        _persist(_ready_ledger_rows(tenor="weekly"), tmp_path, "weekly")
        R.write_local_report(AS_OF, state_dir=tmp_path, freeze=True)
        got = R.rows_for_ticker("2026-01-01", "T000", state_dir=tmp_path)
        assert got["assess"]["monthly"]["status"] == "ready" and got["assess"]["monthly"]["frozen"]
        view = got["tenors"]["monthly"]
        assert "settlement_blinded" not in view
        assert view["expiry_close"] == _put_close(-0.5) and view["settled_on"] == "2026-02-02"

    def test_assess_error_counts_as_not_ready(self, tmp_path):
        """assess 判定失败（冻结文件坏了）⇒ 按未就绪盲化，不按「判不了就放行」。变异：error 视为解盲。"""
        _persist(_ready_ledger_rows(), tmp_path)
        p = LG.prereg_result_path("monthly", tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json", encoding="utf-8")
        got = R.rows_for_ticker("2026-01-01", "T000", state_dir=tmp_path)
        assert got["assess"]["monthly"]["status"] == "error"
        assert got["tenors"]["monthly"]["settlement_blinded"] is True


class TestLiveFreshPayload:
    """F2-8：MCP 现算先清该票的进程内 payload 缓存（常驻进程的 4h 缓存会把旧报价当现价），并带出报价时刻。"""

    def test_default_path_invalidates_before_fetching(self, tmp_path, monkeypatch):
        """变异：不清缓存 / 取链之后才清 / 清全部票（传 None）。"""
        import cboe_options
        calls = []
        monkeypatch.setattr(cboe_options, "invalidate_payload_cache", lambda t=None: calls.append(("drop", t)))

        def fake(t, *, as_of, **_kw):
            calls.append(("fetch", t, as_of))
            raw = _raw(t, AS_OF)
            raw.update(payload_last_trade_time="2026-09-23T13:34:58", session_live=True, iv30=31.0)
            return raw, None
        monkeypatch.setattr(cboe_options, "fetch_cboe_raw_contracts", fake)
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
        out = R.compute_live("nvda")
        assert calls == [("drop", "NVDA"), ("fetch", "NVDA", None)]
        assert out["data_available"] is True
        assert (out["payload_last_trade_time"], out["session_live"], out["iv30"]) == \
            ("2026-09-23T13:34:58", True, 31.0)
        assert out["tenors"]["monthly"]["payload_last_trade_time"] == "2026-09-23T13:34:58"
        assert "settlement_blinded" not in out["tenors"]["monthly"], "现算行没有可盲的结算字段"

    def test_injected_fetch_does_not_touch_the_cache(self, monkeypatch):
        """注入的 fetch_fn 不走进程缓存，不清（别替调用方做决定）。变异：无条件清。"""
        import cboe_options
        monkeypatch.setattr(cboe_options, "invalidate_payload_cache",
                            lambda t=None: pytest.fail("注入取数时不该清缓存"))
        assert R.compute_live("AAA", fetch_fn=lambda t, as_of: (_raw(t, AS_OF), None))["data_available"]


class TestYieldPresentation:
    """N2：单笔风险回报 yield_raw（= credit / 占用资金；价差 / 铁鹰即 credit / max_loss）是主数字，
    年化是次要且注明「单利年化，价差类会显得很大」。夹具 0.20 档 short put：K=90、bid=1.0、30 DTE
    ⇒ 单笔 1/90 = 1.11%，年化 ×365/30 = 13.5%。"""

    def _md(self, tmp_path):
        LG.record_rows(AS_OF, "monthly", [_row(AS_OF, "AAA", settle_status="pending")], state_dir=tmp_path)
        return R.render_markdown(AS_OF, state_dir=tmp_path).splitlines()

    def test_structure_table_leads_with_per_trade_return(self, tmp_path):
        """变异：结构表只给年化（删单笔回报列）/ 年化排在单笔回报前 / 单笔回报列填的是年化。"""
        lines = self._md(tmp_path)
        head = next(x for x in lines if x.startswith("| 结构 |"))
        cols = [c.strip() for c in head.strip("|").split("|")]
        assert cols[1:5] == ["route 档 credit", "max_loss", "单笔回报", "年化（单利）"], cols
        sp = next(x for x in lines if x.startswith("| 卖 put |"))
        cells = [c.strip() for c in sp.strip("|").split("|")]
        assert cells[1:5] == ["1.00", "89.00", "1.11%", "13.5%"], cells

    def test_ladder_table_and_note(self, tmp_path):
        """变异：梯子表只写年化 / 删掉收益口径说明（或说明里不提单利年化）。"""
        lines = self._md(tmp_path)
        assert f"**收益口径**：{R.YIELD_NOTE}" in lines
        assert "单利年化" in R.YIELD_NOTE and "yield_raw = credit / 占用资金" in R.YIELD_NOTE
        assert "单笔回报（年化）" in next(x for x in lines if x.startswith("| 档位 |"))
        assert "1.11%（年化 13.5%）" in next(x for x in lines if x.startswith("| 0.20 |"))

    def test_mcp_views_carry_the_note_and_per_trade_return(self, tmp_path, monkeypatch):
        """变异：MCP（读账本 / 现算）输出不带 yield_note / 结构摘要丢 yield_raw。"""
        LG.record_rows(AS_OF, "weekly", [_row(AS_OF, "AAA", tenor="weekly", settle_status="pending")],
                       state_dir=tmp_path)
        got = R.rows_for_ticker(AS_OF, "AAA", state_dir=tmp_path)
        assert got["yield_note"] == R.YIELD_NOTE
        assert got["tenors"]["weekly"]["structures"]["base"]["short_put"]["yield_raw"] == 0.011111
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "home"))
        live = R.compute_live("AAA", fetch_fn=lambda t, as_of: (_raw(t, AS_OF), None))
        assert live["data_available"] and live["yield_note"] == R.YIELD_NOTE


# ═════════════════════════════════════════ 登记前定稿（续，2026-09-24 最终评审）：R1 ~ R7
# 每条对应 reviews_final.md 里一条探针实测过的缺陷；docstring 写让它变红的变异。

X_AMC = dt.date(2026, 10, 13)      # 周二盘后财报


def _chronos_info(day: dt.date) -> dict:
    """ChronosBee 的真实形状：days_until = (财报日 00:00 − 扫描时刻 14:00).days，< 0 就丢 ⇒ 财报当天「无财报」。"""
    du = (dt.datetime.combine(X_AMC, dt.time(0, 0)) - dt.datetime.combine(day, dt.time(14, 0))).days
    if du >= 0:
        return {"earnings_date": X_AMC.isoformat(), "source": "chronos_bee_catalyst"}
    return {"earnings_date": None, "source": "chronos_bee_no_earnings"}


def _chronos_rows(tenor: str, ticker="XYZ") -> list:
    """评审探针 probe_earn.py 的场景：每个工作日按真实 `select_expiry` 选到期日、按 ChronosBee 逻辑定财报。"""
    fridays = [dt.date(2026, 10, 9) + dt.timedelta(days=7 * i) for i in range(8)]
    rows, day = [], dt.date(2026, 9, 28)
    while day <= dt.date(2026, 10, 23):
        if day.weekday() < 5:
            contracts = [{"expiry": e.isoformat(), "dte": (e - day).days} for e in fridays if e >= day]
            sel = C.select_expiry(contracts, tenor)
            if sel:
                info = _chronos_info(day)
                rows.append(_row(day.isoformat(), ticker, sel[0], tenor=tenor,
                                 earnings_status=C.classify_earnings(info, day.isoformat(), sel[0]),
                                 earnings_date=info["earnings_date"]))
        day += dt.timedelta(days=1)
    return rows


class TestEarningsTaintedByTicker:
    """R1（major）：单位在财报当天**才第一次**被选中时，它唯一的行误标 none、没有更早的 before_expiry 行可连坐。
    按票补一刀：该票首条记录日当时已知的财报日 d 落在 [首条记录日, expiry] ⇒ 整单位排除。"""

    def test_unit_first_selected_on_the_amc_earnings_day_is_excluded(self):
        """评审探针原样（真 select_expiry / classify_earnings）：周度周二换到 10-30，恰逢周二盘后财报。
        正对照：那个单位确实只有 none 行（单位级规则单独抓不住它）。变异：删掉按票那一刀。"""
        rows = _chronos_rows("weekly")
        unit = [r for r in rows if (r["ticker"], r["expiry"]) == ("XYZ", "2026-10-30")]
        assert unit and unit[0]["date"] == X_AMC.isoformat(), "夹具没咬住：10-30 单位不是财报当天首次选中"
        assert {r["earnings_status"] for r in unit} == {"none"}, "夹具没咬住：单位级规则自己就能排除它"
        units = LG.independent_units(rows)
        straddle = [(u["date"], u["expiry"]) for u in units if u["date"] <= X_AMC.isoformat() <= u["expiry"]]
        assert not straddle, f"跨财报的单位进了检验：{straddle}"
        assert ("XYZ", "2026-10-30") not in {(u["ticker"], u["expiry"]) for u in units}

    def test_monthly_unit_is_tainted_by_the_weekly_row_through_assess(self, tmp_path):
        """财报日只出现在**另一个 tenor** 的行里（月度单位在 X 首次选中、之前没有月度行）：assess 从账本读
        必须把两个 tenor 都当财报来源。正对照：只给月度行时该单位入选。变异：assess 不读另一个 tenor。"""
        m = _row(X_AMC.isoformat(), "XYZ", "2026-11-20", earnings_status="none")
        w = _row("2026-10-12", "XYZ", "2026-10-23", tenor="weekly", earnings_status="before_expiry",
                 earnings_date=X_AMC.isoformat())
        assert [u["expiry"] for u in LG.independent_units([m])] == ["2026-11-20"]
        _persist([m], tmp_path, "monthly")
        _persist([w], tmp_path, "weekly")
        prog = LG.assess("monthly", state_dir=tmp_path)["progress"]
        assert (prog["n_independent"], prog["n_units_earnings_excluded"]) == (0, 1)
        assert LG.assess("monthly", state_dir=tmp_path, as_of="2026-10-11")["progress"]["n_rows"] == 0

    def test_only_dates_known_by_the_first_row_inside_the_unit_life_of_the_same_ticker(self):
        """五只票各一个 (票, 2026-10-30) 单位，首条记录都是 10-13、都标 none，只差「别的行给了什么财报日」：
          HIT   10-12 的行给 10-13            ⇒ 排除（正对照）
          LATE  10-14 的行给 10-20（首条记录之后才出现）⇒ 保留（防后视）
          PAST  10-05 的行给 10-12（首条记录之前已过）  ⇒ 保留
          AFTER 10-12 的行给 11-05（到期之后）          ⇒ 保留
          OTHER 自己没有财报行（HIT 的财报日不串票）   ⇒ 保留
        变异：去掉 `rd <= d0` / `d0 <= ed` / `ed <= exp` 任一条件，或 D 不按票分。"""
        rows = [_row("2026-10-13", t, "2026-10-30") for t in ("HIT", "LATE", "PAST", "AFTER", "OTHER")]
        rows += [_row("2026-10-12", "HIT", "2026-10-23", earnings_status="before_expiry",
                      earnings_date="2026-10-13"),
                 _row("2026-10-14", "LATE", "2026-11-20", earnings_status="before_expiry",
                      earnings_date="2026-10-20"),
                 _row("2026-10-05", "PAST", "2026-10-16", earnings_status="before_expiry",
                      earnings_date="2026-10-12"),
                 _row("2026-10-12", "AFTER", "2026-11-20", earnings_status="before_expiry",
                      earnings_date="2026-11-05")]
        got = {(u["ticker"], u["expiry"]) for u in LG.independent_units(rows)}
        assert got == {("LATE", "2026-10-30"), ("PAST", "2026-10-30"), ("AFTER", "2026-10-30"),
                       ("OTHER", "2026-10-30")}


def _stressed_weeks_rows():
    """评审探针 probe_gate.py：12 个周度块 × 10 只票；第 3、8 周全市场负 gamma（全员 flag），其余无人被 flag。"""
    rows = []
    base = dt.date(2026, 10, 6)
    for w in range(12):
        d = (base + dt.timedelta(days=7 * w)).isoformat()
        exp = (base + dt.timedelta(days=7 * w + 17)).isoformat()
        f = w in (3, 8)
        rows += [_row(d, f"T{i:02d}", exp, tenor="weekly", flag_put=f, flag_call=f,
                      close=_put_close(-0.5 if f else 0.8)) for i in range(10)]
    return rows


class TestGateCountsInformativeBlocks:
    """R3：就绪闸的 flag / normal 只数信息块（同一记录日两种标签都有）里的单位——置换只从那里取信息。"""

    def test_all_flags_in_uninformative_blocks_is_not_ready(self):
        """探针原样：旧口径三道闸全过（120 单位 / 12 到期日 / flag 20 normal 100），信息块 0，p 恒 1。
        新口径 ⇒ accruing，不跑检验。变异：闸门数全体 n_flagged / n_normal。"""
        res = LG.assess("weekly", rows=_stressed_weeks_rows())
        ps = res["progress"]["per_side"]["put"]
        assert (res["progress"]["n_independent"], res["progress"]["n_distinct_expiries"]) == (120, 12)
        assert (ps["n_flagged"], ps["n_normal"], ps["n_informative_blocks"]) == (20, 100, 0)
        assert (ps["n_flagged_informative"], ps["n_normal_informative"]) == (0, 0)
        assert res["progress"]["n_informative_blocks"] == {"put": 0, "call": 0}
        assert res["gates"]["independent_units"] and res["gates"]["distinct_expiries"]
        assert not res["gates"]["put_groups"] and not res["gates"]["call_groups"]
        assert res["status"] == "accruing" and not (set(_walk_keys(res)) & LG.BLINDED_KEYS)
        assert "信息块 0" in LG.summary_line(res)

    def test_one_flag_moved_out_of_an_informative_block_drops_below_the_gate(self):
        """`_ready_rows` 恰好够闸（10 个 flag 各在一个信息块里）。把第 0 块的 4 个 normal 挪到第 11 块
        （单位数、到期日数、全体 flag 数都不变）⇒ 第 0 块只剩 1 个 flag、不再是信息块 ⇒ 信息块内 flag 9 < 10。
        变异：同上。"""
        rows = _ready_rows()
        ok = LG.assess("monthly", rows=rows)
        assert ok["status"] == "ready", ok["gates"]
        assert (ok["progress"]["per_side"]["put"]["n_flagged_informative"],
                ok["progress"]["per_side"]["put"]["n_informative_blocks"]) == (10, 10)
        assert ok["progress"]["per_side"]["put"]["n_informative_blocks"] == \
            ok["test"]["put"]["n_informative_blocks"], "闸门与检验的信息块定义分叉了"
        day0, day11 = rows[0]["date"], rows[11]["date"]
        moved = [dict(r, date=day11) if (r["date"] == day0 and not r["route"]["flag_put"]) else r for r in rows]
        res = LG.assess("monthly", rows=moved)
        ps = res["progress"]["per_side"]["put"]
        assert (res["progress"]["n_independent"], ps["n_flagged"]) == (60, 10)
        assert (ps["n_flagged_informative"], ps["n_informative_blocks"]) == (9, 9)
        assert res["gates"]["independent_units"] and res["gates"]["distinct_expiries"]
        assert not res["gates"]["put_groups"] and res["status"] == "accruing"

    def test_scope_constant_is_load_bearing(self, monkeypatch):
        """`PREREG["per_group_scope"]` 改成代码不认的口径 ⇒ KeyError，不静默退回全体计数。"""
        monkeypatch.setitem(LG.PREREG, "per_group_scope", "all_units")
        with pytest.raises(KeyError):
            LG.assess("monthly", rows=_ready_rows())


class TestFutureDatesRejected:
    """R4：写路径拒绝晚于 PDT 今天的日期；冻结的 ready_date = min(as_of, 数据视界)。"""

    def test_write_paths_reject_future_dates_and_write_nothing(self, tmp_path, monkeypatch):
        """变异：run_for_date / settle / assess(freeze=True) 任一处删掉日期闸。"""
        monkeypatch.setattr(LG, "_today", lambda: "2026-09-24")
        with pytest.raises(ValueError, match="晚于今天"):
            LG.run_for_date("2026-09-25", tickers=["AAA"], upcoming_fn=_no_earnings,
                            fetch_fn=lambda t, as_of: (_raw(t, as_of), None),
                            bars_fn=lambda t: None, state_dir=tmp_path)
        assert not any(tmp_path.rglob("*.jsonl")), "拒绝了却写了账本"
        _pending(tmp_path, date="2026-09-01", expiry="2026-09-18")
        with pytest.raises(ValueError, match="晚于今天"):
            LG.settle("2026-09-25", "monthly", state_dir=tmp_path,
                      bars_fn=lambda t: [{"date": "2026-09-18", "close": 99.0}])
        assert LG.load_rows("monthly", tmp_path)[0]["settle_status"] == "pending"
        assert LG.settle("2026-09-24", "monthly", state_dir=tmp_path,
                         bars_fn=lambda t: [{"date": "2026-09-18", "close": 99.0}]) == 1, "今天本身要放行"
        with pytest.raises(ValueError, match="晚于今天"):
            LG.assess("monthly", state_dir=tmp_path, as_of="2026-09-25", freeze=True)
        assert LG.assess("monthly", state_dir=tmp_path, as_of="2026-09-25")["status"] == "accruing", \
            "只读的 assess 不拦未来日期"

    def test_cli_rejects_a_future_date_before_doing_anything(self, tmp_path, capsys, monkeypatch):
        """评审探针原样：`--assess --date 2027-12-31`（手误）。退出码 1、stderr 说明、不冻结。变异：CLI 不拦。"""
        monkeypatch.setattr(LG, "_today", lambda: "2027-01-25")
        _persist(_ready_ledger_rows(), tmp_path)
        assert LG.main(["--assess", "--date", "2027-12-31", "--state-dir", str(tmp_path)]) == 1
        assert "晚于今天" in capsys.readouterr().err
        assert not LG.prereg_result_path("monthly", tmp_path).exists()

    def test_frozen_ready_date_is_capped_at_the_data_horizon(self, tmp_path):
        """as_of 给得比数据晚（但不晚于今天）⇒ ready_date = 数据视界，此后 as_of ≥ 视界的判定都拿到检验。
        变异：ready_date = as_of（探针：ready_date 2027-12-31 ⇒ 之后一年日报都不给检验结果）。"""
        _persist(_ready_ledger_rows(), tmp_path)
        res = LG.assess("monthly", state_dir=tmp_path, as_of="2026-06-30", freeze=True)
        d = json.loads(LG.prereg_result_path("monthly", tmp_path).read_text(encoding="utf-8"))
        assert (d["ready_date"], d["data_horizon"], d["as_of"]) == ("2026-02-13", "2026-02-13", "2026-06-30")
        assert res["frozen"]["ready_date"] == "2026-02-13" and "test" in res
        later = LG.assess("monthly", state_dir=tmp_path, as_of="2026-03-01")
        assert later["frozen"]["applies"] is True and later["test"] == res["test"]

    def test_freeze_with_injected_rows_is_refused(self, tmp_path):
        """rows= 注入不读不写冻结文件——freeze=True 与它矛盾 ⇒ ValueError（不静默忽略）。"""
        with pytest.raises(ValueError, match="rows= 注入"):
            LG.assess("monthly", rows=_ready_rows(), state_dir=tmp_path, freeze=True)
        assert not LG.prereg_result_path("monthly", tmp_path).exists()


class TestStaleIntradayRows:
    """R6：收盘后读到的仍是盘中文件（cboe_stale_intraday）⇒ 照记、按来源计数、不进检验。"""

    def test_stale_rows_are_not_eligible_and_are_counted(self):
        """AAA：09-10 陈旧、09-11 正常 ⇒ 单位取 09-11；BBB 只有陈旧行 ⇒ 无单位。变异：不看报价来源。"""
        stale = cboe_options.STALE_INTRADAY_SOURCE
        assert LG.PREREG["excluded_underlying_price_sources"] == (stale, "cboe_intraday")
        rows = [_row("2026-09-10", "AAA", price_source=stale), _row("2026-09-11", "AAA"),
                _row("2026-09-10", "BBB", price_source=stale)]
        assert [(u["ticker"], u["date"]) for u in LG.independent_units(rows)] == [("AAA", "2026-09-11")]
        res = LG.assess("monthly", rows=rows)
        assert res["progress"]["price_source"] == {stale: 2, "cboe_close": 1}
        assert res["progress"]["n_rows_price_source_excluded"] == 2
        assert "非收盘后报价不进检验 2 行" in LG.summary_line(res)

    def test_same_intraday_quotes_are_excluded_whatever_the_read_time(self):
        """2026-09-26 delta 评审（探针 rv7/p6b）：同一份盘中报价，盘中读判 `cboe_intraday` + session_live=True，
        收盘后读判 `cboe_stale_intraday`——两种都不得进检验，否则进不进只取决于读取时刻。
        另：来源看似 `cboe_close` 但 session_live 为真（标签与时钟矛盾）也排除——判据是「不是收盘后快照」，
        不是某个字面量。变异：排除集合只含陈旧标签 / 不看 session_live。"""
        stale = cboe_options.STALE_INTRADAY_SOURCE
        read_intraday = _row("2026-09-10", "AAA", price_source="cboe_intraday")
        read_intraday["session_live"] = True
        read_after_close = _row("2026-09-10", "BBB", price_source=stale)
        read_after_close["session_live"] = False
        clock_says_live = _row("2026-09-10", "CCC")          # cboe_close 标签
        clock_says_live["session_live"] = True
        clean = _row("2026-09-10", "DDD")
        clean["session_live"] = False
        rows = [read_intraday, read_after_close, clock_says_live, clean]
        assert [u["ticker"] for u in LG.independent_units(rows)] == ["DDD"]
        assert LG.assess("monthly", rows=rows)["progress"]["n_rows_price_source_excluded"] == 3

    def test_run_for_date_counts_rows_by_price_source(self, tmp_path):
        """评审探针 p3：陈旧行照常 recorded，但 per_tenor 必须数得出来。变异：per_tenor 不按来源计数。"""
        stale = cboe_options.STALE_INTRADAY_SOURCE

        def fetch(t, *, as_of):
            raw = _raw(t, as_of)
            if t == "AAA":
                raw["underlying_price_source"] = stale
            return raw, None
        res = LG.run_for_date(AS_OF, tickers=["AAA", "BBB"], upcoming_fn=_no_earnings, fetch_fn=fetch,
                              bars_fn=lambda t: None, state_dir=tmp_path)
        for tenor in LG.TENORS:
            assert res["per_tenor"][tenor]["recorded"] == 2
            assert res["per_tenor"][tenor]["price_source"] == {stale: 1, "cboe_close": 1}

    def test_caveat_no_longer_claims_every_quote_is_post_close(self):
        """CAVEATS 如实写：账本行可能是收盘后读到的盘中文件（标签写明、不进检验）。变异：改回「报价取收盘后快照」。"""
        text = " ".join(R.CAVEATS)
        assert cboe_options.STALE_INTRADAY_SOURCE in text and "不进预注册检验" in text
        assert not any(c.startswith("报价取 t 日收盘后的快照") for c in R.CAVEATS)


class TestSingleFreezeWriter:
    """R7：冻结只有一个写者（日报钩子经 write_local_report(freeze=True)）；MCP / CLI 只读。"""

    def test_mcp_paths_on_a_ready_ledger_never_freeze(self, tmp_path):
        """评审探针 p4 原样：就绪但未冻结的账本上调 MCP 两条路径，冻结文件仍不存在；显示「等待日报冻结」、
        不给检验、结算字段仍省掉。变异：`_assess_safe` / `assess` 缺省 freeze=True。"""
        _persist(_ready_ledger_rows(), tmp_path)
        path = LG.prereg_result_path("monthly", tmp_path)
        got = R.rows_for_ticker("2026-01-01", "T000", state_dir=tmp_path)
        live = R.compute_live("T000", fetch_fn=lambda t, as_of: (_raw(t, AS_OF), None), state_dir=tmp_path)
        assert not path.exists(), "MCP 调用写了冻结文件"
        for brief in (got["assess"]["monthly"], live["assess"]["monthly"]):
            assert brief["status"] == "ready" and brief["awaiting_freeze"] is True and brief["frozen"] is None
            assert "等待日报冻结" in brief["summary"]
            assert not (set(_walk_keys(brief)) & LG.BLINDED_KEYS)
        assert got["tenors"]["monthly"]["settlement_blinded"] is True, "未冻结时不返回结算字段"

    def test_read_only_assess_and_cli_do_not_freeze(self, tmp_path, capsys):
        """变异：assess 缺省 freeze=True / CLI 传 freeze=True。"""
        _persist(_ready_ledger_rows(), tmp_path)
        a = LG.assess("monthly", state_dir=tmp_path)
        assert a["status"] == "ready" and a["awaiting_freeze"] is True and "test" not in a
        assert LG.main(["--assess", "--date", AS_OF, "--state-dir", str(tmp_path)]) == 0
        assert "✅ monthly：已就绪，等待日报冻结" in capsys.readouterr().out
        assert not LG.prereg_result_path("monthly", tmp_path).exists()

    def test_only_the_hook_report_path_freezes(self, tmp_path):
        """本地报告缺省不冻结；`write_local_report(freeze=True)`（日报钩子那条路径）冻结，报告里写判定。
        变异：render_markdown 不把 freeze 传给 assess。"""
        _persist(_ready_ledger_rows(), tmp_path)
        path = LG.prereg_result_path("monthly", tmp_path)
        assert "等待日报冻结" in R.render_markdown(AS_OF, state_dir=tmp_path)
        R.write_local_report(AS_OF, state_dir=tmp_path)
        assert not path.exists()
        md = R.write_local_report(AS_OF, state_dir=tmp_path, freeze=True).read_text(encoding="utf-8")
        d = json.loads(path.read_text(encoding="utf-8"))
        assert path.is_file() and d["ready_date"] == "2026-02-13"
        # 判定逐字核对冻结文件（`"reject_h0" in md` 也匹配 `fail_to_reject_h0`，两个判定都绿，测不出任何事）
        dec = {s: d["per_side"][s]["decision"] for s in LG.SIDES}
        assert set(dec.values()) <= {"reject_h0", "fail_to_reject_h0"}
        assert "预注册检验已于 2026-02-13 冻结" in md
        assert f"：put: {dec['put']} · call: {dec['call']}" in md, md


class TestBlindingClaimIsHonest:
    """R2：预注册 §7 不再宣称 MCP / 本地报告已盲化，写明「从任何出口重建结果 = 偷看」。"""

    def test_prereg_section_7_states_what_the_code_does_and_does_not_guarantee(self):
        """变异：§7 改回「不剔除就等于没盲」式的盲化宣称 / 删掉「任何出口重建 = 偷看」。"""
        text = PREREG_DOC.read_text(encoding="utf-8")
        sec7 = text[text.index("## 7."):text.index("## 8.")]
        assert "不剔除就等于没盲" not in text
        assert "**不等于**盲化" in sec7 and "从任何出口" in sec7 and "偷看" in sec7
        assert "2026-09-24 最终评审" in text[text.index("## 登记前定稿记录（续"):]


# ═════════════════════════════════════════ 登记前定稿（续，2026-09-26 二次检查）：S1 ~ S9
# 每条对应二次检查里一条复核过的缺陷；docstring 写让它变红的变异（台账见预注册文档定稿记录 #14 起）。

class TestConcurrentWriters:
    """S1：record / settle 的分片读-改-写持 tenor 目录锁；settle 取 K 线在锁外、套用时锁内重读。"""

    def test_row_recorded_during_settle_bar_fetch_survives(self, tmp_path):
        """settle 取 K 线期间（锁外）另一个写者往同一分片记了一行：settle 写回后那行必须还在。
        评审探针原样（原实现：BBB 消失）。变异：③ 不重读、在 ① 的快照上改完整片写回。"""
        _pending(tmp_path, date="2026-10-01", expiry="2026-10-16")

        def bars(_t):
            LG.record_rows("2026-10-02", "monthly",
                           [_row("2026-10-02", "BBB", "2026-10-30", settle_status="pending")], state_dir=tmp_path)
            return [{"date": "2026-10-16", "close": 99.0}]

        assert LG.settle("2026-10-19", "monthly", bars_fn=bars, state_dir=tmp_path) == 1
        got = {r["ticker"]: r for r in LG.load_rows("monthly", tmp_path)}
        assert set(got) == {"AAA", "BBB"}, f"settle 覆盖掉了并发记录的行：{sorted(got)}"
        assert (got["AAA"]["settle_status"], got["AAA"]["expiry_close"]) == ("settled", 99.0)
        assert got["BBB"]["settle_status"] == "pending"

    def test_row_settled_by_another_writer_meanwhile_is_not_overwritten(self, tmp_path):
        """取 K 线期间另一个 settle 已把同一行结算掉：本次以对方为准、计 skipped_concurrent，不改写、不重复计数。
        变异：③ 不核对「仍是 pending 且到期日未变」。"""
        _pending(tmp_path, date="2026-10-01", expiry="2026-10-16")

        def bars(_t):
            LG._settle("2026-10-19", "monthly", state_dir=tmp_path,
                       bars_fn=lambda t: [{"date": "2026-10-16", "close": 99.0}])
            return [{"date": "2026-10-16", "close": 88.0}]

        st = LG._settle("2026-10-19", "monthly", bars_fn=bars, state_dir=tmp_path)
        r = LG.load_rows("monthly", tmp_path)[0]
        assert (r["settle_status"], r["expiry_close"]) == ("settled", 99.0)
        assert (st["settled"], st["skipped_concurrent"]) == (0, 1)

    def test_record_blocks_on_the_lock_and_rereads_under_it(self, tmp_path, monkeypatch):
        """record 的读-改-写整段持锁：另一个写者持锁补结算期间，record 必须等它、再在锁内读到新版本。
        变异：`_record_rows` 不拿锁（锁外读 → 对方写 → 用旧快照覆盖 ⇒ 结算丢失）。"""
        _pending(tmp_path, date="2026-10-01", expiry="2026-10-16")
        shard = LG._shard("monthly", "2026-10-01", tmp_path)
        real_write = LG._write_shard
        other_wrote = threading.Event()

        def gated_write(path, rows, bad=()):
            # 只拦 record 线程的写：没有锁时它此刻已拿着旧快照，等「另一个写者」写完再写 ⇒ 必然覆盖
            if threading.current_thread().name == "record-thread":
                other_wrote.wait(5)
            return real_write(path, rows, bad)

        monkeypatch.setattr(LG, "_write_shard", gated_write)
        errors = []

        def rec():
            try:
                LG.record_rows("2026-10-02", "monthly",
                               [_row("2026-10-02", "BBB", "2026-10-30", settle_status="pending")], state_dir=tmp_path)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=rec, name="record-thread")
        with LG._tenor_lock("monthly", tmp_path):
            t.start()
            time.sleep(0.3)                       # 无锁时 record 线程早已读完旧分片、卡在写之前
            rows, bad = LG._load_shard(shard)
            rows[0].update(settle_status="settled", expiry_close=101.0, expiry_close_date="2026-10-16",
                           settled_on="2026-10-19")
            real_write(shard, rows, bad)          # 「另一个进程」持锁补上结算
            other_wrote.set()
        t.join(10)
        assert not t.is_alive() and not errors, errors
        got = {r["ticker"]: r for r in LG.load_rows("monthly", tmp_path)}
        assert set(got) == {"AAA", "BBB"}
        assert (got["AAA"]["settle_status"], got["AAA"]["expiry_close"]) == ("settled", 101.0), \
            "record 用旧快照覆盖了并发写入的结算"

    def test_lock_is_on_the_directory_and_leaves_no_file_behind(self, tmp_path):
        """锁的是 tenor 目录本身：不多出锁文件（私有备份只拷文本后缀，多一个文件就进 skipped 清单）。
        同进程另一个 fd 也拿不到（flock 语义）。变异：改成另建 `.lock` 文件 / 不加锁。"""
        LG.record_rows(AS_OF, "monthly", [_row(AS_OF, settle_status="pending")], state_dir=tmp_path)
        assert sorted(p.name for p in (tmp_path / "monthly").iterdir()) == ["2026-09.jsonl"]
        import fcntl
        import os
        with LG._tenor_lock("monthly", tmp_path) as d:
            fd = os.open(str(d), os.O_RDONLY)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)


class TestAssessSafeWarns:
    """S2：`_assess_safe` 把异常变成报告里的一行字之外，必须打 WARNING（冻结路径另有专门一句）。"""

    def test_corrupt_frozen_file_on_the_freeze_path_is_a_warning(self, tmp_path, caplog):
        """冻结文件坏了：日报钩子那条路径（write_local_report(freeze=True)）每天都失败——原实现日志里一个字都没有。
        变异：删掉 `_assess_safe` 里的 warning。"""
        _persist(_ready_ledger_rows(), tmp_path)
        p = LG.prereg_result_path("monthly", tmp_path)
        p.write_text("{truncated", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            md = R.write_local_report(AS_OF, state_dir=tmp_path, freeze=True).read_text(encoding="utf-8")
        assert "就绪度判定失败" in md
        warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
        hit = [r.getMessage() for r in warns if "卖权预注册检验 就绪度判定/冻结失败" in r.getMessage()]
        assert hit and "monthly" in hit[0] and "RuntimeError" in hit[0], [r.getMessage() for r in warns]

    def test_read_only_path_failure_is_a_warning_too(self, tmp_path, caplog):
        """MCP 只读路径同样要响（不写「冻结」字样）。变异：只在 freeze=True 时打。"""
        LG.prereg_result_path("weekly", tmp_path).parent.mkdir(parents=True)
        LG.prereg_result_path("weekly", tmp_path).write_text("not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert R._assess_safe("weekly", tmp_path, None)["status"] == "error"
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("就绪度判定失败" in m and "只读路径" in m and "weekly" in m for m in msgs), msgs


class TestCrossTenorUnblinding:
    """S3：结算字段只在**全部** tenor 都冻结后才返回——周度到期日 94% 同时是月度到期日，月度先冻结就返回
    月度行的 expiry_close，等于替仍在盲期的周度单位递刀。"""

    def test_one_frozen_tenor_does_not_unblind(self, tmp_path):
        """月度冻结、周度未冻结 ⇒ 两档行视图都不返回结算字段；周度也冻结后才返回。
        正对照：月度确实已冻结且适用。变异：按单个 tenor 的 `_unblinded` 决定。"""
        _persist(_ready_ledger_rows(), tmp_path)
        _persist(_ready_ledger_rows(40, 12, 10, tenor="weekly"), tmp_path, "weekly")     # 周度未就绪
        R.write_local_report(AS_OF, state_dir=tmp_path, freeze=True)
        got = R.rows_for_ticker("2026-01-01", "T000", state_dir=tmp_path)
        assert got["assess"]["monthly"]["frozen"]["applies"] is True, "正对照：月度已冻结"
        assert got["assess"]["weekly"]["status"] == "accruing"
        for tenor in LG.TENORS:
            view = got["tenors"][tenor]
            assert view["settlement_blinded"] is True, f"{tenor}：只有月度冻结就解盲了"
            assert not set(view) & set(R.BLINDED_ROW_FIELDS)
        assert "expiry_close" not in json.dumps(got)

        _persist(_ready_ledger_rows(60, 12, 10, tag="W", tenor="weekly"), tmp_path, "weekly")
        R.write_local_report(AS_OF, state_dir=tmp_path, freeze=True)
        got = R.rows_for_ticker("2026-01-01", "T000", state_dir=tmp_path)
        assert all(got["assess"][t]["frozen"]["applies"] for t in LG.TENORS)
        for tenor in LG.TENORS:
            assert "settlement_blinded" not in got["tenors"][tenor]
            assert got["tenors"][tenor]["expiry_close"] == _put_close(-0.5)

    def test_missing_tenor_assessment_counts_as_blinded(self):
        """`_all_unblinded` 缺一档（或判定失败）⇒ 不解盲。变异：只数传进来的那几档（空 / 缺档恒真）。"""
        frozen = {"status": "ready", "frozen": {"applies": True}}
        assert R._all_unblinded({t: frozen for t in LG.TENORS})
        assert not R._all_unblinded({"monthly": frozen})
        assert not R._all_unblinded({"monthly": frozen, "weekly": {"status": "error"}})


def _with_missing_put_outcomes(rows, flagged, normal):
    """把两个 flag 单位与一个 normal 单位的 0.20 put 腿改成三种不可报价：点差过宽 / 容差内无合约 / bid=0。"""
    f1, f2 = flagged
    f1["ladder"]["put"]["0.20"]["short"].update(bid=0.5, ask=1.5, spread_pct=1.0)
    f2["ladder"]["put"]["0.20"] = {"short": None, "wing": None, "reasons": ["no_delta_within_tol:nearest=0.3100"]}
    normal["ladder"]["put"]["0.20"]["short"].update(bid=0.0, quote_ok=False)
    return rows


class TestMissingnessByReasonAndFlag:
    """S4：§3 / §11 承诺「按侧记缺失原因、就绪时一并报告」，原实现只有 no_flag / no_outcome 两个总数、
    且不按 flag 分——差异缺失（flag 组缺得多）查不出来。"""

    WANT = {"no_outcome": 3, "no_outcome:short_spread_too_wide": 1,
            "no_outcome:missing_leg:no_delta_within_tol": 1, "no_outcome:quote_not_ok": 1}
    WANT_BY_FLAG = {"flag": {"no_outcome:short_spread_too_wide": 1, "no_outcome:missing_leg:no_delta_within_tol": 1},
                    "normal": {"no_outcome:quote_not_ok": 1}}

    def _rows(self, n_units, n_flag):
        rows = _ready_rows(n_units, 12, n_flag)
        flagged = [r for r in rows if r["route"]["flag_put"]]
        normal = [r for r in rows if not r["route"]["flag_put"]]
        return _with_missing_put_outcomes(rows, flagged[:2], normal[0])

    def test_counts_by_reason_and_by_flag_in_progress_and_in_the_frozen_test(self):
        """72 单位、每天一个 flag（12 个信息块），挖掉 2 个 flag + 1 个 normal 的 put 结果 ⇒ 仍就绪。
        变异：原因不带前缀 / 不按 flag 分 / 缺腿不接梯子原因 / 检验结果里不带按 flag 的缺失。"""
        res = LG.assess("monthly", rows=self._rows(72, 12))
        assert res["status"] == "ready", res["gates"]
        put = res["progress"]["per_side"]["put"]
        assert put["skipped"] == self.WANT
        assert put["skipped_by_flag"] == self.WANT_BY_FLAG
        assert res["progress"]["per_side"]["call"]["skipped_by_flag"] == {"flag": {}, "normal": {}}
        assert res["test"]["put"]["skipped_by_flag"] == self.WANT_BY_FLAG, "冻结的检验结果里要带按 flag 的缺失"
        assert (put["n_flagged"], put["n_normal"]) == (10, 59)

    def test_reported_at_readiness_not_before(self):
        """就绪时 summary_line（本地报告 / CLI / MCP 都转述它）写出按侧 × flag/normal × 原因的缺失；未就绪不写。
        变异：summary_line 不写缺失 / 未就绪也写（§3：就绪时一并报告）。"""
        line = LG.summary_line(LG.assess("monthly", rows=self._rows(72, 12)))
        assert ("0.20 档结果缺失（不进检验）：put flag 2（missing_leg:no_delta_within_tol 1、short_spread_too_wide 1）"
                "/normal 1（quote_not_ok 1） · call flag 0/normal 0") in line, line
        early = LG.assess("monthly", rows=self._rows(40, 10))
        assert early["status"] == "accruing"
        assert early["progress"]["per_side"]["put"]["skipped_by_flag"] == self.WANT_BY_FLAG, \
            "计数任何时候都在 progress 里（§7：进度计数任何时候可看）"
        assert "档结果缺失" not in LG.summary_line(early)


class TestVersionTruncation:
    """S5：§10「协议变更前记的行默认不进检验」——原实现不看任何版本戳，rule_version 2 的行会被静默池化。"""

    def test_rows_with_foreign_version_stamps_are_excluded_and_counted(self):
        """变异：`_eligible_except_earnings` 不看版本戳 / `_component_versions` 不含 prereg。"""
        ok = _row("2026-09-10", "OK")
        rule2 = _row("2026-09-10", "RULE2")
        rule2["route"]["rule_version"] = 2
        comp2 = _row("2026-09-10", "COMP2")
        comp2["component_versions"] = dict(comp2["component_versions"], route_rule=2)
        no_prereg = _row("2026-09-10", "NOPRE")           # v0.45.333 首版的戳：没有 prereg 这一项
        no_prereg["component_versions"] = {k: v for k, v in no_prereg["component_versions"].items() if k != "prereg"}
        schema2 = _row("2026-09-10", "SCHEMA2")
        schema2["schema_version"] = 2
        bare = _row("2026-09-10", "BARE")
        bare["component_versions"] = None
        rows = [ok, rule2, comp2, no_prereg, schema2, bare]
        assert [u["ticker"] for u in LG.independent_units(rows)] == ["OK"]
        pg = LG.assess("monthly", rows=rows)["progress"]
        assert pg["n_rows_version_excluded"] == 5
        assert pg["version_mismatch"] == {"route.rule_version": 1, "component_versions.route_rule": 1,
                                          "component_versions.prereg": 1, "schema_version": 1,
                                          "component_versions": 1}
        assert "版本戳与现行协议不符不进检验 5 行" in LG.summary_line(LG.assess("monthly", rows=rows))

    def test_row_level_like_the_other_filters(self):
        """按行判（同报价来源规则）：单位最早那行是旧版本 ⇒ 由同单位最早的新版本行代表。变异：按单位整体排除。"""
        old = _row("2026-09-10", "AAA")
        old["route"]["rule_version"] = 0
        new = _row("2026-09-11", "AAA")
        assert [(u["ticker"], u["date"]) for u in LG.independent_units([old, new])] == [("AAA", "2026-09-11")]

    def test_rows_written_by_current_code_carry_matching_stamps(self):
        """正对照：生产路径（build_tenor_row）写出的行版本戳全符——否则上面的过滤会把全部生产行排除。
        变异：`_blank_row` 与 `_version_mismatch` 各用一份版本表、两边不同步。"""
        raw = _raw("AAA", AS_OF)
        lm = L.level_map(raw["contracts"], raw["underlying_price"])
        for tenor in LG.TENORS:
            row = LG.build_tenor_row(raw, lm, tenor, as_of=AS_OF, earnings_info=_no_earnings("AAA"), ticker="AAA")
            assert LG._version_mismatch(row) == []
            assert row["component_versions"]["prereg"] == LG.PREREG["version"]
            assert row["route"]["rule_version"] == C.ROUTE_RULE_VERSION


class TestSettleAttemptsCountDays:
    """S6：settle_max_attempts 数的是天，不是运行次数；K 线源没追上到期日不计次。"""

    def test_same_as_of_counts_at_most_once(self, tmp_path):
        """同一 as_of 跑 5 次 ⇒ 只计 1 次、仍 pending（原实现：第 5 次永久放弃）；倒填更早的 as_of 也不计；
        换到下一天才计第 2 次。变异：删掉「同一 as_of 至多一次」。"""
        _pending(tmp_path)                        # 到期 2026-10-16
        bars = lambda t: [{"date": "2026-10-15", "close": 81.0}, {"date": "2026-10-19", "close": 99.0}]  # noqa: E731
        stats = [LG._settle("2026-10-20", "monthly", bars_fn=bars, state_dir=tmp_path) for _ in range(5)]
        r = LG.load_rows("monthly", tmp_path)[0]
        assert (r["settle_status"], r["settle_attempts"], r["settle_last_attempt_on"]) == ("pending", 1, "2026-10-20")
        assert [s["attempts_incremented"] for s in stats] == [1, 0, 0, 0, 0]
        assert [s["attempts_already_counted"] for s in stats] == [0, 1, 1, 1, 1]
        LG.settle("2026-10-19", "monthly", bars_fn=bars, state_dir=tmp_path)       # 倒填
        assert LG.load_rows("monthly", tmp_path)[0]["settle_attempts"] == 1
        LG.settle("2026-10-21", "monthly", bars_fn=bars, state_dir=tmp_path)
        r = LG.load_rows("monthly", tmp_path)[0]
        assert (r["settle_attempts"], r["settle_last_attempt_on"]) == (2, "2026-10-21")

    def test_lagging_bar_source_is_bars_unavailable_not_an_attempt(self, tmp_path):
        """K 线源最后一根不晚于到期日（源没追上）⇒ 不计次，记进 bars_unavailable / bars_lagging（过期 warning 照响）。
        连跑 settle_max_attempts 天也不放弃。变异：删掉「最后一根须晚于到期日」。"""
        _pending(tmp_path)                        # 到期 2026-10-16
        lag = lambda t: [{"date": "2026-10-14", "close": 80.0}, {"date": "2026-10-15", "close": 81.0}]  # noqa: E731
        for i in range(LG.CONFIG["settle_max_attempts"] + 1):
            st = LG._settle(f"2026-10-{20 + i}", "monthly", bars_fn=lag, state_dir=tmp_path)
            assert (st["bars_unavailable"], st["bars_lagging"], st["attempts_incremented"]) == (1, 1, 0)
        r = LG.load_rows("monthly", tmp_path)[0]
        assert (r["settle_status"], r["settle_attempts"]) == ("pending", 0)
        res = LG.run_for_date("2026-10-27", tickers=[], upcoming_fn=_no_earnings, fetch_fn=None,
                              bars_fn=lag, state_dir=tmp_path)
        m = res["per_tenor"]["monthly"]
        assert (m["settle_bars_unavailable"], m["settle_bars_lagging"], m["pending_overdue"]) == (1, 1, 1)


class TestUndecodableShardLine:
    """S7：分片里一个非 UTF-8 字节原先在 json 的 try 之外抛 UnicodeDecodeError，整个 tenor 读不了。"""

    BAD = b'{"ticker": "ZZZ", "note": "\xff\xfe broken"}'

    def test_non_utf8_line_is_a_counted_corrupt_line_preserved_byte_exactly(self, tmp_path, caplog):
        """读得了、计为坏行、打 WARNING；重写（record 同分片另一只票 + settle）后逐字节原样。
        变异：文本模式读（UnicodeDecodeError）/ 坏行按 errors="replace" 写回（字节变了）。"""
        _pending(tmp_path, date="2026-10-01", expiry="2026-10-16")
        shard = LG._shard("monthly", "2026-10-01", tmp_path)
        shard.write_bytes(shard.read_bytes() + self.BAD + b"\n" + b"{not json either\n")
        with caplog.at_level(logging.WARNING):
            LG.record_rows("2026-10-02", "monthly", [_row("2026-10-02", "BBB", "2026-10-30",
                                                          settle_status="pending")], state_dir=tmp_path)
        assert any("其中非 UTF-8 1 行" in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
        assert LG.settle("2026-10-19", "monthly", state_dir=tmp_path,
                         bars_fn=lambda t: [{"date": "2026-10-16", "close": 99.0}]) == 1
        lines = shard.read_bytes().split(b"\n")
        assert self.BAD in lines and b"{not json either" in lines, "坏行在重写后不再逐字节原样"
        assert sorted(r["ticker"] for r in LG.load_rows("monthly", tmp_path)) == ["AAA", "BBB"]
        assert LG.assess("monthly", state_dir=tmp_path)["progress"]["n_corrupt_lines"] == 2


class TestFetchBreakerAndBudget:
    """S8：run_for_date 跑在生产扫描之内、save_report / 部署之前——断网时逐票重试能把扫描顶出步骤时限。"""

    TICKERS = ["A1", "A2", "A3", "A4", "A5", "A6"]

    def _run(self, tmp_path, fetch):
        return LG.run_for_date(AS_OF, tickers=self.TICKERS, upcoming_fn=_no_earnings, fetch_fn=fetch,
                               bars_fn=lambda t: None, state_dir=tmp_path)

    def test_consecutive_network_failures_trip_the_breaker(self, tmp_path, caplog):
        """连续 3 只网络类失败（payload_unavailable / exception:*）⇒ 其余不再取、记 fetch_skipped_breaker、WARNING。
        变异：删熔断 / 熔断不数 exception:*。"""
        calls = []

        def fetch(t, *, as_of):
            calls.append(t)
            if t == "A2":
                raise ConnectionError("dns")
            return None, "payload_unavailable"

        with caplog.at_level(logging.WARNING):
            res = self._run(tmp_path, fetch)
        assert calls == ["A1", "A2", "A3"]
        assert res["fetch_reasons"] == {"payload_unavailable": 2, "exception:ConnectionError": 1,
                                        LG.FETCH_SKIPPED_BREAKER: 3}
        assert res["fetch_aborted"] == LG.FETCH_SKIPPED_BREAKER
        for tenor in LG.TENORS:
            assert res["per_tenor"][tenor]["unavailable"][LG.FETCH_SKIPPED_BREAKER] == 3
        assert any("熔断" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)

    def test_success_or_non_network_failure_resets_the_count(self, tmp_path):
        """一只取到、或非网络类失败（stale_vintage：payload 取到了，网络是通的）⇒ 连续计数清零，全部照取。
        变异：成功不清零 / 所有失败都计。"""
        plan = {"A1": "payload_unavailable", "A2": "payload_unavailable", "A3": None,
                "A4": "payload_unavailable", "A5": "payload_unavailable", "A6": "stale_vintage"}
        calls = []

        def fetch(t, *, as_of):
            calls.append(t)
            return (_raw(t, as_of), None) if plan[t] is None else (None, plan[t])

        res = self._run(tmp_path, fetch)
        assert calls == self.TICKERS and res["fetch_aborted"] is None
        assert res["fetch_reasons"] == {"payload_unavailable": 4, "ok": 1, "stale_vintage": 1}
        plan.update(A3="stale_vintage", A6="payload_unavailable")
        calls.clear()
        assert self._run(tmp_path, fetch)["fetch_aborted"] is None and calls == self.TICKERS

    def test_time_budget_skips_the_rest(self, tmp_path, monkeypatch, caplog):
        """注入时钟：每次取数耗 250 s，预算 600 s ⇒ 取 3 只（0 / 250 / 500 时开始），其余记 fetch_skipped_time_budget。
        变异：删时间预算。"""
        clock = [1000.0]
        monkeypatch.setattr(LG, "_monotonic", lambda: clock[0])
        calls = []

        def fetch(t, *, as_of):
            calls.append(t)
            clock[0] += 250.0
            return None, "stale_vintage"

        with caplog.at_level(logging.WARNING):
            res = self._run(tmp_path, fetch)
        assert calls == ["A1", "A2", "A3"]
        assert res["fetch_reasons"] == {"stale_vintage": 3, LG.FETCH_SKIPPED_TIME_BUDGET: 3}
        assert res["fetch_aborted"] == LG.FETCH_SKIPPED_TIME_BUDGET
        assert any("预算" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)

    def test_skipped_ticker_does_not_downgrade_a_row_recorded_earlier_that_day(self, tmp_path):
        """熔断跳过的票记 unavailable，但同日已 recorded 的旧行照「不降级」保留。变异：跳过的票绕过 record 不变式。"""
        LG.run_for_date(AS_OF, tickers=["A4"], upcoming_fn=_no_earnings, bars_fn=lambda t: None,
                        fetch_fn=lambda t, as_of: (_raw(t, as_of), None), state_dir=tmp_path)
        res = self._run(tmp_path, lambda t, as_of: (None, "payload_unavailable"))
        assert res["per_tenor"]["monthly"]["kept_previous_recorded"] == 1
        assert {r["ticker"]: r["status"] for r in LG.rows_for_date(AS_OF, "monthly", tmp_path)}["A4"] == "recorded"


def _strong_flag_effect_rows(tenor="monthly"):
    """每个信息块里被 flag 的那只都最差（-1.0 倍权利金）、其余全收（1.0）⇒ put 侧检验必拒绝。"""
    return _ready_ledger_rows(tenor=tenor, outcome=lambda i, flag: -1.0 if flag else 1.0)


#: 中等效应夹具的各日 flag 位置：结果按日固定为 (1.0, 0.8, 0.5, 0.0, -0.5)，flag 取第 k 个。
_MID_P_PICKS = (4, 4, 4, 3, 3, 3, 3, 3, 2, 2, 2, 2)


def _mid_p_rows():
    """首日一个**非信息块**（排序在最前：它若也消耗随机数，后面每个块的抽样全都移位），其后 12 个信息块。
    预注册 seed / n_perm 下 p = 118/5001 ≈ 0.0236：落在 α_each 0.0125 与 α_family 0.05 之间。"""
    vals = (1.0, 0.8, 0.5, 0.0, -0.5)
    rows = [_row("2026-01-01", f"N{j}", "2026-03-01", close=_put_close(1.0)) for j in range(5)]
    for d, pick in enumerate(_MID_P_PICKS, start=1):
        for j, v in enumerate(vals):
            rows.append(_row(f"2026-01-{1 + d:02d}", f"T{j}", f"2026-03-{1 + d:02d}",
                             flag_put=(j == pick), flag_call=(j == pick), close=_put_close(v)))
    return rows


class TestPreregTestPinned:
    """S9：检验本身的几处关键实现，此前任何一个变异都能全绿通过。"""

    def test_strong_effect_freezes_reject_h0_with_the_plus_one_p(self, tmp_path):
        """冻结路径真能产出 reject_h0（此前的夹具全是纯日期效应，判定恒为 fail_to_reject_h0），报告逐字写出；
        p 恰为 1/(1+n_perm)（+1 修正：没有一次置换 ≤ 观测值）。变异：`(count)/n_perm` / 判定写反 / 报告不转述。"""
        _persist(_strong_flag_effect_rows(), tmp_path)
        md = R.write_local_report(AS_OF, state_dir=tmp_path, freeze=True).read_text(encoding="utf-8")
        d = json.loads(LG.prereg_result_path("monthly", tmp_path).read_text(encoding="utf-8"))["per_side"]
        assert d["put"]["p"] == 1 / (1 + LG.PREREG["n_perm"])
        assert (d["put"]["decision"], d["call"]["decision"]) == ("reject_h0", "fail_to_reject_h0")
        assert re.search(r"：put: reject_h0 · call: fail_to_reject_h0", md), md

    def test_golden_p_under_the_preregistered_seed(self):
        """固定数据集 + 预注册 seed / n_perm ⇒ p 逐位钉死。变异：不用传入的 seed / 改抽样方式（如逐行 permutation）/
        非信息块也消耗随机数 / 块不按日期排序 / 去掉 +1 修正。"""
        t = LG.assess("monthly", rows=_mid_p_rows())["test"]["put"]
        assert (t["n_blocks"], t["n_informative_blocks"]) == (13, 12)
        assert t["p"] == 118 / 5001, repr(t["p"])

    def test_decision_uses_alpha_each_not_alpha_family(self):
        """α_each 0.0125 < p ≈ 0.024 ≤ α_family 0.05 ⇒ fail_to_reject_h0。变异：`p <= PREREG["alpha_family"]`。"""
        t = LG.assess("monthly", rows=_mid_p_rows())["test"]["put"]
        assert LG.PREREG["alpha_each"] < t["p"] <= LG.PREREG["alpha_family"]
        assert t["decision"] == "fail_to_reject_h0"

    def test_frozen_result_applies_on_the_ready_date_itself(self, tmp_path):
        """as_of == ready_date ⇒ 冻结结果适用（给 test，报告写判定）；前一天不适用。
        变异：`as_of > ready_date`（原有断言「预注册检验已于 … 冻结」两个分支都打印，测不出来）。"""
        _persist(_ready_ledger_rows(), tmp_path)
        LG.assess("monthly", state_dir=tmp_path, freeze=True)
        on = LG.assess("monthly", state_dir=tmp_path, as_of="2026-02-13")
        assert on["frozen"]["applies"] is True and "test" in on
        line = R._assess_line(on)
        assert "预注册检验（单侧" in line and "早于该日" not in line, line
        before = LG.assess("monthly", state_dir=tmp_path, as_of="2026-02-12")
        assert before["frozen"]["applies"] is False and "test" not in before

    def test_first_writer_wins_an_existing_frozen_file_is_never_replaced(self, tmp_path):
        """另一个写者在本进程判「尚未冻结」之后、link 之前抢先冻结：本次不得覆盖，返回的是对方那份，不留 tmp。
        变异：`os.link` 换成 `os.replace`（原子，但后到者赢）。"""
        _persist(_ready_ledger_rows(), tmp_path)
        LG.assess("monthly", state_dir=tmp_path, freeze=True)
        p = LG.prereg_result_path("monthly", tmp_path)
        before = p.read_bytes()
        late = dict(json.loads(before), frozen_at="LATE-WRITER")
        got = LG._freeze_prereg_result("monthly", late, tmp_path)
        assert p.read_bytes() == before, "后到的写者覆盖了已冻结的检验结果"
        assert got == json.loads(before) and got["frozen_at"] != "LATE-WRITER"
        assert [x.name for x in p.parent.iterdir() if ".tmp." in x.name] == []
