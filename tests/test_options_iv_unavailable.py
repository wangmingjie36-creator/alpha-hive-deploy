"""期权 IV 不可得就是 None，不拿常数 25.0 冒充（v0.45.471）。

**背景。** `OptionsAgent.analyze` 当日 ATM IV 无效（<5% / >150% / ATM 窗口里没有合约）且
`last_valid_iv` 缓存不可用时，`current_iv = 25.0`。这个常数被当成「当前隐含波动率」：
写进快照与 `signal_archive` 的 `options.iv_current`（进 IC）、`iv_rv_spread = 25 − RV`、
论点失效检查的 `iv`、报告与 LLM 提示词；有真实 IV 历史时还当 IV Rank 的分子 ⇒ `options_score`。
`data_quality` 照判 "real"——raw_ivs 非空但中位数落在 (150%, 200%) 或 (0.5%, 5%) 时 `_has_real_iv`
为真、25.0 又 ≥ 5。实测数据根 21 份快照 `iv_current == 25.0`（2026-04 ~ 09），6 份标 real。

与 v0.45.462 的常数现价 145.0 同一形状（`test_options_snapshot_price_honesty.py`）。

**纪律。** 每条断言的 docstring 写明什么变异会让它变红。本文件零外部依赖（夹具全是内存 dict，
时钟钉死），结构上没有写 `skip` 的余地。
"""

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import iv_history
import market_intelligence as mi
import options_analyzer as oa
import scan_timing
from options_analyzer import OptionsAgent, OptionsAnalyzer

ET = ZoneInfo("America/New_York")
AFTER_CLOSE = datetime(2026, 10, 12, 17, 30, tzinfo=ET)   # 周一收盘后（生产扫描的时刻）
MARKET_OPEN = datetime(2026, 10, 12, 11, 0, tzinfo=ET)    # 周一盘中


def _chain(call_iv, put_iv=0.38):
    """行权价围着 300 摆，调用方给价 300 ⇒ 三档全在 ATM ±20% 窗口里。"""
    calls = [{"strike": k, "openInterest": 3000, "impliedVolatility": call_iv, "gamma": 0.05,
              "volume": 50, "expiry": "2026-11-20"} for k in (280.0, 300.0, 320.0)]
    puts = [{"strike": k, "openInterest": 3000, "impliedVolatility": put_iv, "gamma": 0.04,
             "volume": 50, "expiry": "2026-11-20"} for k in (280.0, 300.0, 320.0)]
    return {"calls": calls, "puts": puts, "expirations": ["2026-11-20"], "source": "real"}


def _pin_clock(monkeypatch, at):
    """analyze 里判开市用 `datetime.now(timezone.utc)`（模块级名字 `oa.datetime`），快照会话用
    `_snapshot_now`。两处一起钉——不钉的话本文件的结果取决于在一天中的什么时候跑。"""
    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz is not None else at.astimezone(ET).replace(tzinfo=None)
    monkeypatch.setattr(oa, "datetime", _FixedDT)
    monkeypatch.setattr(oa, "_snapshot_now", lambda: at)


@pytest.fixture
def saved(monkeypatch):
    return []


@pytest.fixture
def agent(monkeypatch, tmp_path, stub_cboe_payload, stub_yfinance, saved):
    monkeypatch.setattr(OptionsAnalyzer, "_iv_term_points_yfinance",
                        staticmethod(lambda ticker, stock_price: ([], ["offline"])))
    # 记下 analyze 传给 calculate_iv_rv_spread 的 IV（读者端行为另有一组测试）
    monkeypatch.setattr(mi, "calculate_iv_rv_spread",
                        lambda ticker, iv, *a, **k: {"_iv_in": iv, "rv_30d": 30.0,
                                                     "iv_rv_spread": None if iv is None else iv - 30.0,
                                                     "iv_rv_signal": "unknown" if iv is None else "fair"})
    monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
    _pin_clock(monkeypatch, AFTER_CLOSE)
    a = OptionsAgent()
    monkeypatch.setattr(a.fetcher, "cache_dir", str(tmp_path))
    monkeypatch.setattr(a.fetcher, "fetch_historical_hv", lambda t: [25.0 + i for i in range(20)])
    monkeypatch.setattr(a.fetcher, "_save_last_valid_iv", lambda t, iv: saved.append(iv))
    monkeypatch.setattr(a.fetcher, "_read_last_valid_iv", lambda t: None)
    oa.reset_snapshot_slot_stats()
    yield a
    oa.reset_snapshot_slot_stats()


