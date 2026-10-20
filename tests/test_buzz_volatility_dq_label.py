"""v0.45.559：BuzzBee `data_quality["volatility"]` 不再硬编码 "real"。

`volatility_20d` 为 None 时波动率通道回中性 50，标签却照报 "real" ⇒ Queen 的 `data_real_pct` 与 S5 冲突仲裁的
DQ 票重都按满分计（同 v0.45.209 GuardBee crowding 那个形状）。生产史 153 行（08-26 起 7 个扫描日）全是这样。

守三件事：
1. 三个行情通道（动量 / 量比 / 波动率）同一规则：取不到 ⇒ 通道 50 且标签 "unavailable"；取到 ⇒ "real"。
   参数化而不是只测波动率——三条里任何一条退回硬编码都会红。
2. 标签只是标签：同一份输入下，Buzz 的分数 / 方向 / 置信度 / 通道值都不读它（改标签不该动 H1 的输入层或冻结层）。
3. "unavailable" 在 Queen 的 PROXY_SOURCES 里（0.7），不是未分类（0 分 + dq_unclassified）。

⚠️ 全部离线、不用 skip：外部依赖钉在源头（同 tests/test_news_source_visibility.py 的 buzz_env）。
"""
from __future__ import annotations

import pytest

_CNN_FG = {"value": 47, "classification": "Neutral", "sentiment_score": 4.7,
           "is_real_data": True, "source": "cnn", "timestamp": "2026-10-06T14:15:27"}
_STOCK = {"price": 238.9, "momentum_5d": 1.2, "avg_volume": 180_000_000,
          "volume_ratio": 1.1, "volatility_20d": 41.0}
_NEWS = {"ticker": "NVDA", "articles": [{"title": f"h{i}"} for i in range(4)], "total_articles": 4,
         "bullish_count": 1, "bearish_count": 1, "neutral_count": 2, "sentiment_score": 6.0,
         "dominant_theme": "t", "source": "massive", "is_real_data": True,
         "data_quality": {"issues": [], "passed": True}}

# 行情字段 → (DQ 标签键, details.components 通道键)
_CHANNELS = {
    "momentum_5d": ("momentum", "momentum_signal"),
    "volume_ratio": ("volume", "volume_signal"),
    "volatility_20d": ("volatility", "volatility_signal"),
}


@pytest.fixture
def buzz(monkeypatch, stub_reddit):
    """返回 run(**行情覆盖) -> BuzzBee AgentResult dict。"""
    import fear_greed
    import llm_service
    import newsapi_client as nc
    import yahoo_trending
    from pheromone_board import PheromoneBoard
    from swarm_agents import BuzzBeeWhisper
    from swarm_agents import cache as _swarm_cache

    monkeypatch.setattr(llm_service, "is_available", lambda: False)
    monkeypatch.setattr(yahoo_trending, "get_ticker_attention",
                        lambda ticker: yahoo_trending._default_result(ticker))
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: dict(_CNN_FG))
    monkeypatch.setattr(nc, "get_ticker_news", lambda ticker, max_articles=10: dict(_NEWS))

    def run(**overrides):
        stock = {**_STOCK, **overrides}
        monkeypatch.setattr(_swarm_cache, "_fetch_stock_data",
                            lambda ticker, target_date=None: dict(stock))
        r = BuzzBeeWhisper(PheromoneBoard()).analyze("NVDA")
        assert "error" not in r, r.get("error")
        return r
    return run


class TestLabelFollowsAvailability:

    @pytest.mark.parametrize("field", sorted(_CHANNELS))
    def test_missing_field_is_unavailable_and_neutral(self, buzz, field):
        """变红的变异：把任一标签改回 `"real"` 硬编码（v0.45.559 之前波动率就是这样）。"""
        dq_key, comp_key = _CHANNELS[field]
        r = buzz(**{field: None})
        assert r["details"]["components"][comp_key] == 50, "缺数据时通道应回中性 50"
        assert r["data_quality"][dq_key] == "unavailable", (
            f"{field}=None 时通道已是中性 50，DQ 标签却报 {r['data_quality'][dq_key]!r}")
        others = {k for k in _CHANNELS.values() if k[0] != dq_key}
        assert all(r["data_quality"][k] == "real" for k, _ in others), "只该降级缺的那一条"

    def test_all_present_all_real(self, buzz):
        r = buzz()
        assert {r["data_quality"][k] for k, _ in _CHANNELS.values()} == {"real"}

    def test_zero_volatility_is_a_value_not_missing(self, buzz):
        """0.0 是取到的数（is not None），不是缺失——别把判据写成真值判断。"""
        assert buzz(volatility_20d=0.0)["data_quality"]["volatility"] == "real"


