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
            " close_t7 REAL, price_at_predict REAL)"
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

    #: 已知世代的**最小集合**：这些 (日期, 版本) 必须在表里。
    #: ⚠️ **刻意写死，不许从 `_COHORT_HISTORY` 派生** —— 派生即恒真。
    #: 追加新世代不需要动这里（子集断言）；只有**删掉**一条历史世代才会红。
    #: 表头写着「只追加，不改写（审计轨迹）」，此前没有任何东西执行这句话。
    MUST_BE_ENUMERATED = frozenset({
        ("2026-08-17", "v0.44.1~0.44.3"),
        ("2026-08-26", "v0.45.30"),
        ("2026-08-27", "v0.45.50"),
        ("2026-09-05", "v0.45.128"),
        ("2026-09-07", "v0.45.151"),
        ("2026-09-07", "v0.45.156"),
        ("2026-09-07", "v0.45.163"),
        # v0.45.334：GexRegimeModifier 断开（作废 120 条的那条；原定 09-24 未赶上扫描，改 09-28）。
        # 钉住它也钉住了 `test_cohort_start_never_moves_backwards` 的下限 —— 删掉它，边界会退回 09-18。
        ("2026-09-28", "v0.45.334"),
    })

    def test_no_known_cohort_has_vanished(self):
        """审计轨迹只能变长。

        v0.45.166 实测（变异）：删掉**最新**一条 → 22 项全绿；
        删掉**最早**一条 → 同样 22 项全绿。**两头都没人看守** ——
        比姊妹表 `probability_scorecard._ML_ESTIMATOR_GENERATIONS` 还松
        （那张至少钉住了最早那条）。
        """
        assert self.MUST_BE_ENUMERATED, "最小集合被清空了——本条已退化为恒真"
        live = {(d, v) for d, v, _ in rr._COHORT_HISTORY}
        missing = self.MUST_BE_ENUMERATED - live
        assert not missing, (
            f"世代边界表少了已知条目 {sorted(missing)}；表头写明「只追加，不改写」。"
            "若确实要改写审计轨迹，请连同本最小集合一起显式修改。")

    def test_cohort_start_never_moves_backwards(self):
        """成对的另一半：钉的是**消费方看到的值**，不是表的内容。

        这两条抓的东西不一样，缺一不可：
          · 删掉**中间**一条 → 只有 `test_no_known_cohort_has_vanished` 红。
          · 删掉**末尾**一条 → 两条都红，而这条说出的是**后果**：
            `cohort_start()` 取 `[-1]`，末条一没，边界就**往回退**，
            于是上一代的样本被静默并进当前世代 —— 正是这张表存在要防的那件事
            （`assess()` 按 `date >= 边界` 过滤，边界退了没有任何告警）。

        对追加安全：新世代只会把边界往后推，`>=` 恒成立。
        """
        pinned_max = max(d for d, _ in self.MUST_BE_ENUMERATED)
        got = rr.cohort_start()["date"]
        assert got >= pinned_max, (
            f"世代边界回退了：cohort_start()={got} 早于已知的 {pinned_max}。"
            "边界前的样本会被当作本代样本混算，且混算是静默的。")

    def test_history_is_append_only_and_ordered(self):
        """世代历史是审计轨迹：只追加、按时间递增，(日期, 版本) 不重复。

        v0.45.156：原断言是 `len(set(dates)) == len(dates)`（**日期**唯一）。
        它与同一份表 docstring 里「再次改动 … **必须追加一条**」直接冲突——
        同一天部署两个都改 final_score 的版本时，照规矩追加就会让它变红，
        于是它保护的不是不变式，而是「别在同一天改两次」。
        （与 v0.45.151 改 `TestCohortBoundaryAppended` 是同一物种。）

        真正的不变式：日期**非降**（`sorted` 本就允许并列）+ (日期, 版本) 唯一。
        同日多条不影响语义——`cohort_start()` 取 `[-1]`，`assess` 按 `date >= 边界`
        过滤，两者都只看日期值本身。
        """
        entries = [(d, v) for d, v, _ in rr._COHORT_HISTORY]
        dates = [d for d, _ in entries]
        assert dates == sorted(dates), f"世代边界未按时间排序: {dates}"
        assert len(set(entries)) == len(entries), f"(日期, 版本) 重复: {entries}"

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
        # v0.45.128：today 由世代起点推导。此前写死 "2026-10-12"，与从 _COHORT_START
        # 推出来的夹具日期是两个钟（v0.45.96 那类定时炸弹）——边界一动就把 8 周变成 4 周。
        rows = _weekly_rows(4)
        res = rr.assess(db_path=db(rows), today=_after_cohort(8))
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


