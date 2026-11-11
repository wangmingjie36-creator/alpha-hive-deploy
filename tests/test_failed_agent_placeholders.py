"""v0.45.768：出错蜂的占位值（`make_error_result` 的 5.0 / 中性）不再流进不看 error 键的读者。

用户 2026-11-11 对 v0.45.767 报告里「出错蜂的中性方向仍流进 agent_agreement / 共识度归档 / 逐蜂准确率；5.0 进 signal_archive」
答「一起处理了」。蜂后自己从来不用这些占位（`valid_results` 按 error 键排除），漏的是四个下游：

1. 蜂后的 `agent_directions`（从全体结果建）⇒ ML 特征 `agent_agreement`（训练端读 `predictions.agent_directions`、服务端读这里，
   同一公式）与逐蜂准确率诊断。改在源头：只记有效结果（同 `agent_breakdown`），训练 / 服务一起变。
2. `signal_archive` 的 `agent.<蜂>.score` / `.direction` 存下占位；
3. `composite.swarm_agreement` 把占位「中性」算进多数派占比；
4. 网站「蜂群分歧度」把占位 5.0 算进标准差 / 极差 / 共识度。

判据唯一实现 `models.agent_record_failed`：error 键为真，或 discovery 以 Error / ❌ 开头（老记录只有前缀）。

⚠️ 全部离线、不用 skip；蜂后用真实 `QueenDistiller`，结果用真实 `make_error_result` 造。
"""
from __future__ import annotations

import copy

import pytest

import dashboard_renderer as dr
import signal_archive as sa
from models import agent_record_failed
from pheromone_board import PheromoneBoard
from swarm_agents.queen_distiller import QueenDistiller
from swarm_agents.utils import make_error_result

TICKER = "TEST"

_DIMS = {"ScoutBeeNova": "signal", "BuzzBeeWhisper": "sentiment", "ChronosBeeHorizon": "catalyst",
         "OracleBeeEcho": "odds", "GuardBeeSentinel": "risk_adj", "RivalBeeVanguard": "ml_auxiliary",
         "BearBeeContrarian": "contrarian"}
#: 各蜂方向故意不全中性：出错的蜂若被数成「中性」，多数派占比 / agreement 会变
_DIRS = {"ScoutBeeNova": "bullish", "BuzzBeeWhisper": "bullish", "ChronosBeeHorizon": "bullish",
         "OracleBeeEcho": "bearish", "GuardBeeSentinel": "bullish", "RivalBeeVanguard": "neutral",
         "BearBeeContrarian": "bearish"}
_SCORES = {"ScoutBeeNova": 6.5, "BuzzBeeWhisper": 7.2, "ChronosBeeHorizon": 6.8, "OracleBeeEcho": 4.1,
           "GuardBeeSentinel": 7.0, "RivalBeeVanguard": 5.5, "BearBeeContrarian": 3.2}


def _results(failed=(), absent=()):
    out = []
    for a, d in _DIMS.items():
        if a in absent:
            continue
        if a in failed:
            out.append(make_error_result(a, d, "桩：取数失败"))
            continue
        out.append({"source": a, "dimension": d, "direction": _DIRS[a], "score": _SCORES[a], "confidence": 0.6,
                    "discovery": f"{a} 合成", "data_quality": {"x": "real"},
                    "details": {"macro_regime": "neutral"} if a == "GuardBeeSentinel" else {}})
    return out


def _distill(results):
    return QueenDistiller(PheromoneBoard(), enable_llm=False, ml_model=None).distill(
        TICKER, copy.deepcopy(results), dealer_gex={"regime": "unknown"})


def _agreement(sr):
    """ML 的 agent_agreement，照抄训练端 / 服务端共用的公式（`ml_predictor` 训练、`generate_ml_report` 服务）。"""
    ad = sr["agent_directions"]
    return sum(1 for d in ad.values() if d == sr["direction"]) / len(ad)


# ════════════════════════════════════════════════════════════════════════════
# 判据
# ════════════════════════════════════════════════════════════════════════════

