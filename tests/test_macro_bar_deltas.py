"""宏观条涨跌小字（v0.45.352，重做 v0.45.78 的意图）

v0.45.78（`6024fbf`，从未合并）的做法是「今天的 `vix` 减上一份日报里的 `vix`」。
拿 09-10 ~ 09-25 的真实日报一核，这个做法会造出看起来真实的假数：

  · 日报里的 `vix` 不带观测日。CBOE 缓存陈旧时相邻两份日报读到同一个收盘
    （09-24 / 09-25 都是 **09-22** 的 14.21）⇒ 相减得一个假「+0.0」。
  · `vix` 本身是 CBOE 的**上一交易日**收盘（CSV 在 17:00 ET 扫描时还没更新当日），
    而现成的 `vix_change_pct` 来自 yfinance、是**当日**的（09-11：vix 17.84 = CBOE 09-10 收盘，
    vix_change_pct −11.21% = 09-11 当日 15.84/17.84−1）。两者拼起来错位一天。
  · 10Y 在兜底日是常量 4.5（没有日期）；中间缺一份日报时，「上一份」可能是几天前。

所以本版的判据是：**每个涨跌都必须能说出它是哪两次观测之差，说不出就不显示**；
且三项统一只表示「较前一交易日」。本文件锁住这条，并锁住它在真实数据形态上的结果。
"""

import json

import pytest

import dashboard_renderer as dr
import fred_macro as fm


# ────────── 真实日报里的宏观形态（取自 ~/alpha-hive-data 的 09-10/11/18/22/24/25 日报，只保留用到的键）──────────

def _live(date, vix, tnx, gld, gld_chg, **extra):
    d = {"vix": vix, "vix_source": "cboe", "treasury_10y": tnx, "gold_price": gld,
         "gold_change_pct": gld_chg, "data_source": "treasury+finnhub+yfinance+fred",
         "field_sources": {"TNX": f"treasury_gov@{date}", "GLD": "finnhub:GLD"}}
    d.update(extra)
    return d


M_0910 = _live("2026-09-10", 16.46, 4.95, 396.36, -1.73)
M_0911 = _live("2026-09-11", 17.84, 4.96, 398.77, 0.61)
M_0918 = _live("2026-09-18", 15.44, 5.01, 401.17, 0.71)
M_0922 = _live("2026-09-22", 14.87, 4.96, 400.07, 0.42)
# 09-24 / 09-25：yfinance 全灭，只有 VIX 来自（陈旧的）CBOE 缓存，其余是兜底常量
M_FALLBACK = {"vix": 14.21, "vix_source": "cboe", "treasury_10y": 4.5, "gold_price": None,
              "gold_change_pct": 0.0, "data_source": "fallback"}
# 本版 fred_macro 新增的三个键（旧日报没有 ⇒ 旧日报上 VIX 不显示涨跌，这是对的）
VIX_DATED_0911 = {"vix_as_of": "2026-09-10", "vix_prev_close": 16.46, "vix_prev_as_of": "2026-09-09"}


def _cls_and_text(span):
    import re
    m = re.fullmatch(r'<span class="ah-macro-delta ?(\w*)" title="[^"]*">([^<]*)</span>', span)
    assert m, span
    return m.group(1), m.group(2)


# ────────── A. 真实数据回放 ──────────

