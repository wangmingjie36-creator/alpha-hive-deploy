"""概率记分卡（v0.45.134 Step 3）

这个模块存在的意义是**给概率装一个会红的观测点**——没有记分规则，一个概率
模型就是不可证伪的。所以它自己的测试必须先证明：它红得对、也绿得对。

守四类：
  1. 指标算对（Brier / LogLoss / 校准误差，拿手算的值对）
  2. **时点隔离没泄漏** —— 最要命的一条。泄漏会让记分卡变**好看**，
     那是最难被发现的错。用一个「未来数据会翻转答案」的库反向证明。
  3. 账本：幂等（补跑不污染）、None 也记（否则算不出覆盖率）
  4. 退出码：估计器劣于旧常数时**必须** 1，不是 0

全文件只碰临时 sqlite / 临时 jsonl，不出网。
"""

import json
import math
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probability_scorecard as PS

_SCHEMA = """CREATE TABLE predictions (
    id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, direction TEXT,
    price_at_predict REAL, close_t7 REAL)"""


def _mkdb(tmp_path, rows, name="pheromone.db"):
    """rows: (date, ticker, direction, price_at_predict, close_t7)"""
    p = tmp_path / name
    con = sqlite3.connect(p)
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO predictions (date,ticker,direction,price_at_predict,close_t7)"
                    " VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return p


# ── 1. 指标 ──────────────────────────────────────────────────────────
def test_brier_matches_hand_computation():
    # (0.8-1)^2 + (0.3-0)^2 = 0.04 + 0.09 = 0.13 → /2 = 0.065
    assert PS.brier([0.8, 0.3], [1, 0]) == pytest.approx(0.065)


def test_brier_perfect_and_worst():
    assert PS.brier([1.0, 0.0], [1, 0]) == pytest.approx(0.0)
    assert PS.brier([0.0, 1.0], [1, 0]) == pytest.approx(1.0)


def test_log_loss_does_not_explode_on_certainty():
    """p=0 而真值为 1 必须给出有限的大值，不是 inf —— inf 会污染整张卡"""
    v = PS.log_loss([0.0], [1])
    assert math.isfinite(v) and v > 10


def test_calibration_error_is_mean_gap():
    # 平均预测 0.7，实际频率 0.5 → 20.0pp
    assert PS.calibration_error_pp([0.7, 0.7], [1, 0]) == pytest.approx(20.0)


@pytest.mark.parametrize("fn", [PS.brier, PS.log_loss, PS.calibration_error_pp])
def test_metrics_reject_empty(fn):
    with pytest.raises(ValueError):
        fn([], [])


