"""补跑时宏观必须是**目标日**的（v0.45.59）

用户问：「接上 market.json：让补跑的宏观走快照而不是运行当天」。

修之前有两处错位，方向相同：
  · VIX / F&G —— `market.json` 里存着目标日的真实观测，但 `load_market()`
    在生产代码里**一个调用者都没有**（「死字段：算了没人读」）
  · 国债 / SPX / 美元 / 黄金 —— `yf.Ticker(sym).history(period="5d")`
    永远取**最近** 5 天，与目标日无关

结果：8/27 的报告里宏观是 8/28 的，且没有任何标记说明这一点。
本文件锁住三条：取数对齐目标日、VIX 走快照、标签如实写明口径。
"""

import datetime as dt

import pytest

import fred_macro as fm

# conftest 的 `_block_same_day_macro` 会把 `fm._same_day_macro_data` 换成桩
# （防止测试打真网络）。要验证**它本身**的行为就得拿到真函数 ——
# 模块加载早于 autouse fixture，此刻抓到的还是原件。
_REAL_SAME_DAY = fm._same_day_macro_data


@pytest.fixture(autouse=True)
def _clean():
    fm.set_macro_snapshot(None)
    yield
    fm.set_macro_snapshot(None)


MARKET_827 = {
    "cboe": {"vix_term": {"vix_spot": 15.21, "vix_1m": 17.15, "source": "vx_futures"}},
    "fear_greed": {"value": 58, "classification": "Greed", "is_real_data": True},
}


class _FakeHist:
    """最小 DataFrame 替身：支持 .empty / .index.date / ["Close"].iloc / 布尔掩码。

    ⚠️ `.date` 必须返回 **numpy 数组**而非 list —— 真 pandas 的
    `DatetimeIndex.date` 是数组，`arr <= date` 是逐元素比较；换成 list 会
    抛 TypeError，于是测试红的是替身、不是被测代码。第一版就栽在这里。
    """

    def __init__(self, rows):           # rows: [(date, close)]
        self._rows = sorted(rows)

    @property
    def empty(self):
        return not self._rows

    def __len__(self):
        # 取数循环里有 `len(hist) >= 2`；替身缺 __len__ 会抛 TypeError，
        # 被 `except Exception` 吞掉 → data 空 → 走「yfinance 全灭」分支，
        # 于是测试红在一条与被测逻辑无关的路径上。第二版栽在这里。
        return len(self._rows)

    @property
    def index(self):
        import numpy as np
        rows = self._rows

        class _Idx:
            date = np.array([d for d, _ in rows], dtype=object)

            def __getitem__(self, i):
                class _Stamp:
                    def __init__(self, d): self._d = d
                    def date(self): return self._d
                return _Stamp(rows[i][0])

        return _Idx()

    def __getitem__(self, k):
        if isinstance(k, str) and k == "Close":
            class _Col:
                def __init__(self, vals): self._v = vals
                @property
                def iloc(self): return self._v
                def __getitem__(self, i): return self._v[i]
            return _Col([c for _, c in self._rows])
        return _FakeHist([r for r, keep in zip(self._rows, list(k)) if keep])


class TestProviderLifecycle:
    def test_set_and_clear(self):
        assert fm.get_macro_snapshot() is None
        fm.set_macro_snapshot("2026-08-27", MARKET_827)
        assert fm.get_macro_snapshot()["date"] == "2026-08-27"
        fm.set_macro_snapshot(None)
        assert fm.get_macro_snapshot() is None

    def test_setting_invalidates_cache(self):
        """口径变了旧缓存必须作废，否则补跑会拿到上一次实时调用的结果。"""
        fm._CACHE, fm._CACHE_TS = {"vix": 99.0}, 9e9
        fm.set_macro_snapshot("2026-08-27", MARKET_827)
        assert fm._CACHE is None and fm._CACHE_TS == 0.0


