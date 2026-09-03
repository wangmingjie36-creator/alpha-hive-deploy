"""v0.45.99 四合约报价集（select_quote_set / fetch_cboe_quote_set / analyze 挂载）

全部离线：payload 手工合成、`now` 固定，不碰网络、不写 cache/。
"""

import math
from datetime import datetime

import pytest

import cboe_options
from cboe_options import select_quote_set
from options_analyzer import OptionsAgent

NOW = datetime(2026, 9, 3, 10, 0)  # 固定「今天」，DTE 全部从它倒推


def _occ(expiry: str, cp: str, strike: float, tk: str = "XYZ") -> str:
    """'2026-10-02', 'C', 100.0 → 'XYZ261002C00100000'"""
    yy, mm, dd = expiry[2:4], expiry[5:7], expiry[8:10]
    return f"{tk}{yy}{mm}{dd}{cp}{int(round(strike * 1000)):08d}"


def _row(expiry, cp, strike, *, bid, ask, delta, iv=0.30, oi=100.0, **extra):
    r = {"option": _occ(expiry, cp, strike), "bid": bid, "ask": ask, "delta": delta,
         "iv": iv, "open_interest": oi, "volume": 1.0, "gamma": 0.01, "vega": 0.1,
         "theta": -0.05, "theo": (bid + ask) / 2 if isinstance(bid, float) and isinstance(ask, float) else None,
         "last_trade_time": "2026-09-03T09:37:10"}
    r.update(extra)
    return r


# 相对 NOW=2026-09-03：09-06 → 3 DTE；10-01 → 28 DTE；10-08 → 35 DTE；11-20 → 78 DTE
EXP_3, EXP_28, EXP_35, EXP_78 = "2026-09-06", "2026-10-01", "2026-10-08", "2026-11-20"


def _strip(expiry, S=100.0, *, bid_scale=1.0):
    """一个到期日的合成条：行权价 90..110，delta 从 call 0.9→0.1 / put -0.1→-0.9。"""
    rows = []
    strikes = [90, 95, 100, 105, 110]
    call_deltas = [0.90, 0.70, 0.50, 0.30, 0.12]
    put_deltas = [-0.10, -0.30, -0.50, -0.70, -0.88]
    for k, cd, pd in zip(strikes, call_deltas, put_deltas):
        c_mid = max(S - k, 0) + 2.0
        p_mid = max(k - S, 0) + 2.0
        rows.append(_row(expiry, "C", k, bid=round(c_mid - 0.1, 2) * bid_scale,
                         ask=round(c_mid + 0.1, 2), delta=cd))
        rows.append(_row(expiry, "P", k, bid=round(p_mid - 0.1, 2) * bid_scale,
                         ask=round(p_mid + 0.1, 2), delta=pd))
    return rows


def _payload(*expiries, S=100.0, **kw):
    return {"current_price": S, "close": S, "iv30": 0.31,
            "options": [r for e in expiries for r in _strip(e, S, **kw)]}


# ==================== 到期日选择 ====================

class TestExpirySelection:
    def test_picks_nearest_to_30_ignoring_dte_below_7(self):
        qs = select_quote_set(_payload(EXP_3, EXP_28, EXP_78), 100.0, now=NOW)
        assert qs["data_available"] is True
        assert qs["selected_expiry"] == EXP_28
        assert qs["selected_dte"] == 28

    def test_tie_goes_to_later_expiry(self):
        # 28 与 35 离 30 的距离分别是 2 和 5 → 28；改成 25 vs 35 才是平手
        exp_25 = "2026-09-28"
        qs = select_quote_set(_payload(exp_25, EXP_35), 100.0, now=NOW)
        assert qs["selected_expiry"] == EXP_35, "平手必须取更远的到期日"
        qs2 = select_quote_set(_payload(EXP_28, EXP_35), 100.0, now=NOW)
        assert qs2["selected_expiry"] == EXP_28

    def test_dte_3_alone_is_not_a_candidate(self):
        qs = select_quote_set(_payload(EXP_3), 100.0, now=NOW)
        assert qs["data_available"] is False
        assert "dte>=7" in qs["error"]
        assert qs["contracts"] == {"atm_call": None, "atm_put": None, "c25": None, "p25": None}
        assert qs["atm_straddle_mid"] is None

    def test_empty_payload(self):
        assert select_quote_set({"options": []}, 100.0, now=NOW)["data_available"] is False
        assert select_quote_set({}, 100.0, now=NOW)["data_available"] is False


