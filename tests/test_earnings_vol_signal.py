"""v0.45.101 财报事件波动率信号（earnings_vol_signal）。全部离线。"""

import json

import pytest

import earnings_vol_signal as evs

AS_OF = "2026-09-03"
EXPIRY = "2026-10-02"     # 29 DTE
EARN = "2026-09-20"


def _leg(sym, cp, strike, bid, ask, quote_ok=True, spread_pct=None):
    mid = round((bid + ask) / 2, 4) if quote_ok else None
    if spread_pct is None and quote_ok:
        spread_pct = round((ask - bid) / mid, 4)
    return {"symbol": sym, "type": cp, "role": f"atm_{'call' if cp == 'C' else 'put'}",
            "strike": strike, "expiry": EXPIRY, "dte": 29, "bid": bid, "ask": ask,
            "mid": mid, "spread_pct": spread_pct if quote_ok else None, "iv": 0.3,
            "delta": 0.5 if cp == "C" else -0.5, "quote_ok": quote_ok}


def _qs(S=100.0, c=(4.9, 5.1), p=(4.9, 5.1), expiry=EXPIRY, dte=29, call_ok=True, put_ok=True,
        call_spread=None, put_spread=None, available=True, d25_spread=None):
    call = _leg("XYZ261002C00100000", "C", 100.0, *c, quote_ok=call_ok, spread_pct=call_spread)
    put = _leg("XYZ261002P00100000", "P", 100.0, *p, quote_ok=put_ok, spread_pct=put_spread)
    ok = call_ok and put_ok
    straddle = round(call["mid"] + put["mid"], 4) if ok else None
    c25 = p25 = None
    if d25_spread is not None:
        c25 = _leg("XYZ261002C00110000", "C", 110.0, 1.0, 1.2, spread_pct=d25_spread)
        p25 = _leg("XYZ261002P00090000", "P", 90.0, 1.0, 1.2, spread_pct=d25_spread)
    return {"data_available": available, "source": "cboe", "error": None, "target_dte": 30,
            "selected_expiry": expiry, "selected_dte": dte, "underlying_price": S,
            "iv30": 0.3, "market_open": False, "fetched_at": "2026-09-03T16:10:00",
            "contracts": {"atm_call": call, "atm_put": put, "c25": c25, "p25": p25},
            "missing_reasons": {}, "atm_straddle_mid": straddle,
            "implied_move_pct": round(straddle / S * 100, 4) if straddle else None}


def _stats(n=8, median=5.0):
    return {"ticker": "XYZ", "n": n, "n_missing": 0, "median_abs_move_pct": median,
            "mean_abs_move_pct": median, "max_abs_move_pct": median * 2, "moves": [],
            "source": "synthetic", "usable": n >= 4}


UP = {"earnings_date": EARN, "earnings_time": "AMC"}


def _no_nan(obj):
    json.dumps(obj, allow_nan=False)   # 有 NaN/Inf 会抛


# ==================== 合格条件 ====================

