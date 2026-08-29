"""
v0.41.6 回归测试：`--date` 补跑历史交易日时，股价必须锚定该日期的真实收盘价，
而不是脚本运行那一刻的实时报价。

事故：7/21 的报告多次重新生成，NVDA 现价先后显示 $206.34 / $205.10——两个
都不是 7/21 的真实收盘价 $207.29，是不同重跑时刻各自的实时报价。CBOE/
AlphaVantage/Finnhub 全是当前实时报价源，没有免费的历史快照能力，只能靠
yfinance 的 start=/end= 历史区间实现日期锚定。

v0.45.72 ⚠️ fixture 必须从「被测日期」倒推，不许写死起点。
原写法是 `date_range("2026-06-22", periods=26)` 配 `as_of_date="2026-07-21"`
——两个日期各写各的，中间隔着 4 天的缺口，谁也没校验它们对得上。
v0.45.69/0.45.70 给 `_fetch_historical_stock_data` 加了「末行不是目标日就拒绝、
**不以前一交易日冒充**」的闸之后，这个一直存在的缺口才第一次被看见：
生产行为是对的，是 fixture 从来没锚定过它自己要测的那一天。
与 v0.45.64「单测里的定时炸弹」同形——日期能推就别钉。
"""

from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from data_pipeline import fetch_stock_data, _fetch_historical_stock_data


# 被测日期：2026-07-21，就是事故当天（NVDA 真实收盘 $207.29）。
# 下面所有假日线都从它倒推，改这一个常量即可整体平移。
AS_OF = "2026-07-21"
# 「as_of 之后」的一天，用来喂那种 yfinance 多吐了未来行的情形。
DAY_AFTER_AS_OF = (date.fromisoformat(AS_OF) + timedelta(days=1)).isoformat()


def _fake_history(prices, last_date=AS_OF):
    """构造假日线，**最后一根 K 线正好落在 last_date 当天**，收盘价 = prices[-1]。

    用 `end=` 倒推而不是 `start=` 正推：periods 或被测日期怎么改，锚点都不会
    松脱。freq 保持 "D"（含周末）也是为了这个——`freq="B"` 遇到 end 落在周末
    时会静默往前回滚，那正是本文件要根治的那种漂移。
    """
    idx = pd.date_range(end=last_date, periods=len(prices), freq="D")
    assert idx[-1].date() == date.fromisoformat(last_date), (
        f"fixture 锚点脱靶：末行 {idx[-1].date()} != 目标日 {last_date}"
    )
    return pd.DataFrame({
        "Close": prices,
        "Volume": [1_000_000] * len(prices),
    }, index=idx)


class TestAsOfDateBypassesLiveSources:
    def test_as_of_date_in_past_skips_live_fetcher(self, monkeypatch):
        """as_of_date 是过去日期时，不应调用 get_fetcher()（CBOE/实时报价链）"""
        called = {"live": False}

        def _fake_get_fetcher():
            called["live"] = True
            raise AssertionError("不应该走实时报价链")

        monkeypatch.setattr("data_pipeline.get_fetcher", _fake_get_fetcher)
        monkeypatch.setattr("hive_logger.pdt_today", lambda: "2099-01-01")

        hist = _fake_history([100.0] * 25 + [207.29])
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist
            result = fetch_stock_data("NVDA", as_of_date=AS_OF)

        assert called["live"] is False
        assert result["price"] == pytest.approx(207.29)
        assert result["source_name"] == "yfinance_historical"

    def test_as_of_date_equal_to_today_uses_live_fetcher(self, monkeypatch):
        """as_of_date 等于今天时，行为与不传 as_of_date 完全一致（走实时链）"""
        monkeypatch.setattr("hive_logger.pdt_today", lambda: AS_OF)
        called = {"live": False}

        class _FakeFetcher:
            def fetch(self, ticker):
                called["live"] = True
                return {"price": 999.0, "source_name": "cboe"}

        monkeypatch.setattr("data_pipeline.get_fetcher", lambda: _FakeFetcher())
        result = fetch_stock_data("NVDA", as_of_date=AS_OF)

        assert called["live"] is True
        assert result["price"] == 999.0

    def test_no_as_of_date_uses_live_fetcher(self, monkeypatch):
        """不传 as_of_date（默认 None）时行为不变——当日实时扫描不受影响"""
        called = {"live": False}

        class _FakeFetcher:
            def fetch(self, ticker):
                called["live"] = True
                return {"price": 999.0, "source_name": "cboe"}

        monkeypatch.setattr("data_pipeline.get_fetcher", lambda: _FakeFetcher())
        result = fetch_stock_data("NVDA")

        assert called["live"] is True
        assert result["price"] == 999.0


class TestFetchHistoricalStockData:
    def test_picks_close_on_as_of_date_not_later(self):
        """必须取 as_of_date 当天的收盘价，即使 yfinance 多返回了之后的行。

        v0.45.72：原写法把未来那行**在喂进去之前就自己滤掉了**，等于只测了
        「末行就是目标日」这一种输入，与用例名承诺的「即使多返回了之后的行」
        相反。现在整帧原样喂进去，由生产代码里的 `<= as_of` 那道过滤去干活。
        """
        # 末行落在 as_of 的**后一天**（999.0 = 不该被取到的未来价），
        # 于是 as_of 当天正好是倒数第二行（207.29）。
        hist = _fake_history([100.0] * 24 + [207.29, 999.0],
                             last_date=DAY_AFTER_AS_OF)

        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist
            result = _fetch_historical_stock_data("NVDA", AS_OF)

        assert result["price"] == pytest.approx(207.29)
        assert result["data_source"] == "real"

    def test_empty_history_returns_fallback(self):
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame()
            result = _fetch_historical_stock_data("NVDA", AS_OF)

        assert result["price"] == 0.0
        assert result["data_source"] == "fallback"
        assert result["_data_unavailable"] is True


class TestRefusesToSubstituteNeighbouringTradingDay:
    """v0.45.72 新增：给 v0.45.69/0.45.70 那道「末行不是目标日就拒绝」的闸补一条测试。

    ⚠️ 这条是修 fixture 的**必要配套**，不是附赠。锚点修好之后，本文件其余
    用例的末行都正好落在目标日 —— 于是没有一条还会走到这道闸。实测把
    `if _last_date != as_of:` 改成 `if False:`（等于整个回退 v0.45.69/70），
    修完的 5 条用例照样全绿：旧那条脱靶的用例是**靠自己变红**在「覆盖」这道闸的，
    那不叫覆盖。

    NaN 形状与云端快照兜底的语义由 `tests/test_cloud_snapshot_price.py` 专管，
    这里只钉本文件自己的教训：**日线整段就停在目标日之前**（不是 NaN，是压根
    没这一行）时，必须标不可用 —— 那正是 v0.45.72 之前这份 fixture 无意中
    构造出来、却从没有人校验过的那个 4 天缺口。
    """

    def test_stale_last_bar_is_refused_not_substituted(self, monkeypatch):
        monkeypatch.setattr("cloud_snapshot_loader.load_ticker", lambda *a, **k: None)
        stale = (date.fromisoformat(AS_OF) - timedelta(days=4)).isoformat()
        hist = _fake_history([100.0] * 25 + [207.29], last_date=stale)

        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist
            result = _fetch_historical_stock_data("NVDA", AS_OF)

        assert result["price"] == 0.0, "拿到了非目标日的收盘价 = 前一交易日冒充"
        assert result["data_source"] == "fallback"
        assert result["_data_unavailable"] is True
        assert result["_reason"] == f"no_close_on_{AS_OF}"
