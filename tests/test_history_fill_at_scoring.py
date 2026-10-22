"""历史字段回落告警按「评分用的那份行情」判；ML 报告复用评分时的预取行情（v0.45.625）

背景（2026-10-22）
------------------
P2「历史字段回落后仍缺：volume_ratio 10 只、volatility_20d 9 只」，正文说「signal_archive 当天缺对应的 price.* 行」——
可当天入档的 price.* 30/30 齐全。原因：`data_pipeline.history_fill_stats()["still_missing"]` 记的是每只票**最近一次**
出口后的状态；评分用的数据 14:00 预取时完整，14:39 生成 ML 报告时 `_gen_one` 为 12 只票重新走一遍取数出口
（离预取已过半小时、5 分钟内存缓存早失效），撞上 14:23–14:45 的全机 TLS 断网，把 10 只盖成「仍缺」。

本文件钉住
----------
1. `data_pipeline.mark_history_fill_scoring()`：在评分那一刻拍快照，之后的出口记录改不动它；没标记过 ⇒ None，
   标记时还没解析过 ⇒ 全零（与「没标记」分开）；reset 一并清掉。
2. `alert_manager._check_history_fill`：P2 只按快照判（10-22 的形状 ⇒ 不再报 P2）；评分完整、之后的重取才缺的
   另列一条 INFO（不冒充 H1 输入缺失）；没有快照 ⇒ 退回进程口径并注明。
3. `scan_timing.summary_line` 同口径。
4. 扫描接线：`_init_scan_context` 在预取之后标记、留下预取行情；`_generate_ml_reports` 有预取价就不再取数，
   没有才单独取（行为测试，取数函数打桩计数）。
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
import types

import pytest

import alert_manager as am
import data_pipeline as dp
import scan_timing as stt


@pytest.fixture(autouse=True)
def _clean_history_fill():
    dp.reset_history_fill_stats()
    yield
    dp.reset_history_fill_stats()


def _exit(ticker, path="source:cboe", **missing):
    """模拟一次取数出口：missing 里给出的字段记成 missing，其余 present。"""
    outcome = {f: ("missing" if missing.get(f) else "present") for f in ("momentum", "volume", "volatility")}
    dp._record_history_fill(ticker, path, outcome, False)


# ══════════════════════════════════════════════════════════════════════════
# 1. 快照语义
# ══════════════════════════════════════════════════════════════════════════

class TestSnapshot:
    def test_later_exits_do_not_rewrite_the_snapshot(self):
        _exit("NVDA")
        _exit("AMC", volume=True)
        dp.mark_history_fill_scoring()
        _exit("NVDA", volume=True, volatility=True)          # 评分之后的重取撞上断网
        hf = dp.history_fill_stats()
        assert hf["still_missing"]["volume"] == ["AMC", "NVDA"], "进程口径照旧：最近一次"
        sc = hf["at_scoring"]
        assert sc["still_missing"] == {"momentum": [], "volume": ["AMC"], "volatility": []}
        assert sc["resolutions"] == 2 and sc["by_path"]["source:cboe"]["n"] == 2
        assert hf["resolutions"] == 3

    def test_snapshot_is_a_copy(self):
        _exit("NVDA")
        dp.mark_history_fill_scoring()
        dp.history_fill_stats()["at_scoring"]["still_missing"]["volume"].append("X")
        assert dp.history_fill_stats()["at_scoring"]["still_missing"]["volume"] == []

    def test_unmarked_is_none_and_marked_before_any_exit_is_zero(self):
        _exit("NVDA")
        assert dp.history_fill_stats()["at_scoring"] is None
        dp.reset_history_fill_stats()
        dp.mark_history_fill_scoring()
        _exit("NVDA", volume=True)
        sc = dp.history_fill_stats()["at_scoring"]
        assert sc["resolutions"] == 0 and sc["still_missing"] == {"momentum": [], "volume": [], "volatility": []}

    def test_reset_clears_the_snapshot(self):
        _exit("NVDA")
        dp.mark_history_fill_scoring()
        dp.reset_history_fill_stats()
        _exit("NVDA")
        assert dp.history_fill_stats()["at_scoring"] is None


# ══════════════════════════════════════════════════════════════════════════
# 2. 告警
# ══════════════════════════════════════════════════════════════════════════

_TEN = ["AMC", "AMZN", "BRK-B", "CRCL", "MU", "NFLX", "NVDA", "T", "VKTX", "WMT"]


def _analyze(tmp_path, counter):
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": {"history_fill": counter}, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    p2 = [x for x in a.alerts if "历史字段回落后仍缺" in x.message]
    info = [x for x in a.alerts if "评分之后的重取" in x.message]
    return p2, info, a


def _counter_1022():
    """10-22 的形状（生产 scan_timing 逐项对得上：cboe 41 次 + yfinance 1 次，名单同）：30 只预取全齐；
    ML 报告阶段 12 只重取，10 只缺量比、9 只缺波动率。"""
    _exit("T0", path="source:yfinance")
    for t in [f"T{i}" for i in range(1, 20)] + _TEN:
        _exit(t)
    dp.mark_history_fill_scoring()
    for t in _TEN:
        _exit(t, volume=True, volatility=(t != "AMC"))
    _exit("XOM")
    _exit("TSLA")
    return dp.history_fill_stats()


class TestAlert:
    def test_1022_shape_is_no_p2_but_an_info(self, tmp_path):
        p2, info, _ = _analyze(tmp_path, _counter_1022())
        assert p2 == [], "评分用的数据是齐的——不该报「signal_archive 缺 price.* 行」"
        (i,) = info
        assert i.level == am.AlertLevel.INFO
        assert "volume_ratio 10 只" in i.message and "volatility_20d 9 只" in i.message
        assert "评分用的数据不受影响" in i.message
        assert "NVDA" in i.details["volume_ratio"] and "AMC" not in i.details["volatility_20d"]
        assert "source:cboe：12 次" in i.details["各出口（评分之后的增量）"]
        assert "volume 缺 10 补上 0" in i.details["各出口（评分之后的增量）"]
        assert "yfinance" not in i.details["各出口（评分之后的增量）"], "评分之后没走 yfinance 出口，不列"

    def test_info_only_day_is_counted_in_the_alerts_file(self, tmp_path):
        """只有 INFO 的那天，alerts 文件别显示成 0/0/0（第一个 INFO 级告警，v0.45.625）。"""
        _, _, a = _analyze(tmp_path, _counter_1022())
        out = tmp_path / "alerts.json"
        a.save_alerts(out)
        d = json.loads(out.read_text(encoding="utf-8"))
        assert d["info_count"] == 1
        assert d["total_alerts"] == d["critical_count"] + d["high_count"] + d["medium_count"] + d["info_count"], \
            "每条告警都要落进某一级的计数"

    def test_missing_at_scoring_is_p2_and_only_the_extra_is_info(self, tmp_path):
        _exit("NVDA", volume=True)
        _exit("AMD")
        dp.mark_history_fill_scoring()
        _exit("AMD", volume=True)
        _exit("NVDA", volume=True)
        p2, info, _ = _analyze(tmp_path, dp.history_fill_stats())
        (p,) = p2
        assert p.level == am.AlertLevel.MEDIUM and "volume_ratio 1 只" in p.message
        assert p.details["volume_ratio"] == "NVDA" and "口径" not in p.details
        assert "source:cboe：2 次" in p.details["各出口"], "各出口取评分那一刻的计数"
        (i,) = info
        assert i.details["volume_ratio"] == "AMD"

    def test_no_snapshot_falls_back_to_process_view_and_says_so(self, tmp_path):
        _exit("NVDA", volume=True)
        hf = dp.history_fill_stats()
        assert hf["at_scoring"] is None
        p2, info, _ = _analyze(tmp_path, hf)
        (p,) = p2
        assert "口径" in p.details and "未必成立" in p.details["口径"]
        assert info == []

    def test_old_counter_without_the_key_behaves_as_before(self, tmp_path):
        old = {"resolutions": 1, "by_path": {}, "window_behind_session": 0,
               "still_missing": {"momentum": [], "volume": ["NVDA"], "volatility": []}}
        p2, info, _ = _analyze(tmp_path, old)
        assert len(p2) == 1 and info == []

    def test_clean_is_silent(self, tmp_path):
        _exit("NVDA")
        dp.mark_history_fill_scoring()
        _exit("NVDA")
        p2, info, a = _analyze(tmp_path, dp.history_fill_stats())
        assert p2 == [] and info == []
        assert not any("历史字段回落检查" in s for s in a.checks_skipped)


class TestSummaryLine:
    def test_scored_numbers_and_later_segment(self):
        line = stt.summary_line({"phases": {}, "counters": {"history_fill": _counter_1022()}})
        assert "回落后仍缺" not in line, "评分那一刻什么都没缺"
        assert "评分后重取仍缺 动量 0 只/量比 10 只/波动率 9 只" in line

    def test_without_snapshot_unchanged(self):
        _exit("NVDA", volume=True)
        line = stt.summary_line({"phases": {}, "counters": {"history_fill": dp.history_fill_stats()}})
        assert "回落后仍缺 动量 0 只/量比 1 只/波动率 0 只" in line and "评分后重取" not in line


# ══════════════════════════════════════════════════════════════════════════
# 3. 扫描接线
# ══════════════════════════════════════════════════════════════════════════

def _calls_in_order(fn) -> list:
    """函数体里的调用名，按出现行号排序。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
            out.append((n.lineno, name))
    return [name for _, name in sorted(out)]


