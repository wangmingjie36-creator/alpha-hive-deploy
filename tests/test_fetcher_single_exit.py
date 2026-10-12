"""v0.45.473：`MultiSourceFetcher.fetch` 唯一出口——momentum_5d / volume_ratio 的回落是出口的属性，不是某个源的。

事故形状（auto-memory `alpha-hive-fallback-not-reached.md`，第四次）：自攒价格索引 / Twelve Data 两条独立回落
只挂在 `CBOESource.fetch` 内部与「全源失败」一支（v0.45.270）。其余出口原样把 None 交出去：
  - Finnhub / Alpha Vantage 胜出（两源都硬写 `momentum_5d=None, volume_ratio=None`）；
  - CBOE 盘中陈旧价被推迟、其余源全挂时退用它（`defer`，v0.45.234）；
  - 过期缓存；
以及 CBOE 内部 yfinance「返回空历史」（不抛）、「均量 NaN」两条分支。前三次（270 / 463 / 465）
都是给某一条分支补一次——这里改成**结构**：分支全在 `_resolve`（只返回 StockData），`fetch` 只有一个
return，经 `_emit` → `_fill_history_fields`；两个回落函数在生产代码里只有这一个调用点。

本文件：
  1. 每个出口经**真** `MultiSourceFetcher.fetch` 走一遍（源打桩，回落函数打记录桩）；
  2. Finnhub 胜出那条再用真索引 + 沙箱库端到端走一次（不靠记录桩）；
  3. 计数 → scan_timing → alert_manager P2；
  4. AST 结构守卫，各带正对照（对真源码做变异，真跑同一个检查函数看它红）。
"""

import ast
import copy
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import cboe_options as co
import data_pipeline as dp
from hive_logger import PATHS
from tests._repo_files import own_python_files

ROOT = Path(__file__).resolve().parent.parent
DP_FILE = ROOT / "data_pipeline.py"
FILL_FUNCS = ("_fill_momentum_from_index", "_fill_volume_from_twelvedata")


@pytest.fixture(autouse=True)
def _fresh_stats():
    dp.reset_history_fill_stats()
    yield
    dp.reset_history_fill_stats()


class _Fills:
    """两条回落的记录桩：记下谁、带什么 session_close 被调了几次。`mom` / `vol` 为 None ⇒ 回落也拿不到。"""

    def __init__(self, mp, mom=-2.5, vol=0.8, as_of="2026-10-08"):
        self.mom_calls, self.vol_calls = [], []

        def _m(ticker, out, session_close=None):
            self.mom_calls.append((ticker, session_close))
            if mom is None:
                return out
            return {"momentum_5d": mom, "momentum_source": "price_index", "momentum_as_of": as_of}

        def _v(ticker, out):
            self.vol_calls.append(ticker)
            if vol is None:
                return out
            return {"volume_ratio": vol, "avg_volume": 5_000_000, "volume_source": "twelvedata"}

        mp.setattr(dp, "_fill_momentum_from_index", _m)
        mp.setattr(dp, "_fill_volume_from_twelvedata", _v)


class _Src:
    def __init__(self, name, data):
        self.name, self.data, self.calls = name, data, 0

    def fetch(self, ticker):
        """同真源：每次调用给一个新对象（出口就地补字段、打 history_checked 标记）。"""
        self.calls += 1
        return copy.copy(self.data) if self.data is not None else None


def _fetcher(sources):
    f = dp.MultiSourceFetcher()
    f._sources = list(sources)
    return f


def _degraded(name, price=100.0, **kw):
    """Finnhub / AV 那一类：有价、历史字段硬写 None。"""
    return dp.StockData(price=price, data_source=dp.DataQuality.DEGRADED, source_name=name,
                        momentum_source="unavailable", fetch_timestamp=time.time(), **kw)


def _yf_frame(closes, volumes=None, start="2026-10-05"):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"Close": closes, "Volume": volumes or [1_000_000.0] * len(closes)}, index=idx)