def _snapshot(tmp_path):
    files = list(tmp_path.glob("options_snapshot_*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


# ═══════════════════════════════════════════════════════════════ 生产者

class TestNoConstantIV:

    @pytest.mark.parametrize("call_iv", [1.80, 0.03], ids=["median_180pct", "median_3pct"])
    def test_invalid_raw_no_cache_is_none_and_not_real(self, agent, monkeypatch, tmp_path, call_iv):
        """raw_ivs **非空**、中位数无效（180% 过了 200% 的原始过滤却 >150%；3% 过了 0.5% 却 <5%）、无缓存。

        这正是旧代码判 data_quality="real" 的那条路（`_has_real_iv` 为真、25.0 ≥ 5）。

        变红的变异：恢复 `current_iv = 25.0`（iv_current 变 25.0、data_quality 变 real）；
        或删掉 `_snap_count("iv_unavailable")`；或 data_quality 判据不认 None。
        """
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(call_iv))
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_current"] is None, "IV 不可得 ⇒ None，不是常数"
        assert r["iv_current_source"] == "unavailable"
        assert r["data_quality"] != "real"
        assert r["iv_raw_observed"] is None
        assert r["iv_rv_detail"]["_iv_in"] is None, "不能拿常数去算 IV − RV"
        assert r["iv_rv_spread"] is None
        snap = _snapshot(tmp_path)
        assert snap["iv_current"] is None and snap["iv_current_source"] == "unavailable"
        assert oa.snapshot_slot_stats()["iv_unavailable"] == 1

    def test_no_price_means_no_atm_window_means_none(self, agent, monkeypatch, tmp_path):
        """09-17 形状：现价不可得 ⇒ ATM 窗口定不了位 ⇒ raw_ivs 空 ⇒ 无缓存时 IV 不可得。

        变红的变异：恢复 25.0 兜底（这条路旧代码给 25.0、data_quality degraded）。
        """
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(0.34))
        r = agent.analyze("NVDA", stock_price=0.0)
        assert r["iv_current"] is None
        assert r["data_quality"] == "degraded"
        assert oa.snapshot_slot_stats()["iv_unavailable"] == 1
        assert oa.snapshot_slot_stats()["stock_price_unavailable"] == 1

    def test_valid_cache_still_used_and_labelled(self, agent, monkeypatch, tmp_path):
        """缓存可用 ⇒ 照旧用缓存（本版不改这条路），出处写 last_valid_cache，不计 iv_unavailable。

        变红的变异：把缓存路径也改成 None（过度修复）；或出处标签写错。
        """
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda t: 42.5)
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(1.80))
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_current"] == pytest.approx(42.5)
        assert r["iv_current_source"] == "last_valid_cache"
        assert oa.snapshot_slot_stats()["iv_unavailable"] == 0

    def test_valid_observation_unchanged(self, agent, monkeypatch, saved):
        """当日实拉有效 ⇒ 用它、刷新缓存、data_quality real、出处 observed（正对照：修复不碰好路径）。

        变红的变异：有效路径被误判为不可得。
        """
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(0.34))
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_current"] == pytest.approx(34.0)
        assert r["iv_current_source"] == "observed"
        assert r["data_quality"] == "real"
        assert saved == [pytest.approx(34.0)]
        assert oa.snapshot_slot_stats()["iv_unavailable"] == 0

    # v0.45.474：样本天数必须 ≥ IV_RANK_MIN_DAYS，否则真实 IV 历史分支根本不进，下面这条测试就是空的
    # （v0.45.471 原版给 40 天 < 63：变异恢复 25.0 时它照样红，但红在重复第一条测试的
    # `iv_current is None` 上，它自己声称要守的 iv_rank_source 在变异下也是 hv_proxy）。
    _REAL_DAYS = iv_history.IV_RANK_MIN_DAYS + 7

    def _stub_real_history(self, monkeypatch):
        n = self._REAL_DAYS
        monkeypatch.setattr(iv_history, "load_iv_history",
                            lambda t, d: ([30.0 + i for i in range(n)], n))

    def test_real_history_stub_is_actually_reached(self, agent, monkeypatch):
        """正对照：当日 IV 有效时，同一份历史桩确实把 IV Rank 带进真实 IV 历史分支。
        没有这条，下一条的 `hv_proxy` 可能只是「桩没接上 / 天数不够」造成的。

        变红的变异：样本天数降到阈值以下；或 load_iv_history 的桩没接上。
        """
        self._stub_real_history(monkeypatch)
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(0.34))
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_rank_source"] == f"real_iv_{self._REAL_DAYS}d"

    def test_constant_never_becomes_iv_rank_numerator(self, agent, monkeypatch):
        """有真实 IV 历史时，旧代码把 25.0 当 IV Rank 分子 ⇒ 算出一个看似正常的 real_iv rank ⇒ options_score。
        现在分子是 None ⇒ `iv_rank_from_history` 返回 None ⇒ 走 HV 代理（如实标 hv_proxy）。

        变红的变异：恢复 25.0（iv_rank_source 变 real_iv_70d、iv_rank 由 25 相对 [30, 99] 算成 0.0）；
        或只在分子处兜一个常数（iv_current 仍是 None——所以 rank 断言放在前面，红在它自己身上）。
        """
        self._stub_real_history(monkeypatch)
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(1.80))
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_rank_source"] == "hv_proxy", "不可得的 IV 不能当 IV Rank 分子"
        assert r["iv_current"] is None

    def test_snapshot_hit_repairs_rv_even_without_iv(self, agent, monkeypatch):
        """快照命中时补算 RV（`_refresh_price_derived`）：IV 不可得照样补——RV 是日K 派生、与 IV 无关。

        471 改 None 后，`if _iv:` 让「IV 缺 ⇒ RV 也不补」（此前 25.0 恒为真、照补）。
        变红的变异：恢复 `if _iv:` 守卫。
        """
        cached = {"iv_current": None, "rv_30d": None, "iv_rv_spread": None,
                  "iv_rank": 12.0, "iv_rank_source": "hv_proxy"}
        assert agent._refresh_price_derived(cached, "NVDA") is True
        assert cached["rv_30d"] == pytest.approx(30.0)
        assert cached["iv_rv_spread"] is None and cached["iv_rv_signal"] == "unknown"

    def test_market_open_over_150_is_not_used_or_cached(self, agent, monkeypatch, saved):
        """盘中当日中位数 180%：旧代码开市分支只挡 `< 5%`，>150% 照用并写进缓存
        （与 `_iv_raw_observed` 的 5%~150% 有效口径不一致）。

        变红的变异：开市判据回到 `current_iv < _MIN_VALID_IV`（iv_current 变 180、缓存被写 180）。
        """
        _pin_clock(monkeypatch, MARKET_OPEN)
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(1.80))
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_current"] is None
        assert saved == [], "无效 IV 不能写进 last_valid 缓存"
        assert oa.snapshot_slot_stats()["iv_unavailable"] == 1

    def test_market_open_valid_still_cached(self, agent, monkeypatch, saved):
        """盘中有效 ⇒ 照旧用并缓存（开市判据改动的正对照）。"""
        _pin_clock(monkeypatch, MARKET_OPEN)
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(0.34))
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_current"] == pytest.approx(34.0)
        assert saved == [pytest.approx(34.0)]

    def test_sample_early_return_carries_source_key(self, agent, monkeypatch):
        """样本链早退分支 schema 与主路径一致（读者 `.get("iv_current_source")` 两边都有值）。"""
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain",
                            lambda t: {**_chain(0.34), "source": "sample"})
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_current"] is None and r["iv_current_source"] == "unavailable"