class TestEligibility:

    def test_happy_path_eligible(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=None)
        assert s["eligible"] is True and s["reason"] is None
        assert s["straddle_move_pct"] == pytest.approx(10.0)
        _no_nan(s)

    def test_event_after_expiry_is_ineligible(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), {"earnings_date": "2026-10-10"}, _stats(), None)
        assert s["eligible"] is False and "not within" in s["reason"]

    def test_event_must_clear_the_ledger_expiry_buffer_on_both_sides(self):
        """M1 回归：合格上界是 expiry − expiry_buffer_days，不是 expiry。

        账本（options_paper_leg）在到期前 buffer 天强平，所以落在缓冲里的信号一定会
        在它自己要测的财报**之前**被平掉，只往 KPI 里塞一次纯来回点差损失。
        旧断言正好把这个坏情形（earnings_date == EXPIRY）当成"合格"钉住了。
        """
        import options_paper_leg as opl
        buf = opl.CONFIG["expiry_buffer_days"]
        last_ok = evs._minus_days(EXPIRY, buf)
        assert last_ok == "2026-09-30"                      # 2026-10-02 − 2 天
        # 下界不变：财报日必须严格晚于 as_of
        assert evs.compute_signal("XYZ", AS_OF, _qs(), {"earnings_date": AS_OF},
                                  _stats(), None)["eligible"] is False
        # 边界内侧：正好 expiry − buffer 仍然合格
        assert evs.compute_signal("XYZ", AS_OF, _qs(), {"earnings_date": last_ok},
                                  _stats(), None)["eligible"] is True
        # 边界外侧：缓冲之内（含 ed == expiry）一律不合格
        for bad in (evs._minus_days(EXPIRY, buf - 1), EXPIRY):
            s = evs.compute_signal("XYZ", AS_OF, _qs(), {"earnings_date": bad}, _stats(), None)
            assert s["eligible"] is False and "not within" in s["reason"], bad

    def test_eligibility_buffer_reads_the_ledger_config(self):
        """缓冲天数只有一个真相（options_paper_leg.CONFIG）；降级常量必须与它相等。"""
        import options_paper_leg as opl
        assert evs._exit_buffer_days() == opl.CONFIG["expiry_buffer_days"]
        assert evs._EXIT_BUFFER_FALLBACK_DAYS == opl.CONFIG["expiry_buffer_days"]

    def test_signal_record_initialises_realized_move_pct(self):
        """settle_signals 会写 realized_move_pct，compute_signal 必须先声明它（schema 漂移）。"""
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), None)
        assert "realized_move_pct" in s and s["realized_move_pct"] is None

    def test_atm_leg_quote_not_ok(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(put_ok=False), UP, _stats(), None)
        assert s["eligible"] is False and s["reason"] == "atm leg quote not ok"

    def test_quote_set_missing_or_unavailable(self):
        assert evs.compute_signal("XYZ", AS_OF, None, UP, _stats(), None)["reason"] == "quote_set unavailable"
        assert evs.compute_signal("XYZ", AS_OF, _qs(available=False), UP, _stats(), None)["eligible"] is False

    def test_no_upcoming_date(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), None, _stats(), None)
        assert s["reason"] == "no upcoming earnings date"

    def test_insufficient_history(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(n=3), None)
        assert s["eligible"] is False and "insufficient earnings history" in s["reason"]
        assert evs.compute_signal("XYZ", AS_OF, _qs(), UP, None, None)["eligible"] is False

    def test_ineligible_still_reports_intermediates(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(n=2), rv_30d=30.0)
        assert s["straddle_move_pct"] == pytest.approx(10.0) and s["hist_n"] == 2
        assert s["ratio"] is None and s["label"] is None


# ==================== 扩散扣除 ====================

