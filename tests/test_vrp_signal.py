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

    def test_backfilled_files_are_excluded_by_glob_and_regex(self, state):
        """补跑文件用真实命名（业务日在中间、跑批日在末尾）。record_day 不该收它们 ——
        收了就等于把业务日记到跑批日名下。v0.45.104 删掉了那行显式 `_backfilled-`
        过滤：它读起来像在承重，实际 glob 与 _SNAP_RE 都已经把它们挡在外面。
        本测试把"挡住"钉在行为上，同时钉住两道机制各自确实不匹配。"""
        _snap(state, "AAA", AS_OF)
        bf = state / f"options_snapshot_BBB_2026-08-28_backfilled-{AS_OF}.json"
        bf.write_text(json.dumps({"iv_raw_observed": 30, "rv_30d": 20}))
        assert [r["ticker"] for r in vrp.record_day(AS_OF, cache_dir=state)] == ["AAA"]
        # 跑批日与业务日两个口径都不该把它捞进来
        assert bf not in set(state.glob(f"options_snapshot_*_{AS_OF}.json"))
        assert bf not in set(state.glob("options_snapshot_*_2026-08-28.json"))
        assert vrp._SNAP_RE.match(bf.name) is None
        assert [r["ticker"] for r in vrp.record_day("2026-08-28", cache_dir=state)] == []

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

    def test_rerun_preserves_settlement_written_fields(self, state):
        """v0.45.104 回归：同日重跑不得抹掉 settle() 已填好的结算字段。

        修复前：record_day 丢掉该日全部旧行、用 rv_forward/vrp_realized/settled_on
        全为 None 的新行顶上，日志照打"记录 N 行"。日报 --date 回填路径就走这里。
        """
        _snap(state, "AAA", AS_OF, iv_raw=30.0, rv=20.0)
        vrp.record_day(AS_OF, cache_dir=state)
        # settle 填上结算字段（用真实 settle，不手写，免得钉住一个假形状）
        bars = _bars(AS_OF, [100.0 + (i % 3) for i in range(40)])
        assert vrp.settle(_day(-90), bars_fn=lambda t: bars) == 1
        before = dict(vrp.load_rows()[0])
        assert before["rv_forward"] is not None and before["settled_on"] is not None

        vrp.record_day(AS_OF, cache_dir=state)                    # 同日重跑
        (after,) = vrp.load_rows()
        for k in ("rv_forward", "vrp_realized", "settled_on"):
            assert after[k] == before[k], f"{k} 被重跑抹掉了"

    def test_rerun_preserves_every_settlement_field(self, state):
        """逐字段钉住 _SETTLEMENT_FIELDS：从元组里漏掉任何一个（比如后加的
        settle_give_up 三兄弟），那个字段就会被同日重跑悄悄抹成 None。
        没有这条，漏字段的改动能全程绿灯通过。"""
        _snap(state, "AAA", AS_OF, iv_raw=30.0, rv=20.0)
        vrp.record_day(AS_OF, cache_dir=state)
        sentinel = {"rv_forward": 12.5, "vrp_realized": 17.5, "settled_on": "2026-08-01",
                    "settle_attempts": 3, "settle_give_up": True,
                    "settle_give_up_reason": "out_of_fetch_window",
                    "settle_give_up_on": "2026-08-02"}
        assert set(sentinel) == set(vrp._SETTLEMENT_FIELDS), "改了 _SETTLEMENT_FIELDS 就要同步本用例"
        (row,) = vrp.load_rows()
        row.update(sentinel)
        _seed_rows([row])

        vrp.record_day(AS_OF, cache_dir=state)
        (after,) = vrp.load_rows()
        for k, want in sentinel.items():
            assert after[k] == want, f"{k} 被重跑抹掉了"

    def test_rerun_recomputes_vrp_realized_when_iv_revised(self, state):
        """iv 被修订过时搬运结算字段还要维持 vrp_realized == iv − rv_forward。"""
        _snap(state, "AAA", AS_OF, iv_raw=30.0, rv=20.0)
        vrp.record_day(AS_OF, cache_dir=state)
        bars = _bars(AS_OF, [100.0 + (i % 3) for i in range(40)])
        vrp.settle(_day(-90), bars_fn=lambda t: bars)
        rv_f = vrp.load_rows()[0]["rv_forward"]

        _snap(state, "AAA", AS_OF, iv_raw=44.0, rv=20.0)          # 快照重抓，iv 变了
        vrp.record_day(AS_OF, cache_dir=state)
        (row,) = vrp.load_rows()
        assert row["iv"] == 44.0 and row["rv_forward"] == rv_f
        assert row["vrp_realized"] == pytest.approx(44.0 - rv_f, abs=1e-4)

    def test_prior_history_accepts_backfilled_snapshot_rv(self, state):
        """v0.45.104 回归：补跑快照的 rv_30d 也要能进 VRP 历史。

        真实命名 `options_snapshot_WMT_2026-08-28_backfilled-2026-08-29.json`：
        IV 索引早就收它（iv_history 走的是别的路径），_snapshot_rv 从前只开精确
        文件名 → 该业务日单向地"有 IV、无 vrp" → assess 的分母永远够不着。
        """
        dates = [_day(i + 1) for i in range(10)]
        _index(state, "AAA", dates, iv=30.0)
        for d in dates[:6]:
            _snap(state, "AAA", d, iv_raw=30.0, rv=25.0)
        for d in dates[6:]:                                       # 这 4 天只有补跑文件
            (state / f"options_snapshot_AAA_{d}_backfilled-{_day(-1, d)}.json").write_text(
                json.dumps({"iv_raw_observed": 30.0, "rv_30d": 25.0}))
        assert vrp._snapshot_rv(state, "AAA", dates[8]) == 25.0
        _snap(state, "AAA", AS_OF, iv_raw=30.0, rv=20.0)
        (row,) = vrp.record_day(AS_OF, cache_dir=state)
        assert row["n_obs"] == 10                                 # 修复前是 6

    def test_backfilled_fallback_prefers_newest_run(self, state):
        """同一业务日补跑多次 → 取跑批日最新的一份。"""
        for run, rv in (("2026-08-29", 11.0), ("2026-09-02", 22.0)):
            (state / f"options_snapshot_AAA_2026-08-28_backfilled-{run}.json").write_text(
                json.dumps({"rv_30d": rv}))
        assert vrp._snapshot_rv(state, "AAA", "2026-08-28") == 22.0

    def test_plain_snapshot_wins_over_backfilled(self, state):
        _snap(state, "AAA", "2026-08-28", rv=7.0)
        (state / "options_snapshot_AAA_2026-08-28_backfilled-2026-08-29.json").write_text(
            json.dumps({"rv_30d": 99.0}))
        assert vrp._snapshot_rv(state, "AAA", "2026-08-28") == 7.0

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

    def test_zero_forward_vol_is_rejected_like_upstream(self):
        """v0.45.104 回归：rv_forward == 0 不是观测，是坏数据（停牌/补齐/取错列）。

        修复前这里放行 0，于是常数收盘价凭空造出一条**最大可能**的"卖方全赢"
        观测（vrp_realized == iv），再进 settlement_stats.by_label 的均值。
        口径必须与 twelve_data.realized_vol 的 `0 < rv <= 300` 一致。
        """
        assert vrp.realized_vol_from_closes([100.0] * 22) is None
        _seed_rows([_row("AAA", D0, 10.0, iv=30.0)])
        bars = _bars(D0, [100.0] * 40)
        assert vrp.settle("2026-09-01", bars_fn=lambda t: bars) == 0
        (row,) = vrp.load_rows()
        assert row["rv_forward"] is None and row["vrp_realized"] is None
        assert row["settled_on"] is None
        # 而非退化序列照常结算
        _seed_rows([_row("AAA", D0, 10.0, iv=30.0)])
        bars = _bars(D0, [100.0 + (i % 3) for i in range(40)])
        assert vrp.settle("2026-09-01", bars_fn=lambda t: bars) == 1
        (row,) = vrp.load_rows()
        assert row["rv_forward"] > 0 and row["settled_on"] == "2026-09-01"

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
        """v0.45.104 重写：旧版本删掉 `b["date"] <= as_of` 过滤仍然全绿 ——
        它的夹具切出来的片段在有无过滤时**逐字节相同**，等于没测。

        判别性场景（唯一能让两者分叉的那种）：基准 2026-07-01，as_of=2026-07-30。
        (D0, as_of] 有 21 个**工作日**，settle 的零成本预筛因此放行；但 2026-07-03
        是独立日观察日休市，真实交易日只有 20 个。
          · 有过滤：可用 K 线 21 根（基准 + 20），差 1 根 → _forward_closes 返回 None。
          · 无过滤：直接切前 22 根，末根是 2026-07-31 —— as_of **之后**一个交易日，
            偷看了未来。
        """
        holidays = {"2026-07-03", "2026-09-07"}               # 独立日观察日 / 劳动节
        bars, d = [], dt.date.fromisoformat(D0)
        while d <= dt.date(2026, 9, 30):
            if d.weekday() < 5 and d.isoformat() not in holidays:
                bars.append({"date": d.isoformat(), "close": 100.0 + len(bars)})
            d += dt.timedelta(days=1)
        as_of = "2026-07-30"
        assert vrp._weekdays_after(D0, as_of) == 21            # 预筛放行……
        assert sum(1 for b in bars if b["date"] <= as_of) == 21   # ……但只有 20 个交易日
        assert bars[21]["date"] == "2026-07-31" > as_of        # 无过滤会用到的那根

        _seed_rows([_row("AAA", D0, 10.0)])
        calls = []

        def fn(t):
            calls.append(t)
            return bars
        assert vrp._forward_closes(bars, D0, as_of, 21) is None
        assert vrp.settle(as_of, bars_fn=fn) == 0             # 删掉过滤 → 1，本行变红
        assert calls == ["AAA"]                               # K 线确实取了，不是被预筛挡掉的
        (row,) = vrp.load_rows()
        assert row["rv_forward"] is None and row["settled_on"] is None

        # 再等一个交易日，第 21 根到位，正常结算
        as_of2 = "2026-07-31"
        assert vrp.settle(as_of2, bars_fn=fn) == 1
        (row,) = vrp.load_rows()
        assert row["rv_forward"] == pytest.approx(
            vrp.realized_vol_from_closes([b["close"] for b in bars[:22]]), abs=1e-4)

    def test_settle_skips_fetch_when_not_enough_weekdays(self):
        """预筛：连工作日都不够 forward_days 的行压根不取 K 线（省一次限流额度）。"""
        _seed_rows([_row("AAA", D0, 10.0)])
        bars = _bars(D0, [100.0 + i for i in range(60)])
        calls = []

        def fn(t):
            calls.append(t)
            return bars
        assert vrp.settle(bars[15]["date"], bars_fn=fn) == 0
        assert calls == []

    def test_base_bar_falls_back_to_last_close_on_or_before_date(self):
        _seed_rows([_row("AAA", "2026-07-04", 10.0)])         # 周六快照
        bars = _bars(D0, [100.0 + (i % 3) for i in range(40)])   # 非退化（rv == 0 已不合法）
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

    def test_out_of_fetch_window_rows_give_up_and_stop_costing_calls(self):
        """v0.45.104 回归：基准日掉出 _default_bars 的 120 根窗口 → 永远结算不了。

        修复前 pending 只看 `rv_forward is None`，这种行每票每天白取一次 K 线、
        永远取不完，而且完全看不见（既不是 settled 也没有任何标记）。
        """
        old = "2025-01-02"                                    # 离 as_of 远超 120+15 个工作日
        _seed_rows([_row("AAA", old, 10.0), _row("BBB", D0, 10.0)])
        calls = []

        def fn(t):
            calls.append(t)
            return _bars(D0, [100.0 + (i % 3) for i in range(40)])
        assert vrp.settle("2026-09-01", bars_fn=fn) == 1      # 只有 BBB 结算成功
        assert calls == ["BBB"]                               # 修复前这里还会为 AAA 取一次
        a = next(r for r in vrp.load_rows() if r["ticker"] == "AAA")
        assert a["settle_give_up"] is True
        assert a["settle_give_up_reason"] == "out_of_fetch_window"
        assert a["settle_give_up_on"] == "2026-09-01"
        assert vrp.settlement_stats()["n_gave_up"] == 1
        # 放弃是终态：再跑一次不再产生任何取数
        assert vrp.settle("2026-09-02", bars_fn=fn) == 0
        assert calls == ["BBB"]

    def test_repeated_unsettleable_rows_give_up_after_max_attempts(self):
        """拿到了 K 线却始终算不出 rv_forward → 计次，到上限打放弃标记。"""
        _seed_rows([_row("AAA", D0, 10.0)])
        bars = _bars(D0, [100.0] * 40)                        # 常数 → rv == 0 → 永远算不出
        calls = []

        def fn(t):
            calls.append(t)
            return bars
        for i in range(1, vrp.CONFIG["settle_max_attempts"] + 1):
            assert vrp.settle("2026-09-01", bars_fn=fn) == 0
            (row,) = vrp.load_rows()
            assert row["settle_attempts"] == i
        assert row["settle_give_up"] is True
        assert row["settle_give_up_reason"] == "max_attempts_exhausted"
        n_calls = len(calls)
        assert vrp.settle("2026-09-01", bars_fn=fn) == 0
        assert len(calls) == n_calls                          # 放弃后不再取数

    def test_transient_fetch_failure_does_not_count_as_attempt(self):
        """限流/断网是暂时的，不该消耗放弃次数 —— 否则一周 429 就把行判死。"""
        _seed_rows([_row("AAA", D0, 10.0), _row("BBB", D0, 10.0)])

        def fn(t):
            if t == "AAA":
                raise ConnectionError("429")
            return None
        for _ in range(vrp.CONFIG["settle_max_attempts"] + 2):
            assert vrp.settle("2026-09-01", bars_fn=fn) == 0
        for r in vrp.load_rows():
            assert r["settle_attempts"] is None and r["settle_give_up"] is None

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
        deep = _past_dates(63)
        for i in range(20):
            _index(state, f"T{i:02d}", deep)
            for d in deep:                                    # 可用历史也要够，不能只有索引
                _snap(state, f"T{i:02d}", d, iv_raw=30.0, rv=20.0)
        for i in range(20, 30):
            _index(state, f"T{i:02d}", _past_dates(10))
        res = vrp.assess(cache_dir=state)
        assert res["status"] == "ready" and res["ready"] is True and res["n_ready_tickers"] == 20
        assert res["n_index_ready_tickers"] == 20 and res["median_usable_obs"] >= 63
        assert res["eta_weeks"] == 0.0
        assert vrp.summary_line(res).startswith("✅")
        # 19 只 → 差一只就不算
        (state / "iv_history_T19.jsonl").unlink()
        assert vrp.assess(cache_dir=state)["status"] == "accruing"

    def test_ready_verdict_uses_usable_history_not_index_count(self, state, monkeypatch):
        """v0.45.104 回归：闸门量的必须是 record_day 真正用得上的那个数。

        修复前 assess 数 len(_read_index(t))，record_day 却按 len(_prior_history(...))
        判 ready —— 于是能打印「✅ VRP 信号已就绪」而当天唯一一行是
        ready=False, n_obs=4。两个分母根本不是同一个量。
        """
        monkeypatch.setitem(vrp.CONFIG, "min_obs_per_ticker", 5)
        monkeypatch.setitem(vrp.CONFIG, "min_ready_tickers", 1)
        dates = [_day(i + 1) for i in range(6)]
        _index(state, "AAA", dates, iv=30.0)
        for d in dates[:4]:                                   # 索引 6 条，但只有 4 条配得上快照
            _snap(state, "AAA", d, iv_raw=30.0, rv=25.0)
        _snap(state, "AAA", AS_OF, iv_raw=30.0, rv=20.0)

        (row,) = vrp.record_day(AS_OF, cache_dir=state)
        assert row["ready"] is False and row["n_obs"] == 4     # record_day 的真实处境

        # as_of 显式钉死：assess 默认走 pdt_today()，而本用例的日期是相对 AS_OF 造的 ——
        # 断言两端来自两个不同的钟就是定时炸弹（MEMORY：v0.45.96）。
        res = vrp.assess(cache_dir=state, as_of=AS_OF)
        assert res["per_ticker_obs"]["AAA"] == 6               # 索引口径（旧分母）
        assert res["per_ticker_usable_obs"]["AAA"] == 4        # 可用口径（新分母）
        assert res["n_index_ready_tickers"] == 1               # 旧口径会说"就绪"
        assert res["status"] == "accruing" and res["ready"] is False
        line = vrp.summary_line(res)
        assert line.startswith("⏳") and "✅" not in line
        assert "瓶颈不是攒得不够" in res["eta_note"]

        # 补上缺的两天快照 → 两个口径合流，这才真就绪
        for d in dates[4:]:
            _snap(state, "AAA", d, iv_raw=30.0, rv=25.0)
        res = vrp.assess(cache_dir=state, as_of=AS_OF)
        assert res["status"] == "ready" and res["per_ticker_usable_obs"]["AAA"] == 6

    def test_assess_settlement_is_scoped_to_passed_rows(self, state):
        """v0.45.104 回归：assess(cache_dir=某目录) 从前不带参调 settlement_stats()，
        探针式调用会把全局信号文件的生产结算统计打印成那个目录的结果。"""
        _seed_rows([_row("AAA", _day(i + 1), 1.0, vrp_realized=2.0) for i in range(12)])
        assert vrp.assess(cache_dir=state)["settlement"]["n_settled"] == 12
        assert vrp.assess(cache_dir=state, rows=[])["settlement"] == {
            "n_rows": 0, "n_settled": 0, "n_gave_up": 0, "by_label": {},
            "spearman_within_ticker": {"n_tickers": 0, "n_pairs": 0, "rho": None}}

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

    def test_spearman_is_within_ticker_not_pooled(self, state):
        """v0.45.104 重写：这是本模块的招牌主张（绝不横截面池化），从前却没被测到。

        旧夹具 x = 100k + i、y = 300k + 2i 把两轴的票间偏移推向**同一方向**，
        逐票秩化与"把 75 个原值倒一个池子排名"给出的都是 rho = 1.0 —— 把
        within-ticker 归一化整段换成朴素池化排名，39/39 依旧全绿。

        判别性夹具：把 y 的偏移翻向（y = −300k + 2i）。票内两轴仍严格同向递增，
        逐票秩化 rho = +1.0；池化原值则被票间偏移主导，rho = −0.7781。
        这是本项目被引用最多的失效模式（MEMORY：alpha-hive-cross-sectional-pooling，
        v0.45.50 那 7 条机器失效条件 28/30 只从未触发就是它）。
        """
        rows = []
        for k, t in enumerate(["AAA", "BBB", "CCC"]):
            for i in range(25):
                rows.append(_row(t, _day(i + 1), 100.0 * k + i,
                                 rv_forward=1.0, vrp_realized=-300.0 * k + 2 * i))
        _seed_rows(rows)
        sp = vrp.within_ticker_spearman(vrp.load_rows())
        assert sp == {"n_tickers": 3, "n_pairs": 75, "rho": 1.0}

        # 同一份数据，朴素池化排名给出的是完全不同（且符号相反）的答案 ——
        # 这一段就是被否决的那个实现，钉在这里以证明上面那条断言有判别力。
        pooled = vrp._pearson(vrp._avg_ranks([r["vrp_ex_ante"] for r in rows]),
                              vrp._avg_ranks([r["vrp_realized"] for r in rows]))
        assert pooled == pytest.approx(-0.7781, abs=1e-4)
        assert sp["rho"] != pytest.approx(pooled, abs=0.5)

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

    def test_exit_codes_and_out_file(self, state, tmp_path, monkeypatch):
        # min_ready 降到 2 纯为省掉 20×63 份快照的写入；min_obs 保持 63 不动
        monkeypatch.setitem(vrp.CONFIG, "min_ready_tickers", 2)
        out = tmp_path / "vrp.json"
        assert vrp.main(["--json", "--out", str(out), "--cache-dir", str(state), "--date", AS_OF]) == 3
        assert json.loads(out.read_text())["assessment"]["status"] == "undetermined"
        for i in range(25):
            _index(state, f"T{i:02d}", _past_dates(10))
        assert vrp.main(["--json", "--out", str(out), "--cache-dir", str(state), "--date", AS_OF]) == 1
        assert json.loads(out.read_text())["assessment"]["status"] == "accruing"
        deep = _past_dates(63)
        for i in range(2):
            _index(state, f"T{i:02d}", deep)
            for d in deep:
                _snap(state, f"T{i:02d}", d, iv_raw=30.0, rv=20.0)
        assert vrp.main(["--cache-dir", str(state), "--date", AS_OF]) == 0

    def test_record_zero_rows_warns_without_changing_exit_code(self, state, tmp_path, capsys):
        """v0.45.104 回归：`--date D --record` 一行没写时，退出码与健康的"攒数中"
        逐字节相同 —— 报错和正常态长得一模一样。退出码归就绪度（编排器契约不动），
        成败改由 WARNING + JSON 计数表达。"""
        for i in range(25):
            _index(state, f"T{i:02d}", _past_dates(10))       # 有索引、无当日快照
        out = tmp_path / "vrp.json"
        code = vrp.main(["--record", "--json", "--out", str(out),
                         "--cache-dir", str(state), "--date", AS_OF])
        assert code == 1                                      # 契约不变
        j = json.loads(out.read_text())
        assert j["recorded"] == 0
        assert j["warnings"] and "一行没写" in j["warnings"][0]
        assert AS_OF in j["warnings"][0]
        # 健康那一天：写了行就不该有告警
        _snap(state, "AAA", AS_OF)
        vrp.main(["--record", "--json", "--out", str(out),
                  "--cache-dir", str(state), "--date", AS_OF])
        j = json.loads(out.read_text())
        assert j["recorded"] == 1 and j["warnings"] == []

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


