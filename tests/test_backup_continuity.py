"""
数据备份连续性判定的纯逻辑测试（v0.45.284）

背景：编排器 Step 14 刻意不动 OVERALL_STATUS、不发 Slack（单次备份失败是噪音，
同 Step 10/12 先例）。但这也意味着"连续多天卡在同一种失败/陈旧状态"没有任何
东西会主动告诉任何人——与 2026-09-09~11 三次 DB 备份被 TCC 拒无人发现的事故
同构。`backup_continuity.py` 补的正是这个聚合信号，模式照抄
`tests/test_scan_continuity.py`。

本文件只测纯逻辑（历史日志解析、空档判定、门槛判定），全部用合成数据，
不依赖生产 `backup_status_history.jsonl`。
"""

import datetime as dt
import json

import pytest

from backup_continuity import (
    alert_line,
    assess,
    read_history,
)
from scan_continuity import recent_trading_days


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ────────────────────────────────────────────────────────────────────────────
# 历史日志解析
# ────────────────────────────────────────────────────────────────────────────

class TestReadHistory:
    def test_missing_file_returns_empty_not_crash(self, tmp_path):
        healthy, last_stage = read_history(tmp_path / "absent.jsonl")
        assert healthy == set()
        assert last_stage == {}

    def test_ok_true_marks_day_healthy(self, tmp_path):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": "2026-09-10", "stage": "done", "ok": True}])
        healthy, last_stage = read_history(p)
        assert healthy == {"2026-09-10"}
        assert last_stage == {"2026-09-10": "done"}

    def test_ok_false_does_not_mark_healthy(self, tmp_path):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": "2026-09-10", "stage": "push", "ok": False}])
        healthy, last_stage = read_history(p)
        assert healthy == set()
        assert last_stage == {"2026-09-10": "push"}

    def test_same_day_retry_eventually_ok_counts_healthy(self, tmp_path):
        """人工同一天重跑：先失败后成功——当天最终算健康，与 status.json
        本身"只反映最近一次"的语义一致。"""
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [
            {"date": "2026-09-10", "stage": "push", "ok": False},
            {"date": "2026-09-10", "stage": "done", "ok": True},
        ])
        healthy, last_stage = read_history(p)
        assert healthy == {"2026-09-10"}
        assert last_stage["2026-09-10"] == "done"

    def test_malformed_line_is_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "h.jsonl"
        p.write_text(
            '{"date": "2026-09-09", "stage": "done", "ok": true}\n'
            'not even json\n'
            '{"date": "2026-09-10", "stage": "done", "ok": true}\n',
            encoding="utf-8",
        )
        healthy, _ = read_history(p)
        assert healthy == {"2026-09-09", "2026-09-10"}

    def test_blank_lines_are_skipped(self, tmp_path):
        p = tmp_path / "h.jsonl"
        p.write_text('{"date": "2026-09-09", "stage": "done", "ok": true}\n\n\n', encoding="utf-8")
        healthy, _ = read_history(p)
        assert healthy == {"2026-09-09"}

    def test_missing_date_field_is_skipped(self, tmp_path):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"stage": "done", "ok": True}])
        healthy, last_stage = read_history(p)
        assert healthy == set()
        assert last_stage == {}


# ────────────────────────────────────────────────────────────────────────────
# 端到端判定
# ────────────────────────────────────────────────────────────────────────────

class TestAssess:
    def test_full_coverage_is_healthy(self, tmp_path):
        days = recent_trading_days(10, end=_d("2026-08-14"))
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": d.isoformat(), "stage": "done", "ok": True} for d in days])
        res = assess(history_path=p, days=10, end="2026-08-14")
        assert res["healthy"] is True
        assert res["coverage"] == 1.0
        assert res["longest_gap"] == 0
        assert alert_line(res) is None, "健康时必须静默，不得产生噪音"

    def test_low_coverage_is_degraded(self, tmp_path):
        days = recent_trading_days(10, end=_d("2026-08-14"))
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [
            {"date": days[0].isoformat(), "stage": "done", "ok": True},
            {"date": days[-1].isoformat(), "stage": "done", "ok": True},
        ])
        res = assess(history_path=p, days=10, end="2026-08-14")
        assert res["healthy"] is False
        assert res["coverage"] == pytest.approx(0.2)
        line = alert_line(res)
        assert line and "连续性降级" in line

    def test_gap_alone_triggers_degraded(self, tmp_path):
        """覆盖率达标但存在长空档——两个门槛是 AND 关系，不是 OR。"""
        days = recent_trading_days(20, end=_d("2026-08-14"))
        skip = {d.isoformat() for d in days[5:8]}
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [
            {"date": d.isoformat(), "stage": "done", "ok": True}
            for d in days if d.isoformat() not in skip
        ])
        res = assess(history_path=p, days=20, end="2026-08-14", max_gap=2)
        assert res["coverage"] >= 0.80
        assert res["longest_gap"] == 3
        assert res["healthy"] is False

    def test_all_failures_same_stage_is_visible_in_alert(self, tmp_path):
        """连续多天卡在同一种失败——alert_line 要能指出涉及哪个阶段，
        不只是说"降级了"，否则跟 Step 14 本来就有的单次日志没有区别。"""
        days = recent_trading_days(5, end=_d("2026-08-14"))
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [
            {"date": d.isoformat(), "stage": "stale_or_missing", "ok": False} for d in days
        ])
        res = assess(history_path=p, days=5, end="2026-08-14")
        assert res["healthy"] is False
        line = alert_line(res)
        assert "stale_or_missing" in line

    def test_missing_history_returns_empty_not_crash(self, tmp_path):
        res = assess(history_path=tmp_path / "absent.jsonl", days=5, end="2026-08-14")
        assert res["backed_up_days"] == 0
        assert res["healthy"] is False

    def test_since_window_overrides_days(self, tmp_path):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": "2026-08-12", "stage": "done", "ok": True}])
        res = assess(history_path=p, since="2026-08-10", end="2026-08-14")
        assert res["window"]["start"] == "2026-08-10"
        assert res["window"]["trading_days"] == 5


