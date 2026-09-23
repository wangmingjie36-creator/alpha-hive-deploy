"""维度 IC 证据协议执行器（P2，v0.45.325）的守卫。

执行器对应协议 `experiments/dim_ic_preregistration.md` §12 验收清单。这里逐条守，且每条都配一个
「违反它就会红」的用例——全绿本身证明不了什么（[[alpha-hive-test-guard-failures]]）：

- 结果变量：合成库里 `price_t7` / `return_t7` 与 `close_t7` **方向相反**，读错列 IC 符号就反。
- p 值：判别用例的 t 落在 [z 临界, t(25) 临界) 之间，正态近似会在中检误判「拒绝」。
- 盲化：返回值顶层键白名单 + 未出结论的假设不得出现在 `verdicts`。
- 截断：世代边界（沿用 `generation_boundaries` 的影响面）与 config 权重的 git 历史，各自正反两例。
- 接线：就绪度闸 `--quiet` 那一行的**段顺序**是契约（周度任务按「第三段 = F&G」解析）。

全部用合成数据 / 合成 git 仓库，零外部依赖，不需要 skip。
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import json
import math
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from scipy import stats

import ic_diagnostics
import signal_archive

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "experiments" / "dim_ic_forward_test.py"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = _load("dim_ic_forward_test", "experiments/dim_ic_forward_test.py")
P = _load("dim_ic_protocol", "experiments/dim_ic_protocol.py")

DIMS = ("signal", "catalyst", "sentiment", "odds", "risk_adj")
_TOP_KEYS_ALWAYS = {"forward_start", "today", "status", "h1_weeks", "next_look_at", "looks_done",
                    "verdicts", "truncation", "weight_change", "stale"}


# ── 合成数据 ────────────────────────────────────────────────────────────────

_SCHEMA = ("CREATE TABLE predictions (date TEXT, ticker TEXT, dimension_scores TEXT, "
           "price_at_predict REAL, checked_t7 INTEGER, close_t7 REAL, price_t7 REAL, "
           "return_t7 REAL, exit_price REAL, exit_reason TEXT)")


def _day_rows(date, n=25, sign=1.0, ripe=True, width_dims=DIMS, sltp_truncated=False):
    """一天的横截面：sentiment = i；`close_t7` 与 sentiment 同向（sign=+1 时 IC=+1），
    **`price_t7` / `return_t7` 反向**——读错列 IC 符号就反。"""
    rows = []
    for i in range(n):
        ds = {"signal": (i * 7) % n, "catalyst": (i * 3) % n, "sentiment": i,
              "odds": (i * 5) % n, "risk_adj": (i * 11) % n}
        ds = {k: v for k, v in ds.items() if k in width_dims}
        close = 100.0 * (1 + sign * 0.001 * (i + 1))
        wrong = 100.0 * (1 - sign * 0.001 * (i + 1))
        exit_price = close if sltp_truncated else 99.0
        rows.append((date, f"T{i:02d}", json.dumps(ds), 100.0, 1 if ripe else 0,
                     close if ripe else None, wrong, (wrong / 100 - 1) * 100,
                     exit_price, "SL" if sltp_truncated else "TIME"))
    return rows


def _make_db(tmp_path, rows, name="p.db"):
    db = tmp_path / name
    con = sqlite3.connect(db)
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def _weekdays(start, n):
    d, out = dt.date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _wk(i):
    """合成周键：只需可排序。"""
    return (2026 + i // 52, i % 52 + 1)


def _series(n, target_t, lag=1):
    """n 个周值，NW(lag) 下单样本 t 恰为 target_t（平移不改 NW SE）。"""
    base = [((i * 7919) % 13 - 6) / 6.0 for i in range(n)]
    m = sum(base) / n
    base = [b - m for b in base]
    shift = target_t * fx.nw_se(base, lag)
    return [b + shift for b in base]


def _weeks(vals, start=0):
    return [(_wk(start + i), v, 5) for i, v in enumerate(vals)]


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_keys(v)


# ── 1. 数据层 ────────────────────────────────────────────────────────────────

class TestDataLayer:
    def _h1(self, rows, **kw):
        it = fx.items(P)["H1"]
        return fx.weekly_series(rows, it["value"], it["signals"], P, **kw)

    def test_reads_close_t7_not_price_t7(self, tmp_path):
        """判别：close_t7 与 sentiment 同向、price_t7/return_t7 反向。读对列 ⇒ IC=+1。"""
        db = _make_db(tmp_path, _day_rows("2026-09-28", sign=+1))
        rows = fx.load_rows(db, P, "2026-12-31")
        w = self._h1(rows)
        assert len(w) == 1 and w[0][1] == pytest.approx(1.0)

    def test_width_below_min_drops_the_day(self, tmp_path):
        rows19 = fx.load_rows(_make_db(tmp_path, _day_rows("2026-09-28", n=P.MIN_WIDTH - 1), "a.db"),
                              P, "2026-12-31")
        rows20 = fx.load_rows(_make_db(tmp_path, _day_rows("2026-09-28", n=P.MIN_WIDTH), "b.db"),
                              P, "2026-12-31")
        assert self._h1(rows19) == []
        assert len(self._h1(rows20)) == 1

    def test_all_tied_scores_drop_the_day(self, tmp_path):
        rows = [r[:2] + (json.dumps({"sentiment": 5.0}),) + r[3:] for r in _day_rows("2026-09-28")]
        assert self._h1(fx.load_rows(_make_db(tmp_path, rows), P, "2026-12-31")) == []

    def test_weekend_prewindow_and_future_rows_are_ignored(self, tmp_path):
        rows = (_day_rows("2026-10-03") + _day_rows("2026-09-25")      # 周六 / 窗口前（周五）
                + _day_rows("2027-01-04"))                               # today 之后
        loaded = fx.load_rows(_make_db(tmp_path, rows), P, "2026-12-31")
        assert loaded == []

    def test_unsettled_rows_carry_no_return(self, tmp_path):
        loaded = fx.load_rows(_make_db(tmp_path, _day_rows("2026-09-28", ripe=False)), P, "2026-12-31")
        assert loaded and all(r["ret"] is None for r in loaded)

    def test_quarantine_excludes_only_the_named_hypothesis(self, tmp_path, monkeypatch):
        monkeypatch.setattr(signal_archive, "QUARANTINE", [
            {"date": "2026-09-28", "signals": ("agent.BuzzBeeWhisper.score",), "reason": "t"}])
        rows = fx.load_rows(_make_db(tmp_path, _day_rows("2026-09-28")), P, "2026-12-31")
        assert self._h1(rows) == []
        cat = fx.items(P)["catalyst"]
        assert len(fx.weekly_series(rows, cat["value"], cat["signals"], P)) == 1

    def test_week_value_is_equal_weight_mean_of_days(self, tmp_path):
        rows = (_day_rows("2026-09-28", sign=+1) + _day_rows("2026-09-29", sign=-1)
                + _day_rows("2026-09-30", sign=+1))
        w = self._h1(fx.load_rows(_make_db(tmp_path, rows), P, "2026-12-31"))
        assert len(w) == 1
        assert w[0][1] == pytest.approx(1 / 3) and w[0][2] == 3

    def test_truncation_date_cuts_samples_on_and_after_it(self, tmp_path):
        rows = _day_rows("2026-09-28") + _day_rows("2026-10-05")
        loaded = fx.load_rows(_make_db(tmp_path, rows), P, "2026-12-31")
        assert len(self._h1(loaded, truncate_at="2026-10-05")) == 1

    def test_h2_requires_all_five_dims(self, tmp_path):
        rows = _day_rows("2026-09-28", width_dims=("catalyst", "sentiment", "odds"))
        loaded = fx.load_rows(_make_db(tmp_path, rows), P, "2026-12-31")
        h2 = fx.items(P)["H2"]
        assert fx.weekly_series(loaded, h2["value"], h2["signals"], P) == []

    def test_truncated_outcome_fingerprint_is_cannot_judge(self, tmp_path):
        db = _make_db(tmp_path, _day_rows("2026-09-28", sltp_truncated=True))
        res = fx.run(db_path=str(db), today="2026-12-31", repo_root=tmp_path, history=[])
        assert res["status"] == "cannot_judge" and "截断指纹" in res["reason"]

    def test_outcome_must_equal_the_repo_forward_close_column(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ic_diagnostics, "FORWARD_CLOSE_COL", {"t7": "price_t7", "t30": "price_t30"})
        with pytest.raises(fx.ProtocolError):
            fx.load_rows(_make_db(tmp_path, _day_rows("2026-09-28")), P, "2026-12-31")


class TestSettledPrefix:
    def test_pending_week_blocks_later_weeks_until_grace(self):
        rows = [{"date": "2026-09-28", "ret": 1.0}, {"date": "2026-10-05", "ret": None},
                {"date": "2026-10-12", "ret": 1.0}]
        assert fx.settled_prefix(rows, "2026-10-20") == [(2026, 40)]
        # W41 周日 10-11 + 21 天 = 11-01 起按缺失处理
        assert fx.settled_prefix(rows, "2026-11-01") == [(2026, 40), (2026, 41), (2026, 42)]


# ── 2. 统计 ─────────────────────────────────────────────────────────────────

class TestStatistics:
    VALS = [0.12, -0.05, 0.30, 0.08, -0.11, 0.22, 0.04, 0.17, -0.02, 0.09, 0.15, -0.07]

    def test_nw_se_agrees_with_ic_diagnostics_newey_west_t(self):
        m = sum(self.VALS) / len(self.VALS)
        assert m / fx.nw_se(self.VALS, 1) == pytest.approx(ic_diagnostics.newey_west_t(self.VALS, 1))

    def test_p_is_two_sided_t_with_g_minus_1_df(self):
        s = fx.series_test(self.VALS, 0.05, P)
        assert s["df"] == len(self.VALS) - 1
        assert s["p"] == pytest.approx(2 * stats.t.sf(abs(s["t"]), s["df"]))

    def test_ci_uses_the_same_se_and_t_critical_value(self):
        s = fx.series_test(self.VALS, 0.05, P)
        c = stats.t.ppf(0.975, s["df"])
        assert s["ci95"] == pytest.approx((s["ic"] - c * s["se_nw"], s["ic"] + c * s["se_nw"]))

    def test_discriminating_case_normal_would_reject_at_interim(self):
        """G=26、t=2.68 ∈ [z 临界 2.576, t(25) 临界 2.787)：正态两侧 p=0.0074 < 0.01（误判拒绝），
        t(25) 给 0.0129（不拒绝）。执行器必须判不拒绝。"""
        vals = _series(26, 2.68)
        s = fx.series_test(vals, 0.01, P)
        assert s["t"] == pytest.approx(2.68)
        assert math.erfc(2.68 / math.sqrt(2)) < 0.01, "前提：正态近似在这里会拒绝"
        assert s["p"] == pytest.approx(0.01285, abs=2e-4) and not s["reject"]
        res = fx.decide(_weeks(vals), _weeks(vals), P)
        assert res["status"] == "in_progress" and res["verdicts"] == {}

    def test_too_few_weeks_is_not_testable(self):
        assert fx.series_test([0.1, 0.2], 0.05, P)["testable"] is False

    def test_zero_variance_is_explicit(self):
        s = fx.series_test([0.1] * 5, 0.05, P)
        assert s["degenerate"] and s["p"] == 0.0 and s["reject"]
        assert fx.series_test([0.0] * 5, 0.05, P)["p"] == 1.0

    def test_holm_adjustment(self):
        adj = fx.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5})
        assert adj == pytest.approx({"a": 0.04, "c": 0.09, "b": 0.09, "d": 0.5})


# ── 3. 检视 + 固定序列 + 盲化 ────────────────────────────────────────────────

class TestDecide:
    def test_below_first_look_reports_progress_only(self):
        res = fx.decide(_weeks(_series(20, 9.0)), _weeks(_series(20, 9.0)), P)
        assert res["status"] == "not_ready" and res["verdicts"] == {}
        assert res["next_look_at"] == 26 and "descriptive" not in res

    def test_interim_not_crossed_reveals_nothing(self):
        res = fx.decide(_weeks(_series(30, 1.0)), _weeks(_series(30, 1.0)), P,
                        descriptive={"odds": _weeks(_series(30, 5.0))})
        assert res["status"] == "in_progress" and res["looks_done"] == ["中检"]
        assert res["verdicts"] == {} and "descriptive" not in res

    def test_interim_both_rejected_concludes(self):
        res = fx.decide(_weeks(_series(26, 5.0)), _weeks(_series(26, 4.5)), P,
                        descriptive={d: _weeks(_series(26, 0.5)) for d in P.DESCRIPTIVE_DIMS})
        assert res["status"] == "concluded"
        assert {h: v["result"] for h, v in res["verdicts"].items()} == {
            "H1": "significant_positive", "H2": "significant_positive"}
        assert set(res["descriptive"]) == set(P.DESCRIPTIVE_DIMS)

    def test_h2_not_rejected_at_interim_stays_blinded(self):
        res = fx.decide(_weeks(_series(30, 5.0)), _weeks(_series(30, 0.5)), P,
                        descriptive={"odds": _weeks(_series(30, 5.0))})
        assert res["status"] == "in_progress"
        assert set(res["verdicts"]) == {"H1"}
        assert "descriptive" not in res

    def test_interim_uses_only_the_first_26_weeks(self):
        h1 = _weeks(_series(26, 5.0) + [-50.0] * 10)
        res = fx.decide(h1, _weeks(_series(36, 4.0)), P)
        assert res["verdicts"]["H1"]["result"] == "significant_positive"
        assert res["verdicts"]["H1"]["G"] == 26

    def test_final_h1_not_significant_concludes_and_h2_not_tested(self):
        res = fx.decide(_weeks(_series(52, 0.3)), _weeks(_series(52, 6.0)), P)
        assert res["status"] == "concluded"
        assert res["verdicts"]["H1"]["result"] == "not_significant"
        assert res["verdicts"]["H1"]["look"] == "终检"
        assert res["verdicts"]["H2"]["result"] == "not_tested"

    def test_h1_rejected_at_final_then_h2_tested_at_final_alpha(self):
        h1 = _series(52, 3.0)
        assert not fx.series_test(h1[:26], 0.01, P)["reject"], "前提：中检不应已拒绝"
        res = fx.decide(_weeks(h1), _weeks(_series(52, 0.5)), P)
        assert res["status"] == "concluded"
        assert res["verdicts"]["H1"]["result"] == "significant_positive"
        assert res["verdicts"]["H1"]["alpha"] == 0.04
        assert res["verdicts"]["H2"]["result"] == "not_significant"

    def test_significant_negative_maps_to_review_not_flip(self):
        res = fx.decide(_weeks(_series(26, -5.0)), _weeks(_series(26, 0.0)), P)
        v = res["verdicts"]["H1"]
        assert v["result"] == "significant_negative" and "不自动取反" in v["action"]

    def test_h1_truncated_below_next_look_is_untestable(self):
        res = fx.decide(_weeks(_series(10, 9.0)), _weeks(_series(10, 9.0)), P, h1_capacity=12)
        assert res["status"] == "concluded"
        assert res["verdicts"]["H1"]["result"] == "untestable"
        assert res["verdicts"]["H2"]["result"] == "not_tested"

    def test_h1_truncated_but_capacity_sufficient_keeps_waiting(self):
        res = fx.decide(_weeks(_series(10, 9.0)), _weeks(_series(10, 9.0)), P, h1_capacity=30)
        assert res["status"] == "not_ready"

    def test_h2_truncated_short_is_untestable(self):
        res = fx.decide(_weeks(_series(26, 5.0)), _weeks(_series(12, 5.0)), P, h2_truncated=True)
        assert res["verdicts"]["H2"]["result"] == "untestable"

    def test_h2_weight_history_unknown_is_untestable_when_its_turn_comes(self):
        res = fx.decide(_weeks(_series(26, 5.0)), _weeks(_series(26, 5.0)), P, h2_unknown="git 不可用")
        assert res["verdicts"]["H2"]["result"] == "untestable"
        assert "git 不可用" in res["verdicts"]["H2"]["reason"]

    def test_h2_uses_only_weeks_up_to_the_h1_cut(self):
        h2 = _series(26, 5.0) + [-50.0] * 10
        res = fx.decide(_weeks(_series(26, 5.0)), _weeks(h2), P)
        assert res["verdicts"]["H2"]["G"] == 26


# ── 4. 截断 ─────────────────────────────────────────────────────────────────

class TestTruncationPoint:
    @pytest.fixture(autouse=True)
    def _scopes(self, monkeypatch):
        monkeypatch.setattr(signal_archive, "COHORT_SIGNAL_SCOPE", {
            **signal_archive.COHORT_SIGNAL_SCOPE,
            "vT_buzz": ("agent.BuzzBeeWhisper.*",), "vT_scout": ("agent.ScoutBeeNova.*",),
            "vT_queen": ()})

    def _h(self, k):
        return fx.items(P)[k]["signals"]

    def test_buzz_boundary_truncates_h1_and_h2(self):
        hist = [("2026-10-20", "vT_buzz", "改了 Buzz")]
        assert fx.truncation_point(self._h("H1"), P, hist) == {"date": "2026-10-20", "version": "vT_buzz"}
        assert fx.truncation_point(self._h("H2"), P, hist)["date"] == "2026-10-20"

    def test_scout_boundary_touches_neither_hypothesis(self):
        hist = [("2026-10-20", "vT_scout", "改了 Scout")]
        assert fx.truncation_point(self._h("H1"), P, hist) is None
        assert fx.truncation_point(self._h("H2"), P, hist) is None
        assert fx.truncation_point(self._h("signal"), P, hist)["date"] == "2026-10-20"

    def test_queen_only_boundary_touches_nothing(self):
        assert fx.truncation_point(self._h("H1"), P, [("2026-10-20", "vT_queen", "")]) is None

    def test_boundary_before_window_is_ignored(self):
        assert fx.truncation_point(self._h("H1"), P, [("2026-09-26", "vT_buzz", "")]) is None

    def test_undeclared_boundary_is_conservatively_everything(self):
        assert fx.truncation_point(self._h("H1"), P, [("2026-11-02", "v_undeclared", "")])["date"] == "2026-11-02"

    def test_earliest_touching_boundary_wins(self):
        hist = [("2026-12-01", "vT_buzz", ""), ("2026-11-02", "vT_buzz", "")]
        assert fx.truncation_point(self._h("H1"), P, hist)["date"] == "2026-11-02"


_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}


def _config_repo(tmp_path, commits):
    """commits: [(日期, 权重 dict)]。每个提交写一版 config.py。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    base = {k: v for k, v in os.environ.items() if k not in fx._GIT_DIR_VARS}

    def git(*a, date=None):
        env = {**base, **_GIT_ENV}
        if date:
            env.update(GIT_AUTHOR_DATE=f"{date}T12:00:00-07:00", GIT_COMMITTER_DATE=f"{date}T12:00:00-07:00")
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env=env)

    git("init", "-q")
    for date, w in commits:
        (repo / "config.py").write_text(f"X = 1\nEVALUATION_WEIGHTS = {w!r}\n", encoding="utf-8")
        git("add", "-A")
        git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", date, date=date)
    return repo


