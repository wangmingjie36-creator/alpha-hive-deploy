"""F&G 组合层敞口控制门前瞻检验（v0.45.242 共振加成那次的预注册模式，本次移植到组合层）的守卫。

`experiments/fg_exposure_gate_forward_test.py` 是一份**预注册**：价值全在"看结果之前写死"。
这里守的不是"算得对不对"这一件事，而是五件（同 `tests/test_resonance_boost_forward_test.py`
的立场）：

1. 预注册常量没被悄悄改掉。
2. 检视按**时间顺序最先攒到的**前 N 周算，未到检视点时返回值里没有任何效应量（盲化在
   数据结构上）。
3. 自证是前提：机制本身错了（或复现不了生产实际记录）就"无法判定"，不算出一个没意义的数。
4. 统计对象与共振加成那次**不同**——这次改的是仓位大小不是排序，横截面 IC 不适用，
   用的是两个变体（`paper_portfolio.run_replay` 真实重放）净值曲线的周度收益率差，
   一并守住"这套改法真的接的是真实 `paper_portfolio` 代码，不是重新写一遍"。
5. 自证的**起点**（v0.45.297 事后修订）：A/B 必须从生产在窗口起点的状态起跑，否则仓位基数
   （= 本金 + 累计已实现盈亏）对不上，自证在真实数据上必红——而这个缺陷在首次拿到真实前瞻样本
   之前从未暴露过（旧测试用手写的「生产记录」，从没见过一份真实状态）。第 10~14 节守这一条。
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "fg_exposure_gate_forward_test", _ROOT / "experiments" / "fg_exposure_gate_forward_test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fwd = _load_module()

_EFFECT_KEYS = {"stats", "mean", "t", "p", "delta", "weekly_deltas", "stats_all_weeks",
               "weeks_used", "adjusted_trades"}

# 白名单，不是黑名单（v0.45.308 独立审查，I-2）：`_EFFECT_KEYS` 只堵得住"叫这几个名字"的效应量键，
# 换个名字（比如把 `adjusted_trades` 换成 `adj_summary` 再无条件写进 `evaluate()` 的返回字典）
# 照样能泄漏而不被抓到——15 个存活变异里就有这一个。这里反过来显式列出 `evaluate()`/`run()` 在
# `not_ready`/`cannot_judge` 状态下**允许**出现的顶层键（逐个 early-return 分支核对得出），
# 任何超出都判定为可疑效应量键，不管它叫什么名字。
_NOT_READY_OR_CANNOT_JUDGE_ALLOWED_KEYS = {
    "status", "mode", "n_dates", "seed_last_run_date", "selfproof", "selfproof_rate",
    "selfproof_decision_rate", "weeks_available", "weeks", "next_look_at",
    "looks_passed_without_verdict", "reason", "stale", "mechanism_selfcheck_ok",
    # v0.45.391 复审 S1：回放行情窗口的取数计数（`paper_portfolio._ReplayOhlcWindow.stats()`）。白名单照样管到它的
    # **子键**（下面 `_OHLC_WINDOW_KEYS`）——不让它变成一个可以随手塞效应量的口袋。
    "ohlc_window",
    # v0.45.408 逐日重锚：连续 A 的复现计数（诊断）与分段情况——只有 A 的计数 / 日期 / 来源名，没有效应量。
    # 子键同样白名单（`_SEGMENT_KEYS` / `_SELFPROOF_KEYS`），不让它们变成可以随手塞效应量的口袋。
    "selfproof_continuous", "selfproof_segments",
}
#: `ohlc_window` 允许的子键：只有窗口区间、取数次数与失败标的（及原因），没有价格 / 净值 / 收益
_OHLC_WINDOW_KEYS = {"window", "wide_fetches", "served", "out_of_window", "fallback", "fallback_tickers",
                     "direct_requests", "direct_empty", "degraded"}


_SELFPROOF_KEYS = {"real_entries", "reproduced", "decision_reproduced", "a_only_entries"}
_SEGMENT_KEYS = {"segments", "multi_day_segments", "stale_anchor_segments", "invalid_anchors",
                 "production_unprocessed_dates", "anchor_sources", "unreadable_state_commits", "overlap_days_checked"}


def _assert_no_unexpected_top_level_keys(res):
    extra = set(res) - _NOT_READY_OR_CANNOT_JUDGE_ALLOWED_KEYS
    assert not extra, f"出现白名单外的顶层键（可能是新换了名字的效应量泄漏）：{extra}"
    for k in ("selfproof", "selfproof_continuous"):
        if k in res:
            assert set(res[k]) <= _SELFPROOF_KEYS, f"{k} 出现白名单外的子键：{set(res[k]) - _SELFPROOF_KEYS}"
    if "selfproof_segments" in res:
        assert set(res["selfproof_segments"]) <= _SEGMENT_KEYS, \
            f"selfproof_segments 出现白名单外的子键：{set(res['selfproof_segments']) - _SEGMENT_KEYS}"
    if "ohlc_window" in res:
        ow = res["ohlc_window"]
        assert isinstance(ow, dict) and set(ow) <= _OHLC_WINDOW_KEYS, f"ohlc_window 出现白名单外的子键：{set(ow) - _OHLC_WINDOW_KEYS}"
        assert all(isinstance(v, str) for v in ow["fallback_tickers"].values()), "fallback_tickers 只许是 标的→原因 文本"


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_keys(v)


# ── 1. 预注册常量 ─────────────────────────────────────────────────────────────

class TestPreregistrationFrozen:
    def test_constants_are_the_registered_ones(self):
        assert fwd.FORWARD_START == "2026-09-16"
        assert fwd.LOOKS == ((15, 0.02, "中期"), (30, 0.045, "终期"))
        assert fwd.SELFPROOF_MIN_RATE == 0.95
        assert fwd.FG_GATE_TEST_CONFIG == {
            "fg_exposure_gate": {"enabled": True, "extreme_fear": 25, "extreme_greed": 75,
                                "long_size_mult": 0.5, "short_size_mult": 0.5},
        }


# ── 2. 统计：单侧检验（复用 ic_diagnostics.basic_stats，只补单侧转换）───────────

class TestOneSidedStat:
    def test_positive_mean_gives_small_one_sided_p(self):
        st = fwd.one_sided_greater_than_zero([1.0, 1.2, 0.8, 1.5, 0.9, 1.1, 1.3, 0.7, 1.0, 1.4])
        assert st["mean"] > 0
        assert st["p"] < 0.01

    def test_negative_mean_gives_large_one_sided_p(self):
        st = fwd.one_sided_greater_than_zero([-1.0, -1.2, -0.8, -1.5, -0.9, -1.1, -1.3, -0.7, -1.0, -1.4])
        assert st["mean"] < 0
        assert st["p"] > 0.9

    def test_all_zero_is_not_significant(self):
        """多数周没有极端 F&G 触发时的正常形态——全 0 不该被判定为"确认"。"""
        st = fwd.one_sided_greater_than_zero([0.0] * 10)
        assert st["p"] == 1.0

    def test_all_same_positive_value_is_significant(self):
        """零方差但均值为正——退化情形，`ic_diagnostics.basic_stats` 给出 nan t，
        必须显式按符号判，不能让 nan 悄悄漏成"无法判定不了了之"。"""
        st = fwd.one_sided_greater_than_zero([2.0] * 10)
        assert st["p"] == 0.0

    def test_single_value_cannot_be_tested(self):
        st = fwd.one_sided_greater_than_zero([1.0])
        assert st["t"] is None and st["p"] == 1.0

    # ── v0.45.323：p 取 t(n−1)，不是正态近似（事前实现对齐预注册「单侧 t 检验」）──

    @pytest.mark.parametrize("values", [
        [1.0, 1.2, 0.8, 1.5, 0.9, 1.1, 1.3, 0.7, 1.0, 1.4],
        [0.3, -0.1, 0.0, 0.0, 0.5, -0.2, 0.0, 0.1, 0.0, 0.0, 0.4, -0.3, 0.0, 0.2, 0.0],
        [-0.4, 0.1, -0.2, 0.0, -0.1, 0.3, -0.5],
    ], ids=["强正", "零膨胀", "偏负"])
    def test_p_equals_scipy_one_sample_t_greater(self, values):
        """与 scipy 的单样本 t 检验（H1: 均值 > 0）逐值相等——包括 t<0 那一侧。"""
        from scipy import stats
        ref = stats.ttest_1samp(values, 0.0, alternative="greater").pvalue
        assert fwd.one_sided_greater_than_zero(values)["p"] == pytest.approx(ref, rel=1e-9)

    @staticmethod
    def _series_with_t(base, target_t):
        """把零均值的 base 平移，使单样本 t 恰为 target_t（平移不改标准差）。"""
        import math
        from statistics import mean, stdev
        assert abs(mean(base)) < 1e-12
        shift = target_t * stdev(base) / math.sqrt(len(base))
        return [x + shift for x in base]

    def test_interim_boundary_case_is_not_confirmed(self):
        """判别用例：n=15、t=2.15 落在 [z 临界 2.054, t(14) 临界 2.264) 之间。
        正态近似给单侧 p=0.0158（<0.02 ⇒ 中期「确认」），t(14) 给 0.0248（不确认）。
        v0.45.323 之前的实现在这里判 confirmed——本条在旧实现上必红。"""
        import math
        deltas = self._series_with_t([1.0, -1.0] * 7 + [0.0], 2.15)
        z_p = 0.5 * math.erfc(2.15 / math.sqrt(2))
        assert z_p < 0.02, "前提：正态近似会在这里误判确认——否则本用例区分不了 z 与 t"
        assert fwd.one_sided_greater_than_zero(deltas)["p"] == pytest.approx(0.02478, abs=1e-4)
        res = fwd.decide(TestDecide()._weeks(deltas))
        assert res["status"] == "not_ready"
        assert not (_EFFECT_KEYS & set(_all_keys(res))), "中期未过界时不得带出效应量"

    def test_final_boundary_case_is_not_confirmed(self):
        """判别用例：n=30、t=1.72，正态近似单侧 p=0.0427（<0.045 ⇒ 终期「确认」），
        t(29) 给 0.0480（未确认，结案）。前 15 周 t≈1.45，不会提前在中期触发。"""
        import math
        deltas = self._series_with_t([1.0, -1.0] * 15, 1.72)
        assert 0.5 * math.erfc(1.72 / math.sqrt(2)) < 0.045, "前提：正态近似会在这里误判确认"
        assert fwd.decide(TestDecide()._weeks(deltas[:15]))["status"] == "not_ready"
        res = fwd.decide(TestDecide()._weeks(deltas))
        assert res["status"] == "not_confirmed"
        assert res["look"] == "终期"
        assert res["stats"]["p"] == pytest.approx(0.04798, abs=1e-4)


# ── 3. 周度净值差 ─────────────────────────────────────────────────────────────

class TestWeeklyDeltas:
    def _eq(self, pairs):
        return [{"date": d, "nav": v} for d, v in pairs]

    def test_two_consecutive_weeks_return_pct(self):
        a = self._eq([("2026-03-09", 100.0), ("2026-03-16", 110.0)])  # 03-09/16 均为周一
        b = self._eq([("2026-03-09", 100.0), ("2026-03-16", 121.0)])
        weeks = fwd.weekly_deltas(a, b)
        assert len(weeks) == 1
        w = weeks[0]
        assert w["ret_a"] == pytest.approx(10.0)
        assert w["ret_b"] == pytest.approx(21.0)
        assert w["delta"] == pytest.approx(11.0)

    def test_only_first_day_of_each_iso_week_counts(self):
        a = self._eq([("2026-03-09", 100.0), ("2026-03-10", 105.0), ("2026-03-16", 110.0)])
        b = self._eq([("2026-03-09", 100.0), ("2026-03-10", 105.0), ("2026-03-16", 110.0)])
        weeks = fwd.weekly_deltas(a, b)
        assert len(weeks) == 1  # 03-10 不是当周第一天，不该多算出一周

    def test_missing_nav_rows_are_skipped(self):
        a = self._eq([("2026-03-09", 100.0), ("2026-03-16", None), ("2026-03-23", 120.0)])
        b = self._eq([("2026-03-09", 100.0), ("2026-03-16", None), ("2026-03-23", 120.0)])
        weeks = fwd.weekly_deltas(a, b)
        # None 那行被跳过 → 只剩两个"首日"取值点 → 一个区间
        assert len(weeks) == 1

    def test_only_common_weeks_are_paired(self):
        a = self._eq([("2026-03-09", 100.0), ("2026-03-16", 110.0)])
        b = self._eq([("2026-03-09", 100.0), ("2026-03-16", 105.0), ("2026-03-23", 115.0)])
        weeks = fwd.weekly_deltas(a, b)
        assert {w["week"] for w in weeks} == {"2026-W12"}  # A 没有 03-23 那周


# ── 4. 成组序贯判定：盲化 + 检视 ────────────────────────────────────────────────

class TestDecide:
    def _weeks(self, deltas):
        return [{"week": f"2026-W{i:02d}", "delta": d} for i, d in enumerate(deltas)]

    def test_below_first_look_is_progress_only(self):
        res = fwd.decide(self._weeks([1.0] * 5))
        assert res == {"status": "not_ready", "weeks": 5, "next_look_at": 15,
                       "looks_passed_without_verdict": []}
        assert not (_EFFECT_KEYS & set(_all_keys(res)))

    def test_interim_not_crossed_reports_no_effect(self):
        """中期 15 周未过界（p 不够小）——只报"继续"，连"未达标"的具体数字都不带出来。"""
        res = fwd.decide(self._weeks([0.0] * 15))
        assert res["status"] == "not_ready"
        assert not (_EFFECT_KEYS & set(_all_keys(res)))

    def test_interim_uses_first_fifteen_weeks_only(self):
        """中期只用前 15 周——后面再攒多少周、多好或多差都不影响这次检视。"""
        deltas = [2.0] * 15 + [-100.0] * 20  # 后面的极端值不该污染中期判定
        res = fwd.decide(self._weeks(deltas))
        assert res["status"] == "confirmed"
        assert res["look"] == "中期"
        assert res["weeks_used"] == [f"2026-W{i:02d}" for i in range(15)]

    def test_final_look_can_confirm(self):
        """需要精心选的数字：中期(t=1.75，t(14) 单侧 p=0.051)不够格(<0.02)但终期
        (t=2.15，t(29) 单侧 p=0.020)够格(<0.045)——不能拿"处处都显著"的数据测，
        那测不出"两次检视门槛不同"这件事。（v0.45.323 前是正态近似，同一组数据为 0.040 / 0.016。）"""
        deltas = ([1.4, -0.6] * 15)[:30]
        interim = fwd.decide(self._weeks(deltas[:15]))
        assert interim["status"] == "not_ready"  # 先确认中期确实没过
        res = fwd.decide(self._weeks(deltas))
        assert res["status"] == "confirmed"
        assert res["look"] == "终期"

    def test_final_look_without_effect_is_not_confirmed(self):
        res = fwd.decide(self._weeks([0.0] * 30))
        assert res["status"] == "not_confirmed"
        assert res["look"] == "终期"

    def test_json_round_trip_still_has_no_effect_keys_when_not_ready(self):
        import json
        res = fwd.decide(self._weeks([1.0] * 10))
        reloaded = json.loads(json.dumps(res))
        assert not (_EFFECT_KEYS & set(_all_keys(reloaded)))


# ── 5. 建仓四元组 key：Position（在场）与 ClosedTrade（已平仓）两种形状都要处理 ──

class TestEntryKey:
    def test_open_position_is_keyed_by_shares_times_entry_price_too(self):
        """v0.45.396：在场仓位**不**读 `size_usd`，与已平仓同一个算式（原先读 `size_usd` ⇒ 1234.57）。"""
        pos = {"ticker": "NVDA", "entry_date": "2026-09-16", "direction": "bullish",
              "size_usd": 1234.567, "shares": 10.0, "entry_price": 123.456}
        assert fwd._entry_key(pos) == ("NVDA", "2026-09-16", "bullish", 1234.56)

    def test_closed_trade_derives_size_from_shares_times_entry_price(self):
        trade = {"ticker": "NVDA", "entry_date": "2026-09-16", "direction": "bullish",
                 "shares": 10.0, "entry_price": 123.456}
        assert fwd._entry_key(trade) == ("NVDA", "2026-09-16", "bullish", 1234.56)

    def test_entries_in_window_merges_closed_and_open_and_filters_by_date(self):
        closed = [{"ticker": "A", "entry_date": "2026-09-16", "direction": "bullish",
                  "shares": 1.0, "entry_price": 100.0},
                 {"ticker": "B", "entry_date": "2026-09-01", "direction": "bullish",  # 窗口外
                  "shares": 1.0, "entry_price": 100.0}]
        open_pos = [{"ticker": "C", "entry_date": "2026-09-17", "direction": "bearish",
                    "size_usd": 500.0, "shares": 5.0, "entry_price": 100.0}]
        got = fwd._entries_in_window(closed, open_pos, "2026-09-16", "2026-09-20")
        assert got == {("A", "2026-09-16", "bullish", 100.0), ("C", "2026-09-17", "bearish", 500.0)}


# ── 6. 附带统计：被调整的已平仓笔数 ─────────────────────────────────────────────

class TestAdjustedTradesSummary:
    def test_same_size_is_not_counted_as_adjusted(self):
        t = {"ticker": "A", "entry_date": "2026-09-16", "shares": 10.0, "entry_price": 100.0,
            "pnl_usd": 50.0}
        out = fwd._adjusted_trades_summary([t], [dict(t)], "2026-09-16", "2026-09-20")
        assert out["adjusted_closed_trades"] == 0

    def test_different_size_is_counted_with_pnl_sums(self):
        ta = {"ticker": "A", "entry_date": "2026-09-16", "shares": 10.0, "entry_price": 100.0,
             "pnl_usd": 50.0}
        tb = {"ticker": "A", "entry_date": "2026-09-16", "shares": 5.0, "entry_price": 100.0,
             "pnl_usd": 25.0}
        out = fwd._adjusted_trades_summary([ta], [tb], "2026-09-16", "2026-09-20")
        assert out == {"adjusted_closed_trades": 1, "pnl_sum_a": 50.0, "pnl_sum_b": 25.0}


# ── 7. 集成：真实 `paper_portfolio.run_replay`（离线，OHLC 打桩）───────────────

def _fake_ohlc_flat(ticker, start, end):
    """价格恒定 → 只会触发 TIME 止损，不触发 SL/TP，行为最好预测。"""
    out = {}
    d = datetime.date.fromisoformat(start)
    end_d = datetime.date.fromisoformat(end)
    while d < end_d:
        if d.weekday() < 5:
            out[d.isoformat()] = {"Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.0}
        d += datetime.timedelta(days=1)
    return out


def _seed_fg_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE signal_archive (date TEXT, ticker TEXT, signal TEXT, value REAL)")
    con.executemany("INSERT INTO signal_archive VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()


def _write_snapshot(snap_dir, ticker, date, score, direction):
    import json as _json
    (snap_dir / f"{ticker}_{date}.json").write_text(_json.dumps({
        "ticker": ticker, "date": date, "composite_score": score, "direction": direction,
        "agent_votes": {"a": score, "b": score}, "entry_price": 100.0,
    }))


@pytest.fixture
def pp(monkeypatch, tmp_path):
    import paper_portfolio as _pp
    monkeypatch.setattr(_pp, "_fetch_ohlc", _fake_ohlc_flat)
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    monkeypatch.setattr(_pp, "SNAPSHOT_DIR", snap_dir)
    fg_db = tmp_path / "fg.db"
    monkeypatch.setattr(_pp, "_pheromone_db_path", lambda: fg_db)
    return _pp, snap_dir, fg_db


class TestRealRunReplayIntegration:

    def test_mechanism_selfcheck_passes_with_matching_dates(self, pp, tmp_path):
        """`--insample` 的机制自检：baseline（默认配置）与显式 enabled=False 必须逐笔相同。"""
        _pp, snap_dir, fg_db = pp
        dates = ["2026-09-01", "2026-09-02"]
        _seed_fg_db(fg_db, [(d, "NVDA", sig, v) for d in dates
                           for sig, v in (("market.fear_greed", 80.0), ("market.fear_greed_is_cnn", 1.0))])
        for d in dates:
            _write_snapshot(snap_dir, "NVDA", d, 7.5, "bullish")
        res = fwd.evaluate(dates, "2026-09-01", "2026-09-03", tmp_path / "sandbox", insample=True)
        assert res["mechanism_selfcheck_ok"] is True

    def test_extreme_greed_reduces_new_long_size(self, pp, tmp_path):
        """端到端：真实 `run_replay` 在极度贪婪日确实把新多头仓位打了折，反映在
        `adjusted_trades` 与非零周度 delta 上——不是靠假设，是真的跑出来的。"""
        _pp, snap_dir, fg_db = pp
        dates = ["2026-09-01", "2026-09-08"]  # 相隔一周，制造两个"首日"取值点
        _seed_fg_db(fg_db, [(d, "NVDA", sig, v) for d in dates
                           for sig, v in (("market.fear_greed", 90.0), ("market.fear_greed_is_cnn", 1.0))])
        for d in dates:
            _write_snapshot(snap_dir, "NVDA", d, 7.5, "bullish")
        res = fwd.evaluate(dates, "2026-09-01", "2026-09-09", tmp_path / "sandbox", insample=True)
        assert res["mechanism_selfcheck_ok"] is True
        # 恒定价格下没有已实现盈亏（TIME 止损前不会平仓）——用"新开仓市值"直接核对更直接。
        a_dir, b_dir = tmp_path / "sandbox" / "A_baseline", tmp_path / "sandbox" / "B_treatment"
        a_open = _pp._load_jsonl(a_dir / "positions.jsonl")
        b_open = _pp._load_jsonl(b_dir / "positions.jsonl")
        assert a_open and b_open
        assert b_open[0]["size_usd"] == pytest.approx(a_open[0]["size_usd"] * 0.5, rel=1e-6)

    def test_self_proof_fails_when_production_never_ran(self, pp, tmp_path, monkeypatch):
        """forward 模式：生产 `paper_portfolio_state/` 里这个窗口一条真实记录都没有 ⇒
        `not_ready`（不是 `cannot_judge`——"从没跑过"和"跑了但对不上"是两种不同的失败）。"""
        _pp, snap_dir, fg_db = pp
        dates = ["2026-09-16"]
        for d in dates:
            _write_snapshot(snap_dir, "NVDA", d, 7.5, "bullish")
        res = fwd.evaluate(dates, "2026-09-16", "2026-09-17", tmp_path / "sandbox", insample=False, seed={})
        assert res["status"] == "not_ready"
        assert res["selfproof_rate"] is None

    def test_self_proof_fails_when_reproduction_rate_too_low(self, pp, tmp_path):
        """forward 模式：生产**有**真实记录，但重放对不上（评分链/配置在期间被改动过，
        或重放机制本身有问题）⇒ `cannot_judge`，不是静默算出一个没意义的 ΔNAV。
        用一条与真实开仓大小明显不同的"生产记录"制造 100% 不吻合，触发阈值判定。"""
        _pp, snap_dir, fg_db = pp
        dates = ["2026-09-16"]
        _write_snapshot(snap_dir, "NVDA", "2026-09-16", 7.5, "bullish")
        # 真实"生产记录"：同一笔仓位但市值故意写成一个重放永远算不出来的数
        _pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _pp._append_jsonl(_pp.CLOSED_FILE, {
            "ticker": "NVDA", "direction": "bullish", "entry_date": "2026-09-16",
            "entry_price": 100.0, "exit_date": "2026-09-17", "exit_price": 100.0,
            "holding_days": 1, "shares": 9999.0, "gross_return_pct": 0.0, "net_return_pct": 0.0,
            "cost_pct": 0.0, "pnl_usd": 0.0, "exit_reason": "TIME", "confidence": "high", "score": 7.5,
        })
        res = fwd.evaluate(dates, "2026-09-16", "2026-09-17", tmp_path / "sandbox", insample=False, seed={})
        assert res["status"] == "cannot_judge"
        assert res["selfproof"]["reproduced"] == 0


# ── 8. run()：取数与运行防线 ──────────────────────────────────────────────────

class TestRunGuards:

    def test_no_snapshots_at_all_is_cannot_judge(self, monkeypatch, tmp_path):
        import paper_portfolio as _pp
        monkeypatch.setattr(_pp, "SNAPSHOT_DIR", tmp_path / "empty")
        (tmp_path / "empty").mkdir()
        res = fwd.run(today="2026-09-15")
        assert res["status"] == "cannot_judge"

    def test_no_forward_samples_yet_is_not_ready(self, monkeypatch, tmp_path):
        import paper_portfolio as _pp
        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        _write_snapshot(snap_dir, "NVDA", "2026-03-09", 7.0, "bullish")  # 早于 FORWARD_START
        monkeypatch.setattr(_pp, "SNAPSHOT_DIR", snap_dir)
        res = fwd.run(today="2026-09-20")
        assert res["status"] == "not_ready"
        assert res.get("stale") is not True  # 才过 4 天，还没到 STALE_DAYS

    def test_long_silence_is_flagged_stale(self, monkeypatch, tmp_path):
        import paper_portfolio as _pp
        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        _write_snapshot(snap_dir, "NVDA", "2026-03-09", 7.0, "bullish")
        monkeypatch.setattr(_pp, "SNAPSHOT_DIR", snap_dir)
        res = fwd.run(today="2026-10-20")  # FORWARD_START 之后超过 21 天
        assert res["status"] == "not_ready"
        assert res.get("stale") is True


# ── 9. status_line：一行摘要，不含效应量 ─────────────────────────────────────

class TestCarriedByReadiness:
    """承载在 `ic_rerun_readiness.py`（同 `resonance_forward_status` 那条挂载点）——
    这里只测"接线接对了"，不重复 `TestStatusLine` 已经测过的格式细节。"""

    def _stub_assess(self, ready):
        def _f(*a, **k):
            return {"ready": ready, "cohort": {"date": "2026-09-14", "version": "v0", "reason": "",
                                                "n_generations": 1},
                   "target_ic": 0.09, "weeks_required": 25, "n_ripe_samples": 0, "n_all_samples": 0,
                   "weeks_accrued": 0, "weeks_remaining": 25, "scan_weeks_in_cohort": 0,
                   "calendar_weeks_elapsed": 0, "weeks_per_calendar_week": 0.0, "eta_date": None,
                   "eta_calendar_weeks": 0, "pool_note": "", "next_step": "?"}
        return _f

    def test_load_failure_renders_as_a_line_not_an_exception(self, monkeypatch, tmp_path):
        import ic_rerun_readiness as rr
        monkeypatch.setattr(rr, "ALPHAHIVE_DIR", tmp_path / "nowhere")
        res = rr.fg_exposure_gate_forward_status(today="2026-09-15")
        assert res["status"] == "cannot_judge"
        assert "F&G 敞口门前瞻检验无法判定" in res["line"]

    def test_quiet_summary_carries_both_forward_tests(self, monkeypatch, tmp_path, capsys):
        import sys
        import ic_rerun_readiness as rr
        db = tmp_path / "p.db"
        db.write_bytes(b"")
        monkeypatch.setattr(rr, "assess", self._stub_assess(False))
        monkeypatch.setattr(sys, "argv", ["ic_rerun_readiness.py", "--db", str(db), "--quiet"])
        rr.main()
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        assert "共振加成前瞻检验" in out[0] and "F&G 敞口门前瞻检验" in out[0]

    def test_json_payload_has_fg_gate_key(self, monkeypatch, tmp_path, capsys):
        import sys
        import ic_rerun_readiness as rr
        db = tmp_path / "p.db"
        db.write_bytes(b"")
        monkeypatch.setattr(rr, "assess", self._stub_assess(False))
        monkeypatch.setattr(sys, "argv", ["ic_rerun_readiness.py", "--db", str(db), "--json"])
        rr.main()
        import json as _json
        payload = _json.loads(capsys.readouterr().out)
        assert set(payload["fg_exposure_gate_forward_test"]) == {"status", "line"}


class TestStatusLine:
    _OW = {"window": ["2026-09-15", "2026-10-02"], "wide_fetches": 11, "served": 0, "out_of_window": 0,
           "fallback": 11, "fallback_tickers": {"NVDA": "ConnectionError: x"}, "direct_requests": 144,
           "direct_empty": 0, "degraded": True}
    _NR = {"status": "not_ready", "weeks": 3, "next_look_at": 15, "looks_passed_without_verdict": [],
           "selfproof_rate": 1.0}

    def test_healthy_window_leaves_the_line_unchanged(self):
        """复审 S1：健康时（degraded=False）进度行与没有 `ohlc_window` 键时逐字相同——周报 / 编排器读到的不变。"""
        healthy = dict(self._OW, fallback=0, fallback_tickers={}, served=300, direct_requests=0, degraded=False)
        assert fwd.status_line({**self._NR, "ohlc_window": healthy}) == fwd.status_line(self._NR)
        assert fwd.status_line(self._NR) == "⏳ F&G 敞口门前瞻检验：3/15 个合格周，自证 100%"

    def test_degraded_window_is_visible_and_turns_the_icon(self):
        """复审 S1：整段取数全部退回直连 ⇒ 段首 ⏳ 换 ⚠️、段尾说几个标的、退回了什么；不含 `｜`（--quiet 段分隔符）。
        变异「status_line 不看 ohlc_window」⇒ 红。"""
        line = fwd.status_line({**self._NR, "ohlc_window": self._OW})
        assert line.startswith("⚠️ F&G 敞口门前瞻检验：3/15 个合格周，自证 100%；⚠️ 回放行情窗口降级："), line
        assert "11/11 个标的整段取数失败" in line and "退回逐次直连" in line and "｜" not in line
        cj = fwd.status_line({"status": "cannot_judge", "reason": "r", "ohlc_window": self._OW})
        assert cj.startswith("⚠️ F&G 敞口门前瞻检验无法判定：r；⚠️ 回放行情窗口降级")
        ck = fwd.status_line({"status": "confirmed", "look": "中期", "ohlc_window": self._OW})
        assert ck.startswith("🔔 ") and "回放行情窗口降级" in ck

    def test_selfproof_reason_names_an_ohlc_outage(self):
        """复审 S3：served=0 且直连全空 ⇒ 原因说行情不可得，不再推给评分链 / 配置 / 漏跑；
        整段被拒但直连照常（S1 的形状）⇒ 原文案不变。"""
        sp = {"exact": 1, "total": 10, "decision": 3, "rate": 0.1, "decision_rate": 0.3}
        outage = dict(self._OW, direct_empty=144)
        r = fwd._selfproof_failure_reason(sp, outage)
        assert "OHLC 不可得" in r and "11/11 个标的全部失败" in r and "144 次请求也全是空" in r
        assert "已被改动" not in r
        assert fwd._selfproof_failure_reason(sp, self._OW) == fwd._selfproof_failure_reason(sp)
        assert "评分链/入场规则/组合层配置已被改动" in fwd._selfproof_failure_reason(sp, None)
        # 二次复审：几乎全断（部分直连拿到了 bar / 窗口服务过少数标的）⇒ 不说「OHLC 不可得」，
        # 原因照旧写在前面，但末尾必须说出行情缺失——限流的真实形状常是只有一两只取到。
        # 变异「`_ohlc_partly_missing` 恒 False」⇒ 这两格红。
        base = fwd._selfproof_failure_reason(sp)
        for partial in (dict(outage, direct_empty=143), dict(outage, served=5)):
            r = fwd._selfproof_failure_reason(sp, partial)
            assert r.startswith(base) and "OHLC 不可得" not in r, r
            assert "但这次重放有行情缺失" in r and f"里 {partial['direct_empty']} 次一根 bar 都没有" in r, r
        # 金额层分支同样追加
        sp_amt = dict(sp, decision=10, decision_rate=1.0)
        r = fwd._selfproof_failure_reason(sp_amt, dict(outage, served=5))
        assert "仓位金额" in r and "但这次重放有行情缺失" in r

    def test_human_output_shows_the_degraded_window(self, capsys):
        """二次复审：演练分支不经 status_line，人读输出是唯一出口。变异「`_print_human` 里 `if False:`」⇒ 红。"""
        fwd._print_human({"mode": "rehearse", "status": "cannot_judge", "reason": "r", "ohlc_window": self._OW})
        assert "回放行情窗口降级" in capsys.readouterr().out
        fwd._print_human({"mode": "rehearse", "status": "cannot_judge", "reason": "r",
                          "ohlc_window": dict(self._OW, degraded=False)})
        assert "回放行情窗口降级" not in capsys.readouterr().out

    def test_not_ready_line_has_no_effect_size(self):
        line = fwd.status_line({"status": "not_ready", "weeks": 3, "next_look_at": 15,
                                "looks_passed_without_verdict": []})
        assert "3/15" in line
        assert not any(k in line for k in ("mean", "delta"))

    def test_cannot_judge_line_carries_reason(self):
        line = fwd.status_line({"status": "cannot_judge", "reason": "路径错了"})
        assert "路径错了" in line

    def test_confirmed_line_points_to_the_script(self):
        line = fwd.status_line({"status": "confirmed", "look": "中期"})
        assert "fg_exposure_gate_forward_test.py" in line


# ══════════════════════════════════════════════════════════════════════════════
# 10~14：窗口起点种子（v0.45.297 事后修订）
#
# 真因：A/B 沙箱从空状态起跑，而生产在窗口首日已带着累计已实现盈亏。仓位基数
# `nav = cash + Σ size_usd` 是**状态**，空沙箱只能复现「开哪只、什么方向」（决策层），
# 复现不了「买多少」（金额层）——首次拿到真实前瞻样本就报「精确 0/8、决策层 8/8」。
# ══════════════════════════════════════════════════════════════════════════════

_SEED_DIR = _ROOT / "experiments" / "fg_gate_forward_seed"


def _pos_row(ticker, entry_date, size_usd, entry_price=100.0, direction="bullish"):
    return {"ticker": ticker, "direction": direction, "entry_date": entry_date,
            "entry_price": entry_price, "sl_price": round(entry_price * 0.93, 4),
            "tp_price": round(entry_price * 1.15, 4), "shares": round(size_usd / entry_price, 4),
            "size_usd": size_usd, "time_stop_date": "2026-09-24", "confidence": "high",
            "score": 7.5, "rationale": "seed", "sizing": "tier"}


def _closed_row(ticker, pnl, entry_date="2026-08-20", exit_date="2026-08-27"):
    return {"ticker": ticker, "direction": "bullish", "entry_date": entry_date, "entry_price": 100.0,
            "exit_date": exit_date, "exit_price": 100.0 + pnl / 10, "holding_days": 7, "shares": 10.0,
            "gross_return_pct": 0.0, "net_return_pct": 0.0, "cost_pct": 0.0, "pnl_usd": pnl,
            "exit_reason": "TP", "confidence": "high", "score": 7.5}


def _jsonl(rows):
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


def _make_seed(*, cash=46000.0, positions=(), closed=(), last_run_date="2026-09-15"):
    meta = {"version": "test", "starting_capital": 50000.0, "starting_date": "2026-03-09",
            "cash": cash, "last_run_date": last_run_date, "config_snapshot": {}}
    return {"meta.json": json.dumps(meta).encode(), "positions.jsonl": _jsonl(positions),
            "closed_trades.jsonl": _jsonl(closed)}


def _write_seed_dir(d, files, *, window_start="2026-09-16", manifest_overrides=None):
    """校验和按**传入内容**现算——内容坏但校验和对，才能单独考验内容校验那一层。"""
    d.mkdir(parents=True, exist_ok=True)
    for n, b in files.items():
        (d / n).write_bytes(b)
    manifest = {"schema": 1, "window_start": window_start,
                "seed_last_run_date": json.loads(files["meta.json"]).get("last_run_date"),
                "files": {n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
                          for n, b in files.items()}}
    manifest.update(manifest_overrides or {})
    (d / "SEED_SOURCE.json").write_text(json.dumps(manifest), encoding="utf-8")


# ── 10. 仓库里冻结的那份真种子（环境无关：文件随仓库发布 ⇒ 是断言，不是 skip）───────

class TestFrozenSeedIsThePreregisteredOne:
    def test_manifest_is_bound_to_the_preregistered_window(self):
        m = json.loads((_SEED_DIR / "SEED_SOURCE.json").read_text(encoding="utf-8"))
        assert m["window_start"] == fwd.FORWARD_START == "2026-09-16"
        assert m["seed_last_run_date"] == "2026-09-15"  # 窗口首日前一交易日收盘
        commit = m["source"]["commit"]
        assert len(commit) == 40 and set(commit) <= set("0123456789abcdef")
        assert set(m["files"]) == set(fwd.SEED_STATE_FILES)

    def test_load_seed_accepts_the_shipped_seed(self):
        seed = fwd.load_seed()
        assert set(seed) == set(fwd.SEED_STATE_FILES)
        assert all(isinstance(b, bytes) and b for b in seed.values())

    def test_the_seed_deliberately_has_no_equity_curve(self):
        """`equity_curve.jsonl` 只写不读；播进去会让窗口前的周也进统计量。"""
        assert not (_SEED_DIR / "equity_curve.jsonl").exists()

    def test_seed_files_are_tracked_by_git(self):
        """种子若只躺在工作区没进 git，本机全绿、别处 `run()` 永远 cannot_judge——
        这一步只隔着一个未被跟踪的文件。128 = 这里不是 git 仓库（导出的源码包），
        「被不被跟踪」无从谈起，skip 正当；其余环境（检出/worktree/CI）都是 git 仓库。"""
        rels = [f"experiments/{fwd.SEED_DIRNAME}/{n}" for n in (*fwd.SEED_STATE_FILES, fwd.SEED_MANIFEST_NAME)]
        r = subprocess.run(["git", "-C", str(_ROOT), "ls-files", "--error-unmatch", "--", *rels],
                           capture_output=True, text=True)
        if r.returncode == 128:
            pytest.skip("不在 git 仓库里（导出的源码包）")
        assert r.returncode == 0, f"种子文件未被 git 跟踪：{r.stderr.strip()}"


# ── 11. `load_seed` / `_validate_seed_state`：坏种子一律 SeedError，不静默 ───────────

class TestLoadSeedRejectsBadSeeds:
    def _good(self, tmp_path):
        d = tmp_path / "seed_ok"
        files = _make_seed(positions=[_pos_row("OLD", "2026-09-10", 5000.0)],
                           closed=[_closed_row("OLD", -50.0)])
        _write_seed_dir(d, files)
        return d, files

    def test_a_valid_seed_loads_byte_for_byte(self, tmp_path):
        d, files = self._good(tmp_path)
        assert fwd.load_seed(d) == files

    def test_tampered_file_fails_the_checksum(self, tmp_path):
        d, _ = self._good(tmp_path)
        (d / "positions.jsonl").write_bytes(b"")
        with pytest.raises(fwd.SeedError, match="sha256"):
            fwd.load_seed(d)

    def test_manifest_missing_a_files_sha_entry_is_refused(self, tmp_path):
        """M11（v0.45.308 独立审查）：`want = ((manifest.get("files") or {}).get(name) or {}).get("sha256")`
        在清单没登记过某个文件时 `want=None`——`_sha256(blob) != None` 恒真所以现行代码正确拒绝，
        但这只是巧合式安全（换成 `if want and _sha256(blob) != want:` 这种更「防御性」的写法就会
        把 `want` 为假值读成「不比对」、静默放行）。这里删掉清单里 positions.jsonl 的整条记录，
        实测钉住当前写法，也防着以后有人把它「优化」成那种更危险的形式。"""
        d, _ = self._good(tmp_path)
        manifest = json.loads((d / fwd.SEED_MANIFEST_NAME).read_text(encoding="utf-8"))
        del manifest["files"]["positions.jsonl"]
        (d / fwd.SEED_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(fwd.SeedError, match="sha256"):
            fwd.load_seed(d)

    def test_missing_manifest_is_a_seed_error(self, tmp_path):
        d, _ = self._good(tmp_path)
        (d / fwd.SEED_MANIFEST_NAME).unlink()
        with pytest.raises(fwd.SeedError, match="清单"):
            fwd.load_seed(d)

    def test_missing_state_file_is_a_seed_error(self, tmp_path):
        d, _ = self._good(tmp_path)
        (d / "closed_trades.jsonl").unlink()
        with pytest.raises(fwd.SeedError, match="closed_trades"):
            fwd.load_seed(d)

    def test_seed_for_a_different_window_start_is_refused(self, tmp_path):
        """改了 FORWARD_START 没重建种子 ⇒ 红，不是拿旧起点悄悄算。"""
        d = tmp_path / "seed_other"
        _write_seed_dir(d, _make_seed(), window_start="2026-09-23")
        with pytest.raises(fwd.SeedError, match="FORWARD_START"):
            fwd.load_seed(d)

    def test_manifest_last_run_date_must_match_meta(self, tmp_path):
        d = tmp_path / "seed_lrd"
        _write_seed_dir(d, _make_seed(), manifest_overrides={"seed_last_run_date": "2026-09-14"})
        with pytest.raises(fwd.SeedError, match="不一致"):
            fwd.load_seed(d)

    @pytest.mark.parametrize("label, kwargs, match", [
        ("cash 非有限（v0.45.97 NaN 事故形状）", {"cash": float("nan")}, "非有限"),
        ("cash 是 inf", {"cash": float("inf")}, "非有限"),
        ("起点不早于窗口首日", {"last_run_date": "2026-09-16"}, "不早于"),
        ("起点晚于窗口首日", {"last_run_date": "2026-09-20"}, "不早于"),
        ("last_run_date 不是日期", {"last_run_date": "yesterday"}, "YYYY-MM-DD"),
        ("持仓 entry_date 晚于种子日期", {"positions": [_pos_row("X", "2026-09-16", 100.0)]}, "晚于"),
        ("持仓 size_usd 非有限", {"positions": [_pos_row("X", "2026-09-10", float("nan"))]}, "非有限"),
        # M8（v0.45.308 独立审查）：上一条只测了 size_usd——若校验被悄悄narrow成只查 size_usd，
        # entry_price/shares 非有限会被放过，size 一样能除出一个 NaN shares 落进 Position。
        ("持仓 entry_price 非有限", {"positions": [_pos_row("X", "2026-09-10", 100.0, entry_price=float("nan"))]}, "非有限"),
        ("平仓 pnl_usd 非有限", {"closed": [_closed_row("X", float("nan"))]}, "pnl_usd"),
    ])
    def test_bad_content_is_refused_even_with_a_matching_checksum(self, tmp_path, label, kwargs, match):
        d = tmp_path / "seed_bad"
        _write_seed_dir(d, _make_seed(**kwargs))
        with pytest.raises(fwd.SeedError, match=match):
            fwd.load_seed(d)

    def test_position_row_that_cannot_build_a_Position_is_refused(self, tmp_path):
        bad = _pos_row("X", "2026-09-10", 100.0)
        del bad["tp_price"]
        d = tmp_path / "seed_badpos"
        _write_seed_dir(d, _make_seed(positions=[bad]))
        with pytest.raises(fwd.SeedError, match="Position"):
            fwd.load_seed(d)

    # v0.45.308 独立审查（F5）：畸形但「校验和对得上」的内容必须一律 `SeedError`，不许在异常
    # 处理器自己的错误信息构造里（比如 `r.get('ticker')`）撞出别的异常类型逃出契约。
    @pytest.mark.parametrize("label, files_override, match", [
        ("positions 行是 JSON 列表", {"positions.jsonl": b"[1,2]\n"}, "对象"),
        ("positions 行是 JSON null", {"positions.jsonl": b"null\n"}, "对象"),
        ("positions 行非法 UTF-8", {"positions.jsonl": b"\xff\xfe\n"}, "UTF-8"),
        ("closed 行是 JSON 列表", {"closed_trades.jsonl": b"[1]\n"}, "对象"),
    ])
    def test_malformed_jsonl_rows_are_seed_errors_not_other_exceptions(self, tmp_path, label, files_override, match):
        d = tmp_path / "seed_malformed"
        base = _make_seed(positions=[_pos_row("OLD", "2026-09-10", 5000.0)])
        _write_seed_dir(d, {**base, **files_override})
        with pytest.raises(fwd.SeedError, match=match):
            fwd.load_seed(d)

    def test_meta_json_with_a_bom_is_a_seed_error_not_a_silent_pass(self, tmp_path):
        """带 UTF-8 BOM 的 `meta.json`：`json.loads(bytes)` 会自动剥掉 BOM 而放行，
        但消费者 `paper_portfolio._load_meta` 用 `read_text` 再 `json.loads(str)`，BOM
        留在字符串里会让它崩溃——「校验过了、重放崩」。校验器必须用同样的解码方式，
        让这类种子在加载阶段就被拒，不是等到重放才炸。"""
        d = tmp_path / "seed_bom"
        base = _make_seed()
        _write_seed_dir(d, {**base, "meta.json": b"\xef\xbb\xbf" + base["meta.json"]})
        with pytest.raises(fwd.SeedError):
            fwd.load_seed(d)

    def test_manifest_that_is_a_json_list_is_a_seed_error(self, tmp_path):
        d = tmp_path / "seed_badmanifest"
        _write_seed_dir(d, _make_seed())
        (d / fwd.SEED_MANIFEST_NAME).write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(fwd.SeedError, match="对象"):
            fwd.load_seed(d)


# ── 12. `_apply_seed` / `evaluate` 对种子的契约 ─────────────────────────────────────

class TestApplySeedAndEvaluateContract:
    def test_apply_seed_writes_exactly_the_seed_files_verbatim(self, tmp_path):
        seed = _make_seed(positions=[_pos_row("OLD", "2026-09-10", 5000.0)])
        fwd._apply_seed(tmp_path / "sb", seed)
        assert {p.name for p in (tmp_path / "sb").iterdir()} == set(fwd.SEED_STATE_FILES)
        for n, b in seed.items():
            assert (tmp_path / "sb" / n).read_bytes() == b

    def test_apply_seed_refuses_a_sandbox_that_already_has_state(self, tmp_path):
        """`run_replay` 对已存在的 state_dir 是「续跑」语义，往里再播种会与之混在一起。"""
        sb = tmp_path / "sb"
        sb.mkdir()
        (sb / "positions.jsonl").write_text("")
        with pytest.raises(FileExistsError):
            fwd._apply_seed(sb, _make_seed())

    def test_apply_seed_refuses_a_sandbox_that_already_has_an_equity_curve(self, tmp_path):
        """M12（v0.45.308 独立审查）：上一条用的是会播的三个文件之一；碰撞检查若只查
        `SEED_STATE_FILES`（这三个），沙箱里已有的 `equity_curve.jsonl`（不在这三个里，
        `run_replay` 续跑同样会读它）就会被漏检。"""
        sb = tmp_path / "sb"
        sb.mkdir()
        (sb / "equity_curve.jsonl").write_text("")
        with pytest.raises(FileExistsError):
            fwd._apply_seed(sb, _make_seed())

    def test_apply_seed_refuses_to_seed_the_equity_curve(self, tmp_path):
        with pytest.raises(ValueError, match="equity_curve"):
            fwd._apply_seed(tmp_path / "sb", {**_make_seed(), "equity_curve.jsonl": b""})

    def test_forward_without_a_seed_is_an_error_not_an_empty_sandbox(self, pp, tmp_path):
        """前瞻模式不带种子 = v0.45.297 之前的设计缺陷。让它在 API 层就不可能。"""
        with pytest.raises(ValueError, match="seed"):
            fwd.evaluate(["2026-09-16"], "2026-09-16", "2026-09-17", tmp_path / "sb", insample=False)

    def test_insample_with_a_seed_is_an_error(self, pp, tmp_path):
        with pytest.raises(ValueError, match="样本内"):
            fwd.evaluate(["2026-09-01"], "2026-09-01", "2026-09-02", tmp_path / "sb",
                         insample=True, seed=_make_seed())


# ── 13. 回归：离线复现真因 ───────────────────────────────────────────────────────────

_W_DATES = ["2026-09-16", "2026-09-17"]
_W_SINCE, _W_BEFORE = "2026-09-16", "2026-09-18"


def _world(pp_fx, tmp_path, *, closed=None):
    """合成的生产世界：窗口起点前生产已持有 OLD（5000）、cash 46000 ⇒ 成本价 NAV 基数 51000，
    而空沙箱是 50000。窗口内 NVDA/AMD 是新候选；OLD 也在候选里，但生产已持有所以不会重开
    ——与真实数据里 SNOW/VKTX 被空沙箱「多开」同一形状。

    「生产实际发生的事」= 从这份种子起跑的 `run_replay`，落到 conftest 隔离出来的生产状态文件。
    返回种子字节。

    ⚠️ 「生产」的起点文件**必须直接写盘，不许走被测的 `fwd._apply_seed`**：否则播种漏写某个文件时
    「生产」与 A 一起漏、两边错得一致，复现率照样 100%（实测：漏播 meta.json〔现金〕时全部集成测试
    仍绿，只有一条单元测试红——自证拿被测对象去证明被测对象）。"""
    _pp, snap_dir, _fg_db = pp_fx
    for d in _W_DATES:
        for t in ("NVDA", "AMD", "OLD"):
            _write_snapshot(snap_dir, t, d, 7.5, "bullish")
    seed = _make_seed(positions=[_pos_row("OLD", "2026-09-10", 5000.0)],
                      closed=closed if closed is not None else [_closed_row("OLD", -50.0)])
    _run_production(pp_fx, tmp_path, seed, _W_DATES)
    return seed


def _run_production(pp_fx, tmp_path, seed, dates, name="prod_run"):
    """「生产」从 `seed` 起**逐日**跑 `dates`，每天收盘留一份状态（= 真实生产状态历史里那天的提交），
    最后把账本与净值曲线落到 conftest 隔离出来的生产状态文件。返回 `{last_run_date: 三个文件的字节}`。
    净值曲线也要落：逐日重锚靠它判断「生产处理过哪几天」（`_production_processed_dates`）。"""
    _pp = pp_fx[0]
    prod = tmp_path / name
    prod.mkdir()
    for n, blob in seed.items():          # 起点直接写盘，不走被测的 _apply_seed（见 `_world`）
        (prod / n).write_bytes(blob)
    history = {}
    for d in dates:
        _pp.run_replay({}, prod, dates=[d])   # 对已存在的目录是「续跑」
        history[d] = {n: (prod / n).read_bytes() for n in fwd.SEED_STATE_FILES}
    _pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    for n, dst in (("closed_trades.jsonl", _pp.CLOSED_FILE), ("positions.jsonl", _pp.POSITIONS_FILE),
                   ("equity_curve.jsonl", _pp.EQUITY_FILE)):
        shutil.copy(prod / n, dst)
    return history


def _world_with_history(pp_fx, tmp_path):
    """同 `_world`，另返回生产逐日收盘状态（给需要「状态历史」的演练 / 逐日重锚测试）。"""
    _pp, snap_dir, _fg_db = pp_fx
    for d in _W_DATES:
        for t in ("NVDA", "AMD", "OLD"):
            _write_snapshot(snap_dir, t, d, 7.5, "bullish")
    seed = _make_seed(positions=[_pos_row("OLD", "2026-09-10", 5000.0)], closed=[_closed_row("OLD", -50.0)])
    return seed, _run_production(pp_fx, tmp_path, seed, _W_DATES)


class TestDefaultGateAssumptionIsChecked:
    """F4（v0.45.308 独立审查）：A 定义为 `run_replay({}, ...)`——不覆盖任何键，隐含假设生产
    默认 `fg_exposure_gate.enabled=False`。此前没有任何测试钉住这个假设：若它被打破（比如
    已经有人把默认改成 True 却没退役本脚本），A 会悄悄变成跟 B 同一回事，ΔNAV≈0 会被误读成
    「门没有效应」而不是「A/B 的定义已经重合」——是静默失效，不是显式失败。"""

    def test_a_is_cannot_judge_if_production_default_silently_flipped_to_true(self, pp, tmp_path, monkeypatch):
        _pp = pp[0]
        # ⚠️ 不能写成 `monkeypatch.setitem(_pp.CONFIG["fg_exposure_gate"], "enabled", True)`：
        # `run_replay` 的 finally 是 `CONFIG.clear(); CONFIG.update(_orig_config)`，会把
        # `CONFIG["fg_exposure_gate"]` **整体替换**成另一个 dict 对象，不是原地改值——嵌套
        # setitem 记的是旧对象的引用，teardown 时改的是那个已经被换掉、没人再引用的旧对象，
        # 真正生效的 CONFIG 就永久卡在 True，污染这个文件里后面所有测试（实测踩过一次：
        # 本类下一条"正对照"测试单独跑绿、跟在这条后面跑就红）。改在**顶层键**上
        # setitem，顶层 `CONFIG` 对象本身从不被换引用，teardown 才真的复原。
        monkeypatch.setitem(_pp.CONFIG, "fg_exposure_gate", {**_pp.CONFIG["fg_exposure_gate"], "enabled": True})
        _write_snapshot(pp[1], "NVDA", "2026-09-16", 7.5, "bullish")
        res = fwd.evaluate(["2026-09-16"], "2026-09-16", "2026-09-17", tmp_path / "sb", insample=False, seed={})
        assert res["status"] == "cannot_judge"
        assert "True" in res["reason"]

    def test_default_disabled_does_not_trip_the_guard(self, pp, tmp_path):
        """正对照：默认确实是 False 时，这道新守卫不该拦下正常场景。"""
        _write_snapshot(pp[1], "NVDA", "2026-09-16", 7.5, "bullish")
        res = fwd.evaluate(["2026-09-16"], "2026-09-16", "2026-09-17", tmp_path / "sb", insample=False, seed={})
        assert res["status"] != "cannot_judge"


class TestSeededReplayReproducesProduction:
    def test_empty_sandbox_reproduces_decisions_but_not_amounts(self, pp, tmp_path):
        """**这就是 09-16~09-18 那次「0/8」的离线复现**：决策层 100%、金额层 0%。"""
        _world(pp, tmp_path)
        res = fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, tmp_path / "sb_empty", insample=False, seed={})
        assert res["status"] == "cannot_judge"
        sp = res["selfproof"]
        assert sp["reproduced"] == 0
        assert sp["decision_reproduced"] == sp["real_entries"] == 2
        # 文案要把原因指对层：决策层复现了 ⇒ 别怀疑评分链，去查金额（起点状态等）
        assert "决策层" in res["reason"] and "仓位金额" in res["reason"]
        assert "评分链" not in res["reason"]

    def test_seeded_sandbox_reproduces_production_to_the_cent(self, pp, tmp_path):
        seed = _world(pp, tmp_path)
        res = fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, tmp_path / "sb", insample=False, seed=seed)
        assert res["selfproof"] == {"real_entries": 2, "reproduced": 2, "decision_reproduced": 2,
                                    "a_only_entries": 0}
        assert res["selfproof_rate"] == 1.0
        assert res["status"] == "not_ready"  # 自证过了，进入统计阶段（周数不够，如实说继续攒）
        assert res["seed_last_run_date"] == "2026-09-15"

    def test_seeded_cash_and_positions_set_the_sizing_base(self, pp, tmp_path):
        """机制本身，不依赖任何「生产」：新仓位 ∝ nav 基数 = cash + Σ size_usd。
        种子里 cash 46000 + OLD 5000 = 51000，空沙箱是 50000 ⇒ 同一只票的仓位差恰为 51000/50000。"""
        _pp = pp[0]
        seed = _world(pp, tmp_path)

        def nvda_size(label, s):
            d = tmp_path / label
            res = fwd._replay_variant({}, d, _W_DATES, seed=s)
            return next(p["size_usd"] for p in res["open_positions"] if p["ticker"] == "NVDA")
        assert nvda_size("seeded", seed) / nvda_size("empty", {}) == pytest.approx(51000.0 / 50000.0, rel=1e-3)

    def test_both_variants_start_from_the_same_seeded_book(self, pp, tmp_path):
        """B 若不播种，「治疗组 vs 对照组」就不再是同一个起点上的比较。"""
        _pp = pp[0]
        seed = _world(pp, tmp_path)
        sb = tmp_path / "sb"
        fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, sb, insample=False, seed=seed)
        for variant in ("A_baseline", "B_treatment"):
            assert "OLD" in {p["ticker"] for p in _pp._load_jsonl(sb / variant / "positions.jsonl")}
            hist = _pp._load_jsonl(sb / variant / "closed_trades.jsonl")
            assert any(t["ticker"] == "OLD" and t["pnl_usd"] == -50.0 for t in hist)

    def test_seeded_variants_share_the_same_cash_basis(self, pp, tmp_path):
        """M2（v0.45.308 独立审查）：上一条只核了 B 的 `positions.jsonl`/`closed_trades.jsonl`
        带着 OLD，没核 `meta.json`（现金）——B 若漏播现金，会静默退回 `CONFIG["starting_capital"]`
        （$50000）而不是种子的真实现金（$34147+OLD $16264≈$50411）。`_world` 不设 F&G，门从未
        触发，所以 A/B 的仓位大小理应逐分相同；不同就说明 B 的现金基数没跟着种子走。"""
        _pp = pp[0]
        seed = _world(pp, tmp_path)
        sb = tmp_path / "sb"
        fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, sb, insample=False, seed=seed)
        a_pos = {p["ticker"]: p["size_usd"] for p in _pp._load_jsonl(sb / "A_baseline" / "positions.jsonl")}
        b_pos = {p["ticker"]: p["size_usd"] for p in _pp._load_jsonl(sb / "B_treatment" / "positions.jsonl")}
        assert a_pos and a_pos == b_pos, f"门未触发时 A/B 仓位应逐分相同：A={a_pos} B={b_pos}"

    def test_seeded_equity_curve_covers_the_window_only(self, pp, tmp_path):
        """统计量的周度收益率只该来自窗口内的净值——种子里若带着窗口前的净值行，会让窗口前的周
        也进检验（合格周数被虚增，α 花在没有效应可测的周上）。"""
        _pp = pp[0]
        seed = _world(pp, tmp_path)
        sb = tmp_path / "sb"
        fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, sb, insample=False, seed=seed)
        for variant in ("A_baseline", "B_treatment"):
            rows = _pp._load_jsonl(sb / variant / "equity_curve.jsonl")
            assert [r["date"] for r in rows] == _W_DATES

    def test_closed_history_reaches_the_position_sizer(self, pp, tmp_path, monkeypatch):
        """`closed_trades.jsonl` 是**潜伏依赖**：仓位乘数按标的历史胜率算，现行 CONFIG 三档都是 1.0
        所以不播它也复现得了——直到有人改了 `win_rate_multiplier`。用非 1.0 的乘数把它咬出来。"""
        _pp = pp[0]
        monkeypatch.setitem(_pp.CONFIG, "min_samples_for_win_rate", 1)
        monkeypatch.setitem(_pp.CONFIG, "win_rate_multiplier", {"strong": 1.0, "normal": 1.0, "weak": 0.5})
        seed = _world(pp, tmp_path, closed=[_closed_row("OLD", -50.0), _closed_row("NVDA", -80.0),
                                            _closed_row("AMD", -80.0)])
        ok = fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, tmp_path / "sb_ok", insample=False, seed=seed)
        assert ok["selfproof"]["reproduced"] == 2
        no_hist = {k: v for k, v in seed.items() if k != "closed_trades.jsonl"}
        bad = fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, tmp_path / "sb_bad", insample=False, seed=no_hist)
        assert bad["selfproof"]["reproduced"] == 0          # 金额差一倍
        assert bad["selfproof"]["decision_reproduced"] == 2  # 决策层仍对——正是这类差异的形状

    def test_selfproof_still_goes_red_when_the_scoring_or_config_drifts(self, pp, tmp_path, monkeypatch):
        """播种不许把自证变成恒绿：A 的配置在期间被改动，必须仍然红（且文案指向评分链/配置）。"""
        _pp = pp[0]
        seed = _world(pp, tmp_path)
        monkeypatch.setitem(_pp.CONFIG, "entry_score_bull", 9.0)  # 生产当时是 6.5：A 一笔都不开
        res = fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, tmp_path / "sb", insample=False, seed=seed)
        assert res["status"] == "cannot_judge"
        assert res["selfproof"]["decision_reproduced"] == 0
        assert "评分链" in res["reason"]


class TestSelfproofReasonNamesTheLayer:
    K = ("NVDA", "2026-09-16", "bullish")

    def test_stats_separate_the_two_layers(self):
        sp = fwd._selfproof_stats({(*self.K, 100.0), ("AMD", "2026-09-16", "bullish", 50.0)},
                                  {(*self.K, 101.0), ("AMD", "2026-09-16", "bullish", 50.0)})
        assert (sp["total"], sp["exact"], sp["decision"]) == (2, 1, 2)
        assert sp["rate"] == 0.5 and sp["decision_rate"] == 1.0

    def test_amounts_wrong_decisions_right(self):
        sp = fwd._selfproof_stats({(*self.K, 100.0)}, {(*self.K, 101.0)})
        r = fwd._selfproof_failure_reason(sp)
        assert "0/1" in r and "决策层" in r and "1/1" in r and "仓位金额" in r
        assert "评分链" not in r

    def test_decisions_wrong_too(self):
        sp = fwd._selfproof_stats({(*self.K, 100.0)}, {("AMD", "2026-09-16", "bullish", 100.0)})
        r = fwd._selfproof_failure_reason(sp)
        assert "评分链" in r and "0/1" in r
        assert "仓位金额" not in r

    def test_decision_layer_does_not_conflate_opposite_directions(self):
        """M4（v0.45.308 独立审查）：决策层键是 (ticker, entry_date, direction) 三元组——
        同一标的同一天但方向相反，必须记成决策层不吻合，不能被当成「只是金额差」。"""
        real = {("NVDA", "2026-09-16", "bullish", 100.0)}
        a = {("NVDA", "2026-09-16", "bearish", 100.0)}  # 标的/日期都对，方向翻了
        sp = fwd._selfproof_stats(real, a)
        assert sp["exact"] == 0
        assert sp["decision"] == 0, "方向相反不该被判成「决策层复现」"

    def test_decision_rate_exactly_at_threshold_is_treated_as_passing(self):
        """M13b（v0.45.308 独立审查）：分层文案判「决策层够不够格」用 `>=`，不是 `>`——
        恰好等于阈值时该走「金额差」分支，不该被当成「决策层也不够格」而错报评分链被改。"""
        sp = {"total": 20, "exact": 10, "decision": 19, "rate": 0.5, "decision_rate": fwd.SELFPROOF_MIN_RATE}
        r = fwd._selfproof_failure_reason(sp)
        assert "仓位金额" in r and "评分链" not in r


class TestSelfproofThresholdBoundary:
    """M13a（v0.45.308 独立审查）：`SELFPROOF_MIN_RATE=0.95` 是「< 才判红」，即 `>=` 通过——
    直接控制 `_selfproof_stats` 的返回值，绕开真实复现率难以精确命中 0.95 的问题。"""

    def _run(self, pp, tmp_path, monkeypatch, rate):
        monkeypatch.setattr(fwd, "_real_recorded_entries", lambda *a, **k: {("X", "2026-09-16", "bullish", 1.0)})
        monkeypatch.setattr(fwd, "_selfproof_stats",
                            lambda real, a: {"total": 1, "exact": 1, "decision": 1, "a_only": 0,
                                             "rate": rate, "decision_rate": 1.0})
        _write_snapshot(pp[1], "NVDA", "2026-09-16", 7.5, "bullish")
        return fwd.evaluate(["2026-09-16"], "2026-09-16", "2026-09-17", tmp_path / "sb", insample=False, seed={})

    def test_rate_exactly_at_threshold_is_not_cannot_judge(self, pp, tmp_path, monkeypatch):
        res = self._run(pp, tmp_path, monkeypatch, fwd.SELFPROOF_MIN_RATE)
        assert res["status"] != "cannot_judge"

    def test_rate_just_below_threshold_is_cannot_judge(self, pp, tmp_path, monkeypatch):
        res = self._run(pp, tmp_path, monkeypatch, fwd.SELFPROOF_MIN_RATE - 0.001)
        assert res["status"] == "cannot_judge"


class TestSnapshotWindowBoundary:
    def test_snapshot_window_excludes_the_before_boundary(self, monkeypatch):
        """M15（v0.45.308 独立审查）：窗口是左闭右开 [since, before)——`before` 当天的快照
        不该被算进「窗口内」，否则窗口宽度会悄悄多算一天。"""
        import paper_portfolio as _pp
        monkeypatch.setattr(_pp, "_all_snapshot_dates", lambda: ["2026-09-15", "2026-09-16", "2026-09-17"])
        got = fwd._snapshot_dates_in_window("2026-09-15", "2026-09-17")
        assert got == ["2026-09-15", "2026-09-16"]


# ── 14. `run()` 接线：种子不可用 ⇒ 无法判定，绝不退回空沙箱 ──────────────────────────

class TestRunSeedWiring:
    def _forward_setup(self, pp):
        _pp, snap_dir, _ = pp
        _write_snapshot(snap_dir, "NVDA", "2026-09-16", 7.5, "bullish")

    def test_unusable_seed_is_cannot_judge_and_never_falls_back_to_empty(self, pp, monkeypatch):
        self._forward_setup(pp)

        def _boom(*a, **k):
            raise fwd.SeedError("测试：种子坏了")
        monkeypatch.setattr(fwd, "load_seed", _boom)
        monkeypatch.setattr(fwd, "evaluate", lambda *a, **k: pytest.fail("不许在没有种子时仍去重放"))
        res = fwd.run(today="2026-09-17")
        assert res["status"] == "cannot_judge"
        assert "种子" in res["reason"] and "测试：种子坏了" in res["reason"]

    def test_the_loaded_seed_is_handed_to_evaluate(self, pp, monkeypatch):
        self._forward_setup(pp)
        sentinel = _make_seed()
        seen = {}
        monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: sentinel)

        plan_sentinel = {"segments": [], "anchors": {}, "info": {}}
        monkeypatch.setattr(fwd, "_forward_anchor_plan", lambda s, d: plan_sentinel if s is sentinel else pytest.fail(s))

        def _fake_eval(dates, since, before, root, *, insample=False, seed=None, plan=None):
            seen.update(insample=insample, seed=seed, plan=plan)
            return {"status": "not_ready"}
        monkeypatch.setattr(fwd, "evaluate", _fake_eval)
        fwd.run(today="2026-09-17")
        assert seen == {"insample": False, "seed": sentinel, "plan": plan_sentinel}

    def test_insample_never_touches_the_seed(self, pp, monkeypatch):
        """样本内从 `bootstrap_date` 起，生产当时是空状态——加载种子在这里是错的。"""
        self._forward_setup(pp)
        monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: pytest.fail("样本内不该加载种子"))
        seen = {}

        def _fake_eval(dates, since, before, root, *, insample=False, seed=None, plan=None):
            seen.update(insample=insample, seed=seed)
            assert plan is None
            return {"status": "insample"}
        monkeypatch.setattr(fwd, "evaluate", _fake_eval)
        fwd.run(insample=True, today="2026-09-17")
        assert seen == {"insample": True, "seed": None}

    def test_no_forward_dates_is_not_ready_even_if_the_seed_is_broken(self, pp, monkeypatch):
        """「还没有前瞻样本」与「种子坏了」是两种失败，不许互相掩盖：没样本时先说没样本。"""
        _pp, snap_dir, _ = pp
        _write_snapshot(snap_dir, "NVDA", "2026-03-09", 7.0, "bullish")  # 早于 FORWARD_START
        monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: pytest.fail("没样本时不该去加载种子"))
        assert fwd.run(today="2026-09-17")["status"] == "not_ready"


# ── 15. `build_seed_from_git`：定位规则（合成 git 仓库，环境自造 ⇒ 无需 skip）──────────

_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}


def _synthetic_git_env():
    """v0.45.308 独立审查（I-1）：若 pytest 从 git hook 里被拉起，继承的 `GIT_DIR` 等会让下面
    每条对**合成仓库**跑的 git 命令打到**真仓库**上——本仓另外 4 个造合成 git 仓库的测试文件
    （`test_github_tool_status.py`/`test_github_tool_commit.py`/`test_git_failures_are_visible.py`/
    `test_production_sync.py`）都已防了这一点，本文件此前没有。配对对照实测：一次性克隆里带
    `GIT_DIR` 跑本文件的 `TestBuildSeedFromGit`，克隆的提交数从 1284 涨到 1316（多出 32 个垃圾提交）；
    不带 `GIT_DIR` 跑，克隆一个提交都不多。"""
    env = {k: v for k, v in os.environ.items()
          if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")}
    env.update(_GIT_ENV)
    return env


def _state_at(lrd, cash, positions=(), closed=()):
    seed = _make_seed(cash=cash, positions=positions, closed=closed, last_run_date=lrd)
    return {f"paper_portfolio_state/{n}": b for n, b in seed.items()}


def _git_repo(tmp_path, commits, name="repo"):
    """commits：按顺序的 {相对路径: 字节 | None(删除)}。返回 (repo, [各提交 sha])。"""
    repo = tmp_path / name
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                              env=_synthetic_git_env())
    git("init", "-q")
    shas = []
    for i, files in enumerate(commits):
        for rel, blob in files.items():
            p = repo / rel
            if blob is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(blob)
        git("add", "-A")
        git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", f"c{i}")
        shas.append(git("rev-parse", "HEAD").stdout.decode().strip())
    return repo, shas


def _cash_of(files):
    return json.loads(files["meta.json"])["cash"]


class TestBuildSeedFromGit:
    def test_picks_the_state_right_before_the_first_commit_that_reaches_the_window(self, tmp_path):
        repo, shas = _git_repo(tmp_path, [
            _state_at("2026-09-11", 1000.0), _state_at("2026-09-14", 2000.0),
            _state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0), _state_at("2026-09-17", 5000.0)])
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        assert _cash_of(files) == 3000.0
        assert manifest["source"]["commit"] == shas[2]
        assert manifest["window_start"] == "2026-09-16" and manifest["seed_last_run_date"] == "2026-09-15"
        assert manifest["files"]["meta.json"]["sha256"] == hashlib.sha256(files["meta.json"]).hexdigest()

    def test_same_day_reruns_take_the_last_state_before_the_window(self, tmp_path):
        repo, shas = _git_repo(tmp_path, [
            _state_at("2026-09-14", 2000.0), _state_at("2026-09-15", 3000.0),
            _state_at("2026-09-15", 3100.0), _state_at("2026-09-16", 4000.0)])
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        assert _cash_of(files) == 3100.0 and manifest["source"]["commit"] == shas[2]

    def test_a_rollback_after_the_window_does_not_hijack_the_seed(self, tmp_path):
        """窗口之后有人把状态还原到更早日期（备份恢复）——「last_run_date < since 的**最新**提交」
        会被它带偏；按「第一个跨过窗口首日的提交」定位则不受影响。"""
        repo, shas = _git_repo(tmp_path, [
            _state_at("2026-09-14", 2000.0), _state_at("2026-09-15", 3000.0),
            _state_at("2026-09-16", 4000.0), _state_at("2026-09-02", 999.0)])
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        assert _cash_of(files) == 3000.0 and manifest["source"]["commit"] == shas[1]

    def test_a_deleted_then_readded_state_dir_is_not_a_crash(self, tmp_path):
        """数据根迁移阶段 5 会 `git rm --cached`：删除提交上 `git show` 会失败，必须被排除。"""
        gone = {k: None for k in _state_at("2026-09-14", 1.0)}
        repo, shas = _git_repo(tmp_path, [
            _state_at("2026-09-14", 2000.0), gone, _state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0)])
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        assert _cash_of(files) == 3000.0 and manifest["source"]["commit"] == shas[2]

    def test_shallow_clone_is_refused_not_misread_as_no_state(self, tmp_path):
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-14", 2000.0), _state_at("2026-09-15", 3000.0),
                                       _state_at("2026-09-16", 4000.0)])
        shallow = tmp_path / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)],
                       check=True, capture_output=True, env=_synthetic_git_env())
        with pytest.raises(fwd.SeedError, match="浅克隆"):
            fwd.build_seed_from_git("2026-09-16", shallow)

    def test_window_before_any_production_state_is_refused(self, tmp_path):
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-11", 1000.0), _state_at("2026-09-14", 2000.0)])
        with pytest.raises(fwd.SeedError, match="之前生产没有任何状态"):
            fwd.build_seed_from_git("2026-09-01", repo)

    def test_window_production_never_reached_is_refused(self, tmp_path):
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-11", 1000.0), _state_at("2026-09-14", 2000.0)])
        with pytest.raises(fwd.SeedError, match="尚未运行"):
            fwd.build_seed_from_git("2026-09-30", repo)

    def test_a_non_finite_seed_state_is_refused(self, tmp_path):
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-15", float("nan")), _state_at("2026-09-16", 4000.0)])
        with pytest.raises(fwd.SeedError, match="非有限"):
            fwd.build_seed_from_git("2026-09-16", repo)

    def test_not_a_git_repo_is_a_seed_error_not_a_crash(self, tmp_path):
        (tmp_path / "plain").mkdir()
        with pytest.raises(fwd.SeedError):
            fwd.build_seed_from_git("2026-09-16", tmp_path / "plain")

    def test_git_helper_raises_on_nonzero_exit_code(self, monkeypatch, tmp_path):
        """M3（v0.45.308 独立审查）：`_git()` 若不检查退出码，`git show` 在坏对象上失败时返回的
        空 stdout 会被当成正常输出往下传——上游拿着这段空字节继续解析，得到的是一个更难查的
        「文件为空/解析失败」，而不是「git 命令本身失败了」。"""
        class _Fake:
            returncode = 128
            stdout = b""
            stderr = b"fatal: bad object deadbeef"
        monkeypatch.setattr(fwd.subprocess, "run", lambda *a, **k: _Fake())
        with pytest.raises(fwd.SeedError, match="exit 128"):
            fwd._git(tmp_path, "show", "deadbeef:foo")

    def test_a_commit_with_non_string_last_run_date_is_skipped_not_crashed(self, tmp_path):
        """M9（v0.45.308 独立审查）：中间提交的 `last_run_date` 不是字符串（手改成了数字，或
        字段缺失变成 `None`）——不能让 `lrd >= since` 在 `None`/`int` 上做比较崩溃，必须
        优雅跳过这个提交，继续找后面真正合格的那个。"""
        malformed = {"paper_portfolio_state/meta.json": b'{"cash": 1.0, "last_run_date": 42}',
                    "paper_portfolio_state/positions.jsonl": b"", "paper_portfolio_state/closed_trades.jsonl": b""}
        repo, shas = _git_repo(tmp_path, [malformed, _state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0)])
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        assert _cash_of(files) == 3000.0 and manifest["source"]["commit"] == shas[1]

    def test_a_commit_with_unparseable_meta_is_skipped_not_crashed(self, tmp_path):
        """M10（v0.45.308 独立审查）：中间提交的 `meta.json` 干脆不是合法 JSON——同样必须跳过，
        不能让 `json.loads` 的 `ValueError` 逃出 `build_seed_from_git`。"""
        broken = {"paper_portfolio_state/meta.json": b"{not json",
                 "paper_portfolio_state/positions.jsonl": b"", "paper_portfolio_state/closed_trades.jsonl": b""}
        repo, shas = _git_repo(tmp_path, [broken, _state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0)])
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        assert _cash_of(files) == 3000.0 and manifest["source"]["commit"] == shas[1]

    def test_default_repo_root_follows_paths_git_repo_root(self, tmp_path, monkeypatch):
        """git 仓库根走 `PATHS.git_repo_root`（v0.45.268 专为 git plumbing 与数据根解耦而设）。"""
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0)])
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(repo))
        files, _ = fwd.build_seed_from_git("2026-09-16")
        assert _cash_of(files) == 3000.0

    def test_written_seed_round_trips_through_load_seed(self, tmp_path):
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0)])
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        out = tmp_path / "frozen"
        fwd.write_seed_dir(out, files, manifest)
        assert fwd.load_seed(out, forward_start="2026-09-16") == files

    def test_shipped_git_helper_strips_inherited_git_dir_vars(self, tmp_path, monkeypatch):
        """v0.45.308 独立审查（I-1）：脚本自己的 `_git()` 此前直接继承 `os.environ`——若本进程
        是从 pre-commit/pre-push 钩子或 `git rebase -x` 里拉起来的（继承了 `GIT_DIR` 等），
        每条 `git -C <repo>` 命令的 `-C` 会被 `GIT_DIR` 盖过，实际打到那个钩子的真仓库上。
        不跑昂贵的一次性克隆实验（那个只在本地手工核实过一次），这里 spy `subprocess.run`
        的 `env=` 参数，核对四个变量确实被剥掉了——`fwd` 与本测试文件用的是同一个 `subprocess`
        模块对象，这里顺带也核了 `_git_repo` 自己的调用。"""
        monkeypatch.setenv("GIT_DIR", "/should/not/leak/.git")
        monkeypatch.setenv("GIT_WORK_TREE", "/should/not/leak")
        seen_envs = []
        orig_run = fwd.subprocess.run

        def spy(*a, **k):
            if a and a[0] and a[0][0] == "git":
                seen_envs.append(k.get("env"))
            return orig_run(*a, **k)
        monkeypatch.setattr(fwd.subprocess, "run", spy)
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0)])
        fwd.build_seed_from_git("2026-09-16", repo)
        assert seen_envs, "没有观察到任何 git 调用——测试没测到东西"
        for env in seen_envs:
            assert env is not None
            assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env

    def test_write_seed_dir_refuses_to_overwrite_a_frozen_seed(self, tmp_path):
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0)])
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        out = tmp_path / "frozen"
        fwd.write_seed_dir(out, files, manifest)
        before = (out / "meta.json").read_bytes()
        with pytest.raises(FileExistsError):
            fwd.write_seed_dir(out, {**files, "meta.json": b"{}"}, manifest)
        assert (out / "meta.json").read_bytes() == before


# ── 16. `--rehearse`：只重放 A，不出统计量 ────────────────────────────────────────────

class TestRehearse:
    def _synthetic_repo(self, tmp_path, seed, history):
        """窗口起点前一状态（= 种子）+ 生产逐日收盘状态（v0.45.408 起它们是逐日重锚的锚点，内容必须真实）。"""
        commits = [{f"paper_portfolio_state/{n}": b for n, b in seed.items()}]
        commits += [{f"paper_portfolio_state/{n}": b for n, b in history[d].items()} for d in sorted(history)]
        return _git_repo(tmp_path, commits, name="rehearse_repo")[0]

    def _rehearse(self, repo, **kw):
        return fwd.rehearse(_W_SINCE, _W_BEFORE, repo_root=repo, backup_repo=repo, **kw)

    def test_green_when_a_reproduces_production(self, pp, tmp_path):
        seed, hist = _world_with_history(pp, tmp_path)
        res = self._rehearse(self._synthetic_repo(tmp_path, seed, hist))
        assert res["status"] == "rehearsal_ok"
        assert res["selfproof"] == {"real_entries": 2, "reproduced": 2, "decision_reproduced": 2,
                                    "a_only_entries": 0}
        assert res["seed_last_run_date"] == "2026-09-15"

    def test_never_runs_variant_b_and_leaks_no_effect_size(self, pp, tmp_path, monkeypatch):
        """演练只回答「A 还能不能复现生产」——跑了 B 或带出统计量，就等于提前偷看效应。"""
        seed, hist = _world_with_history(pp, tmp_path)
        repo = self._synthetic_repo(tmp_path, seed, hist)
        configs = []
        orig = fwd._replay_variant

        def spy(cfg, *a, **k):
            configs.append(cfg)
            return orig(cfg, *a, **k)
        monkeypatch.setattr(fwd, "_replay_variant", spy)
        res = self._rehearse(repo)
        assert configs and all(c == {} for c in configs), configs  # 只有 A（默认配置）：连续一次 + 逐日各一次
        assert not (_EFFECT_KEYS & set(_all_keys(res)))

    def test_red_when_the_config_drifted(self, pp, tmp_path, monkeypatch):
        _pp = pp[0]
        seed, hist = _world_with_history(pp, tmp_path)
        repo = self._synthetic_repo(tmp_path, seed, hist)
        monkeypatch.setitem(_pp.CONFIG, "entry_score_bull", 9.0)
        res = self._rehearse(repo)
        assert res["status"] == "cannot_judge" and "评分链" in res["reason"]

    def test_judges_by_exact_rate_not_decision_rate(self, pp, tmp_path, monkeypatch):
        """M1（v0.45.308 独立审查）：判定若误用决策层率，「决策层对、金额层错」的场景会被误判为
        `rehearsal_ok`——正是本次真因「0/8」同一种形状，只是从 `evaluate()` 挪到了 `rehearse()`。
        只改仓位大小、不改开哪只/哪个方向，制造「决策层对、精确层错」。"""
        _pp = pp[0]
        seed, hist = _world_with_history(pp, tmp_path)
        repo = self._synthetic_repo(tmp_path, seed, hist)
        # 顶层键 setitem，不是嵌套 setitem——`run_replay` 的 finally 整体替换
        # `CONFIG["size_pct_by_tier"]`，嵌套 setitem 会在 teardown 时改到被换掉的旧对象上，
        # 真正生效的 CONFIG 卡在 4.0 不还原（同 `TestDefaultGateAssumptionIsChecked` 那条注释）。
        monkeypatch.setitem(_pp.CONFIG, "size_pct_by_tier", {**_pp.CONFIG["size_pct_by_tier"], "high": 4.0})
        res = self._rehearse(repo)
        assert res["selfproof_decision_rate"] == 1.0
        assert res["selfproof_rate"] < 1.0
        assert res["status"] == "cannot_judge"

    def test_selfproof_reason_gets_the_window_counters(self, pp, tmp_path, monkeypatch):
        """二次复审（S3 在演练路径）：rehearse() 必须把窗口计数交给 `_selfproof_failure_reason`——
        否则行情全断时演练会把原因推给评分链。变异「rehearse 里丢掉 ohlc 参数」⇒ 红。"""
        _pp = pp[0]
        seed, hist = _world_with_history(pp, tmp_path)
        repo = self._synthetic_repo(tmp_path, seed, hist)
        monkeypatch.setitem(_pp.CONFIG, "entry_score_bull", 9.0)
        seen = []
        orig = fwd._selfproof_failure_reason

        def spy(sp, ohlc=None, segments=None):
            seen.append(ohlc)
            return orig(sp, ohlc, segments)
        monkeypatch.setattr(fwd, "_selfproof_failure_reason", spy)
        res = self._rehearse(repo)
        assert res["status"] == "cannot_judge" and len(seen) == 1
        assert isinstance(seen[0], dict) and seen[0] == res["ohlc_window"] and "served" in seen[0]

    def test_unobtainable_start_state_is_cannot_judge(self, pp, tmp_path):
        (tmp_path / "plain").mkdir()
        res = fwd.rehearse(_W_SINCE, _W_BEFORE, repo_root=tmp_path / "plain")
        assert res["status"] == "cannot_judge" and "无法取窗口起点状态" in res["reason"]

    def test_nothing_to_compare_is_not_ready(self, pp, tmp_path):
        """窗口内生产没有开仓记录 ⇒ 没有可比对的东西，不是「通过」。"""
        repo, _ = _git_repo(tmp_path, [_state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0)],
                            name="empty_win_repo")
        _write_snapshot(pp[1], "NVDA", "2026-09-16", 7.5, "bullish")
        res = self._rehearse(repo)
        assert res["status"] == "not_ready"


# ── 17. CLI ───────────────────────────────────────────────────────────────────────────

class TestCli:
    def test_build_seed_writes_once_and_refuses_to_overwrite(self, tmp_path, monkeypatch, capsys):
        out = tmp_path / "frozen"
        files = _make_seed()
        manifest = {"schema": 1, "window_start": fwd.FORWARD_START, "seed_last_run_date": "2026-09-15",
                    "source": {"commit": "a" * 40}, "files": {}}
        monkeypatch.setattr(fwd, "_seed_dir", lambda *a, **k: out)
        monkeypatch.setattr(fwd, "build_seed_from_git", lambda since, *a, **k: (files, manifest))
        assert fwd.main(["--build-seed"]) == 0
        assert (out / "meta.json").read_bytes() == files["meta.json"]
        marker = (out / "meta.json").read_bytes()
        monkeypatch.setattr(fwd, "build_seed_from_git", lambda since, *a, **k: ({**files, "meta.json": b"{}"}, manifest))
        assert fwd.main(["--build-seed"]) == 3
        assert (out / "meta.json").read_bytes() == marker
        assert "拒绝覆盖" in capsys.readouterr().err

    @pytest.mark.parametrize("status, code", [("rehearsal_ok", 0), ("not_ready", 1), ("cannot_judge", 3)])
    def test_rehearse_exit_codes(self, monkeypatch, status, code):
        monkeypatch.setattr(fwd, "rehearse", lambda since, before, **k: {"status": status, "mode": "rehearse"})
        assert fwd.main(["--rehearse", "2026-09-09", "2026-09-16"]) == code

    def test_unexpected_crash_returns_3_not_1(self, monkeypatch, capsys):
        """F6（v0.45.308 独立审查）：未捕获异常此前与「未就绪」共用退出码 1，两者无法区分——
        崩溃必须报 3，绝不能悄悄读成「正常等待」。"""
        monkeypatch.setattr(fwd, "run", lambda **k: (_ for _ in ()).throw(RuntimeError("模拟崩溃")))
        assert fwd.main([]) == 3
        assert "崩溃" in capsys.readouterr().err

    def test_rehearse_passes_its_two_dates_through(self, monkeypatch):
        seen = []
        monkeypatch.setattr(fwd, "rehearse", lambda since, before, **k: seen.append((since, before)) or {"status": "rehearsal_ok"})
        fwd.main(["--rehearse", "2026-09-09", "2026-09-16"])
        assert seen == [("2026-09-09", "2026-09-16")]

    def test_modes_are_mutually_exclusive(self):
        with pytest.raises(SystemExit) as e:
            fwd.main(["--insample", "--build-seed"])
        assert e.value.code == 2

    def test_rehearse_output_reports_both_layers(self, capsys):
        fwd._print_human({"mode": "rehearse", "status": "cannot_judge", "since": "2026-09-09",
                          "before": "2026-09-16", "seed_commit": "abcdef1234", "seed_last_run_date": "2026-09-08",
                          "n_dates": 5, "reason": "x",
                          "selfproof": {"real_entries": 5, "reproduced": 0, "decision_reproduced": 5}})
        out = capsys.readouterr().out
        assert "精确复现 0/5" in out and "决策层复现 5/5" in out

    @pytest.mark.parametrize("status, must_contain, must_not_contain", [
        ("rehearsal_ok", "✅ 自证通过", "❌"),
        ("cannot_judge", "❌", "✅ 自证通过"),
    ])
    def test_rehearse_conclusion_line_matches_the_status(self, capsys, status, must_contain, must_not_contain):
        """M5（v0.45.308 独立审查）：人读结论行若把判定逻辑反过来写，会对 `cannot_judge`
        打印「✅ 自证通过」——把失败渲染成了成功，比不打印更危险。"""
        fwd._print_human({"mode": "rehearse", "status": status, "since": "2026-09-09", "before": "2026-09-16",
                          "seed_commit": "abcdef1234", "seed_last_run_date": "2026-09-08", "n_dates": 5,
                          "reason": "x", "selfproof": {"real_entries": 5, "reproduced": 5, "decision_reproduced": 5}})
        out = capsys.readouterr().out
        assert must_contain in out
        assert must_not_contain not in out

    def test_build_seed_passes_the_registered_window_start(self, tmp_path, monkeypatch):
        """M6（v0.45.308 独立审查）：`--build-seed` 是一次性且拒绝覆盖的——传错窗口起点会冻结出
        一份错种子且无法重建，必须确认传给 `build_seed_from_git` 的就是 `FORWARD_START`，
        不是随便一个位置参数。"""
        seen = []
        monkeypatch.setattr(fwd, "_seed_dir", lambda *a, **k: tmp_path / "frozen")
        monkeypatch.setattr(fwd, "build_seed_from_git",
                            lambda since, *a, **k: (seen.append(since) or _make_seed(),
                                                    {"schema": 1, "window_start": since,
                                                     "seed_last_run_date": "2026-09-15",
                                                     "source": {"commit": "a" * 40}, "files": {}}))
        fwd.main(["--build-seed"])
        assert seen == [fwd.FORWARD_START]


# ── 18. 盲化：出结论之前，前瞻结果里不许有任何效应量（v0.45.300）───────────────────────
#
# `decide()` 的 docstring 早就写明「盲化在数据结构上，不在打印上」，但 `evaluate()` 在调用
# `decide()` **之前**就把 `adjusted_trades`（A/B 已实现盈亏之和）塞进了返回字典——`--json`
# 或任何直接调用 `run()` 的人（最可能是某个为「看看状态」顺手打印整个结果的 agent 会话）
# 会在检视点之前拿到它。而它泄漏的是实质内容：平仓盈亏 = size_usd × net_pct 与仓位严格成
# 正比，门只把新仓乘以 0.5、不动方向和出场，所以门**直接**调整的那一笔上 B 盈亏约为 A 的一半，
# `pnl_sum_b − pnl_sum_a` 就是这部分效应的方向与大小（v0.45.308 更正：不是「恒为一半」——
# 一旦现金基数因更早一笔被调整过的仓位而分叉，之后「正常」仓位的盈亏也会有残余差异并被
# `_adjusted_trades_summary` 一并计入，比值不再精确是 1:2；下面夹具只有 1 笔调整、无更早分叉，
# 该场景下确实约为一半）。

_G_DATES = ["2026-09-01", "2026-09-16"]
_G_SINCE, _G_BEFORE = "2026-09-01", "2026-09-17"


def _gate_world(pp_fx, tmp_path):
    """「门真的触发了、且被调整的单已经平仓」的合成世界。

    不造这个场景，盲化测试就是空的：门不触发时 B ≡ A，`adjusted_trades` 恒为 0 笔 / 0.00，
    泄不泄漏都断言得过（09-16~09-18 的真实窗口正是这样：F&G=26/29/29，都在 25~75 之间）。

    极度贪婪日（F&G=90）开的多头在 B 里被减半；恒定价格下只会触发 TIME 止损（开仓 +14 自然日），
    所以 09-01 开的 NVDA 在 09-16 那次重放里平仓 ⇒ 窗口内恰有 1 笔「被调整并已平仓」；
    09-16 开的 AMD 仍在场（也被减半，但没平仓，不进 `adjusted_trades`）。
    「生产」= 门关闭的同一段重放，起点文件直接写盘，落到 conftest 隔离出来的生产状态文件。"""
    _pp, snap_dir, fg_db = pp_fx
    _seed_fg_db(fg_db, [(d, "NVDA", sig, v) for d in _G_DATES
                        for sig, v in (("market.fear_greed", 90.0), ("market.fear_greed_is_cnn", 1.0))])
    _write_snapshot(snap_dir, "NVDA", "2026-09-01", 7.5, "bullish")
    _write_snapshot(snap_dir, "AMD", "2026-09-16", 7.5, "bullish")
    prod = tmp_path / "prod_gate"
    prod.mkdir()
    _pp.run_replay({}, prod, dates=_G_DATES)
    _pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(prod / "closed_trades.jsonl", _pp.CLOSED_FILE)
    shutil.copy(prod / "positions.jsonl", _pp.POSITIONS_FILE)


def _eval_gate_world(sandbox, **kw):
    kw.setdefault("seed", {})  # 显式的「生产当时也是空状态」——合成世界的生产就是从空起跑的
    return fwd.evaluate(_G_DATES, _G_SINCE, _G_BEFORE, sandbox, **kw)


class TestBlindingBeforeTheLook:
    def test_the_fixture_really_has_a_closed_adjusted_trade_and_it_is_informative(self, pp, tmp_path):
        """夹具自证（不做这条，下面全部是空断言）：门确实触发、确实有 1 笔被调整并已平仓，
        且 B 的盈亏恰为 A 的一半——这就是「`adjusted_trades` 是实质泄漏」的定量依据。"""
        _pp = pp[0]
        _gate_world(pp, tmp_path)
        sb = tmp_path / "sb"
        _eval_gate_world(sb)
        summary = fwd._adjusted_trades_summary(
            _pp._load_jsonl(sb / "A_baseline" / "closed_trades.jsonl"),
            _pp._load_jsonl(sb / "B_treatment" / "closed_trades.jsonl"), _G_SINCE, _G_BEFORE)
        assert summary["adjusted_closed_trades"] == 1
        assert summary["pnl_sum_a"] != 0
        assert summary["pnl_sum_b"] == pytest.approx(summary["pnl_sum_a"] / 2, abs=0.02)

    def test_not_ready_result_carries_no_effect_even_though_the_gate_fired(self, pp, tmp_path):
        _gate_world(pp, tmp_path)
        res = _eval_gate_world(tmp_path / "sb")
        assert res["status"] == "not_ready"
        assert "adjusted_trades" not in res, f"未出结论就带出了 adjusted_trades：{res['adjusted_trades']}"
        assert not (_EFFECT_KEYS & set(_all_keys(res)))
        assert not (_EFFECT_KEYS & set(_all_keys(json.loads(json.dumps(res)))))
        _assert_no_unexpected_top_level_keys(res)

    def test_cannot_judge_result_carries_no_effect(self, pp, tmp_path, monkeypatch):
        """自证不过的结果本来就在算周度差之前返回；钉住它，免得以后调整顺序时把这条路径漏成新出口。"""
        _pp = pp[0]
        _gate_world(pp, tmp_path)
        monkeypatch.setitem(_pp.CONFIG, "entry_score_bull", 9.0)  # 生产当时是 6.5：A 一笔都不开
        res = _eval_gate_world(tmp_path / "sb")
        assert res["status"] == "cannot_judge"
        assert not (_EFFECT_KEYS & set(_all_keys(res)))
        _assert_no_unexpected_top_level_keys(res)

    def test_json_cli_output_has_no_effect_keys_before_the_look(self, pp, tmp_path, monkeypatch, capsys):
        """泄漏的现实出口就是 `--json`：把真实 `evaluate()` 的结果经 `main(["--json"])` 打出来再查。"""
        _gate_world(pp, tmp_path)
        res = _eval_gate_world(tmp_path / "sb")
        monkeypatch.setattr(fwd, "run", lambda **k: res)
        assert fwd.main(["--json"]) == 1  # not_ready
        payload = json.loads(capsys.readouterr().out)
        assert not (_EFFECT_KEYS & set(_all_keys(payload)))
        _assert_no_unexpected_top_level_keys(payload)

    @pytest.mark.parametrize("status", ["confirmed", "not_confirmed"])
    def test_verdict_results_still_carry_adjusted_trades_and_still_print(
            self, pp, tmp_path, monkeypatch, capsys, status):
        """收紧不能把出结论之后的描述性附带统计也弄丢：`_print_human` 的出结论分支要读它。"""
        _gate_world(pp, tmp_path)
        verdict = {"status": status, "look": "中期", "alpha": 0.02, "weeks_used": ["2026-W01"],
                   "stats": {"n": 15, "mean": 0.1, "t": 2.0, "p": 0.01}}
        monkeypatch.setattr(fwd, "decide", lambda weeks, *a, **k: verdict)
        res = _eval_gate_world(tmp_path / "sb")
        assert res["status"] == status
        assert res["adjusted_trades"]["adjusted_closed_trades"] == 1
        fwd._print_human(res)
        assert "被调整并已平仓的笔数 1" in capsys.readouterr().out

    def test_insample_result_keeps_adjusted_trades(self, pp, tmp_path):
        """样本内本来就不盲化（它是生成假设/自检机制的数据）——收紧只针对前瞻，不能连它一起拿掉。"""
        _gate_world(pp, tmp_path)
        res = _eval_gate_world(tmp_path / "sb", insample=True, seed=None)
        assert res["status"] == "insample"
        assert res["adjusted_trades"]["adjusted_closed_trades"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 15. 四元组第四元的取法 + A 多开诊断（v0.45.396 事后修订）
#
# 真因：在场仓位读 `size_usd`、已平仓算 `shares × entry_price`，`shares` 取 4 位小数 ⇒ 同一笔两种取法差 1~6 分。
# Step 11 在生产当天扫描**之后**跑，生产当天平掉的仓位在 A（窗口不含当天）里仍在场 ⇒ 键错开。
# 10-02 实测精确 13/16（决策 16/16），3 笔全是这一类；修订后 16/16。`--rehearse` 历史窗口同因
# （2/5、5/13、3/8 → 5/5、13/13、8/8）。另：09-24 生产被限流、当天不出场不开仓，A 多开 4 笔——召回式自证看不见。
# ══════════════════════════════════════════════════════════════════════════════

# 10-02 生产账本里的真实一行（MU，当天 TIME 止损）与 A 重放里同一笔的在场形态：shares / entry_price 逐位相同
_MU_CLOSED = {"ticker": "MU", "entry_date": "2026-09-18", "direction": "bullish", "shares": 1.0008,
              "entry_price": 1015.8, "exit_date": "2026-10-02", "exit_reason": "TIME"}
_MU_OPEN = {"ticker": "MU", "entry_date": "2026-09-18", "direction": "bullish", "shares": 1.0008,
            "entry_price": 1015.8, "size_usd": 1016.64}


class TestEntryKeyIgnoresOpenOrClosed:
    def test_the_real_mu_pair_has_the_asymmetry(self):
        """夹具自检：这对真实数据确实分得开新旧两种取法——否则下一条在旧代码上也绿，什么也证明不了。"""
        assert round(_MU_OPEN["size_usd"], 2) != round(_MU_CLOSED["shares"] * _MU_CLOSED["entry_price"], 2)

    def test_same_position_gets_the_same_key_open_or_closed(self):
        assert fwd._entry_key(_MU_OPEN) == fwd._entry_key(_MU_CLOSED) == ("MU", "2026-09-18", "bullish", 1016.61)

    def test_size_usd_is_never_read(self):
        """变异「在场仓位改回读 size_usd」⇒ 红。"""
        assert fwd._entry_key({**_MU_OPEN, "size_usd": 1e9}) == fwd._entry_key(_MU_OPEN)

    @pytest.mark.parametrize("price", [2.66, 47.32, 263.96, 648.43, 1015.8])
    def test_size_differences_above_the_ledger_resolution_stay_visible(self, price):
        """「谁会红？」：建仓市值差 ≥ 2×max(1 分, entry_price×1e-4) 必然换键——这是账本本身的分辨率
        （`shares` 只存 4 位小数），已平仓那一行一直就是这个精度。变异「第四元取整到角 / 元」⇒ 低价那几格红。"""
        step = 2 * max(0.01, price * 1e-4)

        def key(size):
            return fwd._entry_key({"ticker": "X", "entry_date": "d", "direction": "bullish",
                                   "shares": round(size / price, 4), "entry_price": price})[3]
        sizes = [300.0 + i * 13.37 for i in range(300)]
        assert all(key(s) != key(s + step) for s in sizes)


def _bars(levels):
    """`{日期: 价}` → 一个 `_fetch_ohlc` 替身：工作日逐日给 bar，`levels` 里点名的日子 High 用那个价，其余平价。"""
    def fake(ticker, start, end, *, base):
        out = {}
        d, end_d = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
        while d < end_d:
            if d.weekday() < 5:
                hi = levels.get(d.isoformat(), base * 1.001)
                out[d.isoformat()] = {"Open": base, "High": hi, "Low": base * 0.999, "Close": base}
            d += datetime.timedelta(days=1)
        return out
    return fake


class TestProductionClosesOnTheScanDay:
    """Step 11 的真实时序：生产先把当天跑完（含平仓），检验再重放 [FORWARD_START, 当天)。
    同一笔仓位在生产里已平、在 A 里仍在场——10-02 的 ABBV / MU / NVDA。"""
    P = 1015.8                      # 高价票：shares 取 4 位小数的误差 × 价格足以挪动分位
    D1, D2, SCAN_DAY = "2026-09-16", "2026-09-17", "2026-09-18"

    def _world(self, pp_fx, tmp_path, monkeypatch):
        _pp, snap_dir, _ = pp_fx
        fake = _bars({self.SCAN_DAY: self.P * 1.3})   # 扫描当天冲过 +15% 止盈
        monkeypatch.setattr(_pp, "_fetch_ohlc", lambda t, s, e: fake(t, s, e, base=self.P))
        for d in (self.D1, self.D2, self.SCAN_DAY):
            (snap_dir / f"MU_{d}.json").write_text(json.dumps({
                "ticker": "MU", "date": d, "composite_score": 7.5, "direction": "bullish",
                "agent_votes": {"a": 7.5, "b": 7.5}, "entry_price": self.P}))
        seed = _make_seed()
        prod = tmp_path / "prod_run"
        prod.mkdir()
        for name, blob in seed.items():        # 「生产」起点直接写盘，不走被测的 _apply_seed（见 `_world`）
            (prod / name).write_bytes(blob)
        _pp.run_replay({}, prod, dates=[self.D1, self.D2, self.SCAN_DAY])   # 生产：含扫描当天
        _pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(prod / "closed_trades.jsonl", _pp.CLOSED_FILE)
        shutil.copy(prod / "positions.jsonl", _pp.POSITIONS_FILE)
        return seed

    def test_closed_in_production_open_in_a_is_still_reproduced(self, pp, tmp_path, monkeypatch):
        _pp = pp[0]
        seed = self._world(pp, tmp_path, monkeypatch)
        sb = tmp_path / "sb"
        res = fwd.evaluate([self.D1, self.D2], self.D1, self.SCAN_DAY, sb, insample=False, seed=seed)
        # 夹具自检：确实是「生产当天已平、A 仍在场」，且两种旧取法确实差了分——否则这条在旧代码上也绿
        prod_mu = [t for t in _pp._load_jsonl(_pp.CLOSED_FILE) if t["ticker"] == "MU"]
        a_mu = [p for p in _pp._load_jsonl(sb / "A_baseline" / "positions.jsonl") if p["ticker"] == "MU"]
        assert len(prod_mu) == 1 and prod_mu[0]["exit_date"] == self.SCAN_DAY, prod_mu
        assert len(a_mu) == 1
        assert round(a_mu[0]["size_usd"], 2) != round(prod_mu[0]["shares"] * prod_mu[0]["entry_price"], 2)
        # 本体：同一笔，键相同（旧实现这里 0/1 ⇒ cannot_judge）
        assert res["selfproof"] == {"real_entries": 1, "reproduced": 1, "decision_reproduced": 1, "a_only_entries": 0}
        assert res["status"] == "not_ready"

    def test_a_one_dollar_drift_in_the_sizing_base_still_turns_it_red(self, pp, tmp_path):
        """「谁会红？」：修订不许把金额层变迟钝——A 的起点现金只多 $1（成本价 NAV 基数 +$1），精确层照样全红。"""
        seed = _world(pp, tmp_path)
        meta = json.loads(seed["meta.json"])
        meta["cash"] += 1.0
        drifted = {**seed, "meta.json": json.dumps(meta).encode()}
        res = fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, tmp_path / "sb", insample=False, seed=drifted)
        assert res["status"] == "cannot_judge"
        assert res["selfproof"]["reproduced"] == 0 and res["selfproof"]["decision_reproduced"] == 2
        assert "仓位金额" in res["reason"]


class TestAOnlyIsReportedNotJudged:
    """09-24 的形状：生产那一轮行情全取不到 ⇒ 不出场、不开仓（快照没有入场价，要靠当日收盘）；
    A 重放时行情正常 ⇒ 开了生产没开的仓位。分母只数生产记录（预注册），所以它不拉低复现率——
    但要被报出来：平仓后它会把已实现盈亏带进 A 的仓位基数，金额层之后成片对不上。"""

    def _world_with_outage(self, pp_fx, tmp_path, monkeypatch):
        _pp, snap_dir, _ = pp_fx
        d1, d2 = _W_DATES
        _write_snapshot(snap_dir, "NVDA", d1, 7.5, "bullish")
        (snap_dir / f"AMD_{d2}.json").write_text(json.dumps({
            "ticker": "AMD", "date": d2, "composite_score": 7.5, "direction": "bullish",
            "agent_votes": {"a": 7.5, "b": 7.5}, "entry_price": 0.0}))
        seed = _make_seed()
        prod = tmp_path / "prod_run"
        prod.mkdir()
        for name, blob in seed.items():
            (prod / name).write_bytes(blob)
        _pp.run_replay({}, prod, dates=[d1])                       # 生产 d1：行情正常
        monkeypatch.setattr(_pp, "_fetch_ohlc", lambda t, s, e: {})
        _pp.run_replay({}, prod, dates=[d2])                       # 生产 d2：限流，一根 bar 都没有（续跑同一目录）
        monkeypatch.setattr(_pp, "_fetch_ohlc", _fake_ohlc_flat)   # A 重放：行情又正常了
        _pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(prod / "closed_trades.jsonl", _pp.CLOSED_FILE)
        shutil.copy(prod / "positions.jsonl", _pp.POSITIONS_FILE)
        return seed

    def test_an_outage_day_in_production_shows_up_as_a_only_without_moving_the_rate(self, pp, tmp_path, monkeypatch):
        seed = self._world_with_outage(pp, tmp_path, monkeypatch)
        res = fwd.evaluate(_W_DATES, _W_SINCE, _W_BEFORE, tmp_path / "sb", insample=False, seed=seed)
        # 夹具自检：生产真的没开 AMD（否则 a_only=0 也说得通）
        assert {p["ticker"] for p in pp[0]._load_jsonl(pp[0].POSITIONS_FILE)} == {"NVDA"}
        assert res["selfproof"] == {"real_entries": 1, "reproduced": 1, "decision_reproduced": 1, "a_only_entries": 1}
        assert res["selfproof_continuous"]["a_only_entries"] == 1
        assert res["selfproof_rate"] == 1.0          # 变异「分母把 A 多开的也算进去」⇒ 0.5 ⇒ 红
        assert res["status"] == "not_ready"
        line = fwd.status_line(res)
        assert line.startswith("⏳ ") and "（统计用的 A 比生产多开 1 笔）" in line, line
        _assert_no_unexpected_top_level_keys(res)

    def test_a_only_counts_decisions_not_amounts(self):
        """同一笔只是金额不同 ⇒ 是金额层的事，不是「多开」。变异「a_only 按四元组算」⇒ 红。"""
        k = ("NVDA", "2026-09-16", "bullish")
        assert fwd._selfproof_stats({(*k, 100.0)}, {(*k, 101.0)})["a_only"] == 0
        sp = fwd._selfproof_stats({(*k, 100.0)}, {(*k, 100.0), ("AMD", "2026-09-16", "bullish", 5.0)})
        assert (sp["a_only"], sp["rate"]) == (1, 1.0)

    def test_failure_reason_names_the_extra_entries_without_claiming_another_branch(self):
        sp = {"total": 20, "exact": 10, "decision": 20, "a_only": 4, "rate": 0.5, "decision_rate": 1.0}
        r = fwd._selfproof_failure_reason(sp)
        assert "有 4 笔是 A 开了、生产当天没开" in r and "仓位金额" in r and "评分链" not in r
        assert "生产当天没开" not in fwd._selfproof_failure_reason(dict(sp, a_only=0))
        # 附注不能让「决策层也错」那一支看起来像「金额层」那一支（测试靠这两个词分辨分支）
        r_dec = fwd._selfproof_failure_reason(dict(sp, decision=5, decision_rate=0.25))
        assert "评分链" in r_dec and "仓位金额" not in r_dec and "有 4 笔是 A 开了" in r_dec

    def test_status_line_is_unchanged_when_nothing_extra_was_opened(self):
        base = {"status": "not_ready", "weeks": 3, "next_look_at": 15, "looks_passed_without_verdict": [],
                "selfproof_rate": 1.0}
        zero = {**base, "selfproof": {"real_entries": 5, "reproduced": 5, "decision_reproduced": 5, "a_only_entries": 0}}
        assert fwd.status_line(zero) == fwd.status_line(base) == "⏳ F&G 敞口门前瞻检验：3/15 个合格周，自证 100%"

    def test_rehearse_output_shows_the_extra_entries(self, capsys):
        fwd._print_human({"mode": "rehearse", "status": "rehearsal_ok", "since": "2026-09-22",
                          "before": "2026-09-26", "seed_commit": "abcdef1234", "seed_last_run_date": "2026-09-18",
                          "n_dates": 2, "selfproof": {"real_entries": 2, "reproduced": 2, "decision_reproduced": 2,
                                                       "a_only_entries": 4}})
        assert "逐日多开 4 笔次（不进判定）" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════════════
# 16. 逐日重锚（v0.45.408 事后修订）
#
# 连续重放的 A 一旦与生产在某天良性分歧就再也追不回来（09-24/25 生产被限流、当天没出场没开仓，A 多开 4 笔；
# 它们平仓后 A 的仓位基数偏离生产，此后每一笔金额都对不上）。自证改为每个快照日从生产前一日收盘状态起跑；
# **统计量用的 A / B 仍从种子连续重放**（第 16.4 组钉住）。锚点：种子 / 冻结锚点 / 数据备份仓库，重叠日必须逐字节相同。
# ══════════════════════════════════════════════════════════════════════════════

_S_D1, _S_D2, _S_D3, _S_D4 = "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"
_S_DATES = [_S_D1, _S_D2, _S_D3, _S_D4]
_S_SINCE, _S_BEFORE = _S_D1, "2026-09-22"


def _bars_by_ticker(spikes):
    """`_fetch_ohlc` 替身：所有标的平价 100；`spikes={(标的, 日期): High}` 那根冲高（触发止盈用）。"""
    def fake(ticker, start, end):
        out = {}
        d, end_d = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
        while d < end_d:
            if d.weekday() < 5:
                hi = spikes.get((ticker, d.isoformat()), 100.5)
                out[d.isoformat()] = {"Open": 100.0, "High": hi, "Low": 99.5, "Close": 100.0}
            d += datetime.timedelta(days=1)
        return out
    return fake


def _outage_world(pp_fx, tmp_path, monkeypatch):
    """09-24 的完整形状压成四天。D1 生产与 A 都开 NVDA；D2 生产被限流（`_fetch_ohlc` 全空 ⇒ 不出场、不开仓；
    AMD 快照没有入场价，要靠当日收盘），A 照常开 AMD；D3 AMD 冲过止盈 ⇒ **只有 A** 平仓、带回已实现盈亏；
    D4 生产与 A 都开 MSFT——连续 A 的仓位基数此时比生产多出 AMD 那笔盈亏 ⇒ MSFT 金额对不上；
    逐日重锚从生产 D3 收盘状态起跑 ⇒ 对得上。返回 (种子, 生产逐日收盘状态)。"""
    _pp, snap_dir, _ = pp_fx
    _write_snapshot(snap_dir, "NVDA", _S_D1, 7.5, "bullish")
    (snap_dir / f"AMD_{_S_D2}.json").write_text(json.dumps({
        "ticker": "AMD", "date": _S_D2, "composite_score": 7.5, "direction": "bullish",
        "agent_votes": {"a": 7.5, "b": 7.5}, "entry_price": 0.0}))
    _write_snapshot(snap_dir, "MSFT", _S_D4, 7.5, "bullish")
    good = _bars_by_ticker({("AMD", _S_D3): 130.0})
    seed = _make_seed()
    prod = tmp_path / "prod_outage"
    prod.mkdir()
    for n, b in seed.items():
        (prod / n).write_bytes(b)
    history = {}
    for d in _S_DATES:
        monkeypatch.setattr(_pp, "_fetch_ohlc", (lambda t, s, e: {}) if d == _S_D2 else good)
        _pp.run_replay({}, prod, dates=[d])
        history[d] = {n: (prod / n).read_bytes() for n in fwd.SEED_STATE_FILES}
    monkeypatch.setattr(_pp, "_fetch_ohlc", good)       # 重放时行情是好的
    _pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    for n, dst in (("closed_trades.jsonl", _pp.CLOSED_FILE), ("positions.jsonl", _pp.POSITIONS_FILE),
                   ("equity_curve.jsonl", _pp.EQUITY_FILE)):
        shutil.copy(prod / n, dst)
    return seed, history


def _outage_plan(seed, history, **kw):
    return fwd._anchor_plan(seed, [("测试历史", history)], _S_DATES, processed=set(_S_DATES), **kw)


# ── 16.1 端到端：病根与修法 ─────────────────────────────────────────────────────

class TestPerDayReanchoringEndToEnd:
    def test_the_fixture_really_breaks_the_continuous_replay(self, pp, tmp_path, monkeypatch):
        """夹具自检：连续重放（只有种子一个锚点）在这个世界里确实红在金额层——否则下一条绿得没意义。"""
        seed, _ = _outage_world(pp, tmp_path, monkeypatch)
        prod_tickers = {t["ticker"] for t in pp[0]._load_jsonl(pp[0].POSITIONS_FILE)}
        assert prod_tickers == {"NVDA", "MSFT"}, prod_tickers           # 生产从没开过 AMD
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, tmp_path / "sb", insample=False, seed=seed)
        assert res["selfproof"] == {"real_entries": 2, "reproduced": 1, "decision_reproduced": 2, "a_only_entries": 1}
        assert res["status"] == "cannot_judge" and "仓位金额" in res["reason"]

    def test_per_day_reanchoring_reproduces_production_through_the_outage(self, pp, tmp_path, monkeypatch):
        seed, hist = _outage_world(pp, tmp_path, monkeypatch)
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, tmp_path / "sb", insample=False, seed=seed,
                           plan=_outage_plan(seed, hist))
        assert res["selfproof"] == {"real_entries": 2, "reproduced": 2, "decision_reproduced": 2, "a_only_entries": 1}
        assert res["selfproof_continuous"] == {"real_entries": 2, "reproduced": 1, "decision_reproduced": 2,
                                               "a_only_entries": 1}
        seg = res["selfproof_segments"]
        assert seg["segments"] == 4 and seg["multi_day_segments"] == [] and seg["invalid_anchors"] == {}
        assert res["status"] == "not_ready"
        line = fwd.status_line(res)
        assert line == "⏳ F&G 敞口门前瞻检验：1/15 个合格周，自证 100%（统计用的 A 比生产多开 1 笔）", line
        _assert_no_unexpected_top_level_keys(res)

    def test_a_scoring_or_entry_rule_change_still_turns_it_red(self, pp, tmp_path, monkeypatch):
        """「谁会红？」：逐日重锚不许把自证变成恒绿——每段都用**当前**代码 / 配置跑。"""
        seed, hist = _outage_world(pp, tmp_path, monkeypatch)
        monkeypatch.setitem(pp[0].CONFIG, "entry_score_bull", 9.0)
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, tmp_path / "sb", insample=False, seed=seed,
                           plan=_outage_plan(seed, hist))
        assert res["status"] == "cannot_judge" and res["selfproof"]["decision_reproduced"] == 0
        assert "评分链" in res["reason"]

    def test_a_sizing_change_still_turns_it_red_and_names_the_days(self, pp, tmp_path, monkeypatch):
        seed, hist = _outage_world(pp, tmp_path, monkeypatch)
        monkeypatch.setitem(pp[0].CONFIG, "size_pct_by_tier", {**pp[0].CONFIG["size_pct_by_tier"], "high": 4.0})
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, tmp_path / "sb", insample=False, seed=seed,
                           plan=_outage_plan(seed, hist))
        assert res["status"] == "cannot_judge"
        assert (res["selfproof"]["reproduced"], res["selfproof"]["decision_reproduced"]) == (0, 2)
        assert "仓位金额" in res["reason"] and f"未复现的按入场日：{_S_D1}×1、{_S_D4}×1" in res["reason"]


# ── 16.2 分段方案 `_anchor_plan`（纯数据，不重放）────────────────────────────────

def _st(lrd, cash=46000.0):
    return _make_seed(cash=cash, last_run_date=lrd)


class TestAnchorPlan:
    SEED = None

    def setup_method(self):
        self.SEED = _st("2026-09-15")

    def _hist(self, *lrds, **overrides):
        return {d: overrides.get(d, _st(d, 46000.0 + i)) for i, d in enumerate(lrds)}

    def test_one_segment_per_processed_day_and_the_first_anchor_is_the_seed(self):
        plan = fwd._anchor_plan(self.SEED, [("h", self._hist(_S_D1, _S_D2, _S_D3))], _S_DATES, processed=set(_S_DATES))
        assert plan["segments"] == [("2026-09-15", [_S_D1]), (_S_D1, [_S_D2]), (_S_D2, [_S_D3]), (_S_D3, [_S_D4])]
        assert plan["anchors"]["2026-09-15"] is self.SEED
        assert plan["info"]["anchor_sources"] == {fwd.SOURCE_SEED: 1, "h": 3}

    def test_a_missing_anchor_merges_the_day_into_the_previous_segment(self):
        """生产处理了 D2、备份那天没提交 ⇒ D2、D3 都从 D1 的状态起跑（局部退回连续），并报出来。"""
        plan = fwd._anchor_plan(self.SEED, [("h", self._hist(_S_D1, _S_D3))], _S_DATES, processed=set(_S_DATES))
        assert plan["segments"] == [("2026-09-15", [_S_D1]), (_S_D1, [_S_D2, _S_D3]), (_S_D3, [_S_D4])]
        assert plan["info"]["multi_day_segments"] == [[_S_D2, _S_D3]]

    def test_a_day_production_never_processed_is_not_replayed(self):
        """快照有、生产那天没跑组合 ⇒ 那天不重放（生产那天什么都没做），D3 直接从生产真实的前一状态（D1）起跑——
        不能把 D2 并进 D3 那段，那会替生产做一件它没做过的事。变异「忽略 processed」⇒ 红。"""
        plan = fwd._anchor_plan(self.SEED, [("h", self._hist(_S_D1, _S_D3))], _S_DATES,
                                processed={_S_D1, _S_D3, _S_D4})
        assert plan["segments"] == [("2026-09-15", [_S_D1]), (_S_D1, [_S_D3]), (_S_D3, [_S_D4])]
        assert plan["info"]["production_unprocessed_dates"] == [_S_D2]
        assert plan["info"]["multi_day_segments"] == []

    def test_an_invalid_anchor_is_skipped_and_reported(self):
        bad = _st(_S_D2, cash=float("nan"))
        plan = fwd._anchor_plan(self.SEED, [("h", self._hist(_S_D1, _S_D2, _S_D3, **{_S_D2: bad}))], _S_DATES,
                                processed=set(_S_DATES))
        assert list(plan["info"]["invalid_anchors"]) == [_S_D2] and "非有限" in plan["info"]["invalid_anchors"][_S_D2]
        assert (_S_D1, [_S_D2, _S_D3]) in plan["segments"] and _S_D2 not in plan["anchors"]

    def test_sources_that_disagree_are_cannot_judge_not_a_silent_pick(self):
        """用户 10-03 定：不一致本身说明状态被改过，静默挑一份等于把失败改写成「没发生过」。"""
        a, b = self._hist(_S_D1, _S_D2), self._hist(_S_D1, _S_D2, **{_S_D2: _st(_S_D2, 1.0)})
        with pytest.raises(fwd.SeedError, match="不一致"):
            fwd._anchor_plan(self.SEED, [("甲", a), ("乙", b)], _S_DATES, processed=set(_S_DATES))

    def test_a_source_that_disagrees_with_the_seed_is_refused(self):
        with pytest.raises(fwd.SeedError, match="不一致"):
            fwd._anchor_plan(self.SEED, [("h", {"2026-09-15": _st("2026-09-15", 1.0)})], _S_DATES)

    def test_identical_overlaps_are_fine_and_counted_once(self):
        h = self._hist(_S_D1, _S_D2, _S_D3)
        plan = fwd._anchor_plan(self.SEED, [("甲", h), ("乙", dict(h)), ("丙", {"2026-09-15": dict(self.SEED)})],
                                _S_DATES, processed=set(_S_DATES))
        assert plan["info"]["anchor_sources"] == {fwd.SOURCE_SEED: 1, "甲": 3}

    def test_states_outside_the_window_are_ignored(self):
        """只看 [种子日期, 窗口末日)：更早 / 更晚的状态（哪怕不一致）不是锚点，也不该让检验无法判定。"""
        plan = fwd._anchor_plan(self.SEED, [("甲", {"2026-09-01": _st("2026-09-01"), _S_D4: _st(_S_D4)}),
                                            ("乙", {"2026-09-01": _st("2026-09-01", 1.0), _S_D4: _st(_S_D4, 1.0)})],
                                _S_DATES, processed=set(_S_DATES))
        assert plan["segments"] == [("2026-09-15", _S_DATES)]


# ── 16.3 锚点来源：git 历史 / 冻结锚点 / 数据备份仓库 ───────────────────────────

class TestStateHistory:
    def test_last_commit_per_date_wins_and_unreadable_commits_are_counted(self, tmp_path):
        broken = {"paper_portfolio_state/meta.json": b"{not json"}
        repo, shas = _git_repo(tmp_path, [_state_at("2026-09-15", 3000.0), _state_at("2026-09-16", 4000.0),
                                          _state_at("2026-09-16", 4100.0), broken])
        hist, unreadable, _ = fwd._state_history(repo)
        assert sorted(hist) == ["2026-09-15", "2026-09-16"] and unreadable == 1
        assert hist["2026-09-16"][0] == shas[2] and _cash_of(hist["2026-09-16"][1]) == 4100.0

    def test_cat_batch_reads_blobs_and_reports_missing_paths_as_none(self, tmp_path):
        repo, shas = _git_repo(tmp_path, [_state_at("2026-09-15", 3000.0)])
        got = fwd._git_cat_batch(repo, [f"{shas[0]}:paper_portfolio_state/meta.json", f"{shas[0]}:nope.txt"])
        assert _cash_of({"meta.json": got[f"{shas[0]}:paper_portfolio_state/meta.json"]}) == 3000.0
        assert got[f"{shas[0]}:nope.txt"] is None

    @pytest.mark.parametrize("kind", ["missing", "not_a_repo", "shallow"])
    def test_an_unusable_history_is_a_seed_error(self, tmp_path, kind):
        if kind == "missing":
            target = tmp_path / "nowhere"
        elif kind == "not_a_repo":
            target = tmp_path / "plain"
            target.mkdir()
        else:
            repo, _ = _git_repo(tmp_path, [_state_at("2026-09-15", 1.0), _state_at("2026-09-16", 2.0)])
            target = tmp_path / "shallow"
            subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(target)],
                           check=True, capture_output=True, env=_synthetic_git_env())
        with pytest.raises(fwd.SeedError):
            fwd._state_history(target)


_ANCHOR_DIR = _SEED_DIR / "anchors"


class TestFrozenAnchors:
    def test_shipped_anchors_load_and_cover_the_code_repo_era(self):
        anchors = fwd.load_frozen_anchors()
        assert sorted(anchors) == ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-22", "2026-09-24", "2026-09-25"]

    def test_shipped_anchors_are_tracked_by_git(self):
        """同 `test_seed_files_are_tracked_by_git`：128 = 导出的源码包，skip 正当。"""
        r = subprocess.run(["git", "-C", str(_ROOT), "ls-files", "--error-unmatch", "--",
                            f"experiments/{fwd.SEED_DIRNAME}/anchors/{fwd.ANCHORS_MANIFEST_NAME}",
                            *[f"experiments/{fwd.SEED_DIRNAME}/anchors/{d}/{n}"
                              for d in fwd.load_frozen_anchors() for n in fwd.SEED_STATE_FILES]],
                           capture_output=True, text=True)
        if r.returncode == 128:
            pytest.skip("不在 git 仓库里（导出的源码包）")
        assert r.returncode == 0, f"冻结锚点文件未被 git 跟踪：{r.stderr.strip()}"

    def test_shipped_anchors_are_exactly_the_code_repo_history(self):
        """冻结的就是代码仓库里窗口内的**全部**生产状态（一个不多、一个不少、逐字节相同）。代码仓库的状态提交
        止于 2026-09-25；以后若有人又往代码仓库提交了窗口内的状态，这条会红。导出包 / 浅克隆里没有完整历史，skip 正当
        ——**只有这两种**（v0.45.408 二次检查收窄：原先任何 SeedError 都 skip，`cat-file` 解析失败之类的真错也会被吞掉）。"""
        r = subprocess.run(["git", "-C", str(_ROOT), "rev-parse", "--is-shallow-repository"], capture_output=True, text=True)
        if r.returncode == 128:
            pytest.skip("不在 git 仓库里（导出的源码包）")
        if r.stdout.strip() == "true":
            pytest.skip("浅克隆：没有完整的代码仓库历史")
        hist, _, _ = fwd._state_history(_ROOT)
        want = {d: files for d, (_, files) in hist.items() if d >= fwd.FORWARD_START}
        assert fwd.load_frozen_anchors() == want

    def _copy(self, tmp_path):
        d = tmp_path / "seed"
        shutil.copytree(_SEED_DIR, d)
        return d

    def _manifest(self, d):
        return json.loads((d / "anchors" / fwd.ANCHORS_MANIFEST_NAME).read_text(encoding="utf-8"))

    def _write_manifest(self, d, m):
        (d / "anchors" / fwd.ANCHORS_MANIFEST_NAME).write_text(json.dumps(m), encoding="utf-8")

    def test_a_tampered_file_fails_the_checksum(self, tmp_path):
        d = self._copy(tmp_path)
        p = d / "anchors" / "2026-09-17" / "positions.jsonl"
        p.write_bytes(p.read_bytes() + b"\n")
        with pytest.raises(fwd.SeedError, match="sha256"):
            fwd.load_frozen_anchors(d)

    def test_a_directory_not_in_the_manifest_is_refused(self, tmp_path):
        d = self._copy(tmp_path)
        shutil.copytree(d / "anchors" / "2026-09-17", d / "anchors" / "2026-09-19")
        with pytest.raises(fwd.SeedError, match="对不上"):
            fwd.load_frozen_anchors(d)

    def test_a_manifest_entry_without_its_directory_is_refused(self, tmp_path):
        d = self._copy(tmp_path)
        shutil.rmtree(d / "anchors" / "2026-09-17")
        with pytest.raises(fwd.SeedError, match="对不上"):
            fwd.load_frozen_anchors(d)

    def test_wrong_window_start_is_refused(self, tmp_path):
        d = self._copy(tmp_path)
        m = self._manifest(d)
        m["window_start"] = "2026-09-17"
        self._write_manifest(d, m)
        with pytest.raises(fwd.SeedError, match="窗口起点"):
            fwd.load_frozen_anchors(d)

    def test_meta_date_must_match_the_directory(self, tmp_path):
        """目录叫 09-18、里面却是 09-17 的状态（校验和照新内容现算 ⇒ 只考内容那一层）。"""
        d = self._copy(tmp_path)
        src = d / "anchors" / "2026-09-17"
        dst = d / "anchors" / "2026-09-18"
        m = self._manifest(d)
        for n in fwd.SEED_STATE_FILES:
            (dst / n).write_bytes((src / n).read_bytes())
            m["anchors"]["2026-09-18"]["files"][n]["sha256"] = hashlib.sha256((src / n).read_bytes()).hexdigest()
        self._write_manifest(d, m)
        with pytest.raises(fwd.SeedError, match="last_run_date"):
            fwd.load_frozen_anchors(d)

    def test_build_anchors_writes_once_and_refuses_to_overwrite(self, tmp_path, monkeypatch, capsys):
        out = tmp_path / "frozen"
        anchors = {"2026-09-16": _make_seed(last_run_date="2026-09-16")}
        manifest = {"schema": 1, "window_start": fwd.FORWARD_START, "anchors": {}}
        monkeypatch.setattr(fwd, "_seed_dir", lambda *a, **k: out)
        monkeypatch.setattr(fwd, "build_frozen_anchors_from_git", lambda since, *a, **k: (anchors, manifest))
        assert fwd.main(["--build-anchors"]) == 0
        p = out / "anchors" / "2026-09-16" / "meta.json"
        marker = p.read_bytes()
        monkeypatch.setattr(fwd, "build_frozen_anchors_from_git",
                            lambda since, *a, **k: ({"2026-09-16": {**anchors["2026-09-16"], "meta.json": b"{}"}}, manifest))
        assert fwd.main(["--build-anchors"]) == 3
        assert p.read_bytes() == marker and "拒绝覆盖" in capsys.readouterr().err


class TestDataBackupRepoWiring:
    def test_paths_property_is_call_time_and_under_the_data_root(self, tmp_path, monkeypatch):
        from hive_logger import PATHS
        assert PATHS.data_backup_repo == PATHS.home / "_git_backup"
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "elsewhere"))
        assert PATHS.data_backup_repo == tmp_path / "elsewhere" / "_git_backup"

    def _forward_world(self, pp_fx, dates):
        """真冻结种子 + 真冻结锚点；生产净值曲线写上这些日期（「生产处理过」）。"""
        _pp = pp_fx[0]
        _pp.EQUITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _pp._write_jsonl(_pp.EQUITY_FILE, [{"date": d, "nav": 50000.0} for d in dates])

    def _backup(self, tmp_path, states):
        from hive_logger import PATHS
        repo, _ = _git_repo(tmp_path, [{f"paper_portfolio_state/{n}": b for n, b in s.items()} for s in states],
                            name="bk_src")
        target = PATHS.data_backup_repo
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(repo), str(target))
        return target

    def test_the_forward_plan_stitches_seed_frozen_anchors_and_the_backup_repo(self, pp, tmp_path):
        dates = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-22", "2026-09-24", "2026-09-25",
                 "2026-09-28", "2026-09-29"]
        self._forward_world(pp, dates)
        frozen = fwd.load_frozen_anchors()
        self._backup(tmp_path, [frozen["2026-09-25"], _st("2026-09-28", 40000.0)])   # 重叠日 09-25 逐字节相同
        plan = fwd._forward_anchor_plan(fwd.load_seed(), dates)
        assert plan["info"]["segments"] == 8 and plan["info"]["multi_day_segments"] == []
        assert plan["info"]["anchor_sources"] == {fwd.SOURCE_SEED: 1, fwd.SOURCE_FROZEN_ANCHORS: 6,
                                                  fwd.SOURCE_DATA_BACKUP: 1}
        assert plan["segments"][-1] == ("2026-09-28", ["2026-09-29"])

    def test_a_backup_that_disagrees_with_the_frozen_anchors_is_cannot_judge(self, pp, tmp_path):
        _write_snapshot(pp[1], "NVDA", "2026-09-22", 7.5, "bullish")
        self._forward_world(pp, ["2026-09-22"])
        self._backup(tmp_path, [_st("2026-09-18", 1.0)])
        res = fwd.run(today="2026-09-23")
        assert res["status"] == "cannot_judge" and "逐日锚点不可用" in res["reason"] and "不一致" in res["reason"]

    def test_a_missing_backup_repo_is_cannot_judge_and_never_replays(self, pp, monkeypatch):
        _write_snapshot(pp[1], "NVDA", "2026-09-16", 7.5, "bullish")
        monkeypatch.setattr(fwd, "evaluate", lambda *a, **k: pytest.fail("锚点不可用时不许退回连续重放"))
        res = fwd.run(today="2026-09-17")
        assert res["status"] == "cannot_judge" and "逐日锚点不可用" in res["reason"]

    def test_rehearse_without_a_backup_repo_is_cannot_judge(self, pp, tmp_path):
        seed, hist = _world_with_history(pp, tmp_path)
        repo = TestRehearse._synthetic_repo(None, tmp_path, seed, hist)
        res = fwd.rehearse(_W_SINCE, _W_BEFORE, repo_root=repo)        # 不给 backup_repo ⇒ 读 PATHS（沙箱里没有）
        assert res["status"] == "cannot_judge" and "逐日锚点不可用" in res["reason"]


# ── 16.4 统计量必须仍是连续重放（逐日重锚只许用于自证）──────────────────────────

class TestTheStatisticStaysContinuous:
    def test_b_is_replayed_once_continuously_from_the_seed_and_feeds_the_statistic(self, pp, tmp_path, monkeypatch):
        seed, hist = _outage_world(pp, tmp_path, monkeypatch)
        calls, results = [], []
        orig = fwd._replay_variant

        def spy(cfg, state_dir, dates, seed=None):
            r = orig(cfg, state_dir, dates, seed=seed)
            calls.append((cfg, list(dates), seed))
            results.append(r)
            return r
        monkeypatch.setattr(fwd, "_replay_variant", spy)
        fed = []
        orig_wd = fwd.weekly_deltas
        monkeypatch.setattr(fwd, "weekly_deltas", lambda a, b: fed.append((a, b)) or orig_wd(a, b))
        fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, tmp_path / "sb", insample=False, seed=seed,
                     plan=_outage_plan(seed, hist))
        b_calls = [i for i, c in enumerate(calls) if c[0] == fwd.FG_GATE_TEST_CONFIG]
        assert len(b_calls) == 1, "B 只许连续重放一次——逐日重锚 B 会每天抹掉门的累积效应"
        assert calls[b_calls[0]][1] == _S_DATES and calls[b_calls[0]][2] is seed
        assert all(c[0] == {} for i, c in enumerate(calls) if i not in b_calls)
        cont_a = [i for i, c in enumerate(calls) if c[0] == {} and c[1] == _S_DATES and c[2] is seed]
        assert len(cont_a) == 1
        assert len(fed) == 1 and fed[0][0] is results[cont_a[0]]["equity"] and fed[0][1] is results[b_calls[0]]["equity"]
        assert len(calls) == 2 + 4    # 连续 A + 连续 B + 4 个单日段


# ── 16.5 输出 ───────────────────────────────────────────────────────────────

class TestSegmentOutputs:
    _NR = {"status": "not_ready", "weeks": 3, "next_look_at": 15, "looks_passed_without_verdict": [],
           "selfproof_rate": 1.0}

    def test_anchor_gaps_show_up_without_turning_the_icon(self):
        res = {**self._NR, "selfproof_segments": {"multi_day_segments": [["2026-10-05", "2026-10-06"]],
                                                  "invalid_anchors": {"2026-10-02": "x"},
                                                  "production_unprocessed_dates": ["2026-10-07"]}}
        line = fwd.status_line(res)
        assert line == ("⏳ F&G 敞口门前瞻检验：3/15 个合格周，自证 100%"
                        "（逐日锚点：1 段跨多天（缺锚点）、1 个坏锚点、生产没处理 1 个快照日）"), line

    def test_no_gaps_no_note(self):
        res = {**self._NR, "selfproof_segments": {"segments": 11, "multi_day_segments": [], "invalid_anchors": {},
                                                  "production_unprocessed_dates": []}}
        assert fwd.status_line(res) == fwd.status_line(self._NR)

    def test_human_output_shows_segments_and_the_continuous_diagnostic(self, capsys):
        fwd._print_human({"mode": "forward", "status": "not_ready", "n_dates": 11, "weeks": 2, "next_look_at": 15,
                          "looks_passed_without_verdict": [], "selfproof_rate": 1.0,
                          "selfproof": {"real_entries": 19, "reproduced": 19, "decision_reproduced": 19, "a_only_entries": 8},
                          "selfproof_continuous": {"real_entries": 19, "reproduced": 19, "decision_reproduced": 19,
                                                   "a_only_entries": 4},
                          "selfproof_segments": {"segments": 11, "multi_day_segments": [], "invalid_anchors": {},
                                                 "production_unprocessed_dates": [], "anchor_sources": {"种子": 1}}})
        out = capsys.readouterr().out
        assert "逐日重锚：11 段" in out and "连续 A（统计量那份，只作诊断）：精确 19/19" in out and "比生产多开 4 笔" in out

    def test_ohlc_window_covers_every_anchors_positions(self, monkeypatch):
        import paper_portfolio as _pp
        seen = []
        monkeypatch.setattr(_pp, "replay_ohlc_window", lambda s, e: seen.append((s, e)) or contextlib.nullcontext())
        anchor = _make_seed(positions=[_pos_row("OLD", "2026-08-20", 1000.0)], last_run_date="2026-09-17")
        with fwd._replay_ohlc_scope(_S_DATES, _make_seed(), extra_seeds=[anchor]):
            pass
        assert seen and seen[0][0] == "2026-08-20"


class TestCountsThatMustNotBeConfused:
    def test_the_status_line_counts_the_statistics_a_not_the_per_day_events(self):
        """09-24、09-25 生产连挂两天 ⇒ 逐日口径把同一批 4 笔各算一次（8），统计量那份 A 实际多持 4 笔。
        进度行要说的是后者。变异「进度行改读 selfproof.a_only_entries」⇒ 红（端到端夹具里两个口径恰好相等，抓不到）。"""
        res = {"status": "not_ready", "weeks": 2, "next_look_at": 15, "looks_passed_without_verdict": [],
               "selfproof_rate": 1.0,
               "selfproof": {"real_entries": 19, "reproduced": 19, "decision_reproduced": 19, "a_only_entries": 8},
               "selfproof_continuous": {"real_entries": 19, "reproduced": 19, "decision_reproduced": 19,
                                        "a_only_entries": 4}}
        assert fwd.status_line(res) == "⏳ F&G 敞口门前瞻检验：2/15 个合格周，自证 100%（统计用的 A 比生产多开 4 笔）"

    def test_rehearse_takes_later_anchors_from_the_backup_repo(self, pp, tmp_path, monkeypatch):
        """阶段 5 之后的状态只在备份仓库里：代码仓库只到 D1，之后的锚点只能来自备份仓库。演练的日期来自快照，
        这个世界里 D3 没有快照 ⇒ 要比对的是 D1 / D2 / D4，D4 的锚点（D3 收盘状态）只在备份仓库里。
        变异「演练不读备份仓库」⇒ D2、D4 并成一段从 D1 连续重放 ⇒ 撞上 D2 那次生产故障 ⇒ 红。"""
        seed, hist = _outage_world(pp, tmp_path, monkeypatch)
        as_commit = lambda files: {f"paper_portfolio_state/{n}": b for n, b in files.items()}   # noqa: E731
        code, _ = _git_repo(tmp_path, [as_commit(seed), as_commit(hist[_S_D1])], name="code_repo")
        backup, _ = _git_repo(tmp_path, [as_commit(hist[d]) for d in (_S_D1, _S_D2, _S_D3)], name="backup_repo")
        res = fwd.rehearse(_S_SINCE, _S_BEFORE, repo_root=code, backup_repo=backup)
        assert res["status"] == "rehearsal_ok", res.get("reason")
        seg = res["selfproof_segments"]
        assert seg["multi_day_segments"] == [] and seg["anchor_sources"] == {
            fwd.SOURCE_SEED: 1, fwd.SOURCE_CODE_REPO: 1, fwd.SOURCE_DATA_BACKUP: 1}
        assert res["selfproof_continuous"]["reproduced"] == 1        # 对照：连续重放在这个世界里确实是红的


class TestSecondReviewFixes:
    """v0.45.408 二次检查补的两处。"""

    def test_a_ledger_only_repair_commit_is_the_state_used(self, tmp_path):
        """只改 `positions.jsonl`、不碰 meta 的人工修复：「同一日期取最后一个状态」要拿到修复后的那份，
        演练的起点种子也一样。变异「状态提交只按 meta.json 列」⇒ 两处都拿到修复前 ⇒ 红。"""
        before_fix = _state_at("2026-09-15", 3000.0, positions=[_pos_row("OLD", "2026-09-10", 5000.0)])
        fixed = {"paper_portfolio_state/positions.jsonl": _jsonl([_pos_row("OLD", "2026-09-10", 4000.0)])}
        repo, shas = _git_repo(tmp_path, [before_fix, fixed, _state_at("2026-09-16", 4000.0)])
        hist, _, _ = fwd._state_history(repo)
        assert hist["2026-09-15"][0] == shas[1]
        assert json.loads(hist["2026-09-15"][1]["positions.jsonl"])["size_usd"] == 4000.0
        files, manifest = fwd.build_seed_from_git("2026-09-16", repo)
        assert manifest["source"]["commit"] == shas[1]
        assert json.loads(files["positions.jsonl"])["size_usd"] == 4000.0

    def test_icloud_duplicate_directories_do_not_break_the_frozen_anchors(self, tmp_path):
        """本仓在 iCloud 同步的 ~/Desktop 下，会冒出「2026-09-17 2」这类重名副本——不能让检验天天无法判定。
        日期名的多余目录照样红（`TestFrozenAnchors::test_a_directory_not_in_the_manifest_is_refused`）。
        变异「目录核对不过滤日期名」⇒ 红。"""
        d = tmp_path / "seed"
        shutil.copytree(_SEED_DIR, d)
        shutil.copytree(d / "anchors" / "2026-09-17", d / "anchors" / "2026-09-17 2")
        assert fwd.load_frozen_anchors(d) == fwd.load_frozen_anchors()


class TestSecondReviewFindings:
    """v0.45.408 二次检查（两个独立审查 agent，均已实测复现）补的口子。"""

    def _equity(self, pp_fx, dates):
        _pp = pp_fx[0]
        _pp.EQUITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _pp._write_jsonl(_pp.EQUITY_FILE, [{"date": d, "nav": 50000.0} for d in dates])

    def _backup(self, tmp_path, states):
        from hive_logger import PATHS
        repo, shas = _git_repo(tmp_path, [{f"paper_portfolio_state/{n}": b for n, b in st.items()} for st in states],
                               name="bk_src2")
        PATHS.data_backup_repo.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(repo), str(PATHS.data_backup_repo))
        return shas

    def test_a_rebuilt_backup_with_no_overlap_is_cannot_judge_not_a_silent_continuous_replay(self, pp, tmp_path):
        """备份仓库在、是 git、不浅，但历史丢了被重建（只剩一个无关提交）⇒ 原先返回空表、09-25 之后全部从最后一个
        冻结锚点连续重放，等于静默退回。变异「去掉重叠日要求」⇒ 红。"""
        dates = ["2026-09-16", "2026-09-28", "2026-09-29"]
        self._equity(pp, dates)
        self._backup(tmp_path, [_st("2026-10-01", 1.0)])
        with pytest.raises(fwd.SeedError, match="没有一个重叠日"):
            fwd._forward_anchor_plan(fwd.load_seed(), dates)

    def test_overlap_days_are_checked_even_outside_this_windows_range(self, pp, tmp_path):
        """窗口只到 09-17 时 `_anchor_plan` 不看 09-18 之后的状态；但「每次运行都核重叠日」要对所有重叠日成立。"""
        dates = ["2026-09-16", "2026-09-17"]
        self._equity(pp, dates)
        self._backup(tmp_path, [_st("2026-09-18", 1.0)])
        with pytest.raises(fwd.SeedError, match="不一致"):
            fwd._forward_anchor_plan(fwd.load_seed(), dates)

    def test_a_rollback_in_the_backup_history_is_cannot_judge(self, pp, tmp_path):
        """恢复 09-25 之后跑 09-29：「每个日期取最后一个状态」会让 09-29 的锚点取到被弃用的 09-28。
        变异「不检测回退」⇒ 红。回退发生在种子之前（不影响窗口）则不拦。"""
        frozen = fwd.load_frozen_anchors()
        dates = ["2026-09-16", "2026-09-28", "2026-09-29", "2026-09-30"]
        self._equity(pp, dates)
        self._backup(tmp_path, [frozen["2026-09-25"], _st("2026-09-28", 1.0), frozen["2026-09-25"],
                                _st("2026-09-29", 2.0)])
        with pytest.raises(fwd.SeedError, match="回退"):
            fwd._forward_anchor_plan(fwd.load_seed(), dates)
        assert fwd._refuse_rollbacks("x", [("abc", "2026-09-01", "2026-09-05")], "2026-09-15") is None

    def test_processed_dates_reach_the_forward_plan(self, pp, tmp_path):
        """「生产没跑组合的快照日不重放」只在 `_anchor_plan` 上测过；接线若被改成 `processed=None` 全绿。"""
        frozen = fwd.load_frozen_anchors()
        dates = ["2026-09-16", "2026-09-28", "2026-09-29"]
        self._equity(pp, ["2026-09-16", "2026-09-29"])          # 生产 09-28 没跑组合
        self._backup(tmp_path, [frozen["2026-09-25"]])
        plan = fwd._forward_anchor_plan(fwd.load_seed(), dates)
        assert plan["info"]["production_unprocessed_dates"] == ["2026-09-28"]
        assert plan["info"]["overlap_days_checked"] == 1

    def test_processed_dates_reach_the_rehearse_plan(self, pp, tmp_path):
        seed, hist = _world_with_history(pp, tmp_path)
        repo = TestRehearse._synthetic_repo(None, tmp_path, seed, hist)
        self._equity(pp, [_W_DATES[1]])
        plan = fwd._rehearse_anchor_plan(seed, _W_DATES, repo, repo)
        assert plan["info"]["production_unprocessed_dates"] == [_W_DATES[0]]

    def test_a_stale_anchor_is_reported_even_in_a_single_day_segment(self):
        """生产在没有快照的 09-17 跑过组合、那天的备份没提交 ⇒ 09-18 从 09-16 的状态起跑。段是单日的，
        多日段不会报它——要单独报。变异「不报旧锚点」⇒ 红。"""
        seed = _st("2026-09-15")
        plan = fwd._anchor_plan(seed, [("h", {"2026-09-16": _st("2026-09-16")})], ["2026-09-16", "2026-09-18"],
                                processed={"2026-09-16", "2026-09-17", "2026-09-18"})
        assert plan["info"]["multi_day_segments"] == []
        assert plan["info"]["stale_anchor_segments"] == [["2026-09-18", "2026-09-16", "2026-09-17"]]
        assert "1 段锚点比生产前一次运行旧" in fwd._anchor_gap_note({"selfproof_segments": plan["info"]})

    def test_the_failure_reason_names_anchor_gaps(self, pp, tmp_path, monkeypatch):
        """备份缺了限流日附近的提交 ⇒ 多日段把多开带进后面 ⇒ 红；原因要说到锚点缺口，不能只推给种子 / K 线 / 配置。
        变异「失败原因不看 segments」⇒ 红。"""
        seed, hist = _outage_world(pp, tmp_path, monkeypatch)
        plan = fwd._anchor_plan(seed, [("h", {_S_D1: hist[_S_D1]})], _S_DATES, processed=set(_S_DATES))
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, tmp_path / "sb", insample=False, seed=seed, plan=plan)
        assert res["status"] == "cannot_judge"
        assert "逐日锚点有缺口（逐日锚点：1 段跨多天（缺锚点））" in res["reason"]
        assert "单日段里它们不会带进后面的日子" in res["reason"] and "评分链" not in res["reason"]

    def test_rehearse_after_the_code_repo_era_seeds_from_the_backup_repo(self, pp, tmp_path, monkeypatch):
        """代码仓库的状态提交止于 09-25 ⇒ 之后的窗口起点只在备份仓库里。变异「只从代码仓库取种子」⇒ 红。"""
        seed, hist = _outage_world(pp, tmp_path, monkeypatch)
        as_commit = lambda files: {f"paper_portfolio_state/{n}": b for n, b in files.items()}   # noqa: E731
        code, _ = _git_repo(tmp_path, [as_commit(seed), as_commit(hist[_S_D1])], name="code_only")
        backup, _ = _git_repo(tmp_path, [as_commit(hist[d]) for d in _S_DATES], name="backup_all")
        res = fwd.rehearse(_S_D4, _S_BEFORE, repo_root=code, backup_repo=backup)
        assert res["status"] == "rehearsal_ok", res.get("reason")
        assert res["seed_source"] == fwd.SOURCE_DATA_BACKUP and res["seed_last_run_date"] == _S_D3
