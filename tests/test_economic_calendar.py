"""
Tests for economic_calendar module — FOMC/CPI/NFP/GDP event calendar
"""

import pytest
from datetime import date


class TestGetUpcomingEvents:
    """get_upcoming_events() 测试"""

    def test_returns_list(self):
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=365, ref_date=date(2026, 1, 1))
        assert isinstance(events, list)
        assert len(events) > 0

    def test_event_structure_matches_catalyst(self):
        """验证返回格式复用 ChronosBeeHorizon 催化剂结构"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=60, ref_date=date(2026, 3, 1))
        assert len(events) > 0
        e = events[0]
        assert "event" in e
        assert "date" in e
        assert "days_until" in e
        assert "type" in e
        assert "severity" in e
        assert isinstance(e["days_until"], int)
        assert e["days_until"] >= 0
        assert e["type"] in ("fomc", "cpi", "nfp", "gdp")
        assert e["severity"] in ("high", "medium")

    def test_sorted_by_days_until(self):
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=90, ref_date=date(2026, 1, 15))
        days = [e["days_until"] for e in events]
        assert days == sorted(days)

    def test_empty_when_days_zero(self):
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=0, ref_date=date(2026, 6, 1))
        # days=0 means only events on the reference date itself
        assert isinstance(events, list)
        # 所有返回事件的 days_until 必须为 0（当天事件）
        assert all(e["days_until"] == 0 for e in events)

    def test_fomc_dates_2026(self):
        """验证 2026 FOMC 日期正确"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=365, ref_date=date(2026, 1, 1))
        fomc = [e for e in events if e["type"] == "fomc"]
        fomc_dates = {e["date"] for e in fomc}
        assert "2026-01-28" in fomc_dates
        assert "2026-03-18" in fomc_dates
        assert "2026-12-09" in fomc_dates
        assert len(fomc) == 8  # 8 FOMC meetings per year

    def test_respects_ref_date(self):
        """确保只返回 ref_date 当天或之后的事件"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=30, ref_date=date(2026, 6, 15))
        for e in events:
            assert e["date"] >= "2026-06-15"

    def test_no_past_events(self):
        """不返回过去的事件"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=30, ref_date=date(2026, 3, 4))
        for e in events:
            assert e["days_until"] >= 0


class TestGetNextEvent:
    """get_next_event() 测试"""

    def test_returns_nearest(self):
        from economic_calendar import get_next_event
        evt = get_next_event(ref_date=date(2026, 3, 4))
        assert evt is not None
        assert evt["days_until"] >= 0
        # 3/4 → 最近应该是 3/6 NFP (2天后)
        assert evt["days_until"] <= 60

    def test_returns_none_when_no_events(self):
        """超出日历范围时返回 None"""
        from economic_calendar import get_next_event
        evt = get_next_event(ref_date=date(2030, 1, 1))
        assert evt is None

    def test_matches_first_upcoming(self):
        from economic_calendar import get_upcoming_events, get_next_event
        ref = date(2026, 3, 4)
        events = get_upcoming_events(days=60, ref_date=ref)
        nxt = get_next_event(ref_date=ref)
        if events:
            assert nxt == events[0]


# ══════════════════════════════════════════════════════════════════════
# v0.45.65：硬编码日历的「会过期」问题
#
# 上面所有测试都注入了 ref_date，所以它们是**注入时钟**的——日历表被日期走完
# 之后，它们照样全绿。这正是过期能不被发现的原因：单测在 2026 年的世界里跑，
# 而生产在真实的今天跑。
#
# 下面这一组反过来：TestCoverageHorizon 故意**不注入** ref_date，用真实 today
# 守住覆盖地平线；其余几组用注入日期，验证「表走完时会不会吼」。
# ══════════════════════════════════════════════════════════════════════

import logging