class TestDiffusionMath:

    def test_diffusion_pairs_calendar_days_with_365_not_252(self):
        """H1 回归：dte 是**日历日**，只能配 365；252 是交易日年，配交易日计数的 T。

        rv=30%、dte=29 → 0.8·0.30·√(29/365)·100 = 6.764937…
        换回 /252 会得到 8.141564…（√T 放大 √(365/252)=1.2035，扩散虚高 20.35%），
        下面这个精确断言会直接变红。
        """
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=30.0)
        assert s["diffusion_move_pct"] == pytest.approx(6.7649, abs=1e-4)
        assert s["diffusion_move_pct"] != pytest.approx(8.1416, abs=1e-3)   # 旧的 /252

    @pytest.mark.parametrize("name,straddle_leg,rv,median,label,ratio", [
        # 模块自己的两个校准案例。三代口径：
        #   v0.45.99  dte/252            → NVDA 0.333 cheap、COST 扩散>跨式 floor/untradeable
        #   v0.45.104 dte/365            → NVDA 0.9124 fair、COST 0.7298 cheap
        #   v0.45.105 再加回窗口扩散      → 下面这两行（分子分母含同样多的扩散）
        ("NVDA", (6.2, 6.3), 45.0, 8.0, "fair", 0.9966),
        ("COST", (2.75, 2.83), 22.0, 3.5, "fair", 0.8564),
    ])
    def test_calibration_cases_keep_their_labels(self, name, straddle_leg, rv, median, label, ratio):
        s = evs.compute_signal(name, AS_OF, _qs(c=straddle_leg, p=straddle_leg), UP,
                               _stats(median=median), rv_30d=rv)
        assert s["event_move_floor_hit"] is False
        assert s["ratio"] == pytest.approx(ratio, abs=1e-3)
        assert s["raw_label"] == s["label"] == label

    def test_hand_computed_event_move(self):
        # straddle 10%；rv 20%
        #   扩散(dte)      = 0.8·0.20·√(29/365)·100 = 4.50996
        #   扩散(2 交易日)  = 0.8·0.20·√(2/252)·100  = 1.42539（v0.45.105 加回）
        #   event = √(100 − 20.3401 + 2.0318) = 9.0384
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(median=5.0), rv_30d=20.0)
        assert s["diffusion_move_pct"] == pytest.approx(4.5100, abs=1e-3)
        assert s["diffusion_window_move_pct"] == pytest.approx(1.4254, abs=1e-3)
        assert s["implied_event_move_pct"] == pytest.approx(9.0384, abs=1e-3)
        assert s["ratio"] == pytest.approx(1.8077, abs=1e-3)     # 9.0384 / 5.0
        assert s["event_move_basis"] == "straddle_minus_diffusion_plus_window"
        assert s["event_move_floor_hit"] is False
        assert s["implied_event_move_pct"] < s["straddle_move_pct"]

    def test_floor_when_diffusion_exceeds_straddle(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=60.0)   # diffusion 16.3 > 10
        assert s["implied_event_move_pct"] == 0.0 and s["event_move_floor_hit"] is True
        # 被压到 0 的事件波动是退化数字：raw 仍算 cheap，但不得进账本
        assert s["raw_label"] == "cheap"
        assert s["label"] == "untradeable" and s["tradeable"] is False
        assert "floored" in s["untradeable_reason"]
        _no_nan(s)

    def test_rv_none_uses_raw_straddle_no_nan(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=None)
        assert s["event_move_basis"] == "raw_straddle"
        assert s["diffusion_move_pct"] is None
        assert s["implied_event_move_pct"] == pytest.approx(10.0)
        _no_nan(s)

    def test_rv_nan_treated_as_missing(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=float("nan"))
        assert s["rv_30d"] is None and s["event_move_basis"] == "raw_straddle"
        _no_nan(s)


# ==================== 标签阈值 / 可交易性 ====================

