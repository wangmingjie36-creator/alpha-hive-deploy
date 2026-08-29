"""
Tests for economic_calendar_watch —— 上游宏观日程发布监视器（v0.45.67）

全部离线：`_fetch` 被 monkeypatch 掉，不打网络。
本文件守的核心不变式只有一条：**「抓不到 / 看不懂」绝不能被报成「没有新日程」。**
一个静默失败的监视器，就是它要治的那个 bug 升了一级。
"""

import json
import pytest
from datetime import date
from pathlib import Path


# ── 最小页面样本（结构照抄真实页面，内容裁剪）──

FOMC_HTML = """
<html><body>
<h4>2027 FOMC Meetings</h4>
<table><tr><td>January</td><td>26-27</td></tr>
<tr><td>March</td><td>16-17*</td></tr>
<tr><td>April</td><td>27-28</td></tr>
<tr><td>June</td><td>8-9*</td></tr>
<tr><td>July</td><td>27-28</td></tr>
<tr><td>September</td><td>14-15*</td></tr>
<tr><td>October</td><td>26-27</td></tr>
<tr><td>December</td><td>7-8*</td></tr></table>
<p>Note: A two-day meeting is scheduled for January 25-26, 2028.</p>
</body></html>
"""

# 含一条 "22 (notation vote)" —— 非例会，必须被跳过
FOMC_HTML_WITH_NOTATION = """
<html><body>
<h4>2025 FOMC Meetings</h4>
<table><tr><td>July</td><td>29-30</td></tr>
<tr><td>August</td><td>22 (notation vote)</td></tr>
<tr><td>September</td><td>16-17*</td></tr></table>
</body></html>
"""

BLS_HTML = """
<html><body>
<h1>Schedule of Releases for the Consumer Price Index</h1>
<table>
<tr><th>Reference Month</th><th>Release Date</th><th>Release Time</th></tr>
<tr><td>December 2026</td><td>Jan. 13, 2027</td><td>08:30 AM</td></tr>
<tr><td>January 2027</td><td>Feb. 11, 2027</td><td>08:30 AM</td></tr>
<tr><td>February 2027</td><td>Mar. 10, 2027</td><td>08:30 AM</td></tr>
<tr><td>March 2027</td><td>Apr. 13, 2027</td><td>08:30 AM</td></tr>
<tr><td>April 2027</td><td>May 12, 2027</td><td>08:30 AM</td></tr>
<tr><td>May 2027</td><td>Jun. 10, 2027</td><td>08:30 AM</td></tr>
<tr><td>June 2027</td><td>Jul. 13, 2027</td><td>08:30 AM</td></tr>
<tr><td>July 2027</td><td>Aug. 11, 2027</td><td>08:30 AM</td></tr>
<tr><td>August 2027</td><td>Sep. 14, 2027</td><td>08:30 AM</td></tr>
<tr><td>September 2027</td><td>Oct. 13, 2027</td><td>08:30 AM</td></tr>
<tr><td>October 2027</td><td>Nov. 10, 2027</td><td>08:30 AM</td></tr>
<tr><td>November 2027</td><td>Dec. 10, 2027</td><td>08:30 AM</td></tr>
</table>
<p>Subscribe to the BLS Online Calendar</p>
</body></html>
"""

BEA_HTML = """
<html><body><table>
<tr><td><div class="release-date">October 29</div></td>
    <td>GDP (Advance Estimate), 3rd Quarter 2026</td></tr>
<tr><td><div class="release-date">November 25</div></td>
    <td>GDP (Second Estimate) and Corporate Profits, 3rd Quarter 2026</td></tr>
<tr><td><div class="release-date">January 28</div></td>
    <td>GDP (Advance Estimate), 4th Quarter 2026</td></tr>
</table></body></html>
"""

REDESIGNED_HTML = "<html><body><h1>We have moved</h1><p>Nothing here.</p></body></html>"


