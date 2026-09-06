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


# ── 3. 评级已撤（v0.45.139）：只剩三个可核对的数 ─────────────────────
# 实测（n=438 份能对上 T+7 结果的报告）：第 1 章 BUY 命中 54.5% vs HOLD 56.9%
# （z=−0.55）；旧 STRONG BUY 38.3% vs BUY 59.0% —— 反向；ML 五等分非单调；
# 前瞻量各标的相同。没有任何一个概率输入分得开标的。
def test_rating_is_retired_not_defaulted(a):
    """rating / action 键保留（下游不崩），值为 None —— 不是 HOLD、不是 UNRATED"""
    pa = {"forward_estimate_pct": 55.6, "forward_sample_size": 684,
          "hit_rate_pct": 37.0, "sample_size": 46, "basis": "same_direction",
          "risk_reward_ratio": 0.84}
    out = a._generate_recommendation("X", {"probability_analysis": pa})
    assert out["rating"] is None and out["action"] is None
    assert out["rating_retired"] == "v0.45.139"
    assert out["probability_is_ticker_specific"] is False


def test_rationale_carries_all_three_numbers(a):
    pa = {"forward_estimate_pct": 55.6, "forward_sample_size": 684,
          "forward_ci95": [51.8, 59.2],
          "hit_rate_pct": 37.0, "sample_size": 46, "basis": "same_direction",
          "risk_reward_ratio": 0.84}
    r = a._generate_recommendation("X", {"probability_analysis": pa})["rationale"]
    assert "55.6%" in r and "684" in r and "[51.8, 59.2]" in r and "各标的相同" in r
    assert "37.0%" in r and "n=46" in r
    assert "0.84:1" in r


@pytest.mark.parametrize("missing", ["forward_estimate_pct", "hit_rate_pct", "risk_reward_ratio"])
def test_each_missing_number_is_said_not_faked(a, missing):
    """任一数不可得 ⇒ 说「不可得 / 未知」，不印 None、不挑兜底"""
    pa = {"forward_estimate_pct": 55.6, "forward_sample_size": 684,
          "hit_rate_pct": 37.0, "sample_size": 46, "basis": "same_direction",
          "risk_reward_ratio": 0.84}
    pa[missing] = None
    out = a._generate_recommendation("X", {"probability_analysis": pa})
    assert "None" not in out["rationale"]
    assert ("不可得" in out["rationale"]) or ("未知" in out["rationale"])
    if missing == "forward_estimate_pct":
        assert out["confidence"] is None


def test_no_rating_word_survives_in_user_facing_fields(a):
    """成对：撤掉的是评级**词** —— rating / action / rationale 里不许再出现任何旧词。
    （`rating_retired_reason` 是文档字段，引用旧词说明理由，不在此列）"""
    pa = {"forward_estimate_pct": 95.0, "forward_sample_size": 684,
          "hit_rate_pct": 95.0, "sample_size": 99, "basis": "same_direction",
          "risk_reward_ratio": 9.9}
    out = a._generate_recommendation("X", {"probability_analysis": pa})
    blob = " ".join(str(out[k]) for k in ("rating", "action", "rationale"))
    for w in ("STRONG BUY", "BUY", "HOLD", "AVOID", "UNRATED"):
        assert w not in blob, f"评级词 {w} 仍在面向读者的字段里"


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
