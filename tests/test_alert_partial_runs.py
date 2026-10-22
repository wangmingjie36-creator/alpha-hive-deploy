"""partial 运行必须有告警（v0.45.578，v0.45.569 二次检查）。

v0.45.569 把恒真的性能 P1（总耗时 > 7.5 秒）换成按 Step 2 预算判，并在 docstring / CHANGELOG 里写「其余各步有自己的
超时与失败告警」——**不实**。`alert_manager` 当时只认步骤 `status == 'failed'`（外加 Step 2 的 success_with_warning），
而编排器在这几种情况下置 `status=partial` 却不写 failed：
  - 标的丢失：`ticker_completeness = {"status": "incomplete", ...}`；判不了：`{"status": "unknown", ...}`
  - Step 3 超时：`step3_ml_report = {"status": "timeout", ...}`
此前每轮都有那条恒真 P1，所以这些日子「碰巧」不会被渲染成健康；569 之后它们是 `alerts = []` + 日志
「No alerts detected - system healthy」。缺口比 569 早，569 拿掉了唯一碰巧遮住它的那条。

本文件：每种已知 partial 来源各有具体 P1；`status=partial` 而本轮没有任何 P0 / P1 ⇒ 兜底 P1（编排器将来新加的
partial 分支默认会红）；健康日不多报、不多记 checks_skipped（正对照）；`complete` 这个「正常」令牌与编排器写的一致。
"""
import json
import re
from datetime import datetime

import pytest

import alert_manager as am
from tests._orchestrator import repo_orchestrator_text


def _healthy_status():
    """2026-10-20 生产 status.json 的形状（数值照抄，去掉与本文件无关的段）。"""
    return {
        "status": "success",
        "total_duration_seconds": 1992,
        "tickers": [f"T{i:02d}" for i in range(30)],
        "steps_result": {
            "orchestrator_deploy": {"outcome": "already_current", "status": "success", "rc": 0},
            "db_backup": {"status": "success"},
            "data_migrations": {"status": "success", "outcome": "ok"},
            "step2_price_gate": {"attempts": 1, "refused": False},
            "step2_hive_analysis": {"status": "success", "duration_seconds": 1965},
            "ticker_completeness": {"status": "complete", "count": 30},
            "step3_ml_report": {"status": "skipped", "reason": "already_generated_by_step2"},
            "step4_dashboard": {"status": "skipped_builtin"},
            "step5_github_deploy": {"status": "success", "verified_by": "gh_pages_deploy_log"},
        },
        "step2_budget": {"timeout_seconds": 5400, "duration_seconds": 1965,
                         "headroom_seconds": 3435, "headroom_pct": 63},
        "scan_timing": _healthy_scan_timing(),
    }


def _healthy_scan_timing():
    """`main()` 每轮都写进 scan_timing 的那几段的健康形状（同 test_production_sync 健康一轮）——缺了别的检查会出
    与本文件无关的 P1（如「缺整段 scan_timing」），把兜底 P1 遮住。"""
    return {
        "date": "2026-10-20",
        "production_sync": {"outcome": "up_to_date"},
        "extra": {
            "gh_pages": {"success": True},
            "news_sources": {"available": True, "n_known": 30, "n_unknown": 0, "primary_source": "massive",
                             "by_source": {"massive": 30}, "primary_status": {"ok": 30},
                             "non_primary_share": 0.0, "refusal_messages": []},
            "cloud_snapshots": {"available": True, "refresh_ok": True, "refresh_error": None, "refreshed_at": "t",
                                "newest_date": "2026-10-19", "max_lag": 1, "lag": 1, "missing_sessions": []},
            "outcomes_backlog": {"available": True, "pending_files": 30, "pending_offsets": 30,
                                 "oldest_overdue_days": 0, "oldest_item": "X", "max_overdue_days": 7,
                                 "unreadable_files": 0},
            "earnings_dates": {"available": True, "n": 30, "dated": 30, "only_past_dates": 0, "unresolved": 0,
                               "unresolved_tickers": [], "legacy_source": 0, "max_unresolved": 0},
            "observation_archive": {"available": True, "ok": True, "enabled": True, "missing_refs": {},
                                    "missing_payloads": 0},
            "day_health": {"available": True, "version": 1, "verdict": "ok", "min_outage_fields": 2,
                           "degraded_fields": [], "outage_sources": {}},
        },
        "counters": {
            "portfolio_greeks": {"n_stale": 0, "stale": [], "n_quote_stale": 0, "spy": {"stale": False},
                                 "execution_blocked": None, "hedge_undecided": None, "gaps": []},
            "paper_portfolio": {"as_of": "2026-10-20", "ok": True, "error": None},
            "history_fill": {"still_missing": {}, "by_path": {}},
            "ledger_io": {"n": 0, "failures": []},
        },
    }


