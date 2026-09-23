"""
experiments/ml_expected_return_replay.py 的信号强度口径与判定（v0.45.329）

背景：脚本原先把动量 / 拥挤度 rank-IC 算成**一个跨全部日期与标的的池化 Spearman**，再按写死的
IC ±0.02 印「⚠️ 负相关 —— 短期反转」/「✅ 正相关」，按准确率差 ±1pp 印「优于 / 持平 / 差于」。
池化 IC 主要在测「哪天涨」而不是「同一天该挑哪只」（memory `alpha-hive-cross-sectional-pooling`，
第三例）；两个阈值都在噪音之下——v0.45.326 只换收益列，两条判定就一起翻了，而前后都不显著。
现改为照抄 `final_score_dilution.weekly_ic` 的标准口径（日度横截面 → 每 ISO 周第一个可用
交易日 → 周序列），p 用 t(n−1)，判定只看显著性。

夹具
----
· **符号相反夹具**：每天有一个大的公共水平 L_d——动量整体抬高 L_d、收益整体压低 1.5·L_d；
  日内则由小噪音决定排序，方向与之相反。池化 Spearman 被日间差主导 ⇒ 动量强负、拥挤度强正；
  同一天的横截面 ⇒ 动量正、拥挤度负。每周 3 天、L_d 各不相同 ⇒ **周内合并**也是池化的符号
  （memory：去池化要去到同一天，周内合并在 v0.45.326 复核时造出过假 p=0.05）。
· **噪音夹具**：4 周 IC=+1、3 周 IC=−1 ⇒ 池化 IC≈+0.14、准确率差 +7pp，都跨过旧阈值；
  周序列 t 检验 p≈0.7。

「文本判定」两组只断言印出的字串、不碰 JSON 键——拿改动前的脚本原样来跑，
红的理由就是「判定按池化 / 固定阈值给出」，而不是「新键不存在」。
"""

import datetime as dt
import importlib.util
import json
import math
import random
import sqlite3
import statistics
import sys
from pathlib import Path

import pytest
from scipy import stats

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rep = _load("ml_expected_return_replay", "experiments/ml_expected_return_replay.py")

MONDAY0 = dt.date(2026, 6, 1)          # 周一，ISO 2026-W23（已核对）
N_WEEKS_FLIP = 8
TICKERS = [f"T{i}" for i in range(10)]


def _day(week, offset):
    return str(MONDAY0 + dt.timedelta(days=7 * week + offset))


