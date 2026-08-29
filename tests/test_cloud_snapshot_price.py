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
    """把 yfinance 换成可控桩，避免测试联网。

    ⚠️ v0.45.72 修：原写法是 `monkeypatch.setattr(dp, "yf", ..., raising=False)`,
    而 `data_pipeline` 里**没有**模块级的 `yf` —— 四处 `import yfinance as yf`
    全是函数内局部导入，局部名把模块属性整个盖住。于是这个桩谁也没读到，
    五条用例一路打真外网；`raising=False` 又恰好把「属性不存在」这唯一的
    报警吞了。改为 patch `yfinance.Ticker` 本身：函数里 `yf.Ticker(...)` 是
    调用时在真模块上查属性，这样才盖得住局部 import。

    症状会漂：2026-08-29 写这些用例时 yfinance 的 8/28 行 Close 恰好是 NaN，
    真网返回的形状与桩碰巧一致，全绿；等 yfinance 回填了 8/28 的真实收盘，
    同一份代码就在 8/29 之后变红——**红的不是生产代码，是这条从没生效过的桩**。
    （`test_normal_day_unaffected` 更彻底：它拿真实行情断言真实行情，
    桩失效也照样绿。）
    """
    holder = {"calls": 0}

    class _T:
        def __init__(self, ticker): pass

        def history(self, **kw):
            holder["calls"] += 1
            return holder["hist"]

    monkeypatch.setattr("yfinance.Ticker", _T)
    yield holder
    # 桩没被调用 = 打了真网 = 这条用例测的不是它以为在测的东西。
    # 这道断言就是上面那半年没人发现的漏洞的看门人。
    assert holder["calls"] > 0, "yfinance 桩一次都没被调用 —— 用例多半打了真外网"


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
