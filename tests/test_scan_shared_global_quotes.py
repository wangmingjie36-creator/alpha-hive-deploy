"""v0.45.775：全局标的（政体的 ^GSPC 宏观层 / SOXX 板块层，供应链的 TSM / AMAT / ASML / SOXX）一轮扫描只取一次、30 只票共用。

起因（2026-11-13）：ABBV 的 SOXX 板块层缺了——30 只票的线程各取一遍 SOXX，其中一次下载在 yfinance 内部抛了异常、被它自己吞掉
（`TypeError: 'NoneType' object is not subscriptable`，第一异常没人记），ABBV 的政体因此「SOXX 数据不可用」；其余 29 只票取到的 SOXX 层逐只相同。
每轮约 180 次下载是对同一个标的的重复，每一次都是一次「这一层缺」的机会。

本文件锁住：
  ① 开了共享表：同一个全局标的整轮只下载一次（30 个并发线程），结果与不共享时逐项相同；
  ② 没开共享表（单票 CLI / MCP / 测试）：行为逐字等于改动前，每次都自己取，不跨调用缓存；
  ③ 失败不缓存（下一个线程再试 = 天然重试）、连续失败 `SHARED_MAX_FAILS` 次本轮不再试；身份校验丢弃（nan）同样不缓存；
  ④ 共享表只活在一轮里：begin 清旧的、end 关掉（不跨日串数据）；
  ⑤ 接线（扫描开头开、并行阶段结束后关）与告警（整轮没取到的键 ⇒ P2；重试后取到了不报）。
全部离线：`yf_daily.download` 换成按标的造数据、并计数的桩。
"""

import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import pytest

import alert_manager as am
import market_intelligence as mi
import scan_timing


def _frame(sym, n=300):
    base = 100.0 + sum(map(ord, sym)) % 50
    close = base + np.arange(n) * 0.1 + (np.arange(n) % 7) * 0.05
    return pd.DataFrame({"Close": close})


class _Feed:
    """`yf_daily.download` 的桩：按标的出数据并计数；`fail` 里的标的可以按「第几次调用」失败。"""

    def __init__(self):
        self.calls = {}
        self.fail_first = {}          # sym → 前几次调用返回空帧
        self.always_fail = set()
        self.foreign = {}             # sym → 返回别的标的的帧（身份校验会丢弃）
        self.lock = threading.Lock()

    def __call__(self, yf_mod, tickers, *, source, **req):
        sym = tickers if isinstance(tickers, str) else tickers[0]
        with self.lock:
            self.calls[sym] = self.calls.get(sym, 0) + 1
            n = self.calls[sym]
        if sym in self.always_fail or n <= self.fail_first.get(sym, 0):
            return pd.DataFrame(), {sym: False}
        if sym in self.foreign:
            other = self.foreign[sym]
            f = _frame(other)
            f.columns = pd.MultiIndex.from_product([["Close"], [other]])
            return f, {sym: False}
        return _frame(sym), {sym: False}


@pytest.fixture
def feed(monkeypatch):
    f = _Feed()
    import yf_daily
    monkeypatch.setattr(yf_daily, "download", f)
    mi.end_scan_shared()                      # 每条测试从「没开」开始
    mi._SHARED_LAST.clear()
    yield f
    mi.end_scan_shared()
    mi._SHARED_LAST.clear()


TICKERS = [f"T{i:02d}" for i in range(30)]


def _regime_all(ticker_list=TICKERS, workers=8):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(ticker_list, pool.map(mi.detect_market_regime, ticker_list)))


