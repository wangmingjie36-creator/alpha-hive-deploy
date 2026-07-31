"""
波动率预测与仓位分层测试（v0.44.0）

## 背景

2026-07-30 决定性对照（90 只 × 897 交易日）：同一宇宙、同样朴素的特征，
只换预测目标 —— 未来 7 日**收益率** IC=+0.012，未来 7 日**已实现波动** IC=+0.710。
可学性差 60 倍。系统此前在预测一个 IC≈0.01 的目标，天花板与架构无关。

## 本模块的两条硬约束

1. **必须是横截面**：实测 price.volatility_20d 的固定效应 IC=+0.720、
   票内时变仅 +0.012 ⇒ 能答"哪些股票波动大"，不能答"何时变大"。
2. **不参与评分**：vol_score 是并行输出，不进 EVALUATION_WEIGHTS，
   因此不破坏 784 条历史样本的可比性。
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vol_forecast as vf


def _row(iv, v20):
    return {"options.iv_current": iv, "price.volatility_20d": v20}


class TestRankPct:
    def test_monotone_and_bounded(self):
        r = vf._rank_pct([10.0, 20.0, 30.0, 40.0])
        assert r == [0.0, pytest.approx(1 / 3), pytest.approx(2 / 3), 1.0]

    def test_ties_share_average_rank(self):
        r = vf._rank_pct([5.0, 5.0, 9.0])
        assert r[0] == r[1] and r[2] > r[0]

    def test_single_element(self):
        assert vf._rank_pct([7.0]) == [0.5]


class TestScoring:
    def test_higher_inputs_yield_higher_score(self):
        s = vf.score_cross_section({
            "LOW": _row(0.20, 1.0), "MID": _row(0.40, 2.0), "HIGH": _row(0.80, 4.0)})
        assert s["HIGH"] > s["MID"] > s["LOW"]
        assert all(0.0 <= v <= 1.0 for v in s.values())

    def test_missing_component_ticker_is_skipped(self):
        """缺分量的标的必须跳过，而不是填默认值。

        填默认值会让缺数据的标的挤进某个分位，制造虚假分层。
        """
        s = vf.score_cross_section({
            "A": _row(0.2, 1.0), "B": _row(0.4, 2.0),
            "C": _row(0.6, 3.0), "BAD": {"options.iv_current": 0.5}})
        assert "BAD" not in s and len(s) == 3

    def test_too_few_tickers_returns_empty(self):
        """横截面太窄时分位无意义，应返回空而非给出可疑分层"""
        assert vf.score_cross_section({"A": _row(0.2, 1.0), "B": _row(0.4, 2.0)}) == {}

    def test_both_components_contribute(self):
        """两个分量都必须影响结果 —— 否则等权配置形同虚设"""
        base = {"A": _row(0.1, 5.0), "B": _row(0.5, 3.0), "C": _row(0.9, 1.0)}
        s1 = vf.score_cross_section(base)
        flipped = dict(base)
        flipped["A"] = _row(0.1, 1.0)   # 只改 v20
        s2 = vf.score_cross_section(flipped)
        assert s1["A"] != s2["A"], "改动 volatility_20d 未影响结果"


class TestSizing:
    def _scores(self, n=9):
        return {f"T{i}": i / (n - 1) for i in range(n)}

    def test_three_tiers_assigned(self):
        out = vf.size_multipliers(self._scores())
        mults = {d["multiplier"] for d in out.values()}
        assert mults == {1.25, 1.00, 0.70}

    def test_high_vol_gets_smallest_multiplier(self):
        out = vf.size_multipliers(self._scores())
        hi = max(out, key=lambda t: out[t]["vol_score"])
        lo = min(out, key=lambda t: out[t]["vol_score"])
        assert out[hi]["multiplier"] < out[lo]["multiplier"]
        assert out[hi]["multiplier"] == 0.70 and out[lo]["multiplier"] == 1.25

    def test_every_ticker_gets_a_multiplier(self):
        out = vf.size_multipliers(self._scores(12))
        assert len(out) == 12
        assert all(d["multiplier"] > 0 for d in out.values())

    def test_empty_input(self):
        assert vf.size_multipliers({}) == {}


class TestConfigInvariants:
    def test_components_weights_sum_positive(self):
        assert vf.COMPONENTS and sum(vf.COMPONENTS.values()) > 0

    def test_tiers_cover_full_range_without_gap(self):
        """分位区间必须无缝覆盖 [0,1]，否则会有标的拿不到乘数"""
        tiers = sorted(vf.SIZING_TIERS)
        assert tiers[0][0] == 0.0
        assert tiers[-1][1] > 1.0
        for a, b in zip(tiers, tiers[1:]):
            assert a[1] == b[0], f"分层区间有缝隙: {a} → {b}"

    def test_high_vol_multiplier_below_one(self):
        """高波动组必须降仓 —— 这是本模块的全部意义"""
        assert max(t[2] for t in vf.SIZING_TIERS if t[0] >= 0.6) < 1.0

    def test_not_wired_into_evaluation_weights(self):
        """vol_score 不得进入评分权重 —— 会破坏历史样本可比性"""
        import config
        assert "vol" not in " ".join(config.EVALUATION_WEIGHTS.keys()).lower()


class TestLoadDay:
    def test_reads_archived_components(self, tmp_path):
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE signal_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            signal TEXT, value REAL)""")
        for tk, iv, v in [("A", 0.3, 1.5), ("B", 0.6, 3.0)]:
            con.execute("INSERT INTO signal_archive (date,ticker,signal,value) "
                        "VALUES ('2026-07-29',?,'options.iv_current',?)", (tk, iv))
            con.execute("INSERT INTO signal_archive (date,ticker,signal,value) "
                        "VALUES ('2026-07-29',?,'price.volatility_20d',?)", (tk, v))
        con.execute("INSERT INTO signal_archive (date,ticker,signal,value) "
                    "VALUES ('2026-07-28','A','options.iv_current',9.9)")
        con.commit(); con.close()

        rows = vf.load_day("2026-07-29", db)
        assert set(rows) == {"A", "B"}
        assert rows["A"]["options.iv_current"] == 0.3
        # 不得混入别的日期
        assert rows["A"]["options.iv_current"] != 9.9

    def test_missing_date_returns_empty(self, tmp_path):
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE signal_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            signal TEXT, value REAL)""")
        con.commit(); con.close()
        assert vf.load_day("2026-01-01", db) == {}
