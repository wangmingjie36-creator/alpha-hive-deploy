"""Yahoo 求解失败的 IV 在源头剔除（v0.45.482）。

**背景。** Yahoo 的 `impliedVolatility` 是二分法求解的；求解失败时留下减半阶梯 1 / ½ / ¼ / ⅛ / … 2⁻ⁿ（+ 1e-6 量级正偏移）
或地板 1e-05。对 ATM 合约取中位数 ×100 正好得到 50 / 25 / 12.5 / 6.25，落在 5%–150% 有效区间、被当真实观测：
归档里 20 次（12.5 × 11、6.25 × 9），19 次标 real；写进过 last_valid_iv 缓存与自攒 IV 历史（TMO 09-01 = 6.2509）。
10-13 同一 OCC 合约对照 CBOE：阶梯 / 地板 2827 个，99.7% 与 CBOE 相差 > 10 个波动率点。

**纪律。** 每条断言的 docstring 写明什么变异会让它变红。本文件零外部依赖（yfinance 全打桩、时钟钉死、
到期日相对「今天」生成——不埋时间炸弹），结构上没有写 `skip` 的余地。
"""

import asyncio
import json
import types
from datetime import date, timedelta

import pandas as pd
import pytest

import options_analyzer as oa
import scan_timing
from tests.test_options_iv_unavailable import AFTER_CLOSE, _pin_clock
from yahoo_iv import is_placeholder_iv

# 实测的 Yahoo 失败值（快照 key_levels / 10-13 对照）与实测的 Yahoo 真实值
OBSERVED_PLACEHOLDERS = [0.500005, 0.250007, 0.125009, 0.062509, 0.031259, 0.015635, 0.0078155, 1.000005, 1e-05]
OBSERVED_REAL_YAHOO = [0.8300798242187499, 0.765629, 0.623295, 0.4523117, 0.66016, 1.453128]


def _exp(days):
    return (date.today() + timedelta(days=days)).isoformat()


class TestDetector:

    @pytest.mark.parametrize("v", OBSERVED_PLACEHOLDERS)
    def test_observed_failures_flagged(self, v):
        """变红的变异：阶梯只到某一层（如漏掉 1/128）；地板判据删掉；偏移上限收得比实测 1e-5 还紧。"""
        assert is_placeholder_iv(v)

    @pytest.mark.parametrize("v", OBSERVED_REAL_YAHOO + [0.25, 0.5, 1.0, 0.3])
    def test_real_values_and_exact_round_numbers_kept(self, v):
        """真实 Yahoo 值（深度 ≥ 5 的二分结果，含与 CBOE 吻合的 0.7656）与**正好**的整值都不判失败。

        变红的变异：偏移下限放到 0（正好 0.25 / 0.5 被误判——实测 2840 个阶梯命中偏移全部 > 0）；
        或把「二分深度浅」整个当失败（0.765629 深度 6、与 CBOE 0.765 吻合）。
        """
        assert not is_placeholder_iv(v)

    @pytest.mark.parametrize("v", [None, float("nan"), float("inf"), 0, 0.0, -0.125009, "x"])
    def test_non_numbers_are_not_this_kind_of_missing(self, v):
        """非数 / 非有限 / ≤ 0 是另一种缺失，调用方各自处理——这里不认领。"""
        assert not is_placeholder_iv(v)


# ═══════════════════════════════════════════════════════════════ fetch_options_chain（yfinance 分支）

def _yahoo_fetcher(monkeypatch, call_ivs, put_ivs=None, strikes=(280.0, 300.0, 320.0, 290.0, 310.0)):
    n = len(call_ivs)
    put_ivs = put_ivs or [0.38] * n

    def _df(ivs):
        return pd.DataFrame({"strike": list(strikes[:n]), "openInterest": [3000] * n, "volume": [50] * n,
                             "impliedVolatility": list(ivs), "lastPrice": [5.0] * n})

    class _Chain:
        def __init__(self, e):
            self.calls, self.puts = _df(call_ivs), _df(put_ivs)

    class _Ticker:
        options = (_exp(30),)

        def __init__(self, t):
            self.fast_info = {"lastPrice": 300.0}

        def option_chain(self, e):
            return _Chain(e)

    monkeypatch.setattr(oa, "yf", types.SimpleNamespace(Ticker=_Ticker))
    monkeypatch.setattr(oa, "_opt_cb", None)
    f = oa.OptionsDataFetcher()
    monkeypatch.setattr(f, "_try_cboe", lambda t, p=0.0: None)
    monkeypatch.setattr(f, "_read_cache", lambda *a, **k: None)
    monkeypatch.setattr(f, "_write_cache", lambda *a, **k: None)
    return f


