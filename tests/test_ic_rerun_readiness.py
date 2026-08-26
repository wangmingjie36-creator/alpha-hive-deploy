"""
IC 重跑就绪度判定（v0.44.4）

为什么需要这个工具、以及为什么需要测它
--------------------------------------
v0.44.1~0.44.3 修了 ML 预期收益的看多偏斜并把 RivalBee 三个硬编码特征接上真实数据，
但**方向是否变准没有验证** —— 那要等新样本（实测约 **25 个不重叠周**，
见 `experiments/ic_power_report.md`）。

"等攒够"这件事本来没有承载物：不在测试里（测试跑当下）、不在告警里（没有异常），
全靠人记着。半年后没人记得。`ic_rerun_readiness.py` 就是那个承载物。

而它自己也必须被测：**一个永远说"未就绪"的就绪度判定器，和没有它是一样的。**
下面 `TestVerdictActuallyFlips` 就是为此 —— 与
`test_distribution_invariants.py::TestGuardsHaveTeeth` 同一思路。
"""

import sqlite3

import pytest

import ic_rerun_readiness as rr


@pytest.fixture
def db(tmp_path):
    """建一个最小 predictions 表的工厂。

    rows: [(date, ticker, ripe)]  ripe=True 表示 T+7 已回填
    """
    counter = {"n": 0}

    def _make(rows):
        # 每次调用建独立文件 —— 有测试在同一个用例里建两个库做对照
        counter["n"] += 1
        p = tmp_path / f"t{counter['n']}.db"
        con = sqlite3.connect(p)
        con.execute(
            "CREATE TABLE predictions ("
            " date TEXT, ticker TEXT, checked_t7 INTEGER,"
            " price_t7 REAL, price_at_predict REAL)"
        )
        con.executemany(
            "INSERT INTO predictions VALUES (?,?,?,?,?)",
            [(d, t, 1 if ripe else 0, 110.0 if ripe else None, 100.0)
             for d, t, ripe in rows],
        )
        con.commit()
        con.close()
        return p
    return _make


# v0.45.31: 起始日不再硬编码。此前默认写死 "2026-08-17"（当时的最新世代），
# v0.45.30 追加新世代边界后，这些样本全部落到边界之前被过滤掉，7 个测试变红。
# 世代边界会持续追加，硬编码必然反复失效 —— 一律从 _COHORT_HISTORY 末条推导。
_COHORT_START = rr._COHORT_HISTORY[-1][0]


def _after_cohort(weeks=0):
    """世代起始日之后 N 周的日期（供测试构造世代内样本）。"""
    import datetime as dt
    return (dt.date.fromisoformat(_COHORT_START) + dt.timedelta(weeks=weeks)).isoformat()


def _weekly_rows(n_weeks, start=None, tickers=("AAA", "BBB"),
                 ripe=True):
    """每周一条（不重叠取样单位就是周），共 n_weeks 周。默认从当前世代起始日开始。"""
    import datetime as dt
    d0 = dt.date.fromisoformat(start or _COHORT_START)
    out = []
    for w in range(n_weeks):
        d = (d0 + dt.timedelta(weeks=w)).isoformat()
        for t in tickers:
            out.append((d, t, ripe))
    return out


# ════════════════════════════════════════════════════════════════════════════
# 世代边界
# ════════════════════════════════════════════════════════════════════════════

class TestCohortBoundary:

    def test_uses_the_latest_generation(self):
        c = rr.cohort_start()
        assert c["date"] == rr._COHORT_HISTORY[-1][0]
        assert c["n_generations"] == len(rr._COHORT_HISTORY)

    def test_history_is_append_only_and_ordered(self):
        """世代历史是审计轨迹：只追加、按时间递增。"""
        dates = [d for d, _, _ in rr._COHORT_HISTORY]
        assert dates == sorted(dates), f"世代边界未按时间排序: {dates}"
        assert len(set(dates)) == len(dates), "世代边界有重复日期"

    def test_every_generation_records_why(self):
        """只有日期没有原因的边界，半年后无法判断它是否仍然适用。"""
        for date, version, reason in rr._COHORT_HISTORY:
            assert len(date) == 10 and date[4] == "-"
            assert version and reason, f"{date} 缺版本或原因"

    def test_samples_before_boundary_are_excluded(self, db):
        """世代之前的样本必须一条都不算 —— 混算是静默的，数字照出但没意义。

        注意早期序列取的是当前世代边界之前的 20 周，全部应被过滤。
        跨过边界就测不到过滤逻辑了，故起止日均由 _COHORT_START 推导。
        """
        import datetime as dt
        _early = (dt.date.fromisoformat(_COHORT_START) - dt.timedelta(weeks=32)).isoformat()
        rows = (_weekly_rows(20, start=_early)            # 全部早于边界
                + _weekly_rows(2))                        # 世代内 2 周
        res = rr.assess(db_path=db(rows), today=_after_cohort(3))
        assert res["weeks_accrued"] == 2, "边界之前的样本被算进来了"
        assert res["n_all_samples"] == 4, "世代内总样本数也应只数世代内的"


