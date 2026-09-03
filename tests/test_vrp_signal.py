"""v0.45.102 VRP 信号：记录 / 结算 / 就绪度闸。全部离线、临时目录、无网络。"""

import datetime as dt
import json
import math
import statistics

import pytest

import vrp_signal as vrp

AS_OF = "2026-09-03"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    sd = tmp_path / "vrp_state"
    monkeypatch.setattr(vrp, "STATE_DIR", sd)
    monkeypatch.setattr(vrp, "SIGNALS_FILE", sd / "vrp_signals.jsonl")
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache


def _snap(cache, ticker, date, iv_raw=30.0, iv30=None, rv=20.0, price=100.0, extra=None):
    d = {"iv_raw_observed": iv_raw, "rv_30d": rv, "_snapshot_stock_price": price,
         "quote_set": None if iv30 is None else {"iv30": iv30, "data_available": True}}
    if extra:
        d.update(extra)
    p = cache / f"options_snapshot_{ticker}_{date}.json"
    p.write_text(json.dumps(d))
    return p


def _day(k, base=AS_OF):
    """base 之前第 k 天（k ≥ 1）/ 之后（k ≤ 0）。"""
    return (dt.date.fromisoformat(base) - dt.timedelta(days=k)).isoformat()


def _row(ticker, date, vrp_val, iv=30.0, **kw):
    r = {k: None for k in vrp._ROW_FIELDS}
    r.update({"ticker": ticker, "date": date, "iv": iv, "iv_source": "iv_raw_observed",
              "rv_30d": None if vrp_val is None else iv - vrp_val, "vrp_ex_ante": vrp_val,
              "n_obs": 0, "ready": False})
    r.update(kw)
    return r


def _seed_rows(rows):
    vrp._write_jsonl(vrp.SIGNALS_FILE, rows)


def _seed_prior(ticker, values, base=AS_OF):
    """values[i] 落在 base 之前第 i+1 天。"""
    _seed_rows([_row(ticker, _day(i + 1, base), v) for i, v in enumerate(values)])


