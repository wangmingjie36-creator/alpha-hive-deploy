"""v0.45.652：yfinance 日线的唯一取数入口 `yf_daily`——取到即去掉「公司行动空行」，别处不许直调 `.history(`。
v0.45.658（652 二次检查）：`yf.download` 也收进来（还原成 actions=False 的形状）、带日期窗口的 history 读者迁入、末段按「最近一场
已收盘」判、首段 / 末段按连续段分档、守卫按每个调用节点核（不再按函数折叠）、Guard 世代印记出错路径也写。

1. 入口本身：按行号删、末行标志、计数、不认识的形状原样返回、未登记的调用点名直接拒。
2. 真 yfinance 1.2.0 代码路径（假 Yahoo 响应，不联网）：钉住「空行只在首行 / 末行」「`start=` 截首行」「`actions=False` 连末行一起
   静默删」——入口的设计建立在这几条上，yfinance 换了行为这里先红。
3. 调用点行为：板块轮动（进分的那条）、Rival 回退、补跑宏观、HV 新旧逐位对拍。
4. 末行策略逐点登记：用末根当今天的调用点必须读第二个返回值；按日期用整段的必须明确不读。
5. 单一入口守卫：全仓 `.history(` 调用只许在 `yf_daily.py` 与登记的例外里（双向：多一处红、例外失效也红），带有牙自证。
6. 告警接线：scan_timing `yf_daily` 计数 → 摘要行 → alert_manager P2。
7. 世代边界登记（用户 2026-10-25 定「边界 10-19 + 等价判据」，同 v0.45.639）。
"""
from __future__ import annotations

import ast
import collections
import datetime as dt
import functools
import json
import sys
import types
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yf_daily  # noqa: E402

TZ = "America/New_York"
_ET = ZoneInfo(TZ)
_V = "v0.45.652"
_MARK = "macro_event_rows_checked"
_V658 = "v0.45.658"
_MARK658 = "yf_daily_download_checked"
#: 夹具的「此刻」：2026-10-27 收盘后（最近一场已收盘 = 10-27）。读钟只有 `yf_daily._now_et` 一处，这里钉住
_AFTER_CLOSE = dt.datetime(2026, 10, 27, 17, 0, tzinfo=_ET)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(yf_daily, "_now_et", lambda: _AFTER_CLOSE)


def _at(monkeypatch, *args):
    monkeypatch.setattr(yf_daily, "_now_et", lambda: dt.datetime(*args, tzinfo=_ET))


# ══════════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════════

def _frame(days, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, len(days))))
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=TZ) for d in days])
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                         "Volume": rng.integers(500_000, 2_000_000, len(days)).astype("int64"),
                         "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)


def _with_event(df, where, day, *, col="Dividends", value=0.5):
    """生产形状的公司行动空行：开高低收 NaN、量 0、事件列非零（yfinance 1.2.0 `safe_merge_dfs` 补出来的样子）。"""
    row = pd.DataFrame({"Open": [np.nan], "High": [np.nan], "Low": [np.nan], "Close": [np.nan], "Volume": [0],
                        "Dividends": [0.0], "Stock Splits": [0.0]}, index=[pd.Timestamp(day, tz=TZ)])
    row[col] = value
    out = pd.concat([row, df] if where == "first" else [df, row])
    out["Volume"] = out["Volume"].astype("int64")
    return out


def _bdays(start, end):
    return [d.date().isoformat() for d in pd.bdate_range(start, end)]


class _Tk:
    """`yf.Ticker(...)` 的同形桩：记下请求参数，返回给定的日线。"""

    def __init__(self, hist, ticker="ZZTK"):
        self.ticker, self._hist, self.requests = ticker, hist, []

    def history(self, **kw):
        self.requests.append(kw)
        return self._hist


DAYS = _bdays("2026-09-22", "2026-10-21")


# ══════════════════════════════════════════════════════════════════════════
# 1. 入口本身
# ══════════════════════════════════════════════════════════════════════════

class TestEntry:

    def test_clean_frame_is_returned_untouched(self):
        base = _frame(DAYS)
        tk = _Tk(base)
        out, last = yf_daily.history(tk, source="options_hv", period="1y", interval="1d")
        assert out is base and last is False
        assert tk.requests == [{"period": "1y", "interval": "1d"}]          # 请求参数原样转交
        st = yf_daily.stats()
        assert st["calls"] == 1 and st["dropped"] == 0 and st["by_source"]["options_hv"]["calls"] == 1

    def test_first_row_blank_is_dropped_and_only_counted(self):
        base = _frame(DAYS)
        out, last = yf_daily.history(_Tk(_with_event(base, "first", "2026-09-21")), source="sector_rotation", period="5d")
        assert last is False and out.equals(base)
        st = yf_daily.stats()
        assert (st["dropped"], st["first_row"], st["last_row"], st["items_total"]) == (1, 1, 0, 0)

    @pytest.mark.parametrize("col", ["Dividends", "Stock Splits"])
    def test_last_row_blank_is_dropped_flagged_and_itemised(self, col):
        base = _frame(DAYS)
        out, last = yf_daily.history(_Tk(_with_event(base, "last", "2026-10-22", col=col), "META"),
                                     source="sector_rotation", period="5d")
        assert last is True and out.equals(base)
        st = yf_daily.stats()
        assert st["last_row"] == 1 and st["items_total"] == 1
        assert st["items"] == [{"source": "sector_rotation", "ticker": "META", "date": "2026-10-22",
                                "pos": len(base), "n": len(base) + 1, "last": True}]

    def test_drops_by_position_not_by_label(self):
        """空行时间戳与真 K 线相同：按标签删会连真的一起删。变红的变异：改成 `hist.drop(hist.index[pos])`。"""
        base = _frame(DAYS)
        dup = _with_event(base, "first", DAYS[0])
        out, last = yf_daily.history(_Tk(dup), source="options_hv", period="1y")
        assert last is False and len(out) == len(base) and out.equals(base)

    def test_lookalikes_are_left_alone(self):
        """判据是 645 的那一份：只缺收盘、量正常的真 K 线（2026-08-28 型）不是空行。"""
        base = _frame(DAYS)
        base.iloc[-1, base.columns.get_loc("Close")] = np.nan
        out, last = yf_daily.history(_Tk(base), source="options_hv", period="1y")
        assert out is base and last is False

    def test_only_blank_row_counts_as_last_not_first(self):
        single = _with_event(_frame(DAYS).iloc[0:0], "first", "2026-10-22")
        out, last = yf_daily.history(_Tk(single), source="spot_vix", period="2d")
        assert out.empty and last is True
        assert yf_daily.stats()["first_row"] == 0 and yf_daily.stats()["last_row"] == 1

    def test_non_dataframe_returned_as_is_and_counted(self):
        sentinel = object()
        out, last = yf_daily.history(_Tk(sentinel), source="options_hv", period="1y")
        assert out is sentinel and last is False and yf_daily.stats()["uninspectable"] == 1

    def test_unregistered_source_refused_before_fetching(self):
        tk = _Tk(_frame(DAYS))
        with pytest.raises(ValueError, match="未登记"):
            yf_daily.history(tk, source="somewhere_new", period="1y")
        assert tk.requests == []

    def test_fetch_errors_propagate(self):
        class _Boom:
            ticker = "X"

            def history(self, **kw):
                raise ConnectionError("down")
        with pytest.raises(ConnectionError):
            yf_daily.history(_Boom(), source="options_hv", period="1y")

    def test_detail_list_is_capped_but_the_total_is_not(self):
        base = _frame(DAYS)
        for i in range(25):
            yf_daily.history(_Tk(_with_event(base, "last", "2026-10-22"), f"T{i}"), source="macro_quotes", period="5d")
        st = yf_daily.stats()
        assert st["items_total"] == 25 and len(st["items"]) == 20 and st["by_source"]["macro_quotes"]["last_row"] == 25

    def test_stats_are_copies(self):
        yf_daily.history(_Tk(_with_event(_frame(DAYS), "last", "2026-10-22")), source="macro_quotes", period="5d")
        st = yf_daily.stats()
        st["items"].clear()
        st["by_source"]["macro_quotes"]["calls"] = 99
        assert len(yf_daily.stats()["items"]) == 1 and yf_daily.stats()["by_source"]["macro_quotes"]["calls"] == 1


class TestLatestCompletedSession:
    """「最近一场已收盘」：今天已过收盘（提前收盘日 13:00）⇒ 今天，否则上一个交易日（含节假日 / 临时休市）。"""

    @pytest.mark.parametrize("now,expect", [
        ((2026, 10, 27, 17, 0), "2026-10-27"),     # 收盘后
        ((2026, 10, 27, 15, 0), "2026-10-26"),     # 盘中
        ((2026, 10, 27, 8, 0), "2026-10-26"),      # 盘前
        ((2026, 11, 27, 14, 0), "2026-11-27"),     # 感恩节次日 13:00 提前收盘
        ((2026, 11, 27, 12, 0), "2026-11-25"),     # 提前收盘日盘中（11-26 感恩节）
        ((2026, 10, 24, 10, 0), "2026-10-23"),     # 周六
        ((2025, 1, 9, 17, 0), "2025-01-08"),       # 临时休市（卡特国葬）
    ])
    def test_calendar(self, now, expect):
        assert yf_daily.latest_completed_session(dt.datetime(*now, tzinfo=_ET)).isoformat() == expect

    def test_calendar_failure_is_none(self, monkeypatch):
        import is_trading_day
        monkeypatch.setattr(is_trading_day, "session_close_et", lambda d: (_ for _ in ()).throw(RuntimeError("x")))
        assert yf_daily.latest_completed_session(_AFTER_CLOSE) is None


