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
    monkeypatch.setattr(cc, "cboe_official_closes", lambda t, **k: cboe or {})
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
                   cboe={"X": {DATE: 120.0}}, prev_td=DATE)
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    val = con.execute("SELECT price_at_predict FROM predictions").fetchone()[0]
    con.close()
    assert st["disputed"] == 1 and st["corrected"] == 0
    assert val == pytest.approx(100.0), "分歧时必须原样不动"


def test_two_sources_agree_marks_cross_checked(db, monkeypatch):
    p = db([(DATE, "X", 100.0)])
    _patch_sources(monkeypatch, {(DATE, "X"): 105.0},
                   cboe={"X": {DATE: 105.02}}, prev_td=DATE)
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    src = con.execute("SELECT close_correction_source FROM predictions").fetchone()[0]
    con.close()
    assert st["cross_checked"] == 1 and st["corrected"] == 1
    assert src == "yfinance_close+cboe_close"


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
                   cboe={"X": {"2026-08-25": 126.0}}, prev_td=DATE)
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
# `close` 归属哪个交易日
# ══════════════════════════════════════════════════════════════════
# v0.45.243 删掉了 `_session_of_close`（按 CDN timestamp 推）：它把盘中文件的 `close`
# （= 实时价）归到上一交易日。原先那条「盘中 → 前一交易日」的参数是**推断**，
# 从未实拉验证过；2026-09-14 10:55 ET 实拉证伪。归属判据现由 last_trade_time 自述，
# 真实值用例见 tests/test_stale_intraday_consumers.py。


# ══════════════════════════════════════════════════════════════════
# 二次检查发现的三个 bug（v0.45.49）
# ══════════════════════════════════════════════════════════════════

def test_zero_close_is_missing_not_a_price(db, monkeypatch):
    """🔴 0 不是「零元」，是**没有这个价**。

    初版 `official_closes` 只滤 `None`/`NaN`，0 会一路当成合法收盘价流到
    `current / truth`，直接 ZeroDivisionError（构造检验确认）。
    与 v0.45.42「缺失值不许冒充 0」同一条原则。
    """
    p = db([(DATE, "X", 100.0)])
    _patch_sources(monkeypatch, {(DATE, "X"): 0.0})
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)          # 不得抛异常
    val = con.execute("SELECT price_at_predict FROM predictions").fetchone()[0]
    con.close()
    assert st["corrected"] == 0
    assert val == pytest.approx(100.0), "0 被当成了收盘价写进库"


def test_official_closes_filters_zero(monkeypatch):
    """同一条原则守在取数层：0 不该进 closes 表。"""
    import types
    import pandas as pd

    idx = pd.to_datetime(["2026-08-26"])
    fake = pd.DataFrame({"A": [0.0], "B": [10.0]}, index=idx)
    mod = types.SimpleNamespace(download=lambda *a, **k: {"Close": fake})
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    got = cc.official_closes(["A", "B"], "2026-08-26", "2026-08-26")
    assert ("2026-08-26", "B") in got
    assert ("2026-08-26", "A") not in got, "0 值进了 closes 表"


def test_dispute_not_reported_when_nothing_to_correct(db, monkeypatch):
    """🟠 先判「要不要动」，再做交叉印证。

    反过来的话，一条**本就正确**的行遇到 CBOE 分歧会被打出「拒绝校正」
    警告并计入 disputed —— 可它压根没有待校正的内容，那条警告是假的。
    """
    p = db([(DATE, "X", 105.0)])              # 已等于官方收盘
    _patch_sources(monkeypatch, {(DATE, "X"): 105.0},
                   cboe={"X": {DATE: 130.0}}, prev_td=DATE)
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    con.close()
    assert st["disputed"] == 0, "对无需校正的行报了假分歧"
    assert st["already_ok"] == 1