class TestBoundaryDateHasDataEvidence:
    """世代边界的日期，能不能从**数据**上判对错（v0.45.197）。

    背景：本仓记过同一处栽跟头 —— 此前几条边界「核过了」其实**零判别力**，
    因为世代内 0 条样本时，日期写对和写错的输出一模一样
    （auto-memory `alpha-hive-failure-propagation`：「安全性论证与可观测性是
    同一个事实的两面」）。v0.45.197 给口径切换留了个归档印记
    （`advanced_analysis.dealer_gex.chain_view == "cboe_full_expiries"`），
    `cohort_boundary_evidence()` 就是拿它对账的。

    与本文件开篇那句同源：**一个永远说「还没证据」的判别器，和没有判别器是一回事。**
    """

    MARKER = "cboe_full_expiries"

    def _write(self, root, date, view=MARKER, ticker="NVDA"):
        import json as _json
        body = {"advanced_analysis": {"dealer_gex": {}}}
        if view is not None:
            body["advanced_analysis"]["dealer_gex"]["chain_view"] = view
        (root / f"analysis-{ticker}-ml-{date}.json").write_text(
            _json.dumps(body), encoding="utf-8")

    @pytest.fixture
    def boundary(self, monkeypatch):
        # v0.45.334 起印记按版本查表（`_BOUNDARY_MARKERS`）：测试用边界要显式登记它的印记，
        # 否则判别器会如实回 `no_marker`，而不是借 v0.45.197 的印记充数（那正是本版修的 bug）。
        monkeypatch.setattr(
            rr, "_COHORT_HISTORY",
            list(rr._COHORT_HISTORY[:-1]) + [("2026-09-11", "vTEST", "测试用边界")])
        monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vTEST",
                            ("chain_view（测试）", rr._marker_gex_full_chain_view))
        return "2026-09-11"

    def test_marker_on_boundary_date_matches(self, tmp_path, boundary):
        """印记首见日 == 边界日 → matches。

        变红的变异：把 `root = Path(home)` 改成 `root = ALPHAHIVE_DIR`
        —— 那样它去扫代码目录（没有归档）⇒ 恒返回 no_evidence_yet。
        这正是本函数初版的 bug。
        """
        self._write(tmp_path, "2026-09-11")
        assert rr.cohort_boundary_evidence(tmp_path)["verdict"] == "matches"

    def test_marker_later_than_boundary_is_flagged_too_early(self, tmp_path, boundary):
        """印记晚于边界 → boundary_too_early（危险方向：中间那几天是旧口径，会被混算）。

        变红的变异：把 `first > boundary` 与 `first < boundary` 两个分支对调。
        """
        self._write(tmp_path, "2026-09-15")
        ev = rr.cohort_boundary_evidence(tmp_path)
        assert ev["verdict"] == "boundary_too_early"
        assert ev["marker_first_seen"] == "2026-09-15"

    def test_marker_earlier_than_boundary_is_flagged_too_late(self, tmp_path, boundary):
        """印记早于边界 → boundary_too_late（保守方向：白丢了一些新口径样本）。

        变红的变异：同上，两个分支对调。
        """
        self._write(tmp_path, "2026-09-09")
        assert rr.cohort_boundary_evidence(tmp_path)["verdict"] == "boundary_too_late"

    def test_no_marker_is_not_reported_as_matching(self, tmp_path, boundary):
        """没有印记时必须说「还没有证据」，**不能**说「一致」。

        「还没验」和「验过了没问题」必须可区分 —— 本仓六次「失败没传导到下游」
        全是这两者被混成一个。

        变红的变异：把 `verdict = "no_evidence_yet"` 改成 `"matches"`。
        """
        ev = rr.cohort_boundary_evidence(tmp_path)
        assert ev["verdict"] == "no_evidence_yet"
        assert ev["marker_first_seen"] is None

    def test_archives_without_the_marker_are_not_evidence(self, tmp_path, boundary):
        """没有该口径字段的旧归档不算证据 —— 否则判别器会把旧口径认成新口径。

        变红的变异：把 `_marker_gex_full_chain_view` 的 `return view == "cboe_full_expiries"`
        改成 `return True`（v0.45.334 前是 `cohort_boundary_evidence` 里的 `if view == ...`）。
        """
        self._write(tmp_path, "2026-09-01", view=None)
        self._write(tmp_path, "2026-09-02", view="something_else")
        assert rr.cohort_boundary_evidence(tmp_path)["verdict"] == "no_evidence_yet"

    def test_first_seen_is_the_earliest_not_the_last(self, tmp_path, boundary):
        """多天都有印记时取**最早**那天 —— 那才是「首个受影响的业务日」。

        变红的变异：把 `min(first, date)` 改成 `max(first, date)`。

        ⚠️ 夹具的 ticker 名是**刻意**挑的，不是随手起的：归档按文件名排序遍历，
        而文件名是 `analysis-<TICKER>-ml-<DATE>.json` ⇒ **先按 ticker 排，不按日期**。
        若让日期最早的 ticker 恰好排在最前，扫描过程中 `first` 单调下降、
        `min` 与 `max` 走不到分歧点 ⇒ 这条断言对该变异**没有牙**（初版就是这样，
        变异校验当场抓到）。所以要让**日期最晚的 ticker 排在最前**。
        """
        self._write(tmp_path, "2026-09-30", ticker="AAA")   # 排最前、日期最晚
        self._write(tmp_path, "2026-09-20", ticker="MMM")
        self._write(tmp_path, "2026-09-12", ticker="ZZZ")   # 排最后、日期最早
        assert rr.cohort_boundary_evidence(tmp_path)["marker_first_seen"] == "2026-09-12"


