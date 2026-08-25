"""
T+N 评分不得用「正在形成」的 bar（v0.45.10 回归）

`Backtester._get_price_at_date` 的 docstring 写的是"获取预测日后 N 个交易日的
**收盘价**"，实现是 `stock.history(start=目标日, ...)["Close"].iloc[0]`。

盘中调用时，目标日那根 bar 尚未完成——它的 `Close` 是**此刻最新价**，不是收盘价。
此前无护栏，于是任何盘中运行都会拿盘中价当收盘价评分。

实测损害（2026-08-24 那批 T+1，在 08-25 盘中评的，抽样 5 只有 2 只判反）：
- `AMC`  记 +2.251% 判对，真实收盘 −1.124% → 应判**错**
- `BILI` 记 +0.812%（看空却收益为正）判对 → 应判**错**

正常 14:00 PDT 定时扫描在 13:00 收盘后跑，所以这个洞一直没暴露——
和 v0.43.23 那次"手动跑永远 12/12"是同一类：**运行时机掩盖了缺陷**。

评分闸门 `get_pending_checks` 只判 `预测日 + N 交易日 <= 今天`，
**没有任何"当天是否已收盘"的概念**，护栏只能加在取价这一层。
"""

from datetime import date, datetime, time
from datetime import timezone as _tz
from unittest.mock import MagicMock

import pytest

import backtester as B


class _Idx:
    """最小 DatetimeIndex 替身：只需要 .date()"""
    def __init__(self, d):
        self._d = d

    def date(self):
        return self._d


class _Hist:
    """最小 DataFrame 替身"""
    def __init__(self, bar_date, close):
        self.index = [_Idx(bar_date)]
        self._close = close
        self.empty = False

    def __getitem__(self, k):
        assert k == "Close"
        return MagicMock(iloc=[self._close])

    def __len__(self):
        return 1


def _run(monkeypatch, bar_date, exchange_now, close=100.0):
    """把 yfinance 与交易所时钟都换掉，只测护栏本身"""
    tk = MagicMock()
    tk.history.return_value = _Hist(bar_date, close)
    monkeypatch.setattr(B, "yf", MagicMock(Ticker=MagicMock(return_value=tk)))

    import data_pipeline
    monkeypatch.setattr(data_pipeline, "_exchange_now", lambda: exchange_now)

    bt = B.Backtester.__new__(B.Backtester)
    return bt._get_price_at_date("AMC", "2026-08-24", 1)


class TestFormingBarRejected:
    def test_intraday_returns_none(self, monkeypatch):
        """目标日就是今天、且还没收盘 → 必须返回 None，不能拿盘中价充数"""
        d = date(2026, 8, 25)
        noon_et = datetime(2026, 8, 25, 12, 30, tzinfo=_tz.utc).replace(tzinfo=None)
        noon_et = datetime.combine(d, time(12, 30))
        assert _run(monkeypatch, d, noon_et) is None

    @pytest.mark.parametrize("hhmm", [(9, 30), (12, 0), (15, 58)])
    def test_any_time_before_close_rejected(self, monkeypatch, hhmm):
        d = date(2026, 8, 25)
        assert _run(monkeypatch, d, datetime.combine(d, time(*hhmm))) is None


class TestClosedBarAccepted:
    def test_after_close_returns_price(self, monkeypatch):
        """收盘后（15:59 美东起）那根 bar 已完成，可以用"""
        d = date(2026, 8, 25)
        assert _run(monkeypatch, d, datetime.combine(d, time(16, 30)), 123.45) == 123.45

    def test_past_date_always_accepted(self, monkeypatch):
        """目标日是过去的交易日 → 无论此刻几点都已收盘"""
        past, today = date(2026, 8, 20), date(2026, 8, 25)
        now = datetime.combine(today, time(10, 0))  # 今天盘中
        assert _run(monkeypatch, past, now, 88.8) == 88.8

    def test_exchange_clock_unavailable_does_not_block(self, monkeypatch):
        """交易所时钟拿不到时放行——护栏本身故障不该让回测全停"""
        d = date(2026, 8, 25)
        assert _run(monkeypatch, d, None, 77.7) == 77.7


class TestRealDamageScenario:
    def test_amc_case_would_have_been_skipped(self, monkeypatch):
        """复刻真实事故：2026-08-25 盘中评 8/24 的 T+1。
        修复前拿到 2.73（盘中，记 +2.251% 判对），
        真实收盘 2.64（−1.124%，应判错）。护栏应让它返回 None 而不是 2.73。"""
        d = date(2026, 8, 25)
        intraday_1244 = datetime.combine(d, time(15, 44))  # 12:44 PDT = 15:44 ET
        assert _run(monkeypatch, d, intraday_1244, 2.73) is None
