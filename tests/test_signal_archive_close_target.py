"""
signal_archive 前瞻收益的终点价列（v0.45.321）

背景：`load_panel()` 曾用 `f"price_{horizon}"` 当终点价，docstring 称之为「纯价格变动」
（对照路径依赖的 return_t7）。那是 ic_diagnostics 在 v0.45.19 已更正过的**同一个误解**：
`price_t7` 存的是 `_simulate_trade_path` 的 `exit_price`，触 SL/TP 即钉在档位上。
2026-09-23 生产快照：2026-05 起 price_t7 100% 等于 exit_price；与 close_t7 相差 >0.01
的行每月 34%~76%。`analyze()` 的 🟢/🟡/⚪ 判定、噪音地板、固定/时变分解、稳定性
全部是对着截断收益算的。修复后 🟢 由 5 个变 8 个（`price.momentum_5d` / `catalyst.count`
掉出），详见 CHANGELOG v0.45.321。

本文件的核心夹具**故意让两列给出相反的 IC 符号**：信号高的票最终收涨，但途中先跌穿止损
（离场价 −5%）；信号低的票最终收跌，但途中先冲过止盈（离场价 +10%）。
旧实现读 price_t7 ⇒ IC 为负；读 close_t7 ⇒ IC 为正。符号不同，回退即红。
"""

import datetime
import os
import random
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ic_diagnostics as icd
import ic_rerun_readiness as irr
import signal_archive as sa

SIG = "insider.filings"          # 叶子信号：不读系统输出
TICKERS = [f"T{i}" for i in range(8)]


def _bdays(start: str, n: int):
    d = datetime.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


DAYS = _bdays("2026-06-01", 24)   # ≥16 天才过 split_stability 的门槛


def _create_predictions(con, extra_cols: str = ""):
    con.execute(f"""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        price_at_predict REAL, price_t7 REAL, return_t7 REAL, close_t7 REAL,
        exit_price REAL, exit_reason TEXT, checked_t7 INTEGER DEFAULT 0{extra_cols})""")


def _build_reversal_db(tmp_path, close_equals_exit: bool = False):
    """合成库：close_t7 与信号正相关，price_t7（= exit_price）与信号负相关。

    close_equals_exit=True：模拟「有人把离场价写进了 close_t7」的数据层回归 ——
    列名没变、代码没变，只有截断指纹会跳。
    """
    rng = random.Random(321)
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    _create_predictions(con)
    con.commit()
    con.close()
    sa.ensure_schema(db)

    preds, arch = [], []
    for d in DAYS:
        for tk in TICKERS:
            x = rng.gauss(0, 1)
            ret_close = 2.0 * x + rng.gauss(0, 0.3)            # 收盘：跟着信号走
            close = 100.0 * (1 + ret_close / 100.0)
            if ret_close > 0:
                reason, exit_px = "SL", 95.0                   # 先跌穿止损，再收涨
            else:
                reason, exit_px = "TP", 110.0                  # 先冲过止盈，再收跌
            if close_equals_exit:
                close = exit_px
            preds.append((d, tk, 100.0, exit_px, exit_px - 100.0, close, exit_px, reason, 1))
            arch.append((d, tk, SIG, x))
    with sqlite3.connect(db) as c:
        c.executemany(
            "INSERT INTO predictions (date,ticker,price_at_predict,price_t7,return_t7,"
            "close_t7,exit_price,exit_reason,checked_t7) VALUES (?,?,?,?,?,?,?,?,?)", preds)
        c.executemany(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)",
                      arch)
    return db


@pytest.fixture
def no_boundaries(monkeypatch):
    """边界表清空 —— 本文件测的是目标列，不能让真实世代表的日期切掉夹具样本。"""
    monkeypatch.setattr(irr, "_COHORT_HISTORY", [])


def _mean_ic_against(db, col: str) -> float:
    """夹具自检：直接按指定列算信号的平均日度 IC（不经 load_panel）。"""
    by_day = {}
    with sqlite3.connect(db) as c:
        rows = c.execute(
            f"SELECT a.date, a.value, (p.{col} - p.price_at_predict) / p.price_at_predict "
            f"FROM {sa.TABLE} a JOIN predictions p ON p.date=a.date AND p.ticker=a.ticker "
            f"WHERE a.signal=?", (SIG,)).fetchall()
    for d, v, r in rows:
        by_day.setdefault(d, []).append((v, r))
    s = icd._ic_series_from_pairs(by_day)
    return sum(s.values()) / len(s)


