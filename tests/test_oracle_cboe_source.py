"""Oracle 期限结构 / skew 换 CBOE 源 + Bear P/E 复活 + 世代边界（v0.45.128）

两项都是评分输入变更（用户决策）。固化的事实：
  · yfinance 期权链路径「活着」但输出垃圾——09-04 近月 ATM IV 中位 15.6%、9/29 只 <8%
    （NVDA 4.44% vs CBOE 33.4%），skew 中位 ~1.2 vs CBOE ~1.0；
  · Bear 的 fast_info.pe_ratio 在 yfinance 1.2.0 恒 None，496 条 Bear 条目零 P/E 信号。

这里守五条：
  1. 期限结构 / skew 的**档位与分值一字不变**，只换 IV 来源（09-04 NVDA/COST 的 CBOE 数复算得同结论）
  2. CBOE 结果缺席 → unknown / adj=0，**不回退 yfinance**（Oracle 全程零 yfinance 调用）
  3. max pain 去掉 yfinance 兜底：CBOE 取不到即 None
  4. Bear P/E：三档 35/50/80；None / bool / NaN / 负数一律按无 P/E；来源标签由 pe 推导
  5. 世代边界已追加且是最后一条
"""

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yfinance

from pheromone_board import PheromoneBoard
from swarm_agents.oracle_bee import OracleBeeEcho, _finite_pos


def _no_yf(monkeypatch):
    tk = MagicMock(side_effect=AssertionError("Oracle 深度分析不许碰 yfinance"))
    dl = MagicMock(side_effect=AssertionError("Oracle 深度分析不许碰 yfinance"))
    monkeypatch.setattr(yfinance, "Ticker", tk)
    monkeypatch.setattr(yfinance, "download", dl)
    return tk, dl


def _ts(front, back, avail=True, source="cboe"):
    return {"iv_term_structure": {"data_available": avail, "front_iv": front, "back_iv": back,
                                  "iv_spread": (back - front) if (front is not None and back is not None) else None,
                                  "source": source}}


# ───────────────────────────────────────────── 1. 期限结构档位不变、只换源
class TestTermStructureFromCboe:
    @pytest.mark.parametrize("front,back,structure,adj", [
        (30.0, 35.0, "contango", +0.3),            # spread +5
        (30.0, 31.0, "flat", 0.0),                 # +1
        (30.0, 27.0, "backwardation", -0.4),       # -3
        (30.0, 24.0, "severe_backwardation", -0.8),  # -6
        (30.0, 32.0, "flat", 0.0),                 # 边界：+2.0 不算 contango（> 2.0 才算）
        (30.0, 32.5, "contango", +0.3),            # +2.5：落在 (2.0, 3.0] 的档内——档位挪到 3.0 会红
        (30.0, 25.5, "backwardation", -0.4),       # -4.5：落在 (-5.0, -2.0] 的档内
    ])
    def test_tiers_identical_to_old_yfinance_path(self, monkeypatch, front, back, structure, adj):
        _no_yf(monkeypatch)
        r = OracleBeeEcho._term_structure_adj(_ts(front, back))
        assert (r["structure"], r["term_score_adj"]) == (structure, adj)
        assert r["near_iv"] == front and r["far_iv"] == back and r["source"] == "cboe"
        assert r["summary"].startswith(f"TermStr:{structure}(")

    def test_09_04_nvda_and_cost_replay(self, monkeypatch):
        """09-04 生产数据：NVDA 33.4→38.1 是 contango；COST 23.8→22.9 是 flat（yfinance 当天
        把 COST 也判成 contango，靠的是 4.16% 的假近月 IV）。"""
        _no_yf(monkeypatch)
        nvda = OracleBeeEcho._term_structure_adj(_ts(33.4, 38.1))
        cost = OracleBeeEcho._term_structure_adj(_ts(23.8, 22.9))
        assert (nvda["structure"], nvda["term_score_adj"]) == ("contango", 0.3)
        assert (cost["structure"], cost["term_score_adj"]) == ("flat", 0.0)

    @pytest.mark.parametrize("res", [
        {}, None, {"iv_term_structure": None},
        _ts(30.0, 35.0, avail=False),
        _ts(None, 35.0), _ts(30.0, None),
        _ts(float("nan"), 35.0), _ts(True, 35.0), _ts(0.0, 35.0), _ts(-3.0, 35.0),
    ])
    def test_unavailable_is_unknown_and_never_falls_back(self, monkeypatch, res):
        tk, dl = _no_yf(monkeypatch)
        r = OracleBeeEcho._term_structure_adj(res)
        assert r["structure"] == "unknown" and r["term_score_adj"] == 0.0 and r["source"] == "none"
        tk.assert_not_called(); dl.assert_not_called()


