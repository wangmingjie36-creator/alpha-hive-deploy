"""ML 输入最后两个常数特征槽接蜂群真值（v0.45.146）

12 维里 v0.45.135/137/139/140/141 已逐批接线，剩这两个：

  · `crowding_score`：旧读 `metrics.get("crowding_score", _fallback_crowding)`。
    生产 `realtime_metrics` 里 `crowding_score` 与 `short_interest_ratio`
    **两个键都不存在** ⇒ 两级兜底全落到字面量 —— 实测 803 份生产
    analysis-*-ml-*.json 中 777 份恒 50.0、25 份 500.0（clamp 前的越界残留）、1 份 45.0。
  · `agent_agreement`：旧为字面量 `0.5,  # 预测时无蜂群上下文`。那条注释
    自 v0.45.140 起已不成立——同一份 `swarm_data[ticker]` 就在调用点手边。

两处**都接训练端同一个量**，不接"名字对得上"的那个：
  训练 `build_training_data_from_db` 写的是 `crowding_score=_sig * 10`
  （信号维度分×10，**不是**拥挤度）。ScoutBeeNova 的真拥挤度与它
  Spearman ρ = −0.46（近似反号），接那个比留常数更糟——同 v0.45.137 的判据。

全文件不出网。
"""

import ast
import inspect
import os
import sqlite3
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_predictor as MP
from generate_ml_report import MLEnhancedReportGenerator as G

_METRICS = {"sources": {"yahoo_finance": {"current_price": 100.0}}}


def _dims(**kw):
    """五维齐全的维度分，按需覆盖单维。"""
    d = {"signal": 5.0, "catalyst": 5.0, "sentiment": 5.0, "odds": 5.0, "risk_adj": 5.0}
    d.update(kw)
    return d


@pytest.fixture
def g():
    return G.__new__(G)   # 不跑 __init__：它会建 ML 服务与线程池


def _fn_tree(func):
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


# ═══════════════════════════════════════════════════════════════════════
#  槽 1：crowding_score = 蜂群 signal 维分 × 10
# ═══════════════════════════════════════════════════════════════════════

# ── 成对断言之一：真值可得 ⇒ 用真值、不上缺失表 ───────────────────────

@pytest.mark.parametrize("sig,want", [
    (6.0, 60.0), (1.88, 18.8), (9.64, 96.4),   # 生产实测极值
    (5.0, 50.0),                                # 恰好与旧兜底同值，不得被当缺失
    (0.0, 0.0),                                 # 合法 0：`or` 型兜底会吃掉它
    (7, 70.0),                                  # int
], ids=["6.0", "min1.88", "max9.64", "same-as-old-50", "zero", "int7"])
def test_crowding_reads_signal_times_ten(g, sig, want):
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_dimension_scores=_dims(signal=sig))
    assert td.crowding_score == pytest.approx(want)
    assert isinstance(td.crowding_score, float)
    assert "crowding_score" not in g._ml_input_missing


# ── 成对断言之二：不可得 ⇒ None + 上缺失表，不挑兜底值 ─────────────────

@pytest.mark.parametrize("bad", [None, float("nan"), True, False, "6.0"],
                         ids=["none", "nan", "true", "false", "str"])
def test_crowding_unavailable_is_flagged_not_faked(g, bad):
    """`bool` 是 `int` 子类：True→10.0、False→0.0 都会一路通过 float 比较，
    而 0.0 在本量表是最低信号（v0.45.121 同款事故）。NaN 对 `is not None` 透明。"""
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_dimension_scores=_dims(signal=bad))
    assert td.crowding_score is None
    assert "crowding_score" in g._ml_input_missing


def test_crowding_missing_when_no_swarm_at_all(g):
    """整包蜂群缺失（生产 803 份里 57 份如此）也必须记账，不是悄悄给 50.0。"""
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_dimension_scores=None)
    assert td.crowding_score is None
    assert "crowding_score" in g._ml_input_missing


# ── 反陷阱：旧的两条兜底来源都不许再影响这个特征 ───────────────────────

