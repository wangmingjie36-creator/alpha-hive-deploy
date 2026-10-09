"""Scout 拥挤度去掉 `consensus_strength`（v0.45.441）。

根因：Scout 与同伴蜂 Phase-1 **并行**，读板那一刻同伴多半还没发布（2026-10-08：Oracle / Chronos 0/30 可见，Buzz 23/30，
CodeExecutor 30/30 但只是 5.0/中性占位）⇒ 该项 27/30 恒为 0。v0.45.279 / 304 修的是淘汰偏差与身份过滤，没碰时机。
现在：Scout 不读板、不要这一项（由 `calculate_crowding_score` 在其余四项间重归一化）；Guard / Rival 在 Phase-1 之后顺序执行，
读得到完整普查，保持原样。

本文件钉住：
1. `get_real_crowding_metrics(peer_census=False)`：不读板、`bullish_agents` / `consensus_census` 为 None、`data_quality` 不带该键；默认 True 不变。
2. **Scout 的输出与板面无关**（真跑 `ScoutBeeNova.analyze`）——板上有多少看多同伴，拥挤度 / 分 / 方向逐字相同。
3. 世代印记 `consensus_in_score: False` 每条返回路径都在（含无效 ticker / 异常兜底 / 日报合成回退），判别器只认字面量 False 且排补跑行。
4. 只有 Scout 传 `peer_census=False`（AST）；登记表（世代边界 / 印记 / 信号范围）接线。
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

import ic_rerun_readiness as rr
import real_data_sources as rds
import signal_archive as sa
from pheromone_board import PheromoneBoard
from swarm_agents.scout_bee import CONSENSUS_MARKER, ScoutBeeNova

REPO_ROOT = Path(__file__).resolve().parents[1]
_V = "v0.45.441"
TICKER = "TEST"


def _mk_entry(agent, direction, ticker):
    import inspect
    from pheromone_board import PheromoneEntry
    kw = dict(ticker=ticker, discovery=f"{agent} {direction}", source="t",
              self_score=7.0 if direction == "bullish" else 3.0, direction=direction, agent_id=agent)
    sig = inspect.signature(PheromoneEntry)
    if "timestamp" in sig.parameters:
        kw["timestamp"] = None
    return PheromoneEntry(**{k: v for k, v in kw.items() if k in sig.parameters and (k != "timestamp" or v is not None)})


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import congress_trades_scraper
    import edgar_rss
    import llm_service
    import market_intelligence
    import sec_edgar

    class _StubSEC:
        _cik_map: dict = {}

    monkeypatch.setattr(llm_service, "is_available", lambda: False)
    monkeypatch.setattr(sec_edgar, "get_insider_trades", lambda ticker, days=90: None)
    monkeypatch.setattr(sec_edgar, "SECEdgarClient", _StubSEC)
    monkeypatch.setattr(edgar_rss, "get_today_form4_alerts", lambda ticker, cik=None: {"has_fresh_filings": False})
    monkeypatch.setattr(congress_trades_scraper, "get_congress_trades_for_ticker", lambda ticker, days_back=90: {})
    monkeypatch.setattr(market_intelligence, "get_supply_chain_signals", lambda ticker: {})
    monkeypatch.setattr(rds, "get_social_buzz",
                        lambda ticker: {"messages_per_day": 2000, "data_quality": "reddit_apewisdom"})
    monkeypatch.setattr(rds, "get_short_interest",
                        lambda ticker: {"short_pct_float": 0.05, "data_quality": "real"})


def _bee(monkeypatch, board):
    bee = ScoutBeeNova(board)
    monkeypatch.setattr(bee, "_get_history_context", lambda t: "")
    monkeypatch.setattr(bee, "_get_stock_data", lambda t: {
        "price": 100.0, "momentum_5d": 1.0, "volume_ratio": 1.0, "volatility_20d": 20.0})
    monkeypatch.setattr(bee, "_assess_sector_relative_strength", lambda t: {"rs_signal": "unknown"})
    monkeypatch.setattr(bee, "_publish", lambda *a, **k: None)
    return bee


def _board_with(peers):
    b = PheromoneBoard()
    for agent, direction in peers:
        b.publish(_mk_entry(agent, direction, TICKER))
    return b


ALL_BULLISH = [("OracleBeeEcho", "bullish"), ("BuzzBeeWhisper", "bullish"),
               ("ChronosBeeHorizon", "bullish"), ("CodeExecutorAgent", "bullish")]


# ════════════════════════════════════════════════════════════════════════════
# 1. get_real_crowding_metrics(peer_census=...)
# ════════════════════════════════════════════════════════════════════════════

class TestMetricsPeerCensus:
    def test_default_still_counts_peers(self):
        m = rds.get_real_crowding_metrics(TICKER, {"price": 100.0}, _board_with(ALL_BULLISH))
        assert m["bullish_agents"] == 4 and m["data_quality"]["bullish_agents"] == "real"
        assert m["consensus_census"]["peers_bullish"] == sorted(a for a, _ in ALL_BULLISH)

    def test_excluded_does_not_read_the_board(self):
        m = rds.get_real_crowding_metrics(TICKER, {"price": 100.0}, _board_with(ALL_BULLISH), peer_census=False)
        assert m["bullish_agents"] is None and m["consensus_census"] is None
        assert "bullish_agents" not in m["data_quality"], "没被测量 ≠ 降级：不留标签（'unavailable' 会按 0.7 计入 data_real_pct）"

    def test_excluded_leaves_other_metrics_alone(self):
        a = rds.get_real_crowding_metrics(TICKER, {"price": 100.0, "momentum_5d": 2.0}, None)
        b = rds.get_real_crowding_metrics(TICKER, {"price": 100.0, "momentum_5d": 2.0}, None, peer_census=False)
        for k in ("social_messages_per_day", "google_trends_percentile", "seeking_alpha_page_views",
                  "short_float_ratio", "price_momentum_5d"):
            assert a[k] == b[k], k

    def test_reader_that_never_reaches_the_board_would_fail_if_board_were_read(self):
        """反向自证：`peer_census=False` 且板读取会抛——结果仍正常（证明真的没读）。"""
        class Boom:
            def get_live_signals(self, t):
                raise AssertionError("读板了")
        m = rds.get_real_crowding_metrics(TICKER, {"price": 100.0}, Boom(), peer_census=False)
        assert m["bullish_agents"] is None


# ════════════════════════════════════════════════════════════════════════════
# 2. Scout 的输出与板面无关
# ════════════════════════════════════════════════════════════════════════════

class TestScoutOutputIndependentOfBoard:
    def _run(self, monkeypatch, peers):
        return _bee(monkeypatch, _board_with(peers)).analyze(TICKER)

    def test_same_output_whether_peers_are_bullish_or_absent(self, monkeypatch):
        """变红的变异：scout_bee 的 `peer_census=False` 去掉 ⇒ 满板看多与空板的拥挤度 / 分不同。"""
        full = self._run(monkeypatch, ALL_BULLISH)
        empty = self._run(monkeypatch, [])
        for k in ("score", "direction", "confidence"):
            assert full[k] == empty[k], k
        for k in ("crowding_score", "crowding_signal", "components", "adjustment_factor"):
            assert full["details"][k] == empty["details"][k], k
        assert full["data_quality"] == empty["data_quality"]

    def test_consensus_component_is_missing_and_score_is_renormalized_over_the_other_four(self, monkeypatch):
        out = self._run(monkeypatch, ALL_BULLISH)
        comp = out["details"]["components"]
        assert comp["consensus_strength"] is None
        w = rds_weights()
        others = {k: v for k, v in comp.items() if k != "consensus_strength" and v is not None}
        assert set(others) == {"social_volume", "google_trends", "seeking_alpha_views", "short_squeeze_risk"}
        expect = sum(w[k] * v for k, v in others.items()) / sum(w[k] for k in others)
        assert out["details"]["crowding_score"] == pytest.approx(expect)

    def test_census_key_is_gone_and_quality_label_not_counted(self, monkeypatch):
        out = self._run(monkeypatch, ALL_BULLISH)
        assert "consensus_census" not in out["details"]
        assert "bullish_agents" not in out["data_quality"]

    def test_every_remaining_quality_label_is_classified(self, monkeypatch):
        """没有引入新标签：Scout 的每个 data_quality 值都在 Queen 的两集合里（否则静默记 0，见 v0.45.314）。"""
        from swarm_agents.queen_distiller import QueenDistiller as Q
        out = self._run(monkeypatch, ALL_BULLISH)
        for k, v in out["data_quality"].items():
            assert v in Q.REAL_SOURCES or v in Q.PROXY_SOURCES, (k, v)


def rds_weights():
    import config
    return config.CROWDING_WEIGHTS


# ════════════════════════════════════════════════════════════════════════════
# 3. 世代印记
# ════════════════════════════════════════════════════════════════════════════

def _marked(out):
    return isinstance(out.get("details"), dict) and out["details"].get(CONSENSUS_MARKER) is False


class TestMarkerOnEveryPath:
    def test_success(self, monkeypatch):
        assert _marked(_bee(monkeypatch, _board_with([])).analyze(TICKER))

    def test_invalid_ticker(self, monkeypatch):
        out = _bee(monkeypatch, _board_with([])).analyze("bad ticker!")
        assert out.get("error") and _marked(out)

    def test_error_fallback(self, monkeypatch):
        bee = _bee(monkeypatch, _board_with([]))
        monkeypatch.setattr(bee, "_get_history_context", lambda t: (_ for _ in ()).throw(ValueError("桩")))
        out = bee.analyze(TICKER)
        assert out.get("error") and _marked(out)

    def test_literal_is_false_not_falsy(self):
        assert CONSENSUS_MARKER == "consensus_in_score"

    def test_synthetic_swarm_fallback_marks_unconditionally(self):
        """日报合成回退自己拼 Scout details：键必须无条件写（不在 if / try 里）。"""
        src = (REPO_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_generate_synthetic_swarm_results")
        parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
        hits = []
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "_scout_details" and isinstance(node.value, ast.Dict)
                    and any(isinstance(k, ast.Constant) and k.value == CONSENSUS_MARKER for k in node.value.keys)):
                chain, cur = [], node
                while cur in parents:
                    cur = parents[cur]
                    chain.append(type(cur).__name__)
                hits.append(chain)
        assert hits, f"合成回退里 `_scout_details` 的初值没有写 {CONSENSUS_MARKER}"
        assert any(not ({"If", "Try", "ExceptHandler"} & set(c)) for c in hits), hits


class TestDiscriminator:
    def _row(self, details, backfill=False):
        d = {"swarm_results": {"agent_details": {"ScoutBeeNova": {"details": details}}}}
        if backfill:
            d["swarm_results"]["agent_details"]["GuardBeeSentinel"] = {
                "details": {"vix_term_structure": {"macro_as_of_mode": "backfill"}}}
        return d

    def test_new_code_row(self):
        assert rr._marker_scout_consensus_excluded(self._row({CONSENSUS_MARKER: False})) is True

    @pytest.mark.parametrize("details", [{}, {CONSENSUS_MARKER: True}, {CONSENSUS_MARKER: 0},
                                         {CONSENSUS_MARKER: None}, {"consensus_census": {"peers_live": []}}])
    def test_only_literal_false(self, details):
        """变红的变异：判定写成 `not det.get(...)`（缺键 / 0 / None 都被认成新口径）。"""
        assert rr._marker_scout_consensus_excluded(self._row(details)) is False

    def test_backfill_rows_are_not_date_evidence(self):
        assert rr._marker_scout_consensus_excluded(self._row({CONSENSUS_MARKER: False}, backfill=True)) is False

    def test_marker_survives_queen_into_archive_shape(self, monkeypatch):
        from swarm_agents.queen_distiller import QueenDistiller
        scout = _bee(monkeypatch, _board_with([])).analyze(TICKER)
        dims = {"BuzzBeeWhisper": "sentiment", "ChronosBeeHorizon": "catalyst", "OracleBeeEcho": "odds",
                "GuardBeeSentinel": "risk_adj", "RivalBeeVanguard": "ml_auxiliary", "BearBeeContrarian": "contrarian"}
        results = [{"source": a, "dimension": d, "direction": "neutral", "score": 5.0, "confidence": 0.5,
                    "discovery": f"{a} 合成", "data_quality": {"x": "real"},
                    "details": {"macro_regime": "neutral"} if a == "GuardBeeSentinel" else {}}
                   for a, d in dims.items()] + [scout]
        out = QueenDistiller(PheromoneBoard(), enable_llm=False, ml_model=None).distill(
            TICKER, copy.deepcopy(results), dealer_gex={"regime": "unknown"})
        assert rr._marker_scout_consensus_excluded({"swarm_results": out}) is True


# ════════════════════════════════════════════════════════════════════════════
# 4. 只有 Scout 排除；登记表接线
# ════════════════════════════════════════════════════════════════════════════

def _calls_with_peer_census_false():
    """全仓生产代码里传 `peer_census=False` 的调用点（文件相对路径）。"""
    found = set()
    for p in REPO_ROOT.rglob("*.py"):
        rel = p.relative_to(REPO_ROOT)
        if rel.parts[0] in {"tests", "experiments", ".claude", "_retired_pre_phase5", "mcp-servers"} or "worktrees" in rel.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and any(
                    k.arg == "peer_census" and isinstance(k.value, ast.Constant) and k.value.value is False
                    for k in n.keywords):
                found.add(str(rel))
    return found


class TestOnlyScoutExcludes:
    def test_only_scout_passes_false(self):
        """Guard 自己数、Rival 读 Phase-1 之后的板——它们读得到完整普查，不该被一并关掉。
        变红的变异：给 guard_bee / rival_bee 的调用也加 `peer_census=False`。"""
        assert _calls_with_peer_census_false() == {"swarm_agents/scout_bee.py"}


class TestRegistryWiring:
    def test_cohort_entry_is_last_and_dated_before_forward_start(self):
        date, ver, reason = rr._COHORT_HISTORY[-1]
        assert ver == _V and date == "2026-10-09"
        assert date < "2026-10-12", "须早于维度 IC 协议 FORWARD_START，否则 H1 / H2 被截断"
        assert "consensus_in_score" in reason and "非等价" in reason

    def test_marker_registered_and_no_equivalence_judge(self):
        assert _V in rr._BOUNDARY_MARKERS
        assert _V not in rr._BOUNDARY_EQUIVALENCE, "每一行 crowding_score 都变 ⇒ 不存在「旧记录等价」"

    def test_scope_reaches_scout_and_crowding(self):
        assert _V in sa.COHORT_SIGNAL_SCOPE
        universe = set(sa.SIGNAL_UPSTREAM) | {"agent.ScoutBeeNova.score", "agent.ScoutBeeNova.direction",
                                               "crowding.score", "crowding.comp.consensus_strength"}
        hit = sa._scope_closure(sa.COHORT_SIGNAL_SCOPE[_V], universe)
        assert {"agent.ScoutBeeNova.score", "agent.ScoutBeeNova.direction",
                "crowding.score", "crowding.comp.consensus_strength"} <= hit

    def test_ml_estimator_generation_registered_same_day(self):
        """ML 特征 `crowding_score`（= signal 维分 × 10 = Scout 分）与 `agent_agreement`（各蜂方向）的上游随本版变 ⇒
        `probability_scorecard._ML_ESTIMATOR_GENERATIONS` 必须同日追加（v0.45.441 初版漏登，二次检查补；
        `_prepare_ml_input` 没动、测试也不会红，只有这里盯着）。"""
        import probability_scorecard as ps
        assert any(d == "2026-10-09" and v == _V for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)
        assert ps.ml_estimator_generation("2026-10-09") == _V
        assert ps.ml_estimator_generation("2026-10-08") != _V

    def test_old_upstream_edge_is_kept_for_history(self):
        """旧世代数据里 consensus_strength 确实读 Phase-1 方向——边保留，删了会让早先边界的闭包悄悄缩小。"""
        assert "crowding.comp.consensus_strength" in sa.SIGNAL_UPSTREAM
