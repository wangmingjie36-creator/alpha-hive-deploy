"""期权快照的现价与非有限数：缺了就是缺，不拿别的数冒充（v0.45.462）。

**背景。** `OptionsAgent.analyze` 取不到现价时，两处各自兜底成「OI>100 的看涨行权价中位数」，
再没有就是常数 145.0。这个数被当成现价算 GEX / 异动 / 偏斜 / 期限结构 / ATM IV 窗口，
写进快照的 `_snapshot_stock_price`，又被 price_history 的一次性迁移当「我们自己的观测」收进价格索引。
实测：47 份快照现价 = 145.0（6–7 月）；09-17 全源中断那天 OracleBee 拿到 price=0.0，
30/30 份快照是行权价中位数（NVDA 232.5 vs 真价 219.34），进了当天的 Oracle 分与 final_score。
而 CBOE payload 本来就带着与链同一时刻的真实标的价——`fetch_cboe_chain` 算出来又丢了。

出口消毒 `_sanitize_result` 同一形状：NaN → 0.0（P/C=0 读作极度看多、GEX=0 读作 gamma 中性）。

**纪律。** 每条断言的 docstring 写明什么变异会让它变红。本文件零外部依赖（夹具全是内存 dict），
结构上没有写 `skip` 的余地。
"""

import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import cboe_options as C
import ledger_io
import options_analyzer as oa
import price_history as ph
from options_analyzer import OptionsAgent, OptionsAnalyzer

NAN = float("nan")


# ═══════════════════════════════════════════════════════════════ 现价唯一出处

class TestResolveStockPrice:

    def test_caller_price_wins_and_is_labelled(self):
        """调用方给了有效价 ⇒ 用它（与旧行为一致，调用方给价的日子一个数都不变）。

        变红的变异：链价优先于调用方价；或出处不写 "caller"。
        """
        assert oa._resolve_stock_price(219.34, {"underlying_price": 230.0,
                                                "underlying_price_source": "cboe_close"}) == (219.34, "caller")

    @pytest.mark.parametrize("bad", [None, 0, 0.0, -5.0, NAN, float("inf"), "x", True])
    def test_invalid_caller_price_falls_to_chain_price(self, bad):
        """0 / None / 负数 / NaN / inf / 非数 / bool 都不是现价 ⇒ 用链自带的同时刻标的价。
        NaN 是重点：旧代码 `if not stock_price` 拦不住 NaN（NaN 为真值）。

        变红的变异：去掉 `_math.isfinite`；或把判据写回 `if not x`。
        """
        assert oa._resolve_stock_price(bad, {"underlying_price": 219.34,
                                             "underlying_price_source": "cboe_close"}) == (219.34, "cboe_close")

    @pytest.mark.parametrize("chain", [{}, {"underlying_price": None}, {"underlying_price": 0.0},
                                       {"underlying_price": NAN}, None])
    def test_nothing_real_means_none_never_a_strike(self, chain):
        """两边都没有 ⇒ `(None, "unavailable")`。**不是**行权价中位数、不是 145.0。

        变红的变异：恢复任一兜底（中位数 / 常数）。
        """
        assert oa._resolve_stock_price(0.0, chain) == (None, "unavailable")