def _analyze(tmp_path, status):
    # 日报在：否则「未生成日报」这条 P1 会让兜底以为 partial 已有解释（生产里日报照常生成）
    (tmp_path / f"alpha-hive-daily-{datetime.now().strftime('%Y-%m-%d')}.md").write_text("x", encoding="utf-8")
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    alerts = a.analyze(p)
    return a, alerts


def _high(alerts):
    return [x for x in alerts if x.level in (am.AlertLevel.CRITICAL, am.AlertLevel.HIGH)]


def _tagged(alerts, tag):
    return [x for x in alerts if tag in x.tags]


class TestHealthyDayStaysQuiet:

    def test_no_p1_and_no_new_skips(self, tmp_path):
        """正对照：健康日这几条检查一条不报、不往 checks_skipped 里灌东西。"""
        a, alerts = _analyze(tmp_path, _healthy_status())
        assert _high(alerts) == [], [x.message for x in _high(alerts)]
        assert not [s for s in a.checks_skipped if "标的完整性" in s]


class TestKnownPartialSourcesEachFire:

    def test_dropped_tickers_fire_p1(self, tmp_path):
        """二次检查复现原样：10-20 的形状改成 partial + 产出 23/30 ⇒ 569 版 alerts=[]；现在必须是一条具体 P1。"""
        s = _healthy_status()
        s["status"] = "partial"
        s["steps_result"]["ticker_completeness"] = {"status": "incomplete", "intended": 30, "actual": 23}
        _a, alerts = _analyze(tmp_path, s)
        hit = _tagged(alerts, "ticker_completeness")
        assert len(hit) == 1 and hit[0].level == am.AlertLevel.HIGH
        assert "23/30" in hit[0].message
        assert not _tagged(alerts, "partial_unexplained"), "已有具体 P1 时兜底不该再叠一条"

    @pytest.mark.parametrize("tc", [
        {"status": "unknown", "reason": "unreadable"},   # 编排器真实写法：.swarm_results 读不出
        {"status": "something_new"},                       # 编排器将来新加的取值：默认要红
        {},                                                # 没有 status
        "garbage",                                         # 整段不是对象
    ])
    def test_completeness_not_complete_fires(self, tmp_path, tc):
        s = _healthy_status()
        s["status"] = "partial"
        s["steps_result"]["ticker_completeness"] = tc
        _a, alerts = _analyze(tmp_path, s)
        hit = _tagged(alerts, "ticker_completeness")
        assert len(hit) == 1 and hit[0].level == am.AlertLevel.HIGH, [x.message for x in alerts]

    def test_completeness_missing_after_step2_is_recorded_as_skipped(self, tmp_path):
        s = _healthy_status()
        del s["steps_result"]["ticker_completeness"]
        a, alerts = _analyze(tmp_path, s)
        assert not _tagged(alerts, "ticker_completeness")
        assert any("标的完整性检查" in x for x in a.checks_skipped), a.checks_skipped

    def test_completeness_missing_when_step2_never_ran_records_nothing(self, tmp_path):
        s = _healthy_status()
        for k in ("ticker_completeness", "step2_hive_analysis"):
            del s["steps_result"][k]
        del s["step2_budget"]
        a, _alerts = _analyze(tmp_path, s)
        assert not any("标的完整性检查" in x for x in a.checks_skipped)

    @pytest.mark.parametrize("step", ["step3_ml_report", "step2_hive_analysis"])
    def test_step_timeout_fires(self, tmp_path, step):
        s = _healthy_status()
        s["status"] = "partial"
        s["steps_result"][step] = {"status": "timeout", "duration_seconds": 611}
        _a, alerts = _analyze(tmp_path, s)
        hit = [x for x in alerts if x.message == f"⚠️ 【P1 高】步骤超时：{step}"]
        assert len(hit) == 1 and hit[0].level == am.AlertLevel.HIGH
        assert hit[0].details["耗时"] == "611秒"
        assert not _tagged(alerts, "partial_unexplained")