class TestBoundaryEvidenceIsPerVersion:
    """v0.45.334：印记按版本查表（`_BOUNDARY_MARKERS`），只和**同一版本**那条边界的日期比。

    修的 bug：旧实现写死认 v0.45.197 的 chain_view 印记，却拿 `_COHORT_HISTORY[-1]` 的日期比。
    197 之后每追加一条边界，它就在拿一件事的印记去核另一件事的日期 —— 表头是 09-18
    （v0.45.315）时实测报 `boundary_too_late`（印记 09-11 早于 09-18），而那两件事毫不相干。
    **一个会误报的判别器和一个永远不报的一样糟**：误报几次之后，真报也没人看了。
    """

    @staticmethod
    def _write(root, date, body, ticker="NVDA"):
        import json as _json
        (root / f"analysis-{ticker}-ml-{date}.json").write_text(_json.dumps(body), encoding="utf-8")

    @staticmethod
    def _chain_view_body():
        return {"advanced_analysis": {"dealer_gex": {"chain_view": "cboe_full_expiries"}}}

    @staticmethod
    def _gex_mod_body(**extra):
        mod = {"gex_adjustment": -0.15, "gex_regime": "positive_gex", **extra}
        return {"swarm_results": {"gex_regime_mod": mod}}

    @staticmethod
    def _date_of(version):
        return next(d for d, v, _r in rr._COHORT_HISTORY if v == version)

    def test_197_marker_is_not_used_to_judge_the_head_boundary(self, tmp_path):
        """只有 v0.45.197 的印记时，表头那条边界不得被判成「写晚了 / 一致」。

        变红的变异：把 `cohort_boundary_evidence` 改回 v0.45.334 之前的写法 —— 固定用
        chain_view 印记、固定比 `_COHORT_HISTORY[-1]`（本条会拿到 boundary_too_late）。
        """
        self._write(tmp_path, self._date_of("v0.45.197"), self._chain_view_body())
        head = rr.cohort_boundary_evidence(tmp_path)
        assert head["version"] == rr._COHORT_HISTORY[-1][1]
        assert head["marker_first_seen"] is None, head
        assert head["verdict"] in ("no_evidence_yet", "no_marker"), head

    def test_197_marker_still_judges_its_own_boundary(self, tmp_path):
        """成对的另一半：修 bug 不能把 197 的判据修没了。

        变红的变异：把 `_BOUNDARY_MARKERS` 里 "v0.45.197" 那条删掉（本条会拿到 no_marker）。
        """
        self._write(tmp_path, self._date_of("v0.45.197"), self._chain_view_body())
        own = rr.cohort_boundary_evidence(tmp_path, version="v0.45.197")
        assert own["verdict"] == "matches", own
        assert own["boundary"] == self._date_of("v0.45.197")

    def test_334_marker_ignores_records_without_the_key(self, tmp_path):
        """缺 `applied` 键的是 v0.45.334 之前的记录（当时施加过）⇒ **不是**印记。

        变红的变异：把 `_marker_gex_modifier_not_applied` 的 `m.get("applied") is False`
        改成 `not m.get("applied")` —— 缺键被当成印记 ⇒ 首见日落到边界前 ⇒ boundary_too_late，
        生产上就是把全部旧归档认成新口径、恒报「写晚了」。
        """
        import datetime as dt
        b = self._date_of("v0.45.334")
        before = (dt.date.fromisoformat(b) - dt.timedelta(days=2)).isoformat()
        self._write(tmp_path, before, self._gex_mod_body(), ticker="ZZZ")                 # 旧记录：无键
        self._write(tmp_path, before, self._gex_mod_body(applied=True), ticker="YYY")     # 非字面量 False
        self._write(tmp_path, b, self._gex_mod_body(applied=False), ticker="AAA")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.334")
        assert ev["marker_first_seen"] == b and ev["verdict"] == "matches", ev

    def test_334_marker_later_than_boundary_is_too_early(self, tmp_path):
        """推送晚于边界日（09-28）扫描的情形：印记首见晚于边界 ⇒ boundary_too_early（危险方向，会混算）。

        这是本条边界日期的**前提**失败时唯一会红的观测点（见 `_COHORT_HISTORY` v0.45.334 条）。
        变红的变异：把 `_marker_gex_modifier_not_applied` 改成恒 False（判别器失明 ⇒ `marker_first_seen`
        为 None；v0.45.334 起 verdict 仍是 too_early —— 边界后有无印记的归档 —— 红在首见日那一半）。
        """
        import datetime as dt
        b = self._date_of("v0.45.334")
        late = (dt.date.fromisoformat(b) + dt.timedelta(days=1)).isoformat()
        self._write(tmp_path, b, self._gex_mod_body(), ticker="AAA")                # 边界当天仍是旧链
        self._write(tmp_path, late, self._gex_mod_body(applied=False), ticker="BBB")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.334")
        assert ev["verdict"] == "boundary_too_early" and ev["marker_first_seen"] == late, ev

    def test_unregistered_boundary_reports_no_marker(self, tmp_path, monkeypatch):
        """没登记印记的边界 ⇒ `no_marker`，不借别人的印记、也不说「还没证据」。

        变红的变异：取不到 `_BOUNDARY_MARKERS[version]` 时退回 chain_view 印记
        （本条会拿到 boundary_too_late），或退回 `no_evidence_yet`（读者会以为有判据、只是还没到）。
        """
        monkeypatch.setattr(rr, "_COHORT_HISTORY",
                            list(rr._COHORT_HISTORY) + [("2099-01-01", "vNOMARK", "测试用")])
        self._write(tmp_path, self._date_of("v0.45.197"), self._chain_view_body())
        self._write(tmp_path, "2099-01-01", self._gex_mod_body(applied=False), ticker="AAA")
        ev = rr.cohort_boundary_evidence(tmp_path)
        assert ev["version"] == "vNOMARK"
        assert ev["verdict"] == "no_marker" and ev["marker_first_seen"] is None, ev

    def test_unknown_version_raises(self, tmp_path):
        """版本号写错是调用方的错 —— 抛，而不是安静地回一个看起来正常的 verdict。

        变红的变异：把 `raise ValueError(...)` 改成按表头边界继续算。
        """
        with pytest.raises(ValueError):
            rr.cohort_boundary_evidence(tmp_path, version="v9.9.9")

    def test_every_marker_names_a_real_boundary(self):
        """印记表的键必须是真实边界 —— 版本号拼错，那条印记就永远用不上，且没人会红。

        变红的变异：把 `_BOUNDARY_MARKERS` 的 "v0.45.334" 写成 "v0.45.34"。
        """
        versions = {v for _d, v, _r in rr._COHORT_HISTORY}
        assert set(rr._BOUNDARY_MARKERS) <= versions, sorted(set(rr._BOUNDARY_MARKERS) - versions)
        assert {"v0.45.197", "v0.45.334"} <= set(rr._BOUNDARY_MARKERS)

    def test_cli_renders_no_marker_for_the_head(self, monkeypatch, db, capsys):
        """CLI 人读模式要能把 `no_marker` 印出来（带版本），不能 KeyError。

        变红的变异：删掉 `_BOUNDARY_VERDICT_TEXT["no_marker"]`（main() 会 KeyError）。
        """
        import sys as _s
        monkeypatch.setattr(rr, "_COHORT_HISTORY",
                            list(rr._COHORT_HISTORY) + [("2099-01-01", "vNOMARK", "测试用")])
        monkeypatch.setattr(_s, "argv", ["ic_rerun_readiness.py", "--db", str(db([])),
                                         "--today", "2099-01-08"])
        rc = rr.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "没有登记可判别的归档印记" in out and "vNOMARK" in out, out


