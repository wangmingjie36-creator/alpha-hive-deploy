"""v0.45.652：yfinance 日线的唯一取数入口 `yf_daily`——取到即去掉「公司行动空行」，别处不许直调 `.history(`。

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
import functools
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yf_daily  # noqa: E402

TZ = "America/New_York"
_V = "v0.45.652"
_MARK = "macro_event_rows_checked"


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


# ══════════════════════════════════════════════════════════════════════════
# 3. 调用点行为
# ══════════════════════════════════════════════════════════════════════════

class TestSectorRotation:
    """进分的那一条：11 只 SPDR 同日除息，首行空行此前让整张表变空。"""

    @pytest.fixture
    def rotation(self, monkeypatch):
        import fred_macro as fm
        monkeypatch.setattr(fm, "_etf_cache", {})
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

#: `.history(` 直调的存量例外：(文件, 所在函数的限定名) → (类别, 理由)。只许减。
#: 类别决定结构核对：dated_window 必须带 `start=`；intraday 必须 `interval="1m"`；needs_events 必须 `actions=True`。
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
    **{k: ("dated_window", "start= 窗口：yfinance 把事件截到首根 K 线当天起，首行不会有；历史窗口的 K 线都在，末行也碰不到")
       for k in (("backtest_engine.py", "PriceBackfiller._get_price_on_date"),
                 ("backtester.py", "Backtester._history"),
                 ("dynamic_exit_backtest.py", "_fetch_price_series"),
                 ("factor_attribution.py", "_get_stock_returns"),
                 ("outcomes_fetcher.py", "OutcomesFetcher._fetch_remote"),
                 ("paper_portfolio.py", "prefetch_ohlc"),
                 ("paper_portfolio.py", "_ReplayOhlcWindow._download"),
                 ("paper_portfolio.py", "_fetch_ohlc_direct"),
                 ("portfolio_backtest.py", "load_verified_predictions"),
                 ("portfolio_backtest.py", "_fetch_spy_prices"),
                 ("portfolio_factor_attribution.py", "_load_etf_factors"))},
}

#: 每个入口调用点的末行策略。refuse = 用末根当今天，必须读第二个返回值；ignore = 按日期用整段（或末根只是近似），明确不读。
POLICY = {
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
    ("alphabot/beta_long.py", "_fetch_closes"): "ignore",                          # 按日期对齐、只用 as_of 以前的收益；缺最近一根只是样本少一天
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
    """`(限定名, Call 节点)`：pred(call) 为真的每一处，限定名 = 外层类 / 函数名用点连起来。"""
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
    return isinstance(n.func, ast.Attribute) and n.func.attr == "history" and ast.unparse(n.func.value) == "yf_daily"


def _kw(n, name):
    return next((k.value for k in n.keywords if k.arg == name), None)


def _scan_history(trees):
    return {(rel, q): n for rel, t in trees.items() for q, n in _calls(t, _is_attr_call("history"))
            if not _is_entry_call(n)}


class TestSingleEntry:

    def test_direct_history_calls_are_only_the_registered_exceptions(self):
        """多一处直调 ⇒ 红（改走 yf_daily.history）；例外失效（被迁走 / 删了）⇒ 也红（从 EXEMPT 删掉，只许减）。"""
        found = _scan_history(_prod_trees())
        inside = {k for k in found if k[0] == "yf_daily.py"}
        assert len(inside) == 1, inside
        assert set(found) - inside == set(EXEMPT)

    def test_exceptions_still_have_the_shape_that_justifies_them(self):
        """变红的变异：把某个 start= 窗口改成 period=、把 1m 改成 1d、去掉 actions=True。"""
        found = _scan_history(_prod_trees())
        for key, (cat, why) in EXEMPT.items():
            n = found[key]
            if cat == "dated_window":
                assert _kw(n, "start") is not None, (key, why)
            elif cat == "intraday":
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

    def test_no_download_asks_for_actions(self):
        """`yf.download` 缺省 `actions=False`（yfinance 自己删空行）；显式要事件列就把空行请回来了。"""
        bad = [(rel, q) for rel, t in _prod_trees().items()
               for q, n in _calls(t, _is_attr_call("download"))
               if isinstance(_kw(n, "actions"), ast.Constant) and _kw(n, "actions").value is True]
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
        """refuse 的调用点必须读第二个返回值；ignore 的必须不读（变量以 `_` 开头且没被读，或只取 `[0]`）。
        变红的变异：板块轮动删掉 `_last_blank or`；HV 开始读它。"""
        trees = _prod_trees()
        for (rel, q), policy in POLICY.items():
            fn = next(n for qq, n in _scopes(trees[rel]) if qq == q)
            flag = _flag_name(fn)
            loads = {x.id for x in ast.walk(fn) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
            if policy == "refuse":
                assert flag and flag in loads, (rel, q, flag)
            else:
                assert flag is None or (flag.startswith("_") and flag not in loads), (rel, q, flag)

    def test_scan_has_teeth(self):
        """自证：扫到了足够多的生产文件（与 git 清单对账），且在一段违规源码上会命中、对入口调用不会误报。"""
        import subprocess
        trees = _prod_trees()
        r = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=str(_ROOT), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[:200]
        tracked = [x for x in r.stdout.split("\x00") if x and not (_PROD_SKIP & set(Path(x).parts[:-1]))
                   and not any(part.startswith(".") for part in Path(x).parts)]
        assert len(tracked) >= 100 and len(trees) >= 0.4 * len(tracked), (len(trees), len(tracked))
        assert {"fred_macro.py", "options_analyzer.py", "swarm_agents/base.py", "yf_daily.py"} <= set(trees)
        bad = ast.parse("def f(yf):\n    h = yf.Ticker('X').history(period='5d')\n"
                        "    ok, last = yf_daily.history(yf.Ticker('X'), source='options_hv', period='5d')\n")
        assert list(_scan_history({"bad.py": bad})) == [("bad.py", "f")]


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


def _flag_name(fn):
    """`a, flag = yf_daily.history(...)` 里 flag 的名字；`yf_daily.history(...)[0]` ⇒ None。只看本函数自己的语句（不进嵌套函数）。"""
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _is_entry_call(node.value)
                and isinstance(node.targets[0], ast.Tuple)):
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
        assert "其他日线读者去掉公司行动空行 2 行(首行 1, 末行 1, 中间 0)" in line
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


def test_entry_module_does_not_import_data_pipeline():
    """入口不依赖 data_pipeline（cache.py 的旧回落正是在它不可用时才跑，同 price_metrics 的约束）。"""
    tree = ast.parse((_ROOT / "yf_daily.py").read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "data_pipeline" not in names and "price_metrics" in names