class TestChainIngestion:

    def test_failures_zeroed_flagged_counted_and_get_no_gamma(self, monkeypatch):
        """失败值置 0、打 `_iv_placeholder`、计数进链与 snapshot_slot_stats；BS gamma 只给真实 IV 的合约。

        变红的变异：删掉源头剔除（IV 原样留着、gamma 按 0.125 算出来）；或不打标记 / 不计数。
        """
        oa.reset_snapshot_slot_stats()
        ch = _yahoo_fetcher(monkeypatch, [0.125009, 0.4523117, 1e-05]).fetch_options_chain("QCOM")
        ivs = [c["impliedVolatility"] for c in ch["calls"]]
        flags = [c["_iv_placeholder"] for c in ch["calls"]]
        gammas = [c["gamma"] for c in ch["calls"]]
        assert ivs == [0.0, pytest.approx(0.4523117), 0.0]
        assert flags == [True, False, True]
        assert gammas[0] == 0.0 and gammas[2] == 0.0 and gammas[1] > 0
        assert ch["iv_placeholder_contracts"] == 2
        assert oa.snapshot_slot_stats()["iv_placeholder_contracts"] == 2
        oa.reset_snapshot_slot_stats()

    def test_clean_chain_untouched(self, monkeypatch):
        """正对照：全是真实值的 Yahoo 链逐位不变、计数 0。"""
        ch = _yahoo_fetcher(monkeypatch, [0.41, 0.43, 0.45]).fetch_options_chain("NVDA")
        assert [c["impliedVolatility"] for c in ch["calls"]] == [0.41, 0.43, 0.45]
        assert ch["iv_placeholder_contracts"] == 0 and not any(c["_iv_placeholder"] for c in ch["calls"])


# ═══════════════════════════════════════════════════════════════ analyze() 端到端（链由真 fetch 分支产出）

@pytest.fixture
def cache_writes():
    return []


@pytest.fixture
def iv_agent(monkeypatch, tmp_path, stub_cboe_payload, stub_yfinance, cache_writes):
    """收盘后钉钟、快照写 tmp、无 last_valid_iv 缓存（同 test_options_iv_unavailable 的 agent 夹具）。"""
    import market_intelligence as mi
    monkeypatch.setattr(oa.OptionsAnalyzer, "_iv_term_points_yfinance",
                        staticmethod(lambda ticker, stock_price: ([], ["offline"])))
    monkeypatch.setattr(mi, "calculate_iv_rv_spread", lambda *a, **k: {})
    monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
    _pin_clock(monkeypatch, AFTER_CLOSE)
    a = oa.OptionsAgent()
    monkeypatch.setattr(a.fetcher, "cache_dir", str(tmp_path))
    monkeypatch.setattr(a.fetcher, "fetch_historical_hv", lambda t: [25.0 + i for i in range(20)])
    monkeypatch.setattr(a.fetcher, "_save_last_valid_iv", lambda t, iv: cache_writes.append(iv))
    monkeypatch.setattr(a.fetcher, "_read_last_valid_iv", lambda t: None)
    oa.reset_snapshot_slot_stats()
    yield a
    oa.reset_snapshot_slot_stats()


def _analyze_with(iv_agent, monkeypatch, call_ivs):
    strikes = (280.0, 300.0, 320.0, 290.0, 310.0)
    chain = _yahoo_fetcher(monkeypatch, call_ivs, strikes=strikes).fetch_options_chain("QCOM")
    monkeypatch.setattr(iv_agent.fetcher, "fetch_options_chain", lambda t: chain)
    return iv_agent.analyze("QCOM", stock_price=300.0)


class TestAnalyzeAtmWindow:

    def test_all_failures_no_longer_become_12_5(self, iv_agent, monkeypatch):
        """QCOM 06-23 形状：ATM 全是阶梯值。旧代码中位数 0.125009 ⇒ iv_current 12.5、data_quality real。

        变红的变异：删掉源头剔除（中位数又是 12.5）；或删掉多数规则后剔除仍让窗口为空——两条都要守。
        """
        r = _analyze_with(iv_agent, monkeypatch, [0.062509, 0.125009, 0.250007])
        assert r["iv_current"] is None, "失败值的中位数不是观测"
        assert r["data_quality"] != "real"
        assert r["iv_placeholders_atm"] == 3
        assert r["iv_raw_observed"] is None
        assert oa.snapshot_slot_stats()["iv_placeholder_majority"] == 1

    def test_majority_failures_do_not_trust_the_survivors(self, iv_agent, monkeypatch):
        """3 个失败 + 2 个看似正常 ⇒ 不取剩下两个的中位数（10-13 对照：失败主导的链里幸存值 67% 也差 > 10 点）。

        变红的变异：删掉多数规则（iv_current 变 42.0）；或窗口计数不看 `_iv_placeholder` 标记。
        """
        r = _analyze_with(iv_agent, monkeypatch, [0.125009, 0.062509, 0.250007, 0.41, 0.43])
        assert r["iv_current"] is None
        assert r["iv_placeholders_atm"] == 3

    def test_tie_counts_as_majority(self, iv_agent, monkeypatch):
        """失败与有效各 2 ⇒ 也按不可得（门槛是「不少于」：一半的合约求解失败，剩下的一半同样不可信）。

        变红的变异：门槛改成严格多于（iv_current 变 42.0）。
        """
        r = _analyze_with(iv_agent, monkeypatch, [0.125009, 0.062509, 0.41, 0.43])
        assert r["iv_current"] is None
        assert r["iv_placeholders_atm"] == 2

    def test_minority_failures_dropped_rest_used(self, iv_agent, monkeypatch, cache_writes):
        """1 个失败 + 3 个真实 ⇒ 用真实那 3 个的中位数 43.0，照常算观测、刷新缓存。

        变红的变异：删掉源头剔除（中位数混进 0.0625 变 42.0）；或多数规则误伤少数情形（变 None）。
        """
        r = _analyze_with(iv_agent, monkeypatch, [0.062509, 0.41, 0.43, 0.45])
        assert r["iv_current"] == pytest.approx(43.0)
        assert r["iv_current_source"] == "observed"
        assert r["iv_placeholders_atm"] == 1
        assert cache_writes == [pytest.approx(43.0)]
        assert oa.snapshot_slot_stats()["iv_placeholder_majority"] == 0


