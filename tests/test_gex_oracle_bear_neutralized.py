"""v0.45.349 中性化守卫：OracleBee 主链 `gamma_exposure` 不再经任何规则进分。

背景：读 OracleBee 主链 GEX 的评分通道有两条 ——
  ① `options_analyzer.generate_options_score` 的 `gex_signal`：`gex < -0.001` 得 2.0、否则 1.0
     ⇒ 负 GEX 标的 options_score +1.0 ⇒ Oracle 分 +0.846 ⇒ 经 odds 维进加权分；
  ② `swarm_agents/bear_bee.py` 的 `gex < 0 ⇒ options_bear ≥ 5.0` 地板 + 看空信号「GEX 负值」。
用户决定两条一并中性化（① 恒 1.0 = 原 None 分支，options_score 标度不变；② 删除），
世代边界同 2026-09-28。理由（2026-09-27 调查实测）写在 `generate_options_score` 的 GEX 注释里，
本文件不抄数字。自此规则引擎（生产 --no-llm）下 GEX 进评分只剩 `gex_regime.RegimeWeightAdjuster`
（全链视图三值 regime）；LLM 模式下 Oracle / Bear 的 LLM 提示仍带 GEX，不在本文件守的范围。

这里守五件事，照 `tests/test_gex_modifier_disconnected.py`（v0.45.334）的写法：

1. **行为**：`generate_options_score` 对 gex ∈ {-5, -0.0023, None, 0, +5} 给同分同摘要；
   `OptionsAgent.analyze` 真跑负 / 正 / None 三条链（其余评分输入逐位相同）分数相同；OracleBee
   端到端（桩的 OptionsAgent 走**真**公式、`gamma_squeeze_risk` 按真分档给）分数与方向不随 GEX 变；
   BearBee 的 options_bear / 分数 / 方向 / 看空信号不随 GEX 变。每条都带夹具自证：同一夹具上
   **旧**逻辑确实给出不同的结果（否则「没变」恒真）。
2. **印记**：OracleBee 每条返回路径（成功 / OptionsAgent 失败 / 异常兜底 / 无效 ticker）的 details
   都带字面量 `gex_signal_in_score: False`，且经 QueenDistiller 进归档形状后，
   `ic_rerun_readiness` 的 v0.45.349 边界判别器认得出。
3. **印记只有 Oracle 产（复核 D1）**：OptionsAgent 自己的标记用**另一个**键
   （`options_analyzer.OPTIONS_GEX_MARKER`）。旧代码的 OracleBee 把 details 拼成 `{**result, ...}`，
   两键同名时，同会话里旧 Oracle 命中新代码写（或修过）的快照会把印记原样抄走 ⇒「旧 Oracle +
   旧 Bear 地板」的一天被判成新口径。守法：OptionsAgent 每条出口（含写盘的快照）喂给旧 Oracle 的
   复刻拼法都判不出新口径；AST 钉住印记字面量的产出者只有 OracleBee 与日报合成回退。
4. **旧快照修正**：同一 ET 会话里旧代码写的快照（缺标记、GEX<-0.001）命中时精确减 1.0、
   幂等、计数、回写；有标记（含改名前草稿的旧键名）或 GEX 不为负的一律不动。
5. **静态**：生产代码里对 `"gamma_exposure"` / `"gex"` 键读出值做大小比较 / 算术 / 排序，
   或对 `"gamma_squeeze_risk"`（由 GEX 符号分档）做判等 / 查表，只许出现在名单里（双向钉）。
   枚举驱动、带「确实扫到东西」自证与 tmp 树病灶夹具（复核 D2 的四种漏网形状各有一处）。

⚠️ 本文件不用 skip / 模块级 pytestmark：全部合成数据、零外部依赖。
每条断言旁注明能让它变红的变异。
"""
from __future__ import annotations

import ast
import copy
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import options_analyzer as oa
from options_analyzer import OptionsAgent, OptionsAnalyzer
from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents.bear_bee import BearBeeContrarian
from swarm_agents.oracle_bee import OracleBeeEcho
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
#: OracleBee 的世代印记（`ic_rerun_readiness` 的 v0.45.349 边界判别器读它）。**写死字面量**，
#: 不从生产模块取：判别器与写端的契约就是这个字符串本身。
MARKER = "gex_signal_in_score"
#: OptionsAgent 自己的「分数不含 GEX」标记（快照修正的幂等依据）。从生产模块取 —— 它叫什么不重要，
#: 重要的是它**不是** MARKER，那由 TestOptionsResultNeverCarriesOracleMarker 管（复核 D1）。
OPT_MARKER = oa.OPTIONS_GEX_MARKER
PT = ZoneInfo("America/Los_Angeles")
TICKER = "TEST"

#: 覆盖旧阈值的两侧：两个会被旧公式 +1 的负值（含一个贴着 -0.001 的），None，0，正值
_GEX_VALUES = (-5.0, -0.0023, None, 0.0, 5.0)
_LEGACY_BITES = (-5.0, -0.0023)


# ════════════════════════════════════════════════════════════════════════════
# 旧公式的独立复刻 —— 只用来证明夹具「咬得到」，不是生产代码
# ════════════════════════════════════════════════════════════════════════════

def _legacy_options_score(iv_rank, pc, gex, unusual):
    """v0.45.348 及以前的 generate_options_score（逐档独立复刻，不调用生产函数）。"""
    if iv_rank is None:
        iv = 2.0
    elif iv_rank < 20:
        iv = 1.0
    elif iv_rank < 40:
        iv = 2.0
    elif iv_rank <= 70:
        iv = 3.0
    elif iv_rank <= 85:
        iv = 2.0
    else:
        iv = 1.0
    flow = 3.0 if pc < 0.7 else 2.0 if pc < 1.0 else 1.0 if pc < 1.5 else 0.0
    g = 1.0 if gex is None else (2.0 if gex < -0.001 else 1.0)
    un = min(2.0, sum(1 for u in unusual if u.get("bullish", False)) * 0.5)
    return round(iv + flow + g + un, 2)


def _u(n_bull, n_bear=0):
    return [{"bullish": True}] * n_bull + [{"bullish": False}] * n_bear


#: 输入网格：每个 iv 档 × 每个 flow 档 × 几种异动条数（含封顶）
_GRID = [(iv, pc, un)
         for iv in (None, 10.0, 30.0, 50.0, 80.0, 95.0)
         for pc in (0.5, 0.8, 1.2, 2.0)
         for un in (_u(0), _u(1, 3), _u(3), _u(9))]


class TestOptionsScoreIgnoresGex:

    def test_legacy_replica_matches_production_where_gex_never_mattered(self):
        """夹具自证①：独立复刻的旧公式在 GEX 不为负的分支上与生产逐位相等 ——
        否则下一条「旧的会不同」可能只是复刻写错了。"""
        a = OptionsAnalyzer()
        for iv, pc, un in _GRID:
            for gex in (None, 0.0, 5.0, -0.001):
                assert _legacy_options_score(iv, pc, gex, un) == a.generate_options_score(iv, pc, gex, un)[0], (
                    iv, pc, gex)

    @pytest.mark.parametrize("iv,pc,un", _GRID)
    def test_same_score_and_summary_for_every_gex(self, iv, pc, un):
        """变红的变异：把 `gex_signal = 1.0` 改回 `2.0 if gex < -0.001 else 1.0`（任一写法），
        或把摘要里的「负 GEX 利于趋势」加回去。"""
        a = OptionsAnalyzer()
        outs = {gex: a.generate_options_score(iv, pc, gex, un) for gex in _GEX_VALUES}
        assert len(set(outs.values())) == 1, outs
        _score, summary = outs[None]
        assert "GEX" not in summary, summary
        # 夹具自证②：同一输入上旧公式对负 GEX 确实多 1.0 —— 且恰好 1.0（快照修正的精确性前提）
        for gex in _LEGACY_BITES:
            assert _legacy_options_score(iv, pc, gex, un) - outs[gex][0] == 1.0, (iv, pc, gex)

    def test_scale_unchanged_none_branch_value(self):
        """中性化取的是原 None 分支（1.0），不是 0 也不是 2 ⇒ 非负/None 标的分数逐位不变。
        变红的变异：`gex_signal = 0.0`（整体标度下移 1.0，所有标的都变）。"""
        a = OptionsAnalyzer()
        assert a.generate_options_score(50.0, 0.8, None, [])[0] == 6.0


# ════════════════════════════════════════════════════════════════════════════
# OracleBee 端到端：桩 OptionsAgent 走真公式
# ════════════════════════════════════════════════════════════════════════════

#: iv 50 → 3.0，P/C 0.8 → 2.0，GEX 1.0，异动 0 ⇒ 6.0（中性带内）；旧公式对负 GEX 给 7.0 ⇒ 越过 6.5 变看多
_IV, _PC = 50.0, 0.8


