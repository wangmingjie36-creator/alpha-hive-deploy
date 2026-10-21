"""
Tests for economic_calendar_watch —— 上游宏观日程发布监视器（v0.45.67）

全部离线：`_fetch` 被 monkeypatch 掉，不打网络。
本文件守的核心不变式只有一条：**「抓不到 / 看不懂」绝不能被报成「没有新日程」。**
一个静默失败的监视器，就是它要治的那个 bug 升了一级。
"""

import json
import pytest
from datetime import date


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
    <td>GDP (Advance Estimate), 4th Quarter and Year 2026</td></tr>
</table></body></html>
"""

REDESIGNED_HTML = "<html><body><h1>We have moved</h1><p>Nothing here.</p></body></html>"

# ── BEA 两页（v0.45.574）：结构照抄 2026-10-21 的真实页面——表头单元格 `Year YYYY`，日期格不带年份，
#    Q4 那行写作「4th Quarter and Year」──

# 滚动页 10-21 的样子：只列当年剩下的发布（真实页面只到 12-23），唯一一条初值是 Q3
BEA_ROLLING_HTML = """
<html><body><table id="release-schedule-table">
<thead><tr><th>Year 2026</th><th></th><th>Release</th></tr></thead><tbody>
<tr><td><div class="release-date">October 29</div><small>8:30 AM</small></td>
    <td>GDP (Advance Estimate), 3rd Quarter 2026</td></tr>
<tr><td><div class="release-date">November 25</div><small>8:30 AM</small></td>
    <td>GDP (Second Estimate) and Corporate Profits, 3rd Quarter 2026</td></tr>
<tr><td><div class="release-date">December 23</div><small>8:30 AM</small></td>
    <td>GDP (Third Estimate), Industries, Corporate Profits, State GDP, and State Personal Income, 3rd Quarter 2026</td></tr>
</tbody></table></body></html>
"""

# 滚动页过了 10 月末的 Q3 初值：当年只剩二次 / 三次估计，一条初值行都没有——合法状态，不是改版
BEA_ROLLING_NO_ADVANCE_HTML = BEA_ROLLING_HTML.replace(
    """<tr><td><div class="release-date">October 29</div><small>8:30 AM</small></td>
    <td>GDP (Advance Estimate), 3rd Quarter 2026</td></tr>
""", "")

# 次年页 10-21 的样子：Year 2027，四行初值（Q4 2026 + Q1–Q3 2027）
BEA_NEXT_YEAR_HTML = """
<html><body><table id="release-schedule-table">
<thead><tr><th>Year 2027</th><th></th><th>Release</th></tr></thead><tbody>
<tr><td><div class="release-date">January 7</div><small>8:30 AM</small></td>
    <td>U.S. International Trade in Goods and Services, November 2026</td></tr>
<tr><td><div class="release-date">January 28</div><small>8:30 AM</small></td>
    <td>GDP (Advance Estimate), 4th Quarter and Year 2026</td></tr>
<tr><td><div class="release-date">April 29</div><small>8:30 AM</small></td>
    <td>GDP (Advance Estimate), 1st Quarter 2027</td></tr>
<tr><td><div class="release-date">July 29</div><small>8:30 AM</small></td>
    <td>GDP (Advance Estimate), 2nd Quarter 2027</td></tr>
<tr><td><div class="release-date">October 28</div><small>8:30 AM</small></td>
    <td>GDP (Advance Estimate), 3rd Quarter 2027</td></tr>
</tbody></table></body></html>
"""

# BEA 还没发次年日程：表头在、表里一条发布都没有。⚠️ 未发布时真实页面长什么样**没观测过**——
# 这是本版的约定（见 parse_bea_next_year）：有表头的空表 = 还没发；没表头 = 看不懂
BEA_NEXT_YEAR_NOT_POSTED_HTML = """
<html><body><table id="release-schedule-table">
<thead><tr><th>Year 2027</th><th></th><th>Release</th></tr></thead><tbody></tbody>
</table></body></html>
"""

# 次年页列了发布却没有一条 GDP 初值（标题改版的样子）
BEA_NEXT_YEAR_NO_GDP_HTML = """
<html><body><table id="release-schedule-table">
<thead><tr><th>Year 2027</th><th></th><th>Release</th></tr></thead><tbody>
<tr><td><div class="release-date">January 7</div></td>
    <td>U.S. International Trade in Goods and Services, November 2026</td></tr>
