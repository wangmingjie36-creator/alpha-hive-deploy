"""云端快照价接进补跑取价链（v0.45.70）。

2026-08-29 补跑 8/28 时的实况：yfinance 有 8/28 那一行，但 `Close=NaN`
（`Volume=1.94 亿`，当天确实交易了）。于是：
  · 不处理 NaN  → price=NaN，穿过四道 `<= 0` 守卫（v0.45.69 已修）
  · 只 dropna   → 末行退回 8/27，**用前一交易日冒充目标日**（差 4.8%）
  · 本版        → 拿云端快照的 price_at_fetch（目标日收盘后从 CBOE 抓的）
"""
import datetime as dt
import pandas as pd
import pytest

import data_pipeline as dp


def _hist(rows):
    idx = pd.to_datetime([d for d, _, _ in rows])
    return pd.DataFrame(
        {"Close": [c for _, c, _ in rows], "Volume": [v for _, _, v in rows]},
        index=idx,
    )


@pytest.fixture
def _yf(monkeypatch):
    """把 yfinance 换成可控桩，避免测试联网。"""
    holder = {}

    class _T:
        def __init__(self, ticker): pass
        def history(self, **kw): return holder["hist"]

    monkeypatch.setattr(dp, "yf", type("Y", (), {"Ticker": _T}), raising=False)
    return holder


def test_uses_cloud_snapshot_when_target_day_close_is_nan(_yf, monkeypatch):
    """目标日 Close=NaN → 用云端快照价，且**标明来源**。"""
    _yf["hist"] = _hist([("2026-08-26", 209.66, 1.7e8),
                         ("2026-08-27", 227.98, 2.9e8),
                         ("2026-08-28", float("nan"), 1.9e8)])
    monkeypatch.setitem(
        __import__("sys").modules, "cloud_snapshot_loader",
        type("M", (), {"load_ticker": staticmethod(
            lambda d, t, **k: {"price_at_fetch": 217.55, "price_source": "cboe_close"})}))

    r = dp._fetch_historical_stock_data("NVDA", "2026-08-28")
    assert r["price"] == pytest.approx(217.55)
    assert "cloud_snapshot" in str(r.get("source_name"))
    assert r.get("_price_from_cloud_snapshot") is True
    # ⚠️ 绝不能是 227.98 —— 那是前一交易日，正是 8/24 被隔离的口径污染
    assert r["price"] != pytest.approx(227.98)


def test_no_snapshot_means_unavailable_not_previous_day(_yf, monkeypatch):
    """快照也没有时必须标不可用，**不许拿前一日冒充**。"""
    _yf["hist"] = _hist([("2026-08-27", 227.98, 2.9e8),
                         ("2026-08-28", float("nan"), 1.9e8)])
    monkeypatch.setitem(
        __import__("sys").modules, "cloud_snapshot_loader",
        type("M", (), {"load_ticker": staticmethod(lambda d, t, **k: None)}))

    r = dp._fetch_historical_stock_data("NVDA", "2026-08-28")
    assert r.get("_data_unavailable") is True
    assert r.get("_reason") == "no_close_on_2026-08-28"
    assert not r.get("price")


def test_normal_day_unaffected(_yf, monkeypatch):
    """目标日有真实收盘时走原路径 —— 不得因本版改动而改变。"""
    _yf["hist"] = _hist([("2026-08-26", 209.66, 1.7e8),
                         ("2026-08-27", 227.98, 2.9e8)])
    called = []
    monkeypatch.setitem(
        __import__("sys").modules, "cloud_snapshot_loader",
        type("M", (), {"load_ticker": staticmethod(
            lambda d, t, **k: called.append(1) or {"price_at_fetch": 1.0})}))

    r = dp._fetch_historical_stock_data("NVDA", "2026-08-27")
    assert r["price"] == pytest.approx(227.98)
    assert r.get("_price_from_cloud_snapshot") is None
    assert not called, "有真实收盘却去问了云端快照 —— 白白多一次 git show"


def test_snapshot_price_must_be_finite_and_positive(_yf, monkeypatch):
    """快照里的价格本身也要校验，不能照单全收。"""
    _yf["hist"] = _hist([("2026-08-27", 227.98, 2.9e8),
                         ("2026-08-28", float("nan"), 1.9e8)])
    for bad in (float("nan"), 0, -5, "217.55", None):
        monkeypatch.setitem(
            __import__("sys").modules, "cloud_snapshot_loader",
            type("M", (), {"load_ticker": staticmethod(
                lambda d, t, _b=bad, **k: {"price_at_fetch": _b})}))
        r = dp._fetch_historical_stock_data("NVDA", "2026-08-28")
        assert r.get("_data_unavailable") is True, f"快照价 {bad!r} 被当成了有效价"


def test_volume_ratio_withheld_on_snapshot_path(_yf, monkeypatch):
    """volume_ratio 要拿**目标日**成交量比均量，而这条路径下目标日的量没取到。

    用前一日的量冒充会静默失真 —— 宁可不给。
    """
    _yf["hist"] = _hist([("2026-08-2%d" % d, 200.0 + d, 1e8) for d in range(1, 8)]
                        + [("2026-08-28", float("nan"), 1.9e8)])
    monkeypatch.setitem(
        __import__("sys").modules, "cloud_snapshot_loader",
        type("M", (), {"load_ticker": staticmethod(
            lambda d, t, **k: {"price_at_fetch": 217.55, "price_source": "cboe_close"})}))

    r = dp._fetch_historical_stock_data("NVDA", "2026-08-28")
    assert r.get("volume_ratio") is None
    assert r.get("momentum_source") == "5d_snapshot_spliced"