class TestCboeChainCarriesUnderlyingPrice:

    def _payload(self, price):
        def _occ(cp, k):
            return f"NVDA261016{cp}{int(k * 1000):08d}"
        opts = [{"option": _occ(cp, k), "open_interest": 500, "iv": 0.3, "gamma": 0.05,
                 "bid": 1.0, "ask": 1.1, "volume": 10, "last_trade_price": 1.05}
                for cp in ("C", "P") for k in (200.0, 220.0, 240.0)]
        return {"options": opts, "current_price": price, "close": price}

    def test_payload_price_returned_even_when_caller_passes_none(self, monkeypatch):
        """`fetch_cboe_chain` 算出的 payload 标的价要带出来——09-17 那天它就在 payload 里，只是被丢了。

        变红的变异：删掉返回 dict 里的 `underlying_price`；或只在调用方给价时才带。
        """
        monkeypatch.setattr(C, "_fetch_cboe_payload", lambda *a, **k: self._payload(219.34))
        monkeypatch.setattr(C, "is_market_open", lambda *a, **k: True)
        chain = C.fetch_cboe_chain("NVDA", 0.0)
        assert chain is not None
        assert chain["underlying_price"] == pytest.approx(219.34)
        assert chain["underlying_price_source"] == "cboe_intraday"

    def test_payload_without_price_gives_none_not_zero(self, monkeypatch):
        """payload 没价 ⇒ None（official_price 给 0.0 的「不可得」不能当价带出）。

        变红的变异：`"underlying_price": _payload_px`（不加 `or None`）。
        """
        monkeypatch.setattr(C, "_fetch_cboe_payload", lambda *a, **k: self._payload(None))
        monkeypatch.setattr(C, "is_market_open", lambda *a, **k: True)
        chain = C.fetch_cboe_chain("NVDA", 0.0)
        assert chain is not None and chain["underlying_price"] is None
        assert chain["underlying_price_source"] == "unavailable"


# ═══════════════════════════════════════════════════════════════ analyze() 端到端

def _chain(underlying=None, src=None, call_gamma=0.05, put_gamma=0.04):
    """行权价围着 300 摆（中位数 = 300），现价若被兜底成中位数就是 300，一眼认得出。"""
    calls = [{"strike": k, "openInterest": 3000, "impliedVolatility": 0.34, "gamma": call_gamma,
              "volume": 50} for k in (280.0, 300.0, 320.0)]
    puts = [{"strike": k, "openInterest": 3000, "impliedVolatility": 0.38, "gamma": put_gamma,
             "volume": 50} for k in (280.0, 300.0, 320.0)]
    out = {"calls": calls, "puts": puts, "expirations": ["2026-11-20"], "source": "real"}
    if underlying is not None:
        out.update(underlying_price=underlying, underlying_price_source=src)
    return out


@pytest.fixture
def agent(monkeypatch, tmp_path, stub_cboe_payload, stub_yfinance):
    import market_intelligence
    monkeypatch.setattr(OptionsAnalyzer, "_iv_term_points_yfinance",
                        staticmethod(lambda ticker, stock_price: ([], ["offline"])))
    monkeypatch.setattr(market_intelligence, "calculate_iv_rv_spread", lambda *_a, **_k: {})
    # conftest 给全部测试关了快照（防 mock 链写进生产 cache/）；本文件测的就是快照内容，
    # cache_dir 已指向 tmp_path 再打开。不打开 ⇒ 「快照现价是 None」会因根本没写快照而无从断言。
    monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
    monkeypatch.setattr(oa, "_snapshot_now", lambda: datetime(2026, 10, 12, 17, 30, tzinfo=ZoneInfo("America/New_York")))
    a = OptionsAgent()
    monkeypatch.setattr(a.fetcher, "cache_dir", str(tmp_path))
    monkeypatch.setattr(a.fetcher, "fetch_historical_hv", lambda t: [0.25 + i * 0.02 for i in range(20)])
    monkeypatch.setattr(a.fetcher, "_save_last_valid_iv", lambda t, iv: None)
    monkeypatch.setattr(a.fetcher, "_read_last_valid_iv", lambda t: None)
    oa.reset_snapshot_slot_stats()
    yield a
    oa.reset_snapshot_slot_stats()


