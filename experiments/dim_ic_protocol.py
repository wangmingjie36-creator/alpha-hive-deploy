#!/usr/bin/env python3
"""
🐝 Alpha Hive — 维度 IC 证据协议 · 登记常量（P1，v0.45.320；修订 1，v0.45.330；修订 4，v0.45.708；修订 5，v0.45.712）
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
    # 修订 2 / 3 当时只写进了文档、没追加到这里（v0.45.708 补登；文档标题与性质原样照抄，测试按位置逐条核对）
    ("2026-10-13", "事后·窗口内、被排除日的结果尚不存在",
     "修订 2：2026-10-12（事后整日重放补算）不进前瞻检验（signal_archive.FORWARD_TEST_EXCLUDED_DAYS）"),
    ("2026-10-22", "事后·窗口内、只加敏感性分析（主分析不变）",
     "修订 3：并列报告「去掉 source_outage 日」的敏感性分析（day_health 定义指纹冻结）"),
    ("2026-11-03", "事后·窗口内、只看过输入不看收益",
     "修订 4：H1 对象改为 buzz_v1 的非价格子合成（buzz_v1_np）；截断集合收窄为三个情绪通道；buzz_v1 降为描述项"),
    ("2026-11-04", "事后·窗口内、只看过输入不看收益",
     "修订 5：H1 的通道自 2026-10-20 起由冻结通道层从原始观测重算（之前沿用存档值、哈希钉住）；"
     "截断集合换成取数层；H2 降为描述项"),
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

#: buzz_v1 的输入层（修订 1，协议 §13.2）：冻结评分器读进来、自己挡不住的部分——7 个通道、通道读的行情量、F&G 观测。
#: 修订 4 起它只是**描述项 buzz_v1** 的截断集合，不再截断 H1
H1_V1_INPUT_SIGNALS: Tuple[str, ...] = (
    "buzz.comp.momentum_signal", "buzz.comp.volume_signal", "buzz.comp.volatility_signal",
    "buzz.comp.reddit_signal", "buzz.comp.news_signal", "buzz.comp.yahoo_signal",
    "buzz.comp.fear_greed_signal",
    "price.momentum_5d", "price.volume_ratio", "price.volatility_20d", "market.fear_greed",
)

#: buzz_v1 的冻结层（修订 1，协议 §13.2）：从通道值到 `score` 的计算。`sentiment.pct` = int(合成值)，也在这一层。
#: 修订 4 起只属描述项 buzz_v1（H1 没有冻结层：子合成是纯线性、直接由归档通道算）
H1_FROZEN_LAYER_SIGNALS: Tuple[str, ...] = ("agent.BuzzBeeWhisper.score", "sentiment.pct")

#: 描述项 buzz_v1 读的生产维度：截断点之前 buzz_v1 ≡ 锚点之后的生产分（定义如此，§13.2）
H1_PRODUCTION_DIM = "sentiment"

#: H1（修订 4，协议 §13.13）= buzz_v1_np：三个非价格通道 × buzz_v1 的冻结权重，**按这个顺序**从左往右加；
#: 无 clamp、无动量 / 背离调整（后两项的输入含价格）。缺一个通道 ⇒ 该行无效
H1_NP_WEIGHTS: Dict[str, float] = {"reddit_signal": 0.25, "news_signal": 0.25, "yahoo_signal": 0.05}

#: H1 三个通道在 `signal_archive` 里的名字（修订 4 起；修订 5 起只用于 `H1_ARCHIVE_START` 之前那段）
H1_CHANNEL_SIGNALS: Tuple[str, ...] = tuple(f"buzz.comp.{c}" for c in H1_NP_WEIGHTS)

#: 修订 5（协议 §13.14）：自这天起 H1 的三个通道由冻结通道层 `buzz_v1_channels` 从原始观测档案重算
#: （`observation_links` → 档案 payload），不读生产写的通道值；观测不全的（日，标的）从 H1 排除。
#: 这天之前（窗口内 10-13 至 10-19，档案尚未开始攒）沿用 `signal_archive` 的存档值，由下面的哈希钉住
H1_ARCHIVE_START = "2026-10-20"

#: `FORWARD_START` ≤ date < `H1_ARCHIVE_START`（不含协议排除日）的 `H1_CHANNEL_SIGNALS` 存档值的 sha256：
#: 行 = [date, ticker, signal, value]，按 (date, ticker, signal) 排序，`json.dumps(rows, separators=(",", ":"),
#: ensure_ascii=False)`。登记时 5 个扫描日 × 30 只 × 3 个通道 = 450 个值。对不上 ⇒ 执行器判无法判定
H1_PRE_ARCHIVE_ROWS = 450
H1_PRE_ARCHIVE_SHA256 = "60df3c1f7ee096a9d345ffe19548bfb51455081df1a7545ad16edec2dca42cee"

#: H1 的截断集合（修订 5，§13.14）：取数层的三个伪节点（`signal_archive.UNARCHIVED_NODES`）。
#: 改「取哪份原始观测 / 怎么链接」的边界点名它们 ⇒ H1 截断；只改解释原始观测的生产代码不截断 H1（冻结层不受影响）。
#: 冻结通道层自己改动同样经指纹单元 `h1.frozen` 挡住（`signal_code_fingerprints`）
H1_INPUT_SIGNALS: Tuple[str, ...] = ("obs.reddit", "obs.news", "obs.yahoo")

#: 描述项 buzz_v1（修订 4）：每个检视点照报 IC / CI / 朴素 p，不进 Holm 族、不触发任何动作
H1_V1_DESCRIPTIVE = ("buzz_v1", H1_V1_INPUT_SIGNALS + H1_FROZEN_LAYER_SIGNALS)

#: buzz_v1 的定义锚点 = 阶段 1 修复在 `ic_rerun_readiness._COHORT_HISTORY` 里的 version。None = 尚未登记。
#: 到 FORWARD_START 仍未登记、或该边界日期不早于 FORWARD_START ⇒ H1 回退到原登记对象，窗口不再推迟（§13.4）
#: v0.45.340 填入（修订 1 预留的填空，事前实现对齐）：Buzz 情绪动量改按扫描日回看归档 + 通道全精度入档。
#: 修订 4 起它同样是 buzz_v1_np 的前提：通道自这一版起全精度入档
H1_ANCHOR_VERSION: Optional[str] = "v0.45.340"

#: 确认性假设（协议 §2）。修订 5 起只剩 H1（原 H2 降为描述项 `H2_DESCRIPTIVE`）。
#: 每项 = (编号, 对象, 用于截断判定的信号——`signal_archive.generation_boundaries()` 的输入)
HYPOTHESES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("H1", "buzz_v1_np", H1_INPUT_SIGNALS),
)

#: 描述项 weighted_composite（修订 5：原 H2）：协议结束时报 IC / CI / 朴素 p，不进 Holm 族、不触发任何动作。
#: 照旧在三蜂分数的边界或 config 权重偏离冻结值之日截断——只是描述项自己的说明，不影响 H1
H2_DESCRIPTIVE = ("weighted_composite", ("agent.ChronosBeeHorizon.score",
                                         "agent.BuzzBeeWhisper.score",
                                         "agent.OracleBeeEcho.score"))

#: weighted_composite 的权重，冻结为登记时的 `config.EVALUATION_WEIGHTS`。config 此后偏离这组值 ⇒ 该描述项在偏离日截断（协议 §6）
FROZEN_WEIGHTS: Dict[str, float] = {
    "signal": 0.0, "catalyst": 0.332, "sentiment": 0.325, "odds": 0.343, "risk_adj": 0.0,
}

#: 描述项：只报告、不触发动作；族 = 4，Holm 校正（协议 §2）
DESCRIPTIVE_DIMS: Tuple[str, ...] = ("signal", "catalyst", "odds", "risk_adj")
DESCRIPTIVE_CORRECTION = "holm"

#: 维度 ↔ 产生它的蜂（各蜂 `analyze()` 里 `dimension=` 的声明）。描述项的截断信号由此而来
DIM_TO_AGENT: Dict[str, str] = {
    "signal": "ScoutBeeNova", "catalyst": "ChronosBeeHorizon", "sentiment": "BuzzBeeWhisper",
    "odds": "OracleBeeEcho", "risk_adj": "GuardBeeSentinel",
}
