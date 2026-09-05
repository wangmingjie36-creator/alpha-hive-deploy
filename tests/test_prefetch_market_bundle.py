"""预取即一轮扫描的唯一 yfinance 取数点（v0.45.122）

固化的事实：2026-09-04 同一只票的 `.info` 被 Scout / Rival / Bear 各取一次、
`.history` 被 Rival 取一次 + Scout 再 download 一次、Oracle 三处各自抓到期日列表
与近月链；每次都过 yf_gate 0.5 req/s 的闸。30 只 × ~14 次 ≈ 420 次排队。

这里守五条：
  1. 访问器：有预取就用预取、没有就**原样**直连（回退路径 = 改动前路径）
  2. 预取包：一次 download 覆盖标的 + 板块 ETF；单项失败只缺席，不放占位值
  3. 四只蜂的迁移点：注入完整预取包后 `yfinance.Ticker` / `download` 零调用
  4. Oracle 三处共用一份 memo：Ticker 建 1 次、options 读 1 次、每个到期日链只抓 1 次
  5. 静态守卫：swarm_agents/ 里不许再长出新的 yfinance 直连
"""

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, PropertyMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yfinance

from swarm_agents import base as B
from swarm_agents.base import (MARKET_HISTORY_PERIOD, _period_to_days, _sector_etfs_for,
                               _slice_history_period, inject_prefetched, prefetch_market_bundle)
from pheromone_board import PheromoneBoard


# ───────────────────────────────────────────── 合成数据
def _ohlc(periods: int, base: float, step: float) -> pd.DataFrame:
    """以今天为末根、往回 periods 个工作日的 OHLC（tz=纽约，和 yfinance 一样）。"""
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=periods, freq="B",
                        tz="America/New_York")
    periods = len(idx)          # end 落在周末时 pandas 会少给一根，以实际索引长度为准
    close = [base + step * i for i in range(periods)]
    return pd.DataFrame({"Open": [c - 0.5 for c in close], "High": [c + 1 for c in close],
                         "Low": [c - 1 for c in close], "Close": close,
                         "Volume": [1_000_000] * periods}, index=idx)


def _counting_yf(monkeypatch, ticker_obj=None):
    """把 yfinance.Ticker / download 换成计数替身；返回 (Ticker mock, download mock)。"""
    tk = MagicMock(return_value=ticker_obj or MagicMock())
    dl = MagicMock(return_value=pd.DataFrame())
    monkeypatch.setattr(yfinance, "Ticker", tk)
    monkeypatch.setattr(yfinance, "download", dl)
    return tk, dl


class _Bee(B.BeeAgent):
    def analyze(self, ticker):  # pragma: no cover - 只为实例化基类
        return {}


def _bee(market=None):
    b = _Bee(PheromoneBoard())
    b._prefetched_market = market or {}
    return b


# ───────────────────────────────────────────── 1. period 解析与切片
class TestPeriodHelpers:
    @pytest.mark.parametrize("p,days", [("25d", 25), ("3mo", 90), ("1y", 365), ("5wk", 35),
                                        ("xx", None), (None, None), ("mo", None)])
    def test_period_to_days(self, p, days):
        assert _period_to_days(p) == days

    def test_same_period_returns_same_frame(self):
        df = _ohlc(70, 100, 1)
        assert _slice_history_period(df, "3mo", "3mo") is df

    def test_shorter_period_slices_tail_by_calendar_days(self):
        df = _ohlc(70, 100, 1)
        sub = _slice_history_period(df, "25d", "3mo")
        cutoff = date.today() - timedelta(days=25)
        assert len(sub) < len(df) and all(d >= cutoff for d in sub.index.date)
        assert sub.index[-1] == df.index[-1], "切的是尾巴，末根必须保留"

    @pytest.mark.parametrize("want,have", [("1y", "3mo"), ("bogus", "3mo"), ("25d", None)])
    def test_longer_or_unknown_returns_none(self, want, have):
        assert _slice_history_period(_ohlc(10, 1, 1), want, have) is None