def _squeeze_replica(gex):
    """`OptionsAgent.analyze` 的 `gamma_squeeze_risk` 分档（独立复刻；与生产逐条链对得上，见
    `test_fresh_path_score_identical_for_negative_positive_and_none_gex`）。让桩的输出长得和真结果
    一样：Oracle 若按它加减分（复核 D2a：`== "low" ⇒ +1`），下面「分数不随 GEX 变」会红。"""
    if gex is None:
        return "unknown"
    return "high" if gex > 0.001 else "low" if gex < -0.001 else "medium"


def _options_stub(gex, *, legacy=False, raise_exc=None, extra=None):
    """新代码形状的 OptionsAgent 结果（带 OPT_MARKER、不带 MARKER）；`legacy` 只换旧公式的分数，
    `extra` 往结果里塞键（例如上游不该出现的 MARKER）。"""
    class _Stub:
        def analyze(self, ticker, stock_price=None):
            if raise_exc is not None:
                raise raise_exc
            score, summary = OptionsAnalyzer().generate_options_score(_IV, _PC, gex, [])
            if legacy:
                score = _legacy_options_score(_IV, _PC, gex, [])
            out = {"options_score": score, "signal_summary": summary, "gamma_exposure": gex,
                   "gamma_squeeze_risk": _squeeze_replica(gex),
                   "put_call_ratio": _PC, "iv_rank": _IV, "iv_skew_ratio": None,
                   "unusual_activity": [], OPT_MARKER: False}
            out.update(extra or {})
            return out
    return _Stub


def _run_oracle(monkeypatch, stub, *, stock=None, ticker=TICKER):
    import unusual_options
    monkeypatch.setattr(oa, "OptionsAgent", stub)
    monkeypatch.setattr(unusual_options, "detect_unusual_flow",
                        lambda t, stock_price=None: {"data_source": "fallback"})
    board = PheromoneBoard()
    bee = OracleBeeEcho(board)
    bee._prefetched_stock[ticker] = dict(stock or {"price": 100.0, "momentum_5d": 1.0})
    bee._prefetched_context[ticker] = ""
    return bee.analyze(ticker), board


class TestOracleScoreIgnoresGex:

    def test_score_and_direction_identical_across_gex(self, monkeypatch):
        """变红的变异：生产公式里的 +1 接回去；或在 OracleBee 里任何地方按 `gamma_exposure` 加减分 / 改方向；
        或按 `gamma_squeeze_risk` 加减分（复核 D2a：`== "low" ⇒ +1` / `== "high" ⇒ −1`）。"""
        # 夹具自证：五个 GEX 值覆盖了 gamma_squeeze_risk 的全部四档 —— 否则按档加减分的变异可能恰好不触发
        assert {_squeeze_replica(g) for g in _GEX_VALUES} == {"low", "unknown", "medium", "high"}
        outs = {gex: _run_oracle(monkeypatch, _options_stub(gex))[0] for gex in _GEX_VALUES}
        got = {gex: (o["score"], o["direction"]) for gex, o in outs.items()}
        assert len(set(got.values())) == 1, got
        assert got[None] == (6.0, "neutral"), f"前提：夹具落在中性带内，实得 {got[None]}"

    def test_fixture_bites_under_legacy_formula(self, monkeypatch):
        """夹具自证：同一夹具喂旧公式的 options_score，负 GEX 的 Oracle 分与方向**都**会变 ——
        否则上一条的「相同」可能是被 clamp / 融合权重吃掉了。"""
        new, _ = _run_oracle(monkeypatch, _options_stub(-5.0))
        old, _ = _run_oracle(monkeypatch, _options_stub(-5.0, legacy=True))
        assert old["score"] - new["score"] == pytest.approx(1.0)
        assert (new["direction"], old["direction"]) == ("neutral", "bullish")

    def test_gex_still_published_for_display(self, monkeypatch):
        """中性化的是「进分」，不是把 GEX 拔掉：板上照发 `gex`，details 照带 `gamma_exposure`。
        变红的变异：删掉 `_pub_details["gex"] = ...`（展示与 Bear 的 LLM 上下文会断）。"""
        out, board = _run_oracle(monkeypatch, _options_stub(-5.0))
        entry = board.get_agent_entry(TICKER, "OracleBeeEcho")
        assert entry is not None and entry.details.get("gex") == -5.0, entry
        assert out["details"]["gamma_exposure"] == -5.0


class TestOracleMarkerOnEveryPath:
    """`details.gex_signal_in_score is False` 是 v0.45.349 的世代判别物（`ic_rerun_readiness`
    的边界判别器读它）。任何一条返回路径缺键，那一行就会被认成旧代码。"""

    def _assert_marked(self, out):
        assert isinstance(out.get("details"), dict), out
        assert out["details"].get(MARKER) is False, out.get("details")

    def test_synthetic_swarm_fallback_marks_unconditionally(self):
        """`alpha_hive_daily_report._generate_synthetic_swarm_results`（`.swarm_results` 缺失时的回退）
        自己拼 `agent_details.OracleBeeEcho.details`，不经 OracleBee —— 那里也必须**无条件**写印记，
        否则某天走到回退，边界判别把缺键读成旧代码 ⇒ 误报 boundary_too_early。
        静态核：函数体里有 `<x>[MARKER] = False`，且它的祖先里没有 if / try / except（期权取数失败也要写）。
        变红的变异：删掉那行；把它挪进 `if _opts_agent:` 或 try 块里。"""
        src = (REPO_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_generate_synthetic_swarm_results")
        parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
        hits = []
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and node.value.value is False
                    and any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                            and t.slice.value == MARKER for t in node.targets)):
                chain, cur = [], node
                while cur in parents:
                    cur = parents[cur]
                    chain.append(type(cur).__name__)
                hits.append((node.lineno, chain))
        assert hits, "合成回退里没有无条件写 gex_signal_in_score = False"
        assert any(not ({"If", "Try", "ExceptHandler"} & set(chain)) for _ln, chain in hits), hits

    def test_success_path(self, monkeypatch):
        """变红的变异：删掉成功路径 details 里的 `"gex_signal_in_score": False`
        （新代码的 OptionsAgent 结果本就不带它，桩同此）。"""
        out, _ = _run_oracle(monkeypatch, _options_stub(-5.0))
        assert MARKER not in _options_stub(-5.0)().analyze(TICKER), "前提：上游结果里没有印记"
        assert "error" not in out
        self._assert_marked(out)

    def test_success_path_literal_overrides_options_result(self, monkeypatch):
        """字面量放在 `**result` 展开**之后**：即便上游结果写着 True（不该出现），也盖成 False ——
        标记描述的是 Oracle 自己的分数口径，不是抄上游。
        变红的变异：把字面量挪到 `**(result or {})` 之前。"""
        out, _ = _run_oracle(monkeypatch, _options_stub(-5.0, extra={MARKER: True}))
        self._assert_marked(out)

    def test_options_agent_failure_path(self, monkeypatch):
        """OptionsAgent 抛错 ⇒ result={}、options_score=5.0，照样要带标记。
        变红的变异：把标记改成从 OptionsAgent 结果里抄（`result.get(...)`）。"""
        out, _ = _run_oracle(monkeypatch, _options_stub(None, raise_exc=ConnectionError("桩：链不可用")))
        assert "error" not in out and out["data_quality"]["options"] == "fallback", "前提：走的是失败兜底"
        self._assert_marked(out)

    def test_agent_error_fallback_path(self, monkeypatch):
        """`_get_stock_data` 没有 price ⇒ KeyError ⇒ `make_error_result` 兜底，也要带标记。
        变红的变异：把 except 分支改回 `return make_error_result("OracleBeeEcho", "odds", e)`。"""
        out, _ = _run_oracle(monkeypatch, _options_stub(-5.0), stock={"momentum_5d": 1.0})
        assert out.get("error"), "前提：走的是异常兜底"
        self._assert_marked(out)

    def test_invalid_ticker_path(self, monkeypatch):
        """变红的变异：把 `return _mark_gex_out_of_score(_err)` 改回 `return _err`。"""
        out, _ = _run_oracle(monkeypatch, _options_stub(-5.0), ticker="bad ticker!")
        assert out.get("error") == "invalid_ticker", "前提：走的是无效 ticker"
        self._assert_marked(out)

    @pytest.mark.parametrize("path", ["success", "error"])
    def test_marker_survives_queen_into_archive_shape(self, monkeypatch, path):
        """写端 ↔ 读端契约：Oracle 的真输出经 `QueenDistiller.distill` 进 `agent_details` 后，
        v0.45.349 边界判别器认得出。
        变红的变异：QueenDistiller 的 agent_details 白名单不再抄 details；或判别器与写端键名不一致。"""
        import ic_rerun_readiness as rr
        from swarm_agents.queen_distiller import QueenDistiller

        stock = None if path == "success" else {"momentum_5d": 1.0}
        oracle, _ = _run_oracle(monkeypatch, _options_stub(-5.0), stock=stock)
        assert bool(oracle.get("error")) is (path == "error"), "前提：走对了路径"
        dims = {"ScoutBeeNova": "signal", "BuzzBeeWhisper": "sentiment",
                "ChronosBeeHorizon": "catalyst", "GuardBeeSentinel": "risk_adj",
                "RivalBeeVanguard": "ml_auxiliary", "BearBeeContrarian": "contrarian"}
        results = [{"source": a, "dimension": d, "direction": "neutral", "score": 5.0,
                    "confidence": 0.5, "discovery": f"{a} 合成", "data_quality": {"x": "real"},
                    "details": {"macro_regime": "neutral"} if a == "GuardBeeSentinel" else {}}
                   for a, d in dims.items()] + [oracle]
        board = PheromoneBoard()
        out = QueenDistiller(board, enable_llm=False, ml_model=None).distill(
            TICKER, copy.deepcopy(results), dealer_gex={"regime": "unknown"})
        assert out["agent_details"]["OracleBeeEcho"]["details"].get(MARKER) is False
        _desc, judge = rr._BOUNDARY_MARKERS["v0.45.349"]
        assert judge({"swarm_results": out}) is True
        # 反向：缺键的旧记录不被认成新口径
        legacy = copy.deepcopy(out)
        legacy["agent_details"]["OracleBeeEcho"]["details"].pop(MARKER)
        assert judge({"swarm_results": legacy}) is False