def _snapshot(tmp_path):
    files = list(tmp_path.glob("options_snapshot_*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


class TestAnalyzeNeverInventsAPrice:

    def test_no_price_anywhere_records_none_and_counts(self, agent, monkeypatch, tmp_path):
        """09-17 形状：调用方传 0.0、链也没价 ⇒ 快照现价 None、出处 unavailable、计数 +1，
        依赖现价的量都是不可得——**不是**用 300（行权价中位数）算出来的数。

        变红的变异：恢复 `median(all_strikes)` / 145.0 兜底（快照现价变 300 / 145、GEX 变成数）；
        或删掉 `_snap_count("stock_price_unavailable")`。
        """
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain())
        r = agent.analyze("NVDA", stock_price=0.0)
        snap = _snapshot(tmp_path)
        assert snap["_snapshot_stock_price"] is None
        assert snap["_snapshot_stock_price_source"] == "unavailable"
        assert r["gamma_exposure"] is None, "GEX 不能拿行权价中位数当现价算出来"
        assert r["iv_skew_ratio"] is None
        assert r["iv_raw_observed"] is None, "ATM 窗口定不了位 ⇒ 没有当日原始 IV"
        assert oa.snapshot_slot_stats()["stock_price_unavailable"] == 1

    def test_chain_price_used_when_caller_has_none(self, agent, monkeypatch, tmp_path):
        """调用方没价但链带了同一时刻的标的价 ⇒ 用链价，出处照写。

        变红的变异：`_resolve_stock_price` 不看 `options_chain["underlying_price"]`。
        """
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain",
                            lambda t: _chain(underlying=297.5, src="cboe_close"))
        r = agent.analyze("NVDA", stock_price=None)
        snap = _snapshot(tmp_path)
        assert snap["_snapshot_stock_price"] == pytest.approx(297.5)
        assert snap["_snapshot_stock_price_source"] == "cboe_close"
        assert r["gamma_exposure"] is not None
        assert oa.snapshot_slot_stats()["stock_price_unavailable"] == 0

    def test_nan_caller_price_is_not_written_into_snapshot(self, agent, monkeypatch, tmp_path):
        """调用方传 NaN：旧代码第二处兜底 `if not stock_price` 放过 NaN ⇒ 快照现价写成 NaN。

        变红的变异：在解析之后又用回原始入参写快照。
        """
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain())
        agent.analyze("NVDA", stock_price=NAN)
        snap = _snapshot(tmp_path)
        assert snap["_snapshot_stock_price"] is None

    def test_caller_price_wins_for_every_output(self, agent, monkeypatch, tmp_path):
        """调用方给了价 ⇒ 链自带的价**哪里都不用**：整份结果（除出处标签外）与链不带价时逐字段相同。
        只核快照现价挡不住「某个下游悄悄改用链价」（v0.45.462 那条只核了现价，yfinance 分支的漏洞就此漏过）。

        变红的变异：`_resolve_stock_price` 让链价优先；或任一下游改读 `options_chain["underlying_price"]`。
        """
        monkeypatch.setenv("OPTIONS_SNAPSHOT_DISABLE", "1")
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain())
        a = agent.analyze("NVDA", stock_price=301.0)
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain",
                            lambda t: _chain(underlying=280.0, src="cboe_close"))
        b = agent.analyze("NVDA", stock_price=301.0)
        for r in (a, b):
            r.pop("timestamp", None)
        assert a == b
        assert a["gamma_exposure"] is not None

    def test_result_records_chain_source(self, agent, monkeypatch, tmp_path):
        """快照记下期权链来源（v0.45.475）：此前只能事后推，quote_set.error 还会被回填抹掉（10-05 BILI）。

        变红的变异：删掉结果里的 `chain_source`；或写死成某个值。
        """
        monkeypatch.setenv("OPTIONS_SNAPSHOT_DISABLE", "1")
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: {**_chain(), "_source": "cboe"})
        assert agent.analyze("NVDA", stock_price=301.0)["chain_source"] == "cboe"
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: {**_chain(), "_source": "yfinance"})
        assert agent.analyze("NVDA", stock_price=301.0)["chain_source"] == "yfinance"
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain())
        assert agent.analyze("NVDA", stock_price=301.0)["chain_source"] == "unknown"

    def test_sample_chain_records_chain_source_sample(self, agent, monkeypatch, tmp_path):
        """样本链早退那条返回路径也记 `chain_source: "sample"`（v0.45.479 补测试）。

        变红的变异：删掉样本早退结果里的 `chain_source`。
        """
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: {**_chain(), "source": "sample"})
        assert agent.analyze("NVDA", stock_price=301.0)["chain_source"] == "sample"

    def test_caller_price_unchanged_behaviour(self, agent, monkeypatch, tmp_path):
        """调用方给了价 ⇒ 用它，出处 caller（正常日子行为不变的正对照）。"""
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain",
                            lambda t: _chain(underlying=297.5, src="cboe_close"))
        agent.analyze("NVDA", stock_price=301.0)
        snap = _snapshot(tmp_path)
        assert (snap["_snapshot_stock_price"], snap["_snapshot_stock_price_source"]) == (301.0, "caller")

    def test_no_strike_median_fallback_left_in_analyze(self):
        """源码里不许再有「中位行权价 / 145.0 当现价」的兜底（行为测试之外的第二道：防有人在别的分支加回来）。

        变红的变异：任何地方写回 `statistics.median(all_strikes)` 或 `else 145.0`。
        """
        import inspect
        # 只看代码不看注释：注释里记着这段历史（「当时会兜底成 median(all_strikes)」）
        code = "".join(ln.split("#", 1)[0] for ln in inspect.getsource(OptionsAgent.analyze).splitlines(True))
        code = code.replace(" ", "")
        assert "median(all_strikes)" not in code
        assert "else145.0" not in code


