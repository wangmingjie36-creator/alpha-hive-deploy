"""
GuardBee 的 FOMC 临近票（v0.45.582）：看 3 天内**全部**宏观事件，不只看最近一个。

旧规则：`get_next_event()`（只给最近一个事件）是 FOMC 且 ≤3 天 ⇒ risk_off +1。窗口里若有别的事件排在 FOMC
**之前**，这张票就被遮住。BLS 把 2027 年 9 月 CPI 排在 09-14、FOMC 是 09-15 ⇒ 参考日 09-12~09-14 不投票。
这张票是真会定政体的：生产存档 2026-09-15 有 20/30 只标的的 risk_off 恰好 2 票（含 FOMC 那张），少了它就是 neutral，
而 risk_off 政体会经 Queen 的 RegimeWeightAdjuster 下调 sentiment 权重。

改动只在被遮的那几天改输出——下面的等价检查钉住这一点（已累积的样本一条不变，故未登记世代边界，理由见 CHANGELOG 0.45.582）。
全部用显式参考日，不读真实时钟。
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

import economic_calendar as ec
from swarm_agents.guard_bee import FOMC_VOTE_WINDOW_DAYS, fomc_vote_days

#: 被遮的三天（BLS 2027 CPI 09-14 在 FOMC 09-15 前一天）
MASKED_2027_09 = {date(2027, 9, 12): 3, date(2027, 9, 13): 2, date(2027, 9, 14): 1}
SWEEP_FROM, SWEEP_TO = date(2025, 1, 1), date(2028, 1, 26)


def _old_rule(ref):
    """v0.45.582 之前 GuardBee 的判据：参考日起**最近的一个**宏观事件是 FOMC 且 ≤3 天 ⇒ 投票。

    当时经 `get_next_event()` 实现；这里直接用四张原始表重写同一语义（同日 FOMC 优先 = 当时
    `get_upcoming_events` 先扫 `_FOMC`、排序稳定），**不经** `get_next_event`——那是仪表盘的函数，
    改它不该让 GuardBee 的测试红（v0.45.585 解耦）。重写与当时实现逐日一致已在 v0.45.585 核过（CHANGELOG）。
    """
    events = []
    for rank, tbl in enumerate((ec._FOMC, ec._CPI, ec._NFP, ec._GDP)):
        events += [(d, rank) for d in ec._parse_dates(tbl) if d >= ref]
    if not events:
        return None
    d, rank = min(events)
    days = (d - ref).days
    return days if rank == 0 and days <= FOMC_VOTE_WINDOW_DAYS else None


def _days(lo, hi):
    d = lo
    while d <= hi:
        yield d
        d += timedelta(days=1)


def _is_masked(ref):
    """被遮的独立定义（不经被测函数）：窗口里有 FOMC，且有一个非 FOMC 事件严格排在它前面。"""
    hi = ref + timedelta(days=FOMC_VOTE_WINDOW_DAYS)
    fomc = [d for d in ec._parse_dates(ec._FOMC) if ref <= d <= hi]
    if not fomc:
        return False
    others = [d for tbl in (ec._CPI, ec._NFP, ec._GDP) for d in ec._parse_dates(tbl)]
    return any(ref <= d < fomc[0] for d in others)


class TestMaskedDaysNowVote:

    def test_2027_09_window_votes(self):
        for ref, days in MASKED_2027_09.items():
            assert fomc_vote_days(ref) == days, ref
            assert _old_rule(ref) is None, f"{ref} 不再被遮——前提变了（日历改了？），重看本测试"

    def test_through_guard_macro_path(self, monkeypatch, stub_yfinance, stub_vixcentral):
        """真 GuardBee 宏观路径（补跑口径，参考日 = 2027-09-13）：FOMC 票投出、fomc_days = 2"""
        import fred_macro as fm
        from swarm_agents.guard_bee import GuardBeeSentinel
        import vix_term_structure
        monkeypatch.setattr(vix_term_structure, "get_vix_term_structure",
                            lambda force_refresh=False: pytest.fail("补跑不许读实时期限结构"))
        # 测试前后都不许装着快照——conftest 的 autouse `_no_leaked_macro_snapshot` 逐条强制（v0.45.366）⇒
        # 测试前的状态恒为「未装」，卸载就是复原。别改成用 monkeypatch 还原全局：那会在守卫检查之后才还原，守卫照红。
        fm.set_macro_snapshot("2027-09-13", {})
        try:
            g = GuardBeeSentinel.__new__(GuardBeeSentinel)
            with patch("fred_macro.get_macro_context",
                       return_value={"yield_curve": "normal", "gold_trend": "stable"}):
                r = g._calc_macro_adjustment("AMC")
        finally:
            fm.set_macro_snapshot(None)
        assert r["details"]["macro_ref_date"] == "2027-09-13"
        assert r["details"].get("fomc_days") == 2
        assert "FOMC 2天后" in r["signals"]
        assert r["regime_votes"]["risk_off"] >= 1


class TestEquivalenceWithOldRule:
    """改动只在被遮的那几天改输出。"""

    def test_differs_only_on_masked_days(self):
        diffs = {d for d in _days(SWEEP_FROM, SWEEP_TO) if fomc_vote_days(d) != _old_rule(d)}
        assert set(MASKED_2027_09) <= diffs
        assert all(_is_masked(d) for d in diffs), [d for d in diffs if not _is_masked(d)]
        # 反过来：被遮的日子新规则都投票、旧规则都不投（差异的方向只有「补上被遮的票」）
        for d in diffs:
            assert _old_rule(d) is None and fomc_vote_days(d) is not None, d

    def test_no_difference_before_first_masked_day(self):
        """2025-01-01 ~ 2027-09-11 逐日与旧规则相同 ⇒ 生产已累积的样本（与 2027-09 之前将要产生的）一条不变。
        日历的过去日期若被改得造出新的被遮日，这条会红——那时要回头重议「不登记世代边界」的判断。"""
        first = min(MASKED_2027_09)
        diffs = [d for d in _days(SWEEP_FROM, first - timedelta(days=1)) if fomc_vote_days(d) != _old_rule(d)]
        assert diffs == []

    def test_window_bounds_match_old_rule(self):
        """窗口两端（当天 = 0、第 3 天）与旧规则同：FOMC 当天与前 1~3 天投票，前 4 天不投"""
        f = date(2027, 1, 27)
        assert [fomc_vote_days(f - timedelta(days=k)) for k in range(5)] == [0, 1, 2, 3, None]
        assert [_old_rule(f - timedelta(days=k)) for k in range(5)] == [0, 1, 2, 3, None]
