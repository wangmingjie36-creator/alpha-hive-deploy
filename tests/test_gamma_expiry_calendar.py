"""calculate_gamma_expiry_calendar() 的近月窗口回归测试（v0.45.258）。

背景：NVDA 2026-09-14 实测里，Pin Risk 行权价报出 $270（现价 $211，偏离 28%），
根因两层：
  1. 上游 `fetch_options_chain()` 的默认到期日选择器（`_select_expiries`）
     按设计排除 DTE<7，且日历口径少算一天，真正最近、OI 最大的到期日
     （3 天后到期）整个从 calls/puts 里消失，日历只能从剩下的次要到期日
     矮子里拔将军——本文件不覆盖这一层（属 options_analyzer 的取数管线，
     见 tests/test_options_analyzer.py 里的 wiring 测试）。
  2. 即便近月数据补回来，"按 OI 降序取第一名" 本身还有第二层坑：标准 1 月
     到期挂牌以来累积时间最长，总 OI 常年滚雪球式超过任何近月到期日
     （NVDA 实测 2027-01-15 总 OI 反而略高于 3 天后到期的 2026-09-18），
     若不设窗口会把 122 天外的 LEAPS 错认成"下一主要到期日"——Pin Risk/
     Charm 是随本次到期临近才增强的做市商对冲效应，年之外的到期日今天
     不构成 Pin Risk。本文件覆盖的正是这一层：`_PIN_RISK_MAX_DTE` 窗口。
"""

from datetime import date, timedelta

from market_intelligence import calculate_gamma_expiry_calendar


def _mk(strike, oi, expiry, gamma=0.05, side="call"):
    key = "strike"
    row = {key: strike, "openInterest": oi, "gamma": gamma, "expiry": expiry}
    return row


class TestNearTermWindowPreference:
    def test_far_dated_leaps_with_bigger_oi_does_not_win_pin_risk(self):
        """近月到期日 OI 较小，但仍应赢过 OI 更大的远期 LEAPS。"""
        today = date.today()
        near_expiry = (today + timedelta(days=3)).isoformat()
        leaps_expiry = (today + timedelta(days=120)).isoformat()

        calls = [
            _mk(100.0, 20_000, near_expiry),
            _mk(150.0, 50_000, leaps_expiry),  # OI 更大，但太远
        ]
        puts = [
            _mk(100.0, 15_000, near_expiry),
            _mk(150.0, 40_000, leaps_expiry),
        ]

        result = calculate_gamma_expiry_calendar(calls, puts, stock_price=100.0)

        assert result["pin_expiry"] == near_expiry
        assert result["pin_strike"] == 100.0
        assert result["days_to_pin"] == 3

    def test_within_window_still_picks_highest_oi(self):
        """窗口内仍按 OI 排序，不是无脑选最近的到期日。"""
        today = date.today()
        exp_a = (today + timedelta(days=5)).isoformat()
        exp_b = (today + timedelta(days=20)).isoformat()

        calls = [_mk(100.0, 5_000, exp_a), _mk(110.0, 30_000, exp_b)]
        puts = [_mk(100.0, 4_000, exp_a), _mk(110.0, 25_000, exp_b)]

        result = calculate_gamma_expiry_calendar(calls, puts, stock_price=105.0)

        assert result["pin_expiry"] == exp_b
        assert result["pin_strike"] == 110.0

    def test_falls_back_to_full_chain_when_no_near_term_listed(self):
        """链本身稀疏、近月一个到期日都没有挂牌时，不该判成"日历不可用"。"""
        today = date.today()
        leaps_expiry = (today + timedelta(days=200)).isoformat()

        calls = [_mk(100.0, 10_000, leaps_expiry)]
        puts = [_mk(100.0, 8_000, leaps_expiry)]

        result = calculate_gamma_expiry_calendar(calls, puts, stock_price=100.0)

        assert result["pin_expiry"] == leaps_expiry
        assert result["pin_strike"] == 100.0
        assert result["expiry_oi"]  # 没有退化成空列表

    def test_expiry_oi_top_row_matches_pin_expiry(self):
        """generate_ml_report 用 expiry_oi[0] 当"下一主要到期日"，必须与 pin_expiry 一致。"""
        today = date.today()
        near_expiry = (today + timedelta(days=3)).isoformat()
        leaps_expiry = (today + timedelta(days=120)).isoformat()

        calls = [_mk(100.0, 20_000, near_expiry), _mk(150.0, 50_000, leaps_expiry)]
        puts = [_mk(100.0, 15_000, near_expiry), _mk(150.0, 40_000, leaps_expiry)]

        result = calculate_gamma_expiry_calendar(calls, puts, stock_price=100.0)

        assert result["expiry_oi"][0]["expiry"] == result["pin_expiry"]