# ═══════════════════════════════════════════════════════════════ 期限结构 / MCP 展示 / 摘要行

class TestTermStructureYahoo:

    def test_placeholders_excluded_from_atm_term_points(self, monkeypatch):
        """期限结构的 yfinance 路径（0.02 < iv < 2.0 挡不住 ½ / ¼ / ⅛ / 1/16 / 1/32）：每个到期日 ATM 一真一假，
        平均值必须只来自真的 0.30。

        变红的变异：删掉期限结构里的 `not is_placeholder_iv`（atm_iv 变 21.3）。
        """
        class _Chain:
            def __init__(self, e):
                self.calls = pd.DataFrame({"strike": [99.0, 100.0, 101.0, 120.0],
                                           "impliedVolatility": [0.30, 0.125009, 0.30, 0.5]})

        class _Ticker:
            options = tuple(_exp(d) for d in (20, 45, 75, 120, 200))

            def __init__(self, t):
                pass

            def option_chain(self, e):
                return _Chain(e)

        monkeypatch.setattr(oa, "yf", types.SimpleNamespace(Ticker=_Ticker))
        pts, errors = oa.OptionsAnalyzer._iv_term_points_yfinance("NVDA", 100.0)
        assert pts, errors
        assert all(p["atm_iv"] == pytest.approx(30.0) for p in pts), pts


class TestMcpSnapshotDisplay:

    def _call(self, monkeypatch, call_iv):
        import alpha_hive_mcp as m

        def _df(iv):
            return pd.DataFrame({"strike": [100.0, 105.0], "openInterest": [900, 10], "volume": [5, 1],
                                 "impliedVolatility": [iv, 0.4]})

        class _Ticker:
            options = (_exp(30),)

            def __init__(self, t):
                pass

            def option_chain(self, e):
                return types.SimpleNamespace(calls=_df(call_iv), puts=_df(0.4))

        monkeypatch.setattr(m.yf, "Ticker", _Ticker)
        fn = getattr(m.alphahive_get_options_snapshot, "fn", m.alphahive_get_options_snapshot)
        return json.loads(asyncio.run(fn(m.TickerInput(ticker="NVDA"))))

    def test_placeholder_shown_as_none(self, monkeypatch):
        """MCP 展示的最高 OI 合约 IV：失败值写 None，不写 0.125009（≈ 12.5%）。

        变红的变异：删掉 MCP 里的替换。
        """
        out = self._call(monkeypatch, 0.125009)
        assert out["highest_oi_call"]["impliedVolatility"] is None
        assert out["highest_oi_put"]["impliedVolatility"] == pytest.approx(0.4)

    def test_real_value_kept(self, monkeypatch):
        out = self._call(monkeypatch, 0.4523117)
        assert out["highest_oi_call"]["impliedVolatility"] == pytest.approx(0.4523117)


class TestStatusLine:

    def test_counts_show_only_when_nonzero(self):
        """变红的变异：删掉摘要行的两段。"""
        base = {"writes": 30, "hits": 0, "session_mismatch": 0, "hits_before_close": 0}
        line = scan_timing.summary_line({"phases": {}, "counters": {"options_snapshot": {
            **base, "iv_placeholder_contracts": 7, "iv_placeholder_majority": 1}}})
        assert "Yahoo失败IV7个" in line and "失败IV主导1次" in line
        line0 = scan_timing.summary_line({"phases": {}, "counters": {"options_snapshot": {
            **base, "iv_placeholder_contracts": 0, "iv_placeholder_majority": 0}}})
        assert "Yahoo失败IV" not in line0 and "失败IV主导" not in line0