class TestCoverageHorizon:
    """⏰ 陈旧化闸门 —— 唯一一组故意用真实 today 的测试

    它的职责不是验证逻辑正确，而是在日历**还没坏**的时候先变红，
    把「某天悄悄不再返回宏观事件」提前成「今天 CI 红一条」。
    变红时的动作不是改测试阈值，而是去官方站点抄新日程。
    """

    def test_no_table_falls_below_its_horizon_threshold(self):
        """任何一张表的覆盖地平线掉到阈值以下就变红（逐表判，不看总和）"""
        from economic_calendar import get_calendar_health
        health = get_calendar_health()          # ← 真实 today，不注入

        offenders = health["stale_tables"] + health["exhausted_tables"]
        if offenders:
            lines = []
            for key in offenders:
                t = health["per_table"][key]
                lines.append(
                    f"  {key}: 只覆盖到 {t['last_date']}（剩 {t['horizon_days']} 天，"
                    f"阈值 {t['min_horizon_days']} 天）\n"
                    f"        来源 {t['source']}\n"
                    f"        待验证 {t['pending']}"
                )
            pytest.fail(
                "经济日历覆盖不足，宏观催化剂信号即将（或已经）静默失效：\n"
                + "\n".join(lines)
                + "\n\n修复：去上面的来源页抄取新发布的日程，逐条写进 economic_calendar.py，"
                  "并同步上移该表的 verified_through。\n"
                  "     禁止按「每月第二周 / 第一个周五」推算——v0.45.65 正是因此在 2026 年"
                  "写错了 9 个日期。\n"
                  "     注意 bls.gov 会 403 掉默认 UA，取数时带联系人 UA：\n"
                  "       curl -A 'AlphaHive-research/1.0 (你的邮箱)' "
                  "https://www.bls.gov/schedule/news_release/cpi.htm\n"
                  "     阈值本身不是可调项：调高阈值只会让告警闭嘴，不会让日历变长。"
            )

    def test_next_event_actually_resolvable_today(self):
        """真实 today 下必须能取到下一个宏观事件（60 天窗口）"""
        from economic_calendar import get_next_event
        assert get_next_event() is not None, (
            "真实 today 下未来 60 天没有任何宏观事件——日历大概率已走完，"
            "见 get_calendar_health()"
        )


class TestCalendarHealth:
    """get_calendar_health() —— 逐表体检"""

    def test_structure(self):
        from economic_calendar import get_calendar_health
        h = get_calendar_health(ref_date=date(2026, 8, 29))
        for k in ("ok", "status", "horizon_days", "last_date", "binding_table",
                  "binding_margin_days", "stale_tables", "exhausted_tables",
                  "per_table", "checked_at"):
            assert k in h, f"缺字段 {k}"
        assert h["status"] in ("ok", "stale", "exhausted")
        assert set(h["per_table"]) == {"fomc", "cpi", "nfp", "gdp"}
        for key, t in h["per_table"].items():
            assert t["source"].startswith("https://"), f"{key} 缺来源"
            assert t["margin_days"] == t["horizon_days"] - t["min_horizon_days"]

    def test_horizon_is_the_minimum_not_the_maximum(self):
        """总地平线取最短的那张表 —— 用 max 会让 FOMC 掩盖 CPI/NFP/GDP 断供"""
        from economic_calendar import get_calendar_health
        h = get_calendar_health(ref_date=date(2026, 8, 29))
        horizons = [t["horizon_days"] for t in h["per_table"].values()]
        assert h["horizon_days"] == min(horizons)
        assert h["horizon_days"] != max(horizons), (
            "各表地平线全相等时这条断言无意义；出现即说明表结构变了，请重新设计本测试"
        )

    def test_long_fomc_table_does_not_mask_dead_bls_tables(self):
        """🔴 回归：聚合掩盖分量

        2027-06-01 这天，FOMC 还剩 5 次会议（覆盖到 2028-01），
        而 CPI/NFP/GDP 三张表早在 2026-12 就走完了。
        若体检只看「全表最大日期」，会报告一切正常。
        """
        from economic_calendar import get_calendar_health, get_upcoming_events
        ref = date(2027, 6, 1)
        h = get_calendar_health(ref_date=ref)

        assert h["status"] == "exhausted"
        assert set(h["exhausted_tables"]) == {"cpi", "nfp", "gdp"}
        assert h["per_table"]["fomc"]["exhausted"] is False
        assert h["per_table"]["fomc"]["remaining"] > 0

        # 而且此刻 get_upcoming_events 是**非空**的（2027-06-09 有 FOMC）——
        # 所以告警绝不能以「返回值为空」为触发条件
        events = get_upcoming_events(days=30, ref_date=ref)
        assert len(events) > 0
        assert all(e["type"] == "fomc" for e in events)

    def test_all_exhausted_far_future(self):
        from economic_calendar import get_calendar_health
        h = get_calendar_health(ref_date=date(2029, 1, 1))
        assert h["status"] == "exhausted"
        assert h["ok"] is False
        assert set(h["exhausted_tables"]) == {"fomc", "cpi", "nfp", "gdp"}