class TestWeightChange:
    FROZEN = dict(P.FROZEN_WEIGHTS)
    OTHER = {**P.FROZEN_WEIGHTS, "catalyst": 0.4, "odds": 0.275}

    def test_no_change(self, tmp_path):
        repo = _config_repo(tmp_path, [("2026-09-09", self.FROZEN), ("2026-10-01", self.FROZEN)])
        assert fx.weight_change(P, "2026-12-31", repo) == {"date": None}

    def test_committed_change_gives_its_date(self, tmp_path):
        repo = _config_repo(tmp_path, [("2026-09-09", self.FROZEN), ("2026-10-05", self.OTHER)])
        wc = fx.weight_change(P, "2026-12-31", repo)
        assert wc["date"] == "2026-10-05" and "提交" in wc["source"]

    def test_change_before_registration_is_ignored(self, tmp_path):
        repo = _config_repo(tmp_path, [("2026-09-01", self.OTHER), ("2026-09-09", self.FROZEN)])
        assert fx.weight_change(P, "2026-12-31", repo) == {"date": None}

    def test_uncommitted_change_counts_from_today(self, tmp_path):
        repo = _config_repo(tmp_path, [("2026-09-09", self.FROZEN)])
        (repo / "config.py").write_text(f"EVALUATION_WEIGHTS = {self.OTHER!r}\n", encoding="utf-8")
        assert fx.weight_change(P, "2026-11-11", repo) == {"date": "2026-11-11", "source": "工作区未提交的改动"}

    def test_not_a_repo_is_unknown_not_unchanged(self, tmp_path):
        (tmp_path / "config.py").write_text(f"EVALUATION_WEIGHTS = {self.FROZEN!r}\n", encoding="utf-8")
        wc = fx.weight_change(P, "2026-12-31", tmp_path)
        assert "unknown" in wc and wc.get("date") is None

    def test_weights_in_source_reads_the_real_config(self):
        w = fx.weights_in_source((_ROOT / "config.py").read_text(encoding="utf-8"))
        assert w is not None and set(w) == set(DIMS)