# ═══════════════════════════════════════════════════════════════ 出口消毒

class TestSanitizeToNone:

    def test_nan_and_inf_become_none_with_paths(self):
        """非有限数 → None（不是 0.0），并返回被改写的键路径（供计数 + WARNING）。

        变红的变异：改回 `= 0.0`；或不再返回路径（计数恒 0，触发了也没人知道）。
        """
        r = {"put_call_ratio": NAN, "gex": float("-inf"), "ok": 1.5,
             "iv_skew_detail": {"skew_ratio": NAN},
             "unusual_activity": [{"strike": NAN, "volume": 10}], "xs": [1.0, NAN]}
        paths = oa._sanitize_result(r)
        assert r["put_call_ratio"] is None and r["gex"] is None and r["ok"] == 1.5
        assert r["iv_skew_detail"]["skew_ratio"] is None
        assert r["unusual_activity"][0]["strike"] is None and r["xs"] == [1.0, None]
        assert sorted(paths) == sorted(["put_call_ratio", "gex", "iv_skew_detail.skew_ratio",
                                        "unusual_activity[0].strike", "xs[1]"])

    def test_analyze_counts_sanitized_values(self, agent, monkeypatch, tmp_path):
        """analyze 出口真遇到非有限数 ⇒ `nan_sanitized` 计数（进 status.json），快照里是 null。

        变红的变异：删掉 analyze 里把路径数加进 `_snap_stats["nan_sanitized"]` 的那几行。
        """
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(underlying=300.0, src="caller"))
        monkeypatch.setattr(agent.analyzer, "calculate_put_call_ratio", lambda c, p: NAN)
        agent.analyze("NVDA", stock_price=300.0)
        snap = _snapshot(tmp_path)
        assert snap["put_call_ratio"] is None
        assert oa.snapshot_slot_stats()["nan_sanitized"] >= 1


