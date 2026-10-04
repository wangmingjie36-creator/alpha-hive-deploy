"""告警侧接住编排器 B2（v0.45.386）：Step 2 的 `success_with_warning`、步骤解释器兜底 `interp_fallback`、非对象条目。

B2 之前，Step 2 退出 1（ML 常数闸，09-24/25 实况）在 status.json 里是 step2 / step4 两条 `failed` ⇒ 两条通用 P1。
B2 起解释器按「主流程确实跑完」记 `success_with_warning`（step4 记 `skipped_builtin`）——标签更准了，
但**那一天唯一的告警来源不许跟着消失**：这里要求它换成一条专门的 HIGH（带 warning 原因）。
编排器的步骤解释器不可用时按退出码兜底，片段带 `interp_fallback` ⇒ 一条 MEDIUM（摘要缺失本身要人看）。

「谁会红」（每条变异都在仓库副本的拷贝上实测过，见 CHANGELOG 0.45.386）：
  · 删 success_with_warning 那条 HIGH ⇒ TestSuccessWithWarning 红（ML 常数日零步骤告警）
  · 删 interp_fallback 那条 MEDIUM ⇒ TestInterpFallback 红
  · 删非对象守卫 ⇒ TestNonDictEntry 红（此前 AttributeError，整个 Step 6 崩）
  · 新规则误伤旧格式 status.json ⇒ TestPreBStatusUnchanged 红（合并当天：旧编排器 + 新 alert_manager）
"""
import json

import pytest

import alert_manager as am

_STEP_TAGS = ("step_failure", "step_warning", "step_interp_fallback")


def _analyze(tmp_path, steps_result, **top):
    status = {"status": "partial", "total_duration_seconds": 1, "steps_result": steps_result, **top}
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return a


def _step_alerts(a):
    """只看步骤循环产出的告警（其余检查——日报缺失、scan_timing 缺失——与本文件无关）。"""
    return [x for x in a.alerts if x.tags and x.tags[0] in _STEP_TAGS]


class TestSuccessWithWarning:
    @pytest.mark.parametrize("warning", ["ml_model_constant", "rc1_after_completion_unexplained"])
    def test_one_high_naming_the_reason(self, tmp_path, warning):
        a = _analyze(tmp_path, {
            "step2_hive_analysis": {"status": "success_with_warning", "duration_seconds": 2800, "rc": 1,
                                    "warning": warning},
            "step4_dashboard": {"status": "skipped_builtin", "step2_status": "success_with_warning"},
        })
        got = _step_alerts(a)
        assert len(got) == 1, [x.message for x in got]
        hit = got[0]
        assert hit.level == am.AlertLevel.HIGH and "退出码 1" in hit.message and warning in hit.message
        assert hit.tags == ["step_warning", "step2_hive_analysis"]
        assert hit.details["原因"] == warning and hit.details["推送"] is True

    def test_missing_warning_field_still_alerts(self, tmp_path):
        """warning 键缺了也要报（写「原因未记」），不许因为缺字段就静默。"""
        a = _analyze(tmp_path, {"step2_hive_analysis": {"status": "success_with_warning"}})
        got = _step_alerts(a)
        assert len(got) == 1 and "原因未记" in got[0].message

    def test_only_step2_gets_this_rule(self, tmp_path):
        """规则只认 step2：别的步骤出现同名 status（目前不存在）不借用这条文案。"""
        a = _analyze(tmp_path, {"step10_scan_continuity": {"status": "success_with_warning"}})
        assert _step_alerts(a) == []


class TestExistingRulesUnchanged:
    def test_failed_is_the_generic_high(self, tmp_path):
        a = _analyze(tmp_path, {"step2_hive_analysis": {"status": "failed", "duration_seconds": 7}})
        got = _step_alerts(a)
        assert [(x.level, x.message, x.tags) for x in got] == [
            (am.AlertLevel.HIGH, "⚠️ 【P1 高】步骤失败：step2_hive_analysis", ["step_failure", "step2_hive_analysis"])]
        assert got[0].details == {"步骤": "step2_hive_analysis", "耗时": "7秒", "状态": "失败"}

    def test_success_raises_nothing(self, tmp_path):
        a = _analyze(tmp_path, {"step2_hive_analysis": {"status": "success", "duration_seconds": 7},
                                "step4_dashboard": {"status": "skipped_builtin"}})
        assert _step_alerts(a) == []