class TestTrailingVerdict:
    """v0.45.658（二次检查 #2）：末段空行按「剩下的末根是不是最近一场已收盘」判，不再一律当缺。"""

    def test_premarket_on_ex_date_is_pending_not_missing(self, monkeypatch):
        """盘前跑、除息日是今天：剩下的末根（10-21）就是最近一场已收盘的，照用；只计数、不进明细、不报。
        变红的变异：`_trail_verdict` 不看日历（恒当缺了，652 的口径）。"""
        _at(monkeypatch, 2026, 10, 22, 8, 0)
        base = _frame(DAYS)
        out, missing = yf_daily.history(_Tk(_with_event(base, "last", "2026-10-22")), source="sector_rotation", period="5d")
        assert missing is False and out.equals(base)
        st = yf_daily.stats()
        assert (st["last_row"], st["last_row_pending"], st["items_total"]) == (0, 1, 0)

    def test_same_frame_after_close_is_missing(self, monkeypatch):
        _at(monkeypatch, 2026, 10, 22, 17, 0)
        out, missing = yf_daily.history(_Tk(_with_event(_frame(DAYS), "last", "2026-10-22")), source="sector_rotation",
                                        period="5d")
        assert missing is True and yf_daily.stats()["last_row"] == 1

    def test_early_close_day(self, monkeypatch):
        """提前收盘日 13:00 之后那一场就算收了（`session_close_et`），之前不算。"""
        days = _bdays("2026-10-28", "2026-11-25")
        frame = _with_event(_frame([d for d in days if d != "2026-11-26"]), "last", "2026-11-27")
        _at(monkeypatch, 2026, 11, 27, 14, 0)
        assert yf_daily.history(_Tk(frame), source="sector_rotation", period="1mo")[1] is True
        _at(monkeypatch, 2026, 11, 27, 12, 0)
        assert yf_daily.history(_Tk(frame), source="sector_rotation", period="1mo")[1] is False

    def test_calendar_unavailable_counts_as_missing(self, monkeypatch):
        """判不了 ⇒ 宁缺不错，并计数（摘要行 / P2 里看得见）。"""
        monkeypatch.setattr(yf_daily, "latest_completed_session", lambda now=None: None)
        out, missing = yf_daily.history(_Tk(_with_event(_frame(DAYS), "last", "2026-10-22")), source="macro_quotes",
                                        period="5d")
        assert missing is True and yf_daily.stats()["calendar_unavailable"] == 1

    def test_non_daily_interval_skips_the_calendar(self, monkeypatch):
        _at(monkeypatch, 2026, 10, 22, 8, 0)
        out, missing = yf_daily.history(_Tk(_with_event(_frame(DAYS), "last", "2026-10-22")),
                                        source="mcp_price_history", period="3mo", interval="1wk")
        assert missing is True


class TestRuns:
    """v0.45.658（二次检查 #6 / #8）：首段 / 末段按**连续段**分档；判据与按行号删只有 `price_metrics` 那一份。"""

    def _two(self, base, where):
        a = _with_event(base, where, "2026-09-18" if where == "first" else "2026-10-22")
        b = _with_event(a, where, "2026-09-21" if where == "first" else "2026-10-23", col="Stock Splits")
        return b.sort_index() if where == "first" else b

    def test_two_leading_blank_rows_are_both_first_rows(self):
        """变红的变异：首段只认第 0 行（第 1 行会被算成「窗口中间」⇒ 误报 P2）。"""
        base = _frame(DAYS)
        out, missing = yf_daily.history(_Tk(self._two(base, "first")), source="sector_rotation", period="1mo")
        assert missing is False and out.equals(base)
        st = yf_daily.stats()
        assert (st["first_row"], st["middle"], st["items_total"]) == (2, 0, 0)

    def test_two_trailing_blank_rows_are_one_missing_frame(self):
        base = _frame(DAYS)
        out, missing = yf_daily.history(_Tk(self._two(base, "last"), "XX"), source="sector_rotation", period="5d")
        assert missing is True and out.equals(base)
        st = yf_daily.stats()
        assert (st["last_row"], st["middle"], st["items_total"]) == (1, 0, 1)
        assert st["items"][0]["date"] == "2026-10-23" and st["items"][0]["last"] is True

    def test_645_counter_uses_the_same_runs(self):
        """645 的 `price_metrics.event_row_stats` 同一份分档（此前连着两行首段空行会有一行进「不在首行」⇒ 误报 P2）。"""
        import price_metrics
        base = _frame(DAYS)
        price_metrics.drop_event_only_rows(self._two(base, "first"), "ZZ")
        st = price_metrics.event_row_stats()
        assert (st["first_row"], st["elsewhere_total"], st["last_row"]) == (2, 0, 0)

    def test_capital_gains_only_blank_row_is_removed(self):
        """基金 / ETF 的资本利得分配同样单独成行；入口的事件列比 H1 多这一列（H1 只读个股）。"""
        base = _frame(DAYS)
        base["Capital Gains"] = 0.0
        out, missing = yf_daily.history(_Tk(_with_event(base, "first", "2026-09-21", col="Capital Gains")),
                                        source="sector_rotation", period="1mo")
        assert out.equals(base) and missing is False

    def test_entry_and_645_share_one_implementation(self):
        """入口不自己写「按行号删 / 首末段」：只调 `price_metrics.event_row_layout` / `drop_positions`（#8）。"""
        tree = ast.parse((_ROOT / "yf_daily.py").read_text(encoding="utf-8"))
        called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert {"price_metrics.event_row_layout", "price_metrics.drop_positions"} <= called
        assert "price_metrics.event_only_positions" not in called


# ══════════════════════════════════════════════════════════════════════════
# 2. 真 yfinance 1.2.0 代码路径（假 Yahoo 响应，不联网）
# ══════════════════════════════════════════════════════════════════════════

def _chart(bar_days, div_days=(), split_days=()):
    def ts(d):
        return int(pd.Timestamp(f"{d} 09:30", tz=TZ).timestamp())
    px = [100.0 + i for i in range(len(bar_days))]
    res = {"meta": {"instrumentType": "EQUITY", "exchangeTimezoneName": TZ, "currency": "USD", "priceHint": 2,
                    "validRanges": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]},
           "timestamp": [ts(d) for d in bar_days],
           "indicators": {"quote": [{"open": px, "high": px, "low": px, "close": px, "volume": [1_000_000] * len(px)}],
                          "adjclose": [{"adjclose": px}]},
           "events": {}}
    if div_days:
        res["events"]["dividends"] = {str(ts(d)): {"amount": 0.5, "date": ts(d)} for d in div_days}
    if split_days:
        res["events"]["splits"] = {str(ts(d)): {"date": ts(d), "numerator": 2, "denominator": 1, "splitRatio": "2:1"}
                                   for d in split_days}
    return {"chart": {"result": [res], "error": None}}


class _RealYfTicker:
    """用 yfinance 自己的 `PriceHistory.history()` 处理一份假 Yahoo 响应（不建会话、不联网）。"""

    def __init__(self, payload):
        from yfinance.scrapers.history import PriceHistory

        class _Resp:
            text = json.dumps(payload)

            def json(self_inner):
                return payload

        class _Data:
            def get(self_inner, url, params, timeout):
                return _Resp()
            cache_get = get

        self.ticker = "FAKE"
        self._ph = PriceHistory(_Data(), self.ticker, TZ, session=object())

    def history(self, **kw):
        return self._ph.history(**kw)


def _blank_rows(df):
    px = df[["Open", "High", "Low", "Close"]]
    return [i for i in range(len(df)) if px.iloc[i].isna().all()]


class TestRealYfinanceMechanics:
    """入口的前提。yfinance 版本钉在 requirements.txt（1.2.0）；换版本后这里红 = 重新核对 `safe_merge_dfs`。"""

    def test_period_only_leaks_boundary_event_as_first_row_and_entry_removes_it(self):
        raw = _RealYfTicker(_chart(DAYS, ["2026-09-21"])).history(period="1mo")
        assert _blank_rows(raw) == [0]
        out, last = yf_daily.history(_RealYfTicker(_chart(DAYS, ["2026-09-21"])), source="sector_rotation", period="1mo")
        assert _blank_rows(out) == [] and last is False and len(out) == len(DAYS)

    def test_event_after_last_bar_is_last_row_with_or_without_start(self):
        for kw in ({"period": "1mo"}, {"start": DAYS[0]}):
            out, last = yf_daily.history(_RealYfTicker(_chart(DAYS, ["2026-10-22"])), source="macro_quotes", **kw)
            assert last is True and _blank_rows(out) == [] and len(out) == len(DAYS), kw

    def test_splits_behave_the_same(self):
        raw = _RealYfTicker(_chart(DAYS, (), ["2026-09-21"])).history(period="1mo")
        assert _blank_rows(raw) == [0]

    def test_start_trims_the_leading_event(self):
        raw = _RealYfTicker(_chart(DAYS, ["2026-09-21"])).history(start=DAYS[0])
        assert _blank_rows(raw) == []

    def test_mid_window_event_without_a_bar_folds_into_the_previous_bar(self):
        """「空行只在首行 / 末行」的依据：窗口中间没有 K 线的除息日并进前一根，不造空行。"""
        bars = [d for d in DAYS if d != "2026-10-01"]
        raw = _RealYfTicker(_chart(bars, ["2026-10-01"])).history(period="1mo")
        assert _blank_rows(raw) == [] and float(raw.loc[raw.index.date == pd.Timestamp("2026-09-30").date(),
                                                       "Dividends"].iloc[0]) == 0.5

    def test_actions_false_drops_the_last_row_silently(self):
        """为什么入口不用 `actions=False`：它把末行空行也删掉、不留任何信号 ⇒ 前一场冒充今天。"""
        raw = _RealYfTicker(_chart(DAYS, ["2026-10-22"])).history(period="1mo", actions=False)
        assert _blank_rows(raw) == [] and len(raw) == len(DAYS) and "Dividends" not in raw.columns


