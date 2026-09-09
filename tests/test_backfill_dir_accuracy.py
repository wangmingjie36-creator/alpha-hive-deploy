"""
backfill_dir_accuracy.py 的 `_close_after` 未收盘护栏（v0.45.173）

根因取证：`predictions.close_t7` 自 2026-08-28 起持续 NULL，
`backfill_dir_accuracy.py --dry-run` 的自校验每天中位偏离 ~1pp 中止写库
——表面看像"交易日数算错/复权口径不对"，实际是 `backtester._simulate_trade_path`
（见 tests/test_backtest_forming_bar.py）在盘中把"正在形成"的 bar 当成
已收盘价，把 `checked_t7=1`/`price_t7` 写脏了；而 `backfill_dir_accuracy.py`
自己重新拉一次价时，如果**也**恰好在盘中跑，同样会把这根 bar 当真——
两次盘中取价的时间点不同，于是比出一个看似"系统性"的偏离。

`_close_after` 补上跟 `_get_price_at_date` / `_simulate_trade_path` 同一套
护栏后，命中"今天、未收盘"时直接返回 `(None, None)`，调用方把它计入
`misses`（取价失败）而不是塞进自校验样本——不会再拿盘中价跟库里的
（同样可能盘中写脏的）`price_t7` 比出假阳性。
"""

from datetime import date, datetime, time

import pandas as pd
import pytest

import backfill_dir_accuracy as M


def _series(rows):
    """rows: [(date, close), ...] → pandas.Series(index=DatetimeIndex)"""
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.Series([r[1] for r in rows], index=idx)


class TestCloseAfterFormingBarRejected:
    def test_today_intraday_returns_none(self, monkeypatch):
        """命中的第一根 bar 就是今天、市场还没收盘 → 必须拒收。"""
        import data_pipeline
        d = date(2026, 9, 9)
        noon_et = datetime.combine(d, time(12, 0))
        monkeypatch.setattr(data_pipeline, "_exchange_now", lambda: noon_et)

        s = _series([(date(2026, 9, 8), 100.0), (d, 108.0)])
        close, hit_date = M._close_after(s, d)
        assert (close, hit_date) == (None, None)

    @pytest.mark.parametrize("hhmm", [(9, 30), (12, 0), (15, 58)])
    def test_any_time_before_close_rejected(self, monkeypatch, hhmm):
        import data_pipeline
        d = date(2026, 9, 9)
        monkeypatch.setattr(data_pipeline, "_exchange_now",
                             lambda: datetime.combine(d, time(*hhmm)))
        s = _series([(d, 108.0)])
        assert M._close_after(s, d) == (None, None)


class TestCloseAfterClosedBarAccepted:
    def test_after_close_returns_price(self, monkeypatch):
        import data_pipeline
        d = date(2026, 9, 9)
        monkeypatch.setattr(data_pipeline, "_exchange_now",
                             lambda: datetime.combine(d, time(16, 30)))
        s = _series([(d, 108.0)])
        close, hit_date = M._close_after(s, d)
        assert close == 108.0
        assert hit_date == "2026-09-09"

    def test_past_target_date_always_accepted(self, monkeypatch):
        """target_date 是过去的交易日，命中的 bar 自然也是过去的 →
        跟"今天几点"无关，必须放行（`hit_date == _xnow.date()` 天然为假）。"""
        import data_pipeline
        past = date(2026, 8, 20)
        today = date(2026, 9, 9)
        monkeypatch.setattr(data_pipeline, "_exchange_now",
                             lambda: datetime.combine(today, time(10, 0)))
        s = _series([(past, 88.8)])
        close, hit_date = M._close_after(s, past)
        assert close == 88.8

    def test_exchange_clock_unavailable_does_not_block(self, monkeypatch):
        """交易所时钟拿不到时放行——护栏本身故障不该让回填全停。"""
        import data_pipeline
        d = date(2026, 9, 9)
        monkeypatch.setattr(data_pipeline, "_exchange_now", lambda: None)
        s = _series([(d, 108.0)])
        close, hit_date = M._close_after(s, d)
        assert close == 108.0


class TestRealIncidentScenario:
    def test_2026_08_28_batch_would_have_been_deferred_not_aborted(self, monkeypatch):
        """复刻真实事故：2026-08-28 那批 30 条的 T+7 目标日就是「今天」
        （2026-09-09，Labor Day 09-07 被正确跳过），backfill_dir_accuracy.py
        --dry-run 在盘中跑时应当把它当"取价失败"跳过，而不是拿盘中价
        去跟库里的 price_t7 比出"系统性偏离"再中止整个批次。"""
        import data_pipeline
        target = date(2026, 9, 9)
        monkeypatch.setattr(data_pipeline, "_exchange_now",
                             lambda: datetime.combine(target, time(11, 48)))
        s = _series([(date(2026, 8, 28), 255.48), (target, 249.63)])  # 复刻 ABBV
        close, hit_date = M._close_after(s, target)
        assert (close, hit_date) == (None, None)
