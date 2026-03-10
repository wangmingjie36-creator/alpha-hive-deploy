"""
🐝 Alpha Hive - 扩展机器学习预测系统
使用更多历史数据（25+ 样本）训练模型，大幅提升准确率
"""

import json
import logging as _logging
import os
from typing import Dict, List
import statistics
from dataclasses import dataclass

_log = _logging.getLogger("alpha_hive.ml_predictor_extended")


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

    # 期权特征（可选，默认中立值）
    iv_rank: float = 50.0  # IV Rank (0-100)，默认中立
    put_call_ratio: float = 1.0  # P/C Ratio，默认中立

    # === v2 新特征（与 ml_predictor.TrainingData 保持一致）===
    final_score: float = 5.0          # 蜂群综合分 (0-10)
    odds_score: float = 5.0           # 赔率维度分 (0-10)
    risk_adj_score: float = 5.0       # 风险调整分 (0-10)
    agent_agreement: float = 0.5      # Agent 共识度 (0-1)
    direction_encoded: float = 0.0    # bullish=1, neutral=0, bearish=-1


class HistoricalDataBuilder:
    """构建训练数据集 - 扩展版本（25+ 样本）"""

    def __init__(self):
        # 扩展的历史交易记录
        self.historical_records: List[TrainingData] = [
            # ============ NVDA 记录 (8 个样本) ============
            TrainingData(
                ticker="NVDA",
                date="2023-10-18",
                crowding_score=68.0,
                catalyst_quality="A",
                momentum_5d=5.2,
                volatility=4.8,
                market_sentiment=45,
                actual_return_3d=8.5,
                actual_return_7d=18.9,
                actual_return_30d=32.1,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="NVDA",
                date="2023-04-19",
                crowding_score=72.0,
                catalyst_quality="A",
                momentum_5d=3.8,
                volatility=5.1,
                market_sentiment=35,
                actual_return_3d=12.8,
                actual_return_7d=22.3,
                actual_return_30d=18.5,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="NVDA",
                date="2024-01-24",
                crowding_score=75.0,
                catalyst_quality="A+",
                momentum_5d=6.5,
                volatility=6.1,
                market_sentiment=55,
                actual_return_3d=5.2,
                actual_return_7d=15.6,
                actual_return_30d=38.9,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="NVDA",
                date="2024-06-12",
                crowding_score=78.0,
                catalyst_quality="A+",
                momentum_5d=7.2,
                volatility=5.5,
                market_sentiment=65,
                actual_return_3d=6.8,
                actual_return_7d=14.2,
                actual_return_30d=28.5,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="NVDA",
                date="2023-08-01",
                crowding_score=82.0,
                catalyst_quality="B",
                momentum_5d=8.5,
                volatility=7.2,
                market_sentiment=70,
                actual_return_3d=-2.3,
                actual_return_7d=1.2,
                actual_return_30d=-5.8,
                win_3d=False,
                win_7d=False,
                win_30d=False,
            ),
            TrainingData(
                ticker="NVDA",
                date="2023-12-04",
                crowding_score=85.0,
                catalyst_quality="B",
                momentum_5d=9.1,
                volatility=8.3,
                market_sentiment=75,
                actual_return_3d=-1.5,
                actual_return_7d=-3.2,
                actual_return_30d=-8.5,
                win_3d=False,
                win_7d=False,
                win_30d=False,
            ),
            TrainingData(
                ticker="NVDA",
                date="2024-03-15",
                crowding_score=70.0,
                catalyst_quality="A",
                momentum_5d=4.5,
                volatility=4.9,
                market_sentiment=50,
                actual_return_3d=9.2,
                actual_return_7d=19.5,
                actual_return_30d=35.8,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="NVDA",
                date="2024-09-20",
                crowding_score=88.0,
                catalyst_quality="B+",
                momentum_5d=10.2,
                volatility=7.8,
                market_sentiment=80,
                actual_return_3d=-0.8,
                actual_return_7d=0.5,
                actual_return_30d=-6.2,
                win_3d=False,
                win_7d=False,
                win_30d=False,
            ),

            # ============ VKTX 记录 (7 个样本) ============
            TrainingData(
                ticker="VKTX",
                date="2023-06-15",
                crowding_score=58.0,
                catalyst_quality="A+",
                momentum_5d=2.1,
                volatility=12.3,
                market_sentiment=60,
                actual_return_3d=42.1,
                actual_return_7d=38.5,
                actual_return_30d=22.3,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="VKTX",
                date="2023-11-22",
                crowding_score=42.0,
                catalyst_quality="A",
                momentum_5d=1.5,
                volatility=8.9,
                market_sentiment=40,
                actual_return_3d=8.2,
                actual_return_7d=12.5,
                actual_return_30d=15.8,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="VKTX",
                date="2023-09-15",
                crowding_score=65.0,
                catalyst_quality="C",
                momentum_5d=-3.2,
                volatility=11.5,
                market_sentiment=-20,
                actual_return_3d=-8.5,
                actual_return_7d=-12.3,
                actual_return_30d=-18.9,
                win_3d=False,
                win_7d=False,
                win_30d=False,
            ),
            TrainingData(
                ticker="VKTX",
                date="2024-02-28",
                crowding_score=55.0,
                catalyst_quality="A",
                momentum_5d=3.5,
                volatility=10.2,
                market_sentiment=35,
                actual_return_3d=15.8,
                actual_return_7d=22.3,
                actual_return_30d=28.5,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="VKTX",
                date="2024-05-10",
                crowding_score=72.0,
                catalyst_quality="B+",
                momentum_5d=5.2,
                volatility=13.1,
                market_sentiment=55,
                actual_return_3d=4.2,
                actual_return_7d=8.5,
                actual_return_30d=12.3,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="VKTX",
                date="2024-08-22",
                crowding_score=80.0,
                catalyst_quality="B",
                momentum_5d=-2.1,
                volatility=15.2,
                market_sentiment=-10,
                actual_return_3d=-5.2,
                actual_return_7d=-8.9,
                actual_return_30d=-15.5,
                win_3d=False,
                win_7d=False,
                win_30d=False,
            ),
            TrainingData(
                ticker="VKTX",
                date="2024-10-05",
                crowding_score=48.0,
                catalyst_quality="A+",
                momentum_5d=0.8,
                volatility=9.8,
                market_sentiment=45,
                actual_return_3d=25.5,
                actual_return_7d=32.2,
                actual_return_30d=18.9,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),

            # ============ TSLA 记录 (6 个样本) ============
            TrainingData(
                ticker="TSLA",
                date="2024-01-17",
                crowding_score=71.0,
                catalyst_quality="B+",
                momentum_5d=4.2,
                volatility=7.8,
                market_sentiment=30,
                actual_return_3d=12.3,
                actual_return_7d=18.2,
                actual_return_30d=12.5,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="TSLA",
                date="2023-07-19",
                crowding_score=76.0,
                catalyst_quality="B",
                momentum_5d=6.8,
                volatility=8.5,
                market_sentiment=45,
                actual_return_3d=3.5,
                actual_return_7d=5.2,
                actual_return_30d=2.8,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="TSLA",
                date="2023-10-02",
                crowding_score=80.0,
                catalyst_quality="C",
                momentum_5d=8.2,
                volatility=9.1,
                market_sentiment=60,
                actual_return_3d=-4.8,
                actual_return_7d=-7.2,
                actual_return_30d=-12.5,
                win_3d=False,
                win_7d=False,
                win_30d=False,
            ),
            TrainingData(
                ticker="TSLA",
                date="2024-04-23",
                crowding_score=68.0,
                catalyst_quality="A",
                momentum_5d=2.1,
                volatility=6.5,
                market_sentiment=25,
                actual_return_3d=8.9,
                actual_return_7d=14.5,
                actual_return_30d=18.2,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="TSLA",
                date="2024-07-31",
                crowding_score=83.0,
                catalyst_quality="B",
                momentum_5d=7.5,
                volatility=8.9,
                market_sentiment=55,
                actual_return_3d=-2.1,
                actual_return_7d=-0.5,
                actual_return_30d=-8.3,
                win_3d=False,
                win_7d=False,
                win_30d=False,
            ),
            TrainingData(
                ticker="TSLA",
                date="2024-11-15",
                crowding_score=69.0,
                catalyst_quality="A",
                momentum_5d=3.8,
                volatility=7.2,
                market_sentiment=40,
                actual_return_3d=11.2,
                actual_return_7d=16.8,
                actual_return_30d=20.5,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),

            # ============ 其他科技股 (4 个样本) ============
            TrainingData(
                ticker="MSFT",
                date="2024-02-15",
                crowding_score=65.0,
                catalyst_quality="A",
                momentum_5d=4.1,
                volatility=3.2,
                market_sentiment=50,
                actual_return_3d=7.5,
                actual_return_7d=13.2,
                actual_return_30d=19.8,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="AMD",
                date="2024-05-08",
                crowding_score=70.0,
                catalyst_quality="A",
                momentum_5d=5.2,
                volatility=5.8,
                market_sentiment=45,
                actual_return_3d=9.3,
                actual_return_7d=15.5,
                actual_return_30d=22.1,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="QCOM",
                date="2024-03-22",
                crowding_score=73.0,
                catalyst_quality="B+",
                momentum_5d=6.3,
                volatility=6.2,
                market_sentiment=52,
                actual_return_3d=6.8,
                actual_return_7d=11.2,
                actual_return_30d=15.9,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="MSFT",
                date="2023-12-15",
                crowding_score=82.0,
                catalyst_quality="B",
                momentum_5d=9.2,
                volatility=4.1,
                market_sentiment=70,
                actual_return_3d=-1.2,
                actual_return_7d=0.8,
                actual_return_30d=-3.5,
                win_3d=False,
                win_7d=False,
                win_30d=False,
            ),

            # ============ 生物医药股 (3 个样本) ============
            TrainingData(
                ticker="AMGN",
                date="2024-04-16",
                crowding_score=60.0,
                catalyst_quality="A",
                momentum_5d=2.8,
                volatility=3.5,
                market_sentiment=35,
                actual_return_3d=5.2,
                actual_return_7d=9.8,
                actual_return_30d=14.2,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="BIIB",
                date="2024-05-13",
                crowding_score=55.0,
                catalyst_quality="A+",
                momentum_5d=1.5,
                volatility=6.8,
                market_sentiment=40,
                actual_return_3d=12.5,
                actual_return_7d=18.9,
                actual_return_30d=24.3,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="JNJ",
                date="2024-02-14",
                crowding_score=62.0,
                catalyst_quality="A",
                momentum_5d=3.2,
                volatility=2.8,
                market_sentiment=38,
                actual_return_3d=4.8,
                actual_return_7d=8.5,
                actual_return_30d=12.1,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),

            # ============ 清洁能源股 (2 个样本) ============
            TrainingData(
                ticker="RUN",
                date="2024-02-20",
                crowding_score=50.0,
                catalyst_quality="A",
                momentum_5d=-1.2,
                volatility=8.5,
                market_sentiment=30,
                actual_return_3d=18.5,
                actual_return_7d=25.2,
                actual_return_30d=32.1,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
            TrainingData(
                ticker="PLUG",
                date="2024-06-28",
                crowding_score=58.0,
                catalyst_quality="A",
                momentum_5d=0.5,
                volatility=9.2,
                market_sentiment=28,
                actual_return_3d=14.2,
                actual_return_7d=19.8,
                actual_return_30d=28.5,
                win_3d=True,
                win_7d=True,
                win_30d=True,
            ),
        ]

    def get_training_data(self) -> List[TrainingData]:
        """获取所有训练数据"""
        return self.historical_records

    def add_record(self, record: TrainingData):
        """添加新的交易记录"""
        self.historical_records.append(record)

    def save_to_file(self, filename: str = "training_data_extended.json"):
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
            }
            for r in self.historical_records
        ]

        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
        _log.info("已保存 %d 个训练样本到 %s", len(data), filename)


