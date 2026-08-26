"""
v0.45.17 回归：方向口径 (`dir_correct_t7`) 与交易口径 (`correct_t7`) 必须分开。

这组测试守的是一个**语义**不变式，不是某个数值：
  - `correct_t7` 由路径依赖的离场收益算出（SL/TP 触发即截断），
    回答「这笔交易赚钱了吗」；
  - `dir_correct_t7` 由未截断的 T+7 收盘价算出，回答「方向猜对了吗」；
  - 中性预测从不建仓、从无 SL/TP，其判定带宽（5%）也与方向单（1%）不同，
    **不得与方向单合并进同一个分母**。

按项目惯例（见 [[alpha-hive-distribution-guards]]）：每条不变式都配一个
「喂退化数据看它红」的用例，否则测试只会在重构时给出虚假的绿。
"""
import sqlite3

import pytest

from backtester import PredictionStore


SCHEMA = """
CREATE TABLE predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, ticker TEXT, final_score REAL, direction TEXT,
    price_at_predict REAL,
    return_t7 REAL, correct_t7 INTEGER, checked_t7 INTEGER, ambiguous_t7 INTEGER,
    close_t7 REAL, dir_correct_t7 INTEGER, dir_ambiguous_t7 INTEGER
);
"""

# (direction, 交易口径判对, 方向口径判对)
# 关键行：第 3 行是「方向对但被止损打掉」——两个口径必须给出不同答案。
ROWS = [
    ("bullish", 1, 1),
    ("bullish", 0, 0),
    ("bullish", 0, 1),   # ← SL 打掉但 T+7 收盘仍上涨
    ("bearish", 1, 1),
    ("bearish", 0, 1),   # ← 同上，看空版
    ("neutral", 1, 1),
    ("neutral", 1, 0),
]


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    for i, (direction, trade_ok, dir_ok) in enumerate(ROWS):
        conn.execute(
            "INSERT INTO predictions (date, ticker, final_score, direction,"
            " price_at_predict, return_t7, correct_t7, checked_t7, ambiguous_t7,"
            " close_t7, dir_correct_t7, dir_ambiguous_t7)"
            " VALUES (?,?,?,?,?,?,?,1,0,?,?,0)",
            ("2026-08-1%d" % (i % 5 + 1), f"TK{i}", 5.0, direction,
             100.0, 1.0, trade_ok, 101.0, dir_ok),
        )
    conn.commit()
    conn.close()
    return PredictionStore(str(db))


def test_two_metrics_disagree(store):
    """两个口径必须真的不同——若有人把它们合并，这里先红。"""
    trade = store.get_accuracy_stats(period="t7", days=3650)
    direction = store.get_accuracy_stats(period="t7", days=3650,
                                         use_direction_metric=True)
    assert trade["metric"] == "trade"
    assert direction["metric"] == "direction"
    # 交易口径判对 4/7；方向口径判对 5/7
    assert trade["correct_count"] == 4
    assert direction["correct_count"] == 5
    assert trade["overall_accuracy"] != direction["overall_accuracy"]


def test_directional_excludes_neutral(store):
    """方向单合计不得含中性：分母只能是 bullish+bearish。"""
    res = store.get_accuracy_stats(period="t7", days=3650,
                                   use_direction_metric=True)
    assert res["directional_total"] == 5          # 3 bullish + 2 bearish
    assert res["total_checked"] == 7              # 含中性
    assert res["directional_total"] < res["total_checked"]
    assert res["directional_correct"] == 4        # 2 bullish + 2 bearish
    assert res["directional_accuracy"] == pytest.approx(0.8)


def test_ambiguous_rows_excluded(store, tmp_path):
    """dir_ambiguous_t7=1 的样本既不进分子也不进分母。"""
    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE predictions SET dir_ambiguous_t7 = 1 WHERE direction='bearish'")
    conn.commit()
    conn.close()
    res = store.get_accuracy_stats(period="t7", days=3650,
                                   use_direction_metric=True)
    assert res["directional_total"] == 3          # 只剩 bullish
    assert res["total_checked"] == 5


def test_unbackfilled_rows_not_counted_as_wrong(store):
    """
    dir_correct_t7 为 NULL（尚未回填）必须被**排除**，不能当成判错。

    这是本项目最常见的故障模式：拿 COALESCE 把「未知」兜成一个具体值，
    下游就再也分不清「真的错了」和「还没算」。见 [[alpha-hive-silent-degradation]]。
    """
    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE predictions SET dir_correct_t7 = NULL WHERE direction='bullish'")
    conn.commit()
    conn.close()
    res = store.get_accuracy_stats(period="t7", days=3650,
                                   use_direction_metric=True)
    assert res["directional_total"] == 2          # 只剩 bearish，bullish 被排除
    assert res["directional_correct"] == 2
    assert res["directional_accuracy"] == pytest.approx(1.0)


# ── v0.45.22：门面显著性标注 ──────────────────────────────────────────────

def test_wilson_ci_basic():
    """Wilson CI 应包含点估计且随 n 增大收窄。"""
    from backtester import _wilson_ci
    lo1, hi1 = _wilson_ci(50, 100)
    lo2, hi2 = _wilson_ci(500, 1000)
    assert lo1 < 50.0 < hi1
    assert (hi2 - lo2) < (hi1 - lo1), "样本变大 10 倍，区间必须收窄"
    assert _wilson_ci(0, 0) is None, "n=0 必须返回 None 而不是 (0,0)"


def test_t_test_insufficient_returns_none():
    """
    少于 3 周返回 None，**不得**兜底成 0.0 之类的数。

    p=0.0 与「样本不足」在页面上会长得一模一样，而前者意味着极显著——
    这正是本项目最常见的静默降级模式。
    """
    from backtester import _t_test_vs
    assert _t_test_vs([], 50.0) is None
    assert _t_test_vs([55.0, 60.0], 50.0) is None
    assert _t_test_vs([50.0] * 5, 50.0) is None, "零方差应返回 None"


def test_t_test_detects_signal():
    """明显偏离 50 的序列应给出小 p；贴着 50 的序列应给出大 p。"""
    from backtester import _t_test_vs
    strong = _t_test_vs([70.0, 72.0, 68.0, 71.0, 69.0, 73.0], 50.0)
    weak = _t_test_vs([51.0, 49.0, 52.0, 48.0, 50.5, 49.5], 50.0)
    assert strong is not None and strong < 0.01
    assert weak is not None and weak > 0.20


def test_stats_expose_ci_and_weekly_p(store):
    """方向口径必须同时给出 CI、不重叠周数与周度 p；交易口径不给（避免误用）。"""
    res = store.get_accuracy_stats(period="t7", days=3650,
                                   use_direction_metric=True)
    assert "directional_ci" in res
    assert "directional_p" in res
    assert "n_eff_weeks" in res
    if res["directional_total"] > 0:
        assert res["directional_ci"] is not None
        lo, hi = res["directional_ci"]
        assert lo <= res["directional_accuracy"] * 100 <= hi
