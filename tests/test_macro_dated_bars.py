"""宏观报价与板块轮动的日线按日期配对、缺值不再顶数（v0.45.663）

两个根因（用户 10-27 定「按根因修」）：
① 读 Yahoo 日线是**数第几根**：末根当「这一场」、倒数第二根当前一场、`iloc[0]` 当四场前 ⇒ 缺一根整体错位一格、不报错；
② 缺值用看起来正常的数顶上：10Y 缺填 4.5（恰好判「高利率」、宏观分 −1）、涨跌缺填 0.0（印成「持平」）。

第 0 步只读实测（2026-10-27，当时的日报存档 × 今天重拉的 Yahoo，交易日按 `bars_integrity`）：
- 08-31 补跑存的大盘涨跌 −0.58% = 08-31 / 08-27（当时 Yahoo 缺 08-28），按日期是 −0.33%——按位置读真错过一次；
- ^VIX 在 2026-09-07（劳动节）多出一根；今天的 Yahoo 在 08-03 ~ 10-26 一根不缺（08-28 已被补齐，事后看不见）；
- 09-02 起 36 个实时日的板块轮动存档逐只等于按日期重算、冷热三只全同；10-19 起宏观报价全走财政部 / Finnhub。

本文件守：
  A. `bars_integrity.session_change` / `sessions_before` / `closes_by_session`：按日期配对，缺哪根说哪根；
  B. 宏观报价（实时 + 补跑）：这一场那根缺 ⇒ 当没取到；只缺前一场 ⇒ 水平照用、涨跌 None；点名进 `quote_gaps` + WARNING；
  C. 板块轮动：同上；缓存按场次存；±50% 不再「归零」照排；逐只 / 整段失败都 WARNING（10-14 / 10-26 整表为空时一个字都没有）；
  D. 缺值不顶数：10Y / 大盘 / 美元 / 黄金缺了是 None / "unknown"；财政部与 Finnhub 不再写假 0.0；报告「大盘」标签是 1 日；
  E. 守卫：`fred_macro.py` 里不许按位置读日线、不许给 last / prev / change_pct 数字兜底（登记过的例外只许减；v0.45.669 已减到 0）。

钟：实时口径的锚定日只读 `fred_macro._realtime_as_of`（它读 `cboe_options._et_now`）——用到实时的用例一律钉它。
"""
import ast
import datetime as dt
import logging
import pathlib
import sys
import types

import pandas as pd
import pytest

import bars_integrity as bi
import fred_macro as fm

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 收集期抓真身：conftest 的 autouse `_block_same_day_macro` 默认把它换成空桩（防出网）；下面只在桩住财政部与 Finnhub 后调
_REAL_SAME_DAY = fm._same_day_macro_data
TZ = "America/New_York"
LOGGER = "alpha_hive.fred_macro"


def _d(s):
    return dt.date.fromisoformat(s)


def _frame(rows):
    """真 pandas 日线（美东时区索引，同 yfinance）。rows = [(日期, 收盘), ...]，收盘可为 NaN。"""
    closes = [c for _d_, c in rows]
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes,
                         "Volume": [1_000_000] * len(rows)},
                        index=pd.DatetimeIndex([pd.Timestamp(d, tz=TZ) for d, _c in rows]))


def test_calendar_preconditions():
    """下面的用例依赖这些日历事实——先断言，不心算。"""
    assert bi.trading_days(_d("2026-08-24"), _d("2026-09-09")) == [_d(x) for x in (
        "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31",
        "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09")]   # 09-07 劳动节
    assert _d("2025-01-09") in bi.AD_HOC_CLOSURES


# ══════════════════════════════════════════════════════════════════════════
# A. bars_integrity：按日期配对
# ══════════════════════════════════════════════════════════════════════════

class TestSessionsBefore:

    @pytest.mark.parametrize("anchor,n,want", [
        ("2026-09-04", 1, "2026-09-03"),
        ("2026-08-31", 1, "2026-08-28"),     # 跨周末
        ("2026-09-08", 1, "2026-09-04"),     # 跨劳动节
        ("2026-09-08", 4, "2026-09-01"),
        ("2025-01-10", 1, "2025-01-08"),     # 跨临时休市（卡特国葬日，规则历不知道）
    ])
    def test_counts_sessions_not_calendar_days(self, anchor, n, want):
        assert bi.sessions_before(_d(anchor), n) == _d(want)

    def test_n_must_be_positive(self):
        with pytest.raises(ValueError):
            bi.sessions_before(_d("2026-09-04"), 0)


