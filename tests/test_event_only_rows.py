"""v0.45.645：yfinance「公司行动空行」不再污染行情三字段（生产与维度 IC 冻结通道层同口径）。

背景
----
yfinance 把分红 / 拆股并进日线时，那一天若没有价格 K 线，会单独出一行：开高低收全空、成交量 0、Dividends / Stock Splits 非零。
生产观测档案里唯一一例：META 2026-09-21（10-21 收盘后 `period="1mo"` 的窗口是 (09-21, 10-21]，09-21 那根 K 线在窗口外、分红漏了进来，
成了第 0 行）。此前主路径与冻结层都把它当 K 线：落在末根 / 倒数第 5 根 ⇒ 动量 NaN ⇒ Buzz `max(-10, min(10, nan))` = 10 ⇒ 动量通道满分看多
（数据质量照标 real、出口回落只认 None 也绕过）；落在最近 20 根 ⇒ 成交量 0 进 20 日均量 ⇒ 量比偏高约 5%。

本文件钉住
----------
1. 真实形状：夹具 `tests/fixtures/yf_daily_event_row_meta_20261021.json` 的列顺序（Close 在前）/ dtype / 时区 / 时间戳 / 空行原样取自档案，
   生产与冻结层都认出第 0 行。
2. 判据（用户 10-24 定）：四个价格列都不是有限数 + 成交量恰为 0 + 分红或拆股是有限非零数；形似而不是的行（只缺收盘、没有事件、量不为 0、
   量为 NaN）不动——两层逐例同判。
3. 两种后果都没了：四条取数路径（`_fetch_history_metrics` / `YFinanceSource.fetch` / `--date` 补跑 / cache.py 旧回落）；末行是空行（当天没有
   K 线）⇒ 实时路径不拿前一场冒充今天、整段当取不到（用户 10-25 定，代码评审 #1），冻结层同判为「不全」。
4. 四个调用方取到日线后第一步就去掉（AST），冻结层在同一位置（「≥ 3 根才看时钟」数的是去掉之后的根数）。
5. 看得见：计数 → scan_timing `event_rows` → 摘要行；不在首行 ⇒ alert_manager P2（按评分时快照），评分后才遇到 ⇒ INFO，计数缺失 ⇒ checks_skipped。

全部离线：yfinance 在测试里打桩，不联网、不读生产数据。
"""
from __future__ import annotations

import ast
import copy
import json
import math
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import alert_manager as am
import buzz_v1_channels as fc
import observation_layer as ol
import price_metrics as pm
import scan_timing as stt

_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = _ROOT / "tests" / "fixtures" / "yf_daily_event_row_meta_20261021.json"
FIELDS = ("momentum_5d", "volume_ratio", "volatility_20d")


def _payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]


def _frame(payload=None):
    return ol.frame_from_payload(payload or _payload())


def _both(payload):
    """生产判据（经 DataFrame）与冻结层判据（直接读 payload）各算一遍。"""
    return pm.event_only_positions(ol.frame_from_payload(payload)), fc.event_only_positions(payload)


def _same(a, b):
    return (a is None and b is None) or (a is not None and b is not None and (a == b or (math.isnan(a) and math.isnan(b))))


@pytest.fixture
def yf_stub(monkeypatch):
    """yfinance.Ticker(...).history(...) 返回 box["frame"] 的副本；交易所时钟取不到（末根判断原样返回）。"""
    import data_pipeline as dp
    import yfinance
    box = {"frame": None}

    class _T:
        def __init__(self, *_a, **_k):
            pass

        def history(self, **_k):
            return box["frame"].copy()
    monkeypatch.setattr(yfinance, "Ticker", _T)
    monkeypatch.setattr(dp, "_exchange_now", lambda: None)
    return box


# ══════════════════════════════════════════════════════════════════════════
# 1. 真实形状
# ══════════════════════════════════════════════════════════════════════════