# ───────────────────────────────────────────── 2. 访问器：预取优先、落空直连
class TestAccessors:
    def test_info_hit_never_touches_network(self, monkeypatch):
        tk, _ = _counting_yf(monkeypatch)
        b = _bee({"info": {"NVDA": {"sector": "Technology"}}})
        assert b._yf_info("NVDA") == {"sector": "Technology"}
        tk.assert_not_called()

    def test_info_miss_falls_back_to_ticker_info(self, monkeypatch):
        obj = MagicMock(); obj.info = {"sector": "X"}
        tk, _ = _counting_yf(monkeypatch, obj)
        assert _bee({"info": {}})._yf_info("TSLA") == {"sector": "X"}
        tk.assert_called_once_with("TSLA")

    def test_calendar_and_analyst_targets_hit_and_miss(self, monkeypatch):
        obj = MagicMock(); obj.calendar = {"Earnings Date": []}; obj.analyst_price_targets = {"mean": 1}
        tk, _ = _counting_yf(monkeypatch, obj)
        b = _bee({"calendar": {"NVDA": {"c": 1}}, "analyst_targets": {"NVDA": {"mean": 9}}})
        assert b._yf_calendar("NVDA") == {"c": 1} and b._yf_analyst_targets("NVDA") == {"mean": 9}
        tk.assert_not_called()
        assert b._yf_calendar("TSLA") == {"Earnings Date": []}
        assert b._yf_analyst_targets("TSLA") == {"mean": 1}
        assert tk.call_count == 2

    def test_history_hit_same_period_is_identical_frame(self, monkeypatch):
        tk, _ = _counting_yf(monkeypatch)
        df = _ohlc(70, 100, 1)
        b = _bee({"history": {"NVDA": df}, "history_period": "3mo"})
        assert b._yf_history("NVDA", "3mo") is df
        tk.assert_not_called()

    def test_history_shorter_period_is_sliced(self, monkeypatch):
        tk, _ = _counting_yf(monkeypatch)
        b = _bee({"history": {"NVDA": _ohlc(70, 100, 1)}, "history_period": "3mo"})
        assert 10 <= len(b._yf_history("NVDA", "25d")) <= 20
        tk.assert_not_called()

    def test_history_longer_than_bundle_falls_back(self, monkeypatch):
        obj = MagicMock(); obj.history.return_value = pd.DataFrame({"Close": [1.0]})
        tk, _ = _counting_yf(monkeypatch, obj)
        b = _bee({"history": {"NVDA": _ohlc(70, 100, 1)}, "history_period": "3mo"})
        b._yf_history("NVDA", "1y")
        obj.history.assert_called_once_with(period="1y")

    def test_close_panel_both_present(self, monkeypatch):
        tk, dl = _counting_yf(monkeypatch)
        b = _bee({"history": {"NVDA": _ohlc(70, 100, 1), "XLK": _ohlc(70, 50, 0.5)},
                  "history_period": "3mo"})
        panel = b._yf_close_panel(["NVDA", "XLK"], "25d")
        assert list(panel.columns) == ["NVDA", "XLK"] and len(panel) >= 10
        dl.assert_not_called(); tk.assert_not_called()

    def test_close_panel_one_missing_falls_back_whole(self, monkeypatch):
        """不拼一半预取一半实时——两列口径必须同源。"""
        tk, dl = _counting_yf(monkeypatch)
        dl.return_value = pd.DataFrame({"Close": [1.0]})
        b = _bee({"history": {"NVDA": _ohlc(70, 100, 1)}, "history_period": "3mo"})
        b._yf_close_panel(["NVDA", "XLK"], "25d")
        dl.assert_called_once()
        assert dl.call_args.args[0] == ["NVDA", "XLK"] and dl.call_args.kwargs["period"] == "25d"


# ───────────────────────────────────────────── 3. 预取包
class TestPrefetchMarketBundle:
    def _ticker_factory(self, bad_info=()):
        def _mk(t):
            obj = MagicMock()
            if t in bad_info:
                type(obj).info = PropertyMock(side_effect=OSError("429"))
            else:
                obj.info = {"symbol": t, "trailingPE": 30.0}
            obj.calendar = {"Earnings Date": []}
            obj.analyst_price_targets = {"mean": 100.0}
            return obj
        return _mk

    def test_one_download_covers_tickers_and_sector_etfs(self, monkeypatch):
        tickers = ["NVDA", "TSLA"]
        etfs = _sector_etfs_for(tickers)
        assert etfs, "夹具前提：WATCHLIST 里这两只票映射得到板块 ETF"
        frames = {s: _ohlc(70, 100, 1) for s in set(tickers) | set(etfs)}
        dl = MagicMock(return_value=pd.concat(frames, axis=1))
        monkeypatch.setattr(yfinance, "download", dl)
        monkeypatch.setattr(yfinance, "Ticker", MagicMock(side_effect=self._ticker_factory()))

        bundle = prefetch_market_bundle(tickers)
        dl.assert_called_once()
        kw = dl.call_args.kwargs
        assert kw["tickers"] == sorted(set(tickers) | set(etfs))
        assert kw["period"] == MARKET_HISTORY_PERIOD and kw["group_by"] == "ticker"
        assert set(bundle["history"]) == set(tickers) | set(etfs)
        assert set(bundle["info"]) == set(tickers) and set(bundle["calendar"]) == set(tickers)
        assert set(bundle["analyst_targets"]) == set(tickers)
        assert bundle["history_period"] == MARKET_HISTORY_PERIOD

    def test_failed_item_is_absent_not_placeholder(self, monkeypatch):
        monkeypatch.setattr(yfinance, "download", MagicMock(side_effect=OSError("down")))
        monkeypatch.setattr(yfinance, "Ticker", MagicMock(side_effect=self._ticker_factory(bad_info={"TSLA"})))
        bundle = prefetch_market_bundle(["NVDA", "TSLA"])
        assert bundle["history"] == {}, "download 失败 → 日线整体缺席，蜂回退直连"
        assert set(bundle["info"]) == {"NVDA"}, "TSLA 的 info 失败 → 只缺它，不放 {} 占位"
        assert set(bundle["calendar"]) == {"NVDA", "TSLA"}

    def test_empty_tickers_no_network(self, monkeypatch):
        tk, dl = _counting_yf(monkeypatch)
        b = prefetch_market_bundle([])
        assert b["history"] == {} and b["info"] == {}
        tk.assert_not_called(); dl.assert_not_called()

    def test_inject_sets_market_on_every_agent(self):
        a1, a2 = _bee(), _bee()
        inject_prefetched([a1, a2], {"stock_data": {}, "contexts": {}, "market": {"info": {"X": {}}}})
        assert a1._prefetched_market == {"info": {"X": {}}} and a2._prefetched_market is not None