class TestClosesBySession:

    def test_only_finite_positive_closes(self):
        h = _frame([("2026-09-01", 100.0), ("2026-09-02", float("nan")), ("2026-09-03", 0.0), ("2026-09-04", 101.0)])
        assert bi.closes_by_session(h) == {_d("2026-09-01"): 100.0, _d("2026-09-04"): 101.0}

    def test_duplicate_day_is_dropped_not_guessed(self):
        """同一天两行（时区 / 夏令时偶尔多带一根）⇒ 分不清哪根是真的 ⇒ 那天当缺。变红的变异：去掉重复判。"""
        h = _frame([("2026-09-03", 100.0), ("2026-09-04", 101.0), ("2026-09-04", 150.0)])
        assert bi.closes_by_session(h) == {_d("2026-09-03"): 100.0}

    @pytest.mark.parametrize("h", [None, _frame([])])
    def test_nothing(self, h):
        assert bi.closes_by_session(h) == {}


class TestSessionChange:

    def test_pairs_by_date(self):
        h = _frame([("2026-09-02", 98.0), ("2026-09-03", 100.0), ("2026-09-04", 103.0)])
        q = bi.session_change(h, _d("2026-09-04"), 1)
        assert q == {"last": 103.0, "prev": 100.0, "change_pct": (103.0 / 100.0 - 1) * 100,
                     "base": _d("2026-09-03"), "missing": None}

    def test_missing_previous_session_keeps_the_level_only(self):
        """08-31 补跑的实况：Yahoo 缺 08-28 ⇒ 末根前一根是 08-27。按位置 −0.58%（错），按日期：前一场缺 ⇒ 涨跌 None。
        收盘取自 ^GSPC（2026-10-27 重拉）。变红的变异：基准日改回「末根的前一根」。"""
        h = _frame([("2026-08-26", 7675.70), ("2026-08-27", 7730.99), ("2026-08-31", 7686.14)])
        q = bi.session_change(h, _d("2026-08-31"), 1)
        assert (q["last"], q["prev"], q["change_pct"]) == (7686.14, None, None)
        assert q["missing"] == "base_missing:2026-08-28"

    def test_extra_holiday_bar_is_not_the_previous_session(self):
        """^VIX 在 2026-09-07（劳动节）有一根：按位置 09-08 的前一根是它；按日期是 09-04。"""
        h = _frame([("2026-09-03", 15.0), ("2026-09-04", 16.0), ("2026-09-07", 30.0), ("2026-09-08", 17.0)])
        q = bi.session_change(h, _d("2026-09-08"), 1)
        assert (q["prev"], q["base"]) == (16.0, _d("2026-09-04"))

    def test_missing_anchor_gives_nothing(self):
        """这一场那根没出（实时：Yahoo 还没发今天那根；补跑：整根缺 D）⇒ 什么都不给，不拿前一场冒充。"""
        h = _frame([("2026-09-02", 98.0), ("2026-09-03", 100.0)])
        q = bi.session_change(h, _d("2026-09-04"), 1)
        assert (q["last"], q["prev"], q["change_pct"], q["missing"]) == (None, None, None, "anchor_missing:2026-09-04")

    def test_nan_close_on_anchor_is_missing(self):
        """2026-08-28 那种：当天成交了、Yahoo 还没填收盘（NaN）。"""
        h = _frame([("2026-09-03", 100.0), ("2026-09-04", float("nan"))])
        assert bi.session_change(h, _d("2026-09-04"), 1)["missing"] == "anchor_missing:2026-09-04"

    def test_four_sessions_back_skips_the_gap_by_date(self):
        """板块轮动的口径（这一场对往前第 4 个交易日）：中间缺一根不影响基准，基准按日期找。
        旧 `period="5d"` + `iloc[0]`：Yahoo 缺 09-02 时 5 行是 08-28 … 09-04，首行 08-28 = 往前第 5 场（错一格）。"""
        h = _frame([("2026-08-28", 90.0), ("2026-08-31", 100.0), ("2026-09-01", 101.0), ("2026-09-03", 102.0),
                    ("2026-09-04", 110.0)])
        q = bi.session_change(h, _d("2026-09-04"), 4)
        assert (q["base"], q["prev"]) == (_d("2026-08-31"), 100.0)


# ══════════════════════════════════════════════════════════════════════════
# B. 宏观报价
# ══════════════════════════════════════════════════════════════════════════