def _chart_px(bar_days, div_days=(), split_days=(), seed=0):
    out = _chart(bar_days, div_days, split_days)
    q = out["chart"]["result"][0]["indicators"]
    px = [100.0 + seed + i for i in range(len(bar_days))]
    q["quote"][0].update(open=px, high=px, low=px, close=px, volume=[1_000_000 + seed] * len(px))
    q["adjclose"][0]["adjclose"] = px
    return out


@pytest.fixture
def real_download(monkeypatch):
    """真 `yfinance.multi.download`（不联网）：每只票的 `Ticker` 换成吃假 Yahoo 响应的 `_RealYfTicker`。返回装载函数。"""
    import yfinance.multi as ym

    def install(payloads):
        def mk(sym):
            t = _RealYfTicker(payloads[sym])
            t.ticker = sym
            return t
        monkeypatch.setattr(ym, "Ticker", mk)
        return types.SimpleNamespace(download=ym.download)
    return install


_D_CASES = {
    "clean": {"AAA": ((), ()), "BBB": ((), ())},
    "first_one": {"AAA": (["2026-09-21"], ()), "BBB": ((), ())},
    "last_one": {"AAA": (["2026-10-22"], ()), "BBB": ((), ())},
    "last_both": {"AAA": (["2026-10-22"], ()), "BBB": (["2026-10-22"], ())},
    "two_leading": {"AAA": (["2026-09-18"], ["2026-09-21"]), "BBB": ((), ())},
}
_D_EXPECT = {"clean": set(), "first_one": set(), "last_one": {"AAA"}, "last_both": {"AAA", "BBB"}, "two_leading": set()}


class TestDownloadOnRealYfinance:
    """v0.45.658（二次检查 #1）：`yf_daily.download` 内部 actions=True、去掉空行后**还原成 actions=False 的那一帧**——按日期用的
    调用方（ignore）逐位不变，只多拿到「最近一场缺了」的标志。真 yfinance 1.2.0 `multi.download` 代码路径，不联网。
    （`freq` 是 pandas 推断的索引元数据、两边各自推，不比——`DataFrame.equals` 也不看它。）"""

    @pytest.mark.parametrize("case", sorted(_D_CASES))
    @pytest.mark.parametrize("group_by", ["ticker", "column"])
    @pytest.mark.parametrize("syms", [["AAA", "BBB"], ["AAA"]], ids=["two", "one"])
    def test_frame_equals_actions_false_and_flags_are_right(self, real_download, case, group_by, syms):
        """变红的变异：不还原成交量的 int64（别的票因对齐行被升成浮点）/ 不去掉只因空行才存在的行 / 不去事件列。"""
        payloads = {s: _chart_px(DAYS, *_D_CASES[case][s], seed=5 * i) for i, s in enumerate(["AAA", "BBB"])}
        yfm = real_download(payloads)
        kw = dict(period="1mo", interval="1d", auto_adjust=True, progress=False, threads=False, group_by=group_by)
        base = yfm.download(syms, **kw)                                    # 改前：actions=False（yfinance 缺省）
        got, flags = yf_daily.download(yfm, syms, source="prefetch_bundle", **kw)
        pd.testing.assert_frame_equal(base, got, check_freq=False)
        assert {s for s, m in flags.items() if m} == _D_EXPECT[case] & set(syms) and set(flags) == set(syms)

    def test_other_ticker_has_the_day_so_the_row_stays(self, real_download):
        """AAA 缺 10-22（末行空行），BBB 有 10-22 那根：行不能去（BBB 的数据在），AAA 那格置空——与 actions=False 相同。"""
        payloads = {"AAA": _chart_px(DAYS, ["2026-10-22"]), "BBB": _chart_px(DAYS + ["2026-10-22"], seed=5)}
        yfm = real_download(payloads)
        kw = dict(period="1mo", interval="1d", auto_adjust=True, progress=False, threads=False)
        base = yfm.download(["AAA", "BBB"], **kw)
        got, flags = yf_daily.download(yfm, ["AAA", "BBB"], source="prefetch_bundle", **kw)
        pd.testing.assert_frame_equal(base, got, check_freq=False)
        assert flags == {"AAA": True, "BBB": False}

    def test_flat_single_ticker(self, real_download):
        yfm = real_download({"AAA": _chart_px(DAYS, ["2026-09-21", "2026-10-22"])})
        kw = dict(period="1mo", auto_adjust=True, progress=False, multi_level_index=False)
        base = yfm.download("AAA", **kw)
        got, flags = yf_daily.download(yfm, "AAA", source="prefetch_bundle", **kw)
        pd.testing.assert_frame_equal(base, got, check_freq=False)
        assert flags == {"AAA": True}

    def test_raw_actions_true_really_carries_the_blank_row(self, real_download):
        """前提自证：不还原的话 actions=True 那一帧确实多出空行 + 事件列（否则上面的相等是空话）。"""
        yfm = real_download({"AAA": _chart_px(DAYS, ["2026-10-22"]), "BBB": _chart_px(DAYS, seed=5)})
        raw = yfm.download(["AAA", "BBB"], period="1mo", auto_adjust=True, progress=False, threads=False, actions=True)
        base = yfm.download(["AAA", "BBB"], period="1mo", auto_adjust=True, progress=False, threads=False)
        assert len(raw) == len(base) + 1 and "Dividends" in set(raw.columns.get_level_values(0))

    @pytest.mark.parametrize("bad", ["actions", "keepna"])
    def test_caller_may_not_pass_actions_or_keepna(self, bad):
        called = []
        yfm = types.SimpleNamespace(download=lambda *a, **k: called.append(k))
        with pytest.raises(ValueError, match="别传"):
            yf_daily.download(yfm, ["AAA"], source="prefetch_bundle", period="1mo", **{bad: False})
        assert called == []

    def test_non_frame_is_returned_as_is(self):
        sentinel = object()
        out, flags = yf_daily.download(types.SimpleNamespace(download=lambda *a, **k: sentinel), "AAA",
                                       source="prefetch_bundle", period="1mo")
        assert out is sentinel and flags == {} and yf_daily.stats()["uninspectable"] == 1


# ══════════════════════════════════════════════════════════════════════════
# 3. 调用点行为
# ══════════════════════════════════════════════════════════════════════════

class TestSectorRotation:
    """进分的那一条：11 只 SPDR 同日除息，首行空行此前让整张表变空。"""

    @pytest.fixture
    def rotation(self, monkeypatch):
        import fred_macro as fm
        monkeypatch.setattr(fm, "_etf_cache", {})
        # v0.45.663：板块轮动按日期配对、锚定「这一场」——钉在夹具末根那天（否则跟着真实钟走、配不上）
        monkeypatch.setattr(fm, "_realtime_as_of", lambda: DAYS[-1])
        d5 = DAYS[-5:]
        frames = {e: _frame(d5, seed=i) for i, e in enumerate(fm._SECTOR_ETFS)}

        def run(where=None):
            def mk(e):
                return frames[e] if where is None else _with_event(frames[e], where,
                                                                   "2026-10-14" if where == "first" else "2026-10-22")
            fake = types.SimpleNamespace(Ticker=lambda e: _Tk(mk(e), e))
            fm._etf_cache.clear()
            return fm._fetch_sector_rotation(yf_module=fake)
        return run

    def test_first_row_blank_on_all_eleven_no_longer_empties_the_table(self, rotation):
        """变红的变异：`_fetch_one` 改回直调 `t.history(...)`（旧代码这里是 0 只）。"""
        clean, leaked = rotation(), rotation("first")
        assert len(clean["full"]) == 11 and leaked["full"] == clean["full"]
        assert leaked["hot"] == clean["hot"] and leaked["cold"] == clean["cold"]
        assert yf_daily.stats()["by_source"]["sector_rotation"]["first_row"] == 11

    def test_last_row_blank_drops_the_etf_instead_of_using_the_previous_session(self, rotation):
        out = rotation("last")
        assert out["full"] == {} and yf_daily.stats()["by_source"]["sector_rotation"]["last_row"] == 11