class TestScanWiring:
    def test_snapshot_is_taken_right_after_prefetch(self):
        from alpha_hive_daily_report import AlphaHiveDailyReporter
        calls = _calls_in_order(AlphaHiveDailyReporter._init_scan_context)
        assert "mark_history_fill_scoring" in calls, "预取之后要给回落统计拍快照"
        i_pre, i_mark = calls.index("prefetch_shared_data"), calls.index("mark_history_fill_scoring")
        assert i_pre < i_mark < calls.index("price_coverage"), "快照要在预取之后、输入闸之前"
        src = inspect.getsource(AlphaHiveDailyReporter._init_scan_context)
        assert "self._prefetched_stock = prefetched.get(\"stock_data\")" in src


def _reporter(tmp_path, prefetched, monkeypatch):
    from alpha_hive_daily_report import AlphaHiveDailyReporter
    import ml_model_guard
    r = AlphaHiveDailyReporter.__new__(AlphaHiveDailyReporter)
    r.report_dir = tmp_path
    r.date_str = "2026-10-22"
    if prefetched is not None:
        r._prefetched_stock = prefetched
    seen = {}

    class _Gen:
        def generate_ml_enhanced_report(self, ticker, ticker_data, **kw):
            seen[ticker] = ticker_data["sources"]["yahoo_finance"]
            return {"ticker": ticker}

        def generate_html_report(self, ticker, enhanced):
            return "<html></html>"
    r.ml_generator = _Gen()
    (tmp_path / ".swarm_results_2026-10-22.json").write_text(json.dumps({"NVDA": {}, "AMC": {}}), encoding="utf-8")
    monkeypatch.setattr(ml_model_guard, "enforce_day", lambda *a, **k: types.SimpleNamespace(
        verdict="ok", n_files=0, n_numeric=0, distinct=0, modal_count=0, snapshot_present=False, exit_code=0))
    fetched = []

    def _fsd(ticker, as_of_date=None):
        fetched.append((ticker, as_of_date))
        return {"price": 9.0, "momentum_5d": -1.0}
    monkeypatch.setattr(dp, "fetch_stock_data", _fsd)
    return r, seen, fetched