class TestLabelIsOnlyALabel:

    def test_buzz_outputs_do_not_read_the_label(self, buzz, monkeypatch):
        """同一份输入（volatility_20d=None），强行把标签改回旧值后重跑：分 / 方向 / 置信度 / 通道逐项相同。

        做法：把 AgentResult 的构造截住、把 volatility 标签换成 "real" 再交回——若 Buzz 别处读了这个标签，
        两次输出会在标签以外的地方不同。"""
        import models
        new = buzz(volatility_20d=None)

        orig_init = models.AgentResult.__init__

        def _old_label(self, *a, **kw):
            dq = dict(kw.get("data_quality") or {})
            dq["volatility"] = "real"
            kw["data_quality"] = dq
            orig_init(self, *a, **kw)
        monkeypatch.setattr(models.AgentResult, "__init__", _old_label)
        old = buzz(volatility_20d=None)

        assert old["data_quality"]["volatility"] == "real"      # 截住生效（否则本测试恒真）
        for k in ("score", "direction", "confidence"):
            assert new[k] == old[k], k
        assert new["details"]["components"] == old["details"]["components"]
        assert {k: v for k, v in new["data_quality"].items() if k != "volatility"} == \
               {k: v for k, v in old["data_quality"].items() if k != "volatility"}


class TestQueenClassifiesTheLabel:

    def test_unavailable_is_a_registered_proxy_label(self):
        from swarm_agents.queen_distiller import QueenDistiller as Q
        assert "unavailable" in Q.PROXY_SOURCES and "unavailable" not in Q.REAL_SOURCES

    def test_triple_penalty_counts_it_at_0_7_not_0(self, buzz):
        """经真实 `_apply_triple_penalty`：同一条 Buzz 记录，volatility 由 "real" 换成 "unavailable" ⇒
        data_real_pct 恰好少 0.3 / 通道数（记 0.7，不是 0），且不进 dq_unclassified。"""
        from swarm_agents.queen_distiller import QueenDistiller
        new = buzz(volatility_20d=None)
        assert new["data_quality"]["volatility"] == "unavailable"
        old = {**new, "data_quality": {**new["data_quality"], "volatility": "real"}}
        q = QueenDistiller.__new__(QueenDistiller)        # 只用实例方法读类属性，不需要 board / 配置
        tp_new = q._apply_triple_penalty("NVDA", 5.0, [new])
        tp_old = q._apply_triple_penalty("NVDA", 5.0, [old])
        assert tp_new["dq_unclassified"] == []
        n = len(new["data_quality"])
        assert tp_old["data_real_pct"] - tp_new["data_real_pct"] == pytest.approx(30.0 / n, abs=0.11)


# ════════════════════════════════════════════════════════════════════════════
# 世代边界登记（用户 2026-10-20 选「边界、日期 10-19」，同 v0.45.369 的等价处置）
# ════════════════════════════════════════════════════════════════════════════

_V = "v0.45.559"
_MARK = "volatility_dq_from_value"


def _brow(*, vol20=22.5, new=False, details=True, backfill=False):
    """一只标的的蒸馏结果（只放判据 / 印记读的字段）。"""
    agents = {}
    if details:
        det = {"volatility_20d": vol20, "volume_ratio": 1.1}
        if new:
            det[_MARK] = True
        agents["BuzzBeeWhisper"] = {"score": 5.0, "details": det}
    else:
        agents["BuzzBeeWhisper"] = {"score": 5.0, "error": "boom"}
    if backfill:
        agents["GuardBeeSentinel"] = {"details": {"vix_term_structure": {"macro_as_of_mode": "backfill"}}}
    return {"agent_details": agents}


def _write(root, day, rows, archive=("AAA",)):
    import json
    (root / f".swarm_results_{day}.json").write_text(json.dumps(rows), encoding="utf-8")
    for tk in archive:
        (root / f"analysis-{tk}-ml-{day}.json").write_text(json.dumps({"swarm_results": rows.get(tk, _brow())}),
                                                             encoding="utf-8")


