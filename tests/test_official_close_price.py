"""收盘后必须取官方收盘价，不许取盘后价（v0.45.46）

CBOE payload 同时给两个字段，含义完全不同：
    current_price —— 最近一笔成交。收盘后它是**盘后价**（延长时段到 20:00 ET）
    close         —— 该交易日的**官方收盘价**

全部定时扫描在 14:00 PDT（= 17:00 ET）跑，即收盘之后、盘后时段之内。
旧代码三处（cboe_options / data_pipeline / cloud_snapshot_fetch）一律
`current_price or close`，于是记录的一直是盘后价。

2026-08-26 CBOE 实拉，对照 yfinance 官方收盘：
    CRM   current_price=232.3187  close=205.62  ← 当日财报，盘后 +12.98%
    NVDA  current_price=219.53    close=209.66  ← 盘后 +4.71%
    MSFT  current_price=495.94    close=496.37  ← 盘后 −0.09%
三只的 close 与官方收盘逐分不差。

为什么这不是小数点问题：`price_at_predict` 是所有收益计算的**入场价**。
用盘后价当入场价 = 假设能在财报公布后以盘后价成交，收益全错。
"""

from datetime import datetime, time as dtime

import pytest

from cboe_options import is_market_open, official_price

# 2026-08-26 实拉的真实 payload 片段
CRM = {"current_price": 232.3187, "close": 205.62, "prev_day_close": 205.62}
NVDA = {"current_price": 219.53, "close": 209.66, "prev_day_close": 209.66}
MSFT = {"current_price": 495.94, "close": 496.37, "prev_day_close": 496.37}

OPEN_ET = datetime(2026, 8, 26, 11, 0)    # 周三 11:00 ET —— 盘中
AFTER_ET = datetime(2026, 8, 26, 17, 10)  # 周三 17:10 ET —— 收盘后（扫描实际时刻）
PRE_ET = datetime(2026, 8, 26, 8, 0)      # 周三 08:00 ET —— 盘前
SAT_ET = datetime(2026, 8, 29, 12, 0)     # 周六


class TestMarketSession:
    @pytest.mark.parametrize("et,expect", [
        (OPEN_ET, True), (AFTER_ET, False), (PRE_ET, False), (SAT_ET, False),
        (datetime(2026, 8, 26, 9, 30), True),    # 开盘瞬间算盘中
        (datetime(2026, 8, 26, 16, 0), False),   # 收盘瞬间算收盘后
    ])
    def test_session_boundaries(self, et, expect):
        assert is_market_open(et) is expect


class TestAfterHoursNeverUsed:
    @pytest.mark.parametrize("payload,close", [(CRM, 205.62), (NVDA, 209.66), (MSFT, 496.37)])
    def test_after_close_returns_official_close(self, payload, close):
        px, src = official_price(payload, AFTER_ET)
        assert px == close
        assert src == "cboe_close"

    def test_earnings_day_after_hours_rejected(self):
        """CRM 财报日：盘后 +12.98%。取到它 = 假设能在财报后以盘后价入场"""
        px, _ = official_price(CRM, AFTER_ET)
        assert px == 205.62
        assert px != CRM["current_price"], "绝不能取盘后价"

    def test_no_fallback_to_current_price_when_close_missing(self):
        """close 缺失时**不许**回退到 current_price —— 回退等于把 bug 放回来"""
        px, src = official_price({"current_price": 232.3187}, AFTER_ET)
        assert px == 0.0
        assert src == "unavailable"

    @pytest.mark.parametrize("et", [PRE_ET, SAT_ET])
    def test_premarket_and_weekend_also_use_close(self, et):
        px, src = official_price(CRM, et)
        assert px == 205.62 and src == "cboe_close"


class TestIntradayUsesLive:
    def test_market_open_uses_current_price(self):
        """盘中实时成交价才是此刻的真实价格"""
        px, src = official_price(CRM, OPEN_ET)
        assert px == 232.3187
        assert src == "cboe_intraday"

    def test_intraday_falls_back_to_close_if_no_current(self):
        px, src = official_price({"close": 205.62}, OPEN_ET)
        assert px == 205.62 and src == "cboe_intraday"


class TestBadInput:
    @pytest.mark.parametrize("payload", [{}, None, {"close": 0}, {"close": -5},
                                         {"close": "n/a"}, {"close": None}])
    def test_unusable_returns_zero_unavailable(self, payload):
        px, src = official_price(payload, AFTER_ET)
        assert px == 0.0 and src == "unavailable"


class TestCallSitesWired:
    """静态闸：三个取价点都不许再出现旧的 `current_price or close` 模式"""

    @pytest.mark.parametrize("path", [
        "cboe_options.py", "data_pipeline.py", "cloud_snapshot_fetch.py",
    ])
    def test_no_raw_current_price_fallback(self, path):
        from pathlib import Path
        src = Path(path).read_text()
        bad = 'get("current_price") or payload.get("close")'
        bad2 = 'data.get("current_price") or data.get("close")'
        assert bad not in src and bad2 not in src, \
            f"{path} 仍在收盘后取 current_price（盘后价）"
        assert "official_price" in src, f"{path} 未接入 official_price"


class TestNonFiniteRejected:
    """二次检查发现：`inf > 0` 为真，只判正数会让 inf 当成合法价格漏过去。
    NaN 恰好被 `> 0` 挡住（NaN 的任何比较都是 False），inf 不会 —— 两者必须都挡。
    Python 的 json.loads 默认接受 `Infinity` 字面量，所以这不是纯理论问题。"""

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_close_rejected(self, bad):
        px, src = official_price({"close": bad}, AFTER_ET)
        assert px == 0.0 and src == "unavailable", f"{bad} 不该被当成合法价格"

    @pytest.mark.parametrize("bad", [float("inf"), float("nan")])
    def test_non_finite_current_price_rejected_intraday(self, bad):
        px, src = official_price({"current_price": bad, "close": 205.62}, OPEN_ET)
        assert px == 205.62, "盘中 current_price 非有限时应回退到 close"

    def test_string_number_still_works(self):
        assert official_price({"close": "205.62"}, AFTER_ET) == (205.62, "cboe_close")

    @pytest.mark.parametrize("payload", [[], "x", 42, {"close": []}])
    def test_malformed_payload_safe(self, payload):
        assert official_price(payload, AFTER_ET) == (0.0, "unavailable")