class TestGexNonFinite:

    def test_nan_gamma_makes_gex_unavailable_not_nan(self):
        """一条合约 gamma 是 NaN ⇒ 和是 NaN，`NaN <= 0` 为 False 挡不住 ⇒ 必须显式判有限。

        变红的变异：删掉 `if not _math.isfinite(total): return None`。
        """
        calls = [{"strike": 100.0, "openInterest": 100, "gamma": NAN}]
        puts = [{"strike": 100.0, "openInterest": 100, "gamma": 0.02}]
        assert OptionsAnalyzer().calculate_gamma_exposure(calls, puts, 100.0) is None

    def test_bs_gamma_rejects_non_finite_inputs(self):
        """NaN 的 iv 穿过 `sigma < 0.01`（比较恒 False）⇒ 必须先判有限。

        变红的变异：删掉 `_bs_gamma` 开头的 isfinite 判断（返回 NaN）。
        """
        assert C._bs_gamma(100.0, 100.0, 0.1, NAN) == 0.0
        assert C._bs_gamma(NAN, 100.0, 0.1, 0.3) == 0.0
        assert C._bs_gamma(100.0, 100.0, 0.1, 0.3) > 0

    def test_cboe_nan_gamma_takes_bs_fallback(self, monkeypatch):
        """CBOE 给 NaN gamma ⇒ 与 0（CBOE 自己的「缺」）同义，走 BS 兜底，链上不留 NaN。

        变红的变异：把 `if not g or not math.isfinite(g)` 改回 `if not g`。
        """
        opts = [{"option": f"NVDA261016{cp}00100000", "open_interest": 100, "iv": 0.3,
                 "gamma": "NaN", "bid": 1.0, "ask": 1.1, "volume": 1, "last_trade_price": 1.0}
                for cp in ("C", "P")]
        monkeypatch.setattr(C, "_fetch_cboe_payload",
                            lambda *a, **k: {"options": opts, "current_price": 100.0, "close": 100.0})
        monkeypatch.setattr(C, "is_market_open", lambda *a, **k: True)
        chain = C.fetch_cboe_chain("NVDA", 0.0)
        gammas = [r["gamma"] for r in chain["calls"] + chain["puts"]]
        assert gammas and all(math.isfinite(g) and g > 0 for g in gammas)


# ═══════════════════════════════════════════════════════════════ yfinance 备用链

class TestYfinanceChainPrice:
    """yfinance 备用链：BS gamma 的 S 取调用方价 ⇒ fast_info 价；链带出 underlying_price（v0.45.468 二次检查）。

    背景：analyze() 调 fetch_options_chain 从不传价。v0.45.462 前 S 兜底成中位行权价（每天都错），
    v0.45.462 删了兜底却没接上本分支本就取到的 fast_info 价 ⇒ yfinance 链 GEX 恒 None（BILI 几乎天天走这条）。
    """

    def _fetcher(self, monkeypatch, fast_info):
        import types
        import pandas as pd

        def _mk():
            return pd.DataFrame({"strike": [14.0, 15.0, 16.0, 17.0], "openInterest": [500, 800, 700, 300],
                                 "volume": [100, 200, 150, 50], "impliedVolatility": [0.5, 0.45, 0.48, 0.52],
                                 "lastPrice": [1.0, 0.6, 0.3, 0.1]})

        class _Chain:
            def __init__(self, e):
                self.calls, self.puts = _mk(), _mk()

        class _Ticker:
            options = ("2026-11-20", "2026-12-18")

            def __init__(self, t):
                self.fast_info = fast_info

            def option_chain(self, e):
                return _Chain(e)

        monkeypatch.setattr(oa, "yf", types.SimpleNamespace(Ticker=_Ticker))
        monkeypatch.setattr(oa, "_opt_cb", None)
        f = oa.OptionsDataFetcher()
        monkeypatch.setattr(f, "_try_cboe", lambda t, p=0.0: None)
        monkeypatch.setattr(f, "_read_cache", lambda *a, **k: None)
        monkeypatch.setattr(f, "_write_cache", lambda *a, **k: None)
        return f

    def test_fast_info_price_feeds_gamma_and_is_carried_out(self, monkeypatch):
        """没给价 ⇒ 用 fast_info lastPrice 算 gamma，并作为 underlying_price（出处 yfinance_last_price）带出。

        变红的变异：S 只看入参（gamma 全空、GEX None）；或删掉结果里的 underlying_price。
        """
        ch = self._fetcher(monkeypatch, {"lastPrice": 15.49}).fetch_options_chain("BILI")
        gammas = [c.get("gamma") for c in ch["calls"]]
        assert gammas and all(isinstance(g, float) and g > 0 for g in gammas)
        assert (ch["underlying_price"], ch["underlying_price_source"]) == (15.49, "yfinance_last_price")
        assert ch["_source"] == "yfinance", "v0.45.475：链要自报来源，analyze 据此记 chain_source"
        S = 15.49
        expect = [C._bs_gamma(S, k, max(c["dte"], 0.5) / 365.0, c["impliedVolatility"])
                  for k, c in zip([14.0, 15.0, 16.0, 17.0], ch["calls"][:4])]
        assert gammas[:4] == pytest.approx(expect, rel=1e-9), "gamma 必须按 fast_info 价算，不是中位行权价"

    def test_nan_last_price_falls_to_previous_close(self, monkeypatch):
        """lastPrice 是 NaN ⇒ 用 previousClose（旧写法 `lastPrice or previousClose` 会取到 NaN）。

        变红的变异：去掉逐个判有限。
        """
        ch = self._fetcher(monkeypatch, {"lastPrice": NAN, "previousClose": 15.2}).fetch_options_chain("BILI")
        assert (ch["underlying_price"], ch["underlying_price_source"]) == (15.2, "yfinance_previous_close")

    def test_no_price_anywhere_means_no_gamma(self, monkeypatch):
        """fast_info 也没价 ⇒ 不补 gamma、underlying_price None——不回到中位行权价。

        变红的变异：恢复中位行权价兜底。
        """
        ch = self._fetcher(monkeypatch, {}).fetch_options_chain("BILI")
        assert all(not c.get("gamma") for c in ch["calls"])
        assert (ch["underlying_price"], ch["underlying_price_source"]) == (None, "unavailable")
        assert OptionsAnalyzer().calculate_gamma_exposure(ch["calls"], ch["puts"], 15.0) is None


