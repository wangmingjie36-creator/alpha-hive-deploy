#!/usr/bin/env python3
"""
🐝 Alpha Hive — 维度 IC 证据协议 · 登记常量（P1，v0.45.320）
===============================================================
规则全文在 `experiments/dim_ic_preregistration.md`，本文件是同一份登记的**机器可读**写法。
两处必须一致，`tests/test_dim_ic_protocol.py` 钉住。

⚠️ 这里只放常量，不放任何计算——执行器是 P2（证据函数），照这些常量实现。
改动任何一个值 = 改动预注册 ⇒ 必须在 CHANGELOG 标「事后」并写理由（协议 §11）。
"""
from __future__ import annotations

from typing import Dict, Tuple

#: 登记日（本协议首次提交进 git 的日子）
REGISTERED_ON = "2026-09-23"

#: 样本窗口起点：登记后下一个 ISO 周的周一（2026-W40）。之前的数据一律不进检验（协议 §1、§3）
FORWARD_START = "2026-09-28"

#: (所需 H1 合格周数, 两侧 α, 标签)。α 跨检视点按 Bonferroni 拆分，合计 0.05（协议 §5）
LOOKS: Tuple[Tuple[int, float, str], ...] = ((26, 0.01, "中检"), (52, 0.04, "终检"))

TWO_SIDED = True

#: 某日某假设的有效标的数下限（协议 §3）
MIN_WIDTH = 20

#: Newey-West（Bartlett 核）滞后阶数（协议 §4）
NW_LAG = 1

#: 结果变量。`price_t7` 是带 SL/TP 截断的离场价、`return_t7` 是路径依赖收益，都禁用（协议 §3）
OUTCOME_COLUMN = "close_t7"
FORBIDDEN_OUTCOME_COLUMNS: Tuple[str, ...] = ("price_t7", "return_t7")

#: 固定序列，顺序即检验顺序（协议 §2）。
#: 每项 = (编号, 对象, 用于截断判定的归档信号——`signal_archive.generation_boundaries()` 的输入)
HYPOTHESES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("H1", "sentiment", ("agent.BuzzBeeWhisper.score",)),
    ("H2", "weighted_composite", ("agent.ChronosBeeHorizon.score",
                                  "agent.BuzzBeeWhisper.score",
                                  "agent.OracleBeeEcho.score")),
)

#: H2 的权重，冻结为登记时的 `config.EVALUATION_WEIGHTS`。config 此后偏离这组值 ⇒ H2 在偏离日截断（协议 §6）
FROZEN_WEIGHTS: Dict[str, float] = {
    "signal": 0.0, "catalyst": 0.332, "sentiment": 0.325, "odds": 0.343, "risk_adj": 0.0,
}

#: 描述项：只报告、不触发动作；族 = 4，Holm 校正（协议 §2）
DESCRIPTIVE_DIMS: Tuple[str, ...] = ("signal", "catalyst", "odds", "risk_adj")
DESCRIPTIVE_CORRECTION = "holm"

#: 维度 ↔ 产生它的蜂（各蜂 `analyze()` 里 `dimension=` 的声明）。H1/H2 的截断信号由此而来
DIM_TO_AGENT: Dict[str, str] = {
    "signal": "ScoutBeeNova", "catalyst": "ChronosBeeHorizon", "sentiment": "BuzzBeeWhisper",
    "odds": "OracleBeeEcho", "risk_adj": "GuardBeeSentinel",
}