# ── 5. 编排：盲化白名单 / 陈旧 ─────────────────────────────────────────────

class TestRun:
    def _repo(self, tmp_path):
        return _config_repo(tmp_path, [("2026-09-09", dict(P.FROZEN_WEIGHTS))])

    def test_not_ready_output_has_only_whitelisted_keys(self, tmp_path):
        days = _weekdays("2026-09-28", 15)
        rows = [r for d in days for r in _day_rows(d)]
        res = fx.run(db_path=str(_make_db(tmp_path, rows)), today="2026-12-31",
                     repo_root=self._repo(tmp_path), history=[])
        assert res["status"] == "not_ready" and res["h1_weeks"] == 3
        assert set(res) == _TOP_KEYS_ALWAYS
        assert res["verdicts"] == {}
        forbidden = {"ic", "t", "p", "ci95", "se_nw", "t_iid", "descriptive"}
        assert not (forbidden & set(_all_keys(res)))

    def test_before_window_still_checks_weights(self, tmp_path):
        repo = _config_repo(tmp_path, [("2026-09-09", dict(P.FROZEN_WEIGHTS)),
                                       ("2026-09-25", {**P.FROZEN_WEIGHTS, "odds": 0.2, "catalyst": 0.475})])
        res = fx.run(db_path=str(_make_db(tmp_path, [])), today="2026-09-26", repo_root=repo, history=[])
        assert res["weight_change"]["date"] == "2026-09-25"
        assert fx.status_line(res).startswith("⚠️")

    def test_stale_when_window_long_open_and_empty(self, tmp_path):
        res = fx.run(db_path=str(_make_db(tmp_path, [])), today="2026-11-15",
                     repo_root=self._repo(tmp_path), history=[])
        assert res["stale"] is True
        assert fx.status_line(res).startswith("⚠️")

    def test_missing_db_is_cannot_judge(self, tmp_path):
        res = fx.run(db_path=str(tmp_path / "absent.db"), today="2026-12-31", repo_root=tmp_path, history=[])
        assert res["status"] == "cannot_judge"

    def test_h1_boundary_in_window_shows_in_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(signal_archive, "COHORT_SIGNAL_SCOPE", {
            **signal_archive.COHORT_SIGNAL_SCOPE, "vT_buzz": ("agent.BuzzBeeWhisper.*",)})
        days = _weekdays("2026-09-28", 10)
        res = fx.run(db_path=str(_make_db(tmp_path, [r for d in days for r in _day_rows(d)])),
                     today="2026-12-31", repo_root=self._repo(tmp_path),
                     history=[("2026-10-05", "vT_buzz", "")])
        assert res["truncation"]["H1"]["date"] == "2026-10-05"
        # 截断前只有 1 周 < 26 ⇒ H1 永远到不了中检 ⇒ 无法检验、协议结束
        assert res["verdicts"]["H1"]["result"] == "untestable"
        assert "H1 已于 2026-10-05" in fx.status_line(res)