class TestParsers:
    def test_fomc_takes_second_day_of_each_meeting(self):
        from economic_calendar_watch import parse_fomc
        dates, err = parse_fomc(FOMC_HTML)
        assert err is None
        assert [d.isoformat() for d in dates] == [
            "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
            "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
            "2028-01-26",          # 来自脚注
        ]

    def test_fomc_skips_notation_vote(self):
        """2025-08-22 是 notation vote，不是两日例会，不能进日期表"""
        from economic_calendar_watch import parse_fomc
        dates, err = parse_fomc(FOMC_HTML_WITH_NOTATION)
        assert err is None
        assert [d.isoformat() for d in dates] == ["2025-07-30", "2025-09-17"]

    def test_bls_schedule_dates(self):
        from economic_calendar_watch import parse_bls_schedule
        dates, err = parse_bls_schedule(BLS_HTML)
        assert err is None
        assert len(dates) == 12
        assert max(dates).isoformat() == "2027-12-10"

    def test_bls_redesign_reports_error_not_empty_success(self):
        from economic_calendar_watch import parse_bls_schedule
        dates, err = parse_bls_schedule(REDESIGNED_HTML)
        assert dates == []
        assert err and "改版" in err

    def test_bea_reads_reference_quarter_not_release_date(self):
        """BEA 的日期格没有年份，跨年会歧义；季度自带年份"""
        from economic_calendar_watch import parse_bea_advance_quarters
        quarters, err = parse_bea_advance_quarters(BEA_HTML)
        assert err is None
        assert quarters == [(2026, 3), (2026, 4)]

    def test_bea_ignores_second_and_third_estimates(self):
        from economic_calendar_watch import parse_bea_advance_quarters
        quarters, _ = parse_bea_advance_quarters(BEA_HTML)
        assert (2026, 3) in quarters
        assert len(quarters) == 2      # Second Estimate 那行不算

    def test_bea_redesign_reports_error(self):
        from economic_calendar_watch import parse_bea_advance_quarters
        quarters, err = parse_bea_advance_quarters(REDESIGNED_HTML)
        assert quarters == []
        assert err

    @pytest.mark.parametrize("iso,expected", [
        ("2026-01-29", (2025, 4)),
        ("2026-04-29", (2026, 1)),
        ("2026-07-29", (2026, 2)),
        ("2026-10-29", (2026, 3)),
        ("2026-05-15", None),        # 不在 1/4/7/10 → 口径变了，判无法判定
    ])
    def test_release_month_to_reference_quarter(self, iso, expected):
        from economic_calendar_watch import _release_date_to_quarter
        assert _release_date_to_quarter(date.fromisoformat(iso)) == expected


@pytest.fixture
def tmp_state(tmp_path):
    return tmp_path / "watch_state.json"


def _stub_fetch(mapping, default=REDESIGNED_HTML):
    """按 URL 关键字返回样本页面"""
    def _f(url, timeout, ua):
        for kw, html in mapping.items():
            if kw in url:
                return html
        return default
    return _f


class TestCheckSemantics:
    """核心不变式：看不出来 ≠ 没有新的"""

    def test_unreachable_source_is_undeterminable_never_healthy(
            self, monkeypatch, tmp_state):
        import economic_calendar_watch as w

        def _boom(url, timeout, ua):
            raise OSError("connection refused")
        monkeypatch.setattr(w, "_fetch", _boom)

        res = w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        assert set(res["undeterminable_tables"]) == {"fomc", "cpi", "nfp", "gdp"}
        assert res["action_required"] is False, "抓不到不等于「上游没有新日程」"
        for v in res["upstream"].values():
            assert v["determinable"] is False
            assert v["new_available"] is False

    def test_redesigned_page_is_undeterminable_not_healthy(
            self, monkeypatch, tmp_state):
        """页面改版 → 解析 0 条。若报成「无新日程」，日历会一路静默走到见底。"""
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({}))     # 全部返回改版页
        res = w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        assert set(res["undeterminable_tables"]) == {"fomc", "cpi", "nfp", "gdp"}
        assert res["action_required"] is False

    def test_too_few_parsed_dates_is_undeterminable(self, monkeypatch, tmp_state):
        """解析出的条目数低于下限 → 判无法判定，不能拿残缺结果下结论"""
        import economic_calendar_watch as w
        truncated = BLS_HTML.replace(
            "<tr><td>February 2027</td><td>Mar. 10, 2027</td><td>08:30 AM</td></tr>", "")
        for _ in range(3):      # 再删几行，压到 12 条以下
            truncated = truncated.replace(
                truncated[truncated.find("<tr><td>March 2027"):
                          truncated.find("</tr>", truncated.find("<tr><td>March 2027")) + 5],
                "", 1)
        monkeypatch.setattr(w, "_fetch", _stub_fetch({"cpi.htm": truncated}))
        res = w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        assert "cpi" in res["undeterminable_tables"]
        assert res["upstream"]["cpi"]["new_available"] is False

    def test_new_upstream_schedule_sets_action_required(self, monkeypatch, tmp_state):
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({
            "cpi.htm": BLS_HTML, "empsit.htm": BLS_HTML,
            "fomccalendars": FOMC_HTML, "bea.gov": BEA_HTML,
        }))
        res = w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        assert res["action_required"] is True
        assert "cpi" in res["new_schedule_tables"]
        assert "2027-12-10" in res["upstream"]["cpi"]["new_items"]
        # GDP：上游已排到 2026Q4，我们只到 2026Q3
        assert res["upstream"]["gdp"]["new_available"] is True
        assert res["upstream"]["gdp"]["new_items"] == ["2026Q4"]

    def test_watcher_never_mutates_the_calendar_tables(self, monkeypatch, tmp_state):
        """🔴 监视器只报告，绝不代抄 —— 否则等于把「推算代替抄写」自动化了"""
        import economic_calendar_watch as w
        from economic_calendar import _CPI, _FOMC, _NFP, _GDP
        before = (list(_CPI), list(_FOMC), list(_NFP), list(_GDP))
        monkeypatch.setattr(w, "_fetch", _stub_fetch({
            "cpi.htm": BLS_HTML, "empsit.htm": BLS_HTML,
            "fomccalendars": FOMC_HTML, "bea.gov": BEA_HTML,
        }))
        w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        from economic_calendar import _CPI as a, _FOMC as b, _NFP as c, _GDP as d
        assert (a, b, c, d) == before