class TestRealShape:

    def test_fixture_keeps_the_archived_shape(self):
        p = _payload()
        assert p["columns"] == ["Close", "High", "Low", "Open", "Volume", "Dividends", "Stock Splits"], "列顺序会变，夹具要保住档案里那种"
        assert p["dtypes"] == ["float64"] * 4 + ["int64", "float64", "float64"]
        assert p["request"] == {"period": "1mo"} and p["tz"] == "America/New_York" and p["index_unit"] == "s"
        assert len(p["data"]) == 23 and p["index"][0] == "2026-09-21T00:00:00-04:00"
        assert p["data"][0] == [None, None, None, None, 0, 0.525, 0.0]

    def test_both_layers_find_row_zero(self):
        assert _both(_payload()) == ([0], [0])

    def test_dropping_it_leaves_the_requested_window(self):
        df = _frame()
        out, last = pm.drop_event_only_rows(df, "META")
        assert last is False
        assert len(out) == 22 and str(out.index[0])[:10] == "2026-09-22", "窗口 (09-21, 10-21] 恰好 22 个交易日"
        assert [r[0] for r in fc.daily_rows(_payload())] == [str(t)[:10] for t in out.index]
        assert not out[["Open", "High", "Low", "Close"]].isna().any().any()


# ══════════════════════════════════════════════════════════════════════════
# 2. 判据：两层逐例同判
# ══════════════════════════════════════════════════════════════════════════

def _with_row(i, **cells):
    p = copy.deepcopy(_payload())
    cols = p["columns"]
    for c, v in cells.items():
        if c == "Volume" and v is None:
            p["dtypes"][cols.index("Volume")] = "float64"
        p["data"][i][cols.index(c)] = v
    return p


_EMPTY = {"Open": None, "High": None, "Low": None, "Close": None}


class TestCriterion:

    @pytest.mark.parametrize("cells,expect", [
        ({**_EMPTY, "Volume": 0, "Dividends": 0.4}, [0, 7]),
        ({**_EMPTY, "Volume": 0, "Stock Splits": 2.0}, [0, 7]),
        ({**_EMPTY, "Volume": 0}, [0]),                                   # 没有事件：不是这种行
        ({"Close": None, "Volume": 0, "Dividends": 0.4}, [0]),            # 其余价格列还在
        ({"Close": None, "Dividends": 0.4}, [0]),                         # 2026-08-28 那种：只缺收盘、成交量正常
        ({**_EMPTY, "Volume": 3, "Dividends": 0.4}, [0]),                 # 量不为 0
        ({**_EMPTY, "Volume": None, "Dividends": 0.4}, [0]),              # 量为 NaN
        ({**_EMPTY, "Volume": 0, "Dividends": None}, [0]),                # 分红 NaN 不算事件
    ], ids=["dividend", "split", "no_event", "other_prices_present", "close_only_0828", "volume_nonzero",
            "volume_nan", "dividend_nan"])
    def test_lookalikes_and_true_rows(self, cells, expect):
        """变红的变异：判据只看收盘 / 不看事件列 / 不看成交量 / 事件列不判有限——两层任何一层改了都会让某一例对不上。"""
        prod, frozen = _both(_with_row(7, **cells))
        assert prod == frozen == expect

    def test_unknown_shapes_are_left_alone(self):
        p = copy.deepcopy(_payload())
        for c in ("Dividends", "Stock Splits"):
            j = p["columns"].index(c)
            p["columns"].pop(j)
            p["dtypes"].pop(j)
            for r in p["data"]:
                r.pop(j)
        assert _both(p) == ([], []), "没有事件列 ⇒ 认不出，不猜"
        q = copy.deepcopy(_payload())
        j = q["columns"].index("Open")
        q["columns"].pop(j)
        q["dtypes"].pop(j)
        for r in q["data"]:
            r.pop(j)
        assert _both(q) == ([], []), "缺价格列 ⇒ 认不出，不猜"

    def test_column_order_does_not_matter(self):
        p = copy.deepcopy(_payload())
        order = [6, 5, 4, 3, 2, 1, 0]
        p["columns"] = [p["columns"][i] for i in order]
        p["dtypes"] = [p["dtypes"][i] for i in order]
        p["data"] = [[r[i] for i in order] for r in p["data"]]
        assert _both(p) == ([0], [0])


# ══════════════════════════════════════════════════════════════════════════
# 3. 去掉与计数
# ══════════════════════════════════════════════════════════════════════════