def test_realtime_metrics_no_longer_feeds_crowding(g):
    """成对：`metrics` 里塞旧的两个键，有蜂群时不许盖过真值、
    无蜂群时也不许被当成真值复活（否则旧 bug 换个门进来）。"""
    m = {"crowding_score": 88.0,
         "sources": {"yahoo_finance": {"current_price": 100.0,
                                       "short_interest_ratio": 9.9}}}
    assert g._prepare_ml_input(
        "X", m, {}, swarm_dimension_scores=_dims(signal=6.0)).crowding_score == 60.0
    td = g._prepare_ml_input("X", m, {}, swarm_dimension_scores=None)
    assert td.crowding_score is None
    assert "crowding_score" in g._ml_input_missing


def test_crowding_is_not_clamped(g):
    """训练端 `_sig * 10` **不做** clamp。这里加 clamp 会在尾部制造新的口径差。

    实测生产 signal ∈ [1.88, 9.64] ⇒ clamp 从未生效过，所以移除它对真实数据
    零影响；这条断言守的是「别好心加回来」。
    """
    assert g._prepare_ml_input(
        "X", _METRICS, {}, swarm_dimension_scores=_dims(signal=12.5)).crowding_score == 125.0


# ═══════════════════════════════════════════════════════════════════════
#  槽 2：agent_agreement = 与蜂群方向一致的蜂占比
# ═══════════════════════════════════════════════════════════════════════

_AD8 = {"ScoutBeeNova": "bullish", "OracleBeeEcho": "bullish",
        "BuzzBeeWhisper": "bullish", "ChronosBeeHorizon": "neutral",
        "RivalBeeVanguard": "neutral", "GuardBeeSentinel": "bearish",
        "BearBeeContrarian": "bearish", "CodeExecutorAgent": "neutral"}


# ── 成对断言之一：真值可得 ⇒ 用真值、不上缺失表 ───────────────────────

@pytest.mark.parametrize("direction,want", [
    ("bullish", 3 / 8), ("neutral", 3 / 8), ("bearish", 2 / 8),
], ids=["bullish", "neutral", "bearish"])
def test_agreement_counts_agents_matching_direction(g, direction, want):
    """公式照抄训练端：一致蜂数 / 总蜂数。方向不同 ⇒ 同一份 agent_directions
    必须给出不同的值（否则它只是在数蜂，没在测共识）。"""
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_dimension_scores=_dims(),
                             swarm_direction=direction, swarm_agent_directions=_AD8)
    assert td.agent_agreement == pytest.approx(want)
    assert "agent_agreement" not in g._ml_input_missing


@pytest.mark.parametrize("ad,direction,want", [
    ({"A": "bullish"}, "bullish", 1.0),                       # 全体一致
    ({"A": "bullish", "B": "bearish"}, "neutral", 0.0),       # 合法 0：无人同向
    ({"A": "bullish", "B": "bearish"}, "bullish", 0.5),       # 恰好与旧字面量同值
], ids=["unanimous", "legit-zero", "same-as-old-0.5"])
def test_agreement_edge_values_are_real_values(g, ad, direction, want):
    """0.0 与 0.5 都是**合法观测**，不得被当成缺失——`or`/`if not x` 型
    兜底会同时吃掉这两个。"""
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_dimension_scores=_dims(),
                             swarm_direction=direction, swarm_agent_directions=ad)
    assert td.agent_agreement == pytest.approx(want)
    assert "agent_agreement" not in g._ml_input_missing


# ── 成对断言之二：不可得 ⇒ None + 上缺失表 ─────────────────────────────

@pytest.mark.parametrize("ad", [None, {}, [], "bullish", 0.5],
                         ids=["none", "empty-dict", "list", "str", "float"])
def test_agreement_unavailable_is_flagged_not_faked(g, ad):
    """喂 0.5 会与「八蜂正好四比四」同形——那在训练分布里占 37.0%，
    是最不该被伪造的那个值。"""
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_dimension_scores=_dims(),
                             swarm_direction="bullish", swarm_agent_directions=ad)
    assert td.agent_agreement is None
    assert "agent_agreement" in g._ml_input_missing