# ════════════════════════════════════════════════════════════════════════════
# 判据真的会翻转
# ════════════════════════════════════════════════════════════════════════════

class TestVerdictActuallyFlips:
    """一个永远说"未就绪"的判定器 = 没有判定器。

    与 `test_distribution_invariants.py::TestGuardsHaveTeeth` 同一思路：
    喂足量数据必须翻成"已就绪"。
    """

    def test_not_ready_when_short(self, db):
        res = rr.assess(db_path=db(_weekly_rows(5)), today="2026-09-21")
        assert res["ready"] is False
        assert res["weeks_accrued"] == 5
        assert "未就绪" in rr.summary_line(res)

    def test_ready_when_enough_weeks_accrued(self, db):
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        res = rr.assess(db_path=db(_weekly_rows(need)), today="2027-03-01")
        assert res["ready"] is True, f"攒到 {need} 周仍判未就绪"
        assert res["weeks_remaining"] == 0
        assert "已就绪" in rr.summary_line(res)

    def test_exactly_one_week_short_is_not_ready(self, db):
        """边界条件：差一周就是差一周，不许四舍五入。"""
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        res = rr.assess(db_path=db(_weekly_rows(need - 1)), today="2027-03-01")
        assert res["ready"] is False
        assert res["weeks_remaining"] == 1

    def test_lower_bar_becomes_ready_sooner(self, db):
        """只想检出更强的信号（|IC|=0.135）时门槛更低 —— 11 周而非 25 周。"""
        rows = _weekly_rows(12)
        assert rr.assess(db_path=db(rows), today="2026-11-09",
                         target_ic=0.135)["ready"] is True
        assert rr.assess(db_path=db(rows), today="2026-11-09",
                         target_ic=0.090)["ready"] is False


# ════════════════════════════════════════════════════════════════════════════
# 只数"已回填 T+7"的样本
# ════════════════════════════════════════════════════════════════════════════

class TestOnlyRipeSamplesCount:

    def test_unripe_samples_do_not_count_toward_weeks(self, db):
        """未到期样本不能算进不重叠周 —— 它们进不了 IC 计算。

        这条很容易写错：`predictions` 里未到期行的 `checked_t7=0`，
        只按日期过滤会把它们算进来，于是就绪度提前变绿。
        """
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        rows = _weekly_rows(need, ripe=False)
        res = rr.assess(db_path=db(rows), today="2027-03-01")
        assert res["weeks_accrued"] == 0
        assert res["ready"] is False
        assert res["n_all_samples"] > 0, "总样本数应仍然可见（供看进度）"
        assert res["n_ripe_samples"] == 0

    def test_progress_visible_before_anything_ripens(self, db):
        """T+7 有 7 天滞后，刚开始时"已扫描的周"应该已经在动。"""
        rows = _weekly_rows(3, ripe=False)
        res = rr.assess(db_path=db(rows), today="2026-09-07")
        assert res["scan_weeks_in_cohort"] == 3
        assert res["weeks_accrued"] == 0


# ════════════════════════════════════════════════════════════════════════════
# 标的池漂移会打断世代
# ════════════════════════════════════════════════════════════════════════════