class TestStalenessIsLoud:
    """表走完 / 快走完时必须留下日志痕迹，不能安静地返回 []"""

    LOGGER = "alpha_hive.economic_calendar"

    @pytest.fixture(autouse=True)
    def _clear_throttle(self):
        from economic_calendar import _reset_warning_throttle
        _reset_warning_throttle()
        yield
        _reset_warning_throttle()

    def test_exhausted_logs_error(self, caplog):
        from economic_calendar import get_upcoming_events
        with caplog.at_level(logging.DEBUG, logger=self.LOGGER):
            events = get_upcoming_events(days=30, ref_date=date(2029, 1, 1))
        assert events == []
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "日历走完却没有 ERROR 日志 —— 这就是静默降级"
        assert "经济日历已被日期走完" in errors[0].getMessage()

    def test_stale_logs_warning(self, caplog):
        from economic_calendar import get_upcoming_events
        # 2026-09-20 这天：CPI 剩 81 天、NFP 剩 75 天（均低于 90 阈值 → stale），
        # 而四张表都还有未来条目（GDP 尚余 10-29）→ 不会升级成 exhausted。
        # ⚠️ 选点要卡在 gdp 走完（2026-10-29）之前，否则 ERROR 会盖过 WARNING。
        with caplog.at_level(logging.DEBUG, logger=self.LOGGER):
            get_upcoming_events(days=30, ref_date=date(2026, 9, 20))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "日历即将过期却没有 WARNING 日志"
        assert "经济日历即将过期" in warnings[0].getMessage()

    def test_healthy_calendar_is_quiet(self, caplog):
        """健康时不许吼 —— 天天喊狼来了的告警等于没有告警"""
        from economic_calendar import get_upcoming_events
        with caplog.at_level(logging.DEBUG, logger=self.LOGGER):
            get_upcoming_events(days=30, ref_date=date(2026, 1, 15))
        noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not noisy, f"健康日历产生了噪音告警: {[r.getMessage() for r in noisy]}"

    def test_warning_is_throttled_per_process(self, caplog):
        """一次扫描 30 只标的会调 30 次日历，同一状态只吼一次"""
        from economic_calendar import get_upcoming_events
        with caplog.at_level(logging.DEBUG, logger=self.LOGGER):
            for _ in range(5):
                get_upcoming_events(days=30, ref_date=date(2029, 1, 1))
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1, f"节流失效，吼了 {len(errors)} 次"

    def test_empty_result_alone_does_not_trigger_alarm(self, caplog):
        """安静窗口（表健康但未来 N 天恰好无事件）不许报警"""
        from economic_calendar import get_upcoming_events
        # 2026-01-16 起 5 天内没有任何宏观事件（下一个是 01-28 FOMC）
        with caplog.at_level(logging.DEBUG, logger=self.LOGGER):
            events = get_upcoming_events(days=5, ref_date=date(2026, 1, 16))
        assert events == []
        noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not noisy, "把「这几天没事件」误报成「日历坏了」"


