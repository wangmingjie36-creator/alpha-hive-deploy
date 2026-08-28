"""市场政体取数必须校验返回的是哪只标的（v0.45.52）。

背景（实测，非推测）
--------------------
2026-08-26 那次扫描 yfinance 限流 487 次。`market_intelligence` 的 `_get_ma`
只用 `hist.empty` 与 `len(hist) < period` 做守卫 —— 一份完整的**别家**数据
两条都过。板块层 `_get_ma("SOXX", 20, 40)` 与个股层 `_get_ma(ticker, 20, 40)`
用的是同一个 period 字符串（"60d"），于是：

    NVDA / MSFT / TSLA / VKTX 的个股 20MA 全是 $528  ← SOXX 真值 529
    NVDA 自己的 20MA 实为 215.56

个股金叉/死叉判断因此建立在半导体 ETF 的均线上，而这个判断进入
`regime_score_adj` 并被写进日报与网站。

守什么
------
返回的 DataFrame 里的 ticker 必须是请求的那只；不是就返回 `(nan, nan)`，
由调用方走「数据不可用」。**诚实缺失好过安静地用别人的数据。**
"""

import math
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _frame(ticker, n=80, value=100.0):
    import pandas as pd
    idx = pd.bdate_range("2026-04-01", periods=n)
    cols = pd.MultiIndex.from_product([["Close", "High", "Low", "Open"], [ticker]])
    import numpy as np
    data = np.tile(np.full((n, 1), value), (1, 4))
    return pd.DataFrame(data, index=idx, columns=cols)


def _get_ma_of(monkeypatch, requested, returned_frame):
    """把 yf.download 打桩成「无论请求谁都返回 returned_frame」，
    再走真实的 detect_market_regime，取个股那段的结论。"""
    import market_intelligence as mi
    monkeypatch.setitem(sys.modules, "yfinance",
                        types.SimpleNamespace(download=lambda *a, **k: returned_frame))
    return mi.detect_market_regime(requested)


def test_foreign_ticker_frame_is_rejected(monkeypatch):
    """回归：请求 NVDA 却拿到 SOXX 的帧 → 必须判为数据不可用，不许拿来算均线。"""
    res = _get_ma_of(monkeypatch, "NVDA", _frame("SOXX", value=529.0))
    assert res["stock_regime"] == "neutral"
    assert "不可用" in res["stock_detail"], \
        f"用了别家数据：{res['stock_detail']}"
    assert "529" not in res["stock_detail"] and "$528" not in res["stock_detail"]


def test_own_ticker_frame_is_accepted(monkeypatch):
    """正路不误伤：请求 NVDA 拿到 NVDA 的帧，照常算。"""
    res = _get_ma_of(monkeypatch, "NVDA", _frame("NVDA", value=215.0))
    assert "不可用" not in res["stock_detail"]
    assert "NVDA" in res["stock_detail"]


def test_case_insensitive_symbol_match(monkeypatch):
    """大小写不该导致误杀。"""
    res = _get_ma_of(monkeypatch, "nvda", _frame("NVDA", value=215.0))
    assert "不可用" not in res["stock_detail"]


def test_empty_frame_still_unavailable(monkeypatch):
    import pandas as pd
    res = _get_ma_of(monkeypatch, "NVDA", pd.DataFrame())
    assert res["stock_regime"] == "neutral"
    assert "不可用" in res["stock_detail"] or "数据不可用" in res["stock_detail"]