class _Tk:
    def __init__(self, hist):
        self._hist = hist

    def history(self, **kw):
        if isinstance(self._hist, Exception):
            raise self._hist
        return self._hist


def _fake_yf(frames):
    """`yf.Ticker(sym)` → 给定日线；没给的符号返回空表（= 没取到）。"""
    return types.SimpleNamespace(Ticker=lambda s: _Tk(frames.get(s, pd.DataFrame())))


@pytest.fixture
def harness(monkeypatch, stub_cboe_vix, stub_yfinance):
    """`_fetch_macro_data` 的其余外部依赖全钉住：财政部 / Finnhub（可给）、CBOE VIX（不给）、FRED（无 key）、板块轮动（空）。"""
    import cboe_vix
    monkeypatch.setattr(cboe_vix, "get_vix_observation", lambda *a, **k: {})
    monkeypatch.setattr(cboe_vix, "get_vix_observation_asof", lambda *a, **k: {})   # 补跑那支（本机 CSV 缓存热、CI 冷）
    monkeypatch.setattr(fm, "_load_fred_key", lambda: "")
    monkeypatch.setattr(fm, "_fetch_sector_rotation", lambda *a, **k: {"hot": [], "cold": [], "full": {}})
    fm.set_macro_snapshot(None)

    def run(frames, session="2026-09-04", same_day=None):
        monkeypatch.setattr(fm, "_realtime_as_of", lambda: session)
        sd = same_day or ({}, {})
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (dict(sd[0]), dict(sd[1])))
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(frames))
        return fm._fetch_macro_data()
    yield run
    fm.set_macro_snapshot(None)


#: 止于 09-04 的 5 个交易日，GLD 当日 +0.5%（rising）
GLD_OK = [("2026-08-31", 398.0), ("2026-09-01", 399.0), ("2026-09-02", 399.5), ("2026-09-03", 400.0), ("2026-09-04", 402.0)]


class TestMacroQuotesRealtime:

    def test_todays_bar_not_out_is_missing_not_yesterday(self, harness, caplog):
        """实时：Yahoo 还没发今天（09-04）那根，末根是 09-03（+3%）。旧代码拿它当今天 ⇒ 黄金「飙升」⇒ Guard risk_off +2。
        现在：GLD 当没取到、点名、WARNING；趋势 unknown（不投票）。变红的变异：按位置取末根 / 删 WARNING。"""
        stale = [("2026-08-31", 398.0), ("2026-09-01", 399.0), ("2026-09-02", 400.0), ("2026-09-03", 412.0)]
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            r = harness({"GLD": _frame(stale), "^GSPC": _frame(GLD_OK)})
        assert (r["gold_price"], r["gold_change_pct"], r["gold_trend"]) == (None, None, "unknown")
        assert r["quote_gaps"]["GLD"] == "anchor_missing:2026-09-04"
        assert r["as_of"] == "2026-09-04" and "not_on_as_of" not in r
        assert any("实时" in m and "GLD=GLD:anchor_missing:2026-09-04" in m for m in caplog.messages), caplog.messages

    def test_missing_previous_session_gives_no_change(self, harness):
        """SPX 缺 09-03：旧代码拿 09-02 当前一场（两天的涨跌冒充一天）。现在涨跌 None、趋势 unknown、点名 base_missing。"""
        spx = [("2026-08-31", 5000.0), ("2026-09-01", 5010.0), ("2026-09-02", 5020.0), ("2026-09-04", 5100.0)]
        r = harness({"^GSPC": _frame(spx), "GLD": _frame(GLD_OK)})
        assert (r["spx_change_pct"], r["market_trend"]) == (None, "unknown")
        assert r["quote_gaps"]["SPX"] == "base_missing:2026-09-03"
        assert (r["gold_change_pct"], r["gold_trend"]) == (round((402.0 / 400.0 - 1) * 100, 2), "rising")

    def test_10y_level_survives_a_missing_previous_session(self, harness):
        """10Y 缺前一场：水平照用（判利率环境、算曲线），只有涨跌未知。变红的变异：只缺前一场也当整只没取到。"""
        tnx = [("2026-09-01", 4.70), ("2026-09-02", 4.72), ("2026-09-04", 4.80)]
        r = harness({"^TNX": _frame(tnx), "GLD": _frame(GLD_OK)},
                    same_day=({"TWO": {"last": 4.20, "prev": None, "change_pct": None}}, {"TWO": "treasury_gov@2026-09-04"}))
        assert (r["treasury_10y"], r["rate_environment"]) == (4.8, "high")
        assert (r["yield_curve"], r["yield_spread"]) == ("normal", 60.0)
        assert r["quote_gaps"]["TNX"] == "base_missing:2026-09-03"

    def test_extra_holiday_bar_does_not_shift_the_base(self, harness):
        """09-07 劳动节多出的一根（形状取自 ^VIX 实测）：按位置 09-08 对它 +0.49%（rising）；按日期对 09-04 +3.5%（surging）。"""
        gld = [("2026-09-03", 99.0), ("2026-09-04", 100.0), ("2026-09-07", 103.0), ("2026-09-08", 103.5)]
        r = harness({"GLD": _frame(gld)}, session="2026-09-08")
        assert (r["gold_change_pct"], r["gold_trend"]) == (3.5, "surging")

    def test_clean_day_has_an_empty_gap_list(self, harness):
        frames = {s: _frame([(d, c * k) for d, c in GLD_OK])
                  for s, k in (("^VIX", 0.05), ("^TNX", 0.012), ("^FVX", 0.011), ("DX-Y.NYB", 0.26),
                               ("^GSPC", 13.0), ("TLT", 0.22), ("GLD", 1.0))}
        r = harness(frames)
        assert r["quote_gaps"] == {} and r["data_source"] == "yfinance"

    def test_no_session_means_no_positional_guess(self, harness):
        """判不出这批代表哪天（`_realtime_as_of` 给 None）⇒ Yahoo 的符号全部不用、点名 no_session（旧代码照样数位置）。"""
        r = harness({"GLD": _frame(GLD_OK), "^GSPC": _frame(GLD_OK)}, session=None,
                    same_day=({"TWO": {"last": 4.2, "prev": None, "change_pct": None}}, {"TWO": "treasury_gov@x"}))
        assert r["gold_trend"] == "unknown" and r["quote_gaps"]["GLD"] == "no_session"
        assert r["as_of"] is None

    def test_fetch_error_is_named(self, harness):
        r = harness({"GLD": RuntimeError("Too Many Requests"), "^GSPC": _frame(GLD_OK)})
        assert r["quote_gaps"]["GLD"] == "error:RuntimeError"


