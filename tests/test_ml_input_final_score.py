"""ML 输入特征 final_score 改读蜂群综合分（v0.45.141）

v0.45.135（catalyst_quality）、v0.45.139（direction_encoded）之后同一物种的第三处：
  · 训练：ml_predictor.build_training_data_from_db 取 predictions.final_score，
          该列由 backtester.save_predictions 从 swarm_results[t]["final_score"] 落库
  · 服务：generate_ml_report._prepare_ml_input 读 recommendation.score ——
          而 advanced_analyzer._generate_recommendation **从未**返回过 score 键
          （803 份生产 analysis-*-ml-*.json 里 0 次；自 v0.45.50 有缺失表起
          120/120 份都记着 "final_score"）
于是服务端恒为字面量 5.0，「永远缺失」与「真缺失」在输出里同形。
本版服务端改读 swarm_final_score；不可得时值为 None 并进 _ml_input_missing。
全文件不出网（端到端那条的落库取价走 swarm 快照价，不碰 yfinance 兜底）。
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


@pytest.fixture
def g():
    return G.__new__(G)   # 不跑 __init__：它会建 ML 服务与线程池


def _fn_tree(func):
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


# ── 成对断言之一：合法值原样进特征、不上缺失表 ─────────────────────────

@pytest.mark.parametrize("v", [7.3, 0.0, 10.0, 5.0, 3], ids=["7.3", "0.0", "10.0", "5.0", "int3"])
def test_final_score_reads_swarm_value(g, v):
    """含恰好 5.0（与旧兜底同值）与 int——合法值不得被当成缺失。"""
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_final_score=v)
    assert td.final_score == float(v)
    assert isinstance(td.final_score, float)
    assert "final_score" not in g._ml_input_missing


# ── 成对断言之二：不可得 ⇒ None + 上缺失表，不挑兜底值 ─────────────────

@pytest.mark.parametrize("bad", [None, float("nan"), True, "7.3"],
                         ids=["none", "nan", "bool", "str"])
def test_unavailable_is_flagged_missing_not_faked(g, bad):
    """喂 5.0 会与「蜂群正好给了 5.0」同形。bool 是 int 子类（True→1.0 会一路
    通过 float 比较，v0.45.121 事故）；NaN 对 `is not None` 守卫透明。"""
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_final_score=bad)
    assert td.final_score is None
    assert "final_score" in g._ml_input_missing


def test_recommendation_score_no_longer_feeds_final_score(g):
    """成对：塞一个 recommendation.score 进 analysis，无论蜂群值有没有都不许再影响特征。"""
    an = {"recommendation": {"score": 9.9, "rating": "STRONG BUY"}}
    assert g._prepare_ml_input("X", _METRICS, an, swarm_final_score=3.3).final_score == 3.3
    td = g._prepare_ml_input("X", _METRICS, an, swarm_final_score=None)
    assert td.final_score is None, "蜂群不可得时回头去读评级字典 = 旧 bug 换个门进来"
    assert "final_score" in g._ml_input_missing


# ── 训练/服务同源 ───────────────────────────────────────────────────────

def test_serve_matches_train_end_to_end(tmp_path, g):
    """同一份 swarm_results 经 save_predictions → predictions 表 →
    build_training_data_from_db 得到的 final_score，必须与服务端直接读到的相等。

    走真实落库链而不是比源码：两端各自 float() 一次都是恒等映射，
    「读的是哪一列、那一列从哪来」只有落库链能证明。
    """
    from backtester import Backtester
    db = str(tmp_path / "bt.db")
    swarm = {"XOM": {
        "final_score": 7.3, "direction": "bullish",
        "dimension_scores": {"signal": 6.0, "catalyst": 6.5, "sentiment": 5.5,
                             "odds": 5.0, "risk_adj": 6.0},
        "agent_directions": {"ScoutBeeNova": "bullish"},
        # 有快照价就不走 yfinance 兜底（conftest 的离线闸会拦，这里是主动避开）
        "agent_details": {"ScoutBeeNova": {"details": {"price": 110.0}}},
    }}
    assert Backtester(db_path=db).save_predictions(swarm, date="2026-08-03") == 1
    conn = sqlite3.connect(db)          # 训练只取已验证行
    conn.execute("UPDATE predictions SET checked_t7=1, return_t7=0.02, correct_t7=1")
    conn.commit()
    conn.close()
    rows = MP.build_training_data_from_db(db_path=db, min_samples=1)
    assert len(rows) == 1, "对照集为空则下面的相等断言恒真"
    served = g._prepare_ml_input("XOM", _METRICS, {},
                                 swarm_final_score=swarm["XOM"]["final_score"])
    assert rows[0].final_score == served.final_score == 7.3


# ── 结构守卫 ────────────────────────────────────────────────────────────

def test_prepare_ml_input_never_reads_recommendation_dict():
    """评级字典里没有任何一个 ML 特征该读的量（v0.45.135/139/141 三次同一物种）。
    取 AST 常量与 Name，不取子串——注释里提到旧键名是合法的。"""
    tree = _fn_tree(G._prepare_ml_input)
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "recommendation" not in consts
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "_rec" not in names


def test_enhanced_report_forwards_swarm_final_score():
    """接线：generate_ml_enhanced_report → _prepare_ml_input 必须把
    swarm_final_score 原样传下去。「测被调函数 ≠ 测接线」（v0.45.126）。"""
    tree = _fn_tree(G.generate_ml_enhanced_report)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "_prepare_ml_input"]
    assert len(calls) == 1, "锚点自证：调用点数变了先来改这条"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "swarm_final_score" in kw, "参数收了但没传下去——半接线"
    assert isinstance(kw["swarm_final_score"], ast.Name)
    assert kw["swarm_final_score"].id == "swarm_final_score"