class TestAsOfHistory:
    def test_truncates_to_target_date(self):
        """核心：末根不得晚于 as_of。旧实现 period='5d' 永远给最近的。"""
        rows = [(dt.date(2026, 8, 25), 4.639),
                (dt.date(2026, 8, 26), 4.664),
                (dt.date(2026, 8, 27), 4.672)]

        class _T:
            def history(self, **k):
                return _FakeHist(rows)

        class _YF:
            Ticker = staticmethod(lambda s: _T())

        h = fm._asof_history(_YF, "^TNX", "2026-08-26")
        assert h.index.date[-1] == dt.date(2026, 8, 26)
        assert h["Close"].iloc[-1] == 4.664

    def test_uses_date_window_not_period(self):
        """必须用 start/end；`period=` 无法定位历史某日。"""
        seen = {}

        class _T:
            def history(self, **k):
                seen.update(k)
                return _FakeHist([(dt.date(2026, 8, 27), 1.0)])

        class _YF:
            Ticker = staticmethod(lambda s: _T())

        fm._asof_history(_YF, "^TNX", "2026-08-27")
        assert "start" in seen and "end" in seen
        assert "period" not in seen, "period= 取的是最近 N 天，与目标日无关"
        assert seen["start"] < "2026-08-27" <= seen["end"]

    def test_empty_returns_none(self):
        class _T:
            def history(self, **k):
                return _FakeHist([])

        class _YF:
            Ticker = staticmethod(lambda s: _T())

        assert fm._asof_history(_YF, "^TNX", "2026-08-27") is None