# ════════════════════════════════════════════════════════════════════════════
# 边界证据要到得了自动调用方（v0.45.334）
# ════════════════════════════════════════════════════════════════════════════

_TQ_BOUNDARY = "2099-01-05"
_TQ_TODAY = "2099-01-20"


def _write_gex_archive(root, date, applied="__missing__", ticker="NVDA"):
    """归档形状同生产：`swarm_results.gex_regime_mod`；`applied` 缺省 = 旧记录（没有这个键）。"""
    import json as _json
    mod = {"gex_adjustment": -0.15, "gex_regime": "positive_gex"}
    if applied != "__missing__":
        mod["applied"] = applied
    (root / f"analysis-{ticker}-ml-{date}.json").write_text(
        _json.dumps({"swarm_results": {"gex_regime_mod": mod}}), encoding="utf-8")


@pytest.fixture
def tq_boundary(monkeypatch):
    """表尾追加一条测试边界并登记 v0.45.334 同款印记 —— 不依赖「表头恰好是 v0.45.334」，
    下一条真边界追加后本组测试不跟着失效。"""
    monkeypatch.setattr(rr, "_COHORT_HISTORY",
                        list(rr._COHORT_HISTORY) + [(_TQ_BOUNDARY, "vTQ", "测试用边界")])
    monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vTQ",
                        ("applied is False（测试）", rr._marker_gex_modifier_not_applied))
    return _TQ_BOUNDARY