class TestRivalFallback:

    def _rival(self, monkeypatch, hist):
        from swarm_agents.rival_bee import RivalBeeVanguard
        monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=lambda t: _Tk(hist, t)))
        rb = object.__new__(RivalBeeVanguard)
        rb._prefetched_market = {}
        return rb

    def test_first_row_blank_changes_nothing(self, monkeypatch):
        base = _frame(_bdays("2026-07-20", "2026-10-21"), seed=3)
        clean = self._rival(monkeypatch, base)._calc_technical_indicators("ZZRV")
        leaked = self._rival(monkeypatch, _with_event(base, "first", "2026-07-17"))._calc_technical_indicators("ZZRV")
        assert leaked == clean and clean["summary"]

    def test_last_row_blank_goes_neutral_not_yesterday(self, monkeypatch):
        """变红的变异：`_yf_history` 不看第二个返回值（会拿去掉空行后的前一场算技术面）。"""
        base = _frame(_bdays("2026-07-20", "2026-10-21"), seed=3)
        rb = self._rival(monkeypatch, _with_event(base, "last", "2026-10-22"))
        assert rb._yf_history("ZZRV", period="3mo").empty
        out = rb._calc_technical_indicators("ZZRV")
        assert out["tech_direction"] == "neutral" and out["summary"] == ""


def _fake_download(missing=(), calls=None):
    """`yf.download(..., actions=True)` 的同形桩：每只票一份日线（10-27 收盘后），`missing` 里的票末根停在 10-26、
    10-27 是公司行动空行（最近一场缺了）。按 group_by 拼成 yfinance 的两层列。"""
    full = _bdays("2026-09-28", "2026-10-27")

    def dl(tickers, **kw):
        if calls is not None:
            calls.append((tickers, kw))
        assert kw.get("actions") is True, "入口必须自己要事件列"
        names = [tickers] if isinstance(tickers, str) else list(tickers)
        frames = {}
        for i, t in enumerate(names):
            frames[t] = (_with_event(_frame(full[:-1], seed=i), "last", full[-1]) if t in missing
                         else _frame(full, seed=i))
        raw = pd.concat(frames, axis=1, names=["Ticker", "Price"])
        if kw.get("group_by", "column") != "ticker":
            raw = raw.swaplevel(0, 1, axis=1).sort_index(axis=1, level=0)
        return raw
    return dl


class _BundleBee:
    @staticmethod
    def make(market):
        from pheromone_board import PheromoneBoard
        from swarm_agents.base import BeeAgent

        class _B(BeeAgent):
            def analyze(self, ticker):  # pragma: no cover
                return {}
        b = _B(PheromoneBoard())
        b._prefetched_market = market
        return b


class TestPrefetchBundle:
    """v0.45.658（二次检查 #1）：Rival / Scout 的主路径。此前裸 `yf.download`（actions=False）把末行空行一声不吭删掉 ⇒ 前一场冒充今天；
    回退路径（652）却当取不到——同一只蜂两种口径。现在包里记下「最近一场缺了」的票，两个访问器都当取不到、不再直连重取。"""

    @pytest.fixture
    def bundle(self, monkeypatch):
        import yfinance
        from swarm_agents.base import prefetch_market_bundle
        calls = []
        monkeypatch.setattr(yfinance, "download", _fake_download(missing={"ZZMS"}, calls=calls))
        monkeypatch.setattr(yfinance, "Ticker", lambda t: (_ for _ in ()).throw(RuntimeError("no Ticker")))
        return prefetch_market_bundle(["NVDA", "ZZMS"]), calls

    def test_missing_ticker_is_recorded_not_kept(self, bundle):
        """变红的变异：预取包改回裸 `yf.download`（ZZMS 会带着止于 10-26 的日线进包）。"""
        b, calls = bundle
        assert b["history_missing"] == ["ZZMS"] and "ZZMS" not in b["history"] and "NVDA" in b["history"]
        assert len(calls) == 1 and yf_daily.stats()["by_source"]["prefetch_bundle"]["last_row"] == 1

    def test_history_accessor_refuses_without_refetching(self, bundle, monkeypatch):
        """变红的变异：`_yf_history` 不看 `history_missing`（会直连重取一遍同样缺的数据，或拿前一场算技术面）。"""
        import yfinance
        b, _calls = bundle
        hit = []
        monkeypatch.setattr(yfinance, "Ticker", lambda t: hit.append(t))
        bee = _BundleBee.make(b)
        assert bee._yf_history("ZZMS", period="3mo").empty and hit == []
        assert len(bee._yf_history("NVDA", period="3mo")) > 10

    def test_rival_goes_neutral_for_the_missing_ticker(self, bundle):
        from swarm_agents.rival_bee import RivalBeeVanguard
        b, _calls = bundle
        rb = object.__new__(RivalBeeVanguard)
        rb._prefetched_market = b
        out = rb._calc_technical_indicators("ZZMS")
        assert out["tech_direction"] == "neutral" and out["summary"] == ""

    def test_close_panel_drops_the_missing_column_without_refetching(self, bundle, monkeypatch):
        """Scout 相对强度拿末根当今天：缺列走它原有的「取不到」分支。变红的变异：`_yf_close_panel` 不看 `history_missing`。"""
        b, calls = bundle
        bee = _BundleBee.make(b)
        panel = bee._yf_close_panel(["NVDA", "ZZMS"], "25d")
        assert list(panel.columns) == ["NVDA"] and len(calls) == 1

    def test_close_panel_direct_fallback_drops_flagged_columns(self, monkeypatch):
        import yfinance
        calls = []
        monkeypatch.setattr(yfinance, "download", _fake_download(missing={"ZZMS"}, calls=calls))
        bee = _BundleBee.make({})
        panel = bee._yf_close_panel(["NVDA", "ZZMS"], "25d")
        assert list(panel.columns) == ["NVDA"] and len(calls) == 1
        assert yf_daily.stats()["by_source"]["close_panel"]["last_row"] == 1


class TestSupplyChain:

    def test_missing_peer_is_skipped_not_yesterday(self, monkeypatch):
        """变红的变异：供应链不看第二个返回值（TSM 的「5 日涨跌」会止于前一场）。"""
        import yfinance
        import market_intelligence as mi
        monkeypatch.setattr(yfinance, "download", _fake_download(missing={"TSM"}))
        out = mi.get_supply_chain_signals("NVDA")
        syms = {p["symbol"] for p in out.get("peers", [])}
        assert "TSM" not in syms and {"AMAT", "ASML", "SOXX"} <= syms


class TestAsofHistory:

    def _yf(self, hist):
        return types.SimpleNamespace(Ticker=lambda s: _Tk(hist, s))

    def test_last_row_blank_on_target_day_is_not_available(self):
        import fred_macro as fm
        base = _frame(_bdays("2026-10-08", "2026-10-20"))
        assert fm._asof_history(self._yf(_with_event(base, "last", "2026-10-21")), "TLT", "2026-10-21") is None
        assert yf_daily.stats()["by_source"]["macro_asof"]["last_row"] == 1

    def test_clean_window_unchanged(self):
        import fred_macro as fm
        base = _frame(_bdays("2026-10-08", "2026-10-21"))
        out = fm._asof_history(self._yf(base), "TLT", "2026-10-21")
        assert out is not None and out.index[-1].date().isoformat() == "2026-10-21" and len(out) == len(base)


class TestHvValueIdentity:
    """HV（→ iv_rank → Oracle 期权分 → 维度 IC H2 的输入）首行 / 末行空行**逐位不变**：本版不在它的世代边界里，靠这条撑着。
    旧代码 = 同一个函数但取数不去空行（把入口换成透传）；生产 pandas 3.0.1（requirements.txt 钉住）下 `pct_change` 不前向填充。"""

    def _hv(self, monkeypatch, tmp_path, hist, *, passthrough=False):
        import options_analyzer as oa
        monkeypatch.setattr(oa, "yf", types.SimpleNamespace(Ticker=lambda t: _Tk(hist, t)))
        monkeypatch.setitem(sys.modules, "yf_gate", types.SimpleNamespace(ensure=lambda: None))
        if passthrough:
            monkeypatch.setattr(oa.yf_daily, "history", lambda tk, source, **kw: (tk.history(**kw), False))
        f = oa.OptionsDataFetcher(cache_dir=str(tmp_path / ("old" if passthrough else "new")))
        hv = f.fetch_historical_hv("ZZHV")
        monkeypatch.undo()
        return hv, f.hist_hv_untrusted()

    def _days(self):
        from bars_integrity import previous_completed_session, trading_days
        import datetime as dt
        last = previous_completed_session(None)
        return [d.isoformat() for d in trading_days(last - dt.timedelta(days=365), last)], last

    @pytest.mark.parametrize("seed", range(5))
    def test_first_and_last_row_blank_leave_hv_identical(self, monkeypatch, tmp_path, seed):
        import datetime as dt
        days, last = self._days()
        base = _frame(days, seed=seed)
        clean = self._hv(monkeypatch, tmp_path, base)
        assert clean[0] and clean[1] is False
        before = (pd.Timestamp(days[0]) - pd.offsets.BDay(1)).date().isoformat()
        after = (last + dt.timedelta(days=1)).isoformat()
        for where, day in (("first", before), ("last", after)):
            leaked = _with_event(base, where, day)
            assert self._hv(monkeypatch, tmp_path, leaked) == clean, where
            assert self._hv(monkeypatch, tmp_path, leaked, passthrough=True) == clean, where


# ══════════════════════════════════════════════════════════════════════════
# 4 / 5. 静态：末行策略逐点登记 + 单一入口守卫
# ══════════════════════════════════════════════════════════════════════════

