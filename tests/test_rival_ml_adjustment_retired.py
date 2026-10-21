"""v0.45.538 退役守卫：RivalBee 的 ML 不许再进分，合成训练集不许再被评分链读到。

背景（全文见 CHANGELOG v0.45.538 与 `ic_rerun_readiness._COHORT_HISTORY` 同版条目）：
RivalBee 每只标的都 new 一个 `ml_predictor_extended.MLPredictionService`，在 30 条 2023 年硬编码样本上
重训 HGB、写生产 `ml_model.json`，拿它被小样本校准压在 0.54–0.58 的概率当分数；`QueenDistiller` 再把
`(rival_score − 5)·0.1·conf` 乘上**另一个模型**（`ml_model_cache.json`）的 OOS 信任系数加进加权分 ⇒
只向上的抬分（09-15 起重放 613 行：均值 +0.034、最大 +0.110、从不为负）。v0.45.538 三处一起拆：
Queen 的 `ml_adjustment` 与 `_ml_oos_trust_factor`、`ml_predictor_extended` 整个模块、Rival 的模型调用。

守四件事：

1. **行为·蜂后**：同一组结果只改 ml_auxiliary 的分数 / 置信度，`final_score` 必须逐位相同；
   输出里不再有 `ml_adjustment` / `ml_contribution_pct`。
   变红的变异：把 `adjusted_score = base_score` 改回 `base_score + (ml_score − 5)·0.1·conf`。
2. **行为·Rival**：不训练任何模型、不写模型文件、不发布概率；分数 = 5.0 + 0.5·tech；方向 = 预期收益的符号。
   变红的变异：在 `analyze()` 里重新 new 任何带 `train_model` 的服务（`HGBModel.train` 被钉成抛错）。
3. **静态**：本仓自己的 .py 里不许 import `ml_predictor_extended`；生产代码里不许再出现
   `_ml_oos_trust_factor` / `oos_trust_*`；`swarm_agents/` 里不许引用 `HistoricalDataBuilder` /
   `MLPredictionService` / `train_model`（评分链读不到任何硬编码训练样本）。
4. **自证**：静态扫描确实扫到了东西；探测器对病灶源码会红。

⚠️ 本文件不用 skip：全部合成数据、零外部依赖。
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import ml_predictor as mp
from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents.queen_distiller import QueenDistiller
from swarm_agents.rival_bee import RivalBeeVanguard
from tests import _ast_cache
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__


# ══════════════════════════════════════════════════════════════════════════
# 1. 蜂后：ml_auxiliary 不进分
# ══════════════════════════════════════════════════════════════════════════

def _result(source, dim, score, direction="bullish", confidence=0.8):
    return {"source": source, "dimension": dim, "score": score, "direction": direction,
            "confidence": confidence, "discovery": f"{source} 合成", "data_quality": {"x": "real"},
            "details": {}}


def _core_results():
    return [
        _result("ScoutBeeNova", "signal", 6.0),
        _result("OracleBeeEcho", "odds", 7.0),
        _result("BuzzBeeWhisper", "sentiment", 6.5),
        _result("ChronosBeeHorizon", "catalyst", 7.5),
        _result("GuardBeeSentinel", "risk_adj", 6.0),
    ]


@pytest.fixture
def queen(monkeypatch):
    # 同 tests/test_zero_weight_invariant.py：让注入的权重活过 __init__ 里的 reload(config)
    import config as _cfg
    monkeypatch.setattr(importlib, "reload", lambda m: m)
    monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS",
                        {"signal": 0.2, "catalyst": 0.2, "sentiment": 0.2, "odds": 0.2, "risk_adj": 0.2},
                        raising=False)
    return QueenDistiller(PheromoneBoard())


class TestQueenIgnoresMlAuxiliary:

    @pytest.mark.parametrize("rival_score,rival_conf", [(0.0, 0.8), (10.0, 0.8), (9.0, 1.0), (2.0, 0.3)])
    def test_final_score_does_not_depend_on_rival_score(self, queen, rival_score, rival_conf):
        base = queen.distill("TEST", _core_results() + [
            _result("RivalBeeVanguard", "ml_auxiliary", 5.0, confidence=rival_conf)])
        moved = queen.distill("TEST", _core_results() + [
            _result("RivalBeeVanguard", "ml_auxiliary", rival_score, confidence=rival_conf)])
        assert moved["final_score"] == base["final_score"], (
            f"Rival 分 5.0 → {rival_score} 改动了 final_score（{base['final_score']} → "
            f"{moved['final_score']}）——ml_adjustment 被接回去了")
        assert moved["base_score_before_resonance"] == base["base_score_before_resonance"]

    def test_fixture_can_move_the_score(self, queen):
        """自证：同一夹具上改一个**核心维**的分，final_score 会动 ⇒ 上一条的「不动」不是夹具钝。"""
        a = queen.distill("TEST", _core_results())
        bumped = _core_results()
        bumped[3] = _result("ChronosBeeHorizon", "catalyst", 9.5)
        b = queen.distill("TEST", bumped)
        assert b["final_score"] != a["final_score"]

    def test_output_has_no_ml_adjustment_keys(self, queen):
        out = queen.distill("TEST", _core_results() + [_result("RivalBeeVanguard", "ml_auxiliary", 9.0)])
        assert "ml_adjustment" not in out and "ml_contribution_pct" not in out
        ws = queen._compute_weighted_score(
            "TEST", {"catalyst": 7.0}, {"catalyst": 1.0}, 100.0, 1,
            [_result("RivalBeeVanguard", "ml_auxiliary", 9.0)])
        assert "ml_adjustment" not in ws
        assert ws["adjusted_score"] == ws["base_score"]

    def test_trust_gate_is_gone(self):
        assert not hasattr(QueenDistiller, "_ml_oos_trust_factor")
        import config
        assert not any(k.startswith("oos_trust_") for k in config.ML_FEEDBACK_CONFIG)


# ══════════════════════════════════════════════════════════════════════════
# 2. Rival：没有模型
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=False)
def rival(monkeypatch):
    b = RivalBeeVanguard.__new__(RivalBeeVanguard)
    b.board = PheromoneBoard()
    monkeypatch.setattr(b, "_get_history_context", lambda t: "")
    monkeypatch.setattr(b, "_validate_ticker", lambda t: None)
    monkeypatch.setattr(b, "_assess_eps_revision", lambda t: {"revision_signal": "unknown"})
    published = {}
    monkeypatch.setattr(b, "_publish", lambda *a, **k: published.update(args=a, kwargs=k))
    b._published = published
    return b


def _forbid_training(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("RivalBee 不许训练任何模型（v0.45.538）")
    for cls in ("HGBModel", "SGDMLModel", "SimpleMLModel"):
        if hasattr(mp, cls):
            monkeypatch.setattr(getattr(mp, cls), "train", _boom)
            monkeypatch.setattr(getattr(mp, cls), "save_model", _boom)
    monkeypatch.setattr(mp.MLPredictionService, "train_model", _boom)


class TestRivalHasNoModel:

    @pytest.mark.parametrize("mom,chronos,tech_adj", [
        (2.5, 8.8, 0.0), (-3.0, 4.0, 1.0), (None, None, -0.6), (0.0, 7.0, 0.4)])
    def test_analyze_uses_expected_returns_only(self, rival, monkeypatch, mom, chronos, tech_adj):
        _forbid_training(monkeypatch)
        monkeypatch.setattr(rival, "_get_stock_data",
                            lambda t: {"momentum_5d": mom, "volatility_20d": 30.0, "price": 100.0})
        monkeypatch.setattr(rival, "_calc_technical_indicators",
                            lambda t: {"tech_score_adj": tech_adj, "tech_direction": "neutral", "summary": ""})
        if chronos is not None:
            rival.board.publish(PheromoneEntry(
                agent_id="ChronosBeeHorizon", ticker="TEST", discovery="x", source="t",
                self_score=chronos, direction="neutral"))

        out = rival.analyze("TEST")

        cq = mp.catalyst_quality_from_score(chronos) if chronos is not None else None
        import types
        er = mp.expected_returns(types.SimpleNamespace(catalyst_quality=cq, momentum_5d=mom))
        avg = (round(er["expected_7d"], 2) + round(er["expected_30d"], 2)) / 2
        want_dir = "bullish" if avg > 0 else ("bearish" if avg < 0 else "neutral")

        assert out["direction"] == want_dir
        assert out["score"] == pytest.approx(round(max(0.0, min(10.0, 5.0 + 0.5 * tech_adj)), 2))
        assert out["confidence"] == pytest.approx(0.8)
        assert out["data_quality"]["ml_prediction"] == "real"
        assert "probability" not in out["details"]
        assert out["details"]["expected_7d"] == pytest.approx(round(er["expected_7d"], 2))
        assert "ml_probability" not in rival._published["kwargs"]["details"]

    def test_analyze_writes_no_model_file(self, rival, monkeypatch):
        from hive_logger import PATHS
        monkeypatch.setattr(rival, "_get_stock_data", lambda t: {"momentum_5d": 1.0})
        monkeypatch.setattr(rival, "_calc_technical_indicators",
                            lambda t: {"tech_score_adj": 0.0, "tech_direction": "neutral", "summary": ""})
        # v0.45.570：`PATHS.ml_model`（ml_model.json）连同缺省路径已删 ⇒ 改比数据根整体（日志不算产物）
        home = PATHS.home

        def _files():
            return {p.relative_to(home).as_posix() for p in home.rglob("*")
                    if p.is_file() and p.relative_to(home).parts[0] != "logs"} if home.exists() else set()
        before = _files()
        rival.analyze("TEST")
        assert _files() == before, f"RivalBee 往数据根写了文件：{sorted(_files() - before)}"


# ══════════════════════════════════════════════════════════════════════════
# 3. 静态
# ══════════════════════════════════════════════════════════════════════════

_RETIRED_MODULE = "ml_predictor_extended"
_RETIRED_NAMES_PROD = ("_ml_oos_trust_factor", "oos_trust_half_threshold", "oos_trust_zero_threshold")
_SCORING_CHAIN_FORBIDDEN = ("HistoricalDataBuilder", "MLPredictionService", "train_model")


def _imports_module(tree: ast.AST, mod: str) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name.split(".")[0] == mod for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == mod:
            return True
        if (isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == "import_module"
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == mod):
            return True
    return False


def _names_used(tree: ast.AST) -> set:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.alias):
            out.add(n.name.split(".")[-1])
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


def _own_files():
    files, how = own_python_files(REPO_ROOT)
    return files, how


class TestStatic:

    def test_retired_module_file_is_gone(self):
        assert not (REPO_ROOT / f"{_RETIRED_MODULE}.py").exists()

    def test_nobody_imports_the_retired_module(self):
        files, _ = _own_files()
        assert len(files) > 100, f"只扫到 {len(files)} 个文件——枚举坏了，下面的「没人 import」不可信"
        hits = [str(p.relative_to(REPO_ROOT)) for p in files
                if p.exists() and _imports_module(_ast_cache.parse_file(p), _RETIRED_MODULE)]
        assert not hits, f"{hits} 还在 import {_RETIRED_MODULE}（v0.45.538 已退役，勿接回）"

    def test_trust_gate_names_gone_from_production(self):
        files, _ = _own_files()
        prod = [p for p in files if p.relative_to(REPO_ROOT).parts[0] not in ("tests", "experiments")]
        assert any(p.name == "queen_distiller.py" for p in prod), "枚举里没有 queen_distiller.py——扫描没对准"
        hits = []
        for p in prod:
            if not p.exists():
                continue
            used = _names_used(_ast_cache.parse_file(p))
            hits += [(str(p.relative_to(REPO_ROOT)), n) for n in _RETIRED_NAMES_PROD if n in used]
        assert not hits, hits

    def test_scoring_chain_reads_no_hardcoded_training_set(self):
        chain = sorted((REPO_ROOT / "swarm_agents").glob("*.py"))
        assert any(p.name == "rival_bee.py" for p in chain), "swarm_agents/ 枚举不到 rival_bee.py"
        hits = []
        for p in chain:
            used = _names_used(_ast_cache.parse_file(p))
            hits += [(p.name, n) for n in _SCORING_CHAIN_FORBIDDEN if n in used]
        assert not hits, (
            f"{hits}：评分链又碰到了训练 / 硬编码训练集。RivalBee 的合成样本模型 v0.45.538 退役，"
            "要让 ML 进分先预注册前瞻检验")


class TestDetectorsHaveTeeth:
    """探测器本身对病灶源码会红（否则上面的「没命中」证明不了什么）。"""

    @pytest.mark.parametrize("src", [
        "from ml_predictor_extended import MLPredictionService\n",
        "import ml_predictor_extended as mpe\n",
        "def f():\n    from ml_predictor_extended import TrainingData\n",
        "import importlib\nimportlib.import_module('ml_predictor_extended')\n",
    ])
    def test_import_detector_fires(self, src):
        assert _imports_module(ast.parse(src), _RETIRED_MODULE)

    def test_import_detector_quiet_on_primary(self):
        assert not _imports_module(ast.parse("from ml_predictor import expected_returns\n"), _RETIRED_MODULE)

    @pytest.mark.parametrize("src,name", [
        ("x = self._ml_oos_trust_factor()\n", "_ml_oos_trust_factor"),
        ("c = cfg.get('oos_trust_half_threshold', 55)\n", "oos_trust_half_threshold"),
        ("s = MLPredictionService()\ns.train_model()\n", "train_model"),
        ("from ml_predictor import HistoricalDataBuilder\n", "HistoricalDataBuilder"),
    ])
    def test_name_detector_fires(self, src, name):
        assert name in _names_used(ast.parse(src))


# ══════════════════════════════════════════════════════════════════════════
# 5. 世代印记（v0.45.548：初版「缺 ml_adjustment 键」在 03-12 早期格式旧归档上误报 boundary_too_late）
# ══════════════════════════════════════════════════════════════════════════

class TestBoundaryMarker:
    """`_marker_rival_ml_adjustment_retired` 只认新代码**正面**写的 `ml_auxiliary_in_score is False`。"""

    @staticmethod
    def _marker(d):
        from ic_rerun_readiness import _marker_rival_ml_adjustment_retired
        return _marker_rival_ml_adjustment_retired(d)

    def test_queen_writes_the_marker(self, queen):
        out = queen.distill("TEST", _core_results() + [_result("RivalBeeVanguard", "ml_auxiliary", 6.0)])
        assert out["ml_auxiliary_in_score"] is False
        assert self._marker({"swarm_results": out}) is True

    def test_early_format_record_without_ml_adjustment_is_not_new(self):
        """生产 `analysis-NVDA-ml-2026-03-12.json` 的形状：有 final_score、从来没有 ml_adjustment。
        变红的变异：印记改回「有 final_score 且没有 ml_adjustment」。"""
        sr = {"ticker": "NVDA", "final_score": 6.1, "direction": "bullish", "agent_details": {}}
        assert self._marker({"swarm_results": sr}) is False

    def test_old_modern_record_is_not_new(self):
        sr = {"final_score": 6.1, "ml_adjustment": 0.05, "base_score_before_resonance": 6.0}
        assert self._marker({"swarm_results": sr}) is False

    @pytest.mark.parametrize("val", [True, None, 0, "False"])
    def test_only_literal_false_counts(self, val):
        assert self._marker({"swarm_results": {"final_score": 6.0, "ml_auxiliary_in_score": val}}) is False

    def test_backfill_row_is_not_date_evidence(self):
        sr = {"final_score": 6.0, "ml_auxiliary_in_score": False,
              "agent_details": {"GuardBeeSentinel": {"details": {
                  "vix_term_structure": {"macro_as_of_mode": "backfill"}}}}}
        assert self._marker({"swarm_results": sr}) is False