# ═══════════════════════════════════════════════════════════════ 快照落盘

class TestSnapshotWriteIsAtomicAndStrict:

    def test_strict_write_refuses_nan_and_keeps_old_file(self, tmp_path):
        """快照写入走 ledger_io：NaN 整份拒写、旧文件逐字节不动、失败在源头登记。

        变红的变异：`_write_snapshot` 改回 `open("w") + json.dump(default=str)`（会截断旧文件、写出 NaN）。
        """
        p = tmp_path / "options_snapshot_NVDA_2026-10-12.json"
        p.write_text('{"old": 1}\n')
        ledger_io.reset_failures()
        with pytest.raises(ledger_io.LedgerError):
            oa._write_snapshot(p, {"x": NAN})
        assert p.read_text() == '{"old": 1}\n'
        assert ledger_io.failures()["n"] >= 1
        ledger_io.reset_failures()

    def test_snapshot_writers_all_route_through_helper(self):
        """四个快照写入方（首写 + 三个原地回写）都不再直接 `json.dump` 到文件。

        变红的变异：任一回写方改回 `open(snap_path, "w")`。（全仓的同类守卫见
        test_ledger_writes_go_through_ledger_io.py，这里只钉本模块四处、给出就近的失败信息。）
        """
        import inspect
        for fn in (OptionsAgent.analyze, OptionsAgent._refresh_price_derived,
                   OptionsAgent._refill_empty_quote_set, OptionsAgent._drop_legacy_gex_signal):
            src = inspect.getsource(fn)
            assert "json.dump(" not in src, fn.__name__
            assert "_write_snapshot(" in src, fn.__name__


# ═══════════════════════════════════════════════════════════════ 价格索引迁移