class TestStatusLine:

    def test_summary_line_names_iv_unavailable_only_when_it_happened(self):
        """扫描摘要行（status.json 人读面）出过事才占位，同 stock_price_unavailable。

        变红的变异：删掉摘要行的 IV不可得 片段。
        """
        base = {"writes": 30, "hits": 0, "session_mismatch": 0, "hits_before_close": 0}
        line = scan_timing.summary_line({"phases": {}, "counters": {"options_snapshot": {**base, "iv_unavailable": 2}}})
        assert "IV不可得2次" in line
        line0 = scan_timing.summary_line({"phases": {}, "counters": {"options_snapshot": {**base, "iv_unavailable": 0}}})
        assert "IV不可得" not in line0


# ═══════════════════════════════════════════════════════════════ calculate_iv_rv_spread

class TestIvRvSpreadWithoutIV:

    def test_twelve_data_path_keeps_rv_drops_spread(self, monkeypatch, stub_yfinance):
        """RV 是日K 派生、与 IV 无关 ⇒ 照给；价差与信号不可得。

        变红的变异：删掉 None 守卫（`None - rv` TypeError 被外层吞掉 ⇒ 退回 yfinance ⇒ 离线下 rv 也没了）。
        """
        import twelve_data
        monkeypatch.setattr(twelve_data, "is_configured", lambda: True)
        monkeypatch.setattr(twelve_data, "realized_vol", lambda *a, **k: 31.234)
        d = mi.calculate_iv_rv_spread("NVDA", None)
        assert d["rv_30d"] == pytest.approx(31.23)
        assert d["iv_rv_spread"] is None
        assert d["iv_rv_signal"] == "unknown"
        assert d["data_available"] is False
        # generate_ml_report 原样渲染「数据不可用：<error>」——写人话，不写内部 token（v0.45.474）
        assert "IV 不可得" in d["error"] and d["iv_unavailable"] is True

    def test_twelve_data_path_with_iv_unchanged(self, monkeypatch, stub_yfinance):
        """正对照：有 IV 时与旧行为逐位一致。"""
        import twelve_data
        monkeypatch.setattr(twelve_data, "is_configured", lambda: True)
        monkeypatch.setattr(twelve_data, "realized_vol", lambda *a, **k: 30.0)
        d = mi.calculate_iv_rv_spread("NVDA", 45.0)
        assert d["iv_rv_spread"] == pytest.approx(15.0) and d["iv_rv_signal"] == "expensive"

    def test_yfinance_path_keeps_rv_drops_spread(self, monkeypatch):
        """yfinance 路径同理。

        变红的变异：删掉 yfinance 路径的 None 守卫（TypeError ⇒ 整份 `_empty`，rv_30d 也丢）。
        """
        import numpy as np
        import pandas as pd
        import twelve_data
        import yfinance
        monkeypatch.setattr(twelve_data, "is_configured", lambda: False)
        rng = np.random.default_rng(0)
        closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 40)))
        df = pd.DataFrame({"Close": closes}, index=pd.date_range("2026-08-01", periods=40, freq="B"))
        monkeypatch.setattr(yfinance, "download", lambda *a, **k: df)
        d = mi.calculate_iv_rv_spread("NVDA", None)
        assert d["rv_30d"] is not None and d["rv_30d"] > 0
        assert d["iv_rv_spread"] is None and d["iv_rv_signal"] == "unknown"