# ───────────────────────────────────────────── 2. skew 档位不变、只换源
class TestSkewFromCboe:
    @pytest.mark.parametrize("skew,adj", [
        (1.40, -0.6), (1.20, -0.3), (1.00, 0.0), (0.80, +0.2), (0.65, +0.4),
        (1.30, -0.3), (1.15, 0.0),   # 边界：> 才升档
    ])
    def test_tiers_identical_to_old_yfinance_path(self, monkeypatch, skew, adj):
        _no_yf(monkeypatch)
        r = OracleBeeEcho._skew_adj({"iv_skew_ratio": skew,
                                     "iv_skew_detail": {"skew_ratio": skew, "otm_put_iv": 33.2, "otm_call_iv": 31.0}})
        assert r["skew_score_adj"] == adj and r["skew_25d"] == round(skew, 3)
        assert r["otm_put_iv"] == 33.2 and r["otm_call_iv"] == 31.0
        assert ("恐慌对冲" in r["summary"]) is (skew > 1.3)

    def test_ratio_without_detail_still_used(self, monkeypatch):
        _no_yf(monkeypatch)
        assert OracleBeeEcho._skew_adj({"iv_skew_ratio": 1.5})["skew_score_adj"] == -0.6

    def test_09_04_nvda_cost_replay(self, monkeypatch):
        """yfinance 当天给 NVDA 1.243 / COST 1.281（各扣 0.3）；CBOE 是 1.07 / 0.991 → 不扣。"""
        _no_yf(monkeypatch)
        assert OracleBeeEcho._skew_adj({"iv_skew_ratio": 1.07})["skew_score_adj"] == 0.0
        assert OracleBeeEcho._skew_adj({"iv_skew_ratio": 0.991})["skew_score_adj"] == 0.0

    @pytest.mark.parametrize("res", [{}, None, {"iv_skew_ratio": None}, {"iv_skew_ratio": float("nan")},
                                     {"iv_skew_ratio": True}, {"iv_skew_ratio": 0.0}, {"iv_skew_ratio": -1.0}])
    def test_unavailable_is_unknown_and_never_falls_back(self, monkeypatch, res):
        tk, dl = _no_yf(monkeypatch)
        r = OracleBeeEcho._skew_adj(res)
        assert r["skew_25d"] is None and r["skew_score_adj"] == 0.0 and r["source"] == "none"
        tk.assert_not_called(); dl.assert_not_called()


class TestFinitePos:
    @pytest.mark.parametrize("x,ok", [(1.0, True), (30, True), (0.0, False), (-1.0, False),
                                      (float("nan"), False), (float("inf"), False),
                                      (True, False), (None, False), ("30", False)])
    def test_guard(self, x, ok):
        assert _finite_pos(x) is ok


# ───────────────────────────────────────────── 3. max pain 不再回退 yfinance
class TestMaxPainNoFallback:
    """⚠️ v0.45.188 起 `_calc_max_pain` 的数据源换成 `full_chain_oi`。

    「不回退 yfinance」这条承诺不变（本类原本的用意），但「拿得到数据就能算」
    那条必须换到新源上测 —— 旧版用 `fetch_cboe_chain` 喂链，而那条链按设计排除
    DTE<7，正是 v0.45.188 要治的病。口径本身的守卫在
    `tests/test_near_max_pain_window.py`。
    """

    def test_missing_chain_yields_none_without_yfinance(self, monkeypatch):
        tk, dl = _no_yf(monkeypatch)
        bee = OracleBeeEcho(PheromoneBoard())
        r = bee._calc_max_pain("NVDA", 100.0, {"full_chain_oi": None})
        assert r["max_pain"] is None and r["summary"] == ""
        tk.assert_not_called(); dl.assert_not_called()

    def test_full_chain_oi_still_computes(self, monkeypatch):
        """有 `full_chain_oi` 就照常算，且全程不碰 yfinance。"""
        today = date(2026, 9, 10)
        monkeypatch.setattr("swarm_agents.oracle_bee.pdt_today", lambda: today.isoformat())
        near = (today + timedelta(days=2)).isoformat()
        leg = {str(k): {near: oi} for k, oi in ((90.0, 100), (100.0, 5000), (110.0, 100))}
        chain = {"data_available": True, "call_exp_oi": leg, "put_exp_oi": leg}
        _no_yf(monkeypatch)
        r = OracleBeeEcho(PheromoneBoard())._calc_max_pain(
            "NVDA", 100.0, {"full_chain_oi": chain})
        assert r["max_pain"] == 100.0
        assert r["expiries_used"] == [near]


