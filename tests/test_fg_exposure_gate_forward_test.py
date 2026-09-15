"""F&G 组合层敞口控制门前瞻检验（v0.45.242 共振加成那次的预注册模式，本次移植到组合层）的守卫。

`experiments/fg_exposure_gate_forward_test.py` 是一份**预注册**：价值全在"看结果之前写死"。
这里守的不是"算得对不对"这一件事，而是四件（同 `tests/test_resonance_boost_forward_test.py`
的立场）：

1. 预注册常量没被悄悄改掉。
2. 检视按**时间顺序最先攒到的**前 N 周算，未到检视点时返回值里没有任何效应量（盲化在
   数据结构上）。
3. 自证是前提：机制本身错了（或复现不了生产实际记录）就"无法判定"，不算出一个没意义的数。
4. 统计对象与共振加成那次**不同**——这次改的是仓位大小不是排序，横截面 IC 不适用，
   用的是两个变体（`paper_portfolio.run_replay` 真实重放）净值曲线的周度收益率差，
   一并守住"这套改法真的接的是真实 `paper_portfolio` 代码，不是重新写一遍"。
"""
from __future__ import annotations

import datetime
import importlib.util
import sqlite3
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
        """需要精心选的数字：中期(p=0.040)不够格(<0.02)但终期(p=0.016)够格(<0.045)——
        不能拿"处处都显著"的数据测，那测不出"两次检视门槛不同"这件事。"""
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
    def test_open_position_uses_size_usd_directly(self):
        pos = {"ticker": "NVDA", "entry_date": "2026-09-16", "direction": "bullish",
              "size_usd": 1234.567}
        assert fwd._entry_key(pos) == ("NVDA", "2026-09-16", "bullish", 1234.57)

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
                    "size_usd": 500.0}]
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
        res = fwd.evaluate(dates, "2026-09-16", "2026-09-17", tmp_path / "sandbox", insample=False)
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
        res = fwd.evaluate(dates, "2026-09-16", "2026-09-17", tmp_path / "sandbox", insample=False)
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