class TestMacroQuotesBackfill:

    D = "2026-08-31"

    def test_0831_spx_no_longer_pairs_with_0827(self, monkeypatch, harness):
        """08-31 补跑的实况：`_asof_history` 给出 08-26 / 08-27 / 08-31（缺 08-28）。旧代码 −0.58%（市场趋势 bear），
        现在涨跌 None、unknown，点名 base_missing:2026-08-28（D 那根在 ⇒ 不进 654 的 `not_on_as_of`）。"""
        spx = [("2026-08-26", 7675.70), ("2026-08-27", 7730.99), ("2026-08-31", 7686.14)]
        gld = [("2026-08-28", 400.0), ("2026-08-31", 399.56)]
        monkeypatch.setattr(fm, "_asof_history", lambda yf, sym, as_of: {
            "^GSPC": _frame(spx), "GLD": _frame(gld)}.get(sym))
        fm.set_macro_snapshot(self.D, {"cboe": {}})
        r = harness({})
        assert (r["spx_change_pct"], r["market_trend"]) == (None, "unknown")
        assert r["quote_gaps"]["SPX"] == "base_missing:2026-08-28" and r["not_on_as_of"] == []
        assert r["gold_change_pct"] == round((399.56 / 400.0 - 1) * 100, 2)
        assert r["as_of"] == self.D and r["as_of_mode"] == "backfill"


# ══════════════════════════════════════════════════════════════════════════
# C. 板块轮动
# ══════════════════════════════════════════════════════════════════════════

S5 = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def _etf_frames(override=None):
    """11 只 ETF：第 i 只从 100 涨到 100 + i（各不相同，冷热可排）。"""
    out = {}
    for i, etf in enumerate(sorted(fm._SECTOR_ETFS)):
        out[etf] = _frame([(d, 100.0 + i * k / 4) for k, d in enumerate(S5)])
    out.update(override or {})
    return out


@pytest.fixture
def rotation(monkeypatch):
    monkeypatch.setattr(fm, "_etf_cache", {})

    def run(frames, session="2026-09-04"):
        monkeypatch.setattr(fm, "_realtime_as_of", lambda: session)
        return fm._fetch_sector_rotation(yf_module=_fake_yf(frames))
    return run