# ==================== 合约选择 ====================

class TestContractSelection:
    def test_atm_strike_nearest_to_S(self):
        qs = select_quote_set(_payload(EXP_28), 103.0, now=NOW)
        c = qs["contracts"]
        assert c["atm_call"]["strike"] == 105.0
        assert c["atm_put"]["strike"] == 105.0
        assert c["atm_call"]["type"] == "C" and c["atm_put"]["type"] == "P"
        assert c["atm_call"]["role"] == "atm_call" and c["atm_put"]["role"] == "atm_put"
        assert c["atm_call"]["expiry"] == EXP_28 and c["atm_call"]["dte"] == 28
        assert c["atm_call"]["symbol"] == _occ(EXP_28, "C", 105.0)

    def test_25_delta_picks_by_delta_not_strike(self):
        qs = select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        c = qs["contracts"]
        # call delta 0.30 在 105、put delta -0.30 在 95 —— 最接近 ±0.25
        assert c["c25"]["strike"] == 105.0 and c["c25"]["delta"] == 0.30
        assert c["p25"]["strike"] == 95.0 and c["p25"]["delta"] == -0.30
        assert c["c25"]["role"] == "c25" and c["p25"]["role"] == "p25"

    def test_quote_math_and_straddle(self):
        qs = select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        ac, ap = qs["contracts"]["atm_call"], qs["contracts"]["atm_put"]
        assert ac["quote_ok"] and ap["quote_ok"]
        assert ac["bid"] == 1.9 and ac["ask"] == 2.1 and ac["mid"] == 2.0
        assert ac["spread_pct"] == pytest.approx(0.1, abs=1e-4)
        assert qs["atm_straddle_mid"] == pytest.approx(4.0)
        assert qs["implied_move_pct"] == pytest.approx(4.0)
        assert qs["underlying_price"] == 100.0
        assert qs["iv30"] == 0.31
        assert qs["source"] == "cboe" and qs["target_dte"] == 30
        # v0.45.104：注入 now 后这两个必须来自注入钟，不是挂钟
        assert qs["fetched_at"] == NOW.isoformat()
        assert qs["market_open"] is True          # NOW = 周四 10:00 ET，盘中

    def test_contract_has_full_schema(self):
        qs = select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        expected = {"symbol", "type", "role", "strike", "expiry", "dte", "bid", "ask", "mid",
                    "spread_pct", "iv", "delta", "gamma", "vega", "theta", "oi", "volume",
                    "theo", "last_trade_time", "quote_ok"}
        for role in ("atm_call", "atm_put", "c25", "p25"):
            assert set(qs["contracts"][role]) == expected, role


# ==================== 诚实降级 ====================

