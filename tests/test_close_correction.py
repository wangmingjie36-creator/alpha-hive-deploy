"""收盘价校正守卫（v0.45.41）。

背景（实测，非推测）
--------------------
扫描跑在 14:00 PDT = 17:00 ET，正处盘后时段（16:00–20:00 ET）正中。
CBOE `current_price` 跟着盘后交易走，`last_trade_time` 却钉死在 16:00 收盘 ——
所以 v0.45.39 的 vintage 校验对它完全无效：判据和被污染的字段不是同一个东西。

2026-08-26 实测：CRM 当天发财报，库里 `price_at_predict` = 232.93，
官方收盘 205.62，**偏 +13.28%**。全库 1017 条中 95 条需校正。

守什么
------
1. 污染行被修正、原值留痕
2. **幂等** —— 初版用 raw 做判据，校正后重跑仍报「需校正 95」（数据没写错，
   报告在撒谎）。这条测试专门守它
3. 两源分歧 → 拒改（不猜哪个对）
4. 无来源 / 本就正确 → 一律不动
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import close_correction as cc  # noqa: E402

DATE = "2026-08-26"


@pytest.fixture
def db(tmp_path):
    def _make(rows):
        """rows: (date, ticker, price_at_predict)"""
        p = str(tmp_path / "t.db")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, date TEXT, "
                    "ticker TEXT, price_at_predict REAL)")
        con.executemany("INSERT INTO predictions (date,ticker,price_at_predict) VALUES (?,?,?)", rows)
        con.commit()
        con.close()
        return p
    return _make


def _patch_sources(monkeypatch, closes, cboe=None, prev_td=None):
    monkeypatch.setattr(cc, "official_closes", lambda t, lo, hi: closes)
    monkeypatch.setattr(cc, "cboe_prev_closes", lambda t: cboe or {})
    monkeypatch.setattr(cc, "_prev_trading_day", lambda: prev_td)


def test_after_hours_pollution_corrected(db, monkeypatch):
    """CRM 实测场景：盘后财报价 232.93 → 官方收盘 205.62。"""
    p = db([(DATE, "CRM", 232.93)])
    _patch_sources(monkeypatch, {(DATE, "CRM"): 205.62})
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    assert st["corrected"] == 1
    r = con.execute("SELECT price_at_predict, price_at_predict_raw, close_correction_source "
                    "FROM predictions").fetchone()
    con.close()
    assert r[0] == pytest.approx(205.62)
    assert r[1] == pytest.approx(232.93), "原值必须留痕"
    assert r[2] == "yfinance_close"


def test_idempotent_rerun_reports_zero(db, monkeypatch):
    """回归：初版用 raw 做判据，校正后重跑仍报「需校正 N」。

    数据其实没写错（COALESCE 护住了 raw），坏的是**报告** —— 校正完 95 条
    之后还说有 95 条待校正，看起来像什么都没发生。判据必须看当前值。
    """
    p = db([(DATE, "CRM", 232.93)])
    _patch_sources(monkeypatch, {(DATE, "CRM"): 205.62})
    con = sqlite3.connect(p)
    cc.correct(con, apply=True)
    st2 = cc.correct(con, apply=True)
    raw = con.execute("SELECT price_at_predict_raw FROM predictions").fetchone()[0]
    con.close()
    assert st2["corrected"] == 0, "重跑不该再报需校正"
    assert st2["skipped_done"] == 1
    assert raw == pytest.approx(232.93), "重跑不得覆盖 raw"


def test_two_source_dispute_refuses(db, monkeypatch):
    """两源分歧 → 不猜哪个对，拒改并记账。"""
    p = db([(DATE, "X", 100.0)])
    _patch_sources(monkeypatch, {(DATE, "X"): 105.0}, cboe={"X": 120.0}, prev_td=DATE)
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    val = con.execute("SELECT price_at_predict FROM predictions").fetchone()[0]
    con.close()
    assert st["disputed"] == 1 and st["corrected"] == 0
    assert val == pytest.approx(100.0), "分歧时必须原样不动"


def test_two_sources_agree_marks_cross_checked(db, monkeypatch):
    p = db([(DATE, "X", 100.0)])
    _patch_sources(monkeypatch, {(DATE, "X"): 105.0}, cboe={"X": 105.02}, prev_td=DATE)
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    src = con.execute("SELECT close_correction_source FROM predictions").fetchone()[0]
    con.close()
    assert st["cross_checked"] == 1 and st["corrected"] == 1
    assert src == "yfinance_close+cboe_prev"


def test_no_source_leaves_row_untouched(db, monkeypatch):
    """周日样本（已退役的 sample-accumulator 产物）无收盘价可校 —— 必须不动。"""
    p = db([("2026-04-26", "X", 100.0)])
    _patch_sources(monkeypatch, {})
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    r = con.execute("SELECT price_at_predict, close_corrected_at FROM predictions").fetchone()
    con.close()
    assert st.get("aborted") == "no_official_closes"
    assert r[0] == pytest.approx(100.0) and r[1] is None


def test_already_correct_not_touched(db, monkeypatch):
    p = db([(DATE, "X", 105.0)])
    _patch_sources(monkeypatch, {(DATE, "X"): 105.0})
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    r = con.execute("SELECT close_corrected_at FROM predictions").fetchone()[0]
    con.close()
    assert st["already_ok"] == 1 and st["corrected"] == 0
    assert r is None, "本就正确的行不该被打上校正标记"


def test_dry_run_writes_nothing(db, monkeypatch):
    p = db([(DATE, "CRM", 232.93)])
    _patch_sources(monkeypatch, {(DATE, "CRM"): 205.62})
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=False)
    val = con.execute("SELECT price_at_predict FROM predictions").fetchone()[0]
    con.close()
    assert st["corrected"] == 1
    assert val == pytest.approx(232.93), "dry-run 不得写库"


def test_ensure_columns_idempotent(db):
    p = db([(DATE, "X", 1.0)])
    con = sqlite3.connect(p)
    cc.ensure_columns(con)
    cc.ensure_columns(con)      # 二次调用不得抛
    cols = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
    con.close()
    assert {"price_at_predict_raw", "close_corrected_at", "close_correction_source"} <= cols


def test_raw_survives_a_second_correction(db, monkeypatch):
    """二次校正时 raw 必须仍是**最初**的原值，不能被上一轮的结果顶掉。

    场景真实存在：官方收盘被复权修订、或校正口径调整后重跑。
    上一条幂等测试碰不到这里 —— 它第二轮直接 continue，UPDATE 根本没执行。
    """
    p = db([(DATE, "CRM", 232.93)])
    _patch_sources(monkeypatch, {(DATE, "CRM"): 205.62})
    con = sqlite3.connect(p)
    cc.correct(con, apply=True)

    _patch_sources(monkeypatch, {(DATE, "CRM"): 200.00})   # 收盘价被修订
    st = cc.correct(con, apply=True)
    cur, raw = con.execute("SELECT price_at_predict, price_at_predict_raw "
                           "FROM predictions").fetchone()
    con.close()
    assert st["corrected"] == 1
    assert cur == pytest.approx(200.00)
    assert raw == pytest.approx(232.93), \
        "raw 被第二轮覆盖了 —— 最初的原值丢失，留痕失效"