#: `.history(` 直调的存量例外：(文件, 所在函数的限定名) → (类别, 理由)。只许减。每个键恰好对应**一处**调用（多一处同函数里的直调也红）。
#: 类别决定结构核对：intraday 必须 `interval="1m"`；needs_events 必须 `actions=True`；raw_archive / legacy_645 由 645 的守卫钉住下一句。
#: v0.45.658：「dated_window」类（11 处带日期窗口的读者）整类迁入入口——它的理由「末行碰不到」只核得到 `start=`，核不了 `end` 在不在过去。
EXEMPT = {
    ("observation_layer.py", "yf_history"):
        ("raw_archive", "H1 行情原样落档；调用者只许是 data_pipeline 三处，下一句 drop_event_only_rows（645 钉住）"),
    ("swarm_agents/cache.py", "_fetch_stock_data"):
        ("legacy_645", "行情三字段的旧回落，下一句 drop_event_only_rows（tests/test_event_only_rows.py 钉住）"),
    ("paper_portfolio.py", "_fetch_split_events"):
        ("needs_events", "要的就是拆股事件列本身"),
    ("observation_layer.py", "spy_last_minute"):
        ("intraday", "1m：yfinance 丢掉日内的越界事件"),
    ("alphabot/yf_chain.py", "fetch_yf_raw_contracts"):
        ("intraday", "1m：yfinance 丢掉日内的越界事件"),
}

#: 每个入口调用点（`yf_daily.history` / `yf_daily.download`）的末行策略。refuse = 用末根当今天，必须读第二个返回值；
#: ignore = 按日期用整段（或末根只是近似），明确不读。键 = (文件, 函数限定名)；一个函数里有几处入口调用，每一处都按这条核。
POLICY = {
    # ── history：用末根 / 近几根当「现在」──
    ("options_analyzer.py", "OptionsDataFetcher.fetch_historical_hv"): "ignore",   # 缺口链按日期接住末行
    ("swarm_agents/base.py", "BeeAgent._yf_history"): "refuse",
    ("fred_macro.py", "_asof_history"): "refuse",
    ("fred_macro.py", "_fetch_macro_data"): "refuse",
    ("fred_macro.py", "_fetch_sector_rotation._fetch_one"): "refuse",
    ("alpha_hive_daily_report.py", "AlphaHiveDailyReporter._post_scan_notify"): "refuse",
    ("alpha_hive_daily_report.py", "AlphaHiveDailyReporter._generate_synthetic_swarm_results"): "refuse",
    ("dashboard_renderer.py", "_detail"): "refuse",
    ("alpha_hive_mcp.py", "alphahive_get_price_history._fetch"): "ignore",          # 蜡烛自带日期
    ("swarm_agents/cache.py", "check_ticker_validity"): "ignore",                    # 只问有没有成交 / 价格是否极低
    ("vix_term_structure.py", "_get_spot_vix"): "refuse",
    ("iv_crush_scraper.py", "get_iv_crush_data"): "ignore",                          # 财报涨跌按日期；现价只挑平值
    ("crowding_detector.py", "get_crowding_metrics"): "refuse",
    ("risk_engine.py", "_fetch_daily_returns"): "ignore",
    ("risk_engine.py", "historical_var"): "ignore",
    ("risk_engine.py", "run_full_risk_analysis"): "refuse",
    # ── history：带日期窗口、按日期用（v0.45.658 迁入）──
    ("backtest_engine.py", "PriceBackfiller._get_price_on_date"): "ignore",
    ("backtester.py", "Backtester._history"): "ignore",
    ("dynamic_exit_backtest.py", "_fetch_price_series"): "ignore",
    ("factor_attribution.py", "_get_stock_returns"): "ignore",
    ("outcomes_fetcher.py", "OutcomesFetcher._fetch_remote"): "ignore",
    ("paper_portfolio.py", "prefetch_ohlc"): "ignore",
    ("paper_portfolio.py", "_ReplayOhlcWindow._download"): "ignore",
    ("paper_portfolio.py", "_fetch_ohlc_direct"): "ignore",
    ("portfolio_backtest.py", "load_verified_predictions"): "ignore",
    ("portfolio_backtest.py", "_fetch_spy_prices"): "ignore",
    ("portfolio_factor_attribution.py", "_load_etf_factors"): "ignore",
    # ── download（v0.45.658 迁入）──
    ("swarm_agents/base.py", "prefetch_market_bundle"): "refuse",                   # Rival / Scout 主路径
    ("swarm_agents/base.py", "BeeAgent._yf_close_panel"): "refuse",                 # Scout 相对强度
    ("market_intelligence.py", "detect_market_regime._get_ma"): "refuse",           # Guard 市场政体：末根当现价
    ("market_intelligence.py", "get_supply_chain_signals"): "refuse",               # Scout 供应链 5 日涨跌
    ("cboe_fetcher.py", "CBOEDailyFetcher._fetch_index_level"): "refuse",           # 指数无分红，照核
    ("market_intelligence.py", "calculate_iv_rv_spread"): "ignore",                 # 窗口统计；Oracle → H2 输入，口径不动
    ("portfolio_concentration.py", "analyze_concentration"): "ignore",              # 一个月收益相关性
    ("risk_engine.py", "_fetch_multi_returns"): "ignore",
    ("risk_engine.py", "_estimate_beta"): "ignore",
    ("backtester.py", "Backtester._prefetch_backtest_prices"): "ignore",
    ("backfill_dir_accuracy.py", "_fetch_closes"): "ignore",
    ("close_correction.py", "official_closes"): "ignore",
    ("ic_diagnostics.py", "_close_panel"): "ignore",
    ("pead_analyzer.py", "_compute_post_earnings_drift"): "ignore",
    ("scan_coverage_gate.py", "check_prices"): "ignore",
    ("signal_archive.py", "_forward_realized_vol"): "ignore",
}

_PROD_SKIP = {"tests", "experiments", "_archive"}


@functools.lru_cache(maxsize=1)
def _prod_trees():
    from tests._repo_files import walk_own_python_files
    res = walk_own_python_files(_ROOT, skip_dirs=_PROD_SKIP)
    assert res.errors == [], res.errors
    out = {}
    for p in res.files:
        rel = p.relative_to(_ROOT).as_posix()
        try:
            out[rel] = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return out


def _calls(tree, pred):
    """`(限定名, Call 节点)`：pred(call) 为真的**每一处**，限定名 = 外层类 / 函数名用点连起来（同一函数里多处就出现多次）。"""
    hits = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _scope(self, n):
            self.stack.append(n.name)
            self.generic_visit(n)
            self.stack.pop()
        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

        def visit_Call(self, n):
            if pred(n):
                hits.append((".".join(self.stack) or "<module>", n))
            self.generic_visit(n)
    V().visit(tree)
    return hits


def _is_attr_call(name):
    return lambda n: isinstance(n.func, ast.Attribute) and n.func.attr == name


def _is_entry_call(n):
    return (isinstance(n.func, ast.Attribute) and n.func.attr in ("history", "download")
            and ast.unparse(n.func.value) == "yf_daily")


def _kw(n, name):
    return next((k.value for k in n.keywords if k.arg == name), None)


def _scan(trees, attr):
    """直调 `.<attr>(` 的每一处：[((文件, 限定名), 节点)]——**列表**，不折叠（v0.45.658 二次检查 #4：此前按函数建字典，
    同一函数里第二处直调被吞掉，结构核对也只看最后一处）。"""
    return [((rel, q), n) for rel, t in trees.items() for q, n in _calls(t, _is_attr_call(attr)) if not _is_entry_call(n)]