class TestBoundaryRegistered:

    def test_entry_scope_marker_equivalence(self):
        import ic_rerun_readiness as rr
        import signal_archive as sa
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V] == ["2026-10-19"]
        assert sa.COHORT_SIGNAL_SCOPE[_V] == ()          # 空范围 = 只切 composite.final_score；缺登记 ⇒ 按「影响全部」处理
        assert _V in rr._BOUNDARY_MARKERS and _V in rr._BOUNDARY_EQUIVALENCE

    def test_does_not_truncate_dim_ic_h1_or_h2(self):
        """空范围边界不触及 H1 的输入层 / 冻结层，也不触及 H2 的三只蜂分。
        变红的变异：把 `COHORT_SIGNAL_SCOPE["v0.45.559"]` 删掉（未声明 ⇒ 影响全部 ⇒ 截断 H1）或点名 `agent.BuzzBeeWhisper.*`。"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
        import dim_ic_forward_test as F
        import dim_ic_protocol as P
        import ic_rerun_readiness as rr
        entry = next(e for e in rr._COHORT_HISTORY if e[1] == _V)
        for name, _obj, sigs in P.HYPOTHESES:
            assert F.truncation_point(sigs, P, [entry]) is None, name

    def test_ml_estimator_generation_label_extended(self):
        import probability_scorecard as ps
        assert any(d == "2026-10-19" and _V in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)


class TestMarker:

    def test_real_buzz_writes_the_marker(self, buzz):
        import ic_rerun_readiness as rr
        rec = {"swarm_results": {"agent_details": {"BuzzBeeWhisper": buzz(volatility_20d=None)}}}
        assert rr._marker_buzz_volatility_dq_from_value(rec) is True

    @pytest.mark.parametrize("d", [
        {}, {"swarm_results": {"agent_details": {"BuzzBeeWhisper": None}}},
        {"swarm_results": _brow()},                                                    # 旧代码：没有这个键
        {"swarm_results": {"agent_details": {"BuzzBeeWhisper": {"details": {_MARK: 1}}}}},
        {"swarm_results": _brow(new=True, backfill=True)},                             # 补跑行不算日期证据
    ], ids=["empty", "buzz_none", "old_code", "not_literal_true", "backfill"])
    def test_marker_only_literal_true_on_live_rows(self, d):
        import ic_rerun_readiness as rr
        assert rr._marker_buzz_volatility_dq_from_value(d) is False

    def test_synthetic_fallback_writes_the_marker_unconditionally(self):
        """日报合成回退自己拼 Buzz 条目：印记写在字面量 dict 里（不在 if / try 里）。缺键会被判别器读成旧代码。"""
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_generate_synthetic_swarm_results")
        hits = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)
                and any(isinstance(k, ast.Constant) and k.value == _MARK for k in n.keys)
                and any(isinstance(v, ast.Constant) and v.value is True
                        for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant) and k.value == _MARK)]
        assert hits, f"合成回退里没有写 {_MARK}: True"


class TestEquivalenceJudge:

    @pytest.mark.parametrize("row,expect", [
        (_brow(), True),                         # 有波动率 ⇒ 本版是空操作
        (_brow(vol20=0.0), True),                # 0.0 是值
        (_brow(vol20=float("nan")), True),       # NaN 在两版里都标 real（判据与代码同一条件 `is not None`）
        (_brow(vol20=None), False),              # 本版改标签的那一种
        (_brow(details=False), True),            # Buzz 异常兜底：两版都不贴 volatility 标签
        ({"agent_details": {"BuzzBeeWhisper": {"details": {"volume_ratio": 1.1}}}}, False),   # 缺键 ⇒ 证不出
    ], ids=["present", "zero", "nan", "none", "no_details", "key_missing"])
    def test_judge(self, row, expect):
        """变红的变异：判据恒真（`volatility_20d is None` 的旧记录也放行）/ 恒假。"""
        import ic_rerun_readiness as rr
        assert rr._equiv_buzz_volatility_present({"swarm_results": row}) is expect


class TestBoundaryVerdict:

    def test_before_deploy_is_no_evidence_yet(self, tmp_path):
        """上线前的真实形状：10-19 全是旧代码、30/30 有波动率 ⇒ 还没证据，不报警。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _brow(), "BBB": _brow()})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_on_deploy_day_matches(self, tmp_path):
        """首跑后：印记首见 10-20，其前只有可证等价的旧记录 ⇒ matches（边际作废 0 条）。
        变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.559"]`（⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _brow(), "BBB": _brow()})
        _write(tmp_path, "2026-10-20", {"AAA": _brow(new=True, vol20=None), "BBB": _brow(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-10-20", ev
        assert ev["equivalent_before_marker"] == ["2026-10-19"]

    def test_old_code_day_with_missing_volatility_is_too_early(self, tmp_path):
        """首跑前若出现缺波动率的旧代码扫描日 ⇒ 那天真按旧标签算了 ⇒ boundary_too_early。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _brow(), "BBB": _brow(vol20=None)})
        _write(tmp_path, "2026-10-20", {"AAA": _brow(new=True), "BBB": _brow(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-10-19"], ev
