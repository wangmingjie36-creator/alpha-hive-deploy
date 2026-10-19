"""
tests/test_llm_shadow_report.py — v0.45.555：LLM 影子 A/B 的读者

核心要保住的不变式：
  · 规则臂 = 生产同日同标的的 final_score / direction，**不是**影子 swarm_results 里的 rule_score
  · 没到预注册样本量之前，「谁更准」那段代码**不执行**（不是算了不显示）
  · 缺失（生产里没有 / 结果未出）如实计数，不被当成「两边都错」
  · 坏状态文件抛 LedgerCorrupt，不静默少一天
"""
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import llm_shadow_report as rep


# ==================== 夹具 ====================

def _status(day, per_ticker, *, healthy=True, mode="run", cost=0.5):
    return {"schema": 1, "date": day, "mode": mode, "model": "claude-opus-5-5",
            "usage": {"total_cost_usd": cost},
            "quality": {"healthy": healthy, "reasons": [] if healthy else ["x"]},
            "per_ticker": per_ticker}


def _pt(final, direction, rule=None, present=True):
    return {"present": present, "final_score": final, "direction": direction,
            "rule_score": rule if rule is not None else final, "rule_direction": direction}


def _write(root, doc):
    d = Path(root) / rep.STATUS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc['date']}.json").write_text(json.dumps(doc), encoding="utf-8")