class TestSingleEntry:

    def test_direct_history_calls_are_only_the_registered_exceptions(self):
        """多一处直调 ⇒ 红（改走 yf_daily.history）；例外失效（被迁走 / 删了）⇒ 也红（从 EXEMPT 删掉，只许减）。
        **按多重集比**：例外函数里再加一处直调同样红。"""
        found = collections.Counter(k for k, _n in _scan(_prod_trees(), "history"))
        inside = {k: c for k, c in found.items() if k[0] == "yf_daily.py"}
        assert sum(inside.values()) == 1, inside
        outside = found - collections.Counter(inside)
        assert outside == collections.Counter(list(EXEMPT)), (
            {k: c for k, c in outside.items() if c != 1 or k not in EXEMPT}, set(EXEMPT) - set(outside))

    def test_direct_download_calls_only_inside_the_entry(self):
        """v0.45.658（二次检查 #1）：`.download(` 没有例外——只许 `yf_daily.download` 里那一处（它自己要 actions=True 再还原）。"""
        found = collections.Counter(k for k, _n in _scan(_prod_trees(), "download"))
        assert found == collections.Counter({("yf_daily.py", "download"): 1}), found

    def test_exceptions_still_have_the_shape_that_justifies_them(self):
        """变红的变异：把 1m 改成 1d、去掉 actions=True。每一处都核（不只最后一处）。"""
        for key, n in _scan(_prod_trees(), "history"):
            if key not in EXEMPT:
                continue
            cat, why = EXEMPT[key]
            if cat == "intraday":
                v = _kw(n, "interval")
                assert isinstance(v, ast.Constant) and v.value == "1m", key
            elif cat == "needs_events":
                v = _kw(n, "actions")
                assert isinstance(v, ast.Constant) and v.value is True, key

    def test_raw_archive_callers_are_only_data_pipeline(self):
        """`observation_layer.yf_history` 返回原样日线：别的文件调它就绕过了去空行（645 只钉住 data_pipeline 自己的三处）。
        变红的变异：在任何别的生产文件里加一处 `_obs_layer.yf_history(...)`。"""
        hits = [(rel, q) for rel, t in _prod_trees().items() if rel != "observation_layer.py"
                for q, _n in _calls(t, _is_attr_call("yf_history"))]
        assert sorted({rel for rel, _q in hits}) == ["data_pipeline.py"] and len(hits) == 3, hits

    def test_entry_callers_do_not_pass_actions_or_keepna(self):
        """入口自己决定 actions / keepna（运行时也拒）；静态再挡一道。"""
        bad = [(rel, q) for rel, t in _prod_trees().items() for q, n in _calls(t, _is_entry_call)
               if n.func.attr == "download" and (_kw(n, "actions") is not None or _kw(n, "keepna") is not None)]
        assert bad == []

    def test_sources_used_match_the_registry_both_ways(self):
        used = {}
        for rel, t in _prod_trees().items():
            for q, n in _calls(t, _is_entry_call):
                v = _kw(n, "source")
                assert isinstance(v, ast.Constant), f"{rel}::{q} 的 source 必须是字面量"
                used.setdefault(v.value, []).append((rel, q))
        assert set(used) == set(yf_daily.SOURCES), set(used) ^ set(yf_daily.SOURCES)

    def test_every_entry_call_site_has_a_registered_last_row_policy(self):
        sites = {(rel, q) for rel, t in _prod_trees().items() for q, _n in _calls(t, _is_entry_call)
                 if rel != "yf_daily.py"}
        assert sites == set(POLICY), sites ^ set(POLICY)

    def test_last_row_policy_is_what_the_code_does(self):
        """**每一处**入口调用：refuse 必须读第二个返回值；ignore 必须不读（变量以 `_` 开头且没被读，或只取 `[0]`）。
        变红的变异：板块轮动删掉 `_last_blank or`；HV 开始读它；预取包不看 `missing`。"""
        trees = _prod_trees()
        checked = 0
        for rel, t in trees.items():
            if rel == "yf_daily.py":
                continue
            for q, call in _calls(t, _is_entry_call):
                fn = next(n for qq, n in _scopes(t) if qq == q)
                flag = _flag_of(fn, call)
                loads = {x.id for x in ast.walk(fn) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
                if POLICY[(rel, q)] == "refuse":
                    assert flag and flag in loads, (rel, q, flag)
                else:
                    assert flag is None or (flag.startswith("_") and flag not in loads), (rel, q, flag)
                checked += 1
        assert checked == len(POLICY) + 0, checked      # 目前每个登记函数恰好一处入口调用

    def test_scan_has_teeth(self):
        """自证：扫到了足够多的生产文件（与 git 清单对账），且在违规源码上会命中、对入口调用不会误报；
        同一函数里两处直调数成 2（#4 的回归）。"""
        import subprocess
        trees = _prod_trees()
        r = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=str(_ROOT), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[:200]
        tracked = [x for x in r.stdout.split("\x00") if x and not (_PROD_SKIP & set(Path(x).parts[:-1]))
                   and not any(part.startswith(".") for part in Path(x).parts)]
        assert len(tracked) >= 100 and len(trees) >= 0.4 * len(tracked), (len(trees), len(tracked))
        assert {"fred_macro.py", "options_analyzer.py", "swarm_agents/base.py", "yf_daily.py"} <= set(trees)
        bad = ast.parse("def f(yf):\n    h = yf.Ticker('X').history(period='5d')\n"
                        "    h2 = yf.Ticker('Y').history(period='5d')\n"
                        "    d = yf.download('X', period='5d')\n"
                        "    ok, last = yf_daily.history(yf.Ticker('X'), source='options_hv', period='5d')\n"
                        "    ok2, m = yf_daily.download(yf, 'X', source='prefetch_bundle', period='5d')\n")
        assert collections.Counter(k for k, _n in _scan({"bad.py": bad}, "history")) == {("bad.py", "f"): 2}
        assert [k for k, _n in _scan({"bad.py": bad}, "download")] == [("bad.py", "f")]


def _scopes(tree):
    out = []

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}.{ch.name}" if prefix else ch.name
                out.append((q, ch))
                walk(ch, q)
            else:
                walk(ch, prefix)
    walk(tree, "")
    return out


def _flag_of(fn, call):
    """这一处入口调用的第二个返回值赋给了谁：`a, flag = yf_daily.xxx(...)` ⇒ flag；`yf_daily.xxx(...)[0]` ⇒ None。"""
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and node.value is call and isinstance(node.targets[0], ast.Tuple):
            return node.targets[0].elts[1].id
    return None


# ══════════════════════════════════════════════════════════════════════════
# 6. 告警接线
# ══════════════════════════════════════════════════════════════════════════

class TestVisibility:

    def test_counter_reaches_scan_timing_and_summary(self):
        import scan_timing
        yf_daily.history(_Tk(_with_event(_frame(DAYS), "first", "2026-09-21")), source="sector_rotation", period="5d")
        yf_daily.history(_Tk(_with_event(_frame(DAYS), "last", "2026-10-22")), source="macro_quotes", period="5d")
        c = scan_timing.counters()
        assert c["yf_daily"]["dropped"] == 2 and c["yf_daily"]["last_row"] == 1
        line = scan_timing.summary_line({"phases": {}, "counters": c})
        assert "其他日线读者去掉公司行动空行 2 行(首段 1, 最近一场缺了 1 份, 今天未收 0 份, 中间 0)" in line
        yf_daily._reset_stats()
        assert "其他日线读者" not in scan_timing.summary_line({"phases": {}, "counters": scan_timing.counters()})

    @staticmethod
    def _analyze(tmp_path, counter):
        """走完整的 `AlertAnalyzer.analyze()`（同 tests/test_event_only_rows.py 的写法）——证明 analyze 真的调了这条检查。"""
        import alert_manager as am
        status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"counters": {"yf_daily": counter}, "extra": {"gh_pages": {"success": True}},
                                  "production_sync": {"outcome": "up_to_date"}}}
        if counter is None:
            del status["scan_timing"]["counters"]["yf_daily"]
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a.analyze(p)
        return [x for x in a.alerts if "行情三字段以外的读者" in x.message], a

    def test_last_row_is_p2_naming_the_source(self, tmp_path):
        """变红的变异：analyze() 不调 `_check_yf_daily_event_rows`；级别不是 P2。"""
        import alert_manager as am
        yf_daily.history(_Tk(_with_event(_frame(DAYS), "last", "2026-10-22"), "XLK"), source="sector_rotation", period="5d")
        (h,), _a = self._analyze(tmp_path, yf_daily.stats())
        assert h.level == am.AlertLevel.MEDIUM and "sector_rotation · XLK 2026-10-22" in h.details["明细"]

    def test_first_row_only_is_silent(self, tmp_path):
        yf_daily.history(_Tk(_with_event(_frame(DAYS), "first", "2026-09-21")), source="sector_rotation", period="5d")
        hits, a = self._analyze(tmp_path, yf_daily.stats())
        assert hits == [] and not any("yf_daily" in s for s in a.checks_skipped)

    def test_pending_trailing_row_is_silent(self, tmp_path, monkeypatch):
        """盘前跑、今天那场还没收：照用前一场，不报（与没有事件的日子一样）。"""
        _at(monkeypatch, 2026, 10, 22, 8, 0)
        yf_daily.history(_Tk(_with_event(_frame(DAYS), "last", "2026-10-22"), "XLK"), source="sector_rotation", period="5d")
        hits, _a = self._analyze(tmp_path, yf_daily.stats())
        assert hits == [] and yf_daily.stats()["last_row_pending"] == 1

    def test_missing_counter_is_skipped_not_silent(self, tmp_path):
        hits, a = self._analyze(tmp_path, None)
        assert hits == [] and any("yf_daily" in s for s in a.checks_skipped)


# ══════════════════════════════════════════════════════════════════════════
# 7. 世代边界登记
# ══════════════════════════════════════════════════════════════════════════

def _row(*, new=False, guard="ok", rival="ok", backfill=False):
    agents = {}
    if guard == "ok":
        det = {"macro_adj": 0.0}
        if new:
            det[_MARK] = True
        if backfill:
            det["vix_term_structure"] = {"macro_as_of_mode": "backfill"}
        agents["GuardBeeSentinel"] = {"score": 5.0, "details": det}
    elif guard == "errored":
        agents["GuardBeeSentinel"] = {"score": 5.0, "details": {}, "error": "TypeError: boom"}
    if rival == "ok":
        agents["RivalBeeVanguard"] = {"score": 5.0, "details": {}}
    elif rival == "errored":
        agents["RivalBeeVanguard"] = {"score": 5.0, "details": {}, "error": "TypeError: boom"}
    return {"agent_details": agents}


def _write(root, day, rows, archive=("AAA",)):
    (root / f".swarm_results_{day}.json").write_text(json.dumps(rows), encoding="utf-8")
    for tk in archive:
        (root / f"analysis-{tk}-ml-{day}.json").write_text(json.dumps({"swarm_results": rows.get(tk, _row())}),
                                                             encoding="utf-8")