class TestSectorRotation:

    def test_clean_window_matches_the_old_definition(self, rotation):
        """全齐时与旧定义（5 根首尾）逐位相同：round((末 / 首 − 1) × 100, 2)。"""
        r = rotation(_etf_frames())
        for i, etf in enumerate(sorted(fm._SECTOR_ETFS)):
            assert r["full"][etf][1] == round(((100.0 + i) / 100.0 - 1) * 100, 2)
        assert r["gaps"] == {} and r["as_of"] == "2026-09-04" and "not_on_as_of" not in r

    def test_missing_middle_bar_keeps_the_four_session_base(self, rotation):
        """Yahoo 缺 09-02：`period="5d"` 给的 5 行是 08-28 … 09-04。旧 `iloc[0]` 拿 08-28（往前第 5 场）⇒ +15.79%；
        按日期仍是 08-31 ⇒ +10.0%。"""
        frm = _frame([("2026-08-28", 95.0), ("2026-08-31", 100.0), ("2026-09-01", 101.0), ("2026-09-03", 102.0),
                      ("2026-09-04", 110.0)])
        r = rotation(_etf_frames({"XLK": frm}))
        assert r["full"]["XLK"][1] == 10.0 and r["gaps"] == {}

    def test_missing_base_bar_is_left_out_and_named(self, rotation, caplog):
        frm = _frame([("2026-08-28", 95.0), ("2026-09-01", 101.0), ("2026-09-02", 101.0), ("2026-09-03", 102.0),
                      ("2026-09-04", 110.0)])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            r = rotation(_etf_frames({"XLK": frm}))
        assert "XLK" not in r["full"] and r["gaps"] == {"XLK": "base_missing:2026-08-31"}
        assert any("XLK:base_missing:2026-08-31" in m for m in caplog.messages), caplog.messages

    def test_todays_bar_not_out_empties_the_table_loudly(self, rotation, caplog):
        """实时：Yahoo 还没出今天那根 ⇒ 不拿昨天为止的窗口冒充今天（旧代码照排冷热 ⇒ Guard 顺 / 逆风票是昨天的）。"""
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            r = rotation(_etf_frames(), session="2026-09-08")
        assert r["full"] == {} and r["hot"] == [] and r["cold"] == []
        assert set(r["gaps"].values()) == {"anchor_missing:2026-09-08"} and len(r["gaps"]) == 11
        assert any("11 只 ETF 没算进去" in m for m in caplog.messages), caplog.messages

    def test_implausible_move_is_left_out_not_zeroed(self, rotation):
        """±50% 以上：旧代码归零后照排进冷热（0.0 冒充持平）。现在不计、点名。"""
        frm = _frame([(d, 100.0 if k < 4 else 180.0) for k, d in enumerate(S5)])
        r = rotation(_etf_frames({"XLK": frm}))
        assert "XLK" not in r["full"] and r["gaps"] == {"XLK": "implausible:80.0"}
        assert "XLK" not in {e for e, _n, _c in r["hot"] + r["cold"]}

    def test_sentinel_base_is_left_out_and_named(self, rotation):
        frm = _frame([(d, 1.0 if k == 0 else 100.0) for k, d in enumerate(S5)])
        r = rotation(_etf_frames({"XLK": frm}))
        assert r["gaps"] == {"XLK": "base_below_5:1.0"}

    def test_fetch_errors_are_named_and_loud(self, rotation, caplog):
        """10-14 / 10-26 的形状：逐只取数失败 ⇒ 整表空。旧代码只打 debug（INFO 级日志里一个字都没有）。"""
        frames = {e: RuntimeError("Too Many Requests") for e in fm._SECTOR_ETFS}
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            r = rotation(frames)
        assert r["full"] == {} and set(r["gaps"].values()) == {"error:RuntimeError"}
        assert any("11 只 ETF 没算进去" in m for m in caplog.messages), caplog.messages

    def test_whole_section_failure_is_loud(self, rotation, monkeypatch, caplog):
        def boom(*a, **k):
            raise OSError("no threads")
        monkeypatch.setattr(fm, "ThreadPoolExecutor", boom)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            r = rotation(_etf_frames())
        assert r["full"] == {} and r["gaps"] == {"*": "error:OSError"}
        assert any("整段取数失败" in m for m in caplog.messages), caplog.messages

    def test_cache_is_per_session(self, rotation):
        """缓存按（ETF, 那一场）存：上一场的值不当这一场用；同一场的照用（正对照）。"""
        import time as _t
        fm._etf_cache[("XLK", "2026-09-03")] = (_t.time(), ("科技", 99.0))
        r = rotation(_etf_frames())
        assert r["full"]["XLK"][1] != 99.0
        fm._etf_cache.clear()
        fm._etf_cache[("XLK", "2026-09-04")] = (_t.time(), ("科技", 99.0))
        r = rotation(_etf_frames())
        assert r["full"]["XLK"][1] == 99.0

    def test_backfill_base_missing_is_not_an_as_of_miss(self, monkeypatch):
        """补跑：D 那根在、只缺基准 ⇒ 进 `gaps`，不进 654 的 `not_on_as_of`（那是「缺 D」）。"""
        D = "2026-09-04"
        good = _frame([(d, 100.0) for d in S5])
        bad = _frame([(d, 100.0) for d in S5[1:]])
        monkeypatch.setattr(fm, "_asof_history", lambda yf, sym, as_of: bad if sym == "XLK" else good)
        monkeypatch.setattr(fm, "_realtime_as_of", lambda: pytest.fail("补跑不该读实时钟"))
        r = fm._fetch_sector_rotation(types.SimpleNamespace(), as_of=D)
        assert r["gaps"] == {"XLK": "base_missing:2026-08-31"} and r["not_on_as_of"] == []
        assert r["as_of"] == D and len(r["full"]) == 10


