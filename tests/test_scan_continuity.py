"""
扫描连续性判定的纯逻辑测试（v0.44.0）

背景：`experiments/ic_power_analysis.py` 实测扩池 10→30 只把出结论所需的日历
时间缩短 **5.18 倍**，但那个增益的计价单位是**有扫描的 ISO 周数**——T+7 的
不重叠取样单位就是周，漏一周就是少一个观测。所以连续性不是运维洁癖，
是唯一能兑现扩池增益、也是唯一能按比例吃掉它的东西。

本文件只测纯逻辑（空档切分、ISO 周覆盖、门槛判定），全部用合成数据，
不依赖生产库——生产库的分布断言在 `test_distribution_invariants.py`。
"""

import datetime as dt
import json
import sqlite3

import pytest

from scan_continuity import (
    alert_line,
    assess,
    find_gaps,
    recent_trading_days,
    scanned_days_from_snapshots,
    trading_days_between,
    week_coverage,
)


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# ────────────────────────────────────────────────────────────────────────────
# 交易日枚举
# ────────────────────────────────────────────────────────────────────────────

class TestTradingDays:
    def test_weekend_excluded(self):
        # 2026-08-15/16 是周六/周日
        days = trading_days_between(_d("2026-08-14"), _d("2026-08-17"))
        assert days == [_d("2026-08-14"), _d("2026-08-17")]

    def test_holiday_excluded(self):
        """独立日 2026-07-04 是周六 → observed 到 07-03（周五）。"""
        days = trading_days_between(_d("2026-07-02"), _d("2026-07-06"))
        assert _d("2026-07-03") not in days
        assert _d("2026-07-02") in days
        assert _d("2026-07-06") in days

    def test_recent_counts_exactly_n(self):
        days = recent_trading_days(10, end=_d("2026-08-14"))
        assert len(days) == 10
        assert days == sorted(days)
        assert days[-1] == _d("2026-08-14")

    def test_recent_spans_long_holiday_without_truncating(self):
        """跨长假时不能因为自然日上限而少给交易日。"""
        days = recent_trading_days(20, end=_d("2026-01-05"))
        assert len(days) == 20


# ────────────────────────────────────────────────────────────────────────────
# 空档切分
# ────────────────────────────────────────────────────────────────────────────

class TestFindGaps:
    def test_no_gap_when_all_scanned(self):
        exp = [_d("2026-08-10"), _d("2026-08-11"), _d("2026-08-12")]
        assert find_gaps(exp, {d.isoformat() for d in exp}) == []

    def test_single_day_gap(self):
        exp = [_d("2026-08-10"), _d("2026-08-11"), _d("2026-08-12")]
        gaps = find_gaps(exp, {"2026-08-10", "2026-08-12"})
        assert gaps == [{"start": "2026-08-11", "end": "2026-08-11", "n_days": 1}]

    def test_consecutive_days_merge_into_one_gap(self):
        exp = [_d(f"2026-08-{i:02d}") for i in (10, 11, 12, 13, 14)]
        gaps = find_gaps(exp, {"2026-08-10", "2026-08-14"})
        assert gaps == [{"start": "2026-08-11", "end": "2026-08-13", "n_days": 3}]

    def test_trailing_gap_is_reported(self):
        """结尾处的空档最容易漏——扫描刚停就是这个形状。"""
        exp = [_d("2026-08-10"), _d("2026-08-11"), _d("2026-08-12")]
        gaps = find_gaps(exp, {"2026-08-10"})
        assert gaps == [{"start": "2026-08-11", "end": "2026-08-12", "n_days": 2}]

    def test_all_missing_is_one_gap(self):
        exp = [_d("2026-08-10"), _d("2026-08-11")]
        gaps = find_gaps(exp, set())
        assert len(gaps) == 1 and gaps[0]["n_days"] == 2


# ────────────────────────────────────────────────────────────────────────────
# ISO 周覆盖 —— 功效计算的硬通货单位
# ────────────────────────────────────────────────────────────────────────────

class TestWeekCoverage:
    def test_one_scan_covers_the_whole_week(self):
        """一周只要有 1 天扫描就贡献 1 个不重叠 T+7 观测，不需要 5 天全跑。"""
        exp = [_d(f"2026-08-{i:02d}") for i in (10, 11, 12, 13, 14)]
        wk = week_coverage(exp, {"2026-08-12"})
        assert wk["weeks_total"] == 1
        assert wk["weeks_covered"] == 1
        assert wk["week_coverage"] == 1.0
        assert wk["weeks_missed"] == []

    def test_missed_week_is_named(self):
        # W33 = 08-10~08-14, W34 = 08-17~08-21
        exp = [_d("2026-08-12"), _d("2026-08-18")]
        wk = week_coverage(exp, {"2026-08-12"})
        assert wk["weeks_total"] == 2
        assert wk["weeks_covered"] == 1
        assert wk["weeks_missed"] == ["2026-W34"]

    def test_week_coverage_independent_of_day_count(self):
        """5 天里跑 1 天 vs 跑 5 天，周覆盖都是 100% —— 这正是重点：
        提高每周扫描次数不增加不重叠观测，**补上漏掉的周**才增加。"""
        exp = [_d(f"2026-08-{i:02d}") for i in (10, 11, 12, 13, 14)]
        one = week_coverage(exp, {"2026-08-10"})
        five = week_coverage(exp, {f"2026-08-{i:02d}" for i in (10, 11, 12, 13, 14)})
        assert one["week_coverage"] == five["week_coverage"] == 1.0


