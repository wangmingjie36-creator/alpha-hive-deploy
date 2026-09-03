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
        assert isinstance(qs["market_open"], bool)
        assert qs["fetched_at"]

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
        assert calls == [("NVDA", 145.0)]
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

    def test_nan_stock_price_uses_atm_price(self, monkeypatch):
        seen = {}

        def fake(ticker, stock_price=0.0, **kw):
            seen["S"] = stock_price
            return {"data_available": False, "source": "cboe", "error": "x"}

        monkeypatch.setattr(cboe_options, "fetch_cboe_quote_set", fake)
        agent = _agent(monkeypatch, {**_CHAIN, "_source": "cboe"})
        agent.analyze("NVDA", stock_price=float("nan"))
        assert math.isfinite(seen["S"]) and seen["S"] > 0