# ────────────────────────────────────────────────────────────────────────────
# 退出码——编排器 Step 15 唯一的判据
# ────────────────────────────────────────────────────────────────────────────

class TestExitCodes:
    """同 `scan_continuity.py`：3 是编排器 `run_step()` 保留给「脚本不存在」
    的 2 之外的"无法判定"码，不可混用。"""

    def _run(self, monkeypatch, argv):
        import backup_continuity
        monkeypatch.setattr("sys.argv", ["backup_continuity.py", *argv])
        return backup_continuity.main()

    def test_missing_history_returns_3_not_2(self, monkeypatch, tmp_path):
        rc = self._run(monkeypatch, ["--history", str(tmp_path / "absent.jsonl")])
        assert rc == 3, "2 是编排器保留给「脚本不存在」的码，不可占用"

    def test_healthy_returns_0(self, monkeypatch, tmp_path):
        days = recent_trading_days(10, end=_d("2026-08-14"))
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": d.isoformat(), "stage": "done", "ok": True} for d in days])
        rc = self._run(monkeypatch, [
            "--history", str(p), "--days", "10", "--end", "2026-08-14", "--quiet",
        ])
        assert rc == 0

    def test_degraded_returns_1(self, monkeypatch, tmp_path):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": "2026-08-14", "stage": "done", "ok": True}])
        rc = self._run(monkeypatch, [
            "--history", str(p), "--days", "10", "--end", "2026-08-14", "--quiet",
        ])
        assert rc == 1

    def test_json_mode_preserves_exit_code(self, monkeypatch, tmp_path, capsys):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": "2026-08-14", "stage": "done", "ok": True}])
        rc = self._run(monkeypatch, [
            "--history", str(p), "--days", "10", "--end", "2026-08-14", "--json",
        ])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["healthy"] is False
        # 编排器 Step 15 的摘要提取依赖这几个键
        for key in ("window", "backed_up_days", "coverage", "longest_gap", "weeks_missed"):
            assert key in payload, f"编排器 Step 15 依赖的键 {key} 消失了"

    def test_out_writes_json_file_and_keeps_exit_code(self, monkeypatch, tmp_path):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": "2026-08-14", "stage": "done", "ok": True}])
        out = tmp_path / "cont.json"
        rc = self._run(monkeypatch, [
            "--history", str(p), "--days", "10", "--end", "2026-08-14", "--quiet",
            "--out", str(out),
        ])
        assert rc == 1
        payload = json.loads(out.read_text())
        for key in ("window", "backed_up_days", "coverage", "longest_gap", "healthy"):
            assert key in payload

    def test_out_failure_does_not_change_verdict(self, monkeypatch, tmp_path):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": "2026-08-14", "stage": "done", "ok": True}])
        bad = tmp_path / "no_such_dir" / "cont.json"
        rc = self._run(monkeypatch, [
            "--history", str(p), "--days", "10", "--end", "2026-08-14", "--quiet",
            "--out", str(bad),
        ])
        assert rc == 1
        assert not bad.exists()

    def test_slack_flag_does_not_send(self, monkeypatch, tmp_path):
        p = tmp_path / "h.jsonl"
        _write_jsonl(p, [{"date": "2026-08-14", "stage": "done", "ok": True}])
        rc = self._run(monkeypatch, [
            "--history", str(p), "--days", "10", "--end", "2026-08-14", "--quiet", "--slack",
        ])
        assert rc == 1