<tr><td><div class="release-date">January 29</div></td>
    <td>Personal Income and Outlays, December 2026</td></tr>
</tbody></table></body></html>
"""

# 次年页的初值行换了季度写法（读不出季度）——不能当它不存在
BEA_NEXT_YEAR_UNREADABLE_HTML = BEA_NEXT_YEAR_HTML.replace(
    "GDP (Advance Estimate), 1st Quarter 2027", "GDP (Advance Estimate), First Quarter 2027")


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

    @pytest.mark.parametrize("title", [
        "GDP (Advance Estimate), 4th Quarter and Year 2026",   # BEA 真实写法（10-21 次年页）
        "GDP (Advance Estimate), 4th Quarter 2026",
    ])
    def test_bea_q4_row_with_and_year_is_read(self, title):
        """🔴 回归（v0.45.574）：Q4 初值行写作「4th Quarter and Year」，旧正则要求 `Quarter 2026` 紧挨着 ⇒
        每年的 Q4 初值都读不到，10-21 次年页 4 行只认出 3 行"""
        from economic_calendar_watch import parse_bea_advance_quarters
        html = f"<table><tr><td>January 28</td><td>{title}</td></tr></table>"
        assert parse_bea_advance_quarters(html) == ([(2026, 4)], None)

    def test_bea_rolling_page_with_header_and_no_advance_rows_is_valid(self):
        """滚动页只列当年剩下的发布：10 月末的 Q3 初值一过就一条都没有。有表头 ⇒ 是合法的「0 条」，不是改版"""
        from economic_calendar_watch import parse_bea_advance_quarters
        assert parse_bea_advance_quarters(BEA_ROLLING_NO_ADVANCE_HTML) == ([], None)

    def test_bea_unreadable_advance_row_is_error_not_absence(self):
        from economic_calendar_watch import parse_bea_advance_quarters
        quarters, err = parse_bea_advance_quarters(BEA_NEXT_YEAR_UNREADABLE_HTML)
        assert quarters == [] and err and "First Quarter 2027" in err

    def test_bea_next_year_page(self):
        from economic_calendar_watch import parse_bea_next_year
        assert parse_bea_next_year(BEA_NEXT_YEAR_HTML) == (
            2027, [(2026, 4), (2027, 1), (2027, 2), (2027, 3)], None)

    def test_bea_next_year_header_with_empty_table_means_not_posted_yet(self):
        from economic_calendar_watch import parse_bea_next_year
        assert parse_bea_next_year(BEA_NEXT_YEAR_NOT_POSTED_HTML) == (2027, [], None)

    @pytest.mark.parametrize("html,fragment", [
        (REDESIGNED_HTML, "表头"),
        (BEA_NEXT_YEAR_NO_GDP_HTML, "没有一条 GDP 初值行"),
        (BEA_NEXT_YEAR_UNREADABLE_HTML, "读不出参考季度"),
    ])
    def test_bea_next_year_unreadable_shapes_are_errors(self, html, fragment):
        from economic_calendar_watch import parse_bea_next_year
        _, quarters, err = parse_bea_next_year(html)
        assert quarters == [] and err and fragment in err

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


@pytest.fixture(autouse=True)
def _frozen_calendar(monkeypatch):
    """本文件的场景（「我们只核到 2026-12、上游已有 2027」「2026-09-20 本地 stale」）都是照
    抄录 2027 之前的表尾 / verified_through 挑的 ⇒ 钉在冻结形状上（v0.45.572，见 tests/_frozen_calendar.py）。
    否则每抄进一年新日程，这里就有一批场景失效。"""
    from tests._frozen_calendar import freeze_calendar
    freeze_calendar(monkeypatch)
    yield


@pytest.fixture
def tmp_state(tmp_path):
    return tmp_path / "watch_state.json"


def _stub_fetch(mapping, default=REDESIGNED_HTML):
    """按 URL 关键字返回样本页面；几个关键字同时命中时取最长的那个
    （BEA 次年页的 URL 同时含 `bea.gov` 与 `next-year`，v0.45.574）"""
    def _f(url, timeout, ua):
        hits = [kw for kw in mapping if kw in url]
        return mapping[max(hits, key=len)] if hits else default
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
            "next-year": BEA_NEXT_YEAR_NOT_POSTED_HTML,
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
        import economic_calendar
        import economic_calendar_watch as w
        from pathlib import Path
        from economic_calendar import _CPI, _FOMC, _NFP, _GDP
        before = (list(_CPI), list(_FOMC), list(_NFP), list(_GDP))
        src = Path(economic_calendar.__file__)
        src_before = src.read_bytes()
        monkeypatch.setattr(w, "_fetch", _stub_fetch({
            "cpi.htm": BLS_HTML, "empsit.htm": BLS_HTML,
            "fomccalendars": FOMC_HTML, "bea.gov": BEA_HTML,
            "next-year": BEA_NEXT_YEAR_HTML,     # 走一遍「次年页查出新 GDP 季度」的路径（v0.45.574）
        }))
        res = w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        assert res["upstream"]["gdp"]["new_items"], "本用例要走「查出新 GDP 季度」的路径，前提没接上"
        from economic_calendar import _CPI as a, _FOMC as b, _NFP as c, _GDP as d
        assert (a, b, c, d) == before
        assert src.read_bytes() == src_before, "监视器改了 economic_calendar.py 本身"


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


# HTML 样本：页面 <title> 与表头同文，且导航区含一个会被日期正则误吃的行
BLS_HTML_WITH_NAV_DATE = """
<html><head><title>Schedule of Releases for the Consumer Price Index</title></head>
<body>
<h1>Schedule of Releases for the Consumer Price Index</h1>
<nav><ul><li>Prior Years</li>
  <li>Last Modified Date:</li><li>Jan. 05, 2099</li></ul></nav>