class TestSharing:
    def test_global_symbols_fetched_once_for_30_concurrent_tickers(self, feed):
        mi.begin_scan_shared()
        _regime_all()
        assert feed.calls["^GSPC"] == 1 and feed.calls["SOXX"] == 1, feed.calls
        assert all(feed.calls[t] == 2 for t in TICKERS), "个股自己的 50MA / 20MA 两路仍逐票取"
        st = mi.shared_stats()
        assert st["keys"] == 2 and st["fetches"] == 2 and st["hits"] == 58 and st["failed_attempts"] == 0 and not st["unfilled"]

    def test_result_identical_to_unshared(self, feed):
        shared_first = None
        mi.begin_scan_shared()
        a = _regime_all()
        mi.end_scan_shared()
        feed.calls.clear()
        b = _regime_all()                       # 没开共享表
        assert a == b, "共享表改了结果——同一个标的同一份数据，结果必须逐项相同"
        assert feed.calls["^GSPC"] == 30 and feed.calls["SOXX"] == 30, "没开共享表时每只票各取一遍（行为等于改动前）"
        assert shared_first is None

    def test_not_shared_outside_a_scan(self, feed):
        mi.detect_market_regime("T00")
        mi.detect_market_regime("T01")
        assert feed.calls["SOXX"] == 2 and mi.shared_stats() is None

    def test_begin_clears_previous_round_and_end_closes_it(self, feed):
        mi.begin_scan_shared()
        mi.detect_market_regime("T00")
        mi.begin_scan_shared()                  # 新一轮：旧的不许沿用（不跨日串数据）
        mi.detect_market_regime("T01")
        assert feed.calls["SOXX"] == 2
        end = mi.end_scan_shared()
        assert end["fetches"] == 2 and mi.shared_stats() == end, "结束后统计仍读得到"
        mi.detect_market_regime("T02")
        assert feed.calls["SOXX"] == 3, "end 之后不再共享"


class TestFailures:
    def test_failure_is_not_cached_and_next_caller_retries(self, feed):
        feed.fail_first["SOXX"] = 1
        mi.begin_scan_shared()
        res = [mi.detect_market_regime(t) for t in TICKERS[:5]]
        assert "SOXX 数据不可用" in res[0]["sector_detail"], "第一只碰上了失败：诚实记缺"
        assert all("SOXX 数据不可用" not in r["sector_detail"] for r in res[1:]), "下一只重试取到、之后全部命中"
        assert feed.calls["SOXX"] == 2
        st = mi.shared_stats()
        assert st["failed_attempts"] == 1 and not st["unfilled"] and st["hits"] >= 3

    def test_gives_up_after_max_fails_and_stops_trying(self, feed):
        feed.always_fail.add("SOXX")
        mi.begin_scan_shared()
        res = _regime_all(workers=1)
        assert feed.calls["SOXX"] == mi.SHARED_MAX_FAILS, f"连续失败 {mi.SHARED_MAX_FAILS} 次后本轮不再试：{feed.calls}"
        assert all("SOXX 数据不可用" in r["sector_detail"] for r in res.values())
        st = mi.shared_stats()
        assert st["unfilled"] == ["('ma', 'SOXX', 20, 40)"] and st["gave_up"] == st["unfilled"]

    def test_identity_dropped_frame_is_not_cached(self, feed):
        """身份校验丢弃（请求 SOXX 拿到别的标的）⇒ nan ⇒ 不缓存，下一个线程再试。"""
        feed.foreign["SOXX"] = "NVDA"
        mi.begin_scan_shared()
        r0 = mi.detect_market_regime("T00")
        assert "SOXX 数据不可用" in r0["sector_detail"]
        feed.foreign.clear()
        r1 = mi.detect_market_regime("T01")
        assert "SOXX 数据不可用" not in r1["sector_detail"] and feed.calls["SOXX"] == 2

    def test_exception_in_fetch_counts_as_failure_and_releases_the_key(self, feed, monkeypatch):
        import yf_daily
        boom = {"n": 0}
        orig = feed

        def _dl(yf_mod, tickers, *, source, **req):
            if tickers == "SOXX" and boom["n"] == 0:
                boom["n"] += 1
                raise RuntimeError("yfinance 内部异常")
            return orig(yf_mod, tickers, source=source, **req)

        monkeypatch.setattr(yf_daily, "download", _dl)
        mi.begin_scan_shared()
        r0 = mi.detect_market_regime("T00")                 # detect_market_regime 整体吞异常 ⇒ 不可用，不抛
        assert r0["source"] in ("unavailable", "yfinance")
        r1 = mi.detect_market_regime("T01")
        assert "SOXX 数据不可用" not in r1["sector_detail"], "异常之后键没被锁死"