class TestHonestDegradation:
    def test_zero_bid_is_not_a_quote(self):
        payload = _payload(EXP_28)
        for r in payload["options"]:
            if r["option"] == _occ(EXP_28, "C", 100.0):
                r["bid"] = 0.0
        qs = select_quote_set(payload, 100.0, now=NOW)
        ac = qs["contracts"]["atm_call"]
        assert ac["quote_ok"] is False
        assert ac["mid"] is None and ac["spread_pct"] is None
        assert ac["bid"] == 0.0 and ac["ask"] == 2.1        # 原始报价照记，不伪造
        assert qs["atm_straddle_mid"] is None               # 一腿坏 → 跨式不给
        assert qs["implied_move_pct"] is None
        assert qs["contracts"]["atm_put"]["quote_ok"] is True

    def test_nan_bid_does_not_crash_and_is_not_ok(self):
        payload = _payload(EXP_28)
        for r in payload["options"]:
            if r["option"] == _occ(EXP_28, "P", 100.0):
                r["bid"] = float("nan")
                r["delta"] = float("nan")
        qs = select_quote_set(payload, 100.0, now=NOW)
        ap = qs["contracts"]["atm_put"]
        assert ap["quote_ok"] is False and ap["mid"] is None and ap["bid"] is None
        assert ap["delta"] is None
        assert qs["atm_straddle_mid"] is None
        # NaN delta 的行不能被选成 p25（它已经不是观测值了）
        assert qs["contracts"]["p25"]["strike"] != 100.0

    def test_ask_below_bid_is_not_ok(self):
        payload = _payload(EXP_28)
        for r in payload["options"]:
            if r["option"] == _occ(EXP_28, "C", 100.0):
                r["bid"], r["ask"] = 2.5, 2.0
        ac = select_quote_set(payload, 100.0, now=NOW)["contracts"]["atm_call"]
        assert ac["quote_ok"] is False and ac["mid"] is None

    def test_all_deltas_zero_gives_no_25d_but_keeps_atm(self):
        payload = _payload(EXP_28)
        for r in payload["options"]:
            r["delta"] = 0.0
        qs = select_quote_set(payload, 100.0, now=NOW)
        assert qs["data_available"] is True
        assert qs["contracts"]["c25"] is None and qs["contracts"]["p25"] is None
        assert qs["missing_reasons"]["c25"] == "no delta"
        assert qs["missing_reasons"]["p25"] == "no delta"
        assert qs["contracts"]["atm_call"]["strike"] == 100.0
        assert qs["contracts"]["atm_put"]["strike"] == 100.0
        assert qs["atm_straddle_mid"] == pytest.approx(4.0)

    def test_bad_underlying_price(self):
        for bad in (0.0, -1.0, float("nan"), None):
            qs = select_quote_set(_payload(EXP_28), bad, now=NOW)
            assert qs["data_available"] is False, bad


# ==================== fetch_cboe_quote_set 分支（payload 打桩，不联网） ====================