class TestJudge:

    @pytest.mark.parametrize("rec,expect", [
        (make_error_result("ChronosBeeHorizon", "catalyst", "x"), True),                 # 现行：error + 前缀
        ({"score": 5.0, "direction": "neutral", "discovery": "Error: calendar=YFRateLimited"}, True),   # 老记录：只有前缀
        ({"score": 5.0, "direction": "neutral", "discovery": "  ❌ 数据爬取失败"}, True),
        ({"score": 5.0, "direction": "neutral", "discovery": "无前缀", "error": "弃权"}, True),     # 只有 error 键
        ({"score": 5.0, "direction": "neutral", "discovery": "真打了 5.0", "error": None}, False),
        ({"score": 5.0, "direction": "neutral", "discovery": "期权链 Error 率偏高"}, False),     # 「Error」不在开头
        ({}, False), (None, False), ("Error: 不是 dict", False),
    ], ids=["current", "old_prefix_only", "cross_mark", "key_only", "real_5", "error_mid_text", "empty", "none", "str"])
    def test_agent_record_failed(self, rec, expect):
        assert agent_record_failed(rec) is expect

    def test_make_error_result_is_recognised_after_the_queen(self):
        """蜂后透出的 `agent_details` 那一份（落盘的就是它）也认得出——error 键与前缀都在。"""
        sr = _distill(_results(failed=("ChronosBeeHorizon",)))
        rec = sr["agent_details"]["ChronosBeeHorizon"]
        assert agent_record_failed(rec) and rec["error"] and rec["discovery"].startswith("Error")


# ════════════════════════════════════════════════════════════════════════════
# 1. 蜂后 agent_directions ⇒ ML agent_agreement / 逐蜂准确率
# ════════════════════════════════════════════════════════════════════════════

class TestQueenDirections:

    @pytest.mark.parametrize("bee", ["ChronosBeeHorizon", "ScoutBeeNova", "BuzzBeeWhisper", "OracleBeeEcho"])
    def test_failed_bee_equals_absent_bee(self, bee):
        """出错 ≡ 根本不在：`agent_directions`、方向与 ML agreement 逐位相同；`agent_details` 里仍留着出错记录（审计轨迹）。
        变红的变异：`per_agent_directions` 改回从 all_results 建。"""
        failed, absent = _distill(_results(failed=(bee,))), _distill(_results(absent=(bee,)))
        assert bee not in failed["agent_directions"]
        assert failed["agent_directions"] == absent["agent_directions"]
        assert failed["direction"] == absent["direction"]
        assert _agreement(failed) == _agreement(absent)
        assert failed["agent_details"][bee]["error"], "出错记录照留在 agent_details 里"

    def test_directions_agree_with_agent_breakdown(self):
        """同一口径：`agent_directions` 的计数 = `agent_breakdown`（后者一直只数有效结果）。"""
        sr = _distill(_results(failed=("ChronosBeeHorizon", "ScoutBeeNova")))
        vals = list(sr["agent_directions"].values())
        assert {k: vals.count(k) for k in ("bullish", "bearish", "neutral")} == sr["agent_breakdown"]

    def test_fixture_bites_a_failed_bee_used_to_move_agreement(self):
        """夹具自证：把占位「中性」数进去，agreement 确实不同（否则上面的「相同」证明不了什么）。"""
        sr = _distill(_results(failed=("ChronosBeeHorizon",)))
        with_placeholder = dict(sr["agent_directions"], ChronosBeeHorizon="neutral")
        assert sum(1 for d in with_placeholder.values() if d == sr["direction"]) / len(with_placeholder) != _agreement(sr)

    def test_clean_rows_unchanged(self):
        """没有出错的蜂 ⇒ 全体都在（11-09 起生产 60 行都是这种）。"""
        sr = _distill(_results())
        assert sr["agent_directions"] == _DIRS


# ════════════════════════════════════════════════════════════════════════════
# 2 / 3. signal_archive
# ════════════════════════════════════════════════════════════════════════════

def _record(failed=(), old_style=False):
    """一份落盘形状的 ticker 结果（只含抽取器读的键）。old_style：出错记录只有前缀、没有 error 键（v0.45.182 之前）。"""
    ad = {}
    for a in _DIMS:
        if a in failed:
            rec = {"score": 5.0, "direction": "neutral", "confidence": 0.0, "dimension": _DIMS[a],
                   "discovery": "Error: 桩", "details": {}}
            if not old_style:
                rec["error"] = "桩"
            ad[a] = rec
        else:
            ad[a] = {"score": _SCORES[a], "direction": _DIRS[a], "confidence": 0.6, "dimension": _DIMS[a],
                     "discovery": f"{a} 合成", "details": {}, "error": None}
    return {"final_score": 6.0, "direction": "bullish", "agent_details": ad}


