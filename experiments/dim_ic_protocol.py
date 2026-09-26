#!/usr/bin/env python3
"""
🐝 Alpha Hive — 维度 IC 证据协议 · 登记常量（P1，v0.45.320；修订 1，v0.45.330）
=================================================================================
规则全文在 `experiments/dim_ic_preregistration.md`，本文件是同一份登记的**机器可读**写法。
两处必须一致，`tests/test_dim_ic_protocol.py` 钉住。

⚠️ 这里只放常量，不放任何计算——执行器是 P2（证据函数），照这些常量实现。
改动任何一个值 = 改动预注册 ⇒ 必须在 CHANGELOG 标「事后」并写理由（协议 §11），并在 `AMENDMENTS` 追加一条。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

#: 登记日（本协议首次提交进 git 的日子）
REGISTERED_ON = "2026-09-23"

#: 修订记录（协议 §13）：(日期, 性质, 摘要)。只许追加
AMENDMENTS: Tuple[Tuple[str, str, str], ...] = (
    ("2026-09-23", "事后·窗口开始前、零数据",
     "修订 1：窗口推迟到 2026-10-12；H1 对象改为冻结版 Buzz 评分 v1（buzz_v1）"),
)

#: 样本窗口起点（修订 1：2026-09-28 → 2026-10-12，2026-W42 周一）。之前的数据一律不进检验（协议 §1、§3、§13）
FORWARD_START = "2026-10-12"

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

#: H1 的输入层（修订 1，协议 §13.2）：冻结评分器读进来、自己挡不住的部分——7 个通道、通道读的行情量、F&G 观测。
#: 边界触及其一 ⇒ H1 截断
H1_INPUT_SIGNALS: Tuple[str, ...] = (
    "buzz.comp.momentum_signal", "buzz.comp.volume_signal", "buzz.comp.volatility_signal",
    "buzz.comp.reddit_signal", "buzz.comp.news_signal", "buzz.comp.yahoo_signal",
    "buzz.comp.fear_greed_signal",
    "price.momentum_5d", "price.volume_ratio", "price.volatility_20d", "market.fear_greed",
)

#: H1 的冻结层（修订 1，协议 §13.2）：从通道值到 `score` 的计算。`sentiment.pct` = int(合成值)，也在这一层。
#: 边界只触及这些 ⇒ 冻结评分器**实现且自证通过之后**吸收、不截断；此前照截断（保守，§13.4）
H1_FROZEN_LAYER_SIGNALS: Tuple[str, ...] = ("agent.BuzzBeeWhisper.score", "sentiment.pct")

#: 冻结层未被吸收时 H1 读的生产维度：截断点之前 buzz_v1 ≡ 锚点之后的生产分（定义如此，§13.2）
H1_PRODUCTION_DIM = "sentiment"

#: buzz_v1 的定义锚点 = 阶段 1 修复在 `ic_rerun_readiness._COHORT_HISTORY` 里的 version。None = 尚未登记。
#: 到 FORWARD_START 仍未登记、或该边界日期不早于 FORWARD_START ⇒ H1 回退到原登记对象，窗口不再推迟（§13.4）
H1_ANCHOR_VERSION: Optional[str] = None

#: 固定序列，顺序即检验顺序（协议 §2）。
#: 每项 = (编号, 对象, 用于截断判定的归档信号——`signal_archive.generation_boundaries()` 的输入)
HYPOTHESES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("H1", "buzz_v1", H1_INPUT_SIGNALS + H1_FROZEN_LAYER_SIGNALS),
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
