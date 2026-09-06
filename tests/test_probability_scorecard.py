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
    assert res["models"]["hit_rate"]["mean_pred_pct"] == pytest.approx(100.0, abs=1.0), (
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
    assert loose["models"]["hit_rate"]["mean_pred_pct"] < tight["models"]["hit_rate"]["mean_pred_pct"], (
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
    PS.record_published("2026-01-01", "AAA", "bullish", 100.0, "same_direction", 30,
                        ledger_path=led)
    PS.record_published("2026-01-02", "BBB", "bullish", None, None, 3, ledger_path=led)
    db = _mkdb(tmp_path, [("2026-01-01", "AAA", "bullish", 100.0, 110.0)])
    res = PS.score_published(ledger_path=led, db_path=db)
    assert res["status"] == "ok"
    assert res["ledger_rows"] == 2 and res["with_probability"] == 1
    assert res["coverage_pct"] == 50.0 and res["matured"] == 1
    assert res["published"]["brier"] == pytest.approx(0.0)   # 印 100%、真的命中


# ── 4. 退出码：会红的那个观测点 ──────────────────────────────────────
def test_exit_code_1_when_estimator_worse_than_legacy(tmp_path, capsys, monkeypatch):
    """构造一个分票频率必然更差的库：某票历史全胜、之后全败。

    这一条是整个模块的存在理由 —— 它绿了才说明「变差会被发现」。
    """
    rows = [(f"2026-01-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 110.0) for i in range(30)]
    # 之后全部翻车：分票频率仍报 ~100%，实际全败
    rows += [(f"2026-04-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 90.0) for i in range(30)]
    db = _mkdb(tmp_path, rows)
    res = PS.walk_forward(db_path=db, embargo_days=14, min_sample=20)
    assert res["hit_rate_worse_than_legacy"] is True
    code = PS.main(["--walk-forward", "--db", str(db)])
    assert code == 1, "估计器明显更差，退出码却不是 1 —— 这个观测点是哑的"


def test_exit_code_3_when_undeterminable(tmp_path):
    assert PS.main(["--walk-forward", "--db", str(tmp_path / "nope.db")]) == 3


def test_json_output_is_parseable(tmp_path, capsys):
    db = _mkdb(tmp_path, [(f"2026-01-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 110.0)
                          for i in range(25)])
    PS.main(["--walk-forward", "--db", str(db), "--json"])
    json.loads(capsys.readouterr().out)