@pytest.fixture
def yf_history(monkeypatch):
    """把 yfinance.Ticker(...).history 换成给定 DataFrame（或抛）。"""
    import yfinance
    holder = {"frame": None, "exc": None}

    class _T:
        def __init__(self, *_a, **_k):
            pass

        def history(self, **_k):
            if holder["exc"] is not None:
                raise holder["exc"]
            return holder["frame"]

    monkeypatch.setattr(yfinance, "Ticker", _T)
    monkeypatch.setattr(dp, "_exchange_now", lambda: None)
    return holder


# ═════════════════════════════════════════════ 1. 每个出口都过回落

class TestEveryExitFills:
    def test_finnhub_wins_gets_both_fields(self, monkeypatch):
        """头号缺口：CBOE / yfinance 挂了、Finnhub 接住价——此前 momentum / volume 恒 None。"""
        fills = _Fills(monkeypatch)
        fh = _Src("finnhub", _degraded("finnhub"))
        out = _fetcher([_Src("cboe", None), _Src("yfinance", None), fh]).fetch("NVDA")
        assert out["source_name"] == "finnhub" and out["price"] == 100.0
        assert (out["momentum_5d"], out["momentum_source"], out["momentum_as_of"]) == (-2.5, "price_index", "2026-10-08")
        assert (out["volume_ratio"], out["volume_source"], out["avg_volume"]) == (0.8, "twelvedata", 5_000_000)
        assert fills.mom_calls == [("NVDA", None)], "Finnhub 没有核实过的收盘可给，窗口末端不许瞎填"

    def test_real_finnhub_source_through_fetcher(self, monkeypatch):
        """用真 FinnhubSource（只打桩 HTTP）：它硬写的 None 必须在出口被补上。"""
        import http_gate
        fills = _Fills(monkeypatch)
        monkeypatch.setattr(http_gate, "urlopen_gated",
                            lambda url, timeout=10: json.dumps({"c": 101.5, "pc": 100.0}).encode())
        fh = dp.FinnhubSource()
        fh.api_key = "test-key"
        out = _fetcher([_Src("cboe", None), _Src("yfinance", None), fh]).fetch("NVDA")
        assert out["source_name"] == "finnhub" and out["price"] == 101.5
        assert out["momentum_5d"] == -2.5 and out["volume_ratio"] == 0.8
        assert len(fills.mom_calls) == 1 and len(fills.vol_calls) == 1

    def test_real_alpha_vantage_source_through_fetcher(self, monkeypatch):
        import http_gate
        _Fills(monkeypatch)
        monkeypatch.setattr(http_gate, "urlopen_gated", lambda url, timeout=10: json.dumps(
            {"Global Quote": {"05. price": "50.0", "10. change percent": "1.0%", "06. volume": "1000"}}).encode())
        av = dp.AlphaVantageSource()
        av.api_key = "test-key"
        out = _fetcher([_Src("cboe", None), av]).fetch("NVDA")
        assert out["source_name"] == "alpha_vantage"
        assert out["momentum_5d"] == -2.5 and out["volume_ratio"] == 0.8

    def test_deferred_cboe_stale_intraday_gets_fill(self, monkeypatch):
        """CBOE 盘中陈旧价被推迟、其余源全挂 ⇒ 退用它；此前直出 None。陈旧文件的 close 不是收盘 ⇒ 不传末端。"""
        fills = _Fills(monkeypatch)
        monkeypatch.setattr(co, "_fetch_cboe_payload", lambda t, timeout=15: {
            "symbol": "T", "close": 26.225, "current_price": 26.3, "last_trade_time": "2026-09-11T12:10:00"})
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 9, 11, 17, 30))
        out = _fetcher([dp.CBOESource(), _Src("yfinance", None), _Src("finnhub", None)]).fetch("T")
        assert out["price_source"] == co.STALE_INTRADAY_SOURCE, "前提：确实走的是推迟退用那一支"
        assert out["momentum_5d"] == -2.5 and out["volume_ratio"] == 0.8
        assert fills.mom_calls == [("T", None)]
        assert set(dp.history_fill_stats()["by_path"]) == {"deferred:cboe"}

    def test_expired_cache_carries_filled_values(self, monkeypatch):
        """过期缓存：缓存里存的是过了出口的对象 ⇒ 带着补好的值出去，不重跑回落。"""
        fills = _Fills(monkeypatch)
        fh = _Src("finnhub", _degraded("finnhub"))
        f = _fetcher([fh])
        f.fetch("NVDA")
        f._cache["NVDA"].fetch_timestamp = time.time() - (f.CACHE_TTL_DEGRADED + 60)
        fh.data = None                                       # 第二轮全源失败
        out = f.fetch("NVDA")
        assert out["data_source"] == dp.DataQuality.STALE, "前提：确实走的是过期缓存那一支"
        assert out["momentum_5d"] == -2.5 and out["volume_ratio"] == 0.8
        assert len(fills.mom_calls) == 1 and len(fills.vol_calls) == 1

    def test_expired_cache_entry_not_yet_filled_is_filled(self, monkeypatch):
        """缓存里若躺着没过出口的对象（任何绕过 `_emit` 的写入），过期缓存出口也照补——出口不认来路。"""
        fills = _Fills(monkeypatch)
        f = _fetcher([_Src("cboe", None)])
        f._set_cache("NVDA", dp.StockData(price=90.0, data_source=dp.DataQuality.DEGRADED,
                                          source_name="finnhub", fetch_timestamp=time.time() - 1000))
        out = f.fetch("NVDA")
        assert out["data_source"] == dp.DataQuality.STALE
        assert out["momentum_5d"] == -2.5 and fills.mom_calls == [("NVDA", None)]

    def test_fresh_cache_hit_does_not_rerun(self, monkeypatch):
        fills = _Fills(monkeypatch, mom=None, vol=None)       # 回落拿不到 ⇒ 缓存里仍是 None
        f = _fetcher([_Src("finnhub", _degraded("finnhub"))])
        f.fetch("NVDA")
        out = f.fetch("NVDA")
        assert out["momentum_5d"] is None
        assert len(fills.mom_calls) == 1 and len(fills.vol_calls) == 1, "新鲜缓存命中重跑回落 = 白耗 Twelve Data 配额"

    def test_all_sources_failed_still_fills(self, monkeypatch):
        fills = _Fills(monkeypatch)
        out = _fetcher([_Src("cboe", None), _Src("yfinance", None)]).fetch("NVDA")
        assert out["_data_unavailable"] is True and out["price"] == 0.0
        assert out["momentum_5d"] == -2.5 and out["volume_ratio"] == 0.8
        assert fills.mom_calls == [("NVDA", None)]

    def test_real_source_with_values_skips_fill(self, monkeypatch):
        fills = _Fills(monkeypatch)
        good = dp.StockData(price=10.0, data_source=dp.DataQuality.REAL, source_name="yfinance",
                            momentum_5d=1.0, momentum_source="5d_real", volume_ratio=1.2,
                            volume_source="yfinance", fetch_timestamp=time.time())
        out = _fetcher([_Src("yfinance", good)]).fetch("NVDA")
        assert (out["momentum_5d"], out["momentum_source"], out["volume_source"]) == (1.0, "5d_real", "yfinance")
        assert fills.mom_calls == [] and fills.vol_calls == []