""" + BLS_HTML[BLS_HTML.index("<h1>"):]


class TestSecondPassRegressions:
    """v0.45.68 二次检查抓到的四条，逐条钉死"""

    def test_throttled_with_empty_cache_is_undeterminable_not_healthy(self, tmp_state):
        """🔴 回归：节流 + 缓存里没有任何源的结果

        旧实现直接拿缓存的空 dict 当结论：undeterminable=[]、new=[]
        → 报「✅ 上游无新日程」。监视器自己犯了它要治的那个静默失败。
        """
        import economic_calendar_watch as w
        tmp_state.write_text(json.dumps({
            "last_checked_at": "2026-08-28T09:00:00",
            "upstream": {},                       # 从未成功查过任何源
            "action_required": False,
        }), encoding="utf-8")

        res = w.check(state_path=tmp_state, today=date(2026, 8, 29))
        assert res["throttled"] is True
        assert res["network_checked_this_run"] is False
        assert set(res["undeterminable_tables"]) == {"fomc", "cpi", "nfp", "gdp"}
        assert res["upstream_conclusive"] is False
        for v in res["upstream"].values():
            assert v["determinable"] is False
            assert "从未被成功检查过" in v["reason"]

    def test_throttled_with_partial_cache_marks_only_missing_as_undeterminable(
            self, monkeypatch, tmp_state):
        """缓存里只有一部分源 → 只有缺的那些判无法判定，已有的照旧可用"""
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({}))
        tmp_state.write_text(json.dumps({
            "last_checked_at": "2026-08-28T09:00:00",
            "upstream": {"fomc": {"source": "x", "determinable": True,
                                  "new_available": False, "reason": None,
                                  "upstream_latest": "2028-01-26",
                                  "our_verified_through": "2028-01-26"}},
            "action_required": False,
        }), encoding="utf-8")

        res = w.check(state_path=tmp_state, today=date(2026, 8, 29))
        assert res["upstream"]["fomc"]["determinable"] is True
        assert set(res["undeterminable_tables"]) == {"cpi", "nfp", "gdp"}

    def test_never_claims_no_new_schedules_when_sources_unreachable(self, tmp_state):
        """🔴 回归：本地 stale + 四源全抓不到时，不得断言「上游暂无新日程」

        退出码优先级是 action > health > undetermined，所以这一格走 health 分支。
        旧版在这里无条件打「上游暂无新日程」—— 一句毫无依据的断言，
        而且把四个源全部抓取失败整个吃掉，恰好抵消掉监视器存在的意义。
        """
        import economic_calendar_watch as w

        def _boom(url, timeout, ua):
            raise OSError("network unreachable")
        w_fetch_backup = w._fetch
        try:
            w._fetch = _boom
            res = w.check(force=True, state_path=tmp_state, today=date(2026, 9, 20))
        finally:
            w._fetch = w_fetch_backup

        assert res["calendar_health"]["status"] == "stale"
        assert res["upstream_conclusive"] is False
        assert len(res["undeterminable_tables"]) == 4

        # 复刻 main() 的文案分支
        undet = res["undeterminable_tables"]
        note = ("上游暂无新日程" if res["upstream_conclusive"]
                else f"⚠️ 上游 {len(undet)} 个源无法判定（{'、'.join(undet)}）")
        assert "暂无新日程" not in note, "在一个源都没查到的情况下断言了上游没有新日程"
        assert "无法判定" in note

    def test_upstream_conclusive_true_only_when_every_source_parsed(
            self, monkeypatch, tmp_state):
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({
            "cpi.htm": BLS_HTML, "empsit.htm": BLS_HTML,
            "fomccalendars": FOMC_HTML, "bea.gov": BEA_HTML,
            "next-year": BEA_NEXT_YEAR_NOT_POSTED_HTML,
        }))
        res = w.check(force=True, state_path=tmp_state, today=date(2026, 8, 29))
        assert res["upstream_conclusive"] is True
        assert res["undeterminable_tables"] == []

    def test_bls_slice_anchors_to_real_header_not_page_title(self):
        """🔴 回归：页面 <title> 与表头同文

        旧实现用 next() 命中第 0 行的 title，把整段导航纳入切片。
        当前真实页面的导航恰好没有能被日期正则吃掉的行 —— 那是运气不是设计。
        这里放一行 `Last Modified Date: Jan. 05, 2099` 在导航里：
        旧实现会把 2099 当成上游最新日程，直接误报「上游发布新日程了」。
        """
        from economic_calendar_watch import parse_bls_schedule
        dates, err = parse_bls_schedule(BLS_HTML_WITH_NAV_DATE)
        assert err is None
        assert max(dates).year == 2027, f"导航里的 2099 被当成了日程：max={max(dates)}"
        assert len(dates) == 12


# ── v0.45.574：GDP 读滚动页 + 次年页 ──

_MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

#: 写死在测试里、不经被测模块取：URL 被改错时这里要红，而不是跟着一起错
NEXT_YEAR_URL = "https://www.bea.gov/news/schedule/next-year"


def _page(gdp_entry, name):
    """GDP entry 里某一页的读数；没读到这页 ⇒ {}（断言失败给出明确原因，而不是 KeyError）"""
    return (gdp_entry.get("pages") or {}).get(name) or {}


def _bls_page(iso_dates):
    """照真实 BLS 日程页结构造页面（发布日期列 `Mon. D, YYYY`）"""
    rows = "".join(
        f"<tr><td>ref</td><td>{_MON[d.month - 1]}. {d.day}, {d.year}</td><td>08:30 AM</td></tr>"
        for d in map(date.fromisoformat, iso_dates))
    return ("<html><body><h1>Schedule of Releases for the Employment Situation</h1><table>"
            "<tr><th>Reference Month</th><th>Release Date</th><th>Release Time</th></tr>"
            f"{rows}</table><p>Subscribe to the BLS Online Calendar</p></body></html>")


def _quiet_non_gdp_pages():
    """FOMC / CPI / NFP 三源都查通且**无新**（BLS 页取冻结表最后 12 条）——结论只由 GDP 两页决定"""
    import economic_calendar as ec
    return {"cpi.htm": _bls_page(ec._TABLE_SPECS["cpi"]["dates"][-12:]),
            "empsit.htm": _bls_page(ec._TABLE_SPECS["nfp"]["dates"][-12:]),
            "fomccalendars": FOMC_HTML}


class TestBeaNextYear:
    """🔴 回归（v0.45.574）：BEA 次年整年日程只在 `/news/schedule/next-year`。

    旧监视器只读 `/news/schedule`（当年剩下的发布）⇒ BEA 发了次年日程，它照样对 GDP 报「上游无新」（退出码 0）。
    2026-10-21 查明 2027 早挂在 next-year 页上，而监视器与 10-04 两次手工核对都记成「BEA 2027 未发布」。
    冻结日历下我们 GDP 核到 2026-10-29（2026Q3）；2026-08-29 本地四表都健康 ⇒ 退出码只由上游决定。
    """

    TODAY = date(2026, 8, 29)

    def _check(self, monkeypatch, tmp_state, rolling, next_year=None, fetch=None):
        import economic_calendar_watch as w
        pages = _quiet_non_gdp_pages()
        pages["bea.gov"] = rolling
        if next_year is not None:
            pages["next-year"] = next_year
        monkeypatch.setattr(w, "_fetch", fetch or _stub_fetch(pages))
        res = w.check(force=True, state_path=tmp_state, today=self.TODAY)
        assert res["calendar_health"]["status"] == "ok", "场景前提：本地日历健康，退出码只看上游"
        assert {k for k in ("fomc", "cpi", "nfp")
                if res["upstream"][k]["determinable"] and not res["upstream"][k]["new_available"]} \
            == {"fomc", "cpi", "nfp"}, "场景前提：GDP 以外三源都查通且无新"
        return res, w._exit_code(res)

    def test_next_year_not_posted_yet_is_healthy_not_undeterminable(self, monkeypatch, tmp_state):
        """年初 BEA 还没发次年日程：次年页有表头、表是空的 ⇒「还没发」，GDP 照常可判定（不是「看不懂」）"""
        res, rc = self._check(monkeypatch, tmp_state, BEA_ROLLING_HTML, BEA_NEXT_YEAR_NOT_POSTED_HTML)
        g = res["upstream"]["gdp"]
        assert g["determinable"] is True and g["new_available"] is False
        assert res["upstream_conclusive"] is True
        assert rc == 0
        assert _page(g, "next_year").get("quarters") == [], "次年页没被读到——GDP 结论只出自滚动页"

    def test_new_quarter_only_on_next_year_page_is_action_required(self, monkeypatch, tmp_state):
        """🔴 核心场景：滚动页只有我们已抄的 2026Q3，新季度**只**在次年页 ⇒ 必须报「要人动手」"""
        from economic_calendar_watch import parse_bea_advance_quarters
        # 前提（直接问解析器，不经本版新加的字段）：滚动页本身只有我们已抄的 2026Q3
        assert parse_bea_advance_quarters(BEA_ROLLING_HTML) == ([(2026, 3)], None)
        res, rc = self._check(monkeypatch, tmp_state, BEA_ROLLING_HTML, BEA_NEXT_YEAR_HTML)
        g = res["upstream"]["gdp"]
        assert g["new_items"] == ["2026Q4", "2027Q1", "2027Q2", "2027Q3"], \
            "次年页上的新季度没被看见——GDP 又只读了滚动页"
        assert g["determinable"] is True
        assert res["new_schedule_tables"] == ["gdp"] and res["action_required"] is True
        assert rc == 1
        # 人照着 source 去抄：必须指向次年页（10-04 的手工核对正是去错了页）
        assert g["source"] == NEXT_YEAR_URL

    @pytest.mark.parametrize("next_year,fragment", [
        (REDESIGNED_HTML, "表头"),
        (BEA_NEXT_YEAR_NO_GDP_HTML, "没有一条 GDP 初值行"),
        (BEA_NEXT_YEAR_UNREADABLE_HTML, "读不出参考季度"),
    ], ids=["no-year-header", "releases-but-no-gdp", "unreadable-advance-row"])
    def test_next_year_page_redesigned_makes_gdp_undeterminable(
            self, monkeypatch, tmp_state, next_year, fragment):
        """次年页看不懂 ⇒ GDP 判无法判定（退出码 3），绝不因为滚动页读得通就报「上游无新」"""
        res, rc = self._check(monkeypatch, tmp_state, BEA_ROLLING_HTML, next_year)
        g = res["upstream"]["gdp"]
        assert g["determinable"] is False and g["new_available"] is False
        assert "次年页" in g["reason"] and fragment in g["reason"]
        assert res["undeterminable_tables"] == ["gdp"] and res["upstream_conclusive"] is False
        assert rc == 3

    def test_next_year_fetch_failure_makes_gdp_undeterminable(self, monkeypatch, tmp_state):
        pages = _quiet_non_gdp_pages()
        pages["bea.gov"] = BEA_ROLLING_HTML
        stub = _stub_fetch(pages)

        def _fetch(url, timeout, ua):
            if url.endswith("/next-year"):
                raise OSError("HTTP/2 framing layer")
            return stub(url, timeout, ua)
        res, rc = self._check(monkeypatch, tmp_state, BEA_ROLLING_HTML, fetch=_fetch)
        g = res["upstream"]["gdp"]
        assert g["determinable"] is False
        assert "抓取失败" in g["reason"] and "次年页" in g["reason"]
        assert _page(g, "rolling").get("quarters") == ["2026Q3"], "读通的那页照样留读数，给人看"
        assert rc == 3

    def test_new_quarter_on_rolling_page_still_needs_next_year_page(self, monkeypatch, tmp_state):
        """只看半边证据不下结论：滚动页已有新季度，次年页看不懂 ⇒ 仍判无法判定（两页都查通才可判定）"""
        res, rc = self._check(monkeypatch, tmp_state, BEA_HTML, REDESIGNED_HTML)
        g = res["upstream"]["gdp"]
        assert g["determinable"] is False and rc == 3
        assert _page(g, "rolling").get("quarters") == ["2026Q3", "2026Q4"]

    def test_rolling_page_without_advance_rows_is_fine_when_next_year_has_them(
            self, monkeypatch, tmp_state):
        """10 月末 Q3 初值一过，滚动页一条初值都没有（年年如此）：只要次年页有，GDP 照常可判定。
        旧版对这页报「页面改版」⇒ 每年 10-30 起 GDP 恒「无法判定」两个月"""
        res, rc = self._check(monkeypatch, tmp_state, BEA_ROLLING_NO_ADVANCE_HTML, BEA_NEXT_YEAR_HTML)
        g = res["upstream"]["gdp"]
        assert g["determinable"] is True, g.get("reason")
        assert g["new_items"] == ["2026Q4", "2027Q1", "2027Q2", "2027Q3"]
        assert rc == 1
        assert _page(g, "rolling").get("quarters") == []

    def test_no_advance_rows_on_either_page_is_undeterminable(self, monkeypatch, tmp_state):
        """两页都合法地空着，下限「≥ 1 条」落在并集上 ⇒ 无法判定（看不出来 ≠ 没有新的）"""
        res, rc = self._check(monkeypatch, tmp_state, BEA_ROLLING_NO_ADVANCE_HTML,
                              BEA_NEXT_YEAR_NOT_POSTED_HTML)
        g = res["upstream"]["gdp"]
        assert g["determinable"] is False and "合计只解析出 0 条" in g["reason"]
        assert rc == 3

    def test_render_names_the_next_year_page(self, monkeypatch, tmp_state):
        import economic_calendar_watch as w
        res, _ = self._check(monkeypatch, tmp_state, BEA_ROLLING_HTML, BEA_NEXT_YEAR_HTML)
        text = w._render(res)
        assert "次年页 Year 2027" in text and NEXT_YEAR_URL in text


class TestGdpCacheFromBeforeNextYear:
    """v0.45.574：节流缓存里的 GDP 结论若出自只读滚动页的旧版，它的「无新」正是盲区本身 ⇒ 不节流，立即重查"""

    def _state(self, tmp_state, gdp_entry):
        tmp_state.write_text(json.dumps({
            "last_checked_at": "2026-08-27T09:00:00",
            "upstream": {"gdp": gdp_entry}, "action_required": False,
        }), encoding="utf-8")

    def test_old_gdp_cache_defeats_throttle(self, monkeypatch, tmp_state):
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({}))
        self._state(tmp_state, {"source": "https://www.bea.gov/news/schedule", "determinable": True,
                                "new_available": False, "reason": None,
                                "our_latest_quarter": "2026Q3", "upstream_latest_quarter": "2026Q3"})
        res = w.check(state_path=tmp_state, today=date(2026, 8, 29))
        assert res["throttled"] is False and res["network_checked_this_run"] is True

    def test_gdp_cache_with_next_year_page_still_throttles(self, monkeypatch, tmp_state):
        """对照：新版写的缓存（带次年页读数）照常节流——上一条红的理由只能是「缓存出自旧版」"""
        import economic_calendar_watch as w
        monkeypatch.setattr(w, "_fetch", _stub_fetch({}))
        self._state(tmp_state, {"source": "x", "determinable": True, "new_available": False,
                                "reason": None, "pages": {"rolling": {}, "next_year": {}}})
        res = w.check(state_path=tmp_state, today=date(2026, 8, 29))
        assert res["throttled"] is True and res["network_checked_this_run"] is False