def _prod_db(path, rows):
    """rows: (date, ticker, final_score, direction, price_at_predict, close_t7)"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, final_score REAL, "
                "direction TEXT, price_at_predict REAL, close_t7 REAL)")
    con.executemany("INSERT INTO predictions(date,ticker,final_score,direction,price_at_predict,close_t7) "
                    "VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return Path(path)


# ==================== 纯函数 ====================

class TestMcNemar:
    @pytest.mark.parametrize("b,c,p", [(10, 0, 2 / 1024), (0, 10, 2 / 1024), (5, 5, 1.0), (0, 0, 1.0)])
    def test_known_values(self, b, c, p):
        assert rep.mcnemar_exact_p(b, c) == pytest.approx(p)

    def test_symmetric_and_bounded(self):
        for b, c in [(3, 9), (9, 3), (40, 20), (20, 40)]:
            assert rep.mcnemar_exact_p(b, c) == pytest.approx(rep.mcnemar_exact_p(c, b))
            assert 0 < rep.mcnemar_exact_p(b, c) <= 1

    def test_40_vs_20_is_about_001(self):
        assert 0.005 < rep.mcnemar_exact_p(40, 20) < 0.02       # 预注册文档里举的例子


class TestLoadDays:
    def test_missing_dir_is_empty(self, tmp_path):
        assert rep.load_days(tmp_path) == []

    def test_only_requested_mode_and_sorted(self, tmp_path):
        _write(tmp_path, _status("2026-10-20", {}))
        _write(tmp_path, _status("2026-10-19", {}))
        _write(tmp_path, _status("2026-10-21", {}, mode="control"))
        assert [d["date"] for d in rep.load_days(tmp_path)] == ["2026-10-19", "2026-10-20"]
        assert [d["date"] for d in rep.load_days(tmp_path, "control")] == ["2026-10-21"]

    def test_corrupt_file_raises_not_skipped(self, tmp_path):
        import ledger_io
        _write(tmp_path, _status("2026-10-19", {}))
        (tmp_path / rep.STATUS_DIRNAME / "2026-10-20.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ledger_io.LedgerCorrupt):
            rep.load_days(tmp_path)


class TestLlmRows:
    def test_unhealthy_days_excluded(self):
        days = [_status("2026-10-19", {"A": _pt(6.0, "bullish")}, healthy=False),
                _status("2026-10-20", {"A": _pt(6.0, "bullish")})]
        assert [r["date"] for r in rep.llm_rows(days)] == ["2026-10-20"]

    def test_incomplete_tickers_excluded(self):
        d = _status("2026-10-19", {"A": _pt(None, "bullish"), "B": _pt(6.0, "weird"),
                                   "C": {"present": False}, "D": _pt(6.0, "bullish")})
        assert [r["ticker"] for r in rep.llm_rows([d])] == ["D"]

    def test_nan_score_excluded(self):
        d = _status("2026-10-19", {"A": _pt(float("nan"), "bullish")})
        assert rep.llm_rows([d]) == []

    def test_stage_rule_score_is_carried_only_for_decomposition(self):
        d = _status("2026-10-19", {"A": _pt(7.0, "bullish", rule=6.0)})
        (r,) = rep.llm_rows([d])
        assert r["llm_score"] == 7.0 and r["stage_rule_score"] == 6.0
        assert "prod_score" not in r                  # 规则臂不在这里取


# ==================== 与生产对账 ====================

class TestJoinProduction:
    def test_rule_arm_is_production_not_stage_rule_score(self, tmp_path):
        """核心不变式：规则臂取生产库，哪怕影子的 rule_score 恰好是另一个值。"""
        db = _prod_db(tmp_path / "p.db", [("2026-10-19", "A", 5.5, "neutral", 100.0, 103.0)])
        rows = rep.llm_rows([_status("2026-10-19", {"A": _pt(7.0, "bullish", rule=6.9)})])
        (p,), missing = rep.join_production(db, rows)
        assert (p["prod_score"], p["prod_dir"]) == (5.5, "neutral")
        assert p["llm_score"] == 7.0 and p["stage_rule_score"] == 6.9 and missing == 0
        assert p["ret"] == pytest.approx(3.0)

    def test_missing_production_row_counted_not_dropped_silently(self, tmp_path):
        db = _prod_db(tmp_path / "p.db", [("2026-10-19", "A", 5.5, "neutral", 100.0, 103.0)])
        rows = rep.llm_rows([_status("2026-10-19", {"A": _pt(7.0, "bullish"), "B": _pt(6.0, "bearish")})])
        pairs, missing = rep.join_production(db, rows)
        assert [p["ticker"] for p in pairs] == ["A"] and missing == 1

    def test_unmatured_outcome_is_none_not_zero(self, tmp_path):
        db = _prod_db(tmp_path / "p.db", [("2026-10-19", "A", 5.5, "neutral", 100.0, None)])
        rows = rep.llm_rows([_status("2026-10-19", {"A": _pt(7.0, "bullish")})])
        (p,), _ = rep.join_production(db, rows)
        assert p["ret"] is None

    @pytest.mark.parametrize("px", [0, None])
    def test_bad_entry_price_gives_none(self, tmp_path, px):
        db = _prod_db(tmp_path / "p.db", [("2026-10-19", "A", 5.5, "neutral", px, 103.0)])
        rows = rep.llm_rows([_status("2026-10-19", {"A": _pt(7.0, "bullish")})])
        (p,), _ = rep.join_production(db, rows)
        assert p["ret"] is None

    def test_production_db_opened_read_only(self, tmp_path):
        db = _prod_db(tmp_path / "p.db", [("2026-10-19", "A", 5.5, "neutral", 100.0, 103.0)])
        before = db.read_bytes()
        rep.join_production(db, rep.llm_rows([_status("2026-10-19", {"A": _pt(7.0, "bullish")})]))
        assert db.read_bytes() == before


class TestDescribe:
    def _pairs(self):
        return [
            {"llm_score": 7.0, "llm_dir": "bullish", "prod_score": 6.0, "prod_dir": "neutral", "stage_rule_score": 6.5, "ret": 1.0},
            {"llm_score": 5.0, "llm_dir": "bearish", "prod_score": 6.0, "prod_dir": "bearish", "stage_rule_score": 5.5, "ret": 1.0},
        ]

    def test_numbers(self):
        d = rep.describe([_status("2026-10-19", {})], self._pairs(), 3)
        assert d["pairs"] == 2 and d["missing_in_production"] == 3
        assert d["mean_score_diff"] == pytest.approx(0.0)
        assert d["mean_abs_score_diff"] == pytest.approx(1.0)
        assert d["direction_flip_rate"] == pytest.approx(0.5)
        assert d["mean_queen_layer_effect"] == pytest.approx((0.5 + -0.5) / 2)
        assert d["mean_bee_layer_effect"] == pytest.approx((0.5 + -0.5) / 2)

    def test_empty_gives_none_not_zero(self):
        d = rep.describe([], [], 0)
        assert d["mean_score_diff"] is None and d["direction_flip_rate"] is None

    def test_description_contains_no_accuracy_information(self):
        d = rep.describe([_status("2026-10-19", {})], self._pairs(), 0)
        assert not any("correct" in k or "hit" in k or "mcnemar" in k for k in d)

    def test_spend_summed_over_all_run_days(self):
        d = rep.describe([_status("2026-10-19", {}, cost=1.25), _status("2026-10-20", {}, healthy=False, cost=0.75)], [], 0)
        assert d["spend_usd"] == pytest.approx(2.0) and d["unhealthy_days"] == 1


# ==================== 检验与门槛 ====================

def _pair(llm_dir, prod_dir, ret):
    return {"llm_dir": llm_dir, "prod_dir": prod_dir, "ret": ret}


class TestScoring:
    def test_discordant_decided_needs_different_directions_and_outcome(self):
        ps = [_pair("bullish", "bullish", 5.0), _pair("bullish", "bearish", 5.0), _pair("bullish", "bearish", None)]
        assert len(rep.discordant_decided(ps)) == 1

    def test_arm_attribution(self):
        # 涨 +8%：看多对、看空错
        r = rep.score_discordant([_pair("bullish", "bearish", 8.0)])
        assert (r["llm_only_correct"], r["rule_only_correct"]) == (1, 0)
        r = rep.score_discordant([_pair("bearish", "bullish", 8.0)])
        assert (r["llm_only_correct"], r["rule_only_correct"]) == (0, 1)

    def test_ambiguous_is_excluded_not_counted_as_wrong(self):
        r = rep.score_discordant([_pair("bullish", "bearish", 0.1)])     # 涨跌落在模糊带内
        assert r["ambiguous_excluded"] == 1
        assert r["llm_only_correct"] == r["rule_only_correct"] == r["both_wrong"] == 0

    def test_both_wrong(self):
        r = rep.score_discordant([_pair("bullish", "neutral", -9.0)])     # 大跌：看多错，中性也错
        assert r["both_wrong"] == 1


class TestGating:
    def _fill(self, tmp_path, n_days, per_day, *, healthy=True):
        """n_days 个健康日，每天 per_day 只标的，LLM 全看多、生产全看空，T+7 全涨 8%（LLM 全对）。"""
        rows, root = [], tmp_path / "shadow"
        start = date(2026, 9, 1)
        for i in range(n_days):
            ds = (start + timedelta(days=i)).isoformat()
            _write(root, _status(ds, {f"T{j}": _pt(7.0, "bullish") for j in range(per_day)}, healthy=healthy))
            rows += [(ds, f"T{j}", 4.0, "bearish", 100.0, 108.0) for j in range(per_day)]
        return root, _prod_db(tmp_path / "p.db", rows)

    def test_not_ready_by_days_never_runs_scoring(self, tmp_path, monkeypatch):
        root, db = self._fill(tmp_path, n_days=rep.MIN_HEALTHY_DAYS - 1, per_day=10)    # 样本够多，但天数不够
        monkeypatch.setattr(rep, "score_discordant", lambda *_: pytest.fail("未达门槛却计算了命中率"))
        r = rep.build_report(root, db)
        assert r["ready"] is False and r["result"] is None
        assert r["discordant_decided"] >= rep.MIN_DISCORDANT

    def test_not_ready_by_pairs_never_runs_scoring(self, tmp_path, monkeypatch):
        root, db = self._fill(tmp_path, n_days=rep.MIN_HEALTHY_DAYS, per_day=1)         # 天数够，成对数不够（20 < 60）
        monkeypatch.setattr(rep, "score_discordant", lambda *_: pytest.fail("未达门槛却计算了命中率"))
        r = rep.build_report(root, db)
        assert r["ready"] is False and r["discordant_decided"] == rep.MIN_HEALTHY_DAYS

    def test_unhealthy_days_do_not_count_toward_threshold(self, tmp_path, monkeypatch):
        root, db = self._fill(tmp_path, n_days=rep.MIN_HEALTHY_DAYS + 5, per_day=10, healthy=False)
        monkeypatch.setattr(rep, "score_discordant", lambda *_: pytest.fail("不健康的日子被算进了门槛"))
        r = rep.build_report(root, db)
        assert r["ready"] is False and r["describe"]["healthy_days"] == 0

    def test_ready_computes_and_attributes_correctly(self, tmp_path):
        root, db = self._fill(tmp_path, n_days=rep.MIN_HEALTHY_DAYS, per_day=4)          # 80 ≥ 60
        r = rep.build_report(root, db)
        assert r["ready"] is True
        res = r["result"]
        assert res["llm_only_correct"] == 80 and res["rule_only_correct"] == 0
        assert res["mcnemar_p"] < 0.001

    def test_no_production_db_is_not_ready_and_reports_missing(self, tmp_path):
        root = tmp_path / "shadow"
        _write(root, _status("2026-10-19", {"A": _pt(7.0, "bullish")}))
        r = rep.build_report(root, tmp_path / "absent.db")
        assert r["ready"] is False and r["describe"]["missing_in_production"] == 1

    def test_empty_shadow_root(self, tmp_path):
        r = rep.build_report(tmp_path / "nope", tmp_path / "absent.db")
        assert r["describe"]["run_days"] == 0 and r["ready"] is False


class TestNoiseBaseline:
    def test_control_days_measure_input_noise_against_production(self, tmp_path):
        root = tmp_path / "shadow"
        _write(root, _status("2026-10-19", {"A": _pt(6.5, "bullish"), "B": _pt(5.0, "neutral")}, mode="control"))
        db = _prod_db(tmp_path / "p.db", [("2026-10-19", "A", 6.0, "bullish", 100.0, None),
                                          ("2026-10-19", "B", 5.0, "bearish", 100.0, None)])
        d = rep.build_report(root, db)["describe"]
        assert d["noise_control_days"] == 1 and d["noise_control_pairs"] == 2
        assert d["noise_mean_abs_score_diff"] == pytest.approx(0.25)
        assert d["noise_direction_flip_rate"] == pytest.approx(0.5)
        assert d["run_days"] == 0                     # 控制日不算 LLM 日

    def test_no_control_days_gives_none(self, tmp_path):
        d = rep.build_report(tmp_path / "nope", tmp_path / "absent.db")["describe"]
        assert d["noise_control_days"] == 0 and d["noise_mean_abs_score_diff"] is None


# ==================== 命令行 ====================

class TestCli:
    def test_main_prints_waiting_message_below_threshold(self, tmp_path, capsys, monkeypatch):
        root = tmp_path / "shadow"
        _write(root, _status("2026-10-19", {"A": _pt(7.0, "bullish")}))
        prod = tmp_path / "prod"
        prod.mkdir()
        db = _prod_db(prod / "pheromone.db", [("2026-10-19", "A", 5.0, "bearish", 100.0, 108.0)])
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(prod))
        monkeypatch.delenv("ALPHA_HIVE_DB_PATH", raising=False)
        assert rep.main(["--shadow-root", str(root)]) == 0
        out = capsys.readouterr().out
        assert "未达预注册样本量" in out and "McNemar" not in out
        assert db.exists()

    def test_subprocess_smoke(self, tmp_path):
        import os
        import subprocess
        repo = Path(__file__).resolve().parent.parent
        (tmp_path / "prod").mkdir()
        env = dict(os.environ, ALPHA_HIVE_HOME=str(tmp_path / "prod"))
        r = subprocess.run([sys.executable, str(repo / "llm_shadow_report.py"), "--shadow-root", str(tmp_path / "s")],
                           env=env, capture_output=True, text=True, cwd=str(repo), timeout=60)
        assert r.returncode == 0, r.stderr[-600:]
        assert "未达预注册样本量" in r.stdout
