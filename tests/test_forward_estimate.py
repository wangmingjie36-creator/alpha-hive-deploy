"""描述量与前瞻量分列（v0.45.138）

背景：v0.45.134 把常数换成「同标的同方向历史 T+7 命中率」，并同时装了记分卡。
记分卡首跑（时点隔离 embargo 14 天，n=629）当场判定这次换源不够好——
在真正用上分票频率的 147 条上 **分票 Brier 0.2898 vs 池化基准率 0.2611**，
配对差 +0.0287 ± 0.0135（**t = +2.12，显著**）；经验贝叶斯收缩 α 从 0 扫到 ∞，
Brier **单调递减到完全收缩、无内部最优** ⇒ 分票那一层的信息量为零。

结论不是「分票频率是假的」——它作为**描述**完全成立。是「描述过去」与
「预测下一笔」被当成了同一件事。本版把它们拆成两个字段：

  hit_rate_pct         描述量：这只票这个方向过去赢过几成（逐标的）
  forward_estimate_pct 前瞻量：下一笔赢面多大（全书池化，各标的相同）

守五类：
  1. 池化估计量：Wilson 区间不越界、样本闸、命中口径与描述量一致
  2. 前瞻守卫：缺失 / None / NaN / bool 一律 None，不挑兜底值
  3. **评级与融合读前瞻量，不读描述量** —— 这是本版的全部意义
  4. 区分力：概率够不到最低升级闸时，任何 rr 都不改变评级 ⇒ 该评级不区分标的
  5. 账本记两个口径，记分打前瞻量；旧格式行（无该键）跳过而非退回描述量

全文件只碰临时 sqlite，不出网。
"""

import math
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_analyzer import AdvancedAnalyzer, HistoricalAnalyzer

_SCHEMA = """CREATE TABLE predictions (
    id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, direction TEXT, final_score REAL,
    price_at_predict REAL, close_t7 REAL)"""


def _mkdb(tmp_path, rows):
    p = tmp_path / "pheromone.db"
    con = sqlite3.connect(p)
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO predictions (date,ticker,direction,price_at_predict,close_t7)"
                    " VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return p


def _rows(n_win, n_lose, direction="bullish", ticker="AAA"):
    out = [(f"2026-01-{i % 28 + 1:02d}", ticker, direction, 100.0,
            110.0 if direction == "bullish" else 90.0) for i in range(n_win)]
    out += [(f"2026-02-{i % 28 + 1:02d}", ticker, direction, 100.0,
             90.0 if direction == "bullish" else 110.0) for i in range(n_lose)]
    return out


@pytest.fixture
def a():
    """不跑 __init__ —— 它会构造 OptionsAgent / DealerGEX，那些要出网。"""
    return AdvancedAnalyzer.__new__(AdvancedAnalyzer)


# ── 1. 池化估计量 ────────────────────────────────────────────────────
def test_pooled_hit_rate_counts_both_directions(tmp_path):
    """池化是全书汇总：多头空头都算，口径都是「方向调整后 > 0」"""
    db = _mkdb(tmp_path, _rows(60, 40) + _rows(40, 60, direction="bearish", ticker="BBB"))
    out = HistoricalAnalyzer(db).pooled_hit_rate()
    assert out["sample_size"] == 200
    assert out["hit_rate_pct"] == pytest.approx(50.0)   # (60+40)/200
    assert out["basis"] == "pooled_directional"


def test_pooled_below_min_sample_is_none_not_zero(tmp_path):
    """样本不够返回 None。0.0 在本量表恰是「一次没赢过」这个最强结论"""
    db = _mkdb(tmp_path, _rows(30, 30))
    out = HistoricalAnalyzer(db).pooled_hit_rate()
    assert out["hit_rate_pct"] is None and out["ci95"] is None
    assert out["sample_size"] == 60 and "低于" in out["note"]


def test_pooled_ignores_neutral(tmp_path):
    db = _mkdb(tmp_path, _rows(60, 40)
               + [("2026-03-01", "CCC", "neutral", 100.0, 110.0)] * 50)
    assert HistoricalAnalyzer(db).pooled_hit_rate()["sample_size"] == 100


def test_pooled_flat_return_counts_as_miss(tmp_path):
    """口径必须与 calculate_expected_returns 一致：恰好 0 计入分母、算未命中"""
    db = _mkdb(tmp_path, _rows(50, 0)
               + [(f"2026-03-{i % 28 + 1:02d}", "AAA", "bullish", 100.0, 100.0)
                  for i in range(50)])
    assert HistoricalAnalyzer(db).pooled_hit_rate()["hit_rate_pct"] == pytest.approx(50.0)


@pytest.mark.parametrize("k,n", [(0, 10), (10, 10), (1, 3), (5, 10), (999, 1000)])
def test_wilson_never_leaves_unit_interval(k, n):
    """正态近似在 p 靠近 0/1 时会给出 <0 或 >100 的界；Wilson 不会。
    「命中率区间上界 103%」这种数印出去就是错的。"""
    lo, hi = HistoricalAnalyzer._wilson_ci(k, n)
    assert 0.0 <= lo <= hi <= 100.0


def test_wilson_narrows_with_sample_size():
    """成对：样本越多区间越窄 —— 否则「区间」只是装饰"""
    w_small = HistoricalAnalyzer._wilson_ci(50, 100)
    w_big = HistoricalAnalyzer._wilson_ci(500, 1000)
    assert (w_big[1] - w_big[0]) < (w_small[1] - w_small[0])


def test_wilson_rejects_zero_n():
    with pytest.raises(ValueError):
        HistoricalAnalyzer._wilson_ci(0, 0)