class TestFixtureDiscriminates:
    """没有这一组，下面的「IC 为正」可能只是夹具本来就分不出两列。"""

    def test_price_t7_and_close_t7_give_opposite_ic_signs(self, tmp_path):
        db = _build_reversal_db(tmp_path)
        assert _mean_ic_against(db, "close_t7") > 0.5
        assert _mean_ic_against(db, "price_t7") < -0.5


class TestAnalyzeUsesClose:
    def test_analyze_ic_sign_follows_close_t7(self, tmp_path, no_boundaries):
        """回退到 price_t7 时这里是 −0.8 左右 —— 符号相反，不是数值漂移。"""
        res = sa.analyze(_build_reversal_db(tmp_path), draws=30)
        assert res, "夹具面板为空 —— 断言全是空转"
        rows, _floor, _gens = res
        row = next(r for r in rows if r["signal"] == SIG)
        assert row["n_samples"] == len(DAYS) * len(TICKERS)
        assert row["daily_ic"] > 0.5, (
            f"daily_ic={row['daily_ic']:+.3f}：前瞻收益没用 close_t7"
            f"（price_t7 是 SL/TP 离场价，见 ic_diagnostics.FORWARD_CLOSE_COL）")

    def test_load_panel_return_value_is_close_based(self, tmp_path):
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        _create_predictions(con)
        # 离场价 110（止盈档）、收盘 105、return_t7 是钳位值 —— 三者各不相同
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,price_t7,return_t7,"
                    "close_t7,exit_price,exit_reason,checked_t7) VALUES "
                    "('2026-06-01','A',100.0,110.0,9.945,105.0,110.0,'TP',1)")
        con.commit()
        con.close()
        sa.ensure_schema(db)
        with sqlite3.connect(db) as c:
            c.execute(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) "
                      f"VALUES ('2026-06-01','A',?,1.0)", (SIG,))
        (v, ret), = sa.load_panel(db, "t7", min_width=1)[SIG]["2026-06-01"]
        assert ret == pytest.approx(5.0), "应为 (105−100)/100，不是离场价 110 或 return_t7"

    def test_rows_without_price_t7_are_kept(self, tmp_path):
        """生产快照有 8 行 checked_t7=1、price_t7 为空、close_t7 已有 —— 样本成员跟终点列走。"""
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        _create_predictions(con)
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,close_t7,checked_t7)"
                    " VALUES ('2026-06-01','A',100.0,103.0,1)")
        con.commit()
        con.close()
        sa.ensure_schema(db)
        with sqlite3.connect(db) as c:
            c.execute(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) "
                      f"VALUES ('2026-06-01','A',?,1.0)", (SIG,))
        assert sa.load_panel(db, "t7", min_width=1)[SIG]["2026-06-01"][0][1] == \
            pytest.approx(3.0)


class TestHorizonRegistry:
    def test_t7_is_close_not_exit(self):
        assert icd.FORWARD_CLOSE_COL["t7"] == "close_t7"

    def test_t30_uses_price_t30_without_close_t30_column(self, tmp_path):
        """t30 没有 close_t30 列：price_t30 本身就是收盘价（SL/TP 路径模拟只作用于 t7）。"""
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            price_at_predict REAL, price_t30 REAL, checked_t30 INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,price_t30,checked_t30)"
                    " VALUES ('2026-06-01','A',100.0,120.0,1)")
        con.commit()
        con.close()
        sa.ensure_schema(db)
        with sqlite3.connect(db) as c:
            c.execute(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) "
                      f"VALUES ('2026-06-01','A',?,1.0)", (SIG,))
        assert sa.load_panel(db, "t30", min_width=1)[SIG]["2026-06-01"][0][1] == \
            pytest.approx(20.0)

    def test_unregistered_horizon_raises_instead_of_guessing(self, tmp_path):
        with pytest.raises(ValueError, match="FORWARD_CLOSE_COL"):
            sa.load_panel(tmp_path / "nope.db", "t1")


class TestTruncationFingerprint:
    """数据层观测点：列名对了，但列里装的是离场价 —— 只有截断指纹会跳。"""

    def test_warns_when_close_column_holds_exit_prices(self, tmp_path, capsys):
        sa.load_panel(_build_reversal_db(tmp_path, close_equals_exit=True), "t7", 5)
        err = capsys.readouterr().err
        assert "close_t7" in err and "exit_price" in err

    def test_silent_on_clean_close_column(self, tmp_path, capsys):
        """负对照：真收盘价不能误报，否则告警会被当噪音无视。"""
        sa.load_panel(_build_reversal_db(tmp_path), "t7", 5)
        assert "exit_price" not in capsys.readouterr().err