# ════════════════════════════════════════════════════════════════════════════
# BearBee：地板与「GEX 负值」信号已删
# ════════════════════════════════════════════════════════════════════════════

def _publish_oracle(board, gex, direction="neutral"):
    details = {"pc_ratio": 0.9, "iv_rank": 40.0, "iv_skew": 1.0}   # 三项都不触发看空档
    if gex is not None:
        details["gex"] = gex
    board.publish(PheromoneEntry(agent_id="OracleBeeEcho", ticker=TICKER, discovery="Oracle 合成",
                                 source="options", self_score=5.0, direction=direction,
                                 details=details))


def _legacy_assess(orig):
    """旧 `_assess_options_puts`：真方法 + 复刻被删掉的那三行（读板路径、地板在 Oracle 方向规则之前；
    本夹具的 Oracle 方向是 neutral，先后无差别）。"""
    def _wrapped(self, ticker, price, sigs, ds):
        ob, od = orig(self, ticker, price, sigs, ds)
        g = (od or {}).get("gex")
        if g is not None and g < 0:
            ob = max(ob, 5.0)
            sigs.append(f"GEX 负值 {g:,.0f}（做市商助跌）")
        return ob, od
    return _wrapped


@pytest.fixture
def bear_offline(monkeypatch):
    """其余八个分量钉成固定值（内幕给一个非零分量，免得只测到「无信号」那条分支）；期权分量走真方法。"""
    def _insider(self, ticker, sigs, ds):
        sigs.append("内幕净卖出（桩）")
        return 4.0, {"dollar_sold": 1.0, "dollar_bought": 0.0}
    for name, val in (("_assess_valuation", 0.0), ("_assess_momentum_decay", 0.0),
                      ("_assess_catalyst_risk", 0.0), ("_assess_ml_prediction", 0.0),
                      ("_assess_signal_consistency", 0.0)):
        monkeypatch.setattr(BearBeeContrarian, name, lambda self, *a, _v=val, **k: _v)
    monkeypatch.setattr(BearBeeContrarian, "_assess_insider_selling", _insider)
    monkeypatch.setattr(BearBeeContrarian, "_assess_news_sentiment", lambda self, *a, **k: (0.0, None))
    monkeypatch.setattr(BearBeeContrarian, "_assess_short_interest", lambda self, *a, **k: (0.0, None))

    import options_analyzer

    class _NoFallback:
        def analyze(self, *_a, **_k):
            raise AssertionError("读板应读到 Oracle，不该走 OptionsAgent 回落")
    monkeypatch.setattr(options_analyzer, "OptionsAgent", _NoFallback)

    def run(gex):
        board = PheromoneBoard()
        _publish_oracle(board, gex)
        bee = BearBeeContrarian(board)
        bee._prefetched_stock[TICKER] = {"price": 100.0, "momentum_5d": 1.0}
        bee._prefetched_context[TICKER] = ""
        return bee.analyze(TICKER)
    return run


def _bear_view(out):
    d = out["details"]
    return (out["score"], out["direction"], d["options_bear"], d["rule_bear_score"],
            tuple(d["bearish_signals"]))


class TestBearIgnoresGex:

    @pytest.mark.parametrize("gex", [-5.0, 5.0, None])
    def test_assess_options_puts_has_no_gex_floor(self, gex):
        """变红的变异：把 `if gex is not None and gex < 0: options_bear = max(options_bear, 5.0)` 加回去。"""
        board = PheromoneBoard()
        _publish_oracle(board, gex)
        sigs, ds = [], {}
        ob, od = BearBeeContrarian(board)._assess_options_puts(TICKER, 100.0, sigs, ds)
        assert ds.get("options") == "real", "前提：读板读到了 Oracle"
        assert ob == 0.0, (gex, ob, sigs)
        assert not any("GEX" in s for s in sigs), sigs
        assert od["gex"] == gex, "gex 仍作为 LLM 论点上下文随 options_data 传出（不参与规则分）"

    def test_full_analyze_identical_across_gex(self, bear_offline):
        """变红的变异：同上；或在 Bear 任何别处按 Oracle 的 `gex` 符号改 options_bear / 分数 / 方向。"""
        views = {gex: _bear_view(bear_offline(gex)) for gex in (-5.0, 5.0, None)}
        assert len(set(views.values())) == 1, views
        assert not any("GEX" in s for s in views[None][4])

    def test_fixture_bites_under_legacy_floor(self, monkeypatch, bear_offline):
        """夹具自证：同一夹具换回旧地板，负 GEX 的 options_bear / 分数 / 信号都变 ——
        否则上一条的「相同」可能是期权分量根本没进综合分。"""
        new = _bear_view(bear_offline(-5.0))
        monkeypatch.setattr(BearBeeContrarian, "_assess_options_puts",
                            _legacy_assess(BearBeeContrarian._assess_options_puts))
        old = _bear_view(bear_offline(-5.0))
        assert (new[2], old[2]) == (0.0, 5.0)
        assert old[0] != new[0] and old[3] != new[3], (old, new)
        assert any("GEX 负值" in s for s in old[4])


# ════════════════════════════════════════════════════════════════════════════
# OptionsAgent：三条返回路径带标记 + 旧快照修正
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def fresh_agent(monkeypatch, stub_cboe_payload, stub_yfinance):
    """真跑 analyze() 的出口路径。旁支取数（期限结构 / 全链 OI / Gamma 日历 / IV-RV）在源头钉成
    「取不到」（conftest 的显式源桩）；期限结构的 yfinance 支路自带重试，直接给空；IV-RV 价差（只进展示、
    不进评分）在桩掉的 yfinance 上每次白等约 2 秒重试，本文件要真跑 analyze() 十来次，也直接给空。"""
    import market_intelligence
    monkeypatch.setattr(OptionsAnalyzer, "_iv_term_points_yfinance",
                        staticmethod(lambda ticker, stock_price: ([], ["offline: 本文件禁止出网"])))
    monkeypatch.setattr(market_intelligence, "calculate_iv_rv_spread", lambda *_a, **_k: {})
    a = OptionsAgent()
    monkeypatch.setattr(a.fetcher, "fetch_historical_hv", lambda t: [0.25 + i * 0.02 for i in range(20)])
    monkeypatch.setattr(a.fetcher, "_save_last_valid_iv", lambda t, iv: None)
    monkeypatch.setattr(a.fetcher, "_read_last_valid_iv", lambda t: None)
    return a


#: 三条链只差 gamma（OI / IV / 行权价逐位相同 ⇒ P/C、IV Rank、异动都相同），GEX 分落负 / 正 / 算不出。
#: gamma 只进 `calculate_gamma_exposure`（全模块 grep `"gamma"` 的唯一读点）；全零 ⇒ total=0 ⇒ None。
_CHAIN_GAMMAS = {"negative": (0.01, 0.06), "positive": (0.06, 0.01), "none": (0.0, 0.0)}


def _chain(call_gamma, put_gamma):
    calls = [{"strike": 140, "openInterest": 3000, "impliedVolatility": 0.34, "gamma": call_gamma},
             {"strike": 150, "openInterest": 3000, "impliedVolatility": 0.30, "gamma": call_gamma}]
    puts = [{"strike": 140, "openInterest": 3000, "impliedVolatility": 0.38, "gamma": put_gamma},
            {"strike": 145, "openInterest": 3000, "impliedVolatility": 0.35, "gamma": put_gamma}]
    return {"calls": calls, "puts": puts, "expirations": ["2026-10-16"], "source": "real"}


def _run_fresh(agent, monkeypatch, kind):
    monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain(*_CHAIN_GAMMAS[kind]))
    return agent.analyze("NVDA", stock_price=145.0)