class TestFetchQuoteSet:
    def test_snapshot_mode_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(cboe_options, "_SNAPSHOT_PROVIDER", lambda t: {"price_at_fetch": 1})
        monkeypatch.setattr(cboe_options, "_fetch_cboe_payload",
                            lambda *a, **k: pytest.fail("快照模式不得拉 payload"))
        qs = cboe_options.fetch_cboe_quote_set("XYZ", 100.0)
        assert qs["data_available"] is False and "snapshot" in qs["error"]

    def test_payload_none_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(cboe_options, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(cboe_options, "_fetch_cboe_payload", lambda *a, **k: None)
        qs = cboe_options.fetch_cboe_quote_set("XYZ", 100.0)
        assert qs["data_available"] is False and "payload" in qs["error"]

    def test_caller_price_marks_source_caller(self, monkeypatch):
        monkeypatch.setattr(cboe_options, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(cboe_options, "_fetch_cboe_payload", lambda *a, **k: _payload(EXP_28))
        monkeypatch.setattr(cboe_options, "_pdt_now", lambda: NOW)
        qs = cboe_options.fetch_cboe_quote_set("XYZ", 100.0)
        assert qs["data_available"] is True
        assert qs["underlying_price_source"] == "caller"
        assert qs["selected_expiry"] == EXP_28

    def test_missing_price_falls_back_to_official_price(self, monkeypatch):
        monkeypatch.setattr(cboe_options, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(cboe_options, "_fetch_cboe_payload", lambda *a, **k: _payload(EXP_28, S=103.0))
        monkeypatch.setattr(cboe_options, "_pdt_now", lambda: NOW)
        monkeypatch.setattr(cboe_options, "official_price", lambda d, now_et=None: (103.0, "cboe_close"))
        qs = cboe_options.fetch_cboe_quote_set("XYZ", 0.0)
        assert qs["underlying_price_source"] == "cboe_close"
        assert qs["underlying_price"] == 103.0
        assert qs["contracts"]["atm_call"]["strike"] == 105.0

    def test_official_price_unavailable(self, monkeypatch):
        monkeypatch.setattr(cboe_options, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(cboe_options, "_fetch_cboe_payload", lambda *a, **k: _payload(EXP_28))
        monkeypatch.setattr(cboe_options, "official_price", lambda d, now_et=None: (0.0, "unavailable"))
        qs = cboe_options.fetch_cboe_quote_set("XYZ", float("nan"))
        assert qs["data_available"] is False and "price" in qs["error"]


# ==================== OptionsAgent.analyze 挂载 ====================

_CHAIN = {
    "calls": [
        {"strike": 140, "openInterest": 500, "impliedVolatility": 0.35, "gamma": 0.04},
        {"strike": 150, "openInterest": 800, "impliedVolatility": 0.30, "gamma": 0.05},
        {"strike": 160, "openInterest": 300, "impliedVolatility": 0.28, "gamma": 0.03},
    ],
    "puts": [
        {"strike": 130, "openInterest": 400, "impliedVolatility": 0.40, "gamma": 0.03},
        {"strike": 140, "openInterest": 600, "impliedVolatility": 0.38, "gamma": 0.04},
        {"strike": 150, "openInterest": 200, "impliedVolatility": 0.32, "gamma": 0.05},
    ],
    "expirations": ["2026-03-20", "2026-04-17"],
    "source": "real",
}


def _agent(monkeypatch, chain):
    monkeypatch.setenv("OPTIONS_SNAPSHOT_DISABLE", "1")  # 不写 cache/
    agent = OptionsAgent()
    monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda ticker: dict(chain))
    monkeypatch.setattr(agent.fetcher, "fetch_historical_hv",
                        lambda ticker: [0.25 + i * 0.02 for i in range(20)])
    monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda ticker, iv: None)
    monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda ticker: None)
    return agent


class TestAnalyzeMount:
    def test_cboe_chain_carries_quote_set(self, monkeypatch):
        canned = select_quote_set(_payload(EXP_28, S=145.0), 145.0, now=NOW)
        assert canned["data_available"] is True
        calls = []

        def fake(ticker, stock_price=0.0, **kw):
            calls.append((ticker, stock_price))
            return canned

        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set", fake)
        agent = _agent(monkeypatch, {**_CHAIN, "_source": "cboe"})
        result = agent.analyze("NVDA", stock_price=145.0)
        assert result["quote_set"]["data_available"] is True
        assert result["quote_set"]["contracts"]["atm_call"]["role"] == "atm_call"
        # v0.45.104：analyze 一律传 0.0，由 fetch_cboe_quote_set 自己取价
        assert calls == [("NVDA", 0.0)]
        # 既有字段一个不少（不改行为）
        for key in ("iv_rank", "put_call_ratio", "gamma_exposure", "options_score", "flow_direction"):
            assert key in result

    def test_non_cboe_chain_is_unavailable_without_fetching(self, monkeypatch):
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set",
                            lambda *a, **k: pytest.fail("非 CBOE 链不得去拉报价集"))
        agent = _agent(monkeypatch, _CHAIN)  # 没有 _source
        result = agent.analyze("NVDA", stock_price=145.0)
        qs = result["quote_set"]
        assert qs["data_available"] is False
        assert "cboe" in qs["error"].lower()

    def test_quote_set_exception_does_not_abort_analyze(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("cboe exploded")
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set", boom)
        agent = _agent(monkeypatch, {**_CHAIN, "_source": "cboe"})
        result = agent.analyze("NVDA", stock_price=145.0)
        assert result["quote_set"]["data_available"] is False
        assert "cboe exploded" in result["quote_set"]["error"]
        assert result["ticker"] == "NVDA"

    def test_sample_chain_early_return_has_key(self, monkeypatch):
        agent = _agent(monkeypatch, {**_CHAIN, "source": "sample", "expirations": []})
        result = agent.analyze("TEST", stock_price=145.0)
        assert result["data_quality"] == "unavailable"
        assert result["quote_set"]["data_available"] is False
        assert result["quote_set"]["source"] == "none"

    def test_nan_stock_price_does_not_crash_analyze(self, monkeypatch):
        """v0.45.104：此前这里断言「传下去的价 > 0」，而那个正数是行权价中位数。
        取价职责已整个交给 fetch_cboe_quote_set，见
        TestCallerPriceIsNotAStrikeMedian。这里只留「不炸」。"""
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set",
                            lambda t, sp=0.0, **k: {"data_available": False,
                                                    "source": "cboe", "error": "x"})
        agent = _agent(monkeypatch, {**_CHAIN, "_source": "cboe"})
        result = agent.analyze("NVDA", stock_price=float("nan"))
        assert result["ticker"] == "NVDA"
        assert result["quote_set"]["data_available"] is False