class TestRealReportReplay:

    def test_normal_day(self):
        out = dr._macro_deltas({**M_0911, **VIX_DATED_0911}, M_0910, "2026-09-10", "2026-09-11")
        assert _cls_and_text(out["vix"]) == ("up", "+1.4")        # 17.84 − 16.46
        assert "2026-09-10" in out["vix"] and "2026-09-09" in out["vix"]
        assert _cls_and_text(out["10y"]) == ("up", "+0.01")       # 4.96 − 4.95
        assert "2026-09-10" in out["10y"]
        assert _cls_and_text(out["gld"]) == ("up", "+0.6%")

    def test_old_reports_without_vix_dates_show_no_vix_delta(self):
        """v0.45.352 之前的日报没有 vix_as_of —— 宁可不显示，也不跨日报相减。"""
        assert dr._macro_deltas(M_0911, M_0910, "2026-09-10", "2026-09-11")["vix"] == ""

    def test_gap_between_reports_is_not_passed_off_as_daily(self):
        """09-22 的上一份日报是 09-18（09-21 没有）——两日变化不能冒充日环比。"""
        out = dr._macro_deltas(M_0922, M_0918, "2026-09-18", "2026-09-22")
        assert out["10y"] == ""
        assert out["gld"] != ""          # 黄金自带前收盘，不依赖上一份日报

    @pytest.mark.parametrize("cur,prev,cur_date", [
        (M_FALLBACK, M_FALLBACK, "2026-09-25"),
        (M_FALLBACK, M_0922, "2026-09-24"),
    ])
    def test_fallback_days_show_nothing(self, cur, prev, cur_date):
        """v0.45.78 的做法在 09-25 会显示 VIX「+0.0」（两天读到同一个 09-22 收盘）。"""
        assert dr._macro_deltas(cur, prev, "x", cur_date) == {"vix": "", "10y": "", "gld": ""}


# ────────── B. VIX ──────────

class TestVixDelta:

    BASE = {**M_0911, **VIX_DATED_0911}

    @pytest.mark.parametrize("src", ["fallback", "yfinance", "cloud_snapshot_cboe", None])
    def test_only_live_cboe_is_trusted(self, src):
        assert dr._macro_deltas({**self.BASE, "vix_source": src}, None, None, "2026-09-11")["vix"] == ""

    @pytest.mark.parametrize("patch", [
        {"vix_prev_close": None},
        {"vix_prev_as_of": None},
        {"vix_as_of": None},
        {"vix_prev_as_of": "2026-09-10"},                 # 同一次观测
        {"vix_prev_as_of": "2026-09-11"},                 # 顺序反了
        {"vix_prev_close": float("nan")},
    ])
    def test_needs_two_dated_observations(self, patch):
        assert dr._macro_deltas({**self.BASE, **patch}, None, None, "2026-09-11")["vix"] == ""

    @pytest.mark.parametrize("as_of,report,shown", [
        ("2026-09-11", "2026-09-11", True),    # CSV 已更新当日
        ("2026-09-10", "2026-09-11", True),    # 常态：落后一个交易日
        ("2026-09-09", "2026-09-11", False),   # 陈旧缓存
        ("2026-09-11", "2026-09-14", True),    # 周一：前一交易日是上周五
        ("2026-09-04", "2026-09-08", True),    # 劳动节次日：前一交易日是 09-04
        ("2026-09-12", "2026-09-11", False),   # 比报告日还新（补跑时拿了今天的 CBOE）
        ("2026-09-10", "not-a-date", False),   # 判不出窗口 ⇒ 不显示
    ])
    def test_freshness_window(self, as_of, report, shown):
        mctx = {**self.BASE, "vix_as_of": as_of, "vix_prev_as_of": "2026-09-01"}
        assert (dr._macro_deltas(mctx, None, None, report)["vix"] != "") is shown


# ────────── C. 10Y ──────────

class TestTenYearDelta:

    @pytest.mark.parametrize("prev", [
        None,
        M_FALLBACK,                                                   # 兜底常量 4.5，无日期
        {**M_0910, "field_sources": {"TNX": ""}},                     # 走了 yfinance（无日期）
        {**M_0910, "field_sources": {"TNX": "treasury_gov@2026-09-11"}},   # 同一次观测
        {**M_0910, "treasury_10y": float("nan")},
    ])
    def test_hidden_without_a_distinct_dated_base(self, prev):
        assert dr._macro_deltas(M_0911, prev, "2026-09-10", "2026-09-11")["10y"] == ""

    def test_hidden_when_today_is_not_a_treasury_observation(self):
        cur = {**M_0911, "field_sources": {"TNX": ""}}
        assert dr._macro_deltas(cur, M_0910, "2026-09-10", "2026-09-11")["10y"] == ""

    def test_hidden_when_today_treasury_is_stale(self):
        cur = {**M_0911, "field_sources": {"TNX": "treasury_gov@2026-09-09"}}
        prev = {**M_0910, "field_sources": {"TNX": "treasury_gov@2026-09-08"}}
        assert dr._macro_deltas(cur, prev, "2026-09-10", "2026-09-11")["10y"] == ""