def test_download_range_padded_for_non_trading_start(monkeypatch):
    """🟠 下载区间起点必须往前垫，否则非交易日样本回退不到前一交易日。

    实测触发条件：`--since 2026-03-01`（周日）。全量跑靠「最早预测日 2/27
    早于最早周日 3/01」侥幸安全，不能依赖。
    """
    import types
    import pandas as pd

    seen = {}

    def _dl(tk, start=None, end=None, **k):
        seen["start"] = start
        return {"Close": pd.DataFrame({"X": [1.0]}, index=pd.to_datetime(["2026-03-02"]))}

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=_dl))
    cc.official_closes(["X"], "2026-03-01", "2026-03-02")
    assert seen["start"] < "2026-03-01", f"起点未前垫：{seen['start']}"


# ══════════════════════════════════════════════════════════════════
# Twelve Data 兜底（v0.45.257）—— yfinance 缺覆盖时的独立配额补源
# ══════════════════════════════════════════════════════════════════
# 触发实况：2026-09-15 yfinance 批量下载 52/52 只全 YFRateLimitError（429），
# `correct()` 因 `official_closes` 返回空表而以 no_official_closes 中止。
# 这里守的是「缺覆盖时该不该补、补的范围对不对、配置不了时会不会安静跳过」。

class TestTwelveDataFallback:
    def test_totally_absent_when_not_configured(self, monkeypatch):
        """未配置 key（`is_configured()` False）→ 安静返回空表，不调 fetch_bars。

        这也是测试环境里的默认状态：conftest 的 `_block_same_day_macro`
        （autouse）把 `twelve_data.api_key` 恒置空，本条同时验证了那道闸接得上。
        """
        import twelve_data as td
        calls = []
        monkeypatch.setattr(td, "fetch_bars", lambda *a, **k: calls.append(1))
        assert cc._twelve_data_closes(["X"], "2026-08-26", "2026-08-28") == {}
        assert not calls, "未配置时不该发出任何请求"

    def test_fetches_and_windows_correctly(self, monkeypatch):
        """裁到 [lo, hi]；窗口外的行、坏值行都不进表。"""
        import twelve_data as td
        monkeypatch.setattr(td, "api_key", lambda: "k")
        rows = [
            {"date": "2026-08-24", "close": 999.0, "vol": 1},   # 窗口外（早）
            {"date": "2026-08-26", "close": 100.0, "vol": 1},
            {"date": "2026-08-27", "close": None, "vol": 1},    # 坏值：None
            {"date": "2026-08-28", "close": 0.0, "vol": 1},     # 坏值：0
            {"date": "2026-08-29", "close": 102.0, "vol": 1},   # 窗口外（晚，hi=08-28）
        ]
        monkeypatch.setattr(td, "fetch_bars", lambda t, days, end_date: rows)
        got = cc._twelve_data_closes(["X"], "2026-08-26", "2026-08-28")
        assert got == {("2026-08-26", "X"): 100.0}

    def test_fetch_bars_returns_none_is_skipped_not_crashed(self, monkeypatch):
        import twelve_data as td
        monkeypatch.setattr(td, "api_key", lambda: "k")
        monkeypatch.setattr(td, "fetch_bars", lambda *a, **k: None)
        assert cc._twelve_data_closes(["X", "Y"], "2026-08-26", "2026-08-28") == {}

    def test_end_date_and_window_size_passed_through(self, monkeypatch):
        """`days` 必须够宽覆盖 [lo, hi]（含缓冲），`end_date` 必须是 hi。"""
        import twelve_data as td
        monkeypatch.setattr(td, "api_key", lambda: "k")
        seen = {}

        def _fb(t, days, end_date):
            seen["days"], seen["end_date"] = days, end_date
            return []
        monkeypatch.setattr(td, "fetch_bars", _fb)
        cc._twelve_data_closes(["X"], "2026-08-01", "2026-08-31")
        assert seen["end_date"] == "2026-08-31"
        assert seen["days"] >= 30, "跨度 30 天的窗口，days 不能比跨度还窄"