class TestSignalArchive:

    @pytest.mark.parametrize("old_style", [False, True], ids=["error_key", "prefix_only"])
    def test_failed_bee_not_archived(self, old_style):
        """变红的变异：`_agent_score` / `_direction_num` 不问 `_agent_ok`。"""
        sig = sa.extract(_record(failed=("ChronosBeeHorizon",), old_style=old_style))
        assert "agent.ChronosBeeHorizon.score" not in sig and "agent.ChronosBeeHorizon.direction" not in sig
        assert sig["agent.ScoutBeeNova.score"] == 6.5 and sig["agent.ScoutBeeNova.direction"] == 1.0

    @pytest.mark.parametrize("old_style", [False, True], ids=["error_key", "prefix_only"])
    def test_swarm_agreement_skips_failed_bee(self, old_style):
        """出错 ≡ 根本不在。变红的变异：`_swarm_agreement` 不跳过出错的蜂。"""
        failed = sa.extract(_record(failed=("ChronosBeeHorizon",), old_style=old_style))
        rec = _record()
        del rec["agent_details"]["ChronosBeeHorizon"]
        assert failed["composite.swarm_agreement"] == sa.extract(rec)["composite.swarm_agreement"]
        # 夹具自证：把占位中性算进去会不同（4 多 / 7 vs 4 多 / 6）
        assert failed["composite.swarm_agreement"] != pytest.approx(4 / 7)

    def test_clean_record_unchanged(self):
        sig = sa.extract(_record())
        assert sig["agent.ChronosBeeHorizon.score"] == 6.8 and sig["agent.ChronosBeeHorizon.direction"] == 1.0
        assert sig["composite.swarm_agreement"] == pytest.approx(4 / 7)

    def test_a_real_five_is_still_archived(self):
        """真打了 5.0、真判中性的蜂（生产 38 例）照存——只认判据，不认取值。"""
        rec = _record()
        rec["agent_details"]["ChronosBeeHorizon"].update(score=5.0, direction="neutral")
        sig = sa.extract(rec)
        assert sig["agent.ChronosBeeHorizon.score"] == 5.0 and sig["agent.ChronosBeeHorizon.direction"] == 0.0


# ════════════════════════════════════════════════════════════════════════════
# 4. 网站「蜂群分歧度」
# ════════════════════════════════════════════════════════════════════════════

class TestDashboardDivergence:

    def test_failed_bee_equals_absent_bee(self):
        """变红的变异：`_swarm_divergence_entry` 不跳过出错的蜂（占位 5.0 进标准差、中性进共识度）。"""
        failed = dr._swarm_divergence_entry(_record(failed=("ChronosBeeHorizon",)))
        rec = _record()
        del rec["agent_details"]["ChronosBeeHorizon"]
        assert failed == dr._swarm_divergence_entry(rec)
        assert "Chronos" not in {b["name"] for b in failed["bees"]}

    def test_clean_record_lists_every_bee(self):
        e = dr._swarm_divergence_entry(_record())
        assert len(e["bees"]) == 7 and e["votes"] == {"bullish": 4, "bearish": 2, "neutral": 1}

    def test_all_failed_is_none(self):
        assert dr._swarm_divergence_entry(_record(failed=tuple(_DIMS))) is None


# ════════════════════════════════════════════════════════════════════════════
# 历史清理工具（backfill 是 INSERT OR REPLACE，删不掉已有的占位行）
# ════════════════════════════════════════════════════════════════════════════