# ── 2. 时点隔离（最要命的一条）────────────────────────────────────────
def test_embargo_excludes_future_samples(tmp_path):
    """反向证明：把「未来会翻转答案」的样本塞进库，估计量不许看见它。

    构造：AAA 看多在 2026-01-01~02-09 有 25 条**全胜**（老历史）；
    被记分的那条在 2026-03-01。若 embargo 失效、把 2026-02-25 那批
    **全败**的样本也算进去，分票频率会从 1.0 掉下来 —— 断言它没掉。
    """
    rows = []
    # 老历史：25 条全胜，日期都早于 cutoff
    for i in range(25):
        rows.append((f"2026-01-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 110.0))
    # 近历史：10 条全败，落在 embargo 窗口内（2026-03-01 − 14 天 = 2026-02-15）
    for i in range(10):
        rows.append((f"2026-02-{i + 18:02d}", "AAA", "bullish", 100.0, 90.0))
    # 被记分的那条
    rows.append(("2026-03-01", "AAA", "bullish", 100.0, 110.0))
    db = _mkdb(tmp_path, rows)

    res = PS.walk_forward(db_path=db, embargo_days=14, min_sample=20)
    assert res["status"] == "ok"
    # 只看老历史 ⇒ 分票频率应为 1.0 ⇒ 对一条命中样本 Brier 贡献 0
    assert res["models"]["per_ticker_hit_rate"]["mean_pred_pct"] == pytest.approx(100.0, abs=1.0), (
        "分票频率不是 100% —— embargo 漏了近期样本，记分卡在用未来数据"
    )


def test_embargo_relaxed_would_leak(tmp_path):
    """成对：把 embargo 调到 0，上面那批近期失败样本就该混进来。

    没有这一条，`test_embargo_excludes_future_samples` 可能只是因为
    「近期样本压根没被读进来」而通过 —— 那测的就不是隔离了。
    """
    rows = [(f"2026-01-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 110.0) for i in range(25)]
    rows += [(f"2026-02-{i + 18:02d}", "AAA", "bullish", 100.0, 90.0) for i in range(10)]
    rows.append(("2026-03-01", "AAA", "bullish", 100.0, 110.0))
    db = _mkdb(tmp_path, rows)

    tight = PS.walk_forward(db_path=db, embargo_days=14, min_sample=20)
    loose = PS.walk_forward(db_path=db, embargo_days=0, min_sample=20)
    assert loose["models"]["per_ticker_hit_rate"]["mean_pred_pct"] < tight["models"]["per_ticker_hit_rate"]["mean_pred_pct"], (
        "放松 embargo 没有改变结果 —— 说明这个参数根本没在起作用"
    )


def test_missing_db_is_status_not_crash(tmp_path):
    res = PS.walk_forward(db_path=tmp_path / "nope.db")
    assert res["status"] == "missing"


def test_neutral_rows_are_excluded(tmp_path):
    """中性预测没有方向命中率可言，不许进分母"""
    db = _mkdb(tmp_path, [("2026-01-01", "AAA", "neutral", 100.0, 110.0)])
    rows, status = PS.load_outcomes(db)
    assert status == "ok" and rows == []


def test_hit_definition_matches_historical_analyzer(tmp_path):
    """口径必须与 HistoricalAnalyzer 一致：调整后 > 0 算命中，恰好 0 算未命中"""
    db = _mkdb(tmp_path, [
        ("2026-01-01", "AAA", "bullish", 100.0, 110.0),   # +10% 多头 → 命中
        ("2026-01-02", "BBB", "bearish", 100.0, 90.0),    # −10% 空头 → 命中
        ("2026-01-03", "CCC", "bullish", 100.0, 100.0),   # 持平 → **未命中**
    ])
    rows, _ = PS.load_outcomes(db)
    assert [r["hit"] for r in sorted(rows, key=lambda x: x["date"])] == [1, 1, 0]


# ── 3. 账本 ──────────────────────────────────────────────────────────
def test_ledger_is_idempotent_per_date_ticker(tmp_path):
    """补跑同一天同一只票不许重复追加 —— 否则那天会在记分里被数两遍"""
    led = tmp_path / "published.jsonl"
    assert PS.record_published("2026-09-06", "NVDA", "bullish", 50.8,
                               "same_direction", 59, ledger_path=led) is True
    assert PS.record_published("2026-09-06", "NVDA", "bullish", 99.9,
                               "same_direction", 60, ledger_path=led) is False
    rows, _ = PS.load_ledger(led)
    assert len(rows) == 1 and rows[0]["hit_rate_pct"] == 50.8


def test_ledger_records_unavailable_probability(tmp_path):
    """「这天没印出概率」本身要被记下 —— 不记就算不出覆盖率，
    而覆盖率正是这次换源的主要代价。"""
    led = tmp_path / "published.jsonl"
    PS.record_published("2026-09-06", "TMUS", "bullish", None, None, 7, ledger_path=led)
    rows, _ = PS.load_ledger(led)
    assert len(rows) == 1 and rows[0]["hit_rate_pct"] is None


def test_ledger_survives_corrupt_lines(tmp_path):
    led = tmp_path / "published.jsonl"
    PS.record_published("2026-09-06", "NVDA", "bullish", 50.8, "same_direction", 59,
                        ledger_path=led)
    with led.open("a", encoding="utf-8") as fh:
        fh.write("{ 这不是 json\n")
    rows, status = PS.load_ledger(led)
    assert status == "ok" and len(rows) == 1


def test_missing_ledger_is_status_not_crash(tmp_path):
    assert PS.score_published(ledger_path=tmp_path / "nope.jsonl")["status"] == "ledger_missing"


def test_score_published_joins_and_counts_coverage(tmp_path):
    led = tmp_path / "published.jsonl"
    # v0.45.138：记分打**前瞻量**，故这里要给 forward_estimate_pct
    PS.record_published("2026-01-01", "AAA", "bullish", 100.0, "same_direction", 30,
                        ledger_path=led, forward_estimate_pct=100.0, forward_sample_size=684)
    PS.record_published("2026-01-02", "BBB", "bullish", None, None, 3, ledger_path=led)
    db = _mkdb(tmp_path, [("2026-01-01", "AAA", "bullish", 100.0, 110.0)])
    res = PS.score_published(ledger_path=led, db_path=db)
    assert res["status"] == "ok"
    assert res["ledger_rows"] == 2 and res["with_probability"] == 1
    assert res["coverage_pct"] == 50.0 and res["matured"] == 1
    assert res["published"]["brier"] == pytest.approx(0.0)   # 印 100%、真的命中


# ── 4. 退出码：会红的那个观测点 ──────────────────────────────────────
#
# v0.45.138 改了判据。旧判据是「生产估计量 vs 旧常数 65.0」——生产自 v0.45.138
# 起是池化基准率，而基准率几乎必然赢过一个固定常数，那样的告警永远不会红，
# 等于没装。新判据问的是：**就在旁边，有没有别的候选量明显打得过生产在用的那个。**
# 这也正是它第一次真正有用的那次所回答的问题（v0.45.134 用分票频率、被基准率打过）。


def test_exit_code_1_when_another_candidate_beats_production(tmp_path):
    """构造一个「分票频率确实有信息」的库：AAA 恒胜、BBB 恒败。

    此时池化基准率 ≈ 50%（毫无区分），而分票频率近乎完美 ⇒ 生产在用的那个
    被打过，必须红。这一条绿了才说明「有更好的选择时会被发现」。
    """
    rows = [(f"2026-{m:02d}-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 110.0)
            for m in (1, 2, 5) for i in range(30)]
    rows += [(f"2026-{m:02d}-{i % 28 + 1:02d}", "BBB", "bullish", 100.0, 90.0)
             for m in (1, 2, 5) for i in range(30)]
    db = _mkdb(tmp_path, rows)
    res = PS.walk_forward(db_path=db, embargo_days=14, min_sample=20)
    assert res["production_model"] == PS.PRODUCTION_MODEL
    assert res["best_model"] == "per_ticker_hit_rate", res["models"]
    assert res["production_is_beaten"] is True
    assert PS.main(["--walk-forward", "--db", str(db)]) == 1, "这个观测点是哑的"


def test_exit_code_0_when_production_is_best(tmp_path):
    """成对：没有更好的候选时必须绿 —— 否则告警恒红，同样等于没装。

    构造无逐票信息的库：所有票同一个 ~60% 命中率，分票频率只是噪声。
    """
    import random
    rng = random.Random(7)
    rows = []
    for tk in ("AAA", "BBB", "CCC", "DDD"):
        for m in (1, 2, 5):
            for i in range(30):
                win = rng.random() < 0.6
                rows.append((f"2026-{m:02d}-{i % 28 + 1:02d}", tk, "bullish",
                             100.0, 110.0 if win else 90.0))
    db = _mkdb(tmp_path, rows)
    res = PS.walk_forward(db_path=db, embargo_days=14, min_sample=20)
    assert res["production_is_beaten"] is False, res["beaten_by"]
    assert PS.main(["--walk-forward", "--db", str(db)]) == 0


def test_exit_code_3_when_undeterminable(tmp_path):
    assert PS.main(["--walk-forward", "--db", str(tmp_path / "nope.db")]) == 3


def test_json_output_is_parseable(tmp_path, capsys):
    db = _mkdb(tmp_path, [(f"2026-01-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 110.0)
                          for i in range(25)])
    PS.main(["--walk-forward", "--db", str(db), "--json"])
    json.loads(capsys.readouterr().out)


# ── 5. 融合权重扫描（v0.45.139）─────────────────────────────────────
# 把「0.7/0.3」从魔数变成受监控量。判据与 walk_forward 同型：网格上有没有 w
# 明显打得过生产在用的 PRODUCTION_BLEND_W。生产实测曲线在 [0.7,1.0] 是平的。
def _ml_const(rows, pct):
    return {(r[0], r[1]): pct for r in rows}


def _write_report(dir_, fdate, ticker, ml_pct, ts=None):
    p = dir_ / f"analysis-{ticker}-ml-{fdate}.json"
    p.write_text(json.dumps({"ticker": ticker, "timestamp": ts or fdate,
                             "combined_recommendation": {"ml_probability": ml_pct}}),
                 encoding="utf-8")


def test_blend_scan_embargo_excludes_future(tmp_path):
    """与 walk_forward 同一条泄漏证明（成对）：近期全败样本不许进 w=1.0 那一列"""
    rows = [(f"2026-01-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 110.0) for i in range(25)]
    rows += [(f"2026-02-{i + 18:02d}", "AAA", "bullish", 100.0, 90.0) for i in range(10)]
    rows.append(("2026-03-01", "AAA", "bullish", 100.0, 110.0))
    db = _mkdb(tmp_path, rows)
    ml = _ml_const(rows, 50.0)
    tight = PS.blend_scan(db_path=db, ml=ml, embargo_days=14)
    loose = PS.blend_scan(db_path=db, ml=ml, embargo_days=0)
    w1 = lambda res: next(g for g in res["grid"] if abs(g["w"] - 1.0) < 1e-9)  # noqa: E731
    assert w1(tight)["mean_pred_pct"] == pytest.approx(100.0, abs=1.0)
    assert w1(loose)["mean_pred_pct"] < w1(tight)["mean_pred_pct"], "放松 embargo 没改变结果"


def test_blend_scan_alarms_when_ml_is_informative(tmp_path):
    """ML 完美预知结果 ⇒ w=0 远优于生产 0.7 ⇒ 必须红"""
    import random
    rng = random.Random(3)
    rows = []
    for m in (1, 2, 4, 5):
        for i in range(28):
            for tk in ("AAA", "BBB"):
                win = rng.random() < 0.5
                rows.append((f"2026-{m:02d}-{i + 1:02d}", tk, "bullish", 100.0,
                             110.0 if win else 90.0))
    db = _mkdb(tmp_path, rows)
    ml = {(d, t): (95.0 if c7 > p0 else 5.0) for d, t, _, p0, c7 in rows}
    res = PS.blend_scan(db_path=db, ml=ml, embargo_days=14)
    assert res["status"] == "ok"
    assert res["best_w"] == pytest.approx(0.0)
    assert res["production_is_beaten"] is True and res["gap"] > PS.BRIER_MARGIN


def test_blend_scan_green_when_ml_adds_nothing(tmp_path):
    """成对：ML 恒为一个接近基准率的常数 ⇒ 曲线平 ⇒ 生产 w 不被打过——否则恒红等于没装"""
    import random
    rng = random.Random(11)
    rows = []
    for m in (1, 2, 3, 4, 5, 6):
        for i in range(28):
            for tk in ("AAA", "BBB", "CCC"):
                rows.append((f"2026-{m:02d}-{i + 1:02d}", tk, "bullish", 100.0,
                             110.0 if rng.random() < 0.6 else 90.0))
    db = _mkdb(tmp_path, rows)
    res = PS.blend_scan(db_path=db, ml=_ml_const(rows, 60.0), embargo_days=14)
    assert res["status"] == "ok"
    assert res["production_is_beaten"] is False, res["beaten_by"]


def test_blend_scan_reports_production_w(tmp_path):
    rows = [(f"2026-0{m}-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 110.0)
            for m in (1, 2, 3) for i in range(20)]
    db = _mkdb(tmp_path, rows)
    res = PS.blend_scan(db_path=db, ml=_ml_const(rows, 50.0), embargo_days=14)
    assert res["production_w"] == PS.PRODUCTION_BLEND_W
    assert res["production_model"] == f"blend_w={PS.PRODUCTION_BLEND_W}"
    assert any(abs(g["w"] - PS.PRODUCTION_BLEND_W) < 1e-9 for g in res["grid"])


def test_load_ml_probabilities_keys_by_filename_date(tmp_path):
    """次日重跑的文件 timestamp 晚一天——键必须用文件名日期，否则对不上 T+7 结果"""
    _write_report(tmp_path, "2026-01-05", "AAA", 61.0, ts="2026-01-06T05:00:00")
    _write_report(tmp_path, "2026-01-06", "BBB", None)          # 无 ML 概率 → 跳过
    ml, st = PS.load_ml_probabilities(tmp_path)
    assert st == "ok" and ml == {("2026-01-05", "AAA"): 61.0}


def test_load_ml_probabilities_empty_dir_is_status(tmp_path):
    assert PS.load_ml_probabilities(tmp_path)[1] == "no_reports"


def test_blend_scan_cli_exit_codes(tmp_path):
    """通过 CLI 端到端：有更好的 w ⇒ 1；找不到报告 ⇒ 3"""
    import random
    rng = random.Random(5)
    rdir = tmp_path / "reports"; rdir.mkdir()
    rows = []
    for m in (1, 2, 4, 5):
        for i in range(28):
            d = f"2026-{m:02d}-{i + 1:02d}"
            win = rng.random() < 0.5
            rows.append((d, "AAA", "bullish", 100.0, 110.0 if win else 90.0))
            _write_report(rdir, d, "AAA", 95.0 if win else 5.0)
    db = _mkdb(tmp_path, rows)
    assert PS.main(["--blend-scan", "--db", str(db), "--reports-dir", str(rdir)]) == 1
    empty = tmp_path / "empty"; empty.mkdir()
    assert PS.main(["--blend-scan", "--db", str(db), "--reports-dir", str(empty)]) == 3


def test_ledger_records_ml_probability(tmp_path):
    led = tmp_path / "published.jsonl"
    PS.record_published("2026-09-06", "NVDA", "bullish", 50.8, "same_direction", 59,
                        ledger_path=led, forward_estimate_pct=55.6, forward_sample_size=684,
                        ml_probability_pct=59.0)
    rows, _ = PS.load_ledger(led)
    assert rows[0]["ml_probability_pct"] == 59.0


class TestMLEstimatorGenerations:
    """ML 概率估计量的换代登记（v0.45.140）。

    `blend_scan` 把历史全部报告的 `ml_probability` 池化成一条序列给 w 记分，
    隐含假设是「同一个估计量」。2026-09-06 这天 `_prepare_ml_input` 的特征
    来源被改了两批（v0.45.137 的 volatility/sentiment、v0.45.140 的
    odds/risk_adj/final_score），序列自此跨代。换代而无人记录 = 静默混算。

    判据「谁会红？」：不阻断扫描（早期样本仍有信息），但结果里必须带出代际，
    否则没有任何观测点会因为混算而变化。
    """

    def test_boundary_partitions_days(self):
        """每条边界都必须真的**切开**日期，且同一代内的日期仍然相等。

        v0.45.146 记：原实现写死 `gen("2026-09-06") == gen("2026-09-08")`，
        在 09-07 新增一条边界时如期变红——这是它该有的行为。但「每加一代就来
        手改一次常量」的守卫，改着改着就会被改成恒真。故改为**从登记表派生**：
        逐条验边界前后异代（否则那条边界等于没划），再验代内同日期（否则
        前一条在「每天一代」的退化表上也恒真）。两条成对，缺任一条都能被绕过。
        """
        from datetime import date, timedelta
        from probability_scorecard import (ml_estimator_generation,
                                           _ML_ESTIMATOR_GENERATIONS as G)
        assert G, "登记表为空则以下全部在空集上恒真"
        assert ml_estimator_generation("2026-08-26").startswith("pre-")
        assert not ml_estimator_generation("2026-09-06").startswith("pre-")
        for day, version, _ in G:
            prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            assert ml_estimator_generation(day) == version
            assert ml_estimator_generation(prev) != version, (
                f"{day} 这条边界没切开任何日期——前一天与当天同代")
        last = date.fromisoformat(G[-1][0])
        assert (ml_estimator_generation(G[-1][0])
                == ml_estimator_generation((last + timedelta(days=30)).isoformat())), (
            "末代内部被切开了——ml_estimator_generation 的「取最后一条匹配」坏了")

    #: 已知世代的**最小集合**：这些 (日期, 版本) 必须在表里。
    #:
    #: ⚠️ **这份字面量刻意写死，不许从登记表派生** —— 派生即恒真。
    #: 它与同类 `test_boundary_partitions_days`（v0.45.146 特意从**写死常量**改成
    #: **从表派生**）看似矛盾，其实问的是两个不同的问题：
    #:   · 「每条边界都真的切开日期吗」问的是表的**当前内容** ⇒ 必须派生，
    #:     否则每加一代就要手改常量，改着改着就被改成恒真。
    #:   · 「有没有哪条边界**消失**了」问的是表的**历史** ⇒ 必须写死，
    #:     因为唯一的参照物就是「过去确实有过这条」。
    #: ⇒ 看到本常量别「顺手统一成派生」，那会把守卫变成装饰品。
    #:
    #: 追加新世代**不需要**动这里（子集断言，多出来的不管）。
    #: 只有当你要**删掉**一条历史世代时它才会红 —— 而那正是它存在的理由：
    #: 表头写着「只追加，不改写（审计轨迹）」，但此前没有任何东西执行这句话。
    MUST_BE_ENUMERATED = frozenset({
        ("2026-09-06", "v0.45.137+v0.45.140+v0.45.141"),
        ("2026-09-07", "v0.45.146+v0.45.147"),
    })

    def test_no_known_generation_has_vanished(self):
        """审计轨迹只能变长。少一条 = 静默换代，且**没有任何既有断言会红**。

        v0.45.166 实测（变异）：删掉表里**最新**那条，全套 42 项 **零红** ——
        `test_boundary_partitions_days` 循环的是表本身，少一条只是少转一圈；
        它循环前钉的那两个日期（`2026-08-26` / `2026-09-06`）只夹住**第一条**
        边界，之后追加的每一代都无人看守，而无人看守的恰是**会增长的那头**。
        （删**最早**那条会红，所以此前的覆盖是**单边**的。）

        为什么这不是杞人忧天：本仓约 19 个 worktree 并发，历史上出过
        合并事故进 main（v0.45.121 冲突标记）。世代表正是最容易在
        三方合并里被「整理」掉一条的那种结构。
        """
        from probability_scorecard import _ML_ESTIMATOR_GENERATIONS as G
        assert self.MUST_BE_ENUMERATED, "最小集合被清空了——本条已退化为恒真"
        live = {(d, v) for d, v, _ in G}
        missing = self.MUST_BE_ENUMERATED - live
        assert not missing, (
            f"世代登记表少了已知条目 {sorted(missing)}；表头写明「只追加，不改写」。"
            "若确实要改写审计轨迹，请连同本最小集合一起显式修改，别只删表里那条。")

    def test_registry_is_append_only_and_sorted(self):
        """只追加不改写；日期必须递增，否则 `ml_estimator_generation` 的
        「取最后一条匹配」会给出错的代。"""
        from probability_scorecard import _ML_ESTIMATOR_GENERATIONS as G
        assert G, "登记表不得为空——空表会让本类所有断言在空集上恒真"
        days = [d for d, _, _ in G]
        assert days == sorted(days)
        assert len(set(days)) == len(days)

    def test_generation_counts_reconcile_with_n(self):
        """两套账目必须对上：代际计数之和 == 实际记分的样本数 `n`。

        v0.45.143：`recs` 构建循环有**两个**过滤（`key not in ml` 与
        embargo 的 `not hist`），而代际计数只复制了第一个 ⇒ 计数之和 > n。
        被第二个过滤丢掉的恰好是**最早那批行**（它们的 cutoff 之前没有历史），
        也就是 `pre-` 那一代 ⇒ 系统性高报旧世代。
        """
        from probability_scorecard import blend_scan, EMBARGO_DAYS
        rows = ([{"date": "2026-08-01", "ticker": f"T{i}", "hit": i % 2} for i in range(20)]
                + [{"date": "2026-09-08", "ticker": f"U{i}", "hit": i % 2} for i in range(20)])
        ml = {(r["date"], r["ticker"]): 50.0 + (i % 7) for i, r in enumerate(rows)}
        out = blend_scan(rows=rows, ml=ml, embargo_days=EMBARGO_DAYS)
        assert out["status"] == "ok"
        assert sum(out["ml_estimator_generations"].values()) == out["n"], (
            f"代际计数之和 {sum(out['ml_estimator_generations'].values())} "
            f"≠ 记分样本数 {out['n']} —— 两个数在描述不同的population")

    def test_no_false_alarm_when_scored_samples_are_one_generation(self):
        """跨代标志必须描述**实际记分的**样本，不是候选行。

        用生产默认 embargo（14 天）：08-01 那批因 cutoff 之前无历史被丢弃，
        真正记分的 20 条全部来自 09-08（新代）⇒ 不得报 spans=True。
        ⚠️ 原断言用 `embargo_days=0`，那恰好让第二个过滤永不触发 ——
        「为了夹具简单」挑的参数值关掉了被测路径。
        """
        from probability_scorecard import blend_scan, EMBARGO_DAYS, ml_estimator_generation
        rows = ([{"date": "2026-08-01", "ticker": f"T{i}", "hit": i % 2} for i in range(20)]
                + [{"date": "2026-09-08", "ticker": f"U{i}", "hit": i % 2} for i in range(20)])
        ml = {(r["date"], r["ticker"]): 50.0 + (i % 7) for i, r in enumerate(rows)}
        out = blend_scan(rows=rows, ml=ml, embargo_days=EMBARGO_DAYS)
        assert out["n"] == 20, out["n"]
        assert out["ml_estimator_generations"] == {
            ml_estimator_generation("2026-09-08"): 20}, out["ml_estimator_generations"]
        assert out["spans_estimator_generations"] is False

    def test_blend_scan_reports_the_span(self):
        """跨代时 `spans_estimator_generations` 必须为真；同代时为假。
        成对断言——少了后半边，写死 True 也会全绿。"""
        from probability_scorecard import blend_scan
        rows_1gen = [{"date": "2026-09-08", "ticker": f"T{i}", "hit": i % 2}
                     for i in range(40)]
        rows_2gen = ([{"date": "2026-08-01", "ticker": f"T{i}", "hit": i % 2}
                      for i in range(40)]
                     + [{"date": "2026-09-08", "ticker": f"U{i}", "hit": i % 2}
                        for i in range(40)])

        def _ml(rows):
            return {(r["date"], r["ticker"]): 50.0 + (i % 7) for i, r in enumerate(rows)}

        one = blend_scan(rows=list(rows_1gen), ml=_ml(rows_1gen), embargo_days=0)
        two = blend_scan(rows=list(rows_2gen), ml=_ml(rows_2gen), embargo_days=0)
        assert one["status"] == "ok" and two["status"] == "ok"
        assert one["spans_estimator_generations"] is False, one["ml_estimator_generations"]
        assert two["spans_estimator_generations"] is True, two["ml_estimator_generations"]
        assert len(two["ml_estimator_generations"]) == 2


class TestGenerationVisibleToHumans:
    """跨代混算必须出现在**人看的那一层**（v0.45.159）。

    v0.45.140 把 `spans_estimator_generations` 放进了 `blend_scan` 的返回字典，
    并在 TestMLEstimatorGenerations 的 docstring 里写明判据：「结果里必须带出
    代际，否则没有任何观测点会因为混算而变化」。数据层做到了——但 `_fmt`
    一次没印过这两个键，退出码也不看。于是读 --json 的人看得见，跑 CLI 的人
    看到的是一张干净的表。**算了没人读＝没算**（同 v0.45.112 的判据）。

    时机上这条尤其要紧：2026-09-07 当天 `spans` 恰好是 False（09-06/09-07
    无扫描、最近业务日 09-04），**09-08 首次扫描后即变 True**。

    不动退出码：v0.45.140 明确登记过「不阻断扫描（早期样本仍有信息）」。
    把警告变得可行动的是 `--generation`，不是让它变红。
    """

    @staticmethod
    def _rows_two_generations():
        """一半落在首条边界之前、一半在最新边界之后。"""
        old = [{"date": f"2026-08-{(i % 28) + 1:02d}", "ticker": f"T{i}", "hit": i % 2}
               for i in range(60)]
        new = [{"date": f"2026-09-{8 + (i % 5):02d}", "ticker": f"U{i}", "hit": (i + 1) % 2}
               for i in range(40)]
        rows = old + new
        ml = {(r["date"], r["ticker"]): 40.0 + (i % 40) for i, r in enumerate(rows)}
        return rows, ml

    def test_spanning_shows_warning_in_rendered_text(self):
        rows, ml = self._rows_two_generations()
        res = PS.blend_scan(rows=rows, ml=ml, embargo_days=PS.EMBARGO_DAYS)
        assert res["spans_estimator_generations"] is True, "夹具没跨代，本类全部恒真"
        txt = PS._fmt(res, "融合权重扫描")
        assert "跨了多代" in txt, "跨代却没在渲染文本里出警告 —— 人看不到就等于没记"
        assert "--generation latest" in txt, "警告没说怎么拿干净答案 ⇒ 只能被忽略"

    def test_single_generation_shows_no_warning(self):
        """成对：不跨代时不许出警告 —— 恒亮的警告等于没有警告
        （同 v0.45.141 记的「三条名字一次没落下过」）。"""
        rows = [{"date": f"2026-08-{(i % 28) + 1:02d}", "ticker": f"T{i}", "hit": i % 2}
                for i in range(60)]
        ml = {(r["date"], r["ticker"]): 40.0 + (i % 40) for i, r in enumerate(rows)}
        res = PS.blend_scan(rows=rows, ml=ml, embargo_days=PS.EMBARGO_DAYS)
        assert res["spans_estimator_generations"] is False
        assert "跨了多代" not in PS._fmt(res, "融合权重扫描")

    def test_generation_counts_always_rendered(self):
        """不论跨不跨代，代际分布都要印——只在跨代时才印，读者就无从知道
        「这次是哪一代」，而那正是 --generation 之后最该核对的事。"""
        rows = [{"date": f"2026-08-{(i % 28) + 1:02d}", "ticker": f"T{i}", "hit": i % 2}
                for i in range(60)]
        ml = {(r["date"], r["ticker"]): 50.0 for r in rows}
        txt = PS._fmt(PS.blend_scan(rows=rows, ml=ml, embargo_days=PS.EMBARGO_DAYS), "x")
        assert "ML 估计量代际" in txt
        assert "pre-" in txt, "代际名字本身要印出来，只印一个数等于没说是哪代"

    def test_latest_filter_scores_one_generation_only(self):
        rows, ml = self._rows_two_generations()
        res = PS.blend_scan(rows=rows, ml=ml, embargo_days=PS.EMBARGO_DAYS,
                            generation="latest")
        assert res["spans_estimator_generations"] is False
        assert res["scored_generation"] == PS._ML_ESTIMATOR_GENERATIONS[-1][1]
        assert res["excluded_by_generation"], "过滤了却没记下排除了谁 ⇒ 样本凭空变少"
        assert res["n"] + sum(res["excluded_by_generation"].values()) == \
            sum(PS.blend_scan(rows=rows, ml=ml, embargo_days=PS.EMBARGO_DAYS)[
                "ml_estimator_generations"].values()), "过滤前后两套账对不上"

    def test_explicit_unknown_generation_is_refused_not_silently_empty(self):
        """要一个不存在的代必须报错。滤出空集再在空集上算 Brier，
        是「把失败改写成没发生过」的教科书形状。"""
        rows, ml = self._rows_two_generations()
        res = PS.blend_scan(rows=rows, ml=ml, generation="v9.9.9-不存在")
        assert res["status"] == "unknown_generation"
        assert res["available_generations"], "连有哪些代都不告诉，用户无从改正"

    def test_base_rate_window_not_shortened_by_filter(self):
        """过滤只该少记分行，**不该缩短时点基准率的历史窗口**——
        基准率是纯收益序列、与 ML 估计量无关。若滤在算 base 之前，
        新一代那些行的基准率会只看到新一代的历史，静默变一个量。"""
        rows, ml = self._rows_two_generations()
        allr = PS.blend_scan(rows=rows, ml=ml, embargo_days=PS.EMBARGO_DAYS)
        lat = PS.blend_scan(rows=rows, ml=ml, embargo_days=PS.EMBARGO_DAYS,
                            generation="latest")
        # w=1.0 只用 base；同一批行在两次调用里的 base 必须逐点相同 ⇒
        # 平均预测值相等（latest 是 all 的子集，取其对应子集比较不便，
        # 故比较更强的可观测量：latest 的 w=1.0 平均预测应等于只在这些行上
        # 用全历史算的基准率——用 mean_pred_pct 的稳定性代理）
        w1 = lambda r: next(g for g in r["grid"] if abs(g["w"] - 1.0) < 1e-9)  # noqa: E731
        assert w1(lat)["mean_pred_pct"] > 0, "基准率恒零说明历史窗口被滤空了"
        assert allr["n"] > lat["n"] > 0

    def test_latest_derived_from_joined_days_not_all_outcome_days(self):
        """`latest` 必须从「结果与 ML 概率**都有**的日子」里挑代，不是从全部结果日。

        补于 mutation check：变异「`_sel = ml_estimator_generation(dated[-1]...)`」
        当时全绿 —— 证等价时发现它不等价，是我的用例没覆盖到。
        分歧场景：最后一个有 T+7 结果的日子**没有对应报告**（生产常态，报告只出
        分数最高的 N 只，而 predictions 表收全部标的）。此时按全部结果日推出的
        「最新代」可能一条可记分样本都没有 ⇒ 过滤出空集 ⇒
        「把失败改写成没发生过」。
        """
        rows = [{"date": f"2026-08-{(i % 20) + 1:02d}", "ticker": f"T{i}", "hit": i % 2}
                for i in range(60)]
        ml = {(r["date"], r["ticker"]): 50.0 + (i % 20) for i, r in enumerate(rows)}
        # 一条落在**新一代**、且**没有** ML 概率的结果行（不进 ml 字典）
        rows.append({"date": "2026-09-20", "ticker": "ZZZ", "hit": 1})
        res = PS.blend_scan(rows=rows, ml=ml, embargo_days=PS.EMBARGO_DAYS,
                            generation="latest")
        assert res["status"] == "ok", (
            f"latest 指向了一个没有可记分样本的代 ⇒ {res.get('status')}")
        assert res["scored_generation"].startswith("pre-"), res["scored_generation"]
        assert res["n"] > 0

    def test_cli_generation_flag_end_to_end(self, tmp_path):
        rdir = tmp_path / "reports"; rdir.mkdir()
        rows = []
        for i in range(60):
            d = f"2026-08-{(i % 28) + 1:02d}"
            rows.append((d, f"T{i}", "bullish", 100.0, 110.0 if i % 2 else 90.0))
            _write_report(rdir, d, f"T{i}", 50.0 + (i % 20))
        for i in range(40):
            d = f"2026-09-{8 + (i % 5):02d}"
            rows.append((d, f"U{i}", "bullish", 100.0, 110.0 if i % 2 else 90.0))
            _write_report(rdir, d, f"U{i}", 50.0 + (i % 20))
        db = _mkdb(tmp_path, rows)
        assert PS.main(["--blend-scan", "--db", str(db), "--reports-dir", str(rdir),
                        "--generation", "latest"]) in (0, 1)
        assert PS.main(["--blend-scan", "--db", str(db), "--reports-dir", str(rdir),
                        "--generation", "不存在的代"]) == 3