# ────────── D. 黄金 ──────────

class TestGoldDelta:

    def test_default_zero_without_price_is_not_an_observation(self):
        """GLD 没取到时 gold_change_pct 是默认 0.0 —— 显示「+0.0%」就是编数。"""
        cur = {**M_0911, "gold_price": None, "gold_change_pct": 0.0}
        assert dr._macro_deltas(cur, None, None, "2026-09-11")["gld"] == ""

    def test_down_day(self):
        assert _cls_and_text(dr._macro_deltas(M_0910, None, None, "2026-09-10")["gld"]) == ("dn", "-1.7%")


# ────────── E. 小字本身 ──────────

class TestDeltaSpan:

    @pytest.mark.parametrize("delta,nd,cls,text", [
        (0.06, 1, "up", "+0.1"),
        (0.04, 1, "", "+0.0"),      # 显示成 +0.0 就不能标绿
        (-0.04, 1, "", "+0.0"),     # 也不能显示「-0.0」
        (-0.05, 2, "dn", "-0.05"),
        (0.0, 2, "", "+0.00"),
    ])
    def test_class_follows_the_displayed_value(self, delta, nd, cls, text):
        assert _cls_and_text(dr._macro_delta_span(delta, nd, "", "t")) == (cls, text)

    def test_title_is_escaped(self):
        assert 'title="a&quot;&lt;b"' in dr._macro_delta_span(1.0, 1, "", 'a"<b')


# ────────── F. 往日日报的资格（分数变化与宏观涨跌共用）──────────

class TestIterPrevReports:

    @pytest.fixture
    def rdir(self, tmp_path):
        for name in ["2026-09-11", "2026-09-10", "2026-09-10 2",   # iCloud 重名副本
                     "2026-09-09", "2026-09-06",                   # 09-06 是周日
                     "2026-09-14"]:                                # 比报告日晚
            (tmp_path / f"alpha-hive-daily-{name}.json").write_text("{}", encoding="utf-8")
        return tmp_path

    def test_order_and_filters(self, rdir):
        got = [d for d, _ in dr._iter_prev_reports(rdir, "2026-09-11")]
        assert got == ["2026-09-10", "2026-09-09"]

    def test_later_reports_are_not_yesterday_when_backfilling(self, rdir):
        """旧循环只跳过 == date_str：补跑 09-11 时会拿 09-14 当「昨天」。"""
        assert "2026-09-14" not in [d for d, _ in dr._iter_prev_reports(rdir, "2026-09-11")]

    def test_accepts_str_report_dir(self, rdir):
        assert [d for d, _ in dr._iter_prev_reports(str(rdir), "2026-09-11")][:1] == ["2026-09-10"]


# ────────── G. fred_macro 产出这三个键 ──────────

