"""预注册前瞻检验的协议排除日（v0.45.484，`signal_archive.FORWARD_TEST_EXCLUDED_DAYS`）。

2026-10-12 生产扫描遇本机出站故障、30/30 全源 FALLBACK，当日数据由事后整日重放补算并写回；用户决定该日
不进两份预注册前瞻检验（维度 IC、共振加成）。本文件钉三件事：

1. 登记本身：条目齐全，且登记日早于该日任何 T+7 结果可能存在的日子（否则排除可能受结果影响）；
2. 两个执行器真的跳过它、且**说出来**（维度 IC 的 run() 报 `forward_test_excluded_days`，共振加成计入 skipped）——
   各配一条「清空名单就回来」的对照，证明跳过来自名单而不是夹具碰巧缺数据；
3. 它不是 `QUARANTINE`：不作用于入库口——重放写回的值要留在库里给别的读者用（v0.45.484 的设计选择）。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path


import signal_archive
from tests.test_dim_ic_forward_test import _day_rows, _make_db

_ROOT = Path(__file__).resolve().parent.parent
_DAY = "2026-10-12"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = _load("dim_ic_forward_test_x", "experiments/dim_ic_forward_test.py")
P = _load("dim_ic_protocol_x", "experiments/dim_ic_protocol.py")
fwd = _load("resonance_boost_forward_test_x", "experiments/resonance_boost_forward_test.py")


class TestRegistry:
    def test_entry_complete(self):
        e = signal_archive.FORWARD_TEST_EXCLUDED_DAYS[_DAY]
        assert {"version", "registered", "reason", "evidence"} <= set(e)
        assert e["reason"] and e["evidence"]

    def test_registered_before_any_outcome(self):
        """T+7 最早也要 7 个日历日之后才可能到期——登记日必须早于它（宽松下界，真实交易日只会更晚）。"""
        for day, e in signal_archive.FORWARD_TEST_EXCLUDED_DAYS.items():
            earliest_outcome = dt.date.fromisoformat(day) + dt.timedelta(days=7)
            assert dt.date.fromisoformat(e["registered"]) < earliest_outcome, day

    def test_helper(self):
        assert signal_archive.forward_test_excluded(_DAY)
        assert signal_archive.forward_test_excluded(_DAY + " 14:11:56")
        assert not signal_archive.forward_test_excluded("2026-10-13")

    def test_not_a_write_path_quarantine(self):
        """排除日的信号不进 `QUARANTINE`：入库口照常收（is_quarantined 为假）。"""
        for sig in ("sentiment.pct", "agent.BuzzBeeWhisper.score", "price.momentum_5d"):
            assert not signal_archive.is_quarantined(_DAY, "NVDA", sig)
        # 该日可以有针对单只标的、单个信号的 QUARANTINE 条目（v0.45.499 的 BILI 假 IV 即是），但不能有整天全标的的——
        # 那等于用入库口隔离实现了「整天排除」，重放写回的对值会被 backfill / purge 抹掉
        assert not [q for q in signal_archive.QUARANTINE
                    if q["date"] in signal_archive.FORWARD_TEST_EXCLUDED_DAYS and q.get("tickers") is None]


class TestDimIcSkips:
    def _rows(self, tmp_path):
        rows = _day_rows(_DAY) + _day_rows("2026-10-13")
        return fx.load_rows(_make_db(tmp_path, rows), P, "2026-12-31")

    def test_excluded_day_dropped(self, tmp_path):
        dates = {r["date"] for r in self._rows(tmp_path)}
        assert _DAY not in dates and "2026-10-13" in dates

    def test_control_list_emptied_brings_it_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(signal_archive, "FORWARD_TEST_EXCLUDED_DAYS", {})
        assert _DAY in {r["date"] for r in self._rows(tmp_path)}

    def test_run_reports_it(self, tmp_path):
        res = fx.run(db_path=str(_make_db(tmp_path, _day_rows(_DAY))), today="2026-10-20", history=[])
        assert res["forward_test_excluded_days"] == [_DAY]


class TestResonanceSkips:
    def _write(self, home, day):
        sr = {"ticker": "NVDA", "agent_details": {"ScoutBeeNova": {"score": 5.0}}, "data_quality": {"x": 1},
              "base_score_before_resonance": 5.0, "gex_regime_mod": {}, "final_score": 5.0, "direction": "neutral"}
        (home / f"analysis-NVDA-ml-{day}.json").write_text(json.dumps({"swarm_results": sr}), encoding="utf-8")

    def test_excluded_day_skipped_and_counted(self, tmp_path):
        self._write(tmp_path, _DAY)
        self._write(tmp_path, "2026-10-13")
        out = fwd.load_reports(tmp_path)
        assert [r["date"] for r in out["rows"]] == ["2026-10-13"]
        assert sum(v for k, v in out["skipped"].items() if "协议排除日" in k) == 1

    def test_control_list_emptied_brings_it_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(signal_archive, "FORWARD_TEST_EXCLUDED_DAYS", {})
        self._write(tmp_path, _DAY)
        assert [r["date"] for r in fwd.load_reports(tmp_path)["rows"]] == [_DAY]