class TestDropAndCount:

    def test_nothing_to_drop_returns_the_same_object(self):
        df = _frame().iloc[1:]
        out, last = pm.drop_event_only_rows(df, "META")
        assert out is df and last is False

    def test_drops_by_position_not_by_label(self):
        """空行与真 K 线同一个时间戳时，按索引标签删会连真的一起删（变红的变异：`hist.drop(index=…)`）。"""
        df = _frame()
        dup = pd.concat([df.iloc[[1]].assign(Open=np.nan, High=np.nan, Low=np.nan, Close=np.nan, Volume=0, Dividends=0.5),
                         df.iloc[1:]])
        out, _ = pm.drop_event_only_rows(dup, "META")
        assert len(out) == len(dup) - 1 and out.index[0] == df.index[1] and out["Close"].notna().all()

    def test_counts_first_row_and_elsewhere(self):
        df = _frame()
        pm.drop_event_only_rows(df, "META")
        mid = df.copy()
        mid.iloc[10, [mid.columns.get_loc(c) for c in ("Open", "High", "Low", "Close")]] = np.nan
        mid.iloc[10, mid.columns.get_loc("Volume")] = 0
        mid.iloc[10, mid.columns.get_loc("Dividends")] = 0.3
        pm.mark_event_row_scoring()
        pm.drop_event_only_rows(mid, "T")
        assert pm.drop_event_only_rows(_event_at(df.iloc[1:], -1), "U")[1] is True
        st = pm.event_row_stats()
        assert (st["frames"], st["dropped"], st["first_row"], st["last_row"], st["elsewhere_total"]) == (3, 4, 2, 1, 2)
        assert st["elsewhere"] == [{"ticker": "T", "date": str(df.index[10])[:10], "pos": 10, "n": 23, "last": False},
                                   {"ticker": "U", "date": str(df.index[-1])[:10], "pos": 21, "n": 22, "last": True}]
        assert st["at_scoring"] == {"frames": 1, "dropped": 1, "first_row": 1, "last_row": 0, "elsewhere_total": 0,
                                    "elsewhere": []}
        pm._reset_event_row_stats()
        assert pm.event_row_stats() == {"frames": 0, "dropped": 0, "first_row": 0, "last_row": 0, "elsewhere_total": 0,
                                        "elsewhere": [], "at_scoring": None}

    def test_detail_list_is_capped_but_the_total_is_not(self):
        """代码评审 #3：明细最多 20 条，总数照数（变红的变异：总数跟着明细封顶）。"""
        mid = _event_at(_frame(), 10)
        for _ in range(25):
            pm.drop_event_only_rows(mid, "T")
        st = pm.event_row_stats()
        assert st["elsewhere_total"] == 25 and len(st["elsewhere"]) == 20 and st["first_row"] == 25


# ══════════════════════════════════════════════════════════════════════════
# 4. 两种后果在四条取数路径上都没了
# ══════════════════════════════════════════════════════════════════════════

def _event_at(df, i):
    """把第 i 行换成公司行动空行（那天的价格 K 线被吞掉的情形）。"""
    out = df.copy()
    out.iloc[i, [out.columns.get_loc(c) for c in ("Open", "High", "Low", "Close")]] = np.nan
    out.iloc[i, out.columns.get_loc("Volume")] = 0
    out.iloc[i, out.columns.get_loc("Dividends")] = 0.525
    return out