# ══════════════════════════════════════════════════════════════════════════
# D. 缺值不顶数
# ══════════════════════════════════════════════════════════════════════════

class TestMissingIsMissing:

    def test_no_10y_is_unknown_not_4_5(self, harness):
        """旧：缺 10Y ⇒ 4.5 ⇒ rate_environment="high"、宏观分 −1、摘要「10Y 4.50%」。2Y 是真观测，照记。"""
        r = harness({"GLD": _frame(GLD_OK)},
                    same_day=({"TWO": {"last": 4.20, "prev": None, "change_pct": None}}, {"TWO": "treasury_gov@2026-09-04"}))
        assert (r["treasury_10y"], r["rate_environment"]) == (None, "unknown")
        assert (r["treasury_2y"], r["yield_spread"], r["yield_curve"]) == (4.2, None, "unknown")
        assert "10Y —" in r["summary"] and "10Y 4.50" not in r["summary"]
        assert not any("10Y" in h for h in r["macro_headwinds"])

    def test_no_index_moves_are_unknown_not_flat(self, harness):
        """旧：缺大盘 / 美元 / 黄金的涨跌 ⇒ 0.0 ⇒ neutral / stable、摘要「大盘+0.0%」。"""
        r = harness({"^TNX": _frame([(d, 4.5) for d, _c in GLD_OK])})
        assert (r["spx_change_pct"], r["market_trend"]) == (None, "unknown")
        assert r["dollar_trend"] == "unknown"
        assert (r["gold_change_pct"], r["gold_trend"]) == (None, "unknown")
        assert "大盘—" in r["summary"] and "大盘+0.0" not in r["summary"]

    def test_treasury_level_has_no_fake_change(self, monkeypatch):
        """财政部只给这一天的水平 ⇒ 涨跌未知（旧写 0.0）。"""
        import treasury_yields
        monkeypatch.setattr(treasury_yields, "get_yield_curve",
                            lambda as_of=None: {"date": "2026-09-04", "y10": 4.1, "y5": 3.9, "y2": 3.8})
        monkeypatch.setattr(fm, "_finnhub_quote", lambda sym: None)
        data, _src = _REAL_SAME_DAY()
        assert data["TNX"] == {"last": 4.1, "prev": None, "change_pct": None}

    def test_finnhub_without_previous_close_has_no_change(self, monkeypatch):
        """Finnhub 没给前收盘（pc=0）⇒ 涨跌未知（旧：prev=last、0.0 冒充持平）。"""
        import json
        import data_pipeline
        import http_gate
        monkeypatch.setattr(data_pipeline, "_get_secret", lambda k: "k")
        monkeypatch.setattr(http_gate, "urlopen_gated", lambda req, timeout=15: json.dumps({"c": 400.0, "pc": 0, "t": 1}))
        q = fm._finnhub_quote("GLD")
        assert (q["last"], q["prev"], q["change_pct"]) == (400.0, None, None)

    def test_report_label_says_one_day_and_unknown_is_not_shown_as_a_value(self):
        from report_formatters import _build_macro
        md = "\n".join(_build_macro({"data_source": "yfinance", "macro_regime": "neutral", "macro_score": 5.0,
                                     "vix": 15.0, "vix_regime": "low", "treasury_10y": None, "rate_environment": "unknown",
                                     "spx_change_pct": None, "market_trend": "unknown", "dollar_trend": "unknown"}))
        assert "| 大盘(1日) | — | 不可得 |" in md and "5日" not in md
        assert "| 10Y利率 | — | 不可得 |" in md and "| 美元 | — | 不可得 |" in md