class TestOfficialClosesUsesTwelveDataFallback:
    def test_yfinance_total_failure_recovered_by_twelve_data(self, monkeypatch):
        """2026-09-15 实况：批量下载整体抛异常（429）→ 全部标的改走 Twelve Data。"""
        import types
        def _dl(*a, **k):
            raise RuntimeError("YFRateLimitError: Too Many Requests")
        monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=_dl))
        monkeypatch.setattr(cc, "_twelve_data_closes",
                            lambda tickers, lo, hi: {(hi, t): 42.0 for t in tickers})
        got = cc.official_closes(["A", "B"], "2026-08-26", "2026-08-28")
        assert got == {("2026-08-28", "A"): 42.0, ("2026-08-28", "B"): 42.0}

    def test_partial_yfinance_coverage_only_backfills_missing_tickers(self, monkeypatch):
        """A 有数据、B 没有 → Twelve Data 只被问 B，不重复问 A（省配额）。"""
        import types
        import pandas as pd
        idx = pd.to_datetime(["2026-08-26"])
        fake = pd.DataFrame({"A": [100.0]}, index=idx)   # 只有 A，B 缺席
        monkeypatch.setitem(sys.modules, "yfinance",
                            types.SimpleNamespace(download=lambda *a, **k: {"Close": fake}))
        seen = {}

        def _td_fallback(tickers, lo, hi):
            seen["tickers"] = list(tickers)
            return {("2026-08-26", "B"): 200.0}
        monkeypatch.setattr(cc, "_twelve_data_closes", _td_fallback)
        got = cc.official_closes(["A", "B"], "2026-08-26", "2026-08-26")
        assert seen["tickers"] == ["B"], "A 已有覆盖，不该再问 Twelve Data"
        assert got == {("2026-08-26", "A"): 100.0, ("2026-08-26", "B"): 200.0}

    def test_full_yfinance_coverage_skips_twelve_data_entirely(self, monkeypatch):
        """yfinance 全覆盖时**不调用** Twelve Data——没有缺口就不该多花配额。"""
        import types
        import pandas as pd
        idx = pd.to_datetime(["2026-08-26"])
        fake = pd.DataFrame({"A": [100.0], "B": [200.0]}, index=idx)
        monkeypatch.setitem(sys.modules, "yfinance",
                            types.SimpleNamespace(download=lambda *a, **k: {"Close": fake}))
        called = []
        monkeypatch.setattr(cc, "_twelve_data_closes", lambda *a, **k: called.append(1) or {})
        cc.official_closes(["A", "B"], "2026-08-26", "2026-08-26")
        assert not called

    def test_both_sources_fail_still_returns_empty_not_crash(self, monkeypatch):
        """yfinance 抛异常 + Twelve Data 也拿不到 → 空表，`correct()` 据此走 no_official_closes。"""
        import types
        monkeypatch.setitem(sys.modules, "yfinance",
                            types.SimpleNamespace(download=lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("429"))))
        monkeypatch.setattr(cc, "_twelve_data_closes", lambda *a, **k: {})
        assert cc.official_closes(["A"], "2026-08-26", "2026-08-26") == {}


def test_correct_recovers_when_yfinance_down_but_twelve_data_up(db, monkeypatch):
    """端到端：yfinance 整体 429 时不再一律 no_official_closes——Twelve Data 能救回来。"""
    import types
    p = db([(DATE, "CRM", 232.93)])
    monkeypatch.setitem(sys.modules, "yfinance",
                        types.SimpleNamespace(download=lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("YFRateLimitError"))))
    monkeypatch.setattr(cc, "_twelve_data_closes", lambda tickers, lo, hi: {(DATE, "CRM"): 205.62})
    monkeypatch.setattr(cc, "cboe_official_closes", lambda t, **k: {})
    monkeypatch.setattr(cc, "_prev_trading_day", lambda: None)
    con = sqlite3.connect(p)
    st = cc.correct(con, apply=True)
    val, = con.execute("SELECT price_at_predict FROM predictions").fetchone()
    con.close()
    assert st.get("aborted") is None
    assert st["corrected"] == 1 and val == pytest.approx(205.62)