@pytest.mark.parametrize("direction", [None, "", "BULLISH", "Long", 1.0],
                         ids=["none", "empty", "upper", "legacy-Long", "float"])
def test_agreement_requires_a_known_direction(g, direction):
    """共识度是**相对于方向定义**的：没有已知方向就没有「与之一致」可言。

    尤其不能让未知方向静默算出 0.0 —— 那与「真的没有蜂同向」同形。
    大小写/旧词表（v0.45.85 的 Long/Short 遗留）都必须落到缺失侧。
    """
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_dimension_scores=_dims(),
                             swarm_direction=direction, swarm_agent_directions=_AD8)
    assert td.agent_agreement is None
    assert "agent_agreement" in g._ml_input_missing


# ═══════════════════════════════════════════════════════════════════════
#  训练 / 服务同源（走真实落库链，不比源码）
# ═══════════════════════════════════════════════════════════════════════

def test_serve_matches_train_end_to_end(tmp_path, g):
    """同一份 swarm_results 经 save_predictions → predictions 表 →
    build_training_data_from_db，两个槽都必须与服务端直接算出的相等。

    比源码没用：两端各写一遍 `*10` 与 `major/len` 都是「看着一样」，
    只有落库链能证明「读的是哪一列、那一列从哪来」。
    """
    from backtester import Backtester
    db = str(tmp_path / "bt.db")
    swarm = {"XOM": {
        "final_score": 7.3, "direction": "bullish",
        "dimension_scores": _dims(signal=6.4, catalyst=6.5, sentiment=5.5,
                                  odds=5.0, risk_adj=6.0),
        "agent_directions": _AD8,
        # 有快照价就不走 yfinance 兜底（conftest 离线闸会拦，这里主动避开）
        "agent_details": {"ScoutBeeNova": {"details": {"price": 110.0}}},
    }}
    assert Backtester(db_path=db).save_predictions(swarm, date="2026-08-03") == 1
    conn = sqlite3.connect(db)          # 训练只取已验证行
    conn.execute("UPDATE predictions SET checked_t7=1, return_t7=0.02, correct_t7=1")
    conn.commit()
    conn.close()
    rows = MP.build_training_data_from_db(db_path=db, min_samples=1)
    assert len(rows) == 1, "对照集为空则下面的相等断言恒真"

    sr = swarm["XOM"]
    served = g._prepare_ml_input(
        "XOM", _METRICS, {},
        swarm_dimension_scores=sr["dimension_scores"],
        swarm_direction=sr["direction"],
        swarm_agent_directions=sr["agent_directions"])

    assert rows[0].crowding_score == served.crowding_score == pytest.approx(64.0)
    assert rows[0].agent_agreement == served.agent_agreement == pytest.approx(3 / 8)


def test_train_side_still_puts_signal_not_crowding_in_that_slot():
    """反陷阱守卫：这个槽装的是 signal×10。哪天训练端改成真拥挤度，
    服务端就必须同步——本条会先红，提醒去看另一端。"""
    src = textwrap.dedent(inspect.getsource(MP.build_training_data_from_db))
    tree = ast.parse(src)
    kws = [k for n in ast.walk(tree) if isinstance(n, ast.Call)
           for k in n.keywords if k.arg == "crowding_score"]
    assert len(kws) == 1, "锚点自证：训练端赋值点数变了先来改这条"
    assign = ast.unparse(kws[0].value).replace(" ", "")
    assert assign == "_sig*10", f"训练端换量了：{assign}"


# ═══════════════════════════════════════════════════════════════════════
#  接线（测被调函数 ≠ 测接线 —— v0.45.126）
# ═══════════════════════════════════════════════════════════════════════

def test_enhanced_report_forwards_swarm_agent_directions():
    tree = _fn_tree(G.generate_ml_enhanced_report)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "_prepare_ml_input"]
    assert len(calls) == 1, "锚点自证：调用点数变了先来改这条"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "swarm_agent_directions" in kw, "参数收了但没传下去——半接线"
    assert isinstance(kw["swarm_agent_directions"], ast.Name)
    assert kw["swarm_agent_directions"].id == "swarm_agent_directions"