# ── 2. 前瞻守卫 ──────────────────────────────────────────────────────
def test_forward_estimate_passes_through(a):
    assert a._forward_estimate("X", {"hit_rate_pct": 55.6, "sample_size": 684}) == 55.6


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True, False, "55", {}])
def test_forward_estimate_rejects_non_finite_and_bool(a, bad):
    assert a._forward_estimate("X", {"hit_rate_pct": bad}) is None


def test_forward_estimate_zero_is_a_conclusion(a):
    """成对：0.0% 是「一次没赢过」这个明确结论，必须透传"""
    assert a._forward_estimate("X", {"hit_rate_pct": 0.0}) == 0.0


# ── 3. 评级读前瞻量，不读描述量（本版的全部意义）─────────────────────
def test_rating_reads_forward_not_hit_rate(a):
    """描述量高得离谱、前瞻量低 ⇒ 评级必须跟前瞻量走"""
    pa = {"hit_rate_pct": 95.0, "sample_size": 40, "basis": "same_direction",
          "forward_estimate_pct": 45.0, "forward_sample_size": 684,
          "risk_reward_ratio": 3.0}
    assert a._generate_recommendation("X", {"probability_analysis": pa})["rating"] == "AVOID"


def test_rating_unrated_when_forward_missing_even_if_hit_rate_present(a):
    """成对：描述量在、前瞻量缺 ⇒ 仍然不评级（描述量不许顶替前瞻量）"""
    pa = {"hit_rate_pct": 81.8, "sample_size": 22, "basis": "same_direction",
          "forward_estimate_pct": None, "forward_sample_size": 40,
          "risk_reward_ratio": 2.5}
    assert a._generate_recommendation("X", {"probability_analysis": pa})["rating"] == "UNRATED"


def test_rationale_states_the_probability_is_book_wide(a):
    """「所有票都 HOLD」不能被读成「系统逐个评估后都选了 HOLD」"""
    pa = {"forward_estimate_pct": 55.6, "forward_sample_size": 684,
          "forward_ci95": [51.8, 59.2], "risk_reward_ratio": 1.67}
    out = a._generate_recommendation("X", {"probability_analysis": pa})
    assert out["probability_is_ticker_specific"] is False
    assert "各标的相同" in out["rationale"] and "684" in out["rationale"]


# ── 4. 区分力 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("fwd,expect", [
    (45.0, False),   # 够不到最低升级闸 ⇒ rr 无从发挥
    (55.6, False),   # 2026-09-06 生产实测值
    (59.9, False),   # 闸下一线
    (60.0, True),    # 最低升级闸 —— 此处起 rr 开始改变结果
    (75.0, True),
])
def test_rating_discrimination_boundary(a, fwd, expect):
    """概率够不到最低升级闸时，**任何 rr 都不改变评级** ⇒ 该评级不区分标的。

    ⚠️ 这条断言会随生产池化率变化而变红：2026-09-06 实测 55.6%（闸 60.0），
    评级此刻不携带任何逐标的信息。池化率一旦越过 60，就该回来重看这三道
    从未验证过的闸（它们是从 v0.45.134 前的常数量表继承下来的）。
    """
    pa = {"forward_estimate_pct": fwd, "forward_sample_size": 684,
          "risk_reward_ratio": 2.5}
    assert a._generate_recommendation("X", {"probability_analysis": pa})[
        "rating_discriminates_tickers"] is expect


def test_non_discriminating_rating_says_so_in_rationale(a):
    pa = {"forward_estimate_pct": 55.6, "forward_sample_size": 684, "risk_reward_ratio": 9.9}
    r = a._generate_recommendation("X", {"probability_analysis": pa})
    assert "不区分标的" in r["rationale"]


def test_rr_truly_cannot_change_a_non_discriminating_rating(a):
    """把区分力字段算对还不够 —— 直接穷举 rr 证明结果确实不动"""
    base = {"forward_estimate_pct": 55.6, "forward_sample_size": 684}
    got = {a._generate_recommendation("X", {"probability_analysis": dict(base, risk_reward_ratio=rr)})["rating"]
           for rr in (None, 0.0, 0.5, 1.5, 2.0, 5.0, 100.0)}
    assert got == {"HOLD"}, f"rr 竟然改变了评级: {got}"


# ── 5. 账本口径 ──────────────────────────────────────────────────────
def test_ledger_scores_forward_not_hit_rate(tmp_path):
    """记分打前瞻量。旧格式行（无 forward_estimate_pct）跳过，**不退回描述量**
    —— 两个口径混进同一张记分卡，测的就不是同一件事了。"""
    import json

    import probability_scorecard as PS
    led = tmp_path / "published.jsonl"
    PS.record_published("2026-01-01", "AAA", "bullish", 90.0, "same_direction", 40,
                        ledger_path=led, forward_estimate_pct=100.0, forward_sample_size=684)
    # 手写一行 v0.45.138 之前的格式（只有描述量）
    with led.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": "2026-01-02", "ticker": "BBB", "direction": "bullish",
                             "hit_rate_pct": 100.0, "basis": "same_direction",
                             "sample_size": 30}, ensure_ascii=False) + "\n")
    db = _mkdb(tmp_path, [("2026-01-01", "AAA", "bullish", 100.0, 110.0),
                          ("2026-01-02", "BBB", "bullish", 100.0, 90.0)])
    res = PS.score_published(ledger_path=led, db_path=db)
    assert res["ledger_rows"] == 2
    assert res["with_probability"] == 1, "旧格式行不该被算进记分"
    assert res["published"]["brier"] == pytest.approx(0.0)   # 印 100%、真的命中