def _all_floats_finite(obj, path="$"):
    if isinstance(obj, float):
        assert math.isfinite(obj), f"{path} = {obj!r}"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _all_floats_finite(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _all_floats_finite(v, f"{path}[{i}]")


def _index(cache, ticker, dates, iv=30.0):
    (cache / f"iv_history_{ticker}.jsonl").write_text(
        "".join(json.dumps({"date": d, "iv": iv}) + "\n" for d in dates))


def _past_dates(n, end="2025-12-31"):
    """end 之前 n 个工作日（升序），全在过去 → 积累速率可判定。"""
    out, d = [], dt.date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return sorted(out)


# ==================== 记录 ====================

class TestRecord:

    def test_iv_source_order_and_arithmetic(self, state):
        _snap(state, "AAA", AS_OF, iv_raw=32.5, rv=20.0)
        _snap(state, "BBB", AS_OF, iv_raw=None, iv30=28.0, rv=30.0)
        rows = {r["ticker"]: r for r in vrp.record_day(AS_OF, cache_dir=state)}
        assert rows["AAA"]["iv_source"] == "iv_raw_observed"
        assert rows["AAA"]["vrp_ex_ante"] == pytest.approx(12.5)
        assert rows["BBB"]["iv_source"] == "quote_set.iv30"
        assert rows["BBB"]["iv"] == 28.0 and rows["BBB"]["vrp_ex_ante"] == pytest.approx(-2.0)
        assert rows["AAA"]["underlying_price"] == 100.0
        for r in rows.values():
            assert r["reason"] is None and r["ready"] is False and r["ts_pct"] is None and r["label"] is None
            assert r["n_obs"] == 0

    def test_missing_inputs_give_reason_not_default(self, state):
        _snap(state, "AAA", AS_OF, iv_raw=None, iv30=None, rv=None)
        _snap(state, "BBB", AS_OF, iv_raw=None, iv30=None, rv=20.0)
        _snap(state, "CCC", AS_OF, iv_raw=30.0, rv=None)
        rows = {r["ticker"]: r for r in vrp.record_day(AS_OF, cache_dir=state)}
        assert rows["AAA"]["reason"] == "iv_and_rv_missing"
        assert rows["BBB"]["reason"] == "iv_missing"
        assert rows["CCC"]["reason"] == "rv_30d_missing"
        for r in rows.values():
            assert r["vrp_ex_ante"] is None and r["ready"] is False and r["ts_pct"] is None

    def test_backfilled_snapshots_are_ignored(self, state):
        _snap(state, "AAA", AS_OF)
        (state / f"options_snapshot_BBB_{AS_OF}_backfilled-2026-09-10.json").write_text(
            json.dumps({"iv_raw_observed": 30, "rv_30d": 20}))
        assert [r["ticker"] for r in vrp.record_day(AS_OF, cache_dir=state)] == ["AAA"]

    @pytest.mark.parametrize("n_prior,ready", [(62, False), (63, True), (70, True)])
    def test_n_obs_boundary_counts_only_prior_dates(self, state, n_prior, ready):
        _seed_prior("AAA", [float(i) for i in range(n_prior)])
        _snap(state, "AAA", AS_OF, iv_raw=50.0, rv=20.0)      # vrp = 30
        (row,) = vrp.record_day(AS_OF, cache_dir=state)
        assert row["n_obs"] == n_prior                        # 今天这行不算
        assert row["ready"] is ready
        if ready:
            assert 0.0 <= row["ts_pct"] <= 1.0
            assert row["ts_pct"] == pytest.approx(30 / n_prior, abs=1e-4)   # 0..29 < 30（文件里四舍五入到 4 位）
            assert row["label"] in ("rich", "cheap", "mid")
        else:
            assert row["ts_pct"] is None and row["label"] is None

    def test_rows_after_as_of_do_not_count(self, state):
        rows = [_row("AAA", _day(i + 1), float(i)) for i in range(63)]
        rows += [_row("AAA", _day(-i - 1), 999.0) for i in range(10)]   # 未来 10 行
        rows += [_row("ZZZ", _day(i + 1), float(i)) for i in range(63)]  # 别的票
        _seed_rows(rows)
        _snap(state, "AAA", AS_OF, iv_raw=50.0, rv=20.0)
        (row,) = vrp.record_day(AS_OF, cache_dir=state)
        assert row["n_obs"] == 63

    @pytest.mark.parametrize("today_vrp,pct,label", [
        (95.5, 0.96, "rich"), (80.0, 0.80, "rich"), (79.5, 0.80, "rich"),
        (50.5, 0.51, "mid"), (20.5, 0.21, "mid"),
        (20.0, 0.20, "cheap"), (5.5, 0.06, "cheap"),
    ])
    def test_ts_pct_is_fraction_strictly_below_and_labels(self, state, today_vrp, pct, label):
        _seed_prior("AAA", [float(i) for i in range(100)])   # 0..99
        _snap(state, "AAA", AS_OF, iv_raw=today_vrp + 10.0, rv=10.0)
        (row,) = vrp.record_day(AS_OF, cache_dir=state)
        assert row["ts_pct"] == pytest.approx(pct)
        assert row["label"] == label

    def test_prior_history_bootstraps_from_iv_index_plus_snapshot_rv(self, state):
        dates = [_day(i + 1) for i in range(63)]
        _index(state, "AAA", dates, iv=30.0)
        for d in dates:
            _snap(state, "AAA", d, iv_raw=30.0, rv=25.0)          # 历史 vrp = 5
        # 其中 3 天索引有、快照没有 rv → 不计
        for d in dates[:3]:
            _snap(state, "AAA", d, iv_raw=30.0, rv=None)
        _snap(state, "AAA", AS_OF, iv_raw=30.0, rv=20.0)          # 今天 vrp = 10
        (row,) = vrp.record_day(AS_OF, cache_dir=state)
        assert row["n_obs"] == 60 and row["ready"] is False
        _snap(state, "AAA", dates[0], iv_raw=30.0, rv=25.0)
        _snap(state, "AAA", dates[1], iv_raw=30.0, rv=25.0)
        _snap(state, "AAA", dates[2], iv_raw=30.0, rv=25.0)
        (row,) = vrp.record_day(AS_OF, cache_dir=state)
        assert row["n_obs"] == 63 and row["ready"] is True
        assert row["ts_pct"] == pytest.approx(1.0) and row["label"] == "rich"

    def test_file_row_wins_over_index_for_same_date(self, state):
        d = _day(1)
        _seed_rows([_row("AAA", d, None)])          # 该日已处理但 vrp 为 None
        _index(state, "AAA", [d])
        _snap(state, "AAA", d, iv_raw=30.0, rv=25.0)
        _snap(state, "AAA", AS_OF)
        (row,) = vrp.record_day(AS_OF, cache_dir=state)
        assert row["n_obs"] == 0

    def test_idempotent_rewrite_same_date(self, state):
        _seed_prior("AAA", [1.0, 2.0])
        _snap(state, "AAA", AS_OF, iv_raw=30.0, rv=20.0)
        vrp.record_day(AS_OF, cache_dir=state)
        _snap(state, "AAA", AS_OF, iv_raw=35.0, rv=20.0)
        _snap(state, "BBB", AS_OF)
        vrp.record_day(AS_OF, cache_dir=state)
        rows = vrp.load_rows()
        today = [r for r in rows if r["date"] == AS_OF]
        assert sorted(r["ticker"] for r in today) == ["AAA", "BBB"]
        assert next(r for r in today if r["ticker"] == "AAA")["vrp_ex_ante"] == pytest.approx(15.0)
        assert len(rows) == 4                                # 2 条历史保留

    def test_nan_in_snapshot_never_reaches_file(self, state):
        nan = float("nan")
        _snap(state, "AAA", AS_OF, iv_raw=nan, iv30=nan, rv=nan, price=nan)
        _snap(state, "BBB", AS_OF, iv_raw=30.0, rv=nan, price=float("inf"))
        rows = {r["ticker"]: r for r in vrp.record_day(AS_OF, cache_dir=state)}
        assert rows["AAA"]["reason"] == "iv_and_rv_missing"
        assert rows["BBB"]["reason"] == "rv_30d_missing" and rows["BBB"]["underlying_price"] is None
        for line in vrp.SIGNALS_FILE.read_text().splitlines():
            _all_floats_finite(json.loads(line))
        assert "NaN" not in vrp.SIGNALS_FILE.read_text()


# ==================== 结算 ====================

def _bars(start, closes):
    """从 start 起逐个工作日贴收盘价。"""
    out, d = [], dt.date.fromisoformat(start)
    for c in closes:
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        out.append({"date": d.isoformat(), "close": c})
        d += dt.timedelta(days=1)
    return out


D0 = "2026-07-01"           # 周三


class TestSettle:

    def test_constant_closes_realize_zero_vol(self):
        _seed_rows([_row("AAA", D0, 10.0, iv=30.0)])
        bars = _bars(D0, [100.0] * 40)
        assert vrp.settle("2026-09-01", bars_fn=lambda t: bars) == 1
        (row,) = vrp.load_rows()
        assert row["rv_forward"] == 0.0 and row["vrp_realized"] == pytest.approx(30.0)
        assert row["settled_on"] == "2026-09-01"

    def test_alternating_returns_match_project_convention(self):
        rets = [0.01 if i % 2 == 0 else -0.01 for i in range(21)]
        closes = [100.0]
        for r in rets:
            closes.append(closes[-1] * math.exp(r))
        closes += [closes[-1] * 3.0] * 10        # 第 22 根以后放巨跳，用了就露馅
        expected = statistics.stdev(rets) * math.sqrt(252) * 100     # ddof=1
        _seed_rows([_row("AAA", D0, 10.0, iv=30.0)])
        bars = _bars(D0, closes)
        assert vrp.settle("2026-09-01", bars_fn=lambda t: bars) == 1
        (row,) = vrp.load_rows()
        assert row["rv_forward"] == pytest.approx(expected, abs=1e-4)          # 文件里四舍五入到 4 位
        assert row["vrp_realized"] == pytest.approx(30.0 - expected, abs=1e-4)
        assert vrp.realized_vol_from_closes(closes[:22]) == pytest.approx(expected, rel=1e-9)

    def test_not_enough_forward_bars_stays_unsettled(self):
        _seed_rows([_row("AAA", D0, 10.0)])
        bars = _bars(D0, [100.0 + i for i in range(21)])     # 基准 + 20 根 → 差 1 根
        assert vrp.settle("2026-09-01", bars_fn=lambda t: bars) == 0
        (row,) = vrp.load_rows()
        assert row["rv_forward"] is None and row["vrp_realized"] is None and row["settled_on"] is None

    def test_lookahead_guard_ignores_bars_after_as_of(self):
        _seed_rows([_row("AAA", D0, 10.0)])
        bars = _bars(D0, [100.0 + i for i in range(60)])      # 数据够，但 as_of 只放到第 16 根
        as_of = bars[15]["date"]
        calls = []

        def fn(t):
            calls.append(t)
            return bars
        assert vrp.settle(as_of, bars_fn=fn) == 0
        assert calls == []                                   # 连工作日都不够，压根不取 K 线
        as_of = bars[21]["date"]                              # 恰好第 21 个交易日
        assert vrp.settle(as_of, bars_fn=fn) == 1
        assert calls == ["AAA"]
        (row,) = vrp.load_rows()
        assert row["rv_forward"] == pytest.approx(
            vrp.realized_vol_from_closes([b["close"] for b in bars[:22]]), abs=1e-4)

    def test_base_bar_falls_back_to_last_close_on_or_before_date(self):
        _seed_rows([_row("AAA", "2026-07-04", 10.0)])         # 周六快照
        bars = _bars(D0, [100.0] * 40)
        assert vrp.settle("2026-09-01", bars_fn=lambda t: bars) == 1

    def test_one_bars_call_per_ticker_and_quiet_tickers_skipped(self):
        rows = [_row("AAA", D0, 1.0), _row("AAA", "2026-07-02", 2.0), _row("AAA", "2026-07-06", 3.0),
                _row("BBB", D0, 4.0), _row("BBB", "2026-07-02", 5.0),
                _row("CCC", "2026-08-31", 6.0),               # 未到期
                _row("DDD", D0, 7.0, rv_forward=12.0, vrp_realized=18.0, settled_on="2026-08-01"),
                _row("EEE", D0, None, iv=None, reason="iv_missing")]                # 无 iv
        _seed_rows(rows)
        calls = []

        def fn(t):
            calls.append(t)
            return _bars(D0, [100.0 + (i % 3) for i in range(60)])
        assert vrp.settle("2026-09-01", bars_fn=fn) == 5
        assert sorted(calls) == ["AAA", "BBB"] and len(calls) == 2
        assert vrp.settle("2026-09-01", bars_fn=fn) == 0     # 幂等：已结算的不再动
        assert len(calls) == 2
        d = next(r for r in vrp.load_rows() if r["ticker"] == "DDD")
        assert d["settled_on"] == "2026-08-01"

    def test_bars_fn_failure_or_none_is_nonfatal(self):
        _seed_rows([_row("AAA", D0, 1.0), _row("BBB", D0, 2.0)])

        def fn(t):
            if t == "AAA":
                raise ConnectionError("429")
            return None
        assert vrp.settle("2026-09-01", bars_fn=fn) == 0

    def test_settled_values_are_finite(self):
        _seed_rows([_row("AAA", D0, 1.0)])
        bars = _bars(D0, [100.0, 0.0, float("nan")] + [100.0] * 40)
        vrp.settle("2026-09-01", bars_fn=lambda t: bars)     # 坏根 → 直接不结算
        for line in vrp.SIGNALS_FILE.read_text().splitlines():
            _all_floats_finite(json.loads(line))


# ==================== 就绪度 ====================

class TestAssess:

    def test_current_like_coverage_is_accruing(self, state):
        for i in range(30):
            _index(state, f"T{i:02d}", _past_dates(10))
        res = vrp.assess(cache_dir=state)
        assert res["status"] == "accruing" and res["ready"] is False
        assert res["n_tickers"] == 30 and res["n_ready_tickers"] == 0 and res["median_obs"] == 10
        # 速率分母是「首条记录以来到**今天**的交易日数」（iv_history 的口径），不是到末条记录
        rate = res["accrual_rate_per_trading_day"]
        assert rate is not None and 0 < rate <= 1.0
        raw_rate = res["accrual_observed_days"] / res["accrual_elapsed_trading_days"]   # 未取整
        assert res["eta_weeks"] == pytest.approx((63 - 10) / raw_rate / 5, abs=0.1)
        assert res["eta_weeks"] > 0
        assert res["eta_note"] is None
        assert vrp.summary_line(res).startswith("⏳")

    def test_ready_when_enough_tickers_have_enough_obs(self, state):
        for i in range(20):
            _index(state, f"T{i:02d}", _past_dates(63))
        for i in range(20, 30):
            _index(state, f"T{i:02d}", _past_dates(10))
        res = vrp.assess(cache_dir=state)
        assert res["status"] == "ready" and res["ready"] is True and res["n_ready_tickers"] == 20
        assert res["eta_weeks"] == 0.0
        assert vrp.summary_line(res).startswith("✅")
        # 19 只 → 差一只就不算
        (state / "iv_history_T19.jsonl").unlink()
        assert vrp.assess(cache_dir=state)["status"] == "accruing"

    def test_undetermined_on_empty_cache(self, state):
        res = vrp.assess(cache_dir=state)
        assert res["status"] == "undetermined" and res["ready"] is False
        assert res["eta_weeks"] is None
        assert vrp.summary_line(res).startswith("❓")

    def test_eta_none_when_accrual_rate_undeterminable(self, state):
        future = [(dt.date.today() + dt.timedelta(days=i)).isoformat() for i in range(10, 20)]
        for i in range(25):
            _index(state, f"T{i:02d}", future)
        res = vrp.assess(cache_dir=state)
        assert res["status"] == "accruing"
        assert res["accrual_rate_per_trading_day"] is None
        assert res["eta_weeks"] is None and "无法判定" in res["eta_note"]
        assert "ETA 无法判定" in vrp.summary_line(res)

    def test_eta_none_when_too_few_tickers(self, state):
        for i in range(3):
            _index(state, f"T{i:02d}", _past_dates(10))
        res = vrp.assess(cache_dir=state)
        assert res["eta_weeks"] is None and "不足" in res["eta_note"]

    def test_assess_has_no_side_effects(self, state):
        _index(state, "AAA", _past_dates(5))
        before = sorted(p.name for p in state.iterdir())
        vrp.assess(cache_dir=state)
        assert sorted(p.name for p in state.iterdir()) == before
        assert not vrp.SIGNALS_FILE.exists()

    def test_spearman_within_ticker_perfect_monotone(self, state):
        rows = []
        for k, t in enumerate(["AAA", "BBB", "CCC"]):
            off = 100.0 * k                                   # 票间大偏移，池化原值会乱
            for i in range(25):
                rows.append(_row(t, _day(i + 1), off + i, rv_forward=1.0, vrp_realized=off * 3 + 2 * i))
        _seed_rows(rows)
        sp = vrp.within_ticker_spearman(vrp.load_rows())
        assert sp == {"n_tickers": 3, "n_pairs": 75, "rho": 1.0}
        res = vrp.assess(cache_dir=state)
        assert res["settlement"]["spearman_within_ticker"]["rho"] == 1.0
        assert res["settlement"]["n_settled"] == 75

    def test_spearman_none_below_20_settled_per_ticker(self):
        rows = [_row("AAA", _day(i + 1), float(i), vrp_realized=float(i)) for i in range(19)]
        rows += [_row("BBB", _day(i + 1), float(i), vrp_realized=float(i)) for i in range(30)]
        rows += [_row("BBB", _day(i + 40), float(i)) for i in range(30)]   # 未结算不算
        _seed_rows(rows)
        sp = vrp.within_ticker_spearman(vrp.load_rows())
        assert sp["n_tickers"] == 1 and sp["n_pairs"] == 30 and sp["rho"] == 1.0
        _seed_rows(rows[:19])
        assert vrp.within_ticker_spearman(vrp.load_rows()) == {"n_tickers": 0, "n_pairs": 0, "rho": None}

    def test_label_means_need_ten(self):
        rows = [_row("AAA", _day(i + 1), 1.0, label="rich", vrp_realized=2.0) for i in range(10)]
        rows += [_row("AAA", _day(i + 20), 1.0, label="cheap", vrp_realized=-1.0) for i in range(9)]
        _seed_rows(rows)
        st = vrp.settlement_stats()
        assert st["by_label"]["rich"] == {"n": 10, "mean_vrp_realized": 2.0}
        assert st["by_label"]["cheap"] == {"n": 9, "mean_vrp_realized": None}


# ==================== CLI / 日报小节 ====================

class TestCliAndMarkdown:

    def test_exit_codes_and_out_file(self, state, tmp_path):
        out = tmp_path / "vrp.json"
        assert vrp.main(["--json", "--out", str(out), "--cache-dir", str(state), "--date", AS_OF]) == 3
        assert json.loads(out.read_text())["assessment"]["status"] == "undetermined"
        for i in range(25):
            _index(state, f"T{i:02d}", _past_dates(10))
        assert vrp.main(["--json", "--out", str(out), "--cache-dir", str(state), "--date", AS_OF]) == 1
        assert json.loads(out.read_text())["assessment"]["status"] == "accruing"
        for i in range(25):
            _index(state, f"T{i:02d}", _past_dates(63))
        assert vrp.main(["--cache-dir", str(state), "--date", AS_OF]) == 0

    def test_record_flag_writes_rows(self, state, tmp_path, capsys):
        _snap(state, "AAA", AS_OF)
        out = tmp_path / "vrp.json"
        code = vrp.main(["--record", "--settle", "--json", "--out", str(out),
                         "--cache-dir", str(state), "--date", AS_OF])
        assert code == 3
        j = json.loads(out.read_text())
        assert j["recorded"] == 1 and j["settled"] == 0
        assert len(vrp.rows_for_date(AS_OF)) == 1

    def test_render_markdown(self, state):
        assert vrp.render_markdown(AS_OF, cache_dir=state) == ""
        _index(state, "AAA", _past_dates(10))
        _snap(state, "AAA", AS_OF, iv_raw=30.0, rv=20.0)
        _snap(state, "BBB", AS_OF, iv_raw=None, iv30=None, rv=20.0)
        vrp.record_day(AS_OF, cache_dir=state)
        md = vrp.render_markdown(AS_OF, cache_dir=state)
        assert "## VRP 信号（攒数期，未下注）" in md
        assert "| AAA | 30.0% | 20.0% | 10.00 | 0 | — | — |" in md
        assert "| BBB | — | 20.0% | — | — | — | — |" in md
        assert "不影响任何仓位或评分" in md
        assert "**结算**" not in md

    def test_daily_report_wiring(self):
        import inspect
        import alpha_hive_daily_report as ahdr
        cls = next(c for _, c in inspect.getmembers(ahdr, inspect.isclass)
                   if hasattr(c, "_post_scan_notify") and hasattr(c, "_options_paper_leg_markdown"))
        assert hasattr(cls, "_vrp_markdown")
        # v0.45.101 纸面腿块（及纸面组合日更）都在 _post_scan_notify 里，VRP 钩子紧跟其后
        src = inspect.getsource(cls._post_scan_notify)
        assert src.index("期权纸面腿已更新") < src.index("vrp_signal")
        assert "vrp_signal" in src and "record_day(self.date_str)" in src and "settle(self.date_str)" in src
        assert "_vrp_markdown()" in inspect.getsource(ahdr)