class TestFredMacroVixDates:

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, stub_yfinance, stub_cboe_vix, stub_fred):
        fm.set_macro_snapshot(None)
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        monkeypatch.setattr(fm, "_asof_history", lambda *a, **k: None)
        yield
        fm.set_macro_snapshot(None)
        fm._CACHE, fm._CACHE_TS = {}, 0.0

    def _cboe(self, monkeypatch, rows):
        import cboe_vix
        monkeypatch.setattr(cboe_vix, "get_vix_history",
                            lambda max_days=None, force_refresh=False: rows[-max_days:] if max_days else rows)

    def test_live_cboe_carries_both_dates(self, monkeypatch):
        self._cboe(monkeypatch, [("2026-09-08", 15.72), ("2026-09-09", 16.46), ("2026-09-10", 17.84)])
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (
            {"TNX": {"last": 4.96, "prev": 4.96, "change_pct": 0.0}},
            {"TNX": "treasury_gov@2026-09-11"}))
        r = fm.get_macro_context()
        assert (r["vix"], r["vix_source"]) == (17.84, "cboe")
        assert (r["vix_as_of"], r["vix_prev_close"], r["vix_prev_as_of"]) == ("2026-09-10", 16.46, "2026-09-09")

    def test_partial_fallback_path_carries_them_too(self, monkeypatch):
        """09-24 / 09-25 那种：其余全灭、VIX 仍来自 CBOE。"""
        self._cboe(monkeypatch, [("2026-09-21", 14.87), ("2026-09-22", 14.21)])
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: ({}, {}))
        r = fm.get_macro_context()
        assert r["data_source"] == "fallback" and r["vix_source"] == "cboe"
        assert (r["vix_as_of"], r["vix_prev_close"]) == ("2026-09-22", 14.87)

    def test_single_row_has_no_prev(self, monkeypatch):
        self._cboe(monkeypatch, [("2026-09-10", 17.84)])
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: ({}, {}))
        r = fm.get_macro_context()
        assert r["vix_as_of"] == "2026-09-10" and r["vix_prev_close"] is None

    def test_full_fallback_keeps_the_keys_as_none(self, monkeypatch):
        self._cboe(monkeypatch, [])
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: ({}, {}))
        r = fm.get_macro_context()
        assert r["vix_source"] == "fallback"
        assert {k: r[k] for k in ("vix_as_of", "vix_prev_close", "vix_prev_as_of")} == \
            {"vix_as_of": None, "vix_prev_close": None, "vix_prev_as_of": None}

    def test_snapshot_vix_has_no_prev(self, monkeypatch):
        """补跑走快照时没有前一收盘 ⇒ 仪表板不显示 VIX 涨跌（而不是拿今天的 CBOE 凑）。"""
        self._cboe(monkeypatch, [("2026-09-26", 20.0), ("2026-09-27", 21.0)])
        monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (
            {"TNX": {"last": 4.67, "prev": 4.67, "change_pct": 0.0}}, {"TNX": "treasury_gov@2026-08-27"}))
        fm.set_macro_snapshot("2026-08-27", {"cboe": {"vix_term": {"vix_spot": 15.21}}})
        r = fm.get_macro_context()
        assert r["vix_source"] == "cloud_snapshot_cboe" and r["vix"] == 15.21
        assert r["vix_prev_close"] is None and r["vix_as_of"] is None


# ────────── H. 接到页面上 ──────────

class TestRenderedMacroBar:

    @pytest.fixture(autouse=True)
    def _offline(self, stub_yfinance, stub_cboe_vix):
        pass

    def _render(self, monkeypatch, tmp_path, mctx, prev=None):
        monkeypatch.setattr(fm, "get_macro_context", lambda: mctx)
        if prev is not None:
            (tmp_path / "alpha-hive-daily-2026-09-10.json").write_text(
                json.dumps({"macro_context": prev}), encoding="utf-8")
        html = dr.render_dashboard_html(report={"opportunities": []}, date_str="2026-09-11",
                                        report_dir=tmp_path, opportunities=[])
        return html.split('<div class="ah-macro-track">', 1)[1].split('<ul class="sr-only', 1)[0]

    def test_all_three_deltas_in_every_copy(self, monkeypatch, tmp_path):
        track = self._render(monkeypatch, tmp_path, {**M_0911, **VIX_DATED_0911}, prev=M_0910)
        copies = track.count('<div class="ah-macro-items')
        assert copies >= 8
        assert track.count('class="ah-macro-delta') == 3 * copies
        assert "$399" in track, "黄金主值应固定显示价格（v0.45.78 的意图）"
        assert ">+1.4<" in track and ">+0.01<" in track and ">+0.6%<" in track

    def test_fallback_day_renders_no_delta(self, monkeypatch, tmp_path):
        track = self._render(monkeypatch, tmp_path, M_FALLBACK, prev=M_FALLBACK)
        assert "ah-macro-delta" not in track