class TestCboeInternalBranches:
    """CBOE 胜出但 yfinance 历史不全：此前只有「yfinance 抛异常」那一支挂了回落。"""

    def _cboe(self, monkeypatch):
        monkeypatch.setattr(co, "_fetch_cboe_payload", lambda t, timeout=15: {
            "symbol": "VZ", "close": 36.24, "current_price": 36.9, "last_trade_time": "2026-10-09T15:59:58"})
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 10, 9, 17, 30))
        return _fetcher([dp.CBOESource()])

    def test_yfinance_raises(self, monkeypatch, yf_history):
        fills = _Fills(monkeypatch)
        yf_history["exc"] = RuntimeError("Too Many Requests")
        out = self._cboe(monkeypatch).fetch("VZ")
        assert out["price_source"] == "cboe_close"
        assert out["momentum_5d"] == -2.5 and out["volume_ratio"] == 0.8
        assert fills.mom_calls == [("VZ", ("2026-10-09", 36.24))], "核实过的当场收盘要带到出口"

    def test_yfinance_empty_history(self, monkeypatch, yf_history):
        """yfinance 限流时常**返回空表**而不抛——此前 `_fetch_history_metrics` 直接 None，回落一次都没跑。"""
        fills = _Fills(monkeypatch)
        yf_history["frame"] = pd.DataFrame({"Close": [], "Volume": []})
        out = self._cboe(monkeypatch).fetch("VZ")
        assert out["momentum_5d"] == -2.5 and out["volume_ratio"] == 0.8
        assert len(fills.mom_calls) == 1

    def test_yfinance_nan_average_volume(self, monkeypatch, yf_history):
        """均量 NaN：此前 volume 置 None 后不回落（Twelve Data 只挂在异常分支）。"""
        fills = _Fills(monkeypatch)
        yf_history["frame"] = _yf_frame([40.0, 40.0, 40.0, 41.0, 41.0, 41.0], [float("nan")] * 6)
        out = self._cboe(monkeypatch).fetch("VZ")
        assert out["momentum_source"] == "5d_real", "前提：yfinance 算出了动量"
        assert out["volume_ratio"] == 0.8 and out["volume_source"] == "twelvedata"
        assert fills.mom_calls == [] and fills.vol_calls == ["VZ"]