class TestConsequencesGone:

    def test_momentum_never_nan(self, yf_stub):
        """倒数第 5 根是空行。改前：动量 NaN ⇒ Buzz 动量通道 100（满分看多）。变红的变异：任一路径不调 drop_event_only_rows。"""
        import data_pipeline as dp
        yf_stub["frame"] = _event_at(_frame().iloc[1:], -5)
        want = pm.history_price_metrics(pm.drop_event_only_rows(yf_stub["frame"])[0])
        assert math.isfinite(want["momentum_5d"])
        assert pm.history_price_metrics(yf_stub["frame"])["momentum_5d"] is None, \
            "正对照：不去掉就算不出（v0.45.653 起非有限改 None；之前是 NaN、通道 100）"
        assert fc.momentum_channel(float("nan")) == 100.0, "正对照：NaN 进动量通道就是满分"

        out = dp._fetch_history_metrics("META")
        assert out["momentum_5d"] == want["momentum_5d"] and math.isfinite(out["_price_from_hist"])
        src = dp.YFinanceSource().fetch("META")
        assert src.momentum_5d == want["momentum_5d"] and math.isfinite(src.price)

    def test_last_row_blank_never_passes_yesterday_off_as_today(self, yf_stub):
        """末行是空行（当天只有分红、没有 K 线）：去掉后末根是前一场——实时两条路径都不给值（交给下一个源 / 出口回落），不拿前一场冒充今天。
        冻结层同判为「不全」（生产此时不链接这份日线）。变红的变异：忽略第二个返回值。"""
        import data_pipeline as dp
        yf_stub["frame"] = _event_at(_frame().iloc[1:], -1)
        assert dp._fetch_history_metrics("META") is None
        assert dp.YFinanceSource().fetch("META") is None
        p = ol.frame_payload("META", {"period": "1mo"}, yf_stub["frame"])
        with pytest.raises(fc.IncompleteObservations, match="末行"):
            fc.price_fields_from_daily(p, None, True)
        st = pm.event_row_stats()
        assert st["last_row"] == 2 and all(x["last"] for x in st["elsewhere"])

    def test_zero_volume_never_in_the_average(self, yf_stub):
        """19 根真 K 线 + 首行空行 = 20 行：改前均量 = 19/20 × 真均量 ⇒ 量比偏高 20/19。"""
        import data_pipeline as dp
        yf_stub["frame"] = _frame().iloc[:20]
        real = yf_stub["frame"].iloc[1:]
        want = float(real["Volume"].iloc[-1]) / float(real["Volume"].mean())
        stale = pm.history_price_metrics(yf_stub["frame"])["volume_ratio"]
        assert stale == pytest.approx(want * 20 / 19), "正对照：不去掉就偏高 20/19"
        assert dp._fetch_history_metrics("META")["volume_ratio"] == want
        assert dp.YFinanceSource().fetch("META").volume_ratio == want

    def test_backfill_path(self, yf_stub):
        """补跑路径本来就丢 NaN 收盘（v0.45.69），去掉空行后值不变；只多了计数。"""
        import data_pipeline as dp
        df = _frame()
        yf_stub["frame"] = df
        out = dp._fetch_historical_stock_data("META", "2026-10-21")
        assert out.get("_data_unavailable") is not True and all(math.isfinite(out[k]) for k in FIELDS)
        assert pm.event_row_stats()["first_row"] == 1

    def test_backfill_last_row_blank_goes_to_the_snapshot_branch(self, yf_stub, monkeypatch):
        """补跑：目标日只有分红 ⇒ 去掉后末行不是目标日 ⇒ 既有的「改问云端快照 / 标不可用」那一支，不拿前一场冒充目标日。"""
        import cloud_snapshot_loader
        import data_pipeline as dp
        monkeypatch.setattr(cloud_snapshot_loader, "load_official_close", lambda d, t: (None, "snapshot_no_snapshot"))
        yf_stub["frame"] = _event_at(_frame(), -1)
        out = dp._fetch_historical_stock_data("META", "2026-10-21")
        assert out["_data_unavailable"] is True and out["_reason"] == "no_close_on_2026-10-21"

    def test_legacy_cache_path(self, monkeypatch):
        """cache.py 旧回落（data_pipeline 不可用时）：同一个函数、同样第一步去掉。"""
        from swarm_agents import cache
        state = {"hist": _event_at(_frame().iloc[1:], -5)}

        class _Ticker:
            def __init__(self, ticker):
                pass

            def history(self, period="1mo"):
                return state["hist"].copy()
        monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
        monkeypatch.setitem(sys.modules, "data_pipeline", None)
        monkeypatch.setattr(cache.yfinance_limiter, "acquire", lambda *a, **k: None)
        monkeypatch.setattr(cache.yfinance_breaker, "allow_request", lambda *a, **k: True)
        monkeypatch.setattr(cache.yfinance_breaker, "record_success", lambda *a, **k: None)
        with cache._yf_lock:
            cache._yf_cache.clear()
            cache._yf_cache_ts.clear()
            cache._yf_insert_order.clear()
        try:
            out = cache._fetch_stock_data("ZZEV")
        finally:
            with cache._yf_lock:
                cache._yf_cache.clear()
                cache._yf_cache_ts.clear()
                cache._yf_insert_order.clear()
        assert math.isfinite(out["price"]) and math.isfinite(out["momentum_5d"])
        assert pm.event_row_stats()["elsewhere"][0]["ticker"] == "ZZEV"
        state["hist"] = _event_at(_frame().iloc[1:], -1)       # 末行空行 ⇒ 标不可用，不拿前一场冒充今天
        try:
            out = cache._fetch_stock_data("ZZEW")
        finally:
            with cache._yf_lock:
                cache._yf_cache.clear()
                cache._yf_cache_ts.clear()
                cache._yf_insert_order.clear()
        assert out["_data_unavailable"] is True and out["price"] == 0.0 and out["momentum_5d"] is None