class TestSharedBarsCache:
    """v0.45.105：settle 的取数腿改走 `twelve_data.fetch_bars`（进程内共享缓存），
    并且把 `as_of` 当 `end_date` 传下去。

    从前是裸 `_fetch_rows(t, 120)`（`end_date=None`），于是本模块的缓存键
    与另外两个消费方（都传 as_of）不同，同一只票的日线一次扫描要取 3 遍 ——
    而 Twelve Data 是串行 7 次/分钟的队列。
    """

    def test_default_bars_asks_the_shared_cache_with_as_of(self, monkeypatch):
        import twelve_data as td
        seen = []
        monkeypatch.setattr(td, "is_configured", lambda: True)
        monkeypatch.setattr(td, "fetch_bars",
                            lambda t, d=None, end_date=None: seen.append((t, d, end_date)) or [])
        monkeypatch.setattr(td, "_fetch_rows",
                            lambda *a, **k: pytest.fail("绕过共享缓存直接取数了"))
        vrp._default_bars("NVDA", "2026-08-31")
        assert seen == [("NVDA", td.SHARED_BARS_WINDOW, "2026-08-31")]

    def test_settle_threads_as_of_into_the_default_leg(self, monkeypatch):
        """结算腿必须用**结算日**做 end_date —— 键要和另外两个消费方对得上，
        而且 `_forward_closes` 本来就只用 `date <= as_of` 的 K 线。"""
        import twelve_data as td
        seen = []
        monkeypatch.setattr(td, "is_configured", lambda: True)
        monkeypatch.setattr(td, "fetch_bars",
                            lambda t, d=None, end_date=None: seen.append((t, end_date)) or None)
        vrp._write_jsonl(vrp.SIGNALS_FILE, [_row("AAA", _day(90), 8.0)])
        vrp.settle("2026-09-01")
        assert seen == [("AAA", "2026-09-01")], f"end_date 没跟着 as_of 走：{seen}"

    def test_injected_bars_fn_keeps_its_one_arg_signature(self):
        """注入版 `bars_fn` 的签名是外部契约（本文件里十几处在用）。
        默认腿要吃 as_of，只能靠闭包，不能改 `bars_fn(ticker)` 的调用形状。"""
        got = []
        bars = [{"date": _day(90 - i), "close": 100.0 + i} for i in range(60)]
        vrp._write_jsonl(vrp.SIGNALS_FILE, [_row("AAA", _day(90), 8.0)])
        vrp.settle("2026-09-01", bars_fn=lambda t: got.append(t) or bars)
        assert got == ["AAA"]

    def test_settle_result_unchanged_by_the_shared_cache(self, monkeypatch):
        """同一份 K 线，走共享缓存与走老的裸 `_fetch_rows` 必须结算出同一个数。
        缓存只该省调用，不该动结果。"""
        import twelve_data as td
        bars = [{"date": _day(120 - i), "close": 100.0 + (i % 7)} for i in range(120)]
        monkeypatch.setattr(td, "is_configured", lambda: True)

        rows = [_row("AAA", _day(90), 8.0)]
        vrp._write_jsonl(vrp.SIGNALS_FILE, rows)
        monkeypatch.setattr(td, "fetch_bars", lambda t, d=None, end_date=None: list(bars))
        assert vrp.settle("2026-09-01") == 1
        via_cache = vrp.load_rows()[0]

        vrp._write_jsonl(vrp.SIGNALS_FILE, [_row("AAA", _day(90), 8.0)])
        assert vrp.settle("2026-09-01", bars_fn=lambda t: list(bars)) == 1
        direct = vrp.load_rows()[0]

        assert via_cache["rv_forward"] == direct["rv_forward"]
        assert via_cache["vrp_realized"] == direct["vrp_realized"]

    def test_settle_window_constant_matches_the_shared_window(self):
        """`settle_window_bars`(=120) 那条放弃闸量的是「基准日还在取数窗口里吗」。
        取数窗口现在由 `twelve_data.SHARED_BARS_WINDOW` 决定 —— 两者脱钩的话，
        放弃闸要么放早（还能结算的行被丢），要么放晚（永远结算不了的行每天白取
        一次 K 线，v0.45.104 修的就是这个）。"""
        import twelve_data as td
        assert int(vrp.CONFIG["settle_window_bars"]) == td.SHARED_BARS_WINDOW