class TestYFinanceSourceBranches:
    def test_short_history_passes_its_own_last_close(self, monkeypatch, yf_history):
        """yfinance 胜出但不足 5 根：末根已收盘日线就是一场官方收盘，交给出口回落当窗口末端。"""
        fills = _Fills(monkeypatch)
        yf_history["frame"] = _yf_frame([40.0, 41.0, 36.24])   # 10-05 / 06 / 07
        out = _fetcher([dp.YFinanceSource()]).fetch("VZ")
        assert out["source_name"] == "yfinance" and out["momentum_5d"] == -2.5
        assert fills.mom_calls == [("VZ", ("2026-10-07", 36.24))]
        assert out["volume_source"] == "yfinance" and fills.vol_calls == []

    def test_nan_average_volume_is_none_not_one_share(self, monkeypatch, yf_history):
        """此前兜底 avg_vol=1.0 ⇒ avg_volume=1 股、volume_ratio=当日成交量本身。"""
        _Fills(monkeypatch, vol=None)
        yf_history["frame"] = _yf_frame([40.0] * 6, [float("nan")] * 6)
        out = _fetcher([dp.YFinanceSource()]).fetch("VZ")
        assert out["volume_ratio"] is None and out["avg_volume"] is None and out["volume_source"] is None


# ═════════════════════════════════════════════ 2. 标签与值同一处