class TestSupplyChain:
    def test_peers_fetched_once_target_per_ticker_and_same_result(self, feed):
        mi.begin_scan_shared()
        tickers = [f"S{i:02d}" for i in range(10)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            shared = dict(zip(tickers, pool.map(mi.get_supply_chain_signals, tickers)))
        for peer in mi._SUPPLY_CHAIN:
            assert feed.calls[peer] == 1, (peer, feed.calls)
        assert all(feed.calls[t] == 1 for t in tickers)
        mi.end_scan_shared()
        feed.calls.clear()
        plain = {t: mi.get_supply_chain_signals(t) for t in tickers}
        assert shared == plain, "供应链结果必须与不共享时逐项相同"
        assert all(feed.calls[p] == 10 for p in mi._SUPPLY_CHAIN)

    def test_one_failed_peer_is_retried_by_the_next_ticker(self, feed):
        feed.fail_first["ASML"] = 1
        mi.begin_scan_shared()
        a = mi.get_supply_chain_signals("S00")
        b = mi.get_supply_chain_signals("S01")
        assert [p["symbol"] for p in a["peers"]] == ["TSM", "AMAT", "SOXX"], "第一只碰上失败：少一个同行（诚实缺失）"
        assert [p["symbol"] for p in b["peers"]] == ["TSM", "AMAT", "ASML", "SOXX"]
        assert feed.calls["ASML"] == 2


class TestWiring:
    def test_scan_opens_and_closes_the_shared_table(self):
        import alpha_hive_daily_report as m
        init_src = inspect.getsource(m.AlphaHiveDailyReporter._init_scan_context)
        run_src = inspect.getsource(m.AlphaHiveDailyReporter.run_swarm_scan)
        assert "begin_scan_shared()" in init_src, "扫描开头必须开共享表（否则 30 只票照旧各取一遍）"
        assert "end_scan_shared()" in run_src, "并行阶段结束后必须关（不跨轮 / 跨日）"
        assert run_src.index("end_scan_shared()") > run_src.index("_run_pool(retry_items)"), "关得太早：重试轮还要用"


def _analyze(tmp_path, counters):
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": counters, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return a, [x for x in a.alerts if "全局标的整轮没取到" in x.message]


class TestAlert:
    def test_unfilled_key_is_p2(self, tmp_path):
        sq = {"keys": 2, "fetches": 1, "hits": 29, "failed_attempts": 3, "gave_up": ["('ma', 'SOXX', 20, 40)"], "unfilled": ["('ma', 'SOXX', 20, 40)"]}
        a, hits = _analyze(tmp_path, {"shared_quotes": sq})
        (h,) = hits
        assert h.level == am.AlertLevel.MEDIUM and "SOXX" in h.details["没取到的键"]

    def test_retried_but_filled_is_silent(self, tmp_path):
        sq = {"keys": 2, "fetches": 2, "hits": 58, "failed_attempts": 1, "gave_up": [], "unfilled": []}
        a, hits = _analyze(tmp_path, {"shared_quotes": sq})
        assert hits == [] and not any("共享表" in s for s in a.checks_skipped)

    def test_missing_counter_is_skipped_not_silent(self, tmp_path):
        a, hits = _analyze(tmp_path, {})
        assert hits == [] and any("共享表" in s for s in a.checks_skipped)

    def test_counters_and_summary_line(self, feed):
        mi.begin_scan_shared()
        feed.fail_first["SOXX"] = 1
        mi.detect_market_regime("T00")
        mi.detect_market_regime("T01")
        c = scan_timing.counters()
        assert c["shared_quotes"]["failed_attempts"] == 1
        line = scan_timing.summary_line({"phases": {}, "counters": c})
        assert "全局标的共享 取" in line and "失败重试1" in line
        clean = dict(c, shared_quotes={"keys": 2, "fetches": 2, "hits": 58, "failed_attempts": 0, "gave_up": [], "unfilled": []})
        assert "全局标的共享" not in scan_timing.summary_line({"phases": {}, "counters": clean}), "没出事就不占摘要行"
