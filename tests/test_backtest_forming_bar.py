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

v0.45.173 补：护栏当时只补在 `_get_price_at_date` 自己身上，姊妹取价路径
`_simulate_trade_path`（T+7 路径依赖退出，决定 `checked_t7`/`price_t7`/
`return_t7`/`correct_t7`）**从未补上**——`self._history()` 与
`_get_price_at_date` 共用同一份批量预取缓存，同样会带回今天正在形成的
那根 bar。后果：`checked_t7` 被盘中价污染置 1，而同一行的 `close_t7`
（走 `_get_price_at_date`）因护栏生效正确留 None，两者从此永久不一致；
`backfill_dir_accuracy.py` 事后拿收盘价重算时把这个不一致测成了"系统性
口径错误"并中止写库——根因在这边，不在它。`TestSimulateTradePathFormingBar`
覆盖这一半。
"""

from datetime import date, datetime, time, timedelta
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


# ==================== v0.45.173：_simulate_trade_path 姊妹护栏 ====================
# T+7 路径依赖退出用的是另一条取价路径（`self._history()`），上面这些测试
# 完全没有触碰到它。下面复刻同一个洞：批量预取缓存把「今天正在形成」的
# 那根 bar 带回来时，_simulate_trade_path 有没有跟 _get_price_at_date 一样拒收。

import pandas as pd  # noqa: E402


def _make_hist(rows):
    """rows: [(date, open, high, low, close), ...] → 一份 OHLC DataFrame，
    形状与 `yf.Ticker().history()` / `self._history()` 的返回一致。"""
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows]},
        index=idx,
    )


def _bt_with_history(monkeypatch, hist_df, exchange_now):
    import data_pipeline
    monkeypatch.setattr(data_pipeline, "_exchange_now", lambda: exchange_now)
    bt = B.Backtester.__new__(B.Backtester)
    monkeypatch.setattr(bt, "_history", lambda *a, **k: hist_df)
    return bt


class TestSimulateTradePathFormingBar:
    """T+7 = 今天、且未触发 SL/TP 时，_simulate_trade_path 不得把"正在形成"的
    最后一根 bar 当 T7_CLOSE 收盘价——这正是 close_t7 长期 NULL 的根因
    （checked_t7 被这条路径污染置 1，close_t7 被 _get_price_at_date 正确拦下
    留 None，两者从此对不上，backfill_dir_accuracy.py 的自校验测出的正是这个缺口）。"""

    def _week_of_rows(self, last_close=105.0):
        """7 个交易日，前 6 天价格在 SL(95)/TP(110) 区间内平静波动，
        第 7 天（=今天，正在形成）给出 last_close。"""
        base = date(2026, 8, 31)
        rows = [(base + timedelta(days=i), 100.5, 101.5, 99.5, 100.8) for i in range(6)]
        rows.append((date(2026, 9, 9), 100.0, 105.5, 99.8, last_close))
        return rows

    def test_intraday_forming_last_bar_defers(self, monkeypatch):
        """今天盘中、第 7 天是正在形成的 bar → 剔除后只剩 6 天，不够 days_ahead=7，
        必须返回 None（留待下次重评），不能拿它当 T7_CLOSE。"""
        rows = self._week_of_rows(last_close=108.0)
        hist = _make_hist(rows)
        noon_et = datetime(2026, 9, 9, 12, 0)
        bt = _bt_with_history(monkeypatch, hist, noon_et)
        path = bt._simulate_trade_path("TEST", "2026-08-28", 7, 100.0, "bullish")
        assert path is None

    def test_after_close_uses_full_week_including_last_bar(self, monkeypatch):
        """收盘后（15:59 ET 起）第 7 天的 bar 已经是真收盘价，可以正常平仓。"""
        rows = self._week_of_rows(last_close=108.0)
        hist = _make_hist(rows)
        after_close = datetime(2026, 9, 9, 16, 30)
        bt = _bt_with_history(monkeypatch, hist, after_close)
        path = bt._simulate_trade_path("TEST", "2026-08-28", 7, 100.0, "bullish")
        assert path is not None
        assert path["exit_reason"] == "T7_CLOSE"
        assert path["exit_price"] == 108.0
        assert path["holding_days"] == 7

    def test_sl_triggered_on_settled_day_unaffected_by_forming_bar(self, monkeypatch):
        """SL 在第 3 天（已收盘）就触发了——跟第 7 天是否正在形成无关，
        必须照常在第 3 天平仓，不能因为新护栏被拖到 None。"""
        base = date(2026, 8, 31)
        rows = [(base + timedelta(days=i), 100.5, 101.5, 99.5, 100.8) for i in range(3)]
        rows[2] = (base + timedelta(days=2), 100.5, 101.0, 94.0, 94.5)  # 第 3 天击穿 SL(95)
        rows += [(base + timedelta(days=i), 90.0, 91.0, 89.0, 90.0) for i in range(3, 6)]
        rows.append((date(2026, 9, 9), 90.0, 90.5, 89.5, 90.0))  # 第 7 天：正在形成
        hist = _make_hist(rows)
        noon_et = datetime(2026, 9, 9, 12, 0)
        bt = _bt_with_history(monkeypatch, hist, noon_et)
        path = bt._simulate_trade_path("TEST", "2026-08-28", 7, 100.0, "bullish")
        assert path is not None
        assert path["exit_reason"] == "SL"
        assert path["holding_days"] == 3

    def test_exchange_clock_unavailable_does_not_block(self, monkeypatch):
        """交易所时钟拿不到时放行——跟 _get_price_at_date 的降级语义一致，
        护栏本身故障不该让整条路径停摆。"""
        rows = self._week_of_rows(last_close=108.0)
        hist = _make_hist(rows)
        bt = _bt_with_history(monkeypatch, hist, None)
        path = bt._simulate_trade_path("TEST", "2026-08-28", 7, 100.0, "bullish")
        assert path is not None
        assert path["exit_reason"] == "T7_CLOSE"