# ───────────────────────────────────────────── 4. 四只蜂：注入完整包后零直连
def _full_market(ticker="NVDA"):
    etfs = _sector_etfs_for([ticker])
    hist = {ticker: _ohlc(70, 100, 1.0)}
    for e in etfs:
        hist[e] = _ohlc(70, 50, 0.1)
    return {
        "history": hist, "history_period": "3mo",
        "info": {ticker: {"sector": "Technology", "forwardEps": 6.0, "trailingEps": 5.0,
                          "targetMeanPrice": 150.0, "currentPrice": 100.0,
                          "recommendationMean": 1.8, "numberOfAnalystOpinions": 30,
                          "trailingPE": 90.0, "shortPercentOfFloat": 0.25, "shortRatio": 4.0}},
        "calendar": {ticker: {"Earnings Date": []}},
        "analyst_targets": {ticker: {"low": 90, "high": 200, "mean": 150, "median": 145}},
    }


class TestBeesUseOnlyPrefetched:
    def test_scout_relative_strength(self, monkeypatch):
        from swarm_agents.scout_bee import ScoutBeeNova
        tk, dl = _counting_yf(monkeypatch)
        bee = ScoutBeeNova(PheromoneBoard()); bee._prefetched_market = _full_market()
        r = bee._assess_sector_relative_strength("NVDA")
        assert r["rs_signal"] in ("outperform", "underperform", "neutral"), r
        tk.assert_not_called(); dl.assert_not_called()

    def test_rival_eps_revision_and_technicals(self, monkeypatch):
        from swarm_agents.rival_bee import RivalBeeVanguard
        tk, dl = _counting_yf(monkeypatch)
        bee = RivalBeeVanguard(PheromoneBoard()); bee._prefetched_market = _full_market()
        rev = bee._assess_eps_revision("NVDA")
        tech = bee._calc_technical_indicators("NVDA")
        assert rev["revision_signal"] != "unknown", rev
        assert tech["rsi_14"] != 50.0, "70 根单边上涨的 RSI 不该还是默认值 50"
        tk.assert_not_called(); dl.assert_not_called()

    def test_bear_short_interest(self, monkeypatch):
        from swarm_agents.bear_bee import BearBeeContrarian
        tk, dl = _counting_yf(monkeypatch)
        bee = BearBeeContrarian(PheromoneBoard()); bee._prefetched_market = _full_market()
        sig, src = [], {}
        sb, si = bee._assess_short_interest("NVDA", sig, src)
        assert sb == 7.5 and si["short_pct_float"] == 25.0
        tk.assert_not_called(); dl.assert_not_called()

    def test_bear_valuation_deliberately_still_uses_fast_info(self, monkeypatch):
        """**故意没迁**：yfinance 1.2.0 的 fast_info.pe_ratio 实测恒 None，P/E 项在生产上
        是死的；改读预取包 trailingPE 会复活一个评分输入，属评分变更、要世代边界。
        这条测试钉住「未迁」——谁把它迁了，先去看 CHANGELOG v0.45.122 的说明再决定。"""
        from swarm_agents.bear_bee import BearBeeContrarian
        obj = MagicMock(); obj.fast_info = MagicMock(pe_ratio=55.0)
        tk, _ = _counting_yf(monkeypatch, obj)
        bee = BearBeeContrarian(PheromoneBoard()); bee._prefetched_market = _full_market()
        assert bee._assess_valuation("NVDA", {"price": 100.0}, 0.0, 100.0, [], {}) == 5.0
        tk.assert_called_once_with("NVDA")