class TestNumeratorAndDenominatorCarryTheSameDiffusion:
    """v0.45.105：分子分母必须含同样多的扩散，否则两个偏差同向复利。

    分母是 `earnings_history` 的**两个交易日**窗口（天然含 2 天扩散），
    分子若把 dte 天的扩散整个减掉就成了纯事件波动 —— 分子偏小、分母偏大，
    比值被系统性压低、一切被推向 cheap。修法是在分子里留下同样长度的扩散。
    """

    #: 手算（rv=45%、dte=29 日历日、straddle=12.5%、k=0.8）
    #:   扩散(dte)   = 0.8 × 0.45 × √(29/365) × 100 = 10.1474
    #:   扩散(2 交易日) = 0.8 × 0.45 × √(2/252)  × 100 =  3.2071
    #:   事件        = √(12.5² − 10.1474² + 3.2071²)  =  7.9728
    def _sig(self, rv=45.0, straddle_pct=12.5, median=8.0):
        # S=100 且两腿等价 → 每腿 mid = straddle_pct/2，implied_move_pct == straddle_pct
        half = straddle_pct / 2.0
        qs = _qs(c=(half - 0.1, half + 0.1), p=(half - 0.1, half + 0.1))
        assert qs["implied_move_pct"] == pytest.approx(straddle_pct, abs=1e-6)
        return evs.compute_signal("XYZ", AS_OF, qs, UP, _stats(median=median), rv_30d=rv)

    def test_window_diffusion_is_added_back(self):
        s = self._sig()
        assert s["diffusion_move_pct"] == pytest.approx(10.1474, abs=1e-3)
        assert s["diffusion_window_move_pct"] == pytest.approx(3.2071, abs=1e-3)
        assert s["implied_event_move_pct"] == pytest.approx(7.9728, abs=1e-3)
        # 纯事件口径（不加回窗口扩散）是 7.2993——必须**不是**它
        assert s["implied_event_move_pct"] != pytest.approx(7.2993, abs=1e-3)
        assert s["event_move_basis"] == "straddle_minus_diffusion_plus_window"

    def test_identity_holds(self):
        """event² == straddle² − 扩散(dte)² + 扩散(窗口)²，删掉任一项即破。

        容差按落盘精度取：四个量都经 `_r` 舍到 4 位小数，平方后误差在 1e-3 量级，
        不是 1e-6。少了窗口项时两边差 10.29（远超容差），所以这条仍有判别力。
        """
        s = self._sig()
        lhs = s["implied_event_move_pct"] ** 2
        rhs = (s["straddle_move_pct"] ** 2 - s["diffusion_move_pct"] ** 2
               + s["diffusion_window_move_pct"] ** 2)
        assert lhs == pytest.approx(rhs, abs=1e-3)
        # 判别力自证：去掉窗口项后两边相差 ~10.29，远在容差之外
        assert abs(lhs - (rhs - s["diffusion_window_move_pct"] ** 2)) > 1.0

    def test_window_term_uses_trading_days_over_252(self):
        """窗口是 2 个**交易日** → 只能配 252。用 365 会得 2.6649，必须拒绝。"""
        s = self._sig()
        assert s["diffusion_window_move_pct"] == pytest.approx(3.2071, abs=1e-3)
        assert s["diffusion_window_move_pct"] != pytest.approx(2.6649, abs=1e-3)

    def test_calibration_cases_land_near_parity(self):
        """模块自己的两个校准例子：修正后都落在 ~1.0，符合大致公允定价的预期。"""
        nvda = self._sig(rv=45.0, straddle_pct=12.5, median=8.0)
        cost = self._sig(rv=22.0, straddle_pct=5.58, median=3.5)
        assert nvda["ratio"] == pytest.approx(0.9966, abs=1e-3) and nvda["raw_label"] == "fair"
        # 只减不加时 COST 是 0.7299 → cheap；加回窗口扩散后 0.8564 → fair
        assert cost["ratio"] == pytest.approx(0.8564, abs=1e-3) and cost["raw_label"] == "fair"

    def test_window_length_is_configurable_and_moves_the_number(self):
        s2 = self._sig()
        evs.CONFIG["realized_window_trading_days"] = 8
        try:
            s8 = self._sig()
        finally:
            evs.CONFIG["realized_window_trading_days"] = 2
        assert s8["diffusion_window_move_pct"] > s2["diffusion_window_move_pct"]
        assert s8["implied_event_move_pct"] > s2["implied_event_move_pct"]


class TestEarningsTimeIsNotPassedOffAsObserved:
    """v0.45.105：`earnings_time` 恒为写死的 "AMC"，不能顶着观测值的名字落盘。"""

    def test_field_is_renamed_to_assumed(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=30.0)
        assert "earnings_time" not in s, "叫这个名字会被日后的分层校准当成观测值"
        assert s["earnings_time_assumed"] == "AMC"

    def test_observed_slot_exists_and_is_empty(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), rv_30d=30.0)
        assert s["earnings_time_observed"] is None

    def test_source_is_carried_through(self):
        up = dict(UP, source="chronos_bee_catalyst")
        s = evs.compute_signal("XYZ", AS_OF, _qs(), up, _stats(), rv_30d=30.0)
        assert s["earnings_time_source"] == "chronos_bee_catalyst"