class TestLabels:
    def test_unfilled_is_labelled_unavailable(self, monkeypatch):
        _Fills(monkeypatch, mom=None, vol=None)
        out = _fetcher([_Src("cboe", None)]).fetch("NVDA")
        assert out["momentum_5d"] is None and out["momentum_source"] == "unavailable"
        assert out["volume_ratio"] is None and out["volume_source"] is None
        assert out["momentum_as_of"] is None

    def test_internal_fields_not_serialized(self, monkeypatch):
        _Fills(monkeypatch)
        out = _fetcher([_Src("finnhub", _degraded("finnhub", session_close=("2026-10-09", 1.0)))]).fetch("X")
        assert not {"session_close", "history_checked", "defer"} & set(out)

    def test_one_fill_raising_does_not_skip_the_other(self, monkeypatch):
        def _boom(ticker, out, session_close=None):
            raise RuntimeError("index broken")
        _Fills(monkeypatch)
        monkeypatch.setattr(dp, "_fill_momentum_from_index", _boom)
        out = _fetcher([_Src("finnhub", _degraded("finnhub"))]).fetch("NVDA")
        assert out["momentum_5d"] is None and out["momentum_source"] == "unavailable"
        assert out["volume_ratio"] == 0.8, "两条回落互相独立"
        assert dp.history_fill_stats()["by_path"]["source:finnhub"]["momentum"]["errors"] == 1


# ═════════════════════════════════════════════ 3. 真索引端到端（不靠记录桩）

@pytest.fixture
def price_db(tmp_path):
    """沙箱权威价库：VZ 09-28 … 10-08 九个交易日。目标按沙箱自己算、写前断言 PATHS.db 指向它。"""
    target = tmp_path / "test.db"
    assert Path(PATHS.db) == target, "conftest 的 _isolate_env 没生效，本文件作废"
    dates = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
             "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
    closes = [40.0, 40.0, 40.0, 40.0, 40.0, 41.0, 41.0, 41.0, 40.32]
    con = sqlite3.connect(target)
    con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT, price_at_predict REAL)")
    con.executemany("INSERT INTO predictions VALUES (?,?,?)", [(d, "VZ", c) for d, c in zip(dates, closes)])
    con.commit()
    con.close()
    return target


class TestRealIndexEndToEnd:
    def test_finnhub_wins_real_index_fills(self, monkeypatch, price_db):
        monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda t, out: out)
        out = _fetcher([_Src("cboe", None), _Src("finnhub", _degraded("finnhub", price=36.5))]).fetch("VZ")
        expect = round((40.32 / 40.0 - 1) * 100, 4)          # 10-08 / 10-02：4 个交易日窗口
        assert out["momentum_5d"] == expect and out["momentum_as_of"] == "2026-10-08"
        assert out["momentum_source"] == "price_index"
        assert not math.isnan(out["momentum_5d"])


# ═════════════════════════════════════════════ 4. 计数 → status.json → P2