class TestThrottle:
    def test_skips_network_within_window(self, monkeypatch, tmp_state):
        import economic_calendar_watch as w
        calls = []

        def _counting(url, timeout, ua):
            calls.append(url)
            return REDESIGNED_HTML
        monkeypatch.setattr(w, "_fetch", _counting)

        tmp_state.write_text(json.dumps({
            "last_checked_at": "2026-08-27T09:00:00",
            "upstream": {}, "action_required": False,
        }), encoding="utf-8")

        res = w.check(state_path=tmp_state, today=date(2026, 8, 29))
        assert res["throttled"] is True
        assert res["network_checked_this_run"] is False
        assert calls == []

    def test_pending_action_defeats_throttle(self, monkeypatch, tmp_state):
        """上次查出有新日程但人还没抄 → 每天都要再提醒，不许被节流吃掉"""
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({"cpi.htm": BLS_HTML}))
        tmp_state.write_text(json.dumps({
            "last_checked_at": "2026-08-28T09:00:00",
            "upstream": {}, "action_required": True,
        }), encoding="utf-8")

        res = w.check(state_path=tmp_state, today=date(2026, 8, 29))
        assert res["throttled"] is False
        assert res["network_checked_this_run"] is True

    def test_force_ignores_throttle(self, monkeypatch, tmp_state):
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({}))
        tmp_state.write_text(json.dumps({
            "last_checked_at": "2026-08-29T09:00:00",
            "upstream": {}, "action_required": False,
        }), encoding="utf-8")
        res = w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        assert res["throttled"] is False


class TestHealthLabelling:
    def test_binding_and_shortest_numbers_never_get_crossed(
            self, monkeypatch, tmp_state):
        """🔴 回归：binding_table 与 shortest_table 常非同一张表（阈值逐表不同）。

        把甲的表名和乙的天数拼进同一句话，就是本项目反复在治的「列名说谎」。
        """
        import economic_calendar_watch as w
        from economic_calendar import get_calendar_health
        monkeypatch.setattr(w, "_fetch", _stub_fetch({}))

        today = date(2026, 8, 29)
        res = w.check(force=True, state_path=tmp_state, today=today)
        h = res["calendar_health"]
        truth = get_calendar_health(ref_date=today)

        assert h["binding_table"] != h["shortest_table"], (
            "本断言依赖 2026-08-29 时两者不同（nfp vs gdp）；若表结构变了请重设计"
        )
        assert h["binding_last_date"] == truth["per_table"][h["binding_table"]]["last_date"]
        assert h["binding_horizon_days"] == truth["per_table"][h["binding_table"]]["horizon_days"]
        assert h["shortest_last_date"] == truth["per_table"][h["shortest_table"]]["last_date"]
        assert h["shortest_horizon_days"] == truth["per_table"][h["shortest_table"]]["horizon_days"]

    def test_render_mentions_both_tables_when_they_differ(
            self, monkeypatch, tmp_state):
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({}))
        res = w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        text = w._render(res)
        assert res["calendar_health"]["binding_table"] in text
        assert res["calendar_health"]["shortest_table"] in text


class TestUserAgent:
    def test_default_ua_carries_no_personal_email(self, monkeypatch):
        """bls.gov 只校验 UA 里有没有邮箱格式的 token，不校验地址。
        默认值因此用占位域，不把私人邮箱写进仓库或每周发给 BLS。"""
        import economic_calendar_watch as w
        monkeypatch.delenv("ALPHA_HIVE_CONTACT_EMAIL", raising=False)
        ua = w._user_agent()
        assert "@" in ua, "没有邮箱形状的 token 会被 bls.gov 403"
        assert "gmail.com" not in ua and "wangmingjie" not in ua

    def test_env_override(self, monkeypatch):
        import economic_calendar_watch as w
        monkeypatch.setenv("ALPHA_HIVE_CONTACT_EMAIL", "ops@example.com")
        assert "ops@example.com" in w._user_agent()