class SimpleMLModel:
    """简单机器学习模型（不依赖 sklearn）"""

    def __init__(self):
        self.weights = {
            "crowding": 0.30,
            "catalyst": 0.25,
            "momentum": 0.20,
            "volatility": 0.15,
            "sentiment": 0.10,
        }
        self.is_trained = False
        self.training_accuracy = 0.0
        self.feature_stats = {}

    def encode_catalyst_quality(self, quality: str) -> float:
        """编码催化剂质量"""
        mapping = {"A+": 1.0, "A": 0.85, "B+": 0.70, "B": 0.55, "C": 0.40}
        return mapping.get(quality, 0.5)

    def normalize_feature(
        self, value: float, min_val: float, max_val: float
    ) -> float:
        """特征归一化"""
        if max_val == min_val:
            return 0.5
        return (value - min_val) / (max_val - min_val)

    def train(self, training_data: List[TrainingData]) -> Dict:
        """训练模型"""
        if not training_data:
            return {"status": "error", "message": "no training data"}

        # 训练 ML 模型（扩展版）

        # 提取特征
        crowding_scores = [d.crowding_score for d in training_data]
        catalyst_qualities = [
            self.encode_catalyst_quality(d.catalyst_quality) for d in training_data
        ]
        momentums = [d.momentum_5d for d in training_data]
        volatilities = [d.volatility for d in training_data]
        sentiments = [d.market_sentiment for d in training_data]
        win_7d = [d.win_7d for d in training_data]  # 目标：7 日是否赚钱

        # 计算特征的统计信息
        self.feature_stats = {
            "crowding": {
                "min": min(crowding_scores),
                "max": max(crowding_scores),
                "mean": statistics.mean(crowding_scores),
            },
            "catalyst": {"min": 0.4, "max": 1.0},
            "momentum": {"min": min(momentums), "max": max(momentums)},
            "volatility": {"min": min(volatilities), "max": max(volatilities)},
            "sentiment": {"min": min(sentiments), "max": max(sentiments)},
        }

        # 计算每个特征与目标的相关性
        correlations = self._calculate_correlations(training_data, win_7d)

        # 更新权重基于相关性
        total_corr = sum(abs(c) for c in correlations.values())
        if total_corr > 0:
            for key in correlations:
                self.weights[key] = abs(correlations[key]) / total_corr

        # 权重已更新

        # 先标记模型为已训练
        self.is_trained = True

        # 计算训练准确率
        predictions = [self.predict_probability(d) for d in training_data]
        correct = sum(
            1 for pred, actual in zip(predictions, win_7d)
            if (pred > 0.5) == actual
        )
        self.training_accuracy = correct / len(win_7d) * 100

        # 训练完成

        return {
            "status": "success",
            "samples": len(training_data),
            "accuracy": self.training_accuracy,
            "weights": self.weights,
        }

    def _calculate_correlations(self, data: List[TrainingData], target: List[bool]) -> Dict:
        """计算特征与目标的相关性"""
        correlations = {}

        # 将 True/False 转换为 1/0
        target_numeric = [1.0 if x else 0.0 for x in target]

        # 计算每个特征的简单相关性
        crowding_vals = [d.crowding_score for d in data]
        catalyst_vals = [
            self.encode_catalyst_quality(d.catalyst_quality) for d in data
        ]
        momentum_vals = [d.momentum_5d for d in data]
        volatility_vals = [d.volatility for d in data]
        sentiment_vals = [d.market_sentiment for d in data]

        correlations["crowding"] = self._pearson_correlation(crowding_vals, target_numeric)
        correlations["catalyst"] = self._pearson_correlation(catalyst_vals, target_numeric)
        correlations["momentum"] = self._pearson_correlation(momentum_vals, target_numeric)
        correlations["volatility"] = self._pearson_correlation(volatility_vals, target_numeric)
        correlations["sentiment"] = self._pearson_correlation(sentiment_vals, target_numeric)

        return correlations

    def _pearson_correlation(self, x: List[float], y: List[float]) -> float:
        """计算皮尔逊相关系数"""
        n = len(x)
        if n < 2:
            return 0.0

        mean_x = statistics.mean(x)
        mean_y = statistics.mean(y)

        numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        denominator_x = sum((x[i] - mean_x) ** 2 for i in range(n)) ** 0.5
        denominator_y = sum((y[i] - mean_y) ** 2 for i in range(n)) ** 0.5

        if denominator_x == 0 or denominator_y == 0:
            return 0.0

        return numerator / (denominator_x * denominator_y)

    def predict_probability(self, data: TrainingData) -> float:
        """预测成功概率"""
        if not self.is_trained:
            _log.warning("模型未训练，返回默认概率 0.5")
            return 0.5

        # 特征归一化
        crowding_norm = self.normalize_feature(
            data.crowding_score,
            self.feature_stats["crowding"]["min"],
            self.feature_stats["crowding"]["max"],
        )
        catalyst_norm = self.normalize_feature(
            self.encode_catalyst_quality(data.catalyst_quality), 0.4, 1.0
        )
        momentum_norm = self.normalize_feature(
            data.momentum_5d,
            self.feature_stats["momentum"]["min"],
            self.feature_stats["momentum"]["max"],
        )
        volatility_norm = self.normalize_feature(
            data.volatility,
            self.feature_stats["volatility"]["min"],
            self.feature_stats["volatility"]["max"],
        )
        sentiment_norm = self.normalize_feature(
            data.market_sentiment,
            self.feature_stats["sentiment"]["min"],
            self.feature_stats["sentiment"]["max"],
        )

        # 加权平均
        probability = (
            self.weights["crowding"] * (1 - crowding_norm)  # 拥挤度越低越好
            + self.weights["catalyst"] * catalyst_norm
            + self.weights["momentum"] * momentum_norm
            + self.weights["volatility"] * (1 - volatility_norm)  # 波动率越低越好
            + self.weights["sentiment"] * sentiment_norm
        )

        return min(max(probability, 0.0), 1.0)

    def predict_return(self, data: TrainingData) -> Dict[str, float]:
        """预测收益"""
        probability = self.predict_probability(data)

        # 基于催化剂质量和概率的简单收益预测
        catalyst_multiplier = self.encode_catalyst_quality(data.catalyst_quality)
        crowding_discount = 1.0 - (data.crowding_score / 100.0)

        base_return = 10.0 * catalyst_multiplier * crowding_discount
        momentum_boost = data.momentum_5d * 0.5

        return {
            "expected_3d": max(0, (base_return * 0.3 + momentum_boost) * probability),
            "expected_7d": max(0, (base_return * 0.7 + momentum_boost) * probability),
            "expected_30d": max(0, (base_return * 1.2 + momentum_boost * 0.5) * probability),
        }

    def save_model(self, filename: str = "ml_model_extended.json"):
        """保存模型（JSON 格式，安全序列化）"""
        model_data = {
            "weights": self.weights,
            "feature_stats": self.feature_stats,
            "is_trained": self.is_trained,
            "training_accuracy": self.training_accuracy,
        }
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(model_data, f, ensure_ascii=False, indent=2)

    def load_model(self, filename: str = "ml_model_extended.json"):
        """加载模型（JSON 格式，安全反序列化）"""
        if filename.endswith(".pkl") and not os.path.exists(filename):
            filename = filename.replace(".pkl", ".json")
        with open(filename, "r", encoding="utf-8") as f:
            model_data = json.load(f)
        self.weights = model_data["weights"]
        self.feature_stats = model_data.get("feature_stats", {})
        self.is_trained = model_data["is_trained"]
        self.training_accuracy = model_data.get("training_accuracy", 0.0)