class TestOptionsAgentReturnPathsCarryMarker:

    def test_fresh_path_score_identical_for_negative_positive_and_none_gex(self, fresh_agent, monkeypatch):
        """真跑 analyze()：负 / 正 / 算不出三条链的 options_score 与摘要逐位相同，都带 OPT_MARKER。
        只测负 GEX 链接不住「正 GEX 扣分」（复核 D2b：`if gex > 0.001: options_score -= 1` 曾全绿）。
        变红的变异：删掉出口 result 里的 `OPTIONS_GEX_MARKER: False`；公式 +1 接回；或在 analyze 里
        按局部 `gex`（函数返回值，AST 键读守卫看不见）对任一符号加减分。"""
        rs = {k: _run_fresh(fresh_agent, monkeypatch, k) for k in _CHAIN_GAMMAS}
        gex = {k: r["gamma_exposure"] for k, r in rs.items()}
        # 前提①：三条链真的落在旧阈值的三侧
        assert gex["negative"] < -0.001 and gex["positive"] > 0.001 and gex["none"] is None, gex
        # 前提②：评分的其余三个输入逐位相同 ⇒ 分数若不同只能来自 GEX
        rest = {(r["iv_rank"], r["put_call_ratio"],
                 json.dumps(r["unusual_activity"], sort_keys=True, default=str)) for r in rs.values()}
        assert len(rest) == 1, rest
        # 前提③：桩用的分档复刻与生产逐条对得上（Oracle 端到端测试靠它咬 D2a）
        assert {k: r["gamma_squeeze_risk"] for k, r in rs.items()} == {
            k: _squeeze_replica(g) for k, g in gex.items()}
        scores = {k: (r["options_score"], r["signal_summary"]) for k, r in rs.items()}
        assert len(set(scores.values())) == 1, scores
        r0 = rs["none"]
        expect = OptionsAnalyzer().generate_options_score(
            r0["iv_rank"], r0["put_call_ratio"], None, r0["unusual_activity"])[0]
        assert r0["options_score"] == expect
        assert all(r[OPT_MARKER] is False for r in rs.values())
        assert _legacy_options_score(r0["iv_rank"], r0["put_call_ratio"], gex["negative"],
                                     r0["unusual_activity"]) == expect + 1.0, "夹具自证：旧公式会多 1.0"

    def test_sample_chain_early_exit_carries_marker(self, fresh_agent, monkeypatch):
        """变红的变异：删掉样本链早退 dict 里的 `OPTIONS_GEX_MARKER: False`。"""
        monkeypatch.setattr(fresh_agent.fetcher, "fetch_options_chain",
                            lambda t: {"calls": [], "puts": [], "expirations": [], "source": "sample"})
        r = fresh_agent.analyze("NVDA", stock_price=145.0)
        assert r["data_quality"] == "unavailable", "前提：走的是样本链早退"
        assert r[OPT_MARKER] is False


class _Fetched(RuntimeError):
    """探针：快照未命中后的第一个取数动作。"""


_SESSION = "2026-09-25"
_NOW = datetime(2026, 9, 25, 14, 0, tzinfo=PT)          # 周五收盘后
_SNAP_TS = "2026-09-25T13:30:00-07:00"


def _legacy_snapshot(gex, **over):
    """旧代码写的快照：无标记，options_score 用旧公式算（iv 50 / P/C 0.8 / 无异动）。"""
    score = _legacy_options_score(_IV, _PC, gex, [])
    summary = "IV 处于理想水位" + (" | 负 GEX 利于趋势" if gex is not None and gex < -0.001 else "")
    snap = {"iv_rank": _IV, "put_call_ratio": _PC, "gamma_exposure": gex, "unusual_activity": [],
            "options_score": score, "signal_summary": summary, "rv_30d": 30.0,
            "_snapshot_timestamp": _SNAP_TS, "_snapshot_session": _SESSION,
            "_snapshot_session_complete": True}
    snap.update(over)
    return snap


@pytest.fixture
def snap_agent(tmp_path, monkeypatch):
    """同 tests/test_snapshot_session_slot.py：解开 conftest 的快照禁用，cache_dir 指向 tmp，
    取数动作换成探针（走到它 = 没命中快照）。两个回写者换成间谍：记录它们被调用时看到的分数。"""
    monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
    monkeypatch.delenv("ALPHA_HIVE_TARGET_DATE", raising=False)
    monkeypatch.setattr(oa, "_snapshot_now", lambda: _NOW)
    oa.reset_snapshot_slot_stats()
    a = OptionsAgent()
    monkeypatch.setattr(a.fetcher, "cache_dir", str(tmp_path))

    def _probe(*_a, **_k):
        raise _Fetched
    monkeypatch.setattr(a.fetcher, "fetch_options_chain", _probe)
    a.seen_by_refreshers = []
    monkeypatch.setattr(a, "_refresh_price_derived",
                        lambda c, *x, **k: a.seen_by_refreshers.append((c.get("options_score"), c.get(OPT_MARKER))))
    monkeypatch.setattr(a, "_refill_empty_quote_set", lambda *x, **k: False)
    yield a
    oa.reset_snapshot_slot_stats()


def _write_snap(tmp_path, snap, ticker="NVDA"):
    p = tmp_path / f"options_snapshot_{ticker}_{_SESSION}.json"
    p.write_text(json.dumps(snap), encoding="utf-8")
    return p


class TestLegacySnapshotCorrection:

    def test_session_pin_hits(self, snap_agent, tmp_path):
        """前提：夹具的时钟 / 槽位真的命中（否则后面全是「没命中所以没修」的假绿）。"""
        _write_snap(tmp_path, _legacy_snapshot(5.0))
        snap_agent.analyze("NVDA", stock_price=100.0)
        assert oa.snapshot_slot_stats()["hits"] == 1

    @pytest.mark.parametrize("gex", _LEGACY_BITES)
    def test_hit_on_legacy_snapshot_is_corrected_exactly(self, snap_agent, tmp_path, gex, caplog):
        """变红的变异：删掉命中路径的 `self._drop_legacy_gex_signal(...)` 调用；或把减数写成别的值。"""
        p = _write_snap(tmp_path, _legacy_snapshot(gex))
        with caplog.at_level(logging.WARNING, logger="alpha_hive"):
            r = snap_agent.analyze("NVDA", stock_price=100.0)
        new_score, new_summary = OptionsAnalyzer().generate_options_score(_IV, _PC, gex, [])
        assert r["options_score"] == new_score                       # 精确相等，不是 approx
        assert r["signal_summary"] == new_summary == "IV 处于理想水位"
        assert r[OPT_MARKER] is False and MARKER not in r
        assert r["_gex_signal_legacy_correction"]["options_score_before"] == new_score + 1.0
        # 谁会红：按份计数（进 status.json 的 scan_timing）+ WARNING
        import scan_timing
        assert scan_timing.counters()["options_snapshot"]["gex_signal_corrected"] == 1
        assert any("负 GEX +1.0" in rec.getMessage() for rec in caplog.records), caplog.text
        # 回写：文件里分数与标记同进同出
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        assert (on_disk["options_score"], on_disk[OPT_MARKER]) == (new_score, False)
        assert MARKER not in on_disk, "修过回写的快照也不许带 Oracle 印记（复核 D1）"
        # 排在两个回写者之前：它们看到的已是修过、带标记的版本
        assert snap_agent.seen_by_refreshers == [(new_score, False)]

    def test_idempotent_across_repeated_hits(self, snap_agent, tmp_path):
        """同一份快照一轮扫描被读 3~4 次（Oracle / Bear / advanced_analyzer / 日报收尾），只能减一次。
        变红的变异：去掉开头的 `cached.get(OPTIONS_GEX_MARKER) is False` 早退，或修完不打标记。"""
        _write_snap(tmp_path, _legacy_snapshot(-5.0))
        scores = [snap_agent.analyze("NVDA", stock_price=100.0)["options_score"] for _ in range(4)]
        assert scores == [6.0] * 4, scores
        assert oa.snapshot_slot_stats()["gex_signal_corrected"] == 1

    def test_idempotent_on_same_dict_and_when_write_back_fails(self, tmp_path, caplog):
        """同一个 dict 修两次只减一次；回写失败（路径是目录）时每次重读都从原值减一次，不累积。
        变红的变异：同上。"""
        oa.reset_snapshot_slot_stats()
        agent = OptionsAgent()
        d = _legacy_snapshot(-5.0)
        assert agent._drop_legacy_gex_signal(d, "NVDA") is True
        assert agent._drop_legacy_gex_signal(d, "NVDA") is False
        assert d["options_score"] == 6.0
        with caplog.at_level(logging.WARNING, logger="alpha_hive"):
            for _ in range(2):
                fresh = _legacy_snapshot(-5.0)
                assert agent._drop_legacy_gex_signal(fresh, "NVDA", str(tmp_path)) is True
                assert fresh["options_score"] == 6.0
        assert any("回写失败" in rec.getMessage() for rec in caplog.records), "回写失败要看得见"
        oa.reset_snapshot_slot_stats()

    @pytest.mark.parametrize("snap", [
        pytest.param(_legacy_snapshot(-5.0, **{OPT_MARKER: False, "options_score": 7.0}), id="marker-present"),
        # 改名（复核 D1）前的同版草稿写的：只带旧键名 = False，分数早已不含 GEX ⇒ 不许再减一次
        pytest.param(_legacy_snapshot(-5.0, **{MARKER: False, "options_score": 7.0}), id="draft-key-name"),
        pytest.param(_legacy_snapshot(-0.001), id="gex-at-threshold"),
        pytest.param(_legacy_snapshot(0.0), id="gex-zero"),
        pytest.param(_legacy_snapshot(5.0), id="gex-positive"),
        pytest.param(_legacy_snapshot(None), id="gex-none"),
    ])
    def test_untouched_when_marker_present_or_gex_not_negative(self, snap_agent, tmp_path, snap):
        """有标记（新键名，或改名前草稿的旧键名）⇒ 新代码写的（或已修过），分数原样；
        GEX ≥ -0.001 / None ⇒ 旧公式本就给 1.0，分数原样。都不回写文件；返回值一律带 OPT_MARKER、
        不带 Oracle 印记。
        变红的变异：判据写成 `gex < 0`（-0.001 那行被误减）或 `gex <= -0.001`；或不看标记；
        或不认旧键名（draft-key-name 被再减 1.0）；或旧键名只读不删（返回值带着它交给旧 Oracle）。"""
        p = _write_snap(tmp_path, snap)
        before = p.read_bytes()
        r = snap_agent.analyze("NVDA", stock_price=100.0)
        assert r["options_score"] == snap["options_score"]
        assert r[OPT_MARKER] is False
        assert MARKER not in r
        assert "_gex_signal_legacy_correction" not in r
        assert p.read_bytes() == before
        assert oa.snapshot_slot_stats()["gex_signal_corrected"] == 0

    def test_non_numeric_score_is_marked_not_corrected(self, caplog):
        """分数不是数（下游 `_safe_score` 兜成 5.0，本就不含 +1）⇒ 不修、只打标记、留一条 WARNING。"""
        agent = OptionsAgent()
        d = _legacy_snapshot(-5.0, options_score=None)
        with caplog.at_level(logging.WARNING, logger="alpha_hive"):
            assert agent._drop_legacy_gex_signal(d, "NVDA") is False
        assert d["options_score"] is None and d[OPT_MARKER] is False
        assert any("不是数" in rec.getMessage() for rec in caplog.records), caplog.text

    def test_correction_exact_over_the_whole_grid(self):
        """精确性前提在全网格上成立：旧快照分 −1.0 ≡ 新公式分（== 比较，浮点逐位）。"""
        agent = OptionsAgent()
        a = OptionsAnalyzer()
        for iv, pc, un in _GRID:
            for gex in _LEGACY_BITES:
                d = {"gamma_exposure": gex, "options_score": _legacy_options_score(iv, pc, gex, un),
                     "signal_summary": "负 GEX 利于趋势"}
                assert agent._drop_legacy_gex_signal(d, "T") is True
                assert d["options_score"] == a.generate_options_score(iv, pc, gex, un)[0], (iv, pc, gex)
                assert d["signal_summary"] == "信号平衡"


