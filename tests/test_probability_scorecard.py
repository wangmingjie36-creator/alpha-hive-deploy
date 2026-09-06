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