class TestBoundaryRegistered:

    def test_entry_scope_marker_equivalence(self):
        import ic_rerun_readiness as rr
        import signal_archive as sa
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V] == ["2026-10-19"]
        assert sa.COHORT_SIGNAL_SCOPE[_V] == ("agent.GuardBeeSentinel.*",)
        assert _V in rr._BOUNDARY_MARKERS and _V in rr._BOUNDARY_EQUIVALENCE

    def test_moves_no_signal_generation(self):
        """「边际作废 0 条」的可执行版：加这条前后，归档全部信号的当代起点逐个相同。
        变红的变异：作用域里加上 `guard.macro_adj`（09-28 → 10-19）或 `agent.RivalBeeVanguard.*`。"""
        import signal_archive as sa
        hist = sa._cohort_history()
        assert any(v == _V for _d, v, _r in hist)
        sigs = set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)
        without = [e for e in hist if e[1] != _V]
        before, after = sa.generation_boundaries(sigs, without), sa.generation_boundaries(sigs, hist)
        moved = {s for s in sigs if (before.get(s) or {}).get("date") != (after.get(s) or {}).get("date")}
        assert moved == set(), moved

    def test_does_not_truncate_dim_ic_h1_or_h2(self):
        sys.path.insert(0, str(_ROOT / "experiments"))
        import dim_ic_forward_test as F
        import dim_ic_protocol as P
        import ic_rerun_readiness as rr
        entry = next(e for e in rr._COHORT_HISTORY if e[1] == _V)
        for name, _obj, sigs in P.HYPOTHESES:
            assert F.truncation_point(sigs, P, [entry]) is None, name

    def test_ml_estimator_generation_label_extended(self):
        import probability_scorecard as ps
        assert any(d == "2026-10-19" and _V in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)

    def test_guard_success_path_writes_the_marker_unconditionally(self):
        """GuardBee 的 `AgentResult(details={...})` 字面量里无条件写着印记。变红的变异：删掉那一行 / 挪进 if。"""
        tree = ast.parse((_ROOT / "swarm_agents" / "guard_bee.py").read_text(encoding="utf-8"))
        found = []
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "AgentResult":
                for kw in call.keywords:
                    if kw.arg == "details" and isinstance(kw.value, ast.Dict):
                        found += [v for k, v in zip(kw.value.keys, kw.value.values)
                                  if isinstance(k, ast.Constant) and k.value == _MARK]
        assert len(found) == 1 and isinstance(found[0], ast.Constant) and found[0].value is True, found

    def test_frozen_evidence_says_what_the_entry_claims(self):
        """真实证据文件：10-19 ~ 10-23 每天 11 只齐全、预取包全覆盖；10-26（旧代码最后一轮）整轮 yfinance 0 次成功响应；
        负对照 10-14（0 只）/ 10-05（9 只）/ 10-12（预取包 0/35）不算。等价窗口 10-19 起的每个旧代码扫描日都必须证得出。"""
        import ic_rerun_readiness as rr
        doc = json.loads((_ROOT / "experiments" / "macro_rotation_equivalence_20261019.json").read_text(encoding="utf-8"))
        days = doc["days"]
        for d in ("2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"):
            assert days[d]["status"] == "equivalent" and days[d]["sector_rotation_full"] == 11, d
            assert days[d]["prefetch_bundle"] and all(r == "35/35" for r in days[d]["prefetch_bundle"]), d
        gate = days["2026-10-26"]["yfinance_gate"]
        assert days["2026-10-26"]["status"] == "equivalent_no_yfinance_data" and gate["calls"] == gate["rate_limited"] > 0
        assert {d: v["status"] for d, v in days.items() if v["status"] not in rr._MACRO_ROTATION_EQUIV_STATUSES} == {
            "2026-10-05": "not_proven", "2026-10-12": "not_proven", "2026-10-14": "not_proven"}
        assert all(v["status"] in rr._MACRO_ROTATION_EQUIV_STATUSES for d, v in days.items() if d >= "2026-10-19")
        assert (_ROOT / doc["generator"]).exists()


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    """合成证据文件（判据 / 判定的测试不依赖真实文件里哪天是什么状态）。返回写入函数。"""
    import ic_rerun_readiness as rr
    path = tmp_path / "macro_rotation_evidence.json"
    monkeypatch.setattr(rr, "_MACRO_ROTATION_EVIDENCE_PATH", path)

    def write(statuses):
        path.write_text(json.dumps({"days": {d: {"status": s} for d, s in statuses.items()}}), encoding="utf-8")
    write({})
    return write


class TestMarkerAndJudge:

    @pytest.mark.parametrize("d", [{}, {"swarm_results": _row()}, {"swarm_results": _row(new=True, backfill=True)},
                                   {"swarm_results": _row(guard="errored")}],
                             ids=["empty", "old_code", "backfill", "errored"])
    def test_marker_only_on_live_new_rows(self, d):
        import ic_rerun_readiness as rr
        assert rr._marker_guard_macro_event_rows(d) is False
        assert rr._marker_guard_macro_event_rows({"swarm_results": _row(new=True)}) is True

    @pytest.mark.parametrize("status,row,expect", [
        ("equivalent", _row(), True),
        ("equivalent_no_yfinance_data", _row(), True),
        ("not_proven", _row(), False),
        (None, _row(), False),                                    # 证据里没有这天 ⇒ 证不出
        (None, _row(guard="errored", rival="errored"), True),
        (None, _row(guard="absent", rival="absent"), True),
        (None, _row(guard="errored"), False),                     # Rival 还在
    ], ids=["equivalent", "no_yfinance_data", "not_proven", "no_evidence", "both_errored", "both_absent",
            "rival_still_there"])
    def test_judge(self, evidence, status, row, expect):
        """变红的变异：判据恒真 / 不看证据状态 / 只认 equivalent 一种 / 只看 Guard 不看 Rival。"""
        import ic_rerun_readiness as rr
        evidence({} if status is None else {"2026-10-21": status})
        assert rr._equiv_macro_event_rows({"swarm_results": row, "_date": "2026-10-21", "_ticker": "AAA"}) is expect

    def test_unreadable_evidence_proves_nothing(self, evidence):
        import ic_rerun_readiness as rr
        rr._MACRO_ROTATION_EVIDENCE_PATH.write_text("{not json", encoding="utf-8")
        assert rr._equiv_macro_event_rows({"swarm_results": _row(), "_date": "2026-10-21", "_ticker": "AAA"}) is False