# ───────────────────────────────────────────── 5. Oracle：三处共用 memo
def _option_ticker():
    fridays = []
    d = date.today() + timedelta(days=7)
    while len(fridays) < 3:
        if d.weekday() == 4:
            fridays.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    calls = pd.DataFrame({"strike": [90.0, 100.0, 110.0], "impliedVolatility": [0.5, 0.4, 0.45],
                          "openInterest": [100, 200, 300]})
    puts = pd.DataFrame({"strike": [90.0, 100.0, 110.0], "impliedVolatility": [0.6, 0.42, 0.4],
                         "openInterest": [150, 250, 50]})
    class _Tk(MagicMock):           # 独立子类：property 挂在它上，不污染全局 MagicMock
        pass
    opts = PropertyMock(return_value=tuple(fridays))
    _Tk.options = opts
    obj = _Tk()
    obj.option_chain = MagicMock(return_value=MagicMock(calls=calls, puts=puts))
    obj._opts_prop = opts
    return obj, fridays


class TestOracleMemo:
    def _bee(self):
        from swarm_agents.oracle_bee import OracleBeeEcho
        return OracleBeeEcho(PheromoneBoard())

    def test_shared_memo_fetches_each_chain_once(self, monkeypatch):
        import cboe_options
        monkeypatch.setattr(cboe_options, "fetch_cboe_chain", lambda *a, **k: None)   # 逼出 yfinance 兜底
        obj, fridays = _option_ticker()
        tk, _ = _counting_yf(monkeypatch, obj)
        bee = self._bee()
        memo = bee._yf_option_memo("NVDA")
        ts = bee._analyze_term_structure("NVDA", 100.0, memo=memo)
        sk = bee._analyze_deep_skew("NVDA", 100.0, memo=memo)
        mp = bee._calc_max_pain("NVDA", 100.0, memo=memo)
        assert ts["structure"] != "unknown" and sk["skew_25d"] is not None and mp["max_pain"] is not None
        assert tk.call_count == 1, "Ticker 只建一次"
        assert obj._opts_prop.call_count == 1, "到期日列表只读一次"
        assert obj.option_chain.call_count == 3, "三个到期日各抓一次；近月链被三处共用"
        assert sorted(memo["chains"]) == sorted(fridays)

    def test_without_memo_each_helper_fetches_on_its_own(self, monkeypatch):
        """对照：不传 memo 就是改动前的行为——三处各自建 Ticker、近月链重复抓。"""
        import cboe_options
        monkeypatch.setattr(cboe_options, "fetch_cboe_chain", lambda *a, **k: None)
        obj, _ = _option_ticker()
        tk, _ = _counting_yf(monkeypatch, obj)
        bee = self._bee()
        bee._analyze_term_structure("NVDA", 100.0)
        bee._analyze_deep_skew("NVDA", 100.0)
        bee._calc_max_pain("NVDA", 100.0)
        assert tk.call_count == 3 and obj.option_chain.call_count == 5


# ───────────────────────────────────────────── 6. 静态守卫
_ALLOWED = {
    "base.py": 7,        # 4 个访问器的回退（Ticker×4）+ 面板回退（download）+ 预取包（download + Ticker）
    "cache.py": 2,       # _fetch_stock_data 的 yfinance 回退 + ticker 有效性检查
    "bear_bee.py": 1,    # _assess_valuation 未预取时的 fast_info 回退
    "oracle_bee.py": 1,  # _yf_option_memo（期权链换源要走世代边界，本版不动）
}


def _count_direct_yf_calls(src: str) -> int:
    """用 AST 数真正的 `yf.Ticker(...)` / `yf.download(...)` 调用——注释与 docstring 里的
    提及不算（正则版把 docstring 里的示例也数进去，白名单就没法收紧）。"""
    import ast
    n = 0
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("Ticker", "download")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("yf", "_yf", "yfinance")):
            n += 1
    return n


class TestNoNewDirectCalls:
    def test_swarm_agents_direct_yfinance_calls_are_only_the_allowlisted_ones(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "swarm_agents")
        found = {}
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".py"):
                continue
            src = open(os.path.join(root, fn), encoding="utf-8").read()
            n = _count_direct_yf_calls(src)
            if n:
                found[fn] = n
        extra = {f: n for f, n in found.items() if f not in _ALLOWED}
        over = {f: (n, _ALLOWED[f]) for f, n in found.items() if f in _ALLOWED and n > _ALLOWED[f]}
        assert not extra, f"新增了 yfinance 直连（应改走 BeeAgent._yf_* 访问器 / 预取包）: {extra}"
        assert not over, f"直连数超过白名单: {over}"
        assert set(found) == set(_ALLOWED), f"白名单与现状不一致（有的项已经清零，请收紧白名单）: {found}"
