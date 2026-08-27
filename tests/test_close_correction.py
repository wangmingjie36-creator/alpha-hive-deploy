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
    monkeypatch.setattr(cc, "cboe_official_closes", lambda t: cboe or {})
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
    _patch_sources(monkeypatch, {(DATE, "X"): 105.0},
                   cboe={"X": (DATE, 120.0)}, prev_td=DATE)
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    val = con.execute("SELECT price_at_predict FROM predictions").fetchone()[0]
    con.close()
    assert st["disputed"] == 1 and st["corrected"] == 0
    assert val == pytest.approx(100.0), "分歧时必须原样不动"


def test_two_sources_agree_marks_cross_checked(db, monkeypatch):
    p = db([(DATE, "X", 100.0)])
    _patch_sources(monkeypatch, {(DATE, "X"): 105.0},
                   cboe={"X": (DATE, 105.02)}, prev_td=DATE)
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


# ══════════════════════════════════════════════════════════════════
# 非交易日样本：取前一交易日收盘（v0.45.47）
# ══════════════════════════════════════════════════════════════════

SUN = "2026-04-26"      # 周日；已退役的 sample-accumulator 的产物
FRI = "2026-04-24"


def test_trading_day_missing_close_does_not_fall_back(db, monkeypatch):
    """核心不变式：**交易日缺数就是缺数，不许回退到更早的收盘。**

    yfinance 偶发缺一天时回退会静默把前一日收盘当成当日收盘 ——
    正是本工具要治的那种污染，方向还反了。
    """
    p = db([("2026-04-23", "X", 100.0)])          # 周四，交易日
    _patch_sources(monkeypatch, {("2026-04-22", "X"): 90.0})   # 只有前一天有数
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    val, mark = con.execute("SELECT price_at_predict, close_corrected_at "
                            "FROM predictions").fetchone()
    con.close()
    assert st["no_source"] == 1 and st["corrected"] == 0
    assert val == pytest.approx(100.0) and mark is None, "交易日发生了静默回退"


def test_non_trading_day_uses_prior_trading_close(db, monkeypatch):
    """周日样本：取上周五收盘 —— 那正是扫描当时能拿到的最新价。

    实测 CRWD：库存 448.13 是 2026-07-02 四比一拆股**前**的未复权价，
    而 close_t7 用的是复权序列，两边口径不一致才产出垃圾收益。
    """
    p = db([(SUN, "CRWD", 448.13)])
    _patch_sources(monkeypatch, {(FRI, "CRWD"): 112.03})
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    val, src = con.execute("SELECT price_at_predict, close_correction_source "
                           "FROM predictions").fetchone()
    con.close()
    assert st["corrected"] == 1 and st["prior_close_used"] == 1
    assert val == pytest.approx(112.03)
    assert src == f"yfinance_close@{FRI}", \
        "必须如实记下取自哪一天，否则看起来像当日收盘"


def test_calendar_unavailable_treated_as_trading_day(monkeypatch):
    """判不了是不是交易日 → 按交易日处理（严格方向，不回退）。"""
    import builtins
    real = builtins.__import__

    def _boom(name, *a, **k):
        if name == "is_trading_day":
            raise ImportError("simulated")
        return real(name, *a, **k)

    cc._TRADING_DAY_CACHE.clear()
    monkeypatch.setattr(builtins, "__import__", _boom)
    assert cc._is_trading_date(SUN) is True
    got, when = cc._resolve_close({(FRI, "X"): 1.0}, [FRI], SUN, "X")
    assert (got, when) == (None, None), "日历不可用时不该回退"
    cc._TRADING_DAY_CACHE.clear()


def test_cboe_price_from_another_date_is_not_used(db, monkeypatch):
    """回归：CBOE 的 `prev_day_close` 属于**它自己 vintage 日**的前一交易日，
    不是「相对今天的前一交易日」。

    初版直接拿它印证 `prev_td` 那天的行。2026-08-27 盘前实测：CDN 文件仍是
    8/26 vintage → `prev_day_close` 指 8/25，却被用来印证 8/26。
    30 只里 24 只「印证通过」只是因为相邻两天收盘通常差不到 0.2%，
    只有 T（0.39%）与 TMO（0.88%）露馅。**比错日子的印证提供的是假信心。**

    构造：CBOE 那个价属于 8/25，本行是 8/26 且两者差 20% —— 若仍被拿去印证，
    必然判成分歧而拒改；正确行为是**忽略它**，照常按 yfinance 校正。
    """
    p = db([(DATE, "X", 100.0)])
    _patch_sources(monkeypatch, {(DATE, "X"): 105.0},
                   cboe={"X": ("2026-08-25", 126.0)}, prev_td=DATE)
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    val, src = con.execute("SELECT price_at_predict, close_correction_source "
                           "FROM predictions").fetchone()
    con.close()
    assert st["disputed"] == 0, "拿了别的日子的价去印证"
    assert st["cross_checked"] == 0
    assert st["corrected"] == 1 and val == pytest.approx(105.0)
    assert src == "yfinance_close"


# ══════════════════════════════════════════════════════════════════
# `close` 归属哪个交易日 —— 用 2026-08-27 实拉的真实 payload 钉死
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("cdn_ts_utc,expect_session,note", [
    # NVDA：08-27 08:31 ET（**盘前**）→ close=209.66 应属 8/26
    ("2026-08-27 12:31:00", "2026-08-26", "盘前的新文件"),
    # TMO：08-26 21:18 ET（**盘后**）→ close=633.71 应属 8/26
    ("2026-08-27 01:18:00", "2026-08-26", "盘后的当场文件"),
    # 盘中生成 → 当日尚未收盘，close 只能属前一交易日
    ("2026-08-27 17:00:00", "2026-08-26", "盘中"),
    # 周日生成 → 上个交易日是周五
    ("2026-08-30 12:00:00", "2026-08-28", "周日"),
])
def test_session_of_close_pinned_by_real_payloads(cdn_ts_utc, expect_session, note):
    """`close` = 文件生成时刻「最近一个已收盘交易日」的官方收盘价。

    2026-08-27 实拉四只标的验证：

        NVDA 文件 08-27 08:31 ET（盘前）close=209.66 = 8/26 收盘 ✓
        TMO  文件 08-26 21:18 ET（盘后）close=633.71 = 8/26 收盘 ✓

    ⚠️ 刻意不用 `last_trade_time` 定这个日期 —— 盘前它仍停在上一场的最后成交，
    分不清「8/27 盘前的新文件」和「8/26 的旧文件」。本条守的就是这个区分。
    """
    assert cc._session_of_close(cdn_ts_utc) == expect_session, note


def test_session_of_close_unparseable_returns_none():
    """推不出就不做交叉印证 —— 不猜。"""
    for bad in ("", "not-a-timestamp", "2026-13-99 00:00:00"):
        assert cc._session_of_close(bad) is None