# ══════════════════════════════════════════════════════════════════════════════
# v0.45.104 二次复查修复
# ══════════════════════════════════════════════════════════════════════════════

def _chain_rows(expiry, strikes, call_deltas, put_deltas, S):
    """任意行权价/delta 的一条链（不受 _strip 固定 90..110 网格限制）。"""
    rows = []
    for k, cd, pd_ in zip(strikes, call_deltas, put_deltas):
        c_mid = max(S - k, 0) + 2.0
        p_mid = max(k - S, 0) + 2.0
        rows.append(_row(expiry, "C", k, bid=round(c_mid - 0.1, 2),
                         ask=round(c_mid + 0.1, 2), delta=cd))
        rows.append(_row(expiry, "P", k, bid=round(p_mid - 0.1, 2),
                         ask=round(p_mid + 0.1, 2), delta=pd_))
    return {"current_price": S, "close": S, "iv30": 0.31, "options": rows}


# ==================== #1 行权价中位数不得冒充股价 ====================

class TestCallerPriceIsNotAStrikeMedian:
    """analyze 出口必须传 0.0，让 fetch_cboe_quote_set 自己走 official_price。

    传 atm_price 会让 `_qs_num(stock_price)` 直接成立 → 绕过 official_price →
    underlying_price_source 谎报 "caller"，而 atm_price 在 stock_price 缺失时
    根本不是价格（是 median(all_strikes)，甚至字面量 145.0）。
    """

    @pytest.mark.parametrize("bad_price", [float("nan"), 0.0, None])
    def test_analyze_passes_zero_not_atm_price(self, monkeypatch, bad_price):
        seen = {}

        def fake(ticker, stock_price=0.0, **kw):
            seen["S"] = stock_price
            return {"data_available": False, "source": "cboe", "error": "x",
                    "contracts": {r: None for r in cboe_options._QS_ROLES}}

        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set", fake)
        agent = _agent(monkeypatch, {**_CHAIN, "_source": "cboe"})
        agent.analyze("NVDA", stock_price=bad_price)
        # 修复前：150.0（_CHAIN 里 OI>100 的行权价 140/150/160 的中位数）
        assert seen["S"] == 0.0

    def test_cboe_resolves_price_and_labels_source_honestly(self, monkeypatch):
        """端到端：链上真实标的 145，_CHAIN 的中位行权价 150。

        修复前 → underlying_price 150.0 / source "caller" / ATM 150（把行权价
        中位数写成了股价）；修复后 → 145.0 / "cboe_intraday" / ATM 145。
        """
        payload = _chain_rows(EXP_28, [135, 140, 145, 150, 155],
                              [0.85, 0.68, 0.50, 0.30, 0.14],
                              [-0.14, -0.30, -0.50, -0.70, -0.86], S=145.0)
        monkeypatch.setattr(cboe_options, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(cboe_options, "_fetch_cboe_payload", lambda *a, **k: payload)
        monkeypatch.setattr(cboe_options, "_pdt_now", lambda: NOW)
        monkeypatch.setattr(cboe_options, "official_price",
                            lambda d, now_et=None: (145.0, "cboe_intraday"))
        agent = _agent(monkeypatch, {**_CHAIN, "_source": "cboe"})
        qs = agent.analyze("AMCX", stock_price=float("nan"))["quote_set"]

        assert qs["data_available"] is True
        assert qs["underlying_price_source"] == "cboe_intraday"
        assert qs["underlying_price"] == 145.0
        assert qs["contracts"]["atm_call"]["strike"] == 145.0


# ==================== #3 25Δ 必须真的接近 25Δ ====================

class TestTwentyFiveDeltaGuards:

    def _sparse(self, cd, pd_, S=100.0):
        return _chain_rows(EXP_28, [100], [cd], [pd_], S=S)

    def test_far_delta_is_rejected_not_relabeled(self):
        """稀疏链上 Δ=0.60 / −0.55 曾被贴成 c25/p25、quote_ok=True、无 reason。"""
        qs = select_quote_set(self._sparse(0.60, -0.55), 100.0, now=NOW)
        assert qs["data_available"] is True
        assert qs["contracts"]["c25"] is None
        assert qs["contracts"]["p25"] is None
        assert "tol" in qs["missing_reasons"]["c25"]
        assert "0.6000" in qs["missing_reasons"]["c25"]
        assert "tol" in qs["missing_reasons"]["p25"]
        # ATM 不受影响：它本来就该选中这张
        assert qs["contracts"]["atm_call"]["strike"] == 100.0

    def _wing(self, d):
        """3 个行权价，S=100 → ATM=100；翼上（90/110）的 delta 由参数给。
        翼与 ATM 不同张，所以只有距离闸在起作用。"""
        return _chain_rows(EXP_28, [90, 100, 110], [0.90, 0.55, d],
                           [-d, -0.45, -0.90], S=100.0)

    def test_edge_of_tolerance_is_accepted(self):
        """恰好 |Δ−0.25| = tol 仍收，超一点点才拒——闸门不许悄悄收紧。"""
        tol = cboe_options._QS_D25_TOL
        ok = select_quote_set(self._wing(0.25 + tol), 100.0, now=NOW)
        assert ok["contracts"]["c25"] is not None and ok["contracts"]["c25"]["strike"] == 110.0
        assert ok["contracts"]["p25"] is not None and ok["contracts"]["p25"]["strike"] == 90.0
        bad = select_quote_set(self._wing(0.25 + tol + 0.01), 100.0, now=NOW)
        assert bad["contracts"]["c25"] is None and bad["contracts"]["p25"] is None

    def test_25d_may_not_be_the_same_contract_as_atm(self):
        """ATM 那张恰好 Δ≈0.25 时，c25 槽位不得复用它——同一张合约不是两个观测点。"""
        payload = _chain_rows(EXP_28, [100, 130], [0.25, 0.02], [-0.25, -0.98], S=100.0)
        qs = select_quote_set(payload, 100.0, now=NOW)
        assert qs["contracts"]["atm_call"]["strike"] == 100.0
        assert qs["contracts"]["c25"] is None
        assert qs["missing_reasons"]["c25"] == "same contract as atm_call"
        assert qs["contracts"]["p25"] is None
        assert qs["missing_reasons"]["p25"] == "same contract as atm_put"


# ==================== #4 补上两个此前存活的变异 ====================

class TestSurvivingMutations:

    def test_duplicate_rows_pick_the_max_oi_one(self):
        """`_qs_pick_row` 取 OI **最大**那行。改成 min 时此测试必须变红。"""
        payload = _payload(EXP_28)
        atm_call = _occ(EXP_28, "C", 100.0)
        for r in payload["options"]:
            if r["option"] == atm_call:
                r["open_interest"] = 50.0        # 冷门重复行：报价 1.9/2.1
        payload["options"].append(_row(EXP_28, "C", 100.0, bid=3.9, ask=4.1,
                                       delta=0.50, oi=9000.0))
        ac = select_quote_set(payload, 100.0, now=NOW)["contracts"]["atm_call"]
        assert ac["oi"] == 9000.0
        assert ac["bid"] == 3.9 and ac["mid"] == 4.0   # 取的确实是那一行的报价

    def test_atm_prefers_a_strike_with_both_sides(self):
        """`pool = both or (calls|puts)`：两边都有的行权价优先，哪怕它离 S 更远。

        去掉 `both or` 后 ATM 会落到只有 call 的 99（离 S 更近），atm_put 变 None。
        """
        rows = _chain_rows(EXP_28, [99, 105], [0.55, 0.30], [-0.45, -0.70], S=100.0)
        # 把 99 的 put 摘掉：99 只剩 call，105 两边都在
        rows["options"] = [r for r in rows["options"]
                           if r["option"] != _occ(EXP_28, "P", 99.0)]
        qs = select_quote_set(rows, 100.0, now=NOW)
        assert qs["contracts"]["atm_call"]["strike"] == 105.0
        assert qs["contracts"]["atm_put"] is not None
        assert qs["contracts"]["atm_put"]["strike"] == 105.0
        assert qs["atm_straddle_mid"] is not None


# ==================== #5 注入的 now 是唯一时钟 ====================

class TestInjectedNowIsTheOnlyClock:

    def test_injected_now_drives_market_open_and_fetched_at(self):
        inj = datetime(2026, 3, 10, 10, 0)          # 周二 10:00 → 盘中
        payload = _payload("2026-04-09")            # 相对 inj 恰好 30 DTE
        qs = select_quote_set(payload, 100.0, now=inj)
        assert qs["selected_dte"] == 30
        assert qs["fetched_at"] == inj.isoformat()  # 修复前是挂钟的「今天」
        assert qs["market_open"] is True

    def test_injected_weekend_now_is_not_market_open(self):
        inj = datetime(2026, 3, 7, 10, 0)           # 周六
        qs = select_quote_set(_payload("2026-04-09"), 100.0, now=inj)
        assert qs["market_open"] is False
        assert qs["fetched_at"] == inj.isoformat()

    def test_no_injected_now_still_uses_wall_clock(self, monkeypatch):
        """不注入时行为一字不变——真实抓取时挂钟就是抓取时刻。"""
        monkeypatch.setattr(cboe_options, "_pdt_now", lambda: NOW)
        monkeypatch.setattr(cboe_options, "_et_now",
                            lambda: datetime(2026, 9, 3, 14, 25, 14))
        monkeypatch.setattr(cboe_options, "is_market_open", lambda *a: True)
        qs = select_quote_set(_payload(EXP_28), 100.0)
        assert qs["fetched_at"] == "2026-09-03T14:25:14"
        assert qs["market_open"] is True


# ==================== #6 空的 quote_set 不该冻一整天 ====================

class TestRefillEmptyQuoteSet:

    def _agent_only(self, monkeypatch):
        monkeypatch.setenv("OPTIONS_SNAPSHOT_DISABLE", "1")
        return OptionsAgent()

    def test_empty_quote_set_is_refilled(self, monkeypatch):
        fresh = select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        assert fresh["data_available"] is True
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set",
                            lambda t, sp=0.0, **k: fresh)
        agent = self._agent_only(monkeypatch)
        cached = {"ticker": "NVDA",
                  "quote_set": {"data_available": False, "source": "cboe",
                                "error": "cboe payload unavailable"}}
        assert agent._refill_empty_quote_set(cached, "NVDA") is True
        assert cached["quote_set"]["data_available"] is True
        assert cached["_quote_set_refilled_at"]

    def test_captured_quote_set_is_never_refetched(self, monkeypatch):
        """v0.45.43 规则原样保留：抓到手的报价集不许重取（接口没有历史）。"""
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set",
                            lambda *a, **k: pytest.fail("已捕获的 quote_set 不得重取"))
        agent = self._agent_only(monkeypatch)
        captured = select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        cached = {"ticker": "NVDA", "quote_set": captured}
        assert agent._refill_empty_quote_set(cached, "NVDA") is False
        assert cached["quote_set"] is captured
        assert "_quote_set_refilled_at" not in cached

    def test_refill_failure_keeps_the_empty_one(self, monkeypatch):
        empty = {"data_available": False, "source": "cboe", "error": "cboe payload unavailable"}
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set",
                            lambda *a, **k: {"data_available": False, "error": "still down"})
        agent = self._agent_only(monkeypatch)
        cached = {"quote_set": dict(empty)}
        assert agent._refill_empty_quote_set(cached, "NVDA") is False
        assert cached["quote_set"]["error"] == "cboe payload unavailable"
        assert "_quote_set_refilled_at" not in cached

    def test_refill_writes_back_to_snapshot(self, monkeypatch, tmp_path):
        import json as _json
        fresh = select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set",
                            lambda *a, **k: fresh)
        agent = self._agent_only(monkeypatch)
        snap = tmp_path / "snap.json"
        cached = {"quote_set": {"data_available": False, "source": "cboe", "error": "x"}}
        snap.write_text(_json.dumps(cached))
        assert agent._refill_empty_quote_set(cached, "NVDA", str(snap)) is True
        on_disk = _json.loads(snap.read_text())
        assert on_disk["quote_set"]["data_available"] is True
        assert "_quote_set_refilled_at" in on_disk

    # ── 挂载点：光有方法不算数，必须真的挂在快照命中路径上 ──
    def _snap_agent(self, monkeypatch, tmp_path, quote_set):
        """写一份"今天"的快照，返回 (agent, ticker, 快照路径)。"""
        import json as _json
        from options_analyzer import pdt_today
        monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
        monkeypatch.delenv("ALPHA_HIVE_TARGET_DATE", raising=False)
        today = pdt_today()
        agent = OptionsAgent()
        monkeypatch.setattr(agent.fetcher, "cache_dir", str(tmp_path))
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain",
                            lambda t: pytest.fail("快照命中就不该再拉链"))
        snap = tmp_path / f"options_snapshot_ZZZ_{today}.json"
        snap.write_text(_json.dumps({
            "ticker": "ZZZ", "_snapshot_timestamp": f"{today}T14:00:00",
            "rv_30d": 1.0, "iv_rank": 50.0, "iv_rank_source": "real_iv_90d",
            "quote_set": quote_set}))
        return agent, str(snap)

    def test_analyze_refills_an_empty_quote_set_on_snapshot_hit(self, monkeypatch, tmp_path):
        """挂载点回归：#6 的方法必须被 analyze 的快照命中分支真正调用。"""
        fresh = select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set",
                            lambda t, sp=0.0, **k: fresh)
        agent, _ = self._snap_agent(monkeypatch, tmp_path,
                                    {"data_available": False, "source": "cboe",
                                     "error": "cboe payload unavailable"})
        out = agent.analyze("ZZZ")
        assert out["quote_set"]["data_available"] is True
        assert out["_quote_set_refilled_at"]

    def test_analyze_does_not_refetch_a_captured_quote_set(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set",
                            lambda *a, **k: pytest.fail("已捕获的 quote_set 不得重取"))
        captured = select_quote_set(_payload(EXP_28), 100.0, now=NOW)
        agent, _ = self._snap_agent(monkeypatch, tmp_path, captured)
        out = agent.analyze("ZZZ")
        assert out["quote_set"]["selected_expiry"] == EXP_28
        assert "_quote_set_refilled_at" not in out