class TestPoolDriftBreaksCohort:
    """世代内换了标的池，样本同样不可比 —— 与
    `weekly_optimizer.check_ticker_pool_consistency` 同一思路。
    """

    def test_stable_pool_has_no_note(self, db):
        res = rr.assess(db_path=db(_weekly_rows(6)), today="2026-09-28")
        assert res["pool_note"] is None
        assert res["pool_drift"] == pytest.approx(0.0)

    def test_pool_swap_is_flagged_and_blocks_ready(self, db):
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        rows = (_weekly_rows(need - 3, tickers=("AAA", "BBB"))
                + _weekly_rows(3, start="2027-02-01",
                               tickers=("XXX", "YYY", "ZZZ")))
        res = rr.assess(db_path=db(rows), today="2027-03-01")
        assert res["pool_note"] is not None
        assert res["ready"] is False, "池被换掉仍判就绪 —— 样本已不可比"
        assert "世代" in rr.summary_line(res)

    def test_small_addition_does_not_break_cohort(self, db):
        """加 1 只到 10 只池（<20% 门槛）不该打断 —— 闸不能过敏。"""
        ten = tuple(f"T{i}" for i in range(10))
        rows = (_weekly_rows(6, tickers=ten)
                + _weekly_rows(2, start="2026-09-28", tickers=ten + ("NEW",)))
        res = rr.assess(db_path=db(rows), today="2026-10-12")
        assert res["pool_note"] is None


# ════════════════════════════════════════════════════════════════════════════
# ETA 与退出码
# ════════════════════════════════════════════════════════════════════════════

class TestEtaAndExitCodes:

    def test_eta_uses_observed_rate_not_calendar(self, db):
        """ETA 必须按**实际周产出率**外推。

        扫描覆盖率实测只有 36.7%，按日历周外推会给出过于乐观的日期。
        """
        # 8 个日历周里只产出 4 个扫描周 ⇒ 产出率 0.5
        rows = _weekly_rows(4)
        res = rr.assess(db_path=db(rows), today="2026-10-12")
        assert res["weeks_per_calendar_week"] < 1.0
        assert res["eta_calendar_weeks"] > res["weeks_remaining"], (
            "ETA 没有把产出率折算进去"
        )

    def test_no_output_yet_gives_no_false_eta(self, db):
        res = rr.assess(db_path=db([]), today="2026-08-24")
        assert res["eta_date"] is None
        assert "扫描连续性" in rr.summary_line(res)

    def _run(self, monkeypatch, argv):
        import sys as _s
        monkeypatch.setattr(_s, "argv", ["ic_rerun_readiness.py", *argv])
        return rr.main()

    def test_missing_db_returns_3_not_2(self, monkeypatch, tmp_path):
        """2 是编排器 `run_step()` 保留给"脚本不存在"的码，不可占用。"""
        rc = self._run(monkeypatch, ["--db", str(tmp_path / "absent.db")])
        assert rc == 3

    def test_not_ready_returns_1(self, monkeypatch, db):
        rc = self._run(monkeypatch, ["--db", str(db(_weekly_rows(2))),
                                     "--today", _after_cohort(3), "--quiet"])
        assert rc == 1

    def test_ready_returns_0(self, monkeypatch, db):
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        rc = self._run(monkeypatch, ["--db", str(db(_weekly_rows(need))),
                                     "--today", "2027-03-01", "--quiet"])
        assert rc == 0

    def test_json_mode_keeps_keys_the_task_relies_on(self, monkeypatch, db,
                                                     capsys):
        import json
        rc = self._run(monkeypatch, ["--db", str(db(_weekly_rows(2))),
                                     "--today", _after_cohort(3), "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        for key in ("cohort", "weeks_required", "weeks_accrued",
                    "weeks_remaining", "eta_date", "pool_note", "ready",
                    "next_step"):
            assert key in payload, f"周度任务依赖的键 {key} 消失了"


class TestRequirementsTraceToPowerReport:
    """`_WEEKS_REQUIRED` 的数字必须与功效报告一致 —— 它是本工具的唯一判据来源。"""

    def test_default_target_matches_measured_composite_ic(self):
        assert rr.DEFAULT_TARGET_IC == 0.090, (
            "默认判据应是系统综合分的实测 |IC|=0.090"
        )

    def test_weeks_required_is_monotonic_in_ic(self):
        """越强的信号越容易检出 —— 所需周数必须随 |IC| 单调递减。"""
        ics = sorted(rr._WEEKS_REQUIRED)
        weeks = [rr._WEEKS_REQUIRED[i] for i in ics]
        assert weeks == sorted(weeks, reverse=True), f"非单调: {dict(zip(ics, weeks))}"

    def test_headline_numbers_match_the_report(self):
        """钉住报告里的三个关键行，防止两处悄悄分叉。"""
        assert rr._WEEKS_REQUIRED[0.090] == 25    # 系统综合分实测
        assert rr._WEEKS_REQUIRED[0.077] == 35    # 噪音地板
        assert rr._WEEKS_REQUIRED[0.135] == 11    # 20 日动量基准