# ══════════════════════════════════════════════════════════════════════════
# E. 守卫：fred_macro 里不许按位置读日线、不许给 last / prev / change_pct 数字兜底
# ══════════════════════════════════════════════════════════════════════════

_SRC = ROOT / "fred_macro.py"
_BAR_KEYS = {"last", "prev", "change_pct"}

#: 登记过的例外（只许减）：(所在函数, 键, 兜底值, 理由)。
#: v0.45.669 清空：唯一一条（CBOE 与 Yahoo 都没给 VIX 时的 20.0）随整份降级的兜底常量一起改成 None。
ALLOWED_NUMERIC_DEFAULTS: dict = {}


def _is_num(n):
    return isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)


def _scan(tree):
    """→ (按位置读的节点, 数字兜底 {(函数, 键, 值)}, 字典里给 last/prev/change_pct 写数字常量的节点)。"""
    positional, defaults, literal = [], set(), []
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p

    def func_of(n):
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return n.name
        return "<module>"

    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in ("iloc", "iat"):
            positional.append((n.lineno, n.attr))
        if isinstance(n, ast.Subscript):
            v = n.value
            if isinstance(v, ast.Attribute) and (v.attr == "index" or (
                    v.attr == "date" and isinstance(v.value, ast.Attribute) and v.value.attr == "index")):
                positional.append((n.lineno, "index[...]"))
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and len(n.args) == 2 and isinstance(n.args[0], ast.Constant) and n.args[0].value in _BAR_KEYS
                and _is_num(n.args[1])):
            defaults.add((func_of(n), n.args[0].value, n.args[1].value))
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in _BAR_KEYS:
                    branches = [v.body, v.orelse] if isinstance(v, ast.IfExp) else [v]
                    if any(_is_num(b) for b in branches):
                        literal.append((n.lineno, k.value))
    return positional, defaults, literal


class TestNoPositionalBarsNoNumericFill:

    def test_fred_macro_reads_no_bar_by_position(self):
        pos, _d_, _l = _scan(ast.parse(_SRC.read_text(encoding="utf-8")))
        assert pos == [], f"fred_macro.py 按位置读日线（改用 bars_integrity.session_change 按日期配对）：{pos}"

    def test_no_numeric_fill_for_bar_quantities(self):
        _p, defaults, _l = _scan(ast.parse(_SRC.read_text(encoding="utf-8")))
        assert defaults == set(ALLOWED_NUMERIC_DEFAULTS), (
            f"新增：{defaults - set(ALLOWED_NUMERIC_DEFAULTS)}；已不存在（从登记里删掉）：{set(ALLOWED_NUMERIC_DEFAULTS) - defaults}")

    def test_no_numeric_literal_for_bar_quantities(self):
        """`{"change_pct": 0.0}`（旧财政部）、`… if prev > 0 else 0.0`（旧 Finnhub）这一形。"""
        _p, _d_, literal = _scan(ast.parse(_SRC.read_text(encoding="utf-8")))
        assert literal == [], f"给 last / prev / change_pct 写了数字常量（缺就写 None）：{literal}"

    @pytest.mark.parametrize("snippet,which", [
        ("def f(h):\n    return h['Close'].iloc[-2]\n", 0),
        ("def f(h):\n    return h.index.date[-1]\n", 0),
        ("def f(h):\n    return h.index[0]\n", 0),
        ("def _fetch_macro_data(data):\n    return data.get('SPX', {}).get('change_pct', 0.0)\n", 1),
        ("def g(v):\n    return {'last': v, 'prev': v, 'change_pct': 0.0}\n", 2),
        ("def g(a, b):\n    return {'change_pct': (a / b - 1) * 100 if b > 0 else 0.0}\n", 2),
    ])
    def test_guard_has_teeth(self, snippet, which):
        """每种旧形状喂进去都要被认出来（正对照：守卫不是恒真）。"""
        assert _scan(ast.parse(snippet))[which]