# ==================== #11 四个生产者形状一致 ====================

class TestUnavailableShapeIsUniform:

    def test_all_four_producers_emit_the_full_key_set(self, monkeypatch):
        expected = set(cboe_options._quote_set_unavailable("ref"))

        # ① fetch_cboe_quote_set 自己（本来就守约）
        monkeypatch.setattr(cboe_options, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(cboe_options, "_fetch_cboe_payload", lambda *a, **k: None)
        p1 = cboe_options.fetch_cboe_quote_set("XYZ", 100.0)

        # ② 样本链早退
        agent2 = _agent(monkeypatch, {**_CHAIN, "source": "sample", "expirations": []})
        p2 = agent2.analyze("TEST", stock_price=145.0)["quote_set"]

        # ③ 报价集抛异常
        def boom(*a, **k):
            raise RuntimeError("cboe exploded")
        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set", boom)
        agent3 = _agent(monkeypatch, {**_CHAIN, "_source": "cboe"})
        p3 = agent3.analyze("NVDA", stock_price=145.0)["quote_set"]

        # ④ 非 CBOE 链
        agent4 = _agent(monkeypatch, _CHAIN)
        p4 = agent4.analyze("NVDA", stock_price=145.0)["quote_set"]

        for i, qs in enumerate((p1, p2, p3, p4), 1):
            assert set(qs) == expected, f"生产者 {i} 形状不一致"
            # docstring 承诺：下游可以无脑取 contracts 四键
            assert set(qs["contracts"]) == set(cboe_options._QS_ROLES), i
            assert qs["data_available"] is False, i