def test_both_slots_are_in_the_missing_table_source():
    """两套账目（`input_features_missing` 与 `feature_completeness`）要对得上，
    前提是报告端这张表真的列了这两个名字。取 AST 常量，不取子串——
    注释里提到名字是合法的（v0.45.135 的判据）。"""
    tree = _fn_tree(G._prepare_ml_input)
    assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and any(getattr(t, "attr", "") == "_ml_input_missing" for t in n.targets)]
    assert len(assign) == 1, "锚点自证：缺失表赋值点数变了先来改这条"
    consts = {n.value for n in ast.walk(assign[0])
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert {"crowding_score", "agent_agreement"} <= consts


# ═══════════════════════════════════════════════════════════════════════
#  v0.45.162：两条管道之间那道墙 —— 报告侧拿到的是**引用**
# ═══════════════════════════════════════════════════════════════════════
#
# v0.45.146 起，调用点传的是 `_sr.get("agent_directions")` 与
# `_sr.get("dimension_scores")` —— **同一个 dict 对象**，不是副本。
# 而 `Backtester.save_predictions` 正把这两个对象 `json.dumps` 进
# `predictions.agent_directions` / `predictions.dimension_scores`，
# 后者是 `ic_diagnostics.load_daily_ic` **唯一**读的那一列。
#
# 所以「报告侧的改动不影响 IC 闸」这句话，除了「产物不进 predictions」
# （数据通路）之外，还依赖一条从没被断言过的不变式：**报告侧不得就地改写入参**。
# 一旦哪天有人在 `_prepare_ml_input` 里写了 `dims.setdefault(...)` 之类，
# 墙就破了，而且是**静默**的 —— 报告照出、库照写、IC 照算。

class TestPreparedInputDoesNotMutateSwarmDicts:
    """成对：① 没写回入参　② 但确实读了它们。

    只有①会被**空实现**满足（什么都不干的函数当然不改写入参），
    那样这条守卫就退化成恒真。②把它钉在「读了、算了、只是没写回」上。

    ⚠️ 别再补一条「对象身份没被换掉」（`id(dims)` 调用前后相等）：
    v0.45.162 写过又删了 —— 那是**恒真断言**。被调用方无论怎么写都改不了
    调用方局部名字的绑定，不存在能让它变红的实现。变异实测：给函数里植入
    「换成副本再改写副本」，被另外 11 条既有断言抓到，那条身份断言**全绿**。
    （同 v0.45.71「守卫自己恒真」：写 `assert` 前先问哪个实现能让它失败。）
    """

    @staticmethod
    def _live():
        """生产形状：五维 + 逐蜂方向（3 多 1 空 ⇒ 一致率 0.75）。"""
        return (
            {"signal": 6.4, "catalyst": 5.0, "sentiment": 5.0,
             "odds": 5.0, "risk_adj": 5.0},
            {"ScoutBeeNova": "bullish", "BuzzBeeWhisper": "bullish",
             "OracleBeeEcho": "bullish", "BearBeeContrarian": "bearish"},
        )

    def test_inputs_are_untouched_and_still_actually_read(self, g):
        import json

        dims, adirs = self._live()
        before = (json.dumps(dims, sort_keys=True),
                  json.dumps(adirs, sort_keys=True))

        td = g._prepare_ml_input(
            "X", _METRICS, {},
            swarm_dimension_scores=dims,
            swarm_agent_directions=adirs,
            swarm_direction="bullish",
        )

        # ② 先证它真的读了 —— 否则①对空实现也全绿
        assert td.crowding_score == pytest.approx(64.0), "没读 dimension_scores"
        assert td.agent_agreement == pytest.approx(0.75), "没读 agent_directions"

        # ① 再证它没写回。库里那两列的内容就是这两个对象序列化的结果。
        assert (json.dumps(dims, sort_keys=True),
                json.dumps(adirs, sort_keys=True)) == before, (
            "`_prepare_ml_input` 就地改写了入参 —— 这两个对象会被 "
            "`save_predictions` 写进 `predictions`，`dimension_scores` 更是 "
            "IC 闸唯一读的列 ⇒ 报告侧的改动会渗进 IC 管道，且渗得静默。"
        )