# ────────────────────────────────────────────────────────────────────────────
# 端到端判定（合成库）
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def synth_db(tmp_path):
    """建一个最小 predictions 表的工厂。"""
    def _make(dates_with_tickers):
        p = tmp_path / "synth.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT)")
        for d, n in dates_with_tickers.items():
            con.executemany("INSERT INTO predictions VALUES (?,?)",
                            [(d, f"T{i}") for i in range(n)])
        con.commit()
        con.close()
        return p
    return _make


class TestAssess:
    def test_full_coverage_is_healthy(self, synth_db, tmp_path):
        days = recent_trading_days(10, end=_d("2026-08-14"))
        db = synth_db({d.isoformat(): 30 for d in days})
        res = assess(db_path=db, snap_dir=tmp_path / "nope",
                     days=10, end="2026-08-14")
        assert res["healthy"] is True
        assert res["coverage"] == 1.0
        assert res["longest_gap"] == 0
        assert alert_line(res) is None, "健康时必须静默，不得产生噪音"

    def test_low_coverage_is_degraded(self, synth_db, tmp_path):
        days = recent_trading_days(10, end=_d("2026-08-14"))
        db = synth_db({days[0].isoformat(): 30, days[-1].isoformat(): 30})
        res = assess(db_path=db, snap_dir=tmp_path / "nope",
                     days=10, end="2026-08-14")
        assert res["healthy"] is False
        assert res["coverage"] == pytest.approx(0.2)
        line = alert_line(res)
        assert line and "连续性降级" in line

    def test_gap_alone_triggers_degraded(self, synth_db, tmp_path):
        """覆盖率达标但存在长空档 —— 两个门槛是 AND 关系，不是 OR。"""
        days = recent_trading_days(20, end=_d("2026-08-14"))
        # 跑掉 17/20 = 85% > 80%，但中间连续缺 3 天
        skip = {d.isoformat() for d in days[5:8]}
        db = synth_db({d.isoformat(): 30 for d in days
                       if d.isoformat() not in skip})
        res = assess(db_path=db, snap_dir=tmp_path / "nope",
                     days=20, end="2026-08-14", max_gap=2)
        assert res["coverage"] >= 0.80
        assert res["longest_gap"] == 3
        assert res["healthy"] is False

    def test_missing_db_returns_empty_not_crash(self, tmp_path):
        res = assess(db_path=tmp_path / "absent.db", snap_dir=tmp_path / "nope",
                     days=5, end="2026-08-14")
        assert res["scanned_days"] == 0
        assert res["healthy"] is False

    def test_since_window_overrides_days(self, synth_db, tmp_path):
        db = synth_db({"2026-08-12": 30})
        res = assess(db_path=db, snap_dir=tmp_path / "nope",
                     since="2026-08-10", end="2026-08-14")
        assert res["window"]["start"] == "2026-08-10"
        assert res["window"]["trading_days"] == 5