class TestDateProvenance:
    """日期出处纪律 —— 防止「按规律推算」重演"""

    def test_no_dates_beyond_verified_through(self):
        """表里不许出现超过 verified_through 的日期

        加新年份时必须同时上移 verified_through，逼你回源站看一眼。
        """
        from economic_calendar import _TABLE_SPECS
        for key, spec in _TABLE_SPECS.items():
            vt = date.fromisoformat(spec["verified_through"])
            for ds in spec["dates"]:
                assert date.fromisoformat(ds) <= vt, (
                    f"{key} 表含 {ds}，超出已核对边界 {spec['verified_through']}。"
                    f"若确已核对官方日程，请上移 verified_through；"
                    f"若是推算出来的，请删掉——空着比错着好。"
                )

    def test_pending_publication_is_declared(self):
        """未发布的年份必须显式写成待验证，不能留空当作已覆盖"""
        from economic_calendar import _TABLE_SPECS
        for key, spec in _TABLE_SPECS.items():
            assert spec["pending"] and "待验证" in spec["pending"], (
                f"{key} 表没有声明待验证边界"
            )

    def test_tables_sorted_unique_parseable(self):
        from economic_calendar import _TABLE_SPECS
        for key, spec in _TABLE_SPECS.items():
            ds = [date.fromisoformat(x) for x in spec["dates"]]
            assert ds == sorted(ds), f"{key} 表未按日期排序"
            assert len(set(ds)) == len(ds), f"{key} 表有重复日期"


class TestPublishedDates:
    """对官方已发布日程的定点核对（抄错了要红）"""

    def test_fomc_2027_matches_fed_calendar(self):
        """2027 全年八次，取两日会议的第二天（声明发布日）"""
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(days=400, ref_date=date(2027, 1, 1))
        got = sorted(e["date"] for e in events
                     if e["type"] == "fomc" and e["date"].startswith("2027-"))
        assert got == [
            "2027-01-27",   # Jan 26-27
            "2027-03-17",   # Mar 16-17*
            "2027-04-28",   # Apr 27-28
            "2027-06-09",   # Jun 8-9*
            "2027-07-28",   # Jul 27-28
            "2027-09-15",   # Sep 14-15*
            "2027-10-27",   # Oct 26-27
            "2027-12-08",   # Dec 7-8*
        ]

    def test_fomc_2028_only_the_one_published_meeting(self):
        """官方 2028 只发布了 1/25-26 一次，其余不许自行补齐"""
        from economic_calendar import _FOMC
        y2028 = [d for d in _FOMC if d.startswith("2028-")]
        assert y2028 == ["2028-01-26"]

    def test_2026_bls_dates_are_the_published_ones_not_the_rule_of_thumb(self):
        """🔴 回归：v0.45.65 修正的 9 个日期，别再被「规律」改回去"""
        from economic_calendar import _CPI, _NFP, _GDP
        # 官方值 → 曾经的推算错值
        for table, fixes in (
            (_CPI, {"2026-01-13": "2026-01-14", "2026-04-10": "2026-04-14",
                    "2026-09-11": "2026-09-15", "2026-10-14": "2026-10-13",
                    "2026-11-10": "2026-11-17", "2026-12-10": "2026-12-09"}),
            (_NFP, {"2026-02-11": "2026-02-06", "2026-05-08": "2026-05-01"}),
            (_GDP, {"2026-10-29": "2026-10-28"}),
        ):
            for official, stale in fixes.items():
                assert official in table, f"官方日期 {official} 不在表里"
                assert stale not in table, f"推算错值 {stale} 又回来了"

    def test_bls_tables_have_no_unpublished_2027_entries(self):
        """BLS/BEA 2027 日程未发布时，表里不许出现 2027 —— 空着，等官方"""
        from economic_calendar import _TABLE_SPECS
        for key in ("cpi", "nfp", "gdp"):
            spec = _TABLE_SPECS[key]
            if date.fromisoformat(spec["verified_through"]) >= date(2027, 1, 1):
                continue          # 官方已发布并核对过，本条自动让路
            assert not [d for d in spec["dates"] if d >= "2027-01-01"], (
                f"{key} 表出现了 2027 日期，但 verified_through 仍是 "
                f"{spec['verified_through']} —— 要么核对后上移边界，要么删掉"
            )