class TestPartialNeverRendersHealthy:

    def test_unexplained_partial_fires_fallback(self, tmp_path):
        """编排器在本类不认识的地方置了 partial（每一步看着都正常）⇒ 兜底 P1。"""
        s = _healthy_status()
        s["status"] = "partial"
        _a, alerts = _analyze(tmp_path, s)
        hit = _tagged(alerts, "partial_unexplained")
        assert len(hit) == 1 and hit[0].level == am.AlertLevel.HIGH
        assert "无" in str(hit[0].details["非正常结局的步骤"])

    def test_fallback_lists_unfamiliar_step_statuses_only(self, tmp_path):
        s = _healthy_status()
        s["status"] = "partial"
        s["steps_result"]["step9_new_thing"] = {"status": "degraded"}
        _a, alerts = _analyze(tmp_path, s)
        (hit,) = _tagged(alerts, "partial_unexplained")
        odd = hit.details["非正常结局的步骤"]
        assert odd == {"step9_new_thing": "degraded"}, odd   # 没有 status 键的 step2_price_gate 不列

    def test_fallback_is_silent_when_status_is_success(self, tmp_path):
        s = _healthy_status()
        s["steps_result"]["step9_new_thing"] = {"status": "degraded"}
        _a, alerts = _analyze(tmp_path, s)
        assert not _tagged(alerts, "partial_unexplained")

    def test_fallback_runs_after_every_other_check(self):
        """兜底必须是 analyze() 里最后一个检查：排在别的检查前面，它就看不见后面那些检查会报的 P1，会误报。"""
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(am.AlertAnalyzer.analyze))
        calls = [n.func.attr for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "self" and n.func.attr.startswith("_check_")]
        assert calls and calls[-1] == "_check_partial_explained", calls
        assert calls.count("_check_partial_explained") == 1


class TestWriterTokens:

    def test_orchestrator_success_token_is_complete(self):
        """读者判「不等于 complete 就报」⇒ 编排器成功分支写的令牌必须恰好是 complete，否则每天都报。"""
        code = "\n".join(l for l in repo_orchestrator_text().splitlines() if not l.lstrip().startswith("#"))
        vals = re.findall(r'\\"ticker_completeness\\": \{\\"status\\": \\"(\w+)\\"', code)
        assert "complete" in vals, vals
        assert set(vals) <= {"complete", "incomplete", "unknown", "failed"}, (
            f"编排器的 ticker_completeness 多了新取值 {set(vals)}——读者会按「不等于 complete」报 P1，确认这是想要的")

    def test_orchestrator_writes_timeout_for_step3(self):
        code = "\n".join(l for l in repo_orchestrator_text().splitlines() if not l.lstrip().startswith("#"))
        assert re.search(r'\\"step3_ml_report\\": \{\\"status\\": \\"timeout\\"', code)
