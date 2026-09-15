"""
`status.json` 整段缺 `scan_timing` 时该不该报警（v0.45.255）

改动前：不管什么原因，`scan_timing` 整段缺失一律走 `checks_skipped` + 一行 WARNING 日志，
`alerts.json`（真正推给人看的那份）里什么都没有。

2026-09-14 14:48 实测反例：蜂群扫描真跑完了（`step2_hive_analysis` 成功、日报已提交推送、
`total_duration_seconds=2889`），`logs/scan_timing.json` 本身数据完整，唯独编排器
`write_status()` 把它并进 `status.json` 那一步没生效——根因排查了 TCC 权限 / 脚本改动 /
写入时序竞争 / 线程卡死强退四个假设，**逐一用 09-10、09-11 两天的日志证伪**（同样的条件
那两天都存在，合并却都成功）。真正触发条件仍不明——**不追一个可能永远抓不住的瞬时原因，
把这一类失败本身变成可观测的**：扫描真跑完了、`scan_timing` 却整段没进 `status.json`，
这本身就是「不知道自己不知道」，比任何一条已知的推送/提交失败都危险。

判据（对应两条测试）：
  - `step2_hive_analysis` 成功 + `scan_timing` 缺失 ⇒ 新 P1（本文件的核心场景）
  - `step2_hive_analysis` 未成功/未出现 + `scan_timing` 缺失 ⇒ **维持原样**，只
    `checks_skipped`，不报警——空扫描护栏 / `--no-swarm` 早退等路径到不了 `scan_timing.write()`，
    缺失对它们是设计内状态，新逻辑不能误伤（这条是正对照，防判别器写反）。
"""
import json

import pytest

import alert_manager as am


def _write_status(tmp_path, status):
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False))
    return p


def _alerts(tmp_path, status):
    a = am.AlertAnalyzer(report_dir=tmp_path)
    p = _write_status(tmp_path, status)
    return a, [x.message for x in a.analyze(p)]


class TestScanTimingMissingAfterARealScan:

    def test_completed_scan_with_missing_scan_timing_raises_p1(self, tmp_path):
        """核心场景：真跑完的扫描（正对照见下一条断言），scan_timing 整段缺失。"""
        status = {
            "status": "success",
            "total_duration_seconds": 2889,
            "steps_result": {"step2_hive_analysis": {"status": "success"}},
            # 没有 "scan_timing" 键——复刻 2026-09-14 14:48 的真实 status.json
        }
        assert "scan_timing" not in status
        a, msgs = _alerts(tmp_path, status)

        hit = [x for x in a.alerts if "缺整段 scan_timing" in x.message]
        assert hit, msgs
        assert hit[0].level == am.AlertLevel.HIGH
        assert "production_sync" in hit[0].details["影响"] or "git_push" in hit[0].details["影响"]
        # 原有行为必须保留：checks_skipped 与 WARNING 路径不能被新逻辑顶掉
        assert any("scan_timing" in s for s in a.checks_skipped), a.checks_skipped

    def test_legitimate_early_exit_still_stays_silent(self, tmp_path):
        """正对照：step2 没跑到「成功」（早退路径），scan_timing 缺失是设计内状态，不该报警。
        这条不成立，说明判别器把「早退」也当成了「真扫描」，会把日常早退路径全部误报成 P1。"""
        status = {
            "status": "success",
            "total_duration_seconds": 3,
            "steps_result": {"step2_hive_analysis": {"status": "skipped"}},
        }
        a, msgs = _alerts(tmp_path, status)
        assert not any("缺整段 scan_timing" in m for m in msgs), msgs
        assert any("scan_timing" in s for s in a.checks_skipped), a.checks_skipped

    def test_missing_steps_result_entirely_also_stays_silent(self, tmp_path):
        """更早退的一种：连 steps_result 都没有（v0.45.47 那条既有闸已经会单独报，这里
        只核对新逻辑不会在这种「什么都不知道」的场景上重复报警或误判为「真扫描过」。"""
        status = {"status": "success", "total_duration_seconds": 1}
        a, msgs = _alerts(tmp_path, status)
        assert not any("缺整段 scan_timing" in m for m in msgs), msgs

    def test_scan_timing_present_is_unaffected(self, tmp_path):
        """正对照：scan_timing 正常存在时，新逻辑完全不触发（判别器只在「缺失」分支里生效）。"""
        status = {
            "status": "success",
            "total_duration_seconds": 2889,
            "steps_result": {"step2_hive_analysis": {"status": "success"}},
            "scan_timing": {"date": "2026-09-14", "production_sync": None},
        }
        a, msgs = _alerts(tmp_path, status)
        assert not any("缺整段 scan_timing" in m for m in msgs), msgs
        assert not any("status.json 缺 scan_timing" in s for s in a.checks_skipped), a.checks_skipped


class TestSwarmScanActuallyRanPredicate:
    """判别器本身的单元测试（不经过 analyze()，直接测判据）。"""

    @pytest.mark.parametrize("steps_result,expected", [
        ({"step2_hive_analysis": {"status": "success"}}, True),
        ({"step2_hive_analysis": {"status": "skipped"}}, False),
        ({"step2_hive_analysis": {"status": "failed"}}, False),
        ({}, False),
        (None, False),
    ])
    def test_predicate(self, steps_result, expected):
        status = {"steps_result": steps_result} if steps_result is not None else {}
        assert am.AlertAnalyzer._swarm_scan_actually_ran(status) is expected