# ───────────────────────────────────────────── 4. Bear P/E 复活
class TestBearPeRevived:
    def _bee(self, info):
        from swarm_agents.bear_bee import BearBeeContrarian
        b = BearBeeContrarian(PheromoneBoard())
        b._prefetched_market = {"info": {"NVDA": info}}
        return b

    @pytest.mark.parametrize("pe,expected,label", [
        (90.0, 7.0, "yfinance"), (60.0, 5.0, "yfinance"), (40.0, 3.5, "yfinance"),
        (20.0, 0.0, "yfinance"),          # 拿到了但不过档：来源仍是 yfinance，只是不加分
        (35.0, 0.0, "yfinance"),          # 边界：> 35 才算
    ])
    def test_tiers(self, monkeypatch, pe, expected, label):
        tk, _ = _no_yf(monkeypatch)
        sig, src = [], {}
        got = self._bee({"trailingPE": pe})._assess_valuation("NVDA", {"price": 100.0}, 0.0, 100.0, sig, src)
        assert got == expected and src["valuation"] == label
        tk.assert_not_called()

    @pytest.mark.parametrize("raw", [None, True, False, float("nan"), float("inf"), -12.0, 0.0, "45"])
    def test_garbage_is_no_pe(self, monkeypatch, raw):
        _no_yf(monkeypatch)
        sig, src = [], {}
        got = self._bee({"trailingPE": raw})._assess_valuation("NVDA", {"price": 100.0}, 0.0, 100.0, sig, src)
        assert got == 0.0 and src["valuation"] == "unavailable" and not any("P/E" in x for x in sig)

    def test_pe_and_momentum_take_max_not_sum(self, monkeypatch):
        _no_yf(monkeypatch)
        got = self._bee({"trailingPE": 90.0})._assess_valuation("NVDA", {"price": 100.0}, 20.0, 100.0, [], {})
        assert got == 8.0, "动量 8.0 与 P/E 7.0 取 max（原逻辑不变）"

    def test_info_fetch_failure_is_no_pe(self, monkeypatch):
        from swarm_agents.bear_bee import BearBeeContrarian
        tk = MagicMock(side_effect=OSError("429"))
        monkeypatch.setattr(yfinance, "Ticker", tk)
        b = BearBeeContrarian(PheromoneBoard()); b._prefetched_market = {}
        src = {}
        assert b._assess_valuation("NVDA", {"price": 100.0}, 0.0, 100.0, [], src) == 0.0
        assert src["valuation"] == "unavailable"


# ───────────────────────────────────────────── 5. 世代边界
class TestCohortBoundaryAppended:
    def test_this_versions_entry_is_registered(self):
        """v0.45.128 的世代边界登记了、内容对、且表仍是「只追加、按时间递增」。

        ⚠️ v0.45.151 改写：原断言取 `_COHORT_HISTORY[-1]` 并钉死 v0.45.128，
        即「本版必须**永远**是最后一条」——而同一份表的 docstring 要求
        「任何再次改动 expected_returns / predict_probability / RivalBee 特征来源
        都必须追加一条」。两者直接冲突：**照规矩追加就会让这条测试变红**，
        于是它保护的不是不变式，而是「别再追加了」。
        本版改为断言这一条**存在且内容正确**，把「还是不是最新」交给
        各自版本自己的测试（如 test_rival_bee_catalyst_missing.py）。
        """
        import ic_rerun_readiness as rr
        hits = [(d, v, r) for d, v, r in rr._COHORT_HISTORY if v == "v0.45.128"]
        assert len(hits) == 1, "v0.45.128 的世代边界应恰好登记一条"
        date, _, reason = hits[0]
        assert date == "2026-09-05"
        assert "Oracle" in reason and "P/E" in reason and "世代边界" in reason
        dates = [d for d, _, _ in rr._COHORT_HISTORY]
        assert dates == sorted(dates), "只追加、按时间递增"