class TestLabels:

    @pytest.mark.parametrize("median,label", [
        (5.0, "rich"),                          # ratio 2.0
        (10.0 / 1.30, "rich"),                  # 恰好 1.30 → ≥ rich
        (10.0 / 1.29, "fair"),
        (10.0, "fair"),                         # 1.0
        (10.0 / 0.76, "fair"),
        (10.0 / 0.75, "cheap"),                 # 恰好 0.75 → ≤ cheap
        (20.0, "cheap"),                        # 0.5
    ])
    def test_thresholds(self, median, label):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(median=median), None)
        assert s["label"] == s["raw_label"] == label and s["tradeable"] is True

    def test_untradeable_spread_overrides_label(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(call_spread=0.276), UP, _stats(median=5.0), None)
        assert s["eligible"] is True
        assert s["raw_label"] == "rich" and s["label"] == "untradeable"
        assert s["tradeable"] is False and "27.6%" in s["untradeable_reason"]
        assert s["max_leg_spread_pct"] == pytest.approx(0.276)

    def test_spread_at_threshold_is_tradeable(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(call_spread=0.15, put_spread=0.10), UP, _stats(), None)
        assert s["tradeable"] is True and s["max_leg_spread_pct"] == pytest.approx(0.15)

    def test_spread_gate_reads_atm_legs_only_never_the_25d_legs(self):
        """账本成交的是 ATM 跨式，闸门必须只看 ATM 两腿。

        CONFIG 注释以前引的"COST 25Δ 27.6%"是一个**从未被这个过滤器求值过**的数
        （而且是盘后的）。这条测试把腿的选择钉死：改成读 25Δ、或改成四腿取 max，
        两个方向都会变红。
        """
        # 25Δ 烂得离谱、ATM 干净 → 仍然可交易（读 25Δ 或取四腿 max 都会挂）
        wide25 = evs.compute_signal("XYZ", AS_OF, _qs(call_spread=0.02, put_spread=0.02,
                                                      d25_spread=0.90),
                                    UP, _stats(median=5.0), None)
        assert wide25["tradeable"] is True
        assert wide25["max_leg_spread_pct"] == pytest.approx(0.02)
        # 反向：ATM 烂、25Δ 干净 → 不可交易（证明它读的确实是 ATM，而不是"永远放行"）
        wideatm = evs.compute_signal("XYZ", AS_OF, _qs(call_spread=0.30, put_spread=0.02,
                                                       d25_spread=0.01),
                                     UP, _stats(median=5.0), None)
        assert wideatm["tradeable"] is False and wideatm["label"] == "untradeable"
        assert wideatm["max_leg_spread_pct"] == pytest.approx(0.30)


# ==================== scan 幂等 ====================

@pytest.fixture
def state(tmp_path, monkeypatch):
    sd = tmp_path / "options_paper_state"
    monkeypatch.setattr(evs, "STATE_DIR", sd)
    monkeypatch.setattr(evs, "SIGNALS_FILE", sd / "earnings_signals.jsonl")
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache


def _write_snap(cache, ticker, date, qs, rv=None, suffix=""):
    (cache / f"options_snapshot_{ticker}_{date}{suffix}.json").write_text(
        json.dumps({"ticker": ticker, "quote_set": qs, "rv_30d": rv}), encoding="utf-8")


class _Watcher:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def get_earnings_date(self, tk):
        self.calls.append(tk)
        d = self.table.get(tk)
        return {"earnings_date": d, "earnings_time": "AMC"} if d else None