@pytest.mark.network  # 走真实取数路径（yfinance/Treasury），离线必挂；CI 排除，本机照跑
class TestMacroContextUsesSnapshot:
    def _patch(self, monkeypatch, *, close=4.5):
        """把 yfinance 取数替换掉，只验快照逻辑，不打网。"""
        rows = [(dt.date(2026, 8, 26), close), (dt.date(2026, 8, 27), close)]
        monkeypatch.setattr(fm, "_asof_history",
                            lambda yf, sym, as_of: _FakeHist(rows))

    def test_vix_comes_from_snapshot_and_is_labelled(self, monkeypatch):
        """VIX 必须来自快照，且 vix_source 要能区分「今天问的」与「快照里的」。

        两者同形（都是 float），标成同一个 "cboe" 就无从分辨报告里的 VIX
        属于哪一天 —— 正是 MEMORY「读 vix 前先看 vix_source」要防的事。
        """
        self._patch(monkeypatch)
        monkeypatch.setattr(fm, "_fetch_fred_series", lambda *a, **k: {}, raising=False)
        fm.set_macro_snapshot("2026-08-27", MARKET_827)
        r = fm.get_macro_context()
        assert r["vix"] == 15.21
        assert r["vix_source"] == "cloud_snapshot_cboe"
        assert r["as_of"] == "2026-08-27"
        # v0.45.92：mode 必须跟着走。只断言 as_of 的话，把 as_of_mode 写死成
        # "realtime" 也能全绿 —— 实测过，这正是一条抓不住的假守卫。
        assert r["as_of_mode"] == "backfill", "补跑口径必须自称 backfill"
        assert "2026-08-27" in r["data_source"], "data_source 必须写明口径日"
        assert r["data_source"] != "yfinance", "补跑标成实时口径会误导读者"

    def test_no_snapshot_keeps_live_semantics(self, monkeypatch):
        """回归：不装快照时不得走补跑取数路径，标签也不得写成补跑。

        v0.45.92 更新：原先这里断言 `as_of is None` —— 那是拿"哨兵为空"
        当"实时口径"的**代理判断**。实时路径现在也会填 as_of（数据真正代表
        的那个交易日），区分改由 `as_of_mode` 显式承载，故代理判断作废。
        本测试真正守的那条不变式（`_asof_history` 不得被调用）原封不动。
        """
        called = {"n": 0}
        monkeypatch.setattr(fm, "_asof_history",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        fm.set_macro_snapshot(None)
        r = fm.get_macro_context()
        assert called["n"] == 0, "未装快照却走了 as_of 取数路径"
        assert r.get("as_of_mode") != "backfill", "未装快照却自称补跑口径"
        # 反向：实时口径绝不能带上补跑才有的标签，否则又是一个假标签
        assert "cloud_snapshot" not in str(r.get("data_source", "")), \
            "实时口径不得写 cloud_snapshot"
        assert "@" not in str(r.get("data_source", "")), \
            "实时口径的 data_source 不该带 @日期 后缀"

    def test_no_snapshot_still_stamps_a_date(self, monkeypatch):
        """实时口径也要有日期戳 —— 这是 v0.45.92 补上的那件事本身。

        时钟冻在 2026-09-01 17:00 ET（定时任务的真实时点，已收盘），
        所以 as_of 必须是当天，且 mode 明写 realtime。
        """
        import datetime as dt
        from zoneinfo import ZoneInfo
        fixed = dt.datetime(2026, 9, 1, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr("cboe_options._et_now", lambda: fixed)
        monkeypatch.setattr(fm, "_asof_history", lambda *a, **k: None)
        fm.set_macro_snapshot(None)
        r = fm.get_macro_context()
        if r.get("as_of_mode") == "fallback":
            import pytest as _p
            _p.skip("宏观取数整体降级，本条只测非降级路径")
        assert r.get("as_of_mode") == "realtime"
        assert r.get("as_of") == "2026-09-01"

    def test_snapshot_without_vix_does_not_fake_one(self, monkeypatch):
        """快照里没有 vix_term 时不得凭空造一个 —— 退回既有降级链。"""
        self._patch(monkeypatch)
        fm.set_macro_snapshot("2026-08-27", {"cboe": {}})
        r = fm.get_macro_context()
        assert r["vix_source"] != "cloud_snapshot_cboe"


class TestSnapshotModeInstallsMacro:
    """宏观与期权链必须同进同出 —— 只装一半会让期权是目标日的、宏观是今天的。"""

    def test_installs_and_uninstalls(self, monkeypatch):
        import cloud_snapshot_loader as csl

        monkeypatch.setattr(csl, "load_manifest",
                            lambda *a, **k: {"ok": ["NVDA"], "tickers_ok": 1})
        monkeypatch.setattr(csl, "load_ticker", lambda *a, **k: {"ticker": "NVDA"})
        monkeypatch.setattr(csl, "load_market", lambda *a, **k: MARKET_827)

        assert fm.get_macro_snapshot() is None
        with csl.snapshot_mode("2026-08-27"):
            snap = fm.get_macro_snapshot()
            assert snap is not None and snap["date"] == "2026-08-27"
        assert fm.get_macro_snapshot() is None, "退出 with 后宏观快照必须卸载"


class TestSameDayWithoutYfinance:
    """当日扫描的宏观必须能完全脱离 yfinance（v0.45.60）。

    8/27：yfinance 687 次 429 → 7 个宏观符号一起归零 → `data_source: "fallback"`，
    报告里 `treasury_10y: 4.5` 是兜底常量。替代源必须**当天就能出数**
    （FRED 滞后 1–2 天，当日 17:00 ET 的定时任务等不到它）。
    """

    @pytest.mark.network  # 打真实外部端点，离线必挂；CI 排除，本机照跑
    def test_same_day_prefers_non_yfinance(self, monkeypatch):
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (
            {"TNX": {"last": 4.67, "prev": 4.67, "change_pct": 0.0},
             "TWO": {"last": 4.20, "prev": 4.20, "change_pct": 0.0},
             "SPX": {"last": 771.10, "prev": 766.08, "change_pct": 0.66}},
            {"TNX": "treasury_gov@2026-08-27", "TWO": "treasury_gov@2026-08-27",
             "SPX": "finnhub:SPY"}))
        seen = []
        monkeypatch.setattr(fm, "_asof_history",
                            lambda yf, s, a: seen.append(s))
        fm.set_macro_snapshot(None)
        r = fm.get_macro_context()
        assert r["treasury_10y"] == 4.67
        assert "treasury" in r["data_source"] and "finnhub" in r["data_source"]
        assert r["field_sources"]["TNX"].startswith("treasury_gov")

    def test_backfill_asks_treasury_for_the_target_date(self, monkeypatch):
        """补跑要**目标日**的国债，不是最新的。

        财政部一次返回整月，指定日期不额外发请求。走 FRED 也对，但 FRED
        转发的就是这份数据且晚一天 —— 实测 8/28 查询时 FRED 的 2Y 是
        4.19@08-26，财政部已有 4.20@08-27。
        """
        seen = {}
        monkeypatch.setattr(fm, "_same_day_macro_data",
                            lambda as_of=None: (seen.__setitem__("as_of", as_of), ({}, {}))[1])
        monkeypatch.setattr(fm, "_asof_history", lambda *a, **k: None)
        fm.set_macro_snapshot("2026-08-27", MARKET_827)
        fm.get_macro_context()
        assert seen.get("as_of") == "2026-08-27", "补跑没把目标日传给取数层"

    def test_backfill_skips_etf_quotes(self, monkeypatch):
        """ETF 报价只给最新 —— 补跑时必须跳过，拿今天的 SPY 冒充目标日
        比缺失更坏。"""
        called = {"n": 0}
        monkeypatch.setattr(fm, "_finnhub_quote",
                            lambda s: called.__setitem__("n", called["n"] + 1))
        monkeypatch.setattr("treasury_yields.get_yield_curve", lambda *a, **k: None)
        _REAL_SAME_DAY("2026-08-27")
        assert called["n"] == 0, "补跑口径下调用了 Finnhub 实时报价"
        # 当日口径则应该调
        _REAL_SAME_DAY(None)
        assert called["n"] == len(fm._ETF_PROXY)

    @pytest.mark.network  # 打真实外部端点，离线必挂；CI 排除，本机照跑
    def test_real_2y_beats_the_5y_approximation(self, monkeypatch):
        """真 2Y 必须压过 `5Y + 0.15` 近似 —— 那个近似会**错判曲线档位**。

        2026-08-27 实测：5Y=4.38 → 近似 2Y=4.53 → 10Y−2Y=+14bp 判成 flat；
        真 2Y=4.20 → +47bp，实际是 normal。
        """
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (
            {"TNX": {"last": 4.67, "prev": 4.67, "change_pct": 0.0},
             "FVX": {"last": 4.38, "prev": 4.38, "change_pct": 0.0},
             "TWO": {"last": 4.20, "prev": 4.20, "change_pct": 0.0}},
            {"TNX": "treasury_gov", "FVX": "treasury_gov", "TWO": "treasury_gov"}))
        monkeypatch.setattr(fm, "_asof_history", lambda *a, **k: None)
        fm.set_macro_snapshot(None)
        r = fm.get_macro_context()
        assert r["treasury_2y"] == 4.2
        assert r["treasury_2y_source"] == "treasury_gov"
        assert r["yield_spread"] == 47.0
        assert r["yield_curve"] == "normal"

    @pytest.mark.network  # 打真实外部端点，离线必挂；CI 排除，本机照跑
    def test_falls_back_to_approximation_and_labels_it(self, monkeypatch):
        """财政部不可得时仍可用近似，但**必须标出来**。"""
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (
            {"TNX": {"last": 4.67, "prev": 4.67, "change_pct": 0.0},
             "FVX": {"last": 4.38, "prev": 4.38, "change_pct": 0.0}},
            {"TNX": "treasury_gov", "FVX": "treasury_gov"}))
        monkeypatch.setattr(fm, "_asof_history", lambda *a, **k: None)
        monkeypatch.setattr(fm, "_load_fred_key", lambda: "")
        fm.set_macro_snapshot(None)
        r = fm.get_macro_context()
        assert r["treasury_2y_source"] == "approx_from_5y"
        assert r["treasury_2y"] == 4.53

    def test_finnhub_quote_never_returns_zero(self, monkeypatch):
        """拿不到就 None。0.0 会被下游当成"持平"，与"没数据"不可区分。"""
        import data_pipeline
        monkeypatch.setattr(data_pipeline, "_get_secret", lambda n: "k")
        import http_gate
        monkeypatch.setattr(http_gate, "urlopen_gated",
                            lambda *a, **k: b'{"c":0,"pc":0}')
        assert fm._finnhub_quote("SPY") is None

    def test_data_source_never_claims_unused_yfinance(self, monkeypatch):
        """全部由非 yfinance 源供上时，data_source 不得再写 yfinance。

        8/27 那天 yfinance 一个字段都没供上，标签却仍是 `yfinance+fred` ——
        排查限流时会把人引向一个根本没被调用的源。
        """
        src = {"TNX": "treasury_gov", "SPX": "finnhub:SPY"}
        data = {"TNX": {}, "SPX": {}}
        out = fm._compose_data_source(None, src, data, has_fred=True)
        assert out == "treasury+finnhub+fred"
        assert "yfinance" not in out
        # 有字段是 yfinance 兜上来的时候，就必须写上
        data2 = {"TNX": {}, "SPX": {}, "GLD": {}}
        assert "yfinance" in fm._compose_data_source(None, src, data2, has_fred=False)