class TestInterpFallback:
    @pytest.mark.parametrize("frag", [
        {"status": "failed", "duration_seconds": 7, "interp_fallback": "解释器超时（>30s）"},
        {"status": "success", "duration_seconds": 7, "interp_fallback": "orchestrator_steps.py 不存在"},
        # tool-step：生产上到不了这里——Step 6 的分析跑在 Step 10–15 之前（换基后复审）。
        # 这一格只钉「规则对任何步骤名都通用」，留给将来若加末轮分析时用，不代表 10–15 的兜底今天会出告警。
        {"status": "interpreter_unavailable", "rc": 1, "interp_fallback": "x" * 1000},
    ], ids=["failed", "success", "tool-step"])
    def test_one_medium_naming_the_step(self, tmp_path, frag):
        name = "step12_scan_coverage" if frag["status"] == "interpreter_unavailable" else "step2_hive_analysis"
        a = _analyze(tmp_path, {name: frag})
        med = [x for x in _step_alerts(a) if x.tags[0] == "step_interp_fallback"]
        assert len(med) == 1, [x.message for x in _step_alerts(a)]
        assert med[0].level == am.AlertLevel.MEDIUM and name in med[0].message
        assert med[0].tags == ["step_interp_fallback", name]
        assert med[0].details["状态"] == frag["status"] and len(med[0].details["原因"]) <= 300
        # 兜底的 failed 照旧另有那条通用 HIGH（兜底逐字复现 B 之前的 status，alert_manager 靠它出 P1）
        highs = [x for x in _step_alerts(a) if x.tags[0] == "step_failure"]
        assert len(highs) == (1 if frag["status"] == "failed" else 0)


class TestNonDictEntry:
    @pytest.mark.parametrize("bad", ["failed", None, 3, ["failed"]])
    def test_non_dict_is_recorded_not_crashed(self, tmp_path, bad):
        """变异：删 isinstance 守卫 ⇒ `.get` AttributeError，analyze() 崩。"""
        a = _analyze(tmp_path, {"weird": bad, "step2_hive_analysis": {"status": "failed"}})
        assert any("weird" in s and "不是对象" in s for s in a.checks_skipped), a.checks_skipped
        # 同一个循环里后面的条目照查：一个坏条目不许让整组检查失效
        assert [x.tags for x in _step_alerts(a)] == [["step_failure", "step2_hive_analysis"]]


def _pre_b_step_alerts(steps_result):
    """B 之前 P1 循环的全部行为（逐字）：status == 'failed' ⇒ 一条 HIGH「步骤失败」，别的什么都不出。"""
    return [(am.AlertLevel.HIGH, f"⚠️ 【P1 高】步骤失败：{n}", ["step_failure", n])
            for n, r in steps_result.items() if r.get("status") == "failed"]


class TestPreBStatusUnchanged:
    """合并当天（M2）：这一轮仍是旧编排器 + 新 alert_manager ⇒ 告警必须与 B 之前完全相同。"""

    @pytest.mark.parametrize("steps_result", [
        {"step2_hive_analysis": {"status": "success", "duration_seconds": 2800},
         "step4_dashboard": {"status": "skipped_builtin"},
         "step5_github_deploy": {"status": "success", "action": "pushed_new_commit"}},
        {"step2_hive_analysis": {"status": "failed", "duration_seconds": 2800},
         "step4_dashboard": {"status": "failed", "reason": "step2_did_not_complete", "step2_rc": 1},
         "step5_github_deploy": {"status": "failed", "reason": "step2_did_not_complete", "step2_rc": 1}},
        {"step2_hive_analysis": {"status": "timeout", "duration_seconds": 3600},
         "step4_dashboard": {"status": "failed", "reason": "step2_did_not_complete", "step2_rc": 124}},
        {"step2_hive_analysis": {"status": "skipped"},
         "step4_dashboard": {"status": "failed", "reason": "step2_did_not_complete", "step2_rc": 2}},
    ], ids=["rc0", "rc1", "rc124", "rc2"])
    def test_same_step_alerts_as_before(self, tmp_path, steps_result):
        a = _analyze(tmp_path, steps_result)
        assert [(x.level, x.message, x.tags) for x in _step_alerts(a)] == _pre_b_step_alerts(steps_result)
        assert not [s for s in a.checks_skipped if "不是对象" in s]