# ═══════════════════════════════════════════════════════════════ 读者：不可得渲染成「—」

def _narrative_ctx(iv):
    return {"put_call_ratio": 0.9, "total_oi": 1e6, "iv_current": iv, "iv_rank": 40, "iv_skew": 1.0,
            "iv_skew_signal": "neutral", "flow_direction": "neutral", "key_levels": {},
            # ctx 构造处把 None 价差兜成 0.0、RV 照给（calculate_iv_rv_spread 无 IV 时仍出 RV）
            "iv_rv_spread": 0.0, "rv_30d": 31.2, "iv_rv_signal": "unknown",
            "final_score": 6.0, "options_score": 6.0, "ml_7d": 1.0, "ml_30d": 2.0}


class TestDeepReportReaders:

    def test_narrative_renders_dash_and_skips_iv_rv_paragraph(self):
        """深度报告期权叙事：IV None ⇒ 「IV Current —」，不写 IV-RV 段落。

        变红的变异：`{iv:.1f}` 写回（TypeError，`_local_fallback` 整份报告崩）；
        或 P2 判据去掉 `iv is not None`（写出「IV-RV 价差 +0.0%，期权定价合理」这种编出来的结论）。
        """
        import generate_deep_v2 as g
        out = g._build_options_narrative(_narrative_ctx(None))
        assert "IV Current —" in out
        assert "IV-RV 价差" not in out and "定价合理" not in out

    def test_narrative_with_iv_unchanged(self):
        """正对照：有 IV 时照旧写 IV 与 IV-RV 段落。"""
        import generate_deep_v2 as g
        out = g._build_options_narrative(_narrative_ctx(34.0))
        assert "IV Current 34.0%" in out and "IV-RV 价差" in out

    def test_no_raw_iv_current_format_spec_left(self):
        """LLM 提示词里的 `{ctx['iv_current']:.1f}` 一类写法遇 None 即 TypeError——全部经 `_iv_txt`。

        变红的变异：任一处写回带格式说明符的直接取值（单 / 双引号都认）。
        """
        bad = re.findall(r"""\{ctx(?:\[['"]iv_current['"]\]|\.get\(['"]iv_current['"][^)]*\)):[^}]*\}""",
                         _deep_src())
        assert bad == [], bad

    def test_ctx_keeps_none_for_iv_rv_and_rv30(self):
        """ctx 构造处不能把 `iv_rv_spread` / `rv_30d` 兜成 0.0——否则 v0.45.54 Step 1 提示词的
        「不可得」分支永远走不到，报告照写「IV-RV +0.0%」「HV30 0.0%」（v0.45.474）。

        变红的变异：写回 `odet.get("iv_rv_spread") or 0.0` / `odet.get("rv_30d") or 0.0`；
        或提示词写回 `(ctx.get('iv_rv_spread') or 0):+.1f` / `(ctx.get('rv_30d') or 0):.1f`。
        """
        src = _deep_src()
        assert not re.search(r"""["'](?:iv_rv_spread|rv_30d)["']\s*:\s*odet\.get\([^)]*\)\s*or\s*0""", src)
        assert not re.search(r"""\(ctx\.get\(['"](?:iv_rv_spread|rv_30d)['"][^)]*\)\s*or\s*0\)\s*:""", src)

    def test_scenario_strategy_line_says_unavailable(self):
        """情景推演的策略匹配段（`_local_fallback` 生产路径）：价差不可得 ⇒「IV-RV=不可得」，不是 +0.0%。

        变红的变异：写回 `{iv_rv:+.1f}%` + `float(... or 0)`。
        """
        import generate_deep_v2 as g
        ctx = {"iv_rv_spread": None, "rv_30d": 31.2, "iv_current": None, "iv_skew": 1.0, "iv_rank": 40,
               "final_score": 6.0, "key_levels": {}, "direction_zh": "中性", "direction": "neutral"}
        assert "IV-RV=不可得" in g._build_scenario_narrative(ctx)
        assert "IV-RV=+7.3%" in g._build_scenario_narrative({**ctx, "iv_rv_spread": 7.3})


def _deep_src():
    return (Path(__file__).resolve().parent.parent / "generate_deep_v2.py").read_text(encoding="utf-8")