class TestMlReportReusesPrefetch:
    def test_prefetched_price_and_momentum_are_used_without_fetching(self, tmp_path, monkeypatch):
        r, seen, fetched = _reporter(tmp_path, {"NVDA": {"price": 181.5, "momentum_5d": -0.9},
                                                "AMC": {"price": 2.93, "momentum_5d": 0.69}}, monkeypatch)
        assert sorted(r._generate_ml_reports({"opportunities": []})) == ["AMC", "NVDA"]
        assert fetched == [], "有预取行情就不该再联网"
        assert seen["NVDA"] == {"current_price": 181.5, "price_change_5d": -0.9, "change_pct": -0.9}
        assert seen["AMC"]["current_price"] == 2.93

    @pytest.mark.parametrize("prefetched", [None, {}, {"NVDA": {"price": 0.0}, "AMC": {"price": None}}],
                             ids=["not_from_a_swarm_scan", "empty", "no_usable_price"])
    def test_without_a_usable_prefetched_price_it_fetches_anchored_to_the_report_date(
            self, tmp_path, monkeypatch, prefetched):
        r, seen, fetched = _reporter(tmp_path, prefetched, monkeypatch)
        r._generate_ml_reports({"opportunities": []})
        assert sorted(fetched) == [("AMC", "2026-10-22"), ("NVDA", "2026-10-22")]
        assert seen["NVDA"]["current_price"] == 9.0 and seen["NVDA"]["price_change_5d"] == -1.0