# ── 6. 进度行 ───────────────────────────────────────────────────────────────

class TestStatusLine:
    def test_not_ready_line_has_counts_and_no_effect(self):
        line = fx.status_line({"status": "not_ready", "h1_weeks": 3, "next_look_at": 26,
                               "forward_start": "2026-09-28", "truncation": {}, "weight_change": {}})
        assert line.startswith("⏳") and "3/26" in line
        import re
        assert not re.search(r"[+-]\d+\.\d+|p=", line), "进度行里出现了像效应量的数字"

    def test_cannot_judge_line_carries_reason(self):
        line = fx.status_line({"status": "cannot_judge", "reason": "路径错了"})
        assert line.startswith("⚠️") and "路径错了" in line

    def test_concluded_line_points_to_the_script(self):
        line = fx.status_line({"status": "concluded", "verdicts": {"H1": {"result": "not_significant"},
                                                                    "H2": {"result": "not_tested"}},
                               "truncation": {}, "weight_change": {}})
        assert line.startswith("🔔") and "dim_ic_forward_test.py" in line and "H1=not_significant" in line


# ── 7. 静态：执行器源码不碰禁用列、不用正态近似 ─────────────────────────────

def _code_tokens(src: str):
    """源码里除 docstring / 注释之外的名字与字符串常量。"""
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.value
        elif isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr


def _violations(src: str):
    out = []
    for tok in _code_tokens(src):
        for bad in ("price_t7", "return_t7", "normal_two_sided_p", "erfc", "NormalDist"):
            if bad in tok:
                out.append((bad, tok[:60]))
    return out


class TestStaticGuards:
    def test_executor_source_is_clean(self):
        assert _violations(_SRC.read_text(encoding="utf-8")) == []

    @pytest.mark.parametrize("snippet", [
        'con.execute("SELECT price_t7 FROM predictions")',
        "x = ic_diagnostics.normal_two_sided_p(t)",
        "p = math.erfc(abs(t) / math.sqrt(2))",
        'col = "return_t7"',
    ])
    def test_guard_has_teeth(self, snippet):
        assert _violations(snippet), f"静态守卫没抓到：{snippet}"


# ── 8. 接线：就绪度闸 ────────────────────────────────────────────────────────

class TestCarriedByReadiness:
    def _stub(self, monkeypatch, rr):
        monkeypatch.setattr(rr, "assess", lambda *a, **k: {
            "ready": False, "cohort": {"date": "2026-09-18", "version": "v0", "reason": "", "n_generations": 1},
            "target_ic": 0.09, "weeks_required": 25, "n_ripe_samples": 0, "n_all_samples": 0,
            "weeks_accrued": 0, "weeks_remaining": 25, "scan_weeks_in_cohort": 0,
            "calendar_weeks_elapsed": 0, "weeks_per_calendar_week": 0.0, "eta_date": None,
            "eta_calendar_weeks": 0, "pool_note": "", "next_step": "?"})
        monkeypatch.setattr(rr, "resonance_forward_status",
                            lambda *a, **k: {"status": "not_ready", "line": "⏳ 共振加成前瞻检验：桩"})
        monkeypatch.setattr(rr, "fg_exposure_gate_forward_status",
                            lambda *a, **k: {"status": "not_ready", "line": "⏳ F&G 敞口门前瞻检验：桩"})

    def test_quiet_line_appends_dim_segment_last(self, monkeypatch, tmp_path, capsys):
        """段顺序是契约：周度任务 SKILL.md 按「第三段 = F&G」解析，新段只许追加在末尾。"""
        import sys
        import ic_rerun_readiness as rr
        self._stub(monkeypatch, rr)
        db = _make_db(tmp_path, [])
        monkeypatch.setattr(sys, "argv", ["ic_rerun_readiness.py", "--db", str(db), "--quiet",
                                          "--today", "2026-10-01"])
        rr.main()
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        segs = out[0].split("｜")
        assert len(segs) == 4
        assert "F&G" in segs[2] and "维度 IC 协议" in segs[3]

    def test_json_payload_has_dim_key(self, monkeypatch, tmp_path, capsys):
        import sys
        import ic_rerun_readiness as rr
        self._stub(monkeypatch, rr)
        db = _make_db(tmp_path, [])
        monkeypatch.setattr(sys, "argv", ["ic_rerun_readiness.py", "--db", str(db), "--json",
                                          "--today", "2026-10-01"])
        rr.main()
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["dim_ic_forward_test"]) == {"status", "line"}

    def test_load_failure_renders_as_a_line_not_an_exception(self, monkeypatch, tmp_path):
        import ic_rerun_readiness as rr
        monkeypatch.setattr(rr, "ALPHAHIVE_DIR", tmp_path / "nowhere")
        res = rr.dim_ic_forward_status(tmp_path / "p.db", today="2026-10-01")
        assert res["status"] == "cannot_judge" and res["line"].startswith("⚠️ 维度 IC 协议无法判定")