class TestBoundaryEvidenceReachesAutomatedCallers:
    """`cohort_boundary_evidence()` 是 v0.45.334 那条 09-28 边界「推送晚了」时唯一会红的观测点，
    但此前只有人读模式调用它：`--json` / `--quiet` 在它之前 return，`--out` 的 JSON 里也没有。
    而**所有**自动调用方都走那几条路 —— 编排器 Step 11 是 `--quiet --out`，周度任务是 `--quiet`。
    等于这个观测点在自动流程里从没被执行过。

    契约（不许打破）：`--quiet` 仍是**一行**，前四段顺序不变（周度任务按「第三段 = F&G」解析），
    证据**只在要人看时**（日期与印记不符 / 核不了）追加为第五段、以 🚨 开头，正常时仍是四段
    （`tests/test_dim_ic_forward_test.py` 也钉着四段）；退出码 0/1/3 不因它改变；
    `--json` / `--out` 恒带完整结果，编排器读的 JSON 键照旧都在。
    """

    @staticmethod
    def _run(monkeypatch, argv):
        import sys as _s
        monkeypatch.setattr(_s, "argv", ["ic_rerun_readiness.py", *argv])
        return rr.main()

    def _quiet_segments(self, monkeypatch, capsys, db_path):
        rc = self._run(monkeypatch, ["--db", str(db_path), "--today", _TQ_TODAY, "--quiet"])
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1, f"--quiet 必须只打一行（周度任务原样抄那一行）：{lines}"
        return rc, lines[0].split("｜")

    def test_quiet_flags_marker_later_than_boundary(self, monkeypatch, capsys, db, tq_boundary):
        """推送晚了一天：边界日是旧口径、次日才见印记 ⇒ 第五段以 🚨 开头。

        变红的变异：把 `--quiet` 那行末尾的 `+ "｜" + bev["line"]` 删掉（回到 v0.45.334 之前）。
        """
        p = db([])
        _write_gex_archive(p.parent, _TQ_BOUNDARY, ticker="AAA")                     # 旧链
        _write_gex_archive(p.parent, "2099-01-06", applied=False, ticker="BBB")     # 新链
        rc, seg = self._quiet_segments(monkeypatch, capsys, p)
        assert rc == 1, "退出码契约不因边界证据改变（未就绪仍是 1）"
        assert len(seg) == 5, seg
        assert seg[0] == rr.summary_line(rr.assess(db_path=p, today=_TQ_TODAY)), "第一段必须仍是 IC 摘要"
        assert seg[4].startswith("🚨"), seg[4]
        assert "vTQ" in seg[4] and _TQ_BOUNDARY in seg[4] and "2099-01-06" in seg[4], seg[4]

    def test_quiet_flags_old_archives_after_boundary_before_any_marker(
            self, monkeypatch, capsys, db, tq_boundary):
        """推送还没到：边界日已有归档、却一份印记都没有 ⇒ 也是「写早了」，**现在就红**，
        不等新代码真跑起来（此前这种情形一直报 ⏳ no_evidence_yet「生产还没跑到」）。

        变红的变异：把 `cohort_boundary_evidence` 里 `first is None` 分支改回恒 `no_evidence_yet`。
        """
        p = db([])
        _write_gex_archive(p.parent, "2099-01-01", ticker="AAA")       # 边界前的旧记录：不算
        _write_gex_archive(p.parent, _TQ_BOUNDARY, ticker="BBB")       # 边界当天仍是旧链
        rc, seg = self._quiet_segments(monkeypatch, capsys, p)
        assert rc == 1
        assert seg[4].startswith("🚨") and "1 个归档日无印记" in seg[4], seg[4]
        ev = rr.cohort_boundary_evidence(p.parent)
        assert ev["verdict"] == "boundary_too_early" and ev["marker_first_seen"] is None, ev
        assert ev["unmarked_after_boundary"] == [_TQ_BOUNDARY], ev

    def test_before_boundary_archives_only_is_still_no_evidence(self, monkeypatch, capsys, db, tq_boundary):
        """成对的另一半：边界日之后还没有任何归档 ⇒ 仍是 ⏳ no_evidence_yet，不许误报成 🚨。

        变红的变异：把 `elif date >= boundary:` 改成 `else:`（边界前的旧记录也被数进去）。
        """
        p = db([])
        _write_gex_archive(p.parent, "2099-01-01", ticker="AAA")
        rc, seg = self._quiet_segments(monkeypatch, capsys, p)
        assert rc == 1
        assert len(seg) == 4, f"不该追加 🚨 段：{seg[4:]}"
        ev = rr.boundary_evidence_status(p.parent)
        assert ev["verdict"] == "no_evidence_yet" and ev["alarm"] is False, ev
        assert ev["line"].startswith("⏳") and "vTQ" in ev["line"], ev["line"]

    def test_quiet_healthy_boundary_is_not_flagged(self, monkeypatch, capsys, db, tq_boundary):
        """边界日即见印记 ⇒ matches，不加段（一个恒在的 🚨 段，和没有这一段一样没人看）。

        变红的变异：把 `if bev["alarm"]:` 改成恒真（正常时也追加第五段）。
        """
        p = db([])
        _write_gex_archive(p.parent, _TQ_BOUNDARY, applied=False, ticker="AAA")
        _rc, seg = self._quiet_segments(monkeypatch, capsys, p)
        assert len(seg) == 4, f"正常时不该追加段：{seg[4:]}"
        ev = rr.boundary_evidence_status(p.parent)
        assert ev["verdict"] == "matches" and ev["alarm"] is False, "前提：夹具确实是健康的那一种"
        assert ev["line"].startswith("✅"), ev["line"]

    def test_json_and_out_carry_the_evidence(self, monkeypatch, capsys, db, tq_boundary, tmp_path):
        """`--json` 与 `--out` 都带 `cohort_boundary_evidence`，且编排器读的键照旧都在。

        变红的变异：删掉 `res["cohort_boundary_evidence"] = bev`；或把 `bev = …` 挪到 `--out`
        写盘之后（`--json` 仍绿，`--out` 红 —— 编排器读的正是 `--out`）。
        """
        import json as _json
        p = db([])
        _write_gex_archive(p.parent, _TQ_BOUNDARY, ticker="AAA")
        _write_gex_archive(p.parent, "2099-01-06", applied=False, ticker="BBB")
        out_file = tmp_path / "readiness_out.json"
        rc = self._run(monkeypatch, ["--db", str(p), "--today", _TQ_TODAY, "--json",
                                     "--out", str(out_file)])
        assert rc == 1
        printed = _json.loads(capsys.readouterr().out)
        written = _json.loads(out_file.read_text(encoding="utf-8"))
        for payload in (printed, written):
            ev = payload.get("cohort_boundary_evidence")
            assert ev is not None, sorted(payload)
            assert ev["verdict"] == "boundary_too_early" and ev["alarm"] is True, ev
            assert ev["version"] == "vTQ" and ev["marker_first_seen"] == "2099-01-06", ev
            # 编排器 Step 11 的 READINESS_LINE 读这几个键（~/.claude/scripts/alpha-hive-orchestrator.sh）
            for key in ("cohort", "weeks_accrued", "weeks_required", "n_ripe_samples",
                        "eta_date", "pool_note"):
                assert key in payload, key

    def test_evidence_exception_is_rendered_not_raised(self, monkeypatch, capsys, db, tq_boundary):
        """判别器抛异常 ⇒ `cannot_judge`（同样 🚨），退出码照旧。

        抛出去的代价：Python 未捕获异常退出码是 **1**，编排器把 1 读成「未就绪（正常，继续攒）」
        —— 失败被改写成了正常状态，且没有任何一行说它坏了。
        变红的变异：删掉 `boundary_evidence_status` 里的 try/except。
        """
        def boom(*_a, **_k):
            raise RuntimeError("归档读炸了（测试注入）")
        monkeypatch.setattr(rr, "cohort_boundary_evidence", boom)
        rc, seg = self._quiet_segments(monkeypatch, capsys, db([]))
        assert rc == 1
        assert seg[4].startswith("🚨") and "RuntimeError" in seg[4], seg[4]
        st = rr.boundary_evidence_status(db([]).parent)
        assert st["verdict"] == "cannot_judge" and st["alarm"] is True, st

    def test_every_verdict_renders_a_line(self):
        """`_boundary_line` 对每个判定都要出得了一行（缺文案 ⇒ KeyError ⇒ 整个 main 崩）。

        变红的变异：删掉 `_BOUNDARY_VERDICT_TEXT["cannot_judge"]`。
        """
        base = {"version": "vX", "boundary": "2099-01-01", "marker_first_seen": None,
                "unmarked_after_boundary": []}
        for verdict in ("matches", "boundary_too_early", "boundary_too_late", "no_evidence_yet",
                        "no_marker", "cannot_judge"):
            line = rr._boundary_line({**base, "verdict": verdict})
            assert ("vX" in line) and (line.startswith("🚨") == (verdict in rr.BOUNDARY_ALARM_VERDICTS)), line