class TestCountersAndAlert:
    def test_none_before_any_resolution(self):
        assert dp.history_fill_stats() is None

    def test_counts_by_path_and_still_missing(self, monkeypatch):
        _Fills(monkeypatch, mom=None)                         # momentum 补不上、volume 补得上
        f = _fetcher([_Src("finnhub", _degraded("finnhub"))])
        f.fetch("NVDA")
        f.fetch("TSLA")
        s = dp.history_fill_stats()
        p = s["by_path"]["source:finnhub"]
        assert p["n"] == 2 and p["momentum"] == {"needed": 2, "filled": 0, "errors": 0}
        assert p["volume"] == {"needed": 2, "filled": 2, "errors": 0}
        assert s["still_missing"] == {"momentum": ["NVDA", "TSLA"], "volume": []}

    def test_later_success_clears_still_missing(self, monkeypatch):
        _Fills(monkeypatch, mom=None)
        _fetcher([_Src("finnhub", _degraded("finnhub"))]).fetch("NVDA")
        _Fills(monkeypatch)
        _fetcher([_Src("finnhub", _degraded("finnhub"))]).fetch("NVDA")
        assert dp.history_fill_stats()["still_missing"]["momentum"] == []

    def test_window_behind_session_counted(self, monkeypatch):
        _Fills(monkeypatch, as_of="2026-10-08")
        src = _Src("cboe", _degraded("cboe", session_close=("2026-10-09", 36.24)))
        _fetcher([src]).fetch("VZ")
        assert dp.history_fill_stats()["window_behind_session"] == 1

    def test_reaches_scan_timing_and_raises_p2(self, monkeypatch, tmp_path):
        import alert_manager as am
        import scan_timing as stt
        _Fills(monkeypatch, mom=None, vol=None)
        _fetcher([_Src("finnhub", _degraded("finnhub"))]).fetch("NVDA")
        c = stt.counters()
        assert c["history_fill"]["still_missing"] == {"momentum": ["NVDA"], "volume": ["NVDA"]}
        assert "回落后仍缺 动量 1 只/量比 1 只" in stt.summary_line({"phases": {}, "counters": c})
        hits = self._analyze(tmp_path, c["history_fill"])[0]
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.MEDIUM
        assert "NVDA" in hits[0].details["momentum_5d"] and "source:finnhub" in hits[0].details["各出口"]

    @pytest.mark.parametrize("counter, alerted, skipped", [
        ({"resolutions": 3, "by_path": {}, "window_behind_session": 0,
          "still_missing": {"momentum": [], "volume": []}}, False, False),
        (None, False, True),
    ])
    def test_clean_is_silent_and_missing_is_skipped(self, tmp_path, counter, alerted, skipped):
        hits, a = self._analyze(tmp_path, counter)
        assert bool(hits) is alerted
        assert any("历史字段回落检查" in s for s in a.checks_skipped) is skipped

    @staticmethod
    def _analyze(tmp_path, counter):
        import alert_manager as am
        status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"counters": {"history_fill": counter}, "extra": {"gh_pages": {"success": True}},
                                  "production_sync": {"outcome": "up_to_date"}}}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a.analyze(p)
        return [x for x in a.alerts if "历史字段回落后仍缺" in x.message], a


# ═════════════════════════════════════════════ 5. 结构守卫（新分支绕不过出口）

def _method(tree, cls, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"{cls}.{name} 不在 data_pipeline.py 里——守卫要跟着改名")


def _own_nodes(fn):
    """fn 体内的节点，不进嵌套 def / lambda（它们的 return 不是 fn 的出口）。"""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def single_exit_problems(src: str) -> list:
    """`fetch` 恰一个 return 且返回 `self._emit(...)`；`_resolve` 不出字典、每个 return 都是三元组。"""
    tree = ast.parse(src)
    probs = []
    fetch = _method(tree, "MultiSourceFetcher", "fetch")
    rets = [n for n in _own_nodes(fetch) if isinstance(n, ast.Return)]
    if len(rets) != 1:
        probs.append(f"fetch 有 {len(rets)} 个 return（应恰为 1，经 _emit）")
    for r in rets:
        v = r.value
        if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "_emit"
                and isinstance(v.func.value, ast.Name) and v.func.value.id == "self"):
            probs.append(f"fetch 第 {r.lineno} 行的 return 没经 self._emit")
    resolve = _method(tree, "MultiSourceFetcher", "_resolve")
    for n in _own_nodes(resolve):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "to_dict":
            probs.append(f"_resolve 第 {n.lineno} 行调了 .to_dict()（出字典是 _emit 的事）")
        if isinstance(n, ast.Return) and not (isinstance(n.value, ast.Tuple) and len(n.value.elts) == 3):
            probs.append(f"_resolve 第 {n.lineno} 行的 return 不是 (数据, 出口名, 入缓存) 三元组")
    return probs


def fill_call_site_problems(sources: dict) -> list:
    """两个回落函数在生产代码里只许被 `data_pipeline._fill_history_fields` 引用（调用或取属性都算）。

    `sources`：{相对路径: 源码}。只认 Name / Attribute 引用，不认字符串与注释。"""
    probs = []
    for rel, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        owner_of = {}   # id(节点) → 最内层所在函数名（嵌套函数里的引用不算外层函数的）

        def _visit(node, owner):
            for ch in ast.iter_child_nodes(node):
                nxt = ch.name if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
                owner_of[id(ch)] = nxt
                _visit(ch, nxt)
        _visit(tree, None)
        for n in ast.walk(tree):
            name = (n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None)
            if name not in FILL_FUNCS:
                continue
            owner = owner_of.get(id(n))
            if not (rel == "data_pipeline.py" and owner == "_fill_history_fields"):
                probs.append(f"{rel}:{n.lineno} 在 {owner or '模块层'} 里引用了 {name}")
    return probs