class TestExitCodes:
    """退出码是编排器唯一的判据，必须逐个钉住。

    尤其是 3 —— 编排器 `run_step()` 把 **2 保留给「脚本不存在」**，
    若"无法判定"也用 2，编排器就分不清"检查器没装"和"装了但判不了"，
    那本身就是一次静默降级。这条约定只存在于两份文件的注释里，
    没有测试就会在某次重构中悄悄失效。
    """

    def _run(self, monkeypatch, argv):
        import scan_continuity
        monkeypatch.setattr("sys.argv", ["scan_continuity.py", *argv])
        return scan_continuity.main()

    def test_missing_db_returns_3_not_2(self, monkeypatch, tmp_path):
        rc = self._run(monkeypatch, ["--db", str(tmp_path / "absent.db")])
        assert rc == 3, "2 是编排器保留给「脚本不存在」的码，不可占用"

    def test_healthy_returns_0(self, monkeypatch, synth_db, tmp_path):
        days = recent_trading_days(10, end=_d("2026-08-14"))
        db = synth_db({d.isoformat(): 30 for d in days})
        rc = self._run(monkeypatch, [
            "--db", str(db), "--snapshots", str(tmp_path / "nope"),
            "--days", "10", "--end", "2026-08-14", "--quiet",
        ])
        assert rc == 0

    def test_degraded_returns_1(self, monkeypatch, synth_db, tmp_path):
        db = synth_db({"2026-08-14": 30})
        rc = self._run(monkeypatch, [
            "--db", str(db), "--snapshots", str(tmp_path / "nope"),
            "--days", "10", "--end", "2026-08-14", "--quiet",
        ])
        assert rc == 1

    def test_json_mode_preserves_exit_code(self, monkeypatch, synth_db, tmp_path, capsys):
        """编排器用的是 `--json` 路径，它的退出码必须和表格模式一致。"""
        db = synth_db({"2026-08-14": 30})
        rc = self._run(monkeypatch, [
            "--db", str(db), "--snapshots", str(tmp_path / "nope"),
            "--days", "10", "--end", "2026-08-14", "--json",
        ])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["healthy"] is False
        # 编排器的摘要提取依赖这几个键，键名变了要在这里红
        for key in ("window", "scanned_days", "coverage",
                    "weeks_covered", "weeks_total", "longest_gap",
                    "weeks_missed"):
            assert key in payload, f"编排器 Step 10 依赖的键 {key} 消失了"

    def test_out_writes_json_file_and_keeps_exit_code(self, monkeypatch,
                                                      synth_db, tmp_path):
        """编排器走 `--out` 而非重定向 stdout。

        原因：编排器的 `log()` 用 `tee -a` **会写 stdout**，而 `run_step` 在
        "脚本不存在"(return 2) 与 TCC 权限被拒两条路径上都会 log ——
        若用 `> file` 捕获，那些日志行会混进 JSON 让下游解析失败。
        """
        db = synth_db({"2026-08-14": 30})
        out = tmp_path / "cont.json"
        rc = self._run(monkeypatch, [
            "--db", str(db), "--snapshots", str(tmp_path / "nope"),
            "--days", "10", "--end", "2026-08-14", "--quiet",
            "--out", str(out),
        ])
        assert rc == 1
        payload = json.loads(out.read_text())
        # 编排器 Step 10 的摘要提取依赖这些键
        for key in ("window", "scanned_days", "coverage", "weeks_covered",
                    "weeks_total", "longest_gap", "weeks_missed", "healthy"):
            assert key in payload, f"编排器 Step 10 依赖的键 {key} 消失了"

    def test_out_failure_does_not_change_verdict(self, monkeypatch,
                                                 synth_db, tmp_path):
        """写文件失败不能改判定 —— 判定在写盘之前就完成了。"""
        db = synth_db({"2026-08-14": 30})
        bad = tmp_path / "no_such_dir" / "cont.json"
        rc = self._run(monkeypatch, [
            "--db", str(db), "--snapshots", str(tmp_path / "nope"),
            "--days", "10", "--end", "2026-08-14", "--quiet",
            "--out", str(bad),
        ])
        assert rc == 1
        assert not bad.exists()

    def test_slack_flag_does_not_send(self, monkeypatch, synth_db, tmp_path):
        """--slack 是显式未接线的占位。对外动作要先确认，
        所以它必须什么都不发，且不改变退出码。"""
        db = synth_db({"2026-08-14": 30})
        rc = self._run(monkeypatch, [
            "--db", str(db), "--snapshots", str(tmp_path / "nope"),
            "--days", "10", "--end", "2026-08-14", "--quiet", "--slack",
        ])
        assert rc == 1


class TestSnapshotConsistency:
    def test_snapshot_dates_parsed_from_filenames(self, tmp_path):
        snaps = tmp_path / "snaps"
        snaps.mkdir()
        for name in ("NVDA_2026-08-12.json", "TSLA_2026-08-12.json",
                     "BRK-B_2026-08-13.json"):
            (snaps / name).write_text("{}")
        counts = scanned_days_from_snapshots(snaps)
        assert counts == {"2026-08-12": 2, "2026-08-13": 1}

    def test_hyphenated_ticker_does_not_break_date_parse(self, tmp_path):
        """BRK-B 含连字符，用 rpartition('_') 而非 split 才不会解析错。"""
        snaps = tmp_path / "snaps"
        snaps.mkdir()
        (snaps / "BRK-B_2026-08-13.json").write_text("{}")
        assert scanned_days_from_snapshots(snaps) == {"2026-08-13": 1}

    def test_inconsistency_is_surfaced_both_directions(self, synth_db, tmp_path):
        """有库无快照、有快照无库都要报——两个方向都是静默降级的信号。

        实测意义：本工具首次运行即独立重现了 v0.42.4 那个 save_prediction
        业务日碰撞 bug 的受害日（2026-07-07 / 07-21 有快照但无库记录）。
        """
        snaps = tmp_path / "snaps"
        snaps.mkdir()
        (snaps / "NVDA_2026-08-12.json").write_text("{}")   # 有快照
        db = synth_db({"2026-08-13": 30})                    # 有库，不同日
        res = assess(db_path=db, snap_dir=snaps,
                     since="2026-08-12", end="2026-08-13")
        assert res["consistency"]["db_only"] == ["2026-08-13"]
        assert res["consistency"]["snapshot_only"] == ["2026-08-12"]