# ══════════════════════════════════════════════════════════════════════════
# 5. 调用方第一步就去掉（AST）
# ══════════════════════════════════════════════════════════════════════════

def _is_drop_stmt(stmt) -> bool:
    return (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
            and ast.unparse(stmt.value.func) == "price_metrics.drop_event_only_rows"
            and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Tuple)
            and ast.unparse(stmt.targets[0].elts[0]) == "hist"
            and ast.unparse(stmt.value.args[0]) == "hist")


def _tail_checked(rel, func, cls=None):
    """实时路径拿到第二个返回值后，下一句就是 `if <它>:` 且分支里 return（不拿前一场冒充今天）。"""
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body if cls else tree.body
    fn = next(n for n in scope if isinstance(n, ast.FunctionDef) and n.name == func)
    for node in ast.walk(fn):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for k, stmt in enumerate(block):
                if _is_drop_stmt(stmt):
                    flag = ast.unparse(stmt.targets[0].elts[1])
                    nxt = block[k + 1] if k + 1 < len(block) else None
                    return (isinstance(nxt, ast.If) and ast.unparse(nxt.test) == flag
                            and any(isinstance(x, ast.Return) for x in nxt.body))
    return False


def _fetch_sites(rel, fetch_call, period=None):
    """`hist… = <fetch_call>(…)` 的每一处（给了 period 就只认带这个 period 的），返回 (行号, 紧随其后的那条语句)。"""
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for k, stmt in enumerate(block):
                if (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
                        and ast.unparse(stmt.value.func).endswith(fetch_call)
                        and (period is None or any(kw.arg == "period" and ast.unparse(kw.value) == repr(period)
                                                   for kw in stmt.value.keywords))):
                    out.append((stmt.lineno, block[k + 1] if k + 1 < len(block) else None))
    return out


class TestCallSites:

    def test_data_pipeline_drops_right_after_every_fetch(self):
        """变红的变异：任一处漏调 / 挪到判空或末根判断之后。"""
        sites = _fetch_sites("data_pipeline.py", "yf_history")
        assert len(sites) == 3, sites
        for line, nxt in sites:
            assert nxt is not None and _is_drop_stmt(nxt), f"data_pipeline.py:{line} 取到日线后下一句必须是 drop_event_only_rows"

    @pytest.mark.parametrize("rel,func,cls", [("data_pipeline.py", "_fetch_history_metrics", None),
                                              ("data_pipeline.py", "fetch", "YFinanceSource"),
                                              ("swarm_agents/cache.py", "_fetch_stock_data", None)])
    def test_live_paths_refuse_a_last_row_blank(self, rel, func, cls):
        assert _tail_checked(rel, func, cls), f"{rel}::{func} 拿到「末行是空行」后下一句必须 if … return"

    def test_legacy_cache_drops_right_after_its_fetch(self):
        # 只认算三个字段的那一处（`period="5d"` 的是标的有效性检查，不算字段）
        sites = _fetch_sites("swarm_agents/cache.py", "t.history", period="1mo")
        assert len(sites) == 1 and _is_drop_stmt(sites[0][1]), sites

    def test_frozen_layer_drops_inside_daily_rows(self):
        """冻结层的唯一入口是 daily_rows：实时 / 补跑两条分支都从它取行。"""
        p = _payload()
        assert len(fc.daily_rows(p)) == 22
        rows_seen = []
        orig = fc.price_fields
        try:
            fc.price_fields = lambda rows: rows_seen.append(len(rows)) or orig(rows)
            fc.price_fields_from_daily(p, None, True)
        finally:
            fc.price_fields = orig
        assert rows_seen == [22]


# ══════════════════════════════════════════════════════════════════════════
# 6. 看得见
# ══════════════════════════════════════════════════════════════════════════

def _analyze(tmp_path, counter):
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": {"event_rows": counter}, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    if counter is None:
        del status["scan_timing"]["counters"]["event_rows"]
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return [x for x in a.alerts if "公司行动空行" in x.message], a