def _production_sources(root: Path) -> dict:
    files, _how = own_python_files(root)
    out = {}
    for p in files:
        rel = p.relative_to(root)
        if rel.parts[0] in {"tests", "experiments"}:
            continue
        out[rel.as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return out


class TestStructure:
    def test_fetch_has_single_exit(self):
        assert single_exit_problems(DP_FILE.read_text(encoding="utf-8")) == []

    def test_fill_functions_have_one_production_call_site(self):
        srcs = _production_sources(ROOT)
        assert "data_pipeline.py" in srcs and len(srcs) > 50, "文件枚举没接上，本条守卫等于没跑"
        assert fill_call_site_problems(srcs) == []
        # 正面核对：唯一调用点确实在（否则「零引用」也会通过——回落被整个删掉不红）
        tree = ast.parse(srcs["data_pipeline.py"])
        fill = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_fill_history_fields")
        called = {n.func.id for n in ast.walk(fill) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert set(FILL_FUNCS) <= called

    def test_emit_calls_fill(self):
        tree = ast.parse(DP_FILE.read_text(encoding="utf-8"))
        emit = _method(tree, "MultiSourceFetcher", "_emit")
        called = {n.func.id for n in ast.walk(emit) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_fill_history_fields" in called


class TestStructureGuardsHaveTeeth:
    """正对照：对**真**源码做变异，真跑上面同一个检查函数，必须红。"""

    SRC = DP_FILE.read_text(encoding="utf-8")
    FETCH_RET = "        data, path, cacheable = self._resolve(ticker)\n        return self._emit(ticker, data, path, cacheable)\n"

    def _mut(self, old, new):
        assert self.SRC.count(old) == 1, f"变异锚点失效：{old!r}"
        return self.SRC.replace(old, new)

    def test_extra_return_in_fetch_reds(self):
        src = self._mut(self.FETCH_RET, '        if ticker == "X":\n            return {}\n' + self.FETCH_RET)
        assert single_exit_problems(src)

    def test_fetch_returning_dict_directly_reds(self):
        src = self._mut(self.FETCH_RET, "        data, path, cacheable = self._resolve(ticker)\n"
                                        "        return data.to_dict()\n")
        assert single_exit_problems(src)

    def test_resolve_handing_out_dict_reds(self):
        src = self._mut('return cached, "cache_expired", False', "return cached.to_dict()")
        assert single_exit_problems(src)

    def test_per_source_fill_reds(self):
        src = self._mut("            metrics = _fetch_history_metrics(ticker)\n",
                        "            metrics = _fetch_history_metrics(ticker)\n"
                        "            metrics = _fill_momentum_from_index(ticker, metrics)\n")
        assert fill_call_site_problems({"data_pipeline.py": src})

    def test_other_module_calling_fill_reds(self):
        assert fill_call_site_problems({"x.py": "import data_pipeline as dp\ndp._fill_volume_from_twelvedata('A', None)\n"})

    def test_nested_helper_inside_fill_owner_is_not_the_owner(self):
        """调用点藏进 `_fill_history_fields` 里嵌套的函数也算别处——归属按最内层函数。"""
        src = self._mut("    if data.history_checked:\n        return\n",
                        "    if data.history_checked:\n        return\n\n"
                        "    def _sneak():\n        return _fill_volume_from_twelvedata(ticker, None)\n")
        assert fill_call_site_problems({"data_pipeline.py": src})