def _purge_tool():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "experiments" / "purge_failed_agent_placeholder_rows.py"
    spec = importlib.util.spec_from_file_location("purge_failed_agent_placeholder_rows", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(tmp_path, day="2026-10-14"):
    """一份落盘记录 + 一个按**旧抽取器**写的档案（出错的 Chronos 存了占位 5.0 / 0.0，共识度把占位中性算进去）。"""
    import json
    import sqlite3
    tool = _purge_tool()
    rec_failed, rec_clean = _record(failed=("ChronosBeeHorizon",)), _record()
    (tmp_path / f".swarm_results_{day}.json").write_text(json.dumps({"AAA": rec_failed, "BBB": rec_clean}), encoding="utf-8")
    db = tmp_path / "pheromone.db"
    sa.ensure_schema(db)
    rows = []
    for tk, rec in (("AAA", rec_failed), ("BBB", rec_clean)):
        for a, r in rec["agent_details"].items():
            rows.append((day, tk, f"agent.{a}.score", tool._old_agent_cell(r, "score")))
            rows.append((day, tk, f"agent.{a}.direction", tool._old_agent_cell(r, "direction")))
        rows.append((day, tk, "composite.swarm_agreement", tool._old_agreement(rec)))
    with sqlite3.connect(db) as con:
        con.executemany(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)", rows)
    return tool, db


def _cells(db):
    import sqlite3
    with sqlite3.connect(db) as con:
        return {(d, t, s): v for d, t, s, v in con.execute(f"SELECT date, ticker, signal, value FROM {sa.TABLE}")}


class TestPurgeTool:

    def test_dry_run_counts_and_touches_nothing(self, tmp_path):
        tool, db = _seed(tmp_path)
        before = _cells(db)
        assert tool.main(["--db", str(db), "--expect-delete", "2", "--expect-update", "1"]) == 0
        assert _cells(db) == before

    def test_wrong_expectation_stops(self, tmp_path):
        tool, db = _seed(tmp_path)
        with pytest.raises(SystemExit):
            tool.main(["--db", str(db), "--expect-delete", "3", "--expect-update", "1"])

    def test_apply_removes_only_the_placeholders(self, tmp_path):
        """出错的 Chronos 两格删掉、那行共识度改成新抽取器的值；没出错的行一格不动；备份是改前的整库。"""
        tool, db = _seed(tmp_path)
        before = _cells(db)
        bk = tmp_path / "bk.db"
        assert tool.main(["--db", str(db), "--expect-delete", "2", "--expect-update", "1",
                          "--apply", "--backup", str(bk)]) == 0
        after = _cells(db)
        day = "2026-10-14"
        assert (day, "AAA", "agent.ChronosBeeHorizon.score") not in after
        assert (day, "AAA", "agent.ChronosBeeHorizon.direction") not in after
        assert after[(day, "AAA", "composite.swarm_agreement")] == sa.extract(_record(failed=("ChronosBeeHorizon",)))["composite.swarm_agreement"]
        assert {k: v for k, v in after.items() if k[1] == "BBB"} == {k: v for k, v in before.items() if k[1] == "BBB"}
        assert _cells(bk) == before
        # 幂等：再跑一次 0 / 0
        assert tool.main(["--db", str(db), "--expect-delete", "0", "--expect-update", "0"]) == 0

    def test_mismatch_stops_before_touching(self, tmp_path):
        """库里的值不是这份记录写下的占位（如被同日重扫覆盖）⇒ 不动、停下。"""
        import sqlite3
        tool, db = _seed(tmp_path)
        with sqlite3.connect(db) as con:
            con.execute(f"UPDATE {sa.TABLE} SET value=6.1 WHERE ticker='AAA' AND signal='agent.ChronosBeeHorizon.score'")
        before = _cells(db)
        with pytest.raises(SystemExit):
            tool.main(["--db", str(db), "--expect-delete", "1", "--expect-update", "1",
                       "--apply", "--backup", str(tmp_path / "bk.db")])
        assert _cells(db) == before and not (tmp_path / "bk.db").exists()

    def test_apply_needs_a_backup_path(self, tmp_path):
        tool, db = _seed(tmp_path)
        with pytest.raises(SystemExit):
            tool.main(["--db", str(db), "--expect-delete", "2", "--expect-update", "1", "--apply"])


class TestRegistered:

    def test_ml_estimator_generation_label_extended(self):
        """`agent_agreement` 的输入变了（出错蜂不再记成中性）⇒ 11-09 那一代的标签带上本版（同日并入、作废 0 条）。"""
        import probability_scorecard as ps
        assert any(d == "2026-11-09" and "v0.45.768" in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)
        assert "v0.45.768" in ps.ml_estimator_generation("2026-11-10").split("+")