_MID = {"ticker": "T", "date": "2026-10-09", "pos": 11, "n": 22}


class TestVisibility:

    def test_counter_and_summary(self):
        pm.drop_event_only_rows(_frame(), "META")
        c = stt.counters()
        assert c["event_rows"]["dropped"] == 1 and c["event_rows"]["first_row"] == 1
        assert "公司行动空行去掉 1 行(非首行 0, 其中末行 0)" in stt.summary_line({"phases": {}, "counters": c})
        pm._reset_event_row_stats()
        assert "公司行动空行" not in stt.summary_line({"phases": {}, "counters": stt.counters()})

    def test_first_row_only_is_silent(self, tmp_path):
        hits, a = _analyze(tmp_path, {"frames": 30, "dropped": 1, "first_row": 1, "elsewhere": [],
                                      "at_scoring": {"frames": 30, "dropped": 1, "first_row": 1, "elsewhere": []}})
        assert hits == [] and not any("公司行动空行" in s for s in a.checks_skipped)

    def test_elsewhere_at_scoring_is_p2(self, tmp_path):
        """变红的变异：analyze() 不调 `_check_event_rows`；首行也报；级别不是 P2。"""
        hits, _ = _analyze(tmp_path, {"frames": 30, "dropped": 2, "first_row": 1, "elsewhere": [_MID],
                                      "at_scoring": {"frames": 30, "dropped": 2, "first_row": 1, "elsewhere": [_MID]}})
        (h,) = hits
        assert h.level == am.AlertLevel.MEDIUM and "T 2026-10-09（第 11 行 / 共 22 行）" in h.details["明细"]

    def test_after_scoring_only_is_info(self, tmp_path):
        hits, _ = _analyze(tmp_path, {"frames": 40, "dropped": 1, "first_row": 0, "elsewhere": [_MID],
                                      "at_scoring": {"frames": 30, "dropped": 0, "first_row": 0, "elsewhere": []}})
        (h,) = hits
        assert h.level == am.AlertLevel.INFO

    def test_message_counts_the_true_total(self, tmp_path):
        """代码评审 #3：明细封顶 20 条，告警按总数说（变红的变异：按 len(elsewhere) 报）；末行标出来。"""
        items = [dict(_MID, pos=i) for i in range(1, 21)]
        items[0]["last"] = True
        c = {"frames": 60, "dropped": 25, "first_row": 0, "last_row": 1, "elsewhere_total": 25, "elsewhere": items}
        hits, _ = _analyze(tmp_path, {**c, "at_scoring": dict(c)})
        (h,) = hits
        assert "25 行" in h.message and "等共 25 条" in h.details["明细"] and "末行" in h.details["明细"]

    def test_after_scoring_beyond_the_cap_still_shows(self, tmp_path):
        items = [dict(_MID, pos=i) for i in range(1, 21)]
        scored = {"frames": 30, "dropped": 20, "first_row": 0, "last_row": 0, "elsewhere_total": 20, "elsewhere": items}
        hits, _ = _analyze(tmp_path, {**scored, "frames": 60, "dropped": 22, "elsewhere_total": 22, "at_scoring": scored})
        (h,) = [x for x in hits if x.level == am.AlertLevel.INFO]
        assert "2 行" in h.message

    def test_no_snapshot_falls_back_and_says_so(self, tmp_path):
        hits, _ = _analyze(tmp_path, {"frames": 30, "dropped": 1, "first_row": 0, "elsewhere": [_MID], "at_scoring": None})
        (h,) = hits
        assert h.level == am.AlertLevel.MEDIUM and "没有评分时的快照" in h.details["口径"]

    def test_missing_counter_is_skipped_not_silent(self, tmp_path):
        hits, a = _analyze(tmp_path, None)
        assert hits == [] and any("公司行动空行" in s for s in a.checks_skipped)

    def test_scan_marks_the_scoring_snapshot(self):
        """扫描在预取结束那一刻拍快照（与 history_fill / 旧回落同一时点）；变红的变异：删掉那段 try。"""
        src = (_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        i, j, k = (src.index("mark_history_fill_scoring()"), src.index("mark_legacy_fallback_scoring()"),
                   src.index("mark_event_row_scoring()"))
        assert i < j < k < src.index('_timing.record("prefetch"')