class TestCorrectionEntriesKeepTheBoundaryCheckable:
    """v0.45.334 那条边界自带预案：推送晚了就**追加**一条更正、把边界顺延。预案一执行，
    判别器必须跟着核新日期 —— 否则它要么永远拿旧日期报 🚨（「狼来了」之后真报也没人看），
    要么回 `no_marker`（这条边界从此核不了）。两种都是在预案**被正确执行之后**才坏。

    修前：缺省版本取表尾，日期却用 `next(...)` 取**第一条**同名条目；更正若新开标签则查不到印记。
    """

    @pytest.fixture
    def with_boundary(self, monkeypatch):
        def _apply(extra):
            monkeypatch.setattr(rr, "_COHORT_HISTORY", list(rr._COHORT_HISTORY) + extra)
            monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vTQ",
                                ("applied is False（测试）", rr._marker_gex_modifier_not_applied))
        return _apply

    @staticmethod
    def _archives(root):
        _write_gex_archive(root, "2099-01-05", ticker="AAA")                  # 原边界日：旧链
        _write_gex_archive(root, "2099-01-06", applied=False, ticker="BBB")   # 顺延后的边界日：新链

    def test_same_label_correction_uses_the_last_entry(self, tmp_path, with_boundary):
        """更正沿用同一标签：缺省与显式都核**最后一条**，且与 `assess()` 的边界一致。

        变红的变异：把显式分支改回 `next(... for ... in _COHORT_HISTORY ...)`（第一条），
        或把缺省改回「先取表尾的 version、再按 version 回查日期」。
        """
        with_boundary([("2099-01-05", "vTQ", "测试用边界"), ("2099-01-06", "vTQ", "更正：顺延一天")])
        self._archives(tmp_path)
        for ev in (rr.cohort_boundary_evidence(tmp_path),
                   rr.cohort_boundary_evidence(tmp_path, version="vTQ")):
            assert ev["boundary"] == "2099-01-06" == rr.cohort_start()["date"], ev
            assert ev["verdict"] == "matches", ev

    def test_new_label_correction_inherits_the_marker(self, tmp_path, with_boundary, monkeypatch):
        """更正新开标签（表头约定的写法）+ 登记 `_CORRECTS` ⇒ 沿用被更正条目的印记。

        变红的变异：删掉 `cohort_boundary_evidence` 里 `or _BOUNDARY_MARKERS.get(_CORRECTS.get(version))`
        （本条拿到 no_marker）。
        """
        with_boundary([("2099-01-05", "vTQ", "测试用边界"), ("2099-01-06", "vTQ_fix", "更正：顺延一天")])
        monkeypatch.setitem(rr._CORRECTS, "vTQ_fix", "vTQ")
        self._archives(tmp_path)
        ev = rr.cohort_boundary_evidence(tmp_path)
        assert ev["version"] == "vTQ_fix" and ev["boundary"] == "2099-01-06", ev
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2099-01-06", ev
        # 显式核被更正的那条：如实报它当初写早了 —— 那正是它被更正的原因
        assert rr.cohort_boundary_evidence(tmp_path, version="vTQ")["verdict"] == "boundary_too_early"

    def test_unregistered_new_label_correction_is_no_marker(self, tmp_path, with_boundary):
        """对照：新开标签却没登记 `_CORRECTS` ⇒ `no_marker`（表头约定 ② 为什么不能省）。"""
        with_boundary([("2099-01-05", "vTQ", "测试用边界"), ("2099-01-06", "vTQ_fix", "更正：顺延一天")])
        self._archives(tmp_path)
        assert rr.cohort_boundary_evidence(tmp_path)["verdict"] == "no_marker"

    @staticmethod
    def _correction_problems(history, corrects):
        """表头约定的静态核对：`_CORRECTS` 两端都是真实版本、更正在被更正者之后、reason 以「更正」开头；
        反过来，reason 以「更正」开头的条目必须登记在 `_CORRECTS`（漏登 ⇒ 判别器回 no_marker）。"""
        order = {}
        for i, (_d, v, _r) in enumerate(history):
            order.setdefault(v, i)
        problems = []
        for fix, target in corrects.items():
            if fix not in order or target not in order:
                problems.append(f"{fix}→{target}：不在 _COHORT_HISTORY 里")
            elif order[fix] <= order[target]:
                problems.append(f"{fix} 排在它更正的 {target} 之前")
        for _d, v, r in history:
            if v in corrects and not r.startswith("更正"):
                problems.append(f"{v}：登记为更正，reason 却不以「更正」开头")
            if r.startswith("更正") and v not in corrects:
                problems.append(f"{v}：reason 以「更正」开头却没登记 _CORRECTS")
        return problems

    def test_real_correction_table_is_consistent(self):
        assert self._correction_problems(rr._COHORT_HISTORY, rr._CORRECTS) == []

    def test_correction_checker_has_teeth(self):
        """反向自证：上一条对真表恒空时不能是因为核对器是瞎的。"""
        h = [("2099-01-05", "vA", "改了什么"), ("2099-01-06", "vB", "更正：顺延一天")]
        assert self._correction_problems(h, {}) != []                   # 漏登
        assert self._correction_problems(h, {"vB": "vA"}) == []         # 正确登记
        assert self._correction_problems(h, {"vA": "vB"}) != []         # 方向反了
        assert self._correction_problems(h, {"vB": "vZ"}) != []         # 指向不存在的版本
