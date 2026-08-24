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
import subprocess
import sys
from pathlib import Path

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


class TestDailyReportIntegration:
    """v0.44.0：接进日报 —— **只观察，不影响仓位**

    这是"先观察再下注"路线的第一步。核心断言不是"它能输出"，
    而是"它**没有**碰任何下注逻辑" —— 否则纸面组合的历史可比性就断了。
    """

    def _reporter(self, date="2026-07-29"):
        from alpha_hive_daily_report import AlphaHiveDailyReporter as R
        r = R.__new__(R)
        r.date_str = date
        return r

    def test_returns_empty_when_no_archive(self, monkeypatch):
        """无归档数据时安全返回空，不抛异常"""
        import vol_forecast as v
        monkeypatch.setattr(v, "load_day", lambda *a, **k: {})
        assert self._reporter()._volatility_tiers() == {}
        assert self._reporter()._volatility_tier_markdown() == ""

    def test_failure_does_not_break_report(self, monkeypatch):
        """任何异常都不得阻断日报生成 —— 这是观察性输出"""
        import vol_forecast as v
        def boom(*a, **k):
            raise RuntimeError("模拟行情源故障")
        monkeypatch.setattr(v, "load_day", boom)
        assert self._reporter()._volatility_tiers() == {}
        assert self._reporter()._volatility_tier_markdown() == ""

    def test_markdown_states_it_is_observation_only(self, monkeypatch):
        """渲染必须明确写出"未影响仓位" —— 防止读者误以为已在下注"""
        import vol_forecast as v
        monkeypatch.setattr(v, "load_day", lambda *a, **k: {
            "A": {"options.iv_current": 0.2, "price.volatility_20d": 1.0},
            "B": {"options.iv_current": 0.5, "price.volatility_20d": 3.0},
            "C": {"options.iv_current": 0.9, "price.volatility_20d": 6.0},
        })
        md = self._reporter()._volatility_tier_markdown()
        assert "未影响" in md and "观察" in md
        assert "不可用于择时" in md, "必须写明横截面限制"
        for tk in ("A", "B", "C"):
            assert f"| {tk} |" in md

    def test_does_not_touch_position_sizing(self):
        """源码级护栏：vol_forecast 不得被 paper_portfolio 引用。

        路线是"先观察一两个月，再决定是否下注"。若有人提前接进下注逻辑，
        纸面组合的历史可比性会断裂，而这一步应当是显式决策。
        """
        import pathlib
        pp = pathlib.Path(__file__).resolve().parent.parent / "paper_portfolio.py"
        if not pp.exists():
            pytest.skip("paper_portfolio.py 不存在")
        src = pp.read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        assert "vol_forecast" not in code, (
            "paper_portfolio 引用了 vol_forecast —— 当前阶段应为纯观察。"
            "接入下注前请先积累样本并显式决策")

    def test_multiplier_is_advisory_field_only(self, monkeypatch):
        """multiplier 只是建议值，结构里不得混入实际下注字段"""
        import vol_forecast as v
        monkeypatch.setattr(v, "load_day", lambda *a, **k: {
            f"T{i}": {"options.iv_current": i * 0.1,
                      "price.volatility_20d": float(i)} for i in range(1, 7)})
        tiers = self._reporter()._volatility_tiers()
        assert tiers
        for d in tiers.values():
            assert set(d) == {"vol_score", "pct", "multiplier", "tier"}, \
                f"字段集合意外变化: {set(d)}"


# ────────────────────────────────────────────────────────────────────────────
# v0.45.1 二次检查发现的三个缺陷（回归锁）
# ────────────────────────────────────────────────────────────────────────────

class TestLoadDayResilience:
    """load_day 在库/表缺失时必须返回空 dict，而不是抛 OperationalError。

    背景：日报路径外面有 try/except 兜底，所以这个缺陷在日报里看不见；
    但 CLI 直接跑（vol_forecast.py --date）会吐一屏 traceback，
    而正确行为是提示"先跑 --backfill"。
    """

    def test_missing_table_returns_empty(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()          # 库存在，但没有 signal_archive 表
        assert vf.load_day("2026-07-29", db) == {}

    def test_missing_db_file_returns_empty(self, tmp_path):
        assert vf.load_day("2026-07-29", tmp_path / "nope" / "no.db") == {}

    def test_cli_exits_cleanly_without_traceback(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "vol_forecast.py"),
             "--date", "2026-07-29", "--db", str(db)],
            capture_output=True, text=True)
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "backfill" in r.stderr


class TestSingleDatabaseRead:
    """波动率分层每次生成报告只能读一次库。

    旧实现里 `_volatility_tier_markdown()` 内部又调了一次 `_volatility_tiers()`，
    而 `report["volatility_tiers"]` 也调一次 —— 一次报告读两遍库、打两遍日志。
    除了浪费，两次结果若不一致（并发写入 signal_archive）还会让 JSON 与
    Markdown 内容对不上，属于难查的一类不一致。
    """

    def _reporter_with_data(self, monkeypatch, calls):
        """构造一个 reporter，并记录 vf.load_day 的调用次数。"""
        import vol_forecast as _vf
        from alpha_hive_daily_report import AlphaHiveDailyReporter

        fake = {t: {"options.iv_current": 0.2 + i * 0.05,
                    "price.volatility_20d": 0.3 + i * 0.04}
                for i, t in enumerate(["AAA", "BBB", "CCC", "DDD"])}
        monkeypatch.setattr(_vf, "load_day",
                            lambda d, db=None: (calls.append(d), fake)[1])
        r = AlphaHiveDailyReporter.__new__(AlphaHiveDailyReporter)
        r.date_str = "2026-07-29"
        return r

    def test_markdown_accepts_precomputed_tiers(self, monkeypatch):
        calls = []
        r = self._reporter_with_data(monkeypatch, calls)
        tiers = r._volatility_tiers()
        assert len(calls) == 1
        md = r._volatility_tier_markdown(tiers)      # 传入 → 不应再读库
        assert len(calls) == 1, f"传入已算好的分层后仍读了库：{len(calls)} 次"
        assert md, "传入分层后应正常渲染出 Markdown"

    def test_markdown_backward_compatible_without_arg(self, monkeypatch):
        """不传参仍可用（向后兼容），但会自行查询一次。"""
        calls = []
        r = self._reporter_with_data(monkeypatch, calls)
        md = r._volatility_tier_markdown()
        assert len(calls) == 1
        assert md

    def test_both_call_styles_agree(self, monkeypatch):
        """传参与不传参必须渲染出同样的内容。"""
        calls = []
        r = self._reporter_with_data(monkeypatch, calls)
        assert r._volatility_tier_markdown(r._volatility_tiers()) == \
            r._volatility_tier_markdown()