# ════════════════════════════════════════════════════════════════════════════
# 复核 D1：OptionsAgent 的任何出口都不带 Oracle 印记（旧 Oracle 会把它原样抄走）
# ════════════════════════════════════════════════════════════════════════════

def _legacy_oracle_details(result):
    """v0.45.348 OracleBee 成功路径的 details 拼法（独立复刻）：OptionsAgent 结果整个展开，
    **没有**自己的印记字面量（那是 v0.45.349 才加的）。"""
    return {**(result or {}), "term_structure": {}, "deep_skew": {}, "max_pain": {}}


def _legacy_oracle_judged_new(result) -> bool:
    """旧 Oracle 拿着这份 OptionsAgent 结果，归档后会不会被 v0.45.349 边界判别器认成新口径。"""
    import ic_rerun_readiness as rr
    _desc, judge = rr._BOUNDARY_MARKERS["v0.45.349"]
    return judge({"swarm_results": {"agent_details": {
        "OracleBeeEcho": {"details": _legacy_oracle_details(result)}}}})


@pytest.fixture
def snap_fresh_agent(fresh_agent, tmp_path, monkeypatch):
    """fresh_agent + 解开快照禁用、cache_dir 指向 tmp、时钟钉住 ⇒ 第一次 analyze 真算并**写**快照，
    第二次命中。D1 的现场正是「新代码写的快照被同会话旧代码读」，所以写盘的那份也要验。"""
    monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
    monkeypatch.delenv("ALPHA_HIVE_TARGET_DATE", raising=False)
    monkeypatch.setattr(oa, "_snapshot_now", lambda: _NOW)
    monkeypatch.setattr(fresh_agent.fetcher, "cache_dir", str(tmp_path))
    oa.reset_snapshot_slot_stats()
    yield fresh_agent
    oa.reset_snapshot_slot_stats()


class TestOptionsResultNeverCarriesOracleMarker:
    """复核 D1 的复现：旧 OracleBee（`{**result, ...}`、无自有字面量）拿到新代码 OptionsAgent 的任一
    出口 ⇒ details 里**不许**出现 `gex_signal_in_score`，边界判别器必须判「旧口径」。
    （复核实测：两键同名时旧 Oracle 带印记、旧 Bear 地板照触发 ⇒ 那一天被错认成新口径。）
    生产 cache 里没有按旧键名修过的快照（2026-09-27 只读核对 1601 份，0 份），故不做迁移；
    改名前草稿写的槽位由 `_drop_legacy_gex_signal` 在内存里认旧键、删旧键。"""

    def test_probe_bites(self):
        """夹具自证：结果里真带印记时，旧 Oracle 复刻 + 判别器确实判「新口径」—— 否则下面全绿恒真。"""
        assert _legacy_oracle_judged_new({MARKER: False}) is True
        assert _legacy_oracle_judged_new({OPT_MARKER: False}) is False

    @pytest.mark.parametrize("kind", list(_CHAIN_GAMMAS))
    def test_fresh_result_and_the_snapshot_it_writes(self, snap_fresh_agent, monkeypatch, tmp_path, kind):
        """真算路径：返回值、写进 cache 的快照、同会话第二次命中返回的，都不带印记。
        变红的变异：把 `OPTIONS_GEX_MARKER` 改回 `"gex_signal_in_score"`（复核 D1 的原状）。"""
        r1 = _run_fresh(snap_fresh_agent, monkeypatch, kind)
        files = list(tmp_path.glob("options_snapshot_NVDA_*.json"))
        assert len(files) == 1 and oa.snapshot_slot_stats()["writes"] == 1, ("前提：真写了快照", files)
        on_disk = json.loads(files[0].read_text(encoding="utf-8"))
        r2 = snap_fresh_agent.analyze("NVDA", stock_price=145.0)
        assert oa.snapshot_slot_stats()["hits"] == 1, "前提：第二次命中快照"
        for name, d in (("返回值", r1), ("写盘快照", on_disk), ("命中返回", r2)):
            assert d.get(OPT_MARKER) is False, (name, "前提：新代码形状")
            assert MARKER not in d, name
            assert _legacy_oracle_judged_new(d) is False, name

    def test_sample_chain_early_exit(self, fresh_agent, monkeypatch):
        """变红的变异：同上。"""
        monkeypatch.setattr(fresh_agent.fetcher, "fetch_options_chain",
                            lambda t: {"calls": [], "puts": [], "expirations": [], "source": "sample"})
        r = fresh_agent.analyze("NVDA", stock_price=145.0)
        assert r["data_quality"] == "unavailable", "前提：走的是样本链早退"
        assert MARKER not in r and _legacy_oracle_judged_new(r) is False

    @pytest.mark.parametrize("snap", [
        pytest.param(_legacy_snapshot(-5.0), id="legacy-corrected"),
        pytest.param(_legacy_snapshot(5.0), id="legacy-untouched"),
        pytest.param(_legacy_snapshot(-5.0, **{OPT_MARKER: False, "options_score": 6.0}), id="new-code"),
        pytest.param(_legacy_snapshot(-5.0, **{MARKER: False, "options_score": 6.0}), id="draft-key-name"),
    ])
    def test_snapshot_hits(self, snap_agent, tmp_path, snap):
        """命中路径：修过的、原样的、新代码写的、改名前草稿写的 —— 返回值都不带印记；
        文件里原本没有的印记，新代码也不许写进去（修正回写那份）。
        变红的变异：键改回同名；或 `_drop_legacy_gex_signal` 对旧键名只 `.get` 不 `.pop`（draft-key-name 红）。"""
        p = _write_snap(tmp_path, snap)
        r = snap_agent.analyze("NVDA", stock_price=100.0)
        assert oa.snapshot_slot_stats()["hits"] == 1, "前提：命中"
        assert r[OPT_MARKER] is False
        assert MARKER not in r and _legacy_oracle_judged_new(r) is False
        if MARKER not in snap:
            assert MARKER not in json.loads(p.read_text(encoding="utf-8"))