class TestBoundaryVerdict:

    def test_before_deploy_is_no_evidence_yet(self, tmp_path, evidence):
        import ic_rerun_readiness as rr
        evidence({"2026-10-19": "equivalent", "2026-10-26": "equivalent_no_yfinance_data"})
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-26", {"AAA": _row(), "BBB": _row(guard="errored")})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_on_deploy_day_matches(self, tmp_path, evidence):
        """变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.652"]`（⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        evidence({"2026-10-19": "equivalent", "2026-10-26": "equivalent_no_yfinance_data"})
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-26", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-27", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-10-27", ev

    def test_old_code_on_an_unproven_day_is_too_early(self, tmp_path, evidence):
        """新代码没赶上某个扫描日、证据又证不出那天等价 ⇒ 报出来，不当作等价。"""
        import ic_rerun_readiness as rr
        evidence({"2026-10-19": "equivalent"})
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-27", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-28", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-10-27"], ev


# ══════════════════════════════════════════════════════════════════════════
# 8. v0.45.658：Guard 世代印记出错也写（二次检查 #3）+ 本版的世代边界登记
# ══════════════════════════════════════════════════════════════════════════

def _guard_success_literal_marks():
    tree = ast.parse((_ROOT / "swarm_agents" / "guard_bee.py").read_text(encoding="utf-8"))
    keys = []
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "AgentResult":
            for kw in call.keywords:
                if kw.arg == "details" and isinstance(kw.value, ast.Dict):
                    # 认「值是字面量 True」，不认键名后缀（660 的印记叫 `macro_fred_series_isolated`，按 `*_checked` 认会漏）
                    keys += [k.value for k, v in zip(kw.value.keys, kw.value.values)
                             if isinstance(k, ast.Constant) and isinstance(v, ast.Constant) and v.value is True]
    return keys


class TestGuardGenerationMarks:

    def test_registry_matches_the_success_literal_both_ways(self):
        """成功路径 details 字面量里每个「值为字面量 True」的键（世代印记）都在 `GENERATION_MARKS`（出错路径据它补写），反之亦然。
        变红的变异：新加一个印记只写在成功路径（下一个版本最容易犯的那种）。"""
        from swarm_agents import guard_bee
        lit = _guard_success_literal_marks()
        assert len(lit) == len(set(lit)) and set(lit) == set(guard_bee.GENERATION_MARKS), (lit, guard_bee.GENERATION_MARKS)

    @pytest.mark.parametrize("snapshot,mode", [(None, "realtime"), ({"date": "2026-10-20"}, "backfill")])
    def test_error_path_writes_marks_and_mode(self, monkeypatch, snapshot, mode):
        """变红的变异：出错返回不经 `_with_generation_marks`（此前就是这样 ⇒ 新代码出错的行被当成旧代码）。"""
        import fred_macro
        from pheromone_board import PheromoneBoard
        from swarm_agents import guard_bee
        g = guard_bee.GuardBeeSentinel(PheromoneBoard())
        monkeypatch.setattr(g, "_get_history_context", lambda t: (_ for _ in ()).throw(ValueError("boom")))
        fred_macro.set_macro_snapshot((snapshot or {}).get("date"))
        try:
            out = g.analyze("NVDA")
        finally:
            fred_macro.set_macro_snapshot(None)
        assert out.get("error") and out["details"]["macro_as_of_mode"] == mode
        assert all(out["details"][k] is True for k in guard_bee.GENERATION_MARKS)

    def test_invalid_ticker_path_writes_marks(self):
        from pheromone_board import PheromoneBoard
        from swarm_agents import guard_bee
        out = guard_bee.GuardBeeSentinel(PheromoneBoard()).analyze("not a ticker")
        assert out.get("error") and all(out["details"][k] is True for k in guard_bee.GENERATION_MARKS)

    def test_backfill_error_row_is_not_date_evidence(self):
        """出错行的口径写在 details 顶层：补跑时出错的新代码行不能当日期证据（否则把印记首见日拉早）。"""
        import ic_rerun_readiness as rr
        det = {k: True for k in ("macro_event_rows_checked", _MARK658)}
        row = {"agent_details": {"GuardBeeSentinel": {"error": "x", "details": {**det, "macro_as_of_mode": "backfill"}}}}
        assert rr._is_backfill_row({"swarm_results": row}) is True
        assert rr._marker_guard_yf_download({"swarm_results": row}) is False
        row["agent_details"]["GuardBeeSentinel"]["details"]["macro_as_of_mode"] = "realtime"
        assert rr._marker_guard_yf_download({"swarm_results": row}) is True


def _row658(*, new=False, guard="ok", rival="ok", scout="ok"):
    agents = {}
    if guard == "ok":
        det = {"macro_adj": 0.0}
        if new:
            det.update({_MARK: True, _MARK658: True})
        agents["GuardBeeSentinel"] = {"score": 5.0, "details": det}
    elif guard == "errored_new":                          # 新代码出错：`_with_generation_marks` 补写的样子
        from swarm_agents import guard_bee
        agents["GuardBeeSentinel"] = {"score": 5.0, "error": "TypeError: boom",
                                      "details": {**{k: True for k in guard_bee.GENERATION_MARKS},
                                                  "macro_as_of_mode": "realtime"}}
    elif guard == "errored":
        agents["GuardBeeSentinel"] = {"score": 5.0, "details": {}, "error": "TypeError: boom"}
    for name, st in (("RivalBeeVanguard", rival), ("ScoutBeeNova", scout)):
        if st == "ok":
            agents[name] = {"score": 5.0, "details": {}}
        elif st == "errored":
            agents[name] = {"score": 5.0, "details": {}, "error": "TypeError: boom"}
    return {"agent_details": agents}


@pytest.fixture
def evidence658(tmp_path, monkeypatch):
    import ic_rerun_readiness as rr
    path = tmp_path / "yf_download_evidence.json"
    monkeypatch.setattr(rr, "_YF_DOWNLOAD_EVIDENCE_PATH", path)

    def write(statuses):
        path.write_text(json.dumps({"days": {d: {"status": s} for d, s in statuses.items()}}), encoding="utf-8")
    write({})
    return write


class TestBoundary658Registered:

    def test_entry_scope_marker_equivalence(self):
        import ic_rerun_readiness as rr
        import signal_archive as sa
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V658] == ["2026-10-19"]
        assert sa.COHORT_SIGNAL_SCOPE[_V658] == ("agent.GuardBeeSentinel.*",)
        assert _V658 in rr._BOUNDARY_MARKERS and _V658 in rr._BOUNDARY_EQUIVALENCE

    def test_moves_no_signal_generation(self):
        """变红的变异：作用域里加上 `agent.RivalBeeVanguard.*` / `agent.ScoutBeeNova.*`（证据窗口里没有一天受影响 ⇒ 不点名）。"""
        import signal_archive as sa
        hist = sa._cohort_history()
        sigs = set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)
        without = [e for e in hist if e[1] != _V658]
        before, after = sa.generation_boundaries(sigs, without), sa.generation_boundaries(sigs, hist)
        moved = {s for s in sigs if (before.get(s) or {}).get("date") != (after.get(s) or {}).get("date")}
        assert moved == set(), moved

    def test_does_not_truncate_dim_ic_h1_or_h2(self):
        sys.path.insert(0, str(_ROOT / "experiments"))
        import dim_ic_forward_test as F
        import dim_ic_protocol as P
        import ic_rerun_readiness as rr
        entry = next(e for e in rr._COHORT_HISTORY if e[1] == _V658)
        for name, _obj, sigs in P.HYPOTHESES:
            assert F.truncation_point(sigs, P, [entry]) is None, name

    def test_ml_estimator_generation_label_extended(self):
        import probability_scorecard as ps
        assert any(d == "2026-10-19" and _V658 in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)

    def test_frozen_evidence_says_what_the_entry_claims(self):
        """真实证据文件：DELL 10-20 除息是唯一落在扫描日的事件，当天扫描记下的现价 = 10-20 收盘；其余旧代码扫描日读者宇宙里没有事件。
        10-19 起的每个旧代码扫描日都必须证得出。"""
        import ic_rerun_readiness as rr
        doc = json.loads((_ROOT / "experiments" / "yf_daily_download_equivalence_20261019.json").read_text(encoding="utf-8"))
        days = doc["days"]
        assert days and all(v["status"] in rr._YF_DOWNLOAD_EQUIV_STATUSES for v in days.values()), days
        chk = days["2026-10-20"]
        assert chk["status"] == "equivalent_bar_present" and chk["event_pairs"] == [["DELL", "2026-10-20"]]
        assert chk["bar_checks"]["DELL"]["bar_present"] is True
        assert {d for d, v in days.items() if v["status"] == "equivalent"} >= {"2026-10-19", "2026-10-21", "2026-10-22",
                                                                               "2026-10-23", "2026-10-26"}
        assert len(doc["universe"]) >= 40 and (_ROOT / doc["generator"]).exists()


class TestJudge658:

    @pytest.mark.parametrize("status,row,expect", [
        ("equivalent", _row658(), True),
        ("equivalent_bar_present", _row658(), True),
        ("not_proven", _row658(), False),
        (None, _row658(), False),
        (None, _row658(guard="errored", rival="errored", scout="errored"), True),
        (None, _row658(guard="absent", rival="absent", scout="absent"), True),
        (None, _row658(guard="errored", rival="errored"), False),          # Scout 还在
    ], ids=["equivalent", "bar_present", "not_proven", "no_evidence", "all_errored", "all_absent", "scout_still_there"])
    def test_judge(self, evidence658, status, row, expect):
        import ic_rerun_readiness as rr
        evidence658({} if status is None else {"2026-10-21": status})
        assert rr._equiv_yf_download({"swarm_results": row, "_date": "2026-10-21", "_ticker": "AAA"}) is expect

    def test_unreadable_evidence_proves_nothing(self, evidence658):
        import ic_rerun_readiness as rr
        rr._YF_DOWNLOAD_EVIDENCE_PATH.write_text("{not json", encoding="utf-8")
        assert rr._equiv_yf_download({"swarm_results": _row658(), "_date": "2026-10-21", "_ticker": "AAA"}) is False


class TestVerdict658:

    def test_before_deploy_is_no_evidence_yet(self, tmp_path, evidence658):
        import ic_rerun_readiness as rr
        evidence658({"2026-10-19": "equivalent", "2026-10-20": "equivalent_bar_present"})
        _write(tmp_path, "2026-10-19", {"AAA": _row658(), "BBB": _row658()})
        _write(tmp_path, "2026-10-20", {"AAA": _row658(), "BBB": _row658()})
        assert rr.cohort_boundary_evidence(tmp_path, version=_V658)["verdict"] == "no_evidence_yet"

    def test_guard_error_on_the_deploy_day_still_matches(self, tmp_path, evidence658):
        """二次检查 #3 的回归：新代码首跑那天某只票 Guard 出错——它也带印记（出错路径补写）⇒ 认成新代码，不去查证据。
        变红的变异：出错路径不补写印记（⇒ 那一行被当成旧代码、证据里没有这天 ⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        evidence658({"2026-10-19": "equivalent"})
        _write(tmp_path, "2026-10-19", {"AAA": _row658(), "BBB": _row658()})
        _write(tmp_path, "2026-10-28", {"AAA": _row658(new=True), "BBB": _row658(guard="errored_new")})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V658)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-10-28", ev

    def test_old_code_on_an_unproven_day_is_too_early(self, tmp_path, evidence658):
        import ic_rerun_readiness as rr
        evidence658({"2026-10-19": "equivalent"})
        _write(tmp_path, "2026-10-19", {"AAA": _row658(), "BBB": _row658()})
        _write(tmp_path, "2026-10-28", {"AAA": _row658(), "BBB": _row658()})
        _write(tmp_path, "2026-10-29", {"AAA": _row658(new=True), "BBB": _row658(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V658)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-10-28"], ev


def test_entry_module_does_not_import_data_pipeline():
    """入口不依赖 data_pipeline（cache.py 的旧回落正是在它不可用时才跑，同 price_metrics 的约束）。"""
    tree = ast.parse((_ROOT / "yf_daily.py").read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "data_pipeline" not in names and "price_metrics" in names