def _create_tables(con):
    con.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        price_at_predict REAL, price_t7 REAL, return_t7 REAL, close_t7 REAL,
        exit_price REAL, exit_reason TEXT, checked_t7 INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE signal_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ticker TEXT NOT NULL,
        signal TEXT NOT NULL, value REAL, UNIQUE(date, ticker, signal))""")


def _write_db(path, rows):
    """rows: [(date, ticker, momentum, crowding, 真实收益%)]。离场价与收盘价一致（本文件不测列选择）。"""
    con = sqlite3.connect(path)
    _create_tables(con)
    for d, tk, mom, crd, ret in rows:
        close = 100.0 * (1 + ret / 100.0)
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,price_t7,return_t7,"
                    "close_t7,exit_price,exit_reason,checked_t7) VALUES (?,?,?,?,?,?,?,?,1)",
                    (d, tk, 100.0, close, ret, close, close, "T7_CLOSE"))
        con.executemany("INSERT INTO signal_archive (date,ticker,signal,value) VALUES (?,?,?,?)",
                        [(d, tk, "price.momentum_5d", mom), (d, tk, "crowding.score", crd)])
    con.commit()
    con.close()
    return path


def _sign_flip_rows():
    rng = random.Random(329)
    rows = []
    # 第 0 周周一：只有 4 只（< MIN_WIDTH），日内方向与其余天相反 —— 该周应取周二。
    # 符号断言不靠它；它专门给「与 final_score_dilution 同口径」那条断言一个能区分的输入。
    for tk in TICKERS[:4]:
        u = rng.uniform(-1, 1)
        rows.append((_day(0, 0), tk, u, 50.0 + u, -2.0 * u))
    for w in range(N_WEEKS_FLIP):
        levels = [-15.0, 0.0, 15.0]          # 周内三天的公共水平拉开 ⇒ 周内合并也被日间差主导
        rng.shuffle(levels)
        for off, base in zip((1, 2, 3) if w == 0 else (0, 1, 2), levels):
            level = base + rng.uniform(-2.0, 2.0)
            for tk in TICKERS:
                u, v = rng.uniform(-1, 1), rng.uniform(-1, 1)
                rows.append((_day(w, off), tk,
                             level + u,                                  # 动量：日间 ±15，日内 ±1
                             50.0 - 2.0 * level + v,                     # 拥挤度：日间与动量反向
                             -1.5 * level + 2.0 * u - 2.0 * v + rng.gauss(0, 0.1)))
    return rows


def _noise_rows():
    """7 周、每周一天、每天 10 只（5 只动量为负）。周 0/2/4/6：收益 = 动量（IC=+1，
    模型全对）；周 1/3/5：收益 = −动量（IC=−1，模型全错）。"""
    moms = [1.0, 2.0, 3.0, 4.0, 5.0, -1.0, -2.0, -3.0, -4.0, -5.0]
    rows = []
    for w in range(7):
        sign = 1.0 if w % 2 == 0 else -1.0
        for tk, m in zip(TICKERS, moms):
            rows.append((_day(w, 0), tk, m, 30.0 + m, sign * m))
    return rows


def _edge_rows():
    """6 周、模型每天全对（收益 = 动量），每天负动量只数不同 ⇒ 相对恒定看多的优势每周为正且有方差。"""
    rows = []
    for w, n_neg in enumerate((3, 4, 5, 6, 3, 4)):
        moms = [float(k + 1) for k in range(10 - n_neg)] + [-float(k + 1) for k in range(n_neg)]
        for tk, m in zip(TICKERS, moms):
            rows.append((_day(w, 0), tk, m, 30.0, m))
    return rows


def _run_main(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["ml_expected_return_replay.py", *args])
    assert rep.main() == 0, capsys.readouterr().err
    return capsys.readouterr().out


def _iso_week(d):
    return dt.date.fromisoformat(d).isocalendar()[:2]


# ────────────────────────────────────────────────────────────────────────────
# 夹具自检：没有这一组，下面的「横截面为正」可能只是夹具本来就分不出口径
# ────────────────────────────────────────────────────────────────────────────

class TestFixturesDiscriminate:

    def test_sign_flip_pooled_and_within_week_disagree_with_same_day(self):
        rows = _sign_flip_rows()
        assert len({_iso_week(d) for d, *_ in rows}) == N_WEEKS_FLIP
        for col, pooled_sign in ((2, -1), (3, +1)):          # 2=动量, 3=拥挤度
            pooled = rep._spearman([r[col] for r in rows], [r[4] for r in rows])
            assert pooled * pooled_sign > 0.5, f"col {col}: 全样本池化 {pooled:+.3f}"
            by_week, by_day = {}, {}
            for r in rows:
                by_week.setdefault(_iso_week(r[0]), []).append(r)
                by_day.setdefault(r[0], []).append(r)
            wk = [rep._spearman([r[col] for r in rs], [r[4] for r in rs]) for rs in by_week.values()]
            assert all(x * pooled_sign > 0.3 for x in wk), f"col {col}: 周内合并 {wk}"
            same_day = [rep._spearman([r[col] for r in rs], [r[4] for r in rs])
                        for rs in by_day.values() if len(rs) >= rep.MIN_WIDTH]
            assert statistics.fmean(same_day) * pooled_sign < -0.4, f"col {col}: 同日 {same_day}"

    def test_noise_fixture_crosses_the_old_thresholds(self):
        rows = _noise_rows()
        pooled = rep._spearman([r[2] for r in rows], [r[4] for r in rows])
        assert pooled > 0.02, f"旧阈值 +0.02 会印「✅ 正相关」的前提：池化 {pooled:+.4f}"
        rets = [r[4] for r in rows]
        acc = rep._accuracy([rep.new_expected(r[2], r[3]) for r in rows], rets)
        delta = acc["accuracy"] - sum(1 for x in rets if x > 0) / len(rets)
        assert delta > 0.01, f"旧阈值 +1pp 会印「优于」的前提：{delta:+.3%}"


# ────────────────────────────────────────────────────────────────────────────
# 文本判定（只断言字串，改动前的脚本原样跑应在这里红）
# ────────────────────────────────────────────────────────────────────────────

class TestTextVerdictIsCrossSectional:

    def test_sign_flip_prints_cross_sectional_signs(self, tmp_path, monkeypatch, capsys):
        out = _run_main(monkeypatch, capsys, "--db", str(_write_db(tmp_path / "f.db", _sign_flip_rows())))
        assert "短期反转" not in out, "动量判定按全样本池化 IC 给出（池化 <0、同日横截面 >0）"
        assert "✅ 显著正相关 —— sign(动量)" in out
        assert "⚠️ 显著负相关（四口径" in out, "拥挤度：同日横截面 <0、池化 >0"

    def test_noise_prints_no_direction_and_no_accuracy_ranking(self, tmp_path, monkeypatch, capsys):
        out = _run_main(monkeypatch, capsys, "--db", str(_write_db(tmp_path / "n.db", _noise_rows())))
        assert "正相关 —— sign(动量)" not in out, "池化 IC +0.14 跨过 +0.02 就印了正相关——判定没看显著性"
        assert "方向准确率 优于" not in out and "显著优于" not in out, (
            "准确率差 +7pp 跨过 +1pp 就印了优于——判定没看显著性")
        assert "➖ 未检出方向信息" in out
        assert "未检出与恒定看多基准有差异" in out


# ────────────────────────────────────────────────────────────────────────────
# JSON 结构与数值
# ────────────────────────────────────────────────────────────────────────────

class TestJsonHeadline:

    def test_sign_flip_headline_is_cross_sectional_pooled_is_labelled(self, tmp_path, monkeypatch, capsys):
        res = json.loads(_run_main(monkeypatch, capsys, "--db",
                                   str(_write_db(tmp_path / "f.db", _sign_flip_rows())), "--json"))
        mom, crd = res["momentum_ic"], res["crowding_ic"]
        assert mom["weekly"]["n_weeks"] == N_WEEKS_FLIP
        assert mom["verdict"] == "positive" and mom["weekly"]["mean"] > 0.4, mom
        assert crd["verdict"] == "negative" and crd["weekly"]["mean"] < -0.4, crd
        # 池化值保留，但只挂在明确标注的键下，且符号与判定相反
        assert mom["pooled_across_dates"] < -0.5 and crd["pooled_across_dates"] > 0.5
        assert "ic_momentum_vs_forward" not in res and "ic_crowding_vs_forward" not in res, (
            "旧键是池化值：留着同名会让旧读者静默读到另一个量")

    def test_noise_verdicts_not_significant(self, tmp_path, monkeypatch, capsys):
        res = json.loads(_run_main(monkeypatch, capsys, "--db",
                                   str(_write_db(tmp_path / "n.db", _noise_rows())), "--json"))
        assert res["momentum_ic"]["verdict"] == "not_significant", res["momentum_ic"]
        edge = res["new_accuracy_edge_vs_always_bullish"]
        assert edge["verdict"] == "not_significant", edge
        assert edge["weekly"]["mean"] == pytest.approx(0.5 / 7), "同日配对差：±0.5 交替 7 周"

    def test_significant_accuracy_edge_is_still_reported(self, tmp_path, monkeypatch, capsys):
        """反向对照：显著时照样判「优于」——判定不是「永远不显著」。"""
        db = _write_db(tmp_path / "e.db", _edge_rows())
        res = json.loads(_run_main(monkeypatch, capsys, "--db", str(db), "--json"))
        edge = res["new_accuracy_edge_vs_always_bullish"]
        assert edge["verdict"] == "positive" and edge["weekly"]["p"] < 0.001, edge
        assert "✅ 显著优于恒定看多基准" in _run_main(monkeypatch, capsys, "--db", str(db))


class TestWeeklyTTest:

    def test_p_is_student_t_not_normal(self):
        """n=5、t≈2.5：t(4) 的 p≈0.067，正态近似 p≈0.012 —— 用 erfc 会把它判成显著。"""
        base = [-1.0, -0.5, 0.0, 0.5, 1.0]
        shift = 2.5 * statistics.stdev(base) / math.sqrt(len(base))
        series = [b + shift for b in base]
        res = rep.weekly_t_test(series)
        assert res["t"] == pytest.approx(2.5) and res["df"] == 4
        assert res["p"] == pytest.approx(stats.ttest_1samp(series, 0.0).pvalue)
        assert math.erfc(2.5 / math.sqrt(2)) < rep.ALPHA < res["p"], "夹具须能区分 t 与正态"
        assert rep.significance_verdict(res) == "not_significant"

    @pytest.mark.parametrize("series", [[0.1, 0.2], [0.3, 0.3, 0.3, 0.3]])
    def test_too_few_weeks_or_flat_is_insufficient_not_insignificant(self, series):
        res = rep.weekly_t_test(series)
        assert res is None and rep.significance_verdict(res) == "insufficient"


class TestSameAsCanonical:
    """「照抄 final_score_dilution.weekly_ic」是需要被执行的断言：同一输入、同一周序列。
    符号相反夹具第 0 周周一只有 4 只 ⇒ 也钉住 MIN_WIDTH 与「每周第一个**可用**交易日」。"""

    def test_weekly_series_equals_final_score_dilution(self):
        fsd = _load("final_score_dilution", "experiments/final_score_dilution.py")
        assert fsd.MIN_WIDTH == rep.MIN_WIDTH
        rows = _sign_flip_rows()
        by_day = rep.group_by_day([(d, tk, m, c, r) for d, tk, m, c, r in rows])
        fsd_by_day = {d: [{"mom": m, "crd": c, "ret": r} for m, c, r in recs]
                      for d, recs in by_day.items()}
        for col, key in ((rep.MOM, "mom"), (rep.CRD, "crd")):
            ours = rep.weekly_series(rep.daily_cross_sectional_ic(by_day, col))
            theirs = fsd.weekly_ic(fsd_by_day, lambda r, _k=key: r[_k])
            assert ours == pytest.approx(theirs), key
            assert len(ours) == N_WEEKS_FLIP
