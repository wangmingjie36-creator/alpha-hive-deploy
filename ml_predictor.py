"""
🐝 Alpha Hive - 机器学习预测系统
使用历史数据训练模型，优化概率计算和涨跌预测
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List
import statistics
from dataclasses import dataclass

_log = logging.getLogger("alpha_hive.ml_predictor")


@dataclass
class TrainingData:
    """训练数据结构"""
    ticker: str
    date: str
    crowding_score: float
    catalyst_quality: str  # A+, A, B+, B, C
    momentum_5d: float  # 5 日动量 (%)
    volatility: float  # 历史波动率
    market_sentiment: float  # -100 到 +100

    # 目标变量
    actual_return_3d: float  # 实际 3 日收益
    actual_return_7d: float  # 实际 7 日收益
    actual_return_30d: float  # 实际 30 日收益
    win_3d: bool  # 3 日是否赚钱
    win_7d: bool  # 7 日是否赚钱
    win_30d: bool  # 30 日是否赚钱

    # === v2 新特征（均有默认值，旧代码无需改动）===
    iv_rank: float = 50.0             # 0-100, IV 百分位
    put_call_ratio: float = 1.0       # Put/Call ratio
    final_score: float = 5.0          # 蜂群综合分 (0-10)
    odds_score: float = 5.0           # 赔率维度分 (0-10)
    risk_adj_score: float = 5.0       # 风险调整分 (0-10)
    agent_agreement: float = 0.5      # Agent 共识度 (0-1)
    direction_encoded: float = 0.0    # bullish=1, neutral=0, bearish=-1


class HistoricalDataBuilder:
    """构建训练数据集"""

    def __init__(self):
        # 收集的历史交易记录
        self.historical_records: List[TrainingData] = [
            # NVDA 记录
            TrainingData(
                ticker="NVDA", date="2023-10-18",
                crowding_score=68.0, catalyst_quality="A",
                momentum_5d=5.2, volatility=4.8, market_sentiment=45,
                actual_return_3d=8.5, actual_return_7d=18.9, actual_return_30d=32.1,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=62.0, put_call_ratio=0.8, final_score=8.2,
                odds_score=7.5, risk_adj_score=6.8, agent_agreement=0.85, direction_encoded=1.0,
            ),
            TrainingData(
                ticker="NVDA", date="2023-04-19",
                crowding_score=72.0, catalyst_quality="A",
                momentum_5d=3.8, volatility=5.1, market_sentiment=35,
                actual_return_3d=12.8, actual_return_7d=22.3, actual_return_30d=18.5,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=55.0, put_call_ratio=0.75, final_score=7.8,
                odds_score=7.0, risk_adj_score=7.2, agent_agreement=0.71, direction_encoded=1.0,
            ),
            TrainingData(
                ticker="NVDA", date="2024-01-24",
                crowding_score=75.0, catalyst_quality="A+",
                momentum_5d=6.5, volatility=6.1, market_sentiment=55,
                actual_return_3d=5.2, actual_return_7d=15.6, actual_return_30d=38.9,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=70.0, put_call_ratio=0.65, final_score=8.8,
                odds_score=8.0, risk_adj_score=7.5, agent_agreement=0.86, direction_encoded=1.0,
            ),
            # VKTX 记录
            TrainingData(
                ticker="VKTX", date="2023-06-15",
                crowding_score=58.0, catalyst_quality="A+",
                momentum_5d=2.1, volatility=12.3, market_sentiment=60,
                actual_return_3d=42.1, actual_return_7d=38.5, actual_return_30d=22.3,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=78.0, put_call_ratio=0.55, final_score=8.5,
                odds_score=7.8, risk_adj_score=5.5, agent_agreement=0.71, direction_encoded=1.0,
            ),
            TrainingData(
                ticker="VKTX", date="2023-11-22",
                crowding_score=42.0, catalyst_quality="A",
                momentum_5d=1.5, volatility=8.9, market_sentiment=40,
                actual_return_3d=8.2, actual_return_7d=12.5, actual_return_30d=15.8,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=65.0, put_call_ratio=0.90, final_score=7.5,
                odds_score=6.5, risk_adj_score=6.0, agent_agreement=0.57, direction_encoded=1.0,
            ),
            # TSLA 记录
            TrainingData(
                ticker="TSLA", date="2024-01-17",
                crowding_score=71.0, catalyst_quality="B+",
                momentum_5d=4.2, volatility=7.8, market_sentiment=30,
                actual_return_3d=12.3, actual_return_7d=18.2, actual_return_30d=12.5,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=58.0, put_call_ratio=1.10, final_score=7.2,
                odds_score=6.0, risk_adj_score=5.8, agent_agreement=0.57, direction_encoded=1.0,
            ),
            # 负例（失败的交易）
            TrainingData(
                ticker="NVDA", date="2023-08-01",
                crowding_score=82.0, catalyst_quality="B",
                momentum_5d=8.5, volatility=7.2, market_sentiment=70,
                actual_return_3d=-2.3, actual_return_7d=1.2, actual_return_30d=-5.8,
                win_3d=False, win_7d=False, win_30d=False,
                iv_rank=85.0, put_call_ratio=1.35, final_score=6.0,
                odds_score=4.5, risk_adj_score=4.0, agent_agreement=0.43, direction_encoded=1.0,
            ),
            TrainingData(
                ticker="VKTX", date="2023-09-15",
                crowding_score=65.0, catalyst_quality="C",
                momentum_5d=-3.2, volatility=11.5, market_sentiment=-20,
                actual_return_3d=-8.5, actual_return_7d=-12.3, actual_return_30d=-18.9,
                win_3d=False, win_7d=False, win_30d=False,
                iv_rank=72.0, put_call_ratio=1.50, final_score=4.8,
                odds_score=3.5, risk_adj_score=3.2, agent_agreement=0.29, direction_encoded=1.0,
            ),
        ]

    def get_training_data(self) -> List[TrainingData]:
        """获取所有训练数据"""
        return self.historical_records

    def add_record(self, record: TrainingData):
        """添加新的交易记录"""
        self.historical_records.append(record)

    def save_to_file(self, filename: str = "training_data.json"):
        """保存训练数据到文件"""
        data = [
            {
                "ticker": r.ticker,
                "date": r.date,
                "crowding_score": r.crowding_score,
                "catalyst_quality": r.catalyst_quality,
                "momentum_5d": r.momentum_5d,
                "volatility": r.volatility,
                "market_sentiment": r.market_sentiment,
                "actual_return_3d": r.actual_return_3d,
                "actual_return_7d": r.actual_return_7d,
                "actual_return_30d": r.actual_return_30d,
                "win_3d": r.win_3d,
                "win_7d": r.win_7d,
                "win_30d": r.win_30d,
                # v2 新特征
                "iv_rank": r.iv_rank,
                "put_call_ratio": r.put_call_ratio,
                "final_score": r.final_score,
                "odds_score": r.odds_score,
                "risk_adj_score": r.risk_adj_score,
                "agent_agreement": r.agent_agreement,
                "direction_encoded": r.direction_encoded,
            }
            for r in self.historical_records
        ]

        with open(filename, "w") as f:
            json.dump(data, f, indent=2)


# ════════════════════════════════════════════════════════════════════════════
# 预期收益与概率的共享原语（v0.44.1 重做）
#
# 修的是什么
# ---------
# 旧公式 `expected_7d = catalyst_bonus + momentum_bonus - crowding_penalty`
# 结构性恒正：`catalyst_bonus` ∈ {5,10,15,20,25}（默认 10）**永不为负**，
# `crowding_penalty = crowding*0.1` **上限只有 10**，于是唯一能为负的项只剩
# 动量 —— 要让"预期收益"转负需要 5 日跌超 5%（催化剂 A+ 时需跌超 25%）。
#
# 实测后果（2026-08-16，1094 条）：
#   · `ml.expected_7d` **96.7% 为正**，中位数 **+8.00%**，p5 **+0.60%**
#   · 同期真实 7 日收益中位数 **−0.21%**、为正占比 **49.1%**
#   · 下游 `RivalBeeVanguard` 因此 **95.2% 看多**（方向直接取 avg_ret 的符号）
#   · 这解释了 `ml.expected_7d/30d` rank-IC = −0.044、0/4 —— 不是 ML 弱，
#     是它**从来不是模型输出**，只是三行手写加法
#
# 更根本的是量纲错了：`catalyst_bonus` 是催化剂**质量等级**（5~25 的"分"），
# 却直接与 `momentum_5d`（**百分点**）相加当预期收益。于是"B 级催化剂"
# = +10 个百分点的 7 日预期收益。
#
# ⚠️ 生产里 `rival_bee.py` 还把特征写死成常量（`catalyst_quality="B+"`、
# `crowding_score=50.0`、`iv_rank=50.0`、`put_call_ratio=1.0`），代入旧公式得
# **`expected_7d = 8.0 + 0.8 × momentum_5d`** —— 一个截距 +8% 的一元线性式。
# 该闭式在 1057 个配对样本上**零反例**。特征硬编码是另一个独立缺陷，
# 见 `swarm_agents/rival_bee.py` 处注释，本次未动。
#
# 怎么修
# -----
# 催化剂质量只调**幅度**不调**方向** —— 它编码"影响多大"，不编码"影响好坏"
# （与 ChronosBee「权重表只编码影响多大」是同族错误）。拥挤度以中性 50 为
# 中心，可正可负。全部项统一为百分点。于是**无信号时预期收益为 0**，
# 而不是 +8%。
# ════════════════════════════════════════════════════════════════════════════

# 催化剂质量 → 无量纲幅度乘数（B+ = 1.0 基准，不改变符号）
_CATALYST_MAGNITUDE = {"A+": 1.5, "A": 1.3, "B+": 1.0, "B": 0.9, "C": 0.7}
_CATALYST_MAGNITUDE_DEFAULT = 1.0

# 催化剂维度分（0~10）→ 等级。阈值沿用项目既有约定，**勿改**：
# 历史 `predictions` 里的 catalyst_quality 都是按这套阈值生成的，改了会让
# 新旧样本不可比。
_CATALYST_GRADE_CUTS = ((8.5, "A+"), (7.5, "A"), (6.5, "B+"), (5.5, "B"))


def catalyst_quality_from_score(score) -> str:
    """把 ChronosBee 的催化剂维度分（0~10）转成 A+/A/B+/B/C 等级。

    v0.44.3 提为模块级单一真相。此前同一套阈值在**至少三处**各写一份嵌套
    `_cat_qual`（`ml_predictor.py` 内、`alpha_hive_daily_report.py`、
    `generate_ml_report.py`），与 `expected_returns` 曾经的三重复制是同一个
    反模式 —— 阈值一改就得记住改三处，漏一处就静默产生两套口径的历史样本。

    分数不可用（None/NaN/非数值）时返回 "B"（对应 magnitude 0.9，接近中性），
    **不返回 "B+"** —— "B+" 是 magnitude 1.0 的基准档，用它做缺失值会让
    "拿不到数据"与"质量正好中等"不可区分。
    """
    try:
        v = float(score)
    except (TypeError, ValueError):
        return "B"
    if v != v:                      # NaN
        return "B"
    for cut, grade in _CATALYST_GRADE_CUTS:
        if v >= cut:
            return grade
    return "C"

# ── 拥挤度：**刻意不进入 expected_returns**（v0.44.2 决定）───────────────
#
# v0.44.1 曾把拥挤度做成双向倾斜项。v0.44.2 用现成的四口径工具
# （`signal_archive.py --analyze`，噪音地板 |IC|=0.076、需 ≥3/4 口径）复核后移除，
# 三条理由：
#
# 1. **方向未确立。** 实测（n=669）：
#      · `crowding.score`（连续）  IC=**+0.1122**, t=+2.48, **仅 1/4 口径** → 不达标
#      · `crowding.adj_factor`（分档）IC=−0.1117, t=−2.81, 3/4 口径，但**⚠️稀疏**
#        （只有 6 个取值，命中 MEMORY 记载的「distinct_ratio<0.25 → rank-IC 失真」陷阱）
#    两者方向一致，且都指向「**高拥挤 → 收益更高**」——与检测器
#    `get_adjustment_factor`（高拥挤打 30% 折）的设计意图**相反**。
#    连续版不达标、达标版有稀疏问题 ⇒ **符号不该由这份证据决定，也不该无视它**。
#    带着一个与现有证据相反的符号上线，是在给预期收益注入可能反向的噪音。
#
# 2. **三重计数。** 拥挤度已经在 `predict_probability` 里是**权重最大的特征**
#    （`crowding: 0.18`），又经 `CrowdingDetector.get_adjustment_factor` 作用于
#    综合分。再加进 expected_returns 是第三次。
#
# 3. **它本来就不是收益预测量。** 拥挤度是容量/风险概念，其**已确立**的用法是
#    乘性折扣（adj_factor），把它折成"多少个百分点的预期收益"需要凭空定标度。
#
# ⚠️ 移除的代价要说清：v0.44.1（含倾斜）的偏差是 +0.19pp，移除后回到 +1.06pp。
# 但那 +0.19 是**巧合**——倾斜的均值恰好抵消了动量带来的正偏，不是因为符号对。
# **宁要 +1.06pp 的可辩护公式，不要 +0.19pp 的不可辩护公式。**
#
# `CROWDING_NEUTRAL` 保留，但用途变了：它是 `rival_bee` 取不到真实拥挤度时喂给
# **`probability`** 的中性回落值（那里 crowding 是权重最大的特征，回落值选错会
# 直接偏斜概率）。取实测中位数而非量表中点 50 —— 实测只有 4.6% 的样本 ≥50，
# 在这个分布里 50 等于"极不拥挤"。
#
# ⚠️ **这是经验常数，会过期。** 由 `tests/test_ml_expected_return.py::
# TestCrowdingCalibrationDrift` 对着生产库分布守着：漂出容许带就红。
CROWDING_NEUTRAL = 23.30      # 实测中位数（2026-08-16, n=1057）

# 期限缩放。⚠️ 三个期限是**同一个量的缩放**，不是三个独立预测，因此符号恒同。
# 下游 `rival_bee.py` 的 `avg_ret = (expected_7d + expected_30d)/2` 因此等于
# `expected_7d × 1.0` —— 看似在平均两个期限，实则没有平均任何东西。
# 保留缩放（这是期限结构假设），但**下游不得把三者当独立信号**。
_HORIZON_SCALE = {"expected_3d": 0.3, "expected_7d": 0.8, "expected_30d": 1.2}


def expected_returns(data: "TrainingData") -> Dict[str, float]:
    """由动量算三个期限的预期收益（单位：百分点）。

    ⚠️ **改动本函数（或 `predict_probability` / RivalBee 的特征来源）时，
    必须往 `ic_rerun_readiness._COHORT_HISTORY` 追加一条世代边界。**
    否则新旧口径的样本会被混算 —— 而混算是静默的：数字照出，只是没有意义。
    那个文件是"攒够样本后重跑 IC"这件事的唯一承载物。


    **三个模型类（SimpleMLModel / SGDMLModel / HGBModel）共用此函数。**
    旧实现在三处逐字重复同一公式，docstring 还写着"公式与 SimpleMLModel 完全
    一致" —— 那种一致性靠人肉维护，改一处漏两处就会让模型降级链出现口径分裂。

    刻意**不**把 `probability` 掺进来：probability 的特征里已经含 momentum，
    再加进来是对同一个信号双重计数。

    刻意**不**把 `crowding_score` 掺进来：方向未确立（连续版仅过 1/4 口径），
    且它已在 probability 里是权重最大的特征、又经 adj_factor 作用于综合分。
    理由详见上方 `CROWDING_NEUTRAL` 处的长注释。
    """
    mag = _CATALYST_MAGNITUDE.get(
        getattr(data, "catalyst_quality", None), _CATALYST_MAGNITUDE_DEFAULT
    )

    # 动量缺失/NaN → 0（无观点），而不是让 None 参与算术
    mom = getattr(data, "momentum_5d", 0.0)
    if mom is None or mom != mom:      # NaN 自身不等于自身
        mom = 0.0
    try:
        mom = float(mom)
    except (TypeError, ValueError):
        mom = 0.0

    core = mom                         # 百分点
    return {k: mag * core * s for k, s in _HORIZON_SCALE.items()}


def centered_feature(x: float, influence: float, inverse: bool = False) -> float:
    """把归一化特征 x∈[0,1] 映射到**以 0.5 为中心**、总宽度 influence 的区间。

    旧实现的分项形如 `0.3 + x*0.7`（中性点 0.65）与 `1.0 - x*0.3`（中性点 0.85），
    每一项的中性点都在 0.5 **之上**。八项叠加后 probability 的结构性地板是
    **0.3610**（与实测最小值 0.3500 吻合），到 0.5 只剩 0.139 空间，而向上有
    0.45 —— 不对称约 3.2 倍。实测 `ml.probability` **99.6% > 0.5**。

    本函数保留每项原有的影响力系数（`influence`，即旧式里 x 的乘数），
    只把中性点搬回 0.5。因此：
      · 全部特征取中位数时 probability **恰为 0.5**
      · 可达区间关于 0.5 对称
      · 各特征的相对影响力不变（不是靠削弱某项来消除偏斜）

    `influence=1.0` 时本函数等价于恒等映射 —— 旧实现里 catalyst / final_score /
    odds_score / risk_adj_score 四项本来就是裸 `x`，它们**本来就是居中的**，
    改动后数值完全不变。
    """
    delta = (x - 0.5) * influence
    return 0.5 - delta if inverse else 0.5 + delta


# v0.45.3: `centered_feature(0.5, influence, inverse)` 对任意 influence / inverse
# **恒等**返回 0.5（见上面的设计注释：全特征取中位数时 probability 恰为 0.5）。
# 所以 0.5 是这个坐标系里唯一能表达"这一维不投票"的数——缺数据时填别的才是伪造。
_FEATURE_NEUTRAL = 0.5
# 12 维里缺超过这个数，probability 就不该被当结论用（标 unreliable，不静默出数）
_MAX_IMPUTED_FEATURES = 4

_FEATURE_SOURCES = (
    "crowding", "catalyst", "momentum", "volatility", "sentiment",
    "iv_rank", "put_call_ratio", "final_score",
    "odds_score", "risk_adj_score", "agent_agreement", "direction_encoded",
)


def _missing_features(data) -> list:
    """列出 12 维里取不到值的特征名（None 或 NaN）。

    与 `normalize_feature` 的 0.5 插补是**成对**的设计：插补让向量凑得齐，
    这个函数负责把"哪几维是凑出来的"如实说出去。只留插补不留清单，
    等于把伪造换了个更好听的名字。
    """
    vals = (
        data.crowding_score, data.catalyst_quality, data.momentum_5d,
        data.volatility, data.market_sentiment, data.iv_rank,
        data.put_call_ratio, data.final_score, data.odds_score,
        data.risk_adj_score, data.agent_agreement, data.direction_encoded,
    )
    return [name for name, v in zip(_FEATURE_SOURCES, vals)
            if v is None or (isinstance(v, float) and v != v)]


def _feature_quality(data) -> Dict:
    """预测输出里的数据质量字段。缺失必须刺眼，不能只体现为一个居中的概率。"""
    missing = _missing_features(data)
    n = len(missing)
    tk = getattr(data, "ticker", "?")
    unreliable = n > _MAX_IMPUTED_FEATURES
    if n:
        _log.warning("%s: %d/%d 维特征取不到值，按中性插补: %s",
                     tk, n, len(_FEATURE_SOURCES), ",".join(missing))
    if unreliable:
        _log.error("🚨 %s: 12 维特征缺 %d 个（阈值 %d）——probability 不可信，"
                   "已标 unreliable=True，勿据此决策",
                   tk, n, _MAX_IMPUTED_FEATURES)
    return {
        "feature_completeness": f"{len(_FEATURE_SOURCES) - n}/{len(_FEATURE_SOURCES)}",
        "imputed_features": missing,
        "unreliable": unreliable,
    }


class SimpleMLModel:
    """简单机器学习模型（不依赖 sklearn）"""

    def __init__(self):
        self.weights = {
            "crowding": 0.18,
            "catalyst": 0.15,
            "momentum": 0.12,
            "volatility": 0.09,
            "sentiment": 0.06,
            # v2 新特征
            "iv_rank": 0.10,
            "put_call_ratio": 0.08,
            "final_score": 0.07,
            "odds_score": 0.05,
            "risk_adj_score": 0.04,
            "agent_agreement": 0.04,
            "direction_encoded": 0.02,
        }
        self.is_trained = False
        self.training_accuracy = 0.0
        self.feature_stats: Dict = {}

    def encode_catalyst_quality(self, quality: str) -> float:
        """编码催化剂质量"""
        mapping = {"A+": 1.0, "A": 0.85, "B+": 0.70, "B": 0.55, "C": 0.40}
        return mapping.get(quality, 0.5)

    def normalize_feature(
        self, value, min_val: float, max_val: float
    ) -> float:
        """特征归一化；None/NaN → `_FEATURE_NEUTRAL`（本模型精确的"无观点"点）。

        v0.45.3：原实现直接 `(value - min_val)`，value 为 None 时抛 TypeError。
        当前不可达（TrainingData 的构造方都已 sanitize），但契约只靠类型注解
        `momentum_5d: float` 维系，没有运行时强制——而上游 data_pipeline 自
        v0.38.0 起就在诚实吐 None，两边迟早对上。

        为什么是 0.5 而不是抛错、也不是训练均值：见 `_FEATURE_NEUTRAL` 注释。
        为什么这不算静默伪造：缺失清单由 `_missing_features()` 独立报出，
        并写进 `predict_return()` 的 `imputed_features` / `unreliable`。
        """
        if value is None or (isinstance(value, float) and value != value):
            return _FEATURE_NEUTRAL
        if max_val == min_val:
            return _FEATURE_NEUTRAL
        return (value - min_val) / (max_val - min_val)

    def train(self, training_data: List[TrainingData]) -> Dict:
        """训练模型"""
        if not training_data:
            return {"status": "error", "message": "no training data"}

        _log.debug("开始训练 ML 模型...")
        _log.debug("训练样本数：%s", len(training_data))

        # 提取特征
        crowding_scores = [d.crowding_score for d in training_data]
        catalyst_qualities = [
            self.encode_catalyst_quality(d.catalyst_quality) for d in training_data
        ]
        momentums = [d.momentum_5d for d in training_data]
        volatilities = [d.volatility for d in training_data]
        sentiments = [d.market_sentiment for d in training_data]
        iv_ranks = [d.iv_rank for d in training_data]
        put_call_ratios = [d.put_call_ratio for d in training_data]
        final_scores = [d.final_score for d in training_data]
        odds_scores = [d.odds_score for d in training_data]
        risk_adj_scores = [d.risk_adj_score for d in training_data]
        agent_agreements = [d.agent_agreement for d in training_data]
        direction_encodeds = [d.direction_encoded for d in training_data]
        win_7d = [d.win_7d for d in training_data]  # 目标：7 日是否赚钱

        def _safe_stats(vals, name):
            return {"min": min(vals), "max": max(vals),
                    "mean": statistics.mean(vals)} if vals else {"min": 0, "max": 1, "mean": 0.5}

        # 计算特征的统计信息
        self.feature_stats = {
            "crowding": _safe_stats(crowding_scores, "crowding"),
            "catalyst": {"min": 0.4, "max": 1.0, "mean": 0.7},
            "momentum": _safe_stats(momentums, "momentum"),
            "volatility": _safe_stats(volatilities, "volatility"),
            "sentiment": _safe_stats(sentiments, "sentiment"),
            # v2
            "iv_rank": _safe_stats(iv_ranks, "iv_rank"),
            "put_call_ratio": _safe_stats(put_call_ratios, "put_call_ratio"),
            "final_score": _safe_stats(final_scores, "final_score"),
            "odds_score": _safe_stats(odds_scores, "odds_score"),
            "risk_adj_score": _safe_stats(risk_adj_scores, "risk_adj_score"),
            "agent_agreement": _safe_stats(agent_agreements, "agent_agreement"),
            "direction_encoded": _safe_stats(direction_encodeds, "direction_encoded"),
        }

        # 计算每个特征与目标的相关性（简单相关系数）
        correlations = self._calculate_correlations(
            training_data, win_7d
        )

        # 更新权重基于相关性
        total_corr = sum(abs(c) for c in correlations.values())
        if total_corr > 0:
            for key in correlations:
                self.weights[key] = abs(correlations[key]) / total_corr

        _log.info("权重更新：%s", self.weights)

        # 计算训练准确率
        predictions = [self.predict_probability(d) for d in training_data]
        correct = sum(
            1 for pred, actual in zip(predictions, win_7d)
            if (pred > 0.5) == actual
        )
        self.training_accuracy = correct / len(win_7d) * 100

        _log.debug("训练准确率：%.1f%%", self.training_accuracy)

        self.is_trained = True

        return {
            "status": "success",
            "samples": len(training_data),
            "accuracy": self.training_accuracy,
            "weights": self.weights,
        }

    def _calculate_correlations(self, data: List[TrainingData], target: List[bool]) -> Dict:
        """计算特征与目标的相关性"""
        target_numeric = [1.0 if x else 0.0 for x in target]

        feature_vals = {
            "crowding": [d.crowding_score for d in data],
            "catalyst": [self.encode_catalyst_quality(d.catalyst_quality) for d in data],
            "momentum": [d.momentum_5d for d in data],
            "volatility": [d.volatility for d in data],
            "sentiment": [d.market_sentiment for d in data],
            # v2
            "iv_rank": [d.iv_rank for d in data],
            "put_call_ratio": [d.put_call_ratio for d in data],
            "final_score": [d.final_score for d in data],
            "odds_score": [d.odds_score for d in data],
            "risk_adj_score": [d.risk_adj_score for d in data],
            "agent_agreement": [d.agent_agreement for d in data],
            "direction_encoded": [d.direction_encoded for d in data],
        }

        return {name: self._simple_correlation(vals, target_numeric)
                for name, vals in feature_vals.items()}

    def _simple_correlation(self, x: List[float], y: List[float]) -> float:
        """计算简单皮尔逊相关系数"""
        n = len(x)
        if n < 2:
            return 0.0

        mean_x = statistics.mean(x)
        mean_y = statistics.mean(y)

        numerator = sum(
            (x[i] - mean_x) * (y[i] - mean_y) for i in range(n)
        )
        denominator_x = sum((xi - mean_x) ** 2 for xi in x) ** 0.5
        denominator_y = sum((yi - mean_y) ** 2 for yi in y) ** 0.5

        if denominator_x == 0 or denominator_y == 0:
            return 0.0

        return numerator / (denominator_x * denominator_y)

    def predict_probability(self, data: TrainingData) -> float:
        """预测赚钱概率（0-1）"""

        def _norm(feat_name, raw_val):
            stats = self.feature_stats.get(feat_name, {"min": 0, "max": 1})
            return self.normalize_feature(raw_val, stats["min"], stats["max"])

        crowding_norm = _norm("crowding", data.crowding_score)
        catalyst_norm = _norm("catalyst", self.encode_catalyst_quality(data.catalyst_quality))
        momentum_norm = _norm("momentum", data.momentum_5d)
        volatility_norm = _norm("volatility", data.volatility)
        sentiment_norm = _norm("sentiment", data.market_sentiment)
        iv_rank_norm = _norm("iv_rank", data.iv_rank)
        pcr_norm = _norm("put_call_ratio", data.put_call_ratio)
        final_score_norm = _norm("final_score", data.final_score)
        odds_norm = _norm("odds_score", data.odds_score)
        risk_adj_norm = _norm("risk_adj_score", data.risk_adj_score)
        agreement_norm = _norm("agent_agreement", data.agent_agreement)
        direction_norm = _norm("direction_encoded", data.direction_encoded)

        # 加权求和。每个分项经 centered_feature 映射到**以 0.5 为中心**的区间，
        # 权重和 = 1.0 ⇒ 全特征取中位数时 probability 恰为 0.5，可达区间对称。
        #
        # v0.44.1 修正：旧实现的注释声称"每个分项必须在 [0,1] 范围内 …… 加权和
        # 自然在 [0,1]"。范围确实在 [0,1]，但**中性点不在 0.5**：
        #   crowding `1.0 - x*0.3` → [0.7,1.0] 中心 0.85
        #   iv_rank  `1.0 - x*0.3` → [0.7,1.0] 中心 0.85
        #   pcr      `1.0 - x*0.4` → [0.6,1.0] 中心 0.80
        #   volatility `1.0 - x*0.5` → [0.5,1.0] 中心 0.75
        #   momentum / sentiment / agent_agreement / direction_encoded
        #            `0.3 + x*0.7` → [0.3,1.0] 中心 0.65
        # 八项的中性点全在 0.5 之上 ⇒ 结构性地板 0.3610（实测最小 0.3500），
        # 向下空间 0.139 vs 向上 0.45，实测 99.6% > 0.5 —— 它无法表达"强烈看空"。
        #
        # 第二个参数是各特征原有的**影响力系数**（旧式里 x 的乘数），逐一保留，
        # 所以本次只消除偏斜，不改变特征间的相对影响力。
        probability = (
            self.weights.get("crowding", 0) * centered_feature(crowding_norm, 0.3, inverse=True)
            + self.weights.get("catalyst", 0) * centered_feature(catalyst_norm, 1.0)
            + self.weights.get("momentum", 0) * centered_feature(momentum_norm, 0.7)
            + self.weights.get("volatility", 0) * centered_feature(volatility_norm, 0.5, inverse=True)
            + self.weights.get("sentiment", 0) * centered_feature(sentiment_norm, 0.7)
            # v2 新特征
            + self.weights.get("iv_rank", 0) * centered_feature(iv_rank_norm, 0.3, inverse=True)  # 高IV→略降
            + self.weights.get("put_call_ratio", 0) * centered_feature(pcr_norm, 0.4, inverse=True)  # 高P/C→看空
            + self.weights.get("final_score", 0) * centered_feature(final_score_norm, 1.0)
            + self.weights.get("odds_score", 0) * centered_feature(odds_norm, 1.0)
            + self.weights.get("risk_adj_score", 0) * centered_feature(risk_adj_norm, 1.0)
            + self.weights.get("agent_agreement", 0) * centered_feature(agreement_norm, 0.7)
            + self.weights.get("direction_encoded", 0) * centered_feature(direction_norm, 0.7)
        )

        return max(0.0, min(1.0, probability))

    def predict_return(self, data: TrainingData) -> Dict:
        """预测收益。公式唯一真相 = 模块级 `expected_returns()`（三个类共用）。"""
        return {
            "probability": self.predict_probability(data),
            **expected_returns(data),
            **_feature_quality(data),   # v0.45.3: 插补了哪几维，必须随结论一起走
        }

    def get_feature_importance(self) -> dict:
        """获取特征重要度（基于相关性权重）"""
        if not self.is_trained:
            return {}
        total = sum(abs(v) for v in self.weights.values())
        if total == 0:
            return {}
        importance = {}
        for name, w in self.weights.items():
            importance[name] = {
                "weight": round(abs(w) / total, 4),
                "coefficient": round(w, 4),
                "direction": "positive" if w > 0 else ("negative" if w < 0 else "neutral"),
            }
        return dict(sorted(importance.items(), key=lambda x: -x[1]["weight"]))

    def save_model(self, filename: str = "ml_model.json"):
        """保存模型（JSON 格式，安全序列化）"""
        model_data = {
            "weights": self.weights,
            "feature_stats": self.feature_stats,
            "training_accuracy": self.training_accuracy,
            "is_trained": self.is_trained,
        }

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(model_data, f, ensure_ascii=False, indent=2)

        _log.info("模型已保存：%s", filename)

    def load_model(self, filename: str = "ml_model.json"):
        """加载模型（JSON 格式，安全反序列化）"""
        # 兼容旧版 pickle 文件
        if filename.endswith(".pkl") and not os.path.exists(filename):
            filename = filename.replace(".pkl", ".json")
        try:
            with open(filename, "r", encoding="utf-8") as f:
                model_data = json.load(f)

            self.weights = model_data["weights"]
            self.feature_stats = model_data["feature_stats"]
            self.training_accuracy = model_data["training_accuracy"]
            self.is_trained = model_data["is_trained"]

            _log.info("模型已加载：%s", filename)
            return True
        except FileNotFoundError:
            _log.warning("模型文件不存在：%s", filename)
            return False


# ---------------------------------------------------------------------------
# 特征名定义（SGDMLModel 与 SimpleMLModel 共享）
# ---------------------------------------------------------------------------
FEATURE_NAMES = [
    "crowding", "catalyst", "momentum", "volatility", "sentiment",  # v1: 原始 5 维
    "iv_rank", "put_call_ratio", "final_score",                     # v2: DB 直取
    "odds_score", "risk_adj_score",                                  # v2: dimension_scores
    "agent_agreement", "direction_encoded",                          # v2: 派生
]
FEATURE_NAMES_V1 = ["crowding", "catalyst", "momentum", "volatility", "sentiment"]


def _encode_catalyst(quality: str) -> float:
    """编码催化剂质量（共享工具函数）"""
    return {"A+": 1.0, "A": 0.85, "B+": 0.70, "B": 0.55, "C": 0.40}.get(quality, 0.5)


def _extract_features(data: TrainingData) -> list:
    """从 TrainingData 提取 12 维特征向量"""
    return [
        data.crowding_score,
        _encode_catalyst(data.catalyst_quality),
        data.momentum_5d,
        data.volatility,
        data.market_sentiment,
        # v2 新特征
        data.iv_rank,
        data.put_call_ratio,
        data.final_score,
        data.odds_score,
        data.risk_adj_score,
        data.agent_agreement,
        data.direction_encoded,
    ]


class SGDMLModel:
    """
    sklearn SGDClassifier 在线学习模型（JSON 序列化，无 pickle）。

    支持：
    - train(data)           全量训练（多轮 partial_fit 收敛）
    - incremental_train(data) 增量学习（仅新数据 partial_fit 1 轮）
    - predict_probability(data) → float 0~1
    - predict_return(data) → dict（公式与 SimpleMLModel 一致）
    - save_model / load_model（JSON 往返，向下兼容旧格式）
    """

    def __init__(self):
        from sklearn.linear_model import SGDClassifier
        from sklearn.preprocessing import StandardScaler

        self._clf = SGDClassifier(
            loss="log_loss",
            penalty="l2",
            alpha=0.001,
            learning_rate="optimal",
            random_state=42,
        )
        self._scaler = StandardScaler()
        self._scaler_fitted = False
        self._clf_fitted = False
        self._n_samples_seen = 0

        # 兼容 SimpleMLModel 接口
        self.is_trained = False
        self.training_accuracy = 0.0
        self.feature_stats: Dict = {}

    # ---- 属性兼容 SimpleMLModel ----
    @property
    def weights(self) -> dict:
        """将 SGD 系数绝对值归一化为权重 dict"""
        n = len(FEATURE_NAMES)
        uniform = 1.0 / n
        if not self._clf_fitted:
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        raw = self._clf.coef_[0]
        if len(raw) != n:
            # 维度不匹配（旧模型），返回均匀权重
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        abs_sum = sum(abs(w) for w in raw)
        if abs_sum == 0:
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        return {name: abs(raw[i]) / abs_sum for i, name in enumerate(FEATURE_NAMES)}

    @weights.setter
    def weights(self, value):
        """允许外部赋值（加载旧格式时兼容）"""
        pass  # SGD 权重由 coef_ 驱动，忽略手动设置

    # ---- 训练 ----
    def train(self, training_data: List[TrainingData]) -> Dict:
        """全量训练（多轮 partial_fit 直到收敛）"""
        import numpy as np

        if len(training_data) < 2:
            _log.warning("SGD 训练样本不足 (%d)，跳过", len(training_data))
            return {"status": "error", "message": "need >= 2 samples"}

        X = np.array([_extract_features(d) for d in training_data], dtype=np.float64)
        y = np.array([1 if d.win_7d else 0 for d in training_data])

        # 拟合 scaler
        self._scaler.fit(X)
        self._scaler_fitted = True
        X_scaled = self._scaler.transform(X)

        # 多轮 partial_fit 收敛
        classes = np.array([0, 1])
        for _ in range(20):
            self._clf.partial_fit(X_scaled, y, classes=classes)
        self._clf_fitted = True
        self._n_samples_seen = len(training_data)

        # 计算特征统计（兼容 SimpleMLModel）
        self.feature_stats = {
            name: {
                "min": float(X[:, i].min()),
                "max": float(X[:, i].max()),
                "mean": float(X[:, i].mean()),
            }
            for i, name in enumerate(FEATURE_NAMES)
        }

        # 计算训练准确率
        preds = self._clf.predict(X_scaled)
        self.training_accuracy = float((preds == y).mean() * 100)
        self.is_trained = True

        _log.info(
            "SGD 训练完成：%d 样本，准确率 %.1f%%，权重 %s",
            len(training_data),
            self.training_accuracy,
            {k: f"{v:.2f}" for k, v in self.weights.items()},
        )

        return {
            "status": "success",
            "samples": len(training_data),
            "accuracy": self.training_accuracy,
            "weights": self.weights,
        }

    def incremental_train(self, new_data: List[TrainingData]) -> Dict:
        """增量学习：仅对新数据 partial_fit 一次"""
        import numpy as np

        if not new_data:
            return {"status": "skip", "message": "no new data"}

        if not self._scaler_fitted:
            # 冷启动：降级为全量训练
            return self.train(new_data)

        X = np.array([_extract_features(d) for d in new_data], dtype=np.float64)
        y = np.array([1 if d.win_7d else 0 for d in new_data])

        # 增量更新 scaler（partial_fit）
        self._scaler.partial_fit(X)
        X_scaled = self._scaler.transform(X)

        classes = np.array([0, 1])
        self._clf.partial_fit(X_scaled, y, classes=classes)
        self._clf_fitted = True
        self._n_samples_seen += len(new_data)

        # 更新准确率（仅在新数据上评估）
        preds = self._clf.predict(X_scaled)
        batch_acc = float((preds == y).mean() * 100)

        _log.info(
            "SGD 增量学习：+%d 样本（累计 %d），本批准确率 %.1f%%",
            len(new_data), self._n_samples_seen, batch_acc,
        )

        return {
            "status": "success",
            "new_samples": len(new_data),
            "total_samples": self._n_samples_seen,
            "batch_accuracy": batch_acc,
        }

    # ---- 预测 ----
    def predict_probability(self, data: TrainingData) -> float:
        """预测赚钱概率 (0~1)，含小样本校准"""
        import numpy as np

        if not self._clf_fitted:
            return 0.5  # 未训练时返回默认值

        X = np.array([_extract_features(data)], dtype=np.float64)
        # v0.45.3: None 经 np.array(dtype=float64) 会**静默变成 NaN**，再进
        # scaler / predict_proba 要么抛要么吐 NaN。用训练均值填补——
        # StandardScaler 之后恰为 0，即该维不参与投票。
        _bad = ~np.isfinite(X[0])
        if _bad.any():
            _mean = getattr(self._scaler, "mean_", None)
            X[0, _bad] = _mean[_bad] if _mean is not None else 0.0
            _log.warning("%s: %d 维特征为 NaN，已用训练均值填补（标准化后中性）",
                         getattr(data, "ticker", "?"), int(_bad.sum()))
        X_scaled = self._scaler.transform(X)
        prob = self._clf.predict_proba(X_scaled)[0]

        # prob 是 [P(class=0), P(class=1)]
        raw_prob = float(prob[1]) if len(prob) > 1 else float(prob[0])

        # --- 小样本校准（防止极端概率 0%/100%）---
        # 样本 < MIN_CONFIDENT 时，按比例混合先验 50%
        # 随着样本增加，逐步信任模型原始输出
        MIN_CONFIDENT = 100
        confidence_ratio = min(self._n_samples_seen / MIN_CONFIDENT, 1.0)
        calibrated = raw_prob * confidence_ratio + 0.5 * (1 - confidence_ratio)

        # 硬裁剪：永不超过 [5%, 95%] 区间（即使 100+ 样本也不该过度自信）
        calibrated = max(0.05, min(0.95, calibrated))

        return calibrated

    def predict_return(self, data: TrainingData) -> Dict:
        """预测收益。公式唯一真相 = 模块级 `expected_returns()`。

        v0.44.1 之前这里是**逐字复制**的第二份公式，docstring 写着"与
        SimpleMLModel 完全一致" —— 那种一致性靠人肉维护，改一处漏两处
        就会让模型降级链出现口径分裂。现在没有第二份可漏。
        """
        return {
            "probability": self.predict_probability(data),
            **expected_returns(data),
            **_feature_quality(data),   # v0.45.3: 插补了哪几维，必须随结论一起走
        }

    # ---- 序列化（JSON，无 pickle）----
    def save_model(self, filename: str = "ml_model.json"):
        """保存模型到 JSON"""
        import numpy as np

        model_data: Dict = {
            "model_type": "sgd",
            "feature_count": len(FEATURE_NAMES),
            "feature_names": FEATURE_NAMES,
            "is_trained": self.is_trained,
            "training_accuracy": self.training_accuracy,
            "n_samples_seen": self._n_samples_seen,
            "feature_stats": self.feature_stats,
            "weights": self.weights,
            "feature_importance": self.get_feature_importance(),
        }

        # SGD 分类器参数
        if self._clf_fitted:
            model_data["sgd"] = {
                "coef": self._clf.coef_.tolist(),
                "intercept": self._clf.intercept_.tolist(),
                "classes": self._clf.classes_.tolist(),
                "t": float(getattr(self._clf, "t_", 0)),
            }

        # StandardScaler 参数
        if self._scaler_fitted:
            model_data["scaler"] = {
                "mean": self._scaler.mean_.tolist(),
                "var": self._scaler.var_.tolist(),
                "scale": self._scaler.scale_.tolist(),
                "n_samples_seen": int(self._scaler.n_samples_seen_),
            }

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(model_data, f, ensure_ascii=False, indent=2)

        _log.info("SGD 模型已保存：%s", filename)

    def load_model(self, filename: str = "ml_model.json"):
        """从 JSON 加载模型（兼容旧 SimpleMLModel 格式）"""
        import numpy as np

        if filename.endswith(".pkl") and not os.path.exists(filename):
            filename = filename.replace(".pkl", ".json")

        try:
            with open(filename, "r", encoding="utf-8") as f:
                model_data = json.load(f)
        except FileNotFoundError:
            _log.warning("模型文件不存在：%s", filename)
            return False

        # 检测格式类型
        if model_data.get("model_type") == "sgd":
            # SGD 格式
            self.training_accuracy = model_data.get("training_accuracy", 0.0)
            self._n_samples_seen = model_data.get("n_samples_seen", 0)
            self.feature_stats = model_data.get("feature_stats", {})

            # 检测特征维度兼容性
            n_expected = len(FEATURE_NAMES)
            sgd_data = model_data.get("sgd", {})
            saved_coef = sgd_data.get("coef", [[]])
            saved_n = len(saved_coef[0]) if saved_coef and saved_coef[0] else 0

            if saved_n > 0 and saved_n != n_expected:
                _log.info(
                    "模型特征维度不匹配 (saved=%d, expected=%d)，需重新训练",
                    saved_n, n_expected,
                )
                self.is_trained = False  # 强制重训
                return True

            self.is_trained = model_data["is_trained"]

            if "sgd" in model_data and saved_n == n_expected:
                sgd = model_data["sgd"]
                self._clf.coef_ = np.array(sgd["coef"])
                self._clf.intercept_ = np.array(sgd["intercept"])
                self._clf.classes_ = np.array(sgd["classes"])
                if "t" in sgd:
                    self._clf.t_ = sgd["t"]
                self._clf_fitted = True

            if "scaler" in model_data:
                sc = model_data["scaler"]
                sc_mean = np.array(sc["mean"])
                if len(sc_mean) == n_expected:
                    self._scaler.mean_ = sc_mean
                    self._scaler.var_ = np.array(sc["var"])
                    self._scaler.scale_ = np.array(sc["scale"])
                    # partial_fit 需要 numpy 类型（有 .shape 属性），不能用纯 int
                    self._scaler.n_samples_seen_ = np.int64(sc.get("n_samples_seen", 1))
                    self._scaler.n_features_in_ = n_expected  # sklearn 1.x 兼容
                    self._scaler_fitted = True
                else:
                    _log.info("Scaler 维度不匹配，跳过加载")

            _log.info("SGD 模型已加载：%s", filename)
            return True
        else:
            # 旧格式（SimpleMLModel）—— 加载基础属性，需要重新训练 SGD
            _log.info("检测到旧格式模型，加载基础信息后需重新训练 SGD")
            self.feature_stats = model_data.get("feature_stats", {})
            self.training_accuracy = model_data.get("training_accuracy", 0.0)
            # 标记为未训练，让 Service 自动重训
            self.is_trained = False
            return True

    def get_feature_importance(self) -> dict:
        """获取特征重要度（基于 SGD 系数绝对值归一化）

        Returns:
            {feature_name: {"weight": float, "coefficient": float, "direction": str}}
            按重要度降序排列；未训练时返回空 dict。
        """
        if not self._clf_fitted:
            return {}
        raw = self._clf.coef_[0]
        n = len(FEATURE_NAMES)
        if len(raw) != n:
            return {}
        abs_vals = [abs(w) for w in raw]
        total = sum(abs_vals)
        if total == 0:
            return {name: {"weight": 0.0, "coefficient": 0.0, "direction": "neutral"}
                    for name in FEATURE_NAMES}
        importance = {}
        for i, name in enumerate(FEATURE_NAMES):
            importance[name] = {
                "weight": round(abs_vals[i] / total, 4),
                "coefficient": round(float(raw[i]), 4),
                "direction": "positive" if raw[i] > 0 else ("negative" if raw[i] < 0 else "neutral"),
            }
        return dict(sorted(importance.items(), key=lambda x: -x[1]["weight"]))

    def encode_catalyst_quality(self, quality: str) -> float:
        """兼容 SimpleMLModel 接口"""
        return _encode_catalyst(quality)


def build_training_data_from_db(
    db_path: str = None,
    min_samples: int = 30,
    max_rows: int = 500,
) -> List[TrainingData]:
    """从 backtester.predictions 表构建真实训练数据

    仅使用 checked_t7=1 且 return_t7 IS NOT NULL 的已验证记录。
    样本不足 min_samples 时返回空列表（调用方降级到硬编码数据）。

    Args:
        db_path: SQLite 数据库路径（默认使用 config.PATHS.db）
        min_samples: 最少样本数
        max_rows: 最多训练行数

    Returns:
        TrainingData 列表（可能为空）
    """
    import sqlite3

    if db_path is None:
        try:
            from hive_logger import PATHS
            db_path = str(PATHS.db)
        except (ImportError, AttributeError):
            _log.debug("build_training_data_from_db: 无法获取 DB 路径")
            return []

    if not os.path.exists(db_path):
        return []

    def _cat_qual(v: float) -> str:
        if v >= 8.5: return "A+"
        if v >= 7.5: return "A"
        if v >= 6.5: return "B+"
        if v >= 5.5: return "B"
        return "C"

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # v0.45.9 P0：模糊样本（|return| <= 容差）的 correct_t7 标签无意义，
        # 用作训练标签会往模型里灌噪音，故排除。列可能尚未迁移（旧库副本 /
        # 测试夹具），先探测再拼接，避免 "no such column" 直接吞掉全部样本。
        _has_amb = any(
            r[1] == "ambiguous_t7"
            for r in conn.execute("PRAGMA table_info(predictions)")
        )
        _amb_clause = " AND COALESCE(ambiguous_t7, 0) = 0" if _has_amb else ""
        cursor.execute(
            "SELECT * FROM predictions "
            "WHERE checked_t7 = 1 AND return_t7 IS NOT NULL"
            + _amb_clause +
            " ORDER BY date DESC LIMIT ?",
            (max_rows,),
        )
        rows = cursor.fetchall()
        conn.close()
    except (sqlite3.Error, OSError) as e:
        _log.debug("build_training_data_from_db 查询失败: %s", e)
        return []

    if len(rows) < min_samples:
        _log.debug("真实数据不足: %d < %d", len(rows), min_samples)
        return []

    direction_map = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
    result = []
    _skipped_incomplete = 0          # v0.45.50：维度缺失而被剔除的样本数
    _REQUIRED_DIMS = ("signal", "catalyst", "sentiment", "odds", "risk_adj")
    for r in rows:
        try:
            ds = json.loads(r["dimension_scores"] or "{}")

            # ── v0.45.50：维度缺失的样本**剔除**，不再用 5.0 补齐 ──
            # 旧实现 `ds.get("signal", 5.0)` 等五处缺失即补 5.0，而下面的
            # _momentum / _vol / _iv / _pc 全部由这五个值派生 —— 于是一条
            # 「某只蜂当天挂了」的记录，会变成一整行内部完全自洽的
            # 「各维中性、动量 0%、波动率 12.5%、IV Rank 50」正常样本，
            # 模型不会拒绝它，照常学。
            #
            # 讽刺的是下面第 1152/1159 行已经把 `50.0` 与 `1.0` 当可疑哨兵
            # （`_iv_db != 50.0` 才采信 DB 值），却仍在上游亲手制造同类值。
            #
            # 实测代价：910 条训练候选里 96.9% 五维齐全，剔除只损失 28 条（3.1%）。
            # 用 3% 样本换掉伪造特征向量，划算。
            if not all(isinstance(ds.get(k), (int, float)) for k in _REQUIRED_DIMS):
                _skipped_incomplete += 1
                continue
            ad = json.loads(r["agent_directions"] or "{}")

            _dir = r["direction"] or "neutral"
            if ad:
                _majority = sum(1 for d in ad.values() if d == _dir)
                _agree = _majority / len(ad)
            else:
                _agree = 0.5

            return_t7 = float(r["return_t7"])
            # 使用 backtester 的方向感知正确性标记（非简单 return>0）
            is_correct = bool(r["correct_t7"]) if r["correct_t7"] is not None else (return_t7 > 0)

            # 修复死特征：从 dimension_scores 推导真实信号
            # v0.45.50：上方已保证五维齐全，无需再兜底（兜底值会伪造样本）
            _sig = ds["signal"]
            _cat = ds["catalyst"]
            _sent = ds["sentiment"]
            _odds = ds["odds"]
            _risk = ds["risk_adj"]

            # momentum: 信号强度偏离中性的方向 × 幅度（正=看多动量，负=看空动量）
            _momentum = (_sig - 5.0) * 2.0 + (_sent - 5.0) * 1.5

            # volatility: risk_adj 低 → 高风险 → 高波动（反转映射）
            _vol = max(1.0, (10.0 - _risk) * 2.5)

            # iv_rank: 优先 DB 真实值，否则从 odds 维度推导
            try:
                _iv_db = float(r["iv_rank"]) if r["iv_rank"] is not None else None
            except (ValueError, TypeError):
                _iv_db = None
            _iv = _iv_db if (_iv_db is not None and _iv_db != 50.0) else _odds * 10.0

            # put_call_ratio: 优先 DB 真实值，否则从 odds 方向推导
            try:
                _pc_db = float(r["put_call_ratio"]) if r["put_call_ratio"] is not None else None
            except (ValueError, TypeError):
                _pc_db = None
            _pc = _pc_db if (_pc_db is not None and _pc_db != 1.0) else (0.6 + (10.0 - _odds) * 0.15)

            result.append(TrainingData(
                ticker=r["ticker"],
                date=r["date"],
                crowding_score=_sig * 10,
                catalyst_quality=_cat_qual(_cat),
                momentum_5d=round(_momentum, 2),
                volatility=round(_vol, 2),
                market_sentiment=(_sent - 5) * 20,
                actual_return_3d=return_t7 * 0.4,
                actual_return_7d=return_t7,
                actual_return_30d=return_t7 * 2.5,
                win_3d=is_correct,
                win_7d=is_correct,
                win_30d=is_correct,
                iv_rank=round(_iv, 2),
                put_call_ratio=round(_pc, 2),
                final_score=float(r["final_score"]) if r["final_score"] is not None else 5.0,
                odds_score=_odds,
                risk_adj_score=_risk,
                agent_agreement=_agree,
                direction_encoded=direction_map.get(_dir, 0.0),
            ))
        except (KeyError, ValueError, TypeError) as e:
            _log.debug("build_training_data_from_db: 跳过行: %s", e)
            continue

    if _skipped_incomplete:
        _log.warning("build_training_data_from_db: 剔除 %d 条维度缺失样本"
                     "（不再用 5.0 补齐伪造特征向量）", _skipped_incomplete)
    _log.info("build_training_data_from_db: 成功构建 %d 条真实训练数据", len(result))
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  HGBModel — HistGradientBoosting 树模型（sklearn 内置 LightGBM 等价物）
#  v15.0 升级：替代 SGDMLModel，捕捉非线性特征交互，内置 early stopping
# ═══════════════════════════════════════════════════════════════════════════

class HGBModel:
    """
    sklearn HistGradientBoostingClassifier（LightGBM 等价物，无需额外安装）。

    优于 SGDMLModel 的地方：
    - 非线性特征交互（树分裂 vs 超平面）
    - 内置 early stopping（防过拟合）
    - 自动处理缺失值和异常值
    - 特征重要性基于信息增益（vs SGD 系数绝对值）
    """

    def __init__(self):
        from sklearn.ensemble import HistGradientBoostingClassifier

        try:
            from config import ML_HGBC_CONFIG as _cfg
        except (ImportError, AttributeError):
            _cfg = {}

        self._clf = HistGradientBoostingClassifier(
            max_iter=_cfg.get("max_iter", 200),
            max_depth=_cfg.get("max_depth", 4),
            learning_rate=_cfg.get("learning_rate", 0.05),
            min_samples_leaf=_cfg.get("min_samples_leaf", 5),
            l2_regularization=_cfg.get("l2_regularization", 1.0),
            max_features=_cfg.get("max_features", 0.8),
            early_stopping=True,
            validation_fraction=_cfg.get("validation_fraction", 0.15),
            n_iter_no_change=_cfg.get("n_iter_no_change", 15),
            random_state=42,
        )
        self._fitted = False
        self._n_samples_seen = 0

        # 兼容 SimpleMLModel / SGDMLModel 接口
        self.is_trained = False
        self.training_accuracy = 0.0
        # v0.40.0: OOS（时序 purged split 外样本）精度——真实泛化能力指标；
        # training_accuracy 是 in-sample 自考自评，仅作参考
        self.oos_accuracy: float | None = None
        self.feature_stats = {}

    @property
    def weights(self) -> dict:
        """特征重要性（permutation importance）"""
        n = len(FEATURE_NAMES)
        uniform = 1.0 / n
        if not self._fitted or not hasattr(self, "_perm_importance"):
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        imp = self._perm_importance
        if len(imp) != n:
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        total = sum(abs(v) for v in imp)
        if total == 0:
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        return {name: abs(float(imp[i])) / total for i, name in enumerate(FEATURE_NAMES)}

    @weights.setter
    def weights(self, value):
        pass  # 树模型权重由 feature_importances_ 驱动

    # ---- v0.40.0: purged 时序切分 OOS 验证 ----
    def _eval_oos_purged(self, training_data: List[TrainingData],
                         test_pct: float = 0.25, embargo_days: int = 7):
        """按日期排序切分 train/test，train 尾与 test 头之间空出 embargo，
        在 clone 模型上评估外样本精度。返回 OOS accuracy (0-100) 或 None。"""
        import numpy as np
        from datetime import datetime as _dt, timedelta as _td
        from sklearn.base import clone as _clone

        rows = sorted(training_data, key=lambda d: d.date or "")
        n_test = max(10, int(len(rows) * test_pct))
        if len(rows) - n_test < 30:
            return None
        train_rows, test_rows = rows[:-n_test], rows[-n_test:]

        # embargo：剔除距 test 首日不足 embargo_days 的 train 尾部样本
        # （其 t+7 标签窗口与 test 期重叠 = 泄漏）
        try:
            first_test = _dt.strptime(test_rows[0].date[:10], "%Y-%m-%d")
            cutoff = first_test - _td(days=embargo_days)
            train_rows = [r for r in train_rows
                          if _dt.strptime(r.date[:10], "%Y-%m-%d") <= cutoff]
        except (ValueError, TypeError):
            pass  # 日期不可解析时退化为纯时序切分（仍优于随机切分）
        if len(train_rows) < 30:
            return None

        Xtr = np.array([_extract_features(d) for d in train_rows], dtype=np.float64)
        ytr = np.array([1 if d.win_7d else 0 for d in train_rows])
        Xte = np.array([_extract_features(d) for d in test_rows], dtype=np.float64)
        yte = np.array([1 if d.win_7d else 0 for d in test_rows])
        if len(set(ytr.tolist())) < 2:
            return None  # 单一类别无法训练

        from collections import Counter as _Counter
        _c = _Counter(ytr.tolist())
        _w = np.array([len(ytr) / (2 * _c[yi]) for yi in ytr])

        clf = _clone(self._clf)
        clf.set_params(early_stopping=len(train_rows) >= 30)
        clf.fit(Xtr, ytr, sample_weight=_w)
        oos = float((clf.predict(Xte) == yte).mean() * 100)
        _log.info("HGB OOS 验证（purged 时序切分）：train=%d test=%d embargo=%dd → OOS %.1f%%",
                  len(train_rows), len(test_rows), embargo_days, oos)
        return oos

    # ---- 训练 ----
    def train(self, training_data: List[TrainingData]) -> Dict:
        """全量训练（含类别平衡 + early stopping）"""
        import numpy as np

        if len(training_data) < 2:
            _log.warning("HGB 训练样本不足 (%d)，跳过", len(training_data))
            return {"status": "error", "message": "need >= 2 samples"}

        X = np.array([_extract_features(d) for d in training_data], dtype=np.float64)
        y = np.array([1 if d.win_7d else 0 for d in training_data])

        # 类别平衡：用 sample_weight 补偿不均匀
        from collections import Counter
        counts = Counter(y.tolist())
        total_n = len(y)
        sample_weights = np.array([total_n / (2 * counts[yi]) for yi in y])

        # ── v0.40.0: OOS 时序验证（purged split，先验证后全样本部署）────────
        # training_accuracy 是 in-sample 自考自评（曾报 75% 制造虚假安全感）；
        # 这里按日期排序切出尾部 25% 作外样本，train 尾与 test 头之间空出
        # embargo_days（=标签横跨期 7 天，防 t+7 标签泄漏），在 clone 模型上
        # 评估真实泛化精度，然后才全样本重训供生产预测（验证与部署分离）。
        self.oos_accuracy = None
        if len(training_data) >= 60:
            try:
                self.oos_accuracy = self._eval_oos_purged(
                    training_data, test_pct=0.25, embargo_days=7)
            except Exception as _e_oos:
                _log.warning("OOS 时序验证失败（不影响训练）: %s", _e_oos)

        # 样本量极小时关闭 early stopping（需要 ≥20 条验证集）
        if len(training_data) < 30:
            self._clf.set_params(early_stopping=False)
        else:
            self._clf.set_params(early_stopping=True)

        self._clf.fit(X, y, sample_weight=sample_weights)
        self._fitted = True
        self._n_samples_seen = len(training_data)

        # 特征统计
        self.feature_stats = {
            name: {
                "min": float(X[:, i].min()),
                "max": float(X[:, i].max()),
                "mean": float(X[:, i].mean()),
            }
            for i, name in enumerate(FEATURE_NAMES)
        }

        # 训练准确率
        preds = self._clf.predict(X)
        self.training_accuracy = float((preds == y).mean() * 100)
        self.is_trained = True

        # Permutation importance（sklearn HGBC 无 feature_importances_）
        self._perm_importance = [0.0] * len(FEATURE_NAMES)
        try:
            from sklearn.inspection import permutation_importance as _pi
            _perm = _pi(self._clf, X, y, n_repeats=5, random_state=42, n_jobs=1)
            self._perm_importance = _perm.importances_mean.tolist()
        except Exception as _e_pi:
            _log.debug("Permutation importance 计算失败: %s", _e_pi)

        _n_iter = getattr(self._clf, "n_iter_", 0)
        _oos_txt = f"{self.oos_accuracy:.1f}%" if self.oos_accuracy is not None else "N/A(样本<60)"
        _log.info(
            "HGB 训练完成：%d 样本，%d 轮迭代，OOS准确率 %s（in-sample %.1f%% 仅参考），Top 特征 %s",
            len(training_data), _n_iter, _oos_txt, self.training_accuracy,
            sorted(self.weights.items(), key=lambda x: x[1], reverse=True)[:3],
        )

        return {
            "status": "success",
            "samples": len(training_data),
            "accuracy": self.training_accuracy,
            "oos_accuracy": self.oos_accuracy,  # v0.40.0: 真实泛化精度（时序外样本）
            "weights": self.weights,
            "n_iter": _n_iter,
        }

    def incremental_train(self, new_data: List[TrainingData]) -> Dict:
        """增量训练：树模型不支持真正的增量学习，降级为全量重训"""
        if not new_data:
            return {"status": "skip", "message": "no new data"}
        # 树模型的 warm_start 需要完整数据集（不像 SGD 的 partial_fit）
        # 正确做法：从 DB 拉全量数据重训
        try:
            all_data = build_training_data_from_db()
            if all_data and len(all_data) >= 2:
                result = self.train(all_data)
                result["incremental_note"] = "tree model: full retrain with DB data"
                return result
        except Exception as e:
            _log.warning("HGB incremental_train 全量重训失败: %s", e)
        return {"status": "fallback", "message": "incremental retrain failed"}

    # ---- 预测 ----
    def predict_probability(self, data: TrainingData) -> float:
        """预测赚钱概率 (0~1)，含小样本校准"""
        import numpy as np

        if not self._fitted:
            return 0.5

        X = np.array([_extract_features(data)], dtype=np.float64)
        prob = self._clf.predict_proba(X)[0]
        raw_prob = float(prob[1]) if len(prob) > 1 else float(prob[0])

        # 小样本校准（与 SGDMLModel 一致）
        MIN_CONFIDENT = 100
        confidence_ratio = min(self._n_samples_seen / MIN_CONFIDENT, 1.0)
        calibrated = raw_prob * confidence_ratio + 0.5 * (1 - confidence_ratio)

        return max(0.05, min(0.95, calibrated))

    def predict_return(self, data: TrainingData) -> Dict:
        """预测收益。公式唯一真相 = 模块级 `expected_returns()`（第三份重复已删）。"""
        return {
            "probability": self.predict_probability(data),
            **expected_returns(data),
            **_feature_quality(data),   # v0.45.3: 插补了哪几维，必须随结论一起走
        }

    # ---- 序列化 ----
    def save_model(self, filename: str = "ml_model.json"):
        """保存模型到 JSON（pickle base64 + 元数据）"""
        import base64
        import pickle

        model_data: Dict = {
            "model_type": "hgb",
            "feature_count": len(FEATURE_NAMES),
            "feature_names": FEATURE_NAMES,
            "is_trained": self.is_trained,
            "training_accuracy": self.training_accuracy,
            "oos_accuracy": self.oos_accuracy,  # v0.40.0: 时序外样本精度
            "n_samples_seen": self._n_samples_seen,
            "feature_stats": self.feature_stats,
            "weights": self.weights,
            "feature_importance": self.get_feature_importance(),
        }

        if self._fitted:
            # 树模型无法直接 JSON 序列化，用 pickle + base64
            model_bytes = pickle.dumps(self._clf)
            model_data["model_bytes"] = base64.b64encode(model_bytes).decode("ascii")
            model_data["perm_importance"] = getattr(self, "_perm_importance", [])

        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(model_data, f, indent=2, ensure_ascii=False)
            _log.info("HGB 模型已保存：%s", filename)
        except (OSError, TypeError) as e:
            _log.warning("HGB save_model 失败：%s", e)

    def load_model(self, filename: str = "ml_model.json") -> bool:
        """加载 HGB 模型（支持 pickle base64 恢复）"""
        import base64
        import pickle

        if not os.path.exists(filename):
            return False

        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False

        if data.get("model_type") != "hgb":
            _log.debug("模型格式不匹配（%s != hgb），需重新训练", data.get("model_type"))
            return False

        if data.get("feature_count", 0) != len(FEATURE_NAMES):
            _log.warning("特征维度不匹配（%d vs %d），需重新训练",
                         data.get("feature_count", 0), len(FEATURE_NAMES))
            return False

        # 恢复 pickle 模型
        model_bytes_str = data.get("model_bytes")
        if model_bytes_str:
            try:
                self._clf = pickle.loads(base64.b64decode(model_bytes_str))
                self._fitted = True
            except (pickle.UnpicklingError, ValueError, TypeError) as e:
                _log.warning("HGB 模型反序列化失败：%s", e)
                return False

        self.is_trained = data.get("is_trained", False)
        self.training_accuracy = data.get("training_accuracy", 0.0)
        self.oos_accuracy = data.get("oos_accuracy")  # v0.40.0
        self._n_samples_seen = data.get("n_samples_seen", 0)
        self.feature_stats = data.get("feature_stats", {})
        self._perm_importance = data.get("perm_importance", [0.0] * len(FEATURE_NAMES))

        _log.info("HGB 模型已加载：%d 样本，准确率 %.1f%%", self._n_samples_seen, self.training_accuracy)
        return True

    # ---- 特征重要性 ----
    def get_feature_importance(self) -> Dict:
        """返回特征重要性（permutation importance）"""
        n = len(FEATURE_NAMES)
        uniform = 1.0 / n
        if not self._fitted or not hasattr(self, "_perm_importance"):
            return {name: {"weight": uniform, "coefficient": 0.0, "direction": "neutral"}
                    for name in FEATURE_NAMES}

        imp = self._perm_importance
        abs_sum = sum(abs(v) for v in imp) or 1.0
        return {
            name: {
                "weight": float(imp[i]) / abs_sum,
                "coefficient": float(imp[i]),
                "direction": "positive" if imp[i] > 0.01 else "neutral",
            }
            for i, name in enumerate(FEATURE_NAMES)
        }

    def encode_catalyst_quality(self, quality: str) -> float:
        """兼容方法"""
        return _encode_catalyst(quality)


def create_ml_model():
    """工厂函数：优先 HGB → SGD → Simple"""
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: F401
        _log.info("使用 HistGradientBoosting 模型（sklearn 内置 LightGBM 等价）")
        return HGBModel()
    except ImportError:
        pass
    try:
        from sklearn.linear_model import SGDClassifier  # noqa: F401
        _log.info("HGB 不可用，降级使用 SGDMLModel")
        return SGDMLModel()
    except ImportError:
        _log.info("sklearn 不可用，降级使用 SimpleMLModel")
        return SimpleMLModel()


class MLPredictionService:
    """ML 预测服务"""

    def __init__(self):
        self.model = create_ml_model()
        self.data_builder = HistoricalDataBuilder()

    def train_model(self) -> Dict:
        """训练模型 — 优先使用真实数据，不足时降级到硬编码"""
        real_data = []
        try:
            from config import ML_TRAINING_CONFIG as _MTC
            if _MTC.get("use_real_data", True):
                real_data = build_training_data_from_db(
                    min_samples=_MTC.get("min_real_samples", 30),
                    max_rows=_MTC.get("max_training_rows", 500),
                )
        except (ImportError, OSError) as e:
            _log.debug("真实训练数据加载失败: %s", e)

        if real_data:
            _log.info("使用 %d 条真实数据训练 ML 模型", len(real_data))
            training_data = real_data
        else:
            training_data = self.data_builder.get_training_data()
            _log.info("真实数据不足，使用 %d 条硬编码数据", len(training_data))

        result = self.model.train(training_data)

        # 保存模型
        if result.get("status") == "success":
            self.model.save_model()

        return result

    def incremental_train(self, new_data: List[TrainingData]) -> Dict:
        """增量学习（仅 SGDMLModel 支持，SimpleMLModel 降级为全量重训）"""
        if hasattr(self.model, "incremental_train"):
            return self.model.incremental_train(new_data)
        # SimpleMLModel fallback: 追加数据后全量重训
        self.data_builder.historical_records.extend(new_data)
        return self.model.train(self.data_builder.get_training_data())

    def predict_for_opportunity(self, data: TrainingData) -> Dict:
        """为某个机会预测"""
        if not self.model.is_trained:
            self.train_model()

        # 防御性输入验证：crowding_score 必须在 [0, 100]
        # 上游若传入异常值（如 500），这里强制修正并记录警告
        if not (0.0 <= data.crowding_score <= 100.0):
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "predict_for_opportunity: crowding_score=%.1f 超出 [0,100] 范围，"
                "强制 clamp 到合法区间", data.crowding_score
            )
            from dataclasses import replace as _dc_replace
            data = _dc_replace(data, crowding_score=min(100.0, max(0.0, data.crowding_score)))

        prediction = self.model.predict_return(data)

        return {
            "ticker": data.ticker,
            "date": datetime.now().isoformat(),
            "input": {
                "crowding_score": data.crowding_score,
                "catalyst_quality": data.catalyst_quality,
                "momentum_5d": data.momentum_5d,
                "volatility": data.volatility,
                "market_sentiment": data.market_sentiment,
            },
            "prediction": prediction,
            "recommendation": self._generate_recommendation(prediction),
        }

    def _generate_recommendation(self, prediction: Dict) -> str:
        """生成推荐"""
        # v0.45.3: 特征缺太多时不给方向。原来无论插补多少维都照常输出
        # "STRONG BUY"，而插补值全是中性 → 概率被推向 0.5 → 稳定落进
        # "HOLD"，读起来像一个真实判断。
        if prediction.get("unreliable"):
            _n = len(prediction.get("imputed_features") or [])
            return f"NO CALL - 12 维特征缺 {_n} 个，数据不足以支撑结论"
        prob = prediction["probability"]

        if prob >= 0.75:
            return "STRONG BUY - 高概率机会"
        elif prob >= 0.65:
            return "BUY - 值得参与"
        elif prob >= 0.50:
            return "HOLD - 等待更好机会"
        else:
            return "AVOID - 风险大于收益"

    def get_model_info(self) -> Dict:
        """获取模型信息"""
        return {
            "is_trained": self.model.is_trained,
            "training_accuracy": self.model.training_accuracy,
            "weights": self.model.weights,
            "training_samples": len(self.data_builder.get_training_data()),
        }


# ==================== 脚本示例 ====================
if __name__ == "__main__":
    print("🤖 Alpha Hive ML 预测系统")
    print("=" * 60)

    # 创建服务
    service = MLPredictionService()

    # 训练模型
    print("\n📚 第 1 步：训练模型")
    print("-" * 60)
    result = service.train_model()
    print(json.dumps(result, indent=2))

    # 为新的机会做预测
    print("\n\n🔮 第 2 步：预测新机会")
    print("-" * 60)

    # 模拟一个新的交易机会
    new_opportunity = TrainingData(
        ticker="NVDA",
        date="2026-02-23",
        crowding_score=63.5,
        catalyst_quality="A",
        momentum_5d=6.8,
        volatility=4.8,
        market_sentiment=45,
        actual_return_3d=0,  # 未来数据
        actual_return_7d=0,
        actual_return_30d=0,
        win_3d=False,
        win_7d=False,
        win_30d=False,
    )

    prediction = service.predict_for_opportunity(new_opportunity)
    print(json.dumps(prediction, indent=2, default=str))

    # 显示模型信息
    print("\n\n📊 第 3 步：模型性能")
    print("-" * 60)
    info = service.get_model_info()
    print(f"训练状态：{'已训练' if info['is_trained'] else '未训练'}")
    print(f"训练准确率：{info['training_accuracy']:.1f}%")
    print(f"训练样本数：{info['training_samples']}")
    print(f"\n特征权重：")
    for feature, weight in info['weights'].items():
        print(f"  • {feature}: {weight:.1%}")

    print("\n" + "=" * 60)
    print("✅ ML 预测演示完成！")
    print("=" * 60)