class TestPriceIndexMigrationOnlyTakesLabelledPrices:

    def _snap(self, tmp_path, date, px, src, complete=True):
        d = {"_snapshot_stock_price": px, "_snapshot_session_complete": complete}
        if src is not None:
            d["_snapshot_stock_price_source"] = src
        (tmp_path / f"options_snapshot_NVDA_{date}.json").write_text(json.dumps(d))

    def test_unlabelled_and_non_close_sources_are_skipped(self, tmp_path, monkeypatch):
        """重迁只收能自证出处的收盘价：旧快照（无出处，可能是 145 / 中位行权价）、盘中价、陈旧盘中文件、
        不可得一律不收；caller / cboe_close 收。

        变红的变异：删掉 `_SNAPSHOT_CLOSE_SOURCES` 判断（旧快照 145.0 重新进索引）；
        或把 cboe_intraday / cboe_stale_intraday 加进集合。
        """
        monkeypatch.setattr(ph, "_is_trading_date", lambda d: True)
        self._snap(tmp_path, "2026-10-05", 145.0, None)
        self._snap(tmp_path, "2026-10-06", 232.5, "cboe_intraday")
        self._snap(tmp_path, "2026-10-07", 230.0, C.STALE_INTRADAY_SOURCE)
        self._snap(tmp_path, "2026-10-08", None, "unavailable")
        self._snap(tmp_path, "2026-10-09", 219.34, "caller")
        self._snap(tmp_path, "2026-10-12", 220.10, "cboe_close")
        assert ph._scan_snapshots("NVDA", str(tmp_path)) == {"2026-10-09": 219.34, "2026-10-12": 220.10}

    def test_caller_price_from_pre_close_snapshot_is_skipped(self, tmp_path, monkeypatch):
        """收盘前写下的快照里，`caller` 价是盘中价，不是收盘（v0.45.468 二次检查）。

        变红的变异：删掉 `_snapshot_session_complete is not True` 那道判断。
        """
        monkeypatch.setattr(ph, "_is_trading_date", lambda d: True)
        self._snap(tmp_path, "2026-10-09", 219.34, "caller", complete=False)
        self._snap(tmp_path, "2026-10-12", 220.10, "caller", complete=True)
        assert ph._scan_snapshots("NVDA", str(tmp_path)) == {"2026-10-12": 220.10}


# ═══════════════════════════════════════════════════════════════ 下游格式化不崩

class TestDownstreamFormattingToleratesNone:

    def test_ml_report_options_views_render_with_nested_none(self):
        """消毒后嵌套字段可能是 None：ML 报告两处期权视图要渲染「—」，不能 TypeError 把整章拖掉。

        变红的变异：把任一 `_fmt(...)` / `_m(...)` 改回裸的 `:,` / `:.0f` 格式化。
        """
        from generate_ml_report import MLEnhancedReportGenerator
        g = object.__new__(MLEnhancedReportGenerator)
        opts = {"data_quality": "real", "options_score": None, "iv_rank": None, "put_call_ratio": None,
                "key_levels": {"support": [{"strike": 280.0, "oi": None}],
                               "resistance": [{"strike": None, "oi": 10}]},
                "unusual_activity": [{"type": "sweep", "strike": None, "volume": None, "bullish": True}],
                "iv_term_structure": {"data_available": True, "front_iv": None, "back_iv": 30.0,
                                      "shape": "contango"}}
        html1 = g._generate_options_section_html(opts)
        html2 = g._ch3_oracle({"OracleBeeEcho": {"score": 5.0, "details": opts}}, opts, 0)
        assert "None" not in html1 and "—" in html1
        assert "$None" not in html2

    def test_gamma_calendar_survives_missing_price(self):
        """现价 None 时只略去「距当前 x%」这句，整份 Gamma 日历照出。

        变红的变异：把守卫改回 `if pin_strike:`（None 价 ⇒ TypeError ⇒ analyze 吞掉 ⇒ 日历整份消失）。
        """
        from market_intelligence import calculate_gamma_expiry_calendar
        calls = [{"expiration": "2026-11-20", "strike": 300.0, "openInterest": 5000, "gamma": 0.02}]
        puts = [{"expiration": "2026-11-20", "strike": 280.0, "openInterest": 1000, "gamma": 0.01}]
        out = calculate_gamma_expiry_calendar(calls, puts, None)
        assert out["pin_expiry"] == "2026-11-20"
        assert "距当前" not in out["charm_note"]