class TestSecondPassRegressions:
    """v0.45.61 二次检查抓到的三条。都是「已经修过同一形状、又犯一次」。"""

    def test_backfill_label_names_treasury_when_treasury_supplied(self):
        """补跑标签必须反映**实际**供数的源。

        原实现的补跑分支无条件写 `cloud_snapshot+yfinance@日期`，完全不看
        `src_map` —— 而自 v0.45.61 起补跑的国债已改走财政部，于是标签说
        yfinance、实际是 treasury。与 8/27 那次「yfinance 一个字段都没供上、
        标签却仍写 yfinance」是同一形状，在同一个函数里又犯一次。
        """
        src = {"TNX": "treasury_gov@2026-08-27", "FVX": "treasury_gov@2026-08-27"}
        out = fm._compose_data_source("2026-08-27", src,
                                      {"TNX": {}, "FVX": {}, "SPX": {}}, has_fred=True)
        assert "treasury" in out, f"财政部供了数却没进标签：{out}"
        assert "cloud_snapshot" in out and out.endswith("@2026-08-27")
        # 全 yfinance 时不得凭空写 treasury
        assert "treasury" not in fm._compose_data_source(
            "2026-08-27", {}, {"SPX": {}}, has_fred=False)

    def test_treasury_month_key_uses_eastern_not_local(self, monkeypatch):
        """月份键必须按美东取。

        本机在 PT，落后 ET 3 小时：每月 1 号的 ET 00:00~03:00 之间本机还停在
        上个月，会去抓上个月的 XML、找不到 ET 当日 → 返回 None。
        一年 12 次、每次 3 小时，正好覆盖凌晨的定时任务。
        """
        import treasury_yields as ty
        seen = {}
        monkeypatch.setattr(ty, "_fetch_month",
                            lambda mm, **k: seen.setdefault("mm", mm) and None)
        monkeypatch.setattr(ty, "_et_month", lambda: "202609")
        monkeypatch.setattr(ty.time, "strftime", lambda *_: "202608")  # 本机还在上月
        ty.clear_cache()
        ty.get_yield_curve()
        assert seen["mm"] == "202609", "用了本机月份而非美东月份"
