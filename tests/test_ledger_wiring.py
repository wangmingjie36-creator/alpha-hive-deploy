"""v0.45.448：组合 Greeks / 纸面组合接上 `ledger_io` 之后的接线——跑的时候持目录锁、坏行停下来且看得见。

纸面组合的状态目录由 conftest 的自动夹具逐测试指到沙箱（`_pp.POSITIONS_FILE` 等）；组合 Greeks 的在这里自己指。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ledger_io as L
import paper_portfolio as pp
import portfolio_greeks as pg

FG = {"value": 50.0, "is_cnn": True}          # 显式给 F&G：不走实时 / 归档查询


def _analyze(tmp_path: Path, counters: dict):
    import alert_manager as am
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": counters, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return a, [x for x in a.alerts if "纸面组合" in x.message]


@pytest.fixture
def no_pp_run(monkeypatch):
    monkeypatch.setattr(pp, "_LAST_RUN", None)


class TestPaperPortfolioRunIsVisible:

    def test_truncated_positions_line_stops_the_run_and_is_recorded(self, no_pp_run):
        pp.POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        pp.POSITIONS_FILE.write_text('{"ticker": "AAPL", "shares": 10}\n{"ticker": "MS', encoding="utf-8")  # 半行
        before = pp.POSITIONS_FILE.read_bytes()
        with pytest.raises(L.LedgerCorrupt, match="positions.jsonl 第 2 行"):
            pp.run_for_date("2026-10-08", market_fear_greed=FG)
        st = pp.run_stats()
        assert st["ok"] is False and st["as_of"] == "2026-10-08" and "LedgerCorrupt" in st["error"], st
        assert pp.POSITIONS_FILE.read_bytes() == before, "坏文件上不许接着写"

    def test_failure_reaches_status_and_raises_p2(self, tmp_path, no_pp_run, monkeypatch):
        import alert_manager as am
        import scan_timing as stt

        def boom(*a, **k):
            raise L.LedgerLockTimeout("另一个写者卡住了")
        monkeypatch.setattr(pp, "_run_for_date", boom)
        with pytest.raises(L.LedgerLockTimeout):
            pp.run_for_date("2026-10-08", market_fear_greed=FG)
        c = stt.counters()
        assert c["paper_portfolio"]["error"].startswith("LedgerLockTimeout"), c["paper_portfolio"]
        _, hits = _analyze(tmp_path, {"paper_portfolio": c["paper_portfolio"]})
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.MEDIUM and "异常中断" in hits[0].message
        assert "纸面组合异常中断" in stt.summary_line({"phases": {}, "counters": {"paper_portfolio": c["paper_portfolio"]}})

    def test_success_is_recorded_and_silent(self, tmp_path, no_pp_run, monkeypatch):
        monkeypatch.setattr(pp, "_run_for_date", lambda *a, **k: {"nav": 1.0})
        pp.run_for_date("2026-10-08", market_fear_greed=FG)
        assert pp.run_stats() == {"as_of": "2026-10-08", "ok": True, "error": None}
        _, hits = _analyze(tmp_path, {"paper_portfolio": pp.run_stats()})
        assert hits == []

    def test_missing_counter_is_a_skipped_check_not_a_clean_one(self, tmp_path):
        a, hits = _analyze(tmp_path, {"paper_portfolio": None})
        assert hits == [] and any("纸面组合" in s for s in a.checks_skipped), a.checks_skipped

    def test_the_whole_run_holds_the_state_dir_lock(self, no_pp_run, monkeypatch):
        seen = []
        monkeypatch.setattr(pp, "_run_for_date",
                            lambda *a, **k: seen.append(L.is_locked_here(pp.POSITIONS_FILE.parent)) or {})
        pp.run_for_date("2026-10-08", market_fear_greed=FG)
        assert seen == [True] and not L.is_locked_here(pp.POSITIONS_FILE.parent)


class TestGreeksRunHoldsTheLockOnlyWhenItWrites:

    @pytest.fixture
    def hedge_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "hedge_state"
        monkeypatch.setattr(pg, "STATE_DIR", d)
        return d

    @pytest.mark.parametrize("execute, locked", [(True, True), (False, False)])
    def test_lock_follows_execute(self, hedge_dir, monkeypatch, execute, locked):
        seen = []
        monkeypatch.setattr(pg, "_run_for_date", lambda *a, **k: seen.append(L.is_locked_here(hedge_dir)) or {})
        pg.run_for_date("2026-10-08", execute=execute)
        assert seen == [locked] and not L.is_locked_here(hedge_dir)


class TestMigratedHelpersAreStrict:
    """两个模块的旧助手名照旧可用，但行为换成了 ledger_io 的：写不出非法 JSON、读不收坏行。"""

    def test_paper_portfolio_refuses_nan_rows(self, tmp_path):
        p = tmp_path / "equity_curve.jsonl"
        with pytest.raises(L.LedgerWriteError):
            pp._write_jsonl(p, [{"date": "2026-10-08", "nav": float("nan")}])
        assert not p.exists()

    def test_greeks_still_scrubs_nan_to_null(self, tmp_path):
        """portfolio_greeks 一直先 `_scrub`（NaN → null）再写；迁移不改这一口径。"""
        p = tmp_path / "equity_curve.jsonl"
        pg._write_jsonl(p, [{"date": "2026-10-08", "nav": float("nan")}])
        assert L.load_jsonl(p) == [{"date": "2026-10-08", "nav": None}]

    @pytest.mark.parametrize("mod", [pp, pg])
    def test_loaders_reject_non_object_lines(self, tmp_path, mod):
        p = tmp_path / "x.jsonl"
        p.write_text('{"a": 1}\n"str"\n', encoding="utf-8")
        with pytest.raises(L.LedgerCorrupt):
            mod._load_jsonl(p)