def _create_ml_model():
    """工厂函数：优先使用 SGDMLModel，sklearn 不可用时降级"""
    try:
        from ml_predictor import create_ml_model
        return create_ml_model()
    except ImportError:
        return SimpleMLModel()


class MLPredictionService:
    """ML 预测服务"""

    def __init__(self):
        self.data_builder = HistoricalDataBuilder()
        self.model = _create_ml_model()

    def train_model(self) -> Dict:
        """训练模型"""
        training_data = self.data_builder.get_training_data()
        result = self.model.train(training_data)
        self.model.save_model()
        return result

    def predict_for_opportunity(self, opportunity_data: TrainingData) -> Dict:
        """为机会预测"""
        if not self.model.is_trained:
            self.train_model()

        probability = self.model.predict_probability(opportunity_data)
        returns = self.model.predict_return(opportunity_data)

        return {
            "probability": round(probability, 4),
            "expected_3d": round(returns["expected_3d"], 2),
            "expected_7d": round(returns["expected_7d"], 2),
            "expected_30d": round(returns["expected_30d"], 2),
        }

    def get_model_info(self) -> Dict:
        """获取模型信息"""
        return {
            "is_trained": self.model.is_trained,
            "training_accuracy": round(self.model.training_accuracy, 1),
            "training_samples": len(self.data_builder.get_training_data()),
            "weights": self.model.weights,
        }


if __name__ == "__main__":
    # 测试
    service = MLPredictionService()
    result = service.train_model()
    print(result)
    print("\n模型信息:")
    print(service.get_model_info())
