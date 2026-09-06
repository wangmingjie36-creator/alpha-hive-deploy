"""ML 输入特征 direction_encoded 改读蜂群方向（v0.45.139）

撤评级词时发现的第二处 train/serve skew（第一处是 v0.45.135 的 catalyst_quality）：
  · 训练：ml_predictor.build_training_data_from_db 用 {bullish:1, neutral:0, bearish:-1}
  · 服务：generate_ml_report._prepare_ml_input 读 recommendation.rating 再映射
          {STRONG BUY:1, BUY:.5, HOLD:0, AVOID:-1}
两张表不是一回事。评级词撤销后旧路径会静默恒为 0.0——「没发生过」形状。
本版服务端改读 swarm_direction、与训练同一张表；方向不可得时进 _ml_input_missing。
全文件不出网。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_predictor as MP
from generate_ml_report import MLEnhancedReportGenerator as G

_METRICS = {"sources": {"yahoo_finance": {"current_price": 100.0}}}


@pytest.fixture
def g():
    return G.__new__(G)   # 不跑 __init__：它会建 ML 服务与线程池


@pytest.mark.parametrize("d,enc", [("bullish", 1.0), ("bearish", -1.0), ("neutral", 0.0)])
def test_direction_encoded_reads_swarm_direction(g, d, enc):
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_direction=d)
    assert td.direction_encoded == enc
    assert "direction" not in g._ml_input_missing


def test_unknown_direction_is_flagged_missing_not_faked(g):
    td = g._prepare_ml_input("X", _METRICS, {}, swarm_direction=None)
    assert td.direction_encoded == 0.0
    assert "direction" in g._ml_input_missing, "方向不可得却没记进缺失表 —— 与「中性」同形"


def test_rating_no_longer_feeds_direction(g):
    """成对：塞一个「STRONG BUY」评级进 analysis，不许再影响 direction_encoded"""
    an = {"recommendation": {"rating": "STRONG BUY"}}
    assert g._prepare_ml_input("X", _METRICS, an, swarm_direction="bearish").direction_encoded == -1.0


def test_serve_map_matches_train_map(g):
    """服务端三个取值必须与训练端 direction_map 逐一相等——否则就是 train/serve skew"""
    src = open(MP.__file__, encoding="utf-8").read()
    assert 'direction_map = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}' in src, \
        "训练端映射表变了，服务端要同步"
    got = {d: g._prepare_ml_input("X", _METRICS, {}, swarm_direction=d).direction_encoded
           for d in ("bullish", "neutral", "bearish")}
    assert got == {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
