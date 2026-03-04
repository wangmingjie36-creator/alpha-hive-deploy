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


class HistoricalDataBuilder:
    """构建训练数据集"""

    def __init__(self):
        # 收集的历史交易记录
        self.historical_records: List[TrainingData] = [
            # NVDA 记录
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
            # VKTX 记录
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
            # TSLA 记录
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
            # 负例（失败的交易）
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
            }
            for r in self.historical_records
        ]

        with open(filename, "w") as f:
            json.dump(data, f, indent=2)


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

        correlations["crowding"] = self._simple_correlation(
            crowding_vals, target_numeric
        )
        correlations["catalyst"] = self._simple_correlation(
            catalyst_vals, target_numeric
        )
        correlations["momentum"] = self._simple_correlation(
            momentum_vals, target_numeric
        )
        correlations["volatility"] = self._simple_correlation(
            volatility_vals, target_numeric
        )
        correlations["sentiment"] = self._simple_correlation(
            sentiment_vals, target_numeric
        )

        return correlations

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
        # 特征归一化
        crowding_norm = self.normalize_feature(
            data.crowding_score,
            self.feature_stats["crowding"]["min"],
            self.feature_stats["crowding"]["max"],
        )
        catalyst_norm = self.normalize_feature(
            self.encode_catalyst_quality(data.catalyst_quality),
            self.feature_stats["catalyst"]["min"],
            self.feature_stats["catalyst"]["max"],
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

        # 计算预测概率（反向拥挤度：拥挤度越高，概率越低）
        crowding_score = 1.0 - crowding_norm * 0.3

        # 加权求和
        probability = (
            self.weights["crowding"] * crowding_score
            + self.weights["catalyst"] * catalyst_norm
            + self.weights["momentum"] * (momentum_norm + 0.5)  # 正动量更好
            + self.weights["volatility"] * (1.0 - volatility_norm * 0.5)  # 适度波动
            + self.weights["sentiment"] * (sentiment_norm + 0.5)  # 正情绪更好
        )

        return max(0.0, min(1.0, probability))

    def predict_return(self, data: TrainingData) -> Dict:
        """预测收益"""
        probability = self.predict_probability(data)

        # 基于催化剂质量和其他因素预测收益
        catalyst_bonus = {
            "A+": 25,
            "A": 20,
            "B+": 15,
            "B": 10,
            "C": 5,
        }.get(data.catalyst_quality, 10)

        momentum_bonus = data.momentum_5d  # 动量直接加到收益
        crowding_penalty = data.crowding_score * 0.1  # 拥挤度降低预期收益

        # 预测 3 日、7 日、30 日收益
        expected_7d = catalyst_bonus + momentum_bonus - crowding_penalty

        return {
            "probability": probability,
            "expected_3d": expected_7d * 0.3,
            "expected_7d": expected_7d * 0.8,
            "expected_30d": expected_7d * 1.2,
        }

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


class MLPredictionService:
    """ML 预测服务"""

    def __init__(self):
        self.model = SimpleMLModel()
        self.data_builder = HistoricalDataBuilder()

    def train_model(self) -> Dict:
        """训练模型"""
        training_data = self.data_builder.get_training_data()
        result = self.model.train(training_data)

        # 保存模型
        if result.get("status") == "success":
            self.model.save_model()

        return result

    def predict_for_opportunity(self, data: TrainingData) -> Dict:
        """为某个机会预测"""
        if not self.model.is_trained:
            self.train_model()

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