class TestScan:

    def test_scan_is_idempotent_per_date_and_ignores_backfilled(self, state):
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        _write_snap(state, "ABC", AS_OF, _qs(), rv=20.0)
        _write_snap(state, "OLD", AS_OF, _qs(), rv=20.0, suffix="_backfilled-2026-09-04")
        # 另一天的行必须保留
        evs.STATE_DIR.mkdir(parents=True)
        evs.SIGNALS_FILE.write_text(json.dumps({"ticker": "XYZ", "as_of": "2026-09-02"}) + "\n")
        w = _Watcher({"XYZ": EARN, "ABC": "2026-12-01"})
        stats_calls = []

        def stats_fn(tk):
            stats_calls.append(tk)
            return _stats()
        r1 = evs.scan(AS_OF, cache_dir=state, watcher=w, stats_fn=stats_fn)
        r2 = evs.scan(AS_OF, cache_dir=state, watcher=w, stats_fn=stats_fn)
        assert {s["ticker"] for s in r1} == {"XYZ", "ABC"}          # OLD 被忽略
        rows = evs.load_signals()
        assert sum(1 for r in rows if r["as_of"] == AS_OF) == 2
        assert sum(1 for r in rows if r["as_of"] == "2026-09-02") == 1
        assert len(r2) == 2
        xyz = next(s for s in r1 if s["ticker"] == "XYZ")
        abc = next(s for s in r1 if s["ticker"] == "ABC")
        assert xyz["eligible"] is True and xyz["label"] == "rich"
        assert abc["eligible"] is False and "not within" in abc["reason"]
        # 财报不在到期日内的票不该去拉历史（省网络）
        assert stats_calls == ["XYZ", "XYZ"]

    def test_scan_rerun_preserves_settled_fields(self, state):
        """M4 回归：回补/手动重跑重写当天的行时，settle_signals 填的实际波动必须搬过来。

        日更路径撞不上（一行不可能在自己的 as_of 当天就结算），`--date` 回补会：
        不搬就等于把已结算的样本删掉，而新行还带着 M2 的 look-ahead 分母。
        """
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        w = _Watcher({"XYZ": EARN})
        evs.scan(AS_OF, cache_dir=state, watcher=w, stats_fn=lambda tk: _stats())
        rows = evs.load_signals()
        assert len(rows) == 1 and rows[0]["settled_on"] is None
        rows[0].update({"realized_abs_move_pct": 4.0, "realized_move_pct": -4.0,
                        "realized_ratio": 0.44, "settled_on": "2026-09-25"})
        evs._write_jsonl(evs.SIGNALS_FILE, rows)

        evs.scan(AS_OF, cache_dir=state, watcher=w, stats_fn=lambda tk: _stats())
        again = evs.load_signals()
        assert len(again) == 1
        assert again[0]["realized_abs_move_pct"] == pytest.approx(4.0)
        assert again[0]["realized_move_pct"] == pytest.approx(-4.0)
        assert again[0]["realized_ratio"] == pytest.approx(0.44)
        assert again[0]["settled_on"] == "2026-09-25"

    def test_injected_upcoming_fn_is_used_and_watcher_never_built(self, state, monkeypatch):
        """调用方已经有财报日时可以直接注入，不再构造 EarningsWatcher。"""
        import earnings_watcher
        monkeypatch.setattr(earnings_watcher, "EarningsWatcher",
                            lambda *a, **k: pytest.fail("EarningsWatcher 不该被构造"))
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        seen = []

        def up(tk):
            seen.append(tk)
            return {"earnings_date": EARN, "earnings_time": "AMC"}
        r = evs.scan(AS_OF, cache_dir=state, stats_fn=lambda tk: _stats(), upcoming_fn=up)
        assert seen == ["XYZ"]
        assert r[0]["eligible"] is True and r[0]["earnings_date"] == EARN

    def test_scan_skips_network_when_quotes_dead(self, state):
        _write_snap(state, "XYZ", AS_OF, _qs(available=False))
        w = _Watcher({"XYZ": EARN})
        r = evs.scan(AS_OF, cache_dir=state, watcher=w, stats_fn=lambda tk: pytest.fail("should not fetch"))
        assert r[0]["reason"] == "quote_set unavailable" and w.calls == []

    def test_scan_handles_old_snapshot_without_quote_set(self, state):
        (state / f"options_snapshot_XYZ_{AS_OF}.json").write_text(json.dumps({"ticker": "XYZ"}))
        r = evs.scan(AS_OF, cache_dir=state, watcher=_Watcher({}), stats_fn=lambda tk: None)
        assert len(r) == 1 and r[0]["eligible"] is False