# ══════════════════════════════════════════════════════════════════════════
# F. 世代边界冻结证据的判据（experiments/macro_bars_dated_equivalence_audit.py，纯函数离线测）
# ══════════════════════════════════════════════════════════════════════════

def _audit():
    sys.path.insert(0, str(ROOT / "experiments"))
    import macro_bars_dated_equivalence_audit as A
    return A


AD = "2026-10-21"                      # 往前第 4 场 = 10-15（`test_audit_constants_match_production` 断言，不心算）
AD_DAYS = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"]


def _audit_inputs(stored_shift=0.0):
    """11 只 ETF：第 i 只从 100 涨到 100 + i；存档 = 按日期算的值（+ stored_shift）。"""
    closes, full = {}, {}
    for i, etf in enumerate(sorted(fm._SECTOR_ETFS)):
        closes[etf] = {d: 100.0 + i * k / 4 for k, d in enumerate(AD_DAYS)}
        full[etf] = [fm._SECTOR_ETFS[etf], round(float(i), 2) + stored_shift]
    perf = sorted(((e, v[1]) for e, v in full.items()), key=lambda x: x[1], reverse=True)
    mc = {"as_of_mode": "realtime", "vix_source": "cboe", "data_source": "treasury+finnhub+yfinance+fred",
          "field_sources": {"GLD": "finnhub:GLD", "TNX": f"treasury_gov@{AD}"},
          "sector_rotation": {"full": full, "hot": [[e, "", v] for e, v in perf[:3]], "cold": [[e, "", v] for e, v in perf[-3:]]}}
    return mc, closes


class TestEquivalenceAudit:

    def test_audit_constants_match_production(self):
        A = _audit()
        assert set(A.SECTOR_ETFS) == set(fm._SECTOR_ETFS) and A.SESSIONS_BACK == fm.SECTOR_ROTATION_SESSIONS_BACK
        assert bi.sessions_before(_d(AD), A.SESSIONS_BACK) == _d(AD_DAYS[0])

    def test_stored_equals_dated_is_equivalent(self):
        mc, closes = _audit_inputs()
        rec = _audit().judge(AD, mc, closes, None)
        assert (rec["status"], rec["mismatches"], rec["data_source"]) == ("equivalent", {}, mc["data_source"])

    def test_stored_off_by_a_cent_is_not_proven(self):
        """存档对不上今天按日期算的值 ⇒ 当时可能错位过 ⇒ 证不出。变红的变异：容差放宽 / 不逐只比。"""
        mc, closes = _audit_inputs()
        mc["sector_rotation"]["full"]["XLK"][1] += 0.02
        rec = _audit().judge(AD, mc, closes, None)
        assert rec["status"] == "not_proven" and set(rec["mismatches"]) == {"XLK"}

    def test_hot_cold_must_match(self):
        mc, closes = _audit_inputs()
        mc["sector_rotation"]["hot"] = mc["sector_rotation"]["cold"]
        assert _audit().judge(AD, mc, closes, None)["status"] == "not_proven"

    @pytest.mark.parametrize("field,value", [("GLD", None), ("GLD", "yfinance"), ("TNX", "treasury_gov@2026-10-20"),
                                             ("VIX", "yfinance"), ("MODE", "backfill")])
    def test_quotes_through_yahoo_are_not_proven(self, field, value):
        """黄金 / 10Y 走了 Yahoo 日线、VIX 不是 CBOE、补跑 ⇒ 板块再对得上也证不出（那几样会被按日期配对改动）。"""
        mc, closes = _audit_inputs()
        if field == "VIX":
            mc["vix_source"] = value
        elif field == "MODE":
            mc["as_of_mode"] = value
        else:
            mc["field_sources"][field] = value
        assert _audit().judge(AD, mc, closes, None)["status"] == "not_proven"

    @pytest.mark.parametrize("gate,want", [({"calls": 5, "rate_limited": 5}, "equivalent_no_yfinance_data"),
                                           ({"calls": 9, "rate_limited": 5}, "not_proven"),
                                           (None, "not_proven")])
    def test_empty_table_needs_proof_that_yahoo_gave_nothing(self, gate, want):
        """10-26 形状：轮动表为空 ⇒ 只有整轮 yfinance 0 次成功响应才证得出（10-14 没有这份计数 ⇒ 证不出）。"""
        mc, closes = _audit_inputs()
        mc["sector_rotation"] = {"hot": [], "cold": [], "full": {}}
        assert _audit().judge(AD, mc, closes, gate)["status"] == want
