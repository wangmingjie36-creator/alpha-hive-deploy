"""cloud-snapshots 由**读者自己**刷新、陈旧可见、每日观测（v0.45.466）。

阶段 8 后生产代码在独立克隆里，克隆的 `origin/cloud-snapshots` 只有 `git fetch` 会动，而克隆里只有
production_sync 的 `fetch origin main`——此前它的新鲜度全靠开发会话顺手 fetch。陈旧时补跑看到的是
「那天没有快照」，与 routine 真没产出分不开，于是降级用当天的期权数据。

这里全用真 git：本地裸仓库扮演 GitHub（origin 是本地路径 ⇒ conftest 放行，fetch 不出网）。
`_Remote` 的本地克隆停在 D1，云端已有 D2——正是「前一天的快照本地没拉到」的形状。
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alert_manager as am  # noqa: E402
import cloud_snapshot_loader as csl  # noqa: E402
from is_trading_day import is_trading_day  # noqa: E402
from tests.test_cloud_snapshot_loader import _ticker_json  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
D1, D2, LATER = "2026-08-19", "2026-08-20", "2026-08-21"


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


class _Remote:
    """裸仓库 = 云端；work = routine；local = 生产克隆（只经 fetch 看到云端）。"""

    def __init__(self, tmp: Path):
        self.work, self.bare, self.local = tmp / "work", tmp / "origin.git", tmp / "local"
        self.work.mkdir()
        _git(self.work, "init", "-q", "-b", "cloud-snapshots")
        _git(tmp, "init", "-q", "--bare", str(self.bare))
        _git(self.work, "remote", "add", "origin", str(self.bare))

    def add_day(self, date: str) -> None:
        day = self.work / csl.SNAPSHOT_SUBDIR / date
        day.mkdir(parents=True)
        (day / "NVDA.json").write_text(json.dumps(_ticker_json(vintage=date)))
        (day / "manifest.json").write_text(json.dumps({"date": date, "tickers_ok": 1}))
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-qm", date)
        _git(self.work, "push", "-q", "origin", "cloud-snapshots")

    def clone(self) -> str:
        _git(self.work.parent, "clone", "-q", str(self.bare), str(self.local))
        return str(self.local)


@pytest.fixture
def remote(tmp_path):
    r = _Remote(tmp_path)
    r.add_day(D1)
    r.path = r.clone()           # 本地停在 D1
    r.add_day(D2)                # 云端已有 D2
    return r


class TestRefresh:
    def test_refresh_brings_the_local_ref_up_to_date(self, remote):
        assert csl.available_dates(repo=remote.path) == [D1]
        rec = csl.refresh(repo=remote.path)
        assert rec["ok"] and rec["error"] is None and rec["newest_date"] == D2, rec
        assert csl.available_dates(repo=remote.path) == [D1, D2]

    def test_failure_is_returned_not_raised(self, remote):
        _git(remote.path, "remote", "set-url", "origin", "/nonexistent/origin.git")
        rec = csl.refresh(repo=remote.path)
        assert rec["ok"] is False and rec["error"] and rec["newest_date"] == D1, rec

    def test_tests_never_fetch_from_a_real_remote(self, remote, request):
        """conftest 的闸有牙：origin 不是本地路径 ⇒ 不出网、按断网处理并记账。"""
        _git(remote.path, "remote", "set-url", "origin", "git@github.com:someone/elsewhere.git")
        rec = csl.refresh(repo=remote.path)
        assert rec["ok"] is False and "测试默认离线" in rec["error"], rec
        assert request.node._snapshot_fetch_calls, "闸没记账"


class TestWhyMissing:
    def test_date_inside_history_is_really_absent(self, remote):
        kind, why = csl.why_missing("2026-08-18", repo=remote.path)    # 早于本地最新：分支只追加
        assert kind == "absent" and "routine 没产出" in why, why

    def test_not_refreshed_is_stale(self, remote):
        kind, why = csl.why_missing(D2, repo=remote.path)
        assert kind == "ref_stale" and "没刷新过" in why and D1 in why, why

    def test_refresh_failed_is_stale(self, remote):
        _git(remote.path, "remote", "set-url", "origin", "/nonexistent/origin.git")
        csl.refresh(repo=remote.path)
        kind, why = csl.why_missing(D2, repo=remote.path)
        assert kind == "ref_stale" and "刷新失败" in why, why

    def test_refreshed_and_still_missing_is_absent(self, remote):
        csl.refresh(repo=remote.path)
        kind, why = csl.why_missing(LATER, repo=remote.path)
        assert kind == "absent" and "云端确实没有" in why and D2 in why, why

    def test_explicit_other_ref_is_not_judged_for_freshness(self, remote):
        assert csl.why_missing(D2, ref="HEAD", repo=remote.path)[0] == "absent"


class TestReadersSayWhich:
    def test_official_close_tells_stale_from_absent(self, remote, monkeypatch):
        monkeypatch.setattr(csl, "REPO_DIR", remote.path)
        assert csl.load_official_close(D2, "NVDA") == (None, "snapshot_ref_stale")
        csl.refresh()
        assert csl.load_official_close(D2, "NVDA") == (210.0, csl.SNAP_CLOSE)
        assert csl.load_official_close(LATER, "NVDA") == (None, "snapshot_no_snapshot")

    def test_snapshot_mode_says_the_ref_may_be_stale(self, remote, monkeypatch):
        monkeypatch.setattr(csl, "REPO_DIR", remote.path)
        with pytest.raises(csl.SnapshotUnavailable, match="没刷新过"):
            with csl.snapshot_mode(D2):
                pass

    def test_backfill_entry_refreshes_then_uses_the_snapshot(self, remote, monkeypatch, capsys):
        """补跑 D2：本地只到 D1，入口先刷新 ⇒ 真用上 D2 的快照（不再降级成当天的期权数据）。"""
        import alpha_hive_daily_report as ahdr

        class _Args:
            date, no_snapshot = D2, False
        monkeypatch.setattr(csl, "REPO_DIR", remote.path)
        with ahdr._snapshot_ctx(_Args()) as prov:
            assert prov is not None, capsys.readouterr().out
        out = capsys.readouterr().out
        assert f"已刷新：最新快照 {D2}" in out and "云端快照模式" in out, out

    def test_backfill_with_no_snapshot_still_refreshes(self, remote, monkeypatch, capsys):
        """`--no-snapshot` 不走快照模式，但收盘价兜底仍读 cloud-snapshots ⇒ 照样刷新。"""
        import alpha_hive_daily_report as ahdr

        class _Args:
            date, no_snapshot = D2, True
        monkeypatch.setattr(csl, "REPO_DIR", remote.path)
        with ahdr._snapshot_ctx(_Args()):
            pass
        assert csl.available_dates() == [D1, D2]

    def test_entry_price_backfill_real_source_refreshes(self, remote, monkeypatch):
        import entry_price_backfill as epb
        monkeypatch.setattr(csl, "REPO_DIR", remote.path)
        rows = [{"id": 1, "date": D2, "ticker": "NVDA", "price_at_predict": 0.0}]
        dec = epb.plan(rows, yf_closes=lambda *a: {}, td_closes=lambda *a: {})
        assert dec[0]["cboe"] == {"snap_close": 210.0}, dec


class TestDailyLag:
    @staticmethod
    def _lag(monkeypatch, newest, today, ok=True):
        monkeypatch.setattr(csl, "refresh", lambda **k: {"ok": ok, "error": None if ok else "boom",
                                                          "at": "t", "newest_date": newest})
        return csl.lag_summary(dt.date.fromisoformat(today))

    def test_weekend_is_not_lag(self, monkeypatch):
        s = self._lag(monkeypatch, "2026-10-09", "2026-10-12")           # 周五 → 周一
        assert (s["available"], s["lag"], s["missing_sessions"]) == (True, 1, ["2026-10-12"]), s

    def test_holiday_is_not_lag(self, monkeypatch):
        assert is_trading_day(dt.date(2026, 11, 26))[0] is False, "前提：感恩节休市（日历过期会让本条失去意义）"
        s = self._lag(monkeypatch, "2026-11-25", "2026-11-27")
        assert s["lag"] == 1 and s["missing_sessions"] == ["2026-11-27"], s

    def test_real_lag_is_counted(self, monkeypatch):
        s = self._lag(monkeypatch, "2026-10-06", "2026-10-12")
        assert s["lag"] == 4 and s["missing_sessions"][0] == "2026-10-07", s
        assert s["max_lag"] == csl.MAX_LAG_SESSIONS

    def test_no_snapshot_at_all(self, monkeypatch):
        s = self._lag(monkeypatch, None, "2026-10-12")
        assert s["available"] is False and "没有任何快照" in s["reason"]

    def test_refresh_outcome_is_carried(self, monkeypatch):
        s = self._lag(monkeypatch, "2026-10-09", "2026-10-12", ok=False)
        assert s["refresh_ok"] is False and s["refresh_error"] == "boom"

    def test_daily_scan_writes_it_into_scan_timing(self):
        """`main` 写进 `_timing.write(extra=…)` 的键里有 cloud_snapshots（编排器并进 status.json，告警读它）。"""
        tree = ast.parse((REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        keys = set()
        for node in ast.walk(main):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "_timing"):
                for kw in node.keywords:
                    if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                        keys |= {k.value for k in kw.value.keys if isinstance(k, ast.Constant)}
        assert "cloud_snapshots" in keys, keys


def _status(cs="__absent__", step2="success"):
    extra = {"gh_pages": {"success": True}}
    if cs != "__absent__":
        extra["cloud_snapshots"] = cs
    return {"status": "success", "total_duration_seconds": 2000,
            "steps_result": {"step2_hive_analysis": {"status": step2}},
            "scan_timing": {"extra": extra, "production_sync": {"outcome": "up_to_date"}}}


def _cs(lag=1, ok=True, newest="2026-10-09", missing=None):
    return {"available": True, "refresh_ok": ok, "refresh_error": None if ok else "fatal: unable to access",
            "refreshed_at": "t", "newest_date": newest, "max_lag": csl.MAX_LAG_SESSIONS, "lag": lag,
            "missing_sessions": missing if missing is not None else [f"d{i}" for i in range(lag)]}


def _hits(tmp_path, status):
    a = am.AlertAnalyzer(report_dir=tmp_path)
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False))
    a.analyze(p)
    return [x for x in a.alerts if "云端快照" in x.message or "cloud-snapshots" in x.message]


class TestAlert:
    def test_normal_day_is_quiet(self, tmp_path):
        assert _hits(tmp_path, _status(_cs(lag=1))) == []

    def test_lag_beyond_allowance_is_p2(self, tmp_path):
        hits = _hits(tmp_path, _status(_cs(lag=3)))
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM] and "落后 3" in hits[0].message, hits

    def test_refresh_failure_is_p2_once(self, tmp_path):
        hits = _hits(tmp_path, _status(_cs(lag=5, ok=False)))
        assert len(hits) == 1 and "刷新失败" in hits[0].message, [h.message for h in hits]

    def test_missing_observation_is_p2_not_silence(self, tmp_path):
        hits = _hits(tmp_path, _status())
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM] and "未观测" in hits[0].message

    def test_scan_that_did_not_run_is_not_judged(self, tmp_path):
        assert _hits(tmp_path, _status(_cs(lag=9), step2="failed")) == []