# ==================== settle ====================

class TestSettle:

    def test_settle_fills_realized_and_ignores_future(self, state):
        evs.STATE_DIR.mkdir(parents=True)
        past = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(median=5.0), rv_30d=20.0)
        fut = evs.compute_signal("ABC", AS_OF, _qs(), {"earnings_date": "2026-09-30"}, _stats(), None)
        bad = evs.compute_signal("BAD", AS_OF, _qs(put_ok=False), UP, _stats(), None)   # 不合格
        evs._write_jsonl(evs.SIGNALS_FILE, [past, fut, bad])

        # 财报日当天那根也要在：两日窗口要求 pre/ed/post 三根 K 线相邻（earnings_history M5）
        bars = {"XYZ": [{"date": "2026-09-18", "close": 100.0}, {"date": "2026-09-20", "close": 102.0},
                        {"date": "2026-09-21", "close": 104.0}]}
        n = evs.settle_signals("2026-09-25", bars_fn=lambda tk: bars.get(tk))
        assert n == 1
        rows = {r["ticker"]: r for r in evs.load_signals()}
        x = rows["XYZ"]
        assert x["realized_abs_move_pct"] == pytest.approx(4.0)
        assert x["realized_ratio"] == pytest.approx(4.0 / x["implied_event_move_pct"], abs=1e-3)
        assert x["settled_on"] == "2026-09-25"
        assert rows["ABC"]["realized_abs_move_pct"] is None and rows["BAD"]["settled_on"] is None
        # 再跑一次：没有新东西可填
        assert evs.settle_signals("2026-09-26", bars_fn=lambda tk: bars.get(tk)) == 0

    def test_settle_waits_when_post_bar_missing(self, state):
        evs.STATE_DIR.mkdir(parents=True)
        past = evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(), None)
        evs._write_jsonl(evs.SIGNALS_FILE, [past])
        n = evs.settle_signals("2026-09-21", bars_fn=lambda tk: [{"date": "2026-09-18", "close": 100.0}])
        assert n == 0 and evs.load_signals()[0]["realized_abs_move_pct"] is None

    def test_summary_stats_honest_below_min_n(self, state):
        evs.STATE_DIR.mkdir(parents=True)
        sigs = []
        for i, tk in enumerate(["A", "B"]):
            s = evs.compute_signal(tk, AS_OF, _qs(), UP, _stats(median=5.0), None)
            s["realized_ratio"] = 0.5 + i
            sigs.append(s)
        evs._write_jsonl(evs.SIGNALS_FILE, sigs)
        st = evs.summary_stats()
        assert st["n_settled"] == 2 and st["by_label"]["rich"]["n"] == 2
        assert st["by_label"]["rich"]["mean_realized_ratio"] is None
        assert st["mean_realized_ratio"] is None
        sigs.append(dict(sigs[0], ticker="C", realized_ratio=1.0))   # (0.5+1.5+1.0)/3 = 1.0
        evs._write_jsonl(evs.SIGNALS_FILE, sigs)
        assert evs.summary_stats()["by_label"]["rich"]["mean_realized_ratio"] == pytest.approx(1.0)
