"""评分重放工具的守卫（v0.45.33）。

重点不在「算得对」，而在**不会被误读**：
- 功效不足时必须明说，且退出码非 0
- 有效样本量必须按不重叠周报，naive n 会高估数倍
- 默认只用最新世代，跨世代必须显式放宽并标注不可比
- 收益口径必须是未截断的 close_t7，用 return_t7 是无效对比
"""

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import replay_scoring as rs  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(tmp_path):
    def _make(rows):
        """rows: (date, ticker, dims dict, p0, close_t7, ambiguous)"""
        p = str(tmp_path / "t.db")
        con = sqlite3.connect(p)
        con.execute(
            "CREATE TABLE predictions (date TEXT, ticker TEXT, final_score REAL,"
            " dimension_scores TEXT, price_at_predict REAL, close_t7 REAL,"
            " dir_ambiguous_t7 INTEGER, return_t7 REAL)")
        con.executemany(
            "INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)",
            [(d, t, sum(dims.values()) / len(dims), json.dumps(dims), p0, c7, amb, 999.0)
             for d, t, dims, p0, c7, amb in rows])
        con.commit()
        con.close()
        return p
    return _make


def _rows(n_weeks, start="2026-01-05", tickers=("AAA", "BBB"), amb=0):
    out = []
    d0 = dt.date.fromisoformat(start)
    for w in range(n_weeks):
        d = (d0 + dt.timedelta(weeks=w)).isoformat()
        for i, t in enumerate(tickers):
            dims = {k: 5.0 + i + w * 0.1 for k in rs.DIMS}
            out.append((d, t, dims, 100.0, 100.0 + i + w, amb))
    return out


class TestPowerHonesty:
    """功效护栏 —— 这是本工具最重要的部分。"""

    def test_reports_iso_weeks_not_just_n(self, db):
        r = rs.evaluate("x", lambda row: row["dims"]["signal"],
                        rs.load_samples(db(_rows(4)), all_cohorts=True)["rows"])
        assert r["weeks"] == 4, "有效样本量必须按不重叠 ISO 周报"
        assert r["n"] == 8, "naive n 也要报，但不能只报它"

    def test_underpowered_run_exits_nonzero(self, db, monkeypatch):
        """喂退化：只有 4 周（远低于 25）→ 必须非 0 退出码 + 明确警示。"""
        monkeypatch.setattr(rs, "DB_PATH", db(_rows(4)))
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py", "--all-cohorts"])
        code = rs.main()
        assert code == 1, "功效不足却返回 0 —— 会被脚本当成通过"

    def test_no_samples_exits_3_not_0(self, db, monkeypatch):
        monkeypatch.setattr(rs, "DB_PATH", db([]))
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py"])
        assert rs.main() == 3

    def test_cli_prints_power_warning(self):
        """端到端：真实库跑一次，输出里必须有功效结论。"""
        out = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "replay_scoring.py"), "--all-cohorts"],
            capture_output=True, text=True, timeout=180, cwd=_ROOT)
        combined = out.stdout + out.stderr
        if "无可用样本" in combined:
            pytest.skip("生产库无到期样本")
        assert ("功效不足" in combined) or ("达到检出" in combined), \
            "输出未给出功效结论，IC 会被当成可直接采信的数字"
        assert "不重叠" in combined


class TestCohortDefault:
    def test_defaults_to_latest_cohort(self, db, monkeypatch):
        """默认只取最新世代 —— 混算是静默的，数字照出但没意义。"""
        boundary = "2026-06-01"
        monkeypatch.setattr(rs, "latest_cohort_start", lambda: boundary)
        rows = _rows(3, start="2026-01-05") + _rows(2, start="2026-06-01")
        d = rs.load_samples(db(rows))
        assert all(r["date"] >= boundary for r in d["rows"]), "世代之前的样本被算进来了"
        assert d["cohort_start"] == boundary

    def test_all_cohorts_flags_incomparability(self, db):
        d = rs.load_samples(db(_rows(3)), all_cohorts=True)
        assert any("不可比" in n for n in d["notes"]), \
            "跨世代混算却没标注不可比"


class TestCleanReturnCaliber:
    def test_uses_close_t7_not_return_t7(self, db):
        """return_t7 对方向单是钳位离场收益，直接用即无效对比。
        构造：return_t7 全填 999，若被误用，前向收益就会变成常数。"""
        d = rs.load_samples(db(_rows(3)), all_cohorts=True)
        vals = {round(r["fwd_return_pct"], 4) for r in d["rows"]}
        assert 999.0 not in vals, "用了 return_t7 —— 钳位口径不可比"
        assert len(vals) > 1, "前向收益成了常数，取数有误"

    def test_ambiguous_samples_excluded(self, db):
        rows = _rows(3) + _rows(2, start="2026-03-02", amb=1)
        d = rs.load_samples(db(rows), all_cohorts=True)
        assert len(d["rows"]) == 6
        assert any("模糊样本" in n for n in d["notes"])


class TestNoWeightRecommendation:
    """工具不得给出「最优权重」建议 —— 权重 v0.44.0 起只读。"""

    def test_no_optimizer_shaped_api(self):
        forbidden = {"best_weights", "optimize", "recommend_weights", "fit_weights",
                     "tune", "search_weights"}
        public = {n for n in dir(rs) if not n.startswith("_")}
        assert not (public & forbidden), f"出现调参出口：{public & forbidden}"

    def test_output_states_the_caveat(self, db, monkeypatch, capsys):
        monkeypatch.setattr(rs, "DB_PATH", db(_rows(4)))
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py", "--all-cohorts"])
        rs.main()
        assert "最优权重" in capsys.readouterr().out


class TestDimensionInputsArchived:
    """#5 的验收：维度输入已入档，维度计算层的改动才可重放。"""

    def test_new_input_signals_registered(self):
        from signal_archive import SIGNAL_EXTRACTORS as S
        for sig in ("crowding.comp.social_volume", "catalyst.count",
                    "catalyst.nearest_days", "catalyst.max_weight",
                    "buzz.comp.momentum_signal", "options.iv_rank_is_real"):
            assert sig in S, f"{sig} 未注册 —— 该维度的计算改动仍无法重放"

    def test_crowding_extractor_reads_legacy_key(self):
        """v0.45.30 前后键名不同，读不了旧名等于丢掉改名前的全部历史样本。"""
        from signal_archive import SIGNAL_EXTRACTORS as S
        fn = S["crowding.comp.social_volume"]
        legacy = {"agent_details": {"ScoutBeeNova": {"details": {
            "components": {"stocktwits_volume": 42.0}}}}}
        assert fn(legacy) == 42.0, "旧键名读不到"
        current = {"agent_details": {"ScoutBeeNova": {"details": {
            "components": {"social_volume": 7.0}}}}}
        assert fn(current) == 7.0

    def test_catalyst_count_none_when_source_missing(self):
        """来源不可得（v0.45.31 返回 error、details 缺失）必须是 None，不是 0。
        0 的语义是「查过了，确实没有」。"""
        from signal_archive import SIGNAL_EXTRACTORS as S
        fn = S["catalyst.count"]
        assert fn({"agent_details": {"ChronosBeeHorizon": {}}}) is None
        assert fn({"agent_details": {"ChronosBeeHorizon": {
            "details": {"catalysts": []}}}}) == 0.0

    def test_inputs_attachable_to_samples(self, db):
        d = rs.load_samples(db(_rows(2)), all_cohorts=True, with_inputs=True)
        assert all("inputs" in r for r in d["rows"])