class TestOracleMarkerOnlyProducedByOracle:
    """复核 D1 的静态面：世代印记键 `gex_signal_in_score` 的**产出点**只许在 OracleBee 与日报合成回退。

    产出 = 这个字符串常量出现在「读」以外的任何位置：dict 字面量的键、`d[K] = …` 的下标、
    `X = K`（先存进常量再当键用 —— v0.45.349 草稿的 `GEX_SIGNAL_MARKER = "gex_signal_in_score"` 正是这样）、
    `setdefault(K, …)`、关键字参数 `dict(gex_signal_in_score=…)`。
    读 = `.get(K…)` / `.pop(K…)` 的首参、`d[K]` 取值或 del、`K in d` / `K not in d`。docstring 里提到不算（不相等）。
    按文件钉（双向）：生产文件的函数改名不必动这里；新增产出者 / 两处之一不再产出都会红。
    ⚠️ 抓不到拼接出来的键名（`"gex_signal_" + "in_score"`）—— 那由上面的行为测试兜。"""

    PRODUCER_FILES = {
        "swarm_agents/oracle_bee.py": "OracleBee 每条返回路径无条件写字面量（`_mark_gex_out_of_score` + 成功路径 details）",
        "alpha_hive_daily_report.py": "`.swarm_results` 缺失时的合成回退自己拼 OracleBee details，同样无条件写",
    }
    _READ_METHODS = frozenset({"get", "pop"})

    @classmethod
    def _classify(cls, tree):
        """返回 (产出行号, 读行号)。"""
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        prod, read = set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.keyword) and n.arg == MARKER:
                prod.add(n.value.lineno)
                continue
            if not (isinstance(n, ast.Constant) and n.value == MARKER):
                continue
            p = parents.get(n)
            if ((isinstance(p, ast.Call) and isinstance(p.func, ast.Attribute)
                    and p.func.attr in cls._READ_METHODS and p.args and p.args[0] is n)
                    or (isinstance(p, ast.Subscript) and p.slice is n
                        and isinstance(p.ctx, (ast.Load, ast.Del)))
                    or (isinstance(p, ast.Compare) and p.left is n
                        and all(isinstance(o, (ast.In, ast.NotIn)) for o in p.ops))):
                read.add(n.lineno)
            else:
                prod.add(n.lineno)
        return sorted(prod), sorted(read)

    @classmethod
    def _scan(cls, root=None):
        """返回 ({相对路径: 产出行号}, {相对路径: 读行号})。"""
        root = Path(root) if root is not None else REPO_ROOT
        prod, read = {}, {}
        for py in own_python_files(root)[0]:
            rel = py.relative_to(root)
            if rel.parts[0] in TestGexKeyComparisonsAreKnown._SKIP_TOP:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            p_lines, r_lines = cls._classify(tree)
            if p_lines:
                prod[rel.as_posix()] = p_lines
            if r_lines:
                read[rel.as_posix()] = r_lines
        return prod, read

    def test_only_oracle_and_synthetic_fallback_produce_the_marker(self):
        """变红的变异：把 `OPTIONS_GEX_MARKER` 改回 `"gex_signal_in_score"`（options_analyzer.py 成了产出者）；
        或删掉 OracleBee / 合成回退的字面量（产出者少一个）。"""
        prod, read = self._scan()
        # 探针自证：认得出生产代码里的「读」（判别器 `.get`、快照修正 `.pop`）—— 否则分类器可能把一切都判成读
        assert "ic_rerun_readiness.py" in read and "options_analyzer.py" in read, read
        assert set(prod) == set(self.PRODUCER_FILES), (
            f"印记 {MARKER!r} 的产出者变了：{prod}。它是 v0.45.349 的世代判别物，只能由 OracleBee "
            "（及合成回退）无条件写字面量；OptionsAgent 的标记用 options_analyzer.OPTIONS_GEX_MARKER（复核 D1）。")

    @staticmethod
    def _plant(root):
        (root / "swarm_agents").mkdir(parents=True)
        (root / "swarm_agents" / "oracle_bee.py").write_text(
            "def mark(out):\n"
            "    out['details']['gex_signal_in_score'] = False\n"
            "    return {**out, 'gex_signal_in_score': False}\n", encoding="utf-8")
        (root / "alpha_hive_daily_report.py").write_text(
            "def synth(d):\n    d['gex_signal_in_score'] = False\n", encoding="utf-8")
        (root / "options_analyzer.py").write_text(         # 复核 D1 的原状：先存常量再当键
            "OPTIONS_GEX_MARKER = 'gex_signal_in_score'\n\n\n"
            "def analyze():\n    return {OPTIONS_GEX_MARKER: False}\n", encoding="utf-8")
        (root / "other.py").write_text(
            "def f(d):\n"
            "    d.setdefault('gex_signal_in_score', False)\n"
            "    return dict(gex_signal_in_score=False)\n", encoding="utf-8")
        (root / "reader.py").write_text(
            '"""文档里提到 gex_signal_in_score 不算。"""\n\n\n'
            "def judge(det):\n"
            "    if 'gex_signal_in_score' in det and det['gex_signal_in_score'] is False:\n"
            "        det.pop('gex_signal_in_score', None)\n"
            "    del det['gex_signal_in_score']\n"
            "    return det.get('gex_signal_in_score') is False\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "x.py").write_text("D = {'gex_signal_in_score': False}\n", encoding="utf-8")

    def test_pathology_is_caught_and_reads_are_not(self, tmp_path):
        """变红的变异：把 `X = K`（Assign 的值）当读（options_analyzer.py 漏报）；漏掉关键字参数 / setdefault
        （other.py 行号不全）；把 `in` / `.pop` / del 当产出（reader.py 误报）；清空 `_SKIP_TOP`（tests/ 误报）。"""
        self._plant(tmp_path)
        prod, read = self._scan(tmp_path)
        assert prod == {"swarm_agents/oracle_bee.py": [2, 3], "alpha_hive_daily_report.py": [2],
                        "options_analyzer.py": [1], "other.py": [2, 3]}, prod
        assert read == {"reader.py": [5, 6, 7, 8]}, read


# ════════════════════════════════════════════════════════════════════════════
# 静态守卫：GEX 键读出值的大小比较 / 算术 / 排序，GEX 分档的判等 / 查表，只许出现在名单里
# ════════════════════════════════════════════════════════════════════════════

class TestGexKeyComparisonsAreKnown:
    """生产代码里拿 GEX 做判断的地方只许出现在 ALLOWED 名单里（按「文件 :: 函数」钉，双向）。

    两类键、两套口径：
      · 数值键 `"gamma_exposure"` / `"gex"`：`<`/`>`/`<=`/`>=`、算术（BinOp / AugAssign / 一元正负）、
        `max` / `min` / `sorted` / `.sort` / `nlargest` / `nsmallest`（含 `key=lambda`）算「拿来做判断」；
        `is None` / `==` 不算（展示层判缺失 / 判零）。
      · 类别键 `"gamma_squeeze_risk"`（OptionsAgent 按 GEX 符号分档 high / low / medium / unknown，
        等价于 GEX 符号）：上面的大小比较之外，`==` / `!=` / `in` / `not in` 与**查表**
        （`BONUS[risk]` / `BONUS.get(risk)`）也算 —— 复核 D2a 的 `if result.get("gamma_squeeze_risk") == "low":
        options_score += 1` 就是它。拼字符串（`"风险：" + risk`）不算。
    「读到键」= 操作数子树里 ① 有键读 `x["k"]` / `x.get("k", …)`，或 ② 有一个在**同一作用域**里被赋值为
    「含键读的表达式」的名字（一层污点）。污点来源：`=` / 注解赋值 / `+=` / `:=`、**元组 / 列表 / 星号解包**
    （右边是等长字面元组时逐位配对：`a, g = d.get("iv"), d.get("gex")` 只污染 g —— 复核 D2c）、
    `for` 与推导式的循环目标（遍历的东西含键读）。lambda 体算所在函数的一部分（复核 D2d：
    `filter(lambda e: (e.get("gex") or 0) < 0, …)`）。f-string 里的键读（格式化展示）不算。

    ⚠️ 抓不到的：GEX 以函数参数传进来（`generate_options_score(…, gex, …)` / `OptionsAgent.analyze` 的局部
    `gex` 是函数返回值 —— 复核 D2b），或经两层以上赋值才进判断（污点做成传递闭包会把整个容器 dict 染上，
    实测在日报合成回退里造出误报，故不做）、`match` 语句。这些由上面的行为测试兜（对 Oracle / Bear /
    options_score 而言）。跳过 tests/、experiments/（离线代码；重放要在旧记录上复现旧逻辑）；
    `.claude/` 等点号目录由 `own_python_files` 排除（嵌套 worktree 的陈旧副本）。
    """

    #: (相对仓库根, 函数限定名) → 为什么允许。v0.45.349 起逐条从全树扫描结果里核过，全是展示
    #: v0.45.362 删 `dashboard_renderer.py::_detail`：网站 GEX 改读蒸馏结果的 `gex_state`
    #: （全到期日视图、政体路由用的那份），不再读 Oracle 主链 `gamma_exposure`（守卫 tests/test_gex_state.py）。
    ALLOWED = {
        ("generate_deep_v2.py", "_build_options_narrative"):
            "展示：深度报告期权段按 `squeeze == 'high' / 'medium'`（`ctx.get('gamma_squeeze_risk')`）选一句解读文案",
        ("generate_ml_report.py", "MLEnhancedReportGenerator._ch3_oracle"):
            "展示：ML 报告第 3 章表格按 `str(gex).lower() in ('high','很高')` 选信号圆点"
            "（这里的 `gex` 装的是 `gamma_squeeze_risk`）",
        ("options_analyzer.py", "OptionsAgent._drop_legacy_gex_signal"):
            "旧快照修正：识别旧公式 `gex < -0.001` 给过 +1.0 的快照并减掉（v0.45.349）",
    }
    _SKIP_TOP = {"tests", ".git", "experiments"}
    _NUM_KEYS = frozenset({"gamma_exposure", "gex"})
    _CAT_KEYS = frozenset({"gamma_squeeze_risk"})
    _ORDER_OPS = (ast.Lt, ast.Gt, ast.LtE, ast.GtE)
    _EQ_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)          # 只对类别键算
    _ORDER_FUNCS = frozenset({"max", "min", "sorted"})
    _ORDER_METHODS = frozenset({"sort", "nlargest", "nsmallest"})

    @classmethod
    def _key_of(cls, n):
        """n 是对被盯键的读就返回键名，否则 None。"""
        if isinstance(n, ast.Subscript):
            s = n.slice
            if isinstance(s, ast.Constant) and s.value in cls._NUM_KEYS | cls._CAT_KEYS:
                return s.value
            return None
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and n.args):
            a = n.args[0]
            if isinstance(a, ast.Constant) and a.value in cls._NUM_KEYS | cls._CAT_KEYS:
                return a.value
        return None

    @classmethod
    def _walk_no_fstring(cls, node):
        """ast.walk，但不进 f-string（格式化展示不算读值做运算）。"""
        stack = [node]
        while stack:
            n = stack.pop()
            yield n
            if isinstance(n, ast.JoinedStr):
                continue
            stack.extend(ast.iter_child_nodes(n))

    @classmethod
    def _kinds(cls, node, tainted) -> set:
        """node 子树读到的键类别（"num" / "cat" 的子集）：直接键读，或同作用域里被污染的名字。"""
        out = set()
        for n in cls._walk_no_fstring(node):
            k = cls._key_of(n)
            if k is not None:
                out.add("cat" if k in cls._CAT_KEYS else "num")
            elif isinstance(n, ast.Name) and n.id in tainted:
                out |= tainted[n.id]
        return out

    @classmethod
    def _scope_nodes(cls, scope):
        """作用域自身的节点（不进嵌套的函数 / 类；**进** lambda 与推导式 —— 它们的体算这个作用域的）。"""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            n = stack.pop()
            yield n
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.extend(ast.iter_child_nodes(n))

    @classmethod
    def _bind(cls, target, value, out, *, pair=True):
        """value 含键读 ⇒ target 里的名字记上对应类别。`pair`：target 与 value 都是等长字面元组 / 列表
        （无星号）时逐位配对，否则整体污染（星号解包、右边是调用、for / 推导式的循环目标）。"""
        if (pair and isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
                and len(target.elts) == len(value.elts)
                and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))):
            for t, v in zip(target.elts, value.elts):
                cls._bind(t, v, out)
            return
        kinds = cls._kinds(value, {})                      # 一层：值里的名字不再追
        if kinds:
            for n in ast.walk(target):
                if isinstance(n, ast.Name):
                    out.setdefault(n.id, set()).update(kinds)

    @classmethod
    def _tainted(cls, scope) -> dict:
        out: dict = {}
        for n in cls._scope_nodes(scope):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    cls._bind(t, n.value, out)
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and n.value is not None:
                cls._bind(n.target, n.value, out)
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                cls._bind(n.target, n.iter, out, pair=False)
            elif isinstance(n, ast.comprehension):
                cls._bind(n.target, n.iter, out, pair=False)
        return out

    @classmethod
    def _is_hit(cls, n, tainted) -> bool:
        if isinstance(n, ast.Compare):
            kinds = set().union(*(cls._kinds(o, tainted) for o in (n.left, *n.comparators)))
            return bool((kinds and any(isinstance(o, cls._ORDER_OPS) for o in n.ops))
                        or ("cat" in kinds and any(isinstance(o, cls._EQ_OPS) for o in n.ops)))
        if isinstance(n, ast.BinOp):
            operands = (n.left, n.right)
        elif isinstance(n, ast.AugAssign):
            operands = (n.target, n.value)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            operands = (n.operand,)
        elif isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Name) and n.func.id in cls._ORDER_FUNCS)
                or (isinstance(n.func, ast.Attribute) and n.func.attr in cls._ORDER_METHODS)):
            operands = (*n.args, *(kw.value for kw in n.keywords))
        elif isinstance(n, ast.Subscript) and cls._key_of(n) is None:          # 类别查表 `BONUS[risk]`
            return "cat" in cls._kinds(n.slice, tainted)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and n.args and cls._key_of(n) is None):                         # 类别查表 `BONUS.get(risk)`
            return "cat" in cls._kinds(n.args[0], tainted)
        else:
            return False
        return any("num" in cls._kinds(o, tainted) for o in operands)

    @classmethod
    def _scan_tree(cls, tree, rel):
        hits, n_reads = [], 0

        def visit(scope, qual):
            nonlocal n_reads
            tainted = cls._tainted(scope)
            for n in cls._scope_nodes(scope):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(n, f"{qual}.{n.name}" if qual else n.name)
                    continue
                if cls._key_of(n) is not None:
                    n_reads += 1
                if cls._is_hit(n, tainted):
                    hits.append((rel, qual or "<module>", n.lineno))

        visit(tree, "")
        return hits, n_reads

    @classmethod
    def _scan(cls, root=None):
        """返回 ({(相对路径, 函数限定名): [行号…]}, 扫到的文件集合, {相对路径: 键读次数})。"""
        root = Path(root) if root is not None else REPO_ROOT
        found, scanned, reads = {}, set(), {}
        for py in own_python_files(root)[0]:
            rel = py.relative_to(root)
            if rel.parts[0] in cls._SKIP_TOP:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            relp = rel.as_posix()
            scanned.add(relp)
            hits, n_reads = cls._scan_tree(tree, relp)
            if n_reads:
                reads[relp] = n_reads
            for f, q, line in hits:
                found.setdefault((f, q), []).append(line)
        return {k: sorted(set(v)) for k, v in found.items()}, scanned, reads

    def test_enumeration_actually_found_something(self):
        """先自证探针有效：扫到了生产文件，且匹配器在生产代码里**认得出**键读
        （bear_bee 仍读 `_od.get("gex")` 进 options_data；oracle_bee 仍读 `result.get("gamma_exposure")`；
        generate_ml_report 读 `gamma_squeeze_risk`）。
        变红的变异：把键集合写错、或把枚举换成空列表 —— 下一条会恒真地绿。"""
        _found, scanned, reads = self._scan()
        assert len(scanned) > 50, f"只扫到 {len(scanned)} 个生产文件 —— 枚举坏了"
        for f in ("swarm_agents/bear_bee.py", "swarm_agents/oracle_bee.py", "options_analyzer.py",
                  "dashboard_renderer.py", "generate_ml_report.py", "swarm_agents/rival_bee.py",
                  "swarm_agents/guard_bee.py"):
            assert f in scanned, f
        for f in ("swarm_agents/bear_bee.py", "swarm_agents/oracle_bee.py", "generate_ml_report.py"):
            assert reads.get(f, 0) > 0, (f, reads)

    def test_comparisons_match_the_allow_list(self):
        """变红的变异：在 BearBee 写回 `if gex is not None and gex < 0: options_bear = max(options_bear, 5.0)`
        （v0.45.348 的原文）；在任一名单外的生产文件里写 `if d.get("gex") < 0: score += 1`；以及复核 D2 的
        a / c / d：OracleBee `if result.get("gamma_squeeze_risk") == "low": options_score += 1`（或 `== "high"`
        ⇒ −1）、RivalBee `_iv_raw, _pc_raw, _gx = …, _od.get("gex")` 后 `_gx < 0` 缩放 ML 特征、
        GuardBee `lambda e: (e.get("gex") or 0) < 0`。"""
        found, _scanned, _r = self._scan()
        extra = {k: v for k, v in found.items() if k not in self.ALLOWED}
        stale = sorted(set(self.ALLOWED) - set(found))
        assert not extra, (
            f"名单外的生产代码拿 GEX 做了判断（大小比较 / 算术 / 排序 / 分档判等 / 查表）：{extra}。"
            "v0.45.349 起 OracleBee 主链 gamma_exposure（及由它分档的 gamma_squeeze_risk）不进任何规则分"
            "（理由见 options_analyzer.generate_options_score 的 GEX 注释）。"
            "是展示就写明用途加进 ALLOWED；要进分先过前瞻检验并登记世代边界。")
        assert not stale, f"名单里的函数已不再这样读 GEX：{stale} —— 删掉那一项"

    @staticmethod
    def _plant(root):
        """带病灶的树：名单外违规（每种形状一处，含复核 D2 的四种）+ 名单内两处 + 应被排除的「同形但不算」。"""
        root.mkdir(parents=True, exist_ok=True)
        (root / "scoring.py").write_text(                   # 违规：直接键读进比较（任务点名的形状）
            "def f(d, score):\n"
            "    if d.get(\"gex\") < 0:\n"
            "        score += 1\n"
            "    return score\n", encoding="utf-8")
        (root / "renamed.py").write_text(                   # 违规：一层污点（v0.45.348 Bear 的形状）
            "def g(d, s):\n"
            "    g = d[\"gamma_exposure\"]\n"
            "    if g is not None and g < -0.001:\n"
            "        s = s + 1.0\n"
            "    return s\n", encoding="utf-8")
        (root / "arith.py").write_text(                     # 违规：算术
            "def h(d, base):\n"
            "    return base + d['gex'] * 0.1\n", encoding="utf-8")
        (root / "squeeze.py").write_text(                   # 违规：GEX 分档判等 / 查表（复核 D2a）
            "def oracle_like(result, options_score):\n"
            "    if result.get(\"gamma_squeeze_risk\") == \"low\":\n"
            "        options_score += 1\n"
            "    return options_score\n"
            "\n\n"
            "def oracle_like_ne(result, s):\n"
            "    risk = result[\"gamma_squeeze_risk\"]\n"
            "    if risk != \"high\" and risk not in (\"medium\",):\n"
            "        s -= 1\n"
            "    return s\n"
            "\n\n"
            "def lookup(result, s):\n"
            "    return s + {\"low\": 1.0}.get(result.get(\"gamma_squeeze_risk\"), 0.0)\n"
            "\n\n"
            "def lookup_sub(result, s, bonus):\n"
            "    return s + bonus[result[\"gamma_squeeze_risk\"]]\n", encoding="utf-8")
        (root / "tuple.py").write_text(                     # 违规：元组 / 星号解包污点（复核 D2c）
            "def rival_like(od, pc):\n"
            "    iv_raw, pc_raw, gx = od.get(\"iv_rank\"), od.get(\"pc_ratio\"), od.get(\"gex\")\n"
            "    if pc_raw is not None and pc_raw > 1.0 and iv_raw != 50:\n"   # 不算：逐位配对，这两个没被污染
            "        pc = pc_raw\n"
            "    if isinstance(gx, (int, float)) and gx < 0:\n"
            "        pc = pc * 1.1\n"
            "    return pc\n"
            "\n\n"
            "def starred(od, s):\n"
            "    first, *rest = od.get(\"gex\"), 1, 2\n"
            "    return s - first\n", encoding="utf-8")
        (root / "loops.py").write_text(                     # 违规：for / 推导式目标、lambda 体（D2d）、排序
            "def loop_like(rows, s):\n"
            "    for g in [r.get(\"gex\") for r in rows]:\n"
            "        if g < 0:\n"
            "            s += 1\n"
            "    return s\n"
            "\n\n"
            "def comp_like(rows):\n"
            "    return sum(1 for g in (r.get(\"gex\") for r in rows) if g < 0)\n"
            "\n\n"
            "def guard_like(entries):\n"
            "    return list(filter(lambda e: (e.get(\"gex\") or 0) < 0, entries))\n"
            "\n\n"
            "def rank_like(rows):\n"
            "    return sorted(rows, key=lambda r: r[\"gamma_exposure\"])[:3]\n", encoding="utf-8")
        # 名单内：旧快照修正（数值键 + 方法限定名）。v0.45.362 前这里种的是 dashboard_renderer::_detail，
        # 那一项随网站改读 gex_state 从名单删了。
        (root / "options_analyzer.py").write_text(
            "class OptionsAgent:\n"
            "    def _drop_legacy_gex_signal(self, cached):\n"
            "        g = cached.get('gamma_exposure')\n"
            "        return g is not None and g < -0.001\n",
            encoding="utf-8")
        (root / "generate_deep_v2.py").write_text(          # 名单内：分档选文案
            "def _build_options_narrative(ctx):\n"
            "    squeeze = ctx.get('gamma_squeeze_risk', '')\n"
            "    return '高' if squeeze == 'high' else '中' if squeeze == 'medium' else '低'\n",
            encoding="utf-8")
        (root / "display.py").write_text(                   # 不算：f-string / is None / 数值 == / 分档拼字符串
            "def show(d, html):\n"
            "    html += f\"<b>{d['gex']:+.1f}</b>\"\n"
            "    if d.get('gex') is None or d.get('gamma_exposure') == 0:\n"
            "        return html\n"
            "    risk = d.get('gamma_squeeze_risk')\n"
            "    html += '<td>' + str(risk).upper() + f\"{d['gamma_squeeze_risk']}\" + '</td>'\n"
            "    if risk is None:\n"
            "        return html\n"
            "    return html\n", encoding="utf-8")
        for sub in ("tests", "experiments"):
            (root / sub).mkdir()
            (root / sub / "x.py").write_text("def f(d, s):\n    if d.get('gex') < 0:\n        s += 1\n    return s\n",
                                             encoding="utf-8")
        nested = root / ".claude" / "worktrees" / "stale" / "swarm_agents"
        nested.mkdir(parents=True)
        (nested / "bear_bee.py").write_text(
            "def f(_od, ob):\n    gex = _od.get('gex')\n    if gex is not None and gex < 0:\n"
            "        ob = max(ob, 5.0)\n    return ob\n", encoding="utf-8")

    #: 病灶树的期望命中（行号逐个对：多报少报都红）
    _PLANTED_HITS = {
        ("scoring.py", "f"): [2], ("renamed.py", "g"): [3], ("arith.py", "h"): [2],
        ("squeeze.py", "oracle_like"): [2], ("squeeze.py", "oracle_like_ne"): [9],
        ("squeeze.py", "lookup"): [15], ("squeeze.py", "lookup_sub"): [19],
        ("tuple.py", "rival_like"): [5], ("tuple.py", "starred"): [12],
        ("loops.py", "loop_like"): [3], ("loops.py", "comp_like"): [9],
        ("loops.py", "guard_like"): [13], ("loops.py", "rank_like"): [17],
        ("options_analyzer.py", "OptionsAgent._drop_legacy_gex_signal"): [4],
        ("generate_deep_v2.py", "_build_options_narrative"): [3],
    }

    def test_pathology_is_caught_and_exclusions_hold(self, tmp_path):
        """每种违规形状都抓到、行号不多不少，名单内两处被认出，f-string / is None / 数值 == / 分档拼字符串 /
        tests / experiments / 嵌套副本都不报。
        变红的变异：删掉 `_tainted`（renamed / tuple / loops 漏报）；类别键不算 `==`（squeeze 漏报）；
        去掉查表两支（lookup / lookup_sub 漏报）；元组不逐位配对（tuple.py 第 3 行误报）；不进 lambda
        （guard_like 漏报 —— rank_like 的 lambda 是 sorted 的实参，照样被那一支抓到）；不认 for / 推导式目标
        （loop_like / comp_like 漏报）；去掉排序函数（rank_like 漏报）；
        不跳过 JoinedStr（display.py 误报）；数值键也算 Eq（display.py 误报）；类别键也算拼接（display.py
        误报）；清空 `_SKIP_TOP`（tests/、experiments/ 误报）。"""
        self._plant(tmp_path)
        found, scanned, _r = self._scan(tmp_path)
        assert found == self._PLANTED_HITS, found
        assert {k for k in found if k not in self.ALLOWED} == set(self._PLANTED_HITS) - {
            ("options_analyzer.py", "OptionsAgent._drop_legacy_gex_signal"),
            ("generate_deep_v2.py", "_build_options_narrative")}
        assert "display.py" in scanned
        assert not any(s.startswith((".claude", "tests", "experiments")) for s in scanned), scanned

    def test_pathology_fixture_actually_bites(self, tmp_path):
        """反向自证：被排除的几处**确实**长着同形病灶 —— 否则上一条的「不报」恒真。"""
        self._plant(tmp_path)
        texts = {p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
                 for p in tmp_path.rglob("*.py")}
        assert sorted(f for f, t in texts.items() if "< 0" in t) == [
            ".claude/worktrees/stale/swarm_agents/bear_bee.py", "experiments/x.py",
            "loops.py", "scoring.py", "tests/x.py", "tuple.py"]
        # display.py 同时有分档键读、`==`、`+` 拼接：朴素文本匹配会报它
        assert all(tok in texts["display.py"] for tok in ("gamma_squeeze_risk", "== 0", "' + str(risk)"))
